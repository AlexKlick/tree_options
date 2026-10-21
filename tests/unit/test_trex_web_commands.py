"""Wave 2 lane G: the web command backend (authenticated, idempotent, broker-free).

Oracles are the frozen contracts, never this lane's code: ``parse_command``,
``command_body_sha256``, ``CommandResult``, ``Wave2RunDir`` paths and the
shared ``FakeCommandAuthority`` / builders of ``tests/unit/trex_fakes.py``.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from tests.unit.trex_fakes import (
    FakeCommandAuthority,
    SessionClock,
    approve_command,
    entry_proposal,
    mandate_v2,
)
from tree_options.trex.contracts import (
    CommandAuthorityRefused,
    CommandResult,
    RawCommandRequest,
    Wave2RunDir,
    Wave2SupervisedDir,
    actor_binds_request,
    command_body_sha256,
    model_bytes,
    model_document,
    parse_command,
    proposal_bytes,
    proposal_sha256,
    sha256_hex,
)
from tree_options.trex_web import automation
from tree_options.trex_web.app import create_app
from tree_options.trex_web.command_store import CommandStore, command_window_blocker
from tree_options.trex_web.commands_view import LoopbackCommandAuthority

REPO_ROOT = Path(__file__).resolve().parents[2]
COMMANDS = "/api/commands"
KEY = "6f1c2b8e-3d4a-4f5b-9c6d-7e8f9a0b1c2d"
KEY2 = "0a9b8c7d-6e5f-4a3b-8c1d-0e9f8a7b6c5d"
LOOPBACK_ORIGIN = "http://127.0.0.1:8090"


def _iso(instant: datetime) -> str:
    return instant.astimezone(UTC).isoformat().replace("+00:00", "Z")


def command_body(
    clock: SessionClock,
    kind: str = "pause_entries",
    *,
    key: str = KEY,
    args: dict | None = None,
    issued: datetime | None = None,
    ttl_s: int = 120,
) -> dict:
    """A client-authored trex-command/1 body (the backend stamps issued_by)."""
    issued = issued if issued is not None else clock.utc()
    return {
        "schema": "trex-command/1",
        "idempotency_key": key,
        "kind": kind,
        "args": {"reason": "lane G test"} if args is None else args,
        "issued_at": _iso(issued),
        "expires_at": _iso(issued + timedelta(seconds=ttl_s)),
    }


def tree(root: Path) -> dict[str, bytes]:
    """Every file under ``root`` (relative name -> bytes)."""
    if not root.exists():
        return {}
    return {
        str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()
    }


@pytest.fixture
def desk(tmp_path):
    clock = SessionClock()
    authority = FakeCommandAuthority(clock=clock)
    run_dir = tmp_path / "desk-paper"
    supervised = tmp_path / "supervised"
    app = create_app(
        state_dir=str(tmp_path / "legacy"),
        plans_dir=str(tmp_path / "plans"),
        discovery_dir=str(tmp_path / "discovery"),
        desk_state_dir=str(tmp_path / "desk-state"),
        desk_store_dir=str(tmp_path / "desk-store"),
        desk_run_dir=str(run_dir),
        supervised_dir=str(supervised),
        command_authority=authority,
        command_clock=clock,
    )
    client = TestClient(app, base_url=LOOPBACK_ORIGIN, client=("127.0.0.1", 50000))
    return SimpleNamespace(
        clock=clock,
        authority=authority,
        run_dir=run_dir,
        layout=Wave2RunDir(run_dir),
        supervised=supervised,
        client=client,
        app=app,
    )


def post(desk, body: dict | bytes, **headers: str):
    content = body if isinstance(body, bytes) else json.dumps(body).encode()
    return desk.client.post(
        COMMANDS, content=content, headers={"content-type": "application/json", **headers}
    )


# -- authentication: no actor, no effect ----------------------------------------


def test_unauthenticated_command_is_refused_and_writes_nothing(desk):
    desk.authority.refuse_with = "bad_signature"
    response = post(desk, command_body(desk.clock, "halt"))
    assert response.status_code == 403
    doc = response.json()
    assert doc["error"] == "command_unauthorized" and doc["detail"] == "bad_signature"
    assert tree(desk.run_dir) == {}
    # the authority saw the raw request bytes, and refused them
    assert desk.authority.refusals and not desk.authority.actors


def test_the_authority_sees_the_transport_peer_and_raw_bytes(desk):
    raw = json.dumps(command_body(desk.clock)).encode()
    assert post(desk, raw).status_code == 202
    [call] = desk.authority.calls
    assert call.method == "POST" and call.path == COMMANDS
    assert call.peer_host == "127.0.0.1" and call.body == raw
    assert call.headers["content-type"] == "application/json"
    # issued_by is the authority's actor, bound to exactly this request
    [actor] = desk.authority.actors
    stored = parse_command(json.loads(desk.layout.command(KEY).read_bytes()))
    assert stored.issued_by == actor and actor_binds_request(actor, call)


def test_a_non_loopback_peer_is_refused_with_no_effect(desk):
    remote = TestClient(desk.app, base_url=LOOPBACK_ORIGIN, client=("203.0.113.9", 50000))
    response = remote.post(COMMANDS, json=command_body(desk.clock, "halt"))
    assert response.status_code == 403
    assert response.json()["detail"] == "wrong_peer"
    assert tree(desk.run_dir) == {}


def test_client_cannot_author_issued_by(desk):
    body = command_body(desk.clock, "halt")
    body["issued_by"] = {
        "sub": "someone-else",
        "ingress": "family",
        "request_id": "req-x",
        "request_sha256": "0" * 64,
    }
    response = post(desk, body)
    assert response.status_code == 422
    assert response.json()["error"] == "issued_by_is_stamped_by_the_backend"
    assert tree(desk.run_dir) == {}


@pytest.mark.parametrize(
    "mutate",
    [
        lambda b: b.update(kind="arm_live"),
        lambda b: b.update(idempotency_key=KEY.upper()),
        lambda b: b.update(idempotency_key="../../HALT"),
        lambda b: b.update(args={"reason": "x" * 501}),
        lambda b: b.update(extra="field"),
        lambda b: b.update(expires_at=b["issued_at"]),
    ],
    ids=["unknown-kind", "upper-key", "traversal-key", "long-reason", "extra-field", "zero-ttl"],
)
def test_invalid_commands_are_422_and_write_nothing(desk, mutate):
    body = command_body(desk.clock, "halt")
    mutate(body)
    response = post(desk, body)
    assert response.status_code == 422
    assert response.json()["error"] == "invalid_command"
    assert tree(desk.run_dir) == {}


def test_command_ttl_over_300s_is_invalid(desk):
    response = post(desk, command_body(desk.clock, ttl_s=301))
    assert response.status_code == 422
    assert tree(desk.run_dir) == {}


@pytest.mark.parametrize(
    "raw",
    [
        b"not json",
        b"[]",
        b'{"kind": "halt", "kind": "pause_entries"}',
        b'{"reason": NaN}',
    ],
    ids=["not-json", "not-object", "duplicate-key", "nan"],
)
def test_malformed_json_is_422_and_writes_nothing(desk, raw):
    response = post(desk, raw)
    assert response.status_code == 422
    assert response.json()["error"] == "invalid_json"
    assert tree(desk.run_dir) == {}


def test_oversized_body_is_413_and_writes_nothing(desk):
    body = command_body(desk.clock)
    raw = json.dumps(body).encode() + b" " * 70_000
    assert post(desk, raw).status_code == 413
    assert tree(desk.run_dir) == {}


# -- accepted: durably queued for the owner, nothing applied -------------------------


def test_accepted_command_is_queued_atomically_and_nothing_is_applied(desk):
    proposal = entry_proposal()
    args = model_document(approve_command(proposal))["args"]
    response = post(desk, command_body(desk.clock, "approve_entry", args=args))
    assert response.status_code == 202
    result = CommandResult.model_validate(response.json())
    assert result.status == "accepted" and result.reason is None
    assert result.kind == "approve_entry" and result.idempotency_key == KEY

    # the queued command: the contract's file, canonical bytes, actor stamped
    raw = desk.layout.command(KEY).read_bytes()
    stored = parse_command(json.loads(raw))
    assert raw == model_bytes(stored)
    assert stored.issued_by == desk.authority.actors[0]
    assert model_document(stored)["args"] == args
    assert result.command_body_sha256 == command_body_sha256(stored)

    # the durable result is the response
    on_disk = CommandResult.model_validate_json(desk.layout.command_result(KEY).read_bytes())
    assert on_disk == result
    # the two contract files + the store's lock: no staging debris, no kill
    # file, nothing outside commands/, no mandate
    assert sorted(tree(desk.run_dir)) == [
        "commands/.lock",
        f"commands/{KEY}.json",
        f"commands/{KEY}.result.json",
    ]
    assert not desk.supervised.exists()


REASON_KINDS = ("pause_entries", "resume_entries", "halt", "flatten", "resume_all")


@pytest.mark.parametrize("kind", REASON_KINDS)
def test_every_kind_is_queued_and_never_applied_by_the_web(desk, kind):
    """Coordinator decision 2026-10-21: the desk owner applies EVERY kind,
    the safety direction included; the web only queues."""
    desk.run_dir.mkdir(parents=True)
    (desk.run_dir / "HALT").write_text("")
    (desk.run_dir / "PAUSE_ENTRIES").write_text("")
    before = {name: (desk.run_dir / name).read_bytes() for name in ("HALT", "PAUSE_ENTRIES")}
    response = post(desk, command_body(desk.clock, kind))
    assert response.status_code == 202
    result = CommandResult.model_validate(response.json())
    assert result.status == "accepted" and result.reason is None and result.result is None
    # no kill file created, removed or rewritten; nothing outside commands/
    assert {n: (desk.run_dir / n).read_bytes() for n in before} == before
    assert sorted(tree(desk.run_dir)) == [
        "HALT",
        "PAUSE_ENTRIES",
        "commands/.lock",
        f"commands/{KEY}.json",
        f"commands/{KEY}.result.json",
    ]
    assert parse_command(json.loads(desk.layout.command(KEY).read_bytes())).kind == kind


@pytest.mark.parametrize(
    ("kind", "kill_file"), [("pause_entries", "PAUSE_ENTRIES"), ("halt", "HALT")]
)
def test_the_safety_direction_writes_no_kill_file_either(desk, kind, kill_file):
    response = post(desk, command_body(desk.clock, kind))
    assert response.status_code == 202 and response.json()["status"] == "accepted"
    assert not (desk.run_dir / kill_file).exists()
    assert sorted(tree(desk.run_dir)) == [
        "commands/.lock",
        f"commands/{KEY}.json",
        f"commands/{KEY}.result.json",
    ]


@pytest.mark.parametrize("kind", ["halt", "pause_entries", "flatten"])
def test_a_web_that_may_write_only_commands_queues_every_kind(desk, kind):
    """The unit may write ONLY <run_dir>/commands: a read-only run dir root
    changes nothing, because the web never writes there."""
    desk.layout.commands().mkdir(parents=True)
    os.chmod(desk.run_dir, 0o555)
    try:
        response = post(desk, command_body(desk.clock, kind))
    finally:
        os.chmod(desk.run_dir, 0o755)
    assert response.status_code == 202
    result = CommandResult.model_validate(response.json())
    assert result.status == "accepted" and result.detail == ""
    assert sorted(p.name for p in desk.run_dir.iterdir()) == ["commands"]
    assert parse_command(json.loads(desk.layout.command(KEY).read_bytes())).kind == kind


# -- idempotency ----------------------------------------------------------------------


def test_same_key_same_body_is_a_duplicate_naming_the_stored_outcome(desk):
    body = command_body(desk.clock, "flatten")
    first = post(desk, body)
    assert first.status_code == 202
    before = tree(desk.run_dir)

    # a browser retry: same body, different raw bytes and request
    retry = post(desk, json.dumps(body, indent=2).encode())
    assert retry.status_code == 200
    dup = CommandResult.model_validate(retry.json())
    assert dup.status == "duplicate" and dup.reason == "duplicate_command"
    assert dup.command_body_sha256 == first.json()["command_body_sha256"]
    assert dup.result is not None and dup.result["first_status"] == "accepted"
    assert dup.result["first_result"] == first.json()
    assert tree(desk.run_dir) == before  # nothing rewritten

    # the owner moved the result on: the duplicate names the latest outcome
    applied = CommandResult.model_validate(
        {**first.json(), "status": "applied", "result": {"flattened": 0}}
    )
    desk.layout.command_result(KEY).write_bytes(model_bytes(applied))
    desk.layout.command(KEY).rename(desk.layout.commands() / f"{KEY}.claimed")
    again = post(desk, body).json()
    assert again["status"] == "duplicate" and again["result"]["first_status"] == "applied"
    assert not desk.layout.command(KEY).exists()  # never re-queued after a claim


def test_a_retry_after_expiry_is_still_a_duplicate_not_expired(desk):
    body = command_body(desk.clock, "flatten", ttl_s=60)
    assert post(desk, body).status_code == 202
    desk.clock.advance(3600)
    retry = post(desk, body)
    assert retry.status_code == 200 and retry.json()["status"] == "duplicate"


def test_same_key_different_body_is_409_command_body_mismatch(desk):
    body = command_body(desk.clock, "flatten")
    assert post(desk, body).status_code == 202
    before = tree(desk.run_dir)
    changed = {**body, "args": {"reason": "a different reason"}}
    response = post(desk, changed)
    assert response.status_code == 409
    result = CommandResult.model_validate(response.json())
    assert result.status == "refused" and result.reason == "command_body_mismatch"
    assert tree(desk.run_dir) == before


def test_another_principal_reusing_a_key_is_a_mismatch(desk):
    body = command_body(desk.clock, "flatten")
    assert post(desk, body).status_code == 202  # loopback operator
    family = desk.authority.family_actor("owner@example.com", "POST", COMMANDS)
    response = post(desk, body, **{"x-family-actor": family})
    assert response.status_code == 409
    assert response.json()["reason"] == "command_body_mismatch"
    assert desk.authority.actors[-1].ingress == "family"


def test_expired_command_is_refused_and_never_queued(desk):
    issued = desk.clock.utc() - timedelta(seconds=120)
    body = command_body(desk.clock, "halt", issued=issued, ttl_s=120)  # expires == now
    response = post(desk, body)
    assert response.status_code == 409
    result = CommandResult.model_validate(response.json())
    assert result.status == "refused" and result.reason == "command_expired"
    assert not (desk.run_dir / "HALT").exists()
    assert not desk.layout.command(KEY).exists()
    # the refusal is durable: the key is spent, a retry is a duplicate of it
    assert CommandResult.model_validate_json(desk.layout.command_result(KEY).read_bytes()) == result
    retry = post(desk, body).json()
    assert retry["status"] == "duplicate" and retry["result"]["first_status"] == "refused"


# -- one validity rule for every kind, web and owner alike ---------------------------
#
# The rule (stated, not derived from the code): a command is valid while
# issued_at <= now + 60 s (client clock skew) and now < expires_at. Beyond
# the skew it is refused ``command_not_yet_valid``; from expires_at on it is
# refused ``command_expired``. Both refusals are durable (the key is spent).

#: (issued_at minus now in seconds, ttl in seconds, expected refusal or None)
WINDOW_GRID = [
    (600, 120, "command_not_yet_valid"),
    (61, 120, "command_not_yet_valid"),
    (60, 120, None),
    (30, 120, None),
    (0, 120, None),
    (-119, 120, None),
    (-120, 120, "command_expired"),
    (-300, 300, "command_expired"),
]


def approve_args() -> dict:
    return model_document(approve_command(entry_proposal()))["args"]


def kind_args(kind: str) -> dict | None:
    return approve_args() if kind == "approve_entry" else None


@pytest.mark.parametrize(("lead_s", "ttl_s", "refusal"), WINDOW_GRID)
def test_pause_and_approve_get_the_same_verdict_from_the_web(desk, lead_s, ttl_s, refusal):
    issued = desk.clock.utc() + timedelta(seconds=lead_s)
    verdicts = {}
    for n, kind in enumerate(("pause_entries", "approve_entry")):
        key = (KEY, KEY2)[n]
        body = command_body(desk.clock, kind, key=key, args=kind_args(kind), issued=issued)
        body["expires_at"] = _iso(issued + timedelta(seconds=ttl_s))
        response = post(desk, body)
        result = CommandResult.model_validate(response.json())
        verdicts[kind] = (response.status_code, result.status, result.reason)
        if refusal is None:
            assert desk.layout.command(key).exists()
        else:
            # refused durably and never queued: the owner never sees it
            assert not desk.layout.command(key).exists()
            stored = CommandResult.model_validate_json(desk.layout.command_result(key).read_bytes())
            assert stored == result
    expected = (202, "accepted", None) if refusal is None else (409, "refused", refusal)
    assert verdicts == {"pause_entries": expected, "approve_entry": expected}
    assert not (desk.run_dir / "PAUSE_ENTRIES").exists()


@pytest.mark.parametrize(("lead_s", "ttl_s", "refusal"), WINDOW_GRID)
def test_the_owner_applies_exactly_what_the_web_accepts(tmp_path, desk, lead_s, ttl_s, refusal):
    """The same instant, the same command: the web's intake verdict and the
    owner-side ``apply_entries_command`` verdict never disagree."""
    issued = desk.clock.utc() + timedelta(seconds=lead_s)
    body = command_body(desk.clock, "pause_entries", issued=issued)
    body["expires_at"] = _iso(issued + timedelta(seconds=ttl_s))
    actor = desk.authority.verify(desk.authority.make_request(b"{}"))
    command = parse_command({**body, "issued_by": model_document(actor)})
    now = desk.clock()
    assert command_window_blocker(command, now) == refusal
    owner_dir = tmp_path / "owner-run"
    if refusal is None:
        assert automation.apply_entries_command(owner_dir, command, now=now)["entries_paused"]
    else:
        with pytest.raises(ValueError, match=refusal):
            automation.apply_entries_command(owner_dir, command, now=now)
        assert not (owner_dir / "PAUSE_ENTRIES").exists()


def test_a_pause_accepted_ahead_of_its_issued_at_is_not_dropped_by_the_owner(tmp_path, desk):
    """The review P2: the web accepted a pause issued 30 s ahead (client
    skew); the owner claimed it 1 s later, still before issued_at. The owner
    must apply it, not refuse it on a different rule."""
    issued = desk.clock.utc() + timedelta(seconds=30)
    response = post(desk, command_body(desk.clock, "pause_entries", issued=issued))
    assert response.status_code == 202
    queued = parse_command(json.loads(desk.layout.command(KEY).read_bytes()))
    desk.clock.advance(1)
    doc = automation.apply_entries_command(tmp_path / "owner-run", queued, now=desk.clock())
    assert doc["entries_paused"] is True and doc["changed"] is True


@pytest.mark.parametrize("kind", ["approve_entry", "halt", "flatten", "resume_all", "backup_state"])
def test_the_rule_is_kind_blind(desk, kind):
    args = {"backup_state": {}}.get(kind, kind_args(kind))
    actor = desk.authority.verify(desk.authority.make_request(b"{}"))
    for lead_s, ttl_s, refusal in WINDOW_GRID:
        issued = desk.clock.utc() + timedelta(seconds=lead_s)
        body = command_body(desk.clock, kind, args=args, issued=issued, ttl_s=ttl_s)
        command = parse_command({**body, "issued_by": model_document(actor)})
        assert command_window_blocker(command, desk.clock()) == refusal, (kind, lead_s)


def test_an_unreadable_stored_result_is_503_never_overwritten(desk):
    desk.layout.commands().mkdir(parents=True)
    desk.layout.command_result(KEY).write_text("torn")
    response = post(desk, command_body(desk.clock, "halt"))
    assert response.status_code == 503
    assert desk.layout.command_result(KEY).read_text() == "torn"
    assert not (desk.run_dir / "HALT").exists()


def test_concurrent_same_key_submissions_are_written_once(desk):
    authority = FakeCommandAuthority(clock=desk.clock)
    raw = json.dumps(command_body(desk.clock, "flatten")).encode()
    doc = json.loads(raw)
    commands = []
    for n in range(8):
        actor = authority.verify(authority.make_request(raw, request_id=f"req-{n}"))
        commands.append(parse_command({**doc, "issued_by": model_document(actor)}))
    store = CommandStore(desk.run_dir)
    statuses: list[str] = []
    barrier = threading.Barrier(len(commands))

    def submit(command):
        barrier.wait()
        statuses.append(store.submit(command, now=desk.clock.utc()).result.status)

    threads = [threading.Thread(target=submit, args=(c,)) for c in commands]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(statuses) == ["accepted"] + ["duplicate"] * 7
    assert sorted(tree(desk.run_dir)) == [
        "commands/.lock",
        f"commands/{KEY}.json",
        f"commands/{KEY}.result.json",
    ]


def test_a_crash_between_result_and_queue_is_finished_by_the_retry(desk):
    """The result is written before the command becomes visible (so the
    owner can never be overwritten); a crash in between leaves the staged
    command, which the next same-body submission publishes."""
    store = CommandStore(desk.run_dir)
    command = parse_command(
        {
            **command_body(desk.clock, "flatten"),
            "issued_by": model_document(desk.authority.verify(desk.authority.make_request(b"{}"))),
        }
    )
    first = store.submit(command, now=desk.clock.utc())
    assert first.result.status == "accepted"
    # rewind to the crash point: staged, result present, command not visible
    staged = desk.layout.commands() / f".{KEY}.json.staged"
    desk.layout.command(KEY).rename(staged)
    retry = store.submit(command, now=desk.clock.utc())
    assert retry.result.status == "duplicate"
    assert desk.layout.command(KEY).read_bytes() == model_bytes(command)
    assert not staged.exists()


@pytest.mark.parametrize("owner_status", ["applied", "failed", "refused"])
def test_staged_debris_is_never_published_once_the_owner_moved_the_result_on(desk, owner_status):
    """Only an ``accepted`` result means "queued by the web, not yet seen by
    the owner". A staged file beside any other outcome is debris: publishing
    it would hand the owner a command it already finished."""
    store = CommandStore(desk.run_dir)
    command = parse_command(
        {
            **command_body(desk.clock, "flatten"),
            "issued_by": model_document(desk.authority.verify(desk.authority.make_request(b"{}"))),
        }
    )
    assert store.submit(command, now=desk.clock.utc()).result.status == "accepted"
    accepted = CommandResult.model_validate_json(desk.layout.command_result(KEY).read_bytes())
    moved_on = {**model_document(accepted), "status": owner_status, "result": {"done": True}}
    if owner_status != "applied":
        moved_on["reason"] = "kill_file_present"
    desk.layout.command_result(KEY).write_bytes(model_bytes(CommandResult.model_validate(moved_on)))
    staged = desk.layout.commands() / f".{KEY}.json.staged"
    desk.layout.command(KEY).rename(staged)  # as if left behind
    retry = store.submit(command, now=desk.clock.utc())
    assert retry.result.status == "duplicate"
    assert retry.result.result is not None
    assert retry.result.result["first_status"] == owner_status
    assert not desk.layout.command(KEY).exists() and not staged.exists()


# -- read APIs ---------------------------------------------------------------------


def test_get_command_returns_the_stored_result(desk):
    posted = post(desk, command_body(desk.clock, "flatten")).json()
    response = desk.client.get(f"{COMMANDS}/{KEY}")
    assert response.status_code == 200 and response.json() == posted
    assert desk.client.get(f"{COMMANDS}/{KEY2}").status_code == 404
    assert desk.client.get(f"{COMMANDS}/not-a-uuid").status_code == 404
    assert desk.client.get(f"{COMMANDS}/..%2F..%2Fowner").status_code == 404


def _write_proposal(desk, proposal) -> Path:
    path = desk.layout.proposal(proposal.proposal_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(proposal_bytes(proposal))
    return path


def test_proposals_list_pending_with_the_bindings_an_approval_repeats(desk):
    desk.clock.to(datetime(2026, 10, 22, 14, 1, tzinfo=UTC))
    proposal = entry_proposal()
    _write_proposal(desk, proposal)
    response = desk.client.get("/api/proposals")
    assert response.status_code == 200
    doc = response.json()
    assert doc["problems"] == [] and doc["execution_enabled"] is False
    [row] = doc["proposals"]
    assert row["proposal_id"] == proposal.proposal_id
    assert row["proposal_sha256"] == proposal_sha256(proposal)
    assert row["status"] == "pending" and row["asset_kind"] == "equity"
    assert row["proposal"] == model_document(proposal)
    # what approve_command binds is exactly what the page shows
    args = model_document(approve_command(proposal))["args"]
    assert args["proposal_sha256"] == row["proposal_sha256"]
    assert args["owner_epoch"] == row["proposal"]["review"]["owner_epoch"]


def test_proposal_states_follow_tombstones_mandates_and_deadlines(desk):
    desk.clock.to(datetime(2026, 10, 22, 14, 3, tzinfo=UTC))
    pending = entry_proposal()
    rejected = entry_proposal(sleeve_ref=None)
    approved = entry_proposal(program_id="prog-approved")
    _write_proposal(desk, pending)
    _write_proposal(desk, rejected)
    _write_proposal(desk, approved)
    desk.layout.proposal_rejected(rejected.proposal_id).write_text("{}")
    mandate = mandate_v2(approved)
    sup = Wave2SupervisedDir(desk.supervised)
    sup.mandates().mkdir(parents=True)
    sup.mandate(mandate.intent_id).write_bytes(model_bytes(mandate))
    states = {
        r["proposal_id"]: r["status"] for r in desk.client.get("/api/proposals").json()["proposals"]
    }
    assert states == {
        pending.proposal_id: "pending",
        rejected.proposal_id: "rejected",
        approved.proposal_id: "approved",
    }
    # the mandate is consumed (permit minted): sent
    sup.mandate(mandate.intent_id).rename(sup.mandate_consumed(mandate.intent_id))
    rows = desk.client.get("/api/proposals").json()["proposals"]
    assert {r["proposal_id"]: r["status"] for r in rows}[approved.proposal_id] == "consumed"
    # past the send deadline an unapproved proposal is expired
    desk.clock.to(datetime(2026, 10, 22, 14, 10, tzinfo=UTC))
    rows = desk.client.get("/api/proposals").json()["proposals"]
    assert {r["proposal_id"]: r["status"] for r in rows}[pending.proposal_id] == "expired"


def test_bad_proposal_files_are_reported_never_dropped(desk):
    proposal = entry_proposal()
    path = _write_proposal(desk, proposal)
    # a non-canonical encoding of a valid proposal: its file hash would lie
    path.write_text(json.dumps(model_document(proposal), indent=1))
    (desk.layout.proposals() / ("f" * 64 + ".json")).write_text("not json")
    doc = desk.client.get("/api/proposals").json()
    assert doc["proposals"] == []
    reasons = {p["file"]: p["reason"] for p in doc["problems"]}
    assert reasons == {
        f"{proposal.proposal_id}.json": "proposal_not_canonical",
        f"{'f' * 64}.json": "proposal_unreadable",
    }


def test_no_proposals_dir_is_an_empty_list_not_an_error(desk):
    doc = desk.client.get("/api/proposals").json()
    assert doc["proposals"] == [] and doc["problems"] == []
    assert not desk.run_dir.exists()


# -- the legacy POST routes: authenticated, translated, QUEUED ------------------------

#: legacy verb -> the trex-command/1 kind it queues
LEGACY_KINDS = {"halt": "halt", "flatten": "flatten", "resume": "resume_all"}


def legacy(desk, action: str, **headers: str):
    return desk.client.post(f"/api/desk/supervised/{action}", headers=headers)


def queued_commands(desk) -> list:
    folder = desk.layout.commands()
    if not folder.is_dir():
        return []
    return [
        parse_command(json.loads(p.read_bytes()))
        for p in sorted(folder.glob("*.json"))
        if not p.name.endswith(".result.json")
    ]


@pytest.mark.parametrize("action", sorted(LEGACY_KINDS))
def test_legacy_route_requires_the_authority_and_queues_with_the_actor(desk, action):
    desk.run_dir.mkdir(parents=True)
    (desk.run_dir / "HALT").write_text("")
    before = tree(desk.run_dir)
    desk.authority.refuse_with = "wrong_peer"
    response = legacy(desk, action)
    assert response.status_code == 403
    assert response.json()["detail"]["error"] == "command_unauthorized"
    assert tree(desk.run_dir) == before
    desk.authority.refuse_with = None

    response = legacy(desk, action)
    assert response.status_code == 202
    doc = response.json()
    [command] = queued_commands(desk)
    assert command.kind == LEGACY_KINDS[action]
    assert doc["command_key"] == command.idempotency_key
    assert doc["kind"] == command.kind
    assert doc["result_url"] == f"{COMMANDS}/{command.idempotency_key}"
    # issued_by is the AUTHENTICATED actor of this very request, not "cockpit"
    actor = desk.authority.actors[-1]
    assert command.issued_by == actor and actor.ingress == "loopback"
    assert actor_binds_request(actor, desk.authority.calls[-1])
    # stamped on the app's clock, inside the contract's command lifetime
    assert command.issued_at == desk.clock.utc()
    assert (command.expires_at - command.issued_at).total_seconds() == 300
    # the durable result is the queued (accepted) outcome the response carries
    result = CommandResult.model_validate(doc["queued"])
    assert result.status == "accepted" and result.kind == command.kind
    assert result.command_body_sha256 == command_body_sha256(command)
    stored = desk.layout.command_result(command.idempotency_key).read_bytes()
    assert CommandResult.model_validate_json(stored) == result
    # nothing applied: the kill files stand as they were, no audit line
    assert (desk.run_dir / "HALT").read_bytes() == before["HALT"]
    assert not (desk.run_dir / "FLATTEN").exists()
    assert not (desk.run_dir / "automation.jsonl").exists()
    assert doc["kill_files"] == ["HALT"]
    assert sorted(set(tree(desk.run_dir)) - set(before)) == [
        "commands/.lock",
        f"commands/{command.idempotency_key}.json",
        f"commands/{command.idempotency_key}.result.json",
    ]


def test_legacy_halt_route_queues_a_halt_command_and_writes_no_kill_file(desk):
    response = legacy(desk, "halt")
    assert response.status_code == 202
    [command] = queued_commands(desk)
    assert command.kind == "halt"
    assert not (desk.run_dir / "HALT").exists()
    assert sorted(p.name for p in desk.run_dir.iterdir()) == ["commands"]
    # the owner's result is what the read API then serves
    key = response.json()["command_key"]
    applied = CommandResult.model_validate(
        {**response.json()["queued"], "status": "applied", "result": {"kill_file": "HALT"}}
    )
    desk.layout.command_result(key).write_bytes(model_bytes(applied))
    assert desk.client.get(f"{COMMANDS}/{key}").json() == model_document(applied)


def test_each_legacy_post_is_its_own_command(desk):
    first = legacy(desk, "halt").json()["command_key"]
    second = legacy(desk, "halt").json()["command_key"]
    assert first != second
    assert [c.idempotency_key for c in queued_commands(desk)] == sorted([first, second])


def test_legacy_flatten_under_halt_still_warns_and_writes_nothing(desk):
    desk.run_dir.mkdir(parents=True)
    (desk.run_dir / "HALT").write_text("")
    doc = legacy(desk, "flatten").json()
    assert doc["kind"] == "flatten" and "HALT" in doc["warning"]
    assert not (desk.run_dir / "FLATTEN").exists()
    (desk.run_dir / "HALT").unlink()
    assert "warning" not in legacy(desk, "flatten").json()


def test_legacy_resume_never_lifts_a_standing_entries_pause(desk):
    """The cockpit's resume verb meant HALT and FLATTEN only. It queues
    ``resume_all`` (contract default 11: the superset), so while entries are
    paused it is refused rather than widened to lift the pause."""
    desk.run_dir.mkdir(parents=True)
    (desk.run_dir / "HALT").write_text("")
    (desk.run_dir / "PAUSE_ENTRIES").write_text("")
    response = legacy(desk, "resume")
    assert response.status_code == 409
    assert response.json()["error"] == "legacy_resume_would_lift_entries_pause"
    assert queued_commands(desk) == []
    assert (desk.run_dir / "HALT").exists() and (desk.run_dir / "PAUSE_ENTRIES").exists()


def test_legacy_resume_never_lifts_a_queued_entries_pause(desk):
    assert post(desk, command_body(desk.clock, "pause_entries")).status_code == 202
    response = legacy(desk, "resume")
    assert response.status_code == 409
    assert [c.kind for c in queued_commands(desk)] == ["pause_entries"]
    # the owner claims and applies the pause: now the file is what refuses
    desk.layout.command(KEY).rename(desk.layout.commands() / f"{KEY}.claimed")
    (desk.run_dir / "PAUSE_ENTRIES").write_text("")
    assert legacy(desk, "resume").status_code == 409
    # entries resumed by the owner: the legacy resume is a resume_all again
    (desk.run_dir / "PAUSE_ENTRIES").unlink()
    response = legacy(desk, "resume")
    assert response.status_code == 202 and response.json()["kind"] == "resume_all"


def test_legacy_unknown_action_is_422_after_authentication(desk):
    assert legacy(desk, "arm").status_code == 422
    desk.authority.refuse_with = "bad_signature"
    assert legacy(desk, "arm").status_code == 403
    assert not desk.run_dir.exists()


def test_legacy_automation_route_is_authenticated_and_never_applied(desk, monkeypatch):
    """Timer control has no trex-command/1 kind, so it cannot be queued for
    the owner: the web refuses it after authentication and runs nothing."""
    calls: list[object] = []

    def record(*args: object, **kwargs: object) -> object:
        calls.append(args)
        raise AssertionError("the web must not run systemctl for a POST")

    monkeypatch.setattr(subprocess, "run", record)
    desk.authority.refuse_with = "bad_signature"
    assert desk.client.post("/api/desk/automation/desk-lab/run").status_code == 403
    desk.authority.refuse_with = None
    response = desk.client.post("/api/desk/automation/desk-lab/run")
    assert response.status_code == 501
    assert response.json()["error"] == "automation_not_queueable"
    assert desk.client.post("/api/desk/automation/trex-monitor/run").status_code == 404
    assert desk.client.post("/api/desk/automation/desk-lab/restart").status_code == 422
    assert calls == []
    assert not desk.run_dir.exists()


# -- the loopback authority (workspace_guard's rules over the raw request) ----------


def raw(
    *,
    peer: str = "127.0.0.1",
    host: str = "127.0.0.1:8090",
    origin: str | None = LOOPBACK_ORIGIN,
    body: bytes = b'{"kind":"halt"}',
    **extra: str,
) -> RawCommandRequest:
    headers = {"host": host, **{k.replace("_", "-"): v for k, v in extra.items()}}
    if origin is not None:
        headers["origin"] = origin
    return RawCommandRequest(
        method="POST", path=COMMANDS, headers=headers, body=body, peer_host=peer, request_id="r-1"
    )


def test_loopback_authority_accepts_an_enabled_same_origin_loopback_request():
    authority = LoopbackCommandAuthority(enabled=lambda: True)
    for request in (
        raw(),
        raw(peer="::1", host="[::1]:8090", origin="http://[::1]:8090"),
        raw(host="localhost:8090", origin="http://localhost:8090", sec_fetch_site="same-origin"),
    ):
        actor = authority.verify(request)
        assert actor.ingress == "loopback" and actor.sub == "loopback-operator"
        assert actor_binds_request(actor, request)
        assert actor.request_sha256 == sha256_hex(request.body)


@pytest.mark.parametrize(
    ("request_", "detail"),
    [
        (raw(peer="203.0.113.9"), "wrong_peer"),
        (raw(peer="testclient"), "wrong_peer"),
        # the family portal forwards from loopback with the PUBLIC host and origin
        (
            raw(host="pop-os.tail2b3f82.ts.net", origin="https://pop-os.tail2b3f82.ts.net"),
            "host_not_loopback",
        ),
        (raw(origin=None), "origin_mismatch"),
        (raw(origin="null"), "origin_mismatch"),
        (raw(origin="http://127.0.0.1:8090.evil.example"), "origin_mismatch"),
        (raw(origin="https://127.0.0.1:8090"), "origin_mismatch"),
        (raw(host="user@127.0.0.1:8090", origin="http://user@127.0.0.1:8090"), "host_userinfo"),
        (raw(sec_fetch_site="cross-site"), "cross_site"),
        (raw(x_forwarded_for="127.0.0.1"), "forwarded"),
        (raw(forwarded="for=127.0.0.1"), "forwarded"),
        (raw(x_real_ip="127.0.0.1"), "forwarded"),
        (raw(x_family_actor="e30.sig"), "family_ingress_unavailable"),
    ],
    ids=lambda v: v if isinstance(v, str) else None,
)
def test_loopback_authority_refuses_everything_else(request_, detail):
    authority = LoopbackCommandAuthority(enabled=lambda: True)
    with pytest.raises(CommandAuthorityRefused) as refused:
        authority.verify(request_)
    assert refused.value.reason == "command_unauthorized"
    assert refused.value.detail == detail


def test_loopback_authority_is_off_unless_explicitly_enabled(monkeypatch):
    monkeypatch.delenv("TREX_WORKSPACE_CONTROLS", raising=False)
    with pytest.raises(CommandAuthorityRefused) as refused:
        LoopbackCommandAuthority().verify(raw())
    assert refused.value.detail == "controls_disabled"
    monkeypatch.setenv("TREX_WORKSPACE_CONTROLS", "1")
    assert LoopbackCommandAuthority().verify(raw()).ingress == "loopback"


def test_default_app_authority_is_loopback_and_refuses_the_portal_shape(tmp_path, monkeypatch):
    monkeypatch.setenv("TREX_WORKSPACE_CONTROLS", "1")
    clock = SessionClock()
    run_dir = tmp_path / "desk-paper"
    app = create_app(
        state_dir=str(tmp_path / "legacy"),
        plans_dir=str(tmp_path / "plans"),
        discovery_dir=str(tmp_path / "discovery"),
        desk_state_dir=str(tmp_path / "desk-state"),
        desk_run_dir=str(run_dir),
        supervised_dir=str(tmp_path / "supervised"),
        command_clock=clock,
    )
    local = TestClient(app, base_url=LOOPBACK_ORIGIN, client=("127.0.0.1", 50000))
    portal = TestClient(
        app, base_url="https://pop-os.tail2b3f82.ts.net", client=("127.0.0.1", 50000)
    )
    body = command_body(clock, "flatten")
    refused = portal.post(
        COMMANDS, json=body, headers={"origin": "https://pop-os.tail2b3f82.ts.net"}
    )
    assert refused.status_code == 403 and tree(run_dir) == {}
    assert portal.post("/api/desk/supervised/halt").status_code == 403
    assert not (run_dir / "HALT").exists()
    accepted = local.post(COMMANDS, json=body, headers={"origin": LOOPBACK_ORIGIN})
    assert accepted.status_code == 202
    actor = parse_command(json.loads(Wave2RunDir(run_dir).command(KEY).read_bytes())).issued_by
    assert actor.ingress == "loopback" and actor.sub == "loopback-operator"


# -- the repo unit: the web may write only the desk's command queue -------------------


def test_the_repo_web_unit_may_write_only_the_desk_command_queue():
    text = (REPO_ROOT / "deploy" / "trex" / "trex-web.service").read_text()
    [line] = re.findall(r"^ReadWritePaths=(.*)$", text, re.MULTILINE)
    paths = line.split()
    run_dir = "/home/alexk/.local/state/trex/desk-paper"
    # no grant covers the run dir itself (kill files, specs, ledger) ...
    covering = [p for p in paths if (run_dir + "/").startswith(p.rstrip("/") + "/")]
    assert covering == []
    # ... only its commands/ subdirectory, and nothing of the supervised dir
    assert [p for p in paths if p.startswith(run_dir)] == [f"{run_dir}/commands"]
    assert not any("trex/supervised" in p for p in paths)
    assert "ProtectSystem=strict" in text and "ProtectHome=read-only" in text
    # the installed drop-in that still grants the whole run dir is called out
    assert "automation.conf" in text and "narrow that drop-in" in text


# -- broker-free: importing and serving trex_web never loads ib_async -----------------


def test_trex_web_never_imports_ib_async(tmp_path):
    script = f"""
import importlib, json, pkgutil, sys
import tree_options.trex_web as pkg
for info in pkgutil.iter_modules(pkg.__path__):
    importlib.import_module(f"tree_options.trex_web.{{info.name}}")
from fastapi.testclient import TestClient
from tree_options.trex_web.app import create_app
root = {str(tmp_path)!r}
app = create_app(state_dir=root + "/legacy", plans_dir=root + "/plans",
                 discovery_dir=root + "/discovery", desk_state_dir=root + "/desk-state",
                 desk_store_dir=root + "/desk-store", desk_run_dir=root + "/run",
                 supervised_dir=root + "/supervised")
client = TestClient(app, base_url="http://127.0.0.1:8090", client=("127.0.0.1", 1))
codes = [client.get(p).status_code for p in ("/api/proposals", "/api/portfolio",
         "/api/commands/6f1c2b8e-3d4a-4f5b-9c6d-7e8f9a0b1c2d")]
codes.append(client.post("/api/commands", json={{}}).status_code)
loaded = sorted(m for m in sys.modules if m.split(".")[0] in ("ib_async", "ib_insync", "eventkit"))
print(json.dumps({{"codes": codes, "loaded": loaded}}))
"""
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path),
        "PYTHONPATH": str(REPO_ROOT / "src"),
        "TREX_DESK_RUN_DIR": str(tmp_path / "run"),
        "TREX_SUPERVISED_DIR": str(tmp_path / "supervised"),
    }
    done = subprocess.run(
        [sys.executable, "-B", "-c", script],
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
        timeout=180,
        check=False,
    )
    assert done.returncode == 0, done.stderr[-2000:]
    report = json.loads(done.stdout.strip().splitlines()[-1])
    assert report["loaded"] == []
    # the routes really ran: 200 reads, 404 unknown key, 403 unauthenticated POST
    assert report["codes"] == [200, 200, 404, 403]
