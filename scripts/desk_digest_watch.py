#!/usr/bin/env python
"""Nightly challenge-digest watcher — journalctl-visible truth, nothing else.

Read-only. No notifications, no network, no writes: the ops loop reads this
job's journal. Runs at 19:30 America/Denver (deploy/desk/desk-digest-watch.timer),
half an hour after the 19:00 Denver challenge game, and answers four questions
about the game that should have just landed under ``<DESK_STORE>/evaluations/
challenge/``:

1. FRESHNESS — a digest exists stamped at or after 19:00 America/Denver today.
   The game's digest carries the NEXT UTC day's 01:00Z stamp (19:00 MDT),
   so "today's game" cannot be named from a UTC calendar; the cutoff is
   computed from the CURRENT time in ET instead: 21:00 ET on today's ET
   date, which is always the same instant as 19:00 America/Denver (every
   US zone switches DST on the same dates; the ET-Denver offset is a
   constant two hours).
2. STATUS — that digest's ``digest.json`` says ``status == "ok"`` (a
   ``quota_dry`` or ``empty_bundles`` game is an honest result but NOT the
   nightly game this watch exists to confirm).
3. STANDINGS — ``standings.json`` mtime is NEWER than the digest: proof the
   service's ExecStartPost standings rebuild ran after this game, not before
   it (a stale standings file silently freezes every promotion-rule clause).
4. MODEL FAILURES — ``model_failures`` summed across the digest's
   scorecards is zero. Non-zero is a WARNING, never a failure: the digest
   already recorded the failures; this line only makes them impossible to
   miss.

Exit codes (contract):
  0  every check passed (warnings allowed)
  1  any check failed

Usage:
  python scripts/desk_digest_watch.py            # DESK_STORE env or default
  python scripts/desk_digest_watch.py --store /path/to/desk-store
  python scripts/desk_digest_watch.py --now 2026-10-02T01:30:00+00:00
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT / "src") not in sys.path:  # pragma: no cover - import plumbing
    sys.path.insert(0, str(REPO_ROOT / "src"))

from tree_options.desk import paths  # noqa: E402

#: the desk's clock tier: the challenge timer and this watch are scheduled in
#: America/Denver, the desk day is narrated in ET — one rule in both zones
ET = ZoneInfo("America/New_York")
DENVER = ZoneInfo("America/Denver")
#: 19:00 America/Denver == 21:00 America/New_York (constant two-hour offset)
GAME_HOUR_ET = time(21, 0)
#: digest directory names are UTC stamps written by the challenge run
STAMP_RE = re.compile(r"^(\d{8}T\d{6}Z)(?:-\d+)?$")


@dataclass(frozen=True)
class WatchResult:
    """What the watch found: the exit code plus every journald line."""

    failures: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    digest_id: str | None = None
    digest_status: str | None = None
    model_failures: int | None = None
    standings_fresh: bool | None = None
    lines: tuple[str, ...] = field(default=())

    @property
    def exit_code(self) -> int:
        return 1 if self.failures else 0


def game_cutoff_utc(now: datetime) -> datetime:
    """19:00 America/Denver TODAY (the challenge game's slot), in UTC.

    "Today" is read from the current time in ET — robust against the game's
    own 01:00Z-next-day stamp — and 21:00 ET is the same instant as 19:00
    Denver every day of the year (US DST transitions are simultaneous).
    """
    now_et = now.astimezone(ET)
    et_date = now_et.date()
    cutoff_et = datetime.combine(et_date, GAME_HOUR_ET, tzinfo=ET)
    cutoff_denver = datetime.combine(et_date, time(19, 0), tzinfo=DENVER)
    if cutoff_et.astimezone(DENVER) != cutoff_denver:  # pragma: no cover - guard
        raise AssertionError("ET/Denver offset is no longer two hours")
    return cutoff_et.astimezone(UTC)


def _stamp_utc(name: str) -> datetime | None:
    """The UTC instant a digest directory name encodes (``-N`` suffixes are
    collision counters, not a different time)."""
    match = STAMP_RE.match(name)
    if not match:
        return None
    return datetime.strptime(match.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)


def _model_failures(document: dict) -> int:
    """Sum ``model_failures`` over every round's every scorecard."""
    total = 0
    for rnd in document.get("rounds", []) or []:
        for card in rnd.get("scorecards", []) or []:
            total += int(card.get("model_failures", 0) or 0)
    return total


def check_challenge_digest(store_root: Path, now: datetime) -> WatchResult:
    """Run the four checks against one desk store. Pure: reads, prints
    nothing, writes nothing — ``main`` owns the journald lines."""
    failures: list[str] = []
    warnings: list[str] = []
    digest_id: str | None = None
    digest_status: str | None = None
    model_failures: int | None = None
    standings_fresh: bool | None = None

    base = Path(store_root) / "evaluations" / "challenge"
    cutoff = game_cutoff_utc(now)

    # -- 1. freshness: the newest digest must be stamped after the cutoff ----
    stamped = sorted(
        (stamp, path)
        for path in (base.glob("*") if base.is_dir() else [])
        if (stamp := _stamp_utc(path.name)) is not None and path.is_dir()
    )
    if not stamped:
        failures.append(
            f"digest-missing: no digest directory under {base} "
            f"(wanted one stamped at/after {cutoff.isoformat()}, 19:00 America/Denver today)"
        )
    else:
        newest_stamp, newest = stamped[-1]
        digest_id = newest.name
        # at-or-after, not strictly newer: the on-time game IS the 19:00:00
        # Denver slot and its stamp is exactly 01:00:00Z — only a digest from
        # a PREVIOUS night (yesterday's 01:00Z) is stale
        if newest_stamp < cutoff:
            failures.append(
                f"digest-stale: newest digest {digest_id} is older than "
                f"{cutoff.isoformat()} (19:00 America/Denver today): no game ran tonight"
            )

    # -- 2. the digest itself says the game executed --------------------------
    document: dict | None = None
    digest_path = newest / "digest.json" if digest_id else None
    if digest_path is not None:
        if not digest_path.exists():
            failures.append(f"digest-unreadable: {digest_path} does not exist")
        else:
            try:
                document = json.loads(digest_path.read_text())
            except ValueError as error:
                failures.append(f"digest-unreadable: {digest_path} is not JSON ({error})")
    if document is not None:
        digest_status = str(document.get("status", "") or "<absent>")
        if digest_status != "ok":
            failures.append(
                f"digest-status: {digest_id} status is {digest_status!r}, not 'ok' "
                "(quota_dry/empty_bundles games are honest but are not the nightly game)"
            )

    # -- 3. the ExecStartPost standings rebuild ran AFTER this digest ----------
    if digest_path is not None and digest_path.exists():
        standings_path = base / "standings.json"
        if not standings_path.exists():
            failures.append(
                f"standings-missing: {standings_path} absent — the challenge "
                "service's ExecStartPost standings rebuild did not run (or wrote nothing)"
            )
        else:
            standings_fresh = standings_path.stat().st_mtime > digest_path.stat().st_mtime
            if not standings_fresh:
                failures.append(
                    f"standings-stale: {standings_path} mtime "
                    f"{datetime.fromtimestamp(standings_path.stat().st_mtime, UTC).isoformat()} "
                    f"is not newer than the digest {digest_id} (mtime "
                    f"{datetime.fromtimestamp(digest_path.stat().st_mtime, UTC).isoformat()}) "
                    "— standings were not rebuilt after tonight's game"
                )

    # -- 4. model failures: warned, never failed ------------------------------
    if document is not None:
        model_failures = _model_failures(document)
        if model_failures:
            warnings.append(
                f"model-failures: {model_failures} model call failure(s) across "
                f"{digest_id} scorecards (warning only; the digest already records them)"
            )

    if digest_id is None:
        summary = (
            f"summary: FAIL no digest in {base} (cutoff {cutoff.isoformat()}) "
            f"failed={len(failures)} warned={len(warnings)}"
        )
    else:
        summary = (
            f"summary: {'FAIL' if failures else 'OK'} digest={digest_id} "
            f"status={digest_status if digest_status is not None else '-'} "
            f"model_failures={model_failures if model_failures is not None else '-'} "
            f"standings_fresh={'-' if standings_fresh is None else str(standings_fresh).lower()}"
        )
        if failures:
            summary += f" failed={len(failures)} warned={len(warnings)}"
    lines = (
        *(f"FAIL {line}" for line in failures),
        *(f"WARN {line}" for line in warnings),
        summary,
    )
    return WatchResult(
        failures=tuple(failures),
        warnings=tuple(warnings),
        digest_id=digest_id,
        digest_status=digest_status,
        model_failures=model_failures,
        standings_fresh=standings_fresh,
        lines=tuple(lines),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--store",
        type=Path,
        default=None,
        help="desk store root (default: DESK_STORE env, else <repo>/artifacts/desk-store)",
    )
    parser.add_argument(
        "--now",
        type=datetime.fromisoformat,
        default=None,
        help="treat this instant as now, ISO-8601 with timezone (default: current time)",
    )
    args = parser.parse_args(argv)
    store = args.store if args.store else paths.store_root()
    now = args.now if args.now else datetime.now(UTC)
    if now.tzinfo is None:
        parser.error("--now needs a timezone (e.g. 2026-10-02T01:30:00+00:00)")
    result = check_challenge_digest(store, now)
    for line in result.lines:
        print(f"desk-digest-watch: {line}")
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
