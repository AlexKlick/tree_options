"""Workstream C: frozen world registry + byte-exact regeneration (M2 §3.C).

The registry pins each world twice: to its WorldSpec (seeds/rates — never
rewritten) and to the generating code (generator sha — re-pinned only by
an explicit `verify_worlds.py --recompute` after a deliberate generator
change). Validation worlds are the synthetic-era holdout (packet §1.4).
"""

from __future__ import annotations

import json

from tests.conftest import REPO_ROOT

REGISTRY_PATH = REPO_ROOT / "data" / "worlds" / "registry.json"


def _registry() -> dict[str, object]:
    return json.loads(REGISTRY_PATH.read_text())


def test_registry_shape_and_pools() -> None:
    reg = _registry()
    assert reg["registry_version"] == "worlds/1"
    worlds = reg["worlds"]
    assert isinstance(worlds, list) and len(worlds) >= 6
    pools = {w["pool"] for w in worlds}  # type: ignore[index]
    assert pools == {"dev", "validation"}
    validation = [w for w in worlds if w["pool"] == "validation"]  # type: ignore[index]
    assert len(validation) == 5, "exactly 5 frozen validation worlds per spec version"
    kinds = {w["spec"]["kind"] for w in validation}  # type: ignore[index]
    assert kinds == {"null", "alpha"}, "validation pool must cover both world kinds"
    dev_seeds = {w["spec"]["seed"] for w in worlds if w["pool"] == "dev"}  # type: ignore[index]
    val_seeds = {w["spec"]["seed"] for w in validation}
    assert dev_seeds.isdisjoint(val_seeds), "dev and validation seeds must not overlap"


def test_registry_pins_generator_code() -> None:
    """The registry is bound to the generating code: any synth/ change
    invalidates the pin and requires an explicit --recompute."""
    import sys

    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    from verify_worlds import generator_code_sha

    reg = _registry()
    assert reg["generator_code_sha"] == generator_code_sha()


def test_small_dev_worlds_reproduce_byte_exact(static_calendar) -> None:  # type: ignore[no-untyped-def]
    from tree_options.data.ingest import ingest_snapshot
    from tree_options.synth import generate_world
    from tree_options.synth.spec import WorldSpec

    reg = _registry()
    small = [
        w
        for w in reg["worlds"]  # type: ignore[index]
        if w["expected"]["bar_count"] <= 20_000  # type: ignore[index]
    ]
    assert small, "registry must contain at least one gate-speed world"
    for entry in small:
        spec = WorldSpec(**entry["spec"])  # type: ignore[arg-type]
        world = generate_world(spec, static_calendar)
        snapshot = ingest_snapshot(
            world.payload,
            world.master,
            snapshot_id=spec.world_id,
            normalization_code_sha="0" * 64,
        )
        exp = entry["expected"]  # type: ignore[index]
        assert snapshot.manifest.content_sha256 == exp["content_sha256"], entry["world_id"]
        assert snapshot.manifest.bar_count == exp["bar_count"], entry["world_id"]
        assert snapshot.manifest.action_count == exp["action_count"], entry["world_id"]
        assert snapshot.manifest.security_count == exp["security_count"], entry["world_id"]


def test_verify_worlds_cli_gate_subset() -> None:
    """The CLI verifies the dev pool's small worlds end to end (the same
    subset the gate runs; full worlds are the teed artifact run)."""
    import sys

    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    from verify_worlds import main

    rc = main(["--pool", "dev", "--max-bars", "20000"])
    assert rc == 0
