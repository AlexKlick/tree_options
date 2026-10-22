"""The desk owner's command consumer (``trex.desk_commands``).

Commands are queued by the web's own ``CommandStore`` (the intake record
the desk binds to), consumed by ``CommandQueue`` with a recording stand-in
for the entry flows. Oracles: the contract's result shape (read back by the
web store), kill files on disk, the pause audit, and the claim files.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from tests.unit.trex_fakes import FakeCommandAuthority, SessionClock
from tree_options.time.sessions import shift_instant
from tree_options.trex import desk_commands
from tree_options.trex.contracts import (
    AnyCommand,
    CommandResult,
    command_body_sha256,
    model_bytes,
    model_document,
    parse_command,
)
from tree_options.trex.desk_backup import BACKUP_REQUEST
from tree_options.trex.desk_commands import (
    ANSWERED_BY,
    CLAIMED_SUFFIX,
    CommandQueue,
    Outcome,
    applied,
    refused,
)
from tree_options.trex.pause import PAUSE_AUDIT_FILE, is_paused
from tree_options.trex_web.command_store import CommandStore


class Flows:
    """Records the entry calls; answers what it is told to."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.answer: Outcome = applied(proposal_id="p" * 64)
        self.raises: BaseException | None = None

    def _call(self, kind: str, command: AnyCommand) -> Outcome:
        self.calls.append((kind, command.idempotency_key))
        if self.raises is not None:
            raise self.raises
        return self.answer

    def compose(self, command: Any, now: datetime) -> Outcome:
        return self._call("compose", command)

    def approve(self, command: Any, now: datetime) -> Outcome:
        return self._call("approve", command)

    def reject(self, command: Any, now: datetime) -> Outcome:
        return self._call("reject", command)

    def interrupted(self, command: AnyCommand) -> dict[str, Any]:
        return {"order_sent": "unknown", "note": "reconciliation_required"}


class Rig:
    def __init__(self, tmp: Path) -> None:
        self.clock = SessionClock()
        self.root = tmp / "desk"
        self.root.mkdir()
        self.store = CommandStore(self.root)
        self.auth = FakeCommandAuthority(clock=self.clock)
        self.flows = Flows()
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.up = True
        self.queue = self.new_queue()

    def new_queue(self) -> CommandQueue:
        return CommandQueue(
            self.root,
            flows=self.flows,
            clock=self.clock,
            owner_epoch="owner-test",
            event=lambda name, **payload: self.events.append((name, payload)),
            connected=lambda: self.up,
        )

    def command(self, kind: str, args: dict[str, Any], *, ttl_s: int = 120, **doc: Any) -> Any:
        now = self.clock.utc()
        body = {
            "schema": "trex-command/1",
            "idempotency_key": str(uuid.uuid4()),
            "kind": kind,
            "args": args,
            "issued_at": now.isoformat(),
            "expires_at": shift_instant(now, ttl_s).isoformat(),
            **doc,
        }
        raw = json.dumps(body).encode()
        actor = self.auth.verify(self.auth.make_request(raw, family_sub="owner"))
        return parse_command({**body, "issued_by": model_document(actor)})

    def submit(self, kind: str, args: dict[str, Any], **kw: Any) -> str:
        command = self.command(kind, args, **kw)
        assert self.store.submit(command, now=self.clock.utc()).result.status == "accepted"
        return command.idempotency_key

    def queue_raw(self, command: Any, *, intake: bool = True) -> str:
        """The web store's two files, written by hand (no intake check)."""
        key = command.idempotency_key
        folder = self.root / "commands"
        folder.mkdir(exist_ok=True)
        if intake:
            accepted = CommandResult(
                idempotency_key=key,
                kind=command.kind,
                command_body_sha256=command_body_sha256(command),
                status="accepted",
                at=self.clock.utc(),
            )
            (folder / f"{key}.result.json").write_bytes(model_bytes(accepted))
        (folder / f"{key}.json").write_bytes(model_bytes(command))
        return key

    def result(self, key: str) -> CommandResult:
        result = self.store.read_result(key)
        assert result is not None
        return result


@pytest.fixture()
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


def test_a_queued_command_is_claimed_applied_and_answered_once(rig):
    key = rig.submit("pause_entries", {"reason": "lunch"})
    (answer,) = rig.queue.process()
    assert answer.status == "applied" and answer.reason is None
    assert answer.result["answered_by"] == ANSWERED_BY
    assert answer.result["entries_paused"] is True
    assert is_paused(rig.root)
    audit = json.loads((rig.root / PAUSE_AUDIT_FILE).read_text().splitlines()[-1])
    assert (audit["actor"], audit["command"], audit["reason"]) == ("family:owner", key, "lunch")
    folder = rig.root / "commands"
    assert not (folder / f"{key}.json").exists()
    assert not (folder / f"{key}{CLAIMED_SUFFIX}").exists(), "answered: archived"
    assert (folder / desk_commands.ARCHIVE_DIR / f"{key}{CLAIMED_SUFFIX}").exists()
    assert rig.queue.process() == [], "processed exactly once"
    assert ("command_applied", "pause_entries") in [(n, p.get("kind")) for n, p in rig.events]


def test_the_web_reads_the_desk_answer_and_a_retry_is_a_duplicate(rig):
    key = rig.submit("halt", {"reason": "drill"})
    rig.queue.process()
    stored = rig.result(key)
    assert (stored.status, stored.kind, stored.result["kill_file"]) == ("applied", "halt", "HALT")
    archived = rig.root / "commands" / desk_commands.ARCHIVE_DIR / f"{key}.claimed.json"
    command = parse_command(json.loads(archived.read_text()))
    retry = rig.store.submit(command, now=rig.clock.utc())
    assert retry.result.status == "duplicate"
    assert retry.result.result["first_status"] == "applied"


_COMPOSE = {"program_id": "p", "candidate_id": "c", "asset_kind": "equity"}


@pytest.mark.parametrize(
    ("kind", "args"),
    [
        ("resume_all", {"reason": ""}),
        ("resume_entries", {"reason": ""}),
        ("backup_state", {"label": "eod"}),
        ("compose_entry", _COMPOSE),
        ("approve_entry", None),
        ("reject_entry", {"proposal_id": "a" * 64}),
    ],
)
def test_an_expired_command_is_refused_and_nothing_is_applied(rig, kind, args):
    """Only the safety direction outlives its window: a stale resume never
    lifts a newer stop, and a stale entry never trades."""
    rig.submit(kind, _approve_args() if args is None else args, ttl_s=30)
    rig.clock.advance(30)
    (answer,) = rig.queue.process()
    assert (answer.status, answer.reason) == ("refused", "command_expired")
    assert rig.flows.calls == []
    assert not any((rig.root / f).exists() for f in ("HALT", "FLATTEN", BACKUP_REQUEST))


@pytest.mark.parametrize("kind", ["halt", "flatten", "pause_entries"])
def test_an_accepted_safety_command_is_applied_after_its_expiry(rig, kind):
    """W2b review P1: the web answered 202 accepted, then the owner could not
    consume the command inside its TTL (gateway outage, restart wait). A
    HALT, FLATTEN or PAUSE the web accepted is never dropped as expired."""
    key = rig.submit(kind, {"reason": "gateway down"}, ttl_s=300)
    accepted_at = rig.result(key).at
    rig.clock.advance(301)
    (answer,) = rig.queue.process()
    assert (answer.idempotency_key, answer.status, answer.reason) == (key, "applied", None)
    on_disk = {"halt": "HALT", "flatten": "FLATTEN", "pause_entries": "PAUSE_ENTRIES"}[kind]
    assert (rig.root / on_disk).exists()
    assert answer.result["applied_after_expiry"] is True
    assert answer.result["accepted_at"] == accepted_at.isoformat().replace("+00:00", "Z")
    applied_events = [p for n, p in rig.events if n == "command_applied"]
    assert applied_events[-1]["applied_after_expiry"] is True


def test_a_safety_command_the_web_never_accepted_in_its_window_still_expires(rig):
    command = rig.command("halt", {"reason": ""}, ttl_s=30)
    rig.clock.advance(30)
    rig.queue_raw(command)  # an intake record stamped at expires_at: outside the window
    (answer,) = rig.queue.process()
    assert (answer.status, answer.reason) == ("refused", "command_expired")
    assert not (rig.root / "HALT").exists()


def test_the_file_pass_applies_the_kill_switch_kinds_without_a_session(rig):
    """While the gateway is down only the file kinds are taken (in issue
    order); an entry kind stays queued, untouched, for a live session."""
    rig.up = False
    compose = rig.submit("compose_entry", _COMPOSE)
    rig.clock.advance(1)
    halt = rig.submit("halt", {"reason": "gateway down"})
    rig.clock.advance(1)
    resume = rig.submit("resume_entries", {"reason": ""})
    answers = rig.queue.process(kinds=desk_commands.FILE_KINDS)
    assert [(a.idempotency_key, a.status) for a in answers] == [
        (halt, "applied"),
        (resume, "applied"),
    ]
    assert (rig.root / "HALT").exists()
    assert rig.flows.calls == []
    assert (rig.root / "commands" / f"{compose}.json").exists(), "never claimed offline"
    assert rig.result(compose).status == "accepted"
    rig.up = True
    (later,) = rig.queue.process()
    assert later.idempotency_key == compose
    assert rig.flows.calls == [("compose", compose)]


def test_a_malformed_queued_kind_never_breaks_the_file_pass(rig):
    junk = rig.submit("compose_entry", _COMPOSE)
    path = rig.root / "commands" / f"{junk}.json"
    path.write_text(json.dumps({**json.loads(path.read_text()), "kind": ["halt"]}))
    halt = rig.submit("halt", {"reason": ""})
    (answer,) = rig.queue.process(kinds=desk_commands.FILE_KINDS)
    assert (answer.idempotency_key, answer.status) == (halt, "applied")
    assert path.exists(), "left for the full pass, which refuses it"
    (refusal,) = rig.queue.process()
    assert (refusal.idempotency_key, refusal.status) == (junk, "refused")


def test_a_file_error_without_a_session_is_a_failed_answer(rig, monkeypatch):
    def unwritable(path: Path, payload: bytes) -> bool:
        raise PermissionError(13, "read-only")

    rig.up = False
    monkeypatch.setattr(desk_commands, "_place", unwritable)
    key = rig.submit("halt", {"reason": ""})
    (answer,) = rig.queue.process(kinds=desk_commands.FILE_KINDS)
    assert (answer.idempotency_key, answer.status, answer.reason) == (key, "failed", "apply_error")
    assert answer.detail == "PermissionError"


def test_an_interrupted_safety_command_is_reapplied_at_recovery(rig):
    """A claim the dead owner never answered: a HALT/FLATTEN/PAUSE is applied
    again (idempotent: a kill file is placed only when absent); every other
    kind is still answered failed and never re-applied."""
    folder = rig.root / "commands"
    halt = rig.submit("halt", {"reason": "owner died mid-command"}, ttl_s=60)
    resume = rig.submit("resume_all", {"reason": ""}, ttl_s=60)
    for key in (halt, resume):
        (folder / f"{key}.json").rename(folder / f"{key}{CLAIMED_SUFFIX}")
    rig.clock.advance(120)
    answers = {a.idempotency_key: a for a in rig.new_queue().recover_interrupted()}
    assert (answers[halt].status, answers[halt].reason) == ("applied", None)
    assert answers[halt].result["recovered"] is True
    assert (rig.root / "HALT").exists()
    assert (answers[resume].status, answers[resume].reason) == (
        "failed",
        "owner_stopped_before_result",
    )
    assert rig.new_queue().recover_interrupted() == [], "answered once"


def test_a_command_without_the_webs_intake_record_is_refused(rig):
    key = rig.queue_raw(rig.command("halt", {"reason": ""}), intake=False)
    (answer,) = rig.queue.process()
    assert (answer.idempotency_key, answer.status) == (key, "refused")
    assert (answer.reason, answer.detail) == ("command_unauthorized", "no_intake_record")
    assert not (rig.root / "HALT").exists()
    command = rig.command("flatten", {"reason": ""})
    key = rig.queue_raw(command, intake=False)
    accepted = CommandResult(
        idempotency_key=key,
        kind="flatten",
        command_body_sha256="0" * 64,
        status="accepted",
        at=rig.clock.utc(),
    )
    (rig.root / "commands" / f"{key}.result.json").write_bytes(model_bytes(accepted))
    (answer,) = rig.queue.process()
    assert (answer.status, answer.reason) == ("refused", "command_body_mismatch")
    assert not (rig.root / "FLATTEN").exists()


def test_a_tampered_queued_command_is_refused(rig):
    accepted = rig.command("flatten", {"reason": "accepted reason"})
    rig.store.submit(accepted, now=rig.clock.utc())
    key = accepted.idempotency_key
    path = rig.root / "commands" / f"{key}.json"
    doc = json.loads(path.read_text())
    doc["args"]["reason"] = "something else"
    path.write_text(json.dumps(doc))
    (answer,) = rig.queue.process()
    assert answer.command_body_sha256 == command_body_sha256(accepted), "the accepted identity"
    retry = rig.store.submit(accepted, now=rig.clock.utc())
    assert retry.result.status == "duplicate", "a retry of the accepted command stays a duplicate"
    assert (answer.status, answer.reason) == ("refused", "command_body_mismatch")
    assert not (rig.root / "FLATTEN").exists()


def test_an_unparsable_queued_command_is_refused_on_its_intake_record(rig):
    key = rig.submit("halt", {"reason": ""})
    (rig.root / "commands" / f"{key}.json").write_text('{"kind": "halt", "kind": "halt"}')
    (answer,) = rig.queue.process()
    assert (answer.status, answer.reason, answer.kind) == ("refused", "command_invalid", "halt")
    assert not (rig.root / "HALT").exists()


def test_a_command_from_the_future_is_refused(rig):
    command = rig.command(
        "halt",
        {"reason": ""},
        issued_at=shift_instant(rig.clock.utc(), 120).isoformat(),
        expires_at=shift_instant(rig.clock.utc(), 300).isoformat(),
    )
    rig.queue_raw(command)
    (answer,) = rig.queue.process()
    assert (answer.status, answer.reason) == ("refused", "command_not_yet_valid")


def test_the_kill_switch_kinds(rig):
    rig.submit("halt", {"reason": "drill"})
    rig.queue.process()
    halt = json.loads((rig.root / "HALT").read_text())
    assert halt["placed_by"] == "family:owner"
    rig.submit("flatten", {"reason": ""})
    (flatten,) = rig.queue.process()
    assert flatten.result["halt_present"] is True and "HALT" in flatten.detail
    rig.submit("pause_entries", {"reason": ""})
    rig.queue.process()
    rig.submit("resume_all", {"reason": "over"})
    (resumed,) = rig.queue.process()
    assert resumed.result["removed"] == ["HALT", "FLATTEN"]
    assert resumed.result["approvals_revived"] is False
    assert not any((rig.root / f).exists() for f in ("HALT", "FLATTEN", "PAUSE_ENTRIES"))
    rig.submit("backup_state", {"label": "eod-1"})
    (backup,) = rig.queue.process()
    assert backup.result["changed"] is True
    assert json.loads((rig.root / BACKUP_REQUEST).read_text())["label"] == "eod-1"


def test_entry_kinds_go_to_the_flows_in_issue_order(rig):
    first = rig.command(
        "compose_entry", {"program_id": "p", "candidate_id": "c", "asset_kind": "equity"}
    )
    rig.clock.advance(1)
    second_key = rig.submit("reject_entry", {"proposal_id": "a" * 64})
    rig.queue_raw(first)  # issued first, queued last: issue order wins
    rig.flows.answer = refused("proposal_unknown", proposal_id="a" * 64)
    answers = rig.queue.process()
    assert rig.flows.calls == [("compose", first.idempotency_key), ("reject", second_key)]
    assert [a.status for a in answers] == ["refused", "refused"]
    assert answers[1].result["proposal_id"] == "a" * 64


def test_a_flow_bug_is_a_failed_answer_but_a_lost_session_propagates(rig):
    rig.flows.raises = KeyError("bug")
    key = rig.submit("approve_entry", _approve_args())
    (answer,) = rig.queue.process()
    assert (answer.status, answer.reason, answer.detail) == ("failed", "apply_error", "KeyError")
    rig.flows.raises = ConnectionError("gone")
    key = rig.submit("approve_entry", _approve_args())
    with pytest.raises(ConnectionError):
        rig.queue.process()
    assert rig.result(key).status == "accepted", "unanswered: the claim stays open"
    # the next owner answers it, never re-applies it
    rig.flows.raises = None
    queue = rig.new_queue()
    (recovered,) = queue.recover_interrupted()
    assert (recovered.idempotency_key, recovered.status) == (key, "failed")
    assert recovered.reason == "owner_stopped_before_result"
    assert recovered.result["order_sent"] == "unknown"
    assert queue.recover_interrupted() == [], "answered once"
    assert len(rig.flows.calls) == 2, "never re-applied"


def test_a_second_file_for_a_claimed_key_is_set_aside_unprocessed(rig):
    key = rig.submit("halt", {"reason": ""})
    rig.queue.process()
    first = rig.result(key)
    done = rig.root / "commands" / desk_commands.ARCHIVE_DIR
    claimed = done / f"{key}.claimed.json"
    (rig.root / "commands" / f"{key}.json").write_bytes(claimed.read_bytes())
    assert rig.queue.process() == []
    assert (done / f"{key}.duplicate-1.json").exists(), "set aside into the archive"
    assert rig.result(key) == first, "the desk's answer is never replaced"
    # the same once more: the next free slot, never a replacement
    (rig.root / "commands" / f"{key}.json").write_bytes(claimed.read_bytes())
    assert rig.queue.process() == []
    assert (done / f"{key}.duplicate-2.json").exists()
    assert sorted(p.name for p in (rig.root / "commands").glob("*.json")) == [f"{key}.result.json"]


def test_a_claim_still_in_hand_sets_a_second_file_aside_too(rig):
    """A claim not yet archived (its answer still unwritten) is still a
    claim: a second ``<key>.json`` is set aside, never processed."""
    key = rig.submit("compose_entry", _COMPOSE)
    folder = rig.root / "commands"
    (folder / f"{key}.json").rename(folder / f"{key}{CLAIMED_SUFFIX}")
    (folder / f"{key}.json").write_bytes((folder / f"{key}{CLAIMED_SUFFIX}").read_bytes())
    assert rig.queue.process() == []
    assert rig.flows.calls == []
    assert (folder / desk_commands.ARCHIVE_DIR / f"{key}.duplicate-1.json").exists()


def test_recovery_archives_answered_claims_and_set_aside_duplicates(rig):
    """The claims and duplicates an earlier owner left beside answered
    commands (written before the archive existed, or a crash between the
    answer and the archive) move to ``done/`` at the next start; an
    unanswered claim is answered first, then archived."""
    folder = rig.root / "commands"
    answered = rig.submit("pause_entries", {"reason": ""})
    rig.queue.process()
    done = folder / desk_commands.ARCHIVE_DIR
    (done / f"{answered}{CLAIMED_SUFFIX}").rename(folder / f"{answered}{CLAIMED_SUFFIX}")
    (folder / f"{answered}.duplicate-1.json").write_text("{}")
    open_claim = rig.submit("resume_all", {"reason": ""})
    (folder / f"{open_claim}.json").rename(folder / f"{open_claim}{CLAIMED_SUFFIX}")
    (recovered,) = rig.new_queue().recover_interrupted()
    assert (recovered.idempotency_key, recovered.status) == (open_claim, "failed")
    assert sorted(p.name for p in folder.glob("*.json")) == sorted(
        [f"{answered}.result.json", f"{open_claim}.result.json"]
    )
    assert sorted(p.name for p in done.iterdir()) == sorted(
        [
            f"{answered}{CLAIMED_SUFFIX}",
            f"{answered}.duplicate-1.json",
            f"{open_claim}{CLAIMED_SUFFIX}",
        ]
    )
    assert rig.store.pending_kinds() == frozenset()


def test_any_error_while_the_session_is_down_propagates(rig):
    rig.up = False
    rig.flows.raises = RuntimeError("torn down")
    rig.submit("compose_entry", {"program_id": "p", "candidate_id": "c", "asset_kind": "option"})
    with pytest.raises(RuntimeError):
        rig.queue.process()


def _approve_args() -> dict[str, Any]:
    return {
        "proposal_id": "a" * 64,
        "proposal_sha256": "b" * 64,
        "effect_sha256": "c" * 64,
        "owner_epoch": "owner-test",
        "profile_digest": "d" * 64,
    }


def test_results_carry_utc_instants(rig):
    key = rig.submit("resume_entries", {"reason": ""})
    rig.queue.process()
    assert rig.result(key).at.tzinfo == UTC


# ------------------------------------------------- W2c item 3: containment


def _failures(rig: Rig) -> list[dict[str, Any]]:
    return [p for n, p in rig.events if n == "commands_failed"]


def test_a_too_deeply_nested_queued_command_is_refused_never_raised(rig):
    """A queued file nested past the JSON parser's recursion limit (inside
    the size bound) used to escape the listing as a RecursionError and stop
    the owner loop; it is a ``command_invalid`` refusal on its intake record
    like any unparsable command, and the next command still runs."""
    junk = rig.submit("halt", {"reason": ""})
    depth = desk_commands.MAX_COMMAND_BYTES // 2 - 1
    (rig.root / "commands" / f"{junk}.json").write_text("[" * depth + "]" * depth)
    rig.clock.advance(1)
    pause_key = rig.submit("pause_entries", {"reason": ""})
    answers = {a.idempotency_key: a for a in rig.queue.process()}
    assert (answers[junk].status, answers[junk].reason) == ("refused", "command_invalid")
    assert answers[junk].kind == "halt", "answered on the web's intake record"
    assert answers[pause_key].status == "applied"
    assert not (rig.root / "HALT").exists()


def test_an_answer_that_cannot_be_written_is_contained_and_retried(rig, monkeypatch):
    """The answer write (store lock, atomic file) fails for one command: the
    pass goes on with the next command (one ``commands_failed`` event naming
    the stage and the error class), and the applied command's answer is
    written by the next pass once the store is writable again; never
    re-applied."""
    halt = rig.submit("halt", {"reason": ""})
    rig.clock.advance(1)
    pause_key = rig.submit("pause_entries", {"reason": ""})
    real_write = desk_commands._write_atomic
    broken = {halt}

    def flaky(path: Path, payload: bytes) -> None:
        if any(path.name == f"{key}.result.json" for key in broken):
            raise PermissionError(13, "read-only")
        real_write(path, payload)

    monkeypatch.setattr(desk_commands, "_write_atomic", flaky)
    answers = rig.queue.process()
    assert [a.idempotency_key for a in answers] == [pause_key]
    assert (rig.root / "HALT").exists(), "applied once"
    assert rig.result(halt).status == "accepted", "its answer is not written yet"
    (failure,) = _failures(rig)
    assert (failure["stage"], failure["error"], failure["command"]) == (
        "answer",
        "PermissionError",
        halt,
    )
    assert rig.queue.process() == [], "still unwritable: nothing new, noted once"
    assert len(_failures(rig)) == 1
    broken.clear()
    (late,) = rig.queue.process()
    assert (late.idempotency_key, late.status) == (halt, "applied")
    assert rig.result(halt).status == "applied"
    assert rig.result(halt).result["answered_by"] == ANSWERED_BY


def test_a_claim_that_cannot_be_renamed_is_contained_and_left_queued(rig, monkeypatch):
    stuck = rig.submit("compose_entry", _COMPOSE)
    rig.clock.advance(1)
    pause_key = rig.submit("pause_entries", {"reason": ""})
    real_rename = desk_commands.rename_noreplace

    def refuse_one(src: Path, dst: Path) -> None:
        if src.name == f"{stuck}.json":
            raise PermissionError(13, "read-only")
        real_rename(src, dst)

    monkeypatch.setattr(desk_commands, "rename_noreplace", refuse_one)
    answers = rig.queue.process()
    assert [a.idempotency_key for a in answers] == [pause_key]
    assert (rig.root / "commands" / f"{stuck}.json").exists(), "left queued, unclaimed"
    assert rig.flows.calls == []
    assert [(f["stage"], f["command"]) for f in _failures(rig)] == [("claim", stuck)]
    monkeypatch.setattr(desk_commands, "rename_noreplace", real_rename)
    (later,) = rig.queue.process()
    assert later.idempotency_key == stuck and rig.flows.calls == [("compose", stuck)]


def test_a_lost_session_in_the_command_stage_still_propagates(rig, monkeypatch):
    """Containment never hides a lost session: a ConnectionError anywhere in
    the stage (here the answer's store lock) still reaches the loop (exit 6)."""
    rig.submit("pause_entries", {"reason": ""})

    def gone(path: Path, payload: bytes) -> None:
        raise ConnectionError("gone")

    monkeypatch.setattr(desk_commands, "_write_atomic", gone)
    with pytest.raises(ConnectionError):
        rig.queue.process()


def test_a_broken_claim_never_stops_recovery_of_the_others(rig, monkeypatch):
    folder = rig.root / "commands"
    bad = rig.submit("resume_all", {"reason": ""}, ttl_s=60)
    rig.clock.advance(1)
    halt = rig.submit("halt", {"reason": "owner died"}, ttl_s=60)
    for key in (bad, halt):
        (folder / f"{key}.json").rename(folder / f"{key}{CLAIMED_SUFFIX}")
    real_write = desk_commands._write_atomic

    def flaky(path: Path, payload: bytes) -> None:
        if path.name == f"{bad}.result.json":
            raise OSError(28, "no space")
        real_write(path, payload)

    monkeypatch.setattr(desk_commands, "_write_atomic", flaky)
    queue = rig.new_queue()
    answers = queue.recover_interrupted()
    assert [a.idempotency_key for a in answers] == [halt]
    assert (rig.root / "HALT").exists()
    assert [(f["stage"], f["command"]) for f in _failures(rig)] == [("answer", bad)]


def test_kill_switches_go_first_and_a_burst_is_spread_over_passes(rig):
    """A burst of entry commands cannot starve the owner's tick: one pass
    takes every queued kill switch first (issued last, still first), then at
    most ``MAX_COMMANDS_PER_PASS`` other commands in issue order; the rest
    wait, queued, for the next pass."""
    limit = desk_commands.MAX_COMMANDS_PER_PASS
    burst = []
    for _ in range(limit + 3):
        burst.append(rig.submit("compose_entry", _COMPOSE))
        rig.clock.advance(1)
    halt = rig.submit("halt", {"reason": ""})
    rig.clock.advance(1)
    flatten = rig.submit("flatten", {"reason": ""})
    first = [a.idempotency_key for a in rig.queue.process()]
    assert first == [halt, flatten, *burst[:limit]]
    assert [key for _, key in rig.flows.calls] == burst[:limit]
    for key in burst[limit:]:
        assert (rig.root / "commands" / f"{key}.json").exists(), "deferred, still queued"
    second = [a.idempotency_key for a in rig.queue.process()]
    assert second == burst[limit:]


def test_a_slow_pass_stops_taking_commands_at_its_wall_time_budget(rig):
    """Each entry command takes 60 % of the pass budget: the first starts at
    0, the second at 0.6 (inside), the third would start at 1.2 (past it) and
    waits for the next pass. A command slower than the whole budget still
    makes progress, one per pass."""
    budget = desk_commands.COMMAND_PASS_BUDGET_S
    ticks = [0.0]
    rig.queue.monotonic = lambda: ticks[0]
    step = [0.6 * budget]

    def slow(command: Any, now: datetime) -> Outcome:
        rig.flows.calls.append(("compose", command.idempotency_key))
        ticks[0] += step[0]
        return applied()

    rig.flows.compose = slow  # type: ignore[method-assign]
    keys = []
    for _ in range(4):
        keys.append(rig.submit("compose_entry", _COMPOSE))
        rig.clock.advance(1)
    assert [a.idempotency_key for a in rig.queue.process()] == keys[:2]
    step[0] = 3 * budget
    assert [a.idempotency_key for a in rig.queue.process()] == keys[2:3]
    assert [a.idempotency_key for a in rig.queue.process()] == keys[3:]
