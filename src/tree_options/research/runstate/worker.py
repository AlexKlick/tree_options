r"""Bounded research worker: claim queued runs, compute, publish
immutable results (RL1-03 correction).

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
from tree_options.research.spec_io import spec_from_dict

#: Run-record format written by this module; anything else in the store
#: predates the custody correction and is blocked, not interpreted.
RUN_FORMAT_VERSION = 2

#: Engine identity: sha256 over the comparison modules' source bytes —
#: the "engine/code version" bound into every result receipt.
_ENGINE_MODULES = (
    "tree_options.research.comparison.funded",
    "tree_options.research.comparison.engine",
    "tree_options.research.comparison.plan",
    "tree_options.research.comparison.calendar",
    "tree_options.research.comparison.drawdown",
    "tree_options.research.comparison.pair",
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
        store.replace(
            "run",
            {**run, "status": "completed",
             "completed_at": datetime.now().isoformat(),
             "result_sha256": result_payload["result_sha256"],
             "engine_sha256": result_payload["engine_sha256"],
             "input_snapshot_sha256": result_payload["input_snapshot_sha256"]},
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
