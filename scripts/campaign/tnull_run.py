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

v2 RE-STAMP (menu v2, operator ruling 2026-09-23 ~23:45 MDT — drift-
relative null; ``REGISTRATION-NOTES.md`` section 7): ``--baseline``
computes B(W, shape) per menu ``rules.null_baseline`` — the
unconditional drift baseline: the day-clustered NET (5bp RT) per-trade
mean of the ALL-NAMES stream over each declared evaluation window, built
by the SAME code path, holds, Decimal closes, and exclusions as the null
streams (hold-20 close-to-close, house hold filter, 2025-01-09
excluded). Shapes: xsmom-shaped = every first-of-month NYSE session in
the window x ALL sealed-36 tradables (the desk config's
``XSMOM_TRADABLES``, never a panel derivation); event-shaped = every
chain-35 reporter reporting at a first-post-report session, the
reporting name entered. The v2 criteria are then re-evaluated against
the EXISTING v1 seed streams read from ``calibration.json`` (never
re-fetched, re-randomized, or re-run — the streams and their arithmetic
are verified data): per seed x window, criterion 1 unchanged
(day-clustered |t| < 2 on gross) and criterion 2 drift-relative (seed
net per-trade mean within 2 x its day-clustered se of B(W, shape)),
plus the unchanged union entry floors; the tripwire prior block a
CALIBRATED null stamps is carried under the same v1 convention.
``--baseline`` refuses unless the v1 calibration binds this menu's
``supersedes`` sha, the same pinned input hashes, the same trial ids,
the same cutoff/windows, and registry rows that are COMPLETED with the
same sub_eras. It stamps ``calibration-v2.json`` citing the menu v2 sha
(menu ``rules.sequencing`` AMENDMENT v2: family scoring unfreezes only
after this re-stamp passes) and, on any seed still failing, writes the
v2 defect flag so family scoring stays frozen pending another operator
ruling.

v3 RE-STAMP (menu v3, operator ruling 2, 2026-09-23 ~23:55 MDT — the
NOT_EVALUABLE floor for sub-evaluable null cells;
``REGISTRATION-NOTES.md`` section 8): ``--v3-floor`` reads the EXISTING
``calibration-v2.json`` per-cell stats — never re-fetching,
re-randomizing, or re-running the seeds, and never re-deriving B — and
classifies every seed x window x shape cell: a cell with < 5 entry-days
or < 20 complete trades is NOT_EVALUABLE (its values are still reported,
for transparency — that is the point of the floor — but it carries no
flag authority and feeds no prior). The null is CALIBRATED iff every
EVALUABLE cell passes BOTH v2 criteria (their pass arithmetic — t,
delta-vs-B, band — is carried from the v2 stamp unchanged) on every
seed, with the unchanged union entry floors. B(W, shape) and the
all-names shape blocks are carried forward verbatim from the v2 stamp;
the tripwire prior block is stamped only by a CALIBRATED null and is
built from EVALUABLE cells only, so windows with no evaluable cell
carry no prior — families in those windows are gated by B(W, shape)
alone. ``--v3-floor`` refuses unless the v2 artifact binds this menu's
``supersedes`` sha, the same pinned input hashes, the same trial ids,
and the same cutoff, and stamps ``calibration-v3.json`` citing the menu
v3 sha (menu ``rules.sequencing`` AMENDMENT v3: calibration-v3.json
citing the v3 menu sha unfreezes family scoring); on any EVALUABLE cell
failing it writes the v3 defect flag so family scoring stays frozen
pending yet another operator ruling.
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
CALIBRATION_V2_PATH = TNULL_DIR / "calibration-v2.json"
DEFECT_FLAG_V2_PATH = TNULL_DIR / "DEFECT-FLAG-V2.txt"
CALIBRATION_V3_PATH = TNULL_DIR / "calibration-v3.json"
DEFECT_FLAG_V3_PATH = TNULL_DIR / "DEFECT-FLAG-V3.txt"
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
NET_BAND = (-0.0015, 0.0005)  # [-15bp, +5bp] on the net per-trade mean (v1; superseded by menu v2)
T_BAND = 2.0  # day-clustered |t| < 2 on the gross per-trade mean
V2_K = 2.0  # menu v2 criterion 2: |seed net mean - B(W, shape)| <= K x seed day-clustered se
V3_MIN_ENTRY_DAYS = 5  # menu v3 floor: below this the cell is NOT_EVALUABLE
V3_MIN_COMPLETE_TRADES = 20  # menu v3 floor: below this the cell is NOT_EVALUABLE
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


def _baseline_names_xsmom(inputs: Inputs) -> tuple[str, ...]:
    """The all-names xsmom shape trades the desk CONFIG's sealed-36
    tradables (menu v2 rules.null_baseline: ``XSMOM_TRADABLES from the
    config, never a panel derivation``); load_and_bind has already
    refused unless panel-minus-SPY equals that set, so this is a
    cross-check, not a derivation."""
    from tree_options.desk.universe import XSMOM_TRADABLES

    names = tuple(sorted(XSMOM_TRADABLES))
    if len(names) != 36 or set(names) != set(inputs.tradables):
        raise Refused("the desk config's xsmom.tradables are not the sealed 36")
    return names


def baseline_xsmom_stream(inputs: Inputs) -> list[Trade]:
    """rules.null_baseline, xsmom shape: EVERY sealed-36 tradable entered
    at EVERY first-of-month NYSE session in the union span (windows are
    entry-date containment cuts of the same pass, exactly like the null
    streams -- same code path, holds, Decimal closes, exclusions)."""
    cal = inputs.calendar
    union_lo = date.fromisoformat(UNION_START)
    union_hi = date.fromisoformat(UNION_END)
    names = _baseline_names_xsmom(inputs)
    trades: list[Trade] = []
    for s in cal.sessions():
        if not (union_lo <= s <= union_hi) or not cal.is_first_session_of_month(s):
            continue
        sub_era = _sub_era_of(inputs, s)
        for name in names:
            trades.append(_complete_trade(inputs, name, s, sub_era, "fom-all36"))
    return trades


def baseline_event_stream(inputs: Inputs) -> tuple[list[Trade], int]:
    """rules.null_baseline, event shape: EVERY chain-35 reporter reporting
    at a first-post-report session is entered that session (the reporting
    name entered; no hash selection). Same by_entry construction as the
    seeded event stream. A name reporting twice into the same entry
    session is entered once (deterministic earliest-report tie-break);
    the dedupe count is returned for disclosure."""
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
    deduped = 0
    for entry in sorted(by_entry):
        seen: set[str] = set()
        for name, report_iso in sorted(by_entry[entry], key=lambda nr: (nr[1], nr[0])):
            if name in seen:
                deduped += 1
                continue
            seen.add(name)
            trades.append(
                _complete_trade(
                    inputs, name, entry, _sub_era_of(inputs, entry), f"post-report-all:{report_iso}"
                )
            )
    return trades, deduped


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


# -- v2 re-stamp (menu v2, drift-relative null) ----------------------------------------


def _read_v1_calibration(inputs: Inputs) -> Mapping[str, Any]:
    """The v1 calibration artifact is the SEALED seed evidence for v2: its
    verdict cells are read, never re-derived. Refuse unless it binds this
    menu's ``supersedes`` pin, the same pinned inputs, the same trial ids,
    and the same panel cutoff (which fixes the jepa-outer window)."""
    if not CALIBRATION_PATH.exists():
        raise Refused(f"{CALIBRATION_PATH} is missing -- the v1 calibration is the sealed seed evidence")
    body = json.loads(CALIBRATION_PATH.read_text(encoding="utf-8"))
    stamp = body.get("stamp", {})
    supersedes = inputs.menu.get("supersedes")
    if not supersedes:
        raise Refused("the menu carries no `supersedes` pin -- not a v2 amendment")
    if stamp.get("registration_menu_sha256") != supersedes:
        raise Refused(
            "the v1 calibration's menu sha is not this menu's supersedes pin"
            f" ({stamp.get('registration_menu_sha256')} != {supersedes})"
        )
    if stamp.get("dataset_manifest_hash") != inputs.dataset_manifest_hash:
        raise Refused("the v1 calibration was stamped on a different dataset manifest")
    if stamp.get("inputs_sha256") != {
        "ohlc-panel.json": inputs.panel_sha256,
        "earnings-calendar.json": inputs.earnings_sha256,
        "nyse_sessions json": inputs.calendar_sha256,
    }:
        raise Refused("the v1 calibration's per-input sha256 do not match the pinned inputs")
    if stamp.get("artifact_trial_ids") != [_trial_id(c) for c in _slot_configs(inputs)]:
        raise Refused("the v1 calibration binds different artifact trial ids")
    if stamp.get("cutoff_earliest_last_session_chain35") != inputs.cutoff_iso:
        raise Refused("the panel cutoff moved since the v1 stamp -- the windows differ")
    seeds = body.get("verdict", {}).get("seeds", {})
    if sorted(seeds) != ["tnull-s1", "tnull-s2", "tnull-s3"]:
        raise Refused("the v1 calibration does not carry exactly the three seed blocks")
    for slot_id, blk in seeds.items():
        for stream_key in STREAM_KEYS:
            cells = blk.get("cells", {}).get(stream_key, {}).get("windows", {})
            if sorted(cells) != sorted(label for label, _s, _e in _windows(inputs)):
                raise Refused(
                    f"v1 seed {slot_id}/{stream_key} window labels {sorted(cells)}"
                    " do not match the re-derived windows"
                )
    return body


def _registry_g2_check(inputs: Inputs) -> None:
    """Read-only registry bind: the three g2 rows are COMPLETED and their
    registered hyperparameters carry the SAME sub-era windows the current
    pinned inputs re-derive (the executed truth behind calibration.json)."""
    import sqlite3

    conn = sqlite3.connect(f"file:{REGISTRY_PATH}?mode=ro", uri=True)
    try:
        rows = dict(
            conn.execute(
                "SELECT trial_id, status FROM trials"
            ).fetchall()
        )
        hypers = dict(
            conn.execute(
                "SELECT trial_id, hyperparameters_json FROM trials"
            ).fetchall()
        )
    finally:
        conn.close()
    for config in _slot_configs(inputs):
        trial_id = _trial_id(config)
        if rows.get(trial_id) != "COMPLETED":
            raise Refused(f"registry row {trial_id} is {rows.get(trial_id)!r}, not COMPLETED")
        hyper = json.loads(hypers[trial_id])
        stamped = {k: tuple(v) for k, v in hyper.get("sub_eras", {}).items()}
        if stamped != inputs.sub_eras:
            raise Refused(
                f"{trial_id} registered sub-eras {stamped} != the re-derived {inputs.sub_eras}"
            )


def _check_v1_window_alignment(inputs: Inputs, v1: Mapping[str, Any]) -> None:
    """Calendar-arithmetic proof that the v1 cells' windows are the windows
    the current pinned inputs re-derive (no stream re-execution): the
    seeded xsmom stream enters EXACTLY 3 names per first-of-month session
    and the seeded event stream EXACTLY 1 name per first-post-report
    session, so each v1 cell's n_entries must equal 3 x fom-count
    (xsmom) / entry-session-count (event) under the re-derived bounds."""
    cal = inputs.calendar
    union_lo = date.fromisoformat(UNION_START)
    union_hi = date.fromisoformat(UNION_END)
    fom = [
        s
        for s in cal.sessions()
        if union_lo <= s <= union_hi and cal.is_first_session_of_month(s)
    ]
    entry_sessions = set()
    for name in inputs.reporters:
        for report_iso in inputs.earnings[name]:
            entry = cal.first_session_after(date.fromisoformat(report_iso))
            if entry is not None and union_lo <= entry <= union_hi:
                entry_sessions.add(entry)
    for label, start, end in _windows(inputs):
        fom_n = sum(1 for s in fom if date.fromisoformat(start) <= s <= date.fromisoformat(end))
        ev_n = sum(1 for s in entry_sessions if date.fromisoformat(start) <= s <= date.fromisoformat(end))
        for slot_id, blk in v1["verdict"]["seeds"].items():
            cells = blk["cells"]
            got_x = cells["xsmom"]["windows"][label]["n_entries"]
            got_e = cells["event"]["windows"][label]["n_entries"]
            if got_x != 3 * fom_n or got_e != ev_n:
                raise Refused(
                    f"v1 {slot_id} window {label}: n_entries ({got_x} xsmom / {got_e} event)"
                    f" != re-derived calendar arithmetic ({3 * fom_n} / {ev_n})"
                    " -- the windows are not aligned; refusing"
                )


def _baseline_blocks(inputs: Inputs) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build both all-names baseline streams, stamp their per-window cells
    (SAME ``_windows_of`` code path as the null streams), and extract
    B(W, shape) + day-clustered se per window."""
    rows_x = baseline_xsmom_stream(inputs)
    rows_e, deduped_e = baseline_event_stream(inputs)
    blocks: dict[str, Any] = {}
    b_table: dict[str, Any] = {}
    for stream_key, rows in (("xsmom", rows_x), ("event", rows_e)):
        windows = _windows_of(
            [
                {
                    "entry": t.entry,
                    "exit": t.exit_session,
                    "name": t.name,
                    "gross": t.gross,
                    "sub_era": t.sub_era,
                    "detail": t.detail,
                }
                for t in rows
            ],
            inputs,
        )
        b_table[stream_key] = {}
        for label, _s, _e in _windows(inputs):
            cell = windows[label]
            b_table[stream_key][label] = {
                "B_net_per_trade_mean": cell["net_per_trade_mean"],
                "per_trade_mean_sd_day_clustered": cell["per_trade_mean_sd_day_clustered"],
                "B_net_day_clustered_mean": (
                    cell["gross_day_mean"] - RT_PRIMARY
                    if cell["gross_day_mean"] is not None
                    else None
                ),
                "net15_per_trade_mean": cell["net15_per_trade_mean"],
            }
        blocks[stream_key] = {
            "construction": (
                "every first-of-month NYSE session in the union span x ALL sealed-36"
                " tradables (desk-config XSMOM_TRADABLES; cross-checked == panel-minus-SPY)"
                if stream_key == "xsmom"
                else "every chain-35 reporter reporting at a first-post-report session,"
                " the reporting name entered (deduped per name per session,"
                f" earliest-report tie-break; deduped={deduped_e})"
            ),
            "n_trades_union": len(rows),
            "windows": windows,
        }
    return blocks, b_table


def _v2_stream_checks(
    inputs: Inputs,
    seed_cells: Mapping[str, Any],
    stream_key: str,
    b_row: Mapping[str, Any],
) -> tuple[str, list[str], list[str], dict[str, Any]]:
    """Menu v2 criteria for one (seed x stream): floors bind on the union;
    criterion 1 (unchanged) |t| < 2 on gross and criterion 2
    (drift-relative) |net - B(W, shape)| <= 2 x the seed's day-clustered
    se bind per sub-era window. Returns (verdict, defect_reasons,
    disclosures, per-window check arithmetic)."""
    reasons: list[str] = []
    disclosures: list[str] = []
    checks: dict[str, Any] = {}
    if seed_cells["floor_entries_union"] < ENTRY_FLOORS[stream_key]:
        return (
            "NOT_EVALUABLE",
            [],
            [f"entry floor missed: {seed_cells['floor_entries_union']} < {ENTRY_FLOORS[stream_key]}"],
            checks,
        )
    for label, _s, _e in _windows(inputs):
        if label == "union":
            continue  # floors bind on the union; bands on the sub-eras (v1 convention)
        cell = seed_cells["windows"][label]
        base = b_row[label]
        t_stat = cell["gross_clustered_t"]
        net = cell["net_per_trade_mean"]
        cse = cell["per_trade_mean_sd_day_clustered"]
        b_val = base["B_net_per_trade_mean"]
        row: dict[str, Any] = {
            "gross_clustered_t": t_stat,
            "net_per_trade_mean": net,
            "seed_cse_day_clustered": cse,
            "B": b_val,
        }
        if t_stat is None or math.isnan(t_stat):
            disclosures.append(
                f"{label}: clustered t undefined (days={cell['days']}) -- NOT_EVALUABLE cell, disclosed"
            )
            row["t_pass"] = None
        else:
            row["t_pass"] = abs(t_stat) < T_BAND
            if not row["t_pass"]:
                reasons.append(
                    f"{label}: |t|={t_stat:.3f} >= {T_BAND} on gross per-trade mean (criterion 1, unchanged)"
                )
        if b_val is None or cse is None or net is None:
            disclosures.append(
                f"{label}: drift-relative check not evaluable (B={b_val}, seed cse={cse}) -- disclosed"
            )
            row["baseline_pass"] = None
            row["delta_net_minus_B"] = None
            row["band_halfwidth"] = None
        else:
            delta = net - b_val
            band = V2_K * cse
            row["delta_net_minus_B"] = delta
            row["band_halfwidth"] = band
            row["baseline_pass"] = abs(delta) <= band
            if not row["baseline_pass"]:
                reasons.append(
                    f"{label}: net per-trade mean {net:.6f} outside"
                    f" B +/- {V2_K:g}x day-clustered se (B={b_val:.6f},"
                    f" delta={delta:.6f}, band={band:.6f})"
                )
        checks[label] = row
    return ("DEFECT-FLAGGED" if reasons else "CALIBRATED", reasons, disclosures, checks)


def phase_baseline() -> int:
    """The v2 re-stamp: B(W, shape) from the pinned inputs + the v2
    criteria re-evaluated on the v1 calibration's seed cells."""
    inputs = load_and_bind()
    if inputs.menu.get("version") != 2 or "null_baseline" not in inputs.menu.get("rules", {}):
        raise Refused("the sealed menu is not the v2 drift-relative amendment")
    if CALIBRATION_V2_PATH.exists():
        raise Refused(f"{CALIBRATION_V2_PATH} already exists -- the v2 re-stamp is one-shot")
    v1 = _read_v1_calibration(inputs)
    _registry_g2_check(inputs)
    _check_v1_window_alignment(inputs, v1)
    baseline_blocks, b_table = _baseline_blocks(inputs)

    seed_blocks: dict[str, Any] = {}
    defect_reasons: list[str] = []
    not_evaluable_reasons: list[str] = []
    for config in _slot_configs(inputs):
        slot_id = config["slot_id"]
        v1_seed = v1["verdict"]["seeds"][slot_id]
        streams_verdict: dict[str, str] = {}
        stream_cells: dict[str, Any] = {}
        for stream_key in STREAM_KEYS:
            verdict, reasons, disclosures, checks = _v2_stream_checks(
                inputs, v1_seed["cells"][stream_key], stream_key, b_table[stream_key]
            )
            streams_verdict[stream_key] = verdict
            stream_cells[stream_key] = {
                "v1_windows": v1_seed["cells"][stream_key]["windows"],
                "floor_entries_union": v1_seed["cells"][stream_key]["floor_entries_union"],
                "v2_checks": checks,
                "reasons": reasons,
                "disclosures": disclosures,
            }
            if verdict == "DEFECT-FLAGGED":
                defect_reasons.extend(f"{slot_id}/{stream_key}: {r}" for r in reasons)
            elif verdict == "NOT_EVALUABLE":
                not_evaluable_reasons.extend(f"{slot_id}/{stream_key}: {r}" for r in reasons)
        seed_blocks[slot_id] = {
            "trial_id": _trial_id(config),
            "v1_verdict": v1_seed["verdict"],
            "verdict": _lattice(streams_verdict.values()),
            "streams": streams_verdict,
            "cells": stream_cells,
        }
    overall = _lattice(b["verdict"] for b in seed_blocks.values())

    # tripwire prior block: the v1 convention -- stamped ONLY by a
    # CALIBRATED null (wave-0 rule: priors come only from executed null
    # artifacts); on any defect the block stays empty and family scoring
    # stays frozen pending an operator ruling.
    priors: dict[str, Any] = {}
    if overall == "CALIBRATED":
        for stream_key in STREAM_KEYS:
            priors[stream_key] = {}
            for label, _s, _e in _windows(inputs):
                vals = [
                    seed_blocks[c["slot_id"]]["cells"][stream_key]["v1_windows"][label][
                        "per_trade_mean_sd_day_clustered"
                    ]
                    for c in _slot_configs(inputs)
                ]
                usable = [v for v in vals if v is not None]
                priors[stream_key][label] = {
                    "seeds": vals,
                    "mean": statistics.fmean(usable) if usable else None,
                    "convention": "sd of the per-trade mean, day-clustered"
                    " (sd of entry-day means / sqrt(days)); the standing"
                    " D8-analog tripwire prior for family runs after this",
                }

    calibration_v2 = {
        "stamp": {
            **_stamp(inputs, _slot_configs(inputs)[0]),
            "trial_id": None,
            "artifact_trial_ids": [_trial_id(c) for c in _slot_configs(inputs)],
            "menu_version": 2,
            "menu_supersedes_v1": inputs.menu.get("supersedes"),
            "amendment_authority": inputs.menu.get("amendment_authority"),
            "menu_hypothesis": _menu_slot(inputs)["hypothesis"],
            "acceptance_criteria": _menu_slot(inputs)["acceptance_criteria"],
            "verdict_vocabulary": _menu_slot(inputs)["verdict_vocabulary"],
            "null_baseline_rule": inputs.menu["rules"]["null_baseline"],
            "v1_calibration": {
                "path": str(CALIBRATION_PATH),
                "registration_menu_sha256": v1["stamp"]["registration_menu_sha256"],
                "dataset_manifest_hash": v1["stamp"]["dataset_manifest_hash"],
                "artifact_trial_ids": v1["stamp"]["artifact_trial_ids"],
                "verdict": v1["verdict"]["slot"],
            },
            "bands": {
                "gross_clustered_t_abs_lt": T_BAND,
                "v2_net_within_kx_cse_of_B": V2_K,
                "superseded_v1_net_per_trade_mean_band": list(NET_BAND),
                "rt_primary": RT_PRIMARY,
                "rt_robust": RT_ROBUST,
                "entry_floors": dict(ENTRY_FLOORS),
            },
        },
        "baseline": {
            "definition": inputs.menu["rules"]["null_baseline"],
            "shapes": baseline_blocks,
            "B": b_table,
        },
        "verdict": {"slot": overall, "seeds": seed_blocks},
        "tripwire_priors": priors,
        "defect_reasons": defect_reasons,
        "not_evaluable_reasons": not_evaluable_reasons,
        "notes": [
            "v2 RE-STAMP (menu v2 drift-relative amendment): B(W, shape) is computed from the",
            "SAME pinned inputs the v1 run stamped (verified against calibration.json); the v1",
            "seed streams and their arithmetic are read from calibration.json, never re-run.",
            "CALIBRATION ONLY (menu declared_use): no direction, no card, no promotion path.",
            "Criterion 1 (|t| < 2 on gross, per seed x sub-era) is UNCHANGED by the amendment;",
            "criterion 2 is drift-relative: seed net within 2x its day-clustered se of B(W, shape).",
            "B is the standing baseline every absolute-mean leg in the menu is read against;",
            "it is calibration, never a signal (wave-0 priors rule unchanged).",
            "Cells with an undefined clustered t or an not-evaluable drift check are disclosed,",
            "never defects (v1 convention); a zero-variance day-mean sd with drift stamps t=inf",
            "and defects.",
        ],
    }
    TNULL_DIR.mkdir(parents=True, exist_ok=True)
    CALIBRATION_V2_PATH.write_text(
        json.dumps(calibration_v2, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    for stream_key in STREAM_KEYS:
        for label in ("card-era", "vrp-cond", "pead-deep-2", "jepa-outer"):
            b = b_table[stream_key][label]
            print(
                f"B[{stream_key:5s}][{label:11s}] = {b['B_net_per_trade_mean']:+.6f}"
                f"  se={b['per_trade_mean_sd_day_clustered']}"
            )
    print(f"overall v2 verdict: {overall}")
    if overall == "DEFECT-FLAGGED":
        for sid, blk in seed_blocks.items():
            print(f"  {sid}: {blk['verdict']} streams={blk['streams']}")
        flag = "\n".join(
            [
                "campaign-2026-09 T-NULL v2 DEFECT FLAG (drift-relative re-stamp)",
                f"written: {_utcnow().isoformat()}",
                f"calibration artifact: {CALIBRATION_V2_PATH}",
                f"menu: v2 sha256 {inputs.menu_sha256} (supersedes {inputs.menu.get('supersedes')})",
                "",
                "A seed still violates the menu v2 acceptance criteria;",
                "family sealed-window scoring stays FROZEN pending another",
                "operator ruling (menu rules.sequencing AMENDMENT v2).",
                "",
                "v2 defect reasons:",
                *(f"- {r}" for r in defect_reasons),
                *(f"- disclosure: {r}" for r in not_evaluable_reasons),
                "",
            ]
        )
        DEFECT_FLAG_V2_PATH.write_text(flag, encoding="utf-8")
        print(f"DEFECT-FLAGGED (v2) -- flag file {DEFECT_FLAG_V2_PATH}; family scoring stays frozen")
        for r in defect_reasons:
            print(f"  - {r}")
    else:
        print(f"CALIBRATED (v2) -- re-stamp at {CALIBRATION_V2_PATH}; family scoring unfrozen")
    return 0


# -- v3 re-stamp (menu v3, NOT_EVALUABLE floor) -----------------------------------------


def _read_v2_calibration(inputs: Inputs) -> Mapping[str, Any]:
    """The v2 calibration artifact is the SEALED cell evidence for v3: its
    per-window stats (``v1_windows``) and its v2 criterion arithmetic
    (``v2_checks``) are read, never re-derived. Refuse unless it binds
    this menu's ``supersedes`` pin, the same pinned inputs, the same
    trial ids, and the same panel cutoff (which fixes jepa-outer)."""
    if not CALIBRATION_V2_PATH.exists():
        raise Refused(f"{CALIBRATION_V2_PATH} is missing -- the v2 stamp is the sealed cell evidence")
    body = json.loads(CALIBRATION_V2_PATH.read_text(encoding="utf-8"))
    stamp = body.get("stamp", {})
    supersedes = inputs.menu.get("supersedes")
    if not supersedes:
        raise Refused("the menu carries no `supersedes` pin -- not a v3 amendment")
    if stamp.get("registration_menu_sha256") != supersedes:
        raise Refused(
            "the v2 calibration's menu sha is not this menu's supersedes pin"
            f" ({stamp.get('registration_menu_sha256')} != {supersedes})"
        )
    if stamp.get("dataset_manifest_hash") != inputs.dataset_manifest_hash:
        raise Refused("the v2 calibration was stamped on a different dataset manifest")
    if stamp.get("inputs_sha256") != {
        "ohlc-panel.json": inputs.panel_sha256,
        "earnings-calendar.json": inputs.earnings_sha256,
        "nyse_sessions json": inputs.calendar_sha256,
    }:
        raise Refused("the v2 calibration's per-input sha256 do not match the pinned inputs")
    if stamp.get("artifact_trial_ids") != [_trial_id(c) for c in _slot_configs(inputs)]:
        raise Refused("the v2 calibration binds different artifact trial ids")
    if stamp.get("cutoff_earliest_last_session_chain35") != inputs.cutoff_iso:
        raise Refused("the panel cutoff moved since the v2 stamp -- the windows differ")
    labels = [label for label, _s, _e in _windows(inputs)]
    seeds = body.get("verdict", {}).get("seeds", {})
    if sorted(seeds) != ["tnull-s1", "tnull-s2", "tnull-s3"]:
        raise Refused("the v2 calibration does not carry exactly the three seed blocks")
    for slot_id, blk in seeds.items():
        for stream_key in STREAM_KEYS:
            cells = blk.get("cells", {}).get(stream_key, {})
            if sorted(cells.get("v1_windows", {})) != sorted(labels):
                raise Refused(
                    f"v2 seed {slot_id}/{stream_key} window labels do not match the re-derived windows"
                )
            checks = cells.get("v2_checks", {})
            if sorted(checks) != sorted(lbl for lbl in labels if lbl != "union"):
                raise Refused(
                    f"v2 seed {slot_id}/{stream_key} criterion checks do not cover every sub-era"
                )
    b_table = body.get("baseline", {}).get("B", {})
    if sorted(b_table) != sorted(STREAM_KEYS):
        raise Refused("the v2 calibration does not carry B(W, shape) for both shapes")
    for stream_key in STREAM_KEYS:
        if sorted(b_table[stream_key]) != sorted(labels):
            raise Refused(f"the v2 B table does not cover every window for shape {stream_key}")
    return body


def _v3_cell_classify(cell: Mapping[str, Any]) -> tuple[str, list[str]]:
    """Menu v3 floor for one seed x window x shape cell: below 5
    entry-days or 20 complete trades the cell is NOT_EVALUABLE (values
    reported, no flag authority, no priors)."""
    days = cell.get("days")
    n_complete = cell.get("n_complete")
    reasons: list[str] = []
    if not isinstance(days, int) or not isinstance(n_complete, int):
        return "NOT_EVALUABLE", ["days/n_complete not stamped as integers"]
    if days < V3_MIN_ENTRY_DAYS:
        reasons.append(f"entry-days {days} < {V3_MIN_ENTRY_DAYS}")
    if n_complete < V3_MIN_COMPLETE_TRADES:
        reasons.append(f"complete trades {n_complete} < {V3_MIN_COMPLETE_TRADES}")
    return ("NOT_EVALUABLE" if reasons else "EVALUABLE"), reasons


def _v3_stream_checks(
    inputs: Inputs,
    v2_stream_cells: Mapping[str, Any],
    stream_key: str,
) -> tuple[str, list[str], dict[str, Any]]:
    """Menu v3 verdict for one (seed x stream): the union entry floors are
    UNCHANGED (criterion 3); every sub-era cell is first classified by
    the v3 floor and only EVALUABLE cells are judged on the two v2
    criteria, whose pass arithmetic (t, delta-vs-B, band) is carried
    from the v2 stamp unchanged -- same numbers, never re-derived."""
    if v2_stream_cells["floor_entries_union"] < ENTRY_FLOORS[stream_key]:
        return (
            "NOT_EVALUABLE",
            [],
            {
                "floor_entries_union": v2_stream_cells["floor_entries_union"],
                "reason": f"entry floor missed: {v2_stream_cells['floor_entries_union']}"
                f" < {ENTRY_FLOORS[stream_key]}",
            },
        )
    reasons: list[str] = []
    cells_out: dict[str, Any] = {}
    for label, _s, _e in _windows(inputs):
        if label == "union":
            continue  # floors bind on the union; bands on the sub-eras (v1 convention)
        cell = v2_stream_cells["v1_windows"][label]
        classification, floor_reasons = _v3_cell_classify(cell)
        row: dict[str, Any] = {
            "days": cell["days"],
            "n_complete": cell["n_complete"],
            "n_entries": cell["n_entries"],
            "classification": classification,
        }
        if classification == "NOT_EVALUABLE":
            # values reported for transparency; no flag authority, no priors
            row.update(
                {
                    "floor_reasons": floor_reasons,
                    "verdict": "NOT_EVALUABLE",
                    "gross_clustered_t": cell["gross_clustered_t"],
                    "net_per_trade_mean": cell["net_per_trade_mean"],
                    "per_trade_mean_sd_day_clustered": cell["per_trade_mean_sd_day_clustered"],
                }
            )
        else:
            chk = v2_stream_cells["v2_checks"][label]
            t_pass = chk.get("t_pass")
            b_pass = chk.get("baseline_pass")
            row.update(
                {
                    "floor_reasons": [],
                    "criterion_1_t_pass": t_pass,
                    "criterion_2_baseline_pass": b_pass,
                    "gross_clustered_t": chk.get("gross_clustered_t"),
                    "net_per_trade_mean": chk.get("net_per_trade_mean"),
                    "seed_cse_day_clustered": chk.get("seed_cse_day_clustered"),
                    "B": chk.get("B"),
                    "delta_net_minus_B": chk.get("delta_net_minus_B"),
                    "band_halfwidth": chk.get("band_halfwidth"),
                }
            )
            row["verdict"] = "PASS" if (t_pass is True and b_pass is True) else "FAIL"
            if row["verdict"] == "FAIL":
                if t_pass is not True:
                    reasons.append(
                        f"{label}: |t|={chk['gross_clustered_t']:.3f} >= {T_BAND}"
                        " on gross per-trade mean (criterion 1, unchanged)"
                    )
                if b_pass is not True:
                    reasons.append(
                        f"{label}: net per-trade mean {chk['net_per_trade_mean']:.6f} outside"
                        f" B +/- {V2_K:g}x day-clustered se (B={chk['B']:.6f},"
                        f" delta={chk['delta_net_minus_B']:.6f}, band={chk['band_halfwidth']:.6f})"
                    )
        cells_out[label] = row
    return ("DEFECT-FLAGGED" if reasons else "CALIBRATED"), reasons, cells_out


def phase_v3_floor() -> int:
    """The v3 re-stamp: the NOT_EVALUABLE floor applied to the EXISTING
    v2 artifact's per-cell stats; B(W, shape) carried forward verbatim;
    priors from EVALUABLE cells only."""
    inputs = load_and_bind()
    criteria = _menu_slot(inputs)["acceptance_criteria"]
    if inputs.menu.get("version") != 3 or not any(
        "v3 sub-evaluability floor" in c for c in criteria
    ):
        raise Refused("the sealed menu is not the v3 NOT_EVALUABLE-floor amendment")
    if "AMENDMENT v3 sub-evaluability floor" not in inputs.menu.get("rules", {}).get(
        "null_baseline", ""
    ):
        raise Refused("the menu's null_baseline rule does not carry the v3 floor amendment")
    if CALIBRATION_V3_PATH.exists():
        raise Refused(f"{CALIBRATION_V3_PATH} already exists -- the v3 re-stamp is one-shot")
    v2 = _read_v2_calibration(inputs)
    _registry_g2_check(inputs)

    seed_blocks: dict[str, Any] = {}
    defect_reasons: list[str] = []
    not_evaluable_cells: list[dict[str, Any]] = []
    for config in _slot_configs(inputs):
        slot_id = config["slot_id"]
        v2_seed = v2["verdict"]["seeds"][slot_id]
        streams_verdict: dict[str, str] = {}
        stream_cells: dict[str, Any] = {}
        for stream_key in STREAM_KEYS:
            verdict, reasons, cells_out = _v3_stream_checks(
                inputs, v2_seed["cells"][stream_key], stream_key
            )
            streams_verdict[stream_key] = verdict
            stream_cells[stream_key] = {
                "floor_entries_union": v2_seed["cells"][stream_key]["floor_entries_union"],
                "v3_cells": cells_out,
                "reasons": reasons,
            }
            if verdict == "DEFECT-FLAGGED":
                defect_reasons.extend(f"{slot_id}/{stream_key}: {r}" for r in reasons)
            for label, row in cells_out.items():
                if row.get("classification") == "NOT_EVALUABLE":
                    not_evaluable_cells.append(
                        {
                            "seed": slot_id,
                            "shape": stream_key,
                            "window": label,
                            "days": row["days"],
                            "n_complete": row["n_complete"],
                            "floor_reasons": row["floor_reasons"],
                        }
                    )
        seed_blocks[slot_id] = {
            "trial_id": _trial_id(config),
            "v2_verdict": v2_seed["verdict"],
            "verdict": _lattice(streams_verdict.values()),
            "streams": streams_verdict,
            "cells": stream_cells,
        }
    overall = _lattice(b["verdict"] for b in seed_blocks.values())

    # tripwire prior block: the v1 convention -- stamped ONLY by a
    # CALIBRATED null (wave-0 rule: priors come only from executed null
    # artifacts); menu v3: built from EVALUABLE cells only, so a window
    # with no evaluable cell carries NO prior -- families in that window
    # are gated by B(W, shape) alone.
    priors: dict[str, Any] = {}
    if overall == "CALIBRATED":
        for stream_key in STREAM_KEYS:
            priors[stream_key] = {}
            for label, _s, _e in _windows(inputs):
                vals = []
                for config in _slot_configs(inputs):
                    slot_id = config["slot_id"]
                    if label == "union":
                        row_class = "EVALUABLE"  # the union span floors the stream (criterion 3)
                        if v2["verdict"]["seeds"][slot_id]["cells"][stream_key][
                            "floor_entries_union"
                        ] < ENTRY_FLOORS[stream_key]:
                            row_class = "NOT_EVALUABLE"
                    else:
                        row_class = seed_blocks[slot_id]["cells"][stream_key]["v3_cells"][label][
                            "classification"
                        ]
                    if row_class != "EVALUABLE":
                        continue
                    sd = v2["verdict"]["seeds"][slot_id]["cells"][stream_key]["v1_windows"][label][
                        "per_trade_mean_sd_day_clustered"
                    ]
                    if sd is not None:
                        vals.append(sd)
                if not vals:
                    continue  # no evaluable cell -> no prior; B(W, shape) gates that window
                priors[stream_key][label] = {
                    "seeds": vals,
                    "mean": statistics.fmean(vals),
                    "convention": "sd of the per-trade mean, day-clustered"
                    " (sd of entry-day means / sqrt(days)); the standing"
                    " D8-analog tripwire prior for family runs after this;"
                    " menu v3: EVALUABLE cells only",
                }

    calibration_v3 = {
        "stamp": {
            **_stamp(inputs, _slot_configs(inputs)[0]),
            "trial_id": None,
            "artifact_trial_ids": [_trial_id(c) for c in _slot_configs(inputs)],
            "menu_version": 3,
            "menu_supersedes_v2": inputs.menu.get("supersedes"),
            "amendment_authority": inputs.menu.get("amendment_authority"),
            "menu_hypothesis": _menu_slot(inputs)["hypothesis"],
            "acceptance_criteria": criteria,
            "verdict_vocabulary": _menu_slot(inputs)["verdict_vocabulary"],
            "null_baseline_rule": inputs.menu["rules"]["null_baseline"],
            "v2_calibration": {
                "path": str(CALIBRATION_V2_PATH),
                "registration_menu_sha256": v2["stamp"]["registration_menu_sha256"],
                "dataset_manifest_hash": v2["stamp"]["dataset_manifest_hash"],
                "artifact_trial_ids": v2["stamp"]["artifact_trial_ids"],
                "runner_sha256": v2["stamp"]["runner_sha256"],
                "git_sha": v2["stamp"]["git_sha"],
                "verdict": v2["verdict"]["slot"],
            },
            "bands": {
                "gross_clustered_t_abs_lt": T_BAND,
                "v2_net_within_kx_cse_of_B": V2_K,
                "v3_min_entry_days": V3_MIN_ENTRY_DAYS,
                "v3_min_complete_trades": V3_MIN_COMPLETE_TRADES,
                "superseded_v1_net_per_trade_mean_band": list(NET_BAND),
                "rt_primary": RT_PRIMARY,
                "rt_robust": RT_ROBUST,
                "entry_floors": dict(ENTRY_FLOORS),
            },
        },
        "baseline": v2["baseline"],  # B(W, shape) + shape blocks carried forward VERBATIM
        "verdict": {"slot": overall, "seeds": seed_blocks},
        "not_evaluable_cells": not_evaluable_cells,
        "tripwire_priors": priors,
        "defect_reasons": defect_reasons,
        "notes": [
            "v3 RE-STAMP (menu v3 NOT_EVALUABLE-floor amendment): every seed x window x shape",
            "cell was classified from the EXISTING calibration-v2.json per-cell stats;",
            "nothing was re-fetched, re-randomized, or re-run, and B(W, shape) is carried",
            "forward verbatim from the v2 stamp.",
            "Cells below the floor (< 5 entry-days or < 20 complete trades) are NOT_EVALUABLE:",
            "values reported for transparency, no flag authority, no priors.",
            "The null is CALIBRATED iff every EVALUABLE cell passes BOTH criteria on every seed;",
            "the pass arithmetic (t, delta-vs-B, band) is the v2 stamp's own numbers.",
            "Tripwire priors are stamped only because the null is CALIBRATED, and are built",
            "from EVALUABLE cells only; windows absent from the prior block have no evaluable",
            "cell -- families in those windows are gated by B(W, shape) alone.",
            "CALIBRATION ONLY (menu declared_use): no direction, no card, no promotion path.",
        ],
    }
    TNULL_DIR.mkdir(parents=True, exist_ok=True)
    CALIBRATION_V3_PATH.write_text(
        json.dumps(calibration_v3, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"menu v3 sha256 {inputs.menu_sha256} (sidecar-verified)")
    print(f"NOT_EVALUABLE cells: {len(not_evaluable_cells)}")
    for c in not_evaluable_cells:
        print(
            f"  {c['seed']}/{c['shape']:5s}/{c['window']:11s}"
            f"  days={c['days']} complete={c['n_complete']}  ({'; '.join(c['floor_reasons'])})"
        )
    for sid, blk in seed_blocks.items():
        print(f"  {sid}: {blk['verdict']} streams={blk['streams']}")
    print(f"overall v3 verdict: {overall}")
    if overall == "DEFECT-FLAGGED":
        flag = "\n".join(
            [
                "campaign-2026-09 T-NULL v3 DEFECT FLAG (NOT_EVALUABLE-floor re-stamp)",
                f"written: {_utcnow().isoformat()}",
                f"calibration artifact: {CALIBRATION_V3_PATH}",
                f"menu: v3 sha256 {inputs.menu_sha256} (supersedes {inputs.menu.get('supersedes')})",
                "",
                "An EVALUABLE cell violates the menu acceptance criteria;",
                "family sealed-window scoring stays FROZEN pending another",
                "operator ruling (menu rules.sequencing AMENDMENT v3).",
                "",
                "v3 defect reasons:",
                *(f"- {r}" for r in defect_reasons),
                "",
                "NOT_EVALUABLE cells (reported, no flag authority):",
                *(
                    f"- {c['seed']}/{c['shape']}/{c['window']}:"
                    f" days={c['days']} complete={c['n_complete']}"
                    for c in not_evaluable_cells
                ),
                "",
            ]
        )
        DEFECT_FLAG_V3_PATH.write_text(flag, encoding="utf-8")
        print(f"DEFECT-FLAGGED (v3) -- flag file {DEFECT_FLAG_V3_PATH}; family scoring stays frozen")
        for r in defect_reasons:
            print(f"  - {r}")
    else:
        print(f"CALIBRATED (v3) -- re-stamp at {CALIBRATION_V3_PATH}; family scoring unfrozen")
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
    parser.add_argument(
        "--baseline",
        action="store_true",
        help="menu v2 re-stamp: compute B(W, shape) from the pinned inputs, re-evaluate"
        " the drift-relative criteria on the v1 calibration's seed cells, and stamp"
        " calibration-v2.json (or the v2 DEFECT flag)",
    )
    parser.add_argument(
        "--v3-floor",
        action="store_true",
        help="menu v3 re-stamp: classify every seed x window x shape cell by the"
        " NOT_EVALUABLE floor (< 5 entry-days or < 20 complete trades) using the"
        " EXISTING calibration-v2.json cells, carry B(W, shape) forward verbatim,"
        " and stamp calibration-v3.json (or the v3 DEFECT flag)",
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
        if args.baseline:
            return phase_baseline()
        if args.v3_floor:
            return phase_v3_floor()
        if args.plan:
            return phase_plan()
    except Refused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
