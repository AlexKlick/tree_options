"""G3 (protocol 0.2.0): the volume-flow liquidity regime + provenance gates.

Pins the ratified semantics: open interest and spread are DROPPED WITH
DISCLOSURE (NOT_APPLICABLE naming the absence, never a fabricated pass);
the session's traded contracts are the liquidity term under their own rule
name; a model-derived |delta| passes only in a regime that accepts its
provenance; and the era-pending threshold refuses to build a filter — an
unset flow threshold may never default to 0.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from tests.fixtures.contracts import standard_call
from tree_options.candidates.filters import (
    AsOf,
    CandidateFilter,
    CandidateSnapshot,
)
from tree_options.data.massive_options import (
    DERIVED_DELTA_PROVENANCE,
    MassiveCapabilityError,
    build_option_candidate_inputs,
)
from tree_options.protocol.loader import load_protocol

DECISION_SESSION = date(2024, 4, 15)
# The synthetic calendar labels its closes in UTC (the existing filter-test
# convention); the wall-clock ET story is in the comments only.
DECISION_AT = datetime(2024, 4, 15, 20, 0, tzinfo=UTC)
# The pit discipline of this lane: a Monday-close decision consumes the
# PRIOR session's bar (Friday 2024-04-12), whose T+1 receipt wall lands
# Monday morning — before the decision instant. The decision session's
# own bar is NOT yet received and would be future data.
RECEIPT = datetime(2024, 4, 15, 13, 0, tzinfo=UTC)


class _Cell:
    """The structural slice of a MassiveDerivedQuote the builder reads."""

    def __init__(self, *, volume: int | None, abs_delta: Decimal | None) -> None:
        self.contract_id = "OPT-C-2024-06-21-50"
        self.underlying_security_id = "SEC-001"
        self.received_timestamp = RECEIPT
        self.volume = volume

        class _Derived:
            pass

        d = _Derived()
        d.abs_delta = abs_delta
        self.derived = d if abs_delta is not None else None


def _snapshot(abs_delta, volume, *, applicable=True) -> CandidateSnapshot:
    contract = standard_call(expiration=date(2024, 5, 15))
    return CandidateSnapshot(
        contract=contract,
        underlying_security_id=contract.underlying_security_id,
        decision_session=DECISION_SESSION,
        decision_at=DECISION_AT,
        expiration=contract.expiration,
        abs_delta=abs_delta,
        open_interest=None,
        same_day_volume=volume,
        same_day_volume_applicable=applicable,
        bid=None,
        ask=None,
        underlying_20d_median_dollar_volume=AsOf(Decimal("0"), DECISION_AT),
        spans_earnings=AsOf(False, DECISION_AT),
    )


def _flow_filter(synthetic_calendar, threshold: int = 100) -> CandidateFilter:
    return CandidateFilter(
        synthetic_calendar,
        dte_min=30,
        dte_max=60,
        abs_delta_min=Decimal("0.30"),
        abs_delta_max=Decimal("0.60"),
        standard_deliverable_only=True,
        min_open_interest=500,
        min_same_day_volume=100,
        volume_only_if_already_available=True,
        max_spread_fraction_of_midpoint=Decimal("0.10"),
        min_underlying_20d_median_dollar_volume=Decimal("0"),
        exclude_earnings_spanning_hold=True,
        liquidity_regime="volume_flow",
        flow_min_session_volume=threshold,
        accepted_delta_provenance=(DERIVED_DELTA_PROVENANCE, "vendor"),
    )


class TestRegimeDisclosure:
    def test_open_interest_and_spread_dropped_with_disclosure(self, synthetic_calendar):
        d = _flow_filter(synthetic_calendar).evaluate(
            _snapshot(
                AsOf(Decimal("0.45"), RECEIPT, provenance=DERIVED_DELTA_PROVENANCE),
                AsOf(400, RECEIPT),
            )
        )
        by_rule = {r.rule: r for r in d.results}
        assert by_rule["open_interest"].status == "NOT_APPLICABLE"
        assert "no open interest" in by_rule["open_interest"].detail
        assert by_rule["spread"].status == "NOT_APPLICABLE"
        assert "no two-sided market" in by_rule["spread"].detail

    def test_flow_rule_named_and_evaluated(self, synthetic_calendar):
        d = _flow_filter(synthetic_calendar, threshold=250).evaluate(
            _snapshot(
                AsOf(Decimal("0.45"), RECEIPT, provenance=DERIVED_DELTA_PROVENANCE),
                AsOf(400, RECEIPT),
            )
        )
        by_rule = {r.rule: r for r in d.results}
        assert "session_volume_flow" in by_rule
        assert "same_day_volume" not in by_rule
        assert by_rule["session_volume_flow"].status == "PASS"
        assert "flow min 250" in by_rule["session_volume_flow"].detail

    def test_flow_below_threshold_fails(self, synthetic_calendar):
        d = _flow_filter(synthetic_calendar, threshold=500).evaluate(
            _snapshot(
                AsOf(Decimal("0.45"), RECEIPT, provenance=DERIVED_DELTA_PROVENANCE),
                AsOf(400, RECEIPT),
            )
        )
        by_rule = {r.rule: r for r in d.results}
        assert by_rule["session_volume_flow"].status == "FAIL"
        assert not d.accepted

    def test_missing_volume_not_yet_published_is_not_applicable(self, synthetic_calendar):
        d = _flow_filter(synthetic_calendar).evaluate(
            _snapshot(
                AsOf(Decimal("0.45"), RECEIPT, provenance=DERIVED_DELTA_PROVENANCE),
                None,
                applicable=False,
            )
        )
        by_rule = {r.rule: r for r in d.results}
        assert by_rule["session_volume_flow"].status == "NOT_APPLICABLE"


class TestProvenanceGate:
    def test_model_derived_delta_accepted_in_flow_regime(self, synthetic_calendar):
        d = _flow_filter(synthetic_calendar).evaluate(
            _snapshot(
                AsOf(Decimal("0.45"), RECEIPT, provenance=DERIVED_DELTA_PROVENANCE),
                AsOf(400, RECEIPT),
            )
        )
        by_rule = {r.rule: r for r in d.results}
        assert by_rule["delta"].status == "PASS"
        assert DERIVED_DELTA_PROVENANCE in by_rule["delta"].detail

    def test_unaccepted_provenance_is_not_evaluable(self, synthetic_calendar):
        d = _flow_filter(synthetic_calendar).evaluate(
            _snapshot(
                AsOf(Decimal("0.45"), RECEIPT, provenance="hallucinated"),
                AsOf(400, RECEIPT),
            )
        )
        by_rule = {r.rule: r for r in d.results}
        assert by_rule["delta"].status == "NOT_EVALUABLE"
        assert "not accepted" in by_rule["delta"].detail
        assert not d.accepted

    def test_two_sided_regime_rejects_model_derived_delta(self, synthetic_calendar):
        """A model-derived delta may not pass through the vendor-only
        regime: the default filter's accepted set is (vendor,)."""
        f = CandidateFilter.from_protocol(synthetic_calendar, load_protocol())
        assert f.accepted_delta_provenance == ("vendor",)
        d = f.evaluate(
            _snapshot(
                AsOf(Decimal("0.45"), RECEIPT, provenance=DERIVED_DELTA_PROVENANCE),
                AsOf(400, RECEIPT),
            )
        )
        by_rule = {r.rule: r for r in d.results}
        assert by_rule["delta"].status == "NOT_EVALUABLE"


class TestEraPendingThreshold:
    def test_protocol_threshold_is_pending_and_builder_refuses(self, synthetic_calendar):
        p = load_protocol()
        lf = p.option_candidate_defaults.liquidity_volume_flow
        assert lf is not None
        assert lf.flow_min_session_volume is None  # PENDING-era
        with pytest.raises(ValueError, match="PENDING-era"):
            CandidateFilter.from_protocol_volume_flow(synthetic_calendar, p)

    def test_volume_flow_requires_threshold(self, synthetic_calendar):
        with pytest.raises(ValueError, match="flow_min_session_volume"):
            _flow_filter(synthetic_calendar, threshold=None)  # type: ignore[arg-type]

    def test_threshold_set_builds_and_binds(self, synthetic_calendar):
        p = load_protocol()
        lf = p.option_candidate_defaults.liquidity_volume_flow
        patched = lf.model_copy(update={"flow_min_session_volume": 250})
        d2 = p.option_candidate_defaults.model_copy(update={"liquidity_volume_flow": patched})
        p2 = p.model_copy(update={"option_candidate_defaults": d2})
        f = CandidateFilter.from_protocol_volume_flow(synthetic_calendar, p2)
        assert f.flow_min_session_volume == 250
        assert f.liquidity_regime == "volume_flow"

    def test_protocol_amendment_record_present(self):
        p = load_protocol()
        assert p.meta.protocol_version == "0.2.0"
        (amendment,) = p.meta.amendments
        assert amendment.version == "0.2.0"
        assert "PR #11" in amendment.decision
        assert p.fills.vwap.zero_volume_session == "unfillable"


class TestCandidateInputsBuilder:
    def _contract(self):
        return standard_call(expiration=date(2024, 5, 15))

    def test_derived_cell_builds_provenance_stamped_inputs(self):
        cell = _Cell(volume=400, abs_delta=Decimal("0.45"))
        snap = build_option_candidate_inputs(
            self._contract(),
            cell,  # type: ignore[arg-type] — structural slice
            decision_session=DECISION_SESSION,
            decision_at=DECISION_AT,
        )
        assert snap.abs_delta is not None
        assert snap.abs_delta.provenance == DERIVED_DELTA_PROVENANCE
        assert snap.abs_delta.available_at == RECEIPT
        assert snap.same_day_volume is not None
        assert snap.same_day_volume.provenance == "vendor"
        assert snap.same_day_volume.available_at == RECEIPT
        assert snap.open_interest is None and snap.bid is None and snap.ask is None

    def test_not_evaluable_cell_builds_missing_delta_not_a_guess(self):
        cell = _Cell(volume=400, abs_delta=None)
        snap = build_option_candidate_inputs(
            self._contract(),
            cell,  # type: ignore[arg-type]
            decision_session=DECISION_SESSION,
            decision_at=DECISION_AT,
        )
        assert snap.abs_delta is None
        assert snap.same_day_volume is not None  # the observed fact survives

    def test_future_cell_refuses_at_build(self):
        cell = _Cell(volume=400, abs_delta=Decimal("0.45"))
        with pytest.raises(MassiveCapabilityError, match="future data"):
            build_option_candidate_inputs(
                self._contract(),
                cell,  # type: ignore[arg-type]
                decision_session=DECISION_SESSION,
                decision_at=RECEIPT - timedelta(seconds=1),
            )

    def test_contract_mismatch_refuses(self):
        cell = _Cell(volume=400, abs_delta=Decimal("0.45"))
        cell.contract_id = "SOME-OTHER-CONTRACT"
        with pytest.raises(MassiveCapabilityError, match="names contract"):
            build_option_candidate_inputs(
                self._contract(),
                cell,  # type: ignore[arg-type]
                decision_session=DECISION_SESSION,
                decision_at=DECISION_AT,
            )

    def test_provenance_token_matches_protocol_accepted_classes(self):
        p = load_protocol()
        lf = p.option_candidate_defaults.liquidity_volume_flow
        assert DERIVED_DELTA_PROVENANCE in lf.abs_delta_provenance_accepted
        from tree_options.data.massive_overlay import DERIVATION_PROVENANCE

        assert DERIVATION_PROVENANCE == DERIVED_DELTA_PROVENANCE
