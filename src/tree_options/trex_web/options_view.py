"""Recorded per-name options surface for the cockpit symbol pages.

The viewer's live chain envelope is Phase 4's lane; this module serves what
the desk has already RECORDED — ``features/<D>.json`` (schema
desk-features/1) for the cards and the ATM term, plus the immutable
``chains/<D>/<SYM>.json.gz`` snapshot (schema desk-chain/1) for the slice
rows around ATM. Everything is read-only and nullable: a name the store has
never seen answers ``available: false`` with a 200, a missing chain degrades
to cards with ``slice: null``, and no value is ever invented.

``iv30_history`` comes from ``iv-history/vwap_atm.json`` (IVHIST-001, a
manual build whose last session trails the chain store): the ``first`` /
``last`` fields exist so the UI can disclose that staleness — it is not
"fixed" here. ``live`` is parked at null for Phase 4 so the client contract
is stable from day one.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

from tree_options.desk.store import read_chain
from tree_options.trex.series import decimate_pairs, level_extent
from tree_options.trex_web.symbol_history import _ts_ms, history_age_seconds

DEFAULT_WINDOW = 5
MIN_WINDOW, MAX_WINDOW = 0, 15
DEFAULT_MAX_EXPIRIES = 6
MIN_MAX_EXPIRIES, MAX_MAX_EXPIRIES = 1, 12
FEATURES_LOOKBACK = 10  # sessions walked back for a features+chain match
IV30_MAX_POINTS = 400
IV30_SOURCE = "iv-history/vwap_atm.json (IVHIST-001, manual build)"
CARD_KEYS = (
    "iv",
    "iv_rank",
    "skew25",
    "term_slope",
    "yz22_ann",
    "liquidity_score",
    "earnings",
)


def clamp_params(window: int, max_expiries: int) -> tuple[int, int]:
    """:(window, max_expiries) clamped to their sane ranges."""
    w = max(MIN_WINDOW, min(MAX_WINDOW, int(window)))
    m = max(MIN_MAX_EXPIRIES, min(MAX_MAX_EXPIRIES, int(max_expiries)))
    return w, m


def _num(v: Any) -> float | None:
    """A JSON number as float; null (and anything not a number) passes as None."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v)


def _int(v: Any) -> int | None:
    if isinstance(v, bool) or not isinstance(v, int):
        return None
    return v


def _at(cols: dict[str, Any], name: str, i: int) -> Any:
    col = cols.get(name)
    return col[i] if isinstance(col, list) and i < len(col) else None


# ---------------------------------------------------------------- discovery


def _feature_sessions(features_dir: Path) -> list[str]:
    """Dated feature files, newest first (odd filenames ignored)."""
    out: list[str] = []
    if not features_dir.is_dir():
        return out
    for p in features_dir.glob("*.json"):
        try:
            out.append(date.fromisoformat(p.stem).isoformat())
        except ValueError:
            continue
    return sorted(out, reverse=True)


def _read_features(path: Path) -> dict[str, Any] | None:
    try:
        doc = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def _find_features(store_root: Path, sym: str) -> list[tuple[str, dict[str, Any]]]:
    """Sessions (newest first, <= FEATURES_LOOKBACK) whose features carry sym."""
    features_dir = store_root / "features"
    found: list[tuple[str, dict[str, Any]]] = []
    for d in _feature_sessions(features_dir)[:FEATURES_LOOKBACK]:
        doc = _read_features(features_dir / f"{d}.json")
        name = doc.get("names", {}).get(sym) if doc else None
        if isinstance(name, dict):
            found.append((d, name))
    return found


def _read_chain_quiet(store_root: Path, sym: str, session: str) -> dict[str, Any] | None:
    path = store_root / "chains" / session / f"{sym}.json.gz"
    try:
        return read_chain(path)
    except (OSError, ValueError):
        return None


# ------------------------------------------------------------------- slice


def _row(
    cols: dict[str, Any],
    i: int,
    exp: str,
    dte: int,
    strike: float,
    atm_strike: float,
) -> dict[str, Any]:
    bid = _num(_at(cols, "bid", i))
    ask = _num(_at(cols, "ask", i))
    mid = (bid + ask) / 2 if bid is not None and ask is not None else None
    return {
        "exp": exp,
        "dte": dte,
        "right": _at(cols, "right", i),
        "strike": strike,
        "atm": strike == atm_strike,
        "bid": bid,
        "ask": ask,
        "mid": mid,
        "iv": _num(_at(cols, "iv", i)),
        "delta": _num(_at(cols, "delta", i)),
        "gamma": _num(_at(cols, "gamma", i)),
        "theta": _num(_at(cols, "theta", i)),
        "vega": _num(_at(cols, "vega", i)),
        "oi": _int(_at(cols, "oi", i)),
        "volume": _int(_at(cols, "volume", i)),
    }


def _slice_rows(
    chain: dict[str, Any], session: str, spot: float, window: int, max_expiries: int
) -> list[dict[str, Any]]:
    """Nearest ``max_expiries`` expiries, +/- ``window`` ladder rungs around
    that expiry's ATM strike (nearest to spot), both rights, sorted by
    (exp, right, strike)."""
    cols = chain.get("columns") if isinstance(chain.get("columns"), dict) else {}
    exps = cols.get("exp") if isinstance(cols.get("exp"), list) else []
    strikes = cols.get("strike") if isinstance(cols.get("strike"), list) else []
    by_exp: dict[str, list[int]] = {}
    for i, e in enumerate(exps):
        by_exp.setdefault(e, []).append(i)
    rows: list[dict[str, Any]] = []
    for exp in sorted(by_exp)[:max_expiries]:
        idxs = by_exp[exp]
        ladder = sorted(
            {strikes[i] for i in idxs if isinstance(strikes[i], (int, float))}
        )
        if not ladder:
            continue
        atm_strike = min(ladder, key=lambda s: (abs(s - spot), s))
        lo = ladder.index(atm_strike) - window
        keep = set(ladder[max(0, lo) : ladder.index(atm_strike) + window + 1])
        try:
            dte = (date.fromisoformat(exp) - date.fromisoformat(session)).days
        except ValueError:
            continue
        for i in idxs:
            if strikes[i] in keep:
                rows.append(_row(cols, i, exp, dte, strikes[i], atm_strike))
    rows.sort(key=lambda r: (r["exp"], r["right"] or "", r["strike"] or 0.0))
    return rows


# ----------------------------------------------------------- iv30 history


def _iv30_history(store_root: Path, sym: str) -> dict[str, Any] | None:
    try:
        doc = json.loads((store_root / "iv-history" / "vwap_atm.json").read_text())
    except (OSError, ValueError):
        return None
    name = doc.get("names", {}).get(sym) if isinstance(doc, dict) else None
    sessions = name.get("sessions") if isinstance(name, dict) else None
    if not isinstance(sessions, dict):
        return None
    pts: list[tuple[int, float]] = []
    days: list[str] = []
    for day in sorted(sessions):
        raw = sessions[day].get("iv30") if isinstance(sessions[day], dict) else None
        val = _num(raw)
        if val is not None:
            pts.append((_ts_ms(day), val))
            days.append(day)
    if not pts:
        return None
    kept = decimate_pairs(pts, IV30_MAX_POINTS)
    y_lo, y_hi = level_extent(kept)
    return {
        "points": [[ts, v] for ts, v in kept],
        "y_lo": y_lo,
        "y_hi": y_hi,
        "n": len(pts),
        "first": days[0],
        "last": days[-1],
        "source": IV30_SOURCE,
    }


# ---------------------------------------------------------------- payload


def options_payload(
    store_root: Path, sym: str, window: int, max_expiries: int, now: datetime
) -> dict[str, Any]:
    """The /api/market/{sym}/options body: the recorded surface (cards +
    atm_term + slice), the iv30 history, and a parked null ``live`` slot."""
    w, cap = clamp_params(window, max_expiries)
    warnings: list[str] = []
    candidates = _find_features(store_root, sym)
    session: str | None = None
    name: dict[str, Any] | None = None
    chain: dict[str, Any] | None = None
    if candidates:
        session, name = candidates[0]
        for d, n in candidates:
            doc = _read_chain_quiet(store_root, sym, d)
            if doc is not None:
                if d != session:
                    warnings.append(
                        f"chain missing for the newest recorded sessions; "
                        f"serving {sym} from session {d}"
                    )
                session, name, chain = d, n, doc
                break
        else:
            warnings.append(
                f"no recorded chain for {sym} in the newest "
                f"{len(candidates)} feature sessions; cards only (slice null)"
            )
    available = session is not None
    recorded: dict[str, Any] | None = None
    if available and name is not None:
        spot = _num(name.get("spot"))
        if spot is None and chain is not None:
            quote = chain.get("header", {}).get("underlying_quote")
            spot = _num(quote.get("close")) if isinstance(quote, dict) else None
        slice_rows: list[dict[str, Any]] | None = None
        if chain is not None and spot is not None:
            slice_rows = _slice_rows(chain, session, spot, w, cap)
        elif chain is not None:
            warnings.append("no spot in features or chain header; slice null")
        recorded = {
            "session": session,
            "age_seconds": history_age_seconds(session, now),
            "spot": spot,
            "cards": {k: name.get(k) for k in CARD_KEYS},
            "atm_term": name.get("atm_term"),
            "slice": slice_rows,
        }
    return {
        "now": now.isoformat(),
        "symbol": sym,
        "recorded": recorded,
        "iv30_history": _iv30_history(store_root, sym),
        "live": None,  # Phase 4 (live envelope) fills this; key is stable now
        "available": available,
        "warnings": warnings,
    }
