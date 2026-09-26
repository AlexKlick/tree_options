"""Lineage-enforcement regression tests (RL-2 hardening, 2026-09-26).

The post-landing review found the two lineage gates were asserted but
never enforced:

    P1-1 — ``parent_changed`` compared the parent envelope against a
           ParentRef built FROM that same envelope, so the refusal
           could never fire; the stored ParentRef was write-only
           (``load_parent_ref`` had no production caller).
    P1-2 — the child copied the parent's ``input_snapshot_sha256`` /
           ``calendar_sha256`` without recomputing against the live
           catalog, so drifted candidate artifacts produced a child
           whose receipt misrepresented its inputs.

These oracles pin the ENFORCED behavior end-to-end through the
bounded worker (plus one engine-level pair), and the
"unchanged-input identity" acceptance row that previously cited a
nonexistent test.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, date, datetime
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
from tree_options.research.scenarios.lineage import ParentRef
from tree_options.research.scenarios.refusal_codes import (
    SCENARIO_PARENT_CHANGED,
)
from tree_options.research.scenarios.spec_io import scenario_from_dict


def _candidate(artifact_sha: str = "f" * 64) -> ResearchCandidate:
    return ResearchCandidate(
        id="c1", family="f", version="v1",
        evidence_kind=ResearchEvidenceKind.SYNTHETIC_BACKTEST,
        registration=ResearchRegistration.BEFORE_ENTRY_WINDOW_END,
        disposition=ResearchDisposition.PASS,
        plot_funded_account=True,
        supported_start=date(2024, 1, 2), supported_end=date(2024, 1, 31),
        funded_history=FundedHistorySupport.RECONSTRUCTED,
        artifact_hashes={"fixture": artifact_sha},
    )


def _spec() -> ComparisonSpec:
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


def _ws(tmp_path: Path) -> Path:
    ws = tmp_path / "rs"
    ws.mkdir(exist_ok=True)
    return ws


def _spawn_parent(ws: Path, worker: ResearchWorker) -> str:
    s = _spec()
    pid = spec_hash(s)
    with open_runstate_store(ws) as store:
        store.put("spec", s.to_dict(), key=pid, at=datetime.now())
        store.put("run", {"run_id": pid, "spec_hash": pid,
                          "kind": "comparison", "status": "queued",
                          "format_version": RUN_FORMAT_VERSION},
                  key=pid, at=datetime.now())
    assert worker.step()
    return pid


def _spawn_scenario(ws: Path, parent_run_id: str) -> str:
    scn = scenario_from_dict(parent_run_id, {
        "kind": "contribution_planning", "access_mode": "exploratory",
        "diff": {"contribution_per_period": "500"},
    })
    cid = scenario_spec_hash(scn)
    with open_runstate_store(ws) as store:
        store.put("spec", scn.to_dict(), key=cid, at=datetime.now())
        store.put("run", {"run_id": cid, "spec_hash": cid,
                          "kind": "scenario", "parent_run_id": parent_run_id,
                          "status": "queued",
                          "format_version": RUN_FORMAT_VERSION},
                  key=cid, at=datetime.now())
    return cid


# -- P1-1: the stored attach-time ParentRef is actually consulted --------


def test_engine_refuses_when_attach_ref_drifted(tmp_path):
    """Engine-level: passing a PREVIOUSLY-STORED attach ref whose
    engine sha no longer matches the parent's current envelope must
    refuse — this is the comparison the pre-fix code could never make
    (it compared the envelope against itself)."""
    from tree_options.research.scenarios.engine import fork_parent_and_replay

    ws = _ws(tmp_path)
    worker = ResearchWorker(
        workspace=ws, catalog_provider=lambda: [_candidate()],
        engine_fn=run_comparison)
    parent_id = _spawn_parent(ws, worker)
    with open_runstate_store(ws) as store:
        parent_result = store.get("result", parent_id)
    assert parent_result is not None

    stale_ref = ParentRef(
        parent_run_id=parent_id,
        parent_spec_hash=parent_result["spec_hash"],
        parent_engine_sha256="a" * 64,  # what the engine was AT ATTACH
        parent_input_snapshot_sha256=parent_result[
            "input_snapshot_sha256"],
        parent_calendar_sha256=parent_result["calendar_sha256"],
    )
    scn = scenario_from_dict(parent_id, {
        "kind": "contribution_planning",
        "diff": {"contribution_per_period": "500"},
    })
    envelope = {
        "engine_sha256": parent_result["engine_sha256"],
        "input_snapshot_sha256": parent_result["input_snapshot_sha256"],
        "calendar_sha256": parent_result["calendar_sha256"],
        "spec_hash": parent_result["spec_hash"],
        "status": "completed",
    }
    with open_runstate_store(ws) as store:
        outcome = fork_parent_and_replay(
            store, scenario=scn, parent_result_envelope=envelope,
            catalog_provider=lambda: [_candidate()],
            engine_fn=run_comparison, attach_ref=stale_ref)
    assert outcome.refusal is not None
    assert outcome.refusal.code == SCENARIO_PARENT_CHANGED


def test_engine_allows_when_attach_ref_matches(tmp_path):
    """Engine-level: an attach ref that still matches the envelope
    does not refuse (no false positives)."""
    from tree_options.research.scenarios.engine import fork_parent_and_replay

    ws = _ws(tmp_path)
    worker = ResearchWorker(
        workspace=ws, catalog_provider=lambda: [_candidate()],
        engine_fn=run_comparison)
    parent_id = _spawn_parent(ws, worker)
    with open_runstate_store(ws) as store:
        parent_result = store.get("result", parent_id)
    envelope = {
        "engine_sha256": parent_result["engine_sha256"],
        "input_snapshot_sha256": parent_result["input_snapshot_sha256"],
        "calendar_sha256": parent_result["calendar_sha256"],
        "spec_hash": parent_result["spec_hash"],
        "status": "completed",
    }
    matching_ref = ParentRef(
        parent_run_id=parent_id,
        parent_spec_hash=parent_result["spec_hash"],
        parent_engine_sha256=parent_result["engine_sha256"],
        parent_input_snapshot_sha256=parent_result[
            "input_snapshot_sha256"],
        parent_calendar_sha256=parent_result["calendar_sha256"],
    )
    scn = scenario_from_dict(parent_id, {
        "kind": "contribution_planning",
        "diff": {"contribution_per_period": "500"},
    })
    with open_runstate_store(ws) as store:
        outcome = fork_parent_and_replay(
            store, scenario=scn, parent_result_envelope=envelope,
            catalog_provider=lambda: [_candidate()],
            engine_fn=run_comparison, attach_ref=matching_ref)
    assert outcome.refusal is None


def test_worker_refuses_fork_of_drifted_parent(tmp_path):
    """End-to-end through the bounded worker: a ParentRef stored at an
    earlier attach (simulated by pre-seeding a stale one) blocks a
    fork of the parent whose identity has since changed. The stored
    ref is READ back and compared — the write-only defect's direct
    regression test."""
    ws = _ws(tmp_path)
    worker = ResearchWorker(
        workspace=ws, catalog_provider=lambda: [_candidate()],
        engine_fn=run_comparison)
    parent_id = _spawn_parent(ws, worker)
    with open_runstate_store(ws) as store:
        current = store.get("result", parent_id)
    stale = ParentRef(
        parent_run_id=parent_id,
        parent_spec_hash=current["spec_hash"],
        parent_engine_sha256="Z" * 64,  # engine source changed since attach
        parent_input_snapshot_sha256=current["input_snapshot_sha256"],
        parent_calendar_sha256=current["calendar_sha256"],
    )
    with open_runstate_store(ws) as store:
        from tree_options.research.scenarios.lineage import store_parent_ref
        store_parent_ref(store, stale, at=datetime.now())
    child_id = _spawn_scenario(ws, parent_id)
    assert worker.step()
    with open_runstate_store(ws) as store:
        result = store.get("result", child_id)
        run = store.get("run", child_id)
    assert run["status"] == "completed"
    assert result["wire"]["refusal"] == SCENARIO_PARENT_CHANGED
    assert "engine_sha256" in result["wire"]["message"]


# -- P1-2: the child's input snapshot is recomputed, not inherited --------


def test_worker_refuses_fork_when_candidate_artifacts_drifted(tmp_path):
    """The parent ran against catalog artifact sha X; by fork time the
    candidate's artifact hash is Y (same id, new data). The child's
    recomputed input snapshot no longer matches the parent's stored
    one — the fork must refuse instead of publishing a child whose
    receipt claims the parent's inputs."""
    ws = _ws(tmp_path)
    worker_parent = ResearchWorker(
        workspace=ws, catalog_provider=lambda: [_candidate("f" * 64)],
        engine_fn=run_comparison)
    parent_id = _spawn_parent(ws, worker_parent)
    # The catalog now serves the same candidate id with drifted data.
    worker_child = ResearchWorker(
        workspace=ws, catalog_provider=lambda: [_candidate("9" * 64)],
        engine_fn=run_comparison)
    child_id = _spawn_scenario(ws, parent_id)
    assert worker_child.step()
    with open_runstate_store(ws) as store:
        result = store.get("result", child_id)
        parent_result = store.get("result", parent_id)
    assert result["wire"]["refusal"] == SCENARIO_PARENT_CHANGED
    assert "input" in result["wire"]["message"]
    # The refusal is honest about WHICH identity drifted.
    assert parent_result["input_snapshot_sha256"] != (
        result["wire"].get("live_input_snapshot_sha256"))


def test_worker_child_records_recomputed_input_snapshot(tmp_path):
    """Happy path: unchanged inputs — the child's result carries the
    RECOMPUTED snapshot (equal to the parent's), not a blind copy, so
    a future reader can distinguish verified identity from inherited
    bytes."""
    ws = _ws(tmp_path)
    worker = ResearchWorker(
        workspace=ws, catalog_provider=lambda: [_candidate()],
        engine_fn=run_comparison)
    parent_id = _spawn_parent(ws, worker)
    child_id = _spawn_scenario(ws, parent_id)
    assert worker.step()
    with open_runstate_store(ws) as store:
        result = store.get("result", child_id)
    assert result["wire"]["refusal"] is None
    assert result["input_snapshot_verified_against_parent"] is True


# -- the acceptance row that previously cited a nonexistent test --------


def test_unchanged_inputs_share_three_shas(tmp_path):
    """RL-2 acceptance row 2, for real this time: a successful fork's
    ``engine_sha256`` + ``input_snapshot_sha256`` + ``calendar_sha256``
    EQUAL the parent's (same process, same catalog, same pinned
    calendar), while ``scenario_diff_sha256`` is child-only."""
    ws = _ws(tmp_path)
    worker = ResearchWorker(
        workspace=ws, catalog_provider=lambda: [_candidate()],
        engine_fn=run_comparison)
    parent_id = _spawn_parent(ws, worker)
    child_id = _spawn_scenario(ws, parent_id)
    assert worker.step()
    with open_runstate_store(ws) as store:
        parent = store.get("result", parent_id)
        child = store.get("result", child_id)
    assert child["wire"]["refusal"] is None
    assert child["engine_sha256"] == parent["engine_sha256"]
    assert child["input_snapshot_sha256"] == parent["input_snapshot_sha256"]
    assert child["calendar_sha256"] == parent["calendar_sha256"]
    assert child["scenario_diff_sha256"]
    assert "scenario_diff_sha256" not in parent


# -- P2-2: the CLI must not crash on scenario run ids -------------------


def test_cli_inspect_run_handles_scenario_run_id(tmp_path):
    """``inspect --run <scenario_run_id>`` previously raised
    KeyError('input_snapshot') because scenario results legitimately
    lack that key. The CLI must print a payload and exit 0."""
    ws = _ws(tmp_path)
    worker = ResearchWorker(
        workspace=ws, catalog_provider=lambda: [_candidate()],
        engine_fn=run_comparison)
    parent_id = _spawn_parent(ws, worker)
    child_id = _spawn_scenario(ws, parent_id)
    assert worker.step()
    env = dict(os.environ)
    env["PYTHONPATH"] = (
        str(Path(__file__).resolve().parents[2] / "src")
        + os.pathsep + env.get("PYTHONPATH", ""))
    proc = subprocess.run(
        [sys.executable, "-m", "tree_options.research",
         "inspect", "--run", child_id, "--workspace", str(ws)],
        capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["run_id"] == child_id
    assert "engine_sha256" in payload
