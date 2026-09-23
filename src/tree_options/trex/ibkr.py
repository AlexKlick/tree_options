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
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from tree_options.trex.account import AccountSnapshot
from tree_options.trex.engine import ComboQuote, Snapshot
from tree_options.trex.plan import Leg, LegStructure, PutSpread, validate_package_order

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
        raise ValueError(
            f"positions span {len({r.account for r in rows})} accounts; select one"
        )
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
                qtys = [sum((_d(f.execution.shares) for f in fills), Decimal(0)) for fills in per_leg]
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

    def place_combo(
        self, spread: PutSpread, side: str, qty: int, limit: Decimal
    ) -> OrderRef:
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
            t
            for t in self._ib.openTrades()
            if getattr(t.contract, "secType", "") in ("BAG", "OPT")
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
