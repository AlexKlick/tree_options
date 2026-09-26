"""Scenario HTTP routes — RL-2 surface wiring.

The scenarios routes (``GET/POST /api/research/scenarios``) replace
the RL-1 410 Gone stub. These oracles pin the same idempotency /
pre-write validation / content-bound result pattern that RL1-03
established for ``POST /compare``:

    1. POST /scenarios/{parent} validates the diff is bounded by
       SCENARIO_DIFF_FIELDS — anything else is 400 pre-write.
    2. POST is idempotent on identical bodies (same child_run_id,
       200 second time).
    3. POST against a non-existent parent is 404.
    4. GET /scenarios?parent_run_id=<id> returns the lineage.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tree_options.research.comparison.engine import run_comparison
from tree_options.research.contracts import (
    BorrowingPolicy,
    CashflowTiming,
    CollateralPolicy,
    ComparisonSpec,
    CostModelKind,
    Currency,
    FundedHistorySupport,
    IdleCashPolicy,
    PositionSizing,
    PriceBasis,
    Rebalancing,
    ResearchCandidate,
    ResearchDisposition,
    ResearchEvidenceKind,
    ResearchRegistration,
)
from tree_options.research.runstate.spec_hash import spec_hash
from tree_options.research.runstate.store import open_runstate_store
from tree_options.research.runstate.worker import (
    RUN_FORMAT_VERSION,
)
from tree_options.trex_web.research_view import attach


def _candidate() -> ResearchCandidate:
    return ResearchCandidate(
        id="c1", family="f", version="v1",
        evidence_kind=ResearchEvidenceKind.SYNTHETIC_BACKTEST,
        registration=ResearchRegistration.BEFORE_ENTRY_WINDOW_END,
        disposition=ResearchDisposition.PASS,
        plot_funded_account=True,
        supported_start=datetime(2024, 1, 2).date(),
        supported_end=datetime(2024, 1, 31).date(),
        funded_history=FundedHistorySupport.RECONSTRUCTED,
    )


def _spec() -> ComparisonSpec:
    return ComparisonSpec(
        candidate_ids=("c1",),
        starting_capital=Decimal("10000"),
        common_start=datetime(2024, 1, 2).date(),
        common_end=datetime(2024, 1, 8).date(),
        cashflow_timing=CashflowTiming.BEGINNING_OF_PERIOD,
        contribution_per_period=Decimal("0"),
        cost_model_kind=CostModelKind.FIVE_BP_FIXED,
        benchmark_candidate_id=None,
        currency=Currency.USD,
        price_basis=PriceBasis.NOMINAL_PRETAX,
        idle_cash_policy=IdleCashPolicy.CASH_YIELDS_ZERO,
        rebalancing=Rebalancing.NONE,
        position_sizing=PositionSizing.INTEGER,
        collateral=CollateralPolicy.NONE,
        borrowing=BorrowingPolicy.NONE,
        knowledge_cutoff=datetime(2024, 1, 31, tzinfo=UTC),
        proposed_by="operator",
        notes="",
    )


@pytest.fixture
def client(tmp_path: Path):
    ws = tmp_path / "rs"
    ws.mkdir()
    artifacts = tmp_path / "artifacts-empty"
    artifacts.mkdir()
    app = FastAPI()
    worker = attach(
        app, workspace=ws,
        candidate_scopes_root=artifacts,
        start_worker=False,
        engine_fn=run_comparison,
    )
    worker.catalog_provider = lambda: [_candidate()]
    tc = TestClient(app)
    return tc, ws, artifacts


def _spawn_parent(ws: Path) -> str:
    spec = _spec()
    parent_id = spec_hash(spec)
    with open_runstate_store(ws) as store:
        store.put("spec", spec.to_dict(), key=parent_id, at=datetime.now())
        store.put("run",
                  {"run_id": parent_id, "spec_hash": parent_id,
                   "kind": "comparison", "status": "completed",
                   "format_version": RUN_FORMAT_VERSION,
                   "completed_at": datetime.now().isoformat()},
                  key=parent_id, at=datetime.now())
        # The worker writes a result record when it computes; tests
        # that simulate an "existing parent" need that record too.
        store.put("result",
                  {"run_id": parent_id, "spec_hash": parent_id,
                   "format_version": RUN_FORMAT_VERSION,
                   "engine_sha256": "e" * 64,
                   "input_snapshot_sha256": "i" * 64,
                   "calendar_sha256": "c" * 64,
                   "result_sha256": "r" * 64,
                   "wire": {"spec": spec.to_dict(), "rejection": None,
                            "candidates": [], "paired_diff": {}}},
                  key=parent_id, at=datetime.now())
    return parent_id


def test_post_scenario_rejects_unknown_diff_fields_pre_write(client):
    """RL1-03 boundary: body validated before any write; any field
    outside SCENARIO_DIFF_FIELDS is rejected with 400."""
    tc, ws, _ = client
    parent_id = _spawn_parent(ws)
    r = tc.post(f"/api/research/scenarios/{parent_id}",
                json={"kind": "contribution_planning",
                      "diff": {"starting_capital": "5000"}})
    assert r.status_code == 400
    body = r.json()
    assert "outside the scenario surface" in body["detail"]["message"]
    # Nothing was written.
    with open_runstate_store(ws) as store:
        runs = list(store.all_at("run"))
    # only the parent pre-seeded record is on disk
    assert all(r["kind"] == "comparison" for r, _ in runs)


def test_post_scenario_is_idempotent_on_identical_body(client):
    """Same body submitted twice collapses to the same child_run_id
    (200 second time, never a 409)."""
    tc, ws, _ = client
    parent_id = _spawn_parent(ws)
    body = {"kind": "contribution_planning", "access_mode": "exploratory",
            "diff": {"contribution_per_period": "500"}}
    r1 = tc.post(f"/api/research/scenarios/{parent_id}", json=body)
    assert r1.status_code == 202
    child_id = r1.json()["run_id"]
    r2 = tc.post(f"/api/research/scenarios/{parent_id}", json=body)
    assert r2.status_code == 200
    assert r2.json()["run_id"] == child_id


def test_post_scenario_404s_on_unknown_parent(client):
    tc, _ws, _ = client
    body = {"kind": "contribution_planning",
            "diff": {"contribution_per_period": "500"}}
    r = tc.post("/api/research/scenarios/missing_parent_id",
                json=body)
    assert r.status_code == 404
    assert r.json()["detail"]["error"] == "parent_not_found"


def test_post_scenario_409s_on_parent_with_no_result_envelope(client):
    """A parent exists but its ``result`` record is missing (a
    pre-custody-format legacy record, say). Lineage honesty refuses
    with 409 rather than silently re-computing the parent."""
    tc, ws, _ = client
    spec = _spec()
    parent_id = spec_hash(spec)
    with open_runstate_store(ws) as store:
        store.put("spec", spec.to_dict(), key=parent_id, at=datetime.now())
        store.put("run",
                  {"run_id": parent_id, "spec_hash": parent_id,
                   "kind": "comparison", "status": "completed",
                   "format_version": RUN_FORMAT_VERSION},
                  key=parent_id, at=datetime.now())
        # Note: no result record.
    body = {"kind": "contribution_planning",
            "diff": {"contribution_per_period": "500"}}
    r = tc.post(f"/api/research/scenarios/{parent_id}", json=body)
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "parent_missing_result"


def test_list_scenarios_returns_children_for_parent(client):
    """GET /scenarios?parent_run_id=<id> walks the lineage table —
    one parent with two scenario children of distinct diffs (each
    diff is a unique child_run_id by spec_hash)."""
    tc, ws, _ = client
    parent_id = _spawn_parent(ws)
    body1 = {"kind": "contribution_planning",
             "diff": {"contribution_per_period": "500"}}
    body2 = {"kind": "contribution_planning",
             "diff": {"contribution_per_period": "1000"}}
    r1 = tc.post(f"/api/research/scenarios/{parent_id}", json=body1)
    r2 = tc.post(f"/api/research/scenarios/{parent_id}", json=body2)
    assert r1.json()["run_id"] != r2.json()["run_id"]
    r = tc.get("/api/research/scenarios",
               params={"parent_run_id": parent_id})
    assert r.status_code == 200
    body = r.json()
    assert len(body["scenarios"]) == 2
    assert all(s["parent_run_id"] == parent_id for s in body["scenarios"])


def test_post_scenario_rejects_nonfinite_contribution_at_parse(client):
    """RL1-03 boundary: contribution_per_period must be finite."""
    tc, ws, _ = client
    parent_id = _spawn_parent(ws)
    r = tc.post(f"/api/research/scenarios/{parent_id}",
                json={"kind": "contribution_planning",
                      "diff": {"contribution_per_period": "Infinity"}})
    assert r.status_code == 400
    assert "must be finite" in r.json()["detail"]["message"]


def test_post_scenario_409s_when_stored_parent_ref_drifted(client):
    """RL-2 hardening (P1-1 route half): a ParentRef stored at an
    earlier attach no longer matches the parent's current identity
    (the parent was re-run under changed source/inputs). The lineage
    is immutable once attached, so submitting a NEW child spec on
    that parent must 409 with the parent_changed reason — never the
    unhandled RunstateStoreError 500 the pre-hardening route raised."""
    from tree_options.research.scenarios.lineage import (
        ParentRef,
        store_parent_ref,
    )

    tc, ws, _ = client
    parent_id = _spawn_parent(ws)
    # Simulate an attach that happened when the parent's engine sha
    # was different from what its current result record claims.
    with open_runstate_store(ws) as store:
        store_parent_ref(store, ParentRef(
            parent_run_id=parent_id,
            parent_spec_hash="x" * 64,
            parent_engine_sha256="Z" * 64,   # drifted vs "e"*64
            parent_input_snapshot_sha256="b" * 64,
            parent_calendar_sha256="c" * 64,
        ), at=datetime.now())
    body = {"kind": "contribution_planning",
            "diff": {"contribution_per_period": "750"}}  # fresh child
    r = tc.post(f"/api/research/scenarios/{parent_id}", json=body)
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "research.scenario.parent_changed"
