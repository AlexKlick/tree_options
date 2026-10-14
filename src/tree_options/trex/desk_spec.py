"""Persisted supervised desk specification, shared by owner and read models.

This contract is broker-free; reading exposure must not open a broker session.
"""

from datetime import datetime
from typing import Literal

from pydantic import Field

from tree_options.schemas.common import IdStr, StrictModel
from tree_options.trex.plan import LegStructure

SPEC_SCHEMA = "trex-desk-spec/1"


class DeskSpec(StrictModel):
    """One supervised structure the runtime owns (``specs/<sid>.json``)."""

    schema_version: Literal["trex-desk-spec/1"] = Field(default="trex-desk-spec/1", alias="schema")
    intent_id: IdStr
    account_id: IdStr
    structure: LegStructure
    entry_order_ref: str
    registered_at: datetime
