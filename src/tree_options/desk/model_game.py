"""Blind, source-bound historical selection exercise over a frozen replay.

The agents see early modeled outcomes and later candidate features only.
Scores are descriptive for this tiny selected sample, not a strategy test or
broker authorization. Names and calendar dates are masked in the prompt to
reduce direct historical-event recall; the source artifact remains private to
the scorer.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

SCHEMA = "desk-model-game/1"
CUTOFF = date(2025, 9, 1)
START = date(2024, 9, 3)


def _features(row: dict[str, Any], aliases: dict[str, str], candidate_id: str) -> dict[str, Any]:
    entry = date.fromisoformat(row["entry"])
    exit_day = date.fromisoformat(row["exit"])
    expiry = date.fromisoformat(row["expiry"])
    legs = row["legs"]
    strikes = [Decimal(str(leg["strike"])) for leg in legs]
    return {
        "id": candidate_id, "asset_alias": aliases[row["name"]],
        "signal": row["signal"], "structure": row["structure"],
        "decision_day": (date.fromisoformat(row["decision"]) - START).days,
        "entry_day": (entry - START).days, "exit_day": (exit_day - START).days,
        "entry_dte": (expiry - entry).days,
        "strike_width": str(max(strikes) - min(strikes)) if len(strikes) > 1 else None,
        "right": legs[0]["right"], "max_loss": str(row["max_loss"]),
    }


def prepare(replay: dict[str, Any]) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    if replay.get("schema") != "desk-historical-replay/1":
        raise ValueError("unsupported replay")
    rows = replay.get("rows")
    if not isinstance(rows, list):
        raise ValueError("missing replay rows")
    ordered = sorted(rows, key=lambda row: (
        row["decision"], row["name"], row["signal"], row["structure"]))
    aliases = {name: f"asset-{index:02d}" for index, name in
               enumerate(sorted({row["name"] for row in ordered}), 1)}
    train: list[dict[str, Any]] = []
    blind: list[dict[str, Any]] = []
    hidden: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(ordered, 1):
        candidate_id = f"c{index:03d}"
        features = _features(row, aliases, candidate_id)
        if date.fromisoformat(row["decision"]) < CUTOFF:
            train.append({**features, "modeled_pnl": str(row["pnl"])})
        else:
            blind.append(features)
            hidden[candidate_id] = row
    if not train or not blind:
        raise ValueError("both training and blind periods require rows")
    packet = {
        "schema": SCHEMA,
        "scope": "selected evaluable daily-VWAP rows only; no executable quotes or intraday stop",
        "limits": {"capital": "5000", "max_trade_loss": "300",
                   "max_open_loss": "1500", "max_daily_realized_loss": "300 (not testable here)"},
        "task": "Select blind candidate IDs using training outcomes and displayed features only."
                " Return JSON {selected_ids:[...], policy:string, caveats:[...]}.",
        "training": train, "blind_candidates": blind,
    }
    return packet, hidden


def score(hidden: dict[str, dict[str, Any]], selected_ids: list[str]) -> dict[str, Any]:
    if len(selected_ids) != len(set(selected_ids)) or any(key not in hidden for key in selected_ids):
        raise ValueError("duplicate or unknown candidate ID")
    requested = set(selected_ids)
    active: list[tuple[date, Decimal, Decimal]] = []
    capital = Decimal("5000")
    closed = capital
    minimum_closed = capital
    peak_open = Decimal(0)
    admitted: list[str] = []
    skipped = {"open_cap": 0, "capital": 0}
    for key in sorted(requested, key=lambda item: (
        hidden[item]["entry"], hidden[item]["decision"], item)):
        row = hidden[key]
        entry, exit_day = date.fromisoformat(row["entry"]), date.fromisoformat(row["exit"])
        if exit_day <= entry:
            raise ValueError("invalid holding period")
        loss, pnl = Decimal(str(row["max_loss"])), Decimal(str(row["pnl"]))
        if (not loss.is_finite() or not pnl.is_finite() or loss <= 0
                or loss > 300 or -pnl > loss):
            raise ValueError("invalid modeled risk or outcome")
        still_active = []
        for prior_exit, prior_loss, prior_pnl in active:
            if prior_exit < entry:
                closed += prior_pnl
                minimum_closed = min(minimum_closed, closed)
            else:
                still_active.append((prior_exit, prior_loss, prior_pnl))
        active = still_active
        reserved = sum((item[1] for item in active), Decimal(0))
        if reserved + loss > 1500:
            skipped["open_cap"] += 1
        elif reserved + loss > closed:
            skipped["capital"] += 1
        else:
            active.append((exit_day, loss, pnl))
            admitted.append(key)
            peak_open = max(peak_open, reserved + loss)
    for _, _, pnl in sorted(active):
        closed += pnl
        minimum_closed = min(minimum_closed, closed)
    outcomes = [Decimal(str(hidden[key]["pnl"])) for key in admitted]
    return {
        "requested": len(requested), "admitted": len(admitted), "admitted_ids": admitted,
        "skipped": skipped, "modeled_wins": sum(pnl > 0 for pnl in outcomes),
        "modeled_closed_pnl": str(closed - capital),
        "minimum_closed_capital": str(minimum_closed),
        "peak_open_loss_reserved": str(peak_open),
    }
