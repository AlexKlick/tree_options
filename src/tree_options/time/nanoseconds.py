"""Exact integer provider-time conversion, retaining sub-microsecond residue."""

from calendar import timegm
from datetime import UTC, datetime


def utc_nanoseconds(value: datetime) -> int:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("aware timestamp required")
    utc = value.astimezone(UTC)
    return timegm(utc.utctimetuple()) * 1000000000 + utc.microsecond * 1000


def from_nanoseconds(value: int) -> datetime:
    if type(value) is not int or value <= 0:
        raise ValueError("positive integer nanoseconds required")
    seconds, residue = divmod(value, 1000000000)
    # datetime has microsecond precision. Floor conservatively for freshness;
    # callers retain the original integer nanoseconds as the authoritative fact.
    return datetime.fromtimestamp(seconds, UTC).replace(microsecond=residue // 1000)
