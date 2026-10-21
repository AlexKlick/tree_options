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
runtime then exits what filled.

Pricing (Wave 2b lane c2, review P2: a forced exit used to wait forever for
a real-time bid). The price TRIGGERS (stop, target) count real-time ticks
only, so delayed data never fires them. The close itself is priced:

- from a real-time bid: AT the bid, whatever the reason;
- for a time- or operator-driven exit (``ANY_BID_REASONS``: forced,
  deadline, flatten), and for ANY exit once the forced/deadline time is due
  (a stop or target exit still working then must close too), from the best
  available bid: real-time, else the delayed, frozen or unnamed one,
  ``stale_concession_cents`` below it (2 cents, 2 more per full minute the
  exit has been working, at most 10 cents and 50 bps of the bid). Always a
  DAY limit, never a market order, never more than the open shares;
- otherwise (no positive bid, or a price-triggered exit before the time
  rules are due with no real-time bid) it stays pending: ``limit_cents``
  None. ``ExitQuoteWatch`` turns a close pending for
  ``EXIT_QUOTE_BLIND_AFTER_S`` into one ``exit_quote_blind`` alert per
  structure per session.

``EquityClose.quote_type`` / ``bid_cents`` record what priced the limit,
for the exit event.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass
from datetime import date, datetime, time
from enum import StrEnum
from typing import get_args

from tree_options.time.sessions import shift_instant
from tree_options.trex.clock import ET, session_calendar
from tree_options.trex.contracts.effects import EquityExitPolicy
from tree_options.trex.contracts.proposals import MarketDataType
from tree_options.trex.contracts.vocabulary import DeskEvent
from tree_options.trex.state import ENTRY_LANE, Status, StructureState

#: on an early close the forced exit comes this long before the close
FORCED_EXIT_CLOSE_BUFFER_S = 15 * 60

#: the market-data type names a quote may carry (``contracts.proposals``)
QUOTE_TYPES: frozenset[str] = frozenset(get_args(MarketDataType))

#: a NON-real-time bid (delayed, frozen, unnamed) may already be above the
#: market, so a time-driven exit priced from it sells this many cents (two
#: 1-cent ticks) below it, STEP more per full EVERY_S seconds the exit has
#: been working, at most CAP_CENTS and at most CAP_BPS of the bid
STALE_CONCESSION_STEP_CENTS = 2
STALE_CONCESSION_EVERY_S = 60
STALE_CONCESSION_CAP_CENTS = 10
STALE_CONCESSION_CAP_BPS = 50

#: a close unpriced this long (regular session) raises EXIT_QUOTE_BLIND
EXIT_QUOTE_BLIND_AFTER_S = 5 * 60
#: the alert's ``events.jsonl`` name (a ``DeskEvent`` member once lane K
#: adds it; the plain string compares equal)
EXIT_QUOTE_BLIND = "exit_quote_blind"


class EquityExitReason(StrEnum):
    STOP = "stop"
    TARGET = "target"
    FORCED = "forced"  # the deadline session's forced exit time
    DEADLINE = "deadline"  # the deadline session passed with shares held
    FLATTEN = "flatten"  # runner-injected (kill file); never decided here


#: exits due by the clock or the operator, not by a price: they close on
#: the best available bid (a delayed one cannot fire them falsely)
ANY_BID_REASONS: frozenset[EquityExitReason] = frozenset(
    {EquityExitReason.FORCED, EquityExitReason.DEADLINE, EquityExitReason.FLATTEN}
)

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
    (None = no tick yet). ``market_data_type`` names the type when the
    reader knows it (``QUOTE_TYPES``); it must agree with ``realtime``."""

    bid_cents: int | None
    ask_cents: int | None
    realtime: bool
    at: datetime | None
    market_data_type: str | None = None

    def __post_init__(self) -> None:
        kind = self.market_data_type
        if kind is None:
            return
        if kind not in QUOTE_TYPES:
            raise ValueError(f"market_data_type {kind!r} is not one of {sorted(QUOTE_TYPES)}")
        if (kind == "realtime") != self.realtime:
            raise ValueError(f"market_data_type {kind!r} contradicts realtime={self.realtime}")

    @property
    def quote_type(self) -> str:
        """The named type, else ``realtime`` or ``unknown`` (not real-time,
        type not reported)."""
        if self.market_data_type is not None:
            return self.market_data_type
        return "realtime" if self.realtime else "unknown"


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
    ``limit_cents``; None: no usable bid, pending. ``bid_cents`` and
    ``quote_type`` record the bid that priced it (None while pending): a
    real-time bid is the limit itself, any other is conceded
    ``concession_cents`` (``stale_concession_cents``)."""

    reason: EquityExitReason
    qty: int
    limit_cents: int | None
    quote_type: str | None = None
    bid_cents: int | None = None

    @property
    def concession_cents(self) -> int:
        if self.limit_cents is None or self.bid_cents is None:
            return 0
        return self.bid_cents - self.limit_cents


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
    """A positive REAL-TIME bid, else None: what the price triggers read
    (target) and what prices an exit that needs real time. Time-driven
    exits price through ``exit_close``."""
    if quote is None or not quote.realtime or quote.bid_cents is None:
        return None
    return quote.bid_cents if quote.bid_cents > 0 else None


def stale_concession_cents(bid_cents: int, working_s: int) -> int:
    """How far below a NON-real-time bid of ``bid_cents`` an exit working
    for ``working_s`` seconds sells: ``STALE_CONCESSION_STEP_CENTS`` plus as
    many again per full ``STALE_CONCESSION_EVERY_S``, at most
    ``STALE_CONCESSION_CAP_CENTS`` and at most ``STALE_CONCESSION_CAP_BPS``
    of the bid (floored, so the limit stays positive: under $2 it is 0)."""
    steps = 1 + max(0, working_s) // STALE_CONCESSION_EVERY_S
    return min(
        steps * STALE_CONCESSION_STEP_CENTS,
        STALE_CONCESSION_CAP_CENTS,
        bid_cents * STALE_CONCESSION_CAP_BPS // 10_000,
    )


def exit_working_s(st: StructureState, now: datetime) -> int:
    """Whole seconds the exit has been working (``updated_at`` is set when
    the structure enters EXIT_WORKING); 0 before it is sent or unknown."""
    if st.status is not Status.EXIT_WORKING or st.updated_at is None:
        return 0
    return max(0, int(now.timestamp() - st.updated_at.timestamp()))


def exit_close(
    reason: EquityExitReason,
    qty: int,
    quote: EquityQuote | None,
    *,
    any_bid: bool,
    working_s: int = 0,
) -> EquityClose:
    """The close of ``qty`` shares for ``reason``: AT a positive real-time
    bid; with ``any_bid`` (a time- or operator-driven exit) from a positive
    non-real-time bid less ``stale_concession_cents``; else pending."""
    bid = None if quote is None else quote.bid_cents
    if quote is None or bid is None or bid <= 0:
        return EquityClose(reason, qty, None)
    if quote.realtime:
        return EquityClose(reason, qty, bid, quote_type=quote.quote_type, bid_cents=bid)
    if not any_bid:
        return EquityClose(reason, qty, None)
    limit = bid - stale_concession_cents(bid, working_s)
    return EquityClose(reason, qty, limit, quote_type=quote.quote_type, bid_cents=bid)


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
    return _time_rule(policy, now)


def _time_rule(policy: EquityExitPolicy, now: datetime) -> EquityExitReason | None:
    """The clock's rule due now, if any: deadline passed, or forced time."""
    deadline = deadline_session(policy)
    today = now.astimezone(ET).date()
    if today > deadline:
        return EquityExitReason.DEADLINE
    if today == deadline and now >= forced_exit_at(deadline, policy.forced_exit_et):
        return EquityExitReason.FORCED
    return None


def _close(
    policy: EquityExitPolicy,
    reason: EquityExitReason,
    st: StructureState,
    quote: EquityQuote | None,
    now: datetime,
) -> EquityClose:
    """The open shares' close: any bid for a time-driven reason, or for any
    reason once the clock's rules are due (the position must go)."""
    any_bid = reason in ANY_BID_REASONS or _time_rule(policy, now) is not None
    return exit_close(
        reason, st.open_qty, quote, any_bid=any_bid, working_s=exit_working_s(st, now)
    )


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
        return decided(_close(policy, _working_reason(st.exit_reason), st, quote, now))
    rule = _rule(policy, ticks, quote, now)
    if rule is None:
        return decided(EquityHold("hold"))
    return decided(_close(policy, rule, st, quote, now))


# -- exit_quote_blind ----------------------------------------------------------


@dataclass(frozen=True)
class QuoteBlindAlert:
    """An exit wanted to go out but had no usable bid for ``blind_s``
    seconds of the regular session ``session`` (since ``blind_since``)."""

    structure_id: str
    reason: EquityExitReason
    session: date
    blind_since: datetime
    blind_s: int

    def payload(self) -> dict[str, str | int]:
        """The ``exit_quote_blind`` event fields (plain JSON); the runtime
        adds ``structure`` and ``symbol``."""
        return {
            "reason": self.reason.value,
            "session": self.session.isoformat(),
            "blind_since": self.blind_since.isoformat(),
            "blind_s": self.blind_s,
            "after_s": EXIT_QUOTE_BLIND_AFTER_S,
        }


class ExitQuoteWatch:
    """The desk process's memory for ``exit_quote_blind``. Show it every
    equity close the runtime acts on (``observe``): a close pending without
    a usable bid starts a blind clock, anything else stops it; once a clock
    reaches ``EXIT_QUOTE_BLIND_AFTER_S`` the alert is returned, once per
    structure per ET session (a new session starts a new clock). In memory
    only: a restarted desk starts over, so it may alert once more for the
    same session (the safe side). Broker-free, never sends anything."""

    def __init__(self) -> None:
        self._since: dict[str, datetime] = {}
        self._alerted: set[tuple[str, date]] = set()

    def observe(
        self, structure_id: str, action: EquityAction, now: datetime
    ) -> QuoteBlindAlert | None:
        if not isinstance(action, EquityClose) or action.limit_cents is not None:
            self._since.pop(structure_id, None)
            return None
        session = now.astimezone(ET).date()
        since = self._since.get(structure_id)
        if since is None or since.astimezone(ET).date() != session:
            since = self._since[structure_id] = now
        blind_s = int(now.timestamp() - since.timestamp())
        if blind_s < EXIT_QUOTE_BLIND_AFTER_S or (structure_id, session) in self._alerted:
            return None
        self._alerted.add((structure_id, session))
        return QuoteBlindAlert(structure_id, action.reason, session, since, blind_s)

    def forget(self, structure_id: str) -> None:
        """Drop a closed structure's clock."""
        self._since.pop(structure_id, None)
