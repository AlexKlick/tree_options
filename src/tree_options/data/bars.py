"""Bars: the vendor's raw row (exactly as delivered) and the normalized,
provenance-carrying bar record (handoff §6.4 adapted to equity bars).

Raw and normalized are deliberately separate models: normalization may
change (new revisions), raw never does. The source_row_hash binds every
normalized bar to the exact raw bytes it came from.
"""

from __future__ import annotations

from datetime import date

from pydantic import NonNegativeInt, model_validator

from tree_options.schemas.common import IdStr, Price, StrictModel, UTCDatetime


class RawBarRow(StrictModel):
    """One vendor bar row, exactly as delivered (immutable raw object)."""

    vendor_symbol: IdStr  # ticker AS DELIVERED — identity resolution is ingestion's job
    session: date
    open: Price
    high: Price
    low: Price
    close: Price
    volume: NonNegativeInt
    available_at: UTCDatetime  # vendor publication instant
    source_record_id: IdStr

    @model_validator(mode="after")
    def _coherent(self) -> RawBarRow:
        if not (self.low <= self.open <= self.high and self.low <= self.close <= self.high):
            raise ValueError(
                f"incoherent OHLC for {self.source_record_id}: "
                f"o={self.open} h={self.high} l={self.low} c={self.close}"
            )
        return self


class BarRecord(StrictModel):
    """Normalized bar: security identity resolved, provenance attached."""

    security_id: IdStr
    session: date
    open: Price
    high: Price
    low: Price
    close: Price
    volume: NonNegativeInt
    source: IdStr
    source_record_id: IdStr
    source_row_hash: IdStr  # sha256 of the raw row's canonical bytes
    snapshot_id: IdStr
    available_at: UTCDatetime

    @model_validator(mode="after")
    def _coherent(self) -> BarRecord:
        if not (self.low <= self.open <= self.high and self.low <= self.close <= self.high):
            raise ValueError(f"incoherent OHLC for {self.source_record_id}")
        return self
