"""Durable, idempotent intake of ``trex-command/1`` for the web backend (lane G).

Coordinator decision 2026-10-21: EVERY command kind, ``pause_entries`` and
``halt`` included, is applied by the desk owner process. The web only
authenticates, validates and queues. It writes ONLY under
``<desk run dir>/commands`` (layout C2.6): the command ``<key>.json``
(canonical bytes, written once) and its result ``<key>.result.json``.
Consuming a command (claim-and-rename) and moving its result on
(``applied``/``refused``/``failed``) are the owner's job; the web never
writes a kill file and never executes a broker action.

The one validity rule for every kind (``command_window_blocker``) lives
here so the web's intake and the owner's apply cannot disagree: a command
is valid while ``issued_at <= now + MAX_CLOCK_SKEW_S`` and
``now < expires_at``.

Protocol per idempotency key, under the exclusive ``commands/.lock``:

1. A stored result for the key: the same ``command_body_sha256`` answers
   ``duplicate`` naming the stored (latest) outcome; a different one is
   ``refused`` ``command_body_mismatch``. Nothing is rewritten.
2. Outside its validity window (``command_window_blocker``): ``refused``
   with that reason. The result is written, so the key is spent; the
   command is never queued.
3. The command is staged as ``.<key>.json.staged``, then the ``accepted``
   result is written atomically.
4. The staged file is renamed to ``<key>.json``. Only now can the owner see
   it, so the owner's later result can never be overwritten by ours. A
   crash between 3 and 4 leaves the staged file; the next submission of
   the key publishes it while the stored result is still ``accepted``
   (rename: the staged and the visible file never coexist, so a claimed
   command is never re-queued).
"""

from __future__ import annotations

import fcntl
import json
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final

from pydantic import JsonValue, ValidationError

from tree_options.trex.contracts import (
    AnyCommand,
    Blocker,
    CommandResult,
    Wave2RunDir,
    command_body_sha256,
    model_bytes,
    model_document,
    parse_command,
)
from tree_options.trex.contracts.commands import CommandKind, CommandStatus

__all__ = [
    "COMMAND_NOT_YET_VALID",
    "LOCK_NAME",
    "MAX_CLOCK_SKEW_S",
    "CommandStore",
    "CommandStoreUnavailable",
    "Submission",
    "command_window_blocker",
]

#: The advisory lock every submission holds (inside ``commands/``: the one
#: directory the web unit may write).
LOCK_NAME = ".lock"
#: A command written but not yet visible to the owner.
STAGED_SUFFIX = ".json.staged"
#: Neither a command nor a result is ever this large; a bigger file is not ours.
MAX_STORED_BYTES = 256 * 1024
#: The only stored outcome whose command the web itself still has to queue.
QUEUED_STATUS: Final = "accepted"
#: ``issued_at`` may lead the clock by this much (client clock skew).
MAX_CLOCK_SKEW_S: Final = 60
#: Refusal reason for a command issued further ahead than the skew allows.
#: A plain string until lane K adds it to ``contracts.vocabulary.Blocker``.
COMMAND_NOT_YET_VALID: Final = "command_not_yet_valid"
#: Bounds the scan of queued commands (``pending_kinds``).
MAX_PENDING_SCAN = 512


def command_window_blocker(command: AnyCommand, now: datetime) -> str | None:
    """Why ``command`` may not be taken at ``now`` (None: it may).

    THE validity rule for every kind, used by the web's intake and by the
    owner's apply alike: ``command_not_yet_valid`` while ``issued_at`` is
    more than ``MAX_CLOCK_SKEW_S`` ahead of ``now``; ``command_expired``
    from ``expires_at`` on. Both processes read the same host clock, and
    ``now`` only grows, so a command the web accepted can only stop being
    valid by expiring, never by the owner reading the window differently.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    if (command.issued_at - now).total_seconds() > MAX_CLOCK_SKEW_S:
        return COMMAND_NOT_YET_VALID
    if (now - command.expires_at).total_seconds() >= 0:
        return Blocker.COMMAND_EXPIRED.value
    return None


class CommandStoreUnavailable(RuntimeError):
    """The store cannot answer safely (an unreadable or foreign file)."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class Submission:
    """The answer to one submission; ``written`` = durable state changed."""

    result: CommandResult
    written: bool


def _fsync_dir(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_atomic(path: Path, payload: bytes) -> None:
    """Write ``payload`` to ``path`` through a fsynced temp file + rename."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        try:
            os.write(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _read_bounded(path: Path) -> bytes | None:
    """The file's bytes, None when absent; a foreign/huge file is unavailable."""
    try:
        if path.is_symlink() or path.stat().st_size > MAX_STORED_BYTES:
            raise CommandStoreUnavailable("command_store_foreign_file", path.name)
        return path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise CommandStoreUnavailable("command_store_unreadable", path.name) from error


class CommandStore:
    """The ``commands/`` directory of one desk run dir."""

    def __init__(self, run_dir: Path) -> None:
        self.layout = Wave2RunDir(run_dir)

    # -- reads ---------------------------------------------------------------

    def read_result(self, key: str) -> CommandResult | None:
        """The stored (latest) result for ``key``; None when there is none."""
        raw = _read_bounded(self.layout.command_result(key))
        if raw is None:
            return None
        try:
            return CommandResult.model_validate_json(raw)
        except ValidationError as error:
            raise CommandStoreUnavailable("command_result_invalid", key) from error

    def pending_kinds(self) -> frozenset[CommandKind]:
        """The kinds of the commands queued and not yet claimed by the owner
        (a visible ``<key>.json``). A file that cannot be read as a command
        makes the answer unknowable: ``CommandStoreUnavailable``."""
        folder = self.layout.commands()
        if not folder.is_dir():
            return frozenset()
        kinds: set[CommandKind] = set()
        names = sorted(
            p.name
            for p in folder.glob("*.json")
            if not p.name.endswith(".result.json") and not p.name.startswith(".")
        )
        if len(names) > MAX_PENDING_SCAN:
            raise CommandStoreUnavailable("command_queue_too_long", str(len(names)))
        for name in names:
            raw = _read_bounded(folder / name)
            if raw is None:
                continue  # claimed by the owner while we listed
            try:
                kinds.add(parse_command(_json_object(raw)).kind)
            except (ValueError, ValidationError) as error:
                raise CommandStoreUnavailable("queued_command_invalid", name) from error
        return frozenset(kinds)

    # -- the one write path ----------------------------------------------------

    def submit(self, command: AnyCommand, *, now: datetime) -> Submission:
        """Record ``command`` (see the module docstring for the protocol)."""
        key = command.idempotency_key
        body_sha = command_body_sha256(command)
        self.layout.commands().mkdir(parents=True, exist_ok=True)
        with self._locked():
            stored = self.read_result(key)
            if stored is not None:
                self._finish_publication(key, stored)
                if stored.command_body_sha256 != body_sha:
                    return Submission(
                        _result(
                            command,
                            body_sha,
                            now,
                            "refused",
                            reason=Blocker.COMMAND_BODY_MISMATCH,
                            detail="the idempotency key was used for a different command",
                        ),
                        written=False,
                    )
                return Submission(
                    _result(
                        command,
                        body_sha,
                        now,
                        "duplicate",
                        reason=Blocker.DUPLICATE_COMMAND,
                        result={
                            "first_status": stored.status,
                            "first_result": model_document(stored),
                        },
                    ),
                    written=False,
                )
            blocker = command_window_blocker(command, now)
            if blocker is not None:
                self._staged(key).unlink(missing_ok=True)
                document = model_document(command)
                refused = _result(
                    command,
                    body_sha,
                    now,
                    "refused",
                    reason=blocker,
                    detail=(
                        f"issued_at {document['issued_at']} is more than "
                        f"{MAX_CLOCK_SKEW_S}s ahead of the desk clock"
                        if blocker == COMMAND_NOT_YET_VALID
                        else f"expired at {document['expires_at']}"
                    ),
                )
                _write_atomic(self.layout.command_result(key), model_bytes(refused))
                _fsync_dir(self.layout.commands())
                return Submission(refused, written=True)
            return Submission(self._queue(command, body_sha, now), written=True)

    # -- internals -------------------------------------------------------------

    def _staged(self, key: str) -> Path:
        return self.layout.commands() / f".{key}{STAGED_SUFFIX}"

    @contextmanager
    def _locked(self) -> Iterator[None]:
        fd = os.open(self.layout.commands() / LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)  # closing the descriptor releases the flock

    def _queue(self, command: AnyCommand, body_sha: str, now: datetime) -> CommandResult:
        key = command.idempotency_key
        staged = self._staged(key)
        _write_atomic(staged, model_bytes(command))
        result = _result(command, body_sha, now, "accepted")
        _write_atomic(self.layout.command_result(key), model_bytes(result))
        os.replace(staged, self.layout.command(key))
        _fsync_dir(self.layout.commands())
        return result

    def _finish_publication(self, key: str, stored: CommandResult) -> None:
        """Publish a command a crash left staged after its result was written."""
        staged = self._staged(key)
        raw = _read_bounded(staged)
        if raw is None:
            return
        if stored.status != QUEUED_STATUS:
            # debris: the attempt never got its own result, or the owner has
            # already moved the result on (publishing would re-queue it)
            staged.unlink(missing_ok=True)
            return
        try:
            command = parse_command(_json_object(raw))
        except (ValueError, ValidationError) as error:
            raise CommandStoreUnavailable("staged_command_invalid", key) from error
        if command_body_sha256(command) != stored.command_body_sha256:
            raise CommandStoreUnavailable("staged_command_mismatch", key)
        os.replace(staged, self.layout.command(key))
        _fsync_dir(self.layout.commands())


def _json_object(raw: bytes) -> dict[str, Any]:
    document = json.loads(raw)
    if not isinstance(document, dict):
        raise ValueError("a command is a JSON object")
    return document


def _result(
    command: AnyCommand,
    body_sha: str,
    now: datetime,
    status: CommandStatus,
    *,
    reason: str | None = None,
    detail: str = "",
    result: dict[str, JsonValue] | None = None,
) -> CommandResult:
    return CommandResult.model_validate(
        {
            "idempotency_key": command.idempotency_key,
            "kind": command.kind,
            "command_body_sha256": body_sha,
            "status": status,
            "reason": None if reason is None else str(reason),
            "detail": detail,
            "result": result,
            "at": now,
        }
    )
