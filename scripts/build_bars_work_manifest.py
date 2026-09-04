"""Build a bars-era work manifest from a sealed capture (owner-runnable).

``build_bars_work_manifest`` in ``tree_options.data.bars_manifest`` is the
library seam the launcher's preflight verifies against; this script is the
OWNER-RUNNABLE form of it. The window-A-extension continuation
(2026-09-04) needs a NEW work manifest over the GROWN capture (the
continuation Fridays appended in place under ``artifacts/bars/capture/``),
and the approval record that opens the continuation binds the written
file's raw sha256 — so the build is a deliberate, inspectable step:

  uv run python scripts/build_bars_work_manifest.py \\
      --capture-dir artifacts/bars/capture \\
      --capture-manifest artifacts/bars/capture/capture_manifest.json \\
      --from-as-of 2026-08-28 --budget 8000 \\
      --out artifacts/bars/work-manifest-ext-1-c1.json

Fail-closed, write-once:
  - the capture manifest is verified (and its bytes bound once) before
    anything is emitted, exactly as the launcher's preflight will verify
    the product;
  - ``--from-as-of`` is the CONTINUATION filter: the manifest pins only the
    entries at or after the declared Friday, so a continuation manifest
    describes exactly the work the continuation launch will make (the cost
    pre-charge covers only that work);
  - the declared budget must cover the WORST-CASE wire requests (the
    Budget pre-charge model) — an uncoverable grid refuses at build time,
    before any approval could bind it;
  - ``--out`` is never overwritten: the output is created atomically with
    O_CREAT|O_EXCL|O_NOFOLLOW — a rebuilt manifest is a NEW manifest
    (different content hash) and must not silently replace one an
    approval already binds;
  - ``--verify <path>`` is the read-only post-build check: the written
    file is parsed, self-hash bound, and REGENERATED from the capture dir
    through the same library path the launcher will use (nothing is
    written anywhere).
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:  # pragma: no cover - import plumbing
    sys.path.insert(0, str(REPO_ROOT / "src"))

from tree_options.data.bars_manifest import (  # noqa: E402
    BarsManifestError,
    build_bars_work_manifest,
    load_selection_profile,
    parse_bars_work_manifest,
    verify_bars_work_manifest,
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
        "--from-as-of",
        metavar="YYYY-MM-DD",
        help=(
            "the CONTINUATION filter: pin only the entries at or after this"
            " Friday (a continuation manifest describes exactly the new work;"
            " omit it for the legacy full-grid shape)"
        ),
    )
    parser.add_argument(
        "--budget",
        type=int,
        help="the declared budget rail (must cover the worst-case wire requests)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        help="the work-manifest file to write (never overwritten)",
    )
    parser.add_argument(
        "--verify",
        type=Path,
        help="read-only mode: parse + self-hash + REGENERATE the named work"
        " manifest against --capture-dir (nothing is written; --out/--budget"
        " are refused in this mode)",
    )
    return parser.parse_args(argv)


def _verified_model(args: argparse.Namespace):
    """The parse + regeneration check shared by --verify (read-only report)
    and the build path's post-condition. Returns (manifest, file sha) or
    raises BarsManifestError."""
    raw = args.verify.read_bytes()
    manifest = parse_bars_work_manifest(raw, source=str(args.verify))
    profile = load_selection_profile(args.selection_profile)
    capture_manifest_sha = hashlib.sha256(args.capture_manifest.read_bytes()).hexdigest()
    verify_bars_work_manifest(
        manifest,
        profile=profile,
        capture_manifest_sha256=capture_manifest_sha,
        capture_dir=args.capture_dir,
    )
    return manifest, hashlib.sha256(raw).hexdigest()


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.verify is not None:
        if args.out is not None or args.budget is not None:
            print(
                "REFUSED: --verify is read-only — pass --capture-dir/"
                "--capture-manifest (and optionally --selection-profile),"
                " never --out or --budget",
                file=sys.stderr,
            )
            return 2
        try:
            manifest, file_sha = _verified_model(args)
        except (OSError, BarsManifestError) as exc:
            print(f"REFUSED: {exc}", file=sys.stderr)
            return 3
        if not manifest.cost.budget_covers_worst_case:
            print(
                f"REFUSED: the manifest's declared budget {manifest.cost.budget_limit}"
                f" cannot pre-charge the worst case"
                f" {manifest.cost.worst_case_wire_requests} (Budget charge_block"
                " refuses)",
                file=sys.stderr,
            )
            return 4
        as_of_min = manifest.as_of_min if manifest.as_of_min is not None else "none (full grid)"
        print(
            f"verified: {args.verify} ({len(manifest.entries)} entries,"
            f" as_of_min {as_of_min}, content"
            f" {manifest.content_sha256[:12]}…, file {file_sha[:12]}…)"
        )
        return 0
    if args.out is None or args.budget is None:
        print(
            "REFUSED: the build mode requires --out and --budget (or use"
            " --verify <path> for the read-only check)",
            file=sys.stderr,
        )
        return 2
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
            as_of_min=args.from_as_of,
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
    print(
        f"wrote {args.out} ({len(manifest.entries)} entries,"
        f" as_of_min {manifest.as_of_min},"
        f" {manifest.cost.expected_requests} requests, worst case"
        f" {manifest.cost.worst_case_wire_requests}, profile"
        f" {manifest.profile_sha256[:12]}…, capture manifest"
        f" {manifest.capture_manifest_sha256[:12]}…)"
    )
    print(f"file sha256: {hashlib.sha256(args.out.read_bytes()).hexdigest()}")
    print(
        "NEXT: scripts/build_bars_work_manifest.py --verify"
        f" {args.out} --capture-dir {args.capture_dir} --capture-manifest"
        f" {args.capture_manifest}, then the owner appends the"
        " BARS_LAUNCH_APPROVAL record binding this file's sha256 and the"
        " current protocol hash"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
