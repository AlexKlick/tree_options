"""The XU-SUCCESSOR-001 §4.3 digest-only fence, made mechanical (the prereg's
"assertable by unit test" line; the test belongs to the §6 item-2 change).

Three fences:

1. GREP GATE — the XU namespace markers (``xu-shadow``, ``card-drafts-xu``,
   ``ohlc-panel-xu``) may appear ONLY in the explicit allowlist: the XU
   drafter module + runner, the fetch script, the one-shot read script,
   their tests, the append-only mutant registry (its XU anchors), and the
   two pre-existing research scripts that already referenced the frozen
   panel. Any other desk/digest/roll-up code path touching the XU namespace
   fails this test.
2. IMPORT LINT — the incumbent book's engine modules and every trex_web
   view must not import any xu module (AST walk, no execution).
3. BEHAVIORAL — a drafted session writes EXACTLY ONE file, into the XU
   namespace only, and the draft carries no aggregate (no P&L, cumulative,
   mean or count over cards); plus the A/A twin: re-running the ranking
   reproduces the draft's picks.
"""

from __future__ import annotations

import ast
import json
import re
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from tree_options.desk import signals, xu_shadow

REPO_ROOT = Path(__file__).resolve().parents[2]

MARKERS = ("xu-shadow", "card-drafts-xu", "ohlc-panel-xu")

ALLOWED = {
    "src/tree_options/desk/xu_shadow.py",
    "scripts/desk/fetch_ohlc_xu.py",
    "scripts/desk/xu_shadow_draft.py",
    "scripts/desk/xu-successor-read.py",
    # pre-existing references to the frozen panel (verified 2026-11-16)
    "scripts/gen_trex_calendar.py",
    "scripts/xsmom_12_1_study.py",
    # the append-only mutant registry's XU registrations carry the anchors
    "scripts/mutate.py",
    "tests/unit/test_desk_fetch_ohlc_xu.py",
    "tests/unit/test_desk_xu_shadow.py",
    "tests/unit/test_desk_xu_fence.py",
    "tests/unit/test_desk_xu_successor_read.py",
}

REBALANCE = date(2026, 12, 1)


# --------------------------------------------------------------- 1. grep gate


def test_xu_namespace_markers_appear_only_in_the_allowlist() -> None:
    offenders: dict[str, list[str]] = {}
    for base in (REPO_ROOT / "src", REPO_ROOT / "scripts", REPO_ROOT / "tests"):
        for py in sorted(base.rglob("*.py")):
            rel = py.relative_to(REPO_ROOT).as_posix()
            text = py.read_text(encoding="utf-8")
            hits = [m for m in MARKERS if m in text]
            if hits and rel not in ALLOWED:
                offenders[rel] = hits
    assert not offenders, f"XU namespace escaped its allowlist: {offenders}"


def test_allowlist_entries_that_exist_carry_a_marker() -> None:
    # the allowlist is a grant, not a blind spot: every existing entry (bar
    # the read script's own test, which lands with item 3) carries a marker
    for rel in sorted(ALLOWED - {"tests/unit/test_desk_xu_successor_read.py"}):
        path = REPO_ROOT / rel
        if not path.exists():
            continue
        assert any(m in path.read_text(encoding="utf-8") for m in MARKERS), rel


# ------------------------------------------------------------ 2. import lint


def test_incumbent_engine_and_web_views_import_no_xu_module() -> None:
    guarded = [
        REPO_ROOT / "src/tree_options/desk/eod_equity.py",
        REPO_ROOT / "src/tree_options/desk/card_drafts.py",
        REPO_ROOT / "src/tree_options/desk/bt001.py",
        *sorted((REPO_ROOT / "src/tree_options/trex_web").glob("*.py")),
    ]
    offenders: list[str] = []
    for py in guarded:
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                if any(part == "xu" or part.startswith("xu_") for part in name.split(".")):
                    offenders.append(f"{py.name}: imports {name}")
    assert offenders == []


def test_digest_and_rollup_modules_never_touch_the_xu_namespace() -> None:
    # the digest/roll-up builders specifically: any marker in them is a
    # code path over the XU namespace before READ-1 (already covered by the
    # grep gate; this names the banned surfaces for the reviewer)
    digest_surfaces = [
        *sorted((REPO_ROOT / "src" / "tree_options" / "desk").glob("*.py")),
        REPO_ROOT / "scripts/desk_digest_watch.py",
    ]
    for py in digest_surfaces:
        if py.name == "xu_shadow.py":
            continue
        text = py.read_text(encoding="utf-8")
        assert not any(m in text for m in MARKERS), py.name


# ------------------------------------------------------------ 3. behavioral


def _panel(cal) -> dict[str, dict[str, dict[str, Any]]]:
    sessions = cal.sessions()
    i = sessions.index(REBALANCE)
    window = sessions[i - 299 : i + 1]
    panel: dict[str, dict[str, dict[str, Any]]] = {}
    for idx, name in enumerate(xu_shadow.XU_ROSTER):
        rate = Decimal(1) + Decimal(idx) / Decimal(10_000)
        days = window if name not in xu_shadow.XU_DEAD_SERIES else window[:40]
        panel[name] = {
            d.isoformat(): {"close": str((Decimal(100) * (rate**k)).quantize(Decimal("0.01")))}
            for k, d in enumerate(days)
        }
    return panel


def test_a_drafted_session_writes_exactly_one_xu_namespaced_file(
    static_calendar, tmp_path, monkeypatch
) -> None:
    from scripts.desk import xu_shadow_draft

    panel_path = tmp_path / "ohlc-panel-xu.json"
    panel_path.write_text(json.dumps(_panel(static_calendar)))
    state = tmp_path / "state"

    rc = xu_shadow_draft.main(
        ["--session", REBALANCE.isoformat(), "--panel", str(panel_path), "--state", str(state)]
    )

    assert rc == 0
    xu_dir = state / "card-drafts-xu"
    written = sorted(p.name for p in xu_dir.iterdir())
    assert written == [f"{REBALANCE.isoformat()}-xsmom-top3-xu.md"]  # exactly one
    assert not (state / "card-drafts").exists()  # never the incumbent queue
    text = (xu_dir / written[0]).read_text()
    assert text.isascii()
    # the only dollar figure anywhere is the per-leg notional: nothing summed
    assert set(re.findall(r"\$[0-9][0-9,.]*", text)) == {"$2,500"}
    for banned in ("P&L", "cumulative", "mean", "average", "total"):
        assert banned not in text.lower(), banned


def test_aa_twin_rerunning_the_ranking_reproduces_the_draft(static_calendar) -> None:
    panel = _panel(static_calendar)
    res = signals.xsmom_top3(panel, REBALANCE, static_calendar, names=xu_shadow.XU_ROSTER)
    twin = signals.xsmom_top3(panel, REBALANCE, static_calendar, names=xu_shadow.XU_ROSTER)
    assert res.top3 == twin.top3
    text = xu_shadow.xu_draft(
        panel,
        REBALANCE,
        static_calendar,
        exit_session=date(2026, 12, 29),
        generated_at=datetime(2026, 12, 1, 21, 30),
    )
    assert text is not None
    assert text == xu_shadow.xu_draft(
        panel,
        REBALANCE,
        static_calendar,
        exit_session=date(2026, 12, 29),
        generated_at=datetime(2026, 12, 1, 21, 30),
    )  # A/A: identical inputs, byte-identical draft
    for name in res.top3:
        assert f"| {name} | rank" in text


def test_runner_refuses_with_no_write_when_the_panel_is_short(static_calendar, tmp_path) -> None:
    from scripts.desk import xu_shadow_draft

    panel = _panel(static_calendar)
    del panel["GE"][REBALANCE.isoformat()]
    panel_path = tmp_path / "ohlc-panel-xu.json"
    panel_path.write_text(json.dumps(panel))
    state = tmp_path / "state"

    rc = xu_shadow_draft.main(
        ["--session", REBALANCE.isoformat(), "--panel", str(panel_path), "--state", str(state)]
    )

    assert rc == 3
    assert not state.exists()  # refused: nothing written at all
