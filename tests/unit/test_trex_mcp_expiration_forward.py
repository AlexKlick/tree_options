"""MCP expiration propagation and early request rejection."""
from __future__ import annotations

from fastapi.testclient import TestClient
from tree_options.trex_mcp.server import Config, Principal, create_app


def request(expiration: object) -> dict:
    return {"jsonrpc": "2.0", "id": 9, "method": "tools/call",
            "params": {"name": "trex_options_surface",
                       "arguments": {"symbol": "TSLA", "expiration": expiration}}}


def config() -> Config:
    return Config(public_url="https://mcp.test/mcp", authorization_server="https://id.test",
                  introspection_url="https://id.test/introspect", client_id="test", client_secret="test",
                  tenants={"tenant": "http://127.0.0.1:8090"}, allowed_origins=frozenset())


def test_longdated_expiration_reaches_exact_upstream_projection() -> None:
    calls: list[tuple[str, str, dict]] = []

    async def auth(_token: str) -> Principal:
        return Principal("tenant", "customer")

    async def fetch(tenant: str, path: str, params: dict) -> dict:
        calls.append((tenant, path, params))
        return {"symbol": "TSLA", "now": "2026-11-10T12:00:00-05:00",
                "available": True, "recorded": {"session": "2026-11-09", "slice": [
                    {"exp": "2028-01-21", "right": "P", "strike": 260, "bid": 21.0, "ask": 22.0}
                ]}, "live": None}

    with TestClient(create_app(config=config(), authenticate=auth, fetcher=fetch)) as client:
        resp = client.post("/mcp", json=request("2028-01-21"),
                           headers={"Authorization": "Bearer valid"})
        assert resp.status_code == 200
        projection = resp.json()["result"]["structuredContent"]
        assert projection["requested_expiration"] == "2028-01-21"
        assert projection["recorded"]["expiry_found"] is True
    assert calls == [("tenant", "/api/market/TSLA/options", {
        "window": 15, "max_expiries": 12, "expiration": "2028-01-21"
    })]


def test_noncanonical_expiration_rejected_before_trex_read() -> None:
    calls: list[object] = []

    async def auth(_token: str) -> Principal:
        return Principal("tenant", "customer")

    async def fetch(*args: object) -> dict:
        calls.append(args)
        return {}

    with TestClient(create_app(config=config(), authenticate=auth, fetcher=fetch)) as client:
        for malformed in ("2028-02-30", "2028-1-21",
                          "2028-01-21/../../api/commands", 20280121):
            resp = client.post("/mcp", json=request(malformed),
                               headers={"Authorization": "Bearer valid"})
            assert resp.json()["result"]["isError"] is True
    assert calls == []
