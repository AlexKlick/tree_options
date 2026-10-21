"""C2.8 shared Wave 2 fakes: each behaves like the surface it stands in for.

Oracles stay independent of the fakes: the family actor token is a golden
value minted by family-apps' own ``src/actor.mjs`` (2026-10-21); exec ids,
average cost and builder output are checked against the frozen contract
fixtures; session facts against literal NYSE dates; IBKR object shapes
against ib_async's own classes (when the library is installed).
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import json
import math
import os
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

import tree_options.trex.contracts as c
from tests.unit.trex_fakes import (
    CLOCK_START,
    FAKE_FAMILY_KEY,
    FAMILY_ACTOR_HEADER,
    FakeCommandAuthority,
    FillStep,
    SessionClock,
    StockTicker,
    SupervisedGateway,
    Wave2Gateway,
    approve_command,
    contract_fixture,
    entry_proposal,
    equity_effect,
    ledger_event,
    mandate_v2,
    position_row,
    sign_family_actor,
)
from tree_options.trex.clock import ET
from tree_options.trex.supervised_ibkr import SUPERVISED_CLIENT_ID

REPO = Path(__file__).resolve().parents[2]
ACCOUNT = "DU0000001"
SPY = 756733
ENTRY = "trex:sup:eq-spy-001"
EXIT = "trex:desk:eq-spy-001"
BODY = b'{"kind":"pause_entries"}'

#: family-apps src/actor.mjs signActor(FAKE_FAMILY_KEY, {sub: " Owner@Example.com ",
#: aud: "trex", method: "post", path: "/api/trex/commands",
#: now: Date.parse("2026-10-22T14:01:30Z")}), run under Node on 2026-10-21.
GOLDEN_IAT = 1792677690
GOLDEN_ACTOR = (
    "eyJ2IjoxLCJzdWIiOiJvd25lckBleGFtcGxlLmNvbSIsImF1ZCI6InRyZXgiLCJpYXQiOjE3OTI2Nzc2OTAsImV4"
    "cCI6MTc5MjY3Nzc1MCwibWV0aG9kIjoiUE9TVCIsInBhdGgiOiIvYXBpL3RyZXgvY29tbWFuZHMifQ"
    ".U0l6YW1o87cDNPIYIcpqW6uN1cV1oFEnO2dLmde6IOg"
)


def at(text: str) -> datetime:
    return datetime.fromisoformat(text)


def stock(symbol: str = "SPY", **over: Any) -> Any:
    fields: dict[str, Any] = {
        "secType": "STK",
        "symbol": symbol,
        "exchange": "SMART",
        "currency": "USD",
        "conId": 0,
        "lastTradeDateOrContractMonth": "",
        "strike": 0.0,
        "right": "",
    }
    fields.update(over)
    return SimpleNamespace(**fields)


def limit(
    action: str = "BUY", qty: Any = 2, price: Any = 671.5, ref: str = ENTRY, **over: Any
) -> Any:
    fields: dict[str, Any] = {
        "action": action,
        "totalQuantity": qty,
        "orderType": "LMT",
        "lmtPrice": price,
        "tif": "DAY",
        "outsideRth": False,
        "account": ACCOUNT,
        "orderRef": ref,
        "transmit": True,
    }
    fields.update(over)
    return SimpleNamespace(**fields)


def spy(gw: Wave2Gateway) -> Any:
    contract = stock()
    gw.qualifyContracts(contract)
    return contract


def place(gw: Wave2Gateway, action: str = "BUY", qty: int = 2, price: float = 671.5) -> Any:
    return gw.placeOrder(spy(gw), limit(action, qty, price, ENTRY if action == "BUY" else EXIT))


def bought(gw: Wave2Gateway, qty: int = 2) -> Any:
    """A filled 2 @ 671.50 entry whose commission report has arrived."""
    gw.script_fills(ENTRY, [FillStep()])
    trade = place(gw, qty=qty)
    gw.sleep(0)
    gw.sleep(0)
    return trade


def subscribed_spy(gw: Wave2Gateway) -> Any:
    return gw.reqMktData(spy(gw), "", False, False)


def ib_names(cls: Any) -> set[str]:
    fields = getattr(cls, "_fields", None)
    return set(fields) if fields else {f.name for f in dataclasses.fields(cls)}


# ------------------------------------------------------------ command authority


def authority_at(instant: str = "2026-10-22T14:01:30+00:00", **kw: Any) -> FakeCommandAuthority:
    clock = SessionClock()
    clock.to(at(instant))
    return FakeCommandAuthority(clock=clock, **kw)


def family_raw(
    token: str, *, method: str = "POST", path: str = "/api/trex/commands", peer: str = "127.0.0.1"
) -> c.RawCommandRequest:
    return c.RawCommandRequest(
        method=method,
        path=path,
        headers={FAMILY_ACTOR_HEADER: token},
        body=BODY,
        peer_host=peer,
        request_id="req-0001",
    )


def refusal(auth: FakeCommandAuthority, raw: c.RawCommandRequest) -> c.CommandAuthorityRefused:
    with pytest.raises(c.CommandAuthorityRefused) as caught:
        auth.verify(raw)
    assert caught.value.reason == c.Blocker.COMMAND_UNAUTHORIZED
    return caught.value


def forged_sub() -> str:
    payload, _, signature = GOLDEN_ACTOR.partition(".")
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    claims["sub"] = "mallory@example.com"
    forged = base64.urlsafe_b64encode(json.dumps(claims, separators=(",", ":")).encode())
    return f"{forged.rstrip(b'=').decode()}.{signature}"


def signed(**over: Any) -> str:
    fields: dict[str, Any] = {
        "sub": "owner@example.com",
        "method": "POST",
        "path": "/api/trex/commands",
        "iat": GOLDEN_IAT,
    }
    key = over.pop("key", FAKE_FAMILY_KEY)
    fields.update(over)
    return sign_family_actor(key, **fields)


def test_family_actor_signature_matches_family_apps() -> None:
    assert FAKE_FAMILY_KEY == "fake-family-actor-key-for-tests-only-0123456789"
    assert datetime.fromtimestamp(GOLDEN_IAT, UTC) == at("2026-10-22T14:01:30+00:00")
    token = sign_family_actor(
        FAKE_FAMILY_KEY,
        sub=" Owner@Example.com ",
        method="post",
        path="/api/trex/commands",
        iat=GOLDEN_IAT,
    )
    assert token == GOLDEN_ACTOR


def test_loopback_actor_is_deterministic_and_bound_to_the_request() -> None:
    auth = FakeCommandAuthority()
    raw = auth.make_request(BODY, request_id="req-7")
    actor = auth.verify(raw)
    assert actor == c.Actor(
        sub="operator",
        ingress="loopback",
        request_id="req-7",
        request_sha256=hashlib.sha256(BODY).hexdigest(),
    )
    assert c.actor_binds_request(actor, raw)
    assert auth.verify(raw) == actor
    assert auth.calls == [raw, raw]
    assert auth.actors == [actor, actor]
    assert auth.refusals == []


def test_family_actor_from_the_golden_token() -> None:
    auth = authority_at()
    raw = family_raw(GOLDEN_ACTOR, path="/api/trex/commands?retry=1")
    actor = auth.verify(raw)
    assert (actor.sub, actor.ingress) == ("owner@example.com", "family")
    assert c.actor_binds_request(actor, raw)


def test_make_request_signs_a_family_actor() -> None:
    auth = FakeCommandAuthority()
    raw = auth.make_request(BODY, family_sub="Owner@Example.com", headers={"X-Trace": "1"})
    assert set(raw.headers) == {FAMILY_ACTOR_HEADER, "x-trace"}
    actor = auth.verify(raw)
    assert (actor.sub, actor.ingress) == ("owner@example.com", "family")


def test_fake_authority_satisfies_the_protocol() -> None:
    fake = FakeCommandAuthority()
    authority: c.CommandAuthority = fake
    assert authority.verify(fake.make_request(BODY)).ingress == "loopback"


REFUSALS: list[tuple[str, Callable[[FakeCommandAuthority], c.RawCommandRequest]]] = [
    ("wrong_peer", lambda a: a.make_request(BODY, peer_host="100.101.102.103")),
    (
        "wrong_peer",
        lambda a: a.make_request(BODY, family_sub="owner@example.com", peer_host="192.168.1.20"),
    ),
    (
        "forwarded_without_actor",
        lambda a: a.make_request(BODY, headers={"X-Forwarded-For": "100.64.0.9"}),
    ),
    ("bad_signature", lambda a: family_raw(signed(key="another-signing-key-of-32-bytes-or-more"))),
    ("bad_signature", lambda a: family_raw(forged_sub())),
    ("bad_signature", lambda a: family_raw("not-a-token")),
    ("bad_signature", lambda a: family_raw(GOLDEN_ACTOR + "x")),
    ("wrong_audience", lambda a: family_raw(signed(aud="house-studio"))),
    ("wrong_route", lambda a: family_raw(GOLDEN_ACTOR, method="GET")),
    ("wrong_route", lambda a: family_raw(GOLDEN_ACTOR, path="/api/trex/commands/other")),
]


@pytest.mark.parametrize(
    ("detail", "build"), REFUSALS, ids=[f"{d}-{i}" for i, (d, _) in enumerate(REFUSALS)]
)
def test_refusal_modes(
    detail: str, build: Callable[[FakeCommandAuthority], c.RawCommandRequest]
) -> None:
    auth = authority_at()
    raw = build(auth)
    assert refusal(auth, raw).detail == detail
    assert auth.refusals == [(raw, detail)]
    assert auth.calls == [raw]
    assert auth.actors == []


def test_family_actor_expires_at_exp() -> None:
    live = authority_at("2026-10-22T14:02:29+00:00")
    assert live.verify(family_raw(GOLDEN_ACTOR)).ingress == "family"
    dead = authority_at("2026-10-22T14:02:30+00:00")  # iat + 60
    assert refusal(dead, family_raw(GOLDEN_ACTOR)).detail == "actor_expired"


def test_forced_and_disabled_refusals() -> None:
    forced = FakeCommandAuthority(refuse_with="maintenance")
    assert refusal(forced, forced.make_request(BODY)).detail == "maintenance"
    auth = FakeCommandAuthority()
    auth.allow_loopback = False
    assert refusal(auth, auth.make_request(BODY)).detail == "loopback_disabled"
    assert auth.verify(auth.make_request(BODY, family_sub="owner@example.com")).ingress == "family"
    auth.allow_family = False
    raw = auth.make_request(BODY, family_sub="owner@example.com")
    assert refusal(auth, raw).detail == "family_disabled"


# ---------------------------------------------------------------- qualification


def test_spy_qualifies_to_its_ibkr_con_id() -> None:
    gw = Wave2Gateway()
    contract = stock()
    assert gw.qualifyContracts(contract) == [contract]
    assert contract.conId == SPY == contract_fixture("equity_effect.json")["con_id"]


def test_unknown_symbol_stays_unqualified() -> None:
    gw = Wave2Gateway()
    known, unknown = stock(), stock("ZZZZ")
    assert gw.qualifyContracts(known, unknown) == [known, None]
    assert unknown.conId == 0
    gw.add_stock("ZZZZ", 4242)
    assert gw.qualifyContracts(unknown) == [unknown]
    assert unknown.conId == 4242


def test_preset_con_ids_win_over_the_stock_table() -> None:
    gw = Wave2Gateway({("STK", "SPY", "", 0.0, ""): 1})
    contract = stock()
    gw.qualifyContracts(contract)
    assert contract.conId == 1


def test_wave1_gateway_still_qualifies_any_stock() -> None:
    gw = SupervisedGateway({})
    unknown = stock("ZZZZ")
    assert gw.qualifyContracts(unknown) == [unknown]
    assert unknown.conId == 1001


def test_real_ib_async_stock_and_limit_order_pass_the_wire_check() -> None:
    ib_async = pytest.importorskip("ib_async")
    gw = Wave2Gateway()
    contract = ib_async.Stock("SPY", "SMART", "USD")
    assert gw.qualifyContracts(contract) == [contract]
    assert contract.conId == SPY
    order = ib_async.LimitOrder(
        "BUY", 2, 671.5, tif="DAY", account=ACCOUNT, orderRef=ENTRY, outsideRth=False
    )
    gw.script_fills(ENTRY, [FillStep()])
    trade = gw.placeOrder(contract, order)
    gw.sleep(1)
    assert trade.orderStatus.status == "Filled"
    assert gw.wire_violations == []


# ------------------------------------------------------------------ market data


def test_stock_ticker_is_nan_until_the_first_tick() -> None:
    ticker = subscribed_spy(Wave2Gateway())
    assert isinstance(ticker, StockTicker)
    assert ticker.bid is not None and math.isnan(ticker.bid)
    assert ticker.ask is not None and math.isnan(ticker.ask)
    assert ticker.time is None


@pytest.mark.parametrize(("kind", "code"), [("realtime", 1), ("frozen", 2), ("delayed", 3)])
def test_market_data_type_per_symbol(kind: str, code: int) -> None:
    # IBKR reqMarketDataType codes: 1 live, 2 frozen, 3 delayed
    gw = Wave2Gateway()
    gw.reqMarketDataType(3)
    gw.set_market_data_type("SPY", kind)
    ticker = subscribed_spy(gw)
    gw.push_tick("SPY", 67148, 67152)
    assert ticker.marketDataType == code
    assert gw.md_type_calls == [3]


def test_market_data_type_switch_reaches_a_live_ticker() -> None:
    gw = Wave2Gateway()
    ticker = subscribed_spy(gw)
    assert ticker.marketDataType == 1
    gw.set_market_data_type("SPY", "delayed")
    assert ticker.marketDataType == 3
    with pytest.raises(ValueError, match="market data type"):
        gw.set_market_data_type("SPY", "delayed-frozen")


def test_scripted_ticks_arrive_one_per_sleep_stamped_by_the_clock() -> None:
    gw = Wave2Gateway()
    gw.script_ticks("SPY", [(67148, 67152), (None, 67150)])
    gw.sleep(1)  # nobody subscribed yet: the tick waits
    ticker = subscribed_spy(gw)
    assert math.isnan(ticker.bid)
    gw.sleep(1)
    assert (ticker.bid, ticker.ask) == (671.48, 671.52)
    assert ticker.time == at("2026-10-22T13:30:02+00:00")
    gw.sleep(1)
    assert math.isnan(ticker.bid) and ticker.ask == 671.5
    assert ticker.time == at("2026-10-22T13:30:03+00:00")
    gw.sleep(1)  # script exhausted: the ticker stays as it was
    assert ticker.time == at("2026-10-22T13:30:03+00:00")


def test_ticks_stop_after_the_subscription_is_cancelled() -> None:
    gw = Wave2Gateway()
    ticker = subscribed_spy(gw)
    gw.cancelMktData(ticker.contract)
    gw.script_bids("SPY", [67148])
    gw.sleep(1)
    assert math.isnan(ticker.bid)
    with pytest.raises(AssertionError, match="no live stock market-data subscription"):
        gw.push_tick("SPY", 67148, 67152)


def test_bid_sequence_for_stop_confirmation() -> None:
    stop = contract_fixture("equity_effect.json")["exit_policy"]["stop_price_cents"]
    gw = Wave2Gateway()
    ticker = subscribed_spy(gw)
    gw.script_bids("SPY", [stop + 10, stop, stop - 10, stop - 20])
    seen = []
    for _ in range(4):
        gw.sleep(1)
        seen.append((round(ticker.bid * 100), round(ticker.ask * 100), ticker.time))
    assert [(b, a) for b, a, _ in seen] == [
        (66510, 66514),
        (66500, 66504),
        (66490, 66494),
        (66480, 66484),
    ]
    assert len({t for _, _, t in seen}) == 4


# ------------------------------------------------------------------------ orders


def test_full_fill_at_the_limit() -> None:
    gw = Wave2Gateway()
    details: list[tuple[Any, Any]] = []
    gw.execDetailsEvent += lambda trade, fill: details.append((trade, fill))
    gw.script_fills(ENTRY, [FillStep()])
    trade = place(gw)
    assert trade.orderStatus.status == "Submitted"
    assert trade.fills == []
    assert gw.openTrades() == [trade]
    gw.sleep(1)
    status = trade.orderStatus
    assert (status.status, status.filled, status.remaining, status.avgFillPrice) == (
        "Filled",
        2,
        0,
        671.5,
    )
    [fill] = trade.fills
    assert details == [(trade, fill)]
    ex = fill.execution
    assert ex.execId == contract_fixture("broker_execution.json")["exec_id"]
    assert (ex.side, ex.shares, ex.price, ex.orderRef, ex.acctNumber) == (
        "BOT",
        2.0,
        671.5,
        ENTRY,
        ACCOUNT,
    )
    assert (ex.permId, ex.orderId, ex.clientId) == (
        trade.order.permId,
        trade.order.orderId,
        SUPERVISED_CLIENT_ID,
    )
    assert ex.time == fill.time == at("2026-10-22T13:30:01+00:00")
    assert gw.reqExecutions() == [fill]
    assert gw.fills() == [fill]
    assert gw.openTrades() == []
    assert gw.reqCompletedOrders(True) == [trade]


def test_sub_penny_price_improvement() -> None:
    gw = Wave2Gateway()
    gw.script_fills(ENTRY, [FillStep(price_micros=671_480_000)])
    trade = place(gw)
    gw.sleep(1)
    assert trade.fills[0].execution.price == 671.48
    assert trade.orderStatus.avgFillPrice == 671.48


def test_partial_fill_stays_working_until_scripted_further() -> None:
    gw = Wave2Gateway()
    gw.script_fills(ENTRY, [FillStep(1)])
    trade = place(gw)
    for _ in range(3):
        gw.sleep(1)
    status = trade.orderStatus
    assert (status.status, status.filled, status.remaining) == ("Submitted", 1, 1)
    assert len(trade.fills) == 1
    assert gw.openTrades() == [trade]
    gw.script_fills(ENTRY, [FillStep()])  # attaches to the working order
    gw.sleep(1)
    assert (status.status, status.filled, status.remaining) == ("Filled", 2, 0)
    assert [f.execution.cumQty for f in trade.fills] == [1.0, 2.0]
    assert [f.execution.execId for f in trade.fills] == [
        "0000e0d5.6576d6a5.01.01",
        "0000e0d5.6576d6a6.01.01",
    ]


def test_no_script_means_no_fill_unless_auto_fill() -> None:
    gw = Wave2Gateway()
    trade = place(gw)
    for _ in range(3):
        gw.sleep(1)
    assert (trade.orderStatus.status, trade.orderStatus.filled, trade.fills) == ("Submitted", 0, [])
    auto = Wave2Gateway()
    auto.auto_fill = True
    filled = place(auto)
    auto.sleep(1)
    assert filled.orderStatus.status == "Filled"


def test_status_walk_runs_before_fills_and_rejections_never_fill() -> None:
    gw = Wave2Gateway()
    gw.status_script = ["PendingSubmit", "Submitted"]
    gw.script_fills(ENTRY, [FillStep()])
    trade = place(gw)
    assert trade.orderStatus.status == "PendingSubmit"
    gw.sleep(1)
    assert trade.orderStatus.status == "Filled"
    rejected = Wave2Gateway()
    rejected.status_script = ["PendingSubmit", "Inactive"]
    rejected.reject_code = 201
    rejected.script_fills(ENTRY, [FillStep()])
    dead = place(rejected)
    rejected.sleep(1)
    rejected.sleep(1)
    assert (dead.orderStatus.status, dead.fills) == ("Inactive", [])
    assert dead.log[-1].errorCode == 201


def test_cancel_after_a_partial_fill() -> None:
    gw = Wave2Gateway()
    gw.script_fills(ENTRY, [FillStep(1), FillStep()])
    trade = place(gw)
    gw.sleep(1)
    gw.cancelOrder(trade.order)
    assert trade.orderStatus.status == "PendingCancel"
    assert gw.openTrades() == [trade]
    gw.sleep(1)
    assert (trade.orderStatus.status, trade.orderStatus.filled) == ("Cancelled", 1)
    gw.sleep(1)
    assert len(trade.fills) == 1
    assert gw.order_cancels == [trade.order.orderId]


def test_day_order_dies_at_an_early_close() -> None:
    clock = SessionClock()
    clock.to_et("12:50", date(2026, 11, 27))
    gw = Wave2Gateway(clock=clock)
    trade = place(gw)
    clock.to_et("12:59")
    assert gw.openTrades() == [trade]
    clock.to_et("13:00")
    assert gw.openTrades() == []
    assert trade.orderStatus.status == "Cancelled"


def test_day_order_on_a_regular_day_works_until_16_00() -> None:
    gw = Wave2Gateway()
    trade = place(gw)
    gw.clock.to_et("15:59")
    gw.sleep(59)
    assert trade.orderStatus.status == "Submitted"
    gw.sleep(1)
    assert trade.orderStatus.status == "Cancelled"


ORDER_VIOLATIONS: list[tuple[str, dict[str, Any]]] = [
    ("quantity", {"totalQuantity": 1.5}),
    ("quantity", {"totalQuantity": 0}),
    ("tif", {"tif": "GTC"}),
    ("outside_rth", {"outsideRth": True}),
    ("account", {"account": ""}),
    ("account", {"account": "DU9999999"}),
    ("order_type", {"orderType": "MKT"}),
    ("order_ref", {"orderRef": ""}),
    ("order_ref", {"orderRef": "trex:x-1"}),
    ("order_ref_side", {"orderRef": EXIT}),
    ("limit_price", {"lmtPrice": 671.505}),
    ("limit_price", {"lmtPrice": 0.0}),
    ("transmit", {"transmit": False}),
    ("action", {"action": "SSHORT"}),
]


@pytest.mark.parametrize(
    ("problem", "change"),
    ORDER_VIOLATIONS,
    ids=[f"{p}-{i}" for i, (p, _) in enumerate(ORDER_VIOLATIONS)],
)
def test_off_contract_stock_orders_are_refused(problem: str, change: dict[str, Any]) -> None:
    gw = Wave2Gateway()
    contract = spy(gw)
    order = limit(**change)
    with pytest.raises(AssertionError, match=problem):
        gw.placeOrder(contract, order)
    assert gw.trades == []
    [(ref, problems)] = gw.wire_violations
    assert ref == order.orderRef
    assert problem in problems


@pytest.mark.parametrize(
    ("problem", "change"),
    [
        ("con_id", {"conId": 0}),
        ("con_id", {"conId": 999}),
        ("exchange", {"exchange": "ISLAND"}),
        ("currency", {"currency": "EUR"}),
    ],
)
def test_off_contract_stock_contracts_are_refused(problem: str, change: dict[str, Any]) -> None:
    gw = Wave2Gateway()
    with pytest.raises(AssertionError, match=problem):
        gw.placeOrder(stock(**{"conId": SPY, **change}), limit())
    assert gw.trades == []


def test_never_sells_more_than_held() -> None:
    gw = Wave2Gateway()
    with pytest.raises(AssertionError, match="sell_exceeds_position"):
        place(gw, "SELL", 1)
    bought(gw)
    first = place(gw, "SELL", 2)
    with pytest.raises(AssertionError, match="sell_exceeds_position"):
        place(gw, "SELL", 1)  # 2 held, 2 already offered
    gw.cancelOrder(first.order)
    gw.sleep(1)
    gw.script_fills(EXIT, [FillStep()])
    exit_trade = place(gw, "SELL", 2)
    gw.sleep(1)
    assert exit_trade.orderStatus.status == "Filled"
    assert exit_trade.fills[0].execution.side == "SLD"


def test_manual_position_rows_count_as_held() -> None:
    gw = Wave2Gateway()
    gw.position_rows.append(position_row(SPY, 3.0, account=ACCOUNT, secType="STK", symbol="SPY"))
    assert place(gw, "SELL", 3).orderStatus.status == "Submitted"


@pytest.mark.parametrize(
    ("steps", "match"),
    [
        ([FillStep(3)], "overfills"),
        ([FillStep(1), FillStep(2)], "overfills"),
        ([FillStep(), FillStep(1)], "last step"),
        ([FillStep(price_micros=671_510_000)], "through its limit"),
    ],
)
def test_impossible_fill_scripts_are_refused(steps: list[FillStep], match: str) -> None:
    gw = Wave2Gateway()
    gw.script_fills(ENTRY, steps)
    with pytest.raises(ValueError, match=match):
        place(gw)
    assert gw.trades == []


def test_a_sell_never_fills_below_its_limit() -> None:
    gw = Wave2Gateway()
    bought(gw)
    gw.script_fills(EXIT, [FillStep(price_micros=671_490_000)])
    with pytest.raises(ValueError, match="through its limit"):
        place(gw, "SELL", 2)


def test_scripted_connection_drop_still_applies_to_stock_orders() -> None:
    gw = Wave2Gateway()
    contract = spy(gw)
    gw.drop("placeOrder", ConnectionError("socket gone"))
    with pytest.raises(ConnectionError):
        gw.placeOrder(contract, limit())
    assert gw.trades == []
    assert not gw.isConnected()


# ------------------------------------------------------- executions, commissions


def as_broker_execution(fill: Any) -> c.BrokerExecution:
    """An independent IBKR fill -> contract mapping (lane B owns the real one)."""
    ex, report = fill.execution, fill.commissionReport
    return c.BrokerExecution(
        exec_id=ex.execId,
        base_exec_id=c.base_exec_id_of(ex.execId),
        perm_id=ex.permId,
        order_ref=ex.orderRef,
        account_id=ex.acctNumber,
        con_id=fill.contract.conId,
        sec_type=fill.contract.secType,
        symbol=fill.contract.symbol,
        side=ex.side,
        quantity=int(ex.shares),
        price_micros=round(ex.price * 1_000_000),
        multiplier=int(getattr(fill.contract, "multiplier", "") or 1),
        time=ex.time,
        commission_micros=(
            round(report.commission * 1_000_000) if report.execId == ex.execId else None
        ),
    )


def test_commission_report_arrives_after_the_execution() -> None:
    gw = Wave2Gateway()
    reports: list[tuple[Any, ...]] = []
    gw.commissionReportEvent += lambda *args: reports.append(args)
    gw.script_fills(ENTRY, [FillStep()])
    trade = place(gw)
    gw.sleep(1)
    [fill] = trade.fills
    blank = fill.commissionReport
    assert (blank.execId, blank.commission, blank.currency) == ("", 0.0, "")
    assert reports == []
    gw.sleep(1)
    assert fill.commissionReport is blank  # updated in place, as ib_async does
    assert (blank.execId, blank.commission, blank.currency, blank.realizedPNL) == (
        fill.execution.execId,
        1.0,
        "USD",
        0.0,
    )
    assert reports == [(trade, fill, blank)]


def test_commission_lag_zero_and_held_reports() -> None:
    same_sleep = Wave2Gateway()
    same_sleep.commission_lag = 0
    trade = bought(same_sleep)
    assert trade.fills[0].commissionReport.commission == 1.0
    held = Wave2Gateway()
    held.commission_lag = None
    held.script_fills(ENTRY, [FillStep(1), FillStep(commission_micros=350_000)])
    trade = place(held)
    for _ in range(5):
        held.sleep(1)
    assert [f.commissionReport.execId for f in trade.fills] == ["", ""]
    second = trade.fills[1].execution.execId
    assert held.deliver_commissions(second) == 1
    assert trade.fills[1].commissionReport.commission == 0.35
    assert held.deliver_commissions() == 1
    assert held.deliver_commissions() == 0


def test_fills_map_onto_the_broker_execution_contract() -> None:
    clock = SessionClock()
    clock.to(at("2026-10-22T14:03:04+00:00"))
    gw = Wave2Gateway(clock=clock)
    gw.script_fills(ENTRY, [FillStep(price_micros=671_480_000)])
    trade = place(gw, price=671.5)
    gw.sleep(1)
    [fill] = trade.fills
    expected = contract_fixture("broker_execution.json") | {"perm_id": trade.order.permId}
    assert c.model_document(as_broker_execution(fill)) == expected | {"commission_micros": None}
    gw.sleep(1)
    assert c.model_document(as_broker_execution(fill)) == expected


def test_correction_re_reports_with_the_next_version() -> None:
    gw = Wave2Gateway()
    trade = bought(gw)
    [fill] = trade.fills
    first = fill.execution.execId
    seen: list[Any] = []
    gw.execDetailsEvent += lambda _trade, f: seen.append(f)
    fix = gw.correct_execution(first, quantity=1, price_micros=671_480_000)
    assert first.endswith(".01")
    assert fix.execution.execId == first[:-3] + ".02"
    assert c.base_exec_id_of(fix.execution.execId) == c.base_exec_id_of(first)
    assert (fix.execution.shares, fix.execution.price) == (1.0, 671.48)
    assert (fix.execution.permId, fix.execution.orderRef) == (fill.execution.permId, ENTRY)
    assert seen == [fix]
    assert gw.reqExecutions() == [fill, fix]
    assert trade.fills == [fill, fix]
    assert fix.commissionReport.execId == ""
    gw.sleep(0)
    assert (fix.commissionReport.execId, fix.commissionReport.commission) == (
        fix.execution.execId,
        0.0,
    )
    [row] = gw.positions()
    assert row.position == 1.0
    again = gw.correct_execution(first, price_micros=671_490_000)
    assert again.execution.execId == first[:-3] + ".03"
    assert again.execution.shares == 1.0  # from the latest report, .02
    with pytest.raises(KeyError):
        gw.correct_execution("0000e0d5.deadbeef.01.01")


# --------------------------------------------------------- positions, portfolio


def as_portfolio_item(row: Any) -> c.BrokerPortfolioItem:
    """An independent IBKR portfolio row -> contract mapping (lane B/D own it)."""
    qty = row.position
    whole = isinstance(qty, float) and qty.is_integer()
    return c.BrokerPortfolioItem.model_validate(
        {
            "account_id": row.account,
            "con_id": row.contract.conId,
            "sec_type": row.contract.secType,
            "symbol": row.contract.symbol,
            "position": int(qty) if whole else qty,
            "avg_cost_micros": round(row.averageCost * 1_000_000),
            "as_of": CLOCK_START,
        }
    )


def broker_qty(gw: Wave2Gateway, con_id: int = SPY) -> int:
    return sum(int(row.position) for row in gw.positions() if row.contract.conId == con_id)


def test_average_cost_includes_the_late_commission() -> None:
    clock = SessionClock()
    clock.to(at("2026-10-22T14:03:04+00:00"))
    gw = Wave2Gateway(clock=clock)
    gw.script_fills(ENTRY, [FillStep(price_micros=671_480_000)])
    place(gw)
    gw.sleep(1)
    [row] = gw.positions()
    assert (row.account, row.contract.conId, row.contract.secType) == (ACCOUNT, SPY, "STK")
    assert (row.position, row.avgCost) == (2.0, 671.48)
    gw.sleep(1)
    [row] = gw.positions()
    fixture = contract_fixture("broker_portfolio_item.json")
    assert round(row.avgCost * 1_000_000) == fixture["avg_cost_micros"]


def test_portfolio_marks_from_override_tick_or_cost() -> None:
    gw = Wave2Gateway()
    subscribed_spy(gw)
    bought(gw)  # 2 @ 671.50 + $1.00 commission: average cost 672.00
    [item] = gw.portfolio()
    assert (item.position, item.averageCost, item.marketPrice) == (2.0, 672.0, 672.0)
    gw.push_tick("SPY", 67148, 67152)
    [item] = gw.portfolio()
    assert item.marketPrice == pytest.approx(671.5)
    assert item.marketValue == pytest.approx(1343.0)
    gw.marks[SPY] = 680_000_000
    [item] = gw.portfolio()
    assert (item.marketPrice, item.marketValue) == (680.0, 1360.0)
    assert as_portfolio_item(item).position == 2


def test_a_closed_position_reports_a_zero_row() -> None:
    gw = Wave2Gateway()
    bought(gw)
    gw.script_fills(EXIT, [FillStep()])
    place(gw, "SELL", 2)
    gw.sleep(0)
    [row] = gw.positions()
    assert row.position == 0.0


def test_manual_position_rows_come_first_and_unchanged() -> None:
    gw = Wave2Gateway()
    manual = position_row(5001, -1.0, account=ACCOUNT)
    gw.position_rows.append(manual)
    bought(gw)
    rows = gw.positions()
    assert rows[0] is manual
    assert [r.contract.conId for r in rows] == [5001, SPY]


def test_unexplained_position() -> None:
    gw = Wave2Gateway()
    gw.add_broker_position("SPY", 3, avg_cost_micros=670_000_000)
    mismatch = c.PositionMismatch(
        con_id=SPY, local_qty=0, broker_qty=broker_qty(gw), kind="unexplained"
    )
    assert mismatch.broker_qty == 3
    assert as_portfolio_item(gw.portfolio()[0]).avg_cost_micros == 670_000_000


def test_outside_position() -> None:
    gw = Wave2Gateway()
    gw.add_broker_position("QQQ", 5, con_id=320227571)
    [row] = gw.positions()
    outside = c.OutsidePosition(
        con_id=row.contract.conId,
        sec_type=row.contract.secType,
        symbol=row.contract.symbol,
        quantity=int(row.position),
    )
    assert (outside.con_id, outside.quantity) == (320227571, 5)
    with pytest.raises(ValueError, match="con_id"):
        gw.add_broker_position("QQQ", 5)


@pytest.mark.parametrize("zero_row", [False, True])
def test_missing_position(zero_row: bool) -> None:
    gw = Wave2Gateway()
    bought(gw)
    gw.hide_position(SPY, zero_row=zero_row)
    assert len(gw.positions()) == int(zero_row)
    assert len(gw.portfolio()) == int(zero_row)
    c.PositionMismatch(con_id=SPY, local_qty=2, broker_qty=broker_qty(gw), kind="missing")


def test_quantity_mismatch() -> None:
    gw = Wave2Gateway()
    bought(gw)
    gw.override_position(SPY, 1.0)
    c.PositionMismatch(con_id=SPY, local_qty=2, broker_qty=broker_qty(gw), kind="quantity_mismatch")
    [item] = gw.portfolio()
    assert item.marketValue == pytest.approx(item.marketPrice)
    gw.clear_position_faults()
    assert broker_qty(gw) == 2


def test_unrepresentable_rows_fail_the_contract() -> None:
    gw = Wave2Gateway()
    bought(gw)
    gw.override_position(SPY, Decimal("1.5"))
    with pytest.raises(ValidationError):
        as_portfolio_item(gw.portfolio()[0])
    future = Wave2Gateway()
    future.add_broker_position("ES", 1, con_id=495512563, sec_type="FUT")
    assert future.positions()[0].contract.secType == "FUT"
    with pytest.raises(ValidationError):
        as_portfolio_item(future.portfolio()[0])


def test_unreadable_account_views() -> None:
    gw = Wave2Gateway()
    gw.account_view_error = TimeoutError("account views down")
    for view in (gw.positions, gw.portfolio, gw.accountValues):
        with pytest.raises(TimeoutError):
            view()
    gw.clear_position_faults()
    assert gw.positions() == []


def test_account_values_carry_settled_cash() -> None:
    values = {v.tag: v for v in Wave2Gateway().accountValues()}
    assert (values["SettledCash"].value, values["SettledCash"].currency) == ("1000000.00", "USD")
    assert values["NetLiquidation"].account == ACCOUNT


def test_shapes_match_ib_async() -> None:
    ib_async = pytest.importorskip("ib_async")
    gw = Wave2Gateway()
    ticker = subscribed_spy(gw)
    gw.script_fills(ENTRY, [FillStep()])
    trade = place(gw)
    gw.sleep(1)
    [fill] = trade.fills
    assert vars(fill.commissionReport) == dataclasses.asdict(ib_async.CommissionReport())
    gw.sleep(1)
    assert set(vars(fill)) == ib_names(ib_async.Fill)
    assert set(vars(fill.execution)) == ib_names(ib_async.Execution)
    assert set(vars(fill.commissionReport)) == ib_names(ib_async.CommissionReport)
    assert set(vars(gw.positions()[0])) == ib_names(ib_async.Position)
    assert set(vars(gw.portfolio()[0])) == ib_names(ib_async.PortfolioItem)
    assert set(vars(gw.accountValues()[0])) == ib_names(ib_async.AccountValue)
    assert ib_names(type(trade.orderStatus)) <= ib_names(ib_async.OrderStatus)
    assert ib_names(type(trade)) <= ib_names(ib_async.Trade)
    assert ib_names(type(ticker)) <= ib_names(ib_async.Ticker)
    assert set(vars(trade.log[0])) == ib_names(ib_async.TradeLogEntry)


# ----------------------------------------------------------------- session clock


def test_clock_starts_at_the_fixture_open_with_the_fixture_epoch() -> None:
    clock = SessionClock()
    assert clock() == clock.now() == datetime(2026, 10, 22, 9, 30, tzinfo=ET)
    assert clock.now().utcoffset() == timedelta(hours=-4)
    assert clock.utc() == CLOCK_START
    assert clock.owner_epoch == contract_fixture("proposal_equity.json")["review"]["owner_epoch"]
    assert clock.in_session()


def test_clock_never_runs_backwards() -> None:
    clock = SessionClock()
    with pytest.raises(ValueError):
        clock.advance(-1)
    with pytest.raises(ValueError):
        clock.to(clock.now() - timedelta(seconds=1))
    with pytest.raises(ValueError):
        clock.to(datetime(2026, 10, 23, 10, 0))
    with pytest.raises(ValueError):
        SessionClock(datetime(2026, 10, 22, 13, 30))


def test_elapsed_time_is_dst_correct() -> None:
    clock = SessionClock()
    clock.to_close(date(2026, 10, 30))  # Friday 16:00 EDT
    assert clock.utc() == at("2026-10-30T20:00:00+00:00")
    clock.to_next_open()  # Monday 09:30 EST
    assert clock.utc() == at("2026-11-02T14:30:00+00:00")
    across = SessionClock(at("2026-10-31T12:00:00+00:00"))
    across.advance(86_400)
    assert across.utc() == at("2026-11-01T12:00:00+00:00")
    assert across.now().hour == 7  # 08:00 EDT + 24 h = 07:00 EST


def test_early_close_sessions() -> None:
    clock = SessionClock()
    assert clock.close_of(date(2026, 11, 27)) == datetime(2026, 11, 27, 13, 0, tzinfo=ET)
    assert clock.is_early_close(date(2026, 11, 27))
    assert clock.is_early_close(date(2026, 12, 24))
    assert not clock.is_early_close(date(2026, 11, 25))
    assert clock.close_of(date(2026, 11, 25)) == datetime(2026, 11, 25, 16, 0, tzinfo=ET)
    assert clock.to_close(date(2026, 11, 27), before_s=15 * 60) == datetime(
        2026, 11, 27, 12, 45, tzinfo=ET
    )


def test_sessions_skip_holidays_and_weekends() -> None:
    clock = SessionClock()
    assert clock.next_session(date(2026, 11, 25)) == date(2026, 11, 27)  # Thanksgiving
    assert clock.next_session(date(2026, 10, 23)) == date(2026, 10, 26)
    assert not clock.is_session(date(2026, 11, 26))
    assert clock.trading_session() == date(2026, 10, 22)
    clock.to_et("16:00")
    assert not clock.in_session()
    assert clock.trading_session() == date(2026, 10, 23)


def test_restart_mints_a_new_owner_epoch_and_invalidates_approvals() -> None:
    clock = SessionClock()
    clock.to(at("2026-10-22T14:02:00+00:00"))
    proposal = c.EntryProposal.model_validate(contract_fixture("proposal_equity.json"))
    command = c.parse_command(contract_fixture("command_approve_entry.json"))
    assert isinstance(command, c.ApproveEntryCommand)
    approval = command
    profile = proposal.review.profile_digest

    def blockers() -> tuple[c.Blocker, ...]:
        return c.approval_blockers(
            proposal,
            approval,
            now=clock.utc(),
            owner_epoch=clock.owner_epoch,
            profile_digest=profile,
        )

    assert blockers() == ()
    first = clock.owner_epoch
    second = clock.restart(down_s=30)
    assert second == clock.owner_epoch != first
    assert clock.epochs == [first, second]
    assert clock.restarts == 1
    assert clock.utc() == at("2026-10-22T14:02:30+00:00")
    assert blockers() == (c.Blocker.APPROVAL_EPOCH_MISMATCH,)


def test_restart_across_the_night_into_an_early_close() -> None:
    clock = SessionClock()
    clock.to_et("16:05", date(2026, 11, 25))
    epoch = clock.restart(until=clock.open_of(date(2026, 11, 27)))
    assert clock.now() == datetime(2026, 11, 27, 9, 30, tzinfo=ET)
    assert epoch == "owner-20261127T143000Z-4243"
    assert clock.close_of() == datetime(2026, 11, 27, 13, 0, tzinfo=ET)


def test_gateway_sleep_advances_its_clock_unless_disabled() -> None:
    gw = Wave2Gateway()
    gw.sleep(2.5)
    assert gw.clock.utc() == CLOCK_START + timedelta(seconds=2.5)
    gw.sleep_advances_clock = False
    gw.sleep(10)
    assert gw.clock.utc() == CLOCK_START + timedelta(seconds=2.5)


# ---------------------------------------------------------------------- builders


@pytest.mark.parametrize(
    ("build", "fixture"),
    [
        (equity_effect, "equity_effect.json"),
        (entry_proposal, "proposal_equity.json"),
        (approve_command, "command_approve_entry.json"),
        (mandate_v2, "mandate_v2.json"),
        (ledger_event, "ledger_event_fill.json"),
    ],
)
def test_builders_default_to_the_frozen_fixtures(build: Callable[[], Any], fixture: str) -> None:
    assert c.model_document(build()) == contract_fixture(fixture)


def test_option_proposal_builder_reproduces_its_fixture() -> None:
    document = contract_fixture("proposal_option.json")
    effect = c.SupervisedEffect.model_validate(document["effect"])
    assert c.model_document(entry_proposal(effect)) == document


def test_builders_rebind_after_a_change() -> None:
    effect = equity_effect(quantity=3, intent_id="eq-spy-002")
    assert effect.order_ref == "trex:sup:eq-spy-002"
    proposal = entry_proposal(effect)
    assert proposal.effect_sha256 == c.effect_v1_sha256(effect)
    assert proposal.review.notional_cents == 201_450  # 3 x 671.50
    assert proposal.review.max_loss_cents == 40_290  # 20 % gap reserve > 3 x 6.50
    command = approve_command(proposal)
    now = at("2026-10-22T14:02:00+00:00")
    review = proposal.review
    assert (
        c.approval_blockers(
            proposal,
            command,
            now=now,
            owner_epoch=review.owner_epoch,
            profile_digest=review.profile_digest,
        )
        == ()
    )
    mandate = mandate_v2(proposal, command, now=now)
    assert (mandate.intent_id, mandate.effect_sha256) == ("eq-spy-002", proposal.effect_sha256)
    assert ledger_event(attribution={"sleeve_ref": "s2"}).attribution.sleeve_ref == "s2"


def test_contract_fixture_is_a_fresh_copy() -> None:
    contract_fixture("equity_effect.json")["quantity"] = 99
    assert contract_fixture("equity_effect.json")["quantity"] == 2


# ----------------------------------------------------------------------- hygiene


def test_fakes_import_without_a_broker_library() -> None:
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(REPO / "src"), str(REPO)])}
    probe = "import sys, tests.unit.trex_fakes; sys.exit(1 if 'ib_async' in sys.modules else 0)"
    done = subprocess.run(
        [sys.executable, "-B", "-c", probe], cwd=REPO, env=env, capture_output=True, timeout=120
    )
    assert done.returncode == 0, done.stderr.decode()[-2000:]
