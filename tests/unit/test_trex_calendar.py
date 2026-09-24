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
from typing import Any, ClassVar
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
    def test_sessions_identical_through_2026_except_the_declared_overrides(self) -> None:
        protocol = _payload(PROTOCOL)["sessions"]
        trex = _payload(TREX)["sessions"]
        assert isinstance(protocol, list) and isinstance(trex, list)
        overrides = _payload(TREX)["closure_overrides"]
        assert isinstance(overrides, dict)
        assert [s for s in trex if s <= "2026-12-31"] == [s for s in protocol if s not in overrides]

    def test_early_closes_identical_through_2026(self) -> None:
        protocol = _payload(PROTOCOL)["early_close_sessions"]
        trex = _payload(TREX)["early_close_sessions"]
        assert isinstance(protocol, list) and isinstance(trex, list)
        assert [s for s in trex if s <= "2026-12-31"] == protocol

    def test_session_times_identical(self) -> None:
        protocol, trex = _payload(PROTOCOL), _payload(TREX)
        for key in ("calendar", "source", "timezone", "open", "close", "early_close"):
            assert trex[key] == protocol[key], key


class TestClosureOverrides:
    """exchange-calendars 4.5.2 (the pin) predates the NYSE's 2025-01-09
    closure; the trex calendar declares it, and changes nothing else."""

    # The pinned build (exchange-calendars==4.5.2, 2018-01-02..2028-12-31) as
    # it stood before any override: sha256 over "\n".join(sessions) and
    # "\n".join(early_close_sessions), taken from the pre-override file
    # (sha256 38d580af..., reproduced byte-for-byte by the pinned build on
    # 2026-09-23).
    PINNED_SESSIONS_SHA256 = "571133cf30cd183d3de5c7be2647abe906b1a77848dcec0821bb6e41267199cd"
    PINNED_SESSIONS = 2765
    PINNED_EARLY_SHA256 = "3296813407f8a3332f46cf3c5ea7d4918de1de32a1ebe565828b4a3ea3f3bc33"
    OVERRIDES: ClassVar[dict[str, str]] = {
        "2025-01-09": "NYSE closed: national day of mourning for former President Jimmy Carter"
    }

    def test_the_payload_declares_the_carter_closure(self) -> None:
        payload = _payload(TREX)
        assert payload["closure_overrides"] == self.OVERRIDES
        assert payload["source"] == "exchange-calendars==4.5.2", "the pin is unchanged"

    def test_2025_01_09_is_not_a_session(self, default_calendar: None) -> None:
        assert not clock.is_session(datetime(2025, 1, 9, 10, 0, tzinfo=ET))
        assert clock.is_session(datetime(2025, 1, 8, 10, 0, tzinfo=ET))
        assert clock.is_session(datetime(2025, 1, 10, 10, 0, tzinfo=ET))
        cal = clock.session_calendar()
        assert cal.nth_after(date(2025, 1, 8), 1) == date(2025, 1, 10)

    def test_every_other_session_and_early_close_is_the_pinned_build(self) -> None:
        payload = _payload(TREX)
        sessions, early = payload["sessions"], payload["early_close_sessions"]
        assert isinstance(sessions, list) and isinstance(early, list)
        rebuilt = sorted([*sessions, *self.OVERRIDES])
        assert len(rebuilt) == self.PINNED_SESSIONS
        digest = hashlib.sha256("\n".join(rebuilt).encode()).hexdigest()
        assert digest == self.PINNED_SESSIONS_SHA256
        assert hashlib.sha256("\n".join(early).encode()).hexdigest() == self.PINNED_EARLY_SHA256
        # the neighbouring half days are untouched
        assert {"2024-11-29", "2024-12-24", "2025-07-03"} <= set(early)

    def test_the_generator_applies_overrides_to_the_pinned_payload(self) -> None:
        gen = _generator()
        payload = {
            "calendar": "XNYS",
            "source": "exchange-calendars==4.5.2",
            "scope": "trex-execution",
            "early_close_sessions": ["2024-12-24"],
            "sessions": ["2024-12-24", "2025-01-08", "2025-01-09", "2025-01-10"],
        }
        out = gen.apply_closure_overrides(payload, self.OVERRIDES)
        assert out["sessions"] == ["2024-12-24", "2025-01-08", "2025-01-10"]
        assert out["early_close_sessions"] == ["2024-12-24"]
        assert out["closure_overrides"] == self.OVERRIDES
        assert list(out)[:3] == ["calendar", "source", "closure_overrides"]
        assert payload["sessions"][2] == "2025-01-09", "the input payload is not mutated"
        assert gen.CLOSURE_OVERRIDES == self.OVERRIDES

    def test_an_override_the_pinned_build_does_not_list_fails_loudly(self) -> None:
        gen = _generator()
        payload = {
            "calendar": "XNYS",
            "source": "x",
            "early_close_sessions": [],
            "sessions": ["2025-01-08"],
        }
        with pytest.raises(ValueError, match="2025-01-09"):
            gen.apply_closure_overrides(
                payload, self.OVERRIDES, start=date(2025, 1, 1), end=date(2025, 1, 31)
            )
        # outside the built range it simply does not apply
        out = gen.apply_closure_overrides(
            payload, self.OVERRIDES, start=date(2025, 2, 1), end=date(2025, 2, 28)
        )
        assert out["closure_overrides"] == {}


def _generator() -> Any:
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "gen_trex_calendar", REPO / "scripts" / "gen_trex_calendar.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
