"""Options backtest for the M3 campaign (plan §3.E).

Long-only, validation-first; the two arms share all machinery and differ
ONLY in the holding rule:

- **arm A (momentum hold)** — the transfer arm: time-based exit 4 sessions
  after entry execution. Elections never run.
- **arm B (hold-with-exercise-policy)** — ride to expiry settlement or an
  early-exercise election; machinery oracles only, no signal criterion.

Per session t, strictly in this order (instants are exchange-local via the
calendar):

1. settlements whose publication instant has passed — every settlement
   strikes at close(s) and publishes with bar(s) (23:00 UTC), so it is
   applied at the end of s; by 10:00 ET of s+1 it is already in the
   ledger. The step is the documented home of the ordering invariant.
2. force-closes for ratio actions announced in the (previous 10:00 ET
   window, this 10:00 ET window], executed against the file visible at
   10:00 — which is file(t-1), the last pre-action file;
3. exercise elections at the 10:00 ET window (arm B) over positions held
   AT THE START of the session, from file(t-1) facts only — a settlement
   scheduled to strike at close(t);
4. scheduled exits then entries at 10:00 (sell side first — it frees
   cash), with execution-time cancellation for overnight announcements;
   rejected exits and force-closes retry at the next window;
5. expiry / elected-early-exercise / terminal settlements strike at
   close(t) and apply at bar(t)'s publication instant — the last events
   of the session, keeping the ledger timeline non-decreasing;
6. conservative mark at close(t) from file(t-1)'s EOD BID (strictly
   knowable; marks feed the equity curve only — the §4 statistics use the
   per-position fill-to-fill premium returns; a contract absent from the
   visible file marks at ZERO, the strictly conservative choice);
7. ``assert_conservation()`` EVERY session.

Nonclaims: synthetic worlds; machinery validation only; no real-data or
performance claim. The execution premium embeds session-d's underlying
close — post-decision PUBLIC information priced into the fill (recorded
in the plan, not leakage).
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Literal

from tree_options.candidates.filters import CandidateFilter
from tree_options.data.actions import CorporateActionRecord
from tree_options.data.authority import PointInTimeDataset
from tree_options.data.bars import BarRecord
from tree_options.data.options_pit import NoOptionFileError, OptionPitSurface
from tree_options.evaluation.stats import BacktestSummary, backtest_summary
from tree_options.guards.fills import FillEngine, FillRejection
from tree_options.ledger.book import LedgerBook
from tree_options.ledger.fees import PerContractFeeModel
from tree_options.options import (
    CandidateAudit,
    OptionSignal,
    OptionsStrategyConfig,
    affordable_contracts,
    build_candidates,
    cancellations_at_execution,
    classify_action,
    pending_dividend_per_share,
    plan_orders,
)
from tree_options.options.exercise import ExerciseElectionInputs, should_elect_exercise
from tree_options.options.settlement import mint_settlement
from tree_options.options.strategy import exit_decision_session
from tree_options.schemas.common import FEE_TICK
from tree_options.schemas.market import (
    CrossedQuoteError,
    LockedQuoteError,
    NonpositiveQuoteError,
    NonTickQuoteError,
    NonTradableConditionError,
    StaleQuoteError,
    ZeroSizeQuoteError,
    select_quote,
)
from tree_options.schemas.options import OptionContract
from tree_options.schemas.trading import Fill, Order
from tree_options.time.calendar import SessionCalendar
from tree_options.time.sessions import shift_instant

Arm = Literal["A", "B"]
DATASET_PROVENANCE = "synthetic/v1"
DECLARED_MAX_QUOTE_AGE_SECONDS = 7200  # owner ruling 4: rides in the config hash
EXECUTION_OFFSET_SECONDS = 1800  # 10:00 ET = session open (09:30) + 30 min
BAR_PUBLICATION_HOUR_UTC = 23  # the synthetic vendor's same-date bar wall

# The fill engine's quote-reality rejections propagate as their own error
# types (frozen M0 contract — tests assert them raw); the backtest treats
# every one as a counted fill rejection, never a crash.
QUOTE_REJECTION_ERRORS = (
    CrossedQuoteError,
    LockedQuoteError,
    NonpositiveQuoteError,
    NonTickQuoteError,
    NonTradableConditionError,
    StaleQuoteError,
    ZeroSizeQuoteError,
)


def _rejection_code(exc: Exception) -> str:
    if isinstance(exc, FillRejection):
        return exc.code
    return type(exc).__name__


class OptionsBacktestError(RuntimeError):
    """Fail-closed malformed-input or execution error."""


@dataclass
class _OpenPosition:
    contract_id: str
    underlying_id: str
    call_put: str
    score: float
    label: float | None
    entry_fill_id: str
    entry_price: Decimal
    entry_session: date
    contract_expiration: date  # stamped on rows: criterion 4 needs it
    # (G3 extension, verdict D6) the decision-visible selection facts the
    # band rules classified the entry under; None only for rows minted
    # before a candidate carried them (never in this runner's path)
    strike: Decimal | None = None
    abs_delta: Decimal | None = None
    dte_at_entry: int | None = None
    decision_session: date | None = None
    exit_kind: str | None = None  # sell | early_exercise | expiry | terminal
    exit_fill_id: str | None = None
    exit_price: Decimal | None = None
    exit_session: date | None = None


@dataclass(frozen=True)
class PositionRow:
    """One round trip (or still-open position) — the §4 statistics input.

    The G3-extension fields (verdict D6, additive with defaults so existing
    constructors are untouched) carry the DECISION-VISIBLE selection facts:
    `strike`/`abs_delta` are the ladder probe's file(t-1) values the band
    rules classified on, `dte_at_entry` is the filter's calendar-day
    convention (expiration - decision session), and `decision_session` is
    the close that ordered the entry — without these T-BAND/T-DTE
    falsifiers and the earnings retro-tag are not artifact-computable."""

    contract_id: str
    underlying_security_id: str
    call_put: str
    score: float
    label: float | None
    entry_fill_id: str
    entry_price: Decimal
    entry_session: date
    contract_expiration: date
    exit_kind: str | None  # None == still open at the end
    exit_fill_id: str | None
    exit_price: Decimal | None
    exit_session: date | None
    premium_return: float | None  # (exit - entry) / entry, fill to fill
    strike: Decimal | None = None
    abs_delta: Decimal | None = None
    dte_at_entry: int | None = None
    decision_session: date | None = None


@dataclass
class OptionsCounters:
    not_evaluable_candidates: int = 0
    failed_candidates: int = 0
    no_in_band_expiry: int = 0
    no_in_band_strike: int = 0
    excluded_pending_action: int = 0
    entries_cancelled: int = 0
    entries_skipped_open: int = 0
    entry_fill_rejections: dict[str, int] = field(default_factory=dict)
    exit_fill_rejections: dict[str, int] = field(default_factory=dict)
    force_closes: int = 0
    force_close_rejections: dict[str, int] = field(default_factory=dict)
    early_exercises: int = 0
    expiries: int = 0
    terminals: int = 0
    exit_retries: int = 0
    mark_misses: int = 0
    conservation_checks: int = 0
    rule_histogram: dict[tuple[str, str], int] = field(default_factory=dict)

    def bump(self, bucket: dict[str, int], code: str) -> None:
        bucket[code] = bucket.get(code, 0) + 1


@dataclass(frozen=True)
class FillAudit:
    """Per-fill provenance for the sealed gate's criterion 2: the order's
    decision instant/session and the SELECTED quote's receipt (re-derived
    through the same shared `select_quote` with the same execution instant
    the engine used — zero stress, so the selections are identical)."""

    fill_id: str
    decision_session: date
    decision_at: datetime
    quote_received_at: datetime
    execution_at: datetime
    execution_session: date


@dataclass(frozen=True)
class OptionsBacktestResult:
    dataset_provenance: str
    arm: Arm
    summary: BacktestSummary
    sessions: tuple[date, ...]
    turnovers: tuple[float, ...]
    equities: tuple[Decimal, ...]
    positions: tuple[PositionRow, ...]
    fills: tuple[Fill, ...]
    fill_audit: tuple[FillAudit, ...]
    label_hits: tuple[bool, ...]
    counters: OptionsCounters
    audit: CandidateAudit
    terminal_cash: Decimal
    terminal_market_value: Decimal
    terminal_equity: Decimal
    open_positions: tuple[str, ...]


def _execution_instant(calendar: SessionCalendar, session: date) -> datetime:
    return shift_instant(calendar.session_open(session), EXECUTION_OFFSET_SECONDS)


def _intrinsic(contract: OptionContract, spot_mid: Decimal) -> Decimal:
    if contract.call_put == "C":
        return max(spot_mid - contract.strike, Decimal(0))
    return max(contract.strike - spot_mid, Decimal(0))


def _last_bar_at_or_before(
    bar_map: dict[tuple[str, date], BarRecord], sid: str, session: date
) -> BarRecord | None:
    best: BarRecord | None = None
    for (bar_sid, bar_session), bar in bar_map.items():
        if (
            bar_sid == sid
            and bar_session <= session
            and (best is None or bar_session > best.session)
        ):
            best = bar
    return best


def _row(position: _OpenPosition) -> PositionRow:
    premium_return = None
    if position.exit_price is not None and position.entry_price > 0:
        premium_return = float(position.exit_price / position.entry_price - Decimal(1))
    return PositionRow(
        contract_id=position.contract_id,
        underlying_security_id=position.underlying_id,
        call_put=position.call_put,
        score=position.score,
        label=position.label,
        entry_fill_id=position.entry_fill_id,
        entry_price=position.entry_price,
        entry_session=position.entry_session,
        contract_expiration=position.contract_expiration,
        exit_kind=position.exit_kind,
        exit_fill_id=position.exit_fill_id,
        exit_price=position.exit_price,
        exit_session=position.exit_session,
        premium_return=premium_return,
        strike=position.strike,
        abs_delta=position.abs_delta,
        dte_at_entry=position.dte_at_entry,
        decision_session=position.decision_session,
    )


def run_options_backtest(
    *,
    calendar: SessionCalendar,
    surface: OptionPitSurface,
    dataset: PointInTimeDataset,
    candidate_filter: CandidateFilter,
    signals: Sequence[OptionSignal],
    initial_cash: Decimal,
    config: OptionsStrategyConfig,
    arm: Arm,
    end_session: date | None = None,
    fee_model: PerContractFeeModel | None = None,
    max_quote_age_seconds: int = DECLARED_MAX_QUOTE_AGE_SECONDS,
    execution_calendar: SessionCalendar | None = None,
) -> OptionsBacktestResult:
    """Run one arm of the options strategy over supplied PIT signals.

    `candidate_filter` and `max_quote_age_seconds` are caller-supplied so
    the trial config hash owns them (the 7200 s override is the owner's
    declared ruling; the protocol file stays byte-frozen).

    (P1-1, Codex round 1) THE DUAL-CALENDAR SEAM. `calendar` is the
    DECISION GRID (the era profile's Friday-only grid): it sequences
    decisions, entries, exits, marks and the session loop. The FILL
    ENGINE carries its own session checks (`is_session`,
    `session_close`, ordinal recency, `EXECUTION_INSTANT_MISMATCH`),
    and those must run against the EXECUTION calendar — the daily
    calendar the bars are stamped on (the overlay's
    `MassiveDerivedSessionCalendar`) — so a Friday decision at D can
    fill at the next grid Friday D+1 against the previous TRADING day's
    bar. `execution_calendar=None` (the default) keeps ONE calendar for
    both roles: lane-1/synthetic behavior is byte-identical.

    (022-C, 0.2.2 declaration 3 — owner ruling m4-022-ruling-20260828)
    On the dual-calendar lane the decision grid also supplies the engine's
    `decision_closes`: the frozen VERIFIED closes the fill door's
    DECISION-side comparison consumes (the execution calendar keeps every
    execution-side check). On the real lane `calendar` arrives as the
    runner's BOUND calendar, whose own `session_close` answers the
    boundary-verified map, so the derived dict carries exactly the
    verified instants; the single-calendar lane passes None and keeps
    today's door byte-identically."""
    fill_calendar = calendar if execution_calendar is None else execution_calendar
    decision_closes: Mapping[date, datetime] | None = (
        None
        if execution_calendar is None
        else {s: calendar.session_close(s) for s in calendar.sessions()}
    )
    bar_map: dict[tuple[str, date], BarRecord] = {}
    for bar in dataset.bars:
        bar_map[(bar.security_id, bar.session)] = bar
    if arm not in ("A", "B"):
        raise OptionsBacktestError(f"arm must be A or B, got {arm!r}")
    snapshot_of_dataset = dataset.bars[0].snapshot_id if dataset.bars else None
    if surface.snapshot_id != snapshot_of_dataset:
        raise OptionsBacktestError(
            f"surface world {surface.snapshot_id} != dataset snapshot {snapshot_of_dataset}: "
            "cross-world leakage refused"
        )
    if not signals:
        raise OptionsBacktestError("at least one signal is required")
    seen_signals: set[tuple[date, str]] = set()
    signals_by_session: defaultdict[date, list[OptionSignal]] = defaultdict(list)
    for row in signals:
        key = (row.decision_session, row.security_id)
        if key in seen_signals:
            raise OptionsBacktestError(f"duplicate signal for {key}")
        if not math.isfinite(row.score) or (row.label is not None and not math.isfinite(row.label)):
            raise OptionsBacktestError(f"non-finite signal for {key}")
        seen_signals.add(key)
        signals_by_session[row.decision_session].append(row)

    actions_by_sid: defaultdict[str, list[CorporateActionRecord]] = defaultdict(list)
    for action in dataset.actions:
        actions_by_sid[action.security_id].append(action)
    for records in actions_by_sid.values():
        records.sort(key=lambda a: (a.available_at, a.source_record_id))

    first_execution = calendar.nth_after(min(signals_by_session), 1)
    terminal_session = end_session or calendar.nth_after(max(signals_by_session), 1)
    if calendar.ordinal(terminal_session) < calendar.ordinal(first_execution):
        raise OptionsBacktestError("end_session must include the first execution session")
    sessions = tuple(
        s
        for s in calendar.sessions()
        if calendar.ordinal(first_execution)
        <= calendar.ordinal(s)
        <= calendar.ordinal(terminal_session)
    )

    fees = fee_model or PerContractFeeModel()
    # (P1-1) the fill engine runs on the EXECUTION calendar; the grid
    # calendar keeps every other responsibility in this backtest.
    # (022-C) the dual-calendar lane additionally hands the engine the
    # grid's VERIFIED decision closes for the door's decision-side
    # comparison — the execution calendar keeps the execution-side checks.
    engine = FillEngine(
        fill_calendar,
        fee_model=fees,
        max_quote_age_seconds=max_quote_age_seconds,
        decision_closes=decision_closes,
    )
    ledger = LedgerBook(initial_cash)
    counters = OptionsCounters()
    audit = CandidateAudit()

    open_positions: dict[str, _OpenPosition] = {}
    # (G3 extension) contract_id -> the accepted candidate's decision-visible
    # (strike, |delta|, dte, decision session), minted in schedule_entries
    entry_facts: dict[str, tuple[Decimal, Decimal, int, date]] = {}
    closed_rows: list[PositionRow] = []
    fills: list[Fill] = []
    fill_audit: list[FillAudit] = []
    pending_entries: dict[date, tuple[Order, ...]] = {}
    pending_exits: dict[date, list[Order]] = defaultdict(list)
    order_signals: dict[str, OptionSignal] = {}
    contract_underlying: dict[str, str] = {}
    equities: list[Decimal] = []
    turnovers: list[float] = []
    label_hits: list[bool] = []
    returns: list[float] = []
    previous_equity = ledger.initial_cash
    last_window_instant: datetime | None = None

    def execute_fill(order: Order, *, session: date, instant: datetime) -> Fill:
        quotes = surface.visible_quotes_as_of(order.contract_id, instant)
        contract = surface.contract(order.contract_id)
        fill = engine.execute(
            order,
            quotes,
            contract,
            execution_session=session,
            execution_at=instant,
        )
        # criterion-2 provenance: the same shared selection the engine ran
        # (zero stress => identical effective instant), stamped per fill
        selected = select_quote(quotes, instant)
        fill_audit.append(
            FillAudit(
                fill_id=fill.fill_id,
                decision_session=order.decision_session,
                decision_at=order.decision_at,
                quote_received_at=selected.received_timestamp,
                execution_at=fill.execution_at,
                execution_session=fill.execution_session,
            )
        )
        return fill

    def close_position_row(
        contract_id: str, *, kind: str, price: Decimal, session: date, fill_id: str | None = None
    ) -> None:
        position = open_positions.pop(contract_id, None)
        if position is not None:
            position.exit_kind = kind
            position.exit_price = price
            position.exit_session = session
            position.exit_fill_id = fill_id
            closed_rows.append(_row(position))

    def settle(
        *,
        contract: OptionContract,
        kind: str,
        quantity: int,
        session: date,
        detect_at=None,
        action: CorporateActionRecord | None = None,
        ts_floor: datetime | None = None,
    ) -> None:
        if action is not None:
            # review r2 P1-1 + r3 P1-1: an action-driven terminal prices at
            # the EFFECTIVE session's final bar (last bar at-or-before as the
            # degenerate fallback) and stamps at the detection instant —
            # max(action publication, bar publication) — floored at this
            # session's execution instant: when publication lands between
            # the next open and the fills (13:45Z vs 13:30Z/14:00Z), the
            # due-scan fires a session late and the settlement applies AFTER
            # that session's fills, so its ts must never precede them.
            bar = bar_map.get((contract.underlying_security_id, action.effective_session))
            if bar is None:
                bar = _last_bar_at_or_before(
                    bar_map, contract.underlying_security_id, action.effective_session
                )
            if bar is None:
                raise OptionsBacktestError(
                    f"MISSING_SETTLEMENT_BAR: {contract.underlying_security_id}/"
                    f"{action.effective_session}"
                )
            detect_at = max(action.available_at, bar.available_at)
            if ts_floor is not None:
                detect_at = max(detect_at, ts_floor)
            mint_session = session
        else:
            bar = bar_map.get((contract.underlying_security_id, session))
            if bar is None and kind == "terminal":
                bar = _last_bar_at_or_before(bar_map, contract.underlying_security_id, session)
            if bar is None:
                raise OptionsBacktestError(
                    f"MISSING_SETTLEMENT_BAR: {contract.underlying_security_id}/{session}"
                )
            mint_session = session if detect_at is not None else bar.session
        settlement = mint_settlement(
            contract=contract,
            settlement_id=f"STL-{kind[0].upper()}-{contract.contract_id}-{session:%Y%m%d}",
            kind=kind,  # type: ignore[arg-type]
            quantity=quantity,
            session=mint_session,
            reference_bar=bar,
            terminal_detect_at=detect_at,
        )
        ledger.apply_settlement(settlement)
        close_position_row(
            contract.contract_id,
            kind=kind,
            price=(settlement.cash / (quantity * contract.multiplier)).quantize(FEE_TICK),
            session=settlement.session,
        )

    def retry_sell(contract_id: str, quantity: int, *, decided_session: date) -> None:
        retry_session = calendar.nth_after(decided_session, 1)
        pending_exits[retry_session].append(
            Order(
                order_id=f"OPT-R{retry_session:%Y%m%d}-{contract_id}",
                contract_id=contract_id,
                side="sell",
                intent="close_long",
                quantity=quantity,
                decision_at=calendar.session_close(decided_session),
                decision_session=decided_session,
            )
        )

    def schedule_entries(decision: date) -> None:
        """close(d) decision → candidates → orders pending for the next
        session's execution window (the T+1 discipline)."""
        decision_rows = tuple(signals_by_session.get(decision, ()))
        if not decision_rows:
            return
        candidates = build_candidates(
            surface=surface,
            candidate_filter=candidate_filter,
            decision_session=decision,
            scores=decision_rows,
            config=config,
            actions=tuple(dataset.actions),
            audit=audit,
        )
        for candidate in candidates:
            contract_underlying[candidate.contract_id] = candidate.underlying_security_id
            # (G3 extension) the decision-visible selection facts, keyed for
            # the entry fill: the ladder probe's own strike/|delta| pair and
            # the filter's calendar-day DTE of the DECISION session
            entry_facts[candidate.contract_id] = (
                candidate.strike,
                candidate.abs_delta,
                (candidate.expiration - candidate.signal.decision_session).days,
                candidate.signal.decision_session,
            )
        orders = plan_orders(
            calendar=calendar,
            candidates=candidates,
            cash=ledger.cash,
            config=config,
            fee_model=fees,
        )
        if orders:
            pending_entries[calendar.nth_after(decision, 1)] = orders
        for candidate in candidates:
            for order in orders:
                if order.contract_id == candidate.contract_id:
                    order_signals[order.order_id] = candidate.signal

    # The earliest close(d) decision PRECEDES the first execution session:
    # seed its pending entries so the fold's first scored cohort is tradable
    # (review r1 P1-1 — the loop begins at d+1 and section 7 only converted
    # signals on sessions the loop visits, silently dropping the first cohort).
    schedule_entries(min(signals_by_session))

    for session in sessions:
        instant = _execution_instant(calendar, session)
        traded_notional = Decimal(0)

        # -- 2. force-closes for ratio actions announced since the last window
        if config.allow_force_close:
            for contract_id in sorted(open_positions):
                position = open_positions[contract_id]
                trigger = None
                for action in actions_by_sid.get(position.underlying_id, ()):
                    if classify_action(action.kind) != "ratio":
                        continue
                    if action.available_at <= instant and (
                        last_window_instant is None or action.available_at > last_window_instant
                    ):
                        trigger = action
                        break
                if trigger is None:
                    continue
                held = ledger.quantity(contract_id)
                if held < 1:
                    continue
                prev_session = calendar.sessions()[calendar.ordinal(session) - 1]
                order = Order(
                    order_id=f"OPT-F{session:%Y%m%d}-{contract_id}",
                    contract_id=contract_id,
                    side="sell",
                    intent="close_long",
                    quantity=held,
                    decision_at=calendar.session_close(prev_session),
                    decision_session=prev_session,
                )
                try:
                    fill = execute_fill(order, session=session, instant=instant)
                except (*QUOTE_REJECTION_ERRORS, FillRejection) as rejection:
                    counters.bump(counters.force_close_rejections, _rejection_code(rejection))
                    retry_sell(contract_id, held, decided_session=session)
                    continue
                ledger.apply(fill)
                fills.append(fill)
                traded_notional += fill.notional()
                counters.force_closes += 1
                close_position_row(
                    contract_id,
                    kind="sell",
                    price=fill.price,
                    session=session,
                    fill_id=fill.fill_id,
                )

        # -- 3. exercise elections (arm B): file(t-1) facts only
        elected_today: list[tuple[str, int]] = []
        if arm == "B":
            for contract_id in sorted(open_positions):
                position = open_positions[contract_id]
                contract = surface.contract(contract_id)
                if contract.expiration <= session:
                    continue  # an at-expiry death is an expiry settlement
                try:
                    entry = surface.entry_as_of(position.underlying_id, instant, contract_id)
                    spot_mid = surface.spot_mid_as_of(position.underlying_id, instant)
                except NoOptionFileError:
                    continue
                if entry is None:
                    continue
                inputs = ExerciseElectionInputs(
                    exercise_style=contract.exercise_style,
                    call_put=contract.call_put,
                    expiration_seen=True,
                    mid_premium=(entry.quote_eod.bid + entry.quote_eod.ask) / 2,
                    bid=entry.quote_eod.bid,
                    intrinsic=_intrinsic(contract, spot_mid),
                    pending_dividend_per_share=pending_dividend_per_share(
                        actions=tuple(actions_by_sid.get(position.underlying_id, ())),
                        underlying_id=position.underlying_id,
                        visible_by=instant,
                        effective_after=session,
                        effective_through=contract.expiration,
                    ),
                )
                if should_elect_exercise(inputs):
                    elected_today.append((contract_id, ledger.quantity(contract_id)))

        # -- 4. scheduled exits then entries at 10:00 (sell side first)
        for order in pending_exits.pop(session, []):
            held = ledger.quantity(order.contract_id)
            if held < 1:
                continue
            if order.quantity != held:
                order = order.model_copy(update={"quantity": held})
            try:
                fill = execute_fill(order, session=session, instant=instant)
            except (*QUOTE_REJECTION_ERRORS, FillRejection) as rejection:
                counters.bump(counters.exit_fill_rejections, _rejection_code(rejection))
                counters.exit_retries += 1
                retry_sell(order.contract_id, held, decided_session=session)
                continue
            ledger.apply(fill)
            fills.append(fill)
            traded_notional += fill.notional()
            close_position_row(
                order.contract_id,
                kind="sell",
                price=fill.price,
                session=session,
                fill_id=fill.fill_id,
            )

        todays_entries = pending_entries.pop(session, ())
        cancelled_ids = {
            order.order_id
            for order in cancellations_at_execution(
                orders=todays_entries,
                surface=surface,
                actions=tuple(dataset.actions),
                execution_at=instant,
                contract_underlying=contract_underlying,
                config=config,
            )
        }
        for order in todays_entries:
            if order.order_id in cancelled_ids:
                counters.entries_cancelled += 1
                continue
            if order.contract_id in open_positions:
                # the quintile re-selected a name whose contract is already
                # held: one round trip per contract (the row model), never a
                # silent overwrite of the open position's provenance
                counters.entries_skipped_open += 1
                continue
            underlying = contract_underlying[order.contract_id]
            contract = surface.contract(order.contract_id)
            try:
                visible_entry = surface.entry_as_of(underlying, instant, order.contract_id)
            except NoOptionFileError:
                visible_entry = None
            if visible_entry is None:
                counters.bump(counters.entry_fill_rejections, "NO_VISIBLE_QUOTE")
                continue
            # clamp against solvency (ledger cash) AND the planner's
            # per-candidate premium budget — an overnight ask gap must not
            # spend past the configured session budget (review r1 P1-4:
            # the old cash-only re-clamp filled 100x-gap entries against
            # the whole ledger)
            clamped = order.quantity
            clamp_budgets: tuple[Decimal, ...] = (ledger.cash,)
            if order.budget_notional is not None:
                clamp_budgets += (order.budget_notional,)
            for budget in clamp_budgets:
                clamped = min(
                    clamped,
                    affordable_contracts(
                        budget=budget,
                        ask=visible_entry.quote_eod.ask,
                        multiplier=contract.multiplier,
                        fee_model=fees,
                        cap=clamped,
                    ),
                )
            if clamped < 1:
                counters.bump(counters.entry_fill_rejections, "UNAFFORDABLE_AT_EXECUTION")
                continue
            if clamped != order.quantity:
                order = order.model_copy(update={"quantity": clamped})
            try:
                fill = execute_fill(order, session=session, instant=instant)
            except (*QUOTE_REJECTION_ERRORS, FillRejection) as rejection:
                counters.bump(counters.entry_fill_rejections, _rejection_code(rejection))
                continue
            ledger.apply(fill)
            fills.append(fill)
            traded_notional += fill.notional()
            signal = order_signals.get(order.order_id)
            facts = entry_facts.get(order.contract_id)
            open_positions[order.contract_id] = _OpenPosition(
                contract_id=order.contract_id,
                underlying_id=underlying,
                call_put=contract.call_put,
                score=signal.score if signal else 0.0,
                label=signal.label if signal else None,
                entry_fill_id=fill.fill_id,
                entry_price=fill.price,
                entry_session=session,
                contract_expiration=contract.expiration,
                strike=facts[0] if facts else None,
                abs_delta=facts[1] if facts else None,
                dte_at_entry=facts[2] if facts else None,
                decision_session=facts[3] if facts else order.decision_session,
            )
            if arm == "A":
                exit_session = calendar.nth_after(session, config.exit_sessions_after_entry)
                decision = exit_decision_session(calendar, session, config)
                pending_exits[exit_session].append(
                    Order(
                        order_id=f"OPT-S{decision:%Y%m%d}-{order.contract_id}",
                        contract_id=order.contract_id,
                        side="sell",
                        intent="close_long",
                        quantity=fill.quantity,
                        decision_at=calendar.session_close(decision),
                        decision_session=decision,
                    )
                )
            if signal is not None and signal.label is not None:
                label_hits.append(signal.label > 0.0)

        # -- 5. settlements striking at close(t), applied at bar publication
        settlements_due: list[tuple[str, str, int]] = []
        terminal_actions: dict[str, CorporateActionRecord] = {}
        # terminal actions striking at close(effective_session): the loop must
        # BE at (or past) the effective session — the final bar exists then —
        # and the action must be visible by tomorrow's open (review r1 P1-3:
        # firing on availability alone settled a pub(t-1)/effective(t) merger
        # at t-1 against the pre-merger bar)
        next_open = calendar.session_open(calendar.nth_after(session, 1))
        for contract_id in sorted(open_positions):
            for action in actions_by_sid.get(open_positions[contract_id].underlying_id, ()):
                if (
                    classify_action(action.kind) == "terminal"
                    and action.effective_session <= session
                    and action.available_at <= next_open
                ):
                    settlements_due.append(("terminal", contract_id, ledger.quantity(contract_id)))
                    terminal_actions[contract_id] = action
                    break
        # SILENT deaths (bankruptcy_11 / voluntary_delisting / coverage_lapse
        # emit no action record): the underlying has no bar this session but
        # has a definitive last bar — the complete-vendor inference. Knowable
        # exactly at this session's failed bar publication (23:00 UTC, the
        # vendor wall); settles at the last bar's close, stamped there.
        # This scan runs BEFORE elections and expiries so a death claims the
        # position first: an elected option on an underlying that died last
        # session cannot strike at close(t) (no bar exists) — it settles as a
        # terminal at the last bar, and an at-expiry death on a barless name
        # is terminal for the same reason.
        detect_at_wall = datetime(
            session.year, session.month, session.day, BAR_PUBLICATION_HOUR_UTC, tzinfo=UTC
        )
        silent_deaths: set[str] = set()
        claimed: set[str] = {c for _k, c, _q in settlements_due}
        for contract_id in sorted(open_positions):
            position = open_positions[contract_id]
            if (position.underlying_id, session) in bar_map:
                continue  # alive today
            if _last_bar_at_or_before(bar_map, position.underlying_id, session) is None:
                continue  # never had a bar in the evaluated window
            if contract_id in claimed:
                continue  # the action-driven terminal already claimed it
            silent_deaths.add(contract_id)
            claimed.add(contract_id)
            settlements_due.append(("terminal_silent", contract_id, ledger.quantity(contract_id)))
        for contract_id, quantity in elected_today:
            held_now = ledger.quantity(contract_id)
            if contract_id in claimed or quantity < 1 or held_now < 1:
                continue  # a terminal death outranks the election
            settlements_due.append(("early_exercise", contract_id, min(quantity, held_now)))
            claimed.add(contract_id)
        for contract_id in sorted(open_positions):
            contract = surface.contract(contract_id)
            if (
                contract.expiration == session
                and ledger.quantity(contract_id) >= 1
                and contract_id not in claimed
            ):
                settlements_due.append(("expiry", contract_id, ledger.quantity(contract_id)))
        for kind, contract_id, quantity in sorted(settlements_due):
            settle(
                contract=surface.contract(contract_id),
                kind="terminal" if kind == "terminal_silent" else kind,
                quantity=quantity,
                session=session,
                detect_at=detect_at_wall if kind == "terminal_silent" else None,
                action=None if kind == "terminal_silent" else terminal_actions.get(contract_id),
                ts_floor=instant,
            )
            if kind == "early_exercise":
                counters.early_exercises += 1
            elif kind == "expiry":
                counters.expiries += 1
            else:
                counters.terminals += 1

        # -- 6. conservative mark at close(t) from file(t-1) EOD bid
        close_at = calendar.session_close(session)
        prev_session = calendar.sessions()[calendar.ordinal(session) - 1]
        market_value = Decimal(0)
        for contract_id in sorted(open_positions):
            position = open_positions[contract_id]
            # review r2 P1-3: file(t-1) must EXIST for the underlying — an
            # older visible file is stale (the reach-back marked its bid,
            # overstating equity); the declared behavior is zero + a miss.
            if surface.visible_file_session(position.underlying_id, close_at) != prev_session:
                counters.mark_misses += 1  # marked at 0 — strictly conservative
                continue
            try:
                entry = surface.entry_as_of(position.underlying_id, close_at, contract_id)
            except NoOptionFileError:
                entry = None
            if entry is None:
                counters.mark_misses += 1  # marked at 0 — strictly conservative
                continue
            market_value += (entry.quote_eod.bid * ledger.quantity(contract_id) * 100).quantize(
                FEE_TICK
            )
        equity = (ledger.cash + market_value).quantize(FEE_TICK)
        equities.append(equity)
        if previous_equity > 0:
            returns.append(float(equity / previous_equity - Decimal(1)))
            turnovers.append(float(traded_notional / previous_equity))
        else:
            turnovers.append(0.0)
        previous_equity = equity

        # -- 7. conservation EVERY session
        ledger.assert_conservation()
        counters.conservation_checks += 1
        last_window_instant = instant

        # -- 7. schedule tomorrow's entries from today's close decision
        schedule_entries(session)

    counters.not_evaluable_candidates = audit.filter_not_evaluable
    counters.failed_candidates = audit.filter_fail
    counters.no_in_band_expiry = audit.no_in_band_expiry
    counters.no_in_band_strike = audit.no_in_band_strike
    counters.excluded_pending_action = audit.excluded_pending_action
    counters.rule_histogram = dict(audit.rule_histogram)

    all_rows = closed_rows + [_row(p) for p in open_positions.values()]
    all_rows.sort(key=lambda r: (r.entry_session, r.contract_id))
    return OptionsBacktestResult(
        dataset_provenance=DATASET_PROVENANCE,
        arm=arm,
        summary=backtest_summary(returns, turnovers, label_hits),
        sessions=sessions,
        label_hits=tuple(label_hits),
        turnovers=tuple(turnovers),
        equities=tuple(equities),
        positions=tuple(all_rows),
        fills=tuple(fills),
        fill_audit=tuple(fill_audit),
        counters=counters,
        audit=audit,
        terminal_cash=ledger.cash,
        terminal_market_value=(equities[-1] - ledger.cash) if equities else Decimal(0),
        terminal_equity=equities[-1] if equities else ledger.cash,
        open_positions=tuple(sorted(open_positions)),
    )
