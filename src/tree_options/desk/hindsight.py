"""Hindsight-optimal gap analysis over the recorded boards (pure mechanics).

For every as-of board the lab shows a policy, this module computes what EACH
candidate on that board would have produced later under the SAME valuation
semantics ``intraday_action_graph.replay`` gives a CHOSEN trade: the entry is
filled from the first later bar of each leg (``iag._next``), the exit is the
first later snapshot where both legs have fresh marks (``iag._latest``), and
the PnL is clamped to the entry-time structure bounds. The math is iag's own
primitives and formulas — nothing here re-derives prices.

Semantics note: a candidate's outcome is the ISOLATED counterfactual of
entering exactly that candidate on exactly that board (the accounting state a
single-decision replay starts from: capital 5000, nothing reserved, no prior
losses). The cross-check oracle in the tests pins this to ``iag.replay``:
replaying ``{snapshot: candidate_id}`` must reproduce the claimed outcome
exactly.

Candidates and boards with no evaluable outcome (no later entry bars, an
entry the risk caps would refuse, or a position still open at window end) are
ABSENT from the results — never zero, never fabricated.

LLMs never touch this module: it measures; the GEPA lane (``desk.gepa``)
reads its output as untrusted-but-mechanical evidence.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from tree_options.desk import intraday_action_graph as iag
from tree_options.desk import lab

HINDSIGHT_SCHEMA = "desk-hindsight-gap/1"
#: latest_sessions with an unreachable count means "every session in the bundle"
_ALL_SESSIONS = 1_000_000

_Bars = dict[str, list[tuple[datetime, Decimal]]]


def all_sessions(raw: Mapping[str, Any]) -> list[date]:
    """Every session day the bundle's minute bars cover (sorted)."""
    return lab.latest_sessions(dict(raw), _ALL_SESSIONS)


def parse_bundle(raw: Mapping[str, Any]) -> tuple[_Bars, dict[str, iag.Contract]]:
    """The bundle's verified bars and parsed contracts (iag's own reader),
    carrying the bundle's candidate rules (``iag.ContractUniverse``)."""
    bars = iag._read_bars(raw)
    contracts = iag.bundle_contracts(raw, bars)
    return bars, contracts


def _candidate_outcome(bars: _Bars, sessions: list[date], day: date, clock: str,
                       candidate: dict[str, Any], age_s: int) -> Decimal | None:
    """The later-modeled PnL of entering ``candidate`` on this board, with
    replay's exact entry/exit valuation; None when replay would produce no
    closed trade (no fill, a refused entry, or open at window end)."""
    now = iag._instant(day, clock)
    long_fill = iag._next(bars[candidate["long"]], now, age_s)
    short_fill = iag._next(bars[candidate["short"]], now, age_s)
    if long_fill is None or short_fill is None:
        return None  # replay: missing_later_entry_bars
    entry_debit = long_fill - short_fill
    width = Decimal(candidate["width"])
    debit = candidate["structure"].endswith("debit")
    max_loss = (entry_debit if debit else width + entry_debit) * 100
    max_gain = ((width - entry_debit) if debit else -entry_debit) * 100
    # the isolated counterfactual starts like a fresh single-decision replay:
    # nothing reserved, the full 5000 closed, no prior same-day loss
    reserved = Decimal(0)
    closed_capital = Decimal(5000)
    if max_loss <= 0 or max_loss > 300 or max_gain <= 0 or reserved + max_loss > closed_capital:
        return None  # replay: entry_risk_cap
    if reserved + max_loss > 1500:
        return None  # replay: combined_open_loss_cap
    # exit walk: the scheduled snapshots strictly after this board, in the
    # same order replay iterates them; the first one where both legs have
    # fresh marks closes the trade at the clamped spread mark
    for later_day in sessions:
        if later_day < day:
            continue
        for later_clock in iag.schedule_for(later_day):
            if later_day == day and later_clock <= clock:
                continue
            mark_at = iag._instant(later_day, later_clock)
            long_mark = iag._latest(bars[candidate["long"]], mark_at, age_s)
            short_mark = iag._latest(bars[candidate["short"]], mark_at, age_s)
            if long_mark is not None and short_mark is not None:
                pnl = ((long_mark - short_mark) - entry_debit) * 100
                return min(max_gain, max(-max_loss, pnl))
    return None  # open at window end: no final PnL, so no outcome


def board_outcomes(raw: Mapping[str, Any], day: date, clock: str, *,
                   sessions: list[date] | None = None,
                   max_age_minutes: int = 15) -> dict[str, Decimal]:
    """Per-candidate later-modeled PnL of entering that candidate on this
    board. Boards/candidates with no evaluable outcome are absent."""
    if max_age_minutes < 1 or max_age_minutes > 30:
        raise ValueError("invalid freshness limit")
    if clock not in iag.schedule_for(day):
        raise ValueError("clock is not a scheduled decision point")
    window = ([d for d in all_sessions(raw) if d >= day]
              if sessions is None else list(sessions))
    if day not in window:
        raise ValueError("board day is outside the window")
    if window != sorted(set(window)):
        raise ValueError("sessions must be sorted and unique")
    bars, contracts = parse_bundle(raw)
    now = iag._instant(day, clock)
    candidates = iag._candidates(contracts, bars, now, max_age_minutes * 60)
    return {
        candidate["id"]: pnl
        for candidate in candidates
        if (pnl := _candidate_outcome(bars, window, day, clock, candidate,
                                      max_age_minutes * 60)) is not None
    }


def _row_map(candidates: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """The aliased board rows (lab.board_rows fields) keyed by candidate id."""
    return {row["id"]: row for row in lab.board_rows({"candidates": candidates})}


def gap_report(run_document: Mapping[str, Any], raw: Mapping[str, Any], *,
               max_age_minutes: int = 15) -> dict[str, Any]:
    """Per board the run actually showed (boards with receipts): the chosen
    id or skip, the chosen outcome, the hindsight best (argmax of
    board_outcomes on that board) and the gap, with the aliased feature rows
    of chosen-vs-best. Boards with no evaluable outcome are excluded, not
    zeroed."""
    sessions = [date.fromisoformat(str(d)) for d in run_document["sessions"]]
    bars, contracts = parse_bundle(raw)
    age_s = max_age_minutes * 60
    boards: list[dict[str, Any]] = []
    for receipt in run_document.get("receipts", []):
        snapshot = str(receipt["snapshot"])
        body = snapshot[2:] if snapshot.startswith("s:") else snapshot
        day_text, _, clock = body.rpartition("T")
        day = date.fromisoformat(day_text)
        if day not in sessions:
            continue
        candidates = iag._candidates(contracts, bars, iag._instant(day, clock), age_s)
        outcomes = {
            candidate["id"]: pnl
            for candidate in candidates
            if (pnl := _candidate_outcome(bars, sessions, day, clock,
                                          candidate, age_s)) is not None
        }
        if not outcomes:
            continue  # a no-outcome board is excluded, never zeroed
        rows = _row_map(candidates)
        chosen = receipt.get("choice")
        chosen_outcome = outcomes.get(str(chosen)) if chosen is not None else None
        best = max(sorted(outcomes), key=lambda cid: outcomes[cid])
        best_outcome = outcomes[best]
        realized = chosen_outcome if chosen_outcome is not None else Decimal(0)
        boards.append({
            "snapshot": snapshot, "session": day.isoformat(), "clock_et": clock,
            "board_rows": len(rows),
            "chosen": chosen,
            "chosen_outcome": (None if chosen_outcome is None else str(chosen_outcome)),
            "best": best, "best_outcome": str(best_outcome),
            "best_in_shown_rows": best in rows,
            "gap": str(best_outcome - realized),
            "chosen_row": rows.get(str(chosen)) if chosen is not None else None,
            "best_row": rows.get(best),
        })
    return {
        "schema": HINDSIGHT_SCHEMA,
        "policy": run_document.get("policy"),
        "sessions": [str(d) for d in sessions],
        "boards": boards,
        "totals": {
            "boards": len(boards),
            "entered_choices": sum(1 for b in boards if b["chosen"] is not None),
            "gap_sum": str(sum((Decimal(b["gap"]) for b in boards), Decimal(0))),
        },
    }


def top_gaps(report: Mapping[str, Any], limit: int = 12) -> list[dict[str, Any]]:
    """The highest-gap boards first (deterministic), bounded for prompts."""
    ranked = sorted(report.get("boards", []),
                    key=lambda b: (-Decimal(str(b["gap"])), str(b["snapshot"])))
    return [
        {"snapshot": b["snapshot"], "clock_et": b["clock_et"],
         "chosen": b["chosen"], "chosen_outcome": b["chosen_outcome"],
         "best": b["best"], "best_outcome": b["best_outcome"],
         "gap": b["gap"], "chosen_row": b["chosen_row"], "best_row": b["best_row"]}
        for b in ranked[:limit]
    ]
