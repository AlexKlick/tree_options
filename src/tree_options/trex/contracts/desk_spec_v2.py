"""C2.6: ``trex-desk-spec/2``, the desk spec for either asset kind.

``specs/<sid>.json`` may hold a v1 spec (``trex.desk_spec.DeskSpec``,
options only, unchanged and still written by today's runtime) or a v2
spec. v2 discriminates on ``asset_kind``: ``option`` wraps the existing
``LegStructure``; ``equity`` wraps an ``EquityPositionSpec`` whose
``position_id`` is the intent id (so the equity spec file is
``specs/<intent_id>.json`` and its exit tag ``trex:desk:<intent_id>``).

``parse_desk_spec`` is the one reader for both versions: v1 goes through
the existing ``DeskSpec`` model; an unknown schema refuses (readers report
it as uncertainty, never skip it).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import Field, TypeAdapter, model_validator

from tree_options.schemas.common import IdStr, StrictModel
from tree_options.trex.contracts.canonical import PathSafeId, Symbol, UTCInstant
from tree_options.trex.contracts.effects import (
    EquityExitPolicy,
    SupervisedEffect,
    SupervisedEquityEffect,
)
from tree_options.trex.desk_spec import SPEC_SCHEMA, DeskSpec
from tree_options.trex.plan import LegStructure
from tree_options.trex.supervised_ibkr import supervised_order_ref

__all__ = [
    "DESK_SPEC_V2_ADAPTER",
    "DESK_SPEC_V2_SCHEMA",
    "DeskSpecV2",
    "EquityDeskSpecV2",
    "EquityPositionSpec",
    "OptionDeskSpecV2",
    "desk_spec_v2_for",
    "parse_desk_spec",
]

DESK_SPEC_V2_SCHEMA = "trex-desk-spec/2"


class EquityPositionSpec(StrictModel):
    """The long stock position the desk owns and exits."""

    position_id: PathSafeId
    symbol: Symbol
    con_id: int = Field(strict=True, ge=1)
    quantity: int = Field(strict=True, ge=1)
    entry_limit_cents: int = Field(strict=True, gt=0)
    exit_policy: EquityExitPolicy

    @model_validator(mode="after")
    def _stop_below_entry(self) -> EquityPositionSpec:
        if self.exit_policy.stop_price_cents >= self.entry_limit_cents:
            raise ValueError("stop_price_cents must be below entry_limit_cents")
        target = self.exit_policy.target_price_cents
        if target is not None and target <= self.entry_limit_cents:
            raise ValueError("target_price_cents must be above entry_limit_cents")
        return self


class _DeskSpecV2Base(StrictModel):
    schema_version: Literal["trex-desk-spec/2"] = Field(default="trex-desk-spec/2", alias="schema")
    intent_id: PathSafeId
    account_id: IdStr
    entry_order_ref: str
    registered_at: UTCInstant

    @model_validator(mode="after")
    def _entry_tag(self) -> _DeskSpecV2Base:
        if self.entry_order_ref != supervised_order_ref(self.intent_id):
            raise ValueError("entry_order_ref must be the intent's supervised tag")
        return self


class OptionDeskSpecV2(_DeskSpecV2Base):
    asset_kind: Literal["option"]
    structure: LegStructure


class EquityDeskSpecV2(_DeskSpecV2Base):
    asset_kind: Literal["equity"]
    position: EquityPositionSpec

    @model_validator(mode="after")
    def _position_is_intent(self) -> EquityDeskSpecV2:
        if self.position.position_id != self.intent_id:
            raise ValueError("an equity position_id is its intent_id")
        return self


DeskSpecV2 = Annotated[OptionDeskSpecV2 | EquityDeskSpecV2, Field(discriminator="asset_kind")]
DESK_SPEC_V2_ADAPTER: TypeAdapter[OptionDeskSpecV2 | EquityDeskSpecV2] = TypeAdapter(DeskSpecV2)


def parse_desk_spec(
    document: Mapping[str, Any],
) -> DeskSpec | OptionDeskSpecV2 | EquityDeskSpecV2:
    """Read a v1 or v2 spec document; any other schema refuses."""
    schema = document.get("schema")
    if schema == SPEC_SCHEMA:
        return DeskSpec.model_validate(dict(document))
    if schema == DESK_SPEC_V2_SCHEMA:
        return DESK_SPEC_V2_ADAPTER.validate_python(dict(document))
    raise ValueError(f"unknown desk spec schema: {schema!r}")


def desk_spec_v2_for(
    effect: SupervisedEffect | SupervisedEquityEffect, registered_at: datetime
) -> OptionDeskSpecV2 | EquityDeskSpecV2:
    """The v2 spec the runtime registers for an effect before sending it."""
    if isinstance(effect, SupervisedEquityEffect):
        return EquityDeskSpecV2(
            intent_id=effect.intent_id,
            account_id=effect.account_id,
            entry_order_ref=effect.order_ref,
            registered_at=registered_at,
            asset_kind="equity",
            position=EquityPositionSpec(
                position_id=effect.intent_id,
                symbol=effect.symbol,
                con_id=effect.con_id,
                quantity=effect.quantity,
                entry_limit_cents=effect.limit_cents,
                exit_policy=effect.exit_policy,
            ),
        )
    return OptionDeskSpecV2(
        intent_id=effect.intent_id,
        account_id=effect.account_id,
        entry_order_ref=effect.order_ref,
        registered_at=registered_at,
        asset_kind="option",
        structure=effect.structure,
    )
