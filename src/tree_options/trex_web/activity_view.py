"""``GET /api/activity``: ``trex-activity/1`` (C3.2), the owner's activity feed.

Wave 3 lane D. Read-only and broker-free: it merges, newest first, what the
producers already write, and never writes anything itself.

| Source | Files | One item per |
| --- | --- | --- |
| ``command`` | ``<run dir>/commands/<key>.result.json`` with the command document beside it (``<key>.json`` queued, ``<key>.claimed.json`` being applied, ``done/<key>.claimed.json`` answered) | idempotency key |
| ``desk_event`` | ``<run dir>/events.jsonl`` (``DeskRuntime._event``'s lines) | line |
| ``audit`` | ``<run dir>/pause-entries.jsonl`` (``trex.pause``) and ``<run dir>/automation.jsonl`` (``automation.kill_file_action``) | line |
| ``drill`` | ``<acceptance dir>/drills/<drill_id>.json`` (``DrillRecord``, C3.7) | drill that ran |

Item rules (``ActivityItem``):

- ids are ``<source>:<local id>``: the command key, ``<file>:<byte offset>``
  of a log line (zero padded; append-only logs keep it stable), the drill
  id;
- a command's ``at`` is its latest result's ``at``; its ``actor`` is the
  stamped ``issued_by`` of a command document whose ``command_body_sha256``
  equals the result's (that hash binds the principal and the ingress), so an
  answer never borrows the actor of another document; with no such document
  (refused at intake, never queued) the actor is ``actor_not_recorded``;
- desk events are ``{sub: "trex-desk", ingress: "system"}``; a pause audit
  line's ``<ingress>:<sub>`` actor is shown when it names a command ingress
  (``family``, ``loopback``), and anything else (the terminal's own label,
  the kill-file audit's fixed ``cockpit``, a drill's attester, whose ingress
  is not recorded) is ``actor_not_recorded`` with the label in the summary.

Redaction: summaries are composed from fixed words plus whitelisted fields;
free text a file carries (an owner's reason, a result detail, a terminal
label, a drill note) passes ``redact`` first, which masks IBKR account ids
and anything secret-shaped. ``refs`` hold only path-safe ids from named
fields (``proposal_id``, ``intent_id``, ``program_id``, ``drill_id``,
``path``); no payload is ever copied.

Unknown is unavailable, never empty: a missing desk run dir makes the three
run-dir sources ``unavailable`` (``activity_source_unreadable``); a missing
acceptance dir makes ``drill`` unavailable (``acceptance_missing``). Inside
an existing root a missing file is simply nothing recorded yet. A file that
cannot be read (a link, too big, invalid) makes its source ``unavailable``
while every readable item of it is still listed. A log's last line without
its newline is an append in flight, not an error.

Paging is keyset paging over ``(at, id)``, newest first: the opaque cursor
names the last item served and the next page holds the items strictly older
than it, so pages never repeat or skip an item while new activity arrives on
top. Horizons (documented, not hidden): each log is read from its newest
``MAX_LOG_TAIL_BYTES``; at most ``MAX_COMMAND_RESULTS`` command results (the
newest by file time) are considered.

Wiring (the coordinator's ``app.py``)::

    from tree_options.trex_web.activity_view import attach as attach_activity
    attach_activity(app, run_dir=resolve_run_dir, clock=clock)

``acceptance_dir`` defaults to ``TREX_ACCEPTANCE_DIR`` (default
``~/.local/state/trex/acceptance``) read per request.
"""

from __future__ import annotations

import base64
import binascii
import heapq
import json
import os
import re
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.datastructures import QueryParams

from tree_options.trex.contracts.acceptance import DRILL_IDS, DrillRecord
from tree_options.trex.contracts.canonical import model_document
from tree_options.trex.contracts.commands import (
    AnyCommand,
    CommandResult,
    command_body_sha256,
    parse_command,
)
from tree_options.trex.contracts.layout import FLATTEN, HALT, Wave2RunDir
from tree_options.trex.contracts.wave3 import (
    ACTIVITY_PATH,
    ACTIVITY_SOURCES,
    CONTROL_LABELS,
    DESK_SYSTEM_SUB,
    AcceptanceDir,
    ActivityItem,
    ActivityPage,
    ActivityQuery,
    ActivitySourceStatus,
    Wave3Reason,
)
from tree_options.trex.desk_commands import ARCHIVE_DIR, CLAIMED_SUFFIX
from tree_options.trex.desk_runtime import DeskPaths
from tree_options.trex.pause import PAUSE_AUDIT_FILE

__all__ = [
    "ACCEPTANCE_DIR_DEFAULT",
    "ACCEPTANCE_DIR_ENV",
    "AUTOMATION_AUDIT_FILE",
    "MAX_COMMAND_RESULTS",
    "MAX_LOG_TAIL_BYTES",
    "BadQuery",
    "activity_page",
    "attach",
    "default_acceptance_dir",
    "parse_query",
    "redact",
]

ACCEPTANCE_DIR_ENV: Final = "TREX_ACCEPTANCE_DIR"
ACCEPTANCE_DIR_DEFAULT: Final = "~/.local/state/trex/acceptance"
#: ``automation._audit``'s file in the run dir (the kill-file verbs' audit)
AUTOMATION_AUDIT_FILE: Final = "automation.jsonl"
#: each log is read from its newest this-many bytes (the feed's horizon)
MAX_LOG_TAIL_BYTES: Final = 8 * 1024 * 1024
#: at most this many command results, the newest by file time
MAX_COMMAND_RESULTS: Final = 5000
#: the web store's bound on a stored result (``command_store.MAX_STORED_BYTES``)
MAX_RESULT_BYTES: Final = 256 * 1024
#: the bound on a command document (``desk_commands.MAX_COMMAND_BYTES``)
MAX_COMMAND_BYTES: Final = 64 * 1024
MAX_DRILL_BYTES: Final = 256 * 1024
MAX_SUMMARY_CHARS: Final = 500
#: free text inside a summary (a reason, a detail, a note) is clipped to this
MAX_QUOTED_CHARS: Final = 160
NO_STORE: Final = {"Cache-Control": "no-store"}

_KEY = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_RESULT_NAME = re.compile(rf"^(?P<key>{_KEY})\.result\.json$")
_ITEM_ID = re.compile(r"^(command|desk_event|audit|drill):[A-Za-z0-9._:-]{1,200}$")
#: a value ``refs`` may carry (the contract's ids are path-safe)
_REF_VALUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
#: a machine code worth quoting in a summary (``ReasonCode``'s shape)
_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}(:[a-z0-9_.-]{1,64})?$")
_CURSOR_VERSION: Final = "1"
_NOT_RECORDED: Final = Wave3Reason.ACTOR_NOT_RECORDED.value
_UNREADABLE: Final = Wave3Reason.ACTIVITY_SOURCE_UNREADABLE

# -- redaction -------------------------------------------------------------------

#: IBKR account ids: paper ``DU``/``DF``/``DI``, live ``U``/``F``/``I``, digits
_ACCOUNT = re.compile(r"\b(?:D[UFI]|[UFI])\d{4,10}\b")
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(password|passwd|passphrase|secret|token|api[_-]?key|access[_-]?key|"
    r"private[_-]?key|authorization|cookie)(\s*[:=]\s*)\S+"
)
_BEARER = re.compile(r"(?i)\bbearer\s+\S+")
#: a long run of token characters with a digit in it (keys, base64, hashes)
_LONG_TOKEN = re.compile(r"(?=[A-Za-z0-9+/_=.-]*\d)[A-Za-z0-9+/_=.-]{24,}")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")


def redact(text: str) -> str:
    """``text`` fit for the feed: account ids and secret-shaped tokens masked,
    control characters folded to spaces."""
    text = _CONTROL.sub(" ", text)
    text = _SECRET_ASSIGNMENT.sub(r"\1\2[redacted]", text)
    text = _BEARER.sub("bearer [redacted]", text)
    text = _ACCOUNT.sub("[account]", text)
    return _LONG_TOKEN.sub("[redacted]", text).strip()


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def _quoted(text: Any) -> str:
    """Free text from a file, redacted and clipped (empty when not text)."""
    return _clip(redact(text), MAX_QUOTED_CHARS) if isinstance(text, str) else ""


def _summary(text: str) -> str:
    return _clip(" ".join(text.split()), MAX_SUMMARY_CHARS) or "Activity"


def _code(value: Any) -> str | None:
    return value if isinstance(value, str) and _CODE.fullmatch(value) else None


def _ref(value: Any) -> str | None:
    if not isinstance(value, str) or not _REF_VALUE.fullmatch(value):
        return None
    return None if _ACCOUNT.search(value) else value


#: (field a producer writes, ``refs`` key); the first value found wins
_REF_FIELDS: Final = (
    ("proposal_id", "proposal_id"),
    ("intent_id", "intent_id"),
    ("intent", "intent_id"),
    ("program_id", "program_id"),
    ("program", "program_id"),
    ("drill_id", "drill_id"),
)


def _refs(*sources: Mapping[str, Any] | None) -> dict[str, str]:
    refs: dict[str, str] = {}
    for source in sources:
        if not isinstance(source, Mapping):
            continue
        for name, key in _REF_FIELDS:
            value = _ref(source.get(name))
            if value is not None and key not in refs:
                refs[key] = value
    return refs


# -- words --------------------------------------------------------------------------

_COMMAND_LABELS: Final[Mapping[str, str]] = {
    **CONTROL_LABELS,
    "compose_entry": "Compose a trade proposal",
    "approve_entry": "Approve entry",
    "reject_entry": "Reject trade proposal",
    "backup_state": "Back up desk state",
}
_INGRESS_WORDS: Final[Mapping[str, str]] = {
    "family": "via Family Apps",
    "loopback": "from the local cockpit",
}
_REASON_WORDS: Final[Mapping[str, str]] = {
    "command_expired": "it expired before the desk took it",
    "command_not_yet_valid": "it was issued ahead of the desk clock",
    "command_body_mismatch": "its key was used for a different command",
    "duplicate_command": "a duplicate of an earlier command",
    "command_unauthorized": "it was not an authenticated owner command",
    "command_invalid": "the desk could not read it",
    "approval_epoch_mismatch": "the desk restarted after the proposal was made",
    "approval_expired": "the approval deadline passed",
    "approval_effect_mismatch": "the proposal changed after it was shown",
    "approval_proposal_mismatch": "the proposal changed after it was shown",
    "proposal_unknown": "the desk does not know that proposal",
    "proposal_rejected": "the proposal was already rejected",
    "mandate_already_consumed": "that entry was already sent",
    "owner_stopped_before_result": "the desk stopped before answering; it is never re-applied",
    "gateway_unavailable": "the gateway was not connected",
    "outside_regular_session": "the market was closed",
    "entries_paused": "new entries are paused",
    "halt": "the emergency stop stands",
    "flatten": "close-all stands",
    "broker_rejected": "the paper broker rejected the order",
    "send_uncertain": "the send outcome is uncertain; reconciliation decides",
    "apply_error": "applying it failed",
}
_DESK_WORDS: Final[Mapping[str, str]] = {
    "entries_paused": "New entries paused; exits continue",
    "entries_resumed": "New entries resumed",
    "proposal_composed": "Trade proposal ready for review",
    "entry_approved": "Approved entry sent to the paper broker",
    "entry_rejected": "Trade proposal rejected",
    "approvals_invalidated": "Pending approvals invalidated",
    "command_applied": "The desk applied a command",
    "command_refused": "The desk refused a command",
    "equity_exit_stop": "Equity exit: stop reached",
    "equity_exit_target": "Equity exit: target reached",
    "equity_exit_deadline": "Equity exit: holding deadline reached",
    "equity_exit_forced": "Equity exit forced",
    "reconciliation_clean": "Reconciliation clean",
    "reconciliation_required": "Reconciliation required before the next sell",
    "equity_contract_mismatch": "Equity contract does not match the broker",
    "equity_unqualified": "Equity symbol could not be qualified",
    "exit_evidence_inconclusive": "Exit evidence inconclusive",
    "holdings_unverifiable": "Holdings could not be verified",
    "structure_tick_failed": "A position check failed; its exits are not running",
    "commands_failed": "A command step failed (contained; exits unaffected)",
    "gateway_connected": "Gateway connected",
    "gateway_lost": "Gateway connection lost",
    "books_problem": "Books problem",
    "books_failed": "Books update failed",
    "entry_request": "Entry requested",
    "entry_filled": "Entry filled",
    "entry_uncertain_held": "Entry outcome uncertain; held for review",
    "exit_begin": "Exit started",
    "exit_blocked": "Exit blocked",
    "exit_flat_unexplained": "Flat at the broker without a desk exit",
    "unknown_exposure": "Unknown exposure at the broker",
    "legs_mismatch": "Option legs do not match the broker",
    "closed": "Position closed",
    "backup_failed": "State backup failed",
}
_DRILL_WORDS: Final[Mapping[str, str]] = {
    "relogin": "gateway re-login",
    "restart_mid_position": "restart with a position open",
    "gateway_outage": "gateway outage",
    "duplicate_command": "duplicate command",
    "backup_restore": "backup and restore",
    "notification": "notification",
    "unassisted_use": "unassisted use",
}


def _reason_words(code: str) -> str:
    words = _REASON_WORDS.get(code)
    return f"{words} ({code})" if words else code


def _token(value: Any) -> str:
    """A producer's name as an item ``kind`` (snake case, bounded)."""
    if not isinstance(value, str):
        return ""
    return re.sub(r"[^a-z0-9_.:-]", "_", value.strip().lower())[:64].strip("_")


# -- entries ------------------------------------------------------------------------


@dataclass(frozen=True)
class _Entry:
    """One feed item before the page is cut (built into ``ActivityItem`` only
    when served)."""

    at: datetime
    id: str
    source: str
    fields: dict[str, Any]

    @property
    def position(self) -> tuple[datetime, str]:
        return (self.at, self.id)


@dataclass
class _Source:
    entries: list[_Entry] = field(default_factory=list)
    reason: Wave3Reason | None = None

    def fail(self, reason: Wave3Reason = Wave3Reason.ACTIVITY_SOURCE_UNREADABLE) -> None:
        if self.reason is None:
            self.reason = reason


def _instant(value: Any) -> datetime | None:
    """A recorded instant (``...Z`` or an offset) in UTC; None unless aware."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.utcoffset() is not None else None


def _entry(
    source: str,
    local_id: str,
    at: datetime,
    *,
    kind: str,
    summary: str,
    actor: dict[str, str] | None,
    refs: dict[str, str] | None = None,
    command_key: str | None = None,
    command_status: str | None = None,
    reason: str | None = None,
) -> _Entry:
    return _Entry(
        at=at,
        id=f"{source}:{local_id}",
        source=source,
        fields={
            "kind": kind,
            "summary": _summary(summary),
            "actor": actor,
            "actor_unavailable_reason": None if actor is not None else _NOT_RECORDED,
            "command_key": command_key,
            "command_status": command_status,
            "reason": reason,
            "refs": refs or {},
        },
    )


# -- bounded reads ----------------------------------------------------------------------


def _read_bounded(path: Path, limit: int) -> bytes | None:
    """The bytes of a regular file (never through a link); None when absent;
    ``OSError`` when it is a link, not a regular file, or over ``limit``."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise OSError(f"{path.name} is not a bounded regular file")
        return stream.read(limit + 1)


def _log_lines(path: Path) -> list[tuple[int, bytes]] | None:
    """``(byte offset, line)`` of every complete line in the newest
    ``MAX_LOG_TAIL_BYTES`` of an append-only log; None when absent. The part
    after the last newline is an append in flight and is left out, and so is
    a line the window cuts. ``OSError`` when unreadable (a link, not a file)."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise OSError(f"{path.name} is not a regular file")
        size = info.st_size
        start = max(0, size - MAX_LOG_TAIL_BYTES)
        # one byte before the window tells whether its first line is whole
        stream.seek(max(0, start - 1))
        data = stream.read(size - max(0, start - 1))
    base = max(0, start - 1)
    if start > 0:
        cut = data.find(b"\n")
        if cut < 0:
            return []
        data, base = data[cut + 1 :], base + cut + 1
    lines: list[tuple[int, bytes]] = []
    position = 0
    while (end := data.find(b"\n", position)) >= 0:
        if data[position:end].strip():
            lines.append((base + position, data[position:end]))
        position = end + 1
    return lines


def _json_object(raw: bytes) -> dict[str, Any] | None:
    try:
        document = json.loads(raw)
    except (ValueError, RecursionError):
        return None
    return document if isinstance(document, dict) else None


def _log_id(name: str, offset: int) -> str:
    return f"{name}:{offset:012d}"


# -- command -----------------------------------------------------------------------------


def _result_names(folder: Path) -> list[tuple[str, str]]:
    """``(key, file name)`` of the stored results, at most
    ``MAX_COMMAND_RESULTS`` (the newest by file time)."""
    with os.scandir(folder) as listing:
        names = [
            (match["key"], entry.name)
            for entry in listing
            if (match := _RESULT_NAME.fullmatch(entry.name)) is not None
        ]
    if len(names) <= MAX_COMMAND_RESULTS:
        return names

    def mtime(item: tuple[str, str]) -> float:
        try:
            return (folder / item[1]).lstat().st_mtime
        except OSError:
            return 0.0

    return sorted(names, key=mtime, reverse=True)[:MAX_COMMAND_RESULTS]


def _command_document(
    layout: Wave2RunDir, key: str, result: CommandResult
) -> tuple[str, AnyCommand | None]:
    """(state, the command the result is about): state is ``queued`` /
    ``claimed`` while its file stands, else ``answered``; the command is
    the first document whose body hash is the result's (else None)."""
    folder = layout.commands()
    candidates = (
        ("queued", layout.command(key)),
        ("claimed", folder / f"{key}{CLAIMED_SUFFIX}"),
        ("answered", folder / ARCHIVE_DIR / f"{key}{CLAIMED_SUFFIX}"),
    )
    state = next((name for name, path in candidates if os.path.lexists(path)), "answered")
    for _, path in candidates:
        try:
            raw = _read_bounded(path, MAX_COMMAND_BYTES)
        except OSError:
            continue  # a link or an oversize file names no actor
        document = None if raw is None else _json_object(raw)
        if document is None:
            continue
        try:
            command = parse_command(document)
        except (ValidationError, ValueError):
            continue
        if command_body_sha256(command) == result.command_body_sha256:
            return state, command
    return state, None


def _command_summary(
    result: CommandResult, state: str, command: AnyCommand | None, facts: Mapping[str, Any]
) -> str:
    label = _COMMAND_LABELS.get(result.kind, result.kind)
    status = result.status
    if status == "accepted":
        phrase = {
            "queued": "queued for the desk",
            "claimed": "being applied by the desk",
        }.get(state, "accepted, waiting for the desk")
    elif status == "applied":
        phrase = "applied"
        if facts.get("applied_after_expiry") is True:
            phrase += " after it expired (a safety command is never dropped)"
        if facts.get("superseded_by_newer_stop"):
            phrase += "; a newer stop stays in place"
    elif status == "duplicate":
        phrase = "duplicate of an earlier command"
    else:
        phrase = f"{status}, {_reason_words(result.reason or status)}"
        detail = _quoted(result.detail)
        if detail:
            phrase += f": {detail}"
    text = f"{label}: {phrase}."
    if command is not None:
        text += f" Sent {_INGRESS_WORDS[command.issued_by.ingress]}."
        reason = _quoted(getattr(command.args, "reason", ""))
        if reason:
            text += f" Reason: {reason}"
    return text


def _commands(run_dir: Path) -> _Source:
    source = _Source()
    layout = Wave2RunDir(run_dir)
    folder = layout.commands()
    if not folder.exists():
        return source
    try:
        names = _result_names(folder)
    except OSError:
        source.fail()
        return source
    for key, name in names:
        try:
            raw = _read_bounded(folder / name, MAX_RESULT_BYTES)
            if raw is None:
                continue  # removed while listing
            result = CommandResult.model_validate_json(raw)
        except (OSError, ValidationError, ValueError):
            source.fail()
            continue
        if result.idempotency_key != key:
            source.fail()
            continue
        state, command = _command_document(layout, key, result)
        facts: Mapping[str, Any] = result.result or {}
        refs = _refs(None if command is None else model_document(command.args), facts)
        kill_file = facts.get("kill_file")
        if result.kind in ("halt", "flatten") and kill_file in (HALT, FLATTEN):
            refs.setdefault("path", str(kill_file))
        actor = (
            None
            if command is None
            else {"sub": command.issued_by.sub, "ingress": command.issued_by.ingress}
        )
        source.entries.append(
            _entry(
                "command",
                key,
                result.at,
                kind=result.kind,
                summary=_command_summary(result, state, command, facts),
                actor=actor,
                refs=refs,
                command_key=key,
                command_status=result.status,
                reason=result.reason
                if result.status in ("refused", "failed", "duplicate")
                else None,
            )
        )
    return source


# -- desk events ------------------------------------------------------------------------


def _desk_summary(event: str, payload: Mapping[str, Any]) -> str:
    words = _DESK_WORDS.get(event) or f"Desk event: {event.replace('_', ' ')}"
    if event in ("command_applied", "command_refused"):
        kind = payload.get("kind")
        label = _COMMAND_LABELS.get(kind) if isinstance(kind, str) else None
        if label is not None:
            verb = "applied" if event == "command_applied" else "refused"
            words = f"The desk {verb}: {label}"
    subject = _ref(payload.get("structure")) or _ref(payload.get("intent"))
    if subject is not None:
        words += f" ({subject})"
    outcome = _code(payload.get("outcome"))
    if outcome is not None:
        words += f"; outcome {outcome}"
    reason = _code(payload.get("reason")) or _code(payload.get("why"))
    if reason is not None:
        words += f"; {_reason_words(reason)}"
    return words


def _desk_events(run_dir: Path) -> _Source:
    source = _Source()
    path = DeskPaths(run_dir).events()
    try:
        lines = _log_lines(path)
    except OSError:
        source.fail()
        return source
    for offset, raw in lines or ():
        line = _json_object(raw)
        at = None if line is None else _instant(line.get("ts"))
        kind = "" if line is None else _token(line.get("event"))
        if line is None or at is None or not kind:
            source.fail()
            continue
        source.entries.append(
            _entry(
                "desk_event",
                _log_id(path.name, offset),
                at,
                kind=kind,
                summary=_desk_summary(kind, line),
                actor={"sub": DESK_SYSTEM_SUB, "ingress": "system"},
                refs=_refs(line),
            )
        )
    return source


# -- audit ----------------------------------------------------------------------------------


def _audit_actor(label: Any) -> tuple[dict[str, str] | None, str]:
    """(actor, words): a command ingress's ``<ingress>:<sub>`` is an actor;
    any other label (the terminal's own) is shown, unverified, in words."""
    if not isinstance(label, str):
        return None, ""
    ingress, _, sub = label.partition(":")
    if ingress in _INGRESS_WORDS and sub.strip() and not _CONTROL.search(sub):
        return {"sub": sub.strip(), "ingress": ingress}, _INGRESS_WORDS[ingress]
    quoted = _quoted(label)
    return None, f"at the terminal ({quoted})" if quoted else "at the terminal"


def _pause_item(offset: int, line: Mapping[str, Any]) -> _Entry | None:
    event, at = line.get("event"), _instant(line.get("at"))
    if event not in ("entries_paused", "entries_resumed") or at is None:
        return None
    actor, who = _audit_actor(line.get("actor"))
    paused = event == "entries_paused"
    superseded = line.get("superseded_by_newer_stop") is True
    if superseded:
        head = "Resume of new entries asked"
    else:
        head = "New entries paused" if paused else "New entries resumed"
    text = f"{head} {who}".strip()
    if superseded:
        text += "; a newer pause stays in place"
    elif line.get("changed") is False:
        text += "; no change, already paused" if paused else "; no change, nothing to resume"
    reason = _quoted(line.get("reason"))
    if reason:
        text += f". Reason: {reason}"
    return _entry(
        "audit", _log_id(PAUSE_AUDIT_FILE, offset), at, kind=event, summary=text, actor=actor
    )


def _automation_item(offset: int, line: Mapping[str, Any]) -> _Entry | None:
    at, action, subject = _instant(line.get("at")), line.get("action"), line.get("subject")
    if at is None or not isinstance(action, str) or not isinstance(subject, str):
        return None
    local_id = _log_id(AUTOMATION_AUDIT_FILE, offset)
    refs: dict[str, str] = {}
    if action == "kill_file" and subject.upper() in (HALT, FLATTEN):
        kind, text = "kill_file_placed", f"{subject.upper()} placed at the terminal"
        refs["path"] = subject.upper()
    elif action == "kill_file" and subject == "resume":
        kind, text = "kill_files_removed", f"{HALT} and {FLATTEN} removed at the terminal"
    else:
        kind = _token(f"automation_{action}") or "automation"
        text = f"Automation: {_quoted(action)} {_quoted(subject)}"
    return _entry("audit", local_id, at, kind=kind, summary=text, actor=None, refs=refs)


_AUDITS: Final[tuple[tuple[str, Callable[[int, Mapping[str, Any]], _Entry | None]], ...]] = (
    (PAUSE_AUDIT_FILE, _pause_item),
    (AUTOMATION_AUDIT_FILE, _automation_item),
)


def _audits(run_dir: Path) -> _Source:
    source = _Source()
    for name, build in _AUDITS:
        try:
            lines = _log_lines(run_dir / name)
        except OSError:
            source.fail()
            continue
        for offset, raw in lines or ():
            line = _json_object(raw)
            entry = None if line is None else build(offset, line)
            if entry is None:
                source.fail()
                continue
            source.entries.append(entry)
    return source


# -- drills ---------------------------------------------------------------------------------


def _drills(acceptance_dir: Path) -> _Source:
    source = _Source()
    if not acceptance_dir.exists():
        source.fail(Wave3Reason.ACCEPTANCE_MISSING)
        return source
    if not acceptance_dir.is_dir():
        source.fail()
        return source
    layout = AcceptanceDir(acceptance_dir)
    for drill_id in DRILL_IDS:
        try:
            raw = _read_bounded(layout.drill(drill_id), MAX_DRILL_BYTES)
            if raw is None:
                continue  # not recorded yet
            record = DrillRecord.model_validate_json(raw)
        except (OSError, ValidationError, ValueError):
            source.fail()
            continue
        if record.id != drill_id:
            source.fail()
            continue
        if record.status == "not_run" or record.at is None:
            continue  # nothing happened yet
        text = f"Drill {record.status}: {_DRILL_WORDS.get(drill_id, drill_id)}"
        if record.attested_by is not None:
            text += f" (attested by {record.attested_by})"
        note = _quoted(record.note)
        if note:
            text += f". {note}"
        refs: dict[str, str] = {"drill_id": drill_id}
        if record.evidence:
            refs["path"] = record.evidence[0].path
        source.entries.append(
            _entry(
                "drill",
                drill_id,
                record.at,
                kind=f"drill_{record.status}",
                summary=text,
                actor=None,
                refs=refs,
            )
        )
    return source


# -- the page --------------------------------------------------------------------------------


class BadQuery(ValueError):
    """The query names something the feed cannot answer exactly."""


def _encode_cursor(position: tuple[datetime, str]) -> str:
    at, item_id = position
    text = f"{_CURSOR_VERSION}|{at.astimezone(UTC).isoformat()}|{item_id}"
    return base64.urlsafe_b64encode(text.encode("ascii")).rstrip(b"=").decode("ascii")


def _decode_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        version, at_text, item_id = raw.decode("ascii").split("|", 2)
    except (binascii.Error, UnicodeDecodeError, ValueError) as error:
        raise BadQuery("cursor is not one this feed issued") from error
    at = _instant(at_text)
    if version != _CURSOR_VERSION or at is None or not _ITEM_ID.fullmatch(item_id):
        raise BadQuery("cursor is not one this feed issued")
    return at, item_id


def parse_query(params: QueryParams) -> tuple[ActivityQuery, tuple[datetime, str] | None]:
    """``limit`` (1..200, default 50), ``cursor`` (opaque, from this feed),
    ``source`` (repeatable). ``BadQuery`` names what is wrong."""
    for name in ("limit", "cursor"):
        if len(params.getlist(name)) > 1:
            raise BadQuery(f"{name} is given at most once")
    document: dict[str, Any] = {"sources": tuple(dict.fromkeys(params.getlist("source")))}
    limit = params.get("limit")
    if limit is not None:
        if not re.fullmatch(r"[0-9]{1,4}", limit):
            raise BadQuery("limit is a whole number from 1 to 200")
        document["limit"] = int(limit)
    if params.get("cursor") is not None:
        document["cursor"] = params.get("cursor")
    try:
        query = ActivityQuery.model_validate(document)
    except ValidationError as error:
        fields = sorted({str(e["loc"][0]) for e in error.errors() if e["loc"]})
        raise BadQuery(f"invalid {', '.join(fields) or 'query'}") from error
    position = None if query.cursor is None else _decode_cursor(query.cursor)
    return query, position


def activity_page(
    run_dir: Path,
    acceptance_dir: Path,
    now: datetime,
    query: ActivityQuery,
    position: tuple[datetime, str] | None = None,
) -> ActivityPage:
    """One page of ``trex-activity/1`` (see the module docstring)."""
    if run_dir.is_dir():
        sources = {
            "command": _commands(run_dir),
            "desk_event": _desk_events(run_dir),
            "audit": _audits(run_dir),
        }
    else:
        sources = {name: _Source(reason=_UNREADABLE) for name in ("command", "desk_event", "audit")}
    sources["drill"] = _drills(acceptance_dir)
    wanted = set(query.sources) or set(ACTIVITY_SOURCES)
    candidates = [
        entry
        for name in ACTIVITY_SOURCES
        if name in wanted
        for entry in sources[name].entries
        if position is None or entry.position < position
    ]
    top = heapq.nlargest(query.limit + 1, candidates, key=lambda entry: entry.position)
    page, has_more = top[: query.limit], len(top) > query.limit
    return ActivityPage.model_validate(
        {
            "now": now,
            "limit": query.limit,
            "items": [
                ActivityItem.model_validate(
                    {"id": entry.id, "at": entry.at, "source": entry.source, **entry.fields}
                )
                for entry in page
            ],
            "has_more": has_more,
            "next_cursor": _encode_cursor(page[-1].position) if has_more else None,
            "sources": [
                ActivitySourceStatus(
                    source=name,
                    status="ok" if sources[name].reason is None else "unavailable",
                    unavailable_reason=sources[name].reason,
                )
                for name in ACTIVITY_SOURCES
            ],
            "execution_enabled": False,
        }
    )


def default_acceptance_dir() -> Path:
    """``TREX_ACCEPTANCE_DIR`` (default ``~/.local/state/trex/acceptance``)."""
    return Path(os.environ.get(ACCEPTANCE_DIR_ENV, "") or ACCEPTANCE_DIR_DEFAULT).expanduser()


def attach(
    app: FastAPI,
    *,
    run_dir: Callable[[], Path],
    clock: Callable[[], datetime],
    acceptance_dir: Callable[[], Path] | None = None,
) -> None:
    """Mount ``GET /api/activity``; the directories resolve per request."""
    resolve_acceptance = acceptance_dir or default_acceptance_dir

    @app.get(ACTIVITY_PATH)
    def activity(request: Request) -> JSONResponse:
        try:
            query, position = parse_query(request.query_params)
        except BadQuery as error:
            return JSONResponse(
                {"error": "invalid_query", "detail": str(error)}, status_code=422, headers=NO_STORE
            )
        page = activity_page(run_dir(), resolve_acceptance(), clock(), query, position)
        return JSONResponse(model_document(page), headers=NO_STORE)
