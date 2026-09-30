"""GARCH experiment seam and pure daily/intraday signal logic."""

from __future__ import annotations

import importlib
import math
from collections.abc import Sequence

import numpy as np


class VolatilityError(ValueError):
    pass


def prediction_premium(*, forecast_variance: float, realized_variance: float) -> float:
    if not all(math.isfinite(v) for v in (forecast_variance, realized_variance)):
        raise VolatilityError("variance inputs must be finite")
    if realized_variance <= 0 or forecast_variance < 0:
        raise VolatilityError("realized variance must be >0 and forecast variance >=0")
    return (forecast_variance - realized_variance) / realized_variance


def daily_volatility_signal(*, premium: float, rolling_premium_std: float) -> int | None:
    if not math.isfinite(premium) or not math.isfinite(rolling_premium_std) or rolling_premium_std < 0:
        raise VolatilityError("invalid premium/std")
    if premium > rolling_premium_std:
        return 1
    if premium < -rolling_premium_std:
        return -1
    return None


def intraday_breakout_signal(
    *, close: float, rsi_value: float, lower_band: float, upper_band: float
) -> int | None:
    if not all(math.isfinite(v) for v in (close, rsi_value, lower_band, upper_band)):
        raise VolatilityError("intraday inputs must be finite")
    if lower_band > upper_band:
        raise VolatilityError("lower_band > upper_band")
    if rsi_value > 70 and close > upper_band:
        return 1
    if rsi_value < 30 and close < lower_band:
        return -1
    return None


def tutorial_contrarian_position(*, daily_signal: int | None, intraday_signal: int | None) -> int | None:
    """The tutorial's combined direction, isolated so it is easy to challenge."""
    if daily_signal == 1 and intraday_signal == 1:
        return -1
    if daily_signal == -1 and intraday_signal == -1:
        return 1
    return None


class ArchGarchForecaster:
    """Optional rolling GARCH forecaster; never imported by execution code."""

    def __init__(self, *, p: int = 1, q: int = 3) -> None:
        if p < 1 or q < 1:
            raise VolatilityError("p and q must be >=1")
        self.p = p
        self.q = q

    def forecast_variance(self, log_returns: Sequence[float]) -> float:
        x = np.asarray(log_returns, dtype=float)
        if x.ndim != 1 or x.size < 30 or not np.all(np.isfinite(x)):
            raise VolatilityError("finite 1-D return window with >=30 observations required")
        try:
            arch_model = importlib.import_module("arch").arch_model
        except ImportError as error:  # pragma: no cover - optional integration
            raise VolatilityError("arch package is not installed") from error
        fit = arch_model(y=x, p=self.p, q=self.q).fit(update_freq=0, disp="off")
        value = float(fit.forecast(horizon=1).variance.iloc[-1, 0])
        if not math.isfinite(value) or value < 0:
            raise VolatilityError("GARCH returned invalid variance")
        return value


def lagged_signal_returns(signals: Sequence[int | None], realized_returns: Sequence[float]) -> tuple[float | None, ...]:
    """Evaluate each completed-period signal on the NEXT period's return.

    First period has no prior signal. The strategy remains data-gated until
    registered daily/intraday sources supply admissible availability manifests.
    """
    if len(signals) != len(realized_returns):
        raise VolatilityError('signal/return alignment length mismatch')
    if any(s not in {-1, 0, 1, None} for s in signals) or any(not math.isfinite(r) for r in realized_returns):
        raise VolatilityError('invalid aligned inputs')
    if not signals:
        return ()
    lagged = (None, *signals[:-1])
    return tuple(None if s is None else s * r for s, r in zip(lagged, realized_returns, strict=True))
