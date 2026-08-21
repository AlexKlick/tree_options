#!/usr/bin/env python3
"""Run the frozen M3 options development trials exactly once (OD1-OD3).

Selects ONLY development worlds 103/104 and their pinned options overlays;
never loads or mentions validation worlds. Refuses to reuse a trial
registry or artifact directory. The OD1 tripwire (plan §7): the measured
vehicle-fidelity factor and per-cohort IC sd must land within 2 SE of the
pre-registered priors (rho ~ 0.9, sigma_IC ~ 0.16, lag-1 cohort
autocorrelation ~ 0); a violation HALTS the campaign for root-cause — it
is never silently adjusted, and it is not a reason to touch the sealed
validation gate.

Pre-declared dev signal model (recorded in every config hash): the M2
H5 ridge (mom_1, mom_5, mom_20, dol_vol_20; lambda 1.0) fit walk-forward
on the same protocol folds — arm A is the transfer arm, so the selection
model mirrors the M2 H5 lane.

Exit codes: 0 = all dev trials completed and the tripwire holds;
3 = tripwire violated (HALT); anything else = execution failure.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
from datetime import UTC, date, datetime
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
WORLD_REGISTRY = REPO_ROOT / "data" / "worlds" / "registry.json"
OPTIONS_REGISTRY = REPO_ROOT / "data" / "worlds" / "options_registry.json"
DEFAULT_TRIAL_REGISTRY = REPO_ROOT / "artifacts" / "m3-options-dev-trials.db"
DEFAULT_ARTIFACTS_DIR = REPO_ROOT / "artifacts" / "m3-options-dev"

WORLD_103 = "synth-v1-dev-null-103"
WORLD_104 = "synth-v1-dev-alpha-104"
MODEL_FAMILY = "ridge:v1"
FEATURE_NAMES = ("mom_1", "mom_5", "mom_20", "dol_vol_20")
RIDGE_LAMBDA = 1.0
HORIZON_SESSIONS = 5

# §7 pre-registered priors + the 2-SE tripwire
PRIOR_FIDELITY_RHO = 0.9
PRIOR_SIGMA_IC = 0.16
PRIOR_AUTOCORR = 0.0
TRIPWIRE_SE_MULTIPLE = 2.0


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


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.registry.exists():
        raise RuntimeError(f"refusing to reuse development registry: {args.registry}")
    if args.artifacts_dir.exists():
        raise RuntimeError(f"refusing to reuse development artifacts: {args.artifacts_dir}")

    from tree_options.data.authority import PointInTimeDataset
    from tree_options.data.ingest import ingest_snapshot
    from tree_options.data.options_manifest import (
        build_options_manifest,
        paired_dataset_hash,
    )
    from tree_options.data.options_pit import OptionPitSurface
    from tree_options.labels import build_labels
    from tree_options.models import FitRow, ObsRow, RidgePipeline
    from tree_options.models.determinism import blas_pinned
    from tree_options.options import OptionsStrategyConfig
    from tree_options.protocol.loader import load_protocol
    from tree_options.registry.sqlite import TrialRegistry
    from tree_options.synth import WorldSpec, generate_world
    from tree_options.synth_options import generate_overlay
    from tree_options.time.calendar import StaticSessionCalendar
    from tree_options.trials import run_options_trial

    if not blas_pinned():
        raise RuntimeError("BLAS thread pins were not applied before trial imports")

    equity_registry = json.loads(WORLD_REGISTRY.read_text(encoding="utf-8"))
    options_registry = json.loads(OPTIONS_REGISTRY.read_text(encoding="utf-8"))
    code_sha = _synth_options_code_sha()
    if options_registry["synth_options_code_sha"] != code_sha:
        raise RuntimeError("synth_options code no longer matches the frozen overlay registry")
    equity_by_id = {entry["world_id"]: entry for entry in equity_registry["worlds"]}
    overlay_by_parent = {entry["world_id"]: entry for entry in options_registry["overlays"]}

    calendar = StaticSessionCalendar(
        REPO_ROOT / "data" / "calendar" / "nyse_sessions_2018_01_02_2026_12_31.json",
        REPO_ROOT / "data" / "calendar" / "nyse_sessions_2018_01_02_2026_12_31.sha256",
    )
    protocol = load_protocol(REPO_ROOT / "research_protocol.yaml")
    clock = lambda: datetime.now(UTC)  # noqa: E731 - dev run; the sealed gate injects a fixed clock

    worlds: dict[str, dict] = {}
    for world_id in (WORLD_103, WORLD_104):
        entry = equity_by_id[world_id]
        if entry["pool"] != "dev":
            raise RuntimeError(f"refusing non-development world {world_id}")
        overlay_entry = overlay_by_parent[world_id]
        spec = WorldSpec(**entry["spec"])
        generated = generate_world(spec, calendar)
        snapshot = ingest_snapshot(
            generated.payload,
            generated.master,
            snapshot_id=world_id,
            normalization_code_sha=equity_registry["generator_code_sha"],
        )
        observed = {
            "content_sha256": snapshot.manifest.content_sha256,
            "bar_count": snapshot.manifest.bar_count,
            "action_count": snapshot.manifest.action_count,
            "security_count": snapshot.manifest.security_count,
        }
        if observed != entry["expected"]:
            raise RuntimeError(
                f"registered world mismatch for {world_id}: {observed} != {entry['expected']}"
            )
        dataset = PointInTimeDataset(snapshot, calendar, universe_id=f"{world_id}|pit-universe-v1")
        from tree_options.synth_options import OptionsOverlaySpec

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
            synth_options_code_sha=code_sha,
        )
        if manifest.contract_count != overlay_entry["expected"]["contract_count"]:
            raise RuntimeError(
                f"overlay contract-count mismatch for {world_id}: "
                f"{manifest.contract_count} != {overlay_entry['expected']['contract_count']}"
            )
        worlds[world_id] = {
            "dataset": dataset,
            "surface": OptionPitSurface(overlay),
            "manifest_hash": paired_dataset_hash(
                snapshot.manifest.content_sha256, manifest.content_sha256
            ),
        }
        print(
            f"DEV_WORLD_OK={world_id} BARS={snapshot.manifest.bar_count} "
            f"CONTRACTS={manifest.contract_count} PAIRED={worlds[world_id]['manifest_hash'][:12]}"
        )

    def scored_rows(world_id: str) -> tuple:
        """The pre-declared H5 ridge scored cross-section, walk-forward."""
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
        rows_by_session: dict[date, list[FitRow]] = {}
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
        from tree_options.evaluation.stats import ScoredLabel

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
                name=f"m3-dev-{world_id}/fold-{fold.fold_id:03d}",
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
    artifacts = {}
    try:
        trials = (
            (
                "OD1",
                WORLD_104,
                "A",
                "vehicle fidelity + cohort IC calibration on the alpha dev world",
            ),
            (
                "OD2",
                WORLD_103,
                "B",
                "machinery oracles: settlements, conservation, rejection floors on the null dev world",
            ),
        )
        for config_id, world_id, arm, hypothesis in trials:
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
                model_sha256=None,  # per-fold models; the family + features are pinned in config
                hypothesis=f"{config_id}: {hypothesis}",
                decision_sessions=tuple(sorted({row.session for row in scored})),
                options_manifest_hash=worlds[world_id]["manifest_hash"],
                registry=registry,
                artifacts_dir=args.artifacts_dir,
                repo=REPO_ROOT,
                clock=clock,
            )
            body = json.loads(result.artifact_path.read_text(encoding="utf-8"))
            pooled = body["payload"]["pooled"]
            artifacts[config_id] = (result, pooled)
            print(
                f"DEV_TRIAL={config_id} STATUS={registry.status(result.trial_id)} "
                f"TRIAL_ID={result.trial_id} FOLDS={result.n_folds} "
                f"POSITIONS={result.n_positions} RHO={pooled['fidelity_rho']} "
                f"SD_IC={pooled['stride4_cohort_ic_sd']} T={pooled['stride4_cohort_t']}"
            )
    finally:
        registry.close()

    # ---- OD3: eligible-set + rule-weight audit (103/104, from stamped payloads) ----
    for config_id in ("OD1", "OD2"):
        _result, pooled = artifacts[config_id]
        counters = json.loads(_result.artifact_path.read_text(encoding="utf-8"))["payload"][
            "counters"
        ]
        hist = counters["rule_histogram"]
        total_evals = sum(sum(statuses.values()) for statuses in hist.values())
        print(
            f"OD3_AUDIT={config_id} TOTAL_RULE_EVALS={total_evals} "
            f"NOT_EVALUABLE_CANDIDATES={counters['not_evaluable_candidates']} "
            f"FAILED_CANDIDATES={counters['failed_candidates']} "
            f"NO_IN_BAND_EXPIRY={counters['no_in_band_expiry']} "
            f"NO_IN_BAND_STRIKE={counters['no_in_band_strike']} "
            f"REJECTIONS={counters['rejections']}"
        )

    # ---- the OD1 tripwire (§7) ------------------------------------------------
    _, od1 = artifacts["OD1"]
    rho, n_fidelity = od1["fidelity_rho"], od1["fidelity_n"]
    sd_ic, n_stride4 = od1["stride4_cohort_ic_sd"], od1["n_stride4_cohorts"]
    autocorr, n_cohorts = od1["cohort_ic_autocorr_lag1"], od1["n_cohorts"]

    violations: list[str] = []
    if rho is not None and n_fidelity > 3:
        z_prior = math.atanh(PRIOR_FIDELITY_RHO)
        z_measured = math.atanh(max(min(rho, 0.999999), -0.999999))
        se = 1.0 / math.sqrt(n_fidelity - 3)
        if abs(z_measured - z_prior) > TRIPWIRE_SE_MULTIPLE * se:
            violations.append(
                f"fidelity rho={rho:.4f} outside 2SE of prior {PRIOR_FIDELITY_RHO} "
                f"(n={n_fidelity}, SE_z={se:.5f})"
            )
    else:
        violations.append(f"fidelity rho unmeasurable (n={n_fidelity})")
    if sd_ic is not None and n_stride4 > 2:
        se_sd = PRIOR_SIGMA_IC / math.sqrt(2 * (n_stride4 - 1))
        if abs(sd_ic - PRIOR_SIGMA_IC) > TRIPWIRE_SE_MULTIPLE * se_sd:
            violations.append(
                f"sigma_IC={sd_ic:.4f} outside 2SE of prior {PRIOR_SIGMA_IC} "
                f"(K={n_stride4}, SE={se_sd:.5f})"
            )
    else:
        violations.append(f"sigma_IC unmeasurable (K={n_stride4})")
    if autocorr is not None and n_cohorts > 3:
        se_autocorr = 1.0 / math.sqrt(n_cohorts)
        if abs(autocorr - PRIOR_AUTOCORR) > TRIPWIRE_SE_MULTIPLE * se_autocorr:
            violations.append(
                f"cohort IC autocorr={autocorr:.4f} outside 2SE of prior "
                f"{PRIOR_AUTOCORR} (K={n_cohorts}, SE={se_autocorr:.5f})"
            )

    if violations:
        for violation in violations:
            print(f"TRIPWIRE_VIOLATION={violation}")
        print("DEV_TRIPWIRE=HALT — root-cause before workstream G pins anything")
        return 3
    print(
        f"DEV_TRIPWIRE=OK RHO={rho} SD_IC={sd_ic} AUTOCORR={autocorr} "
        f"N_FIDELITY={n_fidelity} K_STRIDE4={n_stride4}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
