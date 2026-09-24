"""Desk D5 enforcement: refuted research families can never point a trade,
and desk/ has no route into the research program's rule code.

LEDGER_REFUTED is written from artifacts/paper-trades/RESEARCH-LEDGER.md
(2026-09-11, revised 09-12 and 09-23; sections DEFLATED and DEAD), PEAD-SIGN.md
(misses), the plan (the gated short-vol lane) and the semi-reversion
investigation (REFUTED). It is the test's own list: each family must be
in signals.BANNED, refused by require_direction_signal, refused by the
playbook loader in a row, and unable to make the regime point a name.
"""

from __future__ import annotations

import ast
import copy
import hashlib
import re
import tomllib
from datetime import date
from pathlib import Path

import pytest

from tree_options.desk import playbook, regime, signals

REPO = Path(__file__).resolve().parents[2]
DESK_SRC = REPO / "src" / "tree_options" / "desk"
PB_FILES = tuple(REPO / "data" / "desk" / "playbook" / f for f in ("v1.toml", "v2.toml"))

# ledger family (where it died) -> the desk's banned name
LEDGER_REFUTED: dict[str, str] = {
    "time-series momentum raw mom60-h60 (DEFLATED: HORIZON-001 + MOM60-REPL, beta)": "mom60_h60",
    "time-series momentum, every horizon (DEFLATED: 7/8 cells under the base)": "ts_momentum",
    "SQUEEZE (DEFLATED: conditionals -0.80%..+0.18%)": "squeeze",
    "EXEC-001 open-gap entry (DEFLATED: no gap-capture alpha)": "exec_open_gap",
    "GAPS fade (DEFLATED: null vs base)": "gap_fade",
    "GAPS continuation (DEFLATED: dead holdout)": "gap_cont",
    "GATE-001 breadth gates (DEAD: no gate lifts a rule to t>=2)": "breadth_gate",
    "sector rotation (DEAD: SECT-ROT, all beta)": "sector_rotation",
    "XSMOM 60-skip5 forms (DEAD: XU-XSMOM spread inverted)": "xsmom_60skip5",
    "shorts at every horizon (DEAD)": "short_any",
    "the XSMOM short leg (DEAD)": "xsmom_short_leg",
    "MR h1 (DEAD: OOS-2021 holdout)": "mr_h1",
    "R3f+up (DEAD: regime-dependent)": "r3f_up",
    "R3f (DEAD: regime-dependent)": "r3f",
    "R1 (DEAD: regime-dependent)": "r1",
    "VOLSPIKE (DEAD: regime-dependent)": "volspike",
    "continuation rule (DEAD: CRON-paper-engine dead rules)": "cont",
    "SWEEP-001/002 divergence -> swing (DEAD: 1,008 cells, zero passers)": "sweep_divergence_swing",
    "backtest.py MOM top-tercile (DATA INTEGRITY: coded as the bottom 10)": "mom_top_tercile",
    "PEAD on misses (PEAD-SIGN: +2 USD/card)": "pead_miss",
    "the gated short-vol lane (plan D5: refuted)": "short_vol_gated",
    "semi-basket reversion (investigation CLOSED, REFUTED)": "semi_reversion",
}


class TestRefutedFamilies:
    def test_every_ledger_family_is_banned(self) -> None:
        missing = {fam: key for fam, key in LEDGER_REFUTED.items() if key not in signals.BANNED}
        assert not missing, missing
        assert not signals.ALLOWED_DIRECTION & set(LEDGER_REFUTED.values())

    @pytest.mark.parametrize("key", sorted(set(LEDGER_REFUTED.values())))
    def test_refused_at_every_door(self, key: str) -> None:
        with pytest.raises(signals.BannedSignalError, match="banned"):
            signals.require_direction_signal(key)
        for path in PB_FILES:
            doc = tomllib.loads(path.read_text())
            for row in doc["rows"]:
                if row["when"]["direction"] == "bull":
                    bad = copy.deepcopy(doc)
                    (r,) = [x for x in bad["rows"] if x["id"] == row["id"]]
                    r["when"]["signals"] = [key]
                    with pytest.raises(playbook.PlaybookError, match="banned"):
                        playbook.parse_playbook(bad, sha256="0" * 64)

    def test_a_signals_file_cannot_point_with_a_refuted_family(self) -> None:
        session = date(2026, 6, 1)
        doc = {
            "session": session.isoformat(),
            "xsmom": {"fires": False, "top3": ["AMD", "INTC", "AVGO"]},
            "pead": [],
            **{
                key: {"fires": True, "top3": ["KO"], "names": ["KO"]}
                for key in LEDGER_REFUTED.values()
            },
        }
        d = regime.directions(doc, session)
        assert d.status == "ok" and d.bull == {}
        assert {d.of(n) for n in ("KO", "AMD", "INTC")} == {"none"}


class TestPreregistrationSeals:
    def test_sealed_desk_documents_match_their_sidecars(self) -> None:
        """A pre-registration is never edited after its seal (DESK-BT-001
        is sealed before it runs; IVHIST-001/FORECAST-001 before they ran)."""
        docs = REPO / "docs" / "desk"
        sealed = sorted(docs.glob("*.md.sha256"))
        assert {p.name for p in sealed} >= {
            "DESK-BT-001.md.sha256",
            "FORECAST-001.md.sha256",
            "IVHIST-001.md.sha256",
        }
        for side in sealed:
            doc = side.with_suffix("")
            want, name = side.read_text().split()[:2]
            assert name == doc.name
            assert hashlib.sha256(doc.read_bytes()).hexdigest() == want, doc.name

    def test_desk_bt_001_is_amended_never_edited(self) -> None:
        """Codex P1-3: the original entered at the signal session's own VWAP
        (before a close-based signal is observable). A separately sealed
        amendment moves entry to the next session; the original's bytes
        stay as sealed and the amendment cites them by sha256."""
        docs = REPO / "docs" / "desk"
        original = hashlib.sha256((docs / "DESK-BT-001.md").read_bytes()).hexdigest()
        assert original == "e3e1d12d75b043c6f344c928290f5f5a813c22a8df327de6c28e2d9943139f77"
        amendment = docs / "DESK-BT-001-AMENDMENT-1.md"
        text = amendment.read_text()
        assert original in text
        assert (docs / "DESK-BT-001-AMENDMENT-1.md.sha256").exists()
        for words in ("next NYSE session", "signal session", "placebo", "MODELED", "E0"):
            assert words in text


class TestNoRouteIntoResearchCode:
    def _trees(self) -> list[tuple[str, ast.AST]]:
        return [(py.name, ast.parse(py.read_text())) for py in sorted(DESK_SRC.rglob("*.py"))]

    def test_no_dynamic_imports(self) -> None:
        """The import allowlist (test_desk_signals) sees static imports only;
        desk/ must not load code by path or name at run time either."""
        banned_names = {
            "importlib",
            "runpy",
            "__import__",
            "exec",
            "eval",
            "spec_from_file_location",
            "load_module",
        }
        offenders = []
        for name, tree in self._trees():
            for node in ast.walk(tree):
                ident = (
                    node.id
                    if isinstance(node, ast.Name)
                    else node.attr
                    if isinstance(node, ast.Attribute)
                    else None
                )
                if ident in banned_names:
                    offenders.append(f"{name}:{node.lineno} {ident}")
                if isinstance(node, ast.Attribute) and node.attr == "path":
                    parent = node.value
                    if isinstance(parent, ast.Name) and parent.id == "sys":
                        offenders.append(f"{name}:{node.lineno} sys.path")
        assert not offenders, offenders

    def test_the_only_research_script_run_is_the_data_fetcher(self) -> None:
        """desk/ names exactly one script file: fetch_ohlc.py (the panel
        fetcher, run as a subprocess). No rule script (backtest.py,
        iter00x.py, ...) is ever named as a path."""
        script = re.compile(r"^[\w./-]+\.py$")
        named = set()
        for _name, tree in self._trees():
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    if script.match(node.value):
                        named.add(node.value)
        assert named == {"fetch_ohlc.py"}
