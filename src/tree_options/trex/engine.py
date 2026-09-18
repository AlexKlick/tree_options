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
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, time
from decimal import Decimal
from enum import StrEnum

from tree_options.trex.clock import EntryWindow
from tree_options.trex.plan import PutSpread, cents
from tree_options.trex.state import Status, StructureState


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
    """One poll of the market: spots by underlying, combo quotes by structure."""

    ts: datetime  # aware, ET
    spots: Mapping[str, Decimal]
    quotes: Mapping[str, ComboQuote | None]


class ExitReason(StrEnum):
    TOUCH = "touch"
    TAKE_PROFIT = "take_profit"
    TIME_STOP = "time_stop"
    EXPIRY_SAFETY = "expiry_safety"
    FLATTEN = "flatten"  # runner-injected (kill file); engine never emits it


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
