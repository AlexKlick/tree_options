"""Workstream D: candidate→order strategy (M3 plan §3.D)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from tests.conftest import REPO_ROOT
from tree_options.candidates.filters import CandidateFilter
from tree_options.data.actions import CorporateActionRecord
from tree_options.data.ingest import ingest_snapshot
from tree_options.data.options_pit import OptionPitSurface
from tree_options.ledger.fees import PerContractFeeModel
from tree_options.options import (
    OptionSignal,
    OptionsStrategyConfig,
    affordable_contracts,
    build_candidates,
    cancellations_at_execution,
    classify_action,
    exit_decision_session,
    pending_dividend_per_share,
    plan_exit_order,
    plan_orders,
)
from tree_options.synth import generate_world
from tree_options.synth.spec import WorldSpec
from tree_options.synth_options import OptionsOverlaySpec, generate_overlay

D = Decimal
WORLD_ID = "m3-unit-strategy-906"
N_SESSIONS = 200


def _build():
    from tree_options.time.calendar import StaticSessionCalendar

    calendar = StaticSessionCalendar(
        REPO_ROOT / "data" / "calendar" / "nyse_sessions_2018_01_02_2026_12_31.json",
        REPO_ROOT / "data" / "calendar" / "nyse_sessions_2018_01_02_2026_12_31.sha256",
    )
    spec = WorldSpec(
        world_id=WORLD_ID, seed=906, kind="null", n_securities=24, n_sessions=N_SESSIONS
    )
    world = generate_world(spec, calendar)
    snapshot = ingest_snapshot(
        world.payload, world.master, snapshot_id=spec.world_id, normalization_code_sha="0" * 64
    )
    overlay = generate_overlay(
        spec=OptionsOverlaySpec(world_id=WORLD_ID, seed=906, eligible_top_n=10),
        bars=snapshot.bars,
        master=snapshot.master,
        actions=snapshot.actions,
        calendar=calendar,
    )
    return overlay, calendar, snapshot


@pytest.fixture(scope="module")
def built():
    return _build()


@pytest.fixture(scope="module")
def surface(built):
    overlay, _cal, _snap = built
    return OptionPitSurface(overlay)


@pytest.fixture(scope="module")
def calendar(built):
    _overlay, calendar, _snap = built
    return calendar


@pytest.fixture(scope="module")
def protocol():
    from tree_options.protocol.loader import load_protocol

    return load_protocol()


@pytest.fixture(scope="module")
def candidate_filter(calendar):
    """Relaxed thresholds: this suite tests the SELECTION rules (bands,
    direction, budget), not the frozen §9.2 numbers — the protocol filter's
    acceptance path is proven in test_data_options_surface.py. Zeroing the
    volume/liquidity/OI minima keeps the small fixture world usable."""
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


@pytest.fixture(scope="module")
def decision_session(built):
    overlay, calendar, _snap = built
    session = overlay.world_sessions()[80]
    assert overlay.has_any_file(session)
    return calendar.nth_after(session, 1)  # decision sees file(session)


@pytest.fixture(scope="module")
def cross_section(surface, decision_session):
    return surface.eligible_as_of(decision_session)


def test_the_base_surface_decision_close_is_its_own_calendar_close(
    surface, decision_session
) -> None:
    """(R3-P1-2, Codex round 3) lane-1/synthetic byte-identity of the new
    seam: the base `OptionPitSurface` answers `decision_close` from its OWN
    calendar, so every decision-side read in candidate construction
    (`build_candidates`' pending-action instant, the expiry/strike probes,
    the sizing entry read, `candidate_snapshot`'s stamp) is the same instant
    it was before the seam existed."""
    assert surface.decision_close(decision_session) == surface.overlay.calendar.session_close(
        decision_session
    )


@pytest.fixture(scope="module")
def scores(cross_section, decision_session):
    """Deterministic spread of scores over the eligible cross-section."""
    return tuple(
        OptionSignal(decision_session=decision_session, security_id=sid, score=(i + 1) / 10.0)
        for i, sid in enumerate(sorted(cross_section))
    )


CONFIG = OptionsStrategyConfig()


def test_config_validates_bands() -> None:
    with pytest.raises(ValueError, match="abs delta target"):
        OptionsStrategyConfig(target_abs_delta=D("0.65"))
    with pytest.raises(ValueError, match="dte target"):
        OptionsStrategyConfig(target_dte=90)
    with pytest.raises(ValueError, match="premium_budget_fraction"):
        OptionsStrategyConfig(premium_budget_fraction=D("1.5"))


def test_classify_action_table() -> None:
    assert classify_action("split") == "ratio"
    assert classify_action("reverse_split") == "ratio"
    assert classify_action("stock_dividend") == "ratio"
    assert classify_action("delisting") == "terminal"
    assert classify_action("merger") == "terminal"
    assert classify_action("cash_dividend") == "cash_dividend"
    assert classify_action("symbol_change") == "inert"


def test_build_candidates_direction_and_bands(
    surface, built, candidate_filter, decision_session, scores
) -> None:
    _overlay, _cal, _snap = built
    candidates = build_candidates(
        surface=surface,
        candidate_filter=candidate_filter,
        decision_session=decision_session,
        scores=scores,
        config=CONFIG,
    )
    assert candidates, "fixture cross-section must yield at least one candidate"
    by_sid = {c.underlying_security_id: c for c in candidates}
    ranked = sorted(scores, key=lambda row: (-row.score, row.security_id))
    count = max(1, -(-len(scores) // 5))
    top = {row.security_id for row in ranked[:count]}
    bottom = {row.security_id for row in ranked[-count:]}
    for sid, candidate in by_sid.items():
        expected_side = "C" if sid in top else "P"
        assert candidate.call_put == expected_side
        dte = (candidate.expiration - decision_session).days
        assert 30 <= dte <= 60
        assert CONFIG.abs_delta_min <= candidate.abs_delta <= CONFIG.abs_delta_max
        # every accepted candidate quotes on the visible file with a positive ask
        assert candidate.ask > 0
        assert candidate.bid >= 0
    assert set(by_sid) <= (top | bottom)


def _action(
    *,
    record_id: str,
    security_id: str,
    kind: str,
    effective_session: date,
    available_at: datetime,
    cash_amount: Decimal | None = None,
) -> CorporateActionRecord:
    extra: dict[str, object] = {}
    if kind == "split":
        extra = {"ratio_numerator": 2, "ratio_denominator": 1}
    if kind == "merger":
        extra = {"successor_security_id": "SYN-9999"}
    return CorporateActionRecord(
        security_id=security_id,
        kind=kind,  # type: ignore[arg-type]
        effective_session=effective_session,
        cash_amount=cash_amount,
        source="synthetic/v1",
        source_record_id=record_id,
        source_row_hash="0" * 64,
        snapshot_id=WORLD_ID,
        available_at=available_at,
        **extra,  # type: ignore[arg-type]
    )


def test_build_candidates_excludes_pending_action(
    surface, candidate_filter, decision_session, scores, calendar
) -> None:
    decision_at = calendar.session_close(decision_session)
    victim = sorted(scores, key=lambda row: row.score)[-1].security_id  # a bottom name
    pending = _action(
        record_id="ACT-TEST-1",
        security_id=victim,
        kind="split",
        effective_session=decision_session + timedelta(days=3),
        available_at=decision_at - timedelta(hours=1),
    )
    candidates = build_candidates(
        surface=surface,
        candidate_filter=candidate_filter,
        decision_session=decision_session,
        scores=scores,
        config=CONFIG,
        actions=(pending,),
    )
    assert all(c.underlying_security_id != victim for c in candidates)


def test_build_candidates_excludes_ratio_action_effective_on_decision(
    surface, candidate_filter, decision_session, scores, calendar
) -> None:
    """Review r2 P1-2: a split effective ON the decision session (announced
    by the decision) must also block entry. The decision-visible file (at
    best the prior session's) necessarily predates the action and the
    overlay never creates adjusted contracts — the strict
    effective > decision predicate admitted the name, and the
    (decision, execution] cancellation window missed the announcement too,
    so the pre-split fixed-strike deliverable filled against post-split
    prices."""
    decision_at = calendar.session_close(decision_session)
    victim = sorted(scores, key=lambda row: row.score)[-1].security_id  # a bottom name
    effective_now = _action(
        record_id="ACT-TEST-EFF-NOW",
        security_id=victim,
        kind="split",
        effective_session=decision_session,  # effective ON the decision session
        available_at=decision_at - timedelta(hours=1),
    )
    candidates = build_candidates(
        surface=surface,
        candidate_filter=candidate_filter,
        decision_session=decision_session,
        scores=scores,
        config=CONFIG,
        actions=(effective_now,),
    )
    assert all(c.underlying_security_id != victim for c in candidates), (
        "a ratio action effective on the decision session must exclude the "
        "name — its visible file is pre-action and no adjusted contract exists"
    )


def test_scores_outside_decision_session_are_ignored(
    surface, candidate_filter, decision_session, scores
) -> None:
    other = OptionSignal(
        decision_session=decision_session + timedelta(days=99),
        security_id=scores[0].security_id,
        score=99.0,
    )
    base = build_candidates(
        surface=surface,
        candidate_filter=candidate_filter,
        decision_session=decision_session,
        scores=scores,
        config=CONFIG,
    )
    with_other = build_candidates(
        surface=surface,
        candidate_filter=candidate_filter,
        decision_session=decision_session,
        scores=(*scores, other),
        config=CONFIG,
    )
    assert {c.contract_id for c in base} == {c.contract_id for c in with_other}


def test_strike_pick_is_nearest_in_band(surface, decision_session) -> None:
    """The picked |delta| is in band and no other ladder node is strictly
    closer to the target while staying in band (checked against the same
    visible file, single-entry reads)."""
    from tree_options.options.strategy import _pick_expiry, _pick_strike

    picked = None
    for sid in sorted(surface.eligible_as_of(decision_session)):
        expiration = _pick_expiry(surface, sid, decision_session, CONFIG)
        if expiration is None:
            continue
        picked = _pick_strike(surface, sid, decision_session, expiration, "C", CONFIG)
        if picked is not None:
            break
    assert picked is not None, "some eligible name must have an in-band call node"
    _strike, delta = picked
    assert CONFIG.abs_delta_min <= delta <= CONFIG.abs_delta_max
    # nearest in band: re-probe the ladder and confirm nothing is closer
    from tree_options.synth_options import contract_id_of

    decision_at = surface.overlay.calendar.session_close(decision_session)
    best = abs(delta - CONFIG.target_abs_delta)
    for other_strike in surface.strike_ladder(sid, expiration):
        entry = surface.entry_as_of(
            sid,
            decision_at,
            contract_id_of(sid, expiration, "C", other_strike),
        )
        if entry is None:
            continue
        if CONFIG.abs_delta_min <= entry.abs_delta <= CONFIG.abs_delta_max:
            assert abs(entry.abs_delta - CONFIG.target_abs_delta) >= best


def test_affordable_contracts_fee_inclusive() -> None:
    fees = PerContractFeeModel()  # 0.65/contract, 1.00 minimum
    # premium 1.00 x mult 100 = 100/contract; 3 contracts cost
    # 300 + max(3x0.65, 1.00) = 301.95
    assert (
        affordable_contracts(
            budget=D("301.90"), ask=D("1.00"), multiplier=100, fee_model=fees, cap=10
        )
        == 2
    )
    assert (
        affordable_contracts(
            budget=D("301.95"), ask=D("1.00"), multiplier=100, fee_model=fees, cap=10
        )
        == 3
    )
    # the FEE MINIMUM is the case the 0.65/contract estimate cannot see:
    # one contract costs 100 + max(0.65, 1.00) = 101.00, but the estimate
    # divides by 100.65 — a budget in (100.65, 101.00) is affordable only
    # if fees are actually checked (mutant M125)
    assert (
        affordable_contracts(
            budget=D("100.80"), ask=D("1.00"), multiplier=100, fee_model=fees, cap=10
        )
        == 0
    )
    assert (
        affordable_contracts(
            budget=D("50.00"), ask=D("1.00"), multiplier=100, fee_model=fees, cap=10
        )
        == 0
    )
    assert (
        affordable_contracts(
            budget=D("100000.00"), ask=D("1.00"), multiplier=100, fee_model=fees, cap=4
        )
        == 4
    )


def test_plan_orders_shape_and_budget(
    surface, candidate_filter, decision_session, scores, calendar
) -> None:
    candidates = build_candidates(
        surface=surface,
        candidate_filter=candidate_filter,
        decision_session=decision_session,
        scores=scores,
        config=CONFIG,
    )
    cash = D("100000.00")
    orders = plan_orders(
        calendar=calendar,
        candidates=candidates,
        cash=cash,
        config=CONFIG,
        fee_model=PerContractFeeModel(),
    )
    assert orders
    fees = PerContractFeeModel()
    per_candidate = (CONFIG.premium_budget_fraction * cash / len(candidates)).quantize(D("0.01"))
    by_id = {c.contract_id: c for c in candidates}
    for order in orders:
        candidate = by_id[order.contract_id]
        assert order.side == "buy" and order.intent == "open_long"
        assert order.decision_session == decision_session
        assert order.decision_at == calendar.session_close(decision_session)
        assert 1 <= order.quantity <= CONFIG.max_contracts_per_candidate
        cost = candidate.ask * order.quantity * 100 + fees.order_fees(order.quantity)
        assert cost <= per_candidate + D("0.01")
    ids = [o.order_id for o in orders]
    assert len(ids) == len(set(ids))


def test_exit_order_is_time_based(calendar) -> None:
    entry_exec = date(2019, 3, 6)
    decision = exit_decision_session(calendar, entry_exec, CONFIG)
    assert calendar.ordinal(decision) - calendar.ordinal(entry_exec) == 3
    order = plan_exit_order(
        calendar=calendar,
        contract_id="OPT-X",
        quantity=3,
        entry_execution_session=entry_exec,
        config=CONFIG,
    )
    assert order.side == "sell" and order.intent == "close_long"
    assert order.decision_session == decision
    assert order.decision_at == calendar.session_close(decision)
    assert order.quantity == 3
    assert order.order_id == f"OPT-S{decision:%Y%m%d}-OPT-X"


def _order_for(sid: str, decision_session: date, calendar) -> object:
    from tree_options.schemas.trading import Order

    return Order(
        order_id=f"OPT-B{decision_session:%Y%m%d}-0001",
        contract_id=sid,
        side="buy",
        intent="open_long",
        quantity=2,
        decision_at=calendar.session_close(decision_session),
        decision_session=decision_session,
    )


def test_cancellations_window_and_toggle(surface, decision_session, scores, calendar) -> None:
    contract_id = "OPT-SYN-0001-200315-C-00100000"
    order = _order_for(contract_id, decision_session, calendar)
    decision_at = calendar.session_close(decision_session)
    execution_at = calendar.session_open(calendar.nth_after(decision_session, 1)) + timedelta(
        hours=1
    )
    overnight = _action(
        record_id="ACT-TEST-2",
        security_id="SYN-0001",
        kind="merger",
        effective_session=calendar.nth_after(decision_session, 1),
        available_at=decision_at + timedelta(hours=2),
    )
    stale = _action(  # announced BEFORE decision: not a cancellation
        record_id="ACT-TEST-3",
        security_id="SYN-0001",
        kind="merger",
        effective_session=calendar.nth_after(decision_session, 1),
        available_at=decision_at - timedelta(hours=1),
    )
    too_late = _action(  # announced AFTER execution
        record_id="ACT-TEST-4",
        security_id="SYN-0001",
        kind="merger",
        effective_session=calendar.nth_after(decision_session, 1),
        available_at=execution_at + timedelta(hours=1),
    )
    mapping = {contract_id: "SYN-0001"}
    cancelled = cancellations_at_execution(
        orders=(order,),  # type: ignore[arg-type]
        surface=surface,
        actions=(stale, too_late, overnight),
        execution_at=execution_at,
        contract_underlying=mapping,
        config=CONFIG,
    )
    assert [o.order_id for o in cancelled] == [order.order_id]  # type: ignore[attr-defined]
    # toggle off -> no cancellations
    off = cancellations_at_execution(
        orders=(order,),  # type: ignore[arg-type]
        surface=surface,
        actions=(overnight,),
        execution_at=execution_at,
        contract_underlying=mapping,
        config=OptionsStrategyConfig(allow_cancellation=False),
    )
    assert off == ()


def test_pending_dividend_window() -> None:
    t = date(2019, 3, 10)
    expiry = date(2019, 4, 19)
    visible_by = datetime(2019, 3, 11, 0, 0, tzinfo=UTC)
    in_window = _action(
        record_id="ACT-D1",
        security_id="SYN-0001",
        kind="cash_dividend",
        effective_session=date(2019, 3, 15),
        available_at=visible_by - timedelta(days=1),
        cash_amount=D("0.50"),
    )
    after_expiry = _action(
        record_id="ACT-D2",
        security_id="SYN-0001",
        kind="cash_dividend",
        effective_session=date(2019, 5, 17),
        available_at=visible_by - timedelta(days=1),
        cash_amount=D("5.00"),
    )
    not_yet_visible = _action(
        record_id="ACT-D3",
        security_id="SYN-0001",
        kind="cash_dividend",
        effective_session=date(2019, 3, 16),
        available_at=visible_by + timedelta(days=1),
        cash_amount=D("9.00"),
    )
    got = pending_dividend_per_share(
        actions=(in_window, after_expiry, not_yet_visible),
        underlying_id="SYN-0001",
        visible_by=visible_by,
        effective_after=t,
        effective_through=expiry,
    )
    assert got == D("0.50")
    other = pending_dividend_per_share(
        actions=(after_expiry,),
        underlying_id="SYN-0001",
        visible_by=visible_by,
        effective_after=t,
        effective_through=expiry,
    )
    assert other is None


def test_pick_expiry_uses_calendar_days(surface, decision_session) -> None:
    """The DTE band is CALENDAR days (protocol 30-60), re-derived
    independently across MANY decision sessions: the picked expiry is the
    in-band calendar-day pick, never a session-count approximation (mutant
    M127 — 30-60 sessions is a different band than 30-60 calendar days)."""
    from tree_options.options.strategy import _pick_expiry

    calendar = surface.overlay.calendar
    checked = 0
    for offset in range(0, 90, 5):
        session = decision_session if offset == 0 else calendar.nth_after(decision_session, offset)
        decision_at = calendar.session_close(session)
        for sid in sorted(surface.eligible_as_of(session)):
            live = surface.live_expiries_as_of(sid, decision_at)
            in_band = [e for e in live if CONFIG.dte_min <= (e - session).days <= CONFIG.dte_max]
            if not in_band:
                continue
            expected = min(in_band, key=lambda e: (abs((e - session).days - CONFIG.target_dte), e))
            assert _pick_expiry(surface, sid, session, CONFIG) == expected, (sid, session)
            checked += 1
    assert checked >= 10, f"the fixture must exercise in-band expiries broadly (checked {checked})"


# ---- the non-monotone ladder (remediation-3, owner ruling 2026-09-02) -------------
#
# The real bars era surfaced a put ladder whose derived |delta| ordering
# INVERTS on one underlying (META/2025-12-19, live probe 2026-09-02) — a
# data property of noisy derived solves, not a machinery bug. The old fatal
# raise would abort a whole sealed run over ONE name's ladder (the
# consumed-authority-no-verdict class); the M165 discipline applies: a
# refusal is counted and disclosed, never fatal.


class _InvertedPutLadderSurface:
    """Delegates everything to the real surface except ONE underlying: its
    live expiry is a fixed in-band date and its PUT ladder probes return
    |delta|s that DECREASE as the strike rises (the put guard's trip)."""

    def __init__(self, inner, uid: str, expiry: date) -> None:
        self._inner = inner
        self._uid = uid
        self._expiry = expiry
        self._deltas = iter((D("0.55"), D("0.45"), D("0.35")))

    def strike_ladder(self, uid, expiration):
        if uid == self._uid and expiration == self._expiry:
            return (D("100"), D("110"), D("120"))
        return self._inner.strike_ladder(uid, expiration)

    def live_expiries_as_of(self, uid, at):
        if uid == self._uid:
            return (self._expiry,)
        return self._inner.live_expiries_as_of(uid, at)

    def entry_as_of(self, uid, at, contract_id):
        if uid == self._uid:
            delta = next(self._deltas, None)
            if delta is None:
                return None
            return SimpleNamespace(abs_delta=delta)
        return self._inner.entry_as_of(uid, at, contract_id)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _patched_bottom_name(surface, decision_session, scores):
    """The wrapper over the real surface, planted on the LOWEST-scored name
    (the bottom quintile -> the PUT side of the cut)."""
    target = min(scores, key=lambda s: s.score).security_id
    return target, _InvertedPutLadderSurface(surface, target, decision_session + timedelta(days=45))


def test_pick_strike_raises_the_typed_error_on_an_inverted_put_ladder(
    surface, decision_session, scores
) -> None:
    from tree_options.options.strategy import NonMonotoneLadderError, _pick_strike

    _target, patched = _patched_bottom_name(surface, decision_session, scores)
    with pytest.raises(NonMonotoneLadderError, match="non-monotone put"):
        _pick_strike(
            patched,
            min(scores, key=lambda s: s.score).security_id,
            decision_session,
            decision_session + timedelta(days=45),
            "P",
            CONFIG,
        )


def test_build_candidates_counts_and_skips_a_non_monotone_ladder(
    surface, candidate_filter, decision_session, scores
) -> None:
    """THE EVENT-4 LANDMINE: one inverted ladder must cost exactly that one
    name (counted in the audit, visible in the payload counters), never the
    run. The other selected names proceed to acceptance as before."""
    from tree_options.options.strategy import CandidateAudit

    _target, patched = _patched_bottom_name(surface, decision_session, scores)
    audit = CandidateAudit()
    candidates = build_candidates(
        surface=patched,
        candidate_filter=candidate_filter,
        decision_session=decision_session,
        scores=scores,
        config=CONFIG,
        audit=audit,
    )
    assert audit.selected >= 4, "the fixture must exercise the bottom quintile"
    assert audit.non_monotone_ladder == 1, audit
    assert len(candidates) >= 1, "the OTHER names still produce candidates"
    assert all(c.underlying_security_id != _target for c in candidates)
