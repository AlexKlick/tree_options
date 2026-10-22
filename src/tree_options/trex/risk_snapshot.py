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
  - anything else whose loss is bounded by what it is worth (outside
    long positions, outside or unattributed long lots in a touched
    contract, a long lot whose spec is gone, a contract whose lots
    disagree in sign): broker-priced at its full market value, |market
    value| (stocks may use |quantity| x mark);
- ``day_realized_loss_cents``: the ledger's realized losses on the ET
  session date of ``as_of`` (``LedgerFold.day_realized_loss_micros``);
- ``cash_cents``: IBKR ``SettledCash`` (``settled_cash_cents``) minus the
  stock cash the broker figure does not show yet (W2c,
  ``unsettled_stock_cents``). Settled cash moves only when a trade settles
  (US stocks: T+1, the next session), so a same-day purchase is not in it
  and a second same-day entry would spend the same cash twice. The rule,
  conservative by construction: every booked stock execution whose
  settlement session (``equity_settlement_date`` of its ET trade date) is
  after the snapshot's ET date, or cannot be placed, takes out a purchase's
  price (net of its corrections) plus its commission, a sale its
  commission alone; a sale's proceeds are never added early. From the
  settlement session on the broker figure includes the trade and nothing
  is subtracted. Should a broker figure ever already include an unsettled
  purchase, the double count UNDERSTATES cash: it can only refuse an
  entry (``insufficient_cash``), never admit one, and exits never read it;
- ``equity_gross_cents``: market value of every long stock row;
- ``outside_positions``: broker rows in contracts the ledger never touched.

Undefined risk (``OUTSIDE_UNDEFINED_RISK``). A short option or a short
stock has no maximum loss, so |market value| is no reservation for it: it
would understate the open loss and let entries through. Every such
position makes the build carry the blocker ``outside_undefined_risk``
(``RiskSnapshotBuild.blockers``, with one ``UndefinedRisk`` row per
contract) and publishes the snapshot as ``unavailable``, so every reader
of the frozen contract fails closed (``risk_snapshot_stale``) until the
position is gone. The only short option exempt is one of OUR desk-spec'd
structures whose open lots still form whole packages (as many contracts as
the structure has legs, one quantity, the legs' long/short signs): the
spec's defined max loss bounds it. A v1-inbox structure (a desk spec with
no Wave 2 proposal) is ours too: the ledger attributes its fills to the
desk (``ledger.v1_desk_attribution``, W2c), so a v1 canary spread no
longer blocks every Wave 2 entry. Nothing else can be proven defined
risk from these rows: neither a broker position row nor a ledger lot
carries a strike, right or expiry, so an outside spread is not
recognizable and always blocks. The rules, per contract:

- ``outside_short_option``: a broker short option in a contract the
  ledger never touched;
- ``short_option_lot``: a ledger short option lot that no whole
  structure of ours explains (an outside or unattributed lot, our lot
  whose option spec is gone, a leg left without the rest of its package);
- ``unexplained_short_option``: a broker short option in a touched
  contract beyond the short quantity our whole structures hold there;
- ``short_stock``: any broker short stock row (a cash-only, long-only
  account holds none).

Fail closed: an unreadable broker view or cash, a held contract with no
broker mark, an unpriceable row, an inconsistent ledger or undefined risk
gives an ``unavailable`` snapshot (every figure None), which
``risk_snapshot_blockers`` reads as ``risk_snapshot_stale``: entries
block, exits never read it. A snapshot is stale 120 s after ``as_of``.
``RiskSnapshotBuild.entry_blockers`` is the contract's blockers plus the
build's own codes (``outside_undefined_risk``; a plain string until the
vocabulary lane adds the ``Blocker`` member).
"""

from __future__ import annotations

import bisect
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Final, Literal

from pydantic import ValidationError

from tree_options.time.sessions import SESSION_TIMEZONE
from tree_options.trex.contracts.books import BrokerPortfolioItem
from tree_options.trex.contracts.canonical import model_bytes
from tree_options.trex.contracts.defaults import EQUITY_GAP_RESERVE_BPS
from tree_options.trex.contracts.desk_spec_v2 import EquityDeskSpecV2
from tree_options.trex.contracts.layout import Wave2RunDir
from tree_options.trex.contracts.risk import RiskSnapshot, risk_snapshot_blockers
from tree_options.trex.ledger import AnyDeskSpec, LedgerFold, Lot, write_atomic
from tree_options.trex.plan import LegStructure
from tree_options.trex.reconcile import broker_positions, outside_positions

__all__ = [
    "MICROS_PER_CENT",
    "OUTSIDE_UNDEFINED_RISK",
    "RiskSnapshotBuild",
    "UndefinedRisk",
    "build_risk_snapshot",
    "equity_open_loss_cents",
    "equity_settlement_date",
    "option_open_loss_cents",
    "read_risk_snapshot",
    "settled_cash_cents",
    "unsettled_stock_cents",
    "write_risk_snapshot",
]

MICROS_PER_CENT = 10_000
SETTLED_CASH_TAG = "SettledCash"
#: entry blocker: the account holds a position whose loss has no bound
#: (wire string; ``Blocker.OUTSIDE_UNDEFINED_RISK`` once the vocabulary
#: lane adds it, equal by ``StrEnum`` value)
OUTSIDE_UNDEFINED_RISK: Final = "outside_undefined_risk"

UndefinedReason = Literal[
    "outside_short_option", "short_option_lot", "unexplained_short_option", "short_stock"
]


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
class UndefinedRisk:
    """One contract whose open loss has no bound (see the module doc)."""

    con_id: int
    sec_type: Literal["OPT", "STK"]
    #: the short quantity found: the lot's, or the broker row's position
    quantity: int
    reason: UndefinedReason
    #: our intent when the lot is ours (spec gone, broken package), else None
    intent_id: str | None = None

    def describe(self) -> str:
        owner = f" of {self.intent_id}" if self.intent_id else ""
        return (
            f"{OUTSIDE_UNDEFINED_RISK}: con_id {self.con_id} {self.sec_type} "
            f"{self.quantity}{owner} ({self.reason})"
        )


@dataclass(frozen=True)
class RiskSnapshotBuild:
    """The snapshot to publish, why it is unavailable (empty when complete)
    and the entry blockers it carries beyond the contract's."""

    snapshot: RiskSnapshot
    problems: tuple[str, ...] = ()
    #: ``(OUTSIDE_UNDEFINED_RISK,)`` while ``undefined_risk`` is non-empty
    blockers: tuple[str, ...] = ()
    undefined_risk: tuple[UndefinedRisk, ...] = ()

    def entry_blockers(self, now: datetime) -> tuple[str, ...]:
        """The contract's snapshot blockers, then this build's own codes."""
        contract = tuple(b.value for b in risk_snapshot_blockers(self.snapshot, now))
        return (*contract, *self.blockers)


def _unavailable(
    account_id: str,
    as_of: datetime,
    problems: list[str],
    undefined: Sequence[UndefinedRisk] = (),
) -> RiskSnapshotBuild:
    snapshot = RiskSnapshot(
        account_id=account_id,
        as_of=as_of,
        status="unavailable",
        open_loss_cents=None,
        day_realized_loss_cents=None,
        cash_cents=None,
        equity_gross_cents=None,
    )
    return RiskSnapshotBuild(
        snapshot=snapshot,
        problems=tuple(problems),
        blockers=(OUTSIDE_UNDEFINED_RISK,) if undefined else (),
        undefined_risk=tuple(undefined),
    )


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


def _whole_packages(structure: LegStructure, lots: Sequence[Lot]) -> bool:
    """One intent's open option lots are whole packages of ``structure``:
    one lot per leg, one quantity, the legs' long/short signs. A spec names
    no leg con_ids, so this is as far as the rows can prove it."""
    if len(lots) != len(structure.legs) or len({abs(lot.quantity) for lot in lots}) != 1:
        return False
    held = sorted(1 if lot.quantity > 0 else -1 for lot in lots)
    return held == sorted(leg.sign for leg in structure.legs)


def _undefined_risk(
    lots: Sequence[Lot],
    rows: Mapping[int, BrokerPortfolioItem],
    specs: Mapping[str, AnyDeskSpec],
    touched: frozenset[int],
) -> tuple[UndefinedRisk, ...]:
    """Every contract holding a short with no defined max loss (one row per
    contract, the ledger's reason first, ascending con_id)."""
    found: dict[int, UndefinedRisk] = {}
    ours: dict[str, list[Lot]] = defaultdict(list)
    for lot in lots:
        if lot.sec_type != "OPT" or not lot.quantity:
            continue
        intent = lot.attribution.intent_id
        if intent is not None:
            ours[intent].append(lot)
        elif lot.quantity < 0:
            found.setdefault(
                lot.con_id, UndefinedRisk(lot.con_id, "OPT", lot.quantity, "short_option_lot")
            )
    defined_short: dict[int, int] = defaultdict(int)
    for intent in sorted(ours):
        spec = specs.get(intent)
        whole = (
            spec is not None
            and not isinstance(spec, EquityDeskSpecV2)
            and _whole_packages(spec.structure, ours[intent])
        )
        for lot in ours[intent]:
            if lot.quantity > 0:
                continue
            if whole:
                defined_short[lot.con_id] -= lot.quantity
            else:
                found.setdefault(
                    lot.con_id,
                    UndefinedRisk(lot.con_id, "OPT", lot.quantity, "short_option_lot", intent),
                )
    for con_id, row in rows.items():
        reason: UndefinedReason
        if row.position >= 0:
            continue
        if row.sec_type == "STK":
            reason = "short_stock"
        elif -row.position > defined_short[con_id]:
            reason = "unexplained_short_option" if con_id in touched else "outside_short_option"
        else:
            continue
        found.setdefault(con_id, UndefinedRisk(con_id, row.sec_type, row.position, reason))
    return tuple(found[con_id] for con_id in sorted(found))


def equity_settlement_date(trade_date: date) -> date | None:
    """The session a US stock trade of ``trade_date`` settles on (T+1: the
    first session after it, by the trex calendar); None when the calendar
    cannot place it (the caller counts the trade as unsettled)."""
    from tree_options.time.calendar import CalendarError
    from tree_options.trex.clock import session_calendar

    try:
        sessions = session_calendar().sessions()
    except (CalendarError, OSError, ValueError):
        return None
    index = bisect.bisect_right(sessions, trade_date)
    return sessions[index] if index < len(sessions) else None


def unsettled_stock_cents(
    fold: LedgerFold,
    account_id: str,
    as_of: datetime,
    *,
    settlement_date: Callable[[date], date | None] = equity_settlement_date,
) -> int:
    """What the account's booked stock executions took out of cash that the
    broker's settled cash does not show yet, in cents rounded up: every
    ``LedgerFold.stock_cash_out`` whose settlement session (from its ET
    trade date) is after the ET date of ``as_of``, or cannot be placed."""
    day = as_of.astimezone(SESSION_TIMEZONE).date()
    micros = 0
    for out in fold.stock_cash_out:
        if out.account_id != account_id:
            continue
        settles = settlement_date(out.at.astimezone(SESSION_TIMEZONE).date())
        if settles is None or day < settles:
            micros += out.micros
    return _ceil_div(micros, MICROS_PER_CENT)


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
    settlement_date: Callable[[date], date | None] = equity_settlement_date,
) -> RiskSnapshotBuild:
    """The account's risk from one broker session read at ``as_of``.
    ``cash_cents`` is ``settled_cash_cents`` minus the ledger's unsettled
    stock cash (``unsettled_stock_cents``; module docstring)."""
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
        by_intent = _specs_by_intent(specs)
    except _Unpriceable as exc:
        return _unavailable(account_id, as_of, [str(exc)])
    lots = fold.account_lots(account_id)
    touched = fold.touched(account_id)
    undefined = _undefined_risk(lots, rows, by_intent, touched)
    problems = [risk.describe() for risk in undefined]
    try:
        open_loss = _ours_open_loss(lots, rows, by_intent, gap_reserve_bps)
        outside = outside_positions(
            account_id=account_id, touched=touched, broker_rows=list(rows.values())
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
        problems.append(str(exc))
    if problems:
        return _unavailable(account_id, as_of, problems, undefined)
    day = session_day or as_of.astimezone(SESSION_TIMEZONE).date()
    day_loss_micros = fold.day_realized_loss_micros(account_id, day)
    snapshot = RiskSnapshot(
        account_id=account_id,
        as_of=as_of,
        status="complete",
        open_loss_cents=open_loss,
        day_realized_loss_cents=_ceil_div(day_loss_micros, MICROS_PER_CENT),
        cash_cents=settled_cash_cents
        - unsettled_stock_cents(fold, account_id, as_of, settlement_date=settlement_date),
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
