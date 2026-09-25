"""Pure engine ex-dividend protection. Broker observation wiring is NOT implied."""
from datetime import datetime, time
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


def decision(scenario, *, amount='1.00', mid='1.50', spot='106', day=None):
    spec, st, trigger, ex_date = scenario
    leg = next(g for g in spec.legs if g.action == 'SELL' and g.right == 'C')
    key = engine.short_call_key(spec.id, leg.strike, leg.expiry)
    snap = engine.Snapshot(ts=datetime.combine(day or trigger, time(10), ET),
        spots={} if spot is None else {spec.underlying: Decimal(spot)},
        quotes={spec.id: engine.ComboQuote(Decimal('1.80'), Decimal('2.00'))},
        dividends={spec.underlying: engine.DividendCalendar(ex_date, trigger, Decimal(amount))},
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
