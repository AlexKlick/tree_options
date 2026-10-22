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

Wave 4 (lane J) adds two things:

* SAFETY alerts (``SAFETY_ALERTS``): an exit may be blocked or blind until
  the owner looks. Quiet hours never hold them (``next_push``, every
  watchdog using it) and they go out at ``high`` or louder; their
  recoveries still wait for the morning;
* the owner alert catalogue (``WAVE4_ALERTS``), its pure detectors and the
  plan ``trex.alert_sweep`` follows: one push per occurrence, reminders only
  for safety alerts that last, a failed push owed and retried after
  ``NOTIFY_RETRY_S``, and a source that cannot be read never resolves an
  alert it cannot see.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Final
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

#: Alarms (statuses or alert kinds) where an exit may be blocked or blind
#: until the owner looks. Quiet hours never hold them. ``quote_blind`` is
#: trex.exit_watch's name for the exit_quote_blind hazard as seen in the
#: desk's monitor.json (a held equity without a real-time quote).
SAFETY_ALERTS: Final[frozenset[str]] = frozenset(
    {"reconciliation_required", "exit_quote_blind", "quote_blind"}
)
#: a safety alarm is never pushed below this ntfy priority
SAFETY_PRIORITY_FLOOR: Final = "high"
_PRIORITY_RANK: Final = {"min": 0, "low": 1, "default": 2, "high": 3, "urgent": 4, "max": 4}


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
    """ "Wed 14:05 ET" for push text."""
    return datetime.fromtimestamp(epoch, ET).strftime("%a %H:%M ET")


def span_label(seconds: float) -> str:
    m = int(seconds // 60)
    return f"{m // 60}h {m % 60}m" if m >= 60 else f"{m}m"


def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


def at_least(priority: str, floor: str) -> str:
    """The louder of two ntfy priorities (an unknown name ranks as default)."""
    rank = _PRIORITY_RANK.get(priority, 2)
    return priority if rank >= _PRIORITY_RANK.get(floor, 2) else floor


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
    the caller reverts it with :func:`settle_push` if delivery fails. A
    safety alarm (``SAFETY_ALERTS``) is never held for quiet hours and goes
    out at ``SAFETY_PRIORITY_FLOOR`` or louder; its recovery still waits.
    """
    safety = status in SAFETY_ALERTS
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
        if urgency.quiet and not safety:
            book["notify_held"] = True
            return None, book
        title, message = alarm()
        book.update(last_notified_status=status, last_notified_at=now)
        priority = at_least(urgency.priority, SAFETY_PRIORITY_FLOOR) if safety else urgency.priority
        return Push(status, title, message, priority), book
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


# =============================================================================
# Wave 4 owner alerts (lane J)
# =============================================================================


class AlertKind(StrEnum):
    RECONCILIATION_REQUIRED = "reconciliation_required"
    APPROVAL_EXPIRING = "approval_expiring"
    ACCEPTANCE_STREAK_BROKEN = "acceptance_streak_broken"
    EXIT_QUOTE_BLIND = "exit_quote_blind"
    COLD_RESTART_MISSED = "cold_restart_missed"


@dataclass(frozen=True)
class AlertSpec:
    safety: bool  # never held for quiet hours
    priority: str  # ntfy priority
    remind: bool  # reminders while it lasts; otherwise once per occurrence
    persist_s: int  # it must stand this long before the first push
    sender: str  # the one module that pushes it


SWEEP: Final = "trex.alert_sweep"
#: an approval is announced once inside its last minute
APPROVAL_WARN_S: Final = 60
#: two books cycles (desk_books.STATUS_INTERVAL_S): a fill booked one cycle
#: late is not an alarm; the desk's own event push stays immediate
RECONCILIATION_PERSIST_S: Final = 120
#: a streak break older than this many sessions (counted back from the last
#: closed one) is history, not news
STREAK_BREAK_RECENT_SESSIONS: Final = 5
#: acceptance's REQUIRED_STREAK_SESSIONS (pinned by a test; no contract import
#: here, every watchdog loads this module)
STREAK_REQUIRED: Final = 10

WAVE4_ALERTS: Final[Mapping[AlertKind, AlertSpec]] = MappingProxyType(
    {
        AlertKind.RECONCILIATION_REQUIRED: AlertSpec(
            safety=True,
            priority="urgent",
            remind=True,
            persist_s=RECONCILIATION_PERSIST_S,
            sender=SWEEP,
        ),
        AlertKind.APPROVAL_EXPIRING: AlertSpec(
            safety=False, priority="high", remind=False, persist_s=0, sender=SWEEP
        ),
        AlertKind.ACCEPTANCE_STREAK_BROKEN: AlertSpec(
            safety=False, priority="default", remind=False, persist_s=0, sender=SWEEP
        ),
        AlertKind.EXIT_QUOTE_BLIND: AlertSpec(
            safety=True, priority="urgent", remind=False, persist_s=0, sender=SWEEP
        ),
        # Wave 1: trex.gateway_watch sends it (held in quiet hours); one sender
        AlertKind.COLD_RESTART_MISSED: AlertSpec(
            safety=False, priority="default", remind=False, persist_s=0, sender="trex.gateway_watch"
        ),
    }
)

#: acceptance session reasons in the owner's words (pinned to
#: contracts.acceptance.SESSION_REASONS by a test)
SESSION_REASON_WORDS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "session_not_recorded": "session not recorded",
        "heartbeat_unavailable": "desk heartbeat unknown",
        "heartbeat_minutes_exceed_session": "heartbeat record inconsistent",
        "heartbeat_below_99pct": "desk heartbeat under 99% of the session",
        "reconciliation_unavailable": "reconciliation unknown",
        "reconciliation_not_clean_at_close": "books not reconciled at the close",
        "reconciliation_open_unresolved": "opening reconciliation not resolved",
        "reconciliation_unresolved_in_session": "a reconciliation stayed open past the close",
        "unowned_orders_unavailable": "unowned orders unknown",
        "unowned_orders_present": "an order TREX does not own",
        "exit_guard_unavailable": "exit guard unknown",
        "exit_unguarded_over_5min": "a position without an exit owner for over 5 minutes",
    }
)
_MISMATCH_WORDS: Final = MappingProxyType(
    {
        "missing": "in the books only",
        "unexplained": "at the broker only",
        "quantity_mismatch": "quantity differs",
    }
)
_SYMBOL = re.compile(r"^[A-Z0-9.\- ]{1,16}$")
_WORD = re.compile(r"^[a-z_]{1,20}$")


@dataclass(frozen=True)
class Alert:
    """One occurrence of a Wave 4 alert. ``key`` identifies the occurrence
    (dedupe, kept in the book); it never reaches the push text."""

    kind: AlertKind
    key: str
    title: str
    message: str

    @property
    def ident(self) -> str:
        return f"{self.kind.value}:{self.key}"


@dataclass(frozen=True)
class AlertPush:
    ident: str
    kind: AlertKind
    title: str
    message: str
    priority: str


@dataclass(frozen=True)
class PendingApproval:
    """A proposal waiting for the owner (``GET /api/proposals`` "pending")."""

    proposal_id: str
    label: str  # "SPY equity": symbol and asset kind, nothing else
    send_deadline: float  # epoch seconds


@dataclass(frozen=True)
class SessionOutcome:
    """One acceptance ``session_results`` row."""

    session: date
    qualifying: bool
    reasons: tuple[str, ...]


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def reconciliation_alert(
    status: str | None, mismatch_kinds: Sequence[str], as_of: float | None
) -> Alert | None:
    """The books disagree with the broker (``trex-reconciliation/1``)."""
    if status != "reconciliation_required":
        return None
    counts: dict[str, int] = {}
    for kind in mismatch_kinds:
        counts[kind] = counts.get(kind, 0) + 1
    parts = ", ".join(f"{n} {_MISMATCH_WORDS.get(k, k)}" for k, n in sorted(counts.items()))
    when = f" as of {et_label(as_of)}" if as_of is not None else ""
    detail = f" ({parts})" if parts else ""
    return Alert(
        AlertKind.RECONCILIATION_REQUIRED,
        "books",
        "trex: reconciliation required",
        f"The desk's books and the broker disagree on "
        f"{_plural(len(mismatch_kinds), 'position')}{detail}{when}. New entries stay "
        "blocked until they match. Review it in TREX (Portfolio).",
    )


def approval_alerts(pending: Iterable[PendingApproval], now: float) -> list[Alert]:
    """Approvals inside their last ``APPROVAL_WARN_S`` seconds, once each."""
    out: dict[str, Alert] = {}
    for item in pending:
        left = item.send_deadline - now
        if not 0 < left <= APPROVAL_WARN_S or item.proposal_id in out:
            continue
        seconds = int(-(-left // 1))  # whole seconds, rounded up
        out[item.proposal_id] = Alert(
            AlertKind.APPROVAL_EXPIRING,
            item.proposal_id,
            f"trex: approval expires in {seconds} s",
            f"An entry proposal ({item.label}) is waiting for your approval and "
            f"expires in {seconds} s. Nothing is sent unless you approve it in "
            "TREX (Trade review).",
        )
    return list(out.values())


def streak_broken_alert(results: Sequence[SessionOutcome]) -> Alert | None:
    """The streak that just ended, if the last closed sessions all fail and
    a qualifying run came right before them (within the recent window)."""
    if not results or results[-1].qualifying:
        return None
    last_ok = max((i for i, r in enumerate(results) if r.qualifying), default=None)
    if last_ok is None:
        return None
    broke = last_ok + 1
    if broke < len(results) - STREAK_BREAK_RECENT_SESSIONS:
        return None
    run = 0
    for row in reversed(results[:broke]):
        if not row.qualifying:
            break
        run += 1
    row = results[broke]
    day = f"{row.session.strftime('%a')} {row.session.isoformat()}"
    words = "; ".join(SESSION_REASON_WORDS.get(r, r) for r in row.reasons) or "not qualifying"
    return Alert(
        AlertKind.ACCEPTANCE_STREAK_BROKEN,
        row.session.isoformat(),
        "trex: paper acceptance streak broken",
        f"The streak ended on {day} after {_plural(run, 'qualifying session')} "
        f"({words}). It restarts at 0 of {STREAK_REQUIRED}; trading is unchanged.",
    )


def quote_blind_alerts(events: Iterable[Mapping[str, Any]], now: float) -> list[Alert]:
    """``exit_quote_blind`` desk events of the current ET session, once per
    structure (the desk raises one per structure per session)."""
    today = datetime.fromtimestamp(now, ET).date()
    out: dict[str, Alert] = {}
    for event in events:
        if not isinstance(event, Mapping) or event.get("event") != AlertKind.EXIT_QUOTE_BLIND:
            continue
        structure, symbol = event.get("structure"), event.get("symbol")
        blind_s, session = event.get("blind_s"), event.get("session")
        reason = event.get("reason")
        if not (isinstance(structure, str) and 0 < len(structure) <= 128):
            continue
        if not (isinstance(symbol, str) and _SYMBOL.match(symbol)):
            continue
        if not isinstance(blind_s, int) or isinstance(blind_s, bool) or blind_s < 0:
            continue
        try:
            day = date.fromisoformat(session) if isinstance(session, str) else None
        except ValueError:
            day = None
        if day is None or day != today:
            continue
        key = f"{structure}:{day.isoformat()}"
        if key in out:
            continue
        what = reason if isinstance(reason, str) and _WORD.match(reason) else "pending"
        out[key] = Alert(
            AlertKind.EXIT_QUOTE_BLIND,
            key,
            f"trex: exit quote blind ({symbol})",
            f"The {what} exit for {symbol} has had no usable bid for "
            f"{span_label(blind_s)} and cannot be priced. The desk keeps trying every "
            "tick; check the position in TREX (Portfolio).",
        )
    return list(out.values())


def remind_every_s(now: float) -> int:
    return REMIND_MARKET_S if market_hours(now) else REMIND_EVERY_S


def plan_alerts(
    active: Iterable[Alert],
    prior: Mapping[str, Any],
    *,
    now: float,
    quiet: QuietHours | None,
    unknown: frozenset[AlertKind] = frozenset(),
) -> tuple[list[AlertPush], dict[str, dict[str, Any]]]:
    """The pushes due now and the alert book to persist.

    ``prior`` maps ``Alert.ident`` to its book entry. An occurrence no longer
    active is forgotten (resolved), except for kinds in ``unknown`` (their
    source could not be read): those entries are carried unchanged, so a
    blind sweep neither resolves nor repeats them. ``last_notified_at`` is
    set for every planned push; :func:`settle_alert` reverts a failed one.
    """
    hush = quiet is not None and quiet.contains(now)
    pushes: list[AlertPush] = []
    book: dict[str, dict[str, Any]] = {}
    for alert in active:
        spec = WAVE4_ALERTS[alert.kind]
        if spec.sender != SWEEP or alert.ident in book:
            continue
        raw = prior.get(alert.ident)
        prev: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
        first = _num(prev.get("first_seen_at"))
        first = now if first is None else first
        last = _num(prev.get("last_notified_at"))
        failed = _num(prev.get("notify_failed_at"))
        entry: dict[str, Any] = {
            "kind": alert.kind.value,
            "key": alert.key,
            "first_seen_at": first,
            "last_notified_at": last,
            "notify_failed_at": failed,
            "held": False,
        }
        book[alert.ident] = entry
        if now - first < spec.persist_s:
            continue
        if failed is not None and now - failed < NOTIFY_RETRY_S:
            continue
        due = (
            last is None
            or failed is not None
            or (spec.remind and now - last >= remind_every_s(now))
        )
        if not due:
            continue
        if hush and not spec.safety:
            entry["held"] = True
            continue
        entry["last_notified_at"] = now
        pushes.append(AlertPush(alert.ident, alert.kind, alert.title, alert.message, spec.priority))
    unknown_values = {kind.value for kind in unknown}
    for ident, raw in prior.items():
        if ident in book or not isinstance(raw, Mapping):
            continue
        if raw.get("kind") in unknown_values:
            book[ident] = dict(raw)
    return pushes, book


def settle_alert(
    book: dict[str, dict[str, Any]],
    prior: Mapping[str, Any],
    ident: str,
    delivered: bool,
    now: float,
) -> None:
    """Record one push outcome: a failed push is not "notified"; it is owed
    and retried after NOTIFY_RETRY_S (the last delivered time stays)."""
    entry = book[ident]
    if delivered:
        entry["notify_failed_at"] = None
        return
    raw = prior.get(ident)
    previous = raw.get("last_notified_at") if isinstance(raw, Mapping) else None
    entry["last_notified_at"] = _num(previous)
    entry["notify_failed_at"] = now
