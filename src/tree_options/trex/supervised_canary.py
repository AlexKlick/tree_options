"""Canary facts for a supervised permit, observed from the live paper session.

``action_graph.canary.review_canary_package`` is a pure screen over
``CanaryFacts``. This module fills those facts from ONE connected
``IbkrSupervisedBroker`` session and returns the full blocker list plus a
screening digest that ``supervised.issue_permit`` binds into the permit.

What is observed here (never assumed):

- the bound account's snapshot and its observation time;
- paper-gateway verification (the adapter's session guards);
- contract qualification and a two-sided package quote, with the quote's
  observation time = the OLDEST leg tick time (a leg without a tick time
  is not a fresh quote);
- the account's non-zero positions and ALL clients' working option/BAG
  orders (the monitor's included; supervised-tagged orders excluded);
- margin: OUR modeled margin (debit kinds = the debit at the cap; credit
  verticals = width x 100 x quantity). The paper gateway answers what-if
  with all-zero margins (probe 2026-09-25), so IBKR's number is vacuous
  on paper and is only recorded as evidence.

What this module must NOT infer is an explicit ``OperatorCanaryInputs``:
the owner epoch and health, whether the assignment plan and a protective
exit exist, the temporary assignment exposure, and the reconciled open and
day loss. Each is a ruling or a runtime fact the operator/runtime supplies.

Any broker view that cannot be read yields ``facts=None`` and a
``broker_view_unreadable:<view>`` blocker: nothing is defaulted.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from tree_options.action_graph.canary import (
    CanaryFacts,
    package_intent_sha256,
    review_canary_package,
)
from tree_options.action_graph.capital import CapitalProfile
from tree_options.action_graph.proposal import canonical_bytes
from tree_options.trex.supervised_ibkr import (
    SUPERVISED_REF_PREFIX,
    IbkrSupervisedBroker,
    SupervisedEffect,
)

SCREENING_SCHEMA = "supervised-canary-screening/1"
_VERTICALS = ("debit_vertical", "credit_vertical")


@dataclass(frozen=True)
class OperatorCanaryInputs:
    """Facts this code must not infer; each is supplied, never defaulted."""

    owner_epoch: str
    owner_healthy: bool
    assignment_plan_verified: bool
    protective_exit_ready: bool
    temporary_assignment_exposure: Decimal
    current_open_loss: Decimal | None
    realized_daily_loss: Decimal | None


@dataclass(frozen=True)
class CanaryScreening:
    """Blockers for one effect, the facts they came from, and their digest."""

    blockers: tuple[str, ...]
    facts: CanaryFacts | None
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def clear(self) -> bool:
        return not self.blockers

    @property
    def screening_sha256(self) -> str:
        return hashlib.sha256(canonical_bytes(self.evidence)).hexdigest()


def modeled_margin(effect: SupervisedEffect) -> Decimal:
    """Our margin model for the canary kinds (see module doc)."""
    structure = effect.structure
    if structure.is_credit:
        width = structure.width
        if width is None:
            raise ValueError(f"{structure.kind}: no width to margin")
        return width * 100 * effect.quantity
    return structure.limit * 100 * effect.quantity


def _quote_time(broker: IbkrSupervisedBroker, structure_id: str) -> datetime | None:
    """The oldest tick time across the package's legs; None if any is missing."""
    ib = broker.ib
    stamps: list[datetime] = []
    for con_id in ib.leg_con_ids(structure_id):
        sub = ib._md.get(con_id)
        stamp = getattr(getattr(sub, "ticker", None), "time", None)
        if not isinstance(stamp, datetime) or stamp.utcoffset() is None:
            return None
        stamps.append(stamp.astimezone(UTC))
    return min(stamps) if stamps else None


#: An observation time the canary's age rule always refuses (no observation).
_NEVER_OBSERVED = datetime.fromtimestamp(0, UTC)


def collect_canary_screening(
    broker: IbkrSupervisedBroker, effect: SupervisedEffect, *,
    profile: CapitalProfile, mandate_account_id: str,
    inputs: OperatorCanaryInputs, clock: Callable[[], datetime] | None = None,
) -> CanaryScreening:
    """Observe, then screen. Never places, cancels or modifies an order.

    ``checked_at`` is read from ``clock`` AFTER every observation, so an
    observation's own timestamp can never postdate the check.
    """
    clock = clock or (lambda: datetime.now(UTC))
    structure = effect.structure
    evidence: dict[str, Any] = {"schema": SCREENING_SCHEMA,
                                "intent_id": effect.intent_id, "structure_id": structure.id}
    blockers: list[str] = list(broker.paper_blockers(effect.account_id))
    evidence["session_blockers"] = list(blockers)

    ib = broker.ib
    try:
        snapshot = ib.account_snapshot()
    except Exception as error:
        return _unreadable("account_snapshot", error, blockers, evidence)
    try:
        positions = [p for p in ib.positions(effect.account_id) if p.qty != 0]
    except Exception as error:
        return _unreadable("positions", error, blockers, evidence)
    try:
        working = [t for t in ib._ib.reqAllOpenOrders()
                   if getattr(t.contract, "secType", "") in ("BAG", "OPT")
                   and not str(getattr(t.order, "orderRef", "") or "").startswith(
                       SUPERVISED_REF_PREFIX)]
    except Exception as error:
        return _unreadable("all_client_open_orders", error, blockers, evidence)

    contract_verified = True
    try:
        ib.prepare([structure])
        ib._order(structure, effect.side, effect.quantity, effect.limit)
    except (ValueError, RuntimeError) as error:
        contract_verified = False
        evidence["contract_error"] = repr(error)
    quote = ib.package_quote(structure.id) if contract_verified else None
    quote_at = _quote_time(broker, structure.id) if quote is not None else None

    observed_account = snapshot.account_id if snapshot is not None else "unobserved"
    account_at = snapshot.ts.astimezone(UTC) if snapshot is not None else _NEVER_OBSERVED
    now = clock()
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("clock must return timezone-aware datetimes")
    now = now.astimezone(UTC)
    evidence.update({
        "checked_at": now.isoformat(),
        "observed_account_id": observed_account,
        "account_observed_at": account_at.isoformat(),
        "positions": sorted(f"{p.symbol}:{p.sec_type}:{p.con_id}:{p.qty}" for p in positions),
        "working_orders": sorted(str(getattr(t.order, "orderId", "?")) for t in working),
        "package_quote": None if quote is None else [str(quote.bid), str(quote.ask)],
        "quote_observed_at": None if quote_at is None else quote_at.isoformat(),
        "modeled_margin": str(modeled_margin(effect)),
    })

    facts = CanaryFacts(
        intent_sha256=package_intent_sha256(profile, structure, mandate_account_id,
                                            inputs.owner_epoch),
        observed_account_id=observed_account,
        mandate_account_id=mandate_account_id,
        paper_gateway_verified=not evidence["session_blockers"],
        account_observed_at=account_at,
        quote_observed_at=quote_at or _NEVER_OBSERVED,
        checked_at=now,
        owner_epoch=inputs.owner_epoch,
        owner_healthy=inputs.owner_healthy,
        legacy_positions=len(positions),
        legacy_working_orders=len(working),
        package_quantity=effect.quantity,
        contract_verified=contract_verified,
        quote_complete=quote is not None,
        defined_risk_verified=structure.kind in _VERTICALS and structure.width is not None,
        assignment_plan_verified=inputs.assignment_plan_verified,
        protective_exit_ready=inputs.protective_exit_ready,
        worst_case_loss=structure.max_loss(),
        temporary_assignment_exposure=inputs.temporary_assignment_exposure,
        broker_margin_change=modeled_margin(effect),
        current_open_loss=inputs.current_open_loss,
        realized_daily_loss=inputs.realized_daily_loss,
    )
    blockers.extend(review_canary_package(profile, structure, facts))
    if effect.account_id != mandate_account_id:
        blockers.append("effect_account_not_mandate_account")
    unique = tuple(dict.fromkeys(blockers))
    evidence["blockers"] = list(unique)
    return CanaryScreening(blockers=unique, facts=facts, evidence=evidence)


def _unreadable(view: str, error: BaseException, blockers: list[str],
                evidence: dict[str, Any]) -> CanaryScreening:
    evidence[f"{view}_error"] = repr(error)
    unique = tuple(dict.fromkeys([*blockers, f"broker_view_unreadable:{view}"]))
    evidence["blockers"] = list(unique)
    return CanaryScreening(blockers=unique, facts=None, evidence=evidence)
