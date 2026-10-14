"""Owner-published evidence projection. Readers never open the evidence DB.

The publisher builds both views from one verified SQLite backup and replaces a
single JSON file after fsync. Failure leaves the preceding snapshot intact; a
reader enforces freshness and never falls back to a writable database.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from tree_options.desk import production, scorecards
from tree_options.desk.evidence import EvidenceError, EvidenceStore
from tree_options.desk.sessions import Calendar

SCHEMA = "desk-read-snapshot/1"
MAX_AGE_SECONDS = 120
MAX_BYTES = 10_000_000


def _encoded(doc: dict[str, Any]) -> bytes:
    return json.dumps(doc, sort_keys=True, allow_nan=False, separators=(",", ":")).encode()


def validate(doc: Any) -> dict[str, Any]:
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA:
        raise EvidenceError("snapshot_schema_invalid")
    payload = doc.get("payload")
    if not isinstance(payload, dict) or set(payload) != {"health", "scorecards"}:
        raise EvidenceError("snapshot_payload_invalid")
    if hashlib.sha256(_encoded(payload)).hexdigest() != doc.get("payload_sha256"):
        raise EvidenceError("snapshot_digest_mismatch")
    health, cards = payload["health"], payload["scorecards"]
    for part, schema in ((health, "desk-health/2"), (cards, "desk-scorecards/2")):
        if not isinstance(part, dict) or part.get("schema") != schema:
            raise EvidenceError("snapshot_payload_schema_invalid")
        if part.get("execution_enabled") is not False:
            raise EvidenceError("snapshot_authority_invalid")
    audit = health.get("audit")
    if not isinstance(audit, dict) or audit.get("ok") is not True or audit != cards.get("audit"):
        raise EvidenceError("snapshot_audit_mismatch")
    if cards.get("promotion_ready") is not False or not isinstance(cards.get("families"), list):
        raise EvidenceError("snapshot_scorecards_invalid")
    if health.get("evidence_status") not in {
        "ready",
        "degraded",
        "awaiting_publication",
    } or not isinstance(health.get("evidence_blockers"), list):
        raise EvidenceError("snapshot_health_invalid")
    if health["evidence_status"] == "ready" and health["evidence_blockers"]:
        raise EvidenceError("snapshot_health_invalid")
    at = datetime.fromisoformat(doc["published_at"])
    if at.tzinfo is None or health.get("as_of") != doc["published_at"]:
        raise EvidenceError("snapshot_time_invalid")
    if not isinstance(doc.get("source_release"), str) or not doc["source_release"]:
        raise EvidenceError("snapshot_release_missing")
    return doc


def publish(
    *, database: Path, out: Path, now: datetime, cal: Calendar, source_release: str
) -> dict[str, Any]:
    if now.tzinfo is None:
        raise EvidenceError("snapshot_time_invalid")
    if out.resolve() == database.resolve():
        raise EvidenceError("snapshot_overwrites_database")
    out.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix=".evidence-view-", dir=out.parent) as scratch:
        backup = Path(scratch) / "verified.sqlite3"
        with EvidenceStore(database, readonly=True) as source:
            source.backup(backup)
        payload = {
            "health": production.health(database=backup, now=now, cal=cal),
            "scorecards": scorecards.build_scorecards(backup),
        }
    doc = validate(
        {
            "schema": SCHEMA,
            "published_at": now.isoformat(),
            "source_release": source_release,
            "evidence_class": "modeled_shadow",
            "payload": payload,
            "payload_sha256": hashlib.sha256(_encoded(payload)).hexdigest(),
        }
    )
    data = _encoded(doc)
    if len(data) > MAX_BYTES:
        raise EvidenceError("snapshot_too_large")
    fd, name = tempfile.mkstemp(prefix=".evidence-view-", dir=out.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, out)
        directory = os.open(out.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(name)
    return doc


def read(path: Path, *, now: datetime) -> dict[str, Any]:
    try:
        with path.open("rb") as stream:
            data = stream.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise EvidenceError("snapshot_too_large")
        doc = validate(json.loads(data))
        age = (now - datetime.fromisoformat(doc["published_at"])).total_seconds()
        if not 0 <= age <= MAX_AGE_SECONDS:
            raise EvidenceError("snapshot_stale" if age > 0 else "snapshot_from_future")
        return doc
    except FileNotFoundError:
        raise EvidenceError("snapshot_missing") from None
    except (ValueError, TypeError, KeyError):
        raise EvidenceError("snapshot_invalid") from None
