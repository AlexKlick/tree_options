#!/usr/bin/env python3
"""Coverage inspector for the M4-A real-data lane (plan §3 WS-B).

One-pass statistics over a Cboe Option EOD parse result
(`tree_options.data.cboe_eod.parse_cboe_eod_csv`) plus its
`RealOptionOverlay` (the contract-master facts the day files reference):
rows and issues, contracts / distinct expiries / strike-grid width in
spot units per underlying-day, zero-bid and zero-greeks fractions,
((ask-bid)/mid) median + p90 bucketed ATM (|delta| 0.30-0.60) vs wings
against the spike §6 priors, OI/volume concentration by delta band and
nearest-4 tenor, delivery-code distribution, early-close sessions, and
underlying bid/ask sanity. Prints a human markdown summary to stdout and
writes machine JSON to --out-json — the G2 coverage brief's input.

Fail-loud discipline (task contract): every metric reads its fields via
`_required` — a None field raises instead of silently degrading, and
structural oddities (empty chains, mixed 1545 presence, zero underlying
ask, unresolvable contract ids) refuse loudly. The adapter modules are
resolved at RUNTIME because WS-A lands in the same branch; the metric
engine itself is adapter-free, duck-typed against the shared interface
contract via Protocols. Spot = the day file's underlying mid
((bid+ask)/2 of the adapter-mapped file-level underlying quotes — the
OptionDayFile shape carries no active_underlying_price).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

from tree_options.schemas.options import OptionContract
from tree_options.synth_options.generate import OptionDayFile

REPORT_VERSION = "m4-coverage/1"
ATM_DELTA_LO = Decimal("0.30")
ATM_DELTA_HI = Decimal("0.60")
NEAREST_EXPIRIES = 4
ISSUES_HEAD_LIMIT = 20
ATM_PRIOR_NOTE = "~1-2% of mid at-the-money (spike m3 §6.3)"
WINGS_PRIOR_NOTE = "wider than ATM (spike m3 §6.3)"
SPANS_EARNINGS_NOTE = (
    "no earnings events source exists in the Cboe EOD product (spike §6.7, "
    "plan §4): the candidate filter must see NOT_EVALUABLE for real data, "
    "never a silently False flag"
)


# ---- shared-interface duck types (WS-A contract, no adapter import) ----------


class EodStatsSource(Protocol):
    rows_total: int
    rows_mapped: int
    duplicate_rows: int
    zero_greeks_rows: int
    zero_bid_rows: int
    early_close_sessions: int
    nonstandard_delivery_rows: int


class EodParseResultSource(Protocol):
    source_path: Path
    source_sha256: str
    day_files: dict[date, OptionDayFile]
    stats: EodStatsSource
    issues: Sequence[str]


class ContractLookup(Protocol):
    def contract(self, contract_id: str) -> OptionContract: ...


def _required(obj: Any, name: str) -> Any:
    """Loud field access: a None field raises — no metric degrades to None."""
    value = getattr(obj, name)
    if value is None:
        raise ValueError(
            f"{type(obj).__name__}.{name} is None — the metric needs it; refusing to degrade"
        )
    return value


# ---- report shapes -----------------------------------------------------------


@dataclass(frozen=True)
class SpreadStats:
    n: int
    median: Decimal | None  # None only when n == 0 (empty bucket, reported)
    p90: Decimal | None


@dataclass(frozen=True)
class SpreadBands:
    atm: SpreadStats
    wings: SpreadStats


@dataclass(frozen=True)
class Concentration:
    oi_in_delta_band: int
    oi_total: int
    vol_in_delta_band: int
    vol_total: int
    oi_in_nearest4: int
    vol_in_nearest4: int


@dataclass(frozen=True)
class UnderlyingDay:
    underlying: str
    session: date
    contracts: int
    distinct_expiries: int
    min_strike: Decimal
    max_strike: Decimal
    spot_mid: Decimal
    strike_width_ratio: Decimal  # max_strike / min_strike, spot units
    zero_bid_entries: int
    zero_greeks_entries: int
    early_close: bool
    delivery_standard: int
    delivery_nonstandard: int
    underlying_quote_crossed: bool
    underlying_bid_zero: bool
    concentration: Concentration


@dataclass(frozen=True)
class UnderlyingCoverage:
    underlying: str
    days: tuple[UnderlyingDay, ...]
    contract_rows: int
    distinct_contracts: int
    zero_bid_fraction: Decimal
    zero_greeks_fraction: Decimal
    spreads: SpreadBands
    concentration: Concentration
    delivery_standard: int
    delivery_nonstandard: int
    crossed_files: int
    zero_bid_underlying_files: int


@dataclass(frozen=True)
class CoverageReport:
    report_version: str
    variant: str
    source_path: Path
    source_sha256: str
    rows_total: int
    rows_mapped: int
    duplicate_rows: int
    zero_greeks_rows: int
    zero_bid_rows: int
    early_close_sessions: int  # echoed from parse stats
    early_close_sessions_from_files: int
    nonstandard_delivery_rows: int
    issues_count: int
    issues_head: tuple[str, ...]
    underlyings: tuple[UnderlyingCoverage, ...]
    underlying_days: tuple[UnderlyingDay, ...]
    # pooled across every underlying
    contract_rows: int
    distinct_contracts: int
    zero_bid_fraction: Decimal
    zero_greeks_fraction: Decimal
    spreads: SpreadBands
    concentration: Concentration
    delivery_standard: int
    delivery_nonstandard: int
    crossed_files: int
    zero_bid_underlying_files: int


# ---- statistics helpers ------------------------------------------------------


def _median(values: Sequence[Decimal]) -> Decimal | None:
    if not values:
        return None
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2 == 1:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def _p90(values: Sequence[Decimal]) -> Decimal | None:
    """Nearest-rank 90th percentile: the ceil(0.90*n)-th smallest value."""
    if not values:
        return None
    ordered = sorted(values)
    n = len(ordered)
    rank = (9 * n + 9) // 10  # == ceil(0.9 * n) in exact integer arithmetic
    return ordered[max(0, min(rank - 1, n - 1))]


def _spread_stats(values: Sequence[Decimal]) -> SpreadStats:
    return SpreadStats(n=len(values), median=_median(values), p90=_p90(values))


def _share(numerator: int, denominator: int) -> Decimal | None:
    """None only when the population is empty (reported as n=0, never hidden)."""
    if denominator == 0:
        return None
    return Decimal(numerator) / Decimal(denominator)


def _in_atm_band(abs_delta: Decimal) -> bool:
    return ATM_DELTA_LO <= abs_delta <= ATM_DELTA_HI


class _ContractCache:
    def __init__(self, overlay: ContractLookup) -> None:
        self._overlay = overlay
        self._cache: dict[str, OptionContract] = {}

    def get(self, contract_id: str) -> OptionContract:
        contract = self._cache.get(contract_id)
        if contract is None:
            try:
                contract = self._overlay.contract(contract_id)
            except (KeyError, ValueError) as exc:
                raise ValueError(f"overlay has no contract master row for {contract_id!r}") from exc
            self._cache[contract_id] = contract
        return contract


@dataclass(frozen=True)
class _DayScan:
    day: UnderlyingDay
    cids: frozenset[str]
    atm_spreads: tuple[Decimal, ...]
    wing_spreads: tuple[Decimal, ...]


def _scan_day(file: OptionDayFile, contracts: _ContractCache) -> _DayScan:
    sid = _required(file, "underlying_security_id")
    session = _required(file, "session")
    entries = _required(file, "entries")
    if not entries:
        raise ValueError(
            f"{sid} {session}: day file has no entries — refusing (no silent empty chains)"
        )
    underlying_bid = _required(file, "underlying_bid")
    underlying_ask = _required(file, "underlying_ask")
    if underlying_ask <= 0:
        raise ValueError(
            f"{sid} {session}: underlying_ask={underlying_ask} <= 0 — spot mid undefined; "
            "refusing rather than reporting zeros as tradable quotes"
        )
    spot_mid = (underlying_bid + underlying_ask) / 2

    n = len(entries)
    with_1545 = sum(1 for e in entries if e.quote_1545 is not None)
    if with_1545 not in (0, n):
        raise ValueError(
            f"{sid} {session}: mixed 1545 snapshot presence ({with_1545}/{n}) — "
            "a session is either regular or early-close, never half"
        )

    strikes: set[Decimal] = set()
    expiries: set[date] = set()
    zero_bid = zero_greeks = 0
    delivery_standard = delivery_nonstandard = 0
    oi_band = oi_total = vol_band = vol_total = 0
    atm: list[Decimal] = []
    wings: list[Decimal] = []
    cids: set[str] = set()
    rows: list[tuple[date, int, int]] = []  # (expiration, open_interest, volume)

    for entry in entries:
        cid = _required(entry, "contract_id")
        eod = _required(entry, "quote_eod")
        abs_delta = _required(entry, "abs_delta")
        open_interest = _required(entry, "open_interest")
        same_day_volume = _required(entry, "same_day_volume")
        contract = contracts.get(cid)
        expiration = _required(contract, "expiration")
        strike = _required(contract, "strike")
        standard = _required(contract, "standard_contract_flag")

        cids.add(cid)
        strikes.add(strike)
        expiries.add(expiration)
        rows.append((expiration, open_interest, same_day_volume))
        oi_total += open_interest
        vol_total += same_day_volume
        if standard:
            delivery_standard += 1
        else:
            delivery_nonstandard += 1
        if eod.bid == 0 and eod.ask == 0:
            zero_bid += 1
        if abs_delta == 0:
            zero_greeks += 1
        if _in_atm_band(abs_delta):
            oi_band += open_interest
            vol_band += same_day_volume
        mid = (eod.bid + eod.ask) / 2
        if mid > 0 and abs_delta > 0:
            # zero-greeks rows (abs_delta == 0) have no trustworthy band;
            # zero-mid rows have no relative spread — both counted above
            (atm if _in_atm_band(abs_delta) else wings).append((eod.ask - eod.bid) / mid)

    nearest = set(sorted(expiries)[:NEAREST_EXPIRIES])
    oi_n4 = sum(oi for exp, oi, _vol in rows if exp in nearest)
    vol_n4 = sum(vol for exp, _exp, vol in rows if exp in nearest)

    day = UnderlyingDay(
        underlying=sid,
        session=session,
        contracts=n,
        distinct_expiries=len(expiries),
        min_strike=min(strikes),
        max_strike=max(strikes),
        spot_mid=spot_mid,
        strike_width_ratio=max(strikes) / min(strikes),
        zero_bid_entries=zero_bid,
        zero_greeks_entries=zero_greeks,
        early_close=with_1545 == 0,
        delivery_standard=delivery_standard,
        delivery_nonstandard=delivery_nonstandard,
        underlying_quote_crossed=underlying_bid > underlying_ask,
        underlying_bid_zero=underlying_bid == 0,
        concentration=Concentration(
            oi_in_delta_band=oi_band,
            oi_total=oi_total,
            vol_in_delta_band=vol_band,
            vol_total=vol_total,
            oi_in_nearest4=oi_n4,
            vol_in_nearest4=vol_n4,
        ),
    )
    return _DayScan(
        day=day, cids=frozenset(cids), atm_spreads=tuple(atm), wing_spreads=tuple(wings)
    )


@dataclass(frozen=True)
class _Pool:
    contract_rows: int
    distinct_contracts: int
    zero_bid: int
    zero_greeks: int
    spreads: SpreadBands
    concentration: Concentration
    delivery_standard: int
    delivery_nonstandard: int
    crossed_files: int
    zero_bid_underlying_files: int


def _pool(scans: Sequence[_DayScan]) -> _Pool:
    atm = [rel for scan in scans for rel in scan.atm_spreads]
    wings = [rel for scan in scans for rel in scan.wing_spreads]
    cids: set[str] = set()
    for scan in scans:
        cids |= scan.cids
    return _Pool(
        contract_rows=sum(scan.day.contracts for scan in scans),
        distinct_contracts=len(cids),
        zero_bid=sum(scan.day.zero_bid_entries for scan in scans),
        zero_greeks=sum(scan.day.zero_greeks_entries for scan in scans),
        spreads=SpreadBands(atm=_spread_stats(atm), wings=_spread_stats(wings)),
        concentration=Concentration(
            oi_in_delta_band=sum(s.day.concentration.oi_in_delta_band for s in scans),
            oi_total=sum(s.day.concentration.oi_total for s in scans),
            vol_in_delta_band=sum(s.day.concentration.vol_in_delta_band for s in scans),
            vol_total=sum(s.day.concentration.vol_total for s in scans),
            oi_in_nearest4=sum(s.day.concentration.oi_in_nearest4 for s in scans),
            vol_in_nearest4=sum(s.day.concentration.vol_in_nearest4 for s in scans),
        ),
        delivery_standard=sum(scan.day.delivery_standard for scan in scans),
        delivery_nonstandard=sum(scan.day.delivery_nonstandard for scan in scans),
        crossed_files=sum(1 for scan in scans if scan.day.underlying_quote_crossed),
        zero_bid_underlying_files=sum(1 for scan in scans if scan.day.underlying_bid_zero),
    )


def build_coverage_report(
    parse_result: EodParseResultSource,
    overlay: ContractLookup,
    *,
    variant: str = "cgi_or_historical",
) -> CoverageReport:
    """Compute the full coverage report from a parse result + its overlay.

    The parse result supplies the day files, the echoed ingest stats and
    the issue log; the overlay supplies contract-master facts (expiry,
    strike, standard-deliverable flag) for every entry contract id.
    """
    day_files = _required(parse_result, "day_files")
    if not day_files:
        raise ValueError("parse result carries no day files — nothing to inspect")
    stats = _required(parse_result, "stats")
    issues = list(_required(parse_result, "issues"))

    contracts = _ContractCache(overlay)
    scans = [_scan_day(day_files[session], contracts) for session in sorted(day_files)]

    by_sid: dict[str, list[_DayScan]] = {}
    for scan in scans:
        by_sid.setdefault(scan.day.underlying, []).append(scan)

    underlyings = tuple(
        UnderlyingCoverage(
            underlying=sid,
            days=tuple(scan.day for scan in sid_scans),
            **_as_pool_kwargs(_pool(sid_scans)),
        )
        for sid, sid_scans in sorted(by_sid.items())
    )
    total = _pool(scans)
    return CoverageReport(
        report_version=REPORT_VERSION,
        variant=variant,
        source_path=_required(parse_result, "source_path"),
        source_sha256=_required(parse_result, "source_sha256"),
        rows_total=_required(stats, "rows_total"),
        rows_mapped=_required(stats, "rows_mapped"),
        duplicate_rows=_required(stats, "duplicate_rows"),
        zero_greeks_rows=_required(stats, "zero_greeks_rows"),
        zero_bid_rows=_required(stats, "zero_bid_rows"),
        early_close_sessions=_required(stats, "early_close_sessions"),
        early_close_sessions_from_files=sum(1 for scan in scans if scan.day.early_close),
        nonstandard_delivery_rows=_required(stats, "nonstandard_delivery_rows"),
        issues_count=len(issues),
        issues_head=tuple(issues[:ISSUES_HEAD_LIMIT]),
        underlyings=underlyings,
        underlying_days=tuple(
            scan.day for scan in sorted(scans, key=lambda s: (s.day.underlying, s.day.session))
        ),
        **_as_pool_kwargs(total),
    )


def _as_pool_kwargs(pool: _Pool) -> dict[str, Any]:
    """Split a _Pool into UnderlyingCoverage/CoverageReport field kwargs."""
    contract_rows = pool.contract_rows
    zero_bid_fraction = Decimal(pool.zero_bid) / Decimal(contract_rows)
    zero_greeks_fraction = Decimal(pool.zero_greeks) / Decimal(contract_rows)
    return {
        "contract_rows": contract_rows,
        "distinct_contracts": pool.distinct_contracts,
        "zero_bid_fraction": zero_bid_fraction,
        "zero_greeks_fraction": zero_greeks_fraction,
        "spreads": pool.spreads,
        "concentration": pool.concentration,
        "delivery_standard": pool.delivery_standard,
        "delivery_nonstandard": pool.delivery_nonstandard,
        "crossed_files": pool.crossed_files,
        "zero_bid_underlying_files": pool.zero_bid_underlying_files,
    }


# ---- machine JSON ------------------------------------------------------------


def _f(value: Decimal | None) -> float | None:
    return None if value is None else float(value)


def _spread_json(stats: SpreadStats, prior: str) -> dict[str, Any]:
    return {
        "n": stats.n,
        "median": _f(stats.median),
        "p90": _f(stats.p90),
        "prior": prior,
    }


def _concentration_json(c: Concentration) -> dict[str, Any]:
    return {
        "delta_band": [float(ATM_DELTA_LO), float(ATM_DELTA_HI)],
        "nearest_expiries": NEAREST_EXPIRIES,
        "oi_delta_band_share": _f(_share(c.oi_in_delta_band, c.oi_total)),
        "vol_delta_band_share": _f(_share(c.vol_in_delta_band, c.vol_total)),
        "oi_nearest4_share": _f(_share(c.oi_in_nearest4, c.oi_total)),
        "vol_nearest4_share": _f(_share(c.vol_in_nearest4, c.vol_total)),
        "oi_total": c.oi_total,
        "vol_total": c.vol_total,
    }


def _days_json(days: Sequence[UnderlyingDay]) -> list[dict[str, Any]]:
    return [
        {
            "session": day.session.isoformat(),
            "contracts": day.contracts,
            "distinct_expiries": day.distinct_expiries,
            "min_strike": _f(day.min_strike),
            "max_strike": _f(day.max_strike),
            "spot_mid": _f(day.spot_mid),
            "strike_width_ratio": _f(day.strike_width_ratio),
            "zero_bid_entries": day.zero_bid_entries,
            "zero_greeks_entries": day.zero_greeks_entries,
            "early_close": day.early_close,
            "delivery_standard": day.delivery_standard,
            "delivery_nonstandard": day.delivery_nonstandard,
            "underlying_quote_crossed": day.underlying_quote_crossed,
            "underlying_bid_zero": day.underlying_bid_zero,
        }
        for day in days
    ]


def report_to_json(report: CoverageReport) -> dict[str, Any]:
    return {
        "report_version": report.report_version,
        "variant": report.variant,
        "source": {"path": str(report.source_path), "sha256": report.source_sha256},
        "parse_stats": {
            "rows_total": report.rows_total,
            "rows_mapped": report.rows_mapped,
            "duplicate_rows": report.duplicate_rows,
            "zero_greeks_rows": report.zero_greeks_rows,
            "zero_bid_rows": report.zero_bid_rows,
            "early_close_sessions": report.early_close_sessions,
            "nonstandard_delivery_rows": report.nonstandard_delivery_rows,
        },
        "issues_count": report.issues_count,
        "issues_head": list(report.issues_head),
        "aggregate": {
            "underlyings": len(report.underlyings),
            "underlying_days": len(report.underlying_days),
            "contract_rows": report.contract_rows,
            "distinct_contracts": report.distinct_contracts,
            "zero_bid_fraction": _f(report.zero_bid_fraction),
            "zero_greeks_fraction": _f(report.zero_greeks_fraction),
            "spreads": {
                "atm": _spread_json(report.spreads.atm, ATM_PRIOR_NOTE),
                "wings": _spread_json(report.spreads.wings, WINGS_PRIOR_NOTE),
            },
            "concentration": _concentration_json(report.concentration),
            "delivery_code_distribution": {
                "standard": report.delivery_standard,
                "nonstandard": report.delivery_nonstandard,
            },
            "early_close_sessions": report.early_close_sessions,
            "early_close_sessions_from_files": report.early_close_sessions_from_files,
            "underlying_quote_violations": {
                "crossed_files": report.crossed_files,
                "zero_bid_underlying_files": report.zero_bid_underlying_files,
            },
        },
        "per_underlying": {
            u.underlying: {
                "sessions": [day.session.isoformat() for day in u.days],
                "contract_rows": u.contract_rows,
                "distinct_contracts": u.distinct_contracts,
                "zero_bid_fraction": _f(u.zero_bid_fraction),
                "zero_greeks_fraction": _f(u.zero_greeks_fraction),
                "spreads": {
                    "atm": _spread_json(u.spreads.atm, ATM_PRIOR_NOTE),
                    "wings": _spread_json(u.spreads.wings, WINGS_PRIOR_NOTE),
                },
                "concentration": _concentration_json(u.concentration),
                "delivery_code_distribution": {
                    "standard": u.delivery_standard,
                    "nonstandard": u.delivery_nonstandard,
                },
                "underlying_quote_violations": {
                    "crossed_files": u.crossed_files,
                    "zero_bid_underlying_files": u.zero_bid_underlying_files,
                },
                "days": _days_json(u.days),
            }
            for u in report.underlyings
        },
        "spans_earnings": {"status": "NOT_EVALUABLE", "value": None, "note": SPANS_EARNINGS_NOTE},
    }


# ---- human markdown ----------------------------------------------------------


def _pct(value: Decimal | None) -> str:
    return "n/a" if value is None else f"{float(value) * 100:.2f}%"


def _num(value: Decimal | None, places: int = 4) -> str:
    return "n/a" if value is None else f"{float(value):.{places}f}"


def _spread_row(name: str, stats: SpreadStats, prior: str) -> str:
    return f"| {name} | {stats.n} | {_pct(stats.median)} | {_pct(stats.p90)} | {prior} |"


def _pool_markdown(
    title: str,
    spreads: SpreadBands,
    concentration: Concentration,
    delivery_standard: int,
    delivery_nonstandard: int,
) -> list[str]:
    c = concentration
    return [
        f"## {title}",
        "",
        "| band | n | median | p90 | prior |",
        "|---|---|---|---|---|",
        _spread_row(f"ATM ({ATM_DELTA_LO}<=|delta|<={ATM_DELTA_HI})", spreads.atm, ATM_PRIOR_NOTE),
        _spread_row("wings (other, delta>0)", spreads.wings, WINGS_PRIOR_NOTE),
        "",
        "## Concentration",
        "",
        f"- delta band {ATM_DELTA_LO}-{ATM_DELTA_HI}: OI {_pct(_share(c.oi_in_delta_band, c.oi_total))}"
        f" volume {_pct(_share(c.vol_in_delta_band, c.vol_total))}",
        f"- nearest-{NEAREST_EXPIRIES} expiries: OI {_pct(_share(c.oi_in_nearest4, c.oi_total))}"
        f" volume {_pct(_share(c.vol_in_nearest4, c.vol_total))}",
        "",
        "## Delivery codes (rows)",
        "",
        f"- standard={delivery_standard} nonstandard={delivery_nonstandard}",
    ]


def render_markdown(report: CoverageReport) -> str:
    lines = [
        f"# Cboe EOD options coverage — {report.source_path.name}",
        "",
        f"- source: `{report.source_path}` (sha256 `{report.source_sha256[:12]}…`)",
        f"- variant: `{report.variant}`",
        f"- rows: total={report.rows_total} mapped={report.rows_mapped}"
        f" duplicates={report.duplicate_rows} issues={report.issues_count}",
        f"- odd rows: zero-greeks={report.zero_greeks_rows} zero-bid={report.zero_bid_rows}"
        f" nonstandard-delivery={report.nonstandard_delivery_rows}",
        f"- scope: underlyings={len(report.underlyings)}"
        f" underlying-days={len(report.underlying_days)}"
        f" contract-rows={report.contract_rows}"
        f" distinct-contracts={report.distinct_contracts}",
        f"- quality: zero-bid {_pct(report.zero_bid_fraction)}"
        f" | zero-greeks {_pct(report.zero_greeks_fraction)}",
        f"- underlying quote violations: crossed={report.crossed_files}"
        f" zero-bid-underlying={report.zero_bid_underlying_files}",
        "",
        "## Underlying-days",
        "",
        "| underlying | session | contracts | expiries | min K | max K | maxK/minK | spot mid | zero-bid | zero-greeks | early |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for day in report.underlying_days:
        lines.append(
            f"| {day.underlying} | {day.session} | {day.contracts} | {day.distinct_expiries}"
            f" | {day.min_strike} | {day.max_strike} | {_num(day.strike_width_ratio)}"
            f" | {day.spot_mid} | {day.zero_bid_entries} | {day.zero_greeks_entries}"
            f" | {'yes' if day.early_close else 'no'} |"
        )
    lines.append("")
    if report.early_close_sessions == report.early_close_sessions_from_files:
        lines.append(
            f"- early-close sessions: {report.early_close_sessions} (parse stats == files)"
        )
    else:
        lines.append(
            f"- EARLY_CLOSE_MISMATCH stats={report.early_close_sessions}"
            f" files={report.early_close_sessions_from_files}"
        )
    lines.extend(
        _pool_markdown(
            "Aggregate spreads ((ask-bid)/mid, EOD snapshot, mid>0)",
            report.spreads,
            report.concentration,
            report.delivery_standard,
            report.delivery_nonstandard,
        )
    )
    for u in report.underlyings:
        lines.extend(
            _pool_markdown(
                f"{u.underlying} — spreads, concentration, delivery",
                u.spreads,
                u.concentration,
                u.delivery_standard,
                u.delivery_nonstandard,
            )
        )
    lines.extend(
        [
            "## NOT_EVALUABLE inputs",
            "",
            f"- spans_earnings: NOT_EVALUABLE — {SPANS_EARNINGS_NOTE}",
            "",
        ]
    )
    return "\n".join(lines)


# ---- CLI (adapter resolved at runtime: WS-A lands concurrently) ---------------


def _load_adapter() -> tuple[Any, Callable[[Any], Any]]:
    """Resolve (parse_cboe_eod_csv, overlay_factory) per the shared contract.

    The overlay factory prefers the adapter's `build_real_overlay(result)`
    when it exists: it wires the PARSED contract master into the overlay, so
    delivery_code standardness survives (the bare constructor's id
    decomposition falls back to all-standard). The bare-constructor path
    stays as the contracted fallback.
    """
    import importlib
    import importlib.util

    names = ("tree_options.data.cboe_eod", "tree_options.data.real_overlay")
    missing = [
        name for name in names if name not in sys.modules and importlib.util.find_spec(name) is None
    ]
    if missing:
        raise SystemExit(
            f"adapter module(s) missing (WS-A, plan §3): {', '.join(missing)} — "
            "the inspector runs against tree_options.data.cboe_eod + real_overlay"
        )
    cboe = sys.modules.get(names[0]) or importlib.import_module(names[0])
    real = sys.modules.get(names[1]) or importlib.import_module(names[1])
    parse = getattr(cboe, "parse_cboe_eod_csv", None)
    overlay_cls = getattr(real, "RealOptionOverlay", None)
    if parse is None or overlay_cls is None:
        raise SystemExit("adapter contract violation: parse_cboe_eod_csv/RealOptionOverlay missing")
    builder = getattr(real, "build_real_overlay", None)

    def overlay_factory(result: Any) -> Any:
        if builder is not None:
            return builder(result)
        return overlay_cls(result.day_files, source_sha256=result.source_sha256)

    return parse, overlay_factory


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, required=True, help="Cboe Option EOD summary CSV")
    parser.add_argument(
        "--variant",
        choices=("cgi_or_historical", "no_cgi"),
        default="cgi_or_historical",
        help="file variant (index underlyings require cgi_or_historical)",
    )
    parser.add_argument("--out-json", type=Path, required=True, help="machine JSON output path")
    parser.add_argument(
        "--underlying",
        default=None,
        help=(
            "select one underlying symbol when the CSV bundles several "
            "(integration reconciliation: the adapter refuses multi-underlying"
            " files without a selection)"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not args.csv.is_file():
        raise SystemExit(f"csv not found: {args.csv}")
    parse_csv, overlay_factory = _load_adapter()
    result = parse_csv(args.csv, variant=args.variant, underlying=args.underlying)
    overlay = overlay_factory(result)
    report = build_coverage_report(result, overlay, variant=args.variant)
    args.out_json.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(report_to_json(report), indent=2) + "\n"
    args.out_json.write_text(payload, encoding="utf-8")
    print(render_markdown(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
