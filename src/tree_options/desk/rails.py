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
``[limits]`` table, which may only be equal to or STRICTER than the
binding :data:`OPERATOR_LIMITS`, checked on every construction):

- ``paper_only``: the account mode is "paper".
- ``defined_risk``: the legs, kind, quantity and entry price form a valid
  E1 :class:`~tree_options.trex.plan.LegStructure` (every SELL covered, the
  kind's shape, planned exit before the first expiry), and its recomputed
  max loss equals the candidate's stated one.
- ``max_loss_per_trade``: stated max loss <= the per-trade cap.
- ``max_book_loss``: the book's max loss (working entries at their cap,
  as the book view states them) + this trade <= the book cap. A negative
  position loss is refused (it would manufacture headroom).
- ``per_underlying``: positions on this underlying held ACCOUNT-WIDE (the
  legacy trex book included) + 1 <= the cap.
- ``net_beta_delta``: net beta-weighted delta, in $ of P&L per 1% SPY move
  (sum of delta_shares x spot x beta x 0.01), after the trade within
  +/- the cap; a trade that strictly reduces |book delta| always passes.
  Spot must be strictly positive everywhere (a zero spot would erase a
  position's delta); beta and delta may be zero or negative. The beta the
  caller supplies is the Blume-adjusted one (:mod:`tree_options.desk.beta`,
  0.67 x raw + 0.33); ``beta_raw`` is shown beside it in the detail.
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
- ``ex_dividend_short_call`` (main-session ruling (a), 2026-09-23): an
  ex-dividend (declared: one day; projected: its uncertainty interval)
  overlapping [entry session, planned exit] blocks a short call only when
  that call is EXPOSED at entry: in the money or within
  :data:`EXDIV_NEAR_MONEY_FRAC` of it (strike <= spot x 1.02) AND its
  extrinsic value (mid - max(spot - strike, 0)) is below the dividend +
  :data:`EXDIV_EXTRINSIC_BUFFER_USD`. A short call that is not exposed
  passes, and the detail says why. This relaxed rail relies on the engine
  exit rule (carry-forward) that closes any short call ITM with extrinsic
  below the dividend on the session before the ex-date: it must not go
  live without that rule. No dividend data fails closed only for
  structures with a short call; an unknown dividend amount fails closed
  for a near-the-money one.
- ``long_single_delta``: a long single's |leg delta| >= the floor. The leg
  delta must be a per-share delta of the right sign (a call's in [0, 1], a
  put's in [-1, 0]) and reconcile with the position's delta_shares
  (leg delta x 100 x quantity, within :data:`DELTA_RECONCILE_TOLERANCE`
  per share): a unit error is NOT_EVALUABLE, never a pass.
- ``fresh_chain``: the chain's session is at most
  ``max_chain_age_sessions`` NYSE sessions before the ENTRY session (with 1:
  the latest completed session, for the miner's evening run and for the
  runtime's morning re-check alike), and its as-of stamp is aware, not
  before its session's date and not after ``now``.
- ``book_short_vega`` (main-session ruling (b)): NET book vega (long vega
  on one name offsets short vega on another) after the trade >= -cap ($ per
  vol point); a trade that strictly reduces the net short always passes.
- ``news_veto``: None means no veto source is configured (PASS, said so);
  a configured source must answer with real booleans: unavailable, or an
  answer missing, is NOT_EVALUABLE; a veto FAILs.

Units the greeks lane (``trex/greeks.py`` ``StructureGreeks``: floats
``delta_shares``, ``gamma_shares_per_usd``, ``vega_usd_per_volpt``,
``theta_usd_per_day``) supplies: WHOLE-POSITION, signed (x100 multiplier x
quantity, each leg +1 BUY / -1 SELL). :meth:`PositionRisk.from_greeks`
turns them into the rails' Decimals. Spot and beta are per share.
"""

from __future__ import annotations

import bisect
import math
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
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

# Ex-dividend exposure (main-session ruling (a), 2026-09-23; the operator may
# override): a short call is "near the money" when its strike is at most
# 2% above spot, and exposed when, near the money, its extrinsic value is
# below the dividend plus this per-share buffer.
EXDIV_NEAR_MONEY_FRAC = Decimal("0.02")
EXDIV_EXTRINSIC_BUFFER_USD = Decimal("0.05")
# A long single's position delta must match leg delta x 100 x quantity to
# within this per-share delta (vendor chain delta vs the engine's own
# Black-Scholes delta differ a little; a unit error differs 100x).
DELTA_RECONCILE_TOLERANCE = Decimal("0.05")


# ------------------------------------------------------------------ limits


class RailLimitsError(ValueError):
    """The ``[limits]`` table is missing, has an unknown or a missing key,
    or a value of the wrong type or range, or one looser than the
    operator's binding value."""


# The BINDING values: the operator's decisions of 2026-09-23 ($500 a trade,
# $5,000 book, 2 per underlying account-wide, $250 per 1% SPY, 3 admissions a
# session, 15% round-trip cost, leg OI >= 100, leg spread <= 10% of mid, long
# single |delta| >= 0.30) plus the rails brief's chain age (1 session) and
# short-vega cap ($100 per vol point). A configured ``max_*`` may be lower,
# a ``min_*`` higher; never the other way.
OPERATOR_LIMITS: Final[Mapping[str, Decimal]] = MappingProxyType(
    {
        "max_loss_per_trade_usd": Decimal("500"),
        "max_book_loss_usd": Decimal("5000"),
        "max_per_underlying": Decimal("2"),
        "max_net_beta_delta_usd_per_1pct_spy": Decimal("250"),
        "max_admissions_per_session": Decimal("3"),
        "max_roundtrip_cost_frac_of_max_loss": Decimal("0.15"),
        "min_leg_open_interest": Decimal("100"),
        "max_leg_spread_frac_of_mid": Decimal("0.10"),
        "min_long_single_abs_delta": Decimal("0.30"),
        "max_chain_age_sessions": Decimal("1"),
        "max_book_short_vega_usd_per_volpt": Decimal("100"),
    }
)

# key -> (kind, lower bound, lower inclusive, upper bound or None): the
# sanity range; the operator bound above is checked on top of it. Money
# and fractions are Decimals (TOML strings); counts are ints (TOML integers).
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
assert frozenset(OPERATOR_LIMITS) == LIMIT_KEYS


def _check_limit(key: str, value: object) -> None:
    kind, lo, lo_inclusive, hi = _LIMIT_SPEC[key]
    if kind == "int":
        if not isinstance(value, int) or isinstance(value, bool):
            raise RailLimitsError(f"[limits] {key}: expected an integer, got {value!r}")
        num = Decimal(value)
    else:
        if not isinstance(value, Decimal) or not value.is_finite():
            raise RailLimitsError(f"[limits] {key}: expected a finite Decimal, got {value!r}")
        num = value
    if num < lo or (num == lo and not lo_inclusive) or (hi is not None and num > hi):
        bound = f"{'>=' if lo_inclusive else '>'} {lo}" + (
            f" and <= {hi}" if hi is not None else ""
        )
        raise RailLimitsError(f"[limits] {key}: {value!r} out of range (must be {bound})")
    binding = OPERATOR_LIMITS[key]
    looser = num > binding if key.startswith("max_") else num < binding
    if looser:
        raise RailLimitsError(
            f"[limits] {key}: {value} is looser than the operator's binding {binding}"
        )


@dataclass(frozen=True)
class RailLimits:
    """The rails' thresholds. Field names are EXACTLY the sealed playbook's
    ``[limits]`` keys (the playbook lane writes the table with them). Every
    construction is validated (types, ranges, and never looser than
    :data:`OPERATOR_LIMITS`), direct or through the loader."""

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

    def __post_init__(self) -> None:
        for key in _LIMIT_SPEC:
            _check_limit(key, getattr(self, key))


def _limit_value(key: str, raw: object) -> Decimal | int:
    """A TOML value parsed by type (ranges are RailLimits' to check):
    counts are TOML integers, money and fractions TOML strings."""
    if _LIMIT_SPEC[key][0] == "int":
        if not isinstance(raw, int) or isinstance(raw, bool):
            raise RailLimitsError(f"[limits] {key}: expected a TOML integer, got {raw!r}")
        return raw
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
    return num


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


class WholePositionGreeks(Protocol):
    """The fields the rails read from the engine lane's ``StructureGreeks``."""

    @property
    def delta_shares(self) -> float: ...
    @property
    def vega_usd_per_volpt(self) -> float: ...


def _greek(value: object) -> Decimal | None:
    """A model float as an exact Decimal of its repr; non-finite -> None."""
    if isinstance(value, float):
        return Decimal(repr(value)) if math.isfinite(value) else None
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    if isinstance(value, int) and not isinstance(value, bool):
        return Decimal(value)
    return None


@dataclass(frozen=True)
class PositionRisk:
    """Position-level risk inputs (see the module docstring for units).
    ``beta`` is the one the rail uses (Blume-adjusted); ``beta_raw`` is the
    raw regression beta, shown in the detail only."""

    delta_shares: Decimal | None = None
    vega_usd_per_volpt: Decimal | None = None
    spot: Decimal | None = None
    beta: Decimal | None = None
    beta_raw: Decimal | None = None

    @classmethod
    def from_greeks(
        cls,
        greeks: WholePositionGreeks | None,
        *,
        spot: Decimal | None,
        beta: Decimal | None,
        beta_raw: Decimal | None = None,
    ) -> PositionRisk:
        """From ``trex.greeks.StructureGreeks`` (whole-position floats); no
        greeks (None: not evaluable) or a non-finite one stays missing."""
        return cls(
            delta_shares=_greek(greeks.delta_shares) if greeks is not None else None,
            vega_usd_per_volpt=_greek(greeks.vega_usd_per_volpt) if greeks is not None else None,
            spot=spot,
            beta=beta,
            beta_raw=beta_raw,
        )


@dataclass(frozen=True)
class ExDividend:
    """An ex-dividend: ``declared`` by the issuer (``earliest`` = ``latest``
    = ``ex_date``), or ``projected`` from the regular schedule, with its
    uncertainty interval [earliest, latest] around the expected ``ex_date``
    (see :mod:`tree_options.desk.dividends`). ``cash_amount`` is per share
    (the last regular amount for a projection); None when unknown."""

    ex_date: date
    status: str  # declared | projected
    earliest: date | None = None  # None: ex_date
    latest: date | None = None  # None: ex_date
    cash_amount: Decimal | None = None
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


def _positive(value: object, what: str) -> Decimal:
    num = _money(value, what)
    if num <= 0:
        raise _Missing(f"{what} {num} is not positive")
    return num


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
    spot = _positive(risk.spot, f"{who} spot")
    beta = _money(risk.beta, f"{who} beta")
    return delta * spot * beta * _ONE_PCT


def _in_window(d: date, start: date, end: date) -> bool:
    return start <= d <= end


def _ex_interval(x: ExDividend) -> tuple[date, date]:
    lo = x.earliest if x.earliest is not None else x.ex_date
    hi = x.latest if x.latest is not None else x.ex_date
    if not all(isinstance(d, date) for d in (lo, hi, x.ex_date)) or not lo <= x.ex_date <= hi:
        raise _Missing(f"ex-dividend {x.ex_date}: incoherent interval [{lo}, {hi}]")
    return lo, hi


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
    losses = [(p.id, _money(p.max_loss_usd, f"{p.id} max loss")) for p in positions]
    negative = [f"{pid} {x}" for pid, x in losses if x < 0]
    if negative:
        raise _Missing(f"negative position max loss (no headroom from it): {', '.join(negative)}")
    held = sum((x for _pid, x in losses), _ZERO)
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
    raw = f", raw {cand.risk.beta_raw}" if cand.risk.beta_raw is not None else ""
    what = (
        f"net beta delta {_usd(before)} + {_usd(mine)} = {_usd(after)} per 1% SPY"
        f" (cap +/-{_usd(cap)}; candidate beta {cand.risk.beta}{raw})"
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
    short_calls = [(i, g) for i, g in enumerate(cand.legs) if g.right == "C" and g.action == "SELL"]
    if not short_calls:
        return PASS, "no short call"
    start, end = _window(cand, ctx)
    if cand.ex_dividends is None:
        raise _Missing(f"no dividend data for {cand.underlying} (short call)")
    hits: list[ExDividend] = []
    for x in cand.ex_dividends:
        lo, hi = _ex_interval(x)
        if lo <= end and hi >= start:  # the ex-date interval overlaps the hold
            hits.append(x)
    if not hits:
        return PASS, f"no ex-dividend in [{start}, {end}]"
    spot = _positive(cand.risk.spot, "candidate spot")
    near_edge = spot * (1 + EXDIV_NEAR_MONEY_FRAC)
    exposed: list[str] = []
    safe: list[str] = []
    for x in hits:
        for i, g in short_calls:
            where = f"leg {i} C{g.strike} over ex {x.ex_date} ({x.status})"
            if g.strike > near_edge:
                safe.append(
                    f"{where}: strike above {near_edge.quantize(_CENT)}, not near the money"
                )
                continue
            bid, ask = _quote(g, i)
            extrinsic = (bid + ask) / 2 - max(spot - g.strike, _ZERO)
            div = _money(x.cash_amount, f"dividend amount of ex {x.ex_date}")
            if div < 0:
                raise _Missing(f"dividend amount {div} of ex {x.ex_date} is negative")
            need = div + EXDIV_EXTRINSIC_BUFFER_USD
            if extrinsic < need:
                exposed.append(f"{where}: extrinsic {extrinsic} < dividend {div} + {need - div}")
            else:
                safe.append(f"{where}: extrinsic {extrinsic} >= dividend {div} + {need - div}")
    if exposed:
        return FAIL, "short call exposed to early assignment: " + "; ".join(exposed)
    return PASS, (
        "short call not exposed at entry: "
        + "; ".join(safe)
        + " (relies on the engine's pre-ex-date short-call exit)"
    )


def _long_single_delta(
    cand: Candidate, book: BookView | None, ctx: RailContext, lim: RailLimits
) -> Outcome:
    if cand.kind != "long_single":
        return PASS, f"{cand.kind} is not a long single"
    if len(cand.legs) != 1:
        raise _Missing(f"long single with {len(cand.legs)} legs")
    g = cand.legs[0]
    delta = _money(g.delta, "leg delta")
    lo, hi = (_ZERO, Decimal(1)) if g.right == "C" else (Decimal(-1), _ZERO)
    if not lo <= delta <= hi:
        raise _Missing(f"leg delta {delta} is impossible for a {g.right} (per share: [{lo}, {hi}])")
    qty = _count(cand.quantity, "quantity")
    if qty < 1:
        raise _Missing("quantity below 1")
    shares = _money(cand.risk.delta_shares, "position delta_shares")
    sign = 1 if g.action == "BUY" else -1
    expected = delta * MULTIPLIER * qty * sign
    tolerance = DELTA_RECONCILE_TOLERANCE * MULTIPLIER * qty
    if abs(shares - expected) > tolerance:
        raise _Missing(
            f"position delta_shares {shares} does not reconcile with leg delta {delta}"
            f" x 100 x {qty} = {expected} (+/- {tolerance})"
        )
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
    if type(v.available) is not bool or type(v.veto) is not bool:
        raise _Missing(
            f"news veto source {v.source!r} answered without booleans"
            f" (available={v.available!r}, veto={v.veto!r})"
        )
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
