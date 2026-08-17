"""Availability join gate (INV-02/03/04).

A feature is usable for a decision at session t iff
`available_at <= decision_at` where decision_at is exactly `session_close(t)`
(16:00 America/New_York, DST-correct UTC instant) — inclusive, per protocol
`timestamp_semantics.availability_rule`. `decision_instant` is the ONLY way
decision_at is constructed, so the comparison basis cannot drift.

Fundamentals (INV-03): availability is filing acceptance/publication time,
never fiscal period end — the guard only ever sees `available_at`; fixtures
prove the 10-Q boundary. Insider filings (INV-04): the Form-4 acceptance
instant is the available_at; the private transaction date is not.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Iterable, Literal

from tree_options.schemas.features import FeatureEvent, PanelRow
from tree_options.schemas.filings import FilingRecord
from tree_options.time.calendar import SessionCalendar

AvailabilityCode = Literal[
    "AVAILABLE_AFTER_DECISION",
    "OBSERVED_AFTER_AVAILABLE",
    "SESSION_NOT_IN_CALENDAR",
]


class FutureDataError(RuntimeError):
    def __init__(self, code: AvailabilityCode, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class JoinRejection:
    row_ref: str
    security_id: str
    feature_name: str
    code: AvailabilityCode
    detail: str


@dataclass(frozen=True)
class JoinResult:
    compliant: tuple[PanelRow, ...]
    rejections: tuple[JoinRejection, ...]

    @property
    def n_rejected(self) -> int:
        return len(self.rejections)


class AvailabilityGuard:
    def __init__(self, calendar: SessionCalendar) -> None:
        self.calendar = calendar

    def decision_instant(self, session: date) -> datetime:
        if not self.calendar.is_session(session):
            raise FutureDataError(
                "SESSION_NOT_IN_CALENDAR", f"decision session {session} not in calendar"
            )
        return self.calendar.session_close(session)

    def check_feature(self, ev: FeatureEvent, decision_at: datetime) -> None:
        """Raise FutureDataError unless the event is usable at decision_at."""
        if ev.available_at <= decision_at:
            return
        raise FutureDataError(
            "AVAILABLE_AFTER_DECISION",
            f"feature {ev.feature_name!r} available {ev.available_at} "
            f"after decision {decision_at}",
        )

    def check_filing_provenance(self, ev: FeatureEvent, filing: FilingRecord) -> None:
        """INV-03/04 ingestion gate: a filing-derived feature may not claim
        availability before the filing is public.

        The join gate alone cannot catch period-end or transaction-date
        keying: those declare an EARLY available_at, not a late one. This
        check ties the feature's availability to the source record.
        """
        if ev.source_record_id != filing.source_record_id:
            raise FutureDataError(
                "OBSERVED_AFTER_AVAILABLE",
                f"feature {ev.feature_name!r} cites source record "
                f"{ev.source_record_id!r} but the filing under check is "
                f"{filing.source_record_id!r}",
            )
        if ev.available_at < filing.acceptance_instant:
            raise FutureDataError(
                "AVAILABLE_AFTER_DECISION",
                f"feature {ev.feature_name!r} available {ev.available_at} before "
                f"{filing.form_type} acceptance {filing.acceptance_instant} "
                f"(period/event dates are never availability)",
            )

    def audit_panel(self, rows: Iterable[PanelRow]) -> JoinResult:
        """Gate every feature of every row; nothing is silently dropped."""
        compliant: list[PanelRow] = []
        rejections: list[JoinRejection] = []
        for row in rows:
            decision_at = self.decision_instant(row.decision_session)
            row_ok = True
            for f in row.features:
                if f.observed_at > f.available_at:  # backstop; schema enforces too
                    rejections.append(
                        JoinRejection(
                            row_ref=f"{row.security_id}@{row.decision_session}",
                            security_id=row.security_id,
                            feature_name=f.feature_name,
                            code="OBSERVED_AFTER_AVAILABLE",
                            detail=f"observed {f.observed_at} after available {f.available_at}",
                        )
                    )
                    row_ok = False
                    continue
                try:
                    self.check_feature(f, decision_at)
                except FutureDataError as exc:
                    rejections.append(
                        JoinRejection(
                            row_ref=f"{row.security_id}@{row.decision_session}",
                            security_id=row.security_id,
                            feature_name=f.feature_name,
                            code=exc.code,
                            detail=exc.detail,
                        )
                    )
                    row_ok = False
            if row_ok:
                compliant.append(row)
        return JoinResult(compliant=tuple(compliant), rejections=tuple(rejections))
