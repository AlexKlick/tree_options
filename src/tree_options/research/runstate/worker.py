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

RL-3 adds FORECAST runs (``kind == "forecast"``) under the same
lifecycle, with execution-bound identity: the run record carries the
``series_sha256`` and ``engine_sha256`` captured at SUBMISSION, and
the worker RECOMPUTES them before evaluating — a data revision, an
engine change, or a moved calendar (comparison or session authority)
between enqueue and compute publishes a typed refusal
(``research.forecast.source_drift`` / ``engine_changed`` /
``calendar_changed``, both values in the wire), never revised bytes or
new code under the submission's identity. Unknown run kinds now FAIL
loudly instead of silently computing a comparison.

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

from tree_options.desk.contracts import canonical
from tree_options.research.comparison.calendar import calendar_sha256
from tree_options.research.comparison.engine import run_comparison
from tree_options.research.comparison.plan import resolve_plan
from tree_options.research.contracts import ResearchCandidate
from tree_options.research.forecast.contracts import (
    ForecastSourceId,
    forecast_run_id,
)
from tree_options.research.forecast.engine import evaluate_forecast
from tree_options.research.forecast.refusal_codes import (
    FORECAST_CALENDAR_CHANGED,
    FORECAST_ENGINE_CHANGED,
    FORECAST_IDENTITY_MISMATCH,
    FORECAST_SOURCE_DRIFT,
    ForecastRefusal,
)
from tree_options.research.forecast.sources import (
    ForecastSeries,
    load_index,
    load_synthetic,
    session_authority_sha256,
)
from tree_options.research.forecast.spec_io import forecast_from_dict
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
    load_parent_ref,
    store_parent_ref,
)
from tree_options.research.scenarios.refusal_codes import (
    SCENARIO_PARENT_CHANGED,
    SCENARIO_PARENT_MISSING,
    ScenarioRefusal,
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
#: RL-3 adds the forecast package AND ``tree_options.desk.stats``
#: (forecast receipts depend on its DM/OLS); the checkpoint-B pass adds
#: ``evaluation.diagnostics`` (its bootstrap DETERMINES published
#: intervals), ``desk.indices`` (read_store shapes the series), and the
#: worker itself (its forecast orchestration — drift checks, snapshot
#: and envelope shape — determines what a receipt IS; hashing the
#: module that computes the sha is intentional, not circular: the
#: bytes are read from disk, not derived from the digest). Changing
#: the shared sha for every lane is expected and documented: a rerun
#: is a NEW run.
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
    "tree_options.research.forecast.contracts",
    "tree_options.research.forecast.refusal_codes",
    "tree_options.research.forecast.spec_io",
    "tree_options.research.forecast.metrics",
    "tree_options.research.forecast.sources",
    "tree_options.research.forecast.harness",
    "tree_options.research.forecast.engine",
    "tree_options.desk.stats",
    "tree_options.evaluation.diagnostics",
    "tree_options.desk.indices",
    "tree_options.research.runstate.worker",
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
            elif kind == "forecast":
                result_payload = self._compute_forecast(
                    run_id, spec_payload, run)
            elif kind == "comparison":
                result_payload = self._compute(run_id, spec_payload)
            else:
                # An unknown kind must FAIL loudly — pre-RL-3 it fell
                # through to the comparison engine, computing a
                # different job than the record describes.
                raise RunstateStoreError(
                    f"unknown run kind {kind!r} for run {run_id}")
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

    def _compute_forecast(self, run_id: str, spec_payload: dict[str, Any],
                          run: dict[str, Any]) -> dict[str, Any]:
        """Evaluate a forecast run with execution-bound identity (RL-3).

        Refusal order (checkpoint B-prime): engine identity, then BOTH
        calendar identities (checked BEFORE the series is loaded — the
        loader parses the authority file, and a malformed replacement
        must meet the typed ``calendar_changed`` wall, not crash into a
        generic failed run), then the series load, then series drift —
        and finally the run id itself is RECOMPUTED from the stored
        spec plus the current bindings and must reproduce the id under
        which the record was spooled (the submission-side
        single-read/same-id guarantee; a record whose bindings do not
        hash back to its run id can never publish).
        """
        spec = forecast_from_dict(spec_payload)

        engine_now = engine_identity_sha()
        expected_engine = run.get("engine_sha256_at_submission")
        if isinstance(expected_engine, str) and expected_engine != engine_now:
            return _forecast_refusal_result(
                run_id, spec, ForecastRefusal(
                    code=FORECAST_ENGINE_CHANGED,
                    message=("engine identity moved between submission "
                             f"and compute: submitted {expected_engine}, "
                             f"now {engine_now}; refusing to execute new "
                             "code under the submission's run id"),
                    payload={"engine_sha256_at_submission": expected_engine,
                             "engine_sha256_at_compute": engine_now}),
                engine_sha256=expected_engine)

        calendar_now = calendar_sha256()
        authority_now = session_authority_sha256()
        calendar_moved = (
            (isinstance(run.get("calendar_sha256_at_submission"), str)
             and run["calendar_sha256_at_submission"] != calendar_now)
            or (isinstance(
                run.get("session_authority_sha256_at_submission"), str)
                and run["session_authority_sha256_at_submission"]
                != authority_now))
        if calendar_moved:
            return _forecast_refusal_result(
                run_id, spec, ForecastRefusal(
                    code=FORECAST_CALENDAR_CHANGED,
                    message=("a calendar bound into this run's identity "
                             "moved between submission and compute "
                             "(comparison calendar and/or closure-"
                             "corrected session authority); refusing to "
                             "re-grade the grid under the submission's "
                             "run id"),
                    payload={
                        "calendar_sha256_at_submission":
                            run.get("calendar_sha256_at_submission"),
                        "calendar_sha256_at_compute": calendar_now,
                        "session_authority_sha256_at_submission":
                            run.get("session_authority_sha256_at_submission"),
                        "session_authority_sha256_at_compute": authority_now,
                    }),
                engine_sha256=engine_now)

        series = _load_forecast_series(spec)
        if isinstance(series, ForecastRefusal):
            return _forecast_refusal_result(run_id, spec, series)

        # The authority bytes that SHAPED the loaded grid (the loader
        # records the sha of exactly the bytes it intersected with). If
        # the authority was replaced between the identity check above
        # and this load, the grid is B's while the checked binding is
        # A's — refuse rather than publish B's grid under A's run id
        # (checkpoint B-double-prime, NEW P1). Checked BEFORE series
        # drift so a combined CSV+authority change still reports the
        # calendar divergence with its three values (sol final P2).
        # Absent for sources whose grid does not intersect the
        # authority (synthetic lane).
        grid_authority = series.provenance.get(
            "session_authority_sha256")
        if isinstance(grid_authority, str) \
                and grid_authority != authority_now:
            return _forecast_refusal_result(
                run_id, spec, ForecastRefusal(
                    code=FORECAST_CALENDAR_CHANGED,
                    message=("the closure-corrected session authority "
                             "moved between the identity check and the "
                             "series load: the loaded grid was shaped by "
                             "different authority bytes than the checked "
                             "binding — refusing to publish that grid "
                             "under this run id"),
                    payload={
                        "session_authority_sha256_at_submission":
                            run.get("session_authority_sha256_at_submission"),
                        "session_authority_sha256_at_check": authority_now,
                        "session_authority_sha256_that_shaped_the_grid":
                            grid_authority,
                    }),
                engine_sha256=engine_now)

        expected_series = run.get("series_sha256_at_submission")
        if isinstance(expected_series, str) \
                and expected_series != series.series_sha256:
            return _forecast_refusal_result(
                run_id, spec, ForecastRefusal(
                    code=FORECAST_SOURCE_DRIFT,
                    message=("source data drifted between submission and "
                             f"compute: submitted {expected_series}, live "
                             f"resolves to {series.series_sha256}; "
                             "refusing to publish revised bytes under the "
                             "submission's run id"),
                    payload={"series_sha256_at_submission": expected_series,
                             "series_sha256_at_compute":
                                 series.series_sha256}),
                engine_sha256=engine_now)

        recomputed = forecast_run_id(
            spec, series_sha256=series.series_sha256,
            calendar_sha256=calendar_now,
            session_authority_sha256=authority_now,
            engine_sha256=engine_now)
        if recomputed != run_id:
            # The bindings that passed every specific drift check still
            # do not hash back to this run id: the record was spooled
            # with inconsistent bindings (checkpoint B-prime, N1). The
            # execution is refused — never published under an id it
            # does not bind.
            return _forecast_refusal_result(
                run_id, spec, ForecastRefusal(
                    code=FORECAST_IDENTITY_MISMATCH,
                    message=("the run record's bindings do not reproduce "
                             "its run id: recomputing the id from the "
                             "stored spec and the current series / "
                             "calendar / engine bindings yields a "
                             "different id — refusing to publish under "
                             "an identity the execution does not bind"),
                    payload={"run_id": run_id,
                             "recomputed_run_id": recomputed}),
                engine_sha256=engine_now)

        outcome = evaluate_forecast(
            spec, series=series,
            calendar_sha256=calendar_now,
            engine_sha256=engine_now)
        if isinstance(outcome, ForecastRefusal):
            return _forecast_refusal_result(
                run_id, spec, outcome, engine_sha256=engine_now)

        wire = outcome.to_wire()
        result_sha = hashlib.sha256(canonical(wire)).hexdigest()
        snapshot: dict[str, Any] = {
            "schema": "forecast-input/1",
            "source": spec.source.value,
            "series_sha256": series.series_sha256,
            "basis": series.basis,
            "grid_basis": series.grid_basis,
            "provenance": dict(series.provenance),
            "horizon": spec.horizon,
            "evaluation_window": {
                "start": spec.evaluation_start.isoformat(),
                "end": (spec.evaluation_end.isoformat()
                        if spec.evaluation_end else None),
            },
            "calendar_sha256": calendar_now,
            "session_authority_sha256": authority_now,
        }
        snapshot_sha = hashlib.sha256(canonical(snapshot)).hexdigest()
        return {
            "run_id": run_id,
            "spec_hash": run_id,
            "format_version": RUN_FORMAT_VERSION,
            "engine_sha256": engine_now,
            "input_snapshot": snapshot,
            "input_snapshot_sha256": snapshot_sha,
            "calendar_sha256": calendar_now,
            "session_authority_sha256": authority_now,
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
        parent_envelope_payload = store.get("result", parent_run_id)
        if parent_envelope_payload is None:
            # Mirrors the comparison engine's "missing parent" shape;
            # the spec holder refusal is rendered into the result
            # wire so the SPA renders the honest blocker.
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
        # P1-1 enforcement: the attach-time ParentRef (written on the
        # first fork of this parent) is READ BACK and handed to the
        # engine as the lineage baseline. First fork (no stored ref)
        # attaches; every later fork is compared against the original.
        attach_ref = load_parent_ref(store, parent_run_id)
        outcome = fork_parent_and_replay(
            store,
            scenario=spec,
            parent_result_envelope=parent_envelope_for_engine,
            catalog_provider=self.catalog_provider,
            engine_fn=self.engine_fn,
            attach_ref=attach_ref,
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
        # P1-2 enforcement: the child's input identity is RECOMPUTED
        # from the live catalog for the rewritten spec — never
        # inherited from the parent's record. A candidate whose
        # artifact hashes changed under the same id between the
        # parent run and this fork produces a different snapshot, and
        # the fork refuses rather than publishing a receipt that
        # claims the parent's inputs.
        assert outcome.rewritten_spec is not None
        live_plan = resolve_plan(outcome.rewritten_spec)
        if live_plan.refused:
            return _refusal_result(
                run_id, spec,
                ScenarioRefusal(
                    code=SCENARIO_PARENT_CHANGED,
                    message=("the scenario diff made the plan unresolvable "
                             f"at compute time: {live_plan.refusal_reason}"),
                ),
                spec_hash=scenario_spec_hash(spec),
                format_version=RUN_FORMAT_VERSION,
                engine_sha256=engine_identity_sha(),
                parent_run_id=parent_run_id,
                scenario_diff_sha256=scenario_diff_sha256(spec),
            )
        catalog_now = {c.id: c for c in self.catalog_provider()}
        live_cands = tuple(catalog_now[cid]
                           for cid in outcome.rewritten_spec.candidate_ids)
        live_baseline = (
            catalog_now.get(outcome.rewritten_spec.benchmark_candidate_id)
            if outcome.rewritten_spec.benchmark_candidate_id else None)
        live_snapshot, live_snapshot_sha = input_snapshot_sha(
            live_cands, live_baseline, live_plan.calendar_sha256)
        parent_input_sha = parent_envelope_payload.get(
            "input_snapshot_sha256")
        if live_snapshot_sha != parent_input_sha:
            refusal = ScenarioRefusal(
                code=SCENARIO_PARENT_CHANGED,
                message=(f"parent input_snapshot_sha256 drifted: parent "
                         f"record claims {parent_input_sha}, live catalog "
                         f"resolves to {live_snapshot_sha}; refuse to "
                         "publish a child whose receipt would misrepresent "
                         "its inputs"),
            )
            refusal_payload = _refusal_result(
                run_id, spec, refusal,
                spec_hash=scenario_spec_hash(spec),
                format_version=RUN_FORMAT_VERSION,
                engine_sha256=engine_identity_sha(),
                parent_run_id=parent_run_id,
                scenario_diff_sha256=scenario_diff_sha256(spec),
            )
            refusal_payload["wire"][
                "live_input_snapshot_sha256"] = live_snapshot_sha
            return refusal_payload
        wire = outcome.result.to_wire()
        # Append scenario-specific metadata to the wire envelope so
        # the SPA can render the lineage chip without an extra
        # roundtrip to GET /api/research/scenarios.
        wire_meta = dict(wire)
        wire_meta["parent_run_id"] = parent_run_id
        wire_meta["scenario_kind"] = spec.kind.value
        wire_meta["scenario_access_mode"] = spec.access_mode.value
        wire_meta["refusal"] = None
        result_sha = hashlib.sha256(
            json.dumps(wire_meta, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        return {
            "run_id": run_id,
            "spec_hash": scenario_spec_hash(spec),
            "format_version": RUN_FORMAT_VERSION,
            "engine_sha256": engine_identity_sha(),
            "input_snapshot": live_snapshot,
            "input_snapshot_sha256": live_snapshot_sha,
            "input_snapshot_verified_against_parent": True,
            "calendar_sha256": live_plan.calendar_sha256,
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


def _load_forecast_series(spec: Any) -> ForecastSeries | ForecastRefusal:
    """Resolve the spec's source through the registry loaders (a
    refusal is a value — the caller publishes it content-bound)."""
    if spec.source is ForecastSourceId.SYNTHETIC:
        return load_synthetic()
    if spec.source is ForecastSourceId.INDEX_VIX:
        return load_index("VIX")
    raise RunstateStoreError(
        f"no source loader for {spec.source.value!r}")


def _forecast_refusal_result(run_id: str, spec: Any,
                             refusal: ForecastRefusal,
                             **extra: Any) -> dict[str, Any]:
    """A forecast that refused still gets a content-bound result record
    (the scenario discipline): the wire carries the machine-readable
    code, the message, AND the refusal's structured payload — the
    tally/ledger/both-shas evidence — so the SPA renders the honest
    blocker and downstream readers can replay why."""
    wire: dict[str, Any] = {
        "schema": "research-forecast-refusal/1",
        "refusal": refusal.code,
        "message": refusal.message,
        "source": spec.source.value,
        "horizon": spec.horizon,
        **dict(refusal.payload),
    }
    result_sha = hashlib.sha256(canonical(wire)).hexdigest()
    payload = dict(extra)
    payload.update({
        "run_id": run_id,
        "engine_sha256": extra.get("engine_sha256", engine_identity_sha()),
        "result_sha256": result_sha,
        "wire": wire,
    })
    return payload
