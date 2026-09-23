"""Synthetic CBOE delayed-chain payloads for the desk chain recorder.

Shape pinned by a live probe of ``cdn.cboe.com/api/global/delayed_quotes/
options/KO.json`` on 2026-09-23 (one symbol, read-only):

* top level ``{"data", "symbol", "timestamp"}``; ``timestamp`` is a naive
  UTC wall stamp ("2026-09-23 03:49:33"; the object's HTTP Last-Modified
  was 03:49:36 GMT), written overnight after the session;
* ``data`` carries the underlying quote (``current_price``, ``bid``,
  ``ask``, ``open``/``high``/``low``/``close``, ``prev_day_close``,
  ``volume``, ``iv30``, ``last_trade_time`` as an ET-naive
  "2026-09-22T15:59:59", ...) plus ``options``: one row per contract with
  ``option`` (OCC), ``bid``/``ask`` and float sizes, ``iv``, the greeks,
  ``theo``, ``open_interest``, ``volume``, ``last_trade_price`` and an
  ET-naive ``last_trade_time`` (``null`` when the contract never traded).

Every number here is invented; nothing comes from the vendor.
"""

from __future__ import annotations

import json
from typing import Any


def option_row(
    occ: str,
    *,
    bid: float = 1.0,
    ask: float = 1.1,
    last_time: str | None = "2026-09-22T15:30:00",
    last_price: float = 1.05,
    iv: float = 0.25,
    delta: float = 0.5,
    oi: float = 120.0,
    volume: float = 7.0,
) -> dict[str, Any]:
    return {
        "option": occ,
        "bid": bid,
        "bid_size": 12.0,
        "ask": ask,
        "ask_size": 3.0,
        "iv": iv,
        "open_interest": oi,
        "volume": volume,
        "delta": delta,
        "gamma": 0.03,
        "vega": 0.11,
        "theta": -0.02,
        "rho": 0.01,
        "theo": (bid + ask) / 2,
        "change": 0.04,
        "open": 1.0,
        "high": 1.2,
        "low": 0.9,
        "tick": "down",
        "last_trade_price": last_price if last_time else 0.0,
        "last_trade_time": last_time,
        "percent_change": 0.5,
        "prev_day_close": 1.0,
    }


def default_rows(session: str = "2026-09-22", root: str = "KO") -> list[dict[str, Any]]:
    """Four contracts, both rights, all traded on ``session``."""
    yymmdd = "261016"
    t = f"{session}T15:30:00"
    return [
        option_row(f"{root}{yymmdd}P00060000", bid=0.5, ask=0.55, delta=-0.2, last_time=t),
        option_row(f"{root}{yymmdd}C00060000", bid=2.1, ask=2.2, delta=0.8, last_time=t),
        option_row(f"{root}261120C00062500", bid=1.3, ask=1.35, delta=0.55, last_time=t),
        option_row(f"{root}261120P00062500", bid=0.9, ask=0.95, delta=-0.45, last_time=t),
    ]


def chain_payload(
    sym: str = "KO",
    *,
    timestamp: str | None = "2026-09-23 03:49:33",
    session: str = "2026-09-22",
    rows: list[dict[str, Any]] | None = None,
    underlying_last: str | None = None,
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "symbol": sym,
        "security_type": "stock",
        "exchange_id": 3,
        "current_price": 61.25,
        "price_change": 0.5,
        "price_change_percent": 0.8,
        "bid": 61.2,
        "ask": 61.3,
        "bid_size": 600,
        "ask_size": 100,
        "open": 60.9,
        "high": 61.5,
        "low": 60.7,
        "close": 61.25,
        "prev_day_close": 60.75,
        "volume": 17268957,
        "iv30": 18.037,
        "iv30_change": 0.0,
        "iv30_change_percent": 0.0,
        "seqno": 1,
        "last_trade_time": underlying_last or f"{session}T15:59:59",
        "tick": "down",
        "options": default_rows(session, sym) if rows is None else rows,
    }
    doc: dict[str, Any] = {"data": data, "symbol": sym}
    if timestamp is not None:
        doc["timestamp"] = timestamp
    return doc


def encode(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload).encode("utf-8")
