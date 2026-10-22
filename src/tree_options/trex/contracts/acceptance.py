"""C3.4: ``trex-acceptance/1``, the Phase 5 paper acceptance verdict.

The verdict is COMPUTED, never asserted. A document carries its inputs
(per-session records, per-trade lifecycle records, drill attestations), the
slice of the NYSE calendar they were judged against, and the derived
results. Validating a document recomputes every derived field from the
inputs in the same document and refuses any difference, so a hand-edited
verdict, streak or counter cannot parse. ``verify_acceptance`` additionally
checks the embedded calendar slice against the repo calendar, which is what
stops a document from hiding trailing unrecorded sessions.

Rules (frozen; WAVE3-CONTRACTS.md C3.4):

- A session QUALIFIES when every one of these holds:
  ``100 * heartbeat_minutes >= 99 * regular_minutes`` (the desk owner's
  heartbeat was fresh for at least 99% of the regular-hours minutes);
  reconciliation was clean at the close, every not-clean episode was
  resolved inside the session, and a not-clean open was one of those
  resolved episodes; ``unowned_orders == 0``; ``max_exit_unguarded_s <=
  300``. An unknown metric (null, with its reason) never qualifies.
- The STREAK is the run of consecutive calendar sessions that qualify,
  ending at the last session of the window (the last session whose close is
  at or before ``computed_at``). A session without a record, or a
  non-qualifying one, resets it to zero.
- A LIFECYCLE is complete when it was approved, filled at least one unit,
  exited by policy or by flatten, was then seen flat at the broker, was then
  reconciled clean, and every execution has its commission. It counts toward
  acceptance when it is complete and lies inside the current streak (approved
  on or after the streak's first session, reconciled on or before its last;
  ET dates).
- Every drill must be ``passed``, with evidence, attested by an owner.
- ``met`` needs a streak of at least 10 sessions, at least 10 counted
  lifecycles per asset kind (option, equity) and all 7 drills.

``live_authorized`` is the literal ``false``: nothing here unlocks live
trading, whatever the verdict.

This module also holds the primitives Wave 3 shares (``OwnerEmail``,
``EvidenceRef``, ``ReasonCode``); ``contracts.wave3`` re-exports them. Pure:
no I/O, no broker library; durations are whole seconds (no ``timedelta``).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date, datetime
from typing import Annotated, Any, Final, Literal

from pydantic import BeforeValidator, Field, StringConstraints, model_validator

from tree_options.schemas.common import StrictModel
from tree_options.time.calendar import SessionCalendar, calendar_content_sha256
from tree_options.time.sessions import SESSION_TIMEZONE, shift_instant
from tree_options.trex.contracts.canonical import (
    IdempotencyKey,
    PathSafeId,
    Sha256Hex,
    UTCInstant,
)

__all__ = [
    "ACCEPTANCE_SCHEMA",
    "ACCEPTANCE_VIEW_SCHEMA",
    "ASSET_KINDS",
    "DRILL_IDS",
    "DRILL_REASONS",
    "HEARTBEAT_MIN_PERCENT",
    "LIFECYCLE_REASONS",
    "MAX_EXIT_UNGUARDED_S",
    "OPEN_CHECK_WINDOW_S",
    "REQUIRED_LIFECYCLES_PER_KIND",
    "REQUIRED_STREAK_SESSIONS",
    "SESSION_METRICS",
    "SESSION_REASONS",
    "VERDICT_REASONS",
    "AcceptanceDocument",
    "AcceptanceView",
    "AssetKind",
    "CalendarSession",
    "DrillId",
    "DrillRecord",
    "DrillResult",
    "EvidenceRef",
    "FalseLiteral",
    "KindCounters",
    "LifecycleRecord",
    "LifecycleResult",
    "OwnerEmail",
    "PerKind",
    "ReasonCode",
    "ReconciliationEpisode",
    "SessionRecord",
    "SessionResult",
    "SevenLiteral",
    "StreakSummary",
    "TenLiteral",
    "compute_acceptance",
    "et_date",
    "verify_acceptance",
]

ACCEPTANCE_SCHEMA: Final = "trex-acceptance/1"
ACCEPTANCE_VIEW_SCHEMA: Final = "trex-acceptance-view/1"

REQUIRED_STREAK_SESSIONS: Final = 10
REQUIRED_LIFECYCLES_PER_KIND: Final = 10
#: ``100 * heartbeat_minutes >= HEARTBEAT_MIN_PERCENT * regular_minutes``.
HEARTBEAT_MIN_PERCENT: Final = 99
#: The longest continuous time an open position may have no armed exit owner.
MAX_EXIT_UNGUARDED_S: Final = 300
#: ``reconciliation_open`` is the first reconciliation in [open, open + this];
#: ``reconciliation_close`` the last in [close - this, close].
OPEN_CHECK_WINDOW_S: Final = 900

DrillId = Literal[
    "relogin",
    "restart_mid_position",
    "gateway_outage",
    "duplicate_command",
    "backup_restore",
    "notification",
    "unassisted_use",
]
#: Every drill, in the order a document lists them.
DRILL_IDS: Final[tuple[DrillId, ...]] = (
    "relogin",
    "restart_mid_position",
    "gateway_outage",
    "duplicate_command",
    "backup_restore",
    "notification",
    "unassisted_use",
)

AssetKind = Literal["option", "equity"]
ASSET_KINDS: Final[tuple[AssetKind, ...]] = ("option", "equity")

# -- shared primitives ---------------------------------------------------------

#: A lower-case email address (family-apps lower-cases the subject it signs).
OwnerEmail = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z0-9._%+-]+@[a-z0-9-]+(\.[a-z0-9-]+)+$", max_length=254),
]

#: A machine-readable reason code: snake case, optionally ``code:detail``.
ReasonCode = Annotated[
    str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,63}(:[a-z0-9_.-]{1,64})?$")
]

#: A relative POSIX path of path-safe segments (no ``..``, no leading ``/``).
EvidencePath = Annotated[
    str,
    StringConstraints(
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}(/[A-Za-z0-9][A-Za-z0-9._-]{0,95}){0,7}$"
    ),
]

NonNegativeInt = Annotated[int, Field(strict=True, ge=0)]


def _exactly(kind: type) -> BeforeValidator:
    """A literal field accepts only its own JSON type (pydantic's lax
    ``Literal`` takes ``0`` for ``false`` and ``2.0`` for ``2``)."""

    def check(value: Any) -> Any:
        if type(value) is not kind:
            raise ValueError(f"expected a JSON {kind.__name__}")
        return value

    return BeforeValidator(check)


#: ``false`` and nothing else (``live_authorized``, ``execution_enabled``).
FalseLiteral = Annotated[Literal[False], _exactly(bool)]
TenLiteral = Annotated[Literal[10], _exactly(int)]
SevenLiteral = Annotated[Literal[7], _exactly(int)]


class EvidenceRef(StrictModel):
    """One evidence file: ``path`` relative to its evidence root, ``sha256``
    of the file bytes. The producer verifies the hash before citing it."""

    path: EvidencePath
    sha256: Sha256Hex


def et_date(instant: datetime) -> date:
    """The America/New_York calendar date of an instant."""
    return instant.astimezone(SESSION_TIMEZONE).date()


def _seconds(later: datetime, earlier: datetime) -> int:
    return int((later - earlier).total_seconds())


# -- inputs ------------------------------------------------------------------------

ReconciliationStatus = Literal["clean", "reconciliation_required", "unavailable"]

SessionMetric = Literal[
    "heartbeat_minutes",
    "reconciliation_open",
    "reconciliation_close",
    "reconciliation_episodes",
    "unowned_orders",
    "max_exit_unguarded_s",
]
SESSION_METRICS: Final[tuple[SessionMetric, ...]] = (
    "heartbeat_minutes",
    "reconciliation_open",
    "reconciliation_close",
    "reconciliation_episodes",
    "unowned_orders",
    "max_exit_unguarded_s",
)


class CalendarSession(StrictModel):
    """One session of the repo NYSE calendar, as the verdict used it."""

    session: date
    opens_at: UTCInstant
    closes_at: UTCInstant

    @model_validator(mode="after")
    def _span(self) -> CalendarSession:
        span = _seconds(self.closes_at, self.opens_at)
        if span <= 0 or span % 60:
            raise ValueError("a session spans a positive whole number of minutes")
        if et_date(self.opens_at) != self.session:
            raise ValueError("a session opens on its own ET date")
        return self

    @property
    def regular_minutes(self) -> int:
        return _seconds(self.closes_at, self.opens_at) // 60


class ReconciliationEpisode(StrictModel):
    """A stretch during which reconciliation was not clean.

    ``resolved_at`` null: still not clean when the record was written."""

    started_at: UTCInstant
    status: Literal["reconciliation_required", "unavailable"]
    resolved_at: UTCInstant | None

    @model_validator(mode="after")
    def _order(self) -> ReconciliationEpisode:
        if self.resolved_at is not None and self.resolved_at < self.started_at:
            raise ValueError("an episode resolves after it starts")
        return self


class SessionRecord(StrictModel):
    """What the acceptance recorder observed over one regular session.

    Every metric is nullable; a null metric is UNKNOWN and is named in
    ``unavailable`` with its reason (the keys are exactly the null metrics).
    """

    session: date
    heartbeat_minutes: NonNegativeInt | None
    reconciliation_open: ReconciliationStatus | None
    reconciliation_close: ReconciliationStatus | None
    reconciliation_episodes: tuple[ReconciliationEpisode, ...] | None
    unowned_orders: NonNegativeInt | None
    max_exit_unguarded_s: NonNegativeInt | None
    unavailable: dict[SessionMetric, ReasonCode]
    evidence: tuple[EvidenceRef, ...]

    @model_validator(mode="after")
    def _unknowns_named(self) -> SessionRecord:
        nulls = {name for name in SESSION_METRICS if getattr(self, name) is None}
        if nulls != set(self.unavailable):
            raise ValueError("unavailable names exactly the null metrics")
        return self


ExitBy = Literal["policy", "flatten", "manual", "not_exited"]


class LifecycleRecord(StrictModel):
    """One approved paper entry, followed to the end of its life.

    Conditional nulls: ``first_fill_at`` is set iff ``filled_quantity >= 1``;
    ``exited_at`` iff ``exit_by != "not_exited"``. ``broker_flat_at`` and
    ``reconciled_at`` are null until observed. ``commissions_complete`` null
    is unknown and needs ``commissions_unavailable_reason``.
    """

    intent_id: PathSafeId
    asset_kind: AssetKind
    proposal_id: Sha256Hex
    approval_key: IdempotencyKey
    approved_at: UTCInstant
    filled_quantity: NonNegativeInt
    first_fill_at: UTCInstant | None
    exit_by: ExitBy
    exited_at: UTCInstant | None
    broker_flat_at: UTCInstant | None
    reconciled_at: UTCInstant | None
    commissions_complete: bool | None
    commissions_unavailable_reason: ReasonCode | None
    evidence: tuple[EvidenceRef, ...]

    @model_validator(mode="after")
    def _shape(self) -> LifecycleRecord:
        if (self.filled_quantity >= 1) != (self.first_fill_at is not None):
            raise ValueError("first_fill_at is set exactly when something filled")
        if (self.exit_by != "not_exited") != (self.exited_at is not None):
            raise ValueError("exited_at is set exactly when the position exited")
        if self.exit_by != "not_exited" and self.filled_quantity == 0:
            raise ValueError("nothing filled, so nothing exited")
        if (self.commissions_complete is None) != (self.commissions_unavailable_reason is not None):
            raise ValueError("unknown commissions carry a reason")
        steps = [self.approved_at, self.first_fill_at, self.exited_at]
        known = [s for s in steps if s is not None]
        if known != sorted(known):
            raise ValueError("approved, filled and exited happen in that order")
        if self.broker_flat_at is not None and (
            self.exited_at is None or self.broker_flat_at < self.exited_at
        ):
            raise ValueError("the broker is seen flat only after the exit")
        if self.reconciled_at is not None and (
            self.broker_flat_at is None or self.reconciled_at < self.broker_flat_at
        ):
            raise ValueError("reconciliation follows the flat observation")
        return self


class DrillRecord(StrictModel):
    """One operational drill. ``not_run`` carries nothing; ``passed`` and
    ``failed`` carry evidence, the attesting owner and the time."""

    id: DrillId
    status: Literal["not_run", "passed", "failed"]
    evidence: tuple[EvidenceRef, ...]
    attested_by: OwnerEmail | None
    at: UTCInstant | None
    note: Annotated[str, Field(max_length=2000)]

    @model_validator(mode="after")
    def _shape(self) -> DrillRecord:
        ran = self.status != "not_run"
        if ran != bool(self.evidence):
            raise ValueError("a run drill cites evidence; a drill not run cites none")
        if ran != (self.attested_by is not None) or ran != (self.at is not None):
            raise ValueError("attested_by and at are set exactly when the drill ran")
        return self


# -- derived -----------------------------------------------------------------------

#: Why a session does not qualify (wire strings, frozen by a test).
SESSION_REASONS: Final = frozenset(
    {
        "session_not_recorded",
        "heartbeat_unavailable",
        "heartbeat_minutes_exceed_session",
        "heartbeat_below_99pct",
        "reconciliation_unavailable",
        "reconciliation_not_clean_at_close",
        "reconciliation_open_unresolved",
        "reconciliation_unresolved_in_session",
        "unowned_orders_unavailable",
        "unowned_orders_present",
        "exit_guard_unavailable",
        "exit_unguarded_over_5min",
    }
)

#: Why a lifecycle is not complete (wire strings, frozen by a test).
LIFECYCLE_REASONS: Final = frozenset(
    {
        "not_filled",
        "not_exited",
        "exit_not_by_policy",
        "broker_not_flat",
        "not_reconciled",
        "commissions_unknown",
        "commissions_missing",
    }
)

#: Why a drill does not pass (wire strings, frozen by a test).
DRILL_REASONS: Final = frozenset({"drill_not_run", "drill_failed", "attester_not_owner"})

#: Why the verdict is ``not_met``; ``drill_not_passed`` carries ``:<drill id>``.
VERDICT_REASONS: Final = frozenset(
    {"streak_short", "option_lifecycles_short", "equity_lifecycles_short", "drill_not_passed"}
)


class SessionResult(StrictModel):
    session: date
    recorded: bool
    qualifying: bool
    reasons: tuple[str, ...]


class LifecycleResult(StrictModel):
    intent_id: PathSafeId
    asset_kind: AssetKind
    completed: bool
    counted: bool
    reasons: tuple[str, ...]


class DrillResult(StrictModel):
    id: DrillId
    passed: bool
    reasons: tuple[str, ...]


class StreakSummary(StrictModel):
    """``current_start``/``current_end`` are set iff ``current_sessions > 0``."""

    current_sessions: NonNegativeInt
    current_start: date | None
    current_end: date | None
    best_sessions: NonNegativeInt
    required: TenLiteral

    @model_validator(mode="after")
    def _shape(self) -> StreakSummary:
        running = self.current_sessions > 0
        if running != (self.current_start is not None) or running != (self.current_end is not None):
            raise ValueError("start and end are set exactly when a streak is running")
        if self.best_sessions < self.current_sessions:
            raise ValueError("the best streak is at least the current one")
        return self


class KindCounters(StrictModel):
    completed_total: NonNegativeInt
    counted: NonNegativeInt
    required: TenLiteral


class PerKind(StrictModel):
    option: KindCounters
    equity: KindCounters


def _session_reasons(record: SessionRecord, cal: CalendarSession) -> list[str]:
    reasons: list[str] = []
    minutes = record.heartbeat_minutes
    if minutes is None:
        reasons.append("heartbeat_unavailable")
    elif minutes > cal.regular_minutes:
        reasons.append("heartbeat_minutes_exceed_session")
    elif 100 * minutes < HEARTBEAT_MIN_PERCENT * cal.regular_minutes:
        reasons.append("heartbeat_below_99pct")

    rec_open, rec_close = record.reconciliation_open, record.reconciliation_close
    episodes = record.reconciliation_episodes
    if rec_open is None or rec_close is None or episodes is None:
        reasons.append("reconciliation_unavailable")
    else:
        if rec_close != "clean":
            reasons.append("reconciliation_not_clean_at_close")
        if any(e.resolved_at is None or e.resolved_at > cal.closes_at for e in episodes):
            reasons.append("reconciliation_unresolved_in_session")
        open_check_end = shift_instant(cal.opens_at, OPEN_CHECK_WINDOW_S)
        if rec_open != "clean" and not any(e.started_at <= open_check_end for e in episodes):
            # a not-clean open must be one of the episodes this record resolves
            reasons.append("reconciliation_open_unresolved")

    if record.unowned_orders is None:
        reasons.append("unowned_orders_unavailable")
    elif record.unowned_orders > 0:
        reasons.append("unowned_orders_present")

    if record.max_exit_unguarded_s is None:
        reasons.append("exit_guard_unavailable")
    elif record.max_exit_unguarded_s > MAX_EXIT_UNGUARDED_S:
        reasons.append("exit_unguarded_over_5min")
    return sorted(reasons)


def _lifecycle_reasons(record: LifecycleRecord) -> list[str]:
    reasons: list[str] = []
    if record.filled_quantity < 1:
        reasons.append("not_filled")
    if record.exit_by == "not_exited":
        reasons.append("not_exited")
    elif record.exit_by == "manual":
        reasons.append("exit_not_by_policy")
    if record.broker_flat_at is None:
        reasons.append("broker_not_flat")
    if record.reconciled_at is None:
        reasons.append("not_reconciled")
    if record.commissions_complete is None:
        reasons.append("commissions_unknown")
    elif not record.commissions_complete:
        reasons.append("commissions_missing")
    return sorted(reasons)


def _drill_reasons(record: DrillRecord, owners: frozenset[str]) -> list[str]:
    if record.status == "not_run":
        return ["drill_not_run"]
    reasons = []
    if record.status == "failed":
        reasons.append("drill_failed")
    if record.attested_by not in owners:
        reasons.append("attester_not_owner")
    return reasons


def _derive(
    window: Sequence[CalendarSession],
    sessions: Sequence[SessionRecord],
    lifecycles: Sequence[LifecycleRecord],
    drills: Sequence[DrillRecord],
    owners: Iterable[str],
) -> dict[str, Any]:
    """Every derived field of a document, from its inputs alone."""
    by_session = {record.session: record for record in sessions}
    session_results = []
    for cal in window:
        record = by_session.get(cal.session)
        reasons = ["session_not_recorded"] if record is None else _session_reasons(record, cal)
        session_results.append(
            SessionResult(
                session=cal.session,
                recorded=record is not None,
                qualifying=not reasons,
                reasons=tuple(reasons),
            )
        )

    best = run = 0
    for result in session_results:
        run = run + 1 if result.qualifying else 0
        best = max(best, run)
    current = run
    start = session_results[-current].session if current else None
    end = session_results[-1].session if current else None
    streak = StreakSummary(
        current_sessions=current,
        current_start=start,
        current_end=end,
        best_sessions=best,
        required=REQUIRED_STREAK_SESSIONS,
    )

    lifecycle_results = []
    for life in lifecycles:
        reasons = _lifecycle_reasons(life)
        completed = not reasons
        counted = (
            completed
            and start is not None
            and end is not None
            and life.reconciled_at is not None
            and start <= et_date(life.approved_at)
            and et_date(life.reconciled_at) <= end
        )
        lifecycle_results.append(
            LifecycleResult(
                intent_id=life.intent_id,
                asset_kind=life.asset_kind,
                completed=completed,
                counted=counted,
                reasons=tuple(reasons),
            )
        )

    def counters(kind: AssetKind) -> KindCounters:
        rows = [r for r in lifecycle_results if r.asset_kind == kind]
        return KindCounters(
            completed_total=sum(r.completed for r in rows),
            counted=sum(r.counted for r in rows),
            required=REQUIRED_LIFECYCLES_PER_KIND,
        )

    per_kind = PerKind(option=counters("option"), equity=counters("equity"))
    owner_set = frozenset(owners)
    drill_results = []
    for drill in drills:
        reasons = _drill_reasons(drill, owner_set)
        drill_results.append(DrillResult(id=drill.id, passed=not reasons, reasons=tuple(reasons)))
    drills_passed = sum(r.passed for r in drill_results)

    verdict_reasons: list[str] = []
    if current < REQUIRED_STREAK_SESSIONS:
        verdict_reasons.append("streak_short")
    for kind in ASSET_KINDS:
        if getattr(per_kind, kind).counted < REQUIRED_LIFECYCLES_PER_KIND:
            verdict_reasons.append(f"{kind}_lifecycles_short")
    verdict_reasons.extend(f"drill_not_passed:{r.id}" for r in drill_results if not r.passed)
    return {
        "session_results": tuple(session_results),
        "lifecycle_results": tuple(lifecycle_results),
        "drill_results": tuple(drill_results),
        "streak": streak,
        "per_kind": per_kind,
        "drills_passed": drills_passed,
        "verdict": "not_met" if verdict_reasons else "met",
        "verdict_reasons": tuple(verdict_reasons),
    }


class AcceptanceDocument(StrictModel):
    """``trex-acceptance/1``: inputs, the calendar slice, derived results."""

    schema_version: Literal["trex-acceptance/1"] = Field(
        default="trex-acceptance/1", alias="schema"
    )
    computed_at: UTCInstant
    calendar_sha256: Sha256Hex
    owners: tuple[OwnerEmail, ...]
    calendar_window: tuple[CalendarSession, ...]
    sessions: tuple[SessionRecord, ...]
    lifecycles: tuple[LifecycleRecord, ...]
    drills: tuple[DrillRecord, ...]
    # -- derived (recomputed and compared on every validation) --
    session_results: tuple[SessionResult, ...]
    lifecycle_results: tuple[LifecycleResult, ...]
    drill_results: tuple[DrillResult, ...]
    streak: StreakSummary
    per_kind: PerKind
    drills_passed: NonNegativeInt
    drills_required: SevenLiteral
    verdict: Literal["met", "not_met"]
    verdict_reasons: tuple[str, ...]
    live_authorized: FalseLiteral

    @model_validator(mode="after")
    def _inputs_and_derivation(self) -> AcceptanceDocument:
        if not self.owners or list(self.owners) != sorted(set(self.owners)):
            raise ValueError("owners is a non-empty sorted list without repeats")
        window = [c.session for c in self.calendar_window]
        if window != sorted(set(window)):
            raise ValueError("the calendar window is strictly increasing")
        if self.calendar_window and self.calendar_window[-1].closes_at > self.computed_at:
            raise ValueError("the window ends at a session closed by computed_at")
        recorded = [r.session for r in self.sessions]
        if recorded != sorted(set(recorded)):
            raise ValueError("session records are sorted, one per session")
        if not set(recorded) <= set(window):
            raise ValueError("every session record lies in the calendar window")
        if recorded and window and recorded[0] != window[0]:
            raise ValueError("the window starts at the first recorded session")
        if bool(recorded) != bool(window):
            raise ValueError("the window is empty exactly when nothing was recorded")
        intents = [r.intent_id for r in self.lifecycles]
        if len(intents) != len(set(intents)):
            raise ValueError("one lifecycle per intent")
        if tuple(d.id for d in self.drills) != DRILL_IDS:
            raise ValueError("every drill, once, in DRILL_IDS order")
        derived = _derive(
            self.calendar_window, self.sessions, self.lifecycles, self.drills, self.owners
        )
        for name, value in derived.items():
            if getattr(self, name) != value:
                raise ValueError(f"{name} must equal its recomputation from the inputs")
        return self


def _window(
    calendar: SessionCalendar, first: date | None, computed_at: datetime
) -> list[CalendarSession]:
    if first is None:
        return []
    out: list[CalendarSession] = []
    for session in calendar.sessions():
        if session < first:
            continue
        closes = calendar.session_close(session)
        if closes > computed_at:
            break
        out.append(
            CalendarSession(
                session=session, opens_at=calendar.session_open(session), closes_at=closes
            )
        )
    return out


def compute_acceptance(
    *,
    sessions: Iterable[SessionRecord],
    lifecycles: Iterable[LifecycleRecord],
    drills: Iterable[DrillRecord],
    owners: Iterable[str],
    calendar: SessionCalendar,
    computed_at: datetime,
) -> AcceptanceDocument:
    """The one way to build a document: the verdict is derived, never passed in.

    A drill id with no record is listed ``not_run``. A session record outside
    the window (not a calendar session, or not closed by ``computed_at``)
    refuses: the recorder wrote it too early or for a non-session.
    """
    records = sorted(sessions, key=lambda r: r.session)
    by_id = {d.id: d for d in drills}
    drill_rows = [
        by_id.get(drill_id)
        or DrillRecord(
            id=drill_id, status="not_run", evidence=(), attested_by=None, at=None, note=""
        )
        for drill_id in DRILL_IDS
    ]
    lifecycle_rows = sorted(lifecycles, key=lambda r: (r.approved_at, r.intent_id))
    owner_rows = tuple(sorted(set(owners)))
    window = _window(calendar, records[0].session if records else None, computed_at)
    derived = _derive(window, records, lifecycle_rows, drill_rows, owner_rows)
    return AcceptanceDocument.model_validate(
        {
            "schema": ACCEPTANCE_SCHEMA,
            "computed_at": computed_at,
            "calendar_sha256": calendar_content_sha256(calendar),
            "owners": owner_rows,
            "calendar_window": tuple(window),
            "sessions": tuple(records),
            "lifecycles": tuple(lifecycle_rows),
            "drills": tuple(drill_rows),
            **derived,
            "drills_required": len(DRILL_IDS),
            "live_authorized": False,
        }
    )


def verify_acceptance(document: AcceptanceDocument, calendar: SessionCalendar) -> None:
    """Refuse (``ValueError``) a document judged against another calendar or
    a truncated window. Derivation itself was checked when it validated."""
    if document.calendar_sha256 != calendar_content_sha256(calendar):
        raise ValueError("acceptance_calendar_mismatch")
    first = document.calendar_window[0].session if document.calendar_window else None
    expected = _window(calendar, first, document.computed_at)
    if list(document.calendar_window) != expected:
        raise ValueError("acceptance_window_mismatch")


class AcceptanceView(StrictModel):
    """``GET /api/acceptance``: the verified document, or why there is none.

    ``document`` is set iff ``status == "ok"``; ``unavailable_reason`` iff
    ``status == "unavailable"`` (``acceptance_missing``, ``acceptance_invalid``,
    ``acceptance_calendar_mismatch``, ``acceptance_window_mismatch``)."""

    schema_version: Literal["trex-acceptance-view/1"] = Field(
        default="trex-acceptance-view/1", alias="schema"
    )
    now: UTCInstant
    status: Literal["ok", "unavailable"]
    unavailable_reason: ReasonCode | None
    document: AcceptanceDocument | None
    live_authorized: FalseLiteral

    @model_validator(mode="after")
    def _shape(self) -> AcceptanceView:
        if (self.status == "unavailable") != (self.unavailable_reason is not None):
            raise ValueError("unavailable_reason is set exactly when unavailable")
        if (self.status == "ok") != (self.document is not None):
            raise ValueError("the document is present exactly when ok")
        return self
