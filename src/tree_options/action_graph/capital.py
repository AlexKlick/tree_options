"""Capital profile checks for proposal review, never a trading permit.

Paper equity is deliberately absent from these calculations. A future live
account goal can be evaluated in a large paper account without silently
scaling positions up to that account's buying power.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal


def _money(value: Decimal | None, name: str, *, allow_zero: bool = False) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError(f"{name} must be a finite Decimal")
    if value < 0 or (value == 0 and not allow_zero):
        raise ValueError(f"{name} must be {'non-negative' if allow_zero else 'positive'}")
    return value


@dataclass(frozen=True)
class RewardTier:
    """Operator-authored maximum planned loss at or above a reward/risk ratio."""

    min_ratio: Decimal
    max_trade_loss: Decimal

    def __post_init__(self) -> None:
        _money(self.min_ratio, "min_ratio")
        _money(self.max_trade_loss, "max_trade_loss")


@dataclass(frozen=True)
class CapitalProfile:
    """An authored capital/risk envelope; labels never manufacture limits."""

    profile_id: str
    revision: int
    intended_capital: Decimal
    risk_style: str
    goals: tuple[str, ...]
    allowed_strategy_versions: tuple[str, ...]
    max_loss_per_trade: Decimal | None
    max_open_loss: Decimal | None
    max_daily_loss: Decimal | None
    horizon_days: int | None
    reward_tiers: tuple[RewardTier, ...] = ()
    steady_win_floor: Decimal | None = None

    def __post_init__(self) -> None:
        if not self.profile_id or self.revision < 1:
            raise ValueError("profile identity and positive revision required")
        _money(self.intended_capital, "intended_capital")
        if not self.risk_style or not self.goals:
            raise ValueError("risk style and goals required")
        for name in ("max_loss_per_trade", "max_open_loss", "max_daily_loss"):
            value = getattr(self, name)
            if value is not None and _money(value, name) > self.intended_capital:
                raise ValueError(f"{name} cannot exceed intended capital")
        if self.horizon_days is not None and self.horizon_days < 1:
            raise ValueError("horizon_days must be positive")
        ratios = tuple(tier.min_ratio for tier in self.reward_tiers)
        if ratios != tuple(sorted(set(ratios))):
            raise ValueError("reward tier ratios must be unique and increasing")
        if self.max_loss_per_trade is not None and any(
            tier.max_trade_loss > self.max_loss_per_trade for tier in self.reward_tiers
        ):
            raise ValueError("reward tier cannot exceed per-trade cap")
        if self.steady_win_floor is not None:
            _money(self.steady_win_floor, "steady_win_floor")
            if self.steady_win_floor >= Decimal(1):
                raise ValueError("steady_win_floor must be below one")

    @property
    def complete_for_review(self) -> bool:
        return bool(
            self.allowed_strategy_versions
            and self.max_loss_per_trade is not None
            and self.max_open_loss is not None
            and self.max_daily_loss is not None
            and self.horizon_days is not None
        )


@dataclass(frozen=True)
class CandidateRisk:
    strategy_version: str
    worst_case_loss: Decimal
    current_open_loss: Decimal | None
    realized_daily_loss: Decimal | None
    package_defined_risk_verified: bool
    assignment_plan_verified: bool
    broker_margin_change: Decimal | None
    reward_estimate: Decimal | None = None
    reward_basis_verified: bool = False
    objective: Literal["steady", "asymmetric"] = "steady"
    win_probability_estimate: Decimal | None = None
    win_probability_evidence_verified: bool = False


def review_candidate(profile: CapitalProfile, candidate: CandidateRisk) -> tuple[str, ...]:
    """Return explicit blockers; an empty set means reviewable, not approved.

    Worst-case loss must come from the strategy's hard payoff contract, while
    current exposure and day loss come from separately reconciled account
    facts. Missing measurements block the proposal.
    """
    blockers: list[str] = []
    if not profile.complete_for_review:
        blockers.append("profile_limits_or_strategy_scope_missing")
    if candidate.strategy_version not in profile.allowed_strategy_versions:
        blockers.append("strategy_version_out_of_scope")
    if not candidate.package_defined_risk_verified:
        blockers.append("package_risk_unverified")
    if not candidate.assignment_plan_verified:
        blockers.append("assignment_plan_unverified")
    loss = _money(candidate.worst_case_loss, "worst_case_loss")
    if candidate.reward_estimate is None or not candidate.reward_basis_verified:
        blockers.append("reward_basis_unverified")
    elif candidate.objective == "asymmetric":
        ratio = _money(candidate.reward_estimate, "reward_estimate") / loss
        eligible_tiers = [tier for tier in profile.reward_tiers if ratio >= tier.min_ratio]
        if not profile.reward_tiers:
            blockers.append("asymmetric_policy_missing")
        elif not eligible_tiers:
            blockers.append("reward_ratio_below_minimum")
        elif loss > eligible_tiers[-1].max_trade_loss:
            blockers.append("reward_tier_loss_cap_exceeded")
    else:
        _money(candidate.reward_estimate, "reward_estimate")
    if candidate.objective == "steady":
        if profile.steady_win_floor is None:
            blockers.append("steady_win_floor_missing")
        if (candidate.win_probability_estimate is None
                or not candidate.win_probability_evidence_verified):
            blockers.append("win_probability_evidence_missing")
        elif (not isinstance(candidate.win_probability_estimate, Decimal)
              or not candidate.win_probability_estimate.is_finite()
              or not Decimal(0) <= candidate.win_probability_estimate <= Decimal(1)):
            raise ValueError("win probability must be a finite Decimal in [0, 1]")
        elif (profile.steady_win_floor is not None
              and candidate.win_probability_estimate < profile.steady_win_floor):
            blockers.append("win_probability_below_floor")
    if loss > profile.intended_capital:
        blockers.append("trade_exceeds_intended_capital")
    if profile.max_loss_per_trade is not None and loss > profile.max_loss_per_trade:
        blockers.append("trade_loss_cap_exceeded")
    if candidate.broker_margin_change is None:
        blockers.append("broker_margin_unknown")
    elif _money(candidate.broker_margin_change, "broker_margin_change", allow_zero=True) > profile.intended_capital:
        blockers.append("broker_margin_exceeds_intended_capital")
    if candidate.current_open_loss is None:
        blockers.append("open_exposure_unknown")
    else:
        open_loss = _money(candidate.current_open_loss, "current_open_loss", allow_zero=True)
        if profile.max_open_loss is not None and open_loss + loss > profile.max_open_loss:
            blockers.append("open_loss_cap_exceeded")
    if candidate.realized_daily_loss is None:
        blockers.append("daily_loss_unknown")
    else:
        day_loss = _money(candidate.realized_daily_loss, "realized_daily_loss", allow_zero=True)
        if profile.max_daily_loss is not None and day_loss + loss > profile.max_daily_loss:
            blockers.append("daily_loss_cap_exceeded")
    return tuple(blockers)
