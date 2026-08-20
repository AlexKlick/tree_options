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


def test_first_decision_session_cohort_is_tradable(world, surface, relaxed_filter) -> None:
    """Review r1 P1-1: the fold's earliest close(d) decision precedes the
    first execution session, so its pending entries must be seeded before
    the loop — previously the first scored cohort was silently dropped
    (the loop begins at d+1 and only converted signals on visited sessions)."""
    overlay, calendar, _snap, dataset = world
    sessions = overlay.world_sessions()
    result = run_options_backtest(
        calendar=calendar,
        surface=surface,
        dataset=dataset,
        candidate_filter=relaxed_filter,
        signals=_signals(world, surface, n_decision_sessions=1),
        initial_cash=D("100000.00"),
        config=CONFIG,
        arm="A",
        end_session=sessions[105],
    )
    assert result.fills, "the first decision session's cohort must generate entries"
    first_decision = sessions[95]
    assert {p.entry_session for p in result.positions} == {calendar.nth_after(first_decision, 1)}, (
        "entries must execute on the session after the first decision"
    )


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


class _OvernightAskJumpSurface:
    """Delegates to the real surface but multiplies every ask visible on ONE
    session by `factor` — the overnight-gap scenario of review r1 P1-4 (bids
    unchanged, so marks are unaffected)."""

    def __init__(self, inner, on_date, factor):
        self._inner = inner
        self._date = on_date
        self._factor = factor

    def visible_quotes_as_of(self, contract_id: str, as_of):
        quotes = self._inner.visible_quotes_as_of(contract_id, as_of)
        if as_of.date() != self._date:
            return quotes
        return tuple(
            q.model_copy(update={"ask": (q.ask * self._factor).quantize(D("0.01"))}) for q in quotes
        )

    def entry_as_of(self, underlying_id: str, as_of, contract_id: str):
        entry = self._inner.entry_as_of(underlying_id, as_of, contract_id)
        if entry is None or as_of.date() != self._date:
            return entry
        bumped = entry.quote_eod.model_copy(
            update={"ask": (entry.quote_eod.ask * self._factor).quantize(D("0.01"))}
        )
        return entry.model_copy(update={"quote_eod": bumped})

    def __getattr__(self, name: str):
        return getattr(self._inner, name)


def test_execution_gap_cannot_spend_past_the_candidate_budget(
    world, surface, relaxed_filter
) -> None:
    """Review r1 P1-4: plan_orders sizes against the decision-visible
    per-candidate premium budget; the execution re-clamp must PRESERVE that
    budget when the ask gaps overnight. The old re-clamp checked solvency
    against total cash only, so a 100x gap still filled against the whole
    ledger."""
    overlay, calendar, _snap, dataset = world
    sessions = overlay.world_sessions()
    decision = sessions[95]
    execution = calendar.nth_after(decision, 1)
    jumped = _OvernightAskJumpSurface(surface, execution, D("100"))
    result = run_options_backtest(
        calendar=calendar,
        surface=jumped,
        dataset=dataset,
        candidate_filter=relaxed_filter,
        signals=_signals(world, surface, n_decision_sessions=1),
        initial_cash=D("100000.00"),
        config=CONFIG,
        arm="A",
        end_session=sessions[105],
    )
    entered = [p for p in result.positions if p.entry_session == execution]
    assert not entered, (
        "a 100x overnight ask gap must clamp every entry back inside the "
        "per-candidate premium budget (to zero here), not fill against "
        "total ledger cash"
    )
    assert result.counters.entry_fill_rejections.get("UNAFFORDABLE_AT_EXECUTION", 0) >= 1


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


def test_fill_audit_stamps_t1_discipline(world, surface, relaxed_filter) -> None:
    """Sealed-gate criterion 2 inputs: every fill carries its decision
    instant/session and the SELECTED quote's receipt; execution is strictly
    the session after decision, and the quote was received by execution."""
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
    assert result.fills, "the fixture must mint at least one fill"
    assert {a.fill_id for a in result.fill_audit} == {f.fill_id for f in result.fills}, (
        "one audit row per fill"
    )
    for audit in result.fill_audit:
        assert calendar.ordinal(audit.execution_session) > calendar.ordinal(
            audit.decision_session
        ), "execution must be the session AFTER decision"
        assert audit.execution_at > audit.decision_at
        assert audit.quote_received_at <= audit.execution_at, "quote received by execution"
    # criterion 1 input: conservation asserted at EVERY evaluated session
    assert result.counters.conservation_checks == len(result.sessions)


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


class _DroppedEligibilitySurface(OptionPitSurface):
    """Review r2 P1-3 fixture: the underlying left the option-eligible
    top-N after `last_file_session`, so its latest visible file at any
    later instant is that stale session."""

    def __init__(self, overlay, sid, last_file_session) -> None:
        super().__init__(overlay)
        self._drop_sid = sid
        self._drop_last = last_file_session

    def visible_file_session(self, underlying_id, as_of):
        session = super().visible_file_session(underlying_id, as_of)
        if underlying_id == self._drop_sid and session is not None and session > self._drop_last:
            return self._drop_last
        return session


class _AugmentedDataset(PointInTimeDataset):
    """Same snapshot, extra injected actions / truncated bars (path tests)."""

    def __init__(
        self,
        snapshot,
        calendar,
        *,
        universe_id,
        extra_actions=(),
        drop_bars_after=None,
    ) -> None:
        super().__init__(snapshot, calendar, universe_id=universe_id)
        self._extra_actions = tuple(extra_actions)
        self._drop_bars_after = drop_bars_after  # (sid, last_session_inclusive)

    @property
    def actions(self):  # type: ignore[override]
        return (*super().actions, *self._extra_actions)

    @property
    def bars(self):  # type: ignore[override]
        if self._drop_bars_after is None:
            return super().bars
        sid, last = self._drop_bars_after
        return tuple(b for b in super().bars if b.security_id != sid or b.session <= last)


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
    signals = _signals(world, surface, n_decision_sessions=3)
    # dry-run first: inject the action on an underlying that DETERMINISTICALLY
    # entered, so the force-close assertion is unconditional (mutant M120)
    dry = run_options_backtest(
        calendar=calendar,
        surface=surface,
        dataset=_dataset,
        candidate_filter=relaxed_filter,
        signals=signals,
        initial_cash=D("100000.00"),
        config=CONFIG,
        arm="A",
        end_session=sessions[100],
    )
    assert dry.positions, "the fixture must enter at least one position"
    held_underlying = dry.positions[0].underlying_security_id
    entry_session = dry.positions[0].entry_session
    # announced 23:00 UTC of the entry session (after the fill, before the
    # next 10:00 window): the force-close trigger
    announce_at = calendar.session_close(entry_session) + timedelta(hours=7)
    force_session = calendar.nth_after(entry_session, 1)
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
    result = run_options_backtest(
        calendar=calendar,
        surface=surface,
        dataset=augmented,
        candidate_filter=relaxed_filter,
        signals=signals,
        initial_cash=D("100000.00"),
        config=CONFIG,
        arm="A",
        end_session=sessions[101],
    )
    assert any(p.underlying_security_id == held_underlying for p in result.positions), (
        "the dry-run-entered name must still enter under the injected action"
    )
    forced = [
        p
        for p in result.positions
        if p.underlying_security_id == held_underlying
        and p.exit_kind == "sell"
        and p.exit_session == force_session
    ]
    assert forced, "a held position on the split name must force-close at the next window"
    assert result.counters.force_closes >= 1


def test_merger_terminal_strikes_at_effective_session(world, surface, relaxed_filter) -> None:
    """Review r1 P1-3: a merger published at close(t-1) and effective at t
    (with t's final bar present) must settle AT t against t's bar — the old
    availability-only check fired at t-1 and settled one session early
    against the pre-merger bar."""
    overlay, calendar, snapshot, _dataset = world
    sessions = overlay.world_sessions()
    decision = sessions[95]
    victim = sorted(surface.eligible_as_of(decision))[0]  # a top-quintile name
    effective = sessions[100]
    augmented = _AugmentedDataset(
        snapshot,
        calendar,
        universe_id=f"{snapshot.snapshot_id}|pit-universe-v1",
        extra_actions=[
            _extra_action(
                security_id=victim,
                kind="merger",
                effective_session=effective,
                successor_security_id="SYN-9999",
                available_at=calendar.session_close(sessions[99]),  # published t-1
                source_record_id="ACT-TERM-EFF",
            )
        ],
    )
    signals = tuple(
        OptionSignal(decision_session=decision, security_id=sid, score=(i + 1) / 10.0)
        for i, sid in enumerate(sorted(surface.eligible_as_of(decision)))
    )
    result = run_options_backtest(
        calendar=calendar,
        surface=surface,
        dataset=augmented,
        candidate_filter=relaxed_filter,
        signals=signals,
        initial_cash=D("100000.00"),
        config=CONFIG,
        arm="B",
        end_session=sessions[102],
    )
    victim_rows = [p for p in result.positions if p.underlying_security_id == victim]
    assert victim_rows, "the victim must be held"
    terminal_rows = [p for p in victim_rows if p.exit_kind == "terminal"]
    assert terminal_rows, "the merger must terminate the held position"
    assert all(p.exit_session == effective for p in terminal_rows), (
        "settlement strikes at close(effective_session) with the final bar, "
        "not one session early against the pre-merger bar"
    )


def test_late_published_merger_settles_without_backdated_stamp(
    world, surface, relaxed_filter
) -> None:
    """Review r2 P1-1: a merger effective at t (final bar on t) published at
    close(t+1) must settle at the DETECTION instant, priced at t's bar. The
    old path stamped the settlement at bar t's publication instant, so a
    fill executing at 10:00 on t+1 — between that publication and the
    settlement application — crashed OUT_OF_ORDER_SETTLEMENT."""
    overlay, calendar, snapshot, _dataset = world
    sessions = overlay.world_sessions()
    effective = sessions[100]
    detect_session = sessions[101]
    decision = sessions[100]  # decided at close(t), fills 10:00 on t+1
    victim = sorted(surface.eligible_as_of(decision))[0]  # a top-quintile name
    augmented = _AugmentedDataset(
        snapshot,
        calendar,
        universe_id=f"{snapshot.snapshot_id}|pit-universe-v1",
        extra_actions=[
            _extra_action(
                security_id=victim,
                kind="merger",
                effective_session=effective,
                successor_security_id="SYN-9999",
                available_at=calendar.session_close(detect_session) + timedelta(hours=7),
                source_record_id="ACT-TERM-LATE",
            )
        ],
        drop_bars_after=(victim, effective),  # the final bar is t; nothing after
    )
    signals = tuple(
        OptionSignal(decision_session=decision, security_id=sid, score=(i + 1) / 10.0)
        for i, sid in enumerate(sorted(surface.eligible_as_of(decision)))
    )
    result = run_options_backtest(
        calendar=calendar,
        surface=surface,
        dataset=augmented,
        candidate_filter=relaxed_filter,
        signals=signals,
        initial_cash=D("100000.00"),
        config=CONFIG,
        arm="B",
        end_session=sessions[103],
    )
    victim_rows = [p for p in result.positions if p.underlying_security_id == victim]
    assert victim_rows, "the victim must be held"
    terminal_rows = [p for p in victim_rows if p.exit_kind == "terminal"]
    assert terminal_rows, "the late-published merger must still terminate the position"
    assert all(p.exit_session == detect_session for p in terminal_rows), (
        "settlement lands at the detection session (ts = the action's "
        "publication), priced at the effective session's final bar"
    )


def test_mark_is_zero_when_prior_file_absent(world, surface, relaxed_filter) -> None:
    """Review r2 P1-3: when an open position's underlying has no file(t-1)
    (it left the option-eligible top-N), the close(t) mark must be the
    conservative ZERO with a mark_miss — the reach-back through
    entry_as_of returned the stale t-2 bid instead, overstating equity by
    bid(t-2) x quantity x 100."""
    overlay, calendar, _snap, dataset = world
    sessions = overlay.world_sessions()
    signals = _signals(world, surface, n_decision_sessions=3)
    common = dict(
        calendar=calendar,
        dataset=dataset,
        candidate_filter=relaxed_filter,
        signals=signals,
        initial_cash=D("100000.00"),
        config=CONFIG,
        arm="A",
        end_session=sessions[104],
    )
    dry = run_options_backtest(surface=surface, **common)
    assert dry.positions, "the fixture must enter at least one position"
    held = dry.positions[0]
    # the name's files stop after the entry executes: from the second
    # following close on, file(t-1) no longer exists for it
    dropped = _DroppedEligibilitySurface(
        surface.overlay, held.underlying_security_id, held.entry_session
    )
    result = run_options_backtest(surface=dropped, **common)
    assert result.counters.mark_misses > dry.counters.mark_misses, (
        "marking must treat the absent file(t-1) as a miss at zero, not "
        "reach back to the stale file's bid"
    )


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


def test_mark_uses_prior_file_eod_bid(world, surface, relaxed_filter) -> None:
    """Open positions are marked at the strictly-knowable file(t-1) EOD BID
    (mutant M123: marking at the ask inflates equity with a price the
    position could not realize)."""
    from tree_options.schemas.common import FEE_TICK

    _overlay, calendar, _snap, dataset = world
    sessions = world[0].world_sessions()
    result = run_options_backtest(
        calendar=calendar,
        surface=surface,
        dataset=dataset,
        candidate_filter=relaxed_filter,
        signals=_signals(world, surface, n_decision_sessions=3),
        initial_cash=D("100000.00"),
        config=CONFIG,
        arm="A",
        end_session=sessions[97],  # entry at 96, marks at 96/97 closes, no exit yet
    )
    open_rows = [p for p in result.positions if p.exit_kind is None]
    assert open_rows, "positions must still be open inside the mark window"
    qty_by_contract = {f.contract_id: f.quantity for f in result.fills if f.side == "buy"}
    close_at = calendar.session_close(sessions[97])
    expected_mv = Decimal("0")
    for row in open_rows:
        entry = surface.entry_as_of(row.underlying_security_id, close_at, row.contract_id)
        assert entry is not None, f"{row.contract_id} quoted on the visible file"
        expected_mv += (entry.quote_eod.bid * qty_by_contract[row.contract_id] * 100).quantize(
            FEE_TICK
        )
    assert result.equities[-1] == (result.terminal_cash + expected_mv).quantize(FEE_TICK)


def test_election_window_is_ten_oclock_visibility(world, surface, relaxed_filter) -> None:
    """The early-exercise election consumes only actions visible by the
    10:00 ET window (mutant M131: extending visibility to the session close
    would leak same-evening dividend announcements into the election)."""
    overlay, calendar, snapshot, _dataset = world
    sessions = overlay.world_sessions()
    signals = _signals(world, surface, n_decision_sessions=3)
    dry = run_options_backtest(
        calendar=calendar,
        surface=surface,
        dataset=_dataset,
        candidate_filter=relaxed_filter,
        signals=signals,
        initial_cash=D("100000.00"),
        config=CONFIG,
        arm="B",
        end_session=sessions[99],
    )
    assert dry.positions, "the fixture must enter at least one position"
    # branch (a) of the election applies to CALLS only — pick a call victim
    # so the dividend leak is the deciding input (mutant M131)
    victim_row = next((p for p in dry.positions if p.call_put == "C"), None)
    if victim_row is None:
        pytest.skip("fixture entered no call positions")
    victim = victim_row.underlying_security_id
    held_contract = victim_row.contract_id
    # the first 10:00 window at which the position is already held (entries
    # execute at step 4 of the window, elections at step 3 — one session later)
    election_session = calendar.nth_after(victim_row.entry_session, 1)
    # a giant dividend announced AT the session close (hours AFTER the 10:00
    # election window), effective inside the hold: knowable only at pub time
    announce_at = calendar.session_close(election_session)
    augmented = _AugmentedDataset(
        snapshot,
        calendar,
        universe_id=f"{snapshot.snapshot_id}|pit-universe-v1",
        extra_actions=[
            _extra_action(
                security_id=victim,
                kind="cash_dividend",
                effective_session=calendar.nth_after(election_session, 1),
                cash_amount=D("5000.00"),
                available_at=announce_at,
                source_record_id="ACT-ELECT-1",
            )
        ],
    )
    result = run_options_backtest(
        calendar=calendar,
        surface=surface,
        dataset=augmented,
        candidate_filter=relaxed_filter,
        signals=signals,  # the SAME signals that produced the dry-run victim
        initial_cash=D("100000.00"),
        config=CONFIG,
        arm="B",
        end_session=calendar.nth_after(election_session, 2),
    )
    early = [
        p
        for p in result.positions
        if p.contract_id == held_contract and p.exit_kind == "early_exercise"
    ]
    assert not early, (
        "the election must not see a dividend announced after the 10:00 window "
        f"({held_contract} elected on {early[0].exit_session if early else '-'})"
    )


def test_silent_death_settles_at_last_bar(world, surface, relaxed_filter) -> None:
    """bankruptcy_11 / voluntary_delisting / coverage_lapse deaths emit NO
    action record (only mergers do) — the dev run crashed on exactly this:
    a held option rode to a barless expiry and the fail-closed settlement
    guard fired. The complete-vendor inference (a barless session for a
    held name) must settle the position at the LAST bar's close, stamped
    at the detection instant (23:00 UTC of the barless session)."""
    overlay, calendar, snapshot, _dataset = world
    sessions = overlay.world_sessions()
    # pick a name that deterministically ENTERS: dry-run the same signals,
    # then truncate the underlying of the first entered position right
    # after its entry session
    dry = run_options_backtest(
        calendar=calendar,
        surface=surface,
        dataset=_dataset,
        candidate_filter=relaxed_filter,
        signals=_signals(world, surface),
        initial_cash=D("100000.00"),
        config=CONFIG,
        arm="B",
        end_session=sessions[108],
    )
    assert dry.positions, "the fixture must enter at least one position"
    victim_row = dry.positions[0]
    victim = victim_row.underlying_security_id
    entry_session = victim_row.entry_session
    barless_session = calendar.nth_after(entry_session, 1)
    truncated = _AugmentedDataset(
        snapshot,
        calendar,
        universe_id=f"{snapshot.snapshot_id}|pit-universe-v1",
        drop_bars_after=(victim, entry_session),
    )
    result = run_options_backtest(
        calendar=calendar,
        surface=surface,
        dataset=truncated,
        signals=_signals(world, surface),
        candidate_filter=relaxed_filter,
        initial_cash=D("100000.00"),
        config=CONFIG,
        arm="B",
        end_session=calendar.nth_after(entry_session, 3),
    )
    victim_rows = [p for p in result.positions if p.underlying_security_id == victim]
    assert victim_rows, "the victim must have entered"
    row = victim_rows[0]
    assert row.exit_kind == "terminal"
    assert row.exit_session == barless_session
    # exit price == intrinsic vs the LAST bar's close (the entry session's
    # bar is the victim's final one)
    last_close = next(
        Decimal(str(b.close))
        for b in truncated.bars
        if b.security_id == victim and b.session == entry_session
    )
    contract = surface.contract(row.contract_id)
    expected = max(
        (last_close - contract.strike)
        if contract.call_put == "C"
        else (contract.strike - last_close),
        Decimal("0"),
    )
    assert row.exit_price == expected
    assert row.premium_return is not None
    assert result.counters.terminals >= 1
