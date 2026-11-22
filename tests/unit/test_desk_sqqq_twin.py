"""SQQQ-HEDGE-001 deterministic twin generator (scripts/desk/sqqq_twin.py).

The oracle is numpy's least-squares slope over the daily simple returns the
test computes itself from the stored close strings (never the module's own
return series). Panels here are synthetic: 92 weekday sessions 2026-07-27..
2026-12-01 with a known loading, written as the panel's string closes, with
the card's real clock (entry 2026-11-02 close -> exit 2026-12-01 close) and
the sealed card's pinned legs (AMD 466.84 / LLY 1410.99 / INTC 59.97,
3 x $2,500). Every filesystem test runs against a tmp paper-trades tree;
the serving checkout is never touched.
"""

from __future__ import annotations

import errno
import hashlib
import importlib.util
import itertools
import json
import math
import os
import random
import re
import shlex
import sys
from pathlib import Path

import numpy as np
import pytest

from tree_options.desk import store as desk_store

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "desk" / "sqqq_twin.py"
SEALED_DOC = REPO / "docs" / "desk" / "SQQQ-HEDGE-001.md"

_spec = importlib.util.spec_from_file_location("sqqq_twin", SCRIPT)
sqqq = importlib.util.module_from_spec(_spec)
sys.modules["sqqq_twin"] = sqqq  # dataclasses needs the module registered
_spec.loader.exec_module(sqqq)

# 92 weekday sessions; entry at index 70, exit at index 91, so the 60-session
# beta window is SESSIONS[10:70] (2026-08-07..2026-10-30 — the real panel's
# own Card-8 window) and SESSIONS[9] is the pre-window anchor session.
SESSIONS = [
    "2026-07-27",
    "2026-07-28",
    "2026-07-29",
    "2026-07-30",
    "2026-07-31",
    "2026-08-03",
    "2026-08-04",
    "2026-08-05",
    "2026-08-06",
    "2026-08-07",
    "2026-08-10",
    "2026-08-11",
    "2026-08-12",
    "2026-08-13",
    "2026-08-14",
    "2026-08-17",
    "2026-08-18",
    "2026-08-19",
    "2026-08-20",
    "2026-08-21",
    "2026-08-24",
    "2026-08-25",
    "2026-08-26",
    "2026-08-27",
    "2026-08-28",
    "2026-08-31",
    "2026-09-01",
    "2026-09-02",
    "2026-09-03",
    "2026-09-04",
    "2026-09-07",
    "2026-09-08",
    "2026-09-09",
    "2026-09-10",
    "2026-09-11",
    "2026-09-14",
    "2026-09-15",
    "2026-09-16",
    "2026-09-17",
    "2026-09-18",
    "2026-09-21",
    "2026-09-22",
    "2026-09-23",
    "2026-09-24",
    "2026-09-25",
    "2026-09-28",
    "2026-09-29",
    "2026-09-30",
    "2026-10-01",
    "2026-10-02",
    "2026-10-05",
    "2026-10-06",
    "2026-10-07",
    "2026-10-08",
    "2026-10-09",
    "2026-10-12",
    "2026-10-13",
    "2026-10-14",
    "2026-10-15",
    "2026-10-16",
    "2026-10-19",
    "2026-10-20",
    "2026-10-21",
    "2026-10-22",
    "2026-10-23",
    "2026-10-26",
    "2026-10-27",
    "2026-10-28",
    "2026-10-29",
    "2026-10-30",
    "2026-11-02",
    "2026-11-03",
    "2026-11-04",
    "2026-11-05",
    "2026-11-06",
    "2026-11-09",
    "2026-11-10",
    "2026-11-11",
    "2026-11-12",
    "2026-11-13",
    "2026-11-16",
    "2026-11-17",
    "2026-11-18",
    "2026-11-19",
    "2026-11-20",
    "2026-11-23",
    "2026-11-24",
    "2026-11-25",
    "2026-11-26",
    "2026-11-27",
    "2026-11-30",
    "2026-12-01",
]
ENTRY, EXIT = "2026-11-02", "2026-12-01"
WINDOW = SESSIONS[10:70]
ANCHOR = SESSIONS[9]
CARD_FILE = "2026-12-01-xsmom-top3.md"
SEALED_CARD_SHA = "c70acd6883b8b27f89436154f1f080a52143b4911d31b7dbe3408bc070eb1891"
# the sealed regime table's minus signs are U+2212 (the artifact bytes carry them)
REGIME_TOKENS = ("−1.10%", "+0.78%", "+0.03%")  # noqa: RUF001 — sealed regime-table bytes (U+2212)

CARD_TEXT = f"""# Card 8 - XSMOM-TOP3 monthly: AMD + LLY + INTC (entry {ENTRY} close -> exit {EXIT} close)

SEALED 2026-11-14 (synthetic stand-in with the sealed card's structure and
pinned legs; used only inside tmp paper trees by this test module).

## Entries (sealed)
| name | signal at {ENTRY} | entry ({ENTRY} close) | notional | exit |
|---|---|---|---|---|
| AMD | rank 1/36, 273-session return +184.64% | 466.84 | $2,500 LONG | {EXIT} close |
| LLY | rank 2/36, 273-session return +70.94% | 1410.99 | $2,500 LONG | {EXIT} close |
| INTC | rank 3/36, 273-session return +66.86% | 59.97 | $2,500 LONG | {EXIT} close |

## Exit policy (sealed with entry - variant xsmom-monthly)
xsmom-monthly: exit at the 20th-session close after entry ({EXIT}). Parameters frozen at seal.
"""


# ----------------------------------------------------------------- fixtures


def _qrets(seed: int = 7) -> list[float]:
    rng = random.Random(seed)
    return [rng.gauss(0.0, 0.01) for _ in range(len(SESSIONS) - 1)]


ENTRY_IDX = SESSIONS.index(ENTRY)
# the sealed card's entry prices ARE the panel's entry-session closes (the
# real panel: AMD 466.84 / LLY 1410.99 / INTC 59.97 at 2026-11-02)
PINNED_ENTRY = {"AMD": 466.84, "LLY": 1410.99, "INTC": 59.97}


def build_panel(
    *,
    loading: float = 2.0,
    qrets: list[float] | None = None,
    seed: int = 7,
    idio: float = 0.0,
    overrides: dict[tuple[str, int], float] | None = None,
    skip_sessions: dict[str, tuple[str, ...]] | None = None,
    sqqq_entry: float | None = None,
) -> dict[str, dict[str, dict[str, str]]]:
    """A synthetic panel: QQQ plus AMD/LLY/INTC at ``loading`` on QQQ, SQQQ at
    -3x with a drag, closes stored as strings. ``overrides[(name, i)]`` sets
    name's return over transition i (SESSIONS[i] -> SESSIONS[i+1]); a return
    of None drops the bar. ``skip_sessions[name]`` deletes those sessions.
    Each card leg's path is rescaled so its ENTRY close is the sealed card's
    entry price (``--resolve`` cross-checks every leg's entry basis); a
    uniform rescale leaves every return — hence beta — unchanged.
    ``sqqq_entry`` rescales SQQQ the same way (e.g. a >= $1,000 entry)."""
    q = list(qrets or _qrets(seed))
    overrides = overrides or {}
    skip = skip_sessions or {}
    names = ["QQQ", "AMD", "LLY", "INTC", "SQQQ"]
    base = {"QQQ": 400.0, "AMD": 466.84, "LLY": 1410.99, "INTC": 59.97, "SQQQ": 43.55}
    path: dict[str, list[float]] = {n: [] for n in names}
    px = dict(base)
    rng = random.Random(seed + 1)
    for i in range(len(SESSIONS)):
        for n in names:
            path[n].append(px[n])
        if i == len(SESSIONS) - 1:
            break
        qm = q[i]
        for n in names:
            if n == "QQQ":
                r = qm
            elif n == "SQQQ":
                r = -3.0 * qm - 0.0013  # the measured 0.13%/day drag
            else:
                r = loading * qm + (rng.gauss(0.0, idio) if idio else 0.0)
            r = overrides.get((n, i), r)
            if r is not None:
                px[n] *= 1.0 + r
    pins = dict(PINNED_ENTRY)
    if sqqq_entry is not None:
        pins["SQQQ"] = sqqq_entry
    for n, pin in pins.items():
        scale = pin / path[n][ENTRY_IDX]
        path[n] = [v * scale for v in path[n]]
    panel: dict[str, dict[str, dict[str, str]]] = {n: {} for n in names}
    for n in names:
        for s, v in zip(SESSIONS, path[n], strict=True):
            c = f"{v:.6f}"
            panel[n][s] = {"open": c, "high": c, "low": c, "close": c, "volume": "1"}
    for n, ss in skip.items():
        for s in ss:
            panel[n].pop(s, None)
    return panel


def oracle_slope(panel, names, window):
    """numpy slope of the EW basket's daily simple returns on QQQ's, over the
    consecutive in-window pairs (the test's own pairing, not the module's)."""
    xs, ys = [], []
    for a, b in itertools.pairwise(window):
        xs.append(float(panel["QQQ"][b]["close"]) / float(panel["QQQ"][a]["close"]) - 1)
        ys.append(
            sum(float(panel[n][b]["close"]) / float(panel[n][a]["close"]) - 1 for n in names)
            / len(names)
        )
    return float(np.polyfit(np.array(xs), np.array(ys), 1)[0]), len(xs)


def make_paper_tree(tmp_path: Path, panel: dict, *, with_exit: bool = True) -> Path:
    """A tmp paper-trades tree: panel, card-chain ledger, the sealed card file
    (sha patched into the module's pin registry by the caller)."""
    paper = tmp_path / "paper-trades"
    paper.mkdir()
    if not with_exit:
        panel = {n: {s: bar for s, bar in rows.items() if s < EXIT} for n, rows in panel.items()}
    (paper / "ohlc-panel.json").write_text(json.dumps(panel))
    (paper / "ohlc-panel.json.lock").write_text("")  # the panel reader's lock
    (paper / CARD_FILE).write_text(CARD_TEXT)
    (paper / "LEDGER.md").write_text(
        "# Paper-trading ledger — card chain (sealed; READ-ONLY for the twin)\n\n"
        "| 7 | 2026-09-15-r3fup-tsla.md (d23c4ad0…) | R3f+up | … |\n\n"
        "Card 8 — XSMOM-TOP3 monthly AMD+LLY+INTC (sealed 2026-11-14, sha "
        "c70acd6883b8b27f89436154f1f080a52143b4911d31b7dbe3408bc070eb1891): "
        "resolves 2026-12-01.\n"
    )
    (paper / "2026-09-15-r3fup-tsla.md").write_text("# Card 7 (sealed stand-in)\n")
    return paper


def patch_card_pin(monkeypatch, card_text: str = CARD_TEXT) -> None:
    sha = hashlib.sha256(card_text.encode()).hexdigest()
    monkeypatch.setattr(sqqq, "CARD_PINS", {CARD_FILE: sha})


def sha_map(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def run_generate(paper: Path, *, write: bool = True) -> int:
    argv = ["--entry-card", str(paper / CARD_FILE), "--paper-dir", str(paper)]
    if write:
        argv.append("--write")
    return sqqq.main(argv)


def run_resolve(paper: Path, *, write: bool = True) -> int:
    argv = [
        "--entry-card",
        str(paper / CARD_FILE),
        "--paper-dir",
        str(paper),
        "--resolve",
    ]
    if write:
        argv.append("--write")
    return sqqq.main(argv)


# ------------------------------------------------------------- sealed pins


class TestSealedPins:
    def test_card_pin_is_the_sealed_sha(self) -> None:
        assert sqqq.CARD_PINS == {CARD_FILE: SEALED_CARD_SHA}

    def test_regime_table_is_verbatim_from_the_sealed_doc(self) -> None:
        sealed = SEALED_DOC.read_text()
        for line in sqqq.REGIME_TABLE.splitlines():
            assert line in sealed
        assert sqqq.REGIME_TABLE in sealed  # contiguous, in order

    def test_gross_exposure_header_is_verbatim(self) -> None:
        sealed = SEALED_DOC.read_text()
        assert sqqq.GROSS_EXPOSURE_HEADER.splitlines()[0] in sealed


# ----------------------------------------------------------- card parsing


class TestCardParsing:
    def test_parses_legs_and_clock(self, monkeypatch) -> None:
        patch_card_pin(monkeypatch)
        spec = sqqq.parse_card(CARD_TEXT, "2026-12-01-xsmom-top3.md")
        assert spec.entry_date == ENTRY and spec.exit_date == EXIT
        assert spec.legs == (
            ("AMD", 466.84, 2500.0),
            ("LLY", 1410.99, 2500.0),
            ("INTC", 59.97, 2500.0),
        )

    def test_tampered_price_refused(self, monkeypatch) -> None:
        patch_card_pin(monkeypatch)
        bad = CARD_TEXT.replace("466.84", "466.85")
        sha = hashlib.sha256(bad.encode()).hexdigest()
        monkeypatch.setattr(sqqq, "CARD_PINS", {CARD_FILE: sha})
        with pytest.raises(sqqq.Refusal, match="pinned legs"):
            sqqq.parse_card(bad, CARD_FILE)

    def test_short_leg_refused(self, monkeypatch) -> None:
        bad = CARD_TEXT.replace("$2,500 LONG", "$2,500 SHORT")
        sha = hashlib.sha256(bad.encode()).hexdigest()
        monkeypatch.setattr(sqqq, "CARD_PINS", {CARD_FILE: sha})
        with pytest.raises(sqqq.Refusal, match="LONG"):
            sqqq.parse_card(bad, CARD_FILE)


# --------------------------------------------------------------- beta_hat


class TestBetaHat:
    def test_exact_loading_two(self) -> None:
        panel = build_panel(loading=2.0)
        res = sqqq.beta_hat(panel, ("AMD", "LLY", "INTC"), ENTRY)
        assert res.beta is not None and abs(res.beta - 2.0) < 1e-6
        assert res.full_names == ("AMD", "LLY", "INTC")
        assert res.n_returns == 59

    def test_window_is_the_sixty_sessions_before_entry(self) -> None:
        panel = build_panel(loading=1.5, idio=0.004)
        res = sqqq.beta_hat(panel, ("AMD", "LLY", "INTC"), ENTRY)
        assert list(res.window) == WINDOW
        assert res.n_returns == 59  # consecutive in-window pairs

    def test_matches_the_numpy_oracle(self) -> None:
        panel = build_panel(loading=1.5, idio=0.004, seed=13)
        res = sqqq.beta_hat(panel, ("AMD", "LLY", "INTC"), ENTRY)
        want, n = oracle_slope(panel, ["AMD", "LLY", "INTC"], WINDOW)
        assert n == 59
        assert res.beta is not None and abs(res.beta - want) < 1e-9

    def test_window_not_anchored_one_session_earlier(self) -> None:
        """The 60-session window's own pairs (59 returns), NOT the house
        beta.py anchor convention (60 returns incl. the pre-window close).
        The anchor transition (SESSIONS[9]->WINDOW[0]) violates the loading,
        so an anchored implementation must disagree with the oracle."""
        q = _qrets(seed=3)
        overrides = {("QQQ", 9): 0.12, ("AMD", 9): 0.02, ("LLY", 9): 0.02, ("INTC", 9): 0.02}
        panel = build_panel(loading=2.0, qrets=q, overrides=overrides)
        res = sqqq.beta_hat(panel, ("AMD", "LLY", "INTC"), ENTRY)
        want, _ = oracle_slope(panel, ["AMD", "LLY", "INTC"], WINDOW)
        anchored, _ = oracle_slope(panel, ["AMD", "LLY", "INTC"], [ANCHOR, *WINDOW])
        assert abs(anchored - want) > 0.05  # the fixtures genuinely differ
        assert res.beta is not None and abs(res.beta - want) < 1e-9

    def test_missing_pre_window_session_keeps_the_name(self) -> None:
        """The anchor session is OUTSIDE the window: a name without it still
        has a full window (an anchored implementation would drop it)."""
        panel = build_panel(loading=2.0, skip_sessions={"AMD": (ANCHOR,)})
        res = sqqq.beta_hat(panel, ("AMD", "LLY", "INTC"), ENTRY)
        assert res.full_names == ("AMD", "LLY", "INTC")
        assert res.partial_names == ()
        want, _ = oracle_slope(panel, ["AMD", "LLY", "INTC"], WINDOW)
        assert res.beta is not None and abs(res.beta - want) < 1e-9

    def test_no_lookahead(self) -> None:
        panel = build_panel(loading=1.4, idio=0.003)
        before = sqqq.beta_hat(panel, ("AMD", "LLY", "INTC"), ENTRY)
        poisoned = {
            n: {
                s: (
                    {"open": "9", "high": "9", "low": "9", "close": "9", "volume": "9"}
                    if s >= ENTRY
                    else bar
                )
                for s, bar in rows.items()
            }
            for n, rows in panel.items()
        }
        after = sqqq.beta_hat(poisoned, ("AMD", "LLY", "INTC"), ENTRY)
        assert after == before

    def test_beta_above_three_clamps_to_full_book(self) -> None:
        assert sqqq.hedge_notional(4.0) == 7500.0
        assert sqqq.hedge_notional(3.0) == 7500.0  # boundary: cap binds at 3.0
        assert sqqq.hedge_notional(3.0 + 1e-9) == 7500.0

    def test_panel_beta_above_three_clamps_exactly(self) -> None:
        panel = build_panel(loading=4.0)
        res = sqqq.beta_hat(panel, ("AMD", "LLY", "INTC"), ENTRY)
        assert res.beta is not None and res.beta > 3.0
        assert sqqq.hedge_notional(res.beta) == 7500.0  # exactly, for any beta > 3

    def test_beta_at_or_below_zero_is_no_hedge(self) -> None:
        assert sqqq.hedge_notional(-0.4) == 0.0
        assert sqqq.hedge_notional(0.0) == 0.0

    def test_two_of_three_full_windows(self) -> None:
        panel = build_panel(loading=2.0, skip_sessions={"AMD": (WINDOW[30],)})
        res = sqqq.beta_hat(panel, ("AMD", "LLY", "INTC"), ENTRY)
        assert res.full_names == ("LLY", "INTC")
        assert res.partial_names == ("AMD",)
        want, _ = oracle_slope(panel, ["LLY", "INTC"], WINDOW)
        assert res.beta is not None and abs(res.beta - want) < 1e-9

    def test_one_of_three_is_no_hedge(self) -> None:
        panel = build_panel(
            loading=2.0,
            skip_sessions={"AMD": (WINDOW[3],), "LLY": (WINDOW[40],)},
        )
        res = sqqq.beta_hat(panel, ("AMD", "LLY", "INTC"), ENTRY)
        assert res.beta is None
        assert "NO HEDGE" in res.reason
        assert sqqq.hedge_notional(0.0) == 0.0  # the recorded branch


# ---------------------------------------------------------- generate (CLI)


class TestGenerate:
    def test_writes_twin_card_and_ledger_row(self, tmp_path, monkeypatch) -> None:
        paper = make_paper_tree(tmp_path, build_panel(loading=2.0))
        patch_card_pin(monkeypatch)
        assert run_generate(paper) == 0
        twin = paper / "sqqq-twins" / "2026-12-01-sqqq-twin.md"
        ledger = paper / "sqqq-twins" / "LEDGER.md"
        assert twin.is_file() and ledger.is_file()
        text = twin.read_text()
        # provenance: card sha, panel sha, generator sha
        card_sha = hashlib.sha256(CARD_TEXT.encode()).hexdigest()
        panel_sha = hashlib.sha256((paper / "ohlc-panel.json").read_bytes()).hexdigest()
        assert card_sha in text and panel_sha in text
        assert hashlib.sha256(SCRIPT.read_bytes()).hexdigest() in text
        # the rule block + clocks travel with the artifact
        assert "min( max(beta_hat, 0) , 3.0 )" in text
        assert "twin #1 = Card 8's twin" in text
        row = [ln for ln in ledger.read_text().splitlines() if ln.startswith("| #1 |")]
        assert row and "OPEN" in row[0] and card_sha[:16] in row[0]

    def test_regime_table_travels_with_every_artifact(self, tmp_path, monkeypatch) -> None:
        paper = make_paper_tree(tmp_path, build_panel(loading=2.0))
        patch_card_pin(monkeypatch)
        assert run_generate(paper) == 0
        twin = (paper / "sqqq-twins" / "2026-12-01-sqqq-twin.md").read_text()
        ledger = (paper / "sqqq-twins" / "LEDGER.md").read_text()
        assert sqqq.REGIME_TABLE in twin  # verbatim, byte-for-byte
        for tok in REGIME_TOKENS:
            assert tok in twin and tok in ledger
        assert "+6.5%/yr" in twin and "+6.5%/yr" in ledger
        assert "0.13%/day" in twin and "0.13%/day" in ledger
        for line in sqqq.GROSS_EXPOSURE_HEADER.splitlines():
            assert line in twin

    def test_no_write_flag_writes_nothing(self, tmp_path, monkeypatch) -> None:
        paper = make_paper_tree(tmp_path, build_panel(loading=2.0))
        patch_card_pin(monkeypatch)
        before = sha_map(paper)
        assert run_generate(paper, write=False) == 0
        assert sha_map(paper) == before

    def test_duplicate_generation_refused(self, tmp_path, monkeypatch) -> None:
        paper = make_paper_tree(tmp_path, build_panel(loading=2.0))
        patch_card_pin(monkeypatch)
        assert run_generate(paper) == 0
        with pytest.raises(sqqq.Refusal, match="already exists"):
            run_generate(paper)

    def test_card_sha_mismatch_refused_and_writes_nothing(self, tmp_path, monkeypatch) -> None:
        paper = make_paper_tree(tmp_path, build_panel(loading=2.0))
        # NO pin patch: the module keeps the real sealed pin, the tmp card's
        # sha differs -> refuse before any write
        before = sha_map(paper)
        with pytest.raises(sqqq.Refusal, match="sha256"):
            run_generate(paper)
        assert sha_map(paper) == before

    def test_never_writes_the_card_chain(self, tmp_path, monkeypatch) -> None:
        paper = make_paper_tree(tmp_path, build_panel(loading=2.0))
        patch_card_pin(monkeypatch)
        before = sha_map(paper)
        assert run_generate(paper) == 0
        assert run_resolve(paper) == 0
        after = sha_map(paper)
        new = set(after) - set(before)
        gone = set(before) - set(after)
        assert not gone
        assert new  # the twin artifacts exist...
        assert all(p.startswith("sqqq-twins/") for p in new)  # ...and only there
        for path, sha in before.items():
            assert after[path] == sha, f"incumbent file rewritten: {path}"

    def test_ledger_append_never_overwrites(self, tmp_path, monkeypatch) -> None:
        """The twin ledger is append-only across twins: prior content (header,
        regime marker, earlier rows) must survive a new generation."""
        paper = make_paper_tree(tmp_path, build_panel(loading=2.0))
        twins = paper / "sqqq-twins"
        twins.mkdir()
        (twins / "LEDGER.md").write_text(
            "# SQQQ-HEDGE-001 twin ledger — prior content marker\n\nprior-content-marker 0f1d2e3c\n"
        )
        patch_card_pin(monkeypatch)
        assert run_generate(paper) == 0
        ledger = (twins / "LEDGER.md").read_text()
        assert "prior-content-marker 0f1d2e3c" in ledger  # nothing truncated
        assert ledger.index("prior-content-marker") < ledger.index("| #1 |")

    def test_no_hedge_twin_is_still_recorded(self, tmp_path, monkeypatch) -> None:
        panel = build_panel(
            loading=2.0,
            skip_sessions={"AMD": (WINDOW[3],), "LLY": (WINDOW[40],)},
        )
        paper = make_paper_tree(tmp_path, panel)
        patch_card_pin(monkeypatch)
        assert run_generate(paper) == 0
        twin = (paper / "sqqq-twins" / "2026-12-01-sqqq-twin.md").read_text()
        assert "$0.00" in twin and "NO HEDGE" in twin
        row = [
            ln
            for ln in (paper / "sqqq-twins" / "LEDGER.md").read_text().splitlines()
            if ln.startswith("| #1 |")
        ]
        assert row and "$0.00" in row[0]


# ---------------------------------------------------------- resolve (CLI)


class TestResolve:
    def test_refuses_before_the_exit_close_exists(self, tmp_path, monkeypatch) -> None:
        paper = make_paper_tree(tmp_path, build_panel(loading=2.0), with_exit=False)
        patch_card_pin(monkeypatch)
        assert run_generate(paper) == 0
        with pytest.raises(sqqq.Refusal, match="2026-12-01"):
            run_resolve(paper)

    def test_resolution_arithmetic(self, tmp_path, monkeypatch) -> None:
        panel = build_panel(loading=2.0, seed=5)
        paper = make_paper_tree(tmp_path, panel)
        patch_card_pin(monkeypatch)
        assert run_generate(paper) == 0
        twin_path = paper / "sqqq-twins" / "2026-12-01-sqqq-twin.md"
        h = float(
            re.search(r"\| h_notional \| \$([\d,]+\.\d{2}) \|", twin_path.read_text())
            .group(1)
            .replace(",", "")
        )
        assert run_resolve(paper) == 0
        text = twin_path.read_text()
        assert "## RESOLUTION" in text
        # independent arithmetic from the panel + card
        sq0 = float(panel["SQQQ"][ENTRY]["close"])
        sq1 = float(panel["SQQQ"][EXIT]["close"])
        sqqq_leg = round(h * (sq1 / sq0 - 1), 2)
        gross = sum(
            2500.0 * (float(panel[n][EXIT]["close"]) / px - 1)
            for n, px in (("AMD", 466.84), ("LLY", 1410.99), ("INTC", 59.97))
        )
        card_rt = round(7500.0 * 0.0005, 2)
        unhedged_net = round(gross - card_rt, 2)
        hedge_cost = round(h * 0.0005, 2)
        twin_total = round(unhedged_net + sqqq_leg - hedge_cost, 2)
        assert f"{twin_total:+,.2f}" in text
        assert f"{hedge_cost:,.2f}" in text and f"{card_rt:,.2f}" in text
        row = [
            ln
            for ln in (paper / "sqqq-twins" / "LEDGER.md").read_text().splitlines()
            if ln.startswith("| #1 |")
        ]
        assert row and "RESOLVED" in row[0] and f"{twin_total:+,.2f}" in row[0]

    def test_round_trip_cost_is_five_bp_on_h_notional(self, tmp_path, monkeypatch) -> None:
        paper = make_paper_tree(tmp_path, build_panel(loading=2.0))
        patch_card_pin(monkeypatch)
        assert run_generate(paper) == 0
        assert run_resolve(paper) == 0
        text = (paper / "sqqq-twins" / "2026-12-01-sqqq-twin.md").read_text()
        h = float(
            re.search(r"\| h_notional \| \$([\d,]+\.\d{2}) \|", text).group(1).replace(",", "")
        )
        m = re.search(r"hedge 5bp RT on h_notional: −\$([\d,]+\.\d{2})", text)  # noqa: RUF001
        assert m and math.isclose(float(m.group(1)), h * 0.0005, abs_tol=0.005)
        # the cost is SUBTRACTED in the twin total (SQ-M3's invariant): pin the
        # whole arithmetic — unhedged net + SQQQ leg − hedge RT == twin total  # noqa: RUF003
        cost = float(m.group(1))
        unhedged = float(re.search(r"; net ([+-][\d,]+\.\d{2})", text).group(1).replace(",", ""))
        leg = float(re.search(r"SQQQ leg P&L: ([+-][\d,]+\.\d{2})", text).group(1).replace(",", ""))
        total = float(
            re.search(r"twin total = [^=]+= ([+-][\d,]+\.\d{2}) USD", text)
            .group(1)
            .replace(",", "")
        )
        assert math.isclose(total, round(unhedged + leg - cost, 2), abs_tol=0.005)

    def test_resolve_uses_the_sealed_h_not_the_recomputed_beta(self, tmp_path, monkeypatch) -> None:
        panel = build_panel(loading=2.0, seed=5)
        paper = make_paper_tree(tmp_path, panel)
        patch_card_pin(monkeypatch)
        assert run_generate(paper) == 0
        # post-hoc panel surgery inside the beta window must not move the
        # resolution: h was sealed at generation
        text = (paper / "sqqq-twins" / "2026-12-01-sqqq-twin.md").read_text()
        h_sealed = float(
            re.search(r"\| h_notional \| \$([\d,]+\.\d{2}) \|", text).group(1).replace(",", "")
        )
        surg = json.loads((paper / "ohlc-panel.json").read_text())
        for n in ("AMD", "LLY", "INTC"):
            surg[n][WINDOW[5]]["close"] = "1.0"
        (paper / "ohlc-panel.json").write_text(json.dumps(surg))
        assert run_resolve(paper) == 0
        resolved = (paper / "sqqq-twins" / "2026-12-01-sqqq-twin.md").read_text()
        sq0 = float(surg["SQQQ"][ENTRY]["close"])
        sq1 = float(surg["SQQQ"][EXIT]["close"])
        want_leg = round(h_sealed * (sq1 / sq0 - 1), 2)
        assert f"{want_leg:+,.2f}" in resolved

    def test_duplicate_resolution_refused(self, tmp_path, monkeypatch) -> None:
        paper = make_paper_tree(tmp_path, build_panel(loading=2.0))
        patch_card_pin(monkeypatch)
        assert run_generate(paper) == 0
        assert run_resolve(paper) == 0
        with pytest.raises(sqqq.Refusal, match="RESOLUTION"):
            run_resolve(paper)

    def test_resolve_refused_without_generation(self, tmp_path, monkeypatch) -> None:
        paper = make_paper_tree(tmp_path, build_panel(loading=2.0))
        patch_card_pin(monkeypatch)
        with pytest.raises(sqqq.Refusal, match=r"2026-12-01-sqqq-twin\.md"):
            run_resolve(paper)

    def test_card_sha_reverified_at_resolve(self, tmp_path, monkeypatch) -> None:
        paper = make_paper_tree(tmp_path, build_panel(loading=2.0))
        patch_card_pin(monkeypatch)
        assert run_generate(paper) == 0
        (paper / CARD_FILE).write_text(CARD_TEXT.replace("59.97", "59.98"))
        with pytest.raises(sqqq.Refusal, match="sha256"):
            run_resolve(paper)


# ------------------------------- resolve: validate before write (WT2-c)


def _twin_paths(paper: Path) -> tuple[Path, Path]:
    twins = paper / "sqqq-twins"
    return twins / "2026-12-01-sqqq-twin.md", twins / "LEDGER.md"


def _rewrite_panel(paper: Path, edit) -> None:
    panel = json.loads((paper / "ohlc-panel.json").read_text())
    edit(panel)
    (paper / "ohlc-panel.json").write_text(json.dumps(panel))


def _generated(tmp_path: Path, monkeypatch, **panel_kw) -> Path:
    paper = make_paper_tree(tmp_path, build_panel(**{"loading": 2.0, **panel_kw}))
    patch_card_pin(monkeypatch)
    assert run_generate(paper) == 0
    return paper


def _partial_then_enospc(path: Path, data: bytes) -> None:
    """What the house tmp writer (store._write_tmp) leaves behind on a full
    disk: a partial tmp file, then ENOSPC."""
    desk_store._tmp_for(path).write_bytes(data[: len(data) // 2])
    raise OSError(errno.ENOSPC, "No space left on device")


class TestResolveValidatesBeforeWrite:
    """``--resolve`` validates everything before its first write (a refusal
    leaves every file byte-identical), cross-checks every card leg's entry
    basis, and replaces the twin ledger by atomic rename."""

    def test_missing_ledger_refuses_with_nothing_written(self, tmp_path, monkeypatch) -> None:
        paper = _generated(tmp_path, monkeypatch)
        twin, ledger = _twin_paths(paper)
        ledger.unlink()
        before = sha_map(paper)
        with pytest.raises(sqqq.Refusal, match=r"LEDGER\.md does not exist"):
            run_resolve(paper)
        assert sha_map(paper) == before
        assert "## RESOLUTION" not in twin.read_text()

    def test_non_open_ledger_row_refuses_with_nothing_written(self, tmp_path, monkeypatch) -> None:
        paper = _generated(tmp_path, monkeypatch)
        twin, ledger = _twin_paths(paper)
        text = ledger.read_text()
        # the shape a resolved row really has: identity cells kept (F-C3)
        closed = re.sub(
            r"\| OPEN \| — \| — \|",
            f"| RESOLVED {EXIT} | +1.00 | +1.00 |",
            text,
        )
        assert closed != text
        ledger.write_text(closed)
        before = sha_map(paper)
        with pytest.raises(sqqq.Refusal, match="no OPEN ledger row"):
            run_resolve(paper)
        assert sha_map(paper) == before
        assert "## RESOLUTION" not in twin.read_text()

    @pytest.mark.parametrize("leg", ["AMD", "LLY", "INTC"])
    def test_leg_split_rebase_refuses_with_nothing_written(
        self, tmp_path, monkeypatch, leg
    ) -> None:
        """A 2:1 split between entry and exit with the incumbent panel
        rebased: exit_px on the new basis, the sealed entry_px on the old
        one — the leg P&L would be off by the split ratio, so refuse."""
        paper = _generated(tmp_path, monkeypatch)

        def rebase(panel) -> None:
            for bar in panel[leg].values():
                for k in ("open", "high", "low", "close"):
                    bar[k] = f"{float(bar[k]) / 2.0:.6f}"

        _rewrite_panel(paper, rebase)
        before = sha_map(paper)
        with pytest.raises(sqqq.Refusal, match=rf"panel {leg} close at {ENTRY} .*different bases"):
            run_resolve(paper)
        assert sha_map(paper) == before

    def test_one_cent_leg_drift_refuses(self, tmp_path, monkeypatch) -> None:
        paper = _generated(tmp_path, monkeypatch)
        _rewrite_panel(paper, lambda p: p["AMD"][ENTRY].update(close="466.850000"))
        before = sha_map(paper)
        with pytest.raises(sqqq.Refusal, match=r"\(\$466\.85\) no longer matches .*\$466\.84"):
            run_resolve(paper)
        assert sha_map(paper) == before

    def test_leg_without_an_entry_session_close_refuses(self, tmp_path, monkeypatch) -> None:
        # the beta window ends BEFORE entry, so generation still succeeds
        paper = _generated(tmp_path, monkeypatch, skip_sessions={"INTC": (ENTRY,)})
        before = sha_map(paper)
        with pytest.raises(
            sqqq.Refusal, match=f"no usable INTC entry-session close at session {ENTRY}"
        ):
            run_resolve(paper)
        assert sha_map(paper) == before

    def test_same_cents_compares_parsed_decimals(self) -> None:
        assert sqqq.same_cents(1410.99, "1410.99")
        assert sqqq.same_cents(1410.99, "1,410.99")
        assert sqqq.same_cents(12345.67, "12,345.67")
        assert sqqq.same_cents(1410.990001, "1410.99")  # sub-cent noise: same cents
        assert sqqq.same_cents(43.55, "43.55")
        assert not sqqq.same_cents(1411.0, "1410.99")
        assert not sqqq.same_cents(705.495, "1410.99")  # a 2:1 rebase

    def test_lly_leg_over_1000_matches_its_sealed_entry(self, tmp_path, monkeypatch) -> None:
        """LLY's sealed entry is 1410.99: the cents cross-check must MATCH a
        >= $1,000 close (the comma-keeping string compare never could) and
        the leg resolves on the sealed basis."""
        panel = build_panel(loading=2.0)
        assert panel["LLY"][ENTRY]["close"] == "1410.990000"
        paper = make_paper_tree(tmp_path, panel)
        patch_card_pin(monkeypatch)
        assert run_generate(paper) == 0
        assert run_resolve(paper) == 0
        twin, _ = _twin_paths(paper)
        lly_exit = float(panel["LLY"][EXIT]["close"])
        assert f"| LLY (card) | 1410.99 | {lly_exit:.2f} |" in twin.read_text()

    def test_sqqq_entry_over_1000_matches_the_sealed_twin_card(self, tmp_path, monkeypatch) -> None:
        paper = _generated(tmp_path, monkeypatch, sqqq_entry=1234.56)
        twin, _ = _twin_paths(paper)
        assert "| SQQQ entry close | $1,234.56 |" in twin.read_text()
        assert run_resolve(paper) == 0
        assert "| SQQQ (hedge) | 1234.56 |" in twin.read_text()

    def test_ledger_rewrite_is_an_atomic_rename(self, tmp_path, monkeypatch) -> None:
        """The ledger is replaced by tmp + rename, never rewritten in place:
        a reader holding the pre-resolve ledger keeps it whole, and the path
        names a new inode."""
        paper = _generated(tmp_path, monkeypatch)
        _, ledger = _twin_paths(paper)
        pre = ledger.read_bytes()
        held = tmp_path / "ledger-held-by-a-reader"  # outside the paper tree
        os.link(ledger, held)
        assert run_resolve(paper) == 0
        assert held.read_bytes() == pre  # the old inode was never written
        assert ledger.stat().st_ino != held.stat().st_ino
        assert b"RESOLVED" in ledger.read_bytes()

    @pytest.mark.parametrize(
        "fault", ["ledger_tmp_enospc", "ledger_rename_fails", "ledger_rename_fails_on_a_full_disk"]
    )
    def test_ledger_write_failure_restores_the_card(self, tmp_path, monkeypatch, fault) -> None:
        """The ledger's write fails at its data write (ENOSPC) or at its
        rename — the latter AFTER the card was renamed into place, and on a
        disk with no room left for a restore write. Every case ends with the
        card back as the SAME file (its original inode: put back by rename,
        never re-written), no tmp debris, and a re-run that resolves."""
        paper = _generated(tmp_path, monkeypatch)
        twin, ledger = _twin_paths(paper)
        before, card_ino = sha_map(paper), twin.stat().st_ino
        real_write, real_replace = desk_store._write_tmp, os.replace
        disk = {"full": False}

        def write(path: Path, data: bytes) -> Path:
            if disk["full"] or (fault == "ledger_tmp_enospc" and path == ledger):
                _partial_then_enospc(path, data)
            return real_write(path, data)

        def replace(src, dst) -> None:
            if fault != "ledger_tmp_enospc" and Path(dst) == ledger:
                disk["full"] = fault == "ledger_rename_fails_on_a_full_disk"
                raise OSError(errno.EIO, "simulated failure of the ledger rename")
            real_replace(src, dst)

        monkeypatch.setattr(desk_store, "_write_tmp", write)
        monkeypatch.setattr(os, "replace", replace)
        with pytest.raises(OSError):
            run_resolve(paper)
        assert sha_map(paper) == before  # the card is back to its exact bytes, no debris
        assert twin.stat().st_ino == card_ino  # ...as the same file, with no data write
        monkeypatch.setattr(desk_store, "_write_tmp", real_write)
        monkeypatch.setattr(os, "replace", real_replace)
        assert run_resolve(paper) == 0  # ...so a re-run is not blocked

    def test_happy_path_card_and_ledger_agree(self, tmp_path, monkeypatch) -> None:
        paper = _generated(tmp_path, monkeypatch, seed=5)
        twin, ledger = _twin_paths(paper)
        card_pre, ledger_pre = twin.read_bytes(), ledger.read_text()
        chain = {k: v for k, v in sha_map(paper).items() if not k.startswith("sqqq-twins/")}
        assert run_resolve(paper) == 0
        card = twin.read_bytes()
        # the RESOLUTION block lands BELOW the generated bytes, exactly once
        assert card.startswith(card_pre) and card.count(b"## RESOLUTION") == 1
        text = card.decode("utf-8")
        total = re.search(r"twin total = [^=]+= ([+-][\d,]+\.\d{2}) USD", text).group(1)
        unhedged = re.search(r"; net ([+-][\d,]+\.\d{2})", text).group(1)
        pre_lines, post_lines = ledger_pre.splitlines(), ledger.read_text().splitlines()
        changed = [(a, b) for a, b in zip(pre_lines, post_lines, strict=True) if a != b]
        assert len(changed) == 1  # only the twin's own row moved
        open_row, resolved_row = changed[0]
        assert "| OPEN | — | — |" in open_row
        # F-C3: the FULL 8-cell row — the OPEN row's identity cells (twin,
        # card + sha256 16, entry → exit, β̂, h_notional) are KEPT, and only
        # status + the two P&L cells resolve, each under its own header
        card_sha = hashlib.sha256(CARD_TEXT.encode()).hexdigest()
        beta, _ = oracle_slope(build_panel(loading=2.0, seed=5), ["AMD", "LLY", "INTC"], WINDOW)
        h = min(max(beta, 0.0), 3.0) / 3.0 * 7500.0
        assert resolved_row == (
            f"| #1 | {CARD_FILE} ({card_sha[:16]}) | {ENTRY} → {EXIT} | {beta:.6f} | "
            f"${h:,.2f} | RESOLVED {EXIT} | {total} | {unhedged} |"
        )
        assert f"{CARD_FILE} ({card_sha[:16]})" in resolved_row  # it still names WHICH card
        assert resolved_row.startswith(open_row.removesuffix("| OPEN | — | — |"))
        header = next(ln for ln in post_lines if ln.startswith("| twin |"))
        assert len(header.strip("|").split("|")) == len(resolved_row.strip("|").split("|")) == 8
        # the sealed card chain is untouched
        after = {k: v for k, v in sha_map(paper).items() if not k.startswith("sqqq-twins/")}
        assert after == chain
        assert sorted(sha_map(paper)) == sorted(
            [*chain, "sqqq-twins/2026-12-01-sqqq-twin.md", "sqqq-twins/LEDGER.md"]
        )

    def test_resolve_dry_run_writes_nothing(self, tmp_path, monkeypatch) -> None:
        paper = _generated(tmp_path, monkeypatch)
        before = sha_map(paper)
        assert run_resolve(paper, write=False) == 0
        assert sha_map(paper) == before

    @pytest.mark.parametrize(
        "change", ["twin_records_another_generator", "resolver_script_changed"]
    )
    def test_generator_hash_mismatch_refuses_with_nothing_written(
        self, tmp_path, monkeypatch, change
    ) -> None:
        """The twin card records the generator's sha256 at the 11-27 write;
        the 12-01 resolve must be the SAME script. A twin that records
        another generator, or a resolver whose own bytes changed after the
        twin write, refuses — naming both hashes — with nothing written."""
        paper = _generated(tmp_path, monkeypatch)
        twin, _ = _twin_paths(paper)
        running = hashlib.sha256(SCRIPT.read_bytes()).hexdigest()
        if change == "twin_records_another_generator":
            recorded, resolver = "0" * 64, running
            text = twin.read_text()
            line = f"- generator: scripts/desk/sqqq_twin.py — sha256 {running}\n"
            assert text.count(line) == 1
            twin.write_text(text.replace(line, line.replace(running, recorded)))
        else:
            edited = tmp_path / "sqqq_twin.py"  # outside the paper tree
            edited.write_bytes(SCRIPT.read_bytes() + b"# edited after the twin write\n")
            monkeypatch.setattr(sqqq, "__file__", str(edited))
            recorded, resolver = running, hashlib.sha256(edited.read_bytes()).hexdigest()
        assert recorded != resolver
        before = sha_map(paper)
        with pytest.raises(
            sqqq.Refusal,
            match=rf"records generator sha256 {recorded} but this resolver is sha256 {resolver}",
        ):
            run_resolve(paper)
        assert sha_map(paper) == before

    def test_twin_without_its_generator_sha_refuses(self, tmp_path, monkeypatch) -> None:
        paper = _generated(tmp_path, monkeypatch)
        twin, _ = _twin_paths(paper)
        text = twin.read_text()
        stripped = re.sub(r"^- generator: [^\n]*\n", "", text, flags=re.MULTILINE)
        assert stripped != text
        twin.write_text(stripped)
        before = sha_map(paper)
        with pytest.raises(sqqq.Refusal, match="cannot re-read the generator sha256"):
            run_resolve(paper)
        assert sha_map(paper) == before


# ----------- the twin card and the twin ledger commit as ONE (WT2-c-fix)


class TestTwinWritesCommitAsOne:
    """generate (the 11-27 entry write) and --resolve (the 12-01 exit write)
    each write the twin card AND the twin ledger. Both new files are written
    and fsynced as tmp files BEFORE EITHER is renamed into place, the two
    renames run back to back, and a failed second rename is rolled back
    without a data write — so no failure the process survives can leave a
    card and a ledger that disagree (F-C1, F-C2)."""

    @pytest.mark.parametrize("stage", ["generate", "resolve"])
    def test_both_tmp_files_are_written_and_fsynced_before_either_rename(
        self, tmp_path, monkeypatch, stage
    ) -> None:
        """Reviewer repro test_B, inverted: every data write AND its fsync
        precede the first rename into either real file, and the two renames
        are adjacent — no data write between them."""
        if stage == "generate":
            paper = make_paper_tree(tmp_path, build_panel(loading=2.0))
            patch_card_pin(monkeypatch)
            run = run_generate
        else:
            paper = _generated(tmp_path, monkeypatch)
            run = run_resolve
        twin, ledger = _twin_paths(paper)
        finals = (twin.name, ledger.name)
        events: list[tuple[str, str]] = []
        writing: list[str] = []
        real_write, real_fsync = desk_store._write_tmp, os.fsync
        real_replace, real_link = os.replace, os.link

        def spy_write(path: Path, data: bytes) -> Path:
            events.append(("write", path.name))
            writing.append(path.name)
            try:
                return real_write(path, data)
            finally:
                writing.pop()

        def spy_fsync(fd: int) -> None:
            events.append(("fsync", writing[-1] if writing else "?"))
            real_fsync(fd)

        def spy_replace(src, dst) -> None:
            events.append(("rename", Path(dst).name))
            real_replace(src, dst)

        def spy_link(src, dst) -> None:
            events.append(("rename", Path(dst).name))  # a publishing link counts
            real_link(src, dst)

        monkeypatch.setattr(desk_store, "_write_tmp", spy_write)
        monkeypatch.setattr(os, "fsync", spy_fsync)
        monkeypatch.setattr(os, "replace", spy_replace)
        monkeypatch.setattr(os, "link", spy_link)
        assert run(paper) == 0
        renames = [i for i, ev in enumerate(events) if ev[0] == "rename" and ev[1] in finals]
        assert [events[i][1] for i in renames] == [twin.name, ledger.name]
        first = renames[0]
        for name in finals:
            assert ("write", name) in events[:first], events
            assert ("fsync", name) in events[:first], events
        assert renames[1] == first + 1, events  # back to back

    def test_enospc_on_the_ledger_and_on_the_restore_leaves_both_files_untouched(
        self, tmp_path, monkeypatch
    ) -> None:
        """Reviewer repro test_A, inverted: the disk fills after the first
        data write, so the ledger's write fails and so would any restore
        write. Both files stay byte-identical (no RESOLUTION beside an OPEN
        row, no tmp debris) and the re-run resolves."""
        paper = _generated(tmp_path, monkeypatch)
        twin, ledger = _twin_paths(paper)
        before = sha_map(paper)
        real = desk_store._write_tmp
        calls: list[Path] = []

        def disk_fills(path: Path, data: bytes) -> Path:
            calls.append(path)
            if len(calls) >= 2:
                _partial_then_enospc(path, data)
            return real(path, data)

        monkeypatch.setattr(desk_store, "_write_tmp", disk_fills)
        with pytest.raises(OSError) as exc:
            run_resolve(paper)
        assert exc.value.errno == errno.ENOSPC
        assert sha_map(paper) == before
        monkeypatch.setattr(desk_store, "_write_tmp", real)
        assert run_resolve(paper) == 0  # not stuck at "already carries a RESOLUTION block"
        assert "## RESOLUTION" in twin.read_text()
        assert "| OPEN |" not in ledger.read_text()

    def test_generate_ledger_write_failure_strands_no_card(self, tmp_path, monkeypatch) -> None:
        """Reviewer repro test_C, inverted: the 11-27 ledger write fails on a
        full disk — through whichever primitive writes it (the house tmp
        writer, or an in-place append). No twin card may exist without its
        ledger row: re-generation and the 12-01 resolve both still work."""
        paper = make_paper_tree(tmp_path, build_panel(loading=2.0))
        patch_card_pin(monkeypatch)
        twin, ledger = _twin_paths(paper)
        before = sha_map(paper)
        real_write, real_open = desk_store._write_tmp, Path.open

        def full_disk_write(path: Path, data: bytes) -> Path:
            if path == ledger:
                _partial_then_enospc(path, data)
            return real_write(path, data)

        def full_disk_open(self, mode="r", *args, **kwargs):
            if self == ledger and any(c in mode for c in "wax+"):
                raise OSError(errno.ENOSPC, "No space left on device")
            return real_open(self, mode, *args, **kwargs)

        monkeypatch.setattr(desk_store, "_write_tmp", full_disk_write)
        monkeypatch.setattr(Path, "open", full_disk_open)
        with pytest.raises(OSError) as exc:
            run_generate(paper)
        assert exc.value.errno == errno.ENOSPC
        assert sha_map(paper) == before  # no card without a row, no tmp debris
        assert not twin.exists()
        monkeypatch.setattr(desk_store, "_write_tmp", real_write)
        monkeypatch.setattr(Path, "open", real_open)
        assert run_generate(paper) == 0
        assert run_resolve(paper) == 0

    def test_generate_ledger_rename_failure_unpublishes_the_card(
        self, tmp_path, monkeypatch
    ) -> None:
        """The card is published, then the ledger rename fails: the card is
        taken back (it did not exist before), so the twin is not stranded."""
        paper = make_paper_tree(tmp_path, build_panel(loading=2.0))
        patch_card_pin(monkeypatch)
        twin, ledger = _twin_paths(paper)
        before = sha_map(paper)
        real_replace = os.replace

        def replace(src, dst) -> None:
            if Path(dst) == ledger:
                raise OSError(errno.EIO, "simulated failure of the ledger rename")
            real_replace(src, dst)

        monkeypatch.setattr(os, "replace", replace)
        with pytest.raises(OSError, match="ledger rename"):
            run_generate(paper)
        assert sha_map(paper) == before
        assert not twin.exists()
        monkeypatch.setattr(os, "replace", real_replace)
        assert run_generate(paper) == 0
        assert run_resolve(paper) == 0

    def test_generate_never_clobbers_a_card_that_appears_mid_write(
        self, tmp_path, monkeypatch
    ) -> None:
        """The old open("x") semantics: a twin card that appears after the
        pre-check (another writer) is never overwritten — refuse, and leave
        both that card and the ledger as they are."""
        paper = make_paper_tree(tmp_path, build_panel(loading=2.0))
        patch_card_pin(monkeypatch)
        twin, ledger = _twin_paths(paper)
        real_write = desk_store._write_tmp

        def racing_writer(path: Path, data: bytes) -> Path:
            if path == ledger and not twin.exists():
                twin.write_bytes(b"another writer's twin card\n")
            return real_write(path, data)

        monkeypatch.setattr(desk_store, "_write_tmp", racing_writer)
        with pytest.raises(sqqq.Refusal, match="already exists"):
            run_generate(paper)
        assert twin.read_bytes() == b"another writer's twin card\n"
        assert not ledger.exists()
        assert sorted(p.name for p in twin.parent.iterdir()) == [twin.name]  # no debris

    @pytest.mark.parametrize("stage", ["generate", "resolve"])
    def test_a_failed_rollback_names_the_one_command_that_recovers(
        self, tmp_path, monkeypatch, stage
    ) -> None:
        """The ledger rename fails AND the card's rollback fails too. The
        commit cannot restore itself, so it raises Stranded naming the exact
        command — and running that command restores the pre-commit state."""
        if stage == "generate":
            paper = make_paper_tree(tmp_path, build_panel(loading=2.0))
            patch_card_pin(monkeypatch)
            run = run_generate
        else:
            paper = _generated(tmp_path, monkeypatch)
            run = run_resolve
        twin, ledger = _twin_paths(paper)
        before = sha_map(paper)
        real_replace, real_unlink = os.replace, Path.unlink

        def replace(src, dst) -> None:
            if Path(dst) == ledger or Path(src).name.endswith(".pre"):
                raise OSError(errno.EIO, "simulated rename failure")
            real_replace(src, dst)

        def unlink(self, missing_ok=False) -> None:
            if self == twin:
                raise OSError(errno.EIO, "simulated unlink failure")
            real_unlink(self, missing_ok=missing_ok)

        monkeypatch.setattr(os, "replace", replace)
        monkeypatch.setattr(Path, "unlink", unlink)
        with pytest.raises(sqqq.Stranded) as exc:
            run(paper)
        assert str(ledger) in str(exc.value)
        command = re.search(r"`([^`]+)`", str(exc.value)).group(1)
        argv = shlex.split(command)
        monkeypatch.setattr(os, "replace", real_replace)
        monkeypatch.setattr(Path, "unlink", real_unlink)
        if stage == "generate":
            assert argv == ["rm", str(twin)]
            twin.unlink()
        else:
            assert argv[0] == "mv" and argv[2] == str(twin)
            os.replace(argv[1], argv[2])
        assert sha_map(paper) == before  # the named command IS the recovery
        assert run(paper) == 0
