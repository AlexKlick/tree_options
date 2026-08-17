"""Candidate filter predicates (§9.2 / handoff §14 item 8).

Every predicate is evaluated and EVERY rejection is reported — an audit
decision, not a first-failure gate. Missing required inputs reject with
DATA_NOT_EVALUABLE: never silently include a candidate you could not check.
The same-day volume filter applies only when the volume datum is already
available (protocol: volume_only_if_already_available).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from tree_options.candidates.filters import CandidateFilter, CandidateSnapshot


def _snapshot(**over) -> CandidateSnapshot:
    base = dict(
        contract_id="OPT-C-2024-06-21-50",
        underlying_security_id="SEC-001",
        decision_session=date(2024, 4, 15),
        expiration=date(2024, 5, 17),  # 32 DTE
        abs_delta=Decimal("0.45"),
        open_interest=1200,
        same_day_volume=250,
        same_day_volume_available=True,
        bid=Decimal("1.90"),
        ask=Decimal("2.00"),
        standard_contract=True,
        underlying_20d_median_dollar_volume=Decimal("120000000"),
        spans_earnings=False,
    )
    base.update(over)
    return CandidateSnapshot(**base)


@pytest.fixture()
def filt(protocol):
    return CandidateFilter.from_protocol(protocol)


def _codes(decision) -> set[str]:
    return {r.code for r in decision.rejections}


class TestAccepts:
    def test_clean_candidate_accepted(self, filt):
        d = filt.evaluate(_snapshot())
        assert d.accepted
        assert d.rejections == ()

    def test_boundary_values_accepted(self, filt):
        d = filt.evaluate(_snapshot(abs_delta=Decimal("0.30")))
        assert d.accepted
        d = filt.evaluate(_snapshot(abs_delta=Decimal("0.60")))
        assert d.accepted
        d = filt.evaluate(_snapshot(expiration=date(2024, 5, 15)))  # 30 DTE
        assert d.accepted
        d = filt.evaluate(_snapshot(expiration=date(2024, 6, 14)))  # 60 DTE
        assert d.accepted


class TestRejections:
    def test_dte_out_of_range_both_sides(self, filt):
        assert _codes(filt.evaluate(_snapshot(expiration=date(2024, 4, 29)))) == {"DTE_OUT_OF_RANGE"}  # 14
        assert _codes(filt.evaluate(_snapshot(expiration=date(2024, 9, 15)))) == {"DTE_OUT_OF_RANGE"}  # 153

    def test_delta_out_of_range(self, filt):
        assert _codes(filt.evaluate(_snapshot(abs_delta=Decimal("0.20")))) == {"DELTA_OUT_OF_RANGE"}
        assert _codes(filt.evaluate(_snapshot(abs_delta=Decimal("0.75")))) == {"DELTA_OUT_OF_RANGE"}

    def test_open_interest_below_min(self, filt):
        assert _codes(filt.evaluate(_snapshot(open_interest=499))) == {"OPEN_INTEREST_BELOW_MIN"}

    def test_spread_fraction_exceeds(self, filt):
        # mid = 1.75, spread 0.30 -> 17% > 10%
        assert _codes(filt.evaluate(_snapshot(bid=Decimal("1.60"), ask=Decimal("1.90")))) == {
            "SPREAD_FRACTION_EXCEEDS"
        }

    def test_dollar_volume_below_min(self, filt):
        assert _codes(
            filt.evaluate(_snapshot(underlying_20d_median_dollar_volume=Decimal("49999999")))
        ) == {"DOLLAR_VOLUME_BELOW_MIN"}

    def test_earnings_spanning_hold(self, filt):
        assert _codes(filt.evaluate(_snapshot(spans_earnings=True))) == {"EARNINGS_SPAN_HOLD"}

    def test_nonstandard_deliverable(self, filt):
        assert _codes(filt.evaluate(_snapshot(standard_contract=False))) == {
            "NONSTANDARD_DELIVERABLE"
        }

    def test_multiple_rejections_all_reported(self, filt):
        d = filt.evaluate(
            _snapshot(abs_delta=Decimal("0.10"), open_interest=10, spans_earnings=True)
        )
        assert not d.accepted
        assert _codes(d) == {"DELTA_OUT_OF_RANGE", "OPEN_INTEREST_BELOW_MIN", "EARNINGS_SPAN_HOLD"}


class TestVolumeSemantics:
    def test_volume_check_applied_when_available(self, filt):
        assert _codes(filt.evaluate(_snapshot(same_day_volume=99))) == {"VOLUME_BELOW_MIN"}

    def test_volume_check_skipped_when_not_yet_available(self, filt):
        """Same-day volume is NOT yet available at decision time early in the
        session: the protocol skips (does not fabricate) the check."""
        d = filt.evaluate(_snapshot(same_day_volume=None, same_day_volume_available=False))
        assert d.accepted

    def test_volume_flag_true_but_value_missing_is_not_evaluable(self, filt):
        d = filt.evaluate(_snapshot(same_day_volume=None, same_day_volume_available=True))
        assert not d.accepted
        assert _codes(d) == {"DATA_NOT_EVALUABLE"}


class TestDataNotEvaluable:
    @pytest.mark.parametrize(
        "missing",
        [
            {"abs_delta": None},
            {"open_interest": None},
            {"bid": None},
            {"ask": None},
            {"underlying_20d_median_dollar_volume": None},
            {"spans_earnings": None},
        ],
    )
    def test_missing_required_input_rejects(self, filt, missing):
        d = filt.evaluate(_snapshot(**missing))
        assert not d.accepted
        assert "DATA_NOT_EVALUABLE" in _codes(d)
        field = next(r for r in d.rejections if r.code == "DATA_NOT_EVALUABLE").field
        assert field == next(iter(missing))

    def test_crossed_quote_inputs_not_evaluable(self, filt):
        d = filt.evaluate(_snapshot(bid=Decimal("2.10"), ask=Decimal("2.00")))
        assert not d.accepted
        assert "DATA_NOT_EVALUABLE" in _codes(d)
