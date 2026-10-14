"""Publication fault probes and DB-free HTTP readers."""

import json
from datetime import timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tree_options.desk import read_snapshot, shadows
from tree_options.desk.evidence import EvidenceError, EvidenceStore
from tree_options.trex_web import desk_view


def publish(world):
    world.queue_file()
    shadows.update_shadows(
        session=world.session,
        now=world.now,
        cal=world.cal,
        database=world.db,
        queue_dir=world.queues,
        store_root=world.store,
    )
    out = world.root / "published" / "evidence.json"
    read_snapshot.publish(
        database=world.db, out=out, now=world.now, cal=world.cal, source_release="test-head"
    )
    return out


def test_shared_audit_and_db_free_read(world, monkeypatch):
    out = publish(world)
    doc = read_snapshot.read(out, now=world.now)
    assert doc["payload"]["health"]["audit"] == doc["payload"]["scorecards"]["audit"]
    monkeypatch.setenv("TREX_DESK_READ_SNAPSHOT", str(out))
    monkeypatch.setattr(desk_view, "now_et", lambda: world.now)
    app = FastAPI()
    desk_view.attach(app, database=world.db)

    def forbidden(*args, **kwargs):
        raise AssertionError("web reader opened database")

    monkeypatch.setattr(EvidenceStore, "__init__", forbidden)
    with TestClient(app) as client:
        result = client.get("/api/desk/scorecards")
        assert result.status_code == 200
        assert result.json()["read_snapshot"]["source_release"] == "test-head"
        assert result.json()["execution_enabled"] is False
        assert (
            client.get("/api/desk/health").json()["evidence_status"]
            == doc["payload"]["health"]["evidence_status"]
        )


@pytest.mark.parametrize("seconds,reason", [(121, "snapshot_stale"), (-1, "snapshot_from_future")])
def test_expired_or_future_snapshot_is_not_served(world, seconds, reason):
    out = publish(world)
    with pytest.raises(EvidenceError, match=reason):
        read_snapshot.read(out, now=world.now + timedelta(seconds=seconds))


def test_tamper_and_unknown_version_are_rejected(world):
    out = publish(world)
    doc = json.loads(out.read_text())
    doc["payload"]["scorecards"]["families"] = [{"invented": 1}]
    out.write_text(json.dumps(doc))
    with pytest.raises(EvidenceError, match="snapshot_digest_mismatch"):
        read_snapshot.read(out, now=world.now)
    doc["schema"] = "desk-read-snapshot/99"
    out.write_text(json.dumps(doc))
    with pytest.raises(EvidenceError, match="snapshot_schema_invalid"):
        read_snapshot.read(out, now=world.now)


def test_interrupted_publication_keeps_previous_snapshot(world, monkeypatch):
    out = publish(world)
    previous = out.read_bytes()

    def disk_full(*args):
        raise OSError("disk full")

    monkeypatch.setattr(read_snapshot.os, "replace", disk_full)
    with pytest.raises(OSError, match="disk full"):
        read_snapshot.publish(
            database=world.db, out=out, now=world.now, cal=world.cal, source_release="new-head"
        )
    assert out.read_bytes() == previous
    assert list(out.parent.iterdir()) == [out]


def test_missing_snapshot_does_not_fall_back_to_healthy_db(world, monkeypatch):
    out = publish(world)
    out.unlink()
    monkeypatch.setenv("TREX_DESK_READ_SNAPSHOT", str(out))
    app = FastAPI()
    desk_view.attach(app, database=world.db)
    with TestClient(app) as client:
        for route in ("health", "scorecards"):
            result = client.get("/api/desk/" + route)
            assert result.status_code == 503
            assert result.json()["evidence_blockers"] == ["snapshot_missing"]
            assert "families" not in result.json()


def test_corrupt_source_does_not_replace_snapshot(world):
    out = publish(world)
    previous = out.read_bytes()
    other = world.root / "corrupt.sqlite3"
    other.write_bytes(b"not sqlite")
    import sqlite3

    with pytest.raises(sqlite3.DatabaseError):
        read_snapshot.publish(
            database=other, out=out, now=world.now, cal=world.cal, source_release="new-head"
        )
    assert out.read_bytes() == previous


def test_legitimate_pending_evaluation_is_published_as_unready(world):
    out = publish(world)
    after_close = world.now.replace(hour=17)
    doc = read_snapshot.publish(
        database=world.db, out=out, now=after_close, cal=world.cal, source_release="test-head"
    )
    assert doc["payload"]["health"]["evidence_status"] == "awaiting_publication"
    assert "evaluation_cycle_pending" in doc["payload"]["health"]["pending"]
    assert (
        read_snapshot.read(out, now=after_close)["payload"]["health"]["execution_enabled"] is False
    )
