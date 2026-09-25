#!/usr/bin/env python3
"""campaign-2026-09 TERM-GATE SEALED ROUND (round 2, scope ``c09-term``).

Operator authorization 2026-09-24 (ruling 1 of 4): the sealed outer window is
OPEN for the three queued families. This runner opens it ONCE for term-gate:
sealed one-block scoring of the 12 T1 cells (six gate specs x two rules) plus
the 12 T2 descriptive cells, exactly as registered (menu v3, sha256
sidecar-verified 4aca2101...; slot doc sha 2b7e0b94...).

Sealed era (menu fold_mapping.sealed, ONE block, GATE-001/XSMOM-EXITGRID
idiom): XSMOM entries 2024-10-01..2026-08-03 (23 monthly cards; the 2026-09-01
card is incomplete at panel end -- verified, excluded, forward chain); PEAD
entries 2024-10-16..2026-08-06 (141 cards with completed holds = the
published PROTOCOL-PEAD 144-card book minus its three 2024-09 cards; the
PEAD card rule is the published book's |move| >= 1.5% at the first
post-report session, long, hold 20 -- verified to reproduce 144 events over
2024-09-05..2026-08-06 and exactly 141 complete-hold cards in the sealed
window on the pinned inputs). Later cards accrue to the forward chain only.

Registered sealed acceptance (menu acceptance_criteria[0], verbatim; the v3
B-excess amendment): per T1 cell ALL four to be a CANDIDATE --
(1) ON mean - B(declared sealed window, matching book shape: xsmom-shaped
    for XSMOM legs, event-shaped for PEAD legs) > 0 (excess over drift
    baseline) and clustered-t >= 2.64 (one-sided Bonferroni, m = 12);
    B and its day-clustered sd are READ from tnull calibration-v3.json
    (sha 5d0aa0ee..., verdict CALIBRATED), never recomputed. Reading of the
    clustered-t clause (disclosed): the slot doc's pre-amendment text is
    "clustered-t(ON)" and v3 re-centered only the mean leg, so the decisive
    t is the ON cell's own day-clustered t (the round-1 statistic); a
    cross-reading block also stamps t = (ON - B)/sqrt(se_ON^2 + se_B^2) and
    flags any verdict difference.
(2) ON > OFF (per-trade net mean spread > 0, 5bp RT);
(3) gated book mean/card AND total P&L >= ungated (round-1 utility code,
    now binding on the sealed window);
(4) OFF-n >= 8 cards (XSMOM arm) / >= 20 cards (PEAD arm) else NOT_EVALUABLE.
Index outage > 5 sealed sessions = NOT_EVALUABLE rather than imputation
(verified 0 missing index sessions in the sealed window on the pinned CSVs).

Family beat-or-withdraw (acceptance_criteria[1], verbatim): best new-quantity
ON-OFF spread must beat the best incumbent spread by entry-date block
bootstrap (10,000 resamples, one-sided P < 0.05) on the same rule; parity =
WITHDRAW. Convention fixed here because the menu pins the resample count but
not the draw unit (disclosed, never silently chosen): the BLOCK IS THE SEALED
ENTRY DATE (cluster bootstrap -- each draw is one sealed card carrying all
its legs, the slot doc's "10,000 resamples of sealed entry dates"); both
gates are re-scored on the SAME resampled card multiset (paired); a resample
leaving either gate with an empty ON or OFF side is skipped (None statistic,
the vrp-cond sealed convention); one-sided p = #{resample: new <= incumbent}
/ valid. Seed not registered; fixed deterministically (sha256 of
"term-gate-sealed-bootstrap-1")) and disclosed.

Family verdict (slot section 6, pre-declared): PASS-GATE / INCUMBENT-
CONFIRMED / WITHDRAW / NOT_EVALUABLE. WITHDRAW includes "new-quantity
CANDIDATEs exist but none beats the incumbent" (vix_term-parity).

T2 (acceptance_criteria[2], descriptive-only): the frozen XSMOM-EXITGRID
grid re-derived with the regime column on its FULL era (signal dates >
2024-09-03 -- the deferred read; round 1 stamped only the holdout interim),
copy-faithfulness anchored on the published full-era table; REGIME-SIGN-FLIP
requires paired mean > 0 with conservative t >= 2 in STRESSED while <= 0 in
CALM; any cell < 100 signal dates = NOT_EVALUABLE; verdicts prefixed
DESCRIPTIVE-ONLY:; outcome conditioning on already-viewed cells, no live
change possible (XU post-hoc-amendment precedent).

SCOPE ACCOUNTING (disclosed): the registry's 32-cap is per scope_key and
round 1 committed 24 rows under the inner-fold scope_key
(campaign-2026-09/c09-term/inner-round1-inner); 24 + 24 = 48 > 32 makes
subsumption impossible. The sealed rows therefore register under the sealed
fold's own scope_key (outer_fold_id campaign-2026-09/c09-term/outer-
round2-sealed) -- the registry's own fold-derived separation, not a new
scope: the SAME 24 registered menu cells are scored, no configuration is
added, changed, or re-gridded.

Frozen inputs (2026-09-24): every read and write lands under
DESK_REPO_ROOT=/home/alexk/.local/state/campaign-sealed-inputs/root; the
round-1 module's MAIN_ROOT-derived path constants are rebound to that root
before any phase runs (round-1 runner bytes still pinned by
round1_runner_sha256 -- the identical-path guarantee). PANEL BLOCK HISTORY:
a prior sealed attempt (worktree commit 9a81a2b, vrp-cond) BLOCKED
pre-registration on ohlc-panel drift vs the menu pin, concluded
"unrecoverable"; superseded -- the pinned bytes were recovered from the
Wave-1 econ lane sha-named snapshot (full sha256 == 0861f525..., verified)
into the frozen root; the block dissolved, the pin binds.
xsmom_exitgrid.py (frozen machinery, sha 6ef943f7... = the round-1 stamp's
frozen_exitgrid_sha256) was copied byte-exact into the frozen root's
artifacts/paper-trades/ before this run -- the frozen root did not carry it;
the script resolves its panel relative to its own file, so the copy reads
the pinned panel. No other file was added to or changed in the frozen root.

Phases (run in order; the harness logs every one):
* ``--plan`` -- read-only bind + sealed card-set verification (signal-set
  facts only: no sealed return is computed or viewed).
* ``--register`` -- INV-13: write the 24 sealed trial rows (REGISTERED, no
  outcome) BEFORE any sealed outcome exists. An id found REGISTERED without
  an outcome is a resume skip; an id with an outcome is a hard ABORT (the
  seal on that cell is consumed -- one scored run per cell, never
  overwritten).
* ``--execute`` -- one-shot scored run per cell under the slot flock;
  resumable per cell (an outcome-less RUNNING cell from a crashed attempt
  has not consumed its seal and resumes without re-marking).
* ``--stamp`` -- read ONLY the executed artifacts, apply the registered
  criteria verbatim, run the family beat-or-withdraw bootstrap, write
  ``sealed-round.json`` (one-shot).
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib.util
import json
import math
import os
import random
import statistics
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any, Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parents[2]  # the EXECUTION worktree
R1_PATH = REPO_ROOT / "scripts" / "campaign" / "term_gate_run.py"

# ---- frozen-input repoint (2026-09-24; same binding as the vrp-cond sealed round) --------


DESK_REPO_ROOT_ENV = "DESK_REPO_ROOT"
MAIN_CHECKOUT = Path("/home/alexk/documents/tree_options")


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
        "artifacts/paper-trades/xsmom_exitgrid.py",
        "data/calendar/nyse_sessions_2018_01_02_2026_12_31.json",
        "artifacts/desk-store/indices/VIX.csv",
        "artifacts/desk-store/indices/VIX1Y.csv",
        "artifacts/desk-store/indices/VIX3M.csv",
        "artifacts/desk-store/indices/VIX9D.csv",
        "artifacts/campaign-2026-09/tnull/calibration-v3.json",
        "artifacts/campaign-2026-09/term-gate/inner-ranking.json",
        "artifacts/campaign-2026-09/term-gate.db",
        "desk-universe.toml",
    )
    missing = [rel for rel in required if not (frozen / rel).exists()]
    if missing:
        raise SystemExit(
            f"REFUSED: the frozen root {frozen} is missing required inputs: {missing}"
        )
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
        "config_sha256": hashlib.sha256(worktree_universe_path.read_bytes()).hexdigest(),
        "config_panel_names": len(wt_names),
        "frozen_pin_of_record": str(frozen_universe_path),
        "frozen_pin_sha256": hashlib.sha256(frozen_universe_path.read_bytes()).hexdigest(),
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
    return frozen, binding


FROZEN_ROOT, DESK_UNIVERSE_BINDING = _bind_frozen_root()

_spec = importlib.util.spec_from_file_location("term_gate_round1", R1_PATH)
r1 = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
sys.modules["term_gate_round1"] = r1
_spec.loader.exec_module(r1)  # type: ignore[union-attr]

# Rebind the round-1 module's MAIN_ROOT-derived path constants to the frozen
# root. The worktree-side pins (registration + sidecar + protocol + slot doc)
# stay on REPO_ROOT -- the registered docs are read-only truth, not frozen inputs.
r1.MAIN_ROOT = FROZEN_ROOT
r1.PANEL_PATH = FROZEN_ROOT / "artifacts" / "paper-trades" / "ohlc-panel.json"
r1.EARNINGS_PATH = FROZEN_ROOT / "artifacts" / "paper-trades" / "earnings-calendar.json"
r1.CALENDAR_PATH = FROZEN_ROOT / "data" / "calendar" / "nyse_sessions_2018_01_02_2026_12_31.json"
r1.INDICES_DIR = FROZEN_ROOT / "artifacts" / "desk-store" / "indices"
r1.EXITGRID_SCRIPT_PATH = FROZEN_ROOT / "artifacts" / "paper-trades" / "xsmom_exitgrid.py"
r1.CALIBRATION_V3_PATH = (
    FROZEN_ROOT / "artifacts" / "campaign-2026-09" / "tnull" / "calibration-v3.json"
)
r1.CAMPAIGN_DIR = FROZEN_ROOT / "artifacts" / "campaign-2026-09"
r1.REGISTRY_PATH = r1.CAMPAIGN_DIR / "term-gate.db"
r1.TERM_DIR = r1.CAMPAIGN_DIR / "term-gate"
r1.TRIALS_DIR = r1.TERM_DIR / "trials"
r1.RANKING_PATH = r1.TERM_DIR / "inner-ranking.json"
r1.LOCK_PATH = r1.TERM_DIR / "execute.lock"

ROUND = 2
TRIAL_GENERATION = 2
ROUND_LABEL = "round2-sealed"
SLOT_ID = r1.SLOT_ID
SCOPE_ID = r1.SCOPE_ID
OUTER_FOLD_SEALED = f"campaign-2026-09/{SCOPE_ID}/outer-{ROUND_LABEL}"

SEALED_WINDOW_FIRST = date(2024, 10, 1)  # declared sealed boundary
SEALED_WINDOW_LAST = date(2026, 8, 28)  # the card-era window B is stamped on
XSMOM_SEALED_FIRST, XSMOM_SEALED_LAST = date(2024, 10, 1), date(2026, 8, 3)
N_XSMOM_CARDS = 23
PEAD_SEALED_FIRST, PEAD_SEALED_LAST = date(2024, 10, 16), date(2026, 8, 6)
N_PEAD_CARDS = 141  # with completed holds (the published 144-book minus 3)
XSMOM_OFF_FLOOR = 8  # criterion 4, in OFF cards
PEAD_OFF_FLOOR = 20
FAMILY_T = 2.64  # one-sided Bonferroni, m = 12 (menu value, exact)
MAX_INDEX_OUTAGE = 5  # sealed sessions; > this = NOT_EVALUABLE, no imputation
BOOTSTRAP_RESAMPLES = 10_000
BOOTSTRAP_SEED_BYTES = b"term-gate-sealed-bootstrap-1"

# the published XSMOM-EXITGRID FULL-era table (d vs hold60, pooled over
# regimes) this runner must reproduce (copy-faithfulness anchor, full era)
EXITGRID_ANCHOR_FULL = {
    "60-skip5-tercile": {
        "hold60_n": 5364, "hold60_days": 447,
        "hold1": -0.04953, "tp100c": -0.04167, "oco100_150": -0.05019,
    },
    "252-skip21-top3": {
        "hold60_n": 1341, "hold60_days": 447,
        "hold1": -0.15748, "tp100c": -0.13617, "oco100_150": -0.15936,
    },
}
ANCHOR_TOL = 6e-4  # published at 1e-5 precision; absorbs nothing larger

# round-1 provenance pins (refuse if the world moved)
R1_RUNNER_SHA256 = "84e38dac341b0eaf1ad3ddc15acd54402c2af0d2e9e8f348dfdffaa4151e6cb2"
R1_RUNNER_SHA256_NOTE = (
    "the committed round-1 bytes (worktree commit 8fae3c7, the rank phase's"
    " stamp); the g1 execute stamps carry 82b05dd4... (an uncommitted"
    " intermediate that differs only in rank-phase bytes; the committed file"
    " is the one whose execute machinery round 1 verified against the menu)"
)
CALIBRATION_V3_SHA256 = "5d0aa0eeaea9075ec72e48a0436c2e60dd1f715092fabc4d60ee328c3811e683"
FROZEN_EXITGRID_SHA256 = "6ef943f77628e7e8ecc305b289c1862ea5bfba03241acb3c6f6bb92b4b732d7f"
INNER_RANKING_PINS = {
    "inner_fold_best": {"config_id": "T1-03", "value": -0.027426906450446823},
    "best_incumbent": {"config_id": "T1-05", "spread_on_minus_off": -0.06580522927292004},
    "primary": "T1-01",
}

SEAL_PATH = r1.TERM_DIR / "sealed-round.json"

OPERATOR_RULINGS_2026_09_24 = {
    "1": "sealed reads authorized for all three queued families (operative here)",
    "2": "JEPA FLIP-h21 registration DECLINED — no new variant, score exactly as registered (jepa-filter slot)",
    "3": "JEPA V3 divergence ruled NOT_EVALUABLE-defective, stands; no successor registration authorized (jepa-filter slot)",
    "4": "pead-deep-2 stays descriptive (default accepted, no sealed read) (pead-deep-2 slot)",
}

T1_CELLS = r1.T1_CELLS
T2_CELLS = r1.T2_CELLS
ALL_CELLS = r1.ALL_CELLS
CONFIG_IDS = r1.CONFIG_IDS


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _trial_id(config_id: str) -> str:
    return f"{SCOPE_ID}-{config_id}-g{TRIAL_GENERATION}"


def _artifact_path(config_id: str) -> Path:
    return r1.TRIALS_DIR / f"{_trial_id(config_id)}.json"


# ---- frozen-input verification ------------------------------------------------------------


def _verify_frozen_inputs() -> dict[str, Any]:
    if _sha256_file(R1_PATH) != R1_RUNNER_SHA256:
        raise r1.Refused(
            "the round-1 runner's sha256 moved — the identical-path guarantee is void"
        )
    cal_sha = _sha256_file(r1.CALIBRATION_V3_PATH)
    if cal_sha != CALIBRATION_V3_SHA256:
        raise r1.Refused(
            f"tnull calibration-v3 sha256 {cal_sha} is not the pinned {CALIBRATION_V3_SHA256}"
        )
    grid_sha = _sha256_file(r1.EXITGRID_SCRIPT_PATH)
    if grid_sha != FROZEN_EXITGRID_SHA256:
        raise r1.Refused(
            f"frozen xsmom_exitgrid.py sha256 {grid_sha} is not the round-1-stamped"
            f" {FROZEN_EXITGRID_SHA256}"
        )
    ranking = json.loads(r1.RANKING_PATH.read_text(encoding="utf-8"))
    if ranking["stamp"]["registration_menu_sha256"] != _sha256_file(r1.REGISTRATION_PATH):
        raise r1.Refused("the frozen inner-ranking.json does not bind this menu sha")
    if (
        ranking["inner_fold_best"]["config_id"] != INNER_RANKING_PINS["inner_fold_best"]["config_id"]
        or ranking["inner_fold_best"]["value"] != INNER_RANKING_PINS["inner_fold_best"]["value"]
    ):
        raise r1.Refused("the frozen inner-ranking.json best-new pin drifted")
    if (
        ranking["best_incumbent"]["config_id"] != INNER_RANKING_PINS["best_incumbent"]["config_id"]
        or ranking["best_incumbent"]["spread_on_minus_off"]
        != INNER_RANKING_PINS["best_incumbent"]["spread_on_minus_off"]
    ):
        raise r1.Refused("the frozen inner-ranking.json best-incumbent pin drifted")
    return {
        "round1_runner_sha256": R1_RUNNER_SHA256,
        "round1_runner_sha256_note": R1_RUNNER_SHA256_NOTE,
        "calibration_v3_sha256": cal_sha,
        "frozen_exitgrid_sha256": grid_sha,
        "inner_ranking_sha256": _sha256_file(r1.RANKING_PATH),
        "inner_ranking": ranking,
    }


# ---- sealed card sets (signal-set facts; NO returns computed here) ------------------------


def _sealed_xsmom_cards(inputs: r1.Inputs) -> tuple[list[tuple[str, tuple[str, ...]]], dict[str, Any]]:  # type: ignore[name-defined]
    from tree_options.desk.signals import xsmom_top3

    cal, panel = inputs.calendar, inputs.panel
    cards: list[tuple[str, tuple[str, ...]]] = []
    for s in cal.sessions():
        if s < XSMOM_SEALED_FIRST or s > XSMOM_SEALED_LAST:
            continue
        if not cal.is_first_session_of_month(s):
            continue
        res = xsmom_top3(panel, s, cal)
        if res.fires:
            cards.append((s.isoformat(), tuple(res.top3)))
    notes: dict[str, Any] = {}
    if len(cards) != N_XSMOM_CARDS or cards[0][0] != "2024-10-01" or cards[-1][0] != "2026-08-03":
        raise r1.Refused(
            f"sealed XSMOM cards are not the registered 23 (2024-10-01..2026-08-03):"
            f" got {len(cards)} {cards[0][0] if cards else '-'}..{cards[-1][0] if cards else '-'}"
        )
    # the excluded 2026-09-01 FOM card: verify it fires but its hold is
    # incomplete at panel end (structural bar presence only, no returns)
    for s in cal.sessions():
        if s > SEALED_WINDOW_LAST and cal.is_first_session_of_month(s):
            res = xsmom_top3(panel, s, cal)
            i = cal.ordinal(s)
            window = [x.isoformat() for x in cal.sessions()[i + 1 : i + r1.HOLD_SESSIONS + 1]]
            complete = [
                all(w in panel.get(n, {}) for w in window) and len(window) == r1.HOLD_SESSIONS
                for n in res.top3
            ]
            notes["excluded_incomplete_fom"] = {
                "session": s.isoformat(),
                "fires": res.fires,
                "all3_holds_complete": all(complete) if res.fires else None,
                "hold_window_last": window[-1] if len(window) == r1.HOLD_SESSIONS else None,
                "note": "incomplete at panel end; accrues to the forward chain only",
            }
            break
    if notes["excluded_incomplete_fom"]["all3_holds_complete"]:
        raise r1.Refused(
            "the FOM after the sealed window unexpectedly holds complete -- the"
            " registration's exclusion premise drifted"
        )
    return cards, notes


def _sealed_pead_cards(inputs: r1.Inputs) -> tuple[list[dict[str, Any]], dict[str, Any]]:  # type: ignore[name-defined]
    """The sealed PEAD card set: the PROTOCOL-PEAD book definition (|move| >=
    1.5% at the first post-report session, long, hold 20) restricted to
    entries 2024-10-16..2026-08-06 with completed holds; the registration's
    141 = the published 144-card book minus its three 2024-09 cards.
    Completeness is structural (bar presence), no returns computed here."""
    from tree_options.desk.signals import pead_beats

    cal, panel = inputs.calendar, inputs.panel
    sessions = cal.sessions()
    cards: list[dict[str, Any]] = []
    incomplete = 0
    forward = 0
    pre_window = 0
    whole_era = 0
    for s in sessions:
        if s > date(2026, 9, 23):  # panel end
            break
        res = pead_beats(panel, inputs.earnings, s, cal)
        big = [ev for ev in res.evaluated if ev.reason in ("beat", "miss_proxy")]
        if date(2024, 9, 5) <= s <= PEAD_SEALED_LAST:
            whole_era += len(big)
        for ev in big:
            i = cal.ordinal(s)
            window = [x.isoformat() for x in sessions[i + 1 : i + r1.HOLD_SESSIONS + 1]]
            bars = panel.get(ev.name, {})
            ok = len(window) == r1.HOLD_SESSIONS and all(w in bars for w in window)
            if s < PEAD_SEALED_FIRST or s > PEAD_SEALED_LAST:
                if s > PEAD_SEALED_LAST:
                    forward += 1
                elif s < date(2024, 10, 1):
                    pre_window += 1
                continue
            if ok:
                cards.append(
                    {
                        "entry": s.isoformat(),
                        "name": ev.name,
                        "report_date": ev.report_date,
                        "move": float(ev.move) if ev.move is not None else None,
                        "side": ev.reason,  # beat = up-move, miss_proxy = down-move
                    }
                )
            else:
                incomplete += 1
    if len(cards) != N_PEAD_CARDS or cards[0]["entry"] != "2024-10-16" or cards[-1]["entry"] != "2026-08-06":
        raise r1.Refused(
            f"sealed PEAD cards are not the registered 141 with completed holds"
            f" (2024-10-16..2026-08-06): got {len(cards)}"
            f" {cards[0]['entry'] if cards else '-'}..{cards[-1]['entry'] if cards else '-'}"
        )
    if whole_era != 144:
        raise r1.Refused(
            f"the pinned inputs yield {whole_era} big-surprise events over the published"
            " book's era 2024-09-05..2026-08-06, not 144 -- the card rule drifted"
        )
    notes = {
        "card_rule": (
            "PROTOCOL-PEAD book: |move| >= 1.5% at the first post-report session"
            " (pead_beats reasons beat|miss_proxy), long entry close[t], hold 20;"
            " the published 144-card book minus its three 2024-09 cards"
        ),
        "in_window_incomplete_holds": incomplete,
        "forward_chain_events_after_2026-08-06": forward,
        "pre_window_big_surprise_events": pre_window,
    }
    return cards, notes


def _sealed_index_gaps(inputs: r1.Inputs) -> list[str]:  # type: ignore[name-defined]
    gset = set(inputs.gates.sessions)
    return [
        s.isoformat()
        for s in inputs.calendar.sessions()
        if SEALED_WINDOW_FIRST <= s <= SEALED_WINDOW_LAST and s not in gset
    ]


# ---- T1 sealed scoring --------------------------------------------------------------------


def _card_rows(inputs: r1.Inputs, name: str, entry: date) -> dict[str, Any]:  # type: ignore[name-defined]
    t = r1._complete_trade(inputs, name, entry)
    return {
        "entry": t.entry,
        "exit": t.exit_session,
        "name": t.name,
        "gross": t.gross,
        "detail": t.detail,
    }


def run_t1_sealed(
    inputs: r1.Inputs,  # type: ignore[name-defined]
    cell: Mapping[str, str],
    card_sets: Mapping[str, Any],
    index_gaps: Sequence[str],
) -> dict[str, Any]:
    rule = cell["rule"]
    shape = "xsmom" if rule == "xsmom_top3" else "event"
    B = inputs.B[shape]
    floor = XSMOM_OFF_FLOOR if rule == "xsmom_top3" else PEAD_OFF_FLOOR
    rows_on: list[dict[str, Any]] = []
    rows_off: list[dict[str, Any]] = []
    readings: dict[str, dict[str, Any]] = {}  # gate reading per ENTRY DATE
    card_records: list[dict[str, Any]] = []  # one record per CARD (cards, not dates)
    n_gap_readings = 0

    def _reading(entry: date) -> dict[str, Any]:
        nonlocal n_gap_readings
        key = entry.isoformat()
        if key not in readings:
            r = r1.read_gate(inputs, cell["gate"], cell["style"], entry)
            if not r.lag_is_prev_session:
                n_gap_readings += 1
            readings[key] = {
                "lag": r.lag_iso,
                "value": r.value,
                "threshold": r.threshold,
                "off": r.off,
                "trail_n": r.trail_n,
            }
        return readings[key]

    if rule == "xsmom_top3":
        for entry_iso, top3 in card_sets["xsmom"]:
            entry = date.fromisoformat(entry_iso)
            rd = _reading(entry)
            legs = [_card_rows(inputs, name, entry) for name in top3]
            (rows_off if rd["off"] else rows_on).extend(t for t in legs if t["gross"] is not None)
            card_records.append(
                {
                    "key": entry_iso,
                    "entry": entry_iso,
                    "top3": list(top3),
                    "legs": legs,
                    **{k: v for k, v in rd.items()},
                }
            )
        cards_block = [
            {k: v for k, v in c.items() if k != "legs" and k != "key"} for c in card_records
        ]
    else:
        # PEAD cards: one card per report (a date can carry several cards --
        # e.g. 2024-10-30 fired AMD/GOOGL/V); the CARD is the book unit, the
        # entry date is only the day-cluster key
        for card in card_sets["pead"]:
            entry = date.fromisoformat(card["entry"])
            rd = _reading(entry)
            legs = [_card_rows(inputs, card["name"], entry)]
            (rows_off if rd["off"] else rows_on).extend(t for t in legs if t["gross"] is not None)
            card_records.append(
                {
                    "key": f"{card['entry']}|{card['name']}|{card['report_date']}",
                    "entry": card["entry"],
                    "name": card["name"],
                    "side": card["side"],
                    "legs": legs,
                    **{k: v for k, v in rd.items()},
                }
            )
        cards_block = [
            {k: v for k, v in c.items() if k != "legs" and k != "key"} for c in card_records
        ]
    on = r1._cell_stats(rows_on, r1.RT_PRIMARY)
    off = r1._cell_stats(rows_off, r1.RT_PRIMARY)
    spread = (
        on["net_per_trade_mean"] - off["net_per_trade_mean"]
        if on["net_per_trade_mean"] is not None and off["net_per_trade_mean"] is not None
        else None
    )
    off_cards = sum(1 for c in card_records if c["off"])
    on_cards = len(card_records) - off_cards
    # criterion 3 (utility) -- the round-1 computation (per-CARD mean of leg
    # nets; totals over legs), now binding on the sealed window
    card_nets: dict[str, tuple[float, int]] = {}
    for c in card_records:
        net = sum(t["gross"] - r1.RT_PRIMARY for t in c["legs"] if t["gross"] is not None)
        n = sum(1 for t in c["legs"] if t["gross"] is not None)
        if n:
            card_nets[c["key"]] = (net, n)
    ungated_mean_per_card = (
        statistics.fmean(v / n for v, n in card_nets.values()) if card_nets else None
    )
    off_by_key = {c["key"]: c["off"] for c in card_records}
    on_card_nets = {k: vn for k, vn in card_nets.items() if not off_by_key[k]}
    gated_mean_per_card = (
        statistics.fmean(v / n for v, n in on_card_nets.values()) if on_card_nets else None
    )
    ungated_total = sum(v for v, _n in card_nets.values())
    gated_total = sum(v for v, _n in on_card_nets.values())
    # criterion 1: B-excess + clustered t
    on_mean = on["net_per_trade_mean"]
    b_excess = on_mean - B["B_net_per_trade_mean"] if on_mean is not None else None
    t_on = on["net_clustered_t"]
    se_on = on["per_trade_mean_sd_day_clustered"]
    se_b = B["per_trade_mean_sd_day_clustered"]
    if b_excess is not None and se_on is not None and se_on > 0:
        t_cross = b_excess / math.sqrt(se_on * se_on + se_b * se_b)
    else:
        t_cross = None
    crit: dict[str, Any] = {
        "1a_on_minus_B_gt_0": b_excess is not None and b_excess > 0.0,
        "1b_clustered_t_on_ge_2.64": t_on is not None and t_on == t_on and t_on >= FAMILY_T,
        "2_on_gt_off": spread is not None and spread > 0.0,
        "3a_gated_mean_per_card_ge_ungated": (
            gated_mean_per_card is not None
            and ungated_mean_per_card is not None
            and gated_mean_per_card >= ungated_mean_per_card
        ),
        "3b_gated_total_pnl_ge_ungated": gated_total >= ungated_total,
        "4_off_n_ge_floor": off_cards >= floor,
    }
    outage = len(index_gaps)
    if outage > MAX_INDEX_OUTAGE:
        verdict = "NOT_EVALUABLE"
        reason = f"index outage {outage} > {MAX_INDEX_OUTAGE} sealed sessions (no imputation)"
    elif not crit["4_off_n_ge_floor"]:
        verdict = "NOT_EVALUABLE"
        reason = f"power floor unmet: OFF-n {off_cards} < {floor} cards (never FAIL)"
    elif on_cards == 0 or on_mean is None:
        verdict = "NOT_EVALUABLE"
        reason = "empty ON side (no evaluable kept cards) -- never FAIL"
    elif all(
        (
            crit["1a_on_minus_B_gt_0"],
            crit["1b_clustered_t_on_ge_2.64"],
            crit["2_on_gt_off"],
            crit["3a_gated_mean_per_card_ge_ungated"],
            crit["3b_gated_total_pnl_ge_ungated"],
        )
    ):
        verdict = "CANDIDATE"
        reason = "all four sealed criteria hold (candidate; the family verdict is separate)"
    else:
        verdict = "NOT_CANDIDATE"
        reason = "at least one sealed criterion missed"
    # cross-reading disclosure: criterion 1's t on the B-excess with combined se
    cross_all = (
        crit["1a_on_minus_B_gt_0"]
        and t_cross is not None
        and t_cross == t_cross
        and t_cross >= FAMILY_T
        and crit["2_on_gt_off"]
        and crit["3a_gated_mean_per_card_ge_ungated"]
        and crit["3b_gated_total_pnl_ge_ungated"]
    )
    alt_verdict = (
        "NOT_EVALUABLE"
        if (outage > MAX_INDEX_OUTAGE or not crit["4_off_n_ge_floor"] or on_cards == 0)
        else ("CANDIDATE" if cross_all else "NOT_CANDIDATE")
    )
    return {
        "arm": "T1",
        "config": dict(cell),
        "cards": cards_block,
        "on": on,
        "off": off,
        "spread_on_minus_off": spread,
        "spread15_on_minus_off": (
            r1._cell_stats(rows_on, r1.RT_ROBUST)["net_per_trade_mean"]
            - r1._cell_stats(rows_off, r1.RT_ROBUST)["net_per_trade_mean"]
            if rows_on and rows_off
            else None
        ),
        "card_counts": {"on_cards": on_cards, "off_cards": off_cards, "total": len(readings)},
        "B": B,
        "criterion_1": {
            "on_net_per_trade_mean": on_mean,
            "B_net_per_trade_mean": B["B_net_per_trade_mean"],
            "B_excess_on_minus_B": b_excess,
            "clustered_t_on": t_on,
            "threshold_t": FAMILY_T,
            "decisive_reading": (
                "clustered-t(ON) per the slot doc's pre-amendment clause; v3"
                " re-centered only the mean leg on B"
            ),
            "cross_reading_t_excess_combined_se": t_cross,
            "cross_reading_verdict": alt_verdict,
            "cross_reading_would_change_verdict": alt_verdict != verdict,
        },
        "utility_criterion_3": {
            "gated_mean_per_card": gated_mean_per_card,
            "ungated_mean_per_card": ungated_mean_per_card,
            "gated_total_pnl_usd_per_2500_leg": 2500.0 * gated_total,
            "ungated_total_pnl_usd_per_2500_leg": 2500.0 * ungated_total,
        },
        "off_floor": {"required_off_cards": floor, "off_cards": off_cards, "off_days_complete": off["days"], "off_legs_complete": off["n_complete"]},
        "criteria": crit,
        "index_outage_sealed_sessions": outage,
        "index_gap_readings": n_gap_readings,
        "sealed_verdict": verdict,
        "sealed_verdict_reason": reason,
        "sealed_window_scored": True,
    }


# ---- T2 sealed (full era) ------------------------------------------------------------------


def _t2_core_sealed(inputs: r1.Inputs) -> tuple[dict[str, Any], dict[str, Any]]:  # type: ignore[name-defined]
    """One pass over the frozen grid's FULL era (signal dates > 2024-09-03 --
    the deferred read) for all three variants of both constructions, split by
    the r93 regime. Copy-faithfulness anchored on the published full-era
    table."""
    frozen = r1._load_frozen_exitgrid()
    series = frozen.load_panel()
    variants = ("hold1", "tp100c", "oco100_150")
    policies = {p.key: p for p in frozen.POLICIES if p.key in variants}
    if set(policies) != set(variants):
        raise r1.Refused("the frozen grid no longer carries the three declared T2 variants")
    stats: dict[str, Any] = {}
    anchor: dict[str, Any] = {}
    holdout_end = date.fromisoformat(r1.INNER_TUNING_END)
    for label, lb, sk, tk in frozen.CONSTRUCTIONS:
        leg_all, _everything = frozen.signals_for(series, lb, sk, tk)
        leg = [s for s in leg_all if date.fromisoformat(s[0]) > holdout_end]
        h60 = {
            (d, n): series[n][1][p + frozen.HOLD] / entry - 1 - frozen.RT
            for d, n, p, entry in leg
        }
        regimes = {d: r1.regime_of(inputs, date.fromisoformat(d)) for d, _n, _p, _e in leg}
        per_variant: dict[str, Any] = {}
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
                    "hit": sum(1 for x in diffs if x > 0) / len(diffs) if diffs else float("nan"),
                }
            per_variant[key] = cells
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
                "published_d_mean": EXITGRID_ANCHOR_FULL[label][key],
                "abs_drift": abs(statistics.fmean(pooled_diffs) - EXITGRID_ANCHOR_FULL[label][key]),
            }
        base_n = len(set(d for d, _n, _p, _e in leg))
        want = EXITGRID_ANCHOR_FULL[label]
        if len(leg) != want["hold60_n"] or base_n != want["hold60_days"]:
            raise r1.Refused(
                f"{label}: full-era hold-60 signal set ({len(leg)} signals / {base_n} days)"
                f" != the published XSMOM-EXITGRID full table"
                f" ({want['hold60_n']} / {want['hold60_days']}) -- the panel or the"
                " frozen script drifted"
            )
        for key in variants:
            drift = anchor[label][key]["abs_drift"]
            if not (drift <= ANCHOR_TOL):
                raise r1.Refused(
                    f"{label}/{key}: pooled full-era d_mean drift {drift:.6f} > {ANCHOR_TOL}"
                    f" vs the published {want[key]} -- the re-derivation is not copy-faithful"
                )
        stats[label] = per_variant
    return stats, anchor


def run_t2_sealed(
    inputs: r1.Inputs,  # type: ignore[name-defined]
    cell: Mapping[str, str],
    t2_stats: tuple[dict[str, Any], dict[str, Any]],
) -> dict[str, Any]:
    stats, anchor = t2_stats
    construction, variant, regime = cell["construction"], cell["variant"], cell["regime"]
    cellstats = stats[construction][variant][regime]
    pair = stats[construction][variant]
    if cellstats["days"] < r1.T2_MIN_DATES or pair["CALM"]["days"] < r1.T2_MIN_DATES:
        verdict = "DESCRIPTIVE-ONLY:NOT_EVALUABLE"
        reason = f"cell signal dates {cellstats['days']} (or pair) < {r1.T2_MIN_DATES}"
    else:
        stressed = pair["STRESSED"]
        calm = pair["CALM"]
        flips = (
            stressed["d_mean"] > 0
            and stressed["t_cons"] == stressed["t_cons"]
            and stressed["t_cons"] >= r1.T2_FLIP_T
            and calm["d_mean"] <= 0
        )
        verdict = "DESCRIPTIVE-ONLY:REGIME-SIGN-FLIP" if flips else "DESCRIPTIVE-ONLY:NO-REGIME-SIGNAL"
        reason = (
            "paired mean > 0 with conservative t >= 2 in STRESSED while <= 0 in CALM"
            if flips
            else "no variant shows the stressed-only rescue pattern on the full-era read"
        )
    return {
        "arm": "T2",
        "config": dict(cell),
        "era": f"signal dates > {r1.INNER_TUNING_END} (the frozen grid's full era -- the deferred read)",
        "cell": cellstats,
        "pair": {"CALM": pair["CALM"], "STRESSED": pair["STRESSED"]},
        "pooled_full_era_anchor": anchor[construction][variant],
        "power_floor_dates": r1.T2_MIN_DATES,
        "round1_interim_holdout_verdict_crossref": (
            "the round-1 interim read (holdout era, signal dates <= 2024-09-03) stamped"
            " DESCRIPTIVE-ONLY:NO-REGIME-SIGNAL for all 12 cells; this sealed stamp is"
            " the registered full-era read"
        ),
        "sealed_verdict": verdict,
        "sealed_verdict_reason": reason,
        "sealed_window_scored": True,
    }


# ---- family bootstrap -----------------------------------------------------------------------


def _spread_of_gate(card_nets_by_entry: Mapping[str, list[float]], off_flag: Mapping[str, bool]) -> float | None:
    on_vals: list[float] = []
    off_vals: list[float] = []
    for e, nets in card_nets_by_entry.items():
        (on_vals if not off_flag[e] else off_vals).extend(nets)
    if not on_vals or not off_vals:
        return None
    return statistics.fmean(on_vals) - statistics.fmean(off_vals)


def _family_bootstrap(
    rule: str,
    entries: Sequence[str],
    nets_by_entry: Mapping[str, list[float]],
    off_new: Mapping[str, bool],
    off_inc: Mapping[str, bool],
) -> dict[str, Any]:
    seed = int.from_bytes(hashlib.sha256(BOOTSTRAP_SEED_BYTES).digest(), "big")
    n = len(entries)
    point_new = _spread_of_gate(nets_by_entry, off_new)
    point_inc = _spread_of_gate(nets_by_entry, off_inc)
    out: dict[str, Any] = {
        "registered_parameters": {
            "resamples": BOOTSTRAP_RESAMPLES,
            "sided": "one (H1: new spread > incumbent spread)",
            "alpha": 0.05,
        },
        "conventions_disclosed": {
            "unit": (
                "the sealed entry date is the block: each draw is one sealed card"
                " of the rule carrying all its legs (the slot doc's '10,000"
                " resamples of sealed entry dates'); cluster bootstrap"
            ),
            "pairing": "both gates are re-scored on the SAME resampled card multiset",
            "undefined_resamples": (
                "skipped (either gate left with an empty ON or OFF side = None statistic)"
            ),
            "p_definition": "p = #{resample: spread_new <= spread_inc} / valid resamples",
            "seed_bytes": BOOTSTRAP_SEED_BYTES.decode(),
            "seed": seed,
            "seed_registered": False,
            "seed_note": (
                "the menu pins the resample count but no seed; fixed"
                " deterministically here and disclosed"
            ),
        },
        "rule": rule,
        "n_cards": n,
        "point_spread_new": point_new,
        "point_spread_incumbent": point_inc,
        "valid_resamples": 0,
        "new_le_inc": 0,
        "p_one_sided": None,
        "p_plus_one_sensitivity": None,
    }
    if point_new is None or point_inc is None:
        out["reason"] = "a gate's point spread is undefined (empty ON or OFF side)"
        return out
    rng = random.Random(seed)
    valid = 0
    le = 0
    for _ in range(BOOTSTRAP_RESAMPLES):
        drawn = [entries[rng.randrange(n)] for _ in range(n)]
        on_n: list[float] = []
        off_n: list[float] = []
        on_i: list[float] = []
        off_i: list[float] = []
        for e in drawn:
            nets = nets_by_entry[e]
            if off_new[e]:
                off_n.extend(nets)
            else:
                on_n.extend(nets)
            if off_inc[e]:
                off_i.extend(nets)
            else:
                on_i.extend(nets)
        if not on_n or not off_n or not on_i or not off_i:
            continue
        s_new = statistics.fmean(on_n) - statistics.fmean(off_n)
        s_inc = statistics.fmean(on_i) - statistics.fmean(off_i)
        valid += 1
        if s_new <= s_inc:
            le += 1
    out["valid_resamples"] = valid
    out["new_le_inc"] = le
    if valid:
        out["p_one_sided"] = le / valid
        out["p_plus_one_sensitivity"] = (le + 1) / (valid + 1)
    else:
        out["reason"] = "no valid resample produced defined spreads for both gates"
    return out


# ---- stamps / registry -----------------------------------------------------------------------


def _stamp(inputs: r1.Inputs, cell: Mapping[str, Any], frozen: Mapping[str, Any]) -> dict[str, Any]:  # type: ignore[name-defined]
    return {
        "program": "campaign-2026-09",
        "slot_id": SLOT_ID,
        "scope_id": SCOPE_ID,
        "round": ROUND_LABEL,
        "trial_generation": TRIAL_GENERATION,
        "trial_id": _trial_id(cell["id"]) if cell.get("id") else None,
        "config_id": cell.get("id"),
        "registration_menu_sha256": inputs.menu_sha256,
        "slot_doc_sha256": inputs.slot_doc_sha256,
        "protocol_raw_sha256": inputs.protocol_raw_sha256,
        "protocol_canonical_sha256": inputs.protocol_canonical_sha256,
        "dataset_manifest_hash": inputs.dataset_manifest_hash,
        "calibration_v3_sha256": frozen["calibration_v3_sha256"],
        "calibration_v3_verdict": inputs.calibration["verdict"]["slot"],
        "frozen_exitgrid_sha256": frozen["frozen_exitgrid_sha256"],
        "inner_ranking_sha256": frozen["inner_ranking_sha256"],
        "round1_runner_sha256": frozen["round1_runner_sha256"],
        "runner_sha256": _sha256_file(Path(__file__).resolve()),
        "desk_repo_root": str(FROZEN_ROOT),
        "desk_repo_root_note": (
            "DESK_REPO_ROOT frozen snapshot (2026-09-24): every campaign read and"
            " write is bound here; the round-1 module's MAIN_ROOT path constants"
            " were rebound to this root before any phase ran (round-1 runner bytes"
            " still pinned by round1_runner_sha256 -- the identical-path guarantee)"
        ),
        "desk_universe_binding": dict(DESK_UNIVERSE_BINDING),
        "inputs_sha256": {
            "ohlc-panel.json": inputs.panel_sha256,
            "earnings-calendar.json": inputs.earnings_sha256,
            "nyse_sessions json": inputs.calendar_sha256,
            **{f"indices/{k}": v for k, v in inputs.indices_sha256.items()},
            "slots/term-gate.md": inputs.slot_doc_sha256,
            "tnull/calibration-v3.json": frozen["calibration_v3_sha256"],
            "xsmom_exitgrid.py": frozen["frozen_exitgrid_sha256"],
            "inner-ranking.json": frozen["inner_ranking_sha256"],
        },
        "sealed_window": {
            "xsmom_entries": [XSMOM_SEALED_FIRST.isoformat(), XSMOM_SEALED_LAST.isoformat()],
            "n_xsmom_cards": N_XSMOM_CARDS,
            "pead_entries": [PEAD_SEALED_FIRST.isoformat(), PEAD_SEALED_LAST.isoformat()],
            "n_pead_cards": N_PEAD_CARDS,
            "window": [SEALED_WINDOW_FIRST.isoformat(), SEALED_WINDOW_LAST.isoformat()],
            "opened_once_by": "operator ruling 2026-09-24 (sealed reads authorized)",
        },
        "git_sha": r1._git_head(r1.REPO_ROOT),
        "generated_at": r1._utcnow().isoformat(),
    }


def _scope_sealed(inputs: r1.Inputs) -> r1.TrialScope:  # type: ignore[name-defined]
    return r1.TrialScope(
        protocol_id="tree_options",
        protocol_hash=inputs.protocol_canonical_sha256,
        outer_fold_id=OUTER_FOLD_SEALED,
        target_horizon="hold20:t1,hold60:t2",
        feature_set_id="ohlc-panel|cboe-term-structure|v1",
        model_family=r1.MODEL_FAMILY,
    )


def _hyperparameters_sealed(
    inputs: r1.Inputs,  # type: ignore[name-defined]
    cell: Mapping[str, Any],
    frozen: Mapping[str, Any],
    criteria_text: str,
) -> dict[str, Any]:
    hyper: dict[str, Any] = {
        "scope_id": SCOPE_ID,
        "slot_id": SLOT_ID,
        "config_id": cell["id"],
        "arm": cell["arm"],
        "model_family": r1.MODEL_FAMILY,
        "round": ROUND_LABEL,
        "sealed_round": True,
        "role": cell.get("role"),
        "lane": "card-lane (equity close-to-close)",
        "hold_sessions": r1.HOLD_SESSIONS if cell["arm"] == "T1" else r1.EXITGRID_HOLD,
        "rt_primary_bp": 5,
        "rt_robust_bp": 15,
        "trailing_sessions": r1.TRAILING_SESSIONS,
        "q60_percentile": r1.Q60_PCT,
        "q60_method": "linear interpolation (statistics quantiles inclusive)",
        "calendar_exclusions": [r1.PHANTOM_ISO],
        "windows": {
            "sealed_xsmom": [XSMOM_SEALED_FIRST.isoformat(), XSMOM_SEALED_LAST.isoformat()],
            "sealed_pead": [PEAD_SEALED_FIRST.isoformat(), PEAD_SEALED_LAST.isoformat()],
            "sealed_window_end": SEALED_WINDOW_LAST.isoformat(),
            "excluded_incomplete_2026_09_fom": True,
            "forward_chain": "cards after the sealed boundaries accrue to the forward chain only",
        },
        "pead_card_rule": (
            "PROTOCOL-PEAD book: |move| >= 1.5% at the first post-report session"
            " (pead_beats reasons beat|miss_proxy), long, hold 20; 141 sealed cards"
            " with completed holds = the published 144-card book minus its three"
            " 2024-09 cards (menu fold_mapping)"
        ),
        "sealed_acceptance_criteria": criteria_text,
        "family_bar_t": FAMILY_T,
        "off_floor_cards": {
            "xsmom_top3": XSMOM_OFF_FLOOR,
            "pead_beat": PEAD_OFF_FLOOR,
        },
        "index_outage_policy": f"> {MAX_INDEX_OUTAGE} sealed sessions = NOT_EVALUABLE, no imputation",
        "bootstrap": {
            "resamples": BOOTSTRAP_RESAMPLES,
            "block": "sealed entry date (cluster bootstrap over cards)",
            "sided": "one (H1: new spread > incumbent spread)",
            "alpha": 0.05,
            "seed_bytes": BOOTSTRAP_SEED_BYTES.decode(),
            "seed_registered": False,
            "note": "parameters per the menu; the draw unit and seed are fixed deterministically by the sealed runner and disclosed",
        },
        "inputs_sha256": {
            "ohlc-panel.json": inputs.panel_sha256,
            "earnings-calendar.json": inputs.earnings_sha256,
            "nyse_sessions json": inputs.calendar_sha256,
            **{f"indices/{k}": v for k, v in inputs.indices_sha256.items()},
            "slots/term-gate.md": inputs.slot_doc_sha256,
            "tnull/calibration-v3.json": frozen["calibration_v3_sha256"],
            "xsmom_exitgrid.py": frozen["frozen_exitgrid_sha256"],
            "inner-ranking.json": frozen["inner_ranking_sha256"],
        },
        "registration_menu_sha256": inputs.menu_sha256,
        "protocol_raw_sha256": inputs.protocol_raw_sha256,
        "protocol_canonical_sha256": inputs.protocol_canonical_sha256,
        "dataset_manifest_hash": inputs.dataset_manifest_hash,
        "B_card_era": inputs.B,
        "B_note": (
            "B(card-era, shape) read from calibration-v3.json (never recomputed);"
            " the card-era window is EVALUABLE in the v3 stamp, so the sealed"
            " round reads the normal B-excess"
        ),
        "round1_inner_ranking_frozen": {
            "sha256": frozen["inner_ranking_sha256"],
            "inner_fold_best": frozen["inner_ranking"]["inner_fold_best"],
            "best_incumbent": frozen["inner_ranking"]["best_incumbent"],
            "note": "recorded pre-seal selection evidence; not re-derived",
        },
    }
    if cell["arm"] == "T1":
        hyper["gate"] = {
            "variable": cell["gate"],
            "style": cell["style"],
            "rule": cell["rule"],
            "off_condition": (
                f"{cell['gate']}(t-1) >= "
                + (
                    f"trailing-{r1.TRAILING_SESSIONS} q{r1.Q60_PCT}"
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
            "regime_rule": f"r93(t-1) >= trailing-{r1.TRAILING_SESSIONS} median -> STRESSED",
            "era": f"signal dates > {r1.INNER_TUNING_END} (the frozen grid's full era)",
        }
    return hyper


def _open_registry():
    r1.CAMPAIGN_DIR.mkdir(parents=True, exist_ok=True)
    return r1.TrialRegistry(r1.REGISTRY_PATH)


# ---- phases ----------------------------------------------------------------------------------


def _load_card_sets(inputs: r1.Inputs) -> dict[str, Any]:  # type: ignore[name-defined]
    xsmom_cards, xsmom_notes = _sealed_xsmom_cards(inputs)
    pead_cards, pead_notes = _sealed_pead_cards(inputs)
    return {
        "xsmom": xsmom_cards,
        "pead": pead_cards,
        "notes": {"xsmom": xsmom_notes, "pead": pead_notes},
    }


def phase_plan() -> int:
    t0 = time.monotonic()
    frozen = _verify_frozen_inputs()
    inputs = r1.load_and_bind()
    cards = _load_card_sets(inputs)
    gaps = _sealed_index_gaps(inputs)
    print(f"menu sha256 {inputs.menu_sha256} (sidecar-verified)")
    print(f"protocol raw {inputs.protocol_raw_sha256[:16]}... canonical {inputs.protocol_canonical_sha256[:16]}...")
    print(f"tnull calibration-v3: {inputs.calibration['verdict']['slot']} (sha {frozen['calibration_v3_sha256'][:16]}...)")
    for shape in ("xsmom", "event"):
        b = inputs.B[shape]
        print(f"B[card-era,{shape:5s}] = {b['B_net_per_trade_mean']:+.9f} se={b['per_trade_mean_sd_day_clustered']}")
    print(f"round-1 runner sha256 {frozen['round1_runner_sha256'][:16]}... (identical-path pin)")
    print(f"inner-ranking sha256 {frozen['inner_ranking_sha256'][:16]}... (frozen pre-seal selection)")
    print(
        f"sealed XSMOM cards: {len(cards['xsmom'])}"
        f" ({cards['xsmom'][0][0]}..{cards['xsmom'][-1][0]}); excluded incomplete FOM:"
        f" {cards['notes']['xsmom']['excluded_incomplete_fom']['session']}"
        f" (hold window ends {cards['notes']['xsmom']['excluded_incomplete_fom']['hold_window_last']})"
    )
    print(
        f"sealed PEAD cards (completed holds): {len(cards['pead'])}"
        f" ({cards['pead'][0]['entry']}..{cards['pead'][-1]['entry']});"
        f" forward-chain events after window: {cards['notes']['pead']['forward_chain_events_after_2026-08-06']};"
        f" in-window incomplete holds: {cards['notes']['pead']['in_window_incomplete_holds']}"
    )
    print(f"missing index sessions in the sealed window: {len(gaps)} {gaps[:8]}")
    print(f"registry db: {r1.REGISTRY_PATH}")
    print(f"sealed rows land under sealed scope_key (outer fold {OUTER_FOLD_SEALED}); round-1 scope holds 24/32")
    print("NO sealed outcome computed or viewed by this phase")
    print(f"elapsed {time.monotonic() - t0:.1f}s")
    return 0


def phase_register() -> int:
    frozen = _verify_frozen_inputs()
    inputs = r1.load_and_bind()
    cards = _load_card_sets(inputs)  # signal-set verification only (no outcomes)
    gaps = _sealed_index_gaps(inputs)
    if len(gaps) > MAX_INDEX_OUTAGE:
        raise r1.Refused(
            f"index outage {len(gaps)} > {MAX_INDEX_OUTAGE} sealed sessions --"
            " the sealed round is NOT_EVALUABLE by registration; abort and report"
        )
    criteria_text = inputs.slot["acceptance_criteria"][0]
    registry = _open_registry()
    try:
        scope = _scope_sealed(inputs)
        before = registry.count_scope(scope.scope_key())
        for cell in ALL_CELLS:
            trial_id = _trial_id(cell["id"])
            if registry.is_registered(trial_id):
                status = registry.status(trial_id)
                if status in ("COMPLETED", "FAILED"):
                    raise r1.Refused(
                        f"{trial_id} is already scored ({status}) -- ABORT: the seal on"
                        " that cell is consumed; one scored run per cell, never overwrite"
                    )
                print(f"{trial_id}: already REGISTERED (resume skip)")
                continue
            hyper = _hyperparameters_sealed(inputs, cell, frozen, criteria_text)
            record = r1.TrialRecord(
                trial_id=trial_id,
                created_at=r1._utcnow(),
                hypothesis=inputs.slot["hypothesis"],
                git_sha=r1._git_head(r1.REPO_ROOT),
                config_hash=r1._config_hash(hyper),
                dataset_manifest_hash=inputs.dataset_manifest_hash,
                train_window=None,
                validation_window=None,
                test_window=(SEALED_WINDOW_FIRST, SEALED_WINDOW_LAST),
                hyperparameters=hyper,
                scope_key=scope.scope_key(),
            )
            registry.register(record, scope)
            print(f"registered {trial_id} arm={cell['arm']}")
        after = registry.count_scope(scope.scope_key())
        print(
            f"registry: {r1.REGISTRY_PATH}; sealed scope rows {before} -> {after}"
            f" (cap 32; outer fold {OUTER_FOLD_SEALED}); round-1 inner scope keeps"
            " its own 24/32 commitment -- the SAME 24 registered menu cells are"
            " scored, no configuration added or changed; NO sealed outcome computed"
            " or viewed"
        )
    finally:
        registry.close()
    return 0


def phase_execute() -> int:
    frozen = _verify_frozen_inputs()
    inputs = r1.load_and_bind()
    cards = _load_card_sets(inputs)
    gaps = _sealed_index_gaps(inputs)
    r1.TERM_DIR.mkdir(parents=True, exist_ok=True)
    r1.TRIALS_DIR.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(r1.LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise r1.Refused("another term-gate execution holds the lock -- one run at a time") from None
        registry = _open_registry()
        try:
            t2_stats: tuple[dict[str, Any], dict[str, Any]] | None = None
            for cell in ALL_CELLS:
                trial_id = _trial_id(cell["id"])
                artifact = _artifact_path(cell["id"])
                status = registry.status(trial_id)
                if artifact.exists():
                    if status == "COMPLETED":
                        print(f"{trial_id}: COMPLETED already (resume skip)")
                        continue
                    raise r1.Refused(
                        f"{artifact} exists but trial is {status} -- inconsistent state,"
                        " refusing (one scored run per cell)"
                    )
                if status not in ("REGISTERED", "RUNNING"):
                    raise r1.Refused(f"{trial_id} is {status}, not REGISTERED -- refusing")
                # INV-13 resume: a cell left RUNNING by a crashed attempt that
                # recorded NO outcome and wrote NO artifact has not consumed its
                # seal. Resume without re-marking; the first mark_running
                # provenance still binds.
                started = time.monotonic()
                if status == "REGISTERED":
                    hyper = _hyperparameters_sealed(
                        inputs, cell, frozen, inputs.slot["acceptance_criteria"][0]
                    )
                    registry.mark_running(
                        trial_id,
                        git_sha=r1._git_head(r1.REPO_ROOT),
                        config_hash=r1._config_hash(hyper),
                        dataset_manifest_hash=inputs.dataset_manifest_hash,
                        at=r1._utcnow(),
                    )
                if cell["arm"] == "T1":
                    payload = run_t1_sealed(inputs, cell, cards, gaps)
                else:
                    if t2_stats is None:  # one shared pass for all 12 T2 cells
                        t2_stats = _t2_core_sealed(inputs)
                    payload = run_t2_sealed(inputs, cell, t2_stats)
                payload["elapsed_s"] = round(time.monotonic() - started, 3)
                # machinery self-check: an EMPTY T1 cell is a runner defect,
                # never an outcome -- the trial FAILs and no artifact is written
                if cell["arm"] == "T1":
                    if payload["on"]["n_entries"] + payload["off"]["n_entries"] == 0:
                        registry.fail(
                            trial_id,
                            "T1 sealed cell executed with 0 legs -- runner machinery defect",
                            at=r1._utcnow(),
                        )
                        raise r1.Refused(
                            f"{trial_id}: T1 sealed cell executed with 0 legs (machinery"
                            " defect; trial FAILED, no artifact written)"
                        )
                body = {"stamp": _stamp(inputs, cell, frozen), "payload": payload}
                artifact.write_text(
                    json.dumps(body, indent=2, sort_keys=True) + "\n", encoding="utf-8"
                )
                registry.complete(trial_id, metrics_uri=str(artifact), outcome_at=r1._utcnow())
                if cell["arm"] == "T1":
                    print(
                        f"{trial_id}: COMPLETED verdict={payload['sealed_verdict']}"
                        f" ONcards={payload['card_counts']['on_cards']}"
                        f" OFFcards={payload['card_counts']['off_cards']}"
                        f" spread={None if payload['spread_on_minus_off'] is None else round(payload['spread_on_minus_off'], 6)}"
                        f" B_excess={None if payload['criterion_1']['B_excess_on_minus_B'] is None else round(payload['criterion_1']['B_excess_on_minus_B'], 6)}"
                        f" t_on={None if payload['criterion_1']['clustered_t_on'] is None else round(payload['criterion_1']['clustered_t_on'], 4)}"
                    )
                else:
                    print(
                        f"{trial_id}: COMPLETED verdict={payload['sealed_verdict']}"
                        f" cell_days={payload['cell']['days']}"
                        f" d_mean={round(payload['cell']['d_mean'], 6)}"
                        f" t_cons={round(payload['cell']['t_cons'], 4) if payload['cell']['t_cons'] == payload['cell']['t_cons'] else None}"
                    )
        finally:
            registry.close()
    finally:
        os.close(lock_fd)
    return 0


def _read_sealed_artifact(
    inputs: r1.Inputs, frozen: Mapping[str, Any], cell: Mapping[str, Any]  # type: ignore[name-defined]
) -> Mapping[str, Any]:
    artifact = _artifact_path(cell["id"])
    body = json.loads(artifact.read_text(encoding="utf-8"))
    stamp = body.get("stamp", {})
    if (
        stamp.get("config_id") != cell["id"]
        or stamp.get("scope_id") != SCOPE_ID
        or stamp.get("round") != ROUND_LABEL
    ):
        raise r1.Refused(f"{artifact} does not bind {_trial_id(cell['id'])}")
    if stamp.get("registration_menu_sha256") != inputs.menu_sha256:
        raise r1.Refused(f"artifact {cell['id']} was executed against a different menu hash")
    if stamp.get("dataset_manifest_hash") != inputs.dataset_manifest_hash:
        raise r1.Refused(f"artifact {cell['id']} was executed against different inputs")
    if stamp.get("round1_runner_sha256") != frozen["round1_runner_sha256"]:
        raise r1.Refused(f"artifact {cell['id']} lost its identical-path pin")
    return body


def phase_stamp() -> int:
    frozen = _verify_frozen_inputs()
    inputs = r1.load_and_bind()
    if SEAL_PATH.exists():
        raise r1.Refused(f"{SEAL_PATH} already exists -- the sealed stamp is one-shot")
    artifacts = {cell["id"]: _read_sealed_artifact(inputs, frozen, cell) for cell in ALL_CELLS}
    criteria_t1 = inputs.slot["acceptance_criteria"][0]
    criteria_family = inputs.slot["acceptance_criteria"][1]
    criteria_t2 = inputs.slot["acceptance_criteria"][2]
    # ---- T1 cells
    t1: dict[str, Any] = {}
    for cell in T1_CELLS:
        payload = artifacts[cell["id"]]["payload"]
        t1[cell["id"]] = {
            "config": dict(cell),
            "rule": cell["rule"],
            "gate_spec": f"{cell['gate']}x{cell['style']}",
            "role": cell["role"],
            "sealed_verdict": payload["sealed_verdict"],
            "sealed_verdict_reason": payload["sealed_verdict_reason"],
            "criteria": payload["criteria"],
            "on": payload["on"],
            "off": payload["off"],
            "card_counts": payload["card_counts"],
            "spread_on_minus_off": payload["spread_on_minus_off"],
            "criterion_1": payload["criterion_1"],
            "utility_criterion_3": payload["utility_criterion_3"],
            "off_floor": payload["off_floor"],
        }
    # ---- family verdict
    candidates = {k: v for k, v in t1.items() if v["sealed_verdict"] == "CANDIDATE"}
    new_cands = {k: v for k, v in candidates.items() if "incumbent" not in v["role"]}
    inc_cands = {k: v for k, v in candidates.items() if "incumbent" in v["role"]}
    outage = artifacts[T1_CELLS[0]["id"]]["payload"]["index_outage_sealed_sessions"]
    family: dict[str, Any] = {
        "registered_criterion": criteria_family,
        "candidates": sorted(candidates),
        "new_quantity_candidates": sorted(new_cands),
        "incumbent_candidates": sorted(inc_cands),
    }
    if all(v["sealed_verdict"] == "NOT_EVALUABLE" for v in t1.values()):
        family_verdict = "NOT_EVALUABLE"
        family["verdict_reason"] = (
            "every T1 cell is NOT_EVALUABLE (power floors / index outage) -- the"
            " family claims nothing"
        )
    elif not new_cands:
        if inc_cands:
            family_verdict = "INCUMBENT-CONFIRMED"
            family["verdict_reason"] = (
                f"only incumbent cells are CANDIDATEs ({sorted(inc_cands)}) -- vix_term"
                " is confirmed as the context signal, no successor"
            )
        else:
            family_verdict = "WITHDRAW"
            family["verdict_reason"] = "no CANDIDATE on either rule"
    else:
        # best new-quantity candidate by spread (tie -> config id lexicographic)
        best_new_id = sorted(
            new_cands, key=lambda k: (-(new_cands[k]["spread_on_minus_off"]), k)
        )[0]
        best_new = new_cands[best_new_id]
        rule = best_new["rule"]
        inc_on_rule = {
            k: v for k, v in t1.items() if "incumbent" in v["role"] and v["rule"] == rule
        }
        defined_inc = {
            k: v for k, v in inc_on_rule.items() if v["spread_on_minus_off"] is not None
        }
        family["best_new_quantity"] = {
            "config_id": best_new_id,
            "gate_spec": best_new["gate_spec"],
            "rule": rule,
            "spread_on_minus_off": best_new["spread_on_minus_off"],
        }
        family["incumbents_on_rule"] = {
            k: {"spread_on_minus_off": v["spread_on_minus_off"], "sealed_verdict": v["sealed_verdict"]}
            for k, v in inc_on_rule.items()
        }
        if not defined_inc:
            family_verdict = "WITHDRAW"
            family["best_incumbent"] = None
            family["bootstrap"] = None
            family["verdict_reason"] = (
                f"new-quantity CANDIDATEs exist (best {best_new_id}) but no incumbent"
                f" cell on rule {rule} has a defined ON-OFF spread -- the beat cannot"
                " be established, and parity-or-worse cannot be excluded; the"
                " pre-committed posture is WITHDRAW"
            )
        else:
            best_inc_id = sorted(
                defined_inc, key=lambda k: (-(defined_inc[k]["spread_on_minus_off"]), k)
            )[0]
            best_inc = defined_inc[best_inc_id]
            family["best_incumbent"] = {
                "config_id": best_inc_id,
                "gate_spec": best_inc["gate_spec"],
                "rule": rule,
                "spread_on_minus_off": best_inc["spread_on_minus_off"],
            }
            # build the card-level streams for the bootstrap
            rule_key = "xsmom" if rule == "xsmom_top3" else "pead"
            entries = [c["entry"] for c in artifacts[best_new_id]["payload"]["cards"]]
            # per-card leg nets are not stored in the artifacts; rebuild them
            # from the card set + _complete_trade (deterministic, same machinery,
            # same pinned inputs; identical values to the executed cells)
            nets_by_entry: dict[str, list[float]] = {}
            card_sets = _load_card_sets(inputs)
            nets_by_entry = {}
            if rule == "xsmom_top3":
                for entry_iso, top3 in card_sets["xsmom"]:
                    nets = []
                    for name in top3:
                        t = r1._complete_trade(inputs, name, date.fromisoformat(entry_iso))
                        if t.gross is not None:
                            nets.append(t.gross - r1.RT_PRIMARY)
                    nets_by_entry[entry_iso] = nets
            else:
                # pool every card sharing an entry date into that date's block
                # (the cluster is the entry date; a date can carry several cards)
                for c in card_sets["pead"]:
                    t = r1._complete_trade(inputs, c["name"], date.fromisoformat(c["entry"]))
                    if t.gross is not None:
                        nets_by_entry.setdefault(c["entry"], []).append(t.gross - r1.RT_PRIMARY)
            off_new = {
                c["entry"]: c["off"] for c in artifacts[best_new_id]["payload"]["cards"]
            }
            off_inc = {
                c["entry"]: c["off"] for c in artifacts[best_inc_id]["payload"]["cards"]
            }
            if set(off_new) != set(off_inc) or set(off_new) != set(entries):
                raise r1.Refused("card sets across cells on the same rule disagree -- machinery defect")
            boot = _family_bootstrap(rule, entries, nets_by_entry, off_new, off_inc)
            boot["new_gate"] = best_new["gate_spec"]
            boot["incumbent_gate"] = best_inc["gate_spec"]
            boot["point_new_minus_incumbent"] = (
                boot["point_spread_new"] - boot["point_spread_incumbent"]
            )
            family["bootstrap"] = boot
            p = boot.get("p_one_sided")
            beats = (
                p is not None
                and p < 0.05
                and best_new["spread_on_minus_off"] > best_inc["spread_on_minus_off"]
            )
            if beats:
                family_verdict = "PASS-GATE"
                family["verdict_reason"] = (
                    f"{best_new_id} ({best_new['gate_spec']}) is a CANDIDATE and beats the"
                    f" best incumbent {best_inc_id} on rule {rule} by the entry-date"
                    f" block bootstrap (p = {p:.4f} < 0.05)"
                )
            else:
                family_verdict = "WITHDRAW"
                family["verdict_reason"] = (
                    f"new-quantity CANDIDATEs exist (best {best_new_id}) but none beats"
                    f" the best incumbent {best_inc_id} on rule {rule}"
                    f" (p = {p if p is not None else 'n/a'} >= 0.05 or spread not"
                    " greater) -- recorded as vix_term-parity; the family dies"
                )
    family["verdict"] = family_verdict
    # ---- T2 cells
    t2: dict[str, Any] = {}
    for cell in T2_CELLS:
        payload = artifacts[cell["id"]]["payload"]
        t2[cell["id"]] = {
            "config": dict(cell),
            "sealed_verdict": payload["sealed_verdict"],
            "sealed_verdict_reason": payload["sealed_verdict_reason"],
            "cell": payload["cell"],
            "pair": payload["pair"],
            "pooled_full_era_anchor": payload["pooled_full_era_anchor"],
        }
    t2_verdicts = sorted({v["sealed_verdict"] for v in t2.values()})
    record = {
        "stamp": {
            **_stamp(inputs, {"id": "sealed-round"}, frozen),
            "config_id": "sealed-round",
            "trial_id": None,
            "artifact_trial_ids": [_trial_id(c["id"]) for c in ALL_CELLS],
            "menu_hypothesis": inputs.slot["hypothesis"],
        },
        "round": ROUND_LABEL,
        "operator_rulings_2026_09_24": OPERATOR_RULINGS_2026_09_24,
        "sealed_window": {
            "xsmom_entries": [XSMOM_SEALED_FIRST.isoformat(), XSMOM_SEALED_LAST.isoformat()],
            "n_xsmom_cards": N_XSMOM_CARDS,
            "pead_entries": [PEAD_SEALED_FIRST.isoformat(), PEAD_SEALED_LAST.isoformat()],
            "n_pead_cards": N_PEAD_CARDS,
            "window": [SEALED_WINDOW_FIRST.isoformat(), SEALED_WINDOW_LAST.isoformat()],
            "n_sessions": sum(
                1
                for s in inputs.calendar.sessions()
                if SEALED_WINDOW_FIRST <= s <= SEALED_WINDOW_LAST
            ),
            "index_outage_sealed_sessions": outage,
            "opened_once": True,
        },
        "frozen_inputs": {
            "round1_inner_ranking": {
                "path": str(r1.RANKING_PATH),
                "sha256": frozen["inner_ranking_sha256"],
                "inner_fold_best": frozen["inner_ranking"]["inner_fold_best"],
                "best_incumbent": frozen["inner_ranking"]["best_incumbent"],
            },
            "round1_runner_sha256": frozen["round1_runner_sha256"],
            "round1_runner_sha256_note": frozen["round1_runner_sha256_note"],
            "calibration_v3_sha256": frozen["calibration_v3_sha256"],
            "frozen_exitgrid_sha256": frozen["frozen_exitgrid_sha256"],
        },
        "registered_sealed_acceptance": {
            "t1_cells": criteria_t1,
            "family": criteria_family,
            "t2_descriptive": criteria_t2,
        },
        "criteria_reading": {
            "criterion_1_clustered_t": (
                "decisive t = the ON cell's own day-clustered t (the slot doc's"
                " pre-amendment 'clustered-t(ON)'; v3 re-centered only the mean leg"
                " on B); a cross-reading re-evaluates criterion 1 with"
                " t = (ON - B)/sqrt(se_ON^2 + se_B^2) and flags any verdict change"
            ),
            "off_n_unit": "OFF cards (distinct OFF entry dates) -- the registration's own risk note counts cards",
            "family_bootstrap": (
                "block = sealed entry date (cluster bootstrap over cards, both gates"
                " re-scored on the same resampled multiset); 10,000 resamples,"
                " one-sided p = #{new <= incumbent}/valid < 0.05; seed disclosed, not registered"
            ),
            "best_selection_ties": "best spread, tie -> config id lexicographic (disclosed; not registered)",
        },
        "verdict_vocabulary": inputs.slot["verdict_vocabulary"],
        "t1_cells": t1,
        "family_verdict": family,
        "t2_cells": t2,
        "t2_verdicts_present": t2_verdicts,
        "scope_accounting": {
            "scope_id": SCOPE_ID,
            "sealed_scope_key": _scope_sealed(inputs).scope_key(),
            "sealed_outer_fold_id": OUTER_FOLD_SEALED,
            "rows_this_round": len(ALL_CELLS),
            "cap": 32,
            "round1_scope_key": r1._scope(inputs).scope_key() if hasattr(r1, "_scope") else None,
            "note": (
                "the registry's 32-cap binds per scope_key; round 1 committed 24"
                " rows under the inner-fold scope_key, so 24 + 24 = 48 > 32 made"
                " subsumption impossible. The sealed rows register under the sealed"
                " fold's own scope_key (the registry's fold-derived separation),"
                " scoring the SAME 24 registered menu cells -- no configuration"
                " added, changed, or re-gridded"
            ),
        },
        "conventions_disclosed": [
            "criterion 1 decisive t = clustered-t(ON) (slot-doc pre-amendment clause; v3 amended the mean leg only); B-excess t with combined se stamped as cross-reading with verdict-change flag",
            "OFF-n floor counted in OFF cards (distinct OFF entry dates)",
            "family bootstrap block = sealed entry date; draw unit and seed fixed here and disclosed (menu pins only the 10,000 resamples and alpha)",
            "PEAD sealed cards = the PROTOCOL-PEAD book (|move| >= 1.5%, long, hold 20) minus its three 2024-09 cards: 141 with completed holds, verified to reproduce from the pinned inputs",
            "T2 sealed read = the frozen grid's FULL era (signal dates > 2024-09-03), the deferred read; round 1 stamped only the holdout interim",
            "one scored run per cell (INV-13): trial rows registered before any sealed outcome was computed or viewed; per-trial artifacts under trials/*-g2.json",
        ],
        "notes": [
            "Nothing adopts on this round: a PASS-GATE earns at most a recorded"
            " context annotation on future cards plus a queued successor"
            " pre-registration; the sealed card rules are untouched mid-stream"
            " (XSMOM-12-1 ruling); kill-switch accounting stays on the ungated rules.",
            "PANEL BLOCK HISTORY: a prior sealed attempt (worktree commit 9a81a2b,"
            " vrp-cond) BLOCKED pre-registration on ohlc-panel drift vs the menu pin"
            " 0861f525..., concluded 'unrecoverable'. Superseded: the pinned bytes"
            " were recovered from the Wave-1 econ lane sha-named snapshot"
            " ~/.local/state/trex-desk-w1-econ/paper-snapshot-0861f525/ohlc-panel.json"
            " (full 64-hex sha256 == the menu pin, verified) and copied into the"
            " frozen root; the block dissolved, the pin binds.",
            "FROZEN-ROOT BINDING: every campaign read and write of this round landed"
            " under DESK_REPO_ROOT=/home/alexk/.local/state/campaign-sealed-inputs/root;"
            " the live main checkout at /home/alexk/documents/tree_options was never"
            " read for campaign inputs and never written. All dataset_pinning shas"
            " re-verified against the frozen bytes before computing (load_and_bind"
            " re-checks each pin at bind).",
            "xsmom_exitgrid.py (frozen machinery, sha 6ef943f7... = the round-1"
            " stamp's frozen_exitgrid_sha256) was copied byte-exact into the frozen"
            " root's artifacts/paper-trades/ before this run; the frozen root did not"
            " carry it. The script resolves its panel relative to its own file, so"
            " the copy reads the pinned panel. No other file was added to or"
            " changed in the frozen root's inputs.",
            "The frozen root's data/calendar path is a symlink into the main"
            " checkout's data dir as assembled by the operator; the calendar pin"
            " 7f9cccba... verifies byte-exact through it (load_and_bind re-checks"
            " the pin at bind).",
            "The 2026-09-01 XSMOM FOM card fires on the pinned inputs but its"
            " 20-session hold window ends 2026-09-30, past the 2026-09-23 panel end"
            " -- incomplete, excluded exactly as registered, accrues to the forward"
            " chain only. PEAD big-surprise events after 2026-08-06 (7) accrue to"
            " the forward chain only; none was scored.",
            "T2 outcome conditioning is on already-viewed cells (XU post-hoc"
            " precedent): a regime-conditional exit would need a fresh sealed"
            " registration; nothing changes live from the T2 read.",
        ],
    }
    SEAL_PATH.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"FAMILY VERDICT: {family_verdict}")
    for cid in sorted(t1):
        v = t1[cid]
        sp = v["spread_on_minus_off"]
        print(
            f"  {cid} {v['gate_spec']:8s} {v['rule']:10s} {v['sealed_verdict']:14s}"
            f" ONcards={v['card_counts']['on_cards']:3d} OFFcards={v['card_counts']['off_cards']:3d}"
            f" spread={None if sp is None else round(sp, 6)}"
        )
    print(f"T2 verdicts present: {t2_verdicts}")
    print(f"sealed round stamped: {SEAL_PATH}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--plan", action="store_true", help="read-only bind + sealed card-set verification")
    parser.add_argument(
        "--register",
        action="store_true",
        help="INV-13: write the 24 sealed trial rows (REGISTERED, no outcome)",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="one-shot scored run per sealed cell (resumable across cells)",
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
    except r1.Refused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
