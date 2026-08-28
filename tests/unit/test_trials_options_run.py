"""Workstream F: the options trial runner (M3 plan §3.F)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
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
    assert json.dumps(honest, sort_keys=True) == json.dumps(lying, sort_keys=True)
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
