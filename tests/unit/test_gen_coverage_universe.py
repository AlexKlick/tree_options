"""gen_coverage_universe: wrapper parsing, grid validation, CLI exits, pins.

The committed universe manifest is loaded read-only and pinned (29 x 105 =
3,045, content hash a13dd4eb…); every generated manifest is synthetic.
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
    CoverageUniverse,
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
        _wrapper(["SPY", "QQQ"], [FRIDAY_A, FRIDAY_B]), source="wrapper.sh"
    )
    assert universe.underlyings == ("QQQ", "SPY"), "names are sorted"
    assert universe.as_of_fridays == (FRIDAY_A, FRIDAY_B)
    assert universe.expected_masters == 4
    assert universe.source_sha256 and universe.source_sha256 != universe.content_sha256
    verify_universe(universe)


def test_render_round_trips_through_the_model() -> None:
    universe = gen.build_universe(_wrapper(["SPY"], [FRIDAY_A, FRIDAY_B]), source="wrapper.sh")
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
        gen.build_universe(_wrapper(underlyings, fridays), source="wrapper.sh")


def test_a_wrapper_without_the_marker_lines_refused() -> None:
    with pytest.raises(ValueError, match="no 'for d in"):
        gen.build_universe("#!/bin/bash\nexit 0\n", source="wrapper.sh")
    with pytest.raises(ValueError, match="no --underlyings list"):
        gen.build_universe("for d in 2025-03-07; do echo $d; done\n", source="wrapper.sh")


# ---- CLI exit codes ---------------------------------------------------------------------


def test_main_exits_3_on_an_invalid_grid(tmp_path: Path) -> None:
    wrapper = tmp_path / "run.sh"
    wrapper.write_text(_wrapper(["SPY"], [SATURDAY]), encoding="utf-8")
    out = tmp_path / "universe.json"
    assert gen.main(["--from-run-sh", str(wrapper), "--out", str(out)]) == 3
    assert not out.exists(), "an invalid grid writes nothing"


def test_main_exits_2_on_an_unreadable_wrapper(tmp_path: Path) -> None:
    assert gen.main(["--from-run-sh", str(tmp_path / "absent.sh")]) == 2


def test_main_writes_a_verifiable_manifest(tmp_path: Path) -> None:
    wrapper = tmp_path / "run.sh"
    wrapper.write_text(_wrapper(["SPY", "QQQ"], [FRIDAY_A, FRIDAY_B]), encoding="utf-8")
    out = tmp_path / "universe.json"
    assert gen.main(["--from-run-sh", str(wrapper), "--out", str(out)]) == 0
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
    assert universe.content_sha256.startswith("a13dd4eb")
    assert universe.source.endswith("run.sh")
    assert universe.schema_version == "m4-coverage-universe/1"
