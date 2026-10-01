"""Bounded read-only qualification inside the existing IBKR desk owner.

No session constructor, authority grant or economic effect exists here. All
request completion timestamps are local observations, never broker event time.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from tree_options.action_graph.proposal import canonical_bytes
from tree_options.time.sessions import shift_instant
from tree_options.trex.account_ownership import AccountOwnership
from tree_options.trex.supervised import SupervisedRefused, _atomic_write
from tree_options.trex.supervised_desk import SupervisedDesk

SCHEMA = "trex.ibkr.read-only-qualification/v1"
TTL_SECONDS = 30
READ_TIMEOUT_SECONDS = 5
WORKING_STATES = {"PendingSubmit", "PreSubmitted", "Submitted", "PendingCancel", "ApiPending"}
TERMINAL_STATES = {"Filled", "Cancelled", "ApiCancelled", "Inactive"}


def _aware(now: datetime) -> datetime:
    if now.tzinfo is None or now.utcoffset() is None:
        raise SupervisedRefused("qualification_clock_naive")
    return now


def _owner_findings(desk: SupervisedDesk, fence: AccountOwnership, alias: str) -> list[str]:
    findings = []
    handle = desk.runtime._lock_handle
    if handle is None or handle.closed:
        findings.append("desk_owner_lock_not_held")
    if not fence.held or fence.epoch != desk.owner_epoch or fence.alias != alias:
        findings.append("account_ownership_not_held")
    try:
        owner = json.loads(desk.owner_path().read_bytes())
        if (
            owner.get("owner_epoch") != desk.owner_epoch
            or owner.get("pid") != os.getpid()
            or owner.get("client_id") != desk.ib.client_id
        ):
            findings.append("desk_owner_identity_mismatch")
    except (OSError, ValueError, AttributeError):
        findings.append("desk_owner_identity_unavailable")
    if desk.ib is not desk.runtime.ib or desk.ib is not desk.broker.ib:
        findings.append("desk_session_identity_mismatch")
    return findings


def _money(value: Any) -> str:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise SupervisedRefused("account_money_invalid") from None
    if not amount.is_finite():
        raise SupervisedRefused("account_money_invalid")
    return str(amount)


def _run_completed(wire: Any, future: Any) -> Any:
    result = wire.run(future, timeout=READ_TIMEOUT_SECONDS)
    if not future.done() or future.cancelled():
        raise SupervisedRefused("request_completion_unproven")
    return result


def _fresh_balances(wire: Any, account: str) -> tuple[str, list[dict[str, Any]]]:
    # The generated helper caches accountSummary and leaves a subscription.
    # Capture callback rows from a unique temporary read request instead.
    rows: list[tuple[str, str, str, str]] = []
    request_id = wire.client.getReqId()
    prior_callback = wire.wrapper.accountSummary
    had_override = "accountSummary" in vars(wire.wrapper)

    def observed(req_id: int, row_account: str, tag: str, value: str, currency: str) -> None:
        # The SDK public event discards request ID; intercept this exact
        # callback while always delegating to preserve the owner's caches.
        prior_callback(req_id, row_account, tag, value, currency)
        if req_id == request_id:
            rows.append((row_account, tag, value, currency))

    future = wire.wrapper.startReq(request_id)
    wire.wrapper.accountSummary = observed
    try:
        wire.client.reqAccountSummary(
            request_id, "All", "NetLiquidation,TotalCashValue,BuyingPower"
        )
        _run_completed(wire, future)
    finally:
        if had_override:
            wire.wrapper.accountSummary = prior_callback
        else:
            del wire.wrapper.accountSummary
        # Cancel only this newly-created read subscription, never the owner's.
        wire.client.cancelAccountSummary(request_id)
    required = {"NetLiquidation", "TotalCashValue", "BuyingPower"}
    found: dict[str, str] = {}
    for row_account, tag, raw_value, currency in rows:
        if row_account != account or tag not in required:
            continue
        if currency != "USD":
            raise SupervisedRefused("account_balance_currency_unqualified")
        value = _money(raw_value)
        if tag in found and Decimal(found[tag]) != Decimal(value):
            raise SupervisedRefused("account_balance_contradictory")
        found[tag] = value
    if set(found) != required:
        raise SupervisedRefused("account_balance_missing")
    return str(request_id), [
        {"tag": tag, "value": found[tag], "currency": "USD"} for tag in sorted(found)
    ]


def _positions(rows: Any, account: str) -> list[dict[str, Any]]:
    if not isinstance(rows, list):
        raise SupervisedRefused("positions_shape_invalid")
    result = []
    seen = set()
    for row in rows:
        if not isinstance(row.account, str) or not row.account:
            raise SupervisedRefused("provider_row_account_unavailable")
        if row.account != account:
            continue
        contract = row.contract
        if type(contract.conId) is not int or contract.conId <= 0 or not contract.secType:
            raise SupervisedRefused("position_contract_invalid")
        if contract.conId in seen:
            raise SupervisedRefused("position_identity_ambiguous")
        seen.add(contract.conId)
        result.append(
            {
                "con_id": contract.conId,
                "sec_type": contract.secType,
                "symbol": contract.symbol,
                "quantity": _money(row.position),
                "average_cost": _money(row.avgCost),
            }
        )
    return sorted(result, key=lambda row: row["con_id"])


def _orders(rows: Any, account: str) -> list[dict[str, Any]]:
    if not isinstance(rows, list):
        raise SupervisedRefused("orders_shape_invalid")
    result = []
    seen = set()
    for trade in rows:
        order, status = trade.order, trade.orderStatus
        if not isinstance(order.account, str) or not order.account:
            raise SupervisedRefused("provider_row_account_unavailable")
        if order.account != account:
            continue
        identity = (order.clientId, order.orderId)
        if any(type(value) is not int or value < 0 for value in identity) or identity in seen:
            raise SupervisedRefused("order_identity_ambiguous")
        seen.add(identity)
        if status.status not in WORKING_STATES | TERMINAL_STATES:
            raise SupervisedRefused("order_state_ambiguous")
        total, filled, remaining = (
            _money(order.totalQuantity),
            _money(status.filled),
            _money(status.remaining),
        )
        if min(Decimal(total), Decimal(filled), Decimal(remaining)) < 0 or Decimal(
            filled
        ) + Decimal(remaining) != Decimal(total):
            raise SupervisedRefused("order_quantity_contradictory")
        result.append(
            {
                "client_id": identity[0],
                "order_id": identity[1],
                "status": status.status,
                "quantity": total,
                "filled": filled,
                "remaining": remaining,
                "order_ref": order.orderRef,
            }
        )
    return sorted(result, key=lambda row: (row["client_id"], row["order_id"]))


def persist_receipt(output: Path, receipt: dict[str, Any]) -> None:
    content = canonical_bytes(receipt)
    identity = hashlib.sha256(content).hexdigest()
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (output / ".qualification.lock").open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        historical = output / "qualification" / "ibkr" / (identity + ".json")
        if historical.exists():
            if historical.read_bytes() != content:
                raise SupervisedRefused("qualification_receipt_identity_collision")
        else:
            _atomic_write(historical, receipt)
        _atomic_write(output / "read-only-qualification.json", receipt)


def qualify_read_only(
    desk: SupervisedDesk,
    fence: AccountOwnership,
    *,
    account_id: str,
    account_alias: str,
    output: Path,
) -> dict[str, Any]:
    """Call only on the owner's serialized event-loop thread; never reconnect.

    The caller supplies its already-held alias fence. This function neither
    acquires/releases ownership nor changes desk/mandate/order state.
    """
    now = _aware(desk.clock())
    if not account_alias or account_alias.casefold() == account_id.casefold():
        raise SupervisedRefused("qualification_account_alias_requires_private_label")
    output_root = output.resolve()
    for owner_root in (
        desk.paths.root.resolve(),
        desk.supervised.root.resolve(),
        fence.root.resolve(),
    ):
        if output_root.is_relative_to(owner_root) or owner_root.is_relative_to(output_root):
            raise SupervisedRefused("qualification_output_overlaps_owner_state")
    receipt: dict[str, Any] = {
        "schema": SCHEMA,
        "provider": "ibkr",
        "account_alias": account_alias,
        "provider_account_sha256": hashlib.sha256(account_id.encode()).hexdigest(),
        "state_root": str(output.resolve()),
        "owner_state_root": str(desk.paths.root.resolve()),
        "owner_epoch": desk.owner_epoch,
        "assessed_at": now.isoformat(),
        "read_started_at": now.isoformat(),
        "expires_at": shift_instant(now, TTL_SECONDS).isoformat(),
        "verdict": "BLOCKED",
        "environment": "BROKER PAPER",
        "paper_verified": False,
        "ownership_verified_at_assessment": False,
        "orders_authorized": False,
        "equity_execution_ready": False,
        "live_money": False,
        "exact_economics": False,
        "broker_event_time_available": False,
        "observations": [],
        "findings": [],
        "recent_order_scope": "completed orders available in the current gateway session only",
        "limitations": [
            "local completion times are not broker event times",
            "open-order readback is not exact fill economics",
            "options runtime does not implement equity execution",
        ],
    }
    try:
        findings = _owner_findings(desk, fence, account_alias)
        findings.extend(desk.broker.paper_blockers(account_id))
        if set(desk.ib._ib.managedAccounts()) != {account_id}:
            findings.append("qualification_account_alias_ambiguous")
        if findings:
            raise SupervisedRefused(";".join(findings))
        receipt["ownership_verified_at_assessment"] = True
        receipt["paper_verified"] = True
        wire = desk.ib._ib
        if any(
            key in wire.wrapper._futures for key in ("positions", "openOrders", "completedOrders")
        ):
            raise SupervisedRefused("owner_read_request_in_progress")
        prior = {
            (trade.order.clientId, trade.order.orderId)
            for trade in wire.openTrades()
            if trade.order.account == account_id and trade.orderStatus.status in WORKING_STATES
        }
        request_id, balances = _fresh_balances(wire, account_id)
        captured = _aware(desk.clock())
        receipt["observations"].append(
            {
                "operation": "balances",
                "captured_at": captured.isoformat(),
                "request_id": request_id,
                "broker_event_at": None,
                "digest": hashlib.sha256(canonical_bytes(balances)).hexdigest(),
                "row_count": len(balances),
            }
        )
        positions = _positions(_run_completed(wire, wire.reqPositionsAsync()), account_id)
        captured = _aware(desk.clock())
        receipt["observations"].append(
            {
                "operation": "positions",
                "captured_at": captured.isoformat(),
                "request_id": "positions",
                "broker_event_at": None,
                "digest": hashlib.sha256(canonical_bytes(positions)).hexdigest(),
                "row_count": len(positions),
            }
        )
        orders = _orders(_run_completed(wire, wire.reqAllOpenOrdersAsync()), account_id)
        captured = _aware(desk.clock())
        receipt["observations"].append(
            {
                "operation": "orders",
                "captured_at": captured.isoformat(),
                "request_id": "openOrders",
                "broker_event_at": None,
                "digest": hashlib.sha256(canonical_bytes(orders)).hexdigest(),
                "row_count": len(orders),
            }
        )
        completed = _orders(
            _run_completed(wire, wire.reqCompletedOrdersAsync(apiOnly=False)), account_id
        )
        captured = _aware(desk.clock())
        receipt["observations"].append(
            {
                "operation": "completed_orders",
                "captured_at": captured.isoformat(),
                "request_id": "completedOrders",
                "broker_event_at": None,
                "digest": hashlib.sha256(canonical_bytes(completed)).hexdigest(),
                "row_count": len(completed),
            }
        )
        if prior - {(row["client_id"], row["order_id"]) for row in orders}:
            raise SupervisedRefused("previously_known_order_absent")
        now = _aware(desk.clock())
        if any(
            not 0
            <= (now - datetime.fromisoformat(row["captured_at"])).total_seconds()
            < TTL_SECONDS
            for row in receipt["observations"]
        ):
            raise SupervisedRefused("observation_stale_or_future")
        findings = _owner_findings(desk, fence, account_alias) + desk.broker.paper_blockers(
            account_id
        )
        if findings:
            raise SupervisedRefused(";".join(findings))
        receipt["expires_at"] = shift_instant(
            min(datetime.fromisoformat(row["captured_at"]) for row in receipt["observations"]),
            TTL_SECONDS,
        ).isoformat()
        receipt["assessed_at"] = now.isoformat()
        receipt["verdict"] = "QUALIFIED"
    except SupervisedRefused as error:
        receipt["findings"] = error.reason.split(";")
    except Exception:
        # Never disclose raw IB errors, account numbers or credential-bearing data.
        receipt["findings"] = ["owner_observation_unavailable"]
    assessed = _aware(desk.clock())
    receipt["assessed_at"] = assessed.isoformat()
    if assessed < datetime.fromisoformat(receipt["read_started_at"]):
        receipt["verdict"] = "BLOCKED"
        receipt["findings"].append("qualification_clock_reversed")
    persist_receipt(output, receipt)
    return receipt
