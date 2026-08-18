"""Splitter property tests: purge, embargo, grouping, anchor (INV-05/06)."""

from __future__ import annotations

import itertools

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tree_options.splitting.checks import (
    FoldInvariantViolation,
    check_folds,
    check_same_session_grouping,
)
from tree_options.splitting.splitter import WalkForwardSplitter, assign_rows

H_VALUES = [1, 3, 5, 10]


def _splitter(cal, h=5, e=5, val=20, test=10, roll=10, min_train=15):
    return WalkForwardSplitter(
        cal,
        label_horizon_sessions=h,
        embargo_sessions=e,
        val_sessions=val,
        test_sessions=test,
        roll_sessions=roll,
        min_train_sessions=min_train,
    )


class TestExactShape:
    def test_fold_boundaries_and_purge_gap(self, synthetic_calendar):
        cal = synthetic_calendar
        sp = _splitter(cal)
        folds = sp.splits()
        assert len(folds) >= 1
        sessions = cal.sessions()
        f0 = folds[0]
        t0 = 15 + 10 + 20  # min_train + gap + val
        assert min(f0.test_sessions) == sessions[t0]
        assert max(f0.validation_sessions) == sessions[t0 - 1]
        last_train_ord = max(cal.ordinal(s) for s in f0.train_sessions)
        assert last_train_ord == t0 - 20 - 10 - 1  # val_len + gap - 1
        assert len(f0.train_sessions) == 15

    def test_anchored_expanding_train(self, synthetic_calendar):
        sp = _splitter(synthetic_calendar)
        folds = sp.splits()
        anchor = min(folds[0].train_sessions)
        for f in folds:
            assert min(f.train_sessions) == anchor
            assert len(f.train_sessions) >= len(folds[0].train_sessions)

    def test_rolls_forward_by_roll_sessions(self, synthetic_calendar):
        sp = _splitter(synthetic_calendar)
        folds = sp.splits()
        for a, b in itertools.pairwise(folds):
            assert (
                min(cal_o(synthetic_calendar, b.test_sessions))
                - min(cal_o(synthetic_calendar, a.test_sessions))
                == sp.roll_sessions
            )


def cal_o(cal, sessions):
    return [cal.ordinal(s) for s in sessions]


class TestInvariantProperties:
    @pytest.mark.parametrize("h", H_VALUES)
    @given(seed=st.integers(0, 2**31 - 1), val=st.integers(8, 40), test=st.integers(5, 20))
    # synthetic_calendar is immutable and identical for every generated input,
    # so not resetting it between examples is safe.
    @settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_check_folds_passes_on_splitter_output(self, synthetic_calendar, h, seed, val, test):
        import random

        rng = random.Random(seed)
        roll = test  # non-overlapping test blocks
        sp = _splitter(synthetic_calendar, h=h, e=5, val=val, test=test, roll=roll, min_train=10)
        folds = sp.splits()
        assert folds, "parameters must admit at least one fold"
        check_folds(
            folds,
            calendar=synthetic_calendar,
            label_horizon_sessions=h,
            embargo_sessions=5,
        )
        # INV-05 structural: same session rows never split.
        for f in folds:
            rows = rng.choices(synthetic_calendar.sessions(), k=200)
            assignment = assign_rows(f, rows)
            check_same_session_grouping(assignment, rows)

    def test_gap_math_property(self, synthetic_calendar):
        """For every fold: ordinal(first eval) - ordinal(last train) > H + E."""
        h, e = 5, 5
        sp = _splitter(synthetic_calendar, h=h, e=e)
        for f in sp.splits():
            last_train = max(synthetic_calendar.ordinal(s) for s in f.train_sessions)
            first_eval = min(synthetic_calendar.ordinal(s) for s in f.all_eval_sessions)
            assert first_eval - last_train > h + e

    def test_no_train_label_interval_overlaps_eval(self, synthetic_calendar):
        """INV-06 direct: label window [s+1, s+H] ∩ eval block = empty."""
        h = 5
        sp = _splitter(synthetic_calendar, h=h, e=0)
        cal = synthetic_calendar
        for f in sp.splits():
            eval_ords = {cal.ordinal(t) for t in f.all_eval_sessions}
            for s in f.train_sessions:
                s_ord = cal.ordinal(s)
                for k in range(1, h + 1):
                    assert s_ord + k not in eval_ords

    def test_purge_violation_detected(self, synthetic_calendar):
        """Adversarial: hand-built fold with purge overlap must be caught."""
        cal = synthetic_calendar
        sessions = cal.sessions()
        h, e = 5, 5
        bad_train = frozenset(sessions[100:130])
        bad_val = frozenset(sessions[131 : 131 + 10])  # within H of session 129
        bad_test = frozenset(sessions[150 : 150 + 5])
        from tree_options.splitting.splitter import Fold

        bad = Fold(
            fold_id=0,
            train_sessions=bad_train,
            validation_sessions=bad_val,
            test_sessions=bad_test,
            final_fit_train_sessions=bad_train,
        )
        with pytest.raises(FoldInvariantViolation) as ei:
            check_folds([bad], calendar=cal, label_horizon_sessions=h, embargo_sessions=e)
        assert ei.value.code in {"PURGE_OVERLAP", "EMBARGO_GAP"}

    def test_embargo_only_violation_detected(self, synthetic_calendar):
        """Adversarial: gap in (H, H+E] — embargo broken, purge intact.
        Only EMBARGO_GAP may fire here, so a gutted embargo checker fails."""
        from tree_options.splitting.splitter import Fold

        cal = synthetic_calendar
        sessions = cal.sessions()
        bad = Fold(
            fold_id=0,
            train_sessions=frozenset(sessions[100:130]),  # last train = ord 129
            validation_sessions=frozenset(sessions[135:145]),  # gap 6 in (5, 10]
            test_sessions=frozenset(sessions[160:170]),
            final_fit_train_sessions=frozenset(sessions[100:130]),
        )
        with pytest.raises(FoldInvariantViolation) as ei:
            check_folds([bad], calendar=cal, label_horizon_sessions=5, embargo_sessions=5)
        assert ei.value.code == "EMBARGO_GAP"

    def test_anchor_violation_detected(self, synthetic_calendar):
        """Adversarial: train does not start at session 0, everything else
        clean — only ANCHOR_MONOTONE may fire."""
        from tree_options.splitting.splitter import Fold

        cal = synthetic_calendar
        sessions = cal.sessions()
        bad = Fold(
            fold_id=0,
            train_sessions=frozenset(sessions[5:50]),
            validation_sessions=frozenset(sessions[70:80]),
            test_sessions=frozenset(sessions[90:100]),
            final_fit_train_sessions=frozenset(sessions[5:50]),
        )
        with pytest.raises(FoldInvariantViolation) as ei:
            check_folds([bad], calendar=cal, label_horizon_sessions=5, embargo_sessions=5)
        assert ei.value.code == "ANCHOR_MONOTONE"

    def test_coverage_violation_detected(self, synthetic_calendar):
        """Adversarial: two folds share a test session — COVERAGE must fire."""
        from tree_options.splitting.splitter import Fold

        cal = synthetic_calendar
        sessions = cal.sessions()
        f1 = Fold(
            fold_id=0,
            train_sessions=frozenset(sessions[0:30]),
            validation_sessions=frozenset(sessions[40:50]),
            test_sessions=frozenset(sessions[60:70]),
            final_fit_train_sessions=frozenset(sessions[0:30]),
        )
        f2 = Fold(
            fold_id=1,
            # train 0:35, val 45:55, test 65:75 — independently valid
            # (last train ord 34, first eval ord 45: gap 11 > H+E) so ONLY
            # the shared test block 65:69 with fold 1 triggers a violation.
            train_sessions=frozenset(sessions[0:35]),
            validation_sessions=frozenset(sessions[45:55]),
            test_sessions=frozenset(sessions[65:75]),  # overlaps fold 1's 60:70
            final_fit_train_sessions=frozenset(sessions[0:35]),
        )
        with pytest.raises(FoldInvariantViolation) as ei:
            check_folds([f1, f2], calendar=cal, label_horizon_sessions=5, embargo_sessions=5)
        assert ei.value.code == "COVERAGE"

    def test_session_grouping_violation_detected(self, synthetic_calendar):
        cal = synthetic_calendar
        sessions = cal.sessions()
        rows = [sessions[55], sessions[55], sessions[65]]
        assignment = {
            "train": (0,),
            "validation": (1,),
            "test": (2,),
            "final_fit_train": (0,),
        }
        with pytest.raises(FoldInvariantViolation) as ei:
            check_same_session_grouping(assignment, rows)
        assert ei.value.code == "SESSION_GROUPING"

    def test_from_protocol_shape(self, synthetic_calendar, protocol):
        sp = WalkForwardSplitter.from_protocol(synthetic_calendar, protocol)
        assert sp.label_horizon_sessions == 5
        assert sp.embargo_sessions == 5
        folds = sp.splits()
        assert folds
        check_folds(
            folds,
            calendar=synthetic_calendar,
            label_horizon_sessions=5,
            embargo_sessions=5,
        )

    def test_unknown_panel_session_fails_closed(self, synthetic_calendar):
        from datetime import date

        sp = _splitter(synthetic_calendar)
        with pytest.raises(ValueError, match="not in calendar"):
            sp.splits([date(2019, 1, 5)])  # Saturday: not a session

    def test_duplicate_fold_ids_with_overlapping_tests_rejected(self, synthetic_calendar):
        """F16: two folds sharing a fold_id AND a test block must be caught —
        the COVERAGE check may not key on the stored id being different."""
        from tree_options.splitting.splitter import Fold

        cal = synthetic_calendar
        sessions = cal.sessions()
        dup = Fold(
            fold_id=0,  # SAME id as f1 below
            train_sessions=frozenset(sessions[0:35]),
            validation_sessions=frozenset(sessions[45:55]),
            test_sessions=frozenset(sessions[60:70]),
            final_fit_train_sessions=frozenset(sessions[0:35]),
        )
        f1 = Fold(
            fold_id=0,
            train_sessions=frozenset(sessions[0:30]),
            validation_sessions=frozenset(sessions[40:50]),
            test_sessions=frozenset(sessions[60:70]),
            final_fit_train_sessions=frozenset(sessions[0:30]),
        )
        with pytest.raises(FoldInvariantViolation):
            check_folds([f1, dup], calendar=cal, label_horizon_sessions=5, embargo_sessions=5)
