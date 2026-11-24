"""Transactional custody tests, using independent SQLite connections."""

import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

from tree_options.desk.contracts import ContractError, digest
from tree_options.desk.evidence import EvidenceError, EvidenceStore


def test_write_requires_transaction_and_is_idempotent(world):
    with EvidenceStore(world.db) as store:
        with pytest.raises(EvidenceError, match="transaction_required"):
            store.put("queue", "one", {"value": "1.00"}, world.now)
        with store.atomic():
            assert store.put("queue", "one", {"value": "1.00"}, world.now)
            assert not store.put("queue", "one", {"value": "1.00"}, world.now)
        assert store.verify()["events"] == 1


def test_exception_rolls_back_data_and_audit(world):
    with EvidenceStore(world.db) as store:
        with pytest.raises(RuntimeError):
            with store.atomic():
                store.put("queue", "one", {"value": 1}, world.now)
                raise RuntimeError("simulated crash before commit")
        assert store.all("queue") == []
        assert store.verify()["events"] == 0


def test_conflict_does_not_overwrite_evidence(world):
    with EvidenceStore(world.db) as store:
        with store.atomic():
            store.put("queue", "one", {"value": 1}, world.now)
        with pytest.raises(EvidenceError, match="content_conflict"):
            with store.atomic():
                store.put("queue", "one", {"value": 2}, world.now)
        assert store.get("queue", "one") == {"value": 1}


def test_two_writers_claim_once(world):
    with EvidenceStore(world.db):
        pass

    def writer(_):
        with EvidenceStore(world.db) as store, store.atomic():
            return store.put("queue", "one", {"value": 1}, world.now)

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(writer, range(2))) == 1
    with EvidenceStore(world.db, readonly=True) as store:
        assert store.verify()["events"] == 1


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE objects SET payload='{\"value\":2}'",
        "DELETE FROM objects",
        "UPDATE audit SET payload_sha256='wrong'",
    ],
)
def test_tampering_is_detected(world, sql):
    with EvidenceStore(world.db) as store, store.atomic():
        store.put("queue", "one", {"value": 1}, world.now)
    with sqlite3.connect(world.db) as conn:
        conn.execute(sql)
    with EvidenceStore(world.db, readonly=True) as store:
        with pytest.raises(EvidenceError):
            store.verify()


def test_transient_store_does_not_create_paths(world):
    with EvidenceStore(world.db, transient=True) as store, store.atomic():
        store.put("queue", "one", {"value": 1}, world.now)
    assert not world.db.parent.exists()


def test_transient_store_clones_but_does_not_mutate_existing(world):
    with EvidenceStore(world.db) as store, store.atomic():
        store.put("queue", "one", {"value": 1}, world.now)
    with EvidenceStore(world.db, transient=True) as store, store.atomic():
        store.put("queue", "two", {"value": 2}, world.now)
    with EvidenceStore(world.db, readonly=True) as store:
        assert len(store.all("queue")) == 1


def test_backup_roundtrip(world):
    with EvidenceStore(world.db) as store:
        with store.atomic():
            store.put("queue", "one", {"value": 1}, world.now)
        backup = world.root / "backup.sqlite3"
        store.backup(backup)
    with EvidenceStore(backup, readonly=True) as restored:
        assert restored.verify()["events"] == 1
        assert restored.get("queue", "one") == {"value": 1}


def test_corrupted_backup_is_never_published(world):
    with EvidenceStore(world.db) as store:
        with store.atomic():
            store.put("queue", "one", {"value": 1}, world.now)
        store.conn.execute("DELETE FROM objects")
        destination = world.root / "backup.sqlite3"
        with pytest.raises(EvidenceError):
            store.backup(destination)
        assert not destination.exists()
        assert not list(world.root.glob(".desk-backup-*"))


# ---- WT8-B: the audit chain's shape, verify's checks, the backup's bounds ----------
# Every stored row is read through the test's OWN connection, never through the
# code under test; every expectation is a constant or a digest of a stored row.

_EVENT_COLUMNS = ("seq", "occurred_at", "kind", "object_key", "payload_sha256", "previous_sha256")


def _audit_rows(db):
    conn = sqlite3.connect(db)
    try:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute("SELECT * FROM audit ORDER BY seq")]
    finally:
        conn.close()


def _event_of(row):
    """The six columns an audit row's event_sha256 is the digest of."""
    return {column: row[column] for column in _EVENT_COLUMNS}


def _instants(world):
    return tuple(world.now.replace(hour=hour) for hour in (10, 11, 12))


def _one_event(world, payload=None):
    with EvidenceStore(world.db) as store, store.atomic():
        assert store.put("queue", "one", payload or {"value": 1}, world.now)
    with EvidenceStore(world.db, readonly=True) as ro:
        assert ro.verify()["ok"] is True  # the premise: the untouched store verifies


def _verify_refuses(world, code):
    with EvidenceStore(world.db, readonly=True) as ro:
        with pytest.raises(EvidenceError, match=code):
            ro.verify()


def test_each_event_links_to_the_one_before(world):
    t1, t2, t3 = _instants(world)
    with EvidenceStore(world.db) as store:
        for key, value, at in (("one", 1, t1), ("two", 2, t2), ("three", 3, t3)):
            with store.atomic():
                assert store.put("queue", key, {"value": value}, at)
    rows = _audit_rows(world.db)
    assert [row["seq"] for row in rows] == [1, 2, 3]
    assert rows[0]["previous_sha256"] == "0" * 64
    assert rows[1]["previous_sha256"] == rows[0]["event_sha256"]
    assert rows[2]["previous_sha256"] == rows[1]["event_sha256"]
    assert [row["occurred_at"] for row in rows] == [t1.isoformat(), t2.isoformat(), t3.isoformat()]
    for row in rows:
        assert digest(_event_of(row)) == row["event_sha256"]
    with EvidenceStore(world.db, readonly=True) as ro:
        assert ro.verify() == {
            "ok": True,
            "events": 3,
            "objects": 3,
            "head_sha256": rows[2]["event_sha256"],
        }


def test_a_read_only_store_refuses_to_put(world):
    with EvidenceStore(world.db) as store, store.atomic():
        store.put("queue", "one", {"value": 1}, world.now)
    with EvidenceStore(world.db, readonly=True) as ro:
        ro.conn.execute("BEGIN")
        assert ro.conn.in_transaction  # the premise: only `readonly` can refuse now
        with pytest.raises(EvidenceError, match="transaction_required"):
            ro.put("queue", "two", {"value": 2}, world.now)
        ro.conn.rollback()
        assert ro.get("queue", "two") is None
        assert ro.verify()["events"] == 1


def test_a_naive_instant_is_refused_before_anything_is_written(world):
    naive = world.now.replace(tzinfo=None)
    assert naive.tzinfo is None
    with EvidenceStore(world.db) as store:
        with pytest.raises(ContractError, match="aware_timestamp"):
            with store.atomic():
                store.put("queue", "one", {"value": 1}, naive)
        assert store.conn.in_transaction is False
        assert store.get("queue", "one") is None
        assert store.verify() == {"ok": True, "events": 0, "objects": 0, "head_sha256": "0" * 64}


def test_atomic_refuses_a_transaction_already_open(world):
    with EvidenceStore(world.db) as store:
        with store.atomic():
            with pytest.raises(EvidenceError, match="transaction_state"):
                with store.atomic():
                    pass
            assert store.put("queue", "one", {"value": 1}, world.now)
        assert store.get("queue", "one") == {"value": 1}
        assert store.verify()["events"] == 1


def test_verify_leaves_no_transaction_open(world):
    with EvidenceStore(world.db) as store:
        with store.atomic():
            store.put("queue", "one", {"value": 1}, world.now)
        assert store.verify()["ok"] is True
        assert store.conn.in_transaction is False
        # atomic() raises transaction_state if verify() left a transaction open
        with store.atomic():
            assert store.put("queue", "two", {"value": 2}, world.now)
        assert store.verify()["events"] == 2
        with sqlite3.connect(world.db) as conn:
            conn.execute(
                "UPDATE objects SET payload_sha256 = ? WHERE kind = ? AND object_key = ?",
                ("f" * 64, "queue", "one"),
            )
        with pytest.raises(EvidenceError, match="object_integrity"):
            store.verify()
        assert store.conn.in_transaction is False


def test_verify_names_a_broken_sequence(world):
    _one_event(world)
    [row] = _audit_rows(world.db)
    assert digest(_event_of(row)) == row["event_sha256"]
    forged = {**_event_of(row), "seq": 41}
    with sqlite3.connect(world.db) as conn:
        conn.execute("UPDATE audit SET seq = ?, event_sha256 = ?", (41, digest(forged)))
    _verify_refuses(world, "audit_chain")


def test_verify_names_a_broken_link(world):
    with EvidenceStore(world.db) as store:
        for key, value in (("one", 1), ("two", 2)):
            with store.atomic():
                assert store.put("queue", key, {"value": value}, world.now)
    with EvidenceStore(world.db, readonly=True) as ro:
        assert ro.verify()["ok"] is True
    second = _audit_rows(world.db)[1]
    assert second["seq"] == 2
    forged = {**_event_of(second), "previous_sha256": "1" * 64}
    with sqlite3.connect(world.db) as conn:
        conn.execute(
            "UPDATE audit SET previous_sha256 = ?, event_sha256 = ? WHERE seq = ?",
            ("1" * 64, digest(forged), 2),
        )
    _verify_refuses(world, "audit_chain")


def test_verify_names_a_forged_digest(world):
    _one_event(world)  # ONE event: the forged one is the last, no later link to break
    with sqlite3.connect(world.db) as conn:
        conn.execute("UPDATE audit SET event_sha256 = ?", ("f" * 64,))
    _verify_refuses(world, "audit_chain")


def test_verify_names_an_object_whose_recorded_digest_was_changed(world):
    _one_event(world)
    with sqlite3.connect(world.db) as conn:
        conn.execute("UPDATE objects SET payload_sha256 = ?", ("f" * 64,))
    _verify_refuses(world, "object_integrity")


def test_verify_names_an_object_rewritten_with_its_digest(world):
    _one_event(world, {"value": 1})
    with sqlite3.connect(world.db) as conn:
        conn.execute(
            "UPDATE objects SET payload = ?, payload_sha256 = ?",
            ('{"value": 2}', digest({"value": 2})),
        )
    _verify_refuses(world, "object_integrity")


def test_all_at_pairs_each_object_with_its_own_commit(world):
    t1, t2, t3 = _instants(world)
    with EvidenceStore(world.db) as store:
        for kind, key, payload, at in (
            ("alpha", "shared", {"n": 1}, t1),
            ("alpha", "solo", {"n": 2}, t2),
            ("beta", "shared", {"n": 3}, t3),
        ):
            with store.atomic():
                assert store.put(kind, key, payload, at)
        assert store.all_at("alpha") == [({"n": 1}, t1.isoformat()), ({"n": 2}, t2.isoformat())]
        assert store.all_at("beta") == [({"n": 3}, t3.isoformat())]
        assert store.all_at("gamma") == []


def test_backup_refuses_an_existing_destination_and_its_own_file(world):
    with EvidenceStore(world.db) as store:
        with store.atomic():
            store.put("queue", "one", {"value": 1}, world.now)
        dest = world.root / "backup.sqlite3"
        store.backup(dest)
        before = dest.read_bytes()
        with pytest.raises(EvidenceError, match="backup_exists"):
            store.backup(dest)
        with pytest.raises(EvidenceError, match="backup_exists"):
            store.backup(world.db)
        assert dest.read_bytes() == before
        assert not list(world.root.rglob(".desk-backup-*"))
        assert store.verify()["events"] == 1


_CHILD_BACKUP = """
import sys
from datetime import datetime
from pathlib import Path
from tree_options.desk.evidence import EvidenceError, EvidenceStore

db, dest, at = Path(sys.argv[1]), Path(sys.argv[2]), datetime.fromisoformat(sys.argv[3])
with EvidenceStore(db) as store:
    with store.atomic():
        store.put("queue", "one", {"value": 1}, at)
        try:
            store.backup(dest)
        except EvidenceError as exc:
            print("refused: " + str(exc))
        else:
            print("backup RETURNED inside the transaction")
"""


def test_backup_refuses_inside_a_transaction(world):
    dest = world.root / "backup.sqlite3"
    try:
        child = subprocess.run(
            [sys.executable, "-c", _CHILD_BACKUP, str(world.db), str(dest), world.now.isoformat()],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except subprocess.TimeoutExpired:
        pytest.fail("backup() did not return within 30 s inside an open transaction")
    assert child.returncode == 0, child.stderr[-400:]
    assert child.stdout.strip() == "refused: backup_in_transaction"
    assert not dest.exists()
    assert not list(world.root.glob(".desk-backup-*"))
    with EvidenceStore(world.db, readonly=True) as store:
        assert store.verify()["events"] == 1


def test_the_published_backup_is_a_single_rollback_journal_file(world):
    with EvidenceStore(world.db) as store:
        with store.atomic():
            store.put("queue", "one", {"value": 1}, world.now)
        dest = world.root / "backup.sqlite3"
        store.backup(dest)
    conn = sqlite3.connect(dest)
    try:
        journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        conn.close()
    assert journal_mode == "delete"
    assert not (world.root / "backup.sqlite3-wal").exists()
    assert not (world.root / "backup.sqlite3-shm").exists()
    with EvidenceStore(dest, readonly=True) as restored:
        assert restored.verify()["events"] == 1
