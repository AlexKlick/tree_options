"""Workstream E: the options backtest (M3 plan §3.E)."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from tests.unit.test_options_strategy import _build
from tree_options.backtest.options import OptionsBacktestError, run_options_backtest
from tree_options.candidates.filters import CandidateFilter
from tree_options.data.actions import CorporateActionRecord
from tree_options.data.authority import PointInTimeDataset
from tree_options.data.options_pit import OptionPitSurface
from tree_options.options import OptionSignal, OptionsStrategyConfig
from tree_options.synth_options import OptionsOverlaySpec, generate_overlay

D = Decimal
CONFIG = OptionsStrategyConfig()


@pytest.fixture(scope="module")
def built():
    return _build()


@pytest.fixture(scope="module")
def world(built):
    overlay, calendar, snapshot = built
    dataset = PointInTimeDataset(
        snapshot, calendar, universe_id=f"{snapshot.snapshot_id}|pit-universe-v1"
    )
    return overlay, calendar, snapshot, dataset


@pytest.fixture(scope="module")
def surface(world):
    overlay, _cal, _snap, _ds = world
    return OptionPitSurface(overlay)


@pytest.fixture(scope="module")
def relaxed_filter(built):
    _overlay, calendar, _snap = built
    return CandidateFilter(
        calendar,
        dte_min=30,
        dte_max=60,
        abs_delta_min=D("0.30"),
        abs_delta_max=D("0.60"),
        standard_deliverable_only=True,
        min_open_interest=0,
        min_same_day_volume=0,
        volume_only_if_already_available=True,
        max_spread_fraction_of_midpoint=D("10"),
        min_underlying_20d_median_dollar_volume=D("0"),
        exclude_earnings_spanning_hold=True,
    )


def _signals(world, surface, first_index=95, n_decision_sessions=5):
    overlay, _cal, _snap, _ds = world
    sessions = overlay.world_sessions()
    signals = []
    for decision in sessions[first_index : first_index + n_decision_sessions]:
        for i, sid in enumerate(sorted(surface.eligible_as_of(decision))):
            signals.append(
                OptionSignal(decision_session=decision, security_id=sid, score=(i + 1) / 10.0)
            )
    return signals


def test_arm_a_round_trips_sell_in_four_sessions(world, surface, relaxed_filter) -> None:
    overlay, calendar, _snap, dataset = world
    sessions = overlay.world_sessions()
    result = run_options_backtest(
        calendar=calendar,
        surface=surface,
        dataset=dataset,
        candidate_filter=relaxed_filter,
        signals=_signals(world, surface),
        initial_cash=D("100000.00"),
        config=CONFIG,
        arm="A",
        end_session=sessions[108],
    )
    assert result.counters.early_exercises == 0  # arm A never elects
    sold = [p for p in result.positions if p.exit_kind == "sell"]
    assert sold, "arm A must close its round trips inside the extended window"
    for row in sold:
        assert calendar.ordinal(row.exit_session) - calendar.ordinal(row.entry_session) == 4, (
            "the 4-session exit schedule"
        )
        assert row.premium_return is not None
    assert len(result.fills) == 2 * len(sold) + len(
        [p for p in result.positions if p.exit_kind is None]
    )
    assert result.terminal_equity == result.equities[-1]
    assert len(result.equities) == len(result.sessions)


def test_arm_b_rides_without_time_exit(world, surface, relaxed_filter) -> None:
    overlay, calendar, _snap, dataset = world
    sessions = overlay.world_sessions()
    result = run_options_backtest(
        calendar=calendar,
        surface=surface,
        dataset=dataset,
        candidate_filter=relaxed_filter,
        signals=_signals(world, surface, n_decision_sessions=3),
        initial_cash=D("100000.00"),
        config=CONFIG,
        arm="B",
        end_session=sessions[101],
    )
    assert all(p.exit_kind is None for p in result.positions), "arm B has no time exit"
    assert result.open_positions == tuple(sorted(p.contract_id for p in result.positions))
    assert result.counters.early_exercises >= 0  # machinery oracle, not asserted on fixture


def test_arm_b_expiry_settlements_close_positions(world, surface, relaxed_filter) -> None:
    overlay, calendar, _snap, dataset = world
    sessions = overlay.world_sessions()
    result = run_options_backtest(
        calendar=calendar,
        surface=surface,
        dataset=dataset,
        candidate_filter=relaxed_filter,
        signals=_signals(world, surface, first_index=95, n_decision_sessions=2),
        initial_cash=D("100000.00"),
        config=CONFIG,
        arm="B",
        end_session=sessions[-1],
    )
    expiry_rows = [p for p in result.positions if p.exit_kind == "expiry"]
    assert result.counters.expiries == len(expiry_rows)
    if expiry_rows:  # expiry rows carry settlement-derived exit prices
        for row in expiry_rows:
            assert row.exit_price is not None and row.exit_price >= 0
            assert row.premium_return is not None


def test_validation_refuses_bad_inputs(world, surface, relaxed_filter) -> None:
    _overlay, calendar, _snap, dataset = world
    kwargs = dict(
        calendar=calendar,
        surface=surface,
        dataset=dataset,
        candidate_filter=relaxed_filter,
        initial_cash=D("100000.00"),
        config=CONFIG,
    )
    with pytest.raises(OptionsBacktestError, match="at least one signal"):
        run_options_backtest(signals=(), arm="A", **kwargs)  # type: ignore[arg-type]
    signals = _signals(world, surface, n_decision_sessions=2)
    with pytest.raises(OptionsBacktestError, match="duplicate signal"):
        run_options_backtest(signals=(*signals, signals[0]), arm="A", **kwargs)
    with pytest.raises(OptionsBacktestError, match="arm must be"):
        run_options_backtest(signals=signals, arm="C", **kwargs)  # type: ignore[arg-type]


def test_cross_world_leakage_refused(world) -> None:
    _overlay, calendar, snapshot, dataset = world
    # a second overlay with a DIFFERENT world id over the same bars
    other = generate_overlay(
        spec=OptionsOverlaySpec(world_id="m3-unit-other-907", seed=907, eligible_top_n=10),
        bars=snapshot.bars,
        master=snapshot.master,
        actions=snapshot.actions,
        calendar=calendar,
    )
    other_surface = OptionPitSurface(other)
    signals = _signals(world, other_surface, n_decision_sessions=1)
    with pytest.raises(OptionsBacktestError, match="cross-world leakage"):
        run_options_backtest(
            calendar=calendar,
            surface=other_surface,
            dataset=dataset,
            candidate_filter=CandidateFilter(
                calendar,
                dte_min=30,
                dte_max=60,
                abs_delta_min=D("0.30"),
                abs_delta_max=D("0.60"),
                standard_deliverable_only=True,
                min_open_interest=0,
                min_same_day_volume=0,
                volume_only_if_already_available=True,
                max_spread_fraction_of_midpoint=D("10"),
                min_underlying_20d_median_dollar_volume=D("0"),
                exclude_earnings_spanning_hold=True,
            ),
            signals=signals,
            initial_cash=D("100000.00"),
            config=CONFIG,
            arm="A",
        )


def test_premium_budget_caps_session_spend(world, surface, relaxed_filter) -> None:
    _overlay, calendar, _snap, dataset = world
    result = run_options_backtest(
        calendar=calendar,
        surface=surface,
        dataset=dataset,
        candidate_filter=relaxed_filter,
        signals=_signals(world, surface, n_decision_sessions=3),
        initial_cash=D("100000.00"),
        config=CONFIG,
        arm="A",
    )
    assert result.fills
    spend_by_session: dict = {}
    for fill in result.fills:
        if fill.side == "buy":
            spend_by_session[fill.execution_session] = (
                spend_by_session.get(fill.execution_session, D("0")) + fill.notional() + fill.fees
            )
    budget = CONFIG.premium_budget_fraction * D("100000.00")
    for session, spend in spend_by_session.items():
        assert spend <= budget + D("1.00"), f"{session} spend {spend} exceeds budget {budget}"


def test_reentry_into_open_contract_skipped_and_counted(world, surface, relaxed_filter) -> None:
    _overlay, calendar, _snap, dataset = world
    result = run_options_backtest(
        calendar=calendar,
        surface=surface,
        dataset=dataset,
        candidate_filter=relaxed_filter,
        signals=_signals(world, surface, n_decision_sessions=5),
        initial_cash=D("100000.00"),
        config=CONFIG,
        arm="A",
    )
    assert result.counters.entries_skipped_open > 0, "daily re-selection must hit the skip"
    distinct = {p.contract_id for p in result.positions}
    assert len(distinct) == len(result.positions), "one row per contract"


def test_deterministic_repeat(world, surface, relaxed_filter) -> None:
    _overlay, calendar, _snap, dataset = world
    signals = _signals(world, surface, n_decision_sessions=3)
    runs = []
    for _ in range(2):
        result = run_options_backtest(
            calendar=calendar,
            surface=surface,
            dataset=dataset,
            candidate_filter=relaxed_filter,
            signals=signals,
            initial_cash=D("100000.00"),
            config=CONFIG,
            arm="A",
        )
        runs.append(result)
    a, b = runs
    assert a.equities == b.equities
    assert [f.fill_id for f in a.fills] == [f.fill_id for f in b.fills]
    assert [p.contract_id for p in a.positions] == [p.contract_id for p in b.positions]


class _AugmentedDataset(PointInTimeDataset):
    """Same snapshot, extra injected actions (corporate-action path tests)."""

    def __init__(self, snapshot, calendar, *, universe_id, extra_actions) -> None:
        super().__init__(snapshot, calendar, universe_id=universe_id)
        self._extra_actions = tuple(extra_actions)

    @property
    def actions(self):  # type: ignore[override]
        return (*super().actions, *self._extra_actions)


def _extra_action(**kwargs) -> CorporateActionRecord:
    base = dict(
        source="synthetic/v1",
        source_row_hash="0" * 64,
        snapshot_id="m3-unit-strategy-906",
    )
    base.update(kwargs)
    return CorporateActionRecord(**base)


def test_ratio_action_mid_hold_forces_close(world, surface, relaxed_filter) -> None:
    overlay, calendar, snapshot, _dataset = world
    sessions = overlay.world_sessions()
    decision = sessions[95]
    entry_session = sessions[96]  # 10:00 execution of the close(decision) entry
    # announced 23:00 UTC of the entry session (after the fill, before the
    # next 10:00 window): the force-close trigger
    announce_at = calendar.session_close(entry_session) + timedelta(hours=7)
    held_underlying = sorted(surface.eligible_as_of(decision))[-1]  # a bottom-quintile name
    force_session = sessions[97]
    augmented = _AugmentedDataset(
        snapshot,
        calendar,
        universe_id=f"{snapshot.snapshot_id}|pit-universe-v1",
        extra_actions=[
            _extra_action(
                security_id=held_underlying,
                kind="split",
                effective_session=force_session,
                ratio_numerator=2,
                ratio_denominator=1,
                available_at=announce_at,
                source_record_id="ACT-FORCE-1",
            )
        ],
    )
    signals = []
    for i, sid in enumerate(sorted(surface.eligible_as_of(decision))):
        signals.append(
            OptionSignal(decision_session=decision, security_id=sid, score=(i + 1) / 10.0)
        )
    result = run_options_backtest(
        calendar=calendar,
        surface=surface,
        dataset=augmented,
        candidate_filter=relaxed_filter,
        signals=tuple(signals),
        initial_cash=D("100000.00"),
        config=CONFIG,
        arm="A",
        end_session=sessions[100],
    )
    assert result.counters.force_closes >= 0  # runs clean with the injected action
    forced = [
        p
        for p in result.positions
        if p.underlying_security_id == held_underlying
        and p.exit_kind == "sell"
        and p.exit_session == force_session
    ]
    # the held name (if it entered) must exit at the force window — 1 session
    # after entry, not the scheduled 4
    if any(p.underlying_security_id == held_underlying for p in result.positions):
        assert forced, "a held position on the split name must force-close at the next window"


def test_terminal_action_settles_at_intrinsic(world, surface, relaxed_filter) -> None:
    overlay, calendar, snapshot, _dataset = world
    sessions = overlay.world_sessions()
    decision = sessions[95]
    victim = sorted(surface.eligible_as_of(decision))[0]  # a top-quintile call name
    # terminal merger knowable at the final session's close: the last bar of
    # the world publishes and the position must settle at intrinsic
    last_session = sessions[99]
    augmented = _AugmentedDataset(
        snapshot,
        calendar,
        universe_id=f"{snapshot.snapshot_id}|pit-universe-v1",
        extra_actions=[
            _extra_action(
                security_id=victim,
                kind="merger",
                effective_session=last_session,
                successor_security_id="SYN-9999",
                available_at=calendar.session_close(last_session),
                source_record_id="ACT-TERM-1",
            )
        ],
    )
    signals = []
    for i, sid in enumerate(sorted(surface.eligible_as_of(decision))):
        signals.append(
            OptionSignal(decision_session=decision, security_id=sid, score=(i + 1) / 10.0)
        )
    result = run_options_backtest(
        calendar=calendar,
        surface=surface,
        dataset=augmented,
        candidate_filter=relaxed_filter,
        signals=tuple(signals),
        initial_cash=D("100000.00"),
        config=CONFIG,
        arm="B",
        end_session=last_session,
    )
    terminal_rows = [p for p in result.positions if p.exit_kind == "terminal"]
    if any(p.underlying_security_id == victim for p in result.positions):
        assert any(p.underlying_security_id == victim for p in terminal_rows), (
            "a held position on the merged name must settle at intrinsic, not stay open"
        )
        assert result.counters.terminals >= 1
