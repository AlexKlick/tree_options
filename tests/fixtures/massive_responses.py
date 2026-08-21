"""Hermetic Massive/Polygon response fixtures (M4-A WS-D1).

Golden bodies captured from LIVE probes on 2026-08-21 (API key redacted at
capture time; nothing here has ever contained a secret), then TRIMMED to a
handful of records. Every retained record is byte-verbatim from the
capture — including the number formatting, which is the point: the
adapter parses JSON numbers with `parse_float=Decimal`, so `587.5` must
reach the Decimal constructor as the characters `587.5`, and a fixture
that round-tripped through Python floats would silently destroy the
property under test.

Bodies are stored as RAW JSON TEXT, not dicts, for the same reason.

Verbatim from the capture:
- `CONTRACTS_PAGE_1` / `CONTRACTS_PAGE_2` / `CONTRACTS_SINGLE_PAGE` —
  records from `/v3/reference/options/contracts?underlying_ticker=SPY&
  as_of=2025-01-31&limit=1000` (250 results + a `next_url`). Retained:
  the 375 call/put pair, the 560 call (the same contract the aggregates
  capture covers), and the 587.5 call — the fractional strike that proves
  exact decimal parsing. The page-1 `next_url` keeps the captured URL
  shape with a SHORTENED cursor (the real one is ~1 kB of opaque base64);
  the host, path and `cursor` parameter name are unchanged, and the
  captured `next_url` carried NO key parameter.
- `AGGS_DAILY` — 6 of the 24 bars from
  `/v2/aggs/ticker/O:SPY250314C00560000/range/1/day/2025-02-01/2025-03-14`.
  Chosen for coverage: the first bar, an integer-valued low (`"l":18`),
  the 2025-03-07 bar (00:00 EST, UTC-05:00) and the 2025-03-10 bar (00:00
  EDT, UTC-04:00) straddling the 2025-03-09 DST transition, and the
  expiry-day bar. `resultsCount` is re-stated to match the trim.

CONSTRUCTED (not observed; handcrafted to the captured key shapes, the
same discipline as `cboe_eod_rows.py`, and each marked `# CONSTRUCTED:`
at its definition):
- `C_ADJUSTED_90` (`shares_per_contract` 90 — an adjusted deliverable),
  `C_EUROPEAN` (a european index option), `C_MALFORMED`, the
  `CONTRACTS_ODDITIES` / `CONTRACTS_FOREIGN_NEXT_URL` /
  `CONTRACTS_ENDLESS` pages, `AGGS_EMPTY`, `RATE_LIMITED_BODY` and
  `VENDOR_ERROR_BODY`.
- `NOT_AUTHORIZED_SNAPSHOT` reproduces the probed shape — HTTP **200**
  with `status: NOT_AUTHORIZED` — using the vendor message as recorded in
  the probe brief (whose tail was elided as "...").

Also here: the fake clock and fake transport both test modules inject, so
no test in this lane opens a socket or reads a key file.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field

# ---- /v3/reference/options/contracts ---------------------------------------

C375 = (
    '{"cfi":"OCASPS","contract_type":"call","exercise_style":"american",'
    '"expiration_date":"2025-03-14","primary_exchange":"BATO",'
    '"shares_per_contract":100,"strike_price":375,'
    '"ticker":"O:SPY250314C00375000","underlying_ticker":"SPY"}'
)
C560 = (
    '{"cfi":"OCASPS","contract_type":"call","exercise_style":"american",'
    '"expiration_date":"2025-03-14","primary_exchange":"BATO",'
    '"shares_per_contract":100,"strike_price":560,'
    '"ticker":"O:SPY250314C00560000","underlying_ticker":"SPY"}'
)
C587_5 = (
    '{"cfi":"OCASPS","contract_type":"call","exercise_style":"american",'
    '"expiration_date":"2025-03-14","primary_exchange":"BATO",'
    '"shares_per_contract":100,"strike_price":587.5,'
    '"ticker":"O:SPY250314C00587500","underlying_ticker":"SPY"}'
)
P375 = (
    '{"cfi":"OPASPS","contract_type":"put","exercise_style":"american",'
    '"expiration_date":"2025-03-14","primary_exchange":"BATO",'
    '"shares_per_contract":100,"strike_price":375,'
    '"ticker":"O:SPY250314P00375000","underlying_ticker":"SPY"}'
)

# CONSTRUCTED: an adjusted deliverable (90 shares) — the corporate-action
# tell this lane exists to surface. Kept and flagged, never dropped.
C_ADJUSTED_90 = (
    '{"cfi":"OCASPS","contract_type":"call","exercise_style":"american",'
    '"expiration_date":"2025-03-14","primary_exchange":"BATO",'
    '"shares_per_contract":90,"strike_price":400,'
    '"ticker":"O:SPY1250314C00400000","underlying_ticker":"SPY"}'
)
# CONSTRUCTED: a european-style index option.
C_EUROPEAN = (
    '{"cfi":"OCEISS","contract_type":"call","exercise_style":"european",'
    '"expiration_date":"2025-03-21","primary_exchange":"CBOE",'
    '"shares_per_contract":100,"strike_price":5600,'
    '"ticker":"O:SPXW250321C05600000","underlying_ticker":"SPX"}'
)
# CONSTRUCTED: strike_price missing entirely -> refused, counted, named.
C_MALFORMED = (
    '{"cfi":"OCASPS","contract_type":"call","exercise_style":"american",'
    '"expiration_date":"2025-03-14","primary_exchange":"BATO",'
    '"shares_per_contract":100,'
    '"ticker":"O:SPY250314C00999000","underlying_ticker":"SPY"}'
)

# The captured next_url, cursor SHORTENED (see module docstring). Key-free,
# exactly as delivered.
NEXT_URL_PAGE_2 = (
    "https://api.polygon.io/v3/reference/options/contracts"
    "?cursor=YXA9JTdCJTIySUQlMjIlM0ElMjI3NzI4MjU0MzYxMDEzNjMwMjI3JTIyJTdE"
)
# CONSTRUCTED: a next_url pointing somewhere else. Following it would hand
# our API key to a third party — the client must refuse.
NEXT_URL_FOREIGN_HOST = "https://evil.example.com/v3/reference/options/contracts?cursor=ZZZ"


def _contracts_body(records: Sequence[str], *, request_id: str, next_url: str | None = None) -> str:
    tail = f',"next_url":"{next_url}"' if next_url else ""
    return (
        '{"results":['
        + ",".join(records)
        + f'],"status":"OK","request_id":"{request_id}"'
        + tail
        + "}"
    )


CONTRACTS_PAGE_1 = _contracts_body(
    (C375, C560, C587_5),
    request_id="3caaad2cbcbce3794e9efa1cca4b3950",
    next_url=NEXT_URL_PAGE_2,
)
CONTRACTS_PAGE_2 = _contracts_body((P375,), request_id="9f0f1a2b3c4d5e6f7a8b9c0d1e2f3a4b")
CONTRACTS_SINGLE_PAGE = _contracts_body(
    (C375, C560, C587_5, P375), request_id="3caaad2cbcbce3794e9efa1cca4b3950"
)
CONTRACTS_ODDITIES = _contracts_body(
    (C375, C_ADJUSTED_90, C_EUROPEAN, C_MALFORMED, C375),
    request_id="0d0d0d0d0d0d0d0d0d0d0d0d0d0d0d0d",
)
CONTRACTS_EMPTY = _contracts_body((), request_id="1111111111111111111111111111111a")
CONTRACTS_FOREIGN_NEXT_URL = _contracts_body(
    (C375,), request_id="2222222222222222222222222222222b", next_url=NEXT_URL_FOREIGN_HOST
)
# An endless-pagination body: page N always points at another page.
CONTRACTS_ENDLESS = _contracts_body(
    (C375,), request_id="3333333333333333333333333333333c", next_url=NEXT_URL_PAGE_2
)

# ---- /v2/aggs/ticker/{optionTicker}/range/1/day/{from}/{to} -----------------

AGGS_TICKER = "O:SPY250314C00560000"

BAR_2025_02_03 = (
    '{"v":25,"vw":40.08,"o":40.08,"c":40.08,"h":40.08,"l":40.08,"t":1738558800000,"n":1}'
)
# integer-valued low ("l":18) — exercises the int -> Decimal path
BAR_2025_03_04 = '{"v":211,"vw":19.7264,"o":21.6,"c":22.5,"h":22.5,"l":18,"t":1741064400000,"n":39}'
# 2025-03-07 00:00 America/New_York = 05:00Z (EST, UTC-05:00) — pre-DST
BAR_2025_03_07 = (
    '{"v":2002,"vw":15.8958,"o":16.29,"c":18.34,"h":19.65,"l":12.41,"t":1741323600000,"n":194}'
)
# 2025-03-10 00:00 America/New_York = 04:00Z (EDT, UTC-04:00) — post-DST
BAR_2025_03_10 = (
    '{"v":10405,"vw":8.3941,"o":12.71,"c":8.43,"h":13.8,"l":6.4,"t":1741579200000,"n":1883}'
)
BAR_2025_03_13 = (
    '{"v":110087,"vw":1.4883,"o":3.57,"c":0.73,"h":3.69,"l":0.5,"t":1741838400000,"n":15790}'
)
BAR_2025_03_14 = (
    '{"v":259249,"vw":1.7871,"o":0.99,"c":1.7,"h":3.93,"l":0.72,"t":1741924800000,"n":42827}'
)

AGGS_BARS = (
    BAR_2025_02_03,
    BAR_2025_03_04,
    BAR_2025_03_07,
    BAR_2025_03_10,
    BAR_2025_03_13,
    BAR_2025_03_14,
)

# The DST pair, for the epoch -> session boundary test.
DST_BAR_PAIR = (BAR_2025_03_07, BAR_2025_03_10)


def aggs_body(
    records: Sequence[str] = AGGS_BARS,
    *,
    ticker: str = AGGS_TICKER,
    results_count: int | None = None,
    request_id: str = "5ed4150a95e896d5760d30e61f63dbb4",
) -> str:
    """An aggregates body in the captured key order. `results_count` may be
    forced to a wrong value to exercise the truncation guard."""
    count = len(records) if results_count is None else results_count
    return (
        f'{{"ticker":"{ticker}","queryCount":{count},"resultsCount":{count},'
        '"adjusted":true,"results":['
        + ",".join(records)
        + f'],"status":"OK","request_id":"{request_id}","count":{count}}}'
    )


AGGS_DAILY = aggs_body()
# CONSTRUCTED: a contract with no prints in the window.
AGGS_EMPTY = (
    f'{{"ticker":"{AGGS_TICKER}","queryCount":0,"resultsCount":0,"adjusted":true,'
    '"status":"OK","request_id":"4444444444444444444444444444444d"}'
)

# ---- refusals ---------------------------------------------------------------

# Probed shape: HTTP **200** carrying a body-level refusal. A client that
# only inspects the HTTP status reads this as an empty success.
NOT_AUTHORIZED_MESSAGE = "You are not entitled to this data. Please upgrade..."
NOT_AUTHORIZED_SNAPSHOT = (
    f'{{"status":"NOT_AUTHORIZED","message":"{NOT_AUTHORIZED_MESSAGE}",'
    '"request_id":"5555555555555555555555555555555e"}'
)
# CONSTRUCTED: the free tier's 429 body (5 requests/minute exceeded).
RATE_LIMITED_BODY = (
    '{"status":"ERROR","request_id":"6666666666666666666666666666666f",'
    '"error":"You\'ve exceeded the maximum requests per minute"}'
)
# CONSTRUCTED: an OK HTTP status with an unusable vendor status.
VENDOR_ERROR_BODY = '{"status":"ERROR","error":"unknown ticker","request_id":"77777777"}'


# ---- injectable fakes (no socket, no key file, no wall clock) ---------------


@dataclass
class FakeClock:
    """A monotonic clock whose `sleep` advances it, exactly as the real
    pair behaves. Injected into `RateGovernor` so spacing math is
    asserted in microseconds of test time."""

    now: float = 1000.0
    slept: list[float] = field(default_factory=list)

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds

    @property
    def total_slept(self) -> float:
        return sum(self.slept)


@dataclass
class FakeResponse:
    status: int = 200
    body: str = ""
    headers: Mapping[str, str] = field(default_factory=dict)


@dataclass
class FakeTransport:
    """Replays queued responses and records every requested URL.

    The recorded URLs include the API key exactly as a real request would,
    which is what lets the key-leak tests assert the key reaches the wire
    and NOTHING else."""

    responses: list[FakeResponse] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)
    timeouts: list[float] = field(default_factory=list)

    def __call__(self, url: str, *, timeout: float):
        from tree_options.data.massive_client import HttpResponse

        self.urls.append(url)
        self.timeouts.append(timeout)
        if not self.responses:
            raise AssertionError(f"FakeTransport exhausted; unexpected request to {url}")
        queued = self.responses.pop(0)
        return HttpResponse(
            status=queued.status,
            body=queued.body.encode("utf-8"),
            headers=dict(queued.headers),
        )

    @property
    def calls(self) -> int:
        return len(self.urls)

    def paths(self) -> list[str]:
        return [url.split("?", 1)[0] for url in self.urls]


def transport_of(*bodies: str, status: int = 200) -> FakeTransport:
    """A transport that replays `bodies` in order, all with `status`."""
    return FakeTransport([FakeResponse(status=status, body=body) for body in bodies])


def queue(*responses: FakeResponse) -> FakeTransport:
    return FakeTransport(list(responses))


# ---- import-time self-check -------------------------------------------------

_CONTRACT_KEYS = {
    "cfi",
    "contract_type",
    "exercise_style",
    "expiration_date",
    "primary_exchange",
    "shares_per_contract",
    "strike_price",
    "ticker",
    "underlying_ticker",
}
_BAR_KEYS = {"v", "vw", "o", "c", "h", "l", "t", "n"}


def _self_check() -> Iterator[str]:
    for name, text in (
        ("CONTRACTS_PAGE_1", CONTRACTS_PAGE_1),
        ("CONTRACTS_PAGE_2", CONTRACTS_PAGE_2),
        ("CONTRACTS_SINGLE_PAGE", CONTRACTS_SINGLE_PAGE),
        ("CONTRACTS_ODDITIES", CONTRACTS_ODDITIES),
        ("CONTRACTS_EMPTY", CONTRACTS_EMPTY),
        ("AGGS_DAILY", AGGS_DAILY),
        ("AGGS_EMPTY", AGGS_EMPTY),
        ("NOT_AUTHORIZED_SNAPSHOT", NOT_AUTHORIZED_SNAPSHOT),
        ("RATE_LIMITED_BODY", RATE_LIMITED_BODY),
        ("VENDOR_ERROR_BODY", VENDOR_ERROR_BODY),
    ):
        try:
            json.loads(text)
        except ValueError as exc:  # pragma: no cover — a broken fixture
            yield f"{name} is not JSON: {exc}"
    for index, record in enumerate((C375, C560, C587_5, P375, C_ADJUSTED_90, C_EUROPEAN)):
        keys = set(json.loads(record))
        if keys != _CONTRACT_KEYS:  # pragma: no cover — a broken fixture
            yield f"contract record {index} key drift: {sorted(keys ^ _CONTRACT_KEYS)}"
    for index, record in enumerate(AGGS_BARS):
        keys = set(json.loads(record))
        if keys != _BAR_KEYS:  # pragma: no cover — a broken fixture
            yield f"bar record {index} key drift: {sorted(keys ^ _BAR_KEYS)}"


_PROBLEMS = list(_self_check())
if _PROBLEMS:  # pragma: no cover — a broken fixture
    raise AssertionError(f"massive_responses fixture drift: {_PROBLEMS}")
