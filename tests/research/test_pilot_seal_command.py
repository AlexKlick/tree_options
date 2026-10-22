"""Wave 4 lane I: the ``seal_registration`` command kind, end to end.

These tests are GUARDED: they skip until the coordinator applies the
additive amendment ``docs/product/pilot-seal-command.patch`` (the new kind in
``trex-command/1``, ``SealRegistrationArgs`` moved into ``contracts.commands``
and re-exported by ``contracts.wave3``, the desk applying it as a file kind).
Then they drive the real path: the web's ``CommandStore`` queues the
command, the desk's ``CommandQueue`` applies it with the gateway down (a
file kind never needs the broker), and the sealed file is the oracle.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, get_args

import pytest

from tree_options.research.pilot.store import PilotStore
from tree_options.time.sessions import shift_instant
from tree_options.trex.contracts import wave3 as w
from tree_options.trex.contracts.canonical import model_document, sha256_hex
from tree_options.trex.contracts.commands import Actor, CommandKind, parse_command

pytestmark = pytest.mark.skipif(
    "seal_registration" not in get_args(CommandKind),
    reason="seal_registration amendment (docs/product/pilot-seal-command.patch) not applied",
)

REPO = Path(__file__).resolve().parents[2]
DRAFTS = REPO / "data" / "research" / "pilot"
OWNER = "owner@example.com"
OBSERVATION = "qsl-observation"
NOW = datetime(2026, 10, 22, 14, 0, tzinfo=UTC)
SEAL_FIELDS = ("status", "sealed_by", "sealed_at", "sha256")


def reviewed_sha(program_id: str) -> str:
    document = json.loads((DRAFTS / f"{program_id}.draft.json").read_text())
    for name in SEAL_FIELDS:
        document.pop(name)
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def command(args: dict[str, Any], *, sub: str = OWNER, ingress: str = "family") -> Any:
    body = {
        "schema": "trex-command/1",
        "idempotency_key": str(uuid.uuid4()),
        "kind": "seal_registration",
        "args": args,
        "issued_at": NOW.isoformat(),
        "expires_at": shift_instant(NOW, 120).isoformat(),
    }
    raw = json.dumps(body).encode()
    actor = Actor(sub=sub, ingress=ingress, request_id="req-1", request_sha256=sha256_hex(raw))  # type: ignore[arg-type]
    return parse_command({**body, "issued_by": model_document(actor)})


@pytest.fixture()
def desk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    pilot = tmp_path / "pilot"
    PilotStore(pilot).import_drafts(DRAFTS)
    monkeypatch.setenv("TREX_PILOT_DIR", str(pilot))
    monkeypatch.setenv("TREX_OWNER_EMAILS", OWNER)
    root = tmp_path / "desk"
    root.mkdir()
    return root


def apply(root: Path, cmd: Any) -> Any:
    from tree_options.trex.desk_commands import FILE_KINDS, NO_SESSION_ENTRIES, CommandQueue
    from tree_options.trex_web.command_store import CommandStore

    store = CommandStore(root)
    assert store.submit(cmd, now=NOW).result.status == "accepted"
    queue = CommandQueue(
        root,
        flows=NO_SESSION_ENTRIES,
        clock=lambda: NOW,
        owner_epoch="owner-test",
        connected=lambda: False,
    )
    assert "seal_registration" in FILE_KINDS
    queue.process(kinds=FILE_KINDS)
    result = store.read_result(cmd.idempotency_key)
    assert result is not None
    return result


def test_the_args_model_is_shared_by_both_contract_modules() -> None:
    from tree_options.trex.contracts import commands

    assert w.SealRegistrationArgs is commands.SealRegistrationArgs
    cmd = command({"program_id": OBSERVATION, "registration_sha256": "a" * 64})
    assert isinstance(cmd.args, w.SealRegistrationArgs)


@pytest.mark.parametrize(
    "args",
    [
        {"program_id": OBSERVATION},
        {"program_id": "../x", "registration_sha256": "a" * 64},
        {"program_id": OBSERVATION, "registration_sha256": "A" * 64},
        {"program_id": OBSERVATION, "registration_sha256": "a" * 64, "extra": 1},
    ],
    ids=["no-hash", "unsafe-id", "upper-hash", "extra-field"],
)
def test_bad_args_refuse_at_parse(args: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        command(args)


def test_the_owner_seals_through_the_desk(desk: Path, tmp_path: Path) -> None:
    sha = reviewed_sha(OBSERVATION)
    result = apply(desk, command({"program_id": OBSERVATION, "registration_sha256": sha}))
    assert (result.status, result.reason) == ("applied", None)
    assert result.result is not None and result.result["sealed_by"] == OWNER
    sealed = json.loads(
        (tmp_path / "pilot" / "registrations" / f"{OBSERVATION}.sealed.json").read_text()
    )
    assert (sealed["status"], sealed["sealed_by"], sealed["sha256"]) == ("sealed", OWNER, sha)


@pytest.mark.parametrize(
    ("sub", "ingress"),
    [("loopback-operator", "loopback"), ("someone@example.com", "family")],
    ids=["loopback-label", "family-non-owner"],
)
def test_a_non_owner_actor_is_refused(desk: Path, tmp_path: Path, sub: str, ingress: str) -> None:
    sha = reviewed_sha(OBSERVATION)
    cmd = command({"program_id": OBSERVATION, "registration_sha256": sha}, sub=sub, ingress=ingress)
    result = apply(desk, cmd)
    assert (result.status, result.reason) == ("refused", "sealer_not_owner")
    assert not (tmp_path / "pilot" / "registrations" / f"{OBSERVATION}.sealed.json").exists()


def test_an_unreviewed_hash_is_refused(desk: Path, tmp_path: Path) -> None:
    result = apply(desk, command({"program_id": OBSERVATION, "registration_sha256": "0" * 64}))
    assert (result.status, result.reason) == ("refused", "registration_sha256_mismatch")
    assert not (tmp_path / "pilot" / "registrations" / f"{OBSERVATION}.sealed.json").exists()
