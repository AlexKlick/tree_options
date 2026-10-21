"""Shared primitives for the Wave 2 contracts: ids, instants, canonical bytes.

Canonical bytes are THE encoding every hash in Wave 2 is taken over: the
pydantic JSON-mode dump by alias, then sorted-key compact JSON
(``action_graph.proposal.canonical_bytes``, the same function the option
``supervised-effect/1`` already hashes through). Instants normalize to UTC
on validation, so the same instant written with any offset hashes the same.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Annotated, Any

from pydantic import AfterValidator, AwareDatetime, BaseModel, Field, StringConstraints

from tree_options.action_graph.proposal import canonical_bytes
from tree_options.trex.supervised import Sha256Hex

__all__ = [
    "IdempotencyKey",
    "PathSafeId",
    "Reason",
    "Sha256Hex",
    "Symbol",
    "UTCInstant",
    "canonical_bytes",
    "model_bytes",
    "model_document",
    "sha256_hex",
]


def _to_utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


#: A timezone-aware instant (naive input is refused), normalized to UTC.
UTCInstant = Annotated[AwareDatetime, AfterValidator(_to_utc)]

#: An id that is also a file name (``mandates/<intent_id>.json``): no path
#: separators, no leading dot, bounded length.
PathSafeId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")]

#: A lowercase canonical UUID string (any version); also a file name.
IdempotencyKey = Annotated[
    str,
    StringConstraints(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"),
]

#: An exchange ticker as IBKR's ``Stock.symbol`` spells it (upper case).
Symbol = Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9.]{0,9}$")]

#: Free operator text carried by a command; bounded, may be empty.
Reason = Annotated[str, Field(max_length=500)]


def model_document(model: BaseModel) -> dict[str, Any]:
    """The JSON-mode dump by alias: the document every hash is taken over."""
    return model.model_dump(mode="json", by_alias=True)


def model_bytes(model: BaseModel) -> bytes:
    """Canonical bytes of a contract model (sorted keys, compact, UTF-8)."""
    return canonical_bytes(model_document(model))


def sha256_hex(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()
