"""Daily regime states for AI-BEAR-001: a pure function of past closes and VIX.

The state at index ``i`` reads ``closes[: i + 1]`` and VIX observations dated
on or before ``dates[i]``, and nothing later. ``test_desk_aibear_regime``
holds the future-poison test for that guarantee. Every threshold comes from
the sealed parameters (textbook definitions, never fitted):

* CAPITULATION  drawdown <= -20 % AND 20-session return <= -10 % AND
                VIX >= 1.25 x its median over the prior 60 observations
* BEAR          close < 200-day MA AND the 200-day MA is lower than 20
                sessions ago AND drawdown <= -20 %;
  RELIEF        the same bear conditions, but close > 50-day MA (a bear-market
                rally)
* TOPPING       close < 200-day MA, OR drawdown <= -10 % with close < 50-day MA
* BULL          everything else

They are checked in that order, so CAPITULATION wins over BEAR. Drawdown is
measured from the highest close of the last 252 observations. Without a
usable VIX reading (none on or within ``vix_max_staleness_days`` before the
date, or fewer than 60 prior readings), CAPITULATION cannot fire. The other
states are still computed.
"""

from __future__ import annotations

import bisect
import statistics
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import date
from itertools import pairwise
from typing import Any

from tree_options.desk.aibear.params import NOT_EVALUABLE, RegimeParams


@dataclass(frozen=True)
class Features:
    close: float
    ma_fast: float
    ma_slow: float
    slope_slow: float
    drawdown: float
    momentum: float
    vix_ratio: float | None

    def as_dict(self) -> dict[str, Any]:
        return {k: (None if v is None else round(v, 6)) for k, v in asdict(self).items()}


class VixSeries:
    """Sorted VIX closes with a no-lookahead ratio lookup."""

    def __init__(self, points: Sequence[tuple[date, float]]) -> None:
        pts = sorted(points)
        for (d0, _), (d1, _) in pairwise(pts):
            if d0 == d1:
                raise ValueError(f"duplicate VIX date {d0}")
        if any(v <= 0 for _, v in pts):
            raise ValueError("VIX values must be positive")
        self.dates = [d for d, _ in pts]
        self.values = [v for _, v in pts]

    def last_on_or_before(self, d: date, staleness_days: int) -> tuple[int, float] | None:
        j = bisect.bisect_right(self.dates, d) - 1
        if j < 0 or (d - self.dates[j]).days > staleness_days:
            return None
        return j, self.values[j]

    def ratio_at(self, d: date, window: int, staleness_days: int) -> float | None:
        """VIX on (or just before) ``d`` over the median of the ``window``
        readings strictly before it."""
        hit = self.last_on_or_before(d, staleness_days)
        if hit is None:
            return None
        j, value = hit
        if j < window:
            return None
        base = statistics.median(self.values[j - window : j])
        return value / base if base > 0 else None


class _Prefix:
    """Prefix sums for O(1) window means."""

    def __init__(self, xs: Sequence[float]) -> None:
        acc = [0.0]
        for x in xs:
            acc.append(acc[-1] + x)
        self._acc = acc

    def mean(self, lo: int, hi: int) -> float:
        """Mean of xs[lo:hi]."""
        return (self._acc[hi] - self._acc[lo]) / (hi - lo)


def _features(
    closes: Sequence[float], prefix: _Prefix, i: int, p: RegimeParams, vix_ratio: float | None
) -> Features | None:
    if i < p.warmup:
        return None
    c = closes[i]
    ma_fast = prefix.mean(i - p.ma_fast + 1, i + 1)
    ma_slow = prefix.mean(i - p.ma_slow + 1, i + 1)
    lag = i - p.slope_lag
    ma_slow_prev = prefix.mean(lag - p.ma_slow + 1, lag + 1)
    high = max(closes[i - p.high_window + 1 : i + 1])
    return Features(
        close=c,
        ma_fast=ma_fast,
        ma_slow=ma_slow,
        slope_slow=ma_slow / ma_slow_prev - 1.0,
        drawdown=c / high - 1.0,
        momentum=c / closes[i - p.mom_window] - 1.0,
        vix_ratio=vix_ratio,
    )


def classify(f: Features, p: RegimeParams) -> str:
    if (
        f.vix_ratio is not None
        and f.drawdown <= p.capitulation_drawdown
        and f.momentum <= p.capitulation_momentum
        and f.vix_ratio >= p.capitulation_vix_ratio
    ):
        return "CAPITULATION"
    if f.close < f.ma_slow and f.slope_slow < 0.0 and f.drawdown <= p.bear_drawdown:
        return "RELIEF" if f.close > f.ma_fast else "BEAR"
    if f.close < f.ma_slow or (f.drawdown <= p.topping_drawdown and f.close < f.ma_fast):
        return "TOPPING"
    return "BULL"


def _check_series(dates: Sequence[date], closes: Sequence[float]) -> None:
    if len(dates) != len(closes):
        raise ValueError(f"{len(dates)} dates but {len(closes)} closes")
    for d0, d1 in pairwise(dates):
        if d1 <= d0:
            raise ValueError(f"dates must strictly increase: {d0} then {d1}")
    if any(not (c > 0.0) for c in closes):
        raise ValueError("closes must be positive and finite")


def regime_features(
    dates: Sequence[date], closes: Sequence[float], vix: VixSeries | None, p: RegimeParams
) -> list[Features | None]:
    """Per-index features (None until the warm-up is complete)."""
    _check_series(dates, closes)
    prefix = _Prefix(closes)
    out: list[Features | None] = []
    for i, d in enumerate(dates):
        ratio = (
            None if vix is None else vix.ratio_at(d, p.vix_median_window, p.vix_max_staleness_days)
        )
        out.append(_features(closes, prefix, i, p, ratio))
    return out


def regime_series(
    dates: Sequence[date], closes: Sequence[float], vix: VixSeries | None, p: RegimeParams
) -> list[str]:
    """Per-index state, ``NOT_EVALUABLE`` until the warm-up is complete."""
    return [
        NOT_EVALUABLE if f is None else classify(f, p)
        for f in regime_features(dates, closes, vix, p)
    ]
