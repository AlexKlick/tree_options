"""Lane A of the TREX agent trading desk: the end-to-end challenge game.

An automated challenge over EVERY frozen historical minute-bar bundle in the
store: the same three session thirds for every policy, the same mechanical
replay accounting (``intraday_action_graph.replay`` through ``lab.run_lab``)
for every score, hindsight gap samples taken from each policy's OWN receipts,
and one honest digest. The challenge only measures — it proposes nothing and
evolves nothing (the GEPA lane evolves prompts; this lane scores the field it
finds, archive front plus the no_trade control).

The law (unchanged from the lab): LLMs only choose on boards. Every number in
the digest is mechanical — replay summaries and pure functions over them. A
model-claimed figure is never a score. Nothing is promoted; promotion stays
the operator's pre-registered-rule path (docs/desk/DESK-LAB.md).

Budget: HARD CAPS of <= 200 board calls and <= 8 reflection calls per ROUND
(one bundle) and <= 1000 board calls per challenge, enforced by a counter
that refuses (raises) before a grant can exceed them. The quota gate skips
ONLY model policies, and only when a FRESH (< 6 h) windows snapshot shows no
under-using window (reason ``quota_dry``); rules policies always run, they
cost nothing. A stale or missing snapshot means the standing conservative
budget — the caps bound that path too.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from tree_options.desk import gepa, hindsight, lab, paths
from tree_options.desk import intraday_action_graph as iag
from tree_options.desk.lab_scoreboard import STARTING_CAPITAL
from tree_options.trex.grant_policy import QuotaWindow, load_windows

CHALLENGE_SCHEMA = "desk-challenge/1"
#: where the frozen vintages live, relative to the desk store
BUNDLE_REL = ("evaluations", "intraday-graph")
#: the session partition: three disjoint, reproducible thirds
PARTS = 3
DEFAULT_SEED = 7
#: archive champions fielded beside the control (the pareto front's head)
FRONT_MAX = 3
HARD_ROUND_BOARDS = 200
HARD_ROUND_REFLECTIONS = 8
HARD_CHALLENGE_BOARDS = 1000
FRESH_WINDOW_S = 6 * 3600
QUOTA_DRY = "quota_dry"
UNTRUSTED_NOTE = (
    "Model output is untrusted prose. Outcomes are mechanical proxies from "
    "replay accounting on last-traded-minute closes, not executable fills. "
    "Nothing in this digest is promoted: promotion is the operator's "
    "pre-registered-rule path (docs/desk/PROMOTION-RULE.md)."
)
PROMOTION_RULE = (
    "nothing is promoted by a digest; the REGISTERED rule "
    "(docs/desk/PROMOTION-RULE.md, sealed 2026-10-01) decides — clauses "
    "evaluated by `challenge rule-check` against the standings, ruled on "
    "by the operator"
)


# ----------------------------------------------------------------- budget


class BudgetRefused(RuntimeError):
    """The challenge plan would exceed a hard cap; nothing was burned."""


@dataclass(frozen=True)
class ChallengeBudget:
    """The challenge's call budget; the hard caps are not configurable around."""

    boards: int = HARD_ROUND_BOARDS
    reflections: int = HARD_ROUND_REFLECTIONS
    total_boards: int = HARD_CHALLENGE_BOARDS

    def __post_init__(self) -> None:
        if not 1 <= self.boards <= HARD_ROUND_BOARDS:
            raise ValueError(f"round boards must be within 1..{HARD_ROUND_BOARDS}")
        if not 1 <= self.reflections <= HARD_ROUND_REFLECTIONS:
            raise ValueError(f"round reflections must be within 1..{HARD_ROUND_REFLECTIONS}")
        if not 1 <= self.total_boards <= HARD_CHALLENGE_BOARDS:
            raise ValueError(f"challenge boards must be within 1..{HARD_CHALLENGE_BOARDS}")


class BudgetCounter:
    """Reserves calls per round and for the whole challenge; refuses (raises)
    BEFORE a grant can exceed either cap. Pre-reservations a run did not use
    are refunded, so the ledger tracks the burn that actually happened."""

    def __init__(self, budget: ChallengeBudget) -> None:
        self._budget = budget
        self._round_boards = budget.boards
        self._round_reflections = budget.reflections
        self._total_boards = budget.total_boards
        self.rounds = 0

    @property
    def boards_left(self) -> int:
        return self._round_boards

    @property
    def reflections_left(self) -> int:
        return self._round_reflections

    @property
    def total_boards_left(self) -> int:
        return self._total_boards

    @property
    def boards_used(self) -> int:
        return self._budget.total_boards - self._total_boards

    def start_round(self) -> None:
        """A new bundle: the per-round caps reset, the challenge total does not."""
        self._round_boards = self._budget.boards
        self._round_reflections = self._budget.reflections
        self.rounds += 1

    def take_boards(self, count: int) -> None:
        if count < 0:
            raise ValueError("count must be >= 0")
        if count > self._round_boards or count > self._total_boards:
            raise BudgetRefused(
                f"board calls {count} exceed the remaining cap "
                f"(round {self._round_boards}, challenge {self._total_boards})"
            )
        self._round_boards -= count
        self._total_boards -= count

    def take_reflections(self, count: int) -> None:
        if count < 0:
            raise ValueError("count must be >= 0")
        if count > self._round_reflections:
            raise BudgetRefused(
                f"reflection calls {count} exceed the remaining cap {self._round_reflections}"
            )
        self._round_reflections -= count

    def refund_boards(self, count: int) -> None:
        """Return pre-reserved boards a run did not show (never above a cap)."""
        if count < 0:
            raise ValueError("count must be >= 0")
        self._round_boards = min(self._budget.boards, self._round_boards + count)
        self._total_boards = min(self._budget.total_boards, self._total_boards + count)


# ------------------------------------------------------------- the sweep


def challenge_root(store_root: Path) -> Path:
    return Path(store_root) / "evaluations" / "challenge"


def discover_bundles(store_root: Path) -> list[Path]:
    """The frozen minute-bar vintage to challenge per directory: the LARGEST
    ``minute-bars*.json`` file under each ``evaluations/intraday-graph/<vintage>/``
    (size desc, name asc tiebreak). The first real run (20260928T224926Z)
    picked the 14 KB ``minute-bars-probe.json`` by alphabetical-last choice
    and scored 0 boards while the 69 MB bundle sat beside it; probe/cache
    shards are smaller than the real vintage by construction, so largest-
    per-vintage excludes them without a name denylist."""
    root = Path(store_root).joinpath(*BUNDLE_REL)
    if not root.is_dir():
        return []
    found: dict[Path, Path] = {}
    for path in root.glob("*/minute-bars*.json"):
        if not path.is_file():
            continue
        vintage = path.parent
        best = found.get(vintage)
        key = (path.stat().st_size, path.name)
        if best is None or key > (best.stat().st_size, best.name):
            found[vintage] = path
    return [found[vintage] for vintage in sorted(found)]


def partition_sessions(raw: Mapping[str, Any], *, seed: int = DEFAULT_SEED) -> list[list[date]]:
    """The bundle's sessions split into three disjoint thirds.

    Rule (documented, no RNG): sessions sorted ascending; the session at
    index ``j`` belongs to slice ``(j + seed) % PARTS``. Round-robin by index
    rotated by the fixed seed, so every slice spans the whole bundle and the
    split is byte-identical across runs. The lists are disjoint, each in
    ascending order, and together cover every session exactly once.
    """
    turn = seed % PARTS
    slices: list[list[date]] = [[] for _ in range(PARTS)]
    for index, day in enumerate(hindsight.all_sessions(raw)):
        slices[(index + turn) % PARTS].append(day)
    return slices


@dataclass(frozen=True)
class PolicyEntry:
    """One policy in the challenge field."""

    policy: str
    kind: str  # "rules" | "model"
    prompt: str | None = None  # the evolved policy sentence (gepa:<id> only)
    skipped_reason: str | None = None


def policy_field(lab_root: Path) -> list[PolicyEntry]:
    """The field every bundle scores: the honest ``no_trade`` control and the
    ``first_row`` trivial-picker control ALWAYS (the paired bars the
    registered promotion rule needs), plus the archive's pareto front (at
    most ``FRONT_MAX``) as model policies ``gepa:<id>``; an empty archive
    fields the incumbent flash policy ``model:zai`` instead, so the
    challenge always has one model policy."""
    front = gepa.pareto_front(gepa.load_archive(Path(lab_root)))[:FRONT_MAX]
    entries = [
        PolicyEntry(policy="no_trade", kind="rules"),
        PolicyEntry(policy=lab.FIRST_ROW_POLICY, kind="rules"),
    ]
    for record in front:
        entries.append(
            PolicyEntry(
                policy=f"{lab.GEPA_PREFIX}{record['id']}",
                kind="model",
                prompt=str(record["prompt"]),
            )
        )
    if not front:
        entries.append(PolicyEntry(policy="model:zai", kind="model"))
    return entries


def _slice_bundle(raw: Mapping[str, Any], days: Sequence[date]) -> dict[str, Any]:
    """The bundle restricted to one slice's sessions (each contract keeps only
    the bars stamped on the slice's UTC days), so ``lab.run_lab`` scores
    exactly that slice through the unchanged mechanical pipeline."""
    keep = {day.isoformat() for day in days}
    contracts: dict[str, Any] = {}
    for ticker, body in raw.get("contracts", {}).items():
        bars = [
            bar
            for bar in body.get("results", [])
            if datetime.fromtimestamp(bar["t"] / 1000, UTC).date().isoformat() in keep
        ]
        contracts[ticker] = {**body, "results": bars}
    return {**raw, "contracts": contracts}


def _select_bundles(bundles: list[Path], rounds: int | None) -> list[Path]:
    """The challenge's bundles: the newest ``rounds`` vintages (discovery is
    ascending), played in chronological order; all of them by default."""
    if rounds is not None and rounds < 1:
        raise ValueError("rounds must be >= 1")
    return list(bundles) if rounds is None else bundles[-rounds:]


def _boards_per_run(budget: ChallengeBudget, rounds: int, model_policies: int) -> int:
    """The board cap per (model policy, slice): the per-round allowance — the
    round cap, or an even share of the challenge total — divided across the
    model field's three slices, so the whole challenge stays inside both the
    round cap and the challenge total. Rules policies ignore the cap but a
    LabConfig needs >= 1."""
    if model_policies <= 0:
        return 1
    allowance = min(budget.boards, budget.total_boards // max(1, rounds))
    per_run = allowance // (PARTS * model_policies)
    if per_run < 1:
        raise BudgetRefused(
            f"the plan cannot fit even one board per model run: allowance "
            f"{allowance} over {PARTS} slices x {model_policies} model policies"
        )
    return per_run


# ------------------------------------------------------------- scorecards


def _closed_pnl(document: Mapping[str, Any]) -> Decimal | None:
    """One run's closed PnL, read from the MECHANICAL replay summary only.
    A model-claimed figure is never a score: models supply choices, and the
    numbers come from replay accounting."""
    closed = document.get("summary", {}).get("closed_capital_proxy")
    return None if closed is None else Decimal(str(closed)) - STARTING_CAPITAL


def scorecard(entry: PolicyEntry, runs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """One policy's bundle scorecard: the same mechanical fields the lab
    scoreboard folds, summed over the policy's slice runs. Every input is a
    replay-accounting output; nothing an LLM wrote is a number here."""
    entered = wins = losses = boards = calls = failures = 0
    pnl = Decimal(0)
    minimum: Decimal | None = None
    peak: Decimal | None = None
    run_dirs: list[str] = []
    for document in runs:
        summary = document.get("summary", {})
        entered += int(summary.get("entered", 0) or 0)
        wins += int(summary.get("modeled_wins", 0) or 0)
        losses += int(summary.get("modeled_losses", 0) or 0)
        boards += int(document.get("boards_shown", 0) or 0)
        calls += int(document.get("model_calls", 0) or 0)
        failures += int(document.get("model_failures", 0) or 0)
        closed = _closed_pnl(document)
        if closed is not None:
            pnl += closed
        low = summary.get("minimum_closed_capital_proxy")
        if low is not None:
            value = Decimal(str(low))
            minimum = value if minimum is None else min(minimum, value)
        high = summary.get("peak_open_loss_reserved")
        if high is not None:
            value = Decimal(str(high))
            peak = value if peak is None else max(peak, value)
        run_dirs.append(Path(str(document.get("run_dir", ""))).name)
    return {
        "policy": entry.policy,
        "kind": entry.kind,
        "runs": len(run_dirs),
        "boards": boards,
        "model_calls": calls,
        "model_failures": failures,
        "entered": entered,
        "modeled_wins": wins,
        "modeled_losses": losses,
        "closed_pnl_sum": str(pnl),
        "worst_minimum_capital": None if minimum is None else str(minimum),
        "peak_open_loss_reserved": None if peak is None else str(peak),
        "run_dirs": run_dirs,
    }


def _ranked(cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Highest summed mechanical closed-pnl first; ties break by policy id."""
    return sorted(
        cards, key=lambda card: (-Decimal(str(card["closed_pnl_sum"])), str(card["policy"]))
    )


def _session_series(runs: Sequence[Mapping[str, Any]]) -> dict[str, float]:
    """One policy's per-session closed PnL, keyed by session date: the
    pairing unit for the promotion rule's paired bars. Later runs (same
    session replayed again) keep the LAST observation."""
    series: dict[str, float] = {}
    for document in runs:
        for row in (document.get("summary", {}) or {}).get("by_session", []) or []:
            series[str(row["session"])] = float(Decimal(str(row["closed_pnl"])))
    return series


# ------------------------------------------------------------- standings


#: the seal-time digest set (PROMOTION-RULE clause 1): digests at or before
#: this id are the registration sample and NEVER count toward promotion
REGISTRATION_SAMPLE_THROUGH = "20261001T162152Z"
#: measured round-trip cost of one game (2026-09-30 measured-cost lane)
COST_BASELINE_PER_GAME = 14.60
STANDINGS_SCHEMA = "desk-challenge-standings/1"


def accumulate_standings(store_root: Path) -> dict[str, Any]:
    """Rebuild (never append — digests are immutable evidence) the
    cross-digest standings every clause of the registered rule reads:
    per-policy totals plus the per-session paired series recomputed over
    every post-seal digest's run summaries."""
    base = Path(store_root) / "evaluations" / "challenge"
    rows: dict[str, dict[str, Any]] = {}
    counted = 0
    digests = sorted(base.glob("*/digest.json")) if base.is_dir() else []
    for path in digests:
        digest_id = path.parent.name
        if digest_id <= REGISTRATION_SAMPLE_THROUGH:
            continue  # clause 1: never overlap the registration sample
        try:
            doc = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if doc.get("status") != "ok":
            continue  # only executed games accumulate
        counted += 1
        for summary_path in sorted((path.parent / "runs").glob("*/summary.json")):
            try:
                run = json.loads(summary_path.read_text())
            except (OSError, ValueError):
                continue
            policy = str(run.get("policy"))
            if not policy or run.get("status") != "ok":
                continue
            row = rows.setdefault(
                policy,
                {
                    "policy": policy,
                    "games": 0,
                    "boards": 0,
                    "entered": 0,
                    "closed_pnl_sum": Decimal(0),
                    "worst_minimum_capital": None,
                    "model_calls": 0,
                    "model_failures": 0,
                    "session_dates": set(),
                    "session_pnl": {},
                    "digest_ids": set(),
                },
            )
            s = run.get("summary", {}) or {}
            row["digest_ids"].add(digest_id)
            row["boards"] += int(run.get("boards_shown", 0) or 0)
            row["entered"] += int(s.get("entered", 0) or 0)
            closed = s.get("closed_capital_proxy")
            if closed is not None:
                row["closed_pnl_sum"] += Decimal(str(closed)) - STARTING_CAPITAL
            low = s.get("minimum_closed_capital_proxy")
            if low is not None:
                value = Decimal(str(low))
                row["worst_minimum_capital"] = (
                    value
                    if row["worst_minimum_capital"] is None
                    else min(Decimal(str(row["worst_minimum_capital"])), value)
                )
            row["model_calls"] += int(run.get("model_calls", 0) or 0)
            row["model_failures"] += int(run.get("model_failures", 0) or 0)
            for entry in s.get("by_session", []) or []:
                key = f"{digest_id}:{entry['session']}"
                row["session_pnl"][key] = float(Decimal(str(entry["closed_pnl"])))
                row["session_dates"].add(str(entry["session"]))
    out_rows = []
    for policy in sorted(rows):
        row = rows[policy]
        out_rows.append(
            {
                "policy": policy,
                "kind": "rules" if policy in ("no_trade", lab.FIRST_ROW_POLICY) else "model",
                "games": len(row["digest_ids"]),
                "boards": row["boards"],
                "entered": row["entered"],
                "closed_pnl_sum": str(row["closed_pnl_sum"]),
                "worst_minimum_capital": None
                if row["worst_minimum_capital"] is None
                else str(row["worst_minimum_capital"]),
                "model_calls": row["model_calls"],
                "model_failures": row["model_failures"],
                "sessions_distinct": len(row["session_dates"]),
                "session_pnl": dict(sorted(row["session_pnl"].items())),
            }
        )
    return {
        "schema": STANDINGS_SCHEMA,
        "registration_sample_through": REGISTRATION_SAMPLE_THROUGH,
        "games_counted": counted,
        "cost_baseline_per_game": COST_BASELINE_PER_GAME,
        "policies": out_rows,
        "untrusted_note": UNTRUSTED_NOTE,
    }


def rule_check(standings: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The registered PROMOTION-RULE clauses evaluated per model policy
    against the standings. Informational only: promotion is the operator's
    act; this prints pass/fail, it promotes nothing."""
    from tree_options.desk.longrun import holm, paired

    rows = {r["policy"]: r for r in standings.get("policies", [])}
    checks: list[dict[str, Any]] = []
    p_values: dict[str, float] = {}
    for policy, row in sorted(rows.items()):
        if policy in ("no_trade", lab.FIRST_ROW_POLICY):
            continue  # the controls are the bars, not the candidates
        base = rows.get("no_trade")
        first = rows.get(lab.FIRST_ROW_POLICY)
        own = row.get("session_pnl") or {}
        clauses: list[dict[str, Any]] = []
        sample_ok = row["boards"] >= 500 and row["sessions_distinct"] >= 20
        clauses.append(
            {
                "clause": 1,
                "name": "sample >=500 boards / >=20 sessions, post-seal",
                "pass": sample_ok,
                "detail": f"boards {row['boards']}, sessions {row['sessions_distinct']}",
            }
        )
        beats = base is not None and Decimal(row["closed_pnl_sum"]) > Decimal(
            base["closed_pnl_sum"]
        )
        clauses.append(
            {
                "clause": 2,
                "name": "closed pnl above no_trade",
                "pass": beats,
                "detail": f"{row['closed_pnl_sum']} vs {base['closed_pnl_sum'] if base else 'n/a'}",
            }
        )
        paired_vs_null = None
        if base:
            shared = sorted(set(own) & set(base.get("session_pnl") or {}))
            if len(shared) >= 2:
                paired_vs_null = paired(
                    [own[k] for k in shared],
                    [base["session_pnl"][k] for k in shared],
                    draws=2000,
                    seed=7,
                )
                p_values[policy] = paired_vs_null["p_one_sided"]
        clauses.append(
            {
                "clause": 3,
                "name": "paired CI95 > 0 vs no_trade",
                "pass": bool(paired_vs_null and paired_vs_null["ci95"][0] > 0),
                "detail": "no shared sessions" if not paired_vs_null else str(paired_vs_null["ci95"]),
            }
        )
        first_ci = None
        if first:
            shared = sorted(set(first.get("session_pnl") or {}) & set(base.get("session_pnl") or {}))
            if len(shared) >= 2:
                first_ci = paired(
                    [first["session_pnl"][k] for k in shared],
                    [base["session_pnl"][k] for k in shared],
                    draws=2000,
                    seed=7,
                )["ci95"]
        clauses.append(
            {
                "clause": 4,
                "name": "first_row control FAILS clause 3",
                "pass": bool(first_ci and first_ci[0] <= 0),
                "detail": "no first_row sessions" if not first_ci else str(first_ci),
            }
        )
        worst = row.get("worst_minimum_capital")
        clauses.append(
            {
                "clause": 5,
                "name": "worst minimum capital >= 4500",
                "pass": worst is not None and Decimal(worst) >= 4500,
                "detail": str(worst),
            }
        )
        calls = row["model_calls"]
        fails = row["model_failures"]
        clauses.append(
            {
                "clause": 6,
                "name": "model-failure rate < 5%",
                "pass": calls == 0 or (fails / calls) < 0.05,
                "detail": f"{fails}/{calls}",
            }
        )
        checks.append(
            {
                "policy": policy,
                "clauses": clauses,
                "all_pass": all(c["pass"] for c in clauses),
                "paired_vs_no_trade": paired_vs_null,
            }
        )
    adjusted = holm(p_values) if p_values else {}
    for check in checks:
        check["holm_p"] = adjusted.get(check["policy"])
        check["holm_pass"] = check.get("holm_p") is not None and check["holm_p"] < 0.05
        check["all_pass"] = bool(check["all_pass"] and check["holm_pass"])
    return checks


def _with_paired_columns(
    cards: list[dict[str, Any]], paired_cols: Mapping[str, Mapping[str, Mapping[str, Any]]]
) -> list[dict[str, Any]]:
    """Attach the paired-control columns to their scorecards (additive keys;
    controls and cards without shared sessions simply carry none)."""
    for card in cards:
        cols = paired_cols.get(str(card["policy"]))
        if cols:
            card.update(cols)
    return cards


def _paired_columns(
    runs: Mapping[str, list[Mapping[str, Any]]],
    entries: Sequence[PolicyEntry],
    *,
    seed: int,
) -> dict[str, dict[str, dict[str, Any]]]:
    """Paired per-session differences vs the two controls for every policy
    that shares sessions with them (``longrun.paired``: bootstrap CI and a
    one-sided sign-flip p). The registered promotion rule's clauses 2-3 read
    exactly these numbers; nothing about them is a model's claim."""
    from tree_options.desk.longrun import paired

    series = {e.policy: _session_series(runs[e.policy]) for e in entries}
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for entry in entries:
        own = series[entry.policy]
        cols: dict[str, dict[str, Any]] = {}
        for control in ("no_trade", lab.FIRST_ROW_POLICY):
            base = series.get(control) or {}
            keys = sorted(set(own) & set(base))
            if entry.policy == control or len(keys) < 2:
                continue
            cols[f"vs_{control}"] = paired(
                [own[k] for k in keys],
                [base[k] for k in keys],
                draws=2000,
                seed=seed,
            )
        if cols:
            out[entry.policy] = cols
    return out


def _sample_snapshots(days: Sequence[date]) -> list[str]:
    """The deterministic gap-sample boards of one slice: the FIRST and the
    MIDDLE of the slice's scheduled snapshots, in schedule order."""
    scheduled = [f"s:{day.isoformat()}T{clock}" for day in days for clock in iag.schedule_for(day)]
    picks = sorted({0, len(scheduled) // 2}) if scheduled else []
    return [scheduled[index] for index in picks]


def gap_samples(
    document: Mapping[str, Any], raw: Mapping[str, Any], days: Sequence[date]
) -> dict[str, Any]:
    """Hindsight gap rows for two deterministic boards of one slice, computed
    from the POLICY'S OWN receipts (chosen vs the hindsight best, with the
    aliased feature rows). A board with no evaluable outcome is absent, never
    zeroed — that case is reported explicitly."""
    snapshots = _sample_snapshots(days)
    out: dict[str, Any] = {"snapshots": snapshots, "boards": []}
    if not snapshots:
        return {**out, "reason": "no_scheduled_snapshots"}
    wanted = set(snapshots)
    receipts = [
        receipt
        for receipt in document.get("receipts", [])
        if str(receipt.get("snapshot")) in wanted
    ]
    if not receipts:
        return {**out, "reason": "no_receipts_on_sample_boards"}
    report = hindsight.gap_report(
        {"policy": document.get("policy"), "sessions": document["sessions"], "receipts": receipts},
        raw,
    )
    out["boards"] = report["boards"]
    out["totals"] = report["totals"]
    if not report["boards"]:
        out["reason"] = "no_evaluable_outcome"
    return out


# ------------------------------------------------------------------ run


def _challenge_dir(root: Path, stamp: str) -> Path:
    candidate = root / stamp
    suffix = 0
    while candidate.exists():
        suffix += 1
        candidate = root / f"{stamp}-{suffix}"
    return candidate


def run_challenge(
    *,
    store_root: Path,
    now: datetime,
    lab_root: Path | None = None,
    windows: tuple[QuotaWindow, ...] = (),
    windows_age_s: float | None = None,
    budget: ChallengeBudget | None = None,
    rounds: int | None = None,
    transport: Any = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """One challenge: every selected bundle, the whole policy field, the same
    mechanical accounting, gap samples, and one digest. A dry run computes the
    plan and writes nothing."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    store = Path(store_root)
    root = Path(lab_root) if lab_root is not None else lab.default_root()
    plan_budget = budget if budget is not None else ChallengeBudget()
    selected = _select_bundles(discover_bundles(store), rounds)
    if not selected:
        return {
            "schema": CHALLENGE_SCHEMA,
            "status": "no_bundles",
            "at": now.isoformat(),
            "store_root": str(store),
            "bundles": [],
        }

    prepared: list[tuple[Path, dict[str, Any], list[list[date]]]] = []
    for path in selected:
        raw = json.loads(path.read_bytes())
        prepared.append((path, raw, partition_sessions(raw, seed=DEFAULT_SEED)))

    # the gate: a FRESH snapshot gates the model policies; a stale or missing
    # snapshot means the standing conservative budget (the hard caps)
    fresh = windows_age_s is not None and windows_age_s <= FRESH_WINDOW_S and bool(windows)
    mode = "under_using_gate" if fresh else "standing_budget"
    gated = fresh and not lab.burn_allowed(windows)
    field_policies: list[PolicyEntry] = []
    skipped: list[dict[str, str]] = []
    for entry in policy_field(root):
        if entry.kind == "model" and gated:
            skipped.append({"policy": entry.policy, "reason": QUOTA_DRY})
            field_policies.append(replace(entry, skipped_reason=QUOTA_DRY))
        else:
            field_policies.append(entry)
    runners = [entry for entry in field_policies if entry.skipped_reason is None]
    model_runners = [entry for entry in runners if entry.kind == "model"]

    counter = BudgetCounter(plan_budget)
    boards_per_run = _boards_per_run(plan_budget, len(prepared), len(model_runners))
    budget_view = {
        "boards_per_round": plan_budget.boards,
        "reflections_per_round": plan_budget.reflections,
        "total_boards": plan_budget.total_boards,
        "boards_used": counter.boards_used,
        "reflections_used": 0,
    }
    rounds_view = [
        {
            "bundle": path.name,
            "vintage": path.parent.name,
            "sessions": [d.isoformat() for d in hindsight.all_sessions(raw)],
            "slices": [[d.isoformat() for d in part] for part in slices],
        }
        for path, raw, slices in prepared
    ]
    plan_document = {
        "schema": CHALLENGE_SCHEMA,
        "status": "dry_run",
        "at": now.isoformat(),
        "mode": mode,
        "seed": DEFAULT_SEED,
        "store_root": str(store),
        "bundles": [path.name for path, _raw, _s in prepared],
        "policies": [
            {"policy": e.policy, "kind": e.kind, "skipped_reason": e.skipped_reason}
            for e in field_policies
        ],
        "boards_per_run": boards_per_run,
        "budget": budget_view,
        "rounds": rounds_view,
    }
    if dry_run:
        return plan_document

    challenge_dir = _challenge_dir(challenge_root(store), now.strftime("%Y%m%dT%H%M%SZ"))
    run_root = challenge_dir / "runs"
    round_entries: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="desk-challenge-") as tmp:
        for path, raw, slices in prepared:
            counter.start_round()
            runs: dict[str, list[dict[str, Any]]] = {e.policy: [] for e in runners}
            samples: list[dict[str, Any]] = []
            for slice_index, days in enumerate(slices):
                if not days:
                    continue  # a bundle shorter than three sessions: no empty runs
                slice_raw = _slice_bundle(raw, days)
                slice_path = Path(tmp) / f"{path.parent.name}-s{slice_index}.json"
                slice_path.write_text(json.dumps(slice_raw))
                for entry in runners:
                    config = lab.LabConfig(
                        bundle=slice_path,
                        policy=entry.policy,
                        sessions=len(days),
                        boards_cap=boards_per_run,
                        lab_root=run_root,
                        policy_prompt=entry.prompt,
                    )
                    if entry.kind == "model":
                        # refuses BEFORE the burn when the caps cannot hold it
                        counter.take_boards(boards_per_run)
                    document = lab.run_lab(
                        config, windows=windows, transport=transport, now=now, burn_gate=fresh
                    )
                    if entry.kind == "model":
                        shown = int(document.get("boards_shown", 0) or 0)
                        counter.refund_boards(max(0, boards_per_run - shown))
                        if document.get("status") == "ok":
                            samples.append(
                                {
                                    "policy": entry.policy,
                                    "slice": slice_index,
                                    "sessions": [d.isoformat() for d in days],
                                    **gap_samples(document, slice_raw, days),
                                }
                            )
                    runs[entry.policy].append(document)
            round_entries.append(
                {
                    "bundle": path.name,
                    "vintage": path.parent.name,
                    "sessions": [d.isoformat() for d in hindsight.all_sessions(raw)],
                    "slices": [[d.isoformat() for d in part] for part in slices],
                    "skipped": skipped,
                    "scorecards": _ranked(
                        _with_paired_columns(
                            [scorecard(entry, runs[entry.policy]) for entry in runners],
                            _paired_columns(runs, runners, seed=DEFAULT_SEED),
                        )
                    ),
                    "gap_samples": samples,
                }
            )

    archive_after = [
        {"id": record["id"], "generation": record["generation"], "stats": record["stats"]}
        for record in gepa.load_archive(root)
    ]
    # an honest status: a challenge whose every round showed zero boards
    # (a probe shard, an empty vintage) executed nothing measurable — say
    # so instead of a plain ok (the first real run shipped exactly that)
    total_boards = sum(
        int(sc.get("boards", 0) or 0) for rnd in round_entries for sc in rnd["scorecards"]
    )
    status = QUOTA_DRY if gated else ("empty_bundles" if total_boards == 0 else "ok")
    document = {
        "schema": CHALLENGE_SCHEMA,
        "status": status,
        "at": now.isoformat(),
        "mode": mode,
        "seed": DEFAULT_SEED,
        "store_root": str(store),
        "bundles": [path.name for path, _raw, _s in prepared],
        "policies": [
            {"policy": e.policy, "kind": e.kind, "skipped_reason": e.skipped_reason}
            for e in field_policies
        ],
        "boards_per_run": boards_per_run,
        "budget": {**budget_view, "boards_used": counter.boards_used},
        "windows": {
            "fresh": fresh,
            "age_s": windows_age_s,
            "under_using": [w.name for w in windows if w.under_using],
        },
        "rounds": round_entries,
        "archive_after": archive_after,
        "untrusted_note": UNTRUSTED_NOTE,
        "promotion": {"promoted": False, "rule": PROMOTION_RULE},
    }
    challenge_dir.mkdir(parents=True, exist_ok=True)
    (challenge_dir / "digest.json").write_text(json.dumps(document, indent=2, default=str))
    (challenge_dir / "digest.md").write_text(_digest_md(document))
    document["digest_dir"] = str(challenge_dir)
    return document


def _digest_md(document: Mapping[str, Any]) -> str:
    budget = document.get("budget", {})
    lines: list[str] = []
    add = lines.append
    add(f"# Desk challenge digest — {document['at']}")
    add("")
    add(f"> {document['untrusted_note']}")
    add("")
    add(
        f"Mode: {document.get('mode')}; partition seed {document.get('seed')}; "
        f"{len(document.get('rounds', []))} bundle(s); board calls "
        f"{budget.get('boards_used', 0)}/{budget.get('total_boards', 0)} "
        f"(cap {budget.get('boards_per_round', 0)} per round); "
        f"board cap per run {document.get('boards_per_run')}."
    )
    add("")
    add("## Scorecards (mechanical replay accounting; ranked by summed closed-pnl)")
    for entry in document.get("rounds", []):
        add("")
        add(f"### {entry['vintage']} — {entry['bundle']}")
        add(f"- sessions: {' '.join(entry.get('sessions', []))}")
        for index, part in enumerate(entry.get("slices", [])):
            add(f"- slice {index}: {' '.join(part) if part else '(empty)'}")
        for skip in entry.get("skipped", []):
            add(f"- skipped {skip['policy']}: {skip['reason']}")
        for card in entry.get("scorecards", []):
            add(
                f"- {card['policy']} ({card['kind']}): runs {card['runs']}, "
                f"boards {card['boards']}, entered {card['entered']}, "
                f"wins {card['modeled_wins']}, losses {card['modeled_losses']}, "
                f"closed pnl {card['closed_pnl_sum']}, worst minimum capital "
                f"{card['worst_minimum_capital']}, peak reserved "
                f"{card['peak_open_loss_reserved']}"
            )
            for control in ("vs_no_trade", "vs_first_row"):
                if card.get(control):
                    p = card[control]
                    add(
                        f"  - {control}: diff {p['diff_total']} "
                        f"ci95 [{p['ci95'][0]}, {p['ci95'][1]}] "
                        f"p={p['p_one_sided']} over {p['sessions']} sessions"
                    )
            add(f"  - run dirs: {', '.join(card['run_dirs'])}")
        for sample in entry.get("gap_samples", []):
            add(
                f"- gap sample {sample['policy']} slice {sample['slice']}: "
                f"{' '.join(sample['snapshots'])}"
            )
            if sample.get("reason"):
                add(f"  - none evaluable: {sample['reason']}")
            for board in sample.get("boards", []):
                add(
                    f"  - gap {board['gap']} at {board['snapshot']}: chosen "
                    f"{board['chosen']} (outcome {board['chosen_outcome']}) vs "
                    f"best {board['best']} (outcome {board['best_outcome']})"
                )
                if board.get("chosen_row") is not None:
                    add("    - chosen row: " + json.dumps(board["chosen_row"], sort_keys=True))
                if board.get("best_row") is not None:
                    add("    - best row: " + json.dumps(board["best_row"], sort_keys=True))
    add("")
    add("## Archive after the challenge (mechanical stats; nothing promoted)")
    for record in document.get("archive_after", []):
        add(
            f"- gepa:{record['id']} (generation {record['generation']}): "
            + json.dumps(record["stats"], sort_keys=True)
        )
    if not document.get("archive_after"):
        add("- (empty archive)")
    add("")
    promotion = document.get("promotion", {})
    add(f"Promoted: {promotion.get('promoted')} — {promotion.get('rule')}")
    add("")
    return "\n".join(lines) + "\n"


# -------------------------------------------------------------------- CLI


def _cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tree_options.desk challenge run",
        description="The end-to-end challenge game: every policy on every "
        "frozen bundle, one mechanical scorecard per policy.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="score the field over the store's bundles")
    run.add_argument(
        "--bundles-from",
        type=Path,
        default=None,
        help="the desk store holding evaluations/intraday-graph (default DESK_STORE)",
    )
    run.add_argument(
        "--windows",
        type=Path,
        default=None,
        help="quota snapshot (a FRESH snapshot gates model policies)",
    )
    run.add_argument(
        "--lab-root", type=Path, default=None, help="the lab root holding the policy archive"
    )
    run.add_argument(
        "--rounds", type=int, default=None, help="play only the newest N bundles (default: all)"
    )
    run.add_argument(
        "--dry-run", action="store_true", help="compute and print the plan; write nothing"
    )
    args = parser.parse_args(argv)
    store = args.bundles_from if args.bundles_from is not None else paths.store_root()
    now = datetime.now(UTC)
    windows: tuple[QuotaWindow, ...] = ()
    age: float | None = None
    if args.windows is not None and args.windows.exists():
        try:
            windows = load_windows(args.windows)
            age = max(0.0, now.timestamp() - args.windows.stat().st_mtime)
        except (ValueError, OSError) as error:
            print(f"refused: {error}", file=sys.stderr)
            return 2
    try:
        document = run_challenge(
            store_root=store,
            now=now,
            lab_root=args.lab_root,
            windows=windows,
            windows_age_s=age,
            rounds=args.rounds,
            dry_run=args.dry_run,
        )
    except (BudgetRefused, ValueError, OSError, KeyError) as error:
        print(f"refused: {error}", file=sys.stderr)
        return 2
    print(json.dumps(document, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
