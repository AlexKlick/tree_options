"""Signed Black-Scholes greeks of a desk structure (lane E4).

Per leg, the implied vol is solved from the leg's mid with the repo's one
pricer inversion (``tree_options.data.massive_derived.implied_vol`` over
``synth_options.greeks.bs_price``), and the analytic Black-Scholes greeks
are evaluated at that vol under the SAME conventions:

- European exercise, dividend yield q = 0 (declared, not observed: there is
  no dividend feed; early exercise and dividends are ignored);
- the caller's flat risk-free ``rate``;
- time = calendar days to the leg's expiry / 365, floored at half a day
  exactly like ``bs_price`` (synth_options is hash-pinned: imported, never
  edited).

A leg whose vol can't be solved (no quote, a non-positive or crossed quote,
a premium the pricer's bracket can't reach, e.g. below intrinsic, a leg
already expired, no usable spot) makes the WHOLE structure NOT_EVALUABLE:
``structure_greeks`` returns None. A partial sum would be a guess.

Units are the whole position's (x100 multiplier x quantity, each leg signed
+1 BUY / -1 SELL by its action in the opened position):

- ``delta_shares``: share-equivalent delta;
- ``gamma_shares_per_usd``: change of delta_shares per $1 move of spot;
- ``vega_usd_per_volpt``: dollars per vol point (0.01 of sigma);
- ``theta_usd_per_day``: dollars per calendar day.

Floats by design: this is a model boundary (as in massive_derived). Money
stays Decimal outside it; nothing here produces a price.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from tree_options.data.massive_derived import MassiveDerivationError, implied_vol
from tree_options.synth_options.greeks import _MIN_T_DAYS, norm_cdf
from tree_options.trex.plan import Leg, LegStructure

MULTIPLIER = 100
VOL_POINT = 0.01
DAYS_PER_YEAR = 365.0

LegQuote = tuple[Decimal, Decimal]  # (bid, ask) of one leg's contract


@dataclass(frozen=True)
class StructureGreeks:
    """A whole position's greeks (see the module docstring for units)."""

    delta_shares: float
    gamma_shares_per_usd: float
    vega_usd_per_volpt: float
    theta_usd_per_day: float


def structure_greeks(
    spec: LegStructure,
    leg_quotes: Sequence[LegQuote | None],
    spot: Decimal,
    rate: float,
    as_of: date,
) -> StructureGreeks | None:
    """The position's greeks from one quote per leg (``leg_quotes[i]`` is
    ``spec.legs[i]``'s, e.g. IbkrTrex.leg_quote(spec.id, i)), or None when
    any leg is not evaluable. ``as_of`` dates the time to expiry (calendar
    days); a datetime counts by its own date."""
    if len(leg_quotes) != len(spec.legs):
        raise ValueError(f"{spec.id}: {len(leg_quotes)} leg quotes for {len(spec.legs)} legs")
    if not spot.is_finite() or spot <= 0:
        return None
    day = as_of.date() if isinstance(as_of, datetime) else as_of
    s = float(spot)
    delta = gamma = vega = theta = 0.0
    for leg, quote in zip(spec.legs, leg_quotes, strict=True):
        per_share = _leg_greeks(leg, quote, s, float(rate), day)
        if per_share is None:
            return None
        units = leg.sign * MULTIPLIER * spec.quantity
        delta += units * per_share[0]
        gamma += units * per_share[1]
        vega += units * per_share[2] * VOL_POINT
        theta += units * per_share[3] / DAYS_PER_YEAR
    return StructureGreeks(delta, gamma, vega, theta)


def _mid(quote: LegQuote | None) -> float | None:
    if quote is None:
        return None
    bid, ask = quote
    if not (bid.is_finite() and ask.is_finite()) or bid < 0 or ask <= 0 or bid > ask:
        return None
    return float((bid + ask) / 2)


def _leg_greeks(
    leg: Leg, quote: LegQuote | None, spot: float, rate: float, as_of: date
) -> tuple[float, float, float, float] | None:
    """(delta, gamma, vega per 1.00 of sigma, theta per YEAR) of one long
    contract share, at the vol its mid implies; None when not solvable."""
    premium = _mid(quote)
    days = (leg.expiry - as_of).days
    if premium is None or days < 0:
        return None
    strike = float(leg.strike)
    try:
        iv = implied_vol(
            premium=premium,
            spot=spot,
            strike=strike,
            dte_calendar_days=days,
            call_put=leg.right,
            risk_free=rate,
            dividend_yield=0.0,
        )
    except (MassiveDerivationError, ValueError):
        return None
    t = max(float(days), _MIN_T_DAYS) / DAYS_PER_YEAR
    root_t = math.sqrt(t)
    d1 = (math.log(spot / strike) + (rate + 0.5 * iv * iv) * t) / (iv * root_t)
    d2 = d1 - iv * root_t
    density = math.exp(-0.5 * d1 * d1) / math.sqrt(2.0 * math.pi)
    discounted_strike = strike * math.exp(-rate * t)
    decay = -spot * density * iv / (2.0 * root_t)
    gamma = density / (spot * iv * root_t)
    vega = spot * density * root_t
    if leg.right == "C":
        return norm_cdf(d1), gamma, vega, decay - rate * discounted_strike * norm_cdf(d2)
    return norm_cdf(d1) - 1.0, gamma, vega, decay + rate * discounted_strike * norm_cdf(-d2)
