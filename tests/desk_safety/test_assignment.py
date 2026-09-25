"""Pure engine ex-dividend protection. Broker observation wiring is NOT implied."""
from datetime import date, datetime, time
from decimal import Decimal

import pytest

from tree_options.trex import engine
from tree_options.trex.clock import ET
from tree_options.trex.plan import LegStructure
from tree_options.trex.state import Status, StructureState


@pytest.fixture
def scenario(world, monkeypatch):
    monkeypatch.setattr(engine, 'session_calendar', lambda: world.cal)
    spec = LegStructure.model_validate({**world.queue['admissible'][0]['structure'],
                                       'exits': {'touch': False, 'breach': False}})
    trigger = world.cal.nth_after(world.entry, 1)
    ex_date = world.cal.nth_after(trigger, 1)
    st = StructureState()
    st.to(Status.ENTER_WORKING, world.now)
    st.to(Status.OPEN, world.now)
    st.filled_qty, st.entry_fill = spec.quantity, Decimal('2.20')
    return spec, st, trigger, ex_date


def decision(scenario, *, amount='1.00', mid='1.50', spot='106', day=None,
             quote=None, spec=None, st=None, ex_date=None, prev=None):
    base_spec, base_st, trigger, base_ex = scenario
    spec = spec or base_spec
    st = st or base_st
    leg = next(g for g in spec.legs if g.action == 'SELL' and g.right == 'C')
    key = engine.short_call_key(spec.id, leg.strike, leg.expiry)
    snap = engine.Snapshot(ts=datetime.combine(day or trigger, time(10), ET),
        spots={} if spot is None else {spec.underlying: Decimal(spot)},
        quotes={spec.id: quote or engine.ComboQuote(Decimal('1.80'), Decimal('2.00'))},
        dividends={spec.underlying: engine.DividendCalendar(
            ex_date or base_ex, prev or trigger, Decimal(amount))},
        short_call_mids={} if mid is None else {key: Decimal(mid)})
    return engine.decide_structure(spec, st, snap).action


def test_itm_short_with_insufficient_time_value_closes_at_marketable_side(scenario):
    result = decision(scenario)  # 1.50 - (106 - 105) = .50 < $1 dividend
    assert result.reason is engine.ExitReason.ASSIGNMENT_RISK
    assert result.side == 'SELL' and result.limit == Decimal('1.80')
    assert result.qty == scenario[0].quantity


@pytest.mark.parametrize('mid', [None, '-1', 'NaN'])
def test_missing_or_invalid_short_quote_closes_known_itm_risk(scenario, mid):
    assert decision(scenario, mid=mid).reason is engine.ExitReason.ASSIGNMENT_RISK


@pytest.mark.parametrize('kwargs', [
    {'mid': '2.01'}, {'mid': '2.00'}, {'spot': '104'}, {'spot': None},
])
def test_no_trigger_without_itm_and_extrinsic_inequality(scenario, kwargs):
    assert isinstance(decision(scenario, **kwargs), engine.NoAction)


def test_only_pre_ex_session_triggers(scenario, world):
    assert isinstance(decision(scenario, day=world.entry), engine.NoAction)


def test_key_is_numeric_not_decimal_display_dependent(scenario):
    spec = scenario[0]
    assert engine.short_call_key(spec.id, Decimal('105'), spec.first_expiry) == \
           engine.short_call_key(spec.id, Decimal('105.00'), spec.first_expiry)


def test_missing_runtime_dividend_feed_does_not_claim_protection(scenario):
    spec, st, day, _ = scenario
    snap = engine.Snapshot(datetime.combine(day, time(10), ET), {}, {})
    assert isinstance(engine.decide_structure(spec, st, snap).action, engine.NoAction)
    # Admission reports runtime_exit_observations_not_wired until E5 connects it.


# Ordering pins ported from the feat/desk-assignment-exit lane (a5ca17d):
# the rule's position in the exit ladder is a contract, not an accident.


def test_short_call_key_wire_format_is_pinned_once():
    # One literal pin: a future E5 runtime feeding Snapshot.short_call_mids
    # must produce exactly this key shape (every other test derives keys).
    assert engine.short_call_key('dg', Decimal('105'), date(2026, 10, 16)) \
        == 'dg|C105|2026-10-16'


@pytest.mark.parametrize('amount', ['0', '-1'])
def test_non_positive_dividend_stands_down(scenario, amount):
    assert isinstance(decision(scenario, amount=amount), engine.NoAction)


def test_expiry_safety_preempts_assignment_risk(scenario):
    spec, _, _, _ = scenario
    last = engine.last_hold_session(spec.first_expiry)
    act = decision(scenario, day=last, ex_date=spec.first_expiry, prev=last)
    assert act.reason is engine.ExitReason.EXPIRY_SAFETY


def test_a_working_exit_keeps_its_reason_over_assignment_risk(scenario, world):
    _, st, _, _ = scenario
    st.to(Status.EXIT_WORKING, world.now)
    st.exit_reason = 'time_stop'
    assert decision(scenario).reason is engine.ExitReason.TIME_STOP


def test_breach_preempts_assignment_risk_on_a_breach_kind(scenario):
    spec, _, _, _ = scenario
    armed = spec.model_copy(update={'exits': {'touch': False, 'breach': True}})
    # spot 106 >= the short 105 call: breach fires at the same ITM boundary
    assert decision(scenario, spec=armed).reason is engine.ExitReason.BREACH


def test_assignment_risk_fires_before_take_profit(scenario):
    spec, _, _, _ = scenario
    tp = spec.model_copy(update={'exits': {
        'touch': False, 'breach': False, 'take_profit': {'gain_frac': '0.10'}}})
    # mid 2.55 >= entry 2.20 x 1.10 fires take-profit; assignment must win
    quote = engine.ComboQuote(Decimal('2.50'), Decimal('2.60'))
    assert decision(scenario, spec=tp, quote=quote).reason \
        is engine.ExitReason.ASSIGNMENT_RISK
