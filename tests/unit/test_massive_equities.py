from dataclasses import dataclass
from datetime import date

import pytest

from tree_options.data.massive_equities import MassiveEquityError, MassiveEquityResearchAdapter


@dataclass
class Page:
    results: tuple
    request_ids: tuple = ("request-1",)


class FakeClient:
    def __init__(self, pages=None, body=None):
        self.pages = pages or Page(())
        self.body = body or {"results": [], "request_id": "r"}
        self.calls = []

    def paginate(self, path, params=None, **kwargs):
        self.calls.append(("paginate", path, dict(params or {})))
        return self.pages

    def get_json(self, path, params=None, **kwargs):
        self.calls.append(("get", path, dict(params or {})))
        return self.body


def test_universe_explicitly_queries_names_active_on_historical_date():
    # A name may be delisted today but still returns active=true for a date on
    # which it actually traded; this avoids today's-active-list survivorship.
    client = FakeClient(Page(({"ticker": "OLD", "name": "Old Co", "active": True},)))
    adapter = MassiveEquityResearchAdapter(client)
    result = adapter.universe_as_of(as_of=date(2020, 1, 2))
    assert result.members[0].ticker == "OLD"
    _, path, params = client.calls[0]
    assert path == "/v3/reference/tickers"
    assert params["date"] == "2020-01-02"
    assert params["active"] == "true"


def test_financials_filter_future_filing_date_and_use_pit_query():
    client = FakeClient(Page((
        {"filing_date": "2024-01-10", "period_end": "2023-12-31", "x": 1},
        # Keep a future row in the fake response to prove the local fail-closed
        # filter is not delegated entirely to the provider query parameter.
        {"filing_date": "2024-02-10", "period_end": "2023-12-31", "x": 2},
    )))
    adapter = MassiveEquityResearchAdapter(client)
    rows, receipt = adapter.financials_as_of(
        endpoint="/stocks/financials/v1/income-statements",
        ticker="ABC",
        decision_date=date(2024, 1, 31),
    )
    assert [row.raw["x"] for row in rows] == [1]
    assert receipt.request_ids == ("request-1",)
    _, path, params = client.calls[0]
    assert path == "/stocks/financials/v1/income-statements"
    assert params["tickers"] == "ABC"
    assert params["filing_date.lte"] == "2024-01-31"
    assert params["sort"] == "period_end.desc"


def test_financial_missing_filing_date_fails_closed():
    client = FakeClient(Page(({"period_end": "2023-12-31"},)))
    adapter = MassiveEquityResearchAdapter(client)
    with pytest.raises(MassiveEquityError, match="filing_date"):
        adapter.financials_as_of(
            endpoint="/stocks/financials/v1/balance-sheets",
            ticker="ABC",
            decision_date=date(2024, 1, 31),
        )


def test_latest_only_ratios_endpoint_is_refused_for_historical_pit_use():
    adapter = MassiveEquityResearchAdapter(FakeClient())
    with pytest.raises(MassiveEquityError, match="latest-only"):
        adapter.financials_as_of(
            endpoint="/stocks/financials/v1/ratios",
            ticker="ABC",
            decision_date=date(2024, 1, 31),
        )


def test_grouped_daily_parses_decimal_ohlcv(static_calendar):
    client = FakeClient(body={"request_id": "abc", "results": [{"T": "A", "o": 10, "h": 12, "l": 9, "c": 11, "v": 100, "vw": 10.5, "n": 4}]})
    bars, receipt = MassiveEquityResearchAdapter(client, calendar=static_calendar).grouped_daily(session=date(2026, 1, 2))
    assert bars[0].ticker == "A" and str(bars[0].close) == "11"
    assert receipt.request_ids == ("abc",)
