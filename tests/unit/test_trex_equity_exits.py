"""Wave 2 lane C: the pure exit rules of a desk-owned long equity position.

Oracles come from the frozen contract fixture (``equity_effect``: 2 SPY at
671.50, stop 665.00, target 680.00, deadline session 2026-10-23, 3 confirm
ticks) and from the session calendar as the shared ``SessionClock`` reads
it (13:00 ET close on the 2026-11-27 early close); nothing is recomputed
with the module's own helpers.

Wave 2b lane c2 (exit quotes): the documented fallback policy is the oracle
for time-driven exits priced without a real-time bid: 2 cents (two 1-cent
ticks) below the delayed/frozen bid, 2 more per full minute the exit has
been working, never more than 10 cents nor 50 bps of the bid; an exit that
stays unpriced for 300 s raises ``exit_quote_blind`` once per structure per
session.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import pytest

from tests.unit.trex_fakes import SessionClock, equity_effect
from tree_options.trex import equity_exits as ee
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


def _q(
    bid: int | None, at: datetime | None, *, realtime: bool = True, kind: str | None = None
) -> EquityQuote:
    """A quote; ``kind`` names the market-data type (the runtime may omit it)."""
    typed: dict[str, Any] = {} if kind is None else {"market_data_type": kind}
    return EquityQuote(
        bid_cents=bid,
        ask_cents=None if bid is None else bid + 4,
        realtime=realtime if kind is None else kind == "realtime",
        at=None if at is None else at.astimezone(UTC),
        **typed,
    )


def _rt(reason: EquityExitReason, qty: int, bid: int) -> EquityClose:
    """A close priced AT a real-time bid: no concession, typed realtime."""
    return EquityClose(reason, qty, bid, quote_type="realtime", bid_cents=bid)


def _stale(reason: EquityExitReason, qty: int, bid: int, concession: int, kind: str) -> EquityClose:
    """A close priced from a non-real-time bid, ``concession`` cents below it."""
    return EquityClose(reason, qty, bid - concession, quote_type=kind, bid_cents=bid)


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
    assert d.action == _rt(EquityExitReason.STOP, 2, STOP - 25)
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
    assert d.action == _rt(EquityExitReason.TARGET, 2, TARGET)


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
    assert d.action == _rt(EquityExitReason.FORCED, 2, 67000), "real time: AT the bid"


def test_forced_exit_on_an_early_close_deadline_at_1245() -> None:
    day = date(2026, 11, 27)
    policy = POLICY.model_copy(update={"exit_deadline_session": day})
    early = _at(day, 12, 44, 59)
    assert isinstance(decide_equity(policy, _open(), _q(67000, early), early).action, EquityHold)
    at = _at(day, 12, 45)
    d = decide_equity(policy, _open(), _q(67000, at), at)
    assert d.action == _rt(EquityExitReason.FORCED, 2, 67000)


def test_a_passed_deadline_exits_at_once_in_the_next_session() -> None:
    monday = date(2026, 10, 26)
    now = _at(monday, 9, 30, 1)
    d = decide_equity(POLICY, _open(), _q(67000, now), now)
    assert d.action == _rt(EquityExitReason.DEADLINE, 2, 67000)


# ------------------------------------------------------------ exits without a real-time bid
# (review P2: a forced or deadline exit used to stay unpriced forever)


@pytest.mark.parametrize("kind", ["delayed", "frozen"])
def test_a_forced_exit_prices_from_a_non_realtime_bid_two_ticks_below(kind: str) -> None:
    at = _at(DEADLINE, 15, 45)
    d = decide_equity(POLICY, _open(), _q(67000, at, kind=kind), at)
    assert d.action == _stale(EquityExitReason.FORCED, 2, 67000, 2, kind)


def test_a_non_realtime_quote_of_unnamed_type_records_unknown() -> None:
    """The runtime may only know "not real-time": the type is then unknown."""
    at = _at(DEADLINE, 15, 50)
    d = decide_equity(POLICY, _open(), _q(67000, at, realtime=False), at)
    assert d.action == _stale(EquityExitReason.FORCED, 2, 67000, 2, "unknown")


def test_a_passed_deadline_exits_on_a_frozen_bid_at_the_next_open() -> None:
    now = _at(date(2026, 10, 26), 9, 30)
    d = decide_equity(POLICY, _open(), _q(67000, now, kind="frozen"), now)
    assert d.action == _stale(EquityExitReason.DEADLINE, 2, 67000, 2, "frozen")


@pytest.mark.parametrize("quote", [None, "no-bid", "zero-bid"])
def test_a_forced_exit_without_any_bid_stays_unpriced(quote: str | None) -> None:
    at = _at(DEADLINE, 15, 50)
    q = {None: None, "no-bid": _q(None, at, kind="delayed"), "zero-bid": _q(0, at, kind="delayed")}
    d = decide_equity(POLICY, _open(), q[quote], at)
    assert d.action == EquityClose(
        EquityExitReason.FORCED, 2, None, quote_type=None, bid_cents=None
    )


def test_a_delayed_fallback_sells_only_the_open_shares() -> None:
    st = _open(qty=100, exit_filled_qty=63)
    at = _at(DEADLINE, 15, 46)
    d = decide_equity(POLICY, st, _q(67000, at, kind="delayed"), at)
    assert d.action == _stale(EquityExitReason.FORCED, 37, 67000, 2, "delayed")


#: (seconds the exit has been working, concession cents): 2 at once, 2 more
#: per FULL minute, capped at 10 cents
_WALK = [(0, 2), (30, 2), (59, 2), (60, 4), (119, 4), (120, 6), (180, 8), (240, 10), (600, 10)]


@pytest.mark.parametrize(("working_s", "concession"), _WALK)
def test_a_stale_forced_exit_concedes_two_ticks_per_minute_capped(
    working_s: int, concession: int
) -> None:
    began = _at(DEADLINE, 15, 45)
    st = _open(status=Status.EXIT_WORKING, exit_reason="forced", updated_at=began)
    now = began + timedelta(seconds=working_s)
    d = decide_equity(POLICY, st, _q(67000, now, kind="delayed"), now)
    assert d.action == _stale(EquityExitReason.FORCED, 2, 67000, concession, "delayed")


def test_a_realtime_bid_is_never_conceded_however_long_the_exit_works() -> None:
    began = _at(DEADLINE, 15, 45)
    st = _open(status=Status.EXIT_WORKING, exit_reason="forced", updated_at=began)
    now = _at(DEADLINE, 15, 58)
    d = decide_equity(POLICY, st, _q(66990, now), now)
    assert d.action == _rt(EquityExitReason.FORCED, 2, 66990)


@pytest.mark.parametrize(
    ("bid", "working_s", "concession"),
    [
        (1000, 0, 2),  # 50 bps of $10.00 is 5 cents: not binding at first
        (1000, 240, 5),  # ...but it caps the walk below the 10-cent cap
        (400, 0, 2),  # 50 bps of $4.00 is exactly 2 cents
        (399, 0, 1),  # floors to 1 cent
        (199, 600, 0),  # under $2 the bid itself is the limit
    ],
)
def test_the_concession_never_exceeds_fifty_bps_of_the_bid(
    bid: int, working_s: int, concession: int
) -> None:
    assert ee.stale_concession_cents(bid, working_s) == concession


def test_the_documented_fallback_policy_is_the_module_policy() -> None:
    assert (
        ee.STALE_CONCESSION_STEP_CENTS,
        ee.STALE_CONCESSION_EVERY_S,
        ee.STALE_CONCESSION_CAP_CENTS,
        ee.STALE_CONCESSION_CAP_BPS,
        ee.EXIT_QUOTE_BLIND_AFTER_S,
        ee.EXIT_QUOTE_BLIND,
    ) == (2, 60, 10, 50, 300, "exit_quote_blind")
    assert ee.ANY_BID_REASONS == {
        EquityExitReason.FORCED,
        EquityExitReason.DEADLINE,
        EquityExitReason.FLATTEN,
    }


def test_stop_never_confirms_on_delayed_bids_however_many_or_low() -> None:
    st = _open()
    start = _at(ENTRY_DAY, 10, 0)
    for i in range(1, 4 * POLICY.stop_confirm_ticks):
        tick = start + timedelta(seconds=i)
        d = decide_equity(POLICY, st, _q(STOP - 500, tick, kind="delayed"), tick)
        assert isinstance(d.action, EquityHold)
        st.stop_ticks, st.stop_tick_at = d.stop_ticks, d.stop_tick_at
    assert (st.stop_ticks, st.stop_tick_at) == (0, None)


@pytest.mark.parametrize("kind", ["delayed", "frozen"])
def test_target_never_fires_on_a_non_realtime_bid(kind: str) -> None:
    tick = _at(ENTRY_DAY, 11, 0)
    d = decide_equity(POLICY, _open(), _q(TARGET + 500, tick, kind=kind), tick)
    assert isinstance(d.action, EquityHold)


@pytest.mark.parametrize("reason", ["stop", "target"])
def test_a_working_price_triggered_exit_needs_real_time_before_the_forced_time(
    reason: str,
) -> None:
    st = _open(status=Status.EXIT_WORKING, exit_reason=reason, updated_at=_at(ENTRY_DAY, 10, 0))
    now = _at(ENTRY_DAY, 11, 0)
    d = decide_equity(POLICY, st, _q(67000, now, kind="delayed"), now)
    assert d.action == EquityClose(EquityExitReason(reason), 2, None)


@pytest.mark.parametrize("reason", ["stop", "target"])
def test_a_working_price_triggered_exit_takes_any_bid_once_the_forced_time_is_due(
    reason: str,
) -> None:
    """Still held at the forced time: the exit closes whatever priced it.
    Working since the entry day, the concession is at its cap."""
    st = _open(status=Status.EXIT_WORKING, exit_reason=reason, updated_at=_at(ENTRY_DAY, 10, 0))
    now = _at(DEADLINE, 15, 45)
    d = decide_equity(POLICY, st, _q(67000, now, kind="delayed"), now)
    assert d.action == _stale(EquityExitReason(reason), 2, 67000, 10, "delayed")


def test_a_confirmed_stop_without_real_time_stays_unpriced_until_the_forced_time() -> None:
    confirmed = POLICY.stop_confirm_ticks
    st = _open(stop_ticks=confirmed, stop_tick_at=_at(ENTRY_DAY, 10, 0))
    early = _at(DEADLINE, 15, 44, 59)
    d = decide_equity(POLICY, st, _q(66000, early, kind="delayed"), early)
    assert d.action == EquityClose(EquityExitReason.STOP, 2, None)
    due = _at(DEADLINE, 15, 45)
    d = decide_equity(POLICY, st, _q(66000, due, kind="delayed"), due)
    assert d.action == _stale(EquityExitReason.STOP, 2, 66000, 2, "delayed")


def test_a_working_flatten_takes_any_bid() -> None:
    st = _open(status=Status.EXIT_WORKING, exit_reason="flatten", updated_at=_at(ENTRY_DAY, 11, 0))
    now = _at(ENTRY_DAY, 11, 0, 30)
    d = decide_equity(POLICY, st, _q(67000, now, kind="frozen"), now)
    assert d.action == _stale(EquityExitReason.FLATTEN, 2, 67000, 2, "frozen")


def test_exit_close_prices_flatten_like_the_runtime_needs() -> None:
    at = _at(ENTRY_DAY, 11, 0)
    close = ee.exit_close(
        EquityExitReason.FLATTEN, 2, _q(67000, at, kind="delayed"), any_bid=True, working_s=60
    )
    assert close == _stale(EquityExitReason.FLATTEN, 2, 67000, 4, "delayed")
    priced_only_rt = ee.exit_close(
        EquityExitReason.FLATTEN, 2, _q(67000, at, kind="delayed"), any_bid=False
    )
    assert priced_only_rt == EquityClose(EquityExitReason.FLATTEN, 2, None)


def test_a_quote_type_contradicting_realtime_refuses() -> None:
    with pytest.raises(ValueError, match="market_data_type"):
        EquityQuote(bid_cents=1, ask_cents=2, realtime=True, at=None, market_data_type="delayed")
    with pytest.raises(ValueError, match="market_data_type"):
        EquityQuote(bid_cents=1, ask_cents=2, realtime=False, at=None, market_data_type="realtime")
    with pytest.raises(ValueError, match="market_data_type"):
        EquityQuote(bid_cents=1, ask_cents=2, realtime=False, at=None, market_data_type="stale")


# ------------------------------------------------------------ exit_quote_blind alert


def _blind_close(reason: EquityExitReason = EquityExitReason.FORCED) -> EquityClose:
    return EquityClose(reason, 2, None)


def test_an_exit_unpriced_for_five_minutes_alerts_once() -> None:
    watch = ee.ExitQuoteWatch()
    t0 = _at(DEADLINE, 15, 45)
    for s in range(0, 300, 5):
        assert watch.observe("eq-a", _blind_close(), t0 + timedelta(seconds=s)) is None
    alert = watch.observe("eq-a", _blind_close(), t0 + timedelta(seconds=300))
    assert alert == ee.QuoteBlindAlert(
        structure_id="eq-a",
        reason=EquityExitReason.FORCED,
        session=DEADLINE,
        blind_since=t0,
        blind_s=300,
    )
    assert alert is not None
    payload = alert.payload()
    assert payload == {
        "reason": "forced",
        "session": "2026-10-23",
        "blind_since": t0.isoformat(),
        "blind_s": 300,
        "after_s": 300,
    }
    assert json.loads(json.dumps(payload)) == payload, "plain JSON for events.jsonl"
    for s in (301, 600, 900):
        assert watch.observe("eq-a", _blind_close(), t0 + timedelta(seconds=s)) is None


def test_a_priced_tick_or_a_hold_restarts_the_blind_clock() -> None:
    watch = ee.ExitQuoteWatch()
    t0 = _at(DEADLINE, 15, 45)
    priced = _stale(EquityExitReason.FORCED, 2, 67000, 2, "delayed")
    watch.observe("eq-a", _blind_close(), t0)
    assert watch.observe("eq-a", priced, t0 + timedelta(seconds=290)) is None
    assert watch.observe("eq-a", _blind_close(), t0 + timedelta(seconds=300)) is None
    assert watch.observe("eq-a", EquityHold("hold"), t0 + timedelta(seconds=310)) is None
    t1 = t0 + timedelta(seconds=320)
    assert watch.observe("eq-a", _blind_close(), t1) is None
    assert watch.observe("eq-a", _blind_close(), t1 + timedelta(seconds=299)) is None
    alert = watch.observe("eq-a", _blind_close(), t1 + timedelta(seconds=300))
    assert alert is not None and alert.blind_since == t1


def test_blind_again_in_the_same_session_does_not_alert_twice() -> None:
    watch = ee.ExitQuoteWatch()
    t0 = _at(DEADLINE, 10, 0)
    watch.observe("eq-a", _blind_close(), t0)
    assert watch.observe("eq-a", _blind_close(), t0 + timedelta(seconds=300)) is not None
    priced = _stale(EquityExitReason.FORCED, 2, 67000, 2, "delayed")
    watch.observe("eq-a", priced, t0 + timedelta(seconds=400))
    t1 = t0 + timedelta(seconds=500)
    watch.observe("eq-a", _blind_close(), t1)
    assert watch.observe("eq-a", _blind_close(), t1 + timedelta(seconds=900)) is None


def test_each_session_alerts_again_and_restarts_its_clock() -> None:
    """Unpriced at the close, still unpriced at the next open: the clock
    restarts at the open (no instant alert from yesterday's minutes)."""
    watch = ee.ExitQuoteWatch()
    day1 = _at(DEADLINE, 15, 50)
    watch.observe("eq-a", _blind_close(), day1)
    assert watch.observe("eq-a", _blind_close(), _at(DEADLINE, 15, 55)) is not None
    monday = date(2026, 10, 26)
    opened = _at(monday, 9, 30)
    deadline = _blind_close(EquityExitReason.DEADLINE)
    assert watch.observe("eq-a", deadline, opened) is None
    assert watch.observe("eq-a", deadline, _at(monday, 9, 34, 59)) is None
    alert = watch.observe("eq-a", deadline, _at(monday, 9, 35))
    assert alert is not None
    assert (alert.session, alert.blind_since, alert.reason) == (
        monday,
        opened,
        EquityExitReason.DEADLINE,
    )


def test_structures_are_watched_independently() -> None:
    watch = ee.ExitQuoteWatch()
    t0 = _at(DEADLINE, 15, 45)
    watch.observe("eq-a", _blind_close(), t0)
    watch.observe("eq-b", _blind_close(), t0 + timedelta(seconds=60))
    alert_a = watch.observe("eq-a", _blind_close(), t0 + timedelta(seconds=300))
    assert alert_a is not None and alert_a.structure_id == "eq-a"
    assert watch.observe("eq-b", _blind_close(), t0 + timedelta(seconds=300)) is None
    alert_b = watch.observe("eq-b", _blind_close(), t0 + timedelta(seconds=360))
    assert alert_b is not None and alert_b.structure_id == "eq-b"
    watch.forget("eq-a")
    assert watch.observe("eq-a", _blind_close(), t0 + timedelta(seconds=400)) is None


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
    assert d.action == _rt(EquityExitReason.TARGET, 2, 67900)


def test_working_exit_with_an_unknown_reason_keeps_exiting() -> None:
    st = _open(status=Status.EXIT_WORKING, exit_reason="from-a-newer-build")
    tick = _at(ENTRY_DAY, 11, 0)
    d = decide_equity(POLICY, st, _q(67000, tick), tick)
    assert isinstance(d.action, EquityClose) and d.action.qty == 2


def test_the_close_sizes_to_the_open_quantity_only() -> None:
    st = _open(qty=100, exit_filled_qty=63)
    tick = _at(ENTRY_DAY, 11, 0)
    d = decide_equity(POLICY, st, _q(TARGET, tick), tick)
    assert d.action == _rt(EquityExitReason.TARGET, 37, TARGET)


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
