#!/usr/bin/env python
"""Operator write path for durable run state (PR A/A1) — the durable
replacement for `/tmp` markers and ad-hoc exit-code evidence.

One invocation = one journaled fact: a lifecycle transition, a manifest
pin, or a store creation. The caller holds the exclusive lease for the
duration of the write (duplicate launchers are refused while a live owner
exists; a provably stale lease may be adopted with --adopt-stale-lease
AFTER looking at `era_status`).

Exit codes (contract; also in docs/m4-closeout-runbook.md):
  0  recorded
  2  illegal transition (skip/regression/UNKNOWN target)
  3  lease held by a live owner
  4  unknown run (no store; create one with --create-identity)
  5  store unreadable/corrupt (journal or projection)

Usage:
  python scripts/runstate_mark.py <run-id> CAPTURING --reason "era pass 3"
  python scripts/runstate_mark.py <run-id> CAPTURE_COMPLETE \
      --reason "wrapper exited 0" --pin-manifest <capture_manifest content_sha256>
  python scripts/runstate_mark.py <run-id> INSPECTION_RUNNING \
      --adopt-stale-lease --reason "reboot recovery: old owner dead"
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT / "src") not in sys.path:  # pragma: no cover - import plumbing
    sys.path.insert(0, str(REPO_ROOT / "src"))

from tree_options.runstate import RunIdentity, RunState, RunStore  # noqa: E402
from tree_options.runstate import errors as rs_errors  # noqa: E402
from tree_options.runstate import lease as lease_module  # noqa: E402
from tree_options.runstate.store import DEFAULT_STORE_ROOT  # noqa: E402


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("run_id", help="deterministic run id under the store root")
    parser.add_argument(
        "to_state",
        nargs="?",
        choices=[s.value for s in RunState],
        help="target state (omit only with --pin-manifest)",
    )
    parser.add_argument("--reason", required=True, help="operator-visible why")
    parser.add_argument(
        "--store-root",
        type=Path,
        default=DEFAULT_STORE_ROOT,
        help="run-state root (default: artifacts/runstate)",
    )
    parser.add_argument(
        "--pin-manifest",
        metavar="SHA256",
        help="pin the capture manifest content hash instead of transitioning",
    )
    parser.add_argument(
        "--create-identity",
        type=Path,
        metavar="JSON",
        help="create the store first from a RunIdentity JSON file",
    )
    parser.add_argument(
        "--adopt-stale-lease",
        action="store_true",
        help="adopt a provably stale lease (dead pid / boot change / pid reuse / torn)",
    )
    parser.add_argument(
        "--boot-id-override",
        help=argparse.SUPPRESS,  # test seam: pretend a different boot
    )
    parser.add_argument(
        "--now-epoch",
        type=int,
        help=argparse.SUPPRESS,  # test seam: injected clock
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.to_state is None and not args.pin_manifest and not args.create_identity:
        print("ERROR: a to_state or --pin-manifest is required", file=sys.stderr)
        return 2
    if args.create_identity and (args.to_state is not None or args.pin_manifest):
        print(
            "ERROR: --create-identity only creates the store (genesis PLANNED); "
            "transition or pin in a separate invocation",
            file=sys.stderr,
        )
        return 2
    # Round-1 review fix: one journaled fact per invocation — refuse the
    # silent-drop combination (to_state + --pin-manifest). The earlier
    # code took the pin branch and ignored the requested transition.
    if args.to_state is not None and args.pin_manifest:
        print(
            "ERROR: pass either a to_state OR --pin-manifest, never both — "
            "the earlier silent-drop behavior laundered a transition; "
            "use two invocations (runbook §4.2)",
            file=sys.stderr,
        )
        return 2
    from datetime import UTC, datetime

    now_epoch = args.now_epoch or int(datetime.now(UTC).timestamp())
    boot_id_now = args.boot_id_override or lease_module.read_boot_id()
    root: Path = args.store_root

    if args.create_identity:
        identity = RunIdentity.model_validate(
            json.loads(args.create_identity.read_text(encoding="utf-8"))
        )
        if identity.run_id != args.run_id:
            print(
                f"IDENTITY MISMATCH: file names {identity.run_id!r}, argument says {args.run_id!r}",
                file=sys.stderr,
            )
            return 5
        try:
            RunStore.create(root, identity, now_epoch=now_epoch)
        except rs_errors.StoreExistsError as exc:
            print(f"STORE EXISTS: {exc}", file=sys.stderr)
            return 5
        print(json.dumps({"run_id": args.run_id, "created": True}, sort_keys=True))
        return 0

    try:
        store = RunStore.open(root, args.run_id)
    except rs_errors.UnknownRunError as exc:
        print(f"UNKNOWN RUN: {exc}", file=sys.stderr)
        return 4
    except rs_errors.JournalCorruptError as exc:
        print(f"JOURNAL CORRUPT: {exc}", file=sys.stderr)
        return 5

    owner = lease_module.current_owner(now_epoch=now_epoch)
    if args.boot_id_override:
        owner = owner.model_copy(update={"boot_id": args.boot_id_override})
    try:
        lease_module.acquire(
            store.dir,
            owner,
            boot_id_now=boot_id_now,
            allow_stale_adopt=args.adopt_stale_lease,
        )
    except rs_errors.LeaseHeldError as exc:
        print(f"LEASE HELD: {exc}", file=sys.stderr)
        return 3

    try:
        if args.pin_manifest:
            store.pin_manifest(
                args.pin_manifest,
                now_epoch=now_epoch,
                actor_pid=owner.pid,
                actor_boot_id=boot_id_now,
            )
        else:
            store.transition(
                RunState(args.to_state),
                reason=args.reason,
                now_epoch=now_epoch,
                actor_pid=owner.pid,
                actor_boot_id=boot_id_now,
                owner=owner,
            )
    except rs_errors.IllegalTransitionError as exc:
        print(f"ILLEGAL TRANSITION: {exc}", file=sys.stderr)
        return 2
    except rs_errors.JournalCorruptError as exc:
        print(f"JOURNAL CORRUPT: {exc}", file=sys.stderr)
        return 5
    finally:
        lease_module.release(store.dir, owner)

    print(
        json.dumps(
            {
                "run_id": store.identity.run_id,
                "state": store.state.value if store.state else None,
                "seq": len((store.dir / "journal.jsonl").read_text().splitlines()),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
