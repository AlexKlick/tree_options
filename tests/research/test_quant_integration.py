from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from tree_options.research.quant import (
    FrozenUniverse,
    QuantSnapshot,
    register_version,
    run_strategy,
)
from tree_options.research.runstate.store import open_runstate_store
from tree_options.strategy_lab.contracts import Observation

NOW = datetime(2026, 9, 29, 20, tzinfo=UTC)


def snapshot(members=('A', 'B'), as_of=date(2026, 9, 29)):
    observations = []
    for symbol in members:
        for year, month, price in ((2025, 9, '100'), (2026, 8, '110'), (2026, 9, '9999')):
            at = datetime(year, month, 1, tzinfo=UTC)
            observations.append(Observation(symbol, at, at, {'close': Decimal(price)}, 'fixture', f'{symbol}-{month}-{year}'))
    return QuantSnapshot(FrozenUniverse(as_of, members, 'historical-fixture', 'a' * 64), NOW, tuple(observations))


def test_manifest_refuses_today_universe_and_future_observation():
    with pytest.raises(ValueError, match='universe'):
        snapshot(as_of=date(2026, 9, 30))
    s = snapshot()
    future = Observation('A', datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 10, 1, tzinfo=UTC), {'close': Decimal('1')}, 'fixture', 'future')
    with pytest.raises(ValueError, match='cutoff'):
        QuantSnapshot(s.universe, NOW, (*s.observations, future))


def test_momentum_skip_no_signal_and_replay_identity(tmp_path):
    with open_runstate_store(tmp_path) as store:
        version = register_version(store, 'momentum_12_1', code_sha='b' * 40, lock_sha='c' * 64, parameters={'top_n': 1})
        result = run_strategy(store, version, snapshot())
        assert result['scores'][0]['components']['return_12_1'] == '0.1'
        assert result['targets'] == [{'entity_id': 'A', 'weight': '1'}]
        assert run_strategy(store, version, snapshot()) == result
        assert store.verify()['ok']
        empty = QuantSnapshot(FrozenUniverse(NOW.date(), (), 'fixture', 'a' * 64), NOW, ())
        assert run_strategy(store, version, empty)['disposition'] == 'NO_SIGNAL'
        assert result['evidence']['exact_versions']['strategy'] == version['version_id']
        assert store.all('quant_snapshot') and store.all('quant_version')


def test_order_invariance_and_gated_value(tmp_path):
    with open_runstate_store(tmp_path) as store:
        version = register_version(store, 'equal_weight_us_equities', code_sha='b' * 40, lock_sha='c' * 64)
        a = run_strategy(store, version, snapshot())
        s = snapshot()
        reverse = QuantSnapshot(FrozenUniverse(s.universe.as_of, ('B', 'A'), 'historical-fixture', 'a' * 64), NOW, tuple(reversed(s.observations)))
        assert run_strategy(store, version, reverse) == a
        assert sum(Decimal(row['weight']) for row in a['targets']) == 1
        value = register_version(store, 'robust_value_5metric', code_sha='b' * 40, lock_sha='c' * 64)
        gated = run_strategy(store, value, s)
        assert gated['disposition'] == 'DATA-GATED-NOT-RUN'
        assert gated['targets'] == []


def test_snapshot_detaches_mutable_inputs(tmp_path):
    values = {'close': Decimal('10')}
    at = datetime(2026, 9, 1, tzinfo=UTC)
    obs = Observation('A', at, at, values, 'fixture', 'a')
    s = QuantSnapshot(FrozenUniverse(NOW.date(), ('A',), 'fixture', 'a' * 64), NOW, (obs,))
    before = s.identity
    values['close'] = Decimal('1000')
    assert s.identity == before
    assert s.observations[0].values['close'] == Decimal('10')


def test_execution_attribution_requires_real_evidence():
    from tree_options.execution.lifecycle import ExecutionLifecycle
    from tree_options.execution.records import OrderIntent
    from tree_options.research.quant import execution_for_funded_replay
    order = OrderIntent(intent_id='q', contract_id='A', side='BUY', position_effect='OPEN_LONG', quantity=1, order_type='MARKET', intent_created_at=NOW, source='fixture', source_sequence_id='q')
    with pytest.raises(ValueError, match='evidence'):
        execution_for_funded_replay(ExecutionLifecycle.start(order))


def test_cluster_lane_uses_frozen_features_and_exploratory_identity(tmp_path):
    with open_runstate_store(tmp_path) as store:
        members = ('A', 'B', 'C', 'D')
        obs = tuple(Observation(k, NOW, NOW, {'rsi': Decimal(str(v)), 'momentum': Decimal('0'), 'factor_beta': Decimal('0')}, 'fixture', k) for k, v in zip(members, (30, 45, 55, 70), strict=True))
        s = QuantSnapshot(FrozenUniverse(NOW.date(), members, 'fixture', 'a' * 64), NOW, obs)
        version = register_version(store, 'cluster_rsi_factor', code_sha='b' * 40, lock_sha='c' * 64)
        result = run_strategy(store, version, s)
        assert result['disposition'] == 'SCORED'
        assert result['targets'] == [{'entity_id': 'D', 'weight': '1'}]
        assert result['evidence']['diagnostics']['registration'] == 'exploratory'


def test_partial_fill_cannot_bypass_attribution_evidence_gate():
    from tree_options.execution.lifecycle import ExecutionLifecycle
    from tree_options.execution.records import OrderIntent, PartialFill, SubmitAttempt
    from tree_options.research.quant import execution_for_funded_replay
    order = OrderIntent(intent_id='q', contract_id='A', side='BUY', position_effect='OPEN_LONG', quantity=2, order_type='MARKET', intent_created_at=NOW, source='fixture', source_sequence_id='q')
    lifecycle = ExecutionLifecycle.start(order).apply(SubmitAttempt(record_id='s', intent_id='q', send_attempt_at=NOW, source='fixture', source_sequence_id='s'))
    lifecycle = lifecycle.apply(PartialFill(record_id='f', intent_id='q', broker_order_id='b', fill_quantity=1, cumulative_quantity=1, unit_price=Decimal('50'), fees=Decimal('0'), exchange_event_at=NOW, locally_received_at=NOW, source='fixture', source_sequence_id='f', broker_sequence_id='f'))
    with pytest.raises(ValueError, match='evidence'):
        execution_for_funded_replay(lifecycle)


def test_campaign_comparison_freezes_control_and_never_authorizes_effect(tmp_path):
    from tree_options.research.quant import campaign_proposal
    with open_runstate_store(tmp_path) as store:
        control = register_version(store, 'equal_weight_us_equities', code_sha='b' * 40, lock_sha='c' * 64)
        candidate = register_version(store, 'momentum_12_1', code_sha='b' * 40, lock_sha='c' * 64)
        a, b = (run_strategy(store, v, snapshot()) for v in (candidate, control))
        campaign = campaign_proposal(store, a['run_id'], b['run_id'])
        assert campaign['execution_authorized'] is False
        assert campaign['live_money'] is False
        assert campaign['common_snapshot'] == a['snapshot_id'] == b['snapshot_id']
        assert campaign['max_orders'] == 1
        assert store.all('comparison_row') and store.all('quant_provenance')
