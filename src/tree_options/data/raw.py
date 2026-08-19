"""Raw vendor payloads: immutable input objects for ingestion.

The payload carries the vendor's own retrieval instant (not the ingest
machine's clock) so manifests stay deterministic across re-ingestions.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from tree_options.data.actions import RawActionRow
from tree_options.data.bars import RawBarRow
from tree_options.schemas.common import IdStr, StrictModel, UTCDatetime

RawRow = RawBarRow | RawActionRow


class RawPayload(StrictModel):
    provider: IdStr
    retrieved_at: UTCDatetime | None
    bars: tuple[RawBarRow, ...]
    actions: tuple[RawActionRow, ...]
    known_exclusions: tuple[str, ...] = ()

    @property
    def rows(self) -> tuple[RawRow, ...]:
        return (*self.bars, *self.actions)


def build_payload(
    *,
    provider: str,
    rows: Iterable[dict[str, object]],
    retrieved_at: datetime | None = None,
    known_exclusions: tuple[str, ...] = (),
) -> RawPayload:
    """Split vendor-shaped dicts into validated raw row models.

    A row carrying `kind` is an action row; anything else is a bar row.
    Validation (OHLC coherence, non-negative volume, kind consistency,
    naive-timestamp rejection) happens HERE, at the raw boundary.
    """
    bars: list[RawBarRow] = []
    actions: list[RawActionRow] = []
    for row in rows:
        if "kind" in row:
            actions.append(RawActionRow(**row))  # type: ignore[arg-type]
        else:
            bars.append(RawBarRow(**row))  # type: ignore[arg-type]
    return RawPayload(
        provider=provider,
        retrieved_at=retrieved_at,
        bars=tuple(bars),
        actions=tuple(actions),
        known_exclusions=known_exclusions,
    )
