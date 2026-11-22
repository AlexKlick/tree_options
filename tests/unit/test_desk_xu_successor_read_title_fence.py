"""WT4-a: READ-1's title fence, isolated from every other fence.

Wave 3 split the card title regex in two: ``_INCUMBENT_TITLE`` (the
incumbent series' loader: ``XSMOM-TOP3`` only) and ``_CARD_TITLE`` (the
default, for the XU shadow book too: ``XSMOM-TOP3`` or ``XSMOM-TOP3-XU``),
and made ``parse_card`` take the pattern from its caller. Two claims follow,
each pinned here on a hand-built card:

* the incumbent loader refuses an ``-XU`` title BY ITS TITLE ALONE — the card
  below carries the incumbent's own legs (every one in the sealed 36, none in
  the XU roster), so no leg/roster check can be what refuses it;
* the default parser still reads both titles (the shadow evaluation parses
  ``-XU`` cards with it).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from tree_options.desk import xu_shadow
from tree_options.desk.universe import XSMOM_TRADABLES

# The one-shot script's sealed path carries hyphens; load it by file location
# under a name of its own (the main READ-1 test module registers another).
_spec = importlib.util.spec_from_file_location(
    "xu_successor_read_title_fence",
    Path(__file__).resolve().parents[2] / "scripts/desk/xu-successor-read.py",
)
xr = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = xr  # dataclasses resolves the module by name
_spec.loader.exec_module(xr)

ENTRY, EXIT = "2026-11-02", "2026-12-01"
INCUMBENT_LEGS = ("AMD", "LLY", "INTC")
XU_LEGS = ("WMT", "WFC", "VZ")
EMPTY_APPENDIX = (
    "| date | card / month | event | class / reason | evidence |\n|---|---|---|---|---|\n"
)


def _card(tag: str, legs: tuple[str, ...]) -> str:
    lines = [
        f"# Card 9 - {tag} monthly: {' + '.join(legs)} (entry {ENTRY} close -> exit {EXIT} close)",
        "",
        "## Entries (sealed)",
        f"| name | signal at {ENTRY} | entry ({ENTRY} close) | notional | exit |",
        "|---|---|---|---|---|",
        *(
            f"| {n} | rank 1/36, 273-session return +1.00% | 100.00 | $2,500 LONG | {EXIT} close |"
            for n in legs
        ),
        "",
        f"## Resolution ({EXIT} - card resolved at the official close)",
        "",
        "| name | entry | fill session | fill price | reason | P&L | hold-20 ref |",
        "|---|---|---|---|---|---|---|",
        *(
            f"| {n} | 100.00 | {EXIT} | 101.0000 | time-stop at horizon close | 0.00 | 0.00 |"
            for n in legs
        ),
    ]
    return "\n".join(lines) + "\n"


def _paper(root: Path, tag: str) -> Path:
    root.mkdir()
    (root / f"{EXIT}-xsmom-top3.md").write_text(_card(tag, INCUMBENT_LEGS))
    (root / "appendix.md").write_text(EMPTY_APPENDIX)
    return root


def test_the_incumbent_title_fence_alone_refuses_an_xu_title_on_incumbent_legs(
    tmp_path,
) -> None:
    # the isolation: these legs are the incumbent's own, never XU-roster names
    assert set(INCUMBENT_LEGS) <= set(XSMOM_TRADABLES)
    assert not set(INCUMBENT_LEGS) & set(xu_shadow.XU_ROSTER)

    misfiled = _paper(tmp_path / "misfiled", "XSMOM-TOP3-XU")
    with pytest.raises(xr.ReadContractError, match=r"unparseable title: .*XSMOM-TOP3-XU"):
        xr.load_incumbent_series(misfiled, misfiled / "appendix.md")

    # the control: the byte-identical card with the incumbent's title loads
    honest = _paper(tmp_path / "honest", "XSMOM-TOP3")
    cards, voids = xr.load_incumbent_series(honest, honest / "appendix.md")
    assert voids == []
    assert [(c.entry, c.exit, tuple(n for n, _ in c.legs)) for c in cards] == [
        (ENTRY, EXIT, INCUMBENT_LEGS)
    ]


@pytest.mark.parametrize(
    ("tag", "legs"), [("XSMOM-TOP3", INCUMBENT_LEGS), ("XSMOM-TOP3-XU", XU_LEGS)]
)
def test_the_candidate_parser_reads_both_titles(tmp_path, tag, legs) -> None:
    path = tmp_path / "card.md"
    path.write_text(_card(tag, legs))

    card = xr.parse_card(path)

    assert (card.entry, card.exit, card.resolved) == (ENTRY, EXIT, True)
    assert tuple(n for n, _ in card.legs) == legs
