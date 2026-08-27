"""Workstream F: the options trial runner (M3 plan §3.F)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from tests.conftest import REPO_ROOT
from tree_options.data.authority import PointInTimeDataset
from tree_options.data.options_pit import OptionPitSurface
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
    **kwargs,
):
    """One fixture-scale trial; returns the stamped artifact body."""
    import json

    overlay, calendar, snapshot, dataset = world
    surface = OptionPitSurface(overlay)
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
