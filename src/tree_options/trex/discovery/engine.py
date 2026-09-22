"""Discovery scan engine (pure): delayed put chain -> ranked candidates.

Debit vocabulary (D1): buy long_strike, sell short_strike, pay debit.
The protocol filter's tri-state discipline is reused — every rule on
every candidate resolves PASS / FAIL / NOT_EVALUABLE / NOT_APPLICABLE,
missing inputs never become numbers, and degradation is recorded, never
silent (probe evidence 2026-09-22: delayed chains flow, greeks do not,
so `auto` degrades to premium-floor targeting and the delta rule reads
NOT_APPLICABLE).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from tree_options.candidates.filters import (
    NOT_APPLICABLE,
    NOT_EVALUABLE,
    PASS,
    RuleResult,
)
from tree_options.trex.discovery.config import ScanConfig

DELAYED_NOTE = "delayed data (reqMarketDataType=3); quote age carried per row"
NO_GREEKS_DETAIL = "no greeks on delayed paper (probe 2026-09-22: tick 83 absent)"


@dataclass(frozen=True)
class ChainRow:
    """One observed put row for an expiry."""

    strike: float
    bid: float | None
    ask: float | None
    delta: float | None  # negative for puts
    ts: datetime  # quote observation time
    source: str = "delayed"


@dataclass(frozen=True)
class SpotContext:
    symbol: str
    spot: float | None
    source: str  # "cboe_eod" | "none"


@dataclass(frozen=True)
class ScanInput:
    underlying: str
    expiry: str  # yyyymmdd
    dte: int
    rows: list[ChainRow]


@dataclass
class Candidate:
    underlying: str
    expiry: str
    dte: int
    short_strike: float
    long_strike: float
    width: float
    target_mode_used: str
    debit_mid: float | None = None
    debit_bid: float | None = None
    debit_ask: float | None = None
    short_mid: float | None = None
    long_mid: float | None = None
    short_delta: float | None = None
    long_delta: float | None = None
    short_spread_frac: float | None = None
    long_spread_frac: float | None = None
    yield_ratio: float | None = None
    max_profit: float | None = None
    max_loss: float | None = None
    rank: int = 0
    accepted: bool = False
    rules: list[RuleResult] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)


@dataclass
class ScanResult:
    underlying: str
    expiry: str
    dte: int
    effective_target_mode: str
    candidates: list[Candidate] = field(default_factory=list)
    rejected: list[Candidate] = field(default_factory=list)
    rows_quoted: int = 0
    rows_unquoted: int = 0
    greeks_available: bool = False
    notes: list[str] = field(default_factory=list)


def row_mid(row: ChainRow) -> float | None:
    if row.bid is None or row.ask is None or row.bid <= 0 or row.ask < row.bid:
        return None
    return (row.bid + row.ask) / 2


def _leg_frac(row: ChainRow) -> float | None:
    if row.bid is None or row.ask is None or row.bid <= 0:
        return None
    mid = (row.bid + row.ask) / 2
    return (row.ask - row.bid) / mid if mid > 0 else None


def _effective_mode(cfg: ScanConfig, greeks: bool, spot: SpotContext | None) -> str:
    """The auto ladder (D-degradation): delta -> otm -> premium. Explicit
    modes pass through — the caller refuses them honestly when their
    input is missing."""
    if cfg.target_mode != "auto":
        return cfg.target_mode
    if greeks:
        return "delta"
    if spot is not None and spot.spot is not None:
        return "otm"
    return "premium"


def _target_short(
    rows: list[ChainRow], mode: str, cfg: ScanConfig, spot: SpotContext | None
) -> float | None:
    """The short strike the target mode aims at (None = unfiltered)."""
    if mode == "delta":
        best: float | None = None
        best_gap = 1.0
        for r in rows:
            if r.delta is None:
                continue
            gap = abs(abs(r.delta) - cfg.target_delta)
            if gap < best_gap:
                best, best_gap = r.strike, gap
        return best
    if mode == "otm" and spot is not None and spot.spot is not None:
        target = spot.spot * (1.0 - cfg.target_otm_frac)
        return min((r.strike for r in rows), key=lambda s: abs(s - target))
    return None  # premium: rank every valid pair


def _pairs(rows: list[ChainRow], width: float) -> list[tuple[ChainRow, ChainRow]]:
    by_strike = {r.strike: r for r in rows}
    out = []
    for long_row in rows:
        short = by_strike.get(round(long_row.strike - width, 6))
        if short is not None:
            out.append((long_row, short))
    return out


def scan(
    inp: ScanInput,
    cfg: ScanConfig,
    spot: SpotContext | None,
    now: datetime,
) -> ScanResult:
    result = ScanResult(
        underlying=inp.underlying,
        expiry=inp.expiry,
        dte=inp.dte,
        effective_target_mode="?",
        rows_quoted=sum(1 for r in inp.rows if row_mid(r) is not None),
        rows_unquoted=sum(1 for r in inp.rows if row_mid(r) is None),
        greeks_available=any(r.delta is not None for r in inp.rows),
        notes=[DELAYED_NOTE],
    )
    mode = _effective_mode(cfg, result.greeks_available, spot)
    result.effective_target_mode = mode
    if cfg.target_mode == "auto" and mode != "delta":
        result.notes.append(f"auto degraded to {mode}: no greeks observed")
    if mode == "delta" and not result.greeks_available:
        result.notes.append("explicit target_mode=delta refused: no greeks observed")
    target_short = _target_short(inp.rows, mode, cfg, spot)

    for width in cfg.widths:
        for long_row, short_row in _pairs(inp.rows, width):
            if target_short is not None and short_row.strike != target_short:
                continue  # targeted mode: only the aimed-at short strike
            c = Candidate(
                underlying=inp.underlying,
                expiry=inp.expiry,
                dte=inp.dte,
                short_strike=short_row.strike,
                long_strike=long_row.strike,
                width=width,
                target_mode_used=mode,
                short_delta=short_row.delta,
                long_delta=long_row.delta,
                short_spread_frac=_leg_frac(short_row),
                long_spread_frac=_leg_frac(long_row),
            )
            _evaluate(c, inp, cfg, mode, spot, result.greeks_available)
            if c.accepted:
                result.candidates.append(c)
            else:
                result.rejected.append(c)

    result.candidates.sort(key=lambda c: (-(c.yield_ratio or 0.0), c.short_strike))
    for i, c in enumerate(result.candidates, start=1):
        c.rank = i
    overflow = cfg.max_candidates_per_underlying
    result.rejected.extend(result.candidates[overflow:])
    result.candidates = result.candidates[:overflow]
    return result


def _evaluate(
    c: Candidate,
    inp: ScanInput,
    cfg: ScanConfig,
    mode: str,
    spot: SpotContext | None,
    greeks_available: bool,
) -> None:
    rules = c.rules
    reasons = c.reasons
    ok = True

    # dte window
    if cfg.dte_min <= c.dte <= cfg.dte_max:
        rules.append(RuleResult("dte", PASS, f"dte {c.dte}"))
    else:
        rules.append(RuleResult("dte", NOT_EVALUABLE, f"dte {c.dte} outside window"))
        reasons.append(f"dte {c.dte} outside {cfg.dte_min}-{cfg.dte_max}")
        ok = False

    # target selection discipline (targeted modes are filtered upstream;
    # an explicit delta/otm mode missing its input is refused, never silent)
    if mode == "delta" and not greeks_available:
        rules.append(RuleResult("delta", NOT_EVALUABLE, "target_mode=delta requires greeks; none observed"))
        reasons.append("delta targeting unavailable: no greeks")
        ok = False
    elif mode == "otm" and (spot is None or spot.spot is None):
        rules.append(RuleResult("otm", NOT_EVALUABLE, "target_mode=otm requires a spot context"))
        reasons.append("otm targeting unavailable: no spot")
        ok = False
    elif mode in ("delta", "otm"):
        _target_distance_rule(c, mode, cfg, spot, rules)

    # delta-band quality rule whenever greeks exist at all (probe 2026-09-22:
    # delayed paper has none -> NOT_APPLICABLE, disclosed per candidate)
    if "delta" not in {r.rule for r in rules}:
        if c.short_delta is not None:
            band_lo, band_hi = cfg.delta_band
            ad = abs(c.short_delta)
            if band_lo <= ad <= band_hi:
                rules.append(RuleResult("delta", PASS, f"|delta| {ad:.2f} in band"))
            else:
                rules.append(RuleResult("delta", "FAIL", f"|delta| {ad:.2f} outside band"))
                reasons.append(f"|delta| {ad:.2f} outside band {band_lo}-{band_hi}")
                ok = False
        elif greeks_available:
            rules.append(RuleResult("delta", NOT_EVALUABLE, "short delta unobserved"))
        else:
            rules.append(RuleResult("delta", NOT_APPLICABLE, NO_GREEKS_DETAIL))

    # quotes: both legs priced, finite, ordered, bid > 0
    long_mid = short_mid = None
    # re-derive rows from strikes (evaluate receives the scan input rows)
    by_strike = {r.strike: r for r in inp.rows}
    long_row = by_strike.get(c.long_strike)
    short_row = by_strike.get(c.short_strike)
    if long_row is None or short_row is None:
        rules.append(RuleResult("quotes", NOT_EVALUABLE, "row missing"))
        reasons.append("chain row missing")
        ok = False
    else:
        long_mid = row_mid(long_row)
        short_mid = row_mid(short_row)
        if long_mid is None or short_mid is None:
            reasons.append("no market on " + ("long" if long_mid is None else "") +
                           ("short" if short_mid is None else "") + " leg")
            rules.append(RuleResult("quotes", NOT_EVALUABLE, "no market (missing/zero-bid/crossed leg)"))
            ok = False
        else:
            rules.append(RuleResult("quotes", PASS, "both legs quoted"))
            c.long_mid = long_mid
            c.short_mid = short_mid
            # combo pricing from leg NBBOs (matches trex ibkr.py convention)
            c.debit_bid = (long_row.bid or 0.0) - (short_row.ask or 0.0)
            c.debit_ask = (long_row.ask or 0.0) - (short_row.bid or 0.0)
            c.debit_mid = long_mid - short_mid
            c.max_profit = (c.width - c.debit_mid) * 100
            c.max_loss = -c.debit_mid * 100  # negative: money paid at risk
            c.yield_ratio = (c.width - c.debit_mid) / c.debit_mid if c.debit_mid > 0 else None

            # min debit floor
            if c.debit_mid < cfg.min_debit:
                rules.append(RuleResult("min_debit", "FAIL", f"debit {c.debit_mid:.2f} < {cfg.min_debit}"))
                reasons.append(f"debit {c.debit_mid:.2f} below floor {cfg.min_debit}")
                ok = False
            else:
                rules.append(RuleResult("min_debit", PASS, f"debit {c.debit_mid:.2f}"))

            # crossed / arbitrage guard: debit must be well under width
            if c.debit_mid >= c.width:
                rules.append(RuleResult("crossed", "FAIL", "debit >= width"))
                reasons.append("debit >= width (crossed)")
                ok = False

            # leg liquidity
            for label, frac in (("long", c.long_spread_frac), ("short", c.short_spread_frac)):
                if frac is None:
                    rules.append(RuleResult("leg_spread", NOT_EVALUABLE, f"{label} unquoted"))
                    ok = False
                    reasons.append(f"{label} leg unquoted")
                elif frac > cfg.max_leg_spread_frac:
                    rules.append(
                        RuleResult("leg_spread", "FAIL", f"{label} spread {frac:.2f} > {cfg.max_leg_spread_frac}")
                    )
                    reasons.append(f"{label} leg spread {frac:.2f} exceeds {cfg.max_leg_spread_frac}")
                    ok = False
                else:
                    rules.append(RuleResult("leg_spread", PASS, f"{label} spread {frac:.2f}"))

    c.accepted = ok and c.debit_mid is not None


def _target_distance_rule(
    c: Candidate, mode: str, cfg: ScanConfig, spot: SpotContext | None, rules: list[RuleResult]
) -> None:
    """Distance-of-short-from-target rule for delta/otm modes: PASS near,
    FAIL far (premium mode and missing-input cases are handled upstream)."""
    if mode == "delta":
        if c.short_delta is None:
            return  # already NOT_EVALUABLE upstream
        band_lo, band_hi = cfg.delta_band
        ad = abs(c.short_delta)
        if band_lo <= ad <= band_hi:
            rules.append(RuleResult("delta", PASS, f"|delta| {ad:.2f} in band"))
        else:
            rules.append(RuleResult("delta", "FAIL", f"|delta| {ad:.2f} outside band"))
    elif mode == "otm" and spot is not None and spot.spot is not None:
        rules.append(RuleResult("otm", PASS, f"short {c.short_strike} vs otm target"))


def select_top(results: list[ScanResult], cfg: ScanConfig) -> list[ScanResult]:
    """Apply the book-wide candidate cap: keep the globally best yields,
    demote the rest into their scan's rejected list (nothing vanishes)."""
    pool: list[tuple[float, float, Candidate, ScanResult]] = []
    for r in results:
        for c in r.candidates:
            pool.append((-(c.yield_ratio or 0.0), c.short_strike, c, r))
    pool.sort(key=lambda t: (t[0], t[1]))
    keep = {id(c) for _, _, c, _ in pool[: cfg.max_candidates_total]}
    for r in results:
        demoted = [c for c in r.candidates if id(c) not in keep]
        for c in demoted:
            c.rank = 0
            c.accepted = False
        r.rejected.extend(demoted)
        r.candidates = [c for c in r.candidates if id(c) in keep]
    return results
