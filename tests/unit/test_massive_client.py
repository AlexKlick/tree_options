"""WS-D1: Massive/Polygon client — key custody, spacing, cache, refusals.

Hermetic and offline: every test injects `FakeTransport`/`FakeClock` from
`tests/fixtures/massive_responses.py`, no test opens a socket, and no test
reads the real key file (`load_api_key` is only ever called with an
explicit `env=` and `key_path=`).

The key used throughout is a literal test string. Several tests assert it
does NOT appear somewhere; that is the point of the lane.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from tests.fixtures import massive_responses as fx
from tree_options.data.massive_client import (
    API_KEY_ENV_VAR,
    FREE_TIER_REQUESTS_PER_MINUTE,
    BackoffPolicy,
    MassiveApiError,
    MassiveAuthError,
    MassiveClient,
    MassiveNotEntitledError,
    MassivePaginationError,
    MassiveRateLimitError,
    RateGovernor,
    cache_key_for,
    load_api_key,
    loads_exact,
    redact,
)

KEY = "pk_live_TESTKEY_do_not_use_1234567890"


def make_client(
    transport: fx.FakeTransport,
    *,
    cache_dir: Path | None = None,
    clock: fx.FakeClock | None = None,
    requests_per_minute: int | None = None,
    backoff: BackoffPolicy | None = None,
    max_pages: int = 25,
) -> MassiveClient:
    """A client wired to fakes. Rate spacing is OFF unless a test asks for
    it, so unrelated tests are not asserting sleep bookkeeping."""
    clock = fx.FakeClock() if clock is None else clock
    return MassiveClient(
        api_key=KEY,
        transport=transport,
        cache_dir=cache_dir,
        governor=RateGovernor(requests_per_minute, clock=clock, sleeper=clock.sleep),
        backoff=BackoffPolicy() if backoff is None else backoff,
        max_pages=max_pages,
    )


# ---- key custody -------------------------------------------------------------


def test_env_key_wins_over_the_key_file(tmp_path: Path) -> None:
    path = tmp_path / "polygon.key"
    path.write_text("from-file\n")
    path.chmod(0o600)
    assert load_api_key(env={API_KEY_ENV_VAR: "from-env"}, key_path=path) == "from-env"


def test_key_file_used_when_env_absent_and_whitespace_stripped(tmp_path: Path) -> None:
    path = tmp_path / "polygon.key"
    path.write_text(f"  {KEY}  \n")
    path.chmod(0o600)
    assert load_api_key(env={}, key_path=path) == KEY
    # a blank env var is treated as absent, not as an empty key
    assert load_api_key(env={API_KEY_ENV_VAR: "   "}, key_path=path) == KEY


def test_missing_and_blank_keys_fail_closed(tmp_path: Path) -> None:
    absent = tmp_path / "nope.key"
    with pytest.raises(MassiveAuthError, match=API_KEY_ENV_VAR):
        load_api_key(env={}, key_path=absent)
    blank = tmp_path / "blank.key"
    blank.write_text("\n")
    blank.chmod(0o600)
    with pytest.raises(MassiveAuthError, match="blank"):
        load_api_key(env={}, key_path=blank)


def test_group_readable_key_file_refused_and_error_names_mode_not_key(tmp_path: Path) -> None:
    path = tmp_path / "polygon.key"
    path.write_text(f"{KEY}\n")
    path.chmod(0o644)
    with pytest.raises(MassiveAuthError) as exc:
        load_api_key(env={}, key_path=path)
    assert "0644" in str(exc.value)
    assert KEY not in str(exc.value)
    assert KEY not in repr(exc.value)


def test_client_repr_never_carries_the_key() -> None:
    client = make_client(fx.transport_of())
    assert KEY not in repr(client)
    assert "base_url" in repr(client)


def test_display_url_drops_the_key_parameter() -> None:
    client = make_client(fx.transport_of())
    shown = client.display_url("/v3/x", {"apiKey": KEY, "b": "2", "a": "1"})
    assert shown == "/v3/x?a=1&b=2"
    assert KEY not in shown


def test_error_bodies_are_key_redacted() -> None:
    leaky = f'{{"status":"ERROR","error":"bad key {KEY}"}}'
    client = make_client(fx.FakeTransport([fx.FakeResponse(status=500, body=leaky)]))
    with pytest.raises(MassiveApiError) as exc:
        client.get_json("/v3/x")
    assert KEY not in str(exc.value)
    assert "REDACTED" in str(exc.value)
    assert exc.value.http_status == 500


def test_redact_is_a_noop_for_an_empty_secret() -> None:
    assert redact("nothing to hide", "") == "nothing to hide"
    assert redact(f"key={KEY}", KEY) == "key=REDACTED"


# ---- exact decoding ----------------------------------------------------------


def test_loads_exact_builds_decimals_from_raw_text() -> None:
    body = loads_exact(fx.C587_5)
    strike = body["strike_price"]
    assert isinstance(strike, Decimal)
    assert strike == Decimal("587.5")
    assert str(strike) == "587.5"  # the vendor's characters, not a float repr
    assert not isinstance(strike, float)
    integral = loads_exact(fx.C560)["strike_price"]
    assert isinstance(integral, int) and integral == 560


# ---- rate governor -----------------------------------------------------------


def test_governor_spaces_free_tier_requests_by_twelve_seconds() -> None:
    clock = fx.FakeClock()
    governor = RateGovernor(FREE_TIER_REQUESTS_PER_MINUTE, clock=clock, sleeper=clock.sleep)
    assert governor.min_interval == 12.0
    assert governor.acquire() == 0.0  # first request is free
    assert governor.acquire() == 12.0
    assert governor.acquire() == 12.0
    assert clock.slept == [12.0, 12.0]
    assert governor.slept_seconds == 24.0


def test_governor_does_not_sleep_when_the_caller_was_already_slow() -> None:
    clock = fx.FakeClock()
    governor = RateGovernor(5, clock=clock, sleeper=clock.sleep)
    governor.acquire()
    clock.now += 45.0  # a slow caller consumed more than the interval
    assert governor.acquire() == 0.0
    assert clock.slept == []


def test_governor_disabled_for_unlimited_tiers() -> None:
    clock = fx.FakeClock()
    governor = RateGovernor(None, clock=clock, sleeper=clock.sleep)
    assert governor.min_interval == 0.0
    for _ in range(10):
        assert governor.acquire() == 0.0
    assert clock.slept == []
    with pytest.raises(ValueError, match="positive or None"):
        RateGovernor(0)


def test_client_spacing_is_accounted_in_stats() -> None:
    clock = fx.FakeClock()
    client = make_client(
        fx.transport_of(fx.CONTRACTS_EMPTY, fx.CONTRACTS_EMPTY, fx.CONTRACTS_EMPTY),
        clock=clock,
        requests_per_minute=FREE_TIER_REQUESTS_PER_MINUTE,
    )
    for page in range(3):
        client.get_json("/v3/x", {"page": page})
    assert client.stats.requests == 3
    assert client.stats.governor_sleeps == 2
    assert client.stats.governor_slept_seconds == 24.0


# ---- cache -------------------------------------------------------------------


def test_cache_key_excludes_the_api_key_in_any_casing() -> None:
    plain = cache_key_for("/v3/x", {"a": "1"})
    assert cache_key_for("/v3/x", {"a": "1", "apiKey": KEY}) == plain
    assert cache_key_for("/v3/x", {"a": "1", "apikey": "another-key"}) == plain
    assert cache_key_for("/v3/x", {"a": "2"}) != plain
    assert cache_key_for("/v3/y", {"a": "1"}) != plain
    assert KEY not in plain


def test_cache_miss_then_hit_and_a_hit_consumes_no_rate_token(tmp_path: Path) -> None:
    clock = fx.FakeClock()
    client = make_client(
        fx.transport_of(fx.CONTRACTS_SINGLE_PAGE),
        cache_dir=tmp_path,
        clock=clock,
        requests_per_minute=FREE_TIER_REQUESTS_PER_MINUTE,
    )
    first = client.get_json("/v3/reference/options/contracts", {"underlying_ticker": "SPY"})
    second = client.get_json("/v3/reference/options/contracts", {"underlying_ticker": "SPY"})
    assert first == second
    assert client.stats.cache_misses == 1
    assert client.stats.cache_hits == 1
    assert client.stats.requests == 1  # the transport was consulted once
    assert clock.slept == []  # ... so the governor never spaced anything
    assert len(list(tmp_path.glob("*.json"))) == 1


def test_cached_bytes_and_filenames_are_key_free(tmp_path: Path) -> None:
    echoed = f'{{"status":"OK","results":[],"echo":"{KEY}"}}'
    client = make_client(fx.transport_of(echoed), cache_dir=tmp_path)
    client.get_json("/v3/x", {"apiKey": KEY, "a": "1"})
    files = list(tmp_path.glob("*.json"))
    assert len(files) == 1
    assert KEY not in files[0].name
    stored = files[0].read_bytes()
    assert KEY.encode() not in stored
    assert b"REDACTED" in stored


def test_cache_can_be_disabled_entirely() -> None:
    client = make_client(fx.transport_of(fx.CONTRACTS_EMPTY, fx.CONTRACTS_EMPTY))
    client.get_json("/v3/x")
    client.get_json("/v3/x")
    assert client.cache is None
    assert client.stats.requests == 2
    assert client.stats.cache_hits == 0


# ---- 429 backoff -------------------------------------------------------------


def test_429_backs_off_exponentially_then_succeeds() -> None:
    clock = fx.FakeClock()
    transport = fx.queue(
        fx.FakeResponse(status=429, body=fx.RATE_LIMITED_BODY),
        fx.FakeResponse(status=429, body=fx.RATE_LIMITED_BODY),
        fx.FakeResponse(status=200, body=fx.CONTRACTS_EMPTY),
    )
    client = make_client(transport, clock=clock, backoff=BackoffPolicy(max_attempts=3))
    body = client.get_json("/v3/x")
    assert body["status"] == "OK"
    assert clock.slept == [2.0, 4.0]
    assert client.stats.rate_limit_retries == 2
    assert client.stats.requests == 3


def test_429_exhausts_the_cap_and_raises() -> None:
    clock = fx.FakeClock()
    transport = fx.FakeTransport(
        [fx.FakeResponse(status=429, body=fx.RATE_LIMITED_BODY) for _ in range(3)]
    )
    client = make_client(transport, clock=clock, backoff=BackoffPolicy(max_attempts=3))
    with pytest.raises(MassiveRateLimitError) as exc:
        client.get_json("/v3/x")
    assert "3 attempts" in str(exc.value)
    assert f"{FREE_TIER_REQUESTS_PER_MINUTE} requests/minute" in str(exc.value)
    assert clock.slept == [2.0, 4.0]  # bounded: no wait after the final attempt
    assert transport.calls == 3
    assert KEY not in str(exc.value)


def test_retry_after_header_is_honored_but_capped() -> None:
    clock = fx.FakeClock()
    transport = fx.queue(
        fx.FakeResponse(status=429, body="", headers={"retry-after": "7"}),
        fx.FakeResponse(status=429, body="", headers={"retry-after": "9999"}),
        fx.FakeResponse(status=200, body=fx.CONTRACTS_EMPTY),
    )
    client = make_client(
        transport, clock=clock, backoff=BackoffPolicy(max_attempts=4, cap_seconds=30.0)
    )
    client.get_json("/v3/x")
    assert clock.slept == [7.0, 30.0]


def test_backoff_policy_delays_are_capped_and_summable() -> None:
    policy = BackoffPolicy(max_attempts=5, initial_seconds=2.0, multiplier=2.0, cap_seconds=8.0)
    assert [policy.delay_for(a) for a in (1, 2, 3, 4)] == [2.0, 4.0, 8.0, 8.0]
    assert policy.total_seconds() == 22.0  # attempts 1..4 precede the 5th try
    with pytest.raises(ValueError, match="attempt must be"):
        policy.delay_for(0)


# ---- entitlement and body-level refusals -------------------------------------


def test_not_authorized_arrives_as_http_200_and_still_raises() -> None:
    transport = fx.FakeTransport([fx.FakeResponse(status=200, body=fx.NOT_AUTHORIZED_SNAPSHOT)])
    client = make_client(transport)
    with pytest.raises(MassiveNotEntitledError) as exc:
        client.get_json("/v3/snapshot/options/SPY")
    assert exc.value.endpoint == "/v3/snapshot/options/SPY"
    assert exc.value.vendor_message == fx.NOT_AUTHORIZED_MESSAGE
    assert "not entitled" in str(exc.value)
    assert "/v3/snapshot/options/SPY" in str(exc.value)


def test_not_authorized_is_re_raised_from_cache_too(tmp_path: Path) -> None:
    """A refusal cached by an earlier run must not decay into a silent
    success on the next one."""
    client = make_client(
        fx.FakeTransport([fx.FakeResponse(status=200, body=fx.NOT_AUTHORIZED_SNAPSHOT)]),
        cache_dir=tmp_path,
    )
    with pytest.raises(MassiveNotEntitledError):
        client.get_json("/v3/snapshot/options/SPY")
    # nothing was cached (the raise precedes the write), so a replay still refuses
    replay = make_client(
        fx.FakeTransport([fx.FakeResponse(status=200, body=fx.NOT_AUTHORIZED_SNAPSHOT)]),
        cache_dir=tmp_path,
    )
    with pytest.raises(MassiveNotEntitledError):
        replay.get_json("/v3/snapshot/options/SPY")


def test_unknown_vendor_status_and_missing_status_both_refuse() -> None:
    client = make_client(fx.transport_of(fx.VENDOR_ERROR_BODY))
    with pytest.raises(MassiveApiError, match="unknown ticker"):
        client.get_json("/v2/aggs/ticker/O:NOPE/range/1/day/2025-01-01/2025-01-02")
    headless = make_client(fx.transport_of('{"results":[]}'))
    with pytest.raises(MassiveApiError, match="no `status`"):
        headless.get_json("/v3/x")


def test_non_json_body_refused() -> None:
    client = make_client(fx.transport_of("<html>502 Bad Gateway</html>"))
    with pytest.raises(MassiveApiError, match="not JSON"):
        client.get_json("/v3/x")


def test_json_array_body_refused() -> None:
    client = make_client(fx.transport_of("[1,2,3]"))
    with pytest.raises(MassiveApiError, match="expected an object"):
        client.get_json("/v3/x")


# ---- pagination --------------------------------------------------------------


def test_paginate_follows_next_url_and_counts_pages() -> None:
    transport = fx.transport_of(fx.CONTRACTS_PAGE_1, fx.CONTRACTS_PAGE_2)
    client = make_client(transport)
    page = client.paginate(
        "/v3/reference/options/contracts", {"underlying_ticker": "SPY", "limit": 1000}
    )
    assert page.pages_fetched == 2
    assert len(page.results) == 4
    assert [r["ticker"] for r in page.results] == [
        "O:SPY250314C00375000",
        "O:SPY250314C00560000",
        "O:SPY250314C00587500",
        "O:SPY250314P00375000",
    ]
    assert len(page.request_ids) == 2
    assert client.stats.pages_fetched == 2


def test_next_url_is_re_keyed_at_request_time_only() -> None:
    transport = fx.transport_of(fx.CONTRACTS_PAGE_1, fx.CONTRACTS_PAGE_2)
    client = make_client(transport)
    client.paginate("/v3/reference/options/contracts", {"underlying_ticker": "SPY"})
    first, second = transport.urls
    assert f"apiKey={KEY}" in first  # the key reaches the wire...
    assert f"apiKey={KEY}" in second
    assert "cursor=YXA9JTdCJTIySUQlMjIlM0ElMjI3NzI4MjU0MzYxMDEzNjMwMjI3JTIyJTdE" in second
    assert "underlying_ticker" not in second  # the cursor replaces the query
    assert transport.paths() == ["https://api.polygon.io/v3/reference/options/contracts"] * 2


def test_foreign_next_url_host_is_refused_before_the_key_is_sent() -> None:
    transport = fx.transport_of(fx.CONTRACTS_FOREIGN_NEXT_URL)
    client = make_client(transport)
    with pytest.raises(MassivePaginationError, match=r"evil\.example\.com"):
        client.paginate("/v3/reference/options/contracts")
    assert transport.calls == 1  # the foreign host was never contacted
    assert all("evil.example.com" not in url for url in transport.urls)


def test_max_pages_guard_refuses_rather_than_truncates() -> None:
    transport = fx.transport_of(fx.CONTRACTS_ENDLESS, fx.CONTRACTS_ENDLESS)
    client = make_client(transport, max_pages=2)
    with pytest.raises(MassivePaginationError, match="max_pages=2"):
        client.paginate("/v3/reference/options/contracts")
    assert transport.calls == 2


def test_paginate_rejects_a_nonsense_cap() -> None:
    client = make_client(fx.transport_of())
    with pytest.raises(ValueError, match="max_pages must be"):
        client.paginate("/v3/x", max_pages=0)
