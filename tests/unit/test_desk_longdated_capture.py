"""The desk's long-dated capture script: its as_of plan and its write targets.

Static checks only (the script is never executed here): the as_of list is
parsed from the script text and judged against the checked-in trex NYSE
calendar and a hand-listed oracle of traded monthly expiries.
"""

from __future__ import annotations

import re
import subprocess
from datetime import date
from itertools import pairwise
from pathlib import Path

import pytest

from tests.conftest import REPO_ROOT
from tree_options.time.calendar import StaticSessionCalendar

SCRIPT = REPO_ROOT / "scripts" / "desk_longdated_capture.sh"
TREX = REPO_ROOT / "data" / "calendar" / "trex" / "nyse_sessions_2018_01_02_2028_12_29.json"

# Traded monthly expiries whose 90-270 DTE life must be sampled at least once:
# every month from 2025-01 (the first whose 90-DTE day falls after the first
# as_of) to 2027-06 (the last within 270 DTE of the final as_of). Hand-listed;
# 2025-04-17, 2026-06-18 and 2027-06-17 are the holiday-moved Thursdays.
MUST_COVER = (
    "2025-01-17 2025-02-21 2025-03-21 2025-04-17 2025-05-16 2025-06-20 2025-07-18 "
    "2025-08-15 2025-09-19 2025-10-17 2025-11-21 2025-12-19 2026-01-16 2026-02-20 "
    "2026-03-20 2026-04-17 2026-05-15 2026-06-18 2026-07-17 2026-08-21 2026-09-18 "
    "2026-10-16 2026-11-20 2026-12-18 2027-01-15 2027-02-19 2027-03-19 2027-04-16 "
    "2027-05-21 2027-06-17"
).split()


def _as_ofs() -> list[date]:
    text = SCRIPT.read_text()
    block = re.search(r"AS_OFS=\((.*?)\)", text, re.S)
    assert block is not None, "AS_OFS array not found"
    return [date.fromisoformat(tok) for tok in block.group(1).split()]


def test_the_script_parses_under_bash() -> None:
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)


def test_every_as_of_is_an_nyse_session_in_order_and_inside_the_window() -> None:
    calendar = StaticSessionCalendar(TREX, TREX.with_suffix(".sha256"))
    as_ofs = _as_ofs()
    assert len(as_ofs) == 27
    assert as_ofs == sorted(set(as_ofs))
    assert all(calendar.is_session(d) for d in as_ofs)
    # The rolling 2-year edge was 2024-09-23 at launch (probed: a bar request
    # from 2024-09-13 returned its first bar on 2024-09-23).
    assert as_ofs[0] >= date(2024, 9, 23)
    gaps = [(b - a).days for a, b in pairwise(as_ofs)]
    assert max(gaps) <= 29, gaps


def test_every_traded_monthly_is_sampled_inside_its_90_270_dte_life() -> None:
    as_ofs = _as_ofs()
    for iso in MUST_COVER:
        expiry = date.fromisoformat(iso)
        looks = [a for a in as_ofs if 90 <= (expiry - a).days <= 270]
        assert looks, f"{iso} is never 90-270 DTE at a planned as_of"


def test_the_script_writes_only_the_new_desk_dirs() -> None:
    text = SCRIPT.read_text()
    assert 'CACHE="$ARTIFACTS/massive-cache-desk"' in text
    assert 'OUT="$ARTIFACTS/desk-longdated-capture"' in text
    # The load-bearing research cache is only ever the seed SOURCE (and the
    # resolved-path guard protects it: see the executed tests below).
    assert re.findall(r"massive-cache(?!-desk)", text.split("set -u", 1)[1]) == [
        "massive-cache",  # SEED_FROM=
    ]
    assert '--from-cache "$SEED_FROM" --to-cache "$CACHE"' in text
    # Outside the write-path guard (which names them as PROTECTED), the
    # sealed trees are never mentioned at all.
    body = text.split("set -u", 1)[1]
    head, rest = body.split("# ---- write-path guard", 1)
    outside_guard = head + rest.split("\nNAMES=", 1)[1]
    for banned in ("m4b-", "artifacts/bars", "data/bars", "bars*"):
        assert banned not in outside_guard
    assert "--bars-expiries monthly-traded" in text
    assert "--bars-strike-band 2" in text and "--dte-min 90 --dte-max 270" in text


# ---- executed against stubs: no python of ours, no wire, no real sleep -------

STUB_PY = """#!/bin/bash
echo "py $*" >> "$STUB_LOG"
case "$*" in
  *CHAIN_UNIVERSE*) echo "AAA,BBB" ;;
  *tree_options.__file__*) echo "$STUB_REPO/src/tree_options/__init__.py" ;;
  *seed_massive_cache.py*) exit "${STUB_SEED_RC:-0}" ;;
esac
exit 0
"""
FAKE_SLEEP = '#!/bin/bash\necho "sleep $*" >> "$STUB_LOG"\n'


def _sandbox(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    artifacts = tmp_path / "tree" / "artifacts"
    (artifacts / "massive-cache").mkdir(parents=True)
    (artifacts / "bars-authority").mkdir()
    (artifacts / "m4b-captures").mkdir()
    (tmp_path / "tree" / "data" / "bars").mkdir(parents=True)
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    py = fakebin / "stub-python"
    py.write_text(STUB_PY)
    (fakebin / "sleep").write_text(FAKE_SLEEP)
    for f in (py, fakebin / "sleep"):
        f.chmod(0o755)
    log = tmp_path / "calls.log"
    env = {
        "PATH": f"{fakebin}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "DESK_CAPTURE_ARTIFACTS": str(artifacts),
        "DESK_CAPTURE_PYTHON": str(py),
        "STUB_LOG": str(log),
        "STUB_REPO": str(REPO_ROOT),
    }
    return artifacts, env


def _run(env: dict[str, str]) -> tuple[int, list[str], str]:
    proc = subprocess.run(
        ["bash", str(SCRIPT)], env=env, capture_output=True, text=True, check=False
    )
    log = Path(env["STUB_LOG"])
    calls = log.read_text().splitlines() if log.exists() else []
    return proc.returncode, calls, proc.stderr


def _kinds(calls: list[str]) -> list[str]:
    out = []
    for c in calls:
        if c.startswith("sleep"):
            out.append(c)
        elif "seed_massive_cache.py" in c:
            out.append("seed")
        elif "capture_massive_structural.py" in c:
            out.append("capture")
    return out


def test_the_happy_path_seeds_then_cools_down_before_every_wire_stage(tmp_path: Path) -> None:
    _artifacts, env = _sandbox(tmp_path)
    rc, calls, _err = _run(env)
    assert rc == 0
    assert _kinds(calls) == [
        "seed",
        "sleep 13",
        "capture",
        "sleep 13",
        "capture",
        "sleep 13",
        "capture",
    ], "Codex P2-3: a fresh governor per stage must not follow the last request within 12 s"


def test_a_seed_refusal_stops_the_run_before_any_capture(tmp_path: Path) -> None:
    _artifacts, env = _sandbox(tmp_path)
    env["STUB_SEED_RC"] = "2"
    rc, calls, err = _run(env)
    assert rc == 2
    assert _kinds(calls) == ["seed"]
    assert "seed" in err.lower()


@pytest.mark.parametrize(
    ("link", "target"),
    [
        ("massive-cache-desk", "massive-cache"),  # the cache aliased onto the sealed cache
        ("desk-longdated-capture", "m4b-captures"),  # the out dir aliased onto a sealed capture
    ],
)
def test_a_symlinked_write_dir_onto_a_protected_dir_is_refused_before_anything_runs(
    tmp_path: Path, link: str, target: str
) -> None:
    artifacts, env = _sandbox(tmp_path)
    (artifacts / link).symlink_to(artifacts / target)
    rc, calls, err = _run(env)
    assert rc == 64
    assert calls == [], "the guard runs before the first python call"
    assert "REFUSED" in err


def test_a_symlinked_output_subdir_is_refused_too(tmp_path: Path) -> None:
    artifacts, env = _sandbox(tmp_path)
    out = artifacts / "desk-longdated-capture"
    out.mkdir()
    (out / "bars").symlink_to(artifacts / "bars-authority")
    rc, calls, err = _run(env)
    assert rc == 64 and calls == [] and "bars-authority" in err


def test_data_bars_of_the_checkout_is_protected(tmp_path: Path) -> None:
    artifacts, env = _sandbox(tmp_path)
    out = artifacts / "desk-longdated-capture"
    out.mkdir()
    (out / "masters").symlink_to(artifacts.parent / "data" / "bars")
    rc, calls, _err = _run(env)
    assert rc == 64 and calls == []
