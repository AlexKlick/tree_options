"""Forecast routes (RL-3) — metadata, the execution-bound idempotent
spool, pre-write refusals, receipt freshness, and the shared result
route serving forecast runs unmodified.

(The insufficient-history 400 is exercised at the ENGINE level — the
route's live synthetic fixture always clears it; faking a shorter
series here would test the fixture, not the route.)
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tree_options.research.forecast.contracts import forecast_run_id
from tree_options.research.forecast.spec_io import forecast_from_dict
from tree_options.research.runstate.store import open_runstate_store
from tree_options.research.runstate.worker import (
    RUN_FORMAT_VERSION,
    engine_identity_sha,
)
from tree_options.trex_web.research_view import attach

GOOD_BODY = {
    "source": "synthetic-forecast-v1",
    "horizon": 5,
    "evaluation_start": "2019-06-03",
}


@pytest.fixture()
def env(tmp_path: Path):
    ws = tmp_path / "rs"
    ws.mkdir()
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    app = FastAPI()
    worker = attach(app, workspace=ws, candidate_scopes_root=artifacts, start_worker=False)
    assert worker is not None
    return TestClient(app), ws, worker


class TestMetadata:
    def test_registry_shape_and_disabled_horizon_copy(self, env) -> None:
        client, _ws, _worker = env
        r = client.get("/api/research/forecast")
        assert r.status_code == 200
        body = r.json()
        assert body["schema"] == "research-forecast-metadata/1"
        assert body["quantile_grid"] == [0.05, 0.25, 0.5, 0.75, 0.95]
        assert body["origin_floor"] == 12
        low = body["interval_semantics"].lower()
        assert "calibrated" not in low
        assert "not a claim of calibration" in low
        by_source = {s["source"]: s for s in body["sources"]}
        syn = by_source["synthetic-forecast-v1"]
        assert [h["horizon"] for h in syn["horizons"]] == [5]
        assert all(h["enabled"] for h in syn["horizons"])
        vix = by_source["index:VIX"]
        enabled = {h["horizon"]: h for h in vix["horizons"] if h["enabled"]}
        disabled = {h["horizon"]: h for h in vix["horizons"] if not h["enabled"]}
        assert set(enabled) == {5, 20}
        assert set(disabled) == {63, 126}
        for h in disabled.values():
            assert h["status"] == "illustrative_only"
            assert h["status_copy"] == ("not enabled - no evaluation receipt (illustrative only)")


class TestSpool:
    def test_202_then_200_idempotent_same_run_id(self, env) -> None:
        client, ws, _worker = env
        r1 = client.post("/api/research/forecast", json=GOOD_BODY)
        assert r1.status_code == 202
        r2 = client.post("/api/research/forecast", json=GOOD_BODY)
        assert r2.status_code == 200
        assert r1.json()["run_id"] == r2.json()["run_id"]
        assert r1.json()["kind"] == "forecast"
        with open_runstate_store(ws) as store:
            runs = [
                p
                for p, _at in store.all_at("run")
                if isinstance(p, dict) and p.get("kind") == "forecast"
            ]
        assert len(runs) == 1
        run = runs[0]
        assert run["engine_sha256_at_submission"] == engine_identity_sha()
        assert run["series_sha256_at_submission"]
        # P1-1: BOTH calendar identities are captured at submission —
        # they gate the worker's drift check and enter the run id.
        assert run["calendar_sha256_at_submission"]
        assert run["session_authority_sha256_at_submission"]
        # Single-read binding (checkpoint B-prime, N1): the stored
        # bindings hash BACK to the run id the response carried — the
        # id and the record were built from one capture of each value.
        assert r1.json()["run_id"] == forecast_run_id(
            forecast_from_dict(GOOD_BODY),
            series_sha256=run["series_sha256_at_submission"],
            calendar_sha256=run["calendar_sha256_at_submission"],
            session_authority_sha256=run["session_authority_sha256_at_submission"],
            engine_sha256=run["engine_sha256_at_submission"],
        )

    def test_pre_write_400s_persist_nothing(self, env) -> None:
        client, ws, _worker = env
        cases = [
            ("invalid_json", None),
            ("invalid_forecast_spec", {**GOOD_BODY, "models": ["x"]}),
            ("research.forecast.horizon_not_enabled", {**GOOD_BODY, "horizon": 63}),
        ]
        for expected_error, body in cases:
            if body is None:
                r = client.post(
                    "/api/research/forecast",
                    content="not json",
                    headers={"content-type": "application/json"},
                )
            else:
                r = client.post("/api/research/forecast", json=body)
            assert r.status_code == 400, (expected_error, r.text)
            assert r.json()["detail"]["error"] == expected_error
        with open_runstate_store(ws) as store:
            assert not store.all("run")
            assert not store.all("spec")

    def test_horizon_63_is_rejected_not_just_hidden(self, env) -> None:
        # The mutation-killer: a listed-but-disabled horizon never
        # creates a run record, even though the metadata LISTS 63.
        client, ws, _worker = env
        r = client.post("/api/research/forecast", json={**GOOD_BODY, "horizon": 63})
        assert r.status_code == 400
        assert r.json()["detail"]["error"] == "research.forecast.horizon_not_enabled"
        # the synthetic lane lists ONLY h=5 (63 is listed-but-disabled
        # on the index lane, not here)
        assert r.json()["detail"]["listed"] == [5]
        with open_runstate_store(ws) as store:
            assert not store.all("run")


class TestResultSurface:
    def test_shared_result_route_serves_forecast_runs(self, env) -> None:
        client, _ws, worker = env
        r = client.post("/api/research/forecast", json=GOOD_BODY)
        run_id = r.json()["run_id"]
        assert worker.step() is True
        got = client.get(f"/api/research/runs/{run_id}/result")
        assert got.status_code == 200
        body = got.json()
        assert body["status"] == "completed"
        assert body["result_sha256"]
        assert body["engine_sha256"] == engine_identity_sha()
        assert body["input_snapshot_sha256"]
        assert body["calendar_sha256"]
        wire = body["result"]
        assert wire["schema"] == "research-forecast-result/1"
        assert wire["refusal"] is None

    def test_refusal_result_visible_through_shared_route(self, env) -> None:
        client, _ws, worker = env
        r = client.post(
            "/api/research/forecast", json={**GOOD_BODY, "evaluation_start": "2020-10-01"}
        )
        run_id = r.json()["run_id"]
        assert worker.step() is True
        body = client.get(f"/api/research/runs/{run_id}/result").json()
        assert body["status"] == "completed"
        assert body["result"]["refusal"] == "research.forecast.insufficient_origins"
        assert body["result"]["models"]


class TestFreshness:
    def _complete(self, client, worker) -> str:
        r = client.post("/api/research/forecast", json=GOOD_BODY)
        run_id = r.json()["run_id"]
        assert worker.step() is True
        return run_id

    def test_fresh_receipt_points_at_the_run(self, env) -> None:
        client, _ws, worker = env
        run_id = self._complete(client, worker)
        meta = client.get("/api/research/forecast").json()
        syn = next(s for s in meta["sources"] if s["source"] == "synthetic-forecast-v1")
        h5 = next(h for h in syn["horizons"] if h["horizon"] == 5)
        assert h5["latest_receipt_run_id"] == run_id
        assert h5["fresh"] is True
        # freshness covers EVERY execution binding (P2-3): series,
        # engine, and both calendars each publish receipt/current pairs.
        assert h5["receipt_series_sha256"] == h5["current_series_sha256"]
        assert h5["receipt_engine_sha256"] == h5["current_engine_sha256"]
        assert h5["receipt_calendar_sha256"] == h5["current_calendar_sha256"]
        assert h5["receipt_session_authority_sha256"] == h5["current_session_authority_sha256"]

    def test_stale_engine_marks_receipt_never_fresh(self, env) -> None:
        # An engine correction with UNCHANGED data must still mark the
        # old receipt stale (checkpoint B, P2-3): comparing only the
        # series sha would promote an obsolete-engine receipt as fresh.
        client, ws, worker = env
        self._complete(client, worker)  # a fresh receipt exists
        with open_runstate_store(ws) as store:
            runs = [
                p
                for p, _at in store.all_at("run")
                if isinstance(p, dict) and p.get("kind") == "forecast"
            ]
            result = store.get("result", runs[0]["run_id"])
            assert result is not None
            # Simulate an engine correction: a LATER record whose
            # receipt was computed under an older engine, series
            # unchanged (records are immutable — new id, same shape as
            # the series-stale test).
            stale_id = "6" * 64
            store.put(
                "spec",
                {"source": "synthetic-forecast-v1", "horizon": 5, "evaluation_start": "2019-06-03"},
                key=stale_id,
                at=datetime.now(),
            )
            store.put(
                "run",
                {
                    "run_id": stale_id,
                    "spec_hash": stale_id,
                    "kind": "forecast",
                    "status": "completed",
                    "format_version": RUN_FORMAT_VERSION,
                },
                key=stale_id,
                at=datetime.now(),
            )
            store.put(
                "result",
                {**result, "run_id": stale_id, "engine_sha256": "e" * 64},
                key=stale_id,
                at=datetime.now(),
            )
        meta = client.get("/api/research/forecast").json()
        syn = next(s for s in meta["sources"] if s["source"] == "synthetic-forecast-v1")
        h5 = next(h for h in syn["horizons"] if h["horizon"] == 5)
        assert h5["latest_receipt_run_id"] == stale_id
        assert h5["receipt_series_sha256"] == h5["current_series_sha256"]
        assert h5["receipt_engine_sha256"] == "e" * 64
        assert h5["current_engine_sha256"] != "e" * 64
        assert h5["fresh"] is False

    def test_stale_receipt_is_marked_never_promoted(self, env) -> None:
        client, ws, worker = env
        run_id = self._complete(client, worker)
        # Simulate a vendor-style revision: rewrite the stored receipt's
        # series sha so it no longer matches the live fixture — the
        # metadata must mark it stale rather than present it as current.
        with open_runstate_store(ws) as store:
            result = store.get("result", run_id)
            assert result is not None
            wire = dict(result["wire"])
            wire["series"] = {**wire["series"], "series_sha256": "a" * 64}
            # a new id because the payload changed: put under a new key
            stale_id = "5" * 64
            store.put(
                "spec",
                {"source": "synthetic-forecast-v1", "horizon": 5, "evaluation_start": "2019-06-03"},
                key=stale_id,
                at=datetime.now(),
            )
            store.put(
                "run",
                {
                    "run_id": stale_id,
                    "spec_hash": stale_id,
                    "kind": "forecast",
                    "status": "completed",
                    "format_version": RUN_FORMAT_VERSION,
                },
                key=stale_id,
                at=datetime.now(),
            )
            store.put(
                "result",
                {**result, "run_id": stale_id, "wire": wire},
                key=stale_id,
                at=datetime.now(),
            )
        meta = client.get("/api/research/forecast").json()
        syn = next(s for s in meta["sources"] if s["source"] == "synthetic-forecast-v1")
        h5 = next(h for h in syn["horizons"] if h["horizon"] == 5)
        assert h5["latest_receipt_run_id"] == stale_id  # most recent
        assert h5["fresh"] is False
        assert h5["receipt_series_sha256"] == "a" * 64
        assert h5["current_series_sha256"] != "a" * 64

    def test_refused_attempt_surfaces_without_promoting(self, env) -> None:
        client, ws, worker = env
        self._complete(client, worker)  # a good receipt exists
        r = client.post(
            "/api/research/forecast", json={**GOOD_BODY, "evaluation_start": "2020-10-01"}
        )
        refusal_id = r.json()["run_id"]
        assert worker.step() is True
        meta = client.get("/api/research/forecast").json()
        syn = next(s for s in meta["sources"] if s["source"] == "synthetic-forecast-v1")
        h5 = next(h for h in syn["horizons"] if h["horizon"] == 5)
        assert h5["last_attempt_refused"]["code"] == "research.forecast.insufficient_origins"
        assert h5["last_attempt_refused"]["n_evaluated"] is not None
        assert h5["latest_receipt_run_id"] not in (None, refusal_id)
        _ = ws


def test_index_lane_rejects_disabled_horizons_pre_write(env) -> None:
    client, ws, _worker = env
    r = client.post(
        "/api/research/forecast",
        json={"source": "index:VIX", "horizon": 126, "evaluation_start": "2019-06-03"},
    )
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "research.forecast.horizon_not_enabled"
    with open_runstate_store(ws) as store:
        assert not store.all("run")
