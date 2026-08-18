"""INV-07 executable core: fit-on-train-only (review F1)."""

from __future__ import annotations

from datetime import date

import pytest

from tree_options.guards.fitting import FittingGuard, FittingLeakError

TRAIN = frozenset(date(2024, 1, d) for d in range(2, 20))
VAL = frozenset(date(2024, 2, d) for d in range(1, 20))
TEST = frozenset(date(2024, 3, d) for d in range(1, 20))


def test_fit_on_train_apply_to_train_ok():
    g = FittingGuard()
    g.fit_on("zscore_scaler", TRAIN)
    g.apply_to("zscore_scaler", TRAIN)
    g.assert_fit_excludes("zscore_scaler", VAL | TEST)


def test_fit_that_included_eval_is_detected():
    g = FittingGuard()
    leaky = TRAIN | {date(2024, 2, 5)}  # imputer 'accidentally' fit on a val day
    g.fit_on("imputer", leaky)
    with pytest.raises(FittingLeakError) as ei:
        g.assert_fit_excludes("imputer", VAL)
    assert ei.value.code == "FIT_ON_EVAL"


def test_cannot_apply_unfitted_artifact():
    g = FittingGuard()
    with pytest.raises(FittingLeakError) as ei:
        g.apply_to("selector", TRAIN)
    assert ei.value.code == "UNFITTED_ARTIFACT"


def test_no_refit_under_same_name():
    g = FittingGuard()
    g.fit_on("scaler", TRAIN)
    with pytest.raises(FittingLeakError) as ei:
        g.fit_on("scaler", VAL)  # refit on eval under the same name
    assert ei.value.code == "REFIT"


def test_fit_set_is_recorded_and_inspectable():
    g = FittingGuard()
    g.fit_on("scaler", TRAIN)
    assert g.fit_sessions("scaler") == TRAIN
