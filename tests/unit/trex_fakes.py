"""Shared trex test doubles (desk lane E1/E2 and later).

``FakeGateway`` is a duck-typed ``ib_async.IB``: it drives the REAL
``IbkrTrex`` adapter (contract building, NBBO-from-legs quotes, BAG and OPT
orders, positions, what-if margin) with no gateway and no network. It
records what the adapter sent, so tests can pin wire content, and it keeps
the SDK behaviors that bite: a repeated ``reqMktData`` for one contract is a
new request of which ``cancelMktData`` cancels only the LATEST (ib_async
keeps one reqId per ticker); ``openTrades`` is every order not in a done
state; a what-if reply can stay unresolved (IBKR's UNSET-only answer), and
``run`` refuses to wait on anything without a timeout.

``FakeDeskBroker`` is a duck-typed ``IbkrTrex`` for runner-level tests of
multi-leg books (the desk runtime, E5): per-contract quotes summed into
package quotes in debit orientation, BAG or OPT orders tagged like the
adapter's, the same order bounds, ownership, account and release rules.
test_trex_ibkr_multileg runs one shared behavioural suite against both.

The monitor/enter choreography tests keep their own inline legacy fakes.

Wave 2 (docs/product/WAVE2-CONTRACTS.md, C2.8) adds, below and without
changing anything above: ``FakeCommandAuthority`` (+ ``sign_family_actor``),
``Wave2Gateway`` (stocks, market-data types, NBBO ticks, STK DAY limit
orders with scripted fills, IBKR exec ids, late commission reports,
corrections, positions/portfolio with faults), ``SessionClock`` (NYSE
sessions, early closes, restarts minting owner epochs) and contract
builders over ``tests/contracts/fixtures``.

Wave 3 (docs/product/WAVE3-CONTRACTS.md, C3.1) adds, again additively, the
v2 family mode: ``FakeFamilyPortal`` (lane A's ``signTrexCommand`` and the
request the portal forwards) and ``FakeCommandAuthority(family_mode="v2")``.
"""

from __future__ import annotations

import asyncio
import base64
import bisect
import hashlib
import hmac
import json
import math
import os
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from tree_options.trex.clock import ET, session_calendar
from tree_options.trex.contracts import (
    Actor,
    ApproveEntryCommand,
    CommandAuthorityRefused,
    EntryProposal,
    EntryProposalBody,
    LedgerEvent,
    RawCommandRequest,
    SupervisedEffect,
    SupervisedEquityEffect,
    SupervisedMandateV2,
    base_exec_id_of,
    effect_v1_sha256,
    equity_max_loss_cents,
    equity_notional_cents,
    mandate_for_approval,
    model_document,
    parse_command,
    parse_order_ref,
    proposal_sha256,
    sha256_hex,
)
from tree_options.trex.contracts.wave3 import (
    FAMILY_COMMAND_PATH,
    PORTAL_ROUTES,
    FamilyAuthorityConfig,
    family_config_from_env,
    mint_family_command,
    verify_family_command,
)
from tree_options.trex.ibkr import (
    DONE_STATES,
    BrokerPosition,
    OrderRef,
    OrderStatusInfo,
    bag_signature,
    order_tag,
    select_account,
    tagged_structure,
)
from tree_options.trex.plan import LegStructure, validate_package_order
from tree_options.trex.supervised_ibkr import SUPERVISED_CLIENT_ID, supervised_order_ref

ContractKey = tuple[str, str, str, float, str]  # secType, symbol, expiry, strike, right
BAG_CON_ID = 28812380  # IBKR gives every BAG this conId


@dataclass
class FakeTicker:
    bid: float | None = None
    ask: float | None = None


@dataclass
class FakeStatus:
    status: str = "Submitted"
    filled: int = 0
    avgFillPrice: float = 0.0


@dataclass
class FakeTrade:
    contract: Any
    order: Any
    orderStatus: FakeStatus = field(default_factory=FakeStatus)


def contract_key(contract: Any) -> ContractKey:
    return (
        str(contract.secType),
        str(contract.symbol),
        str(getattr(contract, "lastTradeDateOrContractMonth", "") or ""),
        float(getattr(contract, "strike", 0.0) or 0.0),
        str(getattr(contract, "right", "") or ""),
    )


class FakeGateway:
    """Duck-typed ib_async.IB. ``con_ids`` presets conIds by contract key;
    anything else qualifies to the next free id from 1001. Keys listed in
    ``unknown`` stay unqualified (conId 0), like a contract IBKR can't find."""

    def __init__(
        self,
        con_ids: dict[ContractKey, int] | None = None,
        unknown: set[ContractKey] | None = None,
    ) -> None:
        self.con_ids: dict[ContractKey, int] = dict(con_ids or {})
        self.unknown = set(unknown or ())
        self._next_con = 1000
        self.qualify_calls: list[list[Any]] = []
        self.tickers: dict[int, FakeTicker] = {}
        self.subscribed: list[Any] = []
        self.mkt_cancelled: list[int] = []
        # market-data requests the gateway is still streaming, and the one
        # reqId per conId the SDK remembers (cancelMktData cancels only it)
        self.md_live: set[int] = set()
        self._md_latest: dict[int, int] = {}
        self._md_seq = 9000
        self.trades: list[FakeTrade] = []
        self._next_oid = 500
        self.order_cancels: list[int] = []
        self.position_rows: list[Any] = []
        self.fill_rows: list[Any] = []
        # what-if replies: an OrderState carrying ``whatif_margin`` (a str in
        # ib_async), else ``whatif_reply`` verbatim ([] = a failed request
        # with RaiseRequestErrors off), ``whatif_error`` raised, or no answer
        # at all (``whatif_pending``: IBKR's UNSET-only reply never ends it)
        self.whatif_margin: Any = ""
        self.whatif_reply: Any = None
        self.whatif_error: BaseException | None = None
        self.whatif_pending = False
        self.whatif_calls: list[tuple[Any, Any]] = []
        self.run_timeouts: list[float] = []
        self.slept: list[float] = []

    # -- contracts / market data --------------------------------------------

    def con_id(
        self, sec_type: str, symbol: str, expiry: str = "", strike: float = 0.0, right: str = ""
    ) -> int:
        return self.con_ids[(sec_type, symbol, expiry, float(strike), right)]

    def qualifyContracts(self, *contracts: Any) -> list[Any]:
        self.qualify_calls.append(list(contracts))
        for c in contracts:
            key = contract_key(c)
            if key in self.unknown:
                continue
            if key not in self.con_ids:
                self._next_con += 1
                while self._next_con in self.con_ids.values():
                    self._next_con += 1
                self.con_ids[key] = self._next_con
            c.conId = self.con_ids[key]
        return list(contracts)

    def reqMktData(self, contract: Any, *_args: Any, **_kwargs: Any) -> FakeTicker:
        self.subscribed.append(contract)
        self._md_seq += 1
        self.md_live.add(self._md_seq)
        self._md_latest[contract.conId] = self._md_seq
        return self.tickers.setdefault(contract.conId, FakeTicker())

    def cancelMktData(self, contract: Any) -> bool:
        self.mkt_cancelled.append(contract.conId)
        req = self._md_latest.pop(contract.conId, 0)
        if not req:
            return False
        self.md_live.discard(req)
        return True

    def quote(self, con_id: int, bid: float | None, ask: float | None) -> None:
        ticker = self.tickers.setdefault(con_id, FakeTicker())
        ticker.bid, ticker.ask = bid, ask

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)

    def run(self, *awaitables: Any, timeout: float | None = None) -> Any:
        """ib_async's IB.run (util.run): run the loop until the awaitable is
        done, TimeoutError after ``timeout``. Refuses an unbounded run: on
        an unresolved reply the real one would never return."""
        if not timeout:
            for aw in awaitables:
                if asyncio.iscoroutine(aw):
                    aw.close()
            raise AssertionError("unbounded broker request: it hangs on an unresolved reply")
        self.run_timeouts.append(timeout)
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(asyncio.wait_for(awaitables[0], timeout))
        finally:
            loop.close()

    # -- orders --------------------------------------------------------------

    def placeOrder(self, contract: Any, order: Any) -> FakeTrade:
        self._next_oid += 1
        order.orderId = self._next_oid
        trade = FakeTrade(contract=contract, order=order)
        self.trades.append(trade)
        return trade

    def openTrades(self) -> list[FakeTrade]:
        return [t for t in self.trades if t.orderStatus.status not in SDK_DONE_STATES]

    def cancelOrder(self, order: Any) -> None:
        self.order_cancels.append(order.orderId)

    def whatIfOrderAsync(self, contract: Any, order: Any) -> Any:
        self.whatif_calls.append((contract, order))

        async def reply() -> Any:
            if self.whatif_pending:
                await asyncio.get_running_loop().create_future()  # never resolved
            if self.whatif_error is not None:
                raise self.whatif_error
            if self.whatif_reply is not None:
                return self.whatif_reply
            return SimpleNamespace(initMarginChange=self.whatif_margin)

        return reply()

    # -- account -------------------------------------------------------------

    def positions(self) -> list[Any]:
        return list(self.position_rows)

    def fills(self) -> list[Any]:
        return list(self.fill_rows)


# ib_async.OrderStatus.DoneStates (pinned against the SDK in the tests)
SDK_DONE_STATES = frozenset({"Filled", "Cancelled", "ApiCancelled", "Inactive"})


def fill_row(con_id: int, shares: float, price: float, oid: int, client: int) -> Any:
    """One ib_async Fill as the adapter reads it (leg contract + execution)."""
    return SimpleNamespace(
        contract=SimpleNamespace(conId=con_id, secType="OPT"),
        execution=SimpleNamespace(orderId=oid, clientId=client, shares=shares, price=price),
    )


def position_row(
    con_id: int,
    qty: float,
    avg_cost: float = 0.0,
    account: str = "DU0000000",
    **contract: Any,
) -> Any:
    """One ib_async Position as the adapter reads it."""
    fields: dict[str, Any] = {
        "conId": con_id,
        "secType": "OPT",
        "symbol": "",
        "right": "",
        "strike": 0.0,
        "lastTradeDateOrContractMonth": "",
    }
    fields.update(contract)
    return SimpleNamespace(
        account=account,
        contract=SimpleNamespace(**fields),
        position=qty,
        avgCost=avg_cost,
    )


class FakeDeskBroker:
    """Duck-typed IbkrTrex for multi-leg runner tests (no ib_async needed).

    Each distinct leg contract (underlying, right, strike, expiry) gets a
    synthetic conId, so structures sharing a contract share its quote and
    identical packages build identical BAGs, as at IBKR. Orders go through
    the adapter's own rules: plan.validate_package_order, the prepared-spec
    check, the ``trex:<id>`` tag on every order, tag-only ownership
    (ibkr.tagged_structure), ibkr.select_account for positions.
    """

    def __init__(self) -> None:
        self.specs: dict[str, LegStructure] = {}
        self._con_ids: dict[tuple[str, str, Decimal, date], int] = {}
        self.quotes: dict[int, tuple[Decimal, Decimal]] = {}
        self.placed: list[tuple[str, str, int, Decimal]] = []
        self.trades: list[FakeTrade] = []
        self.cancelled: list[int] = []
        self.released: list[str] = []
        self.position_rows: list[BrokerPosition] = []
        self.whatif_margin: Decimal | None = None
        self.whatif_calls: list[tuple[str, str, int, Decimal]] = []
        self.connected = True
        self._next_oid = 900

    # -- contracts -----------------------------------------------------------

    def _con_id(self, underlying: str, leg: Any) -> int:
        key = (underlying, leg.right, leg.strike, leg.expiry)
        return self._con_ids.setdefault(key, 5001 + len(self._con_ids))

    def contract_for(self, struct: LegStructure) -> Any:
        """The contract the adapter would trade: the option of a long
        single, else a BAG of the debit-oriented legs."""
        legs = struct.package_legs()
        if struct.kind == "long_single":
            return SimpleNamespace(secType="OPT", conId=self._con_id(struct.underlying, legs[0]))
        return SimpleNamespace(
            secType="BAG",
            conId=BAG_CON_ID,
            comboLegs=[
                SimpleNamespace(
                    conId=self._con_id(struct.underlying, g), action=g.action, ratio=g.ratio
                )
                for g in legs
            ],
        )

    @staticmethod
    def _package(struct: LegStructure) -> tuple[Any, ...]:
        return (struct.underlying, struct.kind, struct.package_legs())

    def prepare(self, structures: list[LegStructure]) -> None:
        for s in structures:
            held = self.specs.get(s.id)
            if held is not None and self._package(held) != self._package(s):
                raise ValueError(f"{s.id}: already prepared with different legs")
        for s in structures:
            self.specs.setdefault(s.id, s)

    def _prepared(self, struct: LegStructure) -> None:
        held = self.specs.get(struct.id)
        if held is None:
            raise ValueError(f"{struct.id}: not prepared")
        if self._package(held) != self._package(struct):
            raise ValueError(f"{struct.id}: differs from the prepared structure")

    def release(self, structure_id: str) -> None:
        self.released.append(structure_id)
        self.specs.pop(structure_id, None)

    # -- market data ---------------------------------------------------------

    def set_leg_quote(self, sid: str, index: int, bid: str, ask: str) -> None:
        spec = self.specs[sid]
        con = self._con_id(spec.underlying, spec.package_legs()[index])
        self.quotes[con] = (Decimal(bid), Decimal(ask))

    def package_quote(self, structure_id: str) -> Any:
        from tree_options.trex.engine import ComboQuote

        spec = self.specs.get(structure_id)
        if spec is None:
            return None
        buy_bid = buy_ask = sell_bid = sell_ask = Decimal(0)
        for leg in spec.package_legs():
            q = self.quotes.get(self._con_id(spec.underlying, leg))
            if q is None:
                return None
            if leg.action == "BUY":
                buy_bid, buy_ask = buy_bid + q[0], buy_ask + q[1]
            else:
                sell_bid, sell_ask = sell_bid + q[0], sell_ask + q[1]
        return ComboQuote(bid=buy_bid - sell_ask, ask=buy_ask - sell_bid)

    def snapshot(self, structures: list[Any], ts: Any) -> Any:
        from tree_options.trex.engine import Snapshot

        return Snapshot(
            ts=ts, spots={}, quotes={s.id: self.package_quote(s.id) for s in structures}
        )

    # -- orders --------------------------------------------------------------

    def place(self, struct: LegStructure, side: str, qty: int, limit: Decimal) -> OrderRef:
        validate_package_order(struct, side, qty, limit)
        self._prepared(struct)
        self._next_oid += 1
        trade = FakeTrade(
            contract=self.contract_for(struct),
            order=SimpleNamespace(
                orderId=self._next_oid,
                action=side,
                totalQuantity=qty,
                lmtPrice=float(limit),
                orderRef=order_tag(struct.id),
            ),
        )
        self.trades.append(trade)
        self.placed.append((struct.id, side, qty, limit))
        return OrderRef(struct.id, side, qty, limit, trade)

    def working_trades(self) -> list[Any]:
        return [t for t in self.trades if t.orderStatus.status not in DONE_STATES]

    def _matches(self, sid: str, contract: Any) -> bool:
        want = self.contract_for(self.specs[sid])
        sec_type = getattr(contract, "secType", "")
        if want.secType == "OPT":
            return sec_type == "OPT" and getattr(contract, "conId", 0) == want.conId
        if sec_type != "BAG":
            return False
        try:
            return bag_signature(contract.comboLegs) == bag_signature(want.comboLegs)
        except (AttributeError, TypeError, ValueError):
            return False

    def structure_for_contract(self, contract: Any) -> str | None:
        matches = [sid for sid in self.specs if self._matches(sid, contract)]
        return matches[0] if len(matches) == 1 else None

    def structure_for_trade(self, trade: Any) -> str | None:
        sid = tagged_structure(getattr(trade.order, "orderRef", ""))
        if sid is not None and sid in self.specs and self._matches(sid, trade.contract):
            return sid
        return None

    def order_status(self, ref: OrderRef) -> OrderStatusInfo:
        os = ref.trade.orderStatus
        avg = Decimal(str(os.avgFillPrice)) if os.filled else Decimal(0)
        return OrderStatusInfo(status=os.status, filled=os.filled, avg_fill_price=avg)

    def cancel(self, ref: OrderRef) -> None:
        self.cancelled.append(ref.trade.order.orderId)
        ref.trade.orderStatus.status = "Cancelled"

    def whatif(self, struct: LegStructure, side: str, qty: int, limit: Decimal) -> Decimal | None:
        validate_package_order(struct, side, qty, limit)
        self._prepared(struct)
        self.whatif_calls.append((struct.id, side, qty, limit))
        return self.whatif_margin

    def sleep(self, seconds: float) -> None:
        return None

    # -- account -------------------------------------------------------------

    def positions(self, account: str | None = None) -> list[BrokerPosition]:
        return select_account(list(self.position_rows), account)

    def fill(self, ref: OrderRef, qty: int, price: str) -> None:
        ref.trade.orderStatus.status = "Filled"
        ref.trade.orderStatus.filled = qty
        ref.trade.orderStatus.avgFillPrice = float(price)


class SupervisedGateway(FakeGateway):
    """FakeGateway plus what the supervised adapter and canary collector read:
    connection state, managed accounts, account values, the all-client
    views (open orders across clients, completed orders, executions) and
    a scripted order-status walk advanced by each ``sleep``.

    ``drop(method, error)`` scripts a lost connection: the NEXT call of
    ``openTrades`` / ``qualifyContracts`` / ``placeOrder`` / ``sleep``
    raises ``error`` (one-shot) and, unless ``disconnect=False``, the
    session reads as disconnected from then on (ib_async raises
    ``ConnectionError`` once its socket is gone)."""

    def __init__(self, con_ids: dict[ContractKey, int], account: str = "DU1234567") -> None:
        super().__init__(con_ids=con_ids)
        self.is_connected = True
        self.accounts = [account]
        self.account_values: dict[str, str] = {
            "NetLiquidation": "1000000.00",
            "TotalCashValue": "1000000.00",
            "BuyingPower": "4000000.00",
        }
        self.status_script: list[str] = ["Submitted"]
        self.reject_code = 0
        self.foreign_open: list[Any] = []
        self.completed: list[Any] = []
        self.executions: list[Any] = []
        self.views_error: BaseException | None = None
        self._perm = 7000
        self._walk: dict[int, list[str]] = {}
        self.faults: dict[str, tuple[BaseException, bool]] = {}

    def drop(self, method: str, error: BaseException, *, disconnect: bool = True) -> None:
        self.faults[method] = (error, disconnect)

    def _fault(self, method: str) -> None:
        fault = self.faults.pop(method, None)
        if fault is None:
            return
        error, disconnect = fault
        if disconnect:
            self.is_connected = False
        raise error

    def isConnected(self) -> bool:
        return self.is_connected

    def qualifyContracts(self, *contracts: Any) -> list[Any]:
        self._fault("qualifyContracts")
        return super().qualifyContracts(*contracts)

    def openTrades(self) -> list[FakeTrade]:
        self._fault("openTrades")
        return super().openTrades()

    def managedAccounts(self) -> list[str]:
        return list(self.accounts)

    def accountValues(self) -> list[Any]:
        return [
            SimpleNamespace(tag=tag, value=value, account=self.accounts[0])
            for tag, value in self.account_values.items()
        ]

    def placeOrder(self, contract: Any, order: Any) -> FakeTrade:
        self._fault("placeOrder")
        trade = super().placeOrder(contract, order)
        self._perm += 1
        order.permId = self._perm
        trade.log = []  # type: ignore[attr-defined]
        walk = list(self.status_script)
        trade.orderStatus.status = walk.pop(0)
        self._walk[id(trade)] = walk
        self._log(trade)
        return trade

    def _log(self, trade: Any) -> None:
        code = self.reject_code if trade.orderStatus.status in ("Inactive", "Cancelled") else 0
        trade.log.append(
            SimpleNamespace(time=None, status=trade.orderStatus.status, message="", errorCode=code)
        )

    def sleep(self, seconds: float) -> None:
        self._fault("sleep")
        super().sleep(seconds)
        for trade in self.trades:
            walk = self._walk.get(id(trade))
            if walk:
                trade.orderStatus.status = walk.pop(0)
                self._log(trade)

    def _views(self) -> None:
        if self.views_error is not None:
            raise self.views_error

    def reqAllOpenOrders(self) -> list[Any]:
        self._views()
        return [*self.openTrades(), *self.foreign_open]

    def reqCompletedOrders(self, api_only: bool) -> list[Any]:
        self._views()
        done = [t for t in self.trades if t not in self.openTrades()]
        return [*done, *self.completed]

    def reqExecutions(self) -> list[Any]:
        self._views()
        return list(self.executions)


# ===========================================================================
# Wave 2 (C2.8). Everything below is additive: the fakes above are unchanged.
# ===========================================================================

# -- C2.8.1 command authority -------------------------------------------------

FAMILY_ACTOR_HEADER = "x-family-actor"
#: The audience this fake expects; the real trex audience is lane E's call.
FAMILY_ACTOR_AUDIENCE = "trex"
#: Test-only signing key (>= 32 UTF-8 bytes, like family-apps' actorSigningKey).
FAKE_FAMILY_KEY = "fake-family-actor-key-for-tests-only-0123456789"
FAMILY_ACTOR_TTL_S = 60
LOOPBACK_PEERS = frozenset({"127.0.0.1", "::1"})
#: Headers a proxy adds: a request carrying one is never plain loopback.
FORWARDED_HEADERS = ("forwarded", "x-forwarded-for", "x-forwarded-host", "x-real-ip")
#: Arbitrary route for fake requests (lane G owns the real one).
FAKE_COMMAND_PATH = "/api/trex/commands"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def sign_family_actor(
    key: str,
    *,
    sub: str,
    method: str,
    path: str,
    iat: int,
    aud: str = FAMILY_ACTOR_AUDIENCE,
    ttl_s: int = FAMILY_ACTOR_TTL_S,
) -> str:
    """An ``X-Family-Actor`` value as family-apps ``src/actor.mjs`` ``signActor``
    mints it (v1): base64url(compact JSON claims ``{v, sub, aud, iat, exp,
    method, path}``) + ``.`` + base64url(HMAC-SHA256(key, that ASCII payload)).
    ``sub`` is trimmed and lower-cased, ``method`` upper-cased, ``exp = iat + 60``."""
    claims = {
        "v": 1,
        "sub": sub.strip().lower(),
        "aud": aud,
        "iat": iat,
        "exp": iat + ttl_s,
        "method": method.upper(),
        "path": path,
    }
    payload = _b64url(json.dumps(claims, separators=(",", ":"), ensure_ascii=False).encode())
    signature = hmac.new(key.encode("utf-8"), payload.encode("ascii"), hashlib.sha256).digest()
    return f"{payload}.{_b64url(signature)}"


class FakeCommandAuthority:
    """A ``contracts.CommandAuthority`` (lane E builds the real one; lane G
    calls ``verify``).

    - loopback ingress: the transport peer is loopback and the request has
      neither an actor header nor a forwarding header; ``sub`` is
      ``loopback_sub``;
    - family ingress: an ``x-family-actor`` signed with ``key``
      (``sign_family_actor``) for ``audience``, unexpired at ``clock()``
      (refused from ``exp`` on), naming this request's method and path (query
      ignored); ``sub`` is the claim's. The family portal proxies from
      loopback, so a non-loopback peer refuses whatever it carries.

    The ``Actor`` is deterministic and bound to the raw request (its
    ``request_id``, sha256 of its body): ``actor_binds_request`` holds.
    A refusal raises ``CommandAuthorityRefused`` with reason
    ``command_unauthorized`` and ``detail`` naming the mode: ``wrong_peer``,
    ``forwarded_without_actor``, ``bad_signature``, ``wrong_audience``,
    ``actor_expired``, ``wrong_route``, ``loopback_disabled``,
    ``family_disabled``, or ``refuse_with`` verbatim (forced refusal).
    ``calls`` (every request), ``actors`` (minted) and ``refusals``
    (request, detail) record everything. ``make_request`` builds requests.

    ``family_mode="v2"`` (Wave 3, C3.1) judges an ``x-family-actor`` the
    way the real family authority does: the contract's
    ``verify_family_command`` against ``portal`` (default: a
    ``FakeFamilyPortal`` on this clock), then a single-use ``jti`` kept in
    memory (``seen_jtis``; a second use is ``assertion_replayed``); the
    details are ``FamilyRefusal`` values (``allow_family = False`` is
    ``family_ingress_disabled``). ``make_request(family_sub=...)`` then
    builds the request as the portal forwards it (public Host and Origin,
    path ``/api/commands``). The loopback path is the same in both modes;
    ``"v1"`` (the default) is exactly the Wave 2 behaviour.
    """

    def __init__(
        self,
        *,
        key: str = FAKE_FAMILY_KEY,
        audience: str = FAMILY_ACTOR_AUDIENCE,
        loopback_sub: str = "operator",
        clock: Callable[[], datetime] | None = None,
        refuse_with: str | None = None,
        family_mode: str = "v1",
        portal: FakeFamilyPortal | None = None,
    ) -> None:
        if family_mode not in ("v1", "v2"):
            raise ValueError("family_mode is 'v1' (Wave 2) or 'v2' (Wave 3 C3.1)")
        self.key = key
        self.audience = audience
        self.loopback_sub = loopback_sub
        self.clock: Callable[[], datetime] = clock or SessionClock()
        self.refuse_with = refuse_with
        self.family_mode = family_mode
        self.portal: FakeFamilyPortal | None = None
        if family_mode == "v2":
            self.portal = portal if portal is not None else FakeFamilyPortal(clock=self.clock)
        self.seen_jtis: set[str] = set()
        self.allow_loopback = True
        self.allow_family = True
        self.calls: list[RawCommandRequest] = []
        self.actors: list[Actor] = []
        self.refusals: list[tuple[RawCommandRequest, str]] = []

    def verify(self, raw_request: RawCommandRequest) -> Actor:
        self.calls.append(raw_request)
        try:
            actor = self._verify(raw_request)
        except CommandAuthorityRefused as refused:
            self.refusals.append((raw_request, refused.detail))
            raise
        self.actors.append(actor)
        return actor

    def _verify(self, raw: RawCommandRequest) -> Actor:
        if self.refuse_with is not None:
            raise CommandAuthorityRefused(detail=self.refuse_with)
        token = raw.headers.get(FAMILY_ACTOR_HEADER)
        if token is not None and self.family_mode == "v2":
            return self._verify_v2(raw)
        if raw.peer_host not in LOOPBACK_PEERS:
            raise CommandAuthorityRefused(detail="wrong_peer")
        if token is None:
            if not self.allow_loopback:
                raise CommandAuthorityRefused(detail="loopback_disabled")
            if any(h in raw.headers for h in FORWARDED_HEADERS):
                raise CommandAuthorityRefused(detail="forwarded_without_actor")
            return self._actor(self.loopback_sub, "loopback", raw)
        if not self.allow_family:
            raise CommandAuthorityRefused(detail="family_disabled")
        claims = self._claims(token)
        if claims.get("v") != 1 or claims.get("aud") != self.audience:
            raise CommandAuthorityRefused(detail="wrong_audience")
        exp = claims.get("exp")
        if not isinstance(exp, int) or self.clock().timestamp() >= exp:
            raise CommandAuthorityRefused(detail="actor_expired")
        if (
            claims.get("method") != raw.method.upper()
            or claims.get("path") != raw.path.split("?")[0]
        ):
            raise CommandAuthorityRefused(detail="wrong_route")
        sub = claims.get("sub")
        if not isinstance(sub, str) or not sub:
            raise CommandAuthorityRefused(detail="bad_signature")
        return self._actor(sub, "family", raw)

    def _verify_v2(self, raw: RawCommandRequest) -> Actor:
        """C3.1: the contract's pure verifier, then the single-use jti."""
        if not self.allow_family or self.portal is None:
            raise CommandAuthorityRefused(detail="family_ingress_disabled")
        actor, claims = verify_family_command(raw, config=self.portal.config(), now=self.clock())
        if claims.jti in self.seen_jtis:
            raise CommandAuthorityRefused(detail="assertion_replayed")
        self.seen_jtis.add(claims.jti)
        return actor

    def _claims(self, token: str) -> dict[str, Any]:
        payload, dot, signature = token.partition(".")
        try:
            want = hmac.new(self.key.encode("utf-8"), payload.encode("ascii"), hashlib.sha256)
            ok = dot == "." and hmac.compare_digest(_b64url_decode(signature), want.digest())
            claims = json.loads(_b64url_decode(payload)) if ok else None
        except (ValueError, UnicodeError):
            claims = None
        if not isinstance(claims, dict):
            raise CommandAuthorityRefused(detail="bad_signature")
        return claims

    @staticmethod
    def _actor(sub: str, ingress: str, raw: RawCommandRequest) -> Actor:
        return Actor.model_validate(
            {
                "sub": sub,
                "ingress": ingress,
                "request_id": raw.request_id,
                "request_sha256": sha256_hex(raw.body),
            }
        )

    def family_actor(
        self, sub: str = "owner@example.com", method: str = "POST", path: str = FAKE_COMMAND_PATH
    ) -> str:
        """A valid actor header value for this authority, signed now."""
        return sign_family_actor(
            self.key,
            sub=sub,
            method=method,
            path=path,
            iat=int(self.clock().timestamp()),
            aud=self.audience,
        )

    def make_request(
        self,
        body: bytes,
        *,
        family_sub: str | None = None,
        peer_host: str = "127.0.0.1",
        method: str = "POST",
        path: str | None = None,
        request_id: str = "req-0001",
        headers: Mapping[str, str] | None = None,
    ) -> RawCommandRequest:
        """A raw request: loopback by default, family-signed with ``family_sub``
        (v1: a route-bound actor; v2: as ``portal`` forwards it). ``path``
        defaults to ``FAKE_COMMAND_PATH`` (v1) or ``/api/commands`` (v2)."""
        if path is None:
            path = FAMILY_COMMAND_PATH if self.family_mode == "v2" else FAKE_COMMAND_PATH
        merged = {k.lower(): v for k, v in (headers or {}).items()}
        if family_sub is not None and self.portal is not None:
            merged = {**self.portal.headers(body, sub=family_sub), **merged}
        elif family_sub is not None:
            merged[FAMILY_ACTOR_HEADER] = self.family_actor(family_sub, method, path.split("?")[0])
        return RawCommandRequest(
            method=method,
            path=path,
            headers=merged,
            body=body,
            peer_host=peer_host,
            request_id=request_id,
        )


# -- Wave 3 C3.1: family actor protocol 2 (request-bound), additive -------------

#: The golden fixture's TEST-ONLY key (bytes 0..31); never a real key.
FAMILY_V2_KEY = bytes(range(32))
FAMILY_V2_OWNER = "owner@example.com"
FAMILY_V2_ORIGIN = "https://family.example.net"


class FakeFamilyPortal:
    """The Family Apps portal in front of one TREX instance (``/trex`` or
    ``/trex-staging``), as lane A's ``signTrexCommand`` behaves (C3.1).

    ``sign`` mints the ``trex-family-command/1`` assertion for a body (the
    contract's ``mint_family_command``: sub trimmed and lower-cased, ``exp =
    iat + 60``, ``request_id`` = the body's idempotency key); ``forward``
    builds the ``RawCommandRequest`` trex_web then sees: loopback peer, the
    public ``Host`` and ``Origin`` unchanged, the app prefix stripped from
    the path. ``iat`` defaults to ``clock()``; each assertion gets a fresh,
    deterministic ``jti``. ``env`` and ``write_key_file`` configure a real
    ``FamilyCommandAuthority`` for this portal; ``config`` the pure verifier.
    """

    def __init__(
        self,
        *,
        key: bytes = FAMILY_V2_KEY,
        origin: str = FAMILY_V2_ORIGIN,
        prefix: str = "/trex",
        owners: Iterable[str] = (FAMILY_V2_OWNER,),
        clock: Callable[[], datetime] | None = None,
        jti_seed: int = 0,
    ) -> None:
        if prefix not in PORTAL_ROUTES:
            raise ValueError(f"the portal serves TREX only under {sorted(PORTAL_ROUTES)}")
        self.key = key
        self.origin = origin
        self.host = origin.removeprefix("https://")
        self.prefix = prefix
        self.audience = PORTAL_ROUTES[prefix][0]
        self.owners = tuple(owners)
        self.clock: Callable[[], datetime] = clock or SessionClock()
        self.jti_seed = jti_seed
        self.minted = 0
        self.forwarded = 0

    def next_jti(self) -> str:
        self.minted += 1
        seed = f"fake-portal-jti-{self.jti_seed}-{self.minted}".encode()
        return hashlib.sha256(seed).hexdigest()[:32]

    def sign(
        self,
        body: bytes,
        *,
        sub: str = FAMILY_V2_OWNER,
        iat: int | None = None,
        jti: str | None = None,
    ) -> str:
        return mint_family_command(
            self.key,
            sub=sub,
            aud=self.audience,
            origin=self.origin,
            body=body,
            iat=int(self.clock().timestamp()) if iat is None else iat,
            jti=self.next_jti() if jti is None else jti,
        )

    def headers(
        self,
        body: bytes,
        *,
        sub: str = FAMILY_V2_OWNER,
        iat: int | None = None,
        jti: str | None = None,
    ) -> dict[str, str]:
        """What the portal forwards: Host and Origin unchanged plus the assertion."""
        return {
            "host": self.host,
            "origin": self.origin,
            "content-type": "application/json",
            FAMILY_ACTOR_HEADER: self.sign(body, sub=sub, iat=iat, jti=jti),
        }

    def forward(
        self,
        body: bytes,
        *,
        sub: str = FAMILY_V2_OWNER,
        portal_path: str | None = None,
        method: str = "POST",
        request_id: str | None = None,
        peer_host: str = "127.0.0.1",
        iat: int | None = None,
        jti: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> RawCommandRequest:
        """The request trex_web receives for a browser POST to ``portal_path``
        (default ``<prefix>/api/commands``); ``headers`` override the portal's."""
        portal_path = portal_path if portal_path is not None else self.prefix + FAMILY_COMMAND_PATH
        if not portal_path.startswith(self.prefix + "/"):
            raise ValueError(f"{portal_path!r} is not under {self.prefix}/")
        self.forwarded += 1
        merged = self.headers(body, sub=sub, iat=iat, jti=jti)
        merged.update({k.lower(): v for k, v in (headers or {}).items()})
        return RawCommandRequest(
            method=method,
            path=portal_path.removeprefix(self.prefix),
            headers=merged,
            body=body,
            peer_host=peer_host,
            request_id=request_id or f"web-fake-portal-{self.forwarded:04d}",
        )

    def env(self) -> dict[str, str]:
        """The TREX settings that trust this portal (the key file aside)."""
        return {
            "TREX_OWNER_EMAILS": ",".join(self.owners),
            "TREX_FAMILY_ORIGINS": self.origin,
            "TREX_FAMILY_AUDIENCE": self.audience,
        }

    def config(self) -> FamilyAuthorityConfig:
        return family_config_from_env(self.env(), self.key)

    def write_key_file(self, path: Path) -> Path:
        """The key in the pairing-key format: standard padded base64, 0600."""
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        try:
            os.write(fd, base64.b64encode(self.key) + b"\n")
        finally:
            os.close(fd)
        path.chmod(0o600)
        return path


# -- C2.8.5 session clock -----------------------------------------------------

#: 09:30 ET on Thu 2026-10-22: the contract fixtures' desk start.
CLOCK_START = datetime(2026, 10, 22, 13, 30, tzinfo=UTC)
#: The first epoch is then the fixtures' ``owner-20261022T133000Z-4242``.
FIXTURE_EPOCH_SEED = 4242


class SessionClock:
    """A fake desk clock over the trex NYSE calendar (``trex.clock``),
    returning ET-aware instants like ``desk_runtime``'s default clock: pass
    the instance (it is callable) as ``clock=``.

    It moves only when told (``advance``, ``to``, ``to_et``, ``to_open``,
    ``to_close``, ``to_next_open``) or when a ``Wave2Gateway`` sleeps, and
    never backwards (ValueError). Session facts come from the calendar,
    early closes included (``close_of(date(2026, 11, 27))`` is 13:00 ET).

    ``restart(down_s=..., until=...)`` is a desk stop/start: the clock jumps
    over the downtime and mints a new ``owner_epoch``
    (``owner-<UTC stamp>-<seed + restarts>``), so approvals bound to the old
    epoch stop matching (C2.3, default 7). ``epochs`` lists every epoch.
    """

    def __init__(
        self, start: datetime = CLOCK_START, *, epoch_seed: int = FIXTURE_EPOCH_SEED
    ) -> None:
        if start.tzinfo is None:
            raise ValueError("SessionClock needs a timezone-aware start")
        self._now = start.astimezone(ET)
        self.calendar = session_calendar()
        self.epoch_seed = epoch_seed
        self.restarts = 0
        self.owner_epoch = self._mint()
        self.epochs = [self.owner_epoch]

    def __call__(self) -> datetime:
        return self._now

    def now(self) -> datetime:
        return self._now

    def utc(self) -> datetime:
        return self._now.astimezone(UTC)

    def today(self) -> date:
        """The ET date of now (a session or not)."""
        return self._now.date()

    # -- moving -------------------------------------------------------------

    def advance(self, seconds: float) -> datetime:
        if seconds < 0:
            raise ValueError("the clock never runs backwards")
        # elapsed time is UTC arithmetic (aware ET + timedelta is wall time)
        self._now = (self.utc() + timedelta(seconds=seconds)).astimezone(ET)
        return self._now

    def to(self, instant: datetime) -> datetime:
        if instant.tzinfo is None:
            raise ValueError("naive instant")
        if instant < self._now:
            raise ValueError(f"the clock never runs backwards ({instant} < {self._now})")
        self._now = instant.astimezone(ET)
        return self._now

    def to_et(self, wall: str, day: date | None = None) -> datetime:
        """Move to ET wall time ``"HH:MM"`` on ``day`` (default today)."""
        hours, minutes = (int(part) for part in wall.split(":"))
        return self.to(datetime.combine(day or self.today(), time(hours, minutes), tzinfo=ET))

    def to_open(self, day: date | None = None) -> datetime:
        return self.to(self.open_of(day))

    def to_close(self, day: date | None = None, *, before_s: int = 0) -> datetime:
        return self.to(self.close_of(day) - timedelta(seconds=before_s))

    def to_next_open(self) -> datetime:
        """Overnight: the open of the first session after today."""
        return self.to(self.open_of(self.next_session()))

    def restart(self, *, down_s: float = 60.0, until: datetime | None = None) -> str:
        """Desk stop/start: skip the downtime, mint and return a new owner epoch."""
        if until is not None:
            self.to(until)
        else:
            self.advance(down_s)
        self.restarts += 1
        self.owner_epoch = self._mint()
        self.epochs.append(self.owner_epoch)
        return self.owner_epoch

    def _mint(self) -> str:
        return f"owner-{self.utc():%Y%m%dT%H%M%SZ}-{self.epoch_seed + self.restarts}"

    # -- session facts ------------------------------------------------------

    def is_session(self, day: date | None = None) -> bool:
        return self.calendar.is_session(day or self.today())

    def open_of(self, day: date | None = None) -> datetime:
        return self.calendar.session_open(day or self.today()).astimezone(ET)

    def close_of(self, day: date | None = None) -> datetime:
        """The session's close in ET (13:00 on an early close)."""
        return self.calendar.session_close(day or self.today()).astimezone(ET)

    def is_early_close(self, day: date | None = None) -> bool:
        return (day or self.today()) in self.calendar.early_close_sessions()

    def in_session(self) -> bool:
        day = self.today()
        return self.is_session(day) and self.open_of(day) <= self._now < self.close_of(day)

    def next_session(self, day: date | None = None) -> date:
        """The first session strictly after ``day`` (default today)."""
        sessions = self.calendar.sessions()
        return sessions[bisect.bisect_right(sessions, day or self.today())]

    def trading_session(self) -> date:
        """The session a DAY order placed now works in: today's until its
        close, else the next one."""
        day = self.today()
        if self.is_session(day) and self._now < self.close_of(day):
            return day
        return self.next_session(day)


# -- C2.8.2-4 stock wire, executions, positions ---------------------------------

SPY_CON_ID = 756733
#: ``reqMarketDataType`` codes as IBKR delivers them (contracts.proposals.Quote).
MARKET_DATA_TYPES = {"realtime": 1, "frozen": 2, "delayed": 3}
#: IBKR exec ids ``<prefix>.<serial>.<seq>.<version>``; the first fake one is
#: the contract fixture's ``0000e0d5.6576d6a5.01.01``.
EXEC_ID_PREFIX = "0000e0d5"
FIRST_EXEC_SERIAL = 0x6576D6A5
DEFAULT_COMMISSION_MICROS = 1_000_000
#: States in which a scripted fill may execute.
FILLABLE_STATES = frozenset({"PreSubmitted", "Submitted"})
MAX_LIMIT_CENTS = 10**9


def _stock_key(symbol: str) -> ContractKey:
    return ("STK", symbol, "", 0.0, "")


def _whole(value: Any) -> int | None:
    """A whole positive share count, else None (fractional, zero, junk)."""
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not number.is_finite() or number <= 0 or number != number.to_integral_value():
        return None
    return int(number)


def _penny_cents(price: Any) -> int | None:
    """A positive limit on a penny tick, in cents, else None."""
    try:
        cents = Decimal(str(price)) * 100
    except (InvalidOperation, ValueError):
        return None
    if not cents.is_finite() or cents <= 0 or cents > MAX_LIMIT_CENTS:
        return None
    return int(cents) if cents == cents.to_integral_value() else None


def _blank_commission_report() -> Any:
    """ib_async's ``CommissionReport()`` as a fresh ``Fill`` carries it."""
    return SimpleNamespace(
        execId="", commission=0.0, currency="", realizedPNL=0.0, yield_=0.0, yieldRedemptionDate=0
    )


class FakeEvent:
    """The eventkit ``Event`` subset ib_async users touch: ``+=`` / ``-=``
    (or ``connect`` / ``disconnect``) handlers; ``emit`` calls them in order.
    ``emitted`` records every emission's arguments."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.handlers: list[Callable[..., Any]] = []
        self.emitted: list[tuple[Any, ...]] = []

    def connect(self, handler: Callable[..., Any]) -> None:
        self.handlers.append(handler)

    def disconnect(self, handler: Callable[..., Any]) -> None:
        self.handlers.remove(handler)

    def __iadd__(self, handler: Callable[..., Any]) -> FakeEvent:
        self.connect(handler)
        return self

    def __isub__(self, handler: Callable[..., Any]) -> FakeEvent:
        self.disconnect(handler)
        return self

    def emit(self, *args: Any) -> None:
        self.emitted.append(args)
        for handler in list(self.handlers):
            handler(*args)


@dataclass(frozen=True)
class FillStep:
    """One scripted execution of a stock order (one per gateway ``sleep``).

    ``quantity`` None = everything still open (last step only);
    ``price_micros`` None = the order's limit (sub-penny is fine, through
    the limit is refused); ``commission_micros`` arrives in the LATE
    commission report; ``exchange`` is the execution venue."""

    quantity: int | None = None
    price_micros: int | None = None
    commission_micros: int = DEFAULT_COMMISSION_MICROS
    exchange: str = "ARCA"


@dataclass
class StockTicker(FakeTicker):
    """ib_async ``Ticker`` subset for a stock: NaN until the first tick (as
    ib_async), dollars, ``time`` = the last tick's UTC instant,
    ``marketDataType`` 1 realtime / 2 frozen / 3 delayed."""

    bid: float | None = math.nan
    ask: float | None = math.nan
    contract: Any = None
    bidSize: float = math.nan
    askSize: float = math.nan
    time: datetime | None = None
    marketDataType: int = 1


@dataclass
class StockStatus(FakeStatus):
    """ib_async ``OrderStatus`` subset of a stock order."""

    remaining: int = 0
    lastFillPrice: float = 0.0
    permId: int = 0


@dataclass
class StockTrade(FakeTrade):
    """ib_async ``Trade`` subset: ``fills`` in arrival order and ``log``."""

    orderStatus: StockStatus = field(default_factory=StockStatus)
    fills: list[Any] = field(default_factory=list)
    log: list[Any] = field(default_factory=list)


@dataclass
class _StockOrder:
    trade: StockTrade
    symbol: str
    con_id: int
    side: str
    total: int
    limit_micros: int
    session: date
    plan: deque[FillStep] = field(default_factory=deque)
    filled: int = 0
    notional_micros: int = 0
    cancel_requested: bool = False


@dataclass
class _Holding:
    symbol: str
    quantity: int = 0
    cost_micros: int = 0


@dataclass
class _PendingReport:
    due: int | None
    trade: StockTrade
    fill: Any
    commission_micros: int


class Wave2Gateway(SupervisedGateway):
    """``SupervisedGateway`` plus the Wave 2 stock wire (C2.8 items 2-4).
    Option behaviour is inherited unchanged; the account defaults to the
    contract fixtures' ``DU0000001``.

    Contracts: ``qualifyContracts`` resolves a STK contract by symbol from
    ``stocks`` (default SPY -> 756733; ``add_stock``); a preset ``con_ids``
    key wins; any other symbol stays unqualified (conId 0). As in ib_async
    2.x, an unqualified contract's result slot is None.

    Market data: ``reqMarketDataType`` is recorded (``md_type_calls``); the
    type IBKR delivers per symbol is ``set_market_data_type(symbol,
    "realtime"|"frozen"|"delayed")`` (default realtime) and lands on the
    ticker's ``marketDataType``. ``script_ticks(symbol, [(bid_cents,
    ask_cents), ...])`` / ``script_bids`` queue NBBO ticks; each ``sleep``
    delivers the next one to a subscribed stock ticker (dollars, ``time`` =
    the clock's UTC now); ``push_tick`` delivers at once.

    Orders: a STK ``placeOrder`` must be the Wave 2 shape (a known stock
    conId, SMART, USD, LMT, DAY, outsideRth False, transmit, a managed
    ``account``, a whole positive quantity, a penny limit, BUY tagged
    ``trex:sup:`` / SELL tagged ``trex:desk:``, never selling more than held
    net of working sells). Anything else raises AssertionError and is
    recorded in ``wire_violations``, so a swallowed raise still fails a test.
    ``script_fills(order_ref, steps)`` (before or after placement):
    ``[FillStep()]`` full at the limit, ``[FillStep(1)]`` of 2 partial
    (stays Submitted, remaining 1), no script none (rests; ``auto_fill``
    makes unscripted orders fill in full). One step per ``sleep``, after the
    inherited status walk; a completed order is Filled. ``cancelOrder``:
    PendingCancel, then Cancelled on the next sleep (open steps dropped). A
    DAY order is Cancelled once the clock reaches its session's close
    (early closes included).

    Executions: each fill is an ib_async-shaped ``Fill`` (contract,
    execution, commissionReport, time) with an IBKR exec id, permId,
    orderId, clientId 83, orderRef, account, BOT/SLD, cumQty and avgPrice;
    it is appended to the trade's ``fills``, ``fills()`` and
    ``reqExecutions()`` and emitted on ``execDetailsEvent(trade, fill)``.
    Its commission report is LATE: blank until ``commission_lag`` sleeps
    later (default 1; 0 = same sleep; None = only ``deliver_commissions``),
    then filled in place and emitted on ``commissionReportEvent(trade, fill,
    report)``. ``correct_execution`` re-reports a fill: same base exec id,
    last segment incremented (``.01`` -> ``.02``), adjusted quantity/price.

    Positions: stock fills and corrections move a broker position per
    (account, conId); a closed one stays as a 0 row. ``positions()`` = the
    manual ``position_rows`` + those + ``add_broker_position`` rows;
    ``portfolio()`` = the latter two with marks (``marks[con_id]`` micros,
    else the last tick's mid, else the average cost). The average cost
    includes buy commissions once reported (as IBKR's). Faults:
    ``add_broker_position`` (an unexplained or outside position, a
    fractional quantity, an unknown secType), ``hide_position`` (missing),
    ``override_position`` (quantity mismatch), ``account_view_error``
    (positions, portfolio and account values unreadable).

    Time: ``clock`` (a ``SessionClock``) stamps executions and ticks; each
    ``sleep(s)`` advances it by ``s`` unless ``sleep_advances_clock`` is off.
    """

    def __init__(
        self,
        con_ids: dict[ContractKey, int] | None = None,
        account: str = "DU0000001",
        *,
        stocks: Mapping[str, int] | None = None,
        clock: SessionClock | None = None,
    ) -> None:
        super().__init__(con_ids=dict(con_ids or {}), account=account)
        self.stocks: dict[str, int] = dict({"SPY": SPY_CON_ID} if stocks is None else stocks)
        self.clock = clock if clock is not None else SessionClock()
        self.client_id = SUPERVISED_CLIENT_ID
        self.sleep_advances_clock = True
        self.account_values["SettledCash"] = "1000000.00"
        self.account_view_error: BaseException | None = None
        self.md_type_calls: list[int] = []
        self.md_types: dict[str, str] = {}
        self.wire_violations: list[tuple[str, tuple[str, ...]]] = []
        self.auto_fill = False
        self.commission_lag: int | None = 1
        self.marks: dict[int, int] = {}
        self.execDetailsEvent = FakeEvent("execDetailsEvent")
        self.commissionReportEvent = FakeEvent("commissionReportEvent")
        self._steps = 0
        self._exec_serial = FIRST_EXEC_SERIAL - 1
        self._stock_orders: dict[int, _StockOrder] = {}
        self._fill_scripts: dict[str, list[FillStep]] = {}
        self._tick_scripts: dict[str, deque[tuple[int | None, int | None]]] = {}
        self._pending_reports: list[_PendingReport] = []
        self._exec_figures: dict[str, tuple[int, int]] = {}  # exec id -> (qty, price micros)
        self._holdings: dict[tuple[str, int], _Holding] = {}
        self._extra_rows: list[tuple[Any, Any]] = []  # (Position, PortfolioItem)
        self._hidden: dict[int, bool] = {}  # con_id -> report a 0 row instead
        self._overrides: dict[int, Any] = {}

    # -- contracts ------------------------------------------------------------

    def add_stock(self, symbol: str, con_id: int) -> None:
        self.stocks[symbol] = con_id
        self.unknown.discard(_stock_key(symbol))

    def qualifyContracts(self, *contracts: Any) -> list[Any]:
        for c in contracts:
            if getattr(c, "secType", "") != "STK":
                continue
            key = contract_key(c)
            if key in self.con_ids:
                continue
            con = self.stocks.get(str(c.symbol))
            if con is None:
                self.unknown.add(key)
            else:
                self.con_ids[key] = con
        super().qualifyContracts(*contracts)
        return [c if getattr(c, "conId", 0) else None for c in contracts]

    def _stock_ids(self) -> set[int]:
        return {*self.stocks.values(), *(v for k, v in self.con_ids.items() if k[0] == "STK")}

    def _symbol_of(self, contract: Any) -> str:
        symbol = str(getattr(contract, "symbol", "") or "")
        if symbol:
            return symbol
        con = int(getattr(contract, "conId", 0) or 0)
        named = [s for s, c in self.stocks.items() if c == con]
        return named[0] if named else str(con)

    # -- market data ----------------------------------------------------------

    def reqMarketDataType(self, marketDataType: int) -> None:
        self.md_type_calls.append(marketDataType)

    def _md_code(self, symbol: str) -> int:
        return MARKET_DATA_TYPES[self.md_types.get(symbol, "realtime")]

    def set_market_data_type(self, symbol: str, kind: str) -> None:
        if kind not in MARKET_DATA_TYPES:
            raise ValueError(f"market data type is one of {sorted(MARKET_DATA_TYPES)}")
        self.md_types[symbol] = kind
        for ticker in self.tickers.values():
            if isinstance(ticker, StockTicker) and self._symbol_of(ticker.contract) == symbol:
                ticker.marketDataType = MARKET_DATA_TYPES[kind]

    def reqMktData(self, contract: Any, *args: Any, **kwargs: Any) -> FakeTicker:
        if getattr(contract, "secType", "") == "STK" and not isinstance(
            self.tickers.get(contract.conId), StockTicker
        ):
            self.tickers[contract.conId] = StockTicker(
                contract=contract, marketDataType=self._md_code(self._symbol_of(contract))
            )
        return super().reqMktData(contract, *args, **kwargs)

    def script_ticks(self, symbol: str, ticks: Iterable[tuple[int | None, int | None]]) -> None:
        """Queue NBBO ticks ``(bid_cents, ask_cents)`` (None = no bid/ask)."""
        self._tick_scripts.setdefault(symbol, deque()).extend(ticks)

    def script_bids(self, symbol: str, bids_cents: Iterable[int], spread_cents: int = 4) -> None:
        """Queue a bid sequence (e.g. for ``stop_confirm_ticks``), ask = bid + spread."""
        self.script_ticks(symbol, [(bid, bid + spread_cents) for bid in bids_cents])

    def push_tick(self, symbol: str, bid_cents: int | None, ask_cents: int | None) -> StockTicker:
        ticker = self._stock_ticker(symbol)
        if ticker is None:
            raise AssertionError(f"{symbol}: no live stock market-data subscription")
        self._apply_tick(ticker, bid_cents, ask_cents)
        return ticker

    def _stock_ticker(self, symbol: str) -> StockTicker | None:
        con = self.con_ids.get(_stock_key(symbol), self.stocks.get(symbol))
        if con is None or con not in self._md_latest:
            return None
        ticker = self.tickers.get(con)
        return ticker if isinstance(ticker, StockTicker) else None

    def _apply_tick(self, ticker: StockTicker, bid: int | None, ask: int | None) -> None:
        ticker.bid = math.nan if bid is None else bid / 100
        ticker.ask = math.nan if ask is None else ask / 100
        ticker.bidSize = math.nan if bid is None else 100.0
        ticker.askSize = math.nan if ask is None else 100.0
        ticker.time = self.clock.utc()
        ticker.marketDataType = self._md_code(self._symbol_of(ticker.contract))

    # -- orders ---------------------------------------------------------------

    def script_fills(self, order_ref: str, steps: Iterable[FillStep]) -> None:
        """Fills for the order tagged ``order_ref``: attached to its working
        order if one exists, else to the next one placed with that tag."""
        steps = list(steps)
        working = [
            so
            for so in self._stock_orders.values()
            if so.trade.order.orderRef == order_ref
            and so.trade.orderStatus.status not in SDK_DONE_STATES
        ]
        if working:
            so = working[-1]
            plan = [*so.plan, *steps]
            self._check_plan(so, plan)
            so.plan = deque(plan)
        else:
            self._fill_scripts.setdefault(order_ref, []).extend(steps)

    @staticmethod
    def _check_plan(so: _StockOrder, plan: list[FillStep]) -> None:
        open_qty = so.total - so.filled
        for i, step in enumerate(plan):
            if step.quantity is None and i != len(plan) - 1:
                raise ValueError("FillStep() (the rest) must be the last step")
            qty = open_qty if step.quantity is None else step.quantity
            if not 1 <= qty <= open_qty:
                raise ValueError(f"fill plan overfills: {qty} of {open_qty} open")
            open_qty -= qty
            price = so.limit_micros if step.price_micros is None else step.price_micros
            through = price > so.limit_micros if so.side == "BUY" else price < so.limit_micros
            if price < 0 or through:
                raise ValueError("a limit order never fills through its limit")

    def _held(self, account: str, con_id: int) -> int:
        holding = self._holdings.get((account, con_id))
        manual = sum(
            int(row.position)
            for row in self.position_rows
            if row.account == account and row.contract.conId == con_id
        )
        return (holding.quantity if holding else 0) + manual

    def _working_sells(self, account: str, con_id: int) -> int:
        return sum(
            so.total - so.filled
            for so in self._stock_orders.values()
            if so.side == "SELL"
            and so.con_id == con_id
            and so.trade.order.account == account
            and so.trade.orderStatus.status not in SDK_DONE_STATES
        )

    def _stock_order_problems(self, contract: Any, order: Any) -> tuple[str, ...]:
        problems: list[str] = []
        con = int(getattr(contract, "conId", 0) or 0)
        if con not in self._stock_ids():
            problems.append("con_id")
        if getattr(contract, "exchange", "") != "SMART":
            problems.append("exchange")
        if getattr(contract, "currency", "") != "USD":
            problems.append("currency")
        action = getattr(order, "action", "")
        if action not in ("BUY", "SELL"):
            problems.append("action")
        if getattr(order, "orderType", "") != "LMT":
            problems.append("order_type")
        if getattr(order, "tif", "") != "DAY":
            problems.append("tif")
        if getattr(order, "outsideRth", False):
            problems.append("outside_rth")
        if not getattr(order, "transmit", True):
            problems.append("transmit")
        account = getattr(order, "account", "")
        if account not in self.accounts:
            problems.append("account")
        owner = parse_order_ref(getattr(order, "orderRef", ""))
        if owner is None:
            problems.append("order_ref")
        elif (action, owner.kind) not in (("BUY", "sup"), ("SELL", "desk")):
            problems.append("order_ref_side")
        qty = _whole(getattr(order, "totalQuantity", 0))
        if qty is None:
            problems.append("quantity")
        if _penny_cents(getattr(order, "lmtPrice", 0)) is None:
            problems.append("limit_price")
        if (
            action == "SELL"
            and qty is not None
            and qty > self._held(account, con) - self._working_sells(account, con)
        ):
            problems.append("sell_exceeds_position")
        return tuple(problems)

    def placeOrder(self, contract: Any, order: Any) -> FakeTrade:
        if getattr(contract, "secType", "") != "STK":
            return super().placeOrder(contract, order)
        self._fault("placeOrder")
        ref = str(getattr(order, "orderRef", "") or "")
        problems = self._stock_order_problems(contract, order)
        if problems:
            self.wire_violations.append((ref, problems))
            raise AssertionError(f"stock order off the Wave 2 wire contract: {', '.join(problems)}")
        qty = _whole(order.totalQuantity)
        cents = _penny_cents(order.lmtPrice)
        assert qty is not None and cents is not None
        trade = StockTrade(contract=contract, order=order, orderStatus=StockStatus(remaining=qty))
        so = _StockOrder(
            trade=trade,
            symbol=self._symbol_of(contract),
            con_id=int(contract.conId),
            side=str(order.action),
            total=qty,
            limit_micros=cents * 10_000,
            session=self.clock.trading_session(),
        )
        steps = self._fill_scripts.pop(ref, None)
        if steps is None and self.auto_fill:
            steps = [FillStep()]
        if steps:
            self._check_plan(so, steps)
            so.plan = deque(steps)
        self._next_oid += 1
        order.orderId = self._next_oid
        self._perm += 1
        order.permId = trade.orderStatus.permId = self._perm
        self.trades.append(trade)
        self._stock_orders[order.orderId] = so
        walk = list(self.status_script)
        trade.orderStatus.status = walk.pop(0)
        self._walk[id(trade)] = walk
        self._log(trade)
        return trade

    def cancelOrder(self, order: Any) -> None:
        super().cancelOrder(order)
        so = self._stock_orders.get(getattr(order, "orderId", 0))
        if so is not None and so.trade.orderStatus.status not in SDK_DONE_STATES:
            so.cancel_requested = True
            self._walk.pop(id(so.trade), None)
            so.trade.orderStatus.status = "PendingCancel"
            self._log(so.trade)

    def openTrades(self) -> list[FakeTrade]:
        self._fault("openTrades")
        self._expire_day_orders()
        return super().openTrades()

    def _finish(self, so: _StockOrder, status: str) -> None:
        so.plan.clear()
        self._walk.pop(id(so.trade), None)
        so.trade.orderStatus.status = status
        self._log(so.trade)

    def _expire_day_orders(self) -> None:
        now = self.clock.now()
        for so in self._stock_orders.values():
            if so.trade.orderStatus.status in SDK_DONE_STATES:
                continue
            if now >= self.clock.close_of(so.session):
                self._finish(so, "Cancelled")

    def sleep(self, seconds: float) -> None:
        super().sleep(seconds)  # fault, record, status walk
        if self.sleep_advances_clock and seconds > 0:
            self.clock.advance(seconds)
        self._steps += 1
        self._expire_day_orders()
        for so in list(self._stock_orders.values()):
            status = so.trade.orderStatus.status
            if so.cancel_requested and status not in SDK_DONE_STATES:
                self._finish(so, "Cancelled")
            elif so.plan and status in FILLABLE_STATES:
                self._fill(so, so.plan.popleft())
        for symbol, queue in self._tick_scripts.items():
            ticker = self._stock_ticker(symbol)
            if queue and ticker is not None:
                self._apply_tick(ticker, *queue.popleft())
        self._deliver(lambda p: p.due is not None and p.due <= self._steps)

    # -- executions -----------------------------------------------------------

    def _next_exec_id(self) -> str:
        self._exec_serial += 1
        return f"{EXEC_ID_PREFIX}.{self._exec_serial:08x}.01.01"

    def _new_fill(
        self,
        so: _StockOrder,
        *,
        exec_id: str,
        qty: int,
        price_micros: int,
        exchange: str,
        cum_qty: float,
        avg_price: float,
    ) -> Any:
        trade, order = so.trade, so.trade.order
        at = self.clock.utc()
        execution = SimpleNamespace(
            execId=exec_id,
            time=at,
            acctNumber=order.account,
            exchange=exchange,
            side="BOT" if so.side == "BUY" else "SLD",
            shares=float(qty),
            price=price_micros / 1e6,
            permId=order.permId,
            clientId=self.client_id,
            orderId=order.orderId,
            liquidation=0,
            cumQty=cum_qty,
            avgPrice=avg_price,
            orderRef=order.orderRef,
            evRule="",
            evMultiplier=0.0,
            modelCode="",
            lastLiquidity=1,
            pendingPriceRevision=False,
        )
        fill = SimpleNamespace(
            contract=trade.contract,
            execution=execution,
            commissionReport=_blank_commission_report(),
            time=at,
        )
        self._exec_figures[exec_id] = (qty, price_micros)
        trade.fills.append(fill)
        self.executions.append(fill)
        self.fill_rows.append(fill)
        return fill

    def _fill(self, so: _StockOrder, step: FillStep) -> None:
        qty = so.total - so.filled if step.quantity is None else step.quantity
        price = so.limit_micros if step.price_micros is None else step.price_micros
        so.filled += qty
        so.notional_micros += qty * price
        avg = so.notional_micros / so.filled / 1e6
        fill = self._new_fill(
            so,
            exec_id=self._next_exec_id(),
            qty=qty,
            price_micros=price,
            exchange=step.exchange,
            cum_qty=float(so.filled),
            avg_price=avg,
        )
        status = so.trade.orderStatus
        status.filled = so.filled
        status.remaining = so.total - so.filled
        status.avgFillPrice = avg
        status.lastFillPrice = price / 1e6
        if status.remaining == 0:
            status.status = "Filled"
            self._walk.pop(id(so.trade), None)
        self._log(so.trade)
        self._book(so, qty, price)
        self._report_later(so.trade, fill, step.commission_micros)
        self.execDetailsEvent.emit(so.trade, fill)

    def _book(self, so: _StockOrder, qty: int, price_micros: int) -> None:
        account = str(so.trade.order.account)
        holding = self._holdings.setdefault((account, so.con_id), _Holding(symbol=so.symbol))
        if so.side == "BUY":
            holding.quantity += qty
            holding.cost_micros += qty * price_micros
            return
        if holding.quantity:
            holding.cost_micros -= holding.cost_micros * qty // holding.quantity
        holding.quantity -= qty
        if holding.quantity == 0:
            holding.cost_micros = 0

    def _report_later(self, trade: StockTrade, fill: Any, commission_micros: int) -> None:
        due = None if self.commission_lag is None else self._steps + self.commission_lag
        self._pending_reports.append(_PendingReport(due, trade, fill, commission_micros))

    def deliver_commissions(self, exec_id: str | None = None) -> int:
        """Deliver held commission reports now (all, or ``exec_id``'s); the count."""
        return self._deliver(lambda p: exec_id is None or p.fill.execution.execId == exec_id)

    def _deliver(self, ready: Callable[[_PendingReport], bool]) -> int:
        due = [p for p in self._pending_reports if ready(p)]
        for pending in due:
            self._pending_reports.remove(pending)
            fill, report = pending.fill, pending.fill.commissionReport
            report.execId = fill.execution.execId
            report.commission = pending.commission_micros / 1e6
            report.currency = "USD"
            if fill.execution.side == "BOT":
                so = self._stock_orders[fill.execution.orderId]
                holding = self._holdings[(str(fill.execution.acctNumber), so.con_id)]
                if holding.quantity > 0:
                    holding.cost_micros += pending.commission_micros
            self.commissionReportEvent.emit(pending.trade, fill, report)
        return len(due)

    def correct_execution(
        self,
        exec_id: str,
        *,
        quantity: int | None = None,
        price_micros: int | None = None,
        commission_micros: int = 0,
    ) -> Any:
        """IBKR's correction re-report of the fill ``exec_id`` belongs to:
        the same base exec id with the latest version's last segment
        incremented (``.01`` -> ``.02`` -> ``.03``), the adjusted quantity
        and/or price (default: the latest report's), the same permId,
        orderId and orderRef. Delivered at once (``execDetailsEvent``,
        ``reqExecutions``, ``fills()``, the trade's ``fills``); its own
        commission report is late as usual (default 0: no second charge).
        The broker position follows the corrected figures; the order status,
        ``cumQty`` and ``avgPrice`` are left as the corrected report had them."""
        if exec_id not in self._exec_figures:
            raise KeyError(exec_id)
        base = base_exec_id_of(exec_id)
        same = [
            f
            for f in self.executions
            if getattr(f.execution, "execId", "") in self._exec_figures
            and base_exec_id_of(f.execution.execId) == base
        ]
        latest = max(same, key=lambda f: int(f.execution.execId.rpartition(".")[2]))
        version = int(latest.execution.execId.rpartition(".")[2]) + 1
        old_qty, old_price = self._exec_figures[latest.execution.execId]
        new_qty = old_qty if quantity is None else quantity
        new_price = old_price if price_micros is None else price_micros
        if new_qty < 1 or new_price < 0:
            raise ValueError("a corrected execution keeps a positive quantity")
        so = self._stock_orders[latest.execution.orderId]
        fill = self._new_fill(
            so,
            exec_id=f"{base}.{version:02d}",
            qty=new_qty,
            price_micros=new_price,
            exchange=latest.execution.exchange,
            cum_qty=float(latest.execution.cumQty) + new_qty - old_qty,
            avg_price=latest.execution.avgPrice,
        )
        holding = self._holdings[(str(so.trade.order.account), so.con_id)]
        if so.side == "BUY":
            holding.quantity += new_qty - old_qty
            holding.cost_micros += new_qty * new_price - old_qty * old_price
        else:
            holding.quantity -= new_qty - old_qty
        self._report_later(so.trade, fill, commission_micros)
        self.execDetailsEvent.emit(so.trade, fill)
        return fill

    def reqExecutions(self, execFilter: Any = None) -> list[Any]:
        """Every execution today, all clients (the filter is ignored)."""
        return super().reqExecutions()

    # -- positions / portfolio / account -------------------------------------

    def _account_views(self) -> None:
        if self.account_view_error is not None:
            raise self.account_view_error

    def accountValues(self, account: str = "") -> list[Any]:
        self._account_views()
        return [
            SimpleNamespace(
                account=self.accounts[0], tag=tag, value=value, currency="USD", modelCode=""
            )
            for tag, value in self.account_values.items()
            if not account or account == self.accounts[0]
        ]

    @staticmethod
    def _contract(con_id: int, symbol: str, sec_type: str) -> Any:
        return SimpleNamespace(
            conId=con_id,
            secType=sec_type,
            symbol=symbol,
            currency="USD",
            exchange="",
            localSymbol=symbol,
            multiplier="",
            right="",
            strike=0.0,
            lastTradeDateOrContractMonth="",
        )

    def _mark_micros(self, con_id: int, avg_cost_micros: float) -> float:
        if con_id in self.marks:
            return self.marks[con_id]
        ticker = self.tickers.get(con_id)
        if isinstance(ticker, StockTicker):
            bid, ask = ticker.bid, ticker.ask
            if bid is not None and ask is not None and math.isfinite(bid) and math.isfinite(ask):
                return (bid + ask) / 2 * 1e6
        return avg_cost_micros

    def _rows(self, account: str, con_id: int, symbol: str, qty: Any, avg_micros: float) -> Any:
        contract = self._contract(con_id, symbol, "STK")
        position = SimpleNamespace(
            account=account, contract=contract, position=qty, avgCost=avg_micros / 1e6
        )
        price = self._mark_micros(con_id, avg_micros) / 1e6
        item = SimpleNamespace(
            contract=contract,
            position=qty,
            marketPrice=price,
            marketValue=float(qty) * price,
            averageCost=avg_micros / 1e6,
            unrealizedPNL=float(qty) * (price - avg_micros / 1e6),
            realizedPNL=0.0,
            account=account,
        )
        return position, item

    def _derived(self) -> list[tuple[Any, Any]]:
        return [
            self._rows(
                account,
                con,
                h.symbol,
                float(h.quantity),
                h.cost_micros / h.quantity if h.quantity else 0.0,
            )
            for (account, con), h in self._holdings.items()
        ]

    def _faulted(self, rows: list[Any]) -> list[Any]:
        out = []
        for row in rows:
            con = row.contract.conId
            if con in self._hidden and not self._hidden[con]:
                continue
            if con in self._hidden or con in self._overrides:
                row = SimpleNamespace(**vars(row))
                row.position = 0.0 if con in self._hidden else self._overrides[con]
                if hasattr(row, "marketValue"):
                    row.marketValue = float(row.position) * row.marketPrice
            out.append(row)
        return out

    def positions(self, account: str = "") -> list[Any]:
        self._account_views()
        rows = [
            *self.position_rows,
            *(pos for pos, _ in self._derived()),
            *(pos for pos, _ in self._extra_rows),
        ]
        return [r for r in self._faulted(rows) if not account or r.account == account]

    def portfolio(self, account: str = "") -> list[Any]:
        self._account_views()
        items = [*(item for _, item in self._derived()), *(item for _, item in self._extra_rows)]
        return [r for r in self._faulted(items) if not account or r.account == account]

    def add_broker_position(
        self,
        symbol: str,
        quantity: Any,
        *,
        con_id: int | None = None,
        sec_type: str = "STK",
        avg_cost_micros: int = 0,
        account: str | None = None,
    ) -> None:
        """A broker row with no execution behind it: an outside position (a
        contract the ledger never touched), an unexplained one (the ledger
        is flat in it), a fractional ``Decimal``/float quantity, or an
        unknown ``sec_type`` (e.g. ``FUT``), all fail-closed material."""
        con = con_id if con_id is not None else self.stocks.get(symbol)
        if con is None:
            raise ValueError(f"{symbol}: pass con_id for a symbol outside the stock table")
        qty = float(quantity) if isinstance(quantity, int) else quantity
        position, item = self._rows(account or self.accounts[0], con, symbol, qty, avg_cost_micros)
        position.contract.secType = sec_type
        self._extra_rows.append((position, item))

    def hide_position(self, con_id: int, *, zero_row: bool = False) -> None:
        """The broker reports the contract flat (absent, or a 0 row): ``missing``."""
        self._hidden[con_id] = zero_row

    def override_position(self, con_id: int, quantity: Any) -> None:
        """The broker reports another quantity: ``quantity_mismatch`` (or fractional)."""
        self._overrides[con_id] = quantity

    def clear_position_faults(self) -> None:
        self._hidden.clear()
        self._overrides.clear()
        self._extra_rows.clear()
        self.account_view_error = None


# -- C2.8.6 contract builders --------------------------------------------------

CONTRACT_FIXTURES = Path(__file__).resolve().parents[1] / "contracts" / "fixtures"


def contract_fixture(name: str) -> dict[str, Any]:
    """A fresh copy of a ``tests/contracts/fixtures`` document."""
    loaded: dict[str, Any] = json.loads((CONTRACT_FIXTURES / name).read_text())
    return loaded


def equity_effect(**changes: Any) -> SupervisedEquityEffect:
    """The fixture equity effect with top-level changes; ``exit_policy`` is
    merged; a new ``intent_id`` re-tags ``order_ref`` unless one is given."""
    doc = contract_fixture("equity_effect.json")
    doc["exit_policy"].update(changes.pop("exit_policy", {}))
    if "intent_id" in changes and "order_ref" not in changes:
        changes["order_ref"] = supervised_order_ref(changes["intent_id"])
    doc.update(changes)
    return SupervisedEquityEffect.model_validate(doc)


def entry_proposal(
    effect: SupervisedEquityEffect | SupervisedEffect | None = None, **changes: Any
) -> EntryProposal:
    """A minted proposal around ``effect`` (default ``equity_effect()``) on the
    fixture body: ``effect_sha256`` bound; an equity proposal's notional and
    max loss from the contract arithmetic; ``review`` changes merged."""
    effect = effect if effect is not None else equity_effect()
    equity = isinstance(effect, SupervisedEquityEffect)
    body = contract_fixture("proposal_equity.json" if equity else "proposal_option.json")
    del body["proposal_id"]
    body["effect"] = model_document(effect)
    body["effect_sha256"] = effect_v1_sha256(effect)
    if isinstance(effect, SupervisedEquityEffect):
        body["review"]["notional_cents"] = equity_notional_cents(effect)
        body["review"]["max_loss_cents"] = equity_max_loss_cents(effect)
    body["review"].update(changes.pop("review", {}))
    body.update(changes)
    return EntryProposal.create(EntryProposalBody.model_validate(body))


def approve_command(
    proposal: EntryProposal | None = None, *, actor: Actor | None = None, **changes: Any
) -> ApproveEntryCommand:
    """The fixture approve command bound to ``proposal`` (default
    ``entry_proposal()``): its id, file hash, effect hash, epoch and profile;
    ``args`` changes merged, other changes top-level."""
    proposal = proposal if proposal is not None else entry_proposal()
    doc = contract_fixture("command_approve_entry.json")
    doc["args"] = {
        "proposal_id": proposal.proposal_id,
        "proposal_sha256": proposal_sha256(proposal),
        "effect_sha256": proposal.effect_sha256,
        "owner_epoch": proposal.review.owner_epoch,
        "profile_digest": proposal.review.profile_digest,
        **changes.pop("args", {}),
    }
    if actor is not None:
        doc["issued_by"] = model_document(actor)
    doc.update(changes)
    command = parse_command(doc)
    assert isinstance(command, ApproveEntryCommand)
    return command


def mandate_v2(
    proposal: EntryProposal | None = None,
    command: ApproveEntryCommand | None = None,
    *,
    now: datetime | None = None,
) -> SupervisedMandateV2:
    """``mandate_for_approval`` over the builders (default ``now``: the
    fixture mandate's ``granted_at``)."""
    proposal = proposal if proposal is not None else entry_proposal()
    command = command if command is not None else approve_command(proposal)
    granted = datetime.fromisoformat(contract_fixture("mandate_v2.json")["granted_at"])
    return mandate_for_approval(proposal, command, now=now or granted)


def ledger_event(**changes: Any) -> LedgerEvent:
    """The fixture fill event with top-level changes (``attribution`` merged)."""
    doc = contract_fixture("ledger_event_fill.json")
    doc["attribution"].update(changes.pop("attribution", {}))
    doc.update(changes)
    return LedgerEvent.model_validate(doc)
