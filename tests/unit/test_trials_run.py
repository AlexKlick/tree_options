"""Workstream F: the trial runner (M2 §3.F) — register before outcome,
provenance triple end-to-end, stamped artifacts, no-limbo failures.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tree_options.data.authority import PointInTimeDataset
from tree_options.data.ingest import ingest_snapshot
from tree_options.models.determinism import force_single_threaded_blas
from tree_options.protocol.stamping import DirtyWorktreeError
from tree_options.registry.errors import DuplicateTrialError
from tree_options.registry.scope import TrialScope
from tree_options.registry.sqlite import TrialRegistry
from tree_options.synth import ActionRates, WorldSpec, generate_world
from tree_options.time.calendar import StaticSessionCalendar
from tree_options.trials import DEV_TRIAL_CONFIGS, SplitOverride, run_trial

force_single_threaded_blas()

T0 = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)
SMALL_SPLIT = SplitOverride(
    label_horizon_sessions=5,
    embargo_sessions=2,
    val_sessions=10,
    test_sessions=10,
    roll_sessions=10,
    min_train_sessions=20,
)
FEATURES = ("ret_1", "mom_5", "mom_20", "dol_vol_20")


def _clock_factory():
    counter = iter(range(10_000))
    return lambda: T0 + timedelta(minutes=next(counter))


def _dataset(static_calendar: StaticSessionCalendar) -> PointInTimeDataset:
    spec = WorldSpec(
        world_id="synth-v1-test-001",
        seed=20260818,
        kind="null",
        n_securities=24,
        n_sessions=160,
        rates=ActionRates(
            split=0.5,
            reverse_split=0.2,
            cash_dividend=1.0,
            stock_dividend=0.0,
            rename=0.5,
            merger=0.3,
            bankruptcy=0.2,
            voluntary_delisting=0.2,
            coverage_lapse=0.3,
            ipo_per_year=12,
        ),
    )
    world = generate_world(spec, static_calendar)
    snapshot = ingest_snapshot(
        world.payload, world.master, snapshot_id=spec.world_id, normalization_code_sha="0" * 64
    )
    return PointInTimeDataset(snapshot, static_calendar, universe_id="TRIAL-U")


@pytest.fixture()
def clean_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
        "HOME": str(tmp_path),
    }
    subprocess.run(["git", "init", "-q", str(repo)], check=True, env=env)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "--allow-empty", "-m", "init"],
        check=True,
        env=env,
    )
    return repo


def _run(
    static_calendar,
    protocol,
    tmp_path,
    repo,
    *,
    run_index=1,
    horizon=1,
    feature_names=FEATURES,
    model_family="ridge:v1",
    allow_dirty=False,
):  # type: ignore[no-untyped-def]
    dataset = _dataset(static_calendar)
    tmp_path.mkdir(parents=True, exist_ok=True)
    registry = TrialRegistry(tmp_path / "reg.sqlite")
    result = run_trial(
        dataset=dataset,
        calendar=static_calendar,
        protocol=protocol,
        world_id=dataset.snapshot_id,
        horizon_sessions=horizon,
        feature_names=feature_names,
        ridge_lambda=1.0 if model_family == "ridge:v1" else None,
        model_family=model_family,
        hypothesis="fixture-scale runner test: machinery only, no claim",
        decision_sessions=static_calendar.sessions()[:160],
        registry=registry,
        artifacts_dir=tmp_path / "artifacts",
        repo=repo,
        clock=_clock_factory(),
        run_index=run_index,
        split_override=SMALL_SPLIT,
        allow_dirty=allow_dirty,
    )
    return dataset, registry, result


def test_end_to_end_register_execute_stamp_complete(
    static_calendar, protocol, tmp_path, clean_repo
) -> None:  # type: ignore[no-untyped-def]
    dataset, registry, result = _run(static_calendar, protocol, tmp_path, clean_repo)

    assert registry.status(result.trial_id) == "COMPLETED"
    assert registry.has_outcome(result.trial_id)
    assert registry.metrics_uri(result.trial_id) == str(result.artifact_path)
    kinds = tuple(kind for kind, _ in registry.events(result.trial_id))
    # INV-13, mechanically: registration strictly precedes the outcome
    assert kinds.index("REGISTERED") < kinds.index("RUNNING") < kinds.index("COMPLETED")

    body = json.loads(result.artifact_path.read_text())
    stamp = body["stamp"]
    assert stamp["trial_id"] == result.trial_id
    assert stamp["dataset_manifest_hash"] == dataset.manifest_hash
    assert not stamp["git_sha"].startswith("dirty:")
    payload = body["payload"]
    assert payload["n_folds"] == result.n_folds > 0
    assert len(payload["per_fold"]) == payload["n_folds"]
    assert payload["pooled"]["n_rows"] == result.n_scored_rows > 0
    assert payload["fp_gate"]["total"] == payload["n_folds"]
    assert set(payload["feature_names"]) == set(FEATURES)
    backtest = payload["backtest"]
    assert backtest["dataset_provenance"] == "synthetic/v1"
    assert backtest["n_session_returns"] > 0
    assert len(backtest["per_fold"]) == payload["n_folds"]
    assert all(fold["terminal_equity"] for fold in backtest["per_fold"])
    # scope committed and canonical, world identity inside feature_set_id
    scope = TrialScope(**json.loads(registry.scope_json(result.trial_id)))
    assert dataset.snapshot_id in scope.feature_set_id
    assert TrialScope.is_canonical(scope.scope_key())


def test_duplicate_run_index_is_rejected(static_calendar, protocol, tmp_path, clean_repo) -> None:  # type: ignore[no-untyped-def]
    dataset = _dataset(static_calendar)
    registry = TrialRegistry(tmp_path / "reg.sqlite")
    common = dict(
        dataset=dataset,
        calendar=static_calendar,
        protocol=protocol,
        world_id=dataset.snapshot_id,
        horizon_sessions=1,
        feature_names=FEATURES,
        ridge_lambda=1.0,
        hypothesis="duplicate-id test",
        decision_sessions=static_calendar.sessions()[:160],
        registry=registry,
        artifacts_dir=tmp_path / "artifacts",
        repo=clean_repo,
        clock=_clock_factory(),
        split_override=SMALL_SPLIT,
    )
    run_trial(**common, run_index=1)  # type: ignore[arg-type]
    with pytest.raises(DuplicateTrialError):
        run_trial(**common, run_index=1)  # type: ignore[arg-type]
    # a NEW run index is a NEW trial (INV-13: re-running is not revising)
    second = run_trial(**common, run_index=2)  # type: ignore[arg-type]
    assert second.trial_id.endswith("-r2")
    second_scope = TrialScope(**json.loads(registry.scope_json(second.trial_id)))
    first_scope = TrialScope(
        **json.loads(registry.scope_json(f"m2p-{dataset.snapshot_id}-h1-ridge-r1"))
    )
    assert second_scope.scope_key() == first_scope.scope_key()
    # the 32-cap counts TRIALS PER SCOPE: both runs bill the same scope
    assert registry.count_scope(second_scope.scope_key()) == 2


def test_dirty_repo_refused_unless_explicit(
    static_calendar, protocol, tmp_path, clean_repo
) -> None:  # type: ignore[no-untyped-def]
    (clean_repo / "dirt.txt").write_text("x")
    with pytest.raises(DirtyWorktreeError):
        _run(static_calendar, protocol, tmp_path, clean_repo)
    # allow_dirty records the dirtiness in the sha itself
    _, _, result = _run(static_calendar, protocol, tmp_path, clean_repo, allow_dirty=True)
    body = json.loads(result.artifact_path.read_text())
    assert body["stamp"]["git_sha"].startswith("dirty:")


def test_no_folds_fails_before_registration(
    static_calendar, protocol, tmp_path, clean_repo
) -> None:  # type: ignore[no-untyped-def]
    from tree_options.trials import SplitOverride as SO

    dataset = _dataset(static_calendar)
    registry = TrialRegistry(tmp_path / "reg.sqlite")
    with pytest.raises(ValueError, match="no folds"):
        run_trial(
            dataset=dataset,
            calendar=static_calendar,
            protocol=protocol,
            world_id=dataset.snapshot_id,
            horizon_sessions=1,
            feature_names=FEATURES,
            ridge_lambda=1.0,
            hypothesis="no-fold geometry is refused",
            decision_sessions=static_calendar.sessions()[:30],
            registry=registry,
            artifacts_dir=tmp_path / "artifacts",
            repo=clean_repo,
            clock=_clock_factory(),
            split_override=SO(
                label_horizon_sessions=5,
                embargo_sessions=2,
                val_sessions=10,
                test_sessions=10,
                roll_sessions=10,
                min_train_sessions=20,
            ),
        )
    # nothing was registered: the refusal precedes the state machine
    assert not registry.is_registered(f"m2p-{dataset.snapshot_id}-h1-ridge-r1")


def test_artifact_write_failure_marks_trial_failed(
    static_calendar, protocol, tmp_path, clean_repo
) -> None:  # type: ignore[no-untyped-def]
    dataset = _dataset(static_calendar)
    registry = TrialRegistry(tmp_path / "reg.sqlite")
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("block mkdir")
    with pytest.raises(FileExistsError):
        run_trial(
            dataset=dataset,
            calendar=static_calendar,
            protocol=protocol,
            world_id=dataset.snapshot_id,
            horizon_sessions=1,
            feature_names=FEATURES,
            ridge_lambda=1.0,
            hypothesis="artifact failure must not leave a running trial",
            decision_sessions=static_calendar.sessions()[:160],
            registry=registry,
            artifacts_dir=blocked,
            repo=clean_repo,
            clock=_clock_factory(),
            split_override=SMALL_SPLIT,
        )
    trial_id = f"m2p-{dataset.snapshot_id}-h1-ridge-r1"
    assert registry.status(trial_id) == "FAILED"
    assert tuple(kind for kind, _ in registry.events(trial_id)) == (
        "REGISTERED",
        "RUNNING",
        "FAILED",
    )


def test_world_identity_mismatch_fails_before_registration(
    static_calendar, protocol, tmp_path, clean_repo
) -> None:  # type: ignore[no-untyped-def]
    dataset = _dataset(static_calendar)
    registry = TrialRegistry(tmp_path / "reg.sqlite")
    with pytest.raises(ValueError, match="does not match dataset snapshot"):
        run_trial(
            dataset=dataset,
            calendar=static_calendar,
            protocol=protocol,
            world_id="different-world",
            horizon_sessions=1,
            feature_names=FEATURES,
            ridge_lambda=1.0,
            hypothesis="world identity mismatch is refused",
            decision_sessions=static_calendar.sessions()[:160],
            registry=registry,
            artifacts_dir=tmp_path / "artifacts",
            repo=clean_repo,
            clock=_clock_factory(),
            split_override=SMALL_SPLIT,
        )
    assert not registry.is_registered("m2p-different-world-h1-ridge-r1")


def test_payload_is_deterministic(static_calendar, protocol, tmp_path, clean_repo) -> None:  # type: ignore[no-untyped-def]
    _, _, first = _run(static_calendar, protocol, tmp_path / "a", clean_repo)
    _, _, second = _run(static_calendar, protocol, tmp_path / "b", clean_repo)
    a = json.loads(first.artifact_path.read_text())["payload"]
    b = json.loads(second.artifact_path.read_text())["payload"]
    assert a == b


def test_univariate_mom1_baseline_scores_without_a_fitted_model(
    static_calendar, protocol, tmp_path, clean_repo
) -> None:  # type: ignore[no-untyped-def]
    _, registry, result = _run(
        static_calendar,
        protocol,
        tmp_path,
        clean_repo,
        feature_names=("mom_1",),
        model_family="univariate_ic:v1",
    )
    payload = json.loads(result.artifact_path.read_text())["payload"]
    assert payload["model_family"] == "univariate_ic:v1"
    assert payload["feature_names"] == ["mom_1"]
    assert all(fold["model_sha256"] is None for fold in payload["per_fold"])
    scope = TrialScope(**json.loads(registry.scope_json(result.trial_id)))
    assert scope.model_family == "univariate_ic:v1"


def test_dev_trial_configs_are_the_four_pre_registered_plan_entries() -> None:
    assert [config.config_id for config in DEV_TRIAL_CONFIGS] == ["D1", "D2", "D3", "D4"]
    assert [config.world_id for config in DEV_TRIAL_CONFIGS] == [
        "synth-v1-dev-alpha-104",
        "synth-v1-dev-null-103",
        "synth-v1-dev-alpha-104",
        "synth-v1-dev-alpha-104",
    ]
    assert [config.horizon_sessions for config in DEV_TRIAL_CONFIGS] == [1, 1, 5, 1]
    assert [config.model_family for config in DEV_TRIAL_CONFIGS] == [
        "ridge:v1",
        "ridge:v1",
        "ridge:v1",
        "univariate_ic:v1",
    ]
    assert DEV_TRIAL_CONFIGS[-1].feature_names == ("mom_1",)
