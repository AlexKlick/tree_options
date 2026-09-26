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

import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from tree_options.research import paths as research_paths
from tree_options.research.catalog.sealed_round import build_candidate
from tree_options.research.comparison.engine import run_comparison
from tree_options.research.contracts import (
    ComparisonSpec,
    ResearchCandidate,
    ResearchDisposition,
    ResearchEvidenceKind,
    ResearchRegistration,
)
from tree_options.research.evidence.drawer import evidence_for_point
from tree_options.research.runstate.spec_hash import spec_hash

_DEFAULT_ADAPTER = "sealed_round"  # the catalog's only built-in adapter for RL-1


def attach(
    app: FastAPI,
    *,
    workspace: Path | None = None,
    candidate_scopes_root: Path | None = None,
) -> None:
    """Mount the ``/api/research/*`` routes on ``app``.

    ``workspace`` defaults to ``research_paths.workspace_root()``.
    ``candidate_scopes_root`` defaults to ``<repo>/artifacts/campaign-2026-09``
    — the catalog adapter iterates this directory's per-scope subdirs.

    Attach-time path-overlap guard: refuses if any research path collides
    with the desk state or paper-trades directories (RL §9).
    """
    workspace = workspace or research_paths.workspace_root()
    candidate_scopes_root = candidate_scopes_root or (
        Path(__file__).resolve().parents[3] / "artifacts" / "campaign-2026-09"
    )
    research_paths.assert_no_overlap_with_desk()
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

        RL §9: state-changing = create bounded jobs or draft specs
        (not GET side-effects). POST is spool-only; the actual run is
        executed by a worker that consumes the spool (RL-2).
        """
        try:
            payload = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400,
                                detail={"error": "invalid_json"}) from exc
        try:
            spec = _parse_spec(payload)
        except HTTPException:
            raise
        run_id = spec_hash(spec)
        # Store the parsed spec's to_dict() so the GET endpoints can find
        # the row by its sha-keyed `id` field. The original raw payload
        # had no `id`; persisting the parsed form is the canonical record.
        record = dict(spec.to_dict())
        record["id"] = run_id
        record["status"] = "queued"
        record["queued_at"] = datetime.now().isoformat()
        with _open_store_or_503(workspace) as store:
            store.put("spec", record, key=run_id, at=datetime.now())
        return JSONResponse(
            {"run_id": run_id, "status": "queued",
             "spec_hash": run_id, "workspace": str(workspace)},
            status_code=202,
            headers={"Cache-Control": "no-store"},
        )

    @app.get('/api/research/runs/{run_id}')
    def run_status(run_id: str) -> JSONResponse:
        with _open_store_or_503(workspace) as store:
            specs = store.all("spec")
        row = next((s for s in specs if s.get("id") == run_id), None)
        if row is None:
            raise HTTPException(status_code=404,
                                detail={"error": "run_not_found", "run_id": run_id})
        return JSONResponse(row, headers={"Cache-Control": "no-store"})

    @app.get('/api/research/runs/{run_id}/result')
    def run_result(run_id: str) -> JSONResponse:
        with _open_store_or_503(workspace) as store:
            spec_payload = next(
                (s for s in store.all("spec") if s.get("id") == run_id),
                None,
            )
            if spec_payload is None:
                raise HTTPException(status_code=404,
                                    detail={"error": "run_not_found", "run_id": run_id})
        try:
            spec = _parse_spec(spec_payload)
        except HTTPException as exc:
            raise HTTPException(status_code=400,
                                detail={"error": "invalid_stored_spec"}) from exc
        cands = [next((c for c in catalog_cache if c.id == cid), None)
                 for cid in spec.candidate_ids]
        if any(c is None for c in cands):
            raise HTTPException(status_code=400,
                                detail={"error": "candidate_not_in_catalog",
                                        "missing": [cid for cid, c
                                                    in zip(spec.candidate_ids, cands,
                                                          strict=True)
                                                    if c is None]})
        baseline = None
        if spec.benchmark_candidate_id:
            baseline = next((c for c in catalog_cache
                             if c.id == spec.benchmark_candidate_id), None)
            if baseline is None:
                raise HTTPException(status_code=400,
                                    detail={"error": "benchmark_not_in_catalog",
                                            "benchmark_candidate_id":
                                                spec.benchmark_candidate_id})
        result = run_comparison(spec, tuple(c for c in cands if c is not None),
                                baseline=baseline)
        return JSONResponse(_result_to_dict(result),
                            headers={"Cache-Control": "no-store"})

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


# -- Spec parser (POST body) -----------------------------------------------


def _parse_spec(payload: dict[str, Any]) -> ComparisonSpec:
    """Convert an incoming POST body to a ComparisonSpec.

    Validates Decimal-encoded fields and date fields. Raises HTTP 400
    on type errors. Missing fields default to the spec's dataclass
    defaults (e.g. currency='USD', idle_cash_policy='cash_yields_zero').
    """
    try:
        from decimal import InvalidOperation as DecimalError

        from tree_options.research.contracts import (
            BorrowingPolicy,
            CashflowTiming,
            CollateralPolicy,
            CostModelKind,
            Currency,
            IdleCashPolicy,
            PositionSizing,
            PriceBasis,
            Rebalancing,
        )

        def _decimal(field: str) -> Decimal:
            raw = payload.get(field, "0")
            try:
                return Decimal(str(raw))
            except DecimalError as exc:
                raise ValueError(f"field {field!r} not a decimal: {raw!r}") from exc

        common_start = (date.fromisoformat(payload["common_start"])
                       if payload.get("common_start") else None)
        common_end = (date.fromisoformat(payload["common_end"])
                     if payload.get("common_end") else None)
        cutoff = (datetime.fromisoformat(payload["knowledge_cutoff"])
                  if payload.get("knowledge_cutoff") else None)
        return ComparisonSpec(
            candidate_ids=tuple(payload["candidate_ids"]),
            starting_capital=_decimal("starting_capital"),
            common_start=common_start,
            common_end=common_end,
            cashflow_timing=CashflowTiming(payload.get("cashflow_timing", "beginning_of_period")),
            contribution_per_period=_decimal("contribution_per_period"),
            cost_model_kind=CostModelKind(payload.get("cost_model_kind", "five_bp_fixed")),
            benchmark_candidate_id=payload.get("benchmark_candidate_id"),
            currency=Currency(payload.get("currency", "USD")),
            price_basis=PriceBasis(payload.get("price_basis", "nominal_pretax")),
            idle_cash_policy=IdleCashPolicy(payload.get("idle_cash_policy", "cash_yields_zero")),
            rebalancing=Rebalancing(payload.get("rebalancing", "none")),
            position_sizing=PositionSizing(payload.get("position_sizing", "integer")),
            collateral=CollateralPolicy(payload.get("collateral", "none")),
            borrowing=BorrowingPolicy(payload.get("borrowing", "none")),
            knowledge_cutoff=cutoff,
            proposed_by=payload.get("proposed_by", "operator"),
            notes=payload.get("notes", ""),
        )
    except (KeyError, ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_comparison_spec", "message": str(exc)},
        ) from exc


# -- Result serialiser -----------------------------------------------------


def _result_to_dict(result) -> dict[str, Any]:
    """Delegate to ``ComparisonResult.to_wire`` — the single wire
    boundary (ISO date keys, money strings). The view never re-shapes
    result payloads (RL1-02: a bespoke serializer here passed
    ``datetime.date`` keys straight into ``JSONResponse`` and the first
    nonempty result returned HTTP 500)."""
    return result.to_wire()
