"""Point-in-time selection primitives.

The rule is intentionally simple and hard: an observation with
``available_at > decision_at`` is future information and cannot enter a run.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from tree_options.strategy_lab.contracts import Observation, require_utc


class PointInTimeViolation(ValueError):
    pass


def require_available[T: Observation](observation: T, *, decision_at: datetime) -> T:
    cutoff = require_utc(decision_at, field_name="decision_at")
    if observation.available_at > cutoff:
        raise PointInTimeViolation(
            f"{observation.entity_id}/{observation.source_id} became available at "
            f"{observation.available_at.isoformat()} after decision "
            f"{cutoff.isoformat()}"
        )
    return observation


def available_only[T: Observation](
    observations: Iterable[T], *, decision_at: datetime
) -> tuple[T, ...]:
    cutoff = require_utc(decision_at, field_name="decision_at")
    return tuple(item for item in observations if item.available_at <= cutoff)


def latest_available[T: Observation](
    observations: Iterable[T], *, entity_id: str, decision_at: datetime
) -> T | None:
    """Latest known observation for one entity at one decision cut.

    Ties are refused rather than resolved by iteration order unless they are
    the exact same source identity.
    """

    candidates = [
        item
        for item in available_only(observations, decision_at=decision_at)
        if item.entity_id == entity_id
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item.available_at, item.event_at, item.source_id))
    best = candidates[-1]
    tied = [
        item
        for item in candidates
        if (item.available_at, item.event_at) == (best.available_at, best.event_at)
    ]
    identities = {(item.source, item.source_id) for item in tied}
    if len(identities) > 1:
        raise PointInTimeViolation(
            f"ambiguous latest observation for {entity_id}: {sorted(identities)}"
        )
    return best
