"""Publication, evidence separation, legacy owners and failure boundaries."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tree_options.research.experiments import build as builder
from tree_options.research.experiments import io
from tree_options.research.experiments.contracts import Index, digest
from tree_options.research.runstate.store import open_runstate_store
from tree_options.trex_web.experiments_view import attach

NOW = datetime(2026, 10, 14, 20, tzinfo=UTC)


def config(**sources):
    return {
        "schema": "trex-experiment-sources/1",
        "sources": {k: str(v) for k, v in sources.items()},
    }


def index(**sources):
    return builder.build(config(**sources), NOW, "test-release")


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))
    return path


def standings(root, games=3):
    return write(
        root / "standings.json",
        {
            "schema": "desk-challenge-standings/1",
            "policies": [
                {
                    "policy": "control",
                    "games": games,
                    "entered": 2 * games,
                    "sessions_distinct": 2,
                    "closed_pnl_sum": "5.00",
                    "model_calls": 0,
                    "session_pnl": {
                        f"game{i}:2025-01-{day}": 1 for i in range(games) for day in ("02", "03")
                    },
                }
            ],
        },
    )


def test_replays_do_not_inflate_sessions_and_no_portfolio_return(tmp_path):
    standings(tmp_path, 3)
    first = index(challenge=tmp_path).experiments[0]
    standings(tmp_path, 12)
    second = index(challenge=tmp_path).experiments[0]
    assert first.metrics[1].value == second.metrics[1].value == 2
    assert first.metrics[0].value == 3 and second.metrics[0].value == 12
    assert second.account_return is None and second.execution_authorized is False
    assert second.metrics[3].unit == "USD modeled"


def test_one_bad_source_does_not_blank_others(tmp_path):
    standings(tmp_path)
    doc = index(challenge=tmp_path, qsl=tmp_path / "missing.sqlite3")
    assert len(doc.experiments) == 1
    source = next(s for s in doc.sources if s.id == "qsl")
    assert source.status == "unavailable" and source.count is None
    assert not (tmp_path / "missing.sqlite3").exists()


@pytest.mark.parametrize(
    "field,value", [("sessions_distinct", 999), ("games", True), ("closed_pnl_sum", "NaN")]
)
def test_malformed_statistics_are_unavailable(tmp_path, field, value):
    path = standings(tmp_path)
    raw = json.loads(path.read_text())
    raw["policies"][0][field] = value
    write(path, raw)
    doc = index(challenge=tmp_path)
    assert doc.sources[0].status == "unavailable"
    assert not doc.experiments


def test_closed_failed_campaign_is_retained_and_not_mutated(tmp_path):
    path = write(tmp_path / "failed" / "sealed-round.json", {"family_verdict": "FAIL"})
    before = path.read_bytes()
    row = index(campaigns=tmp_path).experiments[0]
    assert row.status == "closed_record: FAIL"
    assert row.strategy_qualified is False
    assert path.read_bytes() == before
    assert "new study" in row.next_decision


def test_readonly_research_uses_verified_snapshot_and_does_not_create_missing_db(tmp_path):
    missing = tmp_path / "missing"
    assert index(research=missing).sources[5].status == "unavailable"
    assert not missing.exists()
    with open_runstate_store(tmp_path) as store:
        store.put(
            "run", {"run_id": "abc", "status": "completed", "kind": "comparison"}, key="abc", at=NOW
        )
    path = tmp_path / "runstate.sqlite3"
    before = path.read_bytes()
    row = index(research=tmp_path).experiments[0]
    assert row.status == "completed" and row.metrics[0].value is None
    assert path.read_bytes() == before
    import sqlite3

    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE objects SET payload_json=?",
            (
                json.dumps(
                    {
                        "run_id": "abc",
                        "status": "completed",
                        "kind": "comparison",
                        "hypothesis": "Altered after publication",
                    }
                ),
            ),
        )
    assert index(research=tmp_path).sources[5].status == "unavailable"


def longrun_doc():
    return {
        "schema": "desk-longrun-digest/1",
        "run_id": "run1",
        "at": NOW.isoformat(),
        "promotion": {"promoted": False},
        "boards": {"total": 10, "scored": 9, "sessions": {"count": 2}},
        "complete": False,
        "headline": "Incomplete; no promotion.",
    }


def test_lineage_requires_both_exact_pinned_hashes(tmp_path, monkeypatch):
    original = write(tmp_path / "runs" / "run1" / "digest.json", longrun_doc())
    correction = write(tmp_path / "corrected.json", {**longrun_doc(), "corrected": True})
    monkeypatch.setattr(builder, "ORIGINAL", io.load(original)[1])
    monkeypatch.setattr(builder, "CORRECTED", io.load(correction)[1])
    doc = index(longrun=original.parent.parent, longrun_correction=correction)
    old, new = doc.experiments
    assert old.superseded_by == new.id and new.supersedes == old.id
    assert new.status == "posthoc_correction_incomplete"
    write(correction, {**longrun_doc(), "corrected": "changed"})
    doc = index(longrun=original.parent.parent, longrun_correction=correction)
    assert all(
        r.supersedes is None and r.superseded_by is None and r.blockers for r in doc.experiments
    )


@pytest.mark.parametrize("seconds,reason", [(121, "index_stale"), (-1, "index_from_future")])
def test_publication_expiry_and_future(tmp_path, seconds, reason):
    path = tmp_path / "snapshot.json"
    io.publish(index(), path)
    with pytest.raises(io.Unavailable, match=reason):
        io.read(path, NOW + timedelta(seconds=seconds))


def test_hash_schema_and_authority_validation(tmp_path):
    path = tmp_path / "snapshot.json"
    io.publish(index(), path)
    raw = json.loads(path.read_text())
    raw["payload"]["live_money"] = True
    write(path, raw)
    with pytest.raises(io.Unavailable, match="digest"):
        io.read(path, NOW)
    raw["sha256"] = digest(raw["payload"])
    write(path, raw)
    with pytest.raises(io.Unavailable, match="index_invalid"):
        io.read(path, NOW)
    with pytest.raises(ValueError):
        Index.model_validate({**index().model_dump(), "schema": "unknown/2"})


def test_interrupted_publication_preserves_previous_file(tmp_path, monkeypatch):
    path = tmp_path / "snapshot.json"
    io.publish(index(), path)
    before = path.read_bytes()

    def fail(*args):
        raise OSError("disk full")

    monkeypatch.setattr(io.os, "replace", fail)
    with pytest.raises(OSError, match="disk full"):
        io.publish(index(), path)
    assert path.read_bytes() == before
    assert list(tmp_path.iterdir()) == [path]


def test_http_is_db_free_detail_404_and_specific_unavailable(tmp_path, monkeypatch):
    standings(tmp_path / "challenge")
    doc = builder.build(config(challenge=tmp_path / "challenge"), datetime.now(UTC), "test")
    path = tmp_path / "snapshot.json"
    io.publish(doc, path)
    app = FastAPI()
    attach(app, snapshot=path)

    def forbidden(*args, **kwargs):
        raise AssertionError("HTTP tried to open a source database")

    monkeypatch.setattr(builder.sqlite3, "connect", forbidden)
    with TestClient(app) as client:
        response = client.get("/api/experiments")
        assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
        assert (
            client.get("/api/experiments/challenge-control").json()["experiment"]["id"]
            == "challenge-control"
        )
        assert client.get("/api/experiments/unknown").status_code == 404
        assert client.post("/api/experiments/challenge-control").status_code == 405
        path.unlink()
        response = client.get("/api/experiments")
        assert response.status_code == 503 and response.json()["reason"] == "index_missing"
        assert "experiments" not in response.json()


def test_forward_clock_denominators_and_budget_gap_remain_separate(tmp_path):
    from datetime import date

    from tree_options.desk.forward_minutes import BARS_SCHEMA, clock_ms

    day = date(2026, 10, 13)
    # 10:01 covers the eight-clock window but not the exact three-clock minute.
    write(
        tmp_path / "bars" / "one.json",
        {
            "schema": BARS_SCHEMA,
            "session": day.isoformat(),
            "captured_at": NOW.isoformat(),
            "contracts": {
                "one": {"status": "ok", "bars": [{"t": clock_ms(day, "10:01")}]},
                "two": {"status": "budget", "bars": []},
            },
        },
    )
    row = index(forward=tmp_path).experiments[0]
    metrics = {m.label: m.value for m in row.metrics}
    assert metrics["Three-clock fully covered contracts"] == 0
    assert metrics["Eight-clock covered points"] == 1
    assert metrics["Eight-clock scheduled points"] == 16
    assert metrics["Budget exhaustion"] == 1
    assert any("60-session" in text for text in row.limitations)


def test_allocations_are_preserved_as_virtual_not_broker_cash(tmp_path):
    path = write(
        tmp_path / "allocations.json",
        {
            "sleeves": [
                {
                    "sleeve_id": str(i),
                    "capital_usd": "50000" if i < 19 else "5000",
                    "account_alias": None,
                }
                for i in range(29)
            ]
        },
    )
    before = path.read_bytes()
    row = index(allocations=path).experiments[0]
    assert [m.value for m in row.metrics] == [29, "1000000", 29]
    assert row.metrics[1].unit == "USD virtual"
    assert path.read_bytes() == before
    assert all(
        p.new_requests == p.new_compute_seconds == p.new_storage_bytes == 0 for p in index().pilot
    )
