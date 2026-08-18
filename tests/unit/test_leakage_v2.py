"""Leakage round 2 (review F2, F3, F4): point-in-time security master,
decision coherence, volume bypass."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from tests.fixtures.contracts import split_adjusted_contract, standard_call
from tests.fixtures.security import renamed_and_delisted_security
from tree_options.candidates.filters import (
    NOT_APPLICABLE,
    NOT_EVALUABLE,
    AsOf,
    CandidateFilter,
    CandidateSnapshot,
)
from tree_options.guards.availability import AvailabilityGuard, FutureDataError
from tree_options.protocol.loader import load_protocol
from tree_options.schemas.features import FeatureEvent
from tree_options.schemas.options import OptionContract

DECISION_AT = datetime(2024, 4, 15, 20, 0, tzinfo=UTC)  # 16:00 ET close
EARLIER = DECISION_AT - timedelta(hours=2)
LATER = DECISION_AT + timedelta(minutes=1)


def _snapshot(**over) -> CandidateSnapshot:
    contract = over.get("contract") or standard_call(expiration=date(2024, 5, 15))
    base = dict(
        contract=contract,
        underlying_security_id=contract.underlying_security_id,
        decision_session=date(2024, 4, 15),
        decision_at=DECISION_AT,
        expiration=contract.expiration,
        abs_delta=AsOf(Decimal("0.45"), EARLIER),
        open_interest=AsOf(1200, EARLIER),
        same_day_volume=AsOf(250, EARLIER),
        same_day_volume_applicable=True,
        bid=AsOf(Decimal("1.90"), EARLIER),
        ask=AsOf(Decimal("2.00"), EARLIER),
        underlying_20d_median_dollar_volume=AsOf(Decimal("120000000"), EARLIER),
        spans_earnings=AsOf(False, EARLIER),
    )
    base.update(over)
    return CandidateSnapshot(**base)


class TestSecurityMasterPointInTime:
    """F2: ticker_on/listed_on must not expose future renames/delistings."""

    def test_january_decision_cannot_see_march_rename(self):
        sec = renamed_and_delisted_security()
        january = datetime(2024, 2, 1, 21, 0, tzinfo=UTC)
        # The March rename is invisible in January: the entity still trades
        # under NEWM, and OLDA is simply not yet knowable.
        assert sec.ticker_on(date(2024, 2, 1), as_of=january) == "NEWM"
        with pytest.raises(KeyError):
            sec.ticker_on(date(2024, 3, 20), as_of=january)  # future date, future info

    def test_post_announcement_decision_sees_rename(self):
        sec = renamed_and_delisted_security()
        april = datetime(2024, 4, 1, 20, 0, tzinfo=UTC)
        assert sec.ticker_on(date(2024, 3, 20), as_of=april) == "OLDA"

    def test_pre_delisting_membership_does_not_know_delisting(self):
        sec = renamed_and_delisted_security()
        june = datetime(2024, 6, 3, 20, 0, tzinfo=UTC)
        # In June the August delisting is unknowable: membership must be true
        # on a future-within-listing date, not silently shortened.
        assert sec.listed_on(date(2024, 7, 15), as_of=june) is True
        assert sec.listed_on(date(2024, 8, 5), as_of=june) is True  # unknown end

    def test_post_delisting_membership_fails_closed(self):
        sec = renamed_and_delisted_security()
        september = datetime(2024, 9, 2, 20, 0, tzinfo=UTC)
        assert sec.listed_on(date(2024, 8, 5), as_of=september) is False

    def test_retrospective_view_still_works(self):
        sec = renamed_and_delisted_security()
        # as_of=None: the record-level retrospective view (audit/research on
        # settled history), availability gates do not apply.
        assert sec.ticker_on(date(2024, 6, 3)) == "OLDA"
        assert sec.listed_on(date(2024, 8, 5)) is False


class TestCandidateDecisionCoherence:
    """F3: the filter itself must validate decision_at against the calendar."""

    @pytest.fixture()
    def filt(self, static_calendar):
        return CandidateFilter.from_protocol(static_calendar, load_protocol())

    def test_incoherent_decision_at_rejected(self, filt):
        # 2024-04-15 close is 20:00 UTC; a 21:00 stamp is post-close info.
        d = filt.evaluate(_snapshot(decision_at=datetime(2024, 4, 15, 21, 0, tzinfo=UTC)))
        assert not d.accepted
        assert any("decision" in r.detail or r.rule == "decision_coherence" for r in d.results)

    def test_naive_decision_at_rejected(self, filt):
        d = filt.evaluate(_snapshot(decision_at=datetime(2024, 4, 15, 20, 0)))
        assert not d.accepted

    def test_naive_asof_input_rejected_not_crash(self, filt):
        d = filt.evaluate(_snapshot(abs_delta=AsOf(Decimal("0.45"), datetime(2024, 4, 15, 18, 0))))
        assert not d.accepted
        assert any(r.status == NOT_EVALUABLE for r in d.results)

    def test_coherent_snapshot_accepted(self, filt):
        d = filt.evaluate(_snapshot())
        assert d.accepted


class TestVolumeBypass:
    """F4: applicability=False must NOT hide a supplied future-dated volume."""

    @pytest.fixture()
    def filt(self, static_calendar):
        return CandidateFilter.from_protocol(static_calendar, load_protocol())

    def test_future_volume_with_not_applicable_flag_still_rejected(self, filt):
        d = filt.evaluate(
            _snapshot(
                same_day_volume=AsOf(250, LATER),
                same_day_volume_applicable=False,
            )
        )
        assert not d.accepted
        vol = next(r for r in d.results if r.rule == "same_day_volume")
        assert vol.status == NOT_EVALUABLE
        assert "future" in vol.detail

    def test_current_volume_with_flag_false_is_evaluated(self, filt):
        """A supplied volume is always evaluated — the flag only excuses a
        MISSING one."""
        d = filt.evaluate(
            _snapshot(same_day_volume=AsOf(250, EARLIER), same_day_volume_applicable=False)
        )
        vol = next(r for r in d.results if r.rule == "same_day_volume")
        assert vol.status == "PASS"

    def test_missing_volume_with_flag_false_still_not_applicable(self, filt):
        d = filt.evaluate(_snapshot(same_day_volume=None, same_day_volume_applicable=False))
        vol = next(r for r in d.results if r.rule == "same_day_volume")
        assert vol.status == NOT_APPLICABLE


class TestAvailabilityGuardBoundSession:
    """F3: check_feature derives decision_at from the guard's own calendar —
    no caller-supplied instant to drift."""

    @staticmethod
    def _event(available_at, decision_at):
        return FeatureEvent(
            feature_name="eps_ltm",
            security_id="SEC-001",
            value=1.25,
            observed_at=available_at,
            available_at=available_at,
            decision_at=decision_at,
            source="10-Q",
            source_record_id="SEC-001-10Q-Q1-2024",
            revision_id="r0",
        )

    def test_check_feature_derives_and_verifies_decision_binding(self, synthetic_calendar):
        guard = AvailabilityGuard(synthetic_calendar)
        d = next(x for x in synthetic_calendar.sessions() if x >= date(2024, 4, 1))
        close = guard.decision_instant(d)
        ev = self._event(close - timedelta(hours=2), close)
        guard.check_feature(ev, decision_session=d)  # derives close internally
        with pytest.raises(FutureDataError):
            guard.check_feature(ev, decision_session=synthetic_calendar.nth_after(d, 3))

    def test_events_own_decision_at_is_verified(self, synthetic_calendar):
        """A feature whose decision_at disagrees with the session close is
        incoherent and fails closed, even if availability looks fine."""
        guard = AvailabilityGuard(synthetic_calendar)
        d = next(x for x in synthetic_calendar.sessions() if x >= date(2024, 4, 1))
        close = guard.decision_instant(d)
        lying = self._event(EARLIER, close + timedelta(hours=1))
        with pytest.raises(FutureDataError) as ei:
            guard.check_feature(lying, decision_session=d)
        assert "decision_at" in ei.value.detail or "incoherent" in ei.value.detail

    def test_caller_supplied_instant_no_longer_accepted(self, synthetic_calendar):
        guard = AvailabilityGuard(synthetic_calendar)
        d = next(x for x in synthetic_calendar.sessions() if x >= date(2024, 4, 1))
        ev = self._event(EARLIER, guard.decision_instant(d))
        with pytest.raises(TypeError):
            guard.check_feature(ev, EARLIER)  # positional instant is gone


class TestDeliverableFromContract:
    """F5: the filter must judge standardness from the contract object, not a
    caller-supplied boolean."""

    @pytest.fixture()
    def filt(self, static_calendar):
        return CandidateFilter.from_protocol(static_calendar, load_protocol())

    def test_snapshot_accepts_contract_and_derives_standardness(self, filt):
        adjusted, _action = split_adjusted_contract()
        snap = _snapshot(contract=adjusted)
        d = filt.evaluate(snap)
        assert not d.accepted
        deliverable = next(r for r in d.results if r.rule == "deliverable")
        assert deliverable.status == "FAIL"

    def test_bool_only_snapshot_rejected_at_construction(self):
        with pytest.raises(TypeError, match="contract"):
            _snapshot(standard_contract=True)


class TestSecurityMasterRecordVisibility:
    """F2 (record level): the master RECORD's own available_at gates both
    lookups — a record that arrived after the decision instant is wholly
    invisible (kill-test for mutant M49)."""

    def _late_record(self):
        sec = renamed_and_delisted_security()
        return sec.model_copy(update={"available_at": datetime(2024, 3, 14, 21, 0, tzinfo=UTC)})

    def test_january_cannot_see_record_that_arrived_in_march(self):
        sec = self._late_record()
        january = datetime(2024, 2, 1, 21, 0, tzinfo=UTC)
        with pytest.raises(KeyError):
            sec.ticker_on(date(2024, 2, 1), as_of=january)
        assert sec.listed_on(date(2024, 2, 1), as_of=january) is False

    def test_post_arrival_decision_sees_the_record(self):
        sec = self._late_record()
        march = datetime(2024, 3, 20, 21, 0, tzinfo=UTC)
        assert sec.ticker_on(date(2024, 3, 20), as_of=march) == "OLDA"
        assert sec.listed_on(date(2024, 3, 20), as_of=march) is True


class TestStandardDeliverableActionId:
    """F5 (deliverable level): a standard contract whose DELIVERABLE carries
    corporate-action provenance is adjusted by construction and must be
    rejected at the schema layer (kill-test for mutant M50)."""

    def test_standard_contract_with_deliverable_action_id_rejected(self):
        base = standard_call()
        data = base.model_dump()
        data["deliverable"]["corporate_action_id"] = "SPLIT-2024-06"
        with pytest.raises(ValidationError, match="corporate_action_id"):
            OptionContract(**data)


class TestSnapshotContractCoherence:
    """Round-2 P1: duplicated snapshot fields must agree with the contract
    object they accompany; DTE is derived from the CONTRACT's expiration."""

    @pytest.fixture()
    def filt(self, static_calendar):
        return CandidateFilter.from_protocol(static_calendar, load_protocol())

    def test_mismatched_expiration_not_evaluable(self, filt):
        d = filt.evaluate(_snapshot(expiration=date(2024, 12, 20)))
        assert not d.accepted
        coherence = next(r for r in d.results if r.rule == "contract_coherence")
        assert coherence.status == NOT_EVALUABLE

    def test_mismatched_underlier_not_evaluable(self, filt):
        d = filt.evaluate(_snapshot(underlying_security_id="SEC-OTHER"))
        assert not d.accepted
        assert any(r.rule == "contract_coherence" for r in d.results)

    def test_unknown_decision_session_not_evaluable_not_crash(self, filt):
        """P2: the tri-state promise — an off-calendar session yields a
        NOT_EVALUABLE audit row, not a NotASessionError crash."""
        d = filt.evaluate(_snapshot(decision_session=date(2024, 4, 14)))  # a Sunday
        assert not d.accepted
        coherence = next(r for r in d.results if r.rule == "decision_coherence")
        assert coherence.status == NOT_EVALUABLE
        assert "calendar" in coherence.detail
