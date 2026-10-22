"""The desk owner's command consumer (``commands/<key>.json``, C2.2).

Coordinator ruling 2026-10-21: EVERY command, the kill-switch kinds
included, is applied by the desk owner process. The web backend
(``trex_web.command_store``) only authenticates, validates and QUEUES a
command (it writes ``<key>.json`` and an ``accepted`` result); this module
consumes it, applies it, and replaces the result with the outcome
(``trex-command-result/1``, the format the web reads back).

Each loop iteration takes the queued commands in this order: every
kill-switch kind (``FILE_KINDS``: ``halt``, ``flatten``, ``pause_entries``,
``resume_all``, ``resume_entries``, ``backup_state``) first, in
``issued_at`` order among themselves (on a tie the resume before the stop),
then the entry kinds in ``issued_at`` order. Per command:

1. **Claim** by a no-replace rename to ``<key>.claimed.json``: a command is
   processed exactly once, and a second ``<key>.json`` after a claim (in
   hand or archived) is set aside (``done/<key>.duplicate-<n>.json``)
   unprocessed.
2. **Validate**: the canonical ``trex-command/1`` document (kind and its
   args, by the contract), its key equals the file name, the web's intake
   result exists with the same kind and ``command_body_sha256`` (which binds
   the ``issued_by`` actor's principal and ingress), not expired
   (``command_expired``) and not issued in the future beyond the skew. The
   safety kinds (``halt``, ``flatten``, ``pause_entries``) never expire
   in the queue once the web accepted them inside their window: their
   answer then says ``applied_after_expiry`` (W2b review P1: a gateway
   outage that keeps the owner away must not drop an accepted HALT).
3. **Apply** by kind: the entry kinds through ``EntryFlows``
   (``trex.desk_entries``); ``pause_entries`` / ``resume_entries`` through
   ``trex.pause`` (the actor ``<ingress>:<sub>`` and the key in its audit);
   ``halt`` / ``flatten`` / ``resume_all`` as the existing kill files
   (``resume_all`` also lifts the pause; approvals are not revived);
   ``backup_state`` drops ``BACKUP-REQUEST`` (taken by the owner's next beat
   outside the session, ``trex.desk_backup``); ``seal_registration`` seals a
   pilot registration for the verified actor
   (``research.pilot.seal.apply_seal_command``: the actor must be in
   ``TREX_OWNER_EMAILS``; written once under ``TREX_PILOT_DIR``).
4. **Answer**: ``applied`` / ``refused`` / ``failed`` (a reason for the
   latter two) with the facts, written atomically under the web store's lock
   (``commands/.lock``), plus a ``command_applied`` / ``command_refused``
   desk event. Every desk answer carries ``result.answered_by =
   "trex-desk"``.
5. **Archive**: the claim moves to ``commands/done/`` (no-replace); the
   answer stays ``commands/<key>.result.json`` where the web reads it. A
   claim or duplicate an earlier owner left beside an answered command is
   archived at the next start (``recover_interrupted``).

The kill-switch kinds are file operations (``FILE_KINDS``): with the
gateway down the owner still applies them (``process(kinds=FILE_KINDS)``:
on its way out with exit 6, before it connects, and at ``start`` before
its first tick); an entry kind stays queued for a live session.

A claim without a desk answer (the owner died mid-command) is answered at
the next start by ``recover_interrupted``: ``failed``,
``owner_stopped_before_result``, never re-applied (an approval reports what
the supervised outbox proves about its order). A safety kind is the
exception: validated and applied again (a kill file is placed only when
absent).

**Containment and budget** (W2c): the command stage runs inside the
owner loop beside the exits, so no command can stop them.

- Every failure of one command (listing, claim, validation, apply, answer)
  is contained: logged, one ``commands_failed`` event per stage, error
  class and command (the class only), and the pass goes on. A command left
  unclaimed is retried by the next pass; an answer the store refused to
  take is kept and written by the next pass (never re-applied). A lost
  session is never contained: a ``ConnectionError`` / ``TimeoutError``, or
  any error of a session kind while the session reads down, propagates
  (the loop exits 6). ``recover_interrupted`` contains per claim the same
  way.
- One full pass takes every queued kill-switch kind (``FILE_KINDS``)
  FIRST, in issue order among themselves, never deferred (they are file
  operations), then at most ``MAX_COMMANDS_PER_PASS`` entry commands in
  issue order, and starts none of those once the pass has run
  ``COMMAND_PASS_BUDGET_S`` (monotonic; the first always starts, so a slow
  command still progresses). The rest stay queued for the next iteration,
  so a burst of compose/approve commands cannot starve the runtime tick.

**A resume never lifts a newer stop** (W2c refix of a review P1: the first
W2c cut put the stops ahead of an OLDER resume, which then lifted them).
Besides the issue order above, every stop file a command places records
that command's ``issued_at`` (``HALT`` / ``FLATTEN`` here, ``PAUSE_ENTRIES``
in ``trex.pause``); a stop applied while its file stands re-stamps it when
newer (the file stands throughout). ``resume_all`` / ``resume_entries``
leave in place any stop recorded as issued after the resume (a resume that
reached the desk late, or a claim retried by a later pass) and answer
``applied`` with ``superseded_by_newer_stop`` naming it. A stop without a
recorded issue time (the terminal's, an older record) lifts as before.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import tempfile
import time
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
    SealRegistrationCommand,
    command_body_sha256,
    parse_command,
)
from tree_options.trex.contracts.layout import FLATTEN, HALT, PAUSE_ENTRIES, Wave2RunDir
from tree_options.trex.contracts.vocabulary import Blocker, DeskEvent
from tree_options.trex.desk_backup import BACKUP_REQUEST

log = logging.getLogger("trex.desk_commands")

__all__ = [
    "ANSWERED_BY",
    "ARCHIVE_DIR",
    "CLAIMED_SUFFIX",
    "COMMAND_NOT_YET_VALID",
    "COMMAND_PASS_BUDGET_S",
    "FILE_KINDS",
    "MAX_COMMANDS_PER_PASS",
    "NO_SESSION_ENTRIES",
    "RESUME_KINDS",
    "SAFETY_KINDS",
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
#: ``commands/done/``: a claim moves here once its answer is written, and a
#: set-aside duplicate goes straight here (evidence kept, never pending in
#: the web's view, W2c)
ARCHIVE_DIR = "done"
COMMAND_NOT_YET_VALID = "command_not_yet_valid"
INTERRUPTED = "owner_stopped_before_result"
GATEWAY_UNAVAILABLE = "gateway_unavailable"
#: errors that mean the gateway went: never answered here, the loop exits 6
_SESSION_ERRORS: tuple[type[BaseException], ...] = (ConnectionError, TimeoutError)
#: the issue time of a queued document that has none readable (sorts first)
_EARLIEST = datetime.min.replace(tzinfo=UTC)
_KEY = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_QUEUED = re.compile(rf"^(?P<key>{_KEY})\.json$")
_CLAIMED = re.compile(rf"^(?P<key>{_KEY}){re.escape(CLAIMED_SUFFIX)}$")
_DUPLICATE = re.compile(rf"^(?P<key>{_KEY})\.duplicate-\d+\.json$")
_INTAKE_STATUSES = frozenset({"accepted", "applied"})
#: the kinds applied by file operations alone (kill files, the pause record,
#: the backup request, a pilot seal): never a broker call, so applicable with
#: the gateway down (``process(kinds=FILE_KINDS)``)
FILE_KINDS = frozenset(
    {
        "pause_entries",
        "resume_entries",
        "halt",
        "flatten",
        "resume_all",
        "backup_state",
        "seal_registration",
    }
)
#: the safety direction (W2b review P1): once the web accepted one inside its
#: window it never expires in the queue, and a claim a dead owner never
#: answered is applied again (idempotent). Resume and entry kinds keep their
#: expiry: a stale resume never lifts a newer stop, a stale entry never trades.
SAFETY_KINDS = frozenset({"halt", "flatten", "pause_entries"})
#: the kinds that lift a stop: never past a stop issued after them
RESUME_KINDS = frozenset({"resume_all", "resume_entries"})
#: one full pass takes at most this many entry commands (every kind outside
#: ``FILE_KINDS``; the rest wait, queued, for the next loop iteration) ...
MAX_COMMANDS_PER_PASS = 8
#: ... and starts none of them once it has run this long (monotonic seconds;
#: the first always starts). The owner loop sleeps 5 s between iterations.
COMMAND_PASS_BUDGET_S = 2.0

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


class _NoSessionEntries:
    """The entry flows of a queue that has no broker session (the file pass
    before the owner connects). That pass never takes an entry kind; one
    that reached it anyway is refused, never applied."""

    def compose(self, command: ComposeEntryCommand, now: datetime) -> Outcome:
        return refused(GATEWAY_UNAVAILABLE)

    def approve(self, command: ApproveEntryCommand, now: datetime) -> Outcome:
        return refused(GATEWAY_UNAVAILABLE)

    def reject(self, command: RejectEntryCommand, now: datetime) -> Outcome:
        return refused(GATEWAY_UNAVAILABLE)

    def interrupted(self, command: AnyCommand) -> dict[str, Any]:
        return {}


NO_SESSION_ENTRIES: EntryFlows = _NoSessionEntries()


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
    """JSON with no duplicate keys and no NaN/Infinity (the web's rule).
    Nesting past the parser's recursion limit is a ValueError like any
    other malformed document (W2c: it used to escape as a RecursionError)."""
    try:
        return json.loads(
            raw.decode("utf-8"), parse_constant=_reject_constant, object_pairs_hook=_unique_object
        )
    except RecursionError as error:
        raise ValueError("nested too deeply") from error


def _parse_queued(raw: bytes) -> AnyCommand:
    """One command document: a strict JSON object of a known kind (ValueError)."""
    document = _strict_json(raw)
    if not isinstance(document, dict):
        raise ValueError("a command is a JSON object")
    return parse_command(document)


@dataclass(frozen=True)
class _Answer:
    """A desk answer decided (and its effect applied) but maybe not yet
    written: the result, and its desk event."""

    result: CommandResult
    event: str
    payload: Mapping[str, Any]


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


def _kill_record(command: AnyCommand, now: datetime) -> dict[str, Any]:
    """A HALT / FLATTEN record: who, why, which command, when applied, and
    the command's ``issued_at`` (what a later resume is compared with)."""
    return {
        "placed_by": actor_label(command.issued_by),
        "reason": getattr(command.args, "reason", ""),
        "command": command.idempotency_key,
        "at": _utc_text(now),
        "issued_at": _utc_text(command.issued_at),
    }


def _place(path: Path, payload: bytes) -> bool:
    """Place a kill/request file unless one stands (True: placed now)."""
    if os.path.lexists(path):
        return False
    _write_atomic(path, payload)
    return True


def _stop(path: Path, command: AnyCommand, now: datetime) -> bool:
    """Place a HALT / FLATTEN unless one stands (True: placed now). A
    standing one is re-stamped with this command's issue time when that is
    newer (``pause.restamp_stop``), so a resume issued between the two
    never lifts it."""
    record = _kill_record(command, now)
    if _place(path, (json.dumps(record, sort_keys=True) + "\n").encode()):
        return True
    pause.restamp_stop(path, command.issued_at, record)
    return False


def _superseded(kept: list[str]) -> str:
    """The answer detail of a resume that left newer stops in place."""
    if not kept:
        return ""
    return f"{', '.join(kept)} issued after this resume: left in place"


class CommandQueue:
    """``commands/`` of one desk run dir, consumed by the owner."""

    def __init__(
        self,
        run_dir: Path,
        *,
        flows: EntryFlows,
        clock: Callable[[], datetime],
        owner_epoch: str | None,
        event: EventFn | None = None,
        connected: Callable[[], bool] = lambda: True,
        monotonic: Callable[[], float] = time.monotonic,
        max_per_pass: int = MAX_COMMANDS_PER_PASS,
        pass_budget_s: float = COMMAND_PASS_BUDGET_S,
    ) -> None:
        self.layout = Wave2RunDir(Path(run_dir))
        self.flows = flows
        self.clock = clock
        self.owner_epoch = owner_epoch
        self.event = event
        self.connected = connected
        self.monotonic = monotonic
        self.max_per_pass = max_per_pass
        self.pass_budget_s = pass_budget_s
        #: how many queued commands the last full pass left for the next one
        self.deferred = 0
        #: answers applied but not yet written (the store refused the write):
        #: every pass writes them first; never re-applied
        self._unwritten: dict[str, _Answer] = {}
        #: (stage, error class, command) already recorded as commands_failed
        self._noted: set[tuple[str, str, str | None]] = set()
        #: the stage of the command in hand (for the containment record)
        self._stage = "list"

    # -- paths --------------------------------------------------------------------

    @property
    def directory(self) -> Path:
        return self.layout.commands()

    def claimed(self, key: str) -> Path:
        return self.directory / f"{key}{CLAIMED_SUFFIX}"

    # -- the loop step --------------------------------------------------------------

    def process(self, kinds: frozenset[str] | None = None) -> list[CommandResult]:
        """Claim, validate, apply and answer every queued command once.

        ``kinds`` (``FILE_KINDS`` while the gateway is down): only the queued
        commands of those kinds, by the queued document; anything else, and
        a document that does not read, stays queued for the full pass.

        The kill switches go first; a full pass (``kinds`` None) then takes
        the others within its budget (module docstring). Every failure but a
        lost session is contained per command."""
        answered = self._write_unwritten()
        started = self.monotonic()
        self._stage = "list"
        try:
            queued = self._queued(kinds)
        except Exception as error:
            # listing is a file operation: a file kinds pass contains it
            # even with the session down
            file_pass = kinds is not None and kinds <= FILE_KINDS
            if isinstance(error, _SESSION_ERRORS) or (not file_pass and not self.connected()):
                raise
            self._note(error, None)
            return answered
        taken = 0
        self.deferred = 0
        for index, (key, path, kind) in enumerate(queued):
            # the kill-switch kinds are never deferred (they come first, in
            # issue order); the budget bounds the entry kinds only
            if kinds is None and kind not in FILE_KINDS:
                if taken and self._spent(taken, started):
                    self.deferred = len(queued) - index  # entry kinds only
                    log.info("%d command(s) wait for the next pass", self.deferred)
                    break
                taken += 1
            try:
                result = self._process(key, path)
            except Exception as error:
                if self._session_lost(error, kind):
                    raise
                self._note(error, key)
                continue
            if result is not None:
                answered.append(result)
        return answered

    def _spent(self, taken: int, started: float) -> bool:
        """The pass took its count, or ran its wall time."""
        if taken >= self.max_per_pass:
            return True
        return self.monotonic() - started >= self.pass_budget_s

    def _session_lost(self, error: BaseException, kind: str) -> bool:
        """A failure the loop must see (exit 6), never contained here: a
        ``ConnectionError`` / ``TimeoutError``, or any error of a session
        kind while the session reads down (as ``_apply`` decides)."""
        if isinstance(error, _SESSION_ERRORS):
            return True
        return kind not in FILE_KINDS and not self.connected()

    def _note(self, error: BaseException, command: str | None) -> None:
        """Record one contained failure (the error class only, as the books
        step does): logged every time, one ``commands_failed`` event per
        stage, class and command."""
        stage, name = self._stage, type(error).__name__
        log.error("command stage %s failed (%s, %s); exits are unaffected", stage, name, command)
        mark = (stage, name, command)
        if mark in self._noted:
            return
        self._noted.add(mark)
        self._emit(DeskEvent.COMMANDS_FAILED.value, stage=stage, error=name, command=command)

    def _queued(self, kinds: frozenset[str] | None = None) -> list[tuple[str, Path, str]]:
        """Queued commands ``(key, path, kind)``: every kill-switch kind
        (``FILE_KINDS``: stops, resumes, backup) first, then the entry
        kinds; each group oldest ``issued_at`` first (an unreadable one
        first of its group: it is answered, refused, at once).

        Stops and resumes stay in issue order among themselves, so the
        owner's latest word wins (W2c refix: putting the stops ahead of an
        older resume let that resume lift them); on a tie the resume goes
        before the stop (the safe direction)."""
        if not self.directory.is_dir():
            return []
        found: list[tuple[int, datetime, int, str, Path, str]] = []
        for path in self.directory.iterdir():
            match = _QUEUED.match(path.name)
            if match is None:
                continue
            issued, kind = _EARLIEST, ""
            try:
                doc = _strict_json(path.read_bytes()[: MAX_COMMAND_BYTES + 1])
                if isinstance(doc, dict):
                    issued, kind = _instant(doc.get("issued_at")), str(doc.get("kind", ""))
            except Exception:  # unreadable: refused by the pass that takes it
                issued, kind = _EARLIEST, ""
            if kinds is not None and kind not in kinds:
                continue
            group = 0 if kind in FILE_KINDS else 1
            tie = 0 if kind in RESUME_KINDS else 1
            found.append((group, issued, tie, match["key"], path, kind))
        return [(key, path, kind) for *_, key, path, kind in sorted(found)]

    def archived(self, key: str) -> Path:
        """Where an answered claim is kept (``commands/done/``)."""
        return self.directory / ARCHIVE_DIR / f"{key}{CLAIMED_SUFFIX}"

    def _process(self, key: str, path: Path) -> CommandResult | None:
        self._stage = "claim"
        claimed = self.claimed(key)
        if os.path.lexists(claimed) or os.path.lexists(self.archived(key)):
            self._set_aside(key, path)
            return None
        try:
            rename_noreplace(path, claimed)  # the claim: processed exactly once
        except FileNotFoundError:
            return None
        except FileExistsError:
            self._set_aside(key, path)
            return None
        self._stage = "validate"
        now = self.clock()
        intake = self._intake(key)
        if intake is not None and _answered_by_desk(intake):
            # a desk answer already stands for this key (its claim gone):
            # never replaced; this second claim is kept aside as a duplicate
            moved = self._move_aside(key, claimed)
            self._emit(
                DeskEvent.COMMAND_REFUSED.value,
                command=key,
                reason=Blocker.DUPLICATE_COMMAND.value,
                detail="already answered by the desk",
                moved_to=None if moved is None else f"{ARCHIVE_DIR}/{moved.name}",
            )
            return None
        command, body_sha, kind, outcome = self._validate(key, claimed, now, intake)
        if kind is None or body_sha is None:
            self._emit(DeskEvent.COMMAND_REFUSED.value, command=key, reason="command_invalid")
            self._archive(key)  # nothing reads: no answer can ever be written
            return None
        if outcome is None:
            assert command is not None
            self._stage = "apply"
            outcome = _late(self._apply(command, now), command, intake, now)
        return self._answer(key, kind, body_sha, outcome, now, command)

    def _move_aside(self, key: str, path: Path) -> Path | None:
        """``path`` into the archive's first free ``<key>.duplicate-<n>.json``
        (no-replace); None when it vanished meanwhile."""
        folder = self.directory / ARCHIVE_DIR
        folder.mkdir(parents=True, exist_ok=True)
        n = 0
        while True:
            n += 1
            target = folder / f"{key}.duplicate-{n}.json"
            try:
                rename_noreplace(path, target)
            except FileExistsError:
                continue
            except FileNotFoundError:
                return None
            return target

    def _set_aside(self, key: str, path: Path) -> None:
        """A second ``<key>.json`` for a claimed key: never processed, kept
        in the archive as a duplicate."""
        moved = self._move_aside(key, path)
        if moved is None:
            return
        self._emit(
            DeskEvent.COMMAND_REFUSED.value,
            command=key,
            reason=Blocker.DUPLICATE_COMMAND.value,
            moved_to=f"{ARCHIVE_DIR}/{moved.name}",
        )

    def _archive(self, key: str) -> None:
        """Move the claim of an answered key into the archive (no-replace:
        a second claim of an archived key is kept as a duplicate). Best
        effort: a failure is recorded and the claim stays where it is (not
        pending either way); the next start archives it."""
        claimed = self.claimed(key)
        self._stage = "archive"
        try:
            if not os.path.lexists(claimed):
                return
            target = self.archived(key)
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                rename_noreplace(claimed, target)
            except FileExistsError:
                self._move_aside(key, claimed)
            except FileNotFoundError:
                return
        except Exception as error:
            self._note(error, key)

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
            command = _parse_queued(raw)
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
        elif (now - command.expires_at).total_seconds() >= 0 and not _durable(command, intake):
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
            # a file kind never touches the session: its error is its own,
            # answered even while the gateway is down
            session_down = command.kind not in FILE_KINDS and not self.connected()
            if isinstance(error, _SESSION_ERRORS) or session_down:
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
                issued_at=command.issued_at,
            )
            if isinstance(command, PauseEntriesCommand):
                event = DeskEvent.ENTRIES_PAUSED
                detail = ""
            else:
                event = DeskEvent.ENTRIES_RESUMED
                kept = [PAUSE_ENTRIES] if facts.get(pause.SUPERSEDED_BY_NEWER_STOP) else []
                facts = {**facts, pause.SUPERSEDED_BY_NEWER_STOP: kept}
                detail = _superseded(kept)
            self._emit(event.value, actor=actor, command=command.idempotency_key, **facts)
            return applied(detail, **facts)
        if isinstance(command, HaltCommand):
            placed = _stop(root / HALT, command, now)
            return applied(kill_file=HALT, changed=placed, halt=True)
        if isinstance(command, FlattenCommand):
            placed = _stop(root / FLATTEN, command, now)
            halted = os.path.lexists(root / HALT)
            detail = "HALT is present: exits wait until it is removed" if halted else ""
            return applied(detail, kill_file=FLATTEN, changed=placed, halt_present=halted)
        if isinstance(command, ResumeAllCommand):
            standing = [name for name in (HALT, FLATTEN) if os.path.lexists(root / name)]
            # a stop issued after this resume is the owner's later word
            kept = [name for name in standing if pause.issued_after(root / name, command.issued_at)]
            removed = [name for name in standing if name not in kept]
            for name in removed:
                (root / name).unlink(missing_ok=True)
            entries = pause.resume_entries(
                root,
                actor=actor,
                reason=command.args.reason,
                now=now,
                command_key=command.idempotency_key,
                issued_at=command.issued_at,
            )
            if entries.get(pause.SUPERSEDED_BY_NEWER_STOP):
                kept.append(PAUSE_ENTRIES)
            return applied(
                _superseded(kept),
                removed=removed,
                entries_paused=entries["entries_paused"],
                pause_lifted=entries["changed"],
                approvals_revived=False,
                **{pause.SUPERSEDED_BY_NEWER_STOP: kept},
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
        if isinstance(command, SealRegistrationCommand):
            from tree_options.research.pilot.seal import apply_seal_command

            sealed = apply_seal_command(command.args, command.issued_by, now)
            if sealed.status == "applied":
                return applied(sealed.detail, **sealed.facts)
            return refused(sealed.reason or "command_invalid", sealed.detail, **sealed.facts)
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
        self._stage = "answer"
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
        name = (
            DeskEvent.COMMAND_APPLIED if outcome.status == "applied" else DeskEvent.COMMAND_REFUSED
        )
        flags = {f: True for f in ("applied_after_expiry", "recovered") if outcome.facts.get(f)}
        answer = _Answer(
            result,
            name.value,
            {
                "command": key,
                "kind": kind,
                "status": outcome.status,
                "reason": outcome.reason,
                "actor": None if command is None else actor_label(command.issued_by),
                **flags,
            },
        )
        try:
            self._write(answer)
        except BaseException:
            # applied (or refused) already: the next pass writes it, never
            # re-applies it
            self._unwritten[key] = answer
            raise
        return result

    def _write(self, answer: _Answer) -> None:
        """The answer where the web reads it, its event, then the claim to
        the archive (best effort, never failing the answer)."""
        key = answer.result.idempotency_key
        with self._store_lock():
            _write_atomic(self.layout.command_result(key), model_bytes(answer.result))
        self._emit(answer.event, **answer.payload)
        self._archive(key)

    def _write_unwritten(self) -> list[CommandResult]:
        """Write the answers a refused store write left (oldest first)."""
        written: list[CommandResult] = []
        for key, answer in list(self._unwritten.items()):
            self._stage = "answer"
            try:
                self._write(answer)
            except Exception as error:
                if self._session_lost(error, answer.result.kind):
                    raise
                self._note(error, key)
                continue
            del self._unwritten[key]
            written.append(answer.result)
        return written

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
        (``owner_stopped_before_result``), never re-applied; except a
        safety kind (``SAFETY_KINDS``), which is validated and applied again
        (placing a kill file is idempotent), so a dead owner never drops a
        HALT, FLATTEN or PAUSE the web accepted.

        Each claim is contained on its own (W2c). A claim whose answer
        stands, and every duplicate left beside the queue, moves to the
        archive (``done/``)."""
        answered: list[CommandResult] = []
        self._stage = "recover"
        try:
            if not self.directory.is_dir():
                return answered
            names = sorted(path.name for path in self.directory.iterdir())
        except Exception as error:
            if self._session_lost(error, ""):
                raise
            self._note(error, None)
            return answered
        now = self.clock()
        for name in names:
            self._stage = "recover"
            match = _CLAIMED.match(name) or _DUPLICATE.match(name)
            if match is None:
                continue
            key = match["key"]
            try:
                if match.re is _DUPLICATE:
                    self._move_aside(key, self.directory / name)
                    continue
                result = self._recover(key, self.directory / name, now)
            except Exception as error:
                if self._session_lost(error, ""):
                    raise
                self._note(error, key)
                continue
            if result is not None:
                answered.append(result)
        return answered

    def _recover(self, key: str, path: Path, now: datetime) -> CommandResult | None:
        """One claim a previous owner left (``recover_interrupted``)."""
        intake = self._intake(key)
        if intake is not None and _answered_by_desk(intake):
            self._archive(key)  # answered, its archive interrupted
            return None
        command: AnyCommand | None
        try:
            command = _parse_queued(path.read_bytes()[:MAX_COMMAND_BYTES])
        except (OSError, ValueError):
            command = None
        if command is not None:
            kind, body_sha = command.kind, command_body_sha256(command)
        elif intake is not None:
            kind, body_sha = intake.kind, intake.command_body_sha256
        else:
            return None  # nothing reads: no answer can be written
        if command is not None and command.kind in SAFETY_KINDS:
            _, sha, accepted_kind, refusal = self._validate(key, path, now, intake)
            self._stage = "apply"
            outcome = refusal or _late(self._apply(command, now), command, intake, now)
            outcome = _with(outcome, recovered=True)
            return self._answer(key, accepted_kind or kind, sha or body_sha, outcome, now, command)
        facts: dict[str, Any] = {}
        if command is not None and command.kind not in FILE_KINDS:
            try:
                facts = self.flows.interrupted(command)
            except Exception as error:  # reporting only
                facts = {"report_error": type(error).__name__}
        outcome = failed(INTERRUPTED, "the command is never re-applied", **facts)
        return self._answer(key, kind, body_sha, outcome, now, command)

    def _emit(self, name: str, **payload: Any) -> None:
        if self.event is None:
            return
        try:
            self.event(name, **payload)
        except Exception:  # a record failure never breaks the owner loop
            log.exception("command event %s not recorded", name)


def _answered_by_desk(result: CommandResult) -> bool:
    return isinstance(result.result, dict) and result.result.get("answered_by") == ANSWERED_BY


def _utc_text(instant: datetime) -> str:
    return instant.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _instant(value: Any) -> datetime:
    """A queued document's ``issued_at`` as an instant (compared as time,
    never as text: offsets and fractions differ); ``_EARLIEST`` when it
    does not read as a timezone-aware instant."""
    if not isinstance(value, str):
        return _EARLIEST
    try:
        instant = datetime.fromisoformat(value)
    except ValueError:
        return _EARLIEST
    return instant if instant.utcoffset() is not None else _EARLIEST


def _durable(command: AnyCommand, intake: CommandResult) -> bool:
    """A safety kind the web accepted inside its window: its expiry bounds
    the web's acceptance, never the owner's consumption (W2b review P1: an
    owner kept away by a gateway outage must not drop an accepted HALT)."""
    return (
        command.kind in SAFETY_KINDS
        and intake.status in _INTAKE_STATUSES
        and (command.expires_at - intake.at).total_seconds() > 0
    )


def _with(outcome: Outcome, **facts: Any) -> Outcome:
    return Outcome(outcome.status, outcome.reason, outcome.detail, {**outcome.facts, **facts})


def _late(
    outcome: Outcome, command: AnyCommand, intake: CommandResult | None, now: datetime
) -> Outcome:
    """An applied durable command past its expiry says so in its facts."""
    if outcome.status != "applied" or intake is None:
        return outcome
    if (now - command.expires_at).total_seconds() < 0:
        return outcome
    return _with(
        outcome,
        applied_after_expiry=True,
        accepted_at=_utc_text(intake.at),
        expired_at=_utc_text(command.expires_at),
    )
