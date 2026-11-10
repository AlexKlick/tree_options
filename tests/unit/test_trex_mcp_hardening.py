"""Red-first safety, provenance and compute-boundary tests for customer MCP."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from tree_options.trex_mcp import server
from tree_options.trex_mcp.server import AccessDenied, Config, Principal, UpstreamUnavailable


def config() -> Config:
    return Config(
        public_url="https://mcp.example/mcp",
        authorization_server="https://auth.example",
        introspection_url="https://auth.example/introspect",
        client_id="read-only", client_secret="secret",
        tenants={"tenant-1": "http://127.0.0.1:8090"},
        allowed_origins=frozenset(),
    )


def payload(name: str = "trex_market_snapshot") -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": name, "arguments": {"symbol": "TSLA"},
    }}


def make_client(*, fetcher=None, auth=None) -> TestClient:
    async def default_auth(_: str) -> Principal:
        return Principal("tenant-1")

    async def default_read(tenant: str, path: str, params: dict[str, Any]) -> dict[str, Any]:
        return {"now": "2026-11-10T10:00:00-05:00", "symbol": "TSLA",
                "quote": {"last": 378.05}, "quote_age_seconds": 3600}

    return TestClient(server.create_app(
        config=config(), authenticate=auth or default_auth, fetcher=fetcher or default_read,
    ))


def test_expired_token_rejected_even_if_active_claim_true() -> None:
    claims = {"active": True, "sub": "customer-1", "aud": "https://mcp.example/mcp",
              "scope": "trex:research:read", "trex_tenant_id": "tenant-1", "exp": 1}
    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=claims))) as client:
            with pytest.raises(AccessDenied):
                await server._introspect("token", config(), client)
    asyncio.run(run())


def test_oauth_introspection_reuses_supplied_client_and_limits_reply() -> None:
    seen: list[str] = []
    claims = {"active": True, "sub": "customer-1", "aud": ["https://mcp.example/mcp"],
              "scope": "trex:research:read", "trex_tenant_id": "tenant-1"}
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.method)
        if len(seen) == 2:
            return httpx.Response(200, content=b'{' + b' ' * (150_000) + b'}')
        return httpx.Response(200, json=claims)
    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            assert (await server._introspect("token", config(), client)).tenant_id == "tenant-1"
            with pytest.raises(UpstreamUnavailable):
                await server._introspect("token", config(), client)
    asyncio.run(run())
    assert seen == ["POST", "POST"]


def test_trex_read_rejects_oversized_response_and_non200() -> None:
    async def run(body: bytes, status: int) -> dict[str, Any]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda _: httpx.Response(status, content=body))) as client:
            return await server._read_trex(config(), "tenant-1", "/api/market/TSLA", {}, client)
    with pytest.raises(UpstreamUnavailable):
        asyncio.run(run(b'{' + b' ' * 2_000_000 + b'}', 200))
    with pytest.raises(UpstreamUnavailable):
        asyncio.run(run(b'{}', 302))
    assert asyncio.run(run(b'{"quote":null}', 200)) == {"quote": None}


def test_app_owns_one_shared_http_client_during_lifespan() -> None:
    app = server.create_app(config=config(),
        authenticate=(lambda token: _auth()), fetcher=(lambda tenant, path, params: _fetch()))
    with TestClient(app) as client:
        shared = app.state.research_http_client
        assert isinstance(shared, httpx.AsyncClient)
        assert not shared.is_closed
        assert client.get('/healthz').status_code == 200
        assert app.state.research_http_client is shared
    assert shared.is_closed


async def _auth() -> Principal:
    return Principal("tenant-1")


async def _fetch() -> dict[str, Any]:
    return {"symbol": "TSLA", "quote": None}


def test_chunked_body_early_abort_avoids_reading_all_chunks() -> None:
    class Chunked:
        def __init__(self) -> None:
            self.consumed = 0
        async def stream(self):
            for _ in range(100):
                self.consumed += 1
                yield b'x' * 1024
    request = Chunked()
    async def run() -> None:
        with pytest.raises(server.RequestTooLarge):
            await server._read_bounded_body(request, limit=4096)
    asyncio.run(run())
    assert request.consumed == 5


def test_malformed_nonstandard_json_constant_rejected_as_parse_error() -> None:
    client = make_client()
    response = client.post('/mcp', content=b'{"jsonrpc":"2.0","id":NaN,"method":"tools/list"}',
                           headers={"Authorization": "Bearer token", "Content-Type": "application/json"})
    assert response.json()['error']['code'] == -32700


def test_oversize_bearer_token_is_rejected_before_introspection() -> None:
    seen: list[str] = []
    async def auth(token: str) -> Principal:
        seen.append(token)
        return Principal("tenant-1")
    client = make_client(auth=auth)
    response = client.post('/mcp', json=payload(), headers={"Authorization": "Bearer " + 'a' * 5000})
    assert response.status_code == 401
    assert seen == []


def test_cross_symbol_upstream_mislabel_is_refused() -> None:
    async def bogus(tenant: str, path: str, params: dict[str, Any]) -> dict[str, Any]:
        return {"symbol": "AAPL", "quote": {"last": 40}, "quote_age_seconds": 1}
    c = make_client(fetcher=bogus)
    r = c.post('/mcp', json=payload(), headers={"Authorization": "Bearer token"})
    assert r.json()['result']['isError'] is True
    assert 'unavailable' in r.json()['result']['content'][0]['text'].lower()


def test_quote_finite_and_whitelisted_without_nan_serialization_fail() -> None:
    async def poisoned(tenant: str, path: str, params: dict[str, Any]) -> dict[str, Any]:
        return {"now": "2026-11-10T10:00:00-05:00", "symbol": "TSLA",
                "quote_age_seconds": float('inf'),
                "quote": {"last": float('nan'), "bid": -1, "ask": 378.4,
                          "source_as_of": "2026-11-10T09:00:00-05:00", "secret": "leak"}}
    c = make_client(fetcher=poisoned)
    r = c.post('/mcp', json=payload(), headers={"Authorization": "Bearer token"})
    assert r.status_code == 200
    doc = r.json()['result']['structuredContent']
    assert doc['quote']['last'] is None
    assert doc['quote']['bid'] is None
    assert doc['quote']['ask'] == 378.4
    assert doc['quote_age_seconds'] is None
    assert 'secret' not in doc['quote']


def test_options_sanitizes_poisoned_numeric_rows_and_bounds_output() -> None:
    async def options(tenant: str, path: str, params: dict[str, Any]) -> dict[str, Any]:
        return {"now": "2026-11-10T10:00:00-05:00", "symbol": "TSLA", "available": True,
                "warnings": ["WARNING" * 500] * 100,
                "recorded": {"session": "2026-11-10", "age_seconds": -99,
                             "cards": {"iv": float('nan'), "earnings": {"secret": "NO"}},
                             "slice": [{"exp": "2027-09-17", "right": "P", "strike": 300,
                                        "bid": float('nan'), "ask": 26.75, "volume": -1,
                                        "open_interest": 1, "secret": "NO"}] * 90},
                "live": None}
    c = make_client(fetcher=options)
    args = {"symbol": "TSLA", "expiration": "2027-09-17", "right": "put"}
    r = c.post('/mcp', json={"jsonrpc":"2.0","id":1,"method":"tools/call",
                            "params":{"name":"trex_options_surface","arguments":args}},
               headers={"Authorization":"Bearer token"})
    assert r.status_code == 200
    doc = r.json()['result']['structuredContent']
    assert len(doc['warnings']) <= 8
    assert max(map(len, doc['warnings'])) <= 200
    assert len(doc['recorded']['returned_rows']) == 80
    assert doc['recorded']['truncated'] is True
    assert doc['recorded']['age_seconds'] is None
    assert doc['recorded']['cards']['iv'] is None
    assert doc['recorded']['cards']['earnings'] is None
    row = doc['recorded']['returned_rows'][0]
    assert row['bid'] is None
    assert row['ask'] == 26.75
    assert row['volume'] is None
    assert 'secret' not in row


def test_nested_quote_symbol_mismatch_is_rejected() -> None:
    async def wrong(tenant: str, path: str, params: dict[str, Any]) -> dict[str, Any]:
        return {"symbol": "TSLA", "quote": {"symbol": "NVDA", "last": 500},
                "quote_age_seconds": 3600}
    c = make_client(fetcher=wrong)
    r = c.post('/mcp', json=payload(), headers={"Authorization": "Bearer token"})
    assert r.json()['result']['isError'] is True


def test_future_quote_timestamp_never_presented_as_valid() -> None:
    async def wrong(tenant: str, path: str, params: dict[str, Any]) -> dict[str, Any]:
        return {"symbol": "TSLA", "now": "2026-11-10T10:00:00-05:00",
                "quote": {"last": 370, "source_as_of": "2026-11-11T14:00:00-05:00"},
                "quote_age_seconds": 0}
    c = make_client(fetcher=wrong)
    r = c.post('/mcp', json=payload(), headers={"Authorization": "Bearer token"})
    assert r.json()['result']['isError'] is True


def test_crossed_bid_ask_never_retains_a_tradable_midpoint() -> None:
    async def wrong(tenant: str, path: str, params: dict[str, Any]) -> dict[str, Any]:
        return {"symbol": "TSLA", "available": True,
                "recorded": {"session":"2026-11-10", "slice":[
                    {"exp":"2027-09-17", "right":"P", "strike":300,
                     "bid":27, "ask":25, "mid":26},
                ]}, "live": None}
    c = make_client(fetcher=wrong)
    doc = c.post('/mcp', json={"jsonrpc":"2.0","id":1,"method":"tools/call",
                               "params":{"name":"trex_options_surface","arguments":{"symbol":"TSLA"}}},
                 headers={"Authorization":"Bearer token"}).json()['result']['structuredContent']
    row = doc['recorded']['returned_rows'][0]
    assert row['mid'] is None
    assert row['quote_quality'] == 'crossed_quote'


def test_huge_integer_poison_does_not_crash_safe_projections() -> None:
    huge = 10 ** 400
    async def poisoned(tenant: str, path: str, params: dict[str, Any]) -> dict[str, Any]:
        return {"symbol":"TSLA", "quote":{"last":huge}, "quote_age_seconds":huge}
    c = make_client(fetcher=poisoned)
    doc = c.post('/mcp', json=payload(), headers={"Authorization":"Bearer token"}).json()['result']['structuredContent']
    assert doc['quote']['last'] is None
    assert doc['quote_age_seconds'] is None


def test_future_expiration_session_data_rejected() -> None:
    async def future(tenant: str, path: str, params: dict[str, Any]) -> dict[str, Any]:
        return {"symbol": "TSLA", "now": "2026-11-10T10:00:00-05:00", "available": True,
                "recorded": {"session": "2026-11-11", "slice": []}}
    c = make_client(fetcher=future)
    doc = c.post('/mcp', json={"jsonrpc":"2.0","id":1,"method":"tools/call",
                               "params":{"name":"trex_options_surface","arguments":{"symbol":"TSLA"}}},
                 headers={"Authorization":"Bearer token"}).json()['result']
    assert doc['isError'] is True


def test_bounded_inflight_calls_shed_overload_without_reaching_upstream() -> None:
    async def scenario() -> None:
        entered, release = asyncio.Event(), asyncio.Event()
        count = 0
        async def slow_auth(token: str) -> Principal:
            nonlocal count
            count += 1
            entered.set()
            await release.wait()
            return Principal("tenant-1")
        async def no_fetch(tenant: str, path: str, params: dict[str, Any]) -> dict[str, Any]:
            return {"symbol":"TSLA", "quote": {"last":378}, "quote_age_seconds":3600}
        app = server.create_app(config=config(), authenticate=slow_auth, fetcher=no_fetch,
                                max_inflight=1)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url='http://test') as client:
            headers = {"Authorization":"Bearer token"}
            pending = asyncio.create_task(client.post('/mcp', json=payload(), headers=headers))
            await asyncio.wait_for(entered.wait(), timeout=1)
            rejected = await client.post('/mcp', json=payload(), headers=headers)
            assert rejected.status_code == 429
            assert rejected.headers.get('Retry-After') == '1'
            assert count == 1
            release.set()
            assert (await pending).status_code == 200
    asyncio.run(scenario())


def test_unframed_upstream_stream_stops_at_budget() -> None:
    class Chunks(httpx.AsyncByteStream):
        def __init__(self) -> None:
            self.seen = 0
        async def __aiter__(self):
            for _ in range(2048):
                self.seen += 1
                yield b' ' * 2048
    chunks = Chunks()
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=chunks)
    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(UpstreamUnavailable):
                await server._read_trex(config(), "tenant-1", "/api/market/TSLA", {}, client)
    asyncio.run(run())
    assert chunks.seen < 2048


def test_json_recursion_bomb_fails_closed_instead_of_500() -> None:
    client = make_client()
    deep = b'{"jsonrpc":"2.0","method":"tools/list","id":1,"params":' + b'['*15000 + b']'*15000 + b'}'
    response = client.post('/mcp', content=deep,
                           headers={"Authorization":"Bearer token", "Content-Type":"application/json"})
    assert response.status_code == 200
    assert response.json()['error']['code'] == -32700


def test_tenant_bindings_refuse_shared_loopback_port(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TREX_MCP_PUBLIC_URL", "https://mcp.example/mcp")
    monkeypatch.setenv("TREX_MCP_AUTHORIZATION_SERVER", "https://auth.example")
    monkeypatch.setenv("TREX_MCP_INTROSPECTION_URL", "https://auth.example/introspect")
    monkeypatch.setenv("TREX_MCP_CLIENT_ID", "read")
    monkeypatch.setenv("TREX_MCP_CLIENT_SECRET", "secret")
    monkeypatch.setenv("TREX_MCP_TENANTS_JSON", '{"a":"http://127.0.0.1:8090","b":"http://localhost:8090"}')
    with pytest.raises(ValueError, match="isolation"):
        Config.from_env()


def test_not_before_claim_cannot_grant_future_access() -> None:
    claims = {"active": True, "sub": "customer-1", "aud": "https://mcp.example/mcp",
              "scope": "trex:research:read", "trex_tenant_id": "tenant-1", "nbf": 9_999_999_999}
    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=claims))) as client:
            with pytest.raises(AccessDenied):
                await server._introspect("token", config(), client)
    asyncio.run(run())
