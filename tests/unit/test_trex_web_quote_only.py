"""Contract tests for the broker-free, quote-only TREX web endpoint.

This module runs in the full tree_options checkout alongside existing
test_trex_web.py; it does not require a running broker or market connection.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tree_options.trex.clock import ET
from tree_options.trex.discovery.market import MarketCache
from tree_options.trex_web.app import create_app


def _client(tmp_path: Path) -> tuple[TestClient, Path]:
    discovery = tmp_path / "discovery"
    app = create_app(
        state_dir=str(tmp_path / "state"),
        plans_dir=str(tmp_path / "plans"),
        discovery_dir=str(discovery),
        desk_store_dir=str(tmp_path / "desk-store"),
        desk_state_dir=str(tmp_path / "desk-state"),
        desk_run_dir=str(tmp_path / "run"),
        supervised_dir=str(tmp_path / "supervised"),
    )
    return TestClient(app), discovery


def _seed(discovery: Path) -> dict[str, object]:
    discovery.mkdir(parents=True, exist_ok=True)
    quote: dict[str, object] = {
        "last": 378.05, "bid": 377.50, "ask": 378.25,
        "source_as_of": datetime.now(ET).isoformat(),
        "source": "test fixture",
    }
    (discovery / "market.json").write_text(
        json.dumps({"last_refresh": quote["source_as_of"], "symbols": {"TSLA": quote}})
    )
    return quote


def test_quote_endpoint_does_not_read_bar_or_news_envelopes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, discovery = _client(tmp_path)
    quote = _seed(discovery)

    def forbid_envelope(*args: object, **kwargs: object) -> None:
        raise AssertionError("quote-only route read an unnecessary bars/news envelope")

    monkeypatch.setattr(MarketCache, "get_envelope", forbid_envelope)
    response = client.get("/api/market/tsla/quote")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"now", "symbol", "quote", "quote_age_seconds"}
    assert body["symbol"] == "TSLA"
    assert body["quote"] == quote
    assert isinstance(body["quote_age_seconds"], int)
    assert body["quote_age_seconds"] >= 0


def test_quote_matches_legacy_full_symbol_projection(tmp_path: Path) -> None:
    client, discovery = _client(tmp_path)
    _seed(discovery)
    full = client.get("/api/market/TSLA").json()
    lean = client.get("/api/market/TSLA/quote").json()
    assert lean["quote"] == full["quote"]
    assert lean["symbol"] == full["symbol"] == "TSLA"
    assert abs(lean["quote_age_seconds"] - full["quote_age_seconds"]) <= 1
    assert "bars" in full and "news" in full
    assert "bars" not in lean and "news" not in lean
    assert "account" not in lean and "positions" not in lean


def test_quote_source_fallback_respects_market_cache(tmp_path: Path) -> None:
    client, discovery = _client(tmp_path)
    (discovery / "market").mkdir(parents=True, exist_ok=True)
    (discovery / "market.json").write_text("{bad json")
    quote = {"last": 333.0, "source_as_of": datetime.now(ET).isoformat()}
    MarketCache(discovery / "market" / "cache").put(
        "quote", "TSLA", quote, datetime.now(ET)
    )
    response = client.get("/api/market/TSLA/quote")
    assert response.status_code == 200
    assert response.json()["quote"] == quote


def test_missing_quote_stays_null_and_no_refresh_is_scheduled(tmp_path: Path) -> None:
    client, discovery = _client(tmp_path)
    before = list(discovery.rglob("*")) if discovery.exists() else []
    response = client.get("/api/market/TSLA/quote")
    after = list(discovery.rglob("*")) if discovery.exists() else []
    assert response.status_code == 200
    assert response.json()["quote"] is None
    assert response.json()["quote_age_seconds"] is None
    assert after == before


@pytest.mark.parametrize("bad", ["ABCDEFG", "A..B", "A.B.C", "TSLA-1"])
def test_quote_endpoint_rejects_invalid_symbols(tmp_path: Path, bad: str) -> None:
    client, _discovery = _client(tmp_path)
    assert client.get(f"/api/market/{bad}/quote").status_code == 404
