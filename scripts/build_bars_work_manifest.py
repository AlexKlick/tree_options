"""Build a bars-era work manifest from a sealed capture (owner-runnable).

``build_bars_work_manifest`` in ``tree_options.data.bars_manifest`` is the
library seam the launcher's preflight verifies against; this script is the
OWNER-RUNNABLE form of it. The window-A-extension continuation
(2026-09-04) needs a NEW work manifest over the GROWN capture (Fridays
2026-08-28 onward appended in place under ``artifacts/bars/capture/``),
and the approval record that opens the continuation binds the written
file's raw sha256 — so the build is a deliberate, inspectable step:

  uv run python scripts/build_bars_work_manifest.py \\
      --capture-dir artifacts/bars/capture \\
      --capture-manifest artifacts/m4b-coverage-era/capture_manifest.json \\
      --budget 64000 --out artifacts/bars/work-manifest.json

Fail-closed, write-once:
  - the capture manifest is verified (and its bytes bound once) before
    anything is emitted, exactly as the launcher's preflight will verify
    the product;
  - the declared budget must cover the WORST-CASE wire requests (the
    Budget pre-charge model) — an uncoverable grid refuses at build time,
    before any approval could bind it;
  - ``--out`` is never overwritten: the output is created atomically with
    O_CREAT|O_EXCL|O_NOFOLLOW — a rebuilt manifest is a NEW manifest
    (different content hash) and must not silently replace one an
    approval already binds.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:  # pragma: no cover - import plumbing
    sys.path.insert(0, str(REPO_ROOT / "src"))

from tree_options.data.bars_manifest import (  # noqa: E402
    BarsManifestError,
    build_bars_work_manifest,
    load_selection_profile,
)

DEFAULT_PROFILE = REPO_ROOT / "data" / "bars" / "selection-profile.json"


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--capture-dir",
        type=Path,
        required=True,
        help="the sealed capture directory (masters/, bars/, capture_manifest.json)",
    )
    parser.add_argument(
        "--capture-manifest",
        type=Path,
        required=True,
        help="the census-bound capture manifest whose bytes the manifest binds"
        " (the launcher's preflight pins the same file)",
    )
    parser.add_argument(
        "--selection-profile",
        type=Path,
        default=DEFAULT_PROFILE,
        help=f"committed selection profile (default: {DEFAULT_PROFILE.relative_to(REPO_ROOT)})",
    )
    parser.add_argument(
        "--budget",
        type=int,
        required=True,
        help="the declared budget rail (must cover the worst-case wire requests)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        required=True,
        help="the work-manifest file to write (never overwritten)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.budget <= 0:
        print("REFUSED: --budget must be a positive integer", file=sys.stderr)
        return 2
    try:
        profile = load_selection_profile(args.selection_profile)
        manifest = build_bars_work_manifest(
            args.capture_dir,
            profile=profile,
            capture_manifest=args.capture_manifest,
            budget_limit=args.budget,
        )
    except (OSError, BarsManifestError) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 3
    if not manifest.cost.budget_covers_worst_case:
        print(
            f"REFUSED: the declared budget {manifest.cost.budget_limit} cannot"
            f" pre-charge the worst case"
            f" {manifest.cost.worst_case_wire_requests} (Budget charge_block"
            " refuses) — raise --budget or narrow the grid",
            file=sys.stderr,
        )
        return 4
    args.out.parent.mkdir(parents=True, exist_ok=True)
    # (Codex round 1 F5) serialize FIRST, then create the final file
    # atomically: O_CREAT|O_EXCL|O_NOFOLLOW — the exists()-then-write pair
    # was a TOCTOU (an intervening file or symlink to an already-approved
    # manifest would be truncated by write_text); the exclusive no-follow
    # create is one OS-atomic act and refuses both races and preplanted
    # aliases
    payload = (manifest.model_dump_json(indent=2) + "\n").encode("utf-8")
    import os

    try:
        fd = os.open(args.out, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o644)
    except FileExistsError:
        print(
            f"REFUSED: {args.out} already exists — a work manifest is written"
            " once; a rebuild is a NEW manifest and requires removing the"
            " file as a deliberate owner act (an approval may already bind"
            " its bytes)",
            file=sys.stderr,
        )
        return 2
    try:
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)
    import hashlib

    print(
        f"wrote {args.out} ({len(manifest.entries)} entries,"
        f" {manifest.cost.expected_requests} requests, worst case"
        f" {manifest.cost.worst_case_wire_requests}, profile"
        f" {manifest.profile_sha256[:12]}…, capture manifest"
        f" {manifest.capture_manifest_sha256[:12]}…)"
    )
    print(f"file sha256: {hashlib.sha256(args.out.read_bytes()).hexdigest()}")
    print(
        "NEXT: the owner appends the BARS_LAUNCH_APPROVAL record binding"
        " this file's sha256 and the current protocol hash"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
