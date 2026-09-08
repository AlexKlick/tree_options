"""M5 options attribution: greeks-at-entry P&L decomposition.

Pure, stdlib-only, deterministic — the conventions of
``evaluation.stats`` and ``evaluation.portfolio``: ``None`` for unevaluable
quantities, exact validation, ``math.fsum``, declared estimators wherever
the literature varies.

Conventions pinned here: the decomposition is the first-order Taylor
expansion of the position value around the ENTRY state — greeks are taken
at entry ONLY (never at exit, never averaged), so the legs are the
declared attribution basis and everything the expansion misses lands in a
reported ``residual`` that is NEVER folded into a leg or zeroed (a
decomposition that hides its interaction/cross terms lies about where the
P&L came from).  Units are declared per position and already scaled by the
contract multiplier: ``delta`` is per unit of underlying move,
``gamma`` per unit of squared underlying move, ``theta_per_year`` per YEAR
of calendar time (caller supplies ``year_fraction``, e.g. days/365), and
``vega_per_vol`` per unit of DECIMAL volatility (iv_change 0.01 == one vol
point).  ``total_pnl`` is gross of costs — fees and slippage belong to
``portfolio.cost_bridge``, not here.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class PositionGreeks:
    """Greeks of one position at its ENTRY state, per position."""

    delta: float  # per unit underlying move
    gamma: float  # per unit squared underlying move
    theta_per_year: float  # per YEAR of calendar time
    vega_per_vol: float  # per unit of decimal volatility


@dataclass(frozen=True)
class GreekAttribution:
    """P&L decomposition of one position (or one aggregated book).

    Conservation: ``delta_pnl + gamma_pnl + theta_pnl + vega_pnl +
    residual == total_pnl`` to float rounding — ``residual`` is DEFINED as
    the difference, so the identity carries by construction.
    """

    delta_pnl: float
    gamma_pnl: float
    theta_pnl: float
    vega_pnl: float
    residual: float
    total_pnl: float


def _require_finite(value: float, *, name: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


def attribute_position(
    *,
    entry_value: float,
    exit_value: float,
    greeks: PositionGreeks,
    underlying_move: float,
    iv_change: float,
    year_fraction: float,
) -> GreekAttribution:
    """First-order Taylor attribution of one position's gross P&L.

    ``underlying_move`` is S_exit - S_entry; ``iv_change`` is
    iv_exit - iv_entry (decimal); ``year_fraction`` is the holding period
    in years (>= 0).  The legs are evaluated with ENTRY greeks; the
    residual carries everything the expansion does not explain.
    """
    entry = _require_finite(entry_value, name="entry_value")
    exit_ = _require_finite(exit_value, name="exit_value")
    move = _require_finite(underlying_move, name="underlying_move")
    iv = _require_finite(iv_change, name="iv_change")
    years = _require_finite(year_fraction, name="year_fraction")
    if years < 0:
        raise ValueError("year_fraction must be >= 0")
    delta = _require_finite(greeks.delta, name="delta")
    gamma = _require_finite(greeks.gamma, name="gamma")
    theta = _require_finite(greeks.theta_per_year, name="theta_per_year")
    vega = _require_finite(greeks.vega_per_vol, name="vega_per_vol")

    total = exit_ - entry
    delta_pnl = delta * move
    gamma_pnl = 0.5 * gamma * move * move
    theta_pnl = theta * years
    vega_pnl = vega * iv
    residual = total - (delta_pnl + gamma_pnl + theta_pnl + vega_pnl)
    return GreekAttribution(
        delta_pnl=delta_pnl,
        gamma_pnl=gamma_pnl,
        theta_pnl=theta_pnl,
        vega_pnl=vega_pnl,
        residual=residual,
        total_pnl=total,
    )


def aggregate_attributions(
    rows: Iterable[GreekAttribution],
) -> GreekAttribution | None:
    """Sum the legs of many attributions into one book-level row.

    The aggregate's ``residual`` is recomputed as the aggregated total
    minus the aggregated legs (NOT the sum of individual residuals) so the
    conservation identity holds on the AGGREGATE numbers a reader will
    actually check; individual residuals remain informative per position.
    ``None`` for an empty book.
    """
    materialized = tuple(rows)
    if not materialized:
        return None
    total = math.fsum(row.total_pnl for row in materialized)
    delta_pnl = math.fsum(row.delta_pnl for row in materialized)
    gamma_pnl = math.fsum(row.gamma_pnl for row in materialized)
    theta_pnl = math.fsum(row.theta_pnl for row in materialized)
    vega_pnl = math.fsum(row.vega_pnl for row in materialized)
    residual = total - (delta_pnl + gamma_pnl + theta_pnl + vega_pnl)
    return GreekAttribution(
        delta_pnl=delta_pnl,
        gamma_pnl=gamma_pnl,
        theta_pnl=theta_pnl,
        vega_pnl=vega_pnl,
        residual=residual,
        total_pnl=total,
    )


def attribution_share(leg_value: float, attribution: GreekAttribution) -> float | None:
    """One leg's share of the SIGNED total P&L.

    ``None`` when the total is zero — a share of nothing is not a number
    this module will fabricate.  Shares are honest about sign and scale: a
    leg can exceed the total (share > 1) when residual opposes it, and a
    losing book reports signed shares, never absolute-value whitewash.
    """
    value = _require_finite(leg_value, name="leg_value")
    if attribution.total_pnl == 0.0:
        return None
    return value / attribution.total_pnl


def leg_values(attribution: GreekAttribution) -> Sequence[float]:
    """The four expansion legs in declared order (delta, gamma, theta, vega).

    Excludes ``residual`` — callers attributing the book to FACTORS want
    the explained legs; the residual is carried separately and must never
    silently join a factor leg.
    """
    return (
        attribution.delta_pnl,
        attribution.gamma_pnl,
        attribution.theta_pnl,
        attribution.vega_pnl,
    )
