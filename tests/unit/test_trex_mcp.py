"""TREX MCP must be read-only, tenant-scoped and source-explicit."""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tree_options.trex_mcp.analysis import InvalidScenario, evaluate_bearish_scenario
from tree_options.trex_mcp.server import Config, Principal, create_app


def _rpc(method: str, *, rpc_id: int = 1, params: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params or {}}


def _client(*, allowed_origins: frozenset[str] = frozenset()) -> tuple[TestClient, list[tuple[str, str, dict[str, Any]]]]:
    calls: list[tuple[str, str, dict[str, Any]]] = []

    async def auth(token: str) -> Principal:
        if token not in ("valid-a", "valid-b"):
            from tree_options.trex_mcp.server import AccessDenied

            raise AccessDenied("invalid")
        return Principal(tenant_id="a" if token == "valid-a" else "b")

    async def read(tenant: str, path: str, params: dict[str, Any]) -> dict[str, Any]:
        calls.append((tenant, path, params))
        if path.endswith("/options"):
            return {
                "available": True, "now": "2026-11-10T10:12:00-05:00",
                "warnings": ["recorded chain may be stale"],
                "recorded": {
                    "session": "2026-11-09", "age_seconds": 100000, "spot": 378.05,
                    "cards": {"iv": 0.8, "account_id": "do-not-leak"},
                    "slice": [
                        {"exp": "2027-09-17", "right": "P", "strike": 300, "ask": 26.75, "bid": 25.6, "account_id": "do-not-leak"},
                        {"exp": "2027-09-17", "right": "C", "strike": 400, "ask": 49.0},
                        {"exp": "2027-10-15", "right": "P", "strike": 300, "ask": None},
                    ],
                },
                "live": None,
            }
        return {
            "now": "2026-11-10T10:12:00-05:00",
            "quote_age_seconds": 3600,
            "quote": {"last": 378.05, "source_as_of": "2026-11-09T16:00:00-05:00", "account_id": "do-not-leak"},
            "news": [{"title": "should not be exposed"}],
        }

    config = Config(
        public_url="https://mcp.example/mcp",
        authorization_server="https://id.example",
        introspection_url="https://id.example/introspect",
        client_id="test", client_secret="test",
        tenants={"a": "http://127.0.0.1:8090", "b": "http://127.0.0.1:8091"},
        allowed_origins=allowed_origins,
    )
    return TestClient(create_app(config=config, authenticate=auth, fetcher=read)), calls


def _post(c: TestClient, body: dict[str, Any], token: str = "valid-a", **headers: str):
    return c.post(
        "/mcp", json=body,
        headers={"authorization": "Bearer " + token, **headers},
    )


def test_expiration_math_matches_tsla_scenarios() -> None:
    put = evaluate_bearish_scenario({
        "symbol": "tsla", "expiration": "2027-09-17", "strategy": "long_put",
        "spot": 378.05, "long_strike": 300, "net_debit": 26.75,
        "contracts": 1, "target_prices": [265, 230, 190, 0, 400],
        "max_risk_budget": 3000,
    })
    assert put["max_loss_usd"] == 2675
    assert put["max_gain_usd"] == 27325
    assert put["breakeven_at_expiration"] == 273.25
    assert [p["pnl_usd"] for p in put["payoffs_at_expiration"]] == [825, 4325, 8325, 27325, -2675]
    assert put["fits_assumed_risk_budget"] is True
    assert put["execution_eligible"] is False

    spread = evaluate_bearish_scenario({
        "symbol": "TSLA", "expiration": "2027-06-17", "strategy": "put_debit_spread",
        "spot": 378.05, "long_strike": 330, "short_strike": 250,
        "net_debit": 25.55, "target_prices": [0, 230, 300, 400],
    })
    assert spread["max_loss_usd"] == 2555
    assert spread["max_gain_usd"] == 5445
    assert spread["breakeven_at_expiration"] == 304.45
    assert [x["pnl_usd"] for x in spread["payoffs_at_expiration"]] == [5445, 5445, 445, -2555]


@pytest.mark.parametrize(
    "change",
    [
        {"symbol": "TSLA/../../api/commands"},
        {"spot": float("nan")},
        {"net_debit": float("inf")},
        {"contracts": 0},
        {"contracts": 1.5},
        {"target_prices": []},
        {"target_prices": [-1]},
        {"target_prices": [1] * 13},
        {"short_strike": 1},
        {"extra": "foo"},
        {"expiration": "2027-02-31"},
    ],
)
def test_bearish_scenario_rejects_invalid_inputs(change: dict[str, Any]) -> None:
    baseline: dict[str, Any] = {
        "symbol": "TSLA", "expiration": "2027-09-17", "strategy": "long_put",
        "spot": 378, "long_strike": 300, "net_debit": 26.75, "target_prices": [260],
    }
    baseline.update(change)
    with pytest.raises(InvalidScenario):
        evaluate_bearish_scenario(baseline)


def test_mcp_initialize_and_tools_are_read_only() -> None:
    c, calls = _client()
    init = _post(c, _rpc("initialize", params={"protocolVersion": "2026-07-28"}))
    assert init.status_code == 200
    assert init.json()["result"]["protocolVersion"] == "2026-07-28"
    assert init.json()["result"]["capabilities"] == {"tools": {"listChanged": False}}
    assert _post(c, _rpc("notifications/initialized")).status_code == 202
    assert _post(c, _rpc("ping")).json()["result"] == {}
    tools = _post(c, _rpc("tools/list")).json()["result"]["tools"]
    assert {x["name"] for x in tools} == {
        "trex_market_snapshot", "trex_options_surface", "trex_bearish_payoff",
        "trex_bearish_compare",
    }
    assert all(x["annotations"]["readOnlyHint"] for x in tools)
    assert calls == []


def test_market_and_options_tools_are_tenant_scoped_get_only() -> None:
    c, calls = _client()
    result = _post(c, _rpc("tools/call", params={
        "name": "trex_market_snapshot", "arguments": {"symbol": "tsla"},
    }), token="valid-b").json()["result"]["structuredContent"]
    assert result["symbol"] == "TSLA"
    assert result["quote_age_seconds"] == 3600
    assert "news" not in result
    assert "account_id" not in result["quote"]
    assert calls == [("b", "/api/market/TSLA", {})]

    result = _post(c, _rpc("tools/call", params={
        "name": "trex_options_surface",
        "arguments": {"symbol": "TSLA", "expiration": "2027-09-17", "right": "put"},
    }), token="valid-a").json()["result"]["structuredContent"]
    assert result["recorded"]["expiries_in_retrieved_slice"] == ["2027-09-17", "2027-10-15"]
    assert result["recorded"]["matching_rows"] == 1
    assert result["recorded"]["returned_rows"][0]["bid"] == 25.6
    assert "account_id" not in result["recorded"]["returned_rows"][0]
    assert "account_id" not in result["recorded"]["cards"]
    assert result["recorded"]["age_seconds"] == 100000
    assert result["live"] is None
    assert calls[1] == ("a", "/api/market/TSLA/options", {"window": 15, "max_expiries": 12})


def test_missing_expiry_is_disclosed_not_fabricated() -> None:
    c, calls = _client()
    doc = _post(c, _rpc("tools/call", params={
        "name": "trex_options_surface",
        "arguments": {"symbol": "TSLA", "expiration": "2028-01-21"},
    })).json()["result"]["structuredContent"]
    assert doc["recorded"]["expiry_found"] is False
    assert doc["recorded"]["matching_rows"] == 0
    assert doc["nearest_expiry_slice_is_not_a_complete_option_chain"] is True
    assert len(calls) == 1


def test_auth_origin_and_nonexistent_tools_fail_closed() -> None:
    c, calls = _client()
    unauth = c.post("/mcp", json=_rpc("tools/list"))
    assert unauth.status_code == 401
    assert "resource_metadata" in unauth.headers["WWW-Authenticate"]
    assert _post(c, _rpc("tools/list"), token="wrong").status_code == 401
    assert _post(c, _rpc("tools/list"), Origin="https://evil.example").status_code == 403
    non_tool = _post(c, _rpc("tools/call", params={
        "name": "trex_send_order", "arguments": {"symbol": "TSLA"},
    })).json()["result"]
    assert non_tool["isError"] is True
    assert _post(c, _rpc("orders/place")).json()["error"]["code"] == -32601
    assert calls == []


def test_mcp_payoff_requires_auth_but_no_broker_activity() -> None:
    c, calls = _client()
    response = _post(c, _rpc("tools/call", params={
        "name": "trex_bearish_payoff", "arguments": {
            "symbol": "TSLA", "expiration": "2027-09-17", "strategy": "long_put",
            "spot": 378.05, "long_strike": 300, "net_debit": 26.75,
            "target_prices": [230],
        },
    }))
    assert response.json()["result"]["structuredContent"]["payoffs_at_expiration"][0]["pnl_usd"] == 4325
    assert calls == []


def test_oauth_metadata_and_request_size_and_parser_fail_closed() -> None:
    c, calls = _client()
    meta = c.get("/.well-known/oauth-protected-resource/mcp").json()
    assert meta["resource"] == "https://mcp.example/mcp"
    assert "trex:research:read" in meta["scopes_supported"]
    assert _post(c, _rpc("tools/list"), **{"Content-Type": "text/plain"}).status_code == 415
    giant = "x" * 70000
    assert c.post("/mcp", content=giant, headers={
        "authorization": "Bearer valid-a", "Content-Type": "application/json",
    }).status_code == 413
    malformed = c.post("/mcp", content="no json", headers={
        "authorization": "Bearer valid-a", "Content-Type": "application/json",
    })
    assert malformed.json()["error"]["code"] == -32700
    assert calls == []


def test_config_refuses_remote_tenant_and_missing_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "TREX_MCP_PUBLIC_URL", "TREX_MCP_AUTHORIZATION_SERVER", "TREX_MCP_INTROSPECTION_URL",
        "TREX_MCP_CLIENT_ID", "TREX_MCP_CLIENT_SECRET", "TREX_MCP_TENANTS_JSON",
    ):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValueError, match="missing mandatory"):
        Config.from_env()
    monkeypatch.setenv("TREX_MCP_PUBLIC_URL", "https://mcp.example/mcp")
    monkeypatch.setenv("TREX_MCP_AUTHORIZATION_SERVER", "https://auth.example")
    monkeypatch.setenv("TREX_MCP_INTROSPECTION_URL", "https://auth.example/introspect")
    monkeypatch.setenv("TREX_MCP_CLIENT_ID", "abc")
    monkeypatch.setenv("TREX_MCP_CLIENT_SECRET", "shh")
    monkeypatch.setenv("TREX_MCP_TENANTS_JSON", json.dumps({"tenant": "http://evil.example:8090"}))
    with pytest.raises(ValueError, match="loopback"):
        Config.from_env()
    monkeypatch.setenv("TREX_MCP_TENANTS_JSON", json.dumps({"tenant": "http://127.0.0.1:8090"}))
    assert Config.from_env().tenants == {"tenant": "http://127.0.0.1:8090"}


def _modern_rpc(method: str, *, name: str | None = None) -> tuple[dict[str, Any], dict[str, str]]:
    params: dict[str, Any] = {
        "_meta": {
            "io.modelcontextprotocol/protocolVersion": "2026-07-28",
            "io.modelcontextprotocol/clientCapabilities": {},
        },
    }
    if name is not None:
        params["name"] = name
        params["arguments"] = {"symbol": "TSLA"}
    headers = {"MCP-Protocol-Version": "2026-07-28", "Mcp-Method": method}
    if name is not None:
        headers["Mcp-Name"] = name
    return _rpc(method, params=params), headers


def test_modern_discovery_and_tool_result_type() -> None:
    c, calls = _client()
    request, headers = _modern_rpc("server/discover")
    response = _post(c, request, **headers)
    assert response.status_code == 200
    assert response.json()["result"]["resultType"] == "complete"
    assert "2026-07-28" in response.json()["result"]["supportedVersions"]
    request, headers = _modern_rpc("tools/list")
    response = _post(c, request, **headers)
    assert response.json()["result"]["resultType"] == "complete"
    assert len(response.json()["result"]["tools"]) == 4
    request, headers = _modern_rpc("tools/call", name="trex_market_snapshot")
    response = _post(c, request, **headers)
    assert response.json()["result"]["structuredContent"]["symbol"] == "TSLA"
    assert calls == [("a", "/api/market/TSLA", {})]


def test_modern_header_mismatch_and_missing_capabilities_rejected() -> None:
    c, calls = _client()
    request, headers = _modern_rpc("tools/call", name="trex_market_snapshot")
    assert _post(c, request, **{**headers, "Mcp-Name": "trex_send_order"}).json()["error"]["code"] == -32020
    invalid = dict(request)
    invalid["params"] = dict(request["params"])
    invalid["params"]["_meta"] = {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}
    response = _post(c, invalid, **headers)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == -32602
    assert calls == []


def test_modern_unknown_method_returns_http_404_and_version_negotiation() -> None:
    c, calls = _client()
    request, headers = _modern_rpc("orders/place")
    response = _post(c, request, **headers)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == -32601
    response = _post(c, request, **{**headers, "MCP-Protocol-Version": "1900-01-01"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == -32022
    assert calls == []
