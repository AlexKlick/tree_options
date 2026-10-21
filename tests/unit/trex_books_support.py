"""Shared test support for the books lane (fills, ledger, reconcile, risk).

The IBKR -> contract mappings here are INDEPENDENT test oracles (lane B owns
the production adapter): they read the ``Wave2Gateway`` fake's ib_async
shaped objects and build the C2.4 broker-fact models, so the books modules
are exercised on the same exec ids, corrections and late commission
reports the fake produces.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import tree_options.trex.contracts as c
from tests.unit.trex_fakes import FillStep, Wave2Gateway, contract_fixture

ACCOUNT = "DU0000001"
SPY = 756733
ENTRY = "trex:sup:eq-spy-001"
EXIT = "trex:desk:eq-spy-001"


def at(text: str) -> datetime:
    return datetime.fromisoformat(text).astimezone(UTC)


def execution(**changes: Any) -> c.BrokerExecution:
    """The fixture execution with top-level changes; a new ``exec_id``
    re-derives ``base_exec_id`` unless one is given."""
    doc = contract_fixture("broker_execution.json")
    if "exec_id" in changes and "base_exec_id" not in changes:
        changes["base_exec_id"] = c.base_exec_id_of(changes["exec_id"])
    doc.update(changes)
    return c.BrokerExecution.model_validate(doc)


def portfolio_item(**changes: Any) -> c.BrokerPortfolioItem:
    doc = contract_fixture("broker_portfolio_item.json")
    doc.update(changes)
    return c.BrokerPortfolioItem.model_validate(doc)


def as_broker_execution(fill: Any) -> c.BrokerExecution:
    """An ib_async ``Fill`` -> ``BrokerExecution`` (commission None until
    the fill's own commission report has arrived)."""
    ex, report = fill.execution, fill.commissionReport
    return c.BrokerExecution(
        exec_id=ex.execId,
        base_exec_id=c.base_exec_id_of(ex.execId),
        perm_id=ex.permId,
        order_ref=ex.orderRef,
        account_id=ex.acctNumber,
        con_id=fill.contract.conId,
        sec_type=fill.contract.secType,
        symbol=fill.contract.symbol,
        side=ex.side,
        quantity=int(ex.shares),
        price_micros=round(ex.price * 1_000_000),
        multiplier=int(getattr(fill.contract, "multiplier", "") or 1),
        time=ex.time,
        commission_micros=(
            round(report.commission * 1_000_000) if report.execId == ex.execId else None
        ),
    )


def gateway_executions(gw: Wave2Gateway) -> list[c.BrokerExecution]:
    """Every execution the gateway reported (all clients), as broker facts."""
    return [as_broker_execution(fill) for fill in gw.reqExecutions()]


def as_portfolio_item(item: Any, as_of: datetime) -> c.BrokerPortfolioItem:
    """An ib_async ``PortfolioItem`` -> ``BrokerPortfolioItem`` (raises on a
    fractional quantity or an unknown secType: the fail-closed material)."""
    qty = item.position
    whole = isinstance(qty, float) and qty.is_integer()
    return c.BrokerPortfolioItem.model_validate(
        {
            "account_id": item.account,
            "con_id": item.contract.conId,
            "sec_type": item.contract.secType,
            "symbol": item.contract.symbol,
            "position": int(qty) if whole else qty,
            "avg_cost_micros": round(item.averageCost * 1_000_000),
            "market_price_micros": round(item.marketPrice * 1_000_000),
            "market_value_cents": round(item.marketValue * 100),
            "as_of": as_of,
        }
    )


def stock(symbol: str = "SPY") -> Any:
    return SimpleNamespace(
        secType="STK",
        symbol=symbol,
        exchange="SMART",
        currency="USD",
        conId=0,
        lastTradeDateOrContractMonth="",
        strike=0.0,
        right="",
    )


def limit_order(action: str, qty: int, price: float, ref: str) -> Any:
    return SimpleNamespace(
        action=action,
        totalQuantity=qty,
        orderType="LMT",
        lmtPrice=price,
        tif="DAY",
        outsideRth=False,
        account=ACCOUNT,
        orderRef=ref,
        transmit=True,
    )


def place(gw: Wave2Gateway, action: str = "BUY", qty: int = 2, price: float = 671.5) -> Any:
    contract = stock()
    gw.qualifyContracts(contract)
    ref = ENTRY if action == "BUY" else EXIT
    return gw.placeOrder(contract, limit_order(action, qty, price, ref))


def run(gw: Wave2Gateway, steps: int = 3) -> None:
    for _ in range(steps):
        gw.sleep(1)


def buy(gw: Wave2Gateway, steps: list[FillStep], qty: int = 2, price: float = 671.5) -> Any:
    gw.script_fills(ENTRY, steps)
    trade = place(gw, "BUY", qty, price)
    run(gw, len(steps) + 2)
    return trade


def sell(gw: Wave2Gateway, steps: list[FillStep], qty: int = 2, price: float = 671.5) -> Any:
    gw.script_fills(EXIT, steps)
    trade = place(gw, "SELL", qty, price)
    run(gw, len(steps) + 2)
    return trade
