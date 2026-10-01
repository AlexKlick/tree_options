"""Desk environment v2: the per-candidate outcome engine (pure mechanics).

The v1 lab scored a choice through ``intraday_action_graph.replay``: no
trading costs, legs filled from unsynchronized last-trade prints, and one
exit only — the next decision clock (a median 45-minute hold: intraday
noise). Environment v2 keeps that accounting and makes each assumption an
explicit knob, per candidate:

* ``costs`` — a :class:`CostModel` round trip (commission + half-spread
  on every leg fill) is charged to ``net``; ``None`` means net == gross;
* ``leg_sync_minutes`` — both legs' prints must be within N minutes of
  each other, at entry (walk FORWARD inside the 15-minute entry window to
  the first synced pair, else ``no_fill``) and at every mark (the most
  recent synced pair inside the 15-minute freshness window, else the clock
  has no mark); ``None`` is v1's unsynced rule;
* ``exit_mode`` — one of :data:`EXIT_MODES`: ``intraday`` (v1: the first
  later clock with fresh marks), ``eod`` (the last clock of the entry
  session with fresh marks, else the next mark), ``hold:N`` (the last
  clock of the Nth later BUNDLE session — holidays and missing days are
  absent sessions — walking forward to the next mark) and ``expiry`` (the
  last mark on or before the expiry date).

A target past the expiry resolves at the last mark on or before expiry; a
target past the data end is ``marked_at_end`` at the last synced mark
(never silently dropped; a fill with no later mark at all is marked at its
own entry value). PnL is clamped to the entry-time structure bounds exactly
as replay clamps it, and a fill the replay risk caps refuse is a
``no_fill`` (``entry_risk_cap``).

Everything reuses iag's primitives (``_read_bars`` via hindsight,
``parse_contract``, ``_candidates``, ``_next``, ``_latest``,
``schedule_for``); the oracle test pins ``exit_mode="intraday"``, no
costs, no sync to ``hindsight._candidate_outcome`` for every candidate.

:func:`spot_series` / :func:`spot_asof` estimate each underlying's spot
from the bundle's OWN options by put-call parity (C - P + K, median over
the strikes of the nearest expiry with fresh prints; the horizon study
measured a p90 error near 0.2% against recorded closes). The as-of variant
reads only prints at or before its instant.

Evidence, not authority: no broker, no model, no network. Instants are UTC;
ages are whole-second differences (no naive date arithmetic).
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from bisect import bisect_right
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, NoReturn

from tree_options.desk import hindsight
from tree_options.desk import intraday_action_graph as iag
from tree_options.desk.cost import (
    TRADEABLE_SYMBOLS,
    CostProvenance,
    Leg,
    LegRef,
    SpreadCostModel,
    UnpricedCostError,
)
from tree_options.desk.sessions import calendar_days_between
from tree_options.trex.clock import session_calendar

TABLE_SCHEMA = "desk-outcome-table/1"
EXIT_MODES = ("intraday", "eod", "hold:1", "hold:3", "hold:5", "hold:10", "expiry")
STATUSES = ("closed", "marked_at_end", "no_fill", "no_price")
STRUCTURES = ("put_credit", "put_debit", "call_credit", "call_debit")
BULLISH = frozenset({"put_credit", "call_debit"})
AGE_S = 15 * 60  # replay's freshness / entry-delay limit (seconds)
SPOT_AGE_S = 30 * 60  # parity prints may be up to 30 minutes old
_INF = Decimal("Infinity")
_ZERO = Decimal(0)

_Points = list[tuple[datetime, Decimal]]


def direction(structure: str) -> str:
    """put_credit / call_debit profit from a rise; the other two from a fall."""
    if structure not in STRUCTURES:
        raise ValueError(f"unknown structure {structure!r}")
    return "bullish" if structure in BULLISH else "bearish"


@dataclass(frozen=True)
class CostModel:
    """Round-trip trading cost of a defined-risk spread, in dollars.

    Every leg fills twice (open and close); each fill pays the commission
    and crosses half the quoted spread on ``multiplier`` shares."""

    commission_per_leg: Decimal = Decimal("0.65")
    half_spread_per_share: Decimal = Decimal("0.03")
    multiplier: int = 100

    def __post_init__(self) -> None:
        if self.commission_per_leg < 0 or self.half_spread_per_share < 0 or self.multiplier < 1:
            raise ValueError("costs must be non-negative and the multiplier positive")

    def round_trip(self, legs: int = 2) -> Decimal:
        fills = 2 * legs
        return (
            fills * self.half_spread_per_share * self.multiplier + fills * self.commission_per_leg
        )


@dataclass(frozen=True)
class OutcomeIndex:
    """One prepared parse of a bundle, reused for every outcome (parse once)."""

    bars: dict[str, _Points]
    contracts: dict[str, iag.Contract]
    sessions: tuple[date, ...]
    #: every scheduled decision clock of the window: (session, clock ET, UTC instant)
    timeline: tuple[tuple[date, str, datetime], ...]
    slots: dict[tuple[date, str], int]
    last_slot: dict[date, int]
    session_pos: dict[date, int]
    underlyings: tuple[str, ...]
    #: (underlying, expiry, strike) -> (call ticker, put ticker)
    parity_pairs: dict[tuple[str, date, Decimal], tuple[str, str]]
    #: underlying -> {date: implied-vol index close} (the bundle's
    #: ``iv_context``; empty for a bundle without one)
    iv: dict[str, dict[date, Decimal]] = field(default_factory=dict)
    _boards: dict[int, list[dict[str, Any]]] = field(
        default_factory=dict, repr=False, compare=False
    )
    _spot: dict[str, dict[date, Decimal]] = field(default_factory=dict, repr=False, compare=False)


def prepare_index(raw: Mapping[str, Any], sessions: list[date] | None = None) -> OutcomeIndex:
    """Parse and verify the bundle once (iag's reader); ``sessions`` defaults
    to every session the bundle covers."""
    bars, contracts = hindsight.parse_bundle(raw)
    window = hindsight.all_sessions(raw) if sessions is None else list(sessions)
    if not window or window != sorted(set(window)):
        raise ValueError("sessions must be non-empty, sorted and unique")
    timeline: list[tuple[date, str, datetime]] = []
    slots: dict[tuple[date, str], int] = {}
    last_slot: dict[date, int] = {}
    for day in window:
        for clock in iag.schedule_for(day):
            slots[(day, clock)] = len(timeline)
            timeline.append((day, clock, iag._instant(day, clock)))
        last_slot[day] = len(timeline) - 1
    rights: dict[tuple[str, date, Decimal], dict[str, str]] = {}
    for ticker, contract in contracts.items():
        key = (contract.underlying, contract.expiry, contract.strike)
        rights.setdefault(key, {})[contract.right] = ticker
    pairs = {
        key: (legs["C"], legs["P"])
        for key, legs in sorted(rights.items())
        if "C" in legs and "P" in legs
    }
    return OutcomeIndex(
        bars=bars,
        contracts=contracts,
        sessions=tuple(window),
        timeline=tuple(timeline),
        slots=slots,
        last_slot=last_slot,
        session_pos={day: i for i, day in enumerate(window)},
        underlyings=tuple(sorted({c.underlying for c in contracts.values()})),
        parity_pairs=pairs,
        iv=_iv_context(raw),
    )


def _iv_context(raw: Mapping[str, Any]) -> dict[str, dict[date, Decimal]]:
    """The bundle's implied-vol index closes {underlying: {date: close}}."""
    context = raw.get("iv_context")
    if context is None:
        return {}
    closes = context.get("closes") if isinstance(context, Mapping) else None
    if not isinstance(closes, Mapping):
        raise ValueError("iv_context must carry closes {underlying: {date: close}}")
    parsed: dict[str, dict[date, Decimal]] = {}
    for underlying, series in closes.items():
        values = {date.fromisoformat(day): Decimal(str(close)) for day, close in series.items()}
        if any(not v.is_finite() or v <= 0 for v in values.values()):
            raise ValueError(f"invalid iv close for {underlying}")
        parsed[str(underlying)] = values
    return parsed


def _slot(index: OutcomeIndex, day: date, clock: str) -> int:
    slot = index.slots.get((day, clock))
    if slot is None:
        raise ValueError(f"{day} {clock} is not a scheduled decision clock of the window")
    return slot


def board_candidates(index: OutcomeIndex, day: date, clock: str) -> list[dict[str, Any]]:
    """The as-of board (iag's candidates, memoized per clock)."""
    slot = _slot(index, day, clock)
    if slot not in index._boards:
        index._boards[slot] = iag._candidates(
            index.contracts, index.bars, index.timeline[slot][2], AGE_S
        )
    return index._boards[slot]


# ------------------------------------------------------------------ fills and marks


@dataclass(frozen=True)
class _Entry:
    debit: Decimal
    max_loss: Decimal
    max_gain: Decimal
    at: datetime


def _fresh(stamp: datetime, at: datetime) -> bool:
    """iag._latest's freshness rule: same ET session day, at most AGE_S old."""
    return (
        stamp.astimezone(iag.ET).date() == at.astimezone(iag.ET).date()
        and (at - stamp).total_seconds() <= AGE_S
    )


def _enter(
    index: OutcomeIndex, slot: int, candidate: Mapping[str, Any], sync_s: int | None
) -> _Entry | str:
    """The entry fill (replay's rule, plus the optional leg sync) or the
    no-fill reason."""
    now = index.timeline[slot][2]
    long_points = index.bars[candidate["long"]]
    short_points = index.bars[candidate["short"]]
    long_fill = iag._next(long_points, now, AGE_S)
    short_fill = iag._next(short_points, now, AGE_S)
    if long_fill is None or short_fill is None:
        return "missing_later_entry_bars"
    i = bisect_right(long_points, (now, _INF))
    j = bisect_right(short_points, (now, _INF))
    if sync_s is not None:
        # walk forward to the first synced pair; advancing the EARLIER print
        # is exhaustive (a discarded print cannot pair with anything later)
        while True:
            if i >= len(long_points) or j >= len(short_points):
                return "legs_out_of_sync"
            long_stamp, short_stamp = long_points[i][0], short_points[j][0]
            if (long_stamp - now).total_seconds() > AGE_S or (
                short_stamp - now
            ).total_seconds() > AGE_S:
                return "legs_out_of_sync"
            gap = (long_stamp - short_stamp).total_seconds()
            if abs(gap) <= sync_s:
                long_fill, short_fill = long_points[i][1], short_points[j][1]
                break
            if gap < 0:
                i += 1
            else:
                j += 1
    entry_debit = long_fill - short_fill
    width = Decimal(candidate["width"])
    debit = candidate["structure"].endswith("debit")
    max_loss = (entry_debit if debit else width + entry_debit) * 100
    max_gain = ((width - entry_debit) if debit else -entry_debit) * 100
    if max_loss <= 0 or max_loss > 300 or max_gain <= 0:
        return "entry_risk_cap"  # replay refuses this entry
    return _Entry(
        debit=entry_debit,
        max_loss=max_loss,
        max_gain=max_gain,
        at=max(long_points[i][0], short_points[j][0]),
    )


def _mark(
    index: OutcomeIndex, candidate: Mapping[str, Any], slot: int, sync_s: int | None
) -> Decimal | None:
    """The spread mark (long - short) at a clock, or None when it has none."""
    at = index.timeline[slot][2]
    long_points = index.bars[candidate["long"]]
    short_points = index.bars[candidate["short"]]
    if sync_s is None:
        long_mark = iag._latest(long_points, at, AGE_S)
        short_mark = iag._latest(short_points, at, AGE_S)
        return None if long_mark is None or short_mark is None else long_mark - short_mark
    # the most recent synced pair inside the freshness window: step the LATER
    # print back (a stale print only gets staler further back)
    i = bisect_right(long_points, (at, _INF)) - 1
    j = bisect_right(short_points, (at, _INF)) - 1
    while i >= 0 and j >= 0:
        (long_stamp, long_price), (short_stamp, short_price) = long_points[i], short_points[j]
        if not (_fresh(long_stamp, at) and _fresh(short_stamp, at)):
            return None
        gap = (long_stamp - short_stamp).total_seconds()
        if abs(gap) <= sync_s:
            return long_price - short_price
        if gap > 0:
            i -= 1
        else:
            j -= 1
    return None


def _backward(
    index: OutcomeIndex, candidate: Mapping[str, Any], sync_s: int | None, high: int, low: int
) -> tuple[int, Decimal] | None:
    """The latest clock in (low, high] with a mark."""
    for slot in range(high, low, -1):
        mark = _mark(index, candidate, slot, sync_s)
        if mark is not None:
            return slot, mark
    return None


_Exit = tuple[int | None, Decimal | None, str, str]  # slot, mark, status, reason


def _expiry_rule(
    index: OutcomeIndex,
    slot: int,
    candidate: Mapping[str, Any],
    sync_s: int | None,
    closed_reason: str,
    end_reason: str,
) -> _Exit:
    """The last mark on/before expiry; past the data end, the last mark."""
    expiry = date.fromisoformat(candidate["expiry"])
    if expiry > index.sessions[-1]:
        found = _backward(index, candidate, sync_s, len(index.timeline) - 1, slot)
        if found is None:
            return None, None, "marked_at_end", "no_mark_after_entry"
        return found[0], found[1], "marked_at_end", end_reason
    last_before = index.sessions[bisect_right(index.sessions, expiry) - 1]
    found = _backward(index, candidate, sync_s, index.last_slot[last_before], slot)
    if found is None:
        return None, None, "marked_at_end", "no_mark_after_entry"
    return found[0], found[1], "closed", closed_reason


def _exit(
    index: OutcomeIndex, slot: int, candidate: Mapping[str, Any], sync_s: int | None, mode: str
) -> _Exit:
    day = index.timeline[slot][0]
    expiry = date.fromisoformat(candidate["expiry"])
    if mode == "expiry":
        return _expiry_rule(
            index, slot, candidate, sync_s, "expiry_last_mark", "target_past_data_end"
        )
    if mode == "intraday":
        start, reason = slot + 1, "next_clock_mark"
    elif mode == "eod":
        found = _backward(index, candidate, sync_s, index.last_slot[day], slot)
        if found is not None:
            return found[0], found[1], "closed", "eod_last_mark"
        start, reason = index.last_slot[day] + 1, "eod_next_session_mark"
    else:
        target = index.session_pos[day] + int(mode.split(":", 1)[1])
        if target >= len(index.sessions):
            return _expiry_rule(
                index, slot, candidate, sync_s, "capped_at_expiry", "target_past_data_end"
            )
        start, reason = index.last_slot[index.sessions[target]], "hold_mark"
    for later in range(start, len(index.timeline)):
        if index.timeline[later][0] > expiry:
            break  # a target (or a walk) past the expiry resolves at the expiry
        mark = _mark(index, candidate, later, sync_s)
        if mark is not None:
            return later, mark, "closed", reason
    # the walk passed the expiry or the data end without a mark
    return _expiry_rule(index, slot, candidate, sync_s, "capped_at_expiry", "no_mark_after_target")


def _no_fill(reason: str) -> dict[str, Any]:
    return {
        "gross": None,
        "net": None,
        "entry_at": None,
        "exit_at": None,
        "hold_minutes": None,
        "status": "no_fill",
        "exit_reason": reason,
    }


def _cost_legs(candidate: Mapping[str, Any], decision_at: datetime) -> tuple[Leg, ...]:
    """Bind both package legs to explicit, available delta inputs.

    Minute closes do not supply delta or quotes. No delta reconstruction or
    EOD-source inference occurs here. Source timestamps describe delta inputs,
    separately from the EOD-derived cost calibration. DTE uses the actual
    package expiry and decision session, rather than a copied calibration DTE.
    """
    reference = LegRef(str(candidate.get("underlying", "")), None, 0)

    def refuse(reason: str) -> NoReturn:
        raise UnpricedCostError(reason, reference)

    supplied = candidate.get("cost_legs")
    if supplied is None:
        refuse("delta_unavailable")
    if (
        not isinstance(supplied, Sequence)
        or isinstance(supplied, (str, bytes))
        or len(supplied) != 2
        or not all(isinstance(row, Mapping) for row in supplied)
    ):
        refuse("incomplete_package")
    assert isinstance(supplied, Sequence)
    identities = (candidate.get("long"), candidate.get("short"))
    if not all(isinstance(ticker, str) and ticker for ticker in identities):
        refuse("incomplete_package")
    expected = (str(identities[0]), str(identities[1]))
    if (
        len(set(expected)) != 2
        or not all(isinstance(row.get("ticker"), str) for row in supplied)
        or {row["ticker"] for row in supplied} != set(expected)
    ):
        refuse("incomplete_package")
    if (
        not isinstance(candidate.get("underlying"), str)
        or candidate["underlying"] not in TRADEABLE_SYMBOLS
    ):
        refuse("unknown_symbol")
    try:
        expiry = date.fromisoformat(candidate["expiry"])
        dte = int(
            calendar_days_between(
                decision_at.astimezone(iag.ET).date().isoformat(), expiry.isoformat()
            )
        )
        if any(
            iag.parse_contract(ticker).expiry != expiry
            or iag.parse_contract(ticker).underlying != candidate.get("underlying")
            for ticker in expected
        ):
            refuse("incomplete_package")
    except (TypeError, ValueError, KeyError):
        refuse("incomplete_package")
    legs = []
    by_ticker = {row["ticker"]: row for row in supplied}
    for ticker in expected:
        row = by_ticker[ticker]
        if type(row.get("dte")) is not int or row["dte"] != dte:
            refuse("inconsistent_dte")
        if row.get("symbol") != candidate.get("underlying"):
            refuse("incomplete_package")
        try:
            event = datetime.fromisoformat(row["source_timestamp_et"])
            available = datetime.fromisoformat(row["available_at"])
            session = date.fromisoformat(row["source_session"])
            if (
                event.tzinfo is None
                or available.tzinfo is None
                or event.utcoffset() is None
                or available.utcoffset() is None
                or event.astimezone(iag.ET).date() != session
                or available < event
                or type(row["is_eod_snapshot"]) is not bool
            ):
                refuse("invalid_provenance")
            if event > decision_at or available > decision_at:
                refuse("future_cost_input")
        except UnpricedCostError:
            raise
        except (TypeError, ValueError, KeyError):
            refuse("invalid_provenance")
        try:
            delta = None if row.get("abs_delta") is None else Decimal(str(row["abs_delta"]))
        except ArithmeticError:
            refuse("invalid_delta")
        legs.append(
            Leg(
                row["symbol"],
                delta,
                dte,
                row["source_session"],
                row["source_timestamp_et"],
                row["is_eod_snapshot"],
            )
        )
    return tuple(legs)


def _no_price(error: UnpricedCostError) -> dict[str, Any]:
    return {
        **_no_fill(error.reason),
        "status": "no_price",
        "pricing_status": "NO_PRICE",
        "pricing_reason": error.reason,
        "pricing_key": error.key.to_json(),
        "cost_model": "derived-spread/1",
        "cost_provenance": CostProvenance.measured_corpus().as_dict(),
    }


def _evaluate(
    index: OutcomeIndex,
    slot: int,
    candidate: Mapping[str, Any],
    modes: Iterable[str],
    costs: CostModel | SpreadCostModel | None,
    sync_s: int | None,
) -> dict[str, dict[str, Any]]:
    """Every requested mode's outcome from ONE entry (shared across modes)."""
    entry = _enter(index, slot, candidate, sync_s)
    if isinstance(entry, str):
        return {mode: _no_fill(entry) for mode in modes}
    metadata: dict[str, Any] = {}
    cost: Decimal | None
    if isinstance(costs, SpreadCostModel):
        try:
            quote = costs.price(_cost_legs(candidate, index.timeline[slot][2]))
        except UnpricedCostError as error:
            return {mode: _no_price(error) for mode in modes}
        cost = quote.total_round_trip
        metadata = {
            "pricing_status": "PRICED_SIMULATION",
            "cost_model": "derived-spread/1",
            "cost_provenance": CostProvenance.measured_corpus().as_dict(),
            "modeled_round_trip_cost": str(cost),
            "cost_quote": quote.to_json(),
        }
    else:
        cost = costs.round_trip() if costs is not None else None
    answer: dict[str, dict[str, Any]] = {}
    for mode in modes:
        exit_slot, mark, status, reason = _exit(index, slot, candidate, sync_s, mode)
        if exit_slot is None or mark is None:
            mark, exit_at = entry.debit, entry.at  # valued at its own entry
        else:
            exit_at = index.timeline[exit_slot][2]
        pnl = (mark - entry.debit) * 100
        gross = min(entry.max_gain, max(-entry.max_loss, pnl))
        answer[mode] = {
            **metadata,
            "gross": gross,
            "net": gross if cost is None else gross - cost,
            "entry_at": entry.at.isoformat(),
            "exit_at": exit_at.isoformat(),
            "hold_minutes": int((exit_at - entry.at).total_seconds() // 60),
            "status": status,
            "exit_reason": reason,
        }
    return answer


def _sync_seconds(leg_sync_minutes: int | None) -> int | None:
    if leg_sync_minutes is None:
        return None
    if (
        isinstance(leg_sync_minutes, bool)
        or not isinstance(leg_sync_minutes, int)
        or leg_sync_minutes < 0
    ):
        raise ValueError("leg_sync_minutes must be None or a non-negative integer")
    return leg_sync_minutes * 60


def _check_modes(modes: Iterable[str]) -> tuple[str, ...]:
    chosen = tuple(modes)
    unknown = [mode for mode in chosen if mode not in EXIT_MODES]
    if unknown or not chosen:
        raise ValueError(f"unknown exit modes {unknown}; choose from {EXIT_MODES}")
    return chosen


def candidate_outcome(
    index: OutcomeIndex,
    day: date,
    clock: str,
    candidate_id: str,
    *,
    exit_mode: str = "intraday",
    costs: CostModel | SpreadCostModel | None = None,
    leg_sync_minutes: int | None = None,
) -> dict[str, Any] | None:
    """The outcome of entering ``candidate_id`` on the (day, clock) board.

    Returns {gross, net, entry_at, exit_at, hold_minutes, status,
    exit_reason}; ``None`` when the id is not on that as-of board (an
    unknown or stale id, as replay treats it)."""
    (mode,) = _check_modes((exit_mode,))
    sync_s = _sync_seconds(leg_sync_minutes)
    slot = _slot(index, day, clock)
    candidate = next(
        (c for c in board_candidates(index, day, clock) if c["id"] == candidate_id), None
    )
    if candidate is None:
        return None
    return _evaluate(index, slot, candidate, (mode,), costs, sync_s)[mode]


# ------------------------------------------------------------------ spot


def _parity_spot(index: OutcomeIndex, at: datetime) -> dict[str, Decimal]:
    """Spot per underlying from prints at or before ``at``: C - P + K per
    strike of the nearest unexpired expiry with fresh prints, median."""
    today = at.astimezone(iag.ET).date()
    by: dict[str, dict[date, list[Decimal]]] = {}
    for (underlying, expiry, strike), (call, put) in index.parity_pairs.items():
        if (
            expiry < today
            or not iag.is_listed(index.contracts, call, today)
            or not iag.is_listed(index.contracts, put, today)
        ):
            continue
        call_price = iag._latest(index.bars[call], at, SPOT_AGE_S)
        put_price = iag._latest(index.bars[put], at, SPOT_AGE_S)
        if call_price is None or put_price is None:
            continue
        by.setdefault(underlying, {}).setdefault(expiry, []).append(call_price - put_price + strike)
    return {underlying: statistics.median(per[min(per)]) for underlying, per in sorted(by.items())}


def spot_asof(index: OutcomeIndex, at: datetime) -> dict[str, Decimal]:
    """As-of spot estimate per underlying at a decision instant (only prints
    at or before ``at``); underlyings without fresh parity pairs are absent."""
    if at.tzinfo is None:
        raise ValueError("an aware instant is required")
    return _parity_spot(index, at)


def spot_series(index: OutcomeIndex) -> dict[str, dict[date, Decimal]]:
    """Per-session closing spot estimate {underlying: {session: spot}}
    (computed once per index; each value reads only prints up to its own
    session close)."""
    if not index._spot:
        calendar = session_calendar()
        series: dict[str, dict[date, Decimal]] = {u: {} for u in index.underlyings}
        for day in index.sessions:
            for underlying, spot in _parity_spot(index, calendar.session_close(day)).items():
                series[underlying][day] = spot
        index._spot.update(series)
    return {underlying: dict(values) for underlying, values in index._spot.items()}


# ------------------------------------------------------------------ table


def outcome_table(
    index: OutcomeIndex,
    *,
    modes: Iterable[str] = EXIT_MODES,
    costs: CostModel | SpreadCostModel | None = None,
    leg_sync_minutes: int | None = None,
) -> Iterator[dict[str, Any]]:
    """One JSON-ready row per (board, candidate, exit mode), every board of
    the window in time order."""
    chosen = _check_modes(modes)
    sync_s = _sync_seconds(leg_sync_minutes)
    for slot, (day, clock, _at) in enumerate(index.timeline):
        snapshot = f"s:{day.isoformat()}T{clock}"
        for candidate in board_candidates(index, day, clock):
            results = _evaluate(index, slot, candidate, chosen, costs, sync_s)
            for mode in chosen:
                outcome = results[mode]
                yield {
                    "snapshot": snapshot,
                    "candidate_id": candidate["id"],
                    "exit_mode": mode,
                    "structure": candidate["structure"],
                    "direction": direction(candidate["structure"]),
                    "underlying": candidate["underlying"],
                    **{
                        key: (str(value) if isinstance(value, Decimal) else value)
                        for key, value in outcome.items()
                    },
                }


class _Summary:
    """Streaming per-mode summary of outcome-table rows."""

    def __init__(self) -> None:
        self.rows = 0
        self.modes: dict[str, dict[str, Any]] = {}

    def add(self, row: Mapping[str, Any]) -> None:
        self.rows += 1
        mode = self.modes.setdefault(
            row["exit_mode"],
            {
                "rows": 0,
                "status": Counter(),
                "reasons": Counter(),
                "gross": [],
                "net": [],
                "closed_net": [],
                "by_direction": {"bullish": [], "bearish": []},
            },
        )
        mode["rows"] += 1
        mode["status"][row["status"]] += 1
        mode["reasons"][row["exit_reason"]] += 1
        if row["status"] in {"no_fill", "no_price"}:
            return
        gross, net = Decimal(str(row["gross"])), Decimal(str(row["net"]))
        mode["gross"].append(gross)
        mode["net"].append(net)
        if row["status"] == "closed":
            mode["closed_net"].append(net)
        mode["by_direction"][row["direction"]].append(net)

    def result(self) -> dict[str, Any]:
        def mean(values: list[Decimal]) -> float | None:
            return round(float(sum(values, _ZERO) / len(values)), 3) if values else None

        modes = {}
        for name in [m for m in EXIT_MODES if m in self.modes]:
            mode = self.modes[name]
            modes[name] = {
                "rows": mode["rows"],
                "status": dict(mode["status"]),
                "reasons": dict(sorted(mode["reasons"].items())),
                "no_fill_share": round(mode["status"]["no_fill"] / mode["rows"], 4),
                "no_price_share": round(mode["status"]["no_price"] / mode["rows"], 4),
                "filled": len(mode["gross"]),
                "mean_gross": mean(mode["gross"]),
                "mean_net": mean(mode["net"]),
                "mean_net_closed_only": mean(mode["closed_net"]),
                "by_direction": {
                    side: {"filled": len(values), "mean_net": mean(values)}
                    for side, values in mode["by_direction"].items()
                },
            }
        return {"rows": self.rows, "modes": modes}


def summarize(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Per exit mode: rows, statuses, no-fill share, mean gross/net over filled
    rows (closed + marked_at_end; closed-only net too) and net by direction."""
    summary = _Summary()
    for row in rows:
        summary.add(row)
    return summary.result()


# ------------------------------------------------------------------ CLI


def _cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tree_options.desk outcome-table",
        description="Every candidate on every board x every exit mode, gross and net.",
    )
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path, help="rows (.jsonl)")
    parser.add_argument(
        "--sync", default="2", help="leg sync minutes at entry and marks (default 2; 'off' = v1)"
    )
    parser.add_argument("--half-spread", default="0.03", help="per share, per leg fill")
    parser.add_argument("--commission", default="0.65", help="per leg fill")
    parser.add_argument(
        "--cost-model",
        choices=("flat", "derived"),
        default="flat",
        help="flat compatibility control or opt-in EOD-derived sensitivity model",
    )
    args = parser.parse_args(argv)
    try:
        sync = None if args.sync == "off" else int(args.sync)
        if sync is not None and sync < 0:
            raise ValueError("--sync must be >= 0 or 'off'")
        costs: CostModel | SpreadCostModel
        if args.cost_model == "derived":
            costs = SpreadCostModel(commission_per_leg=Decimal(args.commission))
        else:
            costs = CostModel(
                commission_per_leg=Decimal(args.commission),
                half_spread_per_share=Decimal(args.half_spread),
            )
        raw = json.loads(args.bundle.read_bytes())
        index = prepare_index(raw)
    except (ValueError, ArithmeticError, OSError, KeyError) as error:
        print(f"refused: {error}", file=sys.stderr)
        return 2
    summary = _Summary()
    boards = candidates = 0
    partial = args.out.with_name(args.out.name + ".partial")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with partial.open("w", encoding="utf-8") as stream:
        for day, clock, _at in index.timeline:
            boards += 1
            candidates += len(board_candidates(index, day, clock))
        for row in outcome_table(index, costs=costs, leg_sync_minutes=sync):
            stream.write(json.dumps(row, separators=(",", ":")) + "\n")
            summary.add(row)
    partial.replace(args.out)
    summary_result = summary.result()
    incomplete = any(mode["status"].get("no_price", 0) for mode in summary_result["modes"].values())
    cost_facts: dict[str, Any] = {
        "cost_model": "derived-spread/1" if isinstance(costs, SpreadCostModel) else "flat-spread/1",
        "commission_per_leg": str(costs.commission_per_leg),
        "multiplier": costs.multiplier,
        "exact_execution_economics": False,
        "execution_authorized": False,
    }
    if isinstance(costs, SpreadCostModel):
        cost_facts["cost_provenance"] = CostProvenance.measured_corpus().as_dict()
    else:
        cost_facts["half_spread_per_share"] = str(costs.half_spread_per_share)
    document = {
        "schema": TABLE_SCHEMA,
        "bundle": str(args.bundle),
        "out": str(args.out),
        "sessions": [
            index.sessions[0].isoformat(),
            index.sessions[-1].isoformat(),
            len(index.sessions),
        ],
        "boards": boards,
        "candidates": candidates,
        "exit_modes": list(EXIT_MODES),
        "sync_minutes": sync,
        "round_trip_cost": None if isinstance(costs, SpreadCostModel) else str(costs.round_trip()),
        "costs": cost_facts,
        "assessment": "DATA_GATED" if incomplete else "SIMULATED_RESEARCH",
        **summary_result,
    }
    Path(f"{args.out}.summary.json").write_text(json.dumps(document, indent=2))
    print(json.dumps(document, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
