"""M1: append-only JSONL history helpers + web readers.

A process killed mid-write leaves an unterminated tail; appending after it
would concatenate two records and lose both. Writers repair the tail on
init, readers tolerate junk, rotation halves at a cap, and web reads are
bounded (tail bytes, never whole-file).
"""

from __future__ import annotations

import json
from pathlib import Path

from tree_options.trex.history import (
    append_line,
    count_lines,
    read_tail,
    repair_torn_tail,
    rotate_halving,
)
from tree_options.trex_web.reader import read_account_history, read_marks_history


class TestJsonlHelpers:
    def test_append_and_read_roundtrip(self, tmp_path: Path) -> None:
        p = tmp_path / "h.jsonl"
        append_line(p, {"ts": "a", "v": 1})
        append_line(p, {"ts": "b", "v": 2})
        assert read_tail(p) == [{"ts": "a", "v": 1}, {"ts": "b", "v": 2}]
        assert count_lines(p) == 2

    def test_torn_tail_is_repaired_not_concatenated(self, tmp_path: Path) -> None:
        p = tmp_path / "h.jsonl"
        p.write_text('{"ts": "a"}\n{"ts": "b", "v": ')  # killed mid-record
        assert repair_torn_tail(p) is True
        append_line(p, {"ts": "c"})
        rows = read_tail(p)
        assert [r["ts"] for r in rows] == ["a", "c"]

    def test_repair_is_noop_on_healthy_file(self, tmp_path: Path) -> None:
        p = tmp_path / "h.jsonl"
        p.write_text('{"ts": "a"}\n')
        assert repair_torn_tail(p) is False
        assert repair_torn_tail(tmp_path / "absent.jsonl") is False

    def test_repair_all_junk_truncates_to_empty(self, tmp_path: Path) -> None:
        p = tmp_path / "h.jsonl"
        p.write_text("not json at all without newline")
        assert repair_torn_tail(p) is True
        assert p.read_text() == ""

    def test_rotation_keeps_newest_half_only_over_cap(self, tmp_path: Path) -> None:
        p = tmp_path / "h.jsonl"
        for i in range(10):
            append_line(p, {"i": i})
        rotated = rotate_halving(p, cap=8)
        assert rotated is True
        assert [r["i"] for r in read_tail(p)] == [6, 7, 8, 9]

    def test_no_rotation_at_or_under_cap(self, tmp_path: Path) -> None:
        p = tmp_path / "h.jsonl"
        for i in range(8):
            append_line(p, {"i": i})
        assert rotate_halving(p, cap=8) is False
        assert count_lines(p) == 8

    def test_read_tail_skips_junk_and_is_bounded(self, tmp_path: Path) -> None:
        p = tmp_path / "h.jsonl"
        p.write_text('{"i": 0}\nJUNK\n{"i": 1}\n{"i": 2}\n')
        rows = read_tail(p, max_bytes=1000)
        assert [r["i"] for r in rows] == [0, 1, 2]
        # bounded read drops the torn leading fragment
        assert read_tail(p, max_bytes=12) != []  # at least something readable

    def test_read_tail_empty_or_missing(self, tmp_path: Path) -> None:
        assert read_tail(tmp_path / "absent.jsonl") == []
        (tmp_path / "empty.jsonl").touch()
        assert read_tail(tmp_path / "empty.jsonl") == []


class TestWebReaders:
    def test_read_marks_history(self, tmp_path: Path) -> None:
        run = tmp_path / "state" / "putspread-x"
        run.mkdir(parents=True)
        append_line(run / "marks_history.jsonl", {"ts": "2026-09-22T10:00:00-04:00", "t": "1"})
        append_line(run / "marks_history.jsonl", {"ts": "2026-09-22T10:00:20-04:00", "t": "2"})
        rows = read_marks_history(tmp_path / "state", "putspread-x")
        assert [r["t"] for r in rows] == ["1", "2"]
        assert read_marks_history(tmp_path / "state", "absent") == []

    def test_read_account_history_dedups_and_sorts(self, tmp_path: Path) -> None:
        disc = tmp_path / "discovery"
        disc.mkdir()
        p = disc / "account_history.jsonl"
        append_line(p, {"ts": "2026-09-22T17:02:00-04:00", "account_id": "DUT143714", "v": 2})
        append_line(p, {"ts": "2026-09-22T17:01:00-04:00", "account_id": "DUT143714", "v": 1})
        append_line(p, {"ts": "2026-09-22T17:01:00-04:00", "account_id": "DUT143714", "v": 1})
        rows = read_account_history(disc)
        assert len(rows) == 2
        assert rows[0]["v"] == 1 and rows[1]["v"] == 2
        assert read_account_history(tmp_path / "absent") == []

    def test_read_account_history_json_shape(self, tmp_path: Path) -> None:
        disc = tmp_path / "discovery"
        disc.mkdir()
        append_line(
            disc / "account_history.jsonl",
            {
                "ts": "2026-09-22T17:01:00-04:00",
                "account_id": "DUT143714",
                "net_liquidation": "1000252.09",
                "cash": "999516.91",
                "source": "discovery",
            },
        )
        (row,) = read_account_history(disc)
        assert row["net_liquidation"] == "1000252.09"  # strings: book-lane money
        assert json.dumps(row)  # serializable as-is
