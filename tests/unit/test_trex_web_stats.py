"""M2: paper-money stats derivation (pure) + the /api/stats payload.

Two separately-labeled bases per the honesty rules: the equity curve is
net-liquidation (account truth, may include non-trading activity); the
P&L series is book-derived trading P&L. Realized-per-day diffs cumulative
exit_fill counters at ISO-date boundaries; missing marks are gaps, never
zeros.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from tree_options.trex_web.payoff import pnl_history_series
from tree_options.trex_web.reader import (
    read_account_history,
    read_marks_history,
)
from tree_options.trex_web.stats import (
    equity_series,
    realized_by_day,
    stats_payload,
    unrealized_eod_by_day,
)

ET = ZoneInfo("America/New_York")


def _iso(h: int, m: int, day: int = 22) -> str:
    return datetime(2026, 9, day, h, m, tzinfo=ET).isoformat()


class TestEquitySeries:
    def test_parity_with_pnl_history_series(self) -> None:
        samples = [{"ts": _iso(10, 0 + i), "total": f"-{i}.00"} for i in range(50)]
        records = [{"ts": _iso(10, 0 + i), "net_liquidation": f"100000{i}.00"} for i in range(50)]
        pnl = pnl_history_series(samples)
        eq = equity_series(records)
        assert pnl is not None and eq is not None
        assert [p[0] for p in eq["points"]] == [p[0] for p in pnl["points"]]
        # M8 flash review F2: equity is a LEVEL series; its axis fits the
        # data (level_extent) instead of flooring at 0, which drew a $12
        # move on $1M as a flat line
        lowest = min(v for _, v in eq["points"])
        assert 0 < eq["y_lo"] < lowest
        assert eq["last"]["value"] == pytest.approx(10000049.0)
        assert eq["last"]["pos"] is True

    def test_decimates_and_needs_two_points(self) -> None:
        many = [
            {"ts": _iso(9, 30, day=18), "net_liquidation": "1000000.00"},
            {"ts": _iso(9, 31, day=18), "net_liquidation": "1000000.50"},
            {"ts": _iso(9, 32, day=18), "net_liquidation": "1000001.00"},
        ]
        series = equity_series(many, max_points=2)
        assert series is not None
        assert len(series["points"]) == 2
        assert series["points"][0][0] != series["points"][-1][0]
        assert equity_series([{"ts": _iso(9, 30), "net_liquidation": "1.00"}]) is None

    def test_empty(self) -> None:
        assert equity_series([]) is None


class TestRealizedByDay:
    def test_diffs_cumulative_proceeds_across_days(self) -> None:
        events = [
            {"ts": _iso(10, 0), "event": "entry_fill", "structure": "nvda-oct",
             "filled": 5, "avg": "0.21"},
            # day 1: exit 2 at 0.35 -> proceeds 0.70 - 2*0.21 = +28
            {"ts": _iso(11, 0), "event": "exit_fill", "structure": "nvda-oct",
             "filled": 2, "avg": "0.35"},
            # day 2: the remaining 3 exit at 0.50 each. The event carries
            # the BLENDED cumulative avg (2*0.35+3*0.50)/5 = 0.44:
            # proceeds delta (5*0.44 - 2*0.35) - 3*0.21 = +87
            {"ts": _iso(10, 30, day=23), "event": "exit_fill", "structure": "nvda-oct",
             "filled": 5, "avg": "0.44"},
        ]
        by_day = realized_by_day(events, {"nvda-oct": 0.21})
        assert by_day["2026-09-22"] == pytest.approx(28.0)
        assert by_day["2026-09-23"] == pytest.approx(87.0)

    def test_ignores_non_exit_and_junk_events(self) -> None:
        events = [
            {"ts": _iso(10, 0), "event": "entry_order", "structure": "nvda-oct"},
            {"ts": _iso(10, 1), "event": "exit_fill"},  # missing fields
            "not-a-dict",  # type: ignore[list-item]
        ]
        assert realized_by_day(events, {}) == {}

    def test_unknown_entry_is_skipped_not_fabricated(self) -> None:
        events = [
            {"ts": _iso(11, 0), "event": "exit_fill", "structure": "qqq-nov",
             "filled": 2, "avg": "0.35"},
        ]
        assert realized_by_day(events, {}) == {}


class TestUnrealizedEod:
    def _row(self, ts: str, open_total: str, quoted: int = 1) -> dict:
        return {
            "ts": ts,
            "total_unrealized_open": open_total,
            "quote_coverage": {"open": 1, "quoted": quoted},
        }

    def test_last_sample_per_day_open_basis(self) -> None:
        rows = [
            self._row(_iso(10, 0), "-10.00"),
            self._row(_iso(15, 0), "-14.00"),  # day's last
            self._row(_iso(10, 0, day=23), "-5.00"),
        ]
        by_day = unrealized_eod_by_day(rows)
        assert by_day["2026-09-22"] == pytest.approx(-14.0)
        assert by_day["2026-09-23"] == pytest.approx(-5.0)

    def test_quoteless_day_is_a_gap_not_zero(self) -> None:
        rows = [self._row(_iso(15, 0), "-14.00", quoted=0)]
        assert unrealized_eod_by_day(rows) == {"2026-09-22": None}

    def test_out_of_order_rows_sorted_by_instant(self) -> None:
        rows = [
            self._row(_iso(15, 0), "-14.00"),
            self._row(_iso(10, 0), "-10.00"),
        ]
        assert unrealized_eod_by_day(rows) == {"2026-09-22": pytest.approx(-14.0)}

    def test_empty(self) -> None:
        assert unrealized_eod_by_day([]) == {}


class TestStatsPayload:
    def _seed(self, tmp_path: Path) -> tuple[Path, Path, Path]:
        from tests.unit.test_trex_web import PLAN_TOML, _write_plan
        from tree_options.trex.state import BookState, Status

        plans = tmp_path / "plans"
        _write_plan(plans, body=PLAN_TOML)
        state = tmp_path / "state"
        run = state / "putspread-test"
        run.mkdir(parents=True)
        book = BookState(["nvda-oct", "qqq-nov"])
        st = book.structures["nvda-oct"]
        st.to(Status.ENTER_WORKING, datetime(2026, 9, 22, 10, 5, tzinfo=ET))
        st.to(Status.OPEN, datetime(2026, 9, 22, 10, 5, tzinfo=ET))
        st.filled_qty = 5
        st.entry_fill = Decimal("0.21")
        st.exit_fill = Decimal("0.35")
        st.exit_filled_qty = 2
        st.to(Status.EXIT_WORKING, datetime(2026, 9, 22, 11, 0, tzinfo=ET))
        book.heartbeat = datetime(2026, 9, 22, 12, 0, tzinfo=ET)
        book.save(run / "book.json")
        (run / "events.jsonl").write_text(
            "\n".join(
                [
                    f'{{"ts": "{_iso(10, 0)}", "event": "entry_fill", "structure": "nvda-oct", "filled": 5, "avg": "0.21"}}',
                    f'{{"ts": "{_iso(11, 0)}", "event": "exit_fill", "structure": "nvda-oct", "filled": 2, "avg": "0.35"}}',
                ]
            )
            + "\n"
        )
        from tree_options.trex.history import append_line

        append_line(run / "marks_history.jsonl", {
            "ts": _iso(10, 0), "total_unrealized": "0.00",
            "total_unrealized_open": "0.00",
            "quote_coverage": {"open": 1, "quoted": 1},
        })
        append_line(run / "marks_history.jsonl", {
            "ts": _iso(15, 0), "total_unrealized": "-5.00",
            "total_unrealized_open": "-5.00",
            "quote_coverage": {"open": 1, "quoted": 1},
        })
        disc = tmp_path / "discovery"
        disc.mkdir()
        append_line(disc / "account_history.jsonl",
                    {"ts": _iso(12, 0), "net_liquidation": "1000000.00"})
        append_line(disc / "account_history.jsonl",
                    {"ts": _iso(15, 0), "net_liquidation": "1000005.00"})
        return state, plans, disc

    def test_payload_shape_and_math(self, tmp_path: Path) -> None:
        state, plans, disc = self._seed(tmp_path)
        payload = stats_payload(state, plans, disc, datetime(2026, 9, 22, 16, 0, tzinfo=ET))
        assert payload["tracking_since"].startswith("2026-09-22")
        assert payload["equity"] is not None
        assert payload["equity"]["last"]["value"] == pytest.approx(1000005.0)
        assert payload["totals"]["realized"] == pytest.approx(28.0)
        assert payload["totals"]["unrealized_last"] == pytest.approx(-5.0)
        assert payload["totals"]["structures_closed"] == 0
        assert payload["days"] == [
            {"date": "2026-09-22", "realized": pytest.approx(28.0),
             "unrealized_eod": pytest.approx(-5.0), "total": pytest.approx(23.0)}
        ]
        assert payload["per_plan"][0]["plan_id"] == "putspread-test"
        row = payload["per_structure"][0]
        assert row["structure_id"] == "nvda-oct" and row["underlying"] == "NVDA"
        assert row["realized"] == pytest.approx(28.0)

    def test_empty_state_is_honest(self, tmp_path: Path) -> None:
        (tmp_path / "plans").mkdir()
        (tmp_path / "state").mkdir()
        disc = tmp_path / "discovery"
        disc.mkdir()
        payload = stats_payload(
            tmp_path / "state", tmp_path / "plans", disc,
            datetime(2026, 9, 22, 16, 0, tzinfo=ET),
        )
        assert payload["equity"] is None
        assert payload["tracking_since"] is None
        assert payload["days"] == []
        assert payload["totals"]["win_rate"] is None


class TestReadersWired:
    def test_readers_importable_from_stats(self, tmp_path: Path) -> None:
        from tree_options.trex.history import append_line

        run = tmp_path / "s" / "p"
        run.mkdir(parents=True)
        append_line(run / "marks_history.jsonl", {"ts": _iso(10, 0), "total_unrealized": "1.00"})
        assert read_marks_history(tmp_path / "s", "p")[0]["total_unrealized"] == "1.00"
        d = tmp_path / "d"
        d.mkdir()
        append_line(d / "account_history.jsonl", {"ts": _iso(10, 0), "net_liquidation": "2.00"})
        assert read_account_history(d)[0]["net_liquidation"] == "2.00"
