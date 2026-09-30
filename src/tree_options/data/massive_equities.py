"""Typed equity-research semantics over TREX's existing Massive client.

This module deliberately does not implement HTTP, authentication, caching,
pagination or rate limiting. The reviewed TREX ``MassiveClient`` already owns
those concerns. Any structurally compatible client can be injected in tests.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from tree_options.time.calendar import SessionCalendar
from tree_options.time.sessions import SESSION_TIMEZONE


class MassiveEquityError(ValueError):
    pass


class PageLike(Protocol):
    results: Sequence[Mapping[str, Any]]
    request_ids: Sequence[str]


class MassiveLike(Protocol):
    def get_json(
        self, path: str, params: Mapping[str, Any] | None = None, *, use_cache: bool = True
    ) -> Mapping[str, Any]: ...

    def paginate(
        self,
        path: str,
        params: Mapping[str, Any] | None = None,
        *,
        results_key: str = "results",
        max_pages: int | None = None,
        use_cache: bool = True,
    ) -> PageLike: ...


@dataclass(frozen=True, slots=True)
class ProviderReceipt:
    endpoint: str
    request_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class UniverseMember:
    ticker: str
    name: str
    active: bool
    primary_exchange: str | None
    currency: str | None


@dataclass(frozen=True, slots=True)
class UniverseSnapshot:
    as_of: date
    members: tuple[UniverseMember, ...]
    receipt: ProviderReceipt


@dataclass(frozen=True, slots=True)
class DailyBar:
    ticker: str
    session: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    vwap: Decimal | None
    transactions: int | None


@dataclass(frozen=True, slots=True)
class FinancialRecord:
    ticker: str
    filing_date: date
    period_end: date | None
    raw: Mapping[str, Any]


def _decimal(value: Any, *, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise MassiveEquityError(f"{field} is not Decimal-compatible: {value!r}") from error
    if not result.is_finite():
        raise MassiveEquityError(f"{field} must be finite")
    return result


def _date(value: Any, *, field: str) -> date:
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        raise MassiveEquityError(f"{field} must be an ISO date")
    try:
        return date.fromisoformat(value[:10])
    except ValueError as error:
        raise MassiveEquityError(f"{field} must be an ISO date") from error


def _request_ids_from_json(body: Mapping[str, Any]) -> tuple[str, ...]:
    request_id = body.get("request_id")
    return (request_id,) if isinstance(request_id, str) and request_id else ()


class MassiveEquityResearchAdapter:
    def __init__(self, client: MassiveLike, *, calendar: SessionCalendar | None = None) -> None:
        self.client = client
        self.calendar = calendar

    def universe_as_of(
        self,
        *,
        as_of: date,
        market: str = "stocks",
        active: bool | None = True,
    ) -> UniverseSnapshot:
        """Historical reference universe using an explicit provider date.

        ``date=<historical date>, active=true`` asks for names actively traded
        on that historical date, which preserves names that were tradable then
        but delisted later. That is the default research universe here. This is
        not the same thing as historical S&P 500 membership; index membership
        requires a separately sourced point-in-time universe manifest.
        """

        if not isinstance(as_of, date):
            raise MassiveEquityError("as_of date is required")
        params: dict[str, Any] = {
            "market": market,
            "date": as_of.isoformat(),
            "limit": 1000,
            "sort": "ticker",
            "order": "asc",
        }
        if active is not None:
            params["active"] = str(active).lower()
        page = self.client.paginate("/v3/reference/tickers", params)
        members: list[UniverseMember] = []
        for raw in page.results:
            ticker = raw.get("ticker")
            if not isinstance(ticker, str) or not ticker:
                raise MassiveEquityError("ticker reference row missing ticker")
            members.append(
                UniverseMember(
                    ticker=ticker,
                    name=str(raw.get("name") or ticker),
                    active=bool(raw.get("active", False)),
                    primary_exchange=(
                        str(raw["primary_exchange"]) if raw.get("primary_exchange") else None
                    ),
                    currency=(str(raw["currency_name"]) if raw.get("currency_name") else None),
                )
            )
        return UniverseSnapshot(
            as_of=as_of,
            members=tuple(sorted(members, key=lambda row: row.ticker)),
            receipt=ProviderReceipt(
                endpoint="/v3/reference/tickers", request_ids=tuple(page.request_ids)
            ),
        )

    def grouped_daily(
        self, *, session: date, adjusted: bool = True
    ) -> tuple[tuple[DailyBar, ...], ProviderReceipt]:
        if self.calendar is None:
            raise MassiveEquityError("daily bars require a TREX session calendar")
        self.calendar.ordinal(session)
        path = f"/v2/aggs/grouped/locale/us/market/stocks/{session.isoformat()}"
        body = self.client.get_json(path, {"adjusted": str(adjusted).lower()})
        rows = body.get("results", ())
        if not isinstance(rows, list | tuple):
            raise MassiveEquityError("grouped daily results must be a sequence")
        bars = tuple(
            sorted(
                (self._parse_bar(row, session=session) for row in rows),
                key=lambda row: row.ticker,
            )
        )
        return bars, ProviderReceipt(endpoint=path, request_ids=_request_ids_from_json(body))

    def daily_bars(
        self,
        *,
        ticker: str,
        start: date,
        end: date,
        adjusted: bool = True,
    ) -> tuple[tuple[DailyBar, ...], ProviderReceipt]:
        if self.calendar is None:
            raise MassiveEquityError("daily bars require a TREX session calendar")
        if start > end:
            raise MassiveEquityError("start must be <= end")
        path = f"/v2/aggs/ticker/{ticker}/range/1/day/{start.isoformat()}/{end.isoformat()}"
        page = self.client.paginate(
            path,
            {
                "adjusted": str(adjusted).lower(),
                "sort": "asc",
                "limit": 50000,
            },
        )
        bars: list[DailyBar] = []
        for raw in page.results:
            raw_session = raw.get("session") or raw.get("date")
            if raw_session is not None:
                session = _date(raw_session, field="session")
            else:
                timestamp_ms = raw.get("t")
                if timestamp_ms is None:
                    raise MassiveEquityError("aggregate row lacks session timestamp")
                try:
                    instant = datetime.fromtimestamp(float(timestamp_ms) / 1000.0, tz=UTC)
                except (TypeError, ValueError, OSError) as error:
                    raise MassiveEquityError(
                        "aggregate row lacks a usable session date/timestamp"
                    ) from error
                session = instant.astimezone(SESSION_TIMEZONE).date()
            self.calendar.ordinal(session)
            if not start <= session <= end:
                raise MassiveEquityError("provider bar outside requested range")
            bars.append(self._parse_bar({**raw, "T": ticker}, session=session))
        return tuple(bars), ProviderReceipt(endpoint=path, request_ids=tuple(page.request_ids))

    def financials_as_of(
        self,
        *,
        endpoint: str,
        ticker: str,
        decision_date: date,
        limit: int = 1000,
    ) -> tuple[tuple[FinancialRecord, ...], ProviderReceipt]:
        """Fetch statement records available no later than ``decision_date``.

        Only the three historical statement endpoints are accepted here. Massive's
        current ``/stocks/financials/v1/ratios`` endpoint is a latest-only screen
        (one current record per ticker), so using it in a historical run would
        silently introduce look-ahead. Historical value ratios must instead be
        reconstructed from point-in-time statements plus point-in-time market
        data, or supplied by another explicitly historical provider.

        We request ``filing_date.lte`` server-side and still hard-filter every
        returned row locally. ``filing_date`` is the availability boundary;
        ``period_end`` is only the accounting period described by the row.
        """

        statement_endpoints = {
            "/stocks/financials/v1/income-statements",
            "/stocks/financials/v1/balance-sheets",
            "/stocks/financials/v1/cash-flow-statements",
        }
        if endpoint == "/stocks/financials/v1/ratios":
            raise MassiveEquityError(
                "Massive ratios is latest-only and cannot satisfy historical point-in-time research; "
                "reconstruct ratios from PIT statements/market data or use a historical provider"
            )
        if endpoint not in statement_endpoints:
            raise MassiveEquityError(
                "financial endpoint must be one of the three historical Massive statement endpoints"
            )
        page = self.client.paginate(
            endpoint,
            {
                "tickers": ticker,
                "filing_date.lte": decision_date.isoformat(),
                "limit": limit,
                "sort": "period_end.desc",
            },
        )
        accepted: list[FinancialRecord] = []
        for raw in page.results:
            if "filing_date" not in raw or raw.get("filing_date") in (None, ""):
                raise MassiveEquityError(
                    f"{ticker} financial row lacks filing_date; refusing PIT use"
                )
            filing = _date(raw.get("filing_date"), field="filing_date")
            if filing > decision_date:
                continue
            period_raw = raw.get("period_end") or raw.get("end_date")
            period_end = _date(period_raw, field="period_end") if period_raw else None
            accepted.append(
                FinancialRecord(
                    ticker=ticker,
                    filing_date=filing,
                    period_end=period_end,
                    raw=raw,
                )
            )
        accepted.sort(key=lambda row: (row.filing_date, row.period_end or date.min), reverse=True)
        return tuple(accepted), ProviderReceipt(
            endpoint=endpoint, request_ids=tuple(page.request_ids)
        )

    @staticmethod
    def _parse_bar(raw: Mapping[str, Any], *, session: date) -> DailyBar:
        ticker = raw.get("T") or raw.get("ticker")
        if not isinstance(ticker, str) or not ticker:
            raise MassiveEquityError("aggregate row missing ticker")
        bar = DailyBar(
            ticker=ticker,
            session=session,
            open=_decimal(raw.get("o"), field="open"),
            high=_decimal(raw.get("h"), field="high"),
            low=_decimal(raw.get("l"), field="low"),
            close=_decimal(raw.get("c"), field="close"),
            volume=_decimal(raw.get("v"), field="volume"),
            vwap=_decimal(raw.get("vw"), field="vwap") if raw.get("vw") is not None else None,
            transactions=int(raw["n"]) if raw.get("n") is not None else None,
        )
        if (
            min(bar.open, bar.high, bar.low, bar.close) <= 0
            or bar.low > bar.high
            or not bar.low <= bar.open <= bar.high
            or not bar.low <= bar.close <= bar.high
        ):
            raise MassiveEquityError(f"invalid OHLC for {ticker} on {session}")
        if bar.volume < 0:
            raise MassiveEquityError(f"negative volume for {ticker} on {session}")
        return bar


__all__ = [
    "DailyBar",
    "FinancialRecord",
    "MassiveEquityError",
    "MassiveEquityResearchAdapter",
    "ProviderReceipt",
    "UniverseMember",
    "UniverseSnapshot",
]
