#!/usr/bin/env python3
"""Run the corrected sealed M2-proper power gate exactly once (gate #2).

Pre-declared in docs/m2-proper-plan.md §10 (owner-ruled 2026-08-19 as the
disposition of gate #1's power-arm FAIL, BEFORE this run): 8 registered
trials over NEW power worlds 708/709 (coefficient 0.01, fresh seeds).

  POWER criterion : univariate H=1 pooled t >= 1.96 on BOTH 708 AND 709
  Reported only   : univariate H=5 and ridge H1/H5 (§9.2 overlap
                    inflation, §9.3 dilution/sign instability)

The FP arm is NOT re-run: it passed at gate #1 and worlds 701-705 are
frozen forever. Gate #1, its registry, and its artifacts are immutable.
Refuses to reuse a registry or artifact directory. The verdict — PASS or
FAIL — is recorded verbatim whatever it is; no further re-run inside
this campaign regardless of outcome.
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
DEFAULT_TRIAL_REGISTRY = REPO_ROOT / "artifacts" / "m2-proper-sealed-2.db"
DEFAULT_ARTIFACTS_DIR = REPO_ROOT / "artifacts" / "m2-proper-sealed-2"

UNIVARIATE = "univariate_ic:v1"
RIDGE = "ridge:v1"
POWER = ("synth-v1-val-alpha-708", "synth-v1-val-alpha-709")
CRITICAL_T = 1.96

SEALED_CONFIGS: tuple[tuple[str, int, str], ...] = (
    *((world, horizon, UNIVARIATE) for world in POWER for horizon in (1, 5)),
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


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.registry.exists():
        raise RuntimeError(f"refusing to reuse sealed registry: {args.registry}")
    if args.artifacts_dir.exists():
        raise RuntimeError(f"refusing to reuse sealed artifacts: {args.artifacts_dir}")

    from tree_options.data.authority import PointInTimeDataset
    from tree_options.data.ingest import ingest_snapshot
    from tree_options.data.quality import verify_manifest
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
        if entry["spec"]["alpha"]["coefficient"] != 0.01:
            raise RuntimeError(f"refusing non-gate-2 power world {world_id}")
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
        print(f"SEALED2_WORLD_OK={world_id} BARS={observed['bar_count']}")

    args.registry.parent.mkdir(parents=True, exist_ok=True)
    registry = TrialRegistry(args.registry)
    payloads: dict[tuple[str, int, str], dict] = {}
    try:
        for world_id, horizon, model_family in sorted(SEALED_CONFIGS):
            dataset = datasets[world_id]
            feature_names = (
                ("mom_1",)
                if model_family == UNIVARIATE
                else ("mom_1", "mom_5", "mom_20", "dol_vol_20")
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
                    "sealed power gate #2 (pre-declared plan §10): "
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
                f"SEALED2_TRIAL={world_id} H{horizon} {model_family} "
                f"STATUS={registry.status(result.trial_id)} FOLDS={result.n_folds} "
                f"MEAN_IC={pooled['mean_ic']} POOLED_T={pooled['t_stat']}"
            )
    finally:
        registry.close()

    # ---- verdict: exactly as pre-declared in plan §10 --------------------
    power_t = {w: payloads[(w, 1, UNIVARIATE)]["pooled"]["t_stat"] for w in POWER}
    checks = {
        "power_h1_univariate_rejects_both": all(
            t is not None and t >= CRITICAL_T for t in power_t.values()
        ),
    }
    reported = {
        "power_h5_univariate": {w: payloads[(w, 5, UNIVARIATE)]["pooled"] for w in POWER},
        "ridge_secondary": {
            f"{w}|h{h}": payloads[(w, h, RIDGE)]["pooled"] for w in POWER for h in (1, 5)
        },
    }
    verdict = "PASS" if all(checks.values()) else "FAIL"

    summary = {
        "gate": "m2-proper-sealed-2/1",
        "ran_at": datetime.now(UTC).isoformat(),
        "trials": len(SEALED_CONFIGS),
        "power_h1_univariate": power_t,
        "checks": {name: bool(ok) for name, ok in checks.items()},
        "reported_only": reported,
        "verdict": verdict,
    }
    stamp = build_stamp(
        protocol,
        trial_id="m2p-sealed-gate2-summary",
        config={"gate": "m2-proper-sealed-2/1", "trials": len(SEALED_CONFIGS)},
        dataset_manifest_hash=hashlib.sha256(WORLD_REGISTRY.read_bytes()).hexdigest(),
        repo=REPO_ROOT,
    )
    write_artifact(args.artifacts_dir / "sealed-gate2-summary.json", summary, stamp)
    print(
        f"SEALED2_GATE_VERDICT={verdict} "
        f"POWER_H1_T={ {w: round(t, 3) if t is not None else None for w, t in power_t.items()} }"
    )
    for name, ok in checks.items():
        print(f"SEALED2_CHECK {'PASS' if ok else 'FAIL'} {name}")
    return 0 if verdict == "PASS" else 4


if __name__ == "__main__":
    sys.exit(main())
