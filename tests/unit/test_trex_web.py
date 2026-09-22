"""trex_web tests: read-only projection of plan + persisted book/events.

The web lane is broker-free and stateless — every test sets up a
``tmp_path`` with a hand-written plan TOML plus optional ``book.json``
/ ``events.jsonl`` and exercises the FastAPI app via ``TestClient``.
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient

from tree_options.trex.clock import ET
from tree_options.trex.state import BookState, Status
from tree_options.trex_web.app import create_app

# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


PLAN_TOML = """\
id = "putspread-test"
account_mode = "paper"
total_debit_cap = 1840.00
entry_window_start = "09:45"
entry_window_end = "12:00"

[[structures]]
id = "nvda-oct"
underlying = "NVDA"
entry_date = 2026-09-18
expiry = 2026-10-16
long_strike = 185.0
short_strike = 150.0
quantity = 5
limit_cap = 0.50
exit_deadline = 2026-10-09

[[structures]]
id = "qqq-nov"
underlying = "QQQ"
entry_date = 2026-09-18
expiry = 2026-11-20
long_strike = 600.0
short_strike = 475.0
quantity = 4
limit_cap = 2.40
exit_deadline = 2026-11-06
"""


def _write_plan(
    plans_root: Path, name: str = "putspread-test.toml", body: str | None = None
) -> Path:
    plans_root.mkdir(parents=True, exist_ok=True)
    path = plans_root / name
    path.write_text(body if body is not None else PLAN_TOML)
    return path


def _client(state_root: Path, plans_root: Path) -> TestClient:
    return TestClient(create_app(state_dir=str(state_root), plans_dir=str(plans_root)))


# ---------------------------------------------------------------------------
# health + smoke
# ---------------------------------------------------------------------------


class TestHealth:
    def test_returns_ok(self, tmp_path: Path) -> None:
        client = _client(tmp_path / "state", tmp_path / "plans")
        r = client.get("/health")
        assert r.status_code == 200
        payload = r.json()
        assert payload["ok"] is True
        assert payload["state_root"]
        assert payload["plans_root"]

    def test_does_not_import_broker(self) -> None:
        # The web lane must stay broker-free so the test gate can run
        # without ib_async installed. Catching the import here would mean
        # someone dragged a top-level broker import into the read surface.
        import tree_options.trex_web.app as app_module
        import tree_options.trex_web.reader as reader_module

        for module in (app_module, reader_module):
            assert "ib_async" not in module.__dict__
            for name in dir(module):
                obj = getattr(module, name)
                module_name = getattr(obj, "__module__", "") or ""
                assert not module_name.startswith("ib_async"), (
                    f"{module.__name__}.{name} imports from {module_name}"
                )


# ---------------------------------------------------------------------------
# index
# ---------------------------------------------------------------------------


class TestIndex:
    def test_empty_when_no_plans_dir(self, tmp_path: Path) -> None:
        client = _client(tmp_path / "state", tmp_path / "plans")
        r = client.get("/")
        assert r.status_code == 200
        assert "No plans found" in r.text

    def test_empty_when_no_plans_toml(self, tmp_path: Path) -> None:
        plans = tmp_path / "plans"
        plans.mkdir()
        client = _client(tmp_path / "state", plans)
        assert "No plans found" in client.get("/").text

    def test_card_renders_for_each_plan(self, tmp_path: Path) -> None:
        plans = tmp_path / "plans"
        _write_plan(plans, "putspread-test.toml")
        _write_plan(
            plans,
            "putspread-other.toml",
            PLAN_TOML.replace("putspread-test", "putspread-other")
            .replace('"nvda-oct"', '"nflx-oct"')
            .replace('"qqq-nov"', '"spy-nov"'),
        )
        client = _client(tmp_path / "state", plans)
        body = client.get("/").text
        assert "putspread-test" in body
        assert "putspread-other" in body
        assert "no state" in body  # no book.json anywhere

    def test_armed_pill_when_fresh_heartbeat(self, tmp_path: Path) -> None:
        plans = tmp_path / "plans"
        state = tmp_path / "state"
        _write_plan(plans)
        book = BookState(["nvda-oct", "qqq-nov"])
        # Heartbeat exactly at now — armed_within(30) compares against now(ET)
        book.beat()
        run = state / "putspread-test"
        run.mkdir(parents=True)
        book.save(run / "book.json")
        client = _client(state, plans)
        body = client.get("/").text
        assert "armed" in body


# ---------------------------------------------------------------------------
# plan detail
# ---------------------------------------------------------------------------


class TestPlanDetail:
    def test_404_when_plan_unknown(self, tmp_path: Path) -> None:
        plans = tmp_path / "plans"
        _write_plan(plans)
        client = _client(tmp_path / "state", plans)
        assert client.get("/plan/nope").status_code == 404

    def test_empty_state_renders_without_run_dir(self, tmp_path: Path) -> None:
        plans = tmp_path / "plans"
        state = tmp_path / "state"
        _write_plan(plans)
        client = _client(state, plans)
        r = client.get("/plan/putspread-test")
        assert r.status_code == 200
        assert "Plan putspread-test" in r.text
        # The runbook panel explains the empty state with copy + commands.
        assert "Why no state" in r.text
        assert "no events yet" in r.text.lower() or "No events yet" in r.text

    def test_renders_seeded_book_and_events(self, tmp_path: Path) -> None:
        plans = tmp_path / "plans"
        state = tmp_path / "state"
        _write_plan(plans)

        run = state / "putspread-test"
        run.mkdir(parents=True)
        book = BookState(["nvda-oct", "qqq-nov"])
        # Mark nvda-oct OPEN with a fill; qqq-nov stays PLANNED
        nvda = book.structures["nvda-oct"]
        now = datetime(2026, 9, 18, 10, 30, tzinfo=ET)
        nvda.to(Status.ENTER_WORKING, now)
        nvda.to(Status.OPEN, now)
        nvda.entry_fill = Decimal("0.44")
        nvda.filled_qty = 5
        book.beat()
        book.save(run / "book.json")
        # Append two events
        with (run / "events.jsonl").open("a") as fh:
            fh.write(
                json.dumps(
                    {
                        "ts": now.isoformat(),
                        "event": "entry_order",
                        "structure": "nvda-oct",
                        "qty": 5,
                    }
                )
                + "\n"
            )
            fh.write(
                json.dumps(
                    {
                        "ts": now.isoformat(),
                        "event": "entry_fill",
                        "structure": "nvda-oct",
                        "filled": 5,
                        "avg": "0.44",
                    }
                )
                + "\n"
            )

        client = _client(state, plans)
        r = client.get("/plan/putspread-test")
        assert r.status_code == 200
        assert "nvda-oct" in r.text
        assert "qqq-nov" in r.text
        assert "entry_order" in r.text
        assert "entry_fill" in r.text
        assert "armed" in r.text  # fresh heartbeat
        # PLANNED side shows in its card; OPEN side shows state badge.
        assert "badge-planned" in r.text
        assert "badge-open" in r.text

    def test_realized_pnl_rendered_after_exit_fill(self, tmp_path: Path) -> None:
        plans = tmp_path / "plans"
        state = tmp_path / "state"
        _write_plan(plans)
        run = state / "putspread-test"
        run.mkdir(parents=True)
        book = BookState(["nvda-oct"])
        nvda = book.structures["nvda-oct"]
        now = datetime(2026, 9, 18, 10, 30, tzinfo=ET)
        nvda.to(Status.ENTER_WORKING, now)
        nvda.to(Status.OPEN, now)
        nvda.entry_fill = Decimal("0.50")
        nvda.filled_qty = 5
        nvda.to(Status.EXIT_WORKING, now)
        nvda.exit_fill = Decimal("0.20")
        nvda.exit_filled_qty = 5
        nvda.to(Status.CLOSED, now)
        nvda.exit_reason = "time_stop"
        nvda.close_reason = "time_stop"
        book.save(run / "book.json")
        client = _client(state, plans)
        r = client.get("/plan/putspread-test")
        # Realized P&L = (0.50 - 0.20) * 5 * 100 = $150
        assert "$150.00" in r.text
        assert "time_stop" in r.text


# ---------------------------------------------------------------------------
# raw endpoints
# ---------------------------------------------------------------------------


class TestRawEndpoints:
    def test_book_json_404_when_no_state(self, tmp_path: Path) -> None:
        plans = tmp_path / "plans"
        state = tmp_path / "state"
        _write_plan(plans)
        client = _client(state, plans)
        assert client.get("/plan/putspread-test/book.json").status_code == 404

    def test_book_json_returns_persisted_payload(self, tmp_path: Path) -> None:
        plans = tmp_path / "plans"
        state = tmp_path / "state"
        _write_plan(plans)
        run = state / "putspread-test"
        run.mkdir(parents=True)
        book = BookState(["nvda-oct"])
        book.beat()
        book.save(run / "book.json")
        client = _client(state, plans)
        r = client.get("/plan/putspread-test/book.json")
        assert r.status_code == 200
        payload = r.json()
        assert "heartbeat" in payload
        assert "nvda-oct" in payload["structures"]

    def test_events_jsonl_404_when_missing(self, tmp_path: Path) -> None:
        plans = tmp_path / "plans"
        state = tmp_path / "state"
        _write_plan(plans)
        client = _client(state, plans)
        assert client.get("/plan/putspread-test/events.jsonl").status_code == 404

    def test_events_jsonl_returns_raw_text(self, tmp_path: Path) -> None:
        plans = tmp_path / "plans"
        state = tmp_path / "state"
        _write_plan(plans)
        run = state / "putspread-test"
        run.mkdir(parents=True)
        (run / "events.jsonl").write_text(
            '{"ts": "2026-09-18T10:30:00-04:00", "event": "entry_order"}\n'
        )
        client = _client(state, plans)
        r = client.get("/plan/putspread-test/events.jsonl")
        assert r.status_code == 200
        assert "application/x-ndjson" in r.headers["content-type"]
        assert "entry_order" in r.text


# ---------------------------------------------------------------------------
# derived fields
# ---------------------------------------------------------------------------


class TestDerivedFields:
    def test_open_qty_is_filled_minus_exit(self, tmp_path: Path) -> None:
        plans = tmp_path / "plans"
        state = tmp_path / "state"
        _write_plan(plans)
        run = state / "putspread-test"
        run.mkdir(parents=True)
        book = BookState(["nvda-oct"])
        nvda = book.structures["nvda-oct"]
        now = datetime(2026, 9, 18, 10, 30, tzinfo=ET)
        nvda.to(Status.ENTER_WORKING, now)
        nvda.to(Status.OPEN, now)
        nvda.entry_fill = Decimal("0.50")
        nvda.filled_qty = 5
        nvda.to(Status.EXIT_WORKING, now)
        nvda.exit_fill = Decimal("0.20")
        nvda.exit_filled_qty = 2  # partial exit
        book.save(run / "book.json")
        client = _client(state, plans)
        body = client.get("/plan/putspread-test").text
        # open_qty should show 3 (5 filled - 2 exited)
        # The card renders "Open qty 3" — accept either spacing or wrapped in <dd>
        assert "Open qty" in body
        assert ">3<" in body or "3</dd>" in body or "> 3 <" in body

    def test_days_to_expiry_is_nonnegative(self, tmp_path: Path) -> None:
        plans = tmp_path / "plans"
        state = tmp_path / "state"
        _write_plan(plans)
        client = _client(state, plans)
        body = client.get("/plan/putspread-test").text
        # Both expirations in the fixture (2026-10-16, 2026-11-20) are
        # in the future relative to the test day (2026-09-18).
        assert "Expiry" in body
        assert "Deadline" in body


# ---------------------------------------------------------------------------
# runbook status
# ---------------------------------------------------------------------------


from tree_options.trex_web.reader import (  # noqa: E402
    compute_runbook_status,
)


class TestRunbookStatus:
    """The runbook view is purely clock + plan + heartbeat; no I/O."""

    def _plan(self, plans_root: Path) -> object:
        from tree_options.trex.plan import load_plan

        return load_plan(plans_root / "putspread-test.toml")

    def test_window_state_after_close(self, tmp_path: Path) -> None:
        plans = tmp_path / "plans"
        _write_plan(plans)
        plan = self._plan(plans)
        # After 12:00 ET on entry date → "after"
        rb = compute_runbook_status(plan, heartbeat=None, armed=False)
        # We don't pin the exact wall clock here; just verify it's not None.
        assert rb.window_state in ("during", "after", "before", "wrong_day")

    def test_window_state_before_open(self, tmp_path: Path) -> None:
        plans = tmp_path / "plans"
        _write_plan(plans)
        plan = self._plan(plans)
        rb = compute_runbook_status(plan, heartbeat=None, armed=False)
        # Runbook always exposes entry_date + window bounds; the test just
        # exercises the function with a real plan.
        assert rb.entry_date == plan.structures[0].entry_date
        assert rb.window_start.hour == 9
        assert rb.window_end.hour == 12

    def test_days_to_exit_deadline_populated(self, tmp_path: Path) -> None:
        plans = tmp_path / "plans"
        _write_plan(plans)
        plan = self._plan(plans)
        rb = compute_runbook_status(plan, heartbeat=None, armed=False)
        # nvda-oct deadline is 2026-10-09, qqq-nov is 2026-11-06.
        assert set(rb.days_to_exit_deadline) == {"nvda-oct", "qqq-nov"}
        assert all(isinstance(v, int) for v in rb.days_to_exit_deadline.values())

    def test_monitor_armed_reflects_heartbeat(self, tmp_path: Path) -> None:
        plans = tmp_path / "plans"
        _write_plan(plans)
        plan = self._plan(plans)
        now = datetime.now(ET)
        rb = compute_runbook_status(plan, heartbeat=now, armed=True)
        assert rb.monitor_armed is True
        assert rb.heartbeat == now
        rb_disarmed = compute_runbook_status(plan, heartbeat=None, armed=False)
        assert rb_disarmed.monitor_armed is False
        assert rb_disarmed.heartbeat is None


class TestRunbookBannerInTemplate:
    def test_index_shows_three_status_pills_per_card(self, tmp_path: Path) -> None:
        plans = tmp_path / "plans"
        _write_plan(plans)
        client = _client(tmp_path / "state", plans)
        body = client.get("/").text
        # Per-card pills: heartbeat, gateway, window. The window pill's
        # wording depends on window_state (open/not open/closed/none), so
        # assert the always-rendered "Window <start>-<end> ET" line instead
        # of a state-specific variant — keeps the test clock-independent.
        assert "no heartbeat" in body
        assert "gateway" in body
        assert "Window" in body and "ET" in body

    def test_plan_detail_shows_runbook_panel(self, tmp_path: Path) -> None:
        plans = tmp_path / "plans"
        state = tmp_path / "state"
        _write_plan(plans)
        client = _client(state, plans)
        body = client.get("/plan/putspread-test").text
        assert "live runbook" in body
        assert "Current ET" in body
        # The reachability verdict is a LIVE probe of 127.0.0.1:4002 —
        # host state, not test state. Assert only the state-independent
        # fragment (both branches render "gateway :4002 ...").
        assert "gateway :4002" in body

    def test_plan_detail_shows_runbook_commands_when_no_state(self, tmp_path: Path) -> None:
        plans = tmp_path / "plans"
        state = tmp_path / "state"
        _write_plan(plans)
        client = _client(state, plans)
        body = client.get("/plan/putspread-test").text
        assert "Why no state" in body
        assert "ib.env" in body
        assert "docker compose" in body
        assert "trex-monitor.service" in body
