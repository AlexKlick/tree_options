"""Security master (§6.1): stable identity, dated ticker mappings, delistings.

Ticker is NOT identity (INV-08): model joins use security_id/figi/cik with
dated ticker mappings; the historical universe includes delisted names.
"""

from __future__ import annotations

from datetime import date

from pydantic import model_validator

from tree_options.schemas.common import IdStr, StrictModel, UTCDatetime


class TickerMappingRecord(StrictModel):
    security_id: IdStr
    ticker: IdStr
    effective_from: date
    effective_to: date | None = None

    @model_validator(mode="after")
    def _ordered(self) -> TickerMappingRecord:
        if self.effective_to is not None and self.effective_to < self.effective_from:
            raise ValueError("effective_to must be >= effective_from")
        return self


class DelistingRecord(StrictModel):
    delisting_session: date
    reason: IdStr
    final_price_available: bool


class SecurityMasterRecord(StrictModel):
    security_id: IdStr
    figi: IdStr | None = None
    cik: IdStr | None = None
    security_type: IdStr = "common_stock"
    listing_start: date
    listing_end: date | None = None
    exchange: IdStr
    sector_as_of: date | None = None
    corporate_action_id: IdStr | None = None
    source: IdStr
    available_at: UTCDatetime
    ticker_mappings: tuple[TickerMappingRecord, ...]
    delisting: DelistingRecord | None = None

    @model_validator(mode="after")
    def _consistent(self) -> SecurityMasterRecord:
        if not self.ticker_mappings:
            raise ValueError("at least one ticker mapping required")
        for m in self.ticker_mappings:
            if m.security_id != self.security_id:
                raise ValueError("ticker mapping security_id mismatch")
        # Non-overlapping windows per ticker (renames chain via adjacent windows).
        by_ticker: dict[str, list[TickerMappingRecord]] = {}
        for m in self.ticker_mappings:
            by_ticker.setdefault(m.ticker, []).append(m)
        for ticker, maps in by_ticker.items():
            if len(maps) > 1:
                raise ValueError(f"duplicate ticker mapping for {ticker}")
        starts = sorted(m.effective_from for m in self.ticker_mappings)
        ends = [m.effective_to for m in sorted(self.ticker_mappings, key=lambda m: m.effective_from)]
        for prev_end, next_start in zip(ends[:-1], starts[1:], strict=False):
            if prev_end is None or prev_end >= next_start:
                raise ValueError("overlapping ticker mapping windows")
        if self.listing_end is not None and self.listing_end < self.listing_start:
            raise ValueError("listing_end must be >= listing_start")
        if self.delisting is not None:
            if self.listing_end is None or self.listing_end != self.delisting.delisting_session:
                raise ValueError("delisted security must have listing_end == delisting_session")
        return self

    def ticker_on(self, d: date) -> str:
        """Ticker in force on d; fails closed if d is outside all windows."""
        for m in self.ticker_mappings:
            if m.effective_from <= d and (m.effective_to is None or d <= m.effective_to):
                return m.ticker
        raise KeyError(f"no ticker mapping covers {d} for {self.security_id}")

    def listed_on(self, d: date) -> bool:
        if d < self.listing_start:
            return False
        return self.listing_end is None or d <= self.listing_end
