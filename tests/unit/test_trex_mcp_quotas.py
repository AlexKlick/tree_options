"""Deterministic per-customer resource budgets, without brokering trading powers."""
from __future__ import annotations

import asyncio

import httpx

from tree_options.trex_mcp import server
from tree_options.trex_mcp.server import Config, Principal
from tree_options.trex_mcp.quota import QuotaLimiter


def test_subject_and_tenant_quota_are_independent_and_replenish() -> None:
    clock = [100.0]
    q = QuotaLimiter(subject_limit=2, tenant_limit=3, window_seconds=30, max_subject_keys=10,
                     clock=lambda: clock[0])
    async def run():
        a = Principal("tenant-a", "alice")
        b = Principal("tenant-a", "bob")
        c = Principal("tenant-b", "alice")
        assert await q.admit(a)
        assert await q.admit(a)
        assert not await q.admit(a)  # Subject isolated
        assert await q.admit(b)
        assert not await q.admit(b)  # Tenant consumed shared allotment
        assert await q.admit(c)  # Same subject, different tenant
        clock[0] += 31
        assert await q.admit(a)  # Window replenished
    asyncio.run(run())


def test_bounded_identity_registry_fails_closed_until_idle_entries_expire() -> None:
    clock = [0.0]
    q = QuotaLimiter(subject_limit=2, tenant_limit=10, window_seconds=10,
                     max_subject_keys=2, clock=lambda: clock[0])
    async def run():
        assert await q.admit(Principal("a", "user-1"))
        assert await q.admit(Principal("a", "user-2"))
        assert not await q.admit(Principal("a", "user-3"))
        assert len(q.subject_windows) == 2
        assert all("user-" not in repr(key) for key in q.subject_windows)
        clock[0] += 11
        assert await q.admit(Principal("a", "user-3"))
        assert len(q.subject_windows) <= 2
    asyncio.run(run())


def test_parallel_calls_do_not_exceed_quota() -> None:
    q = QuotaLimiter(subject_limit=5, tenant_limit=5, window_seconds=60,
                     max_subject_keys=10)
    async def run():
        res = await asyncio.gather(*(q.admit(Principal("tenant", "same")) for _ in range(100)))
        assert sum(res) == 5
    asyncio.run(run())


def test_app_uses_authenticated_identity_for_429_before_tool_work() -> None:
    async def run():
        counts = {"auth": 0, "fetch": 0}
        async def auth(token: str) -> Principal:
            counts["auth"] += 1
            return Principal("tenant", "same-customer")
        async def fetch(tenant: str, path: str, params: dict[str, object]):
            counts["fetch"] += 1
            return {"symbol": "TSLA", "quote": None}
        cfg = Config(
            "https://mcp.example/mcp", "https://id.example", "https://id.example/introspect",
            "id", "secret", {"tenant": "http://127.0.0.1:8090"}, frozenset(),
        )
        app = server.create_app(
            config=cfg, authenticate=auth, fetcher=fetch,
            quota_per_subject=2, quota_per_tenant=5,
        )
        req = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": "trex_market_snapshot", "arguments": {"symbol": "TSLA"}}}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
            responses = [await c.post("/mcp", json=req, headers={"Authorization": "Bearer token"}) for _ in range(3)]
        assert [r.status_code for r in responses] == [200, 200, 429]
        assert responses[-1].headers["Retry-After"]
        assert counts == {"auth": 3, "fetch": 2}
    asyncio.run(run())
