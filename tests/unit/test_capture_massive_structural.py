"""M4-B: the live-capture bridge between WS-D1's client and WS-D2's inspector.

Hermetic. There is no network here, no API key is read, and no live cache is
touched: every test drives `scripts/capture_massive_structural.py` through an
injected transport and a temporary cache directory.

The bridge exists because the two workstreams do not compose on their own —
`MassiveClient.paginate()` flattens pages, while the inspector wants the
vendor's own page bodies so it can judge completeness for itself. What is
worth testing is therefore exactly the seam:

* the vendor's number TOKENS survive into the capture file (a re-serialised
  envelope would launder `587.5` through a float),
* stopping at the page cap LEAVES `next_url` in place, which is the signal
  the inspector reads as an incomplete capture,
* the budget PRE-CHARGES each wire call's worst case and refunds what the
  call did not spend, so it caps wire requests rather than calls,
* deepening is uniform across captures or does not happen, so the
  per-`as_of` columns stay comparable,
* the run's exit code and manifest are honest: a dead key, a crash, a
  nothing-captured run, and a partial run each say so, and the manifest
  always lands on disk,
* nothing written carries the secret.

The routing vendor double lives here rather than in
`tests/fixtures/massive_responses.py` (WS-D1's) or
`massive_structural_sample.py` (WS-D2's): this is a third lane and it does
not edit another lane's shared file.
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit
from zoneinfo import ZoneInfo

import pytest

from tests.conftest import REPO_ROOT
from tree_options.data.massive_client import (
    BackoffPolicy,
    HttpResponse,
    MassiveClient,
    RateGovernor,
    cache_key_for,
)
from tree_options.data.massive_manifest import (
    load_massive_capture_manifest,
    verify_massive_capture_manifest,
)

sys.path.insert(0, str(REPO_ROOT / "scripts"))

import capture_massive_structural as cap  # type: ignore[import-not-found]  # scripts/
import inspect_structural_coverage as structural  # type: ignore[import-not-found]  # scripts/

ET = ZoneInfo("America/New_York")
KEY = "TEST-KEY-NEVER-REAL"

AS_OF = date(2025, 3, 14)
NEAR = date(2025, 4, 17)  # 34 DTE — inside the 30-60 protocol band
FAR = date(2025, 5, 9)  # 56 DTE — also inside the band
OUT = date(2025, 3, 21)  # 7 DTE — outside it

# The exact fractional strike from the live capture: it must reach the
# inspector as Decimal("587.5") and never as a float.
FRACTIONAL_STRIKE = "587.5"


def et_midnight_ms(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=ET).timestamp() * 1000)


def contract_json(
    underlying: str, expiration: date, strike: str, kind: str, *, spc: int = 100
) -> str:
    """A vendor result row as TEXT, so its numeric tokens are byte-exact."""
    cents = int(Decimal(strike) * 1000)
    ticker = f"O:{underlying}{expiration:%y%m%d}{kind[0].upper()}{cents:08d}"
    return (
        f'{{"cfi":"OCASPS","contract_type":"{kind}","exercise_style":"american",'
        f'"expiration_date":"{expiration.isoformat()}","primary_exchange":"BATO",'
        f'"shares_per_contract":{spc},"strike_price":{strike},'
        f'"ticker":"{ticker}","underlying_ticker":"{underlying}"}}'
    )


def contracts_page(rows: list[str], *, request_id: str, next_url: str | None = None) -> str:
    tail = f',"next_url":"{next_url}"' if next_url else ""
    return f'{{"results":[{",".join(rows)}],"status":"OK","request_id":"{request_id}"{tail}}}'


def bars_page(ticker: str, sessions: list[date], *, close: str = "40.08") -> str:
    rows = [
        f'{{"v":25,"vw":{close},"o":{close},"c":{close},"h":{close},"l":{close},'
        f'"t":{et_midnight_ms(s)},"n":1}}'
        for s in sessions
    ]
    return (
        f'{{"ticker":"{ticker}","queryCount":{len(rows)},"resultsCount":{len(rows)},'
        f'"adjusted":true,"results":[{",".join(rows)}],"status":"OK","request_id":"agg"}}'
    )


def spot_page(ticker: str, sessions: list[date], close: str = "560") -> str:
    rows = [
        f'{{"v":1,"vw":1,"o":1,"c":{close},"h":1,"l":1,"t":{et_midnight_ms(s)},"n":1}}'
        for s in sessions
    ]
    return (
        f'{{"ticker":"{ticker}","resultsCount":{len(rows)},"adjusted":true,'
        f'"results":[{",".join(rows)}],"status":"OK","request_id":"a"}}'
    )


NOT_AUTHORIZED = (
    '{"status":"NOT_AUTHORIZED","message":"You are not entitled to this data. Please upgrade..."}'
)


class RoutingVendor:
    """A URL-routed vendor double that records every LIVE call it served."""

    def __init__(
        self, routes: dict[str, str], *, default: str | None = None, status: int = 200
    ) -> None:
        self.routes = routes
        self.default = default
        self.status = status
        self.calls: list[str] = []

    def __call__(self, url: str, *, timeout: float) -> HttpResponse:
        parts = urlsplit(url)
        query = dict(parse_qsl(parts.query))
        assert query.pop("apiKey", None) == KEY, "the key is appended at request time"
        canonical = parts.path
        if query:
            canonical += "?" + urlencode(sorted(query.items()))
        self.calls.append(canonical)
        body = self.routes.get(canonical, self.default)
        if body is None:  # pragma: no cover - a test asked for an unrouted URL
            raise AssertionError(f"unrouted: {canonical}")
        return HttpResponse(self.status, body.encode("utf-8"), {})


def make_client(tmp_path: Path, vendor: RoutingVendor, **kwargs: Any) -> MassiveClient:
    """A client with spacing disabled — the governor is WS-D1's tested unit,
    and paying 12s per request here would buy nothing."""
    return MassiveClient(
        api_key=KEY,
        transport=vendor,
        cache_dir=tmp_path / "cache",
        governor=RateGovernor(None),
        **kwargs,
    )


def contracts_url(underlying: str, as_of: date, *, cursor: str | None = None) -> str:
    params = {
        "as_of": as_of.isoformat(),
        "limit": str(cap.CONTRACTS_LIMIT),
        "underlying_ticker": underlying,
    }
    if cursor is not None:
        params["cursor"] = cursor
    return cap.CONTRACTS_PATH + "?" + urlencode(sorted(params.items()))


def aggs_url(ticker: str, start: date, end: date, *, adjusted: bool = True) -> str:
    """Aggregate URL as the capture builds it.

    Option bars are requested `adjusted=true`; the equity spot proxy is
    requested `adjusted=false`, because a to-today-adjusted close is the
    wrong denominator for an `as_of`-dated strike ladder (see
    `capture_spot_proxy`). The two differ on the wire, so the routes here
    must differ too — routing on the literal URL is what keeps that honest.
    """
    path = cap.AGGS_PATH_TEMPLATE.format(
        ticker=ticker, start=start.isoformat(), end=end.isoformat()
    )
    return f"{path}?adjusted={'true' if adjusted else 'false'}"


def spot_url(ticker: str, start: date, end: date) -> str:
    return aggs_url(ticker, start, end, adjusted=False)


def _one_master_routes() -> dict[str, str]:
    """A complete one-page SPY master, its spot close, and one bar series."""
    return {
        spot_url("SPY", AS_OF, AS_OF): spot_page("SPY", [AS_OF]),
        contracts_url("SPY", AS_OF): contracts_page(
            [
                contract_json("SPY", NEAR, "560", "call"),
                contract_json("SPY", NEAR, "560", "put"),
                contract_json("SPY", FAR, "560", "call"),
            ],
            request_id="r1",
        ),
        aggs_url("O:SPY250417C00560000", AS_OF, NEAR): bars_page(
            "O:SPY250417C00560000", [date(2025, 3, 17)]
        ),
    }


# ---- verbatim capture -------------------------------------------------------


def test_the_vendor_number_token_survives_into_the_capture(tmp_path: Path) -> None:
    """`587.5` must reach the inspector as an exact Decimal.

    Re-serialising a decoded envelope would route the strike through a float
    and the exactness claim would quietly become an approximation claim.
    """
    row = contract_json("SPY", NEAR, FRACTIONAL_STRIKE, "call")
    vendor = RoutingVendor({contracts_url("SPY", AS_OF): contracts_page([row], request_id="r1")})
    client = make_client(tmp_path, vendor)
    capture = cap.capture_master(
        client, "SPY", AS_OF, budget=cap.Budget(limit=5), max_pages=cap.PAGES_PER_MASTER
    )

    text = cap.master_envelope(capture)
    assert f'"strike_price":{FRACTIONAL_STRIKE}' in text  # the raw token, not 587.5000000001

    decoded = structural.decode_payload(text, source="probe")
    strike = decoded["pages"][0]["results"][0]["strike_price"]
    assert isinstance(strike, Decimal)
    assert strike == Decimal(FRACTIONAL_STRIKE)


def test_envelope_supplies_the_as_of_the_vendor_body_omits(tmp_path: Path) -> None:
    row = contract_json("SPY", NEAR, "560", "call")
    vendor = RoutingVendor({contracts_url("SPY", AS_OF): contracts_page([row], request_id="r1")})
    client = make_client(tmp_path, vendor)
    capture = cap.capture_master(client, "SPY", AS_OF, budget=cap.Budget(limit=5), max_pages=4)

    payload = structural.decode_payload(cap.master_envelope(capture), source="probe")
    assert payload["as_of"] == AS_OF.isoformat()
    assert payload["underlying_ticker"] == "SPY"
    assert payload["provider"] == "massive-polygon-free/1"

    master = structural.parse_contract_master(payload, source="SPY.json")
    assert master.as_of == AS_OF
    assert master.capture_complete is True


# ---- truncation is visible, not silent --------------------------------------


def test_the_page_cap_leaves_next_url_so_truncation_is_visible(tmp_path: Path) -> None:
    """Stopping at the cap must NOT tidy up after itself.

    The dangling `next_url` is the entire mechanism by which the inspector
    reports `capture_complete=false`; removing it would turn a short
    universe into a confident wrong number.
    """
    first = contracts_url("SPY", AS_OF)
    nxt = f"https://api.polygon.io{first}&cursor=C2"
    vendor = RoutingVendor(
        {
            first: contracts_page(
                [contract_json("SPY", NEAR, "560", "call")], request_id="r1", next_url=nxt
            ),
            contracts_url("SPY", AS_OF, cursor="C2"): contracts_page(
                [contract_json("SPY", NEAR, "570", "call")], request_id="r2", next_url=nxt
            ),
        }
    )
    client = make_client(tmp_path, vendor)
    capture = cap.capture_master(client, "SPY", AS_OF, budget=cap.Budget(limit=9), max_pages=2)

    assert capture.truncated is True
    assert capture.pending_next_url is True
    assert len(capture.pages) == 2

    payload = structural.decode_payload(cap.master_envelope(capture), source="probe")
    master = structural.parse_contract_master(payload, source="SPY.json")
    assert master.capture_complete is False, "the inspector must see the capture as short"
    assert master.pages == 2


def test_a_complete_chain_is_reported_complete(tmp_path: Path) -> None:
    first = contracts_url("SPY", AS_OF)
    nxt = f"https://api.polygon.io{first}&cursor=C2"
    vendor = RoutingVendor(
        {
            first: contracts_page(
                [contract_json("SPY", NEAR, "560", "call")], request_id="r1", next_url=nxt
            ),
            contracts_url("SPY", AS_OF, cursor="C2"): contracts_page(
                [contract_json("SPY", NEAR, "570", "call")], request_id="r2"
            ),
        }
    )
    client = make_client(tmp_path, vendor)
    capture = cap.capture_master(client, "SPY", AS_OF, budget=cap.Budget(limit=9), max_pages=4)

    assert (capture.truncated, capture.pending_next_url) == (False, False)
    master = structural.parse_contract_master(
        structural.decode_payload(cap.master_envelope(capture), source="p"), source="SPY.json"
    )
    assert master.capture_complete is True
    assert master.contracts[0].strike == Decimal("560")


# ---- the budget -------------------------------------------------------------


def test_a_cache_hit_is_never_charged_to_the_budget(tmp_path: Path) -> None:
    """Re-runs must be free, which is what makes the cache safe to leave on."""
    url = contracts_url("SPY", AS_OF)
    vendor = RoutingVendor(
        {url: contracts_page([contract_json("SPY", NEAR, "560", "call")], request_id="r1")}
    )
    client = make_client(tmp_path, vendor)
    budget = cap.Budget(limit=5)

    cap.capture_master(client, "SPY", AS_OF, budget=budget, max_pages=4)
    assert budget.spent == 1
    assert len(vendor.calls) == 1

    cap.capture_master(client, "SPY", AS_OF, budget=budget, max_pages=4)
    assert budget.spent == 1, "the second walk was served from cache"
    assert len(vendor.calls) == 1, "no second wire call"
    assert client.stats.cache_hits == 1


def test_the_budget_precharges_the_worst_case_and_refunds(tmp_path: Path) -> None:
    """The budget caps WIRE REQUESTS, not calls.

    Every wire call is pre-charged its worst case (`max_attempts` — one
    request plus every retry the backoff might burn) BEFORE the wire is
    touched, so a call that cannot pay its worst case never goes out; the
    unused attempts are refunded the moment the call returns.
    """
    url = contracts_url("SPY", AS_OF)
    backoff = BackoffPolicy(max_attempts=4, initial_seconds=0.0, multiplier=1.0)

    # A budget that cannot cover ONE call's worst case refuses before the wire.
    vendor = RoutingVendor(
        {url: contracts_page([contract_json("SPY", NEAR, "560", "call")], request_id="r1")}
    )
    client = MassiveClient(
        api_key=KEY,
        transport=vendor,
        cache_dir=tmp_path / "cache-a",
        governor=RateGovernor(None),
        backoff=backoff,
    )
    budget = cap.Budget(limit=3)
    with pytest.raises(cap.BudgetExhausted, match="needs 4"):
        cap.capture_master(client, "SPY", AS_OF, budget=budget, max_pages=4)
    assert vendor.calls == [], "a call that cannot pay its worst case never touches the wire"
    assert budget.spent == 0

    # A 2-wire-request call (one 429, then the vendor relents) nets to 2:
    # 4 pre-charged, 2 refunded.
    body = contracts_page([contract_json("SPY", NEAR, "570", "call")], request_id="r2")
    attempts: list[str] = []

    def flaky(request_url: str, *, timeout: float) -> HttpResponse:
        attempts.append(request_url)
        if len(attempts) < 2:
            return HttpResponse(429, b'{"status":"ERROR","message":"rate limit"}', {})
        return HttpResponse(200, body.encode("utf-8"), {})

    retried = MassiveClient(
        api_key=KEY,
        transport=flaky,
        cache_dir=tmp_path / "cache-b",
        governor=RateGovernor(None),
        backoff=backoff,
    )
    refunded = cap.Budget(limit=8)
    cap.capture_master(retried, "SPY", AS_OF, budget=refunded, max_pages=4)

    assert len(attempts) == 2 and retried.stats.requests == 2
    assert refunded.spent == 2, "requests_charged is 2: four charged, two refunded"
    assert refunded.log[:4] == [url] * 4
    assert refunded.log[4:] == [f"{url} [refund 2]"]
    assert refunded.available == 6


def test_a_self_heal_refetch_is_charged_and_labeled(tmp_path: Path) -> None:
    """A "hit" whose cached body no longer decodes refetches from the wire.

    The cost is charged POST-HOC (bounded at `max_attempts`) because
    pre-detecting it would mean decoding every cached body twice — see the
    Budget docstring. The budget log names it for what it is.
    """
    url = contracts_url("SPY", AS_OF)
    vendor = RoutingVendor(
        {url: contracts_page([contract_json("SPY", NEAR, "560", "call")], request_id="r1")}
    )
    client = make_client(tmp_path, vendor)
    # A torn cache entry: present on disk, but it no longer decodes.
    key = cache_key_for(
        cap.CONTRACTS_PATH,
        {
            "underlying_ticker": "SPY",
            "as_of": AS_OF.isoformat(),
            "limit": cap.CONTRACTS_LIMIT,
        },
    )
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True)
    (cache_dir / f"{key}.json").write_text("torn write: not JSON", encoding="utf-8")

    budget = cap.Budget(limit=9)
    capture = cap.capture_master(client, "SPY", AS_OF, budget=budget, max_pages=4)

    assert len(capture.pages) == 1 and capture.error is None
    assert client.stats.cache_self_heals == 1
    assert len(vendor.calls) == 1
    assert budget.spent == 1, "the refetch is charged after the fact"
    assert any("[self-heal refetch]" in entry for entry in budget.log)


def test_budget_exhaustion_keeps_what_was_already_captured(tmp_path: Path) -> None:
    first = contracts_url("SPY", AS_OF)
    nxt = f"https://api.polygon.io{first}&cursor=C2"
    vendor = RoutingVendor(
        {
            first: contracts_page(
                [contract_json("SPY", NEAR, "560", "call")], request_id="r1", next_url=nxt
            ),
            contracts_url("SPY", AS_OF, cursor="C2"): contracts_page(
                [contract_json("SPY", NEAR, "570", "call")], request_id="r2"
            ),
        }
    )
    client = make_client(tmp_path, vendor)
    # Enough to pre-charge ONE call (max_attempts=4) but not a second.
    budget = cap.Budget(limit=4)

    capture = cap.capture_master(client, "SPY", AS_OF, budget=budget, max_pages=4)
    assert len(capture.pages) == 1
    assert capture.truncated is True and capture.pending_next_url is True
    assert budget.spent == 1


def test_an_exhausted_budget_with_nothing_captured_raises(tmp_path: Path) -> None:
    vendor = RoutingVendor({}, default=contracts_page([], request_id="r1"))
    client = make_client(tmp_path, vendor)
    budget = cap.Budget(limit=0)

    with pytest.raises(cap.BudgetExhausted):
        cap.capture_master(client, "SPY", AS_OF, budget=budget, max_pages=4)
    assert vendor.calls == [], "the budget is checked BEFORE the wire is touched"


def test_reservation_protects_the_bar_allocation(tmp_path: Path) -> None:
    budget = cap.Budget(limit=10)
    budget.reserve(6)
    assert budget.available == 4
    for index in range(4):
        budget.charge(f"call-{index}")
    with pytest.raises(cap.BudgetExhausted):
        budget.charge("one too many")
    budget.release(6)
    assert budget.available == 6


# ---- uniform-depth deepening ------------------------------------------------


def _endless(underlying: str, as_of: date, cursor: str | None = None) -> str:
    """A vendor whose chain always continues — with ADVANCING cursors, so a
    deeper round is always a fresh wire request rather than a cache hit."""
    step = 1 if cursor is None else int(cursor.removeprefix("NEXT")) + 1
    return contracts_page(
        [contract_json(underlying, NEAR, "560", "call")],
        request_id="r",
        next_url=f"https://api.polygon.io{contracts_url(underlying, as_of, cursor=f'NEXT{step}')}",
    )


def test_deepening_is_all_or_nothing_across_captures(tmp_path: Path) -> None:
    """A partial round would make page depth vary by (name, as_of).

    Universe size and births/deaths would then partly measure how the budget
    happened to fall rather than the contract universe, so the leftover is
    deliberately left unspent.
    """
    routes = {
        contracts_url("SPY", AS_OF): _endless("SPY", AS_OF),
        contracts_url("SPY", AS_OF, cursor="NEXT1"): _endless("SPY", AS_OF, cursor="NEXT1"),
        contracts_url("TSLA", AS_OF): _endless("TSLA", AS_OF),
        contracts_url("TSLA", AS_OF, cursor="NEXT1"): _endless("TSLA", AS_OF, cursor="NEXT1"),
    }
    vendor = RoutingVendor(routes)
    client = make_client(tmp_path, vendor)
    budget = cap.Budget(limit=8)
    masters = tmp_path / "masters"
    masters.mkdir()

    captures = [
        cap.capture_master(client, name, AS_OF, budget=budget, max_pages=1)
        for name in ("SPY", "TSLA")
    ]
    assert budget.spent == 2 and all(c.pending_next_url for c in captures)

    # One request left cannot pre-charge a round of two: spend nothing.
    budget.limit = 3
    notes = cap._deepen(client, captures, masters, budget=budget)
    assert budget.spent == 2, "a partial round must not be run"
    assert [len(c.pages) for c in captures] == [1, 1]
    assert any("uniform-depth stop" in note for note in notes)
    assert "1 request(s) left unspent" in notes[-1]

    # Eight free slots (two calls' worst case) fund a full round, and both
    # deepen together.
    budget.limit = 10
    cap._deepen(client, captures, masters, budget=budget)
    assert budget.spent == 4
    assert [len(c.pages) for c in captures] == [2, 2]


# ---- spot proxy -------------------------------------------------------------


def test_spot_proxy_keeps_only_as_of_sessions_and_is_exponent_free(tmp_path: Path) -> None:
    as_ofs = [date(2025, 3, 14), date(2025, 9, 15)]
    other = date(2025, 3, 17)
    vendor = RoutingVendor(
        {
            spot_url("SPY", as_ofs[0], as_ofs[1]): (
                '{"ticker":"SPY","resultsCount":3,"adjusted":true,"results":['
                f'{{"v":1,"vw":1,"o":1,"c":560,"h":1,"l":1,"t":{et_midnight_ms(as_ofs[0])},"n":1}},'
                f'{{"v":1,"vw":1,"o":1,"c":999,"h":1,"l":1,"t":{et_midnight_ms(other)},"n":1}},'
                f'{{"v":1,"vw":1,"o":1,"c":661.25,"h":1,"l":1,"t":{et_midnight_ms(as_ofs[1])},"n":1}}'
                '],"status":"OK","request_id":"a"}'
            )
        }
    )
    client = make_client(tmp_path, vendor)
    proxy, notes = cap.capture_spot_proxy(client, ["SPY"], as_ofs, budget=cap.Budget(limit=5))

    assert proxy == {"SPY": {"2025-03-14": "560", "2025-09-15": "661.25"}}
    assert "560" in json.dumps(proxy) and "5.6E+2" not in json.dumps(proxy)
    assert notes == []


def test_a_missing_as_of_session_is_named_not_filled(tmp_path: Path) -> None:
    as_ofs = [date(2025, 3, 14), date(2025, 3, 15)]  # the 15th is a Saturday
    vendor = RoutingVendor(
        {
            spot_url("SPY", as_ofs[0], as_ofs[1]): (
                '{"ticker":"SPY","resultsCount":1,"adjusted":true,"results":['
                f'{{"v":1,"vw":1,"o":1,"c":560,"h":1,"l":1,"t":{et_midnight_ms(as_ofs[0])},"n":1}}'
                '],"status":"OK","request_id":"a"}'
            )
        }
    )
    client = make_client(tmp_path, vendor)
    proxy, notes = cap.capture_spot_proxy(client, ["SPY"], as_ofs, budget=cap.Budget(limit=5))

    assert proxy == {"SPY": {"2025-03-14": "560"}}
    assert any("2025-03-15" in note for note in notes)


def test_a_not_entitled_spot_proxy_is_recorded_not_fatal(tmp_path: Path) -> None:
    """Losing an optional metric must not lose the whole capture."""
    as_ofs = [date(2025, 3, 14), date(2025, 9, 15)]
    vendor = RoutingVendor({spot_url("SPY", as_ofs[0], as_ofs[1]): NOT_AUTHORIZED})
    client = make_client(tmp_path, vendor)
    proxy, notes = cap.capture_spot_proxy(client, ["SPY"], as_ofs, budget=cap.Budget(limit=5))

    assert proxy == {}
    assert len(notes) == 1
    assert "MassiveNotEntitledError" in notes[0]


def test_a_malformed_spot_row_is_skipped_and_counted_not_fatal(tmp_path: Path) -> None:
    """A row without a usable (t, c) pair is a note, never a crash."""
    as_ofs = [AS_OF]
    vendor = RoutingVendor(
        {
            spot_url("SPY", as_ofs[0], as_ofs[0]): (
                '{"ticker":"SPY","resultsCount":2,"adjusted":true,"results":['
                '{"v":1,"vw":1,"o":1,"c":560,"h":1,"l":1,"n":1},'  # no "t"
                f'{{"v":1,"vw":1,"o":1,"c":"560","h":1,"l":1,'
                f'"t":{et_midnight_ms(as_ofs[0])},"n":1}}'  # c is text, not a price
                '],"status":"OK","request_id":"a"}'
            )
        }
    )
    client = make_client(tmp_path, vendor)
    proxy, notes = cap.capture_spot_proxy(client, ["SPY"], as_ofs, budget=cap.Budget(limit=5))

    assert proxy == {}
    assert any("2 malformed row(s) skipped" in note for note in notes)


def test_a_float_close_is_refused_rather_than_coerced() -> None:
    with pytest.raises(ValueError, match="refusing to coerce float"):
        cap._as_decimal(560.1, "SPY.c")


# ---- bar selection ----------------------------------------------------------


def _master_from(rows: list[str], underlying: str, as_of: date) -> cap.MasterCapture:
    page = structural.decode_payload(contracts_page(rows, request_id="r"), source="p")
    return cap.MasterCapture(
        underlying=underlying,
        as_of=as_of,
        pages=[cap.CapturedPage(body=page, text="{}", from_cache=False)],
    )


def test_bar_picks_are_at_the_money_against_the_spot_proxy() -> None:
    rows = [contract_json("SPY", NEAR, k, "call") for k in ("500", "560", "620")]
    rows += [contract_json("SPY", NEAR, k, "put") for k in ("500", "560", "620")]
    rows += [contract_json("SPY", FAR, k, "call") for k in ("500", "560", "620")]
    rows += [contract_json("SPY", OUT, "560", "call")]  # 7 DTE: outside the band
    master = _master_from(rows, "SPY", AS_OF)

    picks, notes = cap.choose_bar_contracts(
        [master], {"SPY": {AS_OF.isoformat(): "558.90"}}, wanted=6
    )

    assert notes == []
    assert [t for t, _, _ in picks] == [
        "O:SPY250417C00560000",
        "O:SPY250417P00560000",
        "O:SPY250509C00560000",
    ], "ATM strike on the near band expiry (call+put) and the far one (call)"
    assert all(start == AS_OF for _, start, _ in picks)
    assert [e for _, _, e in picks] == [NEAR, NEAR, FAR]


def test_without_a_spot_proxy_the_median_strike_is_used_and_said_so() -> None:
    rows = [contract_json("SPY", NEAR, k, "call") for k in ("500", "560", "620")]
    master = _master_from(rows, "SPY", AS_OF)

    picks, notes = cap.choose_bar_contracts([master], {}, wanted=6)

    assert [t for t, _, _ in picks] == ["O:SPY250417C00560000"]
    assert any("no spot proxy" in note for note in notes)


def test_a_single_expiry_band_does_not_pay_for_the_same_contract_twice() -> None:
    """`band[0]` and `band[-1]` collide when the band holds one expiry.

    Without the guard the near-call and far-call targets resolve to the same
    ticker, and the bar sample would silently come back one series short of
    the quota that was budgeted for.
    """
    rows = [contract_json("SPY", NEAR, k, "call") for k in ("500", "560")]
    master = _master_from(rows, "SPY", AS_OF)

    picks, notes = cap.choose_bar_contracts([master], {"SPY": {AS_OF.isoformat(): "560"}}, wanted=6)

    assert [t for t, _, _ in picks] == ["O:SPY250417C00560000"], "no duplicate pick"
    assert any("already picked" in note and "1 expiry" in note for note in notes)


def test_an_underlying_with_no_band_expiry_is_named_not_dropped() -> None:
    master = _master_from([contract_json("SPY", OUT, "560", "call")], "SPY", AS_OF)
    picks, notes = cap.choose_bar_contracts([master], {}, wanted=6)

    assert picks == []
    assert any("no 30-60 DTE expiry" in note for note in notes)


def test_malformed_master_rows_are_skipped_and_counted_not_fatal() -> None:
    """Rows that cannot yield an expiration or a strike are notes, not crashes."""
    rows = [
        contract_json("SPY", NEAR, "560", "call"),
        '{"ticker":"O:SPY250417C00560000","contract_type":"call"}',  # no expiration_date
        '{"expiration_date":"2025-04-17","contract_type":"call"}',  # no strike or ticker
    ]
    master = _master_from(rows, "SPY", AS_OF)

    picks, notes = cap.choose_bar_contracts([master], {"SPY": {AS_OF.isoformat(): "560"}}, wanted=6)

    assert [t for t, _, _ in picks] == ["O:SPY250417C00560000"]
    assert any("2 malformed row(s) skipped" in note for note in notes)


def test_a_contract_with_no_prints_is_not_written_as_an_empty_series(tmp_path: Path) -> None:
    """The inspector refuses an empty series, so an empty one must never be
    written: no prints is a note, not a capture."""
    ticker = "O:SPY250417C00560000"
    vendor = RoutingVendor(
        {
            aggs_url(ticker, AS_OF, NEAR): (
                f'{{"ticker":"{ticker}","resultsCount":0,"adjusted":true,'
                '"results":[],"status":"OK","request_id":"a"}'
            )
        }
    )
    client = make_client(tmp_path, vendor)
    files, notes = cap.capture_bars(client, [(ticker, AS_OF, NEAR)], budget=cap.Budget(limit=5))

    assert files == []
    assert any("no prints" in note for note in notes)


# ---- secret hygiene ---------------------------------------------------------


def test_no_capture_or_manifest_can_carry_the_api_key(tmp_path: Path) -> None:
    """The cache redacts on write, so the verbatim text this bridge reads back
    is already key-free even if the vendor echoed the key at us."""
    leaky = (
        '{"results":[' + contract_json("SPY", NEAR, "560", "call") + '],"status":"OK",'
        f'"request_id":"r1","next_url":"https://api.polygon.io/v3/x?apiKey={KEY}"}}'
    )
    vendor = RoutingVendor({contracts_url("SPY", AS_OF): leaky})
    client = make_client(tmp_path, vendor)
    capture = cap.capture_master(client, "SPY", AS_OF, budget=cap.Budget(limit=4), max_pages=1)

    text = cap.master_envelope(capture)
    assert KEY not in text
    assert "REDACTED" in text
    for cached in (tmp_path / "cache").glob("*.json"):
        assert KEY not in cached.read_text(encoding="utf-8")
        assert KEY not in cached.name


def test_the_bridge_source_carries_no_key_and_no_key_file_read() -> None:
    """This lane must not learn the key by any route but WS-D1's loader.

    Asserted against the CODE, not the module docstring — the docstring is
    allowed (and expected) to name where the key comes from.
    """
    path = REPO_ROOT / "scripts" / "capture_massive_structural.py"
    source = path.read_text(encoding="utf-8")
    code = source.split('"""', 2)[2]  # everything after the module docstring

    assert "polygon.key" not in source, "the key file path is massive_client's business"
    assert "api" + "Key" not in code  # split so this guard plants no needle
    assert "POLYGON" + "_API_KEY" not in code, "the key is read by load_api_key, not here"
    assert "load_api_key" not in code, "the key never enters this module's namespace"
    assert "client_from_environment" in code, "the key stays inside WS-D1's client"


# ---- crash safety, fail-fast, and honest exit codes -------------------------


def test_a_fully_failed_run_exits_nonzero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every route 404s: nothing lands, the run says so, exit 4."""
    monkeypatch.setattr(cap, "UNDERLYINGS", ("SPY",))
    monkeypatch.setattr(cap, "AS_OF_DATES", (AS_OF,))
    monkeypatch.setattr(cap, "BARS_WANTED", 0)
    vendor = RoutingVendor({}, default='{"status":"ERROR","message":"gone"}', status=404)
    monkeypatch.setattr(
        cap, "client_from_environment", lambda **kwargs: make_client(tmp_path, vendor)
    )

    out = tmp_path / "captures"
    assert cap.main(["--out-dir", str(out), "--budget", "9"]) == 4

    manifest = load_massive_capture_manifest(out / "capture_manifest.json")
    verify_massive_capture_manifest(manifest, out, capture_version=cap.CAPTURE_VERSION)
    assert len(manifest.masters) == 1
    assert "MassiveApiError" in (manifest.masters[0].error or ""), "all masters errored"
    assert manifest.files == (), "no capture file was written"
    assert len(vendor.calls) == 2, "the spot probe and the master sweep both tried once"


def test_a_dead_key_fails_fast_with_one_probe_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HTTP 401 is terminal: one probe request, then the run stops with exit 3.

    The manifest still lands (the finally writes it), but the master sweep
    never starts.
    """
    monkeypatch.setattr(cap, "UNDERLYINGS", ("SPY",))
    monkeypatch.setattr(cap, "AS_OF_DATES", (AS_OF,))
    monkeypatch.setattr(cap, "BARS_WANTED", 1)
    vendor = RoutingVendor({}, default="{}", status=401)
    monkeypatch.setattr(
        cap, "client_from_environment", lambda **kwargs: make_client(tmp_path, vendor)
    )

    out = tmp_path / "captures"
    assert cap.main(["--out-dir", str(out), "--budget", "9"]) == 3
    assert vendor.calls == [spot_url("SPY", AS_OF, AS_OF)], "exactly one probe request"

    manifest = load_massive_capture_manifest(out / "capture_manifest.json")
    verify_massive_capture_manifest(manifest, out, capture_version=cap.CAPTURE_VERSION)
    assert manifest.masters == (), "no master sweep happened"


def test_a_crashed_run_still_writes_the_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unexpected crash mid-sweep must not strand what already landed.

    `AssertionError` is the one signal the client never swallows or retries,
    so it is how the vendor double dies here.
    """
    monkeypatch.setattr(cap, "UNDERLYINGS", ("SPY", "TSLA"))
    monkeypatch.setattr(cap, "AS_OF_DATES", (AS_OF,))
    monkeypatch.setattr(cap, "BARS_WANTED", 0)
    routes = {
        spot_url("SPY", AS_OF, AS_OF): spot_page("SPY", [AS_OF]),
        # No TSLA close on the as_of: a note that must survive the crash.
        spot_url("TSLA", AS_OF, AS_OF): spot_page("TSLA", []),
        contracts_url("SPY", AS_OF): contracts_page(
            [contract_json("SPY", NEAR, "560", "call")], request_id="r1"
        ),
    }
    inner = RoutingVendor(routes)

    def exploding(url: str, *, timeout: float) -> HttpResponse:
        if cap.CONTRACTS_PATH in url and "underlying_ticker=TSLA" in url:
            raise AssertionError("vendor double died mid-sweep")
        return inner(url, timeout=timeout)

    client = MassiveClient(
        api_key=KEY,
        transport=exploding,
        cache_dir=tmp_path / "cache",
        governor=RateGovernor(None),
    )
    out = tmp_path / "captures"
    with pytest.raises(AssertionError, match="mid-sweep"):
        cap.run_capture(client, out, budget=cap.Budget(limit=12))

    manifest = load_massive_capture_manifest(out / "capture_manifest.json")
    verify_massive_capture_manifest(manifest, out, capture_version=cap.CAPTURE_VERSION)
    assert [m.underlying for m in manifest.masters] == ["SPY"], "SPY landed before the crash"
    assert (out / "masters" / "SPY_2025-03-14.json").is_file()
    assert (out / "spot_proxy.json").is_file()
    assert any("no session close" in note for note in manifest.notes), "notes so far survive"


def test_a_foreign_next_url_midwalk_becomes_a_master_error_not_a_crash(
    tmp_path: Path,
) -> None:
    """A pagination cursor pointing at a foreign host refuses THIS capture.

    Following it would append the key to someone else's host; aborting the
    whole run would lose every capture already made. The refusal becomes the
    master's `error`.
    """
    first = contracts_url("SPY", AS_OF)
    second = contracts_url("SPY", AS_OF, cursor="C2")
    evil = "https://evil.example.com/v3/reference/options/contracts?cursor=STEAL"
    vendor = RoutingVendor(
        {
            first: contracts_page(
                [contract_json("SPY", NEAR, "560", "call")],
                request_id="r1",
                next_url=f"https://api.polygon.io{second}",
            ),
            second: contracts_page(
                [contract_json("SPY", NEAR, "570", "call")], request_id="r2", next_url=evil
            ),
        }
    )
    client = make_client(tmp_path, vendor)
    capture = cap.capture_master(client, "SPY", AS_OF, budget=cap.Budget(limit=9), max_pages=4)

    assert len(capture.pages) == 2
    assert capture.error is not None
    assert "MassivePaginationError" in capture.error
    assert "evil.example.com" in capture.error, "the error names the pagination refusal"


def test_an_errored_master_with_pages_still_writes_its_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pages already fetched are real captured data, even on a failed walk.

    The envelope is written whenever any page exists; the manifest carries
    the error alongside, so `masters[].file` agrees with the disk.
    """
    monkeypatch.setattr(cap, "UNDERLYINGS", ("SPY",))
    monkeypatch.setattr(cap, "AS_OF_DATES", (AS_OF,))
    monkeypatch.setattr(cap, "BARS_WANTED", 0)
    first = contracts_url("SPY", AS_OF)
    second = contracts_url("SPY", AS_OF, cursor="C2")
    routes = {
        spot_url("SPY", AS_OF, AS_OF): spot_page("SPY", [AS_OF]),
        first: contracts_page(
            [contract_json("SPY", NEAR, "560", "call")],
            request_id="r1",
            next_url=f"https://api.polygon.io{second}",
        ),
        second: '{"status":"ERROR","error":"vendor exploded on page 2"}',
    }
    vendor = RoutingVendor(routes)
    client = make_client(tmp_path, vendor)

    out = tmp_path / "captures"
    manifest = cap.run_capture(client, out, budget=cap.Budget(limit=12))

    entry = manifest["masters"][0]
    assert entry["pages"] == 1
    assert entry["file"] == "SPY_2025-03-14.json"
    assert entry["complete"] is False
    assert "MassiveApiError" in (entry["error"] or "")
    envelope = out / "masters" / "SPY_2025-03-14.json"
    assert envelope.is_file(), "the envelope lands even though the walk failed"
    payload = structural.decode_payload(envelope.read_text(encoding="utf-8"), source="probe")
    assert len(payload["pages"]) == 1


# ---- the CLI profile ----------------------------------------------------------


def test_cli_flags_override_the_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The flags narrow the run; anything not asked for is never routed."""
    near25 = date(2025, 4, 8)  # 25 DTE: inside a 20-60 band, outside the default 30-60
    ticker = "O:TSLA250408C00560000"
    spot = spot_url("TSLA", AS_OF, AS_OF)
    master = contracts_url("TSLA", AS_OF)
    bar = aggs_url(ticker, AS_OF, near25)
    routes = {
        spot: spot_page("TSLA", [AS_OF]),
        master: contracts_page([contract_json("TSLA", near25, "560", "call")], request_id="r1"),
        bar: bars_page(ticker, [date(2025, 3, 17)]),
    }
    seen: dict[str, Any] = {}

    def routed(vendor: RoutingVendor, cache_name: str) -> Any:
        def fake_client(**kwargs: Any) -> MassiveClient:
            seen.update(kwargs)
            return MassiveClient(
                api_key=KEY,
                transport=vendor,
                cache_dir=tmp_path / cache_name,
                governor=RateGovernor(None),
            )

        return fake_client

    # The narrow profile: one underlying, one as_of, no bars. Only the spot
    # probe and the master sweep are routed -- no SPY, no other as_of date.
    vendor = RoutingVendor(routes)
    monkeypatch.setattr(cap, "client_from_environment", routed(vendor, "cache-a"))
    out = tmp_path / "narrow"
    assert (
        cap.main(
            [
                "--out-dir",
                str(out),
                "--underlyings",
                "TSLA",
                "--as-of",
                "2025-03-14",
                "--bars",
                "0",
                "--budget",
                "7",
                "--dte-min",
                "20",
                "--cache-dir",
                str(tmp_path / "flag-cache"),
                "--timeout",
                "5",
                "--max-pages",
                "9",
            ]
        )
        == 0
    )
    assert seen == {
        "cache_dir": Path(str(tmp_path / "flag-cache")),
        "timeout": 5.0,
        "max_pages": 9,
    }, "--cache-dir/--timeout/--max-pages pass through to client_from_environment"
    assert vendor.calls == [spot, master]

    # --dte-min 20 admits the 25-DTE expiry the default band refuses, so the
    # bar series is routed only because the flag moved the band floor.
    widened = RoutingVendor(routes)
    monkeypatch.setattr(cap, "client_from_environment", routed(widened, "cache-b"))
    assert (
        cap.main(
            [
                "--out-dir",
                str(tmp_path / "widened"),
                "--underlyings",
                "TSLA",
                "--as-of",
                "2025-03-14",
                "--bars",
                "1",
                "--budget",
                "7",
                "--dte-min",
                "20",
            ]
        )
        == 0
    )
    assert widened.calls == [spot, master, bar]

    # The default band (from the module globals) refuses the 25-DTE expiry.
    defaulted = RoutingVendor(routes)
    monkeypatch.setattr(cap, "client_from_environment", routed(defaulted, "cache-c"))
    out_c = tmp_path / "default-band"
    assert (
        cap.main(
            [
                "--out-dir",
                str(out_c),
                "--underlyings",
                "TSLA",
                "--as-of",
                "2025-03-14",
                "--bars",
                "1",
                "--budget",
                "7",
            ]
        )
        == 0
    )
    assert defaulted.calls == [spot, master], "the 25-DTE expiry stays unrouted"
    manifest = load_massive_capture_manifest(out_c / "capture_manifest.json")
    assert any("no 30-60 DTE expiry" in note for note in manifest.notes)


def test_budget_must_be_positive(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cap.main(["--out-dir", str(tmp_path / "out"), "--budget", "0"])
    assert excinfo.value.code == 2, "parser.error is the refusal, and it is exit 2"


def test_dry_run_never_touches_the_wire(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """--dry-run is cache-only: zero wire requests, zero budget spent."""
    monkeypatch.setattr(cap, "UNDERLYINGS", ("SPY",))
    monkeypatch.setattr(cap, "AS_OF_DATES", (AS_OF,))
    monkeypatch.setattr(cap, "BARS_WANTED", 1)
    warm = make_client(tmp_path, RoutingVendor(_one_master_routes()))
    cap.run_capture(warm, tmp_path / "warm-out", budget=cap.Budget(limit=9))

    # A vendor with NO routes would fail loudly on any wire call, so serving
    # the replay from it proves the wire was never touched.
    unrouted = RoutingVendor({})
    replay = MassiveClient(
        api_key=KEY,
        transport=unrouted,
        cache_dir=tmp_path / "cache",  # the same cache the warm run filled
        governor=RateGovernor(None),
    )
    monkeypatch.setattr(cap, "client_from_environment", lambda **kwargs: replay)
    out = tmp_path / "dry-out"
    assert cap.main(["--out-dir", str(out), "--dry-run", "--budget", "9"]) == 0
    assert unrouted.calls == []
    replayed = load_massive_capture_manifest(out / "capture_manifest.json")
    verify_massive_capture_manifest(replayed, out, capture_version=cap.CAPTURE_VERSION)
    assert replayed.requests_charged == 0
    assert replayed.client_stats["requests"] == 0
    assert [m.file for m in replayed.masters] == ["SPY_2025-03-14.json"]
    assert replayed.bars == ("O_SPY250417C00560000.json",)

    # An empty cache turns every miss into a note and captures nothing.
    cold = MassiveClient(
        api_key=KEY,
        transport=unrouted,
        cache_dir=tmp_path / "cold-cache",
        governor=RateGovernor(None),
    )
    monkeypatch.setattr(cap, "client_from_environment", lambda **kwargs: cold)
    empty_out = tmp_path / "empty-out"
    assert cap.main(["--out-dir", str(empty_out), "--dry-run", "--budget", "9"]) == 4
    empty = load_massive_capture_manifest(empty_out / "capture_manifest.json")
    assert empty.masters == ()
    assert any("SPY 2025-03-14: not captured (dry-run:" in note for note in empty.notes)


# ---- manifest truth ------------------------------------------------------------


def test_pages_fetched_reflects_the_bridge_truth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bridge never calls `paginate`, so it reports pages itself."""
    later = date(2025, 9, 15)
    monkeypatch.setattr(cap, "UNDERLYINGS", ("SPY",))
    monkeypatch.setattr(cap, "AS_OF_DATES", (AS_OF, later))
    monkeypatch.setattr(cap, "BARS_WANTED", 0)
    routes = {
        spot_url("SPY", AS_OF, later): spot_page("SPY", [AS_OF, later], close="661.25"),
        contracts_url("SPY", AS_OF): contracts_page(
            [contract_json("SPY", NEAR, "560", "call")], request_id="r1"
        ),
        contracts_url("SPY", later): contracts_page(
            [contract_json("SPY", NEAR, "560", "call")], request_id="r2"
        ),
    }
    vendor = RoutingVendor(routes)
    client = make_client(tmp_path, vendor)
    out = tmp_path / "captures"

    manifest = cap.run_capture(client, out, budget=cap.Budget(limit=12))

    assert client.stats.pages_fetched == 0, "the bridge walks pages itself"
    pages_on_disk = sum(
        len(structural.decode_payload(p.read_text(encoding="utf-8"), source="probe")["pages"])
        for p in (out / "masters").glob("*.json")
    )
    assert manifest["client_stats"]["pages_fetched"] == pages_on_disk == 2


def test_the_written_manifest_round_trips_and_verifies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What the bridge writes, the manifest module loads and verifies."""
    monkeypatch.setattr(cap, "UNDERLYINGS", ("SPY",))
    monkeypatch.setattr(cap, "AS_OF_DATES", (AS_OF,))
    monkeypatch.setattr(cap, "BARS_WANTED", 1)
    vendor = RoutingVendor(_one_master_routes())
    client = make_client(tmp_path, vendor)
    out = tmp_path / "captures"
    cap.run_capture(client, out, budget=cap.Budget(limit=9))

    manifest = load_massive_capture_manifest(out / "capture_manifest.json")
    verify_massive_capture_manifest(manifest, out, capture_version=cap.CAPTURE_VERSION)
    assert manifest.capture_version == "m4b-capture/1"
    assert manifest.requests_charged == len(vendor.calls) == 3
    assert [f.path for f in manifest.files] == [
        "bars/O_SPY250417C00560000.json",
        "masters/SPY_2025-03-14.json",
        "spot_proxy.json",
    ]


# ---- end to end -------------------------------------------------------------


def test_a_full_capture_round_trips_into_the_inspector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point of the bridge: what it writes, the inspector reads."""
    monkeypatch.setattr(cap, "AS_OF_DATES", (AS_OF, date(2025, 9, 15)))
    monkeypatch.setattr(cap, "UNDERLYINGS", ("SPY",))
    monkeypatch.setattr(cap, "BARS_WANTED", 1)

    later = date(2025, 9, 15)
    later_near = date(2025, 10, 17)  # 32 DTE
    routes = {
        spot_url("SPY", AS_OF, later): (
            '{"ticker":"SPY","resultsCount":2,"adjusted":true,"results":['
            f'{{"v":1,"vw":1,"o":1,"c":560,"h":1,"l":1,"t":{et_midnight_ms(AS_OF)},"n":1}},'
            f'{{"v":1,"vw":1,"o":1,"c":{FRACTIONAL_STRIKE},"h":1,"l":1,"t":{et_midnight_ms(later)},"n":1}}'
            '],"status":"OK","request_id":"a"}'
        ),
        contracts_url("SPY", AS_OF): contracts_page(
            [
                contract_json("SPY", NEAR, "560", "call"),
                contract_json("SPY", NEAR, "560", "put"),
                contract_json("SPY", FAR, "560", "call"),
            ],
            request_id="r1",
        ),
        contracts_url("SPY", later): contracts_page(
            [
                contract_json("SPY", later_near, "560", "call"),
                contract_json("SPY", later_near, FRACTIONAL_STRIKE, "put", spc=80),
            ],
            request_id="r2",
        ),
        aggs_url("O:SPY250417C00560000", AS_OF, NEAR): bars_page(
            "O:SPY250417C00560000", [date(2025, 3, 17), date(2025, 3, 18)]
        ),
    }
    vendor = RoutingVendor(routes)
    client = make_client(tmp_path, vendor)
    out = tmp_path / "captures"
    manifest = cap.run_capture(client, out, budget=cap.Budget(limit=20))

    assert manifest["requests_charged"] == len(vendor.calls) == 4
    assert manifest["spot_proxy"] == {"SPY": {"2025-03-14": "560", "2025-09-15": "587.5"}}
    assert [m["complete"] for m in manifest["masters"]] == [True, True]
    assert manifest["bars"] == ["O_SPY250417C00560000.json"]

    masters = structural.load_contract_masters(out / "masters")
    bars = structural.load_bar_series(out / "bars")
    spot = structural.load_spot_proxy(out / "spot_proxy.json")
    report = structural.build_structural_report(
        masters, bars=bars, spot_proxy=spot, adapter_status=structural.adapter_status()
    )
    payload = structural.report_to_json(report)

    assert payload["report_version"] == structural.REPORT_VERSION
    assert payload["sources"]["adapter"] == {
        "module": "tree_options.data.massive_options",
        "status": "PRESENT (m4-massive/1)",
    }, "the adapter module must name the provider it captured with"
    assert report.incomplete_captures == ()
    assert report.masters == 2 and report.contract_rows == 5
    # The 80-share row is the reconciliation payoff: WS-D1 flags a
    # non-standard deliverable, WS-D2 counts it, and the exact strike token
    # survived the capture in between.
    assert dict(report.shares_per_contract_counts) == {"80": 1, "100": 4}
    assert report.non_standard_rows == 1
    assert Decimal(FRACTIONAL_STRIKE) in {c.strike for m in masters for c in m.contracts}, (
        "the fractional strike round-tripped exactly"
    )
