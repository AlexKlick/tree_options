"""Workstream D: ridge pipeline + FittingGuard enforcement (M2 §3.D).

The pipeline IS the INV-07 enforcement point: fit-once under a stable
artifact name, fit-set/target-set disjointness asserted at score time,
applications recorded, rows outside the declared target set refused.
"""

from __future__ import annotations

import hashlib
import math
import random
import subprocess
import sys
from datetime import date, timedelta

import pytest

from tests.conftest import REPO_ROOT
from tree_options.guards.fitting import FittingGuard, FittingLeakError
from tree_options.models import FitRow, ObsRow, RidgePipeline

D1 = date(2024, 1, 2)
# held/out-of-sample sessions live far beyond every train range (train
# rows get consecutive dates from D1; the guard MUST fire on overlap)
HELD = date(2025, 6, 2)
FEATURES = ("x1", "x2")


def _linear_rows(seed: int, n: int, start: date) -> list[FitRow]:
    """y = 2*x1 - 1*x2 + 3 exactly (no noise): recoverable to float noise."""
    rng = random.Random(seed)
    rows: list[FitRow] = []
    for i in range(n):
        x1 = rng.uniform(-2.0, 2.0)
        x2 = rng.uniform(-2.0, 2.0)
        rows.append(
            FitRow(
                session=start + timedelta(days=i),
                security_id=f"S{i:04d}",
                features={"x1": x1, "x2": x2},
                label=2.0 * x1 - 1.0 * x2 + 3.0,
            )
        )
    return rows


def _as_obs(rows: list[FitRow]) -> list[ObsRow]:
    return [ObsRow(session=r.session, security_id=r.security_id, features=r.features) for r in rows]


def test_ridge_recovers_exact_linear_map() -> None:
    pipe = RidgePipeline(name="t1", feature_names=FEATURES, ridge_lambda=1e-8)
    train = _linear_rows(7, 200, D1)
    pipe.fit(train, fit_sessions=frozenset(r.session for r in train))
    held = _linear_rows(99, 20, HELD)
    scores = pipe.score(_as_obs(held), target_sessions=frozenset(r.session for r in held))
    assert len(scores) == 20
    for row, obs in zip(scores, held, strict=True):
        assert row.score == pytest.approx(
            2.0 * obs.features["x1"] - obs.features["x2"] + 3.0, abs=1e-6
        )


def test_refit_is_refused() -> None:
    pipe = RidgePipeline(name="t2", feature_names=FEATURES)
    train = _linear_rows(1, 30, D1)
    sessions = frozenset(r.session for r in train)
    pipe.fit(train, fit_sessions=sessions)
    with pytest.raises(FittingLeakError, match="REFIT"):
        pipe.fit(train, fit_sessions=sessions)


def test_score_before_fit_is_refused() -> None:
    pipe = RidgePipeline(name="t3", feature_names=FEATURES)
    obs = [ObsRow(session=D1, security_id="S1", features={"x1": 0.0, "x2": 0.0})]
    with pytest.raises(FittingLeakError, match="UNFITTED_ARTIFACT"):
        pipe.score(obs, target_sessions=frozenset({D1}))


def test_fit_on_eval_sessions_is_detected_at_score() -> None:
    pipe = RidgePipeline(name="t4", feature_names=FEATURES)
    train = _linear_rows(2, 30, D1)  # sessions D1..D1+29
    pipe.fit(train, fit_sessions=frozenset(r.session for r in train))
    # try to score a session the artifact was FIT on
    leak_obs = [ObsRow(session=train[0].session, security_id="SX", features={"x1": 0.0, "x2": 0.0})]
    with pytest.raises(FittingLeakError, match="FIT_ON_EVAL"):
        pipe.score(leak_obs, target_sessions=frozenset({train[0].session}))


def test_rows_outside_declared_targets_are_refused() -> None:
    pipe = RidgePipeline(name="t5", feature_names=FEATURES)
    train = _linear_rows(3, 30, D1)
    pipe.fit(train, fit_sessions=frozenset(r.session for r in train))
    outside = ObsRow(
        session=HELD + timedelta(days=90), security_id="SY", features={"x1": 0.0, "x2": 0.0}
    )
    with pytest.raises(ValueError, match="not in the declared target sessions"):
        pipe.score([outside], target_sessions=frozenset({HELD}))


def test_standardizer_uses_train_statistics_only() -> None:
    pipe = RidgePipeline(name="t6", feature_names=FEATURES, ridge_lambda=1e-8)
    train = _linear_rows(11, 100, D1)
    pipe.fit(train, fit_sessions=frozenset(r.session for r in train))
    mean = pipe._mean
    assert mean is not None
    assert mean[0] == pytest.approx(sum(r.features["x1"] for r in train) / 100)
    # scoring wildly-different rows must not move the stored statistics
    wild = [
        ObsRow(session=HELD, security_id=f"W{i}", features={"x1": 1e6, "x2": -1e6})
        for i in range(5)
    ]
    pipe.score(wild, target_sessions=frozenset({HELD}))
    assert pipe._mean is not None
    assert pipe._mean.tolist() == mean.tolist()


def test_constant_feature_is_inert() -> None:
    rows = _linear_rows(5, 100, D1)
    with_const = [
        FitRow(
            session=r.session,
            security_id=r.security_id,
            features={**r.features, "const": 7.5},
            label=r.label,
        )
        for r in rows
    ]
    plain = RidgePipeline(name="t7a", feature_names=FEATURES, ridge_lambda=1.0)
    plain.fit(rows, fit_sessions=frozenset(r.session for r in rows))
    extended = RidgePipeline(name="t7b", feature_names=(*FEATURES, "const"), ridge_lambda=1.0)
    extended.fit(with_const, fit_sessions=frozenset(r.session for r in rows))
    held = _linear_rows(6, 10, HELD)
    obs_plain = _as_obs(held)
    obs_const = [
        ObsRow(session=r.session, security_id=r.security_id, features={**r.features, "const": 7.5})
        for r in held
    ]
    targets = frozenset(r.session for r in held)
    a = plain.score(obs_plain, target_sessions=targets)
    b = extended.score(obs_const, target_sessions=targets)
    for ra, rb in zip(a, b, strict=True):
        assert ra.score == pytest.approx(rb.score, abs=1e-9)
    assert extended._coef is not None
    assert extended._coef[2] == 0.0


def test_missing_feature_rows_are_skipped_not_imputed() -> None:
    pipe = RidgePipeline(name="t8", feature_names=(*FEATURES, "x3"), ridge_lambda=1.0)
    rows = _linear_rows(8, 50, D1)
    full = [
        FitRow(
            session=r.session,
            security_id=r.security_id,
            features={**r.features, "x3": 0.5},
            label=r.label,
        )
        for r in rows
    ]
    pipe.fit(full, fit_sessions=frozenset(r.session for r in rows))
    held = _linear_rows(9, 10, HELD)
    obs = [
        ObsRow(session=r.session, security_id=r.security_id, features={**r.features, "x3": 0.5})
        for r in held
    ]
    obs[3] = ObsRow(
        session=obs[3].session, security_id=obs[3].security_id, features={"x1": 0.0, "x2": 0.0}
    )  # x3 missing
    scores = pipe.score(obs, target_sessions=frozenset(r.session for r in held))
    assert len(scores) == 9
    assert obs[3].security_id not in {s.security_id for s in scores}


def test_application_recorded_on_guard() -> None:
    guard = FittingGuard()
    pipe = RidgePipeline(name="t9", feature_names=FEATURES, guard=guard)
    rows = _linear_rows(10, 30, D1)
    pipe.fit(rows, fit_sessions=frozenset(r.session for r in rows))
    held = _linear_rows(13, 3, HELD)
    pipe.score(_as_obs(held), target_sessions=frozenset(r.session for r in held))
    assert guard.applied_sessions(pipe.artifact) == frozenset(r.session for r in held)


def test_fit_rows_must_belong_to_declared_fit_sessions() -> None:
    pipe = RidgePipeline(name="t10", feature_names=FEATURES)
    rows = _linear_rows(12, 10, D1)
    with pytest.raises(ValueError, match="not in the declared fit sessions"):
        pipe.fit(rows, fit_sessions=frozenset({rows[0].session}))
    assert not pipe.fitted


@pytest.mark.parametrize("ridge_lambda", [math.nan, math.inf, -math.inf])
def test_ridge_lambda_must_be_finite(ridge_lambda: float) -> None:
    with pytest.raises(ValueError, match="finite and >= 0"):
        RidgePipeline(
            name="nonfinite-lambda",
            feature_names=FEATURES,
            ridge_lambda=ridge_lambda,
        )


@pytest.mark.parametrize(("field", "value"), [("feature", math.nan), ("label", math.inf)])
def test_fit_inputs_must_be_finite(field: str, value: float) -> None:
    pipe = RidgePipeline(name=f"nonfinite-{field}", feature_names=FEATURES)
    row = FitRow(
        session=D1,
        security_id="S1",
        features={"x1": value if field == "feature" else 0.0, "x2": 0.0},
        label=value if field == "label" else 0.0,
    )
    with pytest.raises(ValueError, match="must be finite"):
        pipe.fit([row], fit_sessions=frozenset({D1}))


def test_score_nonfinite_feature_is_refused() -> None:
    pipe = RidgePipeline(name="nonfinite-score", feature_names=FEATURES)
    train = _linear_rows(13, 10, D1)
    pipe.fit(train, fit_sessions=frozenset(row.session for row in train))
    obs = ObsRow(
        session=HELD,
        security_id="S1",
        features={"x1": math.inf, "x2": 0.0},
    )
    with pytest.raises(ValueError, match="score features must be finite"):
        pipe.score([obs], target_sessions=frozenset({HELD}))


def test_score_with_no_usable_rows_is_empty() -> None:
    pipe = RidgePipeline(name="empty-score", feature_names=FEATURES)
    train = _linear_rows(14, 10, D1)
    pipe.fit(train, fit_sessions=frozenset(row.session for row in train))
    obs = ObsRow(session=HELD, security_id="S1", features={"x1": 0.0})
    assert pipe.score([obs], target_sessions=frozenset({HELD})) == ()


_SUBPROCESS_SNIPPET = """
import sys
sys.path.insert(0, {src!r})
sys.path.insert(0, {tests!r})
from tree_options.models.determinism import force_single_threaded_blas
force_single_threaded_blas()
from tests.unit.test_models_pipeline import _linear_rows, fit_hash
import json
print(json.dumps(fit_hash({seed!r})))
"""


def fit_hash(seed: int) -> dict[str, str]:
    rows = _linear_rows(seed, 300, D1)
    pipe = RidgePipeline(name="sub", feature_names=FEATURES, ridge_lambda=1.0)
    pipe.fit(rows, fit_sessions=frozenset(r.session for r in rows))
    blob = pipe.artifact_bytes()
    assert pipe._coef is not None
    return {
        "artifact_sha256": hashlib.sha256(blob).hexdigest(),
        "coef_sha256": hashlib.sha256(
            ",".join(repr(float(v)) for v in pipe._coef).encode()
        ).hexdigest(),
    }


def test_deterministic_across_runs_and_processes() -> None:
    in_process = fit_hash(1234)
    assert fit_hash(1234) == in_process  # same process, twice
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            _SUBPROCESS_SNIPPET.format(src=str(REPO_ROOT / "src"), tests=str(REPO_ROOT), seed=1234),
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    import json

    cross_process = json.loads(result.stdout.strip().splitlines()[-1])
    assert cross_process == in_process
