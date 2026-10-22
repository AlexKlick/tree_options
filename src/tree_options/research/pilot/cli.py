"""``python -m tree_options.research.pilot``: the loopback pilot CLI.

    import-drafts DIR        write every registration in DIR as a draft
    list                     the trex-pilot-view/1 document (JSON)
    seal --program-id ID --sha256 HEX --owner-email EMAIL
                             the owner's seal: EMAIL must be in TREX_OWNER_EMAILS,
                             HEX the body hash the owner reviewed (``list``
                             shows it as review_sha256)
    usage --program-id ID [--day YYYY-MM-DD]
                             one program's usage and open job markers (JSON)
    release --program-id ID --run-id RUN --compute-seconds N
                             settle a job whose worker died and that will not
                             be re-run: charge N seconds, close its marker

``--pilot-dir`` overrides ``TREX_PILOT_DIR``. Exit 0 done, 2 refused (the
reason code on stderr; nothing written). Nothing here starts a job, sends an
order or authorizes live trading.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path

from tree_options.research.pilot import codes
from tree_options.research.pilot.store import (
    PilotError,
    PilotStore,
    owners_from_env,
    pilot_dir_from_env,
)
from tree_options.research.pilot.usage import UsageMeter
from tree_options.research.pilot.view import pilot_view
from tree_options.trex.contracts.acceptance import et_date
from tree_options.trex.contracts.canonical import model_document
from tree_options.trex.contracts.wave3 import registration_body_sha256

__all__ = ["main"]

REFUSED = 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m tree_options.research.pilot")
    parser.add_argument("--pilot-dir", type=Path, default=None)
    sub = parser.add_subparsers(dest="command", required=True)
    imp = sub.add_parser("import-drafts")
    imp.add_argument("directory", type=Path)
    sub.add_parser("list")
    seal = sub.add_parser("seal")
    seal.add_argument("--program-id", required=True)
    seal.add_argument("--sha256", required=True)
    seal.add_argument("--owner-email", required=True)
    usage = sub.add_parser("usage")
    usage.add_argument("--program-id", required=True)
    usage.add_argument("--day", type=date.fromisoformat, default=None)
    release = sub.add_parser("release")
    release.add_argument("--program-id", required=True)
    release.add_argument("--run-id", required=True)
    release.add_argument("--compute-seconds", type=int, required=True)
    return parser


def _print(document: object) -> None:
    print(json.dumps(document, indent=2, sort_keys=True))


def _refuse(code: str) -> int:
    print(f"refused: {code}", file=sys.stderr)
    return REFUSED


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = args.pilot_dir if args.pilot_dir is not None else pilot_dir_from_env()
    store, meter = PilotStore(root), UsageMeter(root)
    now = datetime.now(UTC)
    try:
        if args.command == "import-drafts":
            for program_id in store.import_drafts(args.directory):
                registration = store.state(program_id).registration
                assert registration is not None
                print(f"{program_id} draft {registration_body_sha256(registration)}")
            return 0
        if args.command == "list":
            try:
                owners = owners_from_env()
            except PilotError:
                owners = frozenset()
            _print(pilot_view(root, now=now, owners=owners))
            return 0
        if args.command == "seal":
            sealed = store.seal(
                args.program_id,
                reviewed_sha256=args.sha256,
                sealed_by=args.owner_email.strip().lower(),
                owners=owners_from_env(),
                at=now,
            )
            print(f"{sealed.program_id} sealed {sealed.sha256} by {sealed.sealed_by}")
            return 0
        if args.command == "usage":
            day = args.day if args.day is not None else et_date(now)
            reading = meter.read(args.program_id, day, now=now)
            _print(
                {
                    "program_id": args.program_id,
                    "day": day.isoformat(),
                    "usage": None if reading.usage is None else model_document(reading.usage),
                    "usage_unavailable_reason": reading.reason,
                    "jobs_in_flight": list(meter.jobs(args.program_id)),
                }
            )
            return 0
        if args.command == "release":
            with meter.lock(args.program_id):
                marker = meter.job(args.program_id, args.run_id)
                if marker is None:
                    return _refuse(codes.JOB_NOT_IN_FLIGHT)
                meter.charge(
                    args.program_id, marker.day, compute_seconds=args.compute_seconds, at=now
                )
                meter.close_job(args.program_id, args.run_id)
            print(f"{args.program_id} released {args.run_id}")
            return 0
    except PilotError as refusal:
        return _refuse(refusal.code)
    except (OSError, ValueError) as error:
        return _refuse(f"{codes.PILOT_CLI_FAILED}:{type(error).__name__.lower()}")
    raise AssertionError(f"unhandled command {args.command}")  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
