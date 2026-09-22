"""Thin IBKR adapter for trex (ib_async over the local gateway).

Kept as pure I/O: no decisions live here. ``ib_async`` is imported lazily
so the research/test environment never needs it; the decision core stays
importable without a broker.

Combo pricing is derived from leg NBBOs rather than a combo ticker — on
125-wide wings the combo book is empty and its "mid" is fiction:

    combo_bid = long_bid - short_ask   (best executable sale of the spread)
    combo_ask = long_ask - short_bid   (best executable purchase)

Connection defaults target IB Gateway in paper mode (docker, port 4002).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from tree_options.trex.account import AccountSnapshot
from tree_options.trex.engine import ComboQuote, Snapshot
from tree_options.trex.plan import PutSpread

log = logging.getLogger("trex.ibkr")

GATEWAY_PAPER_PORT = 4002
GATEWAY_LIVE_PORT = 4001


@dataclass(frozen=True)
class OrderRef:
    """Local handle for an order this process placed."""

    structure_id: str
    side: str  # "BUY" | "SELL"
    qty: int
    limit: Decimal
    trade: Any  # ib_async.Trade


@dataclass(frozen=True)
class OrderStatusInfo:
    status: str  # ib orderStatus string, e.g. "Filled", "Cancelled"
    filled: int
    avg_fill_price: Decimal


def _d(value: float | int | None) -> Decimal:
    return Decimal(str(value)) if value is not None else Decimal(0)


class IbkrTrex:
    """One connected session; call :meth:`connect` before anything else."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = GATEWAY_PAPER_PORT,
        client_id: int = 77,
        delayed_ok: bool = True,
    ) -> None:
        self.host = host
        self.port = port
        self.client_id = client_id
        self.delayed_ok = delayed_ok
        self._ib: Any = None
        self._legs: dict[tuple[str, str], Any] = {}  # (structure_id, "long"|"short")
        self._bags: dict[str, Any] = {}  # structure_id -> BAG contract
        self._spots: dict[str, Any] = {}  # underlying -> Stock contract
        self._tickers: dict[Any, Any] = {}

    # -- lifecycle ---------------------------------------------------------

    def connect(self) -> None:
        from ib_async import IB  # lazy: optional dependency group

        self._ib = IB()
        self._ib.connect(self.host, self.port, clientId=self.client_id, timeout=20)
        if self.delayed_ok:
            # Paper accounts without market-data subscriptions: fall back to
            # delayed quotes instead of trading blind on absent NBBO.
            self._ib.reqMarketDataType(3)

    def disconnect(self) -> None:
        if self._ib is not None:
            self._ib.disconnect()

    @property
    def connected(self) -> bool:
        return self._ib is not None and self._ib.isConnected()

    # -- contracts ---------------------------------------------------------

    def prepare(self, spreads: list[PutSpread]) -> None:
        """Qualify all option legs, build the BAG contracts, subscribe spots."""
        from ib_async import ComboLeg, Contract, Option, Stock

        assert self._ib is not None
        spot_contracts: dict[str, Any] = {}
        option_legs: list[Any] = []
        for spread in spreads:
            long_leg = Option(
                symbol=spread.underlying,
                lastTradeDateOrContractMonth=spread.expiry.strftime("%Y%m%d"),
                strike=float(spread.long_strike),
                right="P",
                exchange="SMART",
                tradingClass=spread.underlying,
            )
            short_leg = Option(
                symbol=spread.underlying,
                lastTradeDateOrContractMonth=spread.expiry.strftime("%Y%m%d"),
                strike=float(spread.short_strike),
                right="P",
                exchange="SMART",
                tradingClass=spread.underlying,
            )
            option_legs += [long_leg, short_leg]
            self._legs[(spread.id, "long")] = long_leg
            self._legs[(spread.id, "short")] = short_leg
            spot_contracts.setdefault(
                spread.underlying, Stock(spread.underlying, "SMART", "USD")
            )

        # Spot stocks are qualified alongside the option legs, not separately:
        # they are used as ``_tickers`` / ``_spots`` dict keys below, and
        # ib_async refuses to hash a Contract whose conId is still unset.
        to_qualify = [*option_legs, *spot_contracts.values()]
        self._ib.qualifyContracts(*to_qualify)
        # qualifyContracts fills conId in place; a contract it could not
        # resolve is left with conId 0.
        unqualified = [str(c) for c in to_qualify if getattr(c, "conId", 0) == 0]
        if unqualified:
            raise RuntimeError(f"unqualified contracts: {unqualified}")

        for spread in spreads:
            long_leg = self._legs[(spread.id, "long")]
            short_leg = self._legs[(spread.id, "short")]
            self._bags[spread.id] = Contract(
                symbol=spread.underlying,
                secType="BAG",
                exchange="SMART",
                currency="USD",
                tradingClass=spread.underlying,
                comboLegs=[
                    ComboLeg(
                        conId=long_leg.conId,
                        ratio=1,
                        action="BUY",
                        exchange="SMART",
                    ),
                    ComboLeg(
                        conId=short_leg.conId,
                        ratio=1,
                        action="SELL",
                        exchange="SMART",
                    ),
                ],
            )
            self._tickers[long_leg] = self._ib.reqMktData(long_leg, "", False, False)
            self._tickers[short_leg] = self._ib.reqMktData(short_leg, "", False, False)
        for symbol, stock in spot_contracts.items():
            self._spots[symbol] = stock
            self._tickers[stock] = self._ib.reqMktData(stock, "", False, False)
        # let first quotes arrive
        self._ib.sleep(4)

    # -- market data -------------------------------------------------------

    def spot(self, underlying: str) -> Decimal | None:
        ticker = self._tickers.get(self._spots.get(underlying))
        if ticker is None:
            return None
        for attr in ("last", "close"):
            value = getattr(ticker, attr, None)
            if value is not None and value == value:  # NaN guard
                return _d(value)
        return None

    def leg_quote(self, structure_id: str, which: str) -> tuple[Decimal, Decimal] | None:
        leg = self._legs.get((structure_id, which))
        if leg is None:
            return None
        ticker = self._tickers.get(leg)
        if ticker is None:
            return None
        bid, ask = ticker.bid, ticker.ask
        if bid is None or ask is None or bid != bid or ask != ask:  # NaN guard
            return None
        return _d(bid), _d(ask)

    def combo_quote(self, structure_id: str) -> ComboQuote | None:
        long_q = self.leg_quote(structure_id, "long")
        short_q = self.leg_quote(structure_id, "short")
        if long_q is None or short_q is None:
            return None
        return ComboQuote(bid=long_q[0] - short_q[1], ask=long_q[1] - short_q[0])

    def snapshot(self, spreads: list[PutSpread], ts: datetime) -> Snapshot:
        spots: dict[str, Decimal] = {}
        for spread in spreads:
            if spread.underlying not in spots:
                spot = self.spot(spread.underlying)
                if spot is not None:
                    spots[spread.underlying] = spot
        return Snapshot(
            ts=ts,
            spots=spots,
            quotes={s.id: self.combo_quote(s.id) for s in spreads},
        )

    # -- orders ------------------------------------------------------------

    def place_combo(
        self, spread: PutSpread, side: str, qty: int, limit: Decimal
    ) -> OrderRef:
        from ib_async import LimitOrder

        assert self._ib is not None
        order = LimitOrder(side, qty, float(limit), tif="DAY", transmit=True)
        trade = self._ib.placeOrder(self._bags[spread.id], order)
        return OrderRef(spread.id, side, qty, limit, trade)

    def open_combo_trades(self) -> list[Any]:
        """Working BAG orders at the broker (ours or anyone's session)."""
        return [t for t in self._ib.openTrades() if getattr(t.contract, "secType", "") == "BAG"]

    def structure_for_bag(self, contract: Any) -> str | None:
        """Which plan structure (if any) a BAG contract belongs to."""
        try:
            conids = {leg.conId for leg in contract.comboLegs}
        except AttributeError:
            return None
        for sid, bag in self._bags.items():
            if {leg.conId for leg in bag.comboLegs} == conids:
                return sid
        return None

    def order_status(self, ref: OrderRef) -> OrderStatusInfo:
        os = ref.trade.orderStatus
        status = os.status or "Unknown"
        filled = int(os.filled or 0)
        avg = _d(os.avgFillPrice) if filled else Decimal(0)
        return OrderStatusInfo(status=status, filled=filled, avg_fill_price=avg)

    def cancel(self, ref: OrderRef) -> None:
        if ref.trade.orderStatus.status not in ("Filled", "Cancelled", "ApiCancelled"):
            self._ib.cancelOrder(ref.trade.order)

    def sleep(self, seconds: float) -> None:
        """Pump the event loop (ib_async needs its own sleep, not time.sleep)."""
        self._ib.sleep(seconds)

    # -- account -----------------------------------------------------------

    def account_snapshot(self) -> AccountSnapshot | None:
        """Net liq / cash / buying power from the gateway session.

        Reads the cached accountValues rows first (ib_async auto-subscribes
        account updates on connect); falls back to ``accountSummary(account)``
        — which RETURNS rows — when the cache is empty. Never fabricates:
        a NaN or missing tag means None, not zero.
        """
        from datetime import datetime

        from tree_options.trex.clock import ET

        assert self._ib is not None
        ib = self._ib
        rows = list(ib.accountValues())
        if not rows:
            accounts = list(ib.managedAccounts())
            if not accounts:
                return None
            rows = list(ib.accountSummary(accounts[0]))
        if not rows:
            return None

        def tag(tag_name: str) -> Decimal | None:
            for row in rows:
                if row.tag != tag_name:
                    continue
                try:
                    value = Decimal(str(row.value))
                except Exception:  # junk value: treated as absent
                    continue
                if value == value and value.is_finite():  # NaN guard
                    return value
            return None

        net_liquidation = tag("NetLiquidation")
        cash = tag("TotalCashValue")
        buying_power = tag("BuyingPower")
        if net_liquidation is None or cash is None or buying_power is None:
            return None
        account_id = next((str(r.account) for r in rows if getattr(r, "account", "")), "")
        if not account_id:
            accounts = list(ib.managedAccounts())
            account_id = accounts[0] if accounts else ""
        return AccountSnapshot(
            account_id=account_id,
            net_liquidation=net_liquidation,
            cash=cash,
            buying_power=buying_power,
            currency="USD",
            ts=datetime.now(ET),
        )
