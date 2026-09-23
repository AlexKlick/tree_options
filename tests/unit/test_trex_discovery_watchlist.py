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


class TestProposalLifecycle:
    """M6 (Codex-arch #12): durable ids + provenance, no regenerate-over-
    pending, no reviving dismissals for a week, idempotent decisions."""

    PROV: ClassVar[dict] = {"provider": "local", "model": "Qwen/Qwen3.8-27B",
                            "trigger": "operator", "source_id": "r1"}

    def _wl(self, tmp_path: Path) -> None:
        load_watchlist(tmp_path, seed=["SPY", "QQQ"], now=NOW)

    def test_record_pending_and_block_duplicates(self, tmp_path: Path) -> None:
        from tree_options.trex.discovery.watchlist import blocked_symbols, record_proposals

        self._wl(tmp_path)
        ids = record_proposals(
            tmp_path, [{"symbol": "TSM", "action": "add", "rationale": "r", "confidence": 0.7}],
            self.PROV, NOW, run_note={"status": "ok"},
        )
        assert len(ids) == 1
        doc = load_watchlist(tmp_path)
        prop = doc["proposals"][0]
        assert prop["status"] == "pending" and prop["provenance"]["model"] == "Qwen/Qwen3.8-27B"
        assert doc["last_proposal_run"]["added"] == 1
        assert "TSM" in blocked_symbols(doc, NOW)
        again = record_proposals(tmp_path, [{"symbol": "TSM", "action": "add"}], self.PROV, NOW)
        assert again == []  # never regenerate over a pending proposal

    def test_approve_add_is_idempotent(self, tmp_path: Path) -> None:
        from tree_options.trex.discovery.watchlist import record_proposals

        self._wl(tmp_path)
        (pid,) = record_proposals(tmp_path, [{"symbol": "TSM", "action": "add"}], self.PROV, NOW)
        first = apply_watch_op(tmp_path, "approve", proposal_id=pid, now=NOW)
        assert first["status"] == "approved"
        row = next(r for r in load_watchlist(tmp_path)["symbols"] if r["symbol"] == "TSM")
        assert row["origin"] == "llm" and row["proposal_id"] == pid
        assert apply_watch_op(tmp_path, "approve", proposal_id=pid, now=NOW)["status"] == "noop"
        assert apply_watch_op(tmp_path, "dismiss", proposal_id=pid, now=NOW)["status"] == "noop"
        syms = [r["symbol"] for r in load_watchlist(tmp_path)["symbols"]]
        assert syms.count("TSM") == 1

    def test_approve_remove(self, tmp_path: Path) -> None:
        from tree_options.trex.discovery.watchlist import record_proposals

        self._wl(tmp_path)
        (pid,) = record_proposals(tmp_path, [{"symbol": "QQQ", "action": "remove"}], self.PROV, NOW)
        apply_watch_op(tmp_path, "approve", proposal_id=pid, now=NOW)
        assert [r["symbol"] for r in load_watchlist(tmp_path)["symbols"]] == ["SPY"]

    def test_dismissal_suppresses_for_a_week(self, tmp_path: Path) -> None:
        from tree_options.trex.discovery.watchlist import blocked_symbols, record_proposals

        self._wl(tmp_path)
        (pid,) = record_proposals(tmp_path, [{"symbol": "TSM", "action": "add"}], self.PROV, NOW)
        assert apply_watch_op(tmp_path, "dismiss", proposal_id=pid, now=NOW)["status"] == "dismissed"
        doc = load_watchlist(tmp_path)
        assert "TSM" not in [r["symbol"] for r in doc["symbols"]]
        six_days = datetime(2026, 9, 28, 19, 0, tzinfo=ET)
        eight_days = datetime(2026, 9, 30, 19, 0, tzinfo=ET)
        assert "TSM" in blocked_symbols(doc, six_days)
        assert "TSM" not in blocked_symbols(doc, eight_days)

    def test_unknown_proposal_is_invalid(self, tmp_path: Path) -> None:
        self._wl(tmp_path)
        assert apply_watch_op(tmp_path, "approve", proposal_id="nope", now=NOW)["status"] == "invalid"

    def test_long_decided_proposals_are_pruned_pending_kept(self, tmp_path: Path) -> None:
        from tree_options.trex.discovery.watchlist import record_proposals

        self._wl(tmp_path)
        (old,) = record_proposals(tmp_path, [{"symbol": "TSM", "action": "add"}], self.PROV, NOW)
        apply_watch_op(tmp_path, "dismiss", proposal_id=old, now=NOW)
        record_proposals(tmp_path, [{"symbol": "SMH", "action": "add"}], self.PROV, NOW)
        later = datetime(2026, 11, 1, 19, 0, tzinfo=ET)  # > 30 days
        record_proposals(tmp_path, [], self.PROV, later)
        props = load_watchlist(tmp_path)["proposals"]
        assert [p["symbol"] for p in props] == ["SMH"]  # pending survives, decided pruned


class TestCodexM456Watchlist:
    PROV: ClassVar[dict] = {"provider": "local", "model": "m", "trigger": "t", "source_id": "s"}

    def test_read_watchlist_never_creates_state(self, tmp_path: Path) -> None:
        from tree_options.trex.discovery.watchlist import read_watchlist

        doc = read_watchlist(tmp_path)
        assert doc["symbols"] == [] and not (tmp_path / "watchlist.json").exists()

    def test_cap_never_evicts_pending_or_suppressing_dismissals(self, tmp_path: Path) -> None:
        from tree_options.trex.discovery.watchlist import (
            MAX_PROPOSALS_KEPT,
            blocked_symbols,
            record_proposals,
        )

        load_watchlist(tmp_path, seed=[], now=NOW)
        doc = load_watchlist(tmp_path)
        doc["proposals"] = [
            {"id": f"p{i}", "symbol": f"S{chr(65 + i % 26)}{chr(65 + i // 26)}",
             "action": "add", "status": "pending", "created_at": NOW.isoformat(),
             "decided_at": None}
            for i in range(MAX_PROPOSALS_KEPT)
        ]
        doc["proposals"].insert(0, {"id": "old-dismissed", "symbol": "TSM", "action": "add",
                                    "status": "dismissed", "created_at": "2026-01-01T00:00:00-05:00",
                                    "decided_at": NOW.isoformat()})
        (tmp_path / "watchlist.json").write_text(json.dumps(doc))
        record_proposals(tmp_path, [{"symbol": "ZZ", "action": "add"}], self.PROV, NOW)
        after = load_watchlist(tmp_path)
        ids = {p["id"] for p in after["proposals"]}
        assert {f"p{i}" for i in range(MAX_PROPOSALS_KEPT)} <= ids  # no pending evicted
        assert "old-dismissed" in ids and "TSM" in blocked_symbols(after, NOW)
        assert apply_watch_op(tmp_path, "approve", proposal_id="p0", now=NOW)["status"] == "approved"
