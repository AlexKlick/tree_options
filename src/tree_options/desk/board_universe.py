"""Board universe v3: out-of-the-money short strikes for the desk boards.

The 20260927-v1 bundle is 72 contracts picked once (the 2026-05-08
master) as the three strikes nearest spot: a $5 grid at the money. Under
the $300 per-trade loss cap a 5-wide credit must collect >= $2.00, so its
short leg sits at or in the money; the classic option-selling
(variance-risk-premium) trade -- a 15-30 delta short, a credit of about
15-35% of the width -- cannot appear on a v1 board.

This module selects the extra contracts and assembles a NEW bundle
vintage. The old vintage and its behaviour are untouched: a bundle
without ``candidate_pairing`` keeps iag's adjacent-strike pairing.

Selection rule ``otm-delta-wings/1`` (lookahead-free by construction):

* reference snapshots: contract masters as of m_1 < m_2 < ... (the desk
  long-dated capture, about every four weeks). m_k SERVES the window
  sessions in (m_k, m_k+1] (the last one up to the window end). A
  contract m_k selects is LISTED from the first session m_k serves, and
  the bundle drops every bar before that session, so no consumer (board,
  parity spot, outcome) can see a contract before its reference snapshot
  could have named it.
* expiries: the standard monthly (the third Friday, or the prior session
  when that Friday is a holiday -- 2026-06-18) listed in m_k with
  7 <= DTE <= 60 (iag's board range) on at least one served session.
* short strikes: per target delta, the listed strike nearest
  K = S * exp(sigma^2 T / 2 - z sigma sqrt(T)) for puts and
  K = S * exp(sigma^2 T / 2 + z sigma sqrt(T)) for calls, z = N^-1(1 - delta),
  S = the spot proxy close on m_k, sigma = the CBOE 30-day implied-vol
  index close on m_k (VIX for SPY, VXN for QQQ, RVX for IWM), T = the
  median served DTE / 365, r = q = 0. A delta PROXY (index vol, no skew),
  not a model price. Ties go to the further-OTM strike.
* wings: for each short strike K the listed strikes K - w (puts) or
  K + w (calls) for each width w, only when listed at exactly that width.

The bundle's ``candidate_pairing`` makes iag pair every two strikes of a
chain whose width is in the listed widths (instead of adjacent strikes);
its ``iv_context`` carries the implied-vol index closes the boards expose
as of the PRIOR session. Evidence, not authority: no broker, no network
(the capture script owns the wire).
"""

from __future__ import annotations

import calendar as _calendar
import hashlib
import json
import statistics
from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from statistics import NormalDist
from typing import Any

from tree_options.desk import intraday_action_graph as iag

SELECTION_SCHEMA = "desk-board-universe-selection/1"
RULE = "otm-delta-wings/1"
BUNDLE_SCHEMA = iag.BARS_V2  # carries candidate rules: pre-v3 readers refuse it
IV_INDEX = {"SPY": "VIX", "QQQ": "VXN", "IWM": "RVX"}
#: the 20260929-otm vintage: one 0.20-delta proxy short (about 0.17-0.25 true
#: delta once skew is counted) and 1/2/5-wide wings, sized to a ~1 h free-tier
#: fetch; (0.30, 0.15) doubles the wire (docs: results.json of the lane)
DEFAULT_DELTAS = (Decimal("0.20"),)
DEFAULT_WIDTHS = (Decimal(1), Decimal(2), Decimal(5))
DTE_MIN, DTE_MAX = 7, 60  # iag._candidates' board range (calendar days)
_YEAR = Decimal(365)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ------------------------------------------------------------------ calendar


def third_friday(year: int, month: int) -> date:
    weeks = _calendar.monthcalendar(year, month)
    fridays = [week[_calendar.FRIDAY] for week in weeks if week[_calendar.FRIDAY]]
    return date(year, month, fridays[2])


def monthly_expiry(year: int, month: int, sessions: Iterable[date]) -> date:
    """The standard monthly expiry: the third Friday, or the last session
    before it when that Friday is not a session (a holiday)."""
    friday = third_friday(year, month)
    ordered = sorted(sessions)
    if friday in set(ordered):
        return friday
    before = [day for day in ordered if day < friday]
    if not before:
        raise ValueError(f"no session before {friday}")
    return before[-1]


def served_sessions(
    references: Iterable[date], sessions: Iterable[date], window_end: date
) -> dict[date, list[date]]:
    """Reference date -> the window sessions it serves: (m_k, m_k+1], the
    last reference up to ``window_end``. A reference serving no session is
    absent."""
    refs = sorted(set(references))
    days = sorted(set(sessions))
    served: dict[date, list[date]] = {}
    for position, ref in enumerate(refs):
        until = refs[position + 1] if position + 1 < len(refs) else window_end
        chosen = [day for day in days if ref < day <= min(until, window_end)]
        if chosen:
            served[ref] = chosen
    return served


# ------------------------------------------------------------------ strikes


def short_strike_target(
    spot: Decimal, sigma: Decimal, years: Decimal, delta: Decimal, right: str
) -> Decimal:
    """The strike whose Black-Scholes delta (r = q = 0) is ``delta`` in
    magnitude: the put below spot, the call above it."""
    if right not in ("C", "P") or not (0 < delta < Decimal("0.5")):
        raise ValueError("right must be C or P and 0 < delta < 0.5")
    if spot <= 0 or sigma <= 0 or years <= 0:
        raise ValueError("spot, sigma and years must be positive")
    z = Decimal(repr(NormalDist().inv_cdf(float(1 - delta))))
    root = sigma * years.sqrt()
    drift = sigma * sigma * years / 2
    exponent = drift - z * root if right == "P" else drift + z * root
    return spot * exponent.exp()


def snap(strikes: Iterable[Decimal], target: Decimal, right: str) -> Decimal:
    """The listed strike nearest ``target``; a tie goes further OTM."""
    listed = sorted(set(strikes))
    if not listed:
        raise ValueError("no listed strikes")
    if right == "P":
        return min(listed, key=lambda k: (abs(k - target), k))
    return min(listed, key=lambda k: (abs(k - target), -k))


def wings(
    listed: Iterable[Decimal], short: Decimal, right: str, widths: Iterable[Decimal]
) -> list[Decimal]:
    """The long legs K -/+ w listed at exactly width w (further OTM)."""
    available = set(listed)
    sign = -1 if right == "P" else 1
    return sorted({short + sign * w for w in widths if short + sign * w in available})


def candidate_months(first: date, last: date) -> list[tuple[int, int]]:
    """(year, month) from ``first``'s month through three months after
    ``last``'s: every month whose monthly expiry can be 7..60 DTE on a
    session in [first, last]."""
    start = first.year * 12 + first.month - 1
    stop = last.year * 12 + last.month - 1 + 3
    return [(index // 12, index % 12 + 1) for index in range(start, stop + 1)]


def median_dte(expiry: date, served: Iterable[date]) -> int | None:
    """The median DTE over the served sessions that put ``expiry`` on a
    board (DTE_MIN..DTE_MAX), or None when none do."""
    dtes = [(expiry - day).days for day in served]
    usable = sorted(d for d in dtes if DTE_MIN <= d <= DTE_MAX)
    return None if not usable else int(statistics.median_low(usable))


def master_chains(
    master: Mapping[str, Any], underlying: str
) -> dict[tuple[date, str], dict[Decimal, str]]:
    """(expiry, right) -> {strike: ticker} of one contract master; only
    standard 100-share OCC tickers of ``underlying`` (adjusted and
    non-standard deliverables never parse)."""
    if master.get("underlying_ticker") != underlying:
        raise ValueError(f"master identity mismatch: {master.get('underlying_ticker')}")
    chains: dict[tuple[date, str], dict[Decimal, str]] = {}
    for page in master.get("pages", []):
        for row in page.get("results", []):
            try:
                contract = iag.parse_contract(str(row.get("ticker", "")))
            except ValueError:
                continue
            if contract.underlying != underlying or row.get("shares_per_contract", 100) != 100:
                continue
            chains.setdefault((contract.expiry, contract.right), {})[contract.strike] = (
                contract.ticker
            )
    return chains


def select_reference(
    master: Mapping[str, Any],
    underlying: str,
    spot: Decimal,
    sigma: Decimal,
    served: list[date],
    sessions: Iterable[date],
    deltas: Iterable[Decimal] = DEFAULT_DELTAS,
    widths: Iterable[Decimal] = DEFAULT_WIDTHS,
) -> list[dict[str, Any]]:
    """Every contract one reference snapshot selects for one underlying:
    [{ticker, expiry, right, strike, role, delta_target, target_strike,
    dte_ref}] (a contract picked twice keeps its first role)."""
    chains = master_chains(master, underlying)
    all_sessions = sorted(set(sessions))
    widths = tuple(widths)
    picked: dict[str, dict[str, Any]] = {}
    for year, month in candidate_months(served[0], served[-1]):
        try:
            expiry = monthly_expiry(year, month, all_sessions)
        except ValueError:
            continue
        dte = median_dte(expiry, served)
        if dte is None:
            continue
        years = Decimal(dte) / _YEAR
        for right in ("P", "C"):
            chain = chains.get((expiry, right))
            if not chain:
                continue
            for delta in deltas:
                target = short_strike_target(spot, sigma, years, delta, right)
                short = snap(chain, target, right)
                entries = [(short, "short")] + [
                    (k, "wing") for k in wings(chain, short, right, widths)
                ]
                for strike, role in entries:
                    ticker = chain[strike]
                    picked.setdefault(
                        ticker,
                        {
                            "ticker": ticker,
                            "expiry": expiry.isoformat(),
                            "right": right,
                            "strike": str(strike),
                            "role": role,
                            "delta_target": str(delta),
                            "target_strike": str(target.quantize(Decimal("0.01"))),
                            "dte_ref": dte,
                        },
                    )
    return [picked[ticker] for ticker in sorted(picked)]


# ------------------------------------------------------------------ iv context


def iv_closes(rows: Iterable[tuple[str, ...]], start: date, end: date) -> dict[str, str]:
    """{ISO date: close} of a stored index history (``indices`` rows:
    date, open, high, low, close) between start and end inclusive."""
    return {row[0]: row[4] for row in rows if row[4] and start <= date.fromisoformat(row[0]) <= end}


def iv_prev_close(closes: Mapping[date, Decimal], day: date) -> Decimal | None:
    """The index close of the latest date strictly before ``day`` (as of
    the session's open: today's close is not yet known)."""
    prior = [d for d in closes if d < day]
    return closes[max(prior)] if prior else None


# ------------------------------------------------------------------ bundle


def _session_start_ms(day: date) -> int:
    """Midnight ET of ``day`` in epoch milliseconds (bars before it are
    from earlier sessions)."""
    start = datetime(day.year, day.month, day.day, tzinfo=iag.ET).astimezone(UTC)
    return int(start.timestamp()) * 1000


def verify_body(ticker: str, body: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The minute bars of one aggregates response, refused when it is not
    a complete, positive-volume, ticker-matched minute series."""
    if body.get("status") not in ("OK", "DELAYED") or body.get("ticker") != ticker:
        raise ValueError(f"unusable minute response for {ticker}")
    bars = list(body.get("results") or [])
    if (
        body.get("next_url")
        or len(bars) >= 50000
        or body.get("resultsCount", len(bars)) != len(bars)
    ):
        raise ValueError(f"truncated minute response for {ticker}")
    if any(not isinstance(bar.get("t"), int) or bar.get("v", 0) <= 0 for bar in bars):
        raise ValueError(f"invalid bars for {ticker}")
    return bars


def assemble_bundle(
    selection: Mapping[str, Any],
    series: Mapping[str, list[dict[str, Any]]],
    iv_context: Mapping[str, Any] | None,
    *,
    selection_sha256: str,
    captured_at: str,
    wire_requests: int,
    sources: Mapping[str, str],
) -> dict[str, Any]:
    """The new vintage: each selected contract's bars from its listing
    session on (earlier bars dropped; later bars kept for marks), its
    listing interval, the pairing rule and the iv context. ``series`` maps
    ticker -> verified bars; a selected ticker missing from it is
    recorded, never invented."""
    if selection.get("schema") != SELECTION_SCHEMA:
        raise ValueError("board-universe selection schema required")
    contracts: dict[str, Any] = {}
    listing: dict[str, dict[str, str | None]] = {}
    missing: list[str] = []
    empty: list[str] = []
    dropped_bars = 0
    for ticker, spec in sorted(selection["contracts"].items()):
        iag.parse_contract(ticker)
        if ticker not in series:
            missing.append(ticker)
            continue
        cutoff = _session_start_ms(date.fromisoformat(spec["listed_from"]))
        bars = [bar for bar in series[ticker] if bar["t"] >= cutoff]
        dropped_bars += len(series[ticker]) - len(bars)
        if not bars:
            empty.append(ticker)
            continue
        contracts[ticker] = {"ticker": ticker, "timespan": "minute", "results": bars}
        listing[ticker] = {"from": spec["listed_from"], "until": spec.get("listed_until")}
    widths = selection["rule"]["widths"]
    document: dict[str, Any] = {
        "schema": BUNDLE_SCHEMA,
        "vintage": selection["vintage"],
        "start": selection["window_start"],
        "end": selection["window_end"],
        "selection_sha256": selection_sha256,
        "selection_rule": selection["rule"],
    }
    if widths != "adjacent":  # absent key = iag's v1 adjacent-strike pairing
        document["candidate_pairing"] = {"rule": "widths", "widths": [str(w) for w in widths]}
    return {
        **document,
        "iv_context": iv_context,
        "listing": listing,
        "captured_at": captured_at,
        "requested": len(selection["contracts"]),
        "found": len(contracts),
        "missing_tickers": missing,
        "empty_after_listing": empty,
        "bars_dropped_before_listing": dropped_bars,
        "wire_requests": wire_requests,
        "series_sources": dict(sorted(sources.items())),
        "contracts": contracts,
        "execution_authorized": False,
    }


def canonical_sha256(document: Any) -> str:
    return sha256_bytes(json.dumps(document, sort_keys=True, default=str).encode())
