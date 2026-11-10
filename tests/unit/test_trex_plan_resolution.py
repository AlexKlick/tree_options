"""TDD for plan fast-path eligibility and canonical-resolution safety."""
from pathlib import Path

from tree_options.trex_web.plan_resolution import requires_legacy_resolution


def test_unique_dated_filename_can_use_prevalidated_plan(tmp_path: Path) -> None:
    p = tmp_path / "2026-11-10.toml"
    p.write_text("placeholder")
    assert not requires_legacy_resolution("trade-01", p, tmp_path, {"trade-01": 1})


def test_matching_named_file_can_use_prevalidated_plan(tmp_path: Path) -> None:
    p = tmp_path / "trade-01.toml"
    p.write_text("placeholder")
    assert not requires_legacy_resolution("trade-01", p, tmp_path, {"trade-01": 1})


def test_duplicate_ids_keep_existing_canonical_lookup(tmp_path: Path) -> None:
    p = tmp_path / "b.toml"
    p.write_text("placeholder")
    assert requires_legacy_resolution("trade-01", p, tmp_path, {"trade-01": 2})


def test_filename_collision_keeps_existing_resolution(tmp_path: Path) -> None:
    (tmp_path / "trade-01.toml").write_text("different id")
    p = tmp_path / "2026-11-10.toml"
    p.write_text("actual trade id")
    assert requires_legacy_resolution("trade-01", p, tmp_path, {"trade-01": 1})


def test_missing_count_does_not_claim_unique() -> None:
    root = Path("/unlikely-test-data")
    assert requires_legacy_resolution("x", root / "x.toml", root, {})
