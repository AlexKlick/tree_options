"""AI-BEAR-001 regime engine: hand-computed states and the no-lookahead guarantee.

The expected states come from piecewise-constant series whose moving
averages, drawdowns and VIX ratios can be worked out on paper (shown inline).
They are never recomputed with the engine's own helpers.
"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pytest

from tree_options.desk.aibear.params import NOT_EVALUABLE, RegimeParams, load_params, regime_params
from tree_options.desk.aibear.regime_engine import VixSeries, regime_features, regime_series

P = RegimeParams(
    ma_fast=50,
    ma_slow=200,
    slope_lag=20,
    high_window=252,
    mom_window=20,
    vix_median_window=60,
    vix_max_staleness_days=5,
    bear_drawdown=-0.20,
    topping_drawdown=-0.10,
    capitulation_drawdown=-0.20,
    capitulation_momentum=-0.10,
    capitulation_vix_ratio=1.25,
)


def business_days(n: int, start: date = date(2000, 1, 3)) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def crash_series() -> tuple[list[date], list[float], VixSeries]:
    """300 closes at 100, 30 at 60 (a 40 % crash), then 78 (a bear rally).
    VIX is 20 throughout except 30 on the crash day (index 300)."""
    closes = [100.0] * 300 + [60.0] * 30 + [78.0] * 5
    dates = business_days(len(closes))
    vix = VixSeries([(d, 30.0 if i == 300 else 20.0) for i, d in enumerate(dates)])
    return dates, closes, vix


def test_sealed_file_regime_block_matches_the_textbook_values() -> None:
    assert regime_params(load_params()) == P


def test_warmup_is_the_252_session_high_window() -> None:
    dates, closes, vix = crash_series()
    states = regime_series(dates, closes, vix, P)
    assert P.warmup == 251
    assert states[250] == NOT_EVALUABLE
    assert states[251] == "BULL"  # all 100: ma = close, drawdown 0, slope 0


def test_crash_day_is_capitulation() -> None:
    # i=300: drawdown 60/100-1 = -0.40; momentum 60/closes[280]-1 = -0.40;
    # VIX 30 / median(prior 60 readings = 20) = 1.5 >= 1.25
    dates, closes, vix = crash_series()
    f = regime_features(dates, closes, vix, P)[300]
    assert f is not None
    assert f.drawdown == pytest.approx(-0.40)
    assert f.momentum == pytest.approx(-0.40)
    assert f.vix_ratio == pytest.approx(1.5)
    assert regime_series(dates, closes, vix, P)[300] == "CAPITULATION"


def test_day_after_the_crash_is_bear() -> None:
    # i=301: VIX back to 20 (ratio 1.0); close 60 < ma200 = (198*100+2*60)/200
    # = 99.6; ma200 20 sessions earlier = 100, so the slope is negative;
    # drawdown -0.40; close 60 < ma50 = (48*100+2*60)/50 = 98.4 -> BEAR
    dates, closes, vix = crash_series()
    f = regime_features(dates, closes, vix, P)[301]
    assert f is not None
    assert f.ma_slow == pytest.approx(99.6)
    assert f.ma_fast == pytest.approx(98.4)
    assert regime_series(dates, closes, vix, P)[301] == "BEAR"


def test_bear_market_rally_above_the_50_day_is_relief() -> None:
    # i=330 (first 78): ma50 = (19*100 + 30*60 + 78)/50 = 75.56 < 78;
    # ma200 = (169*100 + 30*60 + 78)/200 = 93.89 > 78; ma200 at i=310 =
    # (189*100 + 11*60)/200 = 97.8, so the slope is negative; drawdown -0.22
    dates, closes, vix = crash_series()
    f = regime_features(dates, closes, vix, P)[330]
    assert f is not None
    assert f.ma_fast == pytest.approx(75.56)
    assert f.ma_slow == pytest.approx(93.89)
    assert f.drawdown == pytest.approx(-0.22)
    assert regime_series(dates, closes, vix, P)[330] == "RELIEF"


def test_a_five_percent_dip_below_the_200_day_is_topping() -> None:
    # 300 at 100 then 95: close 95 < ma200 = (199*100+95)/200 = 99.975;
    # drawdown -0.05 is above the -0.20 bear line -> TOPPING
    closes = [100.0] * 300 + [95.0]
    dates = business_days(len(closes))
    vix = VixSeries([(d, 20.0) for d in dates])
    assert regime_series(dates, closes, vix, P)[300] == "TOPPING"


def test_without_vix_a_crash_cannot_be_capitulation() -> None:
    dates, closes, _ = crash_series()
    assert regime_series(dates, closes, None, P)[300] == "BEAR"


def test_stale_vix_is_not_used() -> None:
    dates, closes, _ = crash_series()
    # VIX stops 10 calendar days before the crash: no ratio on the crash day
    cut = dates[300] - timedelta(days=10)
    vix = VixSeries([(d, 30.0) for d in dates if d <= cut])
    f = regime_features(dates, closes, vix, P)[300]
    assert f is not None and f.vix_ratio is None


def test_state_at_d_ignores_everything_after_d() -> None:
    rng = np.random.default_rng(7)
    n = 900
    closes = list(100.0 * np.exp(np.cumsum(rng.normal(0.0, 0.02, n))))
    dates = business_days(n)
    vix_vals = list(np.abs(rng.normal(20.0, 5.0, n)) + 5.0)
    vix = VixSeries(list(zip(dates, vix_vals, strict=True)))
    base = regime_series(dates, closes, vix, P)
    base_features = regime_features(dates, closes, vix, P)
    spike = 10.0 * max(closes)
    for k in (300, 555, 800):
        # The first poisoned close is a new all-time high, so any read of the
        # close after d moves d's drawdown even when the state does not flip.
        poisoned = [*closes[: k + 1], spike, *rng.uniform(1.0, 1000.0, n - k - 2)]
        pvix = VixSeries(
            list(
                zip(dates, vix_vals[: k + 1] + list(rng.uniform(5.0, 90.0, n - k - 1)), strict=True)
            )
        )
        assert regime_series(dates, poisoned, pvix, P)[: k + 1] == base[: k + 1]
        assert regime_features(dates, poisoned, pvix, P)[: k + 1] == base_features[: k + 1]


def test_bad_series_are_refused() -> None:
    dates = business_days(3)
    with pytest.raises(ValueError):
        regime_series(dates, [1.0, 2.0], None, P)
    with pytest.raises(ValueError):
        regime_series([dates[1], dates[0], dates[2]], [1.0, 2.0, 3.0], None, P)
    with pytest.raises(ValueError):
        regime_series(dates, [1.0, 0.0, 3.0], None, P)
