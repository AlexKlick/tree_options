"""The desk lab's LONG RUN: a paired, resumable, quota-aware evaluation of
many trading policies over every decision board of a historical bundle.

Protocol (the statistics investigation of 2026-09-28,
``~/.local/state/trex-investigation/statistics``):

- PAIRED: every arm (policy x repeat) decides on the SAME boards; a board any
  arm lacks an ok receipt for is excluded from ALL arms when scoring.
- TWO-LEG PACKAGES (rule arms only): a rule's choice may name two board rows
  as one position, ``"idA+idB"`` (the beta-neutral short-vol trade the theory
  lane wanted: put_credit + call_credit on one underlying). Both legs pay
  their own round-trip cost, a no-fill on either leg is unevaluable, and the
  package resolves at the LATER leg's exit; the random null and the controls
  stay single-row (see the digest's ``pair_arms`` protocol note).
- CONTROLS on those boards: ``no_trade``, ``first_row``, the four
  fixed-structure rules, an ``always_bullish`` regime baseline, and a
  ``random`` picker at the incumbent's entry rate (an exact null over the
  same boards and the same outcome table, >= 200 seeds).
- A/A: the incumbent runs twice; when its two repeats differ significantly
  (the paired session-bootstrap 95% CI excludes 0) the whole evaluation is
  flagged INVALID - the harness noise alone would "find" differences.
- UNCERTAINTY: the resampling unit is the SESSION (boards within a session
  share a regime); every total is reported with its 95% bootstrap CI, never
  as a bare point total.
- PAIRED DIFFS per arm: vs the random-null expectation, vs ``first_row``, vs
  the incumbent and vs the ``always_bullish`` regime baseline (session-level,
  bootstrap CI + one-sided sign-flip p).
- WALK-FORWARD: candidates are ranked on sessions <= a cutoff by a metric
  declared before scoring; at most TWO finalists are tested ONCE on the
  sessions after it, Holm-adjusted.
- STABILITY: drop-one-session and half-split sign agreement.
- BENCHMARKS: buy-and-hold series in dollars on the same capital (5000) over
  the same window.

Inputs are INJECTED (the board/outcome engine is owned elsewhere):
``boards`` (list of :class:`Board`), ``outcome(snapshot, candidate_id,
horizon) -> {"gross", "net"} | None``, ``ask(policy_spec, board) -> (choice,
horizon, note)``, ``quota_ok() -> (ok, reason)`` and ``benchmarks`` (name ->
{ISO date: close}). :func:`register_plugin` is the seam a config names them
through (the ``v1`` plug-ins ship here; ``v2`` registers at integration).

NOTHING HERE PROMOTES. Model notes are untrusted prose. The pre-registered
rule is text for the operator; a mechanical check marks at most "eligible
for operator review". The digest carries ``promoted: false`` by
construction and the cockpit refuses to serve a digest that claims otherwise.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import math
import os
import random
import re
import sys
import threading
import time
import urllib.request
from collections.abc import Callable, Iterator, Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import numpy as np

PLAN_SCHEMA = "desk-longrun-plan/1"
RECEIPT_SCHEMA = "desk-longrun-receipt/1"
PROGRESS_SCHEMA = "desk-longrun-progress/1"
DIGEST_SCHEMA = "desk-longrun-digest/1"
VIEW_SCHEMA = "desk-longrun-view/1"
#: the cockpit's override: a run dir, or a root of run dirs
DIR_ENV = "TREX_DESK_LONGRUN_DIR"
STOP_FILE = "STOP"
CAPITAL = 5000.0
MAX_FINALISTS = 2
MIN_RANDOM_SEEDS = 200
EXACT_SIGN_FLIP_MAX = 16
KINDS = ("model", "rule", "control")
STRUCTURES = ("put_credit", "call_debit", "put_debit", "call_credit")
BULLISH_STRUCTURES = frozenset({"put_credit", "call_debit"})
RANDOM = "random"
FIRST_ROW = "first_row"
#: the regime baseline: a policy that is merely net-bullish in a rising window
#: looks skilled against random; the paired diff vs this arm separates the two
REGIME = "always_bullish"
METRICS = ("ci_low_diff_vs_random", "diff_vs_random", "total")
ROW_KEYS = ("board_order", "max_reward_risk", "min_max_loss")
BROKER_URL = "http://127.0.0.1:8019/status"

UNTRUSTED_NOTE = (
    "UNTRUSTED / NEVER PROMOTED. Model choices and notes are untrusted prose; "
    "every figure here is a mechanical outcome proxy from the injected outcome "
    "plug-in (historical valuation proxies, not executable fills). Nothing in "
    "this digest is promoted and this harness has no promotion path: the "
    "pre-registered rule below is text for the operator, checked mechanically "
    "at most to mark a policy eligible for operator review.")

PREREGISTERED_RULE = (
    "Pre-registered in plan.json before any scoring (desk long-run protocol "
    "v1). A challenger policy is ELIGIBLE FOR OPERATOR REVIEW - never promoted "
    "by code - only if ALL hold: (1) the A/A check is valid: the incumbent's "
    "two repeats on the same boards have a paired session-bootstrap 95% CI "
    "that contains 0; (2) it is one of at most 2 finalists chosen ONLY on the "
    "tune sessions (<= the cutoff) by the metric declared in the plan, and it "
    "was tested once on the test sessions (> the cutoff); (3) on the test "
    "sessions its paired diff vs the random-null expectation at the "
    "incumbent's entry rate has a Holm-adjusted one-sided sign-flip p < alpha "
    "AND a 95% CI lower bound > 0; (4) on the test sessions its paired diff "
    "vs the incumbent has a 95% CI lower bound > 0; (5) stability on the test "
    "sessions: the two halves agree in sign and no single dropped session "
    "flips the sign of the diff vs random. Promotion itself stays the "
    "operator's decision.")

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,47}$")

Choice = tuple[str | None, str | None]
RuleFn = Callable[["Board"], Choice]
#: a rule arm may name TWO board rows as one package: ``"idA+idB"`` (exactly
#: two DISTINCT ids, '+'-joined, no whitespace; rule arms ONLY - the model
#: parsers reject a pair, and this module keeps that contract: one row per
#: model reply)
PAIR_SEP = "+"
#: (PolicySpec, Board[, Arm when ``wants_arm``]) -> (choice, horizon, note[, receipt extras])
AskFn = Callable[..., tuple[Any, ...]]
OutcomeFn = Callable[[str, str, str | None], Mapping[str, Any] | None]
QuotaFn = Callable[[], tuple[bool, str]]
Clock = Callable[[], datetime]


def _utcnow() -> datetime:
    return datetime.now(UTC)


def pair_legs(choice: Any) -> list[str] | None:
    """The two legs of a ``"idA+idB"`` pair choice (exactly two non-empty
    '+'-joined parts); None when the choice is not of that shape. Board
    membership and distinctness are the validator's job (``_validated``)."""
    if not isinstance(choice, str):
        return None
    parts = choice.split(PAIR_SEP)
    return parts if len(parts) == 2 and all(parts) else None


def _later_exit(a: str | None, b: str | None) -> str | None:
    """The later of two exit instants (a pair resolves when its last leg
    does). None-safe; compared as instants when both parse, else lexically."""
    if a is None:
        return b
    if b is None:
        return a
    try:
        return a if datetime.fromisoformat(a) >= datetime.fromisoformat(b) else b
    except ValueError:
        return max(a, b)


# ------------------------------------------------------------------ inputs


@dataclass(frozen=True)
class Board:
    """One as-of decision board every arm sees (injected, engine-agnostic)."""

    snapshot: str
    session: str
    clock: str
    rows: list[dict[str, Any]]
    context: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if not self.snapshot:
            raise ValueError("board snapshot id required")
        date.fromisoformat(self.session)  # an ISO session date, or ValueError
        if not self.rows:
            raise ValueError(f"{self.snapshot}: a board needs at least one row")
        ids = [row.get("id") for row in self.rows]
        if any(not isinstance(i, str) or not i for i in ids):
            raise ValueError(f"{self.snapshot}: every row needs a string id")
        if len(set(ids)) != len(ids):
            raise ValueError(f"{self.snapshot}: duplicate row ids")

    @property
    def ids(self) -> list[str]:
        return [str(row["id"]) for row in self.rows]


@dataclass(frozen=True)
class PolicySpec:
    """A policy under evaluation. ``random`` is the scoring-time null control
    (never executed); every other rule/control carries a ``rule``."""

    name: str
    kind: str
    repeats: int = 1
    provider: str | None = None
    prompt: str | None = None
    rule: RuleFn | None = None

    def __post_init__(self) -> None:
        if not _NAME_RE.fullmatch(self.name):
            raise ValueError(f"invalid policy name {self.name!r}")
        if self.kind not in KINDS:
            raise ValueError(f"{self.name}: kind must be one of {KINDS}")
        if not 1 <= self.repeats <= 8:
            raise ValueError(f"{self.name}: repeats must be 1..8")
        if self.name == RANDOM:
            if self.kind != "control" or self.rule is not None or self.repeats != 1:
                raise ValueError("random is the scoring-time null control")
        elif self.kind == "model":
            if self.rule is not None:
                raise ValueError(f"{self.name}: a model policy has no rule")
        elif self.rule is None:
            raise ValueError(f"{self.name}: a {self.kind} policy needs a rule")

    @property
    def executed(self) -> bool:
        return self.name != RANDOM

    def arm_names(self) -> list[str]:
        if self.repeats == 1:
            return [self.name]
        return [f"{self.name}#{i}" for i in range(1, self.repeats + 1)]


@dataclass(frozen=True)
class Arm:
    """One executed series: a policy's ``repeat`` (1-based)."""

    name: str
    policy: PolicySpec
    repeat: int


def arms_of(policies: Sequence[PolicySpec]) -> list[Arm]:
    seen: set[str] = set()
    arms: list[Arm] = []
    for policy in policies:
        if policy.name in seen:
            raise ValueError(f"duplicate policy {policy.name!r}")
        seen.add(policy.name)
        if policy.executed:
            arms.extend(Arm(name, policy, i)
                        for i, name in enumerate(policy.arm_names(), start=1))
    return arms


# --------------------------------------------------------- built-in rules


def rule_no_trade(board: Board) -> Choice:
    return None, None


def rule_first_row(horizon: str | None = None) -> RuleFn:
    def rule(board: Board) -> Choice:
        return board.ids[0], horizon
    return rule


def _num(row: Mapping[str, Any], key: str) -> float | None:
    try:
        value = float(row[key])
    except (KeyError, TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _order_key(key: str) -> Callable[[tuple[int, dict[str, Any]]], tuple[float, int]]:
    """Deterministic best-row keys; missing fields sort last, board order breaks ties."""
    if key not in ROW_KEYS:
        raise ValueError(f"row key must be one of {ROW_KEYS}")

    def board_order(item: tuple[int, dict[str, Any]]) -> tuple[float, int]:
        return 0.0, item[0]

    def max_reward_risk(item: tuple[int, dict[str, Any]]) -> tuple[float, int]:
        value = _num(item[1], "reward_risk")
        return (-value if value is not None else math.inf), item[0]

    def min_max_loss(item: tuple[int, dict[str, Any]]) -> tuple[float, int]:
        value = _num(item[1], "max_loss")
        return (value if value is not None else math.inf), item[0]

    return {"board_order": board_order, "max_reward_risk": max_reward_risk,
            "min_max_loss": min_max_loss}[key]


def _best(board: Board, keep: Callable[[dict[str, Any]], bool], key: str) -> str | None:
    eligible = [(i, row) for i, row in enumerate(board.rows) if keep(row)]
    if not eligible:
        return None
    return str(min(eligible, key=_order_key(key))[1]["id"])


def rule_fixed_structure(structure: str, horizon: str | None = None,
                         key: str = "board_order") -> RuleFn:
    """Always the best-by-``key`` row of ``structure`` (skip when absent)."""
    if structure not in STRUCTURES:
        raise ValueError(f"structure must be one of {STRUCTURES}")
    _order_key(key)

    def rule(board: Board) -> Choice:
        choice = _best(board, lambda row: row.get("structure") == structure, key)
        return choice, (horizon if choice is not None else None)
    return rule


def is_bullish(row: Mapping[str, Any]) -> bool:
    """A row's ``direction`` when given, else its structure's direction."""
    direction = row.get("direction")
    if direction is not None:
        return str(direction).lower() == "bullish"
    return row.get("structure") in BULLISH_STRUCTURES


def rule_always_bullish(horizon: str | None = None, key: str = "board_order") -> RuleFn:
    """The regime baseline: always the best bullish row."""
    _order_key(key)

    def rule(board: Board) -> Choice:
        choice = _best(board, is_bullish, key)
        return choice, (horizon if choice is not None else None)
    return rule


def builtin_controls(horizon: str | None = None) -> list[PolicySpec]:
    """The standard control set every long run carries on the same boards."""
    return [
        PolicySpec("no_trade", "control", rule=rule_no_trade),
        PolicySpec(FIRST_ROW, "control", rule=rule_first_row(horizon)),
        *(PolicySpec(f"always_{s}", "rule", rule=rule_fixed_structure(s, horizon))
          for s in STRUCTURES),
        PolicySpec(REGIME, "control", rule=rule_always_bullish(horizon)),
        PolicySpec(RANDOM, "control"),
    ]


# ---------------------------------------------------------------- protocol


@dataclass(frozen=True)
class Protocol:
    """The scoring parameters, frozen in plan.json at the run's first start."""

    capital: float = CAPITAL
    draws: int = 10_000
    seed: int = 20260928
    random_seeds: int = 1000
    random_horizons: tuple[str | None, ...] | None = None
    incumbent: str | None = None
    cutoff: str | None = None
    metric: str = "ci_low_diff_vs_random"
    max_finalists: int = MAX_FINALISTS
    alpha: float = 0.05
    #: the test split counts decisions entered >= this many sessions after the cutoff
    #: session (desk.purge); 1 = the first session after it (the pre-embargo split)
    embargo_sessions: int = 1

    def __post_init__(self) -> None:
        if not 1 <= self.embargo_sessions <= 60:
            raise ValueError("embargo_sessions must be 1..60")
        if not self.capital > 0:
            raise ValueError("capital must be positive")
        if self.draws < 1000:
            raise ValueError("bootstrap draws must be >= 1000")
        if self.random_seeds < MIN_RANDOM_SEEDS:
            raise ValueError(f"the random null needs >= {MIN_RANDOM_SEEDS} seeds")
        if self.metric not in METRICS:
            raise ValueError(f"metric must be one of {METRICS}")
        if not 1 <= self.max_finalists <= MAX_FINALISTS:
            raise ValueError(f"max_finalists must be 1..{MAX_FINALISTS}")
        if not 0 < self.alpha < 0.5:
            raise ValueError("alpha must be in (0, 0.5)")
        if self.cutoff is not None:
            date.fromisoformat(self.cutoff)

    def to_json(self) -> dict[str, Any]:
        doc = asdict(self)
        if self.random_horizons is not None:
            doc["random_horizons"] = list(self.random_horizons)
        if self.embargo_sessions == 1:  # the default keeps older plan.json files resumable
            doc.pop("embargo_sessions")
        return doc


@dataclass(frozen=True)
class ExecSettings:
    concurrency: int = 8
    seed: int = 20260928
    quota_every: int = 20
    pause_s: float = 300.0
    max_pause_s: float | None = None
    retry_failed: bool = True
    progress_every: int = 10
    failure_backoff_after: int = 25

    def __post_init__(self) -> None:
        if not 1 <= self.concurrency <= 64:
            raise ValueError("concurrency must be 1..64")
        if self.quota_every < 1 or self.progress_every < 1 or self.failure_backoff_after < 1:
            raise ValueError("quota_every, progress_every, failure_backoff_after must be >= 1")
        if not self.pause_s > 0:
            raise ValueError("pause_s must be positive")


# ---------------------------------------------------------------- receipts


def receipts_path(run_dir: Path, arm: str) -> Path:
    return run_dir / "receipts" / (arm.replace("#", "--r") + ".jsonl")


def load_receipts(path: Path) -> dict[str, dict[str, Any]]:
    """The latest receipt per snapshot (an ok receipt is never displaced by a
    later failure); a torn line from a killed process is skipped."""
    latest: dict[str, dict[str, Any]] = {}
    if not path.is_file():
        return latest
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(rec, dict) or not isinstance(rec.get("snapshot"), str):
                continue
            prior = latest.get(rec["snapshot"])
            if rec.get("ok") or prior is None or not prior.get("ok"):
                latest[rec["snapshot"]] = rec
    return latest


def _heal_torn_tail(path: Path) -> None:
    """End a receipts file with a newline, so the next append never fuses
    onto the fragment a killed process left behind."""
    if not path.is_file() or path.stat().st_size == 0:
        return
    with path.open("rb") as stream:
        stream.seek(-1, os.SEEK_END)
        last = stream.read(1)
    if last != b"\n":
        with path.open("a", encoding="utf-8") as stream:
            stream.write("\n")


def _write_json(path: Path, doc: Mapping[str, Any]) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(doc, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, path)


def _validated(board: Board, choice: Any, horizon: Any, note: Any) -> dict[str, Any]:
    """The receipt fields of one decision; an unknown row id is never trusted.

    A choice is either one board row id, or a PAIR ``"idA+idB"``: exactly two
    DISTINCT ids, both on the board, '+'-joined (a whole choice that matches a
    row id wins first, so an id containing ``+`` keeps its single-row meaning).
    A pair receipt records the choice verbatim plus ``legs`` and both ``rows``
    (``row`` stays None: there is no one row)."""
    ids = board.ids
    out: dict[str, Any] = {"note": str(note or "")[:80]}
    if choice is not None and str(choice) not in ids:
        legs = pair_legs(choice)
        if legs is None or legs[0] == legs[1] or any(leg not in ids for leg in legs):
            out["rejected_choice"] = str(choice)[:40]
            choice = None
    if choice is None:
        out.update(choice=None, horizon=None, row=None)
        return out
    horizon = None if horizon is None else str(horizon)[:40]
    legs = pair_legs(choice)
    if legs is None:
        out.update(choice=str(choice), horizon=horizon, row=ids.index(str(choice)))
    else:
        out.update(choice=str(choice), horizon=horizon, row=None,
                   rows=[ids.index(leg) for leg in legs], legs=legs)
    return out


def call_ask(ask: AskFn, arm: Arm, board: Board) -> tuple[Any, ...]:
    """An ask that sets ``wants_arm`` also gets the arm (e.g. its repeat
    seeds the forecaster's display permutation)."""
    if getattr(ask, "wants_arm", False):
        return tuple(ask(arm.policy, board, arm))
    return tuple(ask(arm.policy, board))


class PolicyAsk:
    """Per-policy ask overrides (config ``policies[].ask``); the rest use
    the default ask."""

    wants_arm = True

    def __init__(self, default: AskFn | None, overrides: Mapping[str, AskFn]) -> None:
        self.default, self.overrides = default, dict(overrides)

    def __call__(self, spec: PolicySpec, board: Board, arm: Arm) -> tuple[Any, ...]:
        ask = self.overrides.get(spec.name, self.default)
        if ask is None:
            raise RuntimeError(f"{spec.name}: no ask plug-in configured")
        return call_ask(ask, arm, board)


def decide(arm: Arm, board: Board, ask: AskFn | None,
           monotonic: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    """One arm's decision on one board as a receipt. Failures are recorded,
    never raised: a model outage must not end the run."""
    started = monotonic()
    rec: dict[str, Any] = {"schema": RECEIPT_SCHEMA, "arm": arm.name,
                           "policy": arm.policy.name, "repeat": arm.repeat,
                           "kind": arm.policy.kind, "snapshot": board.snapshot,
                           "session": board.session, "board_rows": len(board.rows)}
    try:
        extra: Any = None
        if arm.policy.kind == "model":
            if ask is None:
                raise RuntimeError("no ask plug-in configured")
            reply = call_ask(ask, arm, board)
            choice, horizon, note = reply[:3]
            extra = reply[3] if len(reply) > 3 else None
        else:
            if arm.policy.rule is None:
                raise RuntimeError("rule missing")
            choice, horizon = arm.policy.rule(board)
            note = ""
        rec.update(_validated(board, choice, horizon, note), ok=True)
        for key, value in dict(extra or {}).items():
            rec.setdefault(key, value)  # extra receipt fields (raw forecasts); core keys never move
    except Exception as error:  # recorded, the run continues
        rec.update(ok=False, choice=None, horizon=None, row=None, note="",
                   error=f"{type(error).__name__}: {str(error)[:160]}")
    rec["latency_s"] = round(monotonic() - started, 3)
    return rec


class OutcomeCache:
    """Memoized outcome lookups -> (gross, net) or None (no fill/not evaluable).

    A PAIR candidate ``"idA+idB"`` (rule arms; :func:`pair_legs`) resolves
    through the SAME outcome fn: each leg at the same horizon, gross and net
    summed (each leg's net already carries its own round-trip cost, so both
    legs pay by construction), a no-fill on EITHER leg -> None (unevaluable,
    exactly like a single no-fill), and ``exit_at`` the LATER of the legs'
    exits (a package resolves when its last leg does; desk.purge stays
    correct)."""

    def __init__(self, outcome: OutcomeFn) -> None:
        self._outcome = outcome
        self._memo: dict[tuple[str, str, str | None], tuple[float, float] | None] = {}
        self._exits: dict[tuple[str, str, str | None], str | None] = {}
        self._lock = threading.Lock()

    def get(self, snapshot: str, candidate: str, horizon: str | None) -> tuple[float, float] | None:
        legs = pair_legs(candidate)
        if legs is None:
            return self._single(snapshot, candidate, horizon)
        key = (snapshot, candidate, horizon)
        with self._lock:
            if key in self._memo:
                return self._memo[key]
        first = self._single(snapshot, legs[0], horizon)
        second = self._single(snapshot, legs[1], horizon)
        value: tuple[float, float] | None = (
            None if first is None or second is None
            else (first[0] + second[0], first[1] + second[1]))
        with self._lock:
            self._memo[key] = value
            self._exits[key] = None if value is None else _later_exit(
                self._exits.get((snapshot, legs[0], horizon)),
                self._exits.get((snapshot, legs[1], horizon)))
        return value

    def _single(self, snapshot: str, candidate: str,
                horizon: str | None) -> tuple[float, float] | None:
        key = (snapshot, candidate, horizon)
        with self._lock:
            if key in self._memo:
                return self._memo[key]
        raw = self._outcome(snapshot, candidate, horizon)
        value: tuple[float, float] | None = None
        if raw is not None:
            gross, net = float(raw["gross"]), float(raw["net"])
            if not (math.isfinite(gross) and math.isfinite(net)):
                raise ValueError(f"non-finite outcome for {snapshot}/{candidate}")
            value = (gross, net)
        with self._lock:
            self._memo[key] = value
            self._exits[key] = None if raw is None or not raw.get("exit_at") \
                else str(raw["exit_at"])
        return value

    def net(self, snapshot: str, candidate: str, horizon: str | None) -> float:
        value = self.get(snapshot, candidate, horizon)
        return 0.0 if value is None else value[1]

    def exit_at(self, snapshot: str, candidate: str, horizon: str | None) -> str | None:
        """The outcome's exit instant when the plug-in reports one (desk.purge)."""
        self.get(snapshot, candidate, horizon)
        return self._exits.get((snapshot, candidate, horizon))


# ---------------------------------------------------------------- executor


class RunLocked(RuntimeError):
    """Another process holds this run dir."""


@contextlib.contextmanager
def _run_lock(run_dir: Path) -> Iterator[bool]:
    with open(run_dir / ".lock", "a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True


class _Executor:
    """Thread-pool executor. Workers only run ``decide``; the main thread owns
    receipts, running stats and progress.json (no shared mutable state)."""

    def __init__(self, run_dir: Path, boards: Sequence[Board], arms: Sequence[Arm], *,
                 ask: AskFn | None, quota_ok: QuotaFn, outcomes: OutcomeCache,
                 settings: ExecSettings, sleep: Callable[[float], None],
                 monotonic: Callable[[], float], clock: Clock) -> None:
        self.run_dir, self.boards, self.arms = run_dir, list(boards), list(arms)
        self.board_ids = {board.snapshot for board in self.boards}
        self.ask, self.quota_ok, self.outcomes = ask, quota_ok, outcomes
        self.settings, self.sleep, self.monotonic, self.clock = settings, sleep, monotonic, clock
        for arm in self.arms:
            _heal_torn_tail(receipts_path(run_dir, arm.name))
        self.receipts = {arm.name: load_receipts(receipts_path(run_dir, arm.name))
                         for arm in self.arms}
        prior = self._prior_progress()
        self.paused_before = float(prior.get("paused_s", 0.0) or 0.0)
        self.started = str(prior.get("started") or clock().isoformat())
        self.paused_process = 0.0
        self.quota: dict[str, Any] = {"ok": None, "reason": "not_checked", "checked_at": None}
        self.status = "running"
        self.digest: str | None = None
        self.model_calls = 0
        self.failed_attempts = 0
        self.consecutive_failures = 0
        self.outcome_errors = 0
        self.t0 = monotonic()
        self._since_progress = 0
        self._skill_memo: dict[str, Any] = {}  # desk.skill's live counterfactual cache

    def _prior_progress(self) -> dict[str, Any]:
        path = self.run_dir / "progress.json"
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return doc if isinstance(doc, dict) else {}

    # --------------------------------------------------------------- tasks

    def todo(self) -> tuple[list[tuple[Arm, Board]], list[tuple[Arm, Board]]]:
        rules: list[tuple[Arm, Board]] = []
        models: list[tuple[Arm, Board]] = []
        for arm in self.arms:
            done = self.receipts[arm.name]
            for board in self.boards:
                rec = done.get(board.snapshot)
                if rec is not None and (rec.get("ok") or not self.settings.retry_failed):
                    continue  # resume: a decided board is never asked again
                (models if arm.policy.kind == "model" else rules).append((arm, board))
        # interleave arms so repeats/policies progress together (paired coverage)
        random.Random(self.settings.seed).shuffle(models)
        return rules, models

    def record(self, rec: dict[str, Any]) -> None:
        rec["at"] = self.clock().isoformat()
        path = receipts_path(self.run_dir, rec["arm"])
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(rec, sort_keys=True) + "\n")
        prior = self.receipts[rec["arm"]].get(rec["snapshot"])
        if rec["ok"] or prior is None or not prior.get("ok"):
            self.receipts[rec["arm"]][rec["snapshot"]] = rec
        if rec["kind"] == "model":
            self.model_calls += 1
            self.consecutive_failures = 0 if rec["ok"] else self.consecutive_failures + 1
        if not rec["ok"]:
            self.failed_attempts += 1
        self._since_progress += 1
        if self._since_progress >= self.settings.progress_every:
            self.write_progress()

    def drain(self, pending: dict[Future[dict[str, Any]], tuple[Arm, Board]]) -> None:
        while pending:
            finished, _ = wait(list(pending), return_when=FIRST_COMPLETED)
            for future in finished:
                del pending[future]
                self.record(future.result())

    # --------------------------------------------------------------- quota

    def check_quota(self) -> bool:
        try:
            ok, why = self.quota_ok()
        except Exception as error:  # a broken meter must not stall the run
            ok, why = True, f"quota_check_error:{type(error).__name__}"
        self.quota = {"ok": bool(ok), "reason": str(why)[:120],
                      "checked_at": self.clock().isoformat()}
        return bool(ok)

    def _pause(self, pending: dict[Future[dict[str, Any]], tuple[Arm, Board]],
               status: str) -> bool:
        """Drain in-flight calls, record the pause, sleep one interval.
        False when the configured pause budget is spent (stop, resumable)."""
        self.drain(pending)
        self.status = status
        self.write_progress()
        limit = self.settings.max_pause_s
        if limit is not None and self.paused_process >= limit:
            return False
        self.sleep(self.settings.pause_s)
        self.paused_process += self.settings.pause_s
        return True

    def quota_gate(self, pending: dict[Future[dict[str, Any]], tuple[Arm, Board]]) -> bool:
        ok = self.check_quota()
        while not ok:
            if not self._pause(pending, "paused"):
                return False
            ok = self.check_quota()
        if self.status != "running":
            self.status = "running"
            self.write_progress()
        return True

    # ----------------------------------------------------------------- run

    def run(self) -> str:
        """Execute every missing (arm, board); '' when complete, else why stopped."""
        rules, models = self.todo()
        self.write_progress()
        for arm, board in rules:  # pure, instant, unmetered
            self.record(decide(arm, board, None, self.monotonic))
        pending: dict[Future[dict[str, Any]], tuple[Arm, Board]] = {}
        tasks = iter(models)
        task = next(tasks, None)
        submitted = 0
        stop = ""
        with ThreadPoolExecutor(max_workers=self.settings.concurrency) as pool:
            while True:
                while task is not None and not stop and len(pending) < self.settings.concurrency:
                    if (self.run_dir / STOP_FILE).exists():
                        stop = "stop_file"
                        break
                    if self.consecutive_failures >= self.settings.failure_backoff_after:
                        if not self._pause(pending, "backoff"):
                            stop = "failure_backoff_limit"
                            break
                        self.consecutive_failures = 0
                        self.status = "running"
                    if submitted % self.settings.quota_every == 0 and not self.quota_gate(pending):
                        stop = "quota_pause_limit"
                        break
                    arm, board = task
                    future = pool.submit(decide, arm, board, self.ask, self.monotonic)
                    pending[future] = (arm, board)
                    submitted += 1
                    task = next(tasks, None)
                if not pending:
                    break
                finished, _ = wait(list(pending), return_when=FIRST_COMPLETED)
                for future in finished:
                    del pending[future]
                    self.record(future.result())
        self.write_progress()
        return stop

    # ------------------------------------------------------------ progress

    def arm_stats(self, arm: Arm) -> dict[str, Any]:
        entered = failures = unevaluable = 0
        net = 0.0
        recs = [r for s, r in self.receipts[arm.name].items() if s in self.board_ids]
        for rec in recs:
            if not rec.get("ok"):
                failures += 1
                continue
            if rec.get("choice") is None:
                continue
            entered += 1
            try:
                value = self.outcomes.get(rec["snapshot"], rec["choice"], rec.get("horizon"))
            except Exception:  # the live view never kills the run
                self.outcome_errors += 1
                continue
            if value is None:
                unevaluable += 1
            else:
                net += value[1]
        reasons = failure_reasons(self.receipts[arm.name], self.boards)
        return {"policy": arm.policy.name, "repeat": arm.repeat, "kind": arm.policy.kind,
                "done": len(recs), "total": len(self.boards), "entered": entered,
                "failures": failures, "unevaluable": unevaluable, "net": round(net, 2),
                **({"failure_reasons": reasons} if reasons else {})}

    def write_progress(self) -> None:
        self._since_progress = 0
        arms = {arm.name: self.arm_stats(arm) for arm in self.arms}
        remaining_model = sum(
            1 for arm in self.arms if arm.policy.kind == "model" for board in self.boards
            if not self.receipts[arm.name].get(board.snapshot, {}).get("ok"))
        active = max(1e-9, self.monotonic() - self.t0 - self.paused_process)
        rate = self.model_calls / active
        eta = 0.0 if remaining_model == 0 else (remaining_model / rate if rate > 0 else None)
        doc = {"schema": PROGRESS_SCHEMA, "run_id": self.run_dir.name, "status": self.status,
               "at": self.clock().isoformat(), "started": self.started,
               "boards": len(self.boards),
               "sessions": len({board.session for board in self.boards}),
               "total": len(self.boards) * len(self.arms),
               "finished": sum(v["done"] for v in arms.values()),
               "failures": sum(v["failures"] for v in arms.values()),
               "failed_attempts_this_process": self.failed_attempts,
               "model_calls_this_process": self.model_calls,
               "outcome_errors": self.outcome_errors,
               "paused_s": round(self.paused_before + self.paused_process, 1),
               "quota": self.quota, "calls_per_s": round(rate, 4),
               "eta_s": None if eta is None else round(eta, 1),
               "arms": arms, "digest": self.digest}
        try:  # "skill significant yet?" (desk.skill); the live view never kills the run
            from tree_options.desk import skill

            doc["skill"] = skill.progress_skill(self.boards, self.arms, self.receipts,
                                                self.outcomes.get, self._skill_memo)
        except Exception as error:
            doc["skill"] = {"error": f"{type(error).__name__}: {str(error)[:120]}"}
        _write_json(self.run_dir / "progress.json", doc)


# -------------------------------------------------------------- statistics


def session_sums(board_sessions: Sequence[str], values: Sequence[float],
                 sessions: Sequence[str]) -> np.ndarray:
    """Per-session sums aligned to ``sessions`` (sessions without entries are 0)."""
    index = {s: i for i, s in enumerate(sessions)}
    out = np.zeros(len(sessions))
    for session, value in zip(board_sessions, values, strict=True):
        out[index[session]] += value
    return out


def bootstrap_ci(session_values: Sequence[float] | np.ndarray, *, draws: int,
                 seed: int) -> tuple[float, float]:
    """95% percentile CI of the SESSION-SUM, resampling sessions (fixed seed)."""
    arr = np.asarray(session_values, dtype=float)
    n = arr.size
    if n == 0:
        return 0.0, 0.0
    rng = np.random.default_rng(seed)
    sums = np.empty(draws)
    for start in range(0, draws, 1000):
        k = min(1000, draws - start)
        sums[start:start + k] = arr[rng.integers(0, n, size=(k, n))].sum(axis=1)
    lo, hi = np.percentile(sums, [2.5, 97.5])
    return round(float(lo), 2), round(float(hi), 2)


def sign_flip_p(diffs: Sequence[float] | np.ndarray, *, draws: int, seed: int) -> float:
    """One-sided (H1: the paired sum > 0) session sign-flip permutation p.
    Exact enumeration for <= 16 sessions, Monte Carlo (+1 corrected) above."""
    d = np.asarray(diffs, dtype=float)
    n = d.size
    if n == 0:
        return 1.0
    observed = float(d.sum()) - 1e-9
    if n <= EXACT_SIGN_FLIP_MAX:
        codes = np.arange(2 ** n)
        signs = ((codes[:, None] >> np.arange(n)) & 1) * 2 - 1
        return float(np.mean(signs @ d >= observed))
    rng = np.random.default_rng(seed)
    count = 0
    for start in range(0, draws, 1000):
        k = min(1000, draws - start)
        signs = rng.choice(np.array([-1.0, 1.0]), size=(k, n))
        count += int(np.sum(signs @ d >= observed))
    return (1 + count) / (draws + 1)


def paired(a: Sequence[float] | np.ndarray, b: Sequence[float] | np.ndarray, *,
           draws: int, seed: int) -> dict[str, Any]:
    """Session-level paired difference a - b: total, 95% CI, one-sided p."""
    d = np.asarray(a, dtype=float) - np.asarray(b, dtype=float)
    lo, hi = bootstrap_ci(d, draws=draws, seed=seed)
    return {"diff_total": round(float(d.sum()), 2), "ci95": [lo, hi],
            "p_one_sided": round(sign_flip_p(d, draws=draws, seed=seed + 1), 4),
            "sessions": int(d.size)}


def holm(pvalues: Mapping[str, float]) -> dict[str, float]:
    """Holm step-down adjusted p-values (monotone, capped at 1)."""
    ordered = sorted(pvalues.items(), key=lambda item: (item[1], item[0]))
    m = len(ordered)
    running = 0.0
    adjusted: dict[str, float] = {}
    for i, (name, p) in enumerate(ordered):
        running = max(running, min(1.0, (m - i) * p))
        adjusted[name] = running
    return adjusted


def stability(session_values: Sequence[float] | np.ndarray,
              sessions: Sequence[str]) -> dict[str, Any]:
    """Drop-one-session range/sign flips and half-split sign agreement."""
    arr = np.asarray(session_values, dtype=float)
    total = float(arr.sum())
    if arr.size == 0:
        return {"total": 0.0, "drop_one_min": 0.0, "drop_one_max": 0.0,
                "drop_one_sign_flips": 0, "most_influential_session": None,
                "half_split": {"first": 0.0, "second": 0.0, "split_after": None,
                               "signs_agree": False}}
    drops = total - arr
    flips = int(np.sum(np.sign(drops) != np.sign(total)))
    influence = arr * (1.0 if total >= 0 else -1.0)
    half = arr.size // 2
    first, second = float(arr[:half].sum()), float(arr[half:].sum())
    return {"total": round(total, 2), "drop_one_min": round(float(drops.min()), 2),
            "drop_one_max": round(float(drops.max()), 2), "drop_one_sign_flips": flips,
            "most_influential_session": sessions[int(np.argmax(influence))],
            "half_split": {"first": round(first, 2), "second": round(second, 2),
                           "split_after": sessions[half - 1] if half else None,
                           "signs_agree": (first > 0 and second > 0) or (first < 0 and second < 0)}}


@dataclass
class NullResult:
    p_enter: float
    seeds: int
    expected_sessions: np.ndarray
    totals: np.ndarray

    def to_json(self, *, draws: int, seed: int, matched_to: Sequence[str],
                horizons: Sequence[str | None]) -> dict[str, Any]:
        lo, hi = np.percentile(self.totals, [2.5, 97.5])
        return {"p_enter": round(self.p_enter, 4), "matched_to": list(matched_to),
                "horizons": list(horizons), "seeds": self.seeds,
                "expected_total": round(float(self.expected_sessions.sum()), 2),
                "expected_ci95": list(bootstrap_ci(self.expected_sessions, draws=draws,
                                                   seed=seed)),
                "simulated_mean_total": round(float(self.totals.mean()), 2),
                "band95": [round(float(lo), 2), round(float(hi), 2)]}


def random_null(board_sessions: Sequence[str], sessions: Sequence[str],
                options: Sequence[np.ndarray], p_enter: float, *, seeds: int,
                seed: int) -> NullResult:
    """The random picker on the SAME boards: enter with probability
    ``p_enter``, then a uniform (row, horizon) option. Exact per-session
    expectation plus ``seeds`` simulated totals for the null band."""
    if seeds < MIN_RANDOM_SEEDS:
        raise ValueError(f"the random null needs >= {MIN_RANDOM_SEEDS} seeds")
    p = min(1.0, max(0.0, p_enter))
    if not options:
        return NullResult(p, seeds, np.zeros(len(sessions)), np.zeros(seeds))
    if any(len(o) == 0 for o in options):
        raise ValueError("every board needs at least one (row, horizon) option")
    index = {s: i for i, s in enumerate(sessions)}
    s_idx = np.array([index[s] for s in board_sessions], dtype=int)
    sizes = np.array([len(o) for o in options], dtype=int)
    means = np.array([float(np.mean(o)) for o in options])
    expected = np.bincount(s_idx, weights=p * means, minlength=len(sessions))
    flat = np.concatenate(list(options))
    offsets = np.concatenate([[0], np.cumsum(sizes)[:-1]]).astype(int)
    totals = np.empty(seeds)
    for k in range(seeds):
        rng = np.random.default_rng([seed, k])
        enter = rng.random(len(options)) < p
        picks = np.minimum((rng.random(len(options)) * sizes).astype(int), sizes - 1)
        totals[k] = float(np.sum(np.where(enter, flat[offsets + picks], 0.0)))
    return NullResult(p, seeds, expected, totals)


def pick_null(realized: float, options: Sequence[np.ndarray], *, seeds: int,
              seed: int) -> dict[str, Any]:
    """Within-board pick skill (random_null.py): on the arm's OWN entered
    boards, a uniform row at the chosen horizon. Where does it sit?"""
    if not options:
        return {"entered_boards": 0, "expected": 0.0, "band95": [0.0, 0.0],
                "percentile": None, "p_one_sided_ge": None}
    sizes = np.array([len(o) for o in options], dtype=int)
    flat = np.concatenate(list(options))
    offsets = np.concatenate([[0], np.cumsum(sizes)[:-1]]).astype(int)
    sims = np.empty(seeds)
    for k in range(seeds):
        rng = np.random.default_rng([seed, 7, k])
        picks = np.minimum((rng.random(len(options)) * sizes).astype(int), sizes - 1)
        sims[k] = float(flat[offsets + picks].sum())
    lo, hi = np.percentile(sims, [2.5, 97.5])
    return {"entered_boards": len(options),
            "expected": round(float(sum(float(np.mean(o)) for o in options)), 2),
            "band95": [round(float(lo), 2), round(float(hi), 2)],
            "percentile": round(float(np.mean(sims < realized)), 4),
            "p_one_sided_ge": round(float(np.mean(sims >= realized)), 4)}


def benchmark_rows(benchmarks: Mapping[str, Mapping[Any, float]], sessions: Sequence[str], *,
                   capital: float, draws: int, seed: int) -> list[dict[str, Any]]:
    """Buy-and-hold in dollars on ``capital`` over the same sessions: bought
    at the last close before the first session (else that session's close),
    per-session PnL = shares x close change (sessions without a close: 0)."""
    rows: list[dict[str, Any]] = []
    if not sessions:
        return rows
    for name in sorted(benchmarks):
        closes = {str(d): float(v) for d, v in benchmarks[name].items()
                  if v is not None and math.isfinite(float(v)) and float(v) > 0}
        before = [d for d in closes if d < sessions[0]]
        base_date = max(before) if before else (sessions[0] if sessions[0] in closes else None)
        if base_date is None:
            rows.append({"name": name, "status": "unavailable",
                         "reason": "no close at or before the first session"})
            continue
        shares = capital / closes[base_date]
        prev = closes[base_date]
        per_session: list[float] = []
        missing = 0
        for session in sessions:
            close = closes.get(session)
            if close is None:
                per_session.append(0.0)
                missing += 1
                continue
            per_session.append(shares * (close - prev))
            prev = close
        lo, hi = bootstrap_ci(per_session, draws=draws, seed=seed)
        rows.append({"name": name, "status": "ok", "kind": "buy_and_hold",
                     "capital": capital, "base_date": base_date,
                     "base_close": closes[base_date], "end_close": prev,
                     "net_total": round(sum(per_session), 2), "net_ci95": [lo, hi],
                     "missing_sessions": missing})
    return rows


# ----------------------------------------------------------------- scoring


def _decisions(receipts: Mapping[str, Mapping[str, Any]],
               boards: Sequence[Board]) -> list[Choice]:
    out: list[Choice] = []
    for board in boards:
        rec = receipts.get(board.snapshot, {})
        out.append((rec.get("choice"), rec.get("horizon")) if rec.get("ok") else (None, None))
    return out


def _horizon_key(h: str | None) -> tuple[bool, str]:
    return h is not None, h or ""


def default_cutoff(sessions: Sequence[str]) -> str | None:
    """The declared default cutoff: the first two thirds of the sessions tune."""
    return sessions[max(0, (2 * len(sessions)) // 3 - 1)] if sessions else None


def walk_forward(pooled: Mapping[str, np.ndarray], expected: np.ndarray,
                 sessions: Sequence[str], *, incumbent: str | None, cutoff: str | None,
                 metric: str, max_finalists: int, draws: int, seed: int,
                 alpha: float, aa_valid: bool, embargo: int = 1) -> dict[str, Any]:
    """Rank candidates on tune sessions (<= cutoff) by the pre-declared metric,
    keep at most MAX_FINALISTS, test them ONCE on the sessions >= ``embargo``
    sessions after the cutoff session (score_run passes purged series)."""
    if not sessions:
        return {"status": "not_applicable", "reason": "no scored sessions"}
    cutoff = cutoff or default_cutoff(sessions)
    tune = np.array([s <= str(cutoff) for s in sessions])
    test = np.arange(len(sessions)) - (int(tune.sum()) - 1) >= max(1, embargo)
    test_sessions = [s for s, t in zip(sessions, test, strict=True) if t]
    base = {"cutoff": cutoff, "metric": metric,
            "tune_sessions": int(tune.sum()), "test_sessions": int(test.sum()),
            "embargo_sessions": max(1, embargo),
            "embargoed_sessions": int((~tune & ~test).sum())}
    if not tune.any() or not test.any() or not pooled:
        return {"status": "not_applicable", **base,
                "reason": "the cutoff leaves no tune or no test sessions, or no candidates"}
    cap = min(max_finalists, MAX_FINALISTS)
    ranking: list[dict[str, Any]] = []
    for name in sorted(pooled):
        series = pooled[name]
        diff = series[tune] - expected[tune]
        if metric == "total":
            value = float(series[tune].sum())
        elif metric == "diff_vs_random":
            value = float(diff.sum())
        else:
            value = bootstrap_ci(diff, draws=draws, seed=seed)[0]
        ranking.append({"policy": name, "metric_value": round(value, 2),
                        "tune_total": round(float(series[tune].sum()), 2),
                        "tune_diff_vs_random": round(float(diff.sum()), 2)})
    ranking.sort(key=lambda r: (-r["metric_value"], r["policy"]))
    finalists = ranking[:cap]
    results: list[dict[str, Any]] = []
    pvalues: dict[str, float] = {}
    for entry in finalists:
        name = entry["policy"]
        series, exp = pooled[name][test], expected[test]
        vs_random = paired(series, exp, draws=draws, seed=seed)
        vs_incumbent = (paired(series, pooled[incumbent][test], draws=draws, seed=seed)
                        if incumbent is not None and incumbent in pooled and name != incumbent
                        else None)
        lo, hi = bootstrap_ci(series, draws=draws, seed=seed)
        pvalues[name] = vs_random["p_one_sided"]
        results.append({"policy": name, "tune_metric": entry["metric_value"],
                        "test": {"net_total": round(float(series.sum()), 2),
                                 "net_ci95": [lo, hi], "vs_random": vs_random,
                                 "vs_incumbent": vs_incumbent,
                                 "stability": stability(series - exp, test_sessions)}})
    adjusted = holm(pvalues)
    for result in results:
        name = result["policy"]
        test_doc = result["test"]
        result["holm_p"] = round(adjusted[name], 4)
        stab = test_doc["stability"]
        checks: dict[str, bool | None] = {
            "aa_valid": aa_valid,
            "holm_p_below_alpha": adjusted[name] < alpha,
            "vs_random_ci_low_above_0": test_doc["vs_random"]["ci95"][0] > 0,
            "vs_incumbent_ci_low_above_0": (None if test_doc["vs_incumbent"] is None
                                            else test_doc["vs_incumbent"]["ci95"][0] > 0),
            "half_split_signs_agree": bool(stab["half_split"]["signs_agree"]),
            "no_drop_one_sign_flip": stab["drop_one_sign_flips"] == 0,
        }
        result["rule_check"] = checks
        result["eligible_for_operator_review"] = (
            name != incumbent and all(v is True for v in checks.values()))
    return {"status": "ok", **base, "max_finalists": cap, "ranking": ranking,
            "finalists": results,
            "note": ("finalists were chosen on the tune sessions only; the test "
                     "figures are the single confirmatory look")}


def failure_reasons(arm_receipts: Mapping[str, Mapping[str, Any]],
                    boards: Sequence[Board]) -> dict[str, int]:
    """Per-arm failure tally from the receipts on disk: truncated / timeout /
    http / other (an unknown reason buckets to other). Counts the same
    failed receipts as the standings ``failures`` column (the latest receipt
    per board snapshot), so the two never disagree."""
    counts: dict[str, int] = {}
    for board in boards:
        rec = arm_receipts.get(board.snapshot)
        if rec is None or rec.get("ok"):
            continue
        text = str(rec.get("error") or "")
        if "truncated at max_tokens" in text:
            reason = "truncated"
        elif "timeout" in text.lower() or "timed out" in text.lower():
            reason = "timeout"
        elif "HTTP " in text:
            reason = "http"
        else:
            reason = "other"
        counts[reason] = counts.get(reason, 0) + 1
    return counts


def heal_tally(arm: Arm, arm_receipts: Mapping[str, Mapping[str, Any]],
               boards: Sequence[Board]) -> dict[str, Any] | None:
    """Per-arm self-heal tally from the receipts on disk: how many of the
    arm's answered boards needed the truncation escalation, the timeout
    escalation or the fallback provider, and who answered (``provider``
    counts). ``None`` for a clean arm - no ``heals`` key anywhere.

    Provider attribution choice (documented, not guessed silently): receipts
    written before 2026-09-29 (the failover lane) carry no ``provider`` field;
    such a receipt is counted under the arm's policy provider when the policy
    names one, and when it does not (a model policy relying on the ask
    plug-in's default), ``providers`` is omitted for that arm rather than
    padded with a guess."""
    escalated = timeout_escalated = fallback = 0
    providers: dict[str, int] = {}
    unattributed = False
    for board in boards:
        rec = arm_receipts.get(board.snapshot)
        if rec is None:
            continue
        escalated += bool(rec.get("escalated"))
        timeout_escalated += bool(rec.get("timeout_escalated"))
        fallback += bool(rec.get("fallback"))
        who = rec.get("provider")
        if who is None and arm.policy.provider is not None:
            who = arm.policy.provider  # an old receipt: the policy's primary answered
        if who is None:
            unattributed = True
        else:
            providers[str(who)] = providers.get(str(who), 0) + 1
    if not (escalated or timeout_escalated or fallback):
        return None
    heals: dict[str, Any] = {"escalated": escalated, "timeout_escalated": timeout_escalated,
                             "fallback": fallback}
    if not unattributed:
        heals["providers"] = providers
    return heals


def score_run(boards: Sequence[Board], arms: Sequence[Arm],
              receipts: Mapping[str, Mapping[str, Mapping[str, Any]]],
              outcomes: OutcomeCache, protocol: Protocol, *,
              benchmarks: Mapping[str, Mapping[Any, float]] | None = None,
              receipts_files: Mapping[str, str] | None = None, run_id: str = "",
              plan_created: str | None = None, complete: bool = True,
              clock: Clock = _utcnow,
              skill_options: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The digest document. Pure over its inputs (fixed seeds throughout)."""
    draws, seed = protocol.draws, protocol.seed
    scored = [b for b in boards
              if all(receipts.get(a.name, {}).get(b.snapshot, {}).get("ok") for a in arms)]
    sessions = sorted({b.session for b in scored})
    board_sessions = [b.session for b in scored]
    per_arm: dict[str, dict[str, Any]] = {}
    for arm in arms:
        decisions = _decisions(receipts.get(arm.name, {}), scored)
        net: list[float] = []
        gross: list[float] = []
        entered = unevaluable = 0
        for board, (choice, horizon) in zip(scored, decisions, strict=True):
            value = None
            if choice is not None:
                entered += 1
                value = outcomes.get(board.snapshot, choice, horizon)
                unevaluable += value is None
            gross.append(0.0 if value is None else value[0])
            net.append(0.0 if value is None else value[1])
        mine = receipts.get(arm.name, {})
        per_arm[arm.name] = {
            "arm": arm, "decisions": decisions, "net": net, "entered": entered,
            "unevaluable": unevaluable,
            "sessions": session_sums(board_sessions, net, sessions),
            "gross_total": float(sum(gross)),
            "failures": sum(1 for b in boards if b.snapshot in mine
                            and not mine[b.snapshot].get("ok")),
            "excluded": sum(1 for b in boards if not mine.get(b.snapshot, {}).get("ok")),
            "failure_reasons": failure_reasons(mine, boards),
            "heals": heal_tally(arm, mine, boards)}

    incumbent_arms = [a.name for a in arms if a.policy.name == protocol.incumbent]
    model_arms = [a.name for a in arms if a.policy.kind == "model"]
    matched = incumbent_arms or model_arms
    if matched and scored:
        p_enter = float(np.mean([per_arm[n]["entered"] / len(scored) for n in matched]))
    else:
        p_enter, matched = 1.0, []
    horizons: list[str | None]
    if protocol.random_horizons is not None:
        horizons = list(protocol.random_horizons)
    else:
        used = {h for n in matched for c, h in per_arm[n]["decisions"] if c is not None}
        horizons = sorted(used, key=_horizon_key) or [None]
    options = [np.array([outcomes.net(b.snapshot, rid, h) for rid in b.ids for h in horizons])
               for b in scored]
    null = random_null(board_sessions, sessions, options, p_enter,
                       seeds=protocol.random_seeds, seed=seed)
    expected = null.expected_sessions

    first_row = next((a.name for a in arms if a.policy.name == FIRST_ROW), None)
    regime = next((a.name for a in arms if a.policy.name == REGIME), None)
    incumbent_ref = incumbent_arms[0] if incumbent_arms else None
    standings: list[dict[str, Any]] = []
    for arm in arms:
        data = per_arm[arm.name]
        series = data["sessions"]
        total = float(series.sum())
        evaluated = data["entered"] - data["unevaluable"]
        chosen_options: list[np.ndarray] = []
        realized = 0.0
        for board, (choice, horizon), value in zip(scored, data["decisions"], data["net"],
                                                   strict=True):
            if choice is not None:
                chosen_options.append(
                    np.array([outcomes.net(board.snapshot, rid, horizon) for rid in board.ids]))
                realized += value
        standings.append({
            "arm": arm.name, "policy": arm.policy.name, "repeat": arm.repeat,
            "kind": arm.policy.kind, "boards": len(scored), "entered": data["entered"],
            "entry_rate": round(data["entered"] / len(scored), 4) if scored else None,
            "unevaluable": data["unevaluable"], "failures": data["failures"],
            "excluded_boards": data["excluded"],
            "net_total": round(total, 2),
            "net_ci95": list(bootstrap_ci(series, draws=draws, seed=seed)),
            "gross_total": round(data["gross_total"], 2),
            "net_per_evaluated_entry": round(total / evaluated, 2) if evaluated else None,
            "vs_random": paired(series, expected, draws=draws, seed=seed),
            "vs_first_row": (paired(series, per_arm[first_row]["sessions"], draws=draws,
                                    seed=seed)
                             if first_row is not None and arm.name != first_row else None),
            "vs_incumbent": (paired(series, per_arm[incumbent_ref]["sessions"], draws=draws,
                                    seed=seed)
                             if incumbent_ref is not None and arm.name != incumbent_ref
                             else None),
            "vs_regime": (paired(series, per_arm[regime]["sessions"], draws=draws, seed=seed)
                          if regime is not None and arm.name != regime else None),
            "null_percentile": round(float(np.mean(null.totals < total)), 4),
            "pick_null": pick_null(realized, chosen_options, seeds=protocol.random_seeds,
                                   seed=seed),
            "stability_vs_random": stability(series - expected, sessions),
            "receipts": (receipts_files or {}).get(arm.name),
            **({"failure_reasons": data["failure_reasons"]}
               if data["failure_reasons"] else {}),  # additive: a clean arm shows none
            **({"heals": data["heals"]}
               if data["heals"] else {}),  # additive: a clean arm shows no heal tally
        })
    standings.sort(key=lambda r: (-r["vs_random"]["ci95"][0], r["arm"]))

    aa = aa_check(per_arm, incumbent_arms, draws=draws, seed=seed)
    # purged walk-forward (desk.purge): selection never sees post-cutoff prices
    from tree_options.desk import purge

    cutoff = protocol.cutoff or default_cutoff(sessions)
    window = sorted({b.session for b in boards})
    selection = {a.name: per_arm[a.name]["sessions"] for a in arms}
    sel_expected, purge_doc = expected, None
    if cutoff is not None:
        by_arm: dict[str, dict[str, int]] = {}
        for arm in arms:
            values, by_arm[arm.name] = purge.purge_decisions(
                scored, per_arm[arm.name]["decisions"], per_arm[arm.name]["net"], outcomes.get,
                outcomes.exit_at, cutoff, window)
            selection[arm.name] = session_sums(board_sessions, values, sessions)
        sel_expected, null_counts = purge.purged_null(scored, sessions, horizons, outcomes.get,
                                                      outcomes.exit_at, cutoff, null.p_enter,
                                                      window)
        purge_doc = {"rule": purge.RULE, "cutoff": cutoff, "by_arm": by_arm,
                     "random_null": null_counts,
                     "own_coverage": {a.name: purge.own_coverage(
                         boards, receipts.get(a.name, {}), outcomes.get, outcomes.exit_at,
                         cutoff, window) for a in arms}}
    pooled: dict[str, np.ndarray] = {}
    for policy_name in sorted({a.policy.name for a in arms
                               if a.policy.kind in ("model", "rule")}):
        members = [selection[a.name] for a in arms if a.policy.name == policy_name]
        pooled[policy_name] = np.mean(np.vstack(members), axis=0) if sessions \
            else np.zeros(0)
    wf = walk_forward(pooled, sel_expected, sessions, incumbent=protocol.incumbent,
                      cutoff=cutoff, metric=protocol.metric,
                      max_finalists=protocol.max_finalists, draws=draws, seed=seed,
                      alpha=protocol.alpha, aa_valid=bool(aa["valid"]),
                      embargo=protocol.embargo_sessions)
    if purge_doc is not None:
        wf["purge"] = purge_doc
    bench = benchmark_rows(benchmarks or {}, sessions, capital=protocol.capital,
                           draws=draws, seed=seed)
    eligible = [f["policy"] for f in wf.get("finalists", [])
                if f.get("eligible_for_operator_review")]
    headline = _headline(aa, wf, eligible, complete)
    try:  # exact counterfactual accounting (desk.skill): descriptive, never fatal
        from tree_options.desk import skill

        skill_doc = skill.skill_section(boards, arms, receipts, outcomes, protocol,
                                        options=skill_options)
    except Exception as error:
        skill_doc = {"status": "error", "error": f"{type(error).__name__}: {str(error)[:200]}"}
    return {
        "schema": DIGEST_SCHEMA,
        "untrusted_note": UNTRUSTED_NOTE,
        "promotion": {"promoted": False, "rule": PREREGISTERED_RULE,
                      "pre_registered_at": plan_created},
        "headline": headline,
        "evaluation_valid": bool(aa["valid"]),
        "complete": complete,
        "run_id": run_id,
        "at": clock().isoformat(),
        "protocol": {
            **protocol.to_json(),
            "resampling_unit": "session",
            "ci": "95% percentile bootstrap of the session sum (fixed seed)",
            "p_value": "one-sided session sign-flip permutation (exact <= 16 sessions)",
            "pairing": ("every arm is scored on the same boards: a board any arm lacks "
                        "an ok receipt for is excluded from all arms"),
            "unevaluable": "a chosen row with no outcome (no fill) scores 0, counted apart",
            "pair_arms": ("a rule arm may choose a two-leg package 'idA+idB' (both rows on "
                          "the board; gross/net summed over the legs, each leg pays its own "
                          "round-trip cost; a no-fill on either leg is unevaluable); the "
                          "random null and the controls stay single-row, so a pair arm is "
                          "scored against the SINGLE-ROW random null - a documented "
                          "comparison, never a silently mixed one"),
            "standings_scope": ("standings cover the whole window (descriptive); only "
                                "the walk-forward test section is confirmatory"),
        },
        "boards": {"total": len(boards), "scored": len(scored),
                   "excluded": len(boards) - len(scored),
                   "sessions": {"count": len(sessions),
                                "first": sessions[0] if sessions else None,
                                "last": sessions[-1] if sessions else None}},
        "aa": aa,
        "random_null": null.to_json(draws=draws, seed=seed, matched_to=matched,
                                    horizons=horizons),
        "standings": standings,
        "walk_forward": wf,
        "benchmarks": bench,
        "skill": skill_doc,
        "receipts": dict(receipts_files or {}),
    }


def aa_check(per_arm: Mapping[str, Mapping[str, Any]], incumbent_arms: Sequence[str], *,
             draws: int, seed: int) -> dict[str, Any]:
    """Incumbent repeat 1 vs repeat 2 on the same boards. A paired CI that
    excludes 0 means the harness noise alone looks like an edge: INVALID."""
    rule = "INVALID when the paired session-bootstrap 95% CI of the A/A diff excludes 0"
    if len(incumbent_arms) < 2:
        return {"status": "not_run", "valid": False, "rule": rule,
                "reason": "the incumbent needs repeats >= 2 on the same boards"}
    a, b = incumbent_arms[0], incumbent_arms[1]
    dec_a, dec_b = per_arm[a]["decisions"], per_arm[b]["decisions"]
    if not dec_a:
        return {"status": "not_run", "valid": False, "rule": rule, "pair": [a, b],
                "reason": "no scored boards for the A/A pair"}
    agreement = sum(1 for x, y in zip(dec_a, dec_b, strict=True) if x == y) / len(dec_a)
    diff = paired(per_arm[a]["sessions"], per_arm[b]["sessions"], draws=draws, seed=seed)
    lo, hi = diff["ci95"]
    significant = lo > 0 or hi < 0
    return {"status": "INVALID" if significant else "valid", "valid": not significant,
            "pair": [a, b], "boards": len(dec_a), "agreement": round(agreement, 4),
            "diff": diff, "significant": significant, "rule": rule}


def _headline(aa: Mapping[str, Any], wf: Mapping[str, Any], eligible: Sequence[str],
              complete: bool) -> str:
    prefix = "" if complete else "PARTIAL (scored from incomplete receipts): "
    if aa["status"] == "INVALID":
        return (prefix + "EVALUATION INVALID - the A/A pair differs significantly; no "
                "comparison in this digest is interpretable. Nothing promoted.")
    if aa["status"] != "valid":
        return (prefix + "UNVALIDATED - no A/A pair (incumbent repeats < 2); treat every "
                "comparison as unvalidated. Nothing promoted.")
    agreement = aa.get("agreement")
    agree = "n/a" if agreement is None else f"{100 * agreement:.1f}%"
    finalists = len(wf.get("finalists", []))
    return (prefix + f"A/A valid (choice agreement {agree}); {finalists} walk-forward "
            f"finalist(s) tested once; {len(eligible)} eligible for operator review; "
            "nothing promoted.")


# ------------------------------------------------------------------ digest


def _ci(ci: Sequence[float] | None) -> str:
    return "n/a" if ci is None else f"[{ci[0]:+.2f}, {ci[1]:+.2f}]"


def _pair(doc: Mapping[str, Any] | None) -> str:
    if doc is None:
        return "-"
    return f"{doc['diff_total']:+.2f} {_ci(doc['ci95'])} p={doc['p_one_sided']:.3f}"


def digest_markdown(doc: Mapping[str, Any]) -> str:
    lines: list[str] = []
    add = lines.append
    add(f"# Desk long run - {doc.get('run_id', '')}")
    add("")
    add(f"> {doc['untrusted_note']}")
    add("")
    add(f"**{doc['headline']}**")
    add("")
    again = doc.get("redigest")
    if again is not None:  # what this document is NOT (never mistaken for the run's own)
        said = [f"re-scored from receipts on disk + the outcome table "
                f"({again['model_calls']} model calls)"]
        if again.get("arms") is not None:
            said.append(f"ARM SUBSET ({doc['boards']['scored']} boards where all "
                        f"{len(again['arms'])} arms answered: "
                        f"{', '.join(again['arms'])}) - not the full policy pairing")
        if again.get("sessions"):
            window = again["sessions"]
            said.append(f"session window {window['first'] or '..'}..{window['last'] or '..'} "
                        f"({window['boards']} boards)")
        add("> Redigest: " + "; ".join(said) + ".")
        add("")
    promotion = doc["promotion"]
    add("## Pre-registered promotion rule (text only - this harness never promotes)")
    add("")
    add(promotion["rule"])
    add("")
    add(f"Promoted: {promotion['promoted']} (pre-registered at "
        f"{promotion.get('pre_registered_at')})")
    add("")
    protocol = doc["protocol"]
    boards = doc["boards"]
    add("## Protocol")
    add("")
    add(f"- boards: {boards['scored']} scored of {boards['total']} "
        f"({boards['excluded']} excluded for a missing/failed receipt in some arm); "
        f"sessions: {boards['sessions']['count']} ({boards['sessions']['first']} .. "
        f"{boards['sessions']['last']})")
    for key in ("resampling_unit", "ci", "p_value", "pairing", "unevaluable", "pair_arms",
                "standings_scope"):
        add(f"- {key}: {protocol[key]}")
    add(f"- draws {protocol['draws']}, seed {protocol['seed']}, random seeds "
        f"{protocol['random_seeds']}, capital ${protocol['capital']:.0f}")
    add(f"- incumbent: {protocol['incumbent']}; walk-forward cutoff "
        f"{protocol['cutoff'] or 'default (first two thirds tune)'}; metric "
        f"{protocol['metric']}; max finalists {protocol['max_finalists']}; "
        f"alpha {protocol['alpha']}")
    add("")
    add("## Standings (net $, 95% session-bootstrap CI; ordered by the vs-random CI low)")
    add("")
    add("| arm | kind | entered | unevaluable | failures | net total [95% CI] | "
        f"vs random | vs first_row | vs incumbent | vs {REGIME} | null pctile |")
    add("|---|---|---|---|---|---|---|---|---|---|---|")
    for row in doc["standings"]:
        add(f"| {row['arm']} | {row['kind']} | {row['entered']} | {row['unevaluable']} | "
            f"{row['failures']} | {row['net_total']:+.2f} {_ci(row['net_ci95'])} | "
            f"{_pair(row['vs_random'])} | {_pair(row['vs_first_row'])} | "
            f"{_pair(row['vs_incumbent'])} | {_pair(row.get('vs_regime'))} | "
            f"{row['null_percentile']:.3f} |")
    null = doc["random_null"]
    add(f"| random (p_enter {null['p_enter']:.3f}) | control | - | - | - | "
        f"{null['expected_total']:+.2f} {_ci(null['expected_ci95'])} | - | - | - | - | - |")
    for bench in doc["benchmarks"]:
        if bench.get("status") == "ok":
            add(f"| {bench['name']} buy-and-hold | benchmark | - | - | - | "
                f"{bench['net_total']:+.2f} {_ci(bench['net_ci95'])} | - | - | - | - | - |")
    add("")
    tallies = [(row["arm"], row["failure_reasons"]) for row in doc["standings"]
               if row.get("failure_reasons")]
    if tallies:  # failure transparency: what each arm's failures were, not just how many
        add("## Failure tally (per arm, from the receipts on disk)")
        add("")
        if not doc.get("complete"):
            add("PARTIAL RUN - receipts incomplete; these counts can still grow.")
            add("")
        for arm, reasons in tallies:
            add(f"- {arm}: " + ", ".join(f"{reason} {count}" for reason, count
                                         in sorted(reasons.items())))
        add("")
    healed = [(row["arm"], row["heals"]) for row in doc["standings"] if row.get("heals")]
    if healed:  # who actually answered: the self-heal ladder in full view
        add("## Self-heals (per arm, from the receipts on disk)")
        add("")
        for arm, tally in healed:
            line = (f"- {arm}: escalated {tally['escalated']}, timeout_escalated "
                    f"{tally['timeout_escalated']}, fallback {tally['fallback']}")
            who = tally.get("providers")
            if who:
                line += ("; answered by "
                         + ", ".join(f"{provider} {count}"
                                     for provider, count in sorted(who.items())))
            else:
                line += "; answered by: unattributed (receipts predate the provider field)"
            add(line)
        add("")
    aa = doc["aa"]
    add("## A/A check")
    add("")
    if aa["status"] == "not_run":
        add(f"NOT RUN - {aa['reason']}.")
    else:
        agreement = aa["agreement"]
        add(f"{aa['status'].upper()}: {aa['pair'][0]} vs {aa['pair'][1]} on "
            f"{aa['boards']} boards; choice agreement "
            f"{'n/a' if agreement is None else f'{100 * agreement:.1f}%'}; diff "
            f"{_pair(aa['diff'])}. Rule: {aa['rule']}.")
    add("")
    add("## Random null")
    add("")
    add(f"Uniform (row, horizon) at entry rate {null['p_enter']:.3f} matched to "
        f"{', '.join(null['matched_to']) or 'no model arm (always enter)'}; "
        f"{null['seeds']} seeds on the same boards: expected "
        f"{null['expected_total']:+.2f} {_ci(null['expected_ci95'])}, simulated 95% band "
        f"{_ci(null['band95'])}.")
    add("")
    wf = doc["walk_forward"]
    add("## Walk-forward (confirmatory)")
    add("")
    if wf.get("status") != "ok":
        add(f"Not applicable - {wf.get('reason')}.")
    else:
        add(f"Cutoff {wf['cutoff']} ({wf['tune_sessions']} tune / {wf['test_sessions']} "
            f"test sessions); metric {wf['metric']}; at most {wf['max_finalists']} "
            "finalists.")
        add("")
        for f in wf["finalists"]:
            test = f["test"]
            add(f"- {f['policy']}: tune metric {f['tune_metric']:+.2f}; test net "
                f"{test['net_total']:+.2f} {_ci(test['net_ci95'])}; vs random "
                f"{_pair(test['vs_random'])}; Holm p {f['holm_p']:.4f}; vs incumbent "
                f"{_pair(test['vs_incumbent'])}; eligible for operator review: "
                f"{f['eligible_for_operator_review']}")
    purged = wf.get("purge")
    if purged:
        add("")
        add(f"Purge + embargo (selection scoring changed 2026-09-28): {purged['rule']}. "
            f"Embargo {wf.get('embargo_sessions', 1)} session(s), "
            f"{wf.get('embargoed_sessions', 0)} embargoed. Purged train decisions (paired "
            "boards): " + (", ".join(f"{a} {c['purged']}/{c['train_entered']}"
                                     for a, c in purged["by_arm"].items()) or "none")
            + f"; random-null options purged {purged['random_null']['purged_options']}/"
              f"{purged['random_null']['train_options']}.")
    add("")
    add("## Stability (paired diff vs the random expectation)")
    add("")
    for row in doc["standings"]:
        stab = row["stability_vs_random"]
        add(f"- {row['arm']}: drop-one range [{stab['drop_one_min']:+.2f}, "
            f"{stab['drop_one_max']:+.2f}], sign flips {stab['drop_one_sign_flips']}; halves "
            f"{stab['half_split']['first']:+.2f} / {stab['half_split']['second']:+.2f} "
            f"(agree: {stab['half_split']['signs_agree']})")
    add("")
    if doc.get("skill"):
        from tree_options.desk import skill

        lines.extend(skill.skill_markdown(doc["skill"]))
    for name, section in (doc.get("reports") or {}).items():
        add(f"## Report: {name}")
        add("")
        add(str((section or {}).get("markdown") or json.dumps(section, default=str)[:4000]))
        add("")
    add("## Receipts")
    add("")
    for arm, path in doc["receipts"].items():
        add(f"- {arm}: {path}")
    add("")
    return "\n".join(lines)


def write_digest(run_dir: Path, doc: Mapping[str, Any]) -> None:
    _write_json(run_dir / "digest.json", doc)
    (run_dir / "digest.md").write_text(digest_markdown(doc), encoding="utf-8")


# --------------------------------------------------------------------- run


def boards_fingerprint(boards: Sequence[Board]) -> str:
    digest = hashlib.sha256()
    for board in boards:
        digest.update(json.dumps([board.snapshot, board.session, board.clock,
                                  board.ids]).encode())
    return digest.hexdigest()


def _plan(run_dir: Path, boards: Sequence[Board], policies: Sequence[PolicySpec],
          protocol: Protocol, meta: Mapping[str, Any] | None, clock: Clock) -> dict[str, Any]:
    """Write plan.json + boards.jsonl on first start; on resume refuse changed
    boards (the pairing) or a changed protocol (the pre-registration)."""
    path = run_dir / "plan.json"
    fingerprint = boards_fingerprint(boards)
    policy_docs = [{"name": p.name, "kind": p.kind, "repeats": p.repeats,
                    "provider": p.provider,
                    "prompt_sha256": (hashlib.sha256(p.prompt.encode()).hexdigest()
                                      if p.prompt else None)} for p in policies]
    if path.is_file():
        plan = json.loads(path.read_text(encoding="utf-8"))
        if plan.get("boards_fingerprint") != fingerprint:
            raise ValueError("the boards changed since this run started; start a new run dir")
        if plan.get("protocol") != protocol.to_json():
            raise ValueError("the protocol is pre-registered in plan.json and cannot change "
                             "on resume")
        plan["policies"] = policy_docs
        plan["resumed_at"] = clock().isoformat()
        _write_json(path, plan)
        return dict(plan)
    sessions = sorted({b.session for b in boards})
    plan = {"schema": PLAN_SCHEMA, "run_id": run_dir.name, "created": clock().isoformat(),
            "boards": len(boards), "boards_fingerprint": fingerprint,
            "sessions": {"count": len(sessions), "first": sessions[0], "last": sessions[-1]},
            "policies": policy_docs, "protocol": protocol.to_json(),
            "preregistered_rule": PREREGISTERED_RULE, "untrusted_note": UNTRUSTED_NOTE,
            "meta": dict(meta or {})}
    with (run_dir / "boards.jsonl").open("w", encoding="utf-8") as stream:
        for board in boards:
            stream.write(json.dumps({"snapshot": board.snapshot, "session": board.session,
                                     "clock": board.clock, "rows": board.rows,
                                     "context": board.context}, default=str) + "\n")
    _write_json(path, plan)
    return plan


def _run_reports(reports: Mapping[str, Callable[..., Mapping[str, Any]]], arms: Sequence[Arm],
                 receipts: Mapping[str, dict[str, dict[str, Any]]], files: dict[str, str],
                 **inputs: Any) -> tuple[list[Arm], dict[str, Any], dict[str, Any]]:
    """Post-execution report plug-ins: each returns a digest ``section`` and
    may add DERIVED deterministic arms (``derived``: [{arm, receipts, file}])
    scored on the same paired scoreboard. A failing report is recorded."""
    all_arms, all_receipts = list(arms), dict(receipts)
    sections: dict[str, Any] = {}
    for name, report in reports.items():
        try:
            out = report(arms=list(arms), receipts=receipts, **inputs)
            added = list(out.get("derived") or ())
            if any(item["arm"].name in all_receipts for item in added):
                raise ValueError("a derived arm name collides with an existing arm")
        except Exception as error:  # the digest still gets written
            sections[name] = {"status": "failed",
                              "error": f"{type(error).__name__}: {str(error)[:200]}"}
            continue
        for item in added:
            all_arms.append(item["arm"])
            all_receipts[item["arm"].name] = item["receipts"]
            files[item["arm"].name] = str(item.get("file") or "derived")
        sections[name] = out.get("section")
    return all_arms, all_receipts, sections


def run_longrun(run_dir: Path, *, boards: Sequence[Board], policies: Sequence[PolicySpec],
                outcome: OutcomeFn, ask: AskFn | None, quota_ok: QuotaFn,
                protocol: Protocol, settings: ExecSettings | None = None,
                benchmarks: Mapping[str, Mapping[Any, float]] | None = None,
                meta: Mapping[str, Any] | None = None, score_only: bool = False,
                sleep: Callable[[float], None] = time.sleep,
                monotonic: Callable[[], float] = time.monotonic,
                clock: Clock = _utcnow,
                skill_options: Mapping[str, Any] | None = None,
                reports: Mapping[str, Callable[..., Mapping[str, Any]]] | None = None
                ) -> dict[str, Any]:
    """Execute (or resume) every missing (arm, board), then score and digest.

    Returns {"status": "finished" | "stopped:<why>", ...}. A stopped run is
    resumable by calling again with the same ``run_dir``."""
    settings = settings or ExecSettings()
    boards = list(boards)
    if not boards:
        raise ValueError("no boards")
    if len({b.snapshot for b in boards}) != len(boards):
        raise ValueError("duplicate board snapshot ids")
    arms = arms_of(policies)
    if not arms:
        raise ValueError("no executable policy")
    if protocol.incumbent is not None and protocol.incumbent not in {p.name for p in policies}:
        raise ValueError(f"incumbent {protocol.incumbent!r} is not a configured policy")
    if any(a.policy.kind == "model" for a in arms) and ask is None and not score_only:
        raise ValueError("model policies need an ask plug-in")
    run_dir.mkdir(parents=True, exist_ok=True)
    with _run_lock(run_dir) as owned:
        if not owned:
            raise RunLocked(f"{run_dir.name}: another process holds this run")
        plan = _plan(run_dir, boards, policies, protocol, meta, clock)
        outcomes = OutcomeCache(outcome)
        executor = _Executor(run_dir, boards, arms, ask=ask, quota_ok=quota_ok,
                             outcomes=outcomes, settings=settings, sleep=sleep,
                             monotonic=monotonic, clock=clock)
        stop = "" if score_only else executor.run()
        if stop:
            executor.status = f"stopped:{stop}"
            executor.write_progress()
            return {"status": executor.status, "run_dir": str(run_dir)}
        complete = all(executor.receipts[a.name].get(b.snapshot, {}).get("ok")
                       for a in arms for b in boards)
        executor.status = "scoring"
        executor.write_progress()
        files = {a.name: str(receipts_path(run_dir, a.name)) for a in arms}
        try:
            scored_arms, scored_receipts, sections = _run_reports(
                reports or {}, arms, executor.receipts, files, run_dir=run_dir, boards=boards,
                outcomes=outcomes, protocol=protocol)
            digest = score_run(boards, scored_arms, scored_receipts, outcomes, protocol,
                               benchmarks=benchmarks, receipts_files=files,
                               run_id=run_dir.name, plan_created=plan.get("created"),
                               complete=complete, clock=clock, skill_options=skill_options)
        except Exception:
            executor.status = "scoring_failed"
            executor.write_progress()
            raise
        if sections:
            digest["reports"] = sections
        write_digest(run_dir, digest)
        executor.status = "finished"
        executor.digest = "digest.json"
        executor.write_progress()
        return {"status": "finished", "run_dir": str(run_dir),
                "headline": digest["headline"], "complete": complete}


# ----------------------------------------------------------------- plug-ins


@dataclass
class PluginContext:
    """What a plug-in factory sees. ``shared`` lets plug-ins share heavy
    inputs (the v1 boards plug-in leaves the parsed bundle for v1 outcome)
    and lets tests inject fakes (e.g. ``shared["transport"]`` for v1 ask)."""

    config_dir: Path
    shared: dict[str, Any] = field(default_factory=dict)
    boards: list[Board] = field(default_factory=list)


#: kind -> name -> factory(params: Mapping, ctx: PluginContext) -> provider
#:   boards:     -> list[Board]
#:   outcome:    -> OutcomeFn (snapshot, candidate_id, horizon) -> {"gross","net"} | None
#:   ask:        -> AskFn (PolicySpec, Board) -> (choice | None, horizon | None, note)
#:   quota:      -> QuotaFn () -> (ok, reason)
#:   benchmarks: -> {name: {ISO date: close}}
#:   report:     -> (run_dir=, boards=, arms=, receipts=, outcomes=, protocol=) ->
#:                  {"section": digest section, "derived": [{arm, receipts, file}]}
PLUGINS: dict[str, dict[str, Callable[[Mapping[str, Any], PluginContext], Any]]] = {
    "boards": {}, "outcome": {}, "ask": {}, "quota": {}, "benchmarks": {}, "report": {}}


def register_plugin(kind: str, name: str,
                    factory: Callable[[Mapping[str, Any], PluginContext], Any], *,
                    replace: bool = False) -> None:
    if kind not in PLUGINS:
        raise ValueError(f"plug-in kind must be one of {sorted(PLUGINS)}")
    if not replace and name in PLUGINS[kind]:
        raise ValueError(f"{kind} plug-in {name!r} is already registered")
    PLUGINS[kind][name] = factory


def plugin(kind: str, name: str) -> Callable[[Mapping[str, Any], PluginContext], Any]:
    try:
        return PLUGINS[kind][name]
    except KeyError:
        raise ValueError(f"unknown {kind} plug-in {name!r} "
                         f"(registered: {sorted(PLUGINS.get(kind, {}))})") from None


def _resolve_path(ctx: PluginContext, value: Any) -> Path:
    if not value:
        raise ValueError("a path parameter is required")
    path = Path(str(value)).expanduser()
    return path if path.is_absolute() else ctx.config_dir / path


def _v1_boards(params: Mapping[str, Any], ctx: PluginContext) -> list[Board]:
    """lab.board_rows over the as-of candidates of every scheduled snapshot of
    every session - exactly iag.decision_packet's candidates (same function,
    same 15-minute freshness), with the bundle parsed once instead of per board."""
    from tree_options.desk import hindsight, lab
    from tree_options.desk import intraday_action_graph as iag

    bundle = _resolve_path(ctx, params.get("bundle"))
    raw = json.loads(bundle.read_bytes())
    bars, contracts = hindsight.parse_bundle(raw)
    sessions = hindsight.all_sessions(raw)
    age_s = 15 * 60
    boards: list[Board] = []
    shown: dict[str, dict[str, dict[str, Any]]] = {}
    for day in sessions:
        for clock in iag.schedule_for(day):
            candidates = iag._candidates(contracts, bars, iag._instant(day, clock), age_s)
            rows = lab.board_rows({"candidates": candidates})
            if not rows:
                continue
            snapshot = f"s:{day.isoformat()}T{clock}"
            ids = {row["id"] for row in rows}
            shown[snapshot] = {c["id"]: c for c in candidates if c["id"] in ids}
            boards.append(Board(snapshot, day.isoformat(), clock, rows,
                                {"bundle": bundle.name}))
    ctx.shared["v1"] = {"bars": bars, "sessions": sessions, "shown": shown, "age_s": age_s,
                        "underlyings": sorted({c.underlying for c in contracts.values()})}
    return boards


def _v1_outcome(params: Mapping[str, Any], ctx: PluginContext) -> OutcomeFn:
    """Hindsight-style isolated intraday outcome (net = gross; horizon ignored:
    the v1 simulator exits at the first later fresh mark)."""
    from tree_options.desk import hindsight

    state = ctx.shared.get("v1")
    if state is None:
        raise ValueError("the v1 outcome plug-in needs the v1 boards plug-in")

    def outcome(snapshot: str, candidate_id: str, horizon: str | None) -> dict[str, float] | None:
        candidate = state["shown"].get(snapshot, {}).get(candidate_id)
        if candidate is None:
            return None
        day_text, _, clock = snapshot.removeprefix("s:").rpartition("T")
        pnl = hindsight._candidate_outcome(state["bars"], state["sessions"],
                                           date.fromisoformat(day_text), clock, candidate,
                                           state["age_s"])
        if pnl is None:
            return None
        value = float(pnl)
        return {"gross": value, "net": value}
    return outcome


def _v1_ask(params: Mapping[str, Any], ctx: PluginContext) -> AskFn:
    """lab.board_prompt through discovery.llm.chat_json; the provider comes
    from the policy spec (default ``params.provider``, minimax-flash)."""
    from tree_options.desk import lab

    default = str(params.get("provider", "minimax-flash"))
    transport = ctx.shared.get("transport")

    def ask(spec: PolicySpec, board: Board) -> tuple[str | None, str | None, str]:
        reply = lab.ask_board(spec.provider or default, board.rows, transport=transport,
                              policy_prompt=spec.prompt)
        choice, note = lab.parse_choice(reply, set(board.ids))
        return choice, None, note
    return ask


def broker_quota(url: str = BROKER_URL, provider: str = "minimax", margin: float = 2.0,
                 opener: Callable[..., Any] | None = None) -> QuotaFn:
    """Burn only while the provider's 5h window is under-using (left >=
    planned - margin). A meter that is down does not stall the run, and says so."""
    open_url = opener or urllib.request.urlopen

    def quota_ok() -> tuple[bool, str]:
        try:
            with open_url(url, timeout=5) as response:
                meter = json.load(response)["providers"][provider]["meter"]
            left, planned = float(meter["interval_pct"]), float(meter["planned_pct_now"])
            return left >= planned - margin, f"left={left:.1f} planned={planned:.1f}"
        except Exception as error:
            return True, f"meter_unavailable:{type(error).__name__}"
    return quota_ok


def _quota_broker(params: Mapping[str, Any], ctx: PluginContext) -> QuotaFn:
    return broker_quota(str(params.get("url", BROKER_URL)),
                        str(params.get("provider", "minimax")),
                        float(params.get("margin", 2.0)))


def _quota_always(params: Mapping[str, Any], ctx: PluginContext) -> QuotaFn:
    return lambda: (True, "no quota gate configured")


def _bench_none(params: Mapping[str, Any], ctx: PluginContext) -> dict[str, dict[str, float]]:
    return {}


def _bench_file(params: Mapping[str, Any], ctx: PluginContext) -> dict[str, dict[str, float]]:
    doc = json.loads(_resolve_path(ctx, params.get("path")).read_text(encoding="utf-8"))
    if not isinstance(doc, dict):
        raise ValueError("benchmarks file must map name -> {date: close}")
    return {str(name): {str(d): float(v) for d, v in series.items()}
            for name, series in doc.items() if isinstance(series, dict)}


def _panel_closes(series: Mapping[str, Any]) -> dict[str, float]:
    out: dict[str, float] = {}
    for day, bar in series.items():
        try:
            out[str(day)] = float(bar["close"])
        except (KeyError, TypeError, ValueError):
            continue
    return out


def equal_weight_index(closes: Mapping[str, Mapping[str, float]],
                       first_session: str) -> dict[str, float]:
    """An un-rebalanced equal-weight basket bought at the last common date
    before ``first_session``: index(d) = mean_i close_i(d) / close_i(base)."""
    common = sorted(set.intersection(*(set(c) for c in closes.values()))) if closes else []
    before = [d for d in common if d < first_session]
    if not common:
        return {}
    base = before[-1] if before else common[0]
    return {d: float(np.mean([c[d] / c[base] for c in closes.values()]))
            for d in common if d >= base}


def _bench_panel(params: Mapping[str, Any], ctx: PluginContext) -> dict[str, dict[str, float]]:
    """Daily closes from the desk's OHLC panel (default <paper>/ohlc-panel.json):
    each ``symbols`` entry, plus an equal-weight basket of ``equal_weight``
    (a symbol list, or "bundle" = the v1 bundle's underlyings)."""
    from tree_options.desk import paths

    path = (_resolve_path(ctx, params["path"]) if params.get("path")
            else paths.paper_dir() / "ohlc-panel.json")
    panel = json.loads(path.read_text(encoding="utf-8"))
    out: dict[str, dict[str, float]] = {}
    for symbol in params.get("symbols", ["SPY", "QQQ"]):
        if isinstance(panel.get(symbol), dict):
            out[str(symbol)] = _panel_closes(panel[symbol])
    basket = params.get("equal_weight")
    if basket and ctx.boards:
        names = (ctx.shared.get("v1", {}).get("underlyings", []) if basket == "bundle"
                 else list(basket))
        closes = {n: _panel_closes(panel[n]) for n in names if isinstance(panel.get(n), dict)}
        if closes:
            first = min(b.session for b in ctx.boards)
            out[f"equal-weight-{len(closes)}"] = equal_weight_index(closes, first)
    return out


def _v2_boards(params: Mapping[str, Any], ctx: PluginContext) -> list[Board]:
    """Environment v2 boards: the stratified, aliased, context-carrying board
    (lab.board_rows_v2 + lab.board_context) of every scheduled snapshot of
    every session, the bundle parsed once (outcomes.prepare_index). Only the
    model-visible ``public`` context rides on the Board (it is serialized into
    the plan); the full BoardContext stays in ``shared`` for the ask plug-in."""
    from tree_options.desk import intraday_action_graph as iag
    from tree_options.desk import lab, outcomes

    bundle = _resolve_path(ctx, params.get("bundle"))
    index = outcomes.prepare_index(json.loads(bundle.read_bytes()))
    boards: list[Board] = []
    contexts: dict[str, Any] = {}
    for day in index.sessions:
        for clock in iag.schedule_for(day):
            candidates = outcomes.board_candidates(index, day, clock)
            if not candidates:
                continue
            context = lab.board_context(index, day, clock)
            packet = {"as_of": iag._instant(day, clock).isoformat(),
                      "candidates": candidates}
            rows = lab.board_rows_v2(packet, context)
            if not rows:
                continue
            snapshot = f"s:{day.isoformat()}T{clock}"
            contexts[snapshot] = context
            boards.append(Board(snapshot, day.isoformat(), clock, rows, dict(context.public)))
    ctx.shared["v2"] = {"index": index, "contexts": contexts}
    return boards


def _v2_outcome(params: Mapping[str, Any], ctx: PluginContext) -> OutcomeFn:
    """Net-of-cost, leg-synced outcomes per horizon. With ``table`` (an
    ``desk outcome-table`` JSONL) the lookup is free; otherwise each
    (snapshot, candidate, horizon) is computed live from the v2 index with
    the cost model named by ``params["cost_model"]`` -- ``flat`` (the frozen
    $14.60 baseline) or ``measured`` (per moneyness) -- and ``sync`` minutes,
    memoized. Under ``measured`` a candidate the model cannot price is
    REFUSED and recorded in the shared ``NoPriceLedger`` rather than scored as
    a zero-profit trade. A missing horizon (rules that do not choose one)
    uses ``default_horizon``."""
    from tree_options.desk import outcomes

    default_horizon = str(params.get("default_horizon", "intraday"))
    if default_horizon not in outcomes.EXIT_MODES:
        raise ValueError(f"default_horizon must be one of {outcomes.EXIT_MODES}")
    table_path = params.get("table")
    if table_path:
        table: dict[tuple[str, str, str], dict[str, Any] | None] = {}
        with _resolve_path(ctx, table_path).open(encoding="utf-8") as stream:
            for line in stream:
                row = json.loads(line)
                if row.get("status") == "no_fill" or row.get("net") is None:
                    value: dict[str, Any] | None = None
                else:  # exit_at feeds the purged walk-forward (desk.purge)
                    value = {"gross": float(row["gross"]), "net": float(row["net"]),
                             "exit_at": row.get("exit_at")}
                table[(row["snapshot"], row["candidate_id"], row["exit_mode"])] = value

        def lookup(snapshot: str, candidate_id: str,
                   horizon: str | None) -> dict[str, Any] | None:
            return table.get((snapshot, candidate_id, horizon or default_horizon))
        return lookup

    state = ctx.shared.get("v2")
    if state is None:
        raise ValueError("the v2 outcome plug-in needs a table or the v2 boards plug-in")
    # The cost model is an EXPLICIT choice, never a silent default. `flat` is
    # the frozen $14.60 baseline the 25-arm digest is calibrated on;
    # `measured` prices per moneyness from the measured surface.
    model_name = str(params.get("cost_model", "flat"))
    if model_name == "flat":
        costs: Any = outcomes.CostModel()
    elif model_name == "measured":
        from tree_options.desk.cost import NoPriceLedger, SpreadCostModel
        costs = SpreadCostModel.measured()
        # A refusal must be COUNTED here, not swallowed: the live path is the
        # one place the measured model will actually run, and an uncounted
        # refusal scores as a zero-profit trade.
        ctx.shared.setdefault("no_price", NoPriceLedger())
    else:
        raise ValueError(f"cost_model must be 'flat' or 'measured', not {model_name!r}")
    sync = params.get("sync", 2)
    memo: dict[tuple[str, str, str], dict[str, Any] | None] = {}

    def live(snapshot: str, candidate_id: str,
             horizon: str | None) -> dict[str, Any] | None:
        mode = horizon or default_horizon
        key = (snapshot, candidate_id, mode)
        if key not in memo:
            day_text, _, clock = snapshot.removeprefix("s:").rpartition("T")
            doc = outcomes.candidate_outcome(
                state["index"], date.fromisoformat(day_text), clock, candidate_id,
                exit_mode=mode, costs=costs,
                leg_sync_minutes=None if sync in (None, "off") else int(sync),
                no_price=ctx.shared.get("no_price"))
            memo[key] = (None if doc is None or doc.get("status") in ("no_fill", "no_price")
                         or doc.get("net") is None
                         else {"gross": float(doc["gross"]), "net": float(doc["net"]),
                               "exit_at": doc.get("exit_at")})
        return memo[key]
    return live


def _v2_ask(params: Mapping[str, Any], ctx: PluginContext) -> AskFn:
    """lab.board_prompt_v2 (public context + whitelisted row fields + horizon
    menu) through discovery.llm.chat_json; parse_choice_v2 rejects an unknown
    id or horizon outright. Provider from the policy spec (default
    ``params.provider``, minimax-flash); ``effort`` sets M3.1's
    reasoning_effort (absent = the provider default, max); ``timeout`` (s)
    and ``max_tokens`` override the provider's budget per call - thinking at
    max effort times out on the hardest boards, and a failed receipt is
    missing-not-at-random, so a resume retry pass may give it more room.
    A truncated reply self-heals once (doubled budget, capped 48000 tokens /
    900 s). ``fallback_provider`` (e.g. "zai" when the primary is
    minimax-flash) takes ONE attempt after the primary's final failure - the
    desk burns both subscriptions and a provider outage stops costing boards.
    llm.ask_json records who answered (provider / escalated / fallback) on
    every receipt; the fallback call never carries the primary's
    provider-specific extras (reasoning_effort)."""
    from tree_options.desk import lab
    from tree_options.desk.forecast import EFFORTS
    from tree_options.trex.discovery.llm import PROVIDERS, ask_json

    default = str(params.get("provider", "minimax-flash"))
    effort = params.get("effort")
    if effort is not None and effort not in EFFORTS:
        raise ValueError(f"effort must be one of {EFFORTS}")
    fallback = params.get("fallback_provider")
    if fallback is not None:
        if fallback not in PROVIDERS:
            raise ValueError(f"fallback_provider must be one of {sorted(PROVIDERS)}")
        if fallback == default:
            raise ValueError("fallback_provider must differ from provider")
    timeout = None if params.get("timeout") is None else float(params["timeout"])
    max_tokens = None if params.get("max_tokens") is None else int(params["max_tokens"])
    if (timeout is not None and not 0 < timeout <= 900) or (
            max_tokens is not None and not 0 < max_tokens <= 64000):
        raise ValueError("timeout must be in (0, 900] s and max_tokens in (0, 64000]")
    extra: dict[str, Any] = {}
    if effort is not None:
        extra["reasoning_effort"] = effort
    if max_tokens is not None:
        extra["max_tokens"] = max_tokens
    transport = ctx.shared.get("transport")
    state = ctx.shared.get("v2")
    if state is None:
        raise ValueError("the v2 ask plug-in needs the v2 boards plug-in")

    def ask(spec: PolicySpec, board: Board) -> tuple[Any, ...]:
        context = state["contexts"][board.snapshot]
        provider = spec.provider or default
        reply, _model, meta = ask_json(
            provider, lab.board_prompt_v2(board.rows, context, spec.prompt),
            fallback=fallback, transport=transport, extra=extra or None,
            timeout=timeout, max_tokens=max_tokens)
        return (*lab.parse_choice_v2(reply, set(board.ids)), meta)
    return ask


register_plugin("boards", "v1", _v1_boards)
register_plugin("outcome", "v1", _v1_outcome)
register_plugin("ask", "v1", _v1_ask)
register_plugin("boards", "v2", _v2_boards)
register_plugin("outcome", "v2", _v2_outcome)
register_plugin("ask", "v2", _v2_ask)
register_plugin("quota", "broker", _quota_broker)
register_plugin("quota", "always", _quota_always)
register_plugin("benchmarks", "none", _bench_none)
register_plugin("benchmarks", "file", _bench_file)
register_plugin("benchmarks", "panel", _bench_panel)


def _forecast_plugin(kind: str) -> Callable[[Mapping[str, Any], PluginContext], Any]:
    """desk.forecast's plug-ins, imported on first use (it imports this module)."""
    def factory(params: Mapping[str, Any], ctx: PluginContext) -> Any:
        from tree_options.desk import forecast
        return getattr(forecast, f"{kind}_plugin")(params, ctx)
    return factory


register_plugin("ask", "forecast", _forecast_plugin("ask"))
register_plugin("report", "forecast", _forecast_plugin("report"))


# ------------------------------------------------------------------ config


def _builtin_rule(entry: Mapping[str, Any]) -> RuleFn:
    builtin = entry.get("builtin")
    horizon = entry.get("horizon")
    key = str(entry.get("key", "board_order"))
    if builtin == "no_trade":
        return rule_no_trade
    if builtin == FIRST_ROW:
        return rule_first_row(horizon)
    if builtin == "fixed_structure":
        return rule_fixed_structure(str(entry.get("structure")), horizon, key)
    if builtin == "always_bullish":
        return rule_always_bullish(horizon, key)
    if builtin == "theory":  # the theory lane's parameterized rules (own module)
        from tree_options.desk.theory_rules import rule_theory
        return rule_theory(entry)
    raise ValueError(f"{entry.get('name')}: unknown builtin {builtin!r}")


def policies_from_config(entries: Sequence[Mapping[str, Any]], *, builtin: bool = True,
                         horizon: str | None = None) -> list[PolicySpec]:
    specs: list[PolicySpec] = []
    for entry in entries:
        name, kind = str(entry.get("name")), str(entry.get("kind"))
        repeats = int(entry.get("repeats", 1))
        if kind == "model":
            specs.append(PolicySpec(name, "model", repeats=repeats,
                                    provider=entry.get("provider"),
                                    prompt=entry.get("prompt")))
        elif entry.get("builtin") == RANDOM or name == RANDOM:
            specs.append(PolicySpec(RANDOM, "control"))
        else:
            specs.append(PolicySpec(name, kind, repeats=repeats, rule=_builtin_rule(entry)))
    if builtin:
        present = {s.name for s in specs}
        specs.extend(s for s in builtin_controls(horizon) if s.name not in present)
    return specs


def protocol_from_config(cfg: Mapping[str, Any]) -> Protocol:
    doc = dict(cfg.get("protocol", {}))
    horizons = doc.get("random_horizons")
    return Protocol(
        capital=float(doc.get("capital", CAPITAL)), draws=int(doc.get("draws", 10_000)),
        seed=int(doc.get("seed", 20260928)), random_seeds=int(doc.get("random_seeds", 1000)),
        random_horizons=None if horizons is None else tuple(horizons),
        incumbent=cfg.get("incumbent"), cutoff=doc.get("cutoff"),
        metric=str(doc.get("metric", "ci_low_diff_vs_random")),
        max_finalists=int(doc.get("max_finalists", MAX_FINALISTS)),
        alpha=float(doc.get("alpha", 0.05)),
        embargo_sessions=int(doc.get("embargo_sessions", 1)))


def settings_from_config(cfg: Mapping[str, Any], concurrency: int | None = None) -> ExecSettings:
    quota = dict(cfg.get("quota", {}))
    max_pause = quota.get("max_pause_s")
    return ExecSettings(
        concurrency=int(concurrency or cfg.get("concurrency", 8)),
        seed=int(cfg.get("seed", 20260928)),
        quota_every=int(quota.get("check_every", 20)),
        pause_s=float(quota.get("pause_s", 300.0)),
        max_pause_s=None if max_pause is None else float(max_pause),
        retry_failed=bool(cfg.get("retry_failed", True)))


def default_root() -> Path:
    from tree_options.desk.paths import store_root

    return store_root() / "evaluations" / "longrun"


def new_run_dir(root: Path, now: datetime) -> Path:
    base = root / now.strftime("%Y%m%dT%H%M%SZ")
    suffix = 0
    while True:
        target = base if suffix == 0 else base.with_name(f"{base.name}-{suffix}")
        try:
            target.mkdir(parents=True, exist_ok=False)
            return target
        except FileExistsError:
            suffix += 1


def run_from_config(config_path: Path, *, run_dir: Path | None = None,
                    limit: int | None = None, score_only: bool = False,
                    concurrency: int | None = None, shared: Mapping[str, Any] | None = None,
                    sleep: Callable[[float], None] = time.sleep,
                    clock: Clock = _utcnow) -> dict[str, Any]:
    """Resolve the config's plug-ins and run (or resume) the long run."""
    raw_config = config_path.read_bytes()
    cfg = json.loads(raw_config)
    if not isinstance(cfg, dict):
        raise ValueError("config must be a JSON object")
    for key in ("boards", "outcome", "policies"):
        if key not in cfg:
            raise ValueError(f"config needs {key!r}")
    ctx = PluginContext(config_dir=config_path.resolve().parent, shared=dict(shared or {}))
    boards = list(plugin("boards", str(cfg["boards"].get("plugin")))(cfg["boards"], ctx))
    if limit is not None:
        if limit < 1:
            raise ValueError("--limit must be >= 1")
        boards = boards[:limit]
    ctx.boards = boards
    outcome = plugin("outcome", str(cfg["outcome"].get("plugin")))(cfg["outcome"], ctx)
    ctx.shared.setdefault("outcome", outcome)  # the forecast plug-ins fit on it (TRAIN only)
    policies = policies_from_config(cfg["policies"], builtin=bool(cfg.get("builtin_controls", True)),
                                    horizon=cfg.get("control_horizon"))
    ask: AskFn | None = None
    if cfg.get("ask") is not None:
        ask = plugin("ask", str(cfg["ask"].get("plugin")))(cfg["ask"], ctx)
    overrides = {str(e["name"]): plugin("ask", str(e["ask"].get("plugin")))(e["ask"], ctx)
                 for e in cfg["policies"] if isinstance(e.get("ask"), Mapping)}
    if overrides:
        missing = [p.name for p in policies if p.kind == "model" and p.name not in overrides]
        if missing and ask is None:
            raise ValueError(f"model policies without an ask plug-in: {missing}")
        ask = PolicyAsk(ask, overrides)
    reports = {str(e.get("name") or e.get("plugin")): plugin("report", str(e.get("plugin")))(e, ctx)
               for e in cfg.get("reports") or ()}
    quota_cfg = cfg.get("quota") or {"plugin": "always"}
    quota_ok = plugin("quota", str(quota_cfg.get("plugin", "always")))(quota_cfg, ctx)
    bench_cfg = cfg.get("benchmarks") or {"plugin": "none"}
    benchmarks = plugin("benchmarks", str(bench_cfg.get("plugin", "none")))(bench_cfg, ctx)
    protocol = protocol_from_config(cfg)
    settings = settings_from_config(cfg, concurrency)
    if run_dir is None:
        root = (_resolve_path(ctx, cfg["out_root"]) if cfg.get("out_root") else default_root())
        run_dir = new_run_dir(root, clock())
    run_dir.mkdir(parents=True, exist_ok=True)
    config_copy = run_dir / "config.json"
    if not config_copy.exists():
        config_copy.write_bytes(raw_config)
    meta = {"config_sha256": hashlib.sha256(raw_config).hexdigest(),
            "plugins": {k: (cfg.get(k) or {}).get("plugin")
                        for k in ("boards", "outcome", "ask", "quota", "benchmarks")},
            "ask_overrides": {str(e["name"]): e["ask"].get("plugin") for e in cfg["policies"]
                              if isinstance(e.get("ask"), Mapping)},
            "reports": sorted(reports), "limit": limit}
    return run_longrun(run_dir, boards=boards, policies=policies, outcome=outcome, ask=ask,
                       quota_ok=quota_ok, protocol=protocol, settings=settings,
                       benchmarks=benchmarks, meta=meta, score_only=score_only,
                       sleep=sleep, clock=clock, skill_options=cfg.get("skill"),
                       reports=reports)


# ------------------------------------------------------------------ cockpit


def find_run_dir(root: Path) -> Path | None:
    """``root`` itself when it is a run dir, else its most recently updated run."""
    if (root / "progress.json").is_file():
        return root
    if not root.is_dir():
        return None
    runs = [p for p in root.iterdir()
            if p.is_dir() and not p.is_symlink() and (p / "progress.json").is_file()]
    if not runs:
        return None
    return max(runs, key=lambda p: ((p / "progress.json").stat().st_mtime, p.name))


def _read_json(path: Path, max_bytes: int) -> dict[str, Any]:
    if path.is_symlink():
        raise ValueError(f"{path.name}: symlinked evidence is refused")
    if path.stat().st_size > max_bytes:
        raise ValueError(f"{path.name}: too large")
    doc = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict):
        raise ValueError(f"{path.name}: not a JSON object")
    return doc


_PROGRESS_KEYS = ("status", "at", "started", "boards", "sessions", "total", "finished",
                  "failures", "paused_s", "quota", "calls_per_s", "eta_s", "arms", "digest",
                  "skill")


def _project_progress(doc: Mapping[str, Any]) -> dict[str, Any]:
    if doc.get("schema") == PROGRESS_SCHEMA:
        return {"schema": PROGRESS_SCHEMA, **{k: doc.get(k) for k in _PROGRESS_KEYS}}
    policies = doc.get("policies")
    if doc.get("schema") is None and isinstance(policies, dict):  # the run_v1.py prototype
        eta_min = doc.get("eta_min")
        return {"schema": PROGRESS_SCHEMA, "legacy": "run_v1", "status": "unknown",
                "at": doc.get("at"), "started": None, "boards": None, "sessions": None,
                "total": doc.get("total"), "finished": doc.get("finished"),
                "failures": doc.get("failures"), "paused_s": doc.get("paused_s"),
                "quota": {"ok": None, "reason": str(doc.get("quota", ""))[:120],
                          "checked_at": None},
                "calls_per_s": doc.get("calls_per_s"),
                "eta_s": None if eta_min is None else float(eta_min) * 60,
                "arms": {str(name): {"policy": str(name), "repeat": 1, "kind": "model",
                                     "done": v.get("done"), "total": v.get("of"),
                                     "entered": v.get("entered"), "failures": v.get("fail"),
                                     "unevaluable": None, "net": None}
                         for name, v in policies.items() if isinstance(v, dict)},
                "digest": None}
    raise ValueError("unrecognized progress document")


def _project_digest(doc: Mapping[str, Any]) -> dict[str, Any]:
    if doc.get("schema") != DIGEST_SCHEMA:
        raise ValueError("unrecognized digest document")
    promotion = doc.get("promotion")
    if not isinstance(promotion, dict) or promotion.get("promoted") is not False:
        raise ValueError("a digest must carry promoted: false")
    keep = ("arm", "policy", "repeat", "kind", "boards", "entered", "entry_rate",
            "unevaluable", "failures", "net_total", "net_ci95", "vs_random", "vs_first_row",
            "vs_incumbent", "vs_regime", "null_percentile", "failure_reasons", "heals")
    wf = doc.get("walk_forward") or {}
    standings = []
    for row in doc.get("standings", []):
        projected = {k: row.get(k) for k in keep}
        if not projected.get("heals"):
            projected.pop("heals", None)  # additive: a clean arm carries no heals key
        standings.append(projected)
    return {"headline": doc.get("headline"), "untrusted_note": doc.get("untrusted_note"),
            "evaluation_valid": doc.get("evaluation_valid"), "complete": doc.get("complete"),
            "at": doc.get("at"),
            "promotion": {"promoted": False, "rule": promotion.get("rule")},
            "boards": doc.get("boards"), "aa": doc.get("aa"),
            "random_null": doc.get("random_null"),
            "standings": standings,
            "walk_forward": {k: wf.get(k) for k in ("status", "cutoff", "metric",
                                                     "max_finalists", "tune_sessions",
                                                     "test_sessions", "reason")}
            | {"finalists": [{"policy": f.get("policy"), "holm_p": f.get("holm_p"),
                              "test": {k: f.get("test", {}).get(k)
                                       for k in ("net_total", "net_ci95", "vs_random",
                                                 "vs_incumbent")},
                              "eligible_for_operator_review":
                                  f.get("eligible_for_operator_review")}
                             for f in wf.get("finalists", [])]},
            "benchmarks": doc.get("benchmarks", []),
            "skill": _skill_projection(doc.get("skill"))}


def _skill_projection(section: Any) -> dict[str, Any] | None:
    from tree_options.desk import skill

    return skill.cockpit_projection(section)


def cockpit_view(root: Path) -> dict[str, Any]:
    """The read-only cockpit projection: live progress plus the digest's
    standings when finished. No absolute paths; never launches work."""
    run_dir = find_run_dir(root)
    if run_dir is None:
        return {"schema": VIEW_SCHEMA, "run": None, "progress": None, "digest": None}
    progress = _project_progress(_read_json(run_dir / "progress.json", 5_000_000))
    digest_path = run_dir / "digest.json"
    digest = (_project_digest(_read_json(digest_path, 50_000_000))
              if digest_path.is_file() else None)
    return {"schema": VIEW_SCHEMA, "run": run_dir.name, "progress": progress,
            "digest": digest}


# --------------------------------------------------------------------- CLI


def register_cli(sub: Any) -> None:
    """``desk longrun run|status`` on the desk CLI's subparsers."""
    parser = sub.add_parser("longrun", help="the desk lab long run (paired, resumable, "
                                            "quota-aware evaluation; never promotes)")
    commands = parser.add_subparsers(dest="longrun_command", required=True)
    run = commands.add_parser("run", help="run (or resume with --run-dir) from a config")
    run.add_argument("--config", type=Path, required=True)
    run.add_argument("--run-dir", type=Path, help="resume this run dir")
    run.add_argument("--limit", type=int, help="smoke: the first N boards only")
    run.add_argument("--concurrency", type=int)
    run.add_argument("--score-only", action="store_true",
                     help="score the receipts on disk now; no model calls")
    status = commands.add_parser("status", help="progress of the latest (or a given) run")
    status.add_argument("--dir", type=Path,
                        help="a run dir or a root of run dirs (default "
                             "DESK_STORE/evaluations/longrun)")
    redigest = commands.add_parser(
        "redigest", help="re-score a run dir from its receipts + the outcome table, with the "
                         "skill section (zero model calls; desk.skill)")
    redigest.add_argument("--run-dir", type=Path, required=True)
    redigest.add_argument("--table", type=Path,
                          help="outcome table JSONL (default: the config's outcome.table)")
    redigest.add_argument("--out", type=Path,
                          help="write digest.json/.md here, never touching the run dir "
                               "(required while the run is live)")
    redigest.add_argument("--sessions", metavar="FIRST:LAST",
                          help="re-score only boards whose session is in [FIRST, LAST] "
                               "(ISO dates, either may be empty; needs --out)")
    redigest.add_argument("--arms", metavar="A,B,...",
                          help="score only these arms (e.g. the model arms mid-run), paired "
                               "on the boards where ALL of them have ok receipts; the "
                               "built-in controls are not forced in (needs --out)")
    from tree_options.desk import reflect  # `longrun reflect` (desk.reflect owns it)

    reflect.register_cli(commands)


def dispatch_cli(args: argparse.Namespace) -> int:
    if args.longrun_command == "redigest":
        from tree_options.desk import skill

        return skill.redigest_cli(args)
    if args.longrun_command == "reflect":
        from tree_options.desk import reflect

        return reflect.dispatch_cli(args)
    if args.longrun_command == "run":
        try:
            result = run_from_config(args.config, run_dir=args.run_dir, limit=args.limit,
                                     score_only=args.score_only,
                                     concurrency=args.concurrency)
        except RunLocked as error:
            print(f"longrun: {error}", file=sys.stderr)
            return 3
        except (ValueError, OSError, KeyError, TypeError, AttributeError) as error:
            print(f"longrun: refused: {error}", file=sys.stderr)
            return 2
        print(json.dumps(result, indent=2))
        return 0 if result["status"] == "finished" else 3
    try:
        view = cockpit_view(args.dir or default_root())
    except (OSError, ValueError) as error:
        print(f"longrun status: unreadable: {error}", file=sys.stderr)
        return 1
    if view["run"] is None:
        print("longrun status: no run found")
        return 1
    progress = view["progress"]
    lines = [f"run {view['run']}: {progress['status']} {progress['finished']}/"
             f"{progress['total']} failures={progress['failures']} paused_s="
             f"{progress['paused_s']} eta_s={progress['eta_s']} quota="
             f"{(progress['quota'] or {}).get('reason')}"]
    for name, arm in (progress.get("arms") or {}).items():
        lines.append(f"  {name}: {arm.get('done')}/{arm.get('total')} entered="
                     f"{arm.get('entered')} failures={arm.get('failures')}"
                     + (f" ({', '.join(f'{k} {v}' for k, v in sorted(
                         arm['failure_reasons'].items()))})"
                        if arm.get("failure_reasons") else "")
                     + f" net={arm.get('net')}")
    if view["digest"] is not None:
        lines.append(f"  digest: {view['digest']['headline']}")
        healed = [(row["arm"], row["heals"]) for row in view["digest"].get("standings", [])
                  if row.get("heals")]
        if healed:  # the self-heal ladder: how much of the run the backups carried
            lines.append("  self-heals: " + "; ".join(
                f"{arm} escalated={t['escalated']} timeout={t['timeout_escalated']} "
                f"fallback={t['fallback']}"
                + (f" providers={','.join(f'{p}:{n}' for p, n in sorted(t['providers'].items()))}"
                   if t.get("providers") else "")
                for arm, t in healed))
    print("\n".join(lines))
    return 0
