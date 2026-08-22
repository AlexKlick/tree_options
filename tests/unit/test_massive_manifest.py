"""M4-B: the unified Massive capture manifest — pin, reconcile, bind.

Hermetic: every capture directory here is synthetic, built under `tmp_path`
from small JSON files shaped like the bridge writes. Nothing touches the
wire, a key, or the live response cache.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tree_options.data.digest import canonical_bytes, sha256_hex
from tree_options.data.massive_manifest import (
    CAPTURE_MANIFEST_FILENAME,
    MASSIVE_MANIFEST_DOMAIN,
    MASSIVE_MANIFEST_SCHEMA_VERSION,
    MassiveCaptureManifest,
    MassiveManifestError,
    MasterEntry,
    build_massive_capture_manifest,
    load_massive_capture_manifest,
    verify_massive_capture_manifest,
)
from tree_options.data.massive_options import MASSIVE_PROVIDER

CAPTURE_VERSION = "m4b-capture/1"

# The bridge's plain-dict master accounting (loose mappings on purpose: the
# manifest module, not the caller, owns the typed shape).
MASTERS: tuple[dict[str, object], ...] = (
    {
        "underlying": "SPY",
        "as_of": "2025-03-14",
        "pages": 1,
        "rows": 3,
        "complete": True,
        "truncated": False,
        "error": None,
        "file": "SPY_2025-03-14.json",
    },
    {
        "underlying": "TSLA",
        "as_of": "2025-03-14",
        "pages": 2,
        "rows": 900,
        "complete": False,
        "truncated": True,
        "error": None,
        "file": "TSLA_2025-03-14.json",
    },
)

CLIENT_STATS: dict[str, int | float] = {
    "requests": 4,
    "cache_hits": 0,
    "pages_fetched": 3,
    "governor_slept_seconds": 24.0,
}


def write_capture(tmp_path: Path) -> Path:
    """A synthetic capture directory: two masters, one bar, one spot proxy."""
    capture = tmp_path / "capture"
    masters = capture / "masters"
    bars = capture / "bars"
    masters.mkdir(parents=True)
    bars.mkdir()
    (masters / "SPY_2025-03-14.json").write_text(
        '{"capture_version":"m4b-capture/1","pages":[{"results":[{"strike_price":587.5}]}]}',
        encoding="utf-8",
    )
    (masters / "TSLA_2025-03-14.json").write_text(
        '{"capture_version":"m4b-capture/1","pages":[{},{}]}\n', encoding="utf-8"
    )
    (bars / "O_SPY250417C00560000.json").write_text(
        '{"resultsCount":1,"results":[{"c":40.08}]}\n', encoding="utf-8"
    )
    (capture / "spot_proxy.json").write_text('{"SPY":{"2025-03-14":"587.5"}}\n', encoding="utf-8")
    return capture


def build_for(capture: Path) -> MassiveCaptureManifest:
    return build_massive_capture_manifest(
        capture,
        capture_version=CAPTURE_VERSION,
        budget_limit=45,
        requests_charged=4,
        client_stats=CLIENT_STATS,
        masters=MASTERS,
        bars=("O_SPY250417C00560000.json",),
        spot_proxy={"SPY": {"2025-03-14": "587.5"}},
        notes=("TSLA 2025-03-14: truncated at the page cap",),
    )


# ---- build pins every file -----------------------------------------------------


def test_manifest_pins_every_file_and_rehashes(tmp_path: Path) -> None:
    capture = write_capture(tmp_path)
    manifest = build_for(capture)

    assert [f.path for f in manifest.files] == [
        "bars/O_SPY250417C00560000.json",
        "masters/SPY_2025-03-14.json",
        "masters/TSLA_2025-03-14.json",
        "spot_proxy.json",
    ], "files[] is path-sorted"
    assert [f.kind for f in manifest.files] == ["bar", "master", "master", "spot_proxy"]
    for entry in manifest.files:
        raw = (capture / entry.path).read_bytes()
        assert entry.bytes == len(raw)
        assert entry.sha256 == sha256_hex(raw), "raw bytes, never a re-serialisation"

    assert manifest.provider == MASSIVE_PROVIDER
    assert manifest.schema_version == MASSIVE_MANIFEST_SCHEMA_VERSION
    assert manifest.masters == (
        MasterEntry(
            underlying="SPY",
            as_of="2025-03-14",
            pages=1,
            rows=3,
            complete=True,
            truncated=False,
            error=None,
            file="SPY_2025-03-14.json",
        ),
        manifest.masters[1],
    ), "loose mappings are coerced into typed entries"
    assert manifest.client_stats == CLIENT_STATS

    # Round-trip through the file the bridge writes; writing the manifest
    # INTO the capture directory also proves the manifest's own file is the
    # one exemption from disk reconciliation.
    target = capture / CAPTURE_MANIFEST_FILENAME
    target.write_text(manifest.model_dump_json(indent=2) + "\n", encoding="utf-8")
    loaded = load_massive_capture_manifest(target)
    assert loaded == manifest
    verify_massive_capture_manifest(loaded, capture, capture_version=CAPTURE_VERSION)


def test_loose_master_entries_are_refused_on_unknown_keys(tmp_path: Path) -> None:
    """A wrong shape is refused by name, never silently dropped."""
    capture = write_capture(tmp_path)
    stray_key = {**MASTERS[0], "pages_fetched": 3}
    with pytest.raises(MassiveManifestError, match="master entry 0") as exc:
        build_massive_capture_manifest(
            capture,
            capture_version=CAPTURE_VERSION,
            budget_limit=45,
            requests_charged=4,
            client_stats=CLIENT_STATS,
            masters=(stray_key, MASTERS[1]),
            bars=(),
            spot_proxy={},
            notes=(),
        )
    assert "pages_fetched" in str(exc.value), "the unknown key is named"
    wrong_shape = {**MASTERS[0], "rows": "many"}
    with pytest.raises(MassiveManifestError, match="master entry 0"):
        build_massive_capture_manifest(
            capture,
            capture_version=CAPTURE_VERSION,
            budget_limit=45,
            requests_charged=4,
            client_stats=CLIENT_STATS,
            masters=(wrong_shape,),
            bars=(),
            spot_proxy={},
            notes=(),
        )


# ---- verify: the capture files on disk -----------------------------------------


def test_verify_detects_a_tampered_capture_file(tmp_path: Path) -> None:
    capture = write_capture(tmp_path)
    manifest = build_for(capture)
    (capture / "masters" / "SPY_2025-03-14.json").write_text(
        '{"pages":[{"results":[{"strike_price":587.500000001}]}]}', encoding="utf-8"
    )
    with pytest.raises(MassiveManifestError, match=r"masters/SPY_2025-03-14\.json.*re-hash"):
        verify_massive_capture_manifest(manifest, capture, capture_version=CAPTURE_VERSION)


def test_verify_detects_a_deleted_capture_file(tmp_path: Path) -> None:
    capture = write_capture(tmp_path)
    manifest = build_for(capture)
    (capture / "bars" / "O_SPY250417C00560000.json").unlink()
    with pytest.raises(MassiveManifestError, match=r"O_SPY250417C00560000\.json.*missing"):
        verify_massive_capture_manifest(manifest, capture, capture_version=CAPTURE_VERSION)


def test_verify_detects_an_unlisted_file_on_disk(tmp_path: Path) -> None:
    capture = write_capture(tmp_path)
    manifest = build_for(capture)
    extra = capture / "masters" / "SPY_2025-09-15.json"
    extra.write_text('{"pages":[]}\n', encoding="utf-8")
    with pytest.raises(MassiveManifestError, match=r"SPY_2025-09-15\.json"):
        verify_massive_capture_manifest(manifest, capture, capture_version=CAPTURE_VERSION)

    # A stray *.json at the capture root is equally unprovenance.
    extra.unlink()
    (capture / "unlisted.json").write_text("{}\n", encoding="utf-8")
    with pytest.raises(MassiveManifestError, match=r"unlisted\.json"):
        verify_massive_capture_manifest(manifest, capture, capture_version=CAPTURE_VERSION)


# ---- the content binding --------------------------------------------------------


def test_content_sha256_is_domain_separated_and_self_binding(tmp_path: Path) -> None:
    capture = write_capture(tmp_path)
    manifest = build_for(capture)
    verify_massive_capture_manifest(manifest, capture, capture_version=CAPTURE_VERSION)

    core = manifest.model_copy(update={"content_sha256": ""})
    assert manifest.content_sha256 != sha256_hex(canonical_bytes(core)), (
        "the domain bytes must participate — a bare body hash is replayable"
    )
    assert manifest.content_sha256 == sha256_hex(MASSIVE_MANIFEST_DOMAIN + canonical_bytes(core)), (
        "the domain is the pinned module constant"
    )

    # Flip one row count WITHOUT recomputing the binding: no file changed on
    # disk and every token still matches, so only content_sha256 catches it.
    flipped = manifest.masters[0].model_copy(update={"rows": manifest.masters[0].rows + 1})
    tampered = manifest.model_copy(update={"masters": (flipped, *manifest.masters[1:])})
    with pytest.raises(MassiveManifestError, match="content_sha256"):
        verify_massive_capture_manifest(tampered, capture, capture_version=CAPTURE_VERSION)


# ---- token pinning ---------------------------------------------------------------


def test_schema_and_provider_tokens_are_refused_on_mismatch(tmp_path: Path) -> None:
    capture = write_capture(tmp_path)
    manifest = build_for(capture)

    wrong_schema = manifest.model_copy(update={"schema_version": "m4b-manifest/9"})
    with pytest.raises(MassiveManifestError, match=r"schema_version .*m4b-manifest/9"):
        verify_massive_capture_manifest(wrong_schema, capture, capture_version=CAPTURE_VERSION)

    wrong_provider = manifest.model_copy(update={"provider": "other-vendor/1"})
    with pytest.raises(MassiveManifestError, match=r"provider .*other-vendor/1"):
        verify_massive_capture_manifest(wrong_provider, capture, capture_version=CAPTURE_VERSION)

    with pytest.raises(MassiveManifestError, match=r"capture_version .*m4b-capture/9"):
        verify_massive_capture_manifest(manifest, capture, capture_version="m4b-capture/9")


def test_load_refuses_a_manifest_that_is_not_the_pinned_shape(tmp_path: Path) -> None:
    capture = write_capture(tmp_path)
    payload = json.loads(build_for(capture).model_dump_json())

    missing = capture / "capture_manifest.json"
    with pytest.raises(MassiveManifestError, match="unreadable"):
        load_massive_capture_manifest(missing)

    not_json = capture / "not_json.json"
    not_json.write_text("not json", encoding="utf-8")
    with pytest.raises(MassiveManifestError, match="not JSON"):
        load_massive_capture_manifest(not_json)

    legacy = capture / "legacy.json"
    legacy.write_text(
        json.dumps({**payload, "spot_proxy_file": "spot_proxy.json"}), encoding="utf-8"
    )
    with pytest.raises(MassiveManifestError, match="pinned shape"):
        load_massive_capture_manifest(legacy)
