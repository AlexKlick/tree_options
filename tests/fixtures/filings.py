"""Filing fixtures: 10-Q (INV-03), Form 4 (INV-04), future_return control.

Two distinct gates are exercised:
- join gate: available_at <= decision_at (negative control, boundary tests)
- filing provenance gate: filing-derived features available only from
  acceptance time — catches period-end / transaction-date keying, which
  declares an EARLY available_at and therefore passes the join gate.
"""

from __future__ import annotations

from datetime import date, timedelta

from tree_options.schemas.features import FeatureEvent
from tree_options.schemas.filings import FilingRecord

Q1_10Q_FIELDS = dict(
    source_record_id="0000950123-24-004567",
    filer_security_id="SEC-10Q",
    period_of_report=date(2024, 2, 29),
)

FORM4_FIELDS = dict(
    source_record_id="0001127602-24-003321",
    filer_security_id="SEC-F4",
    event_date=date(2024, 3, 4),
)


def ten_q_fixture(calendar):
    """10-Q: period ends 2024-02-29; accepted 2024-04-26 ~16:05 ET."""
    acceptance = calendar.session_close(date(2024, 4, 26)) + timedelta(minutes=5)
    filing = FilingRecord.quarterly_report(**Q1_10Q_FIELDS, acceptance_instant=acceptance)
    compliant = FeatureEvent(
        security_id="SEC-10Q",
        feature_name="fund_current_ratio",
        value=1.8,
        observed_at=acceptance,
        available_at=acceptance,
        decision_at=calendar.session_close(date(2024, 4, 29)),
        source="edgar_xbrl",
        source_record_id=filing.source_record_id,
        revision_id="r1",
    )
    violating = compliant.model_copy(
        update=dict(
            feature_name="fund_current_ratio_PERIOD_END",
            observed_at=calendar.session_close(date(2024, 2, 29)),
            available_at=calendar.session_close(date(2024, 2, 29)),
        )
    )
    return filing, compliant, violating


def form4_fixture(calendar):
    """Form 4: transaction 2024-03-04; accepted 2024-03-06 ~16:05 ET."""
    t, t2 = FORM4_FIELDS["event_date"], date(2024, 3, 6)
    acceptance = calendar.session_close(t2) + timedelta(minutes=5)
    filing = FilingRecord.form4(**{**FORM4_FIELDS, "acceptance_instant": acceptance})
    compliant = FeatureEvent(
        security_id="SEC-F4",
        feature_name="insider_net_sell_shares",
        value=-25_000.0,
        observed_at=acceptance,
        available_at=acceptance,
        decision_at=calendar.session_close(date(2024, 3, 7)),
        source="edgar_form4",
        source_record_id=filing.source_record_id,
        revision_id="r1",
    )
    violating = compliant.model_copy(
        update=dict(
            feature_name="insider_net_sell_shares_TXN_DATE",
            observed_at=calendar.session_close(t),
            available_at=calendar.session_close(t),
        )
    )
    return filing, compliant, violating, t, t2


def future_return_event(calendar, session: date, horizon: int = 1) -> FeatureEvent:
    """Negative control (§10): a feature that IS the future return."""
    available = calendar.session_close(calendar.nth_after(session, horizon))
    return FeatureEvent(
        security_id="SEC-CTRL",
        feature_name="future_return",
        value=0.1234,
        observed_at=available,
        available_at=available,
        decision_at=calendar.session_close(session),
        source="negative_control",
        source_record_id="ctrl-1",
        revision_id="r1",
    )
