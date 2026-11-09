"""AI-BEAR-001 scorer: the three sleeves over 1997-2026, against their baselines.

Pre-registered in ``docs/desk/AI-BEAR-001.md``, parameters sealed in
``data/desk/aibear/AI-BEAR-001.params.json``. ``score`` refuses an unsealed
parameter file unless the caller passes ``require_sealed=False``, which is
for tests only.

Per instrument, the daily walk is:

1. regime states (``regime_engine``) from the instrument's closes and VIX;
2. ATM IV for each day: VXN, or before VXN starts, VIX x k_vxn (k_vxn is the
   median VXN/VIX over the first 252 shared sessions), times atm_factor
   (VXN is a variance-swap index, above ATM IV), times the instrument's
   60-session realized vol over the NDX's, clipped to [1, 3] and raised to
   relative_vol_exponent. Both constants were calibrated on 28 recorded
   chain sessions; the log sha256 is in the parameters;
3. each sleeve's trades: signal at the close of t, entry at t+1, an exit
   decided at the close of d and executed at d+1. One open position per
   sleeve and instrument. A position still open at the end is closed at the
   last close (``open_at_end``).

The unit is R, a trade's net P&L over its entry debit (one unit risked per
trade). Statistics are clustered by the calendar month of entry: R is summed
by month, and months are bootstrapped and sign-flipped
(``longrun.bootstrap_ci`` / ``longrun.sign_flip_p``).

Baselines:

* **random null:** per instrument, the same number of trades at random,
  non-overlapping signal days, with the same structure and exits.
  p_null = (1 + #{null total >= observed}) / (1 + seeds), Holm-adjusted
  across the three sleeves.
* **always-on ladder (core only):** a new core spread every 21 sessions
  whenever flat, with no regime stop. This is the unconditional short the
  research ledger already found DEAD.
"""

from __future__ import annotations

import bisect
import hashlib
import json
import math
import statistics
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import numpy as np

from tree_options.desk.aibear.history import read_history
from tree_options.desk.aibear.params import (
    NOT_EVALUABLE,
    file_sha256,
    load_params,
    params_path,
    regime_params,
)
from tree_options.desk.aibear.pricing import PricingParams, SpreadQuote, quote_spread
from tree_options.desk.aibear.regime_engine import VixSeries, regime_series
from tree_options.desk.aibear.rules import (
    SleeveRule,
    bull_runs,
    entry_signal,
    exit_reason,
    sleeve_rules,
    stop_level,
)
from tree_options.desk.indices import read_store
from tree_options.desk.longrun import bootstrap_ci, holm, sign_flip_p
from tree_options.desk.paths import store_root

Point = tuple[date, float]
INDEX_NAMES = ("NDX", "SOX")
BEAR_ERAS = ("dotcom", "gfc", "bear-2022")


# ------------------------------------------------------------------ inputs


def _store_points(path: Path) -> list[Point]:
    out: list[Point] = []
    for d, _o, _h, _l, c in read_store(path):
        if c.strip():
            out.append((date.fromisoformat(d), float(c)))
    return out


@dataclass(frozen=True)
class Inputs:
    series: dict[str, list[Point]]  # instrument -> closes
    vix: list[Point]
    vxn: list[Point]
    dtb3: list[Point]
    sha256: dict[str, str]


def load_inputs(names: Iterable[str], root: Path | None = None) -> Inputs:
    base = root or store_root()
    shas: dict[str, str] = {}
    series: dict[str, list[Point]] = {}
    for n in sorted(set(names) | {"NDX"}):
        series[n] = read_history(n, base)
        shas[f"history/{n}.csv"] = file_sha256(base / "history" / f"{n}.csv")
    vxn = read_history("VXN", base)
    shas["history/VXN.csv"] = file_sha256(base / "history" / "VXN.csv")
    vix = _store_points(base / "indices" / "VIX.csv")
    shas["indices/VIX.csv"] = file_sha256(base / "indices" / "VIX.csv")
    dtb3 = _store_points(base / "indices" / "DTB3.csv")
    shas["indices/DTB3.csv"] = file_sha256(base / "indices" / "DTB3.csv")
    return Inputs(series=series, vix=vix, vxn=vxn, dtb3=dtb3, sha256=shas)


class _AsOf:
    """Last value on or before a date, within a staleness bound."""

    def __init__(self, points: Sequence[Point], staleness_days: int) -> None:
        self.dates = [d for d, _ in points]
        self.values = [v for _, v in points]
        self.staleness = staleness_days

    def get(self, d: date) -> float | None:
        j = bisect.bisect_right(self.dates, d) - 1
        if j < 0 or (d - self.dates[j]).days > self.staleness:
            return None
        return self.values[j]


def k_vxn(vxn: Sequence[Point], vix: Sequence[Point], sessions: int = 252) -> float:
    """Median VXN/VIX over the first ``sessions`` dates both series carry."""
    vix_by = dict(vix)
    ratios = [v / vix_by[d] for d, v in vxn if d in vix_by and vix_by[d] > 0][:sessions]
    if len(ratios) < sessions:
        raise ValueError(f"only {len(ratios)} shared VXN/VIX sessions")
    return float(statistics.median(ratios))


def _rv(closes: Sequence[float], i: int, window: int) -> float | None:
    if i < window:
        return None
    rets = [math.log(closes[k] / closes[k - 1]) for k in range(i - window + 1, i + 1)]
    return statistics.pstdev(rets) * math.sqrt(252.0)


@dataclass
class Instrument:
    name: str
    cost_class: str
    dates: list[date]
    closes: list[float]
    states: list[str]
    bull_run: list[int]
    iv: list[float | None]
    rate: list[float]


def build_instrument(name: str, inp: Inputs, doc: dict[str, Any], *, end: date) -> Instrument:
    rp = regime_params(doc)
    pts = [(d, c) for d, c in inp.series[name] if d <= end]
    dates = [d for d, _ in pts]
    closes = [c for _, c in pts]
    vix = VixSeries(inp.vix)
    states = regime_series(dates, closes, vix, rp)
    stale = rp.vix_max_staleness_days
    vix_at, vxn_at = _AsOf(inp.vix, stale), _AsOf(inp.vxn, stale)
    rate_at = _AsOf(inp.dtb3, 10)
    k = k_vxn(inp.vxn, inp.vix)
    vxn_start = inp.vxn[0][0]
    window = int(doc["pricing"]["relative_vol_window"])
    lo, hi = (float(x) for x in doc["pricing"]["relative_vol_clip"])
    atm_factor = float(doc["pricing"]["atm_factor"])
    gamma = float(doc["pricing"]["relative_vol_exponent"])
    ndx = inp.series["NDX"]
    ndx_dates = [d for d, _ in ndx]
    ndx_closes = [c for _, c in ndx]
    ndx_rv_cache: dict[int, float | None] = {}

    def ndx_rv(d: date) -> float | None:
        j = bisect.bisect_right(ndx_dates, d) - 1
        if j < 0 or (d - ndx_dates[j]).days > stale:
            return None
        if j not in ndx_rv_cache:
            ndx_rv_cache[j] = _rv(ndx_closes, j, window)
        return ndx_rv_cache[j]

    iv: list[float | None] = []
    rate: list[float] = []
    last_rate = 0.0
    for i, d in enumerate(dates):
        if d < vxn_start:
            v = vix_at.get(d)
            base = None if v is None else v * k
        else:
            base = vxn_at.get(d)
        mult: float | None = atm_factor
        if name != "NDX":
            own, ref = _rv(closes, i, window), ndx_rv(d)
            mult = (
                None
                if own is None or not ref
                else atm_factor * min(hi, max(lo, own / ref)) ** gamma
            )
        iv.append(None if base is None or mult is None else base / 100.0 * mult)
        r = rate_at.get(d)
        last_rate = last_rate if r is None else r / 100.0
        rate.append(last_rate)
    return Instrument(
        name=name,
        cost_class=name if name in INDEX_NAMES else "single",
        dates=dates,
        closes=closes,
        states=states,
        bull_run=bull_runs(states),
        iv=iv,
        rate=rate,
    )


# ------------------------------------------------------------------ trades


@dataclass(frozen=True)
class Trade:
    sleeve: str
    instrument: str
    signal_date: str
    entry_date: str
    exit_date: str
    exit_reason: str
    entry_state: str
    debit: float
    proceeds: float
    pnl: float
    r: float
    signal_index: int = field(repr=False)
    exit_index: int = field(repr=False)

    def public(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("signal_index")
        d.pop("exit_index")
        for k in ("debit", "proceeds", "pnl", "r"):
            d[k] = round(d[k], 6)
        return d


class Simulator:
    """Trades on one instrument for one sleeve, memoized by signal index."""

    def __init__(self, inst: Instrument, rule: SleeveRule, pp: PricingParams) -> None:
        self.inst, self.rule, self.pp = inst, rule, pp
        self._memo: dict[tuple[int, bool], Trade | None] = {}

    def _quote(
        self, j: int, s0: float, k_long: float, k_short: float, expiry: date, iv: float
    ) -> SpreadQuote:
        inst = self.inst
        return quote_spread(
            spot=inst.closes[j] * 100.0 / s0,
            k_long=k_long,
            k_short=k_short,
            dte=max((expiry - inst.dates[j]).days, 0),
            atm_iv=iv,
            rate=inst.rate[j],
            right=self.rule.right,
            cost_class=inst.cost_class,
            p=self.pp,
        )

    def _iv_or(self, j: int, fallback: float) -> float:
        v = self.inst.iv[j]
        return fallback if v is None else v

    def trade(self, i: int, *, use_regime_stop: bool = True) -> Trade | None:
        key = (i, use_regime_stop)
        if key not in self._memo:
            self._memo[key] = self._trade(i, use_regime_stop)
        return self._memo[key]

    def _trade(self, i: int, use_regime_stop: bool) -> Trade | None:
        inst, rule = self.inst, self.rule
        e = i + 1
        n = len(inst.dates)
        if e >= n or inst.iv[e] is None:
            return None
        s0 = inst.closes[e]
        k_long, k_short = 100.0 * rule.long_moneyness, 100.0 * rule.short_moneyness
        width = abs(k_long - k_short)
        expiry = inst.dates[e] + timedelta(days=rule.dte)
        iv = self._iv_or(e, 0.0)
        debit = self._quote(e, s0, k_long, k_short, expiry, iv).open_debit
        if not debit > 0.0:
            return None
        stop_low = stop_level(rule, inst.states, inst.closes, i)
        reason, decided = None, n - 1
        for j in range(e, n):
            iv = self._iv_or(j, iv)  # a day without an IV keeps the last one
            q = self._quote(j, s0, k_long, k_short, expiry, iv)
            reason = exit_reason(
                rule,
                states=inst.states,
                closes=inst.closes,
                j=j,
                bull_run=inst.bull_run[j],
                held_sessions=j - e,
                dte_left=(expiry - inst.dates[j]).days,
                value_mid=q.mid,
                width=width,
                stop_low=stop_low,
                use_regime_stop=use_regime_stop,
            )
            if reason is not None:
                decided = j
                break
        if reason is not None and decided + 1 < n:
            x = decided + 1  # decided at the close of d, executed at d + 1
        else:  # no exit decided, or decided on the last close: mark at the end
            x, reason = n - 1, "open_at_end"
        proceeds = self._quote(x, s0, k_long, k_short, expiry, self._iv_or(x, iv)).close_proceeds
        pnl = proceeds - debit
        return Trade(
            sleeve=rule.name,
            instrument=inst.name,
            signal_date=inst.dates[i].isoformat(),
            entry_date=inst.dates[e].isoformat(),
            exit_date=inst.dates[x].isoformat(),
            exit_reason=reason,
            entry_state=inst.states[i],
            debit=debit,
            proceeds=proceeds,
            pnl=pnl,
            r=pnl / debit,
            signal_index=i,
            exit_index=x,
        )


def _first_index(inst: Instrument, start: date) -> int:
    return bisect.bisect_left(inst.dates, start)


def run_strategy(sim: Simulator, start: date) -> list[Trade]:
    inst, rule = sim.inst, sim.rule
    out: list[Trade] = []
    i = _first_index(inst, start)
    n = len(inst.dates)
    while i < n - 1:
        if entry_signal(rule, inst.states, inst.closes, i):
            t = sim.trade(i)
            if t is not None:
                out.append(t)
                nxt = t.exit_index
                if t.exit_reason == "regime_stop":
                    nxt += rule.cooldown_after_regime_stop
                i = max(nxt, i + 1)
                continue
        i += 1
    return out


def run_ladder(sim: Simulator, start: date, every: int) -> list[Trade]:
    inst = sim.inst
    out: list[Trade] = []
    i = _first_index(inst, start)
    n = len(inst.dates)
    while i < n - 1:
        if inst.states[i] != NOT_EVALUABLE:
            t = sim.trade(i, use_regime_stop=False)
            if t is not None:
                out.append(t)
                i = max(t.exit_index, i + every)
                continue
        i += 1
    return out


def null_total(sim: Simulator, start: date, n_trades: int, seed: int) -> tuple[float, int]:
    """Total R of ``n_trades`` random, non-overlapping trades (and how many fit)."""
    inst = sim.inst
    lo = _first_index(inst, start)
    eligible = [
        i
        for i in range(lo, len(inst.dates) - 1)
        if inst.states[i] != NOT_EVALUABLE and inst.iv[i + 1] is not None
    ]
    if n_trades == 0 or not eligible:
        return 0.0, 0
    rng = np.random.default_rng(seed)
    starts: list[int] = []
    ends: list[int] = []
    total = 0.0
    for i in rng.permutation(np.asarray(eligible, dtype=np.int64)):
        t = sim.trade(int(i))
        if t is None:
            continue
        a, b = int(i), t.exit_index
        k = bisect.bisect_left(starts, a)
        if k > 0 and ends[k - 1] >= a:
            continue
        if k < len(starts) and starts[k] <= b:
            continue
        starts.insert(k, a)
        ends.insert(k, b)
        total += t.r
        if len(starts) == n_trades:
            break
    return total, len(starts)


# ------------------------------------------------------------------ stats


def era_of(day: str, eras: Sequence[Mapping[str, str]]) -> tuple[str, str]:
    for e in eras:
        if e["start"] <= day <= e["end"]:
            return e["name"], e["kind"]
    return "unassigned", "none"


def monthly_sums(trades: Sequence[Trade]) -> list[float]:
    by: dict[str, float] = defaultdict(float)
    for t in trades:
        by[t.entry_date[:7]] += t.r
    return [by[k] for k in sorted(by)]


def summarize(trades: Sequence[Trade], *, draws: int, seed: int) -> dict[str, Any]:
    months = monthly_sums(trades)
    rs = [t.r for t in trades]
    lo, hi = bootstrap_ci(months, draws=draws, seed=seed) if months else (0.0, 0.0)
    return {
        "trades": len(trades),
        "months": len(months),
        "total_r": round(sum(rs), 4),
        "mean_r": round(sum(rs) / len(rs), 4) if rs else 0.0,
        "hit_rate": round(sum(1 for r in rs if r > 0) / len(rs), 4) if rs else 0.0,
        "ci95_total_r": [lo, hi],
        "p_sign_flip": round(sign_flip_p(months, draws=draws, seed=seed + 1), 4) if months else 1.0,
    }


def by_era(trades: Sequence[Trade], eras: Sequence[Mapping[str, str]]) -> dict[str, Any]:
    out: dict[str, dict[str, Any]] = {}
    for t in trades:
        name, kind = era_of(t.entry_date, eras)
        row = out.setdefault(name, {"kind": kind, "trades": 0, "total_r": 0.0, "wins": 0})
        row["trades"] += 1
        row["total_r"] += t.r
        row["wins"] += t.r > 0
    for row in out.values():
        row["total_r"] = round(row["total_r"], 4)
        row["hit_rate"] = round(row.pop("wins") / row["trades"], 4)
    return dict(sorted(out.items()))


def by_instrument(trades: Sequence[Trade]) -> dict[str, Any]:
    out: dict[str, dict[str, Any]] = {}
    for t in trades:
        row = out.setdefault(t.instrument, {"trades": 0, "total_r": 0.0})
        row["trades"] += 1
        row["total_r"] += t.r
    return {
        k: {"trades": v["trades"], "total_r": round(v["total_r"], 4)}
        for k, v in sorted(out.items())
    }


def bull_years(eras: Sequence[Mapping[str, str]]) -> float:
    return sum(
        (date.fromisoformat(e["end"]) - date.fromisoformat(e["start"])).days / 365.25
        for e in eras
        if e["kind"] == "bull"
    )


def bull_total(trades: Sequence[Trade], eras: Sequence[Mapping[str, str]]) -> float:
    return sum(t.r for t in trades if era_of(t.entry_date, eras)[1] == "bull")


def verdict(
    name: str, row: Mapping[str, Any], p_holm: float, alpha: float
) -> tuple[int, dict[str, bool], str]:
    """The sealed pass bar: (bear eras positive, checks, verdict).

    P1 full-window total R > 0 with the month-clustered CI lower bound > 0;
    P2 Holm-adjusted random-null p < alpha; P3 positive in >= 2 of the 3
    bear eras; P4 (core only) loses at most half the always-on ladder's
    R per bull-era year. PASS = all; INSURANCE = all but P1; else FAIL."""
    s = row["summary"]
    bear_pos = sum(1 for e in BEAR_ERAS if row["eras"].get(e, {}).get("total_r", 0.0) > 0)
    checks = {
        "P1": s["total_r"] > 0 and s["ci95_total_r"][0] > 0,
        "P2": p_holm < alpha,
        "P3": bear_pos >= 2,
    }
    if name == "core":
        core_b, lad_b = row["bull_r_per_year"], row["ladder_bull_r_per_year"]
        checks["P4"] = core_b >= 0 or (lad_b < 0 and core_b >= 0.5 * lad_b)
    rest = [v for k, v in checks.items() if k != "P1"]
    label = "PASS" if all(checks.values()) else "INSURANCE" if all(rest) else "FAIL"
    return bear_pos, checks, label


# ------------------------------------------------------------------ score


def score(
    *,
    root: Path | None = None,
    params_file: Path | None = None,
    require_sealed: bool = True,
    null_seeds: int | None = None,
) -> tuple[dict[str, Any], list[Trade]]:
    """The AI-BEAR-001 result document and every scored strategy trade."""
    pfile = params_file or params_path()
    doc = load_params(pfile, require_sealed=require_sealed)
    rules = sleeve_rules(doc)
    stats_cfg = doc["statistics"]
    draws, seed = int(stats_cfg["bootstrap_draws"]), int(stats_cfg["seed"])
    alpha = float(stats_cfg["alpha"])
    seeds = int(doc["baselines"]["random_null_seeds"]) if null_seeds is None else null_seeds
    every = int(doc["baselines"]["ladder_every_sessions"])
    eras = doc["eras"]
    start = date.fromisoformat(doc["scoring_start"])
    end = date.fromisoformat(doc["scoring_end"])
    names = sorted({n for r in rules.values() for n in r.instruments})
    inp = load_inputs(names, root)
    insts = {n: build_instrument(n, inp, doc, end=end) for n in names}
    pp = PricingParams.from_doc(doc)
    pp_flat = PricingParams.from_doc(doc, flat_skew=True)

    all_trades: list[Trade] = []
    sleeves: dict[str, Any] = {}
    p_null: dict[str, float] = {}
    for name, rule in rules.items():
        sims = {n: Simulator(insts[n], rule, pp) for n in rule.instruments}
        trades = [t for n in rule.instruments for t in run_strategy(sims[n], start)]
        all_trades.extend(trades)
        observed = sum(t.r for t in trades)
        counts: defaultdict[str, int] = defaultdict(int)
        for t in trades:
            counts[t.instrument] += 1
        null_totals = []
        shortfall = 0
        for s in range(seeds):
            tot = 0.0
            for k, n in enumerate(rule.instruments):
                got, fit = null_total(sims[n], start, counts[n], seed + 1000 * s + k)
                tot += got
                shortfall += counts[n] - fit
            null_totals.append(tot)
        exceed = sum(1 for x in null_totals if x >= observed - 1e-9)
        p_null[name] = (1 + exceed) / (1 + seeds) if seeds else 1.0
        flat = [
            t
            for n in rule.instruments
            for t in run_strategy(Simulator(insts[n], rule, pp_flat), start)
        ]
        row: dict[str, Any] = {
            "summary": summarize(trades, draws=draws, seed=seed),
            "eras": by_era(trades, eras),
            "instruments": by_instrument(trades),
            "exit_reasons": dict(sorted(_count(t.exit_reason for t in trades).items())),
            "null": {
                "seeds": seeds,
                "mean_total_r": round(float(np.mean(null_totals)), 4) if null_totals else 0.0,
                "p95_total_r": round(float(np.percentile(null_totals, 95)), 4)
                if null_totals
                else 0.0,
                "p_null": round(p_null[name], 4),
                "trades_that_did_not_fit": shortfall,
            },
            "sensitivity_flat_skew_total_r": round(sum(t.r for t in flat), 4),
        }
        if name == "core":
            ladder = [t for n in rule.instruments for t in run_ladder(sims[n], start, every)]
            row["ladder"] = {
                "summary": summarize(ladder, draws=draws, seed=seed),
                "eras": by_era(ladder, eras),
            }
            yrs = bull_years(eras)
            row["bull_r_per_year"] = round(bull_total(trades, eras) / yrs, 4)
            row["ladder_bull_r_per_year"] = round(bull_total(ladder, eras) / yrs, 4)
        sleeves[name] = row

    adjusted = holm(p_null)
    for name, row in sleeves.items():
        row["null"]["p_null_holm"] = round(adjusted[name], 4)
        row["bear_eras_positive"], row["checks"], row["verdict"] = verdict(
            name, row, adjusted[name], alpha
        )

    result = {
        "schema": "aibear-result/1",
        "id": doc["id"],
        "params_sha256": file_sha256(pfile),
        "inputs_sha256": dict(sorted(inp.sha256.items())),
        "k_vxn": round(k_vxn(inp.vxn, inp.vix), 6),
        "scoring_window": [doc["scoring_start"], doc["scoring_end"]],
        "instruments": {
            n: {
                "first": i.dates[0].isoformat(),
                "last": i.dates[-1].isoformat(),
                "sessions": len(i.dates),
            }
            for n, i in sorted(insts.items())
        },
        "sleeves": sleeves,
    }
    return result, all_trades


def _count(xs: Iterable[str]) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for x in xs:
        out[x] += 1
    return dict(out)


def write_outputs(out_dir: Path, result: dict[str, Any], trades: Sequence[Trade]) -> dict[str, str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    rj = (json.dumps(result, indent=2, sort_keys=True) + "\n").encode()
    tj = "".join(json.dumps(t.public(), sort_keys=True) + "\n" for t in trades).encode()
    (out_dir / "results.json").write_bytes(rj)
    (out_dir / "trades.jsonl").write_bytes(tj)
    return {
        "results.json": hashlib.sha256(rj).hexdigest(),
        "trades.jsonl": hashlib.sha256(tj).hexdigest(),
    }
