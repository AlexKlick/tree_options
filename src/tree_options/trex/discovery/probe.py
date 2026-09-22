"""One-shot gateway capability report for the discovery lane.

Answers, from the LIVE paper session, the three questions every scan
assumption rests on: do option chains flow, do delayed tickers carry
model greeks, and do account tags arrive. Writes nothing — the caller
(CLI ``--probe``) prints the JSON; a human pastes the outcome into the
evidence doc and sets the config defaults accordingly.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from tree_options.trex.ibkr import IbkrTrex

log = logging.getLogger("trex.discovery.probe")

PROBE_SUBSCRIPTIONS = 6  # stay far under the ~50-line paper budget


def _pick_expiry(expirations: list[str], today: datetime, dte_min: int, dte_max: int) -> str | None:
    """First listed expiry inside the DTE window (skips 0DTE/weeklies past it)."""
    for raw in expirations:
        try:
            expiry = datetime.strptime(raw, "%Y%m%d")
        except ValueError:
            continue
        dte = (expiry.date() - today.date()).days  # calendar-day DTE
        if dte_min <= dte <= dte_max:
            return raw
    return None


def _stock(symbol: str) -> Any:
    """Real ib_async Stock on the gateway; duck-typed stand-in for tests.

    The repo gate runs broker-free (no ib_async installed); the probe is
    still unit-testable because the fake qualifies/uses only the fields
    below. On a real session the lazy import wins and contracts are real.
    """
    try:
        from ib_async import Stock
    except ImportError:
        from types import SimpleNamespace

        return SimpleNamespace(
            symbol=symbol, exchange="SMART", currency="USD", secType="STK", conId=0
        )
    return Stock(symbol, "SMART", "USD")


def _option(symbol: str, expiry: str, strike: float) -> Any:
    try:
        from ib_async import Option
    except ImportError:
        from types import SimpleNamespace

        return SimpleNamespace(
            symbol=symbol,
            lastTradeDateOrContractMonth=expiry,
            strike=strike,
            right="P",
            exchange="SMART",
            tradingClass=symbol,
            conId=0,
        )
    return Option(
        symbol=symbol,
        lastTradeDateOrContractMonth=expiry,
        strike=strike,
        right="P",
        exchange="SMART",
        tradingClass=symbol,
    )


def _expirations(ib: Any, symbol: str, conid: int) -> list[str]:
    params = ib.reqSecDefOptParams(symbol, "", "STK", conid)
    smart = [p for p in params if p.exchange == "SMART"] or list(params)
    return sorted({e for p in smart for e in p.expirations}) if smart else []


def _greeks_sample(
    ibk: IbkrTrex, symbol: str, conid: int, expirations: list[str], today: datetime
) -> dict[str, int]:
    """Subscribe a few strikes in the middle of the chain and count greeks."""
    expiry = _pick_expiry(expirations, today, dte_min=20, dte_max=60)
    if expiry is None:
        return {"rows_total": 0, "rows_with_greeks": 0, "expiry": None}
    params = ibk._ib.reqSecDefOptParams(symbol, "", "STK", conid)
    smart = [p for p in params if p.exchange == "SMART"] or params
    strikes = sorted({float(s) for p in smart for s in p.strikes})
    mid = len(strikes) // 2
    picks = strikes[max(0, mid - 1) : mid + PROBE_SUBSCRIPTIONS // 2 + 1]

    total = 0
    with_greeks = 0
    try:
        for strike in picks[:PROBE_SUBSCRIPTIONS]:
            contract = _option(symbol, expiry, strike)
            ibk._ib.qualifyContracts(contract)
            if getattr(contract, "conId", 0) == 0:
                continue
            ticker = ibk._ib.reqMktData(contract, "", False, False)
            total += 1
            greeks = getattr(ticker, "modelGreeks", None)
            delta = getattr(greeks, "delta", None) if greeks is not None else None
            if delta is not None and delta == delta:  # NaN guard
                with_greeks += 1
        ibk._ib.sleep(4)
    finally:
        log.info("probe subscribed %d option rows for %s", total, symbol)
    return {"rows_total": total, "rows_with_greeks": with_greeks, "expiry": expiry}


def probe_json(ibk: IbkrTrex, underlyings: list[str], now: datetime | None = None) -> dict[str, Any]:
    """Capability report: account + chains + greeks, no files written."""
    from tree_options.trex.clock import ET

    assert ibk._ib is not None
    ib = ibk._ib
    ib_now = now or datetime.now(ET)
    account = ibk.account_snapshot()
    chains: dict[str, Any] = {}
    greeks = {"rows_total": 0, "rows_with_greeks": 0}
    for symbol in underlyings:
        stock = _stock(symbol)
        ib.qualifyContracts(stock)
        conid = getattr(stock, "conId", 0)
        expirations = _expirations(ib, symbol, conid) if conid else []
        chains[symbol] = {"expirations": len(expirations), "strikes": 0}
        if expirations:
            params = ib.reqSecDefOptParams(symbol, "", "STK", conid)
            smart = [p for p in params if p.exchange == "SMART"] or params
            chains[symbol]["strikes"] = len({float(s) for p in smart for s in p.strikes})
            sample = _greeks_sample(ibk, symbol, conid, expirations, ib_now)
            chains[symbol]["expiry_probed"] = sample.get("expiry")
            chains[symbol]["greeks_sample"] = sample
            greeks["rows_total"] += sample["rows_total"]
            greeks["rows_with_greeks"] += sample["rows_with_greeks"]

    return {
        "account": account.to_payload() if account else None,
        "account_tags_seen": sorted({r.tag for r in ib.accountValues()}),
        "chains": chains,
        "greeks": greeks,
        "data_quality": {
            "chains_available": all(c["expirations"] > 0 for c in chains.values())
            if chains
            else False,
        },
    }
