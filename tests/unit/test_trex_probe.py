"""C3: the broker-side account read + discovery probe (FakeIB, no gateway)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from tree_options.trex.account import AccountSnapshot
from tree_options.trex.ibkr import IbkrTrex


@dataclass
class FakeRow:
    tag: str
    value: Any
    currency: str = "USD"
    account: str = "DUT143714"


class FakeIb:
    """Duck-typed ib_async.IB covering only the account surface."""

    def __init__(self, rows: list[FakeRow], summary: list[FakeRow] | None = None) -> None:
        self._rows = rows
        self._summary = summary if summary is not None else []
        self.summary_calls = 0

    def accountValues(self) -> list[FakeRow]:
        return self._rows

    def managedAccounts(self) -> list[str]:
        return ["DUT143714"]

    def accountSummary(self, account: str) -> list[FakeRow]:
        self.summary_calls += 1
        return self._summary


def _ibk(fake: FakeIb) -> IbkrTrex:
    ibk = IbkrTrex()
    ibk._ib = fake  # bypass connect(): the fake IS the gateway session
    return ibk


class TestAccountRead:
    def test_reads_cached_account_values(self) -> None:
        fake = FakeIb(
            [
                FakeRow("NetLiquidation", "12345.67"),
                FakeRow("TotalCashValue", "11111.11"),
                FakeRow("BuyingPower", "22222.22"),
                FakeRow("ExcessLiquidity", "9999.99"),
            ]
        )
        snap = _ibk(fake).account_snapshot()
        assert snap == AccountSnapshot(
            account_id="DUT143714",
            net_liquidation=Decimal("12345.67"),
            cash=Decimal("11111.11"),
            buying_power=Decimal("22222.22"),
            currency="USD",
            ts=snap.ts if snap else None,  # type: ignore[union-attr]
        )
        assert fake.summary_calls == 0  # cache had everything: no summary request

    def test_nan_or_missing_tag_returns_none_never_fabricates(self) -> None:
        nan = float("nan")
        fake = FakeIb(
            [
                FakeRow("NetLiquidation", nan),
                FakeRow("TotalCashValue", "11111.11"),
                FakeRow("BuyingPower", "22222.22"),
            ]
        )
        assert _ibk(fake).account_snapshot() is None
        partial = FakeIb([FakeRow("NetLiquidation", "1.00")])
        assert _ibk(partial).account_snapshot() is None

    def test_empty_cache_falls_back_to_account_summary(self) -> None:
        summary = [
            FakeRow("NetLiquidation", "500.00"),
            FakeRow("TotalCashValue", "400.00"),
            FakeRow("BuyingPower", "300.00"),
        ]
        fake = FakeIb([], summary=summary)
        snap = _ibk(fake).account_snapshot()
        assert snap is not None
        assert snap.net_liquidation == Decimal("500.00")
        assert fake.summary_calls == 1

    def test_no_accounts_at_all_is_none(self) -> None:
        fake = FakeIb([], summary=[])
        assert _ibk(fake).account_snapshot() is None


class TestProbeJson:
    def test_probe_reports_account_chains_greeks_without_writing(self, tmp_path) -> None:
        from tree_options.trex.discovery.probe import probe_json

        fake = _GatewayProbe()
        ibk = IbkrTrex()
        ibk._ib = fake
        report = probe_json(ibk, ["NVDA"], now=None)
        assert report["account"]["account_id"] == "DUT143714"
        assert report["chains"]["NVDA"]["expirations"] == 3
        # 5 strikes -> mid window picks 4; every 3rd subscription lacks greeks
        assert report["greeks"]["rows_total"] == 4
        assert report["greeks"]["rows_with_greeks"] == 3
        assert list(tmp_path.iterdir()) == []  # writes nothing

    def test_probe_reports_chain_failure_honestly(self) -> None:
        from tree_options.trex.discovery.probe import probe_json

        fake = _GatewayProbe(chain_ok=False)
        ibk = IbkrTrex()
        ibk._ib = fake
        report = probe_json(ibk, ["NVDA"], now=None)
        assert report["chains"]["NVDA"]["expirations"] == 0
        assert report["data_quality"]["chains_available"] is False


@dataclass
class FakeOptParam:
    exchange: str
    underlyingConId: int
    tradingClass: str
    multiplier: str
    expirations: list[str]
    strikes: list[float]


@dataclass
class FakeTicker:
    bid: Any = None
    ask: Any = None
    modelGreeks: Any = None


@dataclass
class FakeGreeks:
    delta: float | None = None


class _GatewayProbe:
    """Gateway session fake: account rows + one healthy option chain."""

    def __init__(self, chain_ok: bool = True) -> None:
        self.chain_ok = chain_ok
        self._subs = 0
        self.acct = FakeIb(
            [
                FakeRow("NetLiquidation", "12345.67"),
                FakeRow("TotalCashValue", "11111.11"),
                FakeRow("BuyingPower", "22222.22"),
            ]
        )

    def accountValues(self) -> list[FakeRow]:
        return self.acct.accountValues()

    def managedAccounts(self) -> list[str]:
        return ["DUT143714"]

    def accountSummary(self, account: str) -> list[FakeRow]:
        return []

    def qualifyContracts(self, *contracts: Any) -> list[Any]:
        for c in contracts:
            c.conId = 1000 + len(getattr(c, "localSymbol", "") or "")
        return list(contracts)

    def reqSecDefOptParams(self, symbol: str, _fut: str, _sectype: str, _conid: int):
        if not self.chain_ok:
            return []
        return [
            FakeOptParam(
                exchange="SMART",
                underlyingConId=1000,
                tradingClass=symbol,
                multiplier="100",
                expirations=["20261016", "20261120", "20261218"],
                strikes=[150.0, 160.0, 170.0, 180.0, 190.0],
            )
        ]

    def reqMktData(self, contract: Any, *_args: Any, **_kwargs: Any) -> FakeTicker:
        self._subs += 1  # every third row lacks greeks: exercises the counter
        greeks = FakeGreeks(delta=-0.3) if self._subs % 3 != 0 else None
        return FakeTicker(bid=1.0, ask=1.2, modelGreeks=greeks)

    def sleep(self, _seconds: float) -> None:
        return None
