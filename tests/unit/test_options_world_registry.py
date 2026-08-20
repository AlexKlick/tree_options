"""Workstream A: the options-overlay registry lane (M3 plan §3.A).

The registry pins each overlay to its parent equity world, its
OptionsOverlaySpec, and the generating code (re-pinned only by an
explicit --recompute). Verification is sample-based plus analytic counts;
the small dev overlays reproduce byte-exact inside the gate.
"""

from __future__ import annotations

import json
import sys

from tests.conftest import REPO_ROOT

OPTIONS_REGISTRY_PATH = REPO_ROOT / "data" / "worlds" / "options_registry.json"
EQUITY_REGISTRY_PATH = REPO_ROOT / "data" / "worlds" / "registry.json"


def _registry() -> dict[str, object]:
    return json.loads(OPTIONS_REGISTRY_PATH.read_text())


def test_registry_shape_and_dev_composition() -> None:
    reg = _registry()
    assert reg["options_registry_version"] == "options-worlds/1"
    overlays = reg["overlays"]
    assert isinstance(overlays, list)
    # exact frozen composition: the four dev overlays plus the validation
    # overlays pinned in the pre-declared amendment window (plan §3.G),
    # executed 2026-08-20 (owner ruling recorded in
    # docs/m3-od1-tripwire-decision.md). The window closes at the first
    # sealed trial registration; this assertion changes only in another
    # owner-signed amendment, never silently.
    assert [(o["world_id"], o["pool"]) for o in overlays] == [
        ("synth-v1-dev-null-101", "dev"),
        ("synth-v1-dev-alpha-102", "dev"),
        ("synth-v1-dev-null-103", "dev"),
        ("synth-v1-dev-alpha-104", "dev"),
        ("synth-v1-val-null-701", "validation"),
        ("synth-v1-val-null-702", "validation"),
        ("synth-v1-val-alpha-710", "validation"),
        ("synth-v1-val-alpha-711", "validation"),
    ], f"overlay pool drifted: {[(o['world_id'], o['pool']) for o in overlays]}"
    equity = json.loads(EQUITY_REGISTRY_PATH.read_text())
    parents = {w["world_id"]: w for w in equity["worlds"]}
    for overlay in overlays:
        assert overlay["world_id"] == overlay["spec"]["world_id"]
        parent = parents.get(overlay["world_id"])
        assert parent is not None, f"{overlay['world_id']} has no parent equity world"
        assert overlay["spec"]["seed"] == parent["spec"]["seed"], "overlay seed must match parent"
        assert overlay["expected"], "expected block must be populated (pin with --recompute)"


def test_registry_pins_synth_options_code() -> None:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    from verify_options_worlds import synth_options_code_sha

    assert _registry()["synth_options_code_sha"] == synth_options_code_sha()


def test_code_pin_drift_fails_closed(tmp_path) -> None:  # type: ignore[no-untyped-def]
    reg = _registry()
    reg["synth_options_code_sha"] = "0" * 64
    drifted = tmp_path / "options_registry.json"
    drifted.write_text(json.dumps(reg))
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    from verify_options_worlds import main

    assert main(["--registry", str(drifted)]) == 2


def test_slice_tamper_detected(tmp_path) -> None:  # type: ignore[no-untyped-def]
    reg = _registry()
    small = next(o for o in reg["overlays"] if o["world_id"] == "synth-v1-dev-null-101")
    small["expected"]["sample_slice_hashes"][0][2] = "0" * 64
    tampered = tmp_path / "options_registry.json"
    tampered.write_text(json.dumps(reg))
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    from verify_options_worlds import main

    assert main(["--registry", str(tampered), "--world", "synth-v1-dev-null-101"]) == 1


def test_verify_options_worlds_cli_gate_subset() -> None:
    """The CLI verifies the small dev overlays end to end (the same subset
    the gate runs; full worlds are the teed artifact run)."""
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    from verify_options_worlds import main

    rc = main(["--world", "synth-v1-dev-null-101", "--world", "synth-v1-dev-alpha-102"])
    assert rc == 0
