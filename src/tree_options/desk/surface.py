"""Vol-surface features from one recorded chain (plan D4).

Pure functions over a desk chain document (``chains/<D>/<SYM>.json.gz``,
schema desk-chain/1) plus :func:`build_features`, which the CLI
``python -m tree_options.desk features --session D`` writes to
``DESK_STORE/features/<D>.json``.

Every IV here is our own: the mid ``(bid+ask)/2`` inverted with
``massive_derived.implied_vol`` (the hash-pinned ``bs_price``; q = 0,
declared), and deltas are the analytic BS deltas at that IV. The vendor's
iv/delta columns are recorded, never used. Per name:

* ATM IV per expiry (call/put mean per strike, interpolated in ln(K/F),
  the IVHIST-001 rule) and constant maturity 30/60/90/180 days by
  total-variance interpolation (never extrapolated);
* 25-delta skew (put IV - call IV, IV interpolated in delta) per expiry,
  and at 30/90 days linearly in days-to-expiry;
* term slope IV90/IV30 - 1;
* implied earnings move by the two-expiry variance split over the first two
  expiries on/after the event pair of the next report, with the historical
  two-session event moves beside it;
* liquidity score: contracts 30..240 DTE with |delta| 0.2..0.8, OI >= 500
  and spread <= 5% of mid;
* IV rank/percentile of IV30 against the VWAP IV history (trailing 252
  sessions, n shown, flagged under 120; the history's IVHIST-001 label is
  carried, and the method mismatch, chain mids vs VWAP, is named);
* VRP = IV30 - sqrt(annualized 20-session variance forecast): HAR and the
  naive RV22 both shown; the desk's ``vrp`` follows FORECAST-001's verdict.
"""

from __future__ import annotations

import bisect
import gzip
import itertools
import json
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from tree_options.desk import har, rv
from tree_options.desk.ivhist import (
    MONEYNESS_BAND,
    StrikeIV,
    atm_iv,
    days_between,
    history_series,
    solve_iv,
    total_variance_interp,
)
from tree_options.desk.sessions import Calendar, previous_session
from tree_options.synth_options.greeks import bs_abs_delta

FEATURES_SCHEMA = "desk-features/1"
CM_DAYS: tuple[int, ...] = (30, 60, 90, 180)
SKEW_DAYS: tuple[int, ...] = (30, 90)
SKEW_DELTA = 0.25
SOLVE_BAND = 0.5  # |ln(K/F)| beyond this is never solved (no feature reads it)
LIQ_DTE = (30, 240)
LIQ_ABS_DELTA = (0.2, 0.8)
LIQ_MIN_OI = 500
LIQ_MAX_SPREAD = 0.05
RANK_WINDOW = 252
RANK_MIN_N = 120
VRP_H = 20
DIVIDEND_YIELD = 0.0


# ------------------------------------------------------------------ quotes


@dataclass(frozen=True)
class Quote:
    expiry: date
    dte: int
    right: str
    strike: float
    bid: float | None
    ask: float | None
    mid: float | None
    oi: int | None
    iv: float | None
    delta: float | None  # signed: calls > 0, puts < 0


def _num(x: Any) -> float | None:
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    return float(x) if math.isfinite(x) else None


def chain_spot(doc: Mapping[str, Any]) -> float | None:
    quote = doc.get("header", {}).get("underlying_quote") or {}
    for key in ("close", "current_price"):
        v = _num(quote.get(key))
        if v is not None and v > 0.0:
            return v
    return None


def chain_quotes(doc: Mapping[str, Any], *, session: date, spot: float, rate: float) -> list[Quote]:
    cols = doc["columns"]
    out: list[Quote] = []
    for i in range(len(cols["occ"])):
        expiry = date.fromisoformat(cols["exp"][i])
        dte = days_between(session, expiry)
        if dte < 1:
            continue
        strike = float(cols["strike"][i])
        right = cols["right"][i]
        bid, ask = _num(cols["bid"][i]), _num(cols["ask"][i])
        mid = (bid + ask) / 2.0 if bid is not None and ask is not None and 0 < bid <= ask else None
        oi_raw = _num(cols["oi"][i])
        iv = delta = None
        fwd = spot * math.exp((rate - DIVIDEND_YIELD) * dte / 365.0)
        if mid is not None and abs(math.log(strike / fwd)) <= SOLVE_BAND:
            iv = solve_iv(
                mid, spot=spot, strike=strike, dte=dte, right=right, rate=rate, q=DIVIDEND_YIELD
            )
            if iv is not None:
                d = bs_abs_delta(
                    spot=spot,
                    strike=strike,
                    dte_calendar_days=dte,
                    iv=iv,
                    risk_free=rate,
                    dividend_yield=DIVIDEND_YIELD,
                    call_put=right,
                )
                delta = d if right == "C" else -d
        out.append(
            Quote(
                expiry,
                dte,
                right,
                strike,
                bid,
                ask,
                mid,
                int(oi_raw) if oi_raw is not None else None,
                iv,
                delta,
            )
        )
    return out


def _by_expiry(quotes: Iterable[Quote]) -> dict[date, list[Quote]]:
    out: dict[date, list[Quote]] = {}
    for q in quotes:
        out.setdefault(q.expiry, []).append(q)
    return dict(sorted(out.items()))


# -------------------------------------------------------------------- term


@dataclass(frozen=True)
class TermPoint:
    expiry: date
    dte: int
    iv: float
    n_strikes: int
    how: str  # bracket | one-sided


def atm_term(quotes: Sequence[Quote], *, spot: float, rate: float) -> list[TermPoint]:
    out = []
    for expiry, qs in _by_expiry(quotes).items():
        dte = qs[0].dte
        fwd = spot * math.exp((rate - DIVIDEND_YIELD) * dte / 365.0)
        sides: dict[float, dict[str, float]] = {}
        for q in qs:
            if q.iv is not None:
                sides.setdefault(q.strike, {})[q.right] = q.iv
        strikes = [
            StrikeIV(k, (s["C"] + s["P"]) / 2.0, s["C"], s["P"])
            for k, s in sorted(sides.items())
            if "C" in s and "P" in s and abs(math.log(k / fwd)) <= MONEYNESS_BAND
        ]
        atm = atm_iv(strikes, fwd)
        if atm is not None:
            out.append(TermPoint(expiry, dte, atm[0], len(strikes), atm[1]))
    return out


def constant_maturity(term: Sequence[TermPoint]) -> dict[int, float | None]:
    pts = [(float(p.dte), p.iv) for p in term]
    return {d: total_variance_interp(pts, float(d)) for d in CM_DAYS}


def term_slope(iv: Mapping[int, float | None]) -> float | None:
    a, b = iv.get(30), iv.get(90)
    return b / a - 1.0 if a and b else None


# -------------------------------------------------------------------- skew


def interp_at_delta(points: Sequence[tuple[float, float]], target: float) -> float | None:
    pts = sorted(points)
    for (d1, v1), (d2, v2) in itertools.pairwise(pts):
        if d1 <= target <= d2:
            return v1 if d2 == d1 else v1 + (v2 - v1) * (target - d1) / (d2 - d1)
    return None


def skew_by_expiry(quotes: Sequence[Quote]) -> dict[date, float]:
    """25-delta put IV minus 25-delta call IV per expiry."""
    out = {}
    for expiry, qs in _by_expiry(quotes).items():
        puts: list[tuple[float, float]] = []
        calls: list[tuple[float, float]] = []
        for q in qs:
            if q.delta is not None and q.iv is not None:
                (calls if q.right == "C" else puts).append((q.delta, q.iv))
        p = interp_at_delta(puts, -SKEW_DELTA)
        c = interp_at_delta(calls, SKEW_DELTA)
        if p is not None and c is not None:
            out[expiry] = p - c
    return out


def skew_at(skews: Mapping[date, float], dtes: Mapping[date, int]) -> dict[int, float | None]:
    """Skew at 30/90 days, linear in days-to-expiry between bracketing expiries."""
    pts = sorted((dtes[e], s) for e, s in skews.items() if e in dtes)
    out: dict[int, float | None] = {}
    for d in SKEW_DAYS:
        out[d] = None
        for (t1, s1), (t2, s2) in itertools.pairwise(pts):
            if t1 <= d <= t2:
                out[d] = s1 if t2 == t1 else s1 + (s2 - s1) * (d - t1) / (t2 - t1)
                break
        else:
            exact = [s for t, s in pts if t == d]
            out[d] = exact[0] if exact else None
    return out


# ---------------------------------------------------------------- earnings


def _event_pair(report: str, cal: Calendar) -> tuple[date, date] | None:
    try:
        d = date.fromisoformat(report)
    except (TypeError, ValueError):
        return None
    sessions = cal.sessions()
    i = bisect.bisect_left(sessions, d)
    if i + 1 >= len(sessions):
        return None
    return sessions[i], sessions[i + 1]


# Feasibility tolerances of the two-expiry split: a jump variance within
# (0.1% move)^2 of zero is "no premium"; a background variance more
# negative than (1 vol point)^2 annualized, or a jump variance more
# negative than (0.1% move)^2, is an impossible decomposition (no move).
TOL_JUMP_VAR = 1e-6
TOL_BACKGROUND_VAR = 1e-4


def implied_event_move(
    term: Sequence[TermPoint],
    reports: Iterable[str],
    session: date,
    cal: Calendar,
    *,
    reporter: bool = True,
) -> dict[str, Any]:
    """Two-expiry variance split around the next report whose event pair is
    not fully past: w_i = iv_i^2 T_i = s^2 T_i + J^2 for the first two
    expiries on/after the pair's second session -> J^2 = (w1 T2 - w2 T1)/(T2 - T1).

    The split assumes both expiries hold the SAME single event over a
    constant background variance s^2 = (w2 - w1)/(T2 - T1). It returns no
    move (``implied_move: None`` with the reason) when another known report
    lands before the second expiry, when s^2 is materially negative, or
    when J^2 is. A reporter with no known report ahead is ``schedule:
    incomplete`` (unknown), never "no event"."""
    known = sorted(set(reports))
    upcoming = None
    for rep in known:
        pair = _event_pair(rep, cal)
        if pair is not None and pair[1] > session:
            upcoming = (rep, pair)
            break
    if upcoming is None:
        if not reporter:
            return {"next_report": None}
        return {
            "next_report": None,
            "schedule": "incomplete",
            "reason": "no known report ahead of the session (schedule incomplete)",
        }
    rep, (s, nxt) = upcoming
    out: dict[str, Any] = {
        "next_report": rep,
        "event_sessions": [s.isoformat(), nxt.isoformat()],
        "in_progress": s <= session,
    }
    after = sorted((p for p in term if p.expiry >= nxt), key=lambda p: p.dte)
    if len(after) < 2:
        out.update(implied_move=None, reason="fewer than two expiries after the event")
        return out
    p1, p2 = after[0], after[1]
    out["expiries"] = [p1.expiry.isoformat(), p2.expiry.isoformat()]
    for other in known:
        pair = _event_pair(other, cal)
        if other != rep and pair is not None and s < pair[0] <= p2.expiry:
            out.update(
                implied_move=None,
                reason=f"a second report ({other}) falls before the second expiry "
                f"{p2.expiry.isoformat()}: the expiries do not hold the same single event",
            )
            return out
    t1, t2 = p1.dte / 365.0, p2.dte / 365.0
    w1, w2 = p1.iv**2 * t1, p2.iv**2 * t2
    j2 = (w1 * t2 - w2 * t1) / (t2 - t1)
    ex2 = (w2 - w1) / (t2 - t1)
    out["background_var"] = ex2
    out["jump_var"] = j2
    if ex2 < -TOL_BACKGROUND_VAR:
        out.update(
            implied_move=None,
            ex_event_vol=None,
            reason=f"negative background variance {ex2:.6g} (impossible decomposition)",
        )
        return out
    out["ex_event_vol"] = math.sqrt(max(ex2, 0.0))
    if j2 < -TOL_JUMP_VAR:
        out.update(
            implied_move=None, reason=f"negative jump variance {j2:.6g} (impossible decomposition)"
        )
        return out
    if j2 <= TOL_JUMP_VAR:
        out.update(implied_move=0.0, implied_mean_abs_move=0.0, flag="no_event_premium")
        return out
    j = math.sqrt(j2)
    out.update(implied_move=j, implied_mean_abs_move=j * math.sqrt(2.0 / math.pi))
    return out


def historical_event_moves(
    bars: Mapping[str, Mapping[str, Any]], reports: Iterable[str], session: date, cal: Calendar
) -> dict[str, Any]:
    """|ln(C after the pair / C before it)| over every fully past report."""
    moves = []
    for rep in sorted(set(reports)):
        pair = _event_pair(rep, cal)
        if pair is None or pair[1] > session:
            continue
        prev = previous_session(pair[0], cal)
        if prev is None:
            continue
        a, b = bars.get(prev.isoformat()), bars.get(pair[1].isoformat())
        if a is None or b is None:
            continue
        try:
            ca, cb = float(a["close"]), float(b["close"])
        except (TypeError, KeyError, ValueError):
            continue
        if ca > 0.0 and cb > 0.0:
            moves.append(abs(math.log(cb / ca)))
    return {"n": len(moves), "mean_abs_move": sum(moves) / len(moves) if moves else None}


# ------------------------------------------------------- liquidity, rank, vrp


def liquidity_score(quotes: Sequence[Quote]) -> int:
    n = 0
    for q in quotes:
        if not LIQ_DTE[0] <= q.dte <= LIQ_DTE[1]:
            continue
        if q.delta is None or not LIQ_ABS_DELTA[0] <= abs(q.delta) <= LIQ_ABS_DELTA[1]:
            continue
        if q.oi is None or q.oi < LIQ_MIN_OI:
            continue
        if q.mid is None or q.bid is None or q.ask is None or q.mid <= 0.0:
            continue
        if q.ask - q.bid <= LIQ_MAX_SPREAD * q.mid:
            n += 1
    return n


def iv_rank(
    current: float, history: Mapping[date, float], session: date, cal: Calendar
) -> dict[str, Any]:
    """Rank and percentile of ``current`` against the history's values over
    the RANK_WINDOW sessions strictly before ``session``. The rank clamps
    to [0, 1]; ``outside_range`` says when the current value (chain mids)
    left the history's (VWAP) range."""
    sessions = cal.sessions()
    i = bisect.bisect_left(sessions, session)
    vals = [history[d] for d in sessions[max(0, i - RANK_WINDOW) : i] if d in history]
    n = len(vals)
    rank: float | None = None
    outside: str | None = None
    if vals:
        lo, hi = min(vals), max(vals)
        outside = "below" if current < lo else "above" if current > hi else None
        if hi > lo:
            rank = min(1.0, max(0.0, (current - lo) / (hi - lo)))
    pct = sum(1 for v in vals if v < current) / n if n else None
    return {
        "n": n,
        "rank": rank,
        "percentile": pct,
        "low_n": n < RANK_MIN_N,
        "outside_range": outside,
    }


def vrp(iv: float, var_h: float, calendar_days: int) -> float:
    """IV minus the forecast vol annualized on the window's calendar days."""
    return iv - math.sqrt(var_h * 365.0 / calendar_days)


# ----------------------------------------------------------------- session


def name_features(
    doc: Mapping[str, Any],
    *,
    session: date,
    rate: float,
    cal: Calendar,
    reports: Sequence[str],
    bars: Mapping[str, Mapping[str, Any]] | None,
    iv_history: Mapping[date, float],
    history_label: str,
    forecast: har.PointForecast | None,
    forecast_note: str,
    reporter: bool = True,
) -> dict[str, Any]:
    spot = chain_spot(doc)
    if spot is None:
        return {"status": "NOT_EVALUABLE", "reason": "chain has no underlying close"}
    quotes = chain_quotes(doc, session=session, spot=spot, rate=rate)
    term = atm_term(quotes, spot=spot, rate=rate)
    iv = constant_maturity(term)
    dtes = {p.expiry: p.dte for p in term}
    for q in quotes:
        dtes.setdefault(q.expiry, q.dte)
    skew = skew_at(skew_by_expiry(quotes), dtes)
    out: dict[str, Any] = {
        "spot": spot,
        "rate": rate,
        "atm_term": [[p.expiry.isoformat(), p.dte, p.iv, p.n_strikes, p.how] for p in term],
        "iv": {str(d): v for d, v in iv.items()},
        "skew25": {str(d): v for d, v in skew.items()},
        "term_slope": term_slope(iv),
        "liquidity_score": liquidity_score(quotes),
    }
    ev = implied_event_move(term, reports, session, cal, reporter=reporter)
    if bars is not None and reports:
        hist = historical_event_moves(bars, reports, session, cal)
        ev["hist_n"] = hist["n"]
        ev["hist_mean_abs_move"] = hist["mean_abs_move"]
    out["earnings"] = ev
    iv30 = iv[30]
    if iv30 is None:
        out["iv_rank"] = {"status": "NOT_EVALUABLE", "reason": "no IV30 on this chain"}
    elif not iv_history:
        out["iv_rank"] = {"status": "NOT_EVALUABLE", "reason": "no IV history for this name"}
    else:
        out["iv_rank"] = {
            **iv_rank(iv30, iv_history, session, cal),
            "history_label": history_label,
            "history_method": "IVHIST-001 VWAP ATM 30d",
            "current_method": "chain mid ATM 30d",
        }
    if bars is not None:
        iso = [s.isoformat() for s in cal.sessions() if s <= session][-(rv.TRADING_DAYS + 1) :]
        out["yz22_ann"] = rv.yang_zhang_annualized(bars, iso, len(iso) - 1)
    out["forecast"] = forecast_block(iv30, forecast, session, cal, forecast_note)
    return out


def forecast_block(
    iv30: float | None,
    fc: har.PointForecast | None,
    session: date,
    cal: Calendar,
    note: str,
) -> dict[str, Any]:
    source = har.forecast_source()
    block: dict[str, Any] = {
        "source": source,
        "forecast_001_verdict": har.FORECAST_001_VERDICT,
        "har_status": "validated" if source == "har" else "unvalidated",
        "h": VRP_H,
    }
    if fc is None:
        block.update(status="NOT_EVALUABLE", reason=note or "no forecast for this session")
        return block
    days = days_between(session, cal.nth_after(session, VRP_H))
    block.update(
        window_calendar_days=days,
        har_var=fc.forecast,
        rv22_var=fc.rv22,
        ewma_var=fc.ewma,
        har_vol=math.sqrt(fc.forecast * 365.0 / days),
        rv22_vol=math.sqrt(fc.rv22 * 365.0 / days) if fc.rv22 is not None else None,
        n_earn=fc.n_earn,
        schedule=fc.schedule,
        schedule_reason=fc.schedule_reason,
        fit_through=fc.fit_through.isoformat(),
        be_estimated=fc.be_estimated,
    )
    # an unknown earnings schedule leaves n_earn unknown: the HAR forecast
    # is degraded and never the desk's selected VRP
    degraded = fc.schedule in ("incomplete", "unavailable")
    if degraded:
        block["har_status"] = "degraded"
    if iv30 is not None:
        block["vrp_har"] = vrp(iv30, fc.forecast, days)
        block["vrp_rv22"] = vrp(iv30, fc.rv22, days) if fc.rv22 is not None else None
        if degraded and source == "har":
            block["vrp"] = None
            block["vrp_withheld"] = f"earnings schedule {fc.schedule}: {fc.schedule_reason}"
        else:
            block["vrp"] = block[f"vrp_{source}"]
    return block


def read_chain_file(path: Path) -> dict[str, Any]:
    doc = json.loads(gzip.decompress(path.read_bytes()))
    if not isinstance(doc, dict) or not isinstance(doc.get("columns"), dict):
        raise ValueError(f"{path.name}: not a desk chain document")
    return doc


def build_features(
    session: date,
    *,
    chains: Mapping[str, Path],
    cal: Calendar,
    rate: float,
    rate_label: str,
    panel: Mapping[str, Mapping[str, Mapping[str, Any]]] | None,
    panel_sha256: str | None,
    har_names: Sequence[str],
    earnings: Mapping[str, Sequence[str]],
    earnings_sha256: str | None,
    history: Mapping[str, Any] | None,
    history_sha256: str | None,
    labels: Mapping[str, str],
    warnings: Sequence[str] = (),
    schedule: Mapping[str, Sequence[str]] | None = None,
    schedule_available: bool = True,
    timing_sha256: str | None = None,
) -> dict[str, Any]:
    """``earnings`` is the sealed calendar (the HAR fit, as FORECAST-001
    specifies); ``schedule`` the forward report dates (sealed plus the
    timing file; default: ``earnings``). ``schedule_available=False`` (the
    sealed calendar could not be read) makes every reporter's schedule
    unavailable: its HAR forecast is degraded, never a zero-event one."""
    notes = list(warnings)
    fwd: Mapping[str, Sequence[str]] = (
        (schedule if schedule is not None else earnings) if schedule_available else {}
    )
    forecasts: dict[str, har.PointForecast] = {}
    note = ""
    if panel is None:
        note = "panel unavailable"
    else:
        forecasts = har.forecasts_at(
            panel, earnings, cal, har_names, session, VRP_H, forward_schedule=fwd
        )
        if not forecasts:
            note = f"no HAR forecast for {session} (panel lacks the session or history)"
    names: dict[str, Any] = {}
    inputs: dict[str, Any] = {}
    for sym, path in sorted(chains.items()):
        try:
            doc = read_chain_file(path)
        except (OSError, ValueError) as exc:
            names[sym] = {"status": "NOT_EVALUABLE", "reason": f"unreadable chain ({exc})"}
            continue
        inputs[sym] = doc.get("header", {}).get("raw_sha256")
        hist = {d: v for d, (v, _m) in history_series(history or {}, sym).items()}
        names[sym] = name_features(
            doc,
            session=session,
            rate=rate,
            cal=cal,
            reports=list(fwd.get(sym, ())),
            bars=panel.get(sym) if panel is not None else None,
            iv_history=hist,
            history_label=labels.get(sym, "unlabeled"),
            forecast=forecasts.get(sym),
            forecast_note=note,
            reporter=sym not in har.ETF_NAMES,
        )
    return {
        "schema": FEATURES_SCHEMA,
        "session": session.isoformat(),
        "inputs": {
            "chains_raw_sha256": inputs,
            "panel_sha256": panel_sha256,
            "earnings_calendar_sha256": earnings_sha256,
            "earnings_timing_sha256": timing_sha256,
            "iv_history_sha256": history_sha256,
            "rate": rate,
            "rate_source": rate_label,
            "forecast_001_verdict": har.FORECAST_001_VERDICT,
            "dividend_yield": DIVIDEND_YIELD,
        },
        "warnings": notes,
        "names": names,
    }
