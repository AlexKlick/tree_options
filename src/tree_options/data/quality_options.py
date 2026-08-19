"""Options-manifest verification (M3 plan §3.B).

Fail-closed checks over the (manifest, overlay, parent) triple:
- identity binding (snapshot id, provider, schema version);
- lineage pairing (parent equity content hash) and the generating-code pin;
- spec equality (the manifest describes THIS overlay);
- anchor-slice re-hash — the realization pin (cheap; full materialization
  is infeasible, so slices carry the byte-exactness burden);
- content_sha256 self-binding (the manifest cannot lie about itself).

The ANALYTIC COUNTS (contract/entry/quote-event) are owned by the registry
verifier (`scripts/verify_options_worlds.py`), which already pays the full
enumeration; this verifier re-hashes anchors only, so trial-time checks
stay cheap. That split is declared here, not accidental.
"""

from __future__ import annotations

import hashlib

from tree_options.data.digest import canonical_bytes
from tree_options.data.options_manifest import (
    OPTIONS_CONTENT_DOMAIN,
    OPTIONS_MANIFEST_SCHEMA_VERSION,
    OPTIONS_PROVIDER,
    OptionsManifest,
)
from tree_options.synth_options import GeneratedOptionOverlay


class OptionsManifestError(RuntimeError):
    pass


def verify_options_manifest(
    manifest: OptionsManifest,
    overlay: GeneratedOptionOverlay,
    *,
    parent_content_sha256: str,
    synth_options_code_sha: str,
) -> None:
    def fail(detail: str) -> None:
        raise OptionsManifestError(f"options manifest for {manifest.snapshot_id}: {detail}")

    if manifest.schema_version != OPTIONS_MANIFEST_SCHEMA_VERSION:
        fail(f"schema {manifest.schema_version} != {OPTIONS_MANIFEST_SCHEMA_VERSION}")
    if manifest.provider != OPTIONS_PROVIDER:
        fail(f"provider {manifest.provider} != {OPTIONS_PROVIDER}")
    if manifest.snapshot_id != overlay.spec.world_id:
        fail(f"snapshot id {manifest.snapshot_id} != overlay world {overlay.spec.world_id}")
    if manifest.overlay_spec != overlay.spec:
        fail("overlay spec does not match the overlay")
    if manifest.parent_content_sha256 != parent_content_sha256:
        fail("parent equity content hash mismatch")
    if manifest.synth_options_code_sha != synth_options_code_sha:
        fail("synth_options code pin mismatch")

    expected_slices = overlay.anchor_slices()
    recorded = [(sid, session.isoformat()) for sid, session in expected_slices]
    if [(sid, s) for sid, s, _h in manifest.sample_slice_hashes] != recorded:
        fail("anchor-slice selection drifted (anchors are deterministic)")
    for (sid, session), (_sid2, _s2, recorded_hash) in zip(
        expected_slices, manifest.sample_slice_hashes, strict=True
    ):
        recomputed = hashlib.sha256(overlay.canonical_file_bytes(sid, session)).hexdigest()
        if recomputed != recorded_hash:
            fail(f"anchor slice {sid}@{session} hash mismatch: tampered realization")

    core = manifest.model_copy(update={"content_sha256": ""})
    digest = hashlib.sha256()
    digest.update(OPTIONS_CONTENT_DOMAIN)
    digest.update(canonical_bytes(core))
    if digest.hexdigest() != manifest.content_sha256:
        fail("content_sha256 does not bind the manifest body")
