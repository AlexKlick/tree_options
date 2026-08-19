"""Workstream G: next-open long-only equity backtest."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from tree_options.backtest.equity import BacktestSignal, run_equity_backtest
from tree_options.data.actions import CorporateActionRecord
from tree_options.data.bars import BarRecord
from tree_options.schemas.security import (
    DelistingRecord,
    SecurityMasterRecord,
    TickerMappingRecord,
)
from tree_options.time.synthetic import SyntheticCalendar


def _calendar() -> SyntheticCalendar:
    return SyntheticCalendar(date(2024, 1, 2), 4)


def _at(session: date, hour: int = 23) -> datetime:
    return datetime(session.year, session.month, session.day, hour, tzinfo=UTC)


def _bar(
    security_id: str,
    session: date,
    *,
    open_: str,
    close: str,
) -> BarRecord:
    low = min(Decimal(open_), Decimal(close))
    high = max(Decimal(open_), Decimal(close))
    return BarRecord(
        security_id=security_id,
        session=session,
        open=Decimal(open_),
        high=high,
        low=low,
        close=Decimal(close),
        volume=1_000,
        source="synthetic-generator-v1",
        source_record_id=f"BAR-{security_id}-{session}",
        source_row_hash=f"HASH-{security_id}-{session}",
        snapshot_id="world-test",
        available_at=_at(session),
    )


def _master(
    security_id: str,
    first: date,
    *,
    last: date | None = None,
    bankruptcy: bool = False,
) -> SecurityMasterRecord:
    delisting = None
    if last is not None:
        delisting = DelistingRecord(
            delisting_session=last,
            reason="bankruptcy_11" if bankruptcy else "voluntary_delisting",
            final_price_available=not bankruptcy,
            available_at=_at(last),
        )
    return SecurityMasterRecord(
        security_id=security_id,
        listing_start=first,
        listing_end=last,
        exchange="NYSE",
        source="synthetic-generator-v1",
        available_at=_at(first),
        ticker_mappings=(
            TickerMappingRecord(
                security_id=security_id,
                ticker=security_id,
                effective_from=first,
                effective_to=last,
                available_at=_at(first),
            ),
        ),
        delisting=delisting,
    )


def test_top_quintile_executes_only_at_next_session_open() -> None:
    cal = _calendar()
    decision, execution = cal.sessions()[:2]
    securities = [f"S{i}" for i in range(5)]
    bars = [_bar(sid, execution, open_="10.00", close="10.00") for sid in securities]
    signals = [
        BacktestSignal(decision_session=decision, security_id=sid, score=float(i), label=0.01)
        for i, sid in enumerate(securities)
    ]

    result = run_equity_backtest(
        calendar=cal,
        bars=bars,
        master=[_master(sid, decision) for sid in securities],
        actions=[],
        signals=signals,
        initial_cash=Decimal("10000.00"),
        end_session=execution,
    )

    buys = [fill for fill in result.fills if fill.side == "buy"]
    assert {fill.contract_id for fill in buys} == {"S4"}
    assert {fill.execution_session for fill in result.fills} == {execution}
    assert {fill.execution_at for fill in result.fills} == {cal.session_open(execution)}


def test_fixed_five_basis_point_fee_is_applied_per_side() -> None:
    cal = _calendar()
    decision, execution = cal.sessions()[:2]
    result = run_equity_backtest(
        calendar=cal,
        bars=[_bar("S1", execution, open_="100.00", close="100.00")],
        master=[_master("S1", decision)],
        actions=[],
        signals=[BacktestSignal(decision, "S1", 1.0, 0.0)],
        initial_cash=Decimal("10050.00"),
        end_session=execution,
    )

    [buy] = result.fills
    assert buy.quantity == 100
    assert buy.notional() == Decimal("10000.00")
    assert buy.fees == Decimal("5.00")


def test_bankruptcy_delisting_resolves_at_final_bar_and_applies_loss() -> None:
    cal = _calendar()
    decision, execution, delisting = cal.sessions()[:3]
    result = run_equity_backtest(
        calendar=cal,
        bars=[
            _bar("S1", execution, open_="100.00", close="100.00"),
            _bar("S1", delisting, open_="100.00", close="10.00"),
        ],
        master=[_master("S1", decision, last=delisting, bankruptcy=True)],
        actions=[],
        signals=[BacktestSignal(decision, "S1", 1.0, -0.90)],
        initial_cash=Decimal("10050.00"),
        end_session=delisting,
    )

    sells = [fill for fill in result.fills if fill.side == "sell"]
    assert len(sells) == 1
    assert sells[0].execution_session == delisting
    assert sells[0].execution_at == cal.session_close(delisting)
    assert sells[0].price == Decimal("10.00")
    assert result.positions == ()
    assert result.summary.total_return == pytest.approx(-0.8956, abs=0.001)


def test_split_conversion_preserves_value_and_fifo_conservation_to_penny() -> None:
    cal = _calendar()
    decision, execution, split_session = cal.sessions()[:3]
    action = CorporateActionRecord(
        security_id="S1",
        kind="split",
        effective_session=split_session,
        ratio_numerator=2,
        ratio_denominator=1,
        source="synthetic-generator-v1",
        source_record_id="ACT-SPLIT",
        source_row_hash="HASH-SPLIT",
        snapshot_id="world-test",
        available_at=_at(execution),
    )
    result = run_equity_backtest(
        calendar=cal,
        bars=[
            _bar("S1", execution, open_="100.00", close="100.00"),
            _bar("S1", split_session, open_="50.00", close="50.00"),
        ],
        master=[_master("S1", decision)],
        actions=[action],
        signals=[BacktestSignal(decision, "S1", 1.0, 0.0)],
        initial_cash=Decimal("10050.00"),
        end_session=split_session,
    )

    assert result.positions == (("S1", 200),)
    assert result.terminal_equity == Decimal("10045.00")
    assert result.terminal_cash.quantize(Decimal("0.01")) == Decimal("45.00")
    assert result.dataset_provenance == "synthetic/v1"
