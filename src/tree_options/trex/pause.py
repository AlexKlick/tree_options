"""Entries-only pause: the ``PAUSE_ENTRIES`` file in the desk run dir.

A user-facing stop that refuses NEW entries only. Exits, fills, exit
repricing and reconciliation continue: the desk runtime's exit lane reads
``HALT`` and ``FLATTEN`` (``desk_runtime._tick``) and never this file. That
is the difference from ``HALT``, which also gates exit orders.

- ``pause_entries`` writes the file atomically (temp file, fsync, rename)
  with who, why and when; ``resume_entries`` removes it. Neither ever
  creates, reads for a decision, or removes ``HALT`` / ``FLATTEN``.
- Every request appends one line to ``pause-entries.jsonl`` (append-only),
  including a repeated request that changed nothing (``changed: false``).
- Pause is the safe direction: the file is written first, so a failing
  audit never delays it (the error still surfaces). Resume is the unsafe
  direction: an unauditable resume never stands, so the file is restored
  byte-identical when its audit line cannot be written.
- ``is_paused`` uses ``lexists``: a dangling symlink still pauses (fail
  closed, as the desk treats its other kill files).
- A resume never lifts a newer stop (W2c refix). A pause placed by a queued
  command records the command's ``issued_at``; a pause command applied
  over a standing pause re-stamps it when newer (``restamp_stop``: the file
  stands throughout). A queued resume passes its own ``issued_at`` and
  leaves a pause recorded as issued after it (``superseded_by_newer_stop``,
  audited). A pause without a recorded issue time (the terminal's, an
  older record) and the terminal's resume (no issue time) behave as before.
  ``stop_issued_at`` / ``issued_after`` / ``restamp_stop`` serve the desk's
  ``HALT`` / ``FLATTEN`` records the same way (``trex.desk_commands``); this
  module's own functions never touch those files.

No broker contact; safe for any process (contracts default 10: a pause is
the safety direction and may be applied by whichever process receives it).
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tree_options.trex.contracts.layout import HALT, Wave2RunDir
from tree_options.trex.contracts.vocabulary import Blocker, DeskEvent

__all__ = [
    "HALTED",
    "MAX_ACTOR_CHARS",
    "MAX_REASON_CHARS",
    "PAUSE_AUDIT_FILE",
    "PAUSE_AUDIT_SCHEMA",
    "PAUSE_RECORD_SCHEMA",
    "SUPERSEDED_BY_NEWER_STOP",
    "exit_blockers",
    "is_paused",
    "issued_after",
    "pause_blockers",
    "pause_entries",
    "pause_file",
    "pause_record",
    "restamp_stop",
    "resume_entries",
    "stop_issued_at",
]

PAUSE_RECORD_SCHEMA = "trex-pause-entries/1"
PAUSE_AUDIT_SCHEMA = "trex-pause-audit/1"
#: append-only audit of every pause/resume request, beside the pause file
PAUSE_AUDIT_FILE = "pause-entries.jsonl"
#: the blocker string the desk's admission already uses for a HALT file
HALTED = "halt"
#: the command contract's ``Reason`` bound (``contracts.canonical.Reason``)
MAX_REASON_CHARS = 500
MAX_ACTOR_CHARS = 200
#: the fact a resume reports when it left a stop issued after it in place
SUPERSEDED_BY_NEWER_STOP = "superseded_by_newer_stop"
_MAX_RECORD_BYTES = 64 * 1024


def pause_file(run_dir: Path) -> Path:
    return Wave2RunDir(Path(run_dir)).pause_entries()


def is_paused(run_dir: Path) -> bool:
    """True while ``PAUSE_ENTRIES`` exists (a dangling symlink included)."""
    return os.path.lexists(pause_file(run_dir))


def pause_record(run_dir: Path) -> dict[str, Any] | None:
    """The standing pause (actor, reason, at), ``None`` when not paused,
    ``{"unreadable": True}`` when paused by a file this module did not write."""
    path = pause_file(run_dir)
    if not os.path.lexists(path):
        return None
    doc = _read_object(path)
    if doc is None or doc.get("schema") != PAUSE_RECORD_SCHEMA:
        return {"unreadable": True}
    return doc


def stop_issued_at(path: Path) -> datetime | None:
    """The issue time of the newest stop command a standing stop file
    (``PAUSE_ENTRIES``, ``HALT``, ``FLATTEN``) records; ``None`` when it
    records none (the terminal's, an older record, a link, unreadable)."""
    return _issued(_read_object(Path(path)))


def issued_after(path: Path, issued_at: datetime) -> bool:
    """True when the standing stop file at ``path`` records a stop command
    issued after ``issued_at``: a resume issued at ``issued_at`` leaves it
    (on a tie the resume, the later applied, lifts it)."""
    recorded = stop_issued_at(path)
    return recorded is not None and recorded > issued_at


def restamp_stop(path: Path, issued_at: datetime, fresh: Mapping[str, Any]) -> bool:
    """A stop command applied while its stop file stands: keep the file at
    the newest stop command's issue time, so a resume issued between the
    two never lifts it. The file stands throughout (an atomic replace); a
    record of an earlier stop keeps its fields (who placed it, why, when)
    with the newer ``issued_at``; a regular file that is not a JSON object
    (the terminal's empty ``touch``) becomes ``fresh``; a symlink or
    anything not a regular file stands as it is. True when re-stamped."""
    path = Path(path)
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(mode):
        return False
    doc = _read_object(path)
    recorded = _issued(doc)
    if recorded is not None and recorded >= issued_at:
        return False
    record = dict(fresh) if doc is None else doc
    _write_atomic(path, _encode({**record, "issued_at": _utc(issued_at)}))
    return True


def pause_blockers(run_dir: Path) -> tuple[str, ...]:
    """``entries_paused`` while paused; nothing otherwise."""
    return (Blocker.ENTRIES_PAUSED.value,) if is_paused(run_dir) else ()


def exit_blockers(run_dir: Path) -> tuple[str, ...]:
    """What gates EXIT orders, by the desk runtime's rule: ``HALT`` only.

    ``PAUSE_ENTRIES`` never appears here (and ``FLATTEN`` forces exits, it
    never blocks them). A display or a test can ask this without a runtime.
    """
    return (HALTED,) if os.path.lexists(Path(run_dir) / HALT) else ()


def pause_entries(
    run_dir: Path,
    *,
    actor: str,
    reason: str = "",
    now: datetime,
    command_key: str | None = None,
    issued_at: datetime | None = None,
) -> dict[str, Any]:
    """Place the entries-only pause. Idempotent: a standing pause is kept
    as placed (its actor and time), and the request is still audited.

    ``issued_at`` (a queued pause command's issue time) is recorded, and a
    standing pause is re-stamped with it when newer (``restamp_stop``)."""
    root = Path(run_dir)
    at = _validated(actor, reason, now, command_key, issued_at)
    changed = not is_paused(root)
    record: dict[str, Any] = {
        "schema": PAUSE_RECORD_SCHEMA,
        "actor": actor,
        "reason": reason,
        "at": at,
    }
    if issued_at is not None:
        record["issued_at"] = _utc(issued_at)
    if changed:
        _write_atomic(pause_file(root), _encode(record))
    elif issued_at is not None:
        restamp_stop(pause_file(root), issued_at, record)
    line = _audit_line(DeskEvent.ENTRIES_PAUSED, actor, reason, at, changed, command_key)
    _append_audit(root, line)
    return {"entries_paused": True, "changed": changed}


def resume_entries(
    run_dir: Path,
    *,
    actor: str,
    reason: str = "",
    now: datetime,
    command_key: str | None = None,
    issued_at: datetime | None = None,
) -> dict[str, Any]:
    """Lift the entries-only pause (``HALT`` / ``FLATTEN`` untouched).

    ``issued_at`` (a queued resume command's issue time): a pause recorded
    as issued after it stays, audited, and the answer says
    ``superseded_by_newer_stop``. Without it (the terminal) any pause lifts."""
    root = Path(run_dir)
    at = _validated(actor, reason, now, command_key, issued_at)
    path = pause_file(root)
    if issued_at is not None and is_paused(root) and issued_after(path, issued_at):
        line = _audit_line(DeskEvent.ENTRIES_RESUMED, actor, reason, at, False, command_key)
        _append_audit(root, {**line, SUPERSEDED_BY_NEWER_STOP: True})
        return {"entries_paused": True, "changed": False, SUPERSEDED_BY_NEWER_STOP: True}
    changed = is_paused(root)
    saved: bytes | None = None
    if changed:
        if not path.is_symlink():
            try:
                saved = path.read_bytes()
            except OSError:
                saved = None
        path.unlink(missing_ok=True)
    try:
        _append_audit(
            root, _audit_line(DeskEvent.ENTRIES_RESUMED, actor, reason, at, changed, command_key)
        )
    except BaseException:
        if changed:
            # an unaudited resume never stands: put the pause back as it was
            restored = saved if saved is not None else _encode(_restored_record(at))
            _write_atomic(path, restored)
        raise
    return {"entries_paused": False, "changed": changed}


# -- internals -------------------------------------------------------------


def _validated(
    actor: str,
    reason: str,
    now: datetime,
    command_key: str | None,
    issued_at: datetime | None = None,
) -> str:
    if issued_at is not None and (issued_at.tzinfo is None or issued_at.utcoffset() is None):
        raise ValueError("issued_at must be timezone-aware")
    if not isinstance(actor, str) or not actor.strip() or len(actor) > MAX_ACTOR_CHARS:
        raise ValueError("actor must be a non-empty label")
    if any(not ch.isprintable() for ch in actor):
        raise ValueError("actor must be printable")
    if not isinstance(reason, str) or len(reason) > MAX_REASON_CHARS:
        raise ValueError(f"reason must be at most {MAX_REASON_CHARS} characters")
    if command_key is not None and (not isinstance(command_key, str) or len(command_key) > 96):
        raise ValueError("command_key must be a short string")
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return _utc(now)


def _utc(instant: datetime) -> str:
    return instant.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _read_object(path: Path) -> dict[str, Any] | None:
    """A stop file's JSON object (never through a link, bounded); ``None``
    when it is absent, a link, too large, or not a JSON object."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            raw = stream.read(_MAX_RECORD_BYTES + 1)
        doc = json.loads(raw) if len(raw) <= _MAX_RECORD_BYTES else None
    except (OSError, ValueError, RecursionError):
        return None
    return doc if isinstance(doc, dict) else None


def _issued(doc: Mapping[str, Any] | None) -> datetime | None:
    """A record's ``issued_at`` (timezone-aware), else ``None``."""
    text = None if doc is None else doc.get("issued_at")
    if not isinstance(text, str):
        return None
    try:
        instant = datetime.fromisoformat(text)
    except ValueError:
        return None
    return instant if instant.utcoffset() is not None else None


def _restored_record(at: str) -> dict[str, Any]:
    return {
        "schema": PAUSE_RECORD_SCHEMA,
        "actor": "pause-restore",
        "reason": "resume could not be audited",
        "at": at,
    }


def _audit_line(
    event: DeskEvent,
    actor: str,
    reason: str,
    at: str,
    changed: bool,
    command_key: str | None,
) -> dict[str, Any]:
    return {
        "schema": PAUSE_AUDIT_SCHEMA,
        "event": event.value,
        "actor": actor,
        "reason": reason,
        "at": at,
        "changed": changed,
        "command": command_key,
    }


def _encode(doc: dict[str, Any]) -> bytes:
    return json.dumps(doc, sort_keys=True).encode()


def _write_atomic(path: Path, payload: bytes) -> None:
    """Temp file in the same dir, fsync, rename: readers see all or nothing."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _append_audit(run_dir: Path, line: dict[str, Any]) -> None:
    """One ``O_APPEND`` write per line: concurrent writers never interleave."""
    run_dir.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(line, sort_keys=True) + "\n").encode()
    fd = os.open(run_dir / PAUSE_AUDIT_FILE, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
