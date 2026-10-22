"""IBKR stock wire primitives (Wave 2 lane B) over the shared ``Wave2Gateway``.

Drives the REAL ``IbkrTrex`` (stock qualification with a pinned conId,
NBBO quotes in cents with IBKR's market-data type, executions with late
commission reports, portfolio/position reads) and the owner-run equity
quote probe. Oracles are the frozen contract fixtures
(``tests/contracts/fixtures``), literal cents and literal IBKR codes,
never adapter internals. Nothing here connects to a broker: every session
is a fake gateway (the probe takes an injected IB factory).
"""

from __future__ import annotations

import json
import math
import sys
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import tree_options.trex.contracts as c
from tests.conftest import REPO_ROOT
from tests.unit.trex_fakes import (
    FakeStatus,
    FakeTrade,
    FillStep,
    SessionClock,
    Wave2Gateway,
    contract_fixture,
    position_row,
)
from tree_options.trex.clock import ET
from tree_options.trex.ibkr import (
    EquityQuote,
    IbkrTrex,
    MarketDataTypeName,
    StockConIdMismatch,
    StockNotQualified,
    equity_quote_from_ticker,
    market_data_type_name,
)
from tree_options.trex.plan import LegStructure
from tree_options.trex.supervised_ibkr import SUPERVISED_CLIENT_ID

ib_async = pytest.importorskip("ib_async")

sys.path.insert(0, str(REPO_ROOT / "scripts"))
import probe_ibkr_equity_quotes as probe_script  # type: ignore[import-not-found]  # noqa: E402

ACCOUNT = "DU0000001"
OTHER_ACCOUNT = "DU0000002"
SPY = 756733
QQQ = 320227571
ENTRY = "trex:sup:eq-spy-001"
#: provider text that may carry account details: never in a result
SECRET = "acct DU0000001 provider detail"
AS_OF = datetime(2026, 10, 22, 14, 4, tzinfo=UTC)
TICK_AT = datetime(2026, 10, 22, 14, 0, tzinfo=UTC)
UNSET_DOUBLE = 1.7976931348623157e308


def at(text: str) -> datetime:
    return datetime.fromisoformat(text)


def _attached(gw: Wave2Gateway) -> IbkrTrex:
    ib = IbkrTrex(client_id=SUPERVISED_CLIENT_ID)
    ib._ib = gw
    return ib


def session(clock: SessionClock | None = None, **kw: Any) -> tuple[IbkrTrex, Wave2Gateway]:
    gw = Wave2Gateway(clock=clock, **kw)
    return _attached(gw), gw


def stock_buy(
    gw: Wave2Gateway, qty: int = 2, price: float = 671.5, ref: str = ENTRY, account: str = ACCOUNT
) -> Any:
    """A STK DAY limit BUY on the wire (real ib_async objects)."""
    contract = ib_async.Stock("SPY", "SMART", "USD", conId=SPY)
    order = ib_async.LimitOrder(
        "BUY", qty, price, tif="DAY", account=account, orderRef=ref, outsideRth=False
    )
    return gw.placeOrder(contract, order)


def filled_entry(price_micros: int = 671_480_000) -> tuple[IbkrTrex, Wave2Gateway, Any]:
    """2 SPY bought at ``price_micros`` at 14:03:05Z; the commission report
    (1.00 USD) is still to come (it arrives on the next sleep)."""
    clock = SessionClock()
    clock.to(at("2026-10-22T14:03:04+00:00"))
    ib, gw = session(clock)
    gw.script_fills(ENTRY, [FillStep(price_micros=price_micros)])
    trade = stock_buy(gw)
    gw.sleep(1)
    return ib, gw, trade


def odd_fill(con_id: int = 495512563, sec_type: str = "FUT", **execution: Any) -> Any:
    """An ib_async-shaped Fill the books cannot or can represent."""
    fields: dict[str, Any] = {
        "execId": "0000e0d5.7000aaaa.01.01",
        "time": TICK_AT,
        "acctNumber": ACCOUNT,
        "side": "BOT",
        "shares": 1.0,
        "price": 10.0,
        "permId": 99001,
        "orderRef": "",
    }
    fields.update(execution)
    return SimpleNamespace(
        contract=SimpleNamespace(conId=con_id, secType=sec_type, symbol="ES", multiplier=""),
        execution=SimpleNamespace(**fields),
        commissionReport=SimpleNamespace(execId="", commission=0.0),
        time=TICK_AT,
    )


def tick(bid: Any, ask: Any, time: Any = TICK_AT, md: Any = 1) -> Any:
    return SimpleNamespace(bid=bid, ask=ask, time=time, marketDataType=md)


# ------------------------------------------------------------ qualification


def test_prepare_stock_qualifies_smart_usd_and_subscribes_once() -> None:
    ib, gw = session()
    assert ib.prepare_stock("SPY") == SPY
    [[contract]] = gw.qualify_calls
    assert (contract.secType, contract.symbol, contract.exchange, contract.currency) == (
        "STK",
        "SPY",
        "SMART",
        "USD",
    )
    assert [s.conId for s in gw.subscribed] == [SPY]
    assert ib.prepare_stock("SPY", SPY) == SPY, "a prepared stock answers its pin"
    assert len(gw.qualify_calls) == 1, "never re-qualified"
    assert len(gw.subscribed) == 1, "one stream per symbol"


def test_a_pinned_con_id_must_equal_the_qualified_one() -> None:
    ib, gw = session()
    with pytest.raises(StockConIdMismatch):
        ib.prepare_stock("SPY", SPY + 1)
    assert gw.subscribed == [], "a refused pin subscribes nothing"
    assert ib.equity_quote("SPY") is None
    assert ib.prepare_stock("SPY", SPY) == SPY, "the refusal left nothing held"
    with pytest.raises(StockConIdMismatch):
        ib.prepare_stock("SPY", 1)
    assert len(gw.subscribed) == 1


def test_an_unqualifiable_symbol_refuses_and_subscribes_nothing() -> None:
    ib, gw = session()
    with pytest.raises(StockNotQualified):
        ib.prepare_stock("ZZZZ")
    assert gw.subscribed == []
    assert ib.equity_quote("ZZZZ") is None


def test_settle_sleep_only_for_a_new_stream() -> None:
    ib, gw = session()
    ib.prepare_stock("SPY")
    ib.prepare_stock("SPY")
    assert gw.slept == [2.0]
    quiet_ib, quiet = session()
    quiet_ib.prepare_stock("SPY", settle_s=0)
    assert quiet.slept == []


# --------------------------------------------------------------------- quotes


def test_no_quote_before_the_first_tick_although_ib_async_defaults_to_type_1() -> None:
    ib, gw = session()
    assert ib.equity_quote("SPY") is None, "not prepared"
    ib.prepare_stock("SPY")
    assert getattr(gw.tickers[SPY], "marketDataType", None) == 1
    assert ib.equity_quote("SPY") is None, "NaN until the first tick: never realtime"
    assert ib.equity_market_data_type("SPY") is None


@pytest.mark.parametrize("kind", ["realtime", "delayed", "frozen"])
def test_quote_carries_cents_market_data_type_and_tick_time(kind: MarketDataTypeName) -> None:
    ib, gw = session()
    gw.set_market_data_type("SPY", kind)
    ib.prepare_stock("SPY")
    gw.push_tick("SPY", 67148, 67152)
    quote = ib.equity_quote("SPY")
    assert quote == EquityQuote(
        symbol="SPY",
        con_id=SPY,
        bid_cents=67148,
        ask_cents=67152,
        market_data_type=kind,
        as_of=gw.clock.utc(),
    )
    assert quote.realtime is (kind == "realtime")
    assert ib.equity_market_data_type("SPY") == kind


@pytest.mark.parametrize(
    ("code", "name"),
    [
        (1, "realtime"),
        (2, "frozen"),
        (3, "delayed"),
        (4, "unknown"),
        (0, "unknown"),
        (None, "unknown"),
        (True, "unknown"),
        ("1", "unknown"),
        (1.0, "unknown"),
    ],
)
def test_ibkr_market_data_type_codes(code: Any, name: str) -> None:
    # IBKR reqMarketDataType: 1 live, 2 frozen, 3 delayed, 4 delayed-frozen
    assert market_data_type_name(code) == name


@pytest.mark.parametrize(
    "ticker",
    [
        tick(math.nan, 671.52),
        tick(671.48, math.nan),
        tick(None, 671.52),
        tick(671.48, None),
        tick(0.0, 671.52),
        tick(-1.0, 671.52),
        tick(671.48, 0.0),
        tick(671.53, 671.52),
        tick(671.48, 671.52, time=None),
        tick(671.48, 671.52, time=datetime(2026, 10, 22, 14, 0)),
        tick(UNSET_DOUBLE, 671.52),
        tick("junk", 671.52),
        tick(math.inf, 671.52),
    ],
    ids=[
        "no-bid",
        "no-ask",
        "bid-none",
        "ask-none",
        "zero-bid",
        "minus-one-bid",
        "zero-ask",
        "crossed",
        "no-tick-time",
        "naive-time",
        "unset-bid",
        "junk-bid",
        "inf-bid",
    ],
)
def test_no_usable_two_sided_quote_is_none(ticker: Any) -> None:
    assert equity_quote_from_ticker("SPY", SPY, ticker) is None


def test_sub_penny_quotes_widen_to_the_penny_grid_and_time_is_utc() -> None:
    quote = equity_quote_from_ticker(
        "SPY", SPY, tick(671.485, 671.515, time=datetime(2026, 10, 22, 10, 0, tzinfo=ET), md=3)
    )
    assert quote is not None
    assert (quote.bid_cents, quote.ask_cents) == (67148, 67152), "bid floors, ask ceils"
    assert quote.as_of == TICK_AT and quote.as_of.tzinfo == UTC
    assert quote.market_data_type == "delayed"
    locked = equity_quote_from_ticker("SPY", SPY, tick(671.5, 671.5))
    assert locked is not None and (locked.bid_cents, locked.ask_cents) == (67150, 67150)


def test_quote_maps_onto_the_proposal_quote_contract() -> None:
    quote = EquityQuote(
        symbol="SPY",
        con_id=SPY,
        bid_cents=67148,
        ask_cents=67152,
        market_data_type="delayed",
        as_of=TICK_AT,
    )
    assert c.model_document(quote.to_contract()) == {
        "bid_cents": 67148,
        "ask_cents": 67152,
        "market_data_type": "delayed",
        "as_of": "2026-10-22T14:00:00Z",
    }


def test_release_stock_cancels_its_stream_and_forgets_the_pin() -> None:
    ib, gw = session()
    ib.prepare_stock("SPY")
    ib.release_stock("SPY")
    assert gw.mkt_cancelled == [SPY]
    assert not gw.md_live
    assert ib.equity_quote("SPY") is None
    ib.release_stock("SPY")  # unknown: a no-op
    assert gw.mkt_cancelled == [SPY]


DEBIT_PUT = LegStructure(
    id="dv",
    underlying="SPY",
    kind="debit_vertical",
    legs=[
        {"right": "P", "action": "BUY", "strike": "100", "expiry": date(2026, 10, 16)},
        {"right": "P", "action": "SELL", "strike": "95", "expiry": date(2026, 10, 16)},
    ],
    quantity=2,
    entry_date=date(2026, 9, 28),
    exit_deadline=date(2026, 10, 9),
    limit="1.00",
    exits={"touch": False, "breach": False},
)


def test_a_stream_shared_with_an_option_underlying_survives_either_release() -> None:
    ib, gw = session(
        con_ids={
            ("OPT", "SPY", "20261016", 100.0, "P"): 100,
            ("OPT", "SPY", "20261016", 95.0, "P"): 95,
        }
    )
    ib.prepare([DEBIT_PUT])
    ib.prepare_stock("SPY", SPY)
    assert [s.conId for s in gw.subscribed].count(SPY) == 1, "one request per conId"
    ib.release_stock("SPY")
    assert SPY not in gw.mkt_cancelled, "the option book still owns the stream"
    gw.push_tick("SPY", 67148, 67152)
    ib.release("dv")
    assert SPY in gw.mkt_cancelled


# ----------------------------------------------------------------- executions


def test_executions_map_onto_the_broker_execution_contract_with_late_commissions() -> None:
    ib, gw, trade = filled_entry()
    expected = contract_fixture("broker_execution.json") | {"perm_id": trade.order.permId}
    view = ib.executions()
    assert (view.status, view.error, view.unrepresentable) == ("complete", None, ())
    assert [c.model_document(r) for r in view.rows] == [expected | {"commission_micros": None}]
    gw.sleep(1)  # the commission report arrives after the execution
    assert [c.model_document(r) for r in ib.executions().rows] == [expected]


class FreshExecutionsGateway(Wave2Gateway):
    """As ib_async: ``reqExecutions`` answers with NEW ``Fill`` objects whose
    commission report is blank; the reports land on the session's cached
    ``fills()``. An execution of another client is only in the answer."""

    def reqExecutions(self, execFilter: Any = None) -> list[Any]:
        return [
            SimpleNamespace(
                contract=f.contract,
                execution=f.execution,
                commissionReport=SimpleNamespace(execId="", commission=0.0),
                time=f.time,
            )
            for f in super().reqExecutions()
        ]


def test_commissions_come_from_the_session_fills_not_the_fresh_answer() -> None:
    clock = SessionClock()
    gw = FreshExecutionsGateway(clock=clock)
    ib = _attached(gw)
    gw.script_fills(ENTRY, [FillStep()])
    stock_buy(gw)
    gw.sleep(1)
    gw.sleep(1)
    outside = odd_fill(con_id=SPY, sec_type="STK", execId="0000e0d5.7000bbbb.01.01")
    gw.executions.append(outside)  # another client's: never in fills()
    rows = ib.executions().rows
    assert [r.commission_micros for r in rows] == [1_000_000, None]
    assert [r.order_ref for r in rows] == [ENTRY, ""]


def test_a_correction_is_reported_beside_the_execution_it_corrects() -> None:
    ib, gw, trade = filled_entry()
    gw.sleep(1)
    first = trade.fills[0].execution.execId
    gw.correct_execution(first, quantity=1, price_micros=671_490_000)
    rows = ib.executions().rows
    assert [r.exec_id for r in rows] == [first, first[:-3] + ".02"]
    assert {r.base_exec_id for r in rows} == {"0000e0d5.6576d6a5.01"}
    assert (rows[1].quantity, rows[1].price_micros, rows[1].commission_micros) == (
        1,
        671_490_000,
        None,
    )


@pytest.mark.parametrize(
    ("fill", "code"),
    [
        (odd_fill(), "sec_type_unsupported:495512563"),
        (odd_fill(con_id=SPY, sec_type="STK", shares=1.5), "quantity_not_whole:756733"),
        (odd_fill(con_id=SPY, sec_type="STK", permId=0), "row_invalid:756733"),
        (odd_fill(con_id=SPY, sec_type="STK", time=None), "row_invalid:756733"),
    ],
    ids=["future", "fractional", "no-perm-id", "no-time"],
)
def test_an_unrepresentable_execution_is_reported_never_dropped(fill: Any, code: str) -> None:
    ib, gw, _ = filled_entry()
    if fill.execution.time is None:
        fill.time = None
    gw.executions.append(fill)
    view = ib.executions()
    assert len(view.rows) == 1
    assert view.unrepresentable == (code,)
    assert (view.complete, view.status) == (False, "unrepresentable")


def test_an_unreadable_execution_view_is_unavailable_with_the_error_class_only() -> None:
    ib, gw = session()
    gw.views_error = TimeoutError(SECRET)
    view = ib.executions()
    assert (view.status, view.error, view.rows) == ("unavailable", "TimeoutError", ())
    assert SECRET not in repr(view)


def test_executions_of_another_account_are_out_of_scope() -> None:
    ib, gw, _ = filled_entry()
    gw.executions.append(
        odd_fill(con_id=SPY, sec_type="STK", acctNumber=OTHER_ACCOUNT, execId="0000e0d5.1.01.01")
    )
    assert [r.account_id for r in ib.executions(ACCOUNT).rows] == [ACCOUNT]
    assert [r.account_id for r in ib.executions().rows] == [ACCOUNT, OTHER_ACCOUNT]


# ------------------------------------------------------- portfolio, positions


def test_portfolio_maps_onto_the_broker_portfolio_contract() -> None:
    ib, gw, _ = filled_entry()
    gw.sleep(1)  # commission in: average cost 671.98
    ib.prepare_stock("SPY")
    gw.push_tick("SPY", 67148, 67152)  # mark: the mid, 671.50
    view = ib.portfolio(as_of=AS_OF)
    assert view.status == "complete"
    assert [c.model_document(r) for r in view.rows] == [
        contract_fixture("broker_portfolio_item.json")
    ]


def test_portfolio_keeps_a_zero_row_after_a_close() -> None:
    ib, gw, _ = filled_entry()
    gw.hide_position(SPY, zero_row=True)
    [row] = ib.portfolio(as_of=AS_OF).rows
    assert (row.con_id, row.position) == (SPY, 0)


@pytest.mark.parametrize(
    ("fault", "code"),
    [
        (lambda gw: gw.override_position(SPY, Decimal("1.5")), "quantity_not_whole:756733"),
        (
            lambda gw: gw.add_broker_position("ES", 1, con_id=495512563, sec_type="FUT"),
            "sec_type_unsupported:495512563",
        ),
    ],
    ids=["fractional", "future"],
)
def test_unrepresentable_portfolio_rows_are_reported_never_dropped(fault: Any, code: str) -> None:
    ib, gw, _ = filled_entry()
    fault(gw)
    for view in (ib.portfolio(as_of=AS_OF), ib.position_items(as_of=AS_OF)):
        assert code in view.unrepresentable
        assert view.status == "unrepresentable"
        assert len(view.rows) + len(view.unrepresentable) == len(gw.positions())


def test_unreadable_account_views_are_unavailable_with_the_error_class_only() -> None:
    ib, gw, _ = filled_entry()
    gw.account_view_error = TimeoutError(SECRET)
    for view in (ib.portfolio(as_of=AS_OF), ib.position_items(as_of=AS_OF)):
        assert (view.status, view.error, view.rows) == ("unavailable", "TimeoutError", ())
        assert SECRET not in repr(view)


def test_positions_map_without_market_fields_and_keep_option_signs() -> None:
    ib, gw, _ = filled_entry()
    gw.position_rows.append(position_row(5001, -1.0, avg_cost=125.5, account=ACCOUNT, symbol="SPY"))
    gw.position_rows.append(position_row(5002, 1.0, account=OTHER_ACCOUNT, symbol="SPY"))
    view = ib.position_items(as_of=AS_OF, account=ACCOUNT)
    assert [c.model_document(r) for r in view.rows] == [
        {
            "account_id": ACCOUNT,
            "con_id": 5001,
            "sec_type": "OPT",
            "symbol": "SPY",
            "position": -1,
            "avg_cost_micros": 125_500_000,
            "market_price_micros": None,
            "market_value_cents": None,
            "as_of": "2026-10-22T14:04:00Z",
        },
        {
            "account_id": ACCOUNT,
            "con_id": SPY,
            "sec_type": "STK",
            "symbol": "SPY",
            "position": 2,
            "avg_cost_micros": 671_480_000,
            "market_price_micros": None,
            "market_value_cents": None,
            "as_of": "2026-10-22T14:04:00Z",
        },
    ]
    assert len(ib.position_items(as_of=AS_OF).rows) == 3


def test_a_naive_read_time_is_refused() -> None:
    ib, _ = session()
    with pytest.raises(ValueError, match="timezone-aware"):
        ib.portfolio(as_of=datetime(2026, 10, 22, 14, 4))
    with pytest.raises(ValueError, match="timezone-aware"):
        ib.position_items(as_of=datetime(2026, 10, 22, 14, 4))


# --------------------------------------------------------------------- cash


@pytest.mark.parametrize(
    ("value", "cents"),
    [("1000000.00", 100_000_000), ("12.345", 1234), ("-50.00", -5000), ("-12.345", -1235)],
)
def test_settled_cash_in_whole_cents_floored(value: str, cents: int) -> None:
    ib, gw = session()
    gw.account_values["SettledCash"] = value
    assert ib.settled_cash_cents(ACCOUNT) == cents


@pytest.mark.parametrize("value", [None, "nan", "junk", "inf"])
def test_settled_cash_absent_or_junk_is_none(value: str | None) -> None:
    ib, gw = session()
    if value is None:  # absent: neither SettledCash nor TotalCashValue
        del gw.account_values["SettledCash"]
        del gw.account_values["TotalCashValue"]
    else:  # junk: never replaced by TotalCashValue
        gw.account_values["SettledCash"] = value
    assert ib.settled_cash_cents(ACCOUNT) is None
    assert ib.account_cash(ACCOUNT) is None


def test_settled_cash_of_another_account_is_never_read() -> None:
    ib, _ = session()
    assert ib.settled_cash_cents(OTHER_ACCOUNT) is None
    assert ib.account_cash(OTHER_ACCOUNT) is None


def test_a_margin_type_account_reads_total_cash_value() -> None:
    """2026-10-21: the paper account (margin-type) has no SettledCash tag;
    the adapter reads its TotalCashValue by the books' shared rule and
    names the tag."""
    ib, gw = session()
    del gw.account_values["SettledCash"]
    gw.account_values["TotalCashValue"] = "48657.049"
    cash = ib.account_cash(ACCOUNT)
    assert cash is not None
    assert (cash.cents, cash.source) == (4_865_704, "TotalCashValue")
    assert ib.settled_cash_cents(ACCOUNT) == 4_865_704
    gw.account_values["SettledCash"] = "100.00"  # both: SettledCash wins
    cash = ib.account_cash(ACCOUNT)
    assert cash is not None
    assert (cash.cents, cash.source) == (10_000, "SettledCash")
    assert ib.settled_cash_cents(ACCOUNT) == 10_000


def test_the_adapter_reads_a_base_currency_cash_row(monkeypatch: pytest.MonkeyPatch) -> None:
    ib, gw = session()
    rows = [
        SimpleNamespace(account=ACCOUNT, tag="TotalCashValue", value="9.00", currency="BASE"),
        SimpleNamespace(account=ACCOUNT, tag="NetLiquidation", value="9.00", currency="USD"),
    ]
    monkeypatch.setattr(gw, "accountValues", lambda account="": rows)
    cash = ib.account_cash(ACCOUNT)
    assert cash is not None
    assert (cash.cents, cash.source) == (900, "TotalCashValue")


def test_working_stock_buys_commit_cash_until_filled() -> None:
    ib, gw = session()
    gw.accounts.append(OTHER_ACCOUNT)
    assert ib.working_stock_buy_cents(ACCOUNT) == 0
    stock_buy(gw)
    stock_buy(gw, qty=1, ref="trex:sup:other-001", account=OTHER_ACCOUNT)
    assert ib.working_stock_buy_cents(ACCOUNT) == 134_300
    gw.script_fills(ENTRY, [FillStep(1)])
    gw.sleep(1)
    assert ib.working_stock_buy_cents(ACCOUNT) == 67_150, "the unfilled share only"
    gw.script_fills(ENTRY, [FillStep()])
    gw.sleep(1)
    assert ib.working_stock_buy_cents(ACCOUNT) == 0


@pytest.mark.parametrize("limit", [UNSET_DOUBLE, 0.0, None])
def test_a_working_stock_buy_without_a_limit_is_an_unbounded_commitment(limit: Any) -> None:
    ib, gw = session()
    gw.trades.append(
        FakeTrade(
            contract=SimpleNamespace(secType="STK", conId=SPY),
            order=SimpleNamespace(
                action="BUY", totalQuantity=1.0, lmtPrice=limit, account="", orderId=1
            ),
            orderStatus=FakeStatus(),
        )
    )
    assert ib.working_stock_buy_cents(ACCOUNT) is None


# --------------------------------------------------------------------- probe


class ProbeGateway(Wave2Gateway):
    """A ``Wave2Gateway`` the probe can connect to (as ``ib_async.IB``)."""

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.is_connected = False
        self.connects: list[dict[str, Any]] = []
        self.disconnects = 0

    def connect(self, host: str, port: int, **kw: Any) -> None:
        self.connects.append({"host": host, "port": port, **kw})
        self.is_connected = True

    def disconnect(self) -> None:
        self.disconnects += 1
        self.is_connected = False


PROBE_NOW = datetime(2026, 10, 22, 14, 30, tzinfo=UTC)


def probe_gateway() -> ProbeGateway:
    gw = ProbeGateway()
    gw.add_stock("QQQ", QQQ)
    gw.set_market_data_type("QQQ", "delayed")
    gw.script_ticks("SPY", [(67148, 67152)])
    gw.script_ticks("QQQ", [(48010, 48014)])
    return gw


def test_probe_reports_market_data_type_per_symbol_read_only() -> None:
    gw = probe_gateway()
    calls: list[int] = []

    def factory() -> ProbeGateway:
        calls.append(1)
        return gw

    receipt = probe_script.probe(
        ["SPY", "QQQ", "ZZZZ"], ib_factory=factory, clock=lambda: PROBE_NOW, settle_s=4.0
    )
    assert calls == [1]
    [connect] = gw.connects
    assert (connect["port"], connect["clientId"], connect["readonly"]) == (4002, 75, True)
    assert gw.md_type_calls == [3]
    by_symbol = {row["symbol"]: row for row in receipt["symbols"]}
    assert by_symbol["SPY"] | {"as_of": None} == {
        "symbol": "SPY",
        "qualified": True,
        "con_id": SPY,
        "market_data_type": "realtime",
        "realtime": True,
        "two_sided": True,
        "bid_cents": 67148,
        "ask_cents": 67152,
        "as_of": None,
        "error": None,
    }
    assert by_symbol["QQQ"]["market_data_type"] == "delayed"
    assert by_symbol["QQQ"]["realtime"] is False
    assert by_symbol["ZZZZ"] | {"error": None} == {
        "symbol": "ZZZZ",
        "qualified": False,
        "con_id": None,
        "market_data_type": None,
        "realtime": False,
        "two_sided": False,
        "bid_cents": None,
        "ask_cents": None,
        "as_of": None,
        "error": None,
    }
    assert by_symbol["ZZZZ"]["error"] == "StockNotQualified"
    assert (receipt["realtime_symbols"], receipt["not_realtime_symbols"]) == (["SPY"], ["QQQ"])
    assert receipt["unqualified_symbols"] == ["ZZZZ"]
    assert (receipt["client_id"], receipt["orders_placed"]) == (75, 0)
    assert gw.trades == [], "a probe never places"
    assert gw.disconnects == 1
    assert not gw.md_live, "every stream released"


def test_probe_reports_no_data_when_no_tick_arrives() -> None:
    gw = ProbeGateway()
    receipt = probe_script.probe(["SPY"], ib_factory=lambda: gw, clock=lambda: PROBE_NOW)
    [row] = receipt["symbols"]
    assert (row["qualified"], row["market_data_type"], row["realtime"], row["two_sided"]) == (
        True,
        None,
        False,
        False,
    )
    assert receipt["not_realtime_symbols"] == ["SPY"]


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"client_id": SUPERVISED_CLIENT_ID}, "supervised_client_id_reserved"),
        ({"port": 4001}, "not_paper_gateway_port"),
    ],
)
def test_probe_refuses_the_desk_client_id_and_non_paper_ports(
    kwargs: dict[str, Any], reason: str
) -> None:
    calls: list[int] = []

    def factory() -> ProbeGateway:
        calls.append(1)
        return ProbeGateway()

    with pytest.raises(probe_script.ProbeRefused, match=reason):
        probe_script.probe(["SPY"], ib_factory=factory, clock=lambda: PROBE_NOW, **kwargs)
    assert calls == [], "refused before any connection"


def test_probe_refuses_a_session_without_paper_accounts() -> None:
    gw = probe_gateway()
    gw.accounts = ["U7654321"]
    with pytest.raises(probe_script.ProbeRefused, match="not_paper_session"):
        probe_script.probe(["SPY"], ib_factory=lambda: gw, clock=lambda: PROBE_NOW)
    assert gw.subscribed == [] and gw.qualify_calls == []
    assert gw.disconnects == 1


def test_probe_refuses_malformed_symbols() -> None:
    with pytest.raises(probe_script.ProbeRefused, match="symbol"):
        probe_script.probe(["spy"], ib_factory=ProbeGateway, clock=lambda: PROBE_NOW)


def test_probe_main_writes_a_receipt_and_never_overwrites(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    receipt = tmp_path / "probes" / "receipt.json"
    args = ["SPY", "QQQ", "--receipt", str(receipt)]
    rc = probe_script.main(args, ib_factory=probe_gateway, clock=lambda: PROBE_NOW)
    assert rc == 0
    written = json.loads(receipt.read_text())
    assert written["schema"] == "trex-ibkr-equity-quote-probe/1"
    assert written["realtime_symbols"] == ["SPY"]
    assert written["started_at"] == "2026-10-22T14:30:00Z"
    assert str(receipt) in capsys.readouterr().out
    connected: list[ProbeGateway] = []

    def counting() -> ProbeGateway:
        connected.append(probe_gateway())
        return connected[-1]

    assert probe_script.main(args, ib_factory=counting, clock=lambda: PROBE_NOW) == 3
    assert connected == [], "an existing receipt refuses before connecting"
    assert json.loads(receipt.read_text()) == written, "a receipt is never overwritten"
    assert [p.name for p in receipt.parent.iterdir()] == ["receipt.json"]


def test_probe_main_reports_error_classes_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def broken() -> ProbeGateway:
        gw = ProbeGateway()

        def connect(*_: Any, **__: Any) -> None:
            raise ConnectionRefusedError(SECRET)

        gw.connect = connect  # type: ignore[method-assign]
        return gw

    receipt = tmp_path / "r.json"
    rc = probe_script.main(["SPY", "--receipt", str(receipt)], ib_factory=broken)
    out = capsys.readouterr().out
    assert rc == 2
    assert json.loads(out) == {"error": "ConnectionRefusedError"}
    assert SECRET not in out
    assert not receipt.exists()
    refused = probe_script.main(["SPY", "--client-id", "83"], ib_factory=broken)
    assert refused == 3


def test_probe_default_receipt_lives_under_the_trex_state_dir() -> None:
    path = probe_script.default_receipt_path(PROBE_NOW)
    assert path == Path.home() / ".local/state/trex/probes/ibkr-equity-quotes-20261022T143000Z.json"
