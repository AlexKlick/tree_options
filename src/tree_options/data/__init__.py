"""M1 point-in-time data authority: raw payloads, ingestion, manifests,
resolution, quality gates (handoff §6 + M1 packet workstreams B-E)."""

from tree_options.data.actions import (
    ActionKind,
    CorporateActionRecord,
    RawActionRow,
)
from tree_options.data.bars import BarRecord, RawBarRow
from tree_options.data.digest import canonical_bytes, sha256_hex
from tree_options.data.ingest import DatasetSnapshot, IngestionError, ingest_snapshot
from tree_options.data.manifest import DatasetManifest, content_sha256
from tree_options.data.quality import DataQualityError, validate_snapshot, verify_manifest
from tree_options.data.raw import RawPayload, build_payload
from tree_options.data.resolve import (
    AmbiguousTickerError,
    TickerResolver,
    UnknownTickerError,
)

__all__ = [
    "ActionKind",
    "AmbiguousTickerError",
    "BarRecord",
    "CorporateActionRecord",
    "DataQualityError",
    "DatasetManifest",
    "DatasetSnapshot",
    "IngestionError",
    "RawActionRow",
    "RawBarRow",
    "RawPayload",
    "TickerResolver",
    "UnknownTickerError",
    "build_payload",
    "canonical_bytes",
    "content_sha256",
    "ingest_snapshot",
    "sha256_hex",
    "validate_snapshot",
    "verify_manifest",
]
