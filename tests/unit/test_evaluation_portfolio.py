"""M5 portfolio diagnostics: exact hand-computed fixtures, declared conventions."""

from __future__ import annotations

import datetime as dt

import pytest

from tree_options.evaluation.portfolio import (
    annualized_sharpe,
    cagr,
    calmar_ratio,
    compounded_return,
    cost_bridge,
    excess_over_baseline,
    hit_rate,
    matched_risk_scale,
    max_drawdown,
    profit_factor,
    slice_stability,
    sortino_ratio,
)

D1, D2, D3, D4, D5 = (
    dt.date(2026, 1, 5),
    dt.date(2026, 1, 6),
    dt.date(2026, 1, 7),
    dt.date(2026, 1, 8),
    dt.date(2026, 1, 9),
)


# ---- sharpe / sortino -----------------------------------------------------------------


def test_sharpe_zero_mean_and_formula_transcription() -> None:
    assert annualized_sharpe([0.01, -0.01]) == 0.0
    sample = [0.02, 0.01, 0.03, 0.0]
    mean = sum(sample) / 4
    var = sum((v - mean) ** 2 for v in sample) / 3
    assert annualized_sharpe(sample) == pytest.approx(mean / var**0.5 * 252**0.5)
    assert annualized_sharpe(sample, periods_per_year=1.0) == pytest.approx(mean / var**0.5)
    assert annualized_sharpe([0.01, 0.01, 0.01]) is None  # zero variance
    assert annualized_sharpe([0.01]) is None
    with pytest.raises(ValueError, match="periods_per_year"):
        annualized_sharpe([0.01, 0.02], periods_per_year=0.0)


def test_sortino_full_sample_downside_denominator() -> None:
    sample = [0.01, -0.02, 0.01, -0.02]
    # mean -0.005; shortfalls 0, .02, 0, .02 -> dd = sqrt(.0002)
    assert sortino_ratio(sample) == pytest.approx(-0.005 / (0.0002**0.5) * 252**0.5)
    assert sortino_ratio(sample, target_return=0.01) == pytest.approx(
        (-0.015) / ((sum(max(0.01 - v, 0.0) ** 2 for v in sample) / 4) ** 0.5) * 252**0.5
    )
    assert sortino_ratio([0.01, 0.02]) is None  # no downside sessions
    assert sortino_ratio([0.01]) is None
    with pytest.raises(ValueError, match="target_return"):
        sortino_ratio([0.01, -0.01], target_return=float("nan"))


# ---- compounded curve -----------------------------------------------------------------


def test_compounded_return_and_ruin_refusal() -> None:
    assert compounded_return([0.1, 0.1]) == pytest.approx(0.21)
    assert compounded_return([-0.5, 1.0]) == pytest.approx(0.0)
    with pytest.raises(ValueError, match="less than or equal to -1"):
        compounded_return([-1.0])
    with pytest.raises(ValueError, match="finite"):
        compounded_return([float("inf")])


def test_cagr_geometric_annualization() -> None:
    assert cagr([0.1, 0.1], periods_per_year=2.0) == pytest.approx(0.21)
    assert cagr([0.1, 0.1], periods_per_year=4.0) == pytest.approx(1.21**2 - 1.0)  # 0.4641
    assert cagr([]) is None


def test_cagr_log_space_avoids_overflow_and_underflow() -> None:
    """Long-running series must survive log-space arithmetic.

    With ``periods_per_year=1`` the annualization is the geometric mean of
    the per-session growths, so 1024 doublings give a per-year rate of 1.0
    (100%) and 54 halvings give a per-year rate of -0.5 (-50%) — the
    multiplicative form blew up / vanished in float64 prod under exactly
    these inputs; log-space keeps both finite.
    """
    assert cagr([1.0] * 1024, periods_per_year=1) == pytest.approx(1.0)
    assert cagr([-0.5] * 54, periods_per_year=1) == pytest.approx(-0.5)


def test_max_drawdown_depth_location_and_recovery() -> None:
    result = max_drawdown([0.10, -0.10, 0.05, 0.10, -0.05], sessions=[D1, D2, D3, D4, D5])
    assert result is not None
    # curve 1 -> 1.1 -> 0.99 -> 1.0395 -> 1.14345 -> 1.0862775
    assert result.depth == pytest.approx(-0.1)
    assert (result.peak_session, result.trough_session) == (D1, D2)
    assert result.recovered_session == D4  # 1.14345 >= 1.1
    underwater = max_drawdown([0.1, -0.2], sessions=[D1, D2])
    assert underwater is not None
    assert underwater.depth == pytest.approx(-0.2)
    assert underwater.recovered_session is None
    rising = max_drawdown([0.01, 0.02], sessions=[D1, D2])
    assert rising is not None
    assert rising.depth == 0.0
    assert rising.recovered_session is None  # no drawdown, no recovery event
    assert max_drawdown([]) is None
    with pytest.raises(ValueError, match="align"):
        max_drawdown([0.01], sessions=[D1, D2])
    # alignment validation runs BEFORE the empty short-circuit
    with pytest.raises(ValueError, match="align"):
        max_drawdown([], sessions=[D1])


def test_max_drawdown_presample_peak_is_none_not_first_session() -> None:
    """When the recorded peak is the pre-sample origin, no session closed there."""
    result = max_drawdown([-0.1, 0.0], sessions=[D1, D2])
    assert result is not None
    assert result.depth == pytest.approx(-0.1)
    assert result.peak_session is None  # the peak was pre-sample, NOT D1
    assert result.trough_session == D1


def test_max_drawdown_recovery_equality_boundary() -> None:
    """The `>=` boundary is pinned: D3 closes exactly at the peak (1.25 == 1.25)."""
    result = max_drawdown([0.25, -0.2, 0.25], sessions=[D1, D2, D3])
    assert result is not None
    # curve 1.0 -> 1.25 -> 1.0 -> 1.25; peak at curve index 1 = 1.25; trough at 2 = 1.0
    assert result.depth == pytest.approx(-0.2)
    assert result.peak_session == D1
    assert result.trough_session == D2
    assert result.recovered_session == D3  # 1.25 >= 1.25 — the boundary case


def test_calmar_classic_definition() -> None:
    assert calmar_ratio([0.1, -0.1], periods_per_year=2.0) == pytest.approx(-0.1)
    # total 1.1*0.9 = 0.99 -> CAGR -0.01 over one year; depth 0.1
    assert calmar_ratio([0.01, 0.02]) is None  # no drawdown to divide by
    assert calmar_ratio([]) is None


# ---- hit rate / profit factor ----------------------------------------------------------


def test_profit_factor_and_hit_rate_exact() -> None:
    assert profit_factor([0.05, -0.02, 0.03, -0.01]) == pytest.approx(8.0 / 3.0)
    assert profit_factor([0.01, 0.02]) is None  # infinite, not fabricated
    assert profit_factor([]) is None
    assert hit_rate([0.01, -0.01, 0.0, 0.02]) == 0.5  # zero sessions never count
    assert hit_rate([]) is None


# ---- cost bridge ------------------------------------------------------------------------


def test_cost_bridge_telescopes_gross_to_net() -> None:
    bridge = cost_bridge(
        [0.01, 0.02],
        turnovers=[1.0, 1.0],
        fee_bps_per_side=10.0,
        slippage_bps_per_side=10.0,
        periods_per_year=2.0,
    )
    assert bridge.gross_cagr == pytest.approx(1.01 * 1.02 - 1.0)  # 0.0302
    # fee 10bps -> [0.009, 0.019]; plus slippage 10bps -> [0.008, 0.018]
    assert bridge.net_cagr == pytest.approx(1.008 * 1.018 - 1.0)  # 0.026144
    assert bridge.fee_drag_cagr == pytest.approx(0.0302 - (1.009 * 1.019 - 1.0))
    assert bridge.slippage_drag_cagr == pytest.approx((1.009 * 1.019 - 1.0) - 0.026144)
    # the waterfall telescopes: gross - fees - slippage == net
    assert bridge.gross_cagr - bridge.fee_drag_cagr - bridge.slippage_drag_cagr == pytest.approx(
        bridge.net_cagr
    )
    assert bridge.round_trip_cost_fraction == pytest.approx(0.002)
    with pytest.raises(ValueError, match="same length"):
        cost_bridge([0.01], turnovers=[1.0, 1.0], fee_bps_per_side=1, slippage_bps_per_side=1)
    with pytest.raises(ValueError, match="cannot be negative"):
        cost_bridge([0.01], turnovers=[-0.5], fee_bps_per_side=1, slippage_bps_per_side=1)
    with pytest.raises(ValueError, match="fee_bps_per_side"):
        cost_bridge([0.01], turnovers=[1.0], fee_bps_per_side=-1, slippage_bps_per_side=1)


def test_cost_bridge_turnover_multiplier_changes_the_charge() -> None:
    """Zero and 2x turnovers pin the per-side multiplier is actually applied."""
    # session 0 turnover=0 -> no charge; session 1 turnover=2 -> double charge
    # gross: 0.01, 0.02; fee 10bps + slip 10bps = 20bps = 0.002 per unit turnover
    bridge = cost_bridge(
        [0.01, 0.02],
        turnovers=[0.0, 2.0],
        fee_bps_per_side=10.0,
        slippage_bps_per_side=10.0,
        periods_per_year=2.0,
    )
    # fee_only = [0.01, 0.02 - 2*0.001] = [0.01, 0.018]
    # with_slippage = [0.01, 0.018 - 2*0.002 + 0.001] = [0.01, 0.016]
    # Wait: per_side = 0.002; with_slippage = g - c*0.002
    # session 0: 0.01 - 0 = 0.01
    # session 1: 0.02 - 2*0.002 = 0.016
    assert bridge.net_cagr == pytest.approx(1.01 * 1.016 - 1.0)  # 0.026256
    assert bridge.fee_drag_cagr == pytest.approx(
        (1.01 * 1.02 - 1.0) - (1.01 * 1.018 - 1.0)  # 0.0302 - 0.028318 = 0.001882
    )
    assert bridge.slippage_drag_cagr == pytest.approx(
        (1.01 * 1.018 - 1.0) - (1.01 * 1.016 - 1.0)
    )
    assert bridge.round_trip_cost_fraction == pytest.approx(0.002)


# ---- matched risk / baselines / stability -----------------------------------------------


def test_matched_risk_scale_and_refusals() -> None:
    assert matched_risk_scale(0.10, 0.20) == pytest.approx(0.5)
    assert matched_risk_scale(0.30, 0.10) == pytest.approx(3.0)  # uncapped by declaration
    with pytest.raises(ValueError, match="target_volatility"):
        matched_risk_scale(0.0, 0.1)
    with pytest.raises(ValueError, match="realized_volatility"):
        matched_risk_scale(0.1, 0.0)


def test_excess_over_baseline_aligned() -> None:
    assert excess_over_baseline([0.02, -0.01], [0.01, 0.01]) == (0.01, -0.02)
    with pytest.raises(ValueError, match="same length"):
        excess_over_baseline([0.01], [0.01, 0.02])


def test_slice_stability_first_seen_order_and_within_group_curves() -> None:
    table = slice_stability([0.1, -0.05, 0.02, 0.1], ["2025", "2024", "2025", "2024"])
    assert [g.label for g in table] == ["2025", "2024"]  # first-seen, never sorted
    g2025, g2024 = table
    assert (g2025.n, g2025.mean_return, g2025.hit_rate) == (2, pytest.approx(0.06), 1.0)
    assert g2025.compounded_return == pytest.approx(1.1 * 1.02 - 1.0)  # 0.122
    assert (g2024.n, g2024.mean_return, g2024.hit_rate) == (2, pytest.approx(0.025), 0.5)
    assert g2024.compounded_return == pytest.approx(0.95 * 1.1 - 1.0)  # 0.045
    with pytest.raises(ValueError, match="same length"):
        slice_stability([0.01], ["a", "b"])
