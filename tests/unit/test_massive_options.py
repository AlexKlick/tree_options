"""WS-D1: Massive/Polygon structural adapter — mapping, exactness, refusals.

Hermetic: every body comes from `tests/fixtures/massive_responses.py`, and
the two `fetch_*` tests drive a client wired to `FakeTransport`. Nothing
here opens a socket or reads a key file.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pytest

from tests.fixtures import massive_responses as fx
from tree_options.data.massive_client import (
    BackoffPolicy,
    MassiveClient,
    RateGovernor,
    loads_exact,
)
from tree_options.data.massive_options import (
    CAP_GREEKS,
    CAP_OPEN_INTEREST,
    CAP_QUOTES,
    CAP_STRIKE_LADDER,
    MASSIVE_FREE_CAPABILITIES,
    MassiveCapabilityError,
    MassiveSchemaError,
    build_contract_master,
    build_option_candidate_inputs,
    fetch_contract_master,
    fetch_daily_bars,
    instant_of_epoch_ms,
    parse_daily_bars,
    parse_option_contract,
    session_of_epoch_ms,
)
from tree_options.time.sessions import SESSION_TIMEZONE

KEY = "pk_live_TESTKEY_do_not_use_1234567890"
SPY_EXPIRY = date(2025, 3, 14)
AS_OF = date(2025, 1, 31)


def client_for(*bodies: str, max_pages: int = 25) -> tuple[MassiveClient, fx.FakeTransport]:
    transport = fx.transport_of(*bodies)
    clock = fx.FakeClock()
    client = MassiveClient(
        api_key=KEY,
        transport=transport,
        cache_dir=None,
        governor=RateGovernor(None, clock=clock, sleeper=clock.sleep),
        backoff=BackoffPolicy(),
        max_pages=max_pages,
    )
    return client, transport


def results_of(body: str) -> list[dict]:
    return list(loads_exact(body)["results"])


# ---- contract records --------------------------------------------------------


def test_contract_record_maps_every_probed_field() -> None:
    contract = parse_option_contract(loads_exact(fx.P375))
    assert contract.ticker == "O:SPY250314P00375000"
    assert contract.underlying == "SPY"
    assert contract.expiration == SPY_EXPIRY
    assert contract.strike == Decimal("375")
    assert contract.contract_type == "put"
    assert contract.exercise_style == "american"
    assert contract.shares_per_contract == 100
    assert contract.primary_exchange == "BATO"
    assert contract.cfi == "OPASPS"
    assert contract.is_standard_deliverable is True
    assert contract.is_european is False


def test_fractional_strike_keeps_the_vendors_exact_text() -> None:
    contract = parse_option_contract(loads_exact(fx.C587_5))
    assert isinstance(contract.strike, Decimal)
    assert contract.strike == Decimal("587.5")
    assert str(contract.strike) == "587.5"
    # an integral strike still arrives as an exact Decimal, never a float
    integral = parse_option_contract(loads_exact(fx.C560)).strike
    assert isinstance(integral, Decimal) and integral == Decimal("560")


def test_float_decoded_body_is_refused_not_laundered() -> None:
    """Decoding with plain `json.loads` destroys exactness before the
    adapter ever sees the number; accepting it would hide that."""
    lossy = json.loads(fx.C587_5)
    assert isinstance(lossy["strike_price"], float)
    with pytest.raises(MassiveSchemaError, match="loads_exact"):
        parse_option_contract(lossy)


def test_malformed_records_are_named_not_guessed() -> None:
    with pytest.raises(MassiveSchemaError, match="strike_price"):
        parse_option_contract(loads_exact(fx.C_MALFORMED))
    with pytest.raises(MassiveSchemaError, match="not call/put"):
        parse_option_contract({**loads_exact(fx.C375), "contract_type": "straddle"})
    with pytest.raises(MassiveSchemaError, match="not american/european"):
        parse_option_contract({**loads_exact(fx.C375), "exercise_style": "bermudan"})
    with pytest.raises(MassiveSchemaError, match="not positive"):
        parse_option_contract({**loads_exact(fx.C375), "strike_price": 0})
    with pytest.raises(MassiveSchemaError, match="ISO date"):
        parse_option_contract({**loads_exact(fx.C375), "expiration_date": "14/03/2025"})


# ---- contract master ---------------------------------------------------------


def test_master_maps_a_clean_page() -> None:
    master = build_contract_master(
        results_of(fx.CONTRACTS_SINGLE_PAGE), underlying="SPY", as_of=AS_OF, pages_fetched=1
    )
    assert master.underlying == "SPY"
    assert master.as_of == AS_OF
    assert master.stats.rows_total == 4
    assert master.stats.rows_mapped == 4
    assert master.issues == ()
    assert [c.ticker for c in master.contracts] == [
        "O:SPY250314C00375000",
        "O:SPY250314C00560000",
        "O:SPY250314C00587500",
        "O:SPY250314P00375000",
    ]


def test_master_row_accounting_buckets_sum_to_the_total() -> None:
    master = build_contract_master(results_of(fx.CONTRACTS_ODDITIES), underlying="SPY", as_of=AS_OF)
    stats = master.stats
    assert stats.rows_total == 5
    assert stats.rows_mapped == 2  # the 375 call and the adjusted 90-share call
    assert stats.duplicate_rows == 1
    assert stats.foreign_underlying_rows == 1  # the SPX european contract
    assert stats.malformed_rows == 1
    assert stats.rows_total == (
        stats.rows_mapped
        + stats.duplicate_rows
        + stats.foreign_underlying_rows
        + stats.malformed_rows
    )


def test_nonstandard_deliverable_is_kept_and_flagged() -> None:
    master = build_contract_master(results_of(fx.CONTRACTS_ODDITIES), underlying="SPY", as_of=AS_OF)
    assert master.stats.nonstandard_deliverable_rows == 1
    adjusted = master.by_ticker("O:SPY1250314C00400000")
    assert adjusted.shares_per_contract == 90
    assert adjusted.is_standard_deliverable is False
    assert master.nonstandard_deliverables() == (adjusted,)
    assert any("adjusted/non-standard deliverable" in i for i in master.issues)


def test_european_exercise_style_is_flagged() -> None:
    master = build_contract_master(results_of(fx.CONTRACTS_ODDITIES), underlying="SPX", as_of=AS_OF)
    assert master.stats.european_rows == 1
    contract = master.by_ticker("O:SPXW250321C05600000")
    assert contract.is_european is True
    assert contract.exercise_style == "european"
    assert master.european_contracts() == (contract,)
    assert any("early exercise/assignment" in i for i in master.issues)


def test_duplicate_and_foreign_records_are_refused_by_name() -> None:
    master = build_contract_master(results_of(fx.CONTRACTS_ODDITIES), underlying="SPY", as_of=AS_OF)
    assert any("duplicate ticker O:SPY250314C00375000" in i for i in master.issues)
    assert any("!= requested 'SPY'" in i for i in master.issues)
    assert any("refused" in i and "strike_price" in i for i in master.issues)


def test_empty_master_says_so_instead_of_answering_nothing() -> None:
    master = build_contract_master((), underlying="SPY", as_of=AS_OF)
    assert master.contracts == ()
    assert master.stats.rows_total == 0
    assert any("no contracts mapped" in i for i in master.issues)


def test_ladders_and_expirations_are_derivable() -> None:
    master = build_contract_master(
        results_of(fx.CONTRACTS_SINGLE_PAGE), underlying="SPY", as_of=AS_OF
    )
    assert master.expirations() == (SPY_EXPIRY,)
    assert master.ladder_for(SPY_EXPIRY) == (
        Decimal("375"),
        Decimal("560"),
        Decimal("587.5"),
    )
    with pytest.raises(ValueError, match="no expiry"):
        master.ladder_for(date(2025, 6, 20))
    with pytest.raises(ValueError, match="unknown contract"):
        master.by_ticker("O:SPY250314C00001000")


def test_fetch_contract_master_paginates_and_pins_the_pit_query() -> None:
    client, transport = client_for(fx.CONTRACTS_PAGE_1, fx.CONTRACTS_PAGE_2)
    master = fetch_contract_master(client, "SPY", AS_OF)
    assert master.pages_fetched == 2
    assert master.stats.rows_mapped == 4
    assert len(master.request_ids) == 2
    first = transport.urls[0]
    assert "underlying_ticker=SPY" in first
    assert "as_of=2025-01-31" in first
    assert "limit=1000" in first
    assert f"apiKey={KEY}" in first


def test_fetch_contract_master_rejects_an_illegal_page_size() -> None:
    client, transport = client_for()
    with pytest.raises(ValueError, match="limit must be"):
        fetch_contract_master(client, "SPY", AS_OF, limit=5000)
    assert transport.calls == 0


# ---- epoch -> session (DST) --------------------------------------------------


def test_daily_bar_epoch_resolves_to_the_eastern_session_across_dst() -> None:
    """The vendor anchors a daily bar at 00:00 America/New_York. The two
    fixture bars straddle the 2025-03-09 transition, so the SAME wall
    clock maps to different UTC offsets — the conversion must be done in
    the exchange timezone, not in UTC."""
    pre_ms, post_ms = 1741323600000, 1741579200000
    assert session_of_epoch_ms(pre_ms) == date(2025, 3, 7)
    assert session_of_epoch_ms(post_ms) == date(2025, 3, 10)
    pre_local = instant_of_epoch_ms(pre_ms).astimezone(SESSION_TIMEZONE)
    post_local = instant_of_epoch_ms(post_ms).astimezone(SESSION_TIMEZONE)
    assert (pre_local.hour, pre_local.minute) == (0, 0)
    assert (post_local.hour, post_local.minute) == (0, 0)
    assert pre_local.utcoffset().total_seconds() == -5 * 3600  # EST
    assert post_local.utcoffset().total_seconds() == -4 * 3600  # EDT
    # the same instants in UTC are 05:00Z and 04:00Z respectively
    assert instant_of_epoch_ms(pre_ms).hour == 5
    assert instant_of_epoch_ms(post_ms).hour == 4


def test_epoch_conversion_is_integer_exact() -> None:
    instant = instant_of_epoch_ms(1741323600123)
    assert instant.microsecond == 123000
    assert instant.second == 0


# ---- daily bars --------------------------------------------------------------


def test_daily_bars_map_exactly_and_sort_by_session() -> None:
    bars = parse_daily_bars(
        loads_exact(fx.AGGS_DAILY),
        option_ticker=fx.AGGS_TICKER,
        start=date(2025, 2, 1),
        end=SPY_EXPIRY,
    )
    assert len(bars) == 6
    assert [b.session for b in bars] == sorted(b.session for b in bars)
    first = bars[0]
    assert first.session == date(2025, 2, 3)
    assert first.option_ticker == fx.AGGS_TICKER
    assert str(first.close) == "40.08"  # exact vendor text, not 40.079999...
    assert first.volume == 25
    assert first.transactions == 1
    assert first.epoch_ms == 1738558800000
    expiry_bar = bars[-1]
    assert expiry_bar.session == SPY_EXPIRY
    assert expiry_bar.volume == 259249
    assert str(expiry_bar.vwap) == "1.7871"
    assert all(isinstance(b.close, Decimal) for b in bars)


def test_integer_valued_price_becomes_an_exact_decimal() -> None:
    body = fx.aggs_body((fx.BAR_2025_03_04,))
    bars = parse_daily_bars(loads_exact(body), option_ticker=fx.AGGS_TICKER)
    assert bars[0].low == Decimal("18")
    assert isinstance(bars[0].low, Decimal)
    assert bars[0].vwap == Decimal("19.7264")


def test_no_prints_in_the_window_is_an_empty_tuple_not_a_failure() -> None:
    assert parse_daily_bars(loads_exact(fx.AGGS_EMPTY), option_ticker=fx.AGGS_TICKER) == ()


def test_results_count_mismatch_is_treated_as_a_truncated_body() -> None:
    body = fx.aggs_body(fx.AGGS_BARS, results_count=24)
    with pytest.raises(MassiveSchemaError, match="resultsCount 24 != 6"):
        parse_daily_bars(loads_exact(body), option_ticker=fx.AGGS_TICKER)


def test_duplicate_session_and_broken_ohlc_are_refused() -> None:
    duplicated = fx.aggs_body((fx.BAR_2025_03_07, fx.BAR_2025_03_07))
    with pytest.raises(MassiveSchemaError, match="duplicate sessions"):
        parse_daily_bars(loads_exact(duplicated), option_ticker=fx.AGGS_TICKER)
    inverted = fx.aggs_body(('{"v":1,"vw":5,"o":5,"c":5,"h":4,"l":6,"t":1741323600000,"n":1}',))
    with pytest.raises(MassiveSchemaError, match="low 6 > high 4"):
        parse_daily_bars(loads_exact(inverted), option_ticker=fx.AGGS_TICKER)
    outside = fx.aggs_body(('{"v":1,"vw":5,"o":9,"c":5,"h":6,"l":4,"t":1741323600000,"n":1}',))
    with pytest.raises(MassiveSchemaError, match=r"open 9 outside"):
        parse_daily_bars(loads_exact(outside), option_ticker=fx.AGGS_TICKER)


def test_bars_outside_the_requested_window_are_refused() -> None:
    with pytest.raises(MassiveSchemaError, match="precedes start"):
        parse_daily_bars(
            loads_exact(fx.AGGS_DAILY),
            option_ticker=fx.AGGS_TICKER,
            start=date(2025, 3, 1),
            end=SPY_EXPIRY,
        )
    with pytest.raises(MassiveSchemaError, match="follows end"):
        parse_daily_bars(
            loads_exact(fx.AGGS_DAILY),
            option_ticker=fx.AGGS_TICKER,
            start=date(2025, 2, 1),
            end=date(2025, 3, 10),
        )


def test_fetch_daily_bars_builds_the_probed_path() -> None:
    client, transport = client_for(fx.AGGS_DAILY)
    bars = fetch_daily_bars(client, fx.AGGS_TICKER, date(2025, 2, 1), SPY_EXPIRY)
    assert len(bars) == 6
    url = transport.urls[0]
    assert "/v2/aggs/ticker/O:SPY250314C00560000/range/1/day/2025-02-01/2025-03-14" in url
    assert "adjusted=true" in url
    with pytest.raises(ValueError, match="precedes start"):
        fetch_daily_bars(client, fx.AGGS_TICKER, SPY_EXPIRY, date(2025, 2, 1))


# ---- declared capability boundary -------------------------------------------


def test_capabilities_declare_what_the_free_tier_withholds() -> None:
    caps = MASSIVE_FREE_CAPABILITIES
    assert caps.tier == "free"
    assert caps.has(CAP_STRIKE_LADDER)
    for withheld in (CAP_QUOTES, CAP_GREEKS, CAP_OPEN_INTEREST):
        assert not caps.has(withheld)
        assert withheld in caps.withholds
    assert set(caps.provides).isdisjoint(caps.withholds)
    assert "Starter" in caps.upgrade_tier
    assert caps.as_dict()["provider"] == "massive-polygon"


def test_requiring_a_withheld_capability_raises_and_names_the_tier() -> None:
    with pytest.raises(MassiveCapabilityError) as exc:
        MASSIVE_FREE_CAPABILITIES.require(CAP_STRIKE_LADDER, CAP_GREEKS)
    assert CAP_GREEKS in str(exc.value)
    assert CAP_STRIKE_LADDER not in str(exc.value)  # only the missing ones are named
    assert "required tier" in str(exc.value)
    MASSIVE_FREE_CAPABILITIES.require(CAP_STRIKE_LADDER)  # provided: no raise


def test_candidate_inputs_from_this_source_always_refuse() -> None:
    master = build_contract_master(
        results_of(fx.CONTRACTS_SINGLE_PAGE), underlying="SPY", as_of=AS_OF
    )
    with pytest.raises(MassiveCapabilityError) as exc:
        build_option_candidate_inputs(master)
    message = str(exc.value)
    assert CAP_QUOTES in message
    assert CAP_GREEKS in message
    assert CAP_OPEN_INTEREST in message
    assert "required tier" in message
