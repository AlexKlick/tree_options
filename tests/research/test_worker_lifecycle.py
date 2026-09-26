"""Worker-lifecycle requeue oracle (RL-2 non-prod schedule).

The 2026-09-25 audit and rl1-exit.md's "Next milestone" both call out
that the bounded worker must handle process interruption honestly: a
run left in ``running`` state by a dead process must requeue at the
next start, recompute deterministically, and reach a terminal state
(either completed or failed) — never silently abandoned.

These oracles drive that path synchronously without subprocesses
(test process and worker share one trex-web unit, no need for a
new Python): they simulate the interruption by ``replace``-ing a
``running`` run back to ``queued``, then starting a fresh worker to
assert the requeue path's terminal behavior.

A separate ``test_interrupted_scenario_requeues_on_restart`` covers
the same path for scenarios (the RL-2 surface).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

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
    ResearchWorker,
)
from tree_options.research.scenarios.contracts import scenario_spec_hash
from tree_options.research.scenarios.spec_io import scenario_from_dict


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


def _spawn_comparison_run(workspace: Path) -> str:
    spec = _spec()
    run_id = spec_hash(spec)
    with open_runstate_store(workspace) as store:
        store.put("spec", spec.to_dict(), key=run_id, at=datetime.now())
        store.put("run",
                  {"run_id": run_id, "spec_hash": run_id,
                   "kind": "comparison", "status": "queued",
                   "format_version": RUN_FORMAT_VERSION},
                  key=run_id, at=datetime.now())
    return run_id


def _workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "rs"
    ws.mkdir()
    return ws


def test_queued_run_reaches_completed_on_first_step(tmp_path):
    ws = _workspace(tmp_path)
    worker = ResearchWorker(
        workspace=ws,
        catalog_provider=lambda: [_candidate()],
        engine_fn=run_comparison,
    )
    run_id = _spawn_comparison_run(ws)
    assert worker.step()
    with open_runstate_store(ws) as store:
        run = store.get("run", run_id)
        result = store.get("result", run_id)
    assert run["status"] == "completed"
    assert result is not None


def test_interrupted_run_requeues_on_start(tmp_path):
    """Simulate process death: the run is ``running``, no terminal
    record written. Reopening the worker (via ``start``) requeues
    ``running`` → ``queued``; the next ``step`` claims and completes
    the run."""
    ws = _workspace(tmp_path)
    worker = ResearchWorker(
        workspace=ws,
        catalog_provider=lambda: [_candidate()],
        engine_fn=run_comparison,
    )
    run_id = _spawn_comparison_run(ws)
    # First claim pushes the run to ``running``.
    with open_runstate_store(ws) as store:
        run = store.get("run", run_id)
        store.replace("run",
                      {**run, "status": "running",
                       "started_at": datetime.now().isoformat(),
                       "requeued_at": datetime.now().isoformat()},
                      key=run_id)
    # A fresh worker start requeues ``running`` and processes it.
    worker.start(poll_seconds=0.1)
    try:
        # Drive at least one step manually so the test is deterministic.
        worker.step()
    finally:
        worker.stop()
    with open_runstate_store(ws) as store:
        run = store.get("run", run_id)
        result = store.get("result", run_id)
    assert run["status"] == "completed"
    assert result is not None


def test_interrupted_scenario_requeues_on_restart(tmp_path):
    """Same requeue path for scenarios — the bounded worker does
    not split by ``kind``; both comparison and scenario runs share
    the same requeue discipline (RL1-03 + RL-2)."""
    ws = _workspace(tmp_path)
    worker = ResearchWorker(
        workspace=ws,
        catalog_provider=lambda: [_candidate()],
        engine_fn=run_comparison,
    )
    spec = _spec()
    parent_id = spec_hash(spec)
    with open_runstate_store(ws) as store:
        store.put("spec", spec.to_dict(), key=parent_id, at=datetime.now())
        store.put("run",
                  {"run_id": parent_id, "spec_hash": parent_id,
                   "kind": "comparison", "status": "queued",
                   "format_version": RUN_FORMAT_VERSION},
                  key=parent_id, at=datetime.now())
    worker.step()  # complete the parent
    with open_runstate_store(ws) as store:
        parent = store.get("run", parent_id)
        assert parent["status"] == "completed"
    scn = scenario_from_dict(parent_id, {
        "kind": "contribution_planning", "access_mode": "exploratory",
        "diff": {"contribution_per_period": "500"},
    })
    child_id = scenario_spec_hash(scn)
    with open_runstate_store(ws) as store:
        store.put("spec", scn.to_dict(), key=child_id, at=datetime.now())
        store.put("run",
                  {"run_id": child_id, "spec_hash": child_id,
                   "kind": "scenario", "parent_run_id": parent_id,
                   "status": "queued",
                   "format_version": RUN_FORMAT_VERSION},
                  key=child_id, at=datetime.now())
    with open_runstate_store(ws) as store:
        run = store.get("run", child_id)
        store.replace("run",
                      {**run, "status": "running",
                       "started_at": datetime.now().isoformat()},
                      key=child_id)
    worker.start(poll_seconds=0.1)
    try:
        worker.step()
    finally:
        worker.stop()
    with open_runstate_store(ws) as store:
        run = store.get("run", child_id)
        result = store.get("result", child_id)
    assert run["status"] == "completed"
    assert result is not None
    assert result["parent_run_id"] == parent_id
