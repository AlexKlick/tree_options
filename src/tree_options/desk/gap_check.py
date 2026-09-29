"""The recorder-integrity alarm: is the most recent CLOSED desk session
actually on disk?

A lost chain session is permanent. ``store.finalize_prior`` turns whatever a
session still lacks when the next session's recorder run starts into a
``missing`` manifest entry and a ``gaps.jsonl`` row, and nothing ever repairs
it: ``store.validate`` refuses a past session, and ``regime.vol_state()``
needs ``min_history=120`` chain-recorded sessions. 2026-09-23 was lost this
way (37 symbols) and the 38 ``gaps.jsonl`` rows it wrote sat unread from
2026-09-24 until 2026-09-29 -- a loss is discovered only by writing a line
nobody checks. This is the line somebody checks.

What is checked, and why not the newest session: the manifest of the latest
completed session is still OPEN -- the chain recorder is mid-session, and
``stale`` / ``incomplete`` there mean "CBOE has not published yet", the
normal state of a young manifest, which would alarm every morning. Books
close on D only once a later session exists (that run's ``finalize_prior``
writes the verdict), so the check reads the newest session strictly before
the latest completed one and treats anything outside
``ok | exists | conflict`` there -- or a missing manifest at all -- as
permanent.

The push goes out through :mod:`tree_options.trex.notify` under
:mod:`tree_options.trex.alert_policy`: held in quiet hours, delivered when
they end, re-alarmed on a changed gap set and daily while one persists, plus
one recovery when the manifest goes clean. Clean runs are silent.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from tree_options.desk import paths, store
from tree_options.desk.sessions import Calendar, latest_completed_session
from tree_options.trex.alert_policy import (
    REMIND_EVERY_S,
    Urgency,
    load_quiet_hours,
    next_push,
    settle_push,
    urgency,
)
from tree_options.trex.clock import now_et, session_calendar
from tree_options.trex.notify import load_config, read_env, send

STATE_FILE = "gap-check.json"
MAX_NAMED = 8  # symbols named in the push; the rest are counted, not listed
HEALTHY = "clean"
GAPS = "gaps"
UNREADABLE = "unreadable_manifest"
BAD = frozenset({GAPS, UNREADABLE})
# without a computed urgency (tests, --dry-run callers): loud, 4 h reminders
LOUD = Urgency("high", REMIND_EVERY_S, quiet=False)


@dataclass(frozen=True)
class GapScan:
    """What the newest closed session's manifest holds."""

    session: date | None  # None = no closed session yet (nothing to check)
    symbols: tuple[str, ...] = ()  # manifest symbols outside RECORDED
    unreadable: bool = False  # True = that session has no readable manifest

    @property
    def clean(self) -> bool:
        """True when there is nothing permanent to report -- including the
        case where no session has closed yet, which is a fresh install and
        not a loss."""
        return not self.symbols and not self.unreadable

    @property
    def status(self) -> str:
        """The alert-policy status: one of the three stable values, so the
        shared cadence (and its recovery ping) applies unchanged. Which
        session and which symbols belong in :func:`alarm_text`."""
        if self.session is None or (not self.symbols and not self.unreadable):
            return HEALTHY
        return UNREADABLE if self.unreadable else GAPS


def closed_sessions(cal: Calendar, now: datetime) -> list[date]:
    """Sessions whose books are closed, oldest first.

    ``latest_completed_session`` is the session the recorder is working on
    right now: its manifest is open, so it is deliberately excluded. A
    calendar with no completed session yet (a fresh install) has nothing
    closed and nothing to say.
    """
    try:
        latest = latest_completed_session(now, cal)
    except ValueError:
        return []
    return [s for s in cal.sessions() if s < latest]


def scan(chain_store: store.ChainStore, cal: Calendar, now: datetime) -> GapScan:
    """The newest closed session's unrecorded symbols. Pure: it reads the
    manifest directory only -- no network, no chains, no state lock."""
    closed = closed_sessions(cal, now)
    if not closed:
        return GapScan(None)
    target = closed[-1]
    doc = chain_store.read_manifest(target)
    if doc is None:
        return GapScan(target, unreadable=True)
    pending = sorted(
        str(sym)
        for sym, entry in doc["symbols"].items()
        if not (isinstance(entry, dict) and entry.get("status") in store.RECORDED)
    )
    return GapScan(target, tuple(pending))


def _namer(symbols: tuple[str, ...]) -> str:
    shown = ", ".join(symbols[:MAX_NAMED])
    rest = len(symbols) - MAX_NAMED
    return f"{shown} (+{rest} more)" if rest > 0 else shown


def alarm_text(gap: GapScan) -> tuple[str, str]:
    """(title, body) for the push: the session and the symbols, no URLs,
    hostnames, account ids or money."""
    if gap.unreadable:
        return (
            "trex: desk session has no manifest",
            f"Session {gap.session} is closed but its chain manifest is missing or "
            f"unreadable: no recorder run ever closed its books. A closed session is "
            "never repaired.",
        )
    return (
        f"trex: desk session {gap.session} has {len(gap.symbols)} unrecorded symbols",
        f"Session {gap.session} closed with {len(gap.symbols)} symbols never recorded: "
        f"{_namer(gap.symbols)}. A closed session is never repaired; the per-symbol "
        "detail is in the store's gaps.jsonl.",
    )


def recovery_text(gap: GapScan) -> tuple[str, str]:
    return (
        f"trex: desk session {gap.session} is complete",
        f"Every symbol of session {gap.session} is recorded; the chain gap is closed.",
    )


def _read_state(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state, indent=1))
    os.replace(tmp, path)


def check_once(
    state_path: Path | None,
    chain_store: store.ChainStore,
    *,
    cal: Calendar,
    now: datetime,
    notify: Callable[[str, str, str], bool] | None = None,
    urg: Urgency = LOUD,
    dry_run: bool = False,
) -> tuple[GapScan, int]:
    """Scan, push if there is something permanent to say, return (scan, rc).

    Exit 0 when the newest closed session is complete or there is nothing to
    check yet; 1 when it holds a gap, so the timer shows a failed job on top
    of the push. A push that is not actually sent is never booked as sent
    (the same rule gateway_watch follows), so a dry run or an unconfigured
    notify owes the push to the next slot instead of swallowing it.
    """
    gap = scan(chain_store, cal, now)
    prior = _read_state(state_path) if state_path is not None else {}
    push, book = next_push(
        status=gap.status,
        now=now.timestamp(),
        prior=prior,
        urgency=urg,
        bad=BAD,
        healthy=frozenset({HEALTHY}),
        alarm=lambda: alarm_text(gap),
        recovery=lambda: recovery_text(gap),
    )
    if push is not None and (notify is None or dry_run):
        book["last_notified_status"] = prior.get("last_notified_status")
        book["last_notified_at"] = prior.get("last_notified_at")
        book["notify_failed_at"] = now.timestamp()
    elif push is not None and notify is not None:
        settle_push(
            book, prior, notify(push.title, push.message, push.priority), now.timestamp()
        )
    if state_path is not None and not dry_run:
        _write_state(
            state_path,
            {
                "checked_at": now.isoformat(),
                "session": gap.session.isoformat() if gap.session else None,
                "status": gap.status,
                **book,
            },
        )
    return gap, (0 if gap.clean else 1)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--state", type=Path, default=None,
                        help=f"default: <TREX_DESK_STATE>/{STATE_FILE}")
    parser.add_argument("--dry-run", action="store_true",
                        help="report and change nothing, send nothing")
    args = parser.parse_args(argv)

    env_path = paths.notify_env_path()
    now = now_et()
    gap, rc = check_once(
        args.state if args.state is not None else paths.state_root() / STATE_FILE,
        store.ChainStore(paths.store_root()),
        cal=session_calendar(),
        now=now,
        notify=lambda t, m, p: send(load_config(env_path), t, m, p),
        urg=urgency(
            now.timestamp(), exposed=False, quiet=load_quiet_hours(read_env(env_path))
        ),
        dry_run=args.dry_run,
    )
    print(
        json.dumps(
            {
                "session": gap.session.isoformat() if gap.session else None,
                "status": gap.status,
                "unrecorded": len(gap.symbols),
                "checked_at": now.isoformat(),
            }
        )
    )
    if not gap.clean:
        title, body = alarm_text(gap)
        print(f"{title}: {body}", file=sys.stderr)
    return rc


if __name__ == "__main__":
    sys.exit(main())
