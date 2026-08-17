"""Shared schema primitives: tz-aware timestamps, Decimal money, id types."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, StringConstraints

IdStr = Annotated[str, StringConstraints(min_length=1, strip_whitespace=True)]

PRICE_TICK = Decimal("0.01")
FEE_TICK = Decimal("0.01")


class NaiveTimestampError(ValueError):
    """Naive datetimes are rejected everywhere (protocol: clock=utc)."""


def _require_utc(v: datetime) -> datetime:
    if not isinstance(v, datetime):
        raise NaiveTimestampError(f"expected datetime, got {type(v)}")
    if v.tzinfo is None:
        raise NaiveTimestampError(f"naive datetime rejected: {v!r}")
    return v.astimezone(UTC)


UTCDatetime = Annotated[datetime, BeforeValidator(_require_utc)]

Price = Annotated[Decimal, Field(gt=0, decimal_places=2, max_digits=12)]
Money = Annotated[Decimal, Field(decimal_places=2, max_digits=18)]


class StrictModel(BaseModel):
    """Base: frozen, no extra keys — unknown fields are defects, not features."""

    model_config = ConfigDict(extra="forbid", frozen=True)
