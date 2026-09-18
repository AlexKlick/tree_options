"""trex state tests: legal transitions, durable round-trips, the arm gate."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tree_options.trex.clock import ET
from tree_options.trex.state import (
    BookState,
    Status,
    StructureState,
    transition_allowed,
)


class TestTransitions:
    def test_closed_is_terminal(self) -> None:
        for target in Status:
            assert not transition_allowed(Status.CLOSED, target)

    def test_planned_can_only_start_or_die(self) -> None:
        assert transition_allowed(Status.PLANNED, Status.ENTER_WORKING)
        assert transition_allowed(Status.PLANNED, Status.CLOSED)
        assert not transition_allowed(Status.PLANNED, Status.OPEN)

    def test_open_must_flatten_not_flee(self) -> None:
        assert transition_allowed(Status.OPEN, Status.EXIT_WORKING)
        assert not transition_allowed(Status.OPEN, Status.CLOSED)

    def test_to_enforces_legality(self) -> None:
        st = StructureState()
        with pytest.raises(ValueError, match="illegal transition"):
            st.to(Status.OPEN, datetime.now(ET))


class TestPersistence:
    def test_round_trip_preserves_money_and_status(self, tmp_path: Path) -> None:
        book = BookState(["a", "b"])
        st = book.structures["a"]
        now = datetime(2026, 9, 18, 10, 0, tzinfo=ET)
        st.to(Status.ENTER_WORKING, now)
        st.to(Status.OPEN, now)
        st.entry_fill = Decimal("0.44")
        st.to(Status.EXIT_WORKING, now)
        st.exit_reason = "touch"

        path = tmp_path / "book.json"
        book.save(path)
        loaded = BookState.load(path, ["a", "b"])
        a = loaded.structures["a"]
        assert a.status is Status.EXIT_WORKING
        assert a.entry_fill == Decimal("0.44")
        assert a.exit_reason == "touch"
        assert loaded.structures["b"].status is Status.PLANNED

    def test_load_seeds_new_structure_ids(self, tmp_path: Path) -> None:
        book = BookState(["a"])
        path = tmp_path / "book.json"
        book.save(path)
        loaded = BookState.load(path, ["a", "late-addition"])
        assert loaded.structures["late-addition"].status is Status.PLANNED

    def test_save_is_atomic_no_tmp_residue(self, tmp_path: Path) -> None:
        book = BookState(["a"])
        path = tmp_path / "book.json"
        book.save(path)
        book.save(path)
        assert not path.with_suffix(".tmp").exists()
        assert len(list(tmp_path.iterdir())) == 1


class TestEventsAndHeartbeat:
    def test_event_appends_jsonl(self, tmp_path: Path) -> None:
        book = BookState(["a"])
        events = tmp_path / "events.jsonl"
        book.event(events, "heartbeat", structures=1)
        book.event(events, "order", oid=17, side="BUY")
        lines = [json.loads(line) for line in events.read_text().splitlines()]
        assert [rec["event"] for rec in lines] == ["heartbeat", "order"]
        assert lines[1]["oid"] == 17 and "ts" in lines[1]

    def test_armed_within_fresh_beat(self) -> None:
        book = BookState(["a"])
        assert not book.armed_within(30)  # never beat
        book.beat()
        assert book.armed_within(30)

    def test_armed_within_stale_beat(self) -> None:
        book = BookState(["a"])
        book.heartbeat = datetime.now(ET) - timedelta(seconds=61)
        assert not book.armed_within(30)
