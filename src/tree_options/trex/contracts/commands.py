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
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Annotated, Any, Literal, Protocol

from pydantic import Field, JsonValue, TypeAdapter, model_validator

from tree_options.action_graph.proposal import canonical_bytes
from tree_options.schemas.common import IdStr, StrictModel
from tree_options.trex.contracts.canonical import (
    IdempotencyKey,
    PathSafeId,
    Reason,
    Sha256Hex,
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
    "PauseEntriesCommand",
    "RawCommandRequest",
    "ReasonArgs",
    "RejectEntryArgs",
    "RejectEntryCommand",
    "ResumeAllCommand",
    "ResumeEntriesCommand",
    "TrexCommand",
    "actor_binds_request",
    "command_body_sha256",
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


class ComposeEntryArgs(StrictModel):
    """Ask the desk to screen a candidate and write a proposal for it."""

    program_id: IdStr
    candidate_id: IdStr
    asset_kind: Literal["option", "equity"]
    sleeve_ref: IdStr | None = None


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
