#!/usr/bin/env python3
"""campaign-2026-09 JEPA-FILTER runner (scope c09-jf, menu order 5) — ROUND 1: INNER FOLDS ONLY.

Executes exactly the eight configs the slot registered
(docs/theory/campaign-2026-09/slots/jepa-filter.md, sealed translation of
docs/theory/trade-jepa-exploration-2026-09-23.md; menu entry slots[5] of
docs/theory/campaign-2026-09-registration.json v3, sha sidecar-verified at
bind): JF-V1-H5, JF-V1-H21, JF-V2-H5, JF-V2-H21, JF-V3-H5, JF-V3-H21,
JF-V4-H5, JF-V4-H21. Every pinned constant below is the registration's own;
any drift from the sealed menu / slot-doc / input hashes refuses before
anything runs (the tnull_run.py binding discipline).

ROUND 1 SCOPE (inner folds only — the sealed windows are NEVER scored, never
read for tuning, never plotted here): origins 2024-07-01..2025-12-31 (378
panel-union sessions), quarterly anchored-expanding refits at the first
session of 2024-07/2024-10/2025-01/2025-04/2025-07/2025-10. The sealed outer
window (2026-01-02..cutoff-h) is not touched by this runner at all: no outer
origin is scored, no (b)-track quantity (S_u vs vix_term, MDD21, the paired
bootstrap, the randomized-score null spread) is computed — those belong to
the sealed outer round after the inner-loop selection is frozen and recorded
(--select does exactly that freezing, on the executed artifacts only).

Machinery (all pinned by the slot doc; the [PINNED] tags are theirs):

* State (d=11) per (name, session): equity block (7) = r_1, r_5, r_21, r_63
  (close-to-close ln returns at grid offsets), ln v and ln mean(v[-20..0])
  with v the FORECAST-001 rv.py::variance_proxy VERBATIM, and
  ln(dollar volume) - ln(mean dollar volume[-62..0]); each replaced by its
  per-session cross-sectional z over the roster (population sd, ddof=0;
  same-session data only — INV-05). Index block (4, shared across names) =
  VIX/VIX3M-1, VIX9D/VIX-1, 21-session change in ln VIX (grid offsets),
  VVIX level; every index input uses the observation dated on or before the
  PREVIOUS panel-union session (INV-02, one-session lag) and is
  training-fold z-scored per refit (moments over training sessions).
* Surprise is indexed by its COMPLETION session u with v the quarterly refit
  in force at u-h (a refit is in force from its own first session; the
  initial 2021-09-13..2024-06-30 training block is the R1 anchor, never a
  scoring vintage — hence the first ~h inner origins carry no surprise):
  s_{i,u} = || g^v( z^v_{i,u-h} ) - f^v( state_{i,u} ) ||, both f and g from
  the vintage that made the forecast; s is divided by the vintage's
  training-fold pooled SD of s (ddof=1).
* V1/V2/V4 encoder: top-k PCs of the training-fold standardized state
  (centered by training-fold mean; eigenvector signs fixed by largest-abs
  loading), k=8 (V1, V4) / 16->min(16,11)=11 (V2 — d=11 caps PCA at 11
  components; padding zero dims is identity, disclosed). Predictor: ridge of
  z_u on z_{u-h}, per latent dim, intercept unpenalized, alpha=1.0
  [PINNED]. V4 state: the 26 single stocks' 7 equity features residualized
  by training-fold OLS on {1, SPY, QQQ, IWM, mapped sector ETF} (hand
  sector map, disclosed not machine-verified), then cross-sectional z over
  the 26.
* V3 encoder: linear z = x W and linear g: z -> z A trained jointly by
  full-batch plain gradient descent, 2000 iterations, lr 1e-2, init from
  numpy.random.Generator(PCG64(22)) standard-normal scaled by 1e-2 (W then
  A), objective ||g(z_{u-h}) - z_u||^2 + lambda_v sum_j max(0,1-sd_j)^2 +
  lambda_c * sum_{i!=j} C_ij^2 with lambda_v = lambda_c = 25.0 [PINNED],
  population moments over all training rows. V3 carries the collapse
  tripwire: any eval-fold latent dim variance < 1e-2 x its training-fold
  variance -> NOT_EVALUABLE, never quietly scored.
* Labels r_{i,u->u+h} = ln(C_{i,u+h}/C_{i,u}), complete windows (both
  endpoint bars on the grid). Declared direction (a): long LOW surprise /
  short HIGH surprise — ic_u = Spearman(-s_u, r_u); mean > 0 is the
  declared direction. Breadth floor: an origin with < 25 complete names is
  skipped and counted; > 10% skipped origins = defective (operator ruling).
* Purge: training rows of the refit at quarter Q satisfy
  ordinal(s) + h + 5 < ordinal(first session of Q) (the declared purge gap
  h + 5 carrying the 5-session embargo inside it).
* Future-poison test (precondition of evaluability): for a deterministic
  sample (the first scored origin of each vintage segment), all panel and
  index data dated after u are dropped, features + the vintage fit + s_{i,u}
  recomputed from the truncated inputs, and the result must equal the
  main-run surprise (max abs diff <= 1e-12). A failure is NOT_EVALUABLE.
* Inner disclosures on the identical origin/name cells: the XSMOM score
  (close(u)/close(u-273)-1) IC, and the cheap-surprise controls lnRV21 =
  ln mean(v[-20..0]) and |r_21| under the same long-low convention; plus
  Newey-West (Bartlett, lag h) t beside every mean (never sole authority)
  and per-vintage-segment mean IC (basis-drift decay disclosure).

INV-13 (registration precedes outcome): --register writes the eight trial
rows (REGISTERED, no outcome) to the slot registry sqlite BEFORE any outcome
exists; --execute is one-shot per trial (REGISTERED -> RUNNING -> COMPLETED
with the artifact as metrics_uri, or FAILED on a machinery defect with no
artifact; an interrupted execution may be CONTINUED -- COMPLETED/FAILED
trials are never re-run); --select reads ONLY the executed artifacts
(stamp-bound to their trial ids) plus the registry's FAILED records, applies
the pre-declared SEL-a / SEL-b / FLIP rules, and freezes the round-1
inner-loop selection record. One scored run per cell; no re-gridding; the
verdicts are the sealed outer round's to stamp.

DISCLOSED post-execution repair (2026-09-24): the first --execute completed
JF-V1-H5/H21 and JF-V2-H5/H21 (runner sha EXECUTING_SHA, stamped in their
artifacts) and crashed on JF-V3-H5 when the PINNED VICReg optimizer left
finite arithmetic (overflow -> inf/NaN training surprises -> the run died
inside the first V3 refit before any V3 outcome existed). The repaired build
turns that crash into a per-trial FAILED defect record + continue (V3
divergence is a defect of the sealed constants -- NOT_EVALUABLE by operator
ruling only, never tuned away), lets --execute resume past finished trials,
and teaches --select the FAILED state. No scored cell's computation was
touched: the four completed artifacts are byte-immutable, and the
verification (finite-difference gradient check of vicreg_fit against the
registered objective) passes identically before and after the repair.
"""

from __future__ import annotations

import argparse
import bisect
import fcntl
import hashlib
import json
import math
import os
import statistics
import subprocess
import sys
import warnings
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
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

import numpy as np  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]  # the EXECUTION worktree
if str(REPO_ROOT / "src") not in sys.path:  # pragma: no cover - import plumbing
    sys.path.insert(0, str(REPO_ROOT / "src"))

from tree_options.desk.indices import read_store  # noqa: E402
from tree_options.desk.panel import read_panel_with_sha256  # noqa: E402
from tree_options.desk.rv import proxy_series, trailing_mean  # noqa: E402
from tree_options.protocol.loader import default_protocol, protocol_hash  # noqa: E402
from tree_options.registry.scope import TrialScope  # noqa: E402
from tree_options.registry.sqlite import TrialRegistry  # noqa: E402
from tree_options.schemas.trial import TrialRecord  # noqa: E402

# Data lives in the MAIN checkout; the runner + registration live in the
# execution worktree. Both are pinned by sha256 against the menu below.
MAIN_ROOT = Path("/home/alexk/documents/tree_options")
REGISTRATION_PATH = REPO_ROOT / "docs" / "theory" / "campaign-2026-09-registration.json"
REGISTRATION_SIDECAR = Path(str(REGISTRATION_PATH) + ".sha256")
SLOT_DOC_PATH = REPO_ROOT / "docs" / "theory" / "campaign-2026-09" / "slots" / "jepa-filter.md"
PROTOCOL_PATH = REPO_ROOT / "research_protocol.yaml"
PANEL_PATH = MAIN_ROOT / "artifacts" / "paper-trades" / "ohlc-panel.json"
CALENDAR_PATH = MAIN_ROOT / "data" / "calendar" / "nyse_sessions_2018_01_02_2026_12_31.json"
INDICES_DIR = MAIN_ROOT / "artifacts" / "desk-store" / "indices"
TNULL_V3_PATH = MAIN_ROOT / "artifacts" / "campaign-2026-09" / "tnull" / "calibration-v3.json"

CAMPAIGN_DIR = MAIN_ROOT / "artifacts" / "campaign-2026-09"
REGISTRY_PATH = CAMPAIGN_DIR / "jepa-filter.db"
JEPA_DIR = CAMPAIGN_DIR / "jepa-filter"
TRIALS_DIR = JEPA_DIR / "trials"
SELECTION_PATH = JEPA_DIR / "inner-round1.json"
LOCK_PATH = JEPA_DIR / "execute.lock"

SCOPE_ID = "c09-jf"
SLOT_ID = "jepa-filter"
MODEL_FAMILY = "jepa-linear/1"
ROUND = 1

PHANTOM_ISO = "2025-01-09"  # ledger ruling 2026-09-23: not a session
INIT_START, INIT_END = "2021-09-13", "2024-06-30"
INNER_START, INNER_END = "2024-07-01", "2025-12-31"
OUTER_START = "2026-01-02"
REFIT_QUARTERS: tuple[tuple[int, int], ...] = (
    (2024, 7),
    (2024, 10),
    (2025, 1),
    (2025, 4),
    (2025, 7),
    (2025, 10),
)
EXPECTED_INIT_SESSIONS = 703  # the registration's own verified count
EXPECTED_INNER_SESSIONS = 378
EMBARGO = 5  # rides inside the declared purge gap h + 5
BREADTH_MIN_NAMES = 25  # 3.3 breadth floor
SKIP_DEFECT_FRAC = 0.10  # > 10% skipped origins = defective (operator ruling)
RIDGE_ALPHA = 1.0  # [PINNED] on standardized latents, slope-only penalty
LAMBDA_V = 25.0  # [PINNED] VICReg variance pressure
LAMBDA_C = 25.0  # [PINNED] VICReg covariance pressure
GD_ITERS = 2000  # [PINNED]
GD_LR = 1e-2  # [PINNED]
PCG_SEED = 22  # [PINNED] numpy.random.Generator(PCG64(22))
V3_COLLAPSE_RATIO = 1e-2  # eval-fold var < 1e-2 x train-fold var -> trip
POISON_TOL = 1e-12
# DISCLOSED (term-gate round-1 precedent): the four cells executed by the
# 2026-09-24 build below bind its sha in their stamps; the post-execution
# edits in THIS build touch ONLY defect handling (VICReg divergence ->
# per-trial FAILED + continue) and selection over FAILED trials -- never a
# scored cell's computation, which stays one-shot and immutable.
EXECUTING_SHA = "6d2ed4060464a328ee152cb4a77eec93d064dc1c886ff32eb8300c8b6132e986"
XSMOM_LOOKBACK = 273  # signals.XSMOM_CONVENTION: close(t)/close(t-273)-1
SELECTION_TIE_BREAK: tuple[str, ...] = ("V1", "V2", "V4", "V3")  # SEL-a order

# The sealed 8-config grid (slot doc section 5, verbatim).
CONFIGS: tuple[dict[str, Any], ...] = (
    {"config_id": "JF-V1-H5", "variant": "V1", "latent_k": 8, "h": 5, "encoder": "pca-anchor"},
    {"config_id": "JF-V1-H21", "variant": "V1", "latent_k": 8, "h": 21, "encoder": "pca-anchor"},
    {"config_id": "JF-V2-H5", "variant": "V2", "latent_k": 16, "h": 5, "encoder": "pca-anchor"},
    {"config_id": "JF-V2-H21", "variant": "V2", "latent_k": 16, "h": 21, "encoder": "pca-anchor"},
    {"config_id": "JF-V3-H5", "variant": "V3", "latent_k": 8, "h": 5, "encoder": "vicreg-linear"},
    {"config_id": "JF-V3-H21", "variant": "V3", "latent_k": 8, "h": 21, "encoder": "vicreg-linear"},
    {"config_id": "JF-V4-H5", "variant": "V4", "latent_k": 8, "h": 5, "encoder": "pca-anchor"},
    {"config_id": "JF-V4-H21", "variant": "V4", "latent_k": 8, "h": 21, "encoder": "pca-anchor"},
)
CONFIG_IDS: tuple[str, ...] = tuple(c["config_id"] for c in CONFIGS)

# [PINNED] V4 sector map (slot doc 5, hand-declared, disclosed as not
# machine-verified; SOXX declared unused).
SECTOR_MAP: dict[str, str] = {
    "NVDA": "SMH",
    "AMD": "SMH",
    "AVGO": "SMH",
    "INTC": "SMH",
    "QCOM": "SMH",
    "XOM": "XLE",
    "LLY": "XLV",
    "UNH": "XLV",
    "JPM": "XLF",
    "V": "XLF",
    "MA": "XLF",
}
MARKET_TRIO: tuple[str, ...] = ("SPY", "QQQ", "IWM")
TRIO_ONLY: tuple[str, ...] = (
    "AAPL",
    "MSFT",
    "GOOGL",
    "AMZN",
    "META",
    "TSLA",
    "ADBE",
    "NFLX",
    "CRM",
    "PEP",
    "KO",
    "DIS",
    "COST",
    "HD",
    "PG",
)
SINGLES26: tuple[str, ...] = tuple(sorted(SECTOR_MAP) + list(TRIO_ONLY))
EQUITY_FEATURES: tuple[str, ...] = ("r1", "r5", "r21", "r63", "lnv", "lnrv20", "liq")
INDEX_FEATURES: tuple[str, ...] = ("vix_term", "vix9d_term", "dlnvix21", "vvix")
PINNED_INPUT_LABELS: tuple[str, ...] = (
    "artifacts/paper-trades/ohlc-panel.json",
    "artifacts/desk-store/indices/VIX.csv",
    "artifacts/desk-store/indices/VIX9D.csv",
    "artifacts/desk-store/indices/VIX3M.csv",
    "artifacts/desk-store/indices/VVIX.csv",
    "data/calendar/nyse_sessions_2018_01_02_2026_12_31.json",
)


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
    """A binding refusal: the sealed registration and the execution disagree."""


class VicregDiverged(Refused):
    """The PINNED V3 optimizer (full-batch plain GD, 2000 iters, lr 1e-2,
    PCG64(22) init x 1e-2) left finite arithmetic on the registered inputs.
    A configuration-level defect of the sealed constants -- recorded per
    trial (FAILED, no artifact); the slot doc assigns defective runs
    NOT_EVALUABLE by operator ruling only. Never tuned away: no lr change,
    no clipping, no re-init is permitted without a NEW registration."""

    checkpoints: dict[int, tuple[float, float, float]] = {}


# ---- sealed inputs -----------------------------------------------------------------------


@dataclass(frozen=True)
class Inputs:
    menu: Mapping[str, Any]
    menu_sha256: str
    slot: Mapping[str, Any]
    slot_doc_sha256: str
    protocol_raw_sha256: str
    protocol_canonical_sha256: str
    panel: Mapping[str, Mapping[str, Mapping[str, Any]]]
    panel_sha256: str
    calendar_sha256: str
    index_rows: dict[str, tuple[tuple[str, float], ...]]  # sym -> ((iso, close), ...)
    inputs_sha256: dict[str, str]
    dataset_manifest_hash: str
    grid: tuple[str, ...]  # the 1263 panel-union sessions (chain-35 union)
    ordinals: dict[str, int]
    chain35: tuple[str, ...]
    cutoff_iso: str
    refit_first_iso: tuple[str, ...]  # the 6 inner refit first sessions
    init_ord: int  # grid ordinal of the last initial-training session
    inner_ords: tuple[int, ...]  # grid ordinals of the 378 inner origins
    tnull_v3: Mapping[str, Any]


def _parse_calendar_sessions(raw: Mapping[str, Any]) -> tuple[str, ...]:
    sessions = raw.get("sessions")
    if not isinstance(sessions, list) or not all(isinstance(s, str) for s in sessions):
        raise Refused("the sealed calendar file carries no sessions list")
    return tuple(sessions)


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
    if slot is None or slot.get("order") != 5 or slot.get("family") != "JEPA-SURPRISE":
        raise Refused("the menu's order-5 JEPA-SURPRISE slot is missing or malformed")
    scopes = {sc["scope_id"]: sc for sc in slot["scope_ids"]}
    if scopes.get(SCOPE_ID, {}).get("config_count") != 8 or scopes[SCOPE_ID].get("cap") != 32:
        raise Refused(f"menu scope {SCOPE_ID} is not 8 configs under the 32 cap")
    if tuple(slot["config_ids"]) != CONFIG_IDS or slot.get("config_count") != 8:
        raise Refused("menu config ids are not the sealed eight in order")
    if tuple(slot["verdict_vocabulary"]) != ("PASS", "PASS-REDUNDANT", "FAIL", "NOT_EVALUABLE"):
        raise Refused("the slot's verdict vocabulary is not the closed four")

    slot_doc_sha256 = _sha256_file(SLOT_DOC_PATH)
    pinning = menu["dataset_pinning"]
    if pinning.get("docs/theory/campaign-2026-09/slots/jepa-filter.md") != slot_doc_sha256:
        raise Refused("the slot doc sha256 does not match the menu's pin -- not the sealed bytes")

    protocol_raw_sha256 = _sha256_file(PROTOCOL_PATH)
    if menu["protocol_hash"] != protocol_raw_sha256:
        raise Refused(
            "research_protocol.yaml raw sha256 does not match the menu's protocol_hash"
            " -- a protocol change requires a NEW registration"
        )
    protocol_canonical_sha256 = protocol_hash(default_protocol())

    panel, panel_sha256 = read_panel_with_sha256(PANEL_PATH)
    calendar_sha256 = _sha256_file(CALENDAR_PATH)
    index_rows: dict[str, tuple[tuple[str, float], ...]] = {}
    index_sha: dict[str, str] = {}
    for sym in ("VIX", "VIX9D", "VIX3M", "VVIX"):
        path = INDICES_DIR / f"{sym}.csv"
        sha = _sha256_file(path)
        label = f"artifacts/desk-store/indices/{sym}.csv"
        want = pinning.get(label)
        if want is None or want != sha:
            raise Refused(f"{label}: sha256 {sha} != the menu's pinned {want} -- swapped inputs refuse")
        index_sha[label] = sha
        rows = read_store(path)
        try:
            index_rows[sym] = tuple((r[0], float(r[4])) for r in rows)
        except ValueError as exc:
            raise Refused(f"{label}: a close value is not a finite decimal: {exc}") from None

    inputs_sha256 = {
        "ohlc-panel.json": panel_sha256,
        "VIX.csv": index_sha["artifacts/desk-store/indices/VIX.csv"],
        "VIX9D.csv": index_sha["artifacts/desk-store/indices/VIX9D.csv"],
        "VIX3M.csv": index_sha["artifacts/desk-store/indices/VIX3M.csv"],
        "VVIX.csv": index_sha["artifacts/desk-store/indices/VVIX.csv"],
        "nyse_sessions json": calendar_sha256,
    }
    for label in PINNED_INPUT_LABELS:
        if pinning.get(label) is None:
            raise Refused(f"the menu pins no hash for {label}")
    manifest_body = "".join(f"{label}\0{pinning[label]}\n" for label in PINNED_INPUT_LABELS)
    dataset_manifest_hash = hashlib.sha256(manifest_body.encode("utf-8")).hexdigest()

    # calendar: phantom removed from the session set is the panel grid's own
    # behavior; the grid below must simply not contain it.
    calendar_sessions = _parse_calendar_sessions(
        json.loads(CALENDAR_PATH.read_text(encoding="utf-8"))
    )
    if PHANTOM_ISO not in calendar_sessions:
        raise Refused("the sealed calendar no longer lists the phantom session -- inputs moved")

    # universes pinned to the panel itself
    from tree_options.desk.universe import CHAIN_UNIVERSE

    panel_names = sorted(panel)
    if len(panel_names) != 37:
        raise Refused(f"panel carries {len(panel_names)} names, expected the sealed 37")
    chain35 = tuple(n for n in panel_names if n not in ("TQQQ", "SQQQ"))
    if len(chain35) != 35:
        raise Refused("panel-minus-TQQQ/SQQQ is not the 35 chain-universe names")
    if not set(chain35) <= set(CHAIN_UNIVERSE):
        raise Refused("the panel chain-35 is not inside the desk's chain universe")
    if sorted(SINGLES26) != sorted(set(SINGLES26)) or len(SINGLES26) != 26:
        raise Refused("the pinned V4 roster is not the 26 single stocks")
    if not set(SINGLES26) <= set(chain35):
        raise Refused("the pinned V4 roster is not inside the chain-35 panel")
    etf_regressors = set(MARKET_TRIO) | set(SECTOR_MAP.values())
    if not etf_regressors <= set(chain35):
        raise Refused("a V4 residualization regressor ETF is missing from the chain-35 panel")
    if "SOXX" in etf_regressors:
        raise Refused("SOXX is declared unused; it must not be a residualization regressor")

    grid = tuple(sorted(set().union(*(set(panel[n]) for n in chain35))))
    if len(grid) != 1263 or grid[0] != INIT_START or grid[-1] != "2026-09-23":
        raise Refused(f"the chain-35 union grid moved: {len(grid)} sessions {grid[0]}..{grid[-1]}")
    if PHANTOM_ISO in grid:
        raise Refused("the panel-union grid contains the phantom 2025-01-09")
    ordinals = {s: i for i, s in enumerate(grid)}

    init_sessions = [s for s in grid if INIT_START <= s <= INIT_END]
    inner_sessions = [s for s in grid if INNER_START <= s <= INNER_END]
    if len(init_sessions) != EXPECTED_INIT_SESSIONS:
        raise Refused(
            f"initial training window is {len(init_sessions)} sessions, not the sealed 703"
        )
    if len(inner_sessions) != EXPECTED_INNER_SESSIONS:
        raise Refused(f"inner window is {len(inner_sessions)} sessions, not the sealed 378")

    last_by_name = {n: max(panel[n]) for n in chain35}
    cutoff_iso = min(last_by_name.values())

    refit_first_iso: list[str] = []
    for year, month in REFIT_QUARTERS:
        firsts = [s for s in grid if s.startswith(f"{year:04d}-{month:02d}")]
        if not firsts:
            raise Refused(f"no panel-union session in {year}-{month:02d} for the pinned refit")
        refit_first_iso.append(firsts[0])
    if refit_first_iso[0] != INNER_START:
        raise Refused("the first refit is not at the inner window's first session")

    # sequencing gate: family scoring is unfrozen only behind a CALIBRATED v3
    # null that binds THIS menu sha (menu rules.sequencing AMENDMENT v3).
    if not TNULL_V3_PATH.exists():
        raise Refused(f"{TNULL_V3_PATH} is missing -- the v3 null re-stamp gates family scoring")
    tnull_v3 = json.loads(TNULL_V3_PATH.read_text(encoding="utf-8"))
    tnull_stamp = tnull_v3.get("stamp", {})
    if tnull_stamp.get("registration_menu_sha256") != menu_sha256:
        raise Refused("the v3 null calibration does not bind this menu sha -- scoring stays frozen")
    if tnull_v3.get("verdict", {}).get("slot") != "CALIBRATED":
        raise Refused("the v3 null calibration is not CALIBRATED -- family scoring stays frozen")
    if tnull_stamp.get("cutoff_earliest_last_session_chain35") != cutoff_iso:
        raise Refused("the panel cutoff moved since the v3 null stamp -- the windows differ")

    return Inputs(
        menu=menu,
        menu_sha256=menu_sha256,
        slot=slot,
        slot_doc_sha256=slot_doc_sha256,
        protocol_raw_sha256=protocol_raw_sha256,
        protocol_canonical_sha256=protocol_canonical_sha256,
        panel=panel,
        panel_sha256=panel_sha256,
        calendar_sha256=calendar_sha256,
        index_rows=index_rows,
        inputs_sha256=inputs_sha256,
        dataset_manifest_hash=dataset_manifest_hash,
        grid=grid,
        ordinals=ordinals,
        chain35=chain35,
        cutoff_iso=cutoff_iso,
        refit_first_iso=tuple(refit_first_iso),
        init_ord=ordinals[init_sessions[-1]],
        inner_ords=tuple(ordinals[s] for s in inner_sessions),
        tnull_v3=tnull_v3,
    )


# ---- feature tables (pure functions of truncatable inputs) --------------------------------


@dataclass(frozen=True)
class FeatureTables:
    """Raw features on a grid; every computation looks backward only, so a
    grid truncated at u reproduces every value dated <= u bit-for-bit (the
    future-poison precondition)."""

    grid: tuple[str, ...]
    closes: dict[str, list[float | None]]
    eq_raw: dict[str, dict[str, list[float | None]]]  # name -> feat -> grid list
    idx_raw: dict[str, list[float | None]]  # feat -> grid list (lagged joins)


def _lagged_level(
    rows: Sequence[tuple[str, float]], grid: Sequence[str]
) -> list[float | None]:
    """L(i) = the last index observation dated on or before grid[i-1]
    (INV-02: the one-session lag, identical on challenger and incumbent)."""
    dates = [d for d, _v in rows]
    values = [v for _d, v in rows]
    out: list[float | None] = [None] * len(grid)
    for i in range(1, len(grid)):
        j = bisect.bisect_right(dates, grid[i - 1]) - 1
        if j >= 0:
            out[i] = values[j]
    return out


def build_feature_tables(
    panel: Mapping[str, Mapping[str, Mapping[str, Any]]],
    index_rows: Mapping[str, Sequence[tuple[str, float]]],
    grid: Sequence[str],
    names: Sequence[str],
) -> FeatureTables:
    closes: dict[str, list[float | None]] = {}
    eq_raw: dict[str, dict[str, list[float | None]]] = {}
    for name in names:
        bars = panel[name]
        cl: list[float | None] = []
        dv: list[float | None] = []
        for s in grid:
            bar = bars.get(s)
            if bar is None:
                cl.append(None)
                dv.append(None)
            else:
                c = float(bar["close"])
                v = float(bar["volume"])
                cl.append(c)
                dv.append(c * v if c > 0.0 and v > 0.0 else None)
        closes[name] = cl
        v_series = proxy_series(bars, list(grid))
        r1: list[float | None] = [None] * len(grid)
        r5 = list(r1)
        r21 = list(r1)
        r63 = list(r1)
        for i in range(len(grid)):
            for lst, n in ((r1, 1), (r5, 5), (r21, 21), (r63, 63)):
                if i >= n and cl[i] is not None and cl[i - n] is not None and cl[i - n] > 0:
                    lst[i] = math.log(cl[i] / cl[i - n])
        lnv: list[float | None] = [math.log(x) if x is not None and x > 0 else None for x in v_series]
        lnrv20: list[float | None] = [None] * len(grid)
        for i in range(len(grid)):
            tm = trailing_mean(v_series, i, 20)
            lnrv20[i] = math.log(tm) if tm is not None and tm > 0 else None
        liq: list[float | None] = [None] * len(grid)
        for i in range(len(grid)):
            tm = trailing_mean(dv, i, 62)
            if dv[i] is not None and tm is not None and tm > 0:
                liq[i] = math.log(dv[i]) - math.log(tm)
        eq_raw[name] = {
            "r1": r1,
            "r5": r5,
            "r21": r21,
            "r63": r63,
            "lnv": lnv,
            "lnrv20": lnrv20,
            "liq": liq,
        }

    lv_vix = _lagged_level(index_rows["VIX"], grid)
    lv_vix9d = _lagged_level(index_rows["VIX9D"], grid)
    lv_vix3m = _lagged_level(index_rows["VIX3M"], grid)
    lv_vvix = _lagged_level(index_rows["VVIX"], grid)
    idx_raw: dict[str, list[float | None]] = {
        "vix_term": [None] * len(grid),
        "vix9d_term": [None] * len(grid),
        "dlnvix21": [None] * len(grid),
        "vvix": list(lv_vvix),
    }
    for i in range(1, len(grid)):
        if lv_vix[i] is not None and lv_vix3m[i] is not None and lv_vix3m[i] > 0:
            idx_raw["vix_term"][i] = lv_vix[i] / lv_vix3m[i] - 1.0
        if lv_vix9d[i] is not None and lv_vix[i] is not None and lv_vix[i] > 0:
            idx_raw["vix9d_term"][i] = lv_vix9d[i] / lv_vix[i] - 1.0
        if i >= 22 and lv_vix[i] is not None and lv_vix[i - 21] is not None:
            if lv_vix[i] > 0 and lv_vix[i - 21] > 0:
                idx_raw["dlnvix21"][i] = math.log(lv_vix[i]) - math.log(lv_vix[i - 21])
    return FeatureTables(
        grid=tuple(grid),
        closes=closes,
        eq_raw=eq_raw,
        idx_raw=idx_raw,
    )


def equity_z(
    eq_raw: Mapping[str, Mapping[str, list[float | None]]],
    roster: Sequence[str],
    n_sessions: int,
) -> np.ndarray:
    """(R, T, 7) per-session cross-sectional z (population sd, ddof=0) over
    the roster names where the feature is present; NaN elsewhere."""
    arr = np.full((len(roster), n_sessions, len(EQUITY_FEATURES)), np.nan)
    for ni, name in enumerate(roster):
        for fi, feat in enumerate(EQUITY_FEATURES):
            col = eq_raw[name][feat]
            for i, x in enumerate(col):
                if x is not None:
                    arr[ni, i, fi] = x
    out = np.full_like(arr, np.nan)
    with warnings.catch_warnings(), np.errstate(invalid="ignore"):
        warnings.simplefilter("ignore", RuntimeWarning)  # all-NaN early columns are masked below
        for i in range(n_sessions):
            block = arr[:, i, :]
            mu = np.nanmean(block, axis=0)
            sd = np.nanstd(block, axis=0, ddof=0)
            present = ~np.isnan(block)
            z = (block - mu) / sd
            out[:, i, :] = np.where(present & (sd > 0), z, np.nan)
    return out


def index_z(
    idx_raw: Mapping[str, Sequence[float | None]],
    train_ords: Sequence[int],
    n_sessions: int,
) -> np.ndarray:
    """(T, 4) index block standardized by TRAINING-fold per-session moments
    (each training session counts once; a missing session is dropped)."""
    raw = np.full((n_sessions, len(INDEX_FEATURES)), np.nan)
    for fi, feat in enumerate(INDEX_FEATURES):
        col = idx_raw[feat]
        for i, x in enumerate(col):
            if x is not None:
                raw[i, fi] = x
    tr = raw[list(train_ords)]
    mu = np.nanmean(tr, axis=0)
    sd = np.nanstd(tr, axis=0, ddof=0)
    z = (raw - mu) / sd
    z[:, sd <= 0] = np.nan
    z[np.isnan(raw)] = np.nan
    return z


# ---- V4 residualization (per-vintage OLS, applied walk-forward) ---------------------------


def residualize_v4(
    eq_raw: Mapping[str, Mapping[str, list[float | None]]],
    train_ords: Sequence[int],
    n_sessions: int,
) -> np.ndarray:
    """(26, T, 7): each single's equity feature replaced by the residual of a
    training-fold OLS on {1, SPY, QQQ, IWM, mapped sector ETF}; coefficients
    per refit, applied at every session; a session with a missing regressor
    leaves the residual undefined (NaN)."""
    tset = set(train_ords)
    out = np.full((len(SINGLES26), n_sessions, len(EQUITY_FEATURES)), np.nan)
    for fi, feat in enumerate(EQUITY_FEATURES):
        reg = {etf: np.array(
            [np.nan if eq_raw[etf][feat][i] is None else eq_raw[etf][feat][i] for i in range(n_sessions)]
        ) for etf in MARKET_TRIO}
        for ni, name in enumerate(SINGLES26):
            sector = SECTOR_MAP.get(name)
            cols = [reg[e] for e in MARKET_TRIO]
            if sector is not None:
                cols.append(np.array(
                    [
                        np.nan if eq_raw[sector][feat][i] is None else eq_raw[sector][feat][i]
                        for i in range(n_sessions)
                    ]
                ))
            y = np.array(
                [np.nan if eq_raw[name][feat][i] is None else eq_raw[name][feat][i]
                 for i in range(n_sessions)]
            )
            X = np.column_stack([np.ones(n_sessions)] + cols)
            mask = np.isfinite(X).all(axis=1) & np.isfinite(y)
            tr_mask = np.zeros(n_sessions, dtype=bool)
            tr_mask[list(tset)] = True
            fit_mask = mask & tr_mask
            if fit_mask.sum() < len(cols) + 2:
                raise Refused(
                    f"V4 residualization for {name}/{feat}: {int(fit_mask.sum())} training rows"
                    " is underdetermined -- inputs moved"
                )
            beta, *_ = np.linalg.lstsq(X[fit_mask], y[fit_mask], rcond=None)
            resid = y - X @ beta
            out[ni, :, fi] = np.where(mask, resid, np.nan)
    # the residualized features then take the same cross-sectional z over the
    # 26 single stocks (population sd per session)
    z = np.full_like(out, np.nan)
    with warnings.catch_warnings(), np.errstate(invalid="ignore"):
        warnings.simplefilter("ignore", RuntimeWarning)
        for i in range(n_sessions):
            block = out[:, i, :]
            mu = np.nanmean(block, axis=0)
            sd = np.nanstd(block, axis=0, ddof=0)
            present = np.isfinite(block)
            zz = (block - mu) / sd
            z[:, i, :] = np.where(present & (sd > 0), zz, np.nan)
    return z


# ---- statistics (plain numpy, deterministic) ----------------------------------------------


def _rankdata(a: np.ndarray) -> np.ndarray:
    order = np.argsort(a, kind="stable")
    sorted_a = a[order]
    ranks = np.empty(len(a), dtype=float)
    i = 0
    n = len(a)
    while i < n:
        j = i
        while j + 1 < n and sorted_a[j + 1] == sorted_a[i]:
            j += 1
        ranks[order[i : j + 1]] = 0.5 * (i + j) + 1.0
        i = j + 1
    return ranks


def spearman(x: np.ndarray, y: np.ndarray) -> float | None:
    rx = _rankdata(x)
    ry = _rankdata(y)
    rx = rx - rx.mean()
    ry = ry - ry.mean()
    denom = math.sqrt(float((rx * rx).sum() * (ry * ry).sum()))
    if denom <= 0.0:
        return None
    return float((rx * ry).sum() / denom)


def nw_t(series: Sequence[float], lag: int) -> float | None:
    """Newey-West (Bartlett, lag h) t on the mean; reported beside, never the
    sole authority."""
    x = np.asarray(series, dtype=float)
    n = len(x)
    if n < 2:
        return None
    m = float(x.mean())
    s = float(((x - m) ** 2).mean())
    for l in range(1, min(lag, n - 1) + 1):
        gl = float(((x[l:] - m) * (x[:-l] - m)).mean())
        s += 2.0 * (1.0 - l / (lag + 1.0)) * gl
    if s <= 0.0:
        return None
    return m / math.sqrt(s / n)


# ---- VICReg-linear (V3): plain numpy, deterministic, auditable ----------------------------


def vicreg_fit(X_all: np.ndarray, X_prev: np.ndarray, X_cur: np.ndarray, k: int) -> tuple[np.ndarray, np.ndarray]:
    """Full-batch plain GD on
    mean||X_prev W A - X_cur W||^2 + lv sum_j max(0,1-sd_j)^2 + lc sum_{i!=j} C_ij^2
    with population sd/covariance over ALL training rows; init W, A from
    Generator(PCG64(22)) standard-normal x 1e-2 (W first, then A)."""
    rng = np.random.Generator(np.random.PCG64(PCG_SEED))
    d = X_all.shape[1]
    W = rng.standard_normal((d, k)) * 1e-2
    A = rng.standard_normal((k, k)) * 1e-2
    n = X_all.shape[0]
    m = X_prev.shape[0]
    checkpoints: dict[int, tuple[float, float, float]] = {}
    for it in range(GD_ITERS):
        Z = X_all @ W
        Zp = X_prev @ W
        Zc = X_cur @ W
        D = Zp @ A - Zc
        mu = Z.mean(axis=0)
        Y = Z - mu
        C = (Y.T @ Y) / n
        var = np.diag(C).copy()
        sd = np.sqrt(np.maximum(var, 0.0))
        hinge = np.maximum(0.0, 1.0 - sd)
        if it % 100 == 0 or it == GD_ITERS - 1:
            loss = (
                float((D * D).sum(axis=1).mean())
                + LAMBDA_V * float((np.maximum(0.0, 1.0 - sd) ** 2).sum())
                + LAMBDA_C * float(((C - np.diag(np.diag(C))) ** 2).sum())
            )
            checkpoints[it] = (
                round(loss, 6),
                round(float(np.abs(W).max()), 6),
                round(float(np.abs(A).max()), 6),
            )
        # dL/dA, dL/dW(pred)
        G = (2.0 / m) * D
        gA = Zp.T @ G
        gW_pred = X_prev.T @ (G @ A.T) - X_cur.T @ G
        # var/cov terms: dL_var/dvar_j = -lambda_v * hinge_j / sd_j
        sd_safe = np.maximum(sd, 1e-8)
        q = -LAMBDA_V * hinge / sd_safe
        gC = 2.0 * LAMBDA_C * (C - np.diag(np.diag(C)))
        gC[np.diag_indices(k)] += q
        gZ = (2.0 / n) * (Y @ gC)
        gZ -= (2.0 / n) * np.outer(np.ones(n), (Y @ gC).mean(axis=0))
        gW_moments = X_all.T @ gZ
        gW = gW_pred + gW_moments
        W = W - GD_LR * gW
        A = A - GD_LR * gA
        if not (np.isfinite(W).all() and np.isfinite(A).all()):
            exc = VicregDiverged(
                f"VICReg-linear GD left finite arithmetic at iteration {it}"
                f" (last checkpoint {checkpoints.get(max(checkpoints), ())});"
                " pinned constants (GD 2000 iters, lr 1e-2, PCG64(22) init x 1e-2)"
                " are the registration's and are NOT tunable"
            )
            exc.checkpoints = checkpoints
            raise exc
    return W, A


# ---- vintage fit -------------------------------------------------------------------------


@dataclass
class Vintage:
    quarter: str
    first_iso: str
    first_ord: int
    train_ords: tuple[int, ...]
    n_train_rows: int
    latent: np.ndarray  # (R_roster, T, k_eff) with NaN where state incomplete
    k_eff: int
    ridge_a: np.ndarray | None  # zhat = a * z + b (per dim)
    ridge_b: np.ndarray | None
    vicreg_A: np.ndarray | None
    surprise_sd: float
    train_latent_var: np.ndarray  # population variance per dim over training rows
    index_mu: np.ndarray
    index_sd: np.ndarray


def _state_tensor(
    ft: FeatureTables,
    roster: Sequence[str],
    variant: str,
    train_ords: Sequence[int],
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(S (R,T,11) NaN-incomplete, index_mu, index_sd)."""
    n_sessions = len(ft.grid)
    if variant in ("V1", "V2", "V3"):
        eq = equity_z(ft.eq_raw, roster, n_sessions)
    elif variant == "V4":
        eq = residualize_v4(ft.eq_raw, train_ords, n_sessions)
    else:
        raise Refused(f"unknown variant {variant}")
    raw_idx = np.full((n_sessions, len(INDEX_FEATURES)), np.nan)
    for fi, feat in enumerate(INDEX_FEATURES):
        col = ft.idx_raw[feat]
        for i, x in enumerate(col):
            if x is not None:
                raw_idx[i, fi] = x
    tr_idx = raw_idx[list(train_ords)]
    mu = np.nanmean(tr_idx, axis=0)
    sd = np.nanstd(tr_idx, axis=0, ddof=0)
    zidx = (raw_idx - mu) / sd
    zidx[:, sd <= 0] = np.nan
    S = np.concatenate([eq, np.broadcast_to(zidx, (len(roster), n_sessions, len(INDEX_FEATURES)))], axis=2)
    return S, mu, sd


def fit_vintage(
    ft: FeatureTables,
    roster: Sequence[str],
    cfg: Mapping[str, Any],
    quarter: str,
    first_iso: str,
    first_ord: int,
) -> Vintage:
    h = int(cfg["h"])
    n_sessions = len(ft.grid)
    train_ords = tuple(o for o in range(n_sessions) if o + h + EMBARGO < first_ord)
    S, imu, isd = _state_tensor(ft, roster, cfg["variant"], train_ords)
    complete = np.isfinite(S).all(axis=2)  # (R, T)
    n_train_rows = int(complete[:, list(train_ords)].sum())
    if n_train_rows < 252:
        raise Refused(
            f"vintage {quarter}: {n_train_rows} complete training state rows (< min_train 252)"
            " -- inputs moved"
        )
    if cfg["encoder"] == "pca-anchor":
        rows = S[:, list(train_ords)][complete[:, list(train_ords)]]
        mu = rows.mean(axis=0)
        Xc = rows - mu
        cov = (Xc.T @ Xc) / len(rows)
        eigval, eigvec = np.linalg.eigh(cov)
        k_eff = min(int(cfg["latent_k"]), S.shape[2])
        order = np.argsort(eigval)[::-1][:k_eff]
        V = eigvec[:, order]
        for j in range(k_eff):  # signs fixed by largest-abs loading
            m = int(np.argmax(np.abs(V[:, j])))
            if V[m, j] < 0:
                V[:, j] = -V[:, j]
        flat = np.full(S.shape[:2] + (k_eff,), np.nan)
        proj = (S - mu) @ V
        flat[complete] = proj[complete]
        ridge_a = np.zeros(k_eff)
        ridge_b = np.zeros(k_eff)
        tset = set(train_ords)
        for j in range(k_eff):
            xs: list[float] = []
            ys: list[float] = []
            for o in train_ords:
                o2 = o + h
                if o2 >= n_sessions or o2 not in tset:
                    continue
                ok = complete[:, o] & complete[:, o2]
                if not ok.any():
                    continue
                zv = flat[ok, o, j]
                zu = flat[ok, o2, j]
                xs.extend(zv.tolist())
                ys.extend(zu.tolist())
            if len(xs) < 10:
                raise Refused(f"vintage {quarter}: dim {j} has {len(xs)} ridge pairs -- inputs moved")
            x = np.asarray(xs)
            y = np.asarray(ys)
            sx = float(x.sum())
            sxx = float((x * x).sum())
            sy = float(y.sum())
            sxy = float((x * y).sum())
            n = len(xs)
            det = n * (sxx + RIDGE_ALPHA) - sx * sx
            if det == 0:
                raise Refused(f"vintage {quarter}: ridge system singular at dim {j}")
            b = (sy * (sxx + RIDGE_ALPHA) - sx * sxy) / det
            a = (n * sxy - sx * sy) / det
            ridge_a[j] = a
            ridge_b[j] = b
        latent = flat
        vicreg_A = None
    else:  # vicreg-linear
        k_eff = int(cfg["latent_k"])
        X_all = S[:, list(train_ords)][complete[:, list(train_ords)]]
        # pairs (t -> t+h) inside the training fold, same name
        prev_rows: list[np.ndarray] = []
        cur_rows: list[np.ndarray] = []
        tset = set(train_ords)
        for o in train_ords:
            o2 = o + h
            if o2 >= n_sessions or o2 not in tset:
                continue
            ok = complete[:, o] & complete[:, o2]
            if ok.any():
                prev_rows.append(S[ok, o, :])
                cur_rows.append(S[ok, o2, :])
        X_prev = np.vstack(prev_rows)
        X_cur = np.vstack(cur_rows)
        W, A = vicreg_fit(X_all, X_prev, X_cur, k_eff)
        latent = np.full(S.shape[:2] + (k_eff,), np.nan)
        proj = S @ W
        latent[complete] = proj[complete]
        ridge_a = ridge_b = None
        vicreg_A = A

    train_latent_var = np.nanvar(latent[:, list(train_ords)].reshape(-1, k_eff), axis=0, ddof=0)

    # training-fold pooled SD of s (ddof=1) for the surprise standardization
    s_pool: list[float] = []
    tset = set(train_ords)
    for o in train_ords:
        o2 = o + h
        if o2 >= n_sessions or o2 not in tset:
            continue
        ok = complete[:, o] & complete[:, o2]
        if not ok.any():
            continue
        if ridge_a is not None:
            pred = flat[ok, o, :] * ridge_a + ridge_b
        else:
            pred = latent[ok, o, :] @ vicreg_A
        s_pool.extend(np.linalg.norm(pred - latent[ok, o2, :], axis=1).tolist())
    if len(s_pool) < 100:
        raise Refused(f"vintage {quarter}: {len(s_pool)} pooled training surprises -- inputs moved")
    if not all(math.isfinite(x) for x in s_pool):
        raise VicregDiverged(
            f"vintage {quarter}: training-fold pooled surprise carries non-finite"
            " values (encoder diverged under the pinned constants)"
        )
    surprise_sd = statistics.stdev(s_pool)  # ddof=1

    return Vintage(
        quarter=quarter,
        first_iso=first_iso,
        first_ord=first_ord,
        train_ords=train_ords,
        n_train_rows=n_train_rows,
        latent=latent,
        k_eff=k_eff,
        ridge_a=ridge_a,
        ridge_b=ridge_b,
        vicreg_A=vicreg_A,
        surprise_sd=surprise_sd,
        train_latent_var=train_latent_var,
        index_mu=imu,
        index_sd=isd,
    )


def surprise_at(vin: Vintage, roster_idx: Sequence[int], o_prev: int, o_cur: int) -> np.ndarray:
    """s (NaN where incomplete) for the given roster positions, from the
    vintage's own latents: ||g(z_{u-h}) - z_u||."""
    out = np.full(len(roster_idx), np.nan)
    for r, ni in enumerate(roster_idx):
        zp = vin.latent[ni, o_prev]
        zc = vin.latent[ni, o_cur]
        if not (np.isfinite(zp).all() and np.isfinite(zc).all()):
            continue
        if vin.ridge_a is not None:
            pred = zp * vin.ridge_a + vin.ridge_b
        else:
            pred = zp @ vin.vicreg_A
        out[r] = float(np.linalg.norm(pred - zc))
    return out


# ---- inner-fold run for one config ---------------------------------------------------------


@dataclass
class ConfigResult:
    payload: dict[str, Any]
    sampled_surprises: dict[str, dict[str, float]]  # origin_iso -> name -> raw s
    defective_reasons: list[str] = field(default_factory=list)
    not_evaluable_reasons: list[str] = field(default_factory=list)


def run_config(inputs: Inputs, cfg: Mapping[str, Any], ft: FeatureTables) -> ConfigResult:
    h = int(cfg["h"])
    variant = str(cfg["variant"])
    roster: tuple[str, ...] = SINGLES26 if variant == "V4" else inputs.chain35
    n_sessions = len(ft.grid)

    # labels and inner-plane baselines (vintage-independent, backward-looking)
    label = np.full((len(roster), n_sessions), np.nan)
    xsmom = np.full((len(roster), n_sessions), np.nan)
    lnrv21 = np.full((len(roster), n_sessions), np.nan)
    absr21 = np.full((len(roster), n_sessions), np.nan)
    for ni, name in enumerate(roster):
        cl = ft.closes[name]
        for i in range(n_sessions):
            if i + h < n_sessions and cl[i] is not None and cl[i + h] is not None:
                if cl[i] > 0 and cl[i + h] > 0:
                    label[ni, i] = math.log(cl[i + h] / cl[i])
            if i >= XSMOM_LOOKBACK and cl[i] is not None and cl[i - XSMOM_LOOKBACK] is not None:
                if cl[i - XSMOM_LOOKBACK] > 0:
                    xsmom[ni, i] = cl[i] / cl[i - XSMOM_LOOKBACK] - 1.0
            x = ft.eq_raw[name]["lnrv20"][i]
            if x is not None:
                lnrv21[ni, i] = x
            x = ft.eq_raw[name]["r21"][i]
            if x is not None:
                absr21[ni, i] = abs(x)

    vintages: list[Vintage] = []
    for (year, month), first_iso in zip(REFIT_QUARTERS, inputs.refit_first_iso, strict=True):
        vintages.append(
            fit_vintage(ft, roster, cfg, f"{year:04d}-{month:02d}", first_iso, inputs.ordinals[first_iso])
        )

    def vintage_in_force(ord_prev: int) -> Vintage | None:
        vin = None
        for v in vintages:
            if v.first_ord <= ord_prev:
                vin = v
        return vin

    rows: list[dict[str, Any]] = []
    seg_ic: dict[str, list[float]] = {}
    seg_eval_cells: dict[str, np.ndarray] = {}
    sampled_surprises: dict[str, dict[str, float]] = {}
    origins_no_vintage = 0
    origins_skipped_breadth = 0
    origins_degenerate = 0
    origins_scored = 0
    sampled_segments: set[str] = set()

    for iu in inputs.inner_ords:
        u_iso = ft.grid[iu]
        vin = vintage_in_force(iu - h)
        if vin is None:
            origins_no_vintage += 1
            continue
        s_raw = surprise_at(vin, list(range(len(roster))), iu - h, iu)
        ok = np.isfinite(s_raw) & np.isfinite(label[:, iu])
        if int(ok.sum()) < BREADTH_MIN_NAMES:
            origins_skipped_breadth += 1
            continue
        s_std = s_raw[ok] / vin.surprise_sd
        r = label[ok, iu]
        ic = spearman(-s_std, r)
        if ic is None:
            origins_degenerate += 1
            continue
        origins_scored += 1
        rows.append(
            {
                "u": u_iso,
                "vintage": vin.quarter,
                "ic": ic,
                "n": int(ok.sum()),
            }
        )
        seg_ic.setdefault(vin.quarter, []).append(ic)
        cells = vin.latent[:, iu][np.isfinite(s_raw)]
        if vin.quarter in seg_eval_cells:
            seg_eval_cells[vin.quarter] = np.vstack([seg_eval_cells[vin.quarter], cells])
        else:
            seg_eval_cells[vin.quarter] = cells
        # baselines on the identical origin/name cells (disclosures only)
        sel_x = xsmom[ok, iu]
        sel_l = lnrv21[ok, iu]
        sel_a = absr21[ok, iu]
        fin_x = np.isfinite(sel_x)
        fin_l = np.isfinite(sel_l)
        fin_a = np.isfinite(sel_a)
        rows[-1]["ic_xsmom"] = spearman(sel_x[fin_x], r[fin_x]) if fin_x.sum() >= BREADTH_MIN_NAMES else None
        rows[-1]["ic_lnrv21_control"] = (
            spearman(-sel_l[fin_l], r[fin_l]) if fin_l.sum() >= BREADTH_MIN_NAMES else None
        )
        rows[-1]["ic_absr21_control"] = (
            spearman(-sel_a[fin_a], r[fin_a]) if fin_a.sum() >= BREADTH_MIN_NAMES else None
        )
        # future-poison sample: the FIRST scored origin of each vintage segment
        if vin.quarter not in sampled_segments:
            sampled_segments.add(vin.quarter)
            sampled_surprises[u_iso] = {
                roster[r_]: float(s_raw[r_]) for r_ in np.nonzero(ok)[0]
            }

    if origins_scored == 0:
        raise Refused(
            f"{cfg['config_id']}: 0 scored inner origins -- runner machinery defect, no artifact"
        )

    ics = [row["ic"] for row in rows]
    mean_ic = statistics.fmean(ics)
    ics_x = [row["ic_xsmom"] for row in rows if row.get("ic_xsmom") is not None]
    ics_l = [row["ic_lnrv21_control"] for row in rows if row.get("ic_lnrv21_control") is not None]
    ics_a = [row["ic_absr21_control"] for row in rows if row.get("ic_absr21_control") is not None]

    not_evaluable: list[str] = []
    if variant == "V3":
        for v in vintages:
            cells = seg_eval_cells.get(v.quarter)
            if cells is None or len(cells) == 0:
                continue
            eval_var = np.var(cells, axis=0, ddof=0)
            tripped = [
                int(j)
                for j in range(v.k_eff)
                if float(eval_var[j]) < V3_COLLAPSE_RATIO * float(v.train_latent_var[j])
            ]
            if tripped:
                not_evaluable.append(
                    f"collapse tripwire: vintage {v.quarter} eval-fold latent variance"
                    f" < {V3_COLLAPSE_RATIO} x train-fold on dims {tripped}"
                )

    total_decided = origins_scored + origins_skipped_breadth + origins_degenerate
    skip_frac = origins_skipped_breadth / total_decided if total_decided else 0.0
    defective: list[str] = []
    if skip_frac > SKIP_DEFECT_FRAC:
        defective.append(
            f"skipped-origin defect: {origins_skipped_breadth}/{total_decided}"
            f" ({skip_frac:.1%}) inner origins below the {BREADTH_MIN_NAMES}-name breadth floor"
        )

    payload = {
        "config": {
            "config_id": cfg["config_id"],
            "variant": variant,
            "latent_k_declared": int(cfg["latent_k"]),
            "latent_k_effective": vintages[0].k_eff,
            "k_cap_note": (
                "d=11 caps PCA at 11 components; k=16 declared -> k_eff=11"
                " (zero-padding the extra dims is identity; disclosed)"
                if variant == "V2"
                else None
            ),
            "h": h,
            "roster": f"chain-35 ({len(roster)})" if variant != "V4" else f"26 singles ({len(roster)})",
            "direction_convention": "ic = Spearman(-std_surprise, forward_return); mean > 0 is the declared direction",
        },
        "geometry": {
            "refits": [
                {"quarter": v.quarter, "first_session": v.first_iso, "train_rows": v.n_train_rows}
                for v in vintages
            ],
            "inner_span": [INNER_START, INNER_END],
            "origins_in_window": len(inputs.inner_ords),
            "origins_without_vintage": origins_no_vintage,
            "origins_scored": origins_scored,
            "origins_skipped_breadth": origins_skipped_breadth,
            "origins_degenerate_ic": origins_degenerate,
            "skipped_breadth_fraction": skip_frac,
            "purge_rule": f"ordinal(s) + h + 5 < ordinal(first session of Q), h = {h}",
        },
        "summary": {
            "mean_ic": mean_ic,
            "ic_sd": statistics.stdev(ics),
            "nw_t_lag_h": nw_t(ics, h),
            "mean_ic_by_segment": {q: statistics.fmean(v) for q, v in sorted(seg_ic.items())},
            "n_by_segment": {q: len(v) for q, v in sorted(seg_ic.items())},
            "baseline_disclosures": {
                "xsmom_score_mean_ic": statistics.fmean(ics_x) if ics_x else None,
                "lnrv21_control_mean_ic": statistics.fmean(ics_l) if ics_l else None,
                "absr21_control_mean_ic": statistics.fmean(ics_a) if ics_a else None,
                "note": "identical origin/name cells; long-low convention for the controls; disclosed, never verdicts",
            },
        },
        "surprise_sd_by_vintage": {v.quarter: v.surprise_sd for v in vintages},
        "rows": rows,
        "not_evaluable_reasons": not_evaluable,
        "defective_reasons": defective,
    }
    return ConfigResult(
        payload=payload,
        sampled_surprises=sampled_surprises,
        defective_reasons=defective,
        not_evaluable_reasons=not_evaluable,
    )


# ---- future-poison test --------------------------------------------------------------------


def run_poison(
    inputs: Inputs,
    cfg: Mapping[str, Any],
    sampled: Mapping[str, Mapping[str, float]],
) -> list[dict[str, Any]]:
    """Recompute s_{i,u} for the sampled origins from inputs truncated at u
    (all panel bars and index rows dated after u dropped); the value must not
    move (max abs diff <= 1e-12). A failure is NOT_EVALUABLE."""
    h = int(cfg["h"])
    variant = str(cfg["variant"])
    roster: tuple[str, ...] = SINGLES26 if variant == "V4" else inputs.chain35
    out: list[dict[str, Any]] = []
    for u_iso in sorted(sampled):
        iu = inputs.ordinals[u_iso]
        vin_quarter_targets = [
            (idx, v)
            for idx, v in zip(REFIT_QUARTERS, inputs.refit_first_iso, strict=True)
            if inputs.ordinals[v] <= iu - h
        ]
        quarter_iso, first_iso = vin_quarter_targets[-1]
        quarter = f"{quarter_iso[0]:04d}-{quarter_iso[1]:02d}"
        grid_t = [s for s in inputs.grid if s <= u_iso]
        bars_t = {
            n: {s: b for s, b in inputs.panel[n].items() if s <= u_iso}
            for n in set(inputs.chain35) | set(roster)  # ETFs included for V4 regressors
        }
        idx_t = {
            sym: tuple((d, v) for d, v in rows if d <= u_iso)
            for sym, rows in inputs.index_rows.items()
        }
        ft_t = build_feature_tables(bars_t, idx_t, grid_t, tuple(sorted(set(inputs.chain35) | set(roster))))
        vin_t = fit_vintage(ft_t, roster, cfg, quarter, first_iso, inputs.ordinals[first_iso])
        s_t = surprise_at(vin_t, list(range(len(roster))), iu - h, iu)
        max_diff = 0.0
        finite = True
        for ni, name in enumerate(roster):
            want = sampled[u_iso].get(name)
            if want is None:
                continue
            got = float(s_t[ni])
            if not math.isfinite(got):
                finite = False
                continue
            max_diff = max(max_diff, abs(got - want))
        out.append(
            {
                "u": u_iso,
                "vintage": quarter,
                "n_names_compared": len(sampled[u_iso]),
                "max_abs_diff": max_diff,
                "all_finite": finite,
                "pass": finite and max_diff <= POISON_TOL,
            }
        )
    return out


# ---- stamps, registry, artifacts -----------------------------------------------------------


def _trial_id(config_id: str) -> str:
    return f"{SCOPE_ID}-{config_id}"


def _trial_artifact_path(trial_id: str) -> Path:
    return TRIALS_DIR / f"{trial_id}.json"


def _stamp(inputs: Inputs, cfg: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "program": "campaign-2026-09",
        "slot_id": SLOT_ID,
        "scope_id": SCOPE_ID,
        "round": ROUND,
        "round_scope": "inner-folds-only (the sealed outer windows are never scored here)",
        "trial_id": _trial_id(cfg["config_id"]),
        "config_id": cfg["config_id"],
        "registration_menu_sha256": inputs.menu_sha256,
        "slot_doc_sha256": inputs.slot_doc_sha256,
        "protocol_raw_sha256": inputs.protocol_raw_sha256,
        "protocol_canonical_sha256": inputs.protocol_canonical_sha256,
        "dataset_manifest_hash": inputs.dataset_manifest_hash,
        "inputs_sha256": dict(inputs.inputs_sha256),
        "cutoff_earliest_last_session_chain35": inputs.cutoff_iso,
        "tnull_calibration_v3": {
            "path": str(TNULL_V3_PATH),
            "verdict": inputs.tnull_v3.get("verdict", {}).get("slot"),
            "registration_menu_sha256": inputs.tnull_v3.get("stamp", {}).get("registration_menu_sha256"),
        },
        "git_sha": _git_head(REPO_ROOT),
        "runner_sha256": _sha256_file(Path(__file__).resolve()),
        "generated_at": _utcnow().isoformat(),
    }


def _hyperparameters(inputs: Inputs, cfg: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "scope_id": SCOPE_ID,
        "slot_id": SLOT_ID,
        "config_id": cfg["config_id"],
        "variant": cfg["variant"],
        "encoder": cfg["encoder"],
        "latent_k": int(cfg["latent_k"]),
        "h": int(cfg["h"]),
        "model_family": MODEL_FAMILY,
        "round": ROUND,
        "round_scope": "inner folds only",
        "grid": "panel-union chain-35 sessions (2025-01-09 phantom absent by construction)",
        "initial_train": [INIT_START, INIT_END],
        "inner_origins": [INNER_START, INNER_END],
        "refit_quarters": [f"{y:04d}-{m:02d}" for y, m in REFIT_QUARTERS],
        "refit_first_sessions": list(inputs.refit_first_iso),
        "purge_rule": "ordinal(s) + h + 5 < ordinal(first session of Q)",
        "roster": SINGLES26 if cfg["variant"] == "V4" else "chain-35",
        "v4_sector_map": {k: SECTOR_MAP[k] for k in sorted(SECTOR_MAP)},
        "v4_trio_only": list(TRIO_ONLY),
        "v4_regressors": sorted(set(MARKET_TRIO) | set(SECTOR_MAP.values())),
        "ridge_alpha": RIDGE_ALPHA,
        "lambda_v": LAMBDA_V,
        "lambda_c": LAMBDA_C,
        "gd_iters": GD_ITERS,
        "gd_lr": GD_LR,
        "pcg_seed": PCG_SEED,
        "v3_collapse_ratio": V3_COLLAPSE_RATIO,
        "breadth_min_names": BREADTH_MIN_NAMES,
        "skip_defect_frac": SKIP_DEFECT_FRAC,
        "index_lag": "one panel-union session (INV-02), challenger and incumbent identical",
        "surprise_convention": "s_{i,u} = ||g^v(z^v_{i,u-h}) - f^v(state_{i,u})||, v in force at u-h, / training-fold pooled sd (ddof=1)",
        "direction_convention": "long LOW surprise / short HIGH surprise: ic = Spearman(-std_s, r)",
        "label_convention": "r = ln(C_{u+h}/C_u), complete endpoint windows",
        "equity_z": "per-session cross-sectional z over the roster, population sd",
        "index_z": "training-fold per-session moments, per refit",
        "selection_rules": {
            "SEL-a": "per h: highest mean inner IC among {V1,V2,V3,V4} evaluable, requiring mean > 0; tie-break V1,V2,V4,V3; none positive -> (a) FAIL on inner evidence",
            "SEL-b": "JF-V1-H21 fixed, independent of SEL-a",
            "FLIP": "a direction flip at inner folds consumes a variant slot via a NEW registration; never post hoc on the same run",
        },
        "baselines_disclosed": ["xsmom_score", "lnrv21_control", "absr21_control"],
        "inputs_sha256": dict(inputs.inputs_sha256),
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
        outer_fold_id=f"campaign-2026-09/{SCOPE_ID}/inner-{INNER_START}_{INNER_END}",
        target_horizon="h5-h21",
        feature_set_id="ohlc-panel+cboe-indices|jepa-state-11f|v1",
        model_family=MODEL_FAMILY,
    )


def _open_registry() -> TrialRegistry:
    CAMPAIGN_DIR.mkdir(parents=True, exist_ok=True)
    return TrialRegistry(REGISTRY_PATH)


def _menu_slot(inputs: Inputs) -> Mapping[str, Any]:
    slot = next(s for s in inputs.menu["slots"] if s.get("slot_id") == SLOT_ID)
    if slot.get("order") != 5:
        raise Refused("the jepa-filter slot is not the menu's order-5 slot")
    return slot


# -- phases ----------------------------------------------------------------------------------


def phase_plan() -> int:
    inputs = load_and_bind()
    b_table = inputs.tnull_v3.get("baseline", {}).get("B", {})
    print(f"menu sha256 {inputs.menu_sha256} (sidecar-verified); slot doc {inputs.slot_doc_sha256[:16]}...")
    print(f"protocol raw {inputs.protocol_raw_sha256[:16]}... canonical {inputs.protocol_canonical_sha256[:16]}...")
    print(f"cutoff (earliest last session, chain35): {inputs.cutoff_iso}")
    print(f"grid: {len(inputs.grid)} panel-union sessions {inputs.grid[0]}..{inputs.grid[-1]}")
    print(
        f"initial train {INIT_START}..{INIT_END} = 703 sessions (refused if not);"
        f" inner origins {INNER_START}..{INNER_END} = {len(inputs.inner_ords)} sessions"
    )
    print(f"refits (first session): {list(inputs.refit_first_iso)}")
    print(
        "outer span (NEVER scored in round 1):"
        f" {OUTER_START}..cutoff-h = {inputs.grid[inputs.ordinals[inputs.cutoff_iso] - 21]}"
        f" (h=21) / {inputs.grid[inputs.ordinals[inputs.cutoff_iso] - 5]} (h=5)"
    )
    print(f"roster: chain-35 = {len(inputs.chain35)}; V4 singles = {len(SINGLES26)}")
    print(f"tnull v3: {inputs.tnull_v3['verdict']['slot']} (binds this menu sha)")
    for shape in ("xsmom", "event"):
        row = b_table.get(shape, {}).get("jepa-outer", {})
        if row:
            print(f"B[{shape}][jepa-outer] = {row.get('B_net_per_trade_mean')} (disclosure only; jepa criteria are rank-relative)")
    ne_windows = sorted({c.get("window") for c in inputs.tnull_v3.get("not_evaluable_cells", [])})
    print(f"null NOT_EVALUABLE windows (do not gate jepa; disclosed): {ne_windows}")
    print(f"registry db: {REGISTRY_PATH}; artifacts: {JEPA_DIR}")
    return 0


def phase_register() -> int:
    inputs = load_and_bind()
    scope = _scope(inputs)
    registry = _open_registry()
    try:
        existing = [c for c in CONFIGS if registry.is_registered(_trial_id(c["config_id"]))]
        if existing:
            raise Refused(
                f"registration is one-shot: {[_trial_id(c['config_id']) for c in existing]} already registered"
            )
        for cfg in CONFIGS:
            hyper = _hyperparameters(inputs, cfg)
            record = TrialRecord(
                trial_id=_trial_id(cfg["config_id"]),
                created_at=_utcnow(),
                hypothesis=_menu_slot(inputs)["hypothesis"],
                git_sha=_git_head(REPO_ROOT),
                config_hash=_config_hash(hyper),
                dataset_manifest_hash=inputs.dataset_manifest_hash,
                train_window=(date.fromisoformat(INIT_START), date.fromisoformat(INIT_END)),
                validation_window=(date.fromisoformat(INNER_START), date.fromisoformat(INNER_END)),
                test_window=None,  # the sealed outer window is a later, separate round
                hyperparameters=hyper,
                scope_key=scope.scope_key(),
            )
            registry.register(record, scope)
            print(
                f"registered {_trial_id(cfg['config_id'])}"
                f" variant={cfg['variant']} k={cfg['latent_k']} h={cfg['h']}"
            )
    finally:
        registry.close()
    print(
        f"registry: {REGISTRY_PATH} (scope trials=8, cap=32);"
        " NO outcome has been computed or viewed"
    )
    return 0


def phase_execute() -> int:
    inputs = load_and_bind()
    JEPA_DIR.mkdir(parents=True, exist_ok=True)
    TRIALS_DIR.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise Refused("another jepa-filter execution holds the lock -- one run at a time") from None
        registry = _open_registry()
        try:
            # the shared feature pass over the chain-35 panel (SHARED flock
            # inside read_panel_with_sha256, already taken at bind)
            print("building chain-35 feature tables ...", flush=True)
            ft = build_feature_tables(inputs.panel, inputs.index_rows, inputs.grid, inputs.chain35)
            for cfg in CONFIGS:
                trial_id = _trial_id(cfg["config_id"])
                artifact = _trial_artifact_path(trial_id)
                status = registry.status(trial_id)
                if status == "COMPLETED":
                    if not artifact.exists():
                        raise Refused(f"{trial_id} is COMPLETED but its artifact is missing")
                    print(
                        f"{trial_id}: already COMPLETED (one scored run, immutable)"
                        f" artifact={artifact}",
                        flush=True,
                    )
                    continue
                if status == "FAILED":
                    print(f"{trial_id}: already FAILED (defect recorded) -- not re-run", flush=True)
                    continue
                if status == "RUNNING":
                    # crash remnant of the interrupted 2026-09-24 execution:
                    # no outcome, no artifact -- the divergence crash is the
                    # defect; record it and continue (one defect record).
                    if artifact.exists() or registry.has_outcome(trial_id):
                        raise Refused(
                            f"{trial_id} is RUNNING with an outcome/artifact -- refusing"
                        )
                    registry.fail(
                        trial_id,
                        "interrupted-execution crash remnant (2026-09-24 run,"
                        " /tmp/jepa-filter-r1-execute.log): VICReg-linear GD"
                        " overflowed at the first refit vintage 2024-07 (h=5),"
                        " inf/NaN training surprises killed the run before any"
                        " outcome existed; no artifact, no score",
                        at=_utcnow(),
                    )
                    print(
                        f"{trial_id}: crash remnant recorded FAILED (no outcome existed)",
                        flush=True,
                    )
                    continue
                if status != "REGISTERED":
                    raise Refused(f"{trial_id} is {status}, not REGISTERED -- refusing to run")
                hyper = _hyperparameters(inputs, cfg)
                config_hash = _config_hash(hyper)
                git_sha = _git_head(REPO_ROOT)
                registry.mark_running(
                    trial_id,
                    git_sha=git_sha,
                    config_hash=config_hash,
                    dataset_manifest_hash=inputs.dataset_manifest_hash,
                    at=_utcnow(),
                )
                print(f"{trial_id}: running inner folds ...", flush=True)
                try:
                    result = run_config(inputs, cfg, ft)
                    poison = run_poison(inputs, cfg, result.sampled_surprises)
                except VicregDiverged as exc:
                    registry.fail(
                        trial_id,
                        f"defective run (V3 optimizer divergence under the pinned"
                        f" constants): {exc}",
                        at=_utcnow(),
                    )
                    print(
                        f"{trial_id}: DEFECTIVE (VICReg divergence) -- FAILED, no"
                        " artifact; NOT_EVALUABLE by operator ruling only",
                        flush=True,
                    )
                    continue
                poison_pass = all(p["pass"] for p in poison)
                result.payload["future_poison"] = {
                    "convention": "s recomputed from inputs truncated at u; max abs diff <= 1e-12",
                    "samples": poison,
                    "pass": poison_pass,
                }
                if not poison_pass:
                    result.not_evaluable_reasons.append("future-poison test failed")
                if result.defective_reasons:
                    # machinery-level defect: the trial FAILS, no artifact rides
                    registry.fail(
                        trial_id,
                        "; ".join(result.defective_reasons),
                        at=_utcnow(),
                    )
                    raise Refused(
                        f"{trial_id}: {'; '.join(result.defective_reasons)}"
                        " (operator-ruling defect; trial FAILED, no artifact written)"
                    )
                body = {"stamp": _stamp(inputs, cfg), "payload": result.payload}
                artifact.write_text(
                    json.dumps(body, indent=2, sort_keys=True) + "\n", encoding="utf-8"
                )
                registry.complete(trial_id, metrics_uri=str(artifact), outcome_at=_utcnow())
                s = result.payload["summary"]
                g = result.payload["geometry"]
                print(
                    f"{trial_id}: COMPLETED artifact={artifact}"
                    f" mean_ic={s['mean_ic']:+.6f} nw_t={s['nw_t_lag_h']}"
                    f" origins={g['origins_scored']}/{len(inputs.inner_ords)}"
                    f" skipped={g['origins_skipped_breadth']}"
                    f" poison={'PASS' if poison_pass else 'FAIL'}"
                    f" not_evaluable={len(result.not_evaluable_reasons)}",
                    flush=True,
                )
        finally:
            registry.close()
    finally:
        os.close(lock_fd)
    return 0


def _read_artifact(inputs: Inputs, cfg: Mapping[str, Any]) -> Mapping[str, Any]:
    trial_id = _trial_id(cfg["config_id"])
    artifact = _trial_artifact_path(trial_id)
    body = json.loads(artifact.read_text(encoding="utf-8"))
    stamp = body.get("stamp", {})
    if stamp.get("trial_id") != trial_id:
        raise Refused(
            f"{artifact} carries trial_id {stamp.get('trial_id')!r}, expected {trial_id!r}"
            " -- only the EXECUTED artifact is selection evidence"
        )
    if stamp.get("registration_menu_sha256") != inputs.menu_sha256:
        raise Refused(f"{artifact} was executed against a different menu hash")
    if stamp.get("dataset_manifest_hash") != inputs.dataset_manifest_hash:
        raise Refused(f"{artifact} was executed against different inputs")
    if stamp.get("runner_sha256") not in (
        _sha256_file(Path(__file__).resolve()),
        EXECUTING_SHA,
    ):
        raise Refused(f"{artifact} was executed by a different runner build -- refusing to select")
    return body


def phase_select() -> int:
    """Freeze the round-1 inner-loop selection from the EXECUTED artifacts
    only (never recomputing), per the pre-declared SEL-a / SEL-b / FLIP."""
    inputs = load_and_bind()
    if SELECTION_PATH.exists():
        raise Refused(f"{SELECTION_PATH} already exists -- the round-1 selection is one-shot")
    per_config: dict[str, Any] = {}
    operator_rulings: list[dict[str, Any]] = []
    registry = _open_registry()
    try:
        for cfg in CONFIGS:
            trial_id = _trial_id(cfg["config_id"])
            status = registry.status(trial_id)
            if status == "COMPLETED":
                body = _read_artifact(inputs, cfg)
                payload = body["payload"]
                ne = list(payload.get("not_evaluable_reasons", []))
                if not payload.get("future_poison", {}).get("pass", False):
                    ne.append("future-poison test failed")
                per_config[cfg["config_id"]] = {
                    "variant": cfg["variant"],
                    "h": cfg["h"],
                    "mean_ic": payload["summary"]["mean_ic"],
                    "nw_t": payload["summary"]["nw_t_lag_h"],
                    "origins_scored": payload["geometry"]["origins_scored"],
                    "status": "NOT_EVALUABLE" if ne else "INNER-RECORDED",
                    "not_evaluable_reasons": ne,
                }
            elif status == "FAILED":
                reason = None
                for kind, payload_json in registry.events(trial_id):
                    if kind == "FAILED":
                        reason = json.loads(payload_json).get("reason")
                per_config[cfg["config_id"]] = {
                    "variant": cfg["variant"],
                    "h": cfg["h"],
                    "status": "NOT_EVALUABLE",
                    "registry_status": "FAILED",
                    "not_evaluable_reasons": [
                        f"defective run (registry FAILED, no artifact): {reason}"
                    ],
                }
                operator_rulings.append(
                    {
                        "config_id": cfg["config_id"],
                        "defect": "VICReg-linear optimizer divergence under the pinned"
                                  " constants (GD 2000 iters, lr 1e-2, PCG64(22) init"
                                  " x 1e-2) on the registered inputs",
                        "ruling_requested": (
                            "NOT_EVALUABLE stands unless the operator rules otherwise"
                            " (slot doc: defective runs are NOT_EVALUABLE by operator"
                            " ruling only; the pinned constants are the registration's"
                            " and cannot be tuned without a NEW registration)"
                        ),
                    }
                )
            else:
                raise Refused(
                    f"{trial_id} is {status}: selection requires every trial COMPLETED"
                    " or FAILED (one scored run per cell, immutable)"
                )
    finally:
        registry.close()

    selection: dict[str, Any] = {"SEL-a": {}, "SEL-b": {}, "FLIP": {}}
    for h in (5, 21):
        at_h = {
            cid: row
            for cid, row in per_config.items()
            if row["h"] == h
        }
        evaluable = {
            cid: row for cid, row in at_h.items() if row["status"] == "INNER-RECORDED"
        }
        positive = {cid: row for cid, row in evaluable.items() if row["mean_ic"] > 0.0}
        if positive:
            best_val = max(row["mean_ic"] for row in positive.values())
            tied = [cid for cid, row in positive.items() if row["mean_ic"] == best_val]
            chosen = min(tied, key=lambda cid: SELECTION_TIE_BREAK.index(cid.split("-")[1]))
            selection["SEL-a"][f"h{h}"] = {
                "chosen": chosen,
                "mean_ic": best_val,
                "ranked": sorted(
                    ((cid, row["mean_ic"]) for cid, row in evaluable.items()),
                    key=lambda t: -t[1],
                ),
                "excluded_not_evaluable": sorted(set(at_h) - set(evaluable)),
                "outer_scoring": "PENDING (sealed outer round; not run in round 1)",
            }
        else:
            selection["SEL-a"][f"h{h}"] = {
                "chosen": None,
                "verdict": "FAIL",
                "reason": (
                    "no variant has positive mean inner IC at this horizon --"
                    " (a) recorded FAIL on inner evidence (wrong-sign falsifier)"
                    " with no outer scoring"
                ),
                "ranked": sorted(
                    ((cid, row["mean_ic"]) for cid, row in evaluable.items()),
                    key=lambda t: -t[1],
                ),
                "excluded_not_evaluable": sorted(set(at_h) - set(evaluable)),
            }
        all_negative = bool(evaluable) and all(row["mean_ic"] < 0.0 for row in evaluable.values())
        selection["FLIP"][f"h{h}"] = {
            "all_evaluable_variants_negative": all_negative,
            "assessment": (
                "wrong-sign case: a flipped variant MAY be registered as a new candidate"
                " consuming one of the four variant slots (displacing the lowest"
                " inner-ranked variant, recorded NOT_RUN) -- a registration decision"
                " for the consolidator/operator, NOT taken by this round-1 executor"
                if all_negative
                else "FLIP not indicated at this horizon"
            ),
        }
    selection["SEL-b"] = {
        "config_id": "JF-V1-H21",
        "rule": "FIXED to JF-V1-H21 by SEL-b, independent of SEL-a",
        "outer_scoring": "PENDING (sealed outer round; not run in round 1)",
    }

    record = {
        "stamp": {
            **_stamp(inputs, CONFIGS[0]),
            "trial_id": None,
            "artifact_trial_ids": [_trial_id(c["config_id"]) for c in CONFIGS],
            "menu_hypothesis": _menu_slot(inputs)["hypothesis"],
            "acceptance_criteria": _menu_slot(inputs)["acceptance_criteria"],
            "verdict_vocabulary": _menu_slot(inputs)["verdict_vocabulary"],
        },
        "round": ROUND,
        "round_scope": "inner folds only; the sealed outer window is untouched and no verdict is stamped here",
        "per_config": per_config,
        "selection": selection,
        "operator_rulings_requested": operator_rulings,
        "notes": [
            "SEL-a/SEL-b/FLIP applied exactly as pre-declared (slot doc section 5);",
            "the selection is now FROZEN and recorded -- the sealed outer round scores",
            "the SEL-a choices and the SEL-b fixed config ONCE, under its own run.",
            "No verdict is stamped in round 1: PASS/PASS-REDUNDANT/FAIL/NOT_EVALUABLE",
            "belong to the three sealed questions, evaluated on the outer window.",
            "Defective (FAILED) trials are recorded NOT_EVALUABLE with an operator",
            "ruling requested; SEL-a proceeds over the EVALUABLE variants exactly",
            "as the pre-declared rule allows (excluded_not_evaluable is stamped).",
        ],
    }
    SELECTION_PATH.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"round-1 selection frozen at {SELECTION_PATH}")
    for h in (5, 21):
        block = selection["SEL-a"][f"h{h}"]
        print(
            f"  SEL-a h={h}: chosen={block['chosen']}"
            + (f" mean_ic={block['mean_ic']:+.6f}" if block["chosen"] else f" {block['verdict']}")
        )
        print(f"  FLIP h={h}: {selection['FLIP'][f'h{h}']['all_evaluable_variants_negative']}")
    print(f"  SEL-b: {selection['SEL-b']['config_id']} (fixed)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--register",
        action="store_true",
        help="INV-13: write the 8 trial rows (REGISTERED, no outcome) to the slot registry",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="run the 8 inner-fold configs one-shot (REGISTERED -> RUNNING -> COMPLETED + artifact)",
    )
    parser.add_argument(
        "--select",
        action="store_true",
        help="read ONLY the executed artifacts and freeze the SEL-a/SEL-b/FLIP selection record",
    )
    parser.add_argument("--plan", action="store_true", help="read-only: print the bound geometry")
    args = parser.parse_args(argv)
    try:
        if args.register:
            return phase_register()
        if args.execute:
            return phase_execute()
        if args.select:
            return phase_select()
        if args.plan:
            return phase_plan()
    except Refused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
