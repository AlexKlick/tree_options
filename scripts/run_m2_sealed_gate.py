#!/usr/bin/env python3
"""Run the sealed M2-proper validation gate exactly once.

Pre-declared trial set and criteria (docs/m2-proper-plan.md §9, owner-ruled
2026-08-19 BEFORE this run): 16 registered trials over frozen validation
worlds 701-707 only.

  FP arm, H=1   : pooled-null |t| <= 2.5 across 701/702/703 AND fold-level
                  exact-binomial rejections across 3 nulls x 29 folds
                  <= max_allowed_rejections(n)   [8 of 87 at plan estimate]
  FP arm, H=5   : pooled-null |t| <= 2.5 (pooled only — overlapping labels
                  invalidate fold-level t; dev trial D3, plan §9.2)
  POWER arm     : univariate H=1 rejects (t >= 1.96) on BOTH 706 AND 707
  Reported only : weak stratum 704/705, all H=5 power, ridge secondaries

Refuses to reuse a registry or artifact directory. The verdict — PASS or
FAIL — is recorded verbatim whatever it is; a FAIL is evidence, and any
re-run requires a new owner decision.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
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
WORLD_REGISTRY = REPO_ROOT / "data" / "worlds" / "registry.json"
DEFAULT_TRIAL_REGISTRY = REPO_ROOT / "artifacts" / "m2-proper-sealed.db"
DEFAULT_ARTIFACTS_DIR = REPO_ROOT / "artifacts" / "m2-proper-sealed"

UNIVARIATE = "univariate_ic:v1"
RIDGE = "ridge:v1"
NULLS = ("synth-v1-val-null-701", "synth-v1-val-null-702", "synth-v1-val-null-703")
WEAK = ("synth-v1-val-alpha-704", "synth-v1-val-alpha-705")
POWER = ("synth-v1-val-alpha-706", "synth-v1-val-alpha-707")
POOL_T_LIMIT = 2.5
CRITICAL_T = 1.96

SEALED_CONFIGS: tuple[tuple[str, int, str], ...] = (
    *((world, 1, UNIVARIATE) for world in (*NULLS, *WEAK, *POWER)),
    *((world, 5, UNIVARIATE) for world in (*NULLS, *POWER)),
    *((world, horizon, RIDGE) for world in POWER for horizon in (1, 5)),
)


def _generator_code_sha() -> str:
    digest = hashlib.sha256()
    for path in sorted((REPO_ROOT / "src" / "tree_options" / "synth").glob("*.py")):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, default=DEFAULT_TRIAL_REGISTRY)
    parser.add_argument("--artifacts-dir", type=Path, default=DEFAULT_ARTIFACTS_DIR)
    return parser


def _pooled_t(payloads: list[dict]) -> float | None:
    """Pool per-world session-IC means into one t across worlds (each
    world's session sd recovered from its own mean/t/n)."""
    total_mean = 0.0
    total_var = 0.0
    for payload in payloads:
        pooled = payload["pooled"]
        t, n, mean = pooled["t_stat"], pooled["n_sessions"], pooled["mean_ic"]
        if t is None or n is None or mean is None or t == 0 or n < 2:
            return None
        session_sd = abs(mean) * (n**0.5) / abs(t)
        total_mean += mean * n
        total_var += n * session_sd**2
    if total_var <= 0:
        return None
    return total_mean / total_var**0.5


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.registry.exists():
        raise RuntimeError(f"refusing to reuse sealed registry: {args.registry}")
    if args.artifacts_dir.exists():
        raise RuntimeError(f"refusing to reuse sealed artifacts: {args.artifacts_dir}")

    from tree_options.data.authority import PointInTimeDataset
    from tree_options.data.ingest import ingest_snapshot
    from tree_options.data.quality import verify_manifest
    from tree_options.evaluation import assess_false_positives
    from tree_options.models.determinism import blas_pinned
    from tree_options.protocol.loader import load_protocol
    from tree_options.protocol.stamping import build_stamp, write_artifact
    from tree_options.registry.sqlite import TrialRegistry
    from tree_options.synth import WorldSpec, generate_world
    from tree_options.time.calendar import StaticSessionCalendar
    from tree_options.trials import run_trial

    if not blas_pinned():
        raise RuntimeError("BLAS thread pins were not applied before trial imports")

    world_registry = json.loads(WORLD_REGISTRY.read_text(encoding="utf-8"))
    code_sha = _generator_code_sha()
    if world_registry["generator_code_sha"] != code_sha:
        raise RuntimeError("generator code no longer matches the frozen world registry")
    by_id = {entry["world_id"]: entry for entry in world_registry["worlds"]}

    calendar = StaticSessionCalendar(
        REPO_ROOT / "data" / "calendar" / "nyse_sessions_2018_01_02_2026_12_31.json",
        REPO_ROOT / "data" / "calendar" / "nyse_sessions_2018_01_02_2026_12_31.sha256",
    )
    protocol = load_protocol(REPO_ROOT / "research_protocol.yaml")

    datasets: dict[str, PointInTimeDataset] = {}
    for world_id in sorted({config[0] for config in SEALED_CONFIGS}):
        entry = by_id[world_id]
        if entry["pool"] != "validation":
            raise RuntimeError(f"refusing non-validation world {world_id}")
        spec = WorldSpec(**entry["spec"])
        generated = generate_world(spec, calendar)
        snapshot = ingest_snapshot(
            generated.payload,
            generated.master,
            snapshot_id=world_id,
            normalization_code_sha=code_sha,
        )
        verify_manifest(snapshot, calendar)
        expected = entry["expected"]
        observed = {
            "content_sha256": snapshot.manifest.content_sha256,
            "bar_count": snapshot.manifest.bar_count,
            "action_count": snapshot.manifest.action_count,
            "security_count": snapshot.manifest.security_count,
        }
        if observed != expected:
            raise RuntimeError(f"registered world mismatch for {world_id}: {observed}")
        datasets[world_id] = PointInTimeDataset(
            snapshot, calendar, universe_id=f"{world_id}|pit-universe-v1"
        )
        print(f"SEALED_WORLD_OK={world_id} BARS={observed['bar_count']}")

    args.registry.parent.mkdir(parents=True, exist_ok=True)
    registry = TrialRegistry(args.registry)
    payloads: dict[tuple[str, int, str], dict] = {}
    try:
        for world_id, horizon, model_family in sorted(SEALED_CONFIGS):
            dataset = datasets[world_id]
            feature_names = ("mom_1",) if model_family == UNIVARIATE else (
                "mom_1", "mom_5", "mom_20", "dol_vol_20"
            )
            ridge_lambda = None if model_family == UNIVARIATE else 1.0
            result = run_trial(
                dataset=dataset,
                calendar=calendar,
                protocol=protocol,
                world_id=world_id,
                horizon_sessions=horizon,
                feature_names=feature_names,
                ridge_lambda=ridge_lambda,
                model_family=model_family,
                hypothesis=(
                    "sealed validation gate (pre-declared plan §9): "
                    f"{model_family} H{horizon} on {world_id}"
                ),
                decision_sessions=tuple(sorted({bar.session for bar in dataset.bars})),
                registry=registry,
                artifacts_dir=args.artifacts_dir,
                repo=REPO_ROOT,
                clock=lambda: datetime.now(UTC),
            )
            body = json.loads(result.artifact_path.read_text(encoding="utf-8"))
            payloads[(world_id, horizon, model_family)] = body["payload"]
            pooled = body["payload"]["pooled"]
            print(
                f"SEALED_TRIAL={world_id} H{horizon} {model_family} "
                f"STATUS={registry.status(result.trial_id)} FOLDS={result.n_folds} "
                f"MEAN_IC={pooled['mean_ic']} POOLED_T={pooled['t_stat']}"
            )
    finally:
        registry.close()

    # ---- verdict: exactly as pre-declared in plan §9 --------------------
    null_h1 = [payloads[(w, 1, UNIVARIATE)] for w in NULLS]
    null_h5 = [payloads[(w, 5, UNIVARIATE)] for w in NULLS]
    fold_t: list[float] = []
    for payload in null_h1:
        fold_t.extend(float(entry["t_stat"]) for entry in payload["per_fold"])
    fp_fold = assess_false_positives(fold_t)
    fp_h1_pooled_t = _pooled_t(null_h1)
    fp_h5_pooled_t = _pooled_t(null_h5)
    power_t = {
        w: payloads[(w, 1, UNIVARIATE)]["pooled"]["t_stat"] for w in POWER
    }
    checks = {
        "fp_h1_pooled_abs_t_lte_2.5": (
            fp_h1_pooled_t is not None and abs(fp_h1_pooled_t) <= POOL_T_LIMIT
        ),
        "fp_h1_fold_binomial_within_threshold": fp_fold.passes,
        "fp_h5_pooled_abs_t_lte_2.5": (
            fp_h5_pooled_t is not None and abs(fp_h5_pooled_t) <= POOL_T_LIMIT
        ),
        "power_h1_univariate_rejects_both": all(
            t is not None and t >= CRITICAL_T for t in power_t.values()
        ),
    }
    reported = {
        "weak_stratum_h1": {
            w: payloads[(w, 1, UNIVARIATE)]["pooled"] for w in WEAK
        },
        "power_h5_univariate": {
            w: payloads[(w, 5, UNIVARIATE)]["pooled"] for w in POWER
        },
        "ridge_secondary": {
            f"{w}|h{h}": payloads[(w, h, RIDGE)]["pooled"]
            for w in POWER
            for h in (1, 5)
        },
    }
    verdict = "PASS" if all(checks.values()) else "FAIL"

    summary = {
        "gate": "m2-proper-sealed/1",
        "ran_at": datetime.now(UTC).isoformat(),
        "trials": len(SEALED_CONFIGS),
        "fp_h1": {
            "pooled_t": fp_h1_pooled_t,
            "fold_total": fp_fold.total,
            "fold_rejections": fp_fold.rejections,
            "fold_max_allowed": fp_fold.max_allowed_rejections,
            "fold_passes": fp_fold.passes,
        },
        "fp_h5": {"pooled_t": fp_h5_pooled_t},
        "power_h1_univariate": power_t,
        "checks": {name: bool(ok) for name, ok in checks.items()},
        "reported_only": reported,
        "verdict": verdict,
    }
    stamp = build_stamp(
        protocol,
        trial_id="m2p-sealed-gate-summary",
        config={"gate": "m2-proper-sealed/1", "trials": len(SEALED_CONFIGS)},
        dataset_manifest_hash=hashlib.sha256(WORLD_REGISTRY.read_bytes()).hexdigest(),
        repo=REPO_ROOT,
    )
    write_artifact(args.artifacts_dir / "sealed-gate-summary.json", summary, stamp)
    print(
        f"SEALED_GATE_VERDICT={verdict} "
        f"FP_H1_POOLED_T={fp_h1_pooled_t} FP_H1_FOLDS={fp_fold.rejections}/{fp_fold.total}"
        f"(max {fp_fold.max_allowed_rejections}) FP_H5_POOLED_T={fp_h5_pooled_t} "
        f"POWER_H1_T={ {w: round(t, 3) if t is not None else None for w, t in power_t.items()} }"
    )
    for name, ok in checks.items():
        print(f"SEALED_CHECK {'PASS' if ok else 'FAIL'} {name}")
    return 0 if verdict == "PASS" else 4


if __name__ == "__main__":
    sys.exit(main())
