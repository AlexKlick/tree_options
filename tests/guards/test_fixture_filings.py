"""Guard tests driven by filing fixtures: INV-02/03/04 fail closed."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from tests.fixtures.filings import (
    form4_fixture,
    future_return_event,
    ten_q_fixture,
)
from tree_options.guards.availability import AvailabilityGuard, FutureDataError
from tree_options.schemas.features import FeatureEvent, PanelRow


@pytest.fixture()
def guard(static_calendar):
    return AvailabilityGuard(static_calendar)


class TestTenQ:
    def test_period_end_keying_rejected_at_ingestion(self, guard, static_calendar):
        filing, _, violating = ten_q_fixture(static_calendar)
        with pytest.raises(FutureDataError) as ei:
            guard.check_filing_provenance(violating, filing)
        assert "acceptance" in ei.value.detail

    def test_period_end_keying_passes_join_gate_this_is_why_provenance_exists(
        self, guard, static_calendar
    ):
        """Documented limitation: an EARLY declared available_at passes the
        join gate; only the filing-provenance gate catches it (INV-03)."""
        _, _, violating = ten_q_fixture(static_calendar)
        guard.check_feature(violating, decision_session=date(2024, 4, 29))

    def test_acceptance_keying_passes_both_gates(self, guard, static_calendar):
        filing, compliant, _ = ten_q_fixture(static_calendar)
        guard.check_filing_provenance(compliant, filing)
        guard.check_feature(compliant, decision_session=date(2024, 4, 29))

    def test_acceptance_5min_after_close_not_usable_same_session(self, guard, static_calendar):
        _, compliant, _ = ten_q_fixture(static_calendar)
        same_day = compliant.model_copy(
            update={"decision_at": guard.decision_instant(date(2024, 4, 26))}
        )
        with pytest.raises(FutureDataError):
            guard.check_feature(same_day, decision_session=date(2024, 4, 26))


class TestForm4:
    def test_transaction_date_keying_rejected_at_ingestion(self, guard, static_calendar):
        filing, _, violating, _, _ = form4_fixture(static_calendar)
        with pytest.raises(FutureDataError):
            guard.check_filing_provenance(violating, filing)

    def test_usable_only_after_publication(self, guard, static_calendar):
        filing, compliant, _, t, t2 = form4_fixture(static_calendar)
        guard.check_filing_provenance(compliant, filing)
        # Join gate: not usable at transaction session t nor acceptance session
        # t2 (accepted 5 min after t2's close); usable from t2's next session.
        with pytest.raises(FutureDataError):
            guard.check_feature(compliant, decision_session=t)
        with pytest.raises(FutureDataError):
            guard.check_feature(compliant, decision_session=t2)
        guard.check_feature(compliant, decision_session=static_calendar.nth_after(t2, 1))

    def test_wrong_source_record_rejected(self, guard, static_calendar):
        filing, compliant, _ = ten_q_fixture(static_calendar)
        misattributed = compliant.model_copy(update={"source_record_id": "other-record"})
        with pytest.raises(FutureDataError):
            guard.check_filing_provenance(misattributed, filing)


class TestNegativeControl:
    def test_future_return_feature_rejected(self, guard, static_calendar):
        """Handoff §10: injected future_return must be provably rejected."""
        sessions = static_calendar.sessions()
        for session in (sessions[10], sessions[500], sessions[-40]):
            ev = future_return_event(static_calendar, session, horizon=1)
            with pytest.raises(FutureDataError) as ei:
                guard.check_feature(ev, decision_session=session)
            assert ei.value.code == "AVAILABLE_AFTER_DECISION"

    def test_inclusive_boundary_at_close(self, guard, static_calendar):
        """D3: available_at == decision_at exactly is usable (inclusive)."""
        session = static_calendar.sessions()[100]
        close = guard.decision_instant(session)
        ev = FeatureEvent(
            security_id="SEC-B",
            feature_name="close_derived_feature",
            value=1.0,
            observed_at=close - timedelta(minutes=1),
            available_at=close,
            decision_at=close,
            source="vendor_prices",
            source_record_id="r",
            revision_id="r1",
        )
        guard.check_feature(ev, decision_session=session)
        with pytest.raises(FutureDataError):
            guard.check_feature(
                ev.model_copy(update={"available_at": close + timedelta(seconds=1)}),
                decision_session=session,
            )


class TestAuditPanel:
    def test_never_silently_drops(self, guard, static_calendar):
        session = date(2024, 4, 29)
        close = guard.decision_instant(session)
        good = FeatureEvent(
            security_id="SEC-A",
            feature_name="ok",
            value=1.0,
            observed_at=close,
            available_at=close,
            decision_at=close,
            source="s",
            source_record_id="r",
            revision_id="r1",
        )
        _, accepted_1605, _ = ten_q_fixture(static_calendar)
        bound_1605 = accepted_1605.model_copy(
            update={"decision_at": guard.decision_instant(date(2024, 4, 26))}
        )
        rows = [
            PanelRow(security_id="SEC-A", decision_session=session, features=(good,)),
            PanelRow(
                security_id="SEC-10Q",
                decision_session=date(2024, 4, 26),  # accepted 16:05: 5 min late
                features=(bound_1605,),
            ),
        ]
        result = guard.audit_panel(rows)
        assert len(result.compliant) == 1
        assert result.n_rejected >= 1
        assert all(r.code == "AVAILABLE_AFTER_DECISION" for r in result.rejections)

    def test_unknown_session_fails_closed(self, guard):
        with pytest.raises(FutureDataError) as ei:
            guard.decision_instant(date(2024, 1, 1))  # NYSE holiday
        assert ei.value.code == "SESSION_NOT_IN_CALENDAR"
