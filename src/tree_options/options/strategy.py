"""Candidate→order strategy for the options era (M3 plan §3.D).

The signal is an UNDERLYING-level score over the option-eligible
cross-section (the file(t-1) eligible set of the overlay). Top-quintile
names buy CALLS, bottom-quintile names buy PUTS — the owner's ruling 1.
Contract selection is a pre-declared, data-light rule:

- expiry: the live expiry whose calendar DTE (decision → expiration) is
  inside [dte_min, dte_max] and nearest `target_dte`;
- strike: the ladder strike whose file(t-1) |delta| is nearest
  `target_abs_delta` inside [abs_delta_min, abs_delta_max] (single-entry
  lazy reads; analytic |delta| is monotone in strike for a fixed expiry).

The chosen contract then passes the FULL §9.2 candidate filter through
the PIT surface (`surface.candidate_snapshot`); acceptance requires zero
FAIL and zero NOT_EVALUABLE. Sizing is premium-budgeted whole contracts:
per-candidate budget = premium_budget_fraction * cash / n_candidates,
decremental affordability against the file(t-1) EOD ask INCLUDING fees,
capped at max_contracts_per_candidate.

Exits are time-based: 4 sessions after entry EXECUTION (decision at the
exit session's close — no data needed). Corporate-action passes per plan
§2: pending-action entry exclusion at decision, execution-time
cancellation, mid-hold force-close on ratio actions, terminal
delisting/merger settles at intrinsic (the backtest mints that
settlement; `classify_action` is the shared classifier).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_FLOOR, Decimal
from typing import Literal

from tree_options.candidates.filters import CandidateFilter
from tree_options.data.actions import CorporateActionRecord
from tree_options.data.options_pit import OptionPitSurface
from tree_options.ledger.fees import FeeModel
from tree_options.schemas.options import OptionContract
from tree_options.schemas.trading import Order
from tree_options.synth_options import contract_id_of
from tree_options.time.calendar import SessionCalendar

RATIO_ACTION_KINDS = frozenset({"split", "reverse_split", "stock_dividend"})
TERMINAL_ACTION_KINDS = frozenset({"delisting", "merger"})

ActionClass = Literal["ratio", "terminal", "cash_dividend", "inert"]


def classify_action(kind: str) -> ActionClass:
    """The shared corporate-action classifier (plan §2 corporate actions).

    ratio     → mid-hold force-close at the next 10:00 ET window against the
                last pre-action file (adjusted contracts never exist).
    terminal  → the position settles at intrinsic vs the final bar's close
                at its publication instant (settlement kind "terminal").
    cash_dividend → no forced trade: entry exclusion when pending at
                decision; otherwise the early-exercise election input.
    inert     → nothing (symbol changes etc. never touch deliverables here).
    """
    if kind in RATIO_ACTION_KINDS:
        return "ratio"
    if kind in TERMINAL_ACTION_KINDS:
        return "terminal"
    if kind == "cash_dividend":
        return "cash_dividend"
    return "inert"


@dataclass(frozen=True)
class OptionSignal:
    """One underlying-level scored row at one decision session."""

    decision_session: date
    security_id: str
    score: float
    label: float | None = None


@dataclass(frozen=True)
class OptionsStrategyConfig:
    """Frozen strategy parameters — every knob enters the config hash."""

    target_abs_delta: Decimal = Decimal("0.45")
    abs_delta_min: Decimal = Decimal("0.30")
    abs_delta_max: Decimal = Decimal("0.60")
    target_dte: int = 45
    dte_min: int = 30
    dte_max: int = 60
    premium_budget_fraction: Decimal = Decimal("0.10")
    max_contracts_per_candidate: int = 10
    exit_sessions_after_entry: int = 4
    allow_cancellation: bool = True
    allow_force_close: bool = True

    def __post_init__(self) -> None:
        if not (Decimal(0) < self.premium_budget_fraction <= Decimal(1)):
            raise ValueError("premium_budget_fraction must be in (0, 1]")
        if self.max_contracts_per_candidate < 1:
            raise ValueError("max_contracts_per_candidate must be >= 1")
        if self.exit_sessions_after_entry < 1:
            raise ValueError("exit_sessions_after_entry must be >= 1")
        if not (
            Decimal(0)
            < self.abs_delta_min
            <= self.target_abs_delta
            <= self.abs_delta_max
            < Decimal(1)
        ):
            raise ValueError("abs delta target must lie inside the band")
        if not (0 < self.dte_min <= self.target_dte <= self.dte_max):
            raise ValueError("dte target must lie inside the band")


@dataclass(frozen=True)
class OptionCandidate:
    """An accepted §9.2 candidate with its selection provenance."""

    signal: OptionSignal
    contract: OptionContract
    expiration: date
    strike: Decimal
    call_put: str
    abs_delta: Decimal
    ask: Decimal  # file(t-1) EOD ask — the sizing premium estimate
    bid: Decimal
    mid: Decimal

    @property
    def contract_id(self) -> str:
        return self.contract.contract_id

    @property
    def underlying_security_id(self) -> str:
        return self.contract.underlying_security_id


def _quintile_cut(
    rows: tuple[OptionSignal, ...],
) -> tuple[tuple[OptionSignal, ...], tuple[OptionSignal, ...]]:
    """Top and bottom quintiles over the scored cross-section.

    count = ceil(n / 5), mirroring the equity seam's top-quintile rule;
    ties break on security_id so the cut is deterministic.
    """
    if len(rows) < 2:
        return tuple(rows), ()
    count = max(1, math.ceil(len(rows) / 5))
    ranked = sorted(rows, key=lambda row: (-row.score, row.security_id))
    return tuple(ranked[:count]), tuple(ranked[-count:])


def _pick_expiry(
    surface: OptionPitSurface,
    underlying_id: str,
    decision_session: date,
    config: OptionsStrategyConfig,
) -> date | None:
    decision_at = surface.overlay.calendar.session_close(decision_session)
    live = surface.live_expiries_as_of(underlying_id, decision_at)
    in_band = [
        expiration
        for expiration in live
        if config.dte_min <= (expiration - decision_session).days <= config.dte_max
    ]
    if not in_band:
        return None
    return min(in_band, key=lambda e: (abs((e - decision_session).days - config.target_dte), e))


def _pick_strike(
    surface: OptionPitSurface,
    underlying_id: str,
    decision_session: date,
    expiration: date,
    call_put: str,
    config: OptionsStrategyConfig,
) -> tuple[Decimal, Decimal] | None:
    """Ladder strike whose file(t-1) |delta| is nearest the target inside
    the band. SINGLE-ENTRY lazy reads only (no day-file materialization);
    monotonicity is asserted, not assumed — a non-monotone |delta| ladder
    would make "nearest to target" ill-posed. Returns (strike, abs_delta)
    or None when no in-band strike quotes on the visible file."""
    decision_at = surface.overlay.calendar.session_close(decision_session)
    ladder = surface.strike_ladder(underlying_id, expiration)
    probed: list[tuple[Decimal, Decimal]] = []
    previous: Decimal | None = None
    for strike in ladder:
        entry = surface.entry_as_of(
            underlying_id, decision_at, contract_id_of(underlying_id, expiration, call_put, strike)
        )
        if entry is None:
            continue  # wing node that did not quote that session
        delta = entry.abs_delta
        if previous is not None and call_put == "C" and delta > previous:
            raise ValueError(f"non-monotone call |delta| ladder at {underlying_id}/{expiration}")
        if previous is not None and call_put == "P" and delta < previous:
            raise ValueError(f"non-monotone put |delta| ladder at {underlying_id}/{expiration}")
        previous = delta
        probed.append((strike, delta))
    in_band = [(s, d) for s, d in probed if config.abs_delta_min <= d <= config.abs_delta_max]
    if not in_band:
        return None
    return min(in_band, key=lambda pair: (abs(pair[1] - config.target_abs_delta), pair[0]))


def _pending_action_at(
    actions: tuple[CorporateActionRecord, ...],
    underlying_id: str,
    decision_at,
) -> CorporateActionRecord | None:
    """Any still-pending action on the underlying visible at the decision
    instant (announced by decision, effective after the decision session).
    Plan §2 (i): such names are excluded from entry — their post-action
    chains are unknowable at decision."""
    for action in actions:
        if action.security_id != underlying_id:
            continue
        if action.available_at <= decision_at and action.effective_session > decision_at.date():
            return action
    return None


@dataclass
class CandidateAudit:
    """Mutable drop-reason counters (the backtest payload's NOT_EVALUABLE /
    selection audit — OD3's raw material). One instance may span sessions."""

    scored_cross_section: int = 0
    selected: int = 0
    excluded_pending_action: int = 0
    no_in_band_expiry: int = 0
    no_in_band_strike: int = 0
    filter_not_evaluable: int = 0
    filter_fail: int = 0
    no_visible_quote: int = 0
    accepted: int = 0
    rule_histogram: dict[tuple[str, str], int] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.rule_histogram is None:
            self.rule_histogram = {}

    def tally(self, rule: str, status: str) -> None:
        key = (rule, status)
        self.rule_histogram[key] = self.rule_histogram.get(key, 0) + 1


def build_candidates(
    *,
    surface: OptionPitSurface,
    candidate_filter: CandidateFilter,
    decision_session: date,
    scores: tuple[OptionSignal, ...],
    config: OptionsStrategyConfig,
    actions: tuple[CorporateActionRecord, ...] = (),
    audit: CandidateAudit | None = None,
) -> tuple[OptionCandidate, ...]:
    """Cut quintiles over the OPTION-ELIGIBLE scored cross-section and
    resolve each selected name to one §9.2-accepted contract.

    Top quintile → calls, bottom quintile → puts (owner ruling 1). Names
    with a pending corporate action at decision are excluded (§2 i). A
    name whose expiry/strike rule finds no in-band contract, or whose
    §9.2 evaluation rejects, is simply absent from the returned tuple;
    pass `audit` to capture the drop reasons and the per-rule histogram.
    """
    decision_at = surface.overlay.calendar.session_close(decision_session)
    eligible = frozenset(surface.eligible_as_of(decision_session))
    cross_section = tuple(
        row
        for row in scores
        if row.decision_session == decision_session
        and row.security_id in eligible
        and not math.isnan(row.score)
        and math.isfinite(row.score)
    )
    if audit is not None:
        audit.scored_cross_section += len(cross_section)
    top, bottom = _quintile_cut(cross_section)
    selected: list[tuple[OptionSignal, str]] = [
        *((row, "C") for row in top),
        *((row, "P") for row in bottom),
    ]
    if audit is not None:
        audit.selected += len(selected)

    candidates: list[OptionCandidate] = []
    for signal, call_put in selected:
        underlying_id = signal.security_id
        if _pending_action_at(actions, underlying_id, decision_at) is not None:
            if audit is not None:
                audit.excluded_pending_action += 1
            continue
        expiration = _pick_expiry(surface, underlying_id, decision_session, config)
        if expiration is None:
            if audit is not None:
                audit.no_in_band_expiry += 1
            continue
        picked = _pick_strike(
            surface, underlying_id, decision_session, expiration, call_put, config
        )
        if picked is None:
            if audit is not None:
                audit.no_in_band_strike += 1
            continue
        strike, abs_delta = picked
        contract = surface.contract(contract_id_of(underlying_id, expiration, call_put, strike))
        snapshot = surface.candidate_snapshot(contract, decision_session)
        decision = candidate_filter.evaluate(snapshot)
        if audit is not None:
            for result in decision.results:
                audit.tally(result.rule, result.status)
        if not decision.accepted:
            if audit is not None:
                if any(r.status == "NOT_EVALUABLE" for r in decision.failed()):
                    audit.filter_not_evaluable += 1
                else:
                    audit.filter_fail += 1
            continue
        entry = surface.entry_as_of(
            underlying_id,
            decision_at,
            contract.contract_id,
        )
        if entry is None:  # the filter already rejected this case (spread NOT_EVALUABLE)
            if audit is not None:
                audit.no_visible_quote += 1
            continue
        ask = entry.quote_eod.ask
        bid = entry.quote_eod.bid
        mid = (bid + ask) / 2
        if ask <= 0:
            continue
        candidates.append(
            OptionCandidate(
                signal=signal,
                contract=contract,
                expiration=expiration,
                strike=strike,
                call_put=call_put,
                abs_delta=abs_delta,
                ask=ask,
                bid=bid,
                mid=mid,
            )
        )
        if audit is not None:
            audit.accepted += 1
    return tuple(candidates)


def affordable_contracts(
    *,
    budget: Decimal,
    ask: Decimal,
    multiplier: int,
    fee_model: FeeModel,
    cap: int,
) -> int:
    """Whole-contract affordability INCLUDING fees: the largest n <= cap
    with ask * n * multiplier + fees(n) <= budget, found by decremental
    search from a floored estimate (the fee minimum makes the cost
    non-proportional, so a closed form can overshoot by one contract)."""
    if budget <= 0 or ask <= 0 or cap < 1:
        return 0
    per_contract = ask * multiplier
    estimate = min(
        cap,
        int((budget / (per_contract + Decimal("0.65"))).to_integral_value(rounding=ROUND_FLOOR)),
    )
    while estimate > 0 and per_contract * estimate + fee_model.order_fees(estimate) > budget:
        estimate -= 1
    return estimate


def plan_orders(
    *,
    calendar: SessionCalendar,
    candidates: tuple[OptionCandidate, ...],
    cash: Decimal,
    config: OptionsStrategyConfig,
    fee_model: FeeModel,
) -> tuple[Order, ...]:
    """buy/open_long orders, decision_at = close(d) of each candidate's
    decision session. Per-candidate budget = fraction * cash / n."""
    if not candidates:
        return ()
    decision_sessions = {c.signal.decision_session for c in candidates}
    if len(decision_sessions) != 1:
        raise ValueError(f"candidates span decision sessions: {sorted(decision_sessions)}")
    if not candidates:
        return ()
    session_budget = (config.premium_budget_fraction * cash).quantize(Decimal("0.01"))
    per_candidate = (session_budget / len(candidates)).quantize(Decimal("0.01"))
    orders: list[Order] = []
    for seq, candidate in enumerate(sorted(candidates, key=lambda c: c.contract_id), start=1):
        quantity = affordable_contracts(
            budget=per_candidate,
            ask=candidate.ask,
            multiplier=candidate.contract.multiplier,
            fee_model=fee_model,
            cap=config.max_contracts_per_candidate,
        )
        if quantity < 1:
            continue
        decision_session = candidate.signal.decision_session
        orders.append(
            Order(
                order_id=f"OPT-B{decision_session:%Y%m%d}-{seq:04d}",
                contract_id=candidate.contract_id,
                side="buy",
                intent="open_long",
                quantity=quantity,
                budget_notional=per_candidate,
                decision_at=calendar.session_close(decision_session),
                decision_session=decision_session,
            )
        )
    return tuple(orders)


def exit_decision_session(
    calendar: SessionCalendar, entry_execution_session: date, config: OptionsStrategyConfig
) -> date:
    """The exit's DECISION session: 4 sessions after entry execution means
    decision at e+3, execution at e+4 (premium struck at close(e+3) — the
    4-session hold spans exactly the H=5 window minus theta/spread)."""
    return calendar.nth_after(entry_execution_session, config.exit_sessions_after_entry - 1)


def plan_exit_order(
    *,
    calendar: SessionCalendar,
    contract_id: str,
    quantity: int,
    entry_execution_session: date,
    config: OptionsStrategyConfig,
) -> Order:
    """sell/close_long at the exit session's close — time-based, no data."""
    decision_session = exit_decision_session(calendar, entry_execution_session, config)
    return Order(
        order_id=f"OPT-S{decision_session:%Y%m%d}-{contract_id}",
        contract_id=contract_id,
        side="sell",
        intent="close_long",
        quantity=quantity,
        decision_at=calendar.session_close(decision_session),
        decision_session=decision_session,
    )


def cancellations_at_execution(
    *,
    orders: tuple[Order, ...],
    surface: OptionPitSurface,
    actions: tuple[CorporateActionRecord, ...],
    execution_at,
    contract_underlying: dict[str, str],
    config: OptionsStrategyConfig,
) -> tuple[Order, ...]:
    """Plan §2 (ii): orders whose underlying had an action ANNOUNCED in
    the (decision, execution] window are cancelled at execution time and
    counted — decided on information available BY execution only."""
    if not config.allow_cancellation:
        return ()
    cancelled: list[Order] = []
    for order in orders:
        underlying = contract_underlying.get(order.contract_id)
        if underlying is None:
            continue
        for action in actions:
            if action.security_id != underlying:
                continue
            if order.decision_at < action.available_at <= execution_at:
                cancelled.append(order)
                break
    return tuple(cancelled)


def pending_dividend_per_share(
    *,
    actions: tuple[CorporateActionRecord, ...],
    underlying_id: str,
    visible_by,
    effective_after: date,
    effective_through: date,
) -> Decimal | None:
    """The early-exercise election's dividend input: the cash dividend on
    the underlying visible by `visible_by` with effective session in
    (effective_after, effective_through]. None when no such dividend."""
    best: CorporateActionRecord | None = None
    for action in actions:
        if action.security_id != underlying_id or action.kind != "cash_dividend":
            continue
        if action.available_at > visible_by:
            continue
        if not (effective_after < action.effective_session <= effective_through):
            continue
        if best is None or action.effective_session < best.effective_session:
            best = action
    if best is None or best.cash_amount is None:
        return None
    return best.cash_amount
