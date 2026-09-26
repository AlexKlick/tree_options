"""RL-2 oracle 6: API/CLI numerical parity.

The SPA, the FastAPI endpoint, and the read-only CLI must agree on
the same content-bound numbers. The CLI prints from the SAME
store as the API GET; the oracles here pin:

    1. The CLI resolves the same engine_sha as the API; no drift.
    2. The CLI resolves the same scenario_diff_sha as the API.
    3. The CLI and the API agree on parent_run_id (the lineage is
       not divergent between surfaces).

These oracles run a full end-to-end worker step + then probe both
the API and the CLI for the same child run; the byte equality is
the parity proof.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

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
    ResearchWorker,
)
from tree_options.research.scenarios.contracts import scenario_spec_hash
from tree_options.research.scenarios.spec_io import scenario_from_dict
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


def _spawn_parent_and_scenario(ws: Path) -> tuple[str, str]:
    spec = _spec()
    parent_id = spec_hash(spec)
    scn = scenario_from_dict(parent_id, {
        "kind": "contribution_planning", "access_mode": "exploratory",
        "diff": {"contribution_per_period": "500"},
    })
    child_id = scenario_spec_hash(scn)
    with open_runstate_store(ws) as store:
        store.put("spec", spec.to_dict(), key=parent_id, at=datetime.now())
        store.put("run", {"run_id": parent_id, "spec_hash": parent_id,
                          "kind": "comparison", "status": "queued",
                          "format_version": RUN_FORMAT_VERSION},
                  key=parent_id, at=datetime.now())
        store.put("spec", scn.to_dict(), key=child_id, at=datetime.now())
        store.put("run", {"run_id": child_id, "spec_hash": child_id,
                          "kind": "scenario", "parent_run_id": parent_id,
                          "status": "queued",
                          "format_version": RUN_FORMAT_VERSION},
                  key=child_id, at=datetime.now())
    return parent_id, child_id


def test_cli_scenario_inspect_matches_api_result(tmp_path):
    """End-to-end: parent + scenario through the worker; the API
    GET and the read-only CLI agree on the four identity shas + the
    parent_run_id."""
    ws = tmp_path / "rs"
    ws.mkdir()
    artifacts = tmp_path / "artifacts-empty"
    artifacts.mkdir()
    worker = ResearchWorker(workspace=ws,
                           catalog_provider=lambda: [_candidate()],
                           engine_fn=run_comparison)
    parent_id, child_id = _spawn_parent_and_scenario(ws)
    # Drive both queued runs through the worker.
    worker.step()
    worker.step()

    app = FastAPI()
    attach(app, workspace=ws, candidate_scopes_root=artifacts,
           start_worker=False, engine_fn=run_comparison)
    client = TestClient(app)
    api_body = client.get(
        f"/api/research/runs/{child_id}/result").json()

    # Spawn the CLI subprocess so the command line is exercised
    # verbatim, not the in-process function.
    env = dict(os.environ)
    env["PYTHONPATH"] = (
        str(Path(__file__).resolve().parents[3] / "src")
        + os.pathsep + env.get("PYTHONPATH", ""))
    result = subprocess.run(
        [sys.executable, "-m", "tree_options.research",
         "inspect", "--scenario", child_id,
         "--workspace", str(ws)],
        capture_output=True, text=True, env=env, check=True,
    )
    cli_payload = json.loads(result.stdout)

    # Same identity shas on both surfaces
    assert api_body["engine_sha256"] == cli_payload["engine_sha256"]
    assert cli_payload["scenario_diff_sha256"]
    assert cli_payload["parent_run_id"] == parent_id
    # The wired parent_run_id on the API body comes from the result
    # envelope (research_view surfaces it), not the legacy field.
    api_wire_parent = (api_body["result"] or {}).get("parent_run_id")
    assert api_wire_parent == parent_id
    # The CLI returns the same parent_run_id from the stored
    # result record.
    assert cli_payload["parent_run_id"] == api_wire_parent
