"""Pure numerical feature functions used by research strategies.

These functions calculate values only. Point-in-time eligibility belongs to the
snapshot/Observation layer, not to a numerical indicator implementation.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

import numpy as np


class FeatureError(ValueError):
    pass


def _array(values: Sequence[float], *, minimum: int = 1, name: str = "values") -> np.ndarray:
    out = np.asarray(values, dtype=float)
    if out.ndim != 1 or out.size < minimum or not np.all(np.isfinite(out)):
        raise FeatureError(f"{name} must contain at least {minimum} finite values")
    return out


def garman_klass_volatility(*, open_: float, high: float, low: float, close: float) -> float:
    if min(open_, high, low, close) <= 0 or low > high:
        raise FeatureError("OHLC must be positive and low <= high")
    if not (low <= open_ <= high and low <= close <= high):
        raise FeatureError("open and close must lie within the reported low/high range")
    return ((math.log(high) - math.log(low)) ** 2) / 2 - (
        2 * math.log(2) - 1
    ) * ((math.log(close) - math.log(open_)) ** 2)


def rsi(prices: Sequence[float], *, length: int = 20) -> float:
    x = _array(prices, minimum=length + 1, name="prices")
    delta = np.diff(x)[-length:]
    gains = np.clip(delta, 0, None).mean()
    losses = (-np.clip(delta, None, 0)).mean()
    if losses == 0:
        return 100.0 if gains > 0 else 50.0
    rs = gains / losses
    return float(100 - 100 / (1 + rs))


def bollinger_bands(
    prices: Sequence[float], *, length: int = 20, standard_deviations: float = 2.0
) -> tuple[float, float, float]:
    x = _array(prices, minimum=length, name="prices")[-length:]
    mid = float(x.mean())
    sigma = float(x.std(ddof=0))
    return mid - standard_deviations * sigma, mid, mid + standard_deviations * sigma


def atr(
    highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], *, length: int = 14
) -> float:
    h = _array(highs, minimum=length + 1, name="highs")
    low_array = _array(lows, minimum=length + 1, name="lows")
    c = _array(closes, minimum=length + 1, name="closes")
    if not (h.size == low_array.size == c.size) or np.any(low_array > h):
        raise FeatureError("high/low/close arrays must align and low <= high")
    prev = c[:-1]
    tr = np.maximum.reduce([h[1:] - low_array[1:], np.abs(h[1:] - prev), np.abs(low_array[1:] - prev)])
    return float(tr[-length:].mean())


def _ema(values: np.ndarray, span: int) -> np.ndarray:
    alpha = 2.0 / (span + 1.0)
    out = np.empty_like(values)
    out[0] = values[0]
    for index in range(1, values.size):
        out[index] = alpha * values[index] + (1 - alpha) * out[index - 1]
    return out


def macd(
    prices: Sequence[float], *, fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[float, float, float]:
    if not (0 < fast < slow and signal > 0):
        raise FeatureError("expected 0 < fast < slow and signal > 0")
    x = _array(prices, minimum=slow + signal, name="prices")
    line = _ema(x, fast) - _ema(x, slow)
    signal_line = _ema(line, signal)
    return float(line[-1]), float(signal_line[-1]), float(line[-1] - signal_line[-1])


def dollar_volume(*, adjusted_close: float, volume: float) -> float:
    if adjusted_close < 0 or volume < 0 or not math.isfinite(adjusted_close + volume):
        raise FeatureError("price and volume must be finite and non-negative")
    return adjusted_close * volume
