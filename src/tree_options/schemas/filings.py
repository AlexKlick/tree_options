"""Filing records: the ingestion-time source of truth for availability (INV-03/04).

Derived filing features may not become available before the filing is public:
fundamentals key to acceptance/publication time (never fiscal period end);
insider activity keys to Form 4 publication (never the transaction date).
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from tree_options.schemas.common import IdStr, StrictModel, UTCDatetime


class FilingRecord(StrictModel):
    source_record_id: IdStr  # e.g. EDGAR accession number
    form_type: Literal["10-K", "10-Q", "8-K", "Form 4", "S-1", "13F"]
    filer_security_id: IdStr
    period_of_report: date | None = None  # fiscal period end; NEVER availability
    event_date: date | None = None  # Form 4 transaction date; NEVER availability
    acceptance_instant: UTCDatetime  # filing became public at/after this
    source: IdStr = "edgar"

    @classmethod
    def form4(cls, **kw) -> FilingRecord:
        kw.setdefault("form_type", "Form 4")
        return cls(**kw)

    @classmethod
    def quarterly_report(cls, **kw) -> FilingRecord:
        kw.setdefault("form_type", "10-Q")
        return cls(**kw)
