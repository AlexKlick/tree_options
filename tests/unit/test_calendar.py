"""Calendar tests: checksum verification, ordering, instants, DST, AST ban."""

from __future__ import annotations

import ast
import json
from datetime import UTC, date, datetime

import pytest


class TestStaticCalendarIntegrity:
    def test_loads_vendored_fixture(self, static_calendar):
        sessions = static_calendar.sessions()
        assert sessions[0] == date(2018, 1, 2)
        assert len(sessions) == 2263

    def test_checksum_tamper_fails_closed(self, static_calendar, tmp_path):
        from tree_options.time.calendar import CalendarIntegrityError, StaticSessionCalendar

        payload = json.loads(_fixture_path().read_text())
        payload["sessions"] = payload["sessions"][:-1]  # drop one session
        bad = tmp_path / "cal.json"
        bad.write_text(json.dumps(payload))
        (tmp_path / "cal.sha256").write_text("0" * 64 + "  cal.json\n")

        with pytest.raises(CalendarIntegrityError, match="checksum"):
            StaticSessionCalendar(bad, tmp_path / "cal.sha256")

    def test_checksum_missing_fails_closed(self, tmp_path):
        from tree_options.time.calendar import CalendarIntegrityError, StaticSessionCalendar

        good = _fixture_path()
        with pytest.raises(CalendarIntegrityError, match="checksum"):
            StaticSessionCalendar(good, tmp_path / "nonexistent.sha256")

    def test_non_monotonic_rejected(self, tmp_path):
        from tree_options.time.calendar import CalendarIntegrityError, StaticSessionCalendar

        payload = {
            "calendar": "XNYS",
            "timezone": "America/New_York",
            "open": "09:30",
            "close": "16:00",
            "sessions": ["2024-01-03", "2024-01-02"],
        }
        bad = tmp_path / "cal.json"
        bad.write_text(json.dumps(payload))
        import hashlib

        (tmp_path / "cal.sha256").write_text(
            hashlib.sha256(bad.read_bytes()).hexdigest() + "  cal.json\n"
        )
        with pytest.raises(CalendarIntegrityError, match="strictly increasing"):
            StaticSessionCalendar(bad, tmp_path / "cal.sha256")

    def test_unknown_date_not_a_session(self, static_calendar):
        from tree_options.time.calendar import NotASessionError

        # 2024-01-01 is a NYSE holiday; 2024-01-06 is a Saturday.
        with pytest.raises(NotASessionError):
            static_calendar.ordinal(date(2024, 1, 1))
        with pytest.raises(NotASessionError):
            static_calendar.ordinal(date(2024, 1, 6))

    def test_nth_after_monotone(self, static_calendar):
        d = date(2024, 1, 3)
        assert static_calendar.nth_after(d, 0) == d
        assert static_calendar.nth_after(d, 1) == date(2024, 1, 4)
        # 2024-01-15 is MLK day (closed): sessions after 01/11 are 12, 16, 17.
        assert static_calendar.nth_after(date(2024, 1, 11), 2) == date(2024, 1, 16)
        assert static_calendar.nth_after(date(2024, 1, 11), 3) == date(2024, 1, 17)
        with pytest.raises(ValueError):
            static_calendar.nth_after(d, -1)

    def test_nth_past_end_fails_closed(self, static_calendar):
        from tree_options.time.calendar import NotASessionError

        last = static_calendar.sessions()[-1]
        with pytest.raises(NotASessionError):
            static_calendar.nth_after(last, 1)


class TestSessionInstants:
    def test_close_is_16_et_dst_correct(self, static_calendar):
        # 2024-07-03: EDT (UTC-4) -> close 20:00 UTC
        close_july = static_calendar.session_close(date(2024, 7, 10))  # 07-03 is an early close now
        assert close_july == datetime(2024, 7, 10, 20, 0, tzinfo=UTC)
        # 2024-01-03: EST (UTC-5) -> close 21:00 UTC
        close_jan = static_calendar.session_close(date(2024, 1, 3))
        assert close_jan == datetime(2024, 1, 3, 21, 0, tzinfo=UTC)

    def test_open_is_930_et(self, static_calendar):
        open_july = static_calendar.session_open(date(2024, 7, 3))
        assert open_july == datetime(2024, 7, 3, 13, 30, tzinfo=UTC)

    def test_contains_instant(self, static_calendar):
        d = date(2024, 7, 3)
        assert static_calendar.contains_instant(d, datetime(2024, 7, 3, 15, 0, tzinfo=UTC))
        assert not static_calendar.contains_instant(
            d, datetime(2024, 7, 3, 12, 0, tzinfo=UTC)
        )
        with pytest.raises(ValueError):
            static_calendar.contains_instant(d, datetime(2024, 7, 3, 15, 0))  # naive


class TestSyntheticCalendar:
    def test_weekdays_only_contiguous(self, synthetic_calendar):
        sessions = synthetic_calendar.sessions()
        assert sessions[0] == date(2019, 1, 7)
        assert all(s.weekday() < 5 for s in sessions)
        assert len(sessions) == 1500

    def test_is_a_sessioncalendar_shape(self, synthetic_calendar):
        cal = synthetic_calendar
        assert cal.nth_after(date(2019, 1, 7), 1) == date(2019, 1, 8)
        # Friday -> Monday
        assert cal.nth_after(date(2019, 1, 11), 1) == date(2019, 1, 14)


class TestNoNaiveArithmetic:
    """Production code must not do naive date arithmetic (handoff §14.3)."""

    BANNED_CALLS = frozenset({"timedelta", "toordinal", "fromordinal", "weekday"})

    def test_no_naive_date_arithmetic_in_src(self, repo_root):
        src = repo_root / "src" / "tree_options"
        offenders: list[str] = []
        for py in sorted(src.rglob("*.py")):
            rel = py.relative_to(src).as_posix()
            if rel.startswith("time/"):
                continue  # time/ owns date↔instant conversion and the test double
            tree = ast.parse(py.read_text(), filename=str(py))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    fn = node.func
                    name = fn.id if isinstance(fn, ast.Name) else (fn.attr if isinstance(fn, ast.Attribute) else None)
                    if name in self.BANNED_CALLS:
                        offenders.append(f"{rel}:{node.lineno} calls {name}()")
                if isinstance(node, ast.Attribute) and node.attr in self.BANNED_CALLS:
                    offenders.append(f"{rel}:{node.lineno} references .{node.attr}")
        assert not offenders, f"naive date arithmetic outside time/: {offenders}"

    def test_synthetic_not_imported_by_src(self, repo_root):
        src = repo_root / "src" / "tree_options"
        offenders: list[str] = []
        for py in sorted(src.rglob("*.py")):
            rel = py.relative_to(src).as_posix()
            if rel == "time/synthetic.py":
                continue
            tree = ast.parse(py.read_text(), filename=str(py))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module and "synthetic" in node.module:
                    offenders.append(f"{rel}:{node.lineno}")
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if "synthetic" in alias.name:
                            offenders.append(f"{rel}:{node.lineno}")
        assert not offenders, f"production code imports the synthetic test double: {offenders}"


def _fixture_path():
    from pathlib import Path

    return Path(__file__).resolve().parents[2] / "data" / "calendar" / (
        "nyse_sessions_2018_01_02_2026_12_31.json"
    )


class TestTemporalCoherenceCalendar:
    """Audit §4.4: holiday gap, early close, spring + autumn DST (static NYSE)."""

    def test_holiday_gap_mlK(self, static_calendar):
        # MLK 2024-01-15 (Monday) closed: Thursday -> Tuesday spans it.
        assert not static_calendar.is_session(date(2024, 1, 15))
        assert static_calendar.ordinal(date(2024, 1, 16)) == static_calendar.ordinal(date(2024, 1, 12)) + 1  # Fri -> Tue spans the Monday holiday

    def test_early_close_session(self, static_calendar):
        # 2024-07-03: NYSE half day, 13:00 ET close = 17:00 UTC (EDT).
        d = date(2024, 7, 3)
        assert static_calendar.is_session(d)
        close = static_calendar.session_close(d)
        assert (close.hour, close.minute) == (17, 0)
        # contains_instant honors the early close
        from tree_options.time.sessions import shift_instant

        just_after = shift_instant(close, 1)
        assert static_calendar.contains_instant(d, close)
        assert not static_calendar.contains_instant(d, just_after)

    def test_spring_dst_transition(self, static_calendar):
        # DST starts 2024-03-10: Jan close 21:00 UTC (EST), late Mar 20:00 UTC (EDT).
        assert static_calendar.session_close(date(2024, 1, 10)).hour == 21
        assert static_calendar.session_close(date(2024, 3, 12)).hour == 20

    def test_autumn_dst_transition(self, static_calendar):
        # DST ends 2024-11-03: late Oct 20:00 UTC (EDT), mid Nov 21:00 UTC (EST).
        assert static_calendar.session_close(date(2024, 10, 29)).hour == 20
        assert static_calendar.session_close(date(2024, 11, 5)).hour == 21

    def test_early_closes_are_sessions(self, static_calendar):
        # integrity: every early close is a session in the fixture
        payload_sessions = set(static_calendar.sessions())
        for d in static_calendar._early_closes:
            assert d in payload_sessions
