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
    # The load-bearing research cache is only ever the seed SOURCE.
    assert re.findall(r"massive-cache(?!-desk)", text.split("set -u", 1)[1]) == [
        "massive-cache",  # SEED_FROM=
        "massive-cache",  # the refusal guard
    ]
    assert '--from-cache "$SEED_FROM" --to-cache "$CACHE"' in text
    for banned in ("m4b-", "artifacts/bars", "data/bars"):
        assert banned not in text.split("set -u", 1)[1]
    assert "--bars-expiries monthly-traded" in text
    assert "--bars-strike-band 2" in text and "--dte-min 90 --dte-max 270" in text
