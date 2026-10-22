"""C2.2b: ``trex-command/1``, ``trex-command-result/1`` and ``CommandAuthority``.

Every owner action from the web (or a loopback CLI) is one command. The
client authors the body (``idempotency_key``, ``kind``, ``args``,
``issued_at``, ``expires_at``); the command backend authenticates the
raw request through a ``CommandAuthority`` and stamps ``issued_by``.

Idempotency: ``commands/<idempotency_key>.json`` is written once. A
second command with the same key and the same ``command_body_sha256`` is
answered ``duplicate`` (``duplicate_command``) with the first outcome; the
same key with a different body is refused ``command_body_mismatch``. The
body hash excludes the per-HTTP-request ``issued_by.request_id`` and
``issued_by.request_sha256`` (a browser retry is a duplicate, not a
mismatch) but includes the actor's ``sub`` and ``ingress`` (another
principal reusing a key is a mismatch).

Wave 5 amendment (additive, 2026-10-21): ``compose_entry`` takes an
optional ``manual_equity_ticket`` (``ManualEquityTicket``): the owner's own
whole-share SPY ticket, composed by the desk from the ticket and a live
quote. Its candidate id is ``manual:<lower-case uuid>`` and no draft file
stands behind it. An absent ticket is absent from the document, so every
earlier ``compose_entry`` keeps its bytes and its ``command_body_sha256``.
The review amendment adds one constant, ``MANUAL_MAX_HOLD_SESSIONS`` (no
schema change).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Annotated, Any, Final, Literal, Protocol

from pydantic import (
    Field,
    JsonValue,
    SerializerFunctionWrapHandler,
    TypeAdapter,
    model_serializer,
    model_validator,
)

from tree_options.action_graph.proposal import canonical_bytes
from tree_options.schemas.common import IdStr, StrictModel
from tree_options.trex.contracts.canonical import (
    IdempotencyKey,
    PathSafeId,
    Reason,
    Sha256Hex,
    Symbol,
    UTCInstant,
    model_document,
    sha256_hex,
)
from tree_options.trex.contracts.defaults import COMMAND_MAX_TTL_S
from tree_options.trex.contracts.vocabulary import Blocker

__all__ = [
    "COMMAND_ADAPTER",
    "COMMAND_RESULT_SCHEMA",
    "COMMAND_SCHEMA",
    "MANUAL_CANDIDATE_PREFIX",
    "MANUAL_MAX_HOLD_SESSIONS",
    "MANUAL_STRATEGY_VERSION",
    "Actor",
    "AnyCommand",
    "ApproveEntryArgs",
    "ApproveEntryCommand",
    "BackupStateArgs",
    "BackupStateCommand",
    "CommandAuthority",
    "CommandAuthorityRefused",
    "CommandKind",
    "CommandResult",
    "ComposeEntryArgs",
    "ComposeEntryCommand",
    "FlattenCommand",
    "HaltCommand",
    "ManualEquityTicket",
    "ManualLimit",
    "PauseEntriesCommand",
    "RawCommandRequest",
    "ReasonArgs",
    "RejectEntryArgs",
    "RejectEntryCommand",
    "ResumeAllCommand",
    "ResumeEntriesCommand",
    "SealRegistrationArgs",
    "SealRegistrationCommand",
    "TrexCommand",
    "actor_binds_request",
    "command_body_sha256",
    "manual_intent_id",
    "parse_command",
]

COMMAND_SCHEMA = "trex-command/1"
COMMAND_RESULT_SCHEMA = "trex-command-result/1"

CommandKind = Literal[
    "compose_entry",
    "approve_entry",
    "reject_entry",
    "pause_entries",
    "resume_entries",
    "halt",
    "flatten",
    "resume_all",
    "backup_state",
    # Wave 4 amendment (lane I, C3.5): the owner seals a pilot registration
    "seal_registration",
]


class Actor(StrictModel):
    """Who issued a command, as the ``CommandAuthority`` verified it.

    ``sub``: the authenticated principal (family-apps signed actor subject,
    or the loopback operator label); ``request_id``: the backend's id for
    the HTTP request; ``request_sha256``: sha256 of the raw request body.
    """

    sub: IdStr
    ingress: Literal["loopback", "family"]
    request_id: IdStr
    request_sha256: Sha256Hex


@dataclass(frozen=True)
class RawCommandRequest:
    """The unparsed request a ``CommandAuthority`` verifies.

    ``headers`` keys are lower-case; ``peer_host`` is the transport peer
    (never a forwarded header); ``request_id`` is assigned by the backend.
    """

    method: str
    path: str
    headers: Mapping[str, str]
    body: bytes
    peer_host: str
    request_id: str


class CommandAuthorityRefused(Exception):
    """The request is not an authenticated owner command."""

    def __init__(self, reason: str = Blocker.COMMAND_UNAUTHORIZED, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


class CommandAuthority(Protocol):
    """Authenticates a raw command request (lane E implements; lane G calls).

    Returns the ``Actor`` (with ``request_id`` and ``request_sha256`` bound
    to ``raw_request``) or raises ``CommandAuthorityRefused``. Never parses
    or applies the command itself.
    """

    def verify(self, raw_request: RawCommandRequest) -> Actor: ...


def actor_binds_request(actor: Actor, raw_request: RawCommandRequest) -> bool:
    """True when the actor was minted for exactly this request."""
    return actor.request_id == raw_request.request_id and actor.request_sha256 == sha256_hex(
        raw_request.body
    )


# -- args ------------------------------------------------------------------


#: Wave 5: the candidate id of a manual ticket is this prefix plus a
#: lower-case UUID the client mints once per ticket.
MANUAL_CANDIDATE_PREFIX: Final = "manual:"
#: Wave 5: the ``strategy_version`` the desk binds to every manual ticket.
MANUAL_STRATEGY_VERSION: Final = "owner-manual/1"
#: Wave 5 review amendment (additive): a manual ticket's
#: ``exit_deadline_session`` is at most this many NYSE sessions after its
#: entry session (the desk refuses a later one; the repo calendar counts).
MANUAL_MAX_HOLD_SESSIONS: Final = 20
_MANUAL_CANDIDATE = re.compile(
    r"^manual:([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$"
)


def manual_intent_id(candidate_id: str) -> str:
    """The path-safe intent id of a manual ticket (``manual-<uuid>``): the
    ``:`` of the candidate id cannot be a file name (mandate/2, spec/2)."""
    match = _MANUAL_CANDIDATE.fullmatch(candidate_id)
    if match is None:
        raise ValueError("a manual candidate id is manual:<lower-case uuid>")
    return f"manual-{match.group(1)}"


class ManualLimit(StrictModel):
    """An explicit DAY limit for a manual ticket, in integer cents."""

    limit_cents: int = Field(strict=True, gt=0)


class ManualEquityTicket(StrictModel):
    """The owner's own whole-share, long-only stock ticket (Wave 5).

    The desk turns it into a ``supervised-equity-effect/1``: the stock
    qualified, the limit from a live real-time quote by ``limit_policy``
    (``ask``; ``mid`` = (bid + ask) / 2 rounded up to the cent; or an
    explicit ``limit_cents``), the exit policy exactly as given
    (``stop_confirm_ticks`` and ``forced_exit_et`` at their defaults). The
    allowlist, cash, entry window, session cap, the hold cap
    (``MANUAL_MAX_HOLD_SESSIONS``) and the stop and target against the live
    bid and ask are the desk's screening, never this schema's: a ticket is
    only well formed here."""

    symbol: Symbol
    quantity: int = Field(strict=True, ge=1)
    stop_price_cents: int = Field(strict=True, gt=0)
    target_price_cents: Annotated[int, Field(strict=True, gt=0)] | None = None
    exit_deadline_session: date
    limit_policy: Literal["ask", "mid"] | ManualLimit

    @model_validator(mode="after")
    def _prices(self) -> ManualEquityTicket:
        stop, target = self.stop_price_cents, self.target_price_cents
        if target is not None and target <= stop:
            raise ValueError("target_price_cents must be above stop_price_cents")
        if isinstance(self.limit_policy, ManualLimit):
            limit = self.limit_policy.limit_cents
            if stop >= limit:
                raise ValueError("stop_price_cents must be below limit_cents")
            if target is not None and target <= limit:
                raise ValueError("target_price_cents must be above limit_cents")
        return self


class ComposeEntryArgs(StrictModel):
    """Ask the desk to screen a candidate and write a proposal for it.

    ``manual_equity_ticket`` (Wave 5, optional): set exactly when
    ``candidate_id`` is ``manual:<lower-case uuid>``, and only for
    ``asset_kind`` equity. Absent from the document when None."""

    program_id: IdStr
    candidate_id: IdStr
    asset_kind: Literal["option", "equity"]
    sleeve_ref: IdStr | None = None
    manual_equity_ticket: ManualEquityTicket | None = None

    @model_serializer(mode="wrap")
    def _omit_absent_ticket(self, handler: SerializerFunctionWrapHandler) -> Any:
        """An absent ticket stays absent: an earlier ``compose_entry``
        document (and its body hash) is unchanged."""
        document = handler(self)
        if self.manual_equity_ticket is None and isinstance(document, dict):
            document.pop("manual_equity_ticket", None)
        return document

    @model_validator(mode="after")
    def _manual(self) -> ComposeEntryArgs:
        manual = self.candidate_id.startswith(MANUAL_CANDIDATE_PREFIX)
        if self.manual_equity_ticket is None:
            if manual:
                raise ValueError("a manual: candidate carries its manual_equity_ticket")
            return self
        if self.asset_kind != "equity":
            raise ValueError("a manual ticket is an equity entry")
        if _MANUAL_CANDIDATE.fullmatch(self.candidate_id) is None:
            raise ValueError("a manual ticket's candidate_id is manual:<lower-case uuid>")
        return self


class ApproveEntryArgs(StrictModel):
    """Every binding must equal the stored proposal and the live desk."""

    proposal_id: Sha256Hex
    proposal_sha256: Sha256Hex
    effect_sha256: Sha256Hex
    owner_epoch: IdStr
    profile_digest: Sha256Hex


class RejectEntryArgs(StrictModel):
    proposal_id: Sha256Hex
    reason: Reason = ""


class ReasonArgs(StrictModel):
    """Args of the kill-switch kinds (pause/resume/halt/flatten/resume_all)."""

    reason: Reason = ""


class BackupStateArgs(StrictModel):
    label: PathSafeId | None = None


class SealRegistrationArgs(StrictModel):
    """Args of ``seal_registration`` (WAVE3-CONTRACTS.md C3.5): the pilot
    program to seal and the registration body hash the owner reviewed.
    ``contracts.wave3`` re-exports this class unchanged."""

    program_id: PathSafeId
    registration_sha256: Sha256Hex


# -- commands --------------------------------------------------------------


class _CommandBase(StrictModel):
    schema_version: Literal["trex-command/1"] = Field(default="trex-command/1", alias="schema")
    idempotency_key: IdempotencyKey
    issued_at: UTCInstant
    expires_at: UTCInstant
    issued_by: Actor

    @model_validator(mode="after")
    def _window(self) -> _CommandBase:
        ttl = (self.expires_at - self.issued_at).total_seconds()
        if ttl <= 0:
            raise ValueError("expires_at must be after issued_at")
        if ttl > COMMAND_MAX_TTL_S:
            raise ValueError(f"a command lives at most {COMMAND_MAX_TTL_S}s")
        return self


class ComposeEntryCommand(_CommandBase):
    kind: Literal["compose_entry"]
    args: ComposeEntryArgs


class ApproveEntryCommand(_CommandBase):
    kind: Literal["approve_entry"]
    args: ApproveEntryArgs


class RejectEntryCommand(_CommandBase):
    kind: Literal["reject_entry"]
    args: RejectEntryArgs


class PauseEntriesCommand(_CommandBase):
    kind: Literal["pause_entries"]
    args: ReasonArgs


class ResumeEntriesCommand(_CommandBase):
    kind: Literal["resume_entries"]
    args: ReasonArgs


class HaltCommand(_CommandBase):
    kind: Literal["halt"]
    args: ReasonArgs


class FlattenCommand(_CommandBase):
    kind: Literal["flatten"]
    args: ReasonArgs


class ResumeAllCommand(_CommandBase):
    kind: Literal["resume_all"]
    args: ReasonArgs


class BackupStateCommand(_CommandBase):
    kind: Literal["backup_state"]
    args: BackupStateArgs


class SealRegistrationCommand(_CommandBase):
    kind: Literal["seal_registration"]
    args: SealRegistrationArgs


AnyCommand = (
    ComposeEntryCommand
    | ApproveEntryCommand
    | RejectEntryCommand
    | PauseEntriesCommand
    | ResumeEntriesCommand
    | HaltCommand
    | FlattenCommand
    | ResumeAllCommand
    | BackupStateCommand
    | SealRegistrationCommand
)
#: Discriminated on ``kind``; an unknown kind refuses.
TrexCommand = Annotated[AnyCommand, Field(discriminator="kind")]
COMMAND_ADAPTER: TypeAdapter[AnyCommand] = TypeAdapter(TrexCommand)


def parse_command(document: Mapping[str, Any]) -> AnyCommand:
    """Validate a command document; an unknown kind or bad args refuse."""
    return COMMAND_ADAPTER.validate_python(dict(document))


def command_body_sha256(command: _CommandBase) -> str:
    """The idempotency comparison hash (see the module docstring)."""
    document = model_document(command)
    document["issued_by"] = {
        "sub": command.issued_by.sub,
        "ingress": command.issued_by.ingress,
    }
    return sha256_hex(canonical_bytes(document))


# -- results ---------------------------------------------------------------

CommandStatus = Literal["accepted", "refused", "applied", "failed", "duplicate"]


class CommandResult(StrictModel):
    """The outcome of one command (``commands/<key>.result.json``).

    - ``accepted``: validated and durably queued; nothing applied yet;
    - ``applied``: the effect is done (``result`` carries its facts);
    - ``refused``: refused before any effect (``reason`` required);
    - ``failed``: applying it failed (``reason`` required; any broker
      side effect is preserved as uncertain by the supervised core);
    - ``duplicate``: same key and body as an earlier command (``reason``
      is ``duplicate_command``; ``result`` names the original outcome).
    """

    schema_version: Literal["trex-command-result/1"] = Field(
        default="trex-command-result/1", alias="schema"
    )
    idempotency_key: IdempotencyKey
    kind: CommandKind
    command_body_sha256: Sha256Hex
    status: CommandStatus
    reason: IdStr | None = None
    detail: str = ""
    result: dict[str, JsonValue] | None = None
    at: UTCInstant

    @model_validator(mode="after")
    def _reason_matches_status(self) -> CommandResult:
        if self.status in ("refused", "failed") and self.reason is None:
            raise ValueError(f"a {self.status} result needs a reason")
        if self.status == "duplicate" and self.reason != Blocker.DUPLICATE_COMMAND:
            raise ValueError("a duplicate result's reason is duplicate_command")
        if self.status in ("accepted", "applied") and self.reason is not None:
            raise ValueError(f"an {self.status} result carries no reason")
        return self
