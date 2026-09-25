#!/usr/bin/env python3
"""campaign-2026-09 VRP-COND SEALED ROUND (round 2, scope ``c09-vrp-e``).

Operator authorization 2026-09-24 (ruling 1 of 4): the sealed outer window is
OPEN for the three queued families. This runner opens it ONCE for vrp-cond:
sealed scoring of the two promoted round-1 configs under sealed acceptance
criteria 1-4, with the same-family placebos run through the identical path.

Sealed window: ordinals 443..505 = 2026-06-02..2026-08-31 (63 sessions), the
single sealed window of the registration (menu v3, sha256 sidecar-verified).

What runs (5 cells, ONE scored run per cell — INV-13):

* nominees (frozen by ``round1-selection.json``, never re-derived here):
  ``xe-mkt-hi`` (tuning delta +0.1495510865313336) and ``xp-size-mkt``
  (+0.003169064787271728) — tuning deltas are SELECTION evidence only;
* placebos on the identical path: ``xe-lag21``, ``xp-lag21``, ``xp-shuffle``
  (controls; sealed criterion 3 compares the nominee's decisive delta against
  the same-family placebo decisive deltas);
* scope O stays WITHDRAWN (``scope-O-withdrawal.json``; no ox/op cell runs).

IDENTICAL PATH GUARANTEE: this module imports the round-1 runner
(``scripts/campaign/vrp-cond_run.py``, sha256 pinned to the round-1 stamp
80bc0a41…) and reuses its input binding, feature layer, percentile/raw-ratio
evaluation, decision constructors and trade path verbatim. Only the scored
region changes: entry sessions are the sealed window's (443..505).

Registered sealed acceptance (menu ``acceptance_criteria[1]``, verbatim):
ALL four for each promoted config —
(1) sealed ON-minus-OFF-session base delta > 0 (the matched-sessions
    conditional column — decisive, SQUEEZE precedent);
(2) day-clustered block bootstrap (block 10, 2000 resamples, one-sided)
    p < 0.10 on that delta;
(3) the delta exceeds BOTH same-family placebo deltas on the identical path;
(4) the delta stays > 0 at 15bp RT.
Verdict mapping (slot doc section 6): floors unmet -> NOT_EVALUABLE-SEALED
(never FAIL); all four hold -> SURVIVOR-CANDIDATE (nomination only — nothing
adopts; promotion runs only through >= 20 forward sealed cards); any miss ->
DEFLATED. Power floors: xe-* n_ON >= 6 sealed entries; xp-* n_ON >= 8 sealed
trades; the floor binds on FIRED n_ON, exactly as round 1 bound it.

Reading of the criterion chain (disclosed in every artifact): criterion 1
names THE delta — the decisive matched-sessions ON-minus-OFF column the
round-1 runner stamped descriptively ("the decisive SEALED criterion 1");
criteria 2-4 bind that same delta (2 bootstraps it; 3 compares it to the
placebos' identical-path columns; 4 recomputes it at 15bp RT). The
conditioned-minus-base columns (5bp/15bp) are stamped beside them as
disclosure, and a cross-reading block records whether criteria 3-4 evaluated
on the conditioned-minus-base delta would change the verdict.

Conventions fixed here because the menu does not pin them (each disclosed in
the artifacts, never silently chosen):
* the ON-minus-OFF column is computed at BOTH round turns (5bp decisive,
  15bp for criterion 4) — round 1 stamped 5bp only;
* bootstrap: moving-block resampling over the 63-session sealed grid (each
  grid position carries its day's ON/OFF net-trade counts and sums; draws =
  ceil(63/10) = 7 blocks, starts uniform over 0..53, concatenated then
  truncated to 63 positions — the draw conventions of
  ``tree_options.evaluation.diagnostics.block_bootstrap_ci``; a resample
  with an empty ON or OFF side is skipped as a None statistic);
  one-sided p = #{resample delta <= 0} / valid resamples; the (+1)
  correction is stamped as a sensitivity. Seed: deterministic
  int(sha256("vrp-cond-sealed-bootstrap-1")) — the seed is NOT registered;
  it is fixed here and disclosed;
* a placebo whose sealed decisive delta is undefined (no evaluable ON or OFF
  side on the identical path) is recorded as trivially exceeded, with the
  fact stamped — it carries no competing path.

Phases (run in order; the harness logs every one):
* ``--plan`` — read-only bind + geometry + frozen-input verification. No
  sealed outcome is computed or viewed (feature r values only, which are
  gate inputs, never outcomes).
* ``--register`` — INV-13: write the 5 sealed trial rows (REGISTERED, no
  outcome) BEFORE any sealed outcome exists. An id found REGISTERED without
  an outcome is a resume skip; an id with an outcome is a hard ABORT (the
  seal on that cell is consumed — one scored run per cell, never
  overwritten). Rows land under the round-1 scope_key so the menu's single
  32-cap for c09-vrp-e keeps binding (20 -> 25 rows).
* ``--execute`` — one-shot scored run per cell under the slot flock;
  resumable: a cell whose artifact exists AND is COMPLETED is skipped,
  anything inconsistent refuses. ``mark_running`` provenance (git sha +
  config hash) must match the registration, so register and execute run
  under the SAME commit.
* ``--stamp`` — read ONLY the executed artifacts, apply the registered
  criteria verbatim, write ``sealed-round.json`` (one-shot).
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
R1_PATH = REPO_ROOT / "scripts" / "campaign" / "vrp-cond_run.py"

_spec = importlib.util.spec_from_file_location("vrp_cond_round1", R1_PATH)
r1 = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
sys.modules["vrp_cond_round1"] = r1
_spec.loader.exec_module(r1)  # type: ignore[union-attr]

ROUND = 2
SLOT_ID = r1.SLOT_ID
SCOPE_E = r1.SCOPE_E
SEALED_CONFIGS = ("xe-mkt-hi", "xp-size-mkt", "xe-lag21", "xp-lag21", "xp-shuffle")
NOMINEES = ("xe-mkt-hi", "xp-size-mkt")
FAMILY_PLACEBOS = {"xe": ("xe-lag21",), "xp": ("xp-lag21", "xp-shuffle")}
XSMOM_SEALED_REBALANCES = ("2026-06-02", "2026-07-01", "2026-08-03")
N_SEALED_SESSIONS = 63
SEALED_FIRST_ISO = "2026-06-02"
SEALED_LAST_ISO = "2026-08-31"

BOOTSTRAP_BLOCK = 10
BOOTSTRAP_RESAMPLES = 2000
BOOTSTRAP_SEED_BYTES = b"vrp-cond-sealed-bootstrap-1"

# round-1 provenance pins (refuse if the world moved)
R1_RUNNER_SHA256 = "80bc0a418a1dcd0712f669d7f85a8c163b2ab62c9102ee22b09789464943236a"
CALIBRATION_V3_SHA256 = "5d0aa0eeaea9075ec72e48a0436c2e60dd1f715092fabc4d60ee328c3811e683"
FROZEN_PROMOTED = {
    "xe": ("xe-mkt-hi", 0.1495510865313336),
    "xp": ("xp-size-mkt", 0.003169064787271728),
}

SEAL_PATH = r1.SLOT_DIR / "sealed-round.json"

OPERATOR_RULINGS_2026_09_24 = {
    "1": "sealed reads authorized for all three queued families (operative here)",
    "2": "JEPA FLIP-h21 registration DECLINED — no new variant, score exactly as registered (jepa-filter slot)",
    "3": "JEPA V3 divergence ruled NOT_EVALUABLE-defective, stands; no successor registration authorized (jepa-filter slot)",
    "4": "pead-deep-2 stays descriptive (default accepted, no sealed read) (pead-deep-2 slot)",
}


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _trial_id(config_id: str) -> str:
    return f"{SCOPE_E}-{config_id}-r{ROUND}"


def _artifact_path(config_id: str) -> Path:
    return r1.TRIALS_DIR / f"{_trial_id(config_id)}.json"


def _sealed_sessions(inputs: r1.Inputs) -> list[date]:  # type: ignore[name-defined]
    sessions = [
        inputs.window_sessions[o - 1]
        for o in range(r1.ORD_SEALED_START, r1.ORD_SEALED_END + 1)
    ]
    if (
        len(sessions) != N_SEALED_SESSIONS
        or sessions[0].isoformat() != SEALED_FIRST_ISO
        or sessions[-1].isoformat() != SEALED_LAST_ISO
    ):
        raise r1.Refused(
            f"sealed window is not {N_SEALED_SESSIONS} sessions "
            f"{SEALED_FIRST_ISO}..{SEALED_LAST_ISO}: {len(sessions)} ending {sessions[-1] if sessions else None}"
        )
    return sessions


def _verify_frozen_inputs() -> dict[str, Any]:
    """Bind the frozen round-1 selection + calibration + round-1 runner."""
    if _sha256_file(R1_PATH) != R1_RUNNER_SHA256:
        raise r1.Refused(
            "the round-1 runner's sha256 moved — the identical-path guarantee is void"
        )
    cal_sha = _sha256_file(r1.CALIBRATION_V3_PATH)
    if cal_sha != CALIBRATION_V3_SHA256:
        raise r1.Refused(
            f"tnull calibration-v3 sha256 {cal_sha} is not the pinned {CALIBRATION_V3_SHA256}"
        )
    selection = json.loads(r1.SELECTION_PATH.read_text(encoding="utf-8"))
    for family, (config, delta) in FROZEN_PROMOTED.items():
        got = selection["families"][family]["promoted"]
        got_delta = selection["families"][family]["promoted_delta"]
        if got != config or got_delta != delta:
            raise r1.Refused(
                f"round-1 selection promotes {family}={got!r} (delta {got_delta!r}),"
                f" the frozen input says {config!r} ({delta!r})"
            )
    return {
        "selection_sha256": _sha256_file(r1.SELECTION_PATH),
        "selection": selection,
        "calibration_v3_sha256": cal_sha,
        "round1_runner_sha256": R1_RUNNER_SHA256,
    }


# ---- sealed base streams (same machinery, sealed region) -------------------------------


def xsmom_base_sealed(inputs: r1.Inputs) -> list[dict[str, Any]]:  # type: ignore[name-defined]
    rows: list[dict[str, Any]] = []
    rebalances: list[str] = []
    for session in _sealed_sessions(inputs):
        if not inputs.calendar.is_first_session_of_month(session):
            continue
        res = r1.signals_mod.xsmom_top3(inputs.panel, session, inputs.calendar)
        rebalances.append(session.isoformat())
        if not res.fires:
            raise r1.Refused(
                f"xsmom did not fire on sealed rebalance {session} (n_ranked={res.n_ranked})"
                " -- machinery defect"
            )
        for name in res.top3:
            rows.append(
                {
                    "family": "xe",
                    "name": name,
                    "session": session,
                    "detail": f"xsmom-top3:{session.isoformat()}",
                }
            )
    if tuple(rebalances) != XSMOM_SEALED_REBALANCES:
        raise r1.Refused(
            f"sealed rebalances {rebalances} are not the registration's 3"
            f" {XSMOM_SEALED_REBALANCES}"
        )
    if len(rows) != len(XSMOM_SEALED_REBALANCES) * r1.signals_mod.XSMOM_TOPK:
        raise r1.Refused(f"sealed xsmom base carries {len(rows)} entries, expected 9")
    return rows


def pead_base_sealed(inputs: r1.Inputs) -> list[dict[str, Any]]:  # type: ignore[name-defined]
    rows: list[dict[str, Any]] = []
    for session in _sealed_sessions(inputs):
        res = r1.signals_mod.pead_beats(inputs.panel, inputs.earnings, session, inputs.calendar)
        for ev in res.beats:
            rows.append(
                {
                    "family": "xp",
                    "name": ev.name,
                    "session": session,
                    "report_date": ev.report_date,
                    "move": float(ev.move) if ev.move is not None else None,
                    "detail": f"pead-beat:{ev.report_date}",
                }
            )
    if not rows:
        raise r1.Refused("the sealed window carries zero PEAD beats -- machinery defect")
    return rows


# ---- sealed scoring --------------------------------------------------------------------


def _mean_net(trades: Sequence[Mapping[str, Any]], rt: float) -> float | None:
    return statistics.fmean(t["gross"] - rt for t in trades) if trades else None


def _on_off_columns(
    config_id: str,
    on_complete: Sequence[Mapping[str, Any]],
    base_complete: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    """The decisive matched-sessions ON-minus-OFF column at BOTH round turns.

    Entry gates (+ their placebos): ON = fired complete trades, OFF = the
    base complete trades the gate did not fire (round-1 construction).
    Size config (xp-size-mkt): ON = half-sized (condition-high) complete
    trades, OFF = full-sized ones (round-1 construction).
    """
    if config_id in ("xe-size-mkt", "xe-size-book", "xp-size-name", "xp-size-mkt"):
        hi = [t for t in on_complete if t["weight"] < 1.0]
        lo = [t for t in on_complete if t["weight"] >= 1.0]
        meaning = "half-sized (condition high) vs full-sized entries"
        on_side, off_side = hi, lo
    else:
        fired_keys = {(t["name"], t["entry"]) for t in on_complete}
        off = [t for t in base_complete if (t["name"], t["entry"]) not in fired_keys]
        meaning = "fired (ON) vs base-not-fired (OFF) complete trades, matched region"
        on_side, off_side = on_complete, off
    out: dict[str, Any] = {
        "meaning": meaning,
        "decisive_for": "sealed criterion 1 (SQUEEZE precedent; round-1 descriptive column)",
        "n_on": len(on_side),
        "n_off": len(off_side),
    }
    for tag, rt in (("5bp", r1.RT_PRIMARY), ("15bp", r1.RT_ROBUST)):
        on_mean = _mean_net(on_side, rt)
        off_mean = _mean_net(off_side, rt)
        out[f"on_mean_{tag}"] = on_mean
        out[f"off_mean_{tag}"] = off_mean
        out[f"on_minus_off_{tag}"] = (
            on_mean - off_mean if on_mean is not None and off_mean is not None else None
        )
    return out, list(on_side), list(off_side)


def _day_table(
    grid: Sequence[date],
    on_side: Sequence[Mapping[str, Any]],
    off_side: Sequence[Mapping[str, Any]],
) -> list[list[float]]:
    """Per sealed-session (n_on, sum_on_net5, n_off, sum_off_net5)."""
    idx = {d.isoformat(): i for i, d in enumerate(grid)}
    rows: list[list[float]] = [[0, 0.0, 0, 0.0] for _ in grid]
    for t in on_side:
        i = idx[t["entry"]]
        rows[i][0] += 1
        rows[i][1] += t["gross"] - r1.RT_PRIMARY
    for t in off_side:
        i = idx[t["entry"]]
        rows[i][2] += 1
        rows[i][3] += t["gross"] - r1.RT_PRIMARY
    return rows


def _bootstrap_on_minus_off(rows: Sequence[Sequence[float]]) -> dict[str, Any]:
    """Moving-block bootstrap of the pooled ON-minus-OFF delta (5bp) over the
    sealed session grid; draw conventions mirror
    tree_options.evaluation.diagnostics.block_bootstrap_ci (starts uniform,
    ceil(n/block) blocks, concatenated and truncated to n; None statistics
    skipped). One-sided p = #{resample delta <= 0} / valid."""
    n = len(rows)
    c_on = sum(int(r[0]) for r in rows)
    c_off = sum(int(r[2]) for r in rows)
    point = None
    if c_on and c_off:
        point = (math.fsum(r[1] for r in rows) / c_on) - (math.fsum(r[3] for r in rows) / c_off)
    seed = int.from_bytes(hashlib.sha256(BOOTSTRAP_SEED_BYTES).digest(), "big")
    out: dict[str, Any] = {
        "registered_parameters": {
            "block_sessions": BOOTSTRAP_BLOCK,
            "resamples": BOOTSTRAP_RESAMPLES,
            "sided": "one (H1: delta > 0)",
        },
        "conventions_disclosed": {
            "unit": "the 63-session sealed grid; each grid position carries its day's ON/OFF net counts+sums (5bp RT)",
            "draws": "ceil(n/block) moving blocks, starts uniform over 0..n-block, concatenated, truncated to n (diagnostics.block_bootstrap_ci conventions)",
            "undefined_resamples": "skipped (empty ON or OFF side = None statistic)",
            "p_definition": "p = #{resample delta <= 0} / valid resamples",
            "seed_bytes": BOOTSTRAP_SEED_BYTES.decode(),
            "seed": seed,
            "seed_registered": False,
            "seed_note": "the menu pins the bootstrap parameters but no seed; fixed here deterministically and disclosed",
        },
        "grid_sessions": n,
        "point_on_minus_off_5bp": point,
        "valid_resamples": 0,
        "le_zero": 0,
        "p_one_sided": None,
        "p_plus_one_sensitivity": None,
    }
    if point is None:
        out["reason"] = "point delta unevaluable (empty ON or OFF side)"
        return out
    rng = random.Random(seed)
    draws = -(-n // BOOTSTRAP_BLOCK)
    max_start = n - BOOTSTRAP_BLOCK
    valid = 0
    le0 = 0
    for _ in range(BOOTSTRAP_RESAMPLES):
        positions: list[int] = []
        for _ in range(draws):
            start = rng.randrange(max_start + 1)
            positions.extend(range(start, start + BOOTSTRAP_BLOCK))
        n_on = 0
        s_on = 0.0
        n_off = 0
        s_off = 0.0
        for i in positions[:n]:
            row = rows[i]
            n_on += int(row[0])
            s_on += row[1]
            n_off += int(row[2])
            s_off += row[3]
        if n_on == 0 or n_off == 0:
            continue
        valid += 1
        if (s_on / n_on) - (s_off / n_off) <= 0.0:
            le0 += 1
    out["valid_resamples"] = valid
    out["le_zero"] = le0
    if valid:
        out["p_one_sided"] = le0 / valid
        out["p_plus_one_sensitivity"] = (le0 + 1) / (valid + 1)
    else:
        out["reason"] = "no valid resample produced a defined delta"
    return out


def _score_sealed(
    inputs: r1.Inputs,  # type: ignore[name-defined]
    feats: r1.Features,  # type: ignore[name-defined]
    base_rows: Sequence[Mapping[str, Any]],
    config_id: str,
) -> dict[str, Any]:
    """Score ONE config on the sealed window (the single scored run)."""
    if config_id.startswith("xe-"):
        decisions = r1.xe_decisions(inputs, feats, base_rows, config_id)
    else:
        decisions = r1.xp_decisions(inputs, feats, base_rows, config_id)
    base_trades: list[dict[str, Any]] = []
    for row in base_rows:
        t = r1._complete_trade(inputs, row["name"], row["session"], 1.0, row["detail"])
        base_trades.append(
            {
                "entry": t.entry,
                "exit": t.exit_session,
                "name": t.name,
                "gross": t.gross,
                "weight": t.weight,
                "detail": t.detail,
                "condition": None,
                "condition_value": None,
            }
        )
    on_trades: list[dict[str, Any]] = []
    abstained = 0
    ne_notes: dict[str, int] = {}
    for row, dec in zip(base_rows, decisions, strict=True):
        if not dec.fire:
            if dec.value is None:
                abstained += 1
                key = dec.note.split(":")[0][:80]
                ne_notes[key] = ne_notes.get(key, 0) + 1
            continue
        entry_session: date = row["session"]  # no defer config in the sealed set
        if not (
            r1.ORD_SEALED_START
            <= inputs.window_ordinal(entry_session)
            <= r1.ORD_SEALED_END
        ):
            raise r1.Refused(
                f"{config_id}: entry {entry_session} falls outside the sealed window"
            )
        t = r1._complete_trade(
            inputs, row["name"], entry_session, dec.weight, row["detail"] + f";{config_id}"
        )
        on_trades.append(
            {
                "entry": t.entry,
                "exit": t.exit_session,
                "name": t.name,
                "gross": t.gross,
                "weight": t.weight,
                "detail": t.detail,
                "condition": dec.condition,
                "condition_value": dec.value,
                "note": dec.note,
            }
        )
    family = "xe" if config_id.startswith("xe-") else "xp"
    base_complete = [t for t in base_trades if t["gross"] is not None]
    on_complete = [t for t in on_trades if t["gross"] is not None]
    base_mean = statistics.fmean(t["gross"] - r1.RT_PRIMARY for t in base_complete)
    base_mean15 = statistics.fmean(t["gross"] - r1.RT_ROBUST for t in base_complete)
    strat_mean = r1._weighted_net_mean(on_complete, r1.RT_PRIMARY)
    strat_mean15 = r1._weighted_net_mean(on_complete, r1.RT_ROBUST)
    delta = strat_mean - base_mean if strat_mean is not None else None
    delta15 = strat_mean15 - base_mean15 if strat_mean15 is not None else None
    on_off, on_side, off_side = _on_off_columns(config_id, on_complete, base_complete)
    n_fired = len(on_trades)
    floor = r1.FLOORS[family]
    payload: dict[str, Any] = {
        "config_id": config_id,
        "family": family,
        "placebo": config_id in r1.PLACEBOS,
        "gate_rule": r1.GATE_RULES[config_id],
        "region": {
            "meaning": (
                "sealed window ordinals 443..505 = 2026-06-02..2026-08-31"
                " (63 sessions; the single sealed window, opened once,"
                " operator ruling 2026-09-24)"
            ),
            "first_entry": base_rows[0]["session"].isoformat() if base_rows else None,
            "last_entry": base_rows[-1]["session"].isoformat() if base_rows else None,
            "sealed_window_read": True,
            "note": "entries are sealed-window entries; 20-session holds read forward through the coda/panel edge, incomplete holds dropped and counted (house path)",
        },
        "base_cell": r1._cell_stats(base_trades),
        "on_cell": r1._cell_stats(on_trades),
        "deltas": {
            "cond_minus_base_5bp": delta,
            "cond_minus_base_15bp": delta15,
            "role": "disclosure columns (round-1 selection metric family); NOT the decisive sealed column",
        },
        "on_minus_off": on_off,
        "power_floor": {
            "required_n_on": floor,
            "n_on_fired": n_fired,
            "n_on_complete": len(on_complete),
            "met_on_fired": n_fired >= floor,
            "met_on_complete": len(on_complete) >= floor,
            "binds_on": "fired n_ON (round-1 convention)",
        },
        "condition_tallies": {
            "base_entries": len(base_rows),
            "fired": n_fired,
            "not_fired_gate_off": len(base_rows) - n_fired - abstained,
            "abstained_condition_not_evaluable": abstained,
            "not_evaluable_notes": ne_notes,
        },
        "trades_on": on_trades,
        "base_trades": base_trades,
    }
    if config_id in NOMINEES:
        grid = _sealed_sessions(inputs)
        table = _day_table(grid, on_side, off_side)
        boot = _bootstrap_on_minus_off(table)
        pooled = boot.get("point_on_minus_off_5bp")
        decisive = on_off.get("on_minus_off_5bp")
        if pooled is not None and decisive is not None and abs(pooled - decisive) > 1e-9:
            raise r1.Refused(
                f"{config_id}: bootstrap point {pooled} != decisive column {decisive}"
                " -- machinery defect"
            )
        payload["bootstrap"] = boot
    return payload


# ---- stamps / registry -----------------------------------------------------------------


def _stamp(inputs: r1.Inputs, config_id: str, frozen: Mapping[str, Any]) -> dict[str, Any]:  # type: ignore[name-defined]
    return {
        "program": "campaign-2026-09",
        "slot_id": SLOT_ID,
        "round": ROUND,
        "config_id": config_id,
        "scope_id": SCOPE_E,
        "registration_menu_sha256": inputs.menu_sha256,
        "protocol_raw_sha256": inputs.protocol_raw_sha256,
        "protocol_canonical_sha256": inputs.protocol_canonical_sha256,
        "dataset_manifest_hash": inputs.dataset_manifest_hash,
        "inputs_sha256": {
            "ohlc-panel.json": inputs.panel_sha256,
            "earnings-calendar.json": inputs.earnings_sha256,
            "nyse_sessions json": inputs.calendar_sha256,
            "iv-history/vwap_atm.json": inputs.ivhist_sha256,
        },
        "tnull_calibration_v3_verdict": "CALIBRATED",
        "tnull_calibration_v3_sha256": frozen["calibration_v3_sha256"],
        "iv_fidelity_labels": dict(inputs.iv_labels),
        "git_sha": r1._git_head(r1.REPO_ROOT),
        "runner_sha256": _sha256_file(Path(__file__).resolve()),
        "round1_runner_sha256": frozen["round1_runner_sha256"],
        "round1_selection_sha256": frozen["selection_sha256"],
        "sealed_window": {
            "ordinals": [r1.ORD_SEALED_START, r1.ORD_SEALED_END],
            "sessions": [SEALED_FIRST_ISO, SEALED_LAST_ISO],
            "n_sessions": N_SEALED_SESSIONS,
            "opened_once_by": "operator ruling 2026-09-24 (sealed reads authorized)",
        },
        "generated_at": r1._utcnow().isoformat(),
    }


def _hyperparameters_sealed(
    inputs: r1.Inputs, config_id: str, frozen: Mapping[str, Any]  # type: ignore[name-defined]
) -> dict[str, Any]:
    family = "xe" if config_id.startswith("xe-") else "xp"
    r1_hyper = r1._hyperparameters(inputs, config_id)
    return {
        "scope_id": SCOPE_E,
        "config_id": config_id,
        "slot_id": SLOT_ID,
        "round": ROUND,
        "family": family,
        "placebo": config_id in r1.PLACEBOS,
        "gate_rule": r1.GATE_RULES[config_id],
        "model_family": r1.MODEL_FAMILY_E,
        "lane": "card-lane (equity close-to-close)",
        "hold_sessions": r1.HOLD_SESSIONS,
        "rt_primary_bp": 5,
        "rt_robust_bp": 15,
        "sealed_round": True,
        "region": (
            "sealed window ordinals 443..505 = 2026-06-02..2026-08-31 (63 sessions;"
            " the single sealed window, opened once by operator ruling 2026-09-24)"
        ),
        "frozen_from_round1_selection": {
            "path": str(r1.SELECTION_PATH),
            "sha256": frozen["selection_sha256"],
            "promoted": {f: FROZEN_PROMOTED[f][0] for f in FROZEN_PROMOTED},
            "tuning_deltas_are_selection_evidence_only": True,
        },
        "feature": r1_hyper["feature"],
        "sealed_acceptance_criteria": inputs.slot["acceptance_criteria"][1],
        "power_floors_sealed": inputs.slot["acceptance_criteria"][2],
        "bootstrap": {
            "block_sessions": BOOTSTRAP_BLOCK,
            "resamples": BOOTSTRAP_RESAMPLES,
            "sided": "one (H1: delta > 0)",
            "seed_bytes": BOOTSTRAP_SEED_BYTES.decode(),
            "seed_registered": False,
            "note": "parameters per the menu; the seed is not registered, fixed deterministically by the sealed runner and disclosed",
        },
        "inputs_sha256": r1_hyper["inputs_sha256"],
        "registration_menu_sha256": inputs.menu_sha256,
        "dataset_manifest_hash": inputs.dataset_manifest_hash,
    }


# ---- phases ----------------------------------------------------------------------------


def phase_plan() -> int:
    t0 = time.monotonic()
    frozen = _verify_frozen_inputs()
    inputs = r1.load_and_bind()
    feats = r1.build_features(inputs)
    grid = _sealed_sessions(inputs)
    print(f"menu sha256 {inputs.menu_sha256} (sidecar-verified)")
    print(f"protocol raw {inputs.protocol_raw_sha256[:16]}... canonical {inputs.protocol_canonical_sha256[:16]}...")
    print(
        f"tnull calibration-v3: CALIBRATED (sha {frozen['calibration_v3_sha256'][:16]}...)"
    )
    print(f"round-1 runner sha256 {frozen['round1_runner_sha256'][:16]}... (identical-path pin)")
    print(f"round-1 selection sha256 {frozen['selection_sha256'][:16]}... (promoted frozen)")
    print(
        f"sealed window: ordinals {r1.ORD_SEALED_START}..{r1.ORD_SEALED_END}"
        f" = {grid[0]}..{grid[-1]} ({len(grid)} sessions) -- opening once"
    )
    print(f"HAR h20 origins: {feats.har_first_origin}..{feats.har_last_origin}")
    print(f"evaluable r name-sessions (union, feature values only): {feats.n_r_union}")
    print(f"registry db: {r1.REGISTRY_PATH}")
    print(f"NO sealed outcome computed or viewed by this phase")
    print(f"elapsed {time.monotonic() - t0:.1f}s")
    return 0


def phase_register() -> int:
    frozen = _verify_frozen_inputs()
    inputs = r1.load_and_bind()
    registry = r1._open_registry()
    try:
        scope_key = r1._scope(inputs, SEALED_CONFIGS[0]).scope_key()
        before = registry.count_scope(scope_key)
        for config_id in SEALED_CONFIGS:
            trial_id = _trial_id(config_id)
            if registry.is_registered(trial_id):
                status = registry.status(trial_id)
                if status in ("COMPLETED", "FAILED"):
                    raise r1.Refused(
                        f"{trial_id} is already scored ({status}) -- ABORT: the seal on"
                        " that cell is consumed; one scored run per cell, never overwrite"
                    )
                print(f"{trial_id}: already REGISTERED (resume skip)")
                continue
            hyper = _hyperparameters_sealed(inputs, config_id, frozen)
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
                    _sealed_sessions(inputs)[0],
                    _sealed_sessions(inputs)[-1],
                ),
                hyperparameters=hyper,
                scope_key=r1._scope(inputs, config_id).scope_key(),
            )
            registry.register(record, r1._scope(inputs, config_id))
            print(f"registered {trial_id} ({r1.GATE_RULES[config_id].split(':')[0]})")
        after = registry.count_scope(scope_key)
        print(
            f"registry: {r1.REGISTRY_PATH}; scope rows {before} -> {after} (cap 32;"
            " the sealed rows land under the round-1 scope_key so the menu's single"
            " c09-vrp-e budget keeps binding); NO sealed outcome computed or viewed"
        )
    finally:
        registry.close()
    return 0


def phase_execute() -> int:
    frozen = _verify_frozen_inputs()
    inputs = r1.load_and_bind()
    feats = r1.build_features(inputs)
    r1.SLOT_DIR.mkdir(parents=True, exist_ok=True)
    r1.TRIALS_DIR.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(r1.LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise r1.Refused(
                "another vrp-cond execution holds the lock -- one run at a time"
            ) from None
        registry = r1._open_registry()
        try:
            for config_id in SEALED_CONFIGS:
                trial_id = _trial_id(config_id)
                artifact = _artifact_path(config_id)
                status = registry.status(trial_id)
                if artifact.exists():
                    if status == "COMPLETED":
                        print(f"{trial_id}: COMPLETED already (resume skip)")
                        continue
                    raise r1.Refused(
                        f"{artifact} exists but trial is {status} -- inconsistent state,"
                        " refusing (one scored run per cell)"
                    )
                if status != "REGISTERED":
                    raise r1.Refused(f"{trial_id} is {status}, not REGISTERED -- refusing")
                hyper = _hyperparameters_sealed(inputs, config_id, frozen)
                git_sha = r1._git_head(r1.REPO_ROOT)
                registry.mark_running(
                    trial_id,
                    git_sha=git_sha,
                    config_hash=r1._config_hash(hyper),
                    dataset_manifest_hash=inputs.dataset_manifest_hash,
                    at=r1._utcnow(),
                )
                if config_id.startswith("xe-"):
                    base_rows = xsmom_base_sealed(inputs)
                else:
                    base_rows = pead_base_sealed(inputs)
                payload = _score_sealed(inputs, feats, base_rows, config_id)
                tallies = payload["condition_tallies"]
                if tallies["abstained_condition_not_evaluable"] == tallies["base_entries"]:
                    registry.fail(
                        trial_id,
                        "every sealed base entry carried a NOT_EVALUABLE condition"
                        " -- feature machinery defect, not an outcome",
                        at=r1._utcnow(),
                    )
                    raise r1.Refused(
                        f"{trial_id}: every sealed base entry NOT_EVALUABLE (machinery"
                        " defect; trial FAILED, no artifact written)"
                    )
                body = {"stamp": _stamp(inputs, config_id, frozen), "payload": payload}
                artifact.write_text(
                    json.dumps(body, indent=2, sort_keys=True) + "\n", encoding="utf-8"
                )
                registry.complete(
                    trial_id, metrics_uri=str(artifact), outcome_at=r1._utcnow()
                )
                omo = payload["on_minus_off"].get("on_minus_off_5bp")
                fl = payload["power_floor"]
                print(
                    f"{trial_id}: COMPLETED artifact={artifact}"
                    f" n_on={fl['n_on_fired']}/{fl['required_n_on']}"
                    f" on_minus_off_5bp={omo if omo is None else round(omo, 6)}"
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
        or stamp.get("scope_id") != SCOPE_E
        or stamp.get("round") != ROUND
    ):
        raise r1.Refused(f"{artifact} does not bind {_trial_id(config_id)}")
    if stamp.get("registration_menu_sha256") != inputs.menu_sha256:
        raise r1.Refused(f"{artifact} was executed against a different menu hash")
    if stamp.get("dataset_manifest_hash") != inputs.dataset_manifest_hash:
        raise r1.Refused(f"{artifact} was executed against different inputs")
    if stamp.get("round1_runner_sha256") != frozen["round1_runner_sha256"]:
        raise r1.Refused(f"{artifact} lost its identical-path pin")
    return body


def phase_stamp() -> int:
    frozen = _verify_frozen_inputs()
    inputs = r1.load_and_bind()
    if SEAL_PATH.exists():
        raise r1.Refused(f"{SEAL_PATH} already exists -- the sealed stamp is one-shot")
    artifacts = {
        config_id: _read_sealed_artifact(inputs, frozen, config_id)
        for config_id in SEALED_CONFIGS
    }
    criteria_text = inputs.slot["acceptance_criteria"][1]
    floors_text = inputs.slot["acceptance_criteria"][2]
    nominees: dict[str, Any] = {}
    for nominee in NOMINEES:
        payload = artifacts[nominee]["payload"]
        family = payload["family"]
        omo = payload["on_minus_off"]
        decisive = omo.get("on_minus_off_5bp")
        decisive15 = omo.get("on_minus_off_15bp")
        boot = payload.get("bootstrap", {})
        p = boot.get("p_one_sided")
        fl = payload["power_floor"]
        crit: dict[str, Any] = {
            "1_on_minus_off_gt_0": decisive is not None and decisive > 0.0,
            "2_bootstrap_p_lt_0.10": p is not None and p < 0.10,
            "3_beats_same_family_placebos": {},
            "4_on_minus_off_15bp_gt_0": decisive15 is not None and decisive15 > 0.0,
        }
        crit3_all_met = True
        crit3_detail: dict[str, Any] = {}
        for placebo in FAMILY_PLACEBOS[family]:
            p_omo = artifacts[placebo]["payload"]["on_minus_off"].get("on_minus_off_5bp")
            if p_omo is None:
                crit3_detail[placebo] = {
                    "placebo_on_minus_off_5bp": None,
                    "exceeded": True,
                    "note": "placebo decisive delta undefined on the identical path (no evaluable ON or OFF side) -- trivially exceeded, disclosed",
                }
            else:
                met = decisive is not None and decisive > p_omo
                crit3_detail[placebo] = {
                    "placebo_on_minus_off_5bp": p_omo,
                    "exceeded": met,
                }
                crit3_all_met = crit3_all_met and met
        crit["3_beats_same_family_placebos"] = crit3_detail
        crit["3_all_met"] = crit3_all_met and decisive is not None
        floor_met = fl["met_on_fired"]
        if not floor_met:
            verdict = "NOT_EVALUABLE-SEALED"
            verdict_reason = (
                f"power floor unmet: n_on_fired {fl['n_on_fired']} < {fl['required_n_on']}"
                " (below floor = NOT_EVALUABLE-SEALED, never FAIL; the registration"
                " flags the xe floor as likely unmeetable on 3 sealed XSMOM rebalances)"
                if family == "xe"
                else f"power floor unmet: n_on_fired {fl['n_on_fired']} < {fl['required_n_on']}"
            )
        elif decisive is None:
            verdict = "NOT_EVALUABLE-SEALED"
            verdict_reason = (
                "floors met but the decisive matched-sessions column is unevaluable"
                " (empty ON or OFF side of complete trades) -- never FAIL"
            )
        elif all(
            (
                crit["1_on_minus_off_gt_0"],
                crit["2_bootstrap_p_lt_0.10"],
                crit["3_all_met"],
                crit["4_on_minus_off_15bp_gt_0"],
            )
        ):
            verdict = "SURVIVOR-CANDIDATE"
            verdict_reason = "all four sealed criteria hold -- nomination only; nothing adopts"
        else:
            verdict = "DEFLATED"
            verdict_reason = "at least one sealed criterion missed (any miss = DEFLATED)"
        # cross-reading disclosure: criteria 3-4 on the cond-minus-base delta
        delta5 = payload["deltas"]["cond_minus_base_5bp"]
        delta15 = payload["deltas"]["cond_minus_base_15bp"]
        crit3_alt = True
        crit3_alt_detail: dict[str, Any] = {}
        for placebo in FAMILY_PLACEBOS[family]:
            p_delta = artifacts[placebo]["payload"]["deltas"]["cond_minus_base_5bp"]
            met = delta5 is not None and (p_delta is None or delta5 > p_delta)
            crit3_alt_detail[placebo] = {
                "placebo_cond_minus_base_5bp": p_delta,
                "exceeded": met,
            }
            crit3_alt = crit3_alt and met
        crit4_alt = delta15 is not None and delta15 > 0.0
        cross = {
            "reading": "criteria 3-4 re-evaluated on the conditioned-minus-base delta (the round-1 selection metric family) instead of the decisive ON-minus-OFF column",
            "3_beats_placebos_on_cond_minus_base": crit3_alt,
            "3_detail": crit3_alt_detail,
            "4_cond_minus_base_15bp_gt_0": crit4_alt,
            "cond_minus_base_5bp": delta5,
            "cond_minus_base_15bp": delta15,
            "hypothesis_falsification_trigger_fired": delta5 is not None and delta5 <= 0.0,
            "verdict_would_change": None,
        }
        alt_all = (
            crit["1_on_minus_off_gt_0"]
            and crit["2_bootstrap_p_lt_0.10"]
            and crit3_alt
            and crit4_alt
        )
        if floor_met and decisive is not None:
            alt_verdict = "SURVIVOR-CANDIDATE" if alt_all else "DEFLATED"
            cross["verdict_would_change"] = alt_verdict != verdict
        nominees[nominee] = {
            "verdict": verdict,
            "verdict_reason": verdict_reason,
            "power_floor": fl,
            "on_minus_off": omo,
            "bootstrap": boot,
            "criteria": crit,
            "deltas_disclosure": payload["deltas"],
            "cross_reading_cond_minus_base": cross,
            "condition_tallies": payload["condition_tallies"],
        }
    placebos: dict[str, Any] = {}
    for placebo in ("xe-lag21", "xp-lag21", "xp-shuffle"):
        payload = artifacts[placebo]["payload"]
        placebos[placebo] = {
            "family": payload["family"],
            "gate_rule": payload["gate_rule"],
            "power_floor": payload["power_floor"],
            "on_minus_off": payload["on_minus_off"],
            "deltas_disclosure": payload["deltas"],
            "condition_tallies": payload["condition_tallies"],
            "role": "control on the identical path (criterion 3); no verdict",
        }
    record = {
        "stamp": _stamp(inputs, "sealed-round", frozen),
        "round": ROUND,
        "operator_rulings_2026_09_24": OPERATOR_RULINGS_2026_09_24,
        "sealed_window": {
            "ordinals": [r1.ORD_SEALED_START, r1.ORD_SEALED_END],
            "sessions": [SEALED_FIRST_ISO, SEALED_LAST_ISO],
            "n_sessions": N_SEALED_SESSIONS,
            "opened_once": True,
        },
        "frozen_inputs": {
            "round1_selection_path": str(r1.SELECTION_PATH),
            "round1_selection_sha256": frozen["selection_sha256"],
            "promoted": {
                f: {"config": FROZEN_PROMOTED[f][0], "tuning_delta_5bp": FROZEN_PROMOTED[f][1]}
                for f in FROZEN_PROMOTED
            },
            "tuning_deltas_are_selection_evidence_only": True,
            "scope_O": "WITHDRAWN (scope-O-withdrawal.json); no ox/op cell ran",
        },
        "registered_sealed_acceptance": criteria_text,
        "registered_power_floors": floors_text,
        "criteria_reading": {
            "decisive_delta": "the matched-sessions ON-minus-OFF column (criterion 1 names it; 2 bootstraps it; 3 compares it to the placebos' identical-path columns; 4 recomputes it at 15bp RT)",
            "on_off_semantics": {
                "entry gates": "fired (ON) vs base-not-fired (OFF) complete trades, matched region",
                "xp-size-mkt": "half-sized (condition-high) vs full-sized entries",
            },
            "round1_docstring_pin": "the round-1 runner stamped this column as 'the decisive SEALED criterion 1'",
            "cross_reading_disclosed": "per-nominee cross_reading_cond_minus_base block re-evaluates criteria 3-4 on the cond-minus-base delta and flags any verdict change",
        },
        "verdict_vocabulary": inputs.slot["verdict_vocabulary"],
        "nominees": nominees,
        "placebos": placebos,
        "scope_accounting": {
            "scope_id": SCOPE_E,
            "scope_key": r1._scope(inputs, SEALED_CONFIGS[0]).scope_key(),
            "rows_this_round": len(SEALED_CONFIGS),
            "rows_total_after": 25,
            "cap": 32,
            "note": "sealed rows share the round-1 scope_key so the menu's single 32-cap for c09-vrp-e keeps binding",
        },
        "conventions_disclosed": [
            "on_minus_off computed at both round turns (5bp decisive, 15bp for criterion 4); round 1 stamped 5bp only",
            "bootstrap seed not registered; fixed deterministically (sha256('vrp-cond-sealed-bootstrap-1')) and disclosed in every bootstrap block",
            "bootstrap over the 63-session sealed grid with per-session ON/OFF net counts+sums; moving blocks of 10 sessions, 2000 resamples, None-statistic resamples skipped (diagnostics.block_bootstrap_ci draw conventions)",
            "one-sided p = #{resample delta <= 0}/valid; (+1) correction stamped as sensitivity",
            "a placebo with an undefined sealed decisive delta is trivially exceeded (disclosed per comparison)",
            "entries are sealed-window entries; holds read forward to the panel edge, incomplete holds dropped and counted (house path)",
        ],
        "notes": [
            "Nothing adopts on this round: SURVIVOR-CANDIDATE is at most a nomination;"
            " promotion runs only through >= 20 forward sealed cards (promotion.py).",
            "One scored run per cell (INV-13): trial rows registered before any sealed"
            " outcome was computed or viewed; per-trial artifacts under trials/*-r2.json.",
            "The xe nominee sits on the RICH side (sign-flipped vs the registered"
            " hypothesis direction); tested once exactly as registered.",
        ],
    }
    SEAL_PATH.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    for nominee in NOMINEES:
        blk = nominees[nominee]
        omo = blk["on_minus_off"].get("on_minus_off_5bp")
        p = blk["bootstrap"].get("p_one_sided")
        print(
            f"{nominee}: verdict={blk['verdict']}"
            f" n_on={blk['power_floor']['n_on_fired']}/{blk['power_floor']['required_n_on']}"
            f" on_minus_off_5bp={omo if omo is None else round(omo, 6)}"
            f" p={p if p is None else round(p, 4)}"
            f" crit1={blk['criteria']['1_on_minus_off_gt_0']}"
            f" crit2={blk['criteria']['2_bootstrap_p_lt_0.10']}"
            f" crit3={blk['criteria']['3_all_met']}"
            f" crit4={blk['criteria']['4_on_minus_off_15bp_gt_0']}"
        )
    for placebo in ("xe-lag21", "xp-lag21", "xp-shuffle"):
        omo = placebos[placebo]["on_minus_off"].get("on_minus_off_5bp")
        d5 = placebos[placebo]["deltas_disclosure"]["cond_minus_base_5bp"]
        print(
            f"placebo {placebo}: n_on={placebos[placebo]['power_floor']['n_on_fired']}"
            f" on_minus_off_5bp={omo if omo is None else round(omo, 6)}"
            f" cond_minus_base_5bp={d5 if d5 is None else round(d5, 6)}"
        )
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
        help="INV-13: write the 5 sealed trial rows (REGISTERED, no outcome)",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="one-shot scored run per sealed cell (resumable across cells)",
    )
    parser.add_argument(
        "--stamp",
        action="store_true",
        help="read ONLY the executed artifacts; apply criteria 1-4; write sealed-round.json",
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
