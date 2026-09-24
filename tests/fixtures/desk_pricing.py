"""Synthetic inputs for the desk pricing tests (pit, distribution, pricing).

A research panel on real NYSE sessions, a recorded-chain document priced by
Black-Scholes on a declared smile with declared half-spreads, stored index
files, an IV-history document and an earnings schedule. Every number is
invented; the builders only exercise the code. Test oracles never call the
code under test: they are computed in the test modules themselves.
"""

from __future__ import annotations

import gzip
import json
import math
from collections.abc import Callable, Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np

from tree_options.synth_options.greeks import bs_price
from tree_options.time.calendar import StaticSessionCalendar

REPO = Path(__file__).resolve().parents[2]
TREX_CAL = REPO / "data" / "calendar" / "trex" / "nyse_sessions_2018_01_02_2028_12_29.json"


def trex_calendar() -> StaticSessionCalendar:
    return StaticSessionCalendar(TREX_CAL, TREX_CAL.with_suffix(".sha256"))


def synthetic_panel(
    cal: StaticSessionCalendar,
    names: Sequence[str],
    start: date,
    end: date,
    seed: int,
    *,
    base_vol: float = 0.015,
) -> dict[str, dict[str, Any]]:
    """OHLC bars with a log-AR(1) daily vol (the HAR test's generator)."""
    rng = np.random.default_rng(seed)
    sessions = [s for s in cal.sessions() if start <= s <= end]
    panel: dict[str, dict[str, Any]] = {}
    for name in names:
        close = 100.0
        lv = math.log(base_vol)
        bars: dict[str, Any] = {}
        for s in sessions:
            lv = math.log(base_vol) + 0.95 * (lv - math.log(base_vol)) + 0.2 * rng.normal()
            sig = math.exp(lv)
            o = close * math.exp(0.4 * sig * rng.normal())
            c = o * math.exp(sig * rng.normal())
            hi = max(o, c) * math.exp(abs(rng.normal()) * sig / 3)
            lo = min(o, c) * math.exp(-abs(rng.normal()) * sig / 3)
            bars[s.isoformat()] = {
                "open": f"{o:.4f}",
                "high": f"{hi:.4f}",
                "low": f"{lo:.4f}",
                "close": f"{c:.4f}",
                "volume": 1000,
            }
            close = float(f"{c:.4f}")
        panel[name] = bars
    return panel


def occ(sym: str, expiry: date, right: str, strike: float) -> str:
    return f"{sym}{expiry:%y%m%d}{right}{round(strike * 1000):08d}"


def chain_doc(
    *,
    sym: str,
    session: date,
    spot: float,
    rate: float,
    expiries: Sequence[date],
    strikes: Sequence[float],
    iv: Callable[[float], float],
    half_spread: Callable[[float], float],
    source_as_of: str,
    rights: Sequence[str] = ("C", "P"),
) -> dict[str, Any]:
    """A desk-chain/1 document: each (expiry, right, strike) priced by the
    pinned ``bs_price`` at ``iv(ln(K/spot))`` (q = 0), quoted at price
    +- ``half_spread(price)``. Rows whose bid would not be positive get no
    bid (a one-sided quote)."""
    columns: dict[str, list[Any]] = {
        c: []
        for c in (
            "occ", "exp", "right", "strike", "bid", "ask", "bid_size", "ask_size", "iv",
            "delta", "gamma", "theta", "vega", "rho", "theo", "oi", "volume", "last",
            "last_time",
        )
    }  # fmt: skip
    for expiry in expiries:
        dte = (expiry - session).days
        for right in rights:
            for k in strikes:
                px = bs_price(
                    spot=spot,
                    strike=k,
                    dte_calendar_days=dte,
                    iv=iv(math.log(k / spot)),
                    risk_free=rate,
                    dividend_yield=0.0,
                    call_put="C" if right == "C" else "P",
                )
                hs = half_spread(px)
                bid = px - hs
                columns["occ"].append(occ(sym, expiry, right, k))
                columns["exp"].append(expiry.isoformat())
                columns["right"].append(right)
                columns["strike"].append(float(k))
                columns["bid"].append(bid if bid > 0.0 else None)
                columns["ask"].append(px + hs)
                columns["bid_size"].append(10)
                columns["ask_size"].append(10)
                for col in ("iv", "delta", "gamma", "theta", "vega", "rho", "theo", "last"):
                    columns[col].append(None)
                columns["oi"].append(500)
                columns["volume"].append(10)
                columns["last_time"].append(None)
    header = {
        "schema": "desk-chain/1",
        "session": session.isoformat(),
        "underlying": sym,
        "source": "cboe-delayed",
        "source_as_of": source_as_of,
        "fetched_at": source_as_of,
        "underlying_quote": {"close": spot, "current_price": spot},
        "raw_sha256": "0" * 64,
        "n": len(columns["occ"]),
        "n_skipped": 0,
    }
    return {"header": header, "columns": columns}


def write_chain(store: Path, doc: Mapping[str, Any], *, conflict: bool = False) -> Path:
    h = doc["header"]
    name = f"{h['underlying']}.conflict.json.gz" if conflict else f"{h['underlying']}.json.gz"
    path = store / "chains" / h["session"] / name
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(doc, separators=(",", ":"), sort_keys=True)
    path.write_bytes(gzip.compress(text.encode(), mtime=0))
    return path


def write_index(store: Path, name: str, rows: Sequence[tuple[str, str]]) -> Path:
    """A stored index file (the market lane's format): close only."""
    path = store / "indices" / f"{name}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("date,open,high,low,close\n" + "".join(f"{d},,,,{v}\n" for d, v in rows))
    return path


def iv_history_doc(series: Mapping[str, Mapping[str, float | None]]) -> dict[str, Any]:
    """The ivhist output shape: None is a NOT_EVALUABLE session."""
    names: dict[str, Any] = {}
    for name, per_day in series.items():
        names[name] = {
            "sessions": {
                d: (
                    {"iv30": v, "method": "interpolated"}
                    if v is not None
                    else {"status": "NOT_EVALUABLE", "reason": "no option bars"}
                )
                for d, v in per_day.items()
            }
        }
    return {"schema": "desk-ivhist/1", "names": names}


def smile(m: float) -> float:
    """A declared skewed smile in m = ln(K/S)."""
    return 0.30 - 0.20 * m + 0.30 * m * m


def half_spread(price: float) -> float:
    return 0.02 + 0.02 * price
