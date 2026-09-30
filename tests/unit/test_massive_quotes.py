"""Fresh quote facts retain provider time semantics; never infer from receipt time."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from tree_options.data.massive_quotes import MassiveQuoteError, fresh_equity_quote
from tree_options.trex.massive_paper_quotes import canary_quote_source

NOW = datetime(2026, 9, 30, 16, 0, 0, 123456, tzinfo=UTC)
NS = 1790784000123456000


class Fake:
    def __init__(self, body=None):
        self.body = body or {
            "status": "OK",
            "request_id": "request-1",
            "results": {
                "T": "AAPL",
                "p": Decimal("99.99"),
                "P": Decimal("100.01"),
                "s": 100,
                "S": 200,
                "t": NS - 900,
                "y": NS - 1000,
                "q": 12,
            },
        }
        self.calls = []

    def get_json(self, path, params=None, *, use_cache=True):
        self.calls.append((path, params, use_cache))
        return self.body


def test_exact_times_prices_and_provider_receipt_without_cache():
    client = Fake()
    quote = fresh_equity_quote(client, "AAPL", now=NOW)
    assert client.calls == [("/v2/last/nbbo/AAPL", None, False)]
    assert quote.sip_received_ns == NS - 900
    assert quote.exchange_event_ns == NS - 1000
    assert quote.sip_received_at == datetime(2026, 9, 30, 16, 0, 0, 123455, tzinfo=UTC)
    assert quote.request_id == "request-1" and quote.sequence_number == 12
    assert quote.bid == Decimal("99.99") and quote.ask == Decimal("100.01")
    assert quote.bid_size == 100 and quote.ask_size == 200
    projected = canary_quote_source(client, clock=lambda: NOW)("AAPL")
    assert projected.valid_at(NOW)
    assert projected.observed_at == quote.sip_received_at
    assert str(NS - 900) in projected.source_id and "request-1" in projected.source_id


@pytest.mark.parametrize(
    "fields",
    [
        {"t": None},
        {"t": NS + 1},
        {"t": NS - 15000000001},
        {"y": NS},
        {"y": NS - 15000000001},
        {"t": True},
        {"t": Decimal(NS)},
        {"P": Decimal("99")},
        {"p": float("99.99")},
        {"p": Decimal("NaN")},
        {"s": 0},
        {"S": True},
        {"T": "MSFT"},
        {"q": -1},
    ],
)
def test_invalid_facts_fail_closed(fields):
    client = Fake()
    client.body["results"].update(fields)
    with pytest.raises(MassiveQuoteError):
        fresh_equity_quote(client, "AAPL", now=NOW)


@pytest.mark.parametrize("fields", [{"status": "DELAYED"}, {"request_id": ""}, {"results": []}])
def test_incomplete_or_delayed_response_is_refused(fields):
    client = Fake()
    client.body.update(fields)
    with pytest.raises(MassiveQuoteError):
        fresh_equity_quote(client, "AAPL", now=NOW)


def test_missing_exchange_timestamp_and_symbols_never_repaired():
    client = Fake()
    del client.body["results"]["y"]
    with pytest.raises(MassiveQuoteError):
        fresh_equity_quote(client, "AAPL", now=NOW)
    for symbol in ("../secret", "AAPL?apiKey=secret", "aapl", "O:AAPL", ""):
        fresh = Fake()
        with pytest.raises(MassiveQuoteError):
            fresh_equity_quote(fresh, symbol, now=NOW)
        assert not fresh.calls


def test_clock_is_read_after_wire_latency():
    client = Fake()
    quote_source = canary_quote_source(
        client, clock=lambda: datetime(2026, 9, 30, 16, 1, tzinfo=UTC)
    )
    with pytest.raises(MassiveQuoteError):
        quote_source("AAPL")
