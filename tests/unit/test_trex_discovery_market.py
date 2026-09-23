"""M5a: the market fetcher - CBOE delayed quotes/chains, Google News RSS,
Polygon daily bars; TTL envelope cache; market.json snapshot.

Floats live in this lane by convention (never piped into book.json
writers). Every quote carries its source timestamp so the UI can show
DATA age, not transport age.
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import ClassVar
from zoneinfo import ZoneInfo

import pytest

from tree_options.trex.discovery.market import (
    MarketCache,
    fetch_chain_puts,
    fetch_daily_bars,
    fetch_equity_quote,
    fetch_news,
    market_cycle,
)

ET = ZoneInfo("America/New_York")
NOW = datetime(2026, 9, 22, 18, 30, tzinfo=ET)

CBOE_QUOTE_BODY = {
    "timestamp": "2026-09-22 22:08:55",
    "data": {
        "bid": 773.25,
        "ask": 773.30,
        "close": 773.38,
        "iv30": 11.431,
        "price_change_percent": -0.42,
        "last_trade_time": "2026-09-22T16:00:00",
    },
}

CBOE_CHAIN_BODY = {
    "data": {
        "options": [
            {"option": "SPY260619C00600000", "bid": 223.15, "ask": 223.92},
            {"option": "SPY261218P00575000", "bid": 4.10, "ask": 4.35, "iv": 0.18, "delta": -0.31},
            {"option": "SPY261218P00580000", "bid": 5.60, "ask": 5.85, "iv": 0.17, "delta": -0.36},
        ]
    }
}

NEWS_RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel><title>NVDA - Google News</title>
<item><title>Nvidia chips surge</title><link>https://example.com/a</link>
<pubDate>Mon, 21 Sep 2026 18:14:00 GMT</pubDate><source>Reuters</source></item>
<item><title>Second item</title><link>https://example.com/b</link>
<pubDate>Mon, 21 Sep 2026 12:00:00 GMT</pubDate></item>
</channel></rss>
"""


def _transport(body: bytes, status: int = 200):
    calls: list[str] = []

    def t(url: str, *, timeout: float = 10.0) -> tuple[int, bytes]:
        calls.append(url)
        return status, body

    t.calls = calls  # type: ignore[attr-defined]
    return t


class TestMarketCache:
    def test_ttl_fresh_hit_and_expired_miss(self, tmp_path: Path) -> None:
        cache = MarketCache(tmp_path / "cache")
        cache.put("quote", "SPY", {"bid": 1.0}, now=NOW)
        assert cache.get("quote", "SPY", now=NOW) == {"bid": 1.0}
        later = datetime(2026, 9, 22, 18, 32, tzinfo=ET)  # > 60s ttl
        assert cache.get("quote", "SPY", now=later) is None
        assert cache.get("quote", "QQQ", now=NOW) is None

    def test_kind_ttls(self, tmp_path: Path) -> None:
        cache = MarketCache(tmp_path / "cache")
        assert cache.ttl("quote") == 60
        assert cache.ttl("news") == 1800
        assert cache.ttl("bars") == 86400

    def test_junk_entry_is_a_miss(self, tmp_path: Path) -> None:
        d = tmp_path / "cache" / "quote"
        d.mkdir(parents=True)
        (d / "SPY.json").write_text("{torn")
        assert MarketCache(tmp_path / "cache").get("quote", "SPY", now=NOW) is None
        assert MarketCache(tmp_path / "cache").get_envelope("quote", "SPY") is None

    def test_envelope_ignores_ttl(self, tmp_path: Path) -> None:
        cache = MarketCache(tmp_path / "cache")
        cache.put("news", "SPY", {"items": [1]}, now=NOW)
        much_later = datetime(2026, 12, 1, 9, 0, tzinfo=ET)
        assert cache.get("news", "SPY", now=much_later) is None
        env = cache.get_envelope("news", "SPY")
        assert env is not None
        assert env["payload"] == {"items": [1]}
        assert env["fetched_at"] == NOW.isoformat()


class TestFetchers:
    def test_quote_keys_pinned(self) -> None:
        t = _transport(json.dumps(CBOE_QUOTE_BODY).encode())
        q = fetch_equity_quote("SPY", t)
        assert q["bid"] == pytest.approx(773.25)
        assert q["ask"] == pytest.approx(773.30)
        assert q["close"] == pytest.approx(773.38)
        assert q["iv30"] == pytest.approx(11.431)
        assert q["change_pct"] == pytest.approx(-0.42)
        assert q["source_as_of"] == "2026-09-22T22:08:55+00:00"

    def test_chain_puts_only_with_greeks(self) -> None:
        t = _transport(json.dumps(CBOE_CHAIN_BODY).encode())
        puts = fetch_chain_puts("SPY", t)
        assert set(puts.keys()) == {"20261218"}
        row = puts["20261218"]
        assert 575.0 in row and 580.0 in row
        leg = row[575.0]
        assert leg["bid"] == pytest.approx(4.10)
        assert leg["iv"] == pytest.approx(0.18)
        assert leg["delta"] == pytest.approx(-0.31)

    def test_news_items_capped_and_shaped(self) -> None:
        t = _transport(NEWS_RSS.encode())
        items = fetch_news("NVDA", t)
        assert len(items) == 2
        assert items[0]["title"] == "Nvidia chips surge"
        assert items[0]["link"].startswith("https://")
        assert items[0]["source"] == "Reuters"

    def test_news_junk_xml_returns_empty_with_note(self) -> None:
        t = _transport(b"<not-rss>")
        items = fetch_news("NVDA", t)
        assert items == []

    def test_bad_status_raises(self) -> None:
        t = _transport(b"gateway timeout", status=504)
        with pytest.raises(RuntimeError):
            fetch_equity_quote("SPY", t)

    def test_daily_bars_via_fake_client(self) -> None:
        class FakeMassive:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def get_json(self, path: str, params: dict, *, use_cache: bool = True) -> dict:
                self.calls.append(path)
                assert use_cache is False  # rolling window must bypass
                return {
                    "results": [
                        {"t": 1789992000000, "c": 750.1},
                        {"t": 1790078400000, "c": 757.67, "v": Decimal("13803038.3")},
                    ]
                }

        client = FakeMassive()
        bars = fetch_daily_bars("SPY", client)
        assert bars == [
            {"t": 1789992000000, "c": 750.1, "v": None},
            {"t": 1790078400000, "c": 757.67, "v": 13803038.3},
        ]
        # Decimal volumes must be JSON-serializable (the cache envelope write
        # silently swallowed TypeError before this pin)
        json.dumps(bars)
        assert "/v2/aggs/ticker/SPY/range/1/day/" in client.calls[0]


class TestMarketCycle:
    def _state(self, tmp_path: Path) -> Path:
        state = tmp_path / "state"
        state.mkdir()
        return state

    def test_writes_market_json_from_fetches(self, tmp_path: Path) -> None:
        state = self._state(tmp_path)
        t = _transport(json.dumps(CBOE_QUOTE_BODY).encode())

        class Cfg:
            underlyings: ClassVar[list[str]] = ["SPY"]
            market_refresh_seconds = 60

        ran = market_cycle(state, Cfg(), now=NOW, transport=t)
        assert ran is True
        doc = json.loads((state / "market.json").read_text())
        spy = doc["symbols"]["SPY"]
        assert spy["bid"] == pytest.approx(773.25)
        assert spy["source_as_of"] == "2026-09-22T22:08:55+00:00"
        assert doc["last_refresh"]

    def test_fresh_cache_skips_fetch(self, tmp_path: Path) -> None:
        state = self._state(tmp_path)
        t = _transport(json.dumps(CBOE_QUOTE_BODY).encode())

        class Cfg:
            underlyings: ClassVar[list[str]] = ["SPY"]
            market_refresh_seconds = 60

        market_cycle(state, Cfg(), now=NOW, transport=t)
        market_cycle(state, Cfg(), now=NOW, transport=t)
        assert len(t.calls) == 1  # second cycle: cache hit, no wire

    def test_fetch_failure_isolated_per_symbol(self, tmp_path: Path) -> None:
        state = self._state(tmp_path)

        def t(url: str, *, timeout: float = 10.0) -> tuple[int, bytes]:
            if "QQQ" in url:
                return 503, b"boom"
            return 200, json.dumps(CBOE_QUOTE_BODY).encode()

        class Cfg:
            underlyings: ClassVar[list[str]] = ["SPY", "QQQ"]
            market_refresh_seconds = 60

        ran = market_cycle(state, Cfg(), now=NOW, transport=t)
        assert ran is True
        doc = json.loads((state / "market.json").read_text())
        assert doc["symbols"]["SPY"]["bid"] is not None
        assert "QQQ" in doc["errors"]

    def test_expired_news_rewarms_cold_stays_cold(self, tmp_path: Path) -> None:
        """A warmed symbol's news refreshes after its TTL on ordinary
        cycles; never-warmed symbols cause no ambient RSS traffic."""
        state = self._state(tmp_path)
        cache = MarketCache(state / "market" / "cache")
        cache.put("news", "SPY", {"items": [{"title": "stale"}]}, now=NOW)

        def t(url: str, *, timeout: float = 10.0) -> tuple[int, bytes]:
            t.calls.append(url)  # type: ignore[attr-defined]
            if "news.google.com" in url:
                return 200, NEWS_RSS.encode()
            return 200, json.dumps(CBOE_QUOTE_BODY).encode()

        t.calls = []  # type: ignore[attr-defined]

        class Cfg:
            underlyings: ClassVar[list[str]] = ["SPY", "QQQ"]
            market_refresh_seconds = 60

        later = datetime(2026, 9, 22, 19, 5, tzinfo=ET)  # > 1800s news ttl
        market_cycle(state, Cfg(), now=later, transport=t)
        news_calls = [u for u in t.calls if "news.google.com" in u]  # type: ignore[attr-defined]
        assert len(news_calls) == 1 and "SPY" in news_calls[0]
        fresh = cache.get("news", "SPY", now=later)
        assert fresh is not None and fresh["items"][0]["title"] == "Nvidia chips surge"

    def test_empty_news_fetch_never_overwrites_cache(self, tmp_path: Path) -> None:
        state = self._state(tmp_path)
        cache = MarketCache(state / "market" / "cache")
        cache.put("news", "SPY", {"items": [{"title": "keep me"}]}, now=NOW)

        def t(url: str, *, timeout: float = 10.0) -> tuple[int, bytes]:
            if "news.google.com" in url:
                return 200, b"<not-rss>"  # degraded feed -> []
            return 200, json.dumps(CBOE_QUOTE_BODY).encode()

        class Cfg:
            underlyings: ClassVar[list[str]] = ["SPY"]
            market_refresh_seconds = 60

        later = datetime(2026, 9, 22, 19, 5, tzinfo=ET)
        market_cycle(state, Cfg(), now=later, transport=t, force=True,
                     bars_client=_EmptyBars())
        env = cache.get_envelope("news", "SPY")
        assert env is not None and env["payload"]["items"][0]["title"] == "keep me"
        assert cache.get_envelope("bars", "SPY") is None  # empty bars not cached


class _EmptyBars:
    def get_json(self, path: str, params: dict, *, use_cache: bool = True) -> dict:
        return {"results": []}


class TestCodexM456Market:
    class Cfg:
        underlyings: ClassVar[list[str]] = ["SPY", "QQQ"]
        market_refresh_seconds = 60

    def _quote_t(self, fail: set[str] | None = None, empty: set[str] | None = None):
        def t(url: str, *, timeout: float = 10.0) -> tuple[int, bytes]:
            t.calls.append(url)  # type: ignore[attr-defined]
            if any(s in url for s in (fail or set())):
                return 503, b""
            if any(s in url for s in (empty or set())):
                return 200, json.dumps({"timestamp": "2026-09-22 22:08:55", "data": {}}).encode()
            if "news.google.com" in url:
                return 504, b""
            return 200, json.dumps(CBOE_QUOTE_BODY).encode()

        t.calls = []  # type: ignore[attr-defined]
        return t

    def test_targeted_refresh_merges_into_snapshot(self, tmp_path: Path) -> None:
        market_cycle(tmp_path, self.Cfg(), now=NOW, transport=self._quote_t())
        market_cycle(tmp_path, self.Cfg(), now=NOW, transport=self._quote_t(),
                     symbols=["SPY"], force=True, bars_client=_EmptyBars())
        doc = json.loads((tmp_path / "market.json").read_text())
        assert set(doc["symbols"]) == {"SPY", "QQQ"}

    def test_explicit_empty_watchlist_stays_empty(self, tmp_path: Path) -> None:
        market_cycle(tmp_path, self.Cfg(), now=NOW, transport=self._quote_t(), symbols=[])
        doc = json.loads((tmp_path / "market.json").read_text())
        assert doc["symbols"] == {}

    def test_total_failure_persists_errors_and_carries_last_good(self, tmp_path: Path) -> None:
        market_cycle(tmp_path, self.Cfg(), now=NOW, transport=self._quote_t())
        later = datetime(2026, 9, 22, 18, 40, tzinfo=ET)  # quote TTL expired
        market_cycle(tmp_path, self.Cfg(), now=later,
                     transport=self._quote_t(fail={"SPY", "QQQ"}))
        doc = json.loads((tmp_path / "market.json").read_text())
        assert set(doc["errors"]) == {"SPY", "QQQ"}
        assert doc["symbols"]["SPY"]["bid"] == pytest.approx(773.25)  # carried
        assert doc["last_refresh"] == NOW.isoformat()  # success clock did not move
        assert doc["last_attempt"] == later.isoformat()

    def test_empty_quote_never_overwrites_cache(self, tmp_path: Path) -> None:
        market_cycle(tmp_path, self.Cfg(), now=NOW, transport=self._quote_t())
        market_cycle(tmp_path, self.Cfg(), now=NOW, transport=self._quote_t(empty={"SPY"}),
                     force=True, symbols=["SPY"], bars_client=_EmptyBars())
        cached = MarketCache(tmp_path / "market" / "cache").get("quote", "SPY", now=NOW)
        assert cached is not None and cached["bid"] == pytest.approx(773.25)
        doc = json.loads((tmp_path / "market.json").read_text())
        assert "empty quote" in doc["errors"]["SPY"]

    def test_news_rewarm_is_bounded_and_backs_off(self, tmp_path: Path) -> None:
        syms = ["AAA", "BBB", "CCC", "DDD", "EEE"]
        cache = MarketCache(tmp_path / "market" / "cache")
        for s in syms:
            cache.put("news", s, {"items": [{"title": "old"}]}, now=NOW)
        later = datetime(2026, 9, 22, 19, 5, tzinfo=ET)  # every envelope expired

        def news_calls(t: object) -> int:
            return sum("news.google.com" in u for u in t.calls)  # type: ignore[attr-defined]

        t1 = self._quote_t()
        market_cycle(tmp_path, self.Cfg(), now=later, transport=t1, symbols=syms)
        assert news_calls(t1) == 2  # bounded per cycle (feed is failing)
        t2 = self._quote_t()
        market_cycle(tmp_path, self.Cfg(), now=later, transport=t2, symbols=syms)
        assert news_calls(t2) == 2
        assert all("AAA" not in u and "BBB" not in u
                   for u in t2.calls if "news.google.com" in u)  # failed ones back off
        env = cache.get_envelope("news", "AAA")
        assert env is not None and env["payload"]["items"][0]["title"] == "old"  # kept
