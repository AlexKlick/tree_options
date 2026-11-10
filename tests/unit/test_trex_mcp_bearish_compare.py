"""TDD: bounded side-by-side bearish put comparisons, never trade intents."""
from __future__ import annotations

import asyncio

import httpx
import pytest

from tree_options.trex_mcp.analysis import InvalidScenario, compare_bearish_scenarios
from tree_options.trex_mcp.server import Config, Principal, TOOLS, create_app


BASE = {
    "symbol": "TSLA", "spot": 378.05,
    "target_prices": [190, 230, 265, 400],
    "max_risk_budget": 3000,
    "candidates": [
        {"id": "sep-put", "expiration": "2027-09-17", "strategy": "long_put",
         "long_strike": 300, "net_debit": 26.75},
        {"id": "jan-put", "expiration": "2028-01-21", "strategy": "long_put",
         "long_strike": 260, "net_debit": 22.20},
        {"id": "jun-spread", "expiration": "2027-06-17", "strategy": "put_debit_spread",
         "long_strike": 330, "short_strike": 250, "net_debit": 25.55},
    ],
}


def test_comparison_is_bounded_pure_math_with_distinct_expiries() -> None:
    doc = compare_bearish_scenarios(BASE)
    assert doc["schema"] == "trex-mcp-bearish-comparison/1"
    assert doc["execution_eligible"] is False
    assert doc["market_quote_verified"] is False
    assert [r["id"] for r in doc["candidates"]] == ["sep-put", "jan-put", "jun-spread"]
    assert [r["max_loss_usd"] for r in doc["candidates"]] == [2675, 2220, 2555]
    # Spot $230: September long put $4325, January long put $780, June vertical $5445.
    assert [r["payoffs_at_expiration"][1]["pnl_usd"] for r in doc["candidates"]] == [4325, 780, 5445]
    assert doc["different_expirations"] is True
    assert all(r["fits_assumed_risk_budget"] is True for r in doc["candidates"])
    assert "not simultaneous" in doc["risk_semantics"].lower()


@pytest.mark.parametrize("mutation", [
    {"candidates": BASE["candidates"][:1]},
    {"candidates": BASE["candidates"] * 3},
    {"candidates": [BASE["candidates"][0]] * 2},
    {"candidates": [{**BASE["candidates"][0], "id": "has spaces"}, BASE["candidates"][1]]},
    {"candidates": [{**BASE["candidates"][0], "secret": "yes"}, BASE["candidates"][1]]},
    {"candidates": [{**BASE["candidates"][0], "net_debit": -2}, BASE["candidates"][1]]},
    {"target_prices": [10] * 20},
    {"max_risk_budget": -100},
    {"broker_account_id": "should-never-be-accepted"},
])
def test_bad_batches_fail_closed(mutation) -> None:
    with pytest.raises(InvalidScenario):
        compare_bearish_scenarios({**BASE, **mutation})


def test_tool_is_read_only_and_uses_no_market_or_broker_fetch() -> None:
    async def auth(_: str) -> Principal:
        return Principal("tenant", "customer")
    async def no_data(*_args, **_kwargs):
        raise AssertionError("pure scenario must not read market or broker")
    cfg = Config("https://mcp.example/mcp", "https://id.example", "https://id.example/introspect",
                 "id", "secret", {"tenant":"http://127.0.0.1:8090"}, frozenset())
    app = create_app(config=cfg, authenticate=auth, fetcher=no_data)
    tool = next(t for t in TOOLS if t["name"] == "trex_bearish_compare")
    assert tool["annotations"]["readOnlyHint"] is True

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post("/mcp", json={"jsonrpc":"2.0","id":3,"method":"tools/call",
                "params":{"name":"trex_bearish_compare","arguments":BASE}}, headers={"Authorization":"Bearer valid"})
        assert r.status_code == 200
        response = r.json()["result"]
        assert response["isError"] is False
        assert len(response["structuredContent"]["candidates"]) == 3
    asyncio.run(run())
