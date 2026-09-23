"""Retrospective valuation scenario for a put-debit spread (M4).

"How would this kind of trade have panned out?" The forward path of a
candidate found today is unknown, so the scenario replays its SHAPE over
the daily-close history instead: for every session in the bar window, an
analog spread is opened with strikes matched to the candidate's moneyness
(strike / spot) and held for the candidate's calendar DTE, valued at
intrinsic when its analog expiry is reached. The rolling set of completed
analogs yields an outcome distribution; the most recent one is charted.

HONESTY CONTRACT (Codex-arch #9) - every artifact carries the label
"valuation scenario · approximate · simulated" and these disclosures:
- Black-Scholes European pricing with ONE flat implied vol (no skew, no
  term structure, American early exercise ignored).
- The vol is calibrated to TODAY's quoted debit for this exact spread (or
  CBOE iv30 when calibration fails) - look-ahead: history is priced with
  a vol observed now.
- Entry price is the MODEL debit at the analog entry close; there were no
  historical option quotes. The pessimistic variant adds today's observed
  half-spread (debit_ask - debit_mid) to every entry.
- No exits are modeled (no touch / take-profit / time stop): buy-and-hold
  to analog expiry.
- Analog windows overlap, so outcomes are NOT independent samples.
Scenario results are never counted in real win rates or totals.

Date handling: bars carry epoch-ms session stamps; all spacing here is
integer millisecond arithmetic on instants (the naive-date ban holds).
FLOAT CONVENTION: this lane speaks floats; nothing here writes book files.
"""

from __future__ import annotations

import json
import os
import statistics
from datetime import datetime
from pathlib import Path
from typing import Any

from tree_options.synth_options.greeks import bs_price
from tree_options.trex.series import decimate_pairs, y_extent

LABEL = "valuation scenario · approximate · simulated"
DAY_MS = 86_400_000
RISK_FREE = 0.04
DIVIDEND_YIELD = 0.0
IV_BAND = 0.20  # sensitivity band: iv x (1 - band), iv x (1 + band)
MULTIPLIER = 100
MAX_ARTIFACTS = 30
MIN_ANALOGS = 5


def spread_value(
    spot: float, short: float, long_: float, dte_days: int, iv: float
) -> float:
    """Model value of a put debit spread (long the higher strike). At or
    past expiry the value is intrinsic - never the pricer's half-day
    floor, which would leak time value into an expired position."""
    if dte_days <= 0:
        return min(max(long_ - spot, 0.0), long_ - short)
    args = {
        "spot": spot,
        "dte_calendar_days": dte_days,
        "iv": iv,
        "risk_free": RISK_FREE,
        "dividend_yield": DIVIDEND_YIELD,
        "call_put": "P",
    }
    return bs_price(strike=long_, **args) - bs_price(strike=short, **args)  # type: ignore[arg-type]


def calibrate_iv(
    spot: float, short: float, long_: float, dte_days: int, target: float
) -> float | None:
    """Flat vol that reprices the spread to ``target`` (bisection).

    Returns None when the target is not bracketed on [1%, 300%] (the
    spread's value is not monotone in vol for every geometry) or the
    residual stays wide - the caller then falls back to iv30.
    """
    if target <= 0 or dte_days <= 0:
        return None
    lo, hi = 0.01, 3.0
    f_lo = spread_value(spot, short, long_, dte_days, lo) - target
    f_hi = spread_value(spot, short, long_, dte_days, hi) - target
    if f_lo * f_hi > 0:
        return None
    for _ in range(80):
        mid = (lo + hi) / 2
        f_mid = spread_value(spot, short, long_, dte_days, mid) - target
        if abs(f_mid) < 1e-6:
            return mid
        if f_lo * f_mid <= 0:
            hi = mid
        else:
            lo, f_lo = mid, f_mid
    mid = (lo + hi) / 2
    residual = abs(spread_value(spot, short, long_, dte_days, mid) - target)
    return mid if residual < 1e-3 else None


def _analog(
    bars: list[tuple[int, float]],
    start: int,
    k_short: float,
    k_long: float,
    dte_days: int,
    iv: float,
) -> tuple[float, list[tuple[int, float]]] | None:
    """One analog opened at bars[start]: (entry model debit, P&L path per
    session). None when the analog's expiry lies beyond the bar window."""
    t0, s0 = bars[start]
    expiry_ms = t0 + dte_days * DAY_MS
    if bars[-1][0] < expiry_ms:
        return None  # incomplete window: never extrapolate
    short = k_short * s0
    long_ = k_long * s0
    debit = spread_value(s0, short, long_, dte_days, iv)
    path: list[tuple[int, float]] = []
    for t, spot in bars[start:]:
        if t > expiry_ms:
            break
        remaining = round((expiry_ms - t) / DAY_MS)
        value = spread_value(spot, short, long_, remaining, iv)
        path.append((t, (value - debit) * MULTIPLIER))
    # the last observed session at/before expiry settles at intrinsic
    t_last, spot_last = next(
        (t, s) for t, s in reversed(bars[start:]) if t <= expiry_ms
    )
    final = (spread_value(spot_last, short, long_, 0, iv) - debit) * MULTIPLIER
    path[-1] = (t_last, final)
    return debit, path


def _summary(finals: list[float]) -> dict[str, Any]:
    ordered = sorted(finals)
    wins = sum(1 for p in finals if p > 0)
    return {
        "count": len(finals),
        "wins": wins,
        "win_rate": wins / len(finals),
        "mean_pnl": statistics.fmean(finals),
        "median_pnl": statistics.median(finals),
        "p10_pnl": ordered[max(0, int(0.1 * (len(ordered) - 1)))],
        "worst_pnl": ordered[0],
        "best_pnl": ordered[-1],
    }


def valuation_scenario(
    *,
    short: float,
    long_: float,
    dte_days: int,
    spot_now: float,
    debit_mid: float,
    debit_ask: float | None,
    bars: list[tuple[int, float]],
    iv30: float | None,
) -> dict[str, Any]:
    """Pure core: outcome distribution + recent-analog curve.

    ``bars`` are (epoch_ms, close) ascending; ``iv30`` in CBOE percent
    units (17.5 == 17.5%). Returns {"error": ...} on unusable inputs.
    """
    if long_ <= short or spot_now <= 0 or dte_days <= 0:
        return {"error": "bad structure (need long > short, spot > 0, dte > 0)"}
    bars = sorted((int(t), float(c)) for t, c in bars if c and c > 0)
    iv = calibrate_iv(spot_now, short, long_, dte_days, debit_mid)
    iv_source = "calibrated to today's quoted debit_mid (look-ahead)"
    if iv is None:
        if not iv30 or iv30 <= 0:
            return {"error": "no usable vol (calibration failed, iv30 absent)"}
        iv = iv30 / 100.0
        iv_source = "cboe iv30 (look-ahead; calibration to the quoted debit failed)"
    k_short, k_long = short / spot_now, long_ / spot_now
    half_spread = max(0.0, (debit_ask or debit_mid) - debit_mid)

    finals: list[float] = []
    pessimistic: list[float] = []
    band: dict[str, list[float]] = {"lo": [], "hi": []}
    recent: tuple[int, float, list[tuple[int, float]]] | None = None
    for i in range(len(bars)):
        run = _analog(bars, i, k_short, k_long, dte_days, iv)
        if run is None:
            break  # every later start is incomplete too
        debit, path = run
        final = path[-1][1]
        finals.append(final)
        pessimistic.append(final - half_spread * MULTIPLIER)
        for side, mult in (("lo", 1 - IV_BAND), ("hi", 1 + IV_BAND)):
            alt = _analog(bars, i, k_short, k_long, dte_days, iv * mult)
            if alt is not None:
                band[side].append(alt[1][-1][1])
        recent = (i, debit, path)
    if len(finals) < MIN_ANALOGS or recent is None:
        return {
            "error": f"only {len(finals)} complete analog windows "
            f"(need {MIN_ANALOGS}; dte {dte_days} vs {len(bars)} sessions of bars)"
        }

    start, recent_debit, recent_path = recent
    pts = decimate_pairs(recent_path, 400)
    y_lo, y_hi = y_extent(pts)
    return {
        "error": None,
        "iv": iv,
        "iv_source": iv_source,
        "analogs": _summary(finals),
        "pessimistic": _summary(pessimistic) if half_spread > 0 else None,
        "iv_band_mean_pnl": {
            "lo": statistics.fmean(band["lo"]) if band["lo"] else None,
            "hi": statistics.fmean(band["hi"]) if band["hi"] else None,
        },
        "recent": {
            "entry_ms": bars[start][0],
            "entry_spot": bars[start][1],
            "entry_debit": recent_debit,
            "final_pnl": recent_path[-1][1],
            "series": {
                "points": [[t, v] for t, v in pts],
                "y_lo": y_lo,
                "y_hi": y_hi,
                "last": {
                    "ts_ms": pts[-1][0],
                    "value": pts[-1][1],
                    "pos": pts[-1][1] >= 0,
                },
            },
        },
        "sessions": len(bars),
    }


def assumptions(iv: float | None, iv_source: str | None) -> dict[str, Any]:
    return {
        "model": "black-scholes-european, one flat vol (no skew/term structure)",
        "american_early_exercise": "ignored",
        "exits_modeled": "none (buy-and-hold to analog expiry)",
        "entry_price": "model debit at the analog entry close (no historical quotes)",
        "strikes": "moneyness-matched to each analog's entry spot",
        "iv_source": iv_source,
        "iv": iv,
        "iv_band": IV_BAND,
        "risk_free": RISK_FREE,
        "dividend_yield": DIVIDEND_YIELD,
        "bars_source": "polygon daily closes (delayed)",
        "samples": "overlapping rolling windows - not independent",
    }


# -- artifact store --------------------------------------------------------


def artifact_name(key: str) -> str:
    safe = "".join(c if c.isalnum() or c in ".-_" else "_" for c in key)
    return f"{safe}.json"


def write_artifact(state_dir: Path, key: str, doc: dict[str, Any]) -> Path:
    out_dir = state_dir / "backtests"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / artifact_name(key)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(doc, indent=1) + "\n")
    os.replace(tmp, path)
    stale = sorted(out_dir.glob("*.json"), key=lambda p: p.stat().st_mtime)
    for old in stale[:-MAX_ARTIFACTS]:
        old.unlink(missing_ok=True)
    return path


def read_artifact(state_dir: Path, key: str) -> dict[str, Any] | None:
    path = state_dir / "backtests" / artifact_name(key)
    if not path.exists():
        return None
    try:
        doc = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    # sanitized filenames can collide in principle; the stored key decides
    return doc if isinstance(doc, dict) and doc.get("key") == key else None


def find_structure(state_dir: Path, key: str) -> dict[str, Any] | None:
    """Resolve a shadow key against the runner's OWN artifacts (latest
    scan candidates + rejects, then the shadow book) - request payloads
    never supply prices."""
    from tree_options.trex.discovery.shadow import _norm_expiry, shadow_key

    latest = state_dir / "latest.json"
    if latest.exists():
        try:
            payload = json.loads(latest.read_text()).get("payload", {})
        except (OSError, json.JSONDecodeError):
            payload = {}
        for row in [*payload.get("candidates", []), *payload.get("rejected", [])]:
            try:
                row_key = shadow_key(
                    str(row["underlying"]),
                    _norm_expiry(row["expiry"]),
                    float(row["short_strike"]),
                    float(row["long_strike"]),
                )
            except (KeyError, TypeError, ValueError):
                continue
            if row_key == key and row.get("debit_mid") is not None:
                return {
                    "underlying": str(row["underlying"]),
                    "expiry": _norm_expiry(row["expiry"]),
                    "short": float(row["short_strike"]),
                    "long": float(row["long_strike"]),
                    "dte": int(row["dte"]),
                    "debit_mid": float(row["debit_mid"]),
                    "debit_ask": (
                        float(row["debit_ask"]) if row.get("debit_ask") is not None else None
                    ),
                    "found_in": "latest scan",
                }
    book = state_dir / "shadow_book.json"
    if book.exists():
        try:
            positions = json.loads(book.read_text()).get("positions", [])
        except (OSError, json.JSONDecodeError):
            positions = []
        for p in positions:
            if p.get("key") == key:
                return {
                    "underlying": str(p["underlying"]),
                    "expiry": str(p["expiry"]),
                    "short": float(p["short_strike"]),
                    "long": float(p["long_strike"]),
                    "dte": None,  # resolved against now by the caller
                    "debit_mid": float(p["debit_paid"]),
                    "debit_ask": None,
                    "found_in": "shadow book",
                }
    return None


def materialize(
    state_dir: Path,
    key: str,
    now: datetime,
    structure: dict[str, Any],
    spot_now: float,
    bars: list[tuple[int, float]],
    iv30: float | None,
) -> dict[str, Any]:
    """Run the scenario and persist the labeled artifact (errors too, so
    the UI shows WHY a scenario is unavailable instead of spinning)."""
    result = valuation_scenario(
        short=structure["short"],
        long_=structure["long"],
        dte_days=int(structure["dte"]),
        spot_now=spot_now,
        debit_mid=structure["debit_mid"],
        debit_ask=structure.get("debit_ask"),
        bars=bars,
        iv30=iv30,
    )
    doc = {
        "key": key,
        "generated_at": now.isoformat(),
        "label": LABEL,
        "structure": {**structure, "spot_now": spot_now},
        "assumptions": assumptions(result.get("iv"), result.get("iv_source")),
        **result,
    }
    write_artifact(state_dir, key, doc)
    return doc
