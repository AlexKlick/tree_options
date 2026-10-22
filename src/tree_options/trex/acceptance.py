"""Wave 4 lane H: the paper acceptance recorder and compute job (C3.4, C3.7).

The verdict is COMPUTED, never asserted: this module only gathers the
inputs of ``trex-acceptance/1`` (``contracts.acceptance``) from evidence and
hands them to ``compute_acceptance``, which derives every result. Nothing
here sends an order, touches a broker, grants authority or unlocks live
trading (``live_authorized`` is the contract's literal ``false``).

One command, ``python -m tree_options.trex.acceptance publish``, run by
``trex-acceptance-publish.timer`` (20 seconds after the previous run ends):

1. **Sample** (on NYSE session days, from one hour before the open to one
   hour after the close). One ``trex-acceptance-sample/1`` line is appended
   to ``<TREX_ACCEPTANCE_DIR>/evidence/samples/<ET date>.jsonl``: the desk
   owner's heartbeat (``book.json``), whether the book holds exposure, the
   ``HALT`` file, the latest reconciliation document
   (``ledger/reconciliation.json``: status, ``as_of``, its sha256 and the
   mismatched contracts) and the gateway watch status. Read-only over the
   desk's files; it never probes the runtime lock (a probe could race the
   desk's own exclusive acquire) and never connects to anything. Every
   unknown is null with its reason.
2. **Sessions.** For every calendar session closed by now that has
   samples, a ``SessionRecord``:

   - ``heartbeat_minutes``: regular-hours minutes holding at least one
     sample, every sample of the minute seeing a heartbeat at most
     ``HEARTBEAT_STALE_S`` (the exit watch's 120 s) old. A minute nobody
     sampled is not fresh (unknown never counts).
   - reconciliation: a timeline of points, one per sample: the document's
     own status at its ``as_of`` when it was fresh (at most 120 s old, the
     contract's ``max_age_s``), else ``unavailable`` at the sample's
     instant. ``reconciliation_open`` is the first point in [open, open +
     15 min], ``reconciliation_close`` the last in [close - 15 min, close];
     an episode starts at the first not-clean point and resolves at the
     next clean one.
   - ``unowned_orders``: distinct broker orders (``perm_id``) that filled
     on the account since the previous session's close with an outside
     attribution in the desk's ledger (an untagged order, a foreign tag, a
     trex tag no proposal explains). Orders that never filled are not
     visible to a broker-free recorder.
   - ``max_exit_unguarded_s``: the longest stretch of the session in which
     the book held (or might have held) exposure without a fresh heartbeat
     or with ``HALT`` standing. An unsampled stretch over ``SAMPLE_GAP_S``
     next to exposure counts as unguarded.

   A record is persisted (``sessions/<date>.json``, written once) when the
   session is ``SESSION_SETTLE_S`` past its close and the ledger read; it
   cites an immutable copy of its in-session samples
   (``evidence/sessions/<date>/samples.jsonl``). Before that it is built in
   memory every run, so a closed session counts at once. Session records
   are derived, never primary: delete them (or pass ``--rebuild``) and they
   are rebuilt from the sample evidence, back to the first sample ever
   recorded (the install date).
3. **Lifecycles.** One ``LifecycleRecord`` per consumed
   ``supervised-mandate/2`` (an owner approval that minted its permit, so
   its order may have reached the broker), from the ledger: entry fills are
   tagged ``trex:sup:<intent>``; the exit is the fill that leaves every
   contract of the intent flat; ``exit_by`` is the desk's ``closed`` event
   reason for the closing structure (``trex:desk:<sid>``), else its last
   ``exit_begin``: ``flatten``, a policy rule (``POLICY_EXIT_REASONS``) or
   ``manual`` (anything else, including an exit nobody explains);
   ``broker_flat_at`` is the first reconciliation after the exit was booked
   that is clean or names none of the intent's contracts, ``reconciled_at``
   the first clean one from there (recorder samples and the ledger's
   ``reconciliation`` markers); commissions are complete when every
   execution has its commission event, pending on the execution's ET date
   and missing after it. Evidence: ``evidence/lifecycles/<intent>/facts.json``.
4. **Drills**: ``drills/<id>.json`` written by the owner through
   ``trex.drills`` (an invalid file is a problem and counts as not run).
5. **The document**: ``compute_acceptance`` over all of it, owners from
   ``TREX_OWNER_EMAILS``, written atomically to ``acceptance.json``
   (canonical bytes). No owner configured: no document.

Exit codes: 0 published, 3 degraded (published or sampled with problems,
or no document), 2 refused configuration, 1 failure.
"""

from __future__ import annotations

import argparse
import bisect
import fcntl
import json
import math
import os
import re
import stat
import sys
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from itertools import pairwise
from pathlib import Path
from typing import Annotated, Any, Final, Literal

from pydantic import Field, ValidationError, model_validator

from tree_options.schemas.common import IdStr, StrictModel
from tree_options.time.calendar import SessionCalendar
from tree_options.time.sessions import shift_instant
from tree_options.trex.contracts.acceptance import (
    DRILL_IDS,
    OPEN_CHECK_WINDOW_S,
    AcceptanceDocument,
    AssetKind,
    CalendarSession,
    DrillRecord,
    EvidenceRef,
    LifecycleRecord,
    ReasonCode,
    ReconciliationEpisode,
    SessionMetric,
    SessionRecord,
    compute_acceptance,
    et_date,
)
from tree_options.trex.contracts.books import LedgerEvent
from tree_options.trex.contracts.canonical import (
    Sha256Hex,
    UTCInstant,
    model_bytes,
    model_document,
    sha256_hex,
)
from tree_options.trex.contracts.defaults import RECONCILIATION_MAX_AGE_S
from tree_options.trex.contracts.effects import SupervisedEquityEffect
from tree_options.trex.contracts.layout import Wave2RunDir, Wave2SupervisedDir
from tree_options.trex.contracts.mandates import SupervisedMandateV2
from tree_options.trex.contracts.proposals import EntryProposal
from tree_options.trex.contracts.risk import Reconciliation
from tree_options.trex.contracts.wave3 import (
    TREX_OWNER_EMAILS_ENV,
    AcceptanceDir,
    parse_owner_emails,
)
from tree_options.trex.ledger import LedgerConflict, read_ledger, write_atomic

__all__ = [
    "ACCEPTANCE_DIR_ENV",
    "DEFAULT_ACCEPTANCE_DIR",
    "EXPOSED_STATUSES",
    "HEARTBEAT_STALE_S",
    "POLICY_EXIT_REASONS",
    "SAMPLE_GAP_S",
    "SAMPLE_MARGIN_S",
    "SAMPLE_SCHEMA",
    "SESSION_SETTLE_S",
    "DeskExit",
    "GatewaySeen",
    "PublishOutcome",
    "RecPoint",
    "ReconciliationSeen",
    "ReconciliationSummary",
    "Sample",
    "SampleFile",
    "Sources",
    "acceptance_dir_from_env",
    "append_sample",
    "build_lifecycle",
    "build_session_record",
    "calendar_session",
    "heartbeat_fresh",
    "heartbeat_minutes",
    "log_lines",
    "longest_unguarded_s",
    "main",
    "max_exit_unguarded_s",
    "previous_close",
    "publish",
    "read_desk_exits",
    "read_samples",
    "reconciliation_summary",
    "sample_days",
    "sample_path",
    "should_sample",
    "take_sample",
    "unowned_orders",
]

ACCEPTANCE_DIR_ENV: Final = "TREX_ACCEPTANCE_DIR"
DEFAULT_ACCEPTANCE_DIR: Final = "~/.local/state/trex/acceptance"
SAMPLE_SCHEMA: Final = "trex-acceptance-sample/1"
FACTS_SCHEMA: Final = "trex-acceptance-lifecycle-facts/1"
SAMPLES_DIR: Final = "samples"

#: A heartbeat at most this old is fresh (``trex.exit_watch.HEARTBEAT_STALE_S``,
#: the Today view's ``stale_after_s``; a test pins the equality).
HEARTBEAT_STALE_S: Final = 120
#: Book statuses that hold exposure (``trex.exit_watch.EXPOSED``; pinned).
EXPOSED_STATUSES: Final = frozenset({"enter_working", "open", "exit_working"})
#: Exit reasons of the desk's own rules: option (``engine.ExitReason``) and
#: equity (``equity_exits.EquityExitReason``) minus ``flatten`` (pinned).
POLICY_EXIT_REASONS: Final = frozenset(
    {
        "touch",
        "take_profit",
        "time_stop",
        "expiry_safety",
        "assignment_risk",
        "breach",
        "stop_loss",
        "stop",
        "target",
        "forced",
        "deadline",
    }
)
#: Sampling runs from this long before the open to this long after the close.
SAMPLE_MARGIN_S: Final = 3600
#: Two samples further apart than this leave the stretch between unobserved.
SAMPLE_GAP_S: Final = 90
#: A session record is persisted this long after the close.
SESSION_SETTLE_S: Final = 300
#: A reconciliation dated this far after the sample is not trusted.
FUTURE_SKEW_S: Final = 5
MAX_FILE_BYTES: Final = 4 * 1024 * 1024
MAX_SAMPLE_FILE_BYTES: Final = 64 * 1024 * 1024
#: A longer line of the desk's event log is skipped, never held in memory.
MAX_LOG_LINE_BYTES: Final = 1024 * 1024
SUP_PREFIX: Final = "trex:sup:"
DESK_PREFIX: Final = "trex:desk:"
_DAY_FILE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.jsonl?$")

PositiveInt = Annotated[int, Field(strict=True, ge=1)]
RecStatus = Literal["clean", "reconciliation_required", "unavailable"]


def acceptance_dir_from_env(env: Mapping[str, str] = os.environ) -> Path:
    """``TREX_ACCEPTANCE_DIR`` (default ``~/.local/state/trex/acceptance``)."""
    return Path(env.get(ACCEPTANCE_DIR_ENV) or DEFAULT_ACCEPTANCE_DIR).expanduser()


@dataclass(frozen=True)
class Sources:
    """What the recorder reads (never writes): the desk run dir, the
    supervised authority dir and the gateway watch state file."""

    run_dir: Path
    supervised_dir: Path
    gateway_state: Path

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> Sources:
        def path(name: str, default: str) -> Path:
            return Path(env.get(name) or default).expanduser()

        return cls(
            run_dir=path("TREX_DESK_RUN_DIR", "~/.local/state/trex/desk-paper"),
            supervised_dir=path("TREX_SUPERVISED_DIR", "~/.local/state/trex/supervised"),
            gateway_state=path("TREX_GATEWAY_STATE", "~/.local/state/trex/gateway.json"),
        )


# =============================================================================
# the sample (trex-acceptance-sample/1)
# =============================================================================


class ReconciliationSeen(StrictModel):
    """The reconciliation document a sample read (its own ``as_of``)."""

    status: RecStatus
    as_of: UTCInstant
    sha256: Sha256Hex
    mismatch_con_ids: tuple[PositiveInt, ...]


class GatewaySeen(StrictModel):
    status: IdStr
    checked_at: UTCInstant


def _known_or_why(name: str, value: object, reason: object) -> None:
    if (value is None) == (reason is None):
        raise ValueError(f"{name}: a value, or null with the reason it is unknown")


class Sample(StrictModel):
    """One recorder observation. Every fact is a value, or null with its
    reason (the Wave 3 null rule)."""

    schema_version: Literal["trex-acceptance-sample/1"] = Field(
        default="trex-acceptance-sample/1", alias="schema"
    )
    at: UTCInstant
    heartbeat_at: UTCInstant | None
    heartbeat_unavailable_reason: ReasonCode | None
    exposed: bool | None
    exposed_unavailable_reason: ReasonCode | None
    halt: bool | None
    halt_unavailable_reason: ReasonCode | None
    reconciliation: ReconciliationSeen | None
    reconciliation_unavailable_reason: ReasonCode | None
    gateway: GatewaySeen | None
    gateway_unavailable_reason: ReasonCode | None

    @model_validator(mode="after")
    def _pairs(self) -> Sample:
        _known_or_why("heartbeat_at", self.heartbeat_at, self.heartbeat_unavailable_reason)
        _known_or_why("exposed", self.exposed, self.exposed_unavailable_reason)
        _known_or_why("halt", self.halt, self.halt_unavailable_reason)
        _known_or_why("reconciliation", self.reconciliation, self.reconciliation_unavailable_reason)
        _known_or_why("gateway", self.gateway, self.gateway_unavailable_reason)
        return self


def _read_regular(path: Path, limit: int = MAX_FILE_BYTES) -> bytes:
    """A regular file's bytes (never through a symlink), at most ``limit``.
    ``FileNotFoundError`` when absent; ``OSError``/``ValueError`` otherwise."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError(f"{path.name}: not a bounded regular file")
        chunks = []
        total = 0
        while chunk := os.read(fd, 1024 * 1024):
            chunks.append(chunk)
            total += len(chunk)
            if total > limit:
                raise ValueError(f"{path.name}: grew past its bound")
        return b"".join(chunks)
    finally:
        os.close(fd)


def _aware(text: object) -> datetime | None:
    """An ISO instant WITH an offset, in UTC; None for anything else."""
    if not isinstance(text, str):
        return None
    try:
        value = datetime.fromisoformat(text)
    except ValueError:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        return None
    return value.astimezone(UTC)


def _book(run_dir: Path) -> tuple[datetime | None, str | None, bool | None, str | None]:
    """(heartbeat, its reason, exposed, its reason) from ``book.json``."""
    try:
        raw = _read_regular(run_dir / "book.json")
    except FileNotFoundError:
        return None, "book_missing", None, "book_missing"
    except (OSError, ValueError):
        return None, "book_unreadable", None, "book_unreadable"
    try:
        doc = json.loads(raw)
    except ValueError:
        return None, "book_unreadable", None, "book_unreadable"
    structures = doc.get("structures") if isinstance(doc, dict) else None
    if not isinstance(structures, dict):
        return None, "book_invalid", None, "book_invalid"
    exposed = any(
        isinstance(s, dict) and s.get("status") in EXPOSED_STATUSES for s in structures.values()
    )
    beat = doc.get("heartbeat")
    if beat is None:
        return None, "heartbeat_absent", exposed, None
    instant = _aware(beat)
    if instant is None:
        return None, "heartbeat_invalid", exposed, None
    return instant, None, exposed, None


def _halt(run_dir: Path) -> tuple[bool | None, str | None]:
    try:
        os.lstat(Wave2RunDir(run_dir).halt())
    except FileNotFoundError:
        return False, None
    except OSError:
        return None, "halt_unreadable"
    return True, None


def _reconciliation(run_dir: Path) -> tuple[ReconciliationSeen | None, str | None]:
    try:
        raw = _read_regular(Wave2RunDir(run_dir).reconciliation())
    except FileNotFoundError:
        return None, "reconciliation_missing"
    except (OSError, ValueError):
        return None, "reconciliation_unreadable"
    try:
        doc = json.loads(raw)
    except ValueError:
        return None, "reconciliation_unreadable"
    try:
        rec = Reconciliation.model_validate(doc)
    except ValidationError:
        return None, "reconciliation_invalid"
    seen = ReconciliationSeen(
        status=rec.status,
        as_of=rec.as_of,
        sha256=sha256_hex(raw),
        mismatch_con_ids=tuple(m.con_id for m in rec.mismatches),
    )
    return seen, None


def _gateway(path: Path) -> tuple[GatewaySeen | None, str | None]:
    missing = "gateway_watch_not_reporting"
    try:
        doc = json.loads(_read_regular(path))
    except (OSError, ValueError):
        return None, missing
    status = doc.get("status") if isinstance(doc, dict) else None
    checked = doc.get("checked_at") if isinstance(doc, dict) else None
    if not isinstance(status, str) or isinstance(checked, bool):
        return None, missing
    if not isinstance(checked, int | float) or not math.isfinite(checked):
        return None, missing
    try:
        return GatewaySeen(status=status, checked_at=datetime.fromtimestamp(checked, UTC)), None
    except (ValidationError, ValueError, OverflowError, OSError):
        return None, missing


def take_sample(sources: Sources, now: datetime) -> Sample:
    """One read-only observation of the desk's files at ``now``."""
    beat, beat_why, exposed, exposed_why = _book(sources.run_dir)
    halt, halt_why = _halt(sources.run_dir)
    rec, rec_why = _reconciliation(sources.run_dir)
    gateway, gateway_why = _gateway(sources.gateway_state)
    return Sample(
        at=now,
        heartbeat_at=beat,
        heartbeat_unavailable_reason=beat_why,
        exposed=exposed,
        exposed_unavailable_reason=exposed_why,
        halt=halt,
        halt_unavailable_reason=halt_why,
        reconciliation=rec,
        reconciliation_unavailable_reason=rec_why,
        gateway=gateway,
        gateway_unavailable_reason=gateway_why,
    )


_take_sample = take_sample


def should_sample(calendar: SessionCalendar, now: datetime) -> bool:
    """On a session day, from ``SAMPLE_MARGIN_S`` before the open to as long
    after the close (ET date of ``now``)."""
    day = et_date(now)
    if not calendar.is_session(day):
        return False
    start = shift_instant(calendar.session_open(day), -SAMPLE_MARGIN_S)
    end = shift_instant(calendar.session_close(day), SAMPLE_MARGIN_S)
    return start <= now <= end


def sample_path(adir: AcceptanceDir, day: date) -> Path:
    return adir.evidence() / SAMPLES_DIR / f"{day.isoformat()}.jsonl"


def sample_days(adir: AcceptanceDir) -> set[date]:
    """Every ET date that has a sample file."""
    return _days(adir.evidence() / SAMPLES_DIR)


def append_sample(adir: AcceptanceDir, sample: Sample) -> Path:
    """Append one canonical line to the sample file of the sample's ET date
    (exclusive lock, fsync). A torn last line from a crash is closed first."""
    path = sample_path(adir, et_date(sample.at))
    path.parent.mkdir(parents=True, exist_ok=True)
    line = model_bytes(sample) + b"\n"
    fd = os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        size = os.fstat(fd).st_size
        if size and os.pread(fd, 1, size - 1) != b"\n":
            line = b"\n" + line
        view = memoryview(line)
        while view:
            view = view[os.write(fd, view) :]
        os.fsync(fd)
    finally:
        os.close(fd)
    return path


@dataclass(frozen=True)
class SampleFile:
    samples: tuple[Sample, ...]
    invalid_lines: int
    sha256: str


def read_samples(path: Path) -> SampleFile:
    """Every valid sample of a file; a torn or invalid line is counted, never
    guessed at (a lost sample is simply not evidence)."""
    data = _read_regular(path, MAX_SAMPLE_FILE_BYTES)
    samples: list[Sample] = []
    invalid = 0
    for line in data.split(b"\n"):
        if not line.strip():
            continue
        try:
            samples.append(Sample.model_validate_json(line))
        except ValidationError:
            invalid += 1
    return SampleFile(tuple(samples), invalid, sha256_hex(data))


# =============================================================================
# per-session metrics (pure)
# =============================================================================


def calendar_session(calendar: SessionCalendar, day: date) -> CalendarSession:
    return CalendarSession(
        session=day, opens_at=calendar.session_open(day), closes_at=calendar.session_close(day)
    )


def previous_close(calendar: SessionCalendar, day: date) -> datetime | None:
    """The close of the calendar session before ``day`` (None: the first)."""
    sessions = calendar.sessions()
    index = bisect.bisect_left(sessions, day)
    return calendar.session_close(sessions[index - 1]) if index else None


def _span(later: datetime, earlier: datetime) -> float:
    return (later - earlier).total_seconds()


def heartbeat_fresh(sample: Sample) -> bool:
    if sample.heartbeat_at is None:
        return False
    return 0 <= _span(sample.at, sample.heartbeat_at) <= HEARTBEAT_STALE_S


def heartbeat_minutes(cal: CalendarSession, samples: Iterable[Sample]) -> int:
    """Regular-hours minutes with at least one sample, every one of them
    seeing a fresh heartbeat."""
    verdicts: dict[int, bool] = {}
    for sample in samples:
        if not cal.opens_at <= sample.at < cal.closes_at:
            continue
        minute = int(_span(sample.at, cal.opens_at)) // 60
        verdicts[minute] = verdicts.get(minute, True) and heartbeat_fresh(sample)
    return sum(1 for fresh in verdicts.values() if fresh)


def _session_points(samples: Iterable[Sample]) -> list[tuple[datetime, str]]:
    """(instant, status): a fresh document at its ``as_of``; a missing,
    stale or future-dated one as ``unavailable`` at the sample's instant."""
    points: set[tuple[datetime, str]] = set()
    for sample in samples:
        rec = sample.reconciliation
        if rec is None:
            points.add((sample.at, "unavailable"))
            continue
        age = _span(sample.at, rec.as_of)
        if age > RECONCILIATION_MAX_AGE_S or age < -FUTURE_SKEW_S:
            points.add((sample.at, "unavailable"))
        else:
            points.add((rec.as_of, rec.status))
    return sorted(points)


@dataclass(frozen=True)
class ReconciliationSummary:
    """None: not observed (unknown, never clean)."""

    open: str | None
    close: str | None
    episodes: tuple[ReconciliationEpisode, ...] | None


def reconciliation_summary(
    cal: CalendarSession, samples: Iterable[Sample]
) -> ReconciliationSummary:
    points = [
        (at, st) for at, st in _session_points(samples) if cal.opens_at <= at <= cal.closes_at
    ]
    if not points:
        return ReconciliationSummary(None, None, None)
    open_end = shift_instant(cal.opens_at, OPEN_CHECK_WINDOW_S)
    close_start = shift_instant(cal.closes_at, -OPEN_CHECK_WINDOW_S)
    opening = next((st for at, st in points if at <= open_end), None)
    closing = next((st for at, st in reversed(points) if at >= close_start), None)
    episodes: list[ReconciliationEpisode] = []
    current: tuple[datetime, str] | None = None

    def episode(resolved_at: datetime | None) -> ReconciliationEpisode:
        assert current is not None
        return ReconciliationEpisode.model_validate(
            {"started_at": current[0], "status": current[1], "resolved_at": resolved_at}
        )

    for at, status in points:
        if status == "clean":
            if current is not None:
                episodes.append(episode(at))
                current = None
        elif current is None:
            current = (at, status)
    if current is not None:
        episodes.append(episode(None))
    return ReconciliationSummary(opening, closing, tuple(episodes))


def _guarded(sample: Sample) -> bool:
    """No exposure, or exposure with a fresh exit owner and no HALT."""
    if sample.exposed is False:
        return True
    return sample.exposed is True and heartbeat_fresh(sample) and sample.halt is False


def _watched(sample: Sample) -> bool:
    """Exposure held, or not known not to be."""
    return sample.exposed is not False


def longest_unguarded_s(samples: Iterable[Sample], start: datetime, end: datetime) -> int | None:
    """The longest unguarded stretch of [start, end] (whole seconds, rounded
    up); None when no sample falls inside."""
    inside = sorted((s for s in samples if start <= s.at <= end), key=lambda s: s.at)
    if not inside:
        return None
    longest = 0.0
    run_start: datetime | None = None

    def stretch(a: datetime, b: datetime, unguarded: bool) -> None:
        nonlocal longest, run_start
        if not unguarded:
            run_start = None
            return
        if run_start is None:
            run_start = a
        longest = max(longest, _span(b, run_start))

    first, last = inside[0], inside[-1]
    stretch(start, first.at, _span(first.at, start) > SAMPLE_GAP_S and _watched(first))
    for a, b in pairwise(inside):
        unobserved = _span(b.at, a.at) > SAMPLE_GAP_S and (_watched(a) or _watched(b))
        stretch(a.at, b.at, not _guarded(a) or unobserved)
    tail = _span(end, last.at) > SAMPLE_GAP_S and _watched(last)
    stretch(last.at, end, not _guarded(last) or tail)
    return math.ceil(longest)


def max_exit_unguarded_s(cal: CalendarSession, samples: Iterable[Sample]) -> int | None:
    return longest_unguarded_s(samples, cal.opens_at, cal.closes_at)


def unowned_orders(events: Iterable[LedgerEvent], since: datetime | None, until: datetime) -> int:
    """Distinct broker orders with an outside fill in (since, until]."""
    perms = {
        event.perm_id
        for event in events
        if event.kind in ("fill", "correction")
        and event.attribution.intent_id is None
        and (since is None or since < event.at)
        and event.at <= until
    }
    return len(perms)


def build_session_record(
    cal: CalendarSession,
    samples: Sequence[Sample],
    *,
    ledger: Sequence[LedgerEvent] | None,
    ledger_reason: str | None,
    previous_close: datetime | None,
    evidence: Sequence[EvidenceRef],
) -> SessionRecord:
    unavailable: dict[SessionMetric, str] = {}
    rec = reconciliation_summary(cal, samples)
    if rec.open is None:
        unavailable["reconciliation_open"] = "reconciliation_not_observed"
    if rec.close is None:
        unavailable["reconciliation_close"] = "reconciliation_not_observed"
    if rec.episodes is None:
        unavailable["reconciliation_episodes"] = "reconciliation_not_observed"
    unowned = None if ledger is None else unowned_orders(ledger, previous_close, cal.closes_at)
    if unowned is None:
        unavailable["unowned_orders"] = ledger_reason or "ledger_unreadable"
    guard = max_exit_unguarded_s(cal, samples)
    if guard is None:
        unavailable["max_exit_unguarded_s"] = "samples_missing"
    return SessionRecord.model_validate(
        {
            "session": cal.session,
            "heartbeat_minutes": heartbeat_minutes(cal, samples),
            "reconciliation_open": rec.open,
            "reconciliation_close": rec.close,
            "reconciliation_episodes": rec.episodes,
            "unowned_orders": unowned,
            "max_exit_unguarded_s": guard,
            "unavailable": unavailable,
            "evidence": tuple(evidence),
        }
    )


# =============================================================================
# lifecycles (pure)
# =============================================================================


@dataclass(frozen=True)
class DeskExit:
    """A desk ``closed`` or ``exit_begin`` event for one structure."""

    structure: str
    event: str
    reason: str
    at: datetime


@dataclass(frozen=True)
class RecPoint:
    """A reconciliation as of ``at``. ``mismatch_con_ids`` None: the
    mismatched contracts are not known (a ledger marker)."""

    at: datetime
    status: str
    mismatch_con_ids: frozenset[int] | None


def log_lines(path: Path) -> Iterator[bytes]:
    """The lines of an append-only log, streamed (never the whole file in
    memory; the desk's ``events.jsonl`` only grows). Absent: no lines."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError(f"{path.name}: not a regular file")
        while line := stream.readline(MAX_LOG_LINE_BYTES + 1):
            if len(line) > MAX_LOG_LINE_BYTES:
                while line and not line.endswith(b"\n"):
                    line = stream.readline(MAX_LOG_LINE_BYTES)
                continue
            yield line


def read_desk_exits(path: Path) -> tuple[list[DeskExit], int]:
    """The ``closed``/``exit_begin`` events of the desk's ``events.jsonl``
    in file order, and the number of such lines that could not be read
    (a line naming neither event is never parsed)."""
    exits: list[DeskExit] = []
    invalid = 0
    for line in log_lines(path):
        if not line.strip():
            continue
        if b'"closed"' not in line and b'"exit_begin"' not in line:
            continue
        try:
            doc = json.loads(line)
        except ValueError:
            invalid += 1
            continue
        if not isinstance(doc, dict):
            invalid += 1
            continue
        if doc.get("event") not in ("closed", "exit_begin"):
            continue
        structure, reason, at = doc.get("structure"), doc.get("reason"), _aware(doc.get("ts"))
        if not isinstance(structure, str) or not isinstance(reason, str) or at is None:
            invalid += 1
            continue
        exits.append(DeskExit(structure, doc["event"], reason, at))
    return exits, invalid


def _ref(event: LedgerEvent) -> str:
    return event.order_ref or ""


def _exit_by(structures: set[str], exits: Sequence[DeskExit]) -> str:
    mine = [x for x in exits if x.structure in structures]
    closes = [x for x in mine if x.event == "closed"]
    begins = [x for x in mine if x.event == "exit_begin"]
    pool = closes or begins
    if not pool:
        return "manual"
    reason = max(pool, key=lambda x: x.at).reason
    if reason == "flatten":
        return "flatten"
    return "policy" if reason in POLICY_EXIT_REASONS else "manual"


def _intent_events(ledger: Iterable[LedgerEvent], intent: str) -> list[LedgerEvent]:
    return sorted(
        (
            e
            for e in ledger
            if e.kind in ("fill", "correction") and e.attribution.intent_id == intent
        ),
        key=lambda e: (e.at, e.event_id),
    )


def build_lifecycle(
    mandate: SupervisedMandateV2,
    *,
    asset_kind: AssetKind,
    ledger: Sequence[LedgerEvent],
    desk_exits: Sequence[DeskExit],
    points: Sequence[RecPoint],
    now: datetime,
    evidence: Sequence[EvidenceRef],
) -> LifecycleRecord:
    """The lifecycle of one approved entry, from the ledger and the desk's
    own exit events (see the module docstring)."""
    intent = mandate.intent_id
    mine = _intent_events(ledger, intent)
    entry: dict[int, int] = {}
    for event in mine:
        if _ref(event).startswith(SUP_PREFIX) and event.con_id is not None:
            entry[event.con_id] = entry.get(event.con_id, 0) + event.quantity_delta
    filled = max((abs(q) for q in entry.values()), default=0)
    first_fill_at = None
    if filled >= 1:
        first_fill_at = min(
            e.at for e in mine if _ref(e).startswith(SUP_PREFIX) and e.quantity_delta
        )

    exit_index: int | None = None
    if filled >= 1:
        held: dict[int, int] = {}
        opened = False
        for index, event in enumerate(mine):
            if event.con_id is not None:
                held[event.con_id] = held.get(event.con_id, 0) + event.quantity_delta
            if any(held.values()):
                opened = True
            elif opened:
                exit_index = index
                break

    exit_by, exited_at, flat_at, reconciled_at = "not_exited", None, None, None
    if exit_index is not None:
        upto = mine[: exit_index + 1]
        exited_at = upto[-1].at
        closing = [e for e in upto if not _ref(e).startswith(SUP_PREFIX)]
        if closing and all(_ref(e).startswith(DESK_PREFIX) for e in closing):
            exit_by = _exit_by({_ref(e).removeprefix(DESK_PREFIX) for e in closing}, desk_exits)
        else:
            exit_by = "manual"
        floor = max(max(e.recorded_at for e in upto), exited_at)
        cons = {e.con_id for e in mine if e.con_id is not None}
        ordered = sorted(points, key=lambda p: p.at)
        for p in ordered:
            if p.at < floor:
                continue
            ours_clear = p.mismatch_con_ids is not None and not (cons & p.mismatch_con_ids)
            if p.status == "clean" or (p.status == "reconciliation_required" and ours_clear):
                flat_at = p.at
                break
        if flat_at is not None:
            reconciled_at = next(
                (p.at for p in ordered if p.at >= flat_at and p.status == "clean"), None
            )

    executed = {e.exec_id for e in mine}
    charged = {e.exec_id for e in ledger if e.kind == "commission" and e.exec_id in executed}
    pending = [e for e in mine if e.exec_id not in charged]
    complete: bool | None = True
    commission_reason = None
    if pending:
        last_day = max(et_date(e.at) for e in pending)
        if et_date(now) > last_day:
            complete = False
        else:
            complete, commission_reason = None, "commission_report_pending"

    return LifecycleRecord.model_validate(
        {
            "intent_id": intent,
            "asset_kind": asset_kind,
            "proposal_id": mandate.proposal_id,
            "approval_key": mandate.approval_id,
            "approved_at": mandate.granted_at,
            "filled_quantity": filled,
            "first_fill_at": first_fill_at,
            "exit_by": exit_by,
            "exited_at": exited_at,
            "broker_flat_at": flat_at,
            "reconciled_at": reconciled_at,
            "commissions_complete": complete,
            "commissions_unavailable_reason": commission_reason,
            "evidence": tuple(evidence),
        }
    )


# =============================================================================
# publish (I/O)
# =============================================================================


@dataclass(frozen=True)
class PublishOutcome:
    document: AcceptanceDocument | None
    sampled: Sample | None
    problems: tuple[str, ...]


@contextmanager
def _publish_lock(adir: AcceptanceDir) -> Iterator[bool]:
    fd = os.open(adir.root / ".publish.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        os.close(fd)


def _write_if_changed(path: Path, data: bytes) -> None:
    try:
        if _read_regular(path, len(data) + 1) == data:
            return
    except (OSError, ValueError):
        pass
    write_atomic(path, data)


def _read_ledger_events(sources: Sources) -> tuple[tuple[LedgerEvent, ...] | None, str | None]:
    try:
        return read_ledger(Wave2RunDir(sources.run_dir).ledger_events()), None
    except (LedgerConflict, OSError, ValueError):
        return None, "ledger_unreadable"


def _days(directory: Path) -> set[date]:
    if not directory.is_dir():
        return set()
    out = set()
    for path in directory.iterdir():
        match = _DAY_FILE.match(path.name)
        if match:
            try:
                out.add(date.fromisoformat(match.group(1)))
            except ValueError:
                continue
    return out


def _sessions(
    adir: AcceptanceDir,
    calendar: SessionCalendar,
    now: datetime,
    ledger: Sequence[LedgerEvent] | None,
    ledger_reason: str | None,
    rebuild: bool,
    problems: list[str],
) -> list[SessionRecord]:
    days = _days(adir.evidence() / SAMPLES_DIR) | _days(adir.root / "sessions")
    records: list[SessionRecord] = []
    for day in sorted(days):
        if not calendar.is_session(day):
            continue
        cal = calendar_session(calendar, day)
        if cal.closes_at > now:
            continue
        stored = adir.session(day)
        if not rebuild and stored.exists():
            try:
                kept = SessionRecord.model_validate_json(_read_regular(stored))
            except (OSError, ValueError, ValidationError):
                kept = None
            if kept is not None and kept.session == day:
                records.append(kept)
                continue
            problems.append(f"session_record_invalid:{day.isoformat()}")
        try:
            found = read_samples(sample_path(adir, day))
        except FileNotFoundError:
            problems.append(f"session_evidence_missing:{day.isoformat()}")
            continue
        except (OSError, ValueError):
            problems.append(f"samples_unreadable:{day.isoformat()}")
            continue
        if found.invalid_lines:
            problems.append(f"samples_invalid_lines:{day.isoformat()}")
        inside = [s for s in found.samples if cal.opens_at <= s.at <= cal.closes_at]
        settled = ledger is not None and now >= shift_instant(cal.closes_at, SESSION_SETTLE_S)
        if settled:
            rel = f"sessions/{day.isoformat()}/samples.jsonl"
            copy = b"".join(model_bytes(s) + b"\n" for s in inside)
            write_atomic(adir.evidence() / rel, copy)
            evidence = (EvidenceRef(path=rel, sha256=sha256_hex(copy)),)
        else:
            rel = f"{SAMPLES_DIR}/{day.isoformat()}.jsonl"
            evidence = (EvidenceRef(path=rel, sha256=found.sha256),)
        record = build_session_record(
            cal,
            inside,
            ledger=ledger,
            ledger_reason=ledger_reason,
            previous_close=previous_close(calendar, day),
            evidence=evidence,
        )
        if settled:
            write_atomic(stored, model_bytes(record))
        records.append(record)
    return records


def _final(record: LifecycleRecord) -> bool:
    """Nothing about this lifecycle can change any more."""
    return record.reconciled_at is not None and record.commissions_complete is not None


def _asset_kind(
    sources: Sources, mandate: SupervisedMandateV2, events: Sequence[LedgerEvent]
) -> AssetKind | None:
    try:
        raw = _read_regular(Wave2RunDir(sources.run_dir).proposal(mandate.proposal_id))
        proposal = EntryProposal.model_validate_json(raw)
    except (OSError, ValueError, ValidationError):
        proposal = None
    if proposal is not None and proposal.effect.intent_id == mandate.intent_id:
        return "equity" if isinstance(proposal.effect, SupervisedEquityEffect) else "option"
    kinds = {e.sec_type for e in events}
    if kinds == {"STK"}:
        return "equity"
    if kinds == {"OPT"}:
        return "option"
    return None


class _Points:
    """Reconciliation points for lifecycles: the ledger's markers plus the
    fresh documents recorder samples saw, read lazily per ET date."""

    def __init__(self, adir: AcceptanceDir, ledger: Sequence[LedgerEvent], now: datetime):
        self.adir = adir
        self.now = now
        self.markers = [
            RecPoint(e.at, e.detail, None)
            for e in ledger
            if e.kind == "reconciliation"
            and e.detail in ("clean", "reconciliation_required", "unavailable")
        ]
        self.by_day: dict[date, list[RecPoint]] = {}

    def day(self, day: date) -> list[RecPoint]:
        if day not in self.by_day:
            points: list[RecPoint] = []
            try:
                samples = read_samples(sample_path(self.adir, day)).samples
            except (OSError, ValueError):
                samples = ()
            for s in samples:
                rec = s.reconciliation
                if rec is None:
                    continue
                age = _span(s.at, rec.as_of)
                if -FUTURE_SKEW_S <= age <= RECONCILIATION_MAX_AGE_S:
                    points.append(RecPoint(rec.as_of, rec.status, frozenset(rec.mismatch_con_ids)))
            self.by_day[day] = points
        return self.by_day[day]

    def days_from(self, first: date) -> list[date]:
        last = et_date(self.now)
        return sorted(d for d in sample_days(self.adir) if first <= d <= last)


def _lifecycle_with_points(
    mandate: SupervisedMandateV2,
    kind: AssetKind,
    ledger: Sequence[LedgerEvent],
    exits: Sequence[DeskExit],
    points: _Points,
    now: datetime,
) -> LifecycleRecord:
    """Build the lifecycle, reading sample days only from the exit onward and
    only until the first clean reconciliation is found (every point not yet
    read is later than every point read)."""

    def build(pts: Sequence[RecPoint]) -> LifecycleRecord:
        return build_lifecycle(
            mandate,
            asset_kind=kind,
            ledger=ledger,
            desk_exits=exits,
            points=pts,
            now=now,
            evidence=(),
        )

    record = build(points.markers)
    if record.exited_at is None:
        return record
    gathered = list(points.markers)
    for day in points.days_from(et_date(record.exited_at)):
        gathered.extend(points.day(day))
        record = build(gathered)
        if record.reconciled_at is not None and et_date(record.reconciled_at) <= day:
            break
    return record


def _lifecycles(
    adir: AcceptanceDir,
    sources: Sources,
    now: datetime,
    ledger: Sequence[LedgerEvent] | None,
    rebuild: bool,
    problems: list[str],
) -> list[LifecycleRecord]:
    stored: dict[str, LifecycleRecord] = {}
    lifecycle_dir = adir.root / "lifecycles"
    if lifecycle_dir.is_dir():
        for path in sorted(lifecycle_dir.glob("*.json")):
            try:
                record = LifecycleRecord.model_validate_json(_read_regular(path))
            except (OSError, ValueError, ValidationError):
                problems.append(f"lifecycle_record_invalid:{path.stem}")
                continue
            stored[record.intent_id] = record
    mandates = Wave2SupervisedDir(sources.supervised_dir).mandates()
    try:
        consumed = sorted(mandates.glob("*.consumed.json")) if mandates.is_dir() else []
    except OSError:
        problems.append("mandates_unreadable")
        return list(stored.values())
    exits: list[DeskExit] | None = None
    points: _Points | None = None
    out: list[LifecycleRecord] = []
    seen: set[str] = set()
    for path in consumed:
        try:
            mandate = SupervisedMandateV2.model_validate_json(_read_regular(path))
        except (OSError, ValueError, ValidationError):
            problems.append(f"mandate_unreadable:{path.name}")
            continue
        intent = mandate.intent_id
        prior = stored.get(intent)
        if path.name != f"{intent}.consumed.json":
            problems.append(f"mandate_name_mismatch:{path.name}")
            continue
        seen.add(intent)
        if prior is not None and not rebuild and _final(prior):
            out.append(prior)
            continue
        if ledger is None:
            if prior is not None:
                out.append(prior)
            continue
        events = _intent_events(ledger, intent)
        kind = _asset_kind(sources, mandate, events)
        if kind is None:
            problems.append(f"lifecycle_kind_unknown:{intent}")
            continue
        if exits is None:
            try:
                exits, invalid = read_desk_exits(sources.run_dir / "events.jsonl")
            except (OSError, ValueError):
                exits, invalid = [], 0
                problems.append("desk_events_unreadable")
            if invalid:
                problems.append("desk_events_invalid_lines")
        if points is None:
            points = _Points(adir, ledger, now)
        try:
            bare = _lifecycle_with_points(mandate, kind, ledger, exits, points, now)
        except ValidationError:
            problems.append(f"lifecycle_invalid:{intent}")
            continue
        executed = {e.exec_id for e in events}
        commissions = [e for e in ledger if e.kind == "commission" and e.exec_id in executed]
        structures = {
            _ref(e).removeprefix(DESK_PREFIX) for e in events if _ref(e).startswith(DESK_PREFIX)
        }
        facts = {
            "schema": FACTS_SCHEMA,
            "mandate": model_document(mandate),
            "ledger_events": [
                model_document(e)
                for e in sorted([*events, *commissions], key=lambda e: (e.at, e.event_id))
            ],
            "desk_exits": [
                {"structure": x.structure, "event": x.event, "reason": x.reason, "at": _z(x.at)}
                for x in exits
                if x.structure in structures
            ],
            "lifecycle": model_document(bare),
        }
        data = json.dumps(facts, sort_keys=True, separators=(",", ":")).encode()
        rel = f"lifecycles/{intent}/facts.json"
        _write_if_changed(adir.evidence() / rel, data)
        record = LifecycleRecord.model_validate(
            {**model_document(bare), "evidence": [{"path": rel, "sha256": sha256_hex(data)}]}
        )
        _write_if_changed(adir.lifecycle(intent), model_bytes(record))
        out.append(record)
    # a recorded lifecycle stays an input even after its mandate file moved
    out.extend(record for intent, record in sorted(stored.items()) if intent not in seen)
    return out


def _z(instant: datetime) -> str:
    return instant.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _drills(adir: AcceptanceDir, problems: list[str]) -> list[DrillRecord]:
    records = []
    for drill_id in DRILL_IDS:
        try:
            raw = _read_regular(adir.drill(drill_id))
        except FileNotFoundError:
            continue
        except (OSError, ValueError):
            problems.append(f"drill_record_invalid:{drill_id}")
            continue
        try:
            record = DrillRecord.model_validate_json(raw)
        except ValidationError:
            problems.append(f"drill_record_invalid:{drill_id}")
            continue
        if record.id != drill_id:
            problems.append(f"drill_record_invalid:{drill_id}")
            continue
        records.append(record)
    return records


def publish(
    adir: AcceptanceDir,
    sources: Sources,
    *,
    owners: Iterable[str],
    calendar: SessionCalendar,
    now: datetime,
    take_sample: bool = True,
    rebuild: bool = False,
) -> PublishOutcome:
    """Sample (inside the window), rebuild the inputs, compute and write
    ``acceptance.json``. Never raises for an evidence problem: problems are
    short codes in the outcome."""
    now = now.astimezone(UTC)
    owner_set = frozenset(owners)
    adir.root.mkdir(parents=True, exist_ok=True)
    problems: list[str] = []
    with _publish_lock(adir) as held:
        if not held:
            return PublishOutcome(None, None, ("publish_busy",))
        sampled = None
        if take_sample and should_sample(calendar, now):
            sampled = _take_sample(sources, now)
            try:
                append_sample(adir, sampled)
            except OSError:
                problems.append("sample_unwritten")
        ledger, ledger_reason = _read_ledger_events(sources)
        if ledger_reason is not None:
            problems.append(ledger_reason)
        sessions = _sessions(adir, calendar, now, ledger, ledger_reason, rebuild, problems)
        lifecycles = _lifecycles(adir, sources, now, ledger, rebuild, problems)
        drills = _drills(adir, problems)
        if not owner_set:
            problems.append("owners_not_configured")
            return PublishOutcome(None, sampled, tuple(problems))
        document = compute_acceptance(
            sessions=sessions,
            lifecycles=lifecycles,
            drills=drills,
            owners=owner_set,
            calendar=calendar,
            computed_at=now,
        )
        write_atomic(adir.document(), model_bytes(document))
        return PublishOutcome(document, sampled, tuple(problems))


# =============================================================================
# CLI
# =============================================================================


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m tree_options.trex.acceptance",
        description="Paper acceptance recorder and compute job (never touches a broker).",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    for name, text in (
        ("publish", "sample (inside the window), compute and write acceptance.json"),
        ("sample", "append one sample (inside the window) and stop"),
        ("show", "print the current document's summary"),
    ):
        cmd = sub.add_parser(name, help=text)
        cmd.add_argument("--acceptance-dir", type=Path, default=None)
        cmd.add_argument("--run-dir", type=Path, default=None)
        cmd.add_argument("--supervised-dir", type=Path, default=None)
        cmd.add_argument("--gateway-state", type=Path, default=None)
        if name == "publish":
            cmd.add_argument("--no-sample", action="store_true", help="compute only")
            cmd.add_argument(
                "--rebuild", action="store_true", help="re-derive every record from evidence"
            )
    return parser


def _summary(document: AcceptanceDocument | None) -> dict[str, Any]:
    if document is None:
        return {
            "computed_at": None,
            "verdict": None,
            "streak_sessions": None,
            "drills_passed": None,
        }
    return {
        "computed_at": _z(document.computed_at),
        "verdict": document.verdict,
        "verdict_reasons": list(document.verdict_reasons),
        "streak_sessions": document.streak.current_sessions,
        "option_lifecycles": document.per_kind.option.counted,
        "equity_lifecycles": document.per_kind.equity.counted,
        "drills_passed": document.drills_passed,
    }


def _emit(payload: Mapping[str, Any]) -> None:
    print(json.dumps({**payload, "live_authorized": False}, sort_keys=True))


def main(
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    now: datetime | None = None,
) -> int:
    args = _parser().parse_args(argv)
    env = os.environ if env is None else env
    defaults = Sources.from_env(env)
    sources = Sources(
        run_dir=args.run_dir or defaults.run_dir,
        supervised_dir=args.supervised_dir or defaults.supervised_dir,
        gateway_state=args.gateway_state or defaults.gateway_state,
    )
    adir = AcceptanceDir(args.acceptance_dir or acceptance_dir_from_env(env))
    instant = (now or datetime.now(UTC)).astimezone(UTC)
    from tree_options.trex.clock import session_calendar

    calendar = session_calendar()
    if args.command == "sample":
        if not should_sample(calendar, instant):
            _emit({"status": "skipped", "reason": "outside_sampling_window"})
            return 0
        path = append_sample(adir, _take_sample(sources, instant))
        _emit({"status": "ok", "sample_file": str(path)})
        return 0
    if args.command == "show":
        try:
            document = AcceptanceDocument.model_validate_json(_read_regular(adir.document()))
        except (OSError, ValueError, ValidationError) as error:
            _emit({"status": "unavailable", "reason": type(error).__name__})
            return 3
        _emit({"status": "ok", **_summary(document)})
        return 0
    try:
        owners = parse_owner_emails(env.get(TREX_OWNER_EMAILS_ENV, ""))
    except ValueError:
        _emit({"status": "refused", "reason": "owners_invalid"})
        return 2
    try:
        outcome = publish(
            adir,
            sources,
            owners=owners,
            calendar=calendar,
            now=instant,
            take_sample=not args.no_sample,
            rebuild=args.rebuild,
        )
    except (OSError, ValueError) as error:
        _emit({"status": "failed", "reason": f"publish_failed:{type(error).__name__}"})
        return 1
    _emit(
        {
            "status": "ok" if not outcome.problems else "degraded",
            "problems": list(outcome.problems),
            "sampled": outcome.sampled is not None,
            **_summary(outcome.document),
        }
    )
    return 0 if not outcome.problems else 3


if __name__ == "__main__":
    sys.exit(main())
