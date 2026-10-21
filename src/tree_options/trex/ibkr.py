"""Thin IBKR adapter for trex (ib_async over the local gateway).

Kept as pure I/O: no decisions live here. ``ib_async`` is imported lazily
so the research/test environment never needs it; the decision core stays
importable without a broker.

Every structure is a PACKAGE of option legs, priced and traded in its
DEBIT ORIENTATION (``plan.LegStructure.package_legs``): the direction whose
value is never negative, so every limit sent is positive. Debit kinds open
by BUYING the package; credit kinds open by SELLING it at a positive price.
(IBKR also takes a credit combo as a BUY at a negative price, but a SELL at
a negative price silently becomes a debit: positive prices only remove that
trap.) Long singles trade as plain OPT orders, never a one-leg BAG. Legs
are keyed by (structure id, leg index); the index is the authored order,
which debit orientation keeps.

Package pricing is derived from leg NBBOs rather than a combo ticker — on
125-wide wings the combo book is empty and its "mid" is fiction:

    package_bid = sum(BUY-leg bids) - sum(SELL-leg asks)  (best executable sale)
    package_ask = sum(BUY-leg asks) - sum(SELL-leg bids)  (best executable purchase)

For the legacy put vertical that is long_bid - short_ask / long_ask -
short_bid, exactly as before. The legacy PutSpread methods (prepare,
combo_quote, snapshot, place_combo, open_combo_trades, structure_for_bag)
keep their behavior and wire content byte for byte
(tests/unit/test_trex_legacy_characterization.py).

Connection defaults target IB Gateway in paper mode (docker, port 4002).

Wave 2 stocks (docs/product/WAVE2-CONTRACTS.md, lane B) add whole-share
primitives beside the option packages, still pure I/O:

- ``prepare_stock`` qualifies ``Stock(symbol, SMART, USD)`` and PINS its
  conId (a different conId than the caller's refuses), then subscribes
  one quote stream per conId, shared with an option underlying's spot;
- ``equity_quote`` is the NBBO in integer cents with the market-data type
  IBKR reports on the ticker and the instant of its latest tick; no tick
  yet means no quote (ib_async defaults a fresh ticker to type 1);
- ``executions`` / ``portfolio`` / ``position_items`` map broker rows onto
  ``contracts.books`` (micros for broker prices). A row the contracts
  cannot represent is listed in ``unrepresentable``, never dropped; an
  unreadable view carries its error CLASS only;
- ``settled_cash_cents`` / ``working_stock_buy_cents`` are the cash-only
  facts the supervised equity preflight checks.

The contracts package imports this module (``contracts.books``), so it is
imported lazily here, never at module level.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN, Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, Literal

from tree_options.trex.account import AccountSnapshot
from tree_options.trex.engine import ComboQuote, Snapshot
from tree_options.trex.plan import Leg, LegStructure, PutSpread, validate_package_order

if TYPE_CHECKING:
    from tree_options.trex.contracts.books import BrokerExecution, BrokerPortfolioItem
    from tree_options.trex.contracts.proposals import Quote

log = logging.getLogger("trex.ibkr")

GATEWAY_PAPER_PORT = 4002
GATEWAY_LIVE_PORT = 4001
# orders placed through place() carry this tag + the structure id in
# Order.orderRef, which IBKR keeps with the order across sessions. It is
# the ONLY proof that a working order belongs to a multi-leg structure
# (structure_for_trade): contracts coincide across structures and books.
ORDER_REF_PREFIX = "trex:"
# ib_async marks "no value" with UNSET_DOUBLE (sys.float_info.max)
_UNSET_MARGIN = Decimal("1e300")
# IBKR answers a what-if within seconds when it answers at all; ib_async
# 2.1.0 ends the request only on a reply whose initMarginChange is set
# (an UNSET-only reply leaves it pending) and its RequestTimeout is 0
WHATIF_TIMEOUT_S = 10.0
# ib_async.OrderStatus.DoneStates: statuses after which an order never works again
DONE_STATES: frozenset[str] = frozenset({"Filled", "Cancelled", "ApiCancelled", "Inactive"})


def order_tag(structure_id: str) -> str:
    """The Order.orderRef that marks an order as this structure's."""
    return ORDER_REF_PREFIX + structure_id


def tagged_structure(order_ref: object) -> str | None:
    """The structure id a trex order tag names; None for an untagged or
    foreign reference (an order no trex desk runner placed)."""
    ref = str(order_ref or "")
    if not ref.startswith(ORDER_REF_PREFIX):
        return None
    return ref[len(ORDER_REF_PREFIX) :] or None


#: orderRef prefixes owned by the SUPERVISED lane (clientId 83): its
#: entries (`trex:sup:<intent>`) and exits (`trex:desk:<sid>`). The legacy
#: runners never adopt these; defined HERE (not imported from their owners)
#: because those modules import this one - a test pins them against the
#: owners' constants so they cannot drift.
SUPERVISED_REF_PREFIXES = ("trex:sup:", "trex:desk:")


def is_supervised_ref(order_ref: object) -> bool:
    """True when an order reference belongs to the supervised lane."""
    return str(order_ref or "").startswith(SUPERVISED_REF_PREFIXES)


def bag_signature(combo_legs: Any) -> tuple[tuple[int, str, int], ...]:
    """Order-free identity of a BAG: (conId, action, ratio) per leg."""
    return tuple(sorted((int(g.conId), str(g.action), int(g.ratio)) for g in combo_legs))


def select_account(rows: list[BrokerPosition], account: str | None) -> list[BrokerPosition]:
    """Positions of ONE account: the named one, else the only one present.
    Positions spanning several accounts without a selection are refused: a
    long leg in one account and its short in another are two exposures,
    not one covered package."""
    if account is not None:
        return [r for r in rows if r.account == account]
    if len({r.account for r in rows}) > 1:
        raise ValueError(f"positions span {len({r.account for r in rows})} accounts; select one")
    return rows


@dataclass(frozen=True)
class OrderRef:
    """Local handle for an order this process placed."""

    structure_id: str
    side: str  # "BUY" | "SELL"
    qty: int
    limit: Decimal
    trade: Any  # ib_async.Trade


@dataclass(frozen=True)
class OrderStatusInfo:
    status: str  # ib orderStatus string, e.g. "Filled", "Cancelled"
    filled: int
    avg_fill_price: Decimal


@dataclass(frozen=True)
class BrokerPosition:
    """One account position as the gateway reports it (any secType)."""

    account: str
    con_id: int
    sec_type: str
    symbol: str
    right: str  # "" for non-options
    strike: Decimal
    expiry: str  # lastTradeDateOrContractMonth, YYYYMMDD for options
    qty: Decimal  # signed: + long, - short
    avg_cost: Decimal  # IBKR avgCost: per contract, multiplier included


@dataclass(frozen=True)
class _Package:
    """What the adapter holds for a structure: its legs in debit orientation."""

    sid: str
    underlying: str
    kind: str
    legs: tuple[Leg, ...]

    @property
    def single(self) -> bool:
        return self.kind == "long_single"


@dataclass
class _Subscription:
    """One market-data stream per conId, shared by every structure (leg or
    spot) that needs it. ib_async keeps a single reqId per ticker, so a
    second reqMktData for the same contract would orphan the first request
    (cancelMktData cancels only the latest): subscribe once, count owners,
    cancel when the last one releases."""

    contract: Any
    ticker: Any
    owners: int = 0


def _package(struct: PutSpread | LegStructure) -> _Package:
    if isinstance(struct, PutSpread):
        return _Package(struct.id, struct.underlying, "debit_vertical", struct.package_legs())
    return _Package(struct.id, struct.underlying, struct.kind, struct.package_legs())


def _d(value: float | int | None) -> Decimal:
    return Decimal(str(value)) if value is not None else Decimal(0)


def _margin(raw: Any) -> Decimal | None:
    """A what-if margin string as a Decimal; None for no number (empty,
    junk, NaN, infinite, or ib_async's UNSET sentinel)."""
    if raw is None:
        return None
    try:
        value = Decimal(str(raw).strip())
    except InvalidOperation:
        return None
    if not value.is_finite() or abs(value) >= _UNSET_MARGIN:
        return None
    return value


# -- Wave 2 stocks: quotes, broker facts ------------------------------------

MarketDataTypeName = Literal["realtime", "delayed", "frozen", "unknown"]
#: IBKR ``reqMarketDataType`` codes as a ticker reports them; anything else
#: (4 delayed-frozen, an absent or junk code) is "unknown"
MARKET_DATA_TYPE_NAMES: dict[int, MarketDataTypeName] = {1: "realtime", 2: "frozen", 3: "delayed"}
#: seconds a NEW stock stream may take to deliver its first tick
STOCK_SETTLE_S = 2.0
#: security types ``contracts.books`` can represent
_EXEC_SEC_TYPES = frozenset({"OPT", "STK", "BAG"})
_POSITION_SEC_TYPES = frozenset({"OPT", "STK"})
_CENT = Decimal(100)
_MICRO = Decimal(1_000_000)
ViewStatus = Literal["complete", "unrepresentable", "unavailable"]


class StockNotQualified(RuntimeError):
    """IBKR resolved no unique STK contract for the symbol."""


class StockConIdMismatch(ValueError):
    """The stock qualified to a conId other than the pinned one."""


def market_data_type_name(code: object) -> MarketDataTypeName:
    """The contract name of an IBKR market-data type code (ints only)."""
    if isinstance(code, bool) or not isinstance(code, int):
        return "unknown"
    return MARKET_DATA_TYPE_NAMES.get(code, "unknown")


def _number(raw: object) -> Decimal | None:
    """A finite broker number as a Decimal; None for None, NaN, infinities,
    junk, booleans and ib_async's UNSET sentinel."""
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = Decimal(str(raw).strip())
    except InvalidOperation:
        return None
    if not value.is_finite() or abs(value) >= _UNSET_MARGIN:
        return None
    return value


def _scaled(value: Decimal, unit: Decimal, rounding: str = ROUND_HALF_EVEN) -> int:
    return int((value * unit).to_integral_value(rounding=rounding))


def _whole(raw: object) -> int | None:
    """A whole (signed) quantity as an int; None when fractional or junk."""
    value = _number(raw)
    if value is None or value != value.to_integral_value():
        return None
    return int(value)


def _utc_instant(raw: object) -> datetime | None:
    if isinstance(raw, datetime) and raw.utcoffset() is not None:
        return raw.astimezone(UTC)
    return None


@dataclass(frozen=True)
class EquityQuote:
    """A stock NBBO as IBKR delivered it: integer cents on the penny grid
    (a sub-penny bid floors, a sub-penny ask ceils: the quote only widens),
    the market-data type the ticker reports, and its latest tick (UTC)."""

    symbol: str
    con_id: int
    bid_cents: int
    ask_cents: int
    market_data_type: MarketDataTypeName
    as_of: datetime

    @property
    def realtime(self) -> bool:
        return self.market_data_type == "realtime"

    def to_contract(self) -> Quote:
        """The ``contracts.proposals.Quote`` the owner reviews."""
        from tree_options.trex.contracts.proposals import Quote

        return Quote(
            bid_cents=self.bid_cents,
            ask_cents=self.ask_cents,
            market_data_type=self.market_data_type,
            as_of=self.as_of,
        )


def equity_quote_from_ticker(symbol: str, con_id: int, ticker: Any) -> EquityQuote | None:
    """The two-sided quote a stock ticker carries; None without one: a
    missing, non-positive, UNSET or crossed side, or no tick time yet (a
    fresh ib_async ticker claims type 1 before any data arrives)."""
    bid = _number(getattr(ticker, "bid", None))
    ask = _number(getattr(ticker, "ask", None))
    as_of = _utc_instant(getattr(ticker, "time", None))
    if bid is None or ask is None or as_of is None or bid <= 0 or ask <= 0:
        return None
    bid_cents = _scaled(bid, _CENT, ROUND_FLOOR)
    ask_cents = _scaled(ask, _CENT, ROUND_CEILING)
    if bid_cents <= 0 or bid_cents > ask_cents:
        return None
    return EquityQuote(
        symbol=symbol,
        con_id=con_id,
        bid_cents=bid_cents,
        ask_cents=ask_cents,
        market_data_type=market_data_type_name(getattr(ticker, "marketDataType", None)),
        as_of=as_of,
    )


@dataclass(frozen=True)
class BrokerView[T]:
    """One broker view mapped onto ``contracts.books`` rows.

    ``unrepresentable`` holds one code per broker row the contracts cannot
    carry (``sec_type_unsupported`` / ``quantity_not_whole`` /
    ``row_invalid``, then ``:<conId>``): such a row is reported, never
    dropped, and the books lane treats the view as unavailable or
    reconciliation-required. ``error`` is the error CLASS of a view that
    could not be read at all (provider text never rides along)."""

    rows: tuple[T, ...] = ()
    unrepresentable: tuple[str, ...] = ()
    error: str | None = None

    @property
    def status(self) -> ViewStatus:
        if self.error is not None:
            return "unavailable"
        return "unrepresentable" if self.unrepresentable else "complete"

    @property
    def complete(self) -> bool:
        return self.status == "complete"


class _Unrepresentable(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _con_id_of(contract: Any) -> int:
    con = _whole(getattr(contract, "conId", 0))
    return con if con is not None else 0


def _map_rows[T](raw_rows: Iterable[Any], mapper: Callable[[Any], T]) -> BrokerView[T]:
    rows: list[T] = []
    unrepresentable: list[str] = []
    for raw in raw_rows:
        try:
            rows.append(mapper(raw))
        except _Unrepresentable as bad:
            unrepresentable.append(bad.code)
        except (ValueError, TypeError, AttributeError):  # a field the contract refuses
            unrepresentable.append(f"row_invalid:{_con_id_of(getattr(raw, 'contract', None))}")
    return BrokerView(rows=tuple(rows), unrepresentable=tuple(unrepresentable))


def _has_commission(fill: Any) -> bool:
    """True once the fill's OWN commission report has arrived (ib_async
    keeps a blank report, execId "", until then)."""
    report = getattr(fill, "commissionReport", None)
    exec_id = str(getattr(getattr(fill, "execution", None), "execId", "") or "")
    return bool(exec_id) and str(getattr(report, "execId", "") or "") == exec_id


def broker_execution(fill: Any) -> BrokerExecution:
    """An ib_async ``Fill`` as a ``contracts.books.BrokerExecution``;
    ``commission_micros`` stays None until the fill's own commission report
    has arrived. Raises for a row the contract cannot represent."""
    from tree_options.trex.contracts.books import BrokerExecution, base_exec_id_of

    contract, ex = fill.contract, fill.execution
    con_id = _con_id_of(contract)
    sec_type = str(getattr(contract, "secType", "") or "")
    if sec_type not in _EXEC_SEC_TYPES:
        raise _Unrepresentable(f"sec_type_unsupported:{con_id}")
    quantity = _whole(getattr(ex, "shares", None))
    if quantity is None:
        raise _Unrepresentable(f"quantity_not_whole:{con_id}")
    price = _number(getattr(ex, "price", None))
    multiplier = _whole(getattr(contract, "multiplier", "") or 1)
    at = _utc_instant(getattr(ex, "time", None)) or _utc_instant(getattr(fill, "time", None))
    if price is None or multiplier is None or at is None:
        raise _Unrepresentable(f"row_invalid:{con_id}")
    commission = None
    if _has_commission(fill):
        reported = _number(getattr(fill.commissionReport, "commission", None))
        commission = None if reported is None else _scaled(reported, _MICRO)
    exec_id = str(getattr(ex, "execId", "") or "")
    return BrokerExecution.model_validate(
        {
            "exec_id": exec_id,
            "base_exec_id": base_exec_id_of(exec_id),
            "perm_id": _whole(getattr(ex, "permId", 0)) or 0,
            "order_ref": str(getattr(ex, "orderRef", "") or ""),
            "account_id": str(getattr(ex, "acctNumber", "") or ""),
            "con_id": con_id,
            "sec_type": sec_type,
            "symbol": str(getattr(contract, "symbol", "") or ""),
            "side": str(getattr(ex, "side", "") or ""),
            "quantity": quantity,
            "price_micros": _scaled(price, _MICRO),
            "multiplier": multiplier,
            "time": at,
            "commission_micros": commission,
        }
    )


def broker_portfolio_item(row: Any, as_of: datetime) -> BrokerPortfolioItem:
    """An ib_async ``PortfolioItem`` (or ``Position``: no market fields) as
    a ``contracts.books.BrokerPortfolioItem`` read at ``as_of``. Raises for
    a row the contract cannot represent."""
    from tree_options.trex.contracts.books import BrokerPortfolioItem

    contract = row.contract
    con_id = _con_id_of(contract)
    sec_type = str(getattr(contract, "secType", "") or "")
    if sec_type not in _POSITION_SEC_TYPES:
        raise _Unrepresentable(f"sec_type_unsupported:{con_id}")
    position = _whole(getattr(row, "position", None))
    if position is None:
        raise _Unrepresentable(f"quantity_not_whole:{con_id}")
    is_item = hasattr(row, "averageCost")
    avg_cost = _number(getattr(row, "averageCost" if is_item else "avgCost", None))
    if avg_cost is None:
        raise _Unrepresentable(f"row_invalid:{con_id}")
    market_price = _number(getattr(row, "marketPrice", None)) if is_item else None
    market_value = _number(getattr(row, "marketValue", None)) if is_item else None
    if market_price is not None and market_price < 0:
        market_price = None
    return BrokerPortfolioItem.model_validate(
        {
            "account_id": str(getattr(row, "account", "") or ""),
            "con_id": con_id,
            "sec_type": sec_type,
            "symbol": str(getattr(contract, "symbol", "") or ""),
            "position": position,
            "avg_cost_micros": _scaled(avg_cost, _MICRO),
            "market_price_micros": None if market_price is None else _scaled(market_price, _MICRO),
            "market_value_cents": None if market_value is None else _scaled(market_value, _CENT),
            "as_of": as_of,
        }
    )


def _aware(as_of: datetime) -> datetime:
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")
    return as_of.astimezone(UTC)


class IbkrTrex:
    """One connected session; call :meth:`connect` before anything else."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = GATEWAY_PAPER_PORT,
        client_id: int = 77,
        delayed_ok: bool = True,
    ) -> None:
        self.host = host
        self.port = port
        self.client_id = client_id
        self.delayed_ok = delayed_ok
        self._ib: Any = None
        self._legs: dict[tuple[str, int], Any] = {}  # (structure_id, leg index) -> Option
        self._packages: dict[str, _Package] = {}  # structure_id -> debit-oriented legs
        self._bags: dict[str, Any] = {}  # structure_id -> BAG contract (multi-leg only)
        self._spots: dict[str, Any] = {}  # underlying -> Stock contract
        self._stocks: dict[str, Any] = {}  # equity symbol -> pinned Stock contract
        self._md: dict[int, _Subscription] = {}  # conId -> the one market-data stream
        # IB.run bound for what-if (seconds): an unanswered request is a refusal
        self.whatif_timeout = WHATIF_TIMEOUT_S

    # -- lifecycle ---------------------------------------------------------

    def connect(self) -> None:
        from ib_async import IB  # lazy: optional dependency group

        self._ib = IB()
        self._ib.connect(self.host, self.port, clientId=self.client_id, timeout=20)
        if self.delayed_ok:
            # Paper accounts without market-data subscriptions: fall back to
            # delayed quotes instead of trading blind on absent NBBO.
            self._ib.reqMarketDataType(3)

    def disconnect(self) -> None:
        if self._ib is not None:
            self._ib.disconnect()

    @property
    def connected(self) -> bool:
        return self._ib is not None and self._ib.isConnected()

    # -- contracts ---------------------------------------------------------

    def prepare(self, structures: Sequence[PutSpread | LegStructure]) -> None:
        """Qualify all option legs, build the BAG contracts, subscribe spots.

        Incremental: a structure already prepared with the same legs is
        skipped; one whose legs changed is refused (ValueError). Nothing is
        registered unless every contract qualifies."""
        from ib_async import ComboLeg, Contract, Option, Stock

        assert self._ib is not None
        packages: list[_Package] = []
        for struct in structures:
            pkg = _package(struct)
            held = self._packages.get(pkg.sid)
            if held is not None:
                if held != pkg:
                    raise ValueError(f"{pkg.sid}: already prepared with different legs")
                continue
            if any(p.sid == pkg.sid for p in packages):
                raise ValueError(f"{pkg.sid}: listed twice")
            packages.append(pkg)
        if not packages:
            return

        spot_contracts: dict[str, Any] = {}
        option_legs: list[Any] = []
        legs: dict[tuple[str, int], Any] = {}
        for pkg in packages:
            for i, leg in enumerate(pkg.legs):
                option = Option(
                    symbol=pkg.underlying,
                    lastTradeDateOrContractMonth=leg.expiry.strftime("%Y%m%d"),
                    strike=float(leg.strike),
                    right=leg.right,
                    exchange="SMART",
                    tradingClass=pkg.underlying,
                )
                option_legs.append(option)
                legs[(pkg.sid, i)] = option
            if pkg.underlying not in self._spots:
                spot_contracts.setdefault(pkg.underlying, Stock(pkg.underlying, "SMART", "USD"))

        # Spot stocks are qualified alongside the option legs, not separately:
        # market data is requested for them below, keyed by conId, and
        # ib_async refuses to hash a Contract whose conId is still unset.
        to_qualify = [*option_legs, *spot_contracts.values()]
        self._ib.qualifyContracts(*to_qualify)
        # qualifyContracts fills conId in place; a contract it could not
        # resolve is left with conId 0.
        unqualified = [str(c) for c in to_qualify if getattr(c, "conId", 0) == 0]
        if unqualified:
            raise RuntimeError(f"unqualified contracts: {unqualified}")

        self._legs.update(legs)
        for pkg in packages:
            self._packages[pkg.sid] = pkg
            options = [legs[(pkg.sid, i)] for i in range(len(pkg.legs))]
            if not pkg.single:
                self._bags[pkg.sid] = Contract(
                    symbol=pkg.underlying,
                    secType="BAG",
                    exchange="SMART",
                    currency="USD",
                    tradingClass=pkg.underlying,
                    comboLegs=[
                        ComboLeg(
                            conId=option.conId,
                            ratio=leg.ratio,
                            action=leg.action,
                            exchange="SMART",
                        )
                        for option, leg in zip(options, pkg.legs, strict=True)
                    ],
                )
            for option in options:
                self._subscribe(option)
        self._spots.update(spot_contracts)
        for pkg in packages:  # one spot owner per structure on the underlying
            self._subscribe(self._spots[pkg.underlying])
        # let first quotes arrive
        self._ib.sleep(4)

    def _subscribe(self, contract: Any) -> None:
        sub = self._md.get(contract.conId)
        if sub is None:
            sub = _Subscription(contract, self._ib.reqMktData(contract, "", False, False))
            self._md[contract.conId] = sub
        sub.owners += 1

    def _unsubscribe(self, con_id: int) -> None:
        sub = self._md.get(con_id)
        if sub is None:
            return
        sub.owners -= 1
        if sub.owners <= 0:
            del self._md[con_id]
            self._ib.cancelMktData(sub.contract)

    def release(self, structure_id: str) -> None:
        """Forget a CLOSED structure: drop its claim on each leg's market
        data (and its underlying's spot); a stream is cancelled when its
        last owning structure releases it. Unknown ids are a no-op."""
        pkg = self._packages.pop(structure_id, None)
        if pkg is None:
            return
        assert self._ib is not None
        self._bags.pop(structure_id, None)
        for i in range(len(pkg.legs)):
            self._unsubscribe(self._legs.pop((structure_id, i)).conId)
        stock = self._spots.get(pkg.underlying)
        if stock is not None:
            self._unsubscribe(stock.conId)
            if stock.conId not in self._md:
                del self._spots[pkg.underlying]

    def leg_con_ids(self, structure_id: str) -> tuple[int, ...]:
        """conIds of a prepared structure's legs, by leg index (empty if unknown)."""
        pkg = self._packages.get(structure_id)
        if pkg is None:
            return ()
        return tuple(int(self._legs[(structure_id, i)].conId) for i in range(len(pkg.legs)))

    # -- market data -------------------------------------------------------

    def leg_quote(self, structure_id: str, index: int) -> tuple[Decimal, Decimal] | None:
        leg = self._legs.get((structure_id, index))
        if leg is None:
            return None
        sub = self._md.get(leg.conId)
        if sub is None:
            return None
        bid, ask = sub.ticker.bid, sub.ticker.ask
        if bid is None or ask is None or bid != bid or ask != ask:  # NaN guard
            return None
        if bid <= 0 and ask <= 0:
            # an empty book (combo legs before the MMs open, a gateway whose
            # ticklers never primed) reports 0.0/0.0 — that is NO market,
            # not a $0.00 one. Letting it through marked whole books at
            # zero (a full-loss chart overnight) and could price a forced
            # exit limit at $0.00.
            return None
        return _d(bid), _d(ask)

    def package_quote(self, structure_id: str) -> ComboQuote | None:
        """NBBO of the package in debit orientation, from its legs; None if
        any leg lacks a two-sided quote."""
        pkg = self._packages.get(structure_id)
        if pkg is None:
            return None
        buy_bid = buy_ask = sell_bid = sell_ask = Decimal(0)
        for i, leg in enumerate(pkg.legs):
            quote = self.leg_quote(structure_id, i)
            if quote is None:
                return None
            if leg.action == "BUY":
                buy_bid += quote[0]
                buy_ask += quote[1]
            else:
                sell_bid += quote[0]
                sell_ask += quote[1]
        return ComboQuote(bid=buy_bid - sell_ask, ask=buy_ask - sell_bid)

    def combo_quote(self, structure_id: str) -> ComboQuote | None:
        """The legacy name of :meth:`package_quote`."""
        return self.package_quote(structure_id)

    def snapshot(self, structures: Sequence[PutSpread | LegStructure], ts: datetime) -> Snapshot:
        """Package quotes only. IBKR stock prices are NOT a touch source:
        ``ticker.time`` moves on every bid/ask/size tick, so a fresh ticker
        can carry an old ``last`` (a false touch, or a hidden one), bid/ask
        carry no timestamp at all, and ``close`` is the prior session's.
        The paper account delivers no equity quotes anyway. The monitor
        adds spots from trex.spot (Polygon, session-bounded); re-evaluate
        once live quotes arrive and ``lastTimestamp`` (tick 45) is verified.
        """
        return Snapshot(
            ts=ts, spots={}, quotes={s.id: self.package_quote(s.id) for s in structures}
        )

    def entry_fill_evidence(
        self, structure_id: str, order_id: str | None
    ) -> tuple[int, Decimal | None] | None:
        """What the broker can PROVE was filled of an entry order that is no
        longer working (it filled or died while the entry runner was down):

        * (packages, avg price) from today's executions of ``order_id`` by
          this client on the structure's legs (ib_async fetches the day's
          executions at connect); the price is in debit orientation (BUY
          legs +, SELL legs -), i.e. the debit paid or the credit received;
        * (0, None) when there are none and the account holds none of the legs;
        * None, inconclusive, otherwise (legs disagree, or legs are held
          with no execution of this order today): never guessed.
        """
        assert self._ib is not None
        pkg = self._packages.get(structure_id)
        if pkg is None:
            return None
        options = [self._legs[(structure_id, i)] for i in range(len(pkg.legs))]
        if order_id is not None and order_id.isdigit():
            oid = int(order_id)
            mine = [
                f
                for f in self._ib.fills()
                if f.execution.orderId == oid and f.execution.clientId == self.client_id
            ]
            per_leg = [[f for f in mine if f.contract.conId == o.conId] for o in options]
            if any(per_leg):
                qtys = [
                    sum((_d(f.execution.shares) for f in fills), Decimal(0)) for fills in per_leg
                ]
                if any(q != qtys[0] for q in qtys) or qtys[0] != qtys[0].to_integral_value():
                    return None
                qty = int(qtys[0])
                notional = Decimal(0)
                for leg, fills in zip(pkg.legs, per_leg, strict=True):
                    leg_notional = sum(
                        (_d(f.execution.price) * _d(f.execution.shares) for f in fills), Decimal(0)
                    )
                    notional += leg_notional if leg.action == "BUY" else -leg_notional
                return qty, (notional / qty if qty else None)
        held = {getattr(p.contract, "conId", 0): p.position for p in self._ib.positions()}
        if not any(held.get(o.conId) for o in options):
            return 0, None
        return None

    # -- orders ------------------------------------------------------------

    def place_combo(self, spread: PutSpread, side: str, qty: int, limit: Decimal) -> OrderRef:
        """The legacy put-spread order (untagged), unchanged."""
        from ib_async import LimitOrder

        assert self._ib is not None
        order = LimitOrder(side, qty, float(limit), tif="DAY", transmit=True)
        trade = self._ib.placeOrder(self._bags[spread.id], order)
        return OrderRef(spread.id, side, qty, limit, trade)

    def _order(self, struct: LegStructure, side: str, qty: int, limit: Decimal) -> tuple[Any, Any]:
        """(contract, tagged DAY limit order) for ``qty`` packages at a
        debit-orientation ``limit``: the BAG, or the option of a long single.
        Refuses (ValueError) before anything is sent: any order
        plan.validate_package_order rejects (non-finite or non-positive
        prices, an entry past the cap / below the floor, a credit
        BUY-to-close above the width, more packages than the structure), a
        structure not prepared here, or one whose legs differ from it."""
        from ib_async import LimitOrder

        assert self._ib is not None
        validate_package_order(struct, side, qty, limit)
        pkg = self._packages.get(struct.id)
        if pkg is None:
            raise ValueError(f"{struct.id}: not prepared")
        if pkg != _package(struct):
            raise ValueError(f"{struct.id}: differs from the prepared structure")
        contract = self._legs[(struct.id, 0)] if pkg.single else self._bags[struct.id]
        order = LimitOrder(
            side,
            qty,
            float(limit),
            tif="DAY",
            transmit=True,
            orderRef=order_tag(struct.id),
        )
        return contract, order

    def place(self, struct: LegStructure, side: str, qty: int, limit: Decimal) -> OrderRef:
        """Place a DAY limit order for ``qty`` packages: BUY or SELL the
        debit-orientation package at ``limit`` (open with
        ``struct.open_side``, close with ``struct.close_side``), within the
        bounds of :meth:`_order`."""
        contract, order = self._order(struct, side, qty, limit)
        trade = self._ib.placeOrder(contract, order)
        return OrderRef(struct.id, side, qty, limit, trade)

    def whatif(self, struct: LegStructure, side: str, qty: int, limit: Decimal) -> Decimal | None:
        """IBKR's what-if initial-margin change for the order :meth:`place`
        would send (nothing is placed); None, a refusal, when IBKR gives no
        number: an empty, junk or UNSET margin, a failed request, a lost
        connection, or no answer within ``whatif_timeout`` seconds (ib_async
        2.1.0 leaves a request pending on an UNSET-only reply, and its
        RequestTimeout is 0: without the bound this call could block the
        runner forever). Callers refuse packages IBKR margins above their
        max loss (plan.margin_within_max_loss)."""
        contract, order = self._order(struct, side, qty, limit)
        try:
            state = self._ib.run(
                self._ib.whatIfOrderAsync(contract, order), timeout=self.whatif_timeout
            )
        except Exception as exc:  # timeout, request error, disconnect: fail closed
            log.warning("what-if for %s refused: %s", struct.id, type(exc).__name__)
            return None
        return _margin(getattr(state, "initMarginChange", None))

    def open_combo_trades(self) -> list[Any]:
        """Working BAG orders at the broker (ours or anyone's session)."""
        return [t for t in self._ib.openTrades() if getattr(t.contract, "secType", "") == "BAG"]

    def working_trades(self) -> list[Any]:
        """Working BAG and OPT orders at the broker (ours or anyone's session)."""
        return [
            t for t in self._ib.openTrades() if getattr(t.contract, "secType", "") in ("BAG", "OPT")
        ]

    def structure_for_bag(self, contract: Any) -> str | None:
        """Which plan structure (if any) a BAG contract belongs to."""
        try:
            conids = {leg.conId for leg in contract.comboLegs}
        except AttributeError:
            return None
        for sid, bag in self._bags.items():
            if {leg.conId for leg in bag.comboLegs} == conids:
                return sid
        return None

    def _matches(self, sid: str, contract: Any) -> bool:
        pkg = self._packages[sid]
        sec_type = getattr(contract, "secType", "")
        if pkg.single:
            return sec_type == "OPT" and getattr(contract, "conId", 0) == self._legs[(sid, 0)].conId
        if sec_type != "BAG":
            return False
        try:
            return bag_signature(contract.comboLegs) == bag_signature(self._bags[sid].comboLegs)
        except (AttributeError, TypeError, ValueError):
            return False

    def structure_for_contract(self, contract: Any) -> str | None:
        """The prepared structure whose package a BAG (legs, actions and
        ratios) or OPT (a long single's option) contract is; None when none
        or MORE than one match (e.g. a debit and a credit vertical on the
        same strikes share one debit-oriented BAG). A description, not
        ownership: working orders are attributed by structure_for_trade."""
        matches = [sid for sid in self._packages if self._matches(sid, contract)]
        return matches[0] if len(matches) == 1 else None

    def structure_for_trade(self, trade: Any) -> str | None:
        """The multi-leg structure a working trade belongs to: ONLY through
        its trex order tag, and only when the tagged structure is prepared
        here and the trade's contract is its package. Untagged or foreign
        references are never adopted on contract alone (an old untagged
        order closing a debit vertical carries the same BAG as a credit
        vertical on those strikes). Legacy put-spread orders are untagged:
        they are adopted only on the legacy path (open_combo_trades +
        structure_for_bag)."""
        sid = tagged_structure(getattr(trade.order, "orderRef", ""))
        if sid is not None and sid in self._packages and self._matches(sid, trade.contract):
            return sid
        return None

    def order_status(self, ref: OrderRef) -> OrderStatusInfo:
        os = ref.trade.orderStatus
        status = os.status or "Unknown"
        filled = int(os.filled or 0)
        avg = _d(os.avgFillPrice) if filled else Decimal(0)
        return OrderStatusInfo(status=status, filled=filled, avg_fill_price=avg)

    def cancel(self, ref: OrderRef) -> None:
        if ref.trade.orderStatus.status not in ("Filled", "Cancelled", "ApiCancelled"):
            self._ib.cancelOrder(ref.trade.order)

    def sleep(self, seconds: float) -> None:
        """Pump the event loop (ib_async needs its own sleep, not time.sleep).
        Falls back to a plain sleep when never connected - the discovery
        serve loop now connects lazily and runs with the gateway down."""
        if self._ib is None:
            time.sleep(seconds)
            return
        self._ib.sleep(seconds)

    # -- account -----------------------------------------------------------

    def positions(self, account: str | None = None) -> list[BrokerPosition]:
        """The positions of ONE account (select_account): ``account``'s, or,
        when None, the session's only account; positions spanning several
        accounts without a selection raise ValueError (a long leg in one and
        its short in another are two exposures, not a covered package)."""
        assert self._ib is not None
        out: list[BrokerPosition] = []
        for p in self._ib.positions():
            c = p.contract
            out.append(
                BrokerPosition(
                    account=str(getattr(p, "account", "") or ""),
                    con_id=int(getattr(c, "conId", 0) or 0),
                    sec_type=str(getattr(c, "secType", "") or ""),
                    symbol=str(getattr(c, "symbol", "") or ""),
                    right=str(getattr(c, "right", "") or ""),
                    strike=_d(getattr(c, "strike", None)),
                    expiry=str(getattr(c, "lastTradeDateOrContractMonth", "") or ""),
                    qty=_d(p.position),
                    avg_cost=_d(p.avgCost),
                )
            )
        return select_account(out, account)

    def account_snapshot(self) -> AccountSnapshot | None:
        """Net liq / cash / buying power from the gateway session.

        Reads the cached accountValues rows first (ib_async auto-subscribes
        account updates on connect); falls back to ``accountSummary(account)``
        — which RETURNS rows — when the cache is empty. Never fabricates:
        a NaN or missing tag means None, not zero.
        """
        from datetime import datetime

        from tree_options.trex.clock import ET

        assert self._ib is not None
        ib = self._ib
        rows = list(ib.accountValues())
        if not rows:
            accounts = list(ib.managedAccounts())
            if not accounts:
                return None
            rows = list(ib.accountSummary(accounts[0]))
        if not rows:
            return None

        def tag(tag_name: str) -> Decimal | None:
            for row in rows:
                if row.tag != tag_name:
                    continue
                try:
                    value = Decimal(str(row.value))
                except Exception:  # junk value: treated as absent
                    continue
                if value == value and value.is_finite():  # NaN guard
                    return value
            return None

        net_liquidation = tag("NetLiquidation")
        cash = tag("TotalCashValue")
        buying_power = tag("BuyingPower")
        if net_liquidation is None or cash is None or buying_power is None:
            return None
        account_id = next((str(r.account) for r in rows if getattr(r, "account", "")), "")
        if not account_id:
            accounts = list(ib.managedAccounts())
            account_id = accounts[0] if accounts else ""
        return AccountSnapshot(
            account_id=account_id,
            net_liquidation=net_liquidation,
            cash=cash,
            buying_power=buying_power,
            currency="USD",
            ts=datetime.now(ET),
        )

    # -- stocks (Wave 2) -----------------------------------------------------

    def prepare_stock(
        self, symbol: str, con_id: int | None = None, *, settle_s: float = STOCK_SETTLE_S
    ) -> int:
        """Qualify ``Stock(symbol, "SMART", "USD")`` and subscribe its
        quotes; the qualified conId.

        ``con_id`` PINS the stock: one that qualifies (or was prepared)
        under any other conId refuses (StockConIdMismatch) and nothing new
        is held or subscribed. A symbol IBKR cannot resolve to one STK
        contract refuses (StockNotQualified). Idempotent: a prepared symbol
        is never re-qualified nor subscribed twice. A NEW stream (one no
        option underlying already holds) waits ``settle_s`` for a tick."""
        from ib_async import Stock

        assert self._ib is not None
        held = self._stocks.get(symbol)
        contract = held
        if contract is None:
            contract = Stock(symbol, "SMART", "USD")
            self._ib.qualifyContracts(contract)
            if getattr(contract, "secType", "") != "STK" or _con_id_of(contract) <= 0:
                raise StockNotQualified(symbol)
        qualified = _con_id_of(contract)
        if con_id is not None and qualified != con_id:
            raise StockConIdMismatch(f"{symbol}: the qualified conId is not the pinned one")
        if held is None:
            fresh = qualified not in self._md
            self._stocks[symbol] = contract
            self._subscribe(contract)
            if fresh and settle_s > 0:
                self._ib.sleep(settle_s)
        return qualified

    def release_stock(self, symbol: str) -> None:
        """Forget a prepared stock: drop its claim on the quote stream, which
        is cancelled once no option underlying owns it. Unknown: a no-op."""
        contract = self._stocks.pop(symbol, None)
        if contract is None:
            return
        assert self._ib is not None
        self._unsubscribe(contract.conId)

    def _stock_ticker(self, symbol: str) -> tuple[int, Any] | None:
        contract = self._stocks.get(symbol)
        sub = None if contract is None else self._md.get(contract.conId)
        if contract is None or sub is None:
            return None
        return _con_id_of(contract), sub.ticker

    def equity_quote(self, symbol: str) -> EquityQuote | None:
        """A prepared stock's two-sided quote (``equity_quote_from_ticker``);
        None when the stock is not prepared or has no such quote."""
        held = self._stock_ticker(symbol)
        return None if held is None else equity_quote_from_ticker(symbol, *held)

    def equity_market_data_type(self, symbol: str) -> MarketDataTypeName | None:
        """The market-data type of a prepared stock's stream once a tick has
        arrived (two-sided or not); None before any tick."""
        held = self._stock_ticker(symbol)
        if held is None or _utc_instant(getattr(held[1], "time", None)) is None:
            return None
        return market_data_type_name(getattr(held[1], "marketDataType", None))

    def executions(self, account: str | None = None) -> BrokerView[BrokerExecution]:
        """Today's executions as ``BrokerExecution`` rows (``account``'s only,
        when given): the ``reqExecutions`` answer (all clients) merged with
        the session's ``fills()`` by exec id. ib_async answers
        ``reqExecutions`` with NEW fills whose commission report is blank
        for executions the session already knew; the reports land on the
        cached fills, so the copy whose report arrived wins. A correction
        (same base exec id, next version) is its own row."""
        assert self._ib is not None
        try:
            answered = [*self._ib.reqExecutions(), *self._ib.fills()]
        except Exception as exc:  # an unreadable view proves nothing
            return BrokerView(error=type(exc).__name__)
        by_id: dict[str, Any] = {}
        for i, fill in enumerate(answered):
            execution = getattr(fill, "execution", None)
            key = str(getattr(execution, "execId", "") or "") or f"#{i}"
            seen = by_id.get(key)
            if seen is None or (_has_commission(fill) and not _has_commission(seen)):
                by_id[key] = fill
        fills = [
            f
            for f in by_id.values()
            if account is None
            or str(getattr(getattr(f, "execution", None), "acctNumber", "")) == account
        ]
        return _map_rows(fills, broker_execution)

    def portfolio(
        self, *, as_of: datetime, account: str | None = None
    ) -> BrokerView[BrokerPortfolioItem]:
        """The session's portfolio (positions with marks) as
        ``BrokerPortfolioItem`` rows read at ``as_of``; a 0 row after a
        close is kept."""
        at = _aware(as_of)
        assert self._ib is not None
        try:
            rows = list(self._ib.portfolio())
        except Exception as exc:  # an unreadable view proves nothing
            return BrokerView(error=type(exc).__name__)
        return _map_rows(_of_account(rows, account), lambda row: broker_portfolio_item(row, at))

    def position_items(
        self, *, as_of: datetime, account: str | None = None
    ) -> BrokerView[BrokerPortfolioItem]:
        """The account positions (no marks: the market fields are None) as
        ``BrokerPortfolioItem`` rows read at ``as_of``; option shorts stay
        negative."""
        at = _aware(as_of)
        assert self._ib is not None
        try:
            rows = list(self._ib.positions())
        except Exception as exc:  # an unreadable view proves nothing
            return BrokerView(error=type(exc).__name__)
        return _map_rows(_of_account(rows, account), lambda row: broker_portfolio_item(row, at))

    def settled_cash_cents(self, account: str) -> int | None:
        """``account``'s settled cash in whole cents, floored: its
        ``SettledCash`` account value in USD (else BASE). None when absent,
        junk or ambiguous (two different values). Read errors propagate."""
        assert self._ib is not None
        rows = [
            r
            for r in self._ib.accountValues()
            if getattr(r, "tag", "") == "SettledCash" and str(getattr(r, "account", "")) == account
        ]
        for currency in ("USD", "BASE"):
            values = [_number(r.value) for r in rows if getattr(r, "currency", "") == currency]
            if not values:
                continue
            value = values[0]
            if value is None or any(v != value for v in values):
                return None
            return _scaled(value, _CENT, ROUND_FLOOR)
        return None

    def working_stock_buy_cents(self, account: str) -> int | None:
        """Cash ``account``'s working STK BUY orders commit: unfilled shares
        x limit, in cents (ceil). An order without the account set counts
        (it may be this one's). None when a working buy has no positive
        limit (the commitment is unbounded). Read errors propagate."""
        assert self._ib is not None
        committed = 0
        for trade in self._ib.openTrades():
            order, status = trade.order, trade.orderStatus
            if getattr(trade.contract, "secType", "") != "STK":
                continue
            if getattr(order, "action", "") != "BUY":
                continue
            if str(getattr(order, "account", "") or "") not in ("", account):
                continue
            if str(getattr(status, "status", "") or "") in DONE_STATES:
                continue
            total = _number(getattr(order, "totalQuantity", None)) or Decimal(0)
            remaining = total - (_number(getattr(status, "filled", None)) or Decimal(0))
            if remaining <= 0:
                continue
            limit = _number(getattr(order, "lmtPrice", None))
            if limit is None or limit <= 0:
                return None
            committed += _scaled(remaining * limit, _CENT, ROUND_CEILING)
        return committed


def _of_account(rows: list[Any], account: str | None) -> list[Any]:
    if account is None:
        return rows
    return [r for r in rows if str(getattr(r, "account", "") or "") == account]
