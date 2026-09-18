"""trex engine tests: the exit discipline as pure, restart-safe logic."""

from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from tree_options.trex.clock import EntryWindow
from tree_options.trex.engine import (
    AbortEntry,
    ComboQuote,
    EngineConfig,
    ExitOrder,
    ExitReason,
    NoAction,
    PlaceEntry,
    Snapshot,
    configure,
    decide,
)
from tree_options.trex.plan import PutSpread
from tree_options.trex.state import Status, StructureState

ET = ZoneInfo("America/New_York")
ENTRY_DAY = date(2026, 9, 18)
DEADLINE = date(2026, 10, 9)
EXPIRY = date(2026, 10, 16)
DEFAULT_QUOTE = ComboQuote(Decimal("0.40"), Decimal("0.48"))


@pytest.fixture(autouse=True)
def _engine() -> Any:
    configure(
        EngineConfig(
            entry_window=EntryWindow(time(9, 45), time(12, 0)),
            entry_max_cycles=4,
            exit_max_mid_cycles=3,
        )
    )
    return None


def _spread(**overrides: Any) -> PutSpread:
    base: dict[str, Any] = {
        "id": "nvda-oct",
        "underlying": "NVDA",
        "entry_date": ENTRY_DAY,
        "expiry": EXPIRY,
        "long_strike": "185",
        "short_strike": "150",
        "quantity": 5,
        "limit_cap": "0.50",
        "exit_deadline": DEADLINE,
    }
    base.update(overrides)
    return PutSpread(**base)


def _snap(
    ts: datetime,
    spot: str = "200.00",
    quote: ComboQuote | None = DEFAULT_QUOTE,
) -> Snapshot:
    return Snapshot(
        ts=ts,
        spots={"NVDA": Decimal(spot)},
        quotes={"nvda-oct": quote},
    )


def _at(day: date, hhmm: time) -> datetime:
    return datetime(day.year, day.month, day.day, hhmm.hour, hhmm.minute, tzinfo=ET)


class TestEntryDecisions:
    def test_before_entry_date_waits(self) -> None:
        act = decide(_spread(), StructureState(), _snap(_at(date(2026, 9, 17), time(10, 0))))
        assert isinstance(act, NoAction) and act.reason == "before entry date"

    def test_before_window_waits(self) -> None:
        act = decide(_spread(), StructureState(), _snap(_at(ENTRY_DAY, time(9, 30))))
        assert isinstance(act, NoAction) and act.reason == "before entry window"

    def test_in_window_places_at_mid_under_cap(self) -> None:
        act = decide(_spread(), StructureState(), _snap(_at(ENTRY_DAY, time(10, 0))))
        assert isinstance(act, PlaceEntry) and act.limit == Decimal("0.44")

    def test_mid_above_cap_pays_only_cap(self) -> None:
        quote = ComboQuote(Decimal("0.55"), Decimal("0.60"))
        act = decide(
            _spread(), StructureState(), _snap(_at(ENTRY_DAY, time(10, 0)), quote=quote)
        )
        assert isinstance(act, PlaceEntry) and act.limit == Decimal("0.50")

    def test_escalated_cycle_crosses_to_ask_capped(self) -> None:
        st = StructureState(entry_cycles=4)
        act = decide(_spread(), st, _snap(_at(ENTRY_DAY, time(10, 0))))
        assert isinstance(act, PlaceEntry) and act.limit == Decimal("0.48")

    def test_window_closed_aborts(self) -> None:
        act = decide(_spread(), StructureState(), _snap(_at(ENTRY_DAY, time(12, 30))))
        assert isinstance(act, AbortEntry) and act.reason == "entry window closed"

    def test_day_after_entry_aborts_forever(self) -> None:
        act = decide(
            _spread(),
            StructureState(),
            _snap(_at(date(2026, 9, 21), time(10, 0))),
        )
        assert isinstance(act, AbortEntry) and act.reason == "entry date passed"

    def test_no_quote_waits(self) -> None:
        act = decide(
            _spread(),
            StructureState(),
            _snap(_at(ENTRY_DAY, time(10, 0)), quote=None),
        )
        assert isinstance(act, NoAction) and act.reason == "no quote"

    def test_working_entry_is_left_to_the_runner(self) -> None:
        st = StructureState(status=Status.ENTER_WORKING)
        act = decide(_spread(), st, _snap(_at(ENTRY_DAY, time(10, 0))))
        assert isinstance(act, NoAction) and act.reason == "entry working"


class TestHoldAndTouch:
    def test_open_above_strike_holds(self) -> None:
        st = StructureState(status=Status.OPEN)
        act = decide(_spread(), st, _snap(_at(ENTRY_DAY, time(13, 0))))
        assert isinstance(act, NoAction) and act.reason == "hold"

    def test_touch_exits_at_mid(self) -> None:
        st = StructureState(status=Status.OPEN)
        act = decide(_spread(), st, _snap(_at(ENTRY_DAY, time(13, 0)), spot="184.99"))
        assert isinstance(act, ExitOrder)
        assert act.reason is ExitReason.TOUCH and act.limit == Decimal("0.44")

    def test_exact_strike_is_a_touch(self) -> None:
        st = StructureState(status=Status.OPEN)
        act = decide(_spread(), st, _snap(_at(ENTRY_DAY, time(13, 0)), spot="185"))
        assert isinstance(act, ExitOrder) and act.reason is ExitReason.TOUCH

    def test_touch_without_quote_stays_pending(self) -> None:
        st = StructureState(status=Status.OPEN)
        act = decide(
            _spread(), st, _snap(_at(ENTRY_DAY, time(13, 0)), spot="180", quote=None)
        )
        assert isinstance(act, ExitOrder) and act.reason is ExitReason.TOUCH and act.limit is None

    def test_take_profit_fraction(self) -> None:
        spread = _spread(take_profit_frac="0.45")  # 0.45 * 35 = 15.75
        st = StructureState(status=Status.OPEN)
        quote = ComboQuote(Decimal("15.50"), Decimal("16.10"))  # mid 15.80
        act = decide(spread, st, _snap(_at(ENTRY_DAY, time(13, 0)), quote=quote))
        assert isinstance(act, ExitOrder) and act.reason is ExitReason.TAKE_PROFIT


class TestTimeStops:
    def test_deadline_before_time_stop_holds(self) -> None:
        st = StructureState(status=Status.OPEN)
        act = decide(_spread(), st, _snap(_at(DEADLINE, time(9, 30))))
        assert isinstance(act, NoAction)

    def test_deadline_at_time_stop_flattens(self) -> None:
        st = StructureState(status=Status.OPEN)
        act = decide(_spread(), st, _snap(_at(DEADLINE, time(9, 45))))
        assert isinstance(act, ExitOrder) and act.reason is ExitReason.TIME_STOP

    def test_expiry_backstop_forces_bid_even_mid_cycles(self) -> None:
        st = StructureState(status=Status.OPEN)
        act = decide(
            _spread(),
            st,
            _snap(_at(EXPIRY, time(9, 45)), quote=ComboQuote(Decimal("2"), Decimal("4"))),
        )
        assert isinstance(act, ExitOrder)
        assert act.reason is ExitReason.EXPIRY_SAFETY and act.limit == Decimal("2.00")


class TestExitPricing:
    def test_exit_working_prices_mid_first(self) -> None:
        st = StructureState(
            status=Status.EXIT_WORKING, exit_reason="touch", exit_cycles=0
        )
        act = decide(_spread(), st, _snap(_at(ENTRY_DAY, time(13, 0))))
        assert isinstance(act, ExitOrder) and act.limit == Decimal("0.44")

    def test_exit_escalates_to_bid(self) -> None:
        st = StructureState(
            status=Status.EXIT_WORKING, exit_reason="touch", exit_cycles=3
        )
        act = decide(_spread(), st, _snap(_at(ENTRY_DAY, time(13, 0))))
        assert isinstance(act, ExitOrder) and act.limit == Decimal("0.40")

    def test_force_time_after_deadline_goes_marketable(self) -> None:
        st = StructureState(
            status=Status.EXIT_WORKING, exit_reason="time_stop", exit_cycles=0
        )
        act = decide(_spread(), st, _snap(_at(DEADLINE, time(15, 46))))
        assert isinstance(act, ExitOrder) and act.limit == Decimal("0.40")

    def test_closed_is_terminal_silence(self) -> None:
        st = StructureState(status=Status.CLOSED)
        act = decide(_spread(), st, _snap(_at(ENTRY_DAY, time(13, 0))))
        assert isinstance(act, NoAction) and act.reason == "closed"
