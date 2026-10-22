"""Wave 3/4 frozen contracts: fixtures, the family assertion, views, acceptance, pilot.

Oracles are independent of the implementation where it matters: the
assertion bytes are rebuilt with stdlib ``json``/``hmac``/``base64`` from a
literal claim order; acceptance verdicts are hand-counted against the real
NYSE calendar (November 2026: Thanksgiving closed, 2026-11-27 closes at
13:00 ET, so 210 regular minutes); registration hashes use stdlib
``json.dumps``; label and vocabulary strings are literals.
"""

from __future__ import annotations

import ast
import base64
import copy
import hashlib
import hmac
import json
import os
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

import tree_options.trex.contracts as c
from tree_options.trex.clock import session_calendar
from tree_options.trex.contracts import acceptance as acc
from tree_options.trex.contracts import wave3 as w

FIXTURES = Path(__file__).parent / "fixtures"
WAVE3 = FIXTURES / "wave3"
REPO = Path(__file__).resolve().parents[2]

OWNER = "owner@example.com"
ORIGIN = "https://family.example.net"
HOST = "family.example.net"
#: The claim order family-apps signs (signSpeechControl's, plus jti), literal.
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


def load(name: str) -> dict[str, Any]:
    return json.loads((WAVE3 / name).read_text())


def put(document: dict[str, Any], path: str, value: Any) -> dict[str, Any]:
    out = copy.deepcopy(document)
    *parents, leaf = path.split(".")
    node: Any = out
    for key in parents:
        node = node[int(key)] if isinstance(node, list) else node[key]
    if isinstance(node, list):
        node[int(leaf)] = value
    else:
        node[leaf] = value
    return out


def drop(document: dict[str, Any], path: str) -> dict[str, Any]:
    out = copy.deepcopy(document)
    *parents, leaf = path.split(".")
    node: Any = out
    for key in parents:
        node = node[int(key)] if isinstance(node, list) else node[key]
    del node[leaf]
    return out


def Z(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


# =============================================================================
# fixtures
# =============================================================================

ROUND_TRIPS: list[tuple[str, Callable[[Any], Any], str | None]] = [
    ("today.json", w.TodayView.model_validate, "trex-today/1"),
    ("today_unavailable.json", w.TodayView.model_validate, "trex-today/1"),
    ("activity.json", w.ActivityPage.model_validate, "trex-activity/1"),
    ("acceptance_view.json", acc.AcceptanceView.model_validate, "trex-acceptance-view/1"),
    (
        "pilot_registration_observation.json",
        w.PilotRegistration.model_validate,
        "trex-pilot-registration/1",
    ),
    (
        "pilot_registration_baseline_draft.json",
        w.PilotRegistration.model_validate,
        "trex-pilot-registration/1",
    ),
    (
        "pilot_registration_challenger_sealed.json",
        w.PilotRegistration.model_validate,
        "trex-pilot-registration/1",
    ),
    ("pilot_usage.json", w.BudgetUsage.model_validate, "trex-pilot-usage/1"),
]
#: Fixtures that are not one model: golden bytes, labels, Wave 2 mirrors.
OTHER_FIXTURES = {
    "family_command_assertion.json",
    "control_labels.json",
    "vocabulary.json",
    "wave2_proposal_list.json",
    "wave2_portfolio_unavailable.json",
}


@pytest.mark.parametrize(("name", "parse", "schema"), ROUND_TRIPS, ids=[r[0] for r in ROUND_TRIPS])
def test_fixture_round_trips(name: str, parse: Callable[[Any], Any], schema: str | None) -> None:
    document = load(name)
    model = parse(document)
    assert c.model_document(model) == document
    assert parse(c.model_document(model)) == model
    assert document.get("schema") == schema


def test_every_wave3_fixture_is_covered() -> None:
    names = {p.name for p in WAVE3.glob("*.json")}
    assert names == {r[0] for r in ROUND_TRIPS} | OTHER_FIXTURES


EXTRA_FIELD_CASES = [
    ("today.json", w.TodayView.model_validate, ""),
    ("today.json", w.TodayView.model_validate, "session"),
    ("today.json", w.TodayView.model_validate, "controls.halt"),
    ("today.json", w.TodayView.model_validate, "approvals.items.0"),
    ("today.json", w.TodayView.model_validate, "approvals.items.0.approve_args"),
    ("today.json", w.TodayView.model_validate, "positions.risk"),
    ("today.json", w.TodayView.model_validate, "acceptance"),
    ("today.json", w.TodayView.model_validate, "blockers.0"),
    ("activity.json", w.ActivityPage.model_validate, "items.0"),
    ("activity.json", w.ActivityPage.model_validate, "items.0.actor"),
    ("acceptance_view.json", acc.AcceptanceView.model_validate, "document"),
    ("acceptance_view.json", acc.AcceptanceView.model_validate, "document.sessions.0"),
    ("pilot_registration_baseline_draft.json", w.PilotRegistration.model_validate, "budgets"),
]


@pytest.mark.parametrize(("name", "parse", "where"), EXTRA_FIELD_CASES)
def test_extra_fields_refused(name: str, parse: Callable[[Any], Any], where: str) -> None:
    target = put(load(name), f"{where}.unexpected" if where else "unexpected", 1)
    with pytest.raises(ValidationError):
        parse(target)


@pytest.mark.parametrize(
    "path",
    ["session.calendar_sessions_left", "approvals.problem_count", "gateway.detail", "blockers"],
)
def test_today_keys_are_required(path: str) -> None:
    """Every key is on the wire, null or not: a reader never guesses a default."""
    with pytest.raises(ValidationError):
        w.TodayView.model_validate(drop(load("today.json"), path))


# =============================================================================
# C3.1 trex-family-command/1
# =============================================================================

GOLDEN = load("family_command_assertion.json")
KEY = base64.b64decode(GOLDEN["key_b64"])
BODY = GOLDEN["body"].encode("utf-8")
IAT = GOLDEN["claims"]["iat"]
JTI = GOLDEN["claims"]["jti"]
AT_IAT = datetime.fromtimestamp(IAT, UTC)


def oracle_token(claims: dict[str, Any], key: bytes = KEY) -> str:
    """Independent of the implementation: stdlib JSON in the literal order."""
    payload = b64url(
        json.dumps(
            {name: claims[name] for name in claims}, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
    )
    signature = b64url(hmac.new(key, payload.encode("ascii"), hashlib.sha256).digest())
    return f"{payload}.{signature}"


def ordered(claims: dict[str, Any]) -> dict[str, Any]:
    return {name: claims[name] for name in CLAIM_ORDER}


def config(**overrides: Any) -> w.FamilyAuthorityConfig:
    values: dict[str, Any] = {
        "key": KEY,
        "owner_emails": frozenset({OWNER}),
        "origins": frozenset({ORIGIN}),
        "audience": "trex-command",
    }
    values.update(overrides)
    return w.FamilyAuthorityConfig(**values)


def raw(
    token: str | None = GOLDEN["token"],
    *,
    body: bytes = BODY,
    method: str = "POST",
    path: str = "/api/commands",
    peer: str = "127.0.0.1",
    headers: dict[str, str] | None = None,
) -> c.RawCommandRequest:
    merged = {"host": HOST, "origin": ORIGIN, "content-type": "application/json"}
    if token is not None:
        merged[w.FAMILY_ACTOR_HEADER] = token
    merged.update(headers or {})
    return c.RawCommandRequest(
        method=method,
        path=path,
        headers=merged,
        body=body,
        peer_host=peer,
        request_id="web-0123456789abcdef",
    )


def refusal(request: c.RawCommandRequest, now: datetime = AT_IAT, **cfg: Any) -> str:
    with pytest.raises(c.CommandAuthorityRefused) as caught:
        w.verify_family_command(request, config=config(**cfg), now=now)
    assert caught.value.reason == "command_unauthorized"
    return caught.value.detail


def test_golden_token_matches_a_stdlib_rebuild() -> None:
    claims = ordered(GOLDEN["claims"])
    assert GOLDEN["token"] == oracle_token(claims)
    payload = GOLDEN["token"].split(".")[0]
    decoded = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
    assert list(json.loads(decoded)) == CLAIM_ORDER
    assert claims["request_sha256"] == hashlib.sha256(BODY).hexdigest()
    assert claims["request_id"] == json.loads(BODY)["idempotency_key"]
    assert claims["exp"] == claims["iat"] + 60
    assert (claims["v"], claims["aud"], claims["ingress"]) == (2, "trex-command", "family")
    assert (claims["method"], claims["path"]) == ("POST", "/api/commands")


def test_mint_reproduces_the_golden_token_and_lowercases_sub() -> None:
    token = w.mint_family_command(
        KEY,
        sub=" Owner@Example.com ",
        aud="trex-command",
        origin=ORIGIN,
        body=BODY,
        iat=IAT,
        jti=JTI,
    )
    assert token == GOLDEN["token"]
    assert w.CLAIM_ORDER == tuple(CLAIM_ORDER)


def test_golden_fixture_names_paths_and_test_key() -> None:
    assert GOLDEN["portal_path"] == "/trex" + GOLDEN["path"]
    assert GOLDEN["path"] == w.FAMILY_COMMAND_PATH
    assert KEY == bytes(range(32))


def test_verify_accepts_the_golden_request() -> None:
    request = raw()
    actor, claims = w.verify_family_command(request, config=config(), now=AT_IAT)
    assert actor == c.Actor(
        sub=OWNER,
        ingress="family",
        request_id="web-0123456789abcdef",
        request_sha256=hashlib.sha256(BODY).hexdigest(),
    )
    assert c.actor_binds_request(actor, request)
    assert claims.jti == JTI
    seen = w.assertion_seen_record(claims, backend_request_id=request.request_id, seen_at=AT_IAT)
    assert w.AssertionSeen.model_validate(c.model_document(seen)) == seen
    assert c.model_document(seen)["schema"] == "trex-family-assertion-seen/1"


def resigned(**changes: Any) -> str:
    claims = ordered(GOLDEN["claims"])
    claims.update(changes)
    return oracle_token(claims)


@pytest.mark.parametrize(
    ("change", "why"),
    [
        ({"exp": IAT + 61}, "exp > iat + 60"),
        ({"exp": IAT}, "exp == iat"),
        ({"exp": IAT - 1}, "exp < iat"),
        ({"v": 1}, "route-bound v1 actor"),
        ({"v": "2"}, "string version"),
        ({"v": 2.0}, "float version"),
        ({"iat": float(IAT)}, "float iat"),
        ({"method": "GET"}, "claims a GET"),
        ({"path": "/api/desk/supervised/halt"}, "claims another path"),
        ({"ingress": "tailnet"}, "speech-control ingress"),
        ({"aud": "speech-lab-control"}, "another app's audience"),
        ({"sub": "Owner@Example.com"}, "sub not lower-cased"),
        ({"origin": "https://family.example.net/"}, "origin with a path"),
        ({"origin": "http://family.example.net"}, "plain http origin"),
        ({"origin": "https://family.example.net:443"}, "explicit default port"),
        ({"request_id": "not-a-uuid"}, "request id not a uuid"),
        ({"jti": "short"}, "jti not 32 hex"),
        ({"extra": 1}, "extra claim"),
    ],
)
def test_tampered_claims_are_refused(change: dict[str, Any], why: str) -> None:
    assert refusal(raw(resigned(**change))) == "assertion_claims_invalid", why


def test_missing_claim_and_duplicate_key_refused() -> None:
    claims = ordered(GOLDEN["claims"])
    del claims["jti"]
    assert refusal(raw(oracle_token(claims))) == "assertion_claims_invalid"
    payload_text = json.dumps(ordered(GOLDEN["claims"]), separators=(",", ":"))
    duplicated = payload_text[:-1] + ',"v":2}'
    payload = b64url(duplicated.encode())
    token = f"{payload}.{b64url(hmac.new(KEY, payload.encode(), hashlib.sha256).digest())}"
    assert refusal(raw(token)) == "assertion_claims_invalid"


def test_claims_model_lifetime_rule() -> None:
    claims = dict(GOLDEN["claims"])
    assert w.FamilyCommandClaims.model_validate(claims).exp == IAT + 60
    with pytest.raises(ValidationError):
        w.FamilyCommandClaims.model_validate({**claims, "exp": IAT + 61})
    assert w.FamilyCommandClaims.model_validate({**claims, "exp": IAT + 30}).exp == IAT + 30


@pytest.mark.parametrize(
    "token",
    [
        "no-dot-at-all",
        "a.b.c",
        GOLDEN["token"] + "=",
        GOLDEN["token"] + ", " + GOLDEN["token"],
        "." + GOLDEN["token"].split(".")[1],
        "",
    ],
)
def test_malformed_tokens(token: str) -> None:
    assert refusal(raw(token)) == "assertion_malformed"


def test_bad_signatures() -> None:
    payload, signature = GOLDEN["token"].split(".")
    flipped = signature[:-1] + ("A" if signature[-1] != "A" else "B")
    assert refusal(raw(f"{payload}.{flipped}")) == "assertion_bad_signature"
    assert refusal(raw(oracle_token(ordered(GOLDEN["claims"]), key=b"\x01" * 32))) == (
        "assertion_bad_signature"
    )


def test_disabled_without_key_owners_or_origins() -> None:
    assert refusal(raw(), key=b"") == "family_ingress_disabled"
    assert refusal(raw(), key=KEY[:31]) == "family_ingress_disabled"
    assert refusal(raw(), owner_emails=frozenset()) == "family_ingress_disabled"
    assert refusal(raw(), origins=frozenset()) == "family_ingress_disabled"


def test_transport_and_header_refusals() -> None:
    assert refusal(raw(peer="192.168.1.20")) == "wrong_peer"
    assert refusal(raw(token=None)) == "assertion_missing"


def test_audience_is_per_instance() -> None:
    assert refusal(raw(), audience="trex-staging-command") == "assertion_wrong_audience"
    staging = resigned(aud="trex-staging-command")
    actor, _ = w.verify_family_command(
        raw(staging), config=config(audience="trex-staging-command"), now=AT_IAT
    )
    assert actor.ingress == "family"


@pytest.mark.parametrize(
    ("offset", "detail"),
    [(-6, "assertion_not_yet_valid"), (-5, None), (59, None), (60, "assertion_expired")],
)
def test_time_window(offset: int, detail: str | None) -> None:
    now = datetime.fromtimestamp(IAT + offset, UTC)
    if detail is None:
        w.verify_family_command(raw(), config=config(), now=now)
    else:
        assert refusal(raw(), now=now) == detail


def test_method_path_origin_host_body_mismatches() -> None:
    assert refusal(raw(method="PUT")) == "assertion_method_mismatch"
    assert refusal(raw(path="/api/desk/supervised/halt")) == "assertion_path_mismatch"
    # the portal strips /trex: an unstripped path never verifies
    assert refusal(raw(path="/trex/api/commands")) == "assertion_path_mismatch"
    assert refusal(raw(), origins=frozenset({"https://other.example.net"})) == (
        "assertion_origin_not_allowed"
    )
    assert refusal(raw(headers={"origin": "https://other.example.net"})) == (
        "assertion_origin_mismatch"
    )
    assert refusal(raw(headers={"host": "127.0.0.1:8090"})) == "assertion_host_mismatch"
    assert refusal(raw(body=BODY + b" ")) == "assertion_body_mismatch"


def test_request_id_must_be_the_body_idempotency_key() -> None:
    other = json.dumps(
        {**json.loads(BODY), "idempotency_key": "11111111-2222-4333-8444-555555555555"},
        separators=(",", ":"),
    ).encode()
    token = resigned(request_sha256=hashlib.sha256(other).hexdigest())
    assert refusal(raw(token, body=other)) == "assertion_request_id_mismatch"
    not_json = b"[1,2]"
    token = resigned(request_sha256=hashlib.sha256(not_json).hexdigest())
    assert refusal(raw(token, body=not_json)) == "assertion_request_id_mismatch"


def test_sub_must_be_an_owner() -> None:
    assert refusal(raw(), owner_emails=frozenset({"someone@example.com"})) == (
        "assertion_sub_not_owner"
    )


def test_first_failing_check_is_reported() -> None:
    late = datetime.fromtimestamp(IAT + 120, UTC)
    assert refusal(raw(path="/elsewhere"), now=late) == "assertion_expired"


def test_family_refusal_vocabulary_is_frozen() -> None:
    assert [m.value for m in w.FamilyRefusal] == [
        "family_ingress_disabled",
        "wrong_peer",
        "assertion_missing",
        "assertion_malformed",
        "assertion_bad_signature",
        "assertion_claims_invalid",
        "assertion_wrong_audience",
        "assertion_not_yet_valid",
        "assertion_expired",
        "assertion_method_mismatch",
        "assertion_path_mismatch",
        "assertion_origin_not_allowed",
        "assertion_origin_mismatch",
        "assertion_host_mismatch",
        "assertion_body_mismatch",
        "assertion_request_id_mismatch",
        "assertion_sub_not_owner",
        "assertion_replayed",
        "assertion_ledger_unavailable",
    ]


def test_dispatch_and_the_loopback_authority_refuse_forwarded_identity() -> None:
    from tree_options.trex_web.commands_view import LoopbackCommandAuthority

    assert w.authority_for(raw()) == "family"
    assert w.authority_for(raw(token=None)) == "loopback"
    loopback = LoopbackCommandAuthority(enabled=lambda: True)
    forwarded = raw(token=None, headers={"x-forwarded-for": "100.64.0.9"})
    with pytest.raises(c.CommandAuthorityRefused):
        loopback.verify(forwarded)
    with pytest.raises(c.CommandAuthorityRefused) as caught:
        loopback.verify(raw())
    assert caught.value.detail == "family_ingress_unavailable"


def test_wave2_web_constants_agree() -> None:
    from tree_options.trex_web import commands_view

    assert w.FAMILY_ACTOR_HEADER == commands_view.FAMILY_ACTOR_HEADER == "x-family-actor"
    assert w.PORTAL_COMMAND_MAX_BYTES == commands_view.MAX_COMMAND_BYTES
    assert w.COMMANDS_PATH == commands_view.COMMANDS_PATH == w.FAMILY_COMMAND_PATH
    for prefix, (audience, key_file) in w.PORTAL_ROUTES.items():
        assert (prefix + "/api/commands").removeprefix(prefix) == w.FAMILY_COMMAND_PATH
        assert audience in ("trex-command", "trex-staging-command")
        assert key_file.startswith("~/.config/family-apps/")
    assert w.FAMILY_KEY_FILE_DEFAULT.startswith("~/.config/trex/")


def test_command_key_file_format() -> None:
    text = base64.b64encode(KEY).decode()
    assert w.parse_command_key(f"\n {text}\n") == KEY
    for bad in (
        base64.b64encode(KEY[:31]).decode(),
        text.rstrip("="),
        base64.urlsafe_b64encode(b"\xfb" * 32).decode(),
        "not base64 at all",
        "",
    ):
        with pytest.raises(ValueError):
            w.parse_command_key(bad)


def test_env_parsing() -> None:
    assert w.parse_owner_emails(" Owner@Example.com, ,b@example.org ") == frozenset(
        {OWNER, "b@example.org"}
    )
    assert w.parse_origins(f"{ORIGIN}, https://pop-os.example.ts.net:8443") == frozenset(
        {ORIGIN, "https://pop-os.example.ts.net:8443"}
    )
    for bad in ("https://x.example/", "http://x.example", "https://X.example", "https://x:443"):
        with pytest.raises(ValueError):
            w.parse_origins(bad)
    with pytest.raises(ValueError):
        w.parse_owner_emails("not-an-email")
    env = {"TREX_OWNER_EMAILS": OWNER, "TREX_FAMILY_ORIGINS": ORIGIN}
    cfg = w.family_config_from_env(env, KEY)
    assert cfg.enabled and cfg.audience == "trex-command"
    assert KEY.hex() not in repr(cfg) and "key" not in repr(cfg)
    assert not w.family_config_from_env({}, KEY).enabled
    with pytest.raises(ValueError):
        w.family_config_from_env({**env, "TREX_FAMILY_AUDIENCE": "speech-lab"}, KEY)


# =============================================================================
# C3.2 read views
# =============================================================================

TODAY = load("today.json")
TODAY_U = load("today_unavailable.json")
ACTIVITY = load("activity.json")


@pytest.mark.parametrize(
    ("path", "value"),
    [
        # a known block never carries a null figure (unknown needs a reason)
        ("session.opens_at", None),
        ("desk_owner.heartbeat_age_s", None),
        ("gateway.checked_at", None),
        ("approvals.problem_count", None),
        ("positions.holdings.equity_positions", None),
        ("positions.risk.open_loss_cents", None),
        ("acceptance.streak_sessions", None),
        ("next_events.items", None),
        ("controls.queued.items", None),
        # a reason without the unavailable status
        ("gateway.unavailable_reason", "gateway_watch_stale"),
        ("positions.risk.unavailable_reason", "risk_snapshot_stale"),
    ],
)
def test_known_blocks_refuse_nulls_and_reasons(path: str, value: Any) -> None:
    with pytest.raises(ValidationError):
        w.TodayView.model_validate(put(TODAY, path, value))


@pytest.mark.parametrize(
    ("path", "value"),
    [
        # zero is a value, never a stand-in for unknown
        ("positions.holdings.option_positions", 0),
        ("positions.risk.settled_cash_cents", 0),
        ("acceptance.streak_sessions", 0),
        ("desk_owner.lock_held", False),
        ("approvals.items", []),
        ("session.early_close", False),
        # unavailable without saying why
        ("gateway.unavailable_reason", None),
        ("controls.halt.unavailable_reason", None),
    ],
)
def test_unavailable_blocks_carry_no_figures_and_a_reason(path: str, value: Any) -> None:
    with pytest.raises(ValidationError):
        w.TodayView.model_validate(put(TODAY_U, path, value))


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("controls.halt.since", "2026-10-22T13:00:00Z"),  # clear with a time
        ("controls.flatten.status", "standing"),  # standing without since/reason
        ("controls.halt.name", "FLATTEN"),  # a switch reporting another file
        ("desk_owner.heartbeat_age_s", 121),  # healthy but older than stale_after_s
        ("desk_owner.lock_held", False),  # healthy without the lock
        ("desk_owner.exit_owner_ready", False),
        ("session.session", "2026-10-23"),  # open, but not today's session
        ("approvals.items.0.approve_args.proposal_sha256", "0" * 64),
        ("approvals.items.0.seconds_to_deadline", -1),
        ("approvals.items.0.precheck_blockers", ["not_a_code"]),
        ("next_events.items.0.subject", None),  # per-proposal kind without subject
        ("next_events.items.1.subject", "x"),  # global kind with a subject
        ("next_events.items.0.at", "2026-10-22T21:00:00Z"),  # out of time order
        ("blockers.0.code", "not_a_code"),
        ("blockers.0.message", ""),
        ("execution_enabled", True),
        ("acceptance.live_authorized", True),
        ("acceptance.live_authorized", 0),
        ("acceptance.streak_required", 9),
        ("acceptance.streak_required", 10.0),
    ],
)
def test_today_refusals(path: str, value: Any) -> None:
    with pytest.raises(ValidationError):
        w.TodayView.model_validate(put(TODAY, path, value))


def test_today_accepts_conditional_and_vocabulary_values() -> None:
    standing = put(TODAY, "controls.pause_entries.status", "standing")
    standing = put(standing, "controls.pause_entries.since", "2026-10-22T14:01:00Z")
    standing = put(standing, "controls.pause_entries.reason", "")
    assert w.TodayView.model_validate(standing).controls.pause_entries.reason == ""
    stale = put(TODAY, "desk_owner.status", "stale")
    stale = put(stale, "desk_owner.heartbeat_age_s", 121)
    stale = put(stale, "desk_owner.exit_owner_ready", False)
    assert w.TodayView.model_validate(stale).desk_owner.status == "stale"
    for code in ("halt", "entries_paused", "holdings_missing", "gateway_needs_login"):
        assert w.TodayView.model_validate(put(TODAY, "blockers.0.code", code))
    after = put(TODAY, "session.status", "after_close")
    with pytest.raises(ValidationError):
        w.TodayView.model_validate(after)  # after the close, session is the next one


def test_today_acceptance_line_is_the_documents_summary() -> None:
    view = acc.AcceptanceView.model_validate(load("acceptance_view.json"))
    assert view.document is not None
    summary = w.AcceptanceSummary.of(view.document)
    assert c.model_document(summary) == TODAY["acceptance"]


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("items.0.id", "desk_event:x"),  # id prefix is not its source
        ("items.0.actor", None),  # neither actor nor reason
        ("items.3.actor_unavailable_reason", None),
        ("items.1.command_key", "5f0c2a8e-1b3d-4c6e-9a7b-2d4e6f8a0b1c"),  # desk event with a key
        ("items.0.command_status", None),
        ("items.2.reason", None),  # refused without a reason
        ("items.0.reason", "halt"),  # applied with a reason
        ("items.2.refs", {"wallet": "x"}),
        ("items.1.at", "2026-10-22T14:06:00Z"),  # not newest first
        ("items.2.id", "command:5f0c2a8e-1b3d-4c6e-9a7b-2d4e6f8a0b1c"),  # repeated id
        ("next_cursor", None),  # has_more without a cursor
        ("limit", 3),  # more items than the limit
        ("sources.3.source", "audit"),
        ("execution_enabled", True),
    ],
)
def test_activity_refusals(path: str, value: Any) -> None:
    with pytest.raises(ValidationError):
        w.ActivityPage.model_validate(put(ACTIVITY, path, value))


def test_activity_query_defaults() -> None:
    query = w.ActivityQuery()
    assert (query.limit, query.cursor, query.sources) == (50, None, ())
    with pytest.raises(ValidationError):
        w.ActivityQuery(limit=201)


def test_control_labels() -> None:
    fixture = load("control_labels.json")
    assert fixture == {"primary_control": w.PRIMARY_CONTROL, "labels": dict(w.CONTROL_LABELS)}
    assert w.PRIMARY_CONTROL == "pause_entries"
    assert w.CONTROL_LABELS["halt"] == "Emergency stop (also stops exits)"
    from typing import get_args

    from tree_options.trex.contracts.commands import CommandKind

    assert set(w.CONTROL_LABELS) <= set(get_args(CommandKind))


def test_vocabulary_fixture_pins_the_modules() -> None:
    """``vocabulary.json`` is the frozen list both Python and TypeScript check."""
    assert load("vocabulary.json") == {
        "wave3_reasons": [m.value for m in w.Wave3Reason],
        "family_refusals": [m.value for m in w.FamilyRefusal],
        "drill_ids": list(acc.DRILL_IDS),
        "session_reasons": sorted(acc.SESSION_REASONS),
        "lifecycle_reasons": sorted(acc.LIFECYCLE_REASONS),
        "drill_reasons": sorted(acc.DRILL_REASONS),
        "verdict_reasons": sorted(acc.VERDICT_REASONS),
        "activity_sources": list(w.ACTIVITY_SOURCES),
        "pilot_roles": list(w.PILOT_ROLES),
        "pilot_budget_ceilings": dict(w.PILOT_BUDGET_CEILINGS),
        "api_paths": {
            "today": "/api/today",
            "activity": "/api/activity",
            "acceptance": "/api/acceptance",
            "commands": "/api/commands",
            "proposals": "/api/proposals",
            "portfolio": "/api/portfolio",
        },
        "acceptance_thresholds": {
            "streak_sessions": 10,
            "lifecycles_per_kind": 10,
            "heartbeat_min_percent": 99,
            "max_exit_unguarded_s": 300,
            "open_check_window_s": 900,
        },
    }
    paths = load("vocabulary.json")["api_paths"]
    assert (w.TODAY_PATH, w.ACTIVITY_PATH, w.ACCEPTANCE_PATH) == (
        paths["today"],
        paths["activity"],
        paths["acceptance"],
    )
    assert (w.PROPOSALS_PATH, w.PORTFOLIO_PATH) == (paths["proposals"], paths["portfolio"])
    assert (
        acc.REQUIRED_STREAK_SESSIONS,
        acc.REQUIRED_LIFECYCLES_PER_KIND,
        acc.HEARTBEAT_MIN_PERCENT,
        acc.MAX_EXIT_UNGUARDED_S,
        acc.OPEN_CHECK_WINDOW_S,
    ) == (10, 10, 99, 300, 900)


def test_wave3_vocabulary_is_unique_and_disjoint_from_blocker() -> None:
    values = [m.value for m in w.Wave3Reason]
    assert len(values) == len(set(values))
    assert not set(values) & {m.value for m in c.Blocker}
    for reason in w.Wave3Reason:
        assert reason.name == reason.value.upper()
    for refusal_ in w.FamilyRefusal:
        assert refusal_.name == refusal_.value.upper()
    assert len({r.value for r in w.FamilyRefusal}) == len(w.FamilyRefusal)


def test_wave2_mirror_fixtures_are_what_wave2_serves(tmp_path: Path) -> None:
    """The TS mirrors of ``/api/proposals`` and ``/api/portfolio`` are pinned
    to the Wave 2 functions' real output (reference, not redefinition)."""
    from tree_options.trex_web.commands_view import proposals_payload
    from tree_options.trex_web.portfolio_view import portfolio_payload

    run, sup = tmp_path / "run", tmp_path / "sup"
    (run / "proposals").mkdir(parents=True)
    sup.mkdir()
    proposal = c.EntryProposal.model_validate(
        json.loads((FIXTURES / "proposal_equity.json").read_text())
    )
    (run / "proposals" / f"{proposal.proposal_id}.json").write_bytes(c.proposal_bytes(proposal))
    now = Z("2026-10-22T14:02:00Z")
    assert proposals_payload(run, sup, now) == load("wave2_proposal_list.json")
    assert portfolio_payload(run, now) == load("wave2_portfolio_unavailable.json")


# =============================================================================
# C3.4 trex-acceptance/1 (hand-counted against the real calendar)
# =============================================================================

CAL = session_calendar()
NOV = [
    "2026-11-16",
    "2026-11-17",
    "2026-11-18",
    "2026-11-19",
    "2026-11-20",
    "2026-11-23",
    "2026-11-24",
    "2026-11-25",
    "2026-11-27",  # early close: 14:30Z to 18:00Z, 210 minutes
    "2026-11-30",
]
#: after the 2026-11-30 close (21:00Z), before 2026-12-01's
AFTER_NOV30 = Z("2026-11-30T22:00:00Z")
EV = (acc.EvidenceRef(path="evidence/x.json", sha256="a" * 64),)


def session(day: str, heartbeat: int | None = None, **changes: Any) -> acc.SessionRecord:
    minutes = 210 if day == "2026-11-27" else 390
    values: dict[str, Any] = {
        "session": day,
        "heartbeat_minutes": minutes if heartbeat is None else heartbeat,
        "reconciliation_open": "clean",
        "reconciliation_close": "clean",
        "reconciliation_episodes": [],
        "unowned_orders": 0,
        "max_exit_unguarded_s": 0,
        "unavailable": {},
        "evidence": [],
    }
    values.update(changes)
    return acc.SessionRecord.model_validate(values)


_KEYS = iter(range(10_000))


def lifecycle(
    intent: str, kind: str, approved: str, reconciled: str, **changes: Any
) -> acc.LifecycleRecord:
    """A complete lifecycle: approved and filled at ``approved``, exited by
    policy, flat and reconciled at ``reconciled`` (all instants UTC)."""
    n = next(_KEYS)
    values: dict[str, Any] = {
        "intent_id": intent,
        "asset_kind": kind,
        "proposal_id": f"{n:064x}",
        "approval_key": f"00000000-0000-4000-8000-{n:012x}",
        "approved_at": approved,
        "filled_quantity": 1,
        "first_fill_at": approved,
        "exit_by": "policy",
        "exited_at": reconciled,
        "broker_flat_at": reconciled,
        "reconciled_at": reconciled,
        "commissions_complete": True,
        "commissions_unavailable_reason": None,
        "evidence": [],
    }
    values.update(changes)
    return acc.LifecycleRecord.model_validate(values)


def drills_passed(by: str = OWNER) -> list[acc.DrillRecord]:
    return [
        acc.DrillRecord(
            id=drill_id,
            status="passed",
            evidence=EV,
            attested_by=by,
            at=Z("2026-11-30T23:00:00Z"),
            note="",
        )
        for drill_id in acc.DRILL_IDS
    ]


def ten_per_kind(day: str = "2026-11-23") -> list[acc.LifecycleRecord]:
    out = []
    for i in range(10):
        out.append(lifecycle(f"opt-{i}", "option", f"{day}T15:00:00Z", f"{day}T16:00:00Z"))
        out.append(lifecycle(f"eq-{i}", "equity", f"{day}T15:00:00Z", f"{day}T16:00:00Z"))
    return out


def compute(
    sessions: list[acc.SessionRecord],
    lifecycles: list[acc.LifecycleRecord] | None = None,
    drills: list[acc.DrillRecord] | None = None,
    computed_at: datetime = AFTER_NOV30,
) -> acc.AcceptanceDocument:
    return acc.compute_acceptance(
        sessions=sessions,
        lifecycles=lifecycles if lifecycles is not None else ten_per_kind(),
        drills=drills if drills is not None else drills_passed(),
        owners=[OWNER],
        calendar=CAL,
        computed_at=computed_at,
    )


def test_ten_sessions_ten_lifecycles_per_kind_and_drills_meet_acceptance() -> None:
    doc = compute([session(d) for d in NOV])
    assert doc.verdict == "met" and doc.verdict_reasons == ()
    assert (doc.streak.current_sessions, doc.streak.best_sessions) == (10, 10)
    assert (doc.streak.current_start, doc.streak.current_end) == (
        date(2026, 11, 16),
        date(2026, 11, 30),
    )
    assert (doc.per_kind.option.counted, doc.per_kind.equity.counted) == (10, 10)
    assert doc.drills_passed == 7 and doc.live_authorized is False
    assert [s.regular_minutes for s in doc.calendar_window] == [390] * 8 + [210, 390]
    acc.verify_acceptance(doc, CAL)
    assert c.model_document(doc)["live_authorized"] is False


def test_non_qualifying_session_resets_the_streak() -> None:
    sessions = [session(d, 386 if d == "2026-11-20" else None) for d in NOV]
    lifecycles = [
        *ten_per_kind(),
        lifecycle("opt-early", "option", "2026-11-17T15:00:00Z", "2026-11-17T16:00:00Z"),
    ]
    doc = compute(sessions, lifecycles)
    reasons = {r.session.isoformat(): r.reasons for r in doc.session_results}
    assert reasons["2026-11-20"] == ("heartbeat_below_99pct",)
    # 11-16..11-19 is a run of 4; 11-23, 24, 25, 27, 30 is the current run of 5
    assert (doc.streak.current_sessions, doc.streak.best_sessions) == (5, 5)
    assert doc.streak.current_start == date(2026, 11, 23)
    # the lifecycle approved on 11-17 completed, but before the current streak
    assert (doc.per_kind.option.completed_total, doc.per_kind.option.counted) == (11, 10)
    assert doc.verdict == "not_met" and doc.verdict_reasons == ("streak_short",)


@pytest.mark.parametrize(
    ("day", "heartbeat", "reasons"),
    [
        ("2026-11-16", 390, ()),
        ("2026-11-16", 387, ()),  # 38700 >= 38610
        ("2026-11-16", 386, ("heartbeat_below_99pct",)),  # 38600 < 38610
        ("2026-11-27", 208, ()),  # early close: 20800 >= 20790
        ("2026-11-27", 207, ("heartbeat_below_99pct",)),  # 20700 < 20790
        ("2026-11-27", 211, ("heartbeat_minutes_exceed_session",)),
    ],
)
def test_heartbeat_99_percent_of_regular_minutes(
    day: str, heartbeat: int, reasons: tuple[str, ...]
) -> None:
    doc = compute([session(day, heartbeat)], [], computed_at=Z(f"{day}T23:00:00Z"))
    assert [r.session.isoformat() for r in doc.session_results] == [day]
    assert doc.session_results[0].reasons == reasons
    assert doc.streak.current_sessions == (0 if reasons else 1)


def test_an_unrecorded_session_resets_the_streak() -> None:
    doc = compute([session(d) for d in NOV if d != "2026-11-24"])
    missing = [r for r in doc.session_results if not r.recorded]
    assert [(r.session, r.reasons) for r in missing] == [
        (date(2026, 11, 24), ("session_not_recorded",))
    ]
    # 11-16..11-23 is 6; 11-25, 27, 30 is 3
    assert (doc.streak.current_sessions, doc.streak.best_sessions) == (3, 6)


def test_a_trailing_unrecorded_session_zeroes_the_streak() -> None:
    doc = compute([session(d) for d in NOV], computed_at=Z("2026-12-01T22:00:00Z"))
    assert doc.session_results[-1].session == date(2026, 12, 1)
    assert doc.session_results[-1].reasons == ("session_not_recorded",)
    assert doc.streak.current_sessions == 0
    assert (doc.streak.current_start, doc.streak.current_end) == (None, None)
    assert doc.streak.best_sessions == 10
    assert doc.per_kind.option.counted == 0  # no current streak, nothing counts
    assert doc.verdict_reasons == (
        "streak_short",
        "option_lifecycles_short",
        "equity_lifecycles_short",
    )


@pytest.mark.parametrize(
    ("metric", "reason"),
    [
        ("heartbeat_minutes", "heartbeat_unavailable"),
        ("reconciliation_open", "reconciliation_unavailable"),
        ("reconciliation_close", "reconciliation_unavailable"),
        ("reconciliation_episodes", "reconciliation_unavailable"),
        ("unowned_orders", "unowned_orders_unavailable"),
        ("max_exit_unguarded_s", "exit_guard_unavailable"),
    ],
)
def test_an_unknown_metric_never_qualifies(metric: str, reason: str) -> None:
    record = session("2026-11-16", **{metric: None, "unavailable": {metric: "source_unreadable"}})
    doc = compute([record], [], computed_at=Z("2026-11-16T23:00:00Z"))
    assert doc.session_results[0].reasons == (reason,)
    assert doc.streak.current_sessions == 0


def test_unknown_metrics_are_named() -> None:
    with pytest.raises(ValidationError):  # a null metric must be named
        session("2026-11-16", unowned_orders=None)
    with pytest.raises(ValidationError):  # a named metric must be null
        session("2026-11-16", unavailable={"unowned_orders": "x"})


def episode(start: str, resolved: str | None) -> dict[str, Any]:
    return {"started_at": start, "status": "reconciliation_required", "resolved_at": resolved}


@pytest.mark.parametrize(
    ("changes", "reasons"),
    [
        (
            {"reconciliation_close": "reconciliation_required"},
            ("reconciliation_not_clean_at_close",),
        ),
        ({"reconciliation_close": "unavailable"}, ("reconciliation_not_clean_at_close",)),
        # a dirty open resolved in session (episode started by open + 15 min)
        (
            {
                "reconciliation_open": "reconciliation_required",
                "reconciliation_episodes": [
                    episode("2026-11-16T14:44:00Z", "2026-11-16T15:00:00Z")
                ],
            },
            (),
        ),
        (
            {
                "reconciliation_open": "unavailable",
                "reconciliation_episodes": [
                    episode("2026-11-16T14:30:00Z", "2026-11-16T14:50:00Z")
                ],
            },
            (),
        ),
        ({"reconciliation_open": "reconciliation_required"}, ("reconciliation_open_unresolved",)),
        (
            {
                "reconciliation_open": "reconciliation_required",
                "reconciliation_episodes": [
                    episode("2026-11-16T14:46:00Z", "2026-11-16T15:00:00Z")
                ],
            },
            ("reconciliation_open_unresolved",),
        ),
        (
            {"reconciliation_episodes": [episode("2026-11-16T17:00:00Z", None)]},
            ("reconciliation_unresolved_in_session",),
        ),
        (
            {"reconciliation_episodes": [episode("2026-11-16T20:50:00Z", "2026-11-16T21:05:00Z")]},
            ("reconciliation_unresolved_in_session",),
        ),
        (
            {"reconciliation_episodes": [episode("2026-11-16T17:00:00Z", "2026-11-16T17:20:00Z")]},
            (),
        ),
        ({"unowned_orders": 1}, ("unowned_orders_present",)),
        ({"max_exit_unguarded_s": 300}, ()),
        ({"max_exit_unguarded_s": 301}, ("exit_unguarded_over_5min",)),
    ],
)
def test_session_rules(changes: dict[str, Any], reasons: tuple[str, ...]) -> None:
    doc = compute([session("2026-11-16", **changes)], [], computed_at=Z("2026-11-16T23:00:00Z"))
    assert doc.session_results[0].reasons == reasons


@pytest.mark.parametrize(
    ("changes", "reasons"),
    [
        ({}, ()),
        ({"exit_by": "flatten"}, ()),
        ({"exit_by": "manual"}, ("exit_not_by_policy",)),
        ({"commissions_complete": False}, ("commissions_missing",)),
        (
            {"commissions_complete": None, "commissions_unavailable_reason": "report_pending"},
            ("commissions_unknown",),
        ),
        ({"reconciled_at": None}, ("not_reconciled",)),
        ({"broker_flat_at": None, "reconciled_at": None}, ("broker_not_flat", "not_reconciled")),
        (
            {
                "exit_by": "not_exited",
                "exited_at": None,
                "broker_flat_at": None,
                "reconciled_at": None,
            },
            ("broker_not_flat", "not_exited", "not_reconciled"),
        ),
        (
            {
                "filled_quantity": 0,
                "first_fill_at": None,
                "exit_by": "not_exited",
                "exited_at": None,
                "broker_flat_at": None,
                "reconciled_at": None,
            },
            ("broker_not_flat", "not_exited", "not_filled", "not_reconciled"),
        ),
    ],
)
def test_lifecycle_completeness(changes: dict[str, Any], reasons: tuple[str, ...]) -> None:
    record = lifecycle("eq-x", "equity", "2026-11-16T15:00:00Z", "2026-11-16T16:00:00Z", **changes)
    doc = compute([session("2026-11-16")], [record], computed_at=Z("2026-11-16T23:00:00Z"))
    result = doc.lifecycle_results[0]
    assert result.reasons == reasons
    assert result.completed == (not reasons) == result.counted


@pytest.mark.parametrize(
    "changes",
    [
        {"first_fill_at": "2026-11-16T14:59:59Z"},  # filled before approval
        {"filled_quantity": 0, "first_fill_at": None},  # exited without a fill
        {"first_fill_at": None},  # filled without a fill time
        {"broker_flat_at": "2026-11-16T15:59:00Z"},  # flat before the exit
        {"reconciled_at": "2026-11-16T15:59:00Z", "broker_flat_at": "2026-11-16T16:00:00Z"},
        {"commissions_complete": None},  # unknown without a reason
        {"commissions_unavailable_reason": "x"},  # a reason for a known value
        {"exit_by": "policy", "exited_at": None},
    ],
)
def test_lifecycle_record_shape_refusals(changes: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        lifecycle("eq-x", "equity", "2026-11-16T15:00:00Z", "2026-11-16T16:00:00Z", **changes)


def test_per_kind_counters_and_the_window() -> None:
    lifecycles = [
        lifecycle(f"opt-{i}", "option", "2026-11-23T15:00:00Z", "2026-11-23T16:00:00Z")
        for i in range(10)
    ]
    lifecycles += [
        lifecycle(f"eq-{i}", "equity", "2026-11-23T15:00:00Z", "2026-11-23T16:00:00Z")
        for i in range(9)
    ]
    # complete, but reconciled on 12-01, after the window's last session
    lifecycles.append(
        lifecycle("eq-late", "equity", "2026-11-30T15:00:00Z", "2026-12-01T15:00:00Z")
    )
    lifecycles.append(
        lifecycle(
            "eq-open",
            "equity",
            "2026-11-24T15:00:00Z",
            "2026-11-24T16:00:00Z",
            exit_by="not_exited",
            exited_at=None,
            broker_flat_at=None,
            reconciled_at=None,
        )
    )
    doc = compute([session(d) for d in NOV], lifecycles)
    assert c.model_document(doc.per_kind) == {
        "option": {"completed_total": 10, "counted": 10, "required": 10},
        "equity": {"completed_total": 10, "counted": 9, "required": 10},
    }
    assert doc.verdict_reasons == ("equity_lifecycles_short",)


def test_drills_need_owner_attestation() -> None:
    drills = drills_passed()
    drills[2] = acc.DrillRecord(
        id="gateway_outage",
        status="passed",
        evidence=EV,
        attested_by="helper@example.com",
        at=Z("2026-11-30T23:00:00Z"),
        note="",
    )
    drills[4] = acc.DrillRecord(
        id="backup_restore",
        status="failed",
        evidence=EV,
        attested_by=OWNER,
        at=Z("2026-11-30T23:00:00Z"),
        note="restore lost the ledger",
    )
    del drills[6]  # unassisted_use: never run
    doc = compute([session(d) for d in NOV], drills=drills)
    by_id = {r.id: r.reasons for r in doc.drill_results}
    assert by_id["gateway_outage"] == ("attester_not_owner",)
    assert by_id["backup_restore"] == ("drill_failed",)
    assert by_id["unassisted_use"] == ("drill_not_run",)
    assert doc.drills_passed == 4
    assert doc.verdict_reasons == (
        "drill_not_passed:gateway_outage",
        "drill_not_passed:backup_restore",
        "drill_not_passed:unassisted_use",
    )


@pytest.mark.parametrize(
    "values",
    [
        {"status": "passed", "evidence": [], "attested_by": OWNER, "at": "2026-11-30T23:00:00Z"},
        {"status": "passed", "evidence": [EV[0]], "attested_by": None, "at": None},
        {"status": "not_run", "evidence": [], "attested_by": OWNER, "at": None},
        {"status": "not_run", "evidence": [EV[0]], "attested_by": None, "at": None},
    ],
)
def test_drill_record_shape(values: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        acc.DrillRecord.model_validate({"id": "relogin", "note": "", **values})


def test_acceptance_reason_vocabularies_are_frozen() -> None:
    assert acc.SESSION_REASONS == {
        "session_not_recorded",
        "heartbeat_unavailable",
        "heartbeat_minutes_exceed_session",
        "heartbeat_below_99pct",
        "reconciliation_unavailable",
        "reconciliation_not_clean_at_close",
        "reconciliation_open_unresolved",
        "reconciliation_unresolved_in_session",
        "unowned_orders_unavailable",
        "unowned_orders_present",
        "exit_guard_unavailable",
        "exit_unguarded_over_5min",
    }
    assert acc.LIFECYCLE_REASONS == {
        "not_filled",
        "not_exited",
        "exit_not_by_policy",
        "broker_not_flat",
        "not_reconciled",
        "commissions_unknown",
        "commissions_missing",
    }
    assert acc.DRILL_REASONS == {"drill_not_run", "drill_failed", "attester_not_owner"}
    assert acc.DRILL_IDS == (
        "relogin",
        "restart_mid_position",
        "gateway_outage",
        "duplicate_command",
        "backup_restore",
        "notification",
        "unassisted_use",
    )


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("verdict", "met"),
        ("verdict_reasons", []),
        ("streak.current_sessions", 6),
        ("streak.best_sessions", 4),
        ("session_results.4.qualifying", True),
        ("session_results.4.reasons", []),
        ("per_kind.option.counted", 9),
        ("lifecycle_results.0.counted", False),
        ("drills_passed", 6),
        ("drill_results.0.passed", False),
        ("drills_required", 6),
        ("live_authorized", True),
        ("live_authorized", 0),
        ("live_authorized", "false"),
    ],
)
def test_the_verdict_is_computed_never_asserted(path: str, value: Any) -> None:
    sessions = [session(d, 386 if d == "2026-11-20" else None) for d in NOV]
    document = c.model_document(compute(sessions))
    assert acc.AcceptanceDocument.model_validate(document).verdict == "not_met"
    with pytest.raises(ValidationError):
        acc.AcceptanceDocument.model_validate(put(document, path, value))


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("drills.0.id", "notification"),  # drills out of order / repeated
        ("owners", ["z@example.com", OWNER]),  # unsorted
        ("owners", []),
        ("sessions.1.session", "2026-11-16"),  # repeated session
        ("calendar_window.0.session", "2026-11-14"),  # a Saturday's window
    ],
)
def test_acceptance_input_rules(path: str, value: Any) -> None:
    document = c.model_document(compute([session(d) for d in NOV]))
    with pytest.raises(ValidationError):
        acc.AcceptanceDocument.model_validate(put(document, path, value))


def test_records_outside_the_window_refuse() -> None:
    with pytest.raises(ValidationError):  # a Saturday is no session
        compute([session("2026-11-14")], [], computed_at=Z("2026-11-16T23:00:00Z"))
    with pytest.raises(ValidationError):  # recorded before the session closed
        compute([session("2026-11-16")], [], computed_at=Z("2026-11-16T20:00:00Z"))


def test_verify_catches_another_calendar_and_a_hidden_tail() -> None:
    doc = compute([session(d) for d in NOV])
    tampered = put(c.model_document(doc), "calendar_sha256", "f" * 64)
    with pytest.raises(ValueError, match="acceptance_calendar_mismatch"):
        acc.verify_acceptance(acc.AcceptanceDocument.model_validate(tampered), CAL)
    # moving computed_at past 12-01's close while hiding that session parses
    # (the document is self-consistent) but does not verify
    hidden = put(c.model_document(doc), "computed_at", "2026-12-01T22:00:00Z")
    parsed = acc.AcceptanceDocument.model_validate(hidden)
    assert parsed.verdict == "met"
    with pytest.raises(ValueError, match="acceptance_window_mismatch"):
        acc.verify_acceptance(parsed, CAL)


def test_empty_inputs_compute_not_met() -> None:
    doc = compute([], [], [], computed_at=AFTER_NOV30)
    assert doc.calendar_window == () and doc.streak.current_sessions == 0
    assert doc.drills_passed == 0 and doc.verdict == "not_met"
    assert [r.reasons for r in doc.drill_results] == [("drill_not_run",)] * 7


def test_acceptance_view_shape() -> None:
    view = load("acceptance_view.json")
    for path, value in (
        ("document", None),
        ("unavailable_reason", "acceptance_missing"),
        ("live_authorized", True),
    ):
        with pytest.raises(ValidationError):
            acc.AcceptanceView.model_validate(put(view, path, value))
    missing = {**view, "status": "unavailable", "unavailable_reason": "acceptance_missing"}
    assert acc.AcceptanceView.model_validate({**missing, "document": None}).document is None


# =============================================================================
# C3.5 trex-pilot-registration/1
# =============================================================================

DRAFT = load("pilot_registration_baseline_draft.json")
SEALED = load("pilot_registration_challenger_sealed.json")
OBSERVATION = load("pilot_registration_observation.json")


def body_sha256_oracle(document: dict[str, Any]) -> str:
    body = {
        k: v for k, v in document.items() if k not in ("status", "sealed_by", "sealed_at", "sha256")
    }
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def test_sealed_hash_matches_a_stdlib_oracle() -> None:
    for document in (SEALED, OBSERVATION):
        assert document["sha256"] == body_sha256_oracle(document)
        model = w.PilotRegistration.model_validate(document)
        assert w.registration_body_sha256(model) == document["sha256"]


def test_sealing_is_an_owner_act_and_happens_once() -> None:
    draft = w.PilotRegistration.model_validate(DRAFT)
    sealed = w.seal_registration(
        draft, sealed_by=OWNER, owners=[OWNER], at=Z("2026-10-21T19:00:00Z")
    )
    assert sealed.status == "sealed" and sealed.sha256 == body_sha256_oracle(DRAFT)
    with pytest.raises(ValueError, match="registration_already_sealed"):
        w.seal_registration(sealed, sealed_by=OWNER, owners=[OWNER], at=Z("2026-10-21T19:00:00Z"))
    with pytest.raises(ValueError, match="sealer_not_owner"):
        w.seal_registration(
            draft, sealed_by="x@example.com", owners=[OWNER], at=Z("2026-10-21T19:00:00Z")
        )
    with pytest.raises(ValidationError):  # sealed before it was created
        w.seal_registration(draft, sealed_by=OWNER, owners=[OWNER], at=Z("2026-10-21T17:00:00Z"))


@pytest.mark.parametrize(
    ("document", "path", "value"),
    [
        (SEALED, "hypothesis", "edited after sealing"),
        (SEALED, "budgets.storage_mb", 255),
        (SEALED, "sha256", "0" * 64),
        (SEALED, "sealed_by", None),
        (DRAFT, "sealed_by", OWNER),
        (DRAFT, "sha256", "0" * 64),
        (SEALED, "controls", []),  # a challenger without its baseline
        (DRAFT, "existing_registration", {"path": "a.json", "sha256": "0" * 64}),
        (OBSERVATION, "existing_registration", None),
        (SEALED, "controls.0.program_id", "eq-challenger-spy-gap"),  # itself
        (DRAFT, "budgets.requests_per_day", 5_001),
        (DRAFT, "budgets.compute_minutes_per_day", 121),
        (DRAFT, "budgets.storage_mb", 2_049),
        (DRAFT, "budgets.storage_mb", 1.0),
        (DRAFT, "data_sources", []),
        (DRAFT, "universe.symbols", ["SPY", "QQQ"]),  # unsorted
        (DRAFT, "universe.symbols", []),
        (DRAFT, "status", "running"),
    ],
)
def test_registration_refusals(document: dict[str, Any], path: str, value: Any) -> None:
    with pytest.raises(ValidationError):
        w.PilotRegistration.model_validate(put(document, path, value))


def usage(**changes: Any) -> w.BudgetUsage:
    values = {**load("pilot_usage.json"), **changes}
    return w.BudgetUsage.model_validate(values)


def test_run_blockers() -> None:
    sealed = w.PilotRegistration.model_validate(SEALED)
    draft = w.PilotRegistration.model_validate(DRAFT)
    owners = [OWNER]
    assert w.registration_run_blockers(sealed, usage=usage(), owners=owners) == ()
    assert w.registration_run_blockers(sealed, usage=None, owners=owners) == (
        "budget_usage_unknown",
    )
    assert w.registration_run_blockers(
        draft, usage=usage(program_id="eq-baseline-spy"), owners=owners
    ) == ("registration_not_sealed",)
    assert w.registration_run_blockers(sealed, usage=usage(), owners=["x@example.com"]) == (
        "registration_sealer_not_owner",
    )
    assert w.registration_run_blockers(
        sealed, usage=usage(program_id="eq-baseline-spy"), owners=owners
    ) == ("budget_usage_mismatch",)
    # budgets: 2000 requests, 30 minutes, 256 MiB; the next unit must fit
    assert w.registration_run_blockers(sealed, usage=usage(requests=1_999), owners=owners) == ()
    exhausted = usage(requests=2_000, compute_seconds=1_800, storage_bytes=256 * 1_048_576)
    assert w.registration_run_blockers(sealed, usage=exhausted, owners=owners) == (
        "budget_requests_exhausted",
        "budget_compute_exhausted",
        "budget_storage_exhausted",
    )
    assert (
        w.registration_run_blockers(
            sealed,
            usage=usage(compute_seconds=1_799, storage_bytes=256 * 1_048_576 - 1),
            owners=owners,
        )
        == ()
    )


def test_pilot_roster() -> None:
    rows = [w.PilotRegistration.model_validate(d) for d in (OBSERVATION, DRAFT, SEALED)]
    assert w.pilot_roster_problems(rows) == ()
    assert w.pilot_roster_problems(rows[:2]) == ("pilot_role_count:challenger",)
    assert w.pilot_roster_problems([rows[0], rows[2]]) == (
        "pilot_role_count:baseline",
        "pilot_control_unknown:eq-baseline-spy",
    )
    assert w.PILOT_ROLES == ("observation", "baseline", "challenger")
    args = w.SealRegistrationArgs(program_id="eq-baseline-spy", registration_sha256="a" * 64)
    assert c.model_document(args) == {
        "program_id": "eq-baseline-spy",
        "registration_sha256": "a" * 64,
    }


# =============================================================================
# C3.7 layout, import hygiene, AST guards
# =============================================================================


def test_layout_paths() -> None:
    run = w.Wave3RunDir(Path("/state/desk-paper"))
    assert run.assertion("ab" * 16) == Path(
        f"/state/desk-paper/authority/assertions/{'ab' * 16}.json"
    )
    accept = w.AcceptanceDir(Path("/state/acceptance"))
    assert accept.session(date(2026, 11, 16)) == Path("/state/acceptance/sessions/2026-11-16.json")
    assert accept.lifecycle("eq-1") == Path("/state/acceptance/lifecycles/eq-1.json")
    assert accept.drill("relogin") == Path("/state/acceptance/drills/relogin.json")
    assert accept.document() == Path("/state/acceptance/acceptance.json")
    pilot = w.PilotDir(Path("/state/pilot"))
    assert pilot.draft("p") == Path("/state/pilot/registrations/p.draft.json")
    assert pilot.sealed("p") == Path("/state/pilot/registrations/p.sealed.json")
    assert pilot.usage("p", date(2026, 10, 22)) == Path("/state/pilot/usage/p/2026-10-22.json")


def test_wave3_contracts_import_without_a_broker_library() -> None:
    env = {**os.environ, "PYTHONPATH": str(REPO / "src")}
    probe = (
        "import sys, tree_options.trex.contracts.wave3, tree_options.trex.contracts.acceptance;"
        " sys.exit(1 if 'ib_async' in sys.modules or 'fastapi' in sys.modules else 0)"
    )
    done = subprocess.run(
        [sys.executable, "-B", "-c", probe], cwd=REPO, env=env, capture_output=True, timeout=120
    )
    assert done.returncode == 0, done.stderr.decode()[-2000:]


NEW_PYTHON = [
    REPO / "src/tree_options/trex/contracts/wave3.py",
    REPO / "src/tree_options/trex/contracts/acceptance.py",
    Path(__file__),
]
NEW_TEXT = [
    *NEW_PYTHON,
    REPO / "web/src/lib/contracts.ts",
    REPO / "web/src/lib/routes.ts",
    REPO / "web/src/lib/contracts.test.ts",
    REPO / "docs/product/WAVE3-CONTRACTS.md",
    *sorted(WAVE3.glob("*.json")),
]


@pytest.mark.parametrize("path", NEW_PYTHON[:2], ids=lambda p: p.name)
def test_no_timedelta_outside_time(path: Path) -> None:
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            assert node.id != "timedelta", f"{path.name}:{node.lineno}"
        if isinstance(node, ast.Attribute):
            assert node.attr != "timedelta", f"{path.name}:{node.lineno}"
        if isinstance(node, ast.ImportFrom):
            assert all(a.name != "timedelta" for a in node.names), f"{path.name}:{node.lineno}"


@pytest.mark.parametrize("path", NEW_TEXT, ids=lambda p: p.name)
def test_no_en_or_em_dashes(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    for dash in (chr(0x2013), chr(0x2014)):  # en dash, em dash
        assert dash not in text, f"{path.name} contains U+{ord(dash):04X}"


def test_the_package_reexports_the_wave3_modules_and_the_seal_kind() -> None:
    """Integration: ``contracts`` re-exports the two Wave 3/4 submodules and
    the ``seal_registration`` amendment's classes (one object each)."""
    from typing import get_args

    assert c.wave3 is w and c.acceptance is acc
    assert {"wave3", "acceptance", "SealRegistrationArgs", "SealRegistrationCommand"} <= set(
        c.__all__
    )
    assert c.SealRegistrationArgs is w.SealRegistrationArgs
    assert "seal_registration" in get_args(c.CommandKind)
    command = c.parse_command(
        {
            "schema": "trex-command/1",
            "idempotency_key": "5f0c2a8e-1b3d-4c6e-9a7b-2d4e6f8a0b1c",
            "kind": "seal_registration",
            "args": {"program_id": "eq-baseline-spy-weekly", "registration_sha256": "a" * 64},
            "issued_at": "2026-10-22T14:00:00Z",
            "expires_at": "2026-10-22T14:05:00Z",
            "issued_by": {
                "sub": "owner@example.com",
                "ingress": "family",
                "request_id": "web-1",
                "request_sha256": "b" * 64,
            },
        }
    )
    assert isinstance(command, c.SealRegistrationCommand)


def test_new_integration_text_has_no_en_or_em_dashes() -> None:
    for path in (
        REPO / "docs/product/WAVE34-INTEGRATION.md",
        REPO / "docs/product/WAVE2-CONTRACTS.md",
        REPO / "src/tree_options/trex/contracts/__init__.py",
    ):
        text = path.read_text(encoding="utf-8")
        assert chr(0x2013) not in text and chr(0x2014) not in text, path.name
