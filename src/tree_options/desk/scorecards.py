"""Per-family scorecards over resolved shadow episodes (plan D7).

Pure aggregation over the episode documents
(:mod:`tree_options.desk.shadows`): no deal is ever added or changed here.
A family is a playbook row (plus its tier); the ``input`` view splits the
same episodes by the decision basis the queue recorded (signal rows carry
their signal, textbook rows say so).

Rules (the plan's wording, implemented verbatim):

* **promotion** needs at least ``MIN_RESOLVED`` resolved episodes;
* **retire** when the first ``MIN_RESOLVED`` resolved episodes (by entry
  session) average <= 0 dollars. The plan's other retire trigger ("dies
  under stress fills") has no numeric definition; stress-fill EVs are
  recorded on the card (``mean_stress_ev_dollars``) and stay an operator
  judgment, never an automatic rule;
* **pause** when the trailing ``TRAILING_WEEKS`` week-cluster mean is
  <= 0 AND the cumulative resolved P&L is <= ``PAUSE_CUMULATIVE_DOLLARS``.

Weekly t-statistics cluster resolved episodes by the ISO week of their
entry session (the dedupe unit), so within-week noise does not inflate
significance. All dollar figures come from the episodes' strings and stay
strings in the output documents.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from tree_options.desk import shadows
from tree_options.desk.store import atomic_write_json

SCHEMA = "desk-scorecards/1"
MIN_RESOLVED = 20
TRAILING_WEEKS = 6
PAUSE_CUMULATIVE_DOLLARS = Decimal(-1000)


@dataclass(frozen=True)
class EpisodeView:
    """The slice of a resolved episode the scorecards aggregate."""

    family: str
    tier: str
    input_basis: str
    entry_session: str
    week: str
    pnl: Decimal
    decision_ev: Decimal | None
    slippage: Decimal | None
    stress_ev: Decimal | None
    fallback: bool


def episode_view(ep: shadows.Episode) -> EpisodeView | None:
    res = ep.resolved
    if res is None:
        return None
    try:
        pnl = Decimal(str(res["pnl_dollars"]))
    except (KeyError, ArithmeticError):
        return None
    dec = ep.decision or {}

    def opt(key: str) -> Decimal | None:
        v = dec.get(key) or res.get(key)
        if v is None:
            return None
        try:
            return Decimal(str(v))
        except ArithmeticError:
            return None

    basis = str(dec.get("basis") or "unknown")
    return EpisodeView(
        family=ep.row,
        tier=ep.tier,
        input_basis=basis,
        entry_session=ep.entry_session.isoformat(),
        week=shadows.week_key(ep.entry_session),
        pnl=pnl,
        decision_ev=opt("ev"),
        slippage=opt("slippage_first_mark_dollars"),
        stress_ev=opt("ev_stress_fill"),
        fallback=bool(res.get("fallback")),
    )


def _mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _weekly_t(weeks: Mapping[str, list[float]]) -> float | None:
    """mean of week means over their standard error, None under 2 weeks."""
    means = [_mean(v) for v in weeks.values()]
    if len(means) < 2:
        return None
    m = _mean(means)
    var = sum((x - m) ** 2 for x in means) / (len(means) - 1)
    se = math.sqrt(var / len(means))
    return m / se if se > 0 else None


def _s(x: Decimal | None) -> str | None:
    return None if x is None else str(x)


def family_card(views: Sequence[EpisodeView]) -> dict[str, Any]:
    """The scorecard of one family (or input slice) from its episodes."""
    resolved = sorted(views, key=lambda v: v.entry_session)
    pnls = [float(v.pnl) for v in resolved]
    weeks: dict[str, list[float]] = {}
    for v in resolved:
        weeks.setdefault(v.week, []).append(float(v.pnl))
    week_keys = sorted(weeks)
    trailing = [w for w in week_keys[-TRAILING_WEEKS:]]
    trailing_mean = _mean([x for w in trailing for x in weeks[w]])
    cumulative = sum((v.pnl for v in resolved), Decimal(0))
    first = [float(v.pnl) for v in resolved[:MIN_RESOLVED]]
    evs = [(float(v.decision_ev), float(v.pnl)) for v in resolved if v.decision_ev is not None]
    slips = [float(v.slippage) for v in resolved if v.slippage is not None]
    stress = [float(v.stress_ev) for v in resolved if v.stress_ev is not None]
    n = len(resolved)
    enough = n >= MIN_RESOLVED
    retire = enough and _mean(first) <= 0.0
    pause = (
        n > 0
        and trailing_mean <= 0.0
        and cumulative <= PAUSE_CUMULATIVE_DOLLARS
    )
    return {
        "schema": SCHEMA,
        "n_resolved": n,
        "rules": {
            "promotion_ready": enough and not retire and not pause,
            "min_resolved": MIN_RESOLVED,
            "retire": retire,
            "pause": pause,
            "trailing_weeks": TRAILING_WEEKS,
            "pause_cumulative_dollars": str(PAUSE_CUMULATIVE_DOLLARS),
        },
        "pnl": {
            "total_dollars": _s(cumulative),
            "mean_dollars": _s(Decimal(repr(_mean(pnls))) if pnls else None),
            "win_rate": (sum(1 for p in pnls if p > 0) / n) if n else None,
            "weekly_t_stat": _weekly_t(weeks),
            "n_weeks": len(weeks),
            "fallback_resolutions": sum(1 for v in resolved if v.fallback),
        },
        "prediction": {
            "n_with_ev": len(evs),
            "mean_decision_ev_dollars": (
                _s(Decimal(repr(_mean([e for e, _ in evs])))) if evs else None
            ),
            "mean_realized_minus_ev_dollars": (
                _s(Decimal(repr(_mean([p - e for e, p in evs])))) if evs else None
            ),
            "mean_slippage_first_mark_dollars": (
                _s(Decimal(repr(_mean(slips)))) if slips else None
            ),
            "n_with_stress_ev": len(stress),
            "mean_stress_ev_dollars": (
                _s(Decimal(repr(_mean(stress)))) if stress else None
            ),
        },
    }


def build_scorecards(state: Path) -> dict[str, Any]:
    """Every family's card plus the per-input slices, from the shadows dir."""
    episodes = shadows.load_episodes(state)
    views = [v for ep in episodes if (v := episode_view(ep)) is not None]
    families: dict[str, list[EpisodeView]] = {}
    inputs: dict[str, list[EpisodeView]] = {}
    for v in views:
        families.setdefault(v.family, []).append(v)
        inputs.setdefault(v.input_basis, []).append(v)
    cards = {f"row:{k}": family_card(v) for k, v in sorted(families.items())}
    by_input = {f"input:{k}": family_card(v) for k, v in sorted(inputs.items())}
    return {
        "schema": SCHEMA,
        "episodes_total": len(episodes),
        "resolved_total": len(views),
        "families": cards,
        "inputs": by_input,
    }


def write_scorecards(state: Path, out_dir: Path | None = None) -> dict[str, Any]:
    """Build the scorecards and write them under ``<state>/scorecards/``."""
    doc = build_scorecards(state)
    out = out_dir or (state / "scorecards")
    out.mkdir(parents=True, exist_ok=True)
    for key, card in {**doc["families"], **doc["inputs"]}.items():
        safe = key.replace("/", "_")
        atomic_write_json(out / f"{safe}.json", card)
    atomic_write_json(out / "summary.json", doc)
    return doc


def load_summary(out_dir: Path) -> dict[str, Any]:
    return json.loads((out_dir / "summary.json").read_text())
