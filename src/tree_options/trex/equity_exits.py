"""Wave 2 lane C: the exit rules of a desk-owned long equity position.

Pure and broker-free, like ``trex.engine``: (exit policy, persisted state,
the latest quote, the clock) to at most one action. The desk runtime
(``trex.desk_runtime``) does the I/O around it; every counter lives in the
persisted ``StructureState``, so a restarted desk resumes the same
discipline.

The policy is the effect's ``exit_policy`` (``contracts.EquityExitPolicy``),
bound into the ``trex-desk-spec/2`` equity spec at registration. There is no
resting broker stop or target: every exit is a desk-sent SELL, a DAY limit
marketable at the bid, for the open shares only. Rules, the first that
applies (only inside the regular session of a trading day, early closes
included):

- an exit already working keeps its reason and follows the bid;
- ``stop``: the bid printed at or below ``stop_price_cents`` on
  ``stop_confirm_ticks`` CONSECUTIVE new real-time quote ticks. A tick is
  new when its instant is later than the last one counted
  (``StructureState.stop_tick_at``); a tick above the stop resets the
  count; a non-real-time (delayed, frozen) or missing quote neither counts
  nor resets;
- ``target``: a real-time bid at or above ``target_price_cents`` (when set);
- ``forced``: on the deadline session, from ``forced_exit_et`` (15:45) or,
  on an early close, from 15 minutes before the close if that is earlier
  (12:45 on a 13:00 close);
- ``deadline``: the deadline session has passed with shares still held: at
  once.

A deadline that is not a session resolves to the last session before it
(closing earlier is the safe side). While the ENTRY is still working, any
rule that fires aborts its unfilled remainder (``EquityAbort``); the
runtime then exits what filled. A close is priced only from a real-time
bid; without one it stays pending (``limit_cents`` None).
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass
from datetime import date, datetime, time
from enum import StrEnum

from tree_options.time.sessions import shift_instant
from tree_options.trex.clock import ET, session_calendar
from tree_options.trex.contracts.effects import EquityExitPolicy
from tree_options.trex.contracts.vocabulary import DeskEvent
from tree_options.trex.state import ENTRY_LANE, Status, StructureState

#: on an early close the forced exit comes this long before the close
FORCED_EXIT_CLOSE_BUFFER_S = 15 * 60


class EquityExitReason(StrEnum):
    STOP = "stop"
    TARGET = "target"
    FORCED = "forced"  # the deadline session's forced exit time
    DEADLINE = "deadline"  # the deadline session passed with shares held
    FLATTEN = "flatten"  # runner-injected (kill file); never decided here


#: the frozen C2.7 event each rule emits when it starts an exit
EXIT_EVENTS: dict[EquityExitReason, DeskEvent] = {
    EquityExitReason.STOP: DeskEvent.EQUITY_EXIT_STOP,
    EquityExitReason.TARGET: DeskEvent.EQUITY_EXIT_TARGET,
    EquityExitReason.DEADLINE: DeskEvent.EQUITY_EXIT_DEADLINE,
    EquityExitReason.FORCED: DeskEvent.EQUITY_EXIT_FORCED,
}


@dataclass(frozen=True)
class EquityQuote:
    """The stock's latest NBBO as the desk read it: integer cents (None =
    no price), whether IBKR delivered it real-time, and the tick's instant
    (None = no tick yet)."""

    bid_cents: int | None
    ask_cents: int | None
    realtime: bool
    at: datetime | None


@dataclass(frozen=True)
class EquityHold:
    reason: str


@dataclass(frozen=True)
class EquityAbort:
    """Cancel the working entry's unfilled remainder (a rule fired)."""

    reason: str


@dataclass(frozen=True)
class EquityClose:
    """SELL ``qty`` shares (the open quantity, never more) as a DAY limit at
    ``limit_cents`` (the real-time bid); None: no real-time bid, pending."""

    reason: EquityExitReason
    qty: int
    limit_cents: int | None


EquityAction = EquityHold | EquityAbort | EquityClose


@dataclass(frozen=True)
class EquityDecision:
    """One tick's action plus the stop confirmation the runtime persists."""

    action: EquityAction
    stop_ticks: int
    stop_tick_at: datetime | None


def deadline_session(policy: EquityExitPolicy) -> date:
    """The session the position must be closed by: ``exit_deadline_session``
    or, when that day is not a session, the last session before it."""
    sessions = session_calendar().sessions()
    i = bisect.bisect_right(sessions, policy.exit_deadline_session)
    if i == 0:
        raise ValueError(f"no session on or before {policy.exit_deadline_session}")
    return sessions[i - 1]


def forced_exit_at(session: date, forced_exit_et: str = "15:45") -> datetime:
    """The forced exit instant on ``session`` (ET): ``forced_exit_et``, or 15
    minutes before the session's close when that is earlier (early closes)."""
    hours, minutes = (int(part) for part in forced_exit_et.split(":"))
    wall = datetime.combine(session, time(hours, minutes), tzinfo=ET)
    close = session_calendar().session_close(session)
    buffered = shift_instant(close, -FORCED_EXIT_CLOSE_BUFFER_S).astimezone(ET)
    return min(wall, buffered)


def in_regular_session(now: datetime) -> bool:
    """Inside a trading day's regular session [open, close) (early closes
    included): the only time equity exit rules count or act."""
    cal = session_calendar()
    day = now.astimezone(ET).date()
    if not cal.is_session(day):
        return False
    return cal.session_open(day) <= now < cal.session_close(day)


def _working_reason(stored: str | None) -> EquityExitReason:
    """A working exit's persisted reason; an unknown one (a newer build)
    keeps the exit going rather than stalling it."""
    try:
        return EquityExitReason(stored) if stored else EquityExitReason.FORCED
    except ValueError:
        return EquityExitReason.FORCED


def marketable_limit(quote: EquityQuote | None) -> int | None:
    """The marketable SELL limit: a positive real-time bid, else None."""
    if quote is None or not quote.realtime or quote.bid_cents is None:
        return None
    return quote.bid_cents if quote.bid_cents > 0 else None


def _count_stop(
    policy: EquityExitPolicy, st: StructureState, quote: EquityQuote | None
) -> tuple[int, datetime | None]:
    """(stop_ticks, stop_tick_at) after this quote."""
    if quote is None or not quote.realtime or quote.bid_cents is None or quote.at is None:
        return st.stop_ticks, st.stop_tick_at
    if st.stop_tick_at is not None and quote.at <= st.stop_tick_at:
        return st.stop_ticks, st.stop_tick_at  # not a new tick
    if quote.bid_cents <= policy.stop_price_cents:
        return st.stop_ticks + 1, quote.at
    return 0, quote.at


def _rule(
    policy: EquityExitPolicy, ticks: int, quote: EquityQuote | None, now: datetime
) -> EquityExitReason | None:
    """The first exit rule that fires now, if any."""
    if ticks >= policy.stop_confirm_ticks:
        return EquityExitReason.STOP
    bid = marketable_limit(quote)
    target = policy.target_price_cents
    if target is not None and bid is not None and bid >= target:
        return EquityExitReason.TARGET
    deadline = deadline_session(policy)
    today = now.astimezone(ET).date()
    if today > deadline:
        return EquityExitReason.DEADLINE
    if today == deadline and now >= forced_exit_at(deadline, policy.forced_exit_et):
        return EquityExitReason.FORCED
    return None


def decide_equity(
    policy: EquityExitPolicy,
    st: StructureState,
    quote: EquityQuote | None,
    now: datetime,
) -> EquityDecision:
    """One equity position, one tick, one decision (pure: nothing mutated)."""
    if st.status is Status.CLOSED:
        return EquityDecision(EquityHold("closed"), st.stop_ticks, st.stop_tick_at)
    if st.status is Status.PLANNED:
        return EquityDecision(EquityHold("not sent yet"), st.stop_ticks, st.stop_tick_at)
    if not in_regular_session(now):
        return EquityDecision(
            EquityHold("outside the regular session"), st.stop_ticks, st.stop_tick_at
        )
    ticks, tick_at = _count_stop(policy, st, quote)

    def decided(action: EquityAction) -> EquityDecision:
        return EquityDecision(action, ticks, tick_at)

    if st.status in ENTRY_LANE:
        rule = _rule(policy, ticks, quote, now)
        return decided(EquityAbort(rule.value) if rule else EquityHold("entry working"))
    if st.open_qty <= 0:
        return decided(EquityHold("flat"))
    if st.status is Status.EXIT_WORKING:
        return decided(
            EquityClose(_working_reason(st.exit_reason), st.open_qty, marketable_limit(quote))
        )
    rule = _rule(policy, ticks, quote, now)
    if rule is None:
        return decided(EquityHold("hold"))
    return decided(EquityClose(rule, st.open_qty, marketable_limit(quote)))
