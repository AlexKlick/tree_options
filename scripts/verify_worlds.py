"""Regenerate registered synthetic worlds and assert byte-exact reproduction.

Each registry world is pinned twice: to its WorldSpec (seeds/rates — never
rewritten by this tool) and to the generating code (generator sha — re-pinned
ONLY by an explicit --recompute after a deliberate generator change).
Validation worlds are the synthetic-era holdout (M2 packet §1.4): dev-pool
tuning against them is prohibited; final validation runs happen there.

Usage:
  uv run python scripts/verify_worlds.py                        # verify all (slow)
  uv run python scripts/verify_worlds.py --pool dev --max-bars 20000   # gate subset
  uv run python scripts/verify_worlds.py --world synth-v1-dev-null-101
  uv run python scripts/verify_worlds.py --recompute            # re-pin expected hashes

Exit 0 = every selected world reproduced byte-exact and the generator pin
matches. Any mismatch names the offending world.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = REPO_ROOT / "data" / "worlds" / "registry.json"
SYNTH_SRC = REPO_ROOT / "src" / "tree_options" / "synth"
CALENDAR_JSON = REPO_ROOT / "data" / "calendar" / "nyse_sessions_2018_01_02_2026_12_31.json"
CALENDAR_SHA = CALENDAR_JSON.with_suffix(".sha256")


def generator_code_sha() -> str:
    digest = hashlib.sha256()
    for path in sorted(SYNTH_SRC.glob("*.py")):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _load_calendar():  # type: ignore[no-untyped-def]
    from tree_options.time.calendar import StaticSessionCalendar

    return StaticSessionCalendar(CALENDAR_JSON, CALENDAR_SHA)


def _generate_and_ingest(entry: dict[str, object], code_sha: str):  # type: ignore[no-untyped-def]
    from tree_options.data.ingest import ingest_snapshot
    from tree_options.synth import generate_world
    from tree_options.synth.spec import WorldSpec

    spec = WorldSpec(**entry["spec"])  # type: ignore[arg-type]
    world = generate_world(spec, _load_calendar())
    snapshot = ingest_snapshot(
        world.payload,
        world.master,
        snapshot_id=spec.world_id,
        normalization_code_sha=code_sha,
    )
    return snapshot


def _worlds(registry: dict[str, object], args: argparse.Namespace) -> list[dict[str, object]]:
    worlds = registry["worlds"]  # type: ignore[index]
    selected: list[dict[str, object]] = []
    for entry in worlds:  # type: ignore[union-attr]
        if args.world and entry["world_id"] not in args.world:
            continue
        if args.pool and entry["pool"] != args.pool:
            continue
        if args.max_bars is not None and entry["expected"]["bar_count"] > args.max_bars:  # type: ignore[index]
            continue
        selected.append(entry)
    return selected


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pool", choices=("dev", "validation"))
    parser.add_argument("--world", action="append", help="world_id (repeatable)")
    parser.add_argument(
        "--max-bars", type=int, help="skip worlds with more registered bars"
    )
    parser.add_argument(
        "--recompute",
        action="store_true",
        help="regenerate all worlds and rewrite expected hashes + generator pin",
    )
    parser.add_argument("--registry", type=Path, default=REGISTRY_PATH)
    args = parser.parse_args(argv)

    registry = json.loads(args.registry.read_text())
    code_sha = generator_code_sha()

    if args.recompute:
        worlds = registry["worlds"]  # type: ignore[index]
        for entry in worlds:  # type: ignore[union-attr]
            snapshot = _generate_and_ingest(entry, code_sha)
            entry["expected"] = {
                "content_sha256": snapshot.manifest.content_sha256,
                "bar_count": snapshot.manifest.bar_count,
                "action_count": snapshot.manifest.action_count,
                "security_count": snapshot.manifest.security_count,
            }
            print(f"recomputed {entry['world_id']}")
        registry["generator_code_sha"] = code_sha
        args.registry.write_text(json.dumps(registry, indent=2) + "\n")
        print(f"registry rewritten: {args.registry}")
        return 0

    if registry["generator_code_sha"] != code_sha:  # type: ignore[index]
        print(
            "FAIL generator pin: registry pins "
            f"{registry['generator_code_sha'][:12]}…, current synth/ hashes to {code_sha[:12]}…",  # type: ignore[index]
        )
        print("     a generator change invalidates every registered world; re-pin deliberately")
        print("     with --recompute (and record the change in the evidence doc).")
        return 2

    selected = _worlds(registry, args)
    if not selected:
        print("no worlds selected")
        return 1

    mismatches = 0
    for entry in selected:
        snapshot = _generate_and_ingest(entry, code_sha)
        exp = entry["expected"]  # type: ignore[index]
        ok = (
            snapshot.manifest.content_sha256 == exp["content_sha256"]
            and snapshot.manifest.bar_count == exp["bar_count"]
            and snapshot.manifest.action_count == exp["action_count"]
            and snapshot.manifest.security_count == exp["security_count"]
        )
        status = "OK " if ok else "MISMATCH"
        print(
            f"{status} {entry['world_id']} bars={snapshot.manifest.bar_count} "
            f"actions={snapshot.manifest.action_count} "
            f"securities={snapshot.manifest.security_count}"
        )
        if not ok:
            mismatches += 1
            print(f"     expected {exp}")
    print(
        f"WORLDS_OK={len(selected) - mismatches} MISMATCH={mismatches} "
        f"SELECTED={len(selected)} CODE_PIN=match"
    )
    return 1 if mismatches else 0


if __name__ == "__main__":
    sys.exit(main())
