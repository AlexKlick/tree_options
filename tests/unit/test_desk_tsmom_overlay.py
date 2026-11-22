"""TSMOM-SHIELD-001: the report-only TS-momentum defensive overlay annotation
(docs/desk/TSMOM-SHIELD-001.md, sealed 2026-11-14).

The overlay NEVER points a trade: DEF only scales the would-be SIZE of an
already-fired entry, and everything here writes to a NEW state dir
(``<TREX_DESK_STATE>/overlay/tsmom-shield/``) the sealer never reads
(RT-3 C6b: the sealer stays blind to overlay state). These tests pin:

* the DEF rule ``sign(close_N(t)/close_N(t-60) - 1)`` with t-60 a FIXED
  NYSE session, and the STRICTER hole guard -- a name missing ANY session
  of the 61-session window [t-60, t] is UNKNOWN and takes size 1.0
  (never punitive, never silent);
* would-be sizes in {1.0, 0.5} only (monotone-reduce-only);
* the SPY aggregate recorded descriptive-only, never an input;
* the burned-era construction line (panel sessions <= 2026-09-11 feed DEF
  inputs only; no annotation file pretends to exist for such a session);
* the interim report-only digest at every 10th ENTRY (the prereg pins
  "the card unit is the ENTRY" and rejects the sealed-DOC reading);
* sealer blindness in both directions (import-lint).

Every write goes to tmp TREX_DESK_STATE trees; the live state root, the
real panel and the serving checkout are never touched.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import subprocess
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from tree_options.desk import tsmom_overlay, universe
from tree_options.desk.panel import lock_path
from tree_options.desk.sessions import Calendar

ET = ZoneInfo("America/New_York")
REPO = Path(__file__).resolve().parents[2]
PANEL_END = date(2026, 12, 4)  # a Friday; the fixture panel's last session
ENTRY_NAMES = [n for n in universe.PANEL_NAMES if n != "SPY"]
DIGEST_DIR = "overlay/tsmom-shield/digest"


@pytest.fixture(autouse=True)
def _pinned_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TREX_DESK_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("DESK_PAPER_DIR", str(tmp_path / "paper"))


def _bar(close: Decimal | str) -> dict[str, object]:
    c = str(close)
    return {"open": c, "high": c, "low": c, "close": c, "volume": 1000}


def _growth(name: str) -> Decimal:
    return Decimal(1) + Decimal(universe.PANEL_NAMES.index(name)) / Decimal(100000)


def _write_panel(paper: Path, static_calendar: Calendar, *, n: int = 120) -> Path:
    """39 names of distinct constant growth ending at PANEL_END (like the
    eod-equity fixture, forward-dated past the burned-era cutoff)."""
    cal = static_calendar
    i = cal.ordinal(PANEL_END)
    days = [d.isoformat() for d in cal.sessions()[i - n + 1 : i + 1]]
    panel: dict[str, dict[str, object]] = {}
    for name in universe.PANEL_NAMES:
        px = Decimal(100)
        bars = {}
        for d in days:
            bars[d] = _bar(px)
            px = (px * _growth(name)).quantize(Decimal("0.0001"))
        panel[name] = bars
    paper.mkdir(parents=True, exist_ok=True)
    path = paper / "ohlc-panel.json"
    path.write_text(json.dumps(panel, indent=1, sort_keys=True))
    return path


def _flat_panel(static_calendar: Calendar, *, names=("AAA", "BBB", "CCC", "SPY")) -> dict:
    """Hand panel: every close 100, every window session present."""
    cal = static_calendar
    i = cal.ordinal(PANEL_END)
    days = [d.isoformat() for d in cal.sessions()[i - 61 : i + 1]]
    return {name: {d: _bar(Decimal(100)) for d in days} for name in names}


def _set(panel: dict, name: str, session: date, close: Decimal | str) -> None:
    panel[name][session.isoformat()] = _bar(close)


def _run(
    tmp_path: Path,
    static_calendar: Calendar,
    *,
    session: date,
    entries: list[str] | None = None,
    digest: bool = False,
    panel_path: Path | None = None,
    lock_timeout_s: float = 5.0,
    now: datetime | None = None,
) -> tsmom_overlay.OverlayResult:
    return tsmom_overlay.run_overlay(
        session=session,
        cal=static_calendar,
        state=tmp_path / "state",
        panel_path=panel_path or (tmp_path / "paper" / "ohlc-panel.json"),
        entries=entries or [],
        digest=digest,
        now=now or datetime(2026, 12, 4, 21, 0, tzinfo=ET),
        lock_timeout_s=lock_timeout_s,
    )


def _overlay_file(tmp_path: Path, d: date) -> Path:
    return tmp_path / "state" / "overlay" / "tsmom-shield" / f"{d.isoformat()}.json"


class TestDefRule:
    def test_sign_rule_and_size_mapping(self, static_calendar: Calendar) -> None:
        panel = _flat_panel(static_calendar)
        t = PANEL_END
        cal = static_calendar
        # UP over the window -> DEF +1 -> full size; DOWN -> DEF -1 -> half;
        # EXACTLY flat -> DEF 0 -> not throttled (full size)
        _set(panel, "AAA", t, Decimal(101))
        _set(panel, "BBB", t, Decimal(99))
        states = tsmom_overlay.def_states(panel, t, cal, names=("AAA", "BBB", "CCC"))
        assert (states["AAA"].def_, states["AAA"].reason) == (1, "")
        assert tsmom_overlay.would_be_size(states["AAA"].def_) == 1.0
        assert (states["BBB"].def_, states["BBB"].reason) == (-1, "")
        assert tsmom_overlay.would_be_size(states["BBB"].def_) == 0.5
        assert (states["CCC"].def_, states["CCC"].reason) == (0, "")
        assert tsmom_overlay.would_be_size(states["CCC"].def_) == 1.0
        # unknown is never punitive
        assert tsmom_overlay.would_be_size(None) == 1.0

    def test_offset_is_exactly_sixty_sessions(self, static_calendar: Calendar) -> None:
        """The denominator is the close at t-60 (a fixed NYSE session), not
        t-61: the two probes have opposite signs under the two readings."""
        panel = _flat_panel(static_calendar, names=("AAA", "BBB"))
        t = PANEL_END
        cal = static_calendar
        s = cal.sessions()
        t60, t61 = s[cal.ordinal(t) - 60], s[cal.ordinal(t) - 61]
        # AAA: 99 vs t60=100 is DOWN, vs t61=50 is up  -> DEF -1 pins t-60
        _set(panel, "AAA", t61, Decimal(50))
        _set(panel, "AAA", t60, Decimal(100))
        _set(panel, "AAA", t, Decimal(99))
        # BBB: 99 vs t60=50 is UP, vs t61=100 is down  -> DEF +1 pins t-60
        _set(panel, "BBB", t61, Decimal(100))
        _set(panel, "BBB", t60, Decimal(50))
        _set(panel, "BBB", t, Decimal(99))
        states = tsmom_overlay.def_states(panel, t, cal, names=("AAA", "BBB"))
        assert states["AAA"].def_ == -1
        assert states["BBB"].def_ == 1
        assert states["AAA"].window == (t60, t)
        assert states["AAA"].close_t == Decimal(99)
        assert states["AAA"].close_t_minus_60 == Decimal(100)

    def test_hole_guard_missing_any_window_session_is_unknown(
        self, static_calendar: Calendar
    ) -> None:
        """STRICTER than the burned cells: one missing session ANYWHERE in
        [t-60, t] -- not just a >10-calendar-day gap -- is UNKNOWN, recorded
        as such, and takes the full would-be size."""
        panel = _flat_panel(static_calendar)
        t = PANEL_END
        cal = static_calendar
        s = cal.sessions()
        mid = s[cal.ordinal(t) - 30]
        del panel["AAA"][mid.isoformat()]
        _set(panel, "AAA", t, Decimal(50))  # would be DEF -1 (half) if computed
        states = tsmom_overlay.def_states(panel, t, cal, names=("AAA",))
        st = states["AAA"]
        assert st.def_ is None
        assert st.reason == "hole"
        assert st.missing == (mid,)
        assert tsmom_overlay.would_be_size(st.def_) == 1.0

    def test_missing_t_minus_60_bar_is_unknown(self, static_calendar: Calendar) -> None:
        panel = _flat_panel(static_calendar)
        t = PANEL_END
        cal = static_calendar
        t60 = cal.sessions()[cal.ordinal(t) - 60]
        del panel["AAA"][t60.isoformat()]
        _set(panel, "AAA", t, Decimal(50))
        states = tsmom_overlay.def_states(panel, t, cal, names=("AAA",))
        assert states["AAA"].def_ is None
        assert states["AAA"].missing == (t60,)
        assert tsmom_overlay.would_be_size(states["AAA"].def_) == 1.0

    def test_absent_name_and_missing_session_bar(self, static_calendar: Calendar) -> None:
        panel = _flat_panel(static_calendar, names=("AAA", "BBB"))
        t = PANEL_END
        states = tsmom_overlay.def_states(panel, t, static_calendar, names=("AAA", "BBB", "NOPE"))
        assert states["NOPE"].def_ is None and states["NOPE"].reason == "not_in_panel"
        del panel["BBB"][t.isoformat()]
        states = tsmom_overlay.def_states(panel, t, static_calendar, names=("AAA", "BBB"))
        assert states["BBB"].def_ is None and states["BBB"].reason == "no_session_bar"

    def test_calendar_too_short_is_unknown(self, static_calendar: Calendar) -> None:
        early = static_calendar.sessions()[50]
        panel = {"AAA": {early.isoformat(): _bar(Decimal(100))}}
        states = tsmom_overlay.def_states(panel, early, static_calendar, names=("AAA",))
        assert states["AAA"].def_ is None and states["AAA"].reason == "calendar_too_short"

    def test_spy_aggregate_is_descriptive_only(self, static_calendar: Calendar) -> None:
        panel = _flat_panel(static_calendar)
        t = PANEL_END
        _set(panel, "SPY", t, Decimal(99))
        states = tsmom_overlay.def_states(panel, t, static_calendar, names=("SPY", "AAA"))
        doc = tsmom_overlay.state_doc(states["SPY"])
        assert states["SPY"].def_ == -1  # recorded ...
        assert doc["descriptive_only"] is True  # ... but never an input
        assert tsmom_overlay.state_doc(states["AAA"]).get("descriptive_only") is None


class TestWriter:
    def test_one_json_per_session_with_entries_sizes_and_inputs_sha(
        self, tmp_path: Path, static_calendar: Calendar
    ) -> None:
        panel_path = _write_panel(tmp_path / "paper", static_calendar)
        res = _run(
            tmp_path, static_calendar, session=date(2026, 12, 1), entries=["AMD", "LLY", "INTC"]
        )
        assert (res.exit_code, res.status) == (0, "ok")
        path = _overlay_file(tmp_path, date(2026, 12, 1))
        assert path.is_file()
        doc = json.loads(path.read_text())
        assert doc["schema"] == "desk-tsmom-overlay/1"
        assert doc["prereg"] == "TSMOM-SHIELD-001"
        assert doc["session"] == "2026-12-01"
        rule = doc["rule"]
        assert rule["lookback_sessions"] == 60
        assert rule["burned_era_end"] == "2026-09-11"
        assert rule["hole_guard"] == "missing_any_window_session"
        # inputs sha pins the exact panel bytes the DEF states were read from
        assert doc["panel"]["sha256"] == hashlib.sha256(panel_path.read_bytes()).hexdigest()
        # per-panel-name DEF states: all 39 names, SPY flagged descriptive-only
        assert set(doc["names"]) == set(universe.PANEL_NAMES)
        assert doc["names"]["SPY"]["descriptive_only"] is True
        t60 = static_calendar.sessions()[static_calendar.ordinal(date(2026, 12, 1)) - 60]
        assert doc["names"]["AMD"]["window"] == [t60.isoformat(), "2026-12-01"]
        # per-entry would-be sizes: growth panel -> every DEF +1 -> full size
        assert set(doc["entries"]) == {"AMD", "LLY", "INTC"}
        for _name, entry in doc["entries"].items():
            assert entry["def"] == 1 and entry["would_be_size"] == 1.0
        # writes NOWHERE else: one file under the state root, nothing beside it
        files = sorted(
            p.relative_to(tmp_path / "state")
            for p in (tmp_path / "state").rglob("*")
            if p.is_file()
        )
        assert files == [Path("overlay/tsmom-shield/2026-12-01.json")]

    def test_burned_era_sessions_are_refused(
        self, tmp_path: Path, static_calendar: Calendar
    ) -> None:
        _write_panel(tmp_path / "paper", static_calendar)
        res = _run(tmp_path, static_calendar, session=date(2026, 9, 11), entries=["AMD"])
        assert res.exit_code == 1 and res.status == "burned_era_session"
        assert not _overlay_file(tmp_path, date(2026, 9, 11)).exists()
        assert not (tmp_path / "state").exists()
        # the first session after the cutoff is annotatable, and its look-back
        # reads burned-era closes as INPUTS (the construction warm-up)
        res = _run(tmp_path, static_calendar, session=date(2026, 9, 14))
        assert (res.exit_code, res.status) == (0, "ok")
        doc = json.loads(_overlay_file(tmp_path, date(2026, 9, 14)).read_text())
        w0 = date.fromisoformat(doc["names"]["SPY"]["window"][0])
        assert w0 < date(2026, 9, 11)  # sanctioned: inputs only

    def test_falling_name_gets_half_size_entry(
        self, tmp_path: Path, static_calendar: Calendar
    ) -> None:
        paper = tmp_path / "paper"
        _write_panel(paper, static_calendar)
        path = paper / "ohlc-panel.json"
        panel = json.loads(path.read_text())
        name = ENTRY_NAMES[0]
        px = Decimal(200)  # a strict decline over the whole panel
        for d in sorted(panel[name]):
            panel[name][d] = _bar(px)
            px = (px * Decimal("0.999")).quantize(Decimal("0.0001"))
        path.write_text(json.dumps(panel, indent=1, sort_keys=True))
        res = _run(tmp_path, static_calendar, session=PANEL_END, entries=[name])
        assert res.exit_code == 0
        doc = json.loads(_overlay_file(tmp_path, PANEL_END).read_text())
        assert doc["names"][name]["def"] == -1
        assert doc["entries"][name]["would_be_size"] == 0.5

    def test_unknown_entry_name_is_never_punitive(
        self, tmp_path: Path, static_calendar: Calendar
    ) -> None:
        _write_panel(tmp_path / "paper", static_calendar)
        res = _run(tmp_path, static_calendar, session=PANEL_END, entries=["NOT_A_NAME"])
        assert res.exit_code == 0
        doc = json.loads(_overlay_file(tmp_path, PANEL_END).read_text())
        entry = doc["entries"]["NOT_A_NAME"]
        assert entry["def"] is None and entry["reason"] == "not_in_panel"
        assert entry["would_be_size"] == 1.0

    def test_rerun_merges_entries_and_unchanged_run_writes_nothing(
        self, tmp_path: Path, static_calendar: Calendar
    ) -> None:
        _write_panel(tmp_path / "paper", static_calendar)
        first, second = ENTRY_NAMES[0], ENTRY_NAMES[1]
        _run(tmp_path, static_calendar, session=PANEL_END, entries=[first])
        _run(tmp_path, static_calendar, session=PANEL_END, entries=[second])
        path = _overlay_file(tmp_path, PANEL_END)
        doc = json.loads(path.read_text())
        assert set(doc["entries"]) == {first, second}
        before = path.read_bytes()
        _run(tmp_path, static_calendar, session=PANEL_END, entries=[second])
        assert path.read_bytes() == before  # byte-identical: no rewrite

    def test_not_a_session_panel_missing_locked_and_stale(
        self, tmp_path: Path, static_calendar: Calendar
    ) -> None:
        _write_panel(tmp_path / "paper", static_calendar)
        res = _run(tmp_path, static_calendar, session=date(2026, 12, 5))  # a Saturday
        assert (res.exit_code, res.status) == (0, "not_a_session")
        res = _run(tmp_path, static_calendar, session=PANEL_END, panel_path=tmp_path / "nope.json")
        assert (res.exit_code, res.status) == (1, "panel_missing")
        # a stale panel (nothing for the session) is an error, not 39 unknowns
        stale = _write_panel(tmp_path / "old", static_calendar, n=120)
        old = json.loads(stale.read_text())
        for name in old:
            old[name] = {k: v for k, v in old[name].items() if k <= "2026-11-13"}
        stale.write_text(json.dumps(old, indent=1, sort_keys=True))
        res = _run(tmp_path, static_calendar, session=PANEL_END, panel_path=stale)
        assert (res.exit_code, res.status) == (1, "panel_not_current")
        assert not (tmp_path / "state").exists()

    def test_panel_read_under_the_shared_flock(
        self, tmp_path: Path, static_calendar: Calendar
    ) -> None:
        import fcntl

        paper = tmp_path / "paper"
        panel_path = _write_panel(paper, static_calendar)
        with open(lock_path(panel_path), "a") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)  # a fetch_ohlc.py merge
            res = _run(tmp_path, static_calendar, session=PANEL_END, lock_timeout_s=0.2)
        assert (res.exit_code, res.status) == (3, "panel_locked")
        assert not (tmp_path / "state").exists()


class TestDigest:
    """Interim: report-only digest at every 10 cards -- and the prereg pins
    "the card unit is the ENTRY (per-name card)", rejecting the sealed-DOC
    reading. Implemented: every 10th recorded ENTRY."""

    def test_digest_at_every_tenth_entry_only(
        self, tmp_path: Path, static_calendar: Calendar
    ) -> None:
        paper = tmp_path / "paper"
        _write_panel(paper, static_calendar)
        # one name strictly falling: its entries carry the half size
        path = paper / "ohlc-panel.json"
        panel = json.loads(path.read_text())
        falling = ENTRY_NAMES[3]
        px = Decimal(200)
        for d in sorted(panel[falling]):
            panel[falling][d] = _bar(px)
            px = (px * Decimal("0.999")).quantize(Decimal("0.0001"))
        path.write_text(json.dumps(panel, indent=1, sort_keys=True))

        days = [date(2026, 12, 1), date(2026, 12, 2), date(2026, 12, 3), date(2026, 12, 4)]
        # 9 entries -> no digest even when asked
        for d, names in zip(
            days[:3], (ENTRY_NAMES[0:3], ENTRY_NAMES[3:6], ENTRY_NAMES[6:9]), strict=True
        ):
            res = _run(tmp_path, static_calendar, session=d, entries=names, digest=True)
            assert res.exit_code == 0 and res.digest_boundaries == ()
        assert not (tmp_path / "state" / DIGEST_DIR).exists()
        # the 10th entry lands without --digest: emission waits for a --digest run
        res = _run(tmp_path, static_calendar, session=days[3], entries=[ENTRY_NAMES[9]])
        assert res.digest_boundaries == ()
        assert not (tmp_path / "state" / DIGEST_DIR).exists()
        res = _run(tmp_path, static_calendar, session=days[3], digest=True)
        assert res.digest_boundaries == (10,)
        dig = tmp_path / "state" / DIGEST_DIR / "10.json"
        doc = json.loads(dig.read_text())
        assert doc["schema"] == "desk-tsmom-overlay-digest/1"
        assert doc["entries_covered"] == 10 and doc["entries_total"] == 10
        assert doc["unit"] == "entry"
        assert doc["sizes"] == {"full": 9, "half": 1, "unknown": 0}
        assert doc["on_fraction"] == "0.100000"
        assert doc["session_span"] == ["2026-12-01", "2026-12-04"]
        # the 11th entry: no 20-boundary
        res = _run(
            tmp_path,
            static_calendar,
            session=date(2026, 12, 7),
            entries=[ENTRY_NAMES[10]],
            digest=True,
        )
        assert res.digest_boundaries == ()
        assert sorted(p.name for p in (tmp_path / "state" / DIGEST_DIR).glob("*.json")) == [
            "10.json"
        ]

    def test_digest_is_report_only_and_discloses_the_pair(
        self, tmp_path: Path, static_calendar: Calendar
    ) -> None:
        _write_panel(tmp_path / "paper", static_calendar)
        for i, d in enumerate([date(2026, 12, 1), date(2026, 12, 2), date(2026, 12, 3)]):
            _run(tmp_path, static_calendar, session=d, entries=ENTRY_NAMES[i * 3 : i * 3 + 3])
        _run(tmp_path, static_calendar, session=PANEL_END, entries=[ENTRY_NAMES[9]], digest=True)
        doc = json.loads((tmp_path / "state" / DIGEST_DIR / "10.json").read_text())
        assert doc["report_only"] is True
        assert doc["prereg"] == "TSMOM-SHIELD-001"
        # the pair registers as ONE reporting unit: the joint disclosure
        # sentence appears, and both documents are named
        assert "EB-RECHECK-001" in doc["coupled_pair"]
        assert "TSMOM-SHIELD-001" in doc["coupled_pair"]
        assert doc["disclosure"] == (
            "No independent-evidence claim is made anywhere in this pair: both "
            "estimands descend from the same two 2022 windows of the same frozen "
            "grid on the same panel family, so a joint pass is correlated "
            "evidence about ONE premise, never two confirmations."
        )
        # verdicts are provisional on the premise channel
        assert "PROVISIONAL" in doc["provisional"] and "EB-RECHECK" in doc["provisional"]
        # counts of DEF states only: no outcome/return/statistic interim
        assert not {"returns", "outcomes", "deltas", "verdict"} & set(doc)

    def test_digest_emission_is_idempotent(self, tmp_path: Path, static_calendar: Calendar) -> None:
        _write_panel(tmp_path / "paper", static_calendar)
        for i, d in enumerate([date(2026, 12, 1), date(2026, 12, 2), date(2026, 12, 3)]):
            _run(tmp_path, static_calendar, session=d, entries=ENTRY_NAMES[i * 3 : i * 3 + 3])
        _run(tmp_path, static_calendar, session=PANEL_END, entries=[ENTRY_NAMES[9]], digest=True)
        dig = tmp_path / "state" / DIGEST_DIR / "10.json"
        before = dig.read_bytes()
        res = _run(tmp_path, static_calendar, session=PANEL_END, digest=True)
        assert res.digest_boundaries == () and dig.read_bytes() == before


SRC = REPO / "src" / "tree_options"
SEAL_PATH = [
    SRC / "desk" / "card_drafts.py",
    SRC / "desk" / "eod_equity.py",
    *sorted((SRC / "seal").glob("*.py")),
]
FORBIDDEN_OVERLAY_IMPORTS = {
    "tree_options.desk.card_drafts",
    "tree_options.desk.eod_equity",
    "tree_options.desk.book",
    "tree_options.desk.signals",
    "tree_options.desk.shadows",
    "tree_options.desk.__main__",
    "tree_options.seal",
}
ALLOWED_OVERLAY_IMPORTS = {
    "tree_options.desk.panel",
    "tree_options.desk.sessions",
    "tree_options.desk.store",
    "tree_options.desk.universe",
}


def _imports_of(path: Path) -> set[str]:
    mods: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            mods.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            mods.add(node.module)
    return mods


class TestSealerBlindness:
    def test_seal_path_never_imports_the_overlay(self) -> None:
        offenders = [
            f"{p.name}: {m}"
            for p in SEAL_PATH
            if p.exists()
            for m in _imports_of(p)
            if m == "tree_options.desk.tsmom_overlay" or m.endswith(".tsmom_overlay")
        ]
        assert not offenders, offenders

    def test_overlay_imports_no_sealing_or_state_mutating_module(self) -> None:
        path = SRC / "desk" / "tsmom_overlay.py"
        mods = _imports_of(path)
        tree_mods = {m for m in mods if m.startswith("tree_options")}
        assert tree_mods <= ALLOWED_OVERLAY_IMPORTS, tree_mods - ALLOWED_OVERLAY_IMPORTS
        assert not tree_mods & FORBIDDEN_OVERLAY_IMPORTS


class TestRunner:
    def test_runner_end_to_end(self, tmp_path: Path, static_calendar: Calendar) -> None:
        _write_panel(tmp_path / "paper", static_calendar)
        env = dict(os.environ)
        env["TREX_DESK_STATE"] = str(tmp_path / "state")
        env["DESK_PAPER_DIR"] = str(tmp_path / "paper")
        runner = REPO / "scripts" / "desk" / "tsmom_annotate.py"
        proc = subprocess.run(
            [
                sys.executable,
                str(runner),
                "--session",
                "2026-12-01",
                "--entries",
                "AMD",
                "LLY",
                "INTC",
            ],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
        assert "tsmom-annotate session=2026-12-01 status=ok" in proc.stdout
        assert "entries=3" in proc.stdout and "digest=none" in proc.stdout
        doc = json.loads(_overlay_file(tmp_path, date(2026, 12, 1)).read_text())
        assert set(doc["entries"]) == {"AMD", "LLY", "INTC"}
        # the burned-era refusal through the runner
        proc = subprocess.run(
            [sys.executable, str(runner), "--session", "2026-09-11"],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        assert proc.returncode == 1
        assert "status=burned_era_session" in proc.stdout

    def test_runner_digest_flag_reaches_the_stage(
        self, tmp_path: Path, static_calendar: Calendar
    ) -> None:
        """``--digest`` on the runner reaches ``run_overlay``: ten entries in
        one --digest run cross the 10-entry boundary, so the digest emits."""
        _write_panel(tmp_path / "paper", static_calendar)
        env = dict(os.environ)
        env["TREX_DESK_STATE"] = str(tmp_path / "state")
        env["DESK_PAPER_DIR"] = str(tmp_path / "paper")
        runner = REPO / "scripts" / "desk" / "tsmom_annotate.py"
        proc = subprocess.run(
            [
                sys.executable,
                str(runner),
                "--session",
                "2026-12-01",
                "--entries",
                *ENTRY_NAMES[:10],
                "--digest",
            ],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
        assert "status=ok entries=10 digest=10 exit=0" in proc.stdout
        assert (tmp_path / "state" / DIGEST_DIR / "10.json").is_file()
