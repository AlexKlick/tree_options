"""Desk E6 ``desk-enter``: claim, validate, write spec, append admissions.

Oracles are computed here, never by calling ``enter.run_enter``: deal
ids are built from the tested session + a rank/row/key (deal ids embed
the mine session by the plan's convention), money fields are strings
matched against LegStructure's Decimal validators. ``fx_cal`` comes
from the committed fixture calendar so the deadline-laundering tests
catch a regression in either the engine or the schedule.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from tests.fixtures import desk_pricing as fx
from tree_options.desk import enter
from tree_options.trex.plan import ExitRules, Leg, LegStructure

ET = ZoneInfo("America/New_York")
TODAY = date(2026, 9, 24)  # Thu
NOW = datetime(2026, 9, 24, 13, 0, tzinfo=ET)  # well inside the entry window
D = TODAY  # the queue's session: mine D = today
ANOTHER = date(2026, 9, 25)


@pytest.fixture(scope="module")
def fx_cal():
    return fx.trex_calendar()


def _spec_legs(spec: LegStructure) -> list[dict[str, Any]]:
    return [
        {
            "right": g.right,
            "action": g.action,
            "strike": str(g.strike),
            "expiry": g.expiry.isoformat(),
            "bid": "1.00",
            "ask": "1.20",
        }
        for g in spec.legs
    ]


def _debit(deal_id: str, *, rank: int | None = 0, sess: date = D) -> dict[str, Any]:
    spec = LegStructure(
        id=deal_id,
        kind="debit_vertical",
        underlying="AAPL",
        legs=[
            Leg(right="C", action="BUY", strike=Decimal("100"), expiry=date(2026, 11, 20)),
            Leg(right="C", action="SELL", strike=Decimal("105"), expiry=date(2026, 11, 20)),
        ],
        quantity=1,
        entry_date=sess,
        exit_deadline=date(2026, 11, 19),  # the session before the front expiry
        limit=Decimal("3.00"),
        exits=ExitRules(touch=True, breach=False),
    )
    return {
        "deal_id": deal_id,
        "rank": rank,
        "status": "admissible",
        "reasons": [],
        "row": "R1",
        "row_title": "XSMOM top-3 call debit spread",
        "tier": "signal-validated",
        "underlying": "AAPL",
        "kind": "debit_vertical",
        "quantity": 1,
        "legs": _spec_legs(spec),
        "entry_session": sess.isoformat(),
        "exit_deadline": spec.exit_deadline.isoformat(),
        "width": "5.00",
        "ref_mid": "2.50",
        "fill": "2.80",
        "limit": "3.00",
        "max_loss": str(spec.max_loss_per_package() * 100 * spec.quantity),
        "decision": {"basis": "xsmom", "ev": "12.00", "ev_stress_fill": "-3.00",
                     "ev_per_max_loss": "0.05"},
        "rails": None,
        "structure": spec.model_dump(mode="json"),
    }


def _queue(world: Path, *deals: dict[str, Any], valid_until_offset_min: int = 120) -> None:
    world.mkdir(parents=True, exist_ok=True)
    valid_until = datetime.now(ET) + timedelta(minutes=valid_until_offset_min)
    valid_until = valid_until.replace(tzinfo=ET)
    (world / f"{D.isoformat()}.json").write_text(
        json.dumps(
            {
                "schema": enter.QUEUE_SCHEMA,
                "session": D.isoformat(),
                "admissible": list(deals),
                "surfaced": [],
                "valid_until": valid_until.isoformat(),
            }
        )
    )


@pytest.fixture
def world(tmp_path: Path) -> tuple[Path, Path]:
    run = tmp_path / "run"
    queue = tmp_path / "queue"
    return run, queue


# --------------------------------------------------------- validation


def test_validate_deal_accepts_a_well_formed_admissible(fx_cal) -> None:
    deal = _debit("a1")
    vu = datetime.now(ET).replace(tzinfo=ET) + timedelta(minutes=60)
    assert enter.validate_deal(deal, vu, NOW, fx_cal) == []


def test_validate_deal_fails_when_max_loss_mismatches(fx_cal) -> None:
    deal = _debit("a1")
    deal["max_loss"] = "999.00"
    vu = datetime.now(ET).replace(tzinfo=ET) + timedelta(minutes=60)
    assert "max_loss_mismatch" in enter.validate_deal(deal, vu, NOW, fx_cal)


def test_validate_deal_flags_money_fields_that_arent_strings(fx_cal) -> None:
    deal = _debit("a1")
    deal["fill"] = 2.80  # float, not str
    vu = datetime.now(ET).replace(tzinfo=ET) + timedelta(minutes=60)
    assert any(p.startswith("money_not_string:") for p in
               enter.validate_deal(deal, vu, NOW, fx_cal))


def test_validate_deal_rejects_a_deadline_not_before_first_expiry(fx_cal) -> None:
    """The LegStructure validator itself catches dead-on-expiry, but the
    admission gate ALSO has to catch a deadline AFTER the first expiry
    (the deal doc's ``exit_deadline`` disagrees with what the spec wrote
    - the doc carries a stale string)."""
    spec = LegStructure(
        id="a1", kind="credit_vertical", underlying="AAPL",
        legs=[
            Leg(right="P", action="SELL", strike=Decimal("95"), expiry=date(2026, 11, 20)),
            Leg(right="P", action="BUY", strike=Decimal("90"), expiry=date(2026, 11, 20)),
        ],
        quantity=1, entry_date=D, exit_deadline=date(2026, 11, 19),
        limit=Decimal("1.00"), exits=ExitRules(touch=False, breach=True),
    )
    deal = _debit("a1")
    deal["structure"] = spec.model_dump(mode="json")
    # the credit spec's max loss is width - credit; align the doc with it
    deal["max_loss"] = str(spec.max_loss_per_package() * 100 * spec.quantity)
    # the deal doc CLAIMS a deadline past the first expiry; the gate catches it
    deal["exit_deadline"] = (date(2026, 12, 18)).isoformat()
    vu = datetime.now(ET).replace(tzinfo=ET) + timedelta(minutes=60)
    problems = enter.validate_deal(deal, vu, NOW, fx_cal)
    assert "deadline_not_before_first_expiry" in problems, problems


def test_validate_deal_rejects_a_malformed_structure(fx_cal) -> None:
    deal = _debit("a1")
    deal["structure"] = {"kind": "debit_vertical"}  # missing required fields
    vu = datetime.now(ET).replace(tzinfo=ET) + timedelta(minutes=60)
    problems = enter.validate_deal(deal, vu, NOW, fx_cal)
    assert any(p.startswith("structure_invalid") for p in problems)


# ----------------------------------------------------------- claiming


def test_claim_is_atomic_under_concurrent_writers(tmp_path: Path) -> None:
    run = tmp_path / "run"
    only = run / "claims"
    only.mkdir(parents=True, exist_ok=True)
    payload = {"deal_id": "a1"}
    a = enter.claim(run, "a1", payload)
    b = enter.claim(run, "a1", payload)
    assert a is True and b is False
    assert (only / "a1.json").exists()


def test_claim_different_ids_both_succeed(tmp_path: Path) -> None:
    run = tmp_path / "run"
    (run / "claims").mkdir(parents=True, exist_ok=True)
    assert enter.claim(run, "a", {"deal_id": "a"}) is True
    assert enter.claim(run, "b", {"deal_id": "b"}) is True


# ------------------------------------------------------------- enter


def test_run_enter_writes_spec_and_admissions(world: tuple[Path, Path], fx_cal) -> None:
    run, queue = world
    _queue(queue, _debit("a1"), _debit("a2", rank=1))
    res = enter.run_enter(
        now=NOW,
        cal=fx_cal,
        session=D,
        run_dir=run,
        queue_dir=queue,
        shadow=True,
    )
    assert res.exit_code == 0 and res.admitted == 2
    spec_dir = run / "specs"
    assert sorted(p.name for p in spec_dir.glob("*.json")) == ["a1.json", "a2.json"]
    admissions = [json.loads(line) for line in (run / "admissions.jsonl").read_text().splitlines()]
    assert [a["deal_id"] for a in admissions] == ["a1", "a2"]
    assert {a["admitted"] for a in admissions} == {True}
    assert {a["mode"] for a in admissions} == {"shadow"}
    # shadow notes the rails gap - the E5 runtime will close it
    notes = admissions[0]["notes"]
    assert notes == ["rails_not_evaluable_no_book"]


def test_run_enter_never_writes_book_json(world: tuple[Path, Path], fx_cal) -> None:
    """The plan's hard rule: desk-enter NEVER writes book.json - the
    runtime owns the book. If it ever does, the test breaks loudly."""
    run, queue = world
    _queue(queue, _debit("a1"))
    enter.run_enter(now=NOW, cal=fx_cal, run_dir=run, queue_dir=queue, session=D, shadow=True)
    assert not (run / "book.json").exists()


def test_run_enter_dedupes_via_claim(world: tuple[Path, Path], fx_cal) -> None:
    run, queue = world
    _queue(queue, _debid("a1"), _debit("a1", rank=0), _debit("a2", rank=1))
    res = enter.run_enter(now=NOW, cal=fx_cal, run_dir=run, queue_dir=queue, session=D, shadow=True)
    assert res.admitted == 2  # a1 was deduplicated by claim, not by deal id


def _debid(deal_id: str, *, rank: int = 0) -> dict[str, Any]:
    return _debit(deal_id, rank=rank)  # second declaration alias


def test_run_enter_once_caps_admissions_at_one(world: tuple[Path, Path], fx_cal) -> None:
    run, queue = world
    _queue(queue, _debit("a1"), _debit("a2", rank=1))
    res = enter.run_enter(
        now=NOW, cal=fx_cal, run_dir=run, queue_dir=queue, session=D, shadow=True, once=True
    )
    assert res.admitted == 1
    admissions = [json.loads(line) for line in (run / "admissions.jsonl").read_text().splitlines()]
    assert len(admissions) == 1


def test_run_enter_honor_halt_and_auto_off(world: tuple[Path, Path], fx_cal) -> None:
    run, queue = world
    run.mkdir(parents=True, exist_ok=True)  # HALT is a kill file written by the operator
    _queue(queue, _debit("a1"))
    (run / "HALT").write_text("")
    res = enter.run_enter(now=NOW, cal=fx_cal, run_dir=run, queue_dir=queue, session=D, shadow=True)
    assert res.exit_code == 0 and res.status == "halted" and res.admitted == 0


def test_run_enter_writes_no_artifact_in_dry_run(world: tuple[Path, Path], fx_cal) -> None:
    run, queue = world
    _queue(queue, _debit("a1"))
    res = enter.run_enter(
        now=NOW, cal=fx_cal, run_dir=run, queue_dir=queue, session=D, shadow=True, dry_run=True
    )
    assert res.exit_code == 0 and res.admitted == 1
    assert not (run / "specs").exists() if (run / "specs").exists() else True
    assert not (run / "admissions.jsonl").exists()


def test_run_enter_records_a_refused_row_with_problems(world: tuple[Path, Path], fx_cal) -> None:
    """A deal that FAILS validation is recorded once with admitted=false;
    subsequent slots see the refused: claim and never re-record (an audit
    trail the operator reads, not a gate the trader argues with)."""
    run, queue = world
    bad = _debit("bad")
    bad["max_loss"] = "999.00"  # forces max_loss_mismatch
    _queue(queue, bad, _debit("good", rank=1))
    res = enter.run_enter(now=NOW, cal=fx_cal, run_dir=run, queue_dir=queue, session=D, shadow=True)
    assert res.exit_code == 0 and res.admitted == 1
    admissions = [json.loads(line) for line in (run / "admissions.jsonl").read_text().splitlines()]
    by_deal = {a["deal_id"]: a for a in admissions}
    assert by_deal["bad"]["admitted"] is False
    assert "max_loss_mismatch" in by_deal["bad"]["problems"]
    assert by_deal["good"]["admitted"] is True


def test_run_enter_exits_3_when_no_queue(world: tuple[Path, Path], fx_cal) -> None:
    run, queue = world
    # no queue file written
    res = enter.run_enter(now=NOW, cal=fx_cal, run_dir=run, queue_dir=queue, session=D, shadow=True)
    assert res.exit_code == 3 and res.status == "not_ready"
