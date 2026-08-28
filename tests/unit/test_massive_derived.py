"""Derived greeks: VWAP premium -> (iv, abs_delta) via the shared pricer.

Hermetic pure-math tests: every premium is produced by
`greeks.bs_price` itself (the model boundary is exercised end to end —
the same closed form prices and inverts), so round trips, refusals and
delta limits are exact hand cases. No network, no fixtures, no keys.
"""

from __future__ import annotations

import itertools
from dataclasses import FrozenInstanceError

import pytest

from tree_options.data.massive_client import MassiveError
from tree_options.data.massive_derived import (
    DEFAULT_PRICING_ASSUMPTIONS,
    MassiveDerivationError,
    PricingAssumptions,
    derived_abs_delta,
    implied_vol,
)
from tree_options.synth_options.greeks import CallPut, bs_price
from tree_options.synth_options.spec import OptionsOverlaySpec

SPOT = 100.0
DTE = 30
IV = 0.25
RF = DEFAULT_PRICING_ASSUMPTIONS.risk_free
Q = DEFAULT_PRICING_ASSUMPTIONS.dividend_yield


def _price(
    iv: float,
    strike: float,
    call_put: CallPut,
    *,
    spot: float = SPOT,
    dte: int = DTE,
    risk_free: float = RF,
) -> float:
    return bs_price(
        spot=spot,
        strike=strike,
        dte_calendar_days=dte,
        iv=iv,
        risk_free=risk_free,
        dividend_yield=Q,
        call_put=call_put,
    )


def _solve(premium: float, strike: float, call_put: CallPut, **overrides: object) -> float:
    kwargs: dict[str, object] = {
        "premium": premium,
        "spot": SPOT,
        "strike": strike,
        "dte_calendar_days": DTE,
        "call_put": call_put,
        "risk_free": RF,
        "dividend_yield": Q,
    }
    kwargs.update(overrides)
    return implied_vol(**kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize("strike", [80.0, 100.0, 120.0])
@pytest.mark.parametrize("call_put", ["C", "P"])
def test_implied_vol_round_trips_across_moneyness(strike: float, call_put: str) -> None:
    premium = _price(IV, strike, call_put)  # type: ignore[arg-type]
    solved = _solve(premium, strike, call_put)  # type: ignore[arg-type]
    assert solved == pytest.approx(IV, abs=1e-7)
    # the derived path agrees on iv and yields a sign-free delta in (0, 1)
    solved_2, abs_delta = derived_abs_delta(
        premium=premium,
        spot=SPOT,
        strike=strike,
        dte_calendar_days=DTE,
        call_put=call_put,  # type: ignore[arg-type]
        assumptions=DEFAULT_PRICING_ASSUMPTIONS,
    )
    assert solved_2 == pytest.approx(solved, abs=0.0)
    assert 0.0 < abs_delta < 1.0


def test_implied_vol_is_monotone_in_premium() -> None:
    strike = 120.0  # an OTM call: premium strictly increasing in iv
    solved = [_solve(_price(iv, strike, "C"), strike, "C") for iv in (0.15, 0.25, 0.35, 0.45)]
    assert all(later > earlier for earlier, later in itertools.pairwise(solved))
    for iv, recovered in zip((0.15, 0.25, 0.35, 0.45), solved, strict=True):
        assert recovered == pytest.approx(iv, abs=1e-7)


@pytest.mark.parametrize("premium", [0.0, -1.5])
def test_refuses_non_positive_premium(premium: float) -> None:
    with pytest.raises(MassiveDerivationError, match="is not positive"):
        _solve(premium, 100.0, "C")


def test_refuses_premium_below_the_lower_bound() -> None:
    price_lo = _price(1e-4, 90.0, "C")  # discounted intrinsic: ~10.22
    assert price_lo > 0.0
    with pytest.raises(MassiveDerivationError, match=r"lower vol bound.*lo=0\.0001.*arbitrage"):
        _solve(price_lo / 2.0, 90.0, "C")


def test_refuses_premium_above_the_upper_bound() -> None:
    price_hi = _price(5.0, 100.0, "C")
    with pytest.raises(MassiveDerivationError, match=r"upper vol bound.*hi=5\.0"):
        _solve(price_hi + 5.0, 100.0, "C")


def test_refuses_non_convergence_loudly() -> None:
    # tol=0.0 demands exact float equality, which bisection mids never
    # hit: the guard fires even though the bracket is monotone.
    premium = _price(0.6, 100.0, "C")
    with pytest.raises(MassiveDerivationError, match=r"did not converge in 10 iterations"):
        _solve(premium, 100.0, "C", tol=0.0, max_iterations=10)


def test_derivation_error_is_a_massive_error() -> None:
    assert issubclass(MassiveDerivationError, MassiveError)


def test_dte_zero_stays_priceable_via_the_half_day_floor() -> None:
    premium = _price(IV, 100.0, "C", dte=0)
    iv, abs_delta = derived_abs_delta(
        premium=premium,
        spot=SPOT,
        strike=100.0,
        dte_calendar_days=0,
        call_put="C",
        assumptions=DEFAULT_PRICING_ASSUMPTIONS,
    )
    assert iv == pytest.approx(IV, abs=1e-7)
    assert 0.45 < abs_delta < 0.55  # ATM under a half day of decay


def test_deep_itm_call_delta_tends_to_one() -> None:
    # priced at a low iv: deep ITM carries ~no time value, so the solve
    # lands in the flat low-vol region and |delta| pins to N(large d1)
    premium = _price(0.05, 50.0, "C")
    _, abs_delta = derived_abs_delta(
        premium=premium,
        spot=SPOT,
        strike=50.0,
        dte_calendar_days=DTE,
        call_put="C",
        assumptions=DEFAULT_PRICING_ASSUMPTIONS,
    )
    assert abs_delta > 0.9999


def test_far_otm_call_delta_stays_small() -> None:
    premium = _price(IV, 140.0, "C")
    iv, abs_delta = derived_abs_delta(
        premium=premium,
        spot=SPOT,
        strike=140.0,
        dte_calendar_days=DTE,
        call_put="C",
        assumptions=DEFAULT_PRICING_ASSUMPTIONS,
    )
    assert iv == pytest.approx(IV, abs=1e-6)
    assert 0.0 <= abs_delta < 0.001


def test_assumptions_defaults_pin_the_synthetic_world() -> None:
    spec = OptionsOverlaySpec(world_id="derived-pricing-pin", seed=1)
    defaults = PricingAssumptions()
    assert defaults.risk_free == spec.risk_free == 0.03
    assert defaults.dividend_yield == spec.dividend_yield == 0.0
    assert defaults.model == "black-scholes-1"
    assert defaults.version == "derived-pricing/1"
    assert DEFAULT_PRICING_ASSUMPTIONS == defaults
    with pytest.raises(FrozenInstanceError):
        defaults.risk_free = 0.05  # type: ignore[misc]


def test_module_docstring_states_the_landed_filter_seam() -> None:
    """(w6) Documentation truth, same rot the massive_overlay fix removed:
    the claim that `build_option_candidate_inputs` "keeps raising
    unconditionally" has been false since protocol 0.2.0 landed the builder
    and the G1 lane-2 surface feeds it. The docstring must describe the
    seam that exists — and stay honest that THIS module still contributes
    only pure scalar math to it, never a filter input of its own."""
    import re

    import tree_options.data.massive_derived as md

    # whitespace-normalized: the rot phrase wraps across source lines, so a
    # raw substring assert would pass vacuously over the line break
    doc = re.sub(r"\s+", " ", md.__doc__ or "")
    assert "keeps raising unconditionally" not in doc
    assert "reserved for a future owner-ratified amendment packet" not in doc
    assert "build_option_candidate_inputs" in doc
