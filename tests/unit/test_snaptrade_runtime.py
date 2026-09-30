from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from tree_options.execution.records import OrderIntent
from tree_options.execution.snaptrade_provider import (
    EquityPaperEffect,
    PaperAccountBinding,
    ProviderObservation,
)
from tree_options.time.sessions import shift_instant
from tree_options.trex.account_ownership import AccountOwnership
from tree_options.trex.snaptrade_runtime import CanaryQuote, SnapTradePaperRuntime
from tree_options.trex.supervised import (
    SupervisedPaths,
    SupervisedRefused,
    grant_mandate,
    project_intent,
)

NOW = datetime(2026, 9, 29, 20, tzinfo=UTC)


class FakeProvider:
    def __init__(self, timeout=False):
        self.binding = PaperAccountBinding(alias='alpaca-paper-canary', account_id='account-1', brokerage_slug='ALPACA-PAPER', paper_confirmed_by='operator', credential_sha256='a' * 64)
        self.timeout = timeout
        self.calls = 0
        self.clock = lambda: NOW
        self.rows = []

    def account_snapshot(self):
        obs = ProviderObservation([], NOW, 'req', 'a' * 64)
        return SimpleNamespace(binding=self.binding, fresh_at=lambda now: True, details=obs, balances=ProviderObservation([{'currency': {'code': 'USD'}, 'cash': '1000'}], NOW, 'b', 'b' * 64), positions=obs, orders=ProviderObservation(self.rows, NOW, 'o', 'c' * 64), holdings_at=NOW)

    def _submit(self, **kwargs):
        self.calls += 1
        row = {'status': 'ACCEPTED', 'brokerage_order_id': 'b1', 'client_order_id': kwargs['client_order_id'], 'total_quantity': kwargs['quantity'], 'filled_quantity': 0, 'time_updated': NOW.isoformat(), 'time_placed': NOW.isoformat()}
        self.rows.append(row)
        if self.timeout:
            raise TimeoutError
        return ProviderObservation(row, NOW, 'submit-1', 'd' * 64)

    def order_readback(self, order_id):
        return ProviderObservation(self.rows[0], NOW, 'lookup-1', 'e' * 64)


def setup(tmp_path, provider):
    paths = SupervisedPaths(tmp_path / 'state')
    runtime = SnapTradePaperRuntime(provider, paths, ownership_root=tmp_path / 'owners', clock=lambda: NOW,
        quote_source=lambda symbol: CanaryQuote(symbol, Decimal('49'), Decimal('50'), NOW, 'quote-fixture'))
    runtime.start()
    grant_mandate(paths, now=NOW, account_id=provider.binding.alias, owner_epoch=runtime.owner.epoch,
        strategy_version='operational-canary/1', profile_digest='a' * 64, max_orders=1, ttl_seconds=600,
        granted_by='fixture-operator', environment='broker_paper', max_gross_notional_usd=Decimal('100'))
    effect = EquityPaperEffect(intent_id='intent-canary', account_alias=provider.binding.alias, owner_epoch=runtime.owner.epoch,
        strategy_version='operational-canary/1', symbol='AAPL', quantity=1, limit=Decimal('50'))
    order = OrderIntent(intent_id=effect.intent_id, contract_id=effect.symbol, side='BUY', position_effect='OPEN_LONG', quantity=1,
        order_type='LIMIT', limit_price=effect.limit, intent_created_at=NOW, source=effect.strategy_version, source_sequence_id='canary-1')
    return runtime, paths, effect, order


def test_canary_disabled_then_bounded_single_use_and_halt(tmp_path):
    provider = FakeProvider()
    runtime, paths, effect, order = setup(tmp_path, provider)
    with pytest.raises(SupervisedRefused, match='disabled'):
        runtime.canary(order, effect, operator_approved=False)
    assert provider.calls == 0
    result = runtime.canary(order, effect, operator_approved=True)
    assert result['outcome'] == 'acknowledged'
    assert provider.calls == 1
    with pytest.raises(SupervisedRefused):
        runtime.canary(order, effect, operator_approved=True)
    runtime.halt()
    assert paths.mandate_revoked().exists()
    runtime.close()


def test_timeout_no_retry_restart_readback_fences_effect(tmp_path):
    provider = FakeProvider(timeout=True)
    runtime, paths, effect, order = setup(tmp_path, provider)
    result = runtime.canary(order, effect, operator_approved=True)
    assert result['outcome'] == 'uncertain' and provider.calls == 1
    assert any(r.record_type == 'TIMEOUT_OBSERVED' for r in project_intent(paths, order.intent_id).records)
    runtime.close()
    restarted = SnapTradePaperRuntime(provider, paths, ownership_root=tmp_path / 'owners', clock=lambda: shift_instant(NOW, 121))
    restarted.start()
    assert provider.calls == 1
    assert not restarted.ready
    assert restarted.projection()['executions'][0]['exact_economics'] is False
    restarted.close()


def test_account_alias_cannot_have_two_owners(tmp_path):
    first = AccountOwnership(tmp_path, 'same-account')
    first.acquire()
    second = AccountOwnership(tmp_path, 'same-account')
    with pytest.raises(SupervisedRefused, match='owned'):
        second.acquire()
    first.close()
    second.acquire()
    assert first.epoch != second.epoch
    second.close()


def test_canary_exposure_quote_and_binding_refusals(tmp_path):
    provider = FakeProvider()
    runtime, _paths, effect, order = setup(tmp_path, provider)
    with pytest.raises(SupervisedRefused, match='notional'):
        runtime.canary(order, effect.model_copy(update={'limit': Decimal('101')}), operator_approved=True)
    runtime.quote_source = lambda symbol: CanaryQuote(symbol, Decimal('49'), Decimal('50'), shift_instant(NOW, -100), 'stale')
    with pytest.raises(SupervisedRefused, match='quote'):
        runtime.canary(order, effect, operator_approved=True)
    assert provider.calls == 0
    runtime.close()


def test_order_id_cannot_be_reused_across_economic_intents(tmp_path):
    provider = FakeProvider()
    runtime, _paths, _effect, order = setup(tmp_path, provider)
    runtime.bind_broker_order('broker-one', order.intent_id)
    runtime.bind_broker_order('broker-one', order.intent_id)
    with pytest.raises(SupervisedRefused, match='broker_order_identity'):
        runtime.bind_broker_order('broker-one', 'other-intent')
    runtime.close()


def test_missing_fees_cannot_be_exact_zero():
    from pydantic import ValidationError

    from tree_options.execution.records import CompleteFill
    with pytest.raises(ValidationError):
        CompleteFill(record_id='f', intent_id='i', broker_order_id='b', fill_quantity=1, cumulative_quantity=1,
            unit_price=Decimal('50'), exchange_event_at=NOW, locally_received_at=NOW, source='fixture',
            source_sequence_id='f', broker_sequence_id='f')
