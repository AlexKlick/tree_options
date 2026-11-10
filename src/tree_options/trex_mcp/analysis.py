"""Pure, source-explicit expiration scenarios for defined-risk bearish options.

This is deliberately not an order proposal, signal, quote engine or forecast.
The caller supplies every price. Nothing here can reach a broker or issue a
TREX command. In particular, expiration payoff != mark-to-market P&L today.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

MULTIPLIER = Decimal(100)
SYMBOL = re.compile(r"^[A-Z.]{1,6}$")


class InvalidScenario(ValueError):
    """Client-provided assumptions were not sufficient for a safe calculation."""


def symbol_value(raw: object) -> str:
    if not isinstance(raw, str):
        raise InvalidScenario("symbol must be a string")
    sym = raw.upper()
    if not SYMBOL.fullmatch(sym):
        raise InvalidScenario("symbol must be 1-6 letters or periods")
    return sym


def positive_decimal(raw: object, field: str, *, allow_zero: bool = False) -> Decimal:
    if isinstance(raw, bool) or not isinstance(raw, (str, int, float, Decimal)):
        raise InvalidScenario(f"{field} must be a finite decimal number")
    try:
        result = Decimal(str(raw))
    except InvalidOperation as exc:
        raise InvalidScenario(f"{field} must be a finite decimal number") from exc
    if not result.is_finite() or result < 0 or (not allow_zero and result == 0):
        raise InvalidScenario(f"{field} must be finite and {'nonnegative' if allow_zero else 'positive'}")
    if result > Decimal("1000000"):
        raise InvalidScenario(f"{field} is outside supported bounds")
    return result


def _money(raw: Decimal) -> float:
    return float(raw.quantize(Decimal("0.01")))


def evaluate_bearish_scenario(data: dict[str, Any]) -> dict[str, Any]:
    """Evaluate an explicitly priced long put or put debit vertical at expiry."""
    allowed = {
        "symbol", "expiration", "strategy", "spot", "long_strike",
        "short_strike", "net_debit", "contracts", "target_prices", "max_risk_budget",
    }
    extra = set(data) - allowed
    if extra:
        raise InvalidScenario("unsupported input fields: " + ", ".join(sorted(extra)))
    sym = symbol_value(data.get("symbol"))
    strategy = data.get("strategy")
    if strategy not in ("long_put", "put_debit_spread"):
        raise InvalidScenario("strategy must be long_put or put_debit_spread")
    expiration = data.get("expiration")
    if not isinstance(expiration, str):
        raise InvalidScenario("expiration must be a YYYY-MM-DD date")
    try:
        parsed = date.fromisoformat(expiration)
    except ValueError as exc:
        raise InvalidScenario("expiration must be a valid YYYY-MM-DD date") from exc
    if parsed.isoformat() != expiration:
        raise InvalidScenario("expiration must be a YYYY-MM-DD date")
    spot = positive_decimal(data.get("spot"), "spot")
    long_strike = positive_decimal(data.get("long_strike"), "long_strike")
    net_debit = positive_decimal(data.get("net_debit"), "net_debit")
    count = data.get("contracts", 1)
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 100:
        raise InvalidScenario("contracts must be an integer from 1 to 100")
    short_strike: Decimal | None = None
    if strategy == "put_debit_spread":
        short_strike = positive_decimal(data.get("short_strike"), "short_strike")
        if short_strike >= long_strike:
            raise InvalidScenario("short_strike must be below long_strike")
        if net_debit >= long_strike - short_strike:
            raise InvalidScenario("net_debit must be below spread width")
    elif data.get("short_strike") is not None:
        raise InvalidScenario("short_strike is invalid for long_put")
    elif net_debit >= long_strike:
        raise InvalidScenario("net_debit must be below long put strike")

    targets_raw = data.get("target_prices")
    if not isinstance(targets_raw, list) or not 1 <= len(targets_raw) <= 12:
        raise InvalidScenario("target_prices must contain 1-12 underlying prices")
    targets = [positive_decimal(t, "target_prices item", allow_zero=True) for t in targets_raw]
    multiplier = count * MULTIPLIER
    total_risk = net_debit * multiplier
    width = long_strike if short_strike is None else long_strike - short_strike
    max_profit = (width - net_debit) * multiplier
    breakeven = long_strike - net_debit
    values = []
    for target in targets:
        intrinsic = max(Decimal(0), long_strike - target)
        if short_strike is not None:
            intrinsic -= max(Decimal(0), short_strike - target)
        profit = (intrinsic - net_debit) * multiplier
        values.append(
            {
                "underlying_at_expiration": _money(target),
                "underlying_change_pct": float(((target / spot) - 1) * 100),
                "pnl_usd": _money(profit),
                "return_on_premium_pct": float(profit / total_risk * 100),
            }
        )
    result: dict[str, Any] = {
        "schema": "trex-mcp-options-scenario/1",
        "symbol": sym,
        "expiration": expiration,
        "strategy": strategy,
        "contracts": count,
        "contract_multiplier": 100,
        "assumed_spot": _money(spot),
        "long_strike": _money(long_strike),
        "short_strike": _money(short_strike) if short_strike is not None else None,
        "assumed_net_debit_per_share": _money(net_debit),
        "max_loss_usd": _money(total_risk),
        "max_gain_usd": _money(max_profit),
        "breakeven_at_expiration": _money(breakeven),
        "payoffs_at_expiration": values,
        "price_source": "caller-supplied assumptions; NOT a verified market quote",
        "execution_eligible": False,
        "order_intent_created": False,
        "limitations": [
            "Expiry intrinsic-value payoffs only; excludes commissions, fees and taxes.",
            "Before expiration, option value depends on time, volatility and liquidity.",
            "Exercise, assignment, and early-exit mechanics are not modeled.",
            "This is research math, not a strategy backtest or trade authorization.",
        ],
    }
    if "max_risk_budget" in data:
        budget = positive_decimal(data["max_risk_budget"], "max_risk_budget")
        result["max_risk_budget_usd"] = _money(budget)
        result["fits_assumed_risk_budget"] = total_risk <= budget
    return result
