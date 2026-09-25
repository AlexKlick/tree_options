#!/usr/bin/env python3
"""campaign-2026-09 PEAD-DEEP-2 runner (scope ``c09-pd2``, menu order 2).

ROUND 1 = INNER FOLDS ONLY. Conditioning deepening of the sealed
PEAD-BIGSURPRISE card rule (``pead_beat`` in
``src/tree_options/desk/signals.py``), executed exactly as the sealed menu
entry ``slots[2]`` of ``docs/theory/campaign-2026-09-registration.json``
(menu v3, sha256 sidecar-verified) and
``docs/theory/campaign-2026-09/slots/pead-deep-2.md`` (sha pinned by the
menu's ``dataset_pinning``) specify: 24 registered cells, one scope
``c09-pd2``, cap 32.

Binding invariants enforced before anything runs (REFUSED on drift):
menu sidecar sha == menu bytes; the T-NULL calibration-v3.json artifact
stamps THIS menu sha (family scoring unfroze only under that stamp); the
slot doc hashes to the menu's pin; the protocol raw sha equals the menu's
``protocol_hash``; every input file (ohlc-panel, earnings-calendar,
earnings-timing, VIX.csv, sealed NYSE calendar) hashes to its
``dataset_pinning`` pin; the calendar still carries exactly one removed
phantom session (2025-01-09); the era/inner/sealed ordinal arithmetic
still holds (inner end 2026-04-20 = ordinal 406, 406+40+5 < 452 =
ordinal(2026-06-25); sealed = the final 63 sessions).

Evaluation span (ROUND 1): beat events with entry session in
[2024-09-05 .. 2026-04-20] ONLY. The sealed window (entries
2026-06-25..2026-09-23) is NEVER walked, scored, read for tuning, or
plotted by this runner: the session walk stops at 2026-04-20, and a hard
guard asserts no computed trade row carries an entry or exit session
>= 2026-06-25 (the deepest inner h40 label completes by ordinal ~446,
strictly inside the purge gap the registration verified).

Firing rule: EXACTLY ``pead_beats()`` from the sealed signals module
(threshold +1.5%, clean-back 5, hole/prior-gap guards), driven over the
sealed calendar minus the phantom session. Entry at close(s), exit at
close(s+h) on CALENDAR ordinals; the name must carry every session in
(s, s+h] (house hold filter; the iter002 ``window_clean`` variant is
cross-checked and any divergence disclosed). net = close(s+h)/close(s)
- 1 - 5bp (Decimal closes); 15bp robustness disclosed. Day-clustered t
(ddof=1) per ``iter002.py stats_of``; hole guard > 10 calendar days.

Arms (24 cells, ids frozen in the menu):
  B  PD2-B-h{10,20,40}          unconditioned beats (the reference)
  M  PD2-M-{lo,hi}-h{10,20,40}  beat-move median split (edge from the
                                inner span only, iter002 index convention)
  L  PD2-L-{mod,str,ext}        fixed bands +1.5..3% / 3..6% / >=6%, h20
  V  PD2-V-{lo,mid,hi}-h{...}   prior-session VIX close tercile (inner edges)
  R  PD2-R-{lo,mid,hi}          SPY RV20(t-1) tercile (inner edges), h20

Cell bar (menu v2 amendment, drift-relative; the values are READ from
``artifacts/campaign-2026-09/tnull/calibration-v3.json``, never
recomputed): (1) mean - B(pead-deep-2 window, event shape) > 0 -- the
primary basis pairs the iter002 day-clustered net mean with the
calibration's ``B_net_day_clustered_mean`` (the null_baseline_rule's own
"day-clustered NET per-trade mean"); the per-trade pairing
(per-trade net mean vs ``B_net_per_trade_mean``) is stamped as a
disclosed sensitivity leg, never a second verdict; (2) day-clustered
t >= 2; (3) n_days >= 30; (4) n >= 20; (5) conditional
(stratum - PD2-B same hold) > 0. Verdict: INSUFFICIENT_N when n or
n_days is below the minima (never read as pass or fail); else CANDIDATE
iff every leg passes, else NULL. PD2-B cells carry conditional == 0 by
construction and therefore cannot be CANDIDATE (their role is the
reference; the mechanical application of the pre-declared bar). Family
verdicts (STRATUM-CONFIRMED / FAMILY-NULL) and the terminal
PROMOTE-AS-GATE / CLOSE belong to the sealed round, NOT round 1.

The forward-only timing arm (PD2-T-bmo/amc/unknown) registers 0
backtest cells: historical timing coverage is zero, so its standing
verdict is INSUFFICIENT_COVERAGE and it can never gate a card. The
runner only counts the current in-universe coverage from
earnings-timing.json for disclosure.

INV-13: ``--register`` writes all 24 trial rows (REGISTERED, no
outcome, edges recorded as RULES not values) to
``artifacts/campaign-2026-09/pead-deep-2.db`` BEFORE any outcome is
computed or viewed; ``--execute`` is one-shot per trial (REGISTERED ->
RUNNING -> COMPLETED, the per-config artifact as metrics_uri);
``--summarize`` reads ONLY the executed artifacts (stamp-bound to their
trial ids) and stamps ``round1-summary.json``. ``--timing`` prints the
forward-only coverage note. Nothing is adopted, nothing seals a card,
the RESEARCH-LEDGER is not touched.
"""

from __future__ import annotations

import argparse
import csv
import fcntl
import hashlib
import json
import math
import os
import statistics
import subprocess
import sys
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

from tree_options.desk.signals import (  # noqa: E402
    PEAD_CLEAN_BACK,
    PEAD_THRESHOLD,
    pead_beats,
)
from tree_options.registry.scope import TrialScope  # noqa: E402
from tree_options.registry.sqlite import TrialRegistry  # noqa: E402
from tree_options.schemas.trial import TrialRecord  # noqa: E402

# Data lives in the MAIN checkout; the runner + registration live in the
# execution worktree. Both are pinned by sha256 against the menu below.
MAIN_ROOT = Path("/home/alexk/documents/tree_options")
REGISTRATION_PATH = REPO_ROOT / "docs" / "theory" / "campaign-2026-09-registration.json"
REGISTRATION_SIDECAR = Path(str(REGISTRATION_PATH) + ".sha256")
SLOT_DOC_PATH = REPO_ROOT / "docs" / "theory" / "campaign-2026-09" / "slots" / "pead-deep-2.md"
PROTOCOL_PATH = REPO_ROOT / "research_protocol.yaml"
PANEL_PATH = MAIN_ROOT / "artifacts" / "paper-trades" / "ohlc-panel.json"
PANEL_LOCK_PATH = MAIN_ROOT / "artifacts" / "paper-trades" / "ohlc-panel.json.lock"
EARNINGS_PATH = MAIN_ROOT / "artifacts" / "paper-trades" / "earnings-calendar.json"
TIMING_PATH = MAIN_ROOT / "artifacts" / "paper-trades" / "earnings-timing.json"
VIX_PATH = MAIN_ROOT / "artifacts" / "desk-store" / "indices" / "VIX.csv"
CALENDAR_PATH = MAIN_ROOT / "data" / "calendar" / "nyse_sessions_2018_01_02_2026_12_31.json"
CALIBRATION_V3_PATH = MAIN_ROOT / "artifacts" / "campaign-2026-09" / "tnull" / "calibration-v3.json"

CAMPAIGN_DIR = MAIN_ROOT / "artifacts" / "campaign-2026-09"
REGISTRY_PATH = CAMPAIGN_DIR / "pead-deep-2.db"
PD2_DIR = CAMPAIGN_DIR / "pead-deep-2"
TRIALS_DIR = PD2_DIR / "trials"
SUMMARY_PATH = PD2_DIR / "round1-summary.json"
LOCK_PATH = PD2_DIR / "execute.lock"

SCOPE_ID = "c09-pd2"
SLOT_ID = "pead-deep-2"
MODEL_FAMILY = "pead-beat/conditioned-strata/1"
TRIAL_GENERATION = 1

# The pinned geometry (menu fold_mapping + slot doc section 4).
PHANTOM_ISO = "2025-01-09"  # ledger ruling 2026-09-23: not a session
ERA_START = "2024-09-05"
ERA_END = "2026-09-23"
INNER_END = "2026-04-20"  # ordinal 406; 406+40+5 = 451 < 452 = ordinal(2026-06-25)
SEALED_START = "2026-06-25"  # the final 63 sessions; NEVER touched in round 1
ERA_SESSIONS = 514
SEALED_SESSIONS = 63
HOLD_SESSIONS = (10, 20, 40)
RT_PRIMARY = 0.0005  # 5bp round trip, primary
RT_ROBUST = 0.0015  # 15bp round trip, robustness disclosure
HOLE_DAYS = 10
T_BAR = 2.0
N_MIN = 20
DAYS_MIN = 30
# fixed ladder bands (arm L), on the sealed rule's own variable m
L_MOD = Decimal("0.03")
L_EXT = Decimal("0.06")
# B binding (menu v2 amendment): read from calibration-v3.json, never recomputed
B_WINDOW = "pead-deep-2"
B_SHAPE = "event"

# The frozen 24-cell grid (menu config_ids order; any drift refuses).
EXPECTED_CONFIG_IDS: tuple[str, ...] = (
    "PD2-B-h10",
    "PD2-B-h20",
    "PD2-B-h40",
    "PD2-M-lo-h10",
    "PD2-M-hi-h10",
    "PD2-M-lo-h20",
    "PD2-M-hi-h20",
    "PD2-M-lo-h40",
    "PD2-M-hi-h40",
    "PD2-L-mod",
    "PD2-L-str",
    "PD2-L-ext",
    "PD2-V-lo-h10",
    "PD2-V-mid-h10",
    "PD2-V-hi-h10",
    "PD2-V-lo-h20",
    "PD2-V-mid-h20",
    "PD2-V-hi-h20",
    "PD2-V-lo-h40",
    "PD2-V-mid-h40",
    "PD2-V-hi-h40",
    "PD2-R-lo",
    "PD2-R-mid",
    "PD2-R-hi",
)

VERDICTS = ("CANDIDATE", "NULL", "INSUFFICIENT_N")


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


class Refused(RuntimeError):
    """A binding refusal: the sealed menu and the execution disagree."""


# ---- sealed calendar -------------------------------------------------------------------


class SealedCalendar:
    """The sealed NYSE calendar minus the 2025-01-09 phantom session
    (ClosureCorrectedCalendar idiom; the sealed FILE stays byte-identical)."""

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

    def previous_session(self, d: date) -> date | None:
        import bisect

        sessions = self._sessions
        i = bisect.bisect_left(sessions, d) - 1
        return sessions[i] if i >= 0 else None


@dataclass(frozen=True)
class Inputs:
    menu: Mapping[str, Any]
    menu_sha256: str
    slot: Mapping[str, Any]
    protocol_raw_sha256: str
    protocol_canonical_sha256: str
    slot_doc_sha256: str
    panel: Mapping[str, Mapping[str, Mapping[str, Any]]]
    panel_sha256: str
    earnings: Mapping[str, Sequence[str]]
    earnings_sha256: str
    timing: Mapping[str, Mapping[str, Any]]
    timing_sha256: str
    vix: Mapping[str, float]
    vix_sha256: str
    calendar: SealedCalendar
    calendar_sha256: str
    chain35: tuple[str, ...]
    reporters: tuple[str, ...]
    B: Mapping[str, Any]
    b_not_evaluable: bool
    dataset_manifest_hash: str
    inner_population_sessions: int


def _read_vix(path: Path) -> dict[str, float]:
    out: dict[str, float] = {}
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            d = (row.get("date") or "").strip()
            c = (row.get("close") or "").strip()
            if d and c:
                out[d] = float(c)
    if not out:
        raise Refused("VIX.csv parsed empty")
    return out


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
    if menu.get("version") != 3:
        raise Refused(f"menu version is {menu.get('version')!r}, not the v3 amendment")
    slot = next((s for s in menu["slots"] if s.get("slot_id") == SLOT_ID), None)
    if slot is None or slot.get("order") != 2 or slot.get("family") != "PEAD-DEEP-2":
        raise Refused("the menu's order-2 PEAD-DEEP-2 slot is missing or malformed")
    scopes = {sc["scope_id"]: sc for sc in slot["scope_ids"]}
    if scopes.get(SCOPE_ID, {}).get("config_count") != 24 or scopes[SCOPE_ID].get("cap") != 32:
        raise Refused(f"menu scope {SCOPE_ID} does not carry exactly 24 configs / cap 32")
    if tuple(slot["config_ids"]) != EXPECTED_CONFIG_IDS:
        raise Refused("menu pead-deep-2 config ids are not the frozen 24 in order")
    if slot.get("config_count") != 24:
        raise Refused("menu pead-deep-2 config_count is not 24")

    # the T-NULL calibration gate: family scoring unfroze ONLY under a
    # calibration-v3.json stamping THIS menu sha (menu rules.sequencing,
    # amendment v3); its slot verdict must be CALIBRATED (not DEFECT-FLAGGED)
    if not CALIBRATION_V3_PATH.exists():
        raise Refused(f"{CALIBRATION_V3_PATH} is missing -- the null calibration gate is unmet")
    calibration = json.loads(CALIBRATION_V3_PATH.read_text(encoding="utf-8"))
    c_stamp = calibration.get("stamp", {})
    if c_stamp.get("registration_menu_sha256") != menu_sha256:
        raise Refused(
            "calibration-v3.json does not stamp this menu sha -- family scoring stays frozen"
        )
    if calibration.get("verdict", {}).get("slot") != "CALIBRATED":
        raise Refused(
            f"the T-NULL slot verdict is {calibration.get('verdict', {}).get('slot')!r}"
            " -- family scoring stays frozen pending an operator ruling"
        )
    try:
        b_row = calibration["baseline"]["B"][B_SHAPE][B_WINDOW]
    except KeyError:
        raise Refused(f"calibration-v3.json carries no B({B_WINDOW}, {B_SHAPE})") from None
    for key in ("B_net_per_trade_mean", "B_net_day_clustered_mean"):
        if not isinstance(b_row.get(key), float):
            raise Refused(f"B({B_WINDOW}, {B_SHAPE}).{key} is not stamped")
    b_not_evaluable = any(
        c.get("window") == B_WINDOW and c.get("shape") == B_SHAPE
        for c in calibration.get("not_evaluable_cells", [])
    )

    # protocol: raw bytes must equal the menu pin; canonical hash re-stamped (INV-14)
    from tree_options.protocol.loader import default_protocol, protocol_hash

    protocol_raw_sha256 = _sha256_file(PROTOCOL_PATH)
    if menu["protocol_hash"] != protocol_raw_sha256:
        raise Refused(
            "research_protocol.yaml raw sha256 does not match the menu's protocol_hash"
            " -- a protocol change requires a NEW registration"
        )
    protocol_canonical_sha256 = protocol_hash(default_protocol())

    # data: the five pinned inputs this slot consumes
    pinning = menu["dataset_pinning"]
    slot_doc_sha256 = _sha256_file(SLOT_DOC_PATH)
    panel_sha256 = _sha256_file(PANEL_PATH)
    earnings_sha256 = _sha256_file(EARNINGS_PATH)
    timing_sha256 = _sha256_file(TIMING_PATH)
    vix_sha256 = _sha256_file(VIX_PATH)
    calendar_sha256 = _sha256_file(CALENDAR_PATH)
    for label, got in (
        ("artifacts/paper-trades/ohlc-panel.json", panel_sha256),
        ("artifacts/paper-trades/earnings-calendar.json", earnings_sha256),
        ("artifacts/paper-trades/earnings-timing.json", timing_sha256),
        ("artifacts/desk-store/indices/VIX.csv", vix_sha256),
        ("data/calendar/nyse_sessions_2018_01_02_2026_12_31.json", calendar_sha256),
        ("docs/theory/campaign-2026-09/slots/pead-deep-2.md", slot_doc_sha256),
    ):
        want = pinning.get(label)
        if want is None or want != got:
            raise Refused(f"{label}: sha256 {got} != the menu's pinned {want} -- swapped inputs refuse")

    # the panel is read under the shared flock (slot doc section 3)
    PANEL_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(PANEL_LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_SH)
        panel = json.loads(PANEL_PATH.read_text(encoding="utf-8"))
    finally:
        os.close(lock_fd)
    earnings = json.loads(EARNINGS_PATH.read_text(encoding="utf-8"))
    timing = json.loads(TIMING_PATH.read_text(encoding="utf-8"))
    vix = _read_vix(VIX_PATH)
    calendar_json = json.loads(CALENDAR_PATH.read_text(encoding="utf-8"))
    calendar = SealedCalendar(calendar_json["sessions"])
    if calendar.removed != (PHANTOM_ISO,):
        raise Refused(f"the phantom session was not removed: {calendar.removed}")

    # universes, pinned to the panel itself
    from tree_options.desk.universe import CHAIN_UNIVERSE

    panel_names = sorted(panel)
    if len(panel_names) != 37:
        raise Refused(f"panel carries {len(panel_names)} names, expected the sealed 37")
    chain35 = tuple(n for n in panel_names if n not in ("TQQQ", "SQQQ"))
    if len(chain35) != 35 or not set(chain35) <= set(CHAIN_UNIVERSE):
        raise Refused("panel-minus-TQQQ/SQQQ is not the 35-name chain universe")
    reporters = tuple(sorted(n for n, reps in earnings.items() if reps))
    if len(reporters) != 26 or not set(reporters) <= set(chain35):
        raise Refused(
            f"earnings-calendar reporters are not the 26 chain-universe names: {len(reporters)}"
        )

    # pinned ordinal arithmetic (menu fold_mapping): refuse if the calendar moved.
    # The registration's ordinals are ERA-RELATIVE (era session 1..514).
    era_start = date.fromisoformat(ERA_START)
    era_end = date.fromisoformat(ERA_END)
    inner_end = date.fromisoformat(INNER_END)
    sealed_start = date.fromisoformat(SEALED_START)

    def era_ord(d: date) -> int:
        return calendar.ordinal(d) - calendar.ordinal(era_start) + 1

    if calendar.ordinal(era_end) - calendar.ordinal(era_start) != ERA_SESSIONS - 1:
        raise Refused("the era is no longer 514 sessions -- the calendar moved")
    if calendar.ordinal(era_end) - calendar.ordinal(sealed_start) != SEALED_SESSIONS - 1:
        raise Refused("the sealed window is no longer the final 63 sessions")
    inner_ord = era_ord(inner_end)
    sealed_ord = era_ord(sealed_start)
    if inner_ord + max(HOLD_SESSIONS) + 5 >= sealed_ord:
        raise Refused(
            "the inner purge boundary no longer clears the sealed window at h40+5"
            f" ({inner_ord}+45 >= {sealed_ord})"
        )
    if inner_ord != 406 or sealed_ord != 452:
        raise Refused(
            f"inner/sealed era-ordinals moved: inner={inner_ord} (want 406), sealed={sealed_ord} (want 452)"
        )
    inner_population_sessions = sum(
        1
        for s in calendar.sessions()
        if era_start <= s <= inner_end
    )

    manifest_body = "".join(
        f"{label}\0{pinning[label]}\n"
        for label in (
            "artifacts/paper-trades/ohlc-panel.json",
            "artifacts/paper-trades/earnings-calendar.json",
            "artifacts/paper-trades/earnings-timing.json",
            "artifacts/desk-store/indices/VIX.csv",
            "data/calendar/nyse_sessions_2018_01_02_2026_12_31.json",
        )
    )
    dataset_manifest_hash = hashlib.sha256(manifest_body.encode("utf-8")).hexdigest()
    return Inputs(
        menu=menu,
        menu_sha256=menu_sha256,
        slot=slot,
        protocol_raw_sha256=protocol_raw_sha256,
        protocol_canonical_sha256=protocol_canonical_sha256,
        slot_doc_sha256=slot_doc_sha256,
        panel=panel,
        panel_sha256=panel_sha256,
        earnings=earnings,
        earnings_sha256=earnings_sha256,
        timing=timing,
        timing_sha256=timing_sha256,
        vix=vix,
        vix_sha256=vix_sha256,
        calendar=calendar,
        calendar_sha256=calendar_sha256,
        chain35=chain35,
        reporters=reporters,
        B=b_row,
        b_not_evaluable=b_not_evaluable,
        dataset_manifest_hash=dataset_manifest_hash,
        inner_population_sessions=inner_population_sessions,
    )


# ---- beat events + conditioning features (INNER SPAN ONLY) ------------------------------


@dataclass(frozen=True)
class BeatEvent:
    name: str
    report: str
    session: date
    move: Decimal  # the sealed rule's own variable m (>= +1.5%)
    vix: float | None  # prior-session VIX close (missing -> None, disclosed)
    rv20: float | None  # SPY RV20 at t-1 (population stdev, 20 sessions)


@dataclass(frozen=True)
class TradeRow:
    entry: str
    exit: str
    name: str
    report: str
    move: str  # Decimal string
    vix: float | None
    rv20: float | None
    gross: float
    net: float
    net15: float


def _rv20(inputs: Inputs, entry: date) -> float | None:
    """Population stdev of SPY simple daily returns over the 20 sessions
    ending at t-1 (21 closes from the panel's SPY bars, calendar-pinned)."""
    cal = inputs.calendar
    prior = cal.previous_session(entry)
    if prior is None:
        return None
    j = cal.ordinal(prior)
    if j - 20 < 0:
        return None
    sessions = cal.sessions()[j - 20 : j + 1]  # 21 sessions ending at prior
    spy = inputs.panel["SPY"]
    closes: list[Decimal] = []
    for s in sessions:
        iso = s.isoformat()
        if iso not in spy:
            return None
        closes.append(Decimal(str(spy[iso]["close"])))
    rets = [float(closes[k] / closes[k - 1] - 1) for k in range(1, len(closes))]
    if len(rets) != 20:
        return None
    return statistics.pstdev(rets)


def collect_inner_beats(inputs: Inputs) -> list[BeatEvent]:
    """Every beat the sealed rule fires with entry session <= 2026-04-20
    (the registration's inner loop). The walk NEVER passes INNER_END."""
    cal = inputs.calendar
    era_start = date.fromisoformat(ERA_START)
    inner_end = date.fromisoformat(INNER_END)
    events: list[BeatEvent] = []
    for s in cal.sessions():
        if s < era_start or s > inner_end:
            continue
        result = pead_beats(inputs.panel, inputs.earnings, s, cal)
        for beat in result.beats:
            if beat.move is None:  # pragma: no cover - beats carry moves by construction
                raise Refused(f"beat without a move: {beat}")
            prior = cal.previous_session(s)
            vix = inputs.vix.get(prior.isoformat()) if prior is not None else None
            events.append(
                BeatEvent(
                    name=beat.name,
                    report=beat.report_date,
                    session=s,
                    move=beat.move,
                    vix=vix,
                    rv20=_rv20(inputs, s),
                )
            )
    return events


def _window_clean_iter002(ds: list[str], i: int, hold: int) -> bool:
    """iter002 window_clean(ds, 5, i, hold): no calendar-day hole > 10 over
    [i-5, i+hold] on the name's own bar index."""
    lo, hi = max(0, i - PEAD_CLEAN_BACK), min(len(ds) - 1, i + hold)
    for a, b in zip(ds[lo:hi], ds[lo + 1 : hi + 1]):
        if (date.fromisoformat(b) - date.fromisoformat(a)).days > HOLE_DAYS:
            return False
    return True


def trade_rows_for_hold(inputs: Inputs, events: Sequence[BeatEvent], hold: int) -> dict[int, TradeRow]:
    """The per-event trade rows at one hold. House hold filter: the name must
    carry every calendar session in (s, s+hold]; misses are dropped (the
    event key is the index into ``events``). The iter002 window_clean
    divergence count is returned via the ``disclosures`` block by the caller
    helper ``hold_disclosures``."""
    cal = inputs.calendar
    out: dict[int, TradeRow] = {}
    for key, ev in enumerate(events):
        bars = inputs.panel[ev.name]
        entry_iso = ev.session.isoformat()
        i = cal.ordinal(ev.session)
        exit_date = cal.nth_after(ev.session, hold)
        exit_iso = exit_date.isoformat()
        window = cal.sessions()[i + 1 : i + hold + 1]
        if any(w.isoformat() not in bars for w in window) or exit_iso not in bars:
            continue  # dropped: label incomplete (beyond panel or missing session)
        gross = float(
            Decimal(str(bars[exit_iso]["close"])) / Decimal(str(bars[entry_iso]["close"])) - 1
        )
        out[key] = TradeRow(
            entry=entry_iso,
            exit=exit_iso,
            name=ev.name,
            report=ev.report,
            move=str(ev.move),
            vix=ev.vix,
            rv20=ev.rv20,
            gross=gross,
            net=gross - RT_PRIMARY,
            net15=gross - RT_ROBUST,
        )
    return out


def hold_disclosures(inputs: Inputs, events: Sequence[BeatEvent], hold: int) -> dict[str, int]:
    """Completion/feature disclosure counts for one hold (no returns viewed)."""
    cal = inputs.calendar
    dropped_incomplete = 0
    windowclean_only = 0
    beyond_panel = 0
    for ev in events:
        bars = inputs.panel[ev.name]
        i = cal.ordinal(ev.session)
        exit_date = cal.nth_after(ev.session, hold)
        window = cal.sessions()[i + 1 : i + hold + 1]
        complete = all(w.isoformat() in bars for w in window) and exit_date.isoformat() in bars
        if complete:
            continue
        dropped_incomplete += 1
        if exit_date.isoformat() > max(bars):
            beyond_panel += 1
        ds = sorted(bars)
        idx = ds.index(ev.session.isoformat())
        # the iter002 exit is on the name's OWN index; a hole inside the
        # window would shift it -- count rows iter002 would have kept that
        # the house filter drops (expect 0 on this panel)
        if _window_clean_iter002(ds, idx, hold) and idx + hold < len(ds):
            windowclean_only += 1
    return {
        "n_events": len(events),
        "n_dropped_incomplete": dropped_incomplete,
        "n_dropped_beyond_panel": beyond_panel,
        "n_iter002_windowclean_would_keep": windowclean_only,
    }


# ---- strata edges (inner span only, INV-07) ----------------------------------------------


def _median(vals: list[Decimal]) -> Decimal:
    s = sorted(vals)
    return s[len(s) // 2]  # iter002 index convention


def _tercile_edges(vals: list[float]) -> tuple[float, float]:
    s = sorted(vals)
    return s[len(s) // 3], s[(2 * len(s)) // 3]


# ---- statistics (iter002 stats_of convention, on NET rows) --------------------------------


def stats_of(rows: Sequence[TradeRow]) -> dict[str, Any]:
    """iter002 stats_of over net returns: day-clustered mean/t (ddof=1),
    hit rate, plus the per-trade net mean (disclosed leg basis)."""
    if not rows:
        return {
            "n": 0,
            "days": 0,
            "hit": None,
            "mean": None,
            "t": None,
            "per_trade_net_mean": None,
            "net15_per_trade_mean": None,
        }
    by_day: dict[str, list[float]] = {}
    for r in rows:
        by_day.setdefault(r.entry, []).append(r.net)
    daily = [statistics.fmean(v) for _d, v in sorted(by_day.items())]
    mean = statistics.fmean(daily)
    sd = statistics.stdev(daily) if len(daily) > 1 else None
    t = (mean / (sd / math.sqrt(len(daily)))) if (sd is not None and sd > 0) else None
    nets = [r.net for r in rows]
    return {
        "n": len(rows),
        "days": len(daily),
        "hit": sum(1 for x in nets if x > 0) / len(nets),
        "mean": mean,
        "t": t,
        "per_trade_net_mean": statistics.fmean(nets),
        "net15_per_trade_mean": statistics.fmean(r.net15 for r in rows),
    }


# ---- the 24 cells ------------------------------------------------------------------------


@dataclass(frozen=True)
class CellSpec:
    config_id: str
    arm: str  # B | M | L | V | R
    hold: int
    stratum: str  # None-ish for B; lo/hi; mod/str/ext; lo/mid/hi


def cell_specs() -> list[CellSpec]:
    specs: list[CellSpec] = []
    for hold in HOLD_SESSIONS:
        specs.append(CellSpec(f"PD2-B-h{hold}", "B", hold, "all"))
    for hold in HOLD_SESSIONS:
        for stratum in ("lo", "hi"):
            specs.append(CellSpec(f"PD2-M-{stratum}-h{hold}", "M", hold, stratum))
    for stratum in ("mod", "str", "ext"):
        specs.append(CellSpec(f"PD2-L-{stratum}", "L", 20, stratum))
    for hold in HOLD_SESSIONS:
        for stratum in ("lo", "mid", "hi"):
            specs.append(CellSpec(f"PD2-V-{stratum}-h{hold}", "V", hold, stratum))
    for stratum in ("lo", "mid", "hi"):
        specs.append(CellSpec(f"PD2-R-{stratum}", "R", 20, stratum))
    by_id = {s.config_id: s for s in specs}
    if len(by_id) != len(specs) or set(by_id) != set(EXPECTED_CONFIG_IDS):
        raise Refused("cell spec grid does not match the frozen menu ids")
    return [by_id[c] for c in EXPECTED_CONFIG_IDS]  # menu order


def _stratum_member(spec: CellSpec, ev: BeatEvent, edges: Mapping[str, Any]) -> bool | None:
    """Does the event belong to the cell's stratum? None = feature missing
    (the event is excluded from that ARM entirely, disclosed)."""
    if spec.arm == "B":
        return True
    if spec.arm == "M":
        return ev.move < edges["m_median"] if spec.stratum == "lo" else ev.move >= edges["m_median"]
    if spec.arm == "L":
        if spec.stratum == "mod":
            return PEAD_THRESHOLD <= ev.move < L_MOD
        if spec.stratum == "str":
            return L_MOD <= ev.move < L_EXT
        return ev.move >= L_EXT
    if spec.arm == "V":
        tc = edges.get("vix_terciles")
        if ev.vix is None or tc is None:
            return None
        e1, e2 = tc
        if spec.stratum == "lo":
            return ev.vix < e1
        if spec.stratum == "mid":
            return e1 <= ev.vix < e2
        return ev.vix >= e2
    if spec.arm == "R":
        tc = edges.get("rv_terciles")
        if ev.rv20 is None or tc is None:
            return None
        e1, e2 = tc
        if spec.stratum == "lo":
            return ev.rv20 < e1
        if spec.stratum == "mid":
            return e1 <= ev.rv20 < e2
        return ev.rv20 >= e2
    raise Refused(f"unknown arm {spec.arm}")  # pragma: no cover


def compute_lattice(inputs: Inputs) -> dict[str, Any]:
    """One deterministic pass: inner beats -> features -> per-hold rows ->
    strata edges -> the 24 cells with stats, legs, verdicts. The sealed
    window is never touched (hard guards below)."""
    events = collect_inner_beats(inputs)
    if not events:
        raise Refused("the inner beat population is EMPTY -- machinery defect, not an outcome")
    n_missing_vix = sum(1 for e in events if e.vix is None)
    n_missing_rv = sum(1 for e in events if e.rv20 is None)

    edges: dict[str, Any] = {
        "m_median": _median([e.move for e in events]),
        "vix_terciles": (
            _tercile_edges([e.vix for e in events if e.vix is not None])
            if len(events) - n_missing_vix
            else None
        ),
        "rv_terciles": (
            _tercile_edges([e.rv20 for e in events if e.rv20 is not None])
            if len(events) - n_missing_rv
            else None
        ),
    }

    rows_by_hold = {
        h: trade_rows_for_hold(inputs, events, h) for h in HOLD_SESSIONS
    }
    disclosures = {str(h): hold_disclosures(inputs, events, h) for h in HOLD_SESSIONS}

    specs = cell_specs()
    stats_by_config: dict[str, dict[str, Any]] = {}
    rows_by_config: dict[str, list[TradeRow]] = {}
    for spec in specs:
        member_rows: list[TradeRow] = []
        for key, row in rows_by_hold[spec.hold].items():
            member = _stratum_member(spec, events[key], edges)
            if member is True:
                member_rows.append(row)
        rows_by_config[spec.config_id] = member_rows
        stats_by_config[spec.config_id] = stats_of(member_rows)

    # partition checks (a broken edge rule is a machinery defect, not an outcome)
    for hold in HOLD_SESSIONS:
        for arm, strata in (("M", ("lo", "hi")), ("V", ("lo", "mid", "hi"))):
            probe = CellSpec("x", arm, hold, "lo")
            got = sum(len(rows_by_config[f"PD2-{arm}-{st}-h{hold}"]) for st in strata)
            arm_rows = sum(
                1
                for k in rows_by_hold[hold]
                if _stratum_member(probe, events[k], edges) is not None
            )
            if got != arm_rows:
                raise Refused(
                    f"arm {arm} h{hold} strata do not partition the arm's usable rows"
                    f" ({got} != {arm_rows})"
                )
        ladder = sum(len(rows_by_config[f"PD2-L-{st}"]) for st in ("mod", "str", "ext"))
        if ladder != len(rows_by_config["PD2-B-h20"]):
            raise Refused("arm L bands do not partition the h20 beat rows")
        rsum = sum(len(rows_by_config[f"PD2-R-{st}"]) for st in ("lo", "mid", "hi"))
        r_rows = sum(
            1
            for k in rows_by_hold[20]
            if _stratum_member(CellSpec("x", "R", 20, "lo"), events[k], edges) is not None
        )
        if rsum != r_rows:
            raise Refused("arm R strata do not partition the arm's usable rows")

    # hard sealed-window guard: no computed row may touch the sealed window
    sealed_start = date.fromisoformat(SEALED_START)
    for hold in HOLD_SESSIONS:
        for row in rows_by_hold[hold].values():
            if date.fromisoformat(row.entry) >= sealed_start or date.fromisoformat(row.exit) >= sealed_start:
                raise Refused(
                    f"trade row {row.entry}->{row.exit} touches the sealed window"
                    " -- the inner-only guard fired"
                )

    # verdicts (the pre-declared cell bar, menu v2/v3 drift-relative)
    B_dc = float(inputs.B["B_net_day_clustered_mean"])
    B_pt = float(inputs.B["B_net_per_trade_mean"])
    for spec in specs:
        s = stats_by_config[spec.config_id]
        b_same = stats_by_config[f"PD2-B-h{spec.hold}"]
        row: dict[str, Any] = {
            "config_id": spec.config_id,
            "arm": spec.arm,
            "hold": spec.hold,
            "stratum": spec.stratum,
            "stats": s,
            "legs": None,
            "verdict": None,
        }
        if s["n"] < N_MIN or s["days"] < DAYS_MIN:
            row["verdict"] = "INSUFFICIENT_N"
            row["legs"] = {
                "n_pass": s["n"] >= N_MIN,
                "days_pass": s["days"] >= DAYS_MIN,
                "insufficient_reasons": [
                    *([] if s["n"] >= N_MIN else [f"n {s['n']} < {N_MIN}"]),
                    *([] if s["days"] >= DAYS_MIN else [f"days {s['days']} < {DAYS_MIN}"]),
                ],
            }
        else:
            mean_minus_B_primary = s["mean"] - B_dc
            mean_minus_B_pertrade = s["per_trade_net_mean"] - B_pt
            conditional = s["mean"] - b_same["mean"]
            legs = {
                "n_pass": True,
                "days_pass": True,
                "mean_minus_B_primary": mean_minus_B_primary,
                "mean_minus_B_primary_pass": mean_minus_B_primary > 0,
                "mean_minus_B_pertrade_sensitivity": mean_minus_B_pertrade,
                "mean_minus_B_pertrade_sensitivity_pass": mean_minus_B_pertrade > 0,
                "t_pass": s["t"] is not None and s["t"] >= T_BAR,
                "conditional_vs_B_same_hold": conditional,
                "conditional_pass": conditional > 0,
            }
            row["legs"] = legs
            row["verdict"] = (
                "CANDIDATE"
                if (
                    legs["mean_minus_B_primary_pass"]
                    and legs["t_pass"]
                    and legs["conditional_pass"]
                )
                else "NULL"
            )
        stats_by_config[spec.config_id] = row

    return {
        "edges": {
            "m_median": str(edges["m_median"]),
            "vix_terciles": list(edges["vix_terciles"]) if edges["vix_terciles"] else None,
            "rv_terciles": list(edges["rv_terciles"]) if edges["rv_terciles"] else None,
            "conventions": {
                "m_median": "sorted inner-span beat moves, index len//2 (iter002); lo = m < median, hi = m >= median",
                "terciles": "sorted inner-span feature values, edges at len//3 and 2*len//3; lo = v < e1, mid = e1 <= v < e2, hi = v >= e2",
                "population": "all inner-span beat events (the PD2-B population), feature-missing events excluded per arm and disclosed",
            },
        },
        "population": {
            "n_beats_inner": len(events),
            "n_missing_vix": n_missing_vix,
            "n_missing_rv20": n_missing_rv,
            "inner_population_sessions": inputs.inner_population_sessions,
            "per_hold_disclosures": disclosures,
        },
        "events": events,
        "rows_by_config": rows_by_config,
        "cells": stats_by_config,
    }


# ---- registry / stamps -------------------------------------------------------------------


def _trial_id(config_id: str) -> str:
    return f"{SCOPE_ID}-{config_id}-g{TRIAL_GENERATION}"


def _trial_artifact_path(config_id: str) -> Path:
    return TRIALS_DIR / f"{_trial_id(config_id)}.json"


def _hyperparameters(inputs: Inputs, spec: CellSpec) -> dict[str, Any]:
    strata_rule = {
        "B": "unconditioned beats (the reference stratum)",
        "M": "beat-move median split, edge = inner-span median (index len//2); lo = m < median, hi = m >= median",
        "L": "fixed bands on m: mod = [ +1.5%, +3% ), str = [ +3%, +6% ), ext = >= +6% (no fitted edge)",
        "V": "prior-session VIX close tercile, edges = inner-span terciles (len//3, 2*len//3); lo < e1 <= mid < e2 <= hi",
        "R": "SPY RV20(t-1) tercile (population stdev of the 20 simple returns ending at t-1), edges as V",
    }[spec.arm]
    return {
        "scope_id": SCOPE_ID,
        "slot_id": SLOT_ID,
        "config_id": spec.config_id,
        "model_family": MODEL_FAMILY,
        "trial_generation": TRIAL_GENERATION,
        "arm": spec.arm,
        "stratum": spec.stratum,
        "hold_sessions": spec.hold,
        "strata_rule": strata_rule,
        "lane": "card-lane (equity close-to-close), direction pead_beat (ALLOWED, unchanged)",
        "role": "CONTEXT_ONLY conditioning study; a surviving gate is a SUCCESSOR CANDIDATE only (never a mid-stream card swap)",
        "rt_primary_bp": 5,
        "rt_robust_bp": 15,
        "eval_phase": "round1-inner",
        "eval_span": [ERA_START, INNER_END],
        "sealed_window_reserved": [SEALED_START, ERA_END],
        "sealed_window_policy": "never scored, never read for tuning, never plotted in round 1; family verdicts belong to the sealed round",
        "cell_bar": {
            "mean_minus_B": "day-clustered net mean - B(pead-deep-2 window, event shape).B_net_day_clustered_mean > 0 (primary); per-trade sensitivity leg disclosed",
            "t": f"day-clustered t (ddof=1, iter002 stats_of) >= {T_BAR}",
            "n_days": f">= {DAYS_MIN}",
            "n": f">= {N_MIN}",
            "conditional": "stratum mean - PD2-B same-hold mean > 0 (day-clustered basis)",
            "verdicts": list(VERDICTS),
        },
        "B_binding": {
            "artifact": str(CALIBRATION_V3_PATH),
            "window": B_WINDOW,
            "shape": B_SHAPE,
            "B_net_day_clustered_mean": inputs.B["B_net_day_clustered_mean"],
            "B_net_per_trade_mean": inputs.B["B_net_per_trade_mean"],
            "window_is_null_NOT_EVALUABLE": inputs.b_not_evaluable,
            "note": "B read from calibration-v3.json, never recomputed; the pead-deep-2 event cell is below the v3 floor, so B(W, shape) is the standing drift gate",
        },
        "calendar_exclusions": [PHANTOM_ISO],
        "return_convention": "Decimal close(s+h)/close(s)-1, calendar-ordinal exit, house hold filter (every session in (s, s+h]); net = gross - 5bp",
        "inputs_sha256": {
            "ohlc-panel.json": inputs.panel_sha256,
            "earnings-calendar.json": inputs.earnings_sha256,
            "earnings-timing.json": inputs.timing_sha256,
            "VIX.csv": inputs.vix_sha256,
            "nyse_sessions json": inputs.calendar_sha256,
            "slots/pead-deep-2.md": inputs.slot_doc_sha256,
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
        outer_fold_id=f"campaign-2026-09/{SCOPE_ID}/inner-{ERA_START}_{INNER_END}",
        target_horizon="hold10|20|40",
        feature_set_id="ohlc-panel|earnings-calendar|VIX|SPY-RV20|v1",
        model_family=MODEL_FAMILY,
    )


def _stamp(inputs: Inputs, spec: CellSpec) -> dict[str, Any]:
    return {
        "program": "campaign-2026-09",
        "slot_id": SLOT_ID,
        "scope_id": SCOPE_ID,
        "config_id": spec.config_id,
        "trial_id": _trial_id(spec.config_id),
        "trial_generation": TRIAL_GENERATION,
        "round": 1,
        "eval_phase": "round1-inner",
        "registration_menu_sha256": inputs.menu_sha256,
        "protocol_raw_sha256": inputs.protocol_raw_sha256,
        "protocol_canonical_sha256": inputs.protocol_canonical_sha256,
        "dataset_manifest_hash": inputs.dataset_manifest_hash,
        "inputs_sha256": {
            "ohlc-panel.json": inputs.panel_sha256,
            "earnings-calendar.json": inputs.earnings_sha256,
            "earnings-timing.json": inputs.timing_sha256,
            "VIX.csv": inputs.vix_sha256,
            "nyse_sessions json": inputs.calendar_sha256,
        },
        "universe": {
            "panel_names": 37,
            "chain35": len(inputs.chain35),
            "reporters": len(inputs.reporters),
        },
        "B_binding": {
            "window": B_WINDOW,
            "shape": B_SHAPE,
            "B_net_day_clustered_mean": inputs.B["B_net_day_clustered_mean"],
            "B_net_per_trade_mean": inputs.B["B_net_per_trade_mean"],
            "window_is_null_NOT_EVALUABLE": inputs.b_not_evaluable,
        },
        "git_sha": _git_head(REPO_ROOT),
        "runner_sha256": _sha256_file(Path(__file__).resolve()),
        "generated_at": _utcnow().isoformat(),
    }


def _open_registry() -> TrialRegistry:
    CAMPAIGN_DIR.mkdir(parents=True, exist_ok=True)
    return TrialRegistry(REGISTRY_PATH)


# -- phases ---------------------------------------------------------------------------------


def phase_plan() -> int:
    inputs = load_and_bind()
    print(f"menu sha256 {inputs.menu_sha256} (sidecar-verified, v3)")
    print(
        f"protocol raw {inputs.protocol_raw_sha256[:16]}..."
        f" canonical {inputs.protocol_canonical_sha256[:16]}..."
    )
    print(f"B({B_WINDOW}, {B_SHAPE}): day-clustered {inputs.B['B_net_day_clustered_mean']:+.6f}"
          f" per-trade {inputs.B['B_net_per_trade_mean']:+.6f}"
          f" (NOT_EVALUABLE window: {inputs.b_not_evaluable})")
    cal = inputs.calendar
    print(
        f"era {ERA_START}..{ERA_END} = {cal.ordinal(date.fromisoformat(ERA_END)) - cal.ordinal(date.fromisoformat(ERA_START)) + 1} sessions;"
        f" inner walk {ERA_START}..{INNER_END} ({inputs.inner_population_sessions} sessions);"
        f" sealed {SEALED_START}..{ERA_END} NEVER touched in round 1"
    )
    print(f"cells: {len(cell_specs())} configs, scope {SCOPE_ID} (cap 32)")
    print(f"registry db: {REGISTRY_PATH}")
    print(f"artifacts dir: {PD2_DIR}")
    return 0


def phase_register() -> int:
    inputs = load_and_bind()
    specs = cell_specs()
    scope = _scope(inputs)
    registry = _open_registry()
    try:
        existing = [s for s in specs if registry.is_registered(_trial_id(s.config_id))]
        if existing:
            raise Refused(
                f"registration is one-shot: {[ _trial_id(s.config_id) for s in existing ]} already registered"
            )
        for spec in specs:
            hyper = _hyperparameters(inputs, spec)
            record = TrialRecord(
                trial_id=_trial_id(spec.config_id),
                created_at=_utcnow(),
                hypothesis=inputs.slot["hypothesis"],
                git_sha=_git_head(REPO_ROOT),
                config_hash=_config_hash(hyper),
                dataset_manifest_hash=inputs.dataset_manifest_hash,
                train_window=None,
                validation_window=(date.fromisoformat(ERA_START), date.fromisoformat(INNER_END)),
                test_window=None,  # the sealed window is reserved, NOT run in round 1
                hyperparameters=hyper,
                scope_key=scope.scope_key(),
            )
            registry.register(record, scope)
            print(
                f"registered {_trial_id(spec.config_id)} arm={spec.arm}"
                f" stratum={spec.stratum} hold={spec.hold}"
            )
    finally:
        registry.close()
    print(
        f"registry: {REGISTRY_PATH} (scope trials={len(specs)}, cap=32);"
        " NO outcome has been computed or viewed"
    )
    return 0


def phase_execute() -> int:
    inputs = load_and_bind()
    specs = cell_specs()
    PD2_DIR.mkdir(parents=True, exist_ok=True)
    TRIALS_DIR.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise Refused("another pead-deep-2 execution holds the lock -- one run at a time") from None

        # pre-flight: every trial REGISTERED, no artifact yet (one-shot)
        registry = _open_registry()
        try:
            for spec in specs:
                trial_id = _trial_id(spec.config_id)
                if _trial_artifact_path(spec.config_id).exists():
                    raise Refused(
                        f"{_trial_artifact_path(spec.config_id)} already exists"
                        " -- executions are one-shot per trial"
                    )
                status = registry.status(trial_id)
                if status != "REGISTERED":
                    raise Refused(f"{trial_id} is {status}, not REGISTERED -- refusing to re-run")
        finally:
            registry.close()

        # ONE deterministic pass computes every cell from the pinned inputs
        # (a Refused here leaves every trial REGISTERED, no outcome);
        # each trial then transitions RUNNING -> COMPLETED with its own
        # artifact (one scored run per cell)
        lattice = compute_lattice(inputs)
        registry = _open_registry()
        try:
            for spec in specs:
                trial_id = _trial_id(spec.config_id)
                cell = lattice["cells"][spec.config_id]
                rows = lattice["rows_by_config"][spec.config_id]
                # machinery self-check: an EMPTY stratum with non-empty siblings
                # was already refused by the partition checks; a zero-row B cell
                # is a defect, full stop
                if spec.arm == "B" and cell["stats"]["n"] == 0:
                    registry.mark_running(
                        trial_id,
                        git_sha=_git_head(REPO_ROOT),
                        config_hash=_config_hash(_hyperparameters(inputs, spec)),
                        dataset_manifest_hash=inputs.dataset_manifest_hash,
                        at=_utcnow(),
                    )
                    registry.fail(
                        trial_id,
                        f"{trial_id}: the unconditioned inner beat population executed EMPTY"
                        " -- machinery defect, not an outcome",
                        at=_utcnow(),
                    )
                    raise Refused(
                        f"{trial_id}: unconditioned beat population is empty (machinery defect;"
                        " trial FAILED, no artifact written)"
                    )
                registry.mark_running(
                    trial_id,
                    git_sha=_git_head(REPO_ROOT),
                    config_hash=_config_hash(_hyperparameters(inputs, spec)),
                    dataset_manifest_hash=inputs.dataset_manifest_hash,
                    at=_utcnow(),
                )
                body = {
                    "stamp": _stamp(inputs, spec),
                    "payload": {
                        "config_id": spec.config_id,
                        "arm": spec.arm,
                        "stratum": spec.stratum,
                        "hold": spec.hold,
                        "edges": lattice["edges"],
                        "population": lattice["population"],
                        "cell": cell,
                        "trades": [row.__dict__ for row in rows],
                    },
                }
                artifact = _trial_artifact_path(spec.config_id)
                artifact.write_text(
                    json.dumps(body, indent=2, sort_keys=True) + "\n", encoding="utf-8"
                )
                registry.complete(trial_id, metrics_uri=str(artifact), outcome_at=_utcnow())
                s = cell["stats"]
                print(
                    f"{trial_id}: COMPLETED n={s['n']} days={s['days']}"
                    f" mean={s['mean'] if s['mean'] is None else format(s['mean'], '+.4%')}"
                    f" verdict={cell['verdict']}"
                )
        finally:
            registry.close()
    finally:
        os.close(lock_fd)
    print(f"artifacts: {TRIALS_DIR} ({len(specs)} trials)")
    return 0


def _read_artifact(inputs: Inputs, spec: CellSpec) -> Mapping[str, Any]:
    trial_id = _trial_id(spec.config_id)
    artifact = _trial_artifact_path(spec.config_id)
    body = json.loads(artifact.read_text(encoding="utf-8"))
    stamp = body.get("stamp", {})
    if stamp.get("trial_id") != trial_id:
        raise Refused(
            f"{artifact} carries trial_id {stamp.get('trial_id')!r}, expected {trial_id!r}"
            " -- only the EXECUTED artifact is summary evidence"
        )
    if stamp.get("registration_menu_sha256") != inputs.menu_sha256:
        raise Refused(f"{artifact} was executed against a different menu hash")
    if stamp.get("dataset_manifest_hash") != inputs.dataset_manifest_hash:
        raise Refused(f"{artifact} was executed against different inputs")
    return body


def phase_summarize() -> int:
    inputs = load_and_bind()
    if SUMMARY_PATH.exists():
        raise Refused(f"{SUMMARY_PATH} already exists -- the round-1 summary is one-shot")
    specs = cell_specs()
    rows = []
    verdict_counts = {v: 0 for v in VERDICTS}
    for spec in specs:
        body = _read_artifact(inputs, spec)
        cell = body["payload"]["cell"]
        verdict_counts[cell["verdict"]] += 1
        rows.append(
            {
                "config_id": spec.config_id,
                "arm": spec.arm,
                "stratum": spec.stratum,
                "hold": spec.hold,
                "n": cell["stats"]["n"],
                "days": cell["stats"]["days"],
                "hit": cell["stats"]["hit"],
                "mean_day_clustered": cell["stats"]["mean"],
                "t": cell["stats"]["t"],
                "per_trade_net_mean": cell["stats"]["per_trade_net_mean"],
                "verdict": cell["verdict"],
                "legs": cell["legs"],
            }
        )
    # descriptive best (NOT an adoption): rank the sufficient cells by the
    # study's own discriminant -- the conditional margin vs PD2-B same hold
    best = None
    sufficient = [r for r in rows if r["verdict"] != "INSUFFICIENT_N"]
    if sufficient:
        ranked = sorted(
            sufficient,
            key=lambda r: (
                -(r["legs"]["conditional_vs_B_same_hold"]),
                -(r["t"] if r["t"] is not None else -math.inf),
                r["config_id"],
            ),
        )
        best = ranked[0]

    summary = {
        "stamp": {
            **_stamp(inputs, specs[0]),
            "trial_id": None,
            "config_id": None,
            "artifact_trial_ids": [_trial_id(s.config_id) for s in specs],
            "round": 1,
            "eval_phase": "round1-inner",
            "menu_hypothesis": inputs.slot["hypothesis"],
            "acceptance_criteria": inputs.slot["acceptance_criteria"],
            "verdict_vocabulary": inputs.slot["verdict_vocabulary"],
            "B_binding": {
                "window": B_WINDOW,
                "shape": B_SHAPE,
                "B_net_day_clustered_mean": inputs.B["B_net_day_clustered_mean"],
                "B_net_per_trade_mean": inputs.B["B_net_per_trade_mean"],
                "window_is_null_NOT_EVALUABLE": inputs.b_not_evaluable,
                "primary_basis": "day-clustered net mean vs B_net_day_clustered_mean (like-for-like); per-trade sensitivity disclosed per cell",
            },
            "bands": {
                "t_min": T_BAR,
                "n_min": N_MIN,
                "days_min": DAYS_MIN,
                "rt_primary": RT_PRIMARY,
                "rt_robust": RT_ROBUST,
                "ladder_bands": ["[+1.5%, +3%)", "[+3%, +6%)", ">= +6%"],
            },
        },
        "verdict_counts": verdict_counts,
        "cells": rows,
        "best_descriptive": (
            None
            if best is None
            else {
                "config_id": best["config_id"],
                "metric": "conditional_vs_B_same_hold (day-clustered basis)",
                "value": best["legs"]["conditional_vs_B_same_hold"],
                "rank_rule": "max conditional margin among non-INSUFFICIENT_N cells; ties by t then config id; DESCRIPTIVE ONLY, no adoption",
                "verdict": best["verdict"],
                "t": best["t"],
                "n": best["n"],
                "days": best["days"],
            }
        ),
        "forward_timing_arm": timing_arm_note(inputs),
        "withdrawn_and_gated": {
            "pead-deep-2": [
                "per-name IV conditioning: EXCLUDED at registration (IVHIST-001 low-fidelity gate) -- never a config",
                "options-expression arm: dropped at registration (long-dated bars capture in flight, ETA ~2026-09-27) -- never a config",
                "PD2-T-bmo/amc/unknown: forward-only strata, 0 backtest cells, standing verdict INSUFFICIENT_COVERAGE",
            ],
            "campaign_level_other_slots": [
                "vrp-cond scope O (c09-vrp-o): WITHDRAWN without running (option-bar capture gate) -- owned by the vrp-cond executor",
                "exit-grid-2 scope C (c09-eg2-c): DATA-GATED-NOT-RUN (capture + post-M0 short-leg machinery) -- owned by the exit-grid-2 executor",
            ],
            "pead-deep-2_configs_withdrawn": 0,
        },
        "notes": [
            "ROUND 1 = INNER FOLDS ONLY (entry sessions 2024-09-05..2026-04-20); the sealed window",
            "2026-06-25..2026-09-23 was never scored, read for tuning, or plotted; the runner hard-asserts",
            "no computed trade row carries an entry or exit session >= 2026-06-25.",
            "Family verdicts (STRATUM-CONFIRMED / FAMILY-NULL / INSUFFICIENT_N at the family level) and the",
            "terminal PROMOTE-AS-GATE / CLOSE belong to the sealed round; round 1 verdicts are per-cell only.",
            "PD2-B cells carry conditional == 0 by construction and cannot be CANDIDATE (mechanical",
            "application of the pre-declared bar; their role is the same-hold reference).",
            "One scored run per cell; no re-gridding; bands/terciles/holds/window frozen.",
        ],
    }
    PD2_DIR.mkdir(parents=True, exist_ok=True)
    SUMMARY_PATH.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"round-1 summary stamped: {SUMMARY_PATH}")
    print(f"verdict counts: {verdict_counts}")
    if best is not None:
        print(
            f"best (descriptive): {best['config_id']}"
            f" conditional={best['legs']['conditional_vs_B_same_hold']:+.4%}"
            f" verdict={best['verdict']}"
        )
    else:
        print("best (descriptive): none (every cell INSUFFICIENT_N)")
    return 0


def timing_arm_note(inputs: Inputs) -> dict[str, Any]:
    """Forward-only timing arm: count CURRENT in-universe coverage (no
    backtest cells, no historical coverage by construction)."""
    counts = {"bmo": 0, "amc": 0, "unknown": 0}
    in_universe = 0
    for name, reports in sorted(inputs.timing.items()):
        if name not in inputs.chain35 or name not in inputs.panel:
            continue  # e.g. JNJ: timed but not in the calendar/panel universe
        for report_date, rec in sorted(reports.items()):
            in_universe += 1
            timing = str(rec.get("timing", "unknown"))
            counts[timing if timing in counts else "unknown"] += 1
    earliest = min(
        (
            report_date
            for name, reports in inputs.timing.items()
            if name in inputs.chain35 and name in inputs.panel
            for report_date in reports
        ),
        default=None,
    )
    return {
        "strata": ["PD2-T-bmo", "PD2-T-amc", "PD2-T-unknown"],
        "backtest_cells": 0,
        "standing_verdict": "INSUFFICIENT_COVERAGE",
        "rule": "no verdict until BOTH PD2-T-bmo and PD2-T-amc hold >= 12 valid timed cards (labels attach to sealed cards from 2026-10-01 on)",
        "in_universe_timed_reports": in_universe,
        "counts": counts,
        "earliest_timed_report_date": earliest,
        "historical_timing_coverage": "zero (verified at registration; every timing row is a next report)",
    }


def phase_timing() -> int:
    inputs = load_and_bind()
    note = timing_arm_note(inputs)
    print(json.dumps(note, indent=2, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--register",
        action="store_true",
        help="INV-13: write the 24 trial rows (REGISTERED, no outcome) to the slot registry",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="run the 24 cells one-shot over the INNER span only (REGISTERED -> RUNNING -> COMPLETED + artifact)",
    )
    parser.add_argument(
        "--summarize",
        action="store_true",
        help="read ONLY the executed artifacts and stamp round1-summary.json",
    )
    parser.add_argument(
        "--timing",
        action="store_true",
        help="read-only: the forward-only timing arm coverage note",
    )
    parser.add_argument("--plan", action="store_true", help="read-only: print the bound geometry")
    args = parser.parse_args(argv)
    try:
        if args.register:
            return phase_register()
        if args.execute:
            return phase_execute()
        if args.summarize:
            return phase_summarize()
        if args.timing:
            return phase_timing()
        if args.plan:
            return phase_plan()
    except Refused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
