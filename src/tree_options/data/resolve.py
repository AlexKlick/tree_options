"""Point-in-time ticker resolution (M1 packet workstream C join authority).

The ONLY sanctioned ticker→security join. A ticker resolves through
dated mapping windows AND their available_at instants: a mapping
announced after `as_of` is invisible, a ticker claimed by two issuers on
overlapping windows is a vendor-data defect (ambiguity), and the
current-ticker join (today's mapping applied to historical rows) is
structurally impossible here because both `on` and `as_of` are required.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime

from tree_options.schemas.common import IdStr
from tree_options.schemas.security import SecurityMasterRecord, TickerMappingRecord


class UnknownTickerError(KeyError):
    """No knowable mapping covers (symbol, on, as_of)."""


class AmbiguousTickerError(ValueError):
    """Two knowable mappings claim the ticker on the same date — data defect."""


class TickerResolver:
    def __init__(self, records: tuple[SecurityMasterRecord, ...] | list[SecurityMasterRecord]):
        index: dict[str, list[TickerMappingRecord]] = defaultdict(list)
        for record in records:
            for mapping in record.ticker_mappings:
                index[mapping.ticker].append(mapping)
        self._index: dict[str, tuple[TickerMappingRecord, ...]] = {
            t: tuple(ms) for t, ms in index.items()
        }

    def resolve(self, symbol: str, on: date, as_of: datetime) -> IdStr:
        candidates: list[TickerMappingRecord] = []
        for m in self._index.get(symbol, ()):
            if not (m.effective_from <= on and (m.effective_to is None or on <= m.effective_to)):
                continue
            if m.available_at > as_of:
                continue
            candidates.append(m)
        if not candidates:
            raise UnknownTickerError(
                f"no knowable ticker mapping for {symbol!r} on {on} as of {as_of}"
            )
        if len(candidates) > 1:
            owners = sorted({m.security_id for m in candidates})
            raise AmbiguousTickerError(
                f"ticker {symbol!r} claimed by {owners} on {on} as of {as_of}"
            )
        return candidates[0].security_id
