"""TREX cockpit hot path: each ordinary plan is parsed once per list call.

These tests require the full TREX checkout; the isolated policy suite is
separately runnable without the broker-dependent plan model.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.unit.test_trex_web import PLAN_TOML
from tree_options.trex_web import reader


def _write(plans: Path, filename: str, plan_id: str) -> None:
    plans.mkdir(parents=True, exist_ok=True)
    text = PLAN_TOML.replace('id = "putspread-test"', f'id = "{plan_id}"', 1)
    assert text != PLAN_TOML
    (plans / filename).write_text(text)


def test_unique_plans_parse_once_not_quadratically(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plans = tmp_path / "plans"
    for i in range(20):
        _write(plans, f"batch-{i:03d}.toml", f"batch-plan-{i:03d}")
    original = reader.load_plan
    calls: list[Path] = []

    def counted(path: Path):
        calls.append(path)
        return original(path)

    monkeypatch.setattr(reader, "load_plan", counted)
    views = reader.list_plans(tmp_path / "state", plans)
    assert len(views) == 20
    assert len({v.plan.id for v in views}) == 20
    assert len(calls) == 20  # old code reparsed during every ID resolution


def test_duplicate_ids_remain_on_legacy_canonical_path(tmp_path: Path) -> None:
    plans = tmp_path / "plans"
    _write(plans, "a.toml", "duplicate")
    _write(plans, "b.toml", "duplicate")
    listed = reader.list_plans(tmp_path / "state", plans)
    canonical = reader.load_plan_view("duplicate", tmp_path / "state", plans)
    assert canonical is not None
    assert len(listed) == 2  # existing list contract, no silent dedupe
    assert all(v.plan == canonical.plan for v in listed)


def test_literal_id_filename_precedence_is_not_rewritten(tmp_path: Path) -> None:
    plans = tmp_path / "plans"
    _write(plans, "a.toml", "trade-a")
    _write(plans, "trade-a.toml", "trade-b")
    listed = reader.list_plans(tmp_path / "state", plans)
    canonical = reader.load_plan_view("trade-a", tmp_path / "state", plans)
    assert canonical is not None
    # Old resolver prefers trade-a.toml even when it contains a different ID.
    assert canonical.plan.id == "trade-b"
    assert listed[0].plan == canonical.plan
