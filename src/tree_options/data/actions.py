"""Corporate actions (M1 packet workstream B): raw vendor rows and
normalized records, one per kind, each carrying its own available_at.

Raw prices are stored separately from adjustment factors; these records
are the only sanctioned representation of a corporate action — an
unrepresented split is a quality-gate failure, not a silent adjustment.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Literal

from pydantic import PositiveInt, model_validator

from tree_options.schemas.common import IdStr, Money, StrictModel, UTCDatetime


class ActionKind(StrEnum):
    SPLIT = "split"
    REVERSE_SPLIT = "reverse_split"
    CASH_DIVIDEND = "cash_dividend"
    STOCK_DIVIDEND = "stock_dividend"
    MERGER = "merger"
    SPINOFF = "spinoff"
    SYMBOL_CHANGE = "symbol_change"
    DELISTING = "delisting"


_RATIOD = {ActionKind.SPLIT, ActionKind.REVERSE_SPLIT, ActionKind.STOCK_DIVIDEND}
_CASHED = {ActionKind.CASH_DIVIDEND}


class RawActionRow(StrictModel):
    """One vendor action row, exactly as delivered."""

    vendor_symbol: IdStr
    kind: ActionKind
    effective_session: date
    ratio_numerator: PositiveInt | None = None
    ratio_denominator: PositiveInt | None = None
    cash_amount: Money | None = None
    successor_security_id: IdStr | None = None
    available_at: UTCDatetime
    source_record_id: IdStr

    @model_validator(mode="after")
    def _kind_consistent(self) -> RawActionRow:
        if self.kind in _RATIOD and (
            self.ratio_numerator is None or self.ratio_denominator is None
        ):
            raise ValueError(f"{self.kind.value} requires ratio_numerator and ratio_denominator")
        if self.kind in _CASHED and self.cash_amount is None:
            raise ValueError(f"{self.kind.value} requires cash_amount")
        return self


class CorporateActionRecord(StrictModel):
    """Normalized corporate action: identity resolved, provenance attached."""

    security_id: IdStr
    kind: Literal[
        "split",
        "reverse_split",
        "cash_dividend",
        "stock_dividend",
        "merger",
        "spinoff",
        "symbol_change",
        "delisting",
    ]
    effective_session: date
    ratio_numerator: PositiveInt | None = None
    ratio_denominator: PositiveInt | None = None
    cash_amount: Money | None = None
    successor_security_id: IdStr | None = None
    source: IdStr
    source_record_id: IdStr
    source_row_hash: IdStr
    snapshot_id: IdStr
    available_at: UTCDatetime

    @model_validator(mode="after")
    def _kind_consistent(self) -> CorporateActionRecord:
        kind = ActionKind(self.kind)
        if kind in _RATIOD and (self.ratio_numerator is None or self.ratio_denominator is None):
            raise ValueError(f"{self.kind} requires ratio_numerator and ratio_denominator")
        if kind in _CASHED and self.cash_amount is None:
            raise ValueError(f"{self.kind} requires cash_amount")
        if kind in (ActionKind.MERGER, ActionKind.SPINOFF) and self.successor_security_id is None:
            raise ValueError(f"{self.kind} requires successor_security_id")
        return self
