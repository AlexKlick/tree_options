"""The trex decision core — pure, broker-free, restart-safe.

``decide`` maps (plan structure, persisted state, market snapshot, clock)
to at most one action. All escalation counters live in the persisted state,
so a killed-and-restarted runner resumes the same discipline instead of
resetting it. The engine's opinions are exactly the plan's opinions:

- pay at most the cap; start at mid, escalate toward the ask only after
  ``entry_max_cycles`` repricings, stop trying when the entry window ends;
- once open, exit on touch of the long strike, on the optional mid-of-width
  take-profit, on the deadline date (time stop), or on the expiry backstop;
- exit pricing starts at mid, escalates to the bid (marketable), and goes
  hard-marketable after ``exit_force_time`` on the deadline day.

No action widens a structure, adds contracts, or sells anything naked —
not as a policy comment, as an absence of representable actions.

The desk (multi-leg) path is separate code: ``decide_structure`` for every
plan.LegStructure kind, ``step`` (drain the working order's fills, then
decide) and ``adopted_role`` (which lane a working order found at the
broker belongs to). It shares only the value types and the process config
with the legacy ``decide``, which stays byte-identical for the live
put-spread book (tests/unit/test_trex_legacy_characterization.py).
"""

from __future__ import annotations

import bisect
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from enum import StrEnum
from typing import Literal

from tree_options.trex.clock import EntryWindow, session_calendar
from tree_options.trex.plan import LegStructure, PutSpread, cents
from tree_options.trex.spot import SpotReading
from tree_options.trex.state import ENTRY_LANE, Status, StructureState


@dataclass(frozen=True)
class ComboQuote:
    """NBBO of the vertical as one package (debit terms for the buyer)."""

    bid: Decimal
    ask: Decimal

    @property
    def mid(self) -> Decimal:
        return cents((self.bid + self.ask) / 2)


@dataclass(frozen=True)
class Snapshot:
    """One poll of the market: spots by underlying, combo quotes by structure.

    ``spots`` holds only ACCEPTED readings (trex.spot): an underlying with
    no fresh price is absent, and absent means no touch decision.
    ``spot_sources`` is their provenance, for observation only."""

    ts: datetime  # aware, ET
    spots: Mapping[str, Decimal]
    quotes: Mapping[str, ComboQuote | None]
    spot_sources: Mapping[str, SpotReading] = field(default_factory=dict)


class ExitReason(StrEnum):
    TOUCH = "touch"
    TAKE_PROFIT = "take_profit"
    TIME_STOP = "time_stop"
    EXPIRY_SAFETY = "expiry_safety"
    FLATTEN = "flatten"  # runner-injected (kill file); engine never emits it
    BREACH = "breach"  # desk: spot crossed a short strike (credit kinds)
    STOP_LOSS = "stop_loss"  # desk: held for ExitRules.stop_confirm_ticks ticks


@dataclass(frozen=True)
class EngineConfig:
    entry_window: EntryWindow
    entry_max_cycles: int = 4
    exit_max_mid_cycles: int = 3
    time_stop_time: time = time(9, 45)  # flatten at/after this on deadline day
    exit_force_time: time = time(15, 45)  # hard-marketable backstop
    session_end: time = time(16, 15)  # runner winding down; no new decisions


# -- actions ---------------------------------------------------------------


@dataclass(frozen=True)
class NoAction:
    reason: str


@dataclass(frozen=True)
class PlaceEntry:
    """Place/refresh the entry combo at ``limit``; limit at the ask = marketable."""

    limit: Decimal


@dataclass(frozen=True)
class AbortEntry:
    reason: str


@dataclass(frozen=True)
class ExitOrder:
    reason: ExitReason
    # None: no quote yet, keep the flatten pending. mid = passive, bid =
    # marketable; IBKR takes no true market orders on combos, so the hard
    # backstop is repricing at the fresh bid every cycle until flat.
    limit: Decimal | None


Action = NoAction | PlaceEntry | AbortEntry | ExitOrder


def _entry_limit(spread: PutSpread, quote: ComboQuote, cycles: int) -> PlaceEntry:
    if cycles >= _CFG.entry_max_cycles:
        return PlaceEntry(limit=cents(min(quote.ask, spread.limit_cap)))
    return PlaceEntry(limit=cents(min(quote.mid, spread.limit_cap)))


_CFG = EngineConfig(EntryWindow(time(9, 45), time(12, 0)))  # replaced via configure()


def configure(config: EngineConfig) -> None:
    """Install the process-wide engine config (runners call once at boot)."""
    global _CFG
    _CFG = config


def decide(spread: PutSpread, st: StructureState, snap: Snapshot) -> Action:
    """One structure, one tick, one decision."""
    cfg = _CFG
    now = snap.ts

    if st.status is Status.CLOSED:
        return NoAction("closed")

    # -- entry phase ------------------------------------------------------

    if st.status in (Status.PLANNED, Status.ENTER_WORKING):
        if now.date() > spread.entry_date:
            return AbortEntry("entry date passed")
        if now.date() < spread.entry_date:
            return NoAction("before entry date")
        if cfg.entry_window.after(now):
            return AbortEntry("entry window closed")
        if cfg.entry_window.before(now):
            return NoAction("before entry window")
        if st.status is Status.ENTER_WORKING:
            return NoAction("entry working")  # runner owns order events
        quote = snap.quotes.get(spread.id)
        if quote is None:
            return NoAction("no quote")
        return _entry_limit(spread, quote, st.entry_cycles)

    # -- open / exiting ---------------------------------------------------

    if now.date() >= spread.expiry:
        return ExitOrder(ExitReason.EXPIRY_SAFETY, _exit_limit(snap, spread, st, force=True))

    if st.status is Status.EXIT_WORKING:
        force = now.date() >= spread.exit_deadline and now.time() >= cfg.exit_force_time
        reason = ExitReason(st.exit_reason) if st.exit_reason else ExitReason.TIME_STOP
        return ExitOrder(reason, _exit_limit(snap, spread, st, force=force))

    spot = snap.spots.get(spread.underlying)
    if spot is not None and spot <= spread.long_strike:
        return ExitOrder(ExitReason.TOUCH, _exit_limit(snap, spread, st, force=False))

    quote = snap.quotes.get(spread.id)
    if (
        spread.take_profit_frac is not None
        and quote is not None
        and quote.mid >= cents(spread.take_profit_frac * spread.width)
    ):
        return ExitOrder(ExitReason.TAKE_PROFIT, _exit_limit(snap, spread, st, force=False))

    if now.date() >= spread.exit_deadline and now.time() >= cfg.time_stop_time:
        return ExitOrder(ExitReason.TIME_STOP, _exit_limit(snap, spread, st, force=False))

    return NoAction("hold")


def _exit_limit(
    snap: Snapshot,
    spread: PutSpread,
    st: StructureState,
    force: bool,
) -> Decimal | None:
    quote = snap.quotes.get(spread.id)
    if quote is None:
        return None
    if force:
        return cents(quote.bid)
    if st.exit_cycles >= _CFG.exit_max_mid_cycles:
        return cents(quote.bid)
    return quote.mid


# -- desk: every LegStructure kind ------------------------------------------
#
# All prices are package prices in DEBIT ORIENTATION (plan.LegStructure.
# package_legs), so every limit is positive: a debit kind opens by BUYING
# the package (limit <= cap) and closes by SELLING it; a credit kind opens
# by SELLING it (limit >= floor) and closes by BUYING it back (limit <= the
# package's maximum value). Snapshot.quotes[spec.id] is that package's quote.

STALE_DEAL = "stale_deal"
# an entry aborts once the package mid is MORE than this fraction of the
# deal's reference mid (LegStructure.ref_mid) away from it, either way
STALE_DEAL_MOVE = Decimal("0.25")
# credit kinds without an explicit take-profit: half the credit captured
CREDIT_TAKE_PROFIT_DEFAULT = Decimal("0.50")
_TICK = Decimal("0.01")  # the smallest orderable package price

Side = Literal["BUY", "SELL"]
Role = Literal["entry", "exit"]


@dataclass(frozen=True)
class EntryOrder:
    """Open ``qty`` packages (what the structure still lacks) with ``side``
    (the spec's open side) at ``limit``, a positive price within the cap or
    floor. For a working entry it is the price that order should carry: the
    runtime reprices it (cancel, confirmed, replace) only when it differs."""

    side: Side
    qty: int
    limit: Decimal


@dataclass(frozen=True)
class CloseOrder:
    """Close ``qty`` packages (st.open_qty, never more) with ``side`` (the
    spec's close side) at ``limit``: positive, and for a BUY-to-close never
    above the package's maximum value. ``limit`` None: no quote yet, keep
    the exit pending."""

    reason: ExitReason
    side: Side
    qty: int
    limit: Decimal | None


DeskAction = NoAction | EntryOrder | AbortEntry | CloseOrder


@dataclass(frozen=True)
class StructureDecision:
    """One tick's action, plus the stop-loss confirmation count the runtime
    persists (StructureState.stop_ticks) before acting on it."""

    action: DeskAction
    stop_ticks: int


@dataclass(frozen=True)
class WorkingOrder:
    """The broker's view of a structure's current order: the lane that owns
    it (``adopted_role``) and its CUMULATIVE fills on that order
    (ibkr.OrderStatusInfo: ``filled``, ``avg_fill_price`` in debit
    orientation, i.e. the debit paid or the credit received per package;
    0 when the broker has no average yet), keyed by the broker's
    ``order_id`` (the book's entry_order / exit_order)."""

    role: Role
    filled: int
    avg_fill_price: Decimal
    order_id: str


def last_hold_session(first_expiry: date) -> date:
    """The last NYSE session strictly before ``first_expiry`` (the trex
    session calendar, holidays and weekends respected, no date arithmetic):
    the last day any leg may still be held. An expiry past the calendar's
    horizon resolves to the calendar's last session, i.e. earlier than
    needed (fail safe; clock.calendar_horizon_warn flags the horizon)."""
    sessions = session_calendar().sessions()
    i = bisect.bisect_left(sessions, first_expiry)
    if i == 0:
        raise ValueError(f"no session before {first_expiry} in the trex calendar")
    return sessions[i - 1]


def decide_structure(spec: LegStructure, st: StructureState, snap: Snapshot) -> StructureDecision:
    """One desk structure, one tick, one decision (pure: nothing mutated).

    Entry (PLANNED / ENTER_WORKING): on the entry date inside the entry
    window, never on or after the last hold session; the ladder pays
    min(mid, cap) then, after ``entry_max_cycles`` repricings, min(ask,
    cap) for a debit kind, and receives max(mid, floor) then max(bid,
    floor) for a credit kind; ``stale_deal`` aborts when the mid moved more
    than STALE_DEAL_MOVE from ``spec.ref_mid``.

    Exits (OPEN / EXIT_WORKING), the first that applies:

    1. expiry safety: from the last session before the FIRST expiry
       (last_hold_session), close at marketable prices; no leg is ever
       held into its expiry (for calendars and diagonals this is what makes
       max loss = the debit true);
    2. an exit already working keeps its reason (marketable after
       ``exit_force_time`` on the deadline);
    3. touch (exits.touch): spot at or beyond a long strike toward the view
       (put: spot <= strike, call: spot >= strike);
    4. breach (exits.breach): spot at or beyond a short strike (put: spot <=
       strike, call: spot >= strike);
    5. take-profit on the package mid: width_frac (mid >= cents(value x
       width), the legacy rule), gain_frac (mid >= entry x (1 + value)),
       credit_frac (mid <= entry x (1 - value)); credit kinds default to
       credit_frac CREDIT_TAKE_PROFIT_DEFAULT;
    6. stop-loss once it held ``exits.stop_confirm_ticks`` consecutive
       evaluated ticks (debit_frac: mid <= entry x (1 - value); credit_mult:
       mid >= entry x value); a tick that meets it counts, one that doesn't
       resets, one that can't be evaluated (no quote, no entry price)
       neither counts nor resets;
    7. time stop: at ``time_stop_time`` on the exit deadline, at once after it.

    Without a spot there is no touch or breach decision; without an entry
    price no gain/credit take-profit or stop. Closes size to st.open_qty
    (nothing when flat) and price at mid, then (after
    ``exit_max_mid_cycles`` repricings, or when forced) the bid for a SELL
    and the ask for a BUY; a BUY-to-close is capped at the package's
    maximum value (vertical width, condor wider wing), and no price goes
    below one cent (plan.validate_package_order refuses anything else).
    """
    if st.status is Status.CLOSED:
        return StructureDecision(NoAction("closed"), st.stop_ticks)
    if st.status in ENTRY_LANE:
        return StructureDecision(_desk_entry(spec, st, snap), st.stop_ticks)
    return _desk_exit(spec, st, snap)


def _desk_entry(spec: LegStructure, st: StructureState, snap: Snapshot) -> DeskAction:
    cfg = _CFG
    now = snap.ts
    if now.date() > spec.entry_date:
        return AbortEntry("entry date passed")
    if now.date() < spec.entry_date:
        return NoAction("before entry date")
    if now.date() >= last_hold_session(spec.first_expiry):
        return AbortEntry("inside expiry safety")
    if cfg.entry_window.after(now):
        return AbortEntry("entry window closed")
    if cfg.entry_window.before(now):
        return NoAction("before entry window")
    remaining = spec.quantity - st.filled_qty
    if remaining <= 0:
        return NoAction("entry filled")
    quote = snap.quotes.get(spec.id)
    if quote is None:
        return NoAction("no quote")
    if spec.ref_mid is not None and abs(quote.mid - spec.ref_mid) > STALE_DEAL_MOVE * spec.ref_mid:
        return AbortEntry(STALE_DEAL)
    escalated = st.entry_cycles >= cfg.entry_max_cycles
    if spec.is_credit:
        # receive at least the floor: round up, never below it
        limit = max(quote.bid if escalated else quote.mid, spec.limit).quantize(
            _TICK, rounding=ROUND_CEILING
        )
    else:
        # pay at most the cap: round down, never above it
        limit = min(quote.ask if escalated else quote.mid, spec.limit).quantize(
            _TICK, rounding=ROUND_FLOOR
        )
    if limit <= 0:
        return NoAction("no positive price")
    return EntryOrder(spec.open_side, remaining, limit)


def _desk_exit(spec: LegStructure, st: StructureState, snap: Snapshot) -> StructureDecision:
    cfg = _CFG
    now = snap.ts
    today = now.date()
    if st.open_qty <= 0:
        return StructureDecision(NoAction("flat"), st.stop_ticks)
    quote = snap.quotes.get(spec.id)

    def close(
        reason: ExitReason, *, force: bool = False, ticks: int | None = None
    ) -> StructureDecision:
        limit = _desk_close_limit(spec, st, quote, force)
        order = CloseOrder(reason, spec.close_side, st.open_qty, limit)
        return StructureDecision(order, st.stop_ticks if ticks is None else ticks)

    # 1. expiry safety
    if today >= last_hold_session(spec.first_expiry):
        return close(ExitReason.EXPIRY_SAFETY, force=True)
    # 2. an exit already working
    if st.status is Status.EXIT_WORKING:
        force = today >= spec.exit_deadline and now.time() >= cfg.exit_force_time
        return close(_working_reason(st.exit_reason), force=force)
    # 3. touch (debit kinds: the long strike) / 4. breach (credit kinds: a
    # short strike); the plan refuses the flags elsewhere, and so does this
    spot = snap.spots.get(spec.underlying)
    if spot is not None:
        if (
            spec.exits.touch
            and not spec.is_credit
            and any(_at_or_beyond(g.right, spot, g.strike) for g in spec.legs if g.action == "BUY")
        ):
            return close(ExitReason.TOUCH)
        if (
            spec.exits.breach
            and spec.is_credit
            and any(_at_or_beyond(g.right, spot, g.strike) for g in spec.legs if g.action == "SELL")
        ):
            return close(ExitReason.BREACH)
    # 5. take-profit
    if quote is not None and _take_profit_hit(spec, st, quote.mid):
        return close(ExitReason.TAKE_PROFIT)
    # 6. stop-loss, confirmed over consecutive ticks
    ticks = _stop_ticks(spec, st, quote)
    if spec.exits.stop_loss is not None and ticks >= spec.exits.stop_confirm_ticks:
        return close(ExitReason.STOP_LOSS, ticks=ticks)
    # 7. time stop
    deadline = spec.exit_deadline
    if today > deadline or (today == deadline and now.time() >= cfg.time_stop_time):
        return close(ExitReason.TIME_STOP, ticks=ticks)
    return StructureDecision(NoAction("hold"), ticks)


def _at_or_beyond(right: str, spot: Decimal, strike: Decimal) -> bool:
    """Spot at the strike or past it in the option's money direction."""
    return spot <= strike if right == "P" else spot >= strike


def _working_reason(stored: str | None) -> ExitReason:
    """The persisted reason of a working exit; an unknown one (a book from
    a newer build) keeps the exit going as a time stop, never stalls it."""
    try:
        return ExitReason(stored) if stored else ExitReason.TIME_STOP
    except ValueError:
        return ExitReason.TIME_STOP


def _take_profit_hit(spec: LegStructure, st: StructureState, mid: Decimal) -> bool:
    tp = spec.exits.take_profit
    if tp is not None:
        basis, value = tp.basis, tp.value
    elif spec.is_credit:
        basis, value = "credit_frac", CREDIT_TAKE_PROFIT_DEFAULT
    else:
        return False
    if basis == "width_frac":
        width = spec.width
        return width is not None and mid >= cents(value * width)
    entry = st.entry_fill
    if entry is None:
        return False
    if basis == "gain_frac":
        return mid >= entry * (1 + value)
    return mid <= entry * (1 - value)  # credit_frac: that much of the credit captured


def _stop_ticks(spec: LegStructure, st: StructureState, quote: ComboQuote | None) -> int:
    """The stop-loss confirmation count after this tick."""
    stop = spec.exits.stop_loss
    entry = st.entry_fill
    if stop is None or quote is None or entry is None:
        return st.stop_ticks  # not evaluable: neither counts nor resets
    if stop.basis == "debit_frac":
        hit = quote.mid <= entry * (1 - stop.value)
    else:  # credit_mult: the cost to close reached that multiple of the credit
        hit = quote.mid >= entry * stop.value
    return st.stop_ticks + 1 if hit else 0


def _desk_close_limit(
    spec: LegStructure, st: StructureState, quote: ComboQuote | None, force: bool
) -> Decimal | None:
    if quote is None:
        return None
    marketable = force or st.exit_cycles >= _CFG.exit_max_mid_cycles
    if spec.close_side == "SELL":
        return max(cents(quote.bid if marketable else quote.mid), _TICK)
    width = spec.width
    assert width is not None  # BUY-to-close: credit kinds, verticals and condors
    return min(max(cents(quote.ask if marketable else quote.mid), _TICK), width)


def drain(st: StructureState, order: WorkingOrder) -> int:
    """Merge the broker's report on the structure's working ``order`` into
    its entry or exit side; returns how many packages were newly recorded.

    Quantity and price are reconciled separately, because IBKR can report
    a fill before its average price:

    - quantity: fills beyond the order's checkpoint (``*_order_seen``) are
      recorded at once (they size every close), priced or not; a report of
      fewer fills than recorded is stale and changes nothing;
    - price: whenever the report carries an average, the order's CUMULATIVE
      notional (avg x filled) replaces what the book held for it, even with
      no new quantity, so a price that arrives after its fill still lands
      (else stops and take-profits would never become evaluable) and is
      spread over all the order's fills, never over the new ones alone.
      The side's average (entry_fill / exit_fill) spans only packages
      whose price is known: ``*_unpriced_qty`` counts the rest in the book,
      ``*_order_unpriced`` those of this order.

    The checkpoint belongs to one broker order: a report for a different
    ``order_id`` than the book's entry_order / exit_order starts a fresh
    one. The same report twice records nothing."""
    entry = order.role == "entry"
    if (st.entry_order if entry else st.exit_order) != order.order_id:
        if entry:
            st.entry_order, st.entry_order_seen, st.entry_order_notional = order.order_id, 0, None
            st.entry_order_unpriced = 0
        else:
            st.exit_order, st.exit_order_seen, st.exit_order_notional = order.order_id, 0, None
            st.exit_order_unpriced = 0
    seen = st.entry_order_seen if entry else st.exit_order_seen
    if order.filled < seen:
        return 0  # stale: the book never un-records a fill
    new = order.filled - seen
    recorded = st.filled_qty if entry else st.exit_filled_qty
    fill = st.entry_fill if entry else st.exit_fill
    notional = st.entry_order_notional if entry else st.exit_order_notional
    unpriced = st.entry_unpriced_qty if entry else st.exit_unpriced_qty
    order_unpriced = st.entry_order_unpriced if entry else st.exit_order_unpriced
    avg = order.avg_fill_price
    if order.filled > 0 and avg.is_finite() and avg > 0:
        cumulative = avg * order.filled
        if new == 0 and order_unpriced == 0 and notional == cumulative:
            return 0  # nothing new: the same report again
        # take this order's earlier priced contribution out of the average
        # and put its whole cumulative fill back in
        priced = recorded - unpriced
        others = priced - (seen - order_unpriced) if fill is not None else 0
        others_notional = fill * priced - (notional or 0) if fill is not None and others else 0
        fill = (others_notional + cumulative) / (others + order.filled)
        notional = cumulative
        unpriced -= order_unpriced
        order_unpriced = 0
    elif new == 0:
        return 0
    else:  # quantity now, its price when the broker reports one
        unpriced += new
        order_unpriced += new
    if entry:
        st.filled_qty = recorded + new
        st.entry_fill = fill
        st.entry_order_seen = order.filled
        st.entry_order_notional = notional
        st.entry_unpriced_qty, st.entry_order_unpriced = unpriced, order_unpriced
    else:
        st.exit_filled_qty = recorded + new
        st.exit_fill = fill
        st.exit_order_seen = order.filled
        st.exit_order_notional = notional
        st.exit_unpriced_qty, st.exit_order_unpriced = unpriced, order_unpriced
    return new


def step(
    spec: LegStructure,
    st: StructureState,
    snap: Snapshot,
    order: WorkingOrder | None = None,
) -> StructureDecision:
    """One desk tick for one structure: drain the working order's fills
    into the book FIRST, then decide. A decision on an undrained book sizes
    a close by packages already sold or bought back (a second close order
    for them: a naked position) and re-enters what already filled. Status
    transitions (filled entry -> OPEN, flat exit -> CLOSED) stay with the
    runtime, which records them; the decision is already safe without them
    (``entry filled`` / ``flat``)."""
    if order is not None:
        drain(st, order)
    return decide_structure(spec, st, snap)


def adopted_role(spec: LegStructure, st: StructureState, side: str) -> Role | None:
    """Which lane owns a working order found at the broker for ``spec`` (its
    trex tag already proved the structure: ibkr.structure_for_trade), from
    its side: the package's OPEN side is an entry, its CLOSE side an exit.
    Credit kinds open by SELLING the package, so their SELL is the entry,
    the reverse of the legacy book (whose SELLs are always exits).

    None when the side contradicts the book: an entry-side order on a
    structure no longer in the entry lane, or an exit-side order on one not
    open. That is unknown exposure: never adopted; the runtime alerts."""
    if side == spec.open_side and st.status in ENTRY_LANE:
        return "entry"
    if side == spec.close_side and st.status in (Status.OPEN, Status.EXIT_WORKING):
        return "exit"
    return None
