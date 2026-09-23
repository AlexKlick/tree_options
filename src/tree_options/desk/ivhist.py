"""VWAP-derived 30-day ATM implied-vol history (plan D2; pre-registered in
``docs/desk/IVHIST-001.md``, sealed before any IV was solved).

Inputs, all read-only:

* the Polygon option daily bars in ``artifacts/massive-cache`` (files are
  hash-named; each body is identified by its own ``ticker`` field,
  decoded with ``massive_client.loads_exact`` and typed by
  ``massive_options.parse_daily_bars``). Roots with digits (adjusted
  deliverables) are excluded; duplicate bars that disagree are dropped;
* the underlying's ``adjusted=false`` daily bars from the same cache: spot
  is the session VWAP, the most synchronous match for an option VWAP (the
  research panel is split-ADJUSTED and never paired with option prices);
* DTB3 (``DESK_STORE/indices/DTB3.csv``, FRED layout) as the rate, else a
  declared constant with a warning; q = 0 (the ATM call/put average
  cancels the first-order dividend and early-exercise errors).

Per (name, session): monthly expiries 8..90 calendar days out; per expiry
the strikes with both a call and a put bar inside |ln(K/F)| <= 0.10, IV of
each side by ``massive_derived.implied_vol`` (the hash-pinned ``bs_price``),
the strike's IV the call/put mean, the ATM IV interpolated in ln(K/F) (one
side only within 0.03); 30 days by total-variance interpolation between the
bracketing expiries, else flat from the nearest expiry 15..60 days out
(``extrapolated``), else NOT_EVALUABLE.

:func:`evaluate` scores the history against the CBOE vol indices and
assigns the labels (``ok`` / ``low-fidelity`` / ``not-evaluable``) exactly
as the pre-registration fixes them.
"""

from __future__ import annotations

import bisect
import csv
import hashlib
import itertools
import math
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

from tree_options.data.massive_client import loads_exact
from tree_options.data.massive_derived import MassiveDerivationError, implied_vol
from tree_options.data.massive_options import MassiveDailyBar, MassiveSchemaError, parse_daily_bars
from tree_options.desk import stats
from tree_options.desk.har import ETF_NAMES
from tree_options.desk.sessions import Calendar, calendar_days_between, previous_session
from tree_options.synth_options.greeks import CallPut
from tree_options.time.expiries import is_friday

SCHEMA = "desk-ivhist/1"
VERDICT_SCHEMA = "desk-ivhist-verdict/1"
STUDY = "IVHIST-001"
TARGET_DAYS = 30
TAU_MIN, TAU_MAX = 8, 90
EXTRAP_MIN, EXTRAP_MAX = 15, 60
MONEYNESS_BAND = 0.10
ONE_SIDED_MAX = 0.03
DIVIDEND_YIELD = 0.0
DEFAULT_RATE = 0.04  # declared fallback when no DTB3 file exists (warned)
WINDOW = (date(2024, 8, 26), date(2026, 9, 3))

PAIRS: tuple[tuple[str, str], ...] = (
    ("VIX", "SPY"),
    ("VXN", "QQQ"),
    ("RVX", "IWM"),
    ("VXAPL", "AAPL"),
    ("VXAZN", "AMZN"),
    ("VXGOG", "GOOGL"),
    ("GVZ", "GLD"),
)
INDEX_NAMES: tuple[str, ...] = tuple(i for i, _ in PAIRS)
SINGLE_STOCK_INDICES: tuple[str, ...] = ("VXAPL", "VXAZN", "VXGOG")
MIN_PAIR_N = 250
MAX_ABS_BIAS = 2.0
MIN_CORR = 0.85

_OCC = re.compile(r"^O:([A-Z]+)(\d{6})([CP])(\d{8})$")
_ADJ_ROOT = re.compile(r"^O:[A-Z]+\d+\d{6}[CP]\d{8}$")
_HEAD_TICKER = re.compile(rb'^\{\s*"ticker"\s*:\s*"([^"]+)"')
_HEAD_RESULTS = re.compile(rb'^\{\s*"results"\s*:')


# ------------------------------------------------------------ contracts


def parse_option_ticker(ticker: str) -> tuple[str, date, str, float] | None:
    """(root, expiry, right, strike) of a standard OCC ticker, else None."""
    m = _OCC.match(ticker)
    if m is None:
        return None
    root, ymd, right, strike = m.groups()
    try:
        expiry = date(2000 + int(ymd[:2]), int(ymd[2:4]), int(ymd[4:]))
    except ValueError:
        return None
    return root, expiry, right, int(strike) / 1000


def third_friday(year: int, month: int) -> date:
    for day in range(15, 22):
        d = date(year, month, day)
        if is_friday(d):
            return d
    raise AssertionError("unreachable: every month has a Friday in 15..21")


def is_monthly_expiry_session(expiry: date, cal: Calendar) -> bool:
    """The third Friday, or the last session before it when that Friday is
    not a session (Good Friday moves the April monthly to Thursday)."""
    if not cal.is_session(expiry):
        return False
    tf = third_friday(expiry.year, expiry.month)
    if expiry == tf:
        return True
    if cal.is_session(tf):
        return False
    return previous_session(tf, cal) == expiry


def days_between(a: date, b: date) -> int:
    return round(calendar_days_between(a.isoformat(), b.isoformat()))


# ----------------------------------------------------------------- IV math


@dataclass(frozen=True)
class StrikeIV:
    strike: float
    iv: float  # (iv_call + iv_put) / 2
    iv_call: float
    iv_put: float


def solve_iv(
    premium: float, *, spot: float, strike: float, dte: int, right: str, rate: float, q: float
) -> float | None:
    try:
        return implied_vol(
            premium=premium,
            spot=spot,
            strike=strike,
            dte_calendar_days=dte,
            call_put=cast(CallPut, right),
            risk_free=rate,
            dividend_yield=q,
        )
    except MassiveDerivationError:
        return None


def solve_strike(
    call_px: float, put_px: float, *, spot: float, strike: float, dte: int, rate: float, q: float
) -> StrikeIV | None:
    ivc = solve_iv(call_px, spot=spot, strike=strike, dte=dte, right="C", rate=rate, q=q)
    ivp = solve_iv(put_px, spot=spot, strike=strike, dte=dte, right="P", rate=rate, q=q)
    if ivc is None or ivp is None:
        return None
    return StrikeIV(strike=strike, iv=(ivc + ivp) / 2.0, iv_call=ivc, iv_put=ivp)


def atm_iv(strikes: Sequence[StrikeIV], forward: float) -> tuple[float, str] | None:
    """ATM IV: linear in m = ln(K/F) between the nearest strikes on each side
    (``bracket``); one side only when the nearest |m| <= 0.03 (``one-sided``)."""
    if not strikes:
        return None
    ms = [(math.log(s.strike / forward), s) for s in strikes]
    below = [x for x in ms if x[0] <= 0.0]
    above = [x for x in ms if x[0] >= 0.0]
    if below and above:
        mb, sb = max(below, key=lambda x: x[0])
        ma, sa = min(above, key=lambda x: x[0])
        if ma == mb:
            return sb.iv, "bracket"
        return sb.iv + (sa.iv - sb.iv) * (0.0 - mb) / (ma - mb), "bracket"
    m, s = min(ms, key=lambda x: abs(x[0]))
    if abs(m) <= ONE_SIDED_MAX:
        return s.iv, "one-sided"
    return None


def total_variance_interp(points: Sequence[tuple[float, float]], target: float) -> float | None:
    """IV at ``target`` days from (days, iv) points by linear total variance
    between the bracketing points; None outside their range."""
    pts = sorted(points)
    below = [p for p in pts if p[0] <= target]
    above = [p for p in pts if p[0] >= target]
    if not below or not above:
        return None
    t1, iv1 = below[-1]
    t2, iv2 = above[0]
    if t1 == t2:
        return iv1
    w1, w2 = iv1 * iv1 * t1, iv2 * iv2 * t2
    w = w1 + (w2 - w1) * (target - t1) / (t2 - t1)
    return math.sqrt(w / target) if w > 0.0 else None


def constant_maturity_30(points: Sequence[tuple[int, float]]) -> tuple[float | None, str]:
    """(iv30, method) or (None, reason) under the IVHIST-001 rules."""
    pts = [(float(t), iv) for t, iv in points]
    iv = total_variance_interp(pts, float(TARGET_DAYS))
    if iv is not None:
        return iv, "interpolated"
    near = [p for p in pts if EXTRAP_MIN <= p[0] <= EXTRAP_MAX]
    if near:
        return min(near, key=lambda p: abs(p[0] - TARGET_DAYS))[1], "extrapolated"
    return None, "no expiry brackets 30d or lies in 15..60d"


# -------------------------------------------------------------- cache scan


@dataclass(frozen=True)
class OptionBar:
    expiry: date
    right: str
    strike: float
    vwap: float


@dataclass
class CacheScan:
    source: str
    options: dict[str, dict[date, list[OptionBar]]] = field(default_factory=dict)
    spot: dict[str, dict[date, float]] = field(default_factory=dict)
    spot_conflicts: dict[str, set[date]] = field(default_factory=dict)
    stats: Counter[str] = field(default_factory=Counter)


def _bar_key(bar: MassiveDailyBar) -> int:
    """A compact identity of the whole bar (the exact Decimals hash
    deterministically): duplicates that disagree in any field conflict."""
    return hash((bar.open, bar.high, bar.low, bar.close, bar.vwap, Decimal(bar.volume)))


# session -> (vwap, bar identity); the full bars are not retained (memory)
_Cell = dict[date, tuple[Decimal, int]]


def _merge(cell: _Cell, bad: set[date], bars: Iterable[MassiveDailyBar]) -> None:
    for bar in bars:
        prev = cell.get(bar.session)
        key = _bar_key(bar)
        if prev is None:
            cell[bar.session] = (bar.vwap, key)
        elif prev[1] != key:
            bad.add(bar.session)


def scan_cache(
    cache_dir: Path, names: Iterable[str], start: date, end: date, cal: Calendar
) -> CacheScan:
    """Option bars (monthly expiries of standard roots) and unadjusted spot
    bars of ``names`` for sessions in [start, end], merged across files."""
    wanted = set(names)
    scan = CacheScan(source=str(cache_dir))
    opt: dict[str, _Cell] = {}
    opt_bad: dict[str, set[date]] = {}
    spot: dict[str, _Cell] = {}
    spot_bad: dict[str, set[date]] = {}
    for path in sorted(cache_dir.glob("*.json")):
        if path.name.startswith("."):
            continue
        with open(path, "rb") as fh:
            head = fh.read(96)
        if _HEAD_RESULTS.match(head):
            scan.stats["master_bodies"] += 1
            continue
        m = _HEAD_TICKER.match(head)
        if m is None:
            scan.stats["other_bodies"] += 1
            continue
        ticker = m.group(1).decode("ascii", "replace")
        is_option = ticker.startswith("O:")
        if is_option:
            key = parse_option_ticker(ticker)
            if key is None:
                reason = "adjusted_root_skipped" if _ADJ_ROOT.match(ticker) else "unparsed_ticker"
                scan.stats[reason] += 1
                continue
            if key[0] not in wanted:
                continue
            if not is_monthly_expiry_session(key[1], cal):
                scan.stats["non_monthly_skipped"] += 1
                continue
        elif ticker not in wanted:
            continue
        try:
            body = loads_exact(path.read_bytes())
            if not isinstance(body, dict) or body.get("ticker") != ticker:
                raise MassiveSchemaError(f"{path.name}: ticker mismatch")
            bars = [
                b for b in parse_daily_bars(body, option_ticker=ticker) if start <= b.session <= end
            ]
        except (MassiveSchemaError, ValueError, UnicodeDecodeError):
            scan.stats["refused_bodies"] += 1
            continue
        if is_option and key is not None:
            # only the bars the method can read (8..90 calendar days out)
            bars = [b for b in bars if TAU_MIN <= days_between(b.session, key[1]) <= TAU_MAX]
        if is_option:
            scan.stats["option_bodies"] += 1
            _merge(opt.setdefault(ticker, {}), opt_bad.setdefault(ticker, set()), bars)
        elif body.get("adjusted") is False:
            scan.stats["spot_bodies"] += 1
            _merge(spot.setdefault(ticker, {}), spot_bad.setdefault(ticker, set()), bars)
        else:
            scan.stats["adjusted_spot_bodies_skipped"] += 1
    for ticker, cell in sorted(opt.items()):
        parsed = parse_option_ticker(ticker)
        assert parsed is not None
        root, expiry, right, strike = parsed
        for session, (vwap, _key) in sorted(cell.items()):
            if session in opt_bad[ticker]:
                scan.stats["conflicting_bars_dropped"] += 1
                continue
            if vwap <= 0:
                scan.stats["nonpositive_vwap_dropped"] += 1
                continue
            scan.options.setdefault(root, {}).setdefault(session, []).append(
                OptionBar(expiry=expiry, right=right, strike=strike, vwap=float(vwap))
            )
            scan.stats["option_bars"] += 1
    for ticker, cell in sorted(spot.items()):
        bad = spot_bad[ticker]
        scan.spot_conflicts[ticker] = set(bad)
        scan.spot[ticker] = {
            s: float(vwap) for s, (vwap, _key) in cell.items() if s not in bad and vwap > 0
        }
        scan.stats["spot_conflicts"] += len(bad)
    return scan


# ----------------------------------------------------------------- rates


@dataclass(frozen=True)
class RateSource:
    label: str
    obs: tuple[tuple[date, float], ...] = ()
    fallback: float | None = None

    @classmethod
    def constant(cls, rate: float) -> RateSource:
        return cls(label=f"declared-constant {rate}", fallback=rate)

    @classmethod
    def from_csv(cls, path: Path) -> RateSource:
        """FRED layout (``observation_date,DTB3`` or ``DATE,DTB3``; ``.``
        or empty = missing), percent -> decimal."""
        raw = path.read_bytes()
        rows = list(csv.reader(raw.decode("utf-8").splitlines()))
        obs = []
        for row in rows[1:]:
            if len(row) < 2:
                continue
            try:
                obs.append((date.fromisoformat(row[0].strip()), float(row[1]) / 100.0))
            except ValueError:
                continue
        sha = hashlib.sha256(raw).hexdigest()
        return cls(label=f"DTB3 {path.name} sha256 {sha}", obs=tuple(sorted(obs)))

    def rate_on(self, d: date) -> float | None:
        """The latest observation dated on or before ``d`` (point in time)."""
        i = bisect.bisect_right(self.obs, (d, math.inf)) - 1
        if i >= 0:
            return self.obs[i][1]
        return self.fallback


def rate_source_for_store(store: Path) -> tuple[RateSource, str | None]:
    path = store / "indices" / "DTB3.csv"
    if path.exists():
        return RateSource.from_csv(path), None
    return (
        RateSource.constant(DEFAULT_RATE),
        f"no {path}: using the declared constant rate {DEFAULT_RATE}",
    )


# --------------------------------------------------------------- history


def _nev(reason: str) -> dict[str, Any]:
    return {"status": "NOT_EVALUABLE", "reason": reason}


def iv30_record(
    bars: Sequence[OptionBar], *, spot: float, rate: float, session: date
) -> dict[str, Any]:
    by_exp: dict[date, dict[float, dict[str, float]]] = {}
    for b in bars:
        by_exp.setdefault(b.expiry, {}).setdefault(b.strike, {})[b.right] = b.vwap
    points: list[tuple[int, float]] = []
    used: list[list[Any]] = []
    for expiry in sorted(by_exp):
        tau = days_between(session, expiry)
        if not TAU_MIN <= tau <= TAU_MAX:
            continue
        fwd = spot * math.exp((rate - DIVIDEND_YIELD) * tau / 365.0)
        solved = []
        for strike, sides in sorted(by_exp[expiry].items()):
            if "C" not in sides or "P" not in sides:
                continue
            if abs(math.log(strike / fwd)) > MONEYNESS_BAND:
                continue
            s = solve_strike(
                sides["C"],
                sides["P"],
                spot=spot,
                strike=strike,
                dte=tau,
                rate=rate,
                q=DIVIDEND_YIELD,
            )
            if s is not None:
                solved.append(s)
        atm = atm_iv(solved, fwd)
        if atm is None:
            continue
        points.append((tau, atm[0]))
        used.append([expiry.isoformat(), tau, atm[0], len(solved), atm[1]])
    iv, how = constant_maturity_30(points)
    if iv is None:
        return _nev(how)
    return {"iv30": iv, "method": how, "spot": spot, "rate": rate, "expiries": used}


def build_history(
    scan: CacheScan, names: Sequence[str], sessions: Sequence[date], rates: RateSource
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name in names:
        opts = scan.options.get(name, {})
        if not opts:
            out[name] = {"sessions": {}, "note": "no option bars on disk"}
            continue
        spots = scan.spot.get(name, {})
        conflicts = scan.spot_conflicts.get(name, set())
        recs: dict[str, Any] = {}
        for d in sessions:
            bars = opts.get(d)
            if not bars:
                recs[d.isoformat()] = _nev("no option bars")
            elif d in conflicts:
                recs[d.isoformat()] = _nev("conflicting unadjusted spot bars")
            elif d not in spots:
                recs[d.isoformat()] = _nev("no unadjusted spot bar")
            else:
                rate = rates.rate_on(d)
                if rate is None:
                    recs[d.isoformat()] = _nev("no rate observation")
                    continue
                recs[d.isoformat()] = iv30_record(bars, spot=spots[d], rate=rate, session=d)
        out[name] = {"sessions": recs}
    return {
        "schema": SCHEMA,
        "study": STUDY,
        "method": {
            "target_days": TARGET_DAYS,
            "tau_bounds": [TAU_MIN, TAU_MAX],
            "extrapolation_bounds": [EXTRAP_MIN, EXTRAP_MAX],
            "moneyness_band": MONEYNESS_BAND,
            "one_sided_max": ONE_SIDED_MAX,
            "expiries": "monthly (third Friday or the session before a holiday Friday)",
            "price": "Polygon daily VWAP per contract; spot = underlying adjusted=false VWAP",
            "iv": "massive_derived.implied_vol on the hash-pinned bs_price; call/put mean",
        },
        "assumptions": {"dividend_yield": DIVIDEND_YIELD, "rate_source": rates.label},
        "window": [sessions[0].isoformat(), sessions[-1].isoformat()] if sessions else [],
        "scan": {"source": scan.source, "stats": dict(sorted(scan.stats.items()))},
        "names": out,
    }


def history_series(history: Mapping[str, Any], name: str) -> dict[date, tuple[float, str]]:
    """{session: (iv30, method)} of the evaluable sessions of ``name``."""
    doc = history.get("names", {}).get(name) or {}
    out: dict[date, tuple[float, str]] = {}
    for d, rec in (doc.get("sessions") or {}).items():
        if isinstance(rec, dict) and isinstance(rec.get("iv30"), (int, float)):
            out[date.fromisoformat(d)] = (float(rec["iv30"]), str(rec.get("method", "")))
    return out


# ------------------------------------------------------------- benchmark


def read_index_csv(path: Path) -> dict[date, float]:
    """CBOE daily history: ``DATE,OPEN,HIGH,LOW,CLOSE`` (CLOSE used) or
    ``DATE,<NAME>`` (GVZ); dates MM/DD/YYYY."""
    rows = list(csv.reader(path.read_text().splitlines()))
    if not rows:
        return {}
    header = [h.strip().upper() for h in rows[0]]
    col = header.index("CLOSE") if "CLOSE" in header else len(header) - 1
    out: dict[date, float] = {}
    for row in rows[1:]:
        if len(row) <= col:
            continue
        try:
            d = datetime.strptime(row[0].strip(), "%m/%d/%Y").date()
            out[d] = float(row[col])
        except ValueError:
            continue
    return out


def _metrics(ours: Sequence[float], index: Sequence[float]) -> dict[str, Any]:
    diffs = [a - b for a, b in zip(ours, index, strict=True)]
    out: dict[str, Any] = {"n": len(diffs)}
    if len(diffs) >= 2:
        out["median_bias"] = stats.median(diffs)
        out["mean_bias"] = sum(diffs) / len(diffs)
        out["median_abs_diff"] = stats.median([abs(x) for x in diffs])
        out["corr"] = stats.pearson(ours, index)
    return out


def evaluate(
    history: Mapping[str, Any], indices: Mapping[str, Mapping[date, float]]
) -> dict[str, Any]:
    """Per-pair metrics and verdicts, per-name labels and the overall
    verdict, exactly under the IVHIST-001 rules."""
    pairs: dict[str, Any] = {}
    for index, name in PAIRS:
        series = history_series(history, name)
        idx = indices.get(index) or {}
        common = sorted(d for d in series if d in idx)
        rec: dict[str, Any] = {"name": name}
        if not series:
            rec.update(status="NOT_EVALUABLE", reason=f"no IV history for {name}", n=0)
            pairs[index] = rec
            continue
        if not idx:
            rec.update(status="NOT_EVALUABLE", reason=f"no {index} history", n=0)
            pairs[index] = rec
            continue
        ours = [100.0 * series[d][0] for d in common]
        ref = [idx[d] for d in common]
        rec.update(_metrics(ours, ref))
        interp = [d for d in common if series[d][1] == "interpolated"]
        rec["interpolated_only"] = _metrics(
            [100.0 * series[d][0] for d in interp], [idx[d] for d in interp]
        )
        rec["extrapolated_share"] = (len(common) - len(interp)) / len(common) if common else None
        if len(common) >= 3:
            d_ours = [b - a for a, b in itertools.pairwise(ours)]
            d_ref = [b - a for a, b in itertools.pairwise(ref)]
            rec["corr_changes"] = stats.pearson(d_ours, d_ref)
        if len(common) < MIN_PAIR_N:
            rec.update(status="NOT_EVALUABLE", reason=f"n={len(common)} < {MIN_PAIR_N}")
        else:
            corr = rec.get("corr")
            ok = corr is not None and abs(rec["median_bias"]) <= MAX_ABS_BIAS and corr >= MIN_CORR
            rec["status"] = "PASS" if ok else "FAIL"
        pairs[index] = rec
    single_pass = all(pairs[i]["status"] == "PASS" for i in SINGLE_STOCK_INDICES)
    bench = {name: index for index, name in PAIRS}
    labels: dict[str, str] = {}
    for name in sorted(history.get("names", {})):
        if not history_series(history, name):
            labels[name] = "not-evaluable"
        elif name in bench:
            labels[name] = "ok" if pairs[bench[name]]["status"] == "PASS" else "low-fidelity"
        elif name in ETF_NAMES:
            labels[name] = "low-fidelity"  # the index-ETF pairs never promote others
        else:
            labels[name] = "ok" if single_pass else "low-fidelity"
    core = [pairs[i]["status"] for i, _ in PAIRS if i != "GVZ"]
    n_pass = sum(1 for s in core if s == "PASS")
    overall = "PASS" if n_pass == len(core) else "FAIL" if n_pass == 0 else "PARTIAL"
    return {
        "schema": VERDICT_SCHEMA,
        "study": STUDY,
        "bar": {"min_n": MIN_PAIR_N, "max_abs_median_bias": MAX_ABS_BIAS, "min_corr": MIN_CORR},
        "pairs": pairs,
        "single_stock_generalization": single_pass,
        "labels": labels,
        "overall": overall,
    }
