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

import hashlib
import importlib.util
import json
import math
import random
import re
import sys
from pathlib import Path

import numpy as np
import pytest

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
    "2026-07-27", "2026-07-28", "2026-07-29", "2026-07-30", "2026-07-31",
    "2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06", "2026-08-07",
    "2026-08-10", "2026-08-11", "2026-08-12", "2026-08-13", "2026-08-14",
    "2026-08-17", "2026-08-18", "2026-08-19", "2026-08-20", "2026-08-21",
    "2026-08-24", "2026-08-25", "2026-08-26", "2026-08-27", "2026-08-28",
    "2026-08-31", "2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04",
    "2026-09-07", "2026-09-08", "2026-09-09", "2026-09-10", "2026-09-11",
    "2026-09-14", "2026-09-15", "2026-09-16", "2026-09-17", "2026-09-18",
    "2026-09-21", "2026-09-22", "2026-09-23", "2026-09-24", "2026-09-25",
    "2026-09-28", "2026-09-29", "2026-09-30", "2026-10-01", "2026-10-02",
    "2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09",
    "2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16",
    "2026-10-19", "2026-10-20", "2026-10-21", "2026-10-22", "2026-10-23",
    "2026-10-26", "2026-10-27", "2026-10-28", "2026-10-29", "2026-10-30",
    "2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05", "2026-11-06",
    "2026-11-09", "2026-11-10", "2026-11-11", "2026-11-12", "2026-11-13",
    "2026-11-16", "2026-11-17", "2026-11-18", "2026-11-19", "2026-11-20",
    "2026-11-23", "2026-11-24", "2026-11-25", "2026-11-26", "2026-11-27",
    "2026-11-30", "2026-12-01",
]
ENTRY, EXIT = "2026-11-02", "2026-12-01"
WINDOW = SESSIONS[10:70]
ANCHOR = SESSIONS[9]
CARD_FILE = "2026-12-01-xsmom-top3.md"
SEALED_CARD_SHA = "c70acd6883b8b27f89436154f1f080a52143b4911d31b7dbe3408bc070eb1891"
REGIME_TOKENS = ("−1.10%", "+0.78%", "+0.03%")

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


def build_panel(
    *,
    loading: float = 2.0,
    qrets: list[float] | None = None,
    seed: int = 7,
    idio: float = 0.0,
    overrides: dict[tuple[str, int], float] | None = None,
    skip_sessions: dict[str, tuple[str, ...]] | None = None,
) -> dict[str, dict[str, dict[str, str]]]:
    """A synthetic panel: QQQ plus AMD/LLY/INTC at ``loading`` on QQQ, SQQQ at
    -3x with a drag, closes stored as strings. ``overrides[(name, i)]`` sets
    name's return over transition i (SESSIONS[i] -> SESSIONS[i+1]); a return
    of None drops the bar. ``skip_sessions[name]`` deletes those sessions."""
    q = list(qrets or _qrets(seed))
    overrides = overrides or {}
    skip = skip_sessions or {}
    names = ["QQQ", "AMD", "LLY", "INTC", "SQQQ"]
    base = {"QQQ": 400.0, "AMD": 466.84, "LLY": 1410.99, "INTC": 59.97, "SQQQ": 43.55}
    panel: dict[str, dict[str, dict[str, str]]] = {n: {} for n in names}
    px = dict(base)
    rng = random.Random(seed + 1)
    for i, s in enumerate(SESSIONS):
        for n in names:
            c = f"{px[n]:.6f}"
            panel[n][s] = {"open": c, "high": c, "low": c, "close": c, "volume": "1"}
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
    for n, ss in skip.items():
        for s in ss:
            panel[n].pop(s, None)
    return panel


def oracle_slope(panel, names, window):
    """numpy slope of the EW basket's daily simple returns on QQQ's, over the
    consecutive in-window pairs (the test's own pairing, not the module's)."""
    xs, ys = [], []
    for a, b in zip(window, window[1:]):
        xs.append(float(panel["QQQ"][b]["close"]) / float(panel["QQQ"][a]["close"]) - 1)
        ys.append(
            sum(
                float(panel[n][b]["close"]) / float(panel[n][a]["close"]) - 1
                for n in names
            )
            / len(names)
        )
    return float(np.polyfit(np.array(xs), np.array(ys), 1)[0]), len(xs)


def make_paper_tree(tmp_path: Path, panel: dict, *, with_exit: bool = True) -> Path:
    """A tmp paper-trades tree: panel, card-chain ledger, the sealed card file
    (sha patched into the module's pin registry by the caller)."""
    paper = tmp_path / "paper-trades"
    paper.mkdir()
    if not with_exit:
        panel = {
            n: {s: bar for s, bar in rows.items() if s < EXIT}
            for n, rows in panel.items()
        }
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
        "--entry-card", str(paper / CARD_FILE),
        "--paper-dir", str(paper),
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
        assert spec.legs == (("AMD", 466.84, 2500.0),
                             ("LLY", 1410.99, 2500.0),
                             ("INTC", 59.97, 2500.0))

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
        panel = build_panel(
            loading=2.0, skip_sessions={"AMD": (ANCHOR,)}
        )
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
                s: ({"open": "9", "high": "9", "low": "9", "close": "9", "volume": "9"}
                    if s >= ENTRY else bar)
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
        panel = build_panel(
            loading=2.0, skip_sessions={"AMD": (WINDOW[30],)}
        )
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

    def test_card_sha_mismatch_refused_and_writes_nothing(
        self, tmp_path, monkeypatch
    ) -> None:
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
            "# SQQQ-HEDGE-001 twin ledger — prior content marker\n\n"
            "prior-content-marker 0f1d2e3c\n"
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
        row = [ln for ln in (paper / "sqqq-twins" / "LEDGER.md").read_text().splitlines()
               if ln.startswith("| #1 |")]
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
        row = [ln for ln in (paper / "sqqq-twins" / "LEDGER.md").read_text().splitlines()
               if ln.startswith("| #1 |")]
        assert row and "RESOLVED" in row[0] and f"{twin_total:+,.2f}" in row[0]

    def test_round_trip_cost_is_five_bp_on_h_notional(
        self, tmp_path, monkeypatch
    ) -> None:
        paper = make_paper_tree(tmp_path, build_panel(loading=2.0))
        patch_card_pin(monkeypatch)
        assert run_generate(paper) == 0
        assert run_resolve(paper) == 0
        text = (paper / "sqqq-twins" / "2026-12-01-sqqq-twin.md").read_text()
        h = float(
            re.search(r"\| h_notional \| \$([\d,]+\.\d{2}) \|", text).group(1).replace(",", "")
        )
        m = re.search(r"hedge 5bp RT on h_notional: −\$([\d,]+\.\d{2})", text)
        assert m and math.isclose(float(m.group(1)), h * 0.0005, abs_tol=0.005)

    def test_resolve_uses_the_sealed_h_not_the_recomputed_beta(
        self, tmp_path, monkeypatch
    ) -> None:
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
        with pytest.raises(sqqq.Refusal, match="2026-12-01-sqqq-twin.md"):
            run_resolve(paper)

    def test_card_sha_reverified_at_resolve(self, tmp_path, monkeypatch) -> None:
        paper = make_paper_tree(tmp_path, build_panel(loading=2.0))
        patch_card_pin(monkeypatch)
        assert run_generate(paper) == 0
        (paper / CARD_FILE).write_text(CARD_TEXT.replace("59.97", "59.98"))
        with pytest.raises(sqqq.Refusal, match="sha256"):
            run_resolve(paper)
