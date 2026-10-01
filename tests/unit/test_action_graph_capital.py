from dataclasses import replace
from decimal import Decimal

import pytest

from tree_options.action_graph.capital import (
    CandidateRisk,
    CapitalProfile,
    RewardTier,
    review_candidate,
)


def _profile() -> CapitalProfile:
    return CapitalProfile(
        profile_id="target-5k",
        revision=1,
        intended_capital=Decimal("5000"),
        risk_style="operator_defined",
        goals=("spread_risk", "volatility", "execution_quality"),
        allowed_strategy_versions=("defined-risk-spread@1",),
        max_loss_per_trade=Decimal("100"),
        max_open_loss=Decimal("300"),
        max_daily_loss=Decimal("200"),
        horizon_days=180,
        reward_tiers=(
            RewardTier(Decimal("1.5"), Decimal("50")),
            RewardTier(Decimal("2"), Decimal("100")),
        ),
        steady_win_floor=Decimal("0.70"),
    )


def _candidate() -> CandidateRisk:
    return CandidateRisk(
        "defined-risk-spread@1",
        Decimal("80"),
        Decimal("120"),
        Decimal("40"),
        True,
        True,
        Decimal("80"),
        Decimal("160"),
        True,
        "steady",
        Decimal("0.80"),
        True,
    )


def test_eligibility_uses_target_capital_not_paper_account_equity() -> None:
    assert review_candidate(_profile(), _candidate()) == ()
    assert review_candidate(
        _profile(),
        replace(_candidate(), worst_case_loss=Decimal("5100"), reward_estimate=Decimal("10200")),
    ) == (
        "trade_exceeds_intended_capital",
        "trade_loss_cap_exceeded",
        "open_loss_cap_exceeded",
        "daily_loss_cap_exceeded",
    )


def test_missing_limits_and_reconciliation_block() -> None:
    profile = replace(_profile(), max_loss_per_trade=None, horizon_days=None)
    candidate = replace(_candidate(), current_open_loss=None, realized_daily_loss=None)
    assert review_candidate(profile, candidate) == (
        "profile_limits_or_strategy_scope_missing",
        "open_exposure_unknown",
        "daily_loss_unknown",
    )


def test_strategy_scope_and_aggregate_caps() -> None:
    candidate = CandidateRisk(
        "unknown@1",
        Decimal("90"),
        Decimal("250"),
        Decimal("150"),
        True,
        True,
        Decimal("90"),
        Decimal("180"),
        True,
        "steady",
        Decimal("0.80"),
        True,
    )
    assert review_candidate(_profile(), candidate) == (
        "strategy_version_out_of_scope",
        "open_loss_cap_exceeded",
        "daily_loss_cap_exceeded",
    )


@pytest.mark.parametrize("bad", [Decimal("NaN"), Decimal("-1"), Decimal("5001")])
def test_profile_rejects_invalid_loss_limit(bad: Decimal) -> None:
    with pytest.raises(ValueError):
        replace(_profile(), max_loss_per_trade=bad)


def test_unverified_package_assignment_and_margin_block() -> None:
    candidate = replace(
        _candidate(),
        package_defined_risk_verified=False,
        assignment_plan_verified=False,
        broker_margin_change=None,
    )
    assert review_candidate(_profile(), candidate) == (
        "package_risk_unverified",
        "assignment_plan_unverified",
        "broker_margin_unknown",
    )


def test_reward_tier_scales_below_hard_cap_only() -> None:
    candidate = replace(
        _candidate(), objective="asymmetric", reward_estimate=Decimal("120")
    )  # 1.5:1
    assert review_candidate(_profile(), candidate) == ("reward_tier_loss_cap_exceeded",)
    assert review_candidate(_profile(), replace(candidate, reward_estimate=Decimal("80"))) == (
        "reward_ratio_below_minimum",
    )
    with pytest.raises(ValueError, match="cannot exceed per-trade cap"):
        replace(_profile(), reward_tiers=(RewardTier(Decimal("2"), Decimal("101")),))


def test_steady_objective_requires_trade_win_evidence_not_forecast_quality() -> None:
    candidate = replace(_candidate(), win_probability_evidence_verified=False)
    assert review_candidate(_profile(), candidate) == ("win_probability_evidence_missing",)
    candidate = replace(_candidate(), win_probability_estimate=Decimal("0.65"))
    assert review_candidate(_profile(), candidate) == ("win_probability_below_floor",)
    profile = replace(_profile(), steady_win_floor=None)
    assert review_candidate(profile, _candidate()) == ("steady_win_floor_missing",)


def test_unknown_objective_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown candidate objective"):
        review_candidate(_profile(), replace(_candidate(), objective="unsupported"))  # type: ignore[arg-type]
