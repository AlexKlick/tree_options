"""Bounded atomic publication, and strictly read-only HTTP consumption."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from tree_options.research.experiments.contracts import Index, encoded

MAX_BYTES = 8_000_000
MAX_AGE_SECONDS = 120


class Unavailable(ValueError):
    pass


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise Unavailable("duplicate_json_key")
        out[key] = value
    return out


def load(path: Path) -> tuple[dict[str, Any], str]:
    with path.open("rb") as stream:
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise Unavailable("artifact_size_limit")
    doc = json.loads(raw, object_pairs_hook=_unique)
    if not isinstance(doc, dict):
        raise Unavailable("artifact_not_object")
    return doc, hashlib.sha256(raw).hexdigest()


def publish(index: Index, path: Path) -> None:
    body = index.model_dump()
    raw = encoded({"payload": body, "sha256": hashlib.sha256(encoded(body)).hexdigest()})
    if len(raw) > MAX_BYTES:
        raise Unavailable("index_size_limit")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=".experiment-index-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(name)


def read(path: Path, now: datetime) -> Index:
    try:
        doc, _ = load(path)
        if set(doc) != {"payload", "sha256"}:
            raise Unavailable("index_envelope_invalid")
        if hashlib.sha256(encoded(doc["payload"])).hexdigest() != doc["sha256"]:
            raise Unavailable("index_digest_mismatch")
        index = Index.model_validate(doc["payload"])
        age = (now - datetime.fromisoformat(index.published_at)).total_seconds()
        if age < 0:
            raise Unavailable("index_from_future")
        if age > MAX_AGE_SECONDS:
            raise Unavailable("index_stale")
        return index
    except Unavailable:
        raise
    except FileNotFoundError:
        raise Unavailable("index_missing") from None
    except (OSError, ValueError, KeyError, TypeError):
        raise Unavailable("index_invalid") from None
