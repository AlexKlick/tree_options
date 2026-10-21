"""Admission previews cannot migrate into the executable lane."""

from datetime import datetime, time
from decimal import ROUND_CEILING, Decimal

import pytest
from tests.unit import owner_facts

from tree_options.desk import enter, selection
from tree_options.desk.contracts import ContractError
from tree_options.desk.evidence import EvidenceStore
from tree_options.trex import admission, pause
from tree_options.trex.clock import ET
from tree_options.trex.contracts import Blocker

#: the constant blockers the fact-based screen replaced (wave 2, lane A)
RETIRED = {
    "broker_risk_snapshot_not_available",
    "broker_reconciliation_not_available",
    "broker_margin_not_verified",
    "runtime_exit_observations_not_wired",
}


def run(world, **kwargs):
    return enter.run_enter(
        now=kwargs.pop("now", world.now),
        cal=world.cal,
        database=world.db,
        queue_dir=world.queues,
        run_dir=world.root / "execution",
        **kwargs,
    )


def automatic_only(world) -> set[str]:
    """What only AUTOMATIC admission adds on top of the owner's facts."""
    proposed = selection.load_config().status == "PROPOSED"
    return {"shadow_only_build"} | ({"selection_rule_not_activated"} if proposed else set())


def test_default_preview_records_blockers_but_never_spec_or_book(world):
    world.queue_file()
    doc = run(world)
    assert doc["admitted"] == 0 and doc["execution_enabled"] is False
    assert doc["previewed"] == 1
    row = doc["decisions"][0]
    # no owner status files at all: each missing fact is named
    assert set(row["blockers"]) == automatic_only(world) | {
        Blocker.RISK_SNAPSHOT_STALE.value,
        Blocker.RECONCILIATION_REQUIRED.value,
        admission.EXIT_OWNER_NOT_READY,
    }
    assert not RETIRED & set(row["blockers"])
    assert not list(world.root.rglob("specs"))
    assert not list(world.root.rglob("book.json"))
    with EvidenceStore(world.db, readonly=True) as store:
        assert len(store.all("admission_preview")) == 1
    run(world)
    with EvidenceStore(world.db, readonly=True) as store:
        assert len(store.all("admission_preview")) == 1


@pytest.mark.parametrize("flag", ["HALT", "AUTO_OFF"])
def test_operator_flags_are_additional_blockers(world, flag):
    world.queue_file()
    rd = world.root / "execution"
    rd.mkdir()
    (rd / flag).touch()
    assert flag.lower() in run(world)["decisions"][0]["blockers"]


def test_armed_switch_is_explicitly_unsupported_even_with_dummy_book(world):
    world.queue_file()
    rd = world.root / "execution"
    rd.mkdir()
    (rd / "book.json").write_text("{}")
    with pytest.raises(ContractError, match="execution_not_implemented"):
        run(world, shadow=False)
    assert not world.db.exists()


@pytest.mark.parametrize("hour,minute", [(9, 49), (11, 30), (12, 0)])
def test_closed_entry_window_writes_no_preview(world, hour, minute):
    world.queue_file()
    now = datetime.combine(world.entry, time(hour, minute), ET)
    assert run(world, now=now)["status"] == "outside_entry_window"
    assert not world.db.exists()


def test_dry_run_no_persistent_side_effects(world):
    world.queue_file()
    before = sorted(world.root.rglob("*"))
    assert run(world, dry_run=True)["previewed"] == 1
    assert sorted(world.root.rglob("*")) == before


def test_seal_or_identity_tampering_cannot_be_admitted(world):
    world.queue["admissible"][0]["structure"]["deal_id"] = "forged"
    world.queue_file()
    with pytest.raises(ContractError):
        run(world)
    assert not world.db.exists()


def test_explicit_old_session_cannot_bypass_real_entry_date(world):
    world.queue_file()
    with pytest.raises(ContractError, match="session_not_current"):
        run(world, session=world.cal.sessions()[world.cal.ordinal(world.session) - 1])


def test_fresh_owner_facts_leave_only_the_automatic_admission_blockers(world):
    world.queue_file()
    owner_facts.healthy(world.root / "execution", world.now)
    row = run(world)["decisions"][0]
    assert set(row["blockers"]) == automatic_only(world)
    assert row["decision"] == "blocked" and row["execution_enabled"] is False


@pytest.mark.parametrize(
    "stage,expected",
    [
        (
            lambda rd, now: owner_facts.write_snapshot(rd, now, age_s=121),
            Blocker.RISK_SNAPSHOT_STALE.value,
        ),
        (
            lambda rd, now: owner_facts.write_reconciliation(rd, now, status="unavailable"),
            Blocker.RECONCILIATION_REQUIRED.value,
        ),
        (
            lambda rd, now: owner_facts.write_monitor(rd, now, age_s=31),
            admission.EXIT_OWNER_NOT_READY,
        ),
        (
            lambda rd, now: owner_facts.write_monitor(rd, now, connected=False),
            admission.EXIT_OWNER_NOT_READY,
        ),
        (
            lambda rd, now: owner_facts.write_snapshot(rd, now, cash_cents=-5),
            Blocker.INSUFFICIENT_CASH.value,
        ),
        (
            lambda rd, now: pause.pause_entries(rd, actor="owner", now=now),
            Blocker.ENTRIES_PAUSED.value,
        ),
    ],
)
def test_one_bad_fact_gives_its_specific_blocker(world, stage, expected):
    world.queue_file()
    run_dir = world.root / "execution"
    owner_facts.healthy(run_dir, world.now)
    stage(run_dir, world.now)
    row = run(world)["decisions"][0]
    assert set(row["blockers"]) == automatic_only(world) | {expected}


def test_a_deal_costing_more_than_settled_cash_is_insufficient_cash(world):
    world.queue_file()
    run_dir = world.root / "execution"
    owner_facts.healthy(run_dir, world.now)
    raw = world.queue["admissible"][0]["structure"]
    # the oracle: the deal's worst case at its cap, x100 per package, in cents
    cost = Decimal(raw["limit"]) * 100 * raw["quantity"] * 100
    need = int(cost.to_integral_value(rounding=ROUND_CEILING))
    owner_facts.write_snapshot(run_dir, world.now, cash_cents=need)
    assert set(run(world)["decisions"][0]["blockers"]) == automatic_only(world)
    owner_facts.write_snapshot(run_dir, world.now, cash_cents=need - 1)
    blockers = set(run(world)["decisions"][0]["blockers"])
    assert blockers == automatic_only(world) | {Blocker.INSUFFICIENT_CASH.value}


def test_a_changed_fact_is_a_new_audit_event(world):
    world.queue_file()
    run_dir = world.root / "execution"
    owner_facts.healthy(run_dir, world.now)
    run(world)
    pause.pause_entries(run_dir, actor="owner", now=world.now)
    run(world)
    with EvidenceStore(world.db, readonly=True) as store:
        assert len(store.all("admission_preview")) == 2


def test_research_directory_env_is_not_execution_directory(world, monkeypatch):
    monkeypatch.setenv("DESK_PAPER_DIR", str(world.root / "research"))
    monkeypatch.setenv("HOME", str(world.root))
    assert enter.execution_directory() != world.root / "research"
    monkeypatch.setenv("TREX_DESK_RUN_DIR", str(world.root / "explicit-run"))
    assert enter.execution_directory() == world.root / "explicit-run"
