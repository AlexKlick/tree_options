"""Frozen cross-lane contracts for TREX Waves 3 and 4 (docs/product/WAVE3-CONTRACTS.md).

Wave 3 is the human product (five destinations, desktop and phone, owner
commands through Family Apps); Wave 4 is the paper acceptance
instrumentation and the research pilot. This module fixes what the lanes
exchange. It is schemas and pure helpers only: no file or network I/O, no
broker library, no ``timedelta``.

| Id | What | Schema ids |
| --- | --- | --- |
| C3.1 | family-signed command assertion | ``trex-family-command/1`` (+ ``trex-family-assertion-seen/1``) |
| C3.2 | read views | ``trex-today/1``, ``trex-activity/1`` |
| C3.3 | routes | ``web/src/lib/routes.ts`` (API paths and control labels here) |
| C3.4 | acceptance | ``trex-acceptance/1``, ``trex-acceptance-view/1`` (``contracts.acceptance``) |
| C3.5 | pilot registration | ``trex-pilot-registration/1``, ``trex-pilot-usage/1`` |
| C3.6 | vocabulary | ``Wave3Reason``, ``FamilyRefusal`` |
| C3.7 | layout | ``Wave3RunDir``, ``AcceptanceDir``, ``PilotDir`` |

Wave 2 shapes are reused, never redefined: ``Actor``, ``RawCommandRequest``,
``CommandAuthority``, ``CommandAuthorityRefused``, ``ApproveEntryArgs``,
``CommandKind``, ``Blocker``; ``GET /api/proposals`` stays
``trex-proposal-list/1`` and ``GET /api/portfolio`` stays ``trex-portfolio/1``.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import ipaddress
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum, unique
from pathlib import Path
from typing import Annotated, Any, Final, Literal

from pydantic import (
    AfterValidator,
    BeforeValidator,
    Field,
    StringConstraints,
    TypeAdapter,
    ValidationError,
    model_validator,
)

from tree_options.action_graph.proposal import canonical_bytes
from tree_options.schemas.common import IdStr, StrictModel
from tree_options.trex.contracts.acceptance import (
    AcceptanceDocument,
    AcceptanceView,
    AssetKind,
    EvidenceRef,
    FalseLiteral,
    OwnerEmail,
    ReasonCode,
    SevenLiteral,
    TenLiteral,
)
from tree_options.trex.contracts.canonical import (
    IdempotencyKey,
    PathSafeId,
    Sha256Hex,
    Symbol,
    UTCInstant,
    model_document,
    sha256_hex,
)
from tree_options.trex.contracts.commands import (
    Actor,
    ApproveEntryArgs,
    CommandAuthorityRefused,
    CommandKind,
    RawCommandRequest,
)
from tree_options.trex.contracts.vocabulary import Blocker

__all__ = [
    "ACCEPTANCE_PATH",
    "ACTIVITY_PATH",
    "ACTIVITY_SCHEMA",
    "ACTIVITY_SOURCES",
    "ASSERTION_SEEN_SCHEMA",
    "CLAIM_ORDER",
    "COMMANDS_PATH",
    "CONTROL_LABELS",
    "DESK_SYSTEM_SUB",
    "FAMILY_ACTOR_HEADER",
    "FAMILY_COMMAND_AUDIENCE",
    "FAMILY_COMMAND_MAX_SKEW_S",
    "FAMILY_COMMAND_METHOD",
    "FAMILY_COMMAND_PATH",
    "FAMILY_COMMAND_PROTOCOL",
    "FAMILY_COMMAND_SCHEMA",
    "FAMILY_COMMAND_TTL_S",
    "FAMILY_KEY_FILE_DEFAULT",
    "FAMILY_STAGING_COMMAND_AUDIENCE",
    "PILOT_BUDGET_CEILINGS",
    "PILOT_REGISTRATION_SCHEMA",
    "PILOT_ROLES",
    "PILOT_USAGE_SCHEMA",
    "PORTAL_COMMAND_MAX_BYTES",
    "PORTAL_KEY_CONFIG_FIELD",
    "PORTAL_KEY_FILE_DEFAULT",
    "PORTAL_ROUTES",
    "PORTAL_STAGING_KEY_FILE_DEFAULT",
    "PORTFOLIO_PATH",
    "PRIMARY_CONTROL",
    "PROPOSALS_PATH",
    "TODAY_PATH",
    "TODAY_SCHEMA",
    "TREX_FAMILY_AUDIENCE_ENV",
    "TREX_FAMILY_ORIGINS_ENV",
    "TREX_KEY_FILE_ENV",
    "TREX_OWNER_EMAILS_ENV",
    "AcceptanceDir",
    "AcceptanceDocument",
    "AcceptanceSummary",
    "AcceptanceView",
    "ActivityActor",
    "ActivityItem",
    "ActivityPage",
    "ActivityQuery",
    "ActivitySourceStatus",
    "AssertionSeen",
    "BudgetUsage",
    "Controls",
    "DataSource",
    "DecisionRule",
    "DeskOwnerHealth",
    "EvidenceRef",
    "FamilyAuthorityConfig",
    "FamilyCommandClaims",
    "FamilyRefusal",
    "GatewayHealth",
    "HoldingsSummary",
    "HumanBlocker",
    "KillSwitchState",
    "NextEvents",
    "OwnerEmail",
    "PendingApproval",
    "PendingApprovals",
    "PilotBudgets",
    "PilotControlRef",
    "PilotDir",
    "PilotRegistration",
    "PilotUniverse",
    "PositionsSummary",
    "QueuedCommand",
    "QueuedCommands",
    "ReasonCode",
    "RiskSummary",
    "ScheduledEvent",
    "SealRegistrationArgs",
    "SessionState",
    "TodayView",
    "Wave3Reason",
    "Wave3RunDir",
    "assertion_seen_record",
    "authority_for",
    "decode_family_command",
    "family_config_from_env",
    "mint_family_command",
    "parse_command_key",
    "parse_origins",
    "parse_owner_emails",
    "pilot_roster_problems",
    "registration_body_sha256",
    "registration_run_blockers",
    "seal_registration",
    "sign_family_command",
    "verify_family_command",
]

# =============================================================================
# C3.6 vocabulary
# =============================================================================


@unique
class Wave3Reason(StrEnum):
    """Why a Wave 3 view fact is unavailable, or a Today blocker the Wave 2
    ``Blocker`` vocabulary does not already name. Additive only; disjoint
    from ``Blocker`` (a test pins both)."""

    # -- unavailable reasons --------------------------------------------------
    CALENDAR_UNREADABLE = "calendar_unreadable"
    CALENDAR_EXHAUSTED = "calendar_exhausted"
    HEARTBEAT_ABSENT = "heartbeat_absent"
    RUNTIME_LOCK_UNKNOWN = "runtime_lock_unknown"
    GATEWAY_WATCH_NOT_REPORTING = "gateway_watch_not_reporting"
    GATEWAY_WATCH_STALE = "gateway_watch_stale"
    KILL_FILE_UNREADABLE = "kill_file_unreadable"
    PROPOSALS_DIR_UNREADABLE = "proposals_dir_unreadable"
    ACCEPTANCE_MISSING = "acceptance_missing"
    ACCEPTANCE_INVALID = "acceptance_invalid"
    ACCEPTANCE_CALENDAR_MISMATCH = "acceptance_calendar_mismatch"
    ACCEPTANCE_WINDOW_MISMATCH = "acceptance_window_mismatch"
    SCHEDULE_UNAVAILABLE = "schedule_unavailable"
    ACTIVITY_SOURCE_UNREADABLE = "activity_source_unreadable"
    ACTOR_NOT_RECORDED = "actor_not_recorded"
    # -- Today blockers ---------------------------------------------------------
    DESK_OWNER_STOPPED = "desk_owner_stopped"
    DESK_OWNER_STALE = "desk_owner_stale"
    GATEWAY_NEEDS_LOGIN = "gateway_needs_login"
    GATEWAY_NEEDS_2FA = "gateway_needs_2fa"
    GATEWAY_STARTING = "gateway_starting"
    GATEWAY_API_DOWN = "gateway_api_down"
    GATEWAY_DOWN = "gateway_down"
    EXIT_UNGUARDED = "exit_unguarded"
    CALENDAR_HORIZON_NEAR = "calendar_horizon_near"
    FAMILY_COMMANDS_DISABLED = "family_commands_disabled"


#: Any machine reason a Wave 3 view may show: a Wave 2 ``Blocker`` (including
#: the Wave 2 view reasons such as ``holdings_missing``) or a ``Wave3Reason``.
UnavailableReason = Blocker | Wave3Reason


@unique
class FamilyRefusal(StrEnum):
    """``CommandAuthorityRefused.detail`` values of the family verifier (the
    ``reason`` stays ``command_unauthorized``). Checked in this order."""

    FAMILY_INGRESS_DISABLED = "family_ingress_disabled"
    WRONG_PEER = "wrong_peer"
    ASSERTION_MISSING = "assertion_missing"
    ASSERTION_MALFORMED = "assertion_malformed"
    ASSERTION_BAD_SIGNATURE = "assertion_bad_signature"
    ASSERTION_CLAIMS_INVALID = "assertion_claims_invalid"
    ASSERTION_WRONG_AUDIENCE = "assertion_wrong_audience"
    ASSERTION_NOT_YET_VALID = "assertion_not_yet_valid"
    ASSERTION_EXPIRED = "assertion_expired"
    ASSERTION_METHOD_MISMATCH = "assertion_method_mismatch"
    ASSERTION_PATH_MISMATCH = "assertion_path_mismatch"
    ASSERTION_ORIGIN_NOT_ALLOWED = "assertion_origin_not_allowed"
    ASSERTION_ORIGIN_MISMATCH = "assertion_origin_mismatch"
    ASSERTION_HOST_MISMATCH = "assertion_host_mismatch"
    ASSERTION_BODY_MISMATCH = "assertion_body_mismatch"
    ASSERTION_REQUEST_ID_MISMATCH = "assertion_request_id_mismatch"
    ASSERTION_SUB_NOT_OWNER = "assertion_sub_not_owner"
    # applied by the authority lane after ``verify_family_command`` (I/O):
    ASSERTION_REPLAYED = "assertion_replayed"
    ASSERTION_LEDGER_UNAVAILABLE = "assertion_ledger_unavailable"


# =============================================================================
# C3.1 trex-family-command/1
# =============================================================================

FAMILY_COMMAND_SCHEMA: Final = "trex-family-command/1"
ASSERTION_SEEN_SCHEMA: Final = "trex-family-assertion-seen/1"
#: family-apps ``src/actor.mjs`` ``ACTOR_HEADER``; the portal strips any
#: client-supplied value before it signs its own.
FAMILY_ACTOR_HEADER: Final = "x-family-actor"
#: family-apps actor protocol 2 = request-bound (``signSpeechControl``).
FAMILY_COMMAND_PROTOCOL: Final = 2
FAMILY_COMMAND_AUDIENCE: Final = "trex-command"
FAMILY_STAGING_COMMAND_AUDIENCE: Final = "trex-staging-command"
#: ``exp`` is at most ``iat`` + this (family-apps mints exactly this).
FAMILY_COMMAND_TTL_S: Final = 60
#: ``iat`` may lead the verifier's clock by this much (same host).
FAMILY_COMMAND_MAX_SKEW_S: Final = 5
FAMILY_COMMAND_METHOD: Final = "POST"
#: The path trex_web sees (``RawCommandRequest.path``): the portal strips
#: exactly one app prefix (``/trex`` or ``/trex-staging``) and signs the
#: upstream pathname; the query is never part of it (and must be empty).
FAMILY_COMMAND_PATH: Final = "/api/commands"
FAMILY_INGRESS: Final = "family"
#: The pairing-key format family-apps already uses: 32 random bytes as
#: standard padded base64 (44 characters) in a 0600 regular file.
FAMILY_KEY_BYTES: Final = 32

#: Portal prefix -> (audience, key file default). Prefix strip: the portal
#: forwards ``<prefix>/api/commands`` as ``/api/commands``.
PORTAL_ROUTES: Final[Mapping[str, tuple[str, str]]] = {
    "/trex": (FAMILY_COMMAND_AUDIENCE, "~/.config/family-apps/trex-command.key"),
    "/trex-staging": (
        FAMILY_STAGING_COMMAND_AUDIENCE,
        "~/.config/family-apps/trex-staging-command.key",
    ),
}
#: The private family-apps config field naming the key file (by path only).
PORTAL_KEY_CONFIG_FIELD: Final = "trexCommandKeyPath"
PORTAL_KEY_FILE_DEFAULT: Final = "~/.config/family-apps/trex-command.key"
PORTAL_STAGING_KEY_FILE_DEFAULT: Final = "~/.config/family-apps/trex-staging-command.key"
#: The portal refuses a bigger command body (413); equals trex_web's limit.
PORTAL_COMMAND_MAX_BYTES: Final = 64 * 1024

#: TREX side: the same key bytes, its own file.
TREX_KEY_FILE_ENV: Final = "TREX_FAMILY_COMMAND_KEY_FILE"
FAMILY_KEY_FILE_DEFAULT: Final = "~/.config/trex/family-command.key"
TREX_OWNER_EMAILS_ENV: Final = "TREX_OWNER_EMAILS"
TREX_FAMILY_ORIGINS_ENV: Final = "TREX_FAMILY_ORIGINS"
TREX_FAMILY_AUDIENCE_ENV: Final = "TREX_FAMILY_AUDIENCE"

#: The claim order family-apps signs (``JSON.stringify`` keeps insertion
#: order): ``signSpeechControl``'s order with ``jti`` appended.
CLAIM_ORDER: Final = (
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
)

_ORIGIN_RE = re.compile(r"^https://[a-z0-9-]+(\.[a-z0-9-]+)*(:(?P<port>[1-9][0-9]{0,4}))?$")


def _exact_origin(value: str) -> str:
    match = _ORIGIN_RE.fullmatch(value)
    if match is None:
        raise ValueError("an exact https origin: scheme, lower-case host, optional port")
    port = match.group("port")
    if port is not None and (int(port) == 443 or int(port) > 65535):
        raise ValueError("the origin omits the default port and names a real port")
    return value


#: ``https://host[:port]``, exactly as a browser's ``Origin`` header spells it.
ExactOrigin = Annotated[str, AfterValidator(_exact_origin)]
_ORIGIN_ADAPTER: TypeAdapter[str] = TypeAdapter(ExactOrigin)
_EMAIL_ADAPTER: TypeAdapter[str] = TypeAdapter(OwnerEmail)

#: A single-use assertion id: 16 random bytes as lower-case hex.
Jti = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]
UnixSeconds = Annotated[int, Field(strict=True, ge=0)]


def _exact_int(value: Any) -> Any:
    if type(value) is not int:
        raise ValueError("expected a JSON integer")
    return value


class FamilyCommandClaims(StrictModel):
    """The claims of a ``trex-family-command/1`` assertion.

    ``request_id`` is the command body's ``idempotency_key``;
    ``request_sha256`` the sha256 of the exact body bytes the portal
    forwards. ``exp`` is after ``iat`` and at most ``iat + 60``.
    """

    v: Annotated[Literal[2], BeforeValidator(_exact_int)]
    sub: OwnerEmail
    aud: Literal["trex-command", "trex-staging-command"]
    iat: UnixSeconds
    exp: UnixSeconds
    method: Literal["POST"]
    path: Literal["/api/commands"]
    ingress: Literal["family"]
    origin: ExactOrigin
    request_id: IdempotencyKey
    request_sha256: Sha256Hex
    jti: Jti

    @model_validator(mode="after")
    def _lifetime(self) -> FamilyCommandClaims:
        if not self.iat < self.exp <= self.iat + FAMILY_COMMAND_TTL_S:
            raise ValueError(f"an assertion lives more than 0 and at most {FAMILY_COMMAND_TTL_S}s")
        return self


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


_B64URL_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _b64url_decode(text: str) -> bytes:
    """Strict unpadded base64url: a non-canonical spelling refuses."""
    if not _B64URL_RE.fullmatch(text) or len(text) % 4 == 1:
        raise ValueError("not base64url")
    raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    if _b64url(raw) != text:
        raise ValueError("non-canonical base64url")
    return raw


def _reject_constant(name: str) -> None:
    raise ValueError(f"non-finite number {name}")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            raise ValueError(f"duplicate key {key!r}")
        document[key] = value
    return document


def _strict_json(data: bytes) -> Any:
    return json.loads(
        data.decode("utf-8"), parse_constant=_reject_constant, object_pairs_hook=_unique_object
    )


def _claims_bytes(claims: FamilyCommandClaims) -> bytes:
    """``JSON.stringify(claims)`` in ``CLAIM_ORDER``: compact, UTF-8."""
    document = claims.model_dump(mode="json")
    ordered = {name: document[name] for name in CLAIM_ORDER}
    return json.dumps(ordered, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _signature(key: bytes, payload: str) -> str:
    return _b64url(hmac.new(key, payload.encode("ascii"), hashlib.sha256).digest())


def sign_family_command(key: bytes, claims: FamilyCommandClaims) -> str:
    """``<base64url(claims JSON)>.<base64url(HMAC-SHA256(key, payload ASCII))>``."""
    payload = _b64url(_claims_bytes(claims))
    return f"{payload}.{_signature(key, payload)}"


def mint_family_command(
    key: bytes, *, sub: str, aud: str, origin: str, body: bytes, iat: int, jti: str
) -> str:
    """What the portal's ``signTrexCommand`` produces for ``body`` (tests, fakes).

    ``sub`` is trimmed and lower-cased as family-apps does; ``request_id`` is
    read from the body (a JSON object with a string ``idempotency_key``)."""
    request = _strict_json(body)
    if not isinstance(request, dict) or not isinstance(request.get("idempotency_key"), str):
        raise ValueError("a command body is a JSON object with an idempotency_key")
    claims = FamilyCommandClaims.model_validate(
        {
            "v": FAMILY_COMMAND_PROTOCOL,
            "sub": sub.strip().lower(),
            "aud": aud,
            "iat": iat,
            "exp": iat + FAMILY_COMMAND_TTL_S,
            "method": FAMILY_COMMAND_METHOD,
            "path": FAMILY_COMMAND_PATH,
            "ingress": FAMILY_INGRESS,
            "origin": origin,
            "request_id": request["idempotency_key"],
            "request_sha256": sha256_hex(body),
            "jti": jti,
        }
    )
    return sign_family_command(key, claims)


def parse_command_key(text: str) -> bytes:
    """The HMAC key from a key file's text: ASCII whitespace trimmed, then
    exactly 32 bytes of standard padded base64 (family-apps'
    ``parsePairingKey`` format). Never log the text or the result."""
    trimmed = text.strip(" \t\n\v\f\r")
    if not re.fullmatch(r"[A-Za-z0-9+/]{43}=", trimmed):
        raise ValueError("the command key must be 32 bytes of standard base64")
    try:
        key = base64.b64decode(trimmed, validate=True)
    except binascii.Error as error:
        raise ValueError("the command key must be 32 bytes of standard base64") from error
    if len(key) != FAMILY_KEY_BYTES:
        raise ValueError("the command key must be 32 bytes of standard base64")
    return key


def parse_owner_emails(text: str) -> frozenset[str]:
    """``TREX_OWNER_EMAILS``: comma-separated, trimmed, lower-cased; empty
    entries ignored; an entry that is not an email refuses."""
    out = set()
    for entry in text.split(","):
        email = entry.strip().lower()
        if email:
            out.add(_EMAIL_ADAPTER.validate_python(email))
    return frozenset(out)


def parse_origins(text: str) -> frozenset[str]:
    """``TREX_FAMILY_ORIGINS``: comma-separated exact https origins."""
    out = set()
    for entry in text.split(","):
        origin = entry.strip()
        if origin:
            out.add(_ORIGIN_ADAPTER.validate_python(origin))
    return frozenset(out)


@dataclass(frozen=True)
class FamilyAuthorityConfig:
    """What the family verifier trusts. Enabled only when all three are set."""

    key: bytes = field(repr=False)
    owner_emails: frozenset[str]
    origins: frozenset[str]
    audience: str = FAMILY_COMMAND_AUDIENCE

    @property
    def enabled(self) -> bool:
        return len(self.key) == FAMILY_KEY_BYTES and bool(self.owner_emails) and bool(self.origins)


def family_config_from_env(env: Mapping[str, str], key: bytes) -> FamilyAuthorityConfig:
    """The config from the environment (the key is loaded by the caller from
    ``TREX_FAMILY_COMMAND_KEY_FILE``; an absent key is ``b""``: disabled)."""
    audience = env.get(TREX_FAMILY_AUDIENCE_ENV, "").strip() or FAMILY_COMMAND_AUDIENCE
    if audience not in (FAMILY_COMMAND_AUDIENCE, FAMILY_STAGING_COMMAND_AUDIENCE):
        raise ValueError(f"{TREX_FAMILY_AUDIENCE_ENV} names an unknown audience")
    return FamilyAuthorityConfig(
        key=key,
        owner_emails=parse_owner_emails(env.get(TREX_OWNER_EMAILS_ENV, "")),
        origins=parse_origins(env.get(TREX_FAMILY_ORIGINS_ENV, "")),
        audience=audience,
    )


def authority_for(raw_request: RawCommandRequest) -> Literal["family", "loopback"]:
    """Dispatch rule: a request carrying ``x-family-actor`` is judged by the
    family verifier ONLY (no fallback); every other request by the Wave 2
    ``LoopbackCommandAuthority``, which refuses forwarded headers, so a
    forwarded identity without a valid assertion is always refused."""
    return "family" if FAMILY_ACTOR_HEADER in raw_request.headers else "loopback"


def _refused(detail: FamilyRefusal) -> CommandAuthorityRefused:
    return CommandAuthorityRefused(reason=Blocker.COMMAND_UNAUTHORIZED, detail=detail.value)


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def decode_family_command(token: str, key: bytes) -> FamilyCommandClaims:
    """Signature first, then strict claims; raises ``CommandAuthorityRefused``."""
    parts = token.split(".")
    if len(parts) != 2 or not all(_B64URL_RE.fullmatch(p) for p in parts):
        raise _refused(FamilyRefusal.ASSERTION_MALFORMED)
    payload, signature = parts
    if not hmac.compare_digest(_signature(key, payload), signature):
        raise _refused(FamilyRefusal.ASSERTION_BAD_SIGNATURE)
    try:
        return FamilyCommandClaims.model_validate(_strict_json(_b64url_decode(payload)))
    except (ValueError, ValidationError) as error:
        raise _refused(FamilyRefusal.ASSERTION_CLAIMS_INVALID) from error


def verify_family_command(
    raw_request: RawCommandRequest, *, config: FamilyAuthorityConfig, now: datetime
) -> tuple[Actor, FamilyCommandClaims]:
    """The pure part of the family ``CommandAuthority``.

    Returns the ``Actor`` (bound to ``raw_request`` exactly as the Wave 2
    backend checks: ``request_id`` is the BACKEND's id, ``request_sha256``
    the body's) and the claims, whose ``jti`` the authority lane must then
    record durably (``assertion_replayed`` on a second use) before it
    returns the actor. Refusals: ``FamilyRefusal`` details, in enum order.
    """
    if not config.enabled:
        raise _refused(FamilyRefusal.FAMILY_INGRESS_DISABLED)
    if not _is_loopback(raw_request.peer_host):
        raise _refused(FamilyRefusal.WRONG_PEER)
    token = raw_request.headers.get(FAMILY_ACTOR_HEADER)
    if token is None:
        raise _refused(FamilyRefusal.ASSERTION_MISSING)
    claims = decode_family_command(token, config.key)
    if claims.aud != config.audience:
        raise _refused(FamilyRefusal.ASSERTION_WRONG_AUDIENCE)
    now_s = int(now.timestamp())
    if now_s < claims.iat - FAMILY_COMMAND_MAX_SKEW_S:
        raise _refused(FamilyRefusal.ASSERTION_NOT_YET_VALID)
    if now_s >= claims.exp:
        raise _refused(FamilyRefusal.ASSERTION_EXPIRED)
    if raw_request.method != claims.method:
        raise _refused(FamilyRefusal.ASSERTION_METHOD_MISMATCH)
    if raw_request.path != claims.path:
        raise _refused(FamilyRefusal.ASSERTION_PATH_MISMATCH)
    if claims.origin not in config.origins:
        raise _refused(FamilyRefusal.ASSERTION_ORIGIN_NOT_ALLOWED)
    if raw_request.headers.get("origin") != claims.origin:
        raise _refused(FamilyRefusal.ASSERTION_ORIGIN_MISMATCH)
    if raw_request.headers.get("host") != claims.origin.removeprefix("https://"):
        raise _refused(FamilyRefusal.ASSERTION_HOST_MISMATCH)
    body_sha256 = sha256_hex(raw_request.body)
    if body_sha256 != claims.request_sha256:
        raise _refused(FamilyRefusal.ASSERTION_BODY_MISMATCH)
    try:
        body = _strict_json(raw_request.body)
    except ValueError:
        body = None
    if not isinstance(body, dict) or body.get("idempotency_key") != claims.request_id:
        raise _refused(FamilyRefusal.ASSERTION_REQUEST_ID_MISMATCH)
    if claims.sub not in config.owner_emails:
        raise _refused(FamilyRefusal.ASSERTION_SUB_NOT_OWNER)
    actor = Actor(
        sub=claims.sub,
        ingress="family",
        request_id=raw_request.request_id,
        request_sha256=body_sha256,
    )
    return actor, claims


class AssertionSeen(StrictModel):
    """The replay ledger entry ``<run dir>/authority/assertions/<jti>.json``,
    created with O_CREAT|O_EXCL before the actor is returned (an existing
    file: ``assertion_replayed``; any other failure:
    ``assertion_ledger_unavailable``). Prunable once ``exp`` +
    ``FAMILY_COMMAND_MAX_SKEW_S`` has passed."""

    schema_version: Literal["trex-family-assertion-seen/1"] = Field(
        default="trex-family-assertion-seen/1", alias="schema"
    )
    jti: Jti
    sub: OwnerEmail
    aud: Literal["trex-command", "trex-staging-command"]
    request_id: IdempotencyKey
    request_sha256: Sha256Hex
    exp: UnixSeconds
    backend_request_id: IdStr
    seen_at: UTCInstant


def assertion_seen_record(
    claims: FamilyCommandClaims, *, backend_request_id: str, seen_at: datetime
) -> AssertionSeen:
    return AssertionSeen(
        jti=claims.jti,
        sub=claims.sub,
        aud=claims.aud,
        request_id=claims.request_id,
        request_sha256=claims.request_sha256,
        exp=claims.exp,
        backend_request_id=backend_request_id,
        seen_at=seen_at,
    )


# =============================================================================
# C3.3 API paths and control labels (routes are web/src/lib/routes.ts)
# =============================================================================

TODAY_PATH: Final = "/api/today"
ACTIVITY_PATH: Final = "/api/activity"
ACCEPTANCE_PATH: Final = "/api/acceptance"
#: Wave 2, referenced unchanged.
COMMANDS_PATH: Final = "/api/commands"
PROPOSALS_PATH: Final = "/api/proposals"
PORTFOLIO_PATH: Final = "/api/portfolio"

#: The entry-only pause is THE primary control on every page.
PRIMARY_CONTROL: Final = "pause_entries"
#: Owner-facing button labels per command kind (``halt``'s is the plan's).
CONTROL_LABELS: Final[Mapping[str, str]] = {
    "pause_entries": "Pause new entries",
    "resume_entries": "Resume new entries",
    "halt": "Emergency stop (also stops exits)",
    "flatten": "Close all desk positions",
    "resume_all": "Clear emergency stop, close-all and pause",
}

# =============================================================================
# C3.2 read views
# =============================================================================

TODAY_SCHEMA: Final = "trex-today/1"
ACTIVITY_SCHEMA: Final = "trex-activity/1"
#: The actor of desk-owner events in the activity feed.
DESK_SYSTEM_SUB: Final = "trex-desk"

NonNegativeInt = Annotated[int, Field(strict=True, ge=0)]
PositiveInt = Annotated[int, Field(strict=True, ge=1)]
Cents = Annotated[int, Field(strict=True)]
HumanText = Annotated[str, Field(min_length=1, max_length=500)]


def _block(
    status: str, reason: object, figures: Mapping[str, object], unavailable: str = "unavailable"
) -> None:
    """The null rule: unavailable iff a reason; an unavailable block carries
    no figure; a known block carries every figure (unknown needs a reason)."""
    if (status == unavailable) != (reason is not None):
        raise ValueError("unavailable_reason is set exactly when the status is unavailable")
    if status == unavailable:
        filled = sorted(name for name, value in figures.items() if value is not None)
        if filled:
            raise ValueError(f"an unavailable block carries no figures: {filled}")
    else:
        missing = sorted(name for name, value in figures.items() if value is None)
        if missing:
            raise ValueError(f"a known block has no null figure: {missing}")


class SessionState(StrictModel):
    """Where today sits in the NYSE calendar. ``session`` is today's session
    (``pre_open``/``open``) or the next one (``after_close``/``no_session``)."""

    status: Literal["pre_open", "open", "after_close", "no_session", "unavailable"]
    unavailable_reason: UnavailableReason | None
    et_date: date | None
    session: date | None
    opens_at: UTCInstant | None
    closes_at: UTCInstant | None
    early_close: bool | None
    calendar_sessions_left: NonNegativeInt | None

    @model_validator(mode="after")
    def _shape(self) -> SessionState:
        _block(
            self.status,
            self.unavailable_reason,
            {
                "et_date": self.et_date,
                "session": self.session,
                "opens_at": self.opens_at,
                "closes_at": self.closes_at,
                "early_close": self.early_close,
                "calendar_sessions_left": self.calendar_sessions_left,
            },
        )
        if self.status == "unavailable":
            return self
        assert self.session is not None and self.et_date is not None
        assert self.opens_at is not None and self.closes_at is not None
        if self.opens_at >= self.closes_at:
            raise ValueError("a session opens before it closes")
        today = self.status in ("pre_open", "open")
        if today != (self.session == self.et_date) or self.session < self.et_date:
            raise ValueError("session is today's (pre_open/open) or a later one")
        return self


class DeskOwnerHealth(StrictModel):
    """The desk owner process. ``healthy``: lock held and heartbeat fresh
    (age <= ``stale_after_s``, the exit watch's ``HEARTBEAT_STALE_S``);
    ``stale``: lock held, heartbeat old; ``stopped``: lock not held."""

    status: Literal["healthy", "stale", "stopped", "unavailable"]
    unavailable_reason: UnavailableReason | None
    heartbeat_at: UTCInstant | None
    heartbeat_age_s: NonNegativeInt | None
    lock_held: bool | None
    exit_owner_ready: bool | None
    stale_after_s: PositiveInt

    @model_validator(mode="after")
    def _shape(self) -> DeskOwnerHealth:
        _block(
            self.status,
            self.unavailable_reason,
            {
                "heartbeat_at": self.heartbeat_at,
                "heartbeat_age_s": self.heartbeat_age_s,
                "lock_held": self.lock_held,
                "exit_owner_ready": self.exit_owner_ready,
            },
        )
        if self.status == "unavailable":
            return self
        assert self.heartbeat_age_s is not None
        fresh = self.heartbeat_age_s <= self.stale_after_s
        expected = "stopped" if not self.lock_held else "healthy" if fresh else "stale"
        if self.status != expected:
            raise ValueError(f"lock and heartbeat age say {expected}, not {self.status}")
        if self.exit_owner_ready != (self.status == "healthy"):
            raise ValueError("the exit owner is ready exactly when the desk owner is healthy")
        return self


class GatewayHealth(StrictModel):
    """The IB Gateway watch verdict (``gateway.json``); the statuses are the
    watch's own (``trex.gateway_watch.classify``)."""

    status: Literal[
        "ok", "starting", "checking", "needs_login", "needs_2fa", "api_down", "down", "unavailable"
    ]
    unavailable_reason: UnavailableReason | None
    detail: str | None
    checked_at: UTCInstant | None
    age_s: NonNegativeInt | None

    @model_validator(mode="after")
    def _shape(self) -> GatewayHealth:
        _block(
            self.status,
            self.unavailable_reason,
            {"detail": self.detail, "checked_at": self.checked_at, "age_s": self.age_s},
        )
        return self


class KillSwitchState(StrictModel):
    """One kill file. ``since`` (file time) and ``reason`` (as recorded; may
    be empty) are set exactly when ``standing``."""

    name: Literal["PAUSE_ENTRIES", "HALT", "FLATTEN"]
    status: Literal["clear", "standing", "unavailable"]
    unavailable_reason: UnavailableReason | None
    since: UTCInstant | None
    reason: Annotated[str, Field(max_length=500)] | None

    @model_validator(mode="after")
    def _shape(self) -> KillSwitchState:
        if (self.status == "unavailable") != (self.unavailable_reason is not None):
            raise ValueError("unavailable_reason is set exactly when the status is unavailable")
        standing = self.status == "standing"
        if standing != (self.since is not None) or standing != (self.reason is not None):
            raise ValueError("since and reason are set exactly when the file stands")
        return self


class QueuedCommand(StrictModel):
    """A kill-switch command accepted but not yet applied by the owner."""

    idempotency_key: IdempotencyKey
    kind: CommandKind
    issued_at: UTCInstant
    expires_at: UTCInstant
    issued_by_sub: IdStr


class QueuedCommands(StrictModel):
    status: Literal["ok", "unavailable"]
    unavailable_reason: UnavailableReason | None
    items: tuple[QueuedCommand, ...] | None

    @model_validator(mode="after")
    def _shape(self) -> QueuedCommands:
        _block(self.status, self.unavailable_reason, {"items": self.items})
        return self


class Controls(StrictModel):
    """Kill files as they stand plus queued control commands. The page's
    primary control is ``pause_entries``; ``halt`` is shown as
    ``CONTROL_LABELS["halt"]``."""

    pause_entries: KillSwitchState
    halt: KillSwitchState
    flatten: KillSwitchState
    queued: QueuedCommands

    @model_validator(mode="after")
    def _names(self) -> Controls:
        names = (self.pause_entries.name, self.halt.name, self.flatten.name)
        if names != ("PAUSE_ENTRIES", "HALT", "FLATTEN"):
            raise ValueError("each control reports its own file")
        return self


class PendingApproval(StrictModel):
    """One ``pending`` proposal, with what the approve command must repeat.

    Countdown: ``seconds_to_deadline`` = ``send_deadline - now`` at the
    response's ``now`` (whole seconds, >= 0); a client counts down from the
    moment it received the response, never from its own clock against
    ``send_deadline``. ``precheck_blockers`` is the web's pre-check
    (the desk is authoritative)."""

    proposal_id: Sha256Hex
    proposal_sha256: Sha256Hex
    asset_kind: AssetKind
    intent_id: PathSafeId
    program_id: IdStr
    summary: HumanText
    proposed_at: UTCInstant
    send_deadline: UTCInstant
    seconds_to_deadline: NonNegativeInt
    max_loss_cents: PositiveInt
    notional_cents: PositiveInt
    approve_args: ApproveEntryArgs
    precheck_blockers: tuple[UnavailableReason, ...]

    @model_validator(mode="after")
    def _bound(self) -> PendingApproval:
        args = self.approve_args
        if (args.proposal_id, args.proposal_sha256) != (self.proposal_id, self.proposal_sha256):
            raise ValueError("approve_args repeat this proposal's id and file hash")
        if self.send_deadline <= self.proposed_at:
            raise ValueError("a proposal's deadline follows its composition")
        return self


class PendingApprovals(StrictModel):
    status: Literal["ok", "unavailable"]
    unavailable_reason: UnavailableReason | None
    items: tuple[PendingApproval, ...] | None
    problem_count: NonNegativeInt | None

    @model_validator(mode="after")
    def _shape(self) -> PendingApprovals:
        _block(
            self.status,
            self.unavailable_reason,
            {"items": self.items, "problem_count": self.problem_count},
        )
        return self


class HoldingsSummary(StrictModel):
    """Desk positions from ``trex-holdings/1``: distinct attributed intents
    per asset kind (``STK`` equity, ``OPT`` option)."""

    status: Literal["ok", "unavailable"]
    unavailable_reason: UnavailableReason | None
    as_of: UTCInstant | None
    age_s: NonNegativeInt | None
    option_positions: NonNegativeInt | None
    equity_positions: NonNegativeInt | None

    @model_validator(mode="after")
    def _shape(self) -> HoldingsSummary:
        _block(
            self.status,
            self.unavailable_reason,
            {
                "as_of": self.as_of,
                "age_s": self.age_s,
                "option_positions": self.option_positions,
                "equity_positions": self.equity_positions,
            },
        )
        return self


class RiskSummary(StrictModel):
    """Figures of ``trex-risk-snapshot/1``. ``stale`` keeps its figures (and
    Today lists ``risk_snapshot_stale``); ``unavailable`` has none."""

    status: Literal["fresh", "stale", "unavailable"]
    unavailable_reason: UnavailableReason | None
    as_of: UTCInstant | None
    age_s: NonNegativeInt | None
    open_loss_cents: NonNegativeInt | None
    day_realized_loss_cents: NonNegativeInt | None
    settled_cash_cents: Cents | None
    outside_positions: NonNegativeInt | None

    @model_validator(mode="after")
    def _shape(self) -> RiskSummary:
        _block(
            self.status,
            self.unavailable_reason,
            {
                "as_of": self.as_of,
                "age_s": self.age_s,
                "open_loss_cents": self.open_loss_cents,
                "day_realized_loss_cents": self.day_realized_loss_cents,
                "settled_cash_cents": self.settled_cash_cents,
                "outside_positions": self.outside_positions,
            },
        )
        return self


class PositionsSummary(StrictModel):
    """Today's summary; the detail is ``GET /api/portfolio`` (Wave 2)."""

    holdings: HoldingsSummary
    risk: RiskSummary


class AcceptanceSummary(StrictModel):
    """Today's line of ``trex-acceptance/1`` (``GET /api/acceptance``)."""

    status: Literal["ok", "unavailable"]
    unavailable_reason: UnavailableReason | None
    computed_at: UTCInstant | None
    verdict: Literal["met", "not_met"] | None
    streak_sessions: NonNegativeInt | None
    option_lifecycles: NonNegativeInt | None
    equity_lifecycles: NonNegativeInt | None
    drills_passed: NonNegativeInt | None
    streak_required: TenLiteral
    lifecycles_required: TenLiteral
    drills_required: SevenLiteral
    live_authorized: FalseLiteral

    @model_validator(mode="after")
    def _shape(self) -> AcceptanceSummary:
        _block(
            self.status,
            self.unavailable_reason,
            {
                "computed_at": self.computed_at,
                "verdict": self.verdict,
                "streak_sessions": self.streak_sessions,
                "option_lifecycles": self.option_lifecycles,
                "equity_lifecycles": self.equity_lifecycles,
                "drills_passed": self.drills_passed,
            },
        )
        return self

    @classmethod
    def of(cls, document: AcceptanceDocument) -> AcceptanceSummary:
        """The summary of a (verified) document."""
        return cls(
            status="ok",
            unavailable_reason=None,
            computed_at=document.computed_at,
            verdict=document.verdict,
            streak_sessions=document.streak.current_sessions,
            option_lifecycles=document.per_kind.option.counted,
            equity_lifecycles=document.per_kind.equity.counted,
            drills_passed=document.drills_passed,
            streak_required=10,
            lifecycles_required=10,
            drills_required=7,
            live_authorized=False,
        )


ScheduledKind = Literal[
    "session_open",
    "session_close",
    "entry_window_open",
    "entry_window_close",
    "forced_exit",
    "exit_deadline",
    "approval_deadline",
    "premarket_run",
    "backup",
    "calendar_horizon",
]
#: Kinds about one position or proposal: ``subject`` names it (intent id, or
#: proposal id for ``approval_deadline``); every other kind has no subject.
SUBJECT_KINDS: Final = frozenset({"forced_exit", "exit_deadline", "approval_deadline"})


class ScheduledEvent(StrictModel):
    at: UTCInstant
    kind: ScheduledKind
    label: HumanText
    subject: IdStr | None

    @model_validator(mode="after")
    def _subject(self) -> ScheduledEvent:
        if (self.kind in SUBJECT_KINDS) != (self.subject is not None):
            raise ValueError("subject is set exactly for per-position and per-proposal kinds")
        return self


class NextEvents(StrictModel):
    status: Literal["ok", "unavailable"]
    unavailable_reason: UnavailableReason | None
    items: tuple[ScheduledEvent, ...] | None

    @model_validator(mode="after")
    def _shape(self) -> NextEvents:
        _block(self.status, self.unavailable_reason, {"items": self.items})
        if self.items is not None and [e.at for e in self.items] != sorted(
            e.at for e in self.items
        ):
            raise ValueError("next events are in time order")
        return self


class HumanBlocker(StrictModel):
    """A blocker the owner reads: a machine ``code`` plus plain words."""

    code: UnavailableReason
    scope: Literal["entries", "exits", "approvals", "commands", "display"]
    severity: Literal["info", "warning", "critical"]
    message: HumanText
    next_step: Annotated[str, Field(max_length=500)]


class TodayView(StrictModel):
    """``GET /api/today`` (``trex-today/1``): one page, read-only."""

    schema_version: Literal["trex-today/1"] = Field(default="trex-today/1", alias="schema")
    now: UTCInstant
    session: SessionState
    desk_owner: DeskOwnerHealth
    gateway: GatewayHealth
    controls: Controls
    approvals: PendingApprovals
    positions: PositionsSummary
    acceptance: AcceptanceSummary
    next_events: NextEvents
    blockers: tuple[HumanBlocker, ...]
    execution_enabled: FalseLiteral


ActivitySource = Literal["command", "desk_event", "audit", "drill"]
ACTIVITY_SOURCES: Final[tuple[ActivitySource, ...]] = ("command", "desk_event", "audit", "drill")
CommandStatus = Literal["accepted", "refused", "applied", "failed", "duplicate"]
#: An opaque paging cursor (the producer's encoding; clients never parse it).
Cursor = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{1,200}$")]
ActivityRefKey = Literal["proposal_id", "intent_id", "program_id", "drill_id", "path"]


class ActivityActor(StrictModel):
    """Commands: the stamped ``issued_by`` (``sub``, ``ingress``); desk
    events: ``{sub: "trex-desk", ingress: "system"}``."""

    sub: IdStr
    ingress: Literal["family", "loopback", "system"]


class ActivityItem(StrictModel):
    """One line of the activity feed.

    ``actor`` xor ``actor_unavailable_reason``; ``command_key`` and
    ``command_status`` are set exactly for ``source == "command"``;
    ``reason`` exactly when the command status is refused, failed or
    duplicate."""

    id: Annotated[
        str, StringConstraints(pattern=r"^(command|desk_event|audit|drill):[A-Za-z0-9._:-]{1,200}$")
    ]
    at: UTCInstant
    source: ActivitySource
    kind: IdStr
    summary: HumanText
    actor: ActivityActor | None
    actor_unavailable_reason: ReasonCode | None
    command_key: IdempotencyKey | None
    command_status: CommandStatus | None
    reason: IdStr | None
    refs: dict[ActivityRefKey, IdStr]

    @model_validator(mode="after")
    def _shape(self) -> ActivityItem:
        if not self.id.startswith(f"{self.source}:"):
            raise ValueError("the id is prefixed with its source")
        if (self.actor is None) == (self.actor_unavailable_reason is None):
            raise ValueError("an item has an actor or says why it has none")
        is_command = self.source == "command"
        if is_command != (self.command_key is not None) or is_command != (
            self.command_status is not None
        ):
            raise ValueError("command_key and command_status are set exactly for commands")
        needs_reason = self.command_status in ("refused", "failed", "duplicate")
        if needs_reason != (self.reason is not None):
            raise ValueError("reason is set exactly for refused, failed and duplicate commands")
        return self


class ActivitySourceStatus(StrictModel):
    source: ActivitySource
    status: Literal["ok", "unavailable"]
    unavailable_reason: UnavailableReason | None

    @model_validator(mode="after")
    def _shape(self) -> ActivitySourceStatus:
        _block(self.status, self.unavailable_reason, {})
        return self


class ActivityQuery(StrictModel):
    """``GET /api/activity?limit=&cursor=&source=`` (``source`` repeatable;
    none = every source)."""

    limit: Annotated[int, Field(ge=1, le=200)] = 50
    cursor: Cursor | None = None
    sources: tuple[ActivitySource, ...] = ()


class ActivityPage(StrictModel):
    """``GET /api/activity`` (``trex-activity/1``): newest first. A source
    that cannot be read is listed ``unavailable``, never silently empty."""

    schema_version: Literal["trex-activity/1"] = Field(default="trex-activity/1", alias="schema")
    now: UTCInstant
    limit: Annotated[int, Field(strict=True, ge=1, le=200)]
    items: tuple[ActivityItem, ...]
    has_more: bool
    next_cursor: Cursor | None
    sources: tuple[ActivitySourceStatus, ...]
    execution_enabled: FalseLiteral

    @model_validator(mode="after")
    def _shape(self) -> ActivityPage:
        if len(self.items) > self.limit:
            raise ValueError("a page holds at most limit items")
        ats = [item.at for item in self.items]
        if ats != sorted(ats, reverse=True):
            raise ValueError("items are newest first")
        if len({item.id for item in self.items}) != len(self.items):
            raise ValueError("item ids are unique")
        if self.has_more != (self.next_cursor is not None):
            raise ValueError("next_cursor is set exactly when there is more")
        if tuple(s.source for s in self.sources) != ACTIVITY_SOURCES:
            raise ValueError("every source reports its status, in ACTIVITY_SOURCES order")
        return self


# =============================================================================
# C3.5 trex-pilot-registration/1
# =============================================================================

PILOT_REGISTRATION_SCHEMA: Final = "trex-pilot-registration/1"
PILOT_USAGE_SCHEMA: Final = "trex-pilot-usage/1"
PilotRole = Literal["observation", "baseline", "challenger"]
#: The pilot runs exactly one program per role.
PILOT_ROLES: Final[tuple[PilotRole, ...]] = ("observation", "baseline", "challenger")
#: Per-program daily ceilings a registration may not exceed (defaults until
#: the owner rules). ``storage_mb`` counts MiB (1,048,576 bytes).
PILOT_BUDGET_CEILINGS: Final[Mapping[str, int]] = {
    "requests_per_day": 5_000,
    "compute_minutes_per_day": 120,
    "storage_mb": 2_048,
}
#: Fields excluded from ``registration_body_sha256`` (the seal itself).
_SEAL_FIELDS: Final = ("status", "sealed_by", "sealed_at", "sha256")
LongText = Annotated[str, Field(min_length=1, max_length=4000)]


class PilotUniverse(StrictModel):
    asset_kind: AssetKind
    symbols: tuple[Symbol, ...]

    @model_validator(mode="after")
    def _symbols(self) -> PilotUniverse:
        if not self.symbols or list(self.symbols) != sorted(set(self.symbols)):
            raise ValueError("symbols: non-empty, sorted, no repeats")
        return self


class DataSource(StrictModel):
    """One input with its provenance (a URL, a repo path, or a receipt id)."""

    name: PathSafeId
    provider: IdStr
    dataset: IdStr
    provenance: Annotated[str, Field(min_length=1, max_length=500)]
    license: IdStr
    realtime: bool


class DecisionRule(StrictModel):
    """What decides the hypothesis, fixed before any forward data."""

    statement: LongText
    metric: IdStr
    minimum_sessions: PositiveInt
    minimum_lifecycles: NonNegativeInt


class PilotControlRef(StrictModel):
    program_id: PathSafeId
    relation: Literal["baseline", "observation"]


class PilotBudgets(StrictModel):
    requests_per_day: NonNegativeInt
    compute_minutes_per_day: NonNegativeInt
    storage_mb: NonNegativeInt

    @model_validator(mode="after")
    def _ceilings(self) -> PilotBudgets:
        over = [
            name for name, ceiling in PILOT_BUDGET_CEILINGS.items() if getattr(self, name) > ceiling
        ]
        if over:
            raise ValueError(f"budgets exceed the pilot ceilings: {over}")
        return self


class PilotRegistration(StrictModel):
    """A pilot program's preregistration.

    ``draft``: editable, cannot run. ``sealed``: written once by an owner act
    (``sealed_by`` an owner email, ``sealed_at``, ``sha256`` =
    ``registration_body_sha256``); never edited again, a change is a new
    ``program_id``. ``existing_registration`` is required for the
    ``observation`` role (it cites the existing sealed QSL registration
    instead of re-registering it) and forbidden otherwise.
    """

    schema_version: Literal["trex-pilot-registration/1"] = Field(
        default="trex-pilot-registration/1", alias="schema"
    )
    program_id: PathSafeId
    role: PilotRole
    title: Annotated[str, Field(min_length=1, max_length=200)]
    hypothesis: LongText
    universe: PilotUniverse
    data_sources: tuple[DataSource, ...]
    decision_rule: DecisionRule
    controls: tuple[PilotControlRef, ...]
    budgets: PilotBudgets
    existing_registration: EvidenceRef | None
    created_at: UTCInstant
    status: Literal["draft", "sealed"]
    sealed_by: OwnerEmail | None
    sealed_at: UTCInstant | None
    sha256: Sha256Hex | None

    @model_validator(mode="after")
    def _shape(self) -> PilotRegistration:
        if not self.data_sources:
            raise ValueError("a registration names its data sources")
        names = [s.name for s in self.data_sources]
        if len(names) != len(set(names)):
            raise ValueError("data source names are unique")
        controls = [c.program_id for c in self.controls]
        if len(controls) != len(set(controls)) or self.program_id in controls:
            raise ValueError("controls are other programs, each once")
        if self.role == "challenger" and not any(c.relation == "baseline" for c in self.controls):
            raise ValueError("a challenger names its baseline control")
        if (self.role == "observation") != (self.existing_registration is not None):
            raise ValueError("existing_registration is set exactly for the observation role")
        sealed = self.status == "sealed"
        if any((getattr(self, name) is not None) != sealed for name in _SEAL_FIELDS[1:]):
            raise ValueError("sealed_by, sealed_at and sha256 are set exactly when sealed")
        if sealed:
            assert self.sealed_at is not None
            if self.sealed_at < self.created_at:
                raise ValueError("a registration is sealed after it was created")
            if self.sha256 != registration_body_sha256(self):
                raise ValueError("sha256 is the hash of the registration body")
        return self


def registration_body_sha256(registration: PilotRegistration) -> str:
    """sha256 of the canonical bytes of the registration WITHOUT its seal
    fields (``status``, ``sealed_by``, ``sealed_at``, ``sha256``)."""
    document = model_document(registration)
    for name in _SEAL_FIELDS:
        document.pop(name)
    return sha256_hex(canonical_bytes(document))


def seal_registration(
    draft: PilotRegistration, *, sealed_by: str, owners: Iterable[str], at: datetime
) -> PilotRegistration:
    """The mechanical half of the owner's seal: refuses a sealed input and a
    sealer who is not an owner; returns the sealed document to write once."""
    if draft.status != "draft":
        raise ValueError("registration_already_sealed")
    if sealed_by not in frozenset(owners):
        raise ValueError("sealer_not_owner")
    document = model_document(draft)
    document.update(
        status="sealed",
        sealed_by=sealed_by,
        sealed_at=at,
        sha256=registration_body_sha256(draft),
    )
    return PilotRegistration.model_validate(document)


class SealRegistrationArgs(StrictModel):
    """Args of the ``seal_registration`` command kind the pilot lane adds to
    ``trex-command/1`` (additive member; WAVE3-CONTRACTS.md C3.5)."""

    program_id: PathSafeId
    registration_sha256: Sha256Hex


class BudgetUsage(StrictModel):
    """One program's usage over one ET day (``usage/<program_id>/<day>.json``)."""

    schema_version: Literal["trex-pilot-usage/1"] = Field(
        default="trex-pilot-usage/1", alias="schema"
    )
    program_id: PathSafeId
    day: date
    requests: NonNegativeInt
    compute_seconds: NonNegativeInt
    storage_bytes: NonNegativeInt
    as_of: UTCInstant


def registration_run_blockers(
    registration: PilotRegistration, *, usage: BudgetUsage | None, owners: Iterable[str]
) -> tuple[str, ...]:
    """Why the program may not do more work now (empty = it may).

    A draft never runs; unknown usage never reads as zero; a budget is
    exhausted when the next unit would exceed it (``used >= budget``)."""
    out: list[str] = []
    if registration.status != "sealed":
        out.append("registration_not_sealed")
    elif registration.sealed_by not in frozenset(owners):
        out.append("registration_sealer_not_owner")
    if usage is None:
        out.append("budget_usage_unknown")
        return tuple(out)
    if usage.program_id != registration.program_id:
        out.append("budget_usage_mismatch")
        return tuple(out)
    budgets = registration.budgets
    if usage.requests >= budgets.requests_per_day:
        out.append("budget_requests_exhausted")
    if usage.compute_seconds >= budgets.compute_minutes_per_day * 60:
        out.append("budget_compute_exhausted")
    if usage.storage_bytes >= budgets.storage_mb * 1024 * 1024:
        out.append("budget_storage_exhausted")
    return tuple(out)


def pilot_roster_problems(registrations: Iterable[PilotRegistration]) -> tuple[str, ...]:
    """The pilot is exactly one program per role; controls name programs in it."""
    rows = list(registrations)
    out: list[str] = []
    for role in PILOT_ROLES:
        count = sum(r.role == role for r in rows)
        if count != 1:
            out.append(f"pilot_role_count:{role}")
    ids = {r.program_id for r in rows}
    if len(ids) != len(rows):
        out.append("pilot_program_repeated")
    for row in rows:
        out.extend(
            f"pilot_control_unknown:{c.program_id}" for c in row.controls if c.program_id not in ids
        )
    return tuple(out)


# =============================================================================
# C3.7 layout (pure paths)
# =============================================================================


@dataclass(frozen=True)
class Wave3RunDir:
    """Wave 3 files under the desk run dir (``TREX_DESK_RUN_DIR``)."""

    root: Path

    def assertions(self) -> Path:
        return self.root / "authority" / "assertions"

    def assertion(self, jti: str) -> Path:
        """``trex-family-assertion-seen/1``, created O_EXCL (single use)."""
        return self.assertions() / f"{jti}.json"


@dataclass(frozen=True)
class AcceptanceDir:
    """``TREX_ACCEPTANCE_DIR`` (default ``~/.local/state/trex/acceptance``)."""

    root: Path

    def session(self, session: date) -> Path:
        return self.root / "sessions" / f"{session.isoformat()}.json"

    def lifecycle(self, intent_id: str) -> Path:
        return self.root / "lifecycles" / f"{intent_id}.json"

    def drill(self, drill_id: str) -> Path:
        return self.root / "drills" / f"{drill_id}.json"

    def evidence(self) -> Path:
        """The root every ``EvidenceRef.path`` of acceptance is relative to."""
        return self.root / "evidence"

    def document(self) -> Path:
        """The latest computed ``trex-acceptance/1``."""
        return self.root / "acceptance.json"


@dataclass(frozen=True)
class PilotDir:
    """``TREX_PILOT_DIR`` (default ``~/.local/state/trex/pilot``)."""

    root: Path

    def draft(self, program_id: str) -> Path:
        return self.root / "registrations" / f"{program_id}.draft.json"

    def sealed(self, program_id: str) -> Path:
        """Written once (O_EXCL) by the owner's seal; never rewritten."""
        return self.root / "registrations" / f"{program_id}.sealed.json"

    def usage(self, program_id: str, day: date) -> Path:
        return self.root / "usage" / program_id / f"{day.isoformat()}.json"
