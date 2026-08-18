"""Point-in-time ticker resolution (M1 packet workstream C join authority).

The ONLY sanctioned ticker→security join. A mapping resolves only when
BOTH its record and the mapping itself are knowable at `as_of` (the M0
master contract: a record that arrives after the decision instant is
wholly invisible, mappings included), the effective window covers `on`,
and no second issuer claims the same ticker (ambiguity = vendor-data
defect). The current-ticker join is structurally impossible here because
both `on` and `as_of` are required.
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
        index: dict[str, list[tuple[datetime, TickerMappingRecord]]] = defaultdict(list)
        for record in records:
            for mapping in record.ticker_mappings:
                index[mapping.ticker].append((record.available_at, mapping))
        self._index: dict[str, tuple[tuple[datetime, TickerMappingRecord], ...]] = {
            t: tuple(entries) for t, entries in index.items()
        }

    def resolve(self, symbol: str, on: date, as_of: datetime) -> IdStr:
        candidates: list[TickerMappingRecord] = []
        for record_available, m in self._index.get(symbol, ()):
            if record_available > as_of:
                continue
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
