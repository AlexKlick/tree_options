"""Red/green: OAuth and network deadline invariants of the customer gateway."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from tree_options.trex_mcp import server
from tree_options.trex_mcp.server import AccessDenied, Config, UpstreamUnavailable


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> None:
    fields = {
        "TREX_MCP_PUBLIC_URL": "https://mcp.example/mcp",
        "TREX_MCP_AUTHORIZATION_SERVER": "https://id.example",
        "TREX_MCP_INTROSPECTION_URL": "https://id.example/introspect",
        "TREX_MCP_CLIENT_ID": "id",
        "TREX_MCP_CLIENT_SECRET": "secret",
        "TREX_MCP_TENANTS_JSON": json.dumps({"customer": "http://127.0.0.1:8090"}),
        "TREX_MCP_ALLOWED_ORIGINS": "",
    }
    for k, v in fields.items():
        monkeypatch.setenv(k, v)


@pytest.mark.parametrize("name,value", [
    ("TREX_MCP_PUBLIC_URL", "https://u:p@mcp.example/mcp"),
    ("TREX_MCP_PUBLIC_URL", "https://mcp.example/mcp?token=secret"),
    ("TREX_MCP_PUBLIC_URL", "https://mcp.example/mcp#frag"),
    ("TREX_MCP_PUBLIC_URL", "https:///mcp"),
    ("TREX_MCP_INTROSPECTION_URL", "https://id:password@id.example/check"),
    ("TREX_MCP_INTROSPECTION_URL", "https://id.example/check#frag"),
    ("TREX_MCP_AUTHORIZATION_SERVER", "https://login.example#fragment"),
    ("TREX_MCP_ALLOWED_ORIGINS", "*"),
    ("TREX_MCP_ALLOWED_ORIGINS", "https://host.example/path"),
    ("TREX_MCP_ALLOWED_ORIGINS", "https://u:password@host.example"),
    ("TREX_MCP_TENANTS_JSON", '{"customer":"http://127.0.0.1:0"}'),
])
def test_config_refuses_ambiguous_or_credential_bearing_urls(
    env: None, monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError):
        Config.from_env()


def test_config_preserves_normal_strict_configuration(env: None) -> None:
    assert Config.from_env().tenants == {"customer": "http://127.0.0.1:8090"}


class SlowDrip(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b'{"symbol": "TSLA",'
        await asyncio.sleep(.1)
        yield b'"quote": null}'


def test_upstream_total_deadline_bounds_a_slow_valid_trickle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "MAX_UPSTREAM_TOTAL_SECONDS", .02, raising=False)
    cfg = Config("https://mcp.example/mcp", "https://id.example", "https://id.example/introspect", "id", "secret", {"customer": "http://127.0.0.1:8090"}, frozenset())

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, stream=SlowDrip()))) as client:
            with pytest.raises(UpstreamUnavailable, match="failed"):
                await server._read_trex(cfg, "customer", "/api/market/TSLA", {}, client)
    asyncio.run(run())


def test_introspection_total_deadline_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "MAX_UPSTREAM_TOTAL_SECONDS", .02, raising=False)
    cfg = Config("https://mcp.example/mcp", "https://id.example", "https://id.example/introspect", "id", "secret", {"customer": "http://127.0.0.1:8090"}, frozenset())

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, stream=SlowDrip()))) as client:
            with pytest.raises(UpstreamUnavailable, match="failed"):
                await server._introspect("token", cfg, client)
    asyncio.run(run())


def test_introspection_token_time_claims_must_be_consistent() -> None:
    cfg = Config("https://mcp.example/mcp", "https://id.example", "https://id.example/introspect", "id", "secret", {"customer": "http://127.0.0.1:8090"}, frozenset())
    claims = {"active": True, "sub": "user", "aud": cfg.public_url,
              "scope": "trex:research:read", "trex_tenant_id": "customer", "iat": 9_999_999_999}

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=claims))) as client:
            with pytest.raises(AccessDenied):
                await server._introspect("token", cfg, client)
    asyncio.run(run())
