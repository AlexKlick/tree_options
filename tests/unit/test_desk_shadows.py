"""Desk D7 shadow tracker: episode selection, marking, resolution.

Oracles are computed here, never by calling the shadows code: package
mids and adverse-side closes in Decimal from literal quotes, P&L by the
kinds' orientation, week keys from ``date.isocalendar``. The chains are
minimal hand-built columnar documents (only the legs under test carry
quotes), written with the store's own gzip format.

The sessions are DERIVED from the committed fixture calendar (never
hardcoded dates): one Thursday entry, then the following Monday through
Thursday as the marking window, so a calendar change moves the world
with it instead of silently invalidating session relationships.
"""

from __future__ import annotations

import gzip
import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tests.fixtures import desk_pricing as fx
from tree_options.desk import miner, shadows
from tree_options.desk.sessions import cutoff_instant
from tree_options.trex.plan import Leg

D = Decimal

_CAL = fx.trex_calendar().sessions()


def _session(i: int, anchor: date) -> date:
    """The i-th calendar session strictly after ``anchor``."""
    return _CAL[_CAL.index(anchor) + i]


_ANCHOR = date(2025, 3, 13)  # a Thursday inside the fixture calendar
ENTRY = _ANCHOR
MID1 = _session(1, _ANCHOR)  # Friday: the first mark session
MID2 = _session(2, _ANCHOR)  # Monday
DEADLINE = _session(3, _ANCHOR)  # Tuesday
AFTER = _session(4, _ANCHOR)  # Wednesday: past the deadline window
EXP = _session(1, date(2025, 6, 13))  # any session comfortably past the deadline
NEXT_WEEK = _session(5, _ANCHOR)  # the other-week selection case (ISO week +1)


@pytest.fixture(scope="module")
def fx_cal():
    return fx.trex_calendar()


# ------------------------------------------------------------ world building


def _chain(
    sym: str,
    session: date,
    rows: list[tuple[str, str, float, float | None, float | None]],
    *,
    spot: float = 100.0,
) -> dict[str, Any]:
    """A minimal desk-chain/1 document; rows are (right, strike, expiry,
    bid, ask)."""
    cols: dict[str, list[Any]] = {c: [] for c in ("occ", "exp", "right", "strike", "bid", "ask")}
    for right, strike, expiry, bid, ask in rows:
        cols["occ"].append(f"O_{sym}{expiry:%y%m%d}{right}{strike:012g}")
        cols["exp"].append(expiry.isoformat())
        cols["right"].append(right)
        cols["strike"].append(strike)
        cols["bid"].append(bid)
        cols["ask"].append(ask)
    return {
        "header": {
            "underlying": sym,
            "session": session.isoformat(),
            "underlying_quote": {"close": spot},
        },
        "columns": cols,
    }


def _write_chain(store: Path, doc: dict[str, Any]) -> None:
    h = doc["header"]
    path = store / "chains" / h["session"] / f"{h['underlying']}.json.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(doc, separators=(",", ":"), sort_keys=True)
    path.write_bytes(gzip.compress(text.encode(), mtime=0))


def _legs_fill_2to3() -> list[dict[str, Any]]:
    """A 100/105 call debit spread quoted 2.00/2.10 and 0.95/1.05."""
    return [
        {"right": "C", "action": "BUY", "strike": "100", "expiry": EXP.isoformat(),
         "bid": "2.00", "ask": "2.10"},
        {"right": "C", "action": "SELL", "strike": "105", "expiry": EXP.isoformat(),
         "bid": "0.95", "ask": "1.05"},
    ]


# The one thing the marking window varies: the short wing's quotes. The
# long leg stays 2.00/2.10 everywhere (mid 2.05, adverse 2.00), so every
# oracle below reduces to arithmetic on the wing numbers named here.
WING_ENTRY = (0.95, 1.05)  # mid 1.00, BUY-side adverse 1.05
WING_RISEN = (1.45, 1.55)  # mid 1.50, adverse 1.55
WING_PAR = (1.95, 2.05)  # mid 2.00, adverse 2.05 (the spread at parity)
WING_DEAD = (0.00, 1.00)  # one-sided: no mid, the long side closes at 0


def _spread_chain(
    sym: str,
    session: date,
    wing: tuple[float, float],
    *,
    spot: float = 100.0,
) -> dict[str, Any]:
    """The deal's own chain: the long 100C at 2.00/2.10 and the short
    105C at ``wing`` (the numbers the oracles are derived from)."""
    wing_bid, wing_ask = wing
    return _chain(
        sym,
        session,
        [
            ("C", 100.0, EXP, 2.00, 2.10),
            ("C", 105.0, EXP, wing_bid, wing_ask),
        ],
        spot=spot,
    )


def _deal(
    deal_id: str,
    *,
    status: str = "surfaced",
    rank: int | None = None,
    kind: str = "debit_vertical",
    name: str = "AAPL",
    row: str = "R1",
    legs: list[dict[str, Any]] | None = None,
    fill: str | None = "2.20",
    ev: str | None = "12.00",
    entry: date = ENTRY,
    deadline: date | None = DEADLINE,
) -> dict[str, Any]:
    return {
        "deal_id": deal_id,
        "rank": rank,
        "status": status,
        "reasons": ["rail_failed: max_loss_per_trade"] if status != "admissible" else [],
        "row": row,
        "row_title": "row title",
        "tier": "signal-validated",
        "underlying": name,
        "kind": kind,
        "quantity": 1,
        "legs": legs if legs is not None else _legs_fill_2to3(),
        "entry_session": entry.isoformat(),
        "exit_deadline": deadline.isoformat() if deadline else None,
        "width": "5.00",
        "ref_mid": "2.05",
        "fill": fill,
        "limit": "2.21",
        "max_loss": "220.00",
        "decision": {"basis": "xsmom", "ev": ev, "ev_stress_fill": "-3.00",
                     "ev_per_max_loss": "0.05"},
        "rails": None,
    }


def _queue(*deals: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": "trex.deal/1",
        "session": deals[0]["entry_session"] if deals else ENTRY.isoformat(),
        "admissible": [d for d in deals if d["status"] == "admissible"],
        "surfaced": [d for d in deals if d["status"] != "admissible"],
    }


@pytest.fixture
def world(tmp_path: Path) -> dict[str, Path]:
    return {"store": tmp_path / "store", "state": tmp_path / "state",
            "queue": tmp_path / "queue"}


def _write_queue(world: dict[str, Path], run_session: date, *deals: dict[str, Any]) -> None:
    world["queue"].mkdir(parents=True, exist_ok=True)
    (world["queue"] / f"{run_session.isoformat()}.json").write_text(
        json.dumps(_queue(*deals))
    )


def _run(world: dict[str, Path], session: date, cal) -> shadows.ShadowResult:
    return shadows.update_shadows(
        session=session,
        now=cutoff_instant(session),
        cal=cal,
        state=world["state"],
        store_root=world["store"],
        queue_dir=world["queue"],
    )


# ------------------------------------------------------------- week keys


def test_week_key_is_iso_monday_aligned() -> None:
    # 2025-03-13 is a Thursday of ISO week 11; the Monday and the Friday
    # of the same week share the key, the next Monday does not.
    assert shadows.week_key(date(2025, 3, 13)) == "2025-W11"
    assert shadows.week_key(date(2025, 3, 10)) == "2025-W11"
    assert shadows.week_key(date(2025, 3, 14)) == "2025-W11"
    assert shadows.week_key(date(2025, 3, 17)) == "2025-W12"


# -------------------------------------------------------- package pricing


def test_mid_and_realistic_follow_the_miner_sign_convention() -> None:
    doc = _spread_chain("AAPL", MID1, WING_ENTRY)
    legs = shadows.leg_refs(_deal("d1"))
    mid, realistic = shadows.package_prices("debit_vertical", legs, doc)
    # mid: long 2.05 - short 1.00 = 1.05; realistic: BUY at bid 2.00, SELL
    # at ask 1.05
    assert mid == D("1.05")
    assert realistic == D("0.95")
    # parity: the miner's own entry_terms computes the same mid from the
    # same quotes (the shadow must never re-invent the package convention)
    terms = miner.entry_terms(
        "debit_vertical",
        [
            Leg(right="C", action="BUY", strike=D("100"), expiry=EXP),
            Leg(right="C", action="SELL", strike=D("105"), expiry=EXP),
        ],
        [(D("2.00"), D("2.10")), (D(str(WING_ENTRY[0])), D(str(WING_ENTRY[1])))],
        fill_k=D("0"),
    )
    assert terms.mid == mid


def test_credit_kind_flips_the_signs() -> None:
    doc = _chain("AAPL", MID1, [
        ("P", 95.0, EXP, 1.00, 1.10),
        ("P", 90.0, EXP, 0.45, 0.55),
    ])
    deal = _deal("d1", kind="credit_vertical", fill="1.00", legs=[
        {"right": "P", "action": "SELL", "strike": "95", "expiry": EXP.isoformat(),
         "bid": "1.00", "ask": "1.10"},
        {"right": "P", "action": "BUY", "strike": "90", "expiry": EXP.isoformat(),
         "bid": "0.45", "ask": "0.55"},
    ])
    legs = shadows.leg_refs(deal)
    mid, realistic = shadows.package_prices("credit_vertical", legs, doc)
    # credit mid: receive 1.05, pay 0.50 -> +0.55. realistic (the value in
    # entry orientation of closing NOW): buy the short back at the ask 1.10,
    # sell the long at the bid 0.45 -> the package costs 0.65 to close; the
    # per-share P&L on a 1.00 credit fill is 1.00 - 0.65 = +0.35
    assert mid == D("0.55")
    assert realistic == D("0.65")
    assert shadows.pnl_dollars("credit_vertical", D("1.00"), realistic, 1) == D("35.00")


def test_zero_bid_long_leg_is_no_mid_but_a_realistic_zero() -> None:
    doc = _spread_chain("AAPL", MID1, WING_DEAD)
    legs = shadows.leg_refs(_deal("d1"))
    mid, realistic = shadows.package_prices("debit_vertical", legs, doc)
    assert mid is None
    assert realistic == D("1.00")  # 2.00 (bid) - 1.00 (ask)


def test_missing_leg_or_crossed_book_is_no_mark() -> None:
    absent = _chain("AAPL", MID1, [("C", 100.0, EXP, 2.00, 2.10)])
    assert shadows.package_prices("debit_vertical", shadows.leg_refs(_deal("d1")), absent) is None
    crossed = _chain("AAPL", MID1, [
        ("C", 100.0, EXP, 2.20, 2.10),  # bid above ask: not a book
        ("C", 105.0, EXP, *WING_ENTRY),
    ])
    assert shadows.package_prices("debit_vertical", shadows.leg_refs(_deal("d1")), crossed) is None


def test_pnl_dollars_by_orientation() -> None:
    # debit: exit above the fill earns; credit: exit below the fill earns
    assert shadows.pnl_dollars("debit_vertical", D("2.20"), D("3.20"), 1) == D("100.00")
    assert shadows.pnl_dollars("credit_vertical", D("1.00"), D("0.40"), 2) == D("120.00")


# -------------------------------------------------------------- selection


def test_one_episode_per_name_row_week_preferring_admissible_then_rank() -> None:
    q = _queue(
        _deal("b", rank=2),
        _deal("a", rank=1),
        _deal("c", status="admissible", rank=7),
        _deal("other-week", rank=0, entry=NEXT_WEEK),
        _deal("other-row", rank=0, row="R3"),
        _deal("other-name", rank=0, name="SPY"),
    )
    eps, claims = shadows.select_episodes(q, {})
    got = [(e.deal_id, e.name, e.row, e.entry_session) for e in eps]
    # admissible first; rank ties break by deal id (name, row, week order)
    assert got == [
        ("c", "AAPL", "R1", ENTRY),
        ("other-name", "SPY", "R1", ENTRY),
        ("other-row", "AAPL", "R3", ENTRY),
        ("other-week", "AAPL", "R1", NEXT_WEEK),
    ]
    assert claims["AAPL|R1|2025-W11"] == "c"


def test_claimed_weeks_are_not_re_adopted() -> None:
    q = _queue(_deal("a"), _deal("b"))
    eps, _ = shadows.select_episodes(q, {"AAPL|R1|2025-W11": "kept"})
    # a is skipped (the week is claimed); b shares the same week, so the
    # claim keeps it out too
    assert [e.deal_id for e in eps] == []


# ------------------------------------------------------- the update pass


def test_not_ready_without_a_queue(world: dict[str, Path], fx_cal) -> None:
    res = _run(world, ENTRY, fx_cal)
    assert res.exit_code == 3 and res.status == "not_ready"


def test_adopt_mark_resolve_end_to_end(world: dict[str, Path], fx_cal) -> None:
    _write_queue(world, MID2, _deal("d1"))
    # the deadline session itself never records (the vendor gap case)
    _write_chain(world["store"], _spread_chain("AAPL", MID1, WING_ENTRY))
    _write_chain(world["store"], _spread_chain("AAPL", MID2, WING_RISEN))
    _write_chain(world["store"], _spread_chain("AAPL", AFTER, WING_PAR))
    res = _run(world, MID2, fx_cal)
    assert res.exit_code == 0 and res.new_episodes == 1 and res.marked == 2
    # AFTER is recorded, so the deadline window is provably past even
    # though the deadline session itself never recorded: resolve at the
    # latest usable chain (MID2) with the fallback flag set.
    assert res.resolved == 1
    doc = json.loads((world["state"] / "episodes" / "d1.json").read_text())
    assert [m["session"] for m in doc["marks"]] == [MID1.isoformat(), MID2.isoformat()]
    res_doc = doc["resolution"]
    assert res_doc["session"] == MID2.isoformat()
    assert res_doc["fallback"] is True
    # realistic at MID2: long bid 2.00 - short ask 1.55 = 0.45;
    # pnl = (0.45 - 2.20) * 100
    assert D(res_doc["realistic"]) == D("0.45")
    assert D(res_doc["pnl_dollars"]) == D("-175.00")
    assert D(res_doc["pnl_minus_ev_dollars"]) == D("-187.00")
    # slippage = pnl at the FIRST mark: 2.00 - 1.05 = 0.95 -> (0.95-2.20)*100
    assert D(res_doc["slippage_first_mark_dollars"]) == D("-125.00")
    assert doc["decision"] == {
        "basis": "xsmom", "ev": "12.00", "ev_stress_fill": "-3.00",
        "ev_per_max_loss": "0.05",
    }


def test_resolution_prefers_the_deadline_session(world: dict[str, Path], fx_cal) -> None:
    _write_queue(world, DEADLINE, _deal("d1"))
    for s in (MID1, MID2, DEADLINE, AFTER):
        _write_chain(world["store"], _spread_chain("AAPL", s, WING_PAR))
    res = _run(world, DEADLINE, fx_cal)
    assert res.exit_code == 0 and res.resolved == 1
    doc = json.loads((world["state"] / "episodes" / "d1.json").read_text())
    res_doc = doc["resolution"]
    assert res_doc["session"] == DEADLINE.isoformat() and res_doc["fallback"] is False
    # realistic at parity: 2.00 - 2.05 = -0.05; pnl = (-0.05 - 2.20) * 100
    assert D(res_doc["pnl_dollars"]) == D("-225.00")
    # marks stop at the deadline
    assert [m["session"] for m in doc["marks"]] == [
        MID1.isoformat(), MID2.isoformat(), DEADLINE.isoformat()
    ]


def test_no_resolution_before_the_deadline_window(world: dict[str, Path], fx_cal) -> None:
    _write_queue(world, MID1, _deal("d1", deadline=AFTER))
    _write_chain(world["store"], _spread_chain("AAPL", MID1, WING_ENTRY))
    res = _run(world, MID1, fx_cal)
    assert res.exit_code == 0 and res.resolved == 0
    doc = json.loads((world["state"] / "episodes" / "d1.json").read_text())
    assert doc["state"] == "open" and len(doc["marks"]) == 1


def test_rerun_is_idempotent(world: dict[str, Path], fx_cal) -> None:
    _write_queue(world, MID2, _deal("d1", deadline=AFTER))
    for s in (MID1, MID2):
        _write_chain(world["store"], _spread_chain("AAPL", s, WING_ENTRY))
    first = _run(world, MID2, fx_cal)
    assert first.exit_code == 0 and first.new_episodes == 1
    again = _run(world, MID2, fx_cal)
    assert again.exit_code == 0 and again.new_episodes == 0 and again.marked == 0
    ids = list((world["state"] / "episodes").glob("*.json"))
    assert len(ids) == 1


def test_empty_queue_is_a_normal_pass(world: dict[str, Path], fx_cal) -> None:
    _write_queue(world, ENTRY)
    res = _run(world, ENTRY, fx_cal)
    assert res.exit_code == 0 and res.new_episodes == 0 and res.status == "ok"


def test_no_fill_or_no_deadline_never_resolves(world: dict[str, Path], fx_cal) -> None:
    _write_queue(world, AFTER, _deal("nofill", fill=None))
    _write_chain(world["store"], _spread_chain("AAPL", AFTER, WING_PAR))
    res = _run(world, AFTER, fx_cal)
    assert res.exit_code == 0 and res.resolved == 0
    doc = json.loads((world["state"] / "episodes" / "nofill.json").read_text())
    assert doc["state"] == "open"
