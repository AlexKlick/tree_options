"""Trial runner: register before outcome, execute, stamp, complete
(M2-proper §3.F — the INV-13/14 composition point and the deferred
criterion-7 dataset-lineage wiring).

One trial = one (world x horizon x feature set x model config) evaluated
walk-forward over protocol folds. The state machine is mechanical:

  1. build the stamp EARLY (clean-worktree rule; the provenance triple
     git_sha / config_hash / dataset_manifest_hash is identical at
     registration, at mark_running, and on the artifact — they come from
     the same stamp object);
  2. TrialScope carries world identity in feature_set_id;
     register() commits the scope (the 32-cap budget);
  3. mark_running() confirms the provenance triple;
  4. per fold: RidgePipeline fit on final_fit_train sessions only (the
     FittingGuard inside the pipeline enforces fit-once and fit/eval
     disjointness), scored on the fold's test block;
  5. metrics = per-fold t-statistics + pooled t + the exact-binomial
     false-positive assessment (evaluation.stats), written as a STAMPED
     artifact (write_artifact — no unstamped path exists);
  6. complete() claims the single outcome. Any execution failure runs
     through fail() first — a trial never ends in limbo.

Determinism: no randomness anywhere; the caller injects the clock and
the decision sessions. Production drivers load synthetic worlds through
scripts (the synth import boundary stays intact: tree_options.trials
imports no synth).
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from tree_options.data.authority import PointInTimeDataset
from tree_options.evaluation import (
    FalsePositiveAssessment,
    ScoredLabel,
    assess_false_positives,
    one_sample_t_statistic,
    per_session_rank_ics,
)
from tree_options.labels import build_labels
from tree_options.models import FitRow, ObsRow, RidgePipeline
from tree_options.protocol.loader import protocol_hash
from tree_options.protocol.schema import ResearchProtocol
from tree_options.protocol.stamping import build_stamp, write_artifact
from tree_options.registry.scope import TrialScope
from tree_options.registry.sqlite import TrialRegistry
from tree_options.schemas.trial import TrialRecord
from tree_options.splitting.splitter import Fold, WalkForwardSplitter
from tree_options.time.calendar import SessionCalendar

MODEL_FAMILY = "ridge:v1"
RUNNER_REVISION = "trials.run/v1"


@dataclass(frozen=True)
class SplitOverride:
    """Explicit fold geometry (fixture-scale runs); recorded in the config
    hash, so a deviation from protocol defaults is never silent."""

    label_horizon_sessions: int
    embargo_sessions: int
    val_sessions: int
    test_sessions: int
    roll_sessions: int
    min_train_sessions: int


@dataclass(frozen=True)
class TrialResult:
    trial_id: str
    artifact_path: Path
    n_folds: int
    n_scored_rows: int
    fold_t_statistics: tuple[float, ...]
    pooled_t: float | None
    fp_assessment: FalsePositiveAssessment


def _split_params(protocol: ResearchProtocol, override: SplitOverride | None) -> dict[str, int]:
    if override is not None:
        return {
            "label_horizon_sessions": override.label_horizon_sessions,
            "embargo_sessions": override.embargo_sessions,
            "val_sessions": override.val_sessions,
            "test_sessions": override.test_sessions,
            "roll_sessions": override.roll_sessions,
            "min_train_sessions": override.min_train_sessions,
        }
    f = protocol.folds
    return {
        "label_horizon_sessions": f.label_horizon_sessions,
        "embargo_sessions": f.embargo_sessions,
        "val_sessions": f.validation_window_sessions.default,
        "test_sessions": f.test_window_sessions.default,
        "roll_sessions": f.roll_forward_sessions,
        "min_train_sessions": f.min_train_sessions,
    }


def _row_material(
    dataset: PointInTimeDataset,
    calendar: SessionCalendar,
    *,
    feature_names: tuple[str, ...],
    label_values: dict[tuple[str, date], float],
    decision_sessions: Sequence[date],
) -> dict[date, tuple[FitRow, ...]]:
    """Per decision session: the join of the feature panel with the label
    (rows missing any feature or the label are absent, never imputed)."""
    rows_by_session: dict[date, tuple[FitRow, ...]] = {}
    wanted = set(feature_names)
    for session in decision_sessions:
        decision_at = calendar.session_close(session)
        panel = dataset.features_as_of(
            decision_at=decision_at,
            universe_id=dataset.universe_id,
            dataset_snapshot_id=dataset.snapshot_id,
        )
        rows: list[FitRow] = []
        for row in panel:
            label = label_values.get((row.security_id, session))
            if label is None:
                continue
            values = {f.feature_name: f.value for f in row.features}
            if not wanted <= values.keys():
                continue
            rows.append(
                FitRow(
                    session=session,
                    security_id=row.security_id,
                    features={name: values[name] for name in feature_names},
                    label=label,
                )
            )
        rows_by_session[session] = tuple(rows)
    return rows_by_session


def run_trial(
    *,
    dataset: PointInTimeDataset,
    calendar: SessionCalendar,
    protocol: ResearchProtocol,
    world_id: str,
    horizon_sessions: int,
    feature_names: tuple[str, ...],
    ridge_lambda: float,
    hypothesis: str,
    decision_sessions: Sequence[date],
    registry: TrialRegistry,
    artifacts_dir: Path,
    repo: Path,
    clock: Callable[[], datetime],
    run_index: int = 1,
    split_override: SplitOverride | None = None,
    allow_dirty: bool = False,
) -> TrialResult:
    """Register, execute, stamp, and complete one walk-forward trial."""
    if world_id != dataset.snapshot_id:
        raise ValueError(
            f"world_id {world_id!r} does not match dataset snapshot {dataset.snapshot_id!r}"
        )
    if horizon_sessions not in {1, 5}:
        raise ValueError("horizon_sessions must be 1 or 5")
    if not feature_names or len(set(feature_names)) != len(feature_names):
        raise ValueError("feature_names must be non-empty and unique")
    if not math.isfinite(ridge_lambda) or ridge_lambda < 0:
        raise ValueError("ridge_lambda must be finite and >= 0")
    normalized_sessions = tuple(decision_sessions)
    if not normalized_sessions or tuple(sorted(set(normalized_sessions))) != normalized_sessions:
        raise ValueError("decision_sessions must be non-empty, unique, and strictly increasing")
    decision_sessions_sha256 = hashlib.sha256(
        json.dumps(
            [session.isoformat() for session in normalized_sessions],
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    split = _split_params(protocol, split_override)
    config: dict[str, object] = {
        "runner": RUNNER_REVISION,
        "world_id": world_id,
        "horizon_sessions": horizon_sessions,
        "feature_names": list(feature_names),
        "ridge_lambda": ridge_lambda,
        "model_family": MODEL_FAMILY,
        "split": split,
        "decision_sessions_sha256": decision_sessions_sha256,
        "decision_session_count": len(normalized_sessions),
        "run_index": run_index,
    }
    trial_id = f"m2p-{world_id}-h{horizon_sessions}-ridge-r{run_index}"
    scope = TrialScope(
        protocol_id="tree_options",
        protocol_hash=protocol_hash(protocol),
        outer_fold_id=world_id,
        target_horizon=f"h{horizon_sessions}",
        feature_set_id=f"{world_id}|{'+'.join(feature_names)}|fv2",
        model_family=MODEL_FAMILY,
    )

    splitter = WalkForwardSplitter(
        calendar,
        label_horizon_sessions=split["label_horizon_sessions"],
        embargo_sessions=split["embargo_sessions"],
        val_sessions=split["val_sessions"],
        test_sessions=split["test_sessions"],
        roll_sessions=split["roll_sessions"],
        min_train_sessions=split["min_train_sessions"],
    )
    folds = splitter.splits(normalized_sessions)
    # The splitter walks the FULL calendar; keep only folds whose test
    # blocks lie entirely inside the world's session range (a fold that
    # runs past the world cannot be evaluated and is not half-counted).
    world_sessions = frozenset(normalized_sessions)
    folds = [fold for fold in folds if fold.test_sessions <= world_sessions]
    if not folds:
        raise ValueError(
            f"no folds for {world_id}: {len(normalized_sessions)} decision sessions "
            f"cannot carry the requested geometry {split}"
        )

    stamp = build_stamp(
        protocol,
        trial_id=trial_id,
        config=config,
        dataset_manifest_hash=dataset.manifest_hash,
        repo=repo,
        allow_dirty=allow_dirty,
    )

    test_all = sorted({s for fold in folds for s in fold.test_sessions})
    train_all = sorted({s for fold in folds for s in fold.train_sessions})
    record = TrialRecord(
        trial_id=trial_id,
        created_at=clock(),
        hypothesis=hypothesis,
        git_sha=stamp.git_sha,
        config_hash=stamp.config_hash,
        dataset_manifest_hash=stamp.dataset_manifest_hash,
        train_window=(train_all[0], train_all[-1]),
        test_window=(test_all[0], test_all[-1]),
        hyperparameters={
            "ridge_lambda": ridge_lambda,
            "feature_names": list(feature_names),
            "horizon_sessions": horizon_sessions,
            "split": split,
            "decision_sessions_sha256": decision_sessions_sha256,
            "decision_session_count": len(normalized_sessions),
        },
        scope_key=scope.scope_key(),
    )
    registry.register(record, scope)
    registry.mark_running(
        trial_id,
        git_sha=stamp.git_sha,
        config_hash=stamp.config_hash,
        dataset_manifest_hash=stamp.dataset_manifest_hash,
        at=clock(),
    )

    try:
        payload, result_stats = _execute(
            dataset=dataset,
            calendar=calendar,
            folds=folds,
            decision_sessions=normalized_sessions,
            trial_id=trial_id,
            feature_names=feature_names,
            ridge_lambda=ridge_lambda,
            horizon_sessions=horizon_sessions,
            world_id=world_id,
        )
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        artifact_path = artifacts_dir / f"{trial_id}.json"
        write_artifact(artifact_path, payload, stamp)
        registry.complete(trial_id, metrics_uri=str(artifact_path), outcome_at=clock())
    except Exception as exc:  # a trial never ends in limbo
        if registry.status(trial_id) == "RUNNING":
            registry.fail(trial_id, f"{type(exc).__name__}: {exc}", at=clock())
        raise

    return TrialResult(
        trial_id=trial_id,
        artifact_path=artifact_path,
        n_folds=payload["n_folds"],  # type: ignore[arg-type]
        n_scored_rows=payload["pooled"]["n_rows"],  # type: ignore[index]
        fold_t_statistics=result_stats.fold_t_statistics,
        pooled_t=payload["pooled"]["t_stat"],  # type: ignore[index]
        fp_assessment=result_stats.fp_assessment,
    )


@dataclass(frozen=True)
class _ExecutionStats:
    fold_t_statistics: tuple[float, ...]
    fp_assessment: FalsePositiveAssessment


def _execute(
    *,
    dataset: PointInTimeDataset,
    calendar: SessionCalendar,
    folds: list[Fold],
    decision_sessions: Sequence[date],
    trial_id: str,
    feature_names: tuple[str, ...],
    ridge_lambda: float,
    horizon_sessions: int,
    world_id: str,
) -> tuple[dict[str, object], _ExecutionStats]:
    labels = build_labels(
        dataset,
        calendar,
        horizon_sessions=horizon_sessions,
        decision_sessions=decision_sessions,
    )
    label_values = {(lab.security_id, lab.decision_session): lab.value for lab in labels}
    rows_by_session = _row_material(
        dataset,
        calendar,
        feature_names=feature_names,
        label_values=label_values,
        decision_sessions=decision_sessions,
    )

    per_fold: list[dict[str, object]] = []
    fold_t_statistics: list[float] = []
    scored: list[ScoredLabel] = []
    for fold in folds:
        fit_rows = [
            row for s in sorted(fold.final_fit_train_sessions) for row in rows_by_session.get(s, ())
        ]
        fit_sessions = frozenset(s for s in fold.final_fit_train_sessions if rows_by_session.get(s))
        pipe = RidgePipeline(
            name=f"{trial_id}/fold-{fold.fold_id:03d}",
            feature_names=feature_names,
            ridge_lambda=ridge_lambda,
        )
        pipe.fit(fit_rows, fit_sessions=fit_sessions)
        model_sha256 = hashlib.sha256(pipe.artifact_bytes()).hexdigest()

        obs = [
            ObsRow(session=row.session, security_id=row.security_id, features=row.features)
            for s in sorted(fold.test_sessions)
            for row in rows_by_session.get(s, ())
        ]
        targets = frozenset(s for s in fold.test_sessions if rows_by_session.get(s))
        scores = pipe.score(obs, target_sessions=targets)
        fold_scored = [
            ScoredLabel(
                security_id=row.security_id,
                session=row.session,
                score=row.score,
                label=label_values[(row.security_id, row.session)],
            )
            for row in scores
        ]
        scored.extend(fold_scored)
        ics = per_session_rank_ics(fold_scored)
        t_stat = one_sample_t_statistic([entry.ic for entry in ics])
        if t_stat is None:
            raise ValueError(f"fold {fold.fold_id}: no evaluable per-session ICs on {world_id}")
        fold_t_statistics.append(t_stat)
        per_fold.append(
            {
                "fold_id": fold.fold_id,
                "n_test_sessions": len(ics),
                "n_rows_scored": len(fold_scored),
                "mean_ic": sum(entry.ic for entry in ics) / len(ics),
                "t_stat": t_stat,
                "model_sha256": model_sha256,
            }
        )

    pooled_ics = per_session_rank_ics(scored)
    pooled_t = one_sample_t_statistic([entry.ic for entry in pooled_ics])
    fold_t_values = tuple(fold_t_statistics)
    assessment = assess_false_positives(list(fold_t_values))
    payload: dict[str, object] = {
        "runner": RUNNER_REVISION,
        "world_id": world_id,
        "horizon_sessions": horizon_sessions,
        "model_family": MODEL_FAMILY,
        "feature_names": list(feature_names),
        "n_folds": len(folds),
        "per_fold": per_fold,
        "pooled": {
            "n_sessions": len(pooled_ics),
            "n_rows": len(scored),
            "mean_ic": (
                sum(entry.ic for entry in pooled_ics) / len(pooled_ics) if pooled_ics else None
            ),
            "t_stat": pooled_t,
        },
        "fp_gate": {
            "critical_abs_t": 1.96,
            "total": assessment.total,
            "rejections": assessment.rejections,
            "observed_rate": assessment.observed_rate,
            "max_allowed_rejections": assessment.max_allowed_rejections,
            "exact_upper_tail_probability": assessment.exact_upper_tail_probability,
            "passes": assessment.passes,
        },
    }
    return payload, _ExecutionStats(fold_t_statistics=fold_t_values, fp_assessment=assessment)
