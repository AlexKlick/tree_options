"""Value a multi-leg structure at the planned exit along each path (plan D6).

Input: a :class:`tree_options.trex.plan.LegStructure` (long_single,
debit_vertical, credit_vertical, iron_condor, calendar, diagonal), the
name's :class:`desk.distribution.PathSet` and the entry-day surface from
D's recorded chain (:class:`EntrySurface`, read through ``desk.pit``).

Model (every choice declared, none fitted here):

* entry at D's marks: each leg at its own quote, mid +- FILL_K x its
  half-spread (buy at mid + k hs, sell at mid - k hs), k = 0.5; the stress
  case k = 1.0 (the touch). The package is in its DEBIT ORIENTATION
  (``LegStructure.package_legs``): a debit kind pays mid0 + k hs0, a credit
  kind receives mid0 - k hs0.
* exit on each path at the planned exit session: every remaining leg
  repriced by Black-Scholes (q = 0; the rate is the paths' DTB3), IV held
  at the same moneyness m = ln(K/S) on the entry day's smile of that leg's
  own expiry and right (sticky moneyness; linear in m between quoted
  strikes, flat beyond them), times the ATM mean-reversion factor
  (:func:`iv_mean_reversion`). The half-spread at exit is the entry day's
  at the same moneyness, same interpolation. Sale proceeds of a debit
  package are floored at 0 (a worthless package is abandoned, never paid to
  close); the cost to close a credit package is capped at its width (the
  engine's BUY-to-close cap). Floor or cap binding = max loss.
* IV per leg comes from its OWN mid (``desk.surface.chain_quotes``, the
  hash-pinned ``bs_price`` inverted at the paths' rate), so the model
  reprices every leg to its mid at entry; a runtime guard checks that with
  the pinned scalar ``bs_price`` before any path is valued. The path
  repricing is a vectorized twin of that function (same formula, same
  half-day floor; a test pins it to 1e-12).
* $0.65 per contract per leg on entry and on exit.
* reported, money as Decimal (cents): EV (no-view paths), its Monte Carlo
  standard error, EV / max loss (``LegStructure.max_loss()``), probability
  of profit and of max loss, CVaR 5% (mean of the worst ceil(5% n) P&Ls),
  signal EV (the view's paths, same draws), and the knobs on the no-view
  paths: fill stress (k = 1.0), IV x 1.2 and x 0.8 at exit, a -2% crash day
  (every exit spot x 0.98), and the stress case: IV x 1.2 plus the crash.
* refused (``NotEvaluable``): a leg expiring on or before the planned exit
  (the engine closes before the first expiry), a planned exit after the
  structure's own exit deadline, a leg without a two-sided quote or IV.
"""

from __future__ import annotations

import bisect
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, cast

import numpy as np

from tree_options.desk import stats, surface
from tree_options.desk.distribution import (
    BLOCK_SESSIONS,
    HAR_H,
    N_PATHS,
    EventSlot,
    PathSet,
    SignalDrift,
    build_paths,
)
from tree_options.desk.ivhist import days_between
from tree_options.desk.pit import NotEvaluable, PointInTime
from tree_options.desk.sessions import Calendar
from tree_options.synth_options.greeks import CallPut, bs_price
from tree_options.trex.plan import Leg, LegStructure

FILL_K = 0.5
STRESS_FILL_K = 1.0
COMMISSION = Decimal("0.65")  # per contract per leg, each way
IV_SHOCK = 0.20
CRASH = -0.02
CVAR_ALPHA = 0.05
MULTIPLIER = 100
DIVIDEND_YIELD = 0.0
MIN_T_DAYS = 0.5  # greeks.bs_price floors T at half a day
# ATM mean reversion (declared): the exit IV level moves from today's toward
# the name's own trailing-year median, measured on the IV history (a ratio,
# so the VWAP-vs-mid construction offset cancels), closing half the gap every
# MR_HALF_LIFE sessions (a daily AR(1) coefficient near 0.977).
MR_HALF_LIFE = 30
MR_WINDOW = 252
MR_MIN_N = 120
MR_MAX_STALE = 5  # the current IV must be among the last 5 sessions up to D
MR_CLAMP = (0.5, 2.0)
INTEGRITY_TOL = 1e-9
MID_TOL = 1e-6

_CENT = Decimal("0.01")
_PRICE = Decimal("0.000001")
_RATIO = Decimal("0.0001")
_erf = np.frompyfunc(math.erf, 1, 1)


class ModelIntegrityError(RuntimeError):
    """The vectorized pricer or a solved IV disagrees with the pinned pricer."""


# ------------------------------------------------------------------ pricer


def _norm_cdf(x: np.ndarray) -> np.ndarray:
    # math.erf elementwise: exactly the function greeks.norm_cdf uses
    return 0.5 * (1.0 + np.asarray(_erf(x / math.sqrt(2.0)), dtype=np.float64))


def bs_price_vec(
    *,
    spot: np.ndarray,
    strike: float,
    dte_calendar_days: int,
    iv: np.ndarray,
    risk_free: float,
    dividend_yield: float,
    call_put: str,
) -> np.ndarray:
    """``greeks.bs_price`` over arrays of spots and IVs (one contract)."""
    s = np.asarray(spot, dtype=np.float64)
    v = np.asarray(iv, dtype=np.float64)
    if strike <= 0.0 or np.any(s <= 0.0):
        raise ValueError(f"spot and strike must be positive: {strike=}")
    if np.any(v <= 0.0):
        raise ValueError("iv must be positive")
    t_years = max(float(dte_calendar_days), MIN_T_DAYS) / 365.0
    vol = v * math.sqrt(t_years)
    d1 = (np.log(s / strike) + (risk_free - dividend_yield + 0.5 * v * v) * t_years) / vol
    d2 = d1 - vol
    disc_q = math.exp(-dividend_yield * t_years)
    disc_r = math.exp(-risk_free * t_years)
    if call_put == "C":
        price = s * disc_q * _norm_cdf(d1) - strike * disc_r * _norm_cdf(d2)
    else:
        price = strike * disc_r * _norm_cdf(-d2) - s * disc_q * _norm_cdf(-d1)
    return cast(np.ndarray, np.maximum(price, 0.0))


# ----------------------------------------------------------------- surface


@dataclass(frozen=True)
class Nodes:
    """y over m = ln(K/S0), linear between nodes, flat beyond."""

    m: np.ndarray
    y: np.ndarray

    def at(self, m: np.ndarray) -> np.ndarray:
        return cast(np.ndarray, np.interp(m, self.m, self.y))


@dataclass(frozen=True)
class LegQuote:
    bid: float
    ask: float
    mid: float
    half_spread: float
    iv: float
    dte: int


class EntrySurface:
    """D's recorded chain as the entry marks and the exit smile/spreads.
    IVs are solved lazily, per expiry, at ``rate`` (the paths' DTB3)."""

    def __init__(self, doc: Mapping[str, Any], *, session: date, rate: float) -> None:
        spot = surface.chain_spot(doc)
        if spot is None:
            raise NotEvaluable("chain has no underlying close")
        self.doc = doc
        self.session = session
        self.rate = rate
        self.spot = spot
        self._quotes: dict[date, list[surface.Quote]] = {}
        self._nodes: dict[tuple[str, date, str], Nodes] = {}

    def quotes(self, expiry: date) -> list[surface.Quote]:
        if expiry not in self._quotes:
            cols = self.doc["columns"]
            want = expiry.isoformat()
            idx = [i for i, e in enumerate(cols["exp"]) if e == want]
            keys = ("occ", "exp", "right", "strike", "bid", "ask", "oi")
            sub = {"columns": {k: [cols[k][i] for i in idx] for k in keys}}
            self._quotes[expiry] = surface.chain_quotes(
                sub, session=self.session, spot=self.spot, rate=self.rate
            )
        return self._quotes[expiry]

    def leg_quote(self, leg: Leg) -> LegQuote:
        k = float(leg.strike)
        where = f"{leg.right} {leg.strike} {leg.expiry}"
        for q in self.quotes(leg.expiry):
            if q.right == leg.right and q.strike == k:
                break
        else:
            raise NotEvaluable(f"no quote for {where} in the {self.session} chain")
        if q.mid is None or q.bid is None or q.ask is None:
            raise NotEvaluable(f"no two-sided quote for {where}")
        if q.iv is None:
            raise NotEvaluable(f"no implied vol for the quote of {where}")
        return LegQuote(q.bid, q.ask, q.mid, (q.ask - q.bid) / 2.0, q.iv, q.dte)

    def _build(self, kind: str, expiry: date, right: str) -> Nodes:
        key = (kind, expiry, right)
        if key not in self._nodes:
            pts = []
            for q in self.quotes(expiry):
                if q.right != right or q.mid is None or q.bid is None or q.ask is None:
                    continue
                y = q.iv if kind == "iv" else (q.ask - q.bid) / 2.0
                if y is not None:
                    pts.append((math.log(q.strike / self.spot), y))
            if not pts:
                raise NotEvaluable(f"no {kind} quote nodes for {right} {expiry}")
            pts.sort()
            self._nodes[key] = Nodes(
                np.array([p[0] for p in pts], dtype=np.float64),
                np.array([p[1] for p in pts], dtype=np.float64),
            )
        return self._nodes[key]

    def smile(self, expiry: date, right: str) -> Nodes:
        return self._build("iv", expiry, right)

    def spreads(self, expiry: date, right: str) -> Nodes:
        return self._build("half_spread", expiry, right)


# -------------------------------------------------------- mean reversion


@dataclass(frozen=True)
class IvMeanReversion:
    factor: float  # multiplies every exit IV
    status: str  # applied | unavailable | off
    reason: str
    ratio: float | None = None  # trailing median / current (clamped)
    n: int = 0
    half_life: int = MR_HALF_LIFE

    @classmethod
    def off(cls, reason: str = "not applied") -> IvMeanReversion:
        return cls(1.0, "off", reason)

    def as_dict(self) -> dict[str, Any]:
        return {
            "factor": self.factor,
            "status": self.status,
            "reason": self.reason,
            "ratio": self.ratio,
            "n": self.n,
            "half_life": self.half_life,
        }


def iv_mean_reversion(
    history: Mapping[date, tuple[float, str]],
    session: date,
    n_steps: int,
    cal: Calendar,
    *,
    half_life: int = MR_HALF_LIFE,
) -> IvMeanReversion:
    """factor = 1 + (rho - 1)(1 - 2^(-n_steps / half_life)), rho = the
    trailing-MR_WINDOW median IV30 over the current one (clamped to
    MR_CLAMP), from the history up to ``session`` only. Unavailable
    (factor 1) with fewer than MR_MIN_N values or a stale current value."""
    sessions = cal.sessions()
    i = bisect.bisect_right(sessions, session)
    window = sessions[max(0, i - MR_WINDOW) : i]
    vals = [(d, float(history[d][0])) for d in window if d in history]
    n = len(vals)

    def unavailable(why: str) -> IvMeanReversion:
        return IvMeanReversion(1.0, "unavailable", why, None, n, half_life)

    if n < MR_MIN_N:
        return unavailable(f"{n} IV-history sessions in the trailing {MR_WINDOW} < {MR_MIN_N}")
    last, current = vals[-1]
    if last < window[-MR_MAX_STALE] or not current > 0.0:
        return unavailable(f"no IV-history value in the last {MR_MAX_STALE} sessions (last {last})")
    ratio = min(max(stats.median([v for _d, v in vals]) / current, MR_CLAMP[0]), MR_CLAMP[1])
    factor = 1.0 + (ratio - 1.0) * (1.0 - 0.5 ** (n_steps / half_life))
    return IvMeanReversion(factor, "applied", "", ratio, n, half_life)


# --------------------------------------------------------------- valuation


def _money(x: float) -> Decimal:
    return Decimal(repr(float(x))).quantize(_CENT, rounding=ROUND_HALF_UP)


def _price(x: float) -> Decimal:
    return Decimal(repr(float(x))).quantize(_PRICE, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class Valuation:
    structure_id: str
    underlying: str
    kind: str
    quantity: int
    session: date
    exit_session: date
    n_paths: int
    spot: float
    entry_mid: Decimal  # package mid, debit orientation, per package
    entry_fill: Decimal  # paid (debit kinds) or received (credit kinds), per package
    limit_ok: bool  # the modeled fill is within the structure's cap / floor
    max_loss: Decimal  # LegStructure.max_loss(), dollars
    ev: Decimal
    ev_se: Decimal
    ev_signal: Decimal | None
    ev_per_max_loss: Decimal
    p_profit: float
    p_max_loss: float
    cvar5: Decimal
    ev_fill_stress: Decimal
    ev_iv_up: Decimal
    ev_iv_down: Decimal
    ev_crash: Decimal
    ev_stress: Decimal
    round_trip_cost: Decimal  # expected spread cost both ways + commissions
    iv_mean_reversion: IvMeanReversion
    events: tuple[EventSlot, ...]
    assumptions: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        def s(x: Decimal | None) -> str | None:
            return None if x is None else str(x)

        return {
            "structure_id": self.structure_id,
            "underlying": self.underlying,
            "kind": self.kind,
            "quantity": self.quantity,
            "session": self.session.isoformat(),
            "exit_session": self.exit_session.isoformat(),
            "n_paths": self.n_paths,
            "spot": self.spot,
            "entry_mid": s(self.entry_mid),
            "entry_fill": s(self.entry_fill),
            "limit_ok": self.limit_ok,
            "max_loss": s(self.max_loss),
            "ev": s(self.ev),
            "ev_se": s(self.ev_se),
            "ev_signal": s(self.ev_signal),
            "ev_per_max_loss": s(self.ev_per_max_loss),
            "p_profit": self.p_profit,
            "p_max_loss": self.p_max_loss,
            "cvar5": s(self.cvar5),
            "ev_fill_stress": s(self.ev_fill_stress),
            "ev_iv_up": s(self.ev_iv_up),
            "ev_iv_down": s(self.ev_iv_down),
            "ev_crash": s(self.ev_crash),
            "ev_stress": s(self.ev_stress),
            "round_trip_cost": s(self.round_trip_cost),
            "iv_mean_reversion": self.iv_mean_reversion.as_dict(),
            "events": [
                {
                    "report": e.report.isoformat(),
                    "status": e.status,
                    "timing": e.timing,
                    "steps": list(e.steps),
                }
                for e in self.events
            ],
            "assumptions": dict(self.assumptions),
        }


def _check_integrity(legs: Sequence[Leg], quotes: Sequence[LegQuote], surf: EntrySurface) -> None:
    """The pinned scalar pricer reprices each leg's mid at its IV, and the
    vectorized twin agrees with it."""
    for g, q in zip(legs, quotes, strict=True):
        right = cast(CallPut, g.right)
        k = float(g.strike)
        scalar = bs_price(
            spot=surf.spot,
            strike=k,
            dte_calendar_days=q.dte,
            iv=q.iv,
            risk_free=surf.rate,
            dividend_yield=DIVIDEND_YIELD,
            call_put=right,
        )
        vec = bs_price_vec(
            spot=np.array([surf.spot]),
            strike=k,
            dte_calendar_days=q.dte,
            iv=np.array([q.iv]),
            risk_free=surf.rate,
            dividend_yield=DIVIDEND_YIELD,
            call_put=right,
        )[0]
        if abs(scalar - vec) > INTEGRITY_TOL * max(1.0, scalar):
            raise ModelIntegrityError(f"vectorized pricer {vec} != bs_price {scalar} for {g}")
        if abs(scalar - q.mid) > MID_TOL * max(1.0, q.mid):
            raise ModelIntegrityError(f"IV {q.iv} reprices {scalar}, not the mid {q.mid} of {g}")


def price_structure(
    struct: LegStructure,
    paths: PathSet,
    surf: EntrySurface,
    *,
    mr: IvMeanReversion,
    fill_k: float = FILL_K,
    stress_fill_k: float = STRESS_FILL_K,
    commission: Decimal = COMMISSION,
) -> Valuation:
    spec = paths.spec
    if struct.underlying != spec.name:
        raise ValueError(f"{struct.id}: underlying {struct.underlying} != the paths' {spec.name}")
    if surf.session != spec.session or surf.rate != spec.rate:
        raise ValueError(f"{struct.id}: the surface and the paths disagree on session or rate")
    if spec.exit_session >= struct.first_expiry:
        raise NotEvaluable(
            f"{struct.id}: a leg expires {struct.first_expiry}, on or before the planned exit "
            f"{spec.exit_session} (the engine closes before the first expiry)"
        )
    if spec.exit_session > struct.exit_deadline:
        raise NotEvaluable(
            f"{struct.id}: the planned exit {spec.exit_session} is after the structure's "
            f"exit deadline {struct.exit_deadline}"
        )
    legs = struct.package_legs()
    quotes = [surf.leg_quote(g) for g in legs]
    _check_integrity(legs, quotes, surf)
    signs = [float(g.sign) for g in legs]
    mid0 = sum(sg * q.mid for sg, q in zip(signs, quotes, strict=True))
    hs0 = sum(q.half_spread for q in quotes)
    if not mid0 > 0.0:
        raise NotEvaluable(f"{struct.id}: package mid {mid0:.4f} is not positive")
    credit = struct.is_credit
    width = float(struct.width) if struct.width is not None else math.inf
    if credit and not mid0 - fill_k * hs0 > 0.0:
        raise NotEvaluable(f"{struct.id}: no credit left after the modeled fill")
    per_package = MULTIPLIER * struct.quantity
    comm = float(commission) * len(legs) * struct.quantity * 2
    exit_dte = [days_between(spec.exit_session, g.expiry) for g in legs]
    smiles = [surf.smile(g.expiry, g.right) for g in legs]
    spreads = [surf.spreads(g.expiry, g.right) for g in legs]

    def exit_marks(log_s: np.ndarray, iv_mult: float) -> tuple[np.ndarray, np.ndarray]:
        s = surf.spot * np.exp(log_s)
        mid = np.zeros_like(s)
        hs = np.zeros_like(s)
        for g, sg, dte, sm, sp in zip(legs, signs, exit_dte, smiles, spreads, strict=True):
            m = np.log(float(g.strike) / s)
            iv = sm.at(m) * (mr.factor * iv_mult)
            px = bs_price_vec(
                spot=s,
                strike=float(g.strike),
                dte_calendar_days=dte,
                iv=iv,
                risk_free=spec.rate,
                dividend_yield=DIVIDEND_YIELD,
                call_put=g.right,
            )
            mid += sg * px
            hs += sp.at(m)
        return mid, hs

    def pnl(mid: np.ndarray, hs: np.ndarray, k: float) -> tuple[np.ndarray, np.ndarray]:
        if credit:
            gross = mid + k * hs
            per = (mid0 - k * hs0) - np.clip(gross, 0.0, width)
            hit = gross >= width
        else:
            net = mid - k * hs
            per = np.maximum(net, 0.0) - (mid0 + k * hs0)
            hit = net <= 0.0
        return per * per_package - comm, hit

    base = paths.terminal()
    crash = base + math.log1p(CRASH)
    mid, hs = exit_marks(base, 1.0)
    p, hit = pnl(mid, hs, fill_k)
    n = len(p)

    def ev_of(log_s: np.ndarray, iv_mult: float) -> Decimal:
        return _money(float(pnl(*exit_marks(log_s, iv_mult), fill_k)[0].mean()))

    k_tail = max(1, math.ceil(round(n * CVAR_ALPHA, 9)))
    ev = float(p.mean())
    ev_signal = None
    if paths.signal_offset is not None:
        ev_signal = ev_of(paths.terminal(signal=True), 1.0)
    entry_fill = mid0 - fill_k * hs0 if credit else mid0 + fill_k * hs0
    limit = float(struct.limit)
    max_loss = struct.max_loss()
    return Valuation(
        structure_id=struct.id,
        underlying=struct.underlying,
        kind=struct.kind,
        quantity=struct.quantity,
        session=spec.session,
        exit_session=spec.exit_session,
        n_paths=n,
        spot=surf.spot,
        entry_mid=_price(mid0),
        entry_fill=_price(entry_fill),
        limit_ok=entry_fill >= limit if credit else entry_fill <= limit,
        max_loss=max_loss.quantize(_CENT),
        ev=_money(ev),
        ev_se=_money(float(p.std(ddof=1)) / math.sqrt(n) if n > 1 else 0.0),
        ev_signal=ev_signal,
        ev_per_max_loss=Decimal(repr(ev / float(max_loss))).quantize(
            _RATIO, rounding=ROUND_HALF_UP
        ),
        p_profit=float((p > 0.0).mean()),
        p_max_loss=float(hit.mean()),
        cvar5=_money(float(np.sort(p)[:k_tail].mean())),
        ev_fill_stress=_money(float(pnl(mid, hs, stress_fill_k)[0].mean())),
        ev_iv_up=ev_of(base, 1.0 + IV_SHOCK),
        ev_iv_down=ev_of(base, 1.0 - IV_SHOCK),
        ev_crash=ev_of(crash, 1.0),
        ev_stress=ev_of(crash, 1.0 + IV_SHOCK),
        round_trip_cost=_money((fill_k * (hs0 + float(hs.mean()))) * per_package + comm),
        iv_mean_reversion=mr,
        events=spec.events,
        assumptions={
            "fill_k": fill_k,
            "stress_fill_k": stress_fill_k,
            "commission_per_contract": str(commission),
            "iv_shock": IV_SHOCK,
            "crash": CRASH,
            "cvar_alpha": CVAR_ALPHA,
            "dividend_yield": DIVIDEND_YIELD,
            "rate": spec.rate,
            "block": spec.block,
            "har_h": spec.har_h,
            "iv_model": "sticky moneyness per expiry and right x ATM mean reversion",
            "paths": spec.as_dict(),
        },
    )


# -------------------------------------------------------------- candidates


@dataclass(frozen=True)
class CandidateResult:
    structure_id: str
    valuation: Valuation | None  # None: refused, see reason
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "structure_id": self.structure_id,
            "valuation": self.valuation.as_dict() if self.valuation is not None else None,
            "reason": self.reason,
        }


def value_candidates(
    pit: PointInTime,
    name: str,
    structures: Sequence[LegStructure],
    exit_session: date,
    *,
    row_id: str,
    signal: SignalDrift | None = None,
    n_paths: int = N_PATHS,
    block: int = BLOCK_SESSIONS,
    har_h: int = HAR_H,
) -> list[CandidateResult]:
    """Every candidate of one (name, playbook row) valued on ONE path set
    and ONE entry surface, in input order; a refusal carries its reason."""
    try:
        doc = pit.chain(name)
        paths = build_paths(
            pit,
            name,
            exit_session,
            row_id=row_id,
            signal=signal,
            n_paths=n_paths,
            block=block,
            har_h=har_h,
        )
        surf = EntrySurface(doc, session=pit.session, rate=paths.spec.rate)
    except NotEvaluable as exc:
        return [CandidateResult(s.id, None, str(exc)) for s in structures]
    mr = iv_mean_reversion(pit.iv_history(name), pit.session, len(paths.spec.steps), pit.cal)
    out = []
    for s in structures:
        try:
            out.append(CandidateResult(s.id, price_structure(s, paths, surf, mr=mr), ""))
        except NotEvaluable as exc:
            out.append(CandidateResult(s.id, None, str(exc)))
    return out
