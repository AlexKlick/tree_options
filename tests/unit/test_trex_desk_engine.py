"""Desk decision core (lane E3): decide_structure / step / adopted_role for
every LegStructure kind.

Every expected price, quantity and date is a literal worked out by hand in
the comment beside it (mid = cents((bid + ask) / 2), half-up); nothing is
read back from the implementation. Session dates come from the committed
trex NYSE calendar (2026-11-26 is Thanksgiving, 2026-11-27 a half day).
"""

from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from tree_options.trex import clock
from tree_options.trex.clock import EntryWindow
from tree_options.trex.engine import (
    AbortEntry,
    CloseOrder,
    ComboQuote,
    DividendCalendar,
    EngineConfig,
    EntryOrder,
    ExitReason,
    NoAction,
    Snapshot,
    StructureDecision,
    WorkingOrder,
    adopted_role,
    configure,
    decide_structure,
    drain,
    last_hold_session,
    step,
)
from tree_options.trex.plan import LegStructure
from tree_options.trex.state import BookState, Status, StructureState

ET = ZoneInfo("America/New_York")
ENTRY = date(2026, 9, 24)  # Thu
DEADLINE = date(2026, 10, 9)  # Fri
FRONT = date(2026, 10, 16)  # Fri, monthly expiry
BACK = date(2026, 11, 20)
LAST_HOLD = date(2026, 10, 15)  # the session before FRONT
MIDDAY = time(13, 0)


@pytest.fixture(autouse=True)
def _engine(monkeypatch: pytest.MonkeyPatch) -> None:
    configure(
        EngineConfig(
            entry_window=EntryWindow(time(9, 45), time(12, 0)),
            entry_max_cycles=4,
            exit_max_mid_cycles=3,
        )
    )
    # the committed trex calendar, never an override left by another test
    monkeypatch.delenv("TREX_CALENDAR", raising=False)
    monkeypatch.setattr(clock, "_calendar", None)


def _leg(right: str, action: str, strike: str, expiry: date = FRONT) -> dict[str, Any]:
    return {"right": right, "action": action, "strike": strike, "expiry": expiry}


def _spec(
    sid: str,
    kind: str,
    legs: list[dict[str, Any]],
    limit: str,
    *,
    touch: bool = False,
    breach: bool = False,
    take_profit: dict[str, str] | None = None,
    stop_loss: dict[str, str] | None = None,
    confirm: int = 3,
    **overrides: Any,
) -> LegStructure:
    raw: dict[str, Any] = {
        "id": sid,
        "underlying": "SPY",
        "kind": kind,
        "legs": legs,
        "quantity": 2,
        "entry_date": ENTRY,
        "exit_deadline": DEADLINE,
        "limit": limit,
        "exits": {
            "touch": touch,
            "breach": breach,
            "take_profit": take_profit,
            "stop_loss": stop_loss,
            "stop_confirm_ticks": confirm,
        },
    }
    raw.update(overrides)
    return LegStructure(**raw)


LONG_PUT = _spec("lp", "long_single", [_leg("P", "BUY", "95")], "2.00", touch=True)
LONG_CALL = _spec("lc", "long_single", [_leg("C", "BUY", "105")], "3.00", touch=True)
DEBIT_PUT = _spec(
    "dp", "debit_vertical", [_leg("P", "BUY", "100"), _leg("P", "SELL", "95")], "2.00", touch=True
)
DEBIT_CALL = _spec(
    "dc", "debit_vertical", [_leg("C", "BUY", "100"), _leg("C", "SELL", "110")], "4.00", touch=True
)
CREDIT_PUT = _spec(
    "cp", "credit_vertical", [_leg("P", "SELL", "95"), _leg("P", "BUY", "90")], "1.00", breach=True
)
CREDIT_CALL = _spec(
    "cc",
    "credit_vertical",
    [_leg("C", "SELL", "110"), _leg("C", "BUY", "115")],
    "1.20",
    breach=True,
)
# wings: puts 90-85 = 5, calls 120-110 = 10; the package's max value is 10
CONDOR = _spec(
    "ic",
    "iron_condor",
    [
        _leg("P", "BUY", "85"),
        _leg("P", "SELL", "90"),
        _leg("C", "SELL", "110"),
        _leg("C", "BUY", "120"),
    ],
    "2.00",
    breach=True,
)
CALENDAR = _spec(
    "cal", "calendar", [_leg("C", "SELL", "100", FRONT), _leg("C", "BUY", "100", BACK)], "1.50"
)
DIAGONAL = _spec(
    "dg", "diagonal", [_leg("C", "SELL", "105", FRONT), _leg("C", "BUY", "100", BACK)], "6.00"
)


def _at(day: date, hhmm: time) -> datetime:
    return datetime(day.year, day.month, day.day, hhmm.hour, hhmm.minute, tzinfo=ET)


def _q(bid: str, ask: str) -> ComboQuote:
    return ComboQuote(Decimal(bid), Decimal(ask))


def _snap(
    spec: LegStructure,
    ts: datetime,
    quote: ComboQuote | None,
    spot: str | None = None,  # no spot: no touch/breach decision unless a test sets one
    dividend: tuple[date, date, str] | None = None,  # (ex_date, prev_session, amount)
    short_call_mids: dict[str, str] | None = None,
) -> Snapshot:
    return Snapshot(
        ts=ts,
        spots={} if spot is None else {spec.underlying: Decimal(spot)},
        quotes={spec.id: quote},
        dividends={}
        if dividend is None
        else {
            spec.underlying: DividendCalendar(
                ex_date=dividend[0], prev_session=dividend[1], amount=Decimal(dividend[2])
            )
        },
        short_call_mids={}
        if short_call_mids is None
        else {k: Decimal(v) for k, v in short_call_mids.items()},
    )


def _open(filled: int = 2, entry_fill: str | None = None, **kw: Any) -> StructureState:
    return StructureState(
        status=Status.OPEN,
        filled_qty=filled,
        entry_fill=None if entry_fill is None else Decimal(entry_fill),
        **kw,
    )


def _exiting(reason: str | None, cycles: int = 0, filled: int = 2, **kw: Any) -> StructureState:
    return StructureState(
        status=Status.EXIT_WORKING,
        filled_qty=filled,
        exit_reason=reason,
        exit_cycles=cycles,
        **kw,
    )


def _decide(
    spec: LegStructure,
    st: StructureState,
    day: date,
    hhmm: time,
    quote: ComboQuote | None,
    spot: str | None = None,
    dividend: tuple[date, date, str] | None = None,
    short_call_mids: dict[str, str] | None = None,
) -> StructureDecision:
    return decide_structure(
        spec, st, _snap(spec, _at(day, hhmm), quote, spot, dividend, short_call_mids)
    )


def _action(*args: Any, **kwargs: Any) -> Any:
    return _decide(*args, **kwargs).action


# -- entry -----------------------------------------------------------------


class TestEntryLadder:
    @pytest.mark.parametrize(
        ("spec", "cycles", "quote", "expected"),
        [
            # debit: min(mid, cap) then min(ask, cap)
            (DEBIT_PUT, 0, ("1.80", "1.90"), ("BUY", "1.85")),  # mid 1.85 < cap 2.00
            (DEBIT_PUT, 0, ("2.10", "2.30"), ("BUY", "2.00")),  # mid 2.20 > cap: the cap
            (DEBIT_PUT, 4, ("1.80", "1.90"), ("BUY", "1.90")),  # escalated: the ask
            (DEBIT_PUT, 4, ("1.95", "2.10"), ("BUY", "2.00")),  # ask 2.10 > cap: the cap
            (LONG_CALL, 0, ("2.40", "2.60"), ("BUY", "2.50")),
            (CALENDAR, 0, ("1.20", "1.40"), ("BUY", "1.30")),
            # credit: max(mid, floor) then max(bid, floor), SELL at a positive price
            (CREDIT_PUT, 0, ("1.10", "1.30"), ("SELL", "1.20")),  # mid 1.20 > floor 1.00
            (CREDIT_PUT, 0, ("0.80", "0.90"), ("SELL", "1.00")),  # mid 0.85 < floor: the floor
            (CREDIT_PUT, 4, ("1.10", "1.30"), ("SELL", "1.10")),  # escalated: the bid
            (CREDIT_PUT, 4, ("0.80", "1.30"), ("SELL", "1.00")),  # bid 0.80 < floor: the floor
            (CONDOR, 0, ("2.20", "2.60"), ("SELL", "2.40")),
        ],
    )
    def test_ladder(
        self,
        spec: LegStructure,
        cycles: int,
        quote: tuple[str, str],
        expected: tuple[str, str],
    ) -> None:
        st = StructureState(entry_cycles=cycles)
        act = _action(spec, st, ENTRY, time(10, 0), _q(*quote))
        assert act == EntryOrder(side=expected[0], qty=2, limit=Decimal(expected[1]))

    def test_a_working_entry_carries_the_ladder_price_for_the_remainder(self) -> None:
        st = StructureState(status=Status.ENTER_WORKING, filled_qty=1, entry_cycles=1)
        act = _action(CREDIT_PUT, st, ENTRY, time(10, 30), _q("1.10", "1.30"))
        assert act == EntryOrder(side="SELL", qty=1, limit=Decimal("1.20"))

    def test_fully_filled_entry_orders_nothing(self) -> None:
        st = StructureState(status=Status.ENTER_WORKING, filled_qty=2)
        act = _action(CREDIT_PUT, st, ENTRY, time(10, 30), _q("1.10", "1.30"))
        assert isinstance(act, NoAction) and act.reason == "entry filled"

    def test_the_cap_rounds_toward_safety(self) -> None:
        # a cap/floor finer than a cent: the debit rounds DOWN (never above
        # the cap), the credit UP (never below the floor)
        debit = DEBIT_PUT.model_copy(update={"limit": Decimal("1.505")})
        act = _action(debit, StructureState(), ENTRY, time(10, 0), _q("1.60", "1.70"))
        assert act == EntryOrder(side="BUY", qty=2, limit=Decimal("1.50"))
        credit = CREDIT_PUT.model_copy(update={"limit": Decimal("1.005")})
        act = _action(credit, StructureState(), ENTRY, time(10, 0), _q("0.80", "0.90"))
        assert act == EntryOrder(side="SELL", qty=2, limit=Decimal("1.01"))

    def test_no_positive_debit_price_orders_nothing(self) -> None:
        act = _action(DEBIT_PUT, StructureState(), ENTRY, time(10, 0), _q("-0.05", "0.05"))
        assert isinstance(act, NoAction) and act.reason == "no positive price"

    @pytest.mark.parametrize(
        ("day", "hhmm", "status", "expected"),
        [
            (date(2026, 9, 23), time(10, 0), Status.PLANNED, ("no_action", "before entry date")),
            (ENTRY, time(9, 44), Status.PLANNED, ("no_action", "before entry window")),
            (ENTRY, time(12, 1), Status.PLANNED, ("abort", "entry window closed")),
            (ENTRY, time(12, 1), Status.ENTER_WORKING, ("abort", "entry window closed")),
            (date(2026, 9, 25), time(10, 0), Status.PLANNED, ("abort", "entry date passed")),
        ],
    )
    def test_dates_and_window(
        self, day: date, hhmm: time, status: Status, expected: tuple[str, str]
    ) -> None:
        act = _action(CONDOR, StructureState(status=status), day, hhmm, _q("2.20", "2.60"))
        kind, reason = expected
        assert isinstance(act, NoAction if kind == "no_action" else AbortEntry)
        assert act.reason == reason

    def test_no_quote_waits(self) -> None:
        act = _action(CONDOR, StructureState(), ENTRY, time(10, 0), None)
        assert isinstance(act, NoAction) and act.reason == "no quote"

    def test_never_opens_inside_expiry_safety(self) -> None:
        # entry 11-25 with a holiday deadline 11-26: the schema allows it,
        # but 11-25 is already the last session before the 11-27 expiry
        spec = _spec(
            "late",
            "debit_vertical",
            [
                _leg("P", "BUY", "100", date(2026, 11, 27)),
                _leg("P", "SELL", "95", date(2026, 11, 27)),
            ],
            "2.00",
            entry_date=date(2026, 11, 25),
            exit_deadline=date(2026, 11, 26),
        )
        act = _action(spec, StructureState(), date(2026, 11, 25), time(10, 0), _q("1.80", "1.90"))
        assert isinstance(act, AbortEntry) and act.reason == "inside expiry safety"


class TestStaleDeal:
    """|mid - ref_mid| > 25% of ref_mid aborts: ref 2.00 allows 1.50..2.50."""

    DEAL = CONDOR.model_copy(update={"deal_id": "d-0001", "ref_mid": Decimal("2.00")})

    @pytest.mark.parametrize(
        ("quote", "stale"),
        [
            (("2.48", "2.52"), False),  # mid 2.50: +25% exactly
            (("2.50", "2.52"), True),  # mid 2.51: beyond
            (("1.48", "1.52"), False),  # mid 1.50: -25% exactly
            (("1.48", "1.50"), True),  # mid 1.49: beyond
        ],
    )
    def test_boundary(self, quote: tuple[str, str], stale: bool) -> None:
        act = _action(self.DEAL, StructureState(), ENTRY, time(10, 0), _q(*quote))
        if stale:
            assert act == AbortEntry("stale_deal")
        else:
            assert isinstance(act, EntryOrder)

    def test_a_working_entry_aborts_too(self) -> None:
        st = StructureState(status=Status.ENTER_WORKING)
        act = _action(self.DEAL, st, ENTRY, time(10, 30), _q("3.00", "3.20"))
        assert act == AbortEntry("stale_deal")

    def test_a_reference_without_a_deal_is_still_honoured(self) -> None:
        spec = DEBIT_PUT.model_copy(update={"ref_mid": Decimal("1.00")})
        act = _action(spec, StructureState(), ENTRY, time(10, 0), _q("1.80", "1.90"))
        assert act == AbortEntry("stale_deal")

    def test_no_reference_no_staleness(self) -> None:
        act = _action(DEBIT_PUT, StructureState(), ENTRY, time(10, 0), _q("0.10", "0.20"))
        assert act == EntryOrder(side="BUY", qty=2, limit=Decimal("0.15"))


# -- exits -----------------------------------------------------------------


class TestExpirySafety:
    def test_last_hold_session_uses_the_session_calendar(self) -> None:
        assert last_hold_session(FRONT) == LAST_HOLD
        # Thanksgiving: the day before the 11-27 expiry is a holiday
        assert last_hold_session(date(2026, 11, 27)) == date(2026, 11, 25)
        # a Monday expiry: the Friday before
        assert last_hold_session(date(2026, 10, 19)) == date(2026, 10, 16)

    def test_the_session_before_the_first_expiry_forces_the_close(self) -> None:
        # priority 1: beats the touch that also holds (spot 99 <= 100)
        act = _action(DEBIT_PUT, _open(), LAST_HOLD, time(9, 31), _q("3.00", "3.40"), spot="99")
        assert act == CloseOrder(ExitReason.EXPIRY_SAFETY, "SELL", 2, Decimal("3.00"))

    def test_the_session_before_that_holds(self) -> None:
        late = DEBIT_PUT.model_copy(update={"exit_deadline": LAST_HOLD})
        act = _action(late, _open(), date(2026, 10, 14), MIDDAY, _q("3.00", "3.40"))
        assert act == NoAction("hold")

    def test_a_working_exit_turns_into_expiry_safety(self) -> None:
        st = _exiting("touch", cycles=0)
        act = _action(DEBIT_PUT, st, LAST_HOLD, MIDDAY, _q("3.00", "3.40"))
        assert act == CloseOrder(ExitReason.EXPIRY_SAFETY, "SELL", 2, Decimal("3.00"))

    def test_past_the_expiry_still_closes(self) -> None:
        act = _action(DEBIT_PUT, _open(), FRONT, time(9, 31), _q("3.00", "3.40"))
        assert act == CloseOrder(ExitReason.EXPIRY_SAFETY, "SELL", 2, Decimal("3.00"))

    def test_credit_buys_back_at_the_ask_capped(self) -> None:
        act = _action(CONDOR, _open(), LAST_HOLD, time(9, 31), _q("6.80", "7.00"))
        assert act == CloseOrder(ExitReason.EXPIRY_SAFETY, "BUY", 2, Decimal("7.00"))

    def test_holiday_before_expiry(self) -> None:
        spec = _spec(
            "tg",
            "credit_vertical",
            [
                _leg("P", "SELL", "95", date(2026, 11, 27)),
                _leg("P", "BUY", "90", date(2026, 11, 27)),
            ],
            "1.00",
            entry_date=date(2026, 11, 16),
            exit_deadline=date(2026, 11, 24),
        )
        # 11-25 (Wed) is the last session before the 11-27 expiry; naive
        # "expiry minus one day" would wait for 11-26, a holiday
        act = _action(spec, _open(), date(2026, 11, 25), time(9, 31), _q("0.40", "0.50"))
        assert act == CloseOrder(ExitReason.EXPIRY_SAFETY, "BUY", 2, Decimal("0.50"))
        # the day before: the deadline passed, so the time stop (at mid)
        act = _action(spec, _open(), date(2026, 11, 24), time(10, 0), _q("0.40", "0.50"))
        assert act == CloseOrder(ExitReason.TIME_STOP, "BUY", 2, Decimal("0.45"))

    @pytest.mark.parametrize("spec", [CALENDAR, DIAGONAL], ids=["calendar", "diagonal"])
    def test_two_expiry_kinds_close_before_the_front_expiry(self, spec: LegStructure) -> None:
        # max loss = the debit only if the short front never reaches its
        # expiry: the engine closes on the session before the FRONT expiry,
        # whatever the (later) back expiry and whatever is already working
        late = spec.model_copy(update={"exit_deadline": LAST_HOLD})
        hold = _action(late, _open(), date(2026, 10, 14), MIDDAY, _q("1.60", "1.80"))
        assert isinstance(hold, NoAction) and hold.reason == "hold"
        for st in (_open(), _exiting("time_stop", cycles=0)):
            act = _action(late, st, LAST_HOLD, time(9, 31), _q("1.60", "1.80"))
            assert act == CloseOrder(ExitReason.EXPIRY_SAFETY, "SELL", 2, Decimal("1.60"))
        act = _action(spec, _exiting("time_stop"), FRONT, time(10, 0), _q("1.60", "1.80"))
        assert act == CloseOrder(ExitReason.EXPIRY_SAFETY, "SELL", 2, Decimal("1.60"))


class TestWorkingExit:
    @pytest.mark.parametrize(
        ("spec", "reason", "cycles", "quote", "expected"),
        [
            (DEBIT_PUT, "take_profit", 0, ("3.00", "3.40"), ("take_profit", "SELL", "3.20")),
            (DEBIT_PUT, "take_profit", 3, ("3.00", "3.40"), ("take_profit", "SELL", "3.00")),
            (CREDIT_PUT, "breach", 0, ("2.00", "2.40"), ("breach", "BUY", "2.20")),
            (CREDIT_PUT, "breach", 3, ("2.00", "2.40"), ("breach", "BUY", "2.40")),
            (CREDIT_PUT, "stop_loss", 0, ("2.00", "2.40"), ("stop_loss", "BUY", "2.20")),
            (CREDIT_PUT, None, 0, ("2.00", "2.40"), ("time_stop", "BUY", "2.20")),
        ],
    )
    def test_reason_kept_and_price_escalates(
        self,
        spec: LegStructure,
        reason: str | None,
        cycles: int,
        quote: tuple[str, str],
        expected: tuple[str, str, str],
    ) -> None:
        act = _action(spec, _exiting(reason, cycles), ENTRY, MIDDAY, _q(*quote))
        assert act == CloseOrder(ExitReason(expected[0]), expected[1], 2, Decimal(expected[2]))

    def test_forced_after_the_force_time_on_the_deadline(self) -> None:
        act = _action(CREDIT_PUT, _exiting("time_stop"), DEADLINE, time(15, 46), _q("2.00", "2.40"))
        assert act == CloseOrder(ExitReason.TIME_STOP, "BUY", 2, Decimal("2.40"))

    def test_working_exit_beats_new_triggers(self) -> None:
        # the touch holds too, but an exit is already working for take-profit
        act = _action(
            DEBIT_PUT, _exiting("take_profit"), ENTRY, MIDDAY, _q("3.00", "3.40"), spot="99"
        )
        assert isinstance(act, CloseOrder) and act.reason is ExitReason.TAKE_PROFIT


class TestTouch:
    @pytest.mark.parametrize(
        ("spec", "spot", "fires"),
        [
            (LONG_PUT, "95.00", True),  # puts: spot <= long strike
            (LONG_PUT, "95.01", False),
            (LONG_CALL, "105.00", True),  # calls: spot >= long strike
            (LONG_CALL, "104.99", False),
            (DEBIT_PUT, "100.00", True),  # the BUY leg is the long strike
            (DEBIT_PUT, "100.01", False),
            (DEBIT_CALL, "100.00", True),
            (DEBIT_CALL, "99.99", False),
        ],
    )
    def test_direction_by_right(self, spec: LegStructure, spot: str, fires: bool) -> None:
        act = _action(spec, _open(), ENTRY, MIDDAY, _q("1.00", "1.20"), spot=spot)
        if fires:
            assert act == CloseOrder(ExitReason.TOUCH, "SELL", 2, Decimal("1.10"))
        else:
            assert isinstance(act, NoAction) and act.reason == "hold"

    def test_touch_off_or_no_spot_never_fires(self) -> None:
        off = DEBIT_PUT.model_copy(
            update={"exits": DEBIT_PUT.exits.model_copy(update={"touch": False})}
        )
        assert isinstance(
            _action(off, _open(), ENTRY, MIDDAY, _q("1.00", "1.20"), spot="90"), NoAction
        )
        assert isinstance(
            _action(DEBIT_PUT, _open(), ENTRY, MIDDAY, _q("1.00", "1.20"), spot=None), NoAction
        )

    def test_touch_without_quote_stays_pending(self) -> None:
        act = _action(DEBIT_PUT, _open(), ENTRY, MIDDAY, None, spot="99")
        assert act == CloseOrder(ExitReason.TOUCH, "SELL", 2, None)


class TestBreach:
    @pytest.mark.parametrize(
        ("spec", "spot", "fires"),
        [
            (CREDIT_PUT, "95.00", True),  # short put 95: spot at or below
            (CREDIT_PUT, "95.01", False),
            (CREDIT_CALL, "110.00", True),  # short call 110: spot at or above
            (CREDIT_CALL, "109.99", False),
            (CONDOR, "90.00", True),  # either short strike of the condor
            (CONDOR, "110.00", True),
            (CONDOR, "100.00", False),
        ],
    )
    def test_spot_crosses_a_short_strike(self, spec: LegStructure, spot: str, fires: bool) -> None:
        act = _action(spec, _open(), ENTRY, MIDDAY, _q("1.00", "1.20"), spot=spot)
        if fires:
            assert act == CloseOrder(ExitReason.BREACH, "BUY", 2, Decimal("1.10"))
        else:
            assert isinstance(act, NoAction) and act.reason == "hold"

    def test_breach_off_never_fires(self) -> None:
        off = CONDOR.model_copy(update={"exits": CONDOR.exits.model_copy(update={"breach": False})})
        assert isinstance(
            _action(off, _open(), ENTRY, MIDDAY, _q("1.00", "1.20"), spot="80"), NoAction
        )

    def test_touch_and_breach_never_cross_kinds(self) -> None:
        # flags forced past the schema (model_copy skips validation): a
        # credit kind's long wing is protection, never a touch; a debit
        # kind's short strike is its target, never a breach
        condor = CONDOR.model_copy(
            update={"exits": CONDOR.exits.model_copy(update={"touch": True, "breach": False})}
        )
        act = _action(condor, _open(), ENTRY, MIDDAY, _q("1.00", "1.20"), spot="84")
        assert act == NoAction("hold")
        debit = DEBIT_PUT.model_copy(
            update={"exits": DEBIT_PUT.exits.model_copy(update={"touch": False, "breach": True})}
        )
        act = _action(debit, _open(), ENTRY, MIDDAY, _q("1.00", "1.20"), spot="94")
        assert act == NoAction("hold")


def _short_call_key_of(spec: LegStructure) -> str:
    """The Snapshot.short_call_mids key of a spec's first short call, built
    FROM the spec so a fixture change moves the key with it. The wire
    format itself is pinned literally once, in the test below."""
    leg = next(g for g in spec.legs if g.action == "SELL" and g.right == "C")
    return f"{spec.id}|C{leg.strike}|{leg.expiry.isoformat()}"


class TestAssignmentRisk:
    """The ex-dividend exit rule (carry-forward 2026-09-23: it lands with
    the rails' relaxed entry rail or the relaxation waits). DIAGONAL is
    short the front 105 call with NO breach flag - exactly the kind the
    rule protects (a breach-enabled credit kind exits at the same ITM
    boundary before this rule is reached, which the last test pins)."""

    TRIGGER = date(2026, 10, 5)  # Mon; ex-date Tue 2026-10-06
    EX = date(2026, 10, 6)
    KEY = _short_call_key_of(DIAGONAL)

    def test_the_short_call_key_wire_format(self) -> None:
        # one literal pin: the runtime and the engine must agree on this
        # format forever, and every other test derives its keys
        assert TestAssignmentRisk.KEY == "dg|C105|2026-10-16"

    def test_itm_short_call_with_extrinsic_below_the_dividend_closes(self) -> None:
        # spot 106: intrinsic 1.00, mid 2.50 -> extrinsic 1.50 < 2.00
        act = _action(
            DIAGONAL, _open(entry_fill="5.00"), self.TRIGGER, MIDDAY,
            _q("5.90", "6.10"), spot="106",
            dividend=(self.EX, self.TRIGGER, "2.00"),
            short_call_mids={self.KEY: "2.50"},
        )
        assert act.reason is ExitReason.ASSIGNMENT_RISK
        # a debit diagonal closes by SELLING the package; forced: the window
        # is one session, marketable means the bid
        assert act.side == "SELL" and act.qty == 2
        assert act.limit == Decimal("5.90")

    def test_extrinsic_above_the_dividend_holds(self) -> None:
        # extrinsic 1.50 >= dividend 1.00: no exercise edge, keep the position
        act = _action(
            DIAGONAL, _open(entry_fill="5.00"), self.TRIGGER, MIDDAY,
            _q("5.90", "6.10"), spot="106",
            dividend=(self.EX, self.TRIGGER, "1.00"),
            short_call_mids={self.KEY: "2.50"},
        )
        assert act == NoAction("hold")

    def test_itm_short_call_with_no_leg_quote_closes_fail_closed(self) -> None:
        act = _action(
            DIAGONAL, _open(entry_fill="5.00"), self.TRIGGER, MIDDAY,
            _q("5.90", "6.10"), spot="106",
            dividend=(self.EX, self.TRIGGER, "2.00"),
        )
        assert act.reason is ExitReason.ASSIGNMENT_RISK

    def test_not_itm_holds_even_with_a_small_dividend(self) -> None:
        # spot 104 < strike 105: no exercise to capture, no rule
        act = _action(
            DIAGONAL, _open(entry_fill="5.00"), self.TRIGGER, MIDDAY,
            _q("5.40", "5.60"), spot="104",
            dividend=(self.EX, self.TRIGGER, "5.00"),
            short_call_mids={self.KEY: "1.80"},
        )
        assert act == NoAction("hold")

    def test_only_the_last_session_before_the_ex_date_triggers(self) -> None:
        for day in (date(2026, 10, 1), date(2026, 10, 2)):
            act = _action(
                DIAGONAL, _open(entry_fill="5.00"), day, MIDDAY,
                _q("5.90", "6.10"), spot="106",
                dividend=(self.EX, self.TRIGGER, "2.00"),
                short_call_mids={self.KEY: "2.50"},
            )
            assert isinstance(act, NoAction), day

    def test_no_short_calls_or_no_observation_stands_down(self) -> None:
        # DEBIT_PUT has no short call (spot 101: also clear of its touch);
        # DIAGONAL without a dividend observation, without a spot, or with
        # a zero dividend all stand down
        assert isinstance(
            _action(
                DEBIT_PUT, _open(), self.TRIGGER, MIDDAY, _q("1.00", "1.20"),
                spot="101", dividend=(self.EX, self.TRIGGER, "5.00"),
                # no key can matter: the structure has no short call
                short_call_mids={"whatever": "0.10"},
            ),
            NoAction,
        )
        for kwargs in (
            {"spot": "106", "short_call_mids": {self.KEY: "0.10"}},  # no dividend obs
            {"spot": "106", "dividend": (self.EX, self.TRIGGER, "0")},  # zero dividend
            {"dividend": (self.EX, self.TRIGGER, "2.00"),
             "short_call_mids": {self.KEY: "0.10"}},  # no spot
        ):
            assert isinstance(
                _action(
                    DIAGONAL, _open(entry_fill="5.00"), self.TRIGGER, MIDDAY,
                    _q("5.90", "6.10"), **kwargs,
                ),
                NoAction,
            ), kwargs

    def test_expiry_safety_and_a_working_exit_still_preempt(self) -> None:
        # inside expiry safety the reason stays expiry safety
        act = _action(
            DIAGONAL, _open(entry_fill="5.00"), LAST_HOLD, MIDDAY,
            _q("5.90", "6.10"), spot="106",
            dividend=(date(2026, 10, 17), LAST_HOLD, "2.00"),
            short_call_mids={self.KEY: "2.50"},
        )
        assert act.reason is ExitReason.EXPIRY_SAFETY
        # an exit already working keeps its own reason
        act = _action(
            DIAGONAL, _exiting("time_stop"), self.TRIGGER, MIDDAY,
            _q("5.90", "6.10"), spot="106",
            dividend=(self.EX, self.TRIGGER, "2.00"),
            short_call_mids={self.KEY: "2.50"},
        )
        assert act.reason is ExitReason.TIME_STOP

    def test_fires_before_take_profit_when_both_apply(self) -> None:
        spec = DIAGONAL.model_copy(
            update={
                "exits": DIAGONAL.exits.model_copy(
                    update={"take_profit": {"gain_frac": "0.10"}}
                )
            }
        )
        # package mid 6.00 >= entry 5.00 x 1.10: take-profit would also fire
        act = _action(
            spec, _open(entry_fill="5.00"), self.TRIGGER, MIDDAY,
            _q("5.90", "6.10"), spot="106",
            dividend=(self.EX, self.TRIGGER, "2.00"),
            short_call_mids={self.KEY: "0.30"},  # extrinsic -0.70 < 2.00
        )
        assert act.reason is ExitReason.ASSIGNMENT_RISK

    def test_a_breach_enabled_kind_exits_at_breach_first(self) -> None:
        """The breach rule fires at the same ITM boundary on credit kinds,
        so it preempts assignment risk; the assignment rule exists for the
        non-breach kinds (diagonals, calendars) and any breach-off row."""
        act = _action(
            CREDIT_CALL, _open(entry_fill="1.00"), self.TRIGGER, MIDDAY,
            _q("2.40", "2.60"), spot="111",
            dividend=(self.EX, self.TRIGGER, "2.00"),
            short_call_mids={_short_call_key_of(CREDIT_CALL): "2.50"},  # extrinsic < div
        )
        assert act.reason is ExitReason.BREACH


class TestTakeProfit:
    def test_width_fraction(self) -> None:
        spec = DEBIT_PUT.model_copy(
            update={
                "exits": DEBIT_PUT.exits.model_copy(
                    update={"take_profit": _tp("width_frac", "0.80")}
                )
            }
        )  # threshold 0.80 x width 5 = 4.00
        act = _action(spec, _open(), ENTRY, MIDDAY, _q("3.90", "4.10"))  # mid 4.00
        assert act == CloseOrder(ExitReason.TAKE_PROFIT, "SELL", 2, Decimal("4.00"))
        assert isinstance(
            _action(spec, _open(), ENTRY, MIDDAY, _q("3.90", "4.08")), NoAction
        )  # 3.99

    def test_gain_fraction_needs_the_entry_price(self) -> None:
        spec = LONG_CALL.model_copy(
            update={
                "exits": LONG_CALL.exits.model_copy(update={"take_profit": _tp("gain_frac", "0.5")})
            }
        )  # entry 3.00 x 1.5 = 4.50
        act = _action(spec, _open(entry_fill="3.00"), ENTRY, MIDDAY, _q("4.40", "4.60"))
        assert act == CloseOrder(ExitReason.TAKE_PROFIT, "SELL", 2, Decimal("4.50"))
        hold = _action(spec, _open(entry_fill="3.00"), ENTRY, MIDDAY, _q("4.40", "4.58"))  # 4.49
        assert isinstance(hold, NoAction)
        unknown = _action(spec, _open(entry_fill=None), ENTRY, MIDDAY, _q("9.00", "9.20"))
        assert isinstance(unknown, NoAction)

    def test_credit_default_is_half_the_credit_captured(self) -> None:
        # no take_profit on CREDIT_PUT: default 50% of the 1.20 credit, i.e.
        # buy back at a mid of 0.60 or less
        act = _action(CREDIT_PUT, _open(entry_fill="1.20"), ENTRY, MIDDAY, _q("0.55", "0.65"))
        assert act == CloseOrder(ExitReason.TAKE_PROFIT, "BUY", 2, Decimal("0.60"))
        hold = _action(CREDIT_PUT, _open(entry_fill="1.20"), ENTRY, MIDDAY, _q("0.56", "0.66"))
        assert isinstance(hold, NoAction)  # mid 0.61

    def test_explicit_credit_fraction(self) -> None:
        spec = CONDOR.model_copy(
            update={
                "exits": CONDOR.exits.model_copy(update={"take_profit": _tp("credit_frac", "0.75")})
            }
        )  # entry 2.40 x (1 - 0.75) = 0.60
        act = _action(spec, _open(entry_fill="2.40"), ENTRY, MIDDAY, _q("0.55", "0.65"))
        assert act == CloseOrder(ExitReason.TAKE_PROFIT, "BUY", 2, Decimal("0.60"))
        hold = _action(spec, _open(entry_fill="2.40"), ENTRY, MIDDAY, _q("1.10", "1.30"))
        assert isinstance(hold, NoAction)  # mid 1.20: 50% captured, not 75%


def _tp(basis: str, value: str) -> Any:
    from tree_options.trex.plan import TakeProfit

    return TakeProfit(basis=basis, value=Decimal(value))


def _sl(basis: str, value: str) -> Any:
    from tree_options.trex.plan import StopLoss

    return StopLoss(basis=basis, value=Decimal(value))


def _with_stop(spec: LegStructure, basis: str, value: str, confirm: int = 3) -> LegStructure:
    exits = spec.exits.model_copy(
        update={"stop_loss": _sl(basis, value), "stop_confirm_ticks": confirm}
    )
    return spec.model_copy(update={"exits": exits})


class TestStopLoss:
    # debit: entry 3.00, lose half -> stop at a mid of 1.50 or less
    DEBIT = _with_stop(DEBIT_CALL, "debit_frac", "0.5")
    # credit: entry 1.20, 2x -> stop at a mid of 2.40 or more
    CREDIT = _with_stop(
        CREDIT_PUT.model_copy(
            update={"exits": CREDIT_PUT.exits.model_copy(update={"breach": False})}
        ),
        "credit_mult",
        "2",
    )

    def _tick(
        self, spec: LegStructure, st: StructureState, quote: ComboQuote | None
    ) -> StructureDecision:
        d = _decide(spec, st, ENTRY, MIDDAY, quote)
        st.stop_ticks = d.stop_ticks  # the runtime persists the counter
        return d

    def test_fires_only_after_three_consecutive_ticks(self) -> None:
        st = _open(entry_fill="3.00")
        at_stop = _q("1.40", "1.60")  # mid 1.50
        first, second = self._tick(self.DEBIT, st, at_stop), self._tick(self.DEBIT, st, at_stop)
        assert (first.action, first.stop_ticks) == (NoAction("hold"), 1)
        assert (second.action, second.stop_ticks) == (NoAction("hold"), 2)
        third = self._tick(self.DEBIT, st, at_stop)
        assert third.action == CloseOrder(ExitReason.STOP_LOSS, "SELL", 2, Decimal("1.50"))

    def test_a_recovered_tick_resets_the_count(self) -> None:
        st = _open(entry_fill="3.00", stop_ticks=2)
        d = self._tick(self.DEBIT, st, _q("1.42", "1.60"))  # mid 1.51
        assert (d.action, d.stop_ticks) == (NoAction("hold"), 0)

    def test_a_quoteless_tick_neither_counts_nor_resets(self) -> None:
        st = _open(entry_fill="3.00", stop_ticks=2)
        d = self._tick(self.DEBIT, st, None)
        assert (d.action, d.stop_ticks) == (NoAction("hold"), 2)

    def test_credit_multiple(self) -> None:
        st = _open(entry_fill="1.20", stop_ticks=2)
        d = self._tick(self.CREDIT, st, _q("2.30", "2.50"))  # mid 2.40
        assert d.action == CloseOrder(ExitReason.STOP_LOSS, "BUY", 2, Decimal("2.40"))
        st = _open(entry_fill="1.20", stop_ticks=2)
        d = self._tick(self.CREDIT, st, _q("2.30", "2.48"))  # mid 2.39
        assert (d.action, d.stop_ticks) == (NoAction("hold"), 0)

    def test_one_confirm_tick(self) -> None:
        spec = _with_stop(DEBIT_CALL, "debit_frac", "0.5", confirm=1)
        d = _decide(spec, _open(entry_fill="3.00"), ENTRY, MIDDAY, _q("1.40", "1.60"))
        assert isinstance(d.action, CloseOrder) and d.action.reason is ExitReason.STOP_LOSS

    def test_no_entry_price_is_not_evaluable(self) -> None:
        d = _decide(
            self.DEBIT, _open(entry_fill=None, stop_ticks=1), ENTRY, MIDDAY, _q("0.10", "0.20")
        )
        assert (d.action, d.stop_ticks) == (NoAction("hold"), 1)

    def test_the_count_survives_a_restart(self, tmp_path: Path) -> None:
        book = BookState(["dc"])
        book.structures["dc"] = _open(entry_fill="3.00")
        at_stop = _q("1.40", "1.60")
        for _ in range(2):
            self._tick(self.DEBIT, book.structures["dc"], at_stop)
        book.save(tmp_path / "book.json")
        reloaded = BookState.load(tmp_path / "book.json", ["dc"]).structures["dc"]
        assert reloaded.stop_ticks == 2
        d = self._tick(self.DEBIT, reloaded, at_stop)
        assert isinstance(d.action, CloseOrder) and d.action.reason is ExitReason.STOP_LOSS

    def test_other_exits_leave_the_count_alone(self) -> None:
        st = _exiting("touch", filled=2, stop_ticks=2)
        assert _decide(self.DEBIT, st, ENTRY, MIDDAY, _q("1.40", "1.60")).stop_ticks == 2
        assert (
            _decide(self.DEBIT, StructureState(), ENTRY, time(10, 0), _q("1.40", "1.60")).stop_ticks
            == 0
        )


class TestTimeStop:
    def test_at_0945_on_the_deadline(self) -> None:
        hold = _action(CREDIT_PUT, _open(), DEADLINE, time(9, 44), _q("0.90", "1.10"))
        assert isinstance(hold, NoAction)
        act = _action(CREDIT_PUT, _open(), DEADLINE, time(9, 45), _q("0.90", "1.10"))
        assert act == CloseOrder(ExitReason.TIME_STOP, "BUY", 2, Decimal("1.00"))

    def test_past_the_deadline_at_any_time(self) -> None:
        act = _action(CREDIT_PUT, _open(), date(2026, 10, 12), time(9, 31), _q("0.90", "1.10"))
        assert act == CloseOrder(ExitReason.TIME_STOP, "BUY", 2, Decimal("1.00"))


class TestPriority:
    def test_touch_beats_take_profit(self) -> None:
        spec = DEBIT_PUT.model_copy(
            update={
                "exits": DEBIT_PUT.exits.model_copy(
                    update={"take_profit": _tp("width_frac", "0.5")}
                )
            }
        )
        act = _action(spec, _open(), ENTRY, MIDDAY, _q("3.00", "3.20"), spot="99")
        assert isinstance(act, CloseOrder) and act.reason is ExitReason.TOUCH

    def test_breach_beats_take_profit(self) -> None:
        act = _action(
            CREDIT_PUT, _open(entry_fill="1.20"), ENTRY, MIDDAY, _q("0.10", "0.20"), spot="94"
        )
        assert isinstance(act, CloseOrder) and act.reason is ExitReason.BREACH

    def test_take_profit_beats_the_time_stop(self) -> None:
        act = _action(
            CREDIT_PUT, _open(entry_fill="1.20"), DEADLINE, time(10, 0), _q("0.10", "0.20")
        )
        assert isinstance(act, CloseOrder) and act.reason is ExitReason.TAKE_PROFIT

    def test_confirmed_stop_beats_the_time_stop_building_one_does_not(self) -> None:
        spec = TestStopLoss.DEBIT
        confirmed = _decide(
            spec, _open(entry_fill="3.00", stop_ticks=2), DEADLINE, time(10, 0), _q("1.40", "1.60")
        )
        assert isinstance(confirmed.action, CloseOrder)
        assert confirmed.action.reason is ExitReason.STOP_LOSS
        building = _decide(
            spec, _open(entry_fill="3.00"), DEADLINE, time(10, 0), _q("1.40", "1.60")
        )
        assert isinstance(building.action, CloseOrder)
        assert building.action.reason is ExitReason.TIME_STOP and building.stop_ticks == 1


class TestClosePricing:
    def test_sell_never_goes_below_a_cent(self) -> None:
        # mid cents(-0.01) and bid -0.05 are not orderable prices
        act = _action(DEBIT_PUT, _open(), ENTRY, MIDDAY, _q("-0.05", "0.03"), spot="99")
        assert act == CloseOrder(ExitReason.TOUCH, "SELL", 2, Decimal("0.01"))
        act = _action(DEBIT_PUT, _exiting("touch", cycles=3), ENTRY, MIDDAY, _q("-0.05", "0.03"))
        assert act == CloseOrder(ExitReason.TOUCH, "SELL", 2, Decimal("0.01"))

    @pytest.mark.parametrize(
        ("cycles", "quote", "limit"),
        [
            (0, ("4.90", "5.30"), "5.00"),  # mid 5.10 above the 5 width: the width
            (3, ("4.60", "5.30"), "5.00"),  # the ask above the width: the width
            (3, ("4.60", "4.90"), "4.90"),  # the ask within it
            (0, ("0.00", "0.00"), "0.01"),  # never a zero price
        ],
    )
    def test_buy_to_close_is_capped_at_the_width(
        self, cycles: int, quote: tuple[str, str], limit: str
    ) -> None:
        act = _action(CREDIT_PUT, _exiting("breach", cycles), ENTRY, MIDDAY, _q(*quote))
        assert act == CloseOrder(ExitReason.BREACH, "BUY", 2, Decimal(limit))

    def test_condor_cap_is_the_wider_wing(self) -> None:
        # max value is the 10-wide call wing, not the 5-wide put wing
        act = _action(CONDOR, _exiting("breach", 3), ENTRY, MIDDAY, _q("6.80", "7.00"))
        assert act == CloseOrder(ExitReason.BREACH, "BUY", 2, Decimal("7.00"))
        act = _action(CONDOR, _exiting("breach", 3), ENTRY, MIDDAY, _q("9.80", "10.40"))
        assert act == CloseOrder(ExitReason.BREACH, "BUY", 2, Decimal("10.00"))

    def test_long_single_closes_with_a_plain_sell(self) -> None:
        act = _action(LONG_CALL, _exiting("touch", 3), ENTRY, MIDDAY, _q("4.00", "4.20"))
        assert act == CloseOrder(ExitReason.TOUCH, "SELL", 2, Decimal("4.00"))


class TestCloseQuantity:
    def test_only_what_is_still_open(self) -> None:
        st = _exiting("touch", filled=2, exit_filled_qty=1)
        act = _action(DEBIT_PUT, st, ENTRY, MIDDAY, _q("1.00", "1.20"))
        assert act == CloseOrder(ExitReason.TOUCH, "SELL", 1, Decimal("1.10"))

    @pytest.mark.parametrize("exit_filled", [2, 3])
    def test_flat_orders_nothing(self, exit_filled: int) -> None:
        st = _exiting("touch", filled=2, exit_filled_qty=exit_filled)
        act = _action(DEBIT_PUT, st, LAST_HOLD, MIDDAY, _q("1.00", "1.20"))
        assert isinstance(act, NoAction) and act.reason == "flat"

    def test_closed_is_silent(self) -> None:
        act = _action(
            CONDOR, StructureState(status=Status.CLOSED), LAST_HOLD, MIDDAY, _q("1.00", "1.20")
        )
        assert act == NoAction("closed")


# -- drain, then decide ----------------------------------------------------


class TestStepDrainsBeforeDeciding:
    def test_a_filled_exit_is_never_closed_twice(self) -> None:
        # the broker filled the whole exit since the last tick: deciding on
        # the undrained book would BUY-to-close 2 again (a naked position)
        st = _exiting("breach", filled=2)
        d = step(
            CREDIT_PUT,
            st,
            _snap(CREDIT_PUT, _at(ENTRY, MIDDAY), _q("2.00", "2.40")),
            _wo("exit", 2, "2.30"),
        )
        assert d.action == NoAction("flat")
        assert (st.exit_filled_qty, st.exit_fill, st.open_qty) == (2, Decimal("2.30"), 0)

    def test_a_partial_exit_closes_only_the_remainder(self) -> None:
        st = _exiting("breach", filled=2)
        d = step(
            CREDIT_PUT,
            st,
            _snap(CREDIT_PUT, _at(ENTRY, MIDDAY), _q("2.00", "2.40")),
            _wo("exit", 1, "2.30"),
        )
        assert d.action == CloseOrder(ExitReason.BREACH, "BUY", 1, Decimal("2.20"))

    def test_a_filled_entry_is_not_entered_again(self) -> None:
        st = StructureState(status=Status.ENTER_WORKING)
        d = step(
            CONDOR,
            st,
            _snap(CONDOR, _at(ENTRY, time(10, 0)), _q("2.20", "2.60")),
            _wo("entry", 2, "2.35"),
        )
        assert d.action == NoAction("entry filled")
        assert (st.filled_qty, st.entry_fill) == (2, Decimal("2.35"))

    def test_no_working_order_just_decides(self) -> None:
        d = step(
            CONDOR, StructureState(), _snap(CONDOR, _at(ENTRY, time(10, 0)), _q("2.20", "2.60"))
        )
        assert d.action == EntryOrder(side="SELL", qty=2, limit=Decimal("2.40"))


def _wo(role: str, filled: int, avg: str, order_id: str = "701") -> WorkingOrder:
    """A broker report: CUMULATIVE fills of order ``order_id`` and their
    average (debit orientation; "0" = no average reported yet)."""
    return WorkingOrder(role=role, filled=filled, avg_fill_price=Decimal(avg), order_id=order_id)


class TestDrain:
    def test_checkpoint_makes_it_idempotent(self) -> None:
        st = _exiting("touch", filled=2)
        report = _wo("exit", 1, "0.30")
        assert drain(st, report) == 1
        assert drain(st, report) == 0  # the same broker state again: nothing new
        assert (st.exit_filled_qty, st.exit_fill) == (1, Decimal("0.30"))
        assert (st.exit_order, st.exit_order_seen, st.exit_order_notional) == (
            "701",
            1,
            Decimal("0.30"),
        )

    def test_fills_blend_by_notional(self) -> None:
        # 1 @ 0.30 recorded; the order now says 2 @ avg 0.35, i.e. the
        # second filled at 0.70 - 0.30 = 0.40; the book's average is 0.35
        st = _exiting(
            "touch",
            filled=2,
            exit_filled_qty=1,
            exit_fill=Decimal("0.30"),
            exit_order="701",
            exit_order_seen=1,
            exit_order_notional=Decimal("0.30"),
        )
        assert drain(st, _wo("exit", 2, "0.35")) == 1
        assert (st.exit_filled_qty, st.exit_fill) == (2, Decimal("0.35"))

    def test_a_replacement_order_blends_with_the_book(self) -> None:
        # 1 exited @ 0.30 on an earlier order; a NEW order fills 1 @ 0.50:
        # the book's exit average is (0.30 + 0.50) / 2
        st = _exiting("touch", filled=2, exit_filled_qty=1, exit_fill=Decimal("0.30"))
        assert drain(st, _wo("exit", 1, "0.50")) == 1
        assert (st.exit_filled_qty, st.exit_fill) == (2, Decimal("0.40"))

    def test_a_new_order_id_starts_its_own_checkpoint(self) -> None:
        # order 700 left its checkpoint (1 seen, 0.30) behind; order 701's
        # first fill must not be swallowed as "already seen"
        st = _exiting(
            "touch",
            filled=2,
            exit_filled_qty=1,
            exit_fill=Decimal("0.30"),
            exit_order="700",
            exit_order_seen=1,
            exit_order_notional=Decimal("0.30"),
        )
        assert drain(st, _wo("exit", 1, "0.50", order_id="701")) == 1
        assert (st.exit_filled_qty, st.exit_fill, st.exit_order) == (2, Decimal("0.40"), "701")

    def test_entry_role_fills_the_entry_side(self) -> None:
        st = StructureState(status=Status.ENTER_WORKING)
        assert drain(st, _wo("entry", 1, "2.35")) == 1
        assert (st.filled_qty, st.entry_fill, st.exit_filled_qty) == (1, Decimal("2.35"), 0)
        assert (st.entry_order_seen, st.entry_order_notional) == (1, Decimal("2.35"))

    def test_a_stale_report_changes_nothing(self) -> None:
        st = StructureState(status=Status.ENTER_WORKING)
        drain(st, _wo("entry", 2, "2.35"))
        before = st.to_dict()
        assert drain(st, _wo("entry", 1, "2.30")) == 0  # fewer fills than recorded
        assert st.to_dict() == before

    def test_an_average_without_fills_records_nothing(self) -> None:
        st = StructureState(status=Status.ENTER_WORKING)
        assert drain(st, _wo("entry", 0, "2.30")) == 0
        assert (st.filled_qty, st.entry_fill, st.entry_order_notional) == (0, None, None)


class TestDrainPricesLateFills:
    """Codex P1 on 75a015a: IBKR can report a fill before its average price.
    The quantity is recorded at once (it sizes closes); the price is
    reconciled from the order's CUMULATIVE notional (avg x filled) whenever
    it is present, even with no new quantity, and the book's average only
    ever spans packages whose price is known."""

    def test_a_price_that_arrives_after_its_fill_is_recorded(self) -> None:
        st = StructureState(status=Status.ENTER_WORKING)
        assert drain(st, _wo("entry", 1, "0")) == 1
        assert (st.filled_qty, st.entry_fill) == (1, None)
        assert drain(st, _wo("entry", 1, "2.00")) == 0  # no new package, a price
        assert (st.filled_qty, st.entry_fill) == (1, Decimal("2.00"))
        # and so the stop is evaluable once open: entry 2.00, debit_frac 0.5
        # stops at a mid of 1.00 or less; the first confirming tick counts
        st.status = Status.OPEN
        d = _decide(TestStopLoss.DEBIT, st, ENTRY, MIDDAY, _q("0.90", "1.10"))
        assert d.stop_ticks == 1

    def test_a_late_price_is_not_charged_to_the_new_fills_alone(self) -> None:
        # 1 filled with no price yet, then the order says 2 @ avg 2.00: the
        # book's entry is 2.00 (4.00 / 2), never 4.00 (4.00 / 1 new fill)
        st = StructureState(status=Status.ENTER_WORKING)
        drain(st, _wo("entry", 1, "0"))
        assert drain(st, _wo("entry", 2, "2.00")) == 1
        assert (st.filled_qty, st.entry_fill) == (2, Decimal("2.00"))

    def test_an_unpriced_order_never_skews_another_orders_price(self) -> None:
        # order 700 fills 1 and is replaced before any price is reported;
        # order 701 fills 1 @ 0.50: the average spans the priced package only
        st = _exiting("touch", filled=2)
        drain(st, _wo("exit", 1, "0", order_id="700"))
        assert drain(st, _wo("exit", 1, "0.50", order_id="701")) == 1
        assert (st.exit_filled_qty, st.exit_fill) == (2, Decimal("0.50"))

    def test_a_late_price_reblends_across_orders(self) -> None:
        # order 700: 1 @ 0.30; order 701: 1 filled, its price only later
        # (0.50): the average goes 0.30 (priced part only) -> 0.40
        st = _exiting("touch", filled=2)
        drain(st, _wo("exit", 1, "0.30", order_id="700"))
        assert drain(st, _wo("exit", 1, "0", order_id="701")) == 1
        assert (st.exit_filled_qty, st.exit_fill) == (2, Decimal("0.30"))
        assert drain(st, _wo("exit", 1, "0.50", order_id="701")) == 0
        assert (st.exit_filled_qty, st.exit_fill) == (2, Decimal("0.40"))

    @pytest.mark.parametrize(
        "updates",
        [
            [("1", "0")],
            [("1", "0"), ("1", "2.00")],
            [("1", "0"), ("2", "2.00")],
            [("1", "2.00"), ("2", "2.10")],
        ],
    )
    def test_the_same_update_twice_records_nothing(self, updates: list[tuple[str, str]]) -> None:
        st = StructureState(status=Status.ENTER_WORKING)
        for filled, avg in updates:
            drain(st, _wo("entry", int(filled), avg))
        before = st.to_dict()
        filled, avg = updates[-1]
        assert drain(st, _wo("entry", int(filled), avg)) == 0
        assert st.to_dict() == before

    def test_unpriced_counts_persist_only_while_used(self, tmp_path: Path) -> None:
        book = BookState(["dc"])
        st = book.structures["dc"] = StructureState(status=Status.ENTER_WORKING)
        assert "entry_unpriced_qty" not in st.to_dict()  # a legacy book's shape
        drain(st, _wo("entry", 2, "0"))
        raw = st.to_dict()
        assert (raw["entry_unpriced_qty"], raw["entry_order_unpriced"]) == (2, 2)
        book.save(tmp_path / "book.json")
        # a restart between the fill and its price: the price still lands
        reloaded = BookState.load(tmp_path / "book.json", ["dc"]).structures["dc"]
        assert drain(reloaded, _wo("entry", 2, "1.80")) == 0
        assert (reloaded.filled_qty, reloaded.entry_fill) == (2, Decimal("1.80"))
        raw = reloaded.to_dict()
        assert "entry_unpriced_qty" not in raw and "entry_order_unpriced" not in raw


# -- adoption --------------------------------------------------------------


class TestAdoptedRole:
    """A working order proven ours by its trex tag: which lane owns it, by
    side. Credit kinds OPEN by selling, so their SELL is the entry (the
    legacy book's SELLs are always exits)."""

    @pytest.mark.parametrize(
        ("spec", "status", "side", "role"),
        [
            (CREDIT_PUT, Status.PLANNED, "SELL", "entry"),
            (CREDIT_PUT, Status.ENTER_WORKING, "SELL", "entry"),
            (CREDIT_PUT, Status.OPEN, "BUY", "exit"),
            (CREDIT_PUT, Status.EXIT_WORKING, "BUY", "exit"),
            (CONDOR, Status.ENTER_WORKING, "SELL", "entry"),
            (CONDOR, Status.EXIT_WORKING, "BUY", "exit"),
            (DEBIT_PUT, Status.ENTER_WORKING, "BUY", "entry"),
            (DEBIT_PUT, Status.EXIT_WORKING, "SELL", "exit"),
            (LONG_CALL, Status.OPEN, "SELL", "exit"),
            # contradictions: never adopted (unknown exposure)
            (CREDIT_PUT, Status.ENTER_WORKING, "BUY", None),
            (CREDIT_PUT, Status.EXIT_WORKING, "SELL", None),
            (DEBIT_PUT, Status.OPEN, "BUY", None),
            (DEBIT_PUT, Status.CLOSED, "SELL", None),
            (DEBIT_PUT, Status.PLANNED, "SELL", None),
        ],
    )
    def test_role(self, spec: LegStructure, status: Status, side: str, role: str | None) -> None:
        assert adopted_role(spec, StructureState(status=status), side) == role
