"""Wave 4 owner alerts (lane J): one sweep over the desk's files.

Run by :mod:`tree_options.trex.deadman` before every heartbeat
(``trex-deadman.timer``, about every 30 s). It reads, and never writes, the
desk's state; pushes go through :mod:`tree_options.trex.notify` under the
:mod:`tree_options.trex.alert_policy` catalogue (``WAVE4_ALERTS``); what was
sent is booked in its own file (``trex-alert-book/1``).

| Alert | Read from | Safety |
| --- | --- | --- |
| ``reconciliation_required`` | ``<run dir>/ledger/reconciliation.json`` | yes |
| ``approval_expiring`` | ``<run dir>/proposals/``, ``<supervised>/mandates/`` | no |
| ``acceptance_streak_broken`` | ``<TREX_ACCEPTANCE_DIR>/acceptance.json``, verified | no |
| ``exit_quote_blind`` | the tail of ``<run dir>/events.jsonl`` | yes |
| ``cold_restart_missed`` | sent by ``trex.gateway_watch`` (Wave 1), not here | no |

Safety alerts go out at any hour; the others wait for quiet hours to end
(an approval that expires meanwhile is moot and never sent late).

Unknown is never clear: a source that cannot be read reports its reason in
``sources`` and keeps the alerts it had booked (no repeat, no resolution).
Push text names a symbol, a count or an ET time; never an account id, a
URL, an amount of money or an internal id. No broker or web library is
imported (the pending-proposal reader mirrors ``GET /api/proposals``; a
test pins the two together).
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from pydantic import ValidationError

from tree_options.trex.alert_policy import (
    Alert,
    AlertKind,
    PendingApproval,
    QuietHours,
    SessionOutcome,
    approval_alerts,
    plan_alerts,
    quote_blind_alerts,
    reconciliation_alert,
    settle_alert,
    streak_broken_alert,
)
from tree_options.trex.contracts.acceptance import AcceptanceDocument, verify_acceptance
from tree_options.trex.contracts.effects import SupervisedEquityEffect
from tree_options.trex.contracts.layout import Wave2RunDir, Wave2SupervisedDir
from tree_options.trex.contracts.mandates import SupervisedMandateV2
from tree_options.trex.contracts.proposals import EntryProposal, proposal_bytes
from tree_options.trex.contracts.wave3 import AcceptanceDir
from tree_options.trex.reconcile import read_reconciliation

BOOK_SCHEMA: Final = "trex-alert-book/1"
#: ``GET /api/proposals`` reads the newest this many proposal files ...
MAX_PROPOSALS: Final = 100
#: ... of at most this size (both pinned to trex_web.commands_view by a test)
MAX_PROPOSAL_BYTES: Final = 256 * 1024
MAX_ACCEPTANCE_BYTES: Final = 32 * 1024 * 1024
#: ``DeskPaths.events()`` (trex.desk_runtime), named here to keep the broker
#: session code out of this import; a test pins the two together
DESK_EVENTS: Final = "events.jsonl"
#: only the end of the desk's event log is read on each sweep
EVENTS_TAIL_BYTES: Final = 256 * 1024
OK: Final = "ok"

#: (title, message, priority) -> delivered
SendFn = Callable[[str, str, str], bool]


@dataclass(frozen=True)
class SweepPaths:
    run_dir: Path  # TREX_DESK_RUN_DIR
    supervised_dir: Path  # TREX_SUPERVISED_DIR
    acceptance_dir: Path  # TREX_ACCEPTANCE_DIR
    book: Path  # this sweep's own state (trex-alert-book/1)


@dataclass
class SweepResult:
    sources: dict[str, str]
    planned: list[str] = field(default_factory=list)  # kinds due this sweep
    pushed: list[str] = field(default_factory=list)  # kinds delivered
    failed: int = 0
    held: int = 0
    active: int = 0
    book_written: bool = False
    dry_run: bool = False

    @property
    def degraded(self) -> bool:
        """A push failed or the book could not be written (sources that are
        not there yet are normal, not degraded)."""
        return self.failed > 0 or (not self.dry_run and not self.book_written)

    def summary(self) -> dict[str, Any]:
        return {
            "sources": dict(self.sources),
            "active": self.active,
            "planned": list(self.planned),
            "pushed": list(self.pushed),
            "failed": self.failed,
            "held": self.held,
            "degraded": self.degraded,
        }


# -- sources --------------------------------------------------------------------------


def _reconciliation(run_dir: Path) -> tuple[list[Alert], str]:
    layout = Wave2RunDir(run_dir)
    if not layout.reconciliation().exists():
        return [], "reconciliation_missing"
    rec = read_reconciliation(layout)
    if rec is None:
        return [], "reconciliation_unreadable"
    alert = reconciliation_alert(
        rec.status, [m.kind for m in rec.mismatches], rec.as_of.timestamp()
    )
    return ([alert] if alert is not None else []), OK


def _mandate_blocks(supervised: Wave2SupervisedDir, intent_id: str, proposal_id: str) -> str | None:
    """ "decided" when a mandate for this exact proposal exists (approved or
    consumed), "unknown" when one cannot be read, else None (as the web)."""
    for path in (supervised.mandate_consumed(intent_id), supervised.mandate(intent_id)):
        try:
            if not path.exists():
                continue
            if path.is_symlink() or path.stat().st_size > MAX_PROPOSAL_BYTES:
                return "unknown"
            mandate = SupervisedMandateV2.model_validate_json(path.read_bytes())
        except (OSError, ValidationError):
            return "unknown"
        if mandate.proposal_id == proposal_id:
            return "decided"
    return None


def _label(proposal: EntryProposal) -> str:
    effect = proposal.effect
    if isinstance(effect, SupervisedEquityEffect):
        return f"{effect.symbol} equity"
    return f"{effect.structure.underlying} option"


def pending_approvals(
    run_dir: Path, supervised_dir: Path, now: datetime
) -> tuple[list[PendingApproval], int]:
    """(pending proposals, problem files): the ``pending`` rows of
    ``GET /api/proposals`` (newest ``MAX_PROPOSALS`` files, canonical bytes,
    not rejected, no mandate, before ``send_deadline``)."""
    layout = Wave2RunDir(run_dir)
    supervised = Wave2SupervisedDir(supervised_dir)
    folder = layout.proposals()
    if not folder.is_dir():
        return [], 0
    problems = 0
    candidates: list[tuple[float, Path]] = []
    for path in folder.glob("*.json"):
        if path.name.endswith(".rejected.json"):
            continue
        try:
            candidates.append((path.lstat().st_mtime, path))
        except OSError:
            problems += 1
    candidates.sort(key=lambda item: (item[0], item[1].name), reverse=True)
    pending: list[PendingApproval] = []
    for _, path in candidates[:MAX_PROPOSALS]:
        try:
            if path.is_symlink() or path.stat().st_size > MAX_PROPOSAL_BYTES:
                problems += 1
                continue
            raw = path.read_bytes()
            proposal = EntryProposal.model_validate_json(raw)
        except (OSError, ValidationError, ValueError):
            problems += 1
            continue
        if proposal_bytes(proposal) != raw or path.name != f"{proposal.proposal_id}.json":
            problems += 1
            continue
        if layout.proposal_rejected(proposal.proposal_id).exists():
            continue
        state = _mandate_blocks(supervised, proposal.effect.intent_id, proposal.proposal_id)
        if state == "unknown":
            problems += 1
            continue
        if state == "decided" or now >= proposal.send_deadline:
            continue
        pending.append(
            PendingApproval(
                proposal_id=proposal.proposal_id,
                label=_label(proposal),
                send_deadline=proposal.send_deadline.timestamp(),
            )
        )
    return pending, problems


def _acceptance(acceptance_dir: Path, calendar: Any) -> tuple[list[SessionOutcome] | None, str]:
    """The verified document's session results, or why there are none (the
    ``GET /api/acceptance`` unavailable reasons)."""
    path = AcceptanceDir(acceptance_dir).document()
    try:
        if not path.exists():
            return None, "acceptance_missing"
        if path.is_symlink() or path.stat().st_size > MAX_ACCEPTANCE_BYTES:
            return None, "acceptance_invalid"
        document = AcceptanceDocument.model_validate_json(path.read_bytes())
    except (OSError, ValidationError, ValueError):
        return None, "acceptance_invalid"
    if calendar is None:
        from tree_options.trex.clock import session_calendar

        calendar = session_calendar()
    try:
        verify_acceptance(document, calendar)
    except ValueError as error:
        reason = str(error)
        if reason not in ("acceptance_calendar_mismatch", "acceptance_window_mismatch"):
            reason = "acceptance_invalid"
        return None, reason
    return [
        SessionOutcome(session=r.session, qualifying=r.qualifying, reasons=tuple(r.reasons))
        for r in document.session_results
    ], OK


def _events_tail(run_dir: Path) -> tuple[list[dict[str, Any]], str]:
    """The JSON objects in the last ``EVENTS_TAIL_BYTES`` of the desk's
    event log (a cut first line is dropped; bad lines are skipped)."""
    path = run_dir / DESK_EVENTS
    try:
        with path.open("rb") as stream:
            size = os.fstat(stream.fileno()).st_size
            start = max(0, size - EVENTS_TAIL_BYTES)
            stream.seek(start)
            chunk = stream.read(EVENTS_TAIL_BYTES)
    except FileNotFoundError:
        return [], OK
    except OSError:
        return [], "desk_events_unreadable"
    lines = chunk.split(b"\n")
    if start > 0:
        lines = lines[1:]
    events: list[dict[str, Any]] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            events.append(value)
    return events, OK


@dataclass(frozen=True)
class Observation:
    alerts: tuple[Alert, ...]
    sources: dict[str, str]
    unknown: frozenset[AlertKind]


def observe(paths: SweepPaths, now: datetime, *, calendar: Any = None) -> Observation:
    """Every active Wave 4 alert this sweep can see, and each source's status."""
    epoch = now.timestamp()
    alerts: list[Alert] = []
    sources: dict[str, str] = {}
    unknown: set[AlertKind] = set()

    found, sources["reconciliation"] = _reconciliation(paths.run_dir)
    alerts += found
    if sources["reconciliation"] != OK:
        unknown.add(AlertKind.RECONCILIATION_REQUIRED)

    try:
        pending, _ = pending_approvals(paths.run_dir, paths.supervised_dir, now)
        sources["proposals"] = OK
        alerts += approval_alerts(pending, epoch)
    except OSError:
        sources["proposals"] = "proposals_unreadable"
        unknown.add(AlertKind.APPROVAL_EXPIRING)

    outcomes, sources["acceptance"] = _acceptance(paths.acceptance_dir, calendar)
    if outcomes is None:
        unknown.add(AlertKind.ACCEPTANCE_STREAK_BROKEN)
    else:
        streak = streak_broken_alert(outcomes)
        alerts += [streak] if streak is not None else []

    events, sources["desk_events"] = _events_tail(paths.run_dir)
    if sources["desk_events"] != OK:
        unknown.add(AlertKind.EXIT_QUOTE_BLIND)
    alerts += quote_blind_alerts(events, epoch)
    return Observation(tuple(alerts), sources, frozenset(unknown))


# -- the book -------------------------------------------------------------------------


def _read_book(path: Path) -> dict[str, Any]:
    """The booked alerts; a missing or corrupt book starts over (an alert
    may then go out once more: the safe side)."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    alerts = data.get("alerts") if isinstance(data, dict) else None
    return alerts if isinstance(alerts, dict) else {}


def _write_book(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(json.dumps(document, indent=1, sort_keys=True))
    os.replace(tmp, path)


def sweep(
    paths: SweepPaths,
    *,
    now: datetime,
    quiet: QuietHours | None,
    send: SendFn,
    dry_run: bool = False,
    calendar: Any = None,
) -> SweepResult:
    """Observe, plan, push what is due, book the outcome. ``dry_run`` sends
    nothing and writes nothing (``planned`` shows what would go out)."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    now = now.astimezone(UTC)
    epoch = now.timestamp()
    seen = observe(paths, now, calendar=calendar)
    prior = _read_book(paths.book)
    pushes, book = plan_alerts(seen.alerts, prior, now=epoch, quiet=quiet, unknown=seen.unknown)
    active = {alert.ident for alert in seen.alerts}
    result = SweepResult(
        sources=seen.sources,
        planned=[p.kind.value for p in pushes],
        held=sum(1 for ident in active if book.get(ident, {}).get("held") is True),
        active=len(active),
        dry_run=dry_run,
    )
    if dry_run:
        return result
    for push in pushes:
        try:
            delivered = bool(send(push.title, push.message, push.priority))
        except Exception:  # a push failure never breaks the sweep
            delivered = False
        settle_alert(book, prior, push.ident, delivered, epoch)
        if delivered:
            result.pushed.append(push.kind.value)
        else:
            result.failed += 1
    try:
        _write_book(
            paths.book,
            {"schema": BOOK_SCHEMA, "checked_at": epoch, "sources": seen.sources, "alerts": book},
        )
        result.book_written = True
    except OSError:
        result.book_written = False
    return result
