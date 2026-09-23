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
        [((22, 0), True), ((23, 30), True), ((3, 0), True), ((6, 59), True),
         ((7, 0), False), ((12, 0), False), ((21, 59), False)],
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
        status=status, now=NOW, prior=prior, urgency=urg, bad=BAD, healthy=HEALTHY,
        alarm=lambda: ("t: down", "it is down"), recovery=lambda: ("t: back", "it is back"),
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
