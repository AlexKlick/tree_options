"""Durable, idempotent intake of ``trex-command/1`` for the web backend (lane G).

The web writes ONLY under ``<desk run dir>/commands`` (layout C2.6): the
command ``<key>.json`` (canonical bytes, written once) and its result
``<key>.result.json``. Consuming a command (claim-and-rename) and moving
its result on (``applied``/``refused``/``failed``) are the desk owner's
job; the web never executes a broker action.

Protocol per idempotency key, under the exclusive ``commands/.lock``:

1. A stored result for the key: the same ``command_body_sha256`` answers
   ``duplicate`` naming the stored (latest) outcome; a different one is
   ``refused`` ``command_body_mismatch``. Nothing is rewritten.
2. Expired (``now >= expires_at``): ``refused`` ``command_expired``. The
   result is written, so the key is spent; the command is never queued.
3. The command is staged as ``.<key>.json.staged``; a direct safety apply
   (``pause_entries``/``halt``, contract default 10) runs if the caller
   passed one; then the result (``applied``, or ``accepted``) is written
   atomically.
4. The staged file is renamed to ``<key>.json``. Only now can the owner see
   it, so the owner's later result can never be overwritten by ours. A
   crash between 3 and 4 leaves the staged file; the next submission of
   the key publishes it (rename: the staged and the visible file never
   coexist, so a claimed command is never re-queued).

A direct apply that fails with ``OSError`` (the unit's sandbox lets the
web write only ``commands/``) degrades to ``accepted``: the command is
queued and the owner applies it.
"""

from __future__ import annotations

import fcntl
import json
import os
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

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
from tree_options.trex.contracts.commands import CommandStatus

__all__ = [
    "LOCK_NAME",
    "CommandStore",
    "CommandStoreUnavailable",
    "DirectApply",
    "Submission",
]

#: The advisory lock every submission holds (inside ``commands/``: the one
#: directory the web unit may write).
LOCK_NAME = ".lock"
#: A command written but not yet visible to the owner.
STAGED_SUFFIX = ".json.staged"
#: Neither a command nor a result is ever this large; a bigger file is not ours.
MAX_STORED_BYTES = 256 * 1024
#: Outcomes whose command is (or must be) queued for the owner.
QUEUED_STATUSES = frozenset({"accepted", "applied"})

#: Applies a command's effect in-process and returns its facts; raises
#: ``OSError`` when it cannot (the command is then only queued).
DirectApply = Callable[[AnyCommand], dict[str, JsonValue]]


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

    # -- the one write path ----------------------------------------------------

    def submit(
        self, command: AnyCommand, *, now: datetime, direct_apply: DirectApply | None = None
    ) -> Submission:
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
            if now >= command.expires_at:
                self._staged(key).unlink(missing_ok=True)
                refused = _result(
                    command,
                    body_sha,
                    now,
                    "refused",
                    reason=Blocker.COMMAND_EXPIRED,
                    detail=f"expired at {model_document(command)['expires_at']}",
                )
                _write_atomic(self.layout.command_result(key), model_bytes(refused))
                _fsync_dir(self.layout.commands())
                return Submission(refused, written=True)
            return Submission(self._queue(command, body_sha, now, direct_apply), written=True)

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

    def _queue(
        self,
        command: AnyCommand,
        body_sha: str,
        now: datetime,
        direct_apply: DirectApply | None,
    ) -> CommandResult:
        key = command.idempotency_key
        staged = self._staged(key)
        _write_atomic(staged, model_bytes(command))
        status: CommandStatus = "accepted"
        facts: dict[str, JsonValue] | None = None
        detail = ""
        if direct_apply is not None:
            try:
                facts = direct_apply(command)
                status = "applied"
            except OSError as error:
                why = error.strerror or type(error).__name__
                detail = f"direct apply unavailable ({why}); queued for the desk owner"
        result = _result(command, body_sha, now, status, detail=detail, result=facts)
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
        if stored.status not in QUEUED_STATUSES:
            staged.unlink(missing_ok=True)  # debris of an attempt that never got a result
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
