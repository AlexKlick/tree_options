"""Point-in-time conditions per name per session (plan D5), and the sealed
playbook rows they match.

Every policy number (percentile cut-offs, minimum history, windows, event
kinds) comes from the sealed playbook (:mod:`tree_options.desk.playbook`);
nothing here has a tunable default. Conditions for session D read only
inputs dated D or earlier: history windows end strictly before D, and the
features/index values of D itself are the only same-day inputs, so a
future-poisoned input changes nothing (tests prove it).

* **direction**: ``bull`` only from the allowed direction signals of D's
  signals file (XSMOM top-3 on a rebalance day, PEAD beats); ``none``
  otherwise; ``unknown`` when the signals file is missing, malformed or of
  another session. Never ``bear``: no allowed signal points down. An
  XSMOM pick without an options expression (TQQQ/SQQQ) is logged in
  ``no_options_expression``; the next-ranked name is never promoted.
* **vol** (the playbook's ``[vol_state]``): R = chain ATM IV30 / HAR h=20
  vol from desk features; the state is R's percentile within the name's
  own source-consistent R history (``cheap`` / ``fair`` / ``rich`` by the
  name's fidelity band), ``NOT_EVALUABLE`` during warm-up or without a
  validated forecast. Never a guessed or default state.
* **term**: the name's IV90/IV30 slope sign (``contango`` /
  ``backwardation`` / ``flat``), ``steep`` contango by the slope's own
  percentile (same warm-up rule), the event inversion (front IV above
  back IV around the next macro event) and the market term (VIX vs VIX3M
  closes dated D).
* **events** over the next ``window_sessions`` sessions: earnings (sealed +
  timing file, estimated dates included: they may block, never trigger),
  mapped ETF holdings reporting, counted macro events; ``unknown``
  whenever the schedule does not pin the window.
* **news**: an input hook for the Wave 3 veto-only news model; absent
  means no flag (``news_source: absent``).

Only stdlib, numpy (through desk.har) and tree_options imports.
"""

from __future__ import annotations

import bisect
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from fractions import Fraction
from pathlib import Path
from typing import Any

from tree_options.desk import events as desk_events
from tree_options.desk import paths
from tree_options.desk.har import schedule_status
from tree_options.desk.playbook import (
    EventPolicy,
    Playbook,
    Row,
    TermPolicy,
    VolBand,
    VolStatePolicy,
)
from tree_options.desk.sessions import Calendar
from tree_options.desk.signals import ALLOWED_DIRECTION, require_direction_signal
from tree_options.desk.universe import NO_OPTIONS_EXPRESSION, PANEL_ETFS

SCHEMA = "desk-regime/1"
NE = "NOT_EVALUABLE"


def _num(x: Any) -> float | None:
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    v = float(x)
    return v if v == v and abs(v) != float("inf") else None


def _prior_sessions(session: date, cal: Calendar, window: int) -> Sequence[date]:
    """The ``window`` NYSE sessions strictly before ``session``."""
    sessions = cal.sessions()
    i = bisect.bisect_left(sessions, session)
    return sessions[max(0, i - window) : i]


def _names_of(doc: Mapping[str, Any] | None, schema: str) -> Mapping[str, Any] | None:
    """A features document's per-name table, only if it is the sealed source."""
    if not isinstance(doc, Mapping) or doc.get("schema") != schema:
        return None
    names = doc.get("names")
    return names if isinstance(names, Mapping) else None


def _history(
    name: str,
    session: date,
    cal: Calendar,
    features: Mapping[date, Mapping[str, Any]],
    *,
    window: int,
    schema: str,
    value: Callable[[Any], float | None],
) -> list[float]:
    out: list[float] = []
    for d in _prior_sessions(session, cal, window):
        names = _names_of(features.get(d), schema)
        if names is None:
            continue  # no document, or another source/method: not counted
        v = value(names.get(name))
        if v is not None:
            out.append(v)
    return out


def _percentile(current: float, history: Sequence[float]) -> Fraction:
    """Share of the history strictly below the current value (ties are not below)."""
    return Fraction(sum(1 for h in history if h < current), len(history))


# ------------------------------------------------------------------- vol


@dataclass(frozen=True)
class VolState:
    state: str  # cheap | fair | rich | NOT_EVALUABLE
    reason: str
    ratio: float | None
    percentile: Fraction | None
    n: int
    band_name: str  # validated | unvalidated
    band: VolBand


def _ratio_reason(entry: Any, policy: VolStatePolicy) -> tuple[float | None, str]:
    if not isinstance(entry, Mapping):
        return None, "no features for the name"
    if entry.get("status") == NE:
        return None, f"features not evaluable: {entry.get('reason', '')}"
    iv = entry.get("iv")
    iv30 = _num(iv.get("30")) if isinstance(iv, Mapping) else None
    if iv30 is None or iv30 <= 0.0:
        return None, "no IV30 on the chain"
    fc = entry.get("forecast")
    if not isinstance(fc, Mapping) or fc.get("source") != "har":
        return None, "no HAR forecast"
    if fc.get("har_status") != policy.har_status_required:
        return None, f"HAR status {fc.get('har_status')!r}, not {policy.har_status_required!r}"
    har_vol = _num(fc.get("har_vol"))
    if har_vol is None or har_vol <= 0.0:
        return None, "no HAR vol"
    return iv30 / har_vol, ""


def vol_state(
    name: str,
    session: date,
    cal: Calendar,
    features: Mapping[date, Mapping[str, Any]],
    policy: VolStatePolicy,
) -> VolState:
    band_name, band = policy.band_for(name)

    def nev(reason: str, n: int = 0, ratio: float | None = None) -> VolState:
        return VolState(NE, reason, ratio, None, n, band_name, band)

    if not cal.is_session(session):
        return nev(f"{session} is not an NYSE session")
    doc = features.get(session)
    if doc is None:
        return nev("no features for the session")
    names = _names_of(doc, policy.features_schema)
    if names is None:
        return nev(f"features schema {doc.get('schema')!r} is not the sealed source")
    ratio, why = _ratio_reason(names.get(name), policy)
    if ratio is None:
        return nev(why)
    hist = _history(
        name,
        session,
        cal,
        features,
        window=policy.window_sessions,
        schema=policy.features_schema,
        value=lambda e: _ratio_reason(e, policy)[0],
    )
    if len(hist) < policy.min_history:
        return nev(
            f"warm-up: {len(hist)}/{policy.min_history} sessions of history", len(hist), ratio
        )
    p = _percentile(ratio, hist)
    state = "cheap" if p < band.cheap_below else "rich" if p > band.rich_above else "fair"
    return VolState(state, "", ratio, p, len(hist), band_name, band)


# ------------------------------------------------------------------ term


@dataclass(frozen=True)
class TermState:
    state: str  # contango | backwardation | flat | NOT_EVALUABLE
    reason: str
    slope: float | None
    steep: str  # yes | no | NOT_EVALUABLE
    steep_reason: str
    steep_percentile: Fraction | None
    steep_n: int
    event_inversion: str  # yes | no | n/a | NOT_EVALUABLE
    event_detail: str


def _slope(entry: Any) -> float | None:
    return _num(entry.get("term_slope")) if isinstance(entry, Mapping) else None


def _event_inversion(entry: Any, next_event: date | None, term: TermPolicy) -> tuple[str, str]:
    if next_event is None:
        return "n/a", "no counted macro event in the window"
    raw = entry.get("atm_term") if isinstance(entry, Mapping) else None
    points: list[tuple[int, str, float]] = []
    for p in raw if isinstance(raw, list) else []:
        if isinstance(p, list) and len(p) >= 3 and isinstance(p[0], str):
            dte, iv = p[1], _num(p[2])
            if type(dte) is int and iv is not None:
                points.append((dte, p[0], iv))
    points.sort()
    ev = next_event.isoformat()
    front = next((q for q in points if q[1] > ev and q[0] >= term.event_front_min_dte), None)
    if front is None:
        return NE, f"no expiry after the event {ev} with DTE >= {term.event_front_min_dte}"
    back = next((q for q in points if q[0] >= front[0] + term.event_back_min_gap_days), None)
    if back is None:
        return NE, f"no expiry {term.event_back_min_gap_days}+ days after the front {front[1]}"
    detail = f"front {front[1]} iv {front[2]:.4f} vs back {back[1]} iv {back[2]:.4f}"
    return ("yes" if front[2] > back[2] else "no"), detail


def term_state(
    name: str,
    session: date,
    cal: Calendar,
    features: Mapping[date, Mapping[str, Any]],
    pb: Playbook,
    *,
    next_event: date | None,
) -> TermState:
    term, schema = pb.term, pb.vol_state.features_schema
    names = _names_of(features.get(session), schema)
    entry = names.get(name) if names is not None else None
    slope = _slope(entry)
    inversion, detail = _event_inversion(entry, next_event, term)
    if names is None or slope is None:
        why = "no sealed-source features for the session" if names is None else "no IV30/IV90 slope"
        return TermState(NE, why, None, NE, why, None, 0, inversion, detail)
    state = "contango" if slope > 0 else "backwardation" if slope < 0 else "flat"
    if slope <= 0:
        return TermState(state, "", slope, "no", "", None, 0, inversion, detail)
    hist = _history(
        name, session, cal, features, window=term.window_sessions, schema=schema, value=_slope
    )
    if len(hist) < term.min_history:
        why = f"warm-up: {len(hist)}/{term.min_history} sessions of slope history"
        return TermState(state, "", slope, NE, why, None, len(hist), inversion, detail)
    p = _percentile(slope, hist)
    steep = "yes" if p > term.steep_above else "no"
    return TermState(state, "", slope, steep, "", p, len(hist), inversion, detail)


@dataclass(frozen=True)
class MarketTerm:
    state: str  # contango | backwardation | flat | NOT_EVALUABLE
    reason: str
    front: float | None
    back: float | None


def market_term(
    session: date, indices: Mapping[str, Mapping[date, float]], term: TermPolicy
) -> MarketTerm:
    """VIX vs VIX3M closes dated the session itself (an older close is not
    today's term structure)."""
    front = _num((indices.get(term.market_front) or {}).get(session))
    back = _num((indices.get(term.market_back) or {}).get(session))
    if front is None or back is None:
        return MarketTerm(
            NE, f"no {term.market_front}/{term.market_back} close on {session}", front, back
        )
    state = "contango" if front < back else "backwardation" if front > back else "flat"
    return MarketTerm(state, "", front, back)


# ---------------------------------------------------------------- events


@dataclass(frozen=True)
class MacroCalendar:
    """The sealed macro calendar's events, the range it covers, and its
    declared gaps (``"cpi 2027"``: no information that year)."""

    events: tuple[desk_events.MacroEvent, ...]
    covered: tuple[date, date]
    gaps: frozenset[str]


def load_macro_calendar(path: Path | None = None) -> MacroCalendar | None:
    """The sealed macro calendar, or None when it is missing or its seal is
    broken (the macro state is then ``unknown``, never ``none``)."""
    path = path or paths.events_dir() / desk_events.MACRO_FILE
    try:
        doc = desk_events.load_macro(path)
        lo = date.fromisoformat(doc["range"]["from"])
        hi = date.fromisoformat(doc["range"]["to"])
        evs = desk_events.macro_events(lo, hi, path=path)
        gaps = desk_events.macro_gaps(path=path)
    except (OSError, desk_events.EventsError, KeyError, ValueError):
        return None
    return MacroCalendar(tuple(evs), (lo, hi), frozenset(gaps))


@dataclass(frozen=True)
class EventState:
    window_end: date | None
    earnings: str  # within | none | unknown | n/a (ETF)
    earnings_detail: str
    holdings: str  # within | none | unknown | unmapped (ETF without a map) | n/a (stock)
    holdings_detail: str
    macro: str  # within | none | unknown
    macro_events: tuple[tuple[str, str], ...]
    next_macro: date | None


def _report_state(
    reports: Sequence[str] | None, session: date, end: date, cal: Calendar
) -> tuple[str, str]:
    if reports is None:
        return "unknown", "earnings schedule unavailable"
    sessions = cal.sessions()
    for rep in sorted(set(reports)):
        try:
            d = date.fromisoformat(rep)
        except (TypeError, ValueError):
            continue
        i = bisect.bisect_left(sessions, d)
        if i + 1 >= len(sessions):
            continue
        first, second = sessions[i], sessions[i + 1]  # the event pair (either timing)
        if second > session and first <= end:
            return "within", f"report {rep} (event sessions {first}..{second})"
    status, why = schedule_status(reports, session, end, cal)
    return ("none", "") if status == "complete" else ("unknown", why)


def events_state(
    name: str,
    session: date,
    cal: Calendar,
    policy: EventPolicy,
    *,
    schedule: Mapping[str, Sequence[str]] | None,
    macro: MacroCalendar | None,
) -> EventState:
    try:
        end: date | None = cal.nth_after(session, policy.window_sessions)
    except (ValueError, IndexError, KeyError):
        end = None
    if end is None:
        why = "the calendar does not cover the event window"
        return EventState(None, "unknown", why, "unknown", why, "unknown", (), None)

    def reports(n: str) -> Sequence[str] | None:
        return None if schedule is None else tuple(schedule.get(n, ()))

    if name in PANEL_ETFS:
        earnings, e_detail = "n/a", "an ETF has no earnings of its own"
        holdings_map = desk_events.ETF_HOLDINGS.get(name)
        if holdings_map is None:
            holdings, h_detail = "unmapped", "no large-holdings map for this ETF"
        else:
            states = {h: _report_state(reports(h), session, end, cal) for h in holdings_map}
            within = [f"{h}: {d}" for h, (s, d) in states.items() if s == "within"]
            unknown = [h for h, (s, _d) in states.items() if s == "unknown"]
            if within:
                holdings, h_detail = "within", "; ".join(within)
            elif unknown:
                holdings, h_detail = "unknown", f"schedule not pinned for {unknown}"
            else:
                holdings, h_detail = "none", ""
    else:
        earnings, e_detail = _report_state(reports(name), session, end, cal)
        holdings, h_detail = "n/a", ""

    macro_state, found, nxt = _macro_state(session, end, policy, macro)
    return EventState(end, earnings, e_detail, holdings, h_detail, macro_state, found, nxt)


def _macro_state(
    session: date, end: date, policy: EventPolicy, macro: MacroCalendar | None
) -> tuple[str, tuple[tuple[str, str], ...], date | None]:
    if macro is None:
        return "unknown", (), None
    found = tuple(
        (e.date.isoformat(), e.kind)
        for e in macro.events
        if e.kind in policy.macro_kinds and session < e.date <= end
    )
    if found:
        return "within", found, date.fromisoformat(found[0][0])
    lo, hi = macro.covered
    if session < lo or end > hi:
        return "unknown", (), None  # silence outside the sealed range is no evidence
    years = {str(y) for y in range(session.year, end.year + 1)}
    for gap in macro.gaps:
        kind, _, year = gap.partition(" ")
        if kind in policy.macro_kinds and year in years:
            return "unknown", (), None  # a declared gap (e.g. CPI not yet published)
    return "none", (), None


# ------------------------------------------------------------- direction


@dataclass(frozen=True)
class Directions:
    status: str  # ok | missing | session_mismatch | malformed
    bull: dict[str, tuple[str, ...]] = field(default_factory=dict)
    no_options_expression: tuple[str, ...] = ()

    def of(self, name: str) -> str:
        if self.status != "ok":
            return "unknown"
        return "bull" if name in self.bull else "none"


def directions(doc: Mapping[str, Any] | None, session: date) -> Directions:
    """Bull names of the session from its signals file (the eod-equity job's
    ``signals/<D>.json``). Only the two allowed direction keys are read."""
    if doc is None:
        return Directions("missing")
    if not isinstance(doc, Mapping) or doc.get("session") != session.isoformat():
        return Directions("session_mismatch")
    xs, pead = doc.get("xsmom"), doc.get("pead")
    if not isinstance(xs, Mapping) or not isinstance(pead, list):
        return Directions("malformed")
    fires, top3 = xs.get("fires"), xs.get("top3")
    if type(fires) is not bool or not isinstance(top3, list):
        return Directions("malformed")
    bull: dict[str, list[str]] = {}
    no_expr: list[str] = []

    def point(name: Any, signal: str) -> bool:
        if not isinstance(name, str) or not name:
            return False
        require_direction_signal(signal)  # only the allowed signals point
        bull.setdefault(name, []).append(signal)
        return True

    if fires:
        for name in top3:
            if name in NO_OPTIONS_EXPRESSION:
                no_expr.append(name)  # logged; the 4th-ranked name is never promoted
            elif not point(name, "xsmom_top3"):
                return Directions("malformed")
    for beat in pead:
        if not isinstance(beat, Mapping) or not point(beat.get("name"), "pead_beat"):
            return Directions("malformed")
    assert all(set(s) <= ALLOWED_DIRECTION for s in bull.values())
    return Directions("ok", {n: tuple(s) for n, s in sorted(bull.items())}, tuple(no_expr))


# ---------------------------------------------------------------- regime


@dataclass(frozen=True)
class NameConditions:
    name: str
    session: date
    direction: str  # bull | none | unknown (never bear)
    direction_sources: tuple[str, ...]
    vol: VolState
    term: TermState
    market: MarketTerm
    events: EventState
    news_flags: tuple[str, ...]
    news_source: str  # absent | model
    liquidity_score: int | None


@dataclass(frozen=True)
class Regime:
    session: date
    signals_status: str
    market: MarketTerm
    names: dict[str, NameConditions]
    no_options_expression: tuple[str, ...]


def conditions_at(
    session: date,
    cal: Calendar,
    pb: Playbook,
    *,
    names: Sequence[str],
    signals_doc: Mapping[str, Any] | None,
    features: Mapping[date, Mapping[str, Any]],
    indices: Mapping[str, Mapping[date, float]],
    schedule: Mapping[str, Sequence[str]] | None,
    macro: MacroCalendar | None,
    news_flags: Mapping[str, Sequence[str]] | None,
) -> Regime:
    """Every name's conditions on ``session``. ``features`` maps sessions to
    desk-features documents (only D and the history window before it are
    read); ``schedule`` holds each name's known report dates (sealed +
    timing file; None = unavailable); ``news_flags`` None = no news model."""
    dirs = directions(signals_doc, session)
    market = market_term(session, indices, pb.term)
    today = _names_of(features.get(session), pb.vol_state.features_schema) or {}
    out: dict[str, NameConditions] = {}
    for name in names:
        ev = events_state(name, session, cal, pb.events, schedule=schedule, macro=macro)
        entry = today.get(name)
        liq = entry.get("liquidity_score") if isinstance(entry, Mapping) else None
        flags = () if news_flags is None else tuple(news_flags.get(name, ()))
        out[name] = NameConditions(
            name=name,
            session=session,
            direction=dirs.of(name),
            direction_sources=dirs.bull.get(name, ()),
            vol=vol_state(name, session, cal, features, pb.vol_state),
            term=term_state(name, session, cal, features, pb, next_event=ev.next_macro),
            market=market,
            events=ev,
            news_flags=flags,
            news_source="absent" if news_flags is None else "model",
            liquidity_score=liq if type(liq) is int else None,
        )
    return Regime(session, dirs.status, market, out, dirs.no_options_expression)


# ---------------------------------------------------------------- match


@dataclass(frozen=True)
class RowMatch:
    row_id: str
    matched: bool
    reasons: tuple[str, ...]


def match_row(row: Row, c: NameConditions, *, book_over_delta_cap: bool | None) -> RowMatch:
    """Whether the name's conditions meet the row's ``when``, universe and
    liquidity filter; every unmet (or not evaluable) condition is a reason.
    ``book_over_delta_cap`` is the book-level input of the hedge row
    (None = unknown: never matched)."""
    if row.status != "active":
        return RowMatch(row.id, False, ("dormant",))
    w = row.when
    why: list[str] = []
    if c.name not in row.universe.names:
        why.append("universe: not in the row's universe")
    if c.liquidity_score is None or c.liquidity_score < row.universe.min_liquidity_score:
        why.append(f"liquidity: score {c.liquidity_score} < {row.universe.min_liquidity_score}")
    if w.direction == "bull":
        if c.direction != "bull" or not set(c.direction_sources) & set(w.signals):
            why.append(
                f"direction: {c.direction} {list(c.direction_sources)}, needs {list(w.signals)}"
            )
    elif w.direction == "none" and c.direction != "none":
        why.append(f"direction: {c.direction}, needs none")
    if w.vol is not None:
        if c.vol.state not in w.vol:
            why.append(f"vol: {c.vol.state} {c.vol.reason}".rstrip())
        if row.iv_fidelity == "validated_only" and c.vol.band_name != "validated":
            why.append("fidelity: the row takes validated-IV names only")
    if w.name_term == "steep_contango":
        if c.term.steep != "yes":
            why.append(f"term: steep {c.term.steep} {c.term.steep_reason}".rstrip())
    elif w.name_term != "any" and c.term.state != w.name_term:
        why.append(f"term: {c.term.state}, needs {w.name_term}")
    if w.market_term != "any" and c.market.state != w.market_term:
        why.append(f"market term: {c.market.state}, needs {w.market_term}")
    ev = c.events
    if w.events == "no_event":
        quiet = (
            ev.earnings in ("none", "n/a")
            and ev.holdings in ("none", "unmapped", "n/a")
            and ev.macro == "none"
        )
        if not quiet:
            why.append(f"events: earnings {ev.earnings}, holdings {ev.holdings}, macro {ev.macro}")
    elif w.events == "macro_event_ahead" and ev.macro != "within":
        why.append(f"events: macro {ev.macro}, needs an event ahead")
    if w.front_over_back == "required" and c.term.event_inversion != "yes":
        why.append(f"front over back: {c.term.event_inversion}")
    if w.book == "net_delta_over_cap" and book_over_delta_cap is not True:
        why.append(f"book: over the delta cap is {book_over_delta_cap}")
    if w.news == "block_if_flagged" and c.news_flags:
        why.append(f"news: flagged {list(c.news_flags)}")
    return RowMatch(row.id, not why, tuple(why))


def match_rows(
    pb: Playbook, c: NameConditions, *, book_over_delta_cap: bool | None
) -> list[RowMatch]:
    return [match_row(r, c, book_over_delta_cap=book_over_delta_cap) for r in pb.rows]


def _frac(f: Fraction | None) -> str | None:
    return None if f is None else f"{f.numerator}/{f.denominator}"


def regime_doc(res: Regime, pb: Playbook, *, book_over_delta_cap: bool | None) -> dict[str, Any]:
    """The regime as a JSON document (schema desk-regime/1)."""
    names: dict[str, Any] = {}
    for name, c in sorted(res.names.items()):
        v, t, e = c.vol, c.term, c.events
        names[name] = {
            "direction": {"state": c.direction, "sources": list(c.direction_sources)},
            "vol": {
                "state": v.state,
                "reason": v.reason,
                "ratio": v.ratio,
                "percentile": _frac(v.percentile),
                "n": v.n,
                "band": v.band_name,
                "cheap_below": _frac(v.band.cheap_below),
                "rich_above": _frac(v.band.rich_above),
            },
            "term": {
                "state": t.state,
                "reason": t.reason,
                "slope": t.slope,
                "steep": t.steep,
                "steep_reason": t.steep_reason,
                "steep_percentile": _frac(t.steep_percentile),
                "steep_n": t.steep_n,
                "event_inversion": t.event_inversion,
                "event_detail": t.event_detail,
            },
            "events": {
                "window_end": e.window_end.isoformat() if e.window_end else None,
                "earnings": e.earnings,
                "earnings_detail": e.earnings_detail,
                "holdings": e.holdings,
                "holdings_detail": e.holdings_detail,
                "macro": e.macro,
                "macro_events": [list(x) for x in e.macro_events],
            },
            "news": {"source": c.news_source, "flags": list(c.news_flags)},
            "liquidity_score": c.liquidity_score,
            "rows": {
                m.row_id: {"matched": m.matched, "reasons": list(m.reasons)}
                for m in match_rows(pb, c, book_over_delta_cap=book_over_delta_cap)
            },
        }
    m = res.market
    return {
        "schema": SCHEMA,
        "session": res.session.isoformat(),
        "playbook": pb.version,
        "playbook_sha256": pb.sha256,
        "signals_status": res.signals_status,
        "no_options_expression": list(res.no_options_expression),
        "market_term": {"state": m.state, "reason": m.reason, "front": m.front, "back": m.back},
        "book_over_delta_cap": book_over_delta_cap,
        "names": names,
    }
