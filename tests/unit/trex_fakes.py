"""Shared trex test doubles (desk lane E1/E2 and later).

``FakeGateway`` is a duck-typed ``ib_async.IB``: it drives the REAL
``IbkrTrex`` adapter (contract building, NBBO-from-legs quotes, BAG and OPT
orders, positions, what-if margin) with no gateway and no network. It
records what the adapter sent, so tests can pin wire content.

``FakeDeskBroker`` is a duck-typed ``IbkrTrex`` for runner-level tests of
multi-leg books (the desk runtime, E5): per-leg quotes summed into package
quotes in debit orientation, BAG or OPT orders, positions and what-if
margins. Its method names mirror IbkrTrex's multi-leg surface
(test_trex_ibkr_multileg pins that they stay in step).

The monitor/enter choreography tests keep their own inline legacy fakes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

ContractKey = tuple[str, str, str, float, str]  # secType, symbol, expiry, strike, right


@dataclass
class FakeTicker:
    bid: float | None = None
    ask: float | None = None


@dataclass
class FakeStatus:
    status: str = "Submitted"
    filled: int = 0
    avgFillPrice: float = 0.0


@dataclass
class FakeTrade:
    contract: Any
    order: Any
    orderStatus: FakeStatus = field(default_factory=FakeStatus)


def contract_key(contract: Any) -> ContractKey:
    return (
        str(contract.secType),
        str(contract.symbol),
        str(getattr(contract, "lastTradeDateOrContractMonth", "") or ""),
        float(getattr(contract, "strike", 0.0) or 0.0),
        str(getattr(contract, "right", "") or ""),
    )


class FakeGateway:
    """Duck-typed ib_async.IB. ``con_ids`` presets conIds by contract key;
    anything else qualifies to the next free id from 1001. Keys listed in
    ``unknown`` stay unqualified (conId 0), like a contract IBKR can't find."""

    def __init__(
        self,
        con_ids: dict[ContractKey, int] | None = None,
        unknown: set[ContractKey] | None = None,
    ) -> None:
        self.con_ids: dict[ContractKey, int] = dict(con_ids or {})
        self.unknown = set(unknown or ())
        self._next_con = 1000
        self.qualify_calls: list[list[Any]] = []
        self.tickers: dict[int, FakeTicker] = {}
        self.subscribed: list[Any] = []
        self.mkt_cancelled: list[int] = []
        self.trades: list[FakeTrade] = []
        self._next_oid = 500
        self.order_cancels: list[int] = []
        self.position_rows: list[Any] = []
        self.fill_rows: list[Any] = []
        self.whatif_margin: Any = ""  # OrderState.initMarginChange (a str in ib_async)
        self.whatif_calls: list[tuple[Any, Any]] = []
        self.slept: list[float] = []

    # -- contracts / market data --------------------------------------------

    def con_id(
        self, sec_type: str, symbol: str, expiry: str = "", strike: float = 0.0, right: str = ""
    ) -> int:
        return self.con_ids[(sec_type, symbol, expiry, float(strike), right)]

    def qualifyContracts(self, *contracts: Any) -> list[Any]:
        self.qualify_calls.append(list(contracts))
        for c in contracts:
            key = contract_key(c)
            if key in self.unknown:
                continue
            if key not in self.con_ids:
                self._next_con += 1
                while self._next_con in self.con_ids.values():
                    self._next_con += 1
                self.con_ids[key] = self._next_con
            c.conId = self.con_ids[key]
        return list(contracts)

    def reqMktData(self, contract: Any, *_args: Any, **_kwargs: Any) -> FakeTicker:
        self.subscribed.append(contract)
        return self.tickers.setdefault(contract.conId, FakeTicker())

    def cancelMktData(self, contract: Any) -> bool:
        self.mkt_cancelled.append(contract.conId)
        return True

    def quote(self, con_id: int, bid: float | None, ask: float | None) -> None:
        ticker = self.tickers.setdefault(con_id, FakeTicker())
        ticker.bid, ticker.ask = bid, ask

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)

    # -- orders --------------------------------------------------------------

    def placeOrder(self, contract: Any, order: Any) -> FakeTrade:
        self._next_oid += 1
        order.orderId = self._next_oid
        trade = FakeTrade(contract=contract, order=order)
        self.trades.append(trade)
        return trade

    def openTrades(self) -> list[FakeTrade]:
        return [t for t in self.trades if t.orderStatus.status in ("Submitted", "PreSubmitted")]

    def cancelOrder(self, order: Any) -> None:
        self.order_cancels.append(order.orderId)

    def whatIfOrder(self, contract: Any, order: Any) -> Any:
        self.whatif_calls.append((contract, order))
        return SimpleNamespace(initMarginChange=self.whatif_margin)

    # -- account -------------------------------------------------------------

    def positions(self) -> list[Any]:
        return list(self.position_rows)

    def fills(self) -> list[Any]:
        return list(self.fill_rows)


def fill_row(con_id: int, shares: float, price: float, oid: int, client: int) -> Any:
    """One ib_async Fill as the adapter reads it (leg contract + execution)."""
    return SimpleNamespace(
        contract=SimpleNamespace(conId=con_id, secType="OPT"),
        execution=SimpleNamespace(orderId=oid, clientId=client, shares=shares, price=price),
    )


def position_row(con_id: int, qty: float, avg_cost: float = 0.0, **contract: Any) -> Any:
    """One ib_async Position as the adapter reads it."""
    fields: dict[str, Any] = {
        "conId": con_id,
        "secType": "OPT",
        "symbol": "",
        "right": "",
        "strike": 0.0,
        "lastTradeDateOrContractMonth": "",
    }
    fields.update(contract)
    return SimpleNamespace(
        account="DU0000000",
        contract=SimpleNamespace(**fields),
        position=qty,
        avgCost=avg_cost,
    )


class FakeDeskBroker:
    """Duck-typed IbkrTrex for multi-leg runner tests.

    Quotes are set per (structure id, leg index) and summed into the
    package quote in DEBIT ORIENTATION exactly as the adapter does (the
    oracle for that sum lives in the adapter's own tests). Orders record
    (structure id, side, qty, limit) and go on a BAG, or on the single
    OPT contract for long singles.
    """

    def __init__(self) -> None:
        self.specs: dict[str, Any] = {}
        self.leg_quotes: dict[tuple[str, int], tuple[Decimal, Decimal] | None] = {}
        self.placed: list[tuple[str, str, int, Decimal]] = []
        self.trades: list[FakeTrade] = []
        self.cancelled: list[int] = []
        self.released: list[str] = []
        self.position_rows: list[Any] = []
        self.whatif_margin: Decimal | None = None
        self.whatif_calls: list[tuple[str, str, int, Decimal]] = []
        self.connected = True
        self._next_oid = 900

    def prepare(self, structures: list[Any]) -> None:
        for s in structures:
            self.specs[s.id] = s

    def set_leg_quote(self, sid: str, index: int, bid: str, ask: str) -> None:
        self.leg_quotes[(sid, index)] = (Decimal(bid), Decimal(ask))

    def package_quote(self, structure_id: str) -> Any:
        from tree_options.trex.engine import ComboQuote

        spec = self.specs.get(structure_id)
        if spec is None:
            return None
        bid = ask = Decimal(0)
        for i, leg in enumerate(spec.package_legs()):
            q = self.leg_quotes.get((structure_id, i))
            if q is None:
                return None
            if leg.action == "BUY":
                bid, ask = bid + q[0], ask + q[1]
            else:
                bid, ask = bid - q[1], ask - q[0]
        return ComboQuote(bid=bid, ask=ask)

    def snapshot(self, structures: list[Any], ts: Any) -> Any:
        from tree_options.trex.engine import Snapshot

        return Snapshot(
            ts=ts, spots={}, quotes={s.id: self.package_quote(s.id) for s in structures}
        )

    def place(self, struct: Any, side: str, qty: int, limit: Decimal) -> Any:
        from tree_options.trex.ibkr import OrderRef

        self._next_oid += 1
        sec_type = "OPT" if struct.kind == "long_single" else "BAG"
        trade = FakeTrade(
            contract=SimpleNamespace(secType=sec_type, structure=struct.id),
            order=SimpleNamespace(
                orderId=self._next_oid,
                action=side,
                totalQuantity=qty,
                lmtPrice=float(limit),
                orderRef=f"trex:{struct.id}",
            ),
        )
        self.trades.append(trade)
        self.placed.append((struct.id, side, qty, limit))
        return OrderRef(struct.id, side, qty, limit, trade)

    def working_trades(self) -> list[Any]:
        return [t for t in self.trades if t.orderStatus.status == "Submitted"]

    def structure_for_contract(self, contract: Any) -> str | None:
        return getattr(contract, "structure", None)

    def structure_for_trade(self, trade: Any) -> str | None:
        return self.structure_for_contract(trade.contract)

    def order_status(self, ref: Any) -> Any:
        from tree_options.trex.ibkr import OrderStatusInfo

        os = ref.trade.orderStatus
        avg = Decimal(str(os.avgFillPrice)) if os.filled else Decimal(0)
        return OrderStatusInfo(status=os.status, filled=os.filled, avg_fill_price=avg)

    def cancel(self, ref: Any) -> None:
        self.cancelled.append(ref.trade.order.orderId)
        ref.trade.orderStatus.status = "Cancelled"

    def positions(self) -> list[Any]:
        return list(self.position_rows)

    def release(self, structure_id: str) -> None:
        self.released.append(structure_id)
        self.specs.pop(structure_id, None)

    def whatif(self, struct: Any, side: str, qty: int, limit: Decimal) -> Decimal | None:
        self.whatif_calls.append((struct.id, side, qty, limit))
        return self.whatif_margin

    def sleep(self, seconds: float) -> None:
        return None

    def fill(self, ref: Any, qty: int, price: str) -> None:
        ref.trade.orderStatus.status = "Filled"
        ref.trade.orderStatus.filled = qty
        ref.trade.orderStatus.avgFillPrice = float(price)
