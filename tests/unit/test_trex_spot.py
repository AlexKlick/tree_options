"""Touch-exit spot source (E0): which underlying price the exit machine may act on.

The paper account delivers no equity quotes, and the old fallback read the
ticker's ``close``, the PRIOR session's close, which can fake or hide a
touch. IBKR stock prices are not a touch source at all (a fresh
``ticker.time`` says nothing about the age of ``last``): the spot is the
Polygon snapshot's latest REGULAR-SESSION minute bar of the current
session, at most 20 min old. Fetches run off the exit loop, one in flight
per symbol, each with a total deadline; the tick only reads what has
arrived. No network here.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from tree_options.trex.spot import (
    FEED_LAG_S,
    MAX_INFLIGHT,
    POLYGON_MAX_AGE_S,
    POLYGON_TIMEOUT_S,
    POLYGON_TTL_S,
    SpotFeed,
    SpotReading,
    deadline_transport,
    in_touch_window,
    polygon_fetcher,
    polygon_reading,
)

ET = ZoneInfo("America/New_York")
NOW = datetime(2026, 9, 23, 14, 59, tzinfo=ET)  # a Wednesday session
NOW_S = NOW.timestamp()


def _et(h: int, m: int, day: tuple[int, int, int] = (2026, 9, 23)) -> datetime:
    return datetime(*day, h, m, tzinfo=ET)


def _snapshot(
    symbol: str = "NVDA",
    *,
    now: datetime = NOW,
    min_c: Any = Decimal("181.25"),
    min_age_s: float | None = 16 * 60,
    day_c: Any = Decimal("181.40"),
    updated_age_s: float | None = 15 * 60,
) -> dict[str, Any]:
    """A Polygon stock snapshot body as loads_exact parses it."""
    base = now.timestamp()
    ticker: dict[str, Any] = {"ticker": symbol, "lastTrade": None, "day": {"c": day_c}}
    if min_age_s is not None:
        ticker["min"] = {"t": int((base - min_age_s) * 1000), "c": min_c}
    if updated_age_s is not None:
        ticker["updated"] = int((base - updated_age_s) * 1_000_000_000)
    return {"status": "DELAYED", "request_id": "r", "ticker": ticker}


class TestPolygonReading:
    def test_minute_bar_close_and_age(self) -> None:
        r = polygon_reading(_snapshot(), "NVDA", NOW)
        assert r is not None
        assert r.px == Decimal("181.25") and r.source == "polygon"
        assert r.age_s == pytest.approx(16 * 60, abs=0.01)

    def test_too_old_is_rejected(self) -> None:
        body = _snapshot(min_age_s=POLYGON_MAX_AGE_S + 5, updated_age_s=None)
        assert polygon_reading(body, "NVDA", NOW) is None

    def test_falls_back_to_day_close_and_updated(self) -> None:
        r = polygon_reading(_snapshot(min_age_s=None), "NVDA", NOW)
        assert r is not None
        assert r.px == Decimal("181.40") and r.age_s == pytest.approx(15 * 60, abs=0.01)

    def test_integer_prices_parse_exactly(self) -> None:
        r = polygon_reading(_snapshot(min_c=181), "NVDA", NOW)
        assert r is not None and r.px == Decimal("181")

    def test_wrong_ticker_is_rejected(self) -> None:
        assert polygon_reading(_snapshot("AMD"), "NVDA", NOW) is None

    @pytest.mark.parametrize(
        "body",
        [
            {},
            {"ticker": None},
            {"ticker": {"ticker": "NVDA"}},
            {"ticker": {"ticker": "NVDA", "min": {"t": 0, "c": Decimal("1")}}},
            {"ticker": {"ticker": "NVDA", "min": {"t": 1, "c": True}}},
        ],
    )
    def test_unusable_bodies_are_rejected(self, body: dict[str, Any]) -> None:
        assert polygon_reading(body, "NVDA", NOW) is None

    def test_future_timestamps_are_rejected(self) -> None:
        body = _snapshot(min_age_s=-300, updated_age_s=None)
        assert polygon_reading(body, "NVDA", NOW) is None


class TestSessionWindow:
    """Codex P1: snapshot data includes premarket prints. At 09:30 a 09:14
    bar below the strike passed the age check and fired a touch exit."""

    def test_a_premarket_bar_is_not_a_touch_input(self) -> None:
        at = _et(9, 30)
        body = _snapshot(now=at, min_age_s=16 * 60, updated_age_s=None)  # the 09:14 bar
        assert polygon_reading(body, "NVDA", at) is None

    def test_the_opening_bar_counts(self) -> None:
        at = _et(9, 46)
        body = _snapshot(now=at, min_age_s=16 * 60, updated_age_s=None)  # the 09:30 bar
        assert polygon_reading(body, "NVDA", at) is not None

    def test_a_post_close_bar_is_not_a_touch_input(self) -> None:
        at = _et(16, 14)
        body = _snapshot(now=at, min_age_s=14 * 60, updated_age_s=None)  # the 16:00 bar
        assert polygon_reading(body, "NVDA", at) is None
        last = _snapshot(now=at, min_age_s=15 * 60, updated_age_s=None)  # the 15:59 bar
        assert polygon_reading(last, "NVDA", at) is not None

    def test_early_close_is_respected(self) -> None:
        at = _et(13, 10, (2026, 11, 27))  # 13:00 close
        body = _snapshot(now=at, min_age_s=5 * 60, updated_age_s=None)  # the 13:05 bar
        assert polygon_reading(body, "NVDA", at) is None

    def test_no_session_no_spot(self) -> None:
        at = _et(12, 0, (2026, 9, 26))  # a Saturday
        body = _snapshot(now=at, min_age_s=60, updated_age_s=None)
        assert polygon_reading(body, "NVDA", at) is None

    def test_touch_window_follows_the_calendar(self) -> None:
        assert not in_touch_window(_et(9, 40))  # the delayed feed can't deliver yet
        assert in_touch_window(datetime.fromtimestamp(_et(9, 30).timestamp() + FEED_LAG_S, ET))
        assert in_touch_window(_et(15, 59))
        assert not in_touch_window(_et(16, 1))
        assert not in_touch_window(_et(13, 30, (2026, 11, 27)))  # early close
        assert not in_touch_window(_et(12, 0, (2026, 9, 26)))  # Saturday


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class FakePolygon:
    """Injectable fetcher: records (symbol, timeout); may raise."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, float]] = []
        self.raises: dict[str, Exception] = {}

    def __call__(self, symbol: str, timeout: float) -> dict[str, Any]:
        self.calls.append((symbol, timeout))
        if symbol in self.raises:
            raise self.raises[symbol]
        return _snapshot(symbol)


class Deferred:
    """spawn() that queues jobs instead of running them (a worker pool that
    has not got to them yet): proves the tick never waits for a fetch."""

    def __init__(self) -> None:
        self.jobs: list[Any] = []

    def __call__(self, job: Any) -> None:
        self.jobs.append(job)

    def run(self) -> None:
        jobs, self.jobs = self.jobs, []
        for job in jobs:
            job()


def _feed(
    poly: FakePolygon | None = None, clock: FakeClock | None = None
) -> tuple[SpotFeed, FakePolygon, FakeClock, Deferred]:
    poly = poly or FakePolygon()
    clock = clock or FakeClock()
    spawn = Deferred()
    return SpotFeed(poly, spawn=spawn, monotonic=clock), poly, clock, spawn


class TestSpotFeed:
    def test_the_tick_never_waits_for_the_network(self) -> None:
        """Codex P1: a socket timeout bounds each blocking op, not a request;
        a trickling response held the exit loop. Fetches now run elsewhere."""
        feed, poly, _, spawn = _feed()
        assert feed.readings(["NVDA"], NOW) == {}  # dispatched, not awaited
        assert poly.calls == [] and len(spawn.jobs) == 1
        spawn.run()
        out = feed.readings(["NVDA"], NOW)
        assert out["NVDA"].px == Decimal("181.25")
        assert poly.calls == [("NVDA", POLYGON_TIMEOUT_S)]

    def test_one_request_in_flight_per_symbol(self) -> None:
        feed, _, clock, spawn = _feed()
        feed.readings(["NVDA"], NOW)
        clock.t += POLYGON_TTL_S * 5  # long overdue, but the first is still out
        feed.readings(["NVDA"], NOW)
        assert len(spawn.jobs) == 1

    def test_ttl_between_requests(self) -> None:
        feed, poly, clock, spawn = _feed()
        feed.readings(["NVDA"], NOW)
        spawn.run()
        clock.t += POLYGON_TTL_S - 5
        feed.readings(["NVDA"], NOW)
        assert spawn.jobs == []
        clock.t += 10
        feed.readings(["NVDA"], NOW)
        spawn.run()
        assert len(poly.calls) == 2

    def test_no_symbol_starves(self) -> None:
        """Codex P2: a fixed order behind failing symbols never reached G."""
        poly = FakePolygon()
        for s in "ABCDEF":
            poly.raises[s] = TimeoutError("slow")
        feed, _, _, spawn = _feed(poly)
        feed.readings(list("ABCDEFG"), NOW)
        spawn.run()
        assert {s for s, _ in poly.calls} == set("ABCDEFG")

    def test_in_flight_cap_rotates_oldest_first(self) -> None:
        poly = FakePolygon()
        feed, _, clock, spawn = _feed(poly)
        symbols = [f"S{i}" for i in range(MAX_INFLIGHT + 3)]
        feed.readings(symbols, NOW)
        assert len(spawn.jobs) == MAX_INFLIGHT
        spawn.run()
        clock.t += POLYGON_TTL_S + 1
        feed.readings(symbols, NOW)
        first = {s for s, _ in poly.calls}
        spawn.run()
        later = [s for s, _ in poly.calls][len(first) :]
        assert set(symbols) - first <= set(later[:3])  # never-tried go first

    def test_cached_reading_ages_out(self) -> None:
        feed, _, _, spawn = _feed()
        feed.readings(["NVDA"], NOW)
        spawn.run()
        later = datetime.fromtimestamp(NOW_S + POLYGON_MAX_AGE_S, ET)
        assert feed.readings(["NVDA"], later) == {}

    def test_a_failing_symbol_never_costs_the_others(self) -> None:
        poly = FakePolygon()
        poly.raises["AMD"] = TimeoutError("read timed out")
        feed, _, _, spawn = _feed(poly)
        feed.readings(["AMD", "NVDA"], NOW)
        spawn.run()
        assert set(feed.readings(["AMD", "NVDA"], NOW)) == {"NVDA"}

    def test_a_failed_refresh_keeps_the_last_good_bar(self) -> None:
        poly = FakePolygon()
        feed, _, clock, spawn = _feed(poly)
        feed.readings(["NVDA"], NOW)
        spawn.run()
        clock.t += POLYGON_TTL_S + 1
        poly.raises["NVDA"] = OSError("connection reset")
        feed.readings(["NVDA"], NOW)
        spawn.run()
        assert feed.readings(["NVDA"], NOW)["NVDA"].px == Decimal("181.25")

    def test_only_asked_symbols_are_served(self) -> None:
        feed, _, _, spawn = _feed()
        feed.readings(["NVDA", "AMD"], NOW)
        spawn.run()
        assert set(feed.readings(["NVDA"], NOW)) == {"NVDA"}

    def test_no_fetcher_no_spots(self) -> None:
        assert SpotFeed(None).readings(["NVDA"], NOW) == {}


class TrickleResponse:
    """urlopen() result that sends one byte per read, forever."""

    status = 200

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.closed = False
        self.headers: dict[str, str] = {}

    def read(self, amt: int = -1) -> bytes:
        self.clock.t += 0.5  # each read just under the socket timeout
        return b"{"

    def __enter__(self) -> TrickleResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        self.closed = True


class TestDeadlineTransport:
    def test_a_trickling_response_hits_the_total_deadline(self) -> None:
        from tree_options.data.massive_client import MassiveTransportError

        clock = FakeClock()
        resp = TrickleResponse(clock)
        transport = deadline_transport(5.0, opener=lambda *a, **k: resp, monotonic=clock)
        with pytest.raises(MassiveTransportError, match="deadline"):
            transport("https://api.polygon.io/x?apiKey=SECRETKEY", timeout=3.0)
        assert clock.t - 1000.0 <= 5.5 and resp.closed

    def test_a_normal_response_is_returned(self) -> None:
        clock = FakeClock()

        class Once(TrickleResponse):
            def __init__(self) -> None:
                super().__init__(clock)
                self.parts = [b'{"status":', b'"OK"}', b""]

            def read(self, amt: int = -1) -> bytes:
                return self.parts.pop(0)

        transport = deadline_transport(5.0, opener=lambda *a, **k: Once(), monotonic=clock)
        resp = transport("https://api.polygon.io/x", timeout=3.0)
        assert resp.status == 200 and resp.body == b'{"status":"OK"}'


class TestPolygonFetcher:
    def test_uncached_single_attempt_snapshot_request(self) -> None:
        from tree_options.data.massive_client import HttpResponse, loads_exact

        seen: list[tuple[str, float]] = []

        def transport(url: str, *, timeout: float) -> HttpResponse:
            seen.append((url, timeout))
            body = b'{"status":"OK","ticker":{"ticker":"NVDA","min":{"t":1,"c":1.5}}}'
            return HttpResponse(200, body)

        fetch = polygon_fetcher(api_key=lambda: "SECRETKEY", transport=transport)
        first = fetch("NVDA", 2.5)
        fetch("NVDA", 3.0)
        assert len(seen) == 2  # never served from a cache
        url, timeout = seen[0]
        assert "/v2/snapshot/locale/us/markets/stocks/tickers/NVDA?" in url
        assert timeout == 2.5 and seen[1][1] == 3.0
        assert first == loads_exact(
            b'{"status":"OK","ticker":{"ticker":"NVDA","min":{"t":1,"c":1.5}}}'
        )

    def test_errors_never_carry_the_key(self) -> None:
        from tree_options.data.massive_client import HttpResponse

        def transport(url: str, *, timeout: float) -> HttpResponse:
            return HttpResponse(503, b"upstream " + url.encode())

        fetch = polygon_fetcher(api_key=lambda: "SECRETKEY", transport=transport)
        with pytest.raises(Exception) as err:
            fetch("NVDA", 1.0)
        assert "SECRETKEY" not in str(err.value)

    def test_rejects_non_symbols(self) -> None:
        fetch = polygon_fetcher(api_key=lambda: "k", transport=lambda *a, **k: None)  # type: ignore[arg-type,return-value]
        with pytest.raises(ValueError):
            fetch("../v3/reference", 1.0)


@dataclass
class FakeTicker:
    time: datetime | None = None
    last: float = math.nan
    bid: float = math.nan
    ask: float = math.nan
    close: float = math.nan
    marketDataType: int = 1


class TestIbkrIsNotATouchSource:
    """Codex P1: ``ticker.time`` moves on every bid/ask/size tick, so a fresh
    ticker can carry an old ``last``. The paper account has no equity
    quotes anyway: IBKR stock prices never reach the engine."""

    def test_even_a_fresh_looking_last_yields_no_spot(self) -> None:
        from datetime import date
        from types import SimpleNamespace

        from tree_options.trex.ibkr import IbkrTrex, _Subscription
        from tree_options.trex.plan import PutSpread

        ib = IbkrTrex()
        stock = SimpleNamespace(conId=1)
        ib._spots["NVDA"] = stock
        ticker = FakeTicker(time=datetime.fromtimestamp(NOW_S - 1, UTC), last=150.0, close=150.0)
        ib._md[1] = _Subscription(stock, ticker, owners=1)
        spread = PutSpread(
            id="nvda-oct",
            underlying="NVDA",
            entry_date=date(2026, 9, 18),
            expiry=date(2026, 10, 16),
            long_strike="185",
            short_strike="150",
            quantity=5,
            limit_cap="0.50",
            exit_deadline=date(2026, 10, 9),
        )
        snap = ib.snapshot([spread], NOW)
        assert dict(snap.spots) == {} and dict(snap.spot_sources) == {}


def test_reading_serializes_without_floats_for_money() -> None:
    r = SpotReading(Decimal("181.25"), "polygon", NOW, 912.345)
    assert r.to_json() == {
        "px": "181.25",
        "source": "polygon",
        "as_of": NOW.isoformat(),
        "age_s": 912.3,
    }
