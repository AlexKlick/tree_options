"""Fail-closed review of an operator-authored IBKR paper canary.

This is a pure screening function. It cannot issue a mandate, consume a permit,
reserve risk, or send an order. The eventual broker owner must independently
recheck these facts at the send boundary.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from tree_options.action_graph.capital import CapitalProfile, _money

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
ACCOUNT_MAX_AGE = timedelta(seconds=60)
QUOTE_MAX_AGE = timedelta(seconds=30)


@dataclass(frozen=True)
class CanaryFacts:
    """Fresh observations and checked geometry for exactly one paper package."""

    intent_sha256: str
    observed_account_id: str
    mandate_account_id: str
    paper_gateway_verified: bool
    account_observed_at: datetime
    quote_observed_at: datetime
    checked_at: datetime
    owner_epoch: str
    owner_healthy: bool
    legacy_positions: int
    legacy_working_orders: int
    package_quantity: int
    contract_verified: bool
    quote_complete: bool
    defined_risk_verified: bool
    assignment_plan_verified: bool
    protective_exit_ready: bool
    worst_case_loss: Decimal
    temporary_assignment_exposure: Decimal
    broker_margin_change: Decimal | None
    current_open_loss: Decimal | None
    realized_daily_loss: Decimal | None

    def __post_init__(self) -> None:
        if not _SHA256.fullmatch(self.intent_sha256):
            raise ValueError("intent_sha256 must be a lowercase SHA-256 digest")
        if not self.observed_account_id or not self.mandate_account_id or not self.owner_epoch:
            raise ValueError("account and owner identity required")
        if self.legacy_positions < 0 or self.legacy_working_orders < 0:
            raise ValueError("legacy counts must be non-negative")
        _money(self.worst_case_loss, "worst_case_loss")
        _money(self.temporary_assignment_exposure, "temporary_assignment_exposure", allow_zero=True)
        for name in ("broker_margin_change", "current_open_loss", "realized_daily_loss"):
            value = getattr(self, name)
            if value is not None:
                _money(value, name, allow_zero=True)
        for name in ("account_observed_at", "quote_observed_at", "checked_at"):
            value = getattr(self, name)
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"{name} must be timezone-aware")


def review_canary(profile: CapitalProfile, facts: CanaryFacts) -> tuple[str, ...]:
    """Return blockers, never an authorization or inferred broker state."""

    blockers: list[str] = []
    if not profile.complete_for_review or "operational-canary/1" not in profile.allowed_strategy_versions:
        blockers.append("profile_or_canary_scope_missing")
    if not facts.paper_gateway_verified or facts.observed_account_id != facts.mandate_account_id:
        blockers.append("paper_account_mismatch_or_unverified")
    for name, observed, limit in (
        ("account", facts.account_observed_at, ACCOUNT_MAX_AGE),
        ("quote", facts.quote_observed_at, QUOTE_MAX_AGE),
    ):
        age = facts.checked_at - observed
        if age < timedelta(0) or age > limit:
            blockers.append(f"{name}_stale_or_future")
    if not facts.owner_healthy:
        blockers.append("broker_owner_unhealthy")
    if facts.legacy_positions or facts.legacy_working_orders:
        blockers.append("legacy_book_not_flat")
    if facts.package_quantity != 1:
        blockers.append("canary_quantity_not_one")
    if not facts.contract_verified or not facts.quote_complete:
        blockers.append("contract_or_quote_unverified")
    if not facts.defined_risk_verified or not facts.assignment_plan_verified:
        blockers.append("package_or_assignment_risk_unverified")
    if not facts.protective_exit_ready:
        blockers.append("protective_exit_unavailable")
    if facts.worst_case_loss > profile.intended_capital:
        blockers.append("trade_exceeds_intended_capital")
    if profile.max_loss_per_trade is not None and facts.worst_case_loss > profile.max_loss_per_trade:
        blockers.append("trade_loss_cap_exceeded")
    if facts.temporary_assignment_exposure > profile.intended_capital:
        blockers.append("assignment_exposure_exceeds_budget")
    if facts.broker_margin_change is None:
        blockers.append("broker_margin_unknown")
    elif facts.broker_margin_change > profile.intended_capital:
        blockers.append("broker_margin_exceeds_budget")
    if facts.current_open_loss is None:
        blockers.append("open_exposure_unknown")
    elif (profile.max_open_loss is not None
          and facts.current_open_loss + facts.worst_case_loss > profile.max_open_loss):
        blockers.append("open_loss_cap_exceeded")
    if facts.realized_daily_loss is None:
        blockers.append("daily_loss_unknown")
    elif (profile.max_daily_loss is not None
          and facts.realized_daily_loss + facts.worst_case_loss > profile.max_daily_loss):
        blockers.append("daily_loss_cap_exceeded")
    return tuple(blockers)
