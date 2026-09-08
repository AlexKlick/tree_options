"""M5 options attribution: exact dyadic fixtures, declared conventions."""

from __future__ import annotations

import math

import pytest

from tree_options.evaluation.attribution import (
    GreekAttribution,
    PositionGreeks,
    aggregate_attributions,
    attribute_position,
    attribution_share,
    leg_values,
)

# All fixtures are dyadic (0.5, 0.25, 0.125…) so every asserted value is
# EXACT in float64 — conservation is checked with ==, not approx.
GREEKS = PositionGreeks(delta=0.5, gamma=0.25, theta_per_year=-1.0, vega_per_vol=4.0)


def _fixture_row() -> GreekAttribution:
    return attribute_position(
        entry_value=2.0,
        exit_value=3.0,  # total 1.0
        greeks=GREEKS,
        underlying_move=0.5,  # delta 0.25; gamma 0.5*0.25*0.25 = 0.03125
        iv_change=0.0625,  # vega 4.0 * 0.0625 = 0.25
        year_fraction=0.125,  # theta -1.0 * 0.125 = -0.125
    )


def test_attribute_position_exact_legs_and_residual() -> None:
    row = _fixture_row()
    assert row.total_pnl == 1.0
    assert row.delta_pnl == 0.25
    assert row.gamma_pnl == 0.03125
    assert row.theta_pnl == -0.125
    assert row.vega_pnl == 0.25
    assert row.residual == 1.0 - (0.25 + 0.03125 - 0.125 + 0.25)  # 0.59375
    # conservation carries BY CONSTRUCTION, exactly for dyadic inputs
    assert row.delta_pnl + row.gamma_pnl + row.theta_pnl + row.vega_pnl + row.residual == 1.0


def test_zero_move_and_zero_changes_collapse_to_theta_plus_residual() -> None:
    row = attribute_position(
        entry_value=1.5,
        exit_value=1.25,  # total -0.25
        greeks=PositionGreeks(delta=2.0, gamma=1.0, theta_per_year=-2.0, vega_per_vol=8.0),
        underlying_move=0.0,
        iv_change=0.0,
        year_fraction=0.5,
    )
    assert row.delta_pnl == 0.0
    assert row.gamma_pnl == 0.0
    assert row.vega_pnl == 0.0
    assert row.theta_pnl == -1.0
    assert row.residual == 0.75  # -0.25 - (-1.0)


def test_attribute_position_validations() -> None:
    with pytest.raises(ValueError, match="year_fraction"):
        attribute_position(
            entry_value=1.0,
            exit_value=1.0,
            greeks=GREEKS,
            underlying_move=0.0,
            iv_change=0.0,
            year_fraction=-0.5,
        )
    with pytest.raises(ValueError, match="entry_value"):
        attribute_position(
            entry_value=float("nan"),
            exit_value=1.0,
            greeks=GREEKS,
            underlying_move=0.0,
            iv_change=0.0,
            year_fraction=0.0,
        )
    with pytest.raises(ValueError, match="gamma"):
        attribute_position(
            entry_value=1.0,
            exit_value=1.0,
            greeks=PositionGreeks(
                delta=0.0, gamma=float("inf"), theta_per_year=0.0, vega_per_vol=0.0
            ),
            underlying_move=0.0,
            iv_change=0.0,
            year_fraction=0.0,
        )


def test_aggregate_recomputes_residual_on_aggregate_numbers() -> None:
    row_a = _fixture_row()
    row_b = attribute_position(
        entry_value=1.0,
        exit_value=0.5,  # total -0.5
        greeks=GREEKS,
        underlying_move=-0.25,  # delta -0.125; gamma 0.5*0.25*0.0625 = 0.0078125
        iv_change=-0.125,  # vega -0.5
        year_fraction=0.25,  # theta -0.25
    )
    aggregate = aggregate_attributions([row_a, row_b])
    assert aggregate is not None
    assert aggregate.total_pnl == 0.5
    assert aggregate.delta_pnl == 0.125
    assert aggregate.gamma_pnl == 0.0390625
    assert aggregate.theta_pnl == -0.375
    assert aggregate.vega_pnl == -0.25
    # residual = aggregate total - aggregate legs (NOT sum of row residuals)
    assert aggregate.residual == 0.5 - (0.125 + 0.0390625 - 0.375 - 0.25)  # 0.9609375
    assert (
        aggregate.delta_pnl
        + aggregate.gamma_pnl
        + aggregate.theta_pnl
        + aggregate.vega_pnl
        + aggregate.residual
        == 0.5
    )
    assert aggregate_attributions([]) is None


def test_attribution_share_signed_and_refuses_zero_total() -> None:
    row = _fixture_row()
    assert attribution_share(row.delta_pnl, row) == 0.25
    assert attribution_share(row.theta_pnl, row) == -0.125
    assert attribution_share(row.residual, row) == 0.59375
    flat = attribute_position(
        entry_value=1.0,
        exit_value=1.0,
        greeks=PositionGreeks(delta=0.0, gamma=0.0, theta_per_year=0.0, vega_per_vol=0.0),
        underlying_move=0.0,
        iv_change=0.0,
        year_fraction=0.0,
    )
    assert attribution_share(0.0, flat) is None  # share of nothing
    with pytest.raises(ValueError, match="leg_value"):
        attribution_share(float("inf"), row)


def test_leg_values_excludes_residual_in_declared_order() -> None:
    row = _fixture_row()
    assert leg_values(row) == (0.25, 0.03125, -0.125, 0.25)
    assert math.fsum(leg_values(row)) + row.residual == row.total_pnl
