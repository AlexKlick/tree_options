#!/usr/bin/env python3
"""campaign-2026-09 JEPA-FILTER SEALED ROUND (round 2, scope ``c09-jf``).

Operator authorization 2026-09-24 (ruling 1 of 4): the sealed outer window
is OPEN for the three queued families. This runner opens it ONCE for
jepa-filter: exactly TWO outer questions, one scored run each —

* ``(a) at h=5``: ``JF-V2-H5`` (SEL-a frozen by the round-1 selection,
  inner mean IC 0.012937406031059268 — selection evidence only);
* ``(b)``: ``JF-V1-H21`` (SEL-b FIXED, independent of SEL-a).

``(a) at h=21`` is NOT scored here: the pre-declared SEL-a rule recorded it
FAIL on inner evidence (wrong-sign falsifier — no evaluable variant positive
at h=21) with NO outer window; that verdict is carried into the three-verdict
accounting verbatim from the frozen round-1 selection.

Operator rulings of 2026-09-24 recorded in every artifact:
(2) JEPA FLIP-h21 registration DECLINED — no new variant, scored exactly as
    registered (the inner-round FLIP assessment stays recorded as not-taken);
(3) JEPA V3 divergence ruled NOT_EVALUABLE-defective, stands, no successor
    registration authorized;
(1) sealed reads authorized (operative here);
(4) pead-deep-2 stays descriptive (not operative here).

IDENTICAL-PATH GUARANTEE: this module imports the round-1 runner
(``scripts/campaign/jepa-filter_run.py``, sha256 pinned to the round-1 stamp
5c6c2ead...) and reuses its binding, feature tables, vintage fit, surprise,
Spearman and Newey-West machinery verbatim. The round-1 module's MAIN_ROOT
path constants are rebound to the frozen input root BEFORE any phase runs
(the vrp-cond sealed-round pattern); the round-1 BYTES stay pinned.

Sealed window (registration fold_mapping.sealed, verbatim): origins
2026-01-02 through cutoff-h, cutoff = earliest last session among the 35 =
2026-09-23; 182 sessions in span (round-1 verified), complete origins 177 at
h=5 / 161 at h=21; quarterly anchored-expanding refits continue under the
identical rule (vintages in force over the outer window: 2025-10, 2026-01,
2026-04, 2026-07).

Registered acceptance (menu ``acceptance_criteria``, verbatim; NEVER
re-derived or loosened here):
* (a) PASS iff mean daily cross-sectional Spearman IC on sealed outer
  origins in the declared direction (long LOW surprise / short HIGH), with
  one-sided circular block bootstrap (block = h, B = 2000, seed PCG64(22))
  p < 0.05; PASS-REDUNDANT iff PASS but the partial IC (per-origin
  OLS-residualization of standardized surprise and forward return on
  {XSMOM score, lnRV21}, intercept, per-session cross-section; Spearman of
  the residuals; SAME bootstrap; |mean|/SE < 1) has |t| < 1; FAIL otherwise
  incl. inner wrong-sign.
* (b) PASS iff S_u (cross-sectional mean standardized surprise of the
  SEL-b-fixed JF-V1-H21) beats vix_term (VIX/VIX3M - 1, the same INV-02
  one-session lag) on Spearman with forward RV21(SPY) = sum_{j=1..21}
  v^SPY_{u+j} (rv.py proxy) by a one-sided PAIRED circular block bootstrap
  (joint resample of origins, both correlations recomputed) p < 0.05; the
  MDD21 twin is reported with identical machinery (disclosure); the
  dispersion twin and the S_u-tercile card-gate lift are DISCLOSED, never
  verdicts.
* NOT_EVALUABLE: complete outer origins < 126, > 10% skipped origins,
  future-poison failure, latent-collapse tripwire (eval-fold latent variance
  < 1e-2 x train) on the evaluable variants. Vocabulary CLOSED:
  PASS / PASS-REDUNDANT / FAIL / NOT_EVALUABLE.
* Baselines on identical origins: the no-signal null band (IC = 0 gate =
  the temporal-block bootstrap band), a disclosed-only 3-seed
  randomized-score null (score_seed jepa-null-1/2/3, deterministic
  hash-random scores — tree_options.trials.null_score, the wave-0 T-NULL
  construction), the XSMOM score, the cheap-surprise controls lnRV21 and
  |r_21| (same long-low convention), and vix_term as the (b) incumbent.

Bootstrap conventions (registered parameters; the pinned seed PCG64(22)
starts a FRESH generator per test — disclosed): circular block bootstrap on
the origin-level series — draw ceil(n/block) blocks whose starts are uniform
over 0..n-1 with wraparound, concatenate, truncate to n; statistic = the
mean (a) or the Spearman pair (b); one-sided p = #{resample stat <= 0} /
valid resamples (the vrp-cond sealed-round convention; the (+1) correction
is stamped as a sensitivity); the no-signal band = the 5/50/95 percentiles
of the resample means.

Phases (run in order; the harness logs every one):
* ``--plan`` — read-only bind + geometry + frozen-input verification. No
  sealed outcome is computed or viewed.
* ``--register`` — INV-13: write the 2 sealed trial rows (REGISTERED, no
  outcome) BEFORE any sealed outcome exists. A pre-registered id without an
  outcome is a resume skip; an id WITH an outcome is a hard ABORT (the seal
  on that cell is consumed — one scored run per cell, never overwritten).
  Rows land under the round-1 scope_key so the menu's single 32-cap for
  c09-jf keeps binding (8 -> 10 rows).
* ``--execute`` — one scored run per cell under the slot flock; resumable:
  a cell whose artifact exists AND is COMPLETED is skipped; a cell left
  RUNNING by a crashed attempt with NO outcome and NO artifact has not
  consumed its seal and resumes. ``mark_running`` provenance (git sha +
  config hash) must match the registration, so register and execute run
  under the SAME commit.
* ``--stamp`` — read ONLY the executed artifacts, apply the registered
  criteria verbatim, write ``sealed-round.json`` with the THREE verdicts
  ((a) h=5 scored, (a) h=21 carried FAIL, (b) scored).
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
import sys
import time
from datetime import date
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
R1_PATH = REPO_ROOT / "scripts" / "campaign" / "jepa-filter_run.py"

# ---- frozen-input repoint (2026-09-24; the vrp-cond sealed-round pattern) -----------------
#
# The round-1 runner binds every campaign input AND every artifact path to
# MAIN_ROOT = /home/alexk/documents/tree_options (the live main checkout).
# The sealed round reads and writes ONLY the pin-verified frozen snapshot at
# DESK_REPO_ROOT = /home/alexk/.local/state/campaign-sealed-inputs/root.
# PANEL BLOCK HISTORY: a prior sealed attempt (worktree commit 9a81a2b,
# vrp-cond slot) BLOCKED pre-registration on ohlc-panel drift vs the menu pin
# 0861f525..., concluded "unrecoverable"; superseded by evidence — the pinned
# bytes were recovered from the Wave-1 econ lane sha-named snapshot
# ~/.local/state/trex-desk-w1-econ/paper-snapshot-0861f525/ohlc-panel.json
# (full 64-hex sha256 == the menu pin, verified) and copied into the frozen
# root. The block dissolves; the pin binds. The round-1 runner's BYTES stay
# pinned (R1_RUNNER_SHA256; the identical-path guarantee), so its module-level
# path constants are rebound here, BEFORE any phase runs.
#
# Universe binding (disclosed, the vrp-cond sealed-round binding): the runner
# DERIVES its universe from the pinned 37-name PANEL exactly as round 1 did
# (load_and_bind enforces 37 panel names -> 35 chain-universe; a 39-name
# PANEL roster is a misbind and refuses). The desk-universe.toml CONFIG the
# module loads is the WORKTREE's own copy — the exact bytes round 1 imported;
# the frozen root's 37-name desk-universe.toml is the panel-roster PIN OF
# RECORD but cannot load under universe.py, so the binding here proves the
# two differ by exactly the PLTR/SPCX panel additions and binds the worktree
# bytes. DESK_UNIVERSE is pinned BEFORE the round-1 module executes.

DESK_REPO_ROOT_ENV = "DESK_REPO_ROOT"
MAIN_CHECKOUT = Path("/home/alexk/documents/tree_options")

# every dataset_pinning entry that exists in the frozen root is re-verified
# against the frozen bytes BEFORE computing (the jepa slot's own six inputs
# plus the campaign-wide pins carried in the same frozen root).
PINNED_LABELS_SWEEP: tuple[str, ...] = (
    "artifacts/paper-trades/ohlc-panel.json",
    "artifacts/paper-trades/earnings-calendar.json",
    "artifacts/paper-trades/earnings-timing.json",
    "artifacts/desk-store/indices/VIX.csv",
    "artifacts/desk-store/indices/VIX1Y.csv",
    "artifacts/desk-store/indices/VIX3M.csv",
    "artifacts/desk-store/indices/VIX9D.csv",
    "artifacts/desk-store/indices/VVIX.csv",
    "artifacts/desk-store/iv-history/vwap_atm.json",
    "data/calendar/nyse_sessions_2018_01_02_2026_12_31.json",
)


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _bind_frozen_root() -> tuple[Path, dict[str, Any]]:
    env = os.environ.get(DESK_REPO_ROOT_ENV, "")
    if not env:
        raise SystemExit(
            f"REFUSED: {DESK_REPO_ROOT_ENV} is not set -- the sealed round reads"
            " only the frozen snapshot root"
        )
    frozen = Path(env).resolve()
    if frozen == MAIN_CHECKOUT.resolve():
        raise SystemExit(
            f"REFUSED: {DESK_REPO_ROOT_ENV} points at the live main checkout --"
            " sealed inputs come only from the frozen snapshot"
        )
    required = (
        "artifacts/paper-trades/ohlc-panel.json",
        "artifacts/paper-trades/earnings-calendar.json",
        "data/calendar/nyse_sessions_2018_01_02_2026_12_31.json",
        "artifacts/campaign-2026-09/tnull/calibration-v3.json",
        "artifacts/campaign-2026-09/jepa-filter/inner-round1.json",
        "artifacts/campaign-2026-09/jepa-filter.db",
        "desk-universe.toml",
    )
    missing = [rel for rel in required if not (frozen / rel).exists()]
    if missing:
        raise SystemExit(
            f"REFUSED: the frozen root {frozen} is missing required inputs: {missing}"
        )
    menu = json.loads(
        (REPO_ROOT / "docs" / "theory" / "campaign-2026-09-registration.json").read_text(
            encoding="utf-8"
        )
    )
    pinning = menu["dataset_pinning"]
    sweep: dict[str, str] = {}
    for label in PINNED_LABELS_SWEEP:
        want = pinning.get(label)
        path = frozen / label
        if want is None:
            continue
        if not path.exists():
            raise SystemExit(f"REFUSED: pinned input {label} is missing from the frozen root")
        got = _sha256_file(path)
        if got != want:
            raise SystemExit(
                f"REFUSED: {label}: frozen sha256 {got} != the menu pin {want}"
                " -- swapped inputs refuse"
            )
        sweep[label] = got

    import tomllib

    frozen_universe_path = frozen / "desk-universe.toml"
    frozen_universe = tomllib.loads(frozen_universe_path.read_text(encoding="utf-8"))
    frozen_names = set(frozen_universe.get("panel", {}).get("names", ()))
    if len(frozen_names) != 37:
        raise SystemExit(
            f"REFUSED: frozen desk-universe.toml carries {len(frozen_names)} panel"
            " names, not the registered 37 -- a 39-name roster means the runner"
            " is misbound; abort and report"
        )
    worktree_universe_path = REPO_ROOT / "desk-universe.toml"
    worktree_universe = tomllib.loads(worktree_universe_path.read_text(encoding="utf-8"))
    wt_names = set(worktree_universe.get("panel", {}).get("names", ()))
    if wt_names - frozen_names - {"PLTR", "SPCX"} or frozen_names - wt_names:
        raise SystemExit(
            "REFUSED: the worktree desk-universe.toml and the frozen 37-name pin"
            " differ by more than the registered PLTR/SPCX panel additions"
            f" (worktree {len(wt_names)} names vs frozen {len(frozen_names)})"
        )
    os.environ["DESK_UNIVERSE"] = str(worktree_universe_path)
    binding = {
        "config_bound": str(worktree_universe_path),
        "config_sha256": _sha256_file(worktree_universe_path),
        "config_panel_names": len(wt_names),
        "frozen_pin_of_record": str(frozen_universe_path),
        "frozen_pin_sha256": _sha256_file(frozen_universe_path),
        "frozen_pin_panel_names": len(frozen_names),
        "equivalence": (
            "the bound config's panel = the frozen 37-name pin + exactly"
            " {PLTR, SPCX} (the registered 2026-09-23 additions load_and_bind"
            " tolerates); the runner's derived universe comes from the pinned"
            " 37-name panel"
        ),
        "frozen_copy_note": (
            "the frozen root's 37-name desk-universe.toml is the panel-roster pin"
            " of record; it cannot itself load under universe.py (its"
            " options_close lists still name PLTR/SPCX), so the round-1-identical"
            " worktree bytes are bound instead"
        ),
    }
    return frozen, {"desk_universe_binding": binding, "pinning_sweep": sweep}


FROZEN_ROOT, FROZEN_BINDING = _bind_frozen_root()

_spec = importlib.util.spec_from_file_location("jepa_filter_round1", R1_PATH)
r1 = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
sys.modules["jepa_filter_round1"] = r1
_spec.loader.exec_module(r1)  # type: ignore[union-attr]

# Rebind the round-1 module's MAIN_ROOT-derived path constants to the frozen
# root (load_and_bind and the phases resolve them at call time). The
# worktree-side pins (registration + sidecar + protocol + slot doc) stay on
# REPO_ROOT -- the registered docs are read-only truth, not frozen inputs.
r1.MAIN_ROOT = FROZEN_ROOT
r1.PANEL_PATH = FROZEN_ROOT / "artifacts" / "paper-trades" / "ohlc-panel.json"
r1.CALENDAR_PATH = FROZEN_ROOT / "data" / "calendar" / "nyse_sessions_2018_01_02_2026_12_31.json"
r1.INDICES_DIR = FROZEN_ROOT / "artifacts" / "desk-store" / "indices"
r1.TNULL_V3_PATH = FROZEN_ROOT / "artifacts" / "campaign-2026-09" / "tnull" / "calibration-v3.json"
r1.CAMPAIGN_DIR = FROZEN_ROOT / "artifacts" / "campaign-2026-09"
r1.REGISTRY_PATH = r1.CAMPAIGN_DIR / "jepa-filter.db"
r1.JEPA_DIR = r1.CAMPAIGN_DIR / "jepa-filter"
r1.TRIALS_DIR = r1.JEPA_DIR / "trials"
r1.SELECTION_PATH = r1.JEPA_DIR / "inner-round1.json"
r1.LOCK_PATH = r1.JEPA_DIR / "execute.lock"

ROUND = 2
SLOT_ID = r1.SLOT_ID
SCOPE_ID = r1.SCOPE_ID
CELL_A = "JF-V2-H5"  # SEL-a h=5, frozen by inner-round1.json
CELL_B = "JF-V1-H21"  # SEL-b FIXED, independent of SEL-a
SEALED_CELLS = (CELL_A, CELL_B)
CFG_A = next(c for c in r1.CONFIGS if c["config_id"] == CELL_A)
CFG_B = next(c for c in r1.CONFIGS if c["config_id"] == CELL_B)

OUTER_START = "2026-01-02"
N_SPAN_SESSIONS = 182  # registration fold_mapping.sealed (round-1 verified)
N_COMPLETE_ORIGINS = {5: 177, 21: 161}  # registration fold_mapping.sealed
MIN_COMPLETE_ORIGINS = 126  # NOT_EVALUABLE floor (the protocol's own minimum)
# vintages that can be in force at u-h for an outer origin u (the quarterly
# anchored-expanding schedule continues across the outer window, identical rule)
OUTER_QUARTERS: tuple[tuple[int, int], ...] = ((2025, 10), (2026, 1), (2026, 4), (2026, 7))

BOOTSTRAP_RESAMPLES = 2000  # [PINNED] B
BOOTSTRAP_PCG_SEED = 22  # [PINNED] numpy.random.Generator(PCG64(22)); fresh per test
NULL_SCORE_SEEDS: tuple[str, ...] = ("jepa-null-1", "jepa-null-2", "jepa-null-3")

# round-1 provenance pins (refuse if the world moved)
R1_RUNNER_SHA256 = "5c6c2ead17934612bb815ad6b99cdd178cf78ee67a5e08736ef2930b31515d8c"
CALIBRATION_V3_SHA256 = "5d0aa0eeaea9075ec72e48a0436c2e60dd1f715092fabc4d60ee328c3811e683"
SELECTION_SHA256 = "2fc115332e8cca861a8aa4515accd1526763742c0859a6cb1f5cb3f874f25b60"
FROZEN_SEL_A_H5 = {"config_id": "JF-V2-H5", "mean_ic": 0.012937406031059268}

SEAL_PATH = r1.JEPA_DIR / "sealed-round.json"
EARNINGS_PATH = FROZEN_ROOT / "artifacts" / "paper-trades" / "earnings-calendar.json"

OPERATOR_RULINGS_2026_09_24 = {
    "1": "sealed reads authorized for all three queued families (operative here)",
    "2": "JEPA FLIP-h21 registration DECLINED — no new variant, score exactly as registered (jepa-filter slot)",
    "3": "JEPA V3 divergence ruled NOT_EVALUABLE-defective, stands; no successor registration authorized (jepa-filter slot)",
    "4": "pead-deep-2 stays descriptive (default accepted, no sealed read) (pead-deep-2 slot)",
}


class Refused(RuntimeError):
    """A binding refusal: the sealed registration and the execution disagree."""


# ---- bootstrap machinery (registered parameters; conventions disclosed) --------------------


def _circular_block_indices(rng: np.random.Generator, n: int, block: int) -> np.ndarray:
    """ceil(n/block) circular blocks, starts uniform over 0..n-1 (wraparound),
    concatenated and truncated to n."""
    nblocks = -(-n // block)
    starts = rng.integers(0, n, size=nblocks)
    idx = (starts[:, None] + np.arange(block)[None, :]).ravel()[:n] % n
    return idx


def _boot_mean_one_sided(series: Sequence[float], block: int) -> dict[str, Any]:
    """Circular block bootstrap of the mean; one-sided p = #{resample mean
    <= 0}/B against the no-signal null (mean = 0); the band is the
    5/50/95 percentiles of the resample means."""
    x = np.asarray(series, dtype=float)
    n = len(x)
    rng = np.random.Generator(np.random.PCG64(BOOTSTRAP_PCG_SEED))
    means = np.empty(BOOTSTRAP_RESAMPLES)
    for b in range(BOOTSTRAP_RESAMPLES):
        means[b] = x[_circular_block_indices(rng, n, block)].mean()
    le0 = int((means <= 0.0).sum())
    return {
        "registered_parameters": {
            "block": block,
            "B": BOOTSTRAP_RESAMPLES,
            "seed": f"numpy.random.Generator(PCG64({BOOTSTRAP_PCG_SEED})) [PINNED], fresh generator per test",
            "sided": "one (H1: mean > 0)",
        },
        "conventions_disclosed": {
            "draws": "circular blocks: ceil(n/block) blocks, starts uniform over 0..n-1 with wraparound, concatenated, truncated to n",
            "p_definition": "p = #{resample mean <= 0} / B (vrp-cond sealed-round convention)",
            "band": "no-signal null band = 5/50/95 percentiles of the resample means; the gate is 0 below the 5th percentile",
        },
        "n": n,
        "mean": float(x.mean()),
        "sd": float(x.std(ddof=1)) if n > 1 else None,
        "le_zero": le0,
        "p_one_sided": le0 / BOOTSTRAP_RESAMPLES,
        "p_plus_one_sensitivity": (le0 + 1) / (BOOTSTRAP_RESAMPLES + 1),
        "band_p5_p50_p95": [
            float(np.percentile(means, 5)),
            float(np.percentile(means, 50)),
            float(np.percentile(means, 95)),
        ],
        "bootstrap_se_mean": float(means.std(ddof=1)),
        "resample_means_all_finite": bool(np.isfinite(means).all()),
    }


def _spearman_safe(x: np.ndarray, y: np.ndarray) -> float | None:
    return r1.spearman(np.asarray(x, dtype=float), np.asarray(y, dtype=float))


def _boot_paired_one_sided(
    challenger: np.ndarray,
    incumbent: np.ndarray,
    target: np.ndarray,
    block: int,
    label: str,
) -> dict[str, Any]:
    """PAIRED circular block bootstrap: joint resample of origins, recompute
    BOTH Spearman correlations on the resampled triples, d* = difference;
    one-sided p = #{d* <= 0}/valid (H1: challenger beats incumbent)."""
    n = len(target)
    rho_c = _spearman_safe(challenger, target)
    rho_i = _spearman_safe(incumbent, target)
    rng = np.random.Generator(np.random.PCG64(BOOTSTRAP_PCG_SEED))
    valid = 0
    le0 = 0
    rho_c_star: list[float] = []
    rho_i_star: list[float] = []
    d_star: list[float] = []
    for _ in range(BOOTSTRAP_RESAMPLES):
        idx = _circular_block_indices(rng, n, block)
        rc = _spearman_safe(challenger[idx], target[idx])
        ri = _spearman_safe(incumbent[idx], target[idx])
        if rc is None or ri is None:
            continue  # degenerate resample (constant ranks) -- skipped, counted
        valid += 1
        rho_c_star.append(rc)
        rho_i_star.append(ri)
        d_star.append(rc - ri)
        if rc - ri <= 0.0:
            le0 += 1
    out: dict[str, Any] = {
        "registered_parameters": {
            "block": block,
            "B": BOOTSTRAP_RESAMPLES,
            "seed": f"numpy.random.Generator(PCG64({BOOTSTRAP_PCG_SEED})) [PINNED], fresh generator per test",
            "sided": "one (H1: challenger beats incumbent on Spearman)",
            "pairing": label,
        },
        "conventions_disclosed": {
            "draws": "circular blocks over the COMMON origin index; both correlations recomputed on every resample (joint resample of origins)",
            "p_definition": "p = #{resample d <= 0} / valid resamples",
            "undefined_resamples": "skipped (a degenerate rank vector gives no Spearman)",
        },
        "n_origins": n,
        "rho_challenger": rho_c,
        "rho_incumbent": rho_i,
        "d_challenger_minus_incumbent": (rho_c - rho_i) if (rho_c is not None and rho_i is not None) else None,
        "valid_resamples": valid,
        "le_zero": le0,
    }
    if valid:
        d_arr = np.asarray(d_star)
        out["p_one_sided"] = le0 / valid
        out["p_plus_one_sensitivity"] = (le0 + 1) / (valid + 1)
        out["d_band_p5_p50_p95"] = [
            float(np.percentile(d_arr, 5)),
            float(np.percentile(d_arr, 50)),
            float(np.percentile(d_arr, 95)),
        ]
        out["rho_challenger_band_p5_p95"] = [
            float(np.percentile(rho_c_star, 5)),
            float(np.percentile(rho_c_star, 95)),
        ]
        out["rho_incumbent_band_p5_p95"] = [
            float(np.percentile(rho_i_star, 5)),
            float(np.percentile(rho_i_star, 95)),
        ]
        # per-side one-sided p vs the no-signal null (disclosure)
        out["p_challenger_vs_zero"] = float((np.asarray(rho_c_star) <= 0.0).sum()) / valid
        out["p_incumbent_vs_zero"] = float((np.asarray(rho_i_star) <= 0.0).sum()) / valid
    else:
        out["reason"] = "no valid resample produced a defined pair"
    return out


# ---- sealed geometry -----------------------------------------------------------------------


def _outer_span_ords(inputs: r1.Inputs) -> list[int]:  # type: ignore[name-defined]
    cutoff_ord = inputs.ordinals[inputs.cutoff_iso]
    span = [inputs.ordinals[s] for s in inputs.grid if OUTER_START <= s <= inputs.cutoff_iso]
    if len(span) != N_SPAN_SESSIONS:
        raise Refused(
            f"the outer span is {len(span)} sessions, not the registration's"
            f" {N_SPAN_SESSIONS} ({inputs.grid[span[0]]}..{inputs.grid[span[-1]]})"
        )
    for h, want in N_COMPLETE_ORIGINS.items():
        got = sum(1 for o in span if o + h <= cutoff_ord)
        if got != want:
            raise Refused(
                f"complete outer origins at h={h}: {got}, not the registration's {want}"
            )
    return span


def _outer_first_iso(inputs: r1.Inputs) -> tuple[str, ...]:  # type: ignore[name-defined]
    out: list[str] = []
    for year, month in OUTER_QUARTERS:
        firsts = [s for s in inputs.grid if s.startswith(f"{year:04d}-{month:02d}")]
        if not firsts:
            raise Refused(f"no panel-union session in {year}-{month:02d} for the sealed refit")
        out.append(firsts[0])
    return tuple(out)


def _fit_outer_vintages(
    inputs: r1.Inputs,  # type: ignore[name-defined]
    cfg: Mapping[str, Any],
    ft: r1.FeatureTables,  # type: ignore[name-defined]
) -> list[r1.Vintage]:  # type: ignore[name-defined]
    roster: tuple[str, ...] = r1.SINGLES26 if cfg["variant"] == "V4" else inputs.chain35
    vintages = []
    for (year, month), first_iso in zip(OUTER_QUARTERS, _outer_first_iso(inputs), strict=True):
        vintages.append(
            r1.fit_vintage(
                ft, roster, cfg, f"{year:04d}-{month:02d}", first_iso, inputs.ordinals[first_iso]
            )
        )
    return vintages


def _vintage_in_force(vintages: Sequence[r1.Vintage], ord_prev: int) -> r1.Vintage | None:  # type: ignore[name-defined]
    vin = None
    for v in vintages:
        if v.first_ord <= ord_prev:
            vin = v
    return vin


def _label_and_baseline_grids(
    inputs: r1.Inputs, ft: r1.FeatureTables, roster: Sequence[str], h: int  # type: ignore[name-defined]
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    n_sessions = len(ft.grid)
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
            if i >= r1.XSMOM_LOOKBACK and cl[i] is not None and cl[i - r1.XSMOM_LOOKBACK] is not None:
                if cl[i - r1.XSMOM_LOOKBACK] > 0:
                    xsmom[ni, i] = cl[i] / cl[i - r1.XSMOM_LOOKBACK] - 1.0
            x = ft.eq_raw[name]["lnrv20"][i]
            if x is not None:
                lnrv21[ni, i] = x
            x = ft.eq_raw[name]["r21"][i]
            if x is not None:
                absr21[ni, i] = abs(x)
    return label, xsmom, lnrv21, absr21


def _partial_ic_origin(
    s_std: np.ndarray, r: np.ndarray, x: np.ndarray, l: np.ndarray
) -> tuple[float | None, int]:
    """OLS-residualize the standardized surprise and the forward return on
    {XSMOM score, lnRV21} (intercept, per-session cross-section); Spearman of
    the residuals in the SAME declared direction (long LOW / short HIGH)."""
    m = np.isfinite(s_std) & np.isfinite(r) & np.isfinite(x) & np.isfinite(l)
    n = int(m.sum())
    if n < 3:
        return None, n
    xs = np.column_stack([np.ones(n), x[m], l[m]])
    ss, rs = s_std[m], r[m]
    beta_s, *_ = np.linalg.lstsq(xs, ss, rcond=None)
    beta_r, *_ = np.linalg.lstsq(xs, rs, rcond=None)
    return _spearman_safe(-(ss - xs @ beta_s), rs - xs @ beta_r), n


# ---- cell (a): JF-V2-H5 on the outer window ------------------------------------------------


def score_cell_a(inputs: r1.Inputs, ft: r1.FeatureTables) -> dict[str, Any]:  # type: ignore[name-defined]
    from tree_options.trials.null_score import null_score

    cfg = CFG_A
    h = int(cfg["h"])
    roster: tuple[str, ...] = inputs.chain35
    span = _outer_span_ords(inputs)
    cutoff_ord = inputs.ordinals[inputs.cutoff_iso]
    label, xsmom, lnrv21, absr21 = _label_and_baseline_grids(inputs, ft, roster, h)
    vintages = _fit_outer_vintages(inputs, cfg, ft)

    rows: list[dict[str, Any]] = []
    seg_eval_cells: dict[str, np.ndarray] = {}
    sampled_surprises: dict[str, dict[str, float]] = {}
    sampled_segments: set[str] = set()
    complete_origins = 0
    origins_no_vintage = 0
    origins_skipped_breadth = 0
    origins_degenerate = 0
    origins_scored = 0

    for u_ord in span:
        if u_ord + h > cutoff_ord:
            continue  # label window incomplete (the span's last h sessions)
        complete_origins += 1
        u_iso = ft.grid[u_ord]
        vin = _vintage_in_force(vintages, u_ord - h)
        if vin is None:
            origins_no_vintage += 1
            continue
        s_raw = r1.surprise_at(vin, list(range(len(roster))), u_ord - h, u_ord)
        ok = np.isfinite(s_raw) & np.isfinite(label[:, u_ord])
        if int(ok.sum()) < r1.BREADTH_MIN_NAMES:
            origins_skipped_breadth += 1
            continue
        s_std = s_raw[ok] / vin.surprise_sd
        r = label[ok, u_ord]
        ic = _spearman_safe(-s_std, r)
        if ic is None:
            origins_degenerate += 1
            continue
        origins_scored += 1
        row: dict[str, Any] = {
            "u": u_iso,
            "vintage": vin.quarter,
            "ic": ic,
            "n": int(ok.sum()),
        }
        sel_x = xsmom[ok, u_ord]
        sel_l = lnrv21[ok, u_ord]
        sel_a = absr21[ok, u_ord]
        fin_x, fin_l, fin_a = np.isfinite(sel_x), np.isfinite(sel_l), np.isfinite(sel_a)
        row["ic_xsmom"] = (
            _spearman_safe(sel_x[fin_x], r[fin_x]) if int(fin_x.sum()) >= r1.BREADTH_MIN_NAMES else None
        )
        row["ic_lnrv21_control"] = (
            _spearman_safe(-sel_l[fin_l], r[fin_l]) if int(fin_l.sum()) >= r1.BREADTH_MIN_NAMES else None
        )
        row["ic_absr21_control"] = (
            _spearman_safe(-sel_a[fin_a], r[fin_a]) if int(fin_a.sum()) >= r1.BREADTH_MIN_NAMES else None
        )
        pic, pn = _partial_ic_origin(s_std, r, sel_x, sel_l)
        row["ic_partial"] = pic
        row["n_partial"] = pn
        names_ok = [roster[i] for i in np.nonzero(ok)[0]]
        u_date = date.fromisoformat(u_iso)
        for seed in NULL_SCORE_SEEDS:
            ns = np.array(
                [null_score(seed=seed, session=u_date, security_id=nm) for nm in names_ok]
            )
            row[f"ic_null_{seed}"] = _spearman_safe(-ns, r)
        rows.append(row)
        cells = vin.latent[:, u_ord][np.isfinite(s_raw)]
        if vin.quarter in seg_eval_cells:
            seg_eval_cells[vin.quarter] = np.vstack([seg_eval_cells[vin.quarter], cells])
        else:
            seg_eval_cells[vin.quarter] = cells
        if vin.quarter not in sampled_segments:  # future-poison sample per segment
            sampled_segments.add(vin.quarter)
            sampled_surprises[u_iso] = {nm: float(s_raw[i]) for i, nm in enumerate(roster) if np.isfinite(s_raw[i])}

    if origins_scored == 0:
        raise Refused(f"{CELL_A}: 0 scored outer origins -- machinery defect, no artifact")

    ics = [row["ic"] for row in rows]
    ics_x = [row["ic_xsmom"] for row in rows if row.get("ic_xsmom") is not None]
    ics_l = [row["ic_lnrv21_control"] for row in rows if row.get("ic_lnrv21_control") is not None]
    ics_a = [row["ic_absr21_control"] for row in rows if row.get("ic_absr21_control") is not None]
    ics_p = [row["ic_partial"] for row in rows if row.get("ic_partial") is not None]

    # latent-collapse tripwire (eval-fold latent variance < 1e-2 x train)
    collapse_reasons: list[str] = []
    for v in vintages:
        cells = seg_eval_cells.get(v.quarter)
        if cells is None or len(cells) == 0:
            continue
        eval_var = np.var(cells, axis=0, ddof=0)
        tripped = [
            int(j)
            for j in range(v.k_eff)
            if float(eval_var[j]) < r1.V3_COLLAPSE_RATIO * float(v.train_latent_var[j])
        ]
        if tripped:
            collapse_reasons.append(
                f"collapse tripwire: vintage {v.quarter} eval-fold latent variance"
                f" < {r1.V3_COLLAPSE_RATIO} x train-fold on dims {tripped}"
            )

    total_decided = origins_scored + origins_skipped_breadth + origins_degenerate
    skip_frac = origins_skipped_breadth / total_decided if total_decided else 0.0

    boot_ic = _boot_mean_one_sided(ics, h)
    boot_partial = _boot_mean_one_sided(ics_p, h) if ics_p else None
    t_partial = (
        boot_partial["mean"] / boot_partial["bootstrap_se_mean"]
        if boot_partial and boot_partial["bootstrap_se_mean"]
        else None
    )
    payload = {
        "config": {
            "config_id": cfg["config_id"],
            "variant": cfg["variant"],
            "latent_k_declared": int(cfg["latent_k"]),
            "h": h,
            "roster": f"chain-35 ({len(roster)})",
            "direction_convention": "ic = Spearman(-std_surprise, forward_return); mean > 0 is the declared direction",
            "selected_by": "SEL-a h=5 (frozen by inner-round1.json; inner mean IC is selection evidence only)",
        },
        "geometry": {
            "outer_span": [OUTER_START, inputs.cutoff_iso],
            "span_sessions": N_SPAN_SESSIONS,
            "complete_origins": complete_origins,
            "origins_scored": origins_scored,
            "origins_without_vintage": origins_no_vintage,
            "origins_skipped_breadth": origins_skipped_breadth,
            "origins_degenerate_ic": origins_degenerate,
            "skipped_breadth_fraction": skip_frac,
            "refits": [
                {"quarter": v.quarter, "first_session": v.first_iso, "train_rows": v.n_train_rows}
                for v in vintages
            ],
            "purge_rule": f"ordinal(s) + h + 5 < ordinal(first session of Q), h = {h}",
            "surprise_dating": "indexed by completion session u, vintage in force at u-h (registered)",
        },
        "summary": {
            "mean_ic": statistics.fmean(ics),
            "ic_sd": statistics.stdev(ics),
            "nw_t_lag_h": r1.nw_t(ics, h),
            "mean_ic_by_segment": {
                q: statistics.fmean([row["ic"] for row in rows if row["vintage"] == q])
                for q in sorted({row["vintage"] for row in rows})
            },
            "n_by_segment": {
                q: sum(1 for row in rows if row["vintage"] == q)
                for q in sorted({row["vintage"] for row in rows})
            },
        },
        "bootstrap_mean_ic": boot_ic,
        "partial_ic": {
            "convention": (
                "per outer origin, OLS-residualize standardized surprise and forward"
                " return on {XSMOM score, lnRV21} (intercept, per-session"
                " cross-section); ic_partial = Spearman(-res_s, res_r); SAME"
                " bootstrap; t = mean/bootstrap_se [PINNED |mean|/SE < 1]"
            ),
            "n_origins_with_partial": len(ics_p),
            "mean_partial_ic": statistics.fmean(ics_p) if ics_p else None,
            "nw_t_partial": r1.nw_t(ics_p, h) if ics_p else None,
            "bootstrap": boot_partial,
            "t_partial": t_partial,
        },
        "baselines_disclosed": {
            "xsmom_score": {
                "mean_ic": statistics.fmean(ics_x) if ics_x else None,
                "nw_t": r1.nw_t(ics_x, h) if ics_x else None,
                "bootstrap": _boot_mean_one_sided(ics_x, h) if ics_x else None,
            },
            "lnrv21_control": {
                "mean_ic": statistics.fmean(ics_l) if ics_l else None,
                "nw_t": r1.nw_t(ics_l, h) if ics_l else None,
                "bootstrap": _boot_mean_one_sided(ics_l, h) if ics_l else None,
            },
            "absr21_control": {
                "mean_ic": statistics.fmean(ics_a) if ics_a else None,
                "nw_t": r1.nw_t(ics_a, h) if ics_a else None,
                "bootstrap": _boot_mean_one_sided(ics_a, h) if ics_a else None,
            },
            "randomized_score_null": {
                "score_seeds": list(NULL_SCORE_SEEDS),
                "construction": (
                    "tree_options.trials.null_score.null_score(seed, session=u,"
                    " security_id=name) — the wave-0 T-NULL deterministic"
                    " hash-random score, ranked long-LOW/short-HIGH on the"
                    " identical origin/name cells; disclosed only"
                ),
                "per_seed": {
                    seed: {
                        "mean_ic": statistics.fmean(
                            [row[f"ic_null_{seed}"] for row in rows if row.get(f"ic_null_{seed}") is not None]
                        ),
                        "bootstrap": _boot_mean_one_sided(
                            [row[f"ic_null_{seed}"] for row in rows if row.get(f"ic_null_{seed}") is not None],
                            h,
                        ),
                    }
                    for seed in NULL_SCORE_SEEDS
                },
            },
            "no_signal_null_band": {
                "gate": "IC = 0 outside the temporal-block bootstrap band (5th percentile); equivalent to the one-sided p < 0.05 gate",
                "band_p5_p50_p95": boot_ic["band_p5_p50_p95"],
            },
            "note": "identical origin/name cells; long-low convention for the controls; disclosed, never verdicts",
        },
        "surprise_sd_by_vintage": {v.quarter: v.surprise_sd for v in vintages},
        "not_evaluable_reasons": collapse_reasons,
        "rows": rows,
    }
    poison = sealed_run_poison(inputs, cfg, sampled_surprises)
    poison_pass = all(p["pass"] for p in poison)
    payload["future_poison"] = {
        "convention": "s recomputed from inputs truncated at u; max abs diff <= 1e-12 (round-1 machinery, sealed quarters)",
        "samples": poison,
        "pass": poison_pass,
    }
    if not poison_pass:
        payload["not_evaluable_reasons"].append("future-poison test failed")
    guards = {
        "complete_outer_origins_ge_126": complete_origins >= MIN_COMPLETE_ORIGINS,
        "skipped_fraction_le_10pct": skip_frac <= r1.SKIP_DEFECT_FRAC,
        "future_poison_pass": poison_pass,
        "no_latent_collapse": not collapse_reasons,
    }
    payload["evaluability_guards"] = guards
    return payload


# ---- cell (b): JF-V1-H21 S_u vs the vix_term incumbent --------------------------------------


def _spy_targets(
    inputs: r1.Inputs, ft: r1.FeatureTables  # type: ignore[name-defined]
) -> tuple[dict[int, float], dict[int, float]]:
    """Forward RV21(SPY) = sum_{j=1..21} v^SPY_{u+j} (rv.py proxy) and the
    MDD21 twin, keyed by origin ordinal (complete 21-session windows only)."""
    grid = ft.grid
    v_spy = r1.proxy_series(inputs.panel["SPY"], list(grid))
    closes = ft.closes["SPY"]
    h = 21
    rv: dict[int, float] = {}
    mdd: dict[int, float] = {}
    for u_ord in range(len(grid)):
        if u_ord + h >= len(grid):
            break
        window = list(range(u_ord + 1, u_ord + h + 1))
        vs = [v_spy[j] for j in window]
        cs = [closes[j] for j in window]
        if closes[u_ord] is None or any(x is None for x in vs) or any(x is None for x in cs):
            continue
        if any((not math.isfinite(x)) for x in vs):
            continue
        rv[u_ord] = math.fsum(vs)
        run_max = closes[u_ord]
        worst = 0.0
        for c in cs:
            run_max = max(run_max, c)
            worst = max(worst, 1.0 - c / run_max)
        mdd[u_ord] = worst
    return rv, mdd


def _training_tercile_cuts(
    inputs: r1.Inputs,  # type: ignore[name-defined]
    ft: r1.FeatureTables,  # type: ignore[name-defined]
    roster: Sequence[str],
    vintages: Sequence[r1.Vintage],  # type: ignore[name-defined]
    h: int,
) -> dict[str, tuple[float, float]]:
    """Per-vintage training-fold S tercile cuts (33.3/66.7 percentiles of the
    training sessions' cross-sectional mean standardized surprise)."""
    cuts: dict[str, tuple[float, float]] = {}
    for vin in vintages:
        tset = set(vin.train_ords)
        vals: list[float] = []
        n_sessions = len(ft.grid)
        for o in vin.train_ords:
            o2 = o + h
            if o2 >= n_sessions or o2 not in tset:
                continue
            s_raw = r1.surprise_at(vin, list(range(len(roster))), o, o2)
            ok = np.isfinite(s_raw)
            if int(ok.sum()) < r1.BREADTH_MIN_NAMES:
                continue
            vals.append(float((s_raw[ok] / vin.surprise_sd).mean()))
        if len(vals) < 30:
            raise Refused(
                f"vintage {vin.quarter}: {len(vals)} training S values -- too few for tercile cuts"
            )
        arr = np.asarray(vals)
        cuts[vin.quarter] = (
            float(np.percentile(arr, 100.0 / 3.0)),
            float(np.percentile(arr, 200.0 / 3.0)),
        )
    return cuts


def score_cell_b(inputs: r1.Inputs, ft: r1.FeatureTables) -> dict[str, Any]:  # type: ignore[name-defined]
    cfg = CFG_B
    h = int(cfg["h"])
    roster: tuple[str, ...] = inputs.chain35
    span = _outer_span_ords(inputs)
    cutoff_ord = inputs.ordinals[inputs.cutoff_iso]
    vintages = _fit_outer_vintages(inputs, cfg, ft)
    rv21_by_ord, mdd21_by_ord = _spy_targets(inputs, ft)
    vix_term_col = ft.idx_raw["vix_term"]

    rows: list[dict[str, Any]] = []
    seg_eval_cells: dict[str, np.ndarray] = {}
    sampled_surprises: dict[str, dict[str, float]] = {}
    sampled_segments: set[str] = set()
    complete_origins = 0
    origins_no_vintage = 0
    origins_skipped_breadth = 0
    origins_skipped_target = 0  # S evaluable but SPY RV21/vix_term window incomplete
    origins_degenerate = 0
    origins_scored = 0

    for u_ord in span:
        if u_ord + h > cutoff_ord:
            continue
        complete_origins += 1
        u_iso = ft.grid[u_ord]
        vin = _vintage_in_force(vintages, u_ord - h)
        if vin is None:
            origins_no_vintage += 1
            continue
        s_raw = r1.surprise_at(vin, list(range(len(roster))), u_ord - h, u_ord)
        ok = np.isfinite(s_raw)
        if int(ok.sum()) < r1.BREADTH_MIN_NAMES:
            origins_skipped_breadth += 1
            continue
        s_std = s_raw[ok] / vin.surprise_sd
        S_u = float(s_std.mean())
        DISP_u = float(np.abs(s_std).mean())
        vt = vix_term_col[u_ord]
        rv = rv21_by_ord.get(u_ord)
        if vt is None or rv is None or u_ord not in mdd21_by_ord:
            origins_skipped_target += 1
            continue
        origins_scored += 1
        rows.append(
            {
                "u": u_iso,
                "vintage": vin.quarter,
                "S_u": S_u,
                "dispersion_u": DISP_u,
                "vix_term_u": vt,
                "rv21_spy": rv,
                "mdd21_spy": mdd21_by_ord[u_ord],
                "n": int(ok.sum()),
            }
        )
        cells = vin.latent[:, u_ord][np.isfinite(s_raw)]
        if vin.quarter in seg_eval_cells:
            seg_eval_cells[vin.quarter] = np.vstack([seg_eval_cells[vin.quarter], cells])
        else:
            seg_eval_cells[vin.quarter] = cells
        if vin.quarter not in sampled_segments:
            sampled_segments.add(vin.quarter)
            sampled_surprises[u_iso] = {nm: float(s_raw[i]) for i, nm in enumerate(roster) if np.isfinite(s_raw[i])}

    if origins_scored == 0:
        raise Refused(f"{CELL_B}: 0 scored outer origins -- machinery defect, no artifact")

    S = np.array([row["S_u"] for row in rows])
    DISP = np.array([row["dispersion_u"] for row in rows])
    VT = np.array([row["vix_term_u"] for row in rows])
    RV = np.array([row["rv21_spy"] for row in rows])
    MDD = np.array([row["mdd21_spy"] for row in rows])

    collapse_reasons: list[str] = []
    for v in vintages:
        cells = seg_eval_cells.get(v.quarter)
        if cells is None or len(cells) == 0:
            continue
        eval_var = np.var(cells, axis=0, ddof=0)
        tripped = [
            int(j)
            for j in range(v.k_eff)
            if float(eval_var[j]) < r1.V3_COLLAPSE_RATIO * float(v.train_latent_var[j])
        ]
        if tripped:
            collapse_reasons.append(
                f"collapse tripwire: vintage {v.quarter} eval-fold latent variance"
                f" < {r1.V3_COLLAPSE_RATIO} x train-fold on dims {tripped}"
            )

    total_decided = origins_scored + origins_skipped_breadth + origins_skipped_target
    skip_frac = origins_skipped_breadth / total_decided if total_decided else 0.0

    boot_paired_rv = _boot_paired_one_sided(
        S, VT, RV, h, "S_u vs vix_term (incumbent) on forward RV21(SPY) — the (b) decisive pairing"
    )
    boot_paired_mdd = _boot_paired_one_sided(
        S, VT, MDD, h, "MDD21 twin: S_u vs vix_term on forward MDD21(SPY) — disclosure only"
    )
    boot_paired_disp = _boot_paired_one_sided(
        DISP, VT, RV, h, "dispersion twin: mean|std s| vs vix_term on forward RV21(SPY) — disclosure only"
    )
    nw = r1.nw_t([float(x) for x in S], h)

    tercile = _tercile_card_lift(inputs, ft, roster, vintages, h, rows)

    payload = {
        "config": {
            "config_id": cfg["config_id"],
            "variant": cfg["variant"],
            "latent_k_declared": int(cfg["latent_k"]),
            "h": h,
            "roster": f"chain-35 ({len(roster)})",
            "selected_by": "SEL-b FIXED to JF-V1-H21 (pre-declared, independent of SEL-a)",
            "aggregate_convention": (
                "S_u = cross-sectional mean of standardized s_{i,u} (vintage-pooled"
                " SD standardization); dispersion twin = cross-sectional mean |s|"
            ),
        },
        "geometry": {
            "outer_span": [OUTER_START, inputs.cutoff_iso],
            "span_sessions": N_SPAN_SESSIONS,
            "complete_origins": complete_origins,
            "origins_scored": origins_scored,
            "origins_without_vintage": origins_no_vintage,
            "origins_skipped_breadth": origins_skipped_breadth,
            "origins_skipped_target_incomplete": origins_skipped_target,
            "origins_degenerate_ic": origins_degenerate,
            "skipped_breadth_fraction": skip_frac,
            "refits": [
                {"quarter": v.quarter, "first_session": v.first_iso, "train_rows": v.n_train_rows}
                for v in vintages
            ],
            "purge_rule": f"ordinal(s) + h + 5 < ordinal(first session of Q), h = {h}",
            "surprise_dating": "indexed by completion session u, vintage in force at u-h (registered)",
            "incumbent_lag": "vix_term = VIX/VIX3M - 1 on the observation dated on or before the PREVIOUS panel-union session (INV-02), identical on challenger and incumbent",
        },
        "summary": {
            "S_u_mean": float(S.mean()),
            "S_u_sd": float(S.std(ddof=1)),
            "S_u_nw_t_lag21": nw,
            "mean_by_segment": {
                q: statistics.fmean([row["S_u"] for row in rows if row["vintage"] == q])
                for q in sorted({row["vintage"] for row in rows})
            },
            "nw_note": "Newey-West t reported for the S_u series beside, never the authority; the paired bootstrap is the (b) authority (a single-window paired rank statistic has no per-origin difference to NW-average)",
        },
        "paired_bootstrap_rv21": boot_paired_rv,
        "mdd21_twin_disclosure": {
            "mdd_convention": (
                "MDD21_u = max_{j=1..21} (1 - C^SPY_{u+j} / max(C^SPY_u, C^SPY_{u+1..u+j}))"
                " [PINNED]; identical paired machinery; a PASS with an MDD21 loss is"
                " recorded PASS with that disclosure"
            ),
            "paired_bootstrap": boot_paired_mdd,
        },
        "dispersion_twin_disclosure": {
            "convention": "cross-sectional mean of |standardized s_{i,u}| as the challenger; disclosed, never a verdict",
            "paired_bootstrap": boot_paired_disp,
        },
        "tercile_card_gate_lift_disclosure": tercile,
        "baselines_disclosed": {
            "vix_term_incumbent": {
                "rho_vs_rv21": boot_paired_rv["rho_incumbent"],
                "p_vs_zero": boot_paired_rv.get("p_incumbent_vs_zero"),
                "band_p5_p95": boot_paired_rv.get("rho_incumbent_band_p5_p95"),
                "role": "the (b) incumbent on identical origins (the paired test)",
            },
            "note": "vix_term is the (b) incumbent; XSMOM/lnRV21/|r21| controls and the randomized null are (a)-track baselines stamped on the (a) cell",
        },
        "surprise_sd_by_vintage": {v.quarter: v.surprise_sd for v in vintages},
        "not_evaluable_reasons": collapse_reasons,
        "rows": rows,
    }
    poison = sealed_run_poison(inputs, cfg, sampled_surprises)
    poison_pass = all(p["pass"] for p in poison)
    payload["future_poison"] = {
        "convention": "s recomputed from inputs truncated at u; max abs diff <= 1e-12 (round-1 machinery, sealed quarters)",
        "samples": poison,
        "pass": poison_pass,
    }
    if not poison_pass:
        payload["not_evaluable_reasons"].append("future-poison test failed")
    payload["evaluability_guards"] = {
        "complete_outer_origins_ge_126": complete_origins >= MIN_COMPLETE_ORIGINS,
        "skipped_fraction_le_10pct": skip_frac <= r1.SKIP_DEFECT_FRAC,
        "future_poison_pass": poison_pass,
        "no_latent_collapse": not collapse_reasons,
    }
    return payload


# ---- disclosed tercile card-gate lift -------------------------------------------------------


class _SealedCalendar:
    """The sealed NYSE calendar minus the 2025-01-09 phantom (tnull idiom)."""

    def __init__(self, sessions_iso: Sequence[str]) -> None:
        kept = [d for d in sessions_iso if d != r1.PHANTOM_ISO]
        self.removed = tuple(sorted(set(sessions_iso) & {r1.PHANTOM_ISO}))
        self._sessions = tuple(date.fromisoformat(d) for d in kept)
        self._ordinals = {s: i for i, s in enumerate(self._sessions)}

    def sessions(self) -> tuple[date, ...]:
        return self._sessions

    def is_session(self, d: date) -> bool:
        return d in self._ordinals

    def ordinal(self, d: date) -> int:
        return self._ordinals[d]

    def nth_after(self, d: date, n: int) -> date:
        i = self._ordinals[d] + n
        if not 0 <= i < len(self._sessions):
            raise Refused(f"{n} sessions after {d.isoformat()} is outside the calendar")
        return self._sessions[i]

    def previous_session(self, d: date) -> date | None:
        import bisect

        i = bisect.bisect_left(self._sessions, d) - 1
        return self._sessions[i] if i >= 0 else None

    def is_first_session_of_month(self, d: date) -> bool:
        if not self.is_session(d):
            return False
        prev = self.previous_session(d)
        return prev is None or (prev.year, prev.month) != (d.year, d.month)


HOLD_SESSIONS = 20
RT_PRIMARY = 0.0005  # 5bp RT (the card-lane convention)


def _tercile_card_lift(
    inputs: r1.Inputs,  # type: ignore[name-defined]
    ft: r1.FeatureTables,  # type: ignore[name-defined]
    roster: Sequence[str],
    vintages: Sequence[r1.Vintage],  # type: ignore[name-defined]
    h: int,
    b_rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """DISCLOSED, never a verdict: XSMOM-TOP3 / PEAD card outcomes (hold-20
    close-to-close, 5bp RT, complete holds only — the card-lane conventions)
    grouped by the training-fold tercile of S_u at the entry session."""
    from decimal import Decimal

    from tree_options.desk import signals as signals_mod

    cal = _SealedCalendar(
        json.loads(r1.CALENDAR_PATH.read_text(encoding="utf-8"))["sessions"]
    )
    cuts = _training_tercile_cuts(inputs, ft, roster, vintages, h)
    S_by_iso = {row["u"]: row["S_u"] for row in b_rows}
    vintage_by_iso = {row["u"]: row["vintage"] for row in b_rows}
    span_lo = date.fromisoformat(OUTER_START)
    span_hi = date.fromisoformat(inputs.cutoff_iso)

    def hold20_net(name: str, entry: date) -> float | None:
        bars = inputs.panel[name]
        entry_iso = entry.isoformat()
        if entry_iso not in bars:
            return None
        i = cal.ordinal(entry)
        window = [s.isoformat() for s in cal.sessions()[i + 1 : i + HOLD_SESSIONS + 1]]
        if len(window) != HOLD_SESSIONS or any(w not in bars for w in window):
            return None
        gross = float(
            Decimal(str(bars[window[-1]]["close"])) / Decimal(str(bars[entry_iso]["close"])) - 1
        )
        return gross - RT_PRIMARY

    def tercile_of(entry_iso: str) -> int | None:
        if entry_iso not in S_by_iso:
            return None
        q = vintage_by_iso[entry_iso]
        lo, hi = cuts[q]
        s = S_by_iso[entry_iso]
        return 1 if s <= lo else (3 if s > hi else 2)

    streams: dict[str, dict[str, Any]] = {}
    # XSMOM-TOP3 cards
    xsmom_cards: list[dict[str, Any]] = []
    for s in cal.sessions():
        if not (span_lo <= s <= span_hi) or not cal.is_first_session_of_month(s):
            continue
        res = signals_mod.xsmom_top3(inputs.panel, s, cal)
        if not res.fires:
            continue
        for name in res.top3:
            xsmom_cards.append(
                {
                    "name": name,
                    "entry": s.isoformat(),
                    "net": hold20_net(name, s),
                    "tercile": tercile_of(s.isoformat()),
                }
            )
    # PEAD cards (the desk's beat rule)
    earnings = json.loads(EARNINGS_PATH.read_text(encoding="utf-8"))
    pead_cards: list[dict[str, Any]] = []
    for s in cal.sessions():
        if not (span_lo <= s <= span_hi):
            continue
        res = signals_mod.pead_beats(inputs.panel, earnings, s, cal)
        for ev in res.beats:
            pead_cards.append(
                {
                    "name": ev.name,
                    "entry": s.isoformat(),
                    "report_date": ev.report_date,
                    "net": hold20_net(ev.name, s),
                    "tercile": tercile_of(s.isoformat()),
                }
            )

    def stream_stats(cards: list[dict[str, Any]]) -> dict[str, Any]:
        per_t = {}
        for t in (1, 2, 3):
            complete = [c["net"] for c in cards if c["tercile"] == t and c["net"] is not None]
            per_t[f"T{t}"] = {
                "n_cards": sum(1 for c in cards if c["tercile"] == t),
                "n_complete": len(complete),
                "mean_net": statistics.fmean(complete) if complete else None,
            }
        hi = per_t["T3"]["mean_net"]
        lo = per_t["T1"]["mean_net"]
        return {
            "per_tercile": per_t,
            "lift_T3_minus_T1": (hi - lo) if (hi is not None and lo is not None) else None,
            "cards_without_S_at_entry": sum(1 for c in cards if c["tercile"] is None),
            "incomplete_holds_dropped": sum(1 for c in cards if c["net"] is None),
        }

    streams["xsmom-top3"] = {"cards": xsmom_cards, **stream_stats(xsmom_cards)}
    streams["pead"] = {"cards": pead_cards, **stream_stats(pead_cards)}
    return {
        "role": "DISCLOSED, never a verdict (registration: the dispersion twin and the S_u-tercile card-gate lift are disclosed, never verdicts)",
        "tercile_cuts_training_fold": {q: list(c) for q, c in cuts.items()},
        "tercile_convention": (
            "cuts = 33.3/66.7 percentiles of the vintage-in-force TRAINING-fold S"
            " values; T1 = low S_u, T3 = high S_u; a card entered at a session"
            " without an S value (entry outside the h=21 origin set) is counted"
            " under cards_without_S_at_entry"
        ),
        "card_convention": (
            "hold-20 close-to-close net of 5bp RT, complete holds only (the"
            " card-lane conventions of the tnull baseline); xsmom-top3 = the"
            " desk signal's top 3 at first-of-month sessions; pead = the desk"
            " pead_beats rule at every outer-window session"
        ),
        "streams": streams,
    }


# ---- future-poison (round-1 machinery over the sealed quarters) -----------------------------


def sealed_run_poison(
    inputs: r1.Inputs,  # type: ignore[name-defined]
    cfg: Mapping[str, Any],
    sampled: Mapping[str, Mapping[str, float]],
) -> list[dict[str, Any]]:
    h = int(cfg["h"])
    variant = str(cfg["variant"])
    roster: tuple[str, ...] = r1.SINGLES26 if variant == "V4" else inputs.chain35
    first_iso_all = _outer_first_iso(inputs)
    out: list[dict[str, Any]] = []
    for u_iso in sorted(sampled):
        iu = inputs.ordinals[u_iso]
        targets = [
            (ym, first_iso)
            for ym, first_iso in zip(OUTER_QUARTERS, first_iso_all, strict=True)
            if inputs.ordinals[first_iso] <= iu - h
        ]
        if not targets:
            raise Refused(f"poison sample {u_iso}: no vintage in force at u-{h}")
        (year, month), first_iso = targets[-1]
        quarter = f"{year:04d}-{month:02d}"
        grid_t = [s for s in inputs.grid if s <= u_iso]
        bars_t = {
            n: {s: b for s, b in inputs.panel[n].items() if s <= u_iso}
            for n in set(inputs.chain35) | set(roster)
        }
        idx_t = {
            sym: tuple((d, v) for d, v in rows if d <= u_iso)
            for sym, rows in inputs.index_rows.items()
        }
        ft_t = r1.build_feature_tables(
            bars_t, idx_t, grid_t, tuple(sorted(set(inputs.chain35) | set(roster)))
        )
        vin_t = r1.fit_vintage(ft_t, roster, cfg, quarter, first_iso, inputs.ordinals[first_iso])
        s_t = r1.surprise_at(vin_t, list(range(len(roster))), iu - h, iu)
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
                "pass": finite and max_diff <= r1.POISON_TOL,
            }
        )
    return out


# ---- stamps / registry ----------------------------------------------------------------------


def _trial_id(config_id: str) -> str:
    return f"{SCOPE_ID}-{config_id}-r{ROUND}"


def _artifact_path(config_id: str) -> Path:
    return r1.TRIALS_DIR / f"{_trial_id(config_id)}.json"


def _verify_frozen_selection() -> dict[str, Any]:
    if _sha256_file(R1_PATH) != R1_RUNNER_SHA256:
        raise Refused("the round-1 runner's sha256 moved — the identical-path guarantee is void")
    cal_sha = _sha256_file(r1.TNULL_V3_PATH)
    if cal_sha != CALIBRATION_V3_SHA256:
        raise Refused(f"tnull calibration-v3 sha256 {cal_sha} is not the pinned {CALIBRATION_V3_SHA256}")
    sel_sha = _sha256_file(r1.SELECTION_PATH)
    if sel_sha != SELECTION_SHA256:
        raise Refused(
            f"round-1 selection sha256 {sel_sha} is not the frozen {SELECTION_SHA256}"
        )
    selection = json.loads(r1.SELECTION_PATH.read_text(encoding="utf-8"))
    if selection.get("stamp", {}).get("registration_menu_sha256") != r1._sha256_file(
        r1.REGISTRATION_PATH
    ):
        raise Refused("the frozen round-1 selection does not bind this menu sha")
    sel_a_h5 = selection["selection"]["SEL-a"]["h5"]
    if (
        sel_a_h5.get("chosen") != FROZEN_SEL_A_H5["config_id"]
        or sel_a_h5.get("mean_ic") != FROZEN_SEL_A_H5["mean_ic"]
    ):
        raise Refused(
            "the frozen round-1 SEL-a h=5 is not JF-V2-H5 with the recorded inner"
            " mean IC — the sealed cell is misbound"
        )
    sel_a_h21 = selection["selection"]["SEL-a"]["h21"]
    if sel_a_h21.get("verdict") != "FAIL" or sel_a_h21.get("chosen") is not None:
        raise Refused(
            "the frozen round-1 SEL-a h=21 is not the carried inner FAIL"
            " (wrong-sign falsifier, no outer scoring)"
        )
    if selection["selection"]["SEL-b"].get("config_id") != CELL_B:
        raise Refused("the frozen round-1 SEL-b is not FIXED to JF-V1-H21")
    return {
        "selection_sha256": sel_sha,
        "round1_runner_sha256": R1_RUNNER_SHA256,
        "calibration_v3_sha256": cal_sha,
    }


def _stamp(
    inputs: r1.Inputs, config_id: str, frozen: Mapping[str, Any]  # type: ignore[name-defined]
) -> dict[str, Any]:
    return {
        "program": "campaign-2026-09",
        "slot_id": SLOT_ID,
        "scope_id": SCOPE_ID,
        "round": ROUND,
        "round_scope": "sealed outer window (opened once, operator ruling 2026-09-24)",
        "trial_id": None if config_id == "sealed-round" else _trial_id(config_id),
        "config_id": config_id,
        "registration_menu_sha256": inputs.menu_sha256,
        "slot_doc_sha256": inputs.slot_doc_sha256,
        "protocol_raw_sha256": inputs.protocol_raw_sha256,
        "protocol_canonical_sha256": inputs.protocol_canonical_sha256,
        "dataset_manifest_hash": inputs.dataset_manifest_hash,
        "desk_repo_root": str(FROZEN_ROOT),
        "desk_repo_root_note": (
            "DESK_REPO_ROOT frozen snapshot (2026-09-24): every campaign read and"
            " write is bound here; the round-1 module's MAIN_ROOT path constants"
            " were rebound to this root before any phase ran (round-1 runner bytes"
            " still pinned by round1_runner_sha256 -- the identical-path guarantee)"
        ),
        "desk_universe_binding": dict(FROZEN_BINDING["desk_universe_binding"]),
        "dataset_pinning_sweep": dict(FROZEN_BINDING["pinning_sweep"]),
        "inputs_sha256": dict(inputs.inputs_sha256),
        "cutoff_earliest_last_session_chain35": inputs.cutoff_iso,
        "tnull_calibration_v3": {
            "path": str(r1.TNULL_V3_PATH),
            "verdict": inputs.tnull_v3.get("verdict", {}).get("slot"),
            "registration_menu_sha256": inputs.tnull_v3.get("stamp", {}).get(
                "registration_menu_sha256"
            ),
            "sha256": frozen["calibration_v3_sha256"],
        },
        "git_sha": r1._git_head(r1.REPO_ROOT),
        "runner_sha256": _sha256_file(Path(__file__).resolve()),
        "round1_runner_sha256": frozen["round1_runner_sha256"],
        "round1_selection": {
            "path": str(r1.SELECTION_PATH),
            "sha256": frozen["selection_sha256"],
            "sel_a_h5": FROZEN_SEL_A_H5,
            "sel_b": CELL_B,
        },
        "outer_window": {
            "span": [OUTER_START, inputs.cutoff_iso],
            "n_sessions_span": N_SPAN_SESSIONS,
            "complete_origins_h5": N_COMPLETE_ORIGINS[5],
            "complete_origins_h21": N_COMPLETE_ORIGINS[21],
            "opened_once_by": "operator ruling 2026-09-24 (ruling 1: sealed reads authorized)",
        },
        "generated_at": r1._utcnow().isoformat(),
    }


def _hyperparameters_sealed(
    inputs: r1.Inputs, cfg: Mapping[str, Any], frozen: Mapping[str, Any]  # type: ignore[name-defined]
) -> dict[str, Any]:
    base = r1._hyperparameters(inputs, cfg)
    base.update(
        {
            "round": ROUND,
            "round_scope": "sealed outer window",
            "sealed_round": True,
            "outer_origins": [OUTER_START, inputs.cutoff_iso],
            "outer_quarters": [f"{y:04d}-{m:02d}" for y, m in OUTER_QUARTERS],
            "complete_origins_expected": N_COMPLETE_ORIGINS[int(cfg["h"])],
            "min_complete_origins": MIN_COMPLETE_ORIGINS,
            "frozen_from_round1_selection": {
                "path": str(r1.SELECTION_PATH),
                "sha256": frozen["selection_sha256"],
                "sel_a_h5_mean_ic_is_selection_evidence_only": True,
            },
            "sealed_acceptance_criteria": (
                inputs.slot["acceptance_criteria"][0]
                if cfg["config_id"] == CELL_A
                else inputs.slot["acceptance_criteria"][1]
            ),
            "not_evaluable_guards": inputs.slot["acceptance_criteria"][2],
            "bootstrap": {
                "kind": "circular block",
                "block": int(cfg["h"]),
                "B": BOOTSTRAP_RESAMPLES,
                "seed": f"numpy.random.Generator(PCG64({BOOTSTRAP_PCG_SEED})) [PINNED], fresh generator per test",
                "p_definition": "one-sided p = #{resample stat <= 0}/valid",
            },
            "null_score_seeds": list(NULL_SCORE_SEEDS),
            "operator_rulings_2026_09_24": OPERATOR_RULINGS_2026_09_24,
        }
    )
    return base


# -- phases -----------------------------------------------------------------------------------


def phase_plan() -> int:
    t0 = time.monotonic()
    frozen = _verify_frozen_selection()
    inputs = r1.load_and_bind()
    span = _outer_span_ords(inputs)
    firsts = _outer_first_iso(inputs)
    print(f"menu sha256 {inputs.menu_sha256} (sidecar-verified); slot doc {inputs.slot_doc_sha256[:16]}...")
    print(f"protocol raw {inputs.protocol_raw_sha256[:16]}... canonical {inputs.protocol_canonical_sha256[:16]}...")
    print(f"frozen root {FROZEN_ROOT}")
    print(f"desk-universe binding: worktree bytes ({FROZEN_BINDING['desk_universe_binding']['config_panel_names']} names) = frozen 37-pin + PLTR/SPCX; universe derives from the pinned 37-name panel")
    print(f"dataset_pinning sweep: {len(FROZEN_BINDING['pinning_sweep'])} frozen inputs re-verified byte-exact")
    print(f"tnull calibration-v3: {inputs.tnull_v3['verdict']['slot']} (sha {frozen['calibration_v3_sha256'][:16]}...)")
    print(f"round-1 runner sha256 {frozen['round1_runner_sha256'][:16]}... (identical-path pin)")
    print(f"round-1 selection sha256 {frozen['selection_sha256'][:16]}... (SEL-a h=5 = JF-V2-H5 frozen; SEL-b = JF-V1-H21 fixed)")
    print(
        f"outer span: {inputs.grid[span[0]]}..{inputs.grid[span[-1]]} = {len(span)} sessions;"
        f" complete origins {N_COMPLETE_ORIGINS[5]} (h=5) / {N_COMPLETE_ORIGINS[21]} (h=21);"
        " last h=21 origin = "
        f"{inputs.grid[inputs.ordinals[inputs.cutoff_iso] - 21]}"
    )
    print(f"sealed vintages (first session): {list(zip([f'{y}-{m:02d}' for y, m in OUTER_QUARTERS], firsts))}")
    print(f"cutoff (earliest last session, chain35): {inputs.cutoff_iso}")
    print(f"cells: (a) {CELL_A} on the outer window; (b) {CELL_B} S_u vs vix_term; (a) h=21 carried FAIL (no outer run)")
    print(f"registry db: {r1.REGISTRY_PATH}; artifacts: {r1.TRIALS_DIR}")
    print("NO sealed outcome computed or viewed by this phase")
    print(f"elapsed {time.monotonic() - t0:.1f}s")
    return 0


def phase_register() -> int:
    frozen = _verify_frozen_selection()
    inputs = r1.load_and_bind()
    _outer_span_ords(inputs)  # geometry refuses before any row is written
    registry = r1._open_registry()
    try:
        scope_key = r1._scope(inputs).scope_key()
        before = registry.count_scope(scope_key)
        for cfg in (CFG_A, CFG_B):
            trial_id = _trial_id(cfg["config_id"])
            if registry.is_registered(trial_id):
                status = registry.status(trial_id)
                if status in ("COMPLETED", "FAILED"):
                    raise Refused(
                        f"{trial_id} is already scored ({status}) -- ABORT: the seal on"
                        " that cell is consumed; one scored run per cell, never overwrite"
                    )
                print(f"{trial_id}: already REGISTERED (resume skip)")
                continue
            hyper = _hyperparameters_sealed(inputs, cfg, frozen)
            record = r1.TrialRecord(
                trial_id=trial_id,
                created_at=r1._utcnow(),
                hypothesis=inputs.slot["hypothesis"],
                git_sha=r1._git_head(r1.REPO_ROOT),
                config_hash=r1._config_hash(hyper),
                dataset_manifest_hash=inputs.dataset_manifest_hash,
                train_window=None,
                validation_window=None,
                test_window=(
                    date.fromisoformat(OUTER_START),
                    date.fromisoformat(inputs.cutoff_iso),
                ),
                hyperparameters=hyper,
                scope_key=scope_key,
            )
            registry.register(record, r1._scope(inputs))
            print(f"registered {trial_id} ({'a: SEL-a h=5' if cfg is CFG_A else 'b: SEL-b fixed'})")
        after = registry.count_scope(scope_key)
        print(
            f"registry: {r1.REGISTRY_PATH}; scope rows {before} -> {after} (cap 32;"
            " the sealed rows land under the round-1 scope_key so the menu's"
            " single c09-jf budget keeps binding); NO sealed outcome computed or viewed"
        )
    finally:
        registry.close()
    return 0


def phase_execute() -> int:
    frozen = _verify_frozen_selection()
    inputs = r1.load_and_bind()
    r1.JEPA_DIR.mkdir(parents=True, exist_ok=True)
    r1.TRIALS_DIR.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(r1.LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise Refused("another jepa-filter execution holds the lock -- one run at a time") from None
        registry = r1._open_registry()
        try:
            print("building chain-35 feature tables ...", flush=True)
            ft = r1.build_feature_tables(inputs.panel, inputs.index_rows, inputs.grid, inputs.chain35)
            for cfg in (CFG_A, CFG_B):
                config_id = cfg["config_id"]
                trial_id = _trial_id(config_id)
                artifact = _artifact_path(config_id)
                status = registry.status(trial_id)
                if artifact.exists():
                    if status == "COMPLETED":
                        print(f"{trial_id}: COMPLETED already (resume skip)", flush=True)
                        continue
                    raise Refused(
                        f"{artifact} exists but trial is {status} -- inconsistent state,"
                        " refusing (one scored run per cell)"
                    )
                if status not in ("REGISTERED", "RUNNING"):
                    raise Refused(f"{trial_id} is {status}, not REGISTERED -- refusing")
                # INV-13 resume: RUNNING with NO outcome and NO artifact has not
                # consumed its seal; resume without re-marking (the provenance
                # recorded at the first mark_running still binds).
                hyper = _hyperparameters_sealed(inputs, cfg, frozen)
                if status == "REGISTERED":
                    registry.mark_running(
                        trial_id,
                        git_sha=r1._git_head(r1.REPO_ROOT),
                        config_hash=r1._config_hash(hyper),
                        dataset_manifest_hash=inputs.dataset_manifest_hash,
                        at=r1._utcnow(),
                    )
                print(f"{trial_id}: scoring the sealed outer window ...", flush=True)
                t0 = time.monotonic()
                payload = score_cell_a(inputs, ft) if cfg is CFG_A else score_cell_b(inputs, ft)
                body = {"stamp": _stamp(inputs, config_id, frozen), "payload": payload}
                artifact.write_text(
                    json.dumps(body, indent=2, sort_keys=True) + "\n", encoding="utf-8"
                )
                registry.complete(trial_id, metrics_uri=str(artifact), outcome_at=r1._utcnow())
                if cfg is CFG_A:
                    s = payload["summary"]
                    b = payload["bootstrap_mean_ic"]
                    print(
                        f"{trial_id}: COMPLETED artifact={artifact}"
                        f" mean_ic={s['mean_ic']:+.6f} nw_t={s['nw_t_lag_h']}"
                        f" origins={payload['geometry']['origins_scored']}"
                        f" p_one_sided={b['p_one_sided']:.4f}"
                        f" poison={'PASS' if payload['future_poison']['pass'] else 'FAIL'}"
                        f" ({time.monotonic() - t0:.1f}s)",
                        flush=True,
                    )
                else:
                    b = payload["paired_bootstrap_rv21"]
                    print(
                        f"{trial_id}: COMPLETED artifact={artifact}"
                        f" rho_S={b['rho_challenger']:+.6f} rho_vix={b['rho_incumbent']:+.6f}"
                        f" d={b['d_challenger_minus_incumbent']:+.6f}"
                        f" p_one_sided={b['p_one_sided']:.4f}"
                        f" origins={payload['geometry']['origins_scored']}"
                        f" poison={'PASS' if payload['future_poison']['pass'] else 'FAIL'}"
                        f" ({time.monotonic() - t0:.1f}s)",
                        flush=True,
                    )
        finally:
            registry.close()
    finally:
        os.close(lock_fd)
    return 0


def _read_sealed_artifact(
    inputs: r1.Inputs, frozen: Mapping[str, Any], config_id: str  # type: ignore[name-defined]
) -> Mapping[str, Any]:
    artifact = _artifact_path(config_id)
    body = json.loads(artifact.read_text(encoding="utf-8"))
    stamp = body.get("stamp", {})
    if (
        stamp.get("config_id") != config_id
        or stamp.get("scope_id") != SCOPE_ID
        or stamp.get("round") != ROUND
        or stamp.get("trial_id") != _trial_id(config_id)
    ):
        raise Refused(f"{artifact} does not bind {_trial_id(config_id)}")
    if stamp.get("registration_menu_sha256") != inputs.menu_sha256:
        raise Refused(f"{artifact} was executed against a different menu hash")
    if stamp.get("dataset_manifest_hash") != inputs.dataset_manifest_hash:
        raise Refused(f"{artifact} was executed against different inputs")
    if stamp.get("round1_runner_sha256") != frozen["round1_runner_sha256"]:
        raise Refused(f"{artifact} lost its identical-path pin")
    return body


def _verdict_from_guards(guards: Mapping[str, bool], reasons: Sequence[str]) -> str | None:
    if all(bool(v) for v in guards.values()) and not reasons:
        return None
    unmet = [k for k, v in guards.items() if not v]
    why = unmet or list(reasons)
    return f"NOT_EVALUABLE ({'; '.join(why)})"


def phase_stamp() -> int:
    frozen = _verify_frozen_selection()
    inputs = r1.load_and_bind()
    if SEAL_PATH.exists():
        raise Refused(f"{SEAL_PATH} already exists -- the sealed stamp is one-shot")
    art_a = _read_sealed_artifact(inputs, frozen, CELL_A)
    art_b = _read_sealed_artifact(inputs, frozen, CELL_B)
    pa, pb = art_a["payload"], art_b["payload"]

    # ---- (a) at h=5: criterion chain verbatim ----
    mean_ic = pa["summary"]["mean_ic"]
    p_a = pa["bootstrap_mean_ic"]["p_one_sided"]
    guards_a = pa["evaluability_guards"]
    ne_a = pa["not_evaluable_reasons"]
    ne_verdict_a = _verdict_from_guards(guards_a, ne_a)
    if ne_verdict_a is not None:
        verdict_a = ne_verdict_a
        reason_a = "a NOT_EVALUABLE guard tripped (see evaluability_guards)"
    elif mean_ic > 0.0 and p_a < 0.05:
        t_partial = pa["partial_ic"]["t_partial"]
        if t_partial is not None and abs(t_partial) < 1.0:
            verdict_a = "PASS-REDUNDANT"
            reason_a = (
                "PASS (mean IC > 0, one-sided circular block bootstrap p < 0.05) but the"
                f" partial IC |t| = {abs(t_partial):.4f} < 1 -- re-labeled momentum/vol,"
                " recorded, never promoted"
            )
        else:
            verdict_a = "PASS"
            reason_a = (
                "mean IC > 0 with one-sided circular block bootstrap p < 0.05 and the"
                " partial IC survives (|t| >= 1)"
            )
    else:
        verdict_a = "FAIL"
        reason_a = f"mean IC {mean_ic:+.6f}, one-sided p {p_a:.4f} (gate: mean > 0 and p < 0.05)"

    # ---- (b): paired incumbent beat, verbatim ----
    boot_b = pb["paired_bootstrap_rv21"]
    d = boot_b["d_challenger_minus_incumbent"]
    p_b = boot_b.get("p_one_sided")
    guards_b = pb["evaluability_guards"]
    ne_b = pb["not_evaluable_reasons"]
    ne_verdict_b = _verdict_from_guards(guards_b, ne_b)
    if ne_verdict_b is not None:
        verdict_b = ne_verdict_b
        reason_b = "a NOT_EVALUABLE guard tripped (see evaluability_guards)"
    elif d is not None and d > 0.0 and p_b is not None and p_b < 0.05:
        verdict_b = "PASS"
        mdd = pb["mdd21_twin_disclosure"]["paired_bootstrap"]
        mdd_note = (
            "MDD21 twin: challenger also beats the incumbent on the forward"
            " max-drawdown twin"
            if (mdd.get("d_challenger_minus_incumbent") or 0) > 0
            and (mdd.get("p_one_sided") or 1) < 0.05
            else "MDD21 twin LOSS disclosed (the forward-vol correlation remains the verdict of record)"
        )
        reason_b = (
            f"S_u beats vix_term on Spearman with forward RV21(SPY) (d = {d:+.6f},"
            f" one-sided paired circular block bootstrap p = {p_b:.4f} < 0.05); {mdd_note}"
        )
    else:
        verdict_b = "FAIL"
        reason_b = (
            f"paired d = {d if d is None else round(d, 6)}, one-sided p ="
            f" {p_b if p_b is None else round(p_b, 4)} (gate: d > 0 and p < 0.05);"
            " on FAIL vix_term remains the sole vol-regime context input and the"
            " family closes (registration declared_use)"
        )

    # ---- (a) at h=21: carried FAIL, verbatim from the frozen selection ----
    selection = json.loads(r1.SELECTION_PATH.read_text(encoding="utf-8"))
    sel_a_h21 = selection["selection"]["SEL-a"]["h21"]

    registry = r1._open_registry()
    try:
        scope_key = r1._scope(inputs).scope_key()
        rows_total = registry.count_scope(scope_key)
    finally:
        registry.close()

    record = {
        "stamp": {
            **_stamp(inputs, "sealed-round", frozen),
            "artifact_trial_ids": [_trial_id(CELL_A), _trial_id(CELL_B)],
            "round1_trial_ids": [f"{SCOPE_ID}-{c}" for c in r1.CONFIG_IDS],
            "menu_hypothesis": inputs.slot["hypothesis"],
            "acceptance_criteria": inputs.slot["acceptance_criteria"],
            "verdict_vocabulary": inputs.slot["verdict_vocabulary"],
        },
        "round": ROUND,
        "operator_rulings_2026_09_24": OPERATOR_RULINGS_2026_09_24,
        "frozen_selection": {
            "path": str(r1.SELECTION_PATH),
            "sha256": frozen["selection_sha256"],
            "sel_a_h5": FROZEN_SEL_A_H5,
            "sel_b": CELL_B,
            "inner_mean_ic_is_selection_evidence_only": True,
        },
        "verdicts": {
            "a_h5": {
                "config_id": CELL_A,
                "trial_id": _trial_id(CELL_A),
                "verdict": verdict_a,
                "verdict_reason": reason_a,
                "criterion": inputs.slot["acceptance_criteria"][0],
                "mean_ic": mean_ic,
                "nw_t": pa["summary"]["nw_t_lag_h"],
                "p_one_sided": p_a,
                "p_plus_one_sensitivity": pa["bootstrap_mean_ic"]["p_plus_one_sensitivity"],
                "no_signal_band_p5_p50_p95": pa["bootstrap_mean_ic"]["band_p5_p50_p95"],
                "origins_scored": pa["geometry"]["origins_scored"],
                "partial_ic": pa["partial_ic"],
                "evaluability_guards": guards_a,
                "not_evaluable_reasons": ne_a,
                "artifact": str(_artifact_path(CELL_A)),
            },
            "a_h21": {
                "config_id": None,
                "trial_id": None,
                "verdict": "FAIL",
                "verdict_reason": (
                    "carried from round 1 verbatim: SEL-a pre-declared rule -- no"
                    " evaluable variant has positive mean inner IC at h=21 (wrong-sign"
                    " falsifier), so the pre-declared rule scores NO outer window for"
                    " (a) at h=21; the outer window was NOT opened for this question"
                ),
                "inner_evidence": {
                    "ranked": sel_a_h21["ranked"],
                    "excluded_not_evaluable": sel_a_h21["excluded_not_evaluable"],
                    "round1_reason": sel_a_h21["reason"],
                },
            },
            "b": {
                "config_id": CELL_B,
                "trial_id": _trial_id(CELL_B),
                "verdict": verdict_b,
                "verdict_reason": reason_b,
                "criterion": inputs.slot["acceptance_criteria"][1],
                "rho_S_vs_rv21": boot_b["rho_challenger"],
                "rho_vixterm_vs_rv21": boot_b["rho_incumbent"],
                "d_challenger_minus_incumbent": d,
                "p_one_sided_paired": p_b,
                "p_plus_one_sensitivity": boot_b.get("p_plus_one_sensitivity"),
                "d_band_p5_p50_p95": boot_b.get("d_band_p5_p50_p95"),
                "origins_scored": pb["geometry"]["origins_scored"],
                "mdd21_twin": pb["mdd21_twin_disclosure"],
                "evaluability_guards": guards_b,
                "not_evaluable_reasons": ne_b,
                "artifact": str(_artifact_path(CELL_B)),
            },
        },
        "baselines_disclosed": {
            "a_track": pa["baselines_disclosed"],
            "b_track": pb["baselines_disclosed"],
        },
        "carried_records": {
            "v3_not_evaluable": {
                "configs": ["JF-V3-H5", "JF-V3-H21"],
                "status": (
                    "NOT_EVALUABLE-defective (VICReg-linear optimizer divergence"
                    " under the pinned constants); operator ruling 2026-09-24 (3)"
                    " confirms it stands; no successor registration authorized"
                ),
            },
            "flip_h21": {
                "status": (
                    "operator ruling 2026-09-24 (2): the FLIP-h21 registration is"
                    " DECLINED -- no flipped variant exists; the inner-round FLIP"
                    " assessment stays recorded as not-taken; (a) h=21 scored"
                    " exactly as registered (carried FAIL)"
                ),
            },
            "pead_deep_2": {
                "status": "operator ruling 2026-09-24 (4): stays descriptive (default accepted, no sealed read) -- not this slot",
            },
        },
        "scope_accounting": {
            "scope_id": SCOPE_ID,
            "scope_key": scope_key,
            "rows_round1": 8,
            "rows_this_round": 2,
            "rows_total_after": rows_total,
            "cap": 32,
            "note": "sealed rows share the round-1 scope_key so the menu's single 32-cap for c09-jf keeps binding",
        },
        "panel_block_history": {
            "prior_blocked_attempt": "worktree commit 9a81a2b concluded 'unrecoverable' on ohlc-panel drift vs the menu pin 0861f525...",
            "superseded_by": (
                "the pinned bytes were recovered from the Wave-1 econ lane"
                " sha-named snapshot ~/.local/state/trex-desk-w1-econ/paper-snapshot-0861f525/"
                " ohlc-panel.json (full 64-hex sha256 == the menu pin, verified) and"
                " copied into the frozen root; the block dissolves; the pin binds"
            ),
            "this_run": "every dataset_pinning sha re-verified against the frozen bytes before computing (see stamp.dataset_pinning_sweep)",
        },
        "conventions_disclosed": [
            "circular block bootstrap: ceil(n/block) blocks, starts uniform over 0..n-1 with wraparound, concatenated, truncated to n; block = h (5 for (a), 21 for the (b) targets); B = 2000",
            "the registered seed PCG64(22) starts a FRESH generator per test (each bootstrap block in the artifacts records it)",
            "one-sided p = #{resample stat <= 0}/valid; the (+1) correction is stamped as a sensitivity (vrp-cond sealed-round convention)",
            "no-signal null band = the 5/50/95 percentiles of the bootstrap resample means; the gate is 0 below the 5th percentile (equivalent to the one-sided p < 0.05 gate)",
            "partial IC: per-origin OLS of standardized surprise and forward return on {XSMOM, lnRV21} + intercept, Spearman(-res_s, res_r), SAME bootstrap; t = mean/bootstrap_se [PINNED |mean|/SE < 1]; NW t stamped beside",
            "randomized-score null: tree_options.trials.null_score (the wave-0 T-NULL hash score), seeds jepa-null-1/2/3, long-low convention on the identical origin/name cells; disclosed only",
            "(b) pairing: vix_term incumbent = VIX/VIX3M - 1 on the observation dated on or before the PREVIOUS panel-union session (INV-02), the identical lag the challenger's index block uses",
            "the dispersion twin and the S_u-tercile card-gate lift are DISCLOSED, never verdicts; the MDD21 twin is reported with identical machinery (disclosure)",
            "outer vintages continue the quarterly anchored-expanding schedule under the identical purge rule (ordinal(s)+h+5 < ordinal(first session of Q))",
        ],
        "notes": [
            "Three sealed verdicts total, exactly as registered: (a) h=5 scored now,"
            " (a) h=21 carried FAIL from the frozen inner evidence (its outer window"
            " was never opened), (b) scored now.",
            "One scored run per cell (INV-13): the two sealed trial rows were"
            " registered BEFORE any sealed outcome was computed or viewed; both"
            " per-trial artifacts are one-shot and immutable.",
            "The (a) h=5 cell sits on the SEL-a selection (inner mean IC"
            " 0.012937406031059268) — selection evidence only, never a verdict input.",
            "A PASS/PASS-REDUNDANT on (a) authorizes nothing: long-low/short-high is"
            " a scoring convention; promotion of (a) to a direction input is a"
            " separate later sealed study (declared_use).",
            "On (b) FAIL: vix_term remains the sole vol-regime context input and"
            " the family closes; a successor needs its own sealed pre-registration.",
            "FROZEN-ROOT BINDING: every campaign read and write of this round landed"
            " under DESK_REPO_ROOT=/home/alexk/.local/state/campaign-sealed-inputs/root;"
            " the live main checkout at /home/alexk/documents/tree_options was never"
            " read for campaign inputs and never written.",
            "The frozen root's data/ tree is a symlink into the main checkout's"
            " data dir as assembled by the operator; the calendar pin 7f9cccba..."
            " verifies byte-exact through it (load_and_bind re-checks the pin at"
            " bind).",
        ],
    }
    SEAL_PATH.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    for key in ("a_h5", "a_h21", "b"):
        blk = record["verdicts"][key]
        print(f"verdict {key}: {blk['verdict']} -- {blk['verdict_reason'][:160]}")
    print(f"sealed round stamped: {SEAL_PATH}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--plan", action="store_true", help="read-only bind + geometry check")
    parser.add_argument(
        "--register",
        action="store_true",
        help="INV-13: write the 2 sealed trial rows (REGISTERED, no outcome)",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="one scored run per sealed cell (resumable across cells)",
    )
    parser.add_argument(
        "--stamp",
        action="store_true",
        help="read ONLY the executed artifacts; apply the registered criteria; write sealed-round.json",
    )
    args = parser.parse_args(argv)
    try:
        if args.plan:
            return phase_plan()
        if args.register:
            return phase_register()
        if args.execute:
            return phase_execute()
        if args.stamp:
            return phase_stamp()
    except (Refused, r1.Refused) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
