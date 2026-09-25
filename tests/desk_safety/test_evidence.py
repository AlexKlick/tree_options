"""Transactional custody tests, using independent SQLite connections."""
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from tree_options.desk.evidence import EvidenceError, EvidenceStore


def test_write_requires_transaction_and_is_idempotent(world):
    with EvidenceStore(world.db) as store:
        with pytest.raises(EvidenceError, match='transaction_required'):
            store.put('queue', 'one', {'value': '1.00'}, world.now)
        with store.atomic():
            assert store.put('queue', 'one', {'value': '1.00'}, world.now)
            assert not store.put('queue', 'one', {'value': '1.00'}, world.now)
        assert store.verify()['events'] == 1


def test_exception_rolls_back_data_and_audit(world):
    with EvidenceStore(world.db) as store:
        with pytest.raises(RuntimeError):
            with store.atomic():
                store.put('queue', 'one', {'value': 1}, world.now)
                raise RuntimeError('simulated crash before commit')
        assert store.all('queue') == []
        assert store.verify()['events'] == 0


def test_conflict_does_not_overwrite_evidence(world):
    with EvidenceStore(world.db) as store:
        with store.atomic():
            store.put('queue', 'one', {'value': 1}, world.now)
        with pytest.raises(EvidenceError, match='content_conflict'):
            with store.atomic():
                store.put('queue', 'one', {'value': 2}, world.now)
        assert store.get('queue', 'one') == {'value': 1}


def test_two_writers_claim_once(world):
    with EvidenceStore(world.db):
        pass
    def writer(_):
        with EvidenceStore(world.db) as store, store.atomic():
            return store.put('queue', 'one', {'value': 1}, world.now)
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(writer, range(2))) == 1
    with EvidenceStore(world.db, readonly=True) as store:
        assert store.verify()['events'] == 1


@pytest.mark.parametrize('sql', [
    "UPDATE objects SET payload='{\"value\":2}'",
    "DELETE FROM objects",
    "UPDATE audit SET payload_sha256='wrong'",
])
def test_tampering_is_detected(world, sql):
    with EvidenceStore(world.db) as store, store.atomic():
        store.put('queue', 'one', {'value': 1}, world.now)
    with sqlite3.connect(world.db) as conn:
        conn.execute(sql)
    with EvidenceStore(world.db, readonly=True) as store:
        with pytest.raises(EvidenceError):
            store.verify()


def test_transient_store_does_not_create_paths(world):
    with EvidenceStore(world.db, transient=True) as store, store.atomic():
        store.put('queue', 'one', {'value': 1}, world.now)
    assert not world.db.parent.exists()


def test_transient_store_clones_but_does_not_mutate_existing(world):
    with EvidenceStore(world.db) as store, store.atomic():
        store.put('queue', 'one', {'value': 1}, world.now)
    with EvidenceStore(world.db, transient=True) as store, store.atomic():
        store.put('queue', 'two', {'value': 2}, world.now)
    with EvidenceStore(world.db, readonly=True) as store:
        assert len(store.all('queue')) == 1


def test_backup_roundtrip(world):
    with EvidenceStore(world.db) as store:
        with store.atomic():
            store.put('queue', 'one', {'value': 1}, world.now)
        backup = world.root / 'backup.sqlite3'
        store.backup(backup)
    with EvidenceStore(backup, readonly=True) as restored:
        assert restored.verify()['events'] == 1
        assert restored.get('queue', 'one') == {'value': 1}


def test_corrupted_backup_is_never_published(world):
    with EvidenceStore(world.db) as store:
        with store.atomic():
            store.put('queue', 'one', {'value': 1}, world.now)
        store.conn.execute("DELETE FROM objects")
        destination = world.root / 'backup.sqlite3'
        with pytest.raises(EvidenceError):
            store.backup(destination)
        assert not destination.exists()
        assert not list(world.root.glob('.desk-backup-*'))
