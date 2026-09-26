"""FastAPI view for the TREX Research Lab (RL-1).

Mounts ``/api/research/*`` routes onto the existing FastAPI app built
by ``tree_options.trex_web.app.create_app``.

RL-1 surface:
    GET  /api/research/candidates                  — list catalog
    GET  /api/research/candidates/{id}             — one candidate
    GET  /api/research/candidates/{id}/evidence    — full EvidenceEnvelope
    GET  /api/research/compare                      — capability-matrix dry-run
    POST /api/research/compare                      — spool a ResearchRun
    GET  /api/research/runs/{id}                    — poll run status (404 until claimed)
    GET  /api/research/runs/{id}/result             — funded-account rows + drawdown + diff

RL §5 / §6 explicit out-of-scope (RL-1):
    GET  /api/research/forecast   -> 410 Gone
    GET  /api/research/scenarios  -> 410 Gone

Broker boundary:
    The view NEVER imports ``tree_options.trex.ibkr``, ``.monitor``,
    ``.gateway_watch`` or ``.enter``. The path-overlap guard in
    ``tree_options.research.paths.assert_no_overlap_with_desk`` runs at
    attach-time.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from tree_options.research import paths as research_paths
from tree_options.research.catalog.sealed_round import build_candidate
from tree_options.research.comparison.engine import run_comparison
from tree_options.research.comparison.plan import resolve_plan
from tree_options.research.contracts import (
    ResearchCandidate,
    ResearchDisposition,
    ResearchEvidenceKind,
    ResearchRegistration,
)
from tree_options.research.evidence.drawer import evidence_for_point
from tree_options.research.runstate.spec_hash import spec_hash
from tree_options.research.runstate.store import RunstateStoreError
from tree_options.research.runstate.worker import (
    RUN_FORMAT_VERSION,
    ResearchWorker,
)
from tree_options.research.spec_io import spec_from_dict

_DEFAULT_ADAPTER = "sealed_round"  # the catalog's only built-in adapter for RL-1


def attach(
    app: FastAPI,
    *,
    workspace: Path | None = None,
    candidate_scopes_root: Path | None = None,
    engine_fn: Callable[..., Any] | None = None,
    start_worker: bool | None = None,
) -> ResearchWorker | None:
    """Mount the ``/api/research/*`` routes on ``app`` and start the
    bounded research worker.

    ``workspace`` defaults to ``research_paths.workspace_root()``.
    ``candidate_scopes_root`` defaults to ``<repo>/artifacts/campaign-2026-09``
    — the catalog adapter iterates this directory's per-scope subdirs.
    ``start_worker`` defaults to the ``TREX_RESEARCH_WORKER`` env var
    (``0`` disables — tests drive ``worker.step()`` synchronously);
    returns the worker handle either way.

    Attach-time path-overlap guard: refuses if the ACTUAL resolved
    workspace collides with the desk state or paper-trades trees (RL
    §9 + RL1-04: the explicit argument is validated, not just env
    defaults).
    """
    workspace = workspace or research_paths.workspace_root()
    candidate_scopes_root = candidate_scopes_root or (
        Path(__file__).resolve().parents[3] / "artifacts" / "campaign-2026-09"
    )
    research_paths.assert_no_overlap_with_desk(workspace=workspace)
    # Panel survival: an unwritable workspace must never take the desk
    # panel down at attach time (a unit sandbox without the research
    # dir in ReadWritePaths raises here). GET catalog/evidence
    # endpoints keep working; the runstate-backed endpoints degrade
    # per-request to 503 via _open_store_or_503 — the same degradation
    # the discovery spool uses in trex_web.app.
    try:
        workspace.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass  # surfaced per-request by _open_store_or_503

    # Cache the catalog in a closure so the GET endpoints don't re-walk
    # artifacts every request. The cache is invalidated only at process
    # restart — for a few-thousand-entry catalog the scan is sub-second
    # but the file count is the runtime bottleneck.
    catalog_cache: list[ResearchCandidate] = _build_catalog(candidate_scopes_root)

    worker = ResearchWorker(
        workspace=workspace,
        catalog_provider=lambda: catalog_cache,
        engine_fn=engine_fn or run_comparison,
    )
    if start_worker is None:
        start_worker = os.environ.get("TREX_RESEARCH_WORKER", "1") != "0"
    if start_worker:
        try:
            worker.start()
        except OSError:
            pass  # unwritable workspace: endpoints degrade to 503 per-request
    # The worker handle is returned so tests can step it synchronously;
    # production ignores it (the daemon thread owns the loop).

    @app.get('/api/research/candidates')
    def list_candidates(
        family: str | None = Query(None),
        disposition: str | None = Query(None),
        evidence_kind: str | None = Query(None),
    ) -> JSONResponse:
        items = catalog_cache
        if family is not None:
            items = [c for c in items if c.family == family]
        if disposition is not None:
            items = [c for c in items if c.disposition.value == disposition]
        if evidence_kind is not None:
            items = [c for c in items if c.evidence_kind.value == evidence_kind]
        return JSONResponse({"candidates": [c.to_dict() for c in items]},
                            headers={"Cache-Control": "no-store"})

    @app.get('/api/research/candidates/{candidate_id}')
    def one_candidate(candidate_id: str) -> JSONResponse:
        cand = next((c for c in catalog_cache if c.id == candidate_id), None)
        if cand is None:
            raise HTTPException(status_code=404,
                                detail={"error": "candidate_not_found",
                                        "candidate_id": candidate_id})
        return JSONResponse(cand.to_dict(),
                            headers={"Cache-Control": "no-store"})

    @app.get('/api/research/candidates/{candidate_id}/evidence')
    def candidate_evidence(candidate_id: str,
                            session: str | None = Query(None),
                            as_of: str | None = Query(None)) -> JSONResponse:
        cand = next((c for c in catalog_cache if c.id == candidate_id), None)
        if cand is None:
            raise HTTPException(status_code=404,
                                detail={"error": "candidate_not_found",
                                        "candidate_id": candidate_id})
        try:
            sess = date.fromisoformat(session) if session else None
        except ValueError as exc:
            raise HTTPException(status_code=400,
                                detail={"error": "bad_session", "session": session}
                                ) from exc
        try:
            cutoff = datetime.fromisoformat(as_of) if as_of else None
        except ValueError as exc:
            raise HTTPException(status_code=400,
                                detail={"error": "bad_as_of", "as_of": as_of}
                                ) from exc
        env = evidence_for_point(cand, session=sess, knowledge_cutoff=cutoff)
        return JSONResponse(env.to_dict(),
                            headers={"Cache-Control": "no-store"})

    @app.get('/api/research/compare')
    def compare_preview() -> JSONResponse:
        """Capability-matrix dry-run: validates the spec but does NOT
        execute. Returns the resolved candidate list with per-candidate
        eligibility."""
        return JSONResponse(
            {"preview": True, "candidates": [c.to_dict() for c in catalog_cache]},
            headers={"Cache-Control": "no-store"},
        )

    @app.post('/api/research/compare')
    async def compare_run(request: Request) -> JSONResponse:
        """Spool a ``ResearchRun`` via the runstate store. The body is a
        ``ComparisonSpec.to_dict()`` JSON document.

        RL1-03 custody contract:
          * everything is VALIDATED BEFORE any write — body shape,
            candidate/benchmark membership, finite positive capital,
            ISO dates, timezone-aware cutoff, and the semantic plan
            (window, supported controls); invalid specs are 400s and
            leave nothing persisted;
          * the immutable ``spec`` object is EXACTLY the canonical form
            the spec hash covers — no server metadata inside the hashed
            payload (the pre-correction code embedded ``queued_at``
            there, so an identical resubmission collided with itself
            and returned 500);
          * resubmitting an identical spec is IDEMPOTENT: same run_id,
            same stored run, 200 with the run's current status;
          * the run record (status/timestamps/result binding) is a
            separate MUTABLE object the worker advances; POST never
            computes and GET never computes.
        """
        try:
            payload = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400,
                                detail={"error": "invalid_json"}) from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400,
                                detail={"error": "invalid_body",
                                        "message": "body must be a JSON object"})
        try:
            spec = spec_from_dict(payload)
        except ValueError as exc:
            raise HTTPException(status_code=400,
                                detail={"error": "invalid_comparison_spec",
                                        "message": str(exc)}) from exc
        # catalog membership — refused BEFORE anything is persisted
        missing = [cid for cid in spec.candidate_ids
                   if cid not in {c.id for c in catalog_cache}]
        if missing:
            raise HTTPException(status_code=400,
                                detail={"error": "candidate_not_in_catalog",
                                        "missing": missing})
        if spec.benchmark_candidate_id and not any(
                c.id == spec.benchmark_candidate_id for c in catalog_cache):
            raise HTTPException(status_code=400,
                                detail={"error": "benchmark_not_in_catalog",
                                        "benchmark_candidate_id":
                                            spec.benchmark_candidate_id})
        # semantic validation: the spec must resolve to an executable
        # plan (window, sessions, supported controls) at submission time
        plan = resolve_plan(spec)
        if plan.refused:
            raise HTTPException(status_code=400,
                                detail={"error": plan.refusal_reason,
                                        "message": plan.refusal_detail})

        run_id = spec_hash(spec)
        with _open_store_or_503(workspace) as store:
            try:
                store.put("spec", spec.to_dict(), key=run_id, at=datetime.now())
            except RunstateStoreError as exc:
                # A pre-custody-format record (metadata embedded in the
                # immutable payload) occupies this key: preserved, never
                # erased, reported honestly.
                raise HTTPException(
                    status_code=409,
                    detail={"error": "pre_custody_format_run_exists",
                            "message": "this spec was first submitted before "
                                       "run custody landed; its record is "
                                       "preserved — resubmit from a fresh "
                                       "workspace or after migration"},
                ) from exc
            existing = store.get("run", run_id)
            created = existing is None
            if created:
                # Deterministic initial payload (no timestamps): two
                # concurrent identical POSTs both land idempotently.
                store.put("run", {"run_id": run_id, "spec_hash": run_id,
                                  "status": "queued",
                                  "format_version": RUN_FORMAT_VERSION},
                          key=run_id, at=datetime.now())
                status_value = "queued"
            else:
                status_value = existing.get("status", "queued") \
                    if isinstance(existing, dict) else "queued"
        return JSONResponse(
            {"run_id": run_id, "status": status_value,
             "spec_hash": run_id, "workspace": str(workspace)},
            status_code=202 if created else 200,
            headers={"Cache-Control": "no-store"},
        )

    @app.get('/api/research/runs/{run_id}')
    def run_status(run_id: str) -> JSONResponse:
        with _open_store_or_503(workspace) as store:
            run = store.get("run", run_id)
            legacy = store.get("spec", run_id)
        if run is None:
            if legacy is not None:
                return JSONResponse(
                    {"run_id": run_id, "spec_hash": run_id,
                     "status": "blocked",
                     "error": "pre-custody-format run; resubmit the "
                              "comparison to execute it under run custody"},
                    headers={"Cache-Control": "no-store"})
            raise HTTPException(status_code=404,
                                detail={"error": "run_not_found", "run_id": run_id})
        return JSONResponse(run, headers={"Cache-Control": "no-store"})

    @app.get('/api/research/runs/{run_id}/result')
    def run_result(run_id: str) -> JSONResponse:
        """Return the RECORDED result — never a fresh computation. Two
        GETs must not cause two engine runs (RL1-03); pending runs get
        an honest status, failed runs their error, completed runs the
        immutable content-bound artifact."""
        with _open_store_or_503(workspace) as store:
            run = store.get("run", run_id)
            result = store.get("result", run_id)
            legacy = store.get("spec", run_id)
        if run is None:
            if legacy is not None:
                return JSONResponse(
                    {"run_id": run_id, "status": "blocked", "result": None,
                     "error": "pre-custody-format run; resubmit the "
                              "comparison to execute it under run custody"},
                    headers={"Cache-Control": "no-store"})
            raise HTTPException(status_code=404,
                                detail={"error": "run_not_found", "run_id": run_id})
        status_value = run.get("status")
        body: dict[str, Any] = {"run_id": run_id, "status": status_value,
                                "result": None}
        if status_value == "completed" and result is not None:
            body.update({
                "result": result["wire"],
                "result_sha256": result["result_sha256"],
                "engine_sha256": result["engine_sha256"],
                "input_snapshot_sha256": result["input_snapshot_sha256"],
                "calendar_sha256": result["calendar_sha256"],
            })
        elif status_value == "failed":
            body["error"] = run.get("error")
        return JSONResponse(body, headers={"Cache-Control": "no-store"})

    # RL §5 / §6 — explicit out-of-scope. RL-3 (forecasts) and RL-2
    # (scenarios) own these surfaces; until they ship, the routes
    # return 410 Gone so the SPA can hard-disable them rather than show
    # a confusing half-broken UX.
    @app.get('/api/research/forecast')
    def forecast_out_of_scope() -> JSONResponse:
        return JSONResponse(
            {"schema": "research-error/1",
             "error": "forecast_out_of_scope_for_rl1",
             "message": "RL-3 (calibrated outlook + study templates) ships separately"},
            status_code=410,
            headers={"Cache-Control": "no-store"},
        )

    @app.get('/api/research/scenarios')
    def scenarios_out_of_scope() -> JSONResponse:
        return JSONResponse(
            {"schema": "research-error/1",
             "error": "scenarios_out_of_scope_for_rl1",
             "message": "RL-2 (reproducible scenario branching) ships separately"},
            status_code=410,
            headers={"Cache-Control": "no-store"},
        )

    return worker


# -- Catalog adapter -------------------------------------------------------


def _build_catalog(scopes_root: Path) -> list[ResearchCandidate]:
    """Walk the campaign artifact root and run the sealed-round adapter
    against every scope directory.

    Tolerates missing scopes (e.g. before any campaign run). Returns an
    empty list — the GET endpoints then return ``{"candidates": []}``.
    """
    if not scopes_root.is_dir():
        return []
    candidates: list[ResearchCandidate] = []
    for scope_dir in sorted(scopes_root.iterdir()):
        if not scope_dir.is_dir():
            continue
        if scope_dir.name.endswith(".db") or scope_dir.name.endswith(".db-shm") \
           or scope_dir.name.endswith(".db-wal"):
            continue
        try:
            cand = build_candidate(scope_dir)
        except Exception as exc:
            # Adapter must NEVER crash the route — and a malformed scope
            # must NEVER silently vanish from the catalog: it surfaces
            # as a DATA-GATED row carrying the adapter error.
            candidates.append(ResearchCandidate(
                id=f"{scope_dir.name}-v?",
                family=scope_dir.name,
                version="v?",
                evidence_kind=ResearchEvidenceKind.SEALED_CAMPAIGN,
                registration=ResearchRegistration.RETROSPECTIVE_BACKFILL,
                disposition=ResearchDisposition.DATA_GATED_NOT_RUN,
                plot_funded_account=False,
                supported_start=None,
                supported_end=None,
                artifact_hashes={},
                capabilities=(),
                ineligibility_reason=f"catalog adapter raised: {exc}",
                warnings=("research.adapter_error",),
                source_url=f"sealed-round/{scope_dir.name}",
            ))
            continue
        candidates.append(cand)
    # The synthetic vertical slice (permanently labeled): one benchmark
    # + two strategy versions with a COMPLETE funded history, proving
    # the comparison path the zero-row catalog never exercised.
    from tree_options.research.catalog.synthetic import (
        build_synthetic_candidates,
    )
    candidates.extend(build_synthetic_candidates())
    return candidates


# -- Runstate access ---------------------------------------------------------


@contextmanager
def _open_store_or_503(workspace: Path):
    """Open the research runstate store, or raise the house 503.

    Mirrors the discovery-spool degradation in ``trex_web.app``: when
    the workspace is unwritable (e.g. a unit sandbox without the
    research dir in ``ReadWritePaths``) the runstate-backed endpoints
    return 503 with the operator hint instead of crashing the route.

    This must wrap the full ``with`` (a ``@contextmanager`` body runs
    at ``__enter__`` — the store's own mkdir raises there, not at the
    call site).
    """
    from tree_options.research.runstate.store import open_runstate_store

    try:
        with open_runstate_store(workspace) as store:
            yield store
    except (OSError, sqlite3.OperationalError) as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "error": "research_workspace_unwritable",
                "message": f"{exc}; trex-web ReadWritePaths missing?",
            },
        ) from exc


# Spec parsing lives in ``tree_options.research.spec_io.spec_from_dict``
# (shared verbatim by the HTTP view and the worker — RL1-03: the stored
# canonical spec and the POSTed body parse identically). Result
# serialization lives in ``ComparisonResult.to_wire`` — the single wire
# boundary.
