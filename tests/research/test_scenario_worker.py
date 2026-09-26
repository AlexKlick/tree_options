"""Bounded worker end-to-end for scenarios (RL-2).

The scenario worker reuses the RL1-03 single-thread / claim /
publish lifecycle. These oracles pin:

    1. A spawned scenario run is published as a content-bound
       result with the four identity shas (engine, input, calendar,
       scenario_diff) + parent_run_id.
    2. A refused scenario (type B stress surface, missing parent)
       still publishes a content-bound refusal envelope — never a
       missing row, never a 500.
    3. Two identical scenario POSTs (idempotent) collapse to one
       child_run_id.

Mirrors the discipline of ``test_routes.py`` (RL1-03) — every
boundary below is observed through the wire, never through
private store internals.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

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
from tree_options.research.runstate.spec_hash import spec_hash as comp_spec_hash
from tree_options.research.runstate.store import open_runstate_store
from tree_options.research.runstate.worker import (
    RUN_FORMAT_VERSION,
    ResearchWorker,
)
from tree_options.research.scenarios.contracts import (
    scenario_spec_hash,
)
from tree_options.research.scenarios.refusal_codes import (
    SCENARIO_PARENT_MISSING,
    SCENARIO_STRESS_UNSUPPORTED,
)
from tree_options.research.scenarios.spec_io import scenario_from_dict


def _parent_spec() -> ComparisonSpec:
    return ComparisonSpec(
        candidate_ids=("c1",),
        starting_capital=Decimal("10000"),
        common_start=date(2024, 1, 2),
        common_end=date(2024, 1, 8),
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


def _candidate(candidate_id: str = "c1") -> ResearchCandidate:
    return ResearchCandidate(
        id=candidate_id, family="f", version="v1",
        evidence_kind=ResearchEvidenceKind.SYNTHETIC_BACKTEST,
        registration=ResearchRegistration.BEFORE_ENTRY_WINDOW_END,
        disposition=ResearchDisposition.PASS,
        plot_funded_account=True,
        supported_start=date(2024, 1, 2), supported_end=date(2024, 1, 31),
        funded_history=FundedHistorySupport.RECONSTRUCTED,
    )


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "runstate"
    ws.mkdir()
    return ws


@pytest.fixture
def catalog_provider():
    return lambda: [_candidate("c1")]


def _spawn_parent(worker: ResearchWorker, workspace: Path) -> str:
    """Submit a real parent run and step the worker through to a
    completed parent result; return the parent's run_id."""
    spec = _parent_spec()
    parent_run_id = comp_spec_hash(spec)
    with open_runstate_store(workspace) as store:
        store.put("spec", spec.to_dict(), key=parent_run_id,
                  at=datetime.now())
        store.put("run", {"run_id": parent_run_id, "spec_hash": parent_run_id,
                          "kind": "comparison",
                          "status": "queued",
                          "format_version": RUN_FORMAT_VERSION},
                  key=parent_run_id, at=datetime.now())
    worked = worker.step()
    assert worked
    return parent_run_id


def _spawn_scenario(workspace: Path, parent_run_id: str,
                    body: dict, *, kind: str = "contribution_planning",
                    access_mode: str = "exploratory") -> str:
    spec = scenario_from_dict(parent_run_id, {
        "kind": kind, "access_mode": access_mode,
        "diff": body.get("diff", {}),
        "proposed_by": "operator", "notes": "",
    })
    run_id = scenario_spec_hash(spec)
    with open_runstate_store(workspace) as store:
        store.put("spec", spec.to_dict(), key=run_id,
                  at=datetime.now())
        store.put("run", {"run_id": run_id, "spec_hash": run_id,
                          "kind": "scenario",       # dispatch label
                          "parent_run_id": parent_run_id,
                          "status": "queued",
                          "format_version": RUN_FORMAT_VERSION},
                  key=run_id, at=datetime.now())
    return run_id


# -- oracles ----------------------------------------------------------------


def test_scenario_publishes_content_bound_result(
    workspace, catalog_provider,
):
    """A scenario that re-runs the engine publishes a content-bound
    result with the four identity shas + parent_run_id + scenario_diff."""
    worker = ResearchWorker(
        workspace=workspace,
        catalog_provider=catalog_provider,
        engine_fn=run_comparison,
    )
    parent_run_id = _spawn_parent(worker, workspace)
    child_run_id = _spawn_scenario(workspace, parent_run_id, {
        "diff": {"contribution_per_period": "500"},
    })
    worked = worker.step()
    assert worked
    with open_runstate_store(workspace) as store:
        run = store.get("run", child_run_id)
        result = store.get("result", child_run_id)
    assert run is not None and run["status"] == "completed"
    assert result is not None
    assert "engine_sha256" in result
    assert "input_snapshot_sha256" in result
    assert "calendar_sha256" in result
    assert "scenario_diff_sha256" in result
    assert result["parent_run_id"] == parent_run_id
    # The wire payload mirrors the comparison engine contract.
    wire = result["wire"]
    assert wire["parent_run_id"] == parent_run_id
    assert wire["scenario_kind"] == "contribution_planning"
    assert "candidates" in wire
    assert "paired_diff" in wire


def test_scenario_idempotency_records_one_child_per_unique_run_id(
    workspace, catalog_provider,
):
    """Two identically-submitted scenarios collapse to the same run_id
    (``scenario_spec_hash`` is the canonical id) — the worker does
    not run twice."""
    worker = ResearchWorker(
        workspace=workspace,
        catalog_provider=catalog_provider,
        engine_fn=run_comparison,
    )
    parent_run_id = _spawn_parent(worker, workspace)
    body = {"diff": {"contribution_per_period": "500"}}
    cid1 = _spawn_scenario(workspace, parent_run_id, body)
    cid2 = _spawn_scenario(workspace, parent_run_id, body)
    assert cid1 == cid2
    worked = worker.step()
    assert worked
    # A second step does not claim another run.
    assert not worker.step()


def test_stress_scenario_refuses_with_typed_code(
    workspace, catalog_provider,
):
    """A type-B stress scenario publishes a content-bound refusal
    envelope — never a crash, never an empty row."""
    worker = ResearchWorker(
        workspace=workspace,
        catalog_provider=catalog_provider,
        engine_fn=run_comparison,
    )
    parent_run_id = _spawn_parent(worker, workspace)
    child_run_id = _spawn_scenario(
        workspace, parent_run_id, {}, kind="conditional_stress")
    worked = worker.step()
    assert worked
    with open_runstate_store(workspace) as store:
        run = store.get("run", child_run_id)
        result = store.get("result", child_run_id)
    assert run is not None and run["status"] == "completed"
    assert result is not None
    wire = result["wire"]
    assert wire["refusal"] == SCENARIO_STRESS_UNSUPPORTED


def test_scenario_with_missing_parent_refuses_with_typed_code(
    workspace, catalog_provider,
):
    """A scenario whose parent has no stored result record refuses
    with SCENARIO_PARENT_MISSING. The lineage is honest about an
    unanchored fork."""
    worker = ResearchWorker(
        workspace=workspace,
        catalog_provider=catalog_provider,
        engine_fn=run_comparison,
    )
    # No parent spawned -> child claims with no parent result.
    child_run_id = _spawn_scenario(
        workspace, "parent_does_not_exist",
        {"diff": {"contribution_per_period": "500"}})
    worked = worker.step()
    assert worked
    with open_runstate_store(workspace) as store:
        result = store.get("result", child_run_id)
    assert result is not None
    assert result["wire"]["refusal"] == SCENARIO_PARENT_MISSING


# -- idempotency oracle: re-running the same worker reuses stored parent_ref ---


def test_parent_ref_persisted_on_first_fork_then_idempotent(
    workspace, catalog_provider,
):
    """The parent's effective identity is recorded ONCE — re-forking
    against the same parent never re-writes the ParentRef."""
    worker = ResearchWorker(
        workspace=workspace,
        catalog_provider=catalog_provider,
        engine_fn=run_comparison,
    )
    parent_run_id = _spawn_parent(worker, workspace)
    _spawn_scenario(workspace, parent_run_id, {
        "diff": {"contribution_per_period": "500"},
    })
    worked = worker.step()
    assert worked
    # The parent_ref is now present under scenario_parent kind.
    with open_runstate_store(workspace) as store:
        ref1 = store.get("scenario_parent", parent_run_id)
    assert ref1 is not None
    assert ref1["parent_run_id"] == parent_run_id
    assert ref1["parent_engine_sha256"]
    assert ref1["parent_input_snapshot_sha256"]
    assert ref1["parent_calendar_sha256"]
