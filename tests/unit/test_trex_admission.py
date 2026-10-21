"""Fact-based admission: the owner's status files decide, never a constant.

Oracles: the frozen contract models and their blocker functions
(``risk_snapshot_blockers`` / ``reconciliation_blockers``), the contract
freshness bounds, the fixture documents in ``tests/contracts/fixtures`` and
the desk runtime's ``monitor.json`` shape (``DeskRuntime._write_health``).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tests.unit import owner_facts
from tests.unit.trex_fakes import contract_fixture
from tree_options.time.sessions import shift_instant
from tree_options.trex import admission, pause
from tree_options.trex.contracts import (
    Blocker,
    Reconciliation,
    RiskSnapshot,
    Wave2RunDir,
    reconciliation_blockers,
    risk_snapshot_blockers,
)
from tree_options.trex.contracts.defaults import (
    RECONCILIATION_MAX_AGE_S,
    RISK_SNAPSHOT_STALE_AFTER_S,
)
from tree_options.trex.desk_runtime import HEARTBEAT_MAX_AGE_S

NOW = datetime(2026, 10, 22, 14, 5, tzinfo=UTC)
ACCOUNT = owner_facts.ACCOUNT
STALE = Blocker.RISK_SNAPSHOT_STALE.value
RECON = Blocker.RECONCILIATION_REQUIRED.value
EXIT = admission.EXIT_OWNER_NOT_READY
CASH = Blocker.INSUFFICIENT_CASH.value
ROOT = Path(__file__).resolve().parents[2]


def write_snapshot(run_dir: Path, **kwargs) -> dict:
    return owner_facts.write_snapshot(run_dir, NOW, **kwargs)


def write_reconciliation(run_dir: Path, **kwargs) -> dict:
    return owner_facts.write_reconciliation(run_dir, NOW, **kwargs)


def write_monitor(run_dir: Path, **kwargs) -> dict:
    return owner_facts.write_monitor(run_dir, NOW, **kwargs)


def healthy(run_dir: Path) -> Path:
    return owner_facts.healthy(run_dir, NOW)


def test_missing_facts_name_each_missing_fact(tmp_path):
    assert admission.owner_fact_blockers(tmp_path, NOW) == (STALE, RECON, EXIT)


def test_fresh_clean_facts_admit(tmp_path):
    assert admission.owner_fact_blockers(healthy(tmp_path), NOW) == ()


def test_the_blockers_agree_with_the_contract_functions(tmp_path):
    """The reader feeds the contract's own blocker functions: the same
    document gives the same verdict through both paths."""
    healthy(tmp_path)
    for age in (0, RISK_SNAPSHOT_STALE_AFTER_S, RISK_SNAPSHOT_STALE_AFTER_S + 1):
        doc = write_snapshot(tmp_path, age_s=age)
        expected = tuple(
            b.value for b in risk_snapshot_blockers(RiskSnapshot.model_validate(doc), NOW)
        )
        assert admission.owner_fact_blockers(tmp_path, NOW) == expected
    for age in (0, RECONCILIATION_MAX_AGE_S, RECONCILIATION_MAX_AGE_S + 1):
        write_snapshot(tmp_path)
        doc = write_reconciliation(tmp_path, age_s=age)
        expected = tuple(
            b.value for b in reconciliation_blockers(Reconciliation.model_validate(doc), NOW)
        )
        assert admission.owner_fact_blockers(tmp_path, NOW) == expected


@pytest.mark.parametrize(
    "changes",
    [
        {"age_s": RISK_SNAPSHOT_STALE_AFTER_S + 1},
        {"age_s": -1},  # future-dated
        {
            "status": "unavailable",
            "open_loss_cents": None,
            "day_realized_loss_cents": None,
            "cash_cents": None,
            "equity_gross_cents": None,
            "outside_positions": [],
        },
    ],
)
def test_an_unusable_risk_snapshot_is_risk_snapshot_stale(tmp_path, changes):
    healthy(tmp_path)
    write_snapshot(tmp_path, **changes)
    assert admission.owner_fact_blockers(tmp_path, NOW) == (STALE,)


@pytest.mark.parametrize(
    "changes",
    [
        {"age_s": RECONCILIATION_MAX_AGE_S + 1},
        {"age_s": -1},
        {"status": "unavailable"},
        {
            "status": "reconciliation_required",
            "mismatches": contract_fixture("reconciliation_required.json")["mismatches"],
        },
    ],
)
def test_anything_but_a_fresh_clean_reconciliation_is_reconciliation_required(tmp_path, changes):
    healthy(tmp_path)
    write_reconciliation(tmp_path, **changes)
    assert admission.owner_fact_blockers(tmp_path, NOW) == (RECON,)


def test_exit_owner_freshness_bound_is_the_canary_exit_owner_bound():
    assert admission.EXIT_OWNER_HEALTH_MAX_AGE_S == HEARTBEAT_MAX_AGE_S


@pytest.mark.parametrize(
    "changes",
    [
        {"age_s": HEARTBEAT_MAX_AGE_S + 1},
        {"age_s": -5},  # future-dated
        {"connected": False},
        {"connected": None},
        {"tick_failures": 2},
        {"tick_failures": None},
        {"at": "2026-10-22T14:04:59Z"},  # not epoch seconds
        {"at": None},
        {"at": True},
    ],
)
def test_an_unready_exit_owner_blocks_entries(tmp_path, changes):
    healthy(tmp_path)
    age = changes.pop("age_s", 3)
    write_monitor(tmp_path, age_s=age, **changes)
    assert admission.owner_fact_blockers(tmp_path, NOW) == (EXIT,)


def test_the_exit_owner_bound_is_inclusive(tmp_path):
    healthy(tmp_path)
    write_monitor(tmp_path, age_s=HEARTBEAT_MAX_AGE_S)
    assert admission.owner_fact_blockers(tmp_path, NOW) == ()


@pytest.mark.parametrize(
    "name", ["ledger/risk-snapshot.json", "ledger/reconciliation.json", "monitor.json"]
)
@pytest.mark.parametrize(
    "payload",
    [b"", b"{not json", b"[]", b'{"schema": "trex-risk-snapshot/9"}', b"\xff\xfe"],
)
def test_unreadable_fact_files_fail_closed_and_never_raise(tmp_path, name, payload):
    healthy(tmp_path)
    (tmp_path / name).write_bytes(payload)
    expected = {
        "ledger/risk-snapshot.json": (STALE,),
        "ledger/reconciliation.json": (RECON,),
        "monitor.json": (EXIT,),
    }[name]
    assert admission.owner_fact_blockers(tmp_path, NOW) == expected


def test_an_oversized_fact_file_is_unreadable(tmp_path):
    healthy(tmp_path)
    doc = json.loads(Wave2RunDir(tmp_path).risk_snapshot().read_text())
    padded = json.dumps(doc) + " " * (admission.MAX_FACT_BYTES + 1)
    Wave2RunDir(tmp_path).risk_snapshot().write_text(padded)
    assert admission.owner_fact_blockers(tmp_path, NOW) == (STALE,)


@pytest.mark.parametrize(
    "name", ["ledger/risk-snapshot.json", "ledger/reconciliation.json", "monitor.json"]
)
def test_a_symlinked_fact_file_is_not_a_fact(tmp_path, name):
    healthy(tmp_path)
    real = tmp_path / name
    moved = tmp_path / "elsewhere.json"
    real.rename(moved)
    os.symlink(moved, real)
    assert len(admission.owner_fact_blockers(tmp_path, NOW)) == 1


def test_facts_bind_the_requested_account(tmp_path):
    healthy(tmp_path)
    assert admission.owner_fact_blockers(tmp_path, NOW, account_id=ACCOUNT) == ()
    assert admission.owner_fact_blockers(tmp_path, NOW, account_id="DU7777777") == (STALE, RECON)


def test_a_reconciliation_of_another_account_is_not_a_reconciliation(tmp_path):
    healthy(tmp_path)
    write_reconciliation(tmp_path, account_id="DU7777777")
    assert admission.owner_fact_blockers(tmp_path, NOW) == (RECON,)


def test_negative_settled_cash_is_insufficient_cash(tmp_path):
    healthy(tmp_path)
    write_snapshot(tmp_path, cash_cents=-1)
    assert admission.owner_fact_blockers(tmp_path, NOW) == (CASH,)
    write_snapshot(tmp_path, cash_cents=0)
    assert admission.owner_fact_blockers(tmp_path, NOW) == ()


def test_cash_blockers_compare_required_cents_with_settled_cash(tmp_path):
    doc = write_snapshot(tmp_path)
    snapshot = RiskSnapshot.model_validate(doc)
    cash = doc["cash_cents"]
    assert admission.cash_blockers(snapshot, NOW, cash) == ()
    assert admission.cash_blockers(snapshot, NOW, cash + 1) == (CASH,)
    # no usable snapshot: the stale blocker speaks, never a cash claim
    assert admission.cash_blockers(None, NOW, cash + 1) == ()
    stale = shift_instant(NOW, RISK_SNAPSHOT_STALE_AFTER_S + 1)
    assert admission.cash_blockers(snapshot, stale, cash + 1) == ()


def test_entry_blockers_add_the_kill_files_to_the_facts(tmp_path):
    healthy(tmp_path)
    assert admission.entry_blockers(tmp_path, NOW) == ()
    pause.pause_entries(tmp_path, actor="a", now=NOW)
    assert admission.entry_blockers(tmp_path, NOW) == (Blocker.ENTRIES_PAUSED.value,)
    (tmp_path / "HALT").touch()
    assert admission.entry_blockers(tmp_path, NOW) == (
        Blocker.ENTRIES_PAUSED.value,
        admission.HALTED,
    )


def test_entry_send_blockers_are_the_kill_files_only(tmp_path):
    """What every entry SENDER checks before spending authority: the pause
    and HALT, never an owner fact (an empty run dir lacks every fact, yet
    only the kill files appear here)."""
    assert admission.entry_send_blockers(tmp_path) == ()
    pause.pause_entries(tmp_path, actor="a", now=NOW)
    assert admission.entry_send_blockers(tmp_path) == (Blocker.ENTRIES_PAUSED.value,)
    (tmp_path / "HALT").symlink_to(tmp_path / "missing")  # dangling: still a HALT
    assert admission.entry_send_blockers(tmp_path) == (
        Blocker.ENTRIES_PAUSED.value,
        admission.HALTED,
    )
    pause.resume_entries(tmp_path, actor="a", now=NOW)
    assert admission.entry_send_blockers(tmp_path) == (admission.HALTED,)
    healthy(tmp_path)
    assert admission.entry_blockers(tmp_path, NOW) == admission.entry_send_blockers(tmp_path)


def test_selection_rule_is_never_an_owner_fact(tmp_path):
    """Product entries are owner-approved: the sealed-rule activation gates
    AUTOMATIC admission only (desk.enter), never the owner's facts."""
    assert "selection_rule_not_activated" not in admission.owner_fact_blockers(tmp_path, NOW)
    assert "selection_rule_not_activated" not in admission.entry_blockers(tmp_path, NOW)


def test_the_fact_report_shows_ages_and_verdicts(tmp_path):
    write_snapshot(tmp_path, age_s=42)
    write_reconciliation(
        tmp_path,
        age_s=7,
        status="reconciliation_required",
        mismatches=contract_fixture("reconciliation_required.json")["mismatches"],
    )
    write_monitor(tmp_path, age_s=4)
    report = admission.owner_fact_report(tmp_path, NOW)
    assert report["risk_snapshot"]["age_s"] == 42
    assert report["risk_snapshot"]["status"] == "complete"
    assert report["risk_snapshot"]["usable"] is True
    assert report["reconciliation"]["age_s"] == 7
    assert report["reconciliation"]["status"] == "reconciliation_required"
    assert report["reconciliation"]["mismatches"] == 1
    assert report["exit_owner"]["age_s"] == 4 and report["exit_owner"]["ready"] is True
    assert report["blockers"] == [RECON]


def test_the_fact_report_of_an_empty_dir_says_absent(tmp_path):
    report = admission.owner_fact_report(tmp_path, NOW)
    assert report["risk_snapshot"] == {"state": "absent", "usable": False}
    assert report["reconciliation"] == {"state": "absent", "clean": False}
    assert report["exit_owner"]["ready"] is False
    assert report["blockers"] == [STALE, RECON, EXIT]


def test_naive_now_refuses(tmp_path):
    with pytest.raises(ValueError):
        admission.owner_fact_blockers(tmp_path, datetime(2026, 10, 22, 14, 5))


def test_admission_and_pause_import_no_broker_library():
    probe = (
        "import sys, tree_options.trex.admission, tree_options.trex.pause, "
        "tree_options.desk.enter, tree_options.desk.enter_supervised; "
        "sys.exit(1 if 'ib_async' in sys.modules else 0)"
    )
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    assert subprocess.run([sys.executable, "-c", probe], env=env, check=False).returncode == 0
