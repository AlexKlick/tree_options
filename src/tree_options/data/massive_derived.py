"""Model-derived greeks for the Massive/Polygon free tier (M4 $0 lane).

The free tier carries NO greeks (probed 2026-08-21: the snapshot endpoint
answers HTTP 200 `NOT_AUTHORIZED`), but it DOES serve per-contract DAILY
AGGREGATES with a real VWAP. This module implements the ruled workaround:
invert the repo's own Black-Scholes pricer — the M3 synthetic-era,
stdlib-only `tree_options.synth_options.greeks.bs_price` — on a VWAP
premium, then evaluate the analytic |delta| at the solved iv. The pricer
is shared with the synthetic world on purpose: derived greeks stay
semantically consistent with the M2/M3 era (same model, same rate
convention), instead of importing a second pricing stack that can drift.

THE FLOAT BOUNDARY (single, documented). Every argument to the solvers
here is a float BY DESIGN — this is the one sanctioned float island of
the derived lane, and it exists because bisection iterates on prices.
Callers convert a Decimal VWAP to float EXACTLY ONCE at the call site
and carry the Decimal alongside for every exact/monetary downstream use.
Nothing in this module converts, rounds, stores, or re-serializes a
price; in particular nothing here ever produces a `str(float)` that
could launder a binary approximation into a Decimal field.

ASSUMPTIONS ARE DECLARED, NOT OBSERVED. No free rates feed exists on
this tier, so `PricingAssumptions` states the rates used for derivation.
Its defaults mirror the synthetic world's `OptionsOverlaySpec` defaults
(`risk_free=0.03`, `dividend_yield=0.0`), pinned by a test that asserts
equality against the loaded spec values. That spec carries ONE FLAT
value per overlay applied to every underlying and expiry — there is no
per-underlying rate mechanism in the synthetic world, so none is
mirrored here. The `version` string lets every derived output name the
assumptions it was derived under.

NOT WIRED INTO THE FILTER. Nothing here feeds the M3 candidate filter:
`massive_options.build_option_candidate_inputs` keeps raising
unconditionally, and lifting that gate is reserved for a future
owner-ratified amendment packet. These are pure functions over scalars.
"""

from __future__ import annotations

from dataclasses import dataclass

from tree_options.data.massive_client import MassiveError
from tree_options.synth_options.greeks import CallPut, bs_abs_delta, bs_price


@dataclass(frozen=True)
class PricingAssumptions:
    """The declared pricing inputs of the derived lane.

    No free rates feed exists on the free tier, so these are DECLARED
    assumptions, versioned so every derived output can name them. The
    defaults are the synthetic world's own (`OptionsOverlaySpec`:
    `risk_free=0.03`, `dividend_yield=0.0`) so that derived greeks and
    synthetic greeks are computed under identical conventions; the
    synthetic spec applies one flat value per overlay to every
    underlying and expiry, and this dataclass mirrors that shape.

    `model` names the pricing model (the shared `bs_price`), `version`
    versions this assumption set: change either and derived outputs can
    name what they were computed under.
    """

    risk_free: float = 0.03
    dividend_yield: float = 0.0
    model: str = "black-scholes-1"
    version: str = "derived-pricing/1"


# Immutable, so one shared instance is a safe default (BackoffPolicy precedent).
DEFAULT_PRICING_ASSUMPTIONS = PricingAssumptions()


class MassiveDerivationError(MassiveError):
    """A model derivation refused to run: the premium is unpriceable by
    the shared pricer under the given bound/assumptions, or the solver
    failed to converge. The message names the bound and the numbers."""


def implied_vol(
    *,
    premium: float,
    spot: float,
    strike: float,
    dte_calendar_days: int,
    call_put: CallPut,
    risk_free: float,
    dividend_yield: float,
    lo: float = 1e-4,
    hi: float = 5.0,
    tol: float = 1e-10,
    max_iterations: int = 200,
) -> float:
    """The iv under which `bs_price` reprices `premium` exactly.

    Bisection on the shared pricer: `bs_price` is monotone increasing
    in iv for a positive T (which `bs_price` guarantees by flooring T at
    half a day, so `dte_calendar_days=0` is priceable), so halving a
    bracket that already straddles the premium is exhaustive. Iteration
    stops in the PRICE domain — the first midpoint whose price is within
    `tol` of the premium wins — because price tolerance, not iv
    tolerance, is what a caller can actually interpret.

    Floats are BY DESIGN: this is the single model boundary of the
    derived lane. Callers convert a Decimal VWAP to float exactly once
    and carry the Decimal alongside; see the module docstring.

    Refuses (fail closed, `MassiveDerivationError` naming the bound and
    the numbers — never a silent clamp to a bracket edge):

    - `premium <= 0` — no vol prices a non-positive premium;
    - `premium < bs_price(iv=lo)` — below the nearly-zero-vol price,
      i.e. under discounted intrinsic: arbitrage or degenerate data;
    - `premium > bs_price(iv=hi)` — the bracket cannot reach it;
    - no convergence within `max_iterations` — impossible with a
      monotone bracket in double precision, but failed at loudly anyway.
    """
    if premium <= 0.0:
        raise MassiveDerivationError(
            f"implied_vol: premium {premium!r} is not positive — no implied vol"
            f" prices it (spot={spot!r} strike={strike!r}"
            f" dte={dte_calendar_days} call_put={call_put!r})"
        )
    price_lo = bs_price(
        spot=spot,
        strike=strike,
        dte_calendar_days=dte_calendar_days,
        iv=lo,
        risk_free=risk_free,
        dividend_yield=dividend_yield,
        call_put=call_put,
    )
    price_hi = bs_price(
        spot=spot,
        strike=strike,
        dte_calendar_days=dte_calendar_days,
        iv=hi,
        risk_free=risk_free,
        dividend_yield=dividend_yield,
        call_put=call_put,
    )
    if premium < price_lo:
        raise MassiveDerivationError(
            f"implied_vol: premium {premium!r} is below the lower vol bound:"
            f" bs_price at lo={lo!r} is {price_lo!r} (the nearly-zero-vol /"
            f" discounted-intrinsic price — under it is arbitrage or degenerate"
            f" data); spot={spot!r} strike={strike!r} dte={dte_calendar_days}"
            f" call_put={call_put!r} risk_free={risk_free!r}"
            f" dividend_yield={dividend_yield!r}"
        )
    if premium > price_hi:
        raise MassiveDerivationError(
            f"implied_vol: premium {premium!r} is above the upper vol bound:"
            f" bs_price at hi={hi!r} is {price_hi!r} — the bracket"
            f" [lo={lo!r}, hi={hi!r}] cannot price it (spot={spot!r}"
            f" strike={strike!r} dte={dte_calendar_days} call_put={call_put!r})"
        )
    for _ in range(max_iterations):
        mid = 0.5 * (lo + hi)
        price_mid = bs_price(
            spot=spot,
            strike=strike,
            dte_calendar_days=dte_calendar_days,
            iv=mid,
            risk_free=risk_free,
            dividend_yield=dividend_yield,
            call_put=call_put,
        )
        if abs(price_mid - premium) <= tol:
            return mid
        if price_mid < premium:
            lo = mid
        else:
            hi = mid
    raise MassiveDerivationError(
        f"implied_vol: bisection did not converge in {max_iterations} iterations"
        f" (premium={premium!r}, final bracket [{lo!r}, {hi!r}], tol={tol!r})"
        f" — impossible with a monotone bracket; failing loud"
    )


def derived_abs_delta(
    *,
    premium: float,
    spot: float,
    strike: float,
    dte_calendar_days: int,
    call_put: CallPut,
    assumptions: PricingAssumptions,
) -> tuple[float, float]:
    """`(iv, abs_delta)` derived from one VWAP premium.

    The iv is solved by `implied_vol` under `assumptions`; the |delta|
    is the shared analytic `bs_abs_delta` evaluated at that solved iv —
    the same closed form the synthetic world uses, so a derived delta
    and a synthetic delta at the same (contract, iv) agree by
    construction. Both outputs are floats (the model boundary): callers
    round Decimal-ward at their own exact fields.

    Refuses exactly as `implied_vol` does; the assumptions arrive as a
    versioned object so every derivation names its inputs.
    """
    iv = implied_vol(
        premium=premium,
        spot=spot,
        strike=strike,
        dte_calendar_days=dte_calendar_days,
        call_put=call_put,
        risk_free=assumptions.risk_free,
        dividend_yield=assumptions.dividend_yield,
    )
    abs_delta = bs_abs_delta(
        spot=spot,
        strike=strike,
        dte_calendar_days=dte_calendar_days,
        iv=iv,
        risk_free=assumptions.risk_free,
        dividend_yield=assumptions.dividend_yield,
        call_put=call_put,
    )
    return iv, abs_delta


__all__ = [
    "DEFAULT_PRICING_ASSUMPTIONS",
    "MassiveDerivationError",
    "PricingAssumptions",
    "derived_abs_delta",
    "implied_vol",
]
