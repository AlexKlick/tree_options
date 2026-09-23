"""When and how loudly an operator alert goes out (both trex watchdogs).

Operator decision 2026-09-23:

* market hours with open positions (an NYSE session day, 09:00-16:15 ET:
  30 minutes before the open to the monitor's session end): priority high,
  a reminder every hour;
* quiet hours (default 22:00-07:00 America/Denver, ``QUIET_HOURS`` /
  ``OPERATOR_TZ`` in notify.env): nothing buzzes. An alarm still open when
  they end goes out then (07:00 MDT = 09:00 ET, before the open); a
  recovery waits too, and is only sent for an alarm that was delivered;
* otherwise: default priority, a reminder every 4 hours.

Self-healing never waits for daylight; only the pushes do.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, time
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from tree_options.trex.clock import ET, is_session

WATCH_OPEN = time(9, 0)  # ET, 30 minutes before the open
WATCH_CLOSE = time(16, 15)  # ET, the monitor's session end
REMIND_MARKET_S = 3600
REMIND_EVERY_S = 4 * 3600
NOTIFY_RETRY_S = 300  # a failed push is retried this often
DEFAULT_OPERATOR_TZ = "America/Denver"
DEFAULT_QUIET = "22:00-07:00"

_WINDOW = re.compile(r"^\s*(\d\d?):(\d\d)\s*-\s*(\d\d?):(\d\d)\s*$")


@dataclass(frozen=True)
class QuietHours:
    start: time
    end: time
    tz: ZoneInfo

    def contains(self, now: float) -> bool:
        t = datetime.fromtimestamp(now, self.tz).time()
        if self.start <= self.end:
            return self.start <= t < self.end
        return t >= self.start or t < self.end  # wraps midnight


@dataclass(frozen=True)
class Urgency:
    priority: str  # ntfy priority: "high" | "default"
    remind_every_s: int
    quiet: bool  # inside quiet hours: hold the push


@dataclass(frozen=True)
class Push:
    status: str  # the status reported ("recovered" for a recovery)
    title: str
    message: str
    priority: str


def _window(text: str) -> tuple[time, time] | None:
    m = _WINDOW.match(text)
    if not m:
        return None
    try:
        h1, m1, h2, m2 = (int(g) for g in m.groups())
        return time(h1, m1), time(h2, m2)
    except ValueError:
        return None


def load_quiet_hours(env: dict[str, str]) -> QuietHours | None:
    """From notify.env keys; ``QUIET_HOURS=off`` disables. Anything
    unparseable falls back to the default rather than to "never quiet"."""
    raw = env.get("QUIET_HOURS", "").strip()
    if raw.lower() == "off":
        return None
    window = _window(raw) or _window(DEFAULT_QUIET)
    assert window is not None
    try:
        tz = ZoneInfo(env.get("OPERATOR_TZ", "").strip() or DEFAULT_OPERATOR_TZ)
    except (ZoneInfoNotFoundError, ValueError):
        tz = ZoneInfo(DEFAULT_OPERATOR_TZ)
    return QuietHours(window[0], window[1], tz)


def market_hours(now: float) -> bool:
    dt = datetime.fromtimestamp(now, ET)
    return is_session(dt) and WATCH_OPEN <= dt.time() <= WATCH_CLOSE


def urgency(now: float, *, exposed: bool, quiet: QuietHours | None) -> Urgency:
    """Quiet hours hold every push, market alarms included: a configured
    window wins (the default one ends before the 09:00 ET watch opens)."""
    hush = quiet is not None and quiet.contains(now)
    if exposed and market_hours(now):
        return Urgency("high", REMIND_MARKET_S, quiet=hush)
    return Urgency("default", REMIND_EVERY_S, quiet=hush)


def et_label(epoch: float) -> str:
    """"Wed 14:05 ET" for push text."""
    return datetime.fromtimestamp(epoch, ET).strftime("%a %H:%M ET")


def span_label(seconds: float) -> str:
    m = int(seconds // 60)
    return f"{m // 60}h {m % 60}m" if m >= 60 else f"{m}m"


def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


def next_push(
    *,
    status: str,
    now: float,
    prior: dict[str, Any],
    urgency: Urgency,
    bad: frozenset[str],
    healthy: frozenset[str],
    alarm: Callable[[], tuple[str, str]],
    recovery: Callable[[], tuple[str, str]],
) -> tuple[Push | None, dict[str, Any]]:
    """The push due now (if any) and the notification bookkeeping to persist.

    ``last_notified_*`` only ever records a push handed to the transport;
    the caller reverts it with :func:`settle_push` if delivery fails.
    """
    last_status = prior.get("last_notified_status")
    last_at = _num(prior.get("last_notified_at"))
    failed_at = _num(prior.get("notify_failed_at"))
    book: dict[str, Any] = {
        "last_notified_status": last_status,
        "last_notified_at": last_at,
        "notify_failed_at": failed_at,
        "notify_held": False,
    }
    if failed_at is not None and now - failed_at < NOTIFY_RETRY_S:
        return None, book
    retry = failed_at is not None  # a failed push is still owed, whatever the cadence
    if status in bad:
        due = (
            retry
            or last_status != status
            or last_at is None
            or now - last_at >= urgency.remind_every_s
        )
        if not due:
            return None, book
        if urgency.quiet:
            book["notify_held"] = True
            return None, book
        title, message = alarm()
        book.update(last_notified_status=status, last_notified_at=now)
        return Push(status, title, message, urgency.priority), book
    if status in healthy and last_status in bad:
        if urgency.quiet:
            book["notify_held"] = True
            return None, book
        title, message = recovery()
        book.update(last_notified_status=status, last_notified_at=now)
        return Push("recovered", title, message, "default"), book
    book["notify_failed_at"] = None  # nothing owed any more
    return None, book


def settle_push(state: dict[str, Any], prior: dict[str, Any], delivered: bool, now: float) -> None:
    """Record a push outcome: a failed one is not "notified" and retries
    after NOTIFY_RETRY_S instead of waiting for the next reminder."""
    if delivered:
        state["notify_failed_at"] = None
        return
    state["last_notified_status"] = prior.get("last_notified_status")
    state["last_notified_at"] = prior.get("last_notified_at")
    state["notify_failed_at"] = now
