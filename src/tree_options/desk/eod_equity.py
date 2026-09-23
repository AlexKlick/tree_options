"""eod-equity: the evening equity job that replaces the manual card chain.

The research lane's forward cards stopped on 2026-09-15 because a Claude
cron chain (CRON-paper-engine.md) produced them; the panel stopped on
09-14. This job is the timer-driven replacement for steps 3, 4 and the
signal half of step 6. For one session D (default: the latest session
whose 16:15 ET cutoff has passed) it:

1. no-ops on a non-session or when ``stages/<D>/eod-equity.done.json``
   exists (flushing any push still owed from quiet hours first);
2. extends the research panel through D by running the lane's own
   ``fetch_ohlc.py <S>`` for every session S after the panel's last one
   (a gapless full-tail extend; exit 3 = vendor lag, retried by the timer);
3. computes XSMOM-TOP3 and PEAD beats (:mod:`tree_options.desk.signals`)
   and writes ``signals/<D>.json``;
4. writes DRAFT cards into ``card-drafts/`` for whatever fired (sealing
   stays a human/agent step: nothing here touches the paper-trades cards
   or LEDGER.md);
5. pushes one ntfy line when a rule fires (no money, URLs or ids; quiet
   hours hold it for the next run), then marks the stage done.

State lives under :func:`tree_options.desk.paths.state_root`.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from tree_options.desk import card_drafts, signals
from tree_options.desk.sessions import (
    Calendar,
    cutoff_instant,
    latest_completed_session,
    sessions_after,
)
from tree_options.desk.store import atomic_write_bytes, atomic_write_json
from tree_options.desk.universe import PANEL_NAMES
from tree_options.trex.alert_policy import QuietHours

STAGE = "eod-equity"
SCHEMA = "desk-signals/1"
MAX_GAP_FILL = 25  # sessions; a longer gap needs a deliberate backfill
FETCH_TIMEOUT_S = 1200
OWED_TTL_S = 4 * 86_400  # an owed push older than this is dropped (covers a weekend)
PUSH_TITLE = "trex desk"
REFERENCE = "SPY"  # the panel's session clock (fetch_ohlc.py merges all names at once)

FetchRunner = Callable[[date], tuple[int, str]]
Notify = Callable[[str, str, str], bool]


@dataclass(frozen=True)
class EodResult:
    exit_code: int
    status: str
    detail: str = ""
    session: date | None = None
    signals_path: Path | None = None
    push: str = "none"  # none | sent | held_quiet | failed | unconfigured

    def line(self) -> str:
        d = self.session.isoformat() if self.session else "-"
        tail = f" ({self.detail})" if self.detail else ""
        return (
            f"eod-equity session={d} status={self.status} push={self.push} "
            f"exit={self.exit_code}{tail}"
        )


def subprocess_fetch_runner(
    paper: Path, repo: Path, log: Path, *, timeout_s: float = FETCH_TIMEOUT_S
) -> FetchRunner:
    """``<python> <paper>/fetch_ohlc.py <S>`` with cwd=repo, output appended
    to ``log``. The script writes the research panel itself."""

    def run(session: date) -> tuple[int, str]:
        cmd = [sys.executable, str(paper / "fetch_ohlc.py"), session.isoformat()]
        try:
            proc = subprocess.run(
                cmd, cwd=repo, capture_output=True, text=True, timeout=timeout_s, check=False
            )
            rc, out = proc.returncode, (proc.stdout or "") + (proc.stderr or "")
        except subprocess.TimeoutExpired:
            rc, out = 124, f"fetch_ohlc.py timed out after {timeout_s:.0f}s"
        except OSError as exc:
            rc, out = 127, f"fetch_ohlc.py could not start ({type(exc).__name__})"
        log.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).isoformat(timespec="seconds")
        with open(log, "a") as fh:
            fh.write(f"--- {stamp} fetch_ohlc.py {session.isoformat()} rc={rc}\n{out}\n")
        return rc, "\n".join(out.strip().splitlines()[-3:])

    return run


def _sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text())


def _last_session(panel: dict[str, Any]) -> date | None:
    bars = panel.get(REFERENCE) or {}
    return date.fromisoformat(max(bars)) if bars else None


def _panel_holes(panel: dict[str, Any], d: date, cal: Calendar) -> list[date]:
    bars = panel.get(REFERENCE) or {}
    sessions = cal.sessions()
    i = cal.ordinal(d)
    window = sessions[max(0, i - signals.XSMOM_LOOKBACK) : i + 1]
    return [s for s in window if s.isoformat() not in bars]


def _owed_dir(state: Path) -> Path:
    return state / "push-owed"


def flush_owed(state: Path, now: datetime, notify: Notify | None, quiet: QuietHours | None) -> None:
    """Deliver pushes held by quiet hours or failed delivery (oldest first)."""
    folder = _owed_dir(state)
    if not folder.is_dir():
        return
    t = now.timestamp()
    for path in sorted(folder.glob("*.json")):
        try:
            owed = _load_json(path)
            age = t - float(owed["created_at"])
            title, message = str(owed["title"]), str(owed["message"])
        except (OSError, ValueError, KeyError, TypeError):
            path.unlink(missing_ok=True)
            continue
        if age > OWED_TTL_S:
            path.unlink(missing_ok=True)
            continue
        if notify is None or (quiet is not None and quiet.contains(t)):
            continue
        if notify(title, message, str(owed.get("priority", "default"))):
            path.unlink(missing_ok=True)


def _push_message(xsmom_fires: bool, n_beats: int, session: date) -> str | None:
    parts = []
    if xsmom_fires:
        parts.append("XSMOM rebalance")
    if n_beats:
        parts.append(f"PEAD beat: {n_beats} name{'' if n_beats == 1 else 's'}")
    if not parts:
        return None
    return f"{'; '.join(parts)} for session {session.isoformat()}. Draft card(s) ready for review."


def _deliver(
    state: Path,
    session: date,
    message: str,
    now: datetime,
    notify: Notify | None,
    quiet: QuietHours | None,
) -> str:
    if notify is None:
        return "unconfigured"
    if quiet is not None and quiet.contains(now.timestamp()):
        status = "held_quiet"
    elif notify(PUSH_TITLE, message, "default"):
        return "sent"
    else:
        status = "failed"
    atomic_write_json(
        _owed_dir(state) / f"{session.isoformat()}-{STAGE}.json",
        {
            "session": session.isoformat(),
            "title": PUSH_TITLE,
            "message": message,
            "priority": "default",
            "created_at": now.timestamp(),
            "reason": status,
        },
    )
    return status


def _exit_session(session: date, cal: Calendar) -> date | None:
    try:
        return cal.nth_after(session, signals.HOLD_SESSIONS)
    except (KeyError, ValueError, LookupError, RuntimeError):
        return None  # past the calendar's horizon


def run_eod_equity(
    *,
    session: date | None,
    now: datetime,
    cal: Calendar,
    state: Path,
    paper: Path,
    fetch: FetchRunner,
    notify: Notify | None,
    quiet: QuietHours | None,
    dry_run: bool = False,
) -> EodResult:
    if session is None:
        d = latest_completed_session(now, cal)
    elif not cal.is_session(session):
        return EodResult(0, "not_a_session", f"{session.isoformat()} is not an NYSE session")
    elif now < cutoff_instant(session):
        return EodResult(3, "too_early", "before the session's 16:15 ET cutoff", session)
    else:
        d = session

    if not dry_run:
        flush_owed(state, now, notify, quiet)
    marker = state / "stages" / d.isoformat() / f"{STAGE}.done.json"
    if marker.exists():
        return EodResult(0, "already_done", "", d)

    panel_path = paper / "ohlc-panel.json"
    try:
        panel = _load_json(panel_path)
    except (OSError, ValueError) as exc:
        return EodResult(1, "panel_missing", f"{panel_path.name}: {type(exc).__name__}", d)
    last = _last_session(panel)
    if last is None:
        return EodResult(1, "panel_missing", f"no {REFERENCE} bars in the panel", d)
    todo = sessions_after(last, d, cal)
    if len(todo) > MAX_GAP_FILL:
        return EodResult(
            1,
            "gap_too_large",
            f"panel ends {last}; {len(todo)} sessions to {d} (max {MAX_GAP_FILL}): backfill first",
            d,
        )
    if dry_run:
        plan = (
            f"would fetch {todo[0]}..{todo[-1]} ({len(todo)} sessions)"
            if todo
            else "panel already has the session"
        )
        return EodResult(0, "dry_run", f"panel ends {last}; {plan}", d)

    for s in todo:
        rc, tail = fetch(s)
        if rc == 3:
            return EodResult(3, "vendor_lag", f"fetch_ohlc.py {s}: {tail}", d)
        if rc != 0:
            return EodResult(1, "fetch_failed", f"fetch_ohlc.py {s} rc={rc}: {tail}", d)

    try:
        panel = _load_json(panel_path)
    except (OSError, ValueError) as exc:
        return EodResult(1, "panel_missing", f"after fetch: {type(exc).__name__}", d)
    short = [n for n in PANEL_NAMES if d.isoformat() not in (panel.get(n) or {})]
    if short:
        return EodResult(
            1, "panel_incomplete", f"no {d} bar for {len(short)} names ({', '.join(short[:5])})", d
        )

    cal_path = paper / "earnings-calendar.json"
    try:
        earnings: dict[str, list[str]] = _load_json(cal_path)
        pead_note = ""
    except (OSError, ValueError) as exc:
        earnings, pead_note = {}, f"earnings calendar unreadable ({type(exc).__name__})"

    xs = signals.xsmom_top3(panel, d, cal)
    pe = signals.pead_beats(panel, earnings, d, cal)
    beats, evaluated = signals.pead_doc(pe)
    exit_s = _exit_session(d, cal)
    generated = now.astimezone(UTC)

    drafts: list[str] = []
    if xs.fires:
        name = f"{d.isoformat()}-xsmom-top3.md"
        text = card_drafts.xsmom_draft(
            xs, panel=panel, earnings=earnings, exit_session=exit_s, generated_at=generated
        )
        atomic_write_bytes(state / "card-drafts" / name, text.encode("ascii"))
        drafts.append(name)
    for event in pe.beats:
        name = f"{d.isoformat()}-pead-{event.name.lower()}.md"
        text = card_drafts.pead_draft(
            event, panel=panel, exit_session=exit_s, generated_at=generated
        )
        atomic_write_bytes(state / "card-drafts" / name, text.encode("ascii"))
        drafts.append(name)

    holes = _panel_holes(panel, d, cal)
    now_last = _last_session(panel)
    signals_path = state / "signals" / f"{d.isoformat()}.json"
    atomic_write_json(
        signals_path,
        {
            "schema": SCHEMA,
            "session": d.isoformat(),
            "xsmom": signals.xsmom_doc(xs),
            "pead": beats,
            "pead_evaluated": evaluated,
            "panel_last_session": now_last.isoformat() if now_last else None,
            "drafts": drafts,
            "provenance": {
                "generated_at": generated.isoformat(),
                "code": "tree_options.desk.signals",
                "panel": str(panel_path),
                "panel_sha256": _sha256(panel_path),
                "panel_last_before": last.isoformat(),
                "fetched_sessions": [s.isoformat() for s in todo],
                # NYSE sessions inside the XSMOM look-back missing from the
                # panel's session clock (a gap-free panel has none)
                "panel_holes": [s.isoformat() for s in holes[:20]],
                "panel_holes_n": len(holes),
                "earnings_calendar": str(cal_path),
                "earnings_calendar_sha256": _sha256(cal_path),
                "earnings_note": pead_note,
                "exit_session": exit_s.isoformat() if exit_s else None,
                "allowed_direction": sorted(signals.ALLOWED_DIRECTION),
            },
        },
    )

    message = _push_message(xs.fires, len(pe.beats), d)
    push = _deliver(state, d, message, now, notify, quiet) if message else "none"
    atomic_write_json(
        marker,
        {
            "session": d.isoformat(),
            "stage": STAGE,
            "done_at": generated.isoformat(),
            "signals": str(signals_path),
            "fired": {"xsmom": xs.fires, "pead_beats": [e.name for e in pe.beats]},
            "drafts": drafts,
            "push": push,
        },
    )
    return EodResult(0, "ok", "", d, signals_path, push)
