"""Provider-neutral social/alternative-data ranking."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, localcontext

from tree_options.strategy_lab.contracts import require_utc
from tree_options.strategy_lab.ranking import percentile_scores


@dataclass(frozen=True, slots=True)
class SentimentObservation:
    entity_id: str
    event_at: datetime
    available_at: datetime
    likes: int
    comments: int
    source: str
    source_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_at", require_utc(self.event_at, field_name="event_at"))
        object.__setattr__(
            self, "available_at", require_utc(self.available_at, field_name="available_at")
        )
        if self.available_at < self.event_at:
            raise ValueError("available_at must be >= event_at for an observed social event")
        if self.likes < 0 or self.comments < 0:
            raise ValueError("engagement counts must be non-negative")


def engagement_ratio(observation: SentimentObservation) -> Decimal | None:
    if observation.likes == 0:
        return None
    with localcontext() as ctx:
        ctx.prec = 40
        return Decimal(observation.comments) / Decimal(observation.likes)


def aggregate_engagement(
    observations: Iterable[SentimentObservation],
    *,
    decision_at: datetime,
    min_likes: int = 20,
    min_comments: int = 10,
) -> dict[str, Decimal]:
    cutoff = require_utc(decision_at, field_name="decision_at")
    buckets: dict[str, list[Decimal]] = {}
    for row in observations:
        if row.available_at > cutoff or row.likes <= min_likes or row.comments <= min_comments:
            continue
        ratio = engagement_ratio(row)
        if ratio is not None:
            buckets.setdefault(row.entity_id, []).append(ratio)
    return {
        entity_id: sum(values, Decimal(0)) / Decimal(len(values))
        for entity_id, values in buckets.items()
    }


def sentiment_percentiles(*args, **kwargs) -> dict[str, Decimal]:
    return percentile_scores(aggregate_engagement(*args, **kwargs), higher_is_better=True)
