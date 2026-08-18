"""Option contract master (§6.3): identity, deliverables, corporate actions.

INV-09 hook: `exists_on(d)` — a candidate contract must have existed at the
decision time. A nonstandard deliverable must trace to a corporate action.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import Field, model_validator

from tree_options.schemas.common import IdStr, Price, StrictModel, UTCDatetime


class DeliverableSpec(StrictModel):
    shares_per_contract: Decimal = Field(gt=0)
    underlier_per_share_terms: IdStr = "1.0 share of underlying"
    corporate_action_id: IdStr | None = None


class CorporateActionRecord(StrictModel):
    corporate_action_id: IdStr
    security_id: IdStr
    action_type: Literal["split", "rename", "delisting", "special_dividend", "merger"]
    effective_session: date
    ratio: Decimal | None = None
    available_at: UTCDatetime


class OptionContract(StrictModel):
    contract_id: IdStr
    option_root: IdStr
    underlying_security_id: IdStr
    expiration: date
    strike: Price
    call_put: Literal["C", "P"]
    multiplier: int = Field(default=100, ge=1)
    exercise_style: Literal["american", "european"] = "american"
    listing_start: date
    listing_end: date | None
    deliverable: DeliverableSpec
    standard_contract_flag: bool
    corporate_action_id: IdStr | None = None

    @model_validator(mode="after")
    def _consistent(self) -> OptionContract:
        if self.listing_end is None:
            raise ValueError("contract listing_end is required (== expiration when dead)")
        if self.listing_end < self.listing_start:
            raise ValueError("listing_end must be >= listing_start")
        if self.expiration < self.listing_start:
            raise ValueError("expiration must be >= listing_start")
        if not self.standard_contract_flag:
            if self.corporate_action_id is None:
                raise ValueError("nonstandard deliverable requires corporate_action_id")
            if self.deliverable.shares_per_contract == Decimal("100"):
                raise ValueError("nonstandard flag requires deliverable != 100 shares")
            if self.deliverable.corporate_action_id != self.corporate_action_id:
                raise ValueError("deliverable corporate_action_id must match contract")
        else:
            if self.deliverable.shares_per_contract != Decimal("100"):
                raise ValueError("standard contract must deliver 100 shares")
            if self.multiplier != 100:
                raise ValueError(
                    "standard contract multiplier must be 100 "
                    f"(got {self.multiplier}): cash notional is price x quantity "
                    "x multiplier and must match the 100-share deliverable"
                )
        return self

    def exists_on(self, d: date) -> bool:
        """INV-09: the contract actually existed (was listed) on session d."""
        return self.listing_start <= d <= (self.listing_end or d)

    def expired_on(self, d: date) -> bool:
        return d > self.expiration
