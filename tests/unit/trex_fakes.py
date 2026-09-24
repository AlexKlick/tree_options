"""Shared trex test doubles (desk lane E1/E2 and later).

``FakeGateway`` is a duck-typed ``ib_async.IB``: it drives the REAL
``IbkrTrex`` adapter (contract building, NBBO-from-legs quotes, BAG and OPT
orders, positions, what-if margin) with no gateway and no network. It
records what the adapter sent, so tests can pin wire content, and it keeps
the SDK behaviors that bite: a repeated ``reqMktData`` for one contract is a
new request of which ``cancelMktData`` cancels only the LATEST (ib_async
keeps one reqId per ticker); ``openTrades`` is every order not in a done
state; a what-if reply can stay unresolved (IBKR's UNSET-only answer), and
``run`` refuses to wait on anything without a timeout.

``FakeDeskBroker`` is a duck-typed ``IbkrTrex`` for runner-level tests of
multi-leg books (the desk runtime, E5): per-contract quotes summed into
package quotes in debit orientation, BAG or OPT orders tagged like the
adapter's, the same order bounds, ownership, account and release rules.
test_trex_ibkr_multileg runs one shared behavioural suite against both.

The monitor/enter choreography tests keep their own inline legacy fakes.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from tree_options.trex.ibkr import (
    DONE_STATES,
    BrokerPosition,
    OrderRef,
    OrderStatusInfo,
    bag_signature,
    order_tag,
    select_account,
    tagged_structure,
)
from tree_options.trex.plan import LegStructure, validate_package_order

ContractKey = tuple[str, str, str, float, str]  # secType, symbol, expiry, strike, right
BAG_CON_ID = 28812380  # IBKR gives every BAG this conId


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
        # market-data requests the gateway is still streaming, and the one
        # reqId per conId the SDK remembers (cancelMktData cancels only it)
        self.md_live: set[int] = set()
        self._md_latest: dict[int, int] = {}
        self._md_seq = 9000
        self.trades: list[FakeTrade] = []
        self._next_oid = 500
        self.order_cancels: list[int] = []
        self.position_rows: list[Any] = []
        self.fill_rows: list[Any] = []
        # what-if replies: an OrderState carrying ``whatif_margin`` (a str in
        # ib_async), else ``whatif_reply`` verbatim ([] = a failed request
        # with RaiseRequestErrors off), ``whatif_error`` raised, or no answer
        # at all (``whatif_pending``: IBKR's UNSET-only reply never ends it)
        self.whatif_margin: Any = ""
        self.whatif_reply: Any = None
        self.whatif_error: BaseException | None = None
        self.whatif_pending = False
        self.whatif_calls: list[tuple[Any, Any]] = []
        self.run_timeouts: list[float] = []
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
        self._md_seq += 1
        self.md_live.add(self._md_seq)
        self._md_latest[contract.conId] = self._md_seq
        return self.tickers.setdefault(contract.conId, FakeTicker())

    def cancelMktData(self, contract: Any) -> bool:
        self.mkt_cancelled.append(contract.conId)
        req = self._md_latest.pop(contract.conId, 0)
        if not req:
            return False
        self.md_live.discard(req)
        return True

    def quote(self, con_id: int, bid: float | None, ask: float | None) -> None:
        ticker = self.tickers.setdefault(con_id, FakeTicker())
        ticker.bid, ticker.ask = bid, ask

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)

    def run(self, *awaitables: Any, timeout: float | None = None) -> Any:
        """ib_async's IB.run (util.run): run the loop until the awaitable is
        done, TimeoutError after ``timeout``. Refuses an unbounded run: on
        an unresolved reply the real one would never return."""
        if not timeout:
            for aw in awaitables:
                if asyncio.iscoroutine(aw):
                    aw.close()
            raise AssertionError("unbounded broker request: it hangs on an unresolved reply")
        self.run_timeouts.append(timeout)
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(asyncio.wait_for(awaitables[0], timeout))
        finally:
            loop.close()

    # -- orders --------------------------------------------------------------

    def placeOrder(self, contract: Any, order: Any) -> FakeTrade:
        self._next_oid += 1
        order.orderId = self._next_oid
        trade = FakeTrade(contract=contract, order=order)
        self.trades.append(trade)
        return trade

    def openTrades(self) -> list[FakeTrade]:
        return [t for t in self.trades if t.orderStatus.status not in SDK_DONE_STATES]

    def cancelOrder(self, order: Any) -> None:
        self.order_cancels.append(order.orderId)

    def whatIfOrderAsync(self, contract: Any, order: Any) -> Any:
        self.whatif_calls.append((contract, order))

        async def reply() -> Any:
            if self.whatif_pending:
                await asyncio.get_running_loop().create_future()  # never resolved
            if self.whatif_error is not None:
                raise self.whatif_error
            if self.whatif_reply is not None:
                return self.whatif_reply
            return SimpleNamespace(initMarginChange=self.whatif_margin)

        return reply()

    # -- account -------------------------------------------------------------

    def positions(self) -> list[Any]:
        return list(self.position_rows)

    def fills(self) -> list[Any]:
        return list(self.fill_rows)


# ib_async.OrderStatus.DoneStates (pinned against the SDK in the tests)
SDK_DONE_STATES = frozenset({"Filled", "Cancelled", "ApiCancelled", "Inactive"})


def fill_row(con_id: int, shares: float, price: float, oid: int, client: int) -> Any:
    """One ib_async Fill as the adapter reads it (leg contract + execution)."""
    return SimpleNamespace(
        contract=SimpleNamespace(conId=con_id, secType="OPT"),
        execution=SimpleNamespace(orderId=oid, clientId=client, shares=shares, price=price),
    )


def position_row(
    con_id: int,
    qty: float,
    avg_cost: float = 0.0,
    account: str = "DU0000000",
    **contract: Any,
) -> Any:
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
        account=account,
        contract=SimpleNamespace(**fields),
        position=qty,
        avgCost=avg_cost,
    )


class FakeDeskBroker:
    """Duck-typed IbkrTrex for multi-leg runner tests (no ib_async needed).

    Each distinct leg contract (underlying, right, strike, expiry) gets a
    synthetic conId, so structures sharing a contract share its quote and
    identical packages build identical BAGs, as at IBKR. Orders go through
    the adapter's own rules: plan.validate_package_order, the prepared-spec
    check, the ``trex:<id>`` tag on every order, tag-only ownership
    (ibkr.tagged_structure), ibkr.select_account for positions.
    """

    def __init__(self) -> None:
        self.specs: dict[str, LegStructure] = {}
        self._con_ids: dict[tuple[str, str, Decimal, date], int] = {}
        self.quotes: dict[int, tuple[Decimal, Decimal]] = {}
        self.placed: list[tuple[str, str, int, Decimal]] = []
        self.trades: list[FakeTrade] = []
        self.cancelled: list[int] = []
        self.released: list[str] = []
        self.position_rows: list[BrokerPosition] = []
        self.whatif_margin: Decimal | None = None
        self.whatif_calls: list[tuple[str, str, int, Decimal]] = []
        self.connected = True
        self._next_oid = 900

    # -- contracts -----------------------------------------------------------

    def _con_id(self, underlying: str, leg: Any) -> int:
        key = (underlying, leg.right, leg.strike, leg.expiry)
        return self._con_ids.setdefault(key, 5001 + len(self._con_ids))

    def contract_for(self, struct: LegStructure) -> Any:
        """The contract the adapter would trade: the option of a long
        single, else a BAG of the debit-oriented legs."""
        legs = struct.package_legs()
        if struct.kind == "long_single":
            return SimpleNamespace(secType="OPT", conId=self._con_id(struct.underlying, legs[0]))
        return SimpleNamespace(
            secType="BAG",
            conId=BAG_CON_ID,
            comboLegs=[
                SimpleNamespace(
                    conId=self._con_id(struct.underlying, g), action=g.action, ratio=g.ratio
                )
                for g in legs
            ],
        )

    @staticmethod
    def _package(struct: LegStructure) -> tuple[Any, ...]:
        return (struct.underlying, struct.kind, struct.package_legs())

    def prepare(self, structures: list[LegStructure]) -> None:
        for s in structures:
            held = self.specs.get(s.id)
            if held is not None and self._package(held) != self._package(s):
                raise ValueError(f"{s.id}: already prepared with different legs")
        for s in structures:
            self.specs.setdefault(s.id, s)

    def _prepared(self, struct: LegStructure) -> None:
        held = self.specs.get(struct.id)
        if held is None:
            raise ValueError(f"{struct.id}: not prepared")
        if self._package(held) != self._package(struct):
            raise ValueError(f"{struct.id}: differs from the prepared structure")

    def release(self, structure_id: str) -> None:
        self.released.append(structure_id)
        self.specs.pop(structure_id, None)

    # -- market data ---------------------------------------------------------

    def set_leg_quote(self, sid: str, index: int, bid: str, ask: str) -> None:
        spec = self.specs[sid]
        con = self._con_id(spec.underlying, spec.package_legs()[index])
        self.quotes[con] = (Decimal(bid), Decimal(ask))

    def package_quote(self, structure_id: str) -> Any:
        from tree_options.trex.engine import ComboQuote

        spec = self.specs.get(structure_id)
        if spec is None:
            return None
        buy_bid = buy_ask = sell_bid = sell_ask = Decimal(0)
        for leg in spec.package_legs():
            q = self.quotes.get(self._con_id(spec.underlying, leg))
            if q is None:
                return None
            if leg.action == "BUY":
                buy_bid, buy_ask = buy_bid + q[0], buy_ask + q[1]
            else:
                sell_bid, sell_ask = sell_bid + q[0], sell_ask + q[1]
        return ComboQuote(bid=buy_bid - sell_ask, ask=buy_ask - sell_bid)

    def snapshot(self, structures: list[Any], ts: Any) -> Any:
        from tree_options.trex.engine import Snapshot

        return Snapshot(
            ts=ts, spots={}, quotes={s.id: self.package_quote(s.id) for s in structures}
        )

    # -- orders --------------------------------------------------------------

    def place(self, struct: LegStructure, side: str, qty: int, limit: Decimal) -> OrderRef:
        validate_package_order(struct, side, qty, limit)
        self._prepared(struct)
        self._next_oid += 1
        trade = FakeTrade(
            contract=self.contract_for(struct),
            order=SimpleNamespace(
                orderId=self._next_oid,
                action=side,
                totalQuantity=qty,
                lmtPrice=float(limit),
                orderRef=order_tag(struct.id),
            ),
        )
        self.trades.append(trade)
        self.placed.append((struct.id, side, qty, limit))
        return OrderRef(struct.id, side, qty, limit, trade)

    def working_trades(self) -> list[Any]:
        return [t for t in self.trades if t.orderStatus.status not in DONE_STATES]

    def _matches(self, sid: str, contract: Any) -> bool:
        want = self.contract_for(self.specs[sid])
        sec_type = getattr(contract, "secType", "")
        if want.secType == "OPT":
            return sec_type == "OPT" and getattr(contract, "conId", 0) == want.conId
        if sec_type != "BAG":
            return False
        try:
            return bag_signature(contract.comboLegs) == bag_signature(want.comboLegs)
        except (AttributeError, TypeError, ValueError):
            return False

    def structure_for_contract(self, contract: Any) -> str | None:
        matches = [sid for sid in self.specs if self._matches(sid, contract)]
        return matches[0] if len(matches) == 1 else None

    def structure_for_trade(self, trade: Any) -> str | None:
        sid = tagged_structure(getattr(trade.order, "orderRef", ""))
        if sid is not None and sid in self.specs and self._matches(sid, trade.contract):
            return sid
        return None

    def order_status(self, ref: OrderRef) -> OrderStatusInfo:
        os = ref.trade.orderStatus
        avg = Decimal(str(os.avgFillPrice)) if os.filled else Decimal(0)
        return OrderStatusInfo(status=os.status, filled=os.filled, avg_fill_price=avg)

    def cancel(self, ref: OrderRef) -> None:
        self.cancelled.append(ref.trade.order.orderId)
        ref.trade.orderStatus.status = "Cancelled"

    def whatif(self, struct: LegStructure, side: str, qty: int, limit: Decimal) -> Decimal | None:
        validate_package_order(struct, side, qty, limit)
        self._prepared(struct)
        self.whatif_calls.append((struct.id, side, qty, limit))
        return self.whatif_margin

    def sleep(self, seconds: float) -> None:
        return None

    # -- account -------------------------------------------------------------

    def positions(self, account: str | None = None) -> list[BrokerPosition]:
        return select_account(list(self.position_rows), account)

    def fill(self, ref: OrderRef, qty: int, price: str) -> None:
        ref.trade.orderStatus.status = "Filled"
        ref.trade.orderStatus.filled = qty
        ref.trade.orderStatus.avgFillPrice = float(price)
