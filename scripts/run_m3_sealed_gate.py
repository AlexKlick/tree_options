#!/usr/bin/env python3
"""Run the sealed M3 options validation gate exactly once.

Pre-declared in docs/m3-options-plan.md §4 with TWO owner-ruled amendments
recorded BEFORE this run (docs/m3-od1-tripwire-decision.md, 2026-08-20):
worlds 710/711 are built at coefficient 0.50 (the OD1-re-priced transfer
stratum), and criterion 3 is the amended rejection-path form.

8 registered trials over validation worlds {701, 702, 710, 711} x arms
{A, B}, evaluated ONLY from the stamped payload files:

  1 conservation_every_session : every trial COMPLETED and its stamped
      conservation_checks equal the number of evaluated sessions
  2 fill_discipline            : every stamped fill executes strictly
      after its decision session, against a quote received by execution
  3 rejection_paths_live       : zero-bid/no-liquidity fill rejections
      (ZeroSize + Nonpositive + NO_VISIBLE_QUOTE) >= 100 PER WORLD,
      pooled across the world's arms (the owner-ruled amendment's "per
      world"; review r2 P1-4 corrected the per-trial application) AND
      the same_day_volume FAIL tail >= 5% of rule evaluations
  4 machinery_terminal_states  : arm B - every position closed (sell /
      early_exercise / expiry / terminal) or its contract expires after
      the world's last session
  5 vehicle_fidelity_nulls     : mean fidelity_rho over 701+702 (arm A)
      >= 0.696 (= OD1-measured 0.846 - 0.15, pre-declared)
  6 fp_nulls                   : |stride-4 cohort t| <= 2.5 on EACH null
      world (arm A)
  7 power_transfer             : pooled stride-4 cohort t over 710+711
      (arm A, re-derived from the stamped series) >= 1.96 - the single
      detection criterion; per-world ts reported

The verdict - PASS or FAIL - is recorded verbatim whatever it is; no
re-run inside this campaign regardless of outcome.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import statistics
import sys
from datetime import UTC, datetime
from pathlib import Path

for _name in (
    "OPENBLAS_NUM_THREADS",
    "OMP_NUM_THREADS",
    "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
):
    os.environ[_name] = "1"

REPO_ROOT = Path(__file__).resolve().parents[1]
EQUITY_REGISTRY = REPO_ROOT / "data" / "worlds" / "registry.json"
OPTIONS_REGISTRY = REPO_ROOT / "data" / "worlds" / "options_registry.json"
DEFAULT_TRIAL_REGISTRY = REPO_ROOT / "artifacts" / "m3-options-sealed.db"
DEFAULT_ARTIFACTS_DIR = REPO_ROOT / "artifacts" / "m3-options-sealed"

NULLS = ("synth-v1-val-null-701", "synth-v1-val-null-702")
POWERS = ("synth-v1-val-alpha-710", "synth-v1-val-alpha-711")
SEALED_WORLDS = (*NULLS, *POWERS)
ARMS = ("A", "B")
MODEL_FAMILY = "ridge:v1"
FEATURE_NAMES = ("mom_1", "mom_5", "mom_20", "dol_vol_20")
RIDGE_LAMBDA = 1.0
HORIZON_SESSIONS = 5

# pre-declared thresholds (plan §4 + the two recorded amendments)
FIDELITY_FLOOR = 0.696  # OD1-measured rho 0.846038 - 0.15
CRITICAL_T = 1.96
FP_T_BOUND = 2.5
REJECTION_FLOOR = 100
VOLUME_TAIL_FRACTION = 0.05
ZERO_BID_CODES = ("ZeroSizeQuoteError", "NonpositiveQuoteError", "NO_VISIBLE_QUOTE")

# a FIXED clock: stamped artifacts are byte-deterministic, which is what the
# clean-clone ARTIFACTS_IDENTICAL=1 proof relies on
FIXED_CLOCK = datetime(2026, 8, 20, 12, 0, 0, tzinfo=UTC)

SEALED_CONFIGS: tuple[tuple[str, str], ...] = tuple(
    (world, arm) for world in SEALED_WORLDS for arm in ARMS
)


def _equity_generator_code_sha() -> str:
    digest = hashlib.sha256()
    for path in sorted((REPO_ROOT / "src" / "tree_options" / "synth").glob("*.py")):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _synth_options_code_sha() -> str:
    digest = hashlib.sha256()
    for path in sorted((REPO_ROOT / "src" / "tree_options" / "synth_options").glob("*.py")):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, default=DEFAULT_TRIAL_REGISTRY)
    parser.add_argument("--artifacts-dir", type=Path, default=DEFAULT_ARTIFACTS_DIR)
    return parser


def _measure_overlay(overlay) -> dict:  # type: ignore[no-untyped-def]
    """The same sample-plus-counts pin verify_options_worlds.py checks."""
    entries, quotes = overlay.entry_and_quote_counts()
    slices = [
        [
            sid,
            session.isoformat(),
            hashlib.sha256(overlay.canonical_file_bytes(sid, session)).hexdigest(),
        ]
        for sid, session in overlay.anchor_slices()
    ]
    return {
        "contract_count": overlay.contract_count(),
        "entry_count": entries,
        "quote_event_count": quotes,
        "sample_slice_hashes": slices,
    }


def zero_bid_floor_failures(zero_bid_by_trial: dict[tuple[str, str], int]) -> list[str]:
    """Criterion 3's floor clause is PER WORLD, pooled across the world's
    arms (owner-ruled amendment, docs/m3-od1-tripwire-decision.md: "per
    world"). Review r2 P1-4: both drivers applied it per arm — a world at
    60 arm-A + 60 arm-B qualifying rejections passes the ruling (120
    pooled) but failed twice per-arm."""
    pooled: dict[str, int] = {}
    for (world_id, _arm), count in zero_bid_by_trial.items():
        pooled[world_id] = pooled.get(world_id, 0) + count
    return [
        f"{world_id}: pooled zero-bid rejections {count} < {REJECTION_FLOOR}"
        for world_id, count in sorted(pooled.items())
        if count < REJECTION_FLOOR
    ]


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.registry.exists():
        raise RuntimeError(f"refusing to reuse sealed registry: {args.registry}")
    if args.artifacts_dir.exists():
        raise RuntimeError(f"refusing to reuse sealed artifacts: {args.artifacts_dir}")

    from tree_options.data.authority import PointInTimeDataset
    from tree_options.data.ingest import ingest_snapshot
    from tree_options.data.options_manifest import (
        build_options_manifest,
        paired_dataset_hash,
    )
    from tree_options.data.options_pit import OptionPitSurface
    from tree_options.data.quality import verify_manifest
    from tree_options.labels import build_labels
    from tree_options.models import FitRow, ObsRow, RidgePipeline
    from tree_options.models.determinism import blas_pinned
    from tree_options.options import OptionsStrategyConfig
    from tree_options.protocol.loader import load_protocol
    from tree_options.protocol.stamping import build_stamp, write_artifact
    from tree_options.registry.sqlite import TrialRegistry
    from tree_options.synth import WorldSpec, generate_world
    from tree_options.synth_options import OptionsOverlaySpec, generate_overlay
    from tree_options.time.calendar import StaticSessionCalendar
    from tree_options.trials import run_options_trial

    if not blas_pinned():
        raise RuntimeError("BLAS thread pins were not applied before trial imports")

    equity_registry = json.loads(EQUITY_REGISTRY.read_text(encoding="utf-8"))
    options_registry = json.loads(OPTIONS_REGISTRY.read_text(encoding="utf-8"))
    if equity_registry["generator_code_sha"] != _equity_generator_code_sha():
        raise RuntimeError("equity generator code no longer matches the frozen registry")
    if options_registry["synth_options_code_sha"] != _synth_options_code_sha():
        raise RuntimeError("synth_options code no longer matches the frozen overlay registry")
    equity_by_id = {entry["world_id"]: entry for entry in equity_registry["worlds"]}
    overlay_by_parent = {entry["world_id"]: entry for entry in options_registry["overlays"]}

    calendar = StaticSessionCalendar(
        REPO_ROOT / "data" / "calendar" / "nyse_sessions_2018_01_02_2026_12_31.json",
        REPO_ROOT / "data" / "calendar" / "nyse_sessions_2018_01_02_2026_12_31.sha256",
    )
    protocol = load_protocol(REPO_ROOT / "research_protocol.yaml")

    worlds: dict[str, dict] = {}
    for world_id in SEALED_WORLDS:
        entry = equity_by_id[world_id]
        if entry["pool"] != "validation":
            raise RuntimeError(f"refusing non-validation world {world_id}")
        spec_dict = entry["spec"]
        if world_id in POWERS and spec_dict["alpha"]["coefficient"] != 0.5:
            raise RuntimeError(f"refusing non-ruled transfer world {world_id}")
        if world_id in NULLS and spec_dict["kind"] != "null":
            raise RuntimeError(f"refusing non-null FP world {world_id}")
        spec = WorldSpec(**spec_dict)
        generated = generate_world(spec, calendar)
        snapshot = ingest_snapshot(
            generated.payload,
            generated.master,
            snapshot_id=world_id,
            normalization_code_sha=equity_registry["generator_code_sha"],
        )
        verify_manifest(snapshot, calendar)
        observed = {
            "content_sha256": snapshot.manifest.content_sha256,
            "bar_count": snapshot.manifest.bar_count,
            "action_count": snapshot.manifest.action_count,
            "security_count": snapshot.manifest.security_count,
        }
        if observed != entry["expected"]:
            raise RuntimeError(f"registered world mismatch for {world_id}: {observed}")
        dataset = PointInTimeDataset(snapshot, calendar, universe_id=f"{world_id}|pit-universe-v1")
        overlay_entry = overlay_by_parent[world_id]
        overlay = generate_overlay(
            spec=OptionsOverlaySpec(**overlay_entry["spec"]),
            bars=snapshot.bars,
            master=snapshot.master,
            actions=snapshot.actions,
            calendar=calendar,
        )
        manifest = build_options_manifest(
            overlay,
            parent_content_sha256=snapshot.manifest.content_sha256,
            synth_options_code_sha=options_registry["synth_options_code_sha"],
        )
        measured = _measure_overlay(overlay)
        if measured != overlay_entry["expected"]:
            raise RuntimeError(f"registered overlay mismatch for {world_id}")
        worlds[world_id] = {
            "dataset": dataset,
            "surface": OptionPitSurface(overlay),
            "manifest_hash": paired_dataset_hash(
                snapshot.manifest.content_sha256, manifest.content_sha256
            ),
        }
        print(
            f"SEALED_WORLD_OK={world_id} BARS={observed['bar_count']} "
            f"CONTRACTS={measured['contract_count']} "
            f"PAIRED={worlds[world_id]['manifest_hash'][:12]}"
        )

    def scored_rows(world_id: str) -> tuple:  # type: ignore[no-untyped-def]
        """The pre-declared H5 ridge scored cross-section, walk-forward (the
        same signal lane the dev trials OD1/OD2 ran - frozen before the gate)."""
        from tree_options.evaluation.stats import ScoredLabel
        from tree_options.splitting.splitter import WalkForwardSplitter

        dataset = worlds[world_id]["dataset"]
        decision_sessions = tuple(sorted({bar.session for bar in dataset.bars}))
        labels = build_labels(
            dataset,
            calendar,
            horizon_sessions=HORIZON_SESSIONS,
            decision_sessions=decision_sessions,
        )
        label_values = {(lab.security_id, lab.decision_session): lab.value for lab in labels}
        rows_by_session: dict = {}
        wanted = set(FEATURE_NAMES)
        for session in decision_sessions:
            decision_at = calendar.session_close(session)
            panel = dataset.features_as_of(
                decision_at=decision_at,
                universe_id=dataset.universe_id,
                dataset_snapshot_id=dataset.snapshot_id,
            )
            rows: list[FitRow] = []
            for row in panel:
                label = label_values.get((row.security_id, session))
                if label is None:
                    continue
                values = {f.feature_name: f.value for f in row.features}
                if not wanted <= values.keys():
                    continue
                rows.append(
                    FitRow(
                        session=session,
                        security_id=row.security_id,
                        features={name: values[name] for name in FEATURE_NAMES},
                        label=label,
                    )
                )
            rows_by_session[session] = rows
        f = protocol.folds
        splitter = WalkForwardSplitter(
            calendar,
            label_horizon_sessions=f.label_horizon_sessions,
            embargo_sessions=f.embargo_sessions,
            val_sessions=f.validation_window_sessions.default,
            test_sessions=f.test_window_sessions.default,
            roll_sessions=f.roll_forward_sessions,
            min_train_sessions=f.min_train_sessions,
        )
        world_sessions = frozenset(decision_sessions)
        folds = [
            fold
            for fold in splitter.splits(decision_sessions)
            if fold.test_sessions <= world_sessions
        ]
        scored: list[ScoredLabel] = []
        for fold in folds:
            fit_rows = [
                row
                for s in sorted(fold.final_fit_train_sessions)
                for row in rows_by_session.get(s, ())
            ]
            fit_sessions = frozenset(
                s for s in fold.final_fit_train_sessions if rows_by_session.get(s)
            )
            pipe = RidgePipeline(
                name=f"m3-sealed-{world_id}/fold-{fold.fold_id:03d}",
                feature_names=FEATURE_NAMES,
                ridge_lambda=RIDGE_LAMBDA,
            )
            pipe.fit(fit_rows, fit_sessions=fit_sessions)
            test_rows = [
                row for s in sorted(fold.test_sessions) for row in rows_by_session.get(s, ())
            ]
            obs = [
                ObsRow(session=row.session, security_id=row.security_id, features=row.features)
                for row in test_rows
            ]
            for row in pipe.score(obs, target_sessions=frozenset(fold.test_sessions)):
                scored.append(
                    ScoredLabel(
                        security_id=row.security_id,
                        session=row.session,
                        score=row.score,
                        label=label_values[(row.security_id, row.session)],
                    )
                )
        return tuple(scored)

    args.registry.parent.mkdir(parents=True, exist_ok=True)
    registry = TrialRegistry(args.registry)
    strategy = OptionsStrategyConfig()
    statuses: dict[tuple[str, str], str] = {}
    try:
        for world_id, arm in sorted(SEALED_CONFIGS):
            scored = scored_rows(world_id)
            result = run_options_trial(
                dataset=worlds[world_id]["dataset"],
                surface=worlds[world_id]["surface"],
                calendar=calendar,
                protocol=protocol,
                world_id=world_id,
                arm=arm,
                strategy_config=strategy,
                scored=scored,
                model_family=MODEL_FAMILY,
                model_sha256=None,  # per-fold models; family + features pinned in config
                hypothesis=(
                    "sealed options gate (pre-declared plan §4 + amendments "
                    f"2026-08-20): arm {arm} on {world_id}"
                ),
                decision_sessions=tuple(sorted({row.session for row in scored})),
                options_manifest_hash=worlds[world_id]["manifest_hash"],
                registry=registry,
                artifacts_dir=args.artifacts_dir,
                repo=REPO_ROOT,
                clock=lambda: FIXED_CLOCK,
            )
            statuses[(world_id, arm)] = registry.status(result.trial_id)
            body = json.loads(result.artifact_path.read_text(encoding="utf-8"))
            pooled = body["payload"]["pooled"]
            print(
                f"SEALED_TRIAL={world_id} ARM={arm} STATUS={statuses[(world_id, arm)]} "
                f"FOLDS={result.n_folds} POSITIONS={result.n_positions} "
                f"RHO={pooled['fidelity_rho']} T={pooled['stride4_cohort_t']}"
            )
    finally:
        registry.close()

    # ---- verdict: from the stamped payload files only ----------------------
    payloads: dict[tuple[str, str], dict] = {}
    for world_id, arm in SEALED_CONFIGS:
        trial_id = f"m3-{world_id}-{arm.lower()}-r1"
        body = json.loads((args.artifacts_dir / f"{trial_id}.json").read_text(encoding="utf-8"))
        payloads[(world_id, arm)] = body["payload"]

    def _t_from_series(series: list[float]) -> float | None:
        n = len(series)
        if n < 3:
            return None
        sd = statistics.stdev(series)
        if sd <= 0:
            return None
        return (sum(series) / n) * (n**0.5) / sd

    failures: list[str] = []
    zero_bid_by_trial: dict[tuple[str, str], int] = {}
    reported: dict[str, object] = {"per_trial": {}, "power": {}, "fidelity": {}}

    for world_id, arm in SEALED_CONFIGS:
        payload = payloads[(world_id, arm)]
        counters = payload["counters"]
        per_fold = payload["per_fold"]
        fills = payload["fills_log"]
        key = f"{world_id}|{arm}"

        # 1. conservation every session
        sessions_evaluated = sum(f["n_sessions_evaluated"] for f in per_fold)
        if statuses[(world_id, arm)] != "COMPLETED":
            failures.append(f"{key}: trial status {statuses[(world_id, arm)]}")
        if sessions_evaluated <= 0 or counters["conservation_checks"] != sessions_evaluated:
            failures.append(
                f"{key}: conservation {counters['conservation_checks']} != "
                f"{sessions_evaluated} evaluated sessions"
            )

        # 2. fill discipline (T+1 execution, quote received by execution)
        if not fills:
            failures.append(f"{key}: no stamped fills")
        for fill in fills:
            if not fill["decision_session"] < fill["execution_session"]:
                failures.append(f"{key}: same-session fill {fill['fill_id']}")
                break
        for fill in fills:
            if fill["quote_received_at"] > fill["execution_at"]:
                failures.append(f"{key}: quote received after execution {fill['fill_id']}")
                break

        # 3. rejection paths live (amended form)
        rejections = counters["rejections"]
        zero_bid = sum(
            rejections.get(bucket, {}).get(code, 0)
            for bucket in (
                "entry_fill_rejections",
                "exit_fill_rejections",
                "force_close_rejections",
            )
            for code in ZERO_BID_CODES
        )
        hist = counters["rule_histogram"]
        rule_evals = sum(sum(statuses_.values()) for statuses_ in hist.values())
        volume_fails = hist.get("same_day_volume", {}).get("FAIL", 0)
        # the floor clause is evaluated PER WORLD after the loop (r2 P1-4);
        # the volume-tail clause stays per trial — the stricter form
        zero_bid_by_trial[(world_id, arm)] = zero_bid
        if rule_evals == 0 or volume_fails / rule_evals < VOLUME_TAIL_FRACTION:
            failures.append(
                f"{key}: volume tail {volume_fails}/{rule_evals} below {VOLUME_TAIL_FRACTION:.0%}"
            )

        # 4. machinery terminal states (arm B)
        if arm == "B":
            last_session = payload["world_last_session"]
            for position in payload["pooled"]["positions"]:
                if (
                    position["exit_kind"] is None
                    and position["contract_expiration"] <= last_session
                ):
                    failures.append(
                        f"{key}: position {position['underlying_security_id']} open past "
                        f"expiration {position['contract_expiration']}"
                    )
                    break

        reported["per_trial"][key] = {  # type: ignore[index]
            "status": statuses[(world_id, arm)],
            "sessions_evaluated": sessions_evaluated,
            "conservation_checks": counters["conservation_checks"],
            "zero_bid_rejections": zero_bid,
            "volume_fail_fraction": (volume_fails / rule_evals) if rule_evals else None,
            "n_positions": payload["pooled"]["n_positions"],
            "fidelity_rho": payload["pooled"]["fidelity_rho"],
            "stride4_cohort_t": payload["pooled"]["stride4_cohort_t"],
            "counters": {
                k: v for k, v in counters.items() if k not in ("rule_histogram", "rejections")
            },
        }

    # 3b. rejection floor, PER WORLD pooled across arms (r2 P1-4)
    failures.extend(zero_bid_floor_failures(zero_bid_by_trial))

    # 5. vehicle fidelity on the nulls (arm A - the calibrated vehicle)
    null_rhos = [payloads[(w, "A")]["pooled"]["fidelity_rho"] for w in NULLS]
    if any(rho is None for rho in null_rhos):
        failures.append(f"fidelity rho unmeasurable on a null world: {null_rhos}")
    else:
        mean_rho = sum(null_rhos) / len(null_rhos)  # type: ignore[arg-type]
        reported["fidelity"] = {"null_rhos_arm_a": null_rhos, "mean_rho": mean_rho}  # type: ignore[index]
        if mean_rho < FIDELITY_FLOOR:
            failures.append(f"vehicle fidelity {mean_rho:.4f} < floor {FIDELITY_FLOOR}")
    reported["fidelity"]["null_rhos_arm_b"] = [  # type: ignore[index]
        payloads[(w, "B")]["pooled"]["fidelity_rho"] for w in NULLS
    ]

    # 6. FP bound on each null world (arm A)
    for world_id in NULLS:
        t = _t_from_series(payloads[(world_id, "A")]["pooled"]["stride4_cohort_ics"])
        if t is None:
            failures.append(f"stride-4 t unmeasurable on {world_id}")
            continue
        reported["power"][world_id] = t  # type: ignore[index]
        if abs(t) > FP_T_BOUND:
            failures.append(f"FP null {world_id}: |t|={t:.3f} > {FP_T_BOUND}")

    # 7. power transfer, pooled over 710+711 (arm A) - re-derived from stamps
    pooled_ics: list[float] = []
    per_world_ts = {}
    for world_id in POWERS:
        series = payloads[(world_id, "A")]["pooled"]["stride4_cohort_ics"]
        per_world_ts[world_id] = _t_from_series(series)
        pooled_ics.extend(series)
    pooled_t = _t_from_series(pooled_ics)
    reported["power"]["pooled_t"] = pooled_t  # type: ignore[index]
    reported["power"]["per_world_t"] = per_world_ts  # type: ignore[index]
    reported["power"]["n_pooled_cohorts"] = len(pooled_ics)  # type: ignore[index]
    if pooled_t is None or pooled_t < CRITICAL_T:
        failures.append(f"power transfer pooled t={pooled_t} < {CRITICAL_T}")

    verdict = "PASS" if not failures else "FAIL"
    summary = {
        "gate": "m3-options-sealed/1",
        "ran_at": FIXED_CLOCK.isoformat(),
        "trials": len(SEALED_CONFIGS),
        "thresholds": {
            "fidelity_floor": FIDELITY_FLOOR,
            "critical_t": CRITICAL_T,
            "fp_t_bound": FP_T_BOUND,
            "rejection_floor": REJECTION_FLOOR,
            "volume_tail_fraction": VOLUME_TAIL_FRACTION,
            "amendments": "docs/m3-od1-tripwire-decision.md (owner-ruled 2026-08-20)",
        },
        "failures": failures,
        "reported": reported,
        "verdict": verdict,
    }
    stamp = build_stamp(
        protocol,
        trial_id="m3-options-sealed-summary",
        config={"gate": "m3-options-sealed/1", "trials": len(SEALED_CONFIGS)},
        dataset_manifest_hash=hashlib.sha256(
            EQUITY_REGISTRY.read_bytes() + OPTIONS_REGISTRY.read_bytes()
        ).hexdigest(),
        repo=REPO_ROOT,
    )
    write_artifact(args.artifacts_dir / "sealed-gate-summary.json", summary, stamp)
    print(f"SEALED_GATE_VERDICT={verdict}")
    for failure in failures:
        print(f"SEALED_CHECK FAIL {failure}")
    if verdict == "PASS":
        print("SEALED_CHECK PASS all 7 criteria")
    return 0 if verdict == "PASS" else 4


if __name__ == "__main__":
    sys.exit(main())
