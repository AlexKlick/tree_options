"""M5b: the watchlist - operator-owned symbol list with idempotent
mutations applied through the spool (trex-web stays read-only)."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import ClassVar
from zoneinfo import ZoneInfo

from tree_options.trex.discovery.watchlist import (
    apply_watch_op,
    load_watchlist,
    seed_symbols,
)

ET = ZoneInfo("America/New_York")
NOW = datetime(2026, 9, 22, 19, 0, tzinfo=ET)


class TestLoadSeed:
    def test_absent_creates_from_seed(self, tmp_path: Path) -> None:
        wl = load_watchlist(tmp_path, seed=["NVDA", "QQQ"], now=NOW)
        assert [s["symbol"] for s in wl["symbols"]] == ["NVDA", "QQQ"]
        assert all(s["origin"] == "seed" for s in wl["symbols"])
        # persisted
        doc = json.loads((tmp_path / "watchlist.json").read_text())
        assert doc["version"] == 1

    def test_existing_is_returned_unchanged(self, tmp_path: Path) -> None:
        load_watchlist(tmp_path, seed=["NVDA"], now=NOW)
        wl = load_watchlist(tmp_path, seed=["SPY"], now=NOW)
        assert [s["symbol"] for s in wl["symbols"]] == ["NVDA"]

    def test_seed_symbols_union(self, tmp_path: Path) -> None:
        plans = tmp_path / "plans"
        plans.mkdir()
        (plans / "p.toml").write_text(
            'id = "p"\naccount_mode = "paper"\ntotal_debit_cap = 100.0\n'
            'entry_window_start = "09:45"\nentry_window_end = "12:00"\n\n'
            "[[structures]]\nid = \"a\"\nunderlying = \"MU\"\n"
            "entry_date = 2026-09-18\nexpiry = 2026-10-16\nlong_strike = 100.0\n"
            "short_strike = 95.0\nquantity = 1\nlimit_cap = 0.5\nexit_deadline = 2026-10-09\n"
        )

        class Cfg:
            underlyings: ClassVar[list[str]] = ["NVDA", "QQQ"]

        assert seed_symbols(Cfg(), plans) == ["NVDA", "QQQ", "MU"]


class TestApplyOps:
    def test_add_uppercases_and_validates(self, tmp_path: Path) -> None:
        apply_watch_op(tmp_path, "add", symbol="spy", now=NOW)
        wl = load_watchlist(tmp_path)
        assert [s["symbol"] for s in wl["symbols"]] == ["SPY"]
        assert wl["symbols"][0]["origin"] == "operator"

    def test_add_is_idempotent(self, tmp_path: Path) -> None:
        apply_watch_op(tmp_path, "add", symbol="SPY", now=NOW)
        result = apply_watch_op(tmp_path, "add", symbol="SPY", now=NOW)
        wl = load_watchlist(tmp_path)
        assert len(wl["symbols"]) == 1
        assert result["status"] == "noop"

    def test_add_rejects_bad_symbols(self, tmp_path: Path) -> None:
        for bad in ("", "TOOLONGSYMBOL", "BRK A", "SPY$"):
            result = apply_watch_op(tmp_path, "add", symbol=bad, now=NOW)
            assert result["status"] == "invalid", bad

    def test_remove_is_idempotent(self, tmp_path: Path) -> None:
        apply_watch_op(tmp_path, "add", symbol="SPY", now=NOW)
        assert apply_watch_op(tmp_path, "remove", symbol="SPY", now=NOW)["status"] == "removed"
        assert apply_watch_op(tmp_path, "remove", symbol="SPY", now=NOW)["status"] == "noop"
        assert load_watchlist(tmp_path)["symbols"] == []

    def test_unknown_op_rejected(self, tmp_path: Path) -> None:
        result = apply_watch_op(tmp_path, "explode", symbol="SPY", now=NOW)
        assert result["status"] == "invalid"
