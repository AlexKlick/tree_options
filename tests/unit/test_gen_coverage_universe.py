"""gen_coverage_universe: wrapper parsing, grid validation, CLI exits, pins.

The committed universe manifest is loaded read-only and pinned (29 x 105 =
3,045, content hash 4553fc7a…); every generated manifest is synthetic.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import gen_coverage_universe as gen  # type: ignore[import-not-found]  # scripts/  # noqa: E402
from tree_options.data.coverage_census import (  # noqa: E402
    CensusTaxonomyError,
    CoverageUniverse,
    universe_content_sha256,
    verify_universe,
)

FRIDAY_A = "2025-03-07"
FRIDAY_B = "2025-03-14"
SATURDAY = "2025-03-08"  # inside the same weekend: never a grid date
COMMITTED = REPO_ROOT / "data" / "coverage" / "coverage_universe.json"


def _wrapper(underlyings: list[str], fridays: list[str]) -> str:
    return (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f"for d in {' '.join(fridays)}; do\n"
        "  python scripts/capture_massive_structural.py"
        f' --underlyings {",".join(underlyings)} --as-of "$d" --bars 0\n'
        "done\n"
    )


# ---- build_universe ----------------------------------------------------------------


def test_build_universe_parses_a_small_wrapper() -> None:
    universe = gen.build_universe(
        _wrapper(["SPY", "QQQ"], [FRIDAY_A, FRIDAY_B]), source_id="wrapper.sh"
    )
    assert universe.underlyings == ("QQQ", "SPY"), "names are sorted"
    assert universe.as_of_fridays == (FRIDAY_A, FRIDAY_B)
    assert universe.expected_masters == 4
    assert universe.source_sha256 and universe.source_sha256 != universe.content_sha256
    verify_universe(universe)


def test_render_round_trips_through_the_model() -> None:
    universe = gen.build_universe(_wrapper(["SPY"], [FRIDAY_A, FRIDAY_B]), source_id="wrapper.sh")
    again = CoverageUniverse.model_validate_json(gen.render(universe))
    assert again == universe
    verify_universe(again)


# ---- grid validation ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("underlyings", "fridays", "needle"),
    [
        (["SPY"], [FRIDAY_A, SATURDAY, FRIDAY_B], "non-Friday"),
        (["SPY"], [FRIDAY_A, FRIDAY_A], "duplicate as_of dates"),
        (["SPY"], [FRIDAY_B, FRIDAY_A], "not sorted ascending"),
        (["SPY", "SPY"], [FRIDAY_A], "duplicate underlyings"),
        (["SPY", ""], [FRIDAY_A], "empty underlying name"),
    ],
)
def test_invalid_grids_refused(underlyings: list[str], fridays: list[str], needle: str) -> None:
    with pytest.raises(ValueError, match=needle):
        gen.build_universe(_wrapper(underlyings, fridays), source_id="wrapper.sh")


def test_a_wrapper_without_the_marker_lines_refused() -> None:
    with pytest.raises(ValueError, match="no 'for d in"):
        gen.build_universe("#!/bin/bash\nexit 0\n", source_id="wrapper.sh")
    with pytest.raises(ValueError, match="no --underlyings list"):
        gen.build_universe("for d in 2025-03-07; do echo $d; done\n", source_id="wrapper.sh")


# ---- CLI exit codes ---------------------------------------------------------------------


def test_main_exits_3_on_an_invalid_grid(tmp_path: Path) -> None:
    wrapper = tmp_path / "run.sh"
    wrapper.write_text(_wrapper(["SPY"], [SATURDAY]), encoding="utf-8")
    out = tmp_path / "universe.json"
    assert (
        gen.main(
            [
                "--from-run-sh",
                str(wrapper),
                "--source-id",
                "synthetic/run.sh",
                "--out",
                str(out),
            ]
        )
        == 3
    )
    assert not out.exists(), "an invalid grid writes nothing"


def test_main_exits_2_on_an_unreadable_wrapper(tmp_path: Path) -> None:
    assert gen.main(["--from-run-sh", str(tmp_path / "absent.sh")]) == 2


def test_main_writes_a_verifiable_manifest(tmp_path: Path) -> None:
    wrapper = tmp_path / "run.sh"
    wrapper.write_text(_wrapper(["SPY", "QQQ"], [FRIDAY_A, FRIDAY_B]), encoding="utf-8")
    out = tmp_path / "universe.json"
    assert (
        gen.main(
            [
                "--from-run-sh",
                str(wrapper),
                "--source-id",
                "synthetic/run.sh",
                "--out",
                str(out),
            ]
        )
        == 0
    )
    universe = CoverageUniverse.model_validate_json(out.read_text())
    verify_universe(universe)
    assert universe.expected_masters == 4


# ---- the committed manifest is pinned ------------------------------------------------------


def test_committed_universe_manifest_is_pinned() -> None:
    universe = CoverageUniverse.model_validate_json(COMMITTED.read_text())
    verify_universe(universe)
    assert len(universe.underlyings) == 29
    assert len(universe.as_of_fridays) == 105
    assert universe.expected_masters == 3045
    assert universe.content_sha256.startswith("4553fc7a")
    assert universe.source_id == "artifacts/m4b-coverage-era/run.sh"
    assert not Path(universe.source_id).is_absolute()
    assert universe.schema_version == "m4-coverage-universe/2"


# ---- source identity is spelling- and checkout-root-independent ----------------------
#
# Round 4 normalized relative/absolute spellings to one absolute path. The
# external PR #13 audit caught the remaining cross-clone defect: that absolute
# host path still contaminated the committed bytes and content hash. Version 2
# stores only a canonical repo-relative source_id; the wrapper bytes remain
# independently pinned by source_sha256.


def test_relative_and_absolute_wrapper_spellings_produce_identical_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """build_universe over the same wrapper, spelled once relatively and once
    absolutely, must produce identical manifest bytes and hashes — otherwise
    the checklist's relative spelling can never reproduce the committed
    artifact."""
    wrapper = tmp_path / "era" / "run.sh"
    wrapper.parent.mkdir()
    text = _wrapper(["SPY", "QQQ"], [FRIDAY_A, FRIDAY_B])
    wrapper.write_text(text, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    relative_id = gen.logical_source_id(Path("era/run.sh"), repo_root=tmp_path)
    absolute_id = gen.logical_source_id(wrapper, repo_root=tmp_path)
    relative = gen.build_universe(text, source_id=relative_id)
    absolute = gen.build_universe(text, source_id=absolute_id)
    assert relative.content_sha256 == absolute.content_sha256
    assert gen.render(relative) == gen.render(absolute), "identical bytes, not just hashes"


def test_two_physical_checkout_roots_render_byte_identical_universe(tmp_path: Path) -> None:
    """The committed identity is logical; a clone's host path is not content."""
    text = _wrapper(["SPY", "QQQ"], [FRIDAY_A, FRIDAY_B])
    wrappers: list[tuple[Path, Path]] = []
    for checkout in (tmp_path / "clone-a", tmp_path / "nested" / "clone-b"):
        wrapper = checkout / "artifacts" / "m4b-coverage-era" / "run.sh"
        wrapper.parent.mkdir(parents=True)
        wrapper.write_text(text, encoding="utf-8")
        wrappers.append((checkout, wrapper))

    first = gen.build_universe(
        text, source_id=gen.logical_source_id(wrappers[0][1], repo_root=wrappers[0][0])
    )
    second = gen.build_universe(
        text, source_id=gen.logical_source_id(wrappers[1][1], repo_root=wrappers[1][0])
    )

    assert first.content_sha256 == second.content_sha256
    assert gen.render(first) == gen.render(second)


def test_wrapper_byte_change_changes_universe_identity() -> None:
    text = _wrapper(["SPY"], [FRIDAY_A])
    baseline = gen.build_universe(text, source_id="artifacts/era/run.sh")
    changed = gen.build_universe(
        text + "# owner-visible wrapper edit\n", source_id="artifacts/era/run.sh"
    )

    assert baseline.source_sha256 != changed.source_sha256
    assert baseline.content_sha256 != changed.content_sha256


def test_rehashed_absolute_source_id_is_refused() -> None:
    universe = gen.build_universe(_wrapper(["SPY"], [FRIDAY_A]), source_id="artifacts/era/run.sh")
    contaminated = universe.model_copy(update={"source_id": "/host/checkout/artifacts/era/run.sh"})
    contaminated = contaminated.model_copy(
        update={"content_sha256": universe_content_sha256(contaminated)}
    )

    with pytest.raises(CensusTaxonomyError, match="repo-relative"):
        verify_universe(contaminated)
