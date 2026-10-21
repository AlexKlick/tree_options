"""C2.3: ``supervised-mandate/2``, one owner approval = one order.

Stored at ``<supervised dir>/mandates/<intent_id>.json`` by the authority
lane when an ``approve_entry`` command passes ``approval_blockers``; the
permit for that intent may be minted only from it, after which the file is
renamed to ``<intent_id>.consumed.json`` (rename = consumption; a second
permit refuses ``mandate_already_consumed``). The v1 single active
``mandate.json`` is untouched and keeps governing v1 grants.

Expiry: ``min(send_deadline, approved_at + APPROVAL_TTL_S)``. A desk
restart mints a new owner epoch, so every pending approval stops matching
(``approval_epoch_mismatch``): restart invalidates approvals by design.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from tree_options.schemas.common import IdStr, StrictModel
from tree_options.time.sessions import shift_instant
from tree_options.trex.contracts.canonical import (
    IdempotencyKey,
    PathSafeId,
    Sha256Hex,
    UTCInstant,
)
from tree_options.trex.contracts.commands import Actor, ApproveEntryCommand
from tree_options.trex.contracts.defaults import APPROVAL_TTL_S
from tree_options.trex.contracts.proposals import EntryProposal, proposal_sha256
from tree_options.trex.contracts.vocabulary import Blocker

__all__ = [
    "MANDATE_V2_SCHEMA",
    "SupervisedMandateV2",
    "approval_blockers",
    "approval_expiry",
    "mandate_blockers",
    "mandate_for_approval",
]

MANDATE_V2_SCHEMA = "supervised-mandate/2"


def _reached(now: datetime, instant: datetime) -> bool:
    return (now - instant).total_seconds() >= 0


def approval_expiry(approved_at: datetime, send_deadline: datetime) -> datetime:
    """min(send_deadline, approved_at + APPROVAL_TTL_S), as a UTC instant."""
    return min(send_deadline, shift_instant(approved_at, APPROVAL_TTL_S))


class SupervisedMandateV2(StrictModel):
    """Per-trade authority: exactly one order for exactly one effect."""

    schema_version: Literal["supervised-mandate/2"] = Field(
        default="supervised-mandate/2", alias="schema"
    )
    intent_id: PathSafeId
    environment: Literal["ibkr-paper"] = "ibkr-paper"
    account_id: IdStr
    strategy_version: IdStr
    proposal_id: Sha256Hex
    effect_sha256: Sha256Hex
    max_orders: Literal[1] = 1
    approval_id: IdempotencyKey
    owner_epoch: IdStr
    profile_digest: Sha256Hex
    send_deadline: UTCInstant
    granted_at: UTCInstant
    expires_at: UTCInstant
    granted_by: Actor

    @model_validator(mode="after")
    def _window(self) -> SupervisedMandateV2:
        ttl = (self.expires_at - self.granted_at).total_seconds()
        if ttl <= 0:
            raise ValueError("expires_at must be after granted_at")
        if (self.expires_at - self.send_deadline).total_seconds() > 0:
            raise ValueError("expires_at must not be after the proposal's send_deadline")
        if ttl > APPROVAL_TTL_S:
            raise ValueError(f"an approval lives at most {APPROVAL_TTL_S}s")
        return self

    def expired_at(self, now: datetime) -> bool:
        return _reached(now, self.expires_at)


def approval_blockers(
    proposal: EntryProposal,
    command: ApproveEntryCommand,
    *,
    now: datetime,
    owner_epoch: str,
    profile_digest: str,
) -> tuple[Blocker, ...]:
    """Why ``command`` may NOT approve ``proposal`` now (empty = it may).

    ``owner_epoch`` / ``profile_digest`` are the LIVE desk's values. Pure:
    duplicate keys, consumption and pause state are the caller's checks.
    """
    args = command.args
    blockers: list[Blocker] = []
    if args.proposal_id != proposal.proposal_id or args.proposal_sha256 != proposal_sha256(
        proposal
    ):
        blockers.append(Blocker.APPROVAL_PROPOSAL_MISMATCH)
    if args.effect_sha256 != proposal.effect_sha256:
        blockers.append(Blocker.APPROVAL_EFFECT_MISMATCH)
    if args.owner_epoch != owner_epoch or proposal.review.owner_epoch != owner_epoch:
        blockers.append(Blocker.APPROVAL_EPOCH_MISMATCH)
    if args.profile_digest != profile_digest or proposal.review.profile_digest != profile_digest:
        blockers.append(Blocker.APPROVAL_PROFILE_MISMATCH)
    if _reached(now, command.expires_at):
        blockers.append(Blocker.COMMAND_EXPIRED)
    if _reached(now, proposal.send_deadline):
        blockers.append(Blocker.APPROVAL_EXPIRED)
    return tuple(blockers)


def mandate_for_approval(
    proposal: EntryProposal, command: ApproveEntryCommand, *, now: datetime
) -> SupervisedMandateV2:
    """The mandate an approval grants (call only when ``approval_blockers``
    is empty; the validators still refuse an expired window)."""
    effect = proposal.effect
    return SupervisedMandateV2(
        intent_id=effect.intent_id,
        account_id=effect.account_id,
        strategy_version=proposal.strategy_version,
        proposal_id=proposal.proposal_id,
        effect_sha256=proposal.effect_sha256,
        approval_id=command.idempotency_key,
        owner_epoch=command.args.owner_epoch,
        profile_digest=command.args.profile_digest,
        send_deadline=proposal.send_deadline,
        granted_at=now,
        expires_at=approval_expiry(now, proposal.send_deadline),
        granted_by=command.issued_by,
    )


def mandate_blockers(
    mandate: SupervisedMandateV2,
    *,
    now: datetime,
    effect_sha256: str,
    owner_epoch: str,
    profile_digest: str,
) -> tuple[Blocker, ...]:
    """Why the mandate may NOT mint a permit for ``effect_sha256`` now.

    Consumption (the ``.consumed.json`` rename) is the caller's check.
    """
    blockers: list[Blocker] = []
    if mandate.expired_at(now):
        blockers.append(Blocker.APPROVAL_EXPIRED)
    if mandate.owner_epoch != owner_epoch:
        blockers.append(Blocker.APPROVAL_EPOCH_MISMATCH)
    if mandate.profile_digest != profile_digest:
        blockers.append(Blocker.APPROVAL_PROFILE_MISMATCH)
    if mandate.effect_sha256 != effect_sha256:
        blockers.append(Blocker.APPROVAL_EFFECT_MISMATCH)
    return tuple(blockers)
