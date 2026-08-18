"""Dataset manifests (M1 packet workstream D): immutable, deterministic.

A manifest is a PURE function of (payload content, normalization code
SHA, exclusions) — never of wall-clock time. The retrieval instant comes
from the payload itself, so two clean ingestions of the same source
produce byte-identical manifests. content_sha256 binds the manifest to
the normalized rows; swapping a row after ingest is detectable.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from datetime import date

from pydantic import NonNegativeInt

from tree_options.data.actions import CorporateActionRecord
from tree_options.data.bars import BarRecord
from tree_options.data.digest import canonical_bytes
from tree_options.schemas.common import IdStr, StrictModel, UTCDatetime

MANIFEST_SCHEMA_VERSION = "m1/1"
CONTENT_DOMAIN = b"tree-options-m1-content-v1"


def content_sha256(bars: Iterable[BarRecord], actions: Iterable[CorporateActionRecord]) -> str:
    digest = hashlib.sha256()
    digest.update(CONTENT_DOMAIN)
    for bar in sorted(bars, key=lambda r: (r.security_id, r.session, r.source_record_id)):
        digest.update(canonical_bytes(bar))
    for action in sorted(
        actions, key=lambda r: (r.security_id, r.effective_session, r.source_record_id)
    ):
        digest.update(canonical_bytes(action))
    return digest.hexdigest()


class DatasetManifest(StrictModel):
    provider: IdStr
    snapshot_id: IdStr
    schema_version: str = MANIFEST_SCHEMA_VERSION
    normalization_code_sha: str
    retrieved_at: UTCDatetime | None
    known_exclusions: tuple[str, ...] = ()
    source_row_count: NonNegativeInt
    bar_count: NonNegativeInt
    action_count: NonNegativeInt
    security_count: NonNegativeInt
    session_coverage: tuple[date, date] | None
    source_row_hashes: tuple[str, ...]
    content_sha256: str
