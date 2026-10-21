"""Wave 2 lane C: the pure exit rules of a desk-owned long equity position.

Oracles come from the frozen contract fixture (``equity_effect``: 2 SPY at
671.50, stop 665.00, target 680.00, deadline session 2026-10-23, 3 confirm
ticks) and from the session calendar as the shared ``SessionClock`` reads
it (13:00 ET close on the 2026-11-27 early close); nothing is recomputed
with the module's own helpers.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

import pytest

from tests.unit.trex_fakes import SessionClock, equity_effect
from tree_options.trex.clock import ET
from tree_options.trex.contracts import DeskEvent
from tree_options.trex.equity_exits import (
    EXIT_EVENTS,
    EquityAbort,
    EquityClose,
    EquityExitReason,
    EquityHold,
    EquityQuote,
    deadline_session,
    decide_equity,
    forced_exit_at,
)
from tree_options.trex.state import Status, StructureState

POLICY = equity_effect().exit_policy
STOP = POLICY.stop_price_cents  # 66500
_TARGET = POLICY.target_price_cents  # 68000
assert _TARGET is not None
TARGET: int = _TARGET
ENTRY_DAY = date(2026, 10, 22)
DEADLINE = POLICY.exit_deadline_session  # 2026-10-23 (a Friday session)


def _at(day: date, hh: int, mm: int, ss: int = 0) -> datetime:
    return datetime.combine(day, time(hh, mm, ss), tzinfo=ET)


def _open(qty: int = 2, **over: object) -> StructureState:
    fields: dict[str, object] = {"status": Status.OPEN, "filled_qty": qty, **over}
    return StructureState(**fields)  # type: ignore[arg-type]


def _q(bid: int | None, at: datetime | None, *, realtime: bool = True) -> EquityQuote:
    return EquityQuote(
        bid_cents=bid,
        ask_cents=None if bid is None else bid + 4,
        realtime=realtime,
        at=None if at is None else at.astimezone(UTC),
    )


# ------------------------------------------------------------ session facts


def test_forced_time_is_1545_on_a_regular_session() -> None:
    assert forced_exit_at(DEADLINE, "15:45") == _at(DEADLINE, 15, 45)


def test_forced_time_on_an_early_close_is_fifteen_minutes_before_it() -> None:
    clock = SessionClock(datetime(2026, 11, 27, 14, 0, tzinfo=UTC))
    day = date(2026, 11, 27)
    assert clock.is_early_close(day)
    expected = clock.close_of(day) - timedelta(minutes=15)  # 13:00 -> 12:45 ET
    assert forced_exit_at(day, "15:45") == expected
    assert expected.astimezone(ET).time() == time(12, 45)


def test_a_deadline_that_is_not_a_session_resolves_to_the_session_before() -> None:
    saturday = date(2026, 10, 24)
    policy = POLICY.model_copy(update={"exit_deadline_session": saturday})
    assert deadline_session(policy) == date(2026, 10, 23)
    assert deadline_session(POLICY) == DEADLINE


# ------------------------------------------------------------ stop confirmation


def test_stop_confirms_over_consecutive_new_realtime_ticks() -> None:
    st = _open()
    now = _at(ENTRY_DAY, 10, 0)
    for i in range(1, POLICY.stop_confirm_ticks):
        tick = now + timedelta(seconds=i)
        d = decide_equity(POLICY, st, _q(STOP, tick), tick)
        assert isinstance(d.action, EquityHold), f"tick {i} of 3 only counts"
        assert d.stop_ticks == i
        st.stop_ticks, st.stop_tick_at = d.stop_ticks, d.stop_tick_at
    tick = now + timedelta(seconds=POLICY.stop_confirm_ticks)
    d = decide_equity(POLICY, st, _q(STOP - 25, tick), tick)
    assert d.action == EquityClose(EquityExitReason.STOP, 2, STOP - 25)
    assert d.stop_ticks == POLICY.stop_confirm_ticks


def test_a_tick_above_the_stop_resets_the_count() -> None:
    st = _open(stop_ticks=2, stop_tick_at=_at(ENTRY_DAY, 10, 0))
    tick = _at(ENTRY_DAY, 10, 0, 5)
    d = decide_equity(POLICY, st, _q(STOP + 1, tick), tick)
    assert d.stop_ticks == 0 and isinstance(d.action, EquityHold)
    assert d.stop_tick_at == tick.astimezone(UTC)


def test_the_same_tick_seen_twice_counts_once() -> None:
    seen = _at(ENTRY_DAY, 10, 0)
    st = _open(stop_ticks=2, stop_tick_at=seen)
    later = _at(ENTRY_DAY, 10, 0, 20)  # the desk ticks again, the quote did not
    d = decide_equity(POLICY, st, _q(STOP, seen), later)
    assert d.stop_ticks == 2 and isinstance(d.action, EquityHold)


@pytest.mark.parametrize("bid", [STOP, STOP + 500])
def test_a_delayed_tick_neither_counts_nor_resets(bid: int) -> None:
    st = _open(stop_ticks=2, stop_tick_at=_at(ENTRY_DAY, 10, 0))
    tick = _at(ENTRY_DAY, 10, 0, 5)
    d = decide_equity(POLICY, st, _q(bid, tick, realtime=False), tick)
    assert d.stop_ticks == 2
    assert d.stop_tick_at == _at(ENTRY_DAY, 10, 0)
    assert isinstance(d.action, EquityHold)


def test_no_quote_at_all_holds_and_keeps_the_count() -> None:
    st = _open(stop_ticks=1, stop_tick_at=_at(ENTRY_DAY, 10, 0))
    now = _at(ENTRY_DAY, 10, 1)
    d = decide_equity(POLICY, st, None, now)
    assert (d.stop_ticks, d.stop_tick_at) == (1, _at(ENTRY_DAY, 10, 0))
    assert isinstance(d.action, EquityHold)


# ------------------------------------------------------------ target


def test_target_fires_on_a_realtime_bid_at_the_target() -> None:
    tick = _at(ENTRY_DAY, 11, 0)
    d = decide_equity(POLICY, _open(), _q(TARGET, tick), tick)
    assert d.action == EquityClose(EquityExitReason.TARGET, 2, TARGET)


def test_target_needs_a_realtime_quote() -> None:
    tick = _at(ENTRY_DAY, 11, 0)
    d = decide_equity(POLICY, _open(), _q(TARGET + 100, tick, realtime=False), tick)
    assert isinstance(d.action, EquityHold)


def test_no_target_set_never_takes_profit() -> None:
    policy = POLICY.model_copy(update={"target_price_cents": None})
    tick = _at(ENTRY_DAY, 11, 0)
    d = decide_equity(policy, _open(), _q(TARGET + 10_000, tick), tick)
    assert isinstance(d.action, EquityHold)


# ------------------------------------------------------------ time rules


def test_forced_exit_at_1545_on_the_deadline_session_not_before() -> None:
    before = _at(DEADLINE, 15, 44, 59)
    assert isinstance(decide_equity(POLICY, _open(), _q(67000, before), before).action, EquityHold)
    at = _at(DEADLINE, 15, 45)
    d = decide_equity(POLICY, _open(), _q(67000, at), at)
    assert d.action == EquityClose(EquityExitReason.FORCED, 2, 67000)


def test_forced_exit_on_an_early_close_deadline_at_1245() -> None:
    day = date(2026, 11, 27)
    policy = POLICY.model_copy(update={"exit_deadline_session": day})
    early = _at(day, 12, 44, 59)
    assert isinstance(decide_equity(policy, _open(), _q(67000, early), early).action, EquityHold)
    at = _at(day, 12, 45)
    d = decide_equity(policy, _open(), _q(67000, at), at)
    assert d.action == EquityClose(EquityExitReason.FORCED, 2, 67000)


def test_a_passed_deadline_exits_at_once_in_the_next_session() -> None:
    monday = date(2026, 10, 26)
    now = _at(monday, 9, 30, 1)
    d = decide_equity(POLICY, _open(), _q(67000, now), now)
    assert d.action == EquityClose(EquityExitReason.DEADLINE, 2, 67000)


def test_a_forced_exit_without_a_realtime_bid_is_pending_unpriced() -> None:
    at = _at(DEADLINE, 15, 50)
    d = decide_equity(POLICY, _open(), _q(67000, at, realtime=False), at)
    assert d.action == EquityClose(EquityExitReason.FORCED, 2, None)


# ------------------------------------------------------------ session gate


@pytest.mark.parametrize("hhmm", [(9, 29), (16, 0), (16, 10)])
def test_nothing_is_decided_or_counted_outside_the_regular_session(hhmm: tuple[int, int]) -> None:
    st = _open(stop_ticks=2, stop_tick_at=_at(ENTRY_DAY, 9, 0))
    now = _at(DEADLINE, *hhmm)
    d = decide_equity(POLICY, st, _q(STOP - 100, now), now)
    assert isinstance(d.action, EquityHold)
    assert (d.stop_ticks, d.stop_tick_at) == (2, _at(ENTRY_DAY, 9, 0))


def test_a_weekend_is_not_a_session() -> None:
    now = _at(date(2026, 10, 24), 12, 0)
    d = decide_equity(POLICY, _open(), _q(STOP - 100, now), now)
    assert isinstance(d.action, EquityHold)


# ------------------------------------------------------------ lanes


def test_working_exit_keeps_its_reason_and_reprices_to_the_bid() -> None:
    st = _open(status=Status.EXIT_WORKING, exit_reason="target")
    tick = _at(ENTRY_DAY, 11, 0)
    d = decide_equity(POLICY, st, _q(67900, tick), tick)
    assert d.action == EquityClose(EquityExitReason.TARGET, 2, 67900)


def test_working_exit_with_an_unknown_reason_keeps_exiting() -> None:
    st = _open(status=Status.EXIT_WORKING, exit_reason="from-a-newer-build")
    tick = _at(ENTRY_DAY, 11, 0)
    d = decide_equity(POLICY, st, _q(67000, tick), tick)
    assert isinstance(d.action, EquityClose) and d.action.qty == 2


def test_the_close_sizes_to_the_open_quantity_only() -> None:
    st = _open(qty=100, exit_filled_qty=63)
    tick = _at(ENTRY_DAY, 11, 0)
    d = decide_equity(POLICY, st, _q(TARGET, tick), tick)
    assert d.action == EquityClose(EquityExitReason.TARGET, 37, TARGET)


def test_a_flat_position_holds() -> None:
    st = _open(qty=2, exit_filled_qty=2)
    tick = _at(DEADLINE, 15, 50)
    assert isinstance(decide_equity(POLICY, st, _q(67000, tick), tick).action, EquityHold)


def test_an_exit_rule_firing_during_the_entry_aborts_the_remainder() -> None:
    st = StructureState(status=Status.ENTER_WORKING, filled_qty=37)
    tick = _at(ENTRY_DAY, 11, 0)
    d = decide_equity(POLICY, st, _q(TARGET, tick), tick)
    assert d.action == EquityAbort("target")


def test_a_quiet_entry_holds() -> None:
    st = StructureState(status=Status.ENTER_WORKING)
    tick = _at(ENTRY_DAY, 11, 0)
    assert isinstance(decide_equity(POLICY, st, _q(67100, tick), tick).action, EquityHold)


@pytest.mark.parametrize("status", [Status.PLANNED, Status.CLOSED])
def test_planned_and_closed_never_act(status: Status) -> None:
    st = StructureState(status=status)
    tick = _at(DEADLINE, 15, 50)
    assert isinstance(decide_equity(POLICY, st, _q(STOP - 100, tick), tick).action, EquityHold)


def test_every_rule_has_its_frozen_event_name() -> None:
    assert EXIT_EVENTS == {
        EquityExitReason.STOP: DeskEvent.EQUITY_EXIT_STOP,
        EquityExitReason.TARGET: DeskEvent.EQUITY_EXIT_TARGET,
        EquityExitReason.DEADLINE: DeskEvent.EQUITY_EXIT_DEADLINE,
        EquityExitReason.FORCED: DeskEvent.EQUITY_EXIT_FORCED,
    }
    assert EquityExitReason.FLATTEN not in EXIT_EVENTS
