"""Options manifests (M3 plan §3.B): immutable, deterministic lineage.

The options lane is a PARALLEL manifest, not an extension of the equity
`DatasetSnapshot`: the equity manifest is scoped to {master, bars, actions}
under schema m2/1 and stays byte-identical. An OptionsManifest binds the
overlay's spec, the generating-code pin, the PARENT equity world's
content_sha256 (lineage pairing), the analytic contract count, and the
deterministic anchor-slice hashes that pin the realization byte-exactly
(full materialization is infeasible by design — plan §7).

The paired dataset hash used by trials is sha256 over the JSON pair
[equity content hash, options content hash].
"""

from __future__ import annotations

import hashlib
import json
from datetime import date

from pydantic import NonNegativeInt

from tree_options.data.digest import canonical_bytes
from tree_options.schemas.common import IdStr, StrictModel
from tree_options.synth_options import GeneratedOptionOverlay, OptionsOverlaySpec

OPTIONS_MANIFEST_SCHEMA_VERSION = "m3/1"
OPTIONS_CONTENT_DOMAIN = b"tree-options-m3-options-v1"
OPTIONS_PROVIDER = "synthetic-options/v2"


class OptionsManifest(StrictModel):
    snapshot_id: IdStr  # == the parent equity world id
    provider: IdStr = OPTIONS_PROVIDER
    schema_version: str = OPTIONS_MANIFEST_SCHEMA_VERSION
    overlay_spec: OptionsOverlaySpec
    synth_options_code_sha: str
    parent_content_sha256: str
    contract_count: NonNegativeInt
    sample_slice_hashes: tuple[tuple[str, str, str], ...]  # (sid, session, sha256)
    content_sha256: str


def build_options_manifest(
    overlay: GeneratedOptionOverlay,
    *,
    parent_content_sha256: str,
    synth_options_code_sha: str,
) -> OptionsManifest:
    """Pure function of (overlay realization summary, parent hash, code pin).

    The anchor slices pin the realization; contract_count is the cheap
    analytic commitment (entry/quote-event counts stay with the registry
    verifier, which already pays the full enumeration)."""
    slices = tuple(
        (
            sid,
            session.isoformat(),
            hashlib.sha256(overlay.canonical_file_bytes(sid, session)).hexdigest(),
        )
        for sid, session in overlay.anchor_slices()
    )
    core = OptionsManifest(
        snapshot_id=overlay.spec.world_id,
        synth_options_code_sha=synth_options_code_sha,
        parent_content_sha256=parent_content_sha256,
        contract_count=overlay.contract_count(),
        sample_slice_hashes=slices,
        content_sha256="",
        overlay_spec=overlay.spec,
    )
    digest = hashlib.sha256()
    digest.update(OPTIONS_CONTENT_DOMAIN)
    digest.update(canonical_bytes(core.model_copy(update={"content_sha256": ""})))
    return core.model_copy(update={"content_sha256": digest.hexdigest()})


def paired_dataset_hash(equity_content_sha256: str, options_content_sha256: str) -> str:
    """The trial-side dataset_manifest_hash: sha256 over the JSON pair."""
    payload = json.dumps([equity_content_sha256, options_content_sha256], separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def file_sessions_of(slices: tuple[tuple[str, str, str], ...]) -> tuple[date, ...]:
    """The distinct anchor session dates (inspection helper)."""
    return tuple(dict.fromkeys(date.fromisoformat(s) for _, s, _ in slices))
