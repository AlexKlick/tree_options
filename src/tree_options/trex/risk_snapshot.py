"""Books lane: ``trex-risk-snapshot/1`` from one broker session (pure).

Inputs: the ledger fold, the desk specs (stops and option structures), the
broker's position rows and settled cash, all read in one session at
``as_of``. Figures (WAVE2-CONTRACTS.md C2.5, default 18), integer cents,
every rounding toward MORE risk:

- ``open_loss_cents``, per open position:
  - our equity lot with a v2 equity spec: max(quantity x (mark - stop),
    gap reserve of the mark notional) (``equity_open_loss_cents``; at the
    entry limit this is exactly ``equity_max_loss_cents``);
  - our option structure with a spec: its remaining defined max loss, the
    spec's ``max_loss_per_package()`` x 100 x the packages still open
    (the largest leg quantity), counted once per structure, not per leg;
  - anything else (outside positions, outside or unattributed lots in a
    touched contract, a lot whose spec is gone, a contract whose lots
    disagree in sign): broker-priced at its full market value, |market
    value| (stocks may use |quantity| x mark);
- ``day_realized_loss_cents``: the ledger's realized losses on the ET
  session date of ``as_of`` (``LedgerFold.day_realized_loss_micros``);
- ``cash_cents``: IBKR ``SettledCash`` (``settled_cash_cents``);
- ``equity_gross_cents``: market value of every long stock row;
- ``outside_positions``: broker rows in contracts the ledger never touched.

Fail closed: an unreadable broker view or cash, a held contract with no
broker mark, an unpriceable row or an inconsistent ledger gives an
``unavailable`` snapshot (every figure None), which
``risk_snapshot_blockers`` reads as ``risk_snapshot_stale``: entries
block, exits never read it. A snapshot is stale 120 s after ``as_of``.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from tree_options.time.sessions import SESSION_TIMEZONE
from tree_options.trex.contracts.books import BrokerPortfolioItem
from tree_options.trex.contracts.canonical import model_bytes
from tree_options.trex.contracts.defaults import EQUITY_GAP_RESERVE_BPS
from tree_options.trex.contracts.desk_spec_v2 import EquityDeskSpecV2
from tree_options.trex.contracts.layout import Wave2RunDir
from tree_options.trex.contracts.risk import RiskSnapshot
from tree_options.trex.ledger import AnyDeskSpec, LedgerFold, Lot, write_atomic
from tree_options.trex.plan import LegStructure
from tree_options.trex.reconcile import broker_positions, outside_positions

__all__ = [
    "MICROS_PER_CENT",
    "RiskSnapshotBuild",
    "build_risk_snapshot",
    "equity_open_loss_cents",
    "option_open_loss_cents",
    "read_risk_snapshot",
    "settled_cash_cents",
    "write_risk_snapshot",
]

MICROS_PER_CENT = 10_000
SETTLED_CASH_TAG = "SettledCash"


class _Unpriceable(Exception):
    """A figure cannot be read from this session's broker views."""


def _ceil_div(numerator: int, denominator: int) -> int:
    return -(-numerator // denominator)


def equity_open_loss_cents(
    *,
    quantity: int,
    mark_micros: int,
    stop_cents: int,
    gap_reserve_bps: int = EQUITY_GAP_RESERVE_BPS,
) -> int:
    """max(quantity x (mark - stop), ceil(mark notional x bps / 10000)), in
    cents rounded up; the contract's entry arithmetic with the mark in
    place of the limit."""
    if not 0 <= gap_reserve_bps <= 10_000:
        raise ValueError("gap_reserve_bps must be within [0, 10000]")
    if quantity < 0 or mark_micros < 0:
        raise ValueError("an equity lot is long with a non-negative mark")
    stop_loss_micros = quantity * (mark_micros - stop_cents * MICROS_PER_CENT)
    gap_micros = _ceil_div(quantity * mark_micros * gap_reserve_bps, 10_000)
    return _ceil_div(max(stop_loss_micros, gap_micros), MICROS_PER_CENT)


def option_open_loss_cents(structure: LegStructure, remaining_packages: int) -> int:
    """The structure's defined max loss for the packages still open."""
    if remaining_packages < 0:
        raise ValueError("remaining packages cannot be negative")
    dollars = structure.max_loss_per_package() * 100 * remaining_packages
    return int((dollars * 100).to_integral_value(rounding=ROUND_CEILING))


def settled_cash_cents(rows: Iterable[Any], *, account_id: str | None = None) -> int | None:
    """IBKR ``SettledCash`` in USD cents (sub-cent rounds down); None when
    absent, unparseable or reported twice with two values."""
    found: set[int] = set()
    for row in rows:
        if getattr(row, "tag", None) != SETTLED_CASH_TAG:
            continue
        if getattr(row, "currency", None) != "USD":
            continue
        if account_id is not None and getattr(row, "account", None) != account_id:
            continue
        try:
            amount = Decimal(str(getattr(row, "value", "")))
        except InvalidOperation:
            return None
        if not amount.is_finite():
            return None
        found.add(int((amount * 100).to_integral_value(rounding=ROUND_FLOOR)))
    return found.pop() if len(found) == 1 else None


@dataclass(frozen=True)
class RiskSnapshotBuild:
    """The snapshot plus why it is unavailable (empty when complete)."""

    snapshot: RiskSnapshot
    problems: tuple[str, ...] = ()


def _unavailable(account_id: str, as_of: datetime, problems: list[str]) -> RiskSnapshotBuild:
    snapshot = RiskSnapshot(
        account_id=account_id,
        as_of=as_of,
        status="unavailable",
        open_loss_cents=None,
        day_realized_loss_cents=None,
        cash_cents=None,
        equity_gross_cents=None,
    )
    return RiskSnapshotBuild(snapshot=snapshot, problems=tuple(problems))


def _mark_micros(row: BrokerPortfolioItem | None, con_id: int) -> int:
    if row is not None:
        if row.market_price_micros is not None:
            return row.market_price_micros
        if row.market_value_cents is not None and row.position:
            value = abs(row.market_value_cents) * MICROS_PER_CENT
            return _ceil_div(value, abs(row.position))
    raise _Unpriceable(f"con_id {con_id}: no broker mark")


def _full_value_cents(row: BrokerPortfolioItem | None, quantity: int, con_id: int) -> int:
    """|quantity| of the contract at the broker's value (rounded up)."""
    if row is None or not row.position:
        raise _Unpriceable(f"con_id {con_id}: no broker mark for a held contract")
    if row.market_value_cents is not None:
        return _ceil_div(abs(quantity) * abs(row.market_value_cents), abs(row.position))
    if row.sec_type == "STK" and row.market_price_micros is not None:
        return _ceil_div(abs(quantity) * row.market_price_micros, MICROS_PER_CENT)
    raise _Unpriceable(f"con_id {con_id}: cannot price the {row.sec_type} position")


def _ours_open_loss(
    lots: Sequence[Lot],
    rows: dict[int, BrokerPortfolioItem],
    specs: dict[str, AnyDeskSpec],
    gap_reserve_bps: int,
) -> int:
    by_con: dict[int, list[Lot]] = defaultdict(list)
    for lot in lots:
        by_con[lot.con_id].append(lot)
    loss = 0
    packages: dict[str, int] = {}
    for con_id in sorted(by_con):
        con_lots = by_con[con_id]
        net = sum(lot.quantity for lot in con_lots)
        if net == 0:
            continue
        if con_lots[0].sec_type == "STK" and net < 0:
            raise _Unpriceable(f"con_id {con_id}: {net} shares; equities are long-only")
        row = rows.get(con_id)
        if any(lot.quantity * net < 0 for lot in con_lots):
            loss += _full_value_cents(row, net, con_id)
            continue
        for lot in con_lots:
            intent = lot.attribution.intent_id
            spec = specs.get(intent) if intent is not None else None
            if (
                lot.sec_type == "STK"
                and isinstance(spec, EquityDeskSpecV2)
                and spec.position.con_id == con_id
            ):
                loss += equity_open_loss_cents(
                    quantity=lot.quantity,
                    mark_micros=_mark_micros(row, con_id),
                    stop_cents=spec.position.exit_policy.stop_price_cents,
                    gap_reserve_bps=gap_reserve_bps,
                )
            elif lot.sec_type == "OPT" and intent is not None and spec is not None:
                if isinstance(spec, EquityDeskSpecV2):
                    loss += _full_value_cents(row, lot.quantity, con_id)
                else:
                    packages[intent] = max(packages.get(intent, 0), abs(lot.quantity))
            else:
                loss += _full_value_cents(row, lot.quantity, con_id)
    for intent in sorted(packages):
        spec = specs[intent]
        assert not isinstance(spec, EquityDeskSpecV2)
        loss += option_open_loss_cents(spec.structure, packages[intent])
    return loss


def _specs_by_intent(specs: Iterable[AnyDeskSpec]) -> dict[str, AnyDeskSpec]:
    out: dict[str, AnyDeskSpec] = {}
    for spec in specs:
        if out.setdefault(spec.intent_id, spec) != spec:
            raise _Unpriceable(f"intent {spec.intent_id}: two different desk specs")
    return out


def build_risk_snapshot(
    *,
    account_id: str,
    as_of: datetime,
    fold: LedgerFold,
    specs: Iterable[AnyDeskSpec],
    broker_rows: Sequence[BrokerPortfolioItem] | None,
    settled_cash_cents: int | None,
    session_day: date | None = None,
    gap_reserve_bps: int = EQUITY_GAP_RESERVE_BPS,
) -> RiskSnapshotBuild:
    """The account's risk from one broker session read at ``as_of``."""
    problems = []
    if broker_rows is None:
        problems.append("broker positions unreadable")
    if settled_cash_cents is None:
        problems.append("settled cash unreadable")
    if broker_rows is None or settled_cash_cents is None:
        return _unavailable(account_id, as_of, problems)
    rows = broker_positions(account_id, broker_rows)
    if rows is None:
        return _unavailable(account_id, as_of, ["broker positions contradict each other"])
    try:
        open_loss = _ours_open_loss(
            fold.account_lots(account_id), rows, _specs_by_intent(specs), gap_reserve_bps
        )
        outside = outside_positions(
            account_id=account_id, touched=fold.touched(account_id), broker_rows=list(rows.values())
        )
        for position in outside:
            open_loss += _full_value_cents(
                rows[position.con_id], position.quantity, position.con_id
            )
        equity_gross = sum(
            _full_value_cents(row, row.position, con_id)
            for con_id, row in sorted(rows.items())
            if row.sec_type == "STK" and row.position > 0
        )
    except _Unpriceable as exc:
        return _unavailable(account_id, as_of, [str(exc)])
    day = session_day or as_of.astimezone(SESSION_TIMEZONE).date()
    day_loss_micros = fold.day_realized_loss_micros(account_id, day)
    snapshot = RiskSnapshot(
        account_id=account_id,
        as_of=as_of,
        status="complete",
        open_loss_cents=open_loss,
        day_realized_loss_cents=_ceil_div(day_loss_micros, MICROS_PER_CENT),
        cash_cents=settled_cash_cents,
        equity_gross_cents=equity_gross,
        outside_positions=outside,
    )
    return RiskSnapshotBuild(snapshot=snapshot)


def write_risk_snapshot(run_dir: Wave2RunDir, snapshot: RiskSnapshot) -> Path:
    """Atomically replace ``ledger/risk-snapshot.json`` (canonical bytes)."""
    path = run_dir.risk_snapshot()
    write_atomic(path, model_bytes(snapshot))
    return path


def read_risk_snapshot(run_dir: Wave2RunDir) -> RiskSnapshot | None:
    """The latest snapshot; None when absent or unreadable (which
    ``risk_snapshot_blockers`` treats as stale)."""
    try:
        return RiskSnapshot.model_validate_json(run_dir.risk_snapshot().read_bytes())
    except (OSError, ValidationError, ValueError):
        return None
