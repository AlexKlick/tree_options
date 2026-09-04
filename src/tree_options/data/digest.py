"""Canonical serialization + hashing: the M1 provenance primitive.

Every source-row hash and manifest content hash flows through here, so
canonicalization is a protocol surface: sorted keys, compact separators,
exact decimal strings, ISO timestamps. Two serializations of the same
model are byte-identical by construction.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel


def _default(o: Any) -> str:
    if isinstance(o, Decimal):
        return str(o)
    if isinstance(o, (date, datetime)):
        return o.isoformat()
    raise TypeError(f"not canonically serializable: {type(o)}")


def canonical_bytes(model: BaseModel, *, exclude: set[str] | None = None) -> bytes:
    """Canonical bytes of a model's dump.

    ``exclude`` drops field names from the preimage entirely (never writes
    them as null) — the additive hook behind pre-field hash parity: a model
    that GREW a ``None``-defaulted field must hash exactly as it did before
    the field existed, or every manifest hashed by the older algorithm stops
    verifying (Codex round-1 finding 3, 2026-09-04). ``None`` (the default)
    keeps the historical byte-for-byte behavior for every other caller.
    """
    return json.dumps(
        model.model_dump(exclude=exclude),
        sort_keys=True,
        separators=(",", ":"),
        default=_default,
    ).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
