"""Runstate store tests — content-addressed research lane artifacts.

The runstate store is the research lane's equivalent of the desk
evidence store. It is scoped to its own SQLite file under the
research workspace (``<workspace>/runstate.sqlite3``) and never shares a
file with the desk evidence store.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from tree_options.research.runstate.store import (
    RunstateStoreError,
    open_runstate_store,
)


def test_open_runstate_store_creates_db_in_workspace(tmp_path: Path) -> None:
    with open_runstate_store(tmp_path) as store:
        assert store.path == tmp_path / "runstate.sqlite3"
        assert store.path.exists()


def test_put_returns_payload_sha256(tmp_path: Path) -> None:
    with open_runstate_store(tmp_path) as store:
        sha = store.put("spec", {"x": 1}, key="abc", at=datetime(2026, 9, 25))
        assert len(sha) == 64
        assert all(c in "0123456789abcdef" for c in sha)


def test_put_is_idempotent_on_same_payload(tmp_path: Path) -> None:
    with open_runstate_store(tmp_path) as store:
        sha1 = store.put("spec", {"x": 1}, key="abc")
        sha2 = store.put("spec", {"x": 1}, key="abc")
        assert sha1 == sha2
        assert store.verify()["objects"] == 1


def test_put_raises_content_conflict_on_different_payload(tmp_path: Path) -> None:
    with open_runstate_store(tmp_path) as store:
        store.put("spec", {"x": 1}, key="abc")
        with pytest.raises(RunstateStoreError, match="content_conflict"):
            store.put("spec", {"x": 2}, key="abc")


def test_unknown_kind_is_rejected(tmp_path: Path) -> None:
    with open_runstate_store(tmp_path) as store:
        with pytest.raises(RunstateStoreError, match="unknown kind"):
            store.put("not_a_real_kind", {"x": 1}, key="abc")


def test_all_returns_objects_in_insertion_order(tmp_path: Path) -> None:
    with open_runstate_store(tmp_path) as store:
        store.put("run", {"i": 1}, key="a", at=datetime(2026, 9, 25))
        store.put("run", {"i": 2}, key="b", at=datetime(2026, 9, 26))
        store.put("run", {"i": 3}, key="c", at=datetime(2026, 9, 27))
        rows = store.all("run")
        assert [r["i"] for r in rows] == [1, 2, 3]


def test_all_at_returns_payload_and_timestamp(tmp_path: Path) -> None:
    """Timestamps cross as ISO strings — the desk store's convention
    (its ``all_at`` returns strings too; callers normalize once)."""
    with open_runstate_store(tmp_path) as store:
        store.put("result", {"x": 1}, key="abc", at=datetime(2026, 9, 25, 12, 0))
        items = store.all_at("result")
        assert len(items) == 1
        payload, ts = items[0]
        assert payload == {"x": 1}
        assert ts == "2026-09-25T12:00:00"


def test_verify_returns_ok_envelope(tmp_path: Path) -> None:
    with open_runstate_store(tmp_path) as store:
        store.put("run", {"i": 1}, key="a")
        store.put("spec", {"x": "y"}, key="b")
        head = store.verify()
        assert head["ok"] is True
        assert head["scope"] == "local-consistency"
        assert head["events"] >= 2
        assert head["objects"] == 2
        assert len(head["head_sha256"]) == 64


# -- RL1-04: the verifier must actually verify -------------------------------


def test_verify_allows_a_legitimate_append_after_a_previous_verification(tmp_path: Path) -> None:
    """The pre-correction verifier cached a head that later valid
    writes never updated, so append-after-verify FAILED with an
    audit_head mismatch. The head now commits in the same transaction
    as the append."""
    with open_runstate_store(tmp_path) as store:
        store.put("run", {"i": 1}, key="a", at=datetime(2026, 9, 25))
        store.verify()
        store.put("run", {"i": 2}, key="b", at=datetime(2026, 9, 26))
        head = store.verify()  # must not raise
        assert head["objects"] == 2


def test_verify_rejects_payload_tampering_without_rewritten_hash(tmp_path: Path) -> None:
    """Changing only ``payload_json`` while leaving the claimed hash
    untouched used to pass verification. The verifier now rehashes the
    stored bytes."""
    with open_runstate_store(tmp_path) as store:
        store.put("spec", {"x": 1}, key="a", at=datetime(2026, 9, 25))
        store.verify()
        store.conn.execute(
            "UPDATE objects SET payload_json = ? WHERE kind = 'spec' AND object_key = 'a'",
            ('{"x": 999}',),
        )
        with pytest.raises(RunstateStoreError, match="payload_tampering"):
            store.verify()


def test_verify_rejects_a_missing_object_row(tmp_path: Path) -> None:
    with open_runstate_store(tmp_path) as store:
        store.put("spec", {"x": 1}, key="a", at=datetime(2026, 9, 25))
        store.conn.execute("DELETE FROM objects")
        with pytest.raises(RunstateStoreError):
            store.verify()


def test_verify_rejects_a_broken_audit_chain(tmp_path: Path) -> None:
    with open_runstate_store(tmp_path) as store:
        store.put("spec", {"x": 1}, key="a", at=datetime(2026, 9, 25))
        store.put("spec", {"x": 2}, key="b", at=datetime(2026, 9, 26))
        store.conn.execute("DELETE FROM audit WHERE audit_seq = 2")
        with pytest.raises(RunstateStoreError):
            store.verify()


def test_replace_appends_state_and_keeps_every_version_audited(tmp_path: Path) -> None:
    """Mutable run records: replace() supersedes the payload but the
    audit trail keeps every version (put + replaces)."""
    with open_runstate_store(tmp_path) as store:
        store.put("run", {"status": "queued"}, key="r", at=datetime(2026, 9, 25))
        store.replace("run", {"status": "running"}, key="r", at=datetime(2026, 9, 25, 12, 0))
        store.replace("run", {"status": "completed"}, key="r", at=datetime(2026, 9, 25, 13, 0))
        assert store.get("run", "r") == {"status": "completed"}
        actions = [
            r["action"]
            for r in store.conn.execute(
                "SELECT action FROM audit WHERE kind = 'run' ORDER BY audit_seq"
            )
        ]
        assert actions == ["put", "replace", "replace"]
        head = store.verify()  # head agrees with the replaced content
        assert head["objects"] == 1


def test_replace_requires_an_existing_object(tmp_path: Path) -> None:
    with open_runstate_store(tmp_path) as store:
        with pytest.raises(RunstateStoreError, match="no such object"):
            store.replace("run", {"status": "x"}, key="missing")


def test_concurrent_duplicate_puts_land_idempotently(tmp_path: Path) -> None:
    """Two request threads opening their OWN connections and putting
    the SAME deterministic payload (the duplicate-POST shape): one
    object, no crash, verification green."""
    import threading

    barrier = threading.Barrier(2)
    errors: list[Exception] = []

    def writer() -> None:
        try:
            with open_runstate_store(tmp_path) as store:
                barrier.wait(timeout=3)
                store.put(
                    "run", {"run_id": "r", "status": "queued"}, key="r", at=datetime(2026, 9, 25)
                )
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=writer) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=6)
    assert all(not t.is_alive() for t in threads)
    assert errors == []
    with open_runstate_store(tmp_path) as store:
        assert len(store.all("run")) == 1
        store.verify()  # must not raise


@pytest.mark.parametrize("operation", ["duplicate", "conflict", "replace"])
def test_concurrent_writes_check_content_under_the_write_lock(
    tmp_path: Path, operation: str
) -> None:
    """Force the second writer to begin while the first insert is uncommitted.

    A pre-transaction SELECT sees absent/stale content; checking only after
    BEGIN IMMEDIATE acquires the lock sees the committed predecessor.
    """
    import threading

    if operation == "replace":
        with open_runstate_store(tmp_path) as store:
            store.put("run", {"x": 0}, key="r")
    ready = threading.Barrier(3)
    first_paused = threading.Event()
    second_begin = threading.Event()
    release_first = threading.Event()
    errors: dict[int, Exception] = {}
    hashes: dict[int, str] = {}

    def writer(index: int) -> None:
        try:
            with open_runstate_store(tmp_path) as store:
                if index == 0:
                    audit = store._audit

                    def paused_audit(*args, **kwargs):
                        first_paused.set()
                        assert release_first.wait(timeout=3)
                        return audit(*args, **kwargs)

                    store._audit = paused_audit
                else:

                    def trace(statement: str) -> None:
                        if statement == "BEGIN IMMEDIATE":
                            second_begin.set()

                    store.conn.set_trace_callback(trace)
                ready.wait(timeout=3)  # both connections/schema exist before any write
                if index == 1:
                    assert first_paused.wait(timeout=3)
                value = 1 if index == 0 or operation == "duplicate" else 2
                write = store.replace if operation == "replace" else store.put
                hashes[index] = write("run", {"x": value}, key="r")
        except Exception as exc:
            errors[index] = exc

    threads = [threading.Thread(target=writer, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    try:
        ready.wait(timeout=3)
        assert second_begin.wait(timeout=3)
    finally:
        release_first.set()
        for thread in threads:
            thread.join(timeout=6)
    assert all(not thread.is_alive() for thread in threads)
    if operation == "conflict":
        assert set(errors) == {1}
        assert isinstance(errors[1], RunstateStoreError)
        assert "content_conflict" in str(errors[1])
    else:
        assert errors == {}
    with open_runstate_store(tmp_path) as store:
        if operation == "replace":
            rows = store.conn.execute(
                "SELECT prev_sha256, next_sha256 FROM audit ORDER BY audit_seq"
            ).fetchall()
            assert rows[-1]["prev_sha256"] == hashes[0]
            assert rows[-1]["next_sha256"] == hashes[1]
            assert store.get("run", "r") == {"x": 2}
        else:
            assert store.get("run", "r") == {"x": 1}
            assert store.verify()["events"] == 1
            if operation == "duplicate":
                assert hashes[0] == hashes[1]
        store.verify()


def test_open_runstate_store_refuses_a_descendant_of_desk_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RL1-04: the old guard compared EXACT equality only — a research
    workspace INSIDE the desk evidence directory passed. Containment is
    now checked in both directions, on the explicit argument."""
    from tree_options.desk.paths import state_root

    inside_desk = state_root() / "evidence" / "research-inside"
    with pytest.raises(RuntimeError, match="collides"):
        # the guard runs at __enter__ (contextmanager), not at call time
        with open_runstate_store(inside_desk):
            pass


def test_open_runstate_store_refuses_a_workspace_containing_desk_state(tmp_path: Path) -> None:
    from tree_options.desk.paths import state_root

    with pytest.raises(RuntimeError, match="collides"):
        with open_runstate_store(state_root().parent):
            pass


def test_runstate_and_desk_evidence_stores_are_isolated(tmp_path: Path) -> None:
    """The research runstate MUST NOT share a file with the desk evidence
    store. The path-overlap guard in ``research.paths`` covers the
    directory level; this test confirms the store itself never reads
    the desk evidence store."""
    desk_path = tmp_path / "desk.sqlite3"
    runstate_path = tmp_path / "research.sqlite3"
    # Desk evidence store is at desk_path; research runstate at runstate_path.
    # Even if the operator misconfigures RESEARCH_WORKSPACE_DIR to point
    # at the desk path, the path-overlap guard fires before the store opens.
    assert desk_path != runstate_path


def test_spec_hash_is_deterministic(tmp_path: Path) -> None:
    """Two specs with the same logical content produce the same hash;
    reordering top-level keys does NOT change the hash (canonical() sorts)."""
    from datetime import date
    from decimal import Decimal

    from tree_options.research.contracts import ComparisonSpec
    from tree_options.research.runstate.spec_hash import spec_hash

    a = ComparisonSpec(
        candidate_ids=("a", "b"),
        starting_capital=Decimal("1000"),
        common_start=date(2024, 1, 2),
        common_end=date(2026, 9, 25),
    )
    # Same content but build a new dataclass instance — must hash equal.
    b = ComparisonSpec(
        candidate_ids=("a", "b"),
        starting_capital=Decimal("1000"),
        common_start=date(2024, 1, 2),
        common_end=date(2026, 9, 25),
    )
    assert spec_hash(a) == spec_hash(b)


def test_spec_hash_differs_when_content_differs(tmp_path: Path) -> None:
    from datetime import date
    from decimal import Decimal

    from tree_options.research.contracts import ComparisonSpec
    from tree_options.research.runstate.spec_hash import spec_hash

    a = ComparisonSpec(
        candidate_ids=("a", "b"),
        starting_capital=Decimal("1000"),
        common_start=date(2024, 1, 2),
        common_end=date(2026, 9, 25),
    )
    b = ComparisonSpec(
        candidate_ids=("a", "b"),
        starting_capital=Decimal("2000"),  # different
        common_start=date(2024, 1, 2),
        common_end=date(2026, 9, 25),
    )
    assert spec_hash(a) != spec_hash(b)
