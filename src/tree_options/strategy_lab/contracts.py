"""Small immutable contracts shared by research-only strategy primitives."""

from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from types import MappingProxyType
from typing import Any


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
        object.__setattr__(self, "metadata", MappingProxyType(copy.deepcopy(dict(self.metadata))))
        for key, value in self.values.items():
            if not key:
                raise ValueError("observation value key must be non-empty")
            if not isinstance(value, Decimal) or not value.is_finite():
                raise ValueError(f"{key} must be a finite Decimal")


@dataclass(frozen=True, slots=True)
class StrategyDefinition:
    strategy_id: str
    version: str
    family: str
    registration: str
    required_inputs: tuple[str, ...]
    description: str
    data_status: str = "supported"
    parameters: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class StrategyScore:
    entity_id: str
    score: Decimal
    components: Mapping[str, Decimal] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TargetWeight:
    entity_id: str
    weight: Decimal
