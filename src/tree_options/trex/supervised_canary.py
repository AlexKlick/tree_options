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
  orders (the monitor's included; supervised-tagged orders excluded).
  OPERATOR RULING 2026-09-28 (3b): only those on the canary's OWN
  underlying count as a non-flat book; the whole account stays in the
  evidence, and other books' risk still enters through the open-loss cap;
- temporary assignment exposure = width x 100 x quantity (OPERATOR RULING
  2026-09-28, 1a): the gap loss if the short leg is assigned before the
  long one is exercised, never the short leg's notional;
- margin: OUR modeled margin (debit kinds = the debit at the cap; credit
  verticals = width x 100 x quantity). The paper gateway answers what-if
  with all-zero margins (probe 2026-09-25), so IBKR's number is vacuous
  on paper and is only recorded as evidence.

What this module must NOT infer is an explicit ``OperatorCanaryInputs``:
the owner epoch and health, whether the assignment plan and a protective
exit exist (OPERATOR RULING 2026-09-28, 2b: the E5 desk runtime owns the
exits of supervised positions), and the reconciled open and day loss.
Each is a runtime fact the runtime supplies.

Any broker view that cannot be read yields ``facts=None`` and a
``broker_view_unreadable:<view>`` blocker: nothing is defaulted.

Wave 2 entry screening is kind-aware (``collect_entry_screening``): an
option effect gets the canary screen above, unchanged, plus the shared
per-session entry cap and the product's debit-only rule; an equity effect
gets ``action_graph.canary.review_equity_entry`` over SUPPLIED facts (the
quote the stock wire read, the books' risk snapshot), screened by
``collect_equity_screening`` with its own evidence digest.
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
    EquityEntryFacts,
    entry_session_blockers,
    package_intent_sha256,
    review_canary_package,
    review_equity_entry,
)
from tree_options.action_graph.capital import CapitalProfile
from tree_options.action_graph.proposal import canonical_bytes
from tree_options.trex.contracts.defaults import (
    EQUITY_GAP_RESERVE_BPS,
    EQUITY_SYMBOL_ALLOWLIST,
    MAX_ENTRIES_PER_SESSION,
)
from tree_options.trex.contracts.effects import (
    SupervisedEquityEffect,
    equity_max_loss_cents,
    equity_notional_cents,
)
from tree_options.trex.contracts.proposals import Quote
from tree_options.trex.contracts.risk import RiskSnapshot, risk_snapshot_blockers
from tree_options.trex.supervised_ibkr import (
    SUPERVISED_REF_PREFIX,
    IbkrSupervisedBroker,
    SupervisedEffect,
)

SCREENING_SCHEMA = "supervised-canary-screening/2"
EQUITY_SCREENING_SCHEMA = "supervised-equity-screening/1"
#: the product opens debit packages only (credit spreads are out of scope)
CREDIT_OPEN_BLOCKER = "credit_open_not_supported_v1"
RULINGS = {
    "assignment_exposure": "2026-09-28 1a: width x 100 x quantity",
    "flat_book_scope": "2026-09-28 3b: the canary's own underlying only",
    "protective_exit_owner": "2026-09-28 2b: the E5 desk runtime",
}
_VERTICALS = ("debit_vertical", "credit_vertical")


@dataclass(frozen=True)
class OperatorCanaryInputs:
    """Facts this code must not infer; each is supplied, never defaulted."""

    owner_epoch: str
    owner_healthy: bool
    assignment_plan_verified: bool
    protective_exit_ready: bool
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


def assignment_exposure(effect: SupervisedEffect) -> Decimal:
    """Ruling 1a: the gap loss if the short leg is assigned early."""
    width = effect.structure.width
    if width is None:
        raise ValueError(f"{effect.structure.kind}: no width, exposure undefined")
    return width * 100 * effect.quantity


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
    broker: IbkrSupervisedBroker,
    effect: SupervisedEffect,
    *,
    profile: CapitalProfile,
    mandate_account_id: str,
    inputs: OperatorCanaryInputs,
    clock: Callable[[], datetime] | None = None,
) -> CanaryScreening:
    """Observe, then screen. Never places, cancels or modifies an order.

    ``checked_at`` is read from ``clock`` AFTER every observation, so an
    observation's own timestamp can never postdate the check.
    """
    clock = clock or (lambda: datetime.now(UTC))
    structure = effect.structure
    evidence: dict[str, Any] = {
        "schema": SCREENING_SCHEMA,
        "intent_id": effect.intent_id,
        "structure_id": structure.id,
    }
    blockers: list[str] = list(broker.paper_blockers(effect.account_id))
    evidence["session_blockers"] = list(blockers)

    ib = broker.ib
    try:
        snapshot = ib.account_snapshot()
    except Exception as error:
        return _unreadable("account_snapshot", error, blockers, evidence)
    try:
        held = [p for p in ib.positions(effect.account_id) if p.qty != 0]
    except Exception as error:
        return _unreadable("positions", error, blockers, evidence)
    try:
        foreign = [
            t
            for t in ib._ib.reqAllOpenOrders()
            if getattr(t.contract, "secType", "") in ("BAG", "OPT")
            and not str(getattr(t.order, "orderRef", "") or "").startswith(SUPERVISED_REF_PREFIX)
        ]
    except Exception as error:
        return _unreadable("all_client_open_orders", error, blockers, evidence)
    # Ruling 3b: the flat-book rule covers the canary's own underlying only.
    positions = [p for p in held if p.symbol == structure.underlying]
    working = [
        t for t in foreign if str(getattr(t.contract, "symbol", "") or "") == structure.underlying
    ]

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
    evidence.update(
        {
            "checked_at": now.isoformat(),
            "observed_account_id": observed_account,
            "account_observed_at": account_at.isoformat(),
            "rulings": RULINGS,
            "account_positions": sorted(
                f"{p.symbol}:{p.sec_type}:{p.con_id}:{p.qty}" for p in held
            ),
            "positions": sorted(f"{p.symbol}:{p.sec_type}:{p.con_id}:{p.qty}" for p in positions),
            "account_working_orders": sorted(
                str(getattr(t.order, "orderId", "?")) for t in foreign
            ),
            "working_orders": sorted(str(getattr(t.order, "orderId", "?")) for t in working),
            "package_quote": None if quote is None else [str(quote.bid), str(quote.ask)],
            "quote_observed_at": None if quote_at is None else quote_at.isoformat(),
            "modeled_margin": str(modeled_margin(effect)),
            "assignment_exposure": str(assignment_exposure(effect)),
        }
    )

    facts = CanaryFacts(
        intent_sha256=package_intent_sha256(
            profile, structure, mandate_account_id, inputs.owner_epoch
        ),
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
        temporary_assignment_exposure=assignment_exposure(effect),
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


def _unreadable(
    view: str, error: BaseException, blockers: list[str], evidence: dict[str, Any]
) -> CanaryScreening:
    evidence[f"{view}_error"] = repr(error)
    unique = tuple(dict.fromkeys([*blockers, f"broker_view_unreadable:{view}"]))
    evidence["blockers"] = list(unique)
    return CanaryScreening(blockers=unique, facts=None, evidence=evidence)


# ------------------------------------------------- Wave 2: equity screening


@dataclass(frozen=True)
class EquityScreeningInputs:
    """Facts an equity screening is given, never fetched or defaulted here.

    ``quote``: the NBBO the stock wire read, with the market data type IBKR
    reported; ``risk``: the books' latest risk snapshot (settled cash and the
    reconciled open and day loss); the owner health and exit readiness are
    runtime facts as for options.
    """

    quote: Quote | None
    risk: RiskSnapshot | None
    owner_healthy: bool
    protective_exit_ready: bool


@dataclass(frozen=True)
class EquityScreening:
    """Blockers for one equity effect, the facts they came from, and their digest."""

    blockers: tuple[str, ...]
    facts: EquityEntryFacts
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def clear(self) -> bool:
        return not self.blockers

    @property
    def screening_sha256(self) -> str:
        return hashlib.sha256(canonical_bytes(self.evidence)).hexdigest()


def _now(clock: Callable[[], datetime] | None) -> datetime:
    now = (clock or (lambda: datetime.now(UTC)))()
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("clock must return timezone-aware datetimes")
    return now.astimezone(UTC)


def collect_equity_screening(
    effect: SupervisedEquityEffect,
    *,
    profile: CapitalProfile,
    mandate_account_id: str,
    inputs: EquityScreeningInputs,
    entries_this_session: int | None,
    clock: Callable[[], datetime] | None = None,
) -> EquityScreening:
    """Screen one equity entry over supplied facts (pure apart from ``clock``).

    A risk snapshot that is absent, unavailable, stale or for another
    account gives no cash or loss evidence: its blocker is listed first and
    the cash and loss checks then fail closed as unknown.
    """
    now = _now(clock)
    risk_blockers: list[str] = [b.value for b in risk_snapshot_blockers(inputs.risk, now)]
    risk = inputs.risk
    if risk is not None and not risk_blockers and risk.account_id != effect.account_id:
        risk_blockers.append("risk_snapshot_account_mismatch")
    usable = risk if not risk_blockers else None
    quote = inputs.quote
    facts = EquityEntryFacts(
        mandate_account_id=mandate_account_id,
        checked_at=now,
        market_data_type=None if quote is None else quote.market_data_type,
        quote_observed_at=None if quote is None else quote.as_of,
        settled_cash_cents=None if usable is None else usable.cash_cents,
        open_loss_cents=None if usable is None else usable.open_loss_cents,
        day_realized_loss_cents=None if usable is None else usable.day_realized_loss_cents,
        entries_this_session=entries_this_session,
        owner_healthy=inputs.owner_healthy,
        protective_exit_ready=inputs.protective_exit_ready,
    )
    blockers = tuple(dict.fromkeys([*risk_blockers, *review_equity_entry(profile, effect, facts)]))
    evidence: dict[str, Any] = {
        "schema": EQUITY_SCREENING_SCHEMA,
        "intent_id": effect.intent_id,
        "account_id": effect.account_id,
        "mandate_account_id": mandate_account_id,
        "symbol": effect.symbol,
        "con_id": effect.con_id,
        "quantity": effect.quantity,
        "limit_cents": effect.limit_cents,
        "stop_price_cents": effect.exit_policy.stop_price_cents,
        "checked_at": now.isoformat(),
        "quote": None if quote is None else quote.model_dump(mode="json"),
        "risk_snapshot": None if risk is None else risk.model_dump(mode="json", by_alias=True),
        "notional_cents": equity_notional_cents(effect),
        "max_loss_cents": equity_max_loss_cents(effect),
        "gap_reserve_bps": EQUITY_GAP_RESERVE_BPS,
        "symbol_allowlist": sorted(EQUITY_SYMBOL_ALLOWLIST),
        "entries_this_session": entries_this_session,
        "max_entries_per_session": MAX_ENTRIES_PER_SESSION,
        "owner_healthy": inputs.owner_healthy,
        "protective_exit_ready": inputs.protective_exit_ready,
        "profile": {"id": profile.profile_id, "revision": profile.revision},
        "blockers": list(blockers),
    }
    return EquityScreening(blockers=blockers, facts=facts, evidence=evidence)


def collect_entry_screening(
    effect: SupervisedEffect | SupervisedEquityEffect,
    *,
    profile: CapitalProfile,
    mandate_account_id: str,
    entries_this_session: int | None,
    broker: IbkrSupervisedBroker | None = None,
    option_inputs: OperatorCanaryInputs | None = None,
    equity_inputs: EquityScreeningInputs | None = None,
    clock: Callable[[], datetime] | None = None,
) -> CanaryScreening | EquityScreening:
    """Kind-aware entry screening for the product path.

    Options: the canary screen (``collect_canary_screening``) unchanged, plus
    the debit-only rule and the shared session cap. Equities:
    ``collect_equity_screening``. Missing inputs for the effect's kind are a
    caller defect (``ValueError``), never a silent pass.
    """
    if isinstance(effect, SupervisedEquityEffect):
        if equity_inputs is None:
            raise ValueError("an equity entry needs equity_inputs")
        return collect_equity_screening(
            effect,
            profile=profile,
            mandate_account_id=mandate_account_id,
            inputs=equity_inputs,
            entries_this_session=entries_this_session,
            clock=clock,
        )
    if broker is None or option_inputs is None:
        raise ValueError("an option entry needs the broker session and option_inputs")
    base = collect_canary_screening(
        broker,
        effect,
        profile=profile,
        mandate_account_id=mandate_account_id,
        inputs=option_inputs,
        clock=clock,
    )
    extra: list[str] = []
    if effect.structure.is_credit:
        extra.append(CREDIT_OPEN_BLOCKER)
    extra.extend(entry_session_blockers(entries_this_session))
    blockers = tuple(dict.fromkeys([*base.blockers, *extra]))
    evidence = {
        **base.evidence,
        "entries_this_session": entries_this_session,
        "max_entries_per_session": MAX_ENTRIES_PER_SESSION,
        "blockers": list(blockers),
    }
    return CanaryScreening(blockers=blockers, facts=base.facts, evidence=evidence)
