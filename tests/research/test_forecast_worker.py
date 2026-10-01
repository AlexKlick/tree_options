"""Forecast worker lifecycle (RL-3) — the third dispatch kind with
execution-bound identity: submission-time shas are recomputed at
compute time, the run id is RE-DERIVED from the stored spec plus the
current bindings, and drift or inconsistency publishes a typed refusal
with BOTH values — never revised bytes, new code, or a desynchronized
record under the submission's run id.
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime
from pathlib import Path

import pytest

from tree_options.desk.contracts import canonical
from tree_options.research.comparison.calendar import calendar_sha256
from tree_options.research.forecast.contracts import (
    ForecastSourceId,
    ForecastSpec,
    forecast_run_id,
)
from tree_options.research.forecast.refusal_codes import (
    FORECAST_CALENDAR_CHANGED,
    FORECAST_ENGINE_CHANGED,
    FORECAST_IDENTITY_MISMATCH,
    FORECAST_INSUFFICIENT_ORIGINS,
    FORECAST_SOURCE_DRIFT,
)
from tree_options.research.forecast.sources import (
    ForecastSeries,
    load_synthetic,
    session_authority_sha256,
)
from tree_options.research.runstate.store import open_runstate_store
from tree_options.research.runstate.worker import (
    RUN_FORMAT_VERSION,
    ResearchWorker,
    engine_identity_sha,
)


def _rid(spec: ForecastSpec, series_sha: str, engine_sha: str, **overrides: str) -> str:
    """A run id over the REAL current calendar bindings (the worker
    recomputes the id at compute time, so seeded ids must be ones the
    current world actually reproduces — except where a test overrides a
    binding to force drift)."""
    kw = {
        "calendar_sha256": calendar_sha256(),
        "session_authority_sha256": session_authority_sha256(),
        **overrides,
    }
    return forecast_run_id(spec, series_sha256=series_sha, engine_sha256=engine_sha, **kw)


def _worker(ws: Path) -> ResearchWorker:
    return ResearchWorker(workspace=ws, catalog_provider=lambda: [])


def _seed(
    ws: Path,
    spec: ForecastSpec,
    *,
    run_id: str,
    series_sha: str,
    engine_sha: str,
    spec_payload: dict | None = None,
    extra_run: dict | None = None,
) -> None:
    with open_runstate_store(ws) as store:
        store.put(
            "spec",
            spec_payload if spec_payload is not None else spec.to_dict(),
            key=run_id,
            at=datetime.now(),
        )
        run = {
            "run_id": run_id,
            "spec_hash": run_id,
            "kind": "forecast",
            "status": "queued",
            "format_version": RUN_FORMAT_VERSION,
            "series_sha256_at_submission": series_sha,
            "engine_sha256_at_submission": engine_sha,
        }
        if extra_run:
            run.update(extra_run)
        store.put("run", run, key=run_id, at=datetime.now())


def _live_series() -> ForecastSeries:
    out = load_synthetic()
    assert isinstance(out, ForecastSeries)
    return out


def _good_spec() -> ForecastSpec:
    return ForecastSpec(
        source=ForecastSourceId.SYNTHETIC, horizon=5, evaluation_start=date(2019, 6, 3)
    )


class TestLifecycle:
    def test_completed_run_publishes_content_bound_receipt(self, tmp_path: Path) -> None:
        ws = tmp_path / "rs"
        ws.mkdir()
        series = _live_series()
        engine = engine_identity_sha()
        spec = _good_spec()
        run_id = _rid(spec, series.series_sha256, engine)
        # NOTE: the calendar sha inside the id is route-side binding;
        # the worker independently binds the REAL calendar sha into the
        # published receipt. Only the shas recorded on the run record
        # gate execution here.
        _seed(ws, spec, run_id=run_id, series_sha=series.series_sha256, engine_sha=engine)
        assert _worker(ws).step() is True
        with open_runstate_store(ws) as store:
            run = store.get("run", run_id)
            result = store.get("result", run_id)
        assert run is not None and run["status"] == "completed"
        assert result is not None
        assert result["engine_sha256"] == engine_identity_sha()
        assert result["input_snapshot"]["series_sha256"] == series.series_sha256
        assert result["input_snapshot"]["source"] == spec.source.value
        assert result["wire"]["schema"] == "research-forecast-result/1"
        assert result["wire"]["refusal"] is None
        assert run["result_sha256"] == result["result_sha256"]
        # Independent content binding (checkpoint B-prime, sol #7): the
        # receipt's sha is the canonical hash of the wire, recomputed
        # HERE — a fixed placeholder digest would not survive.
        assert result["result_sha256"] == hashlib.sha256(canonical(result["wire"])).hexdigest()

    def test_second_step_is_a_noop(self, tmp_path: Path) -> None:
        ws = tmp_path / "rs"
        ws.mkdir()
        series = _live_series()
        engine = engine_identity_sha()
        spec = _good_spec()
        run_id = _rid(spec, series.series_sha256, engine)
        _seed(ws, spec, run_id=run_id, series_sha=series.series_sha256, engine_sha=engine)
        worker = _worker(ws)
        assert worker.step() is True
        assert worker.step() is False  # nothing left to claim

    def test_snapshot_identity_follows_the_spec(self, tmp_path: Path) -> None:
        # Same series, different window -> different input snapshot.
        ws = tmp_path / "rs"
        ws.mkdir()
        series = _live_series()
        engine = engine_identity_sha()
        specs = (
            _good_spec(),
            ForecastSpec(
                source=ForecastSourceId.SYNTHETIC, horizon=5, evaluation_start=date(2019, 9, 2)
            ),
        )
        shas = []
        for spec in specs:
            run_id = _rid(spec, series.series_sha256, engine)
            _seed(ws, spec, run_id=run_id, series_sha=series.series_sha256, engine_sha=engine)
            assert _worker(ws).step() is True
            with open_runstate_store(ws) as store:
                result = store.get("result", run_id)
            assert result is not None
            shas.append(result["input_snapshot_sha256"])
        assert shas[0] != shas[1]


class TestExecutionRefusals:
    def test_floor_refusal_publishes_with_ledger(self, tmp_path: Path) -> None:
        ws = tmp_path / "rs"
        ws.mkdir()
        series = _live_series()
        engine = engine_identity_sha()
        spec = ForecastSpec(
            source=ForecastSourceId.SYNTHETIC, horizon=5, evaluation_start=date(2020, 10, 1)
        )
        run_id = _rid(spec, series.series_sha256, engine)
        _seed(ws, spec, run_id=run_id, series_sha=series.series_sha256, engine_sha=engine)
        assert _worker(ws).step() is True
        with open_runstate_store(ws) as store:
            run = store.get("run", run_id)
            result = store.get("result", run_id)
        assert run is not None and run["status"] == "completed"
        assert result is not None
        assert result["wire"]["refusal"] == FORECAST_INSUFFICIENT_ORIGINS
        # the refusal is the receipt of the attempt — ledger retained
        assert result["wire"]["models"]
        assert all(m["ledger"] for m in result["wire"]["models"])
        assert result["wire"]["origins"]["floor_met"] is False

    def test_queued_data_drift_refuses_with_both_shas(self, tmp_path: Path) -> None:
        # POST hashed fixture A; the file moved to B while queued: the
        # worker publishes a refusal naming BOTH, never B's results.
        ws = tmp_path / "rs"
        ws.mkdir()
        series = _live_series()
        engine = engine_identity_sha()
        spec = _good_spec()
        run_id = _rid(spec, "a" * 64, engine)
        _seed(ws, spec, run_id=run_id, series_sha="a" * 64, engine_sha=engine)
        assert _worker(ws).step() is True
        with open_runstate_store(ws) as store:
            result = store.get("result", run_id)
        assert result is not None
        wire = result["wire"]
        assert wire["refusal"] == FORECAST_SOURCE_DRIFT
        assert wire["series_sha256_at_submission"] == "a" * 64
        assert wire["series_sha256_at_compute"] == series.series_sha256

    def test_engine_change_refuses_with_both_shas(self, tmp_path: Path) -> None:
        ws = tmp_path / "rs"
        ws.mkdir()
        series = _live_series()
        spec = _good_spec()
        run_id = _rid(spec, series.series_sha256, "Z" * 64)
        _seed(ws, spec, run_id=run_id, series_sha=series.series_sha256, engine_sha="Z" * 64)
        assert _worker(ws).step() is True
        with open_runstate_store(ws) as store:
            result = store.get("result", run_id)
        assert result is not None
        wire = result["wire"]
        assert wire["refusal"] == FORECAST_ENGINE_CHANGED
        assert wire["engine_sha256_at_submission"] == "Z" * 64
        assert wire["engine_sha256_at_compute"] == engine_identity_sha()

    def test_authority_drift_refuses_with_both_values(self, tmp_path: Path) -> None:
        # A closure correction between submission and compute re-grades
        # every target: the worker refuses under the submission's id and
        # publishes BOTH authority shas (checkpoint B, P1-1) — it never
        # re-runs the grid under the old run id.
        ws = tmp_path / "rs"
        ws.mkdir()
        series = _live_series()
        engine = engine_identity_sha()
        spec = _good_spec()
        run_id = _rid(spec, series.series_sha256, engine, session_authority_sha256="b" * 64)
        _seed(
            ws,
            spec,
            run_id=run_id,
            series_sha=series.series_sha256,
            engine_sha=engine,
            extra_run={"session_authority_sha256_at_submission": "b" * 64},
        )
        assert _worker(ws).step() is True
        with open_runstate_store(ws) as store:
            result = store.get("result", run_id)
        assert result is not None
        wire = result["wire"]
        assert wire["refusal"] == FORECAST_CALENDAR_CHANGED
        assert wire["session_authority_sha256_at_submission"] == "b" * 64
        assert wire["session_authority_sha256_at_compute"] == session_authority_sha256()

    def test_completed_receipt_binds_both_calendars(self, tmp_path: Path) -> None:
        # The published envelope and input snapshot carry BOTH calendar
        # identities (P1-1): the receipt names the calendars that
        # shaped it, not just the series.
        ws = tmp_path / "rs"
        ws.mkdir()
        series = _live_series()
        engine = engine_identity_sha()
        spec = _good_spec()
        run_id = _rid(spec, series.series_sha256, engine)
        _seed(ws, spec, run_id=run_id, series_sha=series.series_sha256, engine_sha=engine)
        assert _worker(ws).step() is True
        with open_runstate_store(ws) as store:
            result = store.get("result", run_id)
        assert result is not None
        assert result["session_authority_sha256"] == session_authority_sha256()
        assert result["calendar_sha256"]
        assert result["input_snapshot"]["session_authority_sha256"] == session_authority_sha256()

    def test_malformed_authority_meets_the_typed_wall(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Malformed authority bytes after submission: the identity
        # check hashes BYTES ONLY (never parses), so the stale binding
        # gets the typed calendar_changed refusal — a completed run
        # with a refusal receipt, never a generic failed run
        # (checkpoint B-double-prime, N2 residual).
        import tree_options.research.forecast.sources as sources_mod

        bad = tmp_path / "authority.json"
        bad.write_text("{not json")
        monkeypatch.setattr(sources_mod, "_SESSION_AUTHORITY", bad)
        ws = tmp_path / "rs"
        ws.mkdir()
        series = _live_series()
        engine = engine_identity_sha()
        spec = _good_spec()
        run_id = _rid(spec, series.series_sha256, engine, session_authority_sha256="b" * 64)
        _seed(
            ws,
            spec,
            run_id=run_id,
            series_sha=series.series_sha256,
            engine_sha=engine,
            extra_run={"session_authority_sha256_at_submission": "b" * 64},
        )
        assert _worker(ws).step() is True
        with open_runstate_store(ws) as store:
            run = store.get("run", run_id)
            result = store.get("result", run_id)
        assert run is not None and run["status"] == "completed"
        assert result is not None
        assert result["wire"]["refusal"] == FORECAST_CALENDAR_CHANGED

    def test_authority_swap_after_check_refuses(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The identity pre-check passed on authority A; the loader then
        # intersected with a REPLACED authority B (the loaded grid's
        # provenance carries B's sha): the run must refuse — B's grid
        # is never published under A's run id (checkpoint
        # B-double-prime, NEW P1).
        ws = tmp_path / "rs"
        ws.mkdir()
        series = _live_series()
        engine = engine_identity_sha()
        spec = _good_spec()
        run_id = _rid(spec, series.series_sha256, engine)
        swapped = ForecastSeries(
            source_id=series.source_id,
            sessions=series.sessions,
            closes=series.closes,
            series_sha256=series.series_sha256,
            basis=series.basis,
            grid_basis=series.grid_basis,
            provenance={**series.provenance, "session_authority_sha256": "b" * 64},
            excluded_rows=series.excluded_rows,
            n_source_rows=series.n_source_rows,
        )
        import tree_options.research.runstate.worker as worker_mod

        monkeypatch.setattr(worker_mod, "_load_forecast_series", lambda _spec: swapped)
        _seed(ws, spec, run_id=run_id, series_sha=series.series_sha256, engine_sha=engine)
        assert _worker(ws).step() is True
        with open_runstate_store(ws) as store:
            run = store.get("run", run_id)
            result = store.get("result", run_id)
        assert run is not None and run["status"] == "completed"
        assert result is not None
        wire = result["wire"]
        assert wire["refusal"] == FORECAST_CALENDAR_CHANGED
        assert wire["session_authority_sha256_at_check"] == session_authority_sha256()
        assert wire["session_authority_sha256_that_shaped_the_grid"] == "b" * 64

    def test_combined_csv_and_authority_change_reports_the_calendar(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # CSV AND authority both replaced between check and load: the
        # refusal must still be the calendar one WITH its three
        # authority values (the grid cannot be graded at all when the
        # authority moved — that divergence outranks the data drift).
        ws = tmp_path / "rs"
        ws.mkdir()
        series = _live_series()
        engine = engine_identity_sha()
        spec = _good_spec()
        run_id = _rid(spec, series.series_sha256, engine)
        swapped = ForecastSeries(
            source_id=series.source_id,
            sessions=series.sessions,
            closes=series.closes,
            series_sha256="f" * 64,  # the CSV ALSO moved
            basis=series.basis,
            grid_basis=series.grid_basis,
            provenance={**series.provenance, "session_authority_sha256": "b" * 64},
            excluded_rows=series.excluded_rows,
            n_source_rows=series.n_source_rows,
        )
        import tree_options.research.runstate.worker as worker_mod

        monkeypatch.setattr(worker_mod, "_load_forecast_series", lambda _spec: swapped)
        _seed(ws, spec, run_id=run_id, series_sha=series.series_sha256, engine_sha=engine)
        assert _worker(ws).step() is True
        with open_runstate_store(ws) as store:
            result = store.get("result", run_id)
        assert result is not None
        wire = result["wire"]
        assert wire["refusal"] == FORECAST_CALENDAR_CHANGED
        assert wire["session_authority_sha256_at_check"] == session_authority_sha256()
        assert wire["session_authority_sha256_that_shaped_the_grid"] == "b" * 64

    def test_inconsistent_bindings_refuse_identity_mismatch(self, tmp_path: Path) -> None:
        # The five bindings that pass every specific drift check must
        # still HASH BACK to the run id (checkpoint B-prime, N1): a
        # record spooled with a desynchronized id — e.g. the id bound a
        # calendar the record does not carry — can never publish.
        ws = tmp_path / "rs"
        ws.mkdir()
        series = _live_series()
        engine = engine_identity_sha()
        spec = _good_spec()
        run_id = _rid(
            spec, series.series_sha256, engine, calendar_sha256="c" * 64
        )  # id bound a phantom
        _seed(
            ws,
            spec,
            run_id=run_id,
            series_sha=series.series_sha256,
            engine_sha=engine,
            extra_run={
                "calendar_sha256_at_submission": calendar_sha256(),
                "session_authority_sha256_at_submission": session_authority_sha256(),
            },
        )
        assert _worker(ws).step() is True
        with open_runstate_store(ws) as store:
            run = store.get("run", run_id)
            result = store.get("result", run_id)
        assert run is not None and run["status"] == "completed"
        assert result is not None
        wire = result["wire"]
        assert wire["refusal"] == FORECAST_IDENTITY_MISMATCH
        assert wire["run_id"] == run_id
        assert wire["recomputed_run_id"] == _rid(spec, series.series_sha256, engine)
        assert wire["recomputed_run_id"] != run_id


class TestHonestFailures:
    def test_unparsable_spec_fails_the_run(self, tmp_path: Path) -> None:
        ws = tmp_path / "rs"
        ws.mkdir()
        _seed(
            ws,
            _good_spec(),
            run_id="9" * 64,
            series_sha=_live_series().series_sha256,
            engine_sha=engine_identity_sha(),
            spec_payload={"source": "nope", "horizon": 5, "evaluation_start": "2019-06-03"},
        )
        assert _worker(ws).step() is True
        with open_runstate_store(ws) as store:
            run = store.get("run", "9" * 64)
        assert run is not None
        assert run["status"] == "failed"
        assert "unknown forecast source" in run["error"]

    def test_unknown_kind_fails_loudly(self, tmp_path: Path) -> None:
        # Pre-RL-3 an unknown kind silently computed a comparison.
        ws = tmp_path / "rs"
        ws.mkdir()
        with open_runstate_store(ws) as store:
            store.put("spec", {"anything": True}, key="7" * 64, at=datetime.now())
            store.put(
                "run",
                {
                    "run_id": "7" * 64,
                    "spec_hash": "7" * 64,
                    "kind": "mystery",
                    "status": "queued",
                    "format_version": RUN_FORMAT_VERSION,
                },
                key="7" * 64,
                at=datetime.now(),
            )
        assert _worker(ws).step() is True
        with open_runstate_store(ws) as store:
            run = store.get("run", "7" * 64)
        assert run is not None
        assert run["status"] == "failed"
        assert "unknown run kind" in run["error"]
