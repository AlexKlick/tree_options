"""The cockpit's account-truth claims, pinned at the SOURCE level.

Two things the desk view used to get wrong, and which no route test can
catch because they are strings in the SPA:

1. the canary's flat-book rule covers the CANDIDATE'S OWN UNDERLYING only
   (``supervised_canary.py``, Ruling 3b), so a SPY drill candidate is
   admitted into an account that still holds NVDA legs. The page claimed
   the opposite ("remains blocked until the legacy book is flat"), which is
   both wrong and the sort of sentence an operator reads to justify a GO.
2. the account's exposure is legs and structures, not "1 underlying row".

These assert on the TSX source (always) and on the BUILT asset when one
exists in the working tree, so the claim cannot come back in either place.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PAGE = REPO / "web" / "src" / "components" / "ActionModelPage.tsx"
BANNER = REPO / "web" / "src" / "components" / "AccountExposureBanner.tsx"
STATIC = REPO / "src" / "tree_options" / "trex_web" / "static"

#: the sentence that was wrong, and would be wrong again if restored
FALSE_CLAIM = "remains blocked until the legacy book is flat"


def _source_texts() -> list[Path]:
    return [path for path in (PAGE, BANNER) if path.exists()]


def _built_assets() -> list[Path]:
    if not STATIC.is_dir():
        return []
    return sorted(p for p in STATIC.rglob("*.js") if p.is_file())


def test_the_canary_is_not_claimed_to_be_blocked_by_the_whole_legacy_book():
    page = PAGE.read_text()
    assert FALSE_CLAIM not in page
    # what IS true (Ruling 3b), stated in the operator's terms
    assert "own underlying" in page
    assert "non-flat account" in page


def test_the_account_is_reported_in_legs_and_structures_not_underlying_rows():
    page = PAGE.read_text()
    assert "underlying rows" in page  # the legacy monitor's own count, kept
    # and the account's real shape beside it
    assert "outside_desk_book.structures" in page
    assert "outside_desk_book.legs" in page
    assert "option legs" in page


def test_the_account_banner_is_wired_above_the_desk_book_panel():
    page = PAGE.read_text()
    assert "AccountExposureBanner" in page
    banner_at = page.index("<AccountExposureBanner")
    book_at = page.index("<DeskStatusLines")
    assert banner_at < book_at  # the banner stands above the book panel
    # standing, not dismissible: the component holds no dismissal state and
    # renders no control that could take the banner down
    banner = BANNER.read_text()
    assert "useState" not in banner
    assert "<button" not in banner and "onClick" not in banner


def test_the_built_asset_carries_the_same_claim_when_one_exists():
    """The SPA the server actually serves is the built asset, not the TSX.
    It is gitignored, so a clean checkout has none: the source assertions
    above carry the gate, and this one catches a stale build when present."""
    assets = _built_assets()
    if not assets:
        pytest.skip("no built asset in this tree (run `npm run build` in web/)")
    joined = "\n".join(p.read_text() for p in assets)
    assert FALSE_CLAIM not in joined
    assert "flat desk book is not a flat account" in joined
