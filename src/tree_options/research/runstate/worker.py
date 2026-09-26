r"""Bounded research worker: claim queued runs, compute, publish
immutable results (RL1-03 correction; RL-2 scenario forks extend the
same single-thread loop).

The 2026-09-25 audit found a spool that never emptied: POST wrote
``queued`` records and every GET of ``/runs/{id}/result`` recomputed
``run_comparison`` on the fly while the stored status stayed ``queued``
forever — two GETs caused two engine invocations, and nothing about a
result was ever immutable or bound to its inputs.

This worker owns the lifecycle::

    queued -> running -> completed (immutable result published)
                     \-> failed    (error recorded, honestly)

Design points the audit demanded:
    * SINGLE-WORKER design (one thread per process): claiming is a
      ``replace`` to ``running``; there is no cross-process CAS. The
      HTTP layer never computes — GET only reads recorded state.
    * The completed result is IMMUTABLE and content-bound: it records
      the engine identity (sha over the comparison modules' source
      bytes), the resolved input snapshot (per-candidate artifact
      hashes) and the calendar sha, so a later source change cannot
      masquerade as the old result — the stored bytes are what they
      were, and a rerun under changed inputs is a NEW run.
    * Interrupted runs (process death between ``running`` and terminal)
      are re-queued at worker start: recompute is deterministic and the
      result put is idempotent.
    * Pre-custody-format runs (records written by the pre-RL1-03 code,
      which stored metadata inside the immutable spec payload) are
      marked ``blocked`` — never erased, never silently rerun.

RL-2 extends this worker with the same lifecycle for SCENARIO runs
(a run record with ``kind == "scenario"``). The scenario's spec is
parsed from the stored immutable payload under ``spec`` (the canonical
diff body) just like a comparison spec; the parent is read out of the
runstate store via the lineage table; the engine forks the parent's
result and publishes the child's content-bound artifact under the
child's own run_id. Status / failure / requeue rules are unchanged.

Workers are unit-testable without threads: ``step()`` claims at most
one queued run synchronously.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from tree_options.research.comparison.engine import run_comparison
from tree_options.research.comparison.plan import resolve_plan
from tree_options.research.contracts import ResearchCandidate
from tree_options.research.runstate.spec_hash import spec_hash as make_spec_hash
from tree_options.research.runstate.store import (
    RunstateStoreError,
    open_runstate_store,
)
from tree_options.research.scenarios.contracts import (
    scenario_diff_sha256,
    scenario_spec_hash,
)
from tree_options.research.scenarios.engine import fork_parent_and_replay
from tree_options.research.scenarios.lineage import (
    ChildRef,
    store_parent_ref,
)
from tree_options.research.scenarios.spec_io import scenario_from_dict
from tree_options.research.spec_io import spec_from_dict

#: Run-record format written by this module; anything else in the store
#: predates the custody correction and is blocked, not interpreted.
RUN_FORMAT_VERSION = 2

#: Engine identity: sha256 over the comparison modules' source bytes —
#: the "engine/code version" bound into every result receipt. RL-2
#: adds the scenarios engine to the same identity surface so a
#: scenario result's ``engine_sha`` matches the comparison engine
#: bytes that produced it (scenarios reuse run_comparison verbatim).
_ENGINE_MODULES = (
    "tree_options.research.comparison.funded",
    "tree_options.research.comparison.engine",
    "tree_options.research.comparison.plan",
    "tree_options.research.comparison.calendar",
    "tree_options.research.comparison.drawdown",
    "tree_options.research.comparison.pair",
    "tree_options.research.scenarios.engine",
    "tree_options.research.scenarios.lineage",
    "tree_options.research.scenarios.contracts",
)


def engine_identity_sha() -> str:
    import importlib

    h = hashlib.sha256()
    for mod_name in _ENGINE_MODULES:
        mod = importlib.import_module(mod_name)
        h.update(Path(mod.__file__).read_bytes())  # type: ignore[arg-type]
    return h.hexdigest()


def input_snapshot_sha(candidates: tuple[ResearchCandidate, ...],
                       baseline: ResearchCandidate | None,
                       calendar_sha: str) -> tuple[dict[str, Any], str]:
    """The resolved input identity of a computation: each candidate's
    artifact hashes and source, plus the calendar the plan pinned."""
    snapshot: dict[str, Any] = {
        "candidates": {
            c.id: {"artifact_hashes": dict(c.artifact_hashes),
                   "source_url": c.source_url,
                   "evidence_kind": c.evidence_kind.value,
                   "version": c.version}
            for c in candidates
        },
        "calendar_sha256": calendar_sha,
    }
    if baseline is not None:
        snapshot["baseline"] = {
            "id": baseline.id,
            "artifact_hashes": dict(baseline.artifact_hashes),
            "source_url": baseline.source_url,
        }
    canonical = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), default=str)
    return snapshot, hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ResearchWorker:
    """Single-worker run executor for one research workspace."""

    def __init__(
        self,
        *,
        workspace: Path,
        catalog_provider: Callable[[], list[ResearchCandidate]],
        engine_fn: Callable[..., Any] = run_comparison,
    ) -> None:
        self.workspace = workspace
        self.catalog_provider = catalog_provider
        self.engine_fn = engine_fn
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # -- synchronous stepping (tests and the thread share this) ----------

    def step(self) -> bool:
        """Claim and process at most one queued run. Returns True when
        a run was processed (so tests can drive exact counts and the
        thread loop can idle politely)."""
        with open_runstate_store(self.workspace) as store:
            claim = self._next_queued(store)
            if claim is None:
                return False
            run_id, run = claim
            self._process(store, run_id, run)
            return True

    def _next_queued(self, store: Any) -> tuple[str, dict[str, Any]] | None:
        queued = [
            (at, payload)
            for payload, at in store.all_at("run")
            if isinstance(payload, dict)
            and payload.get("format_version") == RUN_FORMAT_VERSION
            and payload.get("status") == "queued"
            and isinstance(payload.get("run_id"), str)
        ]
        if not queued:
            return None
        queued.sort(key=lambda item: item[0])
        _, payload = queued[0]
        return payload["run_id"], payload

    def _process(self, store: Any, run_id: str, run: dict[str, Any]) -> None:
        now = datetime.now().isoformat()
        store.replace("run", {**run, "status": "running", "started_at": now},
                      key=run_id)
        try:
            spec_payload = store.get("spec", run_id)
            if spec_payload is None:
                raise RunstateStoreError(f"run {run_id} has no stored spec")
            kind = run.get("kind", "comparison")
            if kind == "scenario":
                result_payload = self._compute_scenario(
                    store, run_id, spec_payload, run)
            else:
                result_payload = self._compute(run_id, spec_payload)
        except Exception as exc:
            store.replace(
                "run",
                {**run, "status": "failed", "error": f"{type(exc).__name__}: {exc}",
                 "completed_at": datetime.now().isoformat()},
                key=run_id,
            )
            return
        store.put("result", result_payload, key=run_id)
        replace_fields = {
            "completed_at": datetime.now().isoformat(),
            "result_sha256": result_payload["result_sha256"],
            "engine_sha256": result_payload["engine_sha256"],
        }
        # input_snapshot_sha is OPTIONAL on the stored result: refusal
        # envelopes (no parent / typed refusal) don't bind an input
        # snapshot — they're honest "no computation happened" records.
        if "input_snapshot_sha256" in result_payload:
            replace_fields["input_snapshot_sha256"] = (
                result_payload["input_snapshot_sha256"])
        if "scenario_diff_sha256" in result_payload:
            replace_fields["scenario_diff_sha256"] = (
                result_payload["scenario_diff_sha256"])
        if "parent_run_id" in result_payload:
            replace_fields["parent_run_id"] = result_payload["parent_run_id"]
        store.replace(
            "run",
            {**run, "status": "completed", **replace_fields},
            key=run_id,
        )

    def _compute(self, run_id: str, spec_payload: dict[str, Any]) -> dict[str, Any]:
        """Parse the stored canonical spec, resolve candidates from the
        live catalog, run the engine, and bind the result to its
        inputs. Any failure raises — the caller records a failed run."""
        spec = spec_from_dict(spec_payload)
        catalog = {c.id: c for c in self.catalog_provider()}
        missing = [cid for cid in spec.candidate_ids if cid not in catalog]
        if missing:
            raise RunstateStoreError(
                f"candidates left the catalog since submission: {missing}")
        cands = tuple(catalog[cid] for cid in spec.candidate_ids)
        baseline = None
        if spec.benchmark_candidate_id:
            if spec.benchmark_candidate_id not in catalog:
                raise RunstateStoreError(
                    f"benchmark left the catalog: {spec.benchmark_candidate_id}")
            baseline = catalog[spec.benchmark_candidate_id]
        plan = resolve_plan(spec)
        if plan.refused:
            raise RunstateStoreError(
                f"plan refused at compute time: {plan.refusal_reason}")
        result = self.engine_fn(spec, cands, baseline=baseline)
        wire = result.to_wire()
        result_sha = hashlib.sha256(
            json.dumps(wire, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        snapshot, snapshot_sha = input_snapshot_sha(
            cands, baseline, plan.calendar_sha256)
        return {
            "run_id": run_id,
            "spec_hash": make_spec_hash(spec),
            "format_version": RUN_FORMAT_VERSION,
            "engine_sha256": engine_identity_sha(),
            "input_snapshot": snapshot,
            "input_snapshot_sha256": snapshot_sha,
            "calendar_sha256": plan.calendar_sha256,
            "result_sha256": result_sha,
            "wire": wire,
        }

    def _compute_scenario(self, store: Any, run_id: str,
                          spec_payload: dict[str, Any],
                          run: dict[str, Any]) -> dict[str, Any]:
        """Fork the parent run, replay the diff via the scenario
        engine, persist the parent_ref on first fork, and publish the
        child's content-bound result.

        The ``parent_run_id`` lives on the run record (not the spec)
        because the spec is body content parsed verbatim at the HTTP
        layer; the URL path is the parent identifier. The parent's
        stored ``result`` envelope is reconstructed from
        ``store.get("result", parent_run_id)``.
        """
        parent_run_id = run.get("parent_run_id")
        if not isinstance(parent_run_id, str) or not parent_run_id:
            raise RunstateStoreError(
                "scenario run is missing parent_run_id")
        spec = scenario_from_dict(parent_run_id, spec_payload)
        # The parent's stored result envelope — the source of truth
        # for identity matching (the parent's wire payload carries
        # the engine/input/calendar shas that must still match).
        parent_envelope_payload = store_get_cached(
            self.workspace, "result", parent_run_id)
        if parent_envelope_payload is None:
            # Mirrors the comparison engine's "missing parent" shape;
            # the spec holder refusal is rendered into the result
            # wire so the SPA renders the honest blocker.
            from tree_options.research.scenarios.refusal_codes import (
                SCENARIO_PARENT_MISSING,
                ScenarioRefusal,
            )
            refusal = ScenarioRefusal(
                code=SCENARIO_PARENT_MISSING,
                message="parent run has no stored result record",
            )
            return _refusal_result(
                run_id, spec, refusal,
                spec_hash=scenario_spec_hash(spec),
                format_version=RUN_FORMAT_VERSION,
                engine_sha256=engine_identity_sha(),
            )
        parent_envelope = parent_envelope_payload.get("wire")
        if not isinstance(parent_envelope, dict):
            from tree_options.research.scenarios.refusal_codes import (
                SCENARIO_PARENT_MISSING,
                ScenarioRefusal,
            )
            refusal = ScenarioRefusal(
                code=SCENARIO_PARENT_MISSING,
                message="parent run result envelope is malformed")
            return _refusal_result(
                run_id, spec, refusal,
                spec_hash=scenario_spec_hash(spec),
                format_version=RUN_FORMAT_VERSION,
                engine_sha256=engine_identity_sha(),
            )
        # The parent's TERMINAL status lives on the run record, not the
        # result envelope; ``parent_missing`` needs the run.status to
        # decide whether the fork can proceed.
        parent_run_record = store.get("run", parent_run_id)
        parent_status = (
            parent_run_record.get("status")
            if isinstance(parent_run_record, dict) else None)
        # Parent envelope identity fields are read off the parent's
        # top-level result record (where the worker writes them via
        # the ``result`` kind), not inside ``wire``.
        parent_identity = {
            "engine_sha256": parent_envelope_payload.get("engine_sha256"),
            "input_snapshot_sha256":
                parent_envelope_payload.get("input_snapshot_sha256"),
            "calendar_sha256":
                parent_envelope_payload.get("calendar_sha256"),
            "spec_hash": parent_envelope_payload.get("spec_hash"),
        }
        parent_envelope_for_engine = {**parent_envelope, **parent_identity,
                                    "status": parent_status}
        outcome = fork_parent_and_replay(
            store,
            scenario=spec,
            parent_result_envelope=parent_envelope_for_engine,
            catalog_provider=self.catalog_provider,
            engine_fn=self.engine_fn,
        )
        if outcome.refusal is not None:
            return _refusal_result(
                run_id, spec, outcome.refusal,
                spec_hash=scenario_spec_hash(spec),
                format_version=RUN_FORMAT_VERSION,
                engine_sha256=engine_identity_sha(),
                parent_run_id=parent_run_id,
                scenario_diff_sha256=scenario_diff_sha256(spec),
            )
        # Persist parent_ref (idempotent), then child pointer.
        if outcome.parent_ref is not None:
            store_parent_ref(store, outcome.parent_ref,
                             at=datetime.now())
        child = ChildRef(
            child_run_id=run_id,
            parent_run_id=parent_run_id,
            scenario_kind=spec.kind.value,
            scenario_diff_sha256=scenario_diff_sha256(spec),
        )
        from tree_options.research.scenarios.lineage import attach_child
        attach_child(store, child, at=datetime.now())
        wire = outcome.result.to_wire()
        # Append scenario-specific metadata to the wire envelope so
        # the SPA can render the lineage chip without an extra
        # roundtrip to GET /api/research/scenarios.
        wire_meta = dict(wire)
        wire_meta["parent_run_id"] = parent_run_id
        wire_meta["scenario_kind"] = spec.kind.value
        wire_meta["scenario_access_mode"] = spec.access_mode.value
        result_sha = hashlib.sha256(
            json.dumps(wire_meta, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        return {
            "run_id": run_id,
            "spec_hash": scenario_spec_hash(spec),
            "format_version": RUN_FORMAT_VERSION,
            "engine_sha256": engine_identity_sha(),
            "input_snapshot_sha256":
                parent_envelope_for_engine["input_snapshot_sha256"],
            "calendar_sha256": parent_envelope_for_engine["calendar_sha256"],
            "scenario_diff_sha256": scenario_diff_sha256(spec),
            "parent_run_id": parent_run_id,
            "scenario_kind": spec.kind.value,
            "result_sha256": result_sha,
            "wire": wire_meta,
        }

    # -- thread loop -------------------------------------------------------

    def start(self, *, poll_seconds: float = 1.0) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._requeue_interrupted()
        self._thread = threading.Thread(
            target=self._loop, kwargs={"poll_seconds": poll_seconds},
            name="research-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def _loop(self, *, poll_seconds: float) -> None:
        while not self._stop.is_set():
            try:
                worked = self.step()
            except Exception:
                worked = False
            if not worked:
                self._stop.wait(poll_seconds)

    def _requeue_interrupted(self) -> None:
        """A run left ``running`` by a dead process is explicitly
        re-queued at startup: recompute is deterministic and result puts
        are idempotent, so this is honest recovery, not silent reuse."""
        with open_runstate_store(self.workspace) as store:
            for payload, _at in store.all_at("run"):
                if (isinstance(payload, dict)
                        and payload.get("status") == "running"):
                    store.replace("run", {**payload, "status": "queued",
                                          "requeued_at": datetime.now().isoformat()},
                                  key=payload["run_id"])


__all__ = ["RUN_FORMAT_VERSION", "ResearchWorker", "engine_identity_sha",
           "input_snapshot_sha"]


# -- module-scope helpers --------------------------------------------------


def store_get_cached(workspace: Path, kind: str, key: str) -> dict[str, Any] | None:
    """Open the runstate store at ``workspace`` and return the payload
    of ``(kind, key)``. Used by ``_compute_scenario`` to read the
    parent's stored result envelope without holding the store open
    across the engine call.

    The store is opened briefly (in its own ``with`` block) so a
    second open for the lineage writes does not stack two openers on
    the same SQLite file (which on some kernels surfaces as
    ``database is locked``)."""
    with open_runstate_store(workspace) as store:
        return store.get(kind, key)


def _refusal_result(run_id: str, spec: Any, refusal: Any, **extra: Any) -> dict[str, Any]:
    """A scenario that refused (parent missing, type-B stress, missing
    capability, etc.) still gets a content-bound result record so the
    SPA can render the honest blocker — never a 404 from a missing
    row. The result_sha binds the canonical refusal text so
    downstream readers can replay why."""
    wire = {"refusal": refusal.code, "message": refusal.message,
            "scenario_kind": spec.kind.value}
    if "parent_run_id" in extra:
        wire["parent_run_id"] = extra["parent_run_id"]
    if "scenario_diff_sha256" in extra:
        wire["scenario_diff_sha256"] = extra["scenario_diff_sha256"]
    result_sha = hashlib.sha256(
        json.dumps(wire, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    payload = dict(extra)
    payload.update({
        "run_id": run_id,
        "result_sha256": result_sha,
        "wire": wire,
    })
    return payload
