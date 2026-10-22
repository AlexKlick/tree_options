"""``GET /api/today``: ``trex-today/1`` (``TodayView``), the owner's Today page.

Wave 3 lane C (docs/product/WAVE3-CONTRACTS.md C3.2). One read-only page
over files the desk owner and its watches already write. Nothing here
writes a file, contacts a broker, runs ``systemctl``, takes a lock or grants
anything: ``execution_enabled`` is false and ``acceptance.live_authorized``
is the literal false. The page lists what the owner may approve; it never
approves anything itself.

Every source is read on its own and a source that cannot be read makes ITS
block ``unavailable`` with a reason (the C3.2 null rule): never a zero, an
empty list or "clear" standing in for unknown. A block that fails in an
unexpected way is logged (error class only) and shown unavailable; the page
itself always answers.

| Block | Source | Unavailable reasons |
| --- | --- | --- |
| ``session`` | the repo NYSE calendar (``trex.clock.session_calendar``) | ``calendar_unreadable``, ``calendar_exhausted`` |
| ``desk_owner`` | ``book.json``'s heartbeat (what ``trex.exit_watch`` judges, with its ``HEARTBEAT_STALE_S``) and whether a process holds ``runtime.lock`` (``proc_locks_probe``) | ``heartbeat_absent``, ``runtime_lock_unknown`` |
| ``gateway`` | ``gateway.json`` (trex-gateway-watch); a missing, malformed or stale file is never healthy | ``gateway_watch_not_reporting``, ``gateway_watch_stale`` |
| ``controls`` | ``PAUSE_ENTRIES``, ``HALT``, ``FLATTEN`` exactly as the desk sees them (a dangling link stands); ``commands/`` | ``kill_file_unreadable``; ``command_store_*`` |
| ``approvals`` | the ``GET /api/proposals`` reader; ``owner.json``'s epoch | ``proposals_dir_unreadable`` |
| ``positions`` | the ``GET /api/portfolio`` reader (Wave 2 books) | ``holdings_*``, ``risk_snapshot_*`` |
| ``acceptance`` | ``<TREX_ACCEPTANCE_DIR>/acceptance.json``, verified against the calendar | ``acceptance_*``, ``calendar_unreadable`` |
| ``next_events`` | the calendar, pending approvals, the desk's open positions (``book.json`` + ``specs/``), host timers (an injected reader) | ``calendar_unreadable``, ``schedule_unavailable`` |
| ``blockers`` only (Wave 3/4 integration) | ``<run dir>/profile.json`` read by the desk's own ``load_profile``; the injected family ingress status | ``profile_absent``, ``profile_invalid``, ``profile_limits_missing``; ``family_commands_disabled`` |

Details worth knowing:

- **Kill files.** ``since`` is the record's ``at`` (when the stop was
  placed; a re-stamp keeps it), else the file's own time (a bare
  ``touch``, a link); ``reason`` is the recorded reason, else ``""``. A
  missing run dir reads clear, as the desk reads it; any other error is
  unknown.
- **Approvals.** ``pending`` is the Wave 2 proposal reader's status, so
  Today never disagrees with the detail page. A proposal composed under
  another owner epoch than ``owner.json`` names is listed INVALIDATED:
  ``approval_epoch_mismatch`` heads its ``precheck_blockers`` and it gets
  no approval deadline (the desk would refuse it). ``approve_args`` repeat
  the proposal's own bindings verbatim. Prechecks are advisory; the desk
  decides. The Wave 2 reader reads the newest 100 proposal files; a
  pending proposal is at most 15 minutes old, so it is among them unless
  more than 100 were composed in those 15 minutes (the session cap is 3
  entries).
- **The lock probe** reads ``/proc/locks`` and never takes the desk's
  lock (a probe that took it could make a starting desk owner fail its
  non-blocking acquire). Locks of processes outside this process's PID
  namespace are not listed there, and on a filesystem whose ``stat``
  device differs from its superblock (btrfs subvolumes) no line matches:
  both read as "not held" (stopped), the safe direction.
- **Next events** are the trading day's open, equity entry window and
  close (the windows of ``action_graph.canary``), the owner's 16:30 ET
  state backup (``trex.desk_backup``), every approvable proposal's
  deadline, each open desk position's forced exit (equities,
  ``trex.equity_exits``) or time stop (options, the engine's
  ``time_stop_time``), the calendar's end when it is near, and whatever
  the injected timer reader returns. The trex-web unit has no ``AF_UNIX``
  (no session bus), so the default is no timer reader: wire one that reads
  a published timer status file, never ``systemctl``.

Route wiring is the coordinator's (``app.py``): ``attach`` mounts the route.
"""

from __future__ import annotations

import bisect
import errno
import json
import logging
import math
import os
import re
import stat
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any, Final, Literal

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from tree_options.action_graph.canary import (
    EQUITY_ENTRY_CLOSE_CUTOFF_S,
    EQUITY_ENTRY_OPEN_DELAY_S,
)
from tree_options.time.calendar import CalendarError, SessionCalendar
from tree_options.time.sessions import session_close_instant, shift_instant
from tree_options.trex.clock import CALENDAR_HORIZON_WARN_SESSIONS, ET, session_calendar
from tree_options.trex.contracts.acceptance import AcceptanceDocument, verify_acceptance
from tree_options.trex.contracts.canonical import model_document
from tree_options.trex.contracts.commands import ApproveEntryArgs, CommandKind, parse_command
from tree_options.trex.contracts.desk_spec_v2 import EquityDeskSpecV2
from tree_options.trex.contracts.effects import SupervisedEquityEffect
from tree_options.trex.contracts.layout import FLATTEN, HALT, PAUSE_ENTRIES, Wave2RunDir
from tree_options.trex.contracts.proposals import EntryProposal
from tree_options.trex.contracts.vocabulary import Blocker
from tree_options.trex.contracts.wave3 import (
    ACCEPTANCE_DIR_DEFAULT,
    ACCEPTANCE_DIR_ENV,
    CONTROL_LABELS,
    TODAY_PATH,
    AcceptanceDir,
    AcceptanceSummary,
    Controls,
    DeskOwnerHealth,
    GatewayHealth,
    HoldingsSummary,
    HumanBlocker,
    KillSwitchState,
    NextEvents,
    PendingApproval,
    PendingApprovals,
    PositionsSummary,
    QueuedCommand,
    QueuedCommands,
    RiskSummary,
    ScheduledEvent,
    SessionState,
    TodayView,
    Wave3Reason,
    acceptance_dir_from_env,
    acceptance_owners_problem,
)
from tree_options.trex.desk_backup import SCHEDULED_AT as BACKUP_AT_ET
from tree_options.trex.desk_runtime import DEFAULT_ENTRY_WINDOW
from tree_options.trex.desk_spec import load_spec
from tree_options.trex.engine import EngineConfig
from tree_options.trex.equity_exits import FORCED_EXIT_CLOSE_BUFFER_S
from tree_options.trex.exit_watch import EXPOSED, HEARTBEAT_STALE_S
from tree_options.trex_web.acceptance_view import FUTURE_SKEW_S as ACCEPTANCE_FUTURE_SKEW_S
from tree_options.trex_web.command_store import (
    MAX_PENDING_SCAN,
    CommandStore,
    CommandStoreUnavailable,
)
from tree_options.trex_web.commands_view import proposals_payload
from tree_options.trex_web.family_authority import IngressProblem
from tree_options.trex_web.gateway_view import DEFAULT_GATEWAY_STATE, WATCH_STALE_AFTER_S
from tree_options.trex_web.portfolio_view import portfolio_payload

__all__ = [
    "ACCEPTANCE_DIR_ENV",
    "CALENDAR_HORIZON_WARN_SESSIONS",
    "DEFAULT_ACCEPTANCE_DIR",
    "PROC_LOCKS",
    "TODAY_PATH",
    "LockProbe",
    "LockStateUnknown",
    "TimerReader",
    "attach",
    "positions_summary",
    "proc_locks_probe",
    "today_view",
]

log = logging.getLogger("trex_web.today")

#: ``TREX_ACCEPTANCE_DIR`` (C3.7, ``ACCEPTANCE_DIR_ENV``), read per request
#: when no dir is pinned (the shared resolver, ``wave3.acceptance_dir_from_env``).
DEFAULT_ACCEPTANCE_DIR: Final = Path(ACCEPTANCE_DIR_DEFAULT)
#: The kernel's table of held file locks (read, never written).
PROC_LOCKS: Final = Path("/proc/locks")
#: ``DeskPaths.lock()``, ``.book()``, ``.specs()`` and the owner file, by name.
RUNTIME_LOCK: Final = "runtime.lock"
BOOK: Final = "book.json"
OWNER_FILE: Final = "owner.json"
SPECS: Final = "specs"
#: ``SupervisedDesk.profile_path()``: the operator's capital profile, by name.
PROFILE_FILE: Final = "profile.json"
#: No state file the page reads is anywhere near this; a bigger one is not ours.
MAX_STATE_BYTES: Final = 1024 * 1024
MAX_ACCEPTANCE_BYTES: Final = 32 * 1024 * 1024
MAX_LOCK_TABLE_BYTES: Final = 8 * 1024 * 1024
#: The kill-switch kinds a queued command may carry (``queued`` lists only these).
KILL_SWITCH_KINDS: Final[frozenset[CommandKind]] = frozenset(
    {"pause_entries", "resume_entries", "halt", "flatten", "resume_all"}
)
#: The gateway watch's own statuses (``trex.gateway_watch.classify``).
GATEWAY_STATUSES: Final = frozenset(
    {"ok", "starting", "checking", "needs_login", "needs_2fa", "api_down", "down"}
)
#: The desk runtime's option time stop (``EngineConfig`` as ``DeskRuntime`` builds it).
TIME_STOP_ET: Final = EngineConfig(DEFAULT_ENTRY_WINDOW).time_stop_time
#: A queued command's file name: ``<key>.json`` (queued) or ``<key>.claimed.json``.
_QUEUED_NAME = re.compile(
    r"^([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(\.claimed)?\.json$"
)
_SEVERITY_ORDER: Final = {"critical": 0, "warning": 1, "info": 2}
NO_STORE: Final = {"Cache-Control": "no-store"}

Reason = Blocker | Wave3Reason
#: ``(runtime.lock path) -> held``; raises ``LockStateUnknown`` when it cannot tell.
LockProbe = Callable[[Path], bool]
#: Host timer events (e.g. ``premarket_run``); raising makes the schedule unknown.
TimerReader = Callable[[], Sequence[ScheduledEvent]]
CalendarSource = Callable[[], SessionCalendar]
PathSource = Callable[[], Path]
#: Why family command ingress is disabled (empty: enabled); the coordinator
#: wires ``family_authority.family_ingress_status(run_dir=...).problems``
#: (the configuration and the replay ledger's writability).
FamilyIngress = Callable[[], Sequence[str]]


class LockStateUnknown(RuntimeError):
    """Whether the desk owner holds its runtime lock cannot be told."""


# -- small readers -----------------------------------------------------------------


def _read_bytes(path: Path, limit: int) -> bytes:
    """A regular file's bytes, never through a symlink, at most ``limit``.
    Raises ``FileNotFoundError`` when absent and ``OSError`` otherwise."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise OSError(errno.EINVAL, "not a regular file", str(path))
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise OSError(errno.EFBIG, "larger than any state file", str(path))
    return data


def _json_object(raw: bytes) -> dict[str, Any]:
    try:
        document = json.loads(raw)
    except RecursionError as error:  # nested too deeply: malformed, never a crash
        raise ValueError("nested too deeply") from error
    if not isinstance(document, dict):
        raise ValueError("not a JSON object")
    return document


def _read_object(path: Path, limit: int = MAX_STATE_BYTES) -> dict[str, Any]:
    """The JSON object at ``path`` (``OSError`` / ``ValueError`` otherwise)."""
    return _json_object(_read_bytes(path, limit))


def _instant(value: object) -> datetime | None:
    """A timezone-aware ISO instant as UTC; None for anything else."""
    if not isinstance(value, str):
        return None
    try:
        instant = datetime.fromisoformat(value)
    except ValueError:
        return None
    if instant.tzinfo is None or instant.utcoffset() is None:
        return None
    return instant.astimezone(UTC)


def _age_s(earlier: datetime, now: datetime) -> int:
    return int((now - earlier).total_seconds())


def _reason(text: object, fallback: Reason) -> Reason:
    """``text`` as a contract reason (Wave 2 ``Blocker`` or ``Wave3Reason``)."""
    if isinstance(text, str):
        try:
            return Blocker(text)
        except ValueError:
            pass
        try:
            return Wave3Reason(text)
        except ValueError:
            pass
    return fallback


def _cents(value: int) -> str:
    return f"{value // 100}.{value % 100:02d}"


def _when(at: datetime, now: datetime) -> str:
    """``10:10 ET`` today, ``Fri Oct 23, 09:30 ET`` on another ET day."""
    local = at.astimezone(ET)
    clock = local.strftime("%H:%M")
    if local.date() == now.astimezone(ET).date():
        return f"{clock} ET"
    return f"{local.strftime('%a %b %d')}, {clock} ET"


def _guard[T](name: str, build: Callable[[], T], fallback: Callable[[], T]) -> T:
    """``build()``; an unexpected failure is logged (class only) and degrades
    to ``fallback()`` (an unavailable block): the page never answers 500."""
    try:
        return build()
    except Exception as error:
        log.error("today: the %s block failed (%s); shown unavailable", name, type(error).__name__)
        return fallback()


# -- session ---------------------------------------------------------------------------


def _session_unavailable(reason: Reason) -> SessionState:
    return SessionState(
        status="unavailable",
        unavailable_reason=reason,
        et_date=None,
        session=None,
        opens_at=None,
        closes_at=None,
        early_close=None,
        calendar_sessions_left=None,
    )


def _session(cal: SessionCalendar, now: datetime) -> SessionState:
    """Today's session while it has not closed, else the next one."""
    et_date = now.astimezone(ET).date()
    sessions = cal.sessions()
    after = bisect.bisect_right(sessions, et_date)
    status: Literal["pre_open", "open", "after_close", "no_session"]
    if cal.is_session(et_date) and now < cal.session_close(et_date):
        session = et_date
        status = "pre_open" if now < cal.session_open(et_date) else "open"
    elif after < len(sessions):
        session = sessions[after]
        status = "after_close" if cal.is_session(et_date) else "no_session"
    else:
        return _session_unavailable(Wave3Reason.CALENDAR_EXHAUSTED)
    closes = cal.session_close(session)
    return SessionState(
        status=status,
        unavailable_reason=None,
        et_date=et_date,
        session=session,
        opens_at=cal.session_open(session),
        closes_at=closes,
        early_close=closes != session_close_instant(session),
        calendar_sessions_left=len(sessions) - after,
    )


# -- desk owner ------------------------------------------------------------------------


def proc_locks_probe(path: Path, *, table: Path = PROC_LOCKS) -> bool:
    """True when some process holds an exclusive ``flock`` on ``path``.

    Reads the kernel's lock table and never takes the lock itself. A missing
    file is not held; an unreadable file or table raises ``LockStateUnknown``.
    A table line is ``<n>: FLOCK  ADVISORY  WRITE <pid> <maj>:<min>:<inode>
    <start> <end>``; a blocked waiter's line carries ``->`` and is not a
    holder."""
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return False
    except OSError as error:
        raise LockStateUnknown(f"lock file unreadable: {type(error).__name__}") from error
    key = f"{os.major(st.st_dev):02x}:{os.minor(st.st_dev):02x}:{st.st_ino}"
    try:
        text = _read_bytes(table, MAX_LOCK_TABLE_BYTES).decode("ascii", "replace")
    except OSError as error:
        raise LockStateUnknown(f"lock table unreadable: {type(error).__name__}") from error
    for line in text.splitlines():
        fields = line.split()
        if len(fields) >= 6 and fields[1:4] == ["FLOCK", "ADVISORY", "WRITE"]:
            if fields[5] == key:
                return True
    return False


def _owner_unavailable(reason: Reason) -> DeskOwnerHealth:
    return DeskOwnerHealth(
        status="unavailable",
        unavailable_reason=reason,
        heartbeat_at=None,
        heartbeat_age_s=None,
        lock_held=None,
        exit_owner_ready=None,
        stale_after_s=HEARTBEAT_STALE_S,
    )


def _desk_owner(run_dir: Path, now: datetime, probe: LockProbe) -> DeskOwnerHealth:
    """Healthy: lock held and heartbeat at most ``HEARTBEAT_STALE_S`` old."""
    try:
        beat = _instant(_read_object(run_dir / BOOK).get("heartbeat"))
    except (OSError, ValueError):
        beat = None
    if beat is None:
        return _owner_unavailable(Wave3Reason.HEARTBEAT_ABSENT)
    age = _age_s(beat, now)
    if age < 0:  # a beat from the future is not evidence of a live owner
        return _owner_unavailable(Wave3Reason.HEARTBEAT_ABSENT)
    try:
        held = probe(run_dir / RUNTIME_LOCK)
    except (LockStateUnknown, OSError):
        return _owner_unavailable(Wave3Reason.RUNTIME_LOCK_UNKNOWN)
    if not isinstance(held, bool):
        return _owner_unavailable(Wave3Reason.RUNTIME_LOCK_UNKNOWN)
    status: Literal["healthy", "stale", "stopped"] = (
        "stopped" if not held else "healthy" if age <= HEARTBEAT_STALE_S else "stale"
    )
    return DeskOwnerHealth(
        status=status,
        unavailable_reason=None,
        heartbeat_at=beat,
        heartbeat_age_s=age,
        lock_held=held,
        exit_owner_ready=status == "healthy",
        stale_after_s=HEARTBEAT_STALE_S,
    )


def _owner_epoch(run_dir: Path) -> str | None:
    """The live owner epoch ``owner.json`` names; None when it cannot be read."""
    try:
        epoch = _read_object(run_dir / OWNER_FILE).get("owner_epoch")
    except (OSError, ValueError):
        return None
    return epoch if isinstance(epoch, str) and epoch.strip() else None


def _profile_problem(run_dir: Path) -> Blocker | None:
    """Why the desk would refuse every entry for its capital profile, read
    exactly as the desk reads it (``supervised_desk.load_profile`` on
    ``<run dir>/profile.json``): ``profile_absent``, ``profile_invalid`` or
    ``profile_limits_missing`` (``CapitalProfile.complete_for_review``);
    None when it is usable. Wave 3/4 integration: onboarding reads these
    blockers (an unreadable run dir is unknown, so it names nothing)."""
    from tree_options.trex.supervised_desk import load_profile  # lazy: a heavy module

    path = run_dir / PROFILE_FILE
    try:
        profile, _digest = load_profile(path)
    except FileNotFoundError:
        return Blocker.PROFILE_ABSENT
    except (NotADirectoryError, PermissionError):
        return None
    except (OSError, ValueError, KeyError, TypeError, ArithmeticError):
        return Blocker.PROFILE_INVALID
    if not profile.complete_for_review:
        return Blocker.PROFILE_LIMITS_MISSING
    return None


def _family_problems(source: FamilyIngress | None) -> tuple[str, ...] | None:
    """Why family commands are refused (empty: enabled); None: not wired."""
    if source is None:
        return None
    return tuple(source())


# -- gateway -----------------------------------------------------------------------------


def _gateway_unavailable(reason: Reason) -> GatewayHealth:
    return GatewayHealth(
        status="unavailable", unavailable_reason=reason, detail=None, checked_at=None, age_s=None
    )


def _gateway(path: Path, now: datetime) -> GatewayHealth:
    try:
        document = _read_object(path)
    except (OSError, ValueError):
        return _gateway_unavailable(Wave3Reason.GATEWAY_WATCH_NOT_REPORTING)
    status = document.get("status")
    detail = document.get("detail")
    checked = document.get("checked_at")
    if (
        status not in GATEWAY_STATUSES
        or not isinstance(detail, str)
        or isinstance(checked, bool)
        or not isinstance(checked, int | float)
        or not math.isfinite(checked)
    ):
        return _gateway_unavailable(Wave3Reason.GATEWAY_WATCH_NOT_REPORTING)
    try:
        checked_at = datetime.fromtimestamp(checked, UTC)
    except (OverflowError, OSError, ValueError):
        return _gateway_unavailable(Wave3Reason.GATEWAY_WATCH_NOT_REPORTING)
    age = int(now.timestamp() - checked)
    if age < 0 or age > WATCH_STALE_AFTER_S:
        return _gateway_unavailable(Wave3Reason.GATEWAY_WATCH_STALE)
    return GatewayHealth(
        status=status, unavailable_reason=None, detail=detail, checked_at=checked_at, age_s=age
    )


# -- controls ------------------------------------------------------------------------------

KillName = Literal["PAUSE_ENTRIES", "HALT", "FLATTEN"]


def _kill_switch(run_dir: Path, name: KillName) -> KillSwitchState:
    """One kill file as the desk sees it (``lexists``: a dangling link stands)."""
    path = run_dir / name
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return KillSwitchState(
            name=name, status="clear", unavailable_reason=None, since=None, reason=None
        )
    except OSError:
        return KillSwitchState(
            name=name,
            status="unavailable",
            unavailable_reason=Wave3Reason.KILL_FILE_UNREADABLE,
            since=None,
            reason=None,
        )
    try:
        record: dict[str, Any] | None = _read_object(path)
    except (OSError, ValueError):
        record = None  # a bare touch, a link, a foreign file: it still stands
    since = _instant(record.get("at")) if record is not None else None
    if since is None:
        since = datetime.fromtimestamp(st.st_mtime, UTC)
    text = record.get("reason") if record is not None else None
    return KillSwitchState(
        name=name,
        status="standing",
        unavailable_reason=None,
        since=since,
        reason=text[:500] if isinstance(text, str) else "",
    )


def _queued_unavailable(reason: Reason) -> QueuedCommands:
    return QueuedCommands(status="unavailable", unavailable_reason=reason, items=None)


def _queued(run_dir: Path) -> QueuedCommands:
    """Kill-switch commands accepted and not yet applied: queued
    (``<key>.json``) or claimed by the owner and not yet answered."""
    folder = Wave2RunDir(run_dir).commands()
    try:
        names = sorted(os.listdir(folder))
    except FileNotFoundError:
        return QueuedCommands(status="ok", unavailable_reason=None, items=())
    except OSError:
        return _queued_unavailable(Blocker.COMMAND_STORE_UNREADABLE)
    found = [match for name in names if (match := _QUEUED_NAME.fullmatch(name))]
    if len(found) > MAX_PENDING_SCAN:
        return _queued_unavailable(Blocker.COMMAND_STORE_UNAVAILABLE)
    store = CommandStore(run_dir)
    items: list[QueuedCommand] = []
    for match in found:
        key, claimed = match.group(1), match.group(2) is not None
        try:
            command = parse_command(
                _json_object(_read_bytes(folder / match.group(0), MAX_STATE_BYTES))
            )
        except FileNotFoundError:
            continue  # claimed or archived by the owner while we listed
        except OSError:
            return _queued_unavailable(Blocker.COMMAND_STORE_UNREADABLE)
        except ValueError:  # pydantic's ValidationError included
            return _queued_unavailable(Blocker.COMMAND_STORE_UNAVAILABLE)
        if command.idempotency_key != key:
            return _queued_unavailable(Blocker.COMMAND_STORE_FOREIGN_FILE)
        if command.kind not in KILL_SWITCH_KINDS:
            continue
        if claimed:
            try:
                result = store.read_result(key)
            except CommandStoreUnavailable as error:
                return _queued_unavailable(_reason(error.reason, Blocker.COMMAND_STORE_UNAVAILABLE))
            if result is not None and result.status != "accepted":
                continue  # answered: the claim only waits for its archive move
        items.append(
            QueuedCommand(
                idempotency_key=command.idempotency_key,
                kind=command.kind,
                issued_at=command.issued_at,
                expires_at=command.expires_at,
                issued_by_sub=command.issued_by.sub,
            )
        )
    items.sort(key=lambda item: (item.issued_at, item.idempotency_key))
    return QueuedCommands(status="ok", unavailable_reason=None, items=tuple(items))


# -- approvals -----------------------------------------------------------------------------


@dataclass(frozen=True)
class _Due:
    """An approvable proposal's deadline, for ``next_events``."""

    at: datetime
    proposal_id: str
    what: str


def _approvals_unavailable(reason: Reason) -> PendingApprovals:
    return PendingApprovals(
        status="unavailable", unavailable_reason=reason, items=None, problem_count=None
    )


def _summary(proposal: EntryProposal) -> str:
    effect = proposal.effect
    if isinstance(effect, SupervisedEquityEffect):
        policy = effect.exit_policy
        target = policy.target_price_cents
        upside = f"target {_cents(target)}" if target is not None else "no target"
        text = (
            f"Buy {effect.quantity} {effect.symbol} at a {_cents(effect.limit_cents)} limit; "
            f"stop {_cents(policy.stop_price_cents)}, {upside}"
        )
    else:
        structure = effect.structure
        legs = "/".join(f"{leg.strike}{leg.right}" for leg in structure.legs)
        text = (
            f"{effect.side.capitalize()} {effect.quantity} {structure.underlying} "
            f"{structure.kind.replace('_', ' ')} {legs} at a {effect.limit} limit; "
            f"time stop {structure.exit_deadline.isoformat()}"
        )
    return text[:500]


def _subject(proposal: EntryProposal) -> str:
    effect = proposal.effect
    if isinstance(effect, SupervisedEquityEffect):
        return effect.symbol
    return effect.structure.underlying


@dataclass(frozen=True)
class _Prechecks:
    """What the page knows that the desk will check again at approval."""

    epoch: str | None
    controls: Controls
    desk_ready: bool
    entry_blockers: tuple[Reason, ...]
    equity_window: tuple[datetime, datetime] | None
    calendar_known: bool

    def of(self, proposal: EntryProposal, now: datetime) -> tuple[Reason, ...]:
        out: list[Reason] = []
        if self.epoch is not None and proposal.review.owner_epoch != self.epoch:
            out.append(Blocker.APPROVAL_EPOCH_MISMATCH)
        controls = self.controls
        if controls.halt.status == "standing":
            out.append(Blocker.HALT)
        if controls.flatten.status == "standing":
            out.append(Blocker.FLATTEN)
        if controls.pause_entries.status == "standing":
            out.append(Blocker.ENTRIES_PAUSED)
        kills = (controls.pause_entries, controls.halt, controls.flatten)
        if any(kill.status == "unavailable" for kill in kills):
            out.append(Wave3Reason.KILL_FILE_UNREADABLE)
        if not self.desk_ready:
            out.append(Blocker.EXIT_OWNER_NOT_READY)
        if isinstance(proposal.effect, SupervisedEquityEffect):
            if not self.calendar_known:
                out.append(Wave3Reason.CALENDAR_UNREADABLE)
            elif self.equity_window is None or not (
                self.equity_window[0] <= now <= self.equity_window[1]
            ):
                out.append(Blocker.ENTRY_WINDOW_CLOSED)
            if proposal.review.quote.market_data_type != "realtime":
                out.append(Blocker.EQUITY_QUOTE_NOT_REALTIME)
        out.extend(self.entry_blockers)
        return tuple(dict.fromkeys(out))


def _approvals(
    run_dir: Path, supervised_dir: Path, now: datetime, checks: _Prechecks
) -> tuple[PendingApprovals, tuple[_Due, ...]]:
    """Pending proposals with their prechecks, and the approvable deadlines."""
    try:
        with os.scandir(Wave2RunDir(run_dir).proposals()):
            pass
    except FileNotFoundError:
        pass  # nothing composed yet: the Wave 2 reader lists none
    except OSError:
        return _approvals_unavailable(Wave3Reason.PROPOSALS_DIR_UNREADABLE), ()
    listing = proposals_payload(run_dir, supervised_dir, now)
    problems = len(listing["problems"])
    items: list[PendingApproval] = []
    due: list[_Due] = []
    for row in listing["proposals"]:
        if row["status"] != "pending":
            continue
        try:
            proposal = EntryProposal.model_validate(row["proposal"])
            blockers = checks.of(proposal, now)
            item = PendingApproval(
                proposal_id=proposal.proposal_id,
                proposal_sha256=row["proposal_sha256"],
                asset_kind=row["asset_kind"],
                intent_id=proposal.effect.intent_id,
                program_id=proposal.program_id,
                summary=_summary(proposal),
                proposed_at=proposal.proposed_at,
                send_deadline=proposal.send_deadline,
                seconds_to_deadline=max(0, _age_s(now, proposal.send_deadline)),
                max_loss_cents=proposal.review.max_loss_cents,
                notional_cents=proposal.review.notional_cents,
                approve_args=ApproveEntryArgs(
                    proposal_id=proposal.proposal_id,
                    proposal_sha256=row["proposal_sha256"],
                    effect_sha256=proposal.effect_sha256,
                    owner_epoch=proposal.review.owner_epoch,
                    profile_digest=proposal.review.profile_digest,
                ),
                precheck_blockers=blockers,
            )
        except ValueError:  # a row the Today contract cannot carry is a problem
            problems += 1
            continue
        items.append(item)
        if Blocker.APPROVAL_EPOCH_MISMATCH not in blockers:
            due.append(_Due(proposal.send_deadline, proposal.proposal_id, _subject(proposal)))
    items.sort(key=lambda item: (item.send_deadline, item.proposal_id))
    block = PendingApprovals(
        status="ok", unavailable_reason=None, items=tuple(items), problem_count=problems
    )
    return block, tuple(due)


# -- positions -----------------------------------------------------------------------------


def _holdings_unavailable(reason: Reason) -> HoldingsSummary:
    return HoldingsSummary(
        status="unavailable",
        unavailable_reason=reason,
        as_of=None,
        age_s=None,
        option_positions=None,
        equity_positions=None,
    )


def _risk_unavailable(reason: Reason) -> RiskSummary:
    return RiskSummary(
        status="unavailable",
        unavailable_reason=reason,
        as_of=None,
        age_s=None,
        open_loss_cents=None,
        day_realized_loss_cents=None,
        settled_cash_cents=None,
        outside_positions=None,
    )


#: Wave 2 view reasons outside the Today vocabulary, by their nearest member.
_HOLDINGS_REASONS: Final[Mapping[str, Reason]] = {
    "holdings_account_mismatch": Blocker.ACCOUNT_MISMATCH,
}


def _holdings_summary(block: Mapping[str, Any]) -> HoldingsSummary:
    if block.get("status") != "ok":
        raw = block.get("reason")
        mapped = _HOLDINGS_REASONS.get(raw) if isinstance(raw, str) else None
        return _holdings_unavailable(mapped or _reason(raw, Blocker.HOLDINGS_INVALID))
    age = block["age_s"]
    if not isinstance(age, int) or age < 0:  # from the future: not a projection we trust
        return _holdings_unavailable(Blocker.HOLDINGS_INVALID)
    intents: dict[str, set[str]] = {"OPT": set(), "STK": set()}
    for row in block["rows"]:
        for lot in row["lots"]:
            intent = lot["attribution"]["intent_id"]
            if intent is not None:
                intents[row["sec_type"]].add(intent)
    return HoldingsSummary(
        status="ok",
        unavailable_reason=None,
        as_of=block["as_of"],
        age_s=age,
        option_positions=len(intents["OPT"]),
        equity_positions=len(intents["STK"]),
    )


def _risk_summary(portfolio: Mapping[str, Any]) -> RiskSummary:
    risk = portfolio["risk"]
    status = risk.get("status")
    if status not in ("fresh", "stale"):
        return _risk_unavailable(_reason(risk.get("reason"), Blocker.RISK_SNAPSHOT_UNREADABLE))
    age = risk["age_s"]
    if not isinstance(age, int) or age < 0:  # future-dated: unusable, age unknowable
        return _risk_unavailable(Blocker.RISK_SNAPSHOT_STALE)
    rows = portfolio["outside_positions"]["rows"]
    figures = (
        risk["open_loss_cents"],
        risk["day_realized_loss_cents"],
        portfolio["cash"]["settled_cash_cents"],
        rows,
    )
    if any(figure is None for figure in figures):
        return _risk_unavailable(Blocker.RISK_SNAPSHOT_INVALID)
    return RiskSummary(
        status=status,
        unavailable_reason=None,
        as_of=risk["as_of"],
        age_s=age,
        open_loss_cents=risk["open_loss_cents"],
        day_realized_loss_cents=risk["day_realized_loss_cents"],
        settled_cash_cents=portfolio["cash"]["settled_cash_cents"],
        outside_positions=len(rows),
    )


def positions_summary(portfolio: Mapping[str, Any]) -> PositionsSummary:
    """Today's ``positions`` from a ``trex-portfolio/1`` document (the Wave 2
    ``GET /api/portfolio`` body): desk positions are the distinct ATTRIBUTED
    intents per asset kind; unknown is unavailable, never zero."""
    return PositionsSummary(
        holdings=_guard(
            "holdings",
            lambda: _holdings_summary(portfolio["holdings"]),
            lambda: _holdings_unavailable(Blocker.HOLDINGS_UNREADABLE),
        ),
        risk=_guard(
            "risk",
            lambda: _risk_summary(portfolio),
            lambda: _risk_unavailable(Blocker.RISK_SNAPSHOT_UNREADABLE),
        ),
    )


# -- acceptance ----------------------------------------------------------------------------


def _acceptance_unavailable(reason: Reason) -> AcceptanceSummary:
    return AcceptanceSummary(
        status="unavailable",
        unavailable_reason=reason,
        computed_at=None,
        verdict=None,
        streak_sessions=None,
        option_lifecycles=None,
        equity_lifecycles=None,
        drills_passed=None,
        streak_required=10,
        lifecycles_required=10,
        drills_required=7,
        live_authorized=False,
    )


def _acceptance(directory: Path, cal: SessionCalendar | None, now: datetime) -> AcceptanceSummary:
    """The C3.4 summary of a document verified against the repo calendar,
    with ``GET /api/acceptance``'s two further checks (Wave 3/4 integration):
    never dated more than ``ACCEPTANCE_FUTURE_SKEW_S`` after ``now``, and
    naming exactly the configured ``TREX_OWNER_EMAILS`` when set."""
    try:
        raw = _read_bytes(AcceptanceDir(directory).document(), MAX_ACCEPTANCE_BYTES)
    except FileNotFoundError:
        return _acceptance_unavailable(Wave3Reason.ACCEPTANCE_MISSING)
    except OSError:
        return _acceptance_unavailable(Wave3Reason.ACCEPTANCE_INVALID)
    try:
        document = AcceptanceDocument.model_validate_json(raw)
    except ValueError:  # pydantic's ValidationError included
        return _acceptance_unavailable(Wave3Reason.ACCEPTANCE_INVALID)
    if document.computed_at > shift_instant(now, ACCEPTANCE_FUTURE_SKEW_S):
        return _acceptance_unavailable(Wave3Reason.ACCEPTANCE_INVALID)
    owners = acceptance_owners_problem(document.owners, os.environ)
    if owners is not None:
        return _acceptance_unavailable(owners)
    if cal is None:
        return _acceptance_unavailable(Wave3Reason.CALENDAR_UNREADABLE)
    try:
        verify_acceptance(document, cal)
    except CalendarError:
        return _acceptance_unavailable(Wave3Reason.CALENDAR_UNREADABLE)
    except ValueError as error:
        mismatch = str(error) == Wave3Reason.ACCEPTANCE_CALENDAR_MISMATCH.value
        return _acceptance_unavailable(
            Wave3Reason.ACCEPTANCE_CALENDAR_MISMATCH
            if mismatch
            else Wave3Reason.ACCEPTANCE_WINDOW_MISMATCH
        )
    return AcceptanceSummary.of(document)


# -- next events ---------------------------------------------------------------------------


def _event(at: datetime, kind: Any, label: str, subject: str | None = None) -> ScheduledEvent:
    return ScheduledEvent(at=at, kind=kind, label=label[:500], subject=subject)


def _equity_window(cal: SessionCalendar, day: date) -> tuple[datetime, datetime] | None:
    """``action_graph.canary``'s equity entry window on ``day`` (None: no session)."""
    if not cal.is_session(day):
        return None
    return (
        shift_instant(cal.session_open(day), EQUITY_ENTRY_OPEN_DELAY_S),
        shift_instant(cal.session_close(day), -EQUITY_ENTRY_CLOSE_CUTOFF_S),
    )


def _today_window(cal: SessionCalendar | None, now: datetime) -> tuple[datetime, datetime] | None:
    return None if cal is None else _equity_window(cal, now.astimezone(ET).date())


def _at_et(day: date, wall: time) -> datetime:
    return datetime.combine(day, wall, tzinfo=ET).astimezone(UTC)


def _forced_exit(cal: SessionCalendar, deadline: date, forced_exit_et: str) -> datetime:
    """``trex.equity_exits``: the last session on or before the deadline, at
    ``forced_exit_et`` or 15 minutes before an earlier close."""
    sessions = cal.sessions()
    index = bisect.bisect_right(sessions, deadline)
    if index == 0:
        raise ValueError(f"no session on or before {deadline}")
    session = sessions[index - 1]
    hours, minutes = (int(part) for part in forced_exit_et.split(":"))
    buffered = shift_instant(cal.session_close(session), -FORCED_EXIT_CLOSE_BUFFER_S)
    return min(_at_et(session, time(hours, minutes)), buffered)


def _time_stop(cal: SessionCalendar, deadline: date) -> datetime | None:
    """The option engine's time stop: ``TIME_STOP_ET`` on the deadline
    session, or the next session's open when the deadline is no session."""
    if cal.is_session(deadline):
        return _at_et(deadline, TIME_STOP_ET)
    sessions = cal.sessions()
    index = bisect.bisect_right(sessions, deadline)
    return cal.session_open(sessions[index]) if index < len(sessions) else None


def _desk_exits(run_dir: Path, cal: SessionCalendar, now: datetime) -> list[ScheduledEvent]:
    """The scheduled exits of every structure the desk book holds open.
    A missing book holds nothing; an unreadable book or spec raises (the
    schedule is then unknown, never silently empty)."""
    try:
        book = _read_object(run_dir / BOOK)
    except FileNotFoundError:
        return []
    structures = book.get("structures")
    if not isinstance(structures, dict):
        raise ValueError("book.json has no structures map")
    out: list[ScheduledEvent] = []
    for sid, state in sorted(structures.items()):
        if not isinstance(state, dict):
            raise ValueError("a book structure is not an object")
        if state.get("status") not in EXPOSED:
            continue
        if not sid or sid in (".", "..") or "/" in sid or "\x00" in sid:
            raise ValueError("a book structure id is not a file name")
        spec = load_spec(_read_object(run_dir / SPECS / f"{sid}.json"))
        if isinstance(spec, EquityDeskSpecV2):
            policy = spec.position.exit_policy
            at = _forced_exit(cal, policy.exit_deadline_session, policy.forced_exit_et)
            label = f"Forced exit of {spec.position.symbol} ({_when(at, now)})"
            out.append(_event(at, "forced_exit", label, spec.intent_id))
            continue
        structure = spec.structure
        stop_at = _time_stop(cal, structure.exit_deadline)
        if stop_at is not None:
            label = f"Time-stop exit of {structure.underlying} ({_when(stop_at, now)})"
            out.append(_event(stop_at, "exit_deadline", label, spec.intent_id))
    return out


def _events_unavailable(reason: Reason) -> NextEvents:
    return NextEvents(status="unavailable", unavailable_reason=reason, items=None)


def _next_events(
    *,
    now: datetime,
    cal: SessionCalendar | None,
    session: SessionState,
    due: Sequence[_Due],
    run_dir: Path,
    timers: TimerReader | None,
) -> NextEvents:
    if cal is None:
        return _events_unavailable(Wave3Reason.CALENDAR_UNREADABLE)
    events: list[ScheduledEvent] = []
    et_date = now.astimezone(ET).date()
    day = session.session
    if session.status != "unavailable" and day is not None:
        opens, closes = cal.session_open(day), cal.session_close(day)
        events.append(_event(opens, "session_open", f"Market opens ({_when(opens, now)})"))
        window = _equity_window(cal, day)
        if window is not None:
            events.append(
                _event(
                    window[0],
                    "entry_window_open",
                    f"Equity entry window opens ({_when(window[0], now)})",
                )
            )
            events.append(
                _event(
                    window[1],
                    "entry_window_close",
                    f"Equity entry window closes ({_when(window[1], now)})",
                )
            )
        early = " early" if session.early_close else ""
        events.append(
            _event(closes, "session_close", f"Market closes{early} ({_when(closes, now)})")
        )
        for backup_day in (et_date if cal.is_session(et_date) else None, day):
            if backup_day is None:
                continue
            backup_at = _at_et(backup_day, BACKUP_AT_ET)
            if backup_at > now:
                events.append(
                    _event(backup_at, "backup", f"Desk state backup ({_when(backup_at, now)})")
                )
                break
        left = session.calendar_sessions_left
        if left is not None and left < CALENDAR_HORIZON_WARN_SESSIONS:
            last = cal.session_close(cal.sessions()[-1])
            events.append(
                _event(
                    last,
                    "calendar_horizon",
                    f"Trading calendar ends ({_when(last, now)}); regenerate it",
                )
            )
    for one in due:
        events.append(
            _event(
                one.at,
                "approval_deadline",
                f"Approve {one.what} by {_when(one.at, now)} or it expires",
                one.proposal_id,
            )
        )
    try:
        events.extend(_desk_exits(run_dir, cal, now))
    except (OSError, ValueError):  # pydantic's ValidationError included
        return _events_unavailable(Wave3Reason.SCHEDULE_UNAVAILABLE)
    if timers is not None:
        try:
            hosted = tuple(timers())
            if not all(isinstance(event, ScheduledEvent) for event in hosted):
                raise TypeError("a timer reader returns ScheduledEvent rows")
        except Exception as error:  # an injected reader: any failure is "unknown"
            log.error("today: the timer reader failed (%s)", type(error).__name__)
            return _events_unavailable(Wave3Reason.SCHEDULE_UNAVAILABLE)
        events.extend(hosted)
    upcoming = sorted(
        (event for event in events if event.at > now),
        key=lambda event: (event.at, event.kind, event.subject or ""),
    )
    return NextEvents(status="ok", unavailable_reason=None, items=tuple(upcoming))


# -- blockers ------------------------------------------------------------------------------

Scope = Literal["entries", "exits", "approvals", "commands", "display"]
Severity = Literal["info", "warning", "critical"]

_DESK_SERVICE = "Check the desk service (trex-desk) on the host."
_REGENERATE = "Regenerate it with scripts/gen_trex_calendar.py."

_GATEWAY_BLOCKERS: Final[Mapping[str, tuple[Reason, Severity, str, str]]] = {
    "needs_login": (
        Wave3Reason.GATEWAY_NEEDS_LOGIN,
        "critical",
        "IB Gateway needs a login: the desk cannot send orders, exits included.",
        "Log in to the paper account in IB Gateway.",
    ),
    "needs_2fa": (
        Wave3Reason.GATEWAY_NEEDS_2FA,
        "critical",
        "IB Gateway is waiting for 2FA approval: the desk cannot send orders, exits included.",
        "Approve the login in IBKR Mobile.",
    ),
    "api_down": (
        Wave3Reason.GATEWAY_API_DOWN,
        "critical",
        "The IB Gateway API is not answering: the desk cannot send orders, exits included.",
        "Check the gateway container on the host.",
    ),
    "down": (
        Wave3Reason.GATEWAY_DOWN,
        "critical",
        "IB Gateway is down: the desk cannot send orders, exits included.",
        "Check the gateway container on the host.",
    ),
    "starting": (Wave3Reason.GATEWAY_STARTING, "info", "IB Gateway is starting.", ""),
}
_PROFILE_STEP = (
    "Install the capital profile (profile.json in the desk run directory) "
    "with all three loss limits."
)
_PROFILE_WORDS: Final[Mapping[Blocker, str]] = {
    Blocker.PROFILE_ABSENT: "No capital profile is installed: the desk refuses every new entry.",
    Blocker.PROFILE_INVALID: "The capital profile cannot be read: the desk refuses every new entry.",
    Blocker.PROFILE_LIMITS_MISSING: (
        "The capital profile has no complete loss limits: new entries are refused at review."
    ),
}
_FAMILY_KEY_STEP = (
    "Install the TREX family command key and set TREX_OWNER_EMAILS and "
    "TREX_FAMILY_ORIGINS (WAVE3-CONTRACTS.md C3.1)."
)
#: the web cannot write its replay ledger: every family command is refused
#: (assertion_ledger_unavailable) whatever the key and settings
_FAMILY_LEDGER_STEP = (
    "Let the web write its replay ledger: create <desk run dir>/authority/assertions "
    "(install -d -m 0700) and grant it in trex-web's ReadWritePaths "
    "(deploy/trex/trex-web.service), then restart trex-web."
)


def _family_step(problems: Sequence[str]) -> str:
    """What to do for these family ingress problems: the key and settings
    step only when one of them is wrong, the ledger step only for the ledger."""
    ledger = IngressProblem.ASSERTION_LEDGER_UNWRITABLE.value
    steps = []
    if any(problem != ledger for problem in problems):
        steps.append(_FAMILY_KEY_STEP)
    if ledger in problems:
        steps.append(_FAMILY_LEDGER_STEP)
    return " ".join(steps)


@dataclass(frozen=True)
class _Page:
    """Every block, for the blocker list."""

    session: SessionState
    desk_owner: DeskOwnerHealth
    gateway: GatewayHealth
    controls: Controls
    approvals: PendingApprovals
    positions: PositionsSummary
    portfolio: Mapping[str, Any] | None
    acceptance: AcceptanceSummary
    next_events: NextEvents
    #: Wave 3/4 integration: the capital profile's problem (None: usable or unknown)
    profile: Blocker | None = None
    #: why family commands are refused (empty: enabled; None: not wired)
    family: tuple[str, ...] | None = None


def _exit_state_words(page: _Page) -> str:
    """Whether exits run NOW, never a promise (review P2): the pause never
    stops exits, but HALT does, and so does a desk owner that is not healthy."""
    if page.controls.halt.status == "standing":
        return f"Exits are NOT running: {CONTROL_LABELS['halt']} is on."
    owner = page.desk_owner
    if owner.status == "unavailable":
        return "Whether exits are running is unknown: the desk owner's state cannot be read."
    if owner.status != "healthy":
        return "Exits are NOT running: the desk owner is not healthy."
    if page.controls.halt.status == "unavailable":
        return "Whether exits are running is unknown: the emergency stop cannot be read."
    return "Pausing never stops exits; they keep running."


def _blockers(page: _Page) -> tuple[HumanBlocker, ...]:
    """Plain words beside each machine code, most severe first."""
    out: list[HumanBlocker] = []

    def add(code: Reason, scope: Scope, severity: Severity, message: str, step: str = "") -> None:
        out.append(
            HumanBlocker(code=code, scope=scope, severity=severity, message=message, next_step=step)
        )

    session = page.session
    if session.unavailable_reason == Wave3Reason.CALENDAR_EXHAUSTED:
        add(
            Wave3Reason.CALENDAR_EXHAUSTED,
            "exits",
            "critical",
            "The trading calendar has no sessions left: the desk sees no session and runs no exits.",
            _REGENERATE,
        )
    elif session.unavailable_reason is not None:
        add(
            session.unavailable_reason,
            "display",
            "warning",
            "The trading calendar cannot be read, so session times are unknown.",
            "Check the repo calendar file and its checksum on the host.",
        )
    elif (
        session.calendar_sessions_left is not None
        and session.calendar_sessions_left < CALENDAR_HORIZON_WARN_SESSIONS
    ):
        add(
            Wave3Reason.CALENDAR_HORIZON_NEAR,
            "display",
            "warning",
            f"Only {session.calendar_sessions_left} sessions are left in the trading calendar.",
            _REGENERATE,
        )

    owner = page.desk_owner
    if owner.unavailable_reason == Wave3Reason.HEARTBEAT_ABSENT:
        add(
            Blocker.EXIT_OWNER_NOT_READY,
            "exits",
            "critical",
            "Nobody is watching open positions: the desk owner's heartbeat is missing.",
            _DESK_SERVICE,
        )
    elif owner.unavailable_reason is not None:
        add(
            owner.unavailable_reason,
            "exits",
            "warning",
            "Whether the desk owner is running cannot be told: its lock state is unreadable.",
            _DESK_SERVICE,
        )
    elif owner.status == "stopped":
        add(
            Wave3Reason.DESK_OWNER_STOPPED,
            "exits",
            "critical",
            "The desk owner is not running: no exit is watched and no approval can be applied.",
            "Start the desk service (trex-desk) on the host.",
        )
    elif owner.status == "stale":
        add(
            Wave3Reason.DESK_OWNER_STALE,
            "exits",
            "critical",
            f"The desk owner's heartbeat is {owner.heartbeat_age_s}s old "
            f"(limit {owner.stale_after_s}s): exits may not be running.",
            _DESK_SERVICE,
        )

    gateway = page.gateway
    if gateway.unavailable_reason is not None:
        stale = gateway.unavailable_reason == Wave3Reason.GATEWAY_WATCH_STALE
        add(
            gateway.unavailable_reason,
            "display",
            "warning",
            "The IB Gateway watch report is stale, so gateway health is unknown."
            if stale
            else "The IB Gateway watch is not reporting, so gateway health is unknown.",
            "Check trex-gateway-watch.timer on the host.",
        )
    elif gateway.status in _GATEWAY_BLOCKERS:
        code, severity, message, step = _GATEWAY_BLOCKERS[gateway.status]
        add(code, "exits" if severity == "critical" else "display", severity, message, step)

    controls = page.controls
    if controls.halt.status == "standing":
        add(
            Blocker.HALT,
            "exits",
            "critical",
            f"{CONTROL_LABELS['halt']} is on: no order is sent, exits included.",
            f'Use "{CONTROL_LABELS["resume_all"]}" once it is safe.',
        )
    if controls.flatten.status == "standing":
        add(
            Blocker.FLATTEN,
            "entries",
            "warning",
            f'"{CONTROL_LABELS["flatten"]}" is on: the desk is closing its positions '
            "and sends no new entry.",
        )
    if controls.pause_entries.status == "standing":
        add(
            Blocker.ENTRIES_PAUSED,
            "entries",
            "info",
            f"New entries are paused. {_exit_state_words(page)}",
            f'Use "{CONTROL_LABELS["resume_entries"]}" to allow entries again.',
        )
    kills = (controls.pause_entries, controls.halt, controls.flatten)
    if any(kill.status == "unavailable" for kill in kills):
        add(
            Wave3Reason.KILL_FILE_UNREADABLE,
            "display",
            "warning",
            "Whether a stop (pause, emergency stop, close all) stands cannot be read.",
            "Check the desk run directory's permissions on the host.",
        )
    if controls.queued.unavailable_reason is not None:
        add(
            controls.queued.unavailable_reason,
            "commands",
            "warning",
            "The command queue cannot be read, so queued commands are unknown.",
            "Check the desk run directory's commands folder on the host.",
        )

    approvals = page.approvals
    if approvals.unavailable_reason is not None:
        add(
            approvals.unavailable_reason,
            "approvals",
            "warning",
            "Proposals cannot be read, so pending approvals are unknown.",
            "Check the desk run directory's proposals folder on the host.",
        )
    items = approvals.items or ()
    invalidated = [i for i in items if Blocker.APPROVAL_EPOCH_MISMATCH in i.precheck_blockers]
    if invalidated:
        add(
            Blocker.APPROVAL_EPOCH_MISMATCH,
            "approvals",
            "warning",
            f"{len(invalidated)} proposal(s) were composed before the desk owner restarted "
            "and can no longer be approved.",
            "Ask the desk for a fresh proposal.",
        )
    if any(Blocker.EQUITY_QUOTE_NOT_REALTIME in i.precheck_blockers for i in items):
        add(
            Blocker.EQUITY_QUOTE_NOT_REALTIME,
            "entries",
            "warning",
            "Equity quotes are delayed, so new equity entries are refused.",
            "Check the market data subscription in IBKR.",
        )

    holdings, risk = page.positions.holdings, page.positions.risk
    if holdings.unavailable_reason is not None:
        add(
            holdings.unavailable_reason,
            "display",
            "warning",
            "The desk's holdings cannot be read, so its position counts are unknown.",
        )
    if risk.status == "stale":
        add(
            Blocker.RISK_SNAPSHOT_STALE,
            "entries",
            "warning",
            f"The risk snapshot is {risk.age_s}s old: new entries are refused until it is fresh.",
        )
    elif risk.unavailable_reason is not None:
        add(
            risk.unavailable_reason,
            "entries",
            "warning",
            "No usable risk snapshot: new entries are refused until there is one.",
        )
    portfolio = page.portfolio
    if portfolio is not None:
        if Blocker.RECONCILIATION_REQUIRED.value in portfolio.get("entry_blockers", ()):
            add(
                Blocker.RECONCILIATION_REQUIRED,
                "entries",
                "warning",
                "Desk positions are not reconciled with the broker: new entries are refused.",
                "Review the reconciliation on the Portfolio page.",
            )
        reasons = {p.get("reason") for p in portfolio.get("problems", ())}
        if Blocker.NON_PAPER_ACCOUNT.value in reasons:
            add(
                Blocker.NON_PAPER_ACCOUNT,
                "entries",
                "critical",
                "The books name a non-paper account. TREX is paper only.",
                "Check which account the desk is bound to.",
            )
        if Blocker.ACCOUNT_MISMATCH.value in reasons:
            add(
                Blocker.ACCOUNT_MISMATCH,
                "display",
                "critical",
                "The books name more than one account.",
                "Check which account the desk is bound to.",
            )
    held = (holdings.option_positions or 0) + (holdings.equity_positions or 0)
    if holdings.status == "ok" and held > 0:
        halted = controls.halt.status == "standing"
        if owner.status != "healthy" or halted:
            why = f"{CONTROL_LABELS['halt']} is on" if halted else "the desk owner is not healthy"
            add(
                Wave3Reason.EXIT_UNGUARDED,
                "exits",
                "critical",
                f"{held} desk position(s) have no armed exit: {why}.",
                f'Use "{CONTROL_LABELS["resume_all"]}" once it is safe.'
                if halted
                else _DESK_SERVICE,
            )

    acceptance = page.acceptance
    if acceptance.unavailable_reason is not None:
        missing = acceptance.unavailable_reason == Wave3Reason.ACCEPTANCE_MISSING
        add(
            acceptance.unavailable_reason,
            "display",
            "info",
            "Paper acceptance has not been computed yet."
            if missing
            else "The paper acceptance document failed verification.",
        )
    if page.next_events.unavailable_reason is not None:
        add(
            page.next_events.unavailable_reason,
            "display",
            "info",
            "Upcoming events cannot be listed.",
        )
    if page.profile is not None:
        add(page.profile, "entries", "warning", _PROFILE_WORDS[page.profile], _PROFILE_STEP)
    if page.family:
        add(
            Wave3Reason.FAMILY_COMMANDS_DISABLED,
            "commands",
            "info",
            "Commands from Family Apps (the phone) are turned off "
            f"({', '.join(page.family)[:200]}). Commands on this host still work.",
            _family_step(page.family),
        )

    unique: dict[str, HumanBlocker] = {}
    for one in out:
        unique.setdefault(one.code, one)
    return tuple(sorted(unique.values(), key=lambda b: _SEVERITY_ORDER[b.severity]))


# -- the page ------------------------------------------------------------------------------


def _calendar(source: CalendarSource) -> SessionCalendar | None:
    try:
        return source()
    except (CalendarError, OSError, ValueError, KeyError, TypeError) as error:
        log.error("today: the calendar is unreadable (%s)", type(error).__name__)
        return None


def today_view(
    *,
    run_dir: Path,
    supervised_dir: Path,
    gateway_state: Path,
    acceptance_dir: Path,
    now: datetime,
    calendar: CalendarSource = session_calendar,
    lock_probe: LockProbe = proc_locks_probe,
    timers: TimerReader | None = None,
    family_ingress: FamilyIngress | None = None,
) -> TodayView:
    """The ``trex-today/1`` page at ``now`` (see the module docstring).

    ``family_ingress`` (Wave 3/4 integration) returns why family commands are
    refused (empty: enabled); a non-empty answer is the
    ``family_commands_disabled`` blocker, None means not wired (no claim)."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    cal = _calendar(calendar)
    session = _guard(
        "session",
        lambda: (
            _session(cal, now)
            if cal is not None
            else _session_unavailable(Wave3Reason.CALENDAR_UNREADABLE)
        ),
        lambda: _session_unavailable(Wave3Reason.CALENDAR_UNREADABLE),
    )
    desk_owner = _guard(
        "desk_owner",
        lambda: _desk_owner(run_dir, now, lock_probe),
        lambda: _owner_unavailable(Wave3Reason.HEARTBEAT_ABSENT),
    )
    gateway = _guard(
        "gateway",
        lambda: _gateway(gateway_state, now),
        lambda: _gateway_unavailable(Wave3Reason.GATEWAY_WATCH_NOT_REPORTING),
    )

    def kill(name: KillName) -> KillSwitchState:
        return _guard(
            name,
            lambda: _kill_switch(run_dir, name),
            lambda: KillSwitchState(
                name=name,
                status="unavailable",
                unavailable_reason=Wave3Reason.KILL_FILE_UNREADABLE,
                since=None,
                reason=None,
            ),
        )

    controls = Controls(
        pause_entries=kill(PAUSE_ENTRIES),
        halt=kill(HALT),
        flatten=kill(FLATTEN),
        queued=_guard(
            "queued",
            lambda: _queued(run_dir),
            lambda: _queued_unavailable(Blocker.COMMAND_STORE_UNAVAILABLE),
        ),
    )

    def books() -> dict[str, Any] | None:
        return portfolio_payload(run_dir, now)

    portfolio = _guard("portfolio", books, lambda: None)
    positions = (
        positions_summary(portfolio)
        if portfolio is not None
        else PositionsSummary(
            holdings=_holdings_unavailable(Blocker.HOLDINGS_UNREADABLE),
            risk=_risk_unavailable(Blocker.RISK_SNAPSHOT_UNREADABLE),
        )
    )
    entry_blockers = tuple(
        _reason(text, Blocker.RISK_SNAPSHOT_STALE)
        for text in (portfolio or {}).get("entry_blockers", ())
    )
    checks = _Prechecks(
        epoch=_owner_epoch(run_dir),
        controls=controls,
        desk_ready=desk_owner.exit_owner_ready is True,
        entry_blockers=entry_blockers,
        equity_window=_guard("window", lambda: _today_window(cal, now), lambda: None),
        calendar_known=cal is not None,
    )
    approvals, due = _guard(
        "approvals",
        lambda: _approvals(run_dir, supervised_dir, now, checks),
        lambda: (_approvals_unavailable(Wave3Reason.PROPOSALS_DIR_UNREADABLE), ()),
    )
    acceptance = _guard(
        "acceptance",
        lambda: _acceptance(acceptance_dir, cal, now),
        lambda: _acceptance_unavailable(Wave3Reason.ACCEPTANCE_INVALID),
    )
    next_events = _guard(
        "next_events",
        lambda: _next_events(
            now=now, cal=cal, session=session, due=due, run_dir=run_dir, timers=timers
        ),
        lambda: _events_unavailable(Wave3Reason.SCHEDULE_UNAVAILABLE),
    )
    page = _Page(
        session=session,
        desk_owner=desk_owner,
        gateway=gateway,
        controls=controls,
        approvals=approvals,
        positions=positions,
        portfolio=portfolio,
        acceptance=acceptance,
        next_events=next_events,
        profile=_guard("profile", lambda: _profile_problem(run_dir), lambda: None),
        family=_guard(
            "family_ingress",
            lambda: _family_problems(family_ingress),
            lambda: ("family_ingress_status_unreadable",),
        ),
    )
    return TodayView(
        now=now,
        session=session,
        desk_owner=desk_owner,
        gateway=gateway,
        controls=controls,
        approvals=approvals,
        positions=positions,
        acceptance=acceptance,
        next_events=next_events,
        blockers=_blockers(page),
        execution_enabled=False,
    )


def _env_path(name: str, default: Path) -> Path:
    return Path(os.environ.get(name) or str(default)).expanduser()


def attach(
    app: FastAPI,
    *,
    run_dir: PathSource,
    supervised_dir: PathSource,
    clock: Callable[[], datetime],
    gateway_state: PathSource | None = None,
    acceptance_dir: PathSource | None = None,
    calendar: CalendarSource | None = None,
    lock_probe: LockProbe | None = None,
    timers: TimerReader | None = None,
    family_ingress: FamilyIngress | None = None,
) -> None:
    """Mount ``GET /api/today``. Every path resolves per request; the
    defaults read ``TREX_GATEWAY_STATE`` and ``TREX_ACCEPTANCE_DIR`` (else
    their C3.7 / gateway-watch defaults), the repo calendar and
    ``/proc/locks``; no timer reader and no family ingress status unless
    one is given (``app.py`` wires ``family_ingress_status``)."""

    def gateway_path() -> Path:
        if gateway_state is not None:
            return gateway_state()
        return _env_path("TREX_GATEWAY_STATE", DEFAULT_GATEWAY_STATE)

    def acceptance_path() -> Path:
        if acceptance_dir is not None:
            return acceptance_dir()
        return acceptance_dir_from_env(os.environ)

    @app.get(TODAY_PATH)
    def get_today() -> JSONResponse:
        view = today_view(
            run_dir=run_dir(),
            supervised_dir=supervised_dir(),
            gateway_state=gateway_path(),
            acceptance_dir=acceptance_path(),
            now=clock(),
            calendar=calendar if calendar is not None else session_calendar,
            lock_probe=lock_probe if lock_probe is not None else proc_locks_probe,
            timers=timers,
            family_ingress=family_ingress,
        )
        return JSONResponse(model_document(view), headers=NO_STORE)
