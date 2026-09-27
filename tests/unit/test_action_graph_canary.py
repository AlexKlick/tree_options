from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tree_options.action_graph.canary import CanaryFacts, review_canary
from tree_options.action_graph.capital import CapitalProfile

NOW = datetime(2026, 9, 27, 15, 0, tzinfo=UTC)


def profile() -> CapitalProfile:
    return CapitalProfile(
        profile_id="paper-5k-canary", revision=1, intended_capital=Decimal("5000"),
        risk_style="operator_defined", goals=("operational_canary",),
        allowed_strategy_versions=("operational-canary/1",),
        max_loss_per_trade=Decimal("300"), max_open_loss=Decimal("1500"),
        max_daily_loss=Decimal("300"), horizon_days=1,
    )


def facts() -> CanaryFacts:
    return CanaryFacts(
        intent_sha256="a" * 64, observed_account_id="DUT143714",
        mandate_account_id="DUT143714", paper_gateway_verified=True,
        account_observed_at=NOW - timedelta(seconds=20),
        quote_observed_at=NOW - timedelta(seconds=10), checked_at=NOW,
        owner_epoch="epoch-1", owner_healthy=True,
        legacy_positions=0, legacy_working_orders=0, package_quantity=1,
        contract_verified=True, quote_complete=True,
        defined_risk_verified=True, assignment_plan_verified=True,
        protective_exit_ready=True, worst_case_loss=Decimal("250"),
        temporary_assignment_exposure=Decimal("4000"),
        broker_margin_change=Decimal("500"), current_open_loss=Decimal("0"),
        realized_daily_loss=Decimal("0"),
    )


def test_fresh_flat_paper_canary_is_reviewable_not_authorized() -> None:
    assert review_canary(profile(), facts()) == ()


def test_account_and_owner_fail_closed() -> None:
    candidate = replace(facts(), observed_account_id="U123", paper_gateway_verified=False,
                        owner_healthy=False, legacy_positions=1, legacy_working_orders=1)
    assert review_canary(profile(), candidate) == (
        "paper_account_mismatch_or_unverified", "broker_owner_unhealthy", "legacy_book_not_flat",
    )


def test_stale_or_future_observations_block() -> None:
    candidate = replace(facts(), account_observed_at=NOW - timedelta(seconds=61),
                        quote_observed_at=NOW + timedelta(seconds=1))
    assert review_canary(profile(), candidate) == (
        "account_stale_or_future", "quote_stale_or_future",
    )


def test_risk_and_unknowns_block_even_in_large_paper_account() -> None:
    candidate = replace(facts(), worst_case_loss=Decimal("301"),
                        temporary_assignment_exposure=Decimal("5001"),
                        broker_margin_change=None, current_open_loss=None,
                        realized_daily_loss=None)
    assert review_canary(profile(), candidate) == (
        "trade_loss_cap_exceeded", "assignment_exposure_exceeds_budget",
        "broker_margin_unknown", "open_exposure_unknown", "daily_loss_unknown",
    )


def test_daily_and_open_reservation_caps() -> None:
    candidate = replace(facts(), current_open_loss=Decimal("1300"),
                        realized_daily_loss=Decimal("100"))
    assert review_canary(profile(), candidate) == (
        "open_loss_cap_exceeded", "daily_loss_cap_exceeded",
    )


def test_unchecked_package_and_exit_block() -> None:
    candidate = replace(facts(), package_quantity=2, contract_verified=False,
                        assignment_plan_verified=False, protective_exit_ready=False)
    assert review_canary(profile(), candidate) == (
        "canary_quantity_not_one", "contract_or_quote_unverified",
        "package_or_assignment_risk_unverified", "protective_exit_unavailable",
    )


@pytest.mark.parametrize("bad", [Decimal("NaN"), Decimal("-1")])
def test_bad_risk_inputs_are_rejected(bad: Decimal) -> None:
    with pytest.raises(ValueError):
        replace(facts(), temporary_assignment_exposure=bad)


def test_naive_snapshot_is_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        replace(facts(), quote_observed_at=NOW.replace(tzinfo=None))
