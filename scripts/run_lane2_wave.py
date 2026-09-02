#!/usr/bin/env python3
"""Run the theory-program lane-2 wave against the FROZEN bars manifest.

Wave-0 tooling (owner rulings 2026-09-02, P1 package ratified after the
event-5 PASS): the pre-declared 20-slot menu below is the ENTIRE theory
program's config budget (arm-A 18 + arm-B 2 of the 32-cap; several slots
alias one executed config — the ledger's alias map is the receipt). The
D4 fold geometry is mt34 / val6 / E2 / test13 / roll13 (3 folds, 39
tested Fridays, refits 13/26/40); H5 = 5 GRID Fridays under Agenda C's
re-denomination bundle.

Pre-registration discipline (the register-at-execution registry makes
upfront ROWS impossible — a pre-inserted trial_id would collide with the
runner's own register and double-burn the cap): the menu + exact params +
hypothesis texts + the frozen manifest/protocol hashes are written to a
COMMITTED ledger artifact BEFORE any outcome-bearing execution, and every
execution asserts param-identity against it (drift refuses).

Sequencing (Agenda A): T-NULL x3 runs FIRST; their realized pooled
stride4_cohort_ic_sd + fidelity_rho (D8) are appended to the ledger by
``--calibrate`` and become the tripwire priors — the synthetic priors are
FORBIDDEN on the real lane. ``--execute`` REFUSES any non-null config
while the calibration block is absent.

Holdout (D7): already enforced by the machinery — the w5 guard refuses
any fold TEST session inside FINAL_HOLDOUT_DATES before registration, the
world's decision sessions exclude the sealed window, and per-position
exit/label-window tagging is stamped. The driver asserts the exclusion
once, defensively.

This driver spends NO seal authority: it is research execution against
the frozen manifest (verify_sealed_inputs is the availability check; only
execute_sealed_run consumes).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

for _name in (
    "OPENBLAS_NUM_THREADS",
    "OMP_NUM_THREADS",
    "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
):
    os.environ[_name] = "1"

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:  # pragma: no cover - import plumbing
    sys.path.insert(0, str(REPO_ROOT / "src"))
if str(REPO_ROOT / "scripts") not in sys.path:  # pragma: no cover
    sys.path.insert(0, str(REPO_ROOT / "scripts"))

from tree_options.evaluation.stats import ScoredLabel  # noqa: E402
from tree_options.options.strategy import OptionsStrategyConfig  # noqa: E402
from tree_options.protocol.holdout import FINAL_HOLDOUT_DATES  # noqa: E402
from tree_options.registry.sqlite import TrialRegistry  # noqa: E402
from tree_options.seal.verified_inputs import (  # noqa: E402
    SealedInputPaths,
    verify_sealed_inputs,
)
from tree_options.trials.g4_event import (  # noqa: E402
    G4_FIXED_CLOCK,
    G4_STALENESS_SESSIONS,
    build_lane2_world,
)
from tree_options.trials.null_score import (  # noqa: E402
    NULL_SCORE_MODEL_FAMILY,
    null_score,
)
from tree_options.trials.options_run import (  # noqa: E402
    OptionsSplitOverride,
    run_options_trial,
)

# (D4, owner ruling 2026-09-02) the theory-wave fold geometry in GRID
# Fridays — H, embargo, val, test, roll, min_train. mt34 buys the third
# fold (the sealed event's mt40/val12 gives only 2); refits at 13/26/40.
WAVE0_GEOMETRY = (5, 2, 6, 13, 13, 34)

# T-MOM's declared model identity: the driver computes mom_20 IN GRID
# WEEKS (Agenda C re-denomination) from the PIT-visible dataset bars; the
# runner treats a non-null family as a caller-declared identity string.
MOM_MODEL_FAMILY = "mom20-quintile/v1"

# The null label rides the declared configuration exactly as the sealed
# lane's does (the wave scores carry no feature panel).
WAVE0_NULL_LABEL = 0.01

WAVE0_ROOT = REPO_ROOT / "artifacts" / "theory" / "wave0"
LEDGER_PATH = WAVE0_ROOT / "wave0-registration.json"
REGISTRY_PATH = WAVE0_ROOT / "wave0.db"
TRIALS_DIR = WAVE0_ROOT / "trials"
SCRATCH_ROOT = WAVE0_ROOT / "scratch"

_NULL = NULL_SCORE_MODEL_FAMILY
_DEFAULT_DELTA = Decimal("0.45")
_DEFAULT_DTE = 45
_DEFAULT_EXIT = 4
_DEFAULT_FLOW = 100


@dataclass(frozen=True)
class WaveConfig:
    """One pre-declared menu slot. ``params_key`` is the dedup identity:
    slots with equal keys alias ONE executed config (the registry refuses
    duplicates by trial_id, and re-running the same config at a fresh
    run_index would burn cap for nothing)."""

    slot_id: str
    family: str
    hypothesis: str
    arm: str
    model_family: str
    score_seed: str | None
    target_abs_delta: Decimal
    target_dte: int
    exit_sessions_after_entry: int
    flow_min_session_volume: int

    def params_key(self) -> tuple[Any, ...]:
        return (
            self.arm,
            self.model_family,
            self.score_seed,
            str(self.target_abs_delta),
            self.target_dte,
            self.exit_sessions_after_entry,
            self.flow_min_session_volume,
        )

    def strategy_config(self) -> OptionsStrategyConfig:
        return OptionsStrategyConfig(
            target_abs_delta=self.target_abs_delta,
            target_dte=self.target_dte,
            exit_sessions_after_entry=self.exit_sessions_after_entry,
        )


_NULL_HYPOTHESIS = (
    "theory wave-0 T-NULL seed {seed_label}: the vehicle's cost floor under"
    " deterministic hash-random entry — cohort_ic_mean expected ~0 (stride-4"
    " t insignificant), total_return expected negative (fee + tick drag);"
    " produces the rejection baseline every other config must beat and the"
    " D8 tripwire calibration (realized stride4_cohort_ic_sd + fidelity_rho"
    " become the priors; the synthetic 0.9/0.16 priors are FORBIDDEN on the"
    " real lane)"
)
_MOM_HYPOTHESIS = (
    "theory wave-0 T-MOM: momentum-quintile vehicle transmission — score ="
    " mom_20 in GRID WEEKS (Agenda C re-denomination; log(c_last/c_first)"
    " over exactly 21 PIT-visible grid closes, warm-up omits the first 20"
    " grid indices), quintile cut top->calls / bottom->puts; falsifiers:"
    " cohort_ic_mean inside the T-NULL x3 spread, total_return <= the null"
    " cost floor"
)
_BAND_HYPOTHESIS = (
    "theory wave-0 T-BAND {delta}: the delta-target ladder point on the null"
    " cohort — falsifiers: premium-return dispersion not increasing as"
    " target delta falls (the derived delta does not order risk), empty"
    " cohorts at the wing (no_in_band_strike)"
)
_DTE_HYPOTHESIS = (
    "theory wave-0 T-DTE {dte}: the dte-target ladder point on the null"
    " cohort (monthly-only grid quantizes the ladder — read by REALIZED dte"
    " from stamped contract_expiration rows); falsifier: monotone returns"
    " across dte surviving the T-NULL spread"
)
_FLOW_HYPOTHESIS = (
    "theory wave-0 T-FLOW {flow}: flow-screen dose-response on the null"
    " score — is session volume-flow predictive of executable premium"
    " returns or only a fill-feasibility screen? Disclose that the screen"
    " and the participation cap key on the SAME variable (bar volume)"
)
_HOLD_HYPOTHESIS = (
    "theory wave-0 T-HOLD exit {n}: the hold-structure ladder point — where"
    " the long premium decays (short-hold turnover vs theta); arm B rides"
    " to expiry settlement (the fill-free premium-decay read)"
)
_ARM_B_HYPOTHESIS = (
    "theory wave-0 arm B on {score_label}: ride-to-expiry settlement or"
    " early-exercise election — entry VWAP vs settlement intrinsic, the"
    " cleanest premium-decay measurement in the menu; boundary disclosed:"
    " expiries past the 2026-08-14 spot proxy settle clamped"
)


def wave0_menu() -> tuple[WaveConfig, ...]:
    """THE pre-declared 20-slot menu (arm-A 18 + arm-B 2). Order is the
    execution order within each wave; Agenda A rules the wave sequencing
    (NULL x3 first, then MOM/HOLD, then BAND/DTE/FLOW)."""
    slots: list[WaveConfig] = []
    for seed_label, seed in (
        ("1", "theory-null-1"),
        ("2", "theory-null-2"),
        ("3", "theory-null-3"),
    ):
        slots.append(
            WaveConfig(
                slot_id=f"null-s{seed_label}",
                family="T-NULL",
                hypothesis=_NULL_HYPOTHESIS.format(seed_label=seed_label),
                arm="A",
                model_family=_NULL,
                score_seed=seed,
                target_abs_delta=_DEFAULT_DELTA,
                target_dte=_DEFAULT_DTE,
                exit_sessions_after_entry=_DEFAULT_EXIT,
                flow_min_session_volume=_DEFAULT_FLOW,
            )
        )
    slots.append(
        WaveConfig(
            slot_id="mom20-a",
            family="T-MOM",
            hypothesis=_MOM_HYPOTHESIS,
            arm="A",
            model_family=MOM_MODEL_FAMILY,
            score_seed=None,
            target_abs_delta=_DEFAULT_DELTA,
            target_dte=_DEFAULT_DTE,
            exit_sessions_after_entry=_DEFAULT_EXIT,
            flow_min_session_volume=_DEFAULT_FLOW,
        )
    )
    for delta in (Decimal("0.35"), Decimal("0.45"), Decimal("0.55")):
        slots.append(
            WaveConfig(
                slot_id=f"band-{delta}",
                family="T-BAND",
                hypothesis=_BAND_HYPOTHESIS.format(delta=f"{delta:.2f}"),
                arm="A",
                model_family=_NULL,
                score_seed="theory-null-1",
                target_abs_delta=delta,
                target_dte=_DEFAULT_DTE,
                exit_sessions_after_entry=_DEFAULT_EXIT,
                flow_min_session_volume=_DEFAULT_FLOW,
            )
        )
    for dte in (35, 45, 55):
        slots.append(
            WaveConfig(
                slot_id=f"dte-{dte:03d}",
                family="T-DTE",
                hypothesis=_DTE_HYPOTHESIS.format(dte=dte),
                arm="A",
                model_family=_NULL,
                score_seed="theory-null-1",
                target_abs_delta=_DEFAULT_DELTA,
                target_dte=dte,
                exit_sessions_after_entry=_DEFAULT_EXIT,
                flow_min_session_volume=_DEFAULT_FLOW,
            )
        )
    for flow in (1, 10, 100, 1000):
        slots.append(
            WaveConfig(
                slot_id=f"flow-{flow:04d}",
                family="T-FLOW",
                hypothesis=_FLOW_HYPOTHESIS.format(flow=flow),
                arm="A",
                model_family=_NULL,
                score_seed="theory-null-1",
                target_abs_delta=_DEFAULT_DELTA,
                target_dte=_DEFAULT_DTE,
                exit_sessions_after_entry=_DEFAULT_EXIT,
                flow_min_session_volume=flow,
            )
        )
    for exit_n in (2, 4, 6, 8):
        slots.append(
            WaveConfig(
                slot_id=f"hold-exit{exit_n}",
                family="T-HOLD",
                hypothesis=_HOLD_HYPOTHESIS.format(n=exit_n),
                arm="A",
                model_family=_NULL,
                score_seed="theory-null-1",
                target_abs_delta=_DEFAULT_DELTA,
                target_dte=_DEFAULT_DTE,
                exit_sessions_after_entry=exit_n,
                flow_min_session_volume=_DEFAULT_FLOW,
            )
        )
    slots.append(
        WaveConfig(
            slot_id="null-s1-b",
            family="T-NULL",
            hypothesis=_ARM_B_HYPOTHESIS.format(score_label="the null score (seed 1)"),
            arm="B",
            model_family=_NULL,
            score_seed="theory-null-1",
            target_abs_delta=_DEFAULT_DELTA,
            target_dte=_DEFAULT_DTE,
            exit_sessions_after_entry=_DEFAULT_EXIT,
            flow_min_session_volume=_DEFAULT_FLOW,
        )
    )
    slots.append(
        WaveConfig(
            slot_id="mom20-b",
            family="T-MOM",
            hypothesis=_ARM_B_HYPOTHESIS.format(score_label="the T-MOM score"),
            arm="B",
            model_family=MOM_MODEL_FAMILY,
            score_seed=None,
            target_abs_delta=_DEFAULT_DELTA,
            target_dte=_DEFAULT_DTE,
            exit_sessions_after_entry=_DEFAULT_EXIT,
            flow_min_session_volume=_DEFAULT_FLOW,
        )
    )
    return tuple(slots)


def alias_map(menu: Sequence[WaveConfig]) -> dict[str, list[str]]:
    """slot_id -> the alias group sharing one executed config (the
    dedup receipt: the registry identifies configs by trial_id and
    refuses duplicates, so aliased slots execute ONCE)."""
    groups: dict[tuple[Any, ...], list[str]] = {}
    for config in menu:
        groups.setdefault(config.params_key(), []).append(config.slot_id)
    return {slot_id: sorted(group) for group in groups.values() for slot_id in group}


def wave0_split_override() -> OptionsSplitOverride:
    """The D4 geometry as the runner's override (field order: H, embargo,
    val, test, roll, min_train — every value in GRID FRIDAYS)."""
    h, embargo, val, test, roll, min_train = WAVE0_GEOMETRY
    return OptionsSplitOverride(
        label_horizon_sessions=h,
        embargo_sessions=embargo,
        val_sessions=val,
        test_sessions=test,
        roll_sessions=roll,
        min_train_sessions=min_train,
    )


def null_scored_rows(
    seed: str, sessions: Sequence[date], underlyings: Sequence[str]
) -> tuple[ScoredLabel, ...]:
    """One hash-null row per (decision session, underlying) under the
    config's seed — the runner's P1-3 seed binding re-verifies every row."""
    return tuple(
        ScoredLabel(
            security_id=underlying,
            session=session,
            score=null_score(seed=seed, session=session, security_id=underlying),
            label=WAVE0_NULL_LABEL,
        )
        for session in sessions
        for underlying in underlyings
    )


def momentum_scored_rows(
    bars: Sequence[Any],
    grid: Any,
    sessions: Sequence[date],
) -> tuple[ScoredLabel, ...]:
    """T-MOM rows: mom_20 IN GRID WEEKS from the PIT-visible dataset bars.

    Availability discipline: a bar is visible at a decision instant only
    when its stamped available_at (the T+1 publication wall) is <= the
    grid's own close for that decision session — the decision session's
    OWN bar publishes the next morning and is INVISIBLE at its close.
    mom_20 = ln(c_last / c_first) over exactly the last 21 visible bars;
    a name with fewer (the warm-up: Agenda C's 21-grid-index dead zone,
    or a capture hole) gets NO row — absent from the quintile cut, never
    imputed across the gap."""
    by_security: dict[str, dict[date, Any]] = {}
    for bar in bars:
        by_security.setdefault(bar.security_id, {})[bar.session] = bar
    # FINAL_HOLDOUT_DATES carries ISO STRINGS (the machinery's own w5 guard
    # compares session.isoformat() against it — same discipline here)
    holdout = frozenset(FINAL_HOLDOUT_DATES)
    rows: list[ScoredLabel] = []
    for session in sessions:
        if session.isoformat() in holdout:  # pragma: no cover - world excludes
            continue
        close_at = grid.session_close(session)
        for security_id in sorted(by_security):
            visible = sorted(
                bar.session
                for bar in by_security[security_id].values()
                if bar.available_at <= close_at
            )
            if len(visible) < 21:
                continue  # the warm-up / a capture hole: absent, never imputed
            first = by_security[security_id][visible[-21]].close
            last = by_security[security_id][visible[-1]].close
            if first <= 0 or last <= 0:  # pragma: no cover - loader-refused upstream
                continue
            rows.append(
                ScoredLabel(
                    security_id=security_id,
                    session=session,
                    score=float((last / first).ln()),
                    label=WAVE0_NULL_LABEL,
                )
            )
    return tuple(rows)


def build_ledger(manifest_hash: str, protocol_hash: str) -> dict[str, Any]:
    menu = wave0_menu()
    slots = [
        {
            "slot_id": config.slot_id,
            "family": config.family,
            "hypothesis": config.hypothesis,
            "arm": config.arm,
            "model_family": config.model_family,
            "score_seed": config.score_seed,
            "target_abs_delta": str(config.target_abs_delta),
            "target_dte": config.target_dte,
            "exit_sessions_after_entry": config.exit_sessions_after_entry,
            "flow_min_session_volume": config.flow_min_session_volume,
            "params_key": [str(part) for part in config.params_key()],
        }
        for config in menu
    ]
    return {
        "program": "theory-wave-0",
        "rules": {
            "sequencing": "Agenda A (owner ruling 2026-09-02): T-NULL x3 FIRST"
            " (D8 calibration); wave 1 T-MOM -> T-HOLD; wave 2 T-BAND -> T-DTE"
            " -> T-FLOW",
            "tripwire": "prior_stride4_cohort_ic_sd + prior_fidelity_rho come"
            " ONLY from the executed null artifacts (the calibration block);"
            " verdict rule 2-SE",
            "holdout": "D7: no end clamp + mandatory exit/label-window tagging"
            " (stamped by the runner) + the w5 fold-test refusal",
        },
        "geometry_grid_fridays": {
            "label_horizon": WAVE0_GEOMETRY[0],
            "embargo": WAVE0_GEOMETRY[1],
            "val": WAVE0_GEOMETRY[2],
            "test": WAVE0_GEOMETRY[3],
            "roll": WAVE0_GEOMETRY[4],
            "min_train": WAVE0_GEOMETRY[5],
        },
        "dataset_manifest_hash": manifest_hash,
        "protocol_hash": protocol_hash,
        "slots": slots,
        "alias_map": alias_map(menu),
        "unique_configs": len({tuple(s["params_key"]) for s in slots}),
        "calibration": None,
        "executions": [],
    }


def _git_head(repo: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git rev-parse HEAD failed: {result.stderr.strip()[:120]}")
    return result.stdout.strip()


def load_ledger(path: Path = LEDGER_PATH) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def verify_config_against_ledger(ledger: Mapping[str, Any], config: WaveConfig) -> None:
    """Drift refusal: an execution whose params differ from the committed
    pre-registration row refuses before anything runs."""
    rows = {row["slot_id"]: row for row in ledger["slots"]}
    row = rows.get(config.slot_id)
    if row is None:
        raise SystemExit(
            f"REFUSED: slot {config.slot_id} is not in the committed pre-registration ledger"
        )
    key = [str(part) for part in config.params_key()]
    if row["params_key"] != key:
        raise SystemExit(
            f"REFUSED: slot {config.slot_id} drifted from the committed"
            f" pre-registration ledger (ledger {row['params_key']} !="
            f" executed {key})"
        )


def require_calibration(ledger: Mapping[str, Any], config: WaveConfig) -> None:
    """Agenda A's sequencing guard: a non-null config refuses while the
    D8 calibration block is absent (the T-NULL x3 seeds must have run and
    their realized stats become the priors)."""
    if config.family == "T-NULL":
        return
    if not ledger.get("calibration"):
        raise SystemExit(
            "REFUSED: the D8 calibration block is absent — the T-NULL x3"
            " seeds must run and --calibrate must append their realized"
            " stride4_cohort_ic_sd + fidelity_rho before any non-null"
            " config executes (Agenda A sequencing)"
        )


def calibrate(artifacts_dir: Path) -> dict[str, Any]:
    """D8: read the three null artifacts' POOLED stats and build the
    recalibrated tripwire priors (2-SE rule). Synthetic priors never
    appear here — the priors are MEASURED on the real lane."""
    menu = wave0_menu()
    null_seeds = [c for c in menu if c.family == "T-NULL" and c.arm == "A"]
    realized: dict[str, dict[str, float]] = {}
    for config in null_seeds:
        # the runner writes artifacts_dir / f"{trial_id}.json" — one
        # artifact per executed slot directory
        matches = sorted((artifacts_dir / config.slot_id).glob("*.json"))
        if not matches:
            raise SystemExit(
                f"REFUSED: no stamped artifact found for {config.slot_id} —"
                " run the null seeds before calibrating"
            )
        payload = json.loads(matches[-1].read_text(encoding="utf-8"))["payload"]
        pooled = payload.get("pooled")
        if not isinstance(pooled, dict):
            pooled = {}
        # .get + the None check below: a stats-less payload is a NAMED
        # refusal, never a raw KeyError out of the calibration path
        realized[config.slot_id] = {
            "stride4_cohort_ic_sd": pooled.get("stride4_cohort_ic_sd"),
            "fidelity_rho": pooled.get("fidelity_rho"),
            "total_return": pooled.get("total_return"),
        }
    sds = [v["stride4_cohort_ic_sd"] for v in realized.values()]
    rhos = [v["fidelity_rho"] for v in realized.values()]
    if any(v is None for v in sds + rhos):
        raise SystemExit(
            "REFUSED: a null artifact carries no pooled stride4_cohort_ic_sd"
            " / fidelity_rho — the calibration cannot be built"
        )
    return {
        "rule": "2-SE against the priors below",
        "prior_stride4_cohort_ic_sd": sum(sds) / len(sds),
        "prior_fidelity_rho": sum(rhos) / len(rhos),
        "realized": realized,
    }


def _scored_for(
    config: WaveConfig,
    world: Any,
) -> tuple[ScoredLabel, ...]:
    if config.model_family == _NULL:
        assert config.score_seed is not None
        return null_scored_rows(
            config.score_seed, world.decision_sessions, tuple(world.overlay.underlyings)
        )
    if config.model_family == MOM_MODEL_FAMILY:
        return momentum_scored_rows(world.dataset.bars, world.grid, world.decision_sessions)
    raise SystemExit(f"REFUSED: unknown model family {config.model_family}")


def _held_paths() -> SealedInputPaths:
    return SealedInputPaths(
        repo=REPO_ROOT,
        lane1_manifest=REPO_ROOT / "artifacts" / "lane1" / "cboe-manifest.json",
        lane1_source=Path(
            "/home/alexk/m2-evidence/cboe-sample/"
            "UnderlyingOptionsEODCalcs_2023-08-25_no_cgi_subscription.csv"
        ),
        lane2_manifest=REPO_ROOT / "artifacts" / "bars" / "capture" / "capture_manifest.json",
        calendar_decision_artifact=REPO_ROOT / "data" / "g4" / "calendar-decision.json",
        spot_proxy_v2=REPO_ROOT / "artifacts" / "spot-proxy-v2.json",
    )


def execute(slot_ids: Sequence[str]) -> int:
    ledger = load_ledger()
    held = verify_sealed_inputs(_held_paths())
    scratch = SCRATCH_ROOT / "world"
    scratch.mkdir(parents=True, exist_ok=True)
    from tree_options.protocol.loader import load_protocol_bytes

    protocol = load_protocol_bytes(held.protocol_bytes)
    world = build_lane2_world(
        held,
        repo_root=REPO_ROOT,
        scratch=scratch,
        protocol=protocol,
        spot_v2_path=REPO_ROOT / "artifacts" / "spot-proxy-v2.json",
        staleness_sessions=G4_STALENESS_SESSIONS,
    )
    # D7 defensive assertion: the world's decision sessions never touch
    # the sealed window (the machinery's own guards also refuse it);
    # FINAL_HOLDOUT_DATES carries ISO strings, so compare in that domain
    leaked = {
        session.isoformat()
        for session in world.decision_sessions
        if session.isoformat() in frozenset(FINAL_HOLDOUT_DATES)
    }
    if leaked:
        raise SystemExit(f"REFUSED: decision sessions inside the holdout: {sorted(leaked)}")
    menu = {c.slot_id: c for c in wave0_menu()}
    registry = TrialRegistry(REGISTRY_PATH)
    manifest_hash = held.packet.lane2_manifest.typed_manifest_content_hash
    for slot_id in slot_ids:
        config = menu.get(slot_id)
        if config is None:
            raise SystemExit(f"REFUSED: unknown slot {slot_id}")
        verify_config_against_ledger(ledger, config)
        require_calibration(ledger, config)
        run_dir = TRIALS_DIR / config.slot_id
        if run_dir.exists():
            raise SystemExit(
                f"REFUSED: {run_dir} already exists — wave executions are one-shot per slot"
            )
        run_dir.mkdir(parents=True)
        result = run_options_trial(
            dataset=world.dataset,
            surface=world.surface,
            calendar=world.grid,
            execution_calendar=world.overlay.calendar,
            protocol=protocol,
            world_id=world.world_id,
            arm=config.arm,
            strategy_config=config.strategy_config(),
            scored=_scored_for(config, world),
            model_family=config.model_family,
            model_sha256=None,
            hypothesis=config.hypothesis,
            decision_sessions=world.decision_sessions,
            options_manifest_hash=manifest_hash,
            registry=registry,
            artifacts_dir=run_dir,
            repo=REPO_ROOT,
            clock=G4_FIXED_CLOCK,
            split_override=wave0_split_override(),
            liquidity_lane=2,
            flow_min_session_volume=config.flow_min_session_volume,
            score_seed=config.score_seed,
        )
        print(
            f"{config.slot_id}: {result.trial_id} ->"
            f" folds={result.n_folds} positions={result.n_positions}",
            flush=True,
        )
        executions = list(ledger["executions"])
        executions.append(
            {
                "slot_id": config.slot_id,
                "trial_id": result.trial_id,
                "artifact_path": str(result.artifact_path.relative_to(REPO_ROOT)),
                "n_folds": result.n_folds,
                "n_positions": result.n_positions,
                "at_head": _git_head(REPO_ROOT),
            }
        )
        ledger["executions"] = executions
        LEDGER_PATH.write_text(
            json.dumps(ledger, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--register-only",
        action="store_true",
        help="write the pre-registration ledger (the committed menu +"
        " hashes) and exit — run BEFORE any execution",
    )
    parser.add_argument(
        "--calibrate",
        action="store_true",
        help="D8: append the tripwire calibration block from the three"
        " executed null artifacts (run after T-NULL x3, before anything"
        " else)",
    )
    parser.add_argument(
        "--execute",
        nargs="+",
        default=[],
        metavar="SLOT",
        help="execute the named pre-registered slots (one-shot per slot)",
    )
    args = parser.parse_args(argv)

    if args.register_only:
        if LEDGER_PATH.exists():
            raise SystemExit(f"REFUSED: {LEDGER_PATH} already exists — one-shot")
        held = verify_sealed_inputs(_held_paths())
        ledger = build_ledger(
            manifest_hash=held.packet.lane2_manifest.typed_manifest_content_hash,
            protocol_hash=held.packet.protocol_hash,
        )
        WAVE0_ROOT.mkdir(parents=True, exist_ok=True)
        LEDGER_PATH.write_text(
            json.dumps(ledger, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(
            f"pre-registration ledger written: {LEDGER_PATH}"
            f" ({len(ledger['slots'])} slots,"
            f" {ledger['unique_configs']} unique configs)"
        )
        return 0

    if args.calibrate:
        ledger = load_ledger()
        if ledger.get("calibration"):
            raise SystemExit("REFUSED: the calibration block already exists — one-shot")
        ledger["calibration"] = calibrate(TRIALS_DIR)
        LEDGER_PATH.write_text(
            json.dumps(ledger, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(
            "calibration appended: prior sd"
            f" {ledger['calibration']['prior_stride4_cohort_ic_sd']:.6g},"
            " prior rho"
            f" {ledger['calibration']['prior_fidelity_rho']:.6g}"
        )
        return 0

    if args.execute:
        return execute(args.execute)

    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
