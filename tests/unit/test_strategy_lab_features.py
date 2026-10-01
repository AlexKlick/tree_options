import math

import pytest

from tree_options.strategy_lab.features import (
    FeatureError,
    atr,
    bollinger_bands,
    garman_klass_volatility,
    macd,
    rsi,
)


def test_garman_klass_finite():
    value = garman_klass_volatility(open_=100, high=105, low=98, close=103)
    assert math.isfinite(value)


def test_indicators_basic_contracts():
    prices = [100 + i * 0.5 + (i % 3) * 0.1 for i in range(60)]
    highs = [p + 1 for p in prices]
    lows = [p - 1 for p in prices]
    assert 0 <= rsi(prices, length=20) <= 100
    low, mid, high = bollinger_bands(prices, length=20)
    assert low <= mid <= high
    assert atr(highs, lows, prices, length=14) > 0
    line, signal, hist = macd(prices)
    assert math.isclose(line - signal, hist)


def test_garman_klass_refuses_ohlc_outside_range():
    with pytest.raises(FeatureError, match="within the reported"):
        garman_klass_volatility(open_=101.0, high=100.0, low=90.0, close=95.0)
