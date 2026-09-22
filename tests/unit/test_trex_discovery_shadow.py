"""M3: shadow alternatives - forward paper-tracking of candidates NOT taken.

Identity: instrument key underlying|yyyymmdd|short|long; an EPISODE is one
instrument opened by one scan run (re-entry after close opens a new
episode; history is never overwritten). Identities matching live plan
structures are excluded. Marking: from the scan's own rows first (M5
upgrades to the CBOE full chain); a miss carries the last mark with the
source disclosed. Expiry finalizes at intrinsic with the quote spot.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from tree_options.trex.discovery.shadow import (
    ShadowBook,
    append_shadow_mark,
    load_shadow,
    mark_from_payload,
    open_from_scan,
    save_shadow,
    shadow_key,
    shadow_stats,
)

ET = ZoneInfo("America/New_York")
NOW = datetime(2026, 9, 22, 16, 11, tzinfo=ET)


def _cand(
    underlying: str = "NVDA",
    expiry: str = "20261016",
    short: float = 150.0,
    long: float = 185.0,
    debit_mid: float = 0.21,
) -> dict:
    return {
        "underlying": underlying,
        "expiry": expiry,
        "short_strike": short,
        "long_strike": long,
        "debit_mid": debit_mid,
        "accepted": True,
    }


def _payload(cands: list[dict], rejected: list[dict] | None = None) -> dict:
    return {"candidates": cands, "rejected": rejected or []}


class TestKey:
    def test_canonical_instrument_key(self) -> None:
        assert shadow_key("NVDA", "20261016", 150.0, 185.0) == "NVDA|20261016|150|185"
        assert shadow_key("NVDA", "20261016", 150.5, 185.25) == "NVDA|20261016|150.5|185.25"

    def test_key_is_float_artifact_free(self) -> None:
        # 150 stored as 150.0 vs 150 must key identically
        assert shadow_key("QQQ", "20261120", 150, 185) == shadow_key("QQQ", "20261120", 150.0, 185.0)


class TestOpenFromScan:
    def test_opens_one_contract_per_new_accepted_candidate(self, tmp_path: Path) -> None:
        book = open_from_scan(
            ShadowBook(), _payload([_cand()]), run_id="run-1", now=NOW
        )
        assert len(book.positions) == 1
        pos = book.positions[0]
        assert pos.key == "NVDA|20261016|150|185"
        assert pos.episode_id == "NVDA|20261016|150|185#run-1"
        assert pos.debit_paid == pytest.approx(0.21)
        assert pos.qty == 1
        assert pos.status == "open"
        assert pos.opened_run_id == "run-1"

    def test_dedupes_open_identity_across_scans(self, tmp_path: Path) -> None:
        book = open_from_scan(ShadowBook(), _payload([_cand()]), "run-1", NOW)
        book = open_from_scan(book, _payload([_cand(debit_mid=0.30)]), "run-2", NOW)
        assert len(book.positions) == 1  # same instrument, still open: no second episode

    def test_reentry_after_close_opens_new_episode(self, tmp_path: Path) -> None:
        book = open_from_scan(ShadowBook(), _payload([_cand()]), "run-1", NOW)
        book.positions[0].status = "expired"
        book = open_from_scan(book, _payload([_cand()]), "run-2", NOW)
        assert len(book.positions) == 2
        assert {p.opened_run_id for p in book.positions} == {"run-1", "run-2"}

    def test_excludes_live_plan_structures_normalized(self) -> None:
        # plan specs carry date objects + Decimal-able strikes
        excluded = [("NVDA", date(2026, 10, 16), "150", "185")]
        book = open_from_scan(
            ShadowBook(), _payload([_cand()]), "run-1", NOW, excluded=excluded
        )
        assert book.positions == []

    def test_only_accepted_candidates_open(self) -> None:
        rejected = _cand()
        book = open_from_scan(ShadowBook(), _payload([], [rejected]), "run-1", NOW)
        assert book.positions == []

    def test_missing_debit_mid_is_skipped(self) -> None:
        book = open_from_scan(
            ShadowBook(), _payload([_cand(debit_mid=None)]), "run-1", NOW
        )
        assert book.positions == []


class TestMarkFromPayload:
    def test_marks_from_later_scan_rows(self) -> None:
        book = open_from_scan(ShadowBook(), _payload([_cand()]), "run-1", NOW)
        later = datetime(2026, 9, 23, 16, 11, tzinfo=ET)
        book = mark_from_payload(
            book, _payload([_cand(debit_mid=0.35)]), now=later
        )
        pos = book.positions[0]
        assert pos.last_mark == pytest.approx(0.35)
        assert pos.mark_source == "scan"
        assert pos.pnl == pytest.approx((0.35 - 0.21) * 100)
        assert pos.best_pnl == pytest.approx(14.0)
        assert pos.worst_pnl == pytest.approx(14.0)

    def test_unquoted_position_carries_last_mark_with_disclosure(self) -> None:
        book = open_from_scan(ShadowBook(), _payload([_cand()]), "run-1", NOW)
        # a later scan that does not re-quote this strike (the Codex-7 trap)
        book = mark_from_payload(book, _payload([_cand(short=999.0)]), now=NOW)
        pos = book.positions[0]
        assert pos.last_mark is None
        assert pos.mark_source == "none"
        assert pos.pnl is None

    def test_expiry_finalizes_at_intrinsic_with_spot(self) -> None:
        book = open_from_scan(ShadowBook(), _payload([_cand()]), "run-1", NOW)
        past = datetime(2026, 10, 17, 16, 0, tzinfo=ET)  # after expiry 10-16
        book = mark_from_payload(book, _payload([]), now=past, spots={"NVDA": 160.0})
        pos = book.positions[0]
        assert pos.status == "expired"
        assert pos.mark_source == "intrinsic-approx"
        # intrinsic: (185-160) - (150-160 -> 0) = 25 -> pnl (25-0.21)*100
        assert pos.final_pnl == pytest.approx((25.0 - 0.21) * 100)

    def test_expiry_intrinsic_floored_at_zero_per_leg(self) -> None:
        book = open_from_scan(ShadowBook(), _payload([_cand()]), "run-1", NOW)
        past = datetime(2026, 10, 17, 16, 0, tzinfo=ET)
        book = mark_from_payload(book, _payload([]), now=past, spots={"NVDA": 200.0})
        pos = book.positions[0]
        assert pos.final_pnl == pytest.approx(-0.21 * 100)  # both legs worthless


class TestPersistence:
    def test_roundtrip_and_atomic_save(self, tmp_path: Path) -> None:
        book = open_from_scan(ShadowBook(), _payload([_cand()]), "run-1", NOW)
        save_shadow(tmp_path, book)
        loaded = load_shadow(tmp_path)
        assert loaded is not None
        assert loaded.positions[0].episode_id == book.positions[0].episode_id
        assert not list(tmp_path.glob("shadow_book.json.tmp"))

    def test_load_absent_is_none(self, tmp_path: Path) -> None:
        assert load_shadow(tmp_path) is None

    def test_marks_append_and_rotation(self, tmp_path: Path) -> None:
        marks = [{"key": "K", "value": 0.3, "pnl": 9.0, "source": "scan"}]
        for i in range(10):
            append_shadow_mark(tmp_path, f"run-{i}", marks, NOW)
        path = tmp_path / "shadow_marks.jsonl"
        assert len(path.read_text().splitlines()) == 10
        # junk tail tolerated
        with path.open("a") as f:
            f.write("{torn")
        lines = path.read_text().splitlines()
        rows = [
            json.loads(line)
            for line in lines
            if line.startswith("{") and "run" in line
        ]
        assert len(rows) == 10


class TestStats:
    def test_stats_shape(self) -> None:
        book = open_from_scan(ShadowBook(), _payload([_cand()]), "run-1", NOW)
        book = mark_from_payload(book, _payload([_cand(debit_mid=0.35)]), NOW)
        s = shadow_stats(book)
        assert s["open"] == 1 and s["expired"] == 0
        assert s["mean_pnl"] == pytest.approx(14.0)
        assert s["hit_rate"] == pytest.approx(1.0)
        assert s["not_executed"] is True
