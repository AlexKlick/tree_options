#!/usr/bin/env python3
"""tsmom_annotate: the nightly TSMOM-SHIELD-001 annotation-stage runner.

Report-only overlay (docs/desk/TSMOM-SHIELD-001.md, sealed 2026-11-14):
writes DEF per panel name and per-entry would-be sizes for the entry names
passed EXPLICITLY to ``<TREX_DESK_STATE>/overlay/tsmom-shield/<D>.json``,
and with ``--digest`` the interim report-only digest at every 10th recorded
ENTRY. The sealer stays blind to overlay state (RT-3 C6b), the stage writes
nowhere else, and nothing here ever points a trade.

Deliberately NOT a ``tree_options.desk.__main__`` subcommand (that CLI file
belongs to lane L3): invoke it as::

    <venv>/python scripts/desk/tsmom_annotate.py --session D [--entries NAME ...] [--digest]

No timer unit is wired here -- deploying one is an owner-gated action.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date

from tree_options.desk import paths, tsmom_overlay
from tree_options.trex.clock import now_et, session_calendar


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tsmom_annotate",
        description="TSMOM-SHIELD-001 report-only overlay annotation (one session)",
    )
    parser.add_argument(
        "--session",
        required=True,
        type=date.fromisoformat,
        metavar="D",
        help="the NYSE session to annotate (required; no implicit clock)",
    )
    parser.add_argument(
        "--entries",
        nargs="+",
        default=[],
        metavar="NAME",
        help="entry names (per-name cards) to record would-be sizes for",
    )
    parser.add_argument(
        "--digest",
        action="store_true",
        help="emit the interim report-only digest at every 10th recorded ENTRY",
    )
    args = parser.parse_args(argv)
    res = tsmom_overlay.run_overlay(
        session=args.session,
        cal=session_calendar(),
        state=paths.state_root(),
        panel_path=paths.paper_dir() / "ohlc-panel.json",
        entries=args.entries,
        digest=args.digest,
        now=now_et(),
    )
    print(res.line())
    return res.exit_code


if __name__ == "__main__":
    sys.exit(main())
