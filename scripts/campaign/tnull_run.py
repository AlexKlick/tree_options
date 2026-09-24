#!/usr/bin/env python3
"""campaign-2026-09 T-NULL runner (scope ``c09-tnull``, menu order 0).

The deterministic null calibration that precedes every family: three
hash-random entry streams on the equity CARD LANE (wave-0 T-NULL idiom,
``scripts/run_lane2_wave.py`` / ``docs/theory/wave0-registration.json``
slots null-s1..s3), executed exactly as the sealed menu entry
``slots[0]`` of ``docs/theory/campaign-2026-09-registration.json``
specifies. The menu is the registration (registrar-authored, sealed by
its sha256 sidecar); this runner only Binds to it: ids, score seeds,
params_keys, run indices, pinned entry rule, sub-eras, and acceptance
criteria are read from the menu and any drift refuses before anything
runs (the wave-0 ``verify_config_against_ledger`` discipline).

Pinned entry rule (menu ``entry_rule_pinned``), per seed:
(a) XSMOM-shaped stream -- 3 names per first-of-month session drawn by
    sha256(score_seed, name, session) over the 36 tradables (panel minus
    SPY): the top 3 hash scores, ties (impossible in practice) in name
    order;
(b) event-shaped stream -- 1 name per first-post-report session
    (earnings-calendar.json, 26 reporters) among the 35 chain-universe
    names (panel minus TQQQ/SQQQ): the reporter with the top hash score
    on that session.
Hold 20 sessions close-to-close (Decimal closes, house hold filter: the
name must carry every session in (t, t+20]; beyond-panel holds are
dropped and counted), 5bp RT primary (15bp robustness), day-clustered t
over per-entry-day means (iter003/xu_xsmom ``stats_of`` convention),
2025-01-09 excluded as a non-session (2026-09-23 ledger ruling; the
sealed calendar file stays byte-identical).

Evaluation span: ONE pass per seed over the union 2024-10-01..2026-09-23,
reported per sub-era exactly as the menu's ``fold_mapping.sealed``
names them (card-era / vrp-cond / pead-deep-2 / jepa-outer). The null is
fit-free (INV-07 vacuous); consuming the families' sealed windows here
is the menu's own declaration (calibration ONLY -- the band is read on
the same sessions); nothing is tuned, adopted, or promoted.

INV-13 (registration precedes outcome): ``--register`` writes the three
trial rows (REGISTERED, no outcome) to the slot registry sqlite BEFORE
any outcome exists; ``--execute`` is one-shot per trial (REGISTERED ->
RUNNING -> COMPLETED with the artifact as metrics_uri); ``--calibrate``
reads ONLY the executed artifacts (stamp-bound to their trial_id) and
stamps the calibration block, or -- on any DEFECT-FLAGGED seed -- writes
the defect flag file and stamps DEFECT-FLAGGED so family scoring stays
frozen pending an operator ruling. Every phase refuses on any mismatch
with the sealed menu (hashes, params, floors, bands).
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
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence

for _name in (
    "OPENBLAS_NUM_THREADS",
    "OMP_NUM_THREADS",
    "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
):
    os.environ.setdefault(_name, "1")

REPO_ROOT = Path(__file__).resolve().parents[2]  # the EXECUTION worktree
if str(REPO_ROOT / "src") not in sys.path:  # pragma: no cover - import plumbing
    sys.path.insert(0, str(REPO_ROOT / "src"))

from tree_options.registry.scope import TrialScope  # noqa: E402
from tree_options.registry.sqlite import TrialRegistry  # noqa: E402
from tree_options.schemas.trial import TrialRecord  # noqa: E402
from tree_options.trials.null_score import null_score  # noqa: E402

# Data lives in the MAIN checkout; the runner + registration live in the
# execution worktree. Both are pinned by sha256 against the menu below.
MAIN_ROOT = Path("/home/alexk/documents/tree_options")
REGISTRATION_PATH = REPO_ROOT / "docs" / "theory" / "campaign-2026-09-registration.json"
REGISTRATION_SIDECAR = Path(str(REGISTRATION_PATH) + ".sha256")
PROTOCOL_PATH = REPO_ROOT / "research_protocol.yaml"
PANEL_PATH = MAIN_ROOT / "artifacts" / "paper-trades" / "ohlc-panel.json"
EARNINGS_PATH = MAIN_ROOT / "artifacts" / "paper-trades" / "earnings-calendar.json"
CALENDAR_PATH = MAIN_ROOT / "data" / "calendar" / "nyse_sessions_2018_01_02_2026_12_31.json"

CAMPAIGN_DIR = MAIN_ROOT / "artifacts" / "campaign-2026-09"
REGISTRY_PATH = CAMPAIGN_DIR / "tnull.db"
TNULL_DIR = CAMPAIGN_DIR / "tnull"
TRIALS_DIR = TNULL_DIR / "trials"
CALIBRATION_PATH = TNULL_DIR / "calibration.json"
DEFECT_FLAG_PATH = TNULL_DIR / "DEFECT-FLAG.txt"
LOCK_PATH = TNULL_DIR / "execute.lock"

SCOPE_ID = "c09-tnull"
SLOT_ID = "tnull"
MODEL_FAMILY = "null-sha256/1"

# Execution generation. g1 (trial ids without a -gN suffix, 2026-09-23) was
# VOIDED before any calibration was stamped: this runner's previous_session
# used bisect_right instead of bisect_left, so is_first_session_of_month was
# never true and the XSMOM-shaped stream executed EMPTY (0/0 entries) while
# the event-shaped stream ran correctly -- a machinery defect caught by the
# menu's entry floor, not an outcome. The g1 rows stay COMPLETED in the
# registry with their artifacts (the audit trail of the defect); g2 re-runs
# the SAME pre-registered configs under fresh trial ids (INV-13: register
# before outcome; one VALID scored run per cell; scope load 6/32 of the
# 32-cap; no parameter changed anywhere). --calibrate binds to g2 only.
TRIAL_GENERATION = 2

# The pinned geometry (menu fold_mapping + entry_rule_pinned).
PHANTOM_ISO = "2025-01-09"  # ledger ruling 2026-09-23: not a session
UNION_START = "2024-10-01"
UNION_END = "2026-09-23"
HOLD_SESSIONS = 20
RT_PRIMARY = 0.0005  # 5bp round trip, primary
RT_ROBUST = 0.0015  # 15bp round trip, robustness disclosure
NET_BAND = (-0.0015, 0.0005)  # [-15bp, +5bp] on the net per-trade mean
T_BAND = 2.0  # day-clustered |t| < 2 on the gross per-trade mean
ENTRY_FLOORS = {"xsmom": 60, "event": 100}
# Fixed sub-era starts/ends from the menu's fold_mapping.sealed; the
# jepa-outer END is computed at run time as cutoff - 21 sessions (the
# widest declared jepa horizon; disclosed intersection of its h in
# {5, 21} origin spans), cutoff = earliest last session among the 35.
JEPA_H = 21
SUB_ERAS: tuple[tuple[str, str, str | None], ...] = (
    ("card-era", "2024-10-01", "2026-08-28"),  # term-gate / exit-grid-2
    ("vrp-cond", "2026-06-02", "2026-08-31"),
    ("pead-deep-2", "2026-06-25", "2026-09-23"),
    ("jepa-outer", "2026-01-02", None),
)
STREAM_KEYS = ("xsmom", "event")


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _git_head(repo: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(f"REFUSED: git rev-parse HEAD failed: {result.stderr.strip()[:120]}")
    return result.stdout.strip()


# ---- sealed inputs -------------------------------------------------------------------


class Refused(RuntimeError):
    """A binding refusal: the sealed menu and the execution disagree."""


class SealedCalendar:
    """The sealed NYSE calendar minus the 2025-01-09 phantom session.

    (ClosureCorrectedCalendar idiom from scripts/xsmom_12_1_study.py: the
    sealed calendar FILE stays byte-identical; the walker removes the
    closure itself.)"""

    def __init__(self, sessions_iso: Sequence[str]) -> None:
        kept = [d for d in sessions_iso if d != PHANTOM_ISO]
        self.removed = tuple(sorted(set(sessions_iso) & {PHANTOM_ISO}))
        self._sessions = tuple(date.fromisoformat(d) for d in kept)
        self._ordinals = {s: i for i, s in enumerate(self._sessions)}
        if len(self._ordinals) != len(self._sessions):
            raise Refused("sealed calendar carries duplicate sessions")

    def sessions(self) -> tuple[date, ...]:
        return self._sessions

    def is_session(self, d: date) -> bool:
        return d in self._ordinals

    def ordinal(self, d: date) -> int:
        try:
            return self._ordinals[d]
        except KeyError:
            raise Refused(f"{d.isoformat()} is not a session") from None

    def nth_after(self, d: date, n: int) -> date:
        i = self.ordinal(d) + n
        if not 0 <= i < len(self._sessions):
            raise Refused(f"{n} sessions after {d.isoformat()} is outside the calendar")
        return self._sessions[i]

    # the desk.session helpers used below (bisect over sessions()); NOTE
    # previous_session uses bisect_LEFT (the session strictly before d,
    # never d itself -- the g1 defect)
    def previous_session(self, d: date) -> date | None:
        sessions = self._sessions
        i = _bisect_left(sessions, d) - 1
        return sessions[i] if i >= 0 else None

    def first_session_after(self, d: date) -> date | None:
        sessions = self._sessions
        i = _bisect_right(sessions, d)
        return sessions[i] if i < len(sessions) else None

    def is_first_session_of_month(self, d: date) -> bool:
        if not self.is_session(d):
            return False
        prev = self.previous_session(d)
        return prev is None or (prev.year, prev.month) != (d.year, d.month)


def _bisect_right(sessions: tuple[date, ...], d: date) -> int:
    import bisect

    return bisect.bisect_right(sessions, d)


def _bisect_left(sessions: tuple[date, ...], d: date) -> int:
    import bisect

    return bisect.bisect_left(sessions, d)


@dataclass(frozen=True)
class Inputs:
    menu: Mapping[str, Any]
    menu_sha256: str
    protocol_raw_sha256: str
    protocol_canonical_sha256: str
    panel: Mapping[str, Mapping[str, Mapping[str, Any]]]
    panel_sha256: str
    earnings: Mapping[str, Sequence[str]]
    earnings_sha256: str
    calendar: SealedCalendar
    calendar_sha256: str
    tradables: tuple[str, ...]  # 36: panel minus SPY
    chain35: tuple[str, ...]  # 35: panel minus TQQQ/SQQQ
    reporters: tuple[str, ...]  # the calendar's 26 names (all in chain35)
    sub_eras: dict[str, tuple[str, str]]  # name -> (start_iso, end_iso)
    cutoff_iso: str
    jepa_end_h5_iso: str
    dataset_manifest_hash: str


def load_and_bind() -> Inputs:
    """Read every sealed input and BIND it to the menu; any mismatch refuses."""

    menu_sha256 = _sha256_file(REGISTRATION_PATH)
    sidecar = REGISTRATION_SIDECAR.read_text().split()
    if not sidecar or sidecar[0] != menu_sha256:
        raise Refused(
            f"{REGISTRATION_PATH.name}: sha256 {menu_sha256} does not match its sidecar"
            " -- the registration is not sealed"
        )
    menu = json.loads(REGISTRATION_PATH.read_text(encoding="utf-8"))
    slot = next((s for s in menu["slots"] if s.get("slot_id") == SLOT_ID), None)
    if slot is None or slot.get("order") != 0 or slot.get("family") != "T-NULL":
        raise Refused("the menu's order-0 T-NULL slot is missing or malformed")
    scopes = {sc["scope_id"]: sc for sc in slot["scope_ids"]}
    if scopes.get(SCOPE_ID, {}).get("config_count") != 3:
        raise Refused(f"menu scope {SCOPE_ID} does not carry exactly 3 configs")
    configs = slot["configs"]
    if [c["slot_id"] for c in configs] != ["tnull-s1", "tnull-s2", "tnull-s3"]:
        raise Refused("menu tnull config ids are not tnull-s1..s3 in order")
    for cfg in configs:
        if cfg["model_family"] != MODEL_FAMILY:
            raise Refused(f"{cfg['slot_id']}: model_family is not {MODEL_FAMILY}")
        expected_key = [
            "card-lane",
            MODEL_FAMILY,
            cfg["score_seed"],
            "hold20",
            "5bp",
        ]
        if list(cfg["params_key"]) != expected_key:
            raise Refused(f"{cfg['slot_id']}: params_key {cfg['params_key']} is not the pinned key")

    # protocol: raw bytes must equal the menu pin; canonical hash re-stamped (INV-14)
    from tree_options.protocol.loader import default_protocol, protocol_hash

    protocol_raw_sha256 = _sha256_file(PROTOCOL_PATH)
    if menu["protocol_hash"] != protocol_raw_sha256:
        raise Refused(
            "research_protocol.yaml raw sha256 does not match the menu's protocol_hash"
            " -- a protocol change requires a NEW registration"
        )
    protocol_canonical_sha256 = protocol_hash(default_protocol())

    # data: the three pinned inputs
    pinning = menu["dataset_pinning"]
    panel_sha256 = _sha256_file(PANEL_PATH)
    earnings_sha256 = _sha256_file(EARNINGS_PATH)
    calendar_sha256 = _sha256_file(CALENDAR_PATH)
    for label, got in (
        ("artifacts/paper-trades/ohlc-panel.json", panel_sha256),
        ("artifacts/paper-trades/earnings-calendar.json", earnings_sha256),
        ("data/calendar/nyse_sessions_2018_01_02_2026_12_31.json", calendar_sha256),
    ):
        want = pinning.get(label)
        if want is None or want != got:
            raise Refused(f"{label}: sha256 {got} != the menu's pinned {want} -- swapped inputs refuse")

    panel = json.loads(PANEL_PATH.read_text(encoding="utf-8"))
    earnings = json.loads(EARNINGS_PATH.read_text(encoding="utf-8"))
    calendar_json = json.loads(CALENDAR_PATH.read_text(encoding="utf-8"))
    calendar = SealedCalendar(calendar_json["sessions"])
    if calendar.removed != (PHANTOM_ISO,):
        raise Refused(f"the phantom session was not removed: {calendar.removed}")

    # universes, pinned to the panel itself and cross-checked against the
    # desk config (menu calendar_and_universe note)
    from tree_options.desk.universe import CHAIN_UNIVERSE, XSMOM_TRADABLES

    panel_names = sorted(panel)
    if len(panel_names) != 37:
        raise Refused(f"panel carries {len(panel_names)} names, expected the sealed 37")
    tradables = tuple(n for n in panel_names if n != "SPY")
    chain35 = tuple(n for n in panel_names if n not in ("TQQQ", "SQQQ"))
    if len(tradables) != 36 or len(chain35) != 35:
        raise Refused("universe derivation is not 36 tradables / 35 chain names")
    if set(tradables) != set(XSMOM_TRADABLES):
        raise Refused("panel-minus-SPY does not equal the desk's sealed xsmom.tradables")
    if not set(chain35) <= set(CHAIN_UNIVERSE):
        raise Refused("panel-minus-TQQQ/SQQQ is not inside the desk's chain universe")
    reporters = tuple(sorted(n for n, reps in earnings.items() if reps))
    if len(reporters) != 26 or not set(reporters) <= set(chain35):
        raise Refused(
            f"earnings-calendar reporters are not the 26 chain-universe names: {len(reporters)}"
        )

    # sub-eras: fixed bounds, jepa-outer end computed from the panel
    last_by_name = {n: max(panel[n]) for n in chain35}
    cutoff_iso = min(last_by_name.values())
    cutoff = date.fromisoformat(cutoff_iso)
    sessions = calendar.sessions()

    def minus_h(iso_h: int) -> str:
        return sessions[calendar.ordinal(cutoff) - iso_h].isoformat()

    sub_eras: dict[str, tuple[str, str]] = {}
    for name, start, end in SUB_ERAS:
        sub_eras[name] = (start, end if end is not None else minus_h(JEPA_H))
    if not UNION_START <= sub_eras["jepa-outer"][0] < sub_eras["jepa-outer"][1] <= UNION_END:
        raise Refused(f"computed jepa-outer window is not inside the union span: {sub_eras['jepa-outer']}")

    manifest_body = "".join(
        f"{label}\0{pinning[label]}\n"
        for label in (
            "artifacts/paper-trades/ohlc-panel.json",
            "artifacts/paper-trades/earnings-calendar.json",
            "data/calendar/nyse_sessions_2018_01_02_2026_12_31.json",
        )
    )
    dataset_manifest_hash = hashlib.sha256(manifest_body.encode("utf-8")).hexdigest()
    return Inputs(
        menu=menu,
        menu_sha256=menu_sha256,
        protocol_raw_sha256=protocol_raw_sha256,
        protocol_canonical_sha256=protocol_canonical_sha256,
        panel=panel,
        panel_sha256=panel_sha256,
        earnings=earnings,
        earnings_sha256=earnings_sha256,
        calendar=calendar,
        calendar_sha256=calendar_sha256,
        tradables=tradables,
        chain35=chain35,
        reporters=reporters,
        sub_eras=sub_eras,
        cutoff_iso=cutoff_iso,
        jepa_end_h5_iso=minus_h(5),
        dataset_manifest_hash=dataset_manifest_hash,
    )


# ---- the two null streams --------------------------------------------------------------


@dataclass(frozen=True)
class Trade:
    entry: str
    exit_session: str | None  # None: beyond-panel hold (dropped and counted)
    name: str
    gross: float | None  # None iff exit_session is None / hold window incomplete
    sub_era: str
    detail: str  # provenance (report date or "first-of-month hash top3")


def _complete_trade(
    inputs: Inputs, name: str, entry: date, sub_era: str, detail: str
) -> Trade:
    cal, panel = inputs.calendar, inputs.panel
    bars = panel[name]
    entry_iso = entry.isoformat()
    if entry_iso not in bars:
        return Trade(entry_iso, None, name, None, sub_era, detail + ";no-entry-bar")
    sessions = cal.sessions()
    i = cal.ordinal(entry)
    window = [s.isoformat() for s in sessions[i + 1 : i + HOLD_SESSIONS + 1]]
    if len(window) != HOLD_SESSIONS or any(w not in bars for w in window):
        return Trade(entry_iso, None, name, None, sub_era, detail + ";hold-incomplete")
    exit_iso = window[-1]
    gross = float(
        Decimal(str(bars[exit_iso]["close"])) / Decimal(str(bars[entry_iso]["close"])) - 1
    )
    return Trade(entry_iso, exit_iso, name, gross, sub_era, detail)


def xsmom_stream(inputs: Inputs, seed: str) -> list[Trade]:
    """(a) 3 names per first-of-month session, top sha256 scores over the 36."""
    cal = inputs.calendar
    trades: list[Trade] = []
    union_lo = date.fromisoformat(UNION_START)
    union_hi = date.fromisoformat(UNION_END)
    for s in cal.sessions():
        if not (union_lo <= s <= union_hi) or not cal.is_first_session_of_month(s):
            continue
        sub_era = _sub_era_of(inputs, s)
        ranked = sorted(
            inputs.tradables,
            key=lambda n: (-null_score(seed=seed, session=s, security_id=n), n),
        )
        for name in ranked[:3]:
            trades.append(_complete_trade(inputs, name, s, sub_era, "fom-hash-top3"))
    return trades


def event_stream(inputs: Inputs, seed: str) -> list[Trade]:
    """(b) 1 name per first-post-report session, top sha256 score that day."""
    cal = inputs.calendar
    union_lo = date.fromisoformat(UNION_START)
    union_hi = date.fromisoformat(UNION_END)
    by_entry: dict[date, list[tuple[str, str]]] = {}
    for name in inputs.reporters:
        for report_iso in inputs.earnings[name]:
            entry = cal.first_session_after(date.fromisoformat(report_iso))
            if entry is None or not (union_lo <= entry <= union_hi):
                continue
            by_entry.setdefault(entry, []).append((name, report_iso))
    trades: list[Trade] = []
    for entry in sorted(by_entry):
        candidates = by_entry[entry]
        pick = max(candidates, key=lambda nr: (null_score(seed=seed, session=entry, security_id=nr[0]), nr[0]))
        name, report_iso = pick
        sub_era = _sub_era_of(inputs, entry)
        trades.append(
            _complete_trade(inputs, name, entry, sub_era, f"post-report:{report_iso}")
        )
    return trades


def _sub_era_of(inputs: Inputs, entry: date) -> str:
    """FIRST matching sub-era label (diagnostic tag only -- the sub-eras
    OVERLAP by design; window membership is computed by entry-date
    containment in ``_in_window``, never from this tag)."""
    iso = entry.isoformat()
    for name, (start, end) in inputs.sub_eras.items():
        if start <= iso <= end:
            return name
    return "union-only"


def _in_window(entry_iso: str, label: str, start: str, end: str) -> bool:
    """A trade belongs to EVERY window that contains its entry session
    (the menu's sub-eras are overlapping reporting cuts, not partitions)."""
    return True if label == "union" else start <= entry_iso <= end


def _windows_of(trades: Sequence[Mapping[str, Any]], inputs: Inputs) -> dict[str, Any]:
    """Per-window cells for one stream's trade rows (entry/exit/name/gross
    dicts -- the executed artifact's ground truth). Used by BOTH execute
    (to stamp the artifact) and calibrate (to re-derive the cells from the
    sealed trade rows, so the aggregation logic lives in exactly one
    place)."""
    return {
        label: _cell_stats(
            [t for t in trades if _in_window(t["entry"], label, start, end)],
            RT_PRIMARY,
        )
        for label, start, end in _windows(inputs)
    }


# ---- statistics (house conventions) ------------------------------------------------------


def _cell_stats(trades: Sequence[Mapping[str, Any]], rt: float) -> dict[str, Any]:
    """Per-trade stats for one (seed x stream x window) cell, over trade
    ROWS ({entry, exit, name, gross, ...} dicts -- the artifact shape).

    Day-clustered t over per-entry-day means of GROSS returns
    (iter003/xu_xsmom ``stats_of`` convention, on gross instead of net:
    the menu bands the gross t and the net mean separately). The
    clustered SE of the day-mean mean is the realized sd of the per-trade
    mean the menu records as the tripwire prior."""
    complete = [t for t in trades if t["gross"] is not None]
    out: dict[str, Any] = {
        "n_entries": len(trades),
        "n_complete": len(complete),
        "n_dropped_hold": len(trades) - len(complete),
    }
    if not complete:
        out.update(
            {
                "days": 0,
                "gross_per_trade_mean": None,
                "gross_day_mean": None,
                "gross_day_sd": None,
                "gross_clustered_t": None,
                "per_trade_mean_sd_day_clustered": None,
                "per_trade_mean_sd_naive": None,
                "net_per_trade_mean": None,
                "net15_per_trade_mean": None,
            }
        )
        return out
    gross = [t["gross"] for t in complete]
    by_day: dict[str, list[float]] = {}
    for t in complete:
        by_day.setdefault(t["entry"], []).append(t["gross"])
    day_means = [statistics.fmean(v) for _d, v in sorted(by_day.items())]
    m = statistics.fmean(day_means)
    sd = statistics.stdev(day_means) if len(day_means) > 1 else None
    if sd is not None and sd > 0:
        t_stat = m / (sd / math.sqrt(len(day_means)))
        cse = sd / math.sqrt(len(day_means))
    elif sd == 0:
        t_stat = math.inf if m else math.nan  # sd==0 with drift: unbounded t
        cse = 0.0
    else:
        t_stat = None
        cse = None
    out.update(
        {
            "days": len(day_means),
            "gross_per_trade_mean": statistics.fmean(gross),
            "gross_day_mean": m,
            "gross_day_sd": sd,
            "gross_clustered_t": t_stat,
            "per_trade_mean_sd_day_clustered": cse,
            "per_trade_mean_sd_naive": (
                statistics.stdev(gross) / math.sqrt(len(gross)) if len(gross) > 1 else None
            ),
            "net_per_trade_mean": statistics.fmean(g - rt for g in gross),
            "net15_per_trade_mean": statistics.fmean(g - RT_ROBUST for g in gross),
        }
    )
    return out


def _windows(inputs: Inputs) -> list[tuple[str, str, str]]:
    """(label, start, end): the four sub-eras plus the union span (report-only)."""
    rows = [(name, s, e) for name, (s, e) in sorted(inputs.sub_eras.items())]
    rows.insert(0, ("union", UNION_START, UNION_END))
    return rows


def _trial_artifact_path(trial_id: str) -> Path:
    return TRIALS_DIR / f"{trial_id}.json"


def _hyperparameters(inputs: Inputs, config: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "scope_id": SCOPE_ID,
        "slot_id": config["slot_id"],
        "model_family": config["model_family"],
        "score_seed": config["score_seed"],
        "params_key": list(config["params_key"]),
        "run_index": config["run_index"],
        "lane": "card-lane (equity close-to-close)",
        "hold_sessions": HOLD_SESSIONS,
        "rt_primary_bp": 5,
        "rt_robust_bp": 15,
        "union_span": [UNION_START, UNION_END],
        "sub_eras": {name: list(bounds) for name, bounds in sorted(inputs.sub_eras.items())},
        "jepa_outer_end_note": f"cutoff({inputs.cutoff_iso}) minus {JEPA_H} sessions; h=5 end {inputs.jepa_end_h5_iso} disclosed",
        "streams": {
            "xsmom": "3 names per first-of-month session, top null_score over the 36 tradables",
            "event": "1 name per first-post-report session, top null_score among that session's reporters (chain-35)",
        },
        "calendar_exclusions": [PHANTOM_ISO],
        "return_convention": "Decimal close(t+20)/close(t)-1; hold filter requires every session in (t, t+20]",
        "inputs_sha256": {
            "ohlc-panel.json": inputs.panel_sha256,
            "earnings-calendar.json": inputs.earnings_sha256,
            "nyse_sessions json": inputs.calendar_sha256,
        },
        "registration_menu_sha256": inputs.menu_sha256,
        "protocol_raw_sha256": inputs.protocol_raw_sha256,
        "protocol_canonical_sha256": inputs.protocol_canonical_sha256,
        "dataset_manifest_hash": inputs.dataset_manifest_hash,
    }


def _config_hash(hyperparameters: Mapping[str, Any]) -> str:
    body = json.dumps(hyperparameters, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _scope(inputs: Inputs) -> TrialScope:
    return TrialScope(
        protocol_id="tree_options",
        protocol_hash=inputs.protocol_canonical_sha256,
        outer_fold_id=f"campaign-2026-09/{SCOPE_ID}/union-{UNION_START}_{UNION_END}",
        target_horizon="hold20",
        feature_set_id="ohlc-panel|card-lane|v1",
        model_family=MODEL_FAMILY,
    )


def _slot_configs(inputs: Inputs) -> tuple[Mapping[str, Any], ...]:
    slot = _menu_slot(inputs)
    return tuple(slot["configs"])


def _menu_slot(inputs: Inputs) -> Mapping[str, Any]:
    """The menu's tnull slot, found by id (never by array position)."""
    slot = next(s for s in inputs.menu["slots"] if s.get("slot_id") == SLOT_ID)
    if slot.get("order") != 0:
        raise Refused("the tnull slot is not the menu's order-0 slot")
    return slot


def _open_registry() -> TrialRegistry:
    CAMPAIGN_DIR.mkdir(parents=True, exist_ok=True)
    return TrialRegistry(REGISTRY_PATH)


def _trial_id(config: Mapping[str, Any]) -> str:
    return f"{SCOPE_ID}-{config['slot_id']}-g{TRIAL_GENERATION}"


def _stamp(inputs: Inputs, config: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "program": "campaign-2026-09",
        "trial_id": _trial_id(config),
        "trial_generation": TRIAL_GENERATION,
        "slot_id": config["slot_id"],
        "scope_id": SCOPE_ID,
        "registration_menu_sha256": inputs.menu_sha256,
        "protocol_raw_sha256": inputs.protocol_raw_sha256,
        "protocol_canonical_sha256": inputs.protocol_canonical_sha256,
        "dataset_manifest_hash": inputs.dataset_manifest_hash,
        "inputs_sha256": {
            "ohlc-panel.json": inputs.panel_sha256,
            "earnings-calendar.json": inputs.earnings_sha256,
            "nyse_sessions json": inputs.calendar_sha256,
        },
        "universe": {
            "panel_names": 37,
            "tradables": len(inputs.tradables),
            "chain35": len(inputs.chain35),
            "reporters": len(inputs.reporters),
        },
        "cutoff_earliest_last_session_chain35": inputs.cutoff_iso,
        "git_sha": _git_head(REPO_ROOT),
        "runner_sha256": _sha256_file(Path(__file__).resolve()),
        "generated_at": _utcnow().isoformat(),
    }


# -- phases ---------------------------------------------------------------------------


def phase_register() -> int:
    inputs = load_and_bind()
    configs = _slot_configs(inputs)
    scope = _scope(inputs)
    registry = _open_registry()
    try:
        existing = [c for c in configs if registry.is_registered(_trial_id(c))]
        if existing:
            raise Refused(
                f"registration is one-shot: {[ _trial_id(c) for c in existing ]} already registered"
            )
        for config in configs:
            hyper = _hyperparameters(inputs, config)
            record = TrialRecord(
                trial_id=_trial_id(config),
                created_at=_utcnow(),
                hypothesis=_menu_slot(inputs)["hypothesis"],
                git_sha=_git_head(REPO_ROOT),
                config_hash=_config_hash(hyper),
                dataset_manifest_hash=inputs.dataset_manifest_hash,
                train_window=None,
                validation_window=None,
                test_window=(date.fromisoformat(UNION_START), date.fromisoformat(UNION_END)),
                hyperparameters=hyper,
                scope_key=scope.scope_key(),
            )
            registry.register(record, scope)
            print(
                f"registered {_trial_id(config)} scope={SCOPE_ID}"
                f" run_index={config['run_index']} seed={config['score_seed']}"
            )
    finally:
        registry.close()
    print(
        f"registry: {REGISTRY_PATH} (scope trials={3}, cap=32);"
        " NO outcome has been computed or viewed"
    )
    return 0


def phase_execute() -> int:
    inputs = load_and_bind()
    configs = _slot_configs(inputs)
    TNULL_DIR.mkdir(parents=True, exist_ok=True)
    TRIALS_DIR.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise Refused("another tnull execution holds the lock -- one run at a time") from None
        registry = _open_registry()
        try:
            for config in configs:
                trial_id = _trial_id(config)
                artifact = _trial_artifact_path(trial_id)
                if artifact.exists():
                    raise Refused(f"{artifact} already exists -- executions are one-shot per trial")
                status = registry.status(trial_id)
                if status != "REGISTERED":
                    raise Refused(f"{trial_id} is {status}, not REGISTERED -- refusing to re-run")
                hyper = _hyperparameters(inputs, config)
                config_hash = _config_hash(hyper)
                git_sha = _git_head(REPO_ROOT)
                registry.mark_running(
                    trial_id,
                    git_sha=git_sha,
                    config_hash=config_hash,
                    dataset_manifest_hash=inputs.dataset_manifest_hash,
                    at=_utcnow(),
                )
                seed = config["score_seed"]
                payload: dict[str, Any] = {"score_seed": seed, "streams": {}}
                for stream_key, builder in (("xsmom", xsmom_stream), ("event", event_stream)):
                    trades = builder(inputs, seed)
                    rows = [
                        {
                            "entry": t.entry,
                            "exit": t.exit_session,
                            "name": t.name,
                            "gross": t.gross,
                            "sub_era": t.sub_era,
                            "detail": t.detail,
                        }
                        for t in trades
                    ]
                    payload["streams"][stream_key] = {
                        "trades": rows,
                        "windows": _windows_of(rows, inputs),
                    }
                # machinery self-check (the g1 lesson): a stream that
                # executed EMPTY is a runner defect, never an outcome --
                # the trial FAILS instead of completing, so no calibration
                # can ever read it. Genuine shortfalls above zero flow to
                # the menu's NOT_EVALUABLE floor verdict instead.
                for stream_key in STREAM_KEYS:
                    if payload["streams"][stream_key]["windows"]["union"]["n_entries"] == 0:
                        registry.fail(
                            trial_id,
                            f"stream {stream_key} executed with 0 entries -- runner machinery defect",
                            at=_utcnow(),
                        )
                        raise Refused(
                            f"{trial_id}: stream {stream_key} executed with 0 entries"
                            " (machinery defect; trial FAILED, no artifact written)"
                        )
                body = {"stamp": _stamp(inputs, config), "payload": payload}
                artifact.write_text(
                    json.dumps(body, indent=2, sort_keys=True) + "\n", encoding="utf-8"
                )
                registry.complete(trial_id, metrics_uri=str(artifact), outcome_at=_utcnow())
                sx = payload["streams"]["xsmom"]["windows"]["union"]
                se = payload["streams"]["event"]["windows"]["union"]
                print(
                    f"{trial_id}: COMPLETED artifact={artifact}"
                    f" xsmom n={sx['n_complete']}/{sx['n_entries']}"
                    f" event n={se['n_complete']}/{se['n_entries']}"
                )
        finally:
            registry.close()
    finally:
        os.close(lock_fd)
    return 0


def _lattice(verdicts: Any) -> str:
    """Worst-first verdict lattice: a NOT_EVALUABLE stream can never ride
    inside a CALIBRATED stamp (the calibration gates every family run
    after it, so it must carry both streams)."""
    vs = list(verdicts)
    if any(v == "DEFECT-FLAGGED" for v in vs):
        return "DEFECT-FLAGGED"
    if any(v == "NOT_EVALUABLE" for v in vs):
        return "NOT_EVALUABLE"
    return "CALIBRATED"


def _read_artifact(inputs: Inputs, config: Mapping[str, Any]) -> Mapping[str, Any]:
    trial_id = _trial_id(config)
    artifact = _trial_artifact_path(trial_id)
    body = json.loads(artifact.read_text(encoding="utf-8"))
    stamp = body.get("stamp", {})
    if stamp.get("trial_id") != trial_id:
        raise Refused(
            f"{artifact} carries trial_id {stamp.get('trial_id')!r}, expected {trial_id!r}"
            " -- only the EXECUTED artifact is calibration evidence"
        )
    if stamp.get("registration_menu_sha256") != inputs.menu_sha256:
        raise Refused(f"{artifact} was executed against a different menu hash")
    if stamp.get("dataset_manifest_hash") != inputs.dataset_manifest_hash:
        raise Refused(f"{artifact} was executed against different inputs")
    return body


def phase_calibrate() -> int:
    inputs = load_and_bind()
    configs = _slot_configs(inputs)
    if CALIBRATION_PATH.exists():
        raise Refused(f"{CALIBRATION_PATH} already exists -- the calibration stamp is one-shot")
    seed_blocks: dict[str, Any] = {}
    defect_reasons: list[str] = []
    for config in configs:
        body = _read_artifact(inputs, config)
        slot_id = config["slot_id"]
        streams_verdict: dict[str, str] = {}
        stream_cells: dict[str, Any] = {}
        for stream_key in STREAM_KEYS:
            stream = body["payload"]["streams"][stream_key]
            # cells are RE-DERIVED from the sealed trade rows by entry-date
            # containment (the g2 artifacts' own per-window block was
            # stamped with the first-match-tag bug for the overlapping
            # sub-eras; the trade rows are the ground truth and identical)
            windows = _windows_of(stream["trades"], inputs)
            union = windows["union"]
            reasons: list[str] = []
            if union["n_entries"] < ENTRY_FLOORS[stream_key]:
                streams_verdict[stream_key] = "NOT_EVALUABLE"
                reasons.append(
                    f"entry floor missed: {union['n_entries']} < {ENTRY_FLOORS[stream_key]}"
                )
            else:
                for label, start, end in _windows(inputs):
                    if label == "union":
                        continue  # floors bind on the union; bands on the sub-eras
                    cell = windows[label]
                    t_stat = cell["gross_clustered_t"]
                    if t_stat is None or math.isnan(t_stat):
                        reasons.append(
                            f"{label}: clustered t undefined (days={cell['days']})"
                            " -- NOT_EVALUABLE cell, disclosed"
                        )
                        continue
                    if not abs(t_stat) < T_BAND:
                        reasons.append(
                            f"{label}: |t|={t_stat:.3f} >= {T_BAND} on gross per-trade mean"
                        )
                    net = cell["net_per_trade_mean"]
                    if net is None or not (NET_BAND[0] <= net <= NET_BAND[1]):
                        reasons.append(
                            f"{label}: net per-trade mean {net} outside {list(NET_BAND)}"
                        )
                streams_verdict[stream_key] = "DEFECT-FLAGGED" if reasons else "CALIBRATED"
            stream_cells[stream_key] = {
                "windows": windows,
                "floor_entries_union": union["n_entries"],
                "reasons": reasons,
            }
        seed_verdict = _lattice(streams_verdict.values())
        defect_reasons.extend(
            f"{slot_id}/{stream}: {r}"
            for stream, verdict in streams_verdict.items()
            if verdict == "DEFECT-FLAGGED"
            for r in stream_cells[stream]["reasons"]
        )
        seed_blocks[slot_id] = {
            "trial_id": _trial_id(config),
            "verdict": seed_verdict,
            "streams": streams_verdict,
            "cells": stream_cells,
        }
    overall = _lattice(b["verdict"] for b in seed_blocks.values())
    not_evaluable_reasons = [
        f"{c['slot_id']}/{stream}: {r}"
        for c in configs
        for stream, block in seed_blocks[c["slot_id"]]["cells"].items()
        if seed_blocks[c["slot_id"]]["streams"][stream] == "NOT_EVALUABLE"
        for r in block["reasons"]
    ]

    # the tripwire prior: mean over the 3 seeds of each cell's realized sd
    # of the per-trade mean (day-clustered) -- priors come ONLY from
    # executed null artifacts (wave-0 rule; synthetic priors FORBIDDEN)
    priors: dict[str, Any] = {}
    if overall == "CALIBRATED":
        for stream_key in STREAM_KEYS:
            priors[stream_key] = {}
            for label, _s, _e in _windows(inputs):
                vals = [
                    seed_blocks[c["slot_id"]]["cells"][stream_key]["windows"][label][
                        "per_trade_mean_sd_day_clustered"
                    ]
                    for c in configs
                ]
                usable = [v for v in vals if v is not None]
                priors[stream_key][label] = {
                    "seeds": vals,
                    "mean": statistics.fmean(usable) if usable else None,
                    "convention": "sd of the per-trade mean, day-clustered"
                    " (sd of entry-day means / sqrt(days)); the standing"
                    " D8-analog tripwire prior for family runs after this",
                }

    calibration = {
        "stamp": {
            **_stamp(inputs, configs[0]),
            "trial_id": None,
            "artifact_trial_ids": [_trial_id(c) for c in configs],
            "menu_hypothesis": _menu_slot(inputs)["hypothesis"],
            "acceptance_criteria": _menu_slot(inputs)["acceptance_criteria"],
            "verdict_vocabulary": _menu_slot(inputs)["verdict_vocabulary"],
            "bands": {
                "gross_clustered_t_abs_lt": T_BAND,
                "net_per_trade_mean_band": list(NET_BAND),
                "rt_primary": RT_PRIMARY,
                "rt_robust": RT_ROBUST,
                "entry_floors": dict(ENTRY_FLOORS),
            },
        },
        "verdict": {"slot": overall, "seeds": seed_blocks},
        "tripwire_priors": priors,
        "defect_reasons": defect_reasons,
        "not_evaluable_reasons": not_evaluable_reasons,
        "notes": [
            "CALIBRATION ONLY (menu declared_use): no direction, no card, no promotion path.",
            "Sub-era windows are entry-date reporting cuts on one pass over the union span;",
            "they overlap by construction (the menu's fold_mapping.sealed); cells are",
            "re-derived from the executed trade rows by entry-date containment.",
            f"jepa-outer end = earliest last session among the 35 ({inputs.cutoff_iso}) minus",
            f" {JEPA_H} sessions; the h=5 end ({inputs.jepa_end_h5_iso}) is disclosed, not scored.",
            f"jepa-outer end = earliest last session among the 35 ({inputs.cutoff_iso}) minus"
            f" {JEPA_H} sessions; the h=5 end ({inputs.jepa_end_h5_iso}) is disclosed, not scored.",
            "Cells with an undefined clustered t (<2 entry days) are disclosed NOT_EVALUABLE,",
            "never defects; a zero-variance day-mean sd with drift stamps t=inf and defects.",
        ],
    }
    TNULL_DIR.mkdir(parents=True, exist_ok=True)
    CALIBRATION_PATH.write_text(
        json.dumps(calibration, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    if overall == "DEFECT-FLAGGED":
        flag = "\n".join(
            [
                "campaign-2026-09 T-NULL DEFECT FLAG",
                f"written: {_utcnow().isoformat()}",
                f"calibration artifact: {CALIBRATION_PATH}",
                "",
                "A seed violated the menu's acceptance bands; family sealed-window",
                "scoring is FROZEN pending an operator ruling (menu rules.sequencing).",
                "",
                "Defect reasons:",
                *(f"- {r}" for r in defect_reasons),
                "",
            ]
        )
        DEFECT_FLAG_PATH.write_text(flag, encoding="utf-8")
        print(f"DEFECT-FLAGGED -- flag file {DEFECT_FLAG_PATH}; family scoring stays frozen")
        for r in defect_reasons:
            print(f"  - {r}")
    elif overall == "NOT_EVALUABLE":
        print(f"NOT_EVALUABLE -- recorded at {CALIBRATION_PATH}; no tripwire prior stamped")
        for r in not_evaluable_reasons:
            print(f"  - {r}")
    else:
        print(f"CALIBRATED -- calibration stamped at {CALIBRATION_PATH}")
        for stream_key in STREAM_KEYS:
            row = priors[stream_key]
            print(
                f"  prior[{stream_key}] union sd(mean)={row['union']['mean']:.6g}"
                f" card-era={row['card-era']['mean']:.6g}"
            )
    return 0


def phase_plan() -> int:
    inputs = load_and_bind()
    print(f"menu sha256 {inputs.menu_sha256} (sidecar-verified)")
    print(f"protocol raw {inputs.protocol_raw_sha256[:16]}... canonical {inputs.protocol_canonical_sha256[:16]}...")
    print(f"cutoff (earliest last session, chain35): {inputs.cutoff_iso}")
    for label, start, end in _windows(inputs):
        print(f"window {label}: {start}..{end}")
    print(f"universe: tradables={len(inputs.tradables)} chain35={len(inputs.chain35)} reporters={len(inputs.reporters)}")
    print(f"registry db: {REGISTRY_PATH}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--register",
        action="store_true",
        help="INV-13: write the 3 trial rows (REGISTERED, no outcome) to the slot registry",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="run the 3 seeds one-shot (REGISTERED -> RUNNING -> COMPLETED + artifact)",
    )
    parser.add_argument(
        "--calibrate",
        action="store_true",
        help="evaluate the menu's acceptance criteria on the EXECUTED artifacts and"
        " stamp the calibration (or the DEFECT flag)",
    )
    parser.add_argument("--plan", action="store_true", help="read-only: print the bound geometry")
    args = parser.parse_args(argv)
    try:
        if args.register:
            return phase_register()
        if args.execute:
            return phase_execute()
        if args.calibrate:
            return phase_calibrate()
        if args.plan:
            return phase_plan()
    except Refused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
