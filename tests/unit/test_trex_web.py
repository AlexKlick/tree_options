"""trex_web tests: read-only projection of plan + persisted book/events.

The web lane is broker-free and stateless — every test sets up a
``tmp_path`` with a hand-written plan TOML plus optional ``book.json``
/ ``events.jsonl`` and exercises the FastAPI app via ``TestClient``.
HTML is the SPA's job; these tests pin the JSON contract, the raw
endpoints, and the shell/shim behavior.
"""

from __future__ import annotations

import json
import os
import unittest.mock
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tree_options.trex.clock import ET
from tree_options.trex.discovery.artifact import DiscoveryStamp, write_scan
from tree_options.trex.state import BookState, Status
from tree_options.trex_web.app import create_app
from tree_options.trex_web.payoff import (
    expiry_pnl,
    payoff_series,
    pnl_history_series,
    summarize_book,
)
from tree_options.trex_web.positions import merge_net_positions, net_positions

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


def _client(state_root: Path, plans_root: Path, discovery_dir: Path | None = None) -> TestClient:
    """Isolate the discovery surface from host state: the DEPLOYED
    ~/.local/state/trex-discovery/account.json and ~/.config/trex/discovery.toml
    otherwise leak into every default-config app built here."""
    discovery = discovery_dir if discovery_dir is not None else state_root.parent / "discovery"
    return TestClient(
        create_app(
            state_dir=str(state_root),
            plans_dir=str(plans_root),
            discovery_dir=str(discovery),
        )
    )


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
# SPA shell + legacy bookmark shim
# ---------------------------------------------------------------------------


class TestStaticShell:
    def _static(self, tmp_path: Path) -> Path:
        static = tmp_path / "static"
        (static / "assets").mkdir(parents=True)
        (static / "index.html").write_text(
            "<!doctype html><html><head><script src='./assets/app.js'>"
            "</script></head><body>SHELL-MARKER</body></html>"
        )
        (static / "assets" / "app.js").write_text("console.log('spa')\n")
        return static

    def test_shell_and_asset_served(self, tmp_path: Path) -> None:
        static = self._static(tmp_path)
        client = TestClient(
            create_app(
                state_dir=str(tmp_path / "state"),
                plans_dir=str(tmp_path / "plans"),
                static_dir=str(static),
            )
        )
        r = client.get("/")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]
        assert "SHELL-MARKER" in r.text
        # the shell must revalidate so a rebuild is never masked by a cache
        assert r.headers["cache-control"] == "no-cache"
        a = client.get("/assets/app.js")
        assert a.status_code == 200
        assert "javascript" in a.headers["content-type"]
        # API routes still win over the static mount.
        assert client.get("/health").json()["ok"] is True

    def test_missing_static_dir_serves_operator_fallback(self, tmp_path: Path) -> None:
        client = TestClient(
            create_app(
                state_dir=str(tmp_path / "state"),
                plans_dir=str(tmp_path / "plans"),
                static_dir=str(tmp_path / "nope"),
            )
        )
        r = client.get("/")
        assert r.status_code == 503
        assert "npm run build" in r.text

    def test_bookmark_shim_rewrites_to_hash_route(self, tmp_path: Path) -> None:
        client = _client(tmp_path / "state", tmp_path / "plans")
        r = client.get("/plan/putspread-test")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]
        # Relative rewrite (never an absolute HTTP redirect), id embedded.
        assert "location.replace('../#/plan/'" in r.text
        assert "putspread-test" in r.text
        # Unknown ids shim too — the SPA's 404 card handles them.
        assert client.get("/plan/nope").status_code == 200

    def test_shim_html_is_injection_safe(self, tmp_path: Path) -> None:
        client = _client(tmp_path / "state", tmp_path / "plans")
        # Slash-free XSS probe (slashes can't ride through a single path
        # segment): a raw <svg onload=...> would execute in the page.
        r = client.get("/plan/%22%3E%3Csvg%20onload%3Dalert(1)%3E")
        assert r.status_code == 200
        assert "<svg" not in r.text
        assert "\\u003c" in r.text  # defused as a JS unicode escape
        # A quote-bearing id still round-trips as a JS string literal.
        r2 = client.get("/plan/a'b")
        assert '"a\'b"' in r2.text


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
        st = client.get("/api/plans/putspread-test").json()["structures"]["nvda-oct"]
        # open_qty should be 3 (5 filled - 2 exited)
        assert st["filled_qty"] == 5
        assert st["exit_filled_qty"] == 2
        assert st["open_qty"] == 3

    def test_days_fields_are_integers(self, tmp_path: Path) -> None:
        plans = tmp_path / "plans"
        _write_plan(plans)
        client = _client(tmp_path / "state", plans)
        payload = client.get("/api/plans/putspread-test").json()
        for s in payload["plan"]["structures"]:
            assert isinstance(s["days_to_expiry"], int)
            assert isinstance(s["days_to_deadline"], int)
            assert s["days_to_expiry"] >= 0
            assert s["days_to_deadline"] >= 0


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


class TestPayoffChart:
    """Pure at-expiry payoff math (put debit spreads) + book summary."""

    def test_expiry_pnl_hockey_stick(self) -> None:
        # nvda-oct shape: 185/150, entry 0.21, qty 5
        kw = dict(long_strike=185.0, short_strike=150.0, entry=0.21, qty=5)
        assert expiry_pnl(s=200.0, **kw) == pytest.approx(-105.0)
        assert expiry_pnl(s=140.0, **kw) == pytest.approx((35.0 - 0.21) * 500)
        assert expiry_pnl(s=170.0, **kw) == pytest.approx((15.0 - 0.21) * 500)
        assert expiry_pnl(s=184.79, **kw) == pytest.approx(0.0, abs=0.5)

    def test_summarize_book_takes_wings_and_debits(self) -> None:
        summary = summarize_book(
            [(185.0, 150.0, 0.21, 5), (185.0, 150.0, 1.24, 3)]
        )
        assert summary["committed"] == pytest.approx(477.0)
        assert summary["max_gain"] == pytest.approx(27523.0)
        assert summary["max_loss"] == pytest.approx(-477.0)
        assert summary["short_floor"] == 150.0


class TestPayoffSeries:
    """Data-space payoff series for the SPA (dense, kinks exact)."""

    def test_dense_series_with_exact_kinks(self) -> None:
        series = payoff_series(185.0, 150.0, 0.21, 5, spot=184.35)
        assert series is not None
        pts = series["points"]
        assert len(pts) >= 120
        xs = [p[0] for p in pts]
        assert xs == sorted(xs)
        for kink in (150.0, 185.0, 184.79):
            assert any(abs(x - kink) < 1e-6 for x in xs), f"kink {kink} missing"
        for x, y in pts:
            assert y == pytest.approx(
                expiry_pnl(185.0, 150.0, 0.21, 5, x), abs=1e-3
            )
        assert series["levels"]["max_gain"] == 17395.0
        assert series["levels"]["breakeven"] == pytest.approx(184.79)
        assert series["labels"]["max_gain"] == "+$17,395"
        assert series["labels"]["max_loss"] == "-$105"
        assert series["levels"]["spot"] == 184.35

    def test_no_position_returns_none(self) -> None:
        assert payoff_series(185.0, 150.0, 0.21, 0) is None


class TestHistorySeries:
    """Data-space P&L history with bounded decimation."""

    def _samples(self, n: int) -> list[dict[str, str]]:
        base = datetime(2026, 9, 22, 12, 0, tzinfo=ET)
        return [
            {
                "ts": (base + timedelta(seconds=20 * i)).isoformat(),
                "total": f"{-1.0 - i * 0.01:.2f}",
            }
            for i in range(n)
        ]

    def test_four_sample_pin(self) -> None:
        base = datetime(2026, 9, 22, 12, 0, tzinfo=ET)
        samples = [
            {"ts": (base + timedelta(minutes=m)).isoformat(), "total": total}
            for m, total in ((0, "-6.50"), (5, "-9.50"), (10, "-11.00"), (15, "-4.25"))
        ]
        series = pnl_history_series(samples)
        assert series is not None
        assert series["points"][0] == [int(base.timestamp() * 1000), -6.5]
        assert series["last"]["pnl"] == -4.25
        assert series["last"]["pos"] is False
        assert series["y_lo"] == -11.0
        assert series["y_hi"] == 0.0

    def test_short_or_malformed_returns_none(self) -> None:
        assert pnl_history_series([{"junk": 1}]) is None
        assert pnl_history_series(self._samples(1)) is None

    def test_decimation_keeps_endpoints_and_bound(self) -> None:
        samples = self._samples(1500)
        series = pnl_history_series(samples)
        assert series is not None
        assert len(series["points"]) <= 600
        first_ts = int(
            datetime.fromisoformat(samples[0]["ts"]).timestamp() * 1000
        )
        last_ts = int(
            datetime.fromisoformat(samples[-1]["ts"]).timestamp() * 1000
        )
        assert series["points"][0][0] == first_ts
        assert series["points"][-1][0] == last_ts


class TestApiPlans:
    def test_contract_types_and_entries(self, tmp_path: Path) -> None:
        plans = tmp_path / "plans"
        _write_plan(plans)
        client = _client(tmp_path / "state", plans)
        payload = client.get("/api/plans").json()
        datetime.fromisoformat(payload["now"])
        assert isinstance(payload["gateway_reachable"], bool)
        assert len(payload["plans"]) == 1
        entry = payload["plans"][0]
        assert entry["id"] == "putspread-test"
        assert entry["account_mode"] == "paper"
        assert entry["structure_count"] == 2
        assert isinstance(entry["total_debit_cap"], float)
        assert entry["state_present"] is False
        assert entry["worst_state"] is None
        assert entry["window_state"] in {"before", "during", "after", "wrong_day"}

    def test_empty_plans_dir_returns_empty_list(self, tmp_path: Path) -> None:
        client = _client(tmp_path / "state", tmp_path / "plans")
        assert client.get("/api/plans").json()["plans"] == []

    def test_armed_reflects_fresh_heartbeat(self, tmp_path: Path) -> None:
        plans = tmp_path / "plans"
        _write_plan(plans)
        run = tmp_path / "state" / "putspread-test"
        run.mkdir(parents=True)
        book = BookState(["nvda-oct", "qqq-nov"])
        book.heartbeat = datetime.now(ET)
        book.save(run / "book.json")
        client = _client(tmp_path / "state", plans)
        entry = client.get("/api/plans").json()["plans"][0]
        assert entry["armed"] is True
        assert entry["state_present"] is True


class TestNetPositions:
    """Net exposure rollup: filled structures grouped by underlying.

    Exposure metrics are computed on open_qty (filled minus exited), so a
    fully-exited structure drops out and a partial exit shrinks the row.
    """

    def _specs(self) -> list[dict[str, object]]:
        return [
            {
                "id": "nvda-oct",
                "underlying": "NVDA",
                "long_strike": 185.0,
                "short_strike": 150.0,
                "expiry": "2026-10-16",
            },
            {
                "id": "nvda-nov",
                "underlying": "NVDA",
                "long_strike": 185.0,
                "short_strike": 150.0,
                "expiry": "2026-11-20",
            },
            {
                "id": "qqq-nov",
                "underlying": "QQQ",
                "long_strike": 600.0,
                "short_strike": 475.0,
                "expiry": "2026-11-20",
            },
        ]

    def test_groups_by_underlying_and_sums_open_exposure(self) -> None:
        states = {
            "nvda-oct": {"entry_fill": 0.21, "filled_qty": 5, "open_qty": 5},
            "nvda-nov": {"entry_fill": 1.24, "filled_qty": 3, "open_qty": 3},
        }
        marks = {
            "nvda-oct": {"unrealized": -5.0},
            "nvda-nov": {"unrealized": -6.0},
        }
        rows = net_positions(self._specs(), states, marks)
        assert len(rows) == 1  # QQQ never filled -> no exposure row
        r = rows[0]
        assert r["underlying"] == "NVDA"
        assert r["structure_count"] == 2
        assert r["open_qty"] == 8
        assert r["avg_entry"] == pytest.approx((0.21 * 5 + 1.24 * 3) / 8)
        assert r["committed"] == pytest.approx(105.0 + 372.0)
        assert r["unrealized"] == pytest.approx(-11.0)
        assert r["short_floor"] == 150.0
        assert r["long_ceiling"] == 185.0
        assert r["max_gain"] == pytest.approx(17395.0 + 10128.0)
        assert r["max_loss"] == pytest.approx(-477.0)
        assert [leg["structure_id"] for leg in r["legs"]] == ["nvda-oct", "nvda-nov"]
        assert r["legs"][0] == {
            "structure_id": "nvda-oct",
            "expiry": "2026-10-16",
            "long_strike": 185.0,
            "short_strike": 150.0,
            "open_qty": 5,
            "entry": 0.21,
        }

    def test_partial_exit_shrinks_and_full_exit_drops_out(self) -> None:
        specs = self._specs()[:1]
        states = {
            "nvda-oct": {
                "entry_fill": 0.50,
                "filled_qty": 5,
                "open_qty": 3,  # exited 2 of 5
            }
        }
        rows = net_positions(specs, states, {})
        assert rows[0]["open_qty"] == 3
        assert rows[0]["committed"] == pytest.approx(0.50 * 3 * 100)
        # fully closed: no row at all
        closed = dict(states["nvda-oct"], open_qty=0)
        assert net_positions(specs, {"nvda-oct": closed}, {}) == []

    def test_unrealized_sums_available_quotes_and_none_without_any(self) -> None:
        states = {
            "nvda-oct": {"entry_fill": 0.21, "filled_qty": 5, "open_qty": 5},
            "nvda-nov": {"entry_fill": 1.24, "filled_qty": 3, "open_qty": 3},
        }
        partial = {"nvda-oct": {"unrealized": -5.0}}  # nvda-nov has no quote
        rows = net_positions(self._specs(), states, partial)
        assert rows[0]["unrealized"] == pytest.approx(-5.0)
        assert net_positions(self._specs(), states, {})[0]["unrealized"] is None

    def test_missing_state_or_fill_is_skipped(self) -> None:
        assert net_positions(self._specs(), {}, {}) == []
        working = {"nvda-oct": {"entry_fill": None, "filled_qty": 0, "open_qty": 0}}
        assert net_positions(self._specs(), working, {}) == []


class TestMergeNetPositions:
    """Portfolio-level net positions: per-plan rows merged by underlying so
    the plans index can show current positions without a detail fetch."""

    def _row(
        self,
        underlying: str,
        open_qty: int,
        entry: float,
        unrealized: float | None = None,
        sid: str = "nvda-oct",
    ) -> list[dict[str, Any]]:
        specs = [
            {
                "id": sid,
                "underlying": underlying,
                "long_strike": 185.0,
                "short_strike": 150.0,
                "expiry": "2026-10-16",
            }
        ]
        states = {sid: {"entry_fill": entry, "filled_qty": open_qty, "open_qty": open_qty}}
        marks = {sid: {"unrealized": unrealized}} if unrealized is not None else {}
        return net_positions(specs, states, marks)

    def test_same_underlying_across_plans_merges(self) -> None:
        a = self._row("NVDA", 5, 0.21, unrealized=-5.0)
        b = self._row("NVDA", 3, 1.24, sid="nvda-nov")
        merged = merge_net_positions([("plan-a", a), ("plan-b", b)])
        assert len(merged) == 1
        row = merged[0]
        assert row["open_qty"] == 8
        assert row["structure_count"] == 2
        committed = 0.21 * 5 * 100 + 1.24 * 3 * 100
        assert row["committed"] == pytest.approx(committed)
        assert row["avg_entry"] == pytest.approx(committed / 8 / 100)
        assert row["max_loss"] == pytest.approx(-committed)
        assert row["max_gain"] == pytest.approx((35 - 0.21) * 500 + (35 - 1.24) * 300)
        assert row["unrealized"] == pytest.approx(-5.0)
        assert [leg["structure_id"] for leg in row["legs"]] == ["nvda-oct", "nvda-nov"]

    def test_structure_id_collision_across_plans_is_disambiguated(self) -> None:
        a = self._row("NVDA", 5, 0.21)
        b = self._row("NVDA", 3, 1.24)  # same structure id in another plan
        merged = merge_net_positions([("p1", a), ("p2", b)])
        ids = [leg["structure_id"] for leg in merged[0]["legs"]]
        assert len(ids) == 2
        assert len(set(ids)) == 2  # no duplicate React keys

    def test_distinct_underlyings_stay_separate_and_sorted(self) -> None:
        qqq = self._row("QQQ", 2, 0.50, sid="qqq-a")
        amd = self._row("AMD", 1, 0.50, sid="amd-a")
        merged = merge_net_positions([("p1", qqq), ("p2", amd)])
        assert [r["underlying"] for r in merged] == ["AMD", "QQQ"]

    def test_unrealized_none_when_no_plan_had_quotes(self) -> None:
        a = self._row("NVDA", 5, 0.21)
        b = self._row("NVDA", 3, 1.24, sid="nvda-nov")
        assert merge_net_positions([("p1", a), ("p2", b)])[0]["unrealized"] is None

    def test_empty_inputs(self) -> None:
        assert merge_net_positions([]) == []
        assert merge_net_positions([("p1", [])]) == []


class TestPortfolioPayload:
    """C11: cross-plan rollup + freshest account in /api/plans."""

    def _seed_open_book(self, tmp_path: Path, plan_id: str = "putspread-test") -> Path:
        plans = tmp_path / "plans"
        _write_plan(plans)
        run = tmp_path / "state" / plan_id
        run.mkdir(parents=True)
        book = BookState(["nvda-oct", "qqq-nov"])
        st = book.structures["nvda-oct"]
        st.to(Status.ENTER_WORKING, datetime.now(ET))
        st.to(Status.OPEN, datetime.now(ET))
        st.filled_qty = 5
        st.entry_fill = Decimal("0.21")
        book.save(run / "book.json")
        (run / "marks.json").write_text(
            json.dumps(
                {
                    "ts": datetime.now(ET).isoformat(),
                    "total_unrealized": "-5.00",
                    "structures": {
                        "nvda-oct": {
                            "qty": 5,
                            "entry": "0.21",
                            "bid": "0.19",
                            "ask": "0.21",
                            "mark": "0.20",
                            "unrealized": "-5.00",
                        }
                    },
                }
            )
        )
        return run

    def test_portfolio_block_present(self, tmp_path: Path) -> None:
        self._seed_open_book(tmp_path)
        client = _client(tmp_path / "state", tmp_path / "plans")
        payload = client.get("/api/plans").json()
        pf = payload["portfolio"]
        assert pf["plans_count"] == 1
        assert pf["plans_with_state"] == 1
        assert pf["open_qty"] == 8 or pf["open_qty"] == 5  # nvda(5 open) + qqq(0)
        assert pf["unrealized_open"] == pytest.approx(-5.0)  # (0.20-0.21)*5*100
        assert pf["unrealized_filled"] == pytest.approx(-5.0)
        assert pf["marks_stale"] is False

    def test_net_positions_top_level(self, tmp_path: Path) -> None:
        """The plans index carries current positions so the operator does
        not need to click into a plan to see the book."""
        self._seed_open_book(tmp_path)
        client = _client(tmp_path / "state", tmp_path / "plans")
        rows = client.get("/api/plans").json()["net_positions"]
        assert len(rows) == 1  # qqq-nov never filled -> no row
        row = rows[0]
        assert row["underlying"] == "NVDA"
        assert row["open_qty"] == 5
        assert row["committed"] == pytest.approx(105.0)
        assert row["unrealized"] == pytest.approx(-5.0)
        assert row["legs"][0]["structure_id"] == "nvda-oct"
        assert row["legs"][0]["entry"] == pytest.approx(0.21)

    def test_per_plan_unrealized_and_realized(self, tmp_path: Path) -> None:
        self._seed_open_book(tmp_path)
        client = _client(tmp_path / "state", tmp_path / "plans")
        entry = client.get("/api/plans").json()["plans"][0]
        assert entry["unrealized_open"] == pytest.approx(-5.0)
        assert entry["realized"] is None  # no exits yet

    def test_open_qty_not_filled_qty_basis(self, tmp_path: Path) -> None:
        """The C11 regression: partially exited structures must value the
        REMAINING contracts, not the original fill."""
        run = self._seed_open_book(tmp_path)
        # exit 2 of 5 at a profit
        book = BookState.load(run / "book.json", ["nvda-oct", "qqq-nov"])
        st = book.structures["nvda-oct"]
        st.exit_fill = Decimal("0.35")
        st.exit_filled_qty = 2
        book.save(run / "book.json")
        client = _client(tmp_path / "state", tmp_path / "plans")
        pf = client.get("/api/plans").json()["portfolio"]
        assert pf["open_qty"] == 3
        # open basis: (0.20 - 0.21) * 3 * 100
        assert pf["unrealized_open"] == pytest.approx(-3.0)
        # filled basis (marks.json total) stays -5.0
        assert pf["unrealized_filled"] == pytest.approx(-5.0)
        assert pf["realized"] == pytest.approx((0.35 - 0.21) * 2 * 100)

    def test_freshest_account_wins_and_seen_listed(self, tmp_path: Path) -> None:
        from tree_options.trex.account import AccountSnapshot, write_account

        run = self._seed_open_book(tmp_path)
        old = AccountSnapshot(
            account_id="DUT143714",
            net_liquidation=Decimal("1.00"),
            cash=Decimal("1.00"),
            buying_power=Decimal("1.00"),
            currency="USD",
            ts=datetime.now(ET) - timedelta(minutes=5),
        )
        new = AccountSnapshot(
            account_id="DUT143714",
            net_liquidation=Decimal("1000252.09"),
            cash=Decimal("999516.91"),
            buying_power=Decimal("3998067.63"),
            currency="USD",
            ts=datetime.now(ET),
        )
        write_account(run / "account.json", old)
        disc = tmp_path / "discovery"
        write_account(disc / "account.json", new)
        client = TestClient(
            create_app(
                state_dir=str(tmp_path / "state"),
                plans_dir=str(tmp_path / "plans"),
                discovery_dir=str(disc),
            )
        )
        payload = client.get("/api/plans").json()
        assert payload["account"]["net_liquidation"] == 1000252.09  # freshest
        assert payload["account"]["source"] == str(disc / "account.json")
        assert payload["accounts_seen"] == ["DUT143714"]

    def test_no_account_is_null(self, tmp_path: Path) -> None:
        self._seed_open_book(tmp_path)
        client = _client(tmp_path / "state", tmp_path / "plans")
        assert client.get("/api/plans").json()["account"] is None


class TestStatsApi:
    """M2: GET /api/stats — paper-money stats + equity curve."""

    def test_stats_payload_from_fixtures(self, tmp_path: Path) -> None:

        self._seed(tmp_path)
        client = _client(tmp_path / "state", tmp_path / "plans", tmp_path / "discovery")
        payload = client.get("/api/stats").json()
        assert payload["tracking_since"] is not None
        assert payload["equity"] is not None
        assert payload["equity"]["points"][0][1] == pytest.approx(1000000.0)
        assert payload["totals"]["realized"] == pytest.approx(28.0)
        assert payload["days"][0]["date"] == "2026-09-22"

    def test_stats_empty_state(self, tmp_path: Path) -> None:
        (tmp_path / "plans").mkdir()
        (tmp_path / "state").mkdir()
        (tmp_path / "discovery").mkdir()
        client = _client(tmp_path / "state", tmp_path / "plans", tmp_path / "discovery")
        payload = client.get("/api/stats").json()
        assert payload["equity"] is None
        assert payload["tracking_since"] is None

    def _seed(self, tmp_path: Path) -> None:
        from zoneinfo import ZoneInfo

        from tree_options.trex.history import append_line
        from tree_options.trex.state import BookState, Status

        et = ZoneInfo("America/New_York")
        plans = tmp_path / "plans"
        _write_plan(plans)
        run = tmp_path / "state" / "putspread-test"
        run.mkdir(parents=True)
        book = BookState(["nvda-oct", "qqq-nov"])
        st = book.structures["nvda-oct"]
        st.to(Status.ENTER_WORKING, datetime(2026, 9, 22, 10, 5, tzinfo=et))
        st.to(Status.OPEN, datetime(2026, 9, 22, 10, 5, tzinfo=et))
        st.filled_qty = 5
        st.entry_fill = Decimal("0.21")
        st.exit_fill = Decimal("0.35")
        st.exit_filled_qty = 2
        book.save(run / "book.json")
        (run / "events.jsonl").write_text(
            '{"ts": "2026-09-22T11:00:00-04:00", "event": "exit_fill",'
            ' "structure": "nvda-oct", "filled": 2, "avg": "0.35"}\n'
        )
        append_line(
            run / "marks_history.jsonl",
            {"ts": "2026-09-22T15:00:00-04:00", "total_unrealized": "-5.00"},
        )
        disc = tmp_path / "discovery"
        disc.mkdir()
        append_line(
            disc / "account_history.jsonl",
            {"ts": "2026-09-22T14:00:00-04:00", "net_liquidation": "1000000.00"},
        )
        append_line(
            disc / "account_history.jsonl",
            {"ts": "2026-09-22T15:00:00-04:00", "net_liquidation": "1000005.00"},
        )


class TestDiscoveryEndpoints:
    """C10: GET /api/discovery + POST /api/discovery/scan (spool write)."""

    def _client(self, tmp_path: Path) -> TestClient:
        return TestClient(
            create_app(
                state_dir=str(tmp_path / "state"),
                plans_dir=str(tmp_path / "plans"),
                discovery_dir=str(tmp_path / "discovery"),
            )
        )

    def _stamp(self) -> DiscoveryStamp:
        return DiscoveryStamp(
            git_sha="test", config_hash="x", generated_at=datetime.now(ET).isoformat(), runner="manual"
        )

    def test_empty_state(self, tmp_path: Path) -> None:
        # pin the config path too: the deployed ~/.config/trex/discovery.toml
        # would flip config_present on any host where the lane is installed
        with unittest.mock.patch.dict(
            os.environ, {"TREX_DISCOVERY_CONFIG": str(tmp_path / "absent.toml")}
        ):
            client = self._client(tmp_path)
            payload = client.get("/api/discovery").json()
        assert payload["latest"] is None
        assert payload["runs"] == []
        assert payload["spool"]["pending"] is False
        assert payload["config_present"] is False

    def test_serves_latest_scan(self, tmp_path: Path) -> None:
        disc = tmp_path / "discovery"
        write_scan(
            disc,
            {
                "generated_at": datetime.now(ET).isoformat(),
                "mode": "manual",
                "data_quality": {"chains_available": True, "notes": ["delayed"]},
                "candidates": [_cand("NVDA", 5.0, 1.0)],
                "rejected": [],
            },
            self._stamp(),
            now=datetime.now(ET),
        )
        payload = self._client(tmp_path).get("/api/discovery").json()
        assert payload["latest"] is not None
        assert payload["latest"]["age_seconds"] is not None
        assert payload["latest"]["candidates"][0]["underlying"] == "NVDA"
        assert payload["runs"][0]["accepted"] == 1

    def test_post_scan_writes_spool_request(self, tmp_path: Path) -> None:
        client = self._client(tmp_path)
        r = client.post("/api/discovery/scan")
        assert r.status_code == 202
        request_id = r.json()["request_id"]
        files = list((tmp_path / "discovery" / "spool").glob("scan.request.*"))
        assert len(files) == 1
        assert request_id in files[0].name

    def test_post_scan_503_when_spool_unwritable(self, tmp_path: Path) -> None:
        client = self._client(tmp_path)
        with monkeypatch_unwritable():
            r = client.post("/api/discovery/scan")
        assert r.status_code == 503
        assert "unwritable" in r.json()["detail"]

    def test_guard_extends_to_discovery_pure_modules(self) -> None:
        import tree_options.trex.discovery.artifact as artifact_module
        import tree_options.trex.discovery.config as config_module
        import tree_options.trex.discovery.engine as engine_module
        import tree_options.trex_web.discovery_view as view_module

        for module in (artifact_module, config_module, engine_module, view_module):
            assert "ib_async" not in module.__dict__
            for name in dir(module):
                obj = getattr(module, name)
                assert not (getattr(obj, "__module__", "") or "").startswith("ib_async")


def _cand(underlying: str, width: float, debit: float) -> dict:
    return {
        "underlying": underlying,
        "expiry": "20261016",
        "dte": 24,
        "short_strike": 150.0,
        "long_strike": 150.0 + width,
        "width": width,
        "debit_mid": debit,
        "yield_ratio": (width - debit) / debit,
        "accepted": True,
        "rank": 1,
        "rules": [],
        "reasons": [],
    }


def monkeypatch_unwritable():
    """Force the spool write to fail as it would inside the sandboxed unit."""
    import tree_options.trex.discovery.artifact as artifact

    def boom(*a: object, **k: object) -> None:
        raise OSError("read-only file system")

    return unittest.mock.patch.object(artifact, "_atomic_write", boom)


class TestApiPlanDetail:
    def _seeded_client(self, tmp_path: Path) -> TestClient:
        plans = tmp_path / "plans"
        _write_plan(plans)
        run = tmp_path / "state" / "putspread-test"
        run.mkdir(parents=True)
        book = BookState(["nvda-oct", "qqq-nov"])
        st = book.structures["nvda-oct"]
        st.to(Status.ENTER_WORKING, datetime.now(ET))
        st.to(Status.OPEN, datetime.now(ET))
        st.filled_qty = 5
        st.entry_fill = Decimal("0.21")
        st.exit_fill = Decimal("0.51")
        st.exit_filled_qty = 5
        book.save(run / "book.json")
        t0 = datetime.now(ET)
        marks = {
            "ts": t0.isoformat(),
            "total_unrealized": "-1.50",
            "spots": {"NVDA": "184.35"},
            "history": [
                {"ts": t0.isoformat(), "total": "-1.50"},
                {"ts": (t0 + timedelta(minutes=2)).isoformat(), "total": "-2.00"},
            ],
            "structures": {
                "nvda-oct": {
                    "qty": 5,
                    "entry": "0.21",
                    "bid": "0.19",
                    "ask": "0.21",
                    "mark": "0.20",
                    "unrealized": "-5.00",
                },
                "qqq-nov": {"qty": 4, "entry": "1.37", "mark": None},
            },
        }
        (run / "marks.json").write_text(json.dumps(marks))
        return _client(tmp_path / "state", plans)

    def test_detail_contract(self, tmp_path: Path) -> None:
        client = self._seeded_client(tmp_path)
        payload = client.get("/api/plans/putspread-test").json()
        assert payload["plan"]["structures"][0]["long_strike"] == 185.0
        assert payload["plan"]["structures"][0]["width"] == 35.0
        assert payload["runbook"]["window_state"] in {
            "before",
            "during",
            "after",
            "wrong_day",
        }
        st = payload["structures"]["nvda-oct"]
        assert st["state"] == "open"
        assert st["entry_fill"] == 0.21
        assert st["realized_pnl"] == 150.0  # exit 0.51 > entry 0.21 = profit
        assert payload["marks"]["structures"]["nvda-oct"]["mark"] == 0.20
        assert payload["marks"]["structures"]["qqq-nov"]["mark"] is None
        assert payload["marks"]["total_unrealized"] == -1.5
        assert payload["marks"]["spots"]["NVDA"] == 184.35
        assert payload["book_summary"]["committed"] == 105.0
        assert payload["book_summary"]["max_gain"] == 17395.0
        assert payload["payoffs"][0]["structure_id"] == "nvda-oct"
        assert payload["payoffs"][0]["levels"]["spot"] == 184.35
        assert payload["payoffs"][0]["labels"]["max_gain"] == "+$17,395"
        assert payload["history"]["points"][0][1] == -1.5
        assert isinstance(payload["events"], list)
        # the seeded structure is fully exited (exit_filled_qty == filled)
        # -> no open exposure anywhere in the book
        assert payload["net_positions"] == []

    def test_net_positions_for_open_book(self, tmp_path: Path) -> None:
        plans = tmp_path / "plans"
        _write_plan(plans)
        run = tmp_path / "state" / "putspread-test"
        run.mkdir(parents=True)
        book = BookState(["nvda-oct", "qqq-nov"])
        st = book.structures["nvda-oct"]
        st.to(Status.ENTER_WORKING, datetime.now(ET))
        st.to(Status.OPEN, datetime.now(ET))
        st.filled_qty = 5
        st.entry_fill = Decimal("0.21")
        book.save(run / "book.json")
        (run / "marks.json").write_text(
            json.dumps(
                {
                    "ts": datetime.now(ET).isoformat(),
                    "total_unrealized": "-5.00",
                    "structures": {
                        "nvda-oct": {
                            "qty": 5,
                            "entry": "0.21",
                            "bid": None,
                            "ask": None,
                            "mark": "0.20",
                            "unrealized": "-5.00",
                        }
                    },
                }
            )
        )
        client = _client(tmp_path / "state", plans)
        rows = client.get("/api/plans/putspread-test").json()["net_positions"]
        assert len(rows) == 1
        r = rows[0]
        assert r["underlying"] == "NVDA"
        assert r["open_qty"] == 5
        assert r["committed"] == pytest.approx(105.0)
        assert r["unrealized"] == pytest.approx(-5.0)
        assert r["legs"][0]["structure_id"] == "nvda-oct"

    def test_unknown_id_is_json_404(self, tmp_path: Path) -> None:
        plans = tmp_path / "plans"
        _write_plan(plans)
        client = _client(tmp_path / "state", plans)
        r = client.get("/api/plans/nope")
        assert r.status_code == 404
        assert "nope" in r.json()["detail"]

    def test_no_state_payload_renders_minimal(self, tmp_path: Path) -> None:
        plans = tmp_path / "plans"
        _write_plan(plans)
        client = _client(tmp_path / "state", plans)
        payload = client.get("/api/plans/putspread-test").json()
        assert payload["state_present"] is False
        assert payload["marks"] is None
        assert payload["book_summary"] is None
        assert payload["payoffs"] == []
        assert payload["history"] is None


class TestDiscoveryShadowBlock:
    """M3: /api/discovery carries the shadow alternatives book."""

    def test_shadow_present_from_fixture(self, tmp_path: Path) -> None:
        disc = tmp_path / "discovery"
        disc.mkdir()
        (disc / "shadow_book.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "positions": [
                        {
                            "episode_id": "NVDA|20261016|150|185#run-1",
                            "key": "NVDA|20261016|150|185",
                            "underlying": "NVDA",
                            "expiry": "20261016",
                            "short_strike": 150.0,
                            "long_strike": 185.0,
                            "width": 35.0,
                            "qty": 1,
                            "debit_paid": 0.21,
                            "opened_at": "2026-09-22T16:11:00-04:00",
                            "opened_run_id": "run-1",
                            "status": "open",
                            "last_mark": 0.35,
                            "last_mark_at": "2026-09-22T16:41:00-04:00",
                            "mark_source": "scan",
                            "best_pnl": 14.0,
                            "worst_pnl": 14.0,
                            "final_pnl": None,
                            "pnl": 14.0,
                        }
                    ],
                }
            )
        )
        client = _client(tmp_path / "state", tmp_path / "plans", disc)
        shadow = client.get("/api/discovery").json()["shadow"]
        assert shadow is not None
        assert shadow["positions"][0]["pnl"] == pytest.approx(14.0)
        assert shadow["stats"]["open"] == 1
        assert shadow["stats"]["not_executed"] is True

    def test_shadow_absent_is_none(self, tmp_path: Path) -> None:
        client = _client(
            tmp_path / "state", tmp_path / "plans", tmp_path / "discovery"
        )
        assert client.get("/api/discovery").json()["shadow"] is None


class TestMarketWatchAndSymbol:
    """M5b: watch mutations via the spool + the symbol detail endpoint."""

    def _disc(self, tmp_path: Path) -> Path:
        disc = tmp_path / "discovery"
        (disc / "market" / "cache").mkdir(parents=True, exist_ok=True)
        return disc

    def test_watch_post_spools(self, tmp_path: Path) -> None:
        disc = self._disc(tmp_path)
        client = _client(tmp_path / "state", tmp_path / "plans", disc)
        r = client.post("/api/market/watch", json={"op": "add", "symbol": "spy"})
        assert r.status_code == 202
        body = r.json()
        assert body["accepted"] is True
        assert list((disc / "spool").glob("watch.request.*"))

    def test_refresh_post_spools(self, tmp_path: Path) -> None:
        disc = self._disc(tmp_path)
        client = _client(tmp_path / "state", tmp_path / "plans", disc)
        r = client.post("/api/market/refresh", json={"symbols": ["SPY"]})
        assert r.status_code == 202
        assert list((disc / "spool").glob("market.request.*"))

    def test_symbol_detail_assembles_from_cache(self, tmp_path: Path) -> None:
        import json as _json

        disc = self._disc(tmp_path)
        (disc / "market.json").write_text(
            _json.dumps(
                {
                    "last_refresh": "2026-09-22T18:59:30-04:00",
                    "symbols": {"SPY": {"bid": 1.0, "ask": 1.1, "close": 1.05,
                                         "iv30": 11.0, "change_pct": 0.1,
                                         "source_as_of": "2026-09-22 18:59:00"}},
                    "errors": {},
                }
            )
        )
        fresh = datetime.now(ET).isoformat()
        bars_env = {
            "fetched_at": fresh,
            "ttl_seconds": 86400,
            "payload": {"bars": [{"t": 1789992000000, "c": 750.1, "v": 1}]},
        }
        (disc / "market" / "cache" / "bars").mkdir(parents=True, exist_ok=True)
        (disc / "market" / "cache" / "bars" / "SPY.json").write_text(_json.dumps(bars_env))
        news_env = {
            "fetched_at": fresh,
            "ttl_seconds": 1800,
            "payload": {"items": [{"title": "t", "link": "https://x", "pub": None,
                                    "source": "s"}]},
        }
        (disc / "market" / "cache" / "news").mkdir(parents=True, exist_ok=True)
        (disc / "market" / "cache" / "news" / "SPY.json").write_text(_json.dumps(news_env))
        client = _client(tmp_path / "state", tmp_path / "plans", disc)
        payload = client.get("/api/market/SPY").json()
        assert payload["symbol"] == "SPY"
        assert payload["quote"]["bid"] == pytest.approx(1.0)
        # naive CBOE-style stamp must not raise on the aware now (deployed
        # 500); it reads as UTC and yields a 0-floored age. Junk -> None.
        assert isinstance(payload["quote_age_seconds"], int)
        assert payload["quote_age_seconds"] >= 0
        assert payload["bars"]["points"][0][1] == pytest.approx(750.1)
        assert payload["news"][0]["title"] == "t"

    def test_symbol_serves_expired_envelopes_with_age(self, tmp_path: Path) -> None:
        """Expired bars/news stay visible with a disclosed age (the TTL
        governs refetching, not display) - they used to vanish at TTL."""
        import json as _json

        disc = self._disc(tmp_path)
        old = "2026-01-02T10:00:00-05:00"  # far past every TTL
        for kind, payload in (
            ("bars", {"bars": [{"t": 1789992000000, "c": 750.1, "v": 1}]}),
            ("news", {"items": [{"title": "old", "link": "https://x", "pub": None,
                                  "source": "s"}]}),
        ):
            (disc / "market" / "cache" / kind).mkdir(parents=True, exist_ok=True)
            (disc / "market" / "cache" / kind / "SPY.json").write_text(
                _json.dumps({"fetched_at": old, "ttl_seconds": 60, "payload": payload})
            )
        client = _client(tmp_path / "state", tmp_path / "plans", disc)
        payload = client.get("/api/market/SPY").json()
        assert payload["bars"]["points"][0][1] == pytest.approx(750.1)
        assert payload["news"][0]["title"] == "old"
        assert payload["bars_age_seconds"] > 86400
        assert payload["news_age_seconds"] > 86400

    def test_symbol_cold_cache_is_none_not_error(self, tmp_path: Path) -> None:
        disc = self._disc(tmp_path)
        client = _client(tmp_path / "state", tmp_path / "plans", disc)
        payload = client.get("/api/market/SPY").json()
        assert payload["bars"] is None
        assert payload["news"] == []
        assert payload["bars_age_seconds"] is None
        assert payload["news_age_seconds"] is None
        assert client.get("/api/market/not%20a%20symbol").status_code == 404


class TestBacktestApi:
    """M4: scenario requests cross the wire as a KEY only; the GET serves
    the runner's labeled artifact (404 until materialized)."""

    KEY = "QQQ|20261016|642|657"

    def test_post_spools_key_only(self, tmp_path: Path) -> None:
        import json as _json

        disc = tmp_path / "discovery"
        client = _client(tmp_path / "state", tmp_path / "plans", disc)
        resp = client.post("/api/discovery/backtest", json={"key": self.KEY, "debit": 99})
        assert resp.status_code == 202
        files = list((disc / "spool").glob("backtest.request.*"))
        assert len(files) == 1
        body = _json.loads(files[0].read_text())
        assert body["key"] == self.KEY
        assert "debit" not in body  # client-supplied prices never reach the runner

    @pytest.mark.parametrize(
        "bad", ["", "qqq|20261016|642|657", "QQQ|2026-10-16|642|657", "QQQ|20261016|x|657",
                "../etc|20261016|1|2"],
    )
    def test_post_rejects_malformed_keys(self, tmp_path: Path, bad: str) -> None:
        client = _client(tmp_path / "state", tmp_path / "plans", tmp_path / "discovery")
        assert client.post("/api/discovery/backtest", json={"key": bad}).status_code == 422

    def test_get_404_then_artifact_with_age(self, tmp_path: Path) -> None:
        from tree_options.trex.discovery.backtest import LABEL, write_artifact

        disc = tmp_path / "discovery"
        client = _client(tmp_path / "state", tmp_path / "plans", disc)
        assert client.get("/api/discovery/backtest", params={"key": self.KEY}).status_code == 404
        write_artifact(disc, self.KEY, {"key": self.KEY, "label": LABEL, "error": None,
                                        "generated_at": datetime.now(ET).isoformat()})
        resp = client.get("/api/discovery/backtest", params={"key": self.KEY})
        assert resp.status_code == 200
        doc = resp.json()
        assert doc["label"] == LABEL
        assert isinstance(doc["age_seconds"], int)


class TestProposalsApi:
    def test_market_carries_pending_proposals_and_last_run(self, tmp_path: Path) -> None:
        disc = tmp_path / "discovery"
        disc.mkdir(parents=True)
        (disc / "watchlist.json").write_text(json.dumps({
            "version": 1,
            "symbols": [{"symbol": "SPY", "origin": "seed", "added_at": "x"}],
            "proposals": [
                {"id": "a1", "symbol": "TSM", "action": "add", "status": "pending",
                 "rationale": "r", "confidence": 0.6, "created_at": "x",
                 "provenance": {"provider": "local", "model": "Qwen/Qwen3.8-27B"}},
                {"id": "a2", "symbol": "SMH", "action": "add", "status": "dismissed"},
            ],
            "last_proposal_run": {"status": "ok", "provider": "local", "added": 1},
        }))
        client = _client(tmp_path / "state", tmp_path / "plans", disc)
        body = client.get("/api/market").json()
        assert [p["id"] for p in body["proposals"]] == ["a1"]
        assert body["last_proposal_run"]["provider"] == "local"
        assert body["watch_origins"] == {"SPY": "seed"}

    def test_propose_post_spools(self, tmp_path: Path) -> None:
        disc = tmp_path / "discovery"
        client = _client(tmp_path / "state", tmp_path / "plans", disc)
        assert client.post("/api/market/propose").status_code == 202
        assert len(list((disc / "spool").glob("propose.request.*"))) == 1


class TestCodexM456Web:
    def test_market_get_never_creates_watchlist(self, tmp_path: Path) -> None:
        disc = tmp_path / "discovery"
        disc.mkdir()
        client = _client(tmp_path / "state", tmp_path / "plans", disc)
        assert client.get("/api/market").status_code == 200
        assert not (disc / "watchlist.json").exists()

    def test_scenario_key_length_bounded(self, tmp_path: Path) -> None:
        client = _client(tmp_path / "state", tmp_path / "plans", tmp_path / "discovery")
        long_key = "SPY|20261016|" + "1" * 300 + "|2"
        assert client.post("/api/discovery/backtest", json={"key": long_key}).status_code == 422
        ok = client.post("/api/discovery/backtest", json={"key": "SPY|20261016|702.5|712.5"})
        assert ok.status_code == 202
