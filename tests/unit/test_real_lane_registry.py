"""PR A riders: the G3 Ask C real-lane registry entries (PENDING-era).

`real_lanes` is a NEW TOP-LEVEL key in both world registries. It is never
nested inside `worlds` (data/worlds/registry.json) or `overlays`
(data/worlds/options_registry.json) — those exact compositions are pinned by
test_world_registry.py / test_options_world_registry.py and are mirrored
here so this file fails loudly if the additive key disturbed the pinned
shape. Token drift is checked BOTH ways: the entry must equal the src
constants, so renaming either side fails.

While a lane is `PENDING-era` its `capture_manifest_sha256` is null BY RULE
(legal only in that state) — the closeout PR must pin both the status and
the capture hash together.
"""

from __future__ import annotations

import json
from typing import Any

from tests.conftest import REPO_ROOT
from tree_options.data.coverage_census import CoverageUniverse, verify_universe
from tree_options.data.massive_client import MASSIVE_PROVIDER
from tree_options.data.massive_manifest import MASSIVE_MANIFEST_SCHEMA_VERSION
from tree_options.data.massive_options import MASSIVE_SCHEMA_VERSION
from tree_options.data.massive_overlay import (
    _KNOWN_CAPTURE_VERSIONS,
    MASSIVE_DERIVED_PROVIDER,
)

REGISTRY_PATH = REPO_ROOT / "data" / "worlds" / "registry.json"
OPTIONS_REGISTRY_PATH = REPO_ROOT / "data" / "worlds" / "options_registry.json"

# Mirrored from test_world_registry.py (the exact frozen composition) —
# the real_lanes addition must not have disturbed any of it.
PINNED_DEV_POOL = [
    (101, "null", "synth-v1-dev-null-101"),
    (102, "alpha", "synth-v1-dev-alpha-102"),
    (103, "null", "synth-v1-dev-null-103"),
    (104, "alpha", "synth-v1-dev-alpha-104"),
]
PINNED_VALIDATION_POOL = [
    (701, "null", "synth-v1-val-null-701"),
    (702, "null", "synth-v1-val-null-702"),
    (703, "null", "synth-v1-val-null-703"),
    (704, "alpha", "synth-v1-val-alpha-704"),
    (705, "alpha", "synth-v1-val-alpha-705"),
    (706, "alpha", "synth-v1-val-alpha-706"),
    (707, "alpha", "synth-v1-val-alpha-707"),
    (708, "alpha", "synth-v1-val-alpha-708"),
    (709, "alpha", "synth-v1-val-alpha-709"),
    (710, "alpha", "synth-v1-val-alpha-710"),
    (711, "alpha", "synth-v1-val-alpha-711"),
]
# Mirrored from test_options_world_registry.py.
PINNED_OVERLAYS = [
    ("synth-v1-dev-null-101", "dev"),
    ("synth-v1-dev-alpha-102", "dev"),
    ("synth-v1-dev-null-103", "dev"),
    ("synth-v1-dev-alpha-104", "dev"),
    ("synth-v1-val-null-701", "validation"),
    ("synth-v1-val-null-702", "validation"),
    ("synth-v1-val-alpha-710", "validation"),
    ("synth-v1-val-alpha-711", "validation"),
]


def _no_real_lanes_nested(node: Any) -> None:
    """`real_lanes` may exist ONLY as a top-level registry key — assert no
    dict reachable under the pinned structures carries it."""
    if isinstance(node, dict):
        assert "real_lanes" not in node, "real_lanes must never be nested"
        for value in node.values():
            _no_real_lanes_nested(value)
    elif isinstance(node, list):
        for item in node:
            _no_real_lanes_nested(item)


def _sole_lane(registry: dict[str, Any], lane: str) -> dict[str, Any]:
    lanes = registry["real_lanes"]
    assert isinstance(lanes, list)
    matches = [e for e in lanes if e["lane"] == lane]
    assert len(matches) == 1, f"expected exactly one {lane} entry, got {len(matches)}"
    return matches[0]


def _load(path: Any) -> dict[str, Any]:
    return json.loads(path.read_text())


def test_real_lanes_are_top_level_only() -> None:
    reg = _load(REGISTRY_PATH)
    oreg = _load(OPTIONS_REGISTRY_PATH)
    assert isinstance(reg["real_lanes"], list)
    assert isinstance(oreg["real_lanes"], list)
    # never nested inside the pinned structures
    _no_real_lanes_nested(reg["worlds"])
    _no_real_lanes_nested(oreg["overlays"])


def test_equity_lane_entry_matches_src_constants() -> None:
    """Both directions: renaming the src constant OR editing the registry
    entry breaks this equality."""
    reg = _load(REGISTRY_PATH)
    entry = _sole_lane(reg, MASSIVE_PROVIDER)  # "massive-polygon-free/1"
    assert entry["tokens"] == [MASSIVE_SCHEMA_VERSION, MASSIVE_MANIFEST_SCHEMA_VERSION]
    assert entry["capture_version"] in _KNOWN_CAPTURE_VERSIONS  # "m4b-capture/1"
    assert entry["status"] == "PENDING-era"


def test_universe_manifest_is_pinned_and_verifies() -> None:
    reg = _load(REGISTRY_PATH)
    entry = _sole_lane(reg, MASSIVE_PROVIDER)
    path = REPO_ROOT / entry["universe_manifest"]
    assert path.is_file(), f"universe manifest missing: {path}"
    universe = CoverageUniverse.model_validate_json(path.read_text())
    verify_universe(universe)  # content-hash binding + self-consistency
    assert entry["universe_manifest_sha256"] == universe.content_sha256
    # owner decision 2026-08-23: expected masters count from the DECLARED
    # work manifest (29 underlyings x 105 Fridays = 3,045), never the
    # docs' 30 x 105 = 3,150 (reconciliation flagged at era-results).
    assert len(universe.underlyings) == 29
    assert len(universe.as_of_fridays) == 105
    assert universe.expected_masters == 3045


def test_capture_pin_null_only_while_pending_era() -> None:
    """capture_manifest_sha256 null is legal ONLY while status is
    PENDING-era; a pinned hash may not coexist with PENDING-era."""
    reg = _load(REGISTRY_PATH)
    for entry in reg["real_lanes"]:
        if entry["capture_manifest_sha256"] is None:
            assert entry["status"] == "PENDING-era", entry["lane"]
        else:
            assert entry["status"] != "PENDING-era", entry["lane"]


def test_options_lane_entry_and_status() -> None:
    oreg = _load(OPTIONS_REGISTRY_PATH)
    entry = _sole_lane(oreg, MASSIVE_DERIVED_PROVIDER)  # "massive-derived-free/1"
    assert entry["depends_on"] == MASSIVE_PROVIDER  # the equity real lane
    # nothing is pinned for this lane yet: it must still be PENDING-era
    assert entry["status"] == "PENDING-era"


def test_pinned_composition_unchanged() -> None:
    """Mirror of the existing composition pins: 15 worlds in the exact
    dev/validation pools and the 8 overlays in exact order — the additive
    real_lanes key must not have changed any of it."""
    reg = _load(REGISTRY_PATH)
    worlds = reg["worlds"]
    assert len(worlds) == 15, f"exactly 15 registered worlds, got {len(worlds)}"
    dev = sorted(
        (w["spec"]["seed"], w["spec"]["kind"], w["world_id"]) for w in worlds if w["pool"] == "dev"
    )
    assert dev == PINNED_DEV_POOL, f"dev pool drifted: {dev}"
    val = sorted(
        (w["spec"]["seed"], w["spec"]["kind"], w["world_id"])
        for w in worlds
        if w["pool"] == "validation"
    )
    assert val == PINNED_VALIDATION_POOL, f"validation pool drifted: {val}"

    oreg = _load(OPTIONS_REGISTRY_PATH)
    assert [(o["world_id"], o["pool"]) for o in oreg["overlays"]] == PINNED_OVERLAYS
