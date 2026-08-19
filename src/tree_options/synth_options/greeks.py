"""Black-Scholes pricing and analytic delta under the planted IV (M3 §3.A).

The generator KNOWS its implied process: IV is planted per
(underlying, expiry) and constant across sessions, so premium and delta are
derived analytically from the same process — the candidate filter's
`abs_delta` input is internally consistent by construction, never an
imported vendor greek (spike §6.5).

Stdlib-only on purpose: the package pin hashes every byte of
`synth_options/*.py` and the package must stay importable without numpy.
"""

from __future__ import annotations

import math
from typing import Literal

CallPut = Literal["C", "P"]

_MIN_T_DAYS = 0.5  # expiry-session quotes still price half a day of decay


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_price(
    *,
    spot: float,
    strike: float,
    dte_calendar_days: int,
    iv: float,
    risk_free: float,
    dividend_yield: float,
    call_put: CallPut,
) -> float:
    """Black-Scholes premium for one contract share (multiply by the
    multiplier for cash terms). T floors at half a day so the expiration
    session itself is priceable."""
    if spot <= 0.0 or strike <= 0.0:
        raise ValueError(f"spot and strike must be positive: {spot=} {strike=}")
    if iv <= 0.0:
        raise ValueError(f"iv must be positive: {iv=}")
    t_years = max(float(dte_calendar_days), _MIN_T_DAYS) / 365.0
    vol = iv * math.sqrt(t_years)
    d1 = (math.log(spot / strike) + (risk_free - dividend_yield + 0.5 * iv * iv) * t_years) / vol
    d2 = d1 - vol
    disc_q = math.exp(-dividend_yield * t_years)
    disc_r = math.exp(-risk_free * t_years)
    if call_put == "C":
        price = spot * disc_q * norm_cdf(d1) - strike * disc_r * norm_cdf(d2)
    else:
        price = strike * disc_r * norm_cdf(-d2) - spot * disc_q * norm_cdf(-d1)
    return max(price, 0.0)


def bs_abs_delta(
    *,
    spot: float,
    strike: float,
    dte_calendar_days: int,
    iv: float,
    risk_free: float,
    dividend_yield: float,
    call_put: CallPut,
) -> float:
    """|delta| of the contract (the §9.2 filter input is sign-free)."""
    if spot <= 0.0 or strike <= 0.0:
        raise ValueError(f"spot and strike must be positive: {spot=} {strike=}")
    if iv <= 0.0:
        raise ValueError(f"iv must be positive: {iv=}")
    t_years = max(float(dte_calendar_days), _MIN_T_DAYS) / 365.0
    vol = iv * math.sqrt(t_years)
    d1 = (math.log(spot / strike) + (risk_free - dividend_yield + 0.5 * iv * iv) * t_years) / vol
    if call_put == "C":
        return abs(norm_cdf(d1))
    return abs(norm_cdf(d1) - 1.0)
