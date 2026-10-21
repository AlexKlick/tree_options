"""Wave 2 lane G: the web command backend (authenticated, idempotent, broker-free).

Oracles are the frozen contracts, never this lane's code: ``parse_command``,
``command_body_sha256``, ``CommandResult``, ``Wave2RunDir`` paths and the
shared ``FakeCommandAuthority`` / builders of ``tests/unit/trex_fakes.py``.
"""

from __future__ import annotations

import json
import os
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
from tree_options.trex_web.app import create_app
from tree_options.trex_web.command_store import CommandStore
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
    assert post(desk, raw).status_code == 200
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


@pytest.mark.parametrize("kind", ["flatten", "resume_all", "resume_entries"])
def test_owner_only_kinds_are_queued_never_applied_by_the_web(desk, kind):
    desk.run_dir.mkdir(parents=True)
    for name in ("HALT", "PAUSE_ENTRIES"):
        (desk.run_dir / name).write_text("")
    response = post(desk, command_body(desk.clock, kind))
    assert response.status_code == 202 and response.json()["status"] == "accepted"
    # flatten creates no FLATTEN, resume_* removes nothing: the desk applies them
    assert (desk.run_dir / "HALT").exists() and (desk.run_dir / "PAUSE_ENTRIES").exists()
    assert not (desk.run_dir / "FLATTEN").exists()
    assert desk.layout.command(KEY).exists()


# -- safety direction: pause_entries / halt apply directly ---------------------------


@pytest.mark.parametrize(
    ("kind", "kill_file"), [("pause_entries", "PAUSE_ENTRIES"), ("halt", "HALT")]
)
def test_safety_direction_applies_the_kill_file_and_still_queues(desk, kind, kill_file):
    response = post(desk, command_body(desk.clock, kind))
    assert response.status_code == 200
    result = CommandResult.model_validate(response.json())
    assert result.status == "applied" and result.reason is None
    assert result.result == {"kill_file": kill_file, "present": True}
    assert (desk.run_dir / kill_file).exists()
    other = {"PAUSE_ENTRIES", "HALT"} - {kill_file}
    assert not any((desk.run_dir / name).exists() for name in other)
    assert not (desk.run_dir / "FLATTEN").exists()
    # the owner still sees the command (it logs the event; re-applying is idempotent)
    assert parse_command(json.loads(desk.layout.command(KEY).read_bytes())).kind == kind


def test_direct_apply_blocked_by_the_sandbox_degrades_to_queued(desk):
    """The unit may write ONLY <run_dir>/commands: a kill file in the run dir
    root then fails, and the command is queued for the owner instead."""
    desk.layout.commands().mkdir(parents=True)
    os.chmod(desk.run_dir, 0o555)
    try:
        response = post(desk, command_body(desk.clock, "halt"))
    finally:
        os.chmod(desk.run_dir, 0o755)
    assert response.status_code == 202
    result = CommandResult.model_validate(response.json())
    assert result.status == "accepted" and "queued for the desk owner" in result.detail
    assert not (desk.run_dir / "HALT").exists()
    assert parse_command(json.loads(desk.layout.command(KEY).read_bytes())).kind == "halt"


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


def test_a_command_issued_in_the_future_is_not_accepted(desk):
    issued = desk.clock.utc() + timedelta(seconds=600)
    response = post(desk, command_body(desk.clock, "halt", issued=issued))
    assert response.status_code == 422
    assert response.json()["error"] == "command_not_yet_valid"
    assert tree(desk.run_dir) == {}


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


# -- the two legacy POST routes are behind the same authority -------------------------


@pytest.mark.parametrize("action", ["halt", "flatten", "resume"])
def test_legacy_kill_file_route_requires_the_authority(desk, action):
    desk.run_dir.mkdir(parents=True)
    (desk.run_dir / "HALT").write_text("")
    before = tree(desk.run_dir)
    desk.authority.refuse_with = "wrong_peer"
    response = desk.client.post(f"/api/desk/supervised/{action}")
    assert response.status_code == 403
    assert response.json()["detail"]["error"] == "command_unauthorized"
    assert tree(desk.run_dir) == before  # no kill file touched, no audit line
    desk.authority.refuse_with = None
    assert desk.client.post(f"/api/desk/supervised/{action}").status_code == 200
    assert tree(desk.run_dir) != before
    # the route acts on the app's run dir (the one the command backend uses)
    assert (desk.run_dir / "automation.jsonl").exists()


def test_legacy_automation_route_requires_the_authority(desk, monkeypatch):
    import tree_options.trex_web.desk_view as desk_view_mod

    calls: list[tuple] = []

    def record(*args: object) -> dict:
        calls.append(args)
        return {}

    monkeypatch.setattr(desk_view_mod, "automation_action", record)
    desk.authority.refuse_with = "bad_signature"
    assert desk.client.post("/api/desk/automation/desk-lab/run").status_code == 403
    assert desk.client.post("/api/desk/automation/desk-lab/bogus").status_code == 403
    assert calls == []
    desk.authority.refuse_with = None
    assert desk.client.post("/api/desk/automation/desk-lab/run").status_code == 200
    assert calls == [(desk.run_dir, "desk-lab", "run")]


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
