"""Declared next-open/close experiments through TREX's funded FIFO ledger.

Each period starts with the same cash and liquidates at its next session's
close. The mean is a mean of independent mechanical experiments, not a
compounded wallet or a monthly strategy backtest. Prices, fees, and slippage
are modeled inputs: these records never supply exact external fill evidence.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from itertools import pairwise
from types import MappingProxyType
from typing import Any

from tree_options.backtest.equity import FiveBasisPointFeeModel
from tree_options.research.comparison.funded import (
    MarkObservation,
    TradeExecution,
    run_funded_account,
)
from tree_options.research.quant import QuantSnapshot, digest, run_strategy
from tree_options.research.runstate.store import RunstateStore
from tree_options.schemas.common import PRICE_TICK
from tree_options.strategy_lab.contracts import require_utc
from tree_options.time.calendar import SessionCalendar, calendar_content_sha256

ZERO = Decimal("0")
ONE = Decimal("1")
CAPITAL_POLICY = "independent_equal_capital_roundtrips"


@dataclass(frozen=True)
class ReplayPeriod:
    """Decision-frozen features and separately identified future outcome prices."""

    snapshot: QuantSnapshot
    execution_at: datetime
    mark_at: datetime
    opens: Mapping[str, Decimal]
    closes: Mapping[str, Decimal]

    def __post_init__(self) -> None:
        object.__setattr__(self, "execution_at", require_utc(self.execution_at))
        object.__setattr__(self, "mark_at", require_utc(self.mark_at))
        if self.execution_at <= self.snapshot.cutoff:
            raise ValueError("execution must be after decision cutoff")
        if self.mark_at <= self.execution_at:
            raise ValueError("mark must be after execution")
        for name in ("opens", "closes"):
            values = dict(getattr(self, name))
            for symbol, price in values.items():
                if (
                    not isinstance(symbol, str)
                    or symbol not in self.snapshot.universe.members
                    or not isinstance(price, Decimal)
                    or not price.is_finite()
                    or price <= ZERO
                ):
                    raise ValueError(
                        "price requires frozen-universe identity and finite positive Decimal"
                    )
            object.__setattr__(self, name, MappingProxyType(dict(sorted(values.items()))))

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot": self.snapshot.to_dict(),
            "execution_at": self.execution_at.isoformat(),
            "mark_at": self.mark_at.isoformat(),
            "opens": {symbol: str(price) for symbol, price in self.opens.items()},
            "closes": {symbol: str(price) for symbol, price in self.closes.items()},
        }

    @property
    def identity(self) -> str:
        return digest(self.to_dict())


def _validate_costs(capital: Decimal, slippage_bps: Decimal) -> None:
    if (
        not isinstance(capital, Decimal)
        or not capital.is_finite()
        or capital <= ZERO
        or capital >= Decimal("10000000000000000")
        or capital != capital.quantize(PRICE_TICK)
    ):
        raise ValueError("capital must be finite positive Decimal cents within ledger precision")
    if (
        not isinstance(slippage_bps, Decimal)
        or not slippage_bps.is_finite()
        or slippage_bps < ZERO
        or slippage_bps >= Decimal("10000")
    ):
        raise ValueError("slippage must be finite Decimal basis points in [0, 10000)")


def _fill_price(price: Decimal, slippage: Decimal, *, buy: bool) -> Decimal:
    raw = price * (ONE + slippage if buy else ONE - slippage)
    if raw >= Decimal("10000000000"):
        raise ValueError("modeled fill price outside existing ledger precision")
    slipped = raw.quantize(PRICE_TICK, rounding=ROUND_HALF_UP)
    if slipped <= ZERO or slipped >= Decimal("10000000000"):
        raise ValueError("modeled fill price outside existing ledger precision")
    return slipped


def evaluate_period(
    store: RunstateStore,
    version: dict[str, Any],
    period: ReplayPeriod,
    calendar: SessionCalendar,
    capital: Decimal = Decimal("10000"),
    slippage_bps: Decimal = Decimal("10"),
) -> dict[str, Any]:
    """Evaluate a declared open/close roundtrip; never consult outcomes for scores."""
    _validate_costs(capital, slippage_bps)
    session = calendar.nth_after(period.snapshot.universe.as_of, 1)
    if period.execution_at != calendar.session_open(session):
        raise ValueError("execution must equal exact next session open")
    if period.mark_at != calendar.session_close(session):
        raise ValueError("mark must equal exact execution session close")

    research_run = run_strategy(store, version, period.snapshot, calendar=calendar)
    costs = {
        "fee_model": "FiveBasisPointFeeModel",
        "fee_bps_per_side": "5",
        "slippage_bps_per_side": str(slippage_bps),
        "fill_price_rounding": "cent_half_up",
        "liquidation_policy": "declared_next_session_close",
    }
    identity = digest(
        {
            "model": "next_session_roundtrip/1",
            "period": period.identity,
            "research_run": research_run["run_id"],
            "capital": str(capital),
            "costs": costs,
            "calendar": calendar_content_sha256(calendar),
        }
    )
    result: dict[str, Any] = {
        "backtest_id": identity,
        "period_id": period.identity,
        "research_run": research_run,
        "evidence_kind": "BACKTEST",
        "execution_authorized": False,
        "exact_external_economics": False,
        "capital_policy": CAPITAL_POLICY,
        "accounting_model": "existing_funded_account_fifo/1",
        "starting_capital": str(capital),
        "execution_at": period.execution_at.isoformat(),
        "mark_at": period.mark_at.isoformat(),
        "costs": costs,
        "economics_source": "modeled_prices_and_declared_cost_assumptions",
        "ledger_timestamp_role": "synthetic_same_session_sequence_only",
        "max_drawdown_scope": "endpoint_loss_only",
        "drawdown_scope": "starting_capital_to_terminal_cash_only",
        "turnover_policy": "gross_buy_and_sell_fill_notional_over_starting_capital",
        "disposition": research_run["disposition"],
        "nav": None,
        "net_return": None,
        "max_drawdown": None,
        "turnover": None,
        "fees": None,
        "executions": [],
        "positions": [],
    }
    if not research_run["targets"]:
        store.put("quant_backtest_period", result, key=identity)
        return result

    selected = {target["entity_id"] for target in research_run["targets"]}
    if selected - period.opens.keys() or selected - period.closes.keys():
        raise ValueError("missing selected or held open/close price; imputation refused")
    fee_model = FiveBasisPointFeeModel()
    slippage = slippage_bps / Decimal("10000")
    buys: list[TradeExecution] = []
    sells: list[TradeExecution] = []
    for target in research_run["targets"]:
        symbol = target["entity_id"]
        price = _fill_price(period.opens[symbol], slippage, buy=True)
        budget = capital * Decimal(target["weight"])
        quantity = fee_model.affordable_quantity(budget=budget, price=price)
        if quantity:
            buys.append(
                TradeExecution(
                    session,
                    symbol,
                    quantity,
                    price,
                    fee_model.order_fees(price=price, quantity=quantity),
                )
            )
            exit_price = _fill_price(period.closes[symbol], slippage, buy=False)
            sells.append(
                TradeExecution(
                    session,
                    symbol,
                    -quantity,
                    exit_price,
                    fee_model.order_fees(price=exit_price, quantity=quantity),
                )
            )
    if not buys:
        result["disposition"] = "NO_AFFORDABLE_POSITION"
        store.put("quant_backtest_period", result, key=identity)
        return result
    executions = [*buys, *sells]
    funded = run_funded_account(
        identity,
        capital,
        calendar=(session,),
        executions=executions,
        marks=tuple(
            MarkObservation(session, symbol, period.closes[symbol]) for symbol in sorted(selected)
        ),
        cashflows=(),
        fee_model=fee_model,
    )
    if funded.refusal_reason or funded.final_nav is None:
        raise ValueError(f"funded replay refused: {funded.refusal_reason or 'missing NAV'}")
    funded.ledger.assert_conservation()
    row = funded.rows[-1]
    if row.inventory:
        raise ValueError("roundtrip liquidation left holdings")
    net_return = funded.final_nav / capital - ONE
    result.update(
        {
            "disposition": "SCORED",
            "nav": str(funded.final_nav),
            "net_return": str(net_return),
            "max_drawdown": str(max(ZERO, -net_return)),
            "turnover": str(
                sum((abs(item.signed_quantity) * item.price for item in executions), ZERO) / capital
            ),
            "fees": str(funded.total_fees),
            "executions": [
                {
                    "symbol": item.symbol,
                    "signed_quantity": item.signed_quantity,
                    "price": str(item.price),
                    "fees": str(item.fees),
                    "modeled_execution_at": (
                        period.execution_at if item.signed_quantity > 0 else period.mark_at
                    ).isoformat(),
                }
                for item in executions
            ],
        }
    )
    store.put("quant_backtest_period", result, key=identity)
    return result


def evaluate_periods(
    store: RunstateStore,
    version: dict[str, Any],
    periods: Sequence[ReplayPeriod],
    calendar: SessionCalendar,
    capital: Decimal = Decimal("10000"),
    slippage_bps: Decimal = Decimal("10"),
) -> dict[str, Any]:
    """Complete equal-capital experiments only; no selective missingness ranking."""
    if not periods:
        raise ValueError("period sequence must be nonempty")
    if any(right.snapshot.cutoff <= left.snapshot.cutoff for left, right in pairwise(periods)):
        raise ValueError("period cutoffs must be strictly increasing")
    rows = [evaluate_period(store, version, p, calendar, capital, slippage_bps) for p in periods]
    complete = all(row["disposition"] == "SCORED" for row in rows)
    result: dict[str, Any] = {
        "evidence_kind": "BACKTEST",
        "capital_policy": CAPITAL_POLICY,
        "disposition": "SCORED" if complete else "INCOMPLETE",
        "execution_authorized": False,
        "exact_external_economics": False,
        "period_count": len(rows),
        "scored_period_count": sum(row["disposition"] == "SCORED" for row in rows),
        "periods": rows,
        "compound_nav": None,
        "max_drawdown_scope": "endpoint_loss_only",
        "turnover_aggregation": "mean_per_period",
        "fees_aggregation": "sum",
        "mean_net_return": None,
        "max_drawdown": None,
        "turnover": None,
        "fees": None,
    }
    if complete:
        result.update(
            {
                "mean_net_return": str(
                    sum((Decimal(row["net_return"]) for row in rows), ZERO) / len(rows)
                ),
                "max_drawdown": str(max(Decimal(row["max_drawdown"]) for row in rows)),
                "turnover": str(sum((Decimal(row["turnover"]) for row in rows), ZERO) / len(rows)),
                "fees": str(sum((Decimal(row["fees"]) for row in rows), ZERO)),
            }
        )
    return result
