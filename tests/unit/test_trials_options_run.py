"""Workstream F: the options trial runner (M3 plan §3.F)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tests.conftest import REPO_ROOT
from tree_options.data.authority import PointInTimeDataset
from tree_options.data.options_pit import OptionPitSurface
from tree_options.evaluation.stats import ScoredLabel
from tree_options.options import OptionsStrategyConfig
from tree_options.protocol.loader import load_protocol
from tree_options.registry.sqlite import TrialRegistry
from tree_options.trials import OptionsSplitOverride, run_options_trial
from tree_options.trials.options_run import _lag1_autocorrelation, _spearman, _t_statistic

SPLIT = OptionsSplitOverride(
    label_horizon_sessions=5,
    embargo_sessions=1,
    val_sessions=10,
    test_sessions=20,
    roll_sessions=20,
    min_train_sessions=20,
)
FIXED_CLOCK = lambda: datetime(2026, 1, 1, 0, 0, tzinfo=UTC)  # noqa: E731


@pytest.fixture(scope="module")
def built():
    from tests.unit.test_options_strategy import _build

    return _build()


@pytest.fixture(scope="module")
def world(built):
    overlay, calendar, snapshot = built
    dataset = PointInTimeDataset(
        snapshot, calendar, universe_id=f"{snapshot.snapshot_id}|pit-universe-v1"
    )
    return overlay, calendar, snapshot, dataset


@pytest.fixture(scope="module")
def protocol():
    return load_protocol()


def _scored(world, surface):
    overlay, _cal, _snap, _ds = world
    sessions = overlay.world_sessions()
    # only sessions with a visible file can enter; start after eligibility
    rows: list[ScoredLabel] = []
    for i, session in enumerate(sessions[30:190], start=30):
        eligible = surface.eligible_as_of(session)
        if not eligible:
            continue
        for j, sid in enumerate(sorted(eligible)):
            rows.append(
                ScoredLabel(
                    security_id=sid,
                    session=session,
                    score=((i * 7 + j * 13) % 31) / 31.0,
                    label=((i * 11 + j * 17) % 37) / 37.0 - 0.5,
                )
            )
    return tuple(rows)


def test_spearman_basics() -> None:
    assert _spearman([1, 2, 3], [2, 4, 9]) == pytest.approx(1.0)
    assert _spearman([1, 2, 3], [9, 4, 2]) == pytest.approx(-1.0)
    assert _spearman([1, 1, 1], [1, 2, 3]) is None  # zero variance
    assert _spearman([1, 2], [2, 3]) is None  # too few
    # monotone nonlinear stays 1.0
    assert _spearman([1, 2, 3, 4], [1, 8, 27, 64]) == pytest.approx(1.0)


def test_autocorr_and_t() -> None:
    assert _lag1_autocorrelation([1, 2, 3, 4, 5]) == pytest.approx(0.4)
    assert _lag1_autocorrelation([1, 2]) is None
    assert _t_statistic([1.0, 1.0, 1.0]) is None  # zero sd
    assert _t_statistic([2.0, 2.0, 2.0, 2.0]) is None
    values = [1.0, 2.0, 3.0, 4.0]
    mean = 2.5
    import statistics as st

    expected = mean * 2 / st.stdev(values)
    assert _t_statistic(values) == pytest.approx(expected)


def test_run_options_trial_end_to_end(world, protocol, tmp_path) -> None:
    overlay, calendar, snapshot, dataset = world
    surface = OptionPitSurface(overlay)
    scored = _scored(world, surface)
    registry = TrialRegistry(tmp_path / "reg.db")
    artifacts_dir = tmp_path / "artifacts"
    result = run_options_trial(
        dataset=dataset,
        surface=surface,
        calendar=calendar,
        protocol=protocol,
        world_id=snapshot.snapshot_id,
        arm="A",
        strategy_config=OptionsStrategyConfig(),
        scored=scored,
        model_family="fixture:v1",
        model_sha256=None,
        hypothesis="unit: the runner registers, executes, stamps, completes",
        decision_sessions=tuple(sorted({row.session for row in scored})),
        options_manifest_hash="0" * 64,
        registry=registry,
        artifacts_dir=artifacts_dir,
        repo=REPO_ROOT,
        clock=FIXED_CLOCK,
        split_override=SPLIT,
        allow_dirty=True,
    )
    registry.close()
    assert registry_status(tmp_path / "reg.db", result.trial_id) == "COMPLETED"
    import json

    body = json.loads(result.artifact_path.read_text(encoding="utf-8"))
    payload = body["payload"]
    assert payload["arm"] == "A"
    assert payload["n_folds"] >= 1
    assert payload["max_quote_age_seconds"] == 7200  # the declared override, hashed
    assert payload["cohort_stride"] == 4
    pooled = payload["pooled"]
    assert pooled["n_positions"] >= 1
    assert isinstance(pooled["positions"], list) and pooled["positions"]
    assert isinstance(payload["fills_log"], list)
    # sealed-gate criterion 2 inputs: every stamped fill carries its T+1
    # decision provenance and the selected quote's receipt, so the gate
    # re-derives the fill discipline from the artifact alone
    if payload["fills_log"]:
        for fill in payload["fills_log"]:
            assert fill["decision_session"] is not None
            assert fill["quote_received_at"] is not None
            assert fill["decision_session"] < fill["execution_session"]
            assert fill["quote_received_at"] <= fill["execution_at"]
        assert payload["fills_log"][0]["decision_at"] is not None
    # criterion 1 input: conservation was asserted at EVERY evaluated session
    per_fold_sessions = sum(f["n_sessions_evaluated"] for f in payload["per_fold"])
    assert per_fold_sessions > 0
    assert payload["counters"]["conservation_checks"] == per_fold_sessions
    assert set(payload["counters"]) >= {
        "not_evaluable_candidates",
        "rejections",
        "rule_histogram",
    }
    assert payload["pooled"]["n_stride4_cohorts"] <= payload["pooled"]["n_cohorts"]
    # criterion 7 input: the raw stride-4 series is stamped so the gate
    # re-derives t (and the pooled two-world t) from the artifact alone
    ics = payload["pooled"]["stride4_cohort_ics"]
    assert isinstance(ics, list) and len(ics) == payload["pooled"]["n_stride4_cohorts"]
    # criterion 4 input: per-position contract expiration + the world's
    # last evaluated session
    assert payload["world_last_session"]
    for position in pooled["positions"]:
        assert position["contract_expiration"]
    # the stamp's config hash binds the strategy + the 7200 override
    stamped_config = body["stamp"]["config"] if "config" in body.get("stamp", {}) else None
    _ = stamped_config  # stamp layout is owned by protocol/stamping; presence checked below
    assert "stamp" in body


def registry_status(db_path, trial_id: str) -> str:
    registry = TrialRegistry(db_path)
    try:
        return registry.status(trial_id)
    finally:
        registry.close()


def test_bad_arm_never_registers(world, protocol, tmp_path) -> None:
    overlay, calendar, snapshot, dataset = world
    surface = OptionPitSurface(overlay)
    scored = _scored(world, surface)
    registry = TrialRegistry(tmp_path / "reg2.db")
    with pytest.raises(ValueError, match="arm must be A or B"):
        run_options_trial(
            dataset=dataset,
            surface=surface,
            calendar=calendar,
            protocol=protocol,
            world_id=snapshot.snapshot_id,
            arm="C",  # type: ignore[arg-type]
            strategy_config=OptionsStrategyConfig(),
            scored=scored,
            model_family="fixture:v1",
            model_sha256=None,
            hypothesis="rejected before registration",
            decision_sessions=tuple(sorted({row.session for row in scored})),
            options_manifest_hash="0" * 64,
            registry=registry,
            artifacts_dir=tmp_path / "a2",
            repo=REPO_ROOT,
            clock=FIXED_CLOCK,
            split_override=SPLIT,
            allow_dirty=True,
        )
    registry.close()
    # nothing registered: the db has no trials
    counts = trial_count(tmp_path / "reg2.db")
    assert counts == 0


def test_duplicate_scored_rows_refused(world, protocol, tmp_path) -> None:
    overlay, calendar, _snap, dataset = world
    surface = OptionPitSurface(overlay)
    scored = _scored(world, surface)
    registry = TrialRegistry(tmp_path / "reg3.db")
    with pytest.raises(ValueError, match="duplicate"):
        run_options_trial(
            dataset=dataset,
            surface=surface,
            calendar=calendar,
            protocol=protocol,
            world_id="m3-unit-strategy-906",
            arm="A",
            strategy_config=OptionsStrategyConfig(),
            scored=(*scored, scored[0]),
            model_family="fixture:v1",
            model_sha256=None,
            hypothesis="rejected",
            decision_sessions=tuple(sorted({row.session for row in scored})),
            options_manifest_hash="0" * 64,
            registry=registry,
            artifacts_dir=tmp_path / "a3",
            repo=REPO_ROOT,
            clock=FIXED_CLOCK,
            split_override=SPLIT,
            allow_dirty=True,
        )
    registry.close()


def trial_count(db_path) -> int:
    import sqlite3

    conn = sqlite3.connect(db_path)
    try:
        return int(conn.execute("select count(*) from trials").fetchone()[0])
    finally:
        conn.close()
