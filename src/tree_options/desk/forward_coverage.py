"""Eight-clock forward coverage: the sealed restart rule's C1/C3 evidence.

WHY THIS EXISTS
---------------
``forward-verify`` (PR #55) asserts bar coverage at the THREE outcome-table
decision clocks (``10:00``/``10:15``/``15:15`` ET, ``outcomes.py``
``decision_clocks_et``). Its ``ok: true`` is evidence that the corpus
accumulates, NOT evidence that restart threshold C1 (eight-clock
observation) is met -- the scope trap the campaign exit records
(``docs/desk/CAMPAIGN-EXIT-20260930.md``, "Restart conditions", scope
note): the restart scorer must compute eight-clock coverage from the
captured full-session bars itself. This module is that computation, kept
apart from the capture path so the sealed rule's future scorer can cite it
without touching the wire.

Semantics, mirroring ``intraday_action_graph``'s staleness discipline:

* the decision clocks are ``intraday_action_graph.SCHEDULE`` -- the eight
  ET instants 10:00, 10:45, 11:30, 12:15, 13:00, 13:45, 14:30, 15:15 that
  ``docs/desk/RESTART-THRESHOLD.md`` C1 names (a test pins all three
  together);
* a contract is COVERED at clock C when its captured per-contract minute
  bars contain a bar in the half-open window ``[C, C+15min)`` -- a real
  observation at-or-after the decision instant, inside the same
  15-minute whole-second age horizon ``intraday_action_graph.replay``
  gives ``_latest`` at decision time (``max_age_minutes=15``). The window
  is closed at C and open at C+15:00: a bar at exactly C+15:00 is stale,
  one at C+14:59 is fresh;
* a missing bar is a GAP, never a fill: every contract the capture
  attempted (``bars/<D>.json`` carries one entry per selected contract)
  counts in every clock's denominator, and the contracts the wire budget
  or the validator refused are listed missing with that reason.

The scorer decides NOTHING. It produces raw per-clock numbers and boolean
facts (``every clock >= 50% covered`` style); the qualifying thresholds
stay the operator's sealed call. Nothing here authorizes execution.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any

from tree_options.desk import store
from tree_options.desk.forward_minutes import (
    BARS_SCHEMA,
    SELECTION_SCHEMA,
    bars_path,
    clock_ms,
    forward_root,
    latest_selection,
    selection_path,
)
from tree_options.desk.intraday_action_graph import ET
from tree_options.desk.intraday_action_graph import SCHEDULE as EIGHT_CLOCKS

#: the ``_latest`` age horizon at decision time, in whole seconds between
#: UTC instants (intraday_action_graph.replay: max_age_minutes=15)
AGE_WINDOW_SECONDS = 15 * 60

#: boolean facts rendered beside the raw numbers. 50 is the example floor
#: of the task that commissioned this scorer; 90 is RESTART-THRESHOLD C3's
#: pooled floor, printed here as a fact only. NEITHER is a verdict: the
#: qualifying threshold is the operator's sealed call, and C3 pools
#: (session, clock) points across the whole qualifying corpus.
FACT_FLOORS_PCT = (50.0, 90.0)

COVERAGE_SCHEMA = "desk-forward-clock-coverage/1"

# the shared desk convention keeps 0/1/2/3; these sit beside the forward
# family's fixed meanings (4: selection missing, run forward-select)
NO_BARS_EXIT = 3  # nothing captured for the session yet: run forward-minutes
MISSING_SELECTION_EXIT = 4


class CoverageInputError(RuntimeError):
    """A missing/unusable input, with the CLI's exit code (fixed text, no paths)."""

    def __init__(self, message: str, exit_code: int) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def coverage_path(session: date, root: Path | None = None) -> Path:
    return (root or forward_root()) / "coverage" / f"{session}.json"


def _parse_schedule(schedule: Sequence[str]) -> tuple[str, ...]:
    if not isinstance(schedule, (tuple, list)) or not schedule:
        raise ValueError("schedule must be a non-empty sequence of ET clocks")
    seen: set[str] = set()
    for clock in schedule:
        if not isinstance(clock, str) or clock.count(":") != 1:
            raise ValueError(f"clock must be HH:MM ET: {clock!r}")
        try:
            hour, minute = (int(part) for part in clock.split(":"))
            if not 0 <= hour <= 23 or not 0 <= minute <= 59:
                raise ValueError
        except ValueError:
            raise ValueError(f"clock must be HH:MM ET: {clock!r}") from None
        if clock in seen:
            raise ValueError(f"duplicate clock: {clock}")
        seen.add(clock)
    return tuple(schedule)


def _windows(session: date, schedule: tuple[str, ...]) -> list[tuple[str, int, int]]:
    """(clock, lo_ms, hi_ms): each clock's half-open [C, C+15min) window."""
    out = []
    for clock in schedule:
        lo = clock_ms(session, clock)
        out.append((clock, lo, lo + AGE_WINDOW_SECONDS * 1000))
    return out


def clock_coverage(
    bars_doc: Mapping[str, Any], schedule: Sequence[str] = EIGHT_CLOCKS
) -> dict[str, Any]:
    """Per-clock coverage of one captured session's full minute bars.

    Pure: no IO, no clock, no wire. ``bars_doc`` is the parsed
    ``bars/<D>.json`` document (``desk-forward-minute-bars/1``); every
    contract entry in it -- whatever its capture status -- is a denominator
    member at every clock, so a wire-budget or validation gap is reported
    as missing coverage, never dropped from the denominator. Returns the
    clocks table, a per-contract detail map, and a session-level summary
    whose only evaluation stops at boolean facts beside raw numbers.
    """
    clocks_list = _parse_schedule(schedule)
    if bars_doc.get("schema") != BARS_SCHEMA:
        raise ValueError("not a forward minute-bars document (schema mismatch)")
    try:
        session = date.fromisoformat(str(bars_doc["session"]))
    except (KeyError, TypeError, ValueError):
        raise ValueError("bars document has no parsable session date") from None
    entries = bars_doc.get("contracts")
    if not isinstance(entries, Mapping) or not entries:
        raise ValueError("bars document has no contracts: nothing to score")
    windows = _windows(session, clocks_list)
    tickers = sorted(entries)
    stamps: dict[str, list[int]] = {}
    for ticker in tickers:
        entry = entries[ticker]
        if not isinstance(entry, Mapping):
            raise ValueError(f"contract entry is not an object: {ticker}")
        ts: list[int] = []
        for bar in entry.get("bars") or []:
            t = bar.get("t") if isinstance(bar, Mapping) else None
            if not isinstance(t, int) or isinstance(t, bool):
                raise ValueError(f"invalid bar timestamp: {ticker}")
            ts.append(t)
        stamps[ticker] = ts
    hits = {
        ticker: [any(lo <= t < hi for t in ts) for _c, lo, hi in windows]
        for ticker, ts in stamps.items()
    }
    total = len(tickers)
    detail: dict[str, dict[str, Any]] = {}
    for ticker in tickers:
        row = hits[ticker]
        covered = [
            clock for (clock, _lo, _hi), hit in zip(windows, row, strict=True) if hit
        ]
        status = str(entries[ticker].get("status", "unknown"))
        ts = stamps[ticker]
        has_gap = len(covered) < len(clocks_list)
        detail[ticker] = {
            "status": status,
            "n_bars": len(ts),
            "covered_clocks": covered,
            "missing_clocks": [c for c in clocks_list if c not in covered],
            "gap_reason": (
                f"capture status: {status}" if has_gap and status != "ok"
                else ("no bar in [clock, clock+15min) ET" if has_gap else "")
            ),
            "first_bar_et": (
                datetime.fromtimestamp(min(ts) / 1000).astimezone(ET).isoformat()
                if ts
                else None
            ),
            "last_bar_et": (
                datetime.fromtimestamp(max(ts) / 1000).astimezone(ET).isoformat()
                if ts
                else None
            ),
        }
    clocks_table: dict[str, dict[str, Any]] = {}
    for index, (clock, _lo, _hi) in enumerate(windows):
        missing_contracts = [t for t in tickers if not hits[t][index]]
        covered_n = total - len(missing_contracts)
        clocks_table[clock] = {
            "covered": covered_n,
            "total": total,
            "pct": round(covered_n / total * 100, 2),
            "missing_contracts": missing_contracts,
        }
    scheduled_points = total * len(windows)
    covered_points = sum(c["covered"] for c in clocks_table.values())
    min_pct = min(c["pct"] for c in clocks_table.values())
    summary = {
        "contracts": total,
        "clocks": len(windows),
        "clocks_fully_covered": sum(
            c["covered"] == c["total"] for c in clocks_table.values()
        ),
        "scheduled_points": scheduled_points,
        "covered_points": covered_points,
        "covered_points_pct": round(covered_points / scheduled_points * 100, 2),
        "min_clock_pct": min_pct,
        "min_clocks": sorted(c for c in clocks_table if clocks_table[c]["pct"] == min_pct),
        "all_clocks_ge_pct": {
            str(int(floor)): all(c["pct"] >= floor for c in clocks_table.values())
            for floor in FACT_FLOORS_PCT
        },
        "threshold_note": (
            "facts only, not a verdict: the qualifying threshold is the "
            "operator's sealed call (RESTART-THRESHOLD), and C3's floor pools "
            "(session, clock) points across the qualifying corpus, not one session"
        ),
    }
    return {
        "session": session.isoformat(),
        "schedule": list(clocks_list),
        "window": {
            "minutes": AGE_WINDOW_SECONDS // 60,
            "edge": "[clock, clock+15min) ET: a bar at exactly C+15:00 is stale",
        },
        "clocks": clocks_table,
        "contracts": detail,
        "summary": summary,
    }


def _load_selection(bars_doc: Mapping[str, Any], root: Path | None) -> dict[str, Any]:
    """The selection behind the bars document: ``selected_on``'s file, else the newest."""
    selected_on = bars_doc.get("selected_on")
    if isinstance(selected_on, str):
        try:
            candidate = selection_path(date.fromisoformat(selected_on), root)
        except ValueError:
            candidate = None
        if candidate is not None and candidate.exists():
            return json.loads(candidate.read_text())
    selection = latest_selection(root)
    if selection is None:
        raise CoverageInputError(
            "no selection in the forward store; run forward-select",
            MISSING_SELECTION_EXIT,
        )
    return selection


def _selection_check(
    selection: Mapping[str, Any], bars_doc: Mapping[str, Any]
) -> dict[str, Any]:
    """Provenance/roster facts (recorded, never enforced)."""
    selected = sorted(str(t) for t in selection.get("tickers", []))
    captured = sorted(bars_doc.get("contracts", {}))
    expected = str(selection.get("source_sha256", ""))
    recorded = str(bars_doc.get("selection_source_sha256", ""))
    return {
        "schema": selection.get("schema"),
        "selected_on": selection.get("selected_on"),
        "provenance": (
            "unknown"
            if not expected or not recorded
            else ("match" if expected == recorded else "mismatch")
        ),
        "contracts": len(selected),
        "missing_from_bars": [t for t in selected if t not in captured],
        "extra_in_bars": [t for t in captured if t not in selected],
    }


def score_session(
    session: date, *, root: Path | None = None, now: datetime | None = None
) -> dict[str, Any]:
    """Read one captured session's bars, score the eight clocks, persist the doc.

    No wire, no judgment: the written document's facts section is the only
    evaluation, and it stops at boolean facts. Raises
    :class:`CoverageInputError` (carrying the CLI exit code) when the
    session has no bars document or the store has no usable selection.
    """
    source = bars_path(session, root)
    if not source.exists():
        raise CoverageInputError(
            "no bars document for the session; run forward-minutes", NO_BARS_EXIT
        )
    raw = source.read_bytes()
    bars_doc = json.loads(raw)
    selection = _load_selection(bars_doc, root)
    if selection.get("schema") != SELECTION_SCHEMA:
        raise CoverageInputError(
            "the forward store's selection is not a selection document",
            MISSING_SELECTION_EXIT,
        )
    doc = {
        "schema": COVERAGE_SCHEMA,
        **clock_coverage(bars_doc),
        "bars_sha256": hashlib.sha256(raw).hexdigest(),
        "selected_on": bars_doc.get("selected_on"),
        "selection_check": _selection_check(selection, bars_doc),
        "checked_at": (now or datetime.now(ET)).isoformat(),
        "execution_authorized": False,
    }
    out = coverage_path(session, root)
    out.parent.mkdir(parents=True, exist_ok=True)
    store.atomic_write_json(out, doc)
    return doc


def render_lines(doc: Mapping[str, Any]) -> list[str]:
    """The human table: raw numbers and boolean facts, no verdicts."""
    check = doc.get("selection_check") or {}
    lines = [
        "forward-coverage: {session} selection {selected_on} "
        "provenance={provenance} contracts={contracts}".format(
            session=doc.get("session"),
            selected_on=check.get("selected_on"),
            provenance=check.get("provenance"),
            contracts=doc["summary"]["contracts"],
        )
    ]
    if check.get("missing_from_bars") or check.get("extra_in_bars"):
        lines.append(
            "  selection roster vs bars: {} selected, missing_from_bars={} "
            "extra_in_bars={}".format(
                check.get("contracts"),
                len(check.get("missing_from_bars", [])),
                len(check.get("extra_in_bars", [])),
            )
        )
    lines.append("clock  covered  pct     missing")
    for clock in doc["schedule"]:
        row = doc["clocks"][clock]
        missing = row["missing_contracts"]
        shown = ",".join(missing[:3]) + (
            f" +{len(missing) - 3} more" if len(missing) > 3 else ""
        )
        lines.append(f"{clock}  {row['covered']}/{row['total']}  {row['pct']:5.1f}%  {shown}".rstrip())
    summary = doc["summary"]
    lines.append(
        "points: {covered}/{scheduled} covered ({pct}%); clocks fully covered "
        "{full}/{clocks}; min clock {mins} at {min_pct}%".format(
            covered=summary["covered_points"],
            scheduled=summary["scheduled_points"],
            pct=summary["covered_points_pct"],
            full=summary["clocks_fully_covered"],
            clocks=summary["clocks"],
            mins=",".join(summary["min_clocks"]) or "-",
            min_pct=summary["min_clock_pct"],
        )
    )
    for floor in sorted(summary["all_clocks_ge_pct"], key=int):
        lines.append(
            f"fact: every clock >= {floor}% covered: "
            f"{'yes' if summary['all_clocks_ge_pct'][floor] else 'no'}"
        )
    lines.append(
        "fact only, not a verdict: the qualifying threshold is the operator's "
        "sealed call (RESTART-THRESHOLD)"
    )
    return lines


__all__ = [
    "AGE_WINDOW_SECONDS",
    "COVERAGE_SCHEMA",
    "EIGHT_CLOCKS",
    "FACT_FLOORS_PCT",
    "MISSING_SELECTION_EXIT",
    "NO_BARS_EXIT",
    "CoverageInputError",
    "clock_coverage",
    "coverage_path",
    "render_lines",
    "score_session",
]
