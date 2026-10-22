"""Wave 4 owner alerts (lane J): the catalogue, the detectors and the plan.

Rules under test (WAVE3-CONTRACTS.md lane map, Wave 4 lane J brief):

- five alerts: reconciliation_required, an approval expiring within 60 s,
  acceptance streak broken, exit_quote_blind, gateway cold-restart missed;
- quiet hours hold every alert EXCEPT the safety ones (reconciliation
  required, exit quote blind), which go out at any hour;
- each occurrence is pushed once (safety alerts remind while they last), a
  failed push is owed and retried after the backoff, an unreadable source
  never "resolves" an alert it cannot see.

Expected values are written by hand from those rules, never computed by the
code under test. The module is reached as ``ap.<name>`` so each test fails on
its own until the name exists.
"""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from tree_options.trex import alert_policy as ap

ET = ZoneInfo("America/New_York")
MT = ZoneInfo("America/Denver")
QUIET = ap.QuietHours(time(22, 0), time(7, 0), MT)


def _at(tz: ZoneInfo, y: int, mo: int, d: int, h: int, mi: int = 0, s: int = 0) -> float:
    return datetime(y, mo, d, h, mi, s, tzinfo=tz).timestamp()


#: Thu 2026-10-22, an NYSE session: 10:00 ET is market hours and outside the
#: default quiet window (08:00 MDT); 01:00 MDT is inside it.
NOW = _at(ET, 2026, 10, 22, 10, 0)
NIGHT = _at(MT, 2026, 10, 22, 1, 0)

SENT_BY_SWEEP = [
    "reconciliation_required",
    "approval_expiring",
    "acceptance_streak_broken",
    "exit_quote_blind",
]


def _kind(name: str) -> Any:
    return ap.AlertKind(name)


def _alert(name: str, key: str = "k1") -> Any:
    return ap.Alert(_kind(name), key, f"trex: {name}", f"{name} happened")


# -- the catalogue -----------------------------------------------------------------


def test_catalogue_names_exactly_the_five_wave4_alerts() -> None:
    assert {k.value for k in ap.WAVE4_ALERTS} == {
        "reconciliation_required",
        "approval_expiring",
        "acceptance_streak_broken",
        "exit_quote_blind",
        "cold_restart_missed",
    }


def test_catalogue_safety_alerts_are_reconciliation_and_exit_quote_blind() -> None:
    safety = {k.value for k, spec in ap.WAVE4_ALERTS.items() if spec.safety}
    assert safety == {"reconciliation_required", "exit_quote_blind"}
    # the push policy's safety set covers them (plus exit_watch's own name)
    assert safety <= ap.SAFETY_ALERTS
    assert "quote_blind" in ap.SAFETY_ALERTS
    for quiet_kind in ("approval_expiring", "acceptance_streak_broken", "cold_restart_missed"):
        assert quiet_kind not in ap.SAFETY_ALERTS


def test_catalogue_cold_restart_missed_stays_with_the_gateway_watch() -> None:
    """Wave 1 already sends it (quiet hours respected): one sender, no duplicate."""
    spec = ap.WAVE4_ALERTS[_kind("cold_restart_missed")]
    assert spec.sender == "trex.gateway_watch" and spec.safety is False
    for name in SENT_BY_SWEEP:
        assert ap.WAVE4_ALERTS[_kind(name)].sender == "trex.alert_sweep", name


def test_catalogue_safety_alerts_are_urgent_and_the_rest_are_not() -> None:
    priorities = {k.value: spec.priority for k, spec in ap.WAVE4_ALERTS.items()}
    assert priorities["reconciliation_required"] == "urgent"
    assert priorities["exit_quote_blind"] == "urgent"
    assert priorities["approval_expiring"] == "high"
    assert priorities["acceptance_streak_broken"] == "default"


def test_catalogue_thresholds() -> None:
    assert ap.APPROVAL_WARN_S == 60
    # two books cycles (desk_books.STATUS_INTERVAL_S = 60): a fill booked one
    # cycle late is not an alarm
    assert ap.RECONCILIATION_PERSIST_S == 120
    assert ap.STREAK_BREAK_RECENT_SESSIONS == 5


def test_catalogue_stays_in_step_with_the_contracts() -> None:
    """alert_policy imports no contract (every watchdog loads it); these
    pins catch drift instead."""
    from tree_options.trex.contracts import acceptance as acc
    from tree_options.trex.contracts.vocabulary import Blocker, DeskEvent
    from tree_options.trex.desk_books import STATUS_INTERVAL_S
    from tree_options.trex.equity_exits import EXIT_QUOTE_BLIND

    assert set(ap.SESSION_REASON_WORDS) == set(acc.SESSION_REASONS)
    assert ap.STREAK_REQUIRED == acc.REQUIRED_STREAK_SESSIONS
    assert ap.RECONCILIATION_PERSIST_S == 2 * STATUS_INTERVAL_S
    # the desk event name (equity_exits) and its blocker code
    assert ap.AlertKind.EXIT_QUOTE_BLIND == EXIT_QUOTE_BLIND == Blocker.EXIT_QUOTE_BLIND
    assert ap.AlertKind.RECONCILIATION_REQUIRED == DeskEvent.RECONCILIATION_REQUIRED


# -- detectors -----------------------------------------------------------------------


def _pending(seconds_left: float, pid: str = "a" * 64, label: str = "SPY equity") -> Any:
    return ap.PendingApproval(proposal_id=pid, label=label, send_deadline=NOW + seconds_left)


@pytest.mark.parametrize(
    ("seconds_left", "alerts"),
    [(61, 0), (60, 1), (45, 1), (1, 1), (0, 0), (-5, 0)],
)
def test_approval_expiring_window_is_the_last_60_seconds(seconds_left: float, alerts: int) -> None:
    assert len(ap.approval_alerts([_pending(seconds_left)], NOW)) == alerts


def test_approval_alert_names_the_proposal_and_the_seconds_left() -> None:
    (alert,) = ap.approval_alerts([_pending(45, label="SPY equity")], NOW)
    assert alert.kind == _kind("approval_expiring")
    assert alert.key == "a" * 64
    assert "SPY equity" in alert.message and "45 s" in alert.message
    assert "approve" in alert.message.lower()
    assert "a" * 64 not in alert.message  # ids stay in the book, not on the lock screen


def test_approval_alerts_one_per_proposal() -> None:
    got = ap.approval_alerts(
        [_pending(30, "a" * 64), _pending(50, "b" * 64), _pending(30, "a" * 64)], NOW
    )
    assert sorted(a.key for a in got) == ["a" * 64, "b" * 64]


def _outcomes(*marks: str) -> list[Any]:
    """'q' qualifying, 'x' not; consecutive days from 2026-11-02."""
    out = []
    for i, mark in enumerate(marks):
        out.append(
            ap.SessionOutcome(
                session=date(2026, 11, 2 + i),
                qualifying=mark == "q",
                reasons=() if mark == "q" else ("heartbeat_below_99pct",),
            )
        )
    return out


def test_streak_broken_after_a_running_streak() -> None:
    alert = ap.streak_broken_alert(_outcomes("q", "q", "q", "x"))
    assert alert is not None and alert.kind == _kind("acceptance_streak_broken")
    assert alert.key == "2026-11-05"
    assert "3" in alert.message and "2026-11-05" in alert.message
    assert "0 of 10" in alert.message
    assert "heartbeat" in alert.message


@pytest.mark.parametrize("marks", [(), ("x",), ("x", "x"), ("q",), ("q", "q"), ("q", "x", "q")])
def test_no_streak_break_without_a_streak_that_just_ended(marks: tuple[str, ...]) -> None:
    assert ap.streak_broken_alert(_outcomes(*marks)) is None


def test_streak_break_is_the_first_failing_session_not_the_last() -> None:
    alert = ap.streak_broken_alert(_outcomes("q", "q", "x", "x"))
    assert alert is not None and alert.key == "2026-11-04"
    assert "2" in alert.message


def test_an_old_streak_break_is_not_news() -> None:
    # the break (index 1) is 5 sessions before the last one, inside the window
    assert ap.streak_broken_alert(_outcomes("q", "x", "x", "x", "x", "x")) is not None
    # one more failing session pushes it out of the 5 most recent
    assert ap.streak_broken_alert(_outcomes("q", "x", "x", "x", "x", "x", "x")) is None


def test_streak_reason_codes_get_human_words() -> None:
    outcomes = _outcomes("q", "q")
    outcomes.append(
        ap.SessionOutcome(
            session=date(2026, 11, 4), qualifying=False, reasons=("session_not_recorded",)
        )
    )
    alert = ap.streak_broken_alert(outcomes)
    assert alert is not None
    assert "not recorded" in alert.message


def _blind_event(**over: Any) -> dict[str, Any]:
    event: dict[str, Any] = {
        "ts": "2026-10-22T10:00:00-04:00",
        "event": "exit_quote_blind",
        "structure": "intent-spy-1",
        "symbol": "SPY",
        "reason": "forced",
        "session": "2026-10-22",
        "blind_since": "2026-10-22T09:55:00-04:00",
        "blind_s": 300,
        "after_s": 300,
    }
    event.update(over)
    return event


def test_exit_quote_blind_event_of_this_session_alerts() -> None:
    (alert,) = ap.quote_blind_alerts([_blind_event()], NOW)
    assert alert.kind == _kind("exit_quote_blind")
    assert alert.key == "intent-spy-1:2026-10-22"
    assert "SPY" in alert.title + alert.message
    assert "5m" in alert.message
    assert "intent-spy-1" not in alert.message


@pytest.mark.parametrize(
    "event",
    [
        _blind_event(session="2026-10-21"),  # yesterday: moot
        _blind_event(event="exit_begin"),
        _blind_event(symbol=None),
        _blind_event(structure=""),
        _blind_event(blind_s="300"),
        _blind_event(session="not-a-date"),
        {"event": "exit_quote_blind"},
    ],
)
def test_exit_quote_blind_ignores_other_sessions_and_garbage(event: dict[str, Any]) -> None:
    assert ap.quote_blind_alerts([event], NOW) == []


def test_exit_quote_blind_once_per_structure_per_session() -> None:
    got = ap.quote_blind_alerts(
        [_blind_event(), _blind_event(blind_s=360), _blind_event(structure="intent-qqq-2")], NOW
    )
    assert sorted(a.key for a in got) == [
        "intent-qqq-2:2026-10-22",
        "intent-spy-1:2026-10-22",
    ]


def test_reconciliation_required_alerts_with_its_mismatch_counts() -> None:
    alert = ap.reconciliation_alert(
        "reconciliation_required", ["quantity_mismatch", "missing", "quantity_mismatch"], NOW
    )
    assert alert is not None and alert.kind == _kind("reconciliation_required")
    assert alert.key == "books"
    assert "3 positions" in alert.message
    assert "Thu 10:00 ET" in alert.message
    assert "entries" in alert.message.lower()


@pytest.mark.parametrize("status", ["clean", "unavailable", None])
def test_reconciliation_other_statuses_do_not_alert(status: str | None) -> None:
    assert ap.reconciliation_alert(status, [], NOW) is None


# -- the plan ------------------------------------------------------------------------


def _plan(alerts: list[Any], prior: dict[str, Any], now: float, **kw: Any) -> tuple[Any, Any]:
    return ap.plan_alerts(alerts, prior, now=now, quiet=kw.pop("quiet", QUIET), **kw)


def _past_persistence(name: str) -> float:
    return float(ap.WAVE4_ALERTS[_kind(name)].persist_s)


def _first_due(alert: Any, start: float) -> tuple[Any, Any, float]:
    """Plan from an empty book until the alert's persistence has passed."""
    persist = _past_persistence(alert.kind.value)
    pushes, book = _plan([alert], {}, start)
    if persist:
        assert pushes == []
        pushes, book = _plan([alert], book, start + persist)
    return pushes, book, start + persist


@pytest.mark.parametrize("name", SENT_BY_SWEEP)
def test_first_sighting_pushes_once_at_its_priority(name: str) -> None:
    alert = _alert(name)
    pushes, book, at = _first_due(alert, NOW)
    assert [(p.kind.value, p.priority) for p in pushes] == [
        (name, ap.WAVE4_ALERTS[_kind(name)].priority)
    ]
    assert pushes[0].title == f"trex: {name}"
    again, _ = _plan([alert], book, at + 30)
    assert again == []


@pytest.mark.parametrize("name", SENT_BY_SWEEP)
def test_quiet_hours_hold_everything_but_safety_alerts(name: str) -> None:
    alert = _alert(name)
    pushes, book, at = _first_due(alert, NIGHT)
    entry = book[alert.ident]
    if name in ("reconciliation_required", "exit_quote_blind"):
        assert len(pushes) == 1 and entry["held"] is False
        assert entry["last_notified_at"] == at
    else:
        assert pushes == [] and entry["held"] is True
        assert entry["last_notified_at"] is None  # still owed when quiet ends


def test_a_held_alert_goes_out_when_quiet_hours_end() -> None:
    alert = _alert("acceptance_streak_broken")
    _, book = _plan([alert], {}, NIGHT)
    morning = _at(MT, 2026, 10, 22, 7, 0)
    pushes, book = _plan([alert], book, morning)
    assert len(pushes) == 1 and book[alert.ident]["held"] is False


def test_quiet_off_holds_nothing() -> None:
    pushes, _ = _plan([_alert("approval_expiring")], {}, NIGHT, quiet=None)
    assert len(pushes) == 1


def test_reconciliation_waits_out_its_persistence_first() -> None:
    alert = _alert("reconciliation_required", "books")
    pushes, book = _plan([alert], {}, NOW)
    assert pushes == [] and book[alert.ident]["first_seen_at"] == NOW
    pushes, book = _plan([alert], book, NOW + 119)
    assert pushes == [] and book[alert.ident]["first_seen_at"] == NOW
    pushes, book = _plan([alert], book, NOW + 120)
    assert len(pushes) == 1


def test_safety_alerts_remind_hourly_in_market_hours() -> None:
    alert = _alert("reconciliation_required", "books")
    start = NOW + 120
    book = {
        alert.ident: {
            "kind": "reconciliation_required",
            "key": "books",
            "first_seen_at": NOW,
            "last_notified_at": start,
            "notify_failed_at": None,
            "held": False,
        }
    }
    assert _plan([alert], book, start + 3599)[0] == []
    assert len(_plan([alert], book, start + 3600)[0]) == 1


def test_safety_alerts_remind_every_four_hours_off_market() -> None:
    alert = _alert("reconciliation_required", "books")
    book = {
        alert.ident: {
            "kind": "reconciliation_required",
            "key": "books",
            "first_seen_at": NIGHT - 600,
            "last_notified_at": NIGHT,
            "notify_failed_at": None,
            "held": False,
        }
    }
    assert _plan([alert], book, NIGHT + 3600)[0] == []
    assert _plan([alert], book, NIGHT + 4 * 3600 - 1)[0] == []
    assert len(_plan([alert], book, NIGHT + 4 * 3600)[0]) == 1  # 05:00 MDT: still quiet


@pytest.mark.parametrize(
    "name", ["approval_expiring", "acceptance_streak_broken", "exit_quote_blind"]
)
def test_one_shot_alerts_never_remind(name: str) -> None:
    alert = _alert(name)
    pushes, book = _plan([alert], {}, NOW)
    assert len(pushes) == 1
    assert _plan([alert], book, NOW + 6 * 3600)[0] == []


def test_a_resolved_alert_is_forgotten_and_a_new_occurrence_alerts() -> None:
    alert = _alert("approval_expiring", "a" * 64)
    _, book = _plan([alert], {}, NOW)
    _, book = _plan([], book, NOW + 30)
    assert book == {}
    pushes, _ = _plan([_alert("approval_expiring", "b" * 64)], book, NOW + 60)
    assert len(pushes) == 1


def test_an_unreadable_source_never_resolves_its_alerts() -> None:
    """Unknown is not clear: the booked alert survives a blind sweep and is
    not pushed again when the source reads again."""
    alert = _alert("reconciliation_required", "books")
    _, book = _plan([alert], {}, NOW)
    pushes, book = _plan([alert], book, NOW + 120)
    assert len(pushes) == 1
    kind = _kind("reconciliation_required")
    pushes, blind = _plan([], book, NOW + 150, unknown=frozenset({kind}))
    assert pushes == [] and blind[alert.ident] == book[alert.ident]
    pushes, _ = _plan([alert], blind, NOW + 180)
    assert pushes == []


def test_a_failed_push_is_owed_and_backs_off() -> None:
    alert = _alert("approval_expiring")
    pushes, book = _plan([alert], {}, NOW)
    (push,) = pushes
    ap.settle_alert(book, {}, push.ident, False, NOW)
    assert book[alert.ident]["last_notified_at"] is None
    assert book[alert.ident]["notify_failed_at"] == NOW
    assert _plan([alert], book, NOW + ap.NOTIFY_RETRY_S - 1)[0] == []
    pushes, book2 = _plan([alert], book, NOW + ap.NOTIFY_RETRY_S)
    assert len(pushes) == 1
    ap.settle_alert(book2, book, pushes[0].ident, True, NOW + ap.NOTIFY_RETRY_S)
    assert book2[alert.ident]["notify_failed_at"] is None
    assert book2[alert.ident]["last_notified_at"] == NOW + ap.NOTIFY_RETRY_S


def test_a_failed_reminder_keeps_the_last_delivered_time() -> None:
    alert = _alert("reconciliation_required", "books")
    delivered = NOW - 3600
    prior = {
        alert.ident: {
            "kind": "reconciliation_required",
            "key": "books",
            "first_seen_at": NOW - 7200,
            "last_notified_at": delivered,
            "notify_failed_at": None,
            "held": False,
        }
    }
    pushes, book = _plan([alert], prior, NOW)
    ap.settle_alert(book, prior, pushes[0].ident, False, NOW)
    assert book[alert.ident]["last_notified_at"] == delivered


def test_the_plan_never_sends_what_another_watchdog_owns() -> None:
    pushes, book = _plan([_alert("cold_restart_missed", "2026-10-18")], {}, NOW)
    assert pushes == [] and book == {}


def test_duplicate_alerts_in_one_sweep_push_once() -> None:
    alert = _alert("exit_quote_blind")
    pushes, book = _plan([alert, alert], {}, NOW)
    assert len(pushes) == 1 and list(book) == [alert.ident]


def test_a_garbage_book_entry_is_treated_as_new() -> None:
    alert = _alert("approval_expiring")
    pushes, book = _plan([alert], {alert.ident: "not a dict"}, NOW)
    assert len(pushes) == 1 and book[alert.ident]["first_seen_at"] == NOW
