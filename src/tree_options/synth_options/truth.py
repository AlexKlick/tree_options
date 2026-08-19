"""OptionsOverlayTruth: the evaluation-side mirror of the overlay recipe.

The realization (which underlyings were eligible when, which contracts
existed, what the chains quoted) is a deterministic function of the spec +
the parent world's records and is pinned by the options registry's
sample-slice hashes and analytic counts — the truth sidecar carries the
recipe and summary statistics, not the realization. Import-blocked outside
`tree_options.synth_options.*` (mirroring the v1 WorldTruth boundary).

Generating equations (the complete list — parameters live in
OptionsOverlaySpec, engineering constants in generate.py):

- eligibility: per session t, the top `eligible_top_n` underlyings by the
  median of close*volume over the trailing `eligibility_window_bars` bars
  (min `min_eligible_bars` bars required), ranked from the world's own
  bars — point-in-time-honest by construction. Registry nulls and alphas
  are independent worlds (v1 seeds every stream with world_id); no
  twin-sharing is assumed anywhere in M3.
- IV plant: iv(u, E) = iv_base(u) * iv_mult(u, E), each drawn once from
  name-keyed streams; CONSTANT across sessions for a given (u, E) — a
  declared simplification that keeps delta internally consistent.
- premium: Black-Scholes (greeks.bs_price) at the session close mid.
- half-spread: atm_half_spread_fraction * (1 + wing_spread_scale*|ln(S/K)|)
  of mid; ask rounded UP to the tick, bid rounded DOWN and floored at
  0.00 — deep wings naturally quantize to zero-bid markets.
- OI/volume shape: oi_base_atm * exp(-(ln m)^2/(2*oi_moneyness_width^2))
  * exp(-DTE/oi_tenor_days) with lognormal noise; volume zero on an
  `untraded_fraction` draw of contract-days.
"""

from __future__ import annotations

from pydantic import Field

from tree_options.schemas.common import StrictModel
from tree_options.synth_options.spec import OptionsOverlaySpec


class OptionsOverlayTruth(StrictModel):
    world_id: str
    seed: int
    spec: OptionsOverlaySpec
    n_underlyings_ever_eligible: int = Field(ge=0)
    n_file_sessions: int = Field(ge=0)
    contract_count: int = Field(ge=0)
