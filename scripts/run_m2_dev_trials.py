#!/usr/bin/env python3
"""Run the four frozen M2-proper development trials exactly once.

This driver deliberately selects only development worlds 103/104.  It never
loads or mentions validation-world payloads, and it refuses to reuse a trial
registry or artifact directory.  A D4 mean IC outside the pre-registered
0.002 +/-20% band halts the campaign after recording all development outputs;
it is not a reason to open the sealed validation gate.
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
DEFAULT_TRIAL_REGISTRY = REPO_ROOT / "artifacts" / "m2-proper-dev-trials.db"
DEFAULT_ARTIFACTS_DIR = REPO_ROOT / "artifacts" / "m2-proper-dev"
D4_EXPECTED_IC = 0.002
D4_RELATIVE_TOLERANCE = 0.20


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
        raise RuntimeError(f"refusing to reuse development registry: {args.registry}")
    if args.artifacts_dir.exists():
        raise RuntimeError(f"refusing to reuse development artifacts: {args.artifacts_dir}")

    from tree_options.data.authority import PointInTimeDataset
    from tree_options.data.ingest import ingest_snapshot
    from tree_options.data.quality import verify_manifest
    from tree_options.models.determinism import blas_pinned
    from tree_options.protocol.loader import load_protocol
    from tree_options.registry.sqlite import TrialRegistry
    from tree_options.synth import WorldSpec, generate_world
    from tree_options.time.calendar import StaticSessionCalendar
    from tree_options.trials import DEV_TRIAL_CONFIGS, run_trial

    if not blas_pinned():
        raise RuntimeError("BLAS thread pins were not applied before trial imports")

    world_registry = json.loads(WORLD_REGISTRY.read_text(encoding="utf-8"))
    code_sha = _generator_code_sha()
    if world_registry["generator_code_sha"] != code_sha:
        raise RuntimeError("generator code no longer matches the frozen world registry")
    by_id = {entry["world_id"]: entry for entry in world_registry["worlds"]}
    allowed_worlds = {config.world_id for config in DEV_TRIAL_CONFIGS}
    if allowed_worlds != {"synth-v1-dev-null-103", "synth-v1-dev-alpha-104"}:
        raise RuntimeError(f"development config escaped worlds 103/104: {sorted(allowed_worlds)}")

    calendar = StaticSessionCalendar(
        REPO_ROOT / "data" / "calendar" / "nyse_sessions_2018_01_02_2026_12_31.json",
        REPO_ROOT / "data" / "calendar" / "nyse_sessions_2018_01_02_2026_12_31.sha256",
    )
    protocol = load_protocol(REPO_ROOT / "research_protocol.yaml")
    datasets: dict[str, PointInTimeDataset] = {}
    for world_id in sorted(allowed_worlds):
        entry = by_id[world_id]
        if entry["pool"] != "dev":
            raise RuntimeError(f"refusing non-development world {world_id}")
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
            raise RuntimeError(
                f"registered world mismatch for {world_id}: {observed} != {expected}"
            )
        datasets[world_id] = PointInTimeDataset(
            snapshot,
            calendar,
            universe_id=f"{world_id}|pit-universe-v1",
        )
        print(
            f"DEV_WORLD_OK={world_id} BARS={snapshot.manifest.bar_count} "
            f"ACTIONS={snapshot.manifest.action_count} MANIFEST={snapshot.manifest.content_sha256}"
        )

    args.registry.parent.mkdir(parents=True, exist_ok=True)
    registry = TrialRegistry(args.registry)
    d4_mean_ic: float | None = None
    try:
        for config in DEV_TRIAL_CONFIGS:
            dataset = datasets[config.world_id]
            decision_sessions = tuple(sorted({bar.session for bar in dataset.bars}))
            result = run_trial(
                dataset=dataset,
                calendar=calendar,
                protocol=protocol,
                world_id=config.world_id,
                horizon_sessions=config.horizon_sessions,
                feature_names=config.feature_names,
                ridge_lambda=config.ridge_lambda,
                model_family=config.model_family,
                hypothesis=config.hypothesis,
                decision_sessions=decision_sessions,
                registry=registry,
                artifacts_dir=args.artifacts_dir,
                repo=REPO_ROOT,
                clock=lambda: datetime.now(UTC),
            )
            body = json.loads(result.artifact_path.read_text(encoding="utf-8"))
            payload = body["payload"]
            mean_ic = payload["pooled"]["mean_ic"]
            print(
                f"DEV_TRIAL={config.config_id} STATUS={registry.status(result.trial_id)} "
                f"TRIAL_ID={result.trial_id} FOLDS={result.n_folds} "
                f"ROWS={result.n_scored_rows} POOLED_T={result.pooled_t} "
                f"MEAN_IC={mean_ic} BACKTEST_RETURN={payload['backtest']['total_return']}"
            )
            if config.config_id == "D4":
                d4_mean_ic = float(mean_ic)
    finally:
        registry.close()

    if d4_mean_ic is None:
        raise RuntimeError("D4 did not produce a pooled mean IC")
    low = D4_EXPECTED_IC * (1.0 - D4_RELATIVE_TOLERANCE)
    high = D4_EXPECTED_IC * (1.0 + D4_RELATIVE_TOLERANCE)
    d4_passes = low <= d4_mean_ic <= high
    print(
        f"DEV_TRIALS_COMPLETED={len(DEV_TRIAL_CONFIGS)} FAILED=0 "
        f"D4_MEAN_IC={d4_mean_ic} D4_LOW={low} D4_HIGH={high} "
        f"D4_WITHIN_20_PERCENT={int(d4_passes)}"
    )
    return 0 if d4_passes else 3


if __name__ == "__main__":
    sys.exit(main())
