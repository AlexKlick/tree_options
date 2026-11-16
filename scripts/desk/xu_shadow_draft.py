#!/usr/bin/env python3
"""On-demand runner for the XU shadow drafter (XU-SUCCESSOR-001 §6 item 2).

Drafts the monthly XU-62 card for one session into the shadow book's own
drafts namespace (``TREX_DESK_STATE/card-drafts-xu/``). No timer, no unit
file, no push: the shadow book is digest-only (§4.3) and opens at the first
monthly seal after the §6 plumbing lands (§4.2) - this runner exists so a
human or agent can draft a month on demand.

Usage:
    uv run --frozen python scripts/desk/xu_shadow_draft.py --session 2026-12-01

Exit codes: 0 drafted (or an honest no-op: not a session / not a rebalance
day); 3 the XU panel does not cover the session (extend it with
scripts/desk/fetch_ohlc_xu.py first) - nothing written; 1 anything else.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, date, datetime
from pathlib import Path

from tree_options.desk import signals, xu_shadow
from tree_options.desk.panel import read_panel
from tree_options.desk.paths import paper_dir, state_root
from tree_options.trex.clock import session_calendar


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--session", required=True, help="session date YYYY-MM-DD")
    ap.add_argument("--panel", help="XU panel path (default: <paper>/ohlc-panel-xu.json)")
    ap.add_argument("--state", help="state root (default: TREX_DESK_STATE)")
    args = ap.parse_args(argv)

    session = date.fromisoformat(args.session)
    cal = session_calendar()
    if not cal.is_session(session):
        print(f"not a session: {session.isoformat()}")
        return 0
    panel_path = Path(args.panel) if args.panel else paper_dir() / xu_shadow.XU_PANEL_NAME
    state = Path(args.state) if args.state else state_root()

    panel = read_panel(panel_path)
    try:
        exit_session = cal.nth_after(session, signals.HOLD_SESSIONS)
    except (KeyError, ValueError, LookupError, RuntimeError):
        exit_session = None  # past the calendar horizon: the draft says so
    try:
        text = xu_shadow.xu_draft(
            panel, session, cal, exit_session=exit_session, generated_at=datetime.now(UTC)
        )
    except xu_shadow.XuCoverageError as exc:
        print(f"REFUSED (no write): the XU panel does not cover {session.isoformat()}: {exc}")
        return 3
    if text is None:
        print(f"no draft: {session.isoformat()} is not the first session of a month")
        return 0
    path = xu_shadow.write_draft(state, session, text)
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
