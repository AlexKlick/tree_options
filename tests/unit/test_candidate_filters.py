"""Tri-state candidate filters (§9.2 + audit §5.2) + fixture inventory §5.3."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from tree_options.candidates.filters import (
    NOT_APPLICABLE,
    NOT_EVALUABLE,
    PASS,
    AsOf,
    CandidateFilter,
    CandidateSnapshot,
)

DECISION_AT = datetime(2024, 4, 15, 20, 0, tzinfo=UTC)  # 16:00 ET close
EARLIER = DECISION_AT - timedelta(hours=2)
LATER = DECISION_AT + timedelta(minutes=1)


def _snapshot(**over) -> CandidateSnapshot:
    base = dict(
        contract_id="OPT-C-2024-06-21-50",
        underlying_security_id="SEC-001",
        decision_session=date(2024, 4, 15),
        decision_at=DECISION_AT,
        expiration=date(2024, 5, 15),  # 30 DTE boundary
        abs_delta=AsOf(Decimal("0.45"), EARLIER),
        open_interest=AsOf(1200, EARLIER),
        same_day_volume=AsOf(250, EARLIER),
        same_day_volume_applicable=True,
        bid=AsOf(Decimal("1.90"), EARLIER),
        ask=AsOf(Decimal("2.00"), EARLIER),
        standard_contract=True,
        underlying_20d_median_dollar_volume=AsOf(Decimal("120000000"), EARLIER),
        spans_earnings=AsOf(False, EARLIER),
    )
    base.update(over)
    return CandidateSnapshot(**base)


@pytest.fixture()
def filt(protocol):
    return CandidateFilter.from_protocol(protocol)


def _statuses(decision) -> dict[str, str]:
    return {r.rule: r.status for r in decision.results}


class TestTriState:
    def test_clean_candidate_all_pass(self, filt):
        d = filt.evaluate(_snapshot())
        assert d.accepted
        assert set(_statuses(d).values()) == {PASS}

    def test_fail_blocks_missing_and_future_alike(self, filt):
        d = filt.evaluate(_snapshot(abs_delta=None))
        assert not d.accepted
        assert _statuses(d)["delta"] == NOT_EVALUABLE

    def test_future_available_delta_not_evaluable(self, filt):
        d = filt.evaluate(_snapshot(abs_delta=AsOf(Decimal("0.45"), LATER)))
        assert not d.accepted
        assert _statuses(d)["delta"] == NOT_EVALUABLE
        assert "future" in next(r for r in d.results if r.rule == "delta").detail

    def test_unavailable_volume_is_not_applicable_and_recorded(self, filt):
        d = filt.evaluate(
            _snapshot(same_day_volume=None, same_day_volume_applicable=False)
        )
        assert d.accepted  # optional rule
        assert _statuses(d)["same_day_volume"] == NOT_APPLICABLE

    def test_applicable_but_missing_volume_not_evaluable(self, filt):
        d = filt.evaluate(_snapshot(same_day_volume=None))
        assert not d.accepted
        assert _statuses(d)["same_day_volume"] == NOT_EVALUABLE

    def test_every_rule_reported(self, filt):
        d = filt.evaluate(_snapshot())
        assert set(_statuses(d)) == {
            "dte", "delta", "deliverable", "open_interest", "same_day_volume",
            "spread", "underlying_liquidity", "earnings_span",
        }


class TestBandEdges:
    def test_dte_calendar_day_convention_boundaries(self, filt):
        # 2024-04-15 + 30 calendar days = 2024-05-15; + 60 = 2024-06-14
        assert filt.evaluate(_snapshot(expiration=date(2024, 5, 15))).accepted
        assert filt.evaluate(_snapshot(expiration=date(2024, 6, 14))).accepted
        assert not filt.evaluate(_snapshot(expiration=date(2024, 5, 14))).accepted  # 29 DTE
        assert not filt.evaluate(_snapshot(expiration=date(2024, 6, 15))).accepted  # 61 DTE

    def test_delta_band_boundaries(self, filt):
        assert filt.evaluate(_snapshot(abs_delta=AsOf(Decimal("0.30"), EARLIER))).accepted
        assert filt.evaluate(_snapshot(abs_delta=AsOf(Decimal("0.60"), EARLIER))).accepted
        assert not filt.evaluate(_snapshot(abs_delta=AsOf(Decimal("0.29"), EARLIER))).accepted

    def test_spread_fraction_boundary(self, filt):
        # bid 1.90 ask 2.10: spread 0.20 / mid 2.00 = exactly 0.10 -> PASS
        d = filt.evaluate(_snapshot(bid=AsOf(Decimal("1.90"), EARLIER),
                                    ask=AsOf(Decimal("2.10"), EARLIER)))
        assert _statuses(d)["spread"] == PASS

    def test_crossed_quote_not_evaluable(self, filt):
        d = filt.evaluate(_snapshot(bid=AsOf(Decimal("2.10"), EARLIER),
                                    ask=AsOf(Decimal("2.00"), EARLIER)))
        assert _statuses(d)["spread"] == NOT_EVALUABLE
        assert not d.accepted

    def test_zero_midpoint_rejects_safely(self, filt):
        d = filt.evaluate(_snapshot(bid=AsOf(Decimal("0.00"), EARLIER),
                                    ask=AsOf(Decimal("0.00"), EARLIER)))
        assert _statuses(d)["spread"] == NOT_EVALUABLE

    def test_thresholds_come_from_protocol(self, filt, protocol):
        p = protocol.option_candidate_defaults
        assert filt.dte_min == p.dte_min and filt.dte_max == p.dte_max
        assert filt.min_open_interest == p.min_open_interest
        assert filt.max_spread_fraction_of_midpoint == p.max_spread_fraction_of_midpoint
        assert (
            filt.min_underlying_20d_median_dollar_volume
            == p.min_underlying_20d_median_dollar_volume
        )


class TestFixtureInventory:
    def test_split_adjusted_contract_rejected(self, filt):
        from tests.fixtures.contracts import split_adjusted_contract

        _contract, _action = split_adjusted_contract()
        d = filt.evaluate(_snapshot(standard_contract=False))
        assert not d.accepted
        assert _statuses(d)["deliverable"] == "FAIL"
        assert "nonstandard" in next(r for r in d.results if r.rule == "deliverable").detail

    def test_locked_nbbo_fixture(self):
        from tests.fixtures.market import locked_quote

        q = locked_quote(DECISION_AT + timedelta(hours=18))
        assert q.bid == q.ask  # locked, not crossed

    def test_early_assignment_candidate_fixture_registered_as_deferred(self):
        """Fixture §5.3: an early-assignment CANDIDATE exists and is recorded
        as a deferred claim — no assignment engine is claimed in M0."""
        from tests.fixtures.assignment import early_assignment_candidate

        cand = early_assignment_candidate()
        assert cand.call_put == "C"
        assert cand.exercise_style == "american"
        assert cand.itm  # deep ITM: underlier 62.30 vs strike 50
        assert cand.claim == "DEFERRED_TO_POST_M0"
        assert cand.note.startswith("no assignment engine in M0")
