"""C2.2a: ``trex-entry-proposal/1``, what the owner approves.

A proposal is written to ``proposals/<proposal_id>.json`` in its canonical
bytes only after the desk's screening of the effect returned zero
blockers. It carries the effect, the facts the owner reviews, and the
bindings an approval must repeat (``effect_sha256``, ``owner_epoch``,
``profile_digest``).

``proposal_id`` = sha256 of the canonical bytes of the proposal document
WITHOUT its ``proposal_id`` key; every field (economic or not) is
therefore bound. ``proposal_sha256`` = sha256 of the full canonical
document (the file bytes), the second hash an approval repeats.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, model_validator

from tree_options.action_graph.proposal import canonical_bytes
from tree_options.schemas.common import IdStr, StrictModel
from tree_options.time.sessions import SESSION_TIMEZONE
from tree_options.trex.contracts.canonical import (
    Sha256Hex,
    UTCInstant,
    model_bytes,
    model_document,
    sha256_hex,
)
from tree_options.trex.contracts.defaults import PROPOSAL_MAX_TTL_S
from tree_options.trex.contracts.effects import (
    EffectV1,
    SupervisedEquityEffect,
    effect_v1_sha256,
    equity_notional_cents,
    equity_stop_loss_cents,
)

__all__ = [
    "PROPOSAL_SCHEMA",
    "EntryProposal",
    "EntryProposalBody",
    "Quote",
    "ReviewFacts",
    "proposal_bytes",
    "proposal_id_of",
    "proposal_sha256",
]

PROPOSAL_SCHEMA = "trex-entry-proposal/1"

MarketDataType = Literal["realtime", "delayed", "frozen", "unknown"]


class Quote(StrictModel):
    """The quote the owner saw. Options: the package quote in debit
    orientation; equities: the NBBO. ``market_data_type`` is what IBKR
    reported (``reqMarketDataType`` 1/3/2, anything else ``unknown``)."""

    bid_cents: int = Field(strict=True, ge=0)
    ask_cents: int = Field(strict=True, gt=0)
    market_data_type: MarketDataType
    as_of: UTCInstant

    @model_validator(mode="after")
    def _ordered(self) -> Quote:
        if self.bid_cents > self.ask_cents:
            raise ValueError("bid_cents must not exceed ask_cents")
        return self


class ReviewFacts(StrictModel):
    """What the owner reviews, and the bindings an approval repeats."""

    quote: Quote
    max_loss_cents: int = Field(strict=True, gt=0)
    notional_cents: int = Field(strict=True, gt=0)
    screening_sha256: Sha256Hex
    owner_epoch: IdStr
    profile_digest: Sha256Hex


class EntryProposalBody(StrictModel):
    """Every proposal field except its id (the hashed body)."""

    schema_version: Literal["trex-entry-proposal/1"] = Field(
        default="trex-entry-proposal/1", alias="schema"
    )
    effect: EffectV1
    effect_sha256: Sha256Hex
    strategy_version: IdStr
    program_id: IdStr
    sleeve_ref: IdStr | None
    proposed_at: UTCInstant
    send_deadline: UTCInstant
    review: ReviewFacts

    @model_validator(mode="after")
    def _consistent(self) -> EntryProposalBody:
        if self.effect_sha256 != effect_v1_sha256(self.effect):
            raise ValueError("effect_sha256 is not the hash of the effect's canonical bytes")
        ttl = (self.send_deadline - self.proposed_at).total_seconds()
        if ttl <= 0:
            raise ValueError("send_deadline must be after proposed_at")
        if ttl > PROPOSAL_MAX_TTL_S:
            raise ValueError(f"send_deadline is more than {PROPOSAL_MAX_TTL_S}s after proposed_at")
        if (self.review.quote.as_of - self.proposed_at).total_seconds() > 0:
            raise ValueError("the reviewed quote cannot postdate the proposal")
        effect = self.effect
        if isinstance(effect, SupervisedEquityEffect):
            if self.review.notional_cents != equity_notional_cents(effect):
                raise ValueError("notional_cents must equal quantity x limit_cents")
            if self.review.max_loss_cents < equity_stop_loss_cents(effect):
                raise ValueError("max_loss_cents is below the stop loss")
            entry_session = self.send_deadline.astimezone(SESSION_TIMEZONE).date()
            if effect.exit_policy.exit_deadline_session < entry_session:
                raise ValueError("exit_deadline_session precedes the entry session")
        return self


def proposal_id_of(body: EntryProposalBody | dict[str, Any]) -> str:
    """sha256 of the canonical bytes of the document without ``proposal_id``."""
    if isinstance(body, dict):
        document = {k: v for k, v in body.items() if k != "proposal_id"}
    else:
        document = model_document(body)
        document.pop("proposal_id", None)
    return sha256_hex(canonical_bytes(document))


class EntryProposal(EntryProposalBody):
    """A proposal with its content-derived id; a wrong id refuses."""

    proposal_id: Sha256Hex

    @model_validator(mode="after")
    def _id_binds_body(self) -> EntryProposal:
        if self.proposal_id != proposal_id_of(self):
            raise ValueError("proposal_id is not the hash of the proposal body")
        return self

    @classmethod
    def create(cls, body: EntryProposalBody) -> EntryProposal:
        """Mint the proposal for a validated body (id computed, never supplied)."""
        document = model_document(body)
        return cls.model_validate({**document, "proposal_id": proposal_id_of(body)})


def proposal_bytes(proposal: EntryProposal) -> bytes:
    """The bytes of ``proposals/<proposal_id>.json``."""
    return model_bytes(proposal)


def proposal_sha256(proposal: EntryProposal) -> str:
    return sha256_hex(proposal_bytes(proposal))
