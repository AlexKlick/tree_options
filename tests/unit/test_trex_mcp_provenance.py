"""Adversarial provenance tests: a provider clock is not the gateway's clock.

Red-first: the TREX cache may report a self-consistent but false `now`, and
its cached age can be wrong independently of the original source timestamp.
None of these cases may cause an AI assistant to claim a verified fresh quote.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from tree_options.trex_mcp import server
from tree_options.trex_mcp.server import UpstreamUnavailable

GATEWAY = datetime(2026, 11, 10, 15, 30, tzinfo=timezone.utc)


def test_future_trex_clock_rejected_even_without_quote_timestamp() -> None:
    doc = {"symbol": "TSLA", "now": "2030-01-01T10:00:00-05:00", "quote": {"last": 450}}
    with pytest.raises(UpstreamUnavailable, match="future|clock"):
        server._safe_market_view(doc, "TSLA", gateway_at=GATEWAY)


def test_future_quote_rejected_even_when_provider_clock_agrees() -> None:
    doc = {"symbol": "TSLA", "now": "2030-01-02T10:00:00-05:00", "quote": {
        "last": 450, "source_as_of": "2030-01-01T16:00:00-05:00"}}
    with pytest.raises(UpstreamUnavailable):
        server._safe_market_view(doc, "TSLA", gateway_at=GATEWAY)


def test_conflicting_reported_age_cannot_be_called_verified_freshness() -> None:
    doc = {"symbol": "TSLA", "now": "2026-11-10T10:00:00-05:00", "quote": {
        "last": 378.05, "source_as_of": "2026-11-09T16:00:00-05:00"},
        "quote_age_seconds": 0}
    result = server._safe_market_view(doc, "TSLA", gateway_at=GATEWAY)
    assert result["quote_age_seconds"] == 0  # legacy reported field retained, not trusted
    assert result["gateway_quote_age_seconds"] == 66_600
    assert result["reported_age_consistent"] is False
    assert result["freshness_known"] is False
    assert result["quote_age_basis"] == "reported_age_conflicts_with_source"


def test_matching_source_age_is_distinguished_from_gateway_delay() -> None:
    doc = {"symbol": "TSLA", "now": "2026-11-10T10:00:00-05:00", "quote": {
        "last": 378.05, "source_as_of": "2026-11-10T09:00:00-05:00"},
        "quote_age_seconds": 3600}
    result = server._safe_market_view(doc, "TSLA", gateway_at=GATEWAY)
    assert result["gateway_quote_age_seconds"] == 5400
    assert result["reported_age_consistent"] is True
    assert result["freshness_known"] is True
    assert result["quote_age_basis"] == "verified_source_timestamp"


def test_age_without_source_timestamp_cannot_be_certified() -> None:
    result = server._safe_market_view({"symbol": "TSLA", "quote": {"last": 300},
                                       "quote_age_seconds": 0}, "TSLA", gateway_at=GATEWAY)
    assert result["freshness_known"] is False
    assert result["gateway_quote_age_seconds"] is None
    assert result["quote_age_basis"] == "unverified_trex_report"


def test_malformed_provider_clock_is_rejected_if_present() -> None:
    with pytest.raises(UpstreamUnavailable):
        server._safe_market_view({"symbol": "TSLA", "now": "2026-11-10T10:00:00",
                                  "quote": {"last": 300}}, "TSLA", gateway_at=GATEWAY)


def test_live_options_cache_cannot_have_future_fetch_timestamp() -> None:
    doc = {"symbol": "TSLA", "now": "2026-11-10T10:00:00-05:00", "available": True,
           "live": {"fetched_at": "2026-11-11T11:00:00-05:00", "age_seconds": 0,
                    "ttl_seconds": 60, "slice": []}}
    with pytest.raises(UpstreamUnavailable):
        server._safe_options_view(doc, "TSLA", None, "put", gateway_at=GATEWAY)


def test_recorded_future_session_rejected_in_exchange_timezone() -> None:
    # 15:30 UTC on Nov 10 is 10:30 New York. An EOD session from Nov 11 is impossible.
    doc = {"symbol": "TSLA", "available": True, "recorded": {
        "session": "2026-11-11", "age_seconds": 0, "slice": []}}
    with pytest.raises(UpstreamUnavailable):
        server._safe_options_view(doc, "TSLA", None, "put", gateway_at=GATEWAY)


def test_live_option_ttl_uses_gateway_age_not_bogus_age_report() -> None:
    doc = {"symbol": "TSLA", "now": "2026-11-10T10:00:00-05:00", "available": True,
           "live": {"fetched_at": "2026-11-10T09:00:00-05:00", "age_seconds": 0,
                    "ttl_seconds": 60, "slice": []}}
    result = server._safe_options_view(doc, "TSLA", None, "put", gateway_at=GATEWAY)
    live = result["live"]
    assert live["gateway_age_seconds"] == 5400
    assert live["cache_expired"] is True
    assert live["cache_age_verified"] is True
    assert live["age_seconds"] == 0  # original source report, clearly distinguished


def test_live_cache_without_fetch_timestamp_has_unknown_ttl_validity() -> None:
    doc = {"symbol": "TSLA", "available": True, "live": {
        "age_seconds": 0, "ttl_seconds": 60, "slice": []}}
    result = server._safe_options_view(doc, "TSLA", None, "put", gateway_at=GATEWAY)
    assert result["live"]["cache_expired"] is None
    assert result["live"]["cache_age_verified"] is False


def test_acceptable_clock_skew_does_not_reject() -> None:
    doc = {"symbol": "TSLA", "now": "2026-11-10T10:30:20-05:00",
           "quote": {"last": 378, "source_as_of": "2026-11-10T10:30:10-05:00"},
           "quote_age_seconds": 10}
    result = server._safe_market_view(doc, "TSLA", gateway_at=GATEWAY)
    assert result["freshness_known"] is True
    assert result["gateway_quote_age_seconds"] == 0


def test_exchange_date_is_not_utc_calendar_date_at_midnight() -> None:
    near_midnight_utc = datetime(2026, 11, 11, 1, 0, tzinfo=timezone.utc)
    with pytest.raises(UpstreamUnavailable, match="future"):
        server._safe_options_view({"symbol": "TSLA", "recorded": {
            "session": "2026-11-11", "slice": []}}, "TSLA", None, "put",
            gateway_at=near_midnight_utc)


def test_http_mcp_tool_rejects_self_consistent_future_source_clock() -> None:
    from fastapi.testclient import TestClient

    async def auth(_token: str) -> server.Principal:
        return server.Principal("test", "customer-1")

    async def source(_tenant: str, _path: str, _params: dict) -> dict:
        return {"symbol": "TSLA", "now": "2030-01-01T10:00:00-05:00",
                "quote": {"last": 500, "source_as_of": "2029-12-31T16:00:00-05:00"},
                "quote_age_seconds": 1}

    app = server.create_app(authenticate=auth, fetcher=source)
    with TestClient(app) as client:
        r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1,
            "method": "tools/call", "params": {
                "name": "trex_market_snapshot", "arguments": {"symbol": "TSLA"}}},
            headers={"Authorization": "Bearer demo"})
    assert r.status_code == 200
    assert r.json()["result"]["isError"] is True
    assert r.json()["result"]["content"][0]["text"] == (
        "TREX read unavailable; no quote inferred")


def test_pilot_gate_includes_new_provenance_suite() -> None:
    """The operator's exact-head gate must not silently skip this new suite."""
    import importlib.util
    from pathlib import Path

    script = Path(__file__).resolve().parents[2] / "scripts/trex_mcp_pilot_gate.py"
    spec = importlib.util.spec_from_file_location("trex_mcp_pilot_gate_provenance", script)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert "tests/unit/test_trex_mcp_provenance.py" in mod.TESTS
