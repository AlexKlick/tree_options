"""Rolling-origin harness (RL-3) — grid classification, the ledger's
uncensoring invariants, the session-authority target oracle, and the
future-data boundary. Every oracle is hand-derived from the declared
semantics (month starts, u + h <= t eligibility, the h-th observed row
after the origin row), never from an implementation expression.
"""
from __future__ import annotations

import itertools
import json
import math
from datetime import date, timedelta
from pathlib import Path

import pytest

from tree_options.research.forecast.contracts import QUANTILE_GRID, LedgerRow
from tree_options.research.forecast.harness import (
    ModelRun,
    bind,
    evaluate_model,
    forward_fan,
    month_origin_grid,
    rw_full,
)

REPO = Path(__file__).resolve().parents[2]


def _dates(*specs: tuple[int, int, int]) -> list[date]:
    return [date(y, m, d) for y, m, d in specs]


def _month_block(year: int, month: int, n: int) -> list[date]:
    out = []
    d = date(year, month, 1)
    while len(out) < n:
        out.append(d)
        d += timedelta(days=1)
    return out


class TestMonthOriginGrid:
    def test_hand_grid_with_history_exclusion(self) -> None:
        sessions = (_month_block(2024, 1, 10)
                    + _month_block(2024, 2, 10)
                    + _month_block(2024, 3, 10))
        # h = 5, min_history = 3 ELIGIBLE PAIRS (u + 5 <= t -> t - 4):
        #   t = 0  : -4 -> 0 pairs  -> excluded insufficient_history
        #   t = 10 : 6 pairs, target index 15 <= 29 -> origin
        #   t = 20 : 16 pairs, target index 25 <= 29 -> origin
        grid = month_origin_grid(sessions, first_eval=date(2024, 1, 1),
                                 last_eval=None, horizon=5, min_history=3)
        assert grid.origins == (10, 20)
        assert grid.excluded == ((0, "insufficient_history"),)
        assert grid.total == 3
        assert grid.reasons() == {"insufficient_history": 1}

    def test_target_beyond_data_is_excluded_not_dropped(self) -> None:
        sessions = (_month_block(2024, 1, 10)
                    + _month_block(2024, 2, 10)
                    + _month_block(2024, 3, 3))
        # last index 22: t = 20 needs 25 -> beyond; t = 10 fine.
        grid = month_origin_grid(sessions, first_eval=date(2024, 1, 1),
                                 last_eval=None, horizon=5, min_history=3)
        assert grid.origins == (10,)
        assert dict(grid.excluded) == {
            0: "insufficient_history",
            20: "target_beyond_data",
        }
        assert grid.total == 3

    def test_window_bounds_the_cohort(self) -> None:
        sessions = (_month_block(2024, 1, 10)
                    + _month_block(2024, 2, 10)
                    + _month_block(2024, 3, 10))
        # A month start BEFORE first_eval is not part of the declared
        # evaluation cohort (not counted, not excluded).
        grid = month_origin_grid(sessions, first_eval=date(2024, 2, 1),
                                 last_eval=date(2024, 2, 28), horizon=5,
                                 min_history=3)
        assert grid.origins == (10,)
        assert grid.excluded == ()
        assert grid.total == 1


def _authority_sessions() -> list[date]:
    doc = json.loads(
        (REPO / "data" / "calendar" / "trex"
         / "nyse_sessions_2018_01_02_2028_12_29.json").read_text())
    return sorted(date.fromisoformat(s) for s in doc["sessions"]
                  if date(2024, 12, 1) <= date.fromisoformat(s)
                  <= date(2025, 3, 1))


class TestSessionAuthorityTargets:
    """The Carter-closure oracle: on the repo's closure-corrected
    session authority, origin 2025-01-02 has h=5 target 2025-01-10 and
    h=20 target 2025-02-03 — NOT 2025-01-09 (the vendor file's row on
    the closure date) and not any holiday-shifted date."""

    def test_h5_target_skips_the_closure_row(self) -> None:
        sessions = _authority_sessions()
        closes = tuple(100.0 + i for i in range(len(sessions)))
        stub = bind(_constant_model, h=5, taus=QUANTILE_GRID)
        grid = month_origin_grid(sessions, first_eval=date(2025, 1, 1),
                                 last_eval=None, horizon=5, min_history=1)
        run = evaluate_model(sessions, closes, grid=grid, horizon=5,
                             taus=QUANTILE_GRID, model_name="stub",
                             model=stub)
        by_origin = {r.origin_date: r for r in run.ledger
                     if r.status == "evaluated"}
        assert by_origin[date(2025, 1, 2)].target_date == date(2025, 1, 10)

    def test_h20_target_lands_on_feb_3(self) -> None:
        sessions = _authority_sessions()
        closes = tuple(100.0 + i for i in range(len(sessions)))
        stub = bind(_constant_model, h=20, taus=QUANTILE_GRID)
        grid = month_origin_grid(sessions, first_eval=date(2025, 1, 1),
                                 last_eval=None, horizon=20, min_history=1)
        run = evaluate_model(sessions, closes, grid=grid, horizon=20,
                             taus=QUANTILE_GRID, model_name="stub",
                             model=stub)
        by_origin = {r.origin_date: r for r in run.ledger
                     if r.status == "evaluated"}
        assert by_origin[date(2025, 1, 2)].target_date == date(2025, 2, 3)


def _constant_model(closes: tuple[float, ...], *, h: int,
                    taus: tuple[float, ...]) -> tuple[float, ...]:
    _ = closes, h
    return tuple(math.log(90.0 + 5.0 * k) for k in range(len(taus)))


class TestEvaluateModel:
    def _setup(self, n: int = 40) -> tuple[list[date], list[float]]:
        sessions = _month_block(2024, 1, n // 3) \
            + _month_block(2024, 2, n // 3) + _month_block(2024, 3, n // 3)
        closes = [100.0 + i for i in range(len(sessions))]
        return sessions, closes

    def test_off_by_one_target_is_the_hth_row_after_origin(self) -> None:
        sessions, closes = self._setup()
        stub = bind(_constant_model, h=5, taus=QUANTILE_GRID)
        grid = month_origin_grid(sessions, first_eval=date(2024, 1, 1),
                                 last_eval=None, horizon=5, min_history=1)
        run = evaluate_model(sessions, closes, grid=grid, horizon=5,
                             taus=QUANTILE_GRID, model_name="stub",
                             model=stub)
        for row in run.ledger:
            if row.status != "evaluated":
                continue
            t = sessions.index(row.origin_date)
            assert row.actual == closes[t + 5]      # NOT t + 6
            assert row.target_date == sessions[t + 5]

    def test_future_data_boundary_is_the_slice(self) -> None:
        # The model receives closes[: t + 1] and nothing after — a model
        # that could read ahead would see a longer slice here.
        sessions, closes = self._setup()
        seen: list[int] = []

        def spy(closes_in: tuple[float, ...]) -> tuple[float, ...]:
            seen.append(len(closes_in))
            return tuple(math.log(95.0 + 2.0 * k)
                         for k in range(len(QUANTILE_GRID)))

        grid = month_origin_grid(sessions, first_eval=date(2024, 1, 1),
                                 last_eval=None, horizon=5, min_history=1)
        evaluate_model(sessions, closes, grid=grid, horizon=5,
                       taus=QUANTILE_GRID, model_name="spy", model=spy)
        for t in grid.origins:
            assert t + 1 in seen      # exactly through the origin
        assert max(seen) <= len(sessions)

    def test_training_count_counts_completed_pairs(self) -> None:
        sessions, closes = self._setup()
        stub = bind(_constant_model, h=5, taus=QUANTILE_GRID)
        grid = month_origin_grid(sessions, first_eval=date(2024, 1, 1),
                                 last_eval=None, horizon=5, min_history=1)
        run = evaluate_model(sessions, closes, grid=grid, horizon=5,
                             taus=QUANTILE_GRID, model_name="stub",
                             model=stub)
        for row in run.ledger:
            t = sessions.index(row.origin_date)
            # eligible pairs u + 5 <= t, floored at zero for early rows
            assert row.training_count == max(0, t - 5 + 1)

    def test_none_return_is_a_counted_failure(self) -> None:
        sessions, closes = self._setup()
        calls: list[int] = []

        def flaky(closes_in: tuple[float, ...]) -> tuple[float, ...] | None:
            calls.append(len(closes_in))
            if len(calls) == 2:      # fail exactly one origin
                return None
            return tuple(math.log(95.0 + 2.0 * k)
                         for k in range(len(QUANTILE_GRID)))

        grid = month_origin_grid(sessions, first_eval=date(2024, 1, 1),
                                 last_eval=None, horizon=5, min_history=1)
        run = evaluate_model(sessions, closes, grid=grid, horizon=5,
                             taus=QUANTILE_GRID, model_name="flaky",
                             model=flaky)
        assert run.failure_reasons == {"fit_failed": 1}
        assert run.n_evaluated == len(grid.origins) - 1
        # the failed origin still records the actual it would have scored
        failed_rows = [r for r in run.ledger if r.status == "failed"]
        assert len(failed_rows) == 1 and failed_rows[0].actual is not None

    def test_unordered_quantiles_fail_never_repair(self) -> None:
        sessions, closes = self._setup()
        calls: list[int] = []

        def unordered(closes_in: tuple[float, ...]) -> tuple[float, ...]:
            calls.append(len(closes_in))
            if len(calls) == 2:
                return tuple(math.log(110.0 - 5.0 * k)   # DESCENDING
                             for k in range(len(QUANTILE_GRID)))
            return tuple(math.log(90.0 + 5.0 * k)
                         for k in range(len(QUANTILE_GRID)))

        grid = month_origin_grid(sessions, first_eval=date(2024, 1, 1),
                                 last_eval=None, horizon=5, min_history=1)
        run = evaluate_model(sessions, closes, grid=grid, horizon=5,
                             taus=QUANTILE_GRID, model_name="unordered",
                             model=unordered)
        assert run.failure_reasons == {"quantile_ordering": 1}

    def test_non_finite_fails(self) -> None:
        sessions, closes = self._setup()
        calls: list[int] = []

        def nan_once(closes_in: tuple[float, ...]) -> tuple[float, ...]:
            calls.append(len(closes_in))
            if len(calls) == 2:
                return (float("nan"),) * len(QUANTILE_GRID)
            return tuple(math.log(90.0 + 5.0 * k)
                         for k in range(len(QUANTILE_GRID)))

        grid = month_origin_grid(sessions, first_eval=date(2024, 1, 1),
                                 last_eval=None, horizon=5, min_history=1)
        run = evaluate_model(sessions, closes, grid=grid, horizon=5,
                             taus=QUANTILE_GRID, model_name="nan",
                             model=nan_once)
        assert run.failure_reasons == {"non_finite": 1}

    def test_ledger_losses_follow_pinball_orientation(self) -> None:
        # Hand-derived oracle (checkpoint B, surviving mutation 1):
        # recompute rho_tau from the row's OWN actual and quantiles and
        # demand the ledger agree — no other test derives production
        # ledger losses independently of the harness expression. The
        # wide stub band straddles the actuals, so BOTH branches
        # (y >= q and y < q) occur; a swapped tau weighting cannot pass.
        sessions, closes = self._setup()

        def wide(closes_in: tuple[float, ...]) -> tuple[float, ...]:
            _ = closes_in
            return tuple(math.log(80.0 + 30.0 * k)
                         for k in range(len(QUANTILE_GRID)))

        grid = month_origin_grid(sessions, first_eval=date(2024, 1, 1),
                                 last_eval=None, horizon=5, min_history=1)
        run = evaluate_model(sessions, closes, grid=grid, horizon=5,
                             taus=QUANTILE_GRID, model_name="wide",
                             model=wide)
        rows = [r for r in run.ledger if r.status == "evaluated"]
        assert rows
        saw_over = saw_under = False
        for row in rows:
            y = row.actual
            assert y is not None
            assert len(row.losses_by_tau) == len(QUANTILE_GRID)
            for tau, q, got in zip(QUANTILE_GRID, row.quantiles,
                                   row.losses_by_tau, strict=True):
                if y >= q:
                    saw_over = True
                    expected = tau * (y - q)
                else:
                    saw_under = True
                    expected = (1.0 - tau) * (q - y)
                assert got == pytest.approx(expected), (row.origin_date,
                                                        tau)
        assert saw_over and saw_under

    def test_exp_overflow_is_a_counted_non_finite_failure(self) -> None:
        # Finite log-quantiles whose LEVELS overflow exp: a counted
        # failure that retains the ledger — never an exception that
        # crashes the run and discards it (checkpoint B, P2-4).
        sessions, closes = self._setup()
        calls: list[int] = []

        def huge(closes_in: tuple[float, ...]) -> tuple[float, ...]:
            calls.append(len(closes_in))
            if len(calls) == 2:      # exactly one origin overflows
                return (1000.0, 1000.1, 1000.2, 1000.3, 1000.4)
            return tuple(math.log(90.0 + 5.0 * k)
                         for k in range(len(QUANTILE_GRID)))

        grid = month_origin_grid(sessions, first_eval=date(2024, 1, 1),
                                 last_eval=None, horizon=5, min_history=1)
        run = evaluate_model(sessions, closes, grid=grid, horizon=5,
                             taus=QUANTILE_GRID, model_name="huge",
                             model=huge)
        assert run.failure_reasons == {"non_finite": 1}
        assert run.n_evaluated == len(grid.origins) - 1
        failed = [r for r in run.ledger if r.status == "failed"]
        assert failed and failed[0].actual is not None

    def test_tally_identity_is_enforced(self) -> None:
        # A tally whose arithmetic does not close is a defect, not a
        # display: the identity assert must fire.
        run = ModelRun(model="broken", actuals=(1.0, 2.0, 3.0),
                       failure_reasons={"fit_failed": 1},
                       ledger=(LedgerRow(
                           origin_date=date(2024, 1, 1),
                           target_date=date(2024, 2, 1),
                           training_count=1, status="excluded",
                           reason="insufficient_history", actual=None),))
        with pytest.raises(AssertionError, match="tally identity"):
            # 3 evaluated + 1 excluded + 1 failed = 5; total 6 does not
            # close — the identity assert must fire.
            run.tally(total=6, floor=12)

    def test_tally_closes_on_a_mixed_run(self) -> None:
        sessions, closes = self._setup()
        calls: list[int] = []

        def flaky(closes_in: tuple[float, ...]) -> tuple[float, ...] | None:
            calls.append(len(closes_in))
            if len(calls) == 1:
                return None
            return tuple(math.log(90.0 + 5.0 * k)
                         for k in range(len(QUANTILE_GRID)))

        grid = month_origin_grid(sessions, first_eval=date(2024, 1, 1),
                                 last_eval=None, horizon=5, min_history=1)
        run = evaluate_model(sessions, closes, grid=grid, horizon=5,
                             taus=QUANTILE_GRID, model_name="flaky",
                             model=flaky)
        tally = run.tally(total=grid.total, floor=12)
        # month starts: 0 (excluded insufficient_history), 13 (failed),
        # 26 (evaluated) on the 39-session setup — the identity itself
        # is asserted inside tally(); here we pin the classification.
        assert tally.excluded == 1
        assert tally.evaluated == run.n_evaluated
        assert run.n_failed == 1
        assert tally.floor_met is False


class TestForwardFan:
    def test_valid_and_invalid(self) -> None:
        good = bind(_constant_model, h=5, taus=QUANTILE_GRID)
        fan = forward_fan((100.0, 101.0, 102.0), good)
        assert fan is not None
        assert fan[0] < fan[-1]

        def none_model(closes_in: tuple[float, ...]
                       ) -> tuple[float, ...] | None:
            _ = closes_in
            return None

        assert forward_fan((100.0, 101.0), none_model) is None

        def unordered(closes_in: tuple[float, ...]) -> tuple[float, ...]:
            _ = closes_in
            return (math.log(110.0), math.log(100.0), math.log(105.0),
                    math.log(103.0), math.log(108.0))

        assert forward_fan((100.0, 101.0), unordered) is None

    def test_overflow_returns_none_never_raises(self) -> None:
        # Finite log-quantiles that overflow exp at the latest fit: the
        # fan is unavailable (None), never an exception (P2-4).
        def huge(closes_in: tuple[float, ...]) -> tuple[float, ...]:
            _ = closes_in
            return (1000.0, 1000.1, 1000.2, 1000.3, 1000.4)

        assert forward_fan((100.0, 101.0, 102.0), huge) is None


def test_rw_full_is_bindable_and_ordered() -> None:
    # An irregular (non-arithmetic) series so the h-step changes are
    # distinct — an arithmetic ladder would make every change identical
    # and every quantile tied.
    closes = tuple(100.0 + ((i * 37) % 23) + 0.1 * i for i in range(30))
    model = bind(rw_full, h=5, taus=QUANTILE_GRID)
    out = model(closes)
    assert out is not None
    assert all(b > a for a, b in itertools.pairwise(out))
