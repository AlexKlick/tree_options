"""Security master (§6.1): stable identity, dated ticker mappings, delistings.

Ticker is NOT identity (INV-08): model joins use security_id/figi/cik with
dated ticker mappings; the historical universe includes delisted names.
"""

from __future__ import annotations

from datetime import date, datetime

from pydantic import model_validator

from tree_options.schemas.common import IdStr, StrictModel, UTCDatetime


class TickerMappingRecord(StrictModel):
    security_id: IdStr
    ticker: IdStr
    effective_from: date
    effective_to: date | None = None
    # When this mapping became KNOWABLE (announcement/filing), not when it
    # became effective — the gap between the two is exactly the leak window.
    available_at: UTCDatetime

    @model_validator(mode="after")
    def _ordered(self) -> TickerMappingRecord:
        if self.effective_to is not None and self.effective_to < self.effective_from:
            raise ValueError("effective_to must be >= effective_from")
        return self


class DelistingRecord(StrictModel):
    delisting_session: date
    reason: IdStr
    final_price_available: bool
    available_at: UTCDatetime  # when the delisting became knowable


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
        ends = [
            m.effective_to for m in sorted(self.ticker_mappings, key=lambda m: m.effective_from)
        ]
        for prev_end, next_start in zip(ends[:-1], starts[1:], strict=False):
            if prev_end is None or prev_end >= next_start:
                raise ValueError("overlapping ticker mapping windows")
        if self.listing_end is not None and self.listing_end < self.listing_start:
            raise ValueError("listing_end must be >= listing_start")
        if self.delisting is not None:
            if self.listing_end is None or self.listing_end != self.delisting.delisting_session:
                raise ValueError("delisted security must have listing_end == delisting_session")
        return self

    def _record_visible(self, as_of: datetime | None) -> bool:
        """The master RECORD itself must be knowable at as_of (review F2):
        a record that arrived after the decision instant is wholly invisible."""
        return as_of is None or self.available_at <= as_of

    def ticker_on(self, d: date, *, as_of: datetime | None = None) -> str:
        """Ticker in force on d, given what was knowable at `as_of`.

        as_of=None is the retrospective (settled-history) view. With as_of
        set, the record and any mapping announced after it are INVISIBLE: a
        January decision cannot see a March rename, and a date covered only
        by an not-yet-known record or mapping fails closed with KeyError.
        """
        if not self._record_visible(as_of):
            raise KeyError(f"security {self.security_id} record not knowable as of {as_of}")
        for m in self.ticker_mappings:
            if as_of is not None and m.available_at > as_of:
                continue
            if m.effective_from <= d and (m.effective_to is None or d <= m.effective_to):
                return m.ticker
        raise KeyError(
            f"no ticker mapping covers {d} for {self.security_id}"
            + (f" as of {as_of}" if as_of is not None else "")
        )

    def listed_on(self, d: date, *, as_of: datetime | None = None) -> bool:
        """Membership on d given knowledge at `as_of`.

        Fails closed to False when the record itself is not yet knowable.
        A listing END is only knowable at as_of when its delisting record is
        visible: with no visible delisting the honest point-in-time answer
        past listing_start is True (unknown end) — answering False would leak
        the future end date.
        """
        if not self._record_visible(as_of):
            return False
        if d < self.listing_start:
            return False
        if as_of is None:
            effective_end: date | None = self.listing_end
        else:
            end_known = self.delisting is not None and self.delisting.available_at <= as_of
            effective_end = self.listing_end if end_known else None
        return effective_end is None or d <= effective_end
