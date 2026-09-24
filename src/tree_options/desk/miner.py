"""The deal miner (plan D6): session D's entry queue, point in time.

A decision for session D is taken after D's close and before its cutoff,
the next session's 09:30 ET open (:func:`desk.pit.decision_cutoff`); its
deals are entered on that next session (the ENTRY session, 09:50-11:30 ET
by E6's desk-enter). Everything is read through :class:`desk.pit.PointInTime`
or dated on or before D:

1. conditions per name (:func:`desk.regime.conditions_at`) from the desk
   features of D and its history window, D's signals file, the stored
   VIX/VIX3M, the report schedule known at the cutoff and the sealed macro
   calendar, and the sealed playbook rows each name matches. R1 may match
   while the vol state is NOT_EVALUABLE only during the warm-up
   (:func:`vol_gate`; the reason is logged on every deal). An XSMOM pick
   without an options expression (TQQQ/SQQQ) is logged, never substituted.
2. for each matched (name, row): the row's grid on D's recorded chain
   (:func:`enumerate_row`: expiries by calendar DTE at entry, strikes by
   |delta| from our own chain IVs, shared expiries and strikes by role),
   deduplicated (:func:`dedupe`), capped by the sealed selection file
   (nearest the |delta| range centres first; the rest surfaced as
   ``enumeration_cap``).
3. refusals before any valuation (:class:`Refusal`): a leg without a
   two-sided quote, a debit fill at or above the width, a credit at or
   below 0 or at or above the width, a cap at or above the width
   (:func:`entry_terms`), no hold window (:func:`exit_deadline`), a
   diagonal that is not protective on its ACTUAL strikes
   (:func:`structural_refusal`), a structure the engine's model refuses.
   The limit is the first cent strictly beyond the modeled base fill.
4. valuation (:func:`desk.pricing.value_candidates`, one path set per
   (name, row, exit)): its refusal, or ``limit_ok`` false, is refused.
5. the ONE admission check (:func:`desk.rails.check`) as a PRE-check:
   greeks from :func:`trex.greeks.structure_greeks`, Blume betas
   (:mod:`desk.beta`), dividends (:mod:`desk.dividends`), the earnings known
   at the cutoff, and the account-wide book (:func:`desk.book.load_book`)
   with every position's greeks priced from D's chains.
6. selection by the sealed rules (:mod:`desk.selection`): the ADMISSIBLE
   deals, ranked, within each row's ``max_open``, jointly re-checked, are
   the entry queue; every other candidate is SURFACED with its reasons.

:func:`run_mine` writes ``<queue dir>/<D>.json`` (schema ``trex.deal/1``):
deterministic bytes (path seeds from sha256 of stable inputs, no wall clock
in the payload), money as strings, written once (a completed session is
never rewritten; a differing recomputation is kept beside it as a
conflict) and marked done in ``<state>/stages/<D>/mine.done.json``.
Nothing here places orders.
"""

from __future__ import annotations

import bisect
import hashlib
import itertools
import json
import re
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Any, cast

from tree_options.desk import beta as desk_beta
from tree_options.desk import dividends, paths, pricing, rails, regime, selection, surface
from tree_options.desk import pit as desk_pit
from tree_options.desk.book import load_book
from tree_options.desk.distribution import SignalDrift, schedule_through
from tree_options.desk.events import EARNINGS_NAMES
from tree_options.desk.ivhist import days_between
from tree_options.desk.panel import PanelLocked
from tree_options.desk.pit import NotEvaluable, PointInTime
from tree_options.desk.playbook import (
    Playbook,
    PlaybookError,
    Row,
    load_playbook,
    require_protective,
)
from tree_options.desk.sessions import (
    Calendar,
    cutoff_instant,
    first_session_after,
    latest_completed_session,
)
from tree_options.desk.store import atomic_create_bytes, atomic_write_bytes, atomic_write_json
from tree_options.desk.universe import CHAIN_UNIVERSE
from tree_options.trex.clock import ET
from tree_options.trex.greeks import structure_greeks
from tree_options.trex.plan import CREDIT_KINDS, Action, Leg, LegStructure, Right

SCHEMA = "trex.deal/1"
STAGE = "mine"
CODE = "tree_options.desk.miner"
# the miner pre-checks as the paper desk; desk-enter and the runtime re-check
# with their broker's real account mode
ACCOUNT_MODE = "paper"
# E6 desk-enter admits from the queue 09:50-11:30 ET on the entry session
ADMISSION_END = time(11, 30)
QUANTITY = 1
TWO_EXPIRY = frozenset({"calendar", "diagonal"})
_CENT = Decimal("0.01")
_ZERO = Decimal(0)
_ONE_PCT = Decimal("0.01")
_DEAL_ROW = re.compile(r"^d-\d{8}-([A-Za-z0-9]+)-")

# deal statuses
ADMISSIBLE = "admissible"
REFUSED = "refused"
RAIL_FAILED = "rail_failed"
NOT_SELECTED = "not_selected"
CAPACITY = "capacity"


# ------------------------------------------------------------- enumerate


@dataclass(frozen=True)
class Proto:
    """One enumerated structure: legs in the row's leg order, each with the
    chain quote it was picked from."""

    row_id: str
    name: str
    kind: str
    legs: tuple[Leg, ...]
    quotes: tuple[surface.Quote, ...]
    centrality: float  # sum over |delta|-picked legs of the distance to the range centre

    @property
    def key(self) -> str:
        """The structure's identity (kind and legs, order-free)."""
        parts = sorted(f"{g.expiry.isoformat()}:{g.right}:{g.action}:{g.strike}" for g in self.legs)
        return f"{self.kind}|" + ";".join(parts)


def _quote_at(
    surf: pricing.EntrySurface, expiry: date, right: str, strike: float
) -> surface.Quote | None:
    for q in surf.quotes(expiry):
        if q.right == right and q.strike == strike:
            return q
    return None


def _gap_ok(row: Row, exp: Mapping[str, date]) -> bool:
    """A two-expiry row: the BUY (back) leg expires ``expiry_gap_days``
    after the SELL (front) leg."""
    back = next(g for g in row.legs if g.action == "BUY")
    front = next(g for g in row.legs if g.action == "SELL")
    gap = row.expiry_gap_days
    assert gap is not None  # the playbook loader requires it on two-expiry rows
    days = days_between(exp[front.role], exp[back.role])
    return gap[0] <= days <= gap[1]


def enumerate_row(row: Row, name: str, surf: pricing.EntrySurface, entry: date) -> list[Proto]:
    """Every structure of ``row`` on the chain: each leg stating a DTE range
    takes every chain expiry whose calendar days from ``entry`` fall in it
    (two-expiry rows also need the row's expiry gap), a ``same_as_<role>``
    leg shares that role's expiry; each |delta|-picked leg takes every
    strike of its expiry and right whose OWN delta (our chain IV,
    :func:`desk.surface.chain_quotes`) lies in its range (inclusive), a
    ``same_as_<role>`` strike is that role's strike. Deterministic order:
    expiries, then strikes, ascending. Duplicates are kept (see
    :func:`dedupe`)."""
    expiries = sorted({date.fromisoformat(e) for e in surf.doc["columns"]["exp"]})
    concrete = [g for g in row.legs if g.dte is not None]
    choices = []
    for g in concrete:
        lo, hi = cast(tuple[int, int], g.dte)
        choices.append([e for e in expiries if lo <= days_between(entry, e) <= hi])
    out: list[Proto] = []
    for combo in itertools.product(*choices):
        exp = {g.role: e for g, e in zip(concrete, combo, strict=True)}
        for g in row.legs:
            if g.dte is None:
                exp[g.role] = exp[cast(str, g.same_expiry_as)]
        if row.kind in TWO_EXPIRY and not _gap_ok(row, exp):
            continue
        per_leg: list[list[surface.Quote | None]] = []
        for g in row.legs:
            if g.abs_delta is None:
                per_leg.append([None])  # another leg's strike
                continue
            lo_d, hi_d = float(g.abs_delta[0]), float(g.abs_delta[1])
            picks: list[surface.Quote | None] = [
                q
                for q in surf.quotes(exp[g.role])
                if q.right == g.right
                and q.delta is not None
                and q.mid is not None
                and lo_d <= abs(q.delta) <= hi_d
            ]
            per_leg.append(sorted(picks, key=lambda q: cast(surface.Quote, q).strike))
        for picked in itertools.product(*per_leg):
            by_role = {g.role: q for g, q in zip(row.legs, picked, strict=True) if q is not None}
            quotes: list[surface.Quote] = []
            for g, q in zip(row.legs, picked, strict=True):
                if q is None:
                    ref = by_role[g.strike[len("same_as_") :]]
                    q = _quote_at(surf, exp[g.role], g.right, ref.strike)
                    if q is None:
                        break  # the shared strike is not listed at this expiry
                quotes.append(q)
            if len(quotes) != len(row.legs):
                continue
            legs = tuple(
                Leg(
                    right=cast(Right, g.right),
                    action=cast(Action, g.action),
                    strike=Decimal(repr(q.strike)),
                    expiry=exp[g.role],
                )
                for g, q in zip(row.legs, quotes, strict=True)
            )
            centre = sum(
                abs(abs(q.delta) - float(g.abs_delta[0] + g.abs_delta[1]) / 2.0)
                for g, q in zip(row.legs, quotes, strict=True)
                if g.abs_delta is not None and q.delta is not None
            )
            out.append(Proto(row.id, name, row.kind, legs, tuple(quotes), centre))
    return out


def dedupe(protos: Sequence[Proto]) -> tuple[list[Proto], int]:
    """First occurrence of each structure (by :attr:`Proto.key`), and how
    many duplicates were dropped (the pricing smoke enumerated AAPL
    2027-03-19 330/350 twice)."""
    seen: set[str] = set()
    out = []
    for p in protos:
        if p.key not in seen:
            seen.add(p.key)
            out.append(p)
    return out, len(protos) - len(out)


# --------------------------------------------------------------- refusals


class Refusal(Exception):
    """A candidate refused before valuation: a stable ``code`` and why."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class EntryTerms:
    """Per-package entry terms in DEBIT ORIENTATION (plan.LegStructure)."""

    mid: Decimal  # the package mid: the deal's ref_mid
    half_spread: Decimal  # sum of the legs' half-spreads
    fill: Decimal  # the modeled base fill: paid (debit kinds) or received (credit kinds)
    limit: Decimal  # the cap (debit) or floor (credit): the first cent beyond the fill
    width: Decimal | None


def _width(kind: str, legs: Sequence[Leg]) -> Decimal | None:
    if kind in ("debit_vertical", "credit_vertical"):
        a, b = legs
        return abs(a.strike - b.strike)
    if kind == "iron_condor":
        puts = sorted(g.strike for g in legs if g.right == "P")
        calls = sorted(g.strike for g in legs if g.right == "C")
        return max(puts[1] - puts[0], calls[1] - calls[0])
    return None


def entry_terms(
    kind: str,
    legs: Sequence[Leg],
    quotes: Sequence[tuple[Decimal | None, Decimal | None]],
    *,
    fill_k: Decimal,
) -> EntryTerms:
    """The package's mid, modeled fill (mid +/- ``fill_k`` x the summed
    half-spreads, desk.pricing's fill) and limit, from each leg's (bid,
    ask); raises :class:`Refusal` for an entry that cannot be right:

    * ``no_two_sided_quote``: a leg without 0 < bid <= ask;
    * debit kinds: ``no_debit`` (package mid <= 0),
      ``debit_fill_at_or_above_width`` (a vertical can never be worth more
      than its width), ``cap_at_or_above_width`` (the limit, the smallest
      cent strictly above the fill, reaches the width);
    * credit kinds: ``credit_at_or_below_zero`` (the credit after the
      modeled fill, or its floor, the largest cent strictly below it),
      ``credit_at_or_above_width`` (a sure profit is a bad quote).
    """
    credit = kind in CREDIT_KINDS
    mid = hs = _ZERO
    for g, (bid, ask) in zip(legs, quotes, strict=True):
        if bid is None or ask is None or not _ZERO < bid <= ask:
            raise Refusal("no_two_sided_quote", f"{g.right} {g.strike} {g.expiry}: {bid}/{ask}")
        sign = (1 if g.action == "BUY" else -1) * (-1 if credit else 1)
        mid += sign * (bid + ask) / 2
        hs += (ask - bid) / 2
    width = _width(kind, legs)
    if credit:
        fill = mid - fill_k * hs
        if fill <= 0:
            raise Refusal("credit_at_or_below_zero", f"modeled credit {fill} (mid {mid})")
        if width is not None and fill >= width:
            raise Refusal("credit_at_or_above_width", f"modeled credit {fill} on width {width}")
        limit = ((fill / _CENT).to_integral_value(rounding=ROUND_CEILING) - 1) * _CENT
        if limit <= 0:
            raise Refusal("credit_at_or_below_zero", f"floor {limit} (modeled credit {fill})")
    else:
        if mid <= 0:
            raise Refusal("no_debit", f"package mid {mid}")
        fill = mid + fill_k * hs
        if width is not None and fill >= width:
            raise Refusal("debit_fill_at_or_above_width", f"modeled fill {fill} on width {width}")
        limit = ((fill / _CENT).to_integral_value(rounding=ROUND_FLOOR) + 1) * _CENT
        if width is not None and limit >= width:
            raise Refusal("cap_at_or_above_width", f"cap {limit} on width {width}")
    return EntryTerms(mid, hs, fill, limit.quantize(_CENT), width)


def exit_deadline(
    row: Row, entry: date, first_expiry: date, cal: Calendar, *, next_event: date | None = None
) -> tuple[date | None, str]:
    """The structure's exit deadline, or (None, why): the EARLIEST of the
    row's time-stop bounds (``hold_sessions`` NYSE sessions after the entry,
    the last session with at least ``min_dte`` days to the first expiry,
    ``sessions_after_event`` sessions after the next macro event) and the
    session before the last one before the first expiry (the engine's
    expiry safety closes at marketable prices from that last session, which
    would pre-empt the 09:45 time stop). It must fall after the entry."""
    sessions = cal.sessions()
    ts = row.exits.time_stop
    before = bisect.bisect_left(sessions, first_expiry)  # sessions[:before] precede it
    if before < 2:
        return None, f"no hold window: the calendar has no room before {first_expiry}"
    bounds = [sessions[before - 2]]
    i = bisect.bisect_left(sessions, entry)
    if ts.hold_sessions is not None:
        if i + ts.hold_sessions >= len(sessions):
            return None, "no hold window: the calendar ends inside the hold"
        bounds.append(sessions[i + ts.hold_sessions])
    if ts.min_dte is not None:
        j = before - 1
        while j >= 0 and days_between(sessions[j], first_expiry) < ts.min_dte:
            j -= 1
        if j < 0:
            return None, "no hold window: no session leaves min_dte"
        bounds.append(sessions[j])
    if ts.sessions_after_event is not None:
        if next_event is None:
            return None, "no hold window: no macro event to close after"
        k = bisect.bisect_left(sessions, next_event)
        if k + ts.sessions_after_event >= len(sessions):
            return None, "no hold window: the calendar ends after the event"
        bounds.append(sessions[k + ts.sessions_after_event])
    deadline = min(bounds)
    if deadline <= entry:
        return None, f"no hold window: the deadline {deadline} is not after the entry {entry}"
    return deadline, ""


def structural_refusal(row: Row, legs: Sequence[Leg]) -> str | None:
    """The playbook's actual-strike rule for two-expiry rows (Codex P2-5):
    a diagonal's long strike must be protective on the ACTUAL strikes."""
    if row.kind not in TWO_EXPIRY:
        return None
    long_ = next(g for g in legs if g.action == "BUY")
    short = next(g for g in legs if g.action == "SELL")
    try:
        require_protective(row, long_strike=long_.strike, short_strike=short.strike)
    except PlaybookError:
        return "protective_strike_rule"
    return None


def vol_gate(row: Row, vol: regime.VolState, pb: Playbook) -> str | None:
    """Why a row that matched with the vol state NOT_EVALUABLE must still be
    refused (carry-forward, playbook v2 ruling): the exemption covers ONLY
    the warm-up (a current ratio with too little history); any other
    reason (no forecast, a degraded HAR, no chain IV) refuses."""
    if row.when.vol is None or row.when.vol_not_evaluable != "match" or vol.state != regime.NE:
        return None
    warm = (
        vol.ratio is not None
        and vol.n < pb.vol_state.min_history
        and vol.reason.startswith("warm-up")
    )
    return None if warm else f"vol_not_evaluable_not_warmup: {vol.reason}"


# ------------------------------------------------------------- the run


@dataclass
class Deal:
    """One candidate as it moves through the miner."""

    proto: Proto
    row: Row
    deal_id: str
    notes: tuple[str, ...]
    signal: tuple[str, SignalDrift] | None
    status: str = ""
    reasons: list[str] = field(default_factory=list)
    terms: EntryTerms | None = None
    deadline: date | None = None
    struct: LegStructure | None = None
    valuation: pricing.Valuation | None = None
    candidate: rails.Candidate | None = None
    report: rails.RailReport | None = None
    decision: selection.Decision | None = None
    rank: int | None = None

    def refuse(self, code: str, detail: str = "") -> None:
        self.status = REFUSED
        self.reasons = [f"{code}: {detail}" if detail else code]


def deal_id(session: date, row_id: str, name: str, key: str) -> str:
    sha = hashlib.sha256(f"{SCHEMA}|{session.isoformat()}|{row_id}|{name}|{key}".encode())
    return f"d-{session.isoformat().replace('-', '')}-{row_id}-{name}-{sha.hexdigest()[:12]}"


def _row_of(did: str | None) -> str | None:
    m = _DEAL_ROW.match(did or "")
    return m.group(1) if m else None


def _dec(x: float | None) -> Decimal | None:
    return Decimal(repr(x)) if x is not None else None


def _instant(raw: object) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        ts = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return ts if ts.tzinfo is not None else None


@dataclass(frozen=True)
class MineInputs:
    """Everything a run reads, loaded (see :func:`run_mine`)."""

    session: date
    cal: Calendar
    playbook: Playbook
    config: selection.MinerConfig
    pit: PointInTime
    features: Mapping[date, Mapping[str, Any]]
    features_sha256: str
    signals_doc: Mapping[str, Any] | None
    signals_sha256: str | None
    macro: regime.MacroCalendar | None
    book: rails.BookView
    store: Path
    universe: tuple[str, ...]
    timing_vintage: desk_pit.TimingVintage | None


class _Ctx:
    """Per-run caches over the point-in-time sources."""

    def __init__(self, inp: MineInputs) -> None:
        self.inp = inp
        self.session = inp.session
        self.cal = inp.cal
        self.pit = inp.pit
        entry = first_session_after(inp.session, inp.cal)
        if entry is None:
            raise NotEvaluable(f"the session calendar ends at {inp.session}")
        self.entry = entry
        self.rate = inp.pit.rate()
        self.limits = rails.RailLimits(**{
            k: getattr(inp.playbook.limits, k) for k in rails.LIMIT_KEYS
        })  # fmt: skip
        self.rail_ctx = rails.RailContext(
            now=inp.pit.cutoff,
            session=entry,
            calendar=inp.cal,
            account_mode=ACCOUNT_MODE,
            admissions_this_session=0,
        )
        self._surfaces: dict[str, pricing.EntrySurface | NotEvaluable] = {}
        self._betas: dict[str, desk_beta.BetaEstimate] = {}
        self.chains: dict[str, str] = {}  # name -> the chain's raw_sha256
        self.book = inp.book  # replaced by the book with its positions' risk

    def surface(self, name: str) -> pricing.EntrySurface:
        if name not in self._surfaces:
            try:
                if self.rate is None:
                    raise NotEvaluable(f"no DTB3 observation knowable at {self.session}")
                doc = self.pit.chain(name)
                self._surfaces[name] = pricing.EntrySurface(
                    doc, session=self.session, rate=self.rate
                )
                self.chains[name] = str(doc["header"].get("raw_sha256"))
            except NotEvaluable as exc:
                self._surfaces[name] = exc
        got = self._surfaces[name]
        if isinstance(got, NotEvaluable):
            raise got
        return got

    def beta(self, name: str) -> desk_beta.BetaEstimate:
        if name not in self._betas:
            self._betas[name] = desk_beta.beta_from_panel(
                self.pit.sources.panel, name, self.session, self.cal
            )
        return self._betas[name]

    def leg_quotes(
        self, surf: pricing.EntrySurface, legs: Sequence[Leg]
    ) -> list[tuple[Decimal | None, Decimal | None]]:
        out: list[tuple[Decimal | None, Decimal | None]] = []
        for g in legs:
            q = _quote_at(surf, g.expiry, g.right, float(g.strike))
            out.append((_dec(q.bid), _dec(q.ask)) if q is not None else (None, None))
        return out

    def risk(self, name: str, spec: LegStructure | None, qty: int) -> rails.PositionRisk:
        """Whole-position risk priced from D's chain (missing parts stay
        None: the rails fail closed)."""
        b = self.beta(name)
        spot: Decimal | None = None
        greeks = None
        try:
            surf = self.surface(name)
            spot = _dec(surf.spot)
            if spec is not None and qty > 0 and spot is not None and self.rate is not None:
                sized = spec.model_copy(update={"quantity": qty})
                quotes = self.leg_quotes(surf, sized.legs)
                if all(b_ is not None and a is not None for b_, a in quotes):
                    greeks = structure_greeks(
                        sized,
                        [(cast(Decimal, b_), cast(Decimal, a)) for b_, a in quotes],
                        spot,
                        self.rate,
                        self.session,
                    )
        except NotEvaluable:
            pass
        return rails.PositionRisk.from_greeks(greeks, spot=spot, beta=b.beta, beta_raw=b.beta_raw)


def _book_with_risk(ctx: _Ctx, book: rails.BookView) -> rails.BookView:
    return book.with_risk(
        {p.id: ctx.risk(p.underlying, p.spec, p.quantity) for p in book.positions}
    )


def _net_beta_delta(book: rails.BookView) -> Decimal | None:
    """$ of P&L per 1% SPY move across the book; None when not evaluable."""
    if book.problems:
        return None
    total = _ZERO
    for p in book.positions:
        r = p.risk
        if r.delta_shares is None or r.spot is None or r.beta is None:
            return None
        total += r.delta_shares * r.spot * r.beta * _ONE_PCT
    return total


def _signal(row: Row, c: regime.NameConditions, pb: Playbook) -> tuple[str, SignalDrift] | None:
    """A signal row's view: of the allowed signals pointing the name, the
    one with the smallest pinned excess (conservative, deterministic)."""
    if row.drift_view != "signal":
        return None
    sigs = sorted(set(c.direction_sources) & set(row.when.signals))
    if not sigs:
        return None
    best = min(sigs, key=lambda s: (pb.drift.excess_20[s], s))
    return best, SignalDrift(
        excess_20=float(pb.drift.excess_20[best]), weight=float(row.drift_weight)
    )


def _rail_earnings(ctx: _Ctx, name: str, deadline: date) -> tuple[Any, ...] | None:
    earn = ctx.pit.earnings(name)
    if not earn.reporter:
        return ()
    status, _why = schedule_through(earn, ctx.session, deadline, ctx.cal)
    if status not in ("n/a", "complete"):
        return None  # the schedule does not pin the hold: unavailable
    return earn.all_known()


def _prepare(ctx: _Ctx, d: Deal, c: regime.NameConditions) -> None:
    """Pre-valuation refusals, the entry terms, the deadline and the spec."""
    cfg = ctx.inp.config
    why = structural_refusal(d.row, d.proto.legs)
    if why is not None:
        d.refuse(why, "the long strike is not protective on the actual strikes")
        return
    quotes = [(_dec(q.bid), _dec(q.ask)) for q in d.proto.quotes]
    try:
        d.terms = entry_terms(d.proto.kind, d.proto.legs, quotes, fill_k=cfg.fill_k)
    except Refusal as exc:
        d.refuse(exc.code, exc.detail)
        return
    first = min(g.expiry for g in d.proto.legs)
    d.deadline, reason = exit_deadline(
        d.row, ctx.entry, first, ctx.cal, next_event=c.events.next_macro
    )
    if d.deadline is None:
        d.refuse("no_hold_window", reason)
        return
    try:
        d.struct = LegStructure(
            id=d.deal_id,
            underlying=d.proto.name,
            kind=cast(Any, d.proto.kind),
            legs=d.proto.legs,
            quantity=QUANTITY,
            entry_date=ctx.entry,
            exit_deadline=d.deadline,
            limit=d.terms.limit,
            exits=d.row.exits.rules,
            deal_id=d.deal_id,
            ref_mid=d.terms.mid,
        )
    except ValueError as exc:
        d.refuse("invalid_structure", " ".join(str(exc).split())[:240])


def _rail_candidate(ctx: _Ctx, d: Deal) -> rails.Candidate:
    struct = cast(LegStructure, d.struct)
    name = struct.underlying
    surf = ctx.surface(name)
    header = surf.doc["header"]
    legs = []
    for g, q in zip(struct.legs, d.proto.quotes, strict=True):
        legs.append(
            rails.CandidateLeg(
                right=g.right,
                action=g.action,
                strike=g.strike,
                expiry=g.expiry,
                bid=_dec(q.bid),
                ask=_dec(q.ask),
                open_interest=q.oi,
                delta=_dec(q.delta),
            )
        )
    deadline = cast(date, d.deadline)
    return rails.Candidate(
        id=d.deal_id,
        underlying=name,
        kind=struct.kind,
        legs=tuple(legs),
        quantity=struct.quantity,
        max_loss_usd=struct.max_loss(),
        entry_price=struct.limit,
        planned_exit=deadline,
        risk=ctx.risk(name, struct, struct.quantity),
        chain_session=date.fromisoformat(str(header.get("session"))),
        chain_as_of=_instant(header.get("source_as_of")),
        earnings=_rail_earnings(ctx, name, deadline),
        ex_dividends=dividends.ex_dividends_for(
            name, ctx.entry, deadline, ctx.cal, as_of=ctx.session, store=ctx.inp.store
        ),
        news_veto=None,
    )


def _failed_rules(report: rails.RailReport) -> list[str]:
    return [f"{r.rule}: {r.status} {r.detail}".strip() for r in report.failed()]


def _queued_position(d: Deal) -> rails.BookPosition:
    struct = cast(LegStructure, d.struct)
    cand = cast(rails.Candidate, d.candidate)
    return rails.BookPosition(
        id=f"queue:{d.deal_id}",
        source="queue",
        underlying=struct.underlying,
        status="working",
        max_loss_usd=struct.max_loss(),
        risk=cand.risk,
        detail="queued ahead in this session",
        spec=struct,
        quantity=struct.quantity,
    )


# -------------------------------------------------------------- payload

RATIONALE = (
    "{row} ({tier}, playbook {playbook}): {kind} x{qty} on {name}, {legs}. "
    "Entry on {entry} at a limit of {limit} per package (debit orientation; the "
    "modeled fill {fill} = mid {mid} {pm} {k} x half-spread {hs}), max loss ${max_loss}. "
    "Valued on {n} bootstrap paths from the {session} closing marks to the {exit} CLOSE; "
    "the engine's time stop fires at 09:45 ET that session, and no take-profit or stop "
    "before it is modeled. Exit legs are repriced by European Black-Scholes, q = 0 "
    "(early exercise and dividends ignored), at the entry smile's moneyness. "
    "IV mean reversion {mr}: {mr_reason}. "
    "No-view EV ${ev} beside signal EV ${ev_signal}{view}; decision EV ({basis}) ${dev} "
    "at base fills, ${dstress} at stress fills, {ratio} of max loss; "
    "P(profit) {pp:.4f}; P(max loss) {pml:.4f}; CVaR 5% ${cvar}.{notes}"
)


def _legs_text(legs: Sequence[Leg]) -> str:
    return ", ".join(f"{g.action} {g.right} {g.strike} {g.expiry.isoformat()}" for g in legs)


def rationale(d: Deal, inp: MineInputs) -> str | None:
    """The FIXED template (never model text), filled from the deal."""
    v, t, s = d.valuation, d.terms, d.struct
    if v is None or t is None or s is None:
        return None
    dec = d.decision
    view = (
        f" (the {d.signal[0]} view: {d.signal[1].weight} x its backtest 20-session excess "
        f"{d.signal[1].excess_20}, unproven forward)"
        if d.signal is not None
        else " (no view on this row)"
    )
    notes = "".join(f" Note: {n}." for n in d.notes)
    return RATIONALE.format(
        row=d.row.id,
        tier=d.row.tier,
        playbook=inp.playbook.version,
        kind=s.kind,
        qty=s.quantity,
        name=s.underlying,
        legs=_legs_text(s.legs),
        entry=s.entry_date.isoformat(),
        limit=s.limit,
        fill=t.fill,
        mid=t.mid,
        pm="-" if s.is_credit else "+",
        k=inp.config.fill_k,
        hs=t.half_spread,
        max_loss=s.max_loss(),
        n=v.n_paths,
        session=inp.session.isoformat(),
        exit=v.exit_session.isoformat(),
        mr=v.iv_mean_reversion.status,
        mr_reason=v.iv_mean_reversion.reason or "none given",
        ev=v.ev,
        ev_signal=v.ev_signal if v.ev_signal is not None else "n/a",
        view=view,
        basis=dec.basis if dec else "n/a",
        dev=dec.ev if dec else "n/a",
        dstress=dec.ev_stress_fill if dec else "n/a",
        ratio=dec.ev_per_max_loss if dec else "n/a",
        pp=v.p_profit,
        pml=v.p_max_loss,
        cvar=v.cvar5,
        notes=notes,
    )


def _s(x: Decimal | None) -> str | None:
    return None if x is None else str(x)


def _deal_doc(d: Deal, inp: MineInputs) -> dict[str, Any]:
    legs = []
    for g, q in zip(d.proto.legs, d.proto.quotes, strict=True):
        legs.append(
            {
                "right": g.right,
                "action": g.action,
                "strike": str(g.strike),
                "expiry": g.expiry.isoformat(),
                "bid": _s(_dec(q.bid)),
                "ask": _s(_dec(q.ask)),
                "oi": q.oi,
                "iv": q.iv,
                "delta": q.delta,
            }
        )
    s, t, v, dec = d.struct, d.terms, d.valuation, d.decision
    return {
        "deal_id": d.deal_id,
        "rank": d.rank,
        "status": d.status,
        "reasons": list(d.reasons),
        "notes": list(d.notes),
        "row": d.row.id,
        "row_title": d.row.title,
        "tier": d.row.tier,
        "underlying": d.proto.name,
        "kind": d.proto.kind,
        "quantity": QUANTITY,
        "legs": legs,
        "entry_session": inp.pit.cutoff.date().isoformat(),
        "exit_deadline": d.deadline.isoformat() if d.deadline else None,
        "width": _s(t.width) if t else None,
        "ref_mid": _s(t.mid) if t else None,
        "fill": _s(t.fill) if t else None,
        "limit": _s(t.limit) if t else None,
        "max_loss": _s(s.max_loss()) if s else None,
        "signal": (
            {
                "name": d.signal[0],
                "excess_20": str(inp.playbook.drift.excess_20[d.signal[0]]),
                "weight": str(d.row.drift_weight),
            }
            if d.signal
            else None
        ),
        "structure": s.model_dump(mode="json") if s else None,
        "valuation": v.as_dict() if v else None,
        "decision": (
            {
                "basis": dec.basis,
                "ev": str(dec.ev),
                "ev_stress_fill": str(dec.ev_stress_fill),
                "ev_per_max_loss": str(dec.ev_per_max_loss),
            }
            if dec
            else None
        ),
        "rails": d.report.to_dict() if d.report else None,
        "rationale": rationale(d, inp),
    }


def _code(reason: str) -> str:
    return reason.split(":", 1)[0]


def _row_summary(row: Row, open_n: int) -> dict[str, Any]:
    return {
        "title": row.title,
        "kind": row.kind,
        "status": row.status,
        "max_open": row.max_open,
        "open": open_n,
        "matched": [],
        "match_refused": [],
        "unmatched": Counter(),
        "enumerated": 0,
        "duplicates": 0,
        "candidates": 0,
        "valued": 0,
        "refused": Counter(),
        "rail_failed_deals": 0,
        "rail_failed": Counter(),
        "not_selected": Counter(),
        "capacity": Counter(),
        "admissible": 0,
    }


def mine(inp: MineInputs) -> dict[str, Any]:
    """The queue document of one session (pure given its inputs: the only
    reads are D's chains and dividend snapshots, through pit and dividends)."""
    pb, cfg, cal, session = inp.playbook, inp.config, inp.cal, inp.session
    ctx = _Ctx(inp)
    feats = inp.features
    indices = {n: _index_closes(inp.pit, n) for n in (pb.term.market_front, pb.term.market_back)}
    schedule = {
        n: inp.pit.earnings(n).known_dates()
        for n in sorted(set(inp.universe) | set(EARNINGS_NAMES))
    }
    reg = regime.conditions_at(
        session,
        cal,
        pb,
        names=inp.universe,
        signals_doc=inp.signals_doc,
        features=feats,
        indices=indices,
        schedule=schedule,
        macro=inp.macro,
        news_flags=None,
    )
    book = ctx.book = _book_with_risk(ctx, inp.book)
    net = _net_beta_delta(book)
    cap = pb.limits.max_net_beta_delta_usd_per_1pct_spy
    over = None if net is None else abs(net) > cap
    row_open = Counter(
        _row_of(p.spec.deal_id)
        for p in book.positions
        if p.source == "desk" and p.spec is not None and p.spec.deal_id
    )
    rows: dict[str, dict[str, Any]] = {
        r.id: _row_summary(r, row_open.get(r.id, 0)) for r in pb.rows
    }
    deals: list[Deal] = []
    for row in pb.rows:
        summary = rows[row.id]
        for name in inp.universe:
            c = reg.names[name]
            m = regime.match_row(row, c, book_over_delta_cap=over)
            if not m.matched:
                summary["unmatched"].update({_code(r) for r in m.reasons})
                continue
            gate = vol_gate(row, c.vol, pb)
            if gate is not None:
                summary["match_refused"].append({"name": name, "reason": gate})
                continue
            try:
                surf = ctx.surface(name)
            except NotEvaluable as exc:
                summary["match_refused"].append({"name": name, "reason": f"not_evaluable: {exc}"})
                continue
            summary["matched"].append(name)
            protos = enumerate_row(row, name, surf, ctx.entry)
            unique, dropped = dedupe(protos)
            summary["enumerated"] += len(protos)
            summary["duplicates"] += dropped
            summary["candidates"] += len(unique)
            order = sorted(unique, key=lambda p: (p.centrality, p.key))
            keep = {p.key for p in order[: cfg.max_valued_per_name_row]}
            signal = _signal(row, c, pb)
            batch = []
            for p in unique:
                d = Deal(p, row, deal_id(session, row.id, name, p.key), m.notes, signal)
                deals.append(d)
                if p.key not in keep:
                    d.refuse("enumeration_cap", f"beyond the {cfg.max_valued_per_name_row} nearest")
                    continue
                _prepare(ctx, d, c)
                if d.struct is not None:
                    batch.append(d)
            _value(ctx, batch, row, name, signal)
    _rails_and_decide(ctx, deals, cfg)
    _select(ctx, deals, book, row_open)
    for d in deals:
        s = rows[d.row.id]
        if d.status == REFUSED:
            s["refused"][_code(d.reasons[0])] += 1
            continue
        s["valued"] += 1
        if d.status == RAIL_FAILED:
            s["rail_failed_deals"] += 1
            s["rail_failed"].update(r.rule for r in cast(rails.RailReport, d.report).failed())
        elif d.status == NOT_SELECTED:
            s["not_selected"].update(_code(r) for r in d.reasons)
        elif d.status == CAPACITY:
            s["capacity"][_code(d.reasons[0])] += 1
        elif d.status == ADMISSIBLE:
            s["admissible"] += 1
    admissible = sorted(
        (d for d in deals if d.status == ADMISSIBLE), key=lambda d: cast(int, d.rank)
    )
    surfaced = [d for d in deals if d.status != ADMISSIBLE]
    for s in rows.values():
        for k in ("unmatched", "refused", "rail_failed", "not_selected", "capacity"):
            s[k] = dict(sorted(s[k].items()))
    vint = inp.timing_vintage
    return {
        "schema": SCHEMA,
        "session": session.isoformat(),
        "entry_session": ctx.entry.isoformat(),
        "decision_cutoff": inp.pit.cutoff.isoformat(),
        "valid_until": datetime.combine(ctx.entry, ADMISSION_END, tzinfo=ET).isoformat(),
        "code": CODE,
        "playbook": {"file": f"{pb.version}.toml", "version": pb.version, "sha256": pb.sha256},
        "miner": {
            "file": f"{cfg.version}.toml",
            "version": cfg.version,
            "sha256": cfg.sha256,
            "status": cfg.status,
            "ruling": cfg.ruling,
            "n_paths": cfg.n_paths,
        },
        "inputs": {
            "universe": list(inp.universe),
            "chains_raw_sha256": dict(sorted(ctx.chains.items())),
            "features_sha256": inp.features_sha256,
            "signals": {"status": reg.signals_status, "sha256": inp.signals_sha256},
            "timing_vintage": {
                "session": vint.session.isoformat() if vint else None,
                "sha256": vint.sha256 if vint else None,
            },
            "dtb3_rate": ctx.rate,
            "market_term": {
                "state": reg.market.state,
                "front": reg.market.front,
                "back": reg.market.back,
            },
            "book": {
                "positions": [
                    {
                        "id": p.id,
                        "underlying": p.underlying,
                        "status": p.status,
                        "max_loss": _s(p.max_loss_usd),
                        "delta_shares": _s(p.risk.delta_shares),
                    }
                    for p in book.positions
                ],
                "problems": list(book.problems),
                "net_beta_delta_usd_per_1pct_spy": _s(
                    net.quantize(_CENT) if net is not None else None
                ),
                "over_delta_cap": over,
            },
        },
        "no_options_expression": {
            "names": list(reg.no_options_expression),
            "policy": pb.xsmom.policy,
        },
        "rows": rows,
        "admissible": [_deal_doc(d, inp) for d in admissible],
        "surfaced": [_deal_doc(d, inp) for d in surfaced],
    }


def _index_closes(pit: PointInTime, name: str) -> dict[date, float]:
    """A stored index's closes as knowable at the cutoff, as floats (an
    unparseable close is absent, never a guess)."""
    out: dict[date, float] = {}
    for d, raw in pit.index(name).items():
        try:
            out[d] = float(raw)
        except ValueError:
            continue
    return out


def _value(
    ctx: _Ctx, batch: list[Deal], row: Row, name: str, signal: tuple[str, SignalDrift] | None
) -> None:
    """One value_candidates call per exit session (one path set each)."""
    by_exit: dict[date, list[Deal]] = {}
    for d in batch:
        by_exit.setdefault(cast(date, d.deadline), []).append(d)
    for exit_session, group in sorted(by_exit.items()):
        results = pricing.value_candidates(
            ctx.pit,
            name,
            [cast(LegStructure, d.struct) for d in group],
            exit_session,
            row_id=row.id,
            signal=signal[1] if signal else None,
            n_paths=ctx.inp.config.n_paths,
        )
        for d, res in zip(group, results, strict=True):
            if res.valuation is None:
                d.refuse("not_evaluable", res.reason)
            elif not res.valuation.limit_ok:
                d.valuation = res.valuation
                d.refuse(
                    "limit_not_ok",
                    f"modeled fill {res.valuation.entry_fill} beyond the limit {d.struct and d.struct.limit}",
                )
            else:
                d.valuation = res.valuation


def _rails_and_decide(ctx: _Ctx, deals: list[Deal], cfg: selection.MinerConfig) -> None:
    for d in deals:
        if d.struct is None:
            continue
        d.candidate = _rail_candidate(ctx, d)
        d.report = rails.check(d.candidate, ctx.book, ctx.rail_ctx, ctx.limits)
        if d.status == REFUSED or d.valuation is None:
            continue
        d.decision = selection.decision_of(d.valuation, signal_view=d.row.drift_view == "signal")
        if not d.report.ok:
            d.status = RAIL_FAILED
            d.reasons = _failed_rules(d.report)
            continue
        if d.decision is None:
            d.status, d.reasons = NOT_SELECTED, ["no_decision_ev"]
            continue
        fails = selection.selection_failures(d.decision, cfg)
        if fails:
            d.status, d.reasons = NOT_SELECTED, fails


def _select(
    ctx: _Ctx, deals: list[Deal], book: rails.BookView, row_open: Counter[str | None]
) -> None:
    """Rank the eligible deals and queue them greedily: each re-checked with
    the higher-ranked queued deals in the book (working at their caps) and
    as this session's admissions, within its row's max_open."""
    eligible = [d for d in deals if not d.status and d.decision is not None]
    eligible.sort(
        key=lambda d: selection.rank_key(
            cast(selection.Decision, d.decision), cast(LegStructure, d.struct).max_loss(), d.deal_id
        )
    )
    queued: list[Deal] = []
    keys: dict[tuple[str, str], str] = {}
    for d in eligible:
        key = (d.proto.name, d.proto.key)
        if key in keys:
            d.status, d.reasons = CAPACITY, [f"duplicate_structure: queued as {keys[key]}"]
            continue
        used = row_open.get(d.row.id, 0) + sum(1 for q in queued if q.row.id == d.row.id)
        if used >= d.row.max_open:
            d.status = CAPACITY
            d.reasons = [f"max_open: row {d.row.id} holds {used} of {d.row.max_open}"]
            continue
        joint = replace(book, positions=book.positions + tuple(_queued_position(q) for q in queued))
        rep = rails.check(
            cast(rails.Candidate, d.candidate),
            joint,
            replace(ctx.rail_ctx, admissions_this_session=len(queued)),
            ctx.limits,
        )
        if not rep.ok:
            d.status = CAPACITY
            d.reasons = ["joint_rails: " + "; ".join(_failed_rules(rep))]
            continue
        queued.append(d)
        keys[key] = d.deal_id
        d.status, d.rank = ADMISSIBLE, len(queued)


# -------------------------------------------------------------------- I/O


def encode(payload: Mapping[str, Any]) -> bytes:
    """The queue's bytes: sorted keys, ASCII, one trailing newline."""
    return (json.dumps(payload, indent=1, sort_keys=True, ensure_ascii=True) + "\n").encode()


@dataclass(frozen=True)
class MineResult:
    exit_code: int  # 0 done | 3 inputs not ready (retry) | 1 failure or conflict | 2 bad arguments
    status: str
    detail: str = ""
    session: date | None = None
    payload: dict[str, Any] | None = None
    path: Path | None = None
    timing_status: str = ""  # written | exists | dry_run | too_late (then the newest older vintage)

    def line(self) -> str:
        d = self.session.isoformat() if self.session else "-"
        tail = f" {self.detail}" if self.detail else ""
        where = f" -> {self.path}" if self.path else ""
        return f"mine {d} {self.status} rc={self.exit_code}{where}{tail}"

    def summary(self) -> list[str]:
        """One line per row that matched anything (counts only, no money)."""
        if self.payload is None:
            return []
        out = []
        for rid, s in self.payload["rows"].items():
            if not (s["matched"] or s["match_refused"] or s["candidates"]):
                continue
            out.append(
                f"  {rid} matched={len(s['matched'])} match_refused={len(s['match_refused'])}"
                f" enumerated={s['enumerated']} candidates={s['candidates']}"
                f" valued={s['valued']} refused={s['refused']}"
                f" rail_failed={s['rail_failed_deals']} {s['rail_failed']}"
                f" not_selected={s['not_selected']} capacity={s['capacity']}"
                f" admissible={s['admissible']}"
            )
        return out


def _read_json(path: Path) -> tuple[Any, str | None]:
    try:
        raw = path.read_bytes()
        return json.loads(raw), hashlib.sha256(raw).hexdigest()
    except (OSError, ValueError):
        return None, None


def load_features(
    store: Path, session: date, cal: Calendar, window: int
) -> dict[date, Mapping[str, Any]]:
    """The desk features documents of ``session`` and the ``window``
    sessions before it (missing or unreadable ones are simply absent: the
    regime counts only documents that prove themselves)."""
    sessions = cal.sessions()
    i = bisect.bisect_right(sessions, session)
    out: dict[date, Mapping[str, Any]] = {}
    for d in sessions[max(0, i - 1 - window) : i]:
        doc, _sha = _read_json(store / "features" / f"{d.isoformat()}.json")
        if isinstance(doc, dict):
            out[d] = doc
    return out


def _has_chains(store: Path, session: date) -> bool:
    chain_dir = store / "chains" / session.isoformat()
    return any(not p.name.endswith(".conflict.json.gz") for p in chain_dir.glob("*.json.gz"))


def run_mine(
    *,
    session: date | None,
    now: datetime,
    cal: Calendar,
    dry_run: bool = False,
    names: Sequence[str] | None = None,
    out: Path | None = None,
    config: selection.MinerConfig | None = None,
    playbook: Playbook | None = None,
    desk_specs: Path | None = None,
    desk_book: Path | None = None,
    build_features: Callable[[date], int] | None = None,
) -> MineResult:
    """Mine session D (default: the latest completed session at ``now``).

    ``now`` (injected) picks the default session, decides whether the
    earnings-timing vintage of D may still be snapshotted (before D's
    cutoff), and stamps the stage marker; it never enters the payload.
    ``dry_run`` writes nothing under the store or the state (the payload
    goes to ``out`` when given); otherwise the vintage is snapshotted and
    the queue written once to ``out`` or ``<queue dir>/<D>.json`` (the
    latter also marks the stage done). ``names`` restricts the universe
    (only for a dry run or an ``out`` file: a live queue is whole).
    ``build_features`` (the CLI's ``features`` job) is run first when D's
    features document is missing and this is not a dry run."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if session is None:
        d = latest_completed_session(now, cal)
    elif not cal.is_session(session):
        return MineResult(2, "bad_session", f"{session} is not an NYSE session", session)
    elif now < cutoff_instant(session):
        return MineResult(2, "not_closed", f"{session} has not closed yet (16:15 ET)", session)
    else:
        d = session
    live = not dry_run and out is None
    if names is not None:
        if live:
            return MineResult(2, "partial_queue", "--names needs --dry-run or --out", d)
        bad = [n for n in names if n not in CHAIN_UNIVERSE]
        if bad or not names:
            return MineResult(2, "bad_names", f"not in the chain universe: {bad}", d)
    universe = tuple(n for n in CHAIN_UNIVERSE if names is None or n in names)
    state, store, paper = paths.state_root(), paths.store_root(), paths.paper_dir()
    marker = state / "stages" / d.isoformat() / f"{STAGE}.done.json"
    if live and marker.exists():
        return MineResult(0, "already_done", "", d)
    try:
        pb = playbook or load_playbook()
        cfg = config or selection.load_config()
    except (PlaybookError, selection.MinerConfigError) as exc:
        return MineResult(1, "sealed_input", str(exc), d)
    # the vintage first: it can only be taken before D's cutoff, so a slot
    # whose chains are not in yet still records what the timing file knew
    try:
        tstatus, vintage = desk_pit.snapshot_timing(
            d, cal, paper=paper, store=store, now=now, dry_run=dry_run
        )
    except NotEvaluable as exc:
        return MineResult(1, "inputs", f"NotEvaluable: {exc}", d)
    if not _has_chains(store, d):
        return MineResult(3, "not_ready", f"no recorded chains for {d}", d, timing_status=tstatus)
    feat_path = store / "features" / f"{d.isoformat()}.json"
    if not feat_path.exists() and not dry_run and build_features is not None:
        build_features(d)
    if not feat_path.exists():
        return MineResult(
            3, "not_ready", f"no features document for {d} (features --session)", d,
            timing_status=tstatus,
        )  # fmt: skip
    try:
        if vintage is None:
            vintage = desk_pit.load_timing_vintage(d, cal, store=store)
        sources = desk_pit.load_sources(
            paper=paper, store=store, timing=vintage.timing if vintage is not None else {}
        )
        pit = PointInTime(d, cal, sources)
    except PanelLocked as exc:
        return MineResult(3, "panel_locked", str(exc), d)
    except (NotEvaluable, OSError, ValueError) as exc:
        return MineResult(1, "inputs", f"{type(exc).__name__}: {exc}", d)
    window = max(pb.vol_state.window_sessions, pb.term.window_sessions)
    signals_doc, signals_sha = _read_json(state / "signals" / f"{d.isoformat()}.json")
    _doc, features_sha = _read_json(feat_path)
    entry = first_session_after(d, cal)
    assert entry is not None  # PointInTime found a cutoff
    book = load_book(as_of=entry, desk_specs=desk_specs, desk_book=desk_book)
    inputs = MineInputs(
        session=d,
        cal=cal,
        playbook=pb,
        config=cfg,
        pit=pit,
        features=load_features(store, d, cal, window),
        features_sha256=str(features_sha),
        signals_doc=signals_doc if isinstance(signals_doc, dict) else None,
        signals_sha256=signals_sha,
        macro=regime.load_macro_calendar(),
        book=book,
        store=store,
        universe=universe,
        timing_vintage=vintage,
    )
    try:
        payload = mine(inputs)
    except NotEvaluable as exc:
        return MineResult(1, "inputs", str(exc), d, timing_status=tstatus)
    data = encode(payload)
    if dry_run:
        if out is not None:
            atomic_write_bytes(out, data)
        return MineResult(0, "dry_run", "", d, payload, out, tstatus)
    target = out or paths.queue_dir() / f"{d.isoformat()}.json"
    sha = hashlib.sha256(data).hexdigest()
    if atomic_create_bytes(target, data):
        status = "written"
    elif target.read_bytes() == data:
        status = "exists"
    else:
        conflict = state / "queue-conflicts" / f"{d.isoformat()}.{sha[:12]}.json"
        atomic_write_bytes(conflict, data)
        return MineResult(
            1,
            "conflict",
            f"{target.name} differs from this run (kept); this run -> {conflict}",
            d,
            payload,
            target,
            tstatus,
        )
    if live:
        atomic_write_json(
            marker,
            {
                "session": d.isoformat(),
                "stage": STAGE,
                "done_at": now.isoformat(),
                "queue": str(target),
                "queue_sha256": sha,
                "admissible": len(payload["admissible"]),
                "surfaced": len(payload["surfaced"]),
            },
        )
    return MineResult(0, status, "", d, payload, target, tstatus)
