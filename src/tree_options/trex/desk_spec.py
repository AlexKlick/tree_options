"""Persisted supervised desk specification, shared by owner and read models.

This contract is broker-free; reading exposure must not open a broker session.

``specs/<sid>.json`` holds a ``trex-desk-spec/1`` option spec (``DeskSpec``,
what the runtime still writes for options) or, since Wave 2, a
``trex-desk-spec/2`` spec (``tree_options.trex.contracts.desk_spec_v2``):
an option one (the same ``LegStructure``) or an equity one (a long stock
position whose spec id is its intent id). :func:`load_spec` is the one
reader both the runtime and the exposure readers use.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

from pydantic import Field

from tree_options.schemas.common import IdStr, StrictModel
from tree_options.trex.plan import LegStructure

if TYPE_CHECKING:
    from tree_options.trex.contracts.desk_spec_v2 import EquityDeskSpecV2

SPEC_SCHEMA = "trex-desk-spec/1"
SPEC_SCHEMA_V2 = "trex-desk-spec/2"


class DeskSpec(StrictModel):
    """One supervised structure the runtime owns (``specs/<sid>.json``)."""

    schema_version: Literal["trex-desk-spec/1"] = Field(default="trex-desk-spec/1", alias="schema")
    intent_id: IdStr
    account_id: IdStr
    structure: LegStructure
    entry_order_ref: str
    registered_at: datetime


def load_spec(document: Any) -> DeskSpec | EquityDeskSpecV2:
    """One spec file's document as the runtime and readers use it.

    - ``trex-desk-spec/2`` is validated by the frozen v2 contract; an
      OPTION v2 spec comes back as the ``DeskSpec`` it describes (the
      option lane has one model), an equity one as ``EquityDeskSpecV2``;
    - anything else is validated as v1 exactly as before (an unknown
      schema refuses on the v1 literal; callers that require the key
      check its presence themselves).

    Raises ``ValueError`` (pydantic's ``ValidationError`` included) on any
    document it cannot represent: readers report that as uncertainty."""
    if isinstance(document, Mapping) and document.get("schema") == SPEC_SCHEMA_V2:
        # lazy: the contracts package imports this module
        from tree_options.trex.contracts.desk_spec_v2 import (
            EquityDeskSpecV2,
            OptionDeskSpecV2,
            parse_desk_spec,
        )

        spec = parse_desk_spec(document)
        if isinstance(spec, EquityDeskSpecV2):
            return spec
        if isinstance(spec, OptionDeskSpecV2):
            return DeskSpec(
                intent_id=spec.intent_id,
                account_id=spec.account_id,
                structure=spec.structure,
                entry_order_ref=spec.entry_order_ref,
                registered_at=spec.registered_at,
            )
    return DeskSpec.model_validate(document)


def spec_id(spec: DeskSpec | EquityDeskSpecV2) -> str:
    """The id a spec file is named by: the structure id of an option spec,
    the intent id (= position id) of an equity one."""
    if isinstance(spec, DeskSpec):
        return spec.structure.id
    return spec.intent_id
