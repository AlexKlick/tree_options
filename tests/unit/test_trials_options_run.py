"""Workstream F: the options trial runner (M3 plan §3.F)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

import pytest

from tests.conftest import REPO_ROOT
from tree_options.data.authority import PointInTimeDataset
from tree_options.data.bars import BarRecord
from tree_options.data.options_pit import OptionPitSurface
from tree_options.data.real_overlay import RealSessionCalendar
from tree_options.data.vwap_pit_surface import VwapPitSurface
from tree_options.evaluation.stats import ScoredLabel
from tree_options.options import OptionsStrategyConfig
from tree_options.protocol.loader import load_protocol
from tree_options.registry.sqlite import TrialRegistry
from tree_options.trials import OptionsSplitOverride, run_options_trial
from tree_options.trials.options_run import (
    _cohort_series,
    _lag1_autocorrelation,
    _spearman,
    _t_statistic,
)

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


def test_stride4_series_aligns_sessions_with_defined_ics() -> None:
    """Review r1 P1-2 (pairing) + review r3 P1-2 (fixed grid): the stride-4
    series selects every COHORT_STRIDE-th session of the FULL entry-session
    grid — the predeclared every-fourth-session statistic — and keeps only
    defined ICs among those grid points. An undefined cohort IC drops its
    POINT without shifting later sessions onto earlier grid positions; it
    must never advance which sessions the grid selects (the r1 repair
    corrected the session->IC pairing but still ran the stride over the
    defined-only compressed list, picking e0,e5 instead of e0,e4)."""

    def _pos(session: str, score: float, ret: float) -> dict[str, object]:
        return {"entry_session": session, "score": score, "signed_premium_return": ret}

    # e1 has only 2 positions -> _spearman is None -> cohort undefined.
    closed = [
        _pos("2020-01-01", 1.0, 0.10),
        _pos("2020-01-01", 2.0, 0.30),
        _pos("2020-01-01", 3.0, 0.20),
        _pos("2020-01-02", 1.0, 0.30),
        _pos("2020-01-02", 2.0, 0.10),
        _pos("2020-01-03", 1.0, 0.10),
        _pos("2020-01-03", 2.0, 0.20),
        _pos("2020-01-03", 3.0, 0.30),
        _pos("2020-01-06", 1.0, 0.30),
        _pos("2020-01-06", 2.0, 0.10),
        _pos("2020-01-06", 3.0, 0.20),
        _pos("2020-01-07", 1.0, 0.20),
        _pos("2020-01-07", 2.0, 0.10),
        _pos("2020-01-07", 3.0, 0.30),
        _pos("2020-01-08", 1.0, 0.30),
        _pos("2020-01-08", 2.0, 0.20),
        _pos("2020-01-08", 3.0, 0.10),
    ]
    stride4, cohort_ics, counts = _cohort_series(closed)
    assert len(cohort_ics) == 5  # e1 (2 positions) is undefined and dropped
    assert counts == [3, 3, 3, 3, 3]
    sessions = [s for s, _ic in stride4]
    assert sessions == ["2020-01-01", "2020-01-07"], (
        "stride-4 runs over the FULL session grid: indices 0 and 4 of "
        "[01-01, 01-02*, 01-03, 01-06, 01-07, 01-08] — the undefined 01-02 "
        "drops its point but must not advance the grid onto 01-08"
    )
    # grid point 01-07 must carry ITS OWN IC: scores/returns ranks
    # [1,2,3] vs [0.20,0.10,0.30] -> ranks [2,1,3] -> rho 0.5.
    own_ic = _spearman([1.0, 2.0, 3.0], [0.20, 0.10, 0.30])
    assert own_ic == pytest.approx(0.5)
    assert stride4[1][1] == pytest.approx(own_ic), (
        "the session->IC pairing must stay aligned across undefined cohorts"
    )


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


# ---- G2: lane-2 filter + threshold injection in the trial runner -----------------


def _run_trial(
    world,
    protocol,
    tmp_path,
    *,
    tag: str,
    decision_calendar=None,
    surface=None,
    dataset=None,
    **kwargs,
):
    """One fixture-scale trial; returns the stamped artifact body.

    `decision_calendar` (R3-P1-1) swaps ONLY the runner's decision grid for
    another calendar over the same sessions — the world, surface, dataset and
    scored rows are untouched, so any config-hash difference below is exactly
    the calendars' identity difference. (R8-P1) `surface` and `dataset`
    override the fixture surface/dataset for the calendar-liar probes: a
    probe calendar must be disclosed by the surface (or the R4-P1 digest
    refuses), and the cancellation-window probe carries its own action in
    the dataset."""
    import json

    overlay, calendar, snapshot, world_dataset = world
    if decision_calendar is not None:
        calendar = decision_calendar
    surface = OptionPitSurface(overlay) if surface is None else surface
    dataset = world_dataset if dataset is None else dataset
    scored = _scored(world, surface)
    registry = TrialRegistry(tmp_path / f"{tag}.db")
    try:
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
            hypothesis=f"unit/{tag}",
            decision_sessions=tuple(sorted({row.session for row in scored})),
            options_manifest_hash="0" * 64,
            registry=registry,
            artifacts_dir=tmp_path / tag,
            repo=REPO_ROOT,
            clock=FIXED_CLOCK,
            split_override=SPLIT,
            allow_dirty=True,
            **kwargs,
        )
    finally:
        registry.close()
    return json.loads(result.artifact_path.read_text(encoding="utf-8"))


def test_lane_2_selects_the_volume_flow_filter(world, protocol, tmp_path) -> None:
    body = _run_trial(world, protocol, tmp_path, tag="lane2", liquidity_lane=2)
    rules = set(body["payload"]["counters"]["rule_histogram"])
    # the regime actually ran: the flow rule name exists, the two-sided
    # volume rule name does not
    assert "session_volume_flow" in rules
    assert "same_day_volume" not in rules
    # the deviation is stamped: regime + effective threshold ride the payload
    assert body["payload"]["liquidity_lane"] == 2
    assert body["payload"]["flow_min_session_volume"] == 100  # the protocol value


def test_lane_1_default_and_explicit_are_byte_identical(world, protocol, tmp_path) -> None:
    default = _run_trial(world, protocol, tmp_path, tag="lane1_default")
    explicit = _run_trial(world, protocol, tmp_path, tag="lane1_explicit", liquidity_lane=1)
    assert default["stamp"]["config_hash"] == explicit["stamp"]["config_hash"]
    # no lane keys leaked into the lane-1 payload (pure addition rule)
    assert "liquidity_lane" not in default["payload"]
    assert "flow_min_session_volume" not in default["payload"]
    # lane 1 keeps the two-sided regime
    rules = set(default["payload"]["counters"]["rule_histogram"])
    assert "same_day_volume" in rules
    assert "session_volume_flow" not in rules


def test_lane_2_threshold_deviation_rides_the_config_hash(world, protocol, tmp_path) -> None:
    at_protocol = _run_trial(world, protocol, tmp_path, tag="flow_default", liquidity_lane=2)
    explicit_protocol = _run_trial(
        world,
        protocol,
        tmp_path,
        tag="flow_explicit_default",
        liquidity_lane=2,
        flow_min_session_volume=100,
    )
    deviated = _run_trial(
        world,
        protocol,
        tmp_path,
        tag="flow_250",
        liquidity_lane=2,
        flow_min_session_volume=250,
    )
    # an explicit protocol-value override is NOT a deviation: same hash
    assert at_protocol["stamp"]["config_hash"] == explicit_protocol["stamp"]["config_hash"]
    # a real deviation changes the hash and is stamped
    assert deviated["stamp"]["config_hash"] != at_protocol["stamp"]["config_hash"]
    assert deviated["payload"]["flow_min_session_volume"] == 250


def test_unknown_lane_and_lane_1_threshold_refuse(world, protocol, tmp_path) -> None:
    overlay, calendar, snapshot, dataset = world
    surface = OptionPitSurface(overlay)
    scored = _scored(world, surface)
    for kwargs, match in (
        ({"liquidity_lane": 3}, "liquidity_lane must be 1 or 2"),
        (
            {"liquidity_lane": 1, "flow_min_session_volume": 100},
            "flow_min_session_volume is a lane-2 config key",
        ),
        ({"liquidity_lane": 2, "flow_min_session_volume": 0}, "int >= 1"),
    ):
        registry = TrialRegistry(tmp_path / "refuse.db")
        with pytest.raises(ValueError, match=match):
            run_options_trial(
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
                hypothesis="refused",
                decision_sessions=tuple(sorted({row.session for row in scored})),
                options_manifest_hash="0" * 64,
                registry=registry,
                artifacts_dir=tmp_path / "refused",
                repo=REPO_ROOT,
                clock=FIXED_CLOCK,
                split_override=SPLIT,
                allow_dirty=True,
                **kwargs,
            )
        registry.close()
    assert trial_count(tmp_path / "refuse.db") == 0


# ---- G5: the null-score seed rides the config hash -------------------------------


def test_score_seed_rides_the_config_hash(world, protocol, tmp_path) -> None:
    plain = _run_trial(world, protocol, tmp_path, tag="seed_none")
    seeded = _run_trial(world, protocol, tmp_path, tag="seed_a", score_seed="t-null/a")
    other = _run_trial(world, protocol, tmp_path, tag="seed_b", score_seed="t-null/b")
    assert plain["stamp"]["config_hash"] != seeded["stamp"]["config_hash"]
    assert seeded["stamp"]["config_hash"] != other["stamp"]["config_hash"]
    # the seed is stamped in the payload too: the artifact discloses the
    # declared score model input on its own
    assert seeded["payload"]["score_seed"] == "t-null/a"
    # no seed, no key: the lane-1 default payload is unchanged
    assert "score_seed" not in plain["payload"]


def test_empty_score_seed_refuses(world, protocol, tmp_path) -> None:
    overlay, _cal, _snap, _ds = world
    surface = OptionPitSurface(overlay)
    scored = _scored(world, surface)
    registry = TrialRegistry(tmp_path / "seed_empty.db")
    with pytest.raises(ValueError, match="score_seed must be a non-empty string"):
        run_options_trial(
            dataset=_ds,
            surface=surface,
            calendar=world[1],
            protocol=protocol,
            world_id=world[2].snapshot_id,
            arm="A",
            strategy_config=OptionsStrategyConfig(),
            scored=scored,
            model_family="fixture:v1",
            model_sha256=None,
            hypothesis="refused",
            decision_sessions=tuple(sorted({row.session for row in scored})),
            options_manifest_hash="0" * 64,
            registry=registry,
            artifacts_dir=tmp_path / "seed_empty",
            repo=REPO_ROOT,
            clock=FIXED_CLOCK,
            split_override=SPLIT,
            allow_dirty=True,
            score_seed="",
        )
    registry.close()


# ---- (P1-3, Codex round 1) the null-score seed is BOUND to trial identity ----------


def _rescored_with(seed: str, scored) -> tuple[ScoredLabel, ...]:
    """The same cross-section re-scored under one null seed — rows that can
    only be honest under exactly that seed."""
    from tree_options.trials.null_score import null_score

    return tuple(
        ScoredLabel(
            security_id=row.security_id,
            session=row.session,
            score=null_score(seed=seed, session=row.session, security_id=row.security_id),
            label=row.label,
        )
        for row in scored
    )


def test_null_family_without_a_seed_refuses_before_registration(world, protocol, tmp_path) -> None:
    """(P1-3) `NULL_SCORE_MODEL_FAMILY` was referenced NOWHERE in the runner:
    `score_seed=None` passed silently for a null-sha256 trial and was omitted
    from the hashed config, so two T-NULL trials with different seeds got
    IDENTICAL config hashes. A null trial now REFUSES to run without its
    seed — an undeclared seed is unregistered randomness (RED before: the
    trial registered and completed)."""
    from tree_options.trials.null_score import NULL_SCORE_MODEL_FAMILY

    assert NULL_SCORE_MODEL_FAMILY == "null-sha256/1"
    overlay, calendar, snapshot, dataset = world
    surface = OptionPitSurface(overlay)
    scored = _scored(world, surface)
    registry = TrialRegistry(tmp_path / "null_noseed.db")
    try:
        with pytest.raises(ValueError, match="must declare its seed"):
            run_options_trial(
                dataset=dataset,
                surface=surface,
                calendar=calendar,
                protocol=protocol,
                world_id=snapshot.snapshot_id,
                arm="A",
                strategy_config=OptionsStrategyConfig(),
                scored=scored,
                model_family=NULL_SCORE_MODEL_FAMILY,
                model_sha256=None,
                hypothesis="refused before registration",
                decision_sessions=tuple(sorted({row.session for row in scored})),
                options_manifest_hash="0" * 64,
                registry=registry,
                artifacts_dir=tmp_path / "null_noseed",
                repo=REPO_ROOT,
                clock=FIXED_CLOCK,
                split_override=SPLIT,
                allow_dirty=True,
                score_seed=None,
            )
    finally:
        registry.close()
    assert trial_count(tmp_path / "null_noseed.db") == 0


def test_null_family_with_a_misstated_seed_refuses_by_name(world, protocol, tmp_path) -> None:
    """(P1-3) The stamped seed must be VERIFIED against every scored row: a
    seed of 'a' over rows generated under 'b' is a misstatement that could
    masquerade as the declared score model — the runner now recomputes
    null_score(seed=score_seed, session, security_id) per row and refuses
    on any mismatch (RED before: the trial registered and completed)."""
    from tree_options.trials.null_score import NULL_SCORE_MODEL_FAMILY

    overlay, calendar, snapshot, dataset = world
    surface = OptionPitSurface(overlay)
    mislabelled = _rescored_with("t-null/b", _scored(world, surface))
    registry = TrialRegistry(tmp_path / "null_misstated.db")
    try:
        with pytest.raises(ValueError, match="score mismatch against the declared seed"):
            run_options_trial(
                dataset=dataset,
                surface=surface,
                calendar=calendar,
                protocol=protocol,
                world_id=snapshot.snapshot_id,
                arm="A",
                strategy_config=OptionsStrategyConfig(),
                scored=mislabelled,
                model_family=NULL_SCORE_MODEL_FAMILY,
                model_sha256=None,
                hypothesis="refused before registration",
                decision_sessions=tuple(sorted({row.session for row in mislabelled})),
                options_manifest_hash="0" * 64,
                registry=registry,
                artifacts_dir=tmp_path / "null_misstated",
                repo=REPO_ROOT,
                clock=FIXED_CLOCK,
                split_override=SPLIT,
                allow_dirty=True,
                score_seed="t-null/a",
            )
    finally:
        registry.close()
    assert trial_count(tmp_path / "null_misstated.db") == 0


def test_null_family_with_matching_rows_runs_and_binds_the_seed(world, protocol, tmp_path) -> None:
    """(P1-3) The honest null trial: rows generated under the declared seed
    pass the verification, the seed rides the config hash (a different seed
    is a different trial identity), and the payload stamps it. Non-null
    families keep today's behavior exactly (seed optional, stamped when
    present) — pinned by test_score_seed_rides_the_config_hash."""
    import json

    from tree_options.trials.null_score import NULL_SCORE_MODEL_FAMILY, null_scored_labels

    fixture_rows = tuple(
        (row.session, row.security_id, row.label)
        for row in _scored(world, OptionPitSurface(world[0]))
    )
    honest = null_scored_labels("t-null/a", fixture_rows)
    other = null_scored_labels("t-null/b", fixture_rows)
    assert honest[0].score != other[0].score  # the seeds really do differ

    def _null_run(tag: str, seed: str, scored) -> dict:
        overlay, calendar, snapshot, dataset = world
        registry = TrialRegistry(tmp_path / f"{tag}.db")
        try:
            result = run_options_trial(
                dataset=dataset,
                surface=OptionPitSurface(overlay),
                calendar=calendar,
                protocol=protocol,
                world_id=snapshot.snapshot_id,
                arm="A",
                strategy_config=OptionsStrategyConfig(),
                scored=scored,
                model_family=NULL_SCORE_MODEL_FAMILY,
                model_sha256=None,
                hypothesis=f"unit/{tag}",
                decision_sessions=tuple(sorted({row.session for row in scored})),
                options_manifest_hash="0" * 64,
                registry=registry,
                artifacts_dir=tmp_path / tag,
                repo=REPO_ROOT,
                clock=FIXED_CLOCK,
                split_override=SPLIT,
                allow_dirty=True,
                score_seed=seed,
            )
        finally:
            registry.close()
        return json.loads(result.artifact_path.read_text(encoding="utf-8"))

    run_a = _null_run("null_a", "t-null/a", honest)
    run_b = _null_run("null_b", "t-null/b", other)
    assert run_a["stamp"]["config_hash"] != run_b["stamp"]["config_hash"]
    assert run_a["payload"]["score_seed"] == "t-null/a"
    assert run_b["payload"]["score_seed"] == "t-null/b"


# ---- (w5) the holdout seal guard: runtime refusal + execution-tail disclosure ------


def test_sealed_test_sessions_are_refused_before_registration(world, protocol, tmp_path) -> None:
    """(w5, verdict D7.1) Runtime enforcement, previously absent: window A
    (protocol.holdout, 13 sealed Fridays 2026-05-08..2026-08-14) was consumed
    only by the amendment builder — nothing stopped a mis-declared grid from
    putting a sealed date inside a REGISTERED fold's test window. The runner
    now refuses (hard error) before registration, so the seal is never spent
    and the config budget is never burned on a leaking trial."""
    from tree_options.protocol.holdout import FINAL_HOLDOUT_DATES

    _overlay, calendar, snapshot, dataset = world
    surface = OptionPitSurface(world[0])
    scored = _scored(world, surface)
    sessions = calendar.sessions()
    start = next(i for i, s in enumerate(sessions) if s >= date(2025, 6, 1))
    end = next(i for i, s in enumerate(sessions) if s >= date(2026, 8, 21))
    grid = sessions[start : end + 1]
    assert any(s.isoformat() in set(FINAL_HOLDOUT_DATES) for s in grid)
    registry = TrialRegistry(tmp_path / "seal_refuse.db")
    try:
        with pytest.raises(ValueError, match="holdout seal"):
            run_options_trial(
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
                hypothesis="must be refused before registration",
                decision_sessions=grid,
                options_manifest_hash="0" * 64,
                registry=registry,
                artifacts_dir=tmp_path / "seal_refuse",
                repo=REPO_ROOT,
                clock=FIXED_CLOCK,
                split_override=SPLIT,
                allow_dirty=True,
            )
    finally:
        registry.close()
    assert trial_count(tmp_path / "seal_refuse.db") == 0


def test_execution_tail_seal_consumption_is_tagged_not_refused(
    world, protocol, tmp_path, monkeypatch
) -> None:
    """(w5, verdict D7.2) The boundary rule: decision sessions stay OUT of the
    sealed window (refused above), but the EXECUTION tail — exits, settlements
    and marks after the last test session, END_BUFFER deep — is DISCLOSED,
    never banned. Every artifact carries the seal block, per-fold tail tags,
    and per-position tags, so a verdict can name the holdout-overlapping
    sensitivity subset instead of silently containing window-A executions."""
    import tree_options.trials.options_run as runner

    plain = _run_trial(world, protocol, tmp_path, tag="seal_plain")
    seal = plain["payload"]["holdout_seal"]
    assert seal["window_id"] == "final-holdout-window-a"
    assert len(seal["sealed_dates"]) == 13
    # the fixture world (2018) consumes nothing: all counters zero, honestly
    assert seal["fold_test_window_intersections"] == 0
    assert seal["folds_with_sealed_execution_tail"] == 0
    assert seal["positions_exiting_in_seal"] == 0
    assert seal["positions_with_seal_overlapping_label_window"] == 0
    # every fold stamps its own test window (the seal's decision boundary)
    for fold in plain["payload"]["per_fold"]:
        assert fold["test_window"]["start"] <= fold["test_window"]["end"]
        assert fold["holdout_seal_consumed"] is False
        assert fold["holdout_seal_consumed_sessions"] == []
    # every position row carries the two disclosure tags
    for position in plain["payload"]["pooled"]["positions"]:
        assert position["exit_in_holdout_seal"] is False
        assert position["label_window_touches_holdout_seal"] is False

    # Now RED-PROVE the disclosure fires: seal the sessions of the LAST fold's
    # execution tail (strictly after its last test session, so the D7.1
    # refusal cannot fire) and require the tags + counts to appear.
    last_test_end = max(
        date.fromisoformat(fold["test_window"]["end"]) for fold in plain["payload"]["per_fold"]
    )
    world_last = date.fromisoformat(plain["payload"]["world_last_session"])
    assert last_test_end < world_last  # the fixture leaves an execution tail
    calendar = world[1]
    sealed = frozenset(
        session.isoformat()
        for session in calendar.sessions()
        if last_test_end < session <= world_last
    )
    assert sealed
    # strictly after every test window, so the D7.1 refusal cannot fire here
    assert all(
        fold["test_window"]["end"] <= last_test_end.isoformat()
        for fold in plain["payload"]["per_fold"]
    )
    monkeypatch.setattr(runner, "_SEALED_HOLDOUT_SESSIONS", sealed)
    tagged = _run_trial(world, protocol, tmp_path, tag="seal_tagged")
    block = tagged["payload"]["holdout_seal"]
    assert block["folds_with_sealed_execution_tail"] >= 1
    assert block["sealed_execution_tail_sessions"]
    assert set(block["sealed_execution_tail_sessions"]) <= sealed
    consuming = [f for f in tagged["payload"]["per_fold"] if f["holdout_seal_consumed"]]
    assert consuming
    for fold in consuming:
        assert fold["holdout_seal_consumed_sessions"]
        assert set(fold["holdout_seal_consumed_sessions"]) <= sealed
    tagged_positions = [
        p
        for p in tagged["payload"]["pooled"]["positions"]
        if p["exit_in_holdout_seal"] or p["label_window_touches_holdout_seal"]
    ]
    assert tagged_positions
    assert block["positions_exiting_in_seal"] == sum(
        1 for p in tagged["payload"]["pooled"]["positions"] if p["exit_in_holdout_seal"]
    )


# ---- G3 extension (w4): verdict-computable per-position + per-fold stamps ---------


def test_g3_extension_per_position_stamps(world, protocol, tmp_path) -> None:
    """(w4, verdict D6) Without per-position `strike` / `abs_delta` at entry /
    `dte_at_entry` / `label_window`, T-BAND and T-DTE falsifiers are not
    artifact-computable; without the hold window a funded events source
    cannot retro-tag earnings overlap without re-running."""
    body = _run_trial(world, protocol, tmp_path, tag="g3_ext_pos")
    positions = body["payload"]["pooled"]["positions"]
    assert positions
    _overlay, calendar, _snap, _ds = world
    sessions = calendar.sessions()
    ordinal = {s: i for i, s in enumerate(sessions)}
    for position in positions:
        # the decision-visible selection facts the band rules classified on
        assert position["strike"] and Decimal(position["strike"]) > 0
        assert position["abs_delta"] and Decimal(position["abs_delta"]) > 0
        assert 30 <= position["dte_at_entry"] <= 60  # the protocol band
        assert position["decision_session"]
        decision = date.fromisoformat(position["decision_session"])
        assert (date.fromisoformat(position["contract_expiration"]) - decision).days == (
            position["dte_at_entry"]
        )
        # the label window is the DECISION's H5 window (labels/build semantics:
        # base = the session before the decision, end = base + 5 sessions)
        window = position["label_window"]
        assert window and window["start"] and window["end"]
        start, end = date.fromisoformat(window["start"]), date.fromisoformat(window["end"])
        assert ordinal[start] == ordinal[decision] - 1
        assert ordinal[end] == ordinal[start] + SPLIT.label_horizon_sessions
        assert start < decision < end
        # the hold window is the executed round trip (end null while open)
        hold = position["hold_window"]
        assert hold["start"] == position["entry_session"]
        assert hold["end"] == position["exit_session"]


def test_g3_extension_per_fold_fee_totals(world, protocol, tmp_path) -> None:
    """(w4, verdict D6) Per-fold `fees_total` so a cost-floor decomposition is
    computable from the artifact alone; the totals are the fold's OWN fills'
    fee sum, real money rather than a zero placeholder."""
    body = _run_trial(world, protocol, tmp_path, tag="g3_ext_fees")
    per_fold = body["payload"]["per_fold"]
    assert per_fold
    assert len({fold["fold_id"] for fold in per_fold}) == len(per_fold)
    for fold in per_fold:
        assert Decimal(fold["fees_total"]) >= 0
    assert any(Decimal(fold["fees_total"]) > 0 for fold in per_fold)


def test_g3_extension_existing_keys_are_byte_identical(world, protocol, tmp_path) -> None:
    """(w4) Additive ONLY: every key the payload carried before this change is
    still present with its previous shape, and dataset_provenance keeps the
    exact synthetic token for a synthetic-sourced dataset."""
    body = _run_trial(world, protocol, tmp_path, tag="g3_ext_shape")
    payload = body["payload"]
    # the pre-w4 top-level payload keys (the artifact never loses a key)
    assert {
        "runner",
        "world_id",
        "arm",
        "model_family",
        "max_quote_age_seconds",
        "cohort_stride",
        "n_folds",
        "world_last_session",
        "per_fold",
        "pooled",
        "fills_log",
        "counters",
        "backtest",
    } <= set(payload)
    # the pre-w4 per-position keys, unchanged in name and form
    for position in payload["pooled"]["positions"]:
        assert {
            "underlying_security_id",
            "call_put",
            "score",
            "label",
            "entry_session",
            "entry_price",
            "contract_expiration",
            "exit_kind",
            "exit_session",
            "exit_price",
            "premium_return",
            "signed_premium_return",
        } <= set(position)
    # the pre-w4 per-fold keys, unchanged
    for fold in payload["per_fold"]:
        assert {
            "fold_id",
            "n_test_sessions",
            "n_positions",
            "n_fills",
            "fills_buy",
            "fills_sell",
            "n_sessions_evaluated",
            "conservation_checks",
            "force_closes",
            "early_exercises",
            "expiries",
            "terminals",
            "total_return",
            "session_returns",
            "equity_start",
            "equity_end",
        } <= set(fold)
    # byte-identical provenance for the synthetic fixture world
    assert payload["backtest"]["dataset_provenance"] == "synthetic/v1"


def test_dataset_provenance_is_derived_from_the_dataset_identity() -> None:
    """(w4) The hardcoded `synthetic/v1` is gone: a synthetic-SOURCED dataset
    keeps the historical token byte-identically (every registered synthetic
    trial), and any other snapshot identity is stamped as ITSELF — a lane-2
    artifact can never claim synthetic provenance."""
    from tree_options.synth.generate import PROVIDER
    from tree_options.trials.options_run import _dataset_provenance

    assert PROVIDER == "synthetic/v1"  # the pinned source token
    assert _dataset_provenance("m3-unit-strategy-906", frozenset({PROVIDER})) == "synthetic/v1"
    assert _dataset_provenance("synth-v1-dev-null-101", frozenset({PROVIDER})) == "synthetic/v1"
    assert _dataset_provenance("massive-derived-free/1", frozenset({"spot-proxy/declared"})) == (
        "massive-derived-free/1"
    )
    # a mixed-source dataset never collapses into the synthetic token either
    assert _dataset_provenance("mixed/1", frozenset({PROVIDER, "spot-proxy/declared"})) == "mixed/1"


# ---- G3: per-fold session-return series + fold equity endpoints ------------------


def test_per_fold_session_returns_and_equity_endpoints_stamped(world, protocol, tmp_path) -> None:
    import math

    body = _run_trial(world, protocol, tmp_path, tag="g3_stamp")
    per_fold = body["payload"]["per_fold"]
    assert per_fold
    for fold in per_fold:
        series = fold["session_returns"]
        assert len(series) == fold["n_sessions_evaluated"]
        # the stamped series is exactly the summary's input: its product IS
        # the fold's total_return
        assert math.prod(1.0 + r for r in series) - 1.0 == pytest.approx(
            fold["total_return"], abs=1e-12
        )
        # the endpoints are the fold's OWN first/last stamped equity (the
        # first evaluated session's close, not the pre-session cash float)
        assert Decimal(fold["equity_start"]) > 0
        assert Decimal(fold["equity_end"]) >= 0


def test_pooled_session_returns_stamped_and_consistent(world, protocol, tmp_path) -> None:
    import math

    body = _run_trial(world, protocol, tmp_path, tag="g3_pooled")
    backtest = body["payload"]["backtest"]
    series = backtest["session_returns"]
    assert len(series) == backtest["n_session_returns"]
    assert math.prod(1.0 + r for r in series) - 1.0 == pytest.approx(
        backtest["total_return"], abs=1e-12
    )
    # the pooled series is the concatenation of the per-fold series (each
    # fold restarts from fresh cash, so nothing is double-counted)
    per_fold_total = sum(len(fold["session_returns"]) for fold in body["payload"]["per_fold"])
    assert per_fold_total == len(series)


# ---- (P2-6, Codex round 1) holdout disclosure honesty ---------------------------------


@pytest.mark.parametrize("lane", [1, 2])
def test_holdout_seal_block_states_its_declared_scope_and_the_artifacts_lane(
    world, protocol, tmp_path, lane
) -> None:
    """(P2-6) The seal is SCOPED 'lane-2 evaluation folds (massive-derived-
    free/1)' but the disclosure was unconditional — every lane-1 artifact
    read as though it claimed a lane-1-scoped seal. The block now states
    the DECLARED scope verbatim, the artifact's own liquidity_lane, and an
    explicit `applied` field naming the unconditional refusal under the
    declared scope. Additive keys only; the refusal itself stays
    unconditional on BOTH lanes (RED before: the keys do not exist)."""
    from tree_options.protocol.holdout import FINAL_HOLDOUT_SCOPE

    body = _run_trial(world, protocol, tmp_path, tag=f"seal_scope_l{lane}", liquidity_lane=lane)
    seal = body["payload"]["holdout_seal"]
    assert seal["declared_scope"] == FINAL_HOLDOUT_SCOPE
    assert seal["declared_scope"] == "lane-2 evaluation folds (massive-derived-free/1)"
    assert seal["liquidity_lane"] == lane
    assert seal["applied"] == (f"unconditional-refusal (declared scope: {FINAL_HOLDOUT_SCOPE})")
    # the pre-existing keys keep their names and shapes (additive only)
    assert seal["window_id"] == "final-holdout-window-a"
    assert seal["scope"] == FINAL_HOLDOUT_SCOPE
    assert len(seal["sealed_dates"]) == 13


@pytest.mark.parametrize("lane", [1, 2])
def test_the_seal_refusal_still_fires_on_both_lanes(world, protocol, tmp_path, lane) -> None:
    """(P2-6) The refusal stays UNCONDITIONAL (strictly safer: a sealed date
    can never enter a registered fold on ANY lane) — the fix is disclosure
    honesty, never a scoped weakening. A grid intersecting the sealed window
    refuses before registration on lane 1 and lane 2 alike."""
    _overlay, calendar, snapshot, dataset = world
    surface = OptionPitSurface(world[0])
    scored = _scored(world, surface)
    sessions = calendar.sessions()
    start = next(i for i, s in enumerate(sessions) if s >= date(2025, 6, 1))
    end = next(i for i, s in enumerate(sessions) if s >= date(2026, 8, 21))
    grid = sessions[start : end + 1]
    registry = TrialRegistry(tmp_path / f"seal_both_lanes_{lane}.db")
    try:
        with pytest.raises(ValueError, match="holdout seal"):
            run_options_trial(
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
                hypothesis="must be refused before registration on every lane",
                decision_sessions=grid,
                options_manifest_hash="0" * 64,
                registry=registry,
                artifacts_dir=tmp_path / f"seal_refused_{lane}",
                repo=REPO_ROOT,
                clock=FIXED_CLOCK,
                split_override=SPLIT,
                liquidity_lane=lane,
                allow_dirty=True,
            )
    finally:
        registry.close()
    assert trial_count(tmp_path / f"seal_both_lanes_{lane}.db") == 0


# ---- (P1-1, Codex round 1) the dual-calendar seam at the runner level ----------------


def _friday_grid():
    """The era's Friday-only decision grid, derived from the NYSE fixture
    (`friday_only_grid_derived_from_the_nyse_fixture`): the fixture's
    Friday sessions whose 48th (index 47) is the era profile's first
    enumerated test session 2025-08-01, running through 2026-06-26 — the
    execution-tail headroom past the last test quarter (the era profile's
    deepest execution mark). Holiday Fridays (Good Friday) are absent
    because the FIXTURE omits them.

    (R2-P1-c, Codex round 2) The grid carries the fixture's EARLY CLOSES —
    the fixture AS COMMITTED. The old helper passed `frozenset()`, which
    stripped them: every session_close answered 16:00 ET, so 2025-11-28
    (inside fold 2's test window, a 13:00 close in the fixture) could never
    reach the lane except as a decision recorded three hours after the
    actual close. Early closes are SESSIONS — the enumeration is unchanged."""
    from tree_options.data.real_overlay import RealSessionCalendar
    from tree_options.time.calendar import StaticSessionCalendar
    from tree_options.time.sessions import early_close_instant

    exchange = StaticSessionCalendar(
        REPO_ROOT / "data" / "calendar" / "nyse_sessions_2018_01_02_2026_12_31.json",
        REPO_ROOT / "data" / "calendar" / "nyse_sessions_2018_01_02_2026_12_31.sha256",
    )
    fridays = [s for s in exchange.sessions() if s.weekday() == 4 and s <= date(2026, 6, 26)]
    start = fridays.index(date(2025, 8, 1)) - 47
    early = frozenset(exchange.early_close_sessions())
    grid = RealSessionCalendar(tuple(fridays[start:]), early & frozenset(fridays[start:]))
    sessions = grid.sessions()
    assert sessions[47] == date(2025, 8, 1)
    assert date(2025, 4, 18) not in sessions  # Good Friday: the fixture omits it
    # the fixture's early closes ride the grid: 2025-11-28 closes 13:00 ET
    assert grid.session_close(date(2025, 11, 28)) == early_close_instant(date(2025, 11, 28))
    return grid


# (expiry, bar span) per active contract: bars on every NYSE session of the
# span — the Thursdays the decisions/entries read, the Thursdays the fills
# select, and the Fridays that keep each contract's observed listing window
# open through its last execution. The second span runs through 2025-12-05
# so the early-close Friday 2025-11-28 is an OVERLAY session too (R2-P1-c:
# the adapter's decision-at seam is proven on a session both calendars
# carry, where the two closes genuinely disagree).
_ERA_CONTRACTS = (
    (date(2025, 9, 19), (date(2025, 7, 24), date(2025, 8, 22))),
    (date(2025, 12, 19), (date(2025, 10, 23), date(2025, 12, 5))),
    (date(2026, 3, 20), (date(2026, 1, 22), date(2026, 2, 20))),
    (date(2026, 6, 19), (date(2026, 4, 23), date(2026, 5, 1))),
)


def _era_scored_rows(grid) -> tuple[ScoredLabel, ...]:
    """One SPY row per grid Friday whose DTE to the then-live expiry sits
    in the protocol band [30, 60] — a decision inside every fold's test
    quarter (the runner refuses a fold with no scored test rows via
    max() on an empty sequence, so each quarter must carry at least one).
    The scores are the null model's own under the trial's declared seed
    (P1-3's verification accepts nothing else)."""
    from tree_options.trials.null_score import null_score

    rows = []
    for session in grid.sessions():
        if session > date(2026, 5, 1):
            continue
        for expiry, _span in _ERA_CONTRACTS:
            dte = (expiry - session).days
            if 30 <= dte <= 60:
                rows.append(
                    ScoredLabel(
                        security_id="SPY",
                        session=session,
                        score=null_score(seed="t-null/era", session=session, security_id="SPY"),
                        label=0.01,
                    )
                )
                break
    assert len(rows) >= 12
    return tuple(rows)


@pytest.fixture(scope="module")
def era_world(tmp_path_factory: pytest.TempPathFactory):
    """The ruled real-lane geometry's world: a Friday-only decision grid and
    DAILY Massive bars, built as a vendor-shaped capture through the fixture
    builders (no network)."""
    from decimal import Decimal as _Decimal

    from tests.fixtures.massive_structural_sample import (
        bar,
        bars_payload,
        contract_result,
        contracts_payload,
    )
    from tests.unit.test_vwap_pit_surface import _exchange_calendar, _t
    from tree_options.data.massive_overlay import load_derived_surface
    from tree_options.synth_options.greeks import bs_price

    grid = _friday_grid()
    exchange = _exchange_calendar()
    capture = tmp_path_factory.mktemp("eralane") / "capture"
    masters = capture / "masters"
    masters.mkdir(parents=True)
    first_bar = min(span[0] for _e, span in _ERA_CONTRACTS)
    (masters / f"spy_{first_bar:%Y-%m-%d}.json").write_text(
        contracts_payload(
            results=tuple(
                contract_result(
                    ticker=f"O:SPY{expiry:%y%m%d}C00600000",
                    underlying="SPY",
                    expiration=f"{expiry:%Y-%m-%d}",
                    strike="600",
                    contract_type="call",
                )
                for expiry, _span in _ERA_CONTRACTS
            ),
            as_of=f"{first_bar:%Y-%m-%d}",
        ),
        encoding="utf-8",
    )
    bars = capture / "bars"
    bars.mkdir()
    spot_sessions = set()
    for expiry, (lo, hi) in _ERA_CONTRACTS:
        span = [s for s in exchange.sessions() if lo <= s <= hi]
        assert len(span) >= 5  # the decision Thursdays + the execution Friday span

        def premium(session: date, _expiry: date = expiry) -> str:
            price = bs_price(
                spot=600.0,
                strike=600.0,
                dte_calendar_days=(_expiry - session).days,
                iv=0.18,
                risk_free=0.03,
                dividend_yield=0.0,
                call_put="C",
            )
            return f"{price:.4f}"

        rows = tuple(
            bar(
                v="120",
                t=_t(session),
                vw=premium(session),
                o=premium(session),
                c=premium(session),
                h=f"{_Decimal(premium(session)) + _Decimal('0.10')}",
                low=f"{_Decimal(premium(session)) - _Decimal('0.10')}",
                n="24",
            )
            for session in span
        )
        (bars / f"bars_{expiry:%Y%m%d}.json").write_text(
            bars_payload(
                ticker=f"O:SPY{expiry:%y%m%d}C00600000",
                results_count=str(len(rows)),
                results=rows,
            ),
            encoding="utf-8",
        )
        spot_sessions.update(span)
    (capture / "spot_proxy.json").write_text(
        '{"SPY": {' + ", ".join(f'"{s:%Y-%m-%d}": "600.00"' for s in sorted(spot_sessions)) + "}}",
        encoding="utf-8",
    )
    overlay = load_derived_surface(capture, staleness_sessions=400)
    return grid, overlay


@dataclass(frozen=True)
class _RealLaneDataset:
    """The slice of `PointInTimeDataset` the runner and its backtest read
    (snapshot identity + bars for the world boundary and the settlement
    scans). Building the real dataset is the T-NULL driver's job."""

    snapshot_id: str
    bars: tuple[BarRecord, ...]
    actions: tuple[object, ...] = ()


def test_dual_calendar_ruled_geometry_yields_the_era_folds(era_world, protocol, tmp_path) -> None:
    """(P1-1, Codex round 1 — the fold-removal horn) The ruled geometry
    (real_lane_split_override: H=5, E=2, val=6, test=13, roll=13,
    min_train=34, all in GRID FRIDAYS) on the Friday grid yields the era
    profile's THREE folds of THIRTEEN test Fridays with no 'no folds'
    error — while the fill engine runs on the overlay's DAILY calendar.
    Under the single DAILY calendar the same call removes every fold (13
    consecutive daily sessions are never a subset of a Fridays-only world
    set); under the single grid calendar the runner completes but
    discloses no calendar identities. Lane 2 under the still-current 0.2.1
    protocol trades zero (the earnings rule answers NOT_EVALUABLE on
    spans_earnings=None, the w2 ruling) — the positions zero here is that
    KNOWN refusal, pinned below, never a calendar failure."""
    import json

    from tree_options.data.bars import BarRecord
    from tree_options.data.vwap_pit_surface import VwapPitSurface
    from tree_options.protocol.era_profile import real_lane_split_override
    from tree_options.time.calendar import calendar_content_sha256

    grid, overlay = era_world
    # (R2-P1-c) the adapter carries the grid the runner splits on as its
    # DECISION calendar: candidate decision_at comes from the grid's
    # early-close-aware session_close, and the runner's filter (built on the
    # same grid) finds every snapshot coherent at the TRUE close.
    # (R2-P2-d) and it carries the DECLARED liquidity term read from the
    # same protocol node the runner builds its filter from — the driver
    # pattern: the surface and the filter must never disagree on the term.
    lf = protocol.option_candidate_defaults.liquidity_volume_flow
    assert lf is not None
    surface = VwapPitSurface(
        overlay,
        decision_calendar=grid,
        underlying_liquidity_term=lf.underlying_liquidity_term,
    )
    world_id = overlay.spec.world_id
    scored = _era_scored_rows(grid)
    decision_sessions = grid.sessions()[: grid.sessions().index(date(2026, 5, 1)) + 1]
    dataset = _RealLaneDataset(
        snapshot_id=world_id,
        bars=(
            BarRecord(
                security_id="SPY",
                session=decision_sessions[-1],
                open=Decimal("600.00"),
                high=Decimal("600.00"),
                low=Decimal("600.00"),
                close=Decimal("600.00"),
                volume=1_000_000,
                source="spot-proxy/declared",
                source_record_id="SPY-20260501",
                source_row_hash="0" * 64,
                snapshot_id=world_id,
                available_at=grid.session_close(decision_sessions[-1]),
            ),
        ),
    )
    registry = TrialRegistry(tmp_path / "era_folds.db")
    try:
        result = run_options_trial(
            dataset=dataset,  # type: ignore[arg-type]
            surface=surface,  # type: ignore[arg-type]
            calendar=grid,
            execution_calendar=overlay.calendar,
            protocol=protocol,
            world_id=world_id,
            arm="A",
            strategy_config=OptionsStrategyConfig(),
            scored=scored,
            model_family="null-sha256/1",
            model_sha256=None,
            hypothesis="unit: the ruled geometry on the Friday grid",
            decision_sessions=decision_sessions,
            options_manifest_hash="0" * 64,
            registry=registry,
            artifacts_dir=tmp_path / "era_folds",
            repo=REPO_ROOT,
            clock=FIXED_CLOCK,
            split_override=real_lane_split_override(),
            liquidity_lane=2,
            score_seed="t-null/era",
            allow_dirty=True,
        )
    finally:
        registry.close()
    assert result.n_folds == 3
    body = json.loads(result.artifact_path.read_text(encoding="utf-8"))
    payload = body["payload"]
    # exactly the era profile's enumeration: three folds x 13 test Fridays,
    # first test session 2025-08-01, quarters starting 08-01 / 10-31 / 01-30,
    # 39 tested Fridays in total, pairwise disjoint
    per_fold = payload["per_fold"]
    assert [f["n_test_sessions"] for f in per_fold] == [13, 13, 13]
    starts = [f["test_window"]["start"] for f in per_fold]
    assert starts == ["2025-08-01", "2025-10-31", "2026-01-30"]
    # (R2 fix 5) the per-fold test-window ENDS, asserted for real: this line
    # was a bare list comprehension — a NO-OP statement, never asserted —
    # which is exactly how a wrong quarter-3 end-date claim slipped through
    # review. 2026-05-01 is the correct 13th session: Good Friday 2026-04-03
    # is absent from the fixture, making 2026-04-24 the 12th. With R2-P1-c
    # restoring the fixture's early closes the enumeration is UNCHANGED
    # (early closes are sessions), and these ends re-pin it.
    ends = [f["test_window"]["end"] for f in per_fold]
    assert ends == ["2025-10-24", "2026-01-23", "2026-05-01"], ends
    tested = sorted({s for fold in per_fold for s in _test_sessions_of(fold)})
    assert len(tested) == 39  # 3 x 13, disjoint
    # both calendar identities are DISCLOSED (additive keys): the grid and
    # the overlay's daily calendar, by name, session count, span, and (R2-P1-b)
    # COMPLETE content identity — the descriptor a config hash rides on
    assert payload["decision_calendar"] == {
        "name": "cboe-eod-real",
        "n_sessions": len(grid.sessions()),
        "first": grid.sessions()[0].isoformat(),
        "last": grid.sessions()[-1].isoformat(),
        "content_sha256": calendar_content_sha256(grid),
    }
    assert payload["execution_calendar"]["n_sessions"] == len(overlay.calendar.sessions())
    assert payload["execution_calendar"]["last"] == overlay.calendar.sessions()[-1].isoformat()
    # the positions zero is the KNOWN 0.2.1 earnings refusal, never a fill
    # failure: the earnings rule answered NOT_EVALUABLE and no BAR_* code fired
    assert payload["pooled"]["n_positions"] == 0
    rules = payload["counters"]["rule_histogram"]
    assert rules.get("earnings_span", {}).get("NOT_EVALUABLE", 0) > 0
    # (R2-P1-c) every evaluated snapshot is COHERENT at the grid's close:
    # the grid carries the fixture's early closes and the adapter carries
    # the grid — without the decision-calendar seam the 16:00 overlay stamp
    # would fail every snapshot on the 13:00 sessions and short-circuit the
    # whole evaluation (decision_coherence is a SILENT-pass rule: any row
    # at all is an incoherent snapshot, so the pin is the ZERO)
    assert "decision_coherence" not in rules
    assert not any(
        code.startswith("BAR_")
        for code in payload["counters"]["rejections"].get("entry_fill_rejections", {})
    )
    # the scored rows are the null model's own under the declared seed (P1-3)
    from tree_options.trials.null_score import null_score

    for row in scored:
        assert row.score == null_score(
            seed="t-null/era", session=row.session, security_id=row.security_id
        )


def _test_sessions_of(fold: dict) -> list[str]:
    """The fold's test-window sessions, re-derived from the grid span the
    payload discloses (n_test_sessions consecutive grid Fridays starting at
    test_window.start)."""

    start = date.fromisoformat(fold["test_window"]["start"])
    grid = _friday_grid()
    index = grid.sessions().index(start)
    return [s.isoformat() for s in grid.sessions()[index : index + fold["n_test_sessions"]]]


# ---- (R2-P1-c, Codex round 2) decision_at from the DECISION grid calendar ------------


def test_decision_at_on_an_early_close_grid_session_is_the_true_close(era_world, protocol) -> None:
    """(R2-P1-c) 2025-11-28 sits inside fold 2's test window and the committed
    NYSE fixture marks it a 13:00 early close — but MassiveDerivedSessionCalendar
    is the overlay's daily calendar with an EMPTY early-close set, so the
    adapter's `candidate_snapshot` stamped 16:00 while `CandidateFilter.evaluate`
    demands the grid's close EXACTLY: no consistent configuration existed. With
    the grid-supplied `decision_calendar` the snapshot's decision_at IS the
    grid's 13:00 and decision_coherence PASSES (RED before R2-P1-c: the
    adapter stamped the overlay's 16:00 and the rule answered NOT_EVALUABLE)."""
    from tree_options.candidates.filters import CandidateFilter
    from tree_options.data.vwap_pit_surface import VwapPitSurface
    from tree_options.synth_options.generate import contract_id_of
    from tree_options.time.sessions import early_close_instant

    grid, overlay = era_world
    session = date(2025, 11, 28)
    assert session in grid.sessions()
    assert session in overlay.calendar.sessions()
    contract = overlay.contract(contract_id_of("SPY", date(2025, 12, 19), "C", Decimal("600")))

    bound = VwapPitSurface(overlay, decision_calendar=grid)
    snap = bound.candidate_snapshot(contract, session)
    assert snap.decision_at == grid.session_close(session)
    assert snap.decision_at == early_close_instant(session)  # 13:00 ET, not 16:00
    decision = CandidateFilter.from_protocol_volume_flow(grid, protocol).evaluate(snap)
    # decision_coherence is a SILENT-pass rule: a coherent snapshot carries
    # NO row for it — the short-circuit list below is what incoherence adds
    assert "decision_coherence" not in {r.rule for r in decision.results}

    # without the dependency: TODAY'S behavior pinned — the overlay (execution)
    # calendar's 16:00, incoherent against the early-close-aware grid (the
    # exact NOT_EVALUABLE that made no consistent configuration exist)
    plain = VwapPitSurface(overlay)
    unbound = plain.candidate_snapshot(contract, session)
    assert unbound.decision_at == overlay.calendar.session_close(session)
    assert unbound.decision_at != early_close_instant(session)
    plain_decision = CandidateFilter.from_protocol_volume_flow(grid, protocol).evaluate(unbound)
    plain_coherence = {r.rule: r for r in plain_decision.results}["decision_coherence"]
    assert plain_coherence.status == "NOT_EVALUABLE"


def test_dual_calendar_lane_1_byte_identity_when_calendars_coincide(
    world, protocol, tmp_path
) -> None:
    """(P1-1) Same object for both calendars -> the default: payload
    byte-identical, config hash unchanged, NO calendar keys (the pure
    addition rule — every existing pinned lane-1 artifact is untouched)."""
    import json

    _overlay, calendar, _snap, _ds = world
    default = _run_trial(world, protocol, tmp_path, tag="cal_default")
    explicit = _run_trial(
        world, protocol, tmp_path, tag="cal_explicit", execution_calendar=calendar
    )
    assert default["stamp"]["config_hash"] == explicit["stamp"]["config_hash"]
    assert json.dumps(default["payload"], sort_keys=True) == json.dumps(
        explicit["payload"], sort_keys=True
    )
    assert "decision_calendar" not in default["payload"]
    assert "execution_calendar" not in default["payload"]


# ---- (R2-P1-b, Codex round 2) calendar identity in the config hash is COMPLETE -------


def _lossy_fields(calendar) -> tuple[object, ...]:
    """Exactly the fields the descriptor disclosed before R2-P1-b (name,
    count, first, last) — the lossy identity two doctored calendars can
    share while differing in content."""
    sessions = calendar.sessions()
    return (
        getattr(calendar, "name", type(calendar).__name__),
        len(sessions),
        sessions[0],
        sessions[-1],
    )


def test_a_calendar_differing_by_one_interior_session_is_a_different_trial_identity(
    world, protocol, tmp_path
) -> None:
    """(R2-P1-b) `_calendar_descriptor` disclosed only
    {name, n_sessions, first, last} — two calendars differing by ONE
    interior session (or by their early-close sets) received the SAME
    config_hash, so INV-14 stamped an incomplete identity. The descriptor
    now carries content_sha256 over the calendar's COMPLETE semantics, and
    it rides BOTH the config hash and the payload (RED before R2-P1-b: the
    two trials below were identical in hash AND descriptor)."""
    from datetime import timedelta

    from tree_options.data.real_overlay import RealSessionCalendar

    _overlay, calendar, _snap, _ds = world
    sessions = calendar.sessions()
    # one interior session swapped for the adjacent Sunday (>= 3 days before
    # its successor), far past the world's 2018 span so fills never touch it:
    # same count, same first/last, ONE session of content different
    index = next(
        i for i in range(1000, len(sessions) - 1) if (sessions[i + 1] - sessions[i]).days >= 3
    )
    swapped = list(sessions)
    swapped[index] = swapped[index] + timedelta(days=2)
    baseline = RealSessionCalendar(sessions, frozenset())
    doctored = RealSessionCalendar(tuple(swapped), frozenset())
    assert _lossy_fields(baseline) == _lossy_fields(doctored)
    assert baseline.sessions() != doctored.sessions()
    base = _run_trial(world, protocol, tmp_path, tag="cal_id_base", execution_calendar=baseline)
    other = _run_trial(world, protocol, tmp_path, tag="cal_id_swap", execution_calendar=doctored)
    assert base["stamp"]["config_hash"] != other["stamp"]["config_hash"]
    assert base["payload"]["execution_calendar"] != other["payload"]["execution_calendar"]
    differing = {
        key
        for key in base["payload"]["execution_calendar"]
        if base["payload"]["execution_calendar"][key] != other["payload"]["execution_calendar"][key]
    }
    assert differing == {"content_sha256"}, (
        "one interior session of calendar content is a trial-identity change"
        " the descriptor must carry — only the content hash may differ"
    )


def test_a_calendar_differing_by_its_early_close_set_is_a_different_trial_identity(
    world, protocol, tmp_path
) -> None:
    """(R2-P1-b) The same construction for the EARLY-CLOSE dimension: the
    identical session tuple with one extra early close is a semantically
    different calendar (its 13:00 sessions are part of its authority), so
    its config hash and descriptor must differ even though every lossy
    field agrees (RED before R2-P1-b: identical hash and descriptor)."""
    from tree_options.data.real_overlay import RealSessionCalendar

    _overlay, calendar, _snap, _ds = world
    sessions = calendar.sessions()
    # the marked session sits far past the world's span, so no order or fill
    # is ever decided on it — content identity is the ONLY difference
    marked = sessions[1500]
    baseline = RealSessionCalendar(sessions, frozenset())
    doctored = RealSessionCalendar(sessions, frozenset({marked}))
    assert _lossy_fields(baseline) == _lossy_fields(doctored)
    assert baseline.sessions() == doctored.sessions()
    base = _run_trial(world, protocol, tmp_path, tag="cal_ec_base", execution_calendar=baseline)
    other = _run_trial(world, protocol, tmp_path, tag="cal_ec_marked", execution_calendar=doctored)
    assert base["stamp"]["config_hash"] != other["stamp"]["config_hash"]
    differing = {
        key
        for key in base["payload"]["execution_calendar"]
        if base["payload"]["execution_calendar"][key] != other["payload"]["execution_calendar"][key]
    }
    assert differing == {"content_sha256"}


# ---- (R3-P1-1, Codex round 3) class identity is trial identity ----------------------


def test_a_subclassed_decision_calendar_is_a_different_trial_identity(
    world, protocol, tmp_path
) -> None:
    """(R3-P1-1; re-scoped by R4-P1) `_calendar_descriptor` rides
    `calendar_content_sha256`, which hashed only the calendar's DATA — so a
    `StaticSessionCalendar` SUBCLASS with identical sessions and early
    closes was the SAME trial identity as the base class (same config hash,
    same descriptor) even though its methods were free to disagree with its
    data (RED before R3-P1-1: identical hash AND descriptor). The digest
    names the concrete class, so the twin grid is a different identity with
    every lossy field and every data field agreeing — pinned here at the
    descriptor, the identity surface.

    (R4-P1 re-scope) This test USED to run the twin as a trial and pin the
    config-hash difference — a configuration where the surface's disclosed
    decision-calendar authority (the overlay calendar) is NOT the stamped
    twin grid. That is exactly the unwired-surface-under-a-grid-stamped-
    trial configuration R4-P1's boundary refuses, so the twin trial no
    longer exists to be hashed: its identity difference is now pinned as a
    REFUSAL at the boundary (the digest difference is load-bearing), and
    lane 1 has exactly one legal grid — the overlay's own calendar — so a
    legal twin trial is structurally impossible by design. The descriptor
    comparison keeps the identity pin; the refusal keeps the trial-level
    one; neither is weakened."""
    from tree_options.data.real_overlay import RealSessionCalendar
    from tree_options.time.calendar import StaticSessionCalendar, calendar_content_sha256
    from tree_options.trials.options_run import _calendar_descriptor

    class _TwinDecisionCalendar(StaticSessionCalendar):
        """IDENTICAL committed data, ZERO overrides — a different WHO."""

    _overlay, calendar, _snap, _ds = world
    twin = _TwinDecisionCalendar(
        REPO_ROOT / "data" / "calendar" / "nyse_sessions_2018_01_02_2026_12_31.json",
        REPO_ROOT / "data" / "calendar" / "nyse_sessions_2018_01_02_2026_12_31.sha256",
    )
    assert twin.sessions() == calendar.sessions()
    assert twin.early_close_sessions() == calendar.early_close_sessions()
    assert _lossy_fields(twin) == _lossy_fields(calendar)
    assert calendar_content_sha256(twin) != calendar_content_sha256(calendar)
    base_descriptor = _calendar_descriptor(calendar)
    twin_descriptor = _calendar_descriptor(twin)
    differing = {key for key in base_descriptor if base_descriptor[key] != twin_descriptor[key]}
    assert differing == {"content_sha256"}, (
        "a subclassed decision grid is a trial-identity change only the"
        " content hash carries — every disclosed field else agrees"
    )

    # (R4-P1) the twin's identity difference is LOAD-BEARING at the boundary:
    # an unwired base surface (authority = the overlay calendar) under a
    # twin-stamped trial refuses before registration — same NAME, same
    # sessions, same early closes, only the concrete class (the digest)
    # differs, so an identity comparison weaker than the content digest
    # would let this configuration run
    fixed_execution = RealSessionCalendar(calendar.sessions(), frozenset())
    with pytest.raises(ValueError, match="decision-calendar authority"):
        _run_trial(
            world,
            protocol,
            tmp_path,
            tag="cal_cls_twin",
            execution_calendar=fixed_execution,
            decision_calendar=twin,
        )
    assert trial_count(tmp_path / "cal_cls_twin.db") == 0


def test_an_overriding_decision_calendar_is_a_different_identity(world) -> None:
    """(R3-P1-1) The Codex probe shape at the descriptor: an `ordinal()+1`
    subclass reports identical data and identical lossy fields, so before the
    class identity entered the payload its descriptor was INDISTINGUISHABLE
    from the base grid's — the shifted liquidity window it would enumerate
    was invisible to trial identity. Not run as a trial (a shifted ordinal
    moves fold enumeration itself); the descriptor is the identity surface."""
    from tree_options.time.calendar import StaticSessionCalendar, calendar_content_sha256
    from tree_options.trials.options_run import _calendar_descriptor

    class _OrdinalShiftedGrid(StaticSessionCalendar):
        def ordinal(self, d):
            return super().ordinal(d) + 1

    _overlay, calendar, _snap, _ds = world
    shifted = _OrdinalShiftedGrid(
        REPO_ROOT / "data" / "calendar" / "nyse_sessions_2018_01_02_2026_12_31.json",
        REPO_ROOT / "data" / "calendar" / "nyse_sessions_2018_01_02_2026_12_31.sha256",
    )
    assert shifted.sessions() == calendar.sessions()
    assert shifted.early_close_sessions() == calendar.early_close_sessions()
    assert _lossy_fields(shifted) == _lossy_fields(calendar)
    assert calendar_content_sha256(shifted) != calendar_content_sha256(calendar)
    base_descriptor = _calendar_descriptor(calendar)
    shifted_descriptor = _calendar_descriptor(shifted)
    differing = {key for key in base_descriptor if base_descriptor[key] != shifted_descriptor[key]}
    assert differing == {"content_sha256"}


# ---- (R4-P1, Codex round 4) the surface's decision-calendar authority BOUND ----------


def _run_era_trial(
    era_world,
    protocol,
    tmp_path,
    *,
    tag: str,
    surface,
    scored: tuple[ScoredLabel, ...] | None = None,
    strategy_config: OptionsStrategyConfig | None = None,
    calendar=None,
):
    """The ruled era configuration — `test_dual_calendar_ruled_geometry_
    yields_the_era_folds`' own — parameterized ONLY by the surface: the
    exact trial configuration the R4-P1 boundary judges, so the unwired and
    the wired surface run under byte-identical everything else. (R6-P1)
    `scored`/`strategy_config` override the ruled rows/config for the
    call-sequence probe, whose divergence session is an early-close Friday
    the ruled scored set never decides on. (R7-P1) `calendar` swaps the
    stamped DECISION grid for a probe calendar (the surface must disclose
    the SAME object, or the R4-P1 digest refuses); the honest `grid` still
    builds every fixture-side read — the scored rows, the decision set, the
    bar's availability — so only the runner's own reads hit the probe."""
    from tree_options.protocol.era_profile import real_lane_split_override

    grid, overlay = era_world
    stamped = grid if calendar is None else calendar
    world_id = overlay.spec.world_id
    scored = _era_scored_rows(grid) if scored is None else scored
    decision_sessions = grid.sessions()[: grid.sessions().index(date(2026, 5, 1)) + 1]
    registry = TrialRegistry(tmp_path / f"{tag}.db")
    try:
        return run_options_trial(
            dataset=_RealLaneDataset(  # type: ignore[arg-type]
                snapshot_id=world_id,
                bars=(
                    BarRecord(
                        security_id="SPY",
                        session=decision_sessions[-1],
                        open=Decimal("600.00"),
                        high=Decimal("600.00"),
                        low=Decimal("600.00"),
                        close=Decimal("600.00"),
                        volume=1_000_000,
                        source="spot-proxy/declared",
                        source_record_id="SPY-20260501",
                        source_row_hash="0" * 64,
                        snapshot_id=world_id,
                        available_at=grid.session_close(decision_sessions[-1]),
                    ),
                ),
            ),
            surface=surface,  # type: ignore[arg-type]
            calendar=stamped,  # type: ignore[arg-type]
            execution_calendar=overlay.calendar,
            protocol=protocol,
            world_id=world_id,
            arm="A",
            strategy_config=(
                strategy_config if strategy_config is not None else OptionsStrategyConfig()
            ),
            scored=scored,
            model_family="null-sha256/1",
            model_sha256=None,
            hypothesis=f"unit/{tag}",
            decision_sessions=decision_sessions,
            options_manifest_hash="0" * 64,
            registry=registry,
            artifacts_dir=tmp_path / tag,
            repo=REPO_ROOT,
            clock=FIXED_CLOCK,
            split_override=real_lane_split_override(),
            liquidity_lane=2,
            score_seed="t-null/era",
            allow_dirty=True,
        )
    finally:
        registry.close()


def test_an_unwired_surface_refuses_a_grid_stamped_trial_before_registration(
    era_world, protocol, tmp_path
) -> None:
    """(R4-P1, Codex round 4) The verified defect: `run_options_trial`
    referenced `decision_calendar` only as a stamped descriptor — it never
    verified that `surface.decision_close()` is answered by that calendar.
    An UNWIRED VwapPitSurface (constructor default None -> the overlay's
    nominal 16:00) under an era-grid-stamped trial therefore ran silently,
    deciding at 16:00 on the grid's 13:00 early-close sessions: different
    counters under the same declared configuration (INV-02 + INV-14 at the
    trial boundary). The boundary now BINDS the disclosed authority to the
    stamped calendar by COMPLETE content identity and refuses BEFORE
    registration (RED before R4-P1: this trial registered, ran, and
    completed — no refusal existed)."""
    from tree_options.data.vwap_pit_surface import VwapPitSurface
    from tree_options.time.calendar import calendar_content_sha256

    grid, overlay = era_world
    unwired = VwapPitSurface(overlay)
    # the mismatch is real: the surface's EFFECTIVE authority is the overlay's
    # daily calendar (nominal 16:00 closes), not the early-close-aware grid
    assert calendar_content_sha256(unwired.decision_calendar) != calendar_content_sha256(grid)
    with pytest.raises(ValueError, match="decision-calendar authority"):
        _run_era_trial(era_world, protocol, tmp_path, tag="r4_unwired", surface=unwired)
    # refused BEFORE registration: no record, no artifact
    assert trial_count(tmp_path / "r4_unwired.db") == 0
    assert not (tmp_path / "r4_unwired").exists()


def test_a_wired_surface_binds_to_the_stamped_grid_and_runs_unchanged(
    era_world, protocol, tmp_path
) -> None:
    """(R4-P1, the control) A surface wired to the stamped grid —
    `decision_calendar == grid`, the era test's own construction — binds
    (digests equal) and runs UNCHANGED: the ruled geometry, the pinned
    per-fold windows, the known 0.2.1 earnings refusal, and the identical
    configuration hash the era configuration has always produced. The bind
    is invisible to trial identity; only the unwired configuration is new
    (it is a refusal)."""
    import json

    from tree_options.data.vwap_pit_surface import VwapPitSurface
    from tree_options.time.calendar import calendar_content_sha256

    grid, overlay = era_world
    lf = protocol.option_candidate_defaults.liquidity_volume_flow
    assert lf is not None
    wired = VwapPitSurface(
        overlay,
        decision_calendar=grid,
        underlying_liquidity_term=lf.underlying_liquidity_term,
    )
    assert wired.decision_calendar is grid
    assert calendar_content_sha256(wired.decision_calendar) == calendar_content_sha256(grid)
    result = _run_era_trial(era_world, protocol, tmp_path, tag="r4_wired", surface=wired)
    body = json.loads(result.artifact_path.read_text(encoding="utf-8"))
    assert result.n_folds == 3
    per_fold = body["payload"]["per_fold"]
    assert [f["n_test_sessions"] for f in per_fold] == [13, 13, 13]
    assert [f["test_window"]["end"] for f in per_fold] == [
        "2025-10-24",
        "2026-01-23",
        "2026-05-01",
    ]
    # the positions zero is the KNOWN 0.2.1 earnings refusal, never a bind
    # failure — the same pinned counters the era test owns
    assert body["payload"]["pooled"]["n_positions"] == 0
    rules = body["payload"]["counters"]["rule_histogram"]
    assert rules.get("earnings_span", {}).get("NOT_EVALUABLE", 0) > 0
    assert "decision_coherence" not in rules
    assert not any(
        code.startswith("BAR_")
        for code in body["payload"]["counters"]["rejections"].get("entry_fill_rejections", {})
    )


def test_the_base_surface_discloses_the_runners_calendar_and_lane_1_stays_byte_identical(
    world, protocol, tmp_path
) -> None:
    """(R4-P1, lane-1/synthetic byte-identity) The base surface's own calendar
    IS the runner's calendar — one object, so the disclosed authority's
    digest equals the stamped one and the bound trial is byte-identical to
    the unbound configuration it replaces: same config hash, same payload
    (the guard is a no-op exactly where the design says it must be)."""
    import json

    from tree_options.time.calendar import calendar_content_sha256

    overlay, calendar, _snap, _ds = world
    surface = OptionPitSurface(overlay)
    assert surface.decision_calendar is calendar
    assert calendar_content_sha256(surface.decision_calendar) == calendar_content_sha256(calendar)
    bound = _run_trial(world, protocol, tmp_path, tag="r4_lane1_bound")
    reference = _run_trial(
        world, protocol, tmp_path, tag="r4_lane1_reference", execution_calendar=calendar
    )
    assert bound["stamp"]["config_hash"] == reference["stamp"]["config_hash"]
    assert json.dumps(bound["payload"], sort_keys=True) == json.dumps(
        reference["payload"], sort_keys=True
    )


def test_a_surface_that_cannot_disclose_its_authority_refuses_cleanly(
    world, protocol, tmp_path
) -> None:
    """(R4-P1, the attribute-absent branch) A surface with no
    `decision_calendar` property cannot be BOUND, and the refusal is the
    runner's own clean ValueError — never a raw AttributeError escaping the
    boundary. That is the invariant's teeth: a synthetic surface is never
    SKIPPED past the bind, it must expose its authority honestly (the
    repo's two real surfaces now do; a double that does not is refused
    before registration)."""
    overlay, calendar, snapshot, dataset = world

    class _NonDisclosingSurface:
        """The lane-1 surface minus the disclosure — nothing else differs."""

        def __init__(self, inner: OptionPitSurface) -> None:
            self._inner = inner

        def __getattr__(self, name: str):
            if name == "decision_calendar":
                raise AttributeError(name)  # exactly the absent property
            return getattr(self._inner, name)

    opaque = _NonDisclosingSurface(OptionPitSurface(overlay))
    scored = _scored(world, OptionPitSurface(overlay))
    registry = TrialRegistry(tmp_path / "opaque.db")
    try:
        with pytest.raises(ValueError, match="cannot disclose its decision-calendar authority"):
            run_options_trial(
                dataset=dataset,
                surface=opaque,  # type: ignore[arg-type]
                calendar=calendar,
                protocol=protocol,
                world_id=snapshot.snapshot_id,
                arm="A",
                strategy_config=OptionsStrategyConfig(),
                scored=scored,
                model_family="fixture:v1",
                model_sha256=None,
                hypothesis="must be refused: no disclosed authority",
                decision_sessions=tuple(sorted({row.session for row in scored})),
                options_manifest_hash="0" * 64,
                registry=registry,
                artifacts_dir=tmp_path / "opaque",
                repo=REPO_ROOT,
                clock=FIXED_CLOCK,
                split_override=SPLIT,
                allow_dirty=True,
            )
    finally:
        registry.close()
    assert trial_count(tmp_path / "opaque.db") == 0


# ---- (R5-P1, Codex round 5) the BEHAVIORAL binding: decision_close itself ----------


class _LyingDisclosureSurface(VwapPitSurface):
    """Codex round 5's reproduced probe class: overrides ONLY
    `decision_calendar` to return the stamped grid — exactly the input the
    R4-P1 digest boundary hashes — while the INHERITED `decision_close()`
    keeps answering from the UNWIRED overlay calendar (the constructor was
    called without one). The disclosure passes the digest guard and lies
    about behavior; only the R5-P1 equality loop catches it."""

    def __init__(self, overlay, grid) -> None:
        super().__init__(overlay)  # deliberately NOT decision_calendar=grid
        self._lying_grid = grid

    @property
    def decision_calendar(self):
        return self._lying_grid


class _WrongInstantSurface(VwapPitSurface):
    """The mismatch branch's owner: discloses the stamped grid AND answers
    every decision session — at the WRONG instant (a no-early-close twin
    over the grid's own sessions, exactly what the unwired overlay calendar
    says wherever it has coverage). The digest passes and `decision_close()`
    never raises: ONLY the instant equality can catch this surface."""

    def __init__(self, overlay, grid) -> None:
        super().__init__(overlay)
        self._lying_grid = grid
        self._no_early_closes = RealSessionCalendar(grid.sessions(), frozenset())

    @property
    def decision_calendar(self):
        return self._lying_grid

    def decision_close(self, decision_session: date) -> datetime:
        return self._no_early_closes.session_close(decision_session)


class _BehaviorallyBoundSurface(VwapPitSurface):
    """The passing shape R5-P1 exists to allow: a subclass that ALSO
    overrides `decision_close` to GENUINELY answer from the stamped grid.
    Behaviorally bound is bound — which override site supplies the answer
    is not the invariant."""

    def __init__(self, overlay, grid) -> None:
        super().__init__(overlay)
        self._bound_grid = grid

    @property
    def decision_calendar(self):
        return self._bound_grid

    def decision_close(self, decision_session: date) -> datetime:
        return self._bound_grid.session_close(decision_session)


def test_a_lying_disclosure_surface_refuses_before_registration(
    era_world, protocol, tmp_path
) -> None:
    """(R5-P1, Codex round 5 — the reproduced probe) The R4-P1 digest bound
    the calendar the surface DISCLOSES; nothing verified the method the
    decisions actually call (`strategy.py` reads `decision_close()`).
    A VwapPitSurface subclass overriding ONLY `decision_calendar` to return
    the stamped grid passes the digest — the disclosure is self-attested —
    while the inherited `decision_close()` still answers from the unwired
    overlay calendar: the trial registered under a configuration whose
    effective decision behavior differs (INV-02 + INV-14 at the trial
    boundary). The boundary now additionally requires
    `surface.decision_close(s) == calendar.session_close(s)` for EVERY
    decision session and refuses, naming the first session the surface
    cannot answer the stamped calendar's way (here the grid's first
    Friday, which the era overlay's capture never covered — an even
    stronger disagreement than Codex's 13:00-vs-16:00 probe; RED before
    R5-P1: the digest matched and the trial REGISTERED, ran, and
    completed on the unwired 16:00 fallback)."""
    from tree_options.time.calendar import calendar_content_sha256

    grid, overlay = era_world
    lying = _LyingDisclosureSurface(overlay, grid)
    # no masking: the R4-P1 digest guard PASSES on this surface — only the
    # behavioral equality can refuse it
    assert calendar_content_sha256(lying.decision_calendar) == calendar_content_sha256(grid)
    with pytest.raises(ValueError, match=r"decision_close\(\) cannot answer decision session"):
        _run_era_trial(era_world, protocol, tmp_path, tag="r5_lying", surface=lying)
    # refused BEFORE registration: no record, no artifact
    assert trial_count(tmp_path / "r5_lying.db") == 0
    assert not (tmp_path / "r5_lying").exists()


def test_a_wrong_instant_surface_refuses_naming_both_instants(
    era_world, protocol, tmp_path
) -> None:
    """(R5-P1, the instant-mismatch branch — the M313 owner) A surface that
    discloses the stamped grid AND answers every decision session — at the
    WRONG instant (16:00 where the grid's early-close Friday closes 13:00,
    exactly the unwired overlay calendar's answer where it has coverage) —
    passes the digest and never raises: only the instant equality catches
    it, and the refusal names the first mismatching session and BOTH
    instants. 2024-11-29 is the grid's first early-close Friday inside the
    decision set (RED before R5-P1: the trial REGISTERED and ran, deciding
    three hours after the true close on every early-close Friday)."""
    from tree_options.time.calendar import calendar_content_sha256

    grid, overlay = era_world
    wrong = _WrongInstantSurface(overlay, grid)
    assert calendar_content_sha256(wrong.decision_calendar) == calendar_content_sha256(grid)
    # the disagreement is real and the surface CAN answer the session
    assert wrong.decision_close(date(2024, 11, 29)) != grid.session_close(date(2024, 11, 29))
    with pytest.raises(ValueError, match=r"2024-11-29.*18:00:00\+00:00.*21:00:00\+00:00") as ei:
        _run_era_trial(era_world, protocol, tmp_path, tag="r5_wronginstant", surface=wrong)
    message = str(ei.value)
    # the refusal names the session, the stamped close, AND the surface's
    # answer — both instants, not just the fact of a mismatch
    assert grid.session_close(date(2024, 11, 29)).isoformat() in message
    assert wrong.decision_close(date(2024, 11, 29)).isoformat() in message
    assert trial_count(tmp_path / "r5_wronginstant.db") == 0
    assert not (tmp_path / "r5_wronginstant").exists()


def test_a_behaviorally_bound_subclass_runs_unchanged(era_world, protocol, tmp_path) -> None:
    """(R5-P1, the point of binding BEHAVIOR) A subclass that ALSO overrides
    `decision_close` to genuinely answer from the stamped grid is
    BEHAVIORALLY bound and runs UNCHANGED: the same pinned era geometry,
    per-fold windows, and known 0.2.1 earnings refusal as the wired
    construction (`test_a_wired_surface_binds_to_the_stamped_grid_and_runs_
    unchanged`). The invariant is what the trial's decisions actually read,
    not which override site supplies it."""
    import json

    grid, overlay = era_world
    bound = _BehaviorallyBoundSurface(overlay, grid)
    result = _run_era_trial(era_world, protocol, tmp_path, tag="r5_bound", surface=bound)
    assert result.n_folds == 3
    body = json.loads(result.artifact_path.read_text(encoding="utf-8"))
    per_fold = body["payload"]["per_fold"]
    assert [f["n_test_sessions"] for f in per_fold] == [13, 13, 13]
    assert [f["test_window"]["end"] for f in per_fold] == [
        "2025-10-24",
        "2026-01-23",
        "2026-05-01",
    ]
    # the positions zero is the KNOWN 0.2.1 earnings refusal, never a bind
    # failure — the same pinned counters the wired era test owns
    assert body["payload"]["pooled"]["n_positions"] == 0
    rules = body["payload"]["counters"]["rule_histogram"]
    assert rules.get("earnings_span", {}).get("NOT_EVALUABLE", 0) > 0
    assert not any(
        code.startswith("BAR_")
        for code in body["payload"]["counters"]["rejections"].get("entry_fill_rejections", {})
    )


# ---- (R6-P1, Codex round 6) the FROZEN decision instants: stateful surfaces ----------


class _StatefulLyingSurface(VwapPitSurface):
    """Codex round 6's reproduced probe class: discloses the stamped grid
    (so the R4-P1 digest binds) and answers each session's FIRST
    `decision_close()` call with the grid's own close (so the R5-P1
    equality loop passes), then every LATER call with the no-early-close
    twin's 16:00 — exactly the call-sequence shape a single-shot boundary
    comparison cannot see: `pre_registration_equal=True`,
    `runtime_equal=False`."""

    def __init__(self, overlay, grid) -> None:
        super().__init__(overlay)
        self._lying_grid = grid
        self._no_early_closes = RealSessionCalendar(grid.sessions(), frozenset())
        self._answered: set[date] = set()

    @property
    def decision_calendar(self):
        return self._lying_grid

    def decision_close(self, decision_session: date) -> datetime:
        if decision_session not in self._answered:
            self._answered.add(decision_session)
            return self._lying_grid.session_close(decision_session)
        return self._no_early_closes.session_close(decision_session)


def _r6_probe_scored(grid) -> tuple[ScoredLabel, ...]:
    """The call-sequence probe's scored rows: the ruled set PLUS a decision
    on 2025-11-28 — the grid's early-close Friday inside fold 2's test
    window, which the ruled scored set never decides on (no era expiry sits
    in its [30, 60] DTE band). Under the probe's [15, 30] band the 2025-12
    -19 expiry IS in band from that Friday, so the trial actually builds a
    candidate there and the 13:00/16:00 disagreement reaches
    `candidate_snapshot` -> the filter's decision_coherence rule."""
    from tree_options.trials.null_score import null_score

    rows = list(_era_scored_rows(grid))
    rows.append(
        ScoredLabel(
            security_id="SPY",
            session=date(2025, 11, 28),
            score=null_score(seed="t-null/era", session=date(2025, 11, 28), security_id="SPY"),
            label=0.01,
        )
    )
    return tuple(rows)


def test_a_stateful_lying_surface_runs_identically_to_the_wired_surface(
    era_world, protocol, tmp_path
) -> None:
    """(R6-P1, Codex round 6 — the reproduced call-sequence probe) The
    boundary VERIFIES `decision_close()` once per session before
    registration; the runtime then called the same OVERRIDABLE method again
    (`strategy.py`'s candidate/expiry/strike reads and the surfaces'
    `candidate_snapshot`), so a STATEFUL subclass — first call right, later
    calls 16:00 — passed every preflight comparison while the wrong instant
    reached build_candidates: on the early-close Friday 2025-11-28 the
    probe's candidate snapshot is stamped 21:00Z where the stamped grid
    closes 18:00Z, the filter's decision_coherence rule answers
    NOT_EVALUABLE, and the trial's counters and artifact DIVERGE from a
    correctly-wired surface's run under the same declared configuration
    (INV-02 + INV-14 at runtime). The runner now FREEZES the
    boundary-verified instants and the run consumes ONLY those, so the
    stateful surface cannot express itself: both trials below are
    byte-identical (RED before R6-P1: the payloads and counters diverge —
    the wrong instant reached build_candidates)."""
    import json

    from tree_options.time.calendar import calendar_content_sha256

    grid, overlay = era_world
    lf = protocol.option_candidate_defaults.liquidity_volume_flow
    assert lf is not None
    wired = VwapPitSurface(
        overlay,
        decision_calendar=grid,
        underlying_liquidity_term=lf.underlying_liquidity_term,
    )
    liar = _StatefulLyingSurface(overlay, grid)
    # NO MASKING — the probe passes every guard R4-P1/R5-P1 own: the digest
    # binds (the disclosure is the stamped grid) and the FIRST call answers
    # the stamped close; only the frozen map can catch the later calls.
    probe = _StatefulLyingSurface(overlay, grid)
    assert calendar_content_sha256(probe.decision_calendar) == calendar_content_sha256(grid)
    assert probe.decision_close(date(2025, 11, 28)) == grid.session_close(date(2025, 11, 28))
    assert probe.decision_close(date(2025, 11, 28)) != grid.session_close(date(2025, 11, 28))
    # the probe's configuration: the ruled geometry, the [15, 30] DTE band
    # that makes 2025-11-28's candidate reachable, and the scored row on it
    probe_config = OptionsStrategyConfig(dte_min=15, target_dte=21, dte_max=30)
    probe_scored = _r6_probe_scored(grid)
    wired_result = _run_era_trial(
        era_world,
        protocol,
        tmp_path,
        tag="r6_wired",
        surface=wired,
        scored=probe_scored,
        strategy_config=probe_config,
    )
    lying_result = _run_era_trial(
        era_world,
        protocol,
        tmp_path,
        tag="r6_stateful",
        surface=liar,
        scored=probe_scored,
        strategy_config=probe_config,
    )
    assert wired_result.n_folds == lying_result.n_folds == 3
    assert wired_result.n_positions == lying_result.n_positions
    wired_body = json.loads(wired_result.artifact_path.read_text(encoding="utf-8"))
    lying_body = json.loads(lying_result.artifact_path.read_text(encoding="utf-8"))
    assert wired_body["stamp"]["config_hash"] == lying_body["stamp"]["config_hash"]
    assert json.dumps(wired_body["payload"], sort_keys=True) == json.dumps(
        lying_body["payload"], sort_keys=True
    )
    # the counters are the identity the divergence moved (RED showed the
    # liar's decision_coherence NOT_EVALUABLE rows the wired run lacks)
    assert wired_body["payload"]["counters"] == lying_body["payload"]["counters"]
    # the run never consulted the surface's method again: the only calls the
    # stateful surface ever answered are the boundary loop's one-per-session
    decision_sessions = grid.sessions()[: grid.sessions().index(date(2026, 5, 1)) + 1]
    assert liar._answered == set(decision_sessions)


def test_the_frozen_decision_map_refuses_unmapped_sessions_fail_closed(era_world) -> None:
    """(R6-P1, the fail-closed horn; R7-P2, the bind's shape) The frozen map
    carries EXACTLY the sessions the boundary verified — every decision-side
    read of the run answers from it, so a session outside the verified set can
    never silently fall back to the surface's overridable method: the bound
    instance refuses, naming the session (the guard is what makes "the runtime
    consumes the frozen map" total rather than best-effort). (R7-P2) the bind
    is a GENUINE instance of the same concrete class — same `type`, real
    `isinstance`, ordinary class-level dispatch everywhere else."""
    from tree_options.trials.options_run import _bind_decision_surface

    grid, overlay = era_world
    first, second = grid.sessions()[0], grid.sessions()[1]
    wired = VwapPitSurface(overlay, decision_calendar=grid)
    bound = _bind_decision_surface(wired, {first: grid.session_close(first)})
    # the bind is the same concrete class, carrying the real state
    assert type(bound) is type(wired)
    assert isinstance(bound, VwapPitSurface)
    assert bound.snapshot_id == wired.snapshot_id
    assert bound.decision_calendar is wired.decision_calendar
    # a mapped session answers the frozen instant, not the surface
    assert bound.decision_close(first) == grid.session_close(first)
    with pytest.raises(ValueError, match="no frozen decision instant for session"):
        bound.decision_close(second)


# ---- (R7-P1, Codex round 7) the VERIFIED instant, never a fresh read ----------


class _ThirdReadLiarCalendar:
    """Codex round 7's probe CALENDAR: `session_close` answers the fixture
    close on each session's FIRST TWO calls and the no-early-close twin's
    16:00 on every later call, counting as it goes.

    The boundary reads the stamped calendar exactly twice per decision
    session — `declared_close = calendar.session_close(session)`, then the
    wired surface's `decision_close(session)` behind the very same object —
    and the digest that binds it hashes sessions + early closes + the class,
    never per-session METHOD STATE, so a calendar that turns wrong on the
    third read passes the entire preflight. Whatever reads it NEXT is then
    fed the wrong instant, and which read that is decides the defect: at
    HEAD the freeze's own re-read is the third."""

    def __init__(self, grid) -> None:
        self._grid = grid
        self._no_early_closes = RealSessionCalendar(grid.sessions(), frozenset())
        self.close_calls: dict[date, int] = {}

    def sessions(self):
        return self._grid.sessions()

    def early_close_sessions(self):
        return self._grid.early_close_sessions()

    def is_session(self, d):
        return self._grid.is_session(d)

    def ordinal(self, d):
        return self._grid.ordinal(d)

    def nth_after(self, d, n):
        return self._grid.nth_after(d, n)

    def session_open(self, d):
        return self._grid.session_open(d)

    def session_close(self, d):
        self.close_calls[d] = self.close_calls.get(d, 0) + 1
        if self.close_calls[d] <= 2:
            return self._grid.session_close(d)
        return self._no_early_closes.session_close(d)

    def contains_instant(self, d, ts):
        return self._grid.contains_instant(d, ts)


def test_the_freeze_consumes_the_verified_instant_never_a_third_calendar_read(
    era_world, protocol, tmp_path
) -> None:
    """(R7-P1, Codex round 7 — the call-count horn) The preflight loop
    VERIFIES `declared_close = calendar.session_close(session)` against the
    surface's answer and then DISCARDED it: the freeze rebuilt the map with
    a SECOND, unverified read of the same overridable method. A calendar
    that answers the fixture close on a session's first two calls and 16:00
    thereafter therefore passes the whole preflight while the freeze stores
    the THIRD, never-compared answer. The fix stores the value that was
    actually compared, so the calendar is never re-read for the freeze: the
    sessions before the first scored row carry no candidates, no folds and
    no fills, and the ONLY calendar reads they can ever attract are the
    preflight's two — exactly two calls pins that the freeze added no third
    (RED before R7-P1: the freeze IS the third read)."""
    grid, overlay = era_world
    lf = protocol.option_candidate_defaults.liquidity_volume_flow
    assert lf is not None
    probe = _ThirdReadLiarCalendar(grid)
    wired = VwapPitSurface(
        overlay,
        decision_calendar=probe,
        underlying_liquidity_term=lf.underlying_liquidity_term,
    )
    probe_scored = _r6_probe_scored(grid)
    decision_sessions = grid.sessions()[: grid.sessions().index(date(2026, 5, 1)) + 1]
    # NO MASKING — the probe passes every guard the boundary owns: it
    # discloses itself (the digest binds the probe to the probe), answers
    # both preflight reads with the fixture close, and never raises. Only
    # the freeze's read count can catch the third call.
    assert probe.close_calls == {}
    result = _run_era_trial(
        era_world,
        protocol,
        tmp_path,
        tag="r7_mutcal",
        surface=wired,
        calendar=probe,
        scored=probe_scored,
        strategy_config=OptionsStrategyConfig(dte_min=15, target_dte=21, dte_max=30),
    )
    assert result.n_folds == 3
    untouched = [s for s in decision_sessions if s < min(r.session for r in probe_scored)]
    assert len(untouched) >= 10  # the first fold's train window: nothing else reads these
    assert {probe.close_calls[s] for s in untouched} == {2}


def test_a_downstream_re_read_of_the_stateful_calendar_fails_closed(
    era_world, protocol, tmp_path
) -> None:
    """(R7-P1, Codex round 7 — the fail-closed horn; SUPERSEDED SHAPE under
    R8-P1, Codex round 8) R7 pinned the transitional guard: the runtime
    decision instant was the VERIFIED close while the candidate filter's
    `expected_close` still re-read the stamped calendar DIRECTLY — never
    the frozen map — so on the early-close Friday 2025-11-28 the two
    disagreed (13:00 verified vs the calendar's third-and-later 16:00) and
    the filter answered decision_coherence NOT_EVALUABLE: the trial FAILED
    CLOSED on the divergent instant instead of deciding on it. R8-P1 bound
    the CALENDAR too, so the filter's expected close now answers the SAME
    frozen verified map the snapshot's decision_at answers — the
    disagreement the transitional guard caught can no longer be EXPRESSED.
    Under the third-read liar (honest through read 2, 16:00 from read 3 —
    a strictly weaker liar than R8's fourth-read probe, which survives one
    more read) the run is now behaviorally IDENTICAL to the honest-grid
    run, no coherence refusal exists, and nothing is ever decided on the
    calendar's later answers: the stronger invariant replaces the refusal.
    (The honest-grid and probe runs' config hashes legitimately differ —
    the dual-calendar disclosure names the calendar's CLASS — so the
    identity here is over the run's BEHAVIOR keys, not the descriptors.)"""
    import json

    grid, overlay = era_world
    lf = protocol.option_candidate_defaults.liquidity_volume_flow
    assert lf is not None
    probe = _ThirdReadLiarCalendar(grid)
    wired = VwapPitSurface(
        overlay,
        decision_calendar=probe,
        underlying_liquidity_term=lf.underlying_liquidity_term,
    )
    honest = VwapPitSurface(
        overlay,
        decision_calendar=grid,
        underlying_liquidity_term=lf.underlying_liquidity_term,
    )
    probe_scored = _r6_probe_scored(grid)
    probe_config = OptionsStrategyConfig(dte_min=15, target_dte=21, dte_max=30)
    liar_result = _run_era_trial(
        era_world,
        protocol,
        tmp_path,
        tag="r7_failclosed",
        surface=wired,
        calendar=probe,
        scored=probe_scored,
        strategy_config=probe_config,
    )
    honest_result = _run_era_trial(
        era_world,
        protocol,
        tmp_path,
        tag="r7_failclosed_ref",
        surface=honest,
        scored=probe_scored,
        strategy_config=probe_config,
    )
    assert liar_result.n_folds == honest_result.n_folds == 3
    liar_body = json.loads(liar_result.artifact_path.read_text(encoding="utf-8"))
    honest_body = json.loads(honest_result.artifact_path.read_text(encoding="utf-8"))
    for key in ("counters", "fills_log", "per_fold", "pooled"):
        assert json.dumps(liar_body["payload"][key], sort_keys=True) == json.dumps(
            honest_body["payload"][key], sort_keys=True
        ), key
    # the transitional refusal is GONE — and nothing replaced it with a
    # decision on the calendar's later 16:00 answers: no coherence rows at
    # all, because both sides of the comparison answer the frozen map
    rules = liar_body["payload"]["counters"]["rule_histogram"]
    assert "decision_coherence" not in rules
    # the liar really would have answered differently on its third read —
    # proven on a SPENT twin so the assertion itself consumes no read of
    # the run's probe
    spent = _ThirdReadLiarCalendar(grid)
    assert spent.session_close(date(2025, 11, 28)) == grid.session_close(date(2025, 11, 28))
    assert spent.session_close(date(2025, 11, 28)) == grid.session_close(date(2025, 11, 28))
    assert spent.session_close(date(2025, 11, 28)) != grid.session_close(date(2025, 11, 28))
    # and nothing after the boundary ever consulted the run's probe: the
    # decision sessions stop at the preflight's two reads, no session
    # exceeds them
    decision_sessions = grid.sessions()[: grid.sessions().index(date(2026, 5, 1)) + 1]
    assert {probe.close_calls[s] for s in decision_sessions} == {2}
    assert max(probe.close_calls.values()) == 2


# ---- (R7-P2, Codex round 7) a GENUINE same-class bind, not a rebind wrapper ----


class _SuperDelegatingSurface(VwapPitSurface):
    """Codex round 7's P2 probe class: a semantically NEUTRAL subclass whose
    `candidate_snapshot` is exactly `super().candidate_snapshot(...)`. It
    changes nothing about what the run reads — subclassing is an explicitly
    accepted input (`options_run.py`'s own behaviorally-bound-subclass test)
    — and yet the wrapper's rebind executed the subclass's function with a
    `self` that is not an instance of it, so `super()` raised TypeError
    AFTER registration: a failed registered trial on a legal input."""

    def __init__(self, overlay, grid, liquidity_term) -> None:
        super().__init__(overlay, decision_calendar=grid, underlying_liquidity_term=liquidity_term)

    def candidate_snapshot(self, contract, decision_session):
        return super().candidate_snapshot(contract, decision_session)


def test_a_super_delegating_subclass_runs_byte_identically_to_the_base_surface(
    era_world, protocol, tmp_path
) -> None:
    """(R7-P2, Codex round 7 — the dispatch horn) `_BoundDecisionSurface.
    candidate_snapshot` ran `type(underlying).candidate_snapshot(self_wrapper,
    ...)`, and `self_wrapper` is NOT an instance of the underlying's class —
    so a subclass whose override is a semantically neutral
    `super().candidate_snapshot(...)` raised TypeError from inside the run,
    AFTER registration (a failed registered trial on an input the boundary
    explicitly accepts). The bind now produces a GENUINE instance of the
    same concrete class — `cls.__new__(cls)` plus a copy of the real state —
    so subclass dispatch, `super()` and `isinstance` are ordinary and the
    neutral subclass runs UNCHANGED: byte-identical payload and config hash
    to the base surface's run (RED before R7-P2: TypeError)."""
    import json

    grid, overlay = era_world
    lf = protocol.option_candidate_defaults.liquidity_volume_flow
    assert lf is not None
    wired = VwapPitSurface(
        overlay,
        decision_calendar=grid,
        underlying_liquidity_term=lf.underlying_liquidity_term,
    )
    neutral = _SuperDelegatingSurface(overlay, grid, lf.underlying_liquidity_term)
    wired_result = _run_era_trial(era_world, protocol, tmp_path, tag="r7_wired", surface=wired)
    neutral_result = _run_era_trial(era_world, protocol, tmp_path, tag="r7_super", surface=neutral)
    assert wired_result.n_folds == neutral_result.n_folds == 3
    assert wired_result.n_positions == neutral_result.n_positions
    wired_body = json.loads(wired_result.artifact_path.read_text(encoding="utf-8"))
    neutral_body = json.loads(neutral_result.artifact_path.read_text(encoding="utf-8"))
    assert wired_body["stamp"]["config_hash"] == neutral_body["stamp"]["config_hash"]
    assert json.dumps(wired_body["payload"], sort_keys=True) == json.dumps(
        neutral_body["payload"], sort_keys=True
    )
    # and the subclass really was dispatched: its override ran, not the base
    # class's method straight through the wrapper
    assert neutral.candidate_snapshot is not VwapPitSurface.candidate_snapshot


class _FrozenSeamProbingSurface(_StatefulLyingSurface):
    """Codex round 7's P2 delegation probe: a subclass overriding a DIFFERENT
    method (`eligible_as_of`, which `build_candidates` calls for every
    decision session) so that it reaches the decision seam through
    `self.decision_close(...)`. The base is the R6 stateful liar, so the
    underlying's own method answers the stamped close only on a session's
    FIRST call and 16:00 thereafter — exactly the wrong-answer underlying
    that distinguishes "the override resolved the FROZEN map" from "the
    override stayed bound to the underlying"."""

    def __init__(self, overlay, grid) -> None:
        super().__init__(overlay, grid)
        self.probed: dict[date, datetime] = {}

    def eligible_as_of(self, session):
        # the base's publication-wall sweep, anchored at the seam instead of
        # the overlay calendar: the wall sits at T+1 09:00, below either
        # close, so the visible session — and therefore the whole run — is
        # unchanged; only WHERE the instant came from moves
        decision_at = self.decision_close(session)
        self.probed[session] = decision_at
        for candidate in sorted(self._overlay.world_sessions(), reverse=True):
            if not self._overlay.has_any_file(candidate):
                continue
            if self._overlay.publication_of(candidate) <= decision_at:
                return self._overlay.eligible_on(candidate)
        return ()


def test_a_subclass_override_resolves_the_frozen_decision_close(
    era_world, protocol, tmp_path
) -> None:
    """(R7-P2, Codex round 7 — the delegation horn) The wrapper delegated
    unknown attributes with `__getattr__`, so a subclass's override of
    ANOTHER method stayed bound to the UNDERLYING — its internal
    `self.decision_close(...)` reached the underlying's unfrozen method, not
    the frozen map. The bind now copies the real state onto a same-class
    instance and installs `decision_close` as an INSTANCE attribute, which
    shadows the class method at EVERY `self.decision_close(...)` call site:
    the override below records exactly what it resolved, and every recorded
    instant is the grid's own close — including the early-close Friday
    2025-11-28's 13:00 ET, which the underlying's later calls answer 16:00
    (RED before R7-P2: every probe recorded the liar's 16:00)."""
    grid, overlay = era_world
    probing = _FrozenSeamProbingSurface(overlay, grid)
    result = _run_era_trial(
        era_world, protocol, tmp_path, tag="r7_seam", surface=probing, scored=_r6_probe_scored(grid)
    )
    assert result.n_folds == 3
    assert probing.probed, "the run must actually reach the overridden seam"
    # every resolved instant is the stamped grid's own close ...
    assert probing.probed == {s: grid.session_close(s) for s, _ in probing.probed.items()}
    # ... including the early-close Friday the underlying answers 16:00
    assert date(2025, 11, 28) in probing.probed
    assert probing.probed[date(2025, 11, 28)] == grid.session_close(date(2025, 11, 28))
    assert probing.probed[date(2025, 11, 28)] != probing._no_early_closes.session_close(
        date(2025, 11, 28)
    )
    # and the underlying's own method really does answer 16:00 by now — the
    # probe is not passing because the liar ran out of lies
    assert probing.decision_close(date(2025, 11, 28)) == probing._no_early_closes.session_close(
        date(2025, 11, 28)
    )


# ---- (R8-P1, Codex round 8) the CALENDAR too: the runtime's own reads ----------


class _FourthReadLiarCalendar:
    """Codex round 8's probe CALENDAR: `session_close` answers the calendar's
    own close on each session's FIRST THREE calls and the no-early-close
    twin's 16:00 on every later call, counting as it goes.

    The boundary reads the stamped calendar exactly twice per decision
    session (the loop's own `declared_close`, then the wired surface's
    `decision_close` behind the very same object) and the runtime's FIRST
    read is the candidate filter's coherence check (`candidates/filters.py`
    re-reads the stamped calendar directly, never the frozen surface) — three
    reads the liar answers honestly, so it passes the ENTIRE preflight and
    the coherence rule alike. Whatever reads it NEXT is fed the wrong
    instant, and which read that is decides the defect: at HEAD the fourth
    is `plan_orders`' entry stamp (`options/strategy.py`), so an early-close
    session's order is stamped 16:00 while its snapshot and the filter
    agreed on 13:00 — the order executes on information unavailable at the
    verified decision instant (INV-02)."""

    def __init__(self, grid) -> None:
        self._grid = grid
        self._no_early_closes = RealSessionCalendar(grid.sessions(), frozenset())
        self.close_calls: dict[date, int] = {}

    def sessions(self):
        return self._grid.sessions()

    def early_close_sessions(self):
        return self._grid.early_close_sessions()

    def is_session(self, d):
        return self._grid.is_session(d)

    def ordinal(self, d):
        return self._grid.ordinal(d)

    def nth_after(self, d, n):
        return self._grid.nth_after(d, n)

    def session_open(self, d):
        return self._grid.session_open(d)

    def session_close(self, d):
        self.close_calls[d] = self.close_calls.get(d, 0) + 1
        if self.close_calls[d] <= 3:
            return self._grid.session_close(d)
        return self._no_early_closes.session_close(d)

    def contains_instant(self, d, ts):
        return self._grid.contains_instant(d, ts)


class _ProbeCalendarSurface(OptionPitSurface):
    """The honest lane-1 surface bound to a PROBE calendar: discloses it
    (the R4-P1 digest binds the probe to the probe) and answers
    `decision_close` from it (the R5-P1 behavioral equality — the accepted
    behaviorally-bound shape). The calendar's statefulness is the probe's,
    never the surface's: this surface is exactly as honest as the calendar
    it answers from, so only the runner's CALENDAR binding can catch the
    liar — never a surface guard (no masking)."""

    def __init__(self, overlay, probe) -> None:
        super().__init__(overlay)
        self._probe_calendar = probe

    @property
    def decision_calendar(self):
        return self._probe_calendar

    def decision_close(self, decision_session: date) -> datetime:
        return self._probe_calendar.session_close(decision_session)


def test_a_fourth_read_calendar_liar_runs_identically_to_the_honest_calendar(
    world, protocol, tmp_path
) -> None:
    """(R8-P1, Codex round 8 — the fourth read) The preflight stores the
    verified `declared_close` and the run consumes the bound SURFACE — but
    `_execute` receives the ORIGINAL mutable `calendar`, and the runtime
    re-reads `session_close` directly at eight functional sites (the filter's
    coherence read, `plan_orders`' entry stamp, `plan_exit_order`, the
    retry/forced/decided sell stamps, the close(t) mark, and the fill doors).
    A calendar answering the fixture close on a session's first THREE reads
    (2 preflight + the coherence read) and 16:00 from the fourth therefore
    passes every boundary guard while `plan_orders` stamps the early-close
    session 2018-07-03 at the twin's 16:00 — the one entry fill decided on
    that session carries the WRONG decision_at under the same declared
    configuration (INV-02 + INV-14 at runtime). The runner now binds the
    calendar with the same-class factory and `_execute` consumes ONLY the
    frozen closes, so the liar cannot express itself: the trial below is
    byte-identical to the honest-calendar run, and NOTHING after the
    preflight reads the liar — every decision session's count is exactly the
    boundary's two (RED before R8-P1: the liar's fills are stamped 20:00Z
    where the honest run stamps the verified 17:00Z, and its decision
    sessions carry 5-24 reads)."""
    import json

    from tree_options.time.calendar import calendar_content_sha256

    overlay, calendar, _snap, _ds = world
    liar = _FourthReadLiarCalendar(calendar)
    surface = _ProbeCalendarSurface(overlay, liar)
    # NO MASKING — the probe passes every guard the boundary owns: it
    # discloses itself through the wired surface (the R4-P1 digest binds the
    # probe to the probe — the trial IS stamped on the probe) and answers
    # the first THREE reads of every session with the calendar's own close —
    # the two preflight reads AND the filter's coherence read. Only a guard
    # on the runtime's LATER calendar reads can catch the fourth.
    probe = _FourthReadLiarCalendar(calendar)
    disclosed = _ProbeCalendarSurface(overlay, probe)
    assert calendar_content_sha256(disclosed.decision_calendar) == calendar_content_sha256(probe)
    assert probe.session_close(date(2018, 7, 3)) == calendar.session_close(date(2018, 7, 3))
    assert probe.session_close(date(2018, 7, 3)) == calendar.session_close(date(2018, 7, 3))
    assert probe.session_close(date(2018, 7, 3)) == calendar.session_close(date(2018, 7, 3))
    assert probe.session_close(date(2018, 7, 3)) != calendar.session_close(date(2018, 7, 3))
    honest = _run_trial(world, protocol, tmp_path, tag="r8_honest_cal")
    lying = _run_trial(
        world, protocol, tmp_path, tag="r8_fourthread", decision_calendar=liar, surface=surface
    )
    assert honest["stamp"]["config_hash"] == lying["stamp"]["config_hash"]
    # compared per-key with a SHORT failure message: a bare equality on the
    # full serialized bodies makes pytest's failure explanation diff two
    # multi-megabyte strings — minutes of rendering that turns a fast kill
    # into a harness timeout
    honest_canon = {k: json.dumps(v, sort_keys=True) for k, v in honest.items()}
    lying_canon = {k: json.dumps(v, sort_keys=True) for k, v in lying.items()}
    diverging = [k for k in honest_canon if honest_canon[k] != lying_canon.get(k)]
    assert not diverging, f"the liar's artifact diverges from the honest run in: {diverging}"
    # the fill decided on the early-close session is stamped the VERIFIED
    # close — 13:00 ET (17:00Z), never the twin's 16:00 ET
    early_fills = [
        f for f in lying["payload"]["fills_log"] if f["decision_session"] == "2018-07-03"
    ]
    assert early_fills, "the run must actually decide an entry on the early-close session"
    verified_close = calendar.session_close(date(2018, 7, 3))
    for fill in early_fills:
        assert fill["decision_at"] == verified_close.isoformat()
    # nothing after the preflight reads the liar: every decision session's
    # count is exactly the boundary's two (declared_close + the wired
    # surface's decision_close), and no session of the calendar exceeds it
    decision_sessions = sorted({row.session for row in _scored(world, surface)})
    assert {liar.close_calls[s] for s in decision_sessions} == {2}
    assert max(liar.close_calls.values()) == 2


def test_the_execution_cancellation_window_consumes_the_verified_instant(
    world, protocol, tmp_path
) -> None:
    """(R8-P1, Codex round 8 — the INV-02 window) The execution-cancellation
    rule (`order.decision_at < action.available_at <= execution_at`) is only
    as honest as the stamp: on the early-close session 2018-07-03 the
    verified close is 13:00 ET (17:00Z) and the no-early-close twin answers
    16:00 ET (20:00Z), so a cash dividend PUBLISHED at 18:30Z — between the
    two — is future information at the verified instant but "already out" at
    the liar's stamp. Under the fourth-read liar the entry order decided
    2018-07-03 is stamped 20:00Z, the window misses the publication, and the
    order EXECUTES on the underlying whose post-close publication it could
    not have seen. With the calendar bound, the stamp IS the verified
    17:00Z, the window catches the publication, and the order is cancelled
    and counted — exactly ONE more cancellation than the same liar run
    without the action, and the fill decided 2018-07-03 vanishes (RED before
    R8-P1: the cancellation count is UNMOVED by the action and the fill
    decided on the future-published underlying exists). The world's own
    seeded actions already cancel one entry, so the count is compared as a
    DELTA between two identical liar runs — never an absolute that the
    baseline could satisfy on its own (no masking)."""
    from tree_options.data.actions import CorporateActionRecord

    overlay, calendar, _snap, snap_dataset = world
    liar = _FourthReadLiarCalendar(calendar)
    surface = _ProbeCalendarSurface(overlay, liar)
    verified_close = calendar.session_close(date(2018, 7, 3))
    published = verified_close + timedelta(minutes=90)
    # strictly inside the (verified close, the twin's 16:00) window
    assert (
        verified_close
        < published
        < _FourthReadLiarCalendar(calendar)._no_early_closes.session_close(date(2018, 7, 3))
    )
    action = CorporateActionRecord(
        security_id="SYN-0009",
        kind="cash_dividend",
        effective_session=date(2018, 9, 3),
        cash_amount=Decimal("0.25"),
        source="synthetic/v1",
        source_record_id="R8-CANCEL-0001",
        source_row_hash="0" * 64,
        snapshot_id=_snap.snapshot_id,
        available_at=published,
    )
    without_action = _run_trial(
        world,
        protocol,
        tmp_path,
        tag="r8_cancel_base",
        decision_calendar=(base := _FourthReadLiarCalendar(calendar)),
        surface=_ProbeCalendarSurface(overlay, base),
    )
    lying = _run_trial(
        world,
        protocol,
        tmp_path,
        tag="r8_cancel",
        decision_calendar=liar,
        surface=surface,
        dataset=_RealLaneDataset(
            snapshot_id=_snap.snapshot_id,
            bars=snap_dataset.bars,
            actions=(*tuple(snap_dataset.actions), action),
        ),
    )
    # the action cancels exactly the one entry its window catches: the
    # verified 17:00Z stamp < the 18:30Z publication <= the 2018-07-05
    # execution. Under the liar's 20:00Z stamp the window misses it and the
    # count is unmoved.
    without_count = without_action["payload"]["counters"]["entries_cancelled"]
    with_count = lying["payload"]["counters"]["entries_cancelled"]
    assert with_count == without_count + 1
    # the entry that would have executed on the future-published underlying
    # never filled: the 2018-07-03 decision produces no fill at all
    decided_early = [
        f for f in lying["payload"]["fills_log"] if f["decision_session"] == "2018-07-03"
    ]
    assert decided_early == []


def test_the_bound_calendar_refuses_unmapped_sessions_fail_closed(world) -> None:
    """(R8-P1, the fail-closed horn) The bound calendar carries the calendar's
    COMPLETE frozen close map — the VERIFIED instants for the trial's
    decision sessions plus one boundary-time read for every other session
    (the runtime's marks, exit decisions and retry/forced stamps read
    sessions the decision loop never visited) — so a session outside the
    calendar's own session set can never silently fall back to the
    overridable method: the bound instance refuses, naming the session. And
    the map's decision-session values are the VERIFIED ones even when the
    underlying calendar would now answer differently — the whole point of
    the freeze (RED before R8-P1: no `_bind_decision_calendar` exists)."""
    from tree_options.trials.options_run import _bind_decision_calendar

    _overlay, calendar, _snap, _ds = world
    sessions = calendar.sessions()
    verified = {s: calendar.session_close(s) for s in sessions[:3]}
    bound = _bind_decision_calendar(calendar, verified)
    # the bind is the same concrete class; geometry stays the class's own
    assert type(bound) is type(calendar)
    assert bound.sessions() == calendar.sessions()
    assert bound.ordinal(sessions[5]) == calendar.ordinal(sessions[5])
    assert bound.nth_after(sessions[0], 2) == sessions[2]
    # the VERIFIED instants answer; so does every other calendar session
    # (one boundary-time read, frozen) — never the method again
    assert bound.session_close(sessions[0]) == verified[sessions[0]]
    late = calendar.session_close(sessions[100])
    assert bound.session_close(sessions[100]) == late
    # a session outside the calendar refuses fail-closed, by name
    non_session = date(2018, 1, 1)
    assert not calendar.is_session(non_session)
    with pytest.raises(ValueError, match="no frozen close for session"):
        bound.session_close(non_session)
    # and the verified instant survives the underlying calendar turning:
    # consume the liar's three honest reads, then bind the VERIFIED value
    liar = _FourthReadLiarCalendar(calendar)
    early = date(2018, 7, 3)
    for _ in range(3):
        assert liar.session_close(early) == calendar.session_close(early)
    bound_liar = _bind_decision_calendar(liar, {early: calendar.session_close(early)})
    assert bound_liar.session_close(early) == calendar.session_close(early)
    assert liar.session_close(early) != calendar.session_close(early)


# ---- (R8-P2, Codex round 8) the bind REFUSES when it cannot install --------


class _DescriptorLyingSurface(VwapPitSurface):
    """Codex round 8's P2 probe class: exposes `decision_close` as a
    callable-returning PROPERTY with a NO-OP setter. The property's stored
    callable answers each session's FIRST call with the stamped grid's close
    (so the R4-P1 digest and the R5-P1 behavioral equality both pass) and
    the no-early-close twin's 16:00 on every later call — the stateful shape
    R6 froze. But a class-level DATA DESCRIPTOR owns the attribute: the
    bind's `bound.decision_close = <closure>` writes through the no-op
    setter and installs NOTHING, so the run proceeds on the unfrozen
    property and the runtime's second call returns 16:00 (a post-close
    publication becomes decision-visible, excluding the candidate) —
    silently, with no refusal anywhere."""

    def __init__(self, overlay, grid) -> None:
        super().__init__(overlay)
        self._lying_grid = grid
        self._no_early_closes = RealSessionCalendar(grid.sessions(), frozenset())
        self._answered: set[date] = set()

    @property
    def decision_calendar(self):
        return self._lying_grid

    @property
    def decision_close(self):
        def stored(decision_session: date) -> datetime:
            if decision_session not in self._answered:
                self._answered.add(decision_session)
                return self._lying_grid.session_close(decision_session)
            return self._no_early_closes.session_close(decision_session)

        return stored

    @decision_close.setter
    def decision_close(self, value) -> None:
        pass  # the no-op: the freeze vanishes


def test_a_descriptor_surface_is_refused_before_registration(era_world, protocol, tmp_path) -> None:
    """(R8-P2, Codex round 8 — the reproduced probe; R9-P2 re-pin: the MRO
    pre-scan now refuses the descriptor CLASS SHAPE before the install is
    ever attempted, so the named error is the scan's — the read-back stays
    as belt-and-suspenders) `bound.decision_close = decision_close` is a
    plain instance-attribute write, and Python does NOT let an instance
    attribute shadow a class-level DATA DESCRIPTOR: a subclass exposing
    decision_close as a callable-returning property with a no-op setter
    passes the ENTIRE preflight (the property's first call per session
    answers the stamped close) while the write silently installs NOTHING —
    the freeze never lands, the run proceeds on the unfrozen property, and
    the runtime's later calls answer 16:00 (RED before R8-P2: this trial
    registered, ran, and completed — no refusal existed)."""
    from tree_options.time.calendar import calendar_content_sha256

    grid, overlay = era_world
    liar = _DescriptorLyingSurface(overlay, grid)
    # NO MASKING — the probe passes every guard the boundary owns: the
    # digest binds (the disclosure is the stamped grid) and the FIRST call
    # per session answers the stamped close; only the class-shape refusal
    # can catch the descriptor
    probe = _DescriptorLyingSurface(overlay, grid)
    assert calendar_content_sha256(probe.decision_calendar) == calendar_content_sha256(grid)
    assert probe.decision_close(date(2025, 11, 28)) == grid.session_close(date(2025, 11, 28))
    assert probe.decision_close(date(2025, 11, 28)) != grid.session_close(date(2025, 11, 28))
    with pytest.raises(ValueError, match="defines decision_close as a class-level data descriptor"):
        _run_era_trial(era_world, protocol, tmp_path, tag="r8_descriptor", surface=liar)
    # refused BEFORE registration: no record, no artifact
    assert trial_count(tmp_path / "r8_descriptor.db") == 0
    assert not (tmp_path / "r8_descriptor").exists()


class _SetterlessPropertySurface(VwapPitSurface):
    """A class-level data descriptor with NO setter: the install assignment
    itself raises AttributeError — the refusal must be the bind's NAMED
    ValueError, never a raw AttributeError escaping the boundary."""

    def __init__(self, overlay, grid) -> None:
        super().__init__(overlay)
        self._lying_grid = grid

    @property
    def decision_calendar(self):
        return self._lying_grid

    @property
    def decision_close(self):
        return self._lying_grid.session_close


def test_a_setterless_descriptor_refuses_with_the_named_error(era_world) -> None:
    """(R8-P2, the assignment-raising horn; R9-P2 re-pin: the MRO pre-scan
    now refuses the property shape up front, so the named error is the
    scan's — the assignment-raising wrap stays as belt-and-suspenders) A
    setter-less property is still a class-level data descriptor, and the
    install assignment against it raises AttributeError. The refusal is a
    boundary failure by name, never a raw crash (RED before R8-P2: the raw
    AttributeError from inside `_bind_decision_surface` escaped the
    boundary)."""
    from tree_options.trials.options_run import _bind_decision_surface

    grid, overlay = era_world
    surface = _SetterlessPropertySurface(overlay, grid)
    first = grid.sessions()[0]
    with pytest.raises(ValueError, match="defines decision_close as a class-level data descriptor"):
        _bind_decision_surface(surface, {first: grid.session_close(first)})


class _SlottedSurface(VwapPitSurface):
    """A subclass declaring nonempty `__slots__`: the bind's state copy is
    `__dict__`-only, so slot state would be silently MISSING on the bound
    instance — an incomplete bind. No repo surface uses slots; the factory
    must refuse by name rather than run half-bound (crash-hardening into a
    named refusal, per the round-8 finding)."""

    __slots__ = ("_slot_state",)

    def __init__(self, overlay, grid, liquidity_term) -> None:
        super().__init__(overlay, decision_calendar=grid, underlying_liquidity_term=liquidity_term)
        self._slot_state = "carried by the slot, invisible to __dict__"


class _SlottedCalendar(RealSessionCalendar):
    """The calendar-side twin of `_SlottedSurface`: same named refusal, same
    reason — the `__dict__` copy cannot carry slot state."""

    __slots__ = ("_extra_state",)

    def __init__(self, grid) -> None:
        super().__init__(grid.sessions(), frozenset(grid.early_close_sessions()))
        self._extra_state = 1


def test_a_slotted_surface_class_is_refused_by_name(era_world) -> None:
    """(R8-P2, the slots horn — surface side) RED before R8-P2: the bind
    constructs and returns a half-copied instance with no refusal."""
    from tree_options.trials.options_run import _bind_decision_surface

    grid, overlay = era_world
    surface = _SlottedSurface(overlay, grid, "evaluated")
    first = grid.sessions()[0]
    with pytest.raises(ValueError, match="__slots__"):
        _bind_decision_surface(surface, {first: grid.session_close(first)})


def test_a_slotted_calendar_class_is_refused_by_name(era_world) -> None:
    """(R8-P2, the slots horn — calendar side) The R8-P1 calendar factory
    shares the `__dict__`-only copy, so it shares the named refusal too."""
    from tree_options.trials.options_run import _bind_decision_calendar

    grid, _overlay = era_world
    slotted = _SlottedCalendar(grid)
    first = grid.sessions()[0]
    with pytest.raises(ValueError, match="__slots__"):
        _bind_decision_calendar(slotted, {first: grid.session_close(first)})


# ---- (post-022-C, M323's re-seam) the ERA-world same-object twin ---------------
#
# 022-C moved the fill door's DECISION-side comparison onto the threaded
# frozen `decision_closes` whenever an execution calendar is supplied, so
# the SYNTHETIC world's same-object kill (the door re-reading the mutable
# original) no longer moves: this file's fixture world quotes TWO-SIDED
# events, and the engine's only remaining execution-calendar `session_close`
# read is the vwap BAR_SESSION_STAMP_MISMATCH line, which never executes
# without vwap quotes. The era world supplies them — but its stock capture
# is DAILY, and a grid-calendar fill engine refuses every Thursday bar
# (BAR_SESSION_NOT_IN_CALENDAR) before any stamp read. The fixture below
# is the era capture rebuilt with FRIDAY-ONLY bars (and volume big enough
# that the volume-flow and participation rules pass), so a fill executing
# at grid Friday D consumes the PREVIOUS GRID FRIDAY's vwap bar and the
# stamp line reads the engine calendar's close of that bar's session.


@pytest.fixture(scope="module")
def era_friday_world(tmp_path_factory: pytest.TempPathFactory):
    """`era_world`'s vendor shape with FRIDAY-ONLY bars and 1e6 volume: the
    overlay calendar aligns with the Friday decision grid, so a fill engine
    running on the grid consumes grid-Friday vwap bars (the stock daily
    capture would refuse every intervening Thursday bar before the stamp
    seam). Each contract's rows run through its expiry so late exits stay
    inside the listing window."""
    from decimal import Decimal as _Decimal

    from tests.fixtures.massive_structural_sample import (
        bar,
        bars_payload,
        contract_result,
        contracts_payload,
    )
    from tests.unit.test_vwap_pit_surface import _exchange_calendar, _t
    from tree_options.data.massive_overlay import load_derived_surface
    from tree_options.synth_options.greeks import bs_price

    grid = _friday_grid()
    exchange = _exchange_calendar()
    capture = tmp_path_factory.mktemp("erafri") / "capture"
    masters = capture / "masters"
    masters.mkdir(parents=True)
    first_bar = min(span[0] for _e, span in _ERA_CONTRACTS)
    (masters / f"spy_{first_bar:%Y-%m-%d}.json").write_text(
        contracts_payload(
            results=tuple(
                contract_result(
                    ticker=f"O:SPY{expiry:%y%m%d}C00600000",
                    underlying="SPY",
                    expiration=f"{expiry:%Y-%m-%d}",
                    strike="600",
                    contract_type="call",
                )
                for expiry, _span in _ERA_CONTRACTS
            ),
            as_of=f"{first_bar:%Y-%m-%d}",
        ),
        encoding="utf-8",
    )
    bars = capture / "bars"
    bars.mkdir()
    friday_sessions: set[date] = set()
    for expiry, (lo, _hi) in _ERA_CONTRACTS:
        last = min(expiry, date(2026, 5, 1))
        span = [s for s in exchange.sessions() if lo <= s <= last and s.weekday() == 4]

        def premium(session: date, _expiry: date = expiry) -> str:
            price = bs_price(
                spot=600.0,
                strike=600.0,
                dte_calendar_days=(_expiry - session).days,
                iv=0.18,
                risk_free=0.03,
                dividend_yield=0.0,
                call_put="C",
            )
            return f"{price:.4f}"

        rows = tuple(
            bar(
                v="1000000",
                t=_t(session),
                vw=premium(session),
                o=premium(session),
                c=premium(session),
                h=f"{_Decimal(premium(session)) + _Decimal('0.10')}",
                low=f"{_Decimal(premium(session)) - _Decimal('0.10')}",
                n="24",
            )
            for session in span
        )
        (bars / f"bars_{expiry:%Y%m%d}.json").write_text(
            bars_payload(
                ticker=f"O:SPY{expiry:%y%m%d}C00600000",
                results_count=str(len(rows)),
                results=rows,
            ),
            encoding="utf-8",
        )
        friday_sessions.update(span)
    (capture / "spot_proxy.json").write_text(
        '{"SPY": {'
        + ", ".join(f'"{s:%Y-%m-%d}": "600.00"' for s in sorted(friday_sessions))
        + "}}",
        encoding="utf-8",
    )
    overlay = load_derived_surface(capture, staleness_sessions=400)
    return grid, overlay


def _protocol_022_lane_on(protocol):
    """The 0.2.2-declared protocol IN MEMORY (never a yaml edit): the
    version bump carries its own amendment record and the earnings
    disclosed-absence declaration — the exact shape the 0.2.2 amendment
    packet proposes, under which the era lane actually trades (the 0.2.1
    protocol refuses every candidate NOT_EVALUABLE and no fill ever
    reaches the engine)."""
    from tree_options.protocol.schema import ResearchProtocol

    data = protocol.model_dump(mode="json")
    data["meta"]["protocol_version"] = "0.2.2"
    data["meta"]["amendments"].append(
        {
            "version": "0.2.2",
            "date": "PENDING-OWNER-RATIFICATION",
            "decision": "unit fixture: the 0.2.2 lane-on declarations",
            "changes": "unit fixture only; the real record rides the amendment packet",
        }
    )
    data["option_candidate_defaults"]["earnings_evaluation"] = "disclosed_absence"
    return ResearchProtocol.model_validate(data)


def _run_m323_era_pair(era_friday_world, protocol, tmp_path, *, tag: str, liar, execution_calendar):
    """One same-object-horn run over the Friday-only era world under the
    0.2.2-declared protocol: the stamped decision grid is the LIAR (the
    surface discloses the same object), and `execution_calendar` is the
    caller's choice — None for the none form, the liar itself for the
    same-object form. The dataset keeps the underlying alive on every grid
    session (the runner's silent-death scan) and the surface carries the
    spot_v2 dollar-volume source over the daily exchange calendar so the
    underlying-liquidity rule evaluates for real."""
    import json

    from tests.unit.test_vwap_pit_surface import _exchange_calendar
    from tree_options.protocol.era_profile import real_lane_split_override

    grid, overlay = era_friday_world
    world_id = overlay.spec.world_id
    exchange = _exchange_calendar()
    surface = VwapPitSurface(
        overlay,
        decision_calendar=liar,
        underlying_liquidity_term="evaluated",
        spot_v2={
            "SPY": {
                s: (Decimal("600.00"), 100_000)
                for s in exchange.sessions()
                if date(2025, 6, 1) <= s <= date(2026, 6, 26)
            }
        },
        exchange_calendar=exchange,
    )
    decision_sessions = grid.sessions()[: grid.sessions().index(date(2026, 5, 1)) + 1]
    registry = TrialRegistry(tmp_path / f"{tag}.db")
    try:
        result = run_options_trial(
            dataset=_RealLaneDataset(
                snapshot_id=world_id,
                bars=tuple(
                    BarRecord(
                        security_id="SPY",
                        session=session,
                        open=Decimal("600.00"),
                        high=Decimal("600.00"),
                        low=Decimal("600.00"),
                        close=Decimal("600.00"),
                        volume=1_000_000,
                        source="spot-proxy/declared",
                        source_record_id=f"SPY-{session:%Y%m%d}",
                        source_row_hash="0" * 64,
                        snapshot_id=world_id,
                        available_at=grid.session_close(session),
                    )
                    for session in decision_sessions
                ),
            ),
            surface=surface,  # type: ignore[arg-type]
            calendar=liar,  # type: ignore[arg-type]
            execution_calendar=execution_calendar,
            protocol=_protocol_022_lane_on(protocol),
            world_id=world_id,
            arm="A",
            strategy_config=OptionsStrategyConfig(),
            scored=_era_scored_rows(grid),
            model_family="null-sha256/1",
            model_sha256=None,
            hypothesis=f"unit/{tag}",
            decision_sessions=decision_sessions,
            options_manifest_hash="0" * 64,
            registry=registry,
            artifacts_dir=tmp_path / tag,
            repo=REPO_ROOT,
            clock=FIXED_CLOCK,
            split_override=real_lane_split_override(),
            liquidity_lane=2,
            score_seed="t-null/era",
            allow_dirty=True,
        )
    finally:
        registry.close()
    return json.loads(result.artifact_path.read_text(encoding="utf-8"))


# ---- (R9-P1, Codex round 9) the SAME-OBJECT execution calendar ---------------


def test_a_same_object_execution_calendar_is_the_none_form_at_runtime(
    world, era_friday_world, protocol, tmp_path
) -> None:
    """(R9-P1, Codex round 9 — the same-object horn) The disclosure treats
    `execution_calendar is calendar` as NO execution calendar (same object
    keeps the historical single-calendar stamp and hash), but `_execute`
    received the ORIGINAL object unchanged, and the backtest routes any
    non-None execution calendar to the fill engine — whose
    `DECISION_INSTANT_NOT_CLOSE` door re-reads it. `execution_calendar=None`
    and `execution_calendar=<the same object>` were therefore stamped and
    hashed IDENTICALLY yet differed at runtime: the None form's fills read
    the BOUND calendar's frozen instants, the same-object form's read the
    MUTABLE original. A calendar honest through the boundary's two reads
    (`declared_close`, then the wired surface's `decision_close`) and 16:00
    thereafter makes the correctly-stamped 13:00 order die in the same-object
    horn only (INV-02/INV-14: different counters and fills under one declared
    configuration). The boundary now normalizes at the `_execute` call, so
    the same-object form IS the None form at runtime — byte-identical
    artifacts, both consuming the frozen instants (RED before R9-P1: the
    same-object run's fill door consumed the liar's third answer and its
    artifact diverged from the None run's).

    (post-022-C re-seam, M323's kill restored) 022-C moved the door's
    DECISION-side comparison onto the threaded frozen closes, so this
    synthetic pair alone no longer detects a dropped normalization — its
    quotes are two-sided and the engine's remaining execution-calendar
    `session_close` read (the vwap bar-stamp line) never executes. The ERA
    twin below runs the same pair over the Friday-only era world under the
    0.2.2-declared protocol, where vwap fills DO flow: with the
    normalization dropped, the same-object run's engine holds the MUTABLE
    liar, whose third-and-later answers (the no-early-close 16:00) MATCH
    the vendor bar's nominal-16:00 stamp on the early-close Friday
    2025-11-28 — the exit decided there FILLS under the mutant while the
    None form's bound calendar (frozen 13:00) refuses it
    BAR_SESSION_STAMP_MISMATCH, and the payloads diverge."""
    import json

    overlay, calendar, _snap, _ds = world
    liar_none = _ThirdReadLiarCalendar(calendar)
    liar_same = _ThirdReadLiarCalendar(calendar)
    surface_none = _ProbeCalendarSurface(overlay, liar_none)
    surface_same = _ProbeCalendarSurface(overlay, liar_same)
    # NO MASKING — the probe passes every guard the boundary owns: it
    # discloses itself through the wired surface (the digest binds the liar
    # to the liar), answers both boundary reads with the fixture close, and
    # never raises. Only the fill engine's read of the UNBOUND original can
    # catch the third answer.
    none_body = _run_trial(
        world,
        protocol,
        tmp_path,
        tag="r9_exec_none",
        decision_calendar=liar_none,
        surface=surface_none,
    )
    same_body = _run_trial(
        world,
        protocol,
        tmp_path,
        tag="r9_exec_same",
        decision_calendar=liar_same,
        surface=surface_same,
        execution_calendar=liar_same,
    )
    # the defect's premise, pinned: the two forms stamp and hash IDENTICALLY
    assert none_body["stamp"]["config_hash"] == same_body["stamp"]["config_hash"]
    # compared per-key with a SHORT failure message (the R8 idiom: a bare
    # equality on the serialized bodies renders a multi-megabyte diff)
    none_canon = {k: json.dumps(v, sort_keys=True) for k, v in none_body.items()}
    same_canon = {k: json.dumps(v, sort_keys=True) for k, v in same_body.items()}
    diverging = [k for k in none_canon if none_canon[k] != same_canon.get(k)]
    assert not diverging, f"the same-object run diverges from the None run in: {diverging}"
    # both runs consume the FROZEN instants: the fill decided on the
    # early-close session is stamped the verified 13:00 ET close, never the
    # no-early-close twin's 16:00
    early = [f for f in same_body["payload"]["fills_log"] if f["decision_session"] == "2018-07-03"]
    assert early, "the run must actually decide an entry on the early-close session"
    verified_close = calendar.session_close(date(2018, 7, 3))
    for fill in early:
        assert fill["decision_at"] == verified_close.isoformat()
    # nothing after the boundary consulted either liar: every read count is
    # EXACTLY the boundary's entitlement — 2 per decision session (the
    # declared close + the wired surface's answer) and 1 per every other
    # calendar session (the freeze's boundary-time population read). The
    # per-session DICT equality against the None run is the load-bearing
    # form (re-seamed post-022-C, M323's kill re-proven): the fill door's
    # decision-side now reads the threaded frozen closes even on the dual
    # path, so a runtime read of the UNBOUND original only inflates a
    # NON-decision session (2 where 1 is entitled) — a bare max()==2
    # cannot see that, because decision sessions are entitled to 2 as
    # well. The same-object run must consult its mutable calendar
    # identically to the None run: same sessions, same counts, or the
    # normalization is dropped and the fill engine holds the original.
    assert max(liar_none.close_calls.values()) == 2
    assert max(liar_same.close_calls.values()) == 2
    assert liar_same.close_calls == liar_none.close_calls, (
        "the same-object run consulted the mutable calendar differently"
        f" than the None run: same={liar_same.close_calls}"
        f" none={liar_none.close_calls} — a runtime read reached the"
        " UNBOUND original (an execution-side read the None form's bound"
        " calendar serves)"
    )
    # the liar really would have answered differently on its third read —
    # proven on a SPENT twin so the assertion consumes no read of the runs'
    # probes
    spent = _ThirdReadLiarCalendar(calendar)
    assert spent.session_close(date(2018, 7, 3)) == calendar.session_close(date(2018, 7, 3))
    assert spent.session_close(date(2018, 7, 3)) == calendar.session_close(date(2018, 7, 3))
    assert spent.session_close(date(2018, 7, 3)) != calendar.session_close(date(2018, 7, 3))

    # ---- the ERA twin (post-022-C re-seam; M323's kill surface) ----------
    #
    # The same pair over the Friday-only era world under the 0.2.2-declared
    # protocol, where VWAP quotes flow through the engine. The early-close
    # grid Friday 2025-11-28 is a DECISION session: the boundary reads its
    # close exactly twice, so the engine's bar-stamp read of the 11-28 bar
    # (the exit decided there executes 2025-12-05 against that bar) is the
    # liar's THIRD — under a dropped normalization the same-object engine
    # consumes the 16:00 lie, which MATCHES the vendor bar's nominal-16:00
    # stamp, and the exit fills; the None form's engine holds the BOUND
    # calendar whose frozen 13:00 refuses the fill
    # (BAR_SESSION_STAMP_MISMATCH) and the exit retries a session later.
    # At HEAD both forms are the None form and the artifacts are identical.
    era_liar_none = _ThirdReadLiarCalendar(era_friday_world[0])
    era_liar_same = _ThirdReadLiarCalendar(era_friday_world[0])
    era_none_body = _run_m323_era_pair(
        era_friday_world,
        protocol,
        tmp_path,
        tag="r9_era_exec_none",
        liar=era_liar_none,
        execution_calendar=None,
    )
    era_same_body = _run_m323_era_pair(
        era_friday_world,
        protocol,
        tmp_path,
        tag="r9_era_exec_same",
        liar=era_liar_same,
        execution_calendar=era_liar_same,
    )
    assert era_none_body["stamp"]["config_hash"] == era_same_body["stamp"]["config_hash"]
    era_none_canon = {k: json.dumps(v, sort_keys=True) for k, v in era_none_body.items()}
    era_same_canon = {k: json.dumps(v, sort_keys=True) for k, v in era_same_body.items()}
    era_diverging = [k for k in era_none_canon if era_none_canon[k] != era_same_canon.get(k)]
    assert not era_diverging, (
        f"the era same-object run diverges from the None run in: {era_diverging}"
    )
    # the seam is EXERCISED, not vacuous: the runs trade, and the exit
    # decided on the early-close Friday is refused by the honest frozen
    # 13:00 against the bar's nominal 16:00 stamp in BOTH forms — no fill
    # is decided on 2025-11-28 and the refusal is counted
    era_rejections = era_none_body["payload"]["counters"]["rejections"]
    era_fills = era_none_body["payload"]["fills_log"]
    assert era_fills, "the era twin must actually trade"
    assert (
        era_rejections.get("exit_fill_rejections", {}).get("BAR_SESSION_STAMP_MISMATCH", 0) > 0
    ), "the early-close Friday's vwap bar must exercise the stamp door"
    assert all(f["decision_session"] != "2025-11-28" for f in era_fills), (
        "the exit decided on the early-close Friday dies at the stamp door and"
        " retries a session later — a 2025-11-28-decided fill means the engine"
        " consumed an unfrozen close"
    )
    # and the same dict-equality discipline as the synthetic pair: the
    # same-object run must consult its mutable calendar IDENTICALLY to the
    # None run — under a dropped normalization the engine's stamp reads
    # (one per vwap fill) inflate the same-object liar's counts
    assert era_liar_same.close_calls == era_liar_none.close_calls, (
        "the era same-object run consulted the mutable calendar differently"
        f" than the None run: same={era_liar_same.close_calls}"
        f" none={era_liar_none.close_calls} — the fill engine's bar-stamp"
        " reads reached the UNBOUND original"
    )
    # the era liar's third answer really is the stamp-matching 16:00 (the
    # no-early-close twin's answer) — proven on a SPENT twin
    era_spent = _ThirdReadLiarCalendar(era_friday_world[0])
    early_friday = date(2025, 11, 28)
    assert era_spent.session_close(early_friday) == era_friday_world[0].session_close(early_friday)
    assert era_spent.session_close(early_friday) == era_friday_world[0].session_close(early_friday)
    assert era_spent.session_close(early_friday) != era_friday_world[0].session_close(early_friday)


# ---- (R9-P2, Codex round 9) the install refusal is DURABLE --------------------


class _TemporalEvasionDescriptor:
    """Codex round 9's probe descriptor: the STATEFUL data descriptor the
    R8-P2 read-back cannot catch. `__set__` ACCEPTS the frozen closure and
    `__get__` returns it for exactly the FIRST post-install read — the
    verification — then answers the instance's own lying callable
    (`_descriptor_lie`, the no-early-close twin's 16:00) on every later
    read. Before any install it answers `_descriptor_honest` (the stamped
    grid's close), so the preflight's per-session reads pass. The one read
    the R8 verification ever made holds identity; every read after it lies."""

    def __init__(self) -> None:
        self._installed: dict[int, object] = {}
        self._reads: dict[int, int] = {}

    def __set__(self, obj, value) -> None:
        self._installed[id(obj)] = value
        self._reads[id(obj)] = 0

    def __get__(self, obj, objtype=None):
        if obj is None:
            return self
        reads = self._reads.get(id(obj), 0) + 1
        self._reads[id(obj)] = reads
        installed = self._installed.get(id(obj))
        if installed is None:
            return obj._descriptor_honest  # pre-install: the preflight reads
        if reads == 1:
            return installed  # the verification: identity holds exactly once
        return obj._descriptor_lie  # every later read lies


class _TemporalEvasionSurface(VwapPitSurface):
    """The surface-side probe: `decision_close` IS the temporal-evasion
    descriptor. It passes every guard the boundary owns — the digest (the
    disclosure is the stamped grid), the behavioral equality (pre-install
    reads answer the stamped closes) — AND the R8-P2 install verification
    (`__set__` accepts the closure; the first post-install read returns it).
    Only a refusal that names the CLASS SHAPE can catch it."""

    def __init__(self, overlay, grid) -> None:
        super().__init__(overlay)
        self._lying_grid = grid
        self._descriptor_honest = grid.session_close
        self._descriptor_lie = RealSessionCalendar(grid.sessions(), frozenset()).session_close

    @property
    def decision_calendar(self):
        return self._lying_grid

    decision_close = _TemporalEvasionDescriptor()


class _TemporalEvasionCalendar(RealSessionCalendar):
    """The calendar-side twin: the same STATEFUL data descriptor on the
    `session_close` seam. The R8-P1 calendar factory shares the one-shot
    read-back, so it shares the same temporal hole; the MRO pre-scan refuses
    the shape on both paths."""

    session_close = _TemporalEvasionDescriptor()

    def __init__(self, grid) -> None:
        super().__init__(grid.sessions(), frozenset(grid.early_close_sessions()))
        self._descriptor_honest = RealSessionCalendar(
            grid.sessions(), frozenset(grid.early_close_sessions())
        ).session_close
        self._descriptor_lie = RealSessionCalendar(grid.sessions(), frozenset()).session_close


def test_a_stateful_descriptor_surface_is_refused_before_registration(
    era_world, protocol, tmp_path
) -> None:
    """(R9-P2, Codex round 9 — the temporal-evasion horn) The R8-P2
    read-back verified exactly ONCE: a STATEFUL data descriptor could accept
    the closure in `__set__`, return it for the verification `__get__`, and
    answer the unfrozen 16:00-lying callable on every later read — the
    trial registers and the runtime consumes the lying callable, the
    post-close order-stamping defect reborn (RED before R9-P2: this trial
    registered, ran, and completed on the lying descriptor — no refusal
    existed). The MRO pre-scan now refuses any class in the MRO defining
    the seam as a data descriptor, BEFORE the install is attempted, by
    name, before registration."""
    from tree_options.time.calendar import calendar_content_sha256

    grid, overlay = era_world
    liar = _TemporalEvasionSurface(overlay, grid)
    # NO MASKING — the probe passes every guard the boundary owns AND the
    # R8 install verification: the digest binds, the pre-install reads
    # answer the stamped closes, __set__ accepts the closure, and the FIRST
    # post-install read returns it (identity holds once — proven on the
    # calendar-side twin below)
    probe = _TemporalEvasionSurface(overlay, grid)
    assert calendar_content_sha256(probe.decision_calendar) == calendar_content_sha256(grid)
    assert probe.decision_close(date(2025, 11, 28)) == grid.session_close(date(2025, 11, 28))
    with pytest.raises(ValueError, match="defines decision_close as a class-level data descriptor"):
        _run_era_trial(era_world, protocol, tmp_path, tag="r9_temporal", surface=liar)
    # refused BEFORE registration: no record, no artifact
    assert trial_count(tmp_path / "r9_temporal.db") == 0
    assert not (tmp_path / "r9_temporal").exists()


def test_a_stateful_descriptor_calendar_seam_is_refused_by_name(era_world) -> None:
    """(R9-P2, the temporal-evasion horn — calendar side) The calendar
    factory shares the surface factory's one-shot read-back, so the same
    stateful descriptor evades it there too (RED before R9-P2: the bind
    returned a bound calendar whose every post-verification read lied).
    The sentinel sequence below IS the evasion, proven: `__set__` accepts
    the install, the first read returns it (the R8 verification passes),
    and the second read already answers the liar."""
    from tree_options.trials.options_run import _bind_decision_calendar

    grid, _overlay = era_world
    probe = _TemporalEvasionCalendar(grid)
    sentinel = object()
    probe.session_close = sentinel  # __set__ ACCEPTS the install
    assert probe.session_close is sentinel  # read 1: the verification passes
    assert probe.session_close is not sentinel  # read 2+: the lying callable
    liar = _TemporalEvasionCalendar(grid)
    first = grid.sessions()[0]
    with pytest.raises(ValueError, match="defines session_close as a class-level data descriptor"):
        _bind_decision_calendar(liar, {first: grid.session_close(first)})


class _GetattributeOverrideSurface(VwapPitSurface):
    """A semantically NEUTRAL `__getattribute__` override — pure delegation,
    it changes no behavior today. The SHAPE is what the boundary refuses:
    an override can rewrite ANY later attribute read, so no installed
    closure is durably authoritative on such a class."""

    def __init__(self, overlay, grid, liquidity_term) -> None:
        super().__init__(overlay, decision_calendar=grid, underlying_liquidity_term=liquidity_term)

    def __getattribute__(self, name):
        return super().__getattribute__(name)


class _GetattributeOverrideCalendar(RealSessionCalendar):
    """The calendar-side twin: the same neutral override, the same named
    refusal — the freeze cannot be durable behind a rewritten lookup."""

    def __init__(self, grid) -> None:
        super().__init__(grid.sessions(), frozenset(grid.early_close_sessions()))

    def __getattribute__(self, name):
        return super().__getattribute__(name)


def test_a_getattribute_overriding_surface_is_refused_by_name(era_world) -> None:
    """(R9-P2, the rewritten-lookup horn) An instance `__getattribute__`
    override performs the temporal interception on ANY attribute read: it
    can return the installed closure for the verification and something
    else for every runtime read, so the freeze cannot be durably installed
    on such a class. The refusal is BY NAME, on the neutral shape itself
    (RED before R9-P2: this bind succeeded — no refusal existed)."""
    from tree_options.trials.options_run import _bind_decision_surface

    grid, overlay = era_world
    surface = _GetattributeOverrideSurface(overlay, grid, "evaluated")
    first = grid.sessions()[0]
    with pytest.raises(ValueError, match="overrides __getattribute__"):
        _bind_decision_surface(surface, {first: grid.session_close(first)})


def test_a_getattribute_overriding_calendar_is_refused_by_name(era_world) -> None:
    """(R9-P2, the rewritten-lookup horn — calendar side) Both factories
    carry the scan; the calendar side refuses the same shape."""
    from tree_options.trials.options_run import _bind_decision_calendar

    grid, _overlay = era_world
    calendar = _GetattributeOverrideCalendar(grid)
    first = grid.sessions()[0]
    with pytest.raises(ValueError, match="overrides __getattribute__"):
        _bind_decision_calendar(calendar, {first: grid.session_close(first)})


class _SetattrSwallowingSurface(VwapPitSurface):
    """The belt-and-suspenders shape: a `__setattr__` override that swallows
    ONLY the seam write. The MRO pre-scan cannot name it (an overriding
    `__setattr__` is neither a seam descriptor nor `__getattribute__`), the
    assignment lands in the void, and the class method answers the
    read-back — so the ONE-TIME READ-BACK is the guard that catches it (the
    R9-P2 scan's disclosed residue, pinned here)."""

    def __init__(self, overlay, grid, liquidity_term) -> None:
        super().__init__(overlay, decision_calendar=grid, underlying_liquidity_term=liquidity_term)

    def __setattr__(self, name, value) -> None:
        if name == "decision_close":
            return  # the swallow: the freeze never lands
        super().__setattr__(name, value)


def test_a_setattr_swallowing_class_is_still_refused_by_the_read_back(era_world) -> None:
    """(R9-P2, the belt-and-suspenders horn) The scan names the class shapes
    it can; this one it cannot — and the read-back still catches it: the
    swallowed install leaves the class method answering the read-back,
    identity fails, and the bind refuses by name (owner of M320 under the
    R9-P2 scan: the descriptor class the mutant's previous owner used is
    now refused BEFORE the install, which would mask the dropped
    read-back)."""
    from tree_options.trials.options_run import _bind_decision_surface

    grid, overlay = era_world
    surface = _SetattrSwallowingSurface(overlay, grid, "evaluated")
    first = grid.sessions()[0]
    with pytest.raises(ValueError, match="the freeze cannot be installed"):
        _bind_decision_surface(surface, {first: grid.session_close(first)})


class _SetattrRaisingSurface(VwapPitSurface):
    """The wrap's residue shape: a `__setattr__` override that RAISES on the
    seam. The scan cannot name it; the assignment's AttributeError becomes
    the bind's named refusal instead of escaping the boundary."""

    def __init__(self, overlay, grid, liquidity_term) -> None:
        super().__init__(overlay, decision_calendar=grid, underlying_liquidity_term=liquidity_term)

    def __setattr__(self, name, value) -> None:
        if name == "decision_close":
            raise AttributeError("the seam refuses assignment")
        super().__setattr__(name, value)


def test_a_setattr_raising_class_is_refused_with_the_named_error(era_world) -> None:
    """(R9-P2, the belt-and-suspenders horn — the AttributeError wrap) The
    `except AttributeError` wrap around the assignment stays for anything
    the scan cannot name: a refusing `__setattr__` raises on the install
    and the bind refuses by name, never a raw AttributeError out of the
    boundary."""
    from tree_options.trials.options_run import _bind_decision_surface

    grid, overlay = era_world
    surface = _SetattrRaisingSurface(overlay, grid, "evaluated")
    first = grid.sessions()[0]
    with pytest.raises(ValueError, match="cannot accept the freeze"):
        _bind_decision_surface(surface, {first: grid.session_close(first)})


# ---- (R9-P3, Codex round 9) the empty-slots no-dict class refuses ------------


class _StatelessStructuralCalendar:
    """(R9-P3, Codex round 9) A LEGAL `__slots__ = ()` class over no
    `__dict__`-bearing base: stateless by construction — the whole geometry
    is structural (class-level constants and date arithmetic), so there is
    no per-instance state to lose — and yet BOTH halves of the bind (the
    `__dict__`-copy state and the frozen override) are `__dict__` writes on
    an instance that HAS no `__dict__`. The nonempty-slots MRO scan waves it
    through (empty slots are falsy), so pre-R9-P3 the factory crashed with
    a raw AttributeError out of the boundary instead of refusing by name."""

    __slots__ = ()
    _SESSIONS = (date(2018, 7, 2), date(2018, 7, 3), date(2018, 7, 5))

    def sessions(self):
        return list(self._SESSIONS)

    def early_close_sessions(self):
        return frozenset()

    def is_session(self, d):
        return d in self._SESSIONS

    def ordinal(self, d):
        return self._SESSIONS.index(d)

    def nth_after(self, d, n):
        return self._SESSIONS[self._SESSIONS.index(d) + n]

    def session_open(self, d):
        return datetime.combine(d, time(13, 30), tzinfo=UTC)

    def session_close(self, d):
        return datetime.combine(d, time(20, 0), tzinfo=UTC)

    def contains_instant(self, d, ts):
        return (
            datetime.combine(d, time(13, 30), tzinfo=UTC)
            <= ts
            <= datetime.combine(d, time(20, 0), tzinfo=UTC)
        )


class _StatelessStructuralSurface:
    """The surface-side twin: `__slots__ = ()`, no `__dict__` anywhere — the
    same named refusal on the surface path."""

    __slots__ = ()

    def decision_close(self, decision_session: date) -> datetime:
        return datetime.combine(decision_session, time(20, 0), tzinfo=UTC)


def test_an_empty_slots_no_dict_calendar_refuses_by_name() -> None:
    """(R9-P3, the empty-slots horn — calendar side) RED before R9-P3: the
    falsy `__slots__ = ()` passed the nonempty-slots scan and the factory
    crashed with a raw AttributeError out of the boundary; the refusal is
    now BY NAME, before registration."""
    from tree_options.trials.options_run import _bind_decision_calendar

    stateless = _StatelessStructuralCalendar()
    assert not hasattr(stateless, "__dict__")  # the shape is real
    first = _StatelessStructuralCalendar._SESSIONS[0]
    with pytest.raises(ValueError, match="no instance __dict__"):
        _bind_decision_calendar(stateless, {first: stateless.session_close(first)})  # type: ignore[arg-type]


def test_an_empty_slots_no_dict_surface_refuses_by_name() -> None:
    """(R9-P3, the empty-slots horn — surface side) The surface factory
    shares the `__dict__`-only copy, so it shares the named refusal."""
    from tree_options.trials.options_run import _bind_decision_surface

    stateless = _StatelessStructuralSurface()
    assert not hasattr(stateless, "__dict__")  # the shape is real
    first = date(2018, 7, 2)
    with pytest.raises(ValueError, match="no instance __dict__"):
        _bind_decision_surface(  # type: ignore[arg-type]
            stateless, {first: stateless.decision_close(first)}
        )


# ---- (round-10 debt-f, Codex round 10) the scan reads CLASS DICTS, not introspection ----


class _IntrospectionHidingDescriptor:
    """(round-10 debt-f) The probe descriptor: `__set__` is DEFINED — a real
    data descriptor under CPython's protocol, which consults the TYPE's
    slots/class dicts, never the object's `__getattribute__` — but the
    descriptor's own class OVERRIDES `__getattribute__` to raise
    AttributeError for `'__set__'`/`'__delete__'`, so the R9-P2 scan's
    `hasattr` classification (which consults exactly that override) sees a
    non-descriptor. `__set__` ACCEPTS the frozen closure; stateful `__get__`
    returns it for exactly the FIRST post-install read — the R8-P2
    verification — then answers the instance's lying callable
    (`_descriptor_lie`, the no-early-close twin's 16:00) on every later
    read; before any install it answers `_descriptor_honest` (the stamped
    grid's close), so the preflight's per-session reads pass."""

    def __init__(self) -> None:
        self._installed: dict[int, object] = {}
        self._reads: dict[int, int] = {}

    def __getattribute__(self, name):
        if name in ("__set__", "__delete__"):
            raise AttributeError(name)  # the hide: hasattr consults this
        return super().__getattribute__(name)

    def __set__(self, obj, value) -> None:
        self._installed[id(obj)] = value
        self._reads[id(obj)] = 0

    def __get__(self, obj, objtype=None):
        if obj is None:
            return self
        reads = self._reads.get(id(obj), 0) + 1
        self._reads[id(obj)] = reads
        installed = self._installed.get(id(obj))
        if installed is None:
            return obj._descriptor_honest  # pre-install: the preflight reads
        if reads == 1:
            return installed  # the verification: identity holds exactly once
        return obj._descriptor_lie  # every later read lies


class _IntrospectionHidingSurface(VwapPitSurface):
    """The surface-side probe: `decision_close` IS the introspection-hiding
    descriptor. The SURFACE class itself overrides nothing the scan names
    (the `__getattribute__` override lives on the DESCRIPTOR's class, which
    the surface MRO never sees), so pre-debt-f the scan passes, `__set__`
    accepts the install, and the first post-install read returns it — only
    a classification that reads class DICTS, as CPython's own protocol
    does, can name the shape."""

    def __init__(self, overlay, grid) -> None:
        super().__init__(overlay)
        self._lying_grid = grid
        self._descriptor_honest = grid.session_close
        self._descriptor_lie = RealSessionCalendar(grid.sessions(), frozenset()).session_close

    @property
    def decision_calendar(self):
        return self._lying_grid

    decision_close = _IntrospectionHidingDescriptor()


def test_an_introspection_hiding_descriptor_is_refused_by_name(era_world) -> None:
    """(round-10 debt-f — the hidden-`__set__` horn) `_refuse_descriptor_
    seams` classified the seam with `hasattr(seam_attr, "__set__")` /
    `hasattr(seam_attr, "__delete__")` — attribute access on the descriptor
    OBJECT, i.e. through its own overridable `__getattribute__`. A hostile
    descriptor class that DEFINES `__set__` (a real data descriptor under
    the protocol, which consults the type's class dicts) while raising
    AttributeError for those two names on introspection therefore passed
    the scan, accepted the installed closure, returned it for the one-time
    read-back, and answered an unfrozen callable on every later read (RED
    before debt-f: the bind succeeded — DID NOT RAISE). The scan now walks
    `type(seam_attr).__mro__` and refuses when any class's `__dict__`
    contains `__set__`/`__delete__` — the protocol's actual rule, unhidable
    by any instance `__getattribute__`."""
    from tree_options.time.calendar import calendar_content_sha256
    from tree_options.trials.options_run import _bind_decision_surface

    grid, overlay = era_world
    # NO MASKING — the probe defeats exactly the introspection the scan
    # used, and nothing else: hasattr sees no __set__, the digest binds
    # (the disclosure is the stamped grid), and the install verification
    # holds for its one read — proven on the spent twin below
    hidden = _IntrospectionHidingSurface.decision_close
    assert not hasattr(hidden, "__set__")  # the hide is real
    assert not hasattr(hidden, "__delete__")
    assert "__set__" in type(hidden).__dict__  # ...and so is the descriptor
    probe = _IntrospectionHidingSurface(overlay, grid)
    assert calendar_content_sha256(probe.decision_calendar) == calendar_content_sha256(grid)
    sentinel = object()
    probe.decision_close = sentinel  # __set__ ACCEPTS the install
    assert probe.decision_close is sentinel  # read 1: the verification passes
    assert probe.decision_close is not sentinel  # read 2+: the lying callable
    liar = _IntrospectionHidingSurface(overlay, grid)
    first = grid.sessions()[0]
    with pytest.raises(ValueError, match="defines decision_close as a class-level data descriptor"):
        _bind_decision_surface(liar, {first: grid.session_close(first)})


# ---- (round-10 debt-g, Codex round 10) the __dict__-copy is a REAL dict --------


class _RaisingDictMapping:
    """(round-10 debt-g) The loud horn: a hostile mapping whose `update`
    raises, so the unguarded `bound.__dict__.update(...)` threw a RAW
    RuntimeError out of the boundary instead of refusing by name."""

    def update(self, *args, **kwargs) -> None:
        raise RuntimeError("the hostile mapping refuses the copy")


class _NoopDictMapping:
    """(round-10 debt-g) The silent horn: a mapping whose `update` accepts
    and drops everything, so the state copy silently vanished and the run
    failed LATER, after registration, on the half-bound instance."""

    def update(self, *args, **kwargs) -> None:
        pass


class _RaisingDictCalendar(RealSessionCalendar):
    """The calendar-side probe: `__dict__` is a class-level property
    returning the RAISING mapping — present (the presence-only check
    passed), never a real dict."""

    def __init__(self, grid) -> None:
        super().__init__(grid.sessions(), frozenset(grid.early_close_sessions()))

    @property
    def __dict__(self):
        return _RaisingDictMapping()


class _NoopDictSurface(VwapPitSurface):
    """The surface-side twin: `__dict__` is the same kind of property
    returning the NO-OP mapping — the copy drops the state silently."""

    def __init__(self, overlay, grid, liquidity_term) -> None:
        super().__init__(overlay, decision_calendar=grid, underlying_liquidity_term=liquidity_term)

    @property
    def __dict__(self):
        return _NoopDictMapping()


def test_a_raising_dict_property_calendar_refuses_by_name(era_world) -> None:
    """(round-10 debt-g — the raw-escape horn, calendar side)
    `_refuse_dictless_bind` proved only `hasattr(bound, "__dict__")`
    (presence), and the `bound.__dict__.update(...)` call was unguarded:
    a class-level `__dict__` property returning a mapping whose `update`
    RAISES escaped the boundary as the RAW exception (RED before debt-g:
    RuntimeError, never a named ValueError). The refusal now requires
    `type(bound.__dict__) is dict` — a property-returned mapping cannot be
    a real dict — and both factories wrap the copy itself in the same
    named refusal."""
    from tree_options.trials.options_run import _bind_decision_calendar

    grid, _overlay = era_world
    liar = _RaisingDictCalendar(grid)
    first = grid.sessions()[0]
    # NO MASKING — the probe defeats exactly the presence-only check the
    # refusal used: __dict__ IS present, it is simply not a real dict
    assert hasattr(liar, "__dict__")
    assert type(liar.__dict__) is not dict
    with pytest.raises(ValueError, match="not a real dict"):
        _bind_decision_calendar(liar, {first: grid.session_close(first)})


def test_a_noop_dict_property_surface_refuses_by_name(era_world) -> None:
    """(round-10 debt-g — the silent-drop horn, surface side) The same
    property shape returning a NO-OP mapping was worse than the raw
    escape: the copy dropped every attribute, the install still landed in
    the real per-instance storage (attribute writes do not consult the
    property), the one-time read-back held identity, and the bind returned
    a state-stripped instance — accepted silently, failing only later,
    after registration (RED before debt-g: DID NOT RAISE). The dict-type
    requirement refuses it BY NAME, before any state moves."""
    from tree_options.trials.options_run import _bind_decision_surface

    grid, overlay = era_world
    surface = _NoopDictSurface(overlay, grid, "evaluated")
    first = grid.sessions()[0]
    assert hasattr(surface, "__dict__")  # presence alone is not dict-hood
    assert type(surface.__dict__) is not dict
    with pytest.raises(ValueError, match="not a real dict"):
        _bind_decision_surface(surface, {first: grid.session_close(first)})
