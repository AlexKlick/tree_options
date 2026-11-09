"""Synthetic spread pricing for AI-BEAR-001 (declared, not measured).

Each leg is priced with ``synth_options.greeks.bs_price`` at a strike-dependent
IV, ``atm_iv x max(skew_floor, 1 + skew_beta x ln(S / K))``. That raises
the IV of out-of-the-money puts and lowers it for out-of-the-money calls,
which is the sign of the equity skew. The sealed skew_beta = 0.5 is a
declared assumption; flat IV is a reported sensitivity.

Costs: every leg fill crosses a half-spread of
``max(floor, pct x leg_mid)`` and pays the commission. The pct values come
from the 2026-11-09 calibration of recorded CBOE chains (median half-spread
as a share of mid, by name and tenor; log sha256 in the parameters).
Closing a spread whose bid side is worth less than nothing is not modeled;
the position is abandoned at zero instead (a debit spread may expire).

All prices are per share on a spot normalized to ``spot_normalized_to`` at
entry, so trades on a $20 stock and a 20,000-point index compare directly.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Literal

from tree_options.synth_options.greeks import bs_price

Right = Literal["C", "P"]


@dataclass(frozen=True)
class PricingParams:
    skew_beta: float
    skew_floor: float
    half_spread_pct: dict[str, float]  # "NDX" / "SOX" / "single"
    half_spread_floor: float
    commission_per_share: float  # commission per leg fill / multiplier

    @classmethod
    def from_doc(cls, doc: dict[str, Any], *, flat_skew: bool = False) -> PricingParams:
        pr, co = doc["pricing"], doc["costs"]
        return cls(
            skew_beta=0.0 if flat_skew else float(pr["skew_beta"]),
            skew_floor=float(pr["skew_floor"]),
            half_spread_pct={k: float(v) for k, v in co["half_spread_pct_of_mid"].items()},
            half_spread_floor=float(co["half_spread_floor_per_share"]),
            commission_per_share=float(co["commission_per_leg_fill"]) / int(co["multiplier"]),
        )

    def pct_for(self, cost_class: str) -> float:
        return self.half_spread_pct.get(cost_class, self.half_spread_pct["single"])


def leg_iv(atm_iv: float, spot: float, strike: float, p: PricingParams) -> float:
    return atm_iv * max(p.skew_floor, 1.0 + p.skew_beta * math.log(spot / strike))


def leg_mid(
    *,
    spot: float,
    strike: float,
    dte: int,
    atm_iv: float,
    rate: float,
    right: Right,
    p: PricingParams,
) -> float:
    return bs_price(
        spot=spot,
        strike=strike,
        dte_calendar_days=dte,
        iv=leg_iv(atm_iv, spot, strike, p),
        risk_free=rate,
        dividend_yield=0.0,
        call_put=right,
    )


@dataclass(frozen=True)
class SpreadQuote:
    long_mid: float
    short_mid: float
    long_half: float
    short_half: float
    commission: float  # both legs, one side (open OR close)

    @property
    def mid(self) -> float:
        return self.long_mid - self.short_mid

    @property
    def open_debit(self) -> float:
        """Pay the long's ask, receive the short's bid, plus commissions."""
        return (
            (self.long_mid + self.long_half) - (self.short_mid - self.short_half) + self.commission
        )

    @property
    def close_proceeds(self) -> float:
        """Sell the long at its bid, buy back the short at its ask, less
        commissions; never below zero (abandon instead of paying to close)."""
        raw = (
            (self.long_mid - self.long_half) - (self.short_mid + self.short_half) - self.commission
        )
        return max(raw, 0.0)


def quote_spread(
    *,
    spot: float,
    k_long: float,
    k_short: float,
    dte: int,
    atm_iv: float,
    rate: float,
    right: Right,
    cost_class: str,
    p: PricingParams,
) -> SpreadQuote:
    lm = leg_mid(spot=spot, strike=k_long, dte=dte, atm_iv=atm_iv, rate=rate, right=right, p=p)
    sm = leg_mid(spot=spot, strike=k_short, dte=dte, atm_iv=atm_iv, rate=rate, right=right, p=p)
    pct = p.pct_for(cost_class)
    return SpreadQuote(
        long_mid=lm,
        short_mid=sm,
        long_half=max(p.half_spread_floor, pct * lm),
        short_half=max(p.half_spread_floor, pct * sm),
        commission=2.0 * p.commission_per_share,
    )
