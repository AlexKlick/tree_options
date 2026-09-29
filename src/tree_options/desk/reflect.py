"""GEPA-style REFLECTION over a desk long run's TRAIN split.

MiniMax-M3.1-Flash acts as a panel of K trader-theorists (seed schools:
trend, mean-reversion, volatility-premium, cost-minimizer, patience/holding,
contrarian-to-own-bias). Each reads the same DOSSIERS, built mechanically
from a long run's receipts and the v2 outcome table, and proposes ONE
revised, falsifiable policy sentence. The accepted proposals become a config
fragment of ``kind: model`` policies that the NEXT long run tests honestly
on the held-out sessions (after the cutoff).

Leakage discipline (the point of this lane):

- only boards whose session is <= the cutoff are loaded; a later board, its
  receipts and its outcomes never enter a dossier;
- an outcome of a train board counts only when it was REALIZED by the
  cutoff (the ET session of its exit is <= the cutoff). A longer hold that
  exits later is WITHHELD ("x"): an expiry trade entered before the cutoff
  would otherwise carry held-out prices into the reflection;
- no dates, snapshot ids or tickers reach the model: decisions are labelled
  d1..dn and each board is rendered with the model's own whitelisted fields,
  aliases and public context.

Model output is untrusted. A proposal that breaks the STRICT JSON contract,
cites a date, a date proxy or a ticker, redefines the reply format, names an
unknown horizon, exceeds 700 characters or nearly duplicates an accepted or
existing prompt is rejected and regenerated (bounded attempts), never
repaired. Nothing here promotes: the fragment is a list of hypotheses for the
next long run, whose pre-registered walk-forward is the only judge.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import re
import statistics
import sys
import threading
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, date, datetime
from itertools import pairwise
from pathlib import Path
from typing import Any

from tree_options.desk import lab, longrun
from tree_options.desk.intraday_action_graph import ET
from tree_options.trex.discovery.llm import LlmError, PostTransport, chat_json, urllib_post

FRAGMENT_SCHEMA = "desk-reflect-fragment/1"
DOSSIER_SCHEMA = "desk-reflect-dossiers/1"
CALL_SCHEMA = "desk-reflect-call/1"
DEFAULT_PROVIDER = "minimax-flash"
HORIZONS: tuple[str, ...] = lab.V2_HORIZONS
#: a train-board outcome not realized by the cutoff (or absent): never shown
WITHHELD = "x"
STRATA = ("win", "loss", "skip_would_win", "skip_right")
NAME_PREFIX = "refl-"
PROMPT_MAX = 700
PROMPT_MIN = 40
HYPOTHESIS_MAX = 400
EVIDENCE_MAX = 1200
EFFECT_MAX = 600
NAME_MAX = 60
MAX_K = 12
MAX_CONCURRENCY = 3
MAX_ATTEMPTS = 5
#: word-bigram Jaccard at or above which two prompts are "near-identical"
DUP_THRESHOLD = 0.6
#: token estimate: compact-JSON characters per token (no tokenizer dependency)
CHARS_PER_TOKEN = 4
DEFAULT_SAMPLES = 8
DEFAULT_MAX_PACK_TOKENS = 48_000
DEFAULT_ATTEMPTS = 3
DEFAULT_TIMEOUT_S = 240.0
POLICY_PLACEHOLDER = "<POLICY>"

#: (key, lens) of the seed theorists; K > 6 cycles them as second takes
PERSONAS: tuple[tuple[str, str], ...] = (
    ("trend", "a trend-following theorist: edges come from trading in the direction of "
              "persistent multi-session moves and staying out when the trend is unclear"),
    ("meanrev", "a mean-reversion theorist: stretched short-horizon moves partially reverse, "
                "so fading extremes (and not chasing them) is the edge"),
    ("volprem", "a volatility-premium theorist: option sellers are paid for bearing variance, "
                "so credit structures, strike distance and realized volatility decide P&L"),
    ("costmin", "a cost-minimizer: a fixed round-trip cost eats small-edge trades, so fewer, "
                "larger-payoff trades held long enough to beat the cost are what survive"),
    ("patience", "a patience/holding theorist: the holding horizon, not the entry, decides "
                 "P&L; time in the trade and when to exit are the edge"),
    ("contrarian", "a contrarian to the policies' own biases: find each policy's systematic "
                   "habit (row position, favourite horizon, favourite direction or structure) "
                   "that the hindsight shows is costly, and design a policy that counters it"),
)

REFLECT_TASK = (
    "You are one trader-theorist on a panel reviewing a paper-trading options policy lab IN "
    "HINDSIGHT. The evidence holds one dossier per LLM policy: its policy sentence, its "
    "aggregate behaviour on the TRAIN boards, and sampled decisions showing the board exactly "
    "as the policy saw it plus the realized net of every row at every horizon; the universe "
    "block gives the base rates of every row. Read it through the lens of your_school (at the "
    "end): diagnose which systematic mistakes cost money and which regularities the policies "
    "missed, then write ONE revised policy sentence. It will be tested honestly on LATER "
    "held-out sessions it never saw, which may be a different regime, so prefer a mechanism "
    "that generalizes over one fitted to these boards. Rules: the sentence replaces "
    f"{POLICY_PLACEHOLDER} in board_task and nothing else - the caps, costs, horizon menu and "
    "the JSON reply contract stay fixed, so never describe a reply format and never use "
    "braces; name only the horizons intraday, eod, hold:5, expiry; use only what the board "
    "shows (aliased underlyings, their returns and realized vol, time of day, the row "
    "fields); never mention dates, months, years, the session ordinal or real ticker "
    "symbols. Return STRICT JSON {\"name\": \"<2-4 word kebab-case label>\", \"hypothesis\": "
    "\"<ONE falsifiable sentence: what beats the random-row baseline and why>\", "
    "\"evidence\": \"<the dossier statistics it rests on, citing policy names and numbers>\", "
    f"\"prompt\": \"<the policy sentence, at most {PROMPT_MAX} characters>\", "
    "\"expected_effect\": \"<the entry rate, horizon and direction mix you expect, and the "
    "held-out result that would FALSIFY the hypothesis>\"}. No other text.")

FEEDBACK = (
    "Your previous reply was rejected ({reasons}). Return a corrected STRICT JSON object with "
    "the keys name, hypothesis, evidence, prompt, expected_effect and no other text; if it "
    "was a near-duplicate, propose a genuinely different mechanism.")

LEGEND: dict[str, Any] = {
    "split": ("TRAIN split only: every board, decision and outcome here comes from the earlier "
              "sessions; outcomes realized only after the split are withheld"),
    "horizons": list(HORIZONS),
    "aggregate": ("per policy over its train decisions: entry_rate; horizon, direction and "
                  "structure mix of its entries; row_position (row0_share vs "
                  "uniform_row0_share, the share a position-blind picker gives the first "
                  "row; mean_relative_position 0 = first row, 1 = last, 0.5 = unbiased); "
                  "outcomes in net dollars after the fixed round-trip cost; "
                  "pnl_by_structure_horizon over its evaluated entries"),
    "samples": ("decisions sampled over the strata win, loss, skip_would_win (skipped a board "
                "whose random-row mean was > 0) and skip_right (random-row mean <= 0)"),
    "board": "the rows exactly as the policy saw them; cols name each row's fields; the row "
             "index is the rendered position",
    "net_by_row": ("per board row, the realized net dollars at each horizon in the order of "
                   "horizons; null = no fill (no trade possible, counts 0); \"x\" = withheld"),
    "board_mean": ("the random-row baseline: the mean net over every (row, horizon) option of "
                   "the board, no fill = 0, withheld options excluded"),
    "board_mean_at_chosen_horizon": "the mean net of every row at the horizon chosen",
    "best": "the hindsight-best (row, horizon); null when no option beat 0 (skipping)",
    "regret": "the best achievable (skip = 0 included) minus the realized net",
    "universe": "every train board row at every horizon: the base rates any policy faces",
}

Cell = float | str | None  # realized net | WITHHELD | None (no fill)
QuotaFn = Callable[[], tuple[bool, str]]

_ISO_DATE = re.compile(r"\b(?:19|20)\d{2}[-/.]\d{1,2}[-/.]\d{1,2}\b|\b\d{1,2}/\d{1,2}/\d{2,4}\b")
_MONTHS = ("January|February|March|April|June|July|August|September|October|November|"
           "December")
_MONTH_NAME = re.compile(rf"\b(?:{_MONTHS})\b")
_MONTH_DAY = re.compile(r"\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|"
                        r"July?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|"
                        r"Dec(?:ember)?)\.?\s+\d{1,2}(?:st|nd|rd|th)?\b")
_YEAR = re.compile(r"(?<![\d.$+-])(?:19|20)\d{2}(?![\d.])")
_DATE_PROXY = re.compile(r"session[_ ]ordinal", re.IGNORECASE)
_INDEX_NAMES = re.compile(r"s&p\s*500|\bnasdaq\b|\brussell\b|\bdow jones\b", re.IGNORECASE)
_CASHTAG = re.compile(r"\$[A-Z]{1,5}\b")
KNOWN_TICKERS = frozenset({
    "SPY", "QQQ", "IWM", "DIA", "SPX", "NDX", "RUT", "VIX", "XSP", "AAPL", "MSFT", "NVDA",
    "TSLA", "AMZN", "GOOGL", "GOOG", "META"})
_HOLD = re.compile(r"hold\s*:\s*(\d+)", re.IGNORECASE)
_ABBREV = re.compile(r"\b(?:e\.g|i\.e|vs|etc|approx|cf|incl|min|max)\.", re.IGNORECASE)
_SENTENCE_BREAK = re.compile(r"[.!?]\s+[A-Z(\"']")


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _cutoff(value: str | date) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value))


def estimate_tokens(obj: Any) -> int:
    """Token estimate of ``obj`` as the compact JSON the model receives."""
    text = obj if isinstance(obj, str) else json.dumps(obj, separators=(",", ":"), default=str)
    return math.ceil(len(text) / CHARS_PER_TOKEN)


def _round(value: float | None, places: int = 1) -> float | None:
    return None if value is None else round(float(value), places)


# ---------------------------------------------------------------- outcomes


@dataclass(frozen=True)
class OutcomeRow:
    net: float | None  # None: no fill
    exit_day: date | None  # the ET session of the exit


@dataclass(frozen=True)
class OutcomeTable:
    cells: dict[tuple[str, str, str], OutcomeRow]
    tickers: frozenset[str]


def _et_day(text: Any) -> date | None:
    if not isinstance(text, str) or not text:
        return None
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(ET).date()


def load_outcome_table(path: Path, snapshots: set[str]) -> OutcomeTable:
    """The v2 outcome table (``desk outcome-table`` JSONL) restricted to
    ``snapshots`` (the train boards) and the v2 horizons; every other row is
    never retained. ``tickers`` (the real underlyings) feed the validator."""
    cells: dict[tuple[str, str, str], OutcomeRow] = {}
    tickers: set[str] = set()
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict):
                continue
            if isinstance(row.get("underlying"), str) and row["underlying"]:
                tickers.add(row["underlying"])
            snapshot, mode = row.get("snapshot"), row.get("exit_mode")
            if snapshot not in snapshots or mode not in HORIZONS:
                continue
            key = (str(snapshot), str(row.get("candidate_id")), str(mode))
            net = row.get("net")
            if row.get("status") == "no_fill" or net is None:
                cells[key] = OutcomeRow(None, None)
            else:
                cells[key] = OutcomeRow(float(net), _et_day(row.get("exit_at")))
    return OutcomeTable(cells, frozenset(tickers))


def cell(table: OutcomeTable, snapshot: str, candidate: str, horizon: str,
         cutoff: date) -> Cell:
    """A train outcome as the reflection may see it: the realized net, None
    (no fill) or WITHHELD (absent, or its exit falls after the cutoff)."""
    row = table.cells.get((snapshot, candidate, horizon))
    if row is None:
        return WITHHELD
    if row.net is None:
        return None
    if row.exit_day is None or row.exit_day > cutoff:
        return WITHHELD
    return row.net


def _value(c: Cell) -> float | None:
    """A cell's contribution to a mean: no fill = 0, withheld = excluded."""
    if isinstance(c, float):
        return c
    return 0.0 if c is None else None


def _fmt(c: Cell) -> float | str | None:
    return round(c, 1) if isinstance(c, float) else c


# -------------------------------------------------------------------- run


@dataclass(frozen=True)
class TrainBoard:
    snapshot: str
    session: date
    rows: list[dict[str, Any]]
    context: dict[str, Any]

    @property
    def ids(self) -> list[str]:
        return [str(row.get("id")) for row in self.rows]


@dataclass(frozen=True)
class PolicyArms:
    name: str
    prompt: str | None
    arms: tuple[str, ...]


@dataclass
class RunView:
    run_dir: Path
    config: dict[str, Any]
    cutoff: date
    policies: list[PolicyArms]
    boards: dict[str, TrainBoard]
    receipts: dict[str, dict[str, dict[str, Any]]]
    failed: dict[str, int]

    @property
    def sessions(self) -> list[date]:
        return sorted({board.session for board in self.boards.values()})


def load_run(run_dir: Path, cutoff: str | date) -> RunView:
    """The TRAIN split of a long-run dir, read-only: its model policies, the
    boards with session <= ``cutoff`` and those boards' ok receipts. Later
    boards are skipped while reading; their receipts are dropped on load."""
    cut = _cutoff(cutoff)
    config = json.loads((run_dir / "config.json").read_text(encoding="utf-8"))
    if not isinstance(config, dict) or not isinstance(config.get("policies"), list):
        raise ValueError(f"{run_dir}: config.json has no policies list")
    specs = longrun.policies_from_config(config["policies"], builtin=False)
    policies = [PolicyArms(s.name, s.prompt, tuple(s.arm_names()))
                for s in specs if s.kind == "model"]
    if not policies:
        raise ValueError(f"{run_dir}: no model policy to reflect on")
    boards: dict[str, TrainBoard] = {}
    with (run_dir / "boards.jsonl").open(encoding="utf-8") as stream:
        for line in stream:
            line = line.strip()
            if not line:
                continue
            doc = json.loads(line)
            session = date.fromisoformat(str(doc["session"]))
            if session > cut:
                continue  # a held-out board is never retained
            boards[str(doc["snapshot"])] = TrainBoard(
                str(doc["snapshot"]), session, list(doc["rows"]), dict(doc.get("context") or {}))
    receipts: dict[str, dict[str, dict[str, Any]]] = {}
    failed: dict[str, int] = {}
    for policy in policies:
        for arm in policy.arms:
            kept: dict[str, dict[str, Any]] = {}
            failures = 0
            for snapshot, rec in longrun.load_receipts(longrun.receipts_path(run_dir, arm)).items():
                board = boards.get(snapshot)
                try:
                    session = date.fromisoformat(str(rec.get("session")))
                except ValueError:
                    continue
                if board is None or session > cut or session != board.session:
                    continue
                if rec.get("ok"):
                    kept[snapshot] = rec
                else:
                    failures += 1
            receipts[arm], failed[arm] = kept, failures
    return RunView(run_dir, config, cut, policies, boards, receipts, failed)


# -------------------------------------------------------------- decisions


@dataclass(frozen=True)
class Decision:
    policy: str
    arm: str
    board: TrainBoard
    choice: str | None
    row: int | None
    horizon: str | None
    note: str
    cells: list[list[Cell]]  # per board row, per HORIZONS
    realized: float | None  # the net earned (skip = 0); None when unevaluable
    stratum: str  # one of STRATA, or "unevaluable"
    board_mean: float | None
    board_mean_h: float | None
    best: tuple[int, str, float] | None
    regret: float | None

    @property
    def entered(self) -> bool:
        return self.choice is not None


def _mean(values: Sequence[float]) -> float | None:
    return float(statistics.fmean(values)) if values else None


def decision_of(policy: str, arm: str, rec: Mapping[str, Any], board: TrainBoard,
                table: OutcomeTable, cutoff: date) -> Decision:
    """One receipt in hindsight against every (row, horizon) of its board."""
    ids = board.ids
    cells = [[cell(table, board.snapshot, rid, h, cutoff) for h in HORIZONS] for rid in ids]
    options = [v for row in cells for c in row if (v := _value(c)) is not None]
    board_mean = _mean(options)
    realized_cells = [(i, h, c) for i, row in enumerate(cells)
                      for h, c in zip(HORIZONS, row, strict=True) if isinstance(c, float)]
    top = max(realized_cells, key=lambda t: (t[2], -t[0], -HORIZONS.index(t[1])),
              default=None)
    best = top if top is not None and top[2] > 0 else None
    best_value = best[2] if best is not None else 0.0
    choice = rec.get("choice")
    horizon = rec.get("horizon")
    note = str(rec.get("note") or "")
    if choice is None:
        stratum = ("unevaluable" if board_mean is None
                   else "skip_would_win" if board_mean > 0 else "skip_right")
        return Decision(policy, arm, board, None, None, None, note, cells,
                        0.0 if board_mean is not None else None, stratum, board_mean, None,
                        best, (best_value if board_mean is not None else None))
    if str(choice) not in ids or horizon not in HORIZONS:
        return Decision(policy, arm, board, str(choice), None, None, note, cells, None,
                        "unevaluable", board_mean, None, best, None)
    row = ids.index(str(choice))
    column = HORIZONS.index(str(horizon))
    at_h = [v for r in cells if (v := _value(r[column])) is not None]
    chosen = cells[row][column]
    if isinstance(chosen, float):
        return Decision(policy, arm, board, str(choice), row, str(horizon), note, cells, chosen,
                        "win" if chosen > 0 else "loss", board_mean, _mean(at_h), best,
                        best_value - chosen)
    return Decision(policy, arm, board, str(choice), row, str(horizon), note, cells, None,
                    "unevaluable", board_mean, _mean(at_h), best, None)


def policy_decisions(view: RunView, policy: PolicyArms,
                     table: OutcomeTable) -> list[Decision]:
    out: list[Decision] = []
    for arm in policy.arms:
        for snapshot, rec in sorted(view.receipts.get(arm, {}).items()):
            out.append(decision_of(policy.name, arm, rec, view.boards[snapshot], table,
                                   view.cutoff))
    return out


def _count(values: Sequence[Any]) -> dict[str, int]:
    return dict(sorted(Counter(str(v) for v in values).items()))


def aggregate(decisions: Sequence[Decision], *, train_boards: int,
              failed: int = 0) -> dict[str, Any]:
    """A policy's behaviour and outcomes on its train decisions."""
    entries = [d for d in decisions if d.entered and d.row is not None]
    evaluated = [d for d in entries if d.realized is not None]
    skips = [d for d in decisions if not d.entered]
    arms = sorted({d.arm for d in decisions})
    rows = [d.board.rows[d.row] for d in entries if d.row is not None]
    rel = [d.row / (len(d.board.rows) - 1) for d in entries
           if d.row is not None and len(d.board.rows) > 1]
    nets = [d.realized for d in evaluated if d.realized is not None]
    by_cell: dict[str, list[float]] = {}
    for d in evaluated:
        key = f"{d.board.rows[d.row or 0].get('structure')}|{d.horizon}"
        by_cell.setdefault(key, []).append(float(d.realized or 0.0))
    withheld = [d for d in entries if d.realized is None and d.row is not None
                and d.horizon is not None
                and d.cells[d.row][HORIZONS.index(d.horizon)] == WITHHELD]
    skip_means = [d.board_mean for d in skips if d.board_mean is not None]
    regrets = [d.regret for d in decisions if d.regret is not None]
    doc: dict[str, Any] = {
        "train_boards": train_boards, "decided": len(decisions), "failed_receipts": failed,
        "entered": len(entries),
        "entry_rate": round(len(entries) / len(decisions), 3) if decisions else None,
        "horizon_mix": _count([d.horizon for d in entries]),
        "direction_mix": _count([row.get("direction") for row in rows]),
        "structure_mix": _count([row.get("structure") for row in rows]),
        "row_position": {
            "row0_share": round(sum(d.row == 0 for d in entries) / len(entries), 3)
            if entries else None,
            "uniform_row0_share": (round(statistics.fmean(1 / len(d.board.rows)
                                                          for d in entries), 3)
                                   if entries else None),
            "mean_relative_position": round(statistics.fmean(rel), 3) if rel else None,
        },
        "outcomes": {
            "evaluated_entries": len(evaluated),
            "no_fill_entries": sum(1 for d in entries if d.realized is None
                                   and d.row is not None and d.horizon is not None
                                   and d.cells[d.row][HORIZONS.index(d.horizon)] is None),
            "withheld_entries": len(withheld),
            "net_total": _round(sum(nets), 1) if nets else 0.0,
            "net_mean": _round(_mean(nets)),
            "win_rate": round(sum(n > 0 for n in nets) / len(nets), 3) if nets else None,
            "pick_vs_board_mean_same_horizon": _round(sum(
                float(d.realized or 0.0) - d.board_mean_h for d in evaluated
                if d.board_mean_h is not None)),
            "skips": len(skips),
            "random_entry_mean_on_skipped_boards": _round(_mean(skip_means)),
            "regret_mean": _round(_mean(regrets)),
        },
        "strata": {name: sum(d.stratum == name for d in decisions)
                   for name in (*STRATA, "unevaluable")},
        "pnl_by_structure_horizon": {
            key: {"n": len(v), "net": round(sum(v), 1), "mean": round(statistics.fmean(v), 1),
                  "win_rate": round(sum(x > 0 for x in v) / len(v), 3)}
            for key, v in sorted(by_cell.items())},
    }
    if len(arms) > 1:
        doc["entry_rate_by_repeat"] = {
            arm: round(sum(d.entered for d in decisions if d.arm == arm)
                       / max(1, sum(d.arm == arm for d in decisions)), 3) for arm in arms}
    return doc


def universe(boards: Sequence[TrainBoard], table: OutcomeTable,
             cutoff: date) -> dict[str, Any]:
    """Base rates over every train board row at every horizon."""
    cells_by: dict[str, dict[str, list[Cell]]] = {"structure": {}, "direction": {}}
    board_means: list[float] = []
    for board in boards:
        options: list[float] = []
        for row in board.rows:
            for h in HORIZONS:
                c = cell(table, board.snapshot, str(row.get("id")), h, cutoff)
                cells_by["structure"].setdefault(f"{row.get('structure')}|{h}", []).append(c)
                cells_by["direction"].setdefault(f"{row.get('direction')}|{h}", []).append(c)
                if (v := _value(c)) is not None:
                    options.append(v)
        if options:
            board_means.append(statistics.fmean(options))

    def summary(values: list[Cell]) -> dict[str, Any]:
        nets = [c for c in values if isinstance(c, float)]
        return {"realized": len(nets), "no_fill": sum(c is None for c in values),
                "withheld": sum(c == WITHHELD for c in values),
                "mean_net": _round(_mean(nets)),
                "win_rate": round(sum(n > 0 for n in nets) / len(nets), 3) if nets else None}

    return {"boards": len(boards), "sessions": len({b.session for b in boards}),
            "random_row_baseline_mean_per_board": _round(_mean(board_means), 2),
            "by_structure_horizon": {k: summary(v)
                                     for k, v in sorted(cells_by["structure"].items())},
            "by_direction_horizon": {k: summary(v)
                                     for k, v in sorted(cells_by["direction"].items())}}


# ---------------------------------------------------------------- samples


def _seed_for(seed: int, name: str) -> int:
    return seed ^ int(hashlib.sha256(name.encode()).hexdigest()[:8], 16)


def stratified_sample(decisions: Sequence[Decision], n: int, seed: int) -> list[Decision]:
    """Up to ``n`` decisions spread over STRATA (a short stratum's quota passes
    to the others), within a stratum a seeded shuffle preferring distinct
    sessions; the result interleaves the strata (w, l, sww, sr, w, ...)."""
    pools: dict[str, list[Decision]] = {}
    for name in STRATA:
        pool = [d for d in decisions if d.stratum == name]
        random.Random(_seed_for(seed, name)).shuffle(pool)
        seen: set[date] = set()
        first: list[Decision] = []
        rest: list[Decision] = []
        for d in pool:
            (rest if d.board.session in seen else first).append(d)
            seen.add(d.board.session)
        pools[name] = first + rest
    quota = dict.fromkeys(STRATA, 0)
    remaining = n
    while remaining > 0:
        progressed = False
        for name in STRATA:
            if remaining and quota[name] < len(pools[name]):
                quota[name] += 1
                remaining -= 1
                progressed = True
        if not progressed:
            break
    taken = {name: pools[name][:quota[name]] for name in STRATA}
    out: list[Decision] = []
    for i in range(max(quota.values(), default=0)):
        out.extend(taken[name][i] for name in STRATA if i < len(taken[name]))
    return out


def _scrub(text: str) -> str:
    return _ISO_DATE.sub("<date>", text)[:80]


def render_sample(label: str, d: Decision) -> dict[str, Any]:
    """A sampled decision as the theorists see it: no snapshot id, no date."""
    fields = list(lab.V2_ROW_FIELDS)
    return {
        "id": label, "stratum": d.stratum, "context": d.board.context,
        "board": {"cols": fields,
                  "rows": [[row.get(k) for k in fields] for row in d.board.rows]},
        "decision": (None if d.choice is None
                     else {"row": d.row, "horizon": d.horizon}),
        "note": _scrub(d.note),
        "realized": _round(d.realized),
        "net_by_row": [[_fmt(c) for c in row] for row in d.cells],
        "best": (None if d.best is None
                 else {"row": d.best[0], "horizon": d.best[1], "net": round(d.best[2], 1)}),
        "board_mean": _round(d.board_mean),
        "board_mean_at_chosen_horizon": _round(d.board_mean_h),
        "regret": _round(d.regret),
    }


# ------------------------------------------------------------------- pack


def build_pack(view: RunView, table: OutcomeTable, *, samples_per_arm: int = DEFAULT_SAMPLES,
               max_tokens: int = DEFAULT_MAX_PACK_TOKENS, seed: int = 20260929,
               only: Sequence[str] | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """(pack, meta): the evidence every theorist reads, and its bookkeeping.
    The pack carries no date, snapshot id or ticker; samples are trimmed
    (largest dossier first, from its tail) until the estimate fits
    ``max_tokens``."""
    if samples_per_arm < 0:
        raise ValueError("samples_per_arm must be >= 0")
    policies = [p for p in view.policies if only is None or p.name in only]
    if only is not None and len(policies) != len(set(only)):
        known = sorted(p.name for p in view.policies)
        raise ValueError(f"unknown policy in {list(only)} (model policies: {known})")
    boards = sorted(view.boards.values(), key=lambda b: b.snapshot)
    counter = 0
    dossiers: list[dict[str, Any]] = []
    for policy in policies:
        decisions = policy_decisions(view, policy, table)
        picked = stratified_sample(decisions, samples_per_arm, _seed_for(seed, policy.name))
        rendered = []
        for d in picked:
            counter += 1
            rendered.append(render_sample(f"d{counter}", d))
        dossiers.append({
            "policy": policy.name,
            "policy_sentence": policy.prompt or lab.POLICY_SENTENCE,
            "default_sentence": policy.prompt is None,
            "aggregate": aggregate(decisions, train_boards=len(boards),
                                   failed=sum(view.failed.get(a, 0) for a in policy.arms)),
            "samples": rendered})
    pack: dict[str, Any] = {
        "schema": DOSSIER_SCHEMA, "legend": LEGEND,
        "split": {"train_sessions": len(view.sessions), "train_boards": len(boards)},
        "universe": universe(boards, table, view.cutoff), "dossiers": dossiers}
    trimmed = 0
    while estimate_tokens(pack) > max_tokens:
        fattest = max(dossiers, key=lambda d: (len(d["samples"]), d["policy"]), default=None)
        if fattest is None or not fattest["samples"]:
            break
        fattest["samples"].pop()
        trimmed += 1
    meta = {"run_dir": str(view.run_dir), "run_id": view.run_dir.name,
            "cutoff": view.cutoff.isoformat(),
            "train_first_session": view.sessions[0].isoformat() if view.sessions else None,
            "train_last_session": view.sessions[-1].isoformat() if view.sessions else None,
            "samples_per_arm": samples_per_arm, "max_pack_tokens": max_tokens,
            "trimmed_samples": trimmed, "seed": seed,
            "pack_tokens_est": estimate_tokens(pack),
            "dossier_tokens_est": {d["policy"]: estimate_tokens(d) for d in dossiers},
            "universe_tokens_est": estimate_tokens(pack["universe"]),
            "pack_sha256": hashlib.sha256(json.dumps(pack, sort_keys=True).encode()).hexdigest()}
    return pack, meta


# -------------------------------------------------------------- validation


@dataclass(frozen=True)
class Proposal:
    name: str
    hypothesis: str
    evidence: str
    prompt: str
    expected_effect: str


def _sentences(text: str) -> int:
    return 1 + len(_SENTENCE_BREAK.findall(_ABBREV.sub("", text)))


def _dates(text: str, *, strict: bool) -> list[str]:
    found = [m.group(0) for pattern in (_ISO_DATE, _MONTH_NAME, _MONTH_DAY)
             for m in pattern.finditer(text)]
    if strict:
        found += [m.group(0) for m in _YEAR.finditer(text)]
        found += [m.group(0) for m in _DATE_PROXY.finditer(text)]
    return found


def _tickers(text: str, tickers: frozenset[str]) -> list[str]:
    names = sorted(KNOWN_TICKERS | tickers)
    pattern = re.compile(r"\b(?:" + "|".join(re.escape(t) for t in names) + r")\b")
    return ([m.group(0) for m in pattern.finditer(text)]
            + [m.group(0) for m in _INDEX_NAMES.finditer(text)]
            + [m.group(0) for m in _CASHTAG.finditer(text)])


_LIMITS = {"name": NAME_MAX, "hypothesis": HYPOTHESIS_MAX, "evidence": EVIDENCE_MAX,
           "prompt": PROMPT_MAX, "expected_effect": EFFECT_MAX}


def validate_reply(reply: Mapping[str, Any], *,
                   tickers: frozenset[str] = frozenset()) -> tuple[Proposal | None, list[str]]:
    """The proposal, or None with every reason it breaks the contract."""
    reasons: list[str] = []
    fields: dict[str, str] = {}
    for key, limit in _LIMITS.items():
        value = reply.get(key)
        if not isinstance(value, str) or not value.strip():
            reasons.append(f"missing:{key}")
            continue
        text = " ".join(value.split())
        if len(text) > limit:
            reasons.append(f"too_long:{key}:{len(text)}>{limit}")
        fields[key] = text
    for key, text in fields.items():
        if _dates(text, strict=key in ("name", "prompt", "hypothesis")):
            reasons.append(f"date:{key}")
        if _tickers(text, tickers):
            reasons.append(f"ticker:{key}")
    prompt = fields.get("prompt")
    if prompt is not None:
        if len(prompt) < PROMPT_MIN:
            reasons.append("too_short:prompt")
        if "{" in prompt or "}" in prompt or re.search(r"\bjson\b", prompt, re.IGNORECASE):
            reasons.append("contract:prompt_redefines_reply")
        if POLICY_PLACEHOLDER in prompt:
            reasons.append("contract:placeholder_in_prompt")
        bad = sorted({f"hold:{n}" for n in _HOLD.findall(prompt) if n != "5"})
        if bad:
            reasons.append(f"horizon:{','.join(bad)}")
    hypothesis = fields.get("hypothesis")
    if hypothesis is not None and _sentences(hypothesis) > 1:
        reasons.append("hypothesis:not_one_sentence")
    evidence = fields.get("evidence")
    if evidence is not None and not re.search(r"\d", evidence):
        reasons.append("evidence:no_statistics")
    if reasons:
        return None, reasons
    return Proposal(**fields), []


def _shingles(text: str) -> set[tuple[str, ...]]:
    words = re.findall(r"[a-z0-9:._]+", text.lower())
    pairs: set[tuple[str, ...]] = set(pairwise(words))
    return pairs or {(w,) for w in words}


def similarity(a: str, b: str) -> float:
    """Word-bigram Jaccard similarity (1.0 = the same wording)."""
    left, right = _shingles(a), _shingles(b)
    if not left and not right:
        return 1.0
    return len(left & right) / len(left | right)


def duplicate_of(prompt: str, taken: Mapping[str, str]) -> str | None:
    """The name of the first taken prompt ``prompt`` nearly duplicates."""
    for name, other in taken.items():
        if similarity(prompt, other) >= DUP_THRESHOLD:
            return name
    return None


def _slug(text: str, limit: int) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:limit].strip("-") or "policy"


# ------------------------------------------------------------------ panel


def board_task_template() -> str:
    """The fixed v2 board task with the policy sentence as a placeholder
    (built by board_prompt_v2 itself, so it never drifts from the board)."""
    context = lab.BoardContext(public={}, aliases={}, spot={},
                               as_of=datetime(2000, 1, 1, tzinfo=UTC))
    content = lab.board_prompt_v2([], context, POLICY_PLACEHOLDER)[0]["content"]
    return str(json.loads(content)["task"])


def personas(k: int) -> list[tuple[str, str]]:
    if not 1 <= k <= MAX_K:
        raise ValueError(f"k must be 1..{MAX_K}")
    out: list[tuple[str, str]] = []
    for i in range(k):
        key, lens = PERSONAS[i % len(PERSONAS)]
        if i >= len(PERSONAS):
            key = f"{key}{i // len(PERSONAS) + 1}"
            lens += ("; you are a second theorist of this school, so pursue a different "
                     "mechanism than the most obvious one")
        out.append((key, lens))
    return out


def theorist_messages(pack: Mapping[str, Any], persona: tuple[str, str]) -> list[dict[str, str]]:
    payload = {"task": REFLECT_TASK, "board_task": board_task_template(), "evidence": pack,
               "your_school": {"name": persona[0], "lens": persona[1]}}
    return [{"role": "user", "content": json.dumps(payload, separators=(",", ":"))}]


class _Recorder:
    """Wraps a transport to keep the raw reply (content, finish, usage) for
    the transcript. Request headers (the key) are never recorded."""

    def __init__(self, base: PostTransport) -> None:
        self.base = base
        self.last: dict[str, Any] | None = None

    def __call__(self, url: str, body: bytes, headers: dict[str, str],
                 timeout: float) -> tuple[int, bytes]:
        self.last = None
        status, raw = self.base(url, body, headers, timeout)
        record: dict[str, Any] = {"http_status": status}
        try:
            envelope = json.loads(raw)
            choice = envelope["choices"][0]
            record.update(content=choice["message"].get("content"),
                          finish_reason=choice.get("finish_reason"),
                          usage=envelope.get("usage"))
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            record.update(content=None, raw_bytes=len(raw or b""))
        self.last = record
        return status, raw


@dataclass(frozen=True)
class Accepted:
    slot: int
    persona: str
    name: str
    proposal: Proposal
    attempt: int
    model: str


def run_panel(pack: Mapping[str, Any], *, k: int, provider: str = DEFAULT_PROVIDER,
              transport: PostTransport | None = None, concurrency: int = 2,
              max_attempts: int = DEFAULT_ATTEMPTS, timeout: float = DEFAULT_TIMEOUT_S,
              tickers: frozenset[str] = frozenset(),
              existing: Mapping[str, str] | None = None,
              sink: Callable[[dict[str, Any]], None] | None = None,
              monotonic: Callable[[], float] = time.monotonic,
              clock: Callable[[], datetime] = _utcnow
              ) -> tuple[list[Accepted], list[dict[str, Any]]]:
    """K theorists, each up to ``max_attempts`` calls; returns the accepted
    proposals (slot order) and every call record (also streamed to ``sink``)."""
    if not 1 <= concurrency <= MAX_CONCURRENCY:
        raise ValueError(f"concurrency must be 1..{MAX_CONCURRENCY}")
    if not 1 <= max_attempts <= MAX_ATTEMPTS:
        raise ValueError(f"max_attempts must be 1..{MAX_ATTEMPTS}")
    base = transport or urllib_post
    lock = threading.Lock()
    taken: dict[str, str] = dict(existing or {})
    accepted: list[Accepted] = []
    calls: list[dict[str, Any]] = []

    def theorist(slot: int, persona: tuple[str, str]) -> None:
        first = theorist_messages(pack, persona)
        messages = first
        for attempt in range(1, max_attempts + 1):
            recorder = _Recorder(base)
            started = monotonic()
            reply: dict[str, Any] | None = None
            model: str | None = None
            error: str | None = None
            try:
                reply, model = chat_json(provider, messages, transport=recorder, timeout=timeout)
            except LlmError as exc:
                error = str(exc)[:200]
            latency = round(monotonic() - started, 3)
            proposal, reasons = ((None, [f"call:{error}"]) if reply is None
                                 else validate_reply(reply, tickers=tickers))
            contract_ok = proposal is not None
            name = None
            with lock:
                if proposal is not None:
                    dup = duplicate_of(proposal.prompt, taken)
                    if dup is not None:
                        reasons, proposal = [f"near_duplicate:{dup}"], None
                    else:
                        stem = f"{NAME_PREFIX}{persona[0][:12]}-{_slug(proposal.name, 24)}"
                        name, n = stem, 2
                        while name in taken:
                            name, n = f"{stem}-{n}", n + 1
                        longrun.PolicySpec(name, "model", prompt=proposal.prompt)  # name check
                        taken[name] = proposal.prompt
                        accepted.append(Accepted(slot, persona[0], name, proposal, attempt,
                                                 str(model)))
                record = {"schema": CALL_SCHEMA, "at": clock().isoformat(), "slot": slot,
                          "persona": persona[0], "attempt": attempt, "provider": provider,
                          "model": model, "latency_s": latency, "messages": messages,
                          "response": recorder.last, "parsed": reply, "error": error,
                          "parse_ok": reply is not None, "contract_ok": contract_ok,
                          "accepted": name, "reasons": reasons}
                calls.append(record)
                if sink is not None:
                    sink(record)
            if proposal is not None:
                return
            feedback = {"role": "user", "content": FEEDBACK.format(reasons="; ".join(reasons))}
            messages = [*first,
                        *([{"role": "assistant", "content": json.dumps(reply)}]
                          if reply is not None else []),
                        feedback]

    slots = list(enumerate(personas(k), start=1))
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        for future in [pool.submit(theorist, slot, persona) for slot, persona in slots]:
            future.result()
    accepted.sort(key=lambda a: a.slot)
    calls.sort(key=lambda c: (c["slot"], c["attempt"]))
    return accepted, calls


def call_stats(calls: Sequence[Mapping[str, Any]], k: int,
               accepted: Sequence[Accepted]) -> dict[str, Any]:
    latencies = sorted(float(c["latency_s"]) for c in calls)
    usage = [c["response"]["usage"] for c in calls
             if isinstance(c.get("response"), dict) and isinstance(c["response"].get("usage"),
                                                                   dict)]
    def category(reason: str) -> str:
        if reason.startswith("call:"):
            return reason[:60]
        if reason.startswith("near_duplicate:"):
            return "near_duplicate"
        return ":".join(reason.split(":")[:2])

    reasons = Counter(category(r) for c in calls for r in c["reasons"])
    n = len(calls)
    return {
        "theorists": k, "accepted": len(accepted), "calls": n,
        "parse_ok": sum(1 for c in calls if c["parse_ok"]),
        "contract_ok": sum(1 for c in calls if c["contract_ok"]),
        "parse_rate": round(sum(1 for c in calls if c["parse_ok"]) / n, 3) if n else None,
        "contract_rate": round(sum(1 for c in calls if c["contract_ok"]) / n, 3) if n else None,
        "first_attempt_accepted": sum(1 for a in accepted if a.attempt == 1),
        "reasons": dict(sorted(reasons.items())),
        "latency_s": ({"min": latencies[0], "median": round(statistics.median(latencies), 1),
                       "max": latencies[-1], "total": round(sum(latencies), 1)}
                      if latencies else None),
        "usage": ({"prompt_tokens": sum(int(u.get("prompt_tokens") or 0) for u in usage),
                   "completion_tokens": sum(int(u.get("completion_tokens") or 0)
                                            for u in usage),
                   "calls_with_usage": len(usage)} if usage else None),
    }


# -------------------------------------------------------------------- run


def _quota_from_config(view: RunView) -> QuotaFn:
    cfg = view.config.get("quota") or {"plugin": "always"}
    ctx = longrun.PluginContext(config_dir=view.run_dir)
    return longrun.plugin("quota", str(cfg.get("plugin", "always")))(cfg, ctx)


def _default_out(run_id: str, now: datetime) -> Path:
    return (Path.home() / ".local" / "state" / "trex-theory" / "reflect"
            / f"{run_id}-reflect-{now.strftime('%Y%m%dT%H%M%SZ')}.json")


def _sibling(out: Path, suffix: str) -> Path:
    return out.with_name(out.name.removesuffix(".json") + suffix)


def run_reflect(run_dir: Path, *, cutoff: str | None = None, k: int = 6,
                out: Path | None = None, table: Path | None = None,
                provider: str = DEFAULT_PROVIDER, transport: PostTransport | None = None,
                concurrency: int = 2, samples_per_arm: int = DEFAULT_SAMPLES,
                max_pack_tokens: int = DEFAULT_MAX_PACK_TOKENS,
                max_attempts: int = DEFAULT_ATTEMPTS, timeout: float = DEFAULT_TIMEOUT_S,
                seed: int = 20260929, only: Sequence[str] | None = None,
                dry_run: bool = False, quota_ok: QuotaFn | None = None,
                clock: Callable[[], datetime] = _utcnow,
                monotonic: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    """Dossiers from the run's train split, then (unless ``dry_run``) the
    panel and the config fragment. The run dir is only ever read."""
    run_dir = Path(run_dir)
    config = json.loads((run_dir / "config.json").read_text(encoding="utf-8"))
    cut = cutoff or (config.get("protocol") or {}).get("cutoff")
    if not cut:
        raise ValueError("no cutoff: pass --cutoff or set protocol.cutoff in the run config")
    personas(k)  # validates k before any work
    view = load_run(run_dir, str(cut))
    if not view.boards:
        raise ValueError(f"no train boards at or before {cut}")
    table_path = table or (config.get("outcome") or {}).get("table")
    if not table_path:
        raise ValueError("no outcome table: pass --table or set outcome.table in the run config")
    outcome_table = load_outcome_table(Path(str(table_path)).expanduser(), set(view.boards))
    pack, meta = build_pack(view, outcome_table, samples_per_arm=samples_per_arm,
                            max_tokens=max_pack_tokens, seed=seed, only=only)
    now = clock()
    out = out or _default_out(view.run_dir.name, now)
    call_tokens = estimate_tokens(theorist_messages(pack, PERSONAS[0])[0]["content"])
    meta.update(table=str(table_path), provider=provider, k=k, built_at=now.isoformat(),
                call_prompt_tokens_est=call_tokens, panel_prompt_tokens_est=call_tokens * k)
    out.parent.mkdir(parents=True, exist_ok=True)
    dossier_path = _sibling(out, ".dossiers.json")
    dossier_path.write_text(json.dumps({"meta": meta, "pack": pack}, indent=1),
                            encoding="utf-8")
    summary: dict[str, Any] = {
        "status": "dry_run" if dry_run else "pending", "dossiers": str(dossier_path),
        "cutoff": meta["cutoff"], "train_sessions": pack["split"]["train_sessions"],
        "train_boards": pack["split"]["train_boards"],
        "decided": {d["policy"]: d["aggregate"]["decided"] for d in pack["dossiers"]},
        "samples": {d["policy"]: len(d["samples"]) for d in pack["dossiers"]},
        "tokens_est": {"pack": meta["pack_tokens_est"], "per_call": call_tokens,
                       "panel_first_attempts": call_tokens * k,
                       "dossiers": meta["dossier_tokens_est"],
                       "universe": meta["universe_tokens_est"],
                       "trimmed_samples": meta["trimmed_samples"]}}
    if dry_run:
        return summary
    ok, why = (quota_ok or _quota_from_config(view))()
    summary["quota"] = {"ok": bool(ok), "reason": str(why)[:120]}
    if not ok:
        summary["status"] = "quota_refused"
        return summary
    transcript = _sibling(out, ".transcript.jsonl")
    write_lock = threading.Lock()

    def sink(record: dict[str, Any]) -> None:
        with write_lock, transcript.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, default=str) + "\n")

    existing = {p.name: p.prompt or lab.POLICY_SENTENCE for p in view.policies}
    accepted, calls = run_panel(pack, k=k, provider=provider, transport=transport,
                                concurrency=concurrency, max_attempts=max_attempts,
                                timeout=timeout, tickers=outcome_table.tickers,
                                existing=existing, sink=sink, monotonic=monotonic, clock=clock)
    stats = call_stats(calls, k, accepted)
    models = sorted({c["model"] for c in calls if c.get("model")})
    fragment = {
        "schema": FRAGMENT_SCHEMA,
        "untrusted_note": ("UNTRUSTED / NEVER PROMOTED. Model-proposed policy sentences from "
                           "a hindsight reflection on the TRAIN split only; each is a "
                           "hypothesis for the next long run's held-out walk-forward."),
        "policies": [{"name": a.name, "kind": "model", "prompt": a.proposal.prompt}
                     for a in accepted],
        "provenance": {
            "run_id": view.run_dir.name, "run_dir": str(view.run_dir),
            "cutoff": meta["cutoff"],
            "split": ("train: boards with session <= cutoff; outcomes counted only when "
                      "realized (exit session) by the cutoff"),
            "train_sessions": pack["split"]["train_sessions"],
            "train_boards": pack["split"]["train_boards"],
            "dossier_policies": [d["policy"] for d in pack["dossiers"]],
            "dossiers": str(dossier_path), "dossier_sha256": meta["pack_sha256"],
            "transcript": str(transcript), "provider": provider, "models": models,
            "at": clock().isoformat(), "k": k,
            "per_policy": {a.name: {"theorist": a.persona, "slot": a.slot,
                                    "attempt": a.attempt, "model": a.model,
                                    "hypothesis": a.proposal.hypothesis,
                                    "evidence": a.proposal.evidence,
                                    "expected_effect": a.proposal.expected_effect,
                                    "label": a.proposal.name,
                                    "prompt_sha256": hashlib.sha256(
                                        a.proposal.prompt.encode()).hexdigest()}
                           for a in accepted}},
        "stats": stats}
    out.write_text(json.dumps(fragment, indent=2), encoding="utf-8")
    summary.update(status="ok" if accepted else "no_proposals", out=str(out),
                   transcript=str(transcript), stats=stats,
                   policies=fragment["policies"])
    return summary


# -------------------------------------------------------------------- CLI


def register_cli(commands: Any) -> None:
    """``desk longrun reflect`` on the long-run subcommand parsers."""
    parser = commands.add_parser(
        "reflect", help="GEPA reflection on a run's TRAIN split: dossiers, a panel of "
                        "theorists and a config fragment of proposed policies (never promotes)")
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--cutoff", help="train = sessions <= this ISO date "
                                         "(default: the run's protocol.cutoff)")
    parser.add_argument("--k", type=int, default=6, help=f"theorists (1..{MAX_K})")
    parser.add_argument("--out", type=Path, help="the fragment JSON (default "
                                                 "~/.local/state/trex-theory/reflect/...)")
    parser.add_argument("--table", type=Path, help="outcome table (default: the run config's)")
    parser.add_argument("--provider", default=DEFAULT_PROVIDER)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--samples-per-arm", type=int, default=DEFAULT_SAMPLES)
    parser.add_argument("--max-pack-tokens", type=int, default=DEFAULT_MAX_PACK_TOKENS)
    parser.add_argument("--max-attempts", type=int, default=DEFAULT_ATTEMPTS)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S)
    parser.add_argument("--seed", type=int, default=20260929)
    parser.add_argument("--policies", help="comma list of model policies (default: all)")
    parser.add_argument("--dry-run", action="store_true",
                        help="build the dossiers and print token estimates; no model call")


def dispatch_cli(args: argparse.Namespace) -> int:
    only = [p.strip() for p in args.policies.split(",") if p.strip()] if args.policies else None
    try:
        summary = run_reflect(args.run_dir, cutoff=args.cutoff, k=args.k, out=args.out,
                              table=args.table, provider=args.provider,
                              concurrency=args.concurrency,
                              samples_per_arm=args.samples_per_arm,
                              max_pack_tokens=args.max_pack_tokens,
                              max_attempts=args.max_attempts, timeout=args.timeout,
                              seed=args.seed, only=only, dry_run=args.dry_run)
    except (ValueError, OSError, KeyError, TypeError) as error:
        print(f"longrun reflect: refused: {error}", file=sys.stderr)
        return 2
    print(json.dumps(summary, indent=2))
    return 0 if summary["status"] in ("ok", "dry_run") else 3
