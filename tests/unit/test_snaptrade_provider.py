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
    return PaperAccountBinding(
        alias="alpaca-paper-canary",
        account_id="account-1",
        brokerage_slug="ALPACA-PAPER",
        paper_confirmed_by="operator",
        credential_sha256="a" * 64,
    )


class FakeSDK:
    def __init__(self):
        self.calls = []
        self.account_information = self
        self.trading = self

    def __getattr__(self, name):
        def call(**kwargs):
            self.calls.append((name, kwargs))
            body = (
                {
                    "id": "account-1",
                    "is_paper": True,
                    "institution_name": "Alpaca",
                    "sync_status": {
                        "holdings": {
                            "initial_sync_completed": True,
                            "last_successful_sync": NOW.isoformat(),
                        }
                    },
                }
                if name == "get_user_account_details"
                else []
            )
            return SimpleNamespace(
                body=body,
                headers={"X-Request-ID": f"req-{name}"},
                response=SimpleNamespace(data=None),
            )

        return call


def test_readonly_snapshot_calls_generated_methods_and_retains_provenance():
    sdk = FakeSDK()
    provider = SnapTradeProvider(
        sdk, binding(), user_id="user", user_secret="SECRET", clock=lambda: NOW
    )
    state = provider.account_snapshot()
    assert state.binding.alias == "alpaca-paper-canary"
    assert state.holdings_at == NOW
    assert state.orders.request_id == "req-get_user_account_orders"
    assert [n for n, _ in sdk.calls] == [
        "get_user_account_details",
        "get_user_account_balance",
        "get_all_account_positions",
        "get_user_account_orders",
    ]
    assert all("timeout" not in k and k["account_id"] == "account-1" for _, k in sdk.calls)
    assert "SECRET" not in repr(provider)


def test_account_identity_and_paper_binding_fail_closed():
    sdk = FakeSDK()
    sdk.get_user_account_details = lambda **kwargs: SimpleNamespace(
        body={"id": "WRONG", "brokerage": {"slug": "ALPACA-PAPER"}},
        headers={},
        response=SimpleNamespace(data=None),
    )
    with pytest.raises(ProviderUnavailable, match="account"):
        SnapTradeProvider(
            sdk, binding(), user_id="user", user_secret="SECRET", clock=lambda: NOW
        ).account_snapshot()


def test_real_sdk_retries_disabled_and_methods_present():
    from tree_options.execution.snaptrade_provider import build_sdk

    sdk = build_sdk(client_id="fixture", consumer_key="fixture")
    assert sdk.trading.api_client.configuration.retries == 0
    assert sdk.trading.api_client.rest_client.pool_manager.connection_pool_kw["retries"].total == 0
    for namespace, method in [
        ("account_information", "get_user_account_details"),
        ("account_information", "get_user_account_orders"),
        ("account_information", "get_user_account_order_detail"),
        ("account_information", "get_all_account_positions"),
        ("trading", "get_user_account_quotes"),
        ("trading", "place_force_order"),
        ("trading", "cancel_order"),
        ("trading", "replace_order"),
    ]:
        assert callable(getattr(getattr(sdk, namespace), method))


@pytest.mark.parametrize("paper", [False, None])
def test_provider_paper_fact_required_even_with_operator_paper_label(paper):
    sdk = FakeSDK()
    sdk.get_user_account_details = lambda **kwargs: SimpleNamespace(
        body={"id": "account-1", "is_paper": paper, "institution_name": "Alpaca"},
        headers={},
        response=SimpleNamespace(data=None),
    )
    with pytest.raises(ProviderUnavailable, match="positively"):
        SnapTradeProvider(sdk, binding(), user_id="user", user_secret="SECRET").account_snapshot()


def test_production_slug_rejected():
    with pytest.raises(ValueError):
        PaperAccountBinding.model_validate({**binding().model_dump(), "brokerage_slug": "ALPACA"})


def test_real_generated_calls_reach_transport_with_deadline_and_no_retry(monkeypatch):
    from decimal import Decimal

    from tree_options.execution.snaptrade_provider import build_sdk

    sdk = build_sdk(client_id="fixture", consumer_key="fixture")
    seen = []

    # The SDK's actual generated signatures and serialization execute. Only
    # the urllib3 transport is replaced; zero external requests are possible.
    def request(method, url, **kwargs):
        seen.append((method, url.split("?")[0], kwargs))
        raise TimeoutError

    monkeypatch.setattr(sdk.trading.api_client.rest_client.pool_manager, "request", request)
    provider = SnapTradeProvider(
        sdk,
        binding().model_copy(update={"account_id": "00000000-0000-4000-8000-000000000001"}),
        user_id="user",
        user_secret="SECRET",
    )
    calls = [
        lambda: provider.account_snapshot(),
        lambda: provider.order_readback("b1"),
        lambda: provider._call(
            "account_information", "get_user_account_orders", state="all", days=7
        ),
        lambda: provider.quotes("AAPL"),
        lambda: provider.activities(),
        lambda: provider._submit(
            symbol="AAPL",
            side="BUY",
            quantity=1,
            limit=Decimal("50"),
            client_order_id="00000000-0000-4000-8000-000000000002",
        ),
        lambda: provider._cancel("b1"),
        lambda: provider._replace("b1", symbol="AAPL", side="BUY", quantity=1, limit=Decimal("50")),
    ]
    for call in calls:
        before = len(seen)
        with pytest.raises(TimeoutError):
            call()
        assert len(seen) == before + 1
        assert seen[-1][2]["timeout"].total == 10


def test_real_sdk_successful_readonly_response_parsing(monkeypatch):
    import json

    import urllib3

    from tree_options.execution.snaptrade_provider import build_sdk

    sdk = build_sdk(client_id="fixture", consumer_key="fixture")
    account_id = "00000000-0000-4000-8000-000000000001"

    def request(method, url, **kwargs):
        path = url.split("?")[0]
        body = (
            {
                "id": account_id,
                "is_paper": True,
                "institution_name": "Alpaca",
                "sync_status": {
                    "holdings": {
                        "initial_sync_completed": True,
                        "last_successful_sync": NOW.isoformat(),
                    }
                },
            }
            if path.endswith(account_id)
            else []
        )
        return urllib3.HTTPResponse(
            body=json.dumps(body).encode(),
            status=200,
            headers={"Content-Type": "application/json", "X-Request-ID": "raw-response-id"},
        )

    monkeypatch.setattr(sdk.trading.api_client.rest_client.pool_manager, "request", request)
    provider = SnapTradeProvider(
        sdk,
        binding().model_copy(update={"account_id": account_id}),
        user_id="user",
        user_secret="SECRET",
        clock=lambda: NOW,
    )
    result = provider.account_snapshot()
    assert result.details.request_id == "raw-response-id"
    assert result.details.body["is_paper"] is True
