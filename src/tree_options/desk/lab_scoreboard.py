"""Aggregate the lab's run evidence into a per-policy scoreboard.

Pure and file-driven: reads every ``evaluations/lab/*/summary.json`` the
burn engine wrote and folds it into cumulative per-policy stats. The
advisory this produces is an ANNOTATION for shadow previews — ``promoted``
is always False because no promotion rule is registered; a scoreboard
number alone promotes nothing (see docs/desk/DESK-LAB.md for the draft
rule and the register -> seal -> rule path).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

SCOREBOARD_SCHEMA = "desk-lab-scoreboard/1"
STARTING_CAPITAL = Decimal("5000")
#: a policy needs this many runs before its stats annotate a preview
ADVISORY_MIN_RUNS = 3


@dataclass
class PolicyStats:
    runs: int = 0
    boards: int = 0
    model_calls: int = 0
    model_failures: int = 0
    entered: int = 0
    wins: int = 0
    losses: int = 0
    closed_pnl_sum: Decimal = Decimal(0)
    worst_minimum_capital: Decimal | None = None
    last_run: str = ""
    kinds: set[str] = field(default_factory=set)

    def fold(self, document: dict[str, Any], run_name: str) -> None:
        summary = document.get("summary", {})
        self.runs += 1
        self.boards += int(document.get("boards_shown", 0) or 0)
        self.model_calls += int(document.get("model_calls", 0) or 0)
        self.model_failures += int(document.get("model_failures", 0) or 0)
        self.entered += int(summary.get("entered", 0) or 0)
        self.wins += int(summary.get("modeled_wins", 0) or 0)
        self.losses += int(summary.get("modeled_losses", 0) or 0)
        closed = summary.get("closed_capital_proxy")
        if closed is not None:
            self.closed_pnl_sum += Decimal(str(closed)) - STARTING_CAPITAL
        minimum = summary.get("minimum_closed_capital_proxy")
        if minimum is not None:
            value = Decimal(str(minimum))
            self.worst_minimum_capital = (
                value
                if self.worst_minimum_capital is None
                else min(self.worst_minimum_capital, value)
            )
        self.last_run = max(self.last_run, run_name)
        self.kinds.add("model" if str(document.get("policy", "")).startswith("model:") else "rules")

    def as_dict(self) -> dict[str, Any]:
        return {
            "runs": self.runs,
            "boards": self.boards,
            "model_calls": self.model_calls,
            "model_failures": self.model_failures,
            "entered": self.entered,
            "modeled_wins": self.wins,
            "modeled_losses": self.losses,
            "closed_pnl_sum": str(self.closed_pnl_sum),
            "worst_minimum_capital": (
                None if self.worst_minimum_capital is None else str(self.worst_minimum_capital)
            ),
            "last_run": self.last_run,
            "kinds": sorted(self.kinds),
        }


def aggregate(lab_root: Path) -> dict[str, Any]:
    """Fold every run summary under ``lab_root`` into per-policy stats."""
    stats: dict[str, PolicyStats] = {}
    if not lab_root.is_dir():
        return {"schema": SCOREBOARD_SCHEMA, "policies": {}}
    for summary_path in sorted(lab_root.glob("*/summary.json")):
        try:
            document = json.loads(summary_path.read_bytes())
        except (OSError, ValueError):
            continue  # a torn or foreign file never poisons the scoreboard
        policy = str(document.get("policy", ""))
        if not policy:
            continue
        stats.setdefault(policy, PolicyStats()).fold(document, summary_path.parent.name)
    return {
        "schema": SCOREBOARD_SCHEMA,
        "policies": {name: s.as_dict() for name, s in sorted(stats.items())},
    }


def best_advisory(scoreboard: dict[str, Any]) -> dict[str, Any] | None:
    """The strongest policy's stats, annotated, NEVER promoted.

    Qualifies only with ``ADVISORY_MIN_RUNS`` runs; ``promoted`` is False
    by construction — promotion is an operator ruling on a pre-registered
    rule, never a scoreboard outcome."""
    best: tuple[Decimal, str, dict[str, Any]] | None = None
    for name, stats in scoreboard.get("policies", {}).items():
        if int(stats.get("runs", 0)) < ADVISORY_MIN_RUNS:
            continue
        pnl = Decimal(str(stats.get("closed_pnl_sum", "0")))
        if best is None or pnl > best[0]:
            best = (pnl, name, stats)
    if best is None:
        return None
    return {
        "policy": best[1],
        "stats": best[2],
        "promoted": False,
        "basis": f"highest summed closed-pnl proxy over >= {ADVISORY_MIN_RUNS} runs",
    }
