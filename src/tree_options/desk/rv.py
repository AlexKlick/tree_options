"""Realized-variance proxies over the split-adjusted research panel.

The FORECAST-001 daily proxy (``docs/desk/FORECAST-001.md``) is the
overnight move plus Garman-Klass::

    v_t = ln(O_t/C_{t-1})^2 + 0.5 ln(H_t/L_t)^2 - (2 ln2 - 1) ln(C_t/O_t)^2

where t-1 is the previous NYSE session. A missing bar, a non-positive price
or a non-positive v makes v_t missing (None), never zero.

Also here: complete-window trailing means, the EWMA(0.94) benchmark (seeded
per contiguous run) and the Yang-Zhang estimator for display.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

GK_K = 2.0 * math.log(2.0) - 1.0
YZ_ALPHA = 0.34  # Yang-Zhang's k numerator
TRADING_DAYS = 252


def variance_proxy(
    open_: float, high: float, low: float, close: float, prev_close: float
) -> float | None:
    if min(open_, high, low, close, prev_close) <= 0.0 or high < low:
        return None
    v = (
        math.log(open_ / prev_close) ** 2
        + 0.5 * math.log(high / low) ** 2
        - GK_K * math.log(close / open_) ** 2
    )
    return v if v > 0.0 else None


def _ohlc(bar: Mapping[str, Any]) -> tuple[float, float, float, float] | None:
    try:
        return (
            float(bar["open"]),
            float(bar["high"]),
            float(bar["low"]),
            float(bar["close"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def proxy_series(
    bars: Mapping[str, Mapping[str, Any]], sessions: Sequence[str]
) -> list[float | None]:
    """v aligned to ``sessions`` (consecutive NYSE sessions, ISO strings):
    v[i] needs the bar of sessions[i] and the close of sessions[i-1]."""
    out: list[float | None] = [None] * len(sessions)
    prev: tuple[float, float, float, float] | None = None
    for i, s in enumerate(sessions):
        bar = bars.get(s)
        cur = _ohlc(bar) if bar is not None else None
        if cur is not None and prev is not None:
            out[i] = variance_proxy(cur[0], cur[1], cur[2], cur[3], prev[3])
        prev = cur
    return out


def trailing_mean(v: Sequence[float | None], i: int, n: int) -> float | None:
    """mean(v[i-n+1..i]) when all n values exist, else None."""
    lo = i - n + 1
    if lo < 0 or i >= len(v):
        return None
    total = 0.0
    for x in v[lo : i + 1]:
        if x is None:
            return None
        total += x
    return total / n


def ewma_series(
    v: Sequence[float | None], *, lam: float = 0.94, seed: int = 22
) -> list[float | None]:
    """RiskMetrics recursion on the proxy: each contiguous run of present v
    is seeded with the mean of its first ``seed`` values (at the run's
    ``seed``-th element); a gap restarts the run."""
    out: list[float | None] = [None] * len(v)
    run: list[float] = []
    sigma2: float | None = None
    for i, x in enumerate(v):
        if x is None:
            run, sigma2 = [], None
            continue
        if sigma2 is None:
            run.append(x)
            if len(run) == seed:
                sigma2 = sum(run) / seed
                out[i] = sigma2
            continue
        sigma2 = lam * sigma2 + (1.0 - lam) * x
        out[i] = sigma2
    return out


def yang_zhang_variance(
    opens: Sequence[float],
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    prev_close: float,
) -> float | None:
    """Daily Yang-Zhang variance over the window (n >= 2 sessions);
    ``prev_close`` is the close before the window's first session."""
    n = len(opens)
    if n < 2 or not (len(highs) == len(lows) == len(closes) == n):
        return None
    overnight: list[float] = []
    intraday: list[float] = []
    rs = 0.0
    p = prev_close
    for o, h, lo, c in zip(opens, highs, lows, closes, strict=True):
        if min(o, h, lo, c, p) <= 0.0:
            return None
        overnight.append(math.log(o / p))
        intraday.append(math.log(c / o))
        rs += math.log(h / c) * math.log(h / o) + math.log(lo / c) * math.log(lo / o)
        p = c
    mo = sum(overnight) / n
    mc = sum(intraday) / n
    s2_o = sum((x - mo) ** 2 for x in overnight) / (n - 1)
    s2_c = sum((x - mc) ** 2 for x in intraday) / (n - 1)
    k = YZ_ALPHA / (1.34 + (n + 1) / (n - 1))
    return s2_o + k * s2_c + (1.0 - k) * rs / n


def yang_zhang_annualized(
    bars: Mapping[str, Mapping[str, Any]], sessions: Sequence[str], i: int, n: int = 22
) -> float | None:
    """Annualized Yang-Zhang vol over sessions[i-n+1..i] (display only)."""
    lo = i - n + 1
    if lo < 1 or i >= len(sessions):
        return None
    prev_bar = bars.get(sessions[lo - 1])
    prev = _ohlc(prev_bar) if prev_bar is not None else None
    rows = []
    for s in sessions[lo : i + 1]:
        bar = bars.get(s)
        cur = _ohlc(bar) if bar is not None else None
        if cur is None:
            return None
        rows.append(cur)
    if prev is None:
        return None
    var = yang_zhang_variance(
        [r[0] for r in rows],
        [r[1] for r in rows],
        [r[2] for r in rows],
        [r[3] for r in rows],
        prev[3],
    )
    if var is None or var < 0.0:
        return None
    return math.sqrt(TRADING_DAYS * var)
