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
    assert isinstance(worlds, list)
    pools = {w["pool"] for w in worlds}  # type: ignore[index]
    assert pools == {"dev", "validation"}
    # exact frozen composition (round-2 P2-2: no extra worlds, no duplicate
    # seeds, no seed->kind reassignment can pass). M2-proper §1.1/§3.A: the
    # pre-trial power extension added 706/707 at coefficient 0.005. §10: the
    # gate #1 disposition added 708/709 at coefficient 0.01. M3 §3.G: the
    # OD1 tripwire disposition added 710/711 at coefficient 0.5 (owner-ruled
    # 2026-08-20) — each amendment window closed at that gate's first trial
    # registration.
    assert len(worlds) == 15, f"exactly 15 registered worlds, got {len(worlds)}"
    dev = sorted(
        (w["spec"]["seed"], w["spec"]["kind"], w["world_id"])  # type: ignore[index]
        for w in worlds
        if w["pool"] == "dev"  # type: ignore[index]
    )
    assert dev == [
        (101, "null", "synth-v1-dev-null-101"),
        (102, "alpha", "synth-v1-dev-alpha-102"),
        (103, "null", "synth-v1-dev-null-103"),
        (104, "alpha", "synth-v1-dev-alpha-104"),
    ], f"dev pool drifted: {dev}"
    val = sorted(
        (w["spec"]["seed"], w["spec"]["kind"], w["world_id"])  # type: ignore[index]
        for w in worlds
        if w["pool"] == "validation"  # type: ignore[index]
    )
    assert val == [
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
    ], f"validation pool drifted: {val}"
    # power stratum frozen: 704/705 weak (0.002), 706/707 gate-#1 power
    # (0.005), 708/709 gate-#2 power (0.01, §10), 710/711 the M3 options
    # transfer stratum (0.5, OD1-re-priced owner ruling) — no coefficient
    # may silently drift
    coefficients = {
        w["spec"]["seed"]: w["spec"]["alpha"]["coefficient"]  # type: ignore[index]
        for w in worlds  # type: ignore[union-attr]
        if w["spec"].get("alpha") is not None  # type: ignore[union-attr]
    }
    assert coefficients == {
        102: 0.002,
        104: 0.002,
        704: 0.002,
        705: 0.002,
        706: 0.005,
        707: 0.005,
        708: 0.01,
        709: 0.01,
        710: 0.5,
        711: 0.5,
    }, f"alpha coefficients drifted: {coefficients}"
    # round-3 P2-1: the OUTER id must equal the spec id (verify_worlds
    # generates and identifies snapshots from the inner spec)
    for w in worlds:  # type: ignore[union-attr]
        assert w["world_id"] == w["spec"]["world_id"], w["world_id"]  # type: ignore[index]


def test_verify_worlds_gates_quality_not_just_hashes() -> None:
    """Round-1 P1-3: regeneration runs the M1 quality gates — a
    quality-invalid world can never be pinned or reported OK."""
    import sys

    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import verify_worlds
    from tree_options.data import quality

    calls: list[object] = []
    original = quality.verify_manifest

    def counting_verify(*args: object, **kwargs: object) -> object:
        calls.append(args)
        return original(*args, **kwargs)  # type: ignore[arg-type]

    quality.verify_manifest = counting_verify  # type: ignore[assignment]
    try:
        reg = _registry()
        small = next(
            w
            for w in reg["worlds"]
            if w["world_id"] == "synth-v1-dev-null-101"  # type: ignore[index]
        )
        verify_worlds._generate_and_ingest(small, "0" * 64)  # type: ignore[arg-type]
    finally:
        quality.verify_manifest = original  # type: ignore[assignment]
    assert calls, "verify_manifest must run inside world regeneration"


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
