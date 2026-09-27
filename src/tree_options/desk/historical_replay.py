"""Exploratory, VWAP-priced option replay over cached daily bars.

This is not DESK-BT-001: its haircut is an explicit scenario input, cached
bars are not fills, and incomplete vendor coverage is reported as omissions.
Signals and contract selection use D's close; option entry is priced from
the NEXT session's VWAP. That VWAP remains a modeled price, not an order.
"""

from __future__ import annotations

import bisect
import math
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from tree_options.desk import ivhist, signals
from tree_options.desk.sessions import Calendar

COMMISSION = 0.65
MULTIPLIER = 100
STRUCTURES = ("long_call", "call_debit", "put_credit")


@dataclass(frozen=True)
class ReplaySpec:
    start: date
    end: date
    names: tuple[str, ...]
    signals: tuple[str, ...] = ("xsmom_top3", "pead_beat")
    structures: tuple[str, ...] = STRUCTURES
    min_dte: int = 30
    max_dte: int = 60
    hold_sessions: int = 20
    haircut: float = 0.01
    max_loss: float = 300.0

    def __post_init__(self) -> None:
        if self.end < self.start or not self.names:
            raise ValueError("invalid dates or empty names")
        if not self.signals or any(s not in signals.ALLOWED_DIRECTION for s in self.signals):
            raise ValueError("unsupported direction signal")
        if not self.structures or any(s not in STRUCTURES for s in self.structures):
            raise ValueError("unsupported option structure")
        if not (7 <= self.min_dte <= self.max_dte <= 365):
            raise ValueError("invalid expiry bounds")
        if (self.hold_sessions < 1 or not math.isfinite(self.haircut)
                or not 0 <= self.haircut < 1 or not math.isfinite(self.max_loss)
                or self.max_loss <= 0):
            raise ValueError("invalid replay risk inputs")


def merge_scans(scans: Sequence[ivhist.CacheScan]) -> ivhist.CacheScan:
    """Union cache sets; disagreeing duplicate VWAPs are excluded."""
    if not scans:
        raise ValueError("at least one cache is required")
    merged = ivhist.CacheScan(source=",".join(s.source for s in scans))
    bars: dict[tuple[str, date, date, str, float], ivhist.OptionBar] = {}
    conflicts: set[tuple[str, date, date, str, float]] = set()
    for scan in scans:
        merged.stats.update(scan.stats)
        for name, by_day in scan.options.items():
            for day, rows in by_day.items():
                for row in rows:
                    key = (name, day, row.expiry, row.right, row.strike)
                    old = bars.get(key)
                    if old is not None and old.vwap != row.vwap:
                        conflicts.add(key)
                    bars[key] = row
        for name, spot_by_day in scan.spot.items():
            spot_out = merged.spot.setdefault(name, {})
            bad = merged.spot_conflicts.setdefault(name, set())
            for day, value in spot_by_day.items():
                if day in spot_out and spot_out[day] != value:
                    bad.add(day)
                else:
                    spot_out[day] = value
            bad.update(scan.spot_conflicts.get(name, set()))
    for key in conflicts:
        bars.pop(key, None)
    for name, bad in merged.spot_conflicts.items():
        for day in bad:
            merged.spot[name].pop(day, None)
    for (name, day, _exp, _right, _strike), row in sorted(bars.items()):
        merged.options.setdefault(name, {}).setdefault(day, []).append(row)
    merged.stats["cross_cache_option_conflicts"] = len(conflicts)
    merged.stats["cross_cache_spot_conflicts"] = sum(map(len, merged.spot_conflicts.values()))
    return merged


def _signals_on(
    panel: Mapping[str, Any], earnings: Mapping[str, Iterable[str]], day: date,
    cal: Calendar, selected: Sequence[str],
) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    if "xsmom_top3" in selected:
        result = signals.xsmom_top3(panel, day, cal)
        if result.fires:
            out.extend(("xsmom_top3", name) for name in result.top3)
    if "pead_beat" in selected:
        out.extend(("pead_beat", event.name) for event in signals.pead_beats(panel, earnings, day, cal).beats)
    return out


def _exit_day(entry: date, expiry: date, cal: Calendar, hold: int) -> date | None:
    sessions = cal.sessions()
    i = bisect.bisect_left(sessions, entry)
    if i == len(sessions) or sessions[i] != entry:
        return None
    limit = min(i + hold, len(sessions) - 1)
    while limit > i and (expiry - sessions[limit]).days < 7:
        limit -= 1
    return sessions[limit] if limit > i else None


def _mark(bars: Sequence[ivhist.OptionBar]) -> dict[tuple[date, str, float], float]:
    return {(b.expiry, b.right, b.strike): b.vwap for b in bars}


def _select(
    bars: Sequence[ivhist.OptionBar], spot: float, entry: date, spec: ReplaySpec,
) -> tuple[date, list[float]] | None:
    by_exp: dict[date, dict[str, set[float]]] = {}
    for bar in bars:
        if not spec.min_dte <= (bar.expiry - entry).days <= spec.max_dte:
            continue
        by_exp.setdefault(bar.expiry, {}).setdefault(bar.right, set()).add(bar.strike)
    for exp in sorted(by_exp, reverse=True):
        calls = sorted(by_exp[exp].get("C", set()))
        puts = sorted(by_exp[exp].get("P", set()))
        both = sorted(set(calls) & set(puts))
        if not both:
            continue
        atm = min(both, key=lambda k: (abs(k - spot), k))
        if abs(atm / spot - 1) > 0.03:
            continue
        return exp, calls
    return None


def _legs(
    kind: str, expiry: date, calls: list[float], entry_marks: Mapping[tuple[date, str, float], float],
    spot: float,
) -> tuple[tuple[str, float, int], ...] | None:
    puts = sorted(k for e, right, k in entry_marks if e == expiry and right == "P")
    both = sorted(set(calls) & set(puts))
    if not both:
        return None
    atm = min(both, key=lambda k: (abs(k - spot), k))
    ci, pi = calls.index(atm), puts.index(atm)
    if kind == "long_call":
        return (("C", atm, 1),)
    if kind == "call_debit" and ci + 2 < len(calls):
        return (("C", atm, 1), ("C", calls[ci + 2], -1))
    if kind == "put_credit" and pi >= 3:
        return (("P", puts[pi - 1], -1), ("P", puts[pi - 3], 1))
    return None


def _cash(legs: Sequence[tuple[str, float, int]], marks: Mapping[tuple[date, str, float], float],
          expiry: date, haircut: float, *, entry: bool) -> float | None:
    result = 0.0
    for right, strike, side in legs:
        px = marks.get((expiry, right, strike))
        if px is None or px <= 0:
            return None
        signed = side if entry else -side
        result -= signed * px * (1 + haircut if signed > 0 else 1 - haircut) * MULTIPLIER
        result -= COMMISSION
    return result


def replay(
    scan: ivhist.CacheScan, panel: Mapping[str, Any], earnings: Mapping[str, Iterable[str]],
    cal: Calendar, spec: ReplaySpec,
) -> dict[str, Any]:
    counts: Counter[str] = Counter()
    rows: list[dict[str, Any]] = []
    attempts: list[dict[str, Any]] = []

    def note(signal: str, name: str, decision: date, kind: str, status: str,
             *, entry: date | None = None, expiry: date | None = None,
             exit_day: date | None = None, max_loss: float | None = None) -> None:
        attempts.append({"signal": signal, "name": name, "decision": decision.isoformat(),
                         "structure": kind, "status": status,
                         "entry": entry.isoformat() if entry else None,
                         "expiry": expiry.isoformat() if expiry else None,
                         "exit": exit_day.isoformat() if exit_day else None,
                         "max_loss": round(max_loss, 2) if max_loss is not None else None})

    available = set(spec.names)
    sessions = [d for d in cal.sessions() if spec.start <= d <= spec.end]
    for decision in sessions:
        for signal, name in _signals_on(panel, earnings, decision, cal, spec.signals):
            if name not in available:
                continue
            i = bisect.bisect_right(cal.sessions(), decision)
            if i >= len(cal.sessions()):
                counts["no_next_session"] += len(spec.structures)
                for kind in spec.structures:
                    note(signal, name, decision, kind, "no_next_session")
                continue
            entry = cal.sessions()[i]
            spot = scan.spot.get(name, {}).get(decision)
            decision_bars = scan.options.get(name, {}).get(decision, ())
            entry_bars = scan.options.get(name, {}).get(entry, ())
            if spot is None or not decision_bars:
                counts["missing_decision_spot_or_options"] += len(spec.structures)
                for kind in spec.structures:
                    note(signal, name, decision, kind, "missing_decision_spot_or_options",
                         entry=entry)
                continue
            selection = _select(decision_bars, spot, entry, spec)
            if selection is None:
                counts["no_eligible_expiry_or_atm"] += len(spec.structures)
                for kind in spec.structures:
                    note(signal, name, decision, kind, "no_eligible_expiry_or_atm", entry=entry)
                continue
            expiry, calls = selection
            exit_day = _exit_day(entry, expiry, cal, spec.hold_sessions)
            if exit_day is None:
                counts["no_exit_session"] += len(spec.structures)
                for kind in spec.structures:
                    note(signal, name, decision, kind, "no_exit_session", entry=entry, expiry=expiry)
                continue
            decision_marks = _mark(decision_bars)
            start_marks = _mark(entry_bars)
            end_marks = _mark(scan.options.get(name, {}).get(exit_day, ()))
            for kind in spec.structures:
                counts["attempted"] += 1
                legs = _legs(kind, expiry, calls, decision_marks, spot)
                if legs is None:
                    counts["missing_entry_leg"] += 1
                    note(signal, name, decision, kind, "missing_entry_leg", entry=entry,
                         expiry=expiry, exit_day=exit_day)
                    continue
                opening = _cash(legs, start_marks, expiry, spec.haircut, entry=True)
                closing = _cash(legs, end_marks, expiry, spec.haircut, entry=False)
                if opening is None or closing is None:
                    counts["missing_exit_or_entry_bar"] += 1
                    note(signal, name, decision, kind, "missing_exit_or_entry_bar", entry=entry,
                         expiry=expiry, exit_day=exit_day)
                    continue
                if kind == "put_credit":
                    width = abs(legs[0][1] - legs[1][1]) * MULTIPLIER
                    max_loss = width - opening + len(legs) * COMMISSION
                else:
                    max_loss = -opening + len(legs) * COMMISSION
                if max_loss <= 0:
                    counts["invalid_payoff"] += 1
                    note(signal, name, decision, kind, "invalid_payoff", entry=entry,
                         expiry=expiry, exit_day=exit_day, max_loss=max_loss)
                    continue
                if max_loss > spec.max_loss:
                    counts["over_trade_loss_cap"] += 1
                    note(signal, name, decision, kind, "over_trade_loss_cap", entry=entry,
                         expiry=expiry, exit_day=exit_day, max_loss=max_loss)
                    continue
                pnl = opening + closing
                rows.append({"signal": signal, "name": name, "structure": kind,
                             "decision": decision.isoformat(), "selection_as_of": decision.isoformat(),
                             "entry": entry.isoformat(),
                             "exit": exit_day.isoformat(), "expiry": expiry.isoformat(),
                             "legs": [{"right": right, "strike": strike, "side": side}
                                      for right, strike, side in legs],
                             "max_loss": round(max_loss, 2), "pnl": round(pnl, 2),
                             "win": pnl > 0})
                counts["evaluable_within_trade_cap"] += 1
                note(signal, name, decision, kind, "evaluable_within_trade_cap", entry=entry,
                     expiry=expiry, exit_day=exit_day, max_loss=max_loss)
    def summary(subset: list[dict[str, Any]]) -> dict[str, Any]:
        pnls = sorted(float(r["pnl"]) for r in subset)
        wins = sum(p > 0 for p in pnls)
        return {"trades": len(pnls), "wins": wins,
                "win_rate": wins / len(pnls) if pnls else None,
                "mean_pnl": sum(pnls) / len(pnls) if pnls else None,
                "worst_pnl": pnls[0] if pnls else None,
                "p10_pnl": pnls[max(0, int(0.1 * (len(pnls) - 1)))] if pnls else None,
                "total_pnl_unconstrained": round(sum(pnls), 2)}

    by_structure = {kind: summary([r for r in rows if r["structure"] == kind])
                    for kind in spec.structures}
    by_variant = {f"{signal}/{kind}": summary([r for r in rows if
                  r["structure"] == kind and r["signal"] == signal])
                  for signal in spec.signals for kind in spec.structures}
    eligibility_by_variant = {
        f"{signal}/{kind}": dict(sorted(Counter(a["status"] for a in attempts if
                                     a["signal"] == signal and a["structure"] == kind).items()))
        for signal in spec.signals for kind in spec.structures
    }
    return {"schema": "desk-historical-replay/1", "label": "exploratory modeled VWAP replay",
            "limitations": ["daily VWAP is not an executable quote or broker fill",
                            "haircut is assumed, not measured from entry quotes",
                            "sealed earnings dates lack historical announcement-time vintages",
                            "contract selection only sees contracts with a decision-day trade bar",
                            "overlapping trades are counted independently; no portfolio or daily risk simulation",
                            "missing bars and cap exclusions select the sample"],
            "spec": {"start": spec.start.isoformat(), "end": spec.end.isoformat(),
                     "names": list(spec.names), "signals": list(spec.signals),
                     "structures": list(spec.structures), "entry_dte": [spec.min_dte, spec.max_dte],
                     "hold_sessions": spec.hold_sessions, "haircut": spec.haircut,
                     "max_loss": spec.max_loss},
            "counts": dict(sorted(counts.items())), "by_structure": by_structure,
            "by_variant": by_variant, "eligibility_by_variant": eligibility_by_variant,
            "attempts": attempts, "rows": rows}
