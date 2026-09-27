"""Forecast engine (RL-3) — receipt shape, refusals with retained
evidence, matched-cohort guards, and the no-leakage oracle.

Oracles are hand-derived or re-derived from the LEDGER (the auditable
source): the wire-vs-ledger test recomputes skill and the DM statistic
from ledger rows alone and demands the wire agree — exactly what any
downstream reader must be able to do.
"""
from __future__ import annotations

import json
import math
from datetime import date, timedelta
from pathlib import Path

import pytest

from tree_options.desk.stats import dm_test
from tree_options.evaluation.diagnostics import block_bootstrap_ci
from tree_options.research.forecast.contracts import (
    BOOTSTRAP_BLOCK,
    DM_LAG_SENSITIVITY,
    N_BOOTSTRAP,
    ORIGIN_FLOOR,
    PAIRED_FLOOR,
    ForecastSourceId,
    ForecastSpec,
)
from tree_options.research.forecast.engine import (
    DEFAULT_MODELS,
    ForecastOutcome,
    evaluate_forecast,
)
from tree_options.research.forecast.harness import month_origin_grid
from tree_options.research.forecast.refusal_codes import (
    FORECAST_HORIZON_NOT_ENABLED,
    FORECAST_INSUFFICIENT_HISTORY,
    FORECAST_INSUFFICIENT_ORIGINS,
    REASON_BOOTSTRAP_DEGENERATE,
)
from tree_options.research.forecast.sources import (
    SOURCE_REGISTRY,
    ForecastSeries,
    load_synthetic,
)

REPO = Path(__file__).resolve().parents[2]


def _spec(horizon: int = 5,
          start: date = date(2019, 6, 3)) -> ForecastSpec:
    return ForecastSpec(source=ForecastSourceId.SYNTHETIC, horizon=horizon,
                        evaluation_start=start)


def _evaluate(spec: ForecastSpec, series: ForecastSeries,
              models=None, **kw):
    return evaluate_forecast(spec, series=series, calendar_sha256="c" * 64,
                             engine_sha256="e" * 64, models=models, **kw)


def _hand_series(n: int = 420) -> ForecastSeries:
    """A hand-built series on plain daily dates (the engine only needs
    a strictly increasing grid); deterministic pseudo-levels."""
    sessions = []
    d = date(2020, 1, 1)
    while len(sessions) < n:
        sessions.append(d)
        d += timedelta(days=1)
    closes = tuple(100.0 + ((i * 37) % 23) * 0.5 + 0.01 * i
                   for i in range(n))
    descriptor = SOURCE_REGISTRY[ForecastSourceId.SYNTHETIC]
    return ForecastSeries(
        source_id=ForecastSourceId.SYNTHETIC,
        sessions=tuple(sessions), closes=closes,
        series_sha256="f" * 64, basis=descriptor.basis,
        grid_basis=descriptor.grid_basis, provenance={"kind": "hand"},
        excluded_rows={}, n_source_rows=n)


def _stub(quantiles_log: tuple[float, ...]):
    def model(closes, *, h, taus):
        _ = closes, h
        return quantiles_log
    return model


def _loss(row) -> float:
    return 2.0 * (sum(row["losses_by_tau"]) / len(row["losses_by_tau"]))


class TestReceiptShape:
    def test_synthetic_end_to_end(self) -> None:
        series = load_synthetic()
        assert isinstance(series, ForecastSeries)
        out = _evaluate(_spec(), series)
        assert isinstance(out, ForecastOutcome)
        wire = out.to_wire()
        assert wire["schema"] == "research-forecast-result/1"
        assert wire["calibration_status"] == "not_claimed"
        assert wire["evaluation_status"] == "receipt_published"
        assert wire["origins"]["floor_met"] is True
        assert wire["origins"]["total"] == wire["origins"]["evaluated"] \
            + wire["origins"]["excluded"]
        names = [m["model"] for m in wire["models"]]
        assert names == [m[0] for m in DEFAULT_MODELS]
        baseline = next(m for m in wire["models"] if m["is_baseline"])
        assert baseline["model"] == "rw_full"
        assert baseline["metrics"]["skill_vs_baseline"] is None
        for m in wire["models"]:
            if m["is_baseline"]:
                continue
            skill = m["metrics"]["skill_vs_baseline"]
            assert skill["baseline"] == "rw_full"
            assert "dm" in skill or "dm_unavailable_reason" in skill
            assert m["ledger"], "every model carries its ledger"
        study = wire["study"]
        assert study["schema"] == "research-forecast-study/1"
        assert study["access_mode"] == "exploratory"
        assert study["benchmark"] == "rw_full"
        assert study["primary_score"] == "grid_quantile_score"
        assert study["inference"]["dm_lag_units"] == "origin_index"
        assert wire["forward"]["beyond_data"] is True
        assert wire["forward"]["target_session"] is None
        assert [f["model"] for f in wire["forward"]["fan"]] == names
        # the wire is canonical-JSON serialisable (hashed payloads only)
        json.dumps(wire, allow_nan=False)

    def test_deterministic_under_identical_inputs(self) -> None:
        series = load_synthetic()
        assert isinstance(series, ForecastSeries)
        a = _evaluate(_spec(), series).to_wire()
        b = _evaluate(_spec(), series).to_wire()
        assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)

    def test_wire_matches_the_ledger(self) -> None:
        # Re-derive skill and the DM statistic from LEDGER rows alone
        # (the auditable source) and demand the wire agree.
        series = load_synthetic()
        assert isinstance(series, ForecastSeries)
        out = _evaluate(_spec(), series)
        assert isinstance(out, ForecastOutcome)
        baseline_rows = {
            r["origin_date"]: _loss(r)
            for m in out.to_wire()["models"] if m["is_baseline"]
            for r in m["ledger"] if r["status"] == "evaluated"}
        model = next(m for m in out.to_wire()["models"]
                     if m["model"] == "ar1_direct")
        model_rows = {r["origin_date"]: _loss(r)
                      for r in model["ledger"] if r["status"] == "evaluated"}
        matched = sorted(set(baseline_rows) & set(model_rows))
        skill = model["metrics"]["skill_vs_baseline"]
        assert skill["paired_n"] == len(matched)
        assert skill["loss_paired"] == pytest.approx(
            sum(model_rows[d] for d in matched) / len(matched))
        assert skill["bench_paired"] == pytest.approx(
            sum(baseline_rows[d] for d in matched) / len(matched))
        d = [baseline_rows[k] - model_rows[k] for k in matched]
        dm = dm_test(d, lag=1)
        assert dm is not None
        assert skill["dm"]["stat"] == pytest.approx(dm.stat)
        assert skill["dm"]["p_one_sided"] == pytest.approx(dm.p_one_sided)

    def test_dm_sensitivity_re_runs_each_lag(self) -> None:
        # Each sensitivity entry is a REAL dm_test at that lag over the
        # matched-cohort differential (checkpoint B, surviving mutation
        # 4): recompute every lag from the ledger and demand agreement —
        # copying the lag-1 result into all three slots must fail here.
        series = load_synthetic()
        assert isinstance(series, ForecastSeries)
        out = _evaluate(_spec(), series)
        assert isinstance(out, ForecastOutcome)
        wire = out.to_wire()
        baseline_rows = {
            r["origin_date"]: _loss(r)
            for m in wire["models"] if m["is_baseline"]
            for r in m["ledger"] if r["status"] == "evaluated"}
        model = next(m for m in wire["models"]
                     if m["model"] == "ar1_direct")
        model_rows = {r["origin_date"]: _loss(r)
                      for r in model["ledger"] if r["status"] == "evaluated"}
        matched = sorted(set(baseline_rows) & set(model_rows))
        d = [baseline_rows[k] - model_rows[k] for k in matched]
        skill = model["metrics"]["skill_vs_baseline"]
        assert skill["dm"] is not None
        sens = skill["dm"]["sensitivity"]
        assert set(sens) == {str(lag) for lag in DM_LAG_SENSITIVITY}
        for lag in DM_LAG_SENSITIVITY:
            alt = dm_test(d, lag=lag)
            entry = sens[str(lag)]
            if alt is None:
                assert entry is None, lag
            else:
                assert entry is not None, lag
                assert entry["stat"] == pytest.approx(alt.stat), lag
                assert entry["p_one_sided"] == pytest.approx(
                    alt.p_one_sided), lag

    def test_coverage_bootstrap_declares_block_and_matches_helper(
            self) -> None:
        # The receipt DECLARES the block it used and its bounds ARE the
        # shared helper's at that block and seed (the engine-level
        # companion of the metrics block oracle).
        series = load_synthetic()
        assert isinstance(series, ForecastSeries)
        wire = _evaluate(_spec(), series).to_wire()
        baseline = next(m for m in wire["models"] if m["is_baseline"])
        cov = baseline["metrics"]["coverage_90"]
        rows = [r for r in baseline["ledger"] if r["status"] == "evaluated"]
        indicators = [
            1.0 if r["quantiles"][0] <= r["actual"] <= r["quantiles"][-1]
            else 0.0 for r in rows]
        assert len(indicators) == cov["n"]
        assert cov["bootstrap_block"] == BOOTSTRAP_BLOCK

        def mean_stat(sample: list[float]) -> float | None:
            return sum(sample) / len(sample) if sample else None

        want = block_bootstrap_ci(
            indicators, statistic=mean_stat,
            block_size=cov["bootstrap_block"], iterations=N_BOOTSTRAP,
            seed=cov["bootstrap_seed"], confidence=0.95)
        if want is None:
            assert cov["bootstrap_low"] is None
            assert cov["bootstrap_high"] is None
            assert cov["bootstrap_reason"] == REASON_BOOTSTRAP_DEGENERATE
        else:
            assert cov["bootstrap_low"] == pytest.approx(want.lower)
            assert cov["bootstrap_high"] == pytest.approx(want.upper)

    def test_forward_fan_failure_is_explicit_not_silent(self) -> None:
        # Latest-fit failure (the model fails ONLY on the full sample):
        # the historical receipt still publishes, and the forward block
        # carries an explicit unavailable entry — never a silent
        # omission (checkpoint B, P2-5).
        series = _hand_series(420)
        n = len(series.closes)

        def ok(closes, *, h, taus):
            _ = h
            return tuple(math.log(95.0 + 2.5 * k) for k in range(len(taus)))

        def latest_only_failure(closes, *, h, taus):
            _ = h
            if len(closes) == n:
                return None
            return tuple(math.log(95.0 + 2.5 * k) for k in range(len(taus)))

        out = _evaluate(
            ForecastSpec(source=ForecastSourceId.SYNTHETIC, horizon=5,
                         evaluation_start=date(2020, 2, 1)),
            series, models=[("b", ok, True),
                            ("m", latest_only_failure, False)],
            min_history=20)
        assert isinstance(out, ForecastOutcome)
        fan = out.to_wire()["forward"]["fan"]
        by_model = {f["model"]: f for f in fan}
        assert by_model["b"]["status"] == "ok"
        assert "quantiles" in by_model["b"]
        assert by_model["m"]["status"] == "unavailable"
        assert "quantiles" not in by_model["m"]


class TestRefusals:
    def test_horizon_not_enabled_rechecked_in_engine(self) -> None:
        # The routes enforce this pre-write, but a spec written straight
        # into the store must meet the same wall.
        series = load_synthetic()
        assert isinstance(series, ForecastSeries)
        out = _evaluate(_spec(horizon=63), series)
        assert not isinstance(out, ForecastOutcome)
        assert out.code == FORECAST_HORIZON_NOT_ENABLED
        # the synthetic lane offers ONLY h=5 (63 is listed-but-disabled
        # on the index lane, not here)
        assert out.payload["enabled"] == [5]
        assert out.payload["listed"] == [5]

    def test_insufficient_history(self) -> None:
        out = _evaluate(_spec(), _hand_series(n=30))
        assert not isinstance(out, ForecastOutcome)
        assert out.code == FORECAST_INSUFFICIENT_HISTORY

    def test_exactly_at_the_floor_publishes(self) -> None:
        # The floor is >= 12, not > 12: a window with EXACTLY 12
        # evaluated origins publishes a receipt (floor_met true). This
        # is the boundary a `<=` mutation of the floor comparison would
        # silently turn into a refusal. The window is DERIVED: start at
        # the 12th-from-last origin of the wide grid.
        series = load_synthetic()
        assert isinstance(series, ForecastSeries)
        wide = month_origin_grid(
            series.sessions, first_eval=date(2019, 1, 1),
            last_eval=None, horizon=5, min_history=260)
        start = series.sessions[wide.origins[-ORIGIN_FLOOR]]
        out = _evaluate(_spec(start=start), series)
        assert isinstance(out, ForecastOutcome), out
        wire = out.to_wire()
        assert wire["origins"]["evaluated"] == ORIGIN_FLOOR
        assert wire["origins"]["floor_met"] is True
        assert wire["evaluation_status"] == "receipt_published"

    def test_paired_n_is_the_intersection_not_the_union(self) -> None:
        # Baseline fails exactly one origin; the model runs all: the
        # skill cohort is the INTERSECTION (n-1), never the union (n)
        # — comparing unmatched origins manufactures skill. (700 daily
        # dates: enough month starts that n-1 still clears the floor.)
        series = _hand_series(700)
        grid = month_origin_grid(
            series.sessions, first_eval=date(2020, 2, 1),
            last_eval=None, horizon=5, min_history=20)
        origins = grid.origins
        cut = origins[0] + 1    # baseline fails exactly the FIRST origin

        def baseline(closes, *, h, taus):
            if len(closes) <= cut:
                return None
            return tuple(math.log(90.0 + 5.0 * k)
                         for k in range(len(taus)))

        def model(closes, *, h, taus):
            # tighter bands than the baseline: strictly better pinball,
            # with per-origin variation so the DM differential has
            # variance
            return tuple(math.log(97.0 + 1.5 * k)
                         for k in range(len(taus)))

        out = _evaluate(
            ForecastSpec(source=ForecastSourceId.SYNTHETIC, horizon=5,
                         evaluation_start=date(2020, 2, 1)),
            series, models=[("b", baseline, True), ("m", model, False)],
            min_history=20)
        assert isinstance(out, ForecastOutcome)
        wire = out.to_wire()
        b = next(m for m in wire["models"] if m["model"] == "b")
        m = next(m for m in wire["models"] if m["model"] == "m")
        assert b["n_evaluated"] == len(origins) - 1
        assert m["n_evaluated"] == len(origins)
        skill = m["metrics"]["skill_vs_baseline"]
        assert skill["paired_n"] == len(origins) - 1   # intersection
        # the skill NUMBER was emitted over the matched cohort (its
        # sign depends on the stubs; the cohort is the oracle)
        assert skill["pinball_skill"] is not None
        assert "dm" in skill or "dm_unavailable_reason" in skill

    def test_nonbaseline_below_floor_refuses_while_baseline_passes(
            self) -> None:
        # The floor is per MODEL (checkpoint B, surviving mutation 5):
        # a non-baseline that evaluates only 11 of ~23 origins must
        # REFUSE the whole receipt even though the baseline clears the
        # floor — an insufficient-data model may not ride a publishable
        # baseline, and restricting the floor check to the first run
        # (the baseline) must fail here.
        series = _hand_series(700)
        grid = month_origin_grid(
            series.sessions, first_eval=date(2020, 2, 1),
            last_eval=None, horizon=5, min_history=20)
        origins = grid.origins
        assert len(origins) >= 20
        cut = origins[11]    # model fails origins[11:] -> 11 evaluated

        def baseline(closes, *, h, taus):
            _ = h
            return tuple(math.log(95.0 + 2.5 * k) for k in range(len(taus)))

        def weak(closes, *, h, taus):
            _ = h
            if len(closes) > cut:
                return None
            return tuple(math.log(95.0 + 2.5 * k) for k in range(len(taus)))

        out = _evaluate(
            ForecastSpec(source=ForecastSourceId.SYNTHETIC, horizon=5,
                         evaluation_start=date(2020, 2, 1)),
            series, models=[("b", baseline, True), ("m", weak, False)],
            min_history=20)
        assert not isinstance(out, ForecastOutcome)
        assert out.code == FORECAST_INSUFFICIENT_ORIGINS
        assert "m evaluated 11 origins" in out.message
        by = {m["model"]: m for m in out.payload["models"]}
        assert by["m"]["tally"]["evaluated"] == 11
        assert by["m"]["tally"]["floor_met"] is False
        assert by["b"]["tally"]["evaluated"] == len(origins)
        assert by["b"]["tally"]["floor_met"] is True
        # the refusal retains BOTH ledgers
        assert by["m"]["ledger"] and by["b"]["ledger"]

    def test_floor_refusal_retains_ledgers(self) -> None:
        series = load_synthetic()
        assert isinstance(series, ForecastSeries)
        # A window whose month starts cannot reach the floor.
        out = _evaluate(
            _spec(start=date(2020, 8, 1)), series)
        assert not isinstance(out, ForecastOutcome)
        assert out.code == FORECAST_INSUFFICIENT_ORIGINS
        assert out.payload["origins"]["floor_met"] is False
        # the refusal IS the receipt of the attempt: ledgers retained
        for m in out.payload["models"]:
            assert m["ledger"]
            assert m["tally"]["total"] == m["tally"]["evaluated"] \
                + m["tally"]["excluded"]
        assert out.payload["origins"]["total"] < ORIGIN_FLOOR \
            or out.payload["grid_reasons"]


class TestPairedCohort:
    def test_small_intersection_gives_no_skill_number(self) -> None:
        # 700 daily dates -> ~23 month-start origins after warm-up.
        series = _hand_series(700)
        grid = month_origin_grid(
            series.sessions, first_eval=date(2020, 2, 1),
            last_eval=None, horizon=5, min_history=20)
        origins = grid.origins
        assert len(origins) >= 20
        # baseline fails the first 8 origins; model fails the last 8:
        # each clears the floor (>= 12 evaluated) but the matched set
        # is small (7) — no skill number may be emitted.
        cut_b = origins[7] + 1     # len(closes) <= this -> baseline None
        cut_m = origins[-8] + 1    # len(closes) >= this -> model None

        def baseline(closes, *, h, taus):
            if len(closes) <= cut_b:
                return None
            return tuple(math.log(90.0 + 5.0 * k)
                         for k in range(len(taus)))

        def model(closes, *, h, taus):
            if len(closes) >= cut_m:
                return None
            return tuple(math.log(90.0 + 5.0 * k)
                         for k in range(len(taus)))

        out = _evaluate(
            ForecastSpec(source=ForecastSourceId.SYNTHETIC, horizon=5,
                         evaluation_start=date(2020, 2, 1)),
            series, models=[("b", baseline, True), ("m", model, False)],
            min_history=20)
        assert isinstance(out, ForecastOutcome)
        wire = out.to_wire()
        m_rec = next(m for m in wire["models"] if m["model"] == "m")
        skill = m_rec["metrics"]["skill_vs_baseline"]
        assert skill["pinball_skill"] is None
        assert skill["reason"] == "paired_cohort_insufficient"
        assert skill["dm"] is None
        assert skill["dm_unavailable_reason"] == \
            "paired_cohort_insufficient"
        assert PAIRED_FLOOR == 8

    def test_exactly_one_baseline_required(self) -> None:
        stub = _stub(tuple(math.log(90.0 + 5.0 * k) for k in range(5)))
        with pytest.raises(ValueError, match="exactly one baseline"):
            _evaluate(_spec(), _hand_series(420),
                      models=[("a", stub, True), ("b", stub, True)],
                      min_history=20)


class TestBootstrapDegeneracy:
    def test_all_hits_marks_bootstrap_degenerate(self) -> None:
        # A baseline whose q05..q95 band covers every actual: coverage
        # is all hits — the bootstrap would print [1, 1] for every
        # resample, which is not uncertainty evidence.
        series = _hand_series(420)
        wide = _stub(tuple(math.log(40.0 + 30.0 * k) for k in range(5)))
        narrow = _stub(tuple(math.log(99.0 + 1.0 * k) for k in range(5)))
        out = _evaluate(
            ForecastSpec(source=ForecastSourceId.SYNTHETIC, horizon=5,
                         evaluation_start=date(2020, 2, 1)),
            series, models=[("wide", wide, True),
                            ("narrow", narrow, False)],
            min_history=20)
        assert isinstance(out, ForecastOutcome)
        wire = out.to_wire()
        b = next(m for m in wire["models"] if m["model"] == "wide")
        cov = b["metrics"]["coverage_90"]
        assert cov["hits"] == cov["n"]
        assert cov["bootstrap_low"] is None
        assert cov["bootstrap_high"] is None
        assert cov["bootstrap_reason"] == "degenerate coverage " \
                                          "(all hits or none)"
        # Wilson is retained and honest at hits == n
        assert cov["wilson_high"] == pytest.approx(1.0, abs=1e-12)
        assert cov["bootstrap_seed"] > 0


class TestNoLeakage:
    def test_target_mutation_changes_loss_not_the_forecast(self) -> None:
        # Mutating a TARGET close: that origin's recorded loss must
        # change (the score responds to the realized outcome) while its
        # forecast quantiles stay frozen (they were fit at origin time).
        series = _hand_series(420)
        stub = _stub(tuple(math.log(95.0 + 2.5 * k) for k in range(5)))
        spec = ForecastSpec(source=ForecastSourceId.SYNTHETIC, horizon=5,
                            evaluation_start=date(2020, 2, 1))
        out_a = _evaluate(spec, series, models=[("s", stub, True)],
                          min_history=20)
        assert isinstance(out_a, ForecastOutcome)
        grid = month_origin_grid(series.sessions,
                                 first_eval=spec.evaluation_start,
                                 last_eval=None, horizon=5, min_history=20)
        t0 = grid.origins[1]
        mutated = ForecastSeries(**{**series.__dict__,
                                    "closes": list(series.closes)})
        object.__setattr__(
            mutated, "closes",
            (*series.closes[: t0 + 5],
             series.closes[t0 + 5] * 1.5,
             *series.closes[t0 + 6:]))
        out_b = _evaluate(spec, mutated, models=[("s", stub, True)],
                          min_history=20)
        assert isinstance(out_b, ForecastOutcome)
        row_a = next(r for m in out_a.models for r in m.ledger
                     if r.origin_date == series.sessions[t0])
        row_b = next(r for m in out_b.models for r in m.ledger
                     if r.origin_date == series.sessions[t0])
        assert row_b.actual == pytest.approx(row_a.actual * 1.5)
        assert row_b.quantiles == row_a.quantiles      # frozen at fit
        assert row_b.losses_by_tau != row_a.losses_by_tau
        # an EARLIER origin (target before the mutation) is untouched
        early_a = next(r for m in out_a.models for r in m.ledger
                       if r.origin_date == series.sessions[grid.origins[0]])
        early_b = next(r for m in out_b.models for r in m.ledger
                       if r.origin_date == series.sessions[grid.origins[0]])
        assert early_b == early_a


def test_all_model_ledger_identities_close() -> None:
    series = load_synthetic()
    assert isinstance(series, ForecastSeries)
    out = _evaluate(_spec(), series)
    assert isinstance(out, ForecastOutcome)
    wire = out.to_wire()
    total = wire["origins"]["total"]
    for m in wire["models"]:
        by = {"evaluated": 0, "failed": 0, "excluded": 0}
        for r in m["ledger"]:
            by[r["status"]] += 1
        assert total == by["evaluated"] + by["failed"] + by["excluded"]
        assert by["evaluated"] == m["n_evaluated"]
        assert sum(m["failure_reasons"].values(), 0) == by["failed"]
        # ledger rows are origin-ordered and pairwise distinct
        dates = [r["origin_date"] for r in m["ledger"]]
        assert dates == sorted(dates)
        assert len(set(dates)) == len(dates)
