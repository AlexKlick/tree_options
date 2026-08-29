"""G3 (protocol 0.2.0 -> 0.2.1): the volume-flow liquidity regime + provenance gates.

Pins the ratified semantics: open interest and spread are DROPPED WITH
DISCLOSURE (NOT_APPLICABLE naming the absence, never a fabricated pass);
the session's traded contracts are the liquidity term under their own rule
name; a model-derived |delta| passes only in a regime that accepts its
provenance; and an unset flow threshold may never default to 0 (the 0.2.1
amendment landed flow_min_session_volume=100, and the PENDING-era refusal
survives as the fail-closed branch for any protocol still carrying null).
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
        d.provenance = DERIVED_DELTA_PROVENANCE
        self.derived = d if abs_delta is not None else None


def _snapshot(abs_delta, volume, *, applicable=True, dollar_volume="sentinel") -> CandidateSnapshot:
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
        underlying_20d_median_dollar_volume=(
            AsOf(Decimal("0"), DECISION_AT) if dollar_volume == "sentinel" else dollar_volume
        ),
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

    # missing-volume behavior lives in TestReviewHardening: it is the
    # review's P0-3 pin (NOT_EVALUABLE — the liquidity term is mandatory).


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


class TestLandedThreshold:
    def test_protocol_threshold_is_landed_and_builder_binds_it(self, synthetic_calendar):
        """The 0.2.1 landed state: the threshold is 100 (owner_deviation
        bound to the exit-5 census 43b0b040ea3c…, per the landed amendment
        record), and from_protocol_volume_flow BUILDS from the real protocol
        and binds it — no refusal for the landed shape. The PENDING-era
        refusal survives as the fail-closed branch: a protocol copy whose
        threshold is still null may never build a filter."""
        from tree_options.protocol.holdout import RATIFIED_HOLDOUT_CENSUS_SHA256

        p = load_protocol()
        lf = p.option_candidate_defaults.liquidity_volume_flow
        assert lf is not None
        assert lf.flow_min_session_volume == 100  # landed by 0.2.1, not pending
        (landed_record,) = [a for a in p.meta.amendments if a.version == "0.2.1"]
        assert "flow_min_session_volume = 100" in landed_record.changes
        assert RATIFIED_HOLDOUT_CENSUS_SHA256 in landed_record.changes
        f = CandidateFilter.from_protocol_volume_flow(synthetic_calendar, p)
        assert f.flow_min_session_volume == 100
        assert f.liquidity_regime == "volume_flow"
        assert f.accepted_delta_provenance == lf.abs_delta_provenance_accepted
        # fail-closed unchanged: the PENDING-era shape still refuses
        patched = lf.model_copy(update={"flow_min_session_volume": None})
        d2 = p.option_candidate_defaults.model_copy(update={"liquidity_volume_flow": patched})
        p_pending = p.model_copy(update={"option_candidate_defaults": d2})
        with pytest.raises(ValueError, match="PENDING-era"):
            CandidateFilter.from_protocol_volume_flow(synthetic_calendar, p_pending)

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
        """The landed protocol content, carried by the 0.2.2 flip: the live
        yaml is 0.2.2 and carries all three records — the 0.2.1 record (the
        threshold landing this class pins) is unchanged inside it, and the
        0.2.2 record names the flip's owner ruling."""
        p = load_protocol()
        assert p.meta.protocol_version == "0.2.2"
        first, landed, flipped = p.meta.amendments
        assert first.version == "0.2.0"
        assert "PR #11" in first.decision
        assert landed.version == "0.2.1"
        assert landed.date == "2026-08-26"
        assert "43b0b040ea3c7936fc08e6b1028ce446e46c99f44ca1d87da9fec02099e12e14" in (
            landed.changes
        )
        assert flipped.version == "0.2.2"
        assert flipped.date == "2026-08-28"
        assert "m4-022-ruling-20260828" in flipped.decision
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


class TestReviewHardening:
    """Pins from the independent Codex review round (gpt-5.6-sol)."""

    def _accepting_inputs(self, provenance=DERIVED_DELTA_PROVENANCE):
        return _snapshot(
            AsOf(Decimal("0.45"), RECEIPT, provenance=provenance),
            AsOf(400, RECEIPT),
        )

    def test_missing_volume_is_not_evaluable_in_flow_regime(self, synthetic_calendar):
        """P0-3: session volume IS the liquidity term — a candidate with no
        volume evidence must not be ACCEPTED via NOT_APPLICABLE."""
        d = _flow_filter(synthetic_calendar).evaluate(
            _snapshot(
                AsOf(Decimal("0.45"), RECEIPT, provenance=DERIVED_DELTA_PROVENANCE),
                None,
                applicable=False,
            )
        )
        by_rule = {r.rule: r for r in d.results}
        assert by_rule["session_volume_flow"].status == "NOT_EVALUABLE"
        assert not d.accepted

    def test_supplied_open_interest_is_regime_incoherent(self, synthetic_calendar):
        """P1-7: the drop is premised on ABSENCE; a snapshot supplying OI
        contradicts the premise and fails closed."""
        snap = _snapshot(
            AsOf(Decimal("0.45"), RECEIPT, provenance=DERIVED_DELTA_PROVENANCE),
            AsOf(400, RECEIPT),
        )
        with_o = CandidateSnapshot(
            **{
                **snap.__dict__,
                "open_interest": AsOf(1200, RECEIPT),
            }
        )
        d = _flow_filter(synthetic_calendar).evaluate(with_o)
        by_rule = {r.rule: r for r in d.results}
        assert by_rule["open_interest"].status == "NOT_EVALUABLE"
        assert "regime incoherent" in by_rule["open_interest"].detail
        assert not d.accepted

    def test_supplied_quotes_are_regime_incoherent(self, synthetic_calendar):
        snap = _snapshot(
            AsOf(Decimal("0.45"), RECEIPT, provenance=DERIVED_DELTA_PROVENANCE),
            AsOf(400, RECEIPT),
        )
        with_q = CandidateSnapshot(
            **{
                **snap.__dict__,
                "bid": AsOf(Decimal("1.90"), RECEIPT),
                "ask": AsOf(Decimal("2.00"), RECEIPT),
            }
        )
        d = _flow_filter(synthetic_calendar).evaluate(with_q)
        by_rule = {r.rule: r for r in d.results}
        assert by_rule["spread"].status == "NOT_EVALUABLE"
        assert "regime incoherent" in by_rule["spread"].detail

    def test_zero_flow_threshold_refuses(self, synthetic_calendar):
        """P0-5b: only None was rejected — a zero threshold must refuse
        too (it would accept everything)."""
        with pytest.raises(ValueError, match="flow_min_session_volume"):
            _flow_filter(synthetic_calendar, threshold=0)

    def test_bool_threshold_refuses_at_protocol_layer(self):
        """P0-5a: YAML `true` must not coerce to 1 and 'activate' the
        pending regime (strict int)."""
        from pydantic import ValidationError

        from tree_options.protocol.schema import LiquidityFlowConfig

        with pytest.raises(ValidationError):
            LiquidityFlowConfig.model_validate(
                {
                    "regime": "volume_flow",
                    "flow_min_session_volume": True,
                    "spread_term": "dropped_no_two_sided_market",
                    "open_interest_term": "dropped_no_open_interest",
                    "abs_delta_provenance_accepted": ["vendor"],
                }
            )

    def test_builder_refuses_unaccepted_derivation_stamp(self):
        """P0-4: the builder re-stamps nothing — a cell derived under a
        foreign provenance refuses at the build, never laundered into the
        ratified token."""
        cell = _Cell(volume=400, abs_delta=Decimal("0.45"))
        cell.derived.provenance = "hallucinated"  # type: ignore[attr-defined]
        with pytest.raises(MassiveCapabilityError, match="re-stamps nothing"):
            build_option_candidate_inputs(
                standard_call(expiration=date(2024, 5, 15)),
                cell,  # type: ignore[arg-type]
                decision_session=DECISION_SESSION,
                decision_at=DECISION_AT,
            )

    def test_declared_version_must_carry_its_content(self):
        """P1-9: a 0.2.0 protocol with the G3 blocks stripped must refuse
        to load, and the real protocol carries them."""
        from pydantic import ValidationError

        from tree_options.protocol.schema import ResearchProtocol

        p = load_protocol()
        stripped = p.model_copy(
            update={
                "option_candidate_defaults": p.option_candidate_defaults.model_copy(
                    update={"liquidity_volume_flow": None}
                ),
                "meta": p.meta.model_copy(update={"amendments": ()}),
            }
        )
        with pytest.raises(ValidationError):
            ResearchProtocol.model_validate(stripped.model_dump())


class TestRoundTwoHardening:
    def test_bool_threshold_refuses_at_constructor(self, synthetic_calendar):
        """R2 P0-5: True == 1 passes the < 1 comparison — the type gate
        must be explicit (bool is an int)."""
        with pytest.raises(ValueError, match="an int >= 1"):
            _flow_filter(synthetic_calendar, threshold=True)  # type: ignore[arg-type]

    def test_nan_threshold_refuses_at_constructor(self, synthetic_calendar):
        """R2 P0-5: NaN compares false against everything and slipped the
        bound check."""
        with pytest.raises(ValueError, match="an int >= 1"):
            _flow_filter(synthetic_calendar, threshold=float("nan"))  # type: ignore[arg-type]

    def test_020_without_derived_provenance_refuses(self):
        """R2 P1-9: 0.2.0 must carry the provenance class the amendment
        ratified — removing it while declaring 0.2.0 is a lie."""
        from pydantic import ValidationError

        from tree_options.protocol.schema import ResearchProtocol

        p = load_protocol()
        lf = p.option_candidate_defaults.liquidity_volume_flow
        assert lf is not None
        stripped = p.model_copy(
            update={
                "option_candidate_defaults": (
                    p.option_candidate_defaults.model_copy(
                        update={
                            "liquidity_volume_flow": lf.model_copy(
                                update={"abs_delta_provenance_accepted": ("vendor",)}
                            )
                        }
                    )
                ),
            }
        )
        with pytest.raises(ValidationError, match="provenance class"):
            ResearchProtocol.model_validate(stripped.model_dump())

    def test_future_oi_value_not_leaked_into_audit(self, synthetic_calendar):
        """R2 new P1: the incoherence detail must report PRESENCE without
        dereferencing a value whose availability was never checked."""
        snap = _snapshot(
            AsOf(Decimal("0.45"), RECEIPT, provenance=DERIVED_DELTA_PROVENANCE),
            AsOf(400, RECEIPT),
        )
        future_oi = AsOf(987654321, DECISION_AT + timedelta(days=1))
        with_o = CandidateSnapshot(**{**snap.__dict__, "open_interest": future_oi})
        d = _flow_filter(synthetic_calendar).evaluate(with_o)
        by_rule = {r.rule: r for r in d.results}
        assert by_rule["open_interest"].status == "NOT_EVALUABLE"
        assert "987654321" not in by_rule["open_interest"].detail
        assert "value withheld" in by_rule["open_interest"].detail


class TestUnderlyingLiquidityTerm:
    """(w7, theory-panel §2 P0-1(a); declared at the 0.2.2 flip, owner
    ruling m4-022-ruling-20260828) The declared disposition of the
    underlying-liquidity term: the ruled drop-with-disclosure fallback, its
    incoherence guard, and the byte-identical standing default."""

    def _dropped_protocol(self):
        """The dropped-term SHAPE as machinery proof: the live protocol
        (0.2.2 since the flip, declaring "evaluated") with exactly that one
        key flipped to the ruled fallback."""
        p = load_protocol()
        lf = p.option_candidate_defaults.liquidity_volume_flow
        assert lf is not None
        return p.model_copy(
            update={
                "option_candidate_defaults": p.option_candidate_defaults.model_copy(
                    update={
                        "liquidity_volume_flow": lf.model_copy(
                            update={"underlying_liquidity_term": "dropped_no_equity_aggregates"}
                        )
                    }
                )
            }
        )

    def test_standing_protocol_defaults_to_evaluated(self):
        """0.2.1 carries no key, so the default must be 'evaluated' and the
        built filter must behave exactly as before this seam existed."""
        from pydantic import ValidationError

        from tree_options.protocol.schema import LiquidityFlowConfig

        lf = load_protocol().option_candidate_defaults.liquidity_volume_flow
        assert lf is not None
        assert lf.underlying_liquidity_term == "evaluated"
        # model_copy skips validation (the round-2 convention): round-trip
        # through model_validate to make the Literal refusal real
        with pytest.raises(ValidationError, match="underlying_liquidity_term"):
            LiquidityFlowConfig.model_validate(
                {**lf.model_dump(), "underlying_liquidity_term": "sometimes"}
            )

    def test_dropped_term_answers_not_applicable_with_disclosure(self, synthetic_calendar):
        d = CandidateFilter.from_protocol_volume_flow(
            synthetic_calendar, self._dropped_protocol()
        ).evaluate(
            _snapshot(
                AsOf(Decimal("0.45"), RECEIPT, provenance=DERIVED_DELTA_PROVENANCE),
                AsOf(400, RECEIPT),
                dollar_volume=None,
            )
        )
        by_rule = {r.rule: r for r in d.results}
        assert by_rule["underlying_liquidity"].status == "NOT_APPLICABLE"
        assert "no equity-aggregates dollar volume" in by_rule["underlying_liquidity"].detail
        # the audit now SAYS the rule is off instead of failing on the sentinel
        assert d.accepted

    def test_dropped_term_with_a_supplied_value_is_regime_incoherent(self, synthetic_calendar):
        """A snapshot that still supplies a dollar-volume value (the lane's
        declared Decimal("0") sentinel, say) contradicts the dropped premise:
        NOT_EVALUABLE with the value withheld — the disclosure may not paper
        over real inputs, exactly like the OI/spread incoherence branches."""
        snap = _snapshot(
            AsOf(Decimal("0.45"), RECEIPT, provenance=DERIVED_DELTA_PROVENANCE),
            AsOf(400, RECEIPT),
        )
        assert snap.underlying_20d_median_dollar_volume is not None  # the sentinel
        d = CandidateFilter.from_protocol_volume_flow(
            synthetic_calendar, self._dropped_protocol()
        ).evaluate(snap)
        by_rule = {r.rule: r for r in d.results}
        assert by_rule["underlying_liquidity"].status == "NOT_EVALUABLE"
        assert "value withheld" in by_rule["underlying_liquidity"].detail
        assert not d.accepted

    def test_evaluated_term_keeps_the_sentinel_fail_byte_identically(self, synthetic_calendar):
        """The standing disposition is untouched: the declared sentinel still
        FAILs the rule (the honest audit row the lane carries today)."""
        snap = _snapshot(
            AsOf(Decimal("0.45"), RECEIPT, provenance=DERIVED_DELTA_PROVENANCE),
            AsOf(400, RECEIPT),
        )
        d = CandidateFilter.from_protocol_volume_flow(synthetic_calendar, load_protocol()).evaluate(
            snap
        )
        by_rule = {r.rule: r for r in d.results}
        assert by_rule["underlying_liquidity"].status == "FAIL"
        assert by_rule["underlying_liquidity"].detail == "below min"
        assert not d.accepted

    def test_unknown_term_token_refuses_at_the_constructor(self, synthetic_calendar):
        with pytest.raises(ValueError, match="unknown underlying_liquidity_term"):
            CandidateFilter(
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
                flow_min_session_volume=100,
                underlying_liquidity_term="dropped_no_bid_ask",
            )
