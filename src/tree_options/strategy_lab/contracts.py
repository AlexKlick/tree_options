"""Small immutable contracts shared by research-only strategy primitives."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from types import MappingProxyType
from typing import Any

from tree_options.research.contracts import StrategyDefinition as StrategyDefinition


def freeze_metadata(value: Any) -> Any:
    """Detach and recursively freeze JSON metadata; refuse opaque mutable objects."""
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("metadata object keys must be strings")
        return MappingProxyType({key: freeze_metadata(item) for key, item in value.items()})
    if isinstance(value, list | tuple):
        return tuple(freeze_metadata(item) for item in value)
    if value is None or isinstance(value, str | bool | int):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise ValueError("metadata must contain finite JSON-compatible values")


def metadata_json(value: Any) -> Any:
    """Return detached JSON containers for canonical hashing/export."""
    if isinstance(value, Mapping):
        return {key: metadata_json(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [metadata_json(item) for item in value]
    return value


def require_utc(value: datetime, *, field_name: str = "timestamp") -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class Observation:
    """One source observation with explicit information availability.

    ``event_at`` is when the underlying fact/event happened. ``available_at``
    is when this observation was first usable by the strategy. Those two times
    are intentionally distinct.
    """

    entity_id: str
    event_at: datetime
    available_at: datetime
    values: Mapping[str, Decimal]
    source: str
    source_id: str
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_at", require_utc(self.event_at, field_name="event_at"))
        object.__setattr__(
            self,
            "available_at",
            require_utc(self.available_at, field_name="available_at"),
        )
        if self.available_at < self.event_at:
            raise ValueError("available_at must be >= event_at for an observed fact")
        if not self.entity_id or not self.source or not self.source_id:
            raise ValueError("entity_id, source and source_id must be non-empty")
        object.__setattr__(self, "values", MappingProxyType(dict(self.values)))
        object.__setattr__(self, "metadata", freeze_metadata(self.metadata))
        for key, value in self.values.items():
            if not key:
                raise ValueError("observation value key must be non-empty")
            if not isinstance(value, Decimal) or not value.is_finite():
                raise ValueError(f"{key} must be a finite Decimal")


@dataclass(frozen=True, slots=True)
class StrategyScore:
    entity_id: str
    score: Decimal
    components: Mapping[str, Decimal] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TargetWeight:
    entity_id: str
    weight: Decimal
