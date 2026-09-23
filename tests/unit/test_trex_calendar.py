"""The trex execution calendar runs through 2028; the protocol one stays sealed.

The exit machine skips every non-session tick, so a calendar ending
2026-12-31 meant holds into 2027 would get no exits at all. trex now reads
its own generated calendar (``data/calendar/trex/``); the protocol's
calendar, which sealed research depends on, is byte-for-byte untouched and
the two agree on every session and close they share.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from tree_options.trex import clock

REPO = Path(__file__).resolve().parents[2]
PROTOCOL = REPO / "data" / "calendar" / "nyse_sessions_2018_01_02_2026_12_31.json"
TREX = REPO / "data" / "calendar" / "trex" / "nyse_sessions_2018_01_02_2028_12_29.json"
# pinned independently of the sidecar: regenerating BOTH files must still fail
PROTOCOL_SHA256 = "7f9cccba702e79f6a7cb93fccca3d23dca5e8f2692ba83455c82f62ef697aea4"
ET = ZoneInfo("America/New_York")


@pytest.fixture()
def default_calendar(monkeypatch: pytest.MonkeyPatch) -> None:
    """The calendar clock.py resolves with no TREX_CALENDAR override."""
    monkeypatch.delenv("TREX_CALENDAR", raising=False)
    monkeypatch.setattr(clock, "_calendar", None)


def _payload(path: Path) -> dict[str, object]:
    raw = json.loads(path.read_text())
    assert isinstance(raw, dict)
    return raw


class TestProtocolCalendarUntouched:
    def test_protocol_bytes_match_the_pinned_digest_and_sidecar(self) -> None:
        digest = hashlib.sha256(PROTOCOL.read_bytes()).hexdigest()
        assert digest == PROTOCOL_SHA256
        assert PROTOCOL.with_suffix(".sha256").read_text().split()[0] == digest

    def test_trex_file_matches_its_own_sidecar(self) -> None:
        digest = hashlib.sha256(TREX.read_bytes()).hexdigest()
        assert TREX.with_suffix(".sha256").read_text().split()[0] == digest


class TestTrexCalendarAgreesWithProtocol:
    def test_sessions_identical_through_2026(self) -> None:
        protocol = _payload(PROTOCOL)["sessions"]
        trex = _payload(TREX)["sessions"]
        assert isinstance(protocol, list) and isinstance(trex, list)
        assert [s for s in trex if s <= "2026-12-31"] == protocol

    def test_early_closes_identical_through_2026(self) -> None:
        protocol = _payload(PROTOCOL)["early_close_sessions"]
        trex = _payload(TREX)["early_close_sessions"]
        assert isinstance(protocol, list) and isinstance(trex, list)
        assert [s for s in trex if s <= "2026-12-31"] == protocol

    def test_session_times_identical(self) -> None:
        protocol, trex = _payload(PROTOCOL), _payload(TREX)
        for key in ("calendar", "source", "timezone", "open", "close", "early_close"):
            assert trex[key] == protocol[key], key


class TestTrexClock:
    def test_2027_sessions_exist(self, default_calendar: None) -> None:
        assert clock.is_session(datetime(2027, 1, 4, 10, 0, tzinfo=ET))
        assert not clock.is_session(datetime(2027, 1, 1, 10, 0, tzinfo=ET))  # New Year
        assert not clock.is_session(datetime(2027, 3, 26, 10, 0, tzinfo=ET))  # Good Friday
        assert not clock.is_session(datetime(2027, 7, 5, 10, 0, tzinfo=ET))  # July 4 observed

    def test_2027_early_close(self, default_calendar: None) -> None:
        close = clock.session_calendar().session_close(date(2027, 11, 26))
        assert close.astimezone(ET).hour == 13

    def test_last_session(self, default_calendar: None) -> None:
        assert clock.calendar_last_session() == date(2028, 12, 29)

    def test_sessions_left(self, default_calendar: None) -> None:
        assert clock.calendar_sessions_left(date(2028, 12, 29)) == 0
        assert clock.calendar_sessions_left(date(2028, 12, 28)) == 1
        assert clock.calendar_sessions_left(date(2028, 12, 30)) == 0
        assert clock.calendar_sessions_left(date(2026, 9, 23)) > 500

    def test_horizon_warning(self, default_calendar: None) -> None:
        assert not clock.calendar_horizon_warn(date(2026, 9, 23))
        assert clock.calendar_horizon_warn(date(2028, 11, 1))
        assert clock.CALENDAR_HORIZON_WARN_SESSIONS == 60

    def test_trex_calendar_env_still_overrides(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TREX_CALENDAR", str(PROTOCOL))
        monkeypatch.setattr(clock, "_calendar", None)
        assert clock.calendar_last_session() == date(2026, 12, 31)
