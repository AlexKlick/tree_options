"""IBKR paper adapter for the supervised paper path's ``SupervisedBroker`` port.

The supervised core (``trex.supervised``) owns authority: mandates, permits,
the outbox and reconciliation. This adapter owns ONLY the wire, and builds
every order from the effect bytes the permit's hash bound:

- ``SupervisedEffect`` is the one order a permit may send (structure, side,
  quantity, limit, bound account, order tag). ``effect_bytes`` is its
  canonical encoding; ``decode_effect`` refuses any payload that is not
  exactly that encoding, so no field can ride along unhashed.
- Orders carry ``orderRef = trex:sup:<intent_id>`` and an EXPLICIT account.
  The existing trex tag parser reads that as structure ``sup:<intent_id>``,
  which no monitor or desk book prepares, so the legacy monitor never adopts
  a supervised order; structure ids starting ``sup:`` are refused here.
- Paper only: the session must be on the paper gateway port, and the bound
  account must be one the session manages with an IBKR paper prefix ("D").
- ``lookup`` answers from the gateway's views across ALL clients (open
  orders, the day's completed orders, the day's executions), matched by the
  order tag. ``NotSubmitted`` is returned only when every view was read;
  any failed read is ``LookupUnknown``. The completed-order and execution
  views cover the current gateway session only: reconcile the same session,
  or check positions by hand after a gateway restart.

Fills after the acknowledgement are not reported here (v1): the receipt
proves the order exists; watching it is the runtime's job.

IBKR lets only the placing clientId cancel an order: the supervised path
uses its own clientId (``SUPERVISED_CLIENT_ID``), distinct from the monitor
(77), discovery (74) and the desk runtime (81).
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import Field, model_validator

from tree_options.action_graph.proposal import canonical_bytes
from tree_options.execution import BrokerAcknowledgement, OrderReject, SubmitAttempt
from tree_options.schemas.common import IdStr, StrictModel
from tree_options.trex.ibkr import GATEWAY_PAPER_PORT, ORDER_REF_PREFIX, IbkrTrex
from tree_options.trex.plan import LegStructure
from tree_options.trex.supervised import (
    Acknowledged,
    LookupUnknown,
    LookupVerdict,
    NotSubmitted,
    Refused,
    SubmissionOutcome,
    Submitted,
    Uncertain,
)

EFFECT_SCHEMA = "supervised-effect/1"
SUPERVISED_REF_PREFIX = ORDER_REF_PREFIX + "sup:"
SUPERVISED_CLIENT_ID = 83
PAPER_ACCOUNT_PREFIX = "D"

#: statuses meaning the broker holds the order (an acknowledgement)
ACK_STATES = frozenset({"PreSubmitted", "Submitted", "Filled"})
#: statuses meaning the order died before working
DEAD_STATES = frozenset({"Cancelled", "ApiCancelled", "Inactive"})
ACK_TIMEOUT_S = 10.0
ACK_POLL_S = 0.25


def supervised_order_ref(intent_id: str) -> str:
    return SUPERVISED_REF_PREFIX + intent_id


class SupervisedEffect(StrictModel):
    """The one order a supervised permit authorizes."""

    schema_version: Literal["supervised-effect/1"] = Field(
        default="supervised-effect/1", alias="schema")
    intent_id: IdStr
    account_id: IdStr
    structure: LegStructure
    side: Literal["BUY", "SELL"]
    quantity: int = Field(strict=True, ge=1)
    limit: Decimal
    order_ref: str

    @model_validator(mode="after")
    def _bind(self) -> SupervisedEffect:
        if self.order_ref != supervised_order_ref(self.intent_id):
            raise ValueError("order_ref must be the intent's supervised tag")
        if self.structure.id.startswith("sup:"):
            raise ValueError("structure ids starting 'sup:' collide with supervised tags")
        if self.side != self.structure.open_side:
            raise ValueError("the supervised path opens packages only")
        if self.quantity > self.structure.quantity:
            raise ValueError("quantity exceeds the structure's packages")
        if not self.account_id.startswith(PAPER_ACCOUNT_PREFIX):
            raise ValueError("the supervised path binds IBKR paper accounts only")
        return self


def effect_bytes(effect: SupervisedEffect) -> bytes:
    """The canonical bytes a permit hashes and the adapter decodes."""
    return canonical_bytes(effect.model_dump(mode="json", by_alias=True))


def decode_effect(payload: bytes) -> SupervisedEffect:
    """Parse and validate, refusing any non-canonical encoding."""
    effect = SupervisedEffect.model_validate(json.loads(payload))
    if effect_bytes(effect) != payload:
        raise ValueError("effect payload is not the canonical encoding")
    return effect


def _utc(clock: Callable[[], datetime]) -> datetime:
    now = clock()
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("clock must return timezone-aware datetimes")
    return now.astimezone(UTC)


def _log_time(trade: Any) -> datetime | None:
    """The newest trade-log timestamp, when ib_async recorded one."""
    stamps = [getattr(entry, "time", None) for entry in getattr(trade, "log", []) or []]
    aware = [s for s in stamps if isinstance(s, datetime) and s.utcoffset() is not None]
    return max(aware).astimezone(UTC) if aware else None


def _reject_reason(trade: Any, status: str) -> str:
    for entry in reversed(getattr(trade, "log", []) or []):
        code = getattr(entry, "errorCode", 0)
        if code:
            return f"IB{code}"
    return status.upper()


class IbkrSupervisedBroker:
    """``SupervisedBroker`` over one connected ``IbkrTrex`` paper session."""

    def __init__(self, ib: IbkrTrex, *, clock: Callable[[], datetime] | None = None,
                 ack_timeout_s: float = ACK_TIMEOUT_S, poll_s: float = ACK_POLL_S) -> None:
        self.ib = ib
        self.clock = clock or (lambda: datetime.now(UTC))
        self.ack_timeout_s = ack_timeout_s
        self.poll_s = poll_s

    # -- guards --------------------------------------------------------------

    def paper_blockers(self, account_id: str) -> list[str]:
        """Why this session may NOT trade ``account_id`` (empty = it may)."""
        blockers: list[str] = []
        if not self.ib.connected:
            return ["gateway_disconnected"]
        if self.ib.port != GATEWAY_PAPER_PORT:
            blockers.append("not_paper_gateway_port")
        if self.ib.client_id != SUPERVISED_CLIENT_ID:
            blockers.append("wrong_client_id")
        try:
            managed = {str(a) for a in self.ib._ib.managedAccounts()}
        except Exception:  # an unreadable account list proves nothing
            managed = set()
        if account_id not in managed:
            blockers.append("account_not_managed_by_session")
        if not account_id.startswith(PAPER_ACCOUNT_PREFIX):
            blockers.append("account_not_paper")
        return blockers

    def preflight(self, effect_payload: bytes) -> list[str]:
        """Blockers observable before a permit: session, encoding, contracts,
        order bounds, and a two-sided package quote. Never places anything."""
        try:
            effect = decode_effect(effect_payload)
        except ValueError as error:
            return [f"effect_invalid:{type(error).__name__}"]
        blockers = self.paper_blockers(effect.account_id)
        if blockers:
            return blockers
        try:
            self.ib.prepare([effect.structure])
            self.ib._order(effect.structure, effect.side, effect.quantity, effect.limit)
        except (ValueError, RuntimeError) as error:
            return [f"order_unbuildable:{error}"]
        if self.ib.package_quote(effect.structure.id) is None:
            blockers.append("package_quote_incomplete")
        return blockers

    # -- SupervisedBroker ----------------------------------------------------

    def submit(self, attempt: SubmitAttempt, effect_payload: bytes) -> SubmissionOutcome:
        """Place the permitted order, or report why the outcome is unknown.

        Every local refusal happens BEFORE placeOrder and is reported as
        ``Uncertain`` with a ``not_sent:`` reason: the core keeps the effect
        held until reconciliation confirms (after its settle window) that
        nothing reached the broker. Nothing here fabricates a broker fact.
        """
        try:
            effect = decode_effect(effect_payload)
        except ValueError as error:
            return Uncertain("not_sent:effect_invalid", repr(error))
        if effect.intent_id != attempt.intent_id:
            return Uncertain("not_sent:intent_mismatch", effect.intent_id)
        blockers = self.paper_blockers(effect.account_id)
        if blockers:
            return Uncertain("not_sent:session", ",".join(blockers))
        existing = self._tagged(effect.order_ref)
        if existing is None:
            return Uncertain("not_sent:duplicate_check_unreadable")
        if existing:
            return Uncertain("not_sent:tag_already_at_broker", effect.order_ref)
        try:
            self.ib.prepare([effect.structure])
            contract, order = self.ib._order(
                effect.structure, effect.side, effect.quantity, effect.limit)
        except (ValueError, RuntimeError) as error:
            return Uncertain("not_sent:order_unbuildable", repr(error))
        order.orderRef = effect.order_ref
        order.account = effect.account_id
        trade = self.ib._ib.placeOrder(contract, order)
        return self._await_ack(attempt, trade)

    def _await_ack(self, attempt: SubmitAttempt, trade: Any) -> SubmissionOutcome:
        polls = max(1, math.ceil(self.ack_timeout_s / self.poll_s))
        for _ in range(polls):
            status = str(getattr(trade.orderStatus, "status", "") or "")
            filled = int(getattr(trade.orderStatus, "filled", 0) or 0)
            order_id = int(getattr(trade.order, "orderId", 0) or 0)
            if status in ACK_STATES and order_id:
                received = _utc(self.clock)
                broker_at = min(_log_time(trade) or received, received)
                broker_at = max(broker_at, attempt.send_attempt_at)
                received = max(received, broker_at)
                sequence = f"ib-ack-{order_id}-{attempt.record_id}"
                return Acknowledged(BrokerAcknowledgement(
                    record_id=sequence, intent_id=attempt.intent_id,
                    broker_order_id=str(order_id), broker_acknowledged_at=broker_at,
                    locally_received_at=received, source="ibkr-supervised",
                    source_sequence_id=sequence, broker_sequence_id=sequence))
            if status in DEAD_STATES and filled == 0:
                received = _utc(self.clock)
                broker_at = max(min(_log_time(trade) or received, received),
                                attempt.send_attempt_at)
                received = max(received, broker_at)
                sequence = f"ib-reject-{order_id}-{attempt.record_id}"
                return Refused(OrderReject(
                    record_id=sequence, intent_id=attempt.intent_id,
                    broker_order_id=str(order_id) if order_id else None,
                    reason_code=_reject_reason(trade, status),
                    broker_acknowledged_at=broker_at, locally_received_at=received,
                    source="ibkr-supervised", source_sequence_id=sequence,
                    broker_sequence_id=sequence))
            if status in DEAD_STATES:
                return Uncertain("dead_with_fills", f"{status} filled={filled}")
            self.ib.sleep(self.poll_s)
        status = str(getattr(trade.orderStatus, "status", "") or "")
        return Uncertain("ack_timeout", f"last status {status or 'none'}")

    def _tagged(self, order_ref: str) -> dict[str, str] | None:
        """Orders carrying ``order_ref`` across every view, keyed by identity
        (permId, IBKR's global order id, else the session orderId) with the
        best broker order id for each; None when any view could not be read
        (absence is then unproven)."""
        ib = self.ib._ib
        found: dict[str, str] = {}

        def note(holder: Any) -> None:
            perm = int(getattr(holder, "permId", 0) or 0)
            oid = int(getattr(holder, "orderId", 0) or 0)
            key = f"perm:{perm}" if perm else f"oid:{oid}"
            if oid or key not in found:
                found[key] = str(oid) if oid else key

        try:
            views: Sequence[Any] = [*ib.reqAllOpenOrders(), *ib.reqCompletedOrders(True)]
            for trade in views:
                if str(getattr(trade.order, "orderRef", "") or "") == order_ref:
                    note(trade.order)
            for fill in ib.reqExecutions():
                if str(getattr(fill.execution, "orderRef", "") or "") == order_ref:
                    note(fill.execution)
        except Exception:  # any failed read: absence is not proven
            return None
        return found

    def lookup(self, intent_id: str) -> LookupVerdict:
        if not self.ib.connected:
            return LookupUnknown("gateway_disconnected")
        found = self._tagged(supervised_order_ref(intent_id))
        if found is None:
            return LookupUnknown("broker_views_unreadable")
        if not found:
            return NotSubmitted()
        if len(found) > 1:
            return LookupUnknown(f"multiple_orders_for_tag:{','.join(sorted(found))}")
        return Submitted(next(iter(found.values())))
