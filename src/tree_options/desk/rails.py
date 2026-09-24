"""Desk rails (plan E4 + D6): the ONE admission check for the options desk.

:func:`check` is pure (no I/O, no clock, no environment): the deal miner
pre-checks every candidate with it, and desk-enter and the desk runtime
re-check authoritatively with it before the first order. Every rule in
:data:`RULES` yields exactly one :class:`RuleResult` with status PASS, FAIL
or NOT_EVALUABLE and a detail string; the report is ``ok`` only if EVERY
rule is PASS. Missing, malformed or incoherent data is NOT_EVALUABLE
(fail closed); an exception inside a rule is caught and recorded as
NOT_EVALUABLE, never raised past the check. Money is Decimal: a float
where money belongs is refused (NOT_EVALUABLE), never coerced.

Rules (thresholds come from :class:`RailLimits`, the sealed playbook's
``[limits]`` table):

- ``paper_only``: the account mode is "paper".
- ``defined_risk``: the legs, kind, quantity and entry price form a valid
  E1 :class:`~tree_options.trex.plan.LegStructure` (every SELL covered, the
  kind's shape, planned exit before the first expiry), and its recomputed
  max loss equals the candidate's stated one.
- ``max_loss_per_trade``: stated max loss <= the per-trade cap.
- ``max_book_loss``: the book's max loss (working entries at their cap,
  as the book view states them) + this trade <= the book cap.
- ``per_underlying``: positions on this underlying held ACCOUNT-WIDE (the
  legacy trex book included) + 1 <= the cap.
- ``net_beta_delta``: net beta-weighted delta, in $ of P&L per 1% SPY move
  (sum of delta_shares x spot x beta x 0.01), after the trade within
  +/- the cap; a trade that strictly reduces |book delta| always passes.
- ``admissions_per_session``: admissions already made this session + 1
  <= the cap.
- ``roundtrip_cost``: entry + exit half-spreads on every leg (the exit
  spread is assumed equal to today's) plus $0.65 per contract per leg on
  both the open and the close, <= the fraction of max loss.
- ``leg_open_interest``: every leg's open interest >= the floor.
- ``leg_spread``: every leg's (ask - bid) / mid <= the fraction.
- ``earnings_short_premium``: a short-premium structure (a credit kind:
  credit vertical, iron condor) holds no confirmed, sealed OR estimated
  earnings date in [entry session, planned exit], inclusive: a report on
  either end blocks whatever its bmo/amc label says. Debit kinds pass.
- ``ex_dividend_short_call``: a structure with any short call holds no
  declared or projected ex-dividend date in [entry session, planned exit],
  inclusive (early assignment); no dividend data fails closed only for
  structures with a short call.
- ``long_single_delta``: a long single's |leg delta| >= the floor.
- ``fresh_chain``: the chain's session is at most
  ``max_chain_age_sessions`` NYSE sessions before the ENTRY session (with 1:
  the latest completed session, for the miner's evening run and for the
  runtime's morning re-check alike), and its as-of stamp is aware, not
  before its session's date and not after ``now``.
- ``book_short_vega``: net book vega after the trade >= -cap ($ per vol
  point); a trade that strictly reduces the net short always passes.
- ``news_veto``: None means no veto source is configured (PASS, said so);
  a configured source that is unavailable is NOT_EVALUABLE; a veto FAILs.

Units the greeks lane (``trex/greeks.py`` ``StructureGreeks``) supplies:
``delta_shares`` and ``vega_usd_per_volpt`` are POSITION-level (quantity
and the 100 multiplier included), signed. Spot and beta are per share.
"""

from __future__ import annotations

import bisect
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Final, Literal, Protocol

from tree_options.desk.events import EarningsEvent
from tree_options.trex.clock import ET
from tree_options.trex.plan import (
    CREDIT_KINDS,
    Action,
    ExitRules,
    Kind,
    Leg,
    LegStructure,
    Right,
)

PASS: Final = "PASS"
FAIL: Final = "FAIL"
NOT_EVALUABLE: Final = "NOT_EVALUABLE"

Status = Literal["PASS", "FAIL", "NOT_EVALUABLE"]

RULES: tuple[str, ...] = (
    "paper_only",
    "defined_risk",
    "max_loss_per_trade",
    "max_book_loss",
    "per_underlying",
    "net_beta_delta",
    "admissions_per_session",
    "roundtrip_cost",
    "leg_open_interest",
    "leg_spread",
    "earnings_short_premium",
    "ex_dividend_short_call",
    "long_single_delta",
    "fresh_chain",
    "book_short_vega",
    "news_veto",
)

# IBKR's per-contract option commission; the round trip pays it on the
# open and the close, per leg.
COMMISSION_PER_CONTRACT_USD = Decimal("0.65")
MULTIPLIER = 100
_ONE_PCT = Decimal("0.01")
_CENT = Decimal("0.01")
_ZERO = Decimal(0)


# ------------------------------------------------------------------ limits


class RailLimitsError(ValueError):
    """The ``[limits]`` table is missing, has an unknown or a missing key,
    or a value of the wrong type or range."""


@dataclass(frozen=True)
class RailLimits:
    """The rails' thresholds. Field names are EXACTLY the sealed playbook's
    ``[limits]`` keys (the playbook lane writes the table with them)."""

    max_loss_per_trade_usd: Decimal
    max_book_loss_usd: Decimal
    max_per_underlying: int
    max_net_beta_delta_usd_per_1pct_spy: Decimal
    max_admissions_per_session: int
    max_roundtrip_cost_frac_of_max_loss: Decimal
    min_leg_open_interest: int
    max_leg_spread_frac_of_mid: Decimal
    min_long_single_abs_delta: Decimal
    max_chain_age_sessions: int
    max_book_short_vega_usd_per_volpt: Decimal


# key -> (kind, lower bound, lower inclusive, upper bound or None). Money
# and fractions are TOML strings (never floats); counts are TOML integers.
_LIMIT_SPEC: dict[str, tuple[str, Decimal, bool, Decimal | None]] = {
    "max_loss_per_trade_usd": ("decimal", _ZERO, False, None),
    "max_book_loss_usd": ("decimal", _ZERO, False, None),
    "max_per_underlying": ("int", Decimal(1), True, None),
    "max_net_beta_delta_usd_per_1pct_spy": ("decimal", _ZERO, False, None),
    "max_admissions_per_session": ("int", Decimal(1), True, None),
    "max_roundtrip_cost_frac_of_max_loss": ("decimal", _ZERO, False, Decimal(1)),
    "min_leg_open_interest": ("int", _ZERO, True, None),
    "max_leg_spread_frac_of_mid": ("decimal", _ZERO, False, Decimal(2)),
    "min_long_single_abs_delta": ("decimal", _ZERO, False, Decimal(1)),
    "max_chain_age_sessions": ("int", _ZERO, True, None),
    "max_book_short_vega_usd_per_volpt": ("decimal", _ZERO, True, None),
}
LIMIT_KEYS: frozenset[str] = frozenset(_LIMIT_SPEC)


def _limit_value(key: str, raw: object) -> Decimal | int:
    kind, lo, lo_inclusive, hi = _LIMIT_SPEC[key]
    if kind == "int":
        if not isinstance(raw, int) or isinstance(raw, bool):
            raise RailLimitsError(f"[limits] {key}: expected a TOML integer, got {raw!r}")
        value: Decimal | int = raw
        num = Decimal(raw)
    else:
        if not isinstance(raw, str):
            raise RailLimitsError(
                f'[limits] {key}: money and fractions are strings (e.g. "500"), got {raw!r}'
            )
        try:
            num = Decimal(raw.strip())
        except ArithmeticError:
            raise RailLimitsError(f"[limits] {key}: {raw!r} is not a decimal") from None
        if not num.is_finite():
            raise RailLimitsError(f"[limits] {key}: {raw!r} is not finite")
        value = num
    if num < lo or (num == lo and not lo_inclusive) or (hi is not None and num > hi):
        bound = f"{'>=' if lo_inclusive else '>'} {lo}" + (
            f" and <= {hi}" if hi is not None else ""
        )
        raise RailLimitsError(f"[limits] {key}: {raw!r} out of range (must be {bound})")
    return value


def limits_from_table(table: Mapping[str, Any]) -> RailLimits:
    """RailLimits from a parsed ``[limits]`` table: EXACTLY the keys of
    :data:`LIMIT_KEYS`; an unknown or a missing key raises."""
    unknown = sorted(set(table) - LIMIT_KEYS)
    missing = sorted(LIMIT_KEYS - set(table))
    if unknown or missing:
        raise RailLimitsError(f"[limits] keys: unknown {unknown}, missing {missing}")
    values = {key: _limit_value(key, table[key]) for key in _LIMIT_SPEC}
    return RailLimits(**values)  # type: ignore[arg-type]


def load_limits(path: Path | str) -> RailLimits:
    """The ``[limits]`` table of a TOML file (the sealed playbook); every
    other table is ignored. The playbook loader owns the seal check."""
    with Path(path).open("rb") as fh:
        doc = tomllib.load(fh)
    table = doc.get("limits")
    if not isinstance(table, dict):
        raise RailLimitsError(f"{Path(path).name}: no [limits] table")
    return limits_from_table(table)


# ------------------------------------------------------------------ inputs


class SessionCalendar(Protocol):
    """The slice of the NYSE calendar the rails read (sorted sessions)."""

    def sessions(self) -> tuple[date, ...]: ...


@dataclass(frozen=True)
class CandidateLeg:
    """One leg with its chain quote. ``delta`` is the per-share option
    delta, signed (calls positive, puts negative)."""

    right: Right
    action: Action
    strike: Decimal
    expiry: date
    bid: Decimal | None
    ask: Decimal | None
    open_interest: int | None
    delta: Decimal | None


@dataclass(frozen=True)
class PositionRisk:
    """Position-level risk inputs (see the module docstring for units)."""

    delta_shares: Decimal | None = None
    vega_usd_per_volpt: Decimal | None = None
    spot: Decimal | None = None
    beta: Decimal | None = None


@dataclass(frozen=True)
class ExDividend:
    """An ex-dividend date: ``declared`` by the issuer, or ``projected``
    from the dividend schedule (see :mod:`tree_options.desk.dividends`)."""

    ex_date: date
    status: str  # declared | projected
    detail: str = ""


@dataclass(frozen=True)
class NewsVeto:
    """A configured news-veto source's answer for this candidate."""

    source: str
    available: bool
    veto: bool
    reason: str = ""


@dataclass(frozen=True)
class Candidate:
    """A trade the desk would open, with the per-underlying facts it is
    judged on.

    ``entry_price`` is the per-package price in DEBIT ORIENTATION (the cap
    paid for a debit kind, the floor received for a credit kind), as
    :class:`~tree_options.trex.plan.LegStructure` ``limit``. ``earnings``
    and ``ex_dividends`` are for this underlying (an ETF reports no
    earnings: ``()``); None means the source is unavailable.
    """

    id: str
    underlying: str
    kind: Kind
    legs: tuple[CandidateLeg, ...]
    quantity: int
    max_loss_usd: Decimal | None
    entry_price: Decimal | None
    planned_exit: date | None
    risk: PositionRisk
    chain_session: date | None
    chain_as_of: datetime | None
    earnings: tuple[EarningsEvent, ...] | None
    ex_dividends: tuple[ExDividend, ...] | None
    news_veto: NewsVeto | None


@dataclass(frozen=True)
class BookPosition:
    """One position the account holds or is entering. ``status``: "open"
    (filled exposure) or "working" (an entry that may fill; its
    ``max_loss_usd`` is at its cap). ``id`` is unique across books."""

    id: str
    source: str
    underlying: str
    status: str
    max_loss_usd: Decimal | None
    risk: PositionRisk = field(default_factory=PositionRisk)
    detail: str = ""


@dataclass(frozen=True)
class BookView:
    """The account's exposure as the rails see it. Any ``problems`` (an
    unreadable book or spec) make every book rule NOT_EVALUABLE."""

    positions: tuple[BookPosition, ...]
    problems: tuple[str, ...] = ()

    def with_risk(self, risk: Mapping[str, PositionRisk]) -> BookView:
        """A copy with each listed position's risk replaced (ids the caller
        has no greeks for keep theirs, i.e. stay None: fail closed)."""
        return replace(
            self,
            positions=tuple(
                replace(p, risk=risk[p.id]) if p.id in risk else p for p in self.positions
            ),
        )


@dataclass(frozen=True)
class RailContext:
    """Per-check facts. ``session`` is the ENTRY session (the session the
    order would be placed in: tomorrow for the evening miner, today for
    desk-enter); ``now`` must be aware and not past that session's date."""

    now: datetime
    session: date
    calendar: SessionCalendar
    account_mode: str | None
    admissions_this_session: int | None


@dataclass(frozen=True)
class RuleResult:
    rule: str
    status: Status
    detail: str


@dataclass(frozen=True)
class RailReport:
    candidate_id: str
    results: tuple[RuleResult, ...]

    @property
    def ok(self) -> bool:
        return tuple(r.rule for r in self.results) == RULES and all(
            r.status == PASS for r in self.results
        )

    def failed(self) -> tuple[RuleResult, ...]:
        """Every rule that is not PASS (FAIL or NOT_EVALUABLE)."""
        return tuple(r for r in self.results if r.status != PASS)

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "ok": self.ok,
            "results": [
                {"rule": r.rule, "status": r.status, "detail": r.detail} for r in self.results
            ],
        }


# ------------------------------------------------------------------ helpers


class _Missing(Exception):
    """An input a rule needs is absent or unusable: NOT_EVALUABLE."""


def _money(value: object, what: str) -> Decimal:
    """A finite Decimal, or _Missing (None, a float, NaN: never coerced)."""
    if value is None:
        raise _Missing(f"{what} missing")
    if not isinstance(value, Decimal):
        raise _Missing(f"{what} is {type(value).__name__}, not Decimal")
    if not value.is_finite():
        raise _Missing(f"{what} is not finite")
    return value


def _count(value: object, what: str) -> int:
    if value is None:
        raise _Missing(f"{what} missing")
    if not isinstance(value, int) or isinstance(value, bool):
        raise _Missing(f"{what} is {type(value).__name__}, not int")
    if value < 0:
        raise _Missing(f"{what} is negative ({value})")
    return value


def _usd(x: Decimal) -> str:
    return f"${x.quantize(_CENT)}"


def _sym(s: str) -> str:
    return s.strip().upper()


def _session_index(cal: SessionCalendar, d: date) -> int | None:
    sessions = cal.sessions()
    i = bisect.bisect_left(sessions, d)
    return i if i < len(sessions) and sessions[i] == d else None


def _context_problem(ctx: RailContext) -> str | None:
    """Why the context can't anchor session-relative rules, or None."""
    if not isinstance(ctx.now, datetime) or ctx.now.tzinfo is None:
        return "context now is not an aware datetime"
    if _session_index(ctx.calendar, ctx.session) is None:
        return f"entry session {ctx.session} is not an NYSE session"
    if ctx.now.astimezone(ET).date() > ctx.session:
        return f"entry session {ctx.session} is before now ({ctx.now.astimezone(ET).date()})"
    return None


def _book_positions(book: BookView | None) -> tuple[BookPosition, ...]:
    if book is None:
        raise _Missing("no book view")
    if book.problems:
        raise _Missing("book unreadable: " + "; ".join(book.problems))
    return book.positions


def _dollars_per_1pct_spy(risk: PositionRisk, who: str) -> Decimal:
    delta = _money(risk.delta_shares, f"{who} delta_shares")
    spot = _money(risk.spot, f"{who} spot")
    beta = _money(risk.beta, f"{who} beta")
    return delta * spot * beta * _ONE_PCT


def _in_window(d: date, start: date, end: date) -> bool:
    return start <= d <= end


def _window(cand: Candidate, ctx: RailContext) -> tuple[date, date]:
    problem = _context_problem(ctx)
    if problem is not None:
        raise _Missing(problem)
    if cand.planned_exit is None:
        raise _Missing("planned exit missing")
    return ctx.session, cand.planned_exit


Outcome = tuple[Status, str]
Rule = Callable[[Candidate, BookView | None, RailContext, RailLimits], Outcome]


# ------------------------------------------------------------------ rules


def _paper_only(
    cand: Candidate, book: BookView | None, ctx: RailContext, lim: RailLimits
) -> Outcome:
    mode = ctx.account_mode
    if not mode:
        return NOT_EVALUABLE, "account mode missing"
    if mode == "paper":
        return PASS, "paper account"
    return FAIL, f"account mode {mode!r}: the desk trades paper only"


def _defined_risk(
    cand: Candidate, book: BookView | None, ctx: RailContext, lim: RailLimits
) -> Outcome:
    problem = _context_problem(ctx)
    if problem is not None:
        raise _Missing(problem)
    price = _money(cand.entry_price, "entry price")
    if cand.planned_exit is None:
        raise _Missing("planned exit missing")
    try:
        struct = LegStructure(
            id=cand.id or "candidate",
            underlying=cand.underlying,
            kind=cand.kind,
            legs=tuple(
                Leg(right=g.right, action=g.action, strike=g.strike, expiry=g.expiry)
                for g in cand.legs
            ),
            quantity=cand.quantity,
            entry_date=ctx.session,
            exit_deadline=cand.planned_exit,
            limit=price,
            exits=ExitRules(touch=False, breach=False),
        )
    except ValueError as exc:  # pydantic's ValidationError is a ValueError
        first = str(exc).strip().splitlines()
        why = " ".join(first[:3])[:300]
        return FAIL, f"not a defined-risk {cand.kind}: {why}"
    stated = _money(cand.max_loss_usd, "max loss")
    recomputed = struct.max_loss()
    if stated != recomputed:
        return FAIL, f"stated max loss {_usd(stated)} != recomputed {_usd(recomputed)}"
    return PASS, f"{cand.kind} x{cand.quantity}, max loss {_usd(recomputed)}"


def _max_loss_per_trade(
    cand: Candidate, book: BookView | None, ctx: RailContext, lim: RailLimits
) -> Outcome:
    loss = _money(cand.max_loss_usd, "max loss")
    if loss <= 0:
        raise _Missing(f"max loss {loss} is not positive")
    cap = lim.max_loss_per_trade_usd
    if loss <= cap:
        return PASS, f"max loss {_usd(loss)} <= {_usd(cap)}"
    return FAIL, f"max loss {_usd(loss)} > {_usd(cap)}"


def _max_book_loss(
    cand: Candidate, book: BookView | None, ctx: RailContext, lim: RailLimits
) -> Outcome:
    positions = _book_positions(book)
    loss = _money(cand.max_loss_usd, "max loss")
    held = sum((_money(p.max_loss_usd, f"{p.id} max loss") for p in positions), _ZERO)
    total = held + loss
    cap = lim.max_book_loss_usd
    working = sum(1 for p in positions if p.status != "open")
    what = f"book {_usd(held)} ({len(positions)} positions, {working} working at cap)"
    if total <= cap:
        return PASS, f"{what} + {_usd(loss)} = {_usd(total)} <= {_usd(cap)}"
    return FAIL, f"{what} + {_usd(loss)} = {_usd(total)} > {_usd(cap)}"


def _per_underlying(
    cand: Candidate, book: BookView | None, ctx: RailContext, lim: RailLimits
) -> Outcome:
    positions = _book_positions(book)
    sym = _sym(cand.underlying)
    if not sym:
        raise _Missing("underlying missing")
    same = [p.id for p in positions if _sym(p.underlying) == sym]
    total = len(same) + 1
    cap = lim.max_per_underlying
    held = f"{sym}: {len(same)} held account-wide ({', '.join(same) or 'none'})"
    if total <= cap:
        return PASS, f"{held} + 1 = {total} <= {cap}"
    return FAIL, f"{held} + 1 = {total} > {cap}"


def _net_beta_delta(
    cand: Candidate, book: BookView | None, ctx: RailContext, lim: RailLimits
) -> Outcome:
    positions = _book_positions(book)
    missing = [p.id for p in positions if None in (p.risk.delta_shares, p.risk.spot, p.risk.beta)]
    if missing:
        raise _Missing(f"book positions without delta/spot/beta: {', '.join(missing)}")
    before = sum((_dollars_per_1pct_spy(p.risk, p.id) for p in positions), _ZERO)
    mine = _dollars_per_1pct_spy(cand.risk, "candidate")
    after = before + mine
    cap = lim.max_net_beta_delta_usd_per_1pct_spy
    what = (
        f"net beta delta {_usd(before)} + {_usd(mine)} = {_usd(after)} per 1% SPY"
        f" (cap +/-{_usd(cap)})"
    )
    if abs(after) <= cap:
        return PASS, what
    if abs(after) < abs(before):
        return PASS, what + ": over the cap, but the trade reduces |book delta|"
    return FAIL, what


def _admissions(
    cand: Candidate, book: BookView | None, ctx: RailContext, lim: RailLimits
) -> Outcome:
    prior = _count(ctx.admissions_this_session, "admissions this session")
    cap = lim.max_admissions_per_session
    if prior + 1 <= cap:
        return PASS, f"{prior} admitted this session + 1 <= {cap}"
    return FAIL, f"{prior} admitted this session + 1 > {cap}"


def _quote(g: CandidateLeg, i: int) -> tuple[Decimal, Decimal]:
    bid = _money(g.bid, f"leg {i} bid")
    ask = _money(g.ask, f"leg {i} ask")
    if bid < 0 or ask < bid:
        raise _Missing(f"leg {i} quote {bid}/{ask} crossed or negative")
    return bid, ask


def _roundtrip_cost(
    cand: Candidate, book: BookView | None, ctx: RailContext, lim: RailLimits
) -> Outcome:
    if not cand.legs:
        raise _Missing("no legs")
    qty = _count(cand.quantity, "quantity")
    spreads = _ZERO
    for i, g in enumerate(cand.legs):
        bid, ask = _quote(g, i)
        spreads += ask - bid  # entry half-spread + exit half-spread
    spread_cost = spreads * MULTIPLIER * qty
    commissions = 2 * COMMISSION_PER_CONTRACT_USD * qty * len(cand.legs)
    cost = spread_cost + commissions
    loss = _money(cand.max_loss_usd, "max loss")
    if loss <= 0:
        raise _Missing(f"max loss {loss} is not positive")
    allowed = lim.max_roundtrip_cost_frac_of_max_loss * loss
    what = (
        f"round trip {_usd(cost)} (spreads {_usd(spread_cost)} + commissions"
        f" {_usd(commissions)}) vs {lim.max_roundtrip_cost_frac_of_max_loss} x {_usd(loss)}"
        f" = {_usd(allowed)}"
    )
    return (PASS if cost <= allowed else FAIL), what


def _leg_open_interest(
    cand: Candidate, book: BookView | None, ctx: RailContext, lim: RailLimits
) -> Outcome:
    if not cand.legs:
        raise _Missing("no legs")
    floor = lim.min_leg_open_interest
    low: list[str] = []
    missing: list[str] = []
    for i, g in enumerate(cand.legs):
        try:
            oi = _count(g.open_interest, f"leg {i} open interest")
        except _Missing as exc:
            missing.append(str(exc))
            continue
        if not oi >= floor:
            low.append(f"leg {i} {g.right}{g.strike} OI {oi}")
    if low:
        return FAIL, f"below {floor}: {', '.join(low)}"
    if missing:
        raise _Missing("; ".join(missing))
    return PASS, f"every leg OI >= {floor}"


def _leg_spread(
    cand: Candidate, book: BookView | None, ctx: RailContext, lim: RailLimits
) -> Outcome:
    if not cand.legs:
        raise _Missing("no legs")
    limit = lim.max_leg_spread_frac_of_mid
    wide: list[str] = []
    missing: list[str] = []
    for i, g in enumerate(cand.legs):
        try:
            bid, ask = _quote(g, i)
        except _Missing as exc:
            missing.append(str(exc))
            continue
        mid = (bid + ask) / 2
        if mid <= 0:
            missing.append(f"leg {i} has no market (mid {mid})")
            continue
        frac = (ask - bid) / mid
        if frac > limit:
            wide.append(f"leg {i} {g.right}{g.strike} {frac:.4f}")
    if wide:
        return FAIL, f"spread/mid above {limit}: {', '.join(wide)}"
    if missing:
        raise _Missing("; ".join(missing))
    return PASS, f"every leg spread/mid <= {limit}"


def _earnings(cand: Candidate, book: BookView | None, ctx: RailContext, lim: RailLimits) -> Outcome:
    if cand.kind not in CREDIT_KINDS:
        return PASS, f"{cand.kind} is not short premium"
    start, end = _window(cand, ctx)
    if cand.earnings is None:
        raise _Missing(f"no earnings source for {cand.underlying} (short premium)")
    hits = [e for e in cand.earnings if _in_window(e.date, start, end)]
    if hits:
        dates = ", ".join(f"{e.date} ({e.status}, {e.timing})" for e in hits)
        return FAIL, f"short premium held across earnings in [{start}, {end}]: {dates}"
    return PASS, f"no earnings in [{start}, {end}] ({len(cand.earnings)} known)"


def _ex_dividend(
    cand: Candidate, book: BookView | None, ctx: RailContext, lim: RailLimits
) -> Outcome:
    short_calls = [g for g in cand.legs if g.right == "C" and g.action == "SELL"]
    if not short_calls:
        return PASS, "no short call"
    start, end = _window(cand, ctx)
    if cand.ex_dividends is None:
        raise _Missing(f"no dividend data for {cand.underlying} (short call)")
    hits = [x for x in cand.ex_dividends if _in_window(x.ex_date, start, end)]
    if hits:
        dates = ", ".join(f"{x.ex_date} ({x.status})" for x in hits)
        return FAIL, f"short call held across ex-dividend in [{start}, {end}]: {dates}"
    return PASS, f"no ex-dividend in [{start}, {end}]"


def _long_single_delta(
    cand: Candidate, book: BookView | None, ctx: RailContext, lim: RailLimits
) -> Outcome:
    if cand.kind != "long_single":
        return PASS, f"{cand.kind} is not a long single"
    if len(cand.legs) != 1:
        raise _Missing(f"long single with {len(cand.legs)} legs")
    delta = _money(cand.legs[0].delta, "leg delta")
    floor = lim.min_long_single_abs_delta
    if abs(delta) >= floor:
        return PASS, f"|delta| {abs(delta)} >= {floor}"
    return FAIL, f"|delta| {abs(delta)} < {floor}: no lottery tickets"


def _fresh_chain(
    cand: Candidate, book: BookView | None, ctx: RailContext, lim: RailLimits
) -> Outcome:
    problem = _context_problem(ctx)
    if problem is not None:
        raise _Missing(problem)
    chain = cand.chain_session
    if chain is None:
        raise _Missing("chain session missing")
    as_of = cand.chain_as_of
    if as_of is None:
        raise _Missing("chain as-of missing")
    if not isinstance(as_of, datetime) or as_of.tzinfo is None:
        raise _Missing("chain as-of is not an aware datetime")
    if as_of > ctx.now:
        raise _Missing(f"chain stamped {as_of.isoformat()} after now")
    if as_of.astimezone(ET).date() < chain:
        raise _Missing(f"chain stamped {as_of.isoformat()} before its session {chain}")
    ci = _session_index(ctx.calendar, chain)
    si = _session_index(ctx.calendar, ctx.session)
    if ci is None:
        raise _Missing(f"chain session {chain} is not an NYSE session")
    assert si is not None  # _context_problem checked it
    age = si - ci
    if age < 0:
        raise _Missing(f"chain session {chain} is after the entry session {ctx.session}")
    cap = lim.max_chain_age_sessions
    what = f"chain {chain} is {age} session(s) before entry {ctx.session}"
    if age <= cap:
        return PASS, f"{what} (<= {cap})"
    return FAIL, f"{what} (> {cap}): stale"


def _book_short_vega(
    cand: Candidate, book: BookView | None, ctx: RailContext, lim: RailLimits
) -> Outcome:
    positions = _book_positions(book)
    missing = [p.id for p in positions if p.risk.vega_usd_per_volpt is None]
    if missing:
        raise _Missing(f"book positions without vega: {', '.join(missing)}")
    before = sum((_money(p.risk.vega_usd_per_volpt, p.id) for p in positions), _ZERO)
    mine = _money(cand.risk.vega_usd_per_volpt, "candidate vega")
    after = before + mine
    cap = lim.max_book_short_vega_usd_per_volpt
    what = (
        f"net vega {_usd(before)} + {_usd(mine)} = {_usd(after)} per vol pt (short cap {_usd(cap)})"
    )
    if after >= -cap:
        return PASS, what
    if after > before:
        return PASS, what + ": over the cap, but the trade reduces the short"
    return FAIL, what


def _news_veto(
    cand: Candidate, book: BookView | None, ctx: RailContext, lim: RailLimits
) -> Outcome:
    v = cand.news_veto
    if v is None:
        return PASS, "no news veto source configured"
    if not v.available:
        raise _Missing(f"news veto source {v.source!r} unavailable")
    if v.veto:
        return FAIL, f"vetoed by {v.source}: {v.reason or 'no reason given'}"
    return PASS, f"{v.source}: no veto"


_RULE_FNS: tuple[tuple[str, Rule], ...] = (
    ("paper_only", _paper_only),
    ("defined_risk", _defined_risk),
    ("max_loss_per_trade", _max_loss_per_trade),
    ("max_book_loss", _max_book_loss),
    ("per_underlying", _per_underlying),
    ("net_beta_delta", _net_beta_delta),
    ("admissions_per_session", _admissions),
    ("roundtrip_cost", _roundtrip_cost),
    ("leg_open_interest", _leg_open_interest),
    ("leg_spread", _leg_spread),
    ("earnings_short_premium", _earnings),
    ("ex_dividend_short_call", _ex_dividend),
    ("long_single_delta", _long_single_delta),
    ("fresh_chain", _fresh_chain),
    ("book_short_vega", _book_short_vega),
    ("news_veto", _news_veto),
)
assert tuple(name for name, _ in _RULE_FNS) == RULES


def _evaluate(
    fn: Rule, cand: Candidate, book: BookView | None, ctx: RailContext, lim: RailLimits
) -> Outcome:
    try:
        return fn(cand, book, ctx, lim)
    except _Missing as exc:
        return NOT_EVALUABLE, str(exc)
    except (ArithmeticError, ValueError, TypeError, AttributeError, KeyError) as exc:
        return NOT_EVALUABLE, f"error: {type(exc).__name__}: {exc}"


def check(
    candidate: Candidate, book: BookView | None, context: RailContext, limits: RailLimits
) -> RailReport:
    """Every rule of :data:`RULES`, in order, for one candidate (pure)."""
    results = tuple(
        RuleResult(name, *_evaluate(fn, candidate, book, context, limits)) for name, fn in _RULE_FNS
    )
    return RailReport(candidate_id=candidate.id, results=results)
