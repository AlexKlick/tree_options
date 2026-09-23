"""Exit-machine watchdog: is anything actually guarding the open positions?

The gateway watchdog covers the broker session; this covers the monitor
itself. The monitor can be dead (no heartbeat), or alive with every tick
failing (caught and logged, systemd still says "running"). Either way the
book has no touch or time-stop exits, and until 2026-09-23 nothing told
the operator. A monitor that is down because the gateway is down is the
gateway's alarm, not a second one.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from tree_options.trex.alert_policy import REMIND_EVERY_S, REMIND_MARKET_S, Urgency
from tree_options.trex.exit_watch import (
    ATTRIBUTION_MAX_S,
    GATEWAY_SETTLE_S,
    HEALTH_STALE_S,
    HEARTBEAT_STALE_S,
    TICK_FAILURES_BAD,
    TOUCH_BLIND_S,
    BookObs,
    ExitObs,
    classify,
    decide,
    scan_books,
    watch_once,
)

ET = ZoneInfo("America/New_York")
DEPLOY = Path(__file__).resolve().parents[2] / "deploy" / "trex"
LOUD = Urgency("high", REMIND_MARKET_S, quiet=False)
DAY = Urgency("default", REMIND_EVERY_S, quiet=False)


def _et(h: int, mi: int = 0) -> float:
    return datetime(2026, 9, 23, h, mi, tzinfo=ET).timestamp()  # a Wednesday session


NOON = _et(12)


def _health(now: float, failures: int = 0, ok_at: float | None = None,
            error: str | None = None) -> dict[str, Any]:
    return {"at": now, "connected": True, "tick_failures": failures,
            "last_tick_ok_at": now if ok_at is None and failures == 0 else ok_at,
            "last_error": error}


def _obs(books: list[BookObs], *, now: float = NOON, gateway: str | None = "ok",
         gateway_since: float | None = NOON - 3600, market: bool = True,
         touch_window: bool | None = None) -> ExitObs:
    return ExitObs(now=now, books=books, gateway_status=gateway, gateway_since=gateway_since,
                   market=market, unit_state="active", touch_window=touch_window)


def _fresh(plan: str = "putspread-20260922", **health: Any) -> BookObs:
    return BookObs(plan, NOON - 20, _health(NOON, **health))


def _dead(plan: str = "putspread-20260922", age: float = HEARTBEAT_STALE_S + 60) -> BookObs:
    return BookObs(plan, NOON - age, None)


class TestClassify:
    def test_no_open_positions_is_idle(self) -> None:
        assert classify(_obs([]))[0] == "idle"

    def test_fresh_heartbeat_and_good_ticks_is_ok(self) -> None:
        assert classify(_obs([_fresh()]))[0] == "ok"

    def test_stale_heartbeat_with_a_settled_gateway_is_monitor_down(self) -> None:
        status, since, detail = classify(_obs([_dead(age=600)]))
        assert status == "monitor_down"
        assert since == NOON - 600  # the last sign of life
        assert "putspread-20260922" in detail

    @pytest.mark.parametrize("gateway", ["needs_login", "needs_2fa", "api_down", "down",
                                         "starting", "checking"])
    def test_a_gateway_outage_is_the_gateways_alarm(self, gateway: str) -> None:
        assert classify(_obs([_dead()], gateway=gateway))[0] == "waiting_for_gateway"

    def test_just_recovered_gateway_gives_the_monitor_time_to_start(self) -> None:
        obs = _obs([_dead()], gateway_since=NOON - GATEWAY_SETTLE_S + 30)
        assert classify(obs)[0] == "waiting_for_gateway"

    def test_silent_gateway_watchdog_cannot_take_the_blame(self) -> None:
        assert classify(_obs([_dead()], gateway=None, gateway_since=None))[0] == "monitor_down"

    def test_failing_ticks_in_market_hours(self) -> None:
        book = _fresh(failures=TICK_FAILURES_BAD, ok_at=NOON - 70, error="TimeoutError")
        status, since, detail = classify(_obs([book]))
        assert status == "monitor_failing"
        assert since == NOON - 70
        assert "TimeoutError" in detail

    def test_failing_ticks_off_hours_are_not_an_alarm(self) -> None:
        book = _fresh(failures=TICK_FAILURES_BAD, ok_at=NOON - 70, error="TimeoutError")
        assert classify(_obs([book], market=False))[0] == "ok"

    def test_failing_ticks_during_a_gateway_blip_are_the_gateways(self) -> None:
        book = _fresh(failures=TICK_FAILURES_BAD, ok_at=NOON - 70, error="ConnectionError")
        assert classify(_obs([book], gateway="checking"))[0] == "waiting_for_gateway"

    def test_missing_tick_health_gets_a_grace_then_alarms(self) -> None:
        """Codex P1: a monitor beating but never finishing a loop (killed
        mid-tick, or health writes failing) read as ok forever."""
        book = BookObs("p", NOON - 20, None)
        status, _, detail = classify(_obs([book]), health_missing_since={"p": NOON - 60})
        assert status == "ok" and "not reported" in detail
        status, since, _ = classify(_obs([book]),
                                    health_missing_since={"p": NOON - HEALTH_STALE_S - 1})
        assert status == "monitor_failing" and since == NOON - HEALTH_STALE_S - 1

    def test_stale_tick_health_alarms_at_any_hour(self) -> None:
        stale = BookObs("p", NOON - 20, _health(NOON - HEALTH_STALE_S - 60))
        status, _, detail = classify(_obs([stale], market=False))
        assert status == "monitor_failing" and "last reported" in detail

    def test_unreadable_book_is_never_idle(self) -> None:
        """Codex P1: a corrupt book.json was dropped, so the only exposed
        book vanished and the verdict became "idle" (and "back")."""
        status, _, detail = classify(_obs([BookObs("p", None, None, unreadable=True)]))
        assert status == "monitor_down" and "unreadable" in detail

    @pytest.mark.parametrize("gateway", ["starting", "checking", "ok"])
    def test_a_quiet_gateway_cannot_cover_a_long_monitor_outage(self, gateway: str) -> None:
        """Codex P1: a gateway flapping checking/ok never reaches an alarm, and
        its "ok" never settles, so both watchdogs stayed silent forever."""
        dead = _dead(age=ATTRIBUTION_MAX_S + 60)
        status, _, _ = classify(_obs([dead], gateway=gateway, gateway_since=NOON - 30))
        assert status == "monitor_down"

    def test_a_gateway_alarm_covers_a_long_outage(self) -> None:
        dead = _dead(age=3 * 3600)
        assert classify(_obs([dead], gateway="needs_login"))[0] == "waiting_for_gateway"

    def test_worst_book_wins(self) -> None:
        status, _, detail = classify(_obs([_fresh("a"), _dead("b")]))
        assert status == "monitor_down" and "b" in detail


class TestDecide:
    def test_monitor_down_pushes_without_urls_or_money(self) -> None:
        state, actions = decide(_obs([_dead(age=600)]), {}, urgency=LOUD)
        assert state["status"] == "monitor_down"
        (note,) = actions
        assert note.priority == "high" and "exit machine" in note.title.lower()
        assert "http" not in note.message and "$" not in note.message

    def test_waiting_for_gateway_sends_nothing(self) -> None:
        state, actions = decide(_obs([_dead()], gateway="needs_login"), {}, urgency=LOUD)
        assert state["status"] == "waiting_for_gateway" and actions == []

    def test_recovery_after_a_delivered_alarm(self) -> None:
        prior = {"status": "monitor_down", "since": NOON - 900,
                 "last_notified_status": "monitor_down", "last_notified_at": NOON - 600}
        state, actions = decide(_obs([_fresh()]), prior, urgency=DAY)
        assert state["status"] == "ok"
        assert [a.title for a in actions] == ["trex: exit machine back"]

    def test_state_carries_per_book_rows_for_the_banner(self) -> None:
        state, _ = decide(_obs([_fresh("a"), _dead("b")]), {}, urgency=LOUD)
        rows = {r["plan"]: r["status"] for r in state["books"]}
        assert rows == {"a": "ok", "b": "monitor_down"}


def _write_book(root: Path, plan: str, statuses: tuple[str, ...], heartbeat: float | None,
                health: dict[str, Any] | None = None) -> None:
    d = root / plan
    d.mkdir(parents=True)
    hb = datetime.fromtimestamp(heartbeat, ET).isoformat() if heartbeat is not None else None
    structures = {f"s{i}": {"status": s} for i, s in enumerate(statuses)}
    (d / "book.json").write_text(json.dumps({"heartbeat": hb, "structures": structures}))
    if health is not None:
        (d / "monitor.json").write_text(json.dumps(health))


class TestScanBooks:
    def test_only_books_with_exposure(self, tmp_path: Path) -> None:
        _write_book(tmp_path, "done", ("closed", "closed"), NOON - 99_999)
        _write_book(tmp_path, "planned", ("planned",), None)
        _write_book(tmp_path, "live", ("open", "closed"), NOON - 20, _health(NOON))
        _write_book(tmp_path, "entering", ("enter_working",), NOON - 20)
        (tmp_path / "gateway.json").write_text("{}")  # not a run dir
        (tmp_path / "junk").mkdir()
        (tmp_path / "junk" / "book.json").write_text("{nope")
        books = {b.plan: b for b in scan_books(tmp_path)}
        assert set(books) == {"live", "entering", "junk"}  # unreadable = can't rule it out
        assert books["junk"].unreadable and not books["live"].unreadable
        assert books["live"].heartbeat == pytest.approx(NOON - 20)
        assert books["live"].health is not None and books["entering"].health is None

    def test_no_state_dir_yet_is_no_books(self, tmp_path: Path) -> None:
        assert scan_books(tmp_path / "absent") == []

    def test_an_unlistable_state_dir_fails_loudly(self, tmp_path: Path) -> None:
        not_a_dir = tmp_path / "file"
        not_a_dir.write_text("")
        with pytest.raises(OSError):
            scan_books(not_a_dir)  # the run fails; the banner goes stale, never "idle"


class TestWatchOnce:
    def _gateway(self, tmp_path: Path, status: str, checked_at: float) -> Path:
        path = tmp_path / "gateway.json"
        path.write_text(json.dumps({"status": status, "since": NOON - 3600,
                                    "checked_at": checked_at}))
        return path

    def test_dead_monitor_end_to_end(self, tmp_path: Path) -> None:
        root = tmp_path / "state"
        _write_book(root, "putspread-20260922", ("open",), NOON - 600)
        sent: list[tuple[str, str, str]] = []
        state = watch_once(
            tmp_path / "exit_watch.json", root=root,
            gateway_state=self._gateway(tmp_path, "ok", NOON - 30),
            notify=lambda t, m, p: sent.append((t, m, p)) or True,
            now=NOON, urgency=LOUD, unit_state="failed",
        )
        assert state["status"] == "monitor_down" and len(sent) == 1
        assert "failed" in state["detail"]
        saved = json.loads((tmp_path / "exit_watch.json").read_text())
        assert saved["status"] == "monitor_down"

    def test_stale_gateway_file_is_not_trusted(self, tmp_path: Path) -> None:
        root = tmp_path / "state"
        _write_book(root, "p", ("open",), NOON - 600)
        state = watch_once(
            tmp_path / "exit_watch.json", root=root,
            gateway_state=self._gateway(tmp_path, "needs_login", NOON - 3600),
            notify=lambda *a: True, now=NOON, urgency=LOUD, unit_state=None,
        )
        assert state["status"] == "monitor_down"  # a dead gateway watchdog blames no one

    def test_failed_push_is_retried(self, tmp_path: Path) -> None:
        root = tmp_path / "state"
        _write_book(root, "p", ("open",), NOON - 600)
        gw = self._gateway(tmp_path, "ok", NOON - 30)
        path = tmp_path / "exit_watch.json"
        first = watch_once(path, root=root, gateway_state=gw, notify=lambda *a: False,
                           now=NOON, urgency=LOUD, unit_state=None)
        assert first["last_notified_status"] is None and first["notify_failed_at"] == NOON

    def test_dry_run_writes_and_sends_nothing(self, tmp_path: Path) -> None:
        root = tmp_path / "state"
        _write_book(root, "p", ("open",), NOON - 600)
        sent: list[object] = []
        watch_once(tmp_path / "x.json", root=root,
                   gateway_state=self._gateway(tmp_path, "ok", NOON - 30),
                   notify=lambda *a: sent.append(a) or True, now=NOON, urgency=LOUD,
                   unit_state=None, dry_run=True)
        assert sent == [] and not (tmp_path / "x.json").exists()


def test_units_run_the_exit_watch_every_minute() -> None:
    service = (DEPLOY / "trex-exit-watch.service").read_text()
    assert "tree_options.trex.exit_watch" in service and "TimeoutStartSec=" in service
    timer = (DEPLOY / "trex-exit-watch.timer").read_text()
    assert "OnUnitActiveSec=60s" in timer and "WantedBy=timers.target" in timer


def test_missing_health_is_timed_across_runs(tmp_path: Path) -> None:
    """The grace for an absent monitor.json is measured by the watchdog
    itself (persisted per plan), since the heartbeat can't say when the
    monitor started."""
    root = tmp_path / "state"
    _write_book(root, "p", ("open",), NOON - 20)
    path = tmp_path / "exit_watch.json"
    gw = tmp_path / "gateway.json"
    gw.write_text(json.dumps({"status": "ok", "since": NOON - 3600, "checked_at": NOON}))
    first = watch_once(path, root=root, gateway_state=gw, notify=lambda *a: True, now=NOON,
                       urgency=LOUD, unit_state=None)
    assert first["status"] == "ok" and first["health_missing_since"] == {"p": NOON}
    (root / "p" / "book.json").write_text(json.dumps({
        "heartbeat": datetime.fromtimestamp(NOON + HEALTH_STALE_S, ET).isoformat(),
        "structures": {"s0": {"status": "open"}}}))
    gw.write_text(json.dumps({"status": "ok", "since": NOON - 3600,
                              "checked_at": NOON + HEALTH_STALE_S + 1}))
    later = watch_once(path, root=root, gateway_state=gw, notify=lambda *a: True,
                       now=NOON + HEALTH_STALE_S + 1, urgency=LOUD, unit_state=None)
    assert later["status"] == "monitor_failing"


def _blind_book(since: float, plan: str = "p", **health: Any) -> BookObs:
    """A healthy monitor whose touch-guarded NVDA has had no accepted spot."""
    blind = {"NVDA": datetime.fromtimestamp(since, ET).isoformat()}
    return BookObs(plan, NOON - 20, {**_health(NOON, **health), "spot_blind": blind})


def _guarded(
    guarded: list[str] | None = None, ok: list[str] | None = None, plan: str = "p"
) -> BookObs:
    """A healthy monitor guarding ``guarded`` with fresh prices for ``ok``."""
    fresh = {s: datetime.fromtimestamp(NOON - 960, ET).isoformat() for s in ok or []}
    health = {**_health(NOON), "spot_blind": {}, "touch_guarded": guarded or ["NVDA"],
              "spot_ok": fresh}
    return BookObs(plan, NOON - 20, health)


class TestTouchBlind:
    """E0: the paper account has no equity quotes, so a monitor can tick
    fine while its touch exit has no price to act on. Ten blind minutes in
    market hours is an alarm, routed like the others (quiet hours hold)."""

    def test_blind_for_ten_minutes_in_market_hours(self) -> None:
        status, since, detail = classify(_obs([_blind_book(NOON - TOUCH_BLIND_S)]))
        assert status == "touch_blind"
        assert since == pytest.approx(NOON - TOUCH_BLIND_S)
        assert "NVDA" in detail

    def test_a_short_gap_is_not_an_alarm(self) -> None:
        assert classify(_obs([_blind_book(NOON - TOUCH_BLIND_S + 30)]))[0] == "ok"

    def test_off_hours_blindness_is_not_an_alarm(self) -> None:
        assert classify(_obs([_blind_book(NOON - 3600)], market=False))[0] == "ok"

    def test_a_down_monitor_outranks_a_blind_one(self) -> None:
        status, _, detail = classify(_obs([_blind_book(NOON - 3600, "a"), _dead("b")]))
        assert status == "monitor_down" and "b" in detail

    def test_failing_ticks_outrank_blindness(self) -> None:
        book = _blind_book(NOON - 3600, failures=TICK_FAILURES_BAD, ok_at=NOON - 70,
                           error="TimeoutError")
        assert classify(_obs([book]))[0] == "monitor_failing"

    def test_blind_outranks_a_book_waiting_on_the_gateway(self) -> None:
        obs = _obs([_blind_book(NOON - 3600, "a"), _dead("b")], gateway="needs_login")
        assert classify(obs)[0] == "touch_blind"

    def test_garbage_entries_are_ignored(self) -> None:
        health = {**_health(NOON), "spot_blind": {"NVDA": 12, "AMD": "not a time", "X": None}}
        assert classify(_obs([BookObs("p", NOON - 20, health)]))[0] == "ok"
        weird = {**_health(NOON), "spot_blind": ["NVDA"]}
        assert classify(_obs([BookObs("p", NOON - 20, weird)]))[0] == "ok"

    def test_alarm_names_the_symbol_and_nothing_sensitive(self) -> None:
        state, actions = decide(_obs([_blind_book(NOON - 900)]), {}, urgency=LOUD)
        assert state["status"] == "touch_blind"
        assert state["books"][0]["status"] == "touch_blind"
        (note,) = actions
        assert note.title == "trex: touch exit blind" and note.priority == "high"
        assert "NVDA" in note.message and "15m" in note.message
        for banned in ("http", "$", ".ts.net", "DU", "p:"):
            assert banned not in note.message

    def test_quiet_hours_hold_the_alarm(self) -> None:
        quiet = Urgency("high", REMIND_MARKET_S, quiet=True)
        state, actions = decide(_obs([_blind_book(NOON - 900)]), {}, urgency=quiet)
        assert actions == [] and state["notify_held"] is True

    def test_recovery_says_the_touch_exit_is_back(self) -> None:
        prior = {"status": "touch_blind", "since": NOON - 1800, "touch_symbols": ["NVDA"],
                 "last_notified_status": "touch_blind", "last_notified_at": NOON - 900}
        state, actions = decide(_obs([_guarded(ok=["NVDA"])]), prior, urgency=DAY)
        assert state["status"] == "ok"
        (note,) = actions
        assert note.title == "trex: touch exit back" and "30m" in note.message
        assert "Fresh prices" in note.message

    def test_the_calendar_close_ends_detection(self) -> None:
        """Codex P2: on a 13:00 early close the fixed 16:15 window alarmed
        for a normal closed market."""
        book = _blind_book(NOON - 3600)
        assert classify(_obs([book], touch_window=False))[0] == "ok"

    def test_session_end_suspends_the_incident_instead_of_recovering(self) -> None:
        """Codex P2: at the close an unresolved blind incident read as ok
        and pushed "fresh prices again" without any price."""
        prior = {"status": "touch_blind", "since": NOON - 1800, "touch_symbols": ["NVDA"],
                 "last_notified_status": "touch_blind", "last_notified_at": NOON - 900}
        state, actions = decide(_obs([_guarded()], market=False, touch_window=False), prior,
                                urgency=DAY)
        assert state["status"] == "touch_suspended" and actions == []
        assert state["touch_symbols"] == ["NVDA"]
        assert state["last_notified_status"] == "touch_blind"  # still owed a recovery

    def test_no_price_yet_at_the_next_open_stays_suspended(self) -> None:
        prior = {"status": "touch_suspended", "since": NOON - 60_000, "touch_symbols": ["NVDA"],
                 "last_notified_status": "touch_blind", "last_notified_at": NOON - 70_000}
        state, actions = decide(_obs([_guarded()]), prior, urgency=LOUD)
        assert state["status"] == "touch_suspended" and actions == []

    def test_a_price_next_session_is_the_recovery(self) -> None:
        prior = {"status": "touch_suspended", "since": NOON - 60_000, "touch_symbols": ["NVDA"],
                 "last_notified_status": "touch_blind", "last_notified_at": NOON - 70_000}
        state, actions = decide(_obs([_guarded(ok=["NVDA"])]), prior, urgency=LOUD)
        assert state["status"] == "ok"
        (note,) = actions
        assert note.title == "trex: touch exit back" and "Fresh prices" in note.message

    def test_still_blind_next_session_alarms_again(self) -> None:
        prior = {"status": "touch_suspended", "since": NOON - 60_000, "touch_symbols": ["NVDA"],
                 "last_notified_status": "touch_blind", "last_notified_at": NOON - 70_000}
        state, actions = decide(_obs([_blind_book(NOON - 900)]), prior, urgency=LOUD)
        assert state["status"] == "touch_blind"
        assert [a.title for a in actions] == ["trex: touch exit blind"]

    def test_closing_the_exposure_ends_the_incident(self) -> None:
        prior = {"status": "touch_suspended", "since": NOON - 60_000, "touch_symbols": ["NVDA"],
                 "last_notified_status": "touch_blind", "last_notified_at": NOON - 70_000}
        state, actions = decide(_obs([]), prior, urgency=DAY)
        assert state["status"] == "idle"
        (note,) = actions
        assert note.title == "trex: touch exit back"
        assert note.message == "No open positions left to guard."

    def test_the_blind_position_closing_ends_it_without_claiming_prices(self) -> None:
        prior = {"status": "touch_suspended", "since": NOON - 60_000, "touch_symbols": ["NVDA"],
                 "last_notified_status": "touch_blind", "last_notified_at": NOON - 70_000}
        other = _guarded(["AMD"], ok=["AMD"])
        state, actions = decide(_obs([other], market=False, touch_window=False), prior,
                                urgency=DAY)
        assert state["status"] == "ok"
        (note,) = actions
        assert "Fresh prices" not in note.message and "closed" in note.message

    def test_watch_once_bounds_detection_by_the_session_calendar(self, tmp_path: Path) -> None:
        """13:30 on the 13:00 early close of 2026-11-27: inside the watch's
        09:00-16:15 window, outside the market's."""
        now = datetime(2026, 11, 27, 13, 30, tzinfo=ET).timestamp()
        root = tmp_path / "state"
        health = {"at": now, "connected": True, "tick_failures": 0, "last_tick_ok_at": now,
                  "spot_blind": {"NVDA": datetime.fromtimestamp(now - 3600, ET).isoformat()},
                  "touch_guarded": ["NVDA"], "spot_ok": {}}
        _write_book(root, "p", ("open",), now - 20, health)
        gw = tmp_path / "gateway.json"
        gw.write_text(json.dumps({"status": "ok", "since": now - 9999, "checked_at": now}))
        state = watch_once(tmp_path / "exit_watch.json", root=root, gateway_state=gw,
                           notify=lambda *a: True, now=now, urgency=LOUD, unit_state=None)
        assert state["market"] is True and state["status"] == "ok"

    def test_calendar_horizon_warning_reaches_the_book_row(self) -> None:
        book = BookObs("p", NOON - 20, {**_health(NOON), "calendar_horizon_warn": True})
        state, _ = decide(_obs([book]), {}, urgency=LOUD)
        assert state["books"][0]["calendar_horizon_warn"] is True
