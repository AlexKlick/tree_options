"""Wave 3 lane B: the family command authority (C3.1 verifier, key, ledger, dispatch).

Oracles are independent of this lane's code: the golden fixture
``tests/contracts/fixtures/wave3/family_command_assertion.json`` (a TEST-ONLY
key, bytes 0..31), tokens re-signed with stdlib ``json``/``hmac``/``base64``
in the literal claim order, the frozen ``FamilyRefusal`` strings and the
C3.7 ledger path spelled out as literals, and the Wave 2
``LoopbackCommandAuthority`` itself for the unchanged loopback path.
"""

from __future__ import annotations

import ast
import base64
import hashlib
import hmac
import json
import logging
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.unit.trex_fakes import (
    FAKE_COMMAND_PATH,
    FakeCommandAuthority,
    FakeFamilyPortal,
    SessionClock,
)
from tree_options.trex.contracts import (
    Actor,
    CommandAuthorityRefused,
    RawCommandRequest,
    actor_binds_request,
)
from tree_options.trex.contracts.wave3 import AssertionSeen, FamilyRefusal
from tree_options.trex_web.app import create_app
from tree_options.trex_web.commands_view import LoopbackCommandAuthority
from tree_options.trex_web.family_authority import (
    DispatchingCommandAuthority,
    FamilyCommandAuthority,
    FamilyIngressDisabled,
    dispatching_authority,
    family_ingress_status,
    prune_assertions,
)

REPO = Path(__file__).resolve().parents[2]
GOLDEN = json.loads(
    (REPO / "tests/contracts/fixtures/wave3/family_command_assertion.json").read_text()
)
KEY = base64.b64decode(GOLDEN["key_b64"])
KEY_TEXT = GOLDEN["key_b64"]
BODY = GOLDEN["body"].encode("utf-8")
CLAIMS = GOLDEN["claims"]
TOKEN = GOLDEN["token"]
IAT = CLAIMS["iat"]
JTI = CLAIMS["jti"]
OWNER = "owner@example.com"
ORIGIN = GOLDEN["origin"]
HOST = GOLDEN["host"]
IDEMPOTENCY_KEY = json.loads(BODY)["idempotency_key"]
BODY_SHA = hashlib.sha256(BODY).hexdigest()
#: The claim order family-apps signs, literal (C3.1).
CLAIM_ORDER = [
    "v",
    "sub",
    "aud",
    "iat",
    "exp",
    "method",
    "path",
    "ingress",
    "origin",
    "request_id",
    "request_sha256",
    "jti",
]
LOOPBACK = "http://127.0.0.1:8090"
OTHER_JTI = "0123456789abcdef0123456789abcdef"


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def token(key: bytes = KEY, **changes: Any) -> str:
    """The golden claims with ``changes``, signed with stdlib only."""
    claims = {name: CLAIMS[name] for name in CLAIM_ORDER}
    claims.update(changes)
    payload = b64url(json.dumps(claims, separators=(",", ":"), ensure_ascii=False).encode())
    signature = b64url(hmac.new(key, payload.encode("ascii"), hashlib.sha256).digest())
    return f"{payload}.{signature}"


class Clock:
    """A fixed UTC clock in Unix seconds (moved by assigning ``at_s``)."""

    def __init__(self, at_s: int) -> None:
        self.at_s = at_s

    def __call__(self) -> datetime:
        return datetime.fromtimestamp(self.at_s, UTC)


def raw(
    tok: str | None = TOKEN,
    *,
    body: bytes = BODY,
    method: str = "POST",
    path: str = "/api/commands",
    peer: str = "127.0.0.1",
    headers: dict[str, str] | None = None,
    request_id: str = "web-00000000000000000000000000000001",
) -> RawCommandRequest:
    """A request as the portal forwards it: public Host and Origin, stripped path."""
    merged = {"host": HOST, "origin": ORIGIN, "content-type": "application/json"}
    if tok is not None:
        merged["x-family-actor"] = tok
    merged.update(headers or {})
    return RawCommandRequest(
        method=method, path=path, headers=merged, body=body, peer_host=peer, request_id=request_id
    )


def loopback_raw(
    headers: dict[str, str] | None = None, peer: str = "127.0.0.1"
) -> RawCommandRequest:
    merged = {"host": "127.0.0.1:8090", "origin": LOOPBACK, "content-type": "application/json"}
    merged.update(headers or {})
    return RawCommandRequest(
        method="POST",
        path="/api/commands",
        headers=merged,
        body=BODY,
        peer_host=peer,
        request_id="web-loopback-0001",
    )


def write_key(path: Path, text: str = KEY_TEXT + "\n", mode: int = 0o600) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(mode)
    return path


@dataclass
class Rig:
    root: Path
    clock: Clock = field(default_factory=lambda: Clock(IAT))

    def __post_init__(self) -> None:
        self.run_dir = self.root / "desk-run"
        self.key_file = write_key(self.root / "config" / "trex" / "family-command.key")
        self.env: dict[str, str] = {
            "TREX_OWNER_EMAILS": " Owner@Example.com ",
            "TREX_FAMILY_ORIGINS": ORIGIN,
            "TREX_FAMILY_COMMAND_KEY_FILE": str(self.key_file),
        }

    def environment(self, **changes: str | None) -> dict[str, str]:
        merged: dict[str, str | None] = {**self.env, **changes}
        return {name: value for name, value in merged.items() if value is not None}

    def authority(self, **env: str | None) -> FamilyCommandAuthority:
        return FamilyCommandAuthority(
            run_dir=lambda: self.run_dir, clock=self.clock, env=self.environment(**env)
        )

    def dispatching(self, **env: str | None) -> DispatchingCommandAuthority:
        return dispatching_authority(
            run_dir=lambda: self.run_dir,
            clock=self.clock,
            env=self.environment(**env),
            loopback=LoopbackCommandAuthority(enabled=lambda: True),
        )

    @property
    def ledger(self) -> Path:
        return self.run_dir / "authority" / "assertions"

    def entries(self) -> list[str]:
        return sorted(p.name for p in self.ledger.iterdir()) if self.ledger.is_dir() else []


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


def refusal(authority: Any, request: RawCommandRequest) -> str:
    with pytest.raises(CommandAuthorityRefused) as caught:
        authority.verify(request)
    assert caught.value.reason == "command_unauthorized"
    return caught.value.detail


# -- the golden assertion ---------------------------------------------------------------------


def test_golden_token_verifies_and_records_the_jti(rig: Rig) -> None:
    request = raw()
    actor = rig.authority().verify(request)
    assert actor == Actor(
        sub=OWNER,
        ingress="family",
        request_id="web-00000000000000000000000000000001",
        request_sha256=BODY_SHA,
    )
    assert actor_binds_request(actor, request)
    assert rig.entries() == [f"{JTI}.json"]
    entry = rig.ledger / f"{JTI}.json"
    assert entry.stat().st_mode & 0o777 == 0o600
    assert json.loads(entry.read_bytes()) == {
        "schema": "trex-family-assertion-seen/1",
        "jti": JTI,
        "sub": OWNER,
        "aud": "trex-command",
        "request_id": IDEMPOTENCY_KEY,
        "request_sha256": BODY_SHA,
        "exp": IAT + 60,
        "backend_request_id": "web-00000000000000000000000000000001",
        "seen_at": "2026-10-22T14:00:01Z",
    }
    assert AssertionSeen.model_validate_json(entry.read_bytes()).jti == JTI


def test_the_golden_token_is_the_stdlib_rebuild() -> None:
    assert token() == TOKEN
    assert KEY == bytes(range(32))


# -- lifetime ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("offset", "detail"),
    [
        (60, "assertion_expired"),
        (3600, "assertion_expired"),
        (-6, "assertion_not_yet_valid"),
        (-600, "assertion_not_yet_valid"),
    ],
)
def test_expired_and_future_assertions_refused(rig: Rig, offset: int, detail: str) -> None:
    rig.clock.at_s = IAT + offset
    assert refusal(rig.authority(), raw()) == detail
    assert rig.entries() == []


@pytest.mark.parametrize("offset", [-5, 0, 59])
def test_lifetime_edges_accepted(rig: Rig, offset: int) -> None:
    rig.clock.at_s = IAT + offset
    assert rig.authority().verify(raw()).ingress == "family"


def test_future_iat_beyond_five_seconds_in_the_token(rig: Rig) -> None:
    """A token minted 6 s ahead of TREX's clock is refused; 5 s ahead is not."""
    ahead6 = token(iat=IAT + 6, exp=IAT + 66, jti=OTHER_JTI)
    assert refusal(rig.authority(), raw(ahead6)) == "assertion_not_yet_valid"
    ahead5 = token(iat=IAT + 5, exp=IAT + 65)
    assert rig.authority().verify(raw(ahead5)).sub == OWNER


def test_lifetime_over_sixty_seconds_is_invalid(rig: Rig) -> None:
    assert refusal(rig.authority(), raw(token(exp=IAT + 61))) == "assertion_claims_invalid"


# -- audience (staging vs prod) ------------------------------------------------------------------


def test_staging_token_refused_by_prod(rig: Rig) -> None:
    staging = token(aud="trex-staging-command")
    assert refusal(rig.authority(), raw(staging)) == "assertion_wrong_audience"


def test_prod_token_refused_by_staging(rig: Rig) -> None:
    staging = rig.authority(TREX_FAMILY_AUDIENCE="trex-staging-command")
    assert refusal(staging, raw()) == "assertion_wrong_audience"
    assert staging.verify(raw(token(aud="trex-staging-command"))).ingress == "family"


# -- request binding ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("change", "detail"),
    [
        ({"method": "PUT"}, "assertion_method_mismatch"),
        ({"path": "/trex/api/commands"}, "assertion_path_mismatch"),
        ({"path": "/api/desk/supervised/halt"}, "assertion_path_mismatch"),
        ({"headers": {"origin": "https://other.example.net"}}, "assertion_origin_mismatch"),
        ({"headers": {"origin": LOOPBACK}}, "assertion_origin_mismatch"),
        ({"headers": {"host": "127.0.0.1:8090"}}, "assertion_host_mismatch"),
        ({"headers": {"host": "family.example.net:8443"}}, "assertion_host_mismatch"),
        ({"body": BODY + b" "}, "assertion_body_mismatch"),
        ({"body": BODY.replace(b"my phone", b"my watch")}, "assertion_body_mismatch"),
        ({"peer": "192.168.1.20"}, "wrong_peer"),
        ({"peer": "100.64.0.9"}, "wrong_peer"),
    ],
)
def test_request_mismatches_refused(rig: Rig, change: dict[str, Any], detail: str) -> None:
    assert refusal(rig.authority(), raw(**change)) == detail
    assert rig.entries() == []


def test_origin_must_be_configured(rig: Rig) -> None:
    other = rig.authority(TREX_FAMILY_ORIGINS="https://other.example.net")
    assert refusal(other, raw()) == "assertion_origin_not_allowed"


def test_request_id_is_the_body_idempotency_key(rig: Rig) -> None:
    other = BODY.replace(IDEMPOTENCY_KEY.encode(), b"11111111-2222-4333-8444-555555555555")
    tok = token(request_sha256=hashlib.sha256(other).hexdigest())
    assert refusal(rig.authority(), raw(tok, body=other)) == "assertion_request_id_mismatch"


def test_non_owner_refused(rig: Rig) -> None:
    assert refusal(rig.authority(TREX_OWNER_EMAILS="someone@example.com"), raw()) == (
        "assertion_sub_not_owner"
    )
    intruder = token(sub="intruder@example.com")
    assert refusal(rig.authority(), raw(intruder)) == "assertion_sub_not_owner"
    assert rig.entries() == []


def test_owner_list_accepts_any_listed_owner(rig: Rig) -> None:
    owners = "someone@example.com, OWNER@example.com"
    assert rig.authority(TREX_OWNER_EMAILS=owners).verify(raw()).sub == OWNER


@pytest.mark.parametrize(
    ("tok", "detail"),
    [
        ("", "assertion_malformed"),
        ("not-a-token", "assertion_malformed"),
        (TOKEN + ", " + TOKEN, "assertion_malformed"),
        (token(key=b"\x01" * 32), "assertion_bad_signature"),
        (token(v=1), "assertion_claims_invalid"),
    ],
)
def test_bad_tokens_refused(rig: Rig, tok: str, detail: str) -> None:
    assert refusal(rig.authority(), raw(tok)) == detail


# -- the replay ledger --------------------------------------------------------------------------


def test_replayed_jti_refused(rig: Rig) -> None:
    authority = rig.authority()
    authority.verify(raw())
    first = (rig.ledger / f"{JTI}.json").read_bytes()
    rig.clock.at_s = IAT + 30
    assert refusal(authority, raw(request_id="web-retry-0002")) == "assertion_replayed"
    assert rig.entries() == [f"{JTI}.json"]
    assert (rig.ledger / f"{JTI}.json").read_bytes() == first


def test_replay_ledger_survives_a_restart(rig: Rig) -> None:
    rig.authority().verify(raw())
    restarted = rig.authority()
    assert refusal(restarted, raw(request_id="web-after-restart")) == "assertion_replayed"


def test_browser_retry_is_a_new_assertion(rig: Rig) -> None:
    """Same body, new jti: the authority passes it (the store answers duplicate)."""
    authority = rig.authority()
    first = authority.verify(raw())
    second = authority.verify(raw(token(jti=OTHER_JTI), request_id="web-retry-0002"))
    assert (first.sub, second.sub) == (OWNER, OWNER)
    assert rig.entries() == sorted([f"{JTI}.json", f"{OTHER_JTI}.json"])
    other = json.loads((rig.ledger / f"{OTHER_JTI}.json").read_bytes())
    assert other["request_id"] == IDEMPOTENCY_KEY
    assert other["backend_request_id"] == "web-retry-0002"


def test_concurrent_uses_of_one_jti_admit_exactly_one(rig: Rig) -> None:
    authority = rig.authority()
    outcomes: list[str] = []
    lock = threading.Lock()
    start = threading.Barrier(8)

    def attempt(n: int) -> None:
        start.wait()
        try:
            authority.verify(raw(request_id=f"web-race-{n}"))
            outcome = "actor"
        except CommandAuthorityRefused as refused:
            outcome = refused.detail
        with lock:
            outcomes.append(outcome)

    threads = [threading.Thread(target=attempt, args=(n,)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(outcomes) == ["actor"] + ["assertion_replayed"] * 7
    assert rig.entries() == [f"{JTI}.json"]


def test_ledger_unwritable_fails_closed(rig: Rig) -> None:
    rig.run_dir.mkdir(parents=True)
    (rig.run_dir / "authority").write_text("not a directory")
    assert refusal(rig.authority(), raw()) == "assertion_ledger_unavailable"


def test_ledger_symlink_fails_closed(rig: Rig) -> None:
    elsewhere = rig.root / "elsewhere"
    elsewhere.mkdir()
    (rig.run_dir / "authority").mkdir(parents=True)
    (rig.run_dir / "authority" / "assertions").symlink_to(elsewhere)
    assert refusal(rig.authority(), raw()) == "assertion_ledger_unavailable"
    assert list(elsewhere.iterdir()) == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_ledger_read_only_fails_closed(rig: Rig) -> None:
    rig.ledger.mkdir(parents=True)
    rig.ledger.chmod(0o500)
    try:
        assert refusal(rig.authority(), raw()) == "assertion_ledger_unavailable"
    finally:
        rig.ledger.chmod(0o700)
    assert rig.entries() == []


def test_prune_removes_only_entries_past_exp_plus_skew(rig: Rig) -> None:
    authority = rig.authority()
    authority.verify(raw())
    later = token(iat=IAT + 100, exp=IAT + 160, jti=OTHER_JTI)
    rig.clock.at_s = IAT + 100
    authority.verify(raw(later))
    (rig.ledger / "ffffffffffffffffffffffffffffffff.json").write_text("{not json")
    # exp + 5 s has not passed for the first entry yet
    assert prune_assertions(rig.run_dir, datetime.fromtimestamp(IAT + 65, UTC)) == 0
    assert prune_assertions(rig.run_dir, datetime.fromtimestamp(IAT + 66, UTC)) == 1
    assert rig.entries() == sorted([f"{OTHER_JTI}.json", "ffffffffffffffffffffffffffffffff.json"])
    assert prune_assertions(rig.root / "no-such-run-dir", datetime.fromtimestamp(IAT, UTC)) == 0


# -- key loading (fail closed with a clear reason) ----------------------------------------------


def disabled(
    authority: FamilyCommandAuthority, request: RawCommandRequest
) -> FamilyIngressDisabled:
    with pytest.raises(FamilyIngressDisabled) as caught:
        authority.verify(request)
    assert caught.value.reason == "command_unauthorized"
    assert caught.value.detail == "family_ingress_disabled"
    assert isinstance(caught.value, CommandAuthorityRefused)
    return caught.value


def test_missing_key_file_fails_closed_with_a_clear_reason(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    rig.key_file.unlink()
    authority = rig.authority()
    with caplog.at_level(logging.WARNING):
        error = disabled(authority, raw())
    assert error.problems == ("key_file_missing",)
    assert "key_file_missing" in str(error)
    status = authority.status()
    assert (status.enabled, status.problems) == (False, ("key_file_missing",))
    assert status.key_file == str(rig.key_file)
    assert "key_file_missing" in caplog.text and str(rig.key_file) in caplog.text
    assert rig.entries() == []


def test_disabled_is_the_first_check(rig: Rig) -> None:
    rig.key_file.unlink()
    assert refusal(rig.authority(), raw("garbage", peer="192.168.1.20")) == (
        "family_ingress_disabled"
    )


def test_key_installed_later_enables_without_a_restart(rig: Rig) -> None:
    rig.key_file.unlink()
    authority = rig.authority()
    disabled(authority, raw())
    write_key(rig.key_file)
    assert authority.verify(raw()).ingress == "family"
    assert authority.status().enabled


def make_symlink(rig: Rig) -> None:
    real = write_key(rig.root / "real.key")
    rig.key_file.unlink()
    rig.key_file.symlink_to(real)


def make_directory(rig: Rig) -> None:
    rig.key_file.unlink()
    rig.key_file.mkdir()


def make_fifo(rig: Rig) -> None:
    rig.key_file.unlink()
    os.mkfifo(rig.key_file, 0o600)


@pytest.mark.parametrize(
    ("damage", "problem"),
    [
        (make_symlink, "key_file_symlink"),
        (make_directory, "key_file_not_regular"),
        (make_fifo, "key_file_not_regular"),
        (lambda rig: rig.key_file.chmod(0o644), "key_file_permissions"),
        (lambda rig: rig.key_file.chmod(0o640), "key_file_permissions"),
        (lambda rig: write_key(rig.key_file, KEY_TEXT + " " * 1100), "key_file_too_large"),
        (lambda rig: write_key(rig.key_file, KEY_TEXT.rstrip("=")), "key_invalid"),
        (lambda rig: write_key(rig.key_file, b64url(KEY)), "key_invalid"),
        (
            lambda rig: write_key(rig.key_file, base64.b64encode(KEY[:31]).decode()),
            "key_invalid",
        ),
        (lambda rig: write_key(rig.key_file, ""), "key_invalid"),
        (lambda rig: rig.key_file.write_bytes(b"\xff" * 44), "key_invalid"),
    ],
    ids=[
        "symlink",
        "directory",
        "fifo",
        "0644",
        "0640",
        "too_large",
        "unpadded",
        "base64url",
        "31_bytes",
        "empty",
        "not_ascii",
    ],
)
def test_key_file_must_be_a_small_private_regular_file(
    rig: Rig,
    damage: Callable[[Rig], object],
    problem: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    damage(rig)
    authority = rig.authority()
    with caplog.at_level(logging.DEBUG):
        error = disabled(authority, raw())
    assert error.problems == (problem,)
    for text in (str(error), repr(error), caplog.text, repr(authority.status())):
        assert KEY_TEXT not in text and KEY_TEXT.rstrip("=") not in text
        assert KEY.hex() not in text


def test_owner_only_mode_0400_is_accepted(rig: Rig) -> None:
    rig.key_file.chmod(0o400)
    assert rig.authority().verify(raw()).ingress == "family"


@pytest.mark.parametrize(
    ("env", "problems"),
    [
        ({"TREX_OWNER_EMAILS": None}, ("owners_missing",)),
        ({"TREX_OWNER_EMAILS": " , "}, ("owners_missing",)),
        ({"TREX_FAMILY_ORIGINS": None}, ("origins_missing",)),
        (
            {"TREX_OWNER_EMAILS": None, "TREX_FAMILY_ORIGINS": None},
            ("owners_missing", "origins_missing"),
        ),
        ({"TREX_OWNER_EMAILS": "not-an-email"}, ("owners_invalid",)),
        ({"TREX_FAMILY_ORIGINS": "http://family.example.net"}, ("origins_invalid",)),
        ({"TREX_FAMILY_AUDIENCE": "speech-lab"}, ("audience_invalid",)),
        (
            {"TREX_FAMILY_COMMAND_KEY_FILE": "relative/family-command.key"},
            ("key_file_not_absolute",),
        ),
    ],
)
def test_configuration_problems_fail_closed(
    rig: Rig, env: dict[str, str | None], problems: tuple[str, ...]
) -> None:
    error = disabled(rig.authority(**env), raw())
    assert error.problems == problems


def test_default_key_file_is_under_home(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(rig.root / "home"))
    authority = rig.authority(TREX_FAMILY_COMMAND_KEY_FILE=None)
    expected = rig.root / "home" / ".config" / "trex" / "family-command.key"
    assert authority.status().key_file == str(expected)
    assert authority.status().problems == ("key_file_missing",)
    write_key(expected)
    assert authority.verify(raw()).sub == OWNER


def test_family_ingress_status_reads_the_process_environment(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name, value in rig.env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("TREX_FAMILY_AUDIENCE", raising=False)
    status = family_ingress_status()
    assert (status.enabled, status.problems, status.audience) == (True, (), "trex-command")
    monkeypatch.setenv("TREX_FAMILY_AUDIENCE", "trex-staging-command")
    assert family_ingress_status().audience == "trex-staging-command"


# -- dispatch -----------------------------------------------------------------------------------


def test_loopback_path_unchanged(rig: Rig) -> None:
    """No x-family-actor: exactly the Wave 2 loopback authority's answer."""
    alone = LoopbackCommandAuthority(enabled=lambda: True)
    dispatch = rig.dispatching()
    assert dispatch.verify(loopback_raw()) == alone.verify(loopback_raw())
    assert dispatch.verify(loopback_raw()).ingress == "loopback"
    for request in (
        loopback_raw({"x-forwarded-for": "100.64.0.9"}),
        loopback_raw({"host": HOST, "origin": ORIGIN}),
        loopback_raw({"origin": ORIGIN}),
        loopback_raw(peer="192.168.1.20"),
        raw(None),
    ):
        assert refusal(dispatch, request) == refusal(alone, request)
    assert rig.entries() == []


def test_loopback_path_unaffected_by_a_missing_key(rig: Rig) -> None:
    rig.key_file.unlink()
    assert rig.dispatching().verify(loopback_raw()).sub == "loopback-operator"


def test_family_header_goes_only_to_the_family_verifier(rig: Rig) -> None:
    dispatch = rig.dispatching()
    assert dispatch.verify(raw()).ingress == "family"
    # a loopback-looking request with a bad assertion never falls back
    for bad in ("", "garbage", token(key=b"\x02" * 32)):
        detail = refusal(dispatch, loopback_raw({"x-family-actor": bad}))
        assert detail in {m.value for m in FamilyRefusal}
    rig.key_file.unlink()
    assert refusal(dispatch, loopback_raw({"x-family-actor": "garbage"})) == (
        "family_ingress_disabled"
    )


def test_forwarded_identity_without_an_assertion_refused(rig: Rig) -> None:
    dispatch = rig.dispatching()
    assert refusal(dispatch, raw(None)) == "host_not_loopback"
    assert refusal(dispatch, raw(None, headers={"x-forwarded-for": "100.64.0.9"})) == ("forwarded")


def test_default_loopback_is_the_wave2_authority(rig: Rig) -> None:
    dispatch = dispatching_authority(run_dir=lambda: rig.run_dir, env=rig.env)
    assert isinstance(dispatch.loopback, LoopbackCommandAuthority)
    assert isinstance(dispatch.family, FamilyCommandAuthority)


# -- through the web command route --------------------------------------------------------------


def make_app(rig: Rig) -> Any:
    return create_app(
        state_dir=str(rig.root / "legacy"),
        plans_dir=str(rig.root / "plans"),
        discovery_dir=str(rig.root / "discovery"),
        desk_state_dir=str(rig.root / "desk-state"),
        desk_store_dir=str(rig.root / "desk-store"),
        desk_run_dir=str(rig.run_dir),
        supervised_dir=str(rig.root / "supervised"),
        command_authority=rig.dispatching(),
        command_clock=rig.clock,
    )


def test_commands_route_queues_a_family_command_once(rig: Rig) -> None:
    app = make_app(rig)
    family = TestClient(app, base_url=f"https://{HOST}", client=("127.0.0.1", 50000))

    def post(tok: str) -> Any:
        return family.post(
            "/api/commands",
            content=BODY,
            headers={"origin": ORIGIN, "content-type": "application/json", "x-family-actor": tok},
        )

    first = post(TOKEN)
    assert first.status_code == 202, first.text
    assert first.json()["status"] == "accepted"
    stored = json.loads((rig.run_dir / "commands" / f"{IDEMPOTENCY_KEY}.json").read_bytes())
    assert (stored["issued_by"]["sub"], stored["issued_by"]["ingress"]) == (OWNER, "family")
    replay = post(TOKEN)
    assert replay.status_code == 403
    assert replay.json() == {
        "error": "command_unauthorized",
        "reason": "command_unauthorized",
        "detail": "assertion_replayed",
    }
    retry = post(token(jti=OTHER_JTI))
    assert retry.status_code == 200, retry.text
    assert retry.json()["status"] == "duplicate"


def test_commands_route_keeps_the_loopback_operator(rig: Rig) -> None:
    app = make_app(rig)
    local = TestClient(app, base_url=LOOPBACK, client=("127.0.0.1", 50000))
    response = local.post(
        "/api/commands",
        content=BODY,
        headers={"origin": LOOPBACK, "content-type": "application/json"},
    )
    assert response.status_code == 202, response.text
    stored = json.loads((rig.run_dir / "commands" / f"{IDEMPOTENCY_KEY}.json").read_bytes())
    assert stored["issued_by"]["ingress"] == "loopback"
    assert rig.entries() == []


def test_commands_route_refuses_a_bad_family_assertion(rig: Rig) -> None:
    app = make_app(rig)
    family = TestClient(app, base_url=f"https://{HOST}", client=("127.0.0.1", 50000))
    response = family.post(
        "/api/commands",
        content=BODY,
        headers={
            "origin": ORIGIN,
            "content-type": "application/json",
            "x-family-actor": token(sub="intruder@example.com"),
        },
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "assertion_sub_not_owner"
    assert not (rig.run_dir / "commands").exists() or not any(
        (rig.run_dir / "commands").glob("*.json")
    )


# -- the v2 family mode of the shared fakes ----------------------------------------------------


def test_fake_portal_mints_the_golden_assertion() -> None:
    portal = FakeFamilyPortal()
    assert portal.sign(BODY, iat=IAT, jti=JTI) == TOKEN
    assert portal.sign(BODY, sub=" OWNER@example.com", iat=IAT, jti=JTI) == TOKEN
    forwarded = portal.forward(BODY, iat=IAT, jti=JTI, request_id="web-x")
    assert forwarded.path == "/api/commands"
    assert forwarded.method == "POST"
    assert forwarded.peer_host == "127.0.0.1"
    assert forwarded.headers["host"] == HOST
    assert forwarded.headers["origin"] == ORIGIN
    assert forwarded.headers["x-family-actor"] == TOKEN
    assert portal.env() == {
        "TREX_OWNER_EMAILS": OWNER,
        "TREX_FAMILY_ORIGINS": ORIGIN,
        "TREX_FAMILY_AUDIENCE": "trex-command",
    }


def test_fake_staging_portal_uses_the_staging_audience() -> None:
    staging = FakeFamilyPortal(prefix="/trex-staging")
    tok = staging.sign(BODY, iat=IAT, jti=JTI)
    assert tok == token(aud="trex-staging-command")
    assert staging.env()["TREX_FAMILY_AUDIENCE"] == "trex-staging-command"
    assert staging.forward(BODY, portal_path="/trex-staging/api/commands").path == "/api/commands"


def test_fake_portal_jtis_are_fresh_per_assertion() -> None:
    portal = FakeFamilyPortal(clock=Clock(IAT))
    first = portal.forward(BODY).headers["x-family-actor"]
    second = portal.forward(BODY).headers["x-family-actor"]
    assert first != second


def test_fake_portal_feeds_the_real_authority(tmp_path: Path) -> None:
    clock = Clock(IAT)
    portal = FakeFamilyPortal(clock=clock)
    key_file = portal.write_key_file(tmp_path / "trex" / "family-command.key")
    assert key_file.stat().st_mode & 0o777 == 0o600
    env = {**portal.env(), "TREX_FAMILY_COMMAND_KEY_FILE": str(key_file)}
    authority = FamilyCommandAuthority(run_dir=lambda: tmp_path / "run", clock=clock, env=env)
    request = portal.forward(BODY)
    assert authority.verify(request).sub == OWNER
    assert authority.verify(portal.forward(BODY)).sub == OWNER  # a retry: new jti
    assert refusal(authority, request) == "assertion_replayed"


def test_fake_authority_v2_mode() -> None:
    clock = Clock(IAT)
    fake = FakeCommandAuthority(clock=clock, family_mode="v2", portal=FakeFamilyPortal(clock=clock))
    request = fake.make_request(BODY, family_sub=OWNER)
    assert request.path == "/api/commands"
    assert request.headers["host"] == HOST
    actor = fake.verify(request)
    assert (actor.sub, actor.ingress) == (OWNER, "family")
    assert actor_binds_request(actor, request)
    assert refusal(fake, request) == "assertion_replayed"
    assert refusal(fake, fake.make_request(BODY, family_sub="intruder@example.com")) == (
        "assertion_sub_not_owner"
    )
    clock.at_s = IAT + 120
    assert refusal(fake, raw()) == "assertion_expired"
    assert fake.verify(fake.make_request(BODY)).ingress == "loopback"
    fake.allow_family = False
    assert refusal(fake, fake.make_request(BODY, family_sub=OWNER)) == "family_ingress_disabled"


def test_fake_authority_v1_mode_is_unchanged() -> None:
    fake = FakeCommandAuthority(clock=SessionClock())
    request = fake.make_request(b"{}", family_sub="owner@example.com")
    assert request.path == FAKE_COMMAND_PATH
    assert fake.verify(request).ingress == "family"
    v2 = FakeFamilyPortal(clock=Clock(IAT)).forward(BODY)
    assert refusal(fake, v2) == "bad_signature"
    with pytest.raises(ValueError):
        FakeCommandAuthority(family_mode="v3")


def test_fake_authority_v2_default_portal_shares_its_clock() -> None:
    clock = Clock(IAT)
    fake = FakeCommandAuthority(clock=clock, family_mode="v2")
    assert fake.portal is not None and fake.portal.clock is clock
    assert fake.verify(fake.make_request(BODY, family_sub=OWNER)).sub == OWNER
    assert fake.verify(raw()).sub == OWNER  # the golden token, signed with the same test key


# -- guards -------------------------------------------------------------------------------------

NEW_PYTHON = [REPO / "src/tree_options/trex_web/family_authority.py", Path(__file__)]


@pytest.mark.parametrize("path", NEW_PYTHON, ids=lambda p: p.name)
def test_no_timedelta_weekday_or_toordinal(path: Path) -> None:
    banned = {"timedelta", "weekday", "toordinal"}
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Name):
            assert node.id not in banned, f"{path.name}:{node.lineno}"
        if isinstance(node, ast.Attribute):
            assert node.attr not in banned, f"{path.name}:{node.lineno}"
        if isinstance(node, ast.ImportFrom):
            assert all(a.name not in banned for a in node.names), f"{path.name}:{node.lineno}"


@pytest.mark.parametrize(
    "path", [*NEW_PYTHON, REPO / "tests/unit/trex_fakes.py"], ids=lambda p: p.name
)
def test_no_en_or_em_dashes(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    for dash in (chr(0x2013), chr(0x2014)):
        assert dash not in text, f"{path.name} contains U+{ord(dash):04X}"


def test_module_imports_no_broker_library() -> None:
    tree = ast.parse(NEW_PYTHON[0].read_text())
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        if isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    assert not any(name.startswith(("ib_async", "ib_insync", "ibapi")) for name in names)
