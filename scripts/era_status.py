#!/usr/bin/env python
"""Read-only run-status command (PR A/A1). Reports, never mutates.

For a journaled run: lifecycle state, heartbeat classification, lease
classification, journal tail hash + damage, pinned manifest. For the
pre-journal legacy coverage era (live right now): discovers the capture
process by /proc cmdline and reports UNKNOWN — a run with no journal is
UNKNOWN, never healthy, never FAILED (constraint 10).

This command NEVER repairs anything: a torn projection is reported
(exit 2), not rebuilt — repair belongs to a writer that holds the lease.

Exit codes (contract; also in docs/m4-closeout-runbook.md):
  0  determinate state (ALIVE / DEAD_TERMINAL / terminal journal state)
  2  store unreadable/corrupt (journal mid-file corruption, torn projection)
  3  UNKNOWN / RECONCILIATION_REQUIRED
  4  no run found (and no legacy capture process either)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT / "src") not in sys.path:  # pragma: no cover - import plumbing
    sys.path.insert(0, str(REPO_ROOT / "src"))

from tree_options.runstate import RunStore  # noqa: E402
from tree_options.runstate import errors as rs_errors  # noqa: E402
from tree_options.runstate import lease as lease_module  # noqa: E402
from tree_options.runstate.heartbeat import HeartbeatClass  # noqa: E402
from tree_options.runstate.store import DEFAULT_STORE_ROOT  # noqa: E402

CAPTURE_SCRIPT_TOKEN = "capture_massive_structural.py"

# Test seam: /proc root for legacy-process discovery.
PROC_ROOT = Path("/proc")


def _find_legacy_capture(capture_dir_token: str) -> list[int]:
    """Pids whose cmdline names the capture script AND the capture dir."""
    hits: list[int] = []
    for entry in PROC_ROOT.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cmdline = (
                (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
            )
        except OSError:
            continue
        if CAPTURE_SCRIPT_TOKEN in cmdline and capture_dir_token in cmdline:
            hits.append(int(entry.name))
    return sorted(hits)


def _latest_run_id(root: Path) -> str | None:
    if not root.exists():
        return None
    candidates = [p for p in root.iterdir() if (p / "run.json").is_file()]
    if not candidates:
        return None
    return max(candidates, key=lambda p: (p / "journal.jsonl").stat().st_mtime).name


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--run-id", help="default: most recently written store")
    parser.add_argument(
        "--store-root", type=Path, default=DEFAULT_STORE_ROOT, help="run-state root"
    )
    parser.add_argument(
        "--capture-dir",
        type=Path,
        help="legacy era capture dir for pre-journal process discovery",
    )
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--boot-id-override", help=argparse.SUPPRESS)
    parser.add_argument("--now-epoch", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--proc-root", type=Path, help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    global PROC_ROOT
    args = _parse_args(argv)
    from datetime import UTC, datetime

    if args.proc_root is not None:
        PROC_ROOT = args.proc_root
    now_epoch = args.now_epoch or int(datetime.now(UTC).timestamp())
    boot_id_now = args.boot_id_override or lease_module.read_boot_id(PROC_ROOT)

    run_id = args.run_id or _latest_run_id(args.store_root)
    if run_id is None:
        legacy = _find_legacy_capture(args.capture_dir.name) if args.capture_dir else []
        if legacy:
            print(
                json.dumps(
                    {
                        "run_id": None,
                        "state": "UNKNOWN",
                        "classification": "UNKNOWN_RESUMABLE",
                        "note": (
                            "pre-journal legacy era: live capture process(es) "
                            f"{legacy}; adopt via runstate_mark at next resume"
                        ),
                    },
                    sort_keys=True,
                )
            )
            return 3
        print("NO RUN FOUND: no store under the root and no legacy process", file=sys.stderr)
        return 4

    try:
        store = RunStore.open(args.store_root, run_id)
    except rs_errors.UnknownRunError:
        print(f"UNKNOWN RUN: {run_id}", file=sys.stderr)
        return 4
    except rs_errors.JournalCorruptError as exc:
        print(f"JOURNAL CORRUPT: {exc}", file=sys.stderr)
        return 2

    try:
        # Read-only command: NEVER repair. A torn projection is reported,
        # not rebuilt — rebuilding is a write and belongs to a lease holder.
        from tree_options.runstate.journal import load_projection

        load_projection(store.dir, run_id=run_id)
    except rs_errors.ProjectionTornError as exc:
        print(f"PROJECTION TORN: {exc}", file=sys.stderr)
        return 2

    status = store.status(now_epoch=now_epoch, boot_id_now=boot_id_now, proc_root=PROC_ROOT)
    payload = {
        "run_id": status.run_id,
        "state": status.state.value if status.state else None,
        "classification": status.heartbeat_class.value,
        "lease": status.lease_class.value if status.lease_class else None,
        "journal_tail": status.tail_hash[:12],
        "seq": status.seq,
        "tail_damaged": status.tail_damaged,
        "pinned_manifest": (
            status.pinned_manifest_sha256[:12] if status.pinned_manifest_sha256 else None
        ),
        "failure_reason": status.failure_reason,
    }
    print(json.dumps(payload, sort_keys=True))

    if status.heartbeat_class in (
        HeartbeatClass.UNKNOWN_RESUMABLE,
        HeartbeatClass.UNKNOWN_RECONCILIATION_REQUIRED,
    ):
        return 3
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
