"""When and how loudly an operator alert goes out (both watchdogs).

Operator decision 2026-09-23: high priority only during market hours with
open positions; nothing buzzes in quiet hours (22:00-07:00 operator time,
America/Denver by default). An alarm raised overnight waits for the
morning (07:00 MDT = 09:00 ET, before the open).
"""

from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

import pytest

from tree_options.trex.alert_policy import (
    REMIND_EVERY_S,
    REMIND_MARKET_S,
    QuietHours,
    Urgency,
    load_quiet_hours,
    market_hours,
    next_push,
    urgency,
)

ET = ZoneInfo("America/New_York")
MT = ZoneInfo("America/Denver")
QUIET = QuietHours(time(22, 0), time(7, 0), MT)


def _at(tz: ZoneInfo, y: int, mo: int, d: int, h: int, mi: int = 0) -> float:
    return datetime(y, mo, d, h, mi, tzinfo=tz).timestamp()


WED = (2026, 9, 23)  # NYSE session
SAT = (2026, 9, 26)
THANKSGIVING = (2026, 11, 26)  # a Thursday, exchange closed


class TestMarketHours:
    @pytest.mark.parametrize(
        ("when", "expected"),
        [
            (_at(ET, *WED, 9, 0), True),  # 30 min before the open
            (_at(ET, *WED, 16, 15), True),  # the monitor's session end
            (_at(ET, *WED, 8, 59), False),
            (_at(ET, *WED, 16, 16), False),
            (_at(ET, *SAT, 11, 0), False),
            (_at(ET, *THANKSGIVING, 11, 0), False),
        ],
    )
    def test_watch_window(self, when: float, expected: bool) -> None:
        assert market_hours(when) is expected


class TestQuietHours:
    @pytest.mark.parametrize(
        ("hm", "expected"),
        [
            ((22, 0), True),
            ((23, 30), True),
            ((3, 0), True),
            ((6, 59), True),
            ((7, 0), False),
            ((12, 0), False),
            ((21, 59), False),
        ],
    )
    def test_window_wraps_midnight(self, hm: tuple[int, int], expected: bool) -> None:
        assert QUIET.contains(_at(MT, *WED, *hm)) is expected

    def test_window_within_a_day(self) -> None:
        nap = QuietHours(time(13, 0), time(14, 0), MT)
        assert nap.contains(_at(MT, *WED, 13, 30))
        assert not nap.contains(_at(MT, *WED, 14, 0))


class TestLoadQuietHours:
    def test_defaults(self) -> None:
        assert load_quiet_hours({}) == QUIET

    def test_custom_window_and_zone(self) -> None:
        got = load_quiet_hours({"QUIET_HOURS": "21:30-06:15", "OPERATOR_TZ": "America/Chicago"})
        assert got == QuietHours(time(21, 30), time(6, 15), ZoneInfo("America/Chicago"))

    def test_off(self) -> None:
        assert load_quiet_hours({"QUIET_HOURS": "off"}) is None

    def test_garbage_falls_back_to_defaults(self) -> None:
        assert load_quiet_hours({"QUIET_HOURS": "late-ish", "OPERATOR_TZ": "Mars/Base"}) == QUIET


class TestUrgency:
    def test_market_hours_with_positions_is_loud_and_hourly(self) -> None:
        got = urgency(_at(ET, *WED, 9, 0), exposed=True, quiet=QUIET)  # 07:00 MDT
        assert got == Urgency("high", REMIND_MARKET_S, quiet=False)

    def test_market_hours_without_positions_is_normal(self) -> None:
        got = urgency(_at(ET, *WED, 11, 0), exposed=False, quiet=QUIET)
        assert got == Urgency("default", REMIND_EVERY_S, quiet=False)

    def test_night_is_quiet_even_with_positions(self) -> None:
        got = urgency(_at(MT, *WED, 1, 0), exposed=True, quiet=QUIET)
        assert got.quiet

    def test_weekend_daytime_is_normal(self) -> None:
        got = urgency(_at(MT, *SAT, 10, 0), exposed=True, quiet=QUIET)
        assert got == Urgency("default", REMIND_EVERY_S, quiet=False)

    def test_quiet_off_never_holds(self) -> None:
        assert not urgency(_at(MT, *WED, 1, 0), exposed=True, quiet=None).quiet


LOUD = Urgency("high", REMIND_MARKET_S, quiet=False)
HUSH = Urgency("default", REMIND_EVERY_S, quiet=True)
NOW = _at(ET, *WED, 12, 0)
BAD = frozenset({"down"})
HEALTHY = frozenset({"ok"})


def _push(status: str, prior: dict[str, object], urg: Urgency) -> tuple[object, dict[str, object]]:
    return next_push(
        status=status,
        now=NOW,
        prior=prior,
        urgency=urg,
        bad=BAD,
        healthy=HEALTHY,
        alarm=lambda: ("t: down", "it is down"),
        recovery=lambda: ("t: back", "it is back"),
    )


class TestNextPush:
    def test_entering_a_bad_state_pushes_at_the_urgency_priority(self) -> None:
        push, book = _push("down", {}, LOUD)
        assert push is not None and push.priority == "high" and push.status == "down"
        assert book["last_notified_status"] == "down" and book["last_notified_at"] == NOW

    def test_quiet_hours_hold_the_alarm(self) -> None:
        push, book = _push("down", {}, HUSH)
        assert push is None and book["notify_held"] is True
        assert book["last_notified_status"] is None  # still due when quiet ends

    def test_reminder_cadence_follows_the_urgency(self) -> None:
        prior = {"last_notified_status": "down", "last_notified_at": NOW - REMIND_MARKET_S}
        assert _push("down", prior, LOUD)[0] is not None
        normal = Urgency("default", REMIND_EVERY_S, quiet=False)
        assert _push("down", prior, normal)[0] is None

    def test_recovery_only_after_a_delivered_alarm(self) -> None:
        normal = Urgency("default", REMIND_EVERY_S, quiet=False)
        push, _ = _push("ok", {"last_notified_status": "down", "last_notified_at": NOW}, normal)
        assert push is not None and push.status == "recovered"
        held, _ = _push("ok", {"last_notified_status": None, "notify_held": True}, normal)
        assert held is None  # the alarm never went out: no "back" either

    def test_recovery_in_quiet_hours_waits(self) -> None:
        push, book = _push("ok", {"last_notified_status": "down", "last_notified_at": NOW}, HUSH)
        assert push is None and book["last_notified_status"] == "down"

    def test_failed_push_backs_off(self) -> None:
        prior = {"notify_failed_at": NOW - 60}
        assert _push("down", prior, LOUD)[0] is None
        prior = {"notify_failed_at": NOW - 301}
        assert _push("down", prior, LOUD)[0] is not None


class TestCodexFixes:
    """Codex review of the exit-watch commit (2026-09-23)."""

    def test_configured_quiet_hours_hold_even_market_alarms(self) -> None:
        """P2: market urgency hard-coded quiet=False, so QUIET_HOURS ending
        08:00 still buzzed at 07:15 (09:15 ET)."""
        late = QuietHours(time(22, 0), time(8, 0), MT)
        got = urgency(_at(MT, *WED, 7, 15), exposed=True, quiet=late)
        assert got == Urgency("high", REMIND_MARKET_S, quiet=True)

    def test_a_failed_reminder_still_retries_when_the_cadence_lengthens(self) -> None:
        """P2: a reminder failing at 16:11 lost its 5-min retry once the
        cadence went from 1 h to 4 h at 16:15."""
        normal = Urgency("default", REMIND_EVERY_S, quiet=False)
        prior = {
            "last_notified_status": "down",
            "last_notified_at": NOW - REMIND_MARKET_S - 300,
            "notify_failed_at": NOW - 301,
        }
        push, book = _push("down", prior, normal)
        assert push is not None and book["last_notified_at"] == NOW

    def test_a_stale_failure_marker_clears_once_nothing_is_pending(self) -> None:
        _, book = _push("ok", {"last_notified_status": "ok", "notify_failed_at": NOW - 999}, LOUD)
        assert book["notify_failed_at"] is None

    def test_a_held_recovery_reports_held(self) -> None:
        """P2: a recovery deferred by quiet hours said notify_held=false."""
        _, book = _push("ok", {"last_notified_status": "down", "last_notified_at": NOW}, HUSH)
        assert book["notify_held"] is True and book["last_notified_status"] == "down"


class TestSafetyAlarms:
    """Wave 4 (lane J): quiet hours are respected EXCEPT for safety alerts,
    the ones where an exit may be blocked or blind until the owner looks
    (``alert_policy.SAFETY_ALERTS``). They go out at any hour, never below
    ``high``; their recoveries still wait for the morning."""

    @staticmethod
    def _safety_push(status: str, prior: dict[str, object], urg: Urgency):  # type: ignore[no-untyped-def]
        return next_push(
            status=status,
            now=NOW,
            prior=prior,
            urgency=urg,
            bad=frozenset({status, "down"}),
            healthy=HEALTHY,
            alarm=lambda: ("t: blind", "exit is blind"),
            recovery=lambda: ("t: back", "exit is back"),
        )

    @pytest.mark.parametrize(
        "status", ["reconciliation_required", "exit_quote_blind", "quote_blind"]
    )
    def test_safety_alarm_is_not_held_by_quiet_hours(self, status: str) -> None:
        push, book = self._safety_push(status, {}, HUSH)
        assert push is not None and push.status == status
        assert push.priority == "high"  # HUSH is "default": safety never goes below high
        assert book["notify_held"] is False and book["last_notified_status"] == status

    def test_safety_alarm_keeps_the_urgency_priority_when_higher(self) -> None:
        loudest = Urgency("urgent", REMIND_MARKET_S, quiet=True)
        push, _ = self._safety_push("exit_quote_blind", {}, loudest)
        assert push is not None and push.priority == "urgent"

    def test_a_non_safety_alarm_is_still_held(self) -> None:
        push, book = self._safety_push("down", {}, HUSH)
        assert push is None and book["notify_held"] is True

    def test_a_safety_recovery_still_waits_for_quiet_hours_to_end(self) -> None:
        prior = {"last_notified_status": "quote_blind", "last_notified_at": NOW - 600}
        push, book = next_push(
            status="ok",
            now=NOW,
            prior=prior,
            urgency=HUSH,
            bad=frozenset({"quote_blind"}),
            healthy=HEALTHY,
            alarm=lambda: ("t", "m"),
            recovery=lambda: ("t: back", "back"),
        )
        assert push is None and book["notify_held"] is True

    def test_exit_watch_quote_blind_goes_out_in_quiet_hours(self) -> None:
        """The exit watch's ``quote_blind`` (a held equity with no real-time
        quote) is the monitor.json face of exit_quote_blind: a safety alarm."""
        from tests.unit.test_trex_exit_watch import NOON, _obs, _quote_blind_book
        from tree_options.trex import exit_watch

        quiet = Urgency("default", REMIND_EVERY_S, quiet=True)
        state, actions = exit_watch.decide(_obs([_quote_blind_book(NOON - 900)]), {}, urgency=quiet)
        (note,) = actions
        assert note.detail == "quote_blind" and note.priority == "high"
        assert state["notify_held"] is False

    def test_exit_watch_touch_blind_is_not_safety_and_still_waits(self) -> None:
        from tests.unit.test_trex_exit_watch import NOON, _blind_book, _obs
        from tree_options.trex import exit_watch

        quiet = Urgency("high", REMIND_MARKET_S, quiet=True)
        state, actions = exit_watch.decide(_obs([_blind_book(NOON - 900)]), {}, urgency=quiet)
        assert actions == [] and state["notify_held"] is True
