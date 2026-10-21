"""The desk owner's command consumer (``commands/<key>.json``, C2.2).

Coordinator ruling 2026-10-21: EVERY command, the kill-switch kinds
included, is applied by the desk owner process. The web backend
(``trex_web.command_store``) only authenticates, validates and QUEUES a
command (it writes ``<key>.json`` and an ``accepted`` result); this module
consumes it, applies it, and replaces the result with the outcome
(``trex-command-result/1``, the format the web reads back).

Per command, each loop iteration, in ``issued_at`` order:

1. **Claim** by a no-replace rename to ``<key>.claimed.json``: a command is
   processed exactly once, and a second ``<key>.json`` after a claim is set
   aside (``<key>.duplicate-<n>.json``) unprocessed.
2. **Validate**: the canonical ``trex-command/1`` document (kind and its
   args, by the contract), its key equals the file name, the web's intake
   result exists with the same kind and ``command_body_sha256`` (which binds
   the ``issued_by`` actor's principal and ingress), not expired
   (``command_expired``) and not issued in the future beyond the skew.
3. **Apply** by kind: the entry kinds through ``EntryFlows``
   (``trex.desk_entries``); ``pause_entries`` / ``resume_entries`` through
   ``trex.pause`` (the actor ``<ingress>:<sub>`` and the key in its audit);
   ``halt`` / ``flatten`` / ``resume_all`` as the existing kill files
   (``resume_all`` also lifts the pause; approvals are not revived);
   ``backup_state`` drops ``BACKUP-REQUEST`` (taken by the owner's next beat
   outside the session, ``trex.desk_backup``).
4. **Answer**: ``applied`` / ``refused`` / ``failed`` (a reason for the
   latter two) with the facts, written atomically under the web store's lock
   (``commands/.lock``), plus a ``command_applied`` / ``command_refused``
   desk event. Every desk answer carries ``result.answered_by =
   "trex-desk"``.

A claim without a desk answer (the owner died mid-command) is answered at
the next start by ``recover_interrupted``: ``failed``,
``owner_stopped_before_result``, never re-applied (an approval reports what
the supervised outbox proves about its order).
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import tempfile
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import ValidationError

from tree_options.trex import pause
from tree_options.trex.contracts.canonical import model_bytes
from tree_options.trex.contracts.commands import (
    Actor,
    AnyCommand,
    ApproveEntryCommand,
    BackupStateCommand,
    CommandResult,
    ComposeEntryCommand,
    FlattenCommand,
    HaltCommand,
    PauseEntriesCommand,
    RejectEntryCommand,
    ResumeAllCommand,
    ResumeEntriesCommand,
    command_body_sha256,
    parse_command,
)
from tree_options.trex.contracts.layout import FLATTEN, HALT, Wave2RunDir
from tree_options.trex.contracts.vocabulary import Blocker, DeskEvent
from tree_options.trex.desk_backup import BACKUP_REQUEST

log = logging.getLogger("trex.desk_commands")

__all__ = [
    "ANSWERED_BY",
    "CLAIMED_SUFFIX",
    "COMMAND_NOT_YET_VALID",
    "CommandQueue",
    "EntryFlows",
    "Outcome",
    "actor_label",
    "applied",
    "failed",
    "refused",
]

#: every desk answer names its writer (the web's own results never do)
ANSWERED_BY = "trex-desk"
CLAIMED_SUFFIX = ".claimed.json"
#: the web store's advisory lock (``trex_web.command_store.LOCK_NAME``)
STORE_LOCK = ".lock"
MAX_COMMAND_BYTES = 64 * 1024
#: ``issued_at`` may lead the desk clock by this much (the web's own bound)
MAX_FUTURE_SKEW_S = 60
COMMAND_NOT_YET_VALID = "command_not_yet_valid"
INTERRUPTED = "owner_stopped_before_result"
#: errors that mean the gateway went: never answered here, the loop exits 6
_SESSION_ERRORS: tuple[type[BaseException], ...] = (ConnectionError, TimeoutError)
_KEY = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_QUEUED = re.compile(rf"^(?P<key>{_KEY})\.json$")
_CLAIMED = re.compile(rf"^(?P<key>{_KEY}){re.escape(CLAIMED_SUFFIX)}$")
_INTAKE_STATUSES = frozenset({"accepted", "applied"})
_KILL_SWITCH_KINDS = frozenset(
    {"pause_entries", "resume_entries", "halt", "flatten", "resume_all", "backup_state"}
)

EventFn = Callable[..., None]
Status = Literal["applied", "refused", "failed"]


@dataclass(frozen=True)
class Outcome:
    """What applying one command did (the result's status, reason, facts)."""

    status: Status
    reason: str | None = None
    detail: str = ""
    facts: Mapping[str, Any] = field(default_factory=dict)


def applied(detail: str = "", **facts: Any) -> Outcome:
    return Outcome("applied", None, detail, facts)


def refused(reason: str, detail: str = "", **facts: Any) -> Outcome:
    return Outcome("refused", str(reason), detail, facts)


def failed(reason: str, detail: str = "", **facts: Any) -> Outcome:
    return Outcome("failed", str(reason), detail, facts)


class EntryFlows(Protocol):
    """The entry kinds (``trex.desk_entries.OwnerEntries`` implements it)."""

    def compose(self, command: ComposeEntryCommand, now: datetime) -> Outcome: ...

    def approve(self, command: ApproveEntryCommand, now: datetime) -> Outcome: ...

    def reject(self, command: RejectEntryCommand, now: datetime) -> Outcome: ...

    def interrupted(self, command: AnyCommand) -> dict[str, Any]: ...


def actor_label(actor: Actor) -> str:
    """The audit actor of a command: ``<ingress>:<sub>`` (as lane A's web
    apply and the pause audit spell it)."""
    return f"{actor.ingress}:{actor.sub}"


def rename_noreplace(src: Path, dst: Path) -> None:
    """``supervised_desk.rename_noreplace`` (the inbox's evidence-safe
    rename), imported at call time: that module wires this one."""
    from tree_options.trex.supervised_desk import rename_noreplace as rename

    rename(src, dst)


def _jsonable(facts: Mapping[str, Any]) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(json.dumps(dict(facts), default=str, sort_keys=True))
    return loaded


def _reject_constant(name: str) -> None:
    raise ValueError(f"non-finite number {name}")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            raise ValueError(f"duplicate key {key!r}")
        document[key] = value
    return document


def _strict_json(raw: bytes) -> Any:
    """JSON with no duplicate keys and no NaN/Infinity (the web's rule)."""
    return json.loads(
        raw.decode("utf-8"), parse_constant=_reject_constant, object_pairs_hook=_unique_object
    )


def _write_atomic(path: Path, payload: bytes) -> None:
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


def _kill_record(command: AnyCommand, now: datetime) -> bytes:
    reason = getattr(command.args, "reason", "")
    return (
        json.dumps(
            {
                "placed_by": actor_label(command.issued_by),
                "reason": reason,
                "command": command.idempotency_key,
                "at": now.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            },
            sort_keys=True,
        )
        + "\n"
    ).encode()


def _place(path: Path, payload: bytes) -> bool:
    """Place a kill/request file unless one stands (True: placed now)."""
    if os.path.lexists(path):
        return False
    _write_atomic(path, payload)
    return True


class CommandQueue:
    """``commands/`` of one desk run dir, consumed by the owner."""

    def __init__(
        self,
        run_dir: Path,
        *,
        flows: EntryFlows,
        clock: Callable[[], datetime],
        owner_epoch: str,
        event: EventFn | None = None,
        connected: Callable[[], bool] = lambda: True,
    ) -> None:
        self.layout = Wave2RunDir(Path(run_dir))
        self.flows = flows
        self.clock = clock
        self.owner_epoch = owner_epoch
        self.event = event
        self.connected = connected

    # -- paths --------------------------------------------------------------------

    @property
    def directory(self) -> Path:
        return self.layout.commands()

    def claimed(self, key: str) -> Path:
        return self.directory / f"{key}{CLAIMED_SUFFIX}"

    # -- the loop step --------------------------------------------------------------

    def process(self) -> list[CommandResult]:
        """Claim, validate, apply and answer every queued command once."""
        answered: list[CommandResult] = []
        for key, path in self._queued():
            result = self._process(key, path)
            if result is not None:
                answered.append(result)
        return answered

    def _queued(self) -> list[tuple[str, Path]]:
        """Queued commands, oldest ``issued_at`` first (an unreadable one
        first of all: it is answered, refused, at once)."""
        if not self.directory.is_dir():
            return []
        found: list[tuple[str, str, Path]] = []
        for path in self.directory.iterdir():
            match = _QUEUED.match(path.name)
            if match is None:
                continue
            issued = ""
            try:
                doc = _strict_json(path.read_bytes()[: MAX_COMMAND_BYTES + 1])
                issued = str(doc.get("issued_at", "")) if isinstance(doc, dict) else ""
            except (OSError, ValueError):
                pass
            found.append((issued, match["key"], path))
        return [(key, path) for _, key, path in sorted(found)]

    def _process(self, key: str, path: Path) -> CommandResult | None:
        claimed = self.claimed(key)
        if os.path.lexists(claimed):
            self._set_aside(key, path)
            return None
        try:
            rename_noreplace(path, claimed)  # the claim: processed exactly once
        except FileNotFoundError:
            return None
        except FileExistsError:
            self._set_aside(key, path)
            return None
        now = self.clock()
        intake = self._intake(key)
        if intake is not None and _answered_by_desk(intake):
            # a desk answer already stands for this key: never replaced
            self._emit(
                DeskEvent.COMMAND_REFUSED.value,
                command=key,
                reason=Blocker.DUPLICATE_COMMAND.value,
                detail="already answered by the desk",
            )
            return None
        command, body_sha, kind, outcome = self._validate(key, claimed, now, intake)
        if kind is None or body_sha is None:
            self._emit(DeskEvent.COMMAND_REFUSED.value, command=key, reason="command_invalid")
            return None
        if outcome is None:
            assert command is not None
            outcome = self._apply(command, now)
        return self._answer(key, kind, body_sha, outcome, now, command)

    def _set_aside(self, key: str, path: Path) -> None:
        n = 0
        while True:
            n += 1
            target = path.with_name(f"{key}.duplicate-{n}.json")
            try:
                rename_noreplace(path, target)
            except FileExistsError:
                continue
            except FileNotFoundError:
                return
            break
        self._emit(
            DeskEvent.COMMAND_REFUSED.value,
            command=key,
            reason=Blocker.DUPLICATE_COMMAND.value,
            moved_to=target.name,
        )

    # -- validation -----------------------------------------------------------------

    def _intake(self, key: str) -> CommandResult | None:
        path = self.layout.command_result(key)
        try:
            if path.is_symlink():
                return None
            return CommandResult.model_validate_json(path.read_bytes())
        except (OSError, ValidationError, ValueError):
            return None

    def _validate(
        self, key: str, claimed: Path, now: datetime, intake: CommandResult | None
    ) -> tuple[AnyCommand | None, str | None, str | None, Outcome | None]:
        """(command, body hash, kind, refusal); kind None = no answer can
        be written (neither the command nor its intake record reads)."""
        fallback_kind = None if intake is None else intake.kind
        fallback_sha = None if intake is None else intake.command_body_sha256
        try:
            if claimed.is_symlink():
                raise ValueError("a command file is never a link")
            raw = claimed.read_bytes()
            if len(raw) > MAX_COMMAND_BYTES:
                raise ValueError("command too large")
            document = _strict_json(raw)
            if not isinstance(document, dict):
                raise ValueError("a command is a JSON object")
            command = parse_command(document)
        except (OSError, ValueError) as error:
            return (
                None,
                fallback_sha,
                fallback_kind,
                refused("command_invalid", type(error).__name__),
            )
        body_sha = command_body_sha256(command)
        # an answer keeps the identity the web ACCEPTED under this key, so a
        # browser retry of that command still reads as its duplicate
        kind = command.kind if intake is None else intake.kind
        sha = body_sha if intake is None else intake.command_body_sha256
        refusal: Outcome | None = None
        if command.idempotency_key != key:
            refusal = refused("command_key_mismatch")
        elif intake is None:
            refusal = refused(Blocker.COMMAND_UNAUTHORIZED, "no_intake_record")
        elif intake.kind != command.kind or intake.command_body_sha256 != body_sha:
            refusal = refused(
                Blocker.COMMAND_BODY_MISMATCH, "the queued command is not the accepted one"
            )
        elif intake.status not in _INTAKE_STATUSES:
            refusal = refused(Blocker.COMMAND_UNAUTHORIZED, f"intake_status:{intake.status}")
        elif (now - command.expires_at).total_seconds() >= 0:
            refusal = refused(Blocker.COMMAND_EXPIRED)
        elif (command.issued_at - now).total_seconds() > MAX_FUTURE_SKEW_S:
            refusal = refused(COMMAND_NOT_YET_VALID)
        return command, sha, kind, refusal

    # -- apply ------------------------------------------------------------------------

    def _apply(self, command: AnyCommand, now: datetime) -> Outcome:
        """One command's effect. A lost session propagates (the claim stays
        unanswered and is reported at the next start, never re-applied);
        any other error is a ``failed`` answer, never a dead owner loop."""
        try:
            return self._dispatch(command, now)
        except Exception as error:
            if isinstance(error, _SESSION_ERRORS) or not self.connected():
                raise
            log.exception("command %s (%s) failed", command.idempotency_key, command.kind)
            return failed("apply_error", type(error).__name__)

    def _dispatch(self, command: AnyCommand, now: datetime) -> Outcome:
        root = self.layout.root
        actor = actor_label(command.issued_by)
        if isinstance(command, ComposeEntryCommand):
            return self.flows.compose(command, now)
        if isinstance(command, ApproveEntryCommand):
            return self.flows.approve(command, now)
        if isinstance(command, RejectEntryCommand):
            return self.flows.reject(command, now)
        if isinstance(command, PauseEntriesCommand | ResumeEntriesCommand):
            flip = (
                pause.pause_entries
                if isinstance(command, PauseEntriesCommand)
                else pause.resume_entries
            )
            facts = flip(
                root,
                actor=actor,
                reason=command.args.reason,
                now=now,
                command_key=command.idempotency_key,
            )
            event = (
                DeskEvent.ENTRIES_PAUSED
                if isinstance(command, PauseEntriesCommand)
                else DeskEvent.ENTRIES_RESUMED
            )
            self._emit(event.value, actor=actor, command=command.idempotency_key, **facts)
            return applied(**facts)
        if isinstance(command, HaltCommand):
            placed = _place(root / HALT, _kill_record(command, now))
            return applied(kill_file=HALT, changed=placed, halt=True)
        if isinstance(command, FlattenCommand):
            placed = _place(root / FLATTEN, _kill_record(command, now))
            halted = os.path.lexists(root / HALT)
            detail = "HALT is present: exits wait until it is removed" if halted else ""
            return applied(detail, kill_file=FLATTEN, changed=placed, halt_present=halted)
        if isinstance(command, ResumeAllCommand):
            removed = [name for name in (HALT, FLATTEN) if os.path.lexists(root / name)]
            for name in removed:
                (root / name).unlink(missing_ok=True)
            entries = pause.resume_entries(
                root,
                actor=actor,
                reason=command.args.reason,
                now=now,
                command_key=command.idempotency_key,
            )
            return applied(
                removed=removed,
                entries_paused=entries["entries_paused"],
                pause_lifted=entries["changed"],
                approvals_revived=False,
            )
        if isinstance(command, BackupStateCommand):
            record = {
                "label": command.args.label,
                "requested_by": actor,
                "command": command.idempotency_key,
                "at": now.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            }
            placed = _place(
                root / BACKUP_REQUEST, (json.dumps(record, sort_keys=True) + "\n").encode()
            )
            return applied(
                "taken by the owner's next beat outside 09:30-16:15 ET",
                backup_request=BACKUP_REQUEST,
                changed=placed,
                label=command.args.label,
            )
        raise ValueError(f"unknown command kind {command.kind}")  # pragma: no cover

    # -- answers ------------------------------------------------------------------------

    def _answer(
        self,
        key: str,
        kind: str,
        body_sha: str,
        outcome: Outcome,
        now: datetime,
        command: AnyCommand | None,
    ) -> CommandResult:
        facts = _jsonable(
            {**outcome.facts, "answered_by": ANSWERED_BY, "owner_epoch": self.owner_epoch}
        )
        result = CommandResult.model_validate(
            {
                "idempotency_key": key,
                "kind": kind,
                "command_body_sha256": body_sha,
                "status": outcome.status,
                "reason": outcome.reason,
                "detail": outcome.detail,
                "result": facts,
                "at": now,
            }
        )
        with self._store_lock():
            _write_atomic(self.layout.command_result(key), model_bytes(result))
        name = (
            DeskEvent.COMMAND_APPLIED if outcome.status == "applied" else DeskEvent.COMMAND_REFUSED
        )
        self._emit(
            name.value,
            command=key,
            kind=kind,
            status=outcome.status,
            reason=outcome.reason,
            actor=None if command is None else actor_label(command.issued_by),
        )
        return result

    @contextmanager
    def _store_lock(self) -> Iterator[None]:
        """The web store's lock: our answer never interleaves its intake."""
        self.directory.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.directory / STORE_LOCK, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def recover_interrupted(self) -> list[CommandResult]:
        """Answer every claim a previous owner never answered: ``failed``
        (``owner_stopped_before_result``), never re-applied."""
        answered: list[CommandResult] = []
        if not self.directory.is_dir():
            return answered
        now = self.clock()
        for path in sorted(self.directory.iterdir()):
            match = _CLAIMED.match(path.name)
            if match is None:
                continue
            key = match["key"]
            intake = self._intake(key)
            if intake is not None and _answered_by_desk(intake):
                continue
            command: AnyCommand | None
            try:
                command = parse_command(_strict_json(path.read_bytes()[:MAX_COMMAND_BYTES]))
            except (OSError, ValueError):
                command = None
            if command is not None:
                kind, body_sha = command.kind, command_body_sha256(command)
            elif intake is not None:
                kind, body_sha = intake.kind, intake.command_body_sha256
            else:
                continue  # nothing reads: no answer can be written
            facts: dict[str, Any] = {}
            if command is not None and command.kind not in _KILL_SWITCH_KINDS:
                try:
                    facts = self.flows.interrupted(command)
                except Exception as error:  # reporting only
                    facts = {"report_error": type(error).__name__}
            outcome = failed(INTERRUPTED, "the command is never re-applied", **facts)
            answered.append(self._answer(key, kind, body_sha, outcome, now, command))
        return answered

    def _emit(self, name: str, **payload: Any) -> None:
        if self.event is None:
            return
        try:
            self.event(name, **payload)
        except Exception:  # a record failure never breaks the owner loop
            log.exception("command event %s not recorded", name)


def _answered_by_desk(result: CommandResult) -> bool:
    return isinstance(result.result, dict) and result.result.get("answered_by") == ANSWERED_BY
