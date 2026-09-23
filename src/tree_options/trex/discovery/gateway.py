"""The real ChainSource: gateway chain I/O for the discovery lane.

Thin on purpose — quote-hygiene and ranking live in engine.py. Holds its
own gateway session on the discovery clientId (74); never places orders.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from tree_options.trex.account import AccountSnapshot
from tree_options.trex.discovery.config import DISCOVERY_CLIENT_ID
from tree_options.trex.discovery.engine import ChainRow
from tree_options.trex.ibkr import IbkrTrex

log = logging.getLogger("trex.discovery.gateway")


class IbkrDiscovery:
    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 4002,
        client_id: int = DISCOVERY_CLIENT_ID,
    ) -> None:
        self._ibk = IbkrTrex(host=host, port=port, client_id=client_id)
        self._subscribed: list[Any] = []

    def connect(self) -> None:
        self._ibk.connect()

    def disconnect(self) -> None:
        self._ibk.disconnect()

    def connected(self) -> bool:
        return self._ibk.connected

    def sleep(self, seconds: float) -> None:
        """Event-loop-pumping sleep: between scans this is what lets
        ib_async deliver account-value updates, so account_history rows
        carry FRESH broker values instead of the first cached read
        re-stamped forever."""
        self._ibk.sleep(seconds)

    def account(self) -> AccountSnapshot | None:
        return self._ibk.account_snapshot()

    def chain(self, symbol: str) -> tuple[list[str], list[float]] | None:
        from ib_async import Stock  # lazy: broker-side only

        assert self._ibk._ib is not None
        stock = Stock(symbol, "SMART", "USD")
        self._ibk._ib.qualifyContracts(stock)
        conid = getattr(stock, "conId", 0)
        if not conid:
            log.warning("%s: stock unqualified", symbol)
            return None
        params = self._ibk._ib.reqSecDefOptParams(symbol, "", "STK", conid)
        smart = [p for p in params if p.exchange == "SMART"] or list(params)
        if not smart:
            return None
        expirations = sorted({e for p in smart for e in p.expirations})
        strikes = sorted({float(s) for p in smart for s in p.strikes})
        if not expirations or not strikes:
            return None
        return expirations, strikes

    def put_rows(
        self, symbol: str, expiry: str, strikes: list[float], limit: int
    ) -> list[ChainRow]:
        """Delayed put quotes for a centered strike window (line budget)."""
        from ib_async import Option  # lazy

        assert self._ibk._ib is not None
        ib = self._ibk._ib
        ordered = sorted(strikes)
        mid = len(ordered) // 2
        half = max(1, limit // 2)
        window = ordered[max(0, mid - half) : mid + half]
        rows: list[ChainRow] = []
        try:
            for strike in window:
                contract = Option(
                    symbol=symbol,
                    lastTradeDateOrContractMonth=expiry,
                    strike=strike,
                    right="P",
                    exchange="SMART",
                    tradingClass=symbol,
                )
                ib.qualifyContracts(contract)
                if getattr(contract, "conId", 0) == 0:
                    continue  # per-strike qualify gaps are expected
                ticker = ib.reqMktData(contract, "", False, False)
                self._subscribed.append((contract, ticker))
            ib.sleep(4)
            observed = datetime.now().astimezone()
            for _contract, ticker in self._subscribed:
                bid = getattr(ticker, "bid", None)
                ask = getattr(ticker, "ask", None)
                greeks = getattr(ticker, "modelGreeks", None)
                delta = getattr(greeks, "delta", None) if greeks is not None else None
                rows.append(
                    ChainRow(
                        strike=float(_contract.strike),
                        bid=float(bid) if bid is not None and bid == bid else None,
                        ask=float(ask) if ask is not None and ask == ask else None,
                        delta=float(delta) if delta is not None and delta == delta else None,
                        ts=observed,
                    )
                )
        finally:
            self.cancel_all()
        return rows

    def cancel_all(self) -> None:
        assert self._ibk._ib is not None
        for contract, _ticker in self._subscribed:
            try:
                self._ibk._ib.cancelMktData(contract)
            except Exception:  # teardown must never raise
                log.exception("cancelMktData failed")
        self._subscribed.clear()
