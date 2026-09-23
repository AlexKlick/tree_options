"""Touch-exit spot source (E0): which underlying price the exit machine may act on.

The paper account delivers no equity quotes, and the old fallback read the
ticker's ``close``, the PRIOR session's close, which can fake or hide a
touch. A spot is accepted only when its data age is known and small: an
IBKR ``last``/mid whose ticker updated within 120 s, else the Polygon
snapshot (15 min delayed) when its minute bar is at most 20 min old. The
Polygon lookup is bounded (TTL, per-call timeout, per-tick budget) and a
failure costs one symbol one tick, never the exit loop. No network here.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from tree_options.trex.spot import (
    IBKR_MAX_AGE_S,
    POLYGON_MAX_AGE_S,
    POLYGON_TIMEOUT_S,
    POLYGON_TTL_S,
    TICK_BUDGET_S,
    SpotReading,
    SpotResolver,
    ibkr_reading,
    polygon_fetcher,
    polygon_reading,
)

ET = ZoneInfo("America/New_York")
NOW = datetime(2026, 9, 23, 14, 59, tzinfo=ET)
NOW_S = NOW.timestamp()


@dataclass
class FakeTicker:
    time: datetime | None = None
    last: float = math.nan
    bid: float = math.nan
    ask: float = math.nan
    close: float = math.nan
    marketDataType: int = 1


def _utc_ago(seconds: float) -> datetime:
    return datetime.fromtimestamp(NOW_S - seconds, UTC)


def _snapshot(
    symbol: str = "NVDA",
    *,
    min_c: Any = Decimal("181.25"),
    min_age_s: float | None = 16 * 60,
    day_c: Any = Decimal("181.40"),
    updated_age_s: float | None = 15 * 60,
) -> dict[str, Any]:
    """A Polygon stock snapshot body as loads_exact parses it."""
    ticker: dict[str, Any] = {"ticker": symbol, "lastTrade": None, "day": {"c": day_c}}
    if min_age_s is not None:
        ticker["min"] = {"t": int((NOW_S - min_age_s) * 1000), "c": min_c}
    if updated_age_s is not None:
        ticker["updated"] = int((NOW_S - updated_age_s) * 1_000_000_000)
    return {"status": "DELAYED", "request_id": "r", "ticker": ticker}


class TestIbkrReading:
    def test_fresh_last_is_accepted(self) -> None:
        r = ibkr_reading(FakeTicker(time=_utc_ago(30), last=181.5), NOW)
        assert r is not None
        assert r.px == Decimal("181.5") and r.source == "ibkr"
        assert r.age_s == pytest.approx(30)
        assert r.as_of.tzinfo is not None

    def test_mid_when_no_last(self) -> None:
        r = ibkr_reading(FakeTicker(time=_utc_ago(5), bid=181.0, ask=181.5), NOW)
        assert r is not None and r.px == Decimal("181.25")

    def test_close_is_never_a_spot(self) -> None:
        """The prior session's close can fake or hide a touch."""
        assert ibkr_reading(FakeTicker(time=_utc_ago(5), close=150.0), NOW) is None

    def test_stale_ticker_is_rejected(self) -> None:
        stale = FakeTicker(time=_utc_ago(IBKR_MAX_AGE_S + 1), last=181.5)
        assert ibkr_reading(stale, NOW) is None

    def test_no_timestamp_is_rejected(self) -> None:
        assert ibkr_reading(FakeTicker(time=None, last=181.5), NOW) is None

    def test_junk_quotes_are_rejected(self) -> None:
        # IB sends -1 for an absent side; a crossed book is not a price
        assert ibkr_reading(FakeTicker(time=_utc_ago(5), bid=-1.0, ask=181.0), NOW) is None
        assert ibkr_reading(FakeTicker(time=_utc_ago(5), bid=182.0, ask=181.0), NOW) is None
        assert ibkr_reading(FakeTicker(time=_utc_ago(5), last=0.0), NOW) is None

    @pytest.mark.parametrize("frozen", [2, 4])
    def test_frozen_data_is_rejected(self, frozen: int) -> None:
        """Frozen = the last value from when the market closed."""
        t = FakeTicker(time=_utc_ago(5), last=181.5, marketDataType=frozen)
        assert ibkr_reading(t, NOW) is None

    def test_delayed_data_reports_its_true_age(self) -> None:
        t = FakeTicker(time=_utc_ago(10), last=181.5, marketDataType=3)
        r = ibkr_reading(t, NOW)
        assert r is not None and r.age_s == pytest.approx(10 + 15 * 60)

    def test_missing_ticker(self) -> None:
        assert ibkr_reading(None, NOW) is None


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


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class FakePolygon:
    """Injectable fetcher: records (symbol, timeout); may cost time or raise."""

    def __init__(self, clock: FakeClock, cost_s: float = 0.2, *, overrun: bool = False) -> None:
        self.clock = clock
        self.cost_s = cost_s
        self.overrun = overrun  # urllib's timeout is per socket op: a call can overrun it
        self.calls: list[tuple[str, float]] = []
        self.raises: dict[str, Exception] = {}
        self.bodies: dict[str, dict[str, Any]] = {}

    def __call__(self, symbol: str, timeout: float) -> dict[str, Any]:
        self.calls.append((symbol, timeout))
        self.clock.t += self.cost_s if self.overrun else min(self.cost_s, timeout)
        if symbol in self.raises:
            raise self.raises[symbol]
        return self.bodies.get(symbol) or _snapshot(symbol)


def _ibkr(px: str = "181.50", age: float = 5) -> SpotReading:
    return SpotReading(Decimal(px), "ibkr", NOW, age)


class TestResolver:
    def test_fresh_ibkr_wins_and_polygon_is_not_called(self) -> None:
        clock = FakeClock()
        poly = FakePolygon(clock)
        res = SpotResolver(poly, monotonic=clock)
        out = res.resolve(["NVDA"], {"NVDA": _ibkr()}, NOW)
        assert out["NVDA"].source == "ibkr" and poly.calls == []

    def test_polygon_fills_in_when_ibkr_is_blind(self) -> None:
        clock = FakeClock()
        poly = FakePolygon(clock)
        out = SpotResolver(poly, monotonic=clock).resolve(["NVDA"], {"NVDA": None}, NOW)
        assert out["NVDA"].source == "polygon" and out["NVDA"].px == Decimal("181.25")
        assert poly.calls == [("NVDA", POLYGON_TIMEOUT_S)]

    def test_no_fetcher_means_ibkr_only(self) -> None:
        assert SpotResolver(None).resolve(["NVDA"], {}, NOW) == {}

    def test_ttl_caches_per_symbol(self) -> None:
        clock = FakeClock()
        poly = FakePolygon(clock)
        res = SpotResolver(poly, monotonic=clock)
        res.resolve(["NVDA"], {}, NOW)
        clock.t += POLYGON_TTL_S - 5
        again = res.resolve(["NVDA"], {}, NOW)
        assert len(poly.calls) == 1 and again["NVDA"].source == "polygon"
        clock.t += 10
        res.resolve(["NVDA"], {}, NOW)
        assert len(poly.calls) == 2

    def test_cached_reading_ages_out(self) -> None:
        """A cached bar is judged by its DATA age at each tick, not fetch time."""
        clock = FakeClock()
        poly = FakePolygon(clock)
        res = SpotResolver(poly, monotonic=clock)
        res.resolve(["NVDA"], {}, NOW)
        later = datetime.fromtimestamp(NOW_S + POLYGON_MAX_AGE_S, ET)
        assert res.resolve(["NVDA"], {}, later) == {}

    def test_a_failing_symbol_never_costs_the_others(self) -> None:
        clock = FakeClock()
        poly = FakePolygon(clock)
        poly.raises["AMD"] = TimeoutError("read timed out")
        out = SpotResolver(poly, monotonic=clock).resolve(["AMD", "NVDA"], {}, NOW)
        assert set(out) == {"NVDA"}

    def test_a_failed_refresh_keeps_the_last_good_bar(self) -> None:
        clock = FakeClock()
        poly = FakePolygon(clock)
        res = SpotResolver(poly, monotonic=clock)
        res.resolve(["NVDA"], {}, NOW)
        clock.t += POLYGON_TTL_S + 1
        poly.raises["NVDA"] = OSError("connection reset")
        out = res.resolve(["NVDA"], {}, NOW)
        assert out["NVDA"].px == Decimal("181.25")

    def test_budget_bounds_the_tick(self) -> None:
        """Slow fetches can't stall the exit loop: the per-tick budget caps
        the total and each call's timeout shrinks to what is left."""
        clock = FakeClock()
        poly = FakePolygon(clock, cost_s=POLYGON_TIMEOUT_S)  # every call times out
        syms = ["A", "B", "C", "D"]
        SpotResolver(poly, monotonic=clock).resolve(syms, {}, NOW)
        assert [s for s, _ in poly.calls] == ["A", "B"]
        assert sum(t for _, t in poly.calls) <= TICK_BUDGET_S

    def test_partial_budget_shrinks_the_timeout(self) -> None:
        clock = FakeClock()
        poly = FakePolygon(clock, cost_s=4.5, overrun=True)
        SpotResolver(poly, monotonic=clock).resolve(["A", "B", "C"], {}, NOW)
        assert poly.calls[1][1] == pytest.approx(TICK_BUDGET_S - 4.5)
        assert [s for s, _ in poly.calls] == ["A", "B"]  # budget spent: C waits a tick


class TestPolygonFetcher:
    def test_uncached_single_attempt_snapshot_request(self, tmp_path: Path) -> None:
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


class TestIbkrSnapshot:
    """IbkrTrex.snapshot puts only ACCEPTED readings into Snapshot.spots."""

    def _ib(self, ticker: FakeTicker, resolver: SpotResolver | None = None) -> Any:
        from tree_options.trex.ibkr import IbkrTrex

        ib = IbkrTrex(spot_resolver=resolver)
        key = object()
        ib._spots["NVDA"] = key
        ib._tickers[key] = ticker
        return ib

    def _spreads(self) -> list[Any]:
        from datetime import date

        from tree_options.trex.plan import PutSpread

        return [
            PutSpread(
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
        ]

    def test_prior_close_never_reaches_the_engine(self) -> None:
        snap = self._ib(FakeTicker(time=_utc_ago(5), close=150.0)).snapshot(self._spreads(), NOW)
        assert dict(snap.spots) == {} and dict(snap.spot_sources) == {}

    def test_fresh_ibkr_last(self) -> None:
        snap = self._ib(FakeTicker(time=_utc_ago(5), last=184.0)).snapshot(self._spreads(), NOW)
        assert snap.spots == {"NVDA": Decimal("184.0")}
        assert snap.spot_sources["NVDA"].source == "ibkr"

    def test_polygon_fallback_through_the_resolver(self) -> None:
        clock = FakeClock()
        res = SpotResolver(FakePolygon(clock), monotonic=clock)
        snap = self._ib(FakeTicker(close=150.0), res).snapshot(self._spreads(), NOW)
        assert snap.spots == {"NVDA": Decimal("181.25")}
        assert snap.spot_sources["NVDA"].source == "polygon"


def test_reading_serializes_without_floats_for_money() -> None:
    r = SpotReading(Decimal("181.25"), "polygon", NOW, 912.345)
    assert r.to_json() == {
        "px": "181.25",
        "source": "polygon",
        "as_of": NOW.isoformat(),
        "age_s": 912.3,
    }
