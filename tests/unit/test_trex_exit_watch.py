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
    GATEWAY_SETTLE_S,
    HEARTBEAT_STALE_S,
    TICK_FAILURES_BAD,
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
         gateway_since: float | None = NOON - 3600, market: bool = True) -> ExitObs:
    return ExitObs(now=now, books=books, gateway_status=gateway, gateway_since=gateway_since,
                   market=market, unit_state="active")


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

    def test_monitor_without_health_reporting_is_judged_by_heartbeat_only(self) -> None:
        status, _, detail = classify(_obs([BookObs("p", NOON - 20, None)]))
        assert status == "ok" and "not reported" in detail

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
        assert set(books) == {"live", "entering"}
        assert books["live"].heartbeat == pytest.approx(NOON - 20)
        assert books["live"].health is not None and books["entering"].health is None


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
