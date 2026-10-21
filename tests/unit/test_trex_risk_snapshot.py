"""Books lane: ``trex-risk-snapshot/1`` from one broker session.

Oracles: the frozen ``risk_snapshot.json`` fixture (reproduced from the
fixture fill, the fixture portfolio row and a worthless outside option) and
hand arithmetic of the contract's open-loss rule in integer cents.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import tree_options.trex.contracts as c
from tests.unit.trex_books_support import (
    ACCOUNT,
    SPY,
    as_portfolio_item,
    at,
    buy,
    execution,
    gateway_executions,
    portfolio_item,
)
from tests.unit.trex_fakes import FillStep, Wave2Gateway, contract_fixture, entry_proposal
from tree_options.trex.contracts.desk_spec_v2 import OptionDeskSpecV2
from tree_options.trex.fills import fold_executions
from tree_options.trex.ledger import AttributionIndex, fill_events, fold_ledger
from tree_options.trex.risk_snapshot import (
    build_risk_snapshot,
    equity_open_loss_cents,
    option_open_loss_cents,
    read_risk_snapshot,
    settled_cash_cents,
    write_risk_snapshot,
)

AS_OF = at("2026-10-22T14:04:00+00:00")
NVDA_OPT = 712345678
QQQ = 320227571
EQUITY_SPEC = c.parse_desk_spec(contract_fixture("desk_spec_v2_equity.json"))
OPTION_SPEC = c.parse_desk_spec(contract_fixture("desk_spec_v2_option.json"))
SPECS = [EQUITY_SPEC, OPTION_SPEC]


def option_proposal() -> c.EntryProposal:
    return c.EntryProposal.model_validate(contract_fixture("proposal_option.json"))


INDEX = AttributionIndex.build(proposals=[entry_proposal(), option_proposal()], specs=SPECS)


def fold_of(executions: list[c.BrokerExecution]) -> Any:
    events = fill_events(fold_executions(executions), INDEX, recorded_at=AS_OF)
    return fold_ledger(events)


def nvda(**kw: Any) -> c.BrokerPortfolioItem:
    fields: dict[str, Any] = {
        "con_id": NVDA_OPT,
        "sec_type": "OPT",
        "symbol": "NVDA",
        "position": 1,
        "avg_cost_micros": 0,
        "market_price_micros": 0,
        "market_value_cents": 0,
    }
    fields.update(kw)
    return portfolio_item(**fields)


def snapshot(**kw: Any) -> Any:
    args: dict[str, Any] = {
        "account_id": ACCOUNT,
        "as_of": AS_OF,
        "fold": fold_of([execution()]),
        "specs": SPECS,
        "broker_rows": [portfolio_item(), nvda()],
        "settled_cash_cents": 4_865_704,
    }
    args.update(kw)
    return build_risk_snapshot(**args)


def test_reproduces_the_fixture_snapshot() -> None:
    build = snapshot()
    assert build.problems == ()
    assert c.model_document(build.snapshot) == contract_fixture("risk_snapshot.json")


# ------------------------------------------------------------ the open-loss rule


def test_equity_open_loss_is_the_gap_reserve_near_the_stop() -> None:
    # 2 x (671.50 - 665.00) = 13.00 < 20% of 1343.00 = 268.60
    assert equity_open_loss_cents(quantity=2, mark_micros=671_500_000, stop_cents=66_500) == 26_860


def test_equity_open_loss_is_the_stop_distance_far_above_the_stop() -> None:
    # 2 x (900.00 - 665.00) = 470.00 > 20% of 1800.00 = 360.00
    assert equity_open_loss_cents(quantity=2, mark_micros=900_000_000, stop_cents=66_500) == 47_000


def test_equity_open_loss_below_the_stop_keeps_the_gap_reserve() -> None:
    assert equity_open_loss_cents(quantity=1, mark_micros=600_000_000, stop_cents=66_500) == 12_000


def test_equity_open_loss_rounds_up_to_the_cent() -> None:
    # 333.333333 - 10.00 = 323.333333 -> 323.34; gap 66.6666666 -> 66.67
    assert equity_open_loss_cents(quantity=1, mark_micros=333_333_333, stop_cents=1_000) == 32_334
    assert (
        equity_open_loss_cents(
            quantity=1, mark_micros=333_333_333, stop_cents=33_000, gap_reserve_bps=2000
        )
        == 6_667
    )
    assert (
        equity_open_loss_cents(
            quantity=3, mark_micros=100_000_000, stop_cents=9_000, gap_reserve_bps=0
        )
        == 3_000
    )


def test_equity_open_loss_matches_the_entry_reservation_at_the_limit() -> None:
    effect = c.SupervisedEquityEffect.model_validate(contract_fixture("equity_effect.json"))
    at_limit = equity_open_loss_cents(
        quantity=effect.quantity,
        mark_micros=effect.limit_cents * 10_000,
        stop_cents=effect.exit_policy.stop_price_cents,
    )
    assert at_limit == c.equity_max_loss_cents(effect)


def test_option_open_loss_is_the_remaining_defined_max_loss() -> None:
    assert isinstance(OPTION_SPEC, OptionDeskSpecV2)
    structure = OPTION_SPEC.structure  # debit vertical capped at 1.20
    assert option_open_loss_cents(structure, 1) == 12_000
    assert option_open_loss_cents(structure, 3) == 36_000
    assert option_open_loss_cents(structure, 0) == 0


def leg(exec_id: str, con_id: int, side: str, price_micros: int, qty: int = 1) -> Any:
    return execution(
        exec_id=exec_id,
        sec_type="OPT",
        con_id=con_id,
        multiplier=100,
        side=side,
        price_micros=price_micros,
        quantity=qty,
        perm_id=77,
        order_ref="trex:sup:canary-001",
        commission_micros=650_000,
    )


def test_an_open_option_structure_reserves_its_defined_max_loss() -> None:
    fold = fold_of(
        [
            leg("0000e0d5.70000001.01.01", 700001, "BOT", 3_000_000),
            leg("0000e0d5.70000002.01.01", 700002, "SLD", 1_900_000),
        ]
    )
    rows = [
        portfolio_item(con_id=700001, sec_type="OPT", position=1, market_value_cents=29_000),
        portfolio_item(con_id=700002, sec_type="OPT", position=-1, market_value_cents=-18_500),
    ]
    build = snapshot(fold=fold, broker_rows=rows)
    assert build.problems == ()
    assert build.snapshot.open_loss_cents == 12_000  # once per structure, not per leg
    assert build.snapshot.equity_gross_cents == 0
    assert build.snapshot.outside_positions == ()


def test_outside_positions_count_broker_priced() -> None:
    rows = [
        portfolio_item(),
        nvda(position=-2, market_value_cents=-41_000),
        portfolio_item(con_id=QQQ, symbol="QQQ", position=5, market_value_cents=250_000),
    ]
    build = snapshot(broker_rows=rows)
    assert build.problems == ()
    snap = build.snapshot
    assert snap.open_loss_cents == 26_860 + 41_000 + 250_000
    assert snap.equity_gross_cents == 134_300 + 250_000
    assert snap.outside_positions == (
        c.OutsidePosition(con_id=QQQ, sec_type="STK", symbol="QQQ", quantity=5),
        c.OutsidePosition(con_id=NVDA_OPT, sec_type="OPT", symbol="NVDA", quantity=-2),
    )


def test_an_outside_stock_without_market_value_is_priced_from_its_mark() -> None:
    rows = [
        portfolio_item(),
        portfolio_item(
            con_id=QQQ,
            symbol="QQQ",
            position=3,
            market_price_micros=500_123_456,
            market_value_cents=None,
        ),
    ]
    build = snapshot(broker_rows=rows)
    # 3 x 500.123456 = 1500.370368 -> 1500.38
    assert build.snapshot.open_loss_cents == 26_860 + 150_038
    assert build.snapshot.equity_gross_cents == 134_300 + 150_038


def test_ledger_lots_without_a_stop_are_priced_at_full_value() -> None:
    build = snapshot(specs=[])  # the SPY lot has no equity spec: no stop known
    assert build.snapshot.open_loss_cents == 134_300


def test_outside_lots_in_a_touched_contract_are_priced_at_full_value() -> None:
    fold = fold_of(
        [
            execution(),
            execution(exec_id="0000e0d5.6576d6a7.01.01", order_ref="", perm_id=55, quantity=1),
        ]
    )
    rows = [portfolio_item(position=3, market_value_cents=201_450)]
    build = snapshot(fold=fold, broker_rows=rows)
    assert build.problems == ()
    # our 2 at the gap reserve of 2 x 671.50, the outside 1 at its value
    assert build.snapshot.open_loss_cents == 26_860 + 67_150


def test_day_realized_loss_comes_from_the_ledger_in_cents() -> None:
    exit_fill = execution(
        exec_id="0000e0d5.6576d6a7.01.01",
        side="SLD",
        perm_id=1234567891,
        order_ref="trex:desk:eq-spy-001",
        price_micros=671_000_000,
        commission_micros=1_000_001,
    )
    fold = fold_of([execution(), exit_fill])
    build = snapshot(fold=fold, broker_rows=[portfolio_item(position=0), nvda()])
    # 2 x (671.00 - 671.48) - 1.00 - 1.000001 = -2.960001 -> 296.0001 cents -> 297
    assert build.snapshot.day_realized_loss_cents == 297
    assert build.snapshot.open_loss_cents == 0
    assert build.snapshot.equity_gross_cents == 0


# ------------------------------------------------------------- fail closed


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        ({"broker_rows": None}, "broker positions unreadable"),
        ({"settled_cash_cents": None}, "settled cash unreadable"),
        ({"broker_rows": [nvda()]}, "no broker mark"),
        (
            {
                "broker_rows": [
                    portfolio_item(market_price_micros=None, market_value_cents=None),
                    nvda(),
                ]
            },
            "no broker mark",
        ),
        ({"broker_rows": [portfolio_item(), nvda(market_value_cents=None)]}, "cannot price"),
    ],
)
def test_unreadable_inputs_make_the_snapshot_unavailable(
    change: dict[str, Any], problem: str
) -> None:
    build = snapshot(**change)
    snap = build.snapshot
    assert snap.status == "unavailable"
    assert (snap.open_loss_cents, snap.cash_cents, snap.outside_positions) == (None, None, ())
    assert any(problem in p for p in build.problems), build.problems
    assert c.risk_snapshot_blockers(snap, AS_OF) == (c.Blocker.RISK_SNAPSHOT_STALE,)


def test_an_inconsistent_ledger_makes_the_snapshot_unavailable() -> None:
    fold = fold_of(
        [execution(exec_id="0000e0d5.6576d6a7.01.01", side="SLD", order_ref="trex:desk:eq-spy-001")]
    )
    build = snapshot(fold=fold, broker_rows=[portfolio_item(position=-2)])
    assert build.snapshot.status == "unavailable"
    assert any("long-only" in p for p in build.problems)


def test_stale_after_120_seconds() -> None:
    snap = snapshot().snapshot
    assert c.risk_snapshot_blockers(snap, at("2026-10-22T14:06:00+00:00")) == ()
    assert c.risk_snapshot_blockers(snap, at("2026-10-22T14:06:01+00:00")) == (
        c.Blocker.RISK_SNAPSHOT_STALE,
    )
    assert c.risk_snapshot_blockers(snap, at("2026-10-22T14:03:59+00:00")) == (
        c.Blocker.RISK_SNAPSHOT_STALE,
    )


# ------------------------------------------------------------ settled cash


def value(tag: str, val: str, currency: str = "USD", account: str = ACCOUNT) -> Any:
    return SimpleNamespace(account=account, tag=tag, value=val, currency=currency, modelCode="")


def test_settled_cash_from_the_gateway_account_values() -> None:
    gw = Wave2Gateway()
    assert settled_cash_cents(gw.accountValues(), account_id=ACCOUNT) == 100_000_000


@pytest.mark.parametrize(
    ("rows", "expected"),
    [
        ([value("SettledCash", "48657.04")], 4_865_704),
        ([value("SettledCash", "-12.5")], -1_250),
        ([value("SettledCash", "10.019")], 1_001),  # sub-cent cash rounds down
        ([value("SettledCash", "-10.011")], -1_002),
        ([value("NetLiquidation", "5.00")], None),
        ([value("SettledCash", "5.00", currency="EUR")], None),
        ([value("SettledCash", "5.00", account="DU0000002")], None),
        ([value("SettledCash", "n/a")], None),
        ([value("SettledCash", "NaN")], None),
        ([value("SettledCash", "5.00"), value("SettledCash", "6.00")], None),
        ([value("SettledCash", "5.00"), value("SettledCash", "5.00")], 500),
    ],
)
def test_settled_cash_parsing(rows: list[Any], expected: int | None) -> None:
    assert settled_cash_cents(rows, account_id=ACCOUNT) == expected


# ---------------------------------------------------------- against the fake


def test_gateway_session_snapshot() -> None:
    gw = Wave2Gateway()
    buy(gw, [FillStep()])
    gw.marks[SPY] = 680_000_000
    fold = fold_ledger(
        fill_events(fold_executions(gateway_executions(gw)), INDEX, recorded_at=AS_OF)
    )
    rows = [as_portfolio_item(item, AS_OF) for item in gw.portfolio()]
    build = build_risk_snapshot(
        account_id=ACCOUNT,
        as_of=AS_OF,
        fold=fold,
        specs=SPECS,
        broker_rows=rows,
        settled_cash_cents=settled_cash_cents(gw.accountValues(), account_id=ACCOUNT),
    )
    assert build.problems == ()
    snap = build.snapshot
    # 2 x (680.00 - 665.00) = 30.00 < 20% of 1360.00 = 272.00
    assert (snap.open_loss_cents, snap.equity_gross_cents) == (27_200, 136_000)
    assert snap.cash_cents == 100_000_000
    assert snap.day_realized_loss_cents == 0


# -------------------------------------------------------------- persistence


def test_snapshot_file_round_trip(tmp_path: Path) -> None:
    run_dir = c.Wave2RunDir(tmp_path)
    assert read_risk_snapshot(run_dir) is None
    snap = snapshot().snapshot
    path = write_risk_snapshot(run_dir, snap)
    assert path == run_dir.risk_snapshot()
    assert path.read_bytes() == c.model_bytes(snap)
    assert read_risk_snapshot(run_dir) == snap
    assert sorted(p.name for p in path.parent.iterdir()) == ["risk-snapshot.json"]
    path.write_text(json.dumps({"schema": "trex-risk-snapshot/1"}))
    assert read_risk_snapshot(run_dir) is None


def test_books_modules_import_without_a_broker_library() -> None:
    repo = Path(__file__).resolve().parents[2]
    modules = ", ".join(f"tree_options.trex.{m}" for m in ("fills", "ledger", "reconcile"))
    probe = (
        f"import sys, {modules}, tree_options.trex.risk_snapshot; "
        "sys.exit(1 if 'ib_async' in sys.modules else 0)"
    )
    env = {**os.environ, "PYTHONPATH": str(repo / "src")}
    done = subprocess.run(
        [sys.executable, "-B", "-c", probe], cwd=repo, env=env, capture_output=True, timeout=120
    )
    assert done.returncode == 0, done.stderr.decode()[-2000:]
