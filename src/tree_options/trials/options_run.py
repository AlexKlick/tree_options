"""Options trial runner (M3 plan §3.F): register before outcome, execute,
stamp, complete — the INV-13/14 discipline mirroring trials/run.py.

One options trial = one (world x arm x strategy config) over protocol
walk-forward folds. The signal is an UNDERLYING-level scored cross-section
(the caller supplies it — the runner is model-agnostic; the model family
and its artifact hash ride in the config). Per fold the options backtest
runs with fresh cash; the payload carries per-position rows and a per-fill
log so the sealed gate's criteria 1-7 are evaluable from stamped artifacts
alone.

The 7200 s quote-age override (owner ruling 4) is an EXPLICIT config key —
hashed into every stamp — while research_protocol.yaml stays byte-frozen.

OD1/OD2/OD3 statistics stamped in the payload (all computed on the
direction-aligned SIGNED premium return — call return as-is, put return
sign-flipped — so a mixed long-call/long-put book pools against one label
direction; the first dev run's mixed-pool rho ~ 0 was exactly this sign
cancellation, caught by the §7 tripwire):
- fidelity factor rho = Spearman(signed premium return, H5 label) over
  closed positions (the vehicle transmits underlying exposure);
- per-cohort IC = Spearman(score, signed premium return) over positions
  entered on one session; the PRIMARY estimator uses DISJOINT cohorts
  (every 4th session, plan §7) and the payload stamps both the all-cohort
  series and the stride-4 subset with sd, autocorrelation, and t;
- per-session ICs (SECONDARY, reported only);
- OD2 machinery oracles: the OptionsCounters + CandidateAudit aggregates.

Determinism: no randomness; the caller injects the clock; tree_options
imports no synth here (the driver owns world construction).
"""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from tree_options.backtest.options import Arm, OptionsBacktestResult, run_options_backtest
from tree_options.candidates.filters import CandidateFilter
from tree_options.data.authority import PointInTimeDataset
from tree_options.data.options_pit import OptionPitSurface
from tree_options.evaluation.stats import ScoredLabel, backtest_summary
from tree_options.options import OptionSignal, OptionsStrategyConfig
from tree_options.protocol.loader import protocol_hash
from tree_options.protocol.schema import ResearchProtocol
from tree_options.protocol.stamping import build_stamp, write_artifact
from tree_options.registry.scope import TrialScope
from tree_options.registry.sqlite import TrialRegistry
from tree_options.schemas.trial import TrialRecord
from tree_options.splitting.splitter import Fold, WalkForwardSplitter
from tree_options.time.calendar import SessionCalendar

RUNNER_REVISION = "trials.options_run/v1"
OPTIONS_BACKTEST_INITIAL_CASH = Decimal("1000000.00")
DECLARED_MAX_QUOTE_AGE_SECONDS = 7200  # owner ruling 4 — a config key, hashed
COHORT_STRIDE = 4  # plan §7: disjoint entry cohorts every 4th session
END_BUFFER_SESSIONS = 6  # let arm-A exits land inside the evaluated window


@dataclass(frozen=True)
class _ODStats:
    n_folds: int
    n_positions: int
    fidelity_rho: float | None
    stride4_cohort_ic_mean: float | None
    stride4_cohort_ic_sd: float | None
    stride4_cohort_t: float | None


@dataclass(frozen=True)
class OptionsTrialResult:
    trial_id: str
    artifact_path: Path
    n_folds: int
    n_positions: int
    fidelity_rho: float | None
    cohort_ic_mean: float | None
    cohort_ic_sd: float | None
    cohort_t: float | None


def _spearman(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    """Spearman rank correlation with average-tie ranks (pooled, exact)."""
    n = len(xs)
    if n < 3 or n != len(ys):
        return None

    def ranks(values: Sequence[float]) -> list[float]:
        order = sorted(range(n), key=lambda i: values[i])
        out = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and values[order[j + 1]] == values[order[i]]:
                j += 1
            average = (i + j) / 2 + 1  # average of ranks i+1 .. j+1
            for k in range(i, j + 1):
                out[order[k]] = average
            i = j + 1
        return out

    rx, ry = ranks(xs), ranks(ys)
    mean_x = sum(rx) / n
    mean_y = sum(ry) / n
    cov = sum((a - mean_x) * (b - mean_y) for a, b in zip(rx, ry, strict=True))
    var_x = sum((a - mean_x) ** 2 for a in rx)
    var_y = sum((b - mean_y) ** 2 for b in ry)
    if var_x <= 0 or var_y <= 0:
        return None
    return cov / math.sqrt(var_x * var_y)


def _lag1_autocorrelation(xs: Sequence[float]) -> float | None:
    n = len(xs)
    if n < 3:
        return None
    mean = sum(xs) / n
    num = sum((xs[i] - mean) * (xs[i + 1] - mean) for i in range(n - 1))
    den = sum((x - mean) ** 2 for x in xs)
    if den <= 0:
        return None
    return num / den


def _t_statistic(values: Sequence[float]) -> float | None:
    n = len(values)
    if n < 2:
        return None
    mean = sum(values) / n
    if n < 3:
        return None
    sd = statistics.stdev(values)
    if sd <= 0:
        return None
    return mean * math.sqrt(n) / sd


def _cohort_series(
    closed: list[dict[str, object]],
) -> tuple[list[tuple[str, float]], list[float], list[int]]:
    """Per-entry-session cohort ICs and the stride-4 disjoint series.

    The stride selects every COHORT_STRIDE-th session of the FULL
    entry-session grid — the predeclared every-fourth-session statistic
    (review r3 P1-2) — and keeps only defined ICs among those grid points:
    an undefined cohort IC (fewer than 3 positions) drops its POINT
    without shifting later ICs onto earlier sessions and without
    advancing which sessions the grid selects.
    """
    by_entry: dict[str, list[dict[str, object]]] = {}
    for p in closed:
        by_entry.setdefault(str(p["entry_session"]), []).append(p)
    cohort_ics: list[float] = []
    cohort_counts: list[int] = []
    stride4: list[tuple[str, float]] = []
    for index, session in enumerate(sorted(by_entry)):
        rows = by_entry[session]
        ic = _spearman(
            [float(p["score"]) for p in rows],  # type: ignore[arg-type]
            [float(p["signed_premium_return"]) for p in rows],  # type: ignore[arg-type]
        )
        if ic is None:
            continue
        cohort_ics.append(ic)
        cohort_counts.append(len(rows))
        if index % COHORT_STRIDE == 0:
            stride4.append((session, ic))
    return stride4, cohort_ics, cohort_counts


def _position_payloads(result: OptionsBacktestResult) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for p in result.positions:
        rows.append(
            {
                "underlying_security_id": p.underlying_security_id,
                "call_put": p.call_put,
                "score": p.score,
                "label": p.label,
                "entry_session": p.entry_session.isoformat(),
                "entry_price": str(p.entry_price),
                "contract_expiration": p.contract_expiration.isoformat(),
                "exit_kind": p.exit_kind,
                "exit_session": p.exit_session.isoformat() if p.exit_session else None,
                "exit_price": str(p.exit_price) if p.exit_price is not None else None,
                "premium_return": p.premium_return,
                # direction-aligned return: the premium return a LONG CALL on
                # the underlying would have earned — puts are sign-flipped so
                # the vehicle statistics pool both sides against the same
                # label direction (OD1's mixed-pool rho ~ 0 was exactly this
                # sign cancellation; found by the §7 tripwire, not tuned)
                "signed_premium_return": (
                    p.premium_return
                    if (p.call_put == "C" or p.premium_return is None)
                    else -p.premium_return
                ),
            }
        )
    return rows


def _fill_log(result: OptionsBacktestResult) -> list[dict[str, object]]:
    audit_by_fill = {a.fill_id: a for a in result.fill_audit}
    rows: list[dict[str, object]] = []
    for f in result.fills:
        audit = audit_by_fill.get(f.fill_id)
        rows.append(
            {
                "fill_id": f.fill_id,
                "contract_id": f.contract_id,
                "side": f.side,
                "quantity": f.quantity,
                "price": str(f.price),
                "execution_at": f.execution_at.isoformat(),
                "execution_session": f.execution_session.isoformat(),
                # criterion 2 (sealed gate): T+1 discipline + quote receipt,
                # re-derivable from the stamped artifact alone
                "decision_session": (audit.decision_session.isoformat() if audit else None),
                "decision_at": audit.decision_at.isoformat() if audit else None,
                "quote_received_at": (audit.quote_received_at.isoformat() if audit else None),
            }
        )
    return rows


@dataclass(frozen=True)
class OptionsSplitOverride:
    """Explicit fold geometry (fixture-scale runs); recorded in the config
    hash, so a deviation from protocol defaults is never silent."""

    label_horizon_sessions: int
    embargo_sessions: int
    val_sessions: int
    test_sessions: int
    roll_sessions: int
    min_train_sessions: int


def _split_params(
    protocol: ResearchProtocol, override: OptionsSplitOverride | None
) -> dict[str, int]:
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


def _fold_backtest(
    *,
    fold_scored: Sequence[ScoredLabel],
    calendar: SessionCalendar,
    surface: OptionPitSurface,
    dataset: PointInTimeDataset,
    candidate_filter: CandidateFilter,
    config: OptionsStrategyConfig,
    arm: Arm,
    world_last_session: date,
) -> OptionsBacktestResult:
    last_execution = calendar.nth_after(max(row.session for row in fold_scored), 1)
    buffered = calendar.nth_after(last_execution, END_BUFFER_SESSIONS)
    end_session = (
        buffered
        if calendar.ordinal(buffered) <= calendar.ordinal(world_last_session)
        else world_last_session
    )
    return run_options_backtest(
        calendar=calendar,
        surface=surface,
        dataset=dataset,
        candidate_filter=candidate_filter,
        signals=[
            OptionSignal(
                decision_session=row.session,
                security_id=row.security_id,
                score=row.score,
                label=row.label,
            )
            for row in fold_scored
        ],
        initial_cash=OPTIONS_BACKTEST_INITIAL_CASH,
        config=config,
        arm=arm,
        end_session=end_session,
    )


def _volume_flow_threshold(protocol: ResearchProtocol, override: int | None) -> int:
    """The lane-2 flow threshold: the explicit hashed override when given,
    else the protocol's ratified value. Validated by the filter's own rule
    (int >= 1, never a bool) BEFORE registration, so an illegal deviation
    refuses loudly instead of failing a registered trial."""
    lf = protocol.option_candidate_defaults.liquidity_volume_flow
    if override is not None:
        if not isinstance(override, int) or isinstance(override, bool) or override < 1:
            raise ValueError(f"flow_min_session_volume must be an int >= 1, got {override!r}")
        return override
    if lf is None:
        raise ValueError(
            "protocol carries no liquidity_volume_flow block: the volume-flow"
            " regime is not ratified"
        )
    value = lf.flow_min_session_volume
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(
            "the protocol's flow_min_session_volume is PENDING-era — supply the"
            " explicit flow_min_session_volume config key to run lane 2"
        )
    return value


def run_options_trial(
    *,
    dataset: PointInTimeDataset,
    surface: OptionPitSurface,
    calendar: SessionCalendar,
    protocol: ResearchProtocol,
    world_id: str,
    arm: Arm,
    strategy_config: OptionsStrategyConfig,
    scored: Sequence[ScoredLabel],
    model_family: str,
    model_sha256: str | None,
    hypothesis: str,
    decision_sessions: Sequence[date],
    options_manifest_hash: str,
    registry: TrialRegistry,
    artifacts_dir: Path,
    repo: Path,
    clock: Callable[[], datetime],
    run_index: int = 1,
    split_override: OptionsSplitOverride | None = None,
    liquidity_lane: int = 1,
    flow_min_session_volume: int | None = None,
    score_seed: str | None = None,
    allow_dirty: bool = False,
) -> OptionsTrialResult:
    """Register, execute, stamp, and complete one options trial.

    `liquidity_lane` selects the candidate-filter regime (G2): 1 keeps the
    two-sided `CandidateFilter.from_protocol` (byte-identical behavior);
    2 selects `from_protocol_volume_flow` for the Massive derived (vwap)
    lane. `flow_min_session_volume` is that regime's EXPLICIT hashed config
    key — default the protocol's ratified value (100), every deviation
    stamped into the config hash. Neither key perturbs a lane-1 trial."""
    if world_id != dataset.snapshot_id or world_id != surface.snapshot_id:
        raise ValueError(
            f"world_id {world_id!r} must match dataset {dataset.snapshot_id!r} "
            f"and surface {surface.snapshot_id!r}"
        )
    if arm not in ("A", "B"):
        raise ValueError(f"arm must be A or B, got {arm!r}")
    if liquidity_lane not in (1, 2):
        raise ValueError(f"liquidity_lane must be 1 or 2, got {liquidity_lane!r}")
    if liquidity_lane == 1 and flow_min_session_volume is not None:
        raise ValueError(
            "flow_min_session_volume is a lane-2 config key; lane 1 keeps the"
            " two-sided regime untouched"
        )
    flow_threshold = (
        _volume_flow_threshold(protocol, flow_min_session_volume) if liquidity_lane == 2 else None
    )
    if score_seed is not None and not score_seed:
        raise ValueError("score_seed must be a non-empty string when supplied")
    if not scored:
        raise ValueError("scored rows are required")
    normalized_sessions = tuple(decision_sessions)
    if not normalized_sessions or tuple(sorted(set(normalized_sessions))) != normalized_sessions:
        raise ValueError("decision_sessions must be non-empty, unique, and strictly increasing")
    scored_keys = {(row.session, row.security_id) for row in scored}
    if len(scored_keys) != len(scored):
        raise ValueError("duplicate (session, security_id) in scored rows")
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
        "arm": arm,
        "strategy": {
            **asdict(strategy_config),
            # Decimals are not JSON-native: string form is the hashed canon
            "target_abs_delta": str(strategy_config.target_abs_delta),
            "abs_delta_min": str(strategy_config.abs_delta_min),
            "abs_delta_max": str(strategy_config.abs_delta_max),
            "premium_budget_fraction": str(strategy_config.premium_budget_fraction),
        },
        "max_quote_age_seconds": DECLARED_MAX_QUOTE_AGE_SECONDS,  # owner ruling 4
        **(
            {
                # G2: the lane-2 regime and its effective flow threshold are
                # config keys — a deviation from the two-sided default (or
                # from the protocol's ratified threshold) rides the hash
                "liquidity_lane": liquidity_lane,
                "flow_min_session_volume": flow_threshold,
            }
            if liquidity_lane != 1
            else {}
        ),
        # G5: the null-score generator's REQUIRED seed is a first-class
        # config key — the declared score model's input rides the hash
        **({"score_seed": score_seed} if score_seed is not None else {}),
        "model_family": model_family,
        "model_sha256": model_sha256,
        "split": split,
        "decision_sessions_sha256": decision_sessions_sha256,
        "decision_session_count": len(normalized_sessions),
        "initial_cash_per_fold": str(OPTIONS_BACKTEST_INITIAL_CASH),
        "cohort_stride": COHORT_STRIDE,
        "options_manifest_hash": options_manifest_hash,
        "run_index": run_index,
    }
    trial_id = f"m3-{world_id}-{arm.lower()}-r{run_index}"
    scope = TrialScope(
        protocol_id="tree_options",
        protocol_hash=protocol_hash(protocol),
        outer_fold_id=world_id,
        target_horizon="h5",
        feature_set_id=f"{world_id}|options|ov1",
        model_family=f"options_{arm}:v1",
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
    world_sessions_set = frozenset(normalized_sessions)
    folds = [
        fold
        for fold in splitter.splits(normalized_sessions)
        if fold.test_sessions <= world_sessions_set
    ]
    if not folds:
        raise ValueError(
            f"no folds for {world_id}: {len(normalized_sessions)} decision sessions "
            f"cannot carry the requested geometry {split}"
        )

    stamp = build_stamp(
        protocol,
        trial_id=trial_id,
        config=config,
        dataset_manifest_hash=options_manifest_hash,
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
            "arm": arm,
            "strategy": config["strategy"],
            "max_quote_age_seconds": DECLARED_MAX_QUOTE_AGE_SECONDS,
            **(
                {"liquidity_lane": liquidity_lane, "flow_min_session_volume": flow_threshold}
                if liquidity_lane != 1
                else {}
            ),
            **({"score_seed": score_seed} if score_seed is not None else {}),
            "model_family": model_family,
            "model_sha256": model_sha256,
            "split": split,
            "decision_sessions_sha256": decision_sessions_sha256,
            "cohort_stride": COHORT_STRIDE,
            "options_manifest_hash": options_manifest_hash,
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
        payload, stats = _execute(
            dataset=dataset,
            surface=surface,
            calendar=calendar,
            protocol=protocol,
            folds=folds,
            scored=scored,
            arm=arm,
            strategy_config=strategy_config,
            trial_id=trial_id,
            model_family=model_family,
            world_id=world_id,
            liquidity_lane=liquidity_lane,
            flow_min_session_volume=flow_threshold,
            score_seed=score_seed,
        )
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        artifact_path = artifacts_dir / f"{trial_id}.json"
        write_artifact(artifact_path, payload, stamp)
        registry.complete(trial_id, metrics_uri=str(artifact_path), outcome_at=clock())
    except Exception as exc:  # a trial never ends in limbo
        if registry.status(trial_id) == "RUNNING":
            registry.fail(trial_id, f"{type(exc).__name__}: {exc}", at=clock())
        raise

    return OptionsTrialResult(
        trial_id=trial_id,
        artifact_path=artifact_path,
        n_folds=stats.n_folds,
        n_positions=stats.n_positions,
        fidelity_rho=stats.fidelity_rho,
        cohort_ic_mean=stats.stride4_cohort_ic_mean,
        cohort_ic_sd=stats.stride4_cohort_ic_sd,
        cohort_t=stats.stride4_cohort_t,
    )


def _execute(
    *,
    dataset: PointInTimeDataset,
    surface: OptionPitSurface,
    calendar: SessionCalendar,
    protocol: ResearchProtocol,
    folds: list[Fold],
    scored: Sequence[ScoredLabel],
    arm: Arm,
    strategy_config: OptionsStrategyConfig,
    trial_id: str,
    model_family: str,
    world_id: str,
    liquidity_lane: int = 1,
    flow_min_session_volume: int | None = None,
    score_seed: str | None = None,
) -> tuple[dict[str, object], _ODStats]:
    if liquidity_lane == 2:
        # G2: the Massive derived (vwap) lane's ratified regime — OI and the
        # spread dropped with disclosure, the session's traded contracts as
        # the liquidity term, model-derived |delta| accepted
        candidate_filter = CandidateFilter.from_protocol_volume_flow(
            calendar, protocol, flow_min_session_volume=flow_min_session_volume
        )
    else:
        candidate_filter = CandidateFilter.from_protocol(calendar, protocol)
    world_last_session = max(bar.session for bar in dataset.bars)
    scored_by_session: dict[date, list[ScoredLabel]] = {}
    for row in scored:
        scored_by_session.setdefault(row.session, []).append(row)

    per_fold: list[dict[str, object]] = []
    all_positions: list[dict[str, object]] = []
    all_fills: list[dict[str, object]] = []
    backtest_returns: list[float] = []
    backtest_turnovers: list[float] = []
    backtest_hits: list[bool] = []
    aggregated_counters: dict[str, int] = {}
    aggregated_rejections: dict[str, dict[str, int]] = {}
    rule_histogram: dict[str, dict[str, int]] = {}

    for fold in folds:
        fold_scored = [
            row
            for session in sorted(fold.test_sessions)
            for row in scored_by_session.get(session, ())
        ]
        result = _fold_backtest(
            fold_scored=fold_scored,
            calendar=calendar,
            surface=surface,
            dataset=dataset,
            candidate_filter=candidate_filter,
            config=strategy_config,
            arm=arm,
            world_last_session=world_last_session,
        )
        per_fold.append(
            {
                "fold_id": fold.fold_id,
                "n_test_sessions": len(fold.test_sessions),
                "n_positions": len(result.positions),
                "n_fills": len(result.fills),
                "fills_buy": sum(1 for f in result.fills if f.side == "buy"),
                "fills_sell": sum(1 for f in result.fills if f.side == "sell"),
                "n_sessions_evaluated": len(result.sessions),
                "conservation_checks": result.counters.conservation_checks,
                "force_closes": result.counters.force_closes,
                "early_exercises": result.counters.early_exercises,
                "expiries": result.counters.expiries,
                "terminals": result.counters.terminals,
                "total_return": result.summary.total_return,
                # G3: the fold's own session-return series and equity
                # endpoints — computed today but discarded; stamped so the
                # artifact alone re-derives every fold statistic
                "session_returns": list(result.summary.session_returns),
                "equity_start": (str(result.equities[0]) if result.equities else None),
                "equity_end": (str(result.equities[-1]) if result.equities else None),
            }
        )
        all_positions.extend(_position_payloads(result))
        all_fills.extend(_fill_log(result))
        backtest_returns.extend(result.summary.session_returns)
        backtest_turnovers.extend(result.turnovers)
        backtest_hits.extend(result.label_hits)
        counters = result.counters
        for key in (
            "not_evaluable_candidates",
            "failed_candidates",
            "no_in_band_expiry",
            "no_in_band_strike",
            "excluded_pending_action",
            "entries_cancelled",
            "entries_skipped_open",
            "force_closes",
            "early_exercises",
            "expiries",
            "terminals",
            "exit_retries",
            "mark_misses",
            "conservation_checks",
        ):
            aggregated_counters[key] = aggregated_counters.get(key, 0) + getattr(counters, key)
        for bucket_name in (
            "entry_fill_rejections",
            "exit_fill_rejections",
            "force_close_rejections",
        ):
            merged = aggregated_rejections.setdefault(bucket_name, {})
            for code, n in getattr(counters, bucket_name).items():
                merged[code] = merged.get(code, 0) + n
        for (rule, status), n in counters.rule_histogram.items():
            rule_histogram.setdefault(rule, {}).setdefault(status, 0)
            rule_histogram[rule][status] += n

    # ---- OD statistics from the stamped per-position rows ------------------
    closed = [
        p for p in all_positions if p["exit_kind"] is not None and p["premium_return"] is not None
    ]
    fidelity_pairs = [
        (float(p["signed_premium_return"]), float(p["label"]))  # type: ignore[arg-type]
        for p in closed
        if p["label"] is not None
    ]
    fidelity_rho = _spearman([a for a, _ in fidelity_pairs], [b for _, b in fidelity_pairs])

    stride4, cohort_ics, cohort_counts = _cohort_series(closed)
    stride4_ics = [ic for _, ic in stride4]
    stride4_sd = statistics.stdev(stride4_ics) if len(stride4_ics) >= 2 else None
    stride4_t = _t_statistic(stride4_ics)
    autocorr = _lag1_autocorrelation(cohort_ics)

    aggregate = backtest_summary(backtest_returns, backtest_turnovers, backtest_hits)
    stats = _ODStats(
        n_folds=len(folds),
        n_positions=len(all_positions),
        fidelity_rho=fidelity_rho,
        stride4_cohort_ic_mean=(sum(stride4_ics) / len(stride4_ics) if stride4_ics else None),
        stride4_cohort_ic_sd=stride4_sd,
        stride4_cohort_t=stride4_t,
    )
    payload: dict[str, object] = {
        "runner": RUNNER_REVISION,
        "world_id": world_id,
        "arm": arm,
        "model_family": model_family,
        "max_quote_age_seconds": DECLARED_MAX_QUOTE_AGE_SECONDS,
        "cohort_stride": COHORT_STRIDE,
        "n_folds": len(folds),
        "world_last_session": world_last_session.isoformat(),
        "per_fold": per_fold,
        "pooled": {
            "n_positions": len(all_positions),
            "n_closed": len(closed),
            "fidelity_rho": fidelity_rho,
            "fidelity_n": len(fidelity_pairs),
            "n_cohorts": len(cohort_ics),
            "mean_cohort_positions": (
                sum(cohort_counts) / len(cohort_counts) if cohort_counts else None
            ),
            "cohort_ic_mean": (sum(cohort_ics) / len(cohort_ics) if cohort_ics else None),
            "cohort_ic_sd": statistics.stdev(cohort_ics) if len(cohort_ics) >= 2 else None,
            "cohort_ic_autocorr_lag1": autocorr,
            "n_stride4_cohorts": len(stride4_ics),
            "stride4_cohort_ic_mean": (
                sum(stride4_ics) / len(stride4_ics) if stride4_ics else None
            ),
            "stride4_cohort_ic_sd": stride4_sd,
            "stride4_cohort_t": stride4_t,
            # the raw series ride in the stamp so the sealed gate re-derives
            # t (and the two-world pooled t, criterion 7) from the artifact
            # alone instead of trusting a pre-computed scalar
            "stride4_cohort_ics": stride4_ics,
            "cohort_ics": cohort_ics,
            "positions": all_positions,
        },
        "fills_log": all_fills,
        "counters": {
            **aggregated_counters,
            "rejections": aggregated_rejections,
            "rule_histogram": rule_histogram,
        },
        "backtest": {
            "dataset_provenance": "synthetic/v1",
            "initial_cash_per_fold": str(OPTIONS_BACKTEST_INITIAL_CASH),
            "n_session_returns": len(aggregate.session_returns),
            "total_return": aggregate.total_return,
            "mean_turnover": aggregate.mean_turnover,
            "hit_rate": aggregate.hit_rate,
            # G3: the pooled series itself (the folds concatenate — each
            # restarts from fresh cash), so the artifact alone re-derives
            # the product identity behind n_session_returns/total_return
            "session_returns": list(aggregate.session_returns),
        },
    }
    if liquidity_lane != 1:
        # G2: the regime deviation is stamped in the payload too, so the
        # artifact alone discloses which filter produced its audit rows
        payload["liquidity_lane"] = liquidity_lane
        payload["flow_min_session_volume"] = flow_min_session_volume
    if score_seed is not None:
        # G5: the declared score model's seed is stamped in the payload so
        # the artifact discloses it without re-hashing the config
        payload["score_seed"] = score_seed
    return payload, stats
