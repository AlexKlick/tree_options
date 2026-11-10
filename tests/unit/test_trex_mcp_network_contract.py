"""End-to-end, in-process HTTPX transport contract through real OAuth + TREX GET paths.

Unlike the injected auth/fetch unit tests, these exercise token introspection
and tenant URL construction. Every network response is deterministic and offline.
"""
from __future__ import annotations

from collections import Counter
from urllib.parse import parse_qs

import httpx
from fastapi.testclient import TestClient

from tree_options.trex_mcp.server import Config, create_app


def config() -> Config:
    return Config(
        public_url="https://mcp.example/mcp", authorization_server="https://id.example",
        introspection_url="https://id.example/introspect", client_id="mcp",
        client_secret="not-a-real-secret",
        tenants={"alpha": "http://127.0.0.1:8090", "beta": "http://127.0.0.1:8091"},
        allowed_origins=frozenset(),
    )


class Harness:
    def __init__(self) -> None:
        self.count = Counter()
        self.revoked: set[str] = set()
        self.account_requests: list[str] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.count["requests"] += 1
        if request.url.host == "id.example":
            self.count["oauth"] += 1
            assert request.method == "POST"
            assert request.url.path == "/introspect"
            # Auth only to the IDP: never recorded in gateway tool output.
            assert request.headers.get("authorization", "").startswith("Basic ")
            token = parse_qs(request.content.decode("utf-8"))["token"][0]
            if token == "oauth-down":
                return httpx.Response(503, json={"error": "unavailable"})
            if token in self.revoked or token == "inactive":
                return httpx.Response(200, json={"active": False})
            tenant = "alpha" if token.endswith("-a") else "beta"
            claims = {
                "active": True, "aud": "https://mcp.example/mcp",
                "scope": "trex:research:read", "trex_tenant_id": tenant, "sub": "customer-" + tenant,
            }
            if token == "wrong-aud-a":
                claims["aud"] = "https://other.example/mcp"
            if token == "wrong-scope-a":
                claims["scope"] = "trex:research:write"
            if token == "unknown-tenant-a":
                claims["trex_tenant_id"] = "gamma"
            return httpx.Response(200, json=claims)
        self.count["trex"] += 1
        assert request.method == "GET"
        assert request.headers.get("authorization") is None  # No OAuth token egress.
        assert request.url.path in ("/api/market/TSLA/quote", "/api/market/TSLA/options")
        self.account_requests.append(f"{request.url.port}:{request.url.path}")
        if request.url.port == 8090:
            spot = 375.0
        elif request.url.port == 8091:
            spot = 380.0
        else:
            raise AssertionError("wrong tenant instance")
        if request.url.path.endswith("/options"):
            assert request.url.params.get("window") == "15"
            assert request.url.params.get("max_expiries") == "12"
            return httpx.Response(200, json={
                "symbol": "TSLA", "now": "2026-11-10T11:00:00-05:00", "available": True,
                "recorded": {"session": "2026-11-09", "spot": spot, "slice": [
                    {"exp": "2027-09-17", "right": "P", "strike": 300.0,
                     "bid": 26.0, "ask": 26.75, "mid": 26.375},
                ]},
            })
        return httpx.Response(200, json={
            "symbol": "TSLA", "now": "2026-11-10T11:00:00-05:00",
            "quote": {"last": spot, "source_as_of": "2026-11-09T16:00:00-05:00"},
            "quote_age_seconds": 68_400,
        })


def request(method: str = "trex_market_snapshot", arguments: dict | None = None) -> dict:
    return {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": method, "arguments": arguments or {"symbol": "TSLA"}}}


def test_actual_oauth_to_isolated_trex_paths_and_no_token_egress() -> None:
    h = Harness()
    app = create_app(config=config(), http_client_factory=lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(h.handle), follow_redirects=False,
    ))
    with TestClient(app) as client:
        a = client.post("/mcp", json=request(), headers={"Authorization": "Bearer valid-a"})
        b = client.post("/mcp", json=request(), headers={"Authorization": "Bearer valid-b"})
        option = client.post("/mcp", json=request("trex_options_surface"),
                             headers={"Authorization": "Bearer valid-a"})
        assert a.json()["result"]["structuredContent"]["quote"]["last"] == 375
        assert b.json()["result"]["structuredContent"]["quote"]["last"] == 380
        assert option.json()["result"]["structuredContent"]["recorded"]["returned_rows"][0]["ask"] == 26.75
        assert h.account_requests == ["8090:/api/market/TSLA/quote", "8091:/api/market/TSLA/quote", "8090:/api/market/TSLA/options"]
        assert h.count["oauth"] == 3
        assert h.count["trex"] == 3
        assert "customer-alpha" not in str(a.json())


def test_revocation_rechecked_without_cached_grants_and_no_fallback() -> None:
    h = Harness()
    app = create_app(config=config(), http_client_factory=lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(h.handle)))
    with TestClient(app) as client:
        assert client.post("/mcp", json=request(), headers={"Authorization": "Bearer valid-a"}).status_code == 200
        h.revoked.add("valid-a")
        assert client.post("/mcp", json=request(), headers={"Authorization": "Bearer valid-a"}).status_code == 401
        assert client.post("/mcp", json=request(), headers={"Authorization": "Bearer oauth-down"}).status_code == 503
        assert h.count["trex"] == 1
        assert h.count["oauth"] == 3


def test_wrong_audience_scope_and_tenant_do_not_reach_trex() -> None:
    h = Harness()
    app = create_app(config=config(), http_client_factory=lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(h.handle)))
    with TestClient(app) as client:
        for token in ("wrong-aud-a", "wrong-scope-a", "unknown-tenant-a", "inactive"):
            response = client.post("/mcp", json=request(), headers={"Authorization": "Bearer " + token})
            assert response.status_code == 401
        assert h.count["oauth"] == 4
        assert h.count["trex"] == 0


def test_unknown_broker_methods_never_open_upstream_connection() -> None:
    h = Harness()
    app = create_app(config=config(), http_client_factory=lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(h.handle)))
    with TestClient(app) as client:
        bad = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "orders/place",
                        "params": {"account": "alpha", "symbol": "TSLA"}},
                          headers={"Authorization": "Bearer valid-a"})
        assert bad.json()["error"]["code"] == -32601
        assert h.count["oauth"] == 1
        assert h.count["trex"] == 0
