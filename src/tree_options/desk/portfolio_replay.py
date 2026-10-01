"""Overlap-aware admission scenario over already modeled historical rows.

This cannot simulate an intraday stop or an IBKR fill. Exit VWAP outcomes are
recognized only after their exit session, so positions exiting on an entry
session still consume risk for that entry. No strategy is selected here: each
signal/structure cell is projected independently from the frozen replay.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any


def _amount(value: Any, label: str, *, positive: bool = False) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise ValueError(f"{label} must be numeric")
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"{label} must be numeric") from exc
    if not result.is_finite() or result < 0 or (positive and result == 0):
        raise ValueError(f"{label} must be finite and {'positive' if positive else 'non-negative'}")
    return result


def simulate(
    replay: dict[str, Any],
    *,
    capital: Decimal = Decimal("5000"),
    max_trade_loss: Decimal = Decimal("300"),
    max_open_loss: Decimal = Decimal("1500"),
) -> dict[str, Any]:
    """Admit modeled rows in time order within each predeclared variant.

    Same-day exit prices have unknown intraday ordering and are not available
    to an entry decision. Reservations release only when exit < new entry.
    """
    if replay.get("schema") != "desk-historical-replay/1":
        raise ValueError("unsupported replay schema")
    capital = _amount(capital, "capital", positive=True)
    max_trade_loss = _amount(max_trade_loss, "max_trade_loss", positive=True)
    max_open_loss = _amount(max_open_loss, "max_open_loss", positive=True)
    if max_open_loss > capital or max_trade_loss > max_open_loss:
        raise ValueError("invalid portfolio risk limits")

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in replay.get("rows", []):
        if not isinstance(row, dict):
            raise ValueError("invalid replay row")
        key = f"{row['signal']}/{row['structure']}"
        if key not in replay.get("by_variant", {}):
            raise ValueError("replay row has undeclared variant")
        grouped[key].append(row)

    variants: dict[str, dict[str, Any]] = {}
    if not isinstance(replay.get("by_variant"), dict) or not isinstance(replay.get("rows"), list):
        raise ValueError("invalid replay summary")
    for key in replay["by_variant"]:
        rows = sorted(
            grouped.get(key, []), key=lambda r: (r["entry"], r["decision"], r["name"], r["expiry"])
        )
        active: list[tuple[date, Decimal, Decimal]] = []
        admitted: list[str] = []
        skipped = {"trade_cap": 0, "open_cap": 0, "capital": 0}
        closed_cash = capital
        low_closed_cash = capital
        peak_reserved = Decimal(0)
        for row in rows:
            entry, exit_day = date.fromisoformat(row["entry"]), date.fromisoformat(row["exit"])
            if exit_day <= entry:
                raise ValueError("exit must follow entry")
            loss = _amount(row["max_loss"], "max_loss", positive=True)
            if isinstance(row["pnl"], bool):
                raise ValueError("pnl must be numeric")
            try:
                pnl = Decimal(str(row["pnl"]))
            except InvalidOperation as exc:
                raise ValueError("pnl must be numeric") from exc
            if not pnl.is_finite():
                raise ValueError("pnl must be finite")
            if -pnl > loss:
                raise ValueError("modeled loss exceeds declared worst-case loss")
            remaining: list[tuple[date, Decimal, Decimal]] = []
            for prior_exit, prior_loss, prior_pnl in active:
                if prior_exit < entry:
                    closed_cash += prior_pnl
                    low_closed_cash = min(low_closed_cash, closed_cash)
                else:
                    remaining.append((prior_exit, prior_loss, prior_pnl))
            active = remaining
            reserved = sum((risk for _, risk, _ in active), Decimal(0))
            if loss > max_trade_loss:
                skipped["trade_cap"] += 1
            elif reserved + loss > max_open_loss:
                skipped["open_cap"] += 1
            elif reserved + loss > closed_cash:
                skipped["capital"] += 1
            else:
                active.append((exit_day, loss, pnl))
                admitted.append(f"{row['decision']}:{row['name']}")
                peak_reserved = max(peak_reserved, reserved + loss)
        for _exit, _loss, pnl in sorted(active):
            closed_cash += pnl
            low_closed_cash = min(low_closed_cash, closed_cash)
        variants[key] = {
            "considered": len(rows),
            "admitted": len(admitted),
            "skipped": skipped,
            "admitted_decision_names": admitted,
            "peak_open_loss_reserved": str(peak_reserved),
            "closed_pnl": str(closed_cash - capital),
            "ending_closed_capital": str(closed_cash),
            "minimum_closed_capital": str(low_closed_cash),
        }
    return {
        "schema": "desk-portfolio-scenario/1",
        "label": "exploratory closed-VWAP scenario by fixed variant",
        "spec": {
            "intended_capital": str(capital),
            "max_trade_loss": str(max_trade_loss),
            "max_open_loss": str(max_open_loss),
        },
        "limitations": [
            "same-day exit proceeds are unavailable to new entries",
            "intraday and daily loss stops cannot be tested from these daily bars",
            "modeled VWAP and assumed haircut are not executable quotes or broker fills",
            "each variant is projected separately; no adaptive selection or combined book",
        ],
        "variants": variants,
    }
