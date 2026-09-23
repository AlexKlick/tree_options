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


class TestSaveOwned:
    """Both runners save the whole book; each adopts the other's lane."""

    def test_adopts_their_structures_and_the_fresher_heartbeat(self, tmp_path: Path) -> None:
        path = tmp_path / "book.json"
        disk = BookState(["a", "b"])
        disk.structures["a"].to(Status.ENTER_WORKING, datetime.now(ET))
        disk.heartbeat = datetime(2026, 9, 23, 10, 0, 30, tzinfo=ET)
        disk.save(path)
        mine = BookState(["a", "b"])  # stale: both PLANNED, older beat
        mine.heartbeat = datetime(2026, 9, 23, 10, 0, 0, tzinfo=ET)
        mine.structures["b"].to(Status.CLOSED, datetime.now(ET))
        mine.save_owned(path, lambda _mine, on_disk: on_disk.status is Status.ENTER_WORKING)
        saved = BookState.load(path, ["a", "b"])
        assert saved.structures["a"].status is Status.ENTER_WORKING  # theirs, adopted
        assert saved.structures["b"].status is Status.CLOSED  # mine, kept
        assert saved.heartbeat == datetime(2026, 9, 23, 10, 0, 30, tzinfo=ET)

    def test_refuses_to_overwrite_an_unreadable_book(self, tmp_path: Path) -> None:
        """Codex P1: exposure can't be ruled out from a torn book, and a
        blind overwrite would erase whatever the other runner recorded."""
        from tree_options.trex.state import BookUnreadableError

        path = tmp_path / "book.json"
        path.write_text("{torn")
        with pytest.raises(BookUnreadableError):
            BookState(["a"]).save_owned(path, lambda *_: True)
        assert path.read_text() == "{torn"

    def test_writers_serialize_on_the_book_lock(self, tmp_path: Path) -> None:
        """Codex P1: load + merge + write + replace must be one critical
        section shared by both runners, or a stale copy lands last."""
        import fcntl
        import threading

        path = tmp_path / "book.json"
        BookState(["a"]).save(path)
        done = threading.Event()
        with open(tmp_path / "book.json.lock", "a") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            writer = threading.Thread(
                target=lambda: (BookState(["a"]).save_owned(path, lambda *_: False), done.set())
            )
            writer.start()
            assert not done.wait(0.3)  # blocked behind the other runner's lock
            fcntl.flock(held, fcntl.LOCK_UN)
        writer.join(5)
        assert done.is_set()

    def test_temp_files_are_per_writer(self, tmp_path: Path) -> None:
        """Codex P1: both runners renamed the same ``book.tmp``."""
        path = tmp_path / "book.json"
        (tmp_path / "book.tmp").mkdir()  # the old shared name, occupied
        BookState(["a"]).save(path)
        BookState(["a"]).save_owned(path, lambda *_: False)
        assert BookState.load(path, ["a"]).structures["a"].status is Status.PLANNED
        assert not [p for p in tmp_path.iterdir() if p.name.endswith(".tmp") and p.is_file()]


_ORDER = [Status.PLANNED, Status.ENTER_WORKING, Status.OPEN, Status.EXIT_WORKING, Status.CLOSED]


@pytest.mark.parametrize("on_disk", _ORDER)
@pytest.mark.parametrize("mine", _ORDER)
def test_merge_never_moves_a_structure_backwards(
    tmp_path: Path, on_disk: Status, mine: Status
) -> None:
    """Even when this writer owns the structure, its copy only wins if the
    state machine can get there from what is on disk: CLOSED never comes
    back, OPEN never returns to the entry lane. The machine's own back
    edges (a cancelled reprice to PLANNED, a cancelled exit to OPEN) stay
    possible."""
    from tree_options.trex.state import status_reachable

    path = tmp_path / "book.json"
    disk = BookState(["a"])
    disk.structures["a"] = StructureState(status=on_disk)
    disk.save(path)
    book = BookState(["a"])
    book.structures["a"] = StructureState(status=mine)
    book.save_owned(path, lambda *_: False)  # "mine" by ownership
    saved = BookState.load(path, ["a"]).structures["a"].status
    assert saved is (mine if status_reachable(on_disk, mine) else on_disk)
    if on_disk is Status.CLOSED:
        assert saved is Status.CLOSED
    if on_disk in (Status.OPEN, Status.EXIT_WORKING):
        assert saved not in (Status.PLANNED, Status.ENTER_WORKING)


def test_reachability_table() -> None:
    from tree_options.trex.state import status_reachable

    assert status_reachable(Status.PLANNED, Status.OPEN)  # placed and filled in one save
    assert status_reachable(Status.EXIT_WORKING, Status.OPEN)  # cancelled exit
    assert status_reachable(Status.ENTER_WORKING, Status.PLANNED)  # cancelled reprice
    assert not status_reachable(Status.CLOSED, Status.OPEN)
    assert not status_reachable(Status.OPEN, Status.ENTER_WORKING)
    assert all(status_reachable(s, s) for s in Status)


def test_entry_order_checkpoint_round_trips() -> None:
    """Codex P1: the entry order's recorded fills persist with the book, so
    a restarted entry runner never re-adds them."""
    st = StructureState(
        status=Status.ENTER_WORKING,
        entry_order="201",
        filled_qty=2,
        entry_order_seen=2,
        entry_order_notional=Decimal("0.88"),
    )
    back = StructureState.from_dict(json.loads(json.dumps(st.to_dict())))
    assert back.entry_order_seen == 2 and back.entry_order_notional == Decimal("0.88")
    legacy = StructureState.from_dict({"status": "enter_working"})
    assert legacy.entry_order_seen == 0 and legacy.entry_order_notional is None


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
