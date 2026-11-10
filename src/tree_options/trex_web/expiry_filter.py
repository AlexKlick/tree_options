"""Bounded or exact-expiration selection for TREX recorded/delayed chains.

A requested canonical ISO date bypasses nearest-N truncation. This does not
prove exchange listing, market entitlement, liquidity, or strike existence.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Mapping


def select_expiries(
    expiries: Mapping[str, Any], *, max_expiries: int,
    expiration: str | None = None, compact: bool = False,
) -> tuple[str, ...]:
    """Return only an observed exact expiry or the existing nearest-N keys.

    `expiration` uses YYYY-MM-DD. The delayed live cache uses YYYYMMDD keys
    when `compact=True`. Missing keys remain missing, never zero quotes.
    """
    if not isinstance(max_expiries, int) or isinstance(max_expiries, bool) or max_expiries < 1:
        raise ValueError("max_expiries must be a positive integer")
    if expiration is not None:
        if not isinstance(expiration, str):
            raise ValueError("expiration must be YYYY-MM-DD")
        try:
            if date.fromisoformat(expiration).isoformat() != expiration:
                raise ValueError("noncanonical expiration")
        except ValueError as exc:
            raise ValueError("expiration must be canonical YYYY-MM-DD") from exc
        key = expiration.replace("-", "") if compact else expiration
        return (key,) if key in expiries else ()
    return tuple(sorted(expiries)[:max_expiries])
