#!/usr/bin/env python3
"""campaign-2026-09 TERM-GATE runner (scope ``c09-term``, menu order 3) — ROUND 1, INNER FOLDS ONLY.

VIX term-structure context gate on the survivor card rules, executed exactly
as the sealed menu entry ``slots[3]`` of
``docs/theory/campaign-2026-09-registration.json`` (v3, sha256 sidecar-pinned)
and ``docs/theory/campaign-2026-09/slots/term-gate.md`` (sha256-pinned in the
menu's dataset_pinning) specify. The menu is the registration; this runner
only BINDS to it and refuses on any drift before anything runs.

ROUND 1 SCOPE (the workflow's inner-fold mandate): the sealed windows named
in the menu — XSMOM entries 2024-10-01..2026-08-03, PEAD entries
2024-10-16..2026-08-06, i.e. the card-era window 2024-10-01..2026-08-28 —
are NEVER scored, never read for tuning, never plotted by this runner. What
round 1 executes is exactly the slot's declared inner fold:

* T1 (card-timing gates), XSMOM leg: the 22 tuning cards with entries
  2022-11-01..2024-08-01 (the only tuning-era selection the registration
  permits: ranking the six gate specs on these cards, recorded before any
  sealed-era run). The 2024-09-03 FOM fires but sits inside the purge gap
  (last tuning signal 2024-08-01 + 20 + 5 = 2024-09-06) and is neither
  tuning nor sealed — dropped, counted, disclosed.
* T1, PEAD leg: ZERO tuning-era cards exist (earnings-calendar era starts
  2024-09-05; verified) — the registration itself declares PEAD thresholds
  fit-free; the inner verdict is NOT_EVALUABLE by registration design, never
  a defect.
* T2 (exit-grid regime interaction, DESCRIPTIVE-ONLY): the frozen
  XSMOM-EXITGRID grid re-derived with a regime column, restricted to the
  HOLDOUT era (signal dates <= 2024-09-03 — the same inner boundary the
  slot declares). The frozen grid's "full" era (signal dates > 2024-09-03)
  overlaps this slot's sealed window and is NOT computed here; it accrues to
  the sealed round. Copy-faithfulness is anchored on the PUBLISHED holdout
  table (XSMOM-EXITGRID.md), not the full-era anchor.

Gate machinery (slot section 3, byte-bound): gate variable at decision
session t uses ONLY index closes of session t-1; trailing statistics over
the 252 index sessions [t-252, t-1] inclusive (the assembly-fixed bracket);
joins on ISO index dates present in the CSVs (the phantom 2025-01-09 has no
CBOE row, so date-keyed joins sidestep it). Variables: r93 =
VIX9D/VIX3M; inc = VIX/VIX3M - 1 (the incumbent vix_term); bs =
VIX3M/VIX1Y - 1. Gate OFF = stressed side = value(t-1) >= threshold;
threshold styles SIGN (fit-free r93 >= 1.0 / inc >= 0 / bs >= 0) and Q60
(value >= its own trailing-252 60th percentile, linear interpolation).
T2's regime split is r93(t-1) >= its trailing-252 median -> STRESSED.

Accounting (slot section 3): the card-lane accounting of the published
baselines — $2,500/leg, entry close[t], exit close[t+20] (Decimal closes,
house hold filter: the name must carry every session in (t, t+20];
beyond-panel holds dropped and counted), 5bp RT primary / 15bp robustness,
day-clustered t (ddof=1, cluster = entry date). T2 uses the frozen grid's
own machinery verbatim (hold-60 baseline, worst-case fill simulator, 5bp
RT, conservative t = min(naive, day-clustered)).

Drift baseline B (menu v2/v3 rules.null_baseline): term-gate's absolute-mean
leg reads ON mean MINUS B(declared sealed window, matching book shape) —
B(card-era, xsmom) for XSMOM legs, B(card-era, event) for PEAD legs. Those
B values are READ from the stamped calibration artifact
``artifacts/campaign-2026-09/tnull/calibration-v3.json`` (never recomputed)
and only bind at the sealed round; the inner-fold ranking metric is the
ON-OFF spread, which is drift-immune by construction. The card-era window
is EVALUABLE in the v3 stamp (the NOT_EVALUABLE windows are pead-deep-2 and
vrp-cond), so the sealed round reads the normal B-excess, not the
B-alone gate.

INV-13 (registration precedes outcome): ``--register`` writes all 24 trial
rows (REGISTERED, no outcome) to the slot registry sqlite BEFORE any
outcome exists; ``--execute`` is one-shot per trial (REGISTERED ->
RUNNING -> COMPLETED with the artifact as metrics_uri); ``--rank`` reads
ONLY the executed artifacts and stamps the mandated six-spec tuning-era
ranking. Verdicts use the slot's pre-declared vocabulary only; where the
registered verdict is by construction sealed-window-only (the T1 cell bar
is "Evaluated ONLY on the sealed window", slot section 6), round 1 stamps
NO verdict for that cell and says so — the inner stats exist to rank the
gate specs, nothing else. Nothing is adopted, nothing seals a card, the
RESEARCH-LEDGER is not touched.

Inner-fold ranking statistic (recorded convention): the ON-OFF per-trade
net mean spread (5bp RT) over the 22 tuning XSMOM cards — the same
statistic the family beat-or-withdraw mandate compares at the sealed
round; ties -> sparser gate (SIGN before Q60), then config id
lexicographic.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib.util
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

# Data lives in the MAIN checkout; the runner + registration live in the
# execution worktree. Both are pinned by sha256 against the menu below.
MAIN_ROOT = Path("/home/alexk/documents/tree_options")
REGISTRATION_PATH = REPO_ROOT / "docs" / "theory" / "campaign-2026-09-registration.json"
REGISTRATION_SIDECAR = Path(str(REGISTRATION_PATH) + ".sha256")
SLOT_DOC_PATH = REPO_ROOT / "docs" / "theory" / "campaign-2026-09" / "slots" / "term-gate.md"
PROTOCOL_PATH = REPO_ROOT / "research_protocol.yaml"
PANEL_PATH = MAIN_ROOT / "artifacts" / "paper-trades" / "ohlc-panel.json"
EARNINGS_PATH = MAIN_ROOT / "artifacts" / "paper-trades" / "earnings-calendar.json"
CALENDAR_PATH = MAIN_ROOT / "data" / "calendar" / "nyse_sessions_2018_01_02_2026_12_31.json"
INDICES_DIR = MAIN_ROOT / "artifacts" / "desk-store" / "indices"
EXITGRID_SCRIPT_PATH = MAIN_ROOT / "artifacts" / "paper-trades" / "xsmom_exitgrid.py"
EXITGRID_DOC_PATH = MAIN_ROOT / "artifacts" / "paper-trades" / "XSMOM-EXITGRID.md"
CALIBRATION_V3_PATH = MAIN_ROOT / "artifacts" / "campaign-2026-09" / "tnull" / "calibration-v3.json"

CAMPAIGN_DIR = MAIN_ROOT / "artifacts" / "campaign-2026-09"
REGISTRY_PATH = CAMPAIGN_DIR / "term-gate.db"
TERM_DIR = CAMPAIGN_DIR / "term-gate"
TRIALS_DIR = TERM_DIR / "trials"
RANKING_PATH = TERM_DIR / "inner-ranking.json"
LOCK_PATH = TERM_DIR / "execute.lock"

SCOPE_ID = "c09-term"
SLOT_ID = "term-gate"
MODEL_FAMILY = "term-gate/1"
TRIAL_GENERATION = 1
ROUND_LABEL = "round1-inner"

# The pinned geometry (menu fold_mapping + slot doc sections 3-5).
PHANTOM_ISO = "2025-01-09"  # ledger ruling 2026-09-23: not a session
HOLD_SESSIONS = 20
RT_PRIMARY = 0.0005  # 5bp round trip, primary
RT_ROBUST = 0.0015  # 15bp round trip, robustness disclosure
TRAILING_SESSIONS = 252  # [t-252, t-1] inclusive (assembly fix), = min_train
Q60_PCT = 60  # trailing percentile for the Q60 threshold style
INNER_TUNING_END = "2024-09-03"  # inner (tuning) sessions <= this date
LAST_TUNING_SIGNAL = "2024-08-01"  # the 22nd tuning card
FIRST_TUNING_SIGNAL = "2022-11-01"  # the 1st tuning card
N_TUNING_CARDS = 22
SEALED_START = "2024-10-01"  # declared sealed boundary (NEVER scored in round 1)
SEALED_END = "2026-08-28"  # the card-era window the B baseline is stamped on
EXITGRID_HOLD = 60
T2_MIN_DATES = 100  # T2 power floor: any cell < 100 signal dates = NOT_EVALUABLE
T2_FLIP_T = 2.0  # DESCRIPTIVE-ONLY:REGIME-SIGN-FLIP bar (conservative t in STRESSED)
INDEX_FILES = ("VIX.csv", "VIX1Y.csv", "VIX3M.csv", "VIX9D.csv")
# The published XSMOM-EXITGRID holdout table this runner must reproduce
# (copy-faithfulness anchor for the T2 re-derivation; pooled over regimes).
EXITGRID_ANCHOR = {
    "60-skip5-tercile": {
        "hold60_n": 8009, "hold60_days": 683,
        "hold1": -0.05078, "tp100c": -0.03996, "oco100_150": -0.05174,
    },
    "252-skip21-top3": {
        "hold60_n": 1425, "hold60_days": 475,
        "hold1": -0.10768, "tp100c": -0.09128, "oco100_150": -0.10804,
    },
}
ANCHOR_TOL = 6e-4  # published at 1e-5 precision; absorbs nothing larger

# The 24 registered cells (menu config_ids + slot doc section 5 tables).
T1_CELLS: tuple[dict[str, str], ...] = (
    {"id": "T1-01", "gate": "r93", "style": "Q60", "rule": "xsmom_top3", "role": "new-quantity (PRIMARY)"},
    {"id": "T1-02", "gate": "r93", "style": "Q60", "rule": "pead_beat", "role": "new-quantity (PRIMARY)"},
    {"id": "T1-03", "gate": "r93", "style": "SIGN", "rule": "xsmom_top3", "role": "new-quantity"},
    {"id": "T1-04", "gate": "r93", "style": "SIGN", "rule": "pead_beat", "role": "new-quantity"},
    {"id": "T1-05", "gate": "inc", "style": "Q60", "rule": "xsmom_top3", "role": "incumbent"},
    {"id": "T1-06", "gate": "inc", "style": "Q60", "rule": "pead_beat", "role": "incumbent"},
    {"id": "T1-07", "gate": "inc", "style": "SIGN", "rule": "xsmom_top3", "role": "incumbent"},
    {"id": "T1-08", "gate": "inc", "style": "SIGN", "rule": "pead_beat", "role": "incumbent"},
    {"id": "T1-09", "gate": "bs", "style": "Q60", "rule": "xsmom_top3", "role": "new-quantity"},
    {"id": "T1-10", "gate": "bs", "style": "Q60", "rule": "pead_beat", "role": "new-quantity"},
    {"id": "T1-11", "gate": "bs", "style": "SIGN", "rule": "xsmom_top3", "role": "new-quantity"},
    {"id": "T1-12", "gate": "bs", "style": "SIGN", "rule": "pead_beat", "role": "new-quantity"},
)
T2_CELLS: tuple[dict[str, str], ...] = (
    {"id": "T2-01", "construction": "60-skip5-tercile", "variant": "hold1", "regime": "CALM"},
    {"id": "T2-02", "construction": "60-skip5-tercile", "variant": "hold1", "regime": "STRESSED"},
    {"id": "T2-03", "construction": "60-skip5-tercile", "variant": "tp100c", "regime": "CALM"},
    {"id": "T2-04", "construction": "60-skip5-tercile", "variant": "tp100c", "regime": "STRESSED"},
    {"id": "T2-05", "construction": "60-skip5-tercile", "variant": "oco100_150", "regime": "CALM"},
    {"id": "T2-06", "construction": "60-skip5-tercile", "variant": "oco100_150", "regime": "STRESSED"},
    {"id": "T2-07", "construction": "252-skip21-top3", "variant": "hold1", "regime": "CALM"},
    {"id": "T2-08", "construction": "252-skip21-top3", "variant": "hold1", "regime": "STRESSED"},
    {"id": "T2-09", "construction": "252-skip21-top3", "variant": "tp100c", "regime": "CALM"},
    {"id": "T2-10", "construction": "252-skip21-top3", "variant": "tp100c", "regime": "STRESSED"},
    {"id": "T2-11", "construction": "252-skip21-top3", "variant": "oco100_150", "regime": "CALM"},
    {"id": "T2-12", "construction": "252-skip21-top3", "variant": "oco100_150", "regime": "STRESSED"},
)
ALL_CELLS = tuple({**c, "arm": "T1"} for c in T1_CELLS) + tuple(
    {**c, "arm": "T2"} for c in T2_CELLS
)
CONFIG_IDS = tuple(c["id"] for c in ALL_CELLS)


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


class SealedCalendar:
    """The sealed NYSE calendar minus the 2025-01-09 phantom session
    (ClosureCorrectedCalendar idiom; the sealed file stays byte-identical)."""

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

        i = bisect.bisect_left(self._sessions, d) - 1
        return self._sessions[i] if i >= 0 else None

    def first_session_after(self, d: date) -> date | None:
        import bisect

        i = bisect.bisect_right(self._sessions, d)
        return self._sessions[i] if i < len(self._sessions) else None

    def is_first_session_of_month(self, d: date) -> bool:
        if not self.is_session(d):
            return False
        prev = self.previous_session(d)
        return prev is None or (prev.year, prev.month) != (d.year, d.month)


@dataclass(frozen=True)
class GateSeries:
    """The three gate variables on the index-session grid (all four CSVs
    present; joins on ISO index dates, per the slot doc)."""

    sessions: tuple[date, ...]  # index sessions (calendar sessions w/ all 4 rows)
    ordinals: dict[date, int]
    values: dict[str, dict[date, float]]  # var -> session -> value
    missing_index_sessions: tuple[date, ...]  # calendar sessions w/o a full row set


def _load_index_closes(path: Path) -> dict[str, float]:
    closes: dict[str, float] = {}
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0].strip().lower() != "date,open,high,low,close":
        raise Refused(f"{path.name}: unexpected header {lines[0]!r}")
    for line in lines[1:]:
        parts = line.split(",")
        if len(parts) < 5 or not parts[4].strip():
            continue
        closes[parts[0]] = float(Decimal(parts[4].strip()))
    if not closes:
        raise Refused(f"{path.name}: no close rows")
    return closes


def _quantile_p60(values: Sequence[float]) -> float:
    """60th percentile, linear interpolation (statistics 'inclusive' =
    numpy 'linear'); declared convention for the Q60 threshold style."""
    return statistics.quantiles(values, n=10, method="inclusive")[5]


# ---- sealed inputs -----------------------------------------------------------------------


@dataclass(frozen=True)
class Inputs:
    menu: Mapping[str, Any]
    menu_sha256: str
    slot: Mapping[str, Any]
    protocol_raw_sha256: str
    protocol_canonical_sha256: str
    panel: Mapping[str, Mapping[str, Mapping[str, Any]]]
    panel_sha256: str
    earnings: Mapping[str, Sequence[str]]
    earnings_sha256: str
    calendar: SealedCalendar
    calendar_sha256: str
    slot_doc_sha256: str
    indices_sha256: dict[str, str]
    gates: GateSeries
    calibration_sha256: str
    calibration: Mapping[str, Any]
    B: dict[str, dict[str, Any]]  # shape -> {B, se} for the card-era window
    tuning_cards: tuple[tuple[str, tuple[str, ...]], ...]  # (iso, top3) x 22
    purge_gap_foms: tuple[str, ...]  # fired FOMs inside the purge gap (dropped)
    pead_tuning_events: int  # must be 0 (verified)
    dataset_manifest_hash: str


def _menu_slot(menu: Mapping[str, Any]) -> Mapping[str, Any]:
    slot = next((s for s in menu["slots"] if s.get("slot_id") == SLOT_ID), None)
    if slot is None or slot.get("order") != 3 or slot.get("family") != "TERM-GATE":
        raise Refused("the menu's order-3 TERM-GATE slot is missing or malformed")
    return slot


def _verify_slot_grid(slot: Mapping[str, Any]) -> None:
    scopes = {sc["scope_id"]: sc for sc in slot["scope_ids"]}
    if list(scopes) != [SCOPE_ID] or scopes[SCOPE_ID]["config_count"] != 24:
        raise Refused(f"menu scope arithmetic is not exactly {SCOPE_ID} with 24 configs")
    if list(slot["config_ids"]) != list(CONFIG_IDS):
        raise Refused("menu term-gate config_ids are not T1-01..T1-12, T2-01..T2-12 in order")
    vocab = list(slot["verdict_vocabulary"])
    expected = [
        "PASS-GATE",
        "INCUMBENT-CONFIRMED",
        "WITHDRAW",
        "NOT_EVALUABLE",
        "DESCRIPTIVE-ONLY:REGIME-SIGN-FLIP",
        "DESCRIPTIVE-ONLY:NO-REGIME-SIGNAL",
        "DESCRIPTIVE-ONLY:NOT_EVALUABLE",
    ]
    if vocab != expected:
        raise Refused(f"term-gate verdict vocabulary drifted: {vocab}")
    gates = list(slot.get("data_gates") or [])
    if gates and "none for the slot proper" not in gates[0]:
        raise Refused("term-gate acquired a data gate — the menu marks this slot ungated")


def _load_calibration(menu_sha256: str) -> tuple[Mapping[str, Any], str]:
    if not CALIBRATION_V3_PATH.exists():
        raise Refused(f"{CALIBRATION_V3_PATH} is missing — the v3 null calibration gates every family run")
    body = json.loads(CALIBRATION_V3_PATH.read_text(encoding="utf-8"))
    stamp = body.get("stamp", {})
    if body.get("verdict", {}).get("slot") != "CALIBRATED":
        raise Refused("the v3 null is not CALIBRATED — family scoring stays frozen")
    if stamp.get("registration_menu_sha256") != menu_sha256:
        raise Refused("the v3 calibration does not bind this menu sha — re-stamp required")
    if stamp.get("menu_version") != 3:
        raise Refused("the calibration is not a v3 stamp")
    return body, _sha256_file(CALIBRATION_V3_PATH)


def _bind_B(calibration: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Read B(card-era, shape) + day-clustered se from the stamped artifact.
    Never recomputed. The card-era window (term-gate's declared evaluation
    window) must not be in the v3 NOT_EVALUABLE list — else the sealed round
    is governed by B alone (standing drift gate)."""
    ne = {
        (c["window"], c["shape"])
        for c in calibration.get("not_evaluable_cells", [])
    }
    b_table = calibration["baseline"]["B"]
    out: dict[str, dict[str, Any]] = {}
    for shape in ("xsmom", "event"):
        cell = b_table[shape]["card-era"]
        out[shape] = {
            "B_net_per_trade_mean": cell["B_net_per_trade_mean"],
            "per_trade_mean_sd_day_clustered": cell["per_trade_mean_sd_day_clustered"],
            "window": "card-era 2024-10-01..2026-08-28",
            "window_not_evaluable_in_v3_stamp": ("card-era", shape) in ne,
        }
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
        raise Refused("the sealed menu is not the v3 amendment this runner binds")
    slot = _menu_slot(menu)
    _verify_slot_grid(slot)

    # protocol: raw bytes must equal the menu pin; canonical hash re-stamped (INV-14)
    from tree_options.protocol.loader import default_protocol, protocol_hash

    protocol_raw_sha256 = _sha256_file(PROTOCOL_PATH)
    if menu["protocol_hash"] != protocol_raw_sha256:
        raise Refused(
            "research_protocol.yaml raw sha256 does not match the menu's protocol_hash"
            " -- a protocol change requires a NEW registration"
        )
    protocol_canonical_sha256 = protocol_hash(default_protocol())

    # data: pinned inputs (panel, earnings, calendar, the four index CSVs, the slot doc)
    pinning = menu["dataset_pinning"]
    checks = {
        "artifacts/paper-trades/ohlc-panel.json": PANEL_PATH,
        "artifacts/paper-trades/earnings-calendar.json": EARNINGS_PATH,
        "data/calendar/nyse_sessions_2018_01_02_2026_12_31.json": CALENDAR_PATH,
        "docs/theory/campaign-2026-09/slots/term-gate.md": SLOT_DOC_PATH,
        **{f"artifacts/desk-store/indices/{f}": INDICES_DIR / f for f in INDEX_FILES},
    }
    got = {label: _sha256_file(path) for label, path in checks.items()}
    for label, digest in got.items():
        want = pinning.get(label)
        if want is None or want != digest:
            raise Refused(f"{label}: sha256 {digest} != the menu's pinned {want} -- swapped inputs refuse")

    panel = json.loads(PANEL_PATH.read_text(encoding="utf-8"))
    earnings = json.loads(EARNINGS_PATH.read_text(encoding="utf-8"))
    calendar_json = json.loads(CALENDAR_PATH.read_text(encoding="utf-8"))
    calendar = SealedCalendar(calendar_json["sessions"])
    if calendar.removed != (PHANTOM_ISO,):
        raise Refused(f"the phantom session was not removed: {calendar.removed}")

    # universes, pinned to the panel and cross-checked against the desk config
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

    # the gate series on the index-session grid
    closes = {f[:-4]: _load_index_closes(INDICES_DIR / f) for f in INDEX_FILES}
    sessions = calendar.sessions()
    gate_sessions = tuple(
        s for s in sessions if all(s.isoformat() in closes[k] for k in closes)
    )
    missing = tuple(s for s in sessions if s not in set(gate_sessions))
    values: dict[str, dict[date, float]] = {
        "r93": {
            s: closes["VIX9D"][s.isoformat()] / closes["VIX3M"][s.isoformat()]
            for s in gate_sessions
        },
        "inc": {
            s: closes["VIX"][s.isoformat()] / closes["VIX3M"][s.isoformat()] - 1.0
            for s in gate_sessions
        },
        "bs": {
            s: closes["VIX3M"][s.isoformat()] / closes["VIX1Y"][s.isoformat()] - 1.0
            for s in gate_sessions
        },
    }
    gates = GateSeries(
        sessions=gate_sessions,
        ordinals={s: i for i, s in enumerate(gate_sessions)},
        values=values,
        missing_index_sessions=missing,
    )

    # the tuning-era card set (signal-set facts only; NO returns computed here)
    from tree_options.desk.signals import pead_beats, xsmom_top3

    inner_end = date.fromisoformat(INNER_TUNING_END)
    fired: list[tuple[str, tuple[str, ...]]] = []
    purge_gap: list[str] = []
    for s in sessions:
        if s > inner_end or not calendar.is_first_session_of_month(s):
            continue
        res = xsmom_top3(panel, s, calendar)
        if not res.fires:
            continue
        if s.isoformat() <= LAST_TUNING_SIGNAL:
            fired.append((s.isoformat(), tuple(res.top3)))
        else:  # after the last tuning signal and <= inner_end: the purge gap
            purge_gap.append(s.isoformat())
    if len(fired) != N_TUNING_CARDS or (
        fired[0][0] != FIRST_TUNING_SIGNAL or fired[-1][0] != LAST_TUNING_SIGNAL
    ):
        raise Refused(
            f"tuning-era XSMOM cards are not the sealed 22 ({FIRST_TUNING_SIGNAL}..{LAST_TUNING_SIGNAL}):"
            f" got {len(fired)} cards {fired[0][0] if fired else '-'}..{fired[-1][0] if fired else '-'}"
        )
    pead_events = 0
    for s in sessions:
        if s > inner_end:
            break
        pead_events += len(pead_beats(panel, earnings, s, calendar).evaluated)
    if pead_events != 0:
        raise Refused(
            f"PEAD carries {pead_events} tuning-era events — the registration declares zero"
            " (calendar era starts 2024-09-05); inputs have drifted"
        )

    calibration, calibration_sha256 = _load_calibration(menu_sha256)
    B = _bind_B(calibration)

    manifest_body = "".join(f"{label}\0{got[label]}\n" for label in sorted(checks))
    dataset_manifest_hash = hashlib.sha256(manifest_body.encode("utf-8")).hexdigest()
    return Inputs(
        menu=menu,
        menu_sha256=menu_sha256,
        slot=slot,
        protocol_raw_sha256=protocol_raw_sha256,
        protocol_canonical_sha256=protocol_canonical_sha256,
        panel=panel,
        panel_sha256=got["artifacts/paper-trades/ohlc-panel.json"],
        earnings=earnings,
        earnings_sha256=got["artifacts/paper-trades/earnings-calendar.json"],
        calendar=calendar,
        calendar_sha256=got["data/calendar/nyse_sessions_2018_01_02_2026_12_31.json"],
        slot_doc_sha256=got["docs/theory/campaign-2026-09/slots/term-gate.md"],
        indices_sha256={f: got[f"artifacts/desk-store/indices/{f}"] for f in INDEX_FILES},
        gates=gates,
        calibration_sha256=calibration_sha256,
        calibration=calibration,
        B=B,
        tuning_cards=tuple(fired),
        purge_gap_foms=tuple(purge_gap),
        pead_tuning_events=pead_events,
        dataset_manifest_hash=dataset_manifest_hash,
    )


# ---- gate evaluation ---------------------------------------------------------------------


@dataclass(frozen=True)
class GateReading:
    lag_iso: str
    value: float
    threshold: float | None  # None for SIGN (a declared constant)
    off: bool  # True = stressed side = card WITHHELD
    trail_n: int
    lag_is_prev_session: bool


def read_gate(inputs: Inputs, var: str, style: str, decision: date) -> GateReading:
    """The gate at decision session t: index closes of session t-1 only,
    trailing stats over the 252 index sessions [t-252, t-1] inclusive."""
    gates = inputs.gates
    cal = inputs.calendar
    i = gates.ordinals.get(decision)
    if i is None or i < TRAILING_SESSIONS:
        raise Refused(
            f"decision session {decision.isoformat()} is not on the index grid"
            " or lacks 252 trailing index sessions"
        )
    lag = gates.sessions[i - 1]
    value = gates.values[var][lag]
    trail = [gates.values[var][s] for s in gates.sessions[i - TRAILING_SESSIONS : i]]
    if len(trail) != TRAILING_SESSIONS:
        raise Refused("trailing window arithmetic is not 252 sessions")
    if style == "Q60":
        threshold = _quantile_p60(trail)
    elif style == "SIGN":
        threshold = {"r93": 1.0, "inc": 0.0, "bs": 0.0}[var]
    else:
        raise Refused(f"unknown threshold style {style!r}")
    return GateReading(
        lag_iso=lag.isoformat(),
        value=value,
        threshold=threshold,
        off=value >= threshold,
        trail_n=len(trail),
        lag_is_prev_session=cal.previous_session(decision) == lag,
    )


def regime_of(inputs: Inputs, decision: date) -> str:
    """T2's regime split: r93(t-1) >= its trailing-252 median -> STRESSED."""
    gates = inputs.gates
    i = gates.ordinals[decision]
    lag = gates.sessions[i - 1]
    trail = [gates.values["r93"][s] for s in gates.sessions[i - TRAILING_SESSIONS : i]]
    return "STRESSED" if gates.values["r93"][lag] >= statistics.median(trail) else "CALM"


# ---- T1: card-lane arithmetic (the tnull _complete_trade code path) -----------------------


@dataclass(frozen=True)
class Trade:
    entry: str
    exit_session: str | None
    name: str
    gross: float | None
    detail: str


def _complete_trade(inputs: Inputs, name: str, entry: date) -> Trade:
    cal, panel = inputs.calendar, inputs.panel
    bars = panel[name]
    entry_iso = entry.isoformat()
    if entry_iso not in bars:
        return Trade(entry_iso, None, name, None, "no-entry-bar")
    sessions = cal.sessions()
    i = cal.ordinal(entry)
    window = [s.isoformat() for s in sessions[i + 1 : i + HOLD_SESSIONS + 1]]
    if len(window) != HOLD_SESSIONS or any(w not in bars for w in window):
        return Trade(entry_iso, None, name, None, "hold-incomplete")
    exit_iso = window[-1]
    gross = float(
        Decimal(str(bars[exit_iso]["close"])) / Decimal(str(bars[entry_iso]["close"])) - 1
    )
    return Trade(entry_iso, exit_iso, name, gross, "ok")


def _cell_stats(trades: Sequence[Mapping[str, Any]], rt: float) -> dict[str, Any]:
    """Per-trade stats for one bucket of trade rows (the tnull convention:
    day-clustered t over per-entry-day means; here computed on NET day means
    — the term-gate cell bar bands the net ON mean and its clustered t)."""
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
                "net_per_trade_mean": None,
                "net_day_mean": None,
                "net_clustered_t": None,
                "gross_per_trade_mean": None,
                "net15_per_trade_mean": None,
                "per_trade_mean_sd_day_clustered": None,
            }
        )
        return out
    nets = [t["gross"] - rt for t in complete]
    by_day: dict[str, list[float]] = {}
    for t, net in zip(complete, nets, strict=True):
        by_day.setdefault(t["entry"], []).append(net)
    day_means = [statistics.fmean(v) for _d, v in sorted(by_day.items())]
    m = statistics.fmean(day_means)
    sd = statistics.stdev(day_means) if len(day_means) > 1 else None
    if sd is not None and sd > 0:
        t_stat = m / (sd / math.sqrt(len(day_means)))
        cse = sd / math.sqrt(len(day_means))
    elif sd == 0:
        t_stat = math.inf if m else math.nan
        cse = 0.0
    else:
        t_stat = None
        cse = None
    out.update(
        {
            "days": len(day_means),
            "net_per_trade_mean": statistics.fmean(nets),
            "net_day_mean": m,
            "net_clustered_t": t_stat,
            "gross_per_trade_mean": statistics.fmean(t["gross"] for t in complete),
            "net15_per_trade_mean": statistics.fmean(t["gross"] - RT_ROBUST for t in complete),
            "per_trade_mean_sd_day_clustered": cse,
        }
    )
    return out


def run_t1(inputs: Inputs, cell: Mapping[str, str]) -> dict[str, Any]:
    """Inner-fold T1 evaluation. XSMOM leg: the 22 tuning cards. PEAD leg:
    zero tuning-era cards by registration — NOT_EVALUABLE, never a defect."""
    rule = cell["rule"]
    index_gaps = 0
    if rule == "pead_beat":
        return {
            "arm": "T1",
            "config": dict(cell),
            "cards": [],
            "on": _cell_stats([], RT_PRIMARY),
            "off": _cell_stats([], RT_PRIMARY),
            "spread_on_minus_off": None,
            "utility": None,
            "index_gap_sessions": 0,
            "round1_verdict": "NOT_EVALUABLE",
            "round1_verdict_reason": (
                "zero tuning-era PEAD cards (earnings-calendar era starts 2024-09-05);"
                " the registration declares PEAD thresholds fit-free — no inner data,"
                " no inner verdict beyond this structural NOT_EVALUABLE"
            ),
            "sealed_window_scored": False,
        }

    rows_on: list[dict[str, Any]] = []
    rows_off: list[dict[str, Any]] = []
    readings: dict[str, dict[str, Any]] = {}
    for entry_iso, top3 in inputs.tuning_cards:
        entry = date.fromisoformat(entry_iso)
        reading = read_gate(inputs, cell["gate"], cell["style"], entry)
        if not reading.lag_is_prev_session:
            index_gaps += 1
        readings[entry_iso] = {
            "lag": reading.lag_iso,
            "value": reading.value,
            "threshold": reading.threshold,
            "off": reading.off,
            "trail_n": reading.trail_n,
        }
        bucket = rows_off if reading.off else rows_on
        for name in top3:
            t = _complete_trade(inputs, name, entry)
            bucket.append(
                {
                    "entry": t.entry,
                    "exit": t.exit_session,
                    "name": t.name,
                    "gross": t.gross,
                    "detail": t.detail,
                }
            )
    on = _cell_stats(rows_on, RT_PRIMARY)
    off = _cell_stats(rows_off, RT_PRIMARY)
    spread = (
        on["net_per_trade_mean"] - off["net_per_trade_mean"]
        if on["net_per_trade_mean"] is not None and off["net_per_trade_mean"] is not None
        else None
    )
    # criterion-3 analog (disclosure only; the utility bar binds at the sealed round)
    card_nets: dict[str, tuple[float, int]] = {}
    for rows, keep in ((rows_on, True), (rows_off, False)):
        for t in rows:
            if t["gross"] is None:
                continue
            net, n = card_nets.get(t["entry"], (0.0, 0))
            card_nets[t["entry"]] = (net + t["gross"] - RT_PRIMARY, n + 1)
    ungated_mean_per_card = (
        statistics.fmean(v / n for v, n in card_nets.values()) if card_nets else None
    )
    on_cards = {
        e: (v, n)
        for e, (v, n) in card_nets.items()
        if readings[e]["off"] is False
    }
    gated_mean_per_card = (
        statistics.fmean(v / n for v, n in on_cards.values()) if on_cards else None
    )
    ungated_total = sum(v for v, _n in card_nets.values())
    gated_total = sum(v for v, _n in on_cards.values())
    return {
        "arm": "T1",
        "config": dict(cell),
        "cards": [
            {"entry": e, "top3": list(top3), **readings[e]}
            for e, top3 in inputs.tuning_cards
        ],
        "on": on,
        "off": off,
        "spread_on_minus_off": spread,
        "spread15_on_minus_off": (
            _cell_stats(rows_on, RT_ROBUST)["net_per_trade_mean"]
            - _cell_stats(rows_off, RT_ROBUST)["net_per_trade_mean"]
            if rows_on and rows_off
            else None
        ),
        "utility_disclosure": {
            "gated_mean_per_card": gated_mean_per_card,
            "ungated_mean_per_card": ungated_mean_per_card,
            "gated_total_pnl_usd_per_2500_leg": 2500.0 * gated_total,
            "ungated_total_pnl_usd_per_2500_leg": 2500.0 * ungated_total,
            "note": "criterion 3 binds on the sealed window only; inner values are disclosure",
        },
        "index_gap_sessions": index_gaps,
        "round1_verdict": None,
        "round1_verdict_reason": (
            "no round-1 verdict: the T1 cell bar is 'Evaluated ONLY on the sealed window'"
            " (slot section 6); the inner stats exist to rank the six gate specs"
        ),
        "sealed_window_scored": False,
    }


# ---- T2: the frozen XSMOM-EXITGRID re-derivation, holdout era only ------------------------


def _load_frozen_exitgrid():
    spec = importlib.util.spec_from_file_location(
        "xsmom_exitgrid_frozen", EXITGRID_SCRIPT_PATH
    )
    if spec is None or spec.loader is None:
        raise Refused(f"cannot import the frozen grid script at {EXITGRID_SCRIPT_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclass annotation resolution needs the entry
    spec.loader.exec_module(module)
    if module.HOLDOUT_END != INNER_TUNING_END or module.HOLD != EXITGRID_HOLD:
        raise Refused("the frozen grid script's geometry drifted from the registration")
    return module


def run_t2(
    inputs: Inputs,
    cell: Mapping[str, str],
    t2_stats: tuple[dict[str, Any], dict[str, Any]],
) -> dict[str, Any]:
    """Inner-fold T2 cell: per-signal paired diff (variant - hold60) within
    (construction, variant, regime) on the HOLDOUT era only. The frozen
    grid's 'full' era overlaps this slot's sealed window and is NOT
    computed. Copy-faithfulness is asserted inside _t2_core (one shared pass
    for all 12 cells) against the published holdout table."""
    stats, anchor = t2_stats
    construction, variant, regime = (
        cell["construction"],
        cell["variant"],
        cell["regime"],
    )
    cellstats = stats[construction][variant][regime]
    # the flip verdict spans the variant's two regime cells (slot section 6)
    pair = stats[construction][variant]
    if cellstats["days"] < T2_MIN_DATES or pair["CALM"]["days"] < T2_MIN_DATES:
        verdict = "DESCRIPTIVE-ONLY:NOT_EVALUABLE"
        reason = f"cell signal dates {cellstats['days']} (or pair) < {T2_MIN_DATES}"
    else:
        stressed = pair["STRESSED"]
        calm = pair["CALM"]
        flips = (
            stressed["d_mean"] > 0
            and stressed["t_cons"] == stressed["t_cons"]
            and stressed["t_cons"] >= T2_FLIP_T
            and calm["d_mean"] <= 0
        )
        verdict = (
            "DESCRIPTIVE-ONLY:REGIME-SIGN-FLIP" if flips else "DESCRIPTIVE-ONLY:NO-REGIME-SIGNAL"
        )
        reason = (
            "paired mean > 0 with conservative t >= 2 in STRESSED while <= 0 in CALM"
            if flips
            else "no variant shows the stressed-only rescue pattern on the inner (holdout) read"
        )
    return {
        "arm": "T2",
        "config": dict(cell),
        "cell": cellstats,
        "pair": {"CALM": pair["CALM"], "STRESSED": pair["STRESSED"]},
        "pooled_holdout_anchor": anchor[construction][variant],
        "power_floor_dates": T2_MIN_DATES,
        "round1_verdict": verdict,
        "round1_verdict_reason": reason,
        "round1_verdict_scope": (
            "INTERIM inner read (holdout era, signal dates <= 2024-09-03); the registered"
            " descriptive verdict covers the frozen grid including its 'full' era, which"
            " overlaps this slot's sealed window and is deferred to the sealed round"
            " (XU post-hoc precedent: no live change is possible from either read)"
        ),
        "sealed_window_scored": False,
    }


def _t2_core(inputs: Inputs) -> tuple[dict[str, Any], dict[str, Any]]:
    """One pass over the frozen grid's HOLDOUT era for all three variants of
    both constructions, split by the r93 regime. Filtering to signal dates
    <= HOLDOUT_END happens BEFORE any return is computed; nothing on the
    'full' era is evaluated."""
    frozen = _load_frozen_exitgrid()
    series = frozen.load_panel()
    variants = ("hold1", "tp100c", "oco100_150")
    policies = {p.key: p for p in frozen.POLICIES if p.key in variants}
    if set(policies) != set(variants):
        raise Refused("the frozen grid no longer carries the three declared T2 variants")
    stats: dict[str, Any] = {}
    anchor: dict[str, Any] = {}
    holdout_end = date.fromisoformat(INNER_TUNING_END)
    for label, lb, sk, tk in frozen.CONSTRUCTIONS:
        leg_all, _everything = frozen.signals_for(series, lb, sk, tk)
        leg = [s for s in leg_all if date.fromisoformat(s[0]) <= holdout_end]
        h60 = {
            (d, n): series[n][1][p + frozen.HOLD] / entry - 1 - frozen.RT
            for d, n, p, entry in leg
        }
        regimes = {d: regime_of(inputs, date.fromisoformat(d)) for d, _n, _p, _e in leg}
        per_variant: dict[str, dict[str, Any]] = {}
        anchor[label] = {}
        for key in variants:
            policy = policies[key]
            rows: list[tuple[str, str, str, float]] = []  # date, regime, name, diff
            for d, n, p, entry in leg:
                ds, _cl, _po, bars = series[n]
                window = [bars[dd] for dd in ds[p + 1 : p + frozen.HOLD + 1]]
                use = window if policy.horizon == frozen.HOLD else window[: policy.horizon]
                fill = frozen.simulate_fill(entry, use, policy)
                net = fill.price / entry - 1 - frozen.RT
                rows.append((d, regimes[d], n, net - h60[(d, n)]))
            cells: dict[str, Any] = {}
            for reg in ("CALM", "STRESSED"):
                diffs = [x for d, r, _n, x in rows if r == reg]
                dates = [d for d, r, _n, x in rows if r == reg]
                tn, tc = frozen.t_stat(diffs, dates)
                cells[reg] = {
                    "n": len(diffs),
                    "days": len(set(dates)),
                    "d_mean": statistics.fmean(diffs) if diffs else float("nan"),
                    "t_naive": tn,
                    "t_clust": tc,
                    "t_cons": min(tn, tc) if diffs else float("nan"),
                    "hit": (
                        sum(1 for x in diffs if x > 0) / len(diffs) if diffs else float("nan")
                    ),
                }
            per_variant[key] = cells
            # pooled (regime-ignored) copy-faithfulness anchor vs the published table
            pooled_diffs = [x for _d, _r, _n, x in rows]
            pooled_dates = [d for d, _r, _n, x in rows]
            ptn, ptc = frozen.t_stat(pooled_diffs, pooled_dates)
            anchor[label][key] = {
                "n": len(pooled_diffs),
                "days": len(set(pooled_dates)),
                "d_mean": statistics.fmean(pooled_diffs),
                "t_naive": ptn,
                "t_clust": ptc,
                "t_cons": min(ptn, ptc),
                "published_d_mean": EXITGRID_ANCHOR[label][key],
                "abs_drift": abs(statistics.fmean(pooled_diffs) - EXITGRID_ANCHOR[label][key]),
            }
        # hold-60 baseline anchor (n/days must reproduce the published table)
        base_n = len(set(d for d, _n, _p, _e in leg))
        want = EXITGRID_ANCHOR[label]
        if len(leg) != want["hold60_n"] or base_n != want["hold60_days"]:
            raise Refused(
                f"{label}: holdout hold-60 signal set ({len(leg)} signals / {base_n} days)"
                f" != the published XSMOM-EXITGRID table ({want['hold60_n']} / {want['hold60_days']})"
                " -- the panel or the frozen script drifted"
            )
        for key in variants:
            drift = anchor[label][key]["abs_drift"]
            if not (drift <= ANCHOR_TOL):
                raise Refused(
                    f"{label}/{key}: pooled holdout d_mean drift {drift:.6f} > {ANCHOR_TOL}"
                    f" vs the published {want[key]} -- the re-derivation is not copy-faithful"
                )
        stats[label] = per_variant
    return stats, anchor


# ---- registry / stamping -----------------------------------------------------------------


def _trial_id(config_id: str) -> str:
    return f"{SCOPE_ID}-{config_id}-g{TRIAL_GENERATION}"


def _cell_by_id(config_id: str) -> Mapping[str, str]:
    cell = next((c for c in ALL_CELLS if c["id"] == config_id), None)
    if cell is None:
        raise Refused(f"{config_id} is not a registered term-gate cell")
    return cell


def _open_registry() -> TrialRegistry:
    CAMPAIGN_DIR.mkdir(parents=True, exist_ok=True)
    return TrialRegistry(REGISTRY_PATH)


def _hyperparameters(inputs: Inputs, cell: Mapping[str, str]) -> dict[str, Any]:
    hyper: dict[str, Any] = {
        "scope_id": SCOPE_ID,
        "slot_id": SLOT_ID,
        "config_id": cell["id"],
        "arm": cell["arm"],
        "model_family": MODEL_FAMILY,
        "round": ROUND_LABEL,
        "role": cell.get("role"),
        "lane": "card-lane (equity close-to-close)",
        "hold_sessions": HOLD_SESSIONS if cell["arm"] == "T1" else EXITGRID_HOLD,
        "rt_primary_bp": 5,
        "rt_robust_bp": 15,
        "trailing_sessions": TRAILING_SESSIONS,
        "q60_percentile": Q60_PCT,
        "q60_method": "linear interpolation (statistics quantiles inclusive)",
        "calendar_exclusions": [PHANTOM_ISO],
        "windows": {
            "inner_tuning": [FIRST_TUNING_SIGNAL, INNER_TUNING_END],
            "tuning_cards_xsmom": [FIRST_TUNING_SIGNAL, LAST_TUNING_SIGNAL],
            "purge_gap_foms_dropped": list(inputs.purge_gap_foms),
            "declared_sealed_NOT_SCORED_round1": [SEALED_START, SEALED_END],
        },
        "t2_full_era_deferred": (
            "the frozen grid's 'full' era (signal dates > 2024-09-03) overlaps the sealed"
            " window; deferred to the sealed round"
        ),
        "inputs_sha256": {
            "ohlc-panel.json": inputs.panel_sha256,
            "earnings-calendar.json": inputs.earnings_sha256,
            "nyse_sessions json": inputs.calendar_sha256,
            **{f"indices/{k}": v for k, v in inputs.indices_sha256.items()},
            "slots/term-gate.md": inputs.slot_doc_sha256,
            "tnull/calibration-v3.json": inputs.calibration_sha256,
        },
        "registration_menu_sha256": inputs.menu_sha256,
        "protocol_raw_sha256": inputs.protocol_raw_sha256,
        "protocol_canonical_sha256": inputs.protocol_canonical_sha256,
        "dataset_manifest_hash": inputs.dataset_manifest_hash,
        "B_card_era": inputs.B,
        "B_note": (
            "B(card-era, shape) read from calibration-v3.json (never recomputed); binds the"
            " sealed-round absolute-mean leg only — the card-era window is EVALUABLE in the"
            " v3 stamp, so the sealed round reads the normal B-excess"
        ),
    }
    if cell["arm"] == "T1":
        hyper["gate"] = {
            "variable": cell["gate"],
            "style": cell["style"],
            "rule": cell["rule"],
            "off_condition": (
                f"{cell['gate']}(t-1) >= "
                + (
                    f"trailing-{TRAILING_SESSIONS} q{Q60_PCT}"
                    if cell["style"] == "Q60"
                    else {"r93": "1.0", "inc": "0", "bs": "0"}[cell["gate"]]
                )
            ),
        }
    else:
        hyper["t2"] = {
            "construction": cell["construction"],
            "variant": cell["variant"],
            "regime": cell["regime"],
            "regime_rule": f"r93(t-1) >= trailing-{TRAILING_SESSIONS} median -> STRESSED",
            "era": f"signal dates <= {INNER_TUNING_END} (the frozen grid's holdout era)",
        }
    return hyper


def _config_hash(hyperparameters: Mapping[str, Any]) -> str:
    body = json.dumps(hyperparameters, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _scope(inputs: Inputs) -> TrialScope:
    return TrialScope(
        protocol_id="tree_options",
        protocol_hash=inputs.protocol_canonical_sha256,
        outer_fold_id=f"campaign-2026-09/{SCOPE_ID}/inner-{ROUND_LABEL}",
        target_horizon="hold20:t1,hold60:t2",
        feature_set_id="ohlc-panel|cboe-term-structure|v1",
        model_family=MODEL_FAMILY,
    )


def _stamp(inputs: Inputs, cell: Mapping[str, str]) -> dict[str, Any]:
    return {
        "program": "campaign-2026-09",
        "trial_id": _trial_id(cell["id"]),
        "trial_generation": TRIAL_GENERATION,
        "slot_id": SLOT_ID,
        "scope_id": SCOPE_ID,
        "round": ROUND_LABEL,
        "config_id": cell["id"],
        "registration_menu_sha256": inputs.menu_sha256,
        "slot_doc_sha256": inputs.slot_doc_sha256,
        "protocol_raw_sha256": inputs.protocol_raw_sha256,
        "protocol_canonical_sha256": inputs.protocol_canonical_sha256,
        "dataset_manifest_hash": inputs.dataset_manifest_hash,
        "calibration_v3_sha256": inputs.calibration_sha256,
        "calibration_v3_verdict": inputs.calibration["verdict"]["slot"],
        "git_sha": _git_head(REPO_ROOT),
        "runner_sha256": _sha256_file(Path(__file__).resolve()),
        "frozen_exitgrid_sha256": _sha256_file(EXITGRID_SCRIPT_PATH),
        "generated_at": _utcnow().isoformat(),
    }


def _trial_artifact_path(trial_id: str) -> Path:
    return TRIALS_DIR / f"{trial_id}.json"


# -- phases --------------------------------------------------------------------------------


def phase_register() -> int:
    inputs = load_and_bind()
    scope = _scope(inputs)
    registry = _open_registry()
    try:
        existing = [cid for cid in CONFIG_IDS if registry.is_registered(_trial_id(cid))]
        if existing:
            raise Refused(
                f"registration is one-shot: {[ _trial_id(c) for c in existing ]} already registered"
            )
        for cell in ALL_CELLS:
            hyper = _hyperparameters(inputs, cell)
            record = TrialRecord(
                trial_id=_trial_id(cell["id"]),
                created_at=_utcnow(),
                hypothesis=inputs.slot["hypothesis"],
                git_sha=_git_head(REPO_ROOT),
                config_hash=_config_hash(hyper),
                dataset_manifest_hash=inputs.dataset_manifest_hash,
                train_window=(
                    date.fromisoformat(FIRST_TUNING_SIGNAL),
                    date.fromisoformat(LAST_TUNING_SIGNAL),
                ),
                validation_window=None,
                test_window=(
                    date.fromisoformat(SEALED_START),
                    date.fromisoformat(SEALED_END),
                ),
                hyperparameters=hyper,
                scope_key=scope.scope_key(),
            )
            registry.register(record, scope)
            print(f"registered {_trial_id(cell['id'])} arm={cell['arm']}")
    finally:
        registry.close()
    print(
        f"registry: {REGISTRY_PATH} (scope trials={len(CONFIG_IDS)}, cap=32);"
        " NO outcome has been computed or viewed"
    )
    return 0


def phase_execute() -> int:
    inputs = load_and_bind()
    TERM_DIR.mkdir(parents=True, exist_ok=True)
    TRIALS_DIR.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise Refused("another term-gate execution holds the lock -- one run at a time") from None
        registry = _open_registry()
        try:
            t2_stats: tuple[dict[str, Any], dict[str, Any]] | None = None
            for cell in ALL_CELLS:
                trial_id = _trial_id(cell["id"])
                artifact = _trial_artifact_path(trial_id)
                if artifact.exists():
                    raise Refused(f"{artifact} already exists -- executions are one-shot per trial")
                status = registry.status(trial_id)
                if status != "REGISTERED":
                    raise Refused(f"{trial_id} is {status}, not REGISTERED -- refusing to re-run")
                hyper = _hyperparameters(inputs, cell)
                git_sha = _git_head(REPO_ROOT)
                registry.mark_running(
                    trial_id,
                    git_sha=git_sha,
                    config_hash=_config_hash(hyper),
                    dataset_manifest_hash=inputs.dataset_manifest_hash,
                    at=_utcnow(),
                )
                started = time.monotonic()
                if cell["arm"] == "T1":
                    payload = run_t1(inputs, cell)
                else:
                    if t2_stats is None:  # one shared pass for all 12 T2 cells
                        t2_stats = _t2_core(inputs)
                    payload = run_t2(inputs, cell, t2_stats)
                payload["elapsed_s"] = round(time.monotonic() - started, 3)
                # machinery self-check (the tnull g1 lesson): an EMPTY T1
                # xsmom cell is a runner defect, never an outcome -- the
                # trial FAILs and no artifact is written. PEAD emptiness is
                # the registration's own declaration and carries its
                # structural NOT_EVALUABLE instead.
                if cell["arm"] == "T1" and cell["rule"] == "xsmom_top3":
                    if payload["on"]["n_entries"] + payload["off"]["n_entries"] == 0:
                        registry.fail(
                            trial_id,
                            "T1 xsmom cell executed with 0 legs -- runner machinery defect",
                            at=_utcnow(),
                        )
                        raise Refused(
                            f"{trial_id}: T1 xsmom cell executed with 0 legs (machinery defect;"
                            " trial FAILED, no artifact written)"
                        )
                body = {"stamp": _stamp(inputs, cell), "payload": payload}
                artifact.write_text(
                    json.dumps(body, indent=2, sort_keys=True) + "\n", encoding="utf-8"
                )
                registry.complete(trial_id, metrics_uri=str(artifact), outcome_at=_utcnow())
                if cell["arm"] == "T1" and cell["rule"] == "xsmom_top3":
                    print(
                        f"{trial_id}: COMPLETED artifact={artifact}"
                        f" ON n={payload['on']['n_complete']}/{payload['on']['n_entries']}"
                        f" OFF n={payload['off']['n_complete']}/{payload['off']['n_entries']}"
                        f" spread={payload['spread_on_minus_off']}"
                    )
                else:
                    print(
                        f"{trial_id}: COMPLETED artifact={artifact}"
                        f" verdict={payload['round1_verdict']}"
                    )
        finally:
            registry.close()
    finally:
        os.close(lock_fd)
    return 0


def phase_rank() -> int:
    """The mandated tuning-era selection: rank the six gate specs on the 22
    XSMOM tuning cards, recorded before any sealed-era run. Reads ONLY the
    executed round-1 artifacts."""
    inputs = load_and_bind()
    if RANKING_PATH.exists():
        raise Refused(f"{RANKING_PATH} already exists -- the ranking stamp is one-shot")
    rows = []
    for cell in T1_CELLS:
        if cell["rule"] != "xsmom_top3":
            continue
        body = json.loads(_trial_artifact_path(_trial_id(cell["id"])).read_text(encoding="utf-8"))
        if body["stamp"]["trial_id"] != _trial_id(cell["id"]):
            raise Refused(f"artifact trial_id mismatch for {cell['id']}")
        if body["stamp"]["registration_menu_sha256"] != inputs.menu_sha256:
            raise Refused(f"artifact {cell['id']} was executed against a different menu hash")
        payload = body["payload"]
        rows.append(
            {
                "config_id": cell["id"],
                "gate_spec": f"{cell['gate']}x{cell['style']}",
                "role": cell["role"],
                "on_net_mean": payload["on"]["net_per_trade_mean"],
                "off_net_mean": payload["off"]["net_per_trade_mean"],
                "spread_on_minus_off": payload["spread_on_minus_off"],
                "on_clustered_t": payload["on"]["net_clustered_t"],
                "n_on_cards": payload["on"]["days"],
                "n_off_cards": payload["off"]["days"],
                "off_legs": payload["off"]["n_complete"],
            }
        )
    sparser = {"SIGN": 0, "Q60": 1}  # ties -> sparser gate first

    def sort_key(r: Mapping[str, Any]) -> tuple[int, float, int, str]:
        spread = r["spread_on_minus_off"]
        # a cell with ZERO OFF tuning cards has no spread to rank on (the
        # slot's own disclosed 5.1%-OFF SIGN risk): it ranks BELOW every
        # cell with a defined spread -- no evidence of separation, recorded
        # as such, never silently dropped from the mandated six-spec ranking
        if spread is None:
            return (1, 0.0, sparser[r["gate_spec"].split("x")[1]], r["config_id"])
        return (0, -spread, sparser[r["gate_spec"].split("x")[1]], r["config_id"])

    ranked = sorted(rows, key=sort_key)
    best = ranked[0]
    incumbent = [r for r in ranked if r["role"] == "incumbent"]
    ranking = {
        "stamp": {
            **_stamp(inputs, {"id": "RANK", "arm": "T1"}),
            "trial_id": None,
            "config_id": None,
            "artifact_trial_ids": [_trial_id(c["id"]) for c in T1_CELLS if c["rule"] == "xsmom_top3"],
            "menu_hypothesis": inputs.slot["hypothesis"],
        },
        "selection_rule": (
            "the only tuning-era selection the registration permits: ranking the six gate"
            " specs on the 22 XSMOM tuning cards (entries 2022-11-01..2024-08-01), recorded"
            " BEFORE any sealed-era run; statistic = ON-OFF per-trade net mean spread (5bp"
            " RT), ties -> sparser gate (SIGN before Q60) then config id lexicographic"
        ),
        "ranking": ranked,
        "inner_fold_best": {
            "config_id": best["config_id"],
            "gate_spec": best["gate_spec"],
            "metric": "on_minus_off_net_per_trade_mean_5bp_rt_22_tuning_xsmom_cards",
            "value": best["spread_on_minus_off"],
        },
        "best_incumbent": incumbent[0] if incumbent else None,
        "notes": [
            "PRIMARY cell T1-01 (r93xQ60) stays the registration's declared primary;"
            "this ranking is the recorded tuning-era evidence, not a re-registration.",
            "The spread is drift-immune (both legs share the window's drift); the"
            " B-excess absolute-mean leg binds at the sealed round only.",
            "PEAD-leg cells carry no tuning-era cards by registration and are excluded.",
        ],
    }
    TERM_DIR.mkdir(parents=True, exist_ok=True)
    RANKING_PATH.write_text(json.dumps(ranking, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    def _fmt(v: float | None) -> str:
        return f"{v:+.6f}" if v is not None else "n/a"

    for r in ranked:
        print(
            f"  {r['config_id']} {r['gate_spec']:8s} {r['role']:26s}"
            f" spread={_fmt(r['spread_on_minus_off'])} ON={_fmt(r['on_net_mean'])}"
            f" OFF={_fmt(r['off_net_mean'])} nOFFcards={r['n_off_cards']}"
        )
    best_spread = (
        f"{best['spread_on_minus_off']:+.6f}" if best["spread_on_minus_off"] is not None else "n/a"
    )
    print(f"inner-fold best: {best['config_id']} ({best['gate_spec']}) spread={best_spread}")
    print(f"ranking stamped: {RANKING_PATH}")
    return 0


def phase_plan() -> int:
    inputs = load_and_bind()
    print(f"menu sha256 {inputs.menu_sha256} (sidecar-verified)")
    print(f"protocol raw {inputs.protocol_raw_sha256[:16]}... canonical {inputs.protocol_canonical_sha256[:16]}...")
    print(f"calibration-v3 sha256 {inputs.calibration_sha256[:16]}... verdict {inputs.calibration['verdict']['slot']}")
    for shape in ("xsmom", "event"):
        b = inputs.B[shape]
        print(
            f"B[card-era,{shape:5s}] = {b['B_net_per_trade_mean']:+.6f}"
            f" se={b['per_trade_mean_sd_day_clustered']}"
            f" not_evaluable={b['window_not_evaluable_in_v3_stamp']}"
        )
    print(f"tuning cards: {len(inputs.tuning_cards)} ({inputs.tuning_cards[0][0]}..{inputs.tuning_cards[-1][0]})")
    print(f"purge-gap FOMs dropped: {list(inputs.purge_gap_foms)}")
    print(f"PEAD tuning-era events: {inputs.pead_tuning_events}")
    gaps_in_span = [s.isoformat() for s in inputs.gates.missing_index_sessions]
    print(f"index sessions missing any of the four CSVs (whole calendar): {len(gaps_in_span)} {gaps_in_span[:8]}")
    print(f"sealed window {SEALED_START}..{SEALED_END}: NEVER scored in round 1")
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
        help="INV-13: write the 24 trial rows (REGISTERED, no outcome) to the slot registry",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="run the INNER folds one-shot per trial (REGISTERED -> RUNNING -> COMPLETED + artifact)",
    )
    parser.add_argument(
        "--rank",
        action="store_true",
        help="stamp the mandated six-spec tuning-era ranking from the executed artifacts",
    )
    parser.add_argument("--plan", action="store_true", help="read-only: print the bound geometry")
    args = parser.parse_args(argv)
    try:
        if args.register:
            return phase_register()
        if args.execute:
            return phase_execute()
        if args.rank:
            return phase_rank()
        if args.plan:
            return phase_plan()
    except Refused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
