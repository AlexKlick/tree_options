"""XSMOM-12-1: the pre-registered true 12-1 momentum study.

Pre-registration: artifacts/paper-trades/XSMOM-12-1-PREREG.md, sha256-sealed
2026-09-23 before any run. This script refuses to run on any other bytes.

Construction under test (true 12-1): on the first NYSE session of each month,
rank by close(t-21)/close(t-273)-1. Comparator (same walk, same code path,
only the numerator changes): the tested no-skip close(t)/close(t-273)-1. Both
rank through ``tree_options.desk.signals.xsmom_rank`` (skip=21 vs skip=0),
whose offsets are NYSE sessions from the static calendar; a name missing any
session inside [t-273, t+hold] is excluded for that month.

Frozen grid, 16 cells per construction and 32 in all: universe {orig-36 (the
ohlc-panel.json tradables, SPY excluded), XU-62 (ohlc-panel-xu.json)} x form
{top3, tercile} x hold {20, 60} x era {holdout: signal <= 2024-09-03; full:
post-holdout only}. Conventions are those of XU-XSMOM.md / iter003: 5bp round
trip, day-clustered t (ddof=1), >= 30-name cross-section, base =
hold-everything on the same walk, cond = mean - base, cand = mean > 0 and
t >= 2 and days >= 30 and cond > 0.

CALENDAR CORRECTION (found during validation, before the scored run): the
static calendar was generated with exchange-calendars 4.5.2, which predates
the NYSE's 2025-01-09 closure (national day of mourning for President
Carter), so it lists that day as a session. Every name in both panels lacks
it (the exchange was shut). Taken literally, the "any missing session
excludes the name" rule would then drop EVERY name from every month whose
274-session window spans 2025-01-09 (about 2025-01 to 2026-02). The study
uses the static calendar minus that one day, so session offsets are true
NYSE offsets and agree with every panel's own rows.

Validation, before trusting it: the same code path with skip=0 on a DAILY
walk must reproduce the published "252-skip21" rows of XU-XSMOM.md and
XSMOM-LONGLEG.md (those rows are the no-skip 273-session construction; see
RESEARCH-LEDGER.md DATA INTEGRITY 2026-09-23), and on the MONTHLY walk the
PROTOCOL-XSMOM.md legs and P&L, on the panels as they stood at publication
(the main panel cut at 2026-09-11).

Usage (read-only on the panels; writes the report and an optional JSON):

    PYTHONPATH=src python scripts/xsmom_12_1_study.py --validate-only
    PYTHONPATH=src python scripts/xsmom_12_1_study.py --json results.json
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import statistics
import subprocess
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT / "src") not in sys.path:  # pragma: no cover - import plumbing
    sys.path.insert(0, str(REPO_ROOT / "src"))

from tree_options.desk import signals  # noqa: E402
from tree_options.desk.sessions import Calendar, is_first_session_of_month  # noqa: E402
from tree_options.time.calendar import CalendarError, NotASessionError  # noqa: E402

DEFAULT_DIR = Path("/home/alexk/documents/tree_options/artifacts/paper-trades")
PREREG = "XSMOM-12-1-PREREG.md"
PREREG_SHA256 = "72210480b030b14e8b42ad1d66ef3773b22a2d8cc23052e3c78a523eccc37e01"
MAIN_PANEL = "ohlc-panel.json"
MAIN_LOCK = "ohlc-panel.json.lock"
XU_PANEL = "ohlc-panel-xu.json"
REPORT = "XSMOM-12-1.md"
PUBLICATION_CUT = "2026-09-11"  # last session in both panels when the legacy rows were written

HOLDOUT_END = "2024-09-03"
RT = 0.0005
NOTIONAL = 2500  # PROTOCOL-XSMOM.md per-leg USD, validation only
MIN_RANKED = signals.XSMOM_MIN_RANKED
LOOKBACK = signals.XSMOM_LOOKBACK
CONSTRUCTIONS: tuple[tuple[str, int], ...] = (("12-1", 21), ("no-skip", 0))
UNIVERSES: tuple[str, ...] = ("orig-36", "XU-62")
FORMS: tuple[str, ...] = ("top3", "tercile")
HOLDS: tuple[int, ...] = (20, 60)
ERAS: tuple[str, ...] = ("holdout", "full")
DSR_N = 32
EULER_GAMMA = 0.5772156649

# NYSE closures the pinned calendar generator (exchange-calendars 4.5.2)
# predates. 2025-01-09: national day of mourning for President Carter.
STATIC_CALENDAR_MISSING_CLOSURES: frozenset[date] = frozenset({date(2025, 1, 9)})

Panel = dict[str, dict[str, dict[str, Any]]]
Trade = tuple[str, float]  # (signal session ISO, gross forward return)
CellKey = tuple[str, int, str]  # (form, hold, era)
CondKey = tuple[str, str, str, int, str]  # (construction, universe, form, hold, era)


class PreregMismatch(RuntimeError):
    """The pre-registration's bytes are not the sealed ones."""


# ---- inputs ------------------------------------------------------------------


def verify_prereg(path: Path, expected: str = PREREG_SHA256) -> str:
    """sha256 of the pre-registration; it must equal both the sealed constant
    and the first token of its `.sha256` sidecar."""
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    sidecar = path.with_name(path.name + ".sha256").read_text().split()
    if digest != expected:
        raise PreregMismatch(f"{path.name}: sha256 {digest} != sealed {expected}")
    if not sidecar or sidecar[0] != digest:
        raise PreregMismatch(f"{path.name}: sidecar does not carry {digest}")
    return digest


def read_panel(path: Path, lock: Path | None = None) -> tuple[Panel, str]:
    """(panel, sha256 of the exact bytes read). With `lock`, the read happens
    under a shared flock (the nightly panel job takes it exclusively)."""
    if lock is None:
        raw = path.read_bytes()
    else:
        fd = os.open(lock, os.O_RDONLY)
        try:
            fcntl.flock(fd, fcntl.LOCK_SH)
            raw = path.read_bytes()
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: panel is not an object")
    return data, hashlib.sha256(raw).hexdigest()


def cut_panel(panel: Mapping[str, Mapping[str, Any]], last: str) -> Panel:
    """The panel as it stood with `last` as its final session."""
    return {n: {d: b for d, b in bars.items() if d <= last} for n, bars in panel.items()}


def panel_range(panel: Mapping[str, Mapping[str, Any]]) -> tuple[str, str]:
    return min(min(b) for b in panel.values() if b), max(max(b) for b in panel.values() if b)


class ClosureCorrectedCalendar:
    """A session calendar minus closures its generator did not know about."""

    def __init__(self, base: Calendar, closed: Iterable[date]) -> None:
        shut = frozenset(closed)
        self._sessions = tuple(s for s in base.sessions() if s not in shut)
        self._ordinals = {s: i for i, s in enumerate(self._sessions)}
        self.removed = tuple(sorted(s for s in shut if base.is_session(s)))

    def sessions(self) -> tuple[date, ...]:
        return self._sessions

    def is_session(self, d: date) -> bool:
        return d in self._ordinals

    def ordinal(self, d: date) -> int:
        try:
            return self._ordinals[d]
        except KeyError:
            raise NotASessionError(f"{d} is not a session") from None

    def nth_after(self, d: date, n: int) -> date:
        i = self.ordinal(d) + n
        if not 0 <= i < len(self._sessions):
            raise CalendarError(f"{n} sessions after {d} is outside the calendar")
        return self._sessions[i]


def study_calendar() -> ClosureCorrectedCalendar:
    from tree_options.trex.clock import session_calendar

    return ClosureCorrectedCalendar(session_calendar(), STATIC_CALENDAR_MISSING_CLOSURES)


# ---- the walk ------------------------------------------------------------------


def monthly_signal_days(cal: Calendar, first: str, last: str) -> list[date]:
    """First NYSE session of each month within [first, last]."""
    return [
        s
        for s in cal.sessions()
        if first <= s.isoformat() <= last and is_first_session_of_month(s, cal)
    ]


def daily_signal_days(cal: Calendar, first: str, last: str) -> list[date]:
    return [s for s in cal.sessions() if first <= s.isoformat() <= last]


@dataclass
class CellData:
    trades: list[Trade] = field(default_factory=list)
    base: list[Trade] = field(default_factory=list)
    picks: dict[str, list[tuple[str, float]]] = field(default_factory=dict)
    sizes: list[int] = field(default_factory=list)


@dataclass
class WalkLog:
    """Why names left each cross-section (name-signal counts)."""

    excluded: dict[str, int] = field(default_factory=dict)
    hold_gap: dict[int, int] = field(default_factory=dict)
    short_days: dict[int, int] = field(default_factory=dict)  # < 30 names after the hold filter


def run_walk(
    panel: Mapping[str, Mapping[str, Mapping[str, Any]]],
    names: Sequence[str],
    cal: Calendar,
    days: Iterable[date],
    *,
    skip: int,
    holds: Sequence[int] = HOLDS,
    forms: Sequence[str] = FORMS,
    log: WalkLog | None = None,
) -> dict[CellKey, CellData]:
    """Every (form, hold, era) cell of one construction on one walk.

    Ranking is `signals.xsmom_rank` (complete [t-273, t] windows only, best
    first, ties in name order); the hold filter then drops any name missing
    a session in (t, t+hold]. Fewer than MIN_RANKED survivors: no signal."""
    cells: dict[CellKey, CellData] = {}
    sessions = cal.sessions()
    for t in days:
        ranked, excluded = signals.xsmom_rank(panel, t, cal, skip=skip, names=names)
        if log is not None:
            for why in excluded.values():
                log.excluded[why] = log.excluded.get(why, 0) + 1
        i = cal.ordinal(t)
        t_iso = t.isoformat()
        era = "holdout" if t_iso <= HOLDOUT_END else "full"
        for hold in holds:
            if len(ranked) < MIN_RANKED or i + hold >= len(sessions):
                continue
            window = [s.isoformat() for s in sessions[i + 1 : i + hold + 1]]
            rows: list[tuple[str, float]] = []
            for name, _score in ranked:
                bars = panel[name]
                if any(w not in bars for w in window):
                    if log is not None:
                        log.hold_gap[hold] = log.hold_gap.get(hold, 0) + 1
                    continue
                fwd = Decimal(str(bars[window[-1]]["close"])) / Decimal(str(bars[t_iso]["close"]))
                rows.append((name, float(fwd - 1)))
            if len(rows) < MIN_RANKED:
                if log is not None:
                    log.short_days[hold] = log.short_days.get(hold, 0) + 1
                continue
            base = statistics.fmean(r for _n, r in rows)
            k = len(rows) // 3
            for form in forms:
                pick = rows[:3] if form == "top3" else rows[:k]
                cell = cells.setdefault((form, hold, era), CellData())
                cell.base.append((t_iso, base))
                cell.trades.extend((t_iso, r) for _n, r in pick)
                cell.sizes.append(len(rows))
                if form == "top3":
                    cell.picks[t_iso] = list(pick)
    return cells


# ---- statistics ---------------------------------------------------------------


@dataclass(frozen=True)
class Stats:
    n: int
    days: int
    hit: float | None
    mean: float | None
    t: float | None


def daily_net(trades: Sequence[Trade]) -> list[tuple[str, float]]:
    """Per-signal-day mean of net returns, in first-seen day order."""
    by_day: dict[str, list[float]] = {}
    for d, r in trades:
        by_day.setdefault(d, []).append(r - RT)
    return [(d, statistics.mean(v)) for d, v in by_day.items()]


def stats_of(trades: Sequence[Trade]) -> Stats:
    """iter003 / xu_xsmom `stats_of`: net of RT, clustered by signal day,
    t over the day means with ddof=1."""
    daily = [v for _d, v in daily_net(trades)]
    if not daily:
        return Stats(0, 0, None, None, None)
    mean = statistics.fmean(daily)
    hit = sum(1 for _d, r in trades if r - RT > 0) / len(trades)
    t = float("nan")
    if len(daily) > 1:
        sd = statistics.stdev(daily)
        t = mean / (sd / math.sqrt(len(daily))) if sd else float("nan")
    return Stats(len(trades), len(daily), hit, mean, t)


def is_cand(s: Stats, b: Stats) -> bool:
    if s.mean is None or b.mean is None or s.t is None or math.isnan(s.t):
        return False
    return s.mean > 0 and s.t >= 2 and s.days >= 30 and s.mean - b.mean > 0


def row_fields(s: Stats, b: Stats) -> list[str]:
    """The legacy row's fields after the label: n, days, hit, mean, t, base,
    cond, cand (xu_xsmom's orig37 column is not part of the comparison)."""
    assert s.mean is not None and s.hit is not None and s.t is not None and b.mean is not None
    return [
        str(s.n),
        str(s.days),
        f"{s.hit:.1%}",
        f"{s.mean:+.3%}",
        f"{s.t:.2f}",
        f"base {b.mean:+.3%}",
        f"cond {s.mean - b.mean:+.3%}",
        "**CANDIDATE**" if is_cand(s, b) else "-",
    ]


def dsr(vals: Sequence[float], sr_trials: Sequence[float], n_eff: int) -> dict[str, float]:
    """iter005 `dsr_block` (Bailey and Lopez de Prado 2014) for one series.

    The only departure is the normal quantile: `statistics.NormalDist`
    (exact) where iter005 carried a 1e-6 rational approximation. Fewer than
    4 observations (the kurtosis estimator's floor) or fewer than 2 trials:
    not evaluable, every statistic NaN."""
    n = len(vals)
    if n < 4 or len(sr_trials) < 2 or statistics.stdev(vals) == 0:
        nan = float("nan")
        return {"n": float(n), "sr": nan, "skew": nan, "kurtosis": nan, "sr0": nan, "dsr": nan}
    m = statistics.fmean(vals)
    sd = statistics.stdev(vals)
    sr = m / sd
    g3 = (n / ((n - 1) * (n - 2))) * sum(((v - m) / sd) ** 3 for v in vals)
    g4 = (
        (n * (n + 1) / ((n - 1) * (n - 2) * (n - 3))) * sum(((v - m) / sd) ** 4 for v in vals)
        - 3 * (n - 1) ** 2 / ((n - 2) * (n - 3))
        + 3
    )
    nd = statistics.NormalDist()
    v_sr = statistics.variance(sr_trials)
    sr0 = math.sqrt(v_sr) * (
        (1 - EULER_GAMMA) * nd.inv_cdf(1 - 1 / n_eff)
        + EULER_GAMMA * nd.inv_cdf(1 - 1 / (n_eff * math.e))
    )
    denom = math.sqrt(max(1e-12, 1 - g3 * sr + (g4 - 1) / 4 * sr * sr))
    return {
        "n": float(n),
        "sr": sr,
        "skew": g3,
        "kurtosis": g4,
        "sr0": sr0,
        "dsr": nd.cdf((sr - sr0) * math.sqrt(n - 1) / denom),
    }


# ---- decision ------------------------------------------------------------------


def decide(conds: Mapping[CondKey, float | None]) -> tuple[bool, list[str]]:
    """The pre-registered DECISION RULE: the true 12-1 is a SUCCESSOR
    CANDIDATE only if, on BOTH universes, top-3 h20 cond is > 0 in BOTH eras
    AND >= the no-skip comparator's cond in the holdout era. A missing cell
    fails closed."""
    lines: list[str] = []
    ok_all = True
    for u in UNIVERSES:
        ho = conds.get(("12-1", u, "top3", 20, "holdout"))
        fu = conds.get(("12-1", u, "top3", 20, "full"))
        ns = conds.get(("no-skip", u, "top3", 20, "holdout"))
        if ho is None or fu is None or ns is None:
            lines.append(f"{u}: a decisive cell is empty -> FAIL (fails closed)")
            ok_all = False
            continue
        legs = [
            (f"12-1 top3 h20 holdout cond {ho:+.3%} > 0", ho > 0),
            (f"12-1 top3 h20 full cond {fu:+.3%} > 0", fu > 0),
            (f"12-1 holdout cond {ho:+.3%} >= no-skip holdout cond {ns:+.3%}", ho >= ns),
        ]
        ok = all(passed for _txt, passed in legs)
        ok_all = ok_all and ok
        detail = "; ".join(f"{txt} {'PASS' if passed else 'FAIL'}" for txt, passed in legs)
        lines.append(f"{u}: {detail} -> {'PASS' if ok else 'FAIL'}")
    return ok_all, lines


# ---- validation against the published legacy rows -----------------------------


def _cells_of(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def published_rows(md: Path, *, prefix: str) -> dict[str, list[str]]:
    """label -> [n, days, hit, mean, t, base, cond, cand] for every table row
    whose label starts with `prefix` (xu_xsmom's orig37 column dropped)."""
    out: dict[str, list[str]] = {}
    for line in md.read_text().splitlines():
        if not line.startswith(f"| {prefix}"):
            continue
        cells = _cells_of(line)
        out[cells[0]] = [*cells[1:8], cells[-1]]
    return out


def protocol_rows(md: Path) -> dict[str, tuple[str, str]]:
    """PROTOCOL-XSMOM.md: entry month -> (legs text, month P&L text)."""
    out: dict[str, tuple[str, str]] = {}
    for line in md.read_text().splitlines():
        cells = _cells_of(line) if line.startswith("| 20") else []
        if len(cells) == 4:
            out[cells[0]] = (cells[1], cells[2])
    return out


@dataclass
class RowCheck:
    source: str
    label: str
    published: list[str]
    ours: list[str] | None

    @property
    def exact(self) -> bool:
        return self.ours == self.published


def validate_daily(
    source: str, md: Path, panel: Panel, names: Sequence[str], cal: Calendar
) -> list[RowCheck]:
    first, last = panel_range(panel)
    cells = run_walk(panel, names, cal, daily_signal_days(cal, first, last), skip=0)
    checks = []
    for label, published in sorted(published_rows(md, prefix="252-skip21-").items()):
        spec, hold_txt, era = label.split()
        form = spec.rsplit("-", 1)[1]
        cell = cells.get((form, int(hold_txt[1:]), era))
        base_key = (form, int(hold_txt[1:]), era)
        ours = None
        if cell is not None and cell.trades:
            ours = row_fields(stats_of(cell.trades), stats_of(cells[base_key].base))
        checks.append(RowCheck(source, label, published, ours))
    return checks


@dataclass
class ProtocolCheck:
    months_published: int
    months_ours: int
    exact: int
    diffs: list[str]


def validate_protocol(md: Path, panel: Panel, names: Sequence[str], cal: Calendar) -> ProtocolCheck:
    first, last = panel_range(panel)
    cells = run_walk(
        panel,
        names,
        cal,
        monthly_signal_days(cal, first, last),
        skip=0,
        holds=(20,),
        forms=("top3",),
    )
    ours: dict[str, tuple[str, str]] = {}
    for era in ERAS:
        cell = cells.get(("top3", 20, era))
        for t_iso, legs in (cell.picks if cell else {}).items():
            pnl = [NOTIONAL * (r - RT) for _n, r in legs]
            ours[t_iso[:7]] = (
                ", ".join(f"{n}: {p:+.0f}" for (n, _r), p in zip(legs, pnl, strict=True)),
                f"{sum(pnl):+.0f}",
            )
    published = protocol_rows(md)
    diffs = [
        f"{m}: published {published.get(m)} | ours {ours.get(m)}"
        for m in sorted(set(published) | set(ours))
        if published.get(m) != ours.get(m)
    ]
    exact = sum(1 for m in published if published[m] == ours.get(m))
    return ProtocolCheck(len(published), len(ours), exact, diffs)


# ---- the study -------------------------------------------------------------------


@dataclass
class Universe:
    name: str
    panel: Panel
    names: list[str]
    sha256: str
    source: str


def run_study(universes: Sequence[Universe], cal: Calendar) -> dict[str, Any]:
    cells: dict[CondKey, CellData] = {}
    logs: dict[tuple[str, str], WalkLog] = {}
    for uni in universes:
        first, last = panel_range(uni.panel)
        days = monthly_signal_days(cal, first, last)
        for cons, skip in CONSTRUCTIONS:
            log = WalkLog()
            walk = run_walk(uni.panel, uni.names, cal, days, skip=skip, log=log)
            logs[(cons, uni.name)] = log
            for (form, hold, era), walked in walk.items():
                cells[(cons, uni.name, form, hold, era)] = walked
    rows: dict[CondKey, dict[str, Any]] = {}
    for cons, _skip in CONSTRUCTIONS:
        for u in (x.name for x in universes):
            for form in FORMS:
                for hold in HOLDS:
                    for era in ERAS:
                        key = (cons, u, form, hold, era)
                        found = cells.get(key)
                        if found is None or not found.trades:
                            rows[key] = {"empty": True}
                            continue
                        s, b = stats_of(found.trades), stats_of(found.base)
                        assert s.mean is not None and b.mean is not None
                        rows[key] = {
                            "empty": False,
                            "stats": asdict(s),
                            "base": asdict(b),
                            "cond": s.mean - b.mean,
                            "cand": is_cand(s, b),
                            "fields": row_fields(s, b),
                            "first": found.base[0][0],
                            "last": found.base[-1][0],
                            "size_min": min(found.sizes),
                            "size_median": statistics.median(found.sizes),
                        }
    conds: dict[CondKey, float | None] = {
        k: (None if v["empty"] else v["cond"]) for k, v in rows.items()
    }
    candidate, decision_lines = decide(conds)
    dsr_blocks: dict[str, dict[str, float]] = {}
    if candidate:
        trials = []
        for trial in cells.values():
            vals = [v for _d, v in daily_net(trial.trades)]
            if len(vals) > 1 and statistics.stdev(vals) > 0:
                trials.append(statistics.fmean(vals) / statistics.stdev(vals))
        for u in (x.name for x in universes):
            for era in ERAS:
                decisive = cells[("12-1", u, "top3", 20, era)]
                vals = [v for _d, v in daily_net(decisive.trades)]
                dsr_blocks[f"{u} 12-1 top3 h20 {era}"] = dsr(vals, trials, DSR_N)
    return {
        "cells": cells,
        "rows": rows,
        "logs": logs,
        "candidate": candidate,
        "decision": decision_lines,
        "dsr": dsr_blocks,
    }


# ---- report -----------------------------------------------------------------------


def _git_head() -> str:
    try:
        head = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()  # fmt: skip
        dirty = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "status", "--porcelain"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()  # fmt: skip
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return head + ("+dirty" if dirty else "")


def render_validation(checks: Sequence[RowCheck], proto: ProtocolCheck) -> list[str]:
    lines = [
        "| source | published row | published (n, days, hit, mean, t, base, cond, cand) "
        "| ours | match |",
        "|---|---|---|---|---|",
    ]
    for c in checks:
        pub = " / ".join(c.published)
        ours = "EMPTY" if c.ours is None else " / ".join(c.ours)
        lines.append(
            f"| {c.source} | {c.label} | {pub} | {'(same)' if c.exact else ours} "
            f"| {'EXACT' if c.exact else 'DIFF'} |"
        )
    lines += [
        "",
        f"PROTOCOL-XSMOM.md (monthly walk, top-3, h20, $2,500/leg, 5bp): "
        f"{proto.exact}/{proto.months_published} published months reproduced exactly "
        f"(legs in rank order, per-leg and month P&L); ours has {proto.months_ours} months.",
    ]
    if proto.diffs:
        lines += ["", "Differing months:", ""] + [f"- {d}" for d in proto.diffs]
    return lines


def _fmt_row(key: CondKey, row: Mapping[str, Any]) -> str:
    cons, u, form, hold, era = key
    head = f"| {cons} | {u} | {form} | h{hold} | {era} |"
    if row["empty"]:
        return head + " 0 | EMPTY | | | | | | |"
    return head + " " + " | ".join(row["fields"]) + " |"


def render_report(
    *,
    prereg_sha: str,
    universes: Sequence[Universe],
    cal: ClosureCorrectedCalendar,
    checks: Sequence[RowCheck],
    proto: ProtocolCheck,
    result: Mapping[str, Any],
    ran_at: str,
) -> str:
    rows = result["rows"]
    lines = [
        "# XSMOM-12-1 — the true 12-1 momentum vs the tested no-skip construction",
        "",
        f"Pre-registration: `XSMOM-12-1-PREREG.md`, sha256 `{prereg_sha}` (matches its",
        "`.sha256` sidecar, sealed 2026-09-23T14:51:29-06:00 before this run). Run",
        f"{ran_at} by `scripts/xsmom_12_1_study.py` at commit `{_git_head()}` (branch",
        "feat/desk-w1-side), one scored run. Every pre-registered cell is reported below;",
        "nothing was re-gridded.",
        "",
        "Inputs:",
        "",
    ]
    for uni in universes:
        first, last = panel_range(uni.panel)
        lines.append(
            f"- **{uni.name}**: `{uni.source}` sha256 `{uni.sha256}`, {len(uni.names)} names, "
            f"{first}..{last}."
        )
    lines += [
        f"- Calendar: the trex static NYSE calendar (`data/calendar/trex/`, "
        f"exchange-calendars 4.5.2) minus {', '.join(d.isoformat() for d in cal.removed)} "
        "(see the calendar correction).",
        "",
        "Construction: on the first NYSE session t of each month, rank by",
        "`close(t-21)/close(t-273)-1` (**12-1**) or `close(t)/close(t-273)-1` (**no-skip**,",
        "the tested construction behind every published XSMOM number), both through",
        "`tree_options.desk.signals.xsmom_rank` (skip=21 / skip=0). A name missing any",
        "session inside [t-273, t+hold] is excluded for that month. Conventions as",
        "XU-XSMOM.md / iter003: 5bp round trip, t clustered by signal day (ddof=1), >= 30",
        "names, base = hold-everything on the same walk, cond = mean - base, cand = mean > 0",
        "and t >= 2 and days >= 30 and cond > 0. Eras: holdout = signal <= 2024-09-03, full =",
        "post-holdout only.",
        "",
        "## Calendar correction (found in validation, before the scored run)",
        "",
        "The static calendar lists **2025-01-09** as a session: its pinned generator",
        "(exchange-calendars 4.5.2) predates the NYSE closure that day (national day of",
        "mourning for President Carter). Both panels lack 2025-01-09 for every name (99/99),",
        "and it is the ONLY disagreement between the calendar and any panel's rows. Taken",
        "literally, the missing-session rule would exclude every name from every month whose",
        "274-session window spans that day (about 2025-01 to 2026-02) and empty most of the",
        "post-holdout era. The study removes that one day from the calendar, so session",
        "offsets are true NYSE offsets. This is a data fact, fixed before any cell was",
        "scored; it does not depend on results. **The same defect sits in the trex and",
        "protocol calendars** (operator follow-up; the live desk's current windows start",
        "after 2025-01-09, so today's signals are unaffected).",
        "",
        "## Validation (the machinery, before trusting it)",
        "",
        "The no-skip construction on the same code path, on a DAILY walk (every session a",
        "signal day, as the legacy scripts walked), against the published `252-skip21` rows",
        "(which ARE the no-skip 273-session construction), on the panels as they stood at",
        f"publication (main panel cut at {PUBLICATION_CUT}; the XU panel ends there):",
        "",
    ]
    lines += render_validation(checks, proto)
    n_exact = sum(1 for c in checks if c.exact)
    lines += [
        "",
        f"Row checks: **{n_exact}/{len(checks)} EXACT** on every printed field.",
        "",
        "## Results: all 32 pre-registered cells",
        "",
        "| construction | universe | form | hold | era | n | days | hit | mean net | t | base "
        "| cond | cand |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for cons, _skip in CONSTRUCTIONS:
        for u in UNIVERSES:
            for form in FORMS:
                for hold in HOLDS:
                    for era in ERAS:
                        lines.append(
                            _fmt_row((cons, u, form, hold, era), rows[(cons, u, form, hold, era)])
                        )
    days_max = max((r["stats"]["days"] for r in rows.values() if not r["empty"]), default=0)
    lines += [
        "",
        f"Note on `cand`: a monthly walk yields at most {days_max} signal months per era, so",
        "the pre-registered `days >= 30` leg cannot pass and no cell carries the CANDIDATE",
        "mark. The decision rule below does not use `cand`. h60 holds overlap across",
        "consecutive monthly signals (about 3 months per hold), so h60 t-statistics are",
        "overstated; the decisive cells are h20.",
        "",
        "## Decision (the pre-registered rule)",
        "",
    ]
    lines += [f"- {ln}" for ln in result["decision"]]
    verdict = (
        "SUCCESSOR CANDIDATE: the true 12-1 is nominated as a successor rule, evaluated only "
        "after the sealed XSMOM-TOP3 rule's own 18/20-card review"
        if result["candidate"]
        else "TESTED, NOT PREFERRED: the true 12-1 is not a successor candidate; per the "
        "pre-registration, the question closes (a new idea is a new pre-registration)"
    )
    lines += ["", f"**VERDICT: {verdict}.**", "", f"## Deflated Sharpe (N = {DSR_N})", ""]
    if result["dsr"]:
        lines += [
            "iter005 `dsr_block` on each decisive series (per-signal-month net means), trial",
            f"variance across all {DSR_N} cells' Sharpe ratios:",
            "",
            "| series | months | SR/month | skew | kurtosis | SR0 | DSR |",
            "|---|---|---|---|---|---|---|",
        ]
        for label, blk in result["dsr"].items():
            lines.append(
                f"| {label} | {int(blk['n'])} | {blk['sr']:.4f} | {blk['skew']:.2f} "
                f"| {blk['kurtosis']:.2f} | {blk['sr0']:.4f} | {blk['dsr']:.1%} |"
            )
    else:
        lines.append(
            "Not computed: there is no successor candidate, and the pre-registration reports"
            " the deflated Sharpe only for one."
        )
    lines += ["", "## Data quality", ""]
    for (cons, u), log in sorted(result["logs"].items()):
        excl = ", ".join(f"{k} {v}" for k, v in sorted(log.excluded.items())) or "none"
        held = ", ".join(f"h{h} {v}" for h, v in sorted(log.hold_gap.items())) or "none"
        short = ", ".join(f"h{h} {v}" for h, v in sorted(log.short_days.items())) or "none"
        lines.append(
            f"- {u} {cons}: name-months excluded before ranking: {excl}; dropped by the hold "
            f"window: {held}; months with < 30 names after the hold filter: {short}."
        )
    lines += [
        "- Known data features (unchanged from XU-XSMOM.md): META's vendor hole",
        "  2022-01-28..2022-06-09 excludes META from windows that span it; BK's panel ends",
        "  2026-05-20 and WBA's 2025-08-27 (delisted; kept); T's 2022-04-11 spin cliff is",
        "  unadjusted in the XU panel and sits inside T's ranking window through 2023-05.",
        "",
        "## Monthly top-3 picks (h20 walk)",
        "",
        "| signal | orig-36 12-1 | orig-36 no-skip | XU-62 12-1 | XU-62 no-skip |",
        "|---|---|---|---|---|",
    ]
    picks: dict[str, dict[tuple[str, str], str]] = {}
    for (cons, u, form, hold, _era), cell in result["cells"].items():
        if form != "top3" or hold != 20:
            continue
        for t_iso, legs in cell.picks.items():
            picks.setdefault(t_iso, {})[(u, cons)] = " ".join(n for n, _r in legs)
    for t_iso in sorted(picks):
        p = picks[t_iso]
        cols = [p.get((u, c), "-") for u in UNIVERSES for c in ("12-1", "no-skip")]
        lines.append(f"| {t_iso} | " + " | ".join(cols) + " |")
    return "\n".join(lines) + "\n"


def _to_json(
    result: Mapping[str, Any], checks: Sequence[RowCheck], proto: ProtocolCheck
) -> dict[str, Any]:
    return {
        "rows": {"|".join(map(str, k)): v for k, v in result["rows"].items()},
        "candidate": result["candidate"],
        "decision": result["decision"],
        "dsr": result["dsr"],
        "validation": {
            "rows": [asdict(c) | {"exact": c.exact} for c in checks],
            "protocol": asdict(proto),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--paper-trades", type=Path, default=DEFAULT_DIR)
    parser.add_argument("--out", type=Path, help=f"report path (default <paper-trades>/{REPORT})")
    parser.add_argument("--json", type=Path, help="optional machine-readable results")
    parser.add_argument(
        "--validate-only", action="store_true", help="run the validation only; write nothing"
    )
    args = parser.parse_args(argv)
    pt: Path = args.paper_trades
    try:
        prereg_sha = verify_prereg(pt / PREREG)
    except (PreregMismatch, OSError) as exc:
        print(f"PREREG REFUSED: {exc}", file=sys.stderr)
        return 2
    print(f"prereg sha256 {prereg_sha} verified")

    main_panel, main_sha = read_panel(pt / MAIN_PANEL, pt / MAIN_LOCK)
    xu_panel, xu_sha = read_panel(pt / XU_PANEL)
    orig_names = sorted(n for n in main_panel if n != "SPY")
    xu_names = sorted(xu_panel)
    if len(orig_names) != 36 or len(xu_names) != 62:
        print(f"UNIVERSE MISMATCH: {len(orig_names)}/36, {len(xu_names)}/62", file=sys.stderr)
        return 3
    cal = study_calendar()

    main_cut = cut_panel(main_panel, PUBLICATION_CUT)
    xu_cut = cut_panel(xu_panel, PUBLICATION_CUT)
    checks = validate_daily("XSMOM-LONGLEG", pt / "XSMOM-LONGLEG.md", main_cut, orig_names, cal)
    checks += validate_daily("XU-XSMOM", pt / "XU-XSMOM.md", xu_cut, xu_names, cal)
    proto = validate_protocol(pt / "PROTOCOL-XSMOM.md", main_cut, orig_names, cal)
    for c in checks:
        print(
            f"validate {c.source} {c.label}: {'EXACT' if c.exact else 'DIFF'}"
            + ("" if c.exact else f"\n  published {c.published}\n  ours      {c.ours}")
        )
    print(
        f"validate PROTOCOL-XSMOM: {proto.exact}/{proto.months_published} months exact, "
        f"ours {proto.months_ours}"
    )
    for d in proto.diffs:
        print(f"  {d}")
    if args.validate_only:
        return 0

    universes = [
        Universe("orig-36", main_panel, orig_names, main_sha, MAIN_PANEL),
        Universe("XU-62", xu_panel, xu_names, xu_sha, XU_PANEL),
    ]
    result = run_study(universes, cal)
    ran_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    report = render_report(
        prereg_sha=prereg_sha,
        universes=universes,
        cal=cal,
        checks=checks,
        proto=proto,
        result=result,
        ran_at=ran_at,
    )
    out = args.out or pt / REPORT
    out.write_text(report)
    print(report)
    print(f"wrote {out}")
    if args.json is not None:
        args.json.write_text(json.dumps(_to_json(result, checks, proto), indent=1, sort_keys=True))
        print(f"wrote {args.json}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
