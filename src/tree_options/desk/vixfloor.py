"""SPEC 1 ``vixfloor`` (2026-09-30): the VIX-regime-gated split of the
``bull_hold_expiry`` survivor.

A standalone, read-only scorer over a FROZEN longrun corpus: it re-runs the
floor-clearing always-bullish/expiry rule split into two pre-registered arms
by the market's own vol-regime location -- the percentile of the VIX close
within its trailing 252 sessions -- and tests whether the survivor's edge is
regime-concentrated. It reuses :func:`tree_options.desk.longrun.score_run`
verbatim (injected deterministic arms, synthetic all-ok receipts); nothing is
written into any live run dir, and no model is called.

Rule (pre-registered, cut fixed at 0.5 before any scoring)::

    VIXpct(d) = #{ s in W(d) : VIX[s] < VIX[v(d)] } / |W(d)|
      v(d)   = the last indices-store session STRICTLY BEFORE board session d
      W(d)   = the 252 indices-store sessions ending at v(d), inclusive
      VIX[x] = the VIX daily CLOSE of session x (artifacts/desk-store/indices/VIX.csv)

    arm "bull_expiry_vixlo": rule_always_bullish(horizon="expiry", key="board_order")
                             IF VIXpct(board.session) <  0.5 ELSE (None, None)
    arm "bull_expiry_vixhi": the same rule, entering when VIXpct >= 0.5

INFORMATION SET (lookahead discipline). Per decision, the gate reads only VIX
CSV rows dated STRICTLY BEFORE the board session (the prior-close convention);
the pick reads only the board's own rows. No per-name data, no chain history,
no ``vol_state``/120-session clock, no outcome data. Unlike
``board_universe.median_dte`` (a block median over the WHOLE served window
applied to day 1 -- the known anti-pattern), the trailing window here ends at
the prior session and never touches the decision session or later.

Costs: every figure is net of the harness's flat CostModel round trip
($14.60 per two-leg spread; ``outcomes.CostModel`` applied inside the frozen
outcome table). The table's marks are historical valuation proxies, not
executable fills -- the same caveat every longrun digest carries.

PAPER research only: this module never trades, arms nothing, and its verdicts
are research output for the operator, never promotions.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np

from tree_options.desk.longrun import (
    Board,
    OutcomeCache,
    PluginContext,
    PolicySpec,
    Protocol,
    arms_of,
    longrun_engine_identity,
    paired,
    plugin,
    rule_always_bullish,
    rule_fixed_structure,
    score_run,
    session_sums,
)


def vixfloor_engine_manifest() -> dict[str, Any]:
    """Bind this evaluator's actual source and the shared scorer's identity.

    The shared identity includes shipped scoring helpers and Python/NumPy
    versions. Injected callbacks and mutable resident code are not inferred.
    """
    manifest = {
        "modules": {__name__: hashlib.sha256(Path(__file__).read_bytes()).hexdigest()},
        "longrun_engine_sha256": longrun_engine_identity(),
    }
    digest = hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {**manifest, "sha256": digest}


EVAL_SCHEMA = "desk-vixfloor-eval/1"
#: the pre-registered percentile cut, fixed before any scoring (SPEC 1.1)
CUT = 0.5
#: the trailing VIX window, in indices-store sessions (SPEC 1.1)
WINDOW = 252
#: the two pre-registered arms and their minimum entered-AND-evaluable floors
#: before any claim (SPEC 1.3); an arm under its floor is NOT_EVALUABLE
N_FLOORS = {"bull_expiry_vixlo": 300, "bull_expiry_vixhi": 120}
#: the descriptive (context-only, never in the Holm family) splits (SPEC 1.1)
DESCRIPTIVE_BASES = (
    ("put_credit_expiry_vix", "put_credit", "expiry"),
    ("call_debit_hold5_vix", "call_debit", "hold:5"),
)
#: the frozen corpus this spec is evaluated on (data-map 2026-09-30)
DEFAULT_BOARDS = (
    "/home/alexk/documents/tree_options/artifacts/desk-store/"
    "evaluations/longrun/20260929T012836Z/boards.jsonl"
)
DEFAULT_VIX = "/home/alexk/documents/tree_options/artifacts/desk-store/indices/VIX.csv"
DEFAULT_TABLE = "/home/alexk/.local/state/trex-longrun/outcome-table-v2.jsonl"
#: the frozen run's own protocol mirrors what score_run gets here (plan.json)
RUN_RANDOM_HORIZONS = ("intraday", "eod", "hold:5", "expiry")
RUN_CUTOFF = "2026-08-14"
PERM_DRAWS = 2_000  # the pre-registered permutation null draws (SPEC 1.3)

Choice = tuple[str | None, str | None]
RuleFn = Callable[[Board], Choice]
OutcomeFn = Callable[[str, str, str | None], Mapping[str, Any] | None]

VIX_LO = "lo"  # VIXpct <  CUT: the low-half regime arm
VIX_HI = "hi"  # VIXpct >= CUT: the high-half regime arm


# ------------------------------------------------------------------ the gate


def load_vix_closes(
    path: Path, *, source_hashes: dict[str, str] | None = None
) -> dict[date, float]:
    """{session: VIX daily close} from an indices-store CSV (date,*,close)."""
    closes: dict[date, float] = {}
    try:
        body = path.read_bytes()
    except FileNotFoundError as error:
        raise ValueError(f"{path}: no VIX rows") from error
    if source_hashes is not None:
        source_hashes["vix_sha256"] = hashlib.sha256(body).hexdigest()
    with io.StringIO(body.decode()) as stream:
        for row in csv.DictReader(stream):
            day, value = date.fromisoformat(str(row["date"])), float(row["close"])
            if not math.isfinite(value) or value <= 0:
                raise ValueError("VIX close must be finite and positive")
            if day in closes and closes[day] != value:
                raise ValueError("VIX close identity collision")
            closes[day] = value
    if not closes:
        raise ValueError(f"{path}: no VIX rows")
    return closes


def vix_percentiles(
    closes: Mapping[date, float], sessions: Sequence[str], *, window: int = WINDOW
) -> dict[str, float]:
    """VIXpct per board session: the fraction of the ``window`` sessions
    ending at the prior session (inclusive) whose close is STRICTLY below the
    prior session's close.

    Information set: ``closes`` rows dated strictly before each session only;
    the window is the trailing ``window`` indices-store sessions, so a session
    without ``window`` prior rows is an error (fail loud, never silently
    shortened -- the frozen corpus's 1990-> VIX history always has 252+)."""
    if window < 2:
        raise ValueError("window must be >= 2")
    dates = sorted(closes)
    out: dict[str, float] = {}
    for session in sessions:
        day = date.fromisoformat(session)
        prior = [d for d in dates if d < day]
        if not prior:
            raise ValueError(f"{session}: no VIX session strictly before it")
        if len(prior) < window:
            raise ValueError(f"{session}: only {len(prior)} prior VIX sessions (need {window})")
        v = float(closes[prior[-1]])
        trailing = prior[-window:]
        below = sum(1 for d in trailing if float(closes[d]) < v)
        out[session] = below / window
    return out


def vixfloor_rule(
    percentile_of: Mapping[str, float], *, side: str, base: RuleFn | None = None, cut: float = CUT
) -> RuleFn:
    """The gated rule: ``base`` (the survivor verbatim:
    ``rule_always_bullish(horizon="expiry", key="board_order")``) when the
    session's pre-computed VIX percentile is on the arm's side of ``cut``,
    else no entry. ``side`` "lo" enters iff pct < cut; "hi" iff pct >= cut."""
    if side not in (VIX_LO, VIX_HI):
        raise ValueError(f"side must be {VIX_LO!r} or {VIX_HI!r}")
    if not 0.0 < cut < 1.0:
        raise ValueError("cut must be in (0, 1)")
    pick = base if base is not None else rule_always_bullish(horizon="expiry")

    def rule(board: Board) -> Choice:
        choice, horizon = pick(board)
        pct = percentile_of.get(board.session)
        if pct is None:
            raise ValueError(f"{board.session}: no VIX percentile computed")
        enters = pct < cut if side == VIX_LO else pct >= cut
        return (choice, horizon) if enters else (None, None)

    return rule


def family_policies(percentile_of: Mapping[str, float]) -> list[PolicySpec]:
    """The two pre-registered arms (the Holm family)."""
    return [
        PolicySpec(f"bull_expiry_vix{side}", "rule", rule=vixfloor_rule(percentile_of, side=side))
        for side in (VIX_LO, VIX_HI)
    ]


def descriptive_policies(percentile_of: Mapping[str, float]) -> list[PolicySpec]:
    """The same VIX split on the other two survivors -- context only, never
    in the Holm family (SPEC 1.1: "descriptive, not in the family")."""
    out: list[PolicySpec] = []
    for stem, structure, horizon in DESCRIPTIVE_BASES:
        base = rule_fixed_structure(structure, horizon=horizon)
        for side in (VIX_LO, VIX_HI):
            out.append(
                PolicySpec(
                    f"{stem}{side}", "rule", rule=vixfloor_rule(percentile_of, side=side, base=base)
                )
            )
    return out


# ------------------------------------------------------------------ loaders


def load_boards(path: Path, *, source_hashes: dict[str, str] | None = None) -> list[Board]:
    """The frozen run's boards.jsonl -> Board objects (read-only)."""
    boards: list[Board] = []
    body = path.read_bytes()
    if source_hashes is not None:
        source_hashes["boards_sha256"] = hashlib.sha256(body).hexdigest()
    with io.StringIO(body.decode()) as stream:
        for line in stream:
            line = line.strip()
            if not line:
                continue
            doc = json.loads(line)
            boards.append(
                Board(
                    str(doc["snapshot"]),
                    str(doc["session"]),
                    str(doc["clock"]),
                    doc["rows"],
                    doc.get("context"),
                )
            )
    if not boards:
        raise ValueError(f"{path}: no boards")
    if len({board.snapshot for board in boards}) != len(boards):
        raise ValueError("duplicate board snapshot identity")
    return boards


def load_outcome_fn(
    path: Path, *, default_horizon: str = "intraday", source_hashes: dict[str, str] | None = None
) -> OutcomeFn:
    """The frozen outcome table -> the same lookup semantics as the harness's
    v2 outcome plug-in (``longrun._v2_outcome``'s table branch): keyed
    ``(snapshot, candidate_id, exit_mode)``, a missing horizon falls back to
    ``default_horizon``, and a ``no_fill`` / null-net row is unevaluable.
    Numeric fields may be JSON numbers (the v2 table) or JSON strings (the
    20260929-long table); both coerce through ``float``."""
    context = PluginContext(path.resolve().parent)
    lookup = plugin("outcome", "v2")(
        {"table": str(path.resolve()), "default_horizon": default_horizon}, context
    )
    if source_hashes is not None:
        source_hashes.update(context.shared["source_hashes"])
    return lookup


def deterministic_receipts(rule: RuleFn, boards: Sequence[Board]) -> dict[str, dict[str, Any]]:
    """All-ok synthetic receipts: the rule applied to every board (the
    deterministic-arm equivalent of an executed receipt series; applying it
    twice yields byte-identical receipts -- the A/A twin)."""
    receipts: dict[str, dict[str, Any]] = {}
    for board in boards:
        choice, horizon = rule(board)
        receipts[board.snapshot] = {
            "ok": True,
            "choice": choice,
            "horizon": horizon,
            "note": "vixfloor deterministic rule",
        }
    return receipts


def apply_rule_series(
    rule: RuleFn, boards: Sequence[Board], outcomes: OutcomeCache
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """The rule's realized series, recomputed outside the digest for the
    stats ``score_run`` does not emit: (per-board nets, per-board entered,
    per-board evaluable) aligned to ``boards``."""
    nets = np.zeros(len(boards))
    entered = np.zeros(len(boards))
    evaluable = np.zeros(len(boards))
    for i, board in enumerate(boards):
        choice, horizon = rule(board)
        if choice is None:
            continue
        entered[i] = 1.0
        value = outcomes.get(board.snapshot, choice, horizon)
        if value is not None:
            evaluable[i] = 1.0
            nets[i] = value[1]
    return nets, entered, evaluable


# ---------------------------------------------------------------- statistics


def per_entry_mean_ci(
    session_nets: np.ndarray, session_counts: np.ndarray, *, draws: int, seed: int
) -> dict[str, Any]:
    """Per-entry net mean with a session-clustered percentile-bootstrap CI:
    sessions are the resampling unit (an entry is never resampled alone), the
    mean re-weights by the resampled entry counts. Draws whose resample holds
    zero entries are dropped (reported)."""
    nets = np.asarray(session_nets, dtype=float)
    counts = np.asarray(session_counts, dtype=float)
    if counts.sum() <= 0:
        return {"n_entries": 0, "mean": None, "ci95": None, "draws_valid": 0}
    mean = float(nets.sum() / counts.sum())
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, nets.size, size=(draws, nets.size))
    sums = nets[idx].sum(axis=1)
    ns = counts[idx].sum(axis=1)
    ok = ns > 0
    gaps = sums[ok] / ns[ok]
    lo, hi = np.percentile(gaps, [2.5, 97.5]) if gaps.size else (float("nan"),) * 2
    return {
        "n_entries": int(counts.sum()),
        "mean": round(mean, 2),
        "ci95": [round(float(lo), 2), round(float(hi), 2)],
        "draws_valid": int(gaps.size),
    }


def split_contrast(
    session_nets: np.ndarray,
    session_counts: np.ndarray,
    lo_sessions: np.ndarray,
    *,
    draws: int,
    seed: int,
    perm_draws: int = PERM_DRAWS,
) -> dict[str, Any]:
    """The secondary contrast (SPEC 1.3): the per-entry mean gap
    ``vixlo - vixhi`` with (a) a session-clustered stratified bootstrap CI
    (each arm's sessions resampled within the arm) and (b) the pre-registered
    permutation null: ``perm_draws`` random splits of the UNGATED survivor's
    sessions into the observed group sizes -- how often a random split of
    these sizes produces an equal or larger gap.

    ``lo_sessions`` is the boolean mask of the vixlo sessions over the same
    session axis as the sums/counts (which here are the survivor's)."""
    nets = np.asarray(session_nets, dtype=float)
    counts = np.asarray(session_counts, dtype=float)
    mask = np.asarray(lo_sessions, dtype=bool)

    def gap(m: np.ndarray) -> float:
        return float(nets[m].sum() / counts[m].sum() - nets[~m].sum() / counts[~m].sum())

    observed = gap(mask)
    rng = np.random.default_rng(seed)
    n_lo = int(mask.sum())
    total = mask.size
    boots = np.empty(draws)
    for k in range(draws):
        lo_idx = rng.integers(0, n_lo, size=n_lo)
        hi_idx = rng.integers(0, total - n_lo, size=total - n_lo)
        lo_sessions_ = np.flatnonzero(mask)[lo_idx]
        hi_sessions_ = np.flatnonzero(~mask)[hi_idx]
        boots[k] = (
            nets[lo_sessions_].sum() / counts[lo_sessions_].sum()
            - nets[hi_sessions_].sum() / counts[hi_sessions_].sum()
        )
    lo_ci, hi_ci = np.percentile(boots, [2.5, 97.5])
    rng = np.random.default_rng([seed, 41])  # the permutation null's own stream
    perm = np.empty(perm_draws)
    for k in range(perm_draws):
        m = np.zeros(total, dtype=bool)
        m[rng.choice(total, size=n_lo, replace=False)] = True
        perm[k] = gap(m)
    ge = int(np.sum(perm >= observed))
    abs_ge = int(np.sum(np.abs(perm) >= abs(observed)))
    return {
        "n_lo_sessions": n_lo,
        "n_hi_sessions": total - n_lo,
        "gap": round(observed, 2),
        "ci95_cluster_bootstrap": [round(float(lo_ci), 2), round(float(hi_ci), 2)],
        "permutation_null": {
            "draws": perm_draws,
            "p_one_sided_ge": round((1 + ge) / (1 + perm_draws), 4),
            "share_ge": round(ge / perm_draws, 4),
            "share_abs_ge": round(abs_ge / perm_draws, 4),
            "band95": [round(float(v), 2) for v in np.percentile(perm, [2.5, 97.5])],
        },
    }


# --------------------------------------------------------------- evaluation


def arm_protocol(base: Protocol, name: str) -> Protocol:
    """``base`` with the incumbent set to the arm itself, so ``score_run``
    matches the random null to THIS arm: its own entry rate, its own boards
    (the per-arm-vs-own-null rule; a shared null would mix denominators)."""
    return replace(base, incumbent=name)


def evaluate_arm(
    spec: PolicySpec,
    boards: Sequence[Board],
    outcome: OutcomeFn,
    base: Protocol,
    *,
    repeats: int = 1,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """One arm through the harness verbatim: ``score_run`` on the frozen
    boards with injected deterministic receipts. Returns (trimmed standing +
    null/aa, the full digest for the receipt file). With ``repeats=2`` the arm
    is registered twice -- the A/A twin (a deterministic rule must agree with
    itself on 100% of boards)."""
    policy = PolicySpec(spec.name, spec.kind, repeats=repeats, rule=spec.rule)
    assert policy.rule is not None
    arms = arms_of([policy])
    receipts = {arm.name: deterministic_receipts(policy.rule, boards) for arm in arms}
    digest = score_run(
        list(boards), arms, receipts, OutcomeCache(outcome), arm_protocol(base, policy.name),
        assessment_class="retrospective_descriptive",  # a frozen corpus re-scored after the fact
    )
    docs = {}
    for arm in arms:
        standing = next(s for s in digest["standings"] if s["arm"] == arm.name)
        docs[arm.name] = {
            "boards": standing["boards"],
            "entered": standing["entered"],
            "unevaluable": standing["unevaluable"],
            "evaluated": standing["entered"] - standing["unevaluable"],
            "entry_rate": standing["entry_rate"],
            "net_total": standing["net_total"],
            "net_ci95": standing["net_ci95"],
            "gross_total": standing["gross_total"],
            "net_per_evaluated_entry": standing["net_per_evaluated_entry"],
            "vs_random": standing["vs_random"],
            "null_percentile": standing["null_percentile"],
            "pick_null": standing["pick_null"],
            "stability_vs_random": standing["stability_vs_random"],
        }
    doc: dict[str, Any] = {
        "policy": policy.name,
        "repeats": repeats,
        "arms": docs,
        "random_null": digest["random_null"],
    }
    if repeats > 1:
        doc["aa"] = digest["aa"]
    return doc, digest


def run_evaluation(
    boards_path: Path,
    vix_path: Path,
    table_path: Path,
    *,
    draws: int = 10_000,
    seed: int = 20260928,
    random_seeds: int = 1000,
    perm_draws: int = PERM_DRAWS,
    cutoff: str | None = RUN_CUTOFF,
) -> dict[str, Any]:
    """The full SPEC 1 evaluation over the frozen corpus (read-only)."""
    source_hashes: dict[str, str] = {}
    boards = load_boards(boards_path, source_hashes=source_hashes)
    sessions = sorted({b.session for b in boards})
    closes = load_vix_closes(vix_path, source_hashes=source_hashes)
    percentile_of = vix_percentiles(closes, sessions)
    outcome = load_outcome_fn(table_path, source_hashes=source_hashes)
    outcomes = OutcomeCache(outcome)
    outcomes.bind_boards(boards)
    protocol = Protocol(
        draws=draws,
        seed=seed,
        random_seeds=random_seeds,
        random_horizons=RUN_RANDOM_HORIZONS,
        cutoff=cutoff,
    )
    pcts = np.array([percentile_of[s] for s in sessions])

    # the ungated survivor on the same boards (baseline + contrast input)
    survivor = rule_always_bullish(horizon="expiry")
    nets, entered, evaluable = apply_rule_series(survivor, boards, outcomes)
    s_nets = session_sums([b.session for b in boards], nets.tolist(), sessions)
    s_counts = session_sums([b.session for b in boards], evaluable.tolist(), sessions)
    survivor_doc = {
        "name": "bull_hold_expiry (ungated survivor)",
        "boards": len(boards),
        "entered": int(entered.sum()),
        "evaluated": int(evaluable.sum()),
        "net_total": round(float(nets.sum()), 2),
        "net_per_evaluated_entry": (
            round(float(nets.sum() / evaluable.sum()), 2) if evaluable.sum() else None
        ),
        **per_entry_mean_ci(s_nets, s_counts, draws=draws, seed=seed),
    }

    family = family_policies(percentile_of)
    arm_docs: dict[str, Any] = {}
    digests: dict[str, Any] = {}
    for spec in family:  # the A/A twin: repeats=2 (SPEC 1.3)
        doc, digest = evaluate_arm(spec, boards, outcome, protocol, repeats=2)
        doc["n_floor"] = N_FLOORS[spec.name]
        doc["floor_met"] = all(d["evaluated"] >= doc["n_floor"] for d in doc["arms"].values())
        arm_docs[spec.name] = doc
        digests[spec.name] = digest
    from tree_options.desk.longrun import holm

    pvalues = {
        name: doc["arms"][f"{name}#1"]["vs_random"]["p_one_sided"] for name, doc in arm_docs.items()
    }
    adjusted = holm(pvalues)
    for name, doc in arm_docs.items():
        doc["holm_p"] = round(adjusted[name], 4)

    # per-arm clustered per-entry CI + vs-survivor paired diff (report stats
    # score_run does not emit; series recomputed and cross-checked below)
    for spec in family:
        assert spec.rule is not None
        nets_a, _, eval_a = apply_rule_series(spec.rule, boards, outcomes)
        a_nets = session_sums([b.session for b in boards], nets_a.tolist(), sessions)
        a_counts = session_sums([b.session for b in boards], eval_a.tolist(), sessions)
        standing = arm_docs[spec.name]["arms"][f"{spec.name}#1"]
        if abs(standing["net_total"] - round(float(nets_a.sum()), 2)) > 0.05 or standing[
            "evaluated"
        ] != int(eval_a.sum()):
            raise AssertionError(f"{spec.name}: recomputed series disagrees with the digest")
        standing["per_entry_ci"] = per_entry_mean_ci(a_nets, a_counts, draws=draws, seed=seed)
        standing["vs_unconditioned_survivor"] = paired(a_nets, s_nets, draws=draws, seed=seed)

    descriptive: dict[str, Any] = {}
    for spec in descriptive_policies(percentile_of):
        doc, _ = evaluate_arm(spec, boards, outcome, protocol)
        descriptive[spec.name] = doc["arms"][spec.name]

    secondary = split_contrast(
        s_nets, s_counts, pcts < CUT, draws=draws, seed=seed, perm_draws=perm_draws
    )
    both_above = all(doc["floor_met"] for doc in arm_docs.values())
    return {
        "schema": EVAL_SCHEMA,
        "verdict": "NOT_PROMOTABLE",
        "evidence_kind": "BACKTEST",
        "execution_authorized": False,
        "exact_external_economics": False,
        "live_money": False,
        "statistics_version": "session-bootstrap-multiplicity/2",
        "engine": vixfloor_engine_manifest(),
        "spec": {
            "id": "vixfloor",
            "cut": CUT,
            "window": WINDOW,
            "value": "close",
            "arms": list(N_FLOORS),
            "n_floors": dict(N_FLOORS),
            "holm_family": list(N_FLOORS),
            "rule": "rule_always_bullish(horizon='expiry', key='board_order') "
            "gated by VIXpct(d) < 0.5 (lo) / >= 0.5 (hi)",
        },
        "inputs": {
            "boards": str(boards_path),
            "vix": str(vix_path),
            "outcome_table": str(table_path),
            "boards_count": len(boards),
            "sessions": len(sessions),
            "first": sessions[0],
            "last": sessions[-1],
            "source_hashes": source_hashes,
            "cost_semantics_verified": False,
            "costs": "declared, unverified table cost assumption: flat CostModel round trip $14.60 (2 legs x 2 fills x "
            "($0.03 x 100 + $0.65)), net-of-cost everywhere; marks are "
            "historical valuation proxies, not executable fills",
        },
        "vix_gate": {
            "coverage": f"{len(percentile_of)}/{len(sessions)} sessions",
            "sessions_below_cut": int(np.sum(pcts < CUT)),
            "sessions_at_or_above": int(np.sum(pcts >= CUT)),
            "pct_min": round(float(pcts.min()), 4),
            "pct_max": round(float(pcts.max()), 4),
        },
        "protocol": {
            "draws": draws,
            "seed": seed,
            "random_seeds": random_seeds,
            "random_horizons": list(RUN_RANDOM_HORIZONS),
            "cutoff": cutoff,
            "perm_draws": perm_draws,
        },
        "survivor": survivor_doc,
        "arms": arm_docs,
        "family": {
            "holm_p": {k: round(v, 4) for k, v in adjusted.items()},
            "all_arms_above_n_floor": both_above,
            "not_evaluable_reason": None
            if both_above
            else "an arm is under its pre-registered minimum n "
            "(SPEC 1.3: an arm under its floor is NOT_EVALUABLE)",
        },
        "secondary_contrast": secondary,
        "descriptive": descriptive,
        "digests": digests,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--boards", default=DEFAULT_BOARDS, type=Path)
    parser.add_argument("--vix", default=DEFAULT_VIX, type=Path)
    parser.add_argument("--table", default=DEFAULT_TABLE, type=Path)
    parser.add_argument(
        "--out",
        default=Path("/home/alexk/.local/state/trex-strategy-20260930/vixfloor-results.json"),
        type=Path,
    )
    parser.add_argument("--draws", type=int, default=10_000)
    parser.add_argument("--seed", type=int, default=20260928)
    parser.add_argument("--random-seeds", type=int, default=1000)
    parser.add_argument("--perm-draws", type=int, default=PERM_DRAWS)
    args = parser.parse_args(argv)
    doc = run_evaluation(
        args.boards,
        args.vix,
        args.table,
        draws=args.draws,
        seed=args.seed,
        random_seeds=args.random_seeds,
        perm_draws=args.perm_draws,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(doc, indent=1, default=str), encoding="utf-8")
    for name, arm in doc["arms"].items():
        s = arm["arms"][f"{name}#1"]
        print(
            f"{name}: entered={s['entered']} evaluated={s['evaluated']} "
            f"(floor {arm['n_floor']}, met={arm['floor_met']}) "
            f"net_total={s['net_total']} per_entry={s['net_per_evaluated_entry']} "
            f"vs_random p={s['vs_random']['p_one_sided']} holm={arm['holm_p']}"
        )
    sec = doc["secondary_contrast"]
    print(
        f"contrast gap={sec['gap']} ci={sec['ci95_cluster_bootstrap']} "
        f"perm_p={sec['permutation_null']['p_one_sided_ge']}"
    )
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
