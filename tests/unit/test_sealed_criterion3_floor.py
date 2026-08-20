"""Review r2 P1-4: criterion 3's rejection floor is PER WORLD (pooled
across the world's arms), per the owner-ruled amendment recorded in
docs/m3-od1-tripwire-decision.md and docs/m3-options-plan.md §4 — both
drivers had applied it per arm, so a world at 60 arm-A + 60 arm-B
qualifying rejections (120 pooled, passes the ruling) failed twice."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


GATE = _load("run_m3_sealed_gate")
CORRECTION = _load("run_m3_sealed_verdict_correction")


def test_pooled_per_world_floor_passes_split_counts() -> None:
    counts = {("w1", "A"): 60, ("w1", "B"): 60, ("w2", "A"): 150, ("w2", "B"): 7}
    for driver in (GATE, CORRECTION):
        assert driver.zero_bid_floor_failures(counts) == [], driver


def test_pooled_per_world_floor_fails_short_worlds() -> None:
    counts = {("w1", "A"): 60, ("w1", "B"): 30, ("w2", "A"): 99, ("w2", "B"): 0}
    expected = [
        "w1: pooled zero-bid rejections 90 < 100",
        "w2: pooled zero-bid rejections 99 < 100",
    ]
    for driver in (GATE, CORRECTION):
        assert driver.zero_bid_floor_failures(counts) == expected, driver
