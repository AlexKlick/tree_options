"""Historical, trade-bar-only action graph. No broker or executable quote authority.

Each decision uses observations stamped no later than its scheduled ET instant.
Entry is valued from later observations and is never supplied to a policy.
Missing observations produce omissions, never interpolated prices.
"""

from __future__ import annotations

import hashlib
import re
from bisect import bisect_right
from calendar import monthrange
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from itertools import pairwise
from typing import Any
from zoneinfo import ZoneInfo

from tree_options.trex.clock import session_calendar

ET = ZoneInfo("America/New_York")
SCHEMA = "desk-intraday-action-graph/1"
SCHEDULE = ("10:00", "10:45", "11:30", "12:15", "13:00", "13:45", "14:30", "15:15")
EARLY_SCHEDULE = ("10:00", "10:40", "11:20", "12:00", "12:40")
OPTION = re.compile(r"^O:([A-Z]+)(\d{2})(\d{2})(\d{2})([CP])(\d{8})$")


@dataclass(frozen=True)
class Contract:
    ticker: str
    underlying: str
    expiry: date
    right: str
    strike: Decimal


def parse_contract(ticker: str) -> Contract:
    match = OPTION.fullmatch(ticker)
    if match is None:
        raise ValueError(f"invalid OCC ticker: {ticker}")
    symbol, year, month, day, right, strike = match.groups()
    return Contract(ticker, symbol, date(2000 + int(year), int(month), int(day)),
                    right, Decimal(int(strike)) / Decimal(1000))


def windows(sessions: list[date], *, months: int = 3, stride_sessions: int = 21
            ) -> list[tuple[date, date]]:
    """Rolling calendar-month windows, stepped by a fixed number of sessions."""
    if months != 3 or stride_sessions < 1 or sessions != sorted(set(sessions)):
        raise ValueError("requires sorted unique sessions, three months, positive stride")
    answer = []
    for start in range(0, len(sessions), stride_sessions):
        d = sessions[start]
        year, month = d.year, d.month + months
        year += (month - 1) // 12
        month = (month - 1) % 12 + 1
        end = date(year, month, min(d.day, monthrange(year, month)[1]))
        next_session = next((day for day in session_calendar().sessions() if day > sessions[-1]), None)
        if next_session is not None and next_session < end:
            break
        contained = [day for day in sessions if d <= day < end]
        if len(contained) >= 40:
            answer.append((d, contained[-1]))
    return answer


def _instant(day: date, clock: str) -> datetime:
    hour, minute = map(int, clock.split(":"))
    return datetime.combine(day, time(hour, minute), ET).astimezone(UTC)


def schedule_for(day: date) -> tuple[str, ...]:
    close = session_calendar().session_close(day).astimezone(ET).time()
    return EARLY_SCHEDULE if close <= time(13, 0) else SCHEDULE


def _read_bars(raw: Mapping[str, Any]) -> dict[str, list[tuple[datetime, Decimal]]]:
    if raw.get("schema") != "desk-option-minute-bars/1":
        raise ValueError("minute-bar bundle schema required")
    result = {}
    for ticker, body in raw.get("contracts", {}).items():
        parse_contract(ticker)
        if body.get("timespan") != "minute" or body.get("ticker") != ticker:
            raise ValueError(f"unverified minute identity: {ticker}")
        points = []
        for bar in body.get("results", []):
            if not isinstance(bar.get("t"), int) or bar.get("v", 0) <= 0:
                raise ValueError(f"invalid minute bar: {ticker}")
            stamp = datetime.fromtimestamp(bar["t"] / 1000, UTC)
            close = Decimal(str(bar["c"]))
            if not close.is_finite() or close <= 0:
                raise ValueError(f"invalid price: {ticker}")
            points.append((stamp, close))
        if any(left[0] >= right[0] for left, right in pairwise(points)):
            raise ValueError(f"duplicate or unordered bars: {ticker}")
        result[ticker] = points
    return result


def _latest(points: list[tuple[datetime, Decimal]], now: datetime,
            max_age: timedelta) -> Decimal | None:
    index = bisect_right(points, (now, Decimal("Infinity"))) - 1
    if index >= 0:
        stamp, price = points[index]
        if stamp.astimezone(ET).date() == now.astimezone(ET).date() and now-stamp <= max_age:
            return price
    return None


def _next(points: list[tuple[datetime, Decimal]], now: datetime,
          max_delay: timedelta) -> Decimal | None:
    index = bisect_right(points, (now, Decimal("Infinity")))
    if index < len(points):
        stamp, price = points[index]
        return price if stamp-now <= max_delay else None
    return None


def _recent_option_move(points: list[tuple[datetime, Decimal]], now: datetime) -> str | None:
    """Mean absolute return across recent observed trades, not implied vol."""
    end = bisect_right(points, (now, Decimal("Infinity")))
    sample = [(t, p) for t, p in points[max(0, end-21):end]
              if t >= now-timedelta(days=5)]
    if len(sample) < 6:
        return None
    moves = [abs(new/old-1) for (_, old), (_, new) in pairwise(sample)]
    return str((sum(moves)/len(moves)).quantize(Decimal("0.0001")))


def _candidates(contracts: dict[str, Contract], bars: dict[str, list[tuple[datetime, Decimal]]],
                now: datetime, max_age: timedelta) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, date, str], list[Contract]] = {}
    for contract in contracts.values():
        if 7 <= (contract.expiry - now.astimezone(ET).date()).days <= 60:
            grouped.setdefault((contract.underlying, contract.expiry, contract.right), []).append(contract)
    candidates = []
    for group in sorted(grouped):
        chain = sorted(grouped[group], key=lambda c: c.strike)
        for low, high in pairwise(chain):
            low_price = _latest(bars[low.ticker], now, max_age)
            high_price = _latest(bars[high.ticker], now, max_age)
            if low_price is None or high_price is None:
                continue
            width = high.strike - low.strike
            if width <= 0:
                continue
            # Both orientations are possible; bar closes are valuation proxies.
            for label, long, short, debit in (
                ("call_debit" if low.right == "C" else "put_credit", low, high, low_price-high_price),
                ("call_credit" if low.right == "C" else "put_debit", high, low, high_price-low_price),
            ):
                if label.endswith("debit"):
                    premium = debit
                    max_loss = premium * 100
                else:
                    premium = -debit
                    max_loss = (width-premium) * 100
                if premium <= 0 or premium >= width or max_loss <= 0 or max_loss > 300:
                    continue
                max_gain = (width-premium)*100 if label.endswith("debit") else premium*100
                key = hashlib.sha256(f"{now.isoformat()}:{label}:{long.ticker}:{short.ticker}".encode()).hexdigest()[:16]
                candidates.append({"id": key, "structure": label, "underlying": low.underlying,
                                   "expiry": low.expiry.isoformat(), "long": long.ticker,
                                   "short": short.ticker, "width": str(width),
                                   "observed_premium": str(premium), "max_loss_proxy": str(max_loss),
                                   "max_gain_proxy": str(max_gain),
                                   "reward_to_risk_proxy": str((max_gain/max_loss).quantize(Decimal("0.01"))),
                                   "long_recent_trade_move": _recent_option_move(bars[long.ticker], now),
                                   "short_recent_trade_move": _recent_option_move(bars[short.ticker], now),
                                   "data_kind": "last-traded-minute-close"})
    return candidates


def decision_packet(raw: Mapping[str, Any], day: date, clock: str) -> dict[str, Any]:
    """One outcome-free decision packet; never includes later bar values."""
    if clock not in schedule_for(day):
        raise ValueError("clock is not a scheduled decision point")
    bars = _read_bars(raw)
    now = _instant(day, clock)
    contracts = {ticker: parse_contract(ticker) for ticker in bars}
    candidates = _candidates(contracts, bars, now, timedelta(minutes=15))
    return {"schema": "desk-intraday-decision/1", "snapshot_id": f"s:{day}T{clock}",
            "as_of": now.isoformat(), "candidates": candidates,
            "allowed_actions": ["skip", "enter_one_candidate"],
            "capital_profile": {"intended_capital": "5000", "per_trade_loss_cap": "300",
                                "combined_open_loss_cap": "1500", "daily_realized_loss_cap": "300"},
            "data_kind": "last-traded-minute-close", "execution_authorized": False}


def replay(raw: Mapping[str, Any], sessions: list[date], decisions: Mapping[str, str | None] | None = None,
           *, max_age_minutes: int = 15, policy: str = "no_trade") -> dict[str, Any]:
    """Build potential and chosen action graph for one bounded window.

    Decisions map snapshot IDs to candidate IDs or null. Fixed policy baselines
    are explicit; none is described as a predictive or winning strategy.
    """
    if max_age_minutes < 1 or max_age_minutes > 30:
        raise ValueError("invalid freshness limit")
    if policy not in {"no_trade", "put_credit", "call_credit", "put_debit", "call_debit"}:
        raise ValueError("unknown policy")
    if decisions is not None and policy != "no_trade":
        raise ValueError("decisions and policy cannot both select actions")
    bars = _read_bars(raw)
    contracts = {ticker: parse_contract(ticker) for ticker in bars}
    if sessions != sorted(set(sessions)):
        raise ValueError("sessions must be sorted and unique")
    age = timedelta(minutes=max_age_minutes)
    external_decisions = decisions is not None
    decisions = {} if decisions is None else decisions
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    receipts: list[dict[str, Any]] = []
    open_trades: list[dict[str, Any]] = []
    closed_capital = Decimal(5000)
    minimum_closed_capital = closed_capital
    daily_loss = Decimal(0)
    peak_reserved = Decimal(0)
    modeled_wins = 0
    modeled_losses = 0
    last_day = None
    seen_decisions: set[str] = set()
    scheduled_snapshots = 0
    for day in sessions:
        if day != last_day:
            daily_loss = Decimal(0)
            last_day = day
        for clock in schedule_for(day):
            scheduled_snapshots += 1
            now = _instant(day, clock)
            sid = f"s:{day.isoformat()}T{clock}"
            nodes.append({"id": sid, "kind": "snapshot", "as_of": now.isoformat(),
                          "session": day.isoformat(), "clock_et": clock})
            remaining = []
            for open_trade in open_trades:
                long_mark = _latest(bars[open_trade["long"]], now, age)
                short_mark = _latest(bars[open_trade["short"]], now, age)
                if long_mark is not None and short_mark is not None:
                    pnl = ((long_mark-short_mark) - open_trade["entry_debit"]) * 100
                    pnl = min(open_trade["max_gain"], max(-open_trade["max_loss"], pnl))
                    closed_capital += pnl
                    minimum_closed_capital = min(minimum_closed_capital, closed_capital)
                    modeled_wins += int(pnl > 0)
                    modeled_losses += int(pnl < 0)
                    daily_loss += min(Decimal(0), pnl)
                    close_id = f"x:{sid}:{open_trade['action_id']}"
                    nodes.append({"id": close_id, "kind": "modeled_exit", "pnl": str(pnl),
                                  "capital_after": str(closed_capital), "data_kind": "last-traded-minute-close"})
                    edges.append({"from": open_trade["action_id"], "to": close_id, "kind": "later_mark"})
                else:
                    remaining.append(open_trade)
            open_trades = remaining
            candidates = _candidates(contracts, bars, now, age)
            for candidate in candidates:
                nodes.append({"id": candidate["id"], "kind": "potential_trade", **candidate})
                edges.append({"from": sid, "to": candidate["id"], "kind": "available_as_of"})
            selected = decisions.get(sid)
            if decisions == {} and policy != "no_trade":
                eligible = [c for c in candidates if c["structure"] == policy]
                if eligible:
                    selected = min(eligible, key=lambda c: (Decimal(c["max_loss_proxy"]),
                                                             c["underlying"], c["id"]))["id"]
            if sid in decisions:
                seen_decisions.add(sid)
            action_id = f"a:{sid}"
            chosen = next((c for c in candidates if c["id"] == selected), None)
            reason = "no_selection" if selected is None else "selected"
            if selected is not None and chosen is None:
                reason = "unknown_or_stale_candidate"
            elif chosen is not None and daily_loss <= -300:
                reason = "daily_loss_cap"
            elif chosen is not None and any(
                    t["long"] == chosen["long"] and t["short"] == chosen["short"]
                    for t in open_trades):
                reason = "same_spread_still_open"
            entry_receipt = None
            if reason == "selected" and chosen is not None:
                long_fill = _next(bars[chosen["long"]], now, age)
                short_fill = _next(bars[chosen["short"]], now, age)
                if long_fill is None or short_fill is None:
                    reason = "missing_later_entry_bars"
                else:
                    entry_debit = long_fill - short_fill
                    width = Decimal(chosen["width"])
                    max_loss = (entry_debit if chosen["structure"].endswith("debit")
                                else width + entry_debit) * 100
                    max_gain = ((width-entry_debit) if chosen["structure"].endswith("debit")
                                else -entry_debit) * 100
                    reserved = sum((t["max_loss"] for t in open_trades), Decimal(0))
                    if (max_loss <= 0 or max_loss > 300 or max_gain <= 0
                            or reserved+max_loss > closed_capital):
                        reason = "entry_risk_cap"
                    elif reserved + max_loss > 1500:
                        reason = "combined_open_loss_cap"
                    else:
                        open_trades.append({"long": chosen["long"], "short": chosen["short"],
                                            "entry_debit": entry_debit, "max_loss": max_loss,
                                            "max_gain": max_gain,
                                            "action_id": action_id})
                        peak_reserved = max(peak_reserved, reserved+max_loss)
                        entry_receipt = {"entry_debit_proxy": str(entry_debit),
                                         "max_loss_at_entry_proxy": str(max_loss),
                                         "max_gain_at_entry_proxy": str(max_gain)}
            action = {"id": action_id, "kind": "trade_action", "snapshot": sid,
                      "candidate_id": selected, "decision": "enter" if reason == "selected" else "skip",
                      "reason": reason, "execution_authorized": False}
            if entry_receipt is not None:
                action.update(entry_receipt)
            nodes.append(action)
            edges.append({"from": sid, "to": action_id, "kind": "decides"})
            if reason == "selected" and selected is not None:
                edges.append({"from": selected, "to": action_id, "kind": "chosen_as"})
            receipts.append(action)
    unknown = set(decisions) - seen_decisions
    if unknown:
        raise ValueError(f"decisions outside window: {sorted(unknown)[:3]}")
    return {"schema": SCHEMA, "policy": "external_decisions" if external_decisions else policy,
            "sessions": len(sessions), "scheduled_snapshots": scheduled_snapshots,
            "potential_trades": sum(n["kind"] == "potential_trade" for n in nodes),
            "entered": sum(a["decision"] == "enter" for a in receipts),
            "modeled_wins": modeled_wins, "modeled_losses": modeled_losses,
            "closed_capital_proxy": str(closed_capital), "open_at_end": len(open_trades),
            "minimum_closed_capital_proxy": str(minimum_closed_capital),
            "peak_open_loss_reserved": str(peak_reserved),
            "nodes": nodes, "edges": edges, "actions": receipts,
            "limitations": ["trade bars are not executable bid/ask quotes", "stale or absent bars omit candidates",
                            "first later trade bars are valuation proxies, not fills",
                            "open positions at window end have no final PnL", "no assignment or fees modeled"],
            "execution_authorized": False}
