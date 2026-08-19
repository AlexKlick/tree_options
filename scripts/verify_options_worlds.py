"""Regenerate registered option-chain overlays and assert byte-exact
reproduction (M3 plan §3.A).

Each overlay is pinned three ways: to its parent equity world (the v1
registry spec — regenerated and manifest-verified first), to the overlay
spec (seeds/knobs — never rewritten by this tool), and to the generating
code (`synth_options_code_sha` over every byte of `synth_options/*.py` —
re-pinned ONLY by an explicit --recompute after a deliberate change).

Because full materialization is infeasible by design (plan §7 scale),
verification is sample-based plus analytic counts: a deterministic anchor
sample of (underlying, session) day files is regenerated and hashed
byte-exactly, and the contract/entry/quote-event counts are recomputed
analytically and compared exactly.

Usage:
  uv run python scripts/verify_options_worlds.py
  uv run python scripts/verify_options_worlds.py --world synth-v1-dev-null-101
  uv run python scripts/verify_options_worlds.py --recompute

Exit 0 = every selected overlay reproduced byte-exact and the code pin
matches; exit 2 = code-pin mismatch (re-pin deliberately); exit 1 = a
selected overlay mismatched.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
EQUITY_REGISTRY_PATH = REPO_ROOT / "data" / "worlds" / "registry.json"
OPTIONS_REGISTRY_PATH = REPO_ROOT / "data" / "worlds" / "options_registry.json"
SYNTH_OPTIONS_SRC = REPO_ROOT / "src" / "tree_options" / "synth_options"
CALENDAR_JSON = REPO_ROOT / "data" / "calendar" / "nyse_sessions_2018_01_02_2026_12_31.json"
CALENDAR_SHA = CALENDAR_JSON.with_suffix(".sha256")


def synth_options_code_sha() -> str:
    digest = hashlib.sha256()
    for path in sorted(SYNTH_OPTIONS_SRC.glob("*.py")):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _load_calendar():  # type: ignore[no-untyped-def]
    from tree_options.time.calendar import StaticSessionCalendar

    return StaticSessionCalendar(CALENDAR_JSON, CALENDAR_SHA)


def _build_overlay(entry: dict[str, object]):  # type: ignore[no-untyped-def]
    """Parent equity world -> ingest -> verify -> overlay."""
    from tree_options.data.ingest import ingest_snapshot
    from tree_options.data.quality import verify_manifest
    from tree_options.synth import generate_world
    from tree_options.synth.spec import WorldSpec
    from tree_options.synth_options import OptionsOverlaySpec, generate_overlay

    world_id = entry["world_id"]
    equity = json.loads(EQUITY_REGISTRY_PATH.read_text())
    parent = next((w for w in equity["worlds"] if w["world_id"] == world_id), None)
    if parent is None:
        raise SystemExit(f"overlay {world_id} has no parent equity world in the registry")
    spec = WorldSpec(**parent["spec"])
    calendar = _load_calendar()
    world = generate_world(spec, calendar)
    snapshot = ingest_snapshot(
        world.payload,
        world.master,
        snapshot_id=spec.world_id,
        normalization_code_sha=json.loads(OPTIONS_REGISTRY_PATH.read_text()).get(
            "synth_options_code_sha", "0" * 64
        ),
    )
    verify_manifest(snapshot, calendar)  # quality gates run inside regeneration
    overlay_spec = OptionsOverlaySpec(**entry["spec"])  # type: ignore[arg-type]
    return generate_overlay(
        spec=overlay_spec,
        bars=snapshot.bars,
        master=snapshot.master,
        actions=snapshot.actions,
        calendar=calendar,
    )


def _measure(overlay):  # type: ignore[no-untyped-def]
    import hashlib as _hashlib

    entries, quotes = overlay.entry_and_quote_counts()
    slices = [
        [
            sid,
            session.isoformat(),
            _hashlib.sha256(overlay.canonical_file_bytes(sid, session)).hexdigest(),
        ]
        for sid, session in overlay.anchor_slices()
    ]
    return {
        "contract_count": overlay.contract_count(),
        "entry_count": entries,
        "quote_event_count": quotes,
        "sample_slice_hashes": slices,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--world", action="append", help="parent world_id (repeatable)")
    parser.add_argument(
        "--recompute",
        action="store_true",
        help="regenerate all overlays and rewrite expected hashes + code pin",
    )
    parser.add_argument("--registry", type=Path, default=OPTIONS_REGISTRY_PATH)
    args = parser.parse_args(argv)

    registry = json.loads(args.registry.read_text())
    code_sha = synth_options_code_sha()

    if args.recompute:
        for entry in registry["overlays"]:
            overlay = _build_overlay(entry)
            entry["expected"] = _measure(overlay)
            print(
                f"recomputed {entry['world_id']} "
                f"contracts={entry['expected']['contract_count']} "
                f"quotes={entry['expected']['quote_event_count']} "
                f"anchors={len(entry['expected']['sample_slice_hashes'])}"
            )
        registry["synth_options_code_sha"] = code_sha
        args.registry.write_text(json.dumps(registry, indent=2) + "\n")
        print(f"registry rewritten: {args.registry}")
        return 0

    if registry["synth_options_code_sha"] != code_sha:
        print(
            "FAIL code pin: registry pins "
            f"{str(registry['synth_options_code_sha'])[:12]}…, current synth_options/ "
            f"hashes to {code_sha[:12]}…"
        )
        print("     a generator change invalidates every registered overlay; re-pin")
        print("     deliberately with --recompute (and record it in the evidence doc).")
        return 2

    selected = [e for e in registry["overlays"] if not args.world or e["world_id"] in args.world]
    if not selected:
        print("no overlays selected")
        return 1

    mismatches = 0
    for entry in selected:
        overlay = _build_overlay(entry)
        measured = _measure(overlay)
        exp = entry["expected"]
        ok = (
            measured["contract_count"] == exp["contract_count"]
            and measured["entry_count"] == exp["entry_count"]
            and measured["quote_event_count"] == exp["quote_event_count"]
            and measured["sample_slice_hashes"] == exp["sample_slice_hashes"]
        )
        status = "OK " if ok else "MISMATCH"
        print(
            f"{status} {entry['world_id']} contracts={measured['contract_count']} "
            f"entries={measured['entry_count']} quotes={measured['quote_event_count']} "
            f"anchors={len(measured['sample_slice_hashes'])}"
        )
        if not ok:
            mismatches += 1
            for key in ("contract_count", "entry_count", "quote_event_count"):
                if measured[key] != exp[key]:
                    print(f"     {key}: expected {exp[key]}, got {measured[key]}")
            if measured["sample_slice_hashes"] != exp["sample_slice_hashes"]:
                got = {tuple(s[:2]): s[2] for s in measured["sample_slice_hashes"]}
                want = {tuple(s[:2]): s[2] for s in exp["sample_slice_hashes"]}
                differing = [k for k in got if got.get(k) != want.get(k)][:5]
                print(f"     slice hashes differ at (first 5): {differing}")
    print(
        f"OVERLAYS_OK={len(selected) - mismatches} MISMATCH={mismatches} "
        f"SELECTED={len(selected)} CODE_PIN=match"
    )
    return 1 if mismatches else 0


if __name__ == "__main__":
    sys.exit(main())
