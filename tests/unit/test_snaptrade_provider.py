from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from tree_options.execution.snaptrade_provider import (
    PaperAccountBinding,
    ProviderUnavailable,
    SnapTradeProvider,
)

NOW = datetime(2026, 9, 29, 20, tzinfo=UTC)


def binding():
    return PaperAccountBinding(alias='alpaca-paper-canary', account_id='account-1', brokerage_slug='ALPACA-PAPER', paper_confirmed_by='operator', credential_sha256='a' * 64)


class FakeSDK:
    def __init__(self):
        self.calls = []
        self.account_information = self
        self.trading = self

    def __getattr__(self, name):
        def call(**kwargs):
            self.calls.append((name, kwargs))
            body = {'id': 'account-1', 'brokerage': {'slug': 'ALPACA-PAPER'}, 'sync_status': {'holdings': {'initial_sync_completed': True, 'last_successful_sync': NOW.isoformat()}}} if name == 'get_user_account_details' else []
            return SimpleNamespace(body=body, headers={'X-Request-ID': f'req-{name}'}, response=SimpleNamespace(data=None))
        return call


def test_readonly_snapshot_calls_generated_methods_and_retains_provenance():
    sdk = FakeSDK()
    provider = SnapTradeProvider(sdk, binding(), user_id='user', user_secret='SECRET', clock=lambda: NOW)
    state = provider.account_snapshot()
    assert state.binding.alias == 'alpaca-paper-canary'
    assert state.holdings_at == NOW
    assert state.orders.request_id == 'req-get_user_account_orders'
    assert [n for n, _ in sdk.calls] == ['get_user_account_details', 'get_user_account_balance', 'get_all_account_positions', 'get_user_account_orders']
    assert all(k['timeout'] == 10 and k['account_id'] == 'account-1' for _, k in sdk.calls)
    assert 'SECRET' not in repr(provider)


def test_account_identity_and_paper_binding_fail_closed():
    sdk = FakeSDK()
    sdk.get_user_account_details = lambda **kwargs: SimpleNamespace(body={'id': 'WRONG', 'brokerage': {'slug': 'ALPACA-PAPER'}}, headers={}, response=SimpleNamespace(data=None))
    with pytest.raises(ProviderUnavailable, match='account'):
        SnapTradeProvider(sdk, binding(), user_id='user', user_secret='SECRET', clock=lambda: NOW).account_snapshot()


def test_real_sdk_retries_disabled_and_methods_present():
    from tree_options.execution.snaptrade_provider import build_sdk
    sdk = build_sdk(client_id='fixture', consumer_key='fixture')
    assert sdk.trading.api_client.configuration.retries == 0
    assert sdk.trading.api_client.rest_client.pool_manager.connection_pool_kw['retries'].total == 0
    for namespace, method in [('account_information', 'get_user_account_details'), ('account_information', 'get_user_account_orders'), ('account_information', 'get_user_account_order_detail'), ('account_information', 'get_all_account_positions'), ('trading', 'get_user_account_quotes'), ('trading', 'place_force_order'), ('trading', 'cancel_order'), ('trading', 'replace_order')]:
        assert callable(getattr(getattr(sdk, namespace), method))
