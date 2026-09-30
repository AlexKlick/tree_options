import math

from tree_options.strategy_lab.volatility import (
    daily_volatility_signal,
    intraday_breakout_signal,
    prediction_premium,
    tutorial_contrarian_position,
)


def test_prediction_and_signal_hand_oracles():
    assert math.isclose(prediction_premium(forecast_variance=0.03, realized_variance=0.02), 0.5)
    assert daily_volatility_signal(premium=0.5, rolling_premium_std=0.2) == 1
    assert intraday_breakout_signal(close=110, rsi_value=75, lower_band=90, upper_band=105) == 1
    assert tutorial_contrarian_position(daily_signal=1, intraday_signal=1) == -1
    assert tutorial_contrarian_position(daily_signal=-1, intraday_signal=-1) == 1


def test_signal_uses_next_period_return_not_same_period_future_return():
    from tree_options.strategy_lab.volatility import lagged_signal_returns
    assert lagged_signal_returns([1, -1, 1], [100.0, 0.1, 0.2]) == (None, 0.1, -0.2)
    assert lagged_signal_returns([1, -1, -1], [999.0, 0.1, 0.2]) == (None, 0.1, -0.2)
