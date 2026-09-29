"""The desk FORECASTER: a model's probabilistic views, scored by proper
scoring rules, turned into trades by a deterministic cost-aware rule.

Why: as a one-shot "pick a row" chooser MiniMax-M3.1-Flash showed no skill
and heavy position bias (row 0 ~83%); ~500 trades per run cannot separate
skill from the regime. A forecaster answers ~688 boards x 3 underlyings x
3 horizons = ~6,200 binary questions per arm, each scored on its own.

Contract (``forecast_prompt`` / ``parse_forecast``): per board the model sees
ONLY the board's public as-of context (aliased underlyings, 1/5/20-session
returns, 20-session realized vol, time of day; no rows, tickers, dates or
levels) and returns, for every underlying and every horizon of
:data:`FORECAST_HORIZONS`, the probability that its spot is STRICTLY higher
at that horizon's exit than at the board's as-of instant (optionally an
expected return in bps). Replies are validated, never repaired. Underlyings
are relabelled AND reordered by a seeded per-(board, repeat) permutation
(:func:`permutation`); the receipt records it, so two repeats are an A/A
check on order sensitivity. No per-row P(net profit): it would multiply the
answer ~5x on a model that already truncates at 12k thinking tokens, and a
row's profit is a deterministic function of direction x payoff shape that
the decision rule computes itself.

Labels (:func:`label_table`): the exit instant is outcomes.py's clock rule
for that exit mode (intraday = the first later clock with a mark; eod = the
entry session's last clock with a mark, else the next mark; hold:N = the
last clock of the Nth later bundle session, walking forward) with the
underlying's spot as the mark; the underlying has no expiry, so a target
past the data end is unresolved (never scored). The entry spot is the
as-of parity spot (prints at or before as-of: the feature side); the exit
spot is the same parity estimate built ONLY from prints strictly after the
as-of instant (:func:`exit_spot`).

Scoring (:func:`score_set`): Brier, log loss (clipped to [0.01, 0.99]), Brier
skill vs climatology (the TRAIN base rate per underlying x horizon) and vs
a momentum baseline p = 0.5 + k*sign(ret_5s) (k per horizon fit on TRAIN),
Murphy reliability/resolution/uncertainty, reliability bins, and
moving-block session bootstrap CIs (5-session blocks for hold:5 and the
pooled set: hold:5 labels overlap five sessions). TRAIN = sessions <= the
cutoff (2026-08-14), TEST = after.

Decision (:func:`decide_ev`, no LLM): approximate expected net of a row at
horizon h = p_win*a*max_gain - (1-p_win)*b*max_loss - cost, where p_win is
the forecast in the row's direction and a/b (per structure x horizon) are
the mean realized fractions of max gain / max loss when the direction was
right / wrong on TRAIN boards; the best (row, horizon) is entered only if
its edge |p-0.5| >= tau and EV > 0. :func:`decide_direction` is the
forecast-only-direction variant: the strongest view picks underlying +
direction + horizon, the harness's own row choice (``longrun._best``) picks
the row. In the model arm tau is pre-registered; the report's DERIVED arms
fit tau on TRAIN forecasts only and sit on the same paired scoreboard.

Cost (live smokes 2026-09-29, 24 boards x 2 repeats): at the provider
default reasoning_effort (max) the ~700-token prompt drew ~4,600 completion
tokens, p50 62 s and 16/27 timeouts at 120 s; at ``effort: low`` 48/48
parsed, p50 7.6 s / p90 13.5 s, ~470 completion tokens. Use effort low.

Evidence, not authority: PAPER research only; nothing here promotes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import statistics
import sys
import threading
import time
from bisect import bisect_right
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np

from tree_options.desk import intraday_action_graph as iag
from tree_options.desk import longrun, outcomes
from tree_options.desk.longrun import Arm, Board, PluginContext, PolicySpec, Protocol
from tree_options.trex.discovery import llm

FORECAST_SCHEMA = "desk-forecast/1"
REPORT_SCHEMA = "desk-forecast-report/1"
FORECAST_HORIZONS = ("intraday", "eod", "hold:5")
DEFAULT_CUTOFF = "2026-08-14"
DEFAULT_SEED = 20260929
DEFAULT_TAU = 0.05
DECISIONS = ("ev", "direction", "none")
LOGLOSS_EPS = 0.01
BINS = 10
TAU_GRID = tuple(i / 100 for i in range(21))
#: moving-block length in sessions: hold:5 labels overlap five sessions
BLOCKS = {"intraday": 1, "eod": 1, "hold:5": 5, "all": 5}
MOMENTUM_K_MAX = 0.45
BPS_MAX = 10_000.0
#: decisions use p clipped to [0.5 - cap, 0.5 + cap]: one degenerate reply
#: (a 0.0 / 1.0) cannot buy an outsized EV; scoring always uses the raw p
EDGE_CAP = 0.25
#: MiniMax-M3.1 reasoning_effort values (always-on thinking; the default is max)
EFFORTS = ("low", "medium", "high", "xhigh", "max")
FORECAST_SENTENCE = (
    "You are a calibrated probabilistic forecaster for a paper-trading research desk.")
_INF = Decimal("Infinity")


def snapshot_id(day: date, clock: str) -> str:
    return f"s:{day.isoformat()}T{clock}"


def aliases_of(index: outcomes.OutcomeIndex) -> dict[str, str]:
    """lab.board_context's alias map: sorted tickers -> U1, U2, ..."""
    return {underlying: f"U{i + 1}" for i, underlying in enumerate(index.underlyings)}


# ------------------------------------------------------------------ labels


@dataclass(frozen=True)
class Label:
    """``y`` = 1 when the exit spot is STRICTLY higher than the as-of spot."""

    y: int
    ret_bps: float
    entry: Decimal
    exit: Decimal
    exit_slot: int


def _print_after(points: list[tuple[datetime, Decimal]], after: datetime | None,
                 at: datetime) -> Decimal | None:
    """iag._latest(points, at, SPOT_AGE_S), restricted to prints strictly
    after ``after`` (None: no lower bound)."""
    i = bisect_right(points, (at, _INF)) - 1
    if i < 0:
        return None
    stamp, price = points[i]
    if after is not None and stamp <= after:
        return None
    if stamp.astimezone(iag.ET).date() != at.astimezone(iag.ET).date():
        return None
    return price if (at - stamp).total_seconds() <= outcomes.SPOT_AGE_S else None


def exit_spot(index: outcomes.OutcomeIndex, after: datetime | None,
              at: datetime) -> dict[str, Decimal]:
    """outcomes' parity spot at ``at`` (C - P + K, median over the nearest
    unexpired expiry with fresh pairs) from prints strictly after ``after``."""
    if at.tzinfo is None or (after is not None and not after < at):
        raise ValueError("an aware exit instant strictly after the as-of instant is required")
    today = at.astimezone(iag.ET).date()
    by: dict[str, dict[date, list[Decimal]]] = {}
    for (underlying, expiry, strike), (call, put) in index.parity_pairs.items():
        if expiry < today:
            continue
        call_price = _print_after(index.bars[call], after, at)
        put_price = _print_after(index.bars[put], after, at)
        if call_price is None or put_price is None:
            continue
        by.setdefault(underlying, {}).setdefault(expiry, []).append(call_price - put_price + strike)
    return {u: statistics.median(per[min(per)]) for u, per in sorted(by.items())}


class Spots:
    """Memoized entry (as-of) and exit-side spots of one index."""

    def __init__(self, index: outcomes.OutcomeIndex) -> None:
        self.index = index
        self._entry: dict[int, dict[str, Decimal]] = {}
        self._exit: dict[tuple[int, datetime | None], dict[str, Decimal]] = {}

    def entry(self, slot: int) -> dict[str, Decimal]:
        if slot not in self._entry:
            self._entry[slot] = outcomes.spot_asof(self.index, self.index.timeline[slot][2])
        return self._entry[slot]

    def exit(self, slot: int, after: datetime) -> dict[str, Decimal]:
        at = self.index.timeline[slot][2]
        if not after < at:
            raise ValueError("an exit clock must be strictly after the as-of instant")
        # the lower bound only binds inside the freshness window (memo sharing)
        bound = after if (at - after).total_seconds() <= outcomes.SPOT_AGE_S else None
        key = (slot, bound)
        if key not in self._exit:
            self._exit[key] = exit_spot(self.index, bound, at)
        return self._exit[key]


def exit_slot(index: outcomes.OutcomeIndex, slot: int, horizon: str,
              marked: Callable[[int], bool]) -> int | None:
    """outcomes._exit's clock rule for ``horizon`` with ``marked`` as the
    mark test; no expiry cap (an underlying has none): None = unresolved."""
    if horizon not in outcomes.EXIT_MODES or horizon == "expiry":
        raise ValueError(f"not a forecastable exit mode: {horizon!r}")
    day = index.timeline[slot][0]
    if horizon == "intraday":
        start = slot + 1
    elif horizon == "eod":
        for later in range(index.last_slot[day], slot, -1):
            if marked(later):
                return later
        start = index.last_slot[day] + 1
    else:
        target = index.session_pos[day] + int(horizon.split(":", 1)[1])
        if target >= len(index.sessions):
            return None
        start = index.last_slot[index.sessions[target]]
    for later in range(start, len(index.timeline)):
        if marked(later):
            return later
    return None


def board_labels(index: outcomes.OutcomeIndex, slot: int, spots: Spots | None = None,
                 horizons: Sequence[str] = FORECAST_HORIZONS
                 ) -> dict[str, dict[str, Label | None]]:
    """{alias: {horizon: Label | None}} of one board. The exit side reads
    only prints strictly after the board's as-of instant."""
    spots = spots or Spots(index)
    as_of = index.timeline[slot][2]
    entry = spots.entry(slot)
    out: dict[str, dict[str, Label | None]] = {}
    for underlying, alias in aliases_of(index).items():
        per: dict[str, Label | None] = {}
        start = entry.get(underlying)

        def marked(later: int, underlying: str = underlying) -> bool:
            return underlying in spots.exit(later, as_of)

        for horizon in horizons:
            if start is None:
                per[horizon] = None
                continue
            found = exit_slot(index, slot, horizon, marked)
            if found is None:
                per[horizon] = None
                continue
            end = spots.exit(found, as_of)[underlying]
            per[horizon] = Label(y=int(end > start), ret_bps=float((end / start - 1) * 10_000),
                                 entry=start, exit=end, exit_slot=found)
        out[alias] = per
    return out


def label_table(index: outcomes.OutcomeIndex, horizons: Sequence[str] = FORECAST_HORIZONS
                ) -> dict[str, dict[str, dict[str, Label | None]]]:
    """Every scheduled board of the index: {snapshot: {alias: {horizon: Label | None}}}."""
    spots = Spots(index)
    return {snapshot_id(day, clock): board_labels(index, slot, spots, horizons)
            for slot, (day, clock, _at) in enumerate(index.timeline)}


# ------------------------------------------------------- prompt and parsing


class ForecastError(ValueError):
    """A reply that breaks the forecast contract (rejected, never repaired)."""


def permutation(seed: int, snapshot: str, repeat: int, items: Sequence[str]) -> list[str]:
    """The seeded per-(board, repeat) display order (process-independent)."""
    digest = hashlib.sha256(f"{seed}|{snapshot}|{repeat}".encode()).digest()
    order = list(items)
    random.Random(int.from_bytes(digest[:8], "big")).shuffle(order)
    return order


def shown_labels(count: int) -> list[str]:
    return [f"U{k + 1}" for k in range(count)]


def forecast_prompt(context: Mapping[str, Any], perm: Sequence[str],
                    sentence: str | None = None) -> list[dict[str, str]]:
    """The forecast task. Display label U{k+1} is the canonical underlying
    ``perm[k]``; only the public context is rendered (no rows)."""
    per = context["underlyings"]
    shown = {label: per[canonical] for label, canonical in zip(shown_labels(len(perm)), perm,
                                                                strict=True)}
    if context.get("time_of_day") == "close":  # lab's bucket for the session's LAST clock
        exits = ("Exits for THIS question (it is the session's last decision clock): intraday "
                 "and eod both = the next session's first decision clock (tomorrow morning); ")
    else:
        exits = ("Exits: intraday = the next decision clock (about 45 minutes later); eod = "
                 "this session's last decision clock (later today); ")
    task = (
        (sentence or FORECAST_SENTENCE)
        + " The underlyings are broad US equity index ETFs under arbitrary aliases (U1, U2, "
        "...), relabelled and reordered for every question. For EACH underlying and EACH "
        "exit give the probability that its price at the exit is STRICTLY HIGHER than now. "
        + exits + "hold:5 = the last decision clock of the fifth following session. "
        "The context gives each "
        "underlying's as-of returns over 1/5/20 sessions and its 20-session annualized "
        "realized vol, in percent (null = not enough history), and the time of day. You are "
        "scored by Brier score and log loss over thousands of such questions: 0.5 means no "
        "view and confident misses are expensive. exp_ret_bps (optional) is your expected "
        "return to each exit in basis points. Return STRICT JSON "
        '{"p_up": {"U1": {"intraday": <p>, "eod": <p>, "hold:5": <p>}, ... one entry per '
        'underlying}, "exp_ret_bps": {same shape, optional}, "note": "<=40 chars"}. '
        "Numbers only; no other text.")
    rendered = {"time_of_day": context.get("time_of_day"),
                "session_ordinal": context.get("session_ordinal"), "underlyings": shown}
    return [{"role": "user", "content": json.dumps({"task": task, "context": rendered})}]


def _prob(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ForecastError(f"probability must be a number, got {type(value).__name__}")
    p = float(value)
    if not (math.isfinite(p) and 0.0 <= p <= 1.0):
        raise ForecastError("probability outside [0, 1]")
    return p


def _bps(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ForecastError("expected return must be a number")
    x = float(value)
    if not (math.isfinite(x) and abs(x) <= BPS_MAX):
        raise ForecastError("expected return out of range")
    return x


def parse_forecast(reply: Mapping[str, Any], perm: Sequence[str],
                   horizons: Sequence[str] = FORECAST_HORIZONS
                   ) -> tuple[dict[str, dict[str, float]], dict[str, dict[str, float]] | None]:
    """(p_up, exp_ret_bps | None) keyed by CANONICAL alias. p_up must name
    exactly the shown underlyings with every horizon (extra keys ignored);
    an invalid optional exp_ret_bps block is dropped, never repaired."""
    shown = shown_labels(len(perm))
    block = reply.get("p_up")
    if not isinstance(block, dict) or set(block) != set(shown):
        raise ForecastError(f"p_up must name exactly {shown}")
    p_up: dict[str, dict[str, float]] = {}
    for label, canonical in zip(shown, perm, strict=True):
        per = block[label]
        if not isinstance(per, dict) or any(h not in per for h in horizons):
            raise ForecastError(f"{label}: every horizon {list(horizons)} is required")
        p_up[canonical] = {h: _prob(per[h]) for h in horizons}
    exp: dict[str, dict[str, float]] | None = None
    raw = reply.get("exp_ret_bps")
    if isinstance(raw, dict) and set(raw) == set(shown):
        try:
            exp = {canonical: {h: _bps(raw[label][h]) for h in horizons}
                   for label, canonical in zip(shown, perm, strict=True)}
        except (ForecastError, KeyError, TypeError):
            exp = None
    return p_up, exp


# ------------------------------------------------------------------ decision


def edge(p: float) -> float:
    """|p - 0.5|, rounded so 0.45 and 0.55 carry the same edge."""
    return round(abs(p - 0.5), 9)


def cap_views(p_up: Mapping[str, Mapping[str, float]],
              cap: float) -> dict[str, dict[str, float]]:
    """The decision-side views: every p clipped to [0.5 - cap, 0.5 + cap]."""
    if not 0 < cap <= 0.5:
        raise ValueError("edge_cap must be in (0, 0.5]")
    return {u: {h: min(0.5 + cap, max(0.5 - cap, float(p))) for h, p in by_h.items()}
            for u, by_h in p_up.items()}


def fit_payoff_map(index: outcomes.OutcomeIndex,
                   labels: Mapping[str, Mapping[str, Mapping[str, Label | None]]],
                   outcome: longrun.OutcomeFn, cutoff: str,
                   horizons: Sequence[str] = FORECAST_HORIZONS) -> dict[str, dict[str, Any]]:
    """Per structure x horizon on TRAIN boards (session <= cutoff): ``a`` =
    mean gross / max_gain when the row's direction was right, ``b`` = mean
    -gross / max_loss when it was wrong (a tie is "not higher")."""
    alias = aliases_of(index)
    wins: dict[tuple[str, str], list[float]] = {}
    losses: dict[tuple[str, str], list[float]] = {}
    for day, clock, _at in index.timeline:
        if day.isoformat() > cutoff:
            continue
        snapshot = snapshot_id(day, clock)
        for cand in outcomes.board_candidates(index, day, clock):
            gain, loss = float(cand["max_gain_proxy"]), float(cand["max_loss_proxy"])
            if gain <= 0 or loss <= 0:
                continue
            bullish = outcomes.direction(cand["structure"]) == "bullish"
            for horizon in horizons:
                label = labels.get(snapshot, {}).get(alias[cand["underlying"]], {}).get(horizon)
                got = outcome(snapshot, cand["id"], horizon) if label is not None else None
                if label is None or got is None:
                    continue
                gross = float(got["gross"])
                key = (cand["structure"], horizon)
                if (label.y == 1) == bullish:
                    wins.setdefault(key, []).append(gross / gain)
                else:
                    losses.setdefault(key, []).append(-gross / loss)
    table: dict[str, dict[str, Any]] = {}
    for structure in outcomes.STRUCTURES:
        table[structure] = {}
        for horizon in horizons:
            won, lost = wins.get((structure, horizon), []), losses.get((structure, horizon), [])
            table[structure][horizon] = {
                "a": round(float(np.mean(won)), 6) if won else 0.0,
                "b": round(float(np.mean(lost)), 6) if lost else 0.0,
                "n_win": len(won), "n_loss": len(lost)}
    return table


def expected_net(row: Mapping[str, Any], p_up: float, payoff: Mapping[str, Any],
                 cost: float) -> float | None:
    """p_win*a*max_gain - (1-p_win)*b*max_loss - cost (None: no economics)."""
    gain, loss = longrun._num(row, "max_gain"), longrun._num(row, "max_loss")
    if gain is None or loss is None or gain <= 0 or loss <= 0:
        return None
    p_win = p_up if longrun.is_bullish(row) else 1.0 - p_up
    return p_win * float(payoff["a"]) * gain - (1.0 - p_win) * float(payoff["b"]) * loss - cost


def round_trip_cost() -> float:
    return float(outcomes.CostModel().round_trip())


def decide_ev(rows: Sequence[Mapping[str, Any]], p_up: Mapping[str, Mapping[str, float]],
              payoff: Mapping[str, Mapping[str, Any]], tau: float | None, cost: float,
              horizons: Sequence[str] = FORECAST_HORIZONS
              ) -> tuple[str | None, str | None, dict[str, Any]]:
    """The best (row, horizon) by approximate expected net; enter only when
    its view's edge >= tau and EV > 0 (tau None: never enter)."""
    detail: dict[str, Any] = {"rule": "ev", "tau": tau}
    if tau is None:
        return None, None, {**detail, "reason": "no_tau"}
    best: tuple[tuple[float, str, int], Mapping[str, Any], str, float, float] | None = None
    for row in rows:
        underlying, structure = row.get("underlying"), row.get("structure")
        if underlying not in p_up or structure not in payoff:
            continue
        for h_index, horizon in enumerate(horizons):
            p = p_up[str(underlying)][horizon]
            if edge(p) < tau:
                continue
            ev = expected_net(row, p, payoff[str(structure)][horizon], cost)
            if ev is None:
                continue
            key = (-ev, str(row["id"]), h_index)
            if best is None or key < best[0]:
                best = (key, row, horizon, ev, edge(p))
    if best is None:
        return None, None, {**detail, "reason": "no_view_above_tau"}
    _key, row, horizon, ev, view = best
    detail.update(ev=round(ev, 2), edge=view)
    if ev <= 0:
        return None, None, {**detail, "reason": "ev_not_positive"}
    return str(row["id"]), horizon, detail


def decide_direction(board: Board, p_up: Mapping[str, Mapping[str, float]], tau: float | None,
                     horizons: Sequence[str] = FORECAST_HORIZONS, key: str = "board_order"
                     ) -> tuple[str | None, str | None, dict[str, Any]]:
    """Forecast-only direction: the strongest view (edge >= tau) picks the
    underlying, direction and horizon; the harness's own row choice
    (``longrun._best`` by ``key``) picks the row; a view without a row in
    its direction falls through to the next view."""
    detail: dict[str, Any] = {"rule": "direction", "tau": tau}
    if tau is None:
        return None, None, {**detail, "reason": "no_tau"}
    views = sorted(((edge(p_up[u][h]), u, i, h) for u in sorted(p_up)
                    for i, h in enumerate(horizons)), key=lambda v: (-v[0], v[1], v[2]))
    for view, underlying, _i, horizon in views:
        if view < tau or view == 0:
            break
        bullish = p_up[underlying][horizon] > 0.5

        def keep(row: dict[str, Any], u: str = underlying, b: bool = bullish) -> bool:
            return row.get("underlying") == u and longrun.is_bullish(row) == b

        choice = longrun._best(board, keep, key)
        if choice is not None:
            return choice, horizon, {**detail, "edge": view, "underlying": underlying,
                                     "direction": "bullish" if bullish else "bearish"}
    return None, None, {**detail, "reason": "no_view_above_tau"}


Decider = Callable[[Board, Mapping[str, Mapping[str, float]], float | None],
                   tuple[str | None, str | None, dict[str, Any]]]


def fit_tau(train: Sequence[tuple[Board, Mapping[str, Mapping[str, float]]]], decide: Decider,
            net: Callable[[str, str, str | None], float],
            grid: Sequence[float] = TAU_GRID) -> dict[str, Any]:
    """tau maximizing the TRAIN net total (ties: the larger tau); a best
    total <= 0 means no train edge: tau None (the arm never enters)."""
    rows: list[dict[str, Any]] = []
    for tau in grid:
        total, entries = 0.0, 0
        for board, p_up in train:
            choice, horizon, _ = decide(board, p_up, tau)
            if choice is not None:
                entries += 1
                total += net(board.snapshot, choice, horizon)
        rows.append({"tau": tau, "train_net": round(total, 2), "entries": entries})
    if not rows:
        return {"tau": None, "reason": "empty grid", "grid": rows}
    best = max(rows, key=lambda r: (r["train_net"], r["tau"]))
    fitted = best["tau"] if best["train_net"] > 0 else None
    return {"tau": fitted, "train_net": best["train_net"], "train_boards": len(train),
            "reason": "best train net" if fitted is not None else "no positive train net",
            "grid": rows}


# ------------------------------------------------------------------ scoring


def brier(p: np.ndarray, y: np.ndarray) -> float:
    return float(np.mean((p - y) ** 2))


def log_loss(p: np.ndarray, y: np.ndarray, eps: float = LOGLOSS_EPS) -> float:
    q = np.clip(p, eps, 1.0 - eps)
    return float(np.mean(-(y * np.log(q) + (1.0 - y) * np.log(1.0 - q))))


def murphy(p: np.ndarray, y: np.ndarray, bins: int = BINS) -> dict[str, Any]:
    """Murphy decomposition over ``bins`` equal-width probability bins:
    Brier = reliability - resolution + uncertainty + residual (the residual
    is the within-bin spread of p; 0 when p is constant inside every bin)."""
    n = len(p)
    if n == 0:
        return {"n": 0}
    idx = np.minimum((p * bins).astype(int), bins - 1)
    ybar = float(np.mean(y))
    rel = res = 0.0
    table: list[dict[str, Any]] = []
    for k in range(bins):
        mask = idx == k
        n_k = int(mask.sum())
        if n_k == 0:
            continue
        p_k, y_k = float(np.mean(p[mask])), float(np.mean(y[mask]))
        rel += n_k * (p_k - y_k) ** 2
        res += n_k * (y_k - ybar) ** 2
        table.append({"bin": [k / bins, (k + 1) / bins], "n": n_k, "mean_p": round(p_k, 4),
                      "freq": round(y_k, 4)})
    rel, res, unc = rel / n, res / n, ybar * (1.0 - ybar)
    score = brier(p, y)
    return {"n": n, "brier": round(score, 6), "reliability": round(rel, 6),
            "resolution": round(res, 6), "uncertainty": round(unc, 6),
            "residual": round(score - (rel - res + unc), 6), "bins": table}


def block_bootstrap(per_session: np.ndarray, block: int, draws: int, seed: int) -> np.ndarray:
    """Moving-block bootstrap of session rows (in date order): ``draws``
    resamples of the column sums, each built from contiguous blocks."""
    sessions = per_session.shape[0]
    size = max(1, min(block, sessions))
    blocks = math.ceil(sessions / size)
    rng = np.random.default_rng(seed)
    starts = rng.integers(0, sessions - size + 1, size=(draws, blocks))
    idx = (starts[:, :, None] + np.arange(size)).reshape(draws, blocks * size)[:, :sessions]
    return np.asarray(per_session[idx].sum(axis=1))


def _ci(values: np.ndarray) -> list[float] | None:
    finite = values[np.isfinite(values)]
    if finite.size == 0:
        return None
    lo, hi = np.percentile(finite, [2.5, 97.5])
    return [round(float(lo), 6), round(float(hi), 6)]


def score_set(p: np.ndarray, y: np.ndarray, p_clim: np.ndarray, p_mom: np.ndarray,
              session: np.ndarray, *, block: int, draws: int, seed: int) -> dict[str, Any]:
    """One scored set: point scores plus block-bootstrap 95% CIs over the
    sessions present (``session`` = date-ordered session positions)."""
    n = len(p)
    if n == 0:
        return {"n": 0}
    se, se_clim, se_mom = (p - y) ** 2, (p_clim - y) ** 2, (p_mom - y) ** 2
    b, b_clim, b_mom = float(se.mean()), float(se_clim.mean()), float(se_mom.mean())
    decided = p != 0.5
    out: dict[str, Any] = {
        "n": n, "base_rate": round(float(y.mean()), 4), "mean_p": round(float(p.mean()), 4),
        "brier": round(b, 6), "brier_clim": round(b_clim, 6), "brier_mom": round(b_mom, 6),
        "bss_clim": round(1 - b / b_clim, 6) if b_clim > 0 else None,
        "bss_mom": round(1 - b / b_mom, 6) if b_mom > 0 else None,
        "log_loss": round(log_loss(p, y), 6), "log_loss_clim": round(log_loss(p_clim, y), 6),
        "log_loss_mom": round(log_loss(p_mom, y), 6),
        "hit_rate": (round(float(np.mean((p[decided] > 0.5) == (y[decided] == 1))), 4)
                     if decided.any() else None),
        "murphy": murphy(p, y)}
    present = np.unique(session)
    rows = np.zeros((len(present), 4))
    np.add.at(rows, np.searchsorted(present, session),
              np.column_stack([np.ones(n), se, se_clim, se_mom]))
    boot = block_bootstrap(rows, block, draws, seed)
    with np.errstate(divide="ignore", invalid="ignore"):
        out["ci95"] = {"brier": _ci(boot[:, 1] / boot[:, 0]),
                       "bss_clim": _ci(1 - boot[:, 1] / boot[:, 2]),
                       "bss_mom": _ci(1 - boot[:, 1] / boot[:, 3]),
                       "brier_minus_clim": _ci((boot[:, 1] - boot[:, 2]) / boot[:, 0])}
    out["sessions"], out["block"] = len(present), max(1, min(block, len(present)))
    return out


def fit_climatology(labels: Mapping[str, Mapping[str, Mapping[str, Label | None]]],
                    sessions: Mapping[str, str], cutoff: str,
                    horizons: Sequence[str] = FORECAST_HORIZONS) -> dict[str, dict[str, float]]:
    """The TRAIN base rate per underlying x horizon (0.5 without data)."""
    counts: dict[tuple[str, str], list[int]] = {}
    for snapshot, per in labels.items():
        if sessions[snapshot] > cutoff:
            continue
        for alias, by_h in per.items():
            for horizon in horizons:
                label = by_h.get(horizon)
                if label is not None:
                    counts.setdefault((alias, horizon), []).append(label.y)
    aliases = sorted({alias for per in labels.values() for alias in per})
    return {alias: {h: (round(float(np.mean(counts[(alias, h)])), 6)
                        if counts.get((alias, h)) else 0.5) for h in horizons}
            for alias in aliases}


def momentum_sign(context: Mapping[str, Any] | None, alias: str) -> int:
    """sign(ret_5s_pct) of an alias in a public context (0 when unknown)."""
    try:
        value = float((context or {})["underlyings"][alias]["ret_5s_pct"])
    except (KeyError, TypeError, ValueError):
        return 0
    return 0 if not math.isfinite(value) or value == 0 else (1 if value > 0 else -1)


def fit_momentum(samples: Mapping[str, Sequence[tuple[int, int]]]) -> dict[str, float]:
    """Per horizon, k minimizing the TRAIN Brier of 0.5 + k*s (closed form
    sum(s*(y-0.5)) / sum(s^2)), clipped to +-MOMENTUM_K_MAX."""
    out: dict[str, float] = {}
    for horizon, pairs in samples.items():
        s = np.array([p[0] for p in pairs], dtype=float)
        y = np.array([p[1] for p in pairs], dtype=float)
        denom = float(np.sum(s * s))
        k = float(np.sum(s * (y - 0.5)) / denom) if denom > 0 else 0.0
        out[horizon] = round(max(-MOMENTUM_K_MAX, min(MOMENTUM_K_MAX, k)), 6)
    return out


# ------------------------------------------------------------ shared state


def fit_state(ctx: PluginContext, cutoff: str) -> dict[str, Any]:
    """Labels + TRAIN-only fits (climatology, momentum k, payoff map),
    computed once per plug-in context and cutoff."""
    cache = ctx.shared.setdefault("forecast_fit", {})
    if cutoff in cache:
        return dict(cache[cutoff])
    date.fromisoformat(cutoff)
    state = ctx.shared.get("v2")
    if state is None:
        raise ValueError("the forecast plug-ins need the v2 boards plug-in")
    index: outcomes.OutcomeIndex = state["index"]
    labels = label_table(index)
    sessions = {snapshot_id(day, clock): day.isoformat() for day, clock, _ in index.timeline}
    samples: dict[str, list[tuple[int, int]]] = {h: [] for h in FORECAST_HORIZONS}
    for snapshot, per in labels.items():
        if sessions[snapshot] > cutoff:
            continue
        context = state["contexts"].get(snapshot)
        public = None if context is None else context.public
        for alias, by_h in per.items():
            for horizon, label in by_h.items():
                if label is not None:
                    samples[horizon].append((momentum_sign(public, alias), label.y))
    outcome = ctx.shared.get("outcome")
    fitted = {"cutoff": cutoff, "labels": labels, "sessions": sessions,
              "climatology": fit_climatology(labels, sessions, cutoff),
              "momentum_k": fit_momentum(samples),
              "payoff": (None if outcome is None
                         else fit_payoff_map(index, labels, outcome, cutoff))}
    cache[cutoff] = fitted
    return dict(fitted)


# ------------------------------------------------------------------ ask


class ForecastAsk:
    """The ``forecast`` ask plug-in: one forecast call per (board, repeat),
    a deterministic decision, and the raw forecast on the receipt."""

    wants_arm = True  # longrun.call_ask passes the arm: its repeat seeds the order

    def __init__(self, *, provider: str, transport: Any, seed: int, decision: str,
                 tau: float | None, payoff: Mapping[str, Any] | None, key: str = "board_order",
                 cost: float | None = None, edge_cap: float = EDGE_CAP,
                 effort: str | None = None, max_tokens: int | None = None,
                 timeout: float | None = None) -> None:
        if decision not in DECISIONS:
            raise ValueError(f"decision must be one of {DECISIONS}")
        if decision == "ev" and payoff is None:
            raise ValueError("decision 'ev' needs the payoff map (an outcome plug-in)")
        if effort is not None and effort not in EFFORTS:
            raise ValueError(f"effort must be one of {EFFORTS}")
        if (timeout is not None and not 0 < timeout <= 900) or (
                max_tokens is not None and not 0 < max_tokens <= 64000):
            raise ValueError("timeout must be in (0, 900] s and max_tokens in (0, 64000]")
        self.provider, self.transport, self.seed = provider, transport, seed
        self.decision, self.tau, self.payoff, self.key = decision, tau, payoff, key
        self.cost = round_trip_cost() if cost is None else cost
        self.edge_cap, self.effort = edge_cap, effort
        self.max_tokens, self.timeout = max_tokens, timeout

    def decide(self, board: Board, p_up: Mapping[str, Mapping[str, float]]
               ) -> tuple[str | None, str | None, dict[str, Any]]:
        views = cap_views(p_up, self.edge_cap)
        if self.decision == "ev" and self.payoff is not None:
            choice, horizon, detail = decide_ev(board.rows, views, self.payoff, self.tau,
                                                self.cost)
        elif self.decision == "direction":
            choice, horizon, detail = decide_direction(board, views, self.tau, key=self.key)
        else:
            choice, horizon, detail = None, None, {"rule": "none"}
        return choice, horizon, {**detail, "edge_cap": self.edge_cap}

    def __call__(self, spec: PolicySpec, board: Board, arm: Arm
                 ) -> tuple[str | None, str | None, str, dict[str, Any]]:
        context = board.context or {}
        if not isinstance(context.get("underlyings"), dict) or not context["underlyings"]:
            raise ForecastError("the board carries no public context")
        perm = permutation(self.seed, board.snapshot, arm.repeat, sorted(context["underlyings"]))
        messages = forecast_prompt(context, perm, spec.prompt)
        kwargs: dict[str, Any] = {}
        if self.transport is not None:
            kwargs["transport"] = self.transport
        extra: dict[str, Any] = {}
        if self.effort is not None:
            extra["reasoning_effort"] = self.effort
        if self.max_tokens is not None:
            extra["max_tokens"] = self.max_tokens
        if extra:
            kwargs["extra"] = extra
        if self.timeout is not None:
            kwargs["timeout"] = self.timeout
        provider = spec.provider or self.provider
        escalated: dict[str, Any] | None = None
        try:
            reply, model = llm.chat_json(provider, messages, **kwargs)
        except llm.LlmError as error:  # one escalating retry, then the failure stands
            if llm.TRUNCATED_NOTE not in str(error):
                raise
            retry_extra, seconds, tokens = llm.escalation_budget(
                provider, max_tokens=self.max_tokens, timeout=self.timeout,
                extra=kwargs.get("extra"))
            reply, model = llm.chat_json(provider, messages,
                                         **{**kwargs, "extra": retry_extra,
                                            "timeout": seconds})
            escalated = {"escalated": True, "max_tokens": tokens, "timeout": seconds}
        p_up, exp = parse_forecast(reply, perm)
        choice, horizon, detail = self.decide(board, p_up)
        forecast = {"schema": FORECAST_SCHEMA, "model": model, "seed": self.seed,
                    "effort": self.effort,
                    "perm": perm, "p_up": p_up, "exp_ret_bps": exp, "decision": detail,
                    "prompt_sha256": hashlib.sha256(
                        messages[0]["content"].encode()).hexdigest()}
        receipt_extra = {"forecast": forecast}
        if escalated is not None:
            receipt_extra = {**escalated, "forecast": forecast}
        return choice, horizon, str(reply.get("note", ""))[:60], receipt_extra


def _tau_param(value: Any) -> float | None:
    return None if value is None else float(value)


def ask_plugin(params: Mapping[str, Any], ctx: PluginContext) -> ForecastAsk:
    """Params: provider (minimax-flash), effort (M3.1 reasoning_effort; None
    = the provider default, max), decision (ev | direction | none), tau
    (pre-registered edge threshold), edge_cap, seed, cutoff (the payoff
    map's TRAIN split), key (the direction variant's row order), timeout (s)
    and max_tokens (per-call budget overrides over the provider spec)."""
    decision = str(params.get("decision", "ev"))
    cutoff = str(params.get("cutoff", DEFAULT_CUTOFF))
    payoff = fit_state(ctx, cutoff)["payoff"] if decision == "ev" else None
    effort = params.get("effort")
    return ForecastAsk(provider=str(params.get("provider", "minimax-flash")),
                       transport=ctx.shared.get("transport"),
                       seed=int(params.get("seed", DEFAULT_SEED)), decision=decision,
                       tau=_tau_param(params.get("tau", DEFAULT_TAU)), payoff=payoff,
                       key=str(params.get("key", "board_order")),
                       edge_cap=float(params.get("edge_cap", EDGE_CAP)),
                       effort=None if effort is None else str(effort),
                       max_tokens=(None if params.get("max_tokens") is None
                                   else int(params["max_tokens"])),
                       timeout=(None if params.get("timeout") is None
                                else float(params["timeout"])))


# ------------------------------------------------------------------ report


def forecast_receipt(rec: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not rec or not rec.get("ok"):
        return None
    forecast = rec.get("forecast")
    if not isinstance(forecast, dict) or not isinstance(forecast.get("p_up"), dict):
        return None
    return forecast


def _records(arm: Arm, boards: Sequence[Board], receipts: Mapping[str, Mapping[str, Any]],
             state: Mapping[str, Any], order: Mapping[str, int]) -> dict[str, np.ndarray]:
    """Flat arrays of every labeled forecast of one arm on ``boards``."""
    cols: dict[str, list[Any]] = {k: [] for k in (
        "snapshot", "alias", "horizon", "p", "y", "p_clim", "p_mom", "session", "train",
        "position")}
    for board in boards:
        forecast = forecast_receipt(receipts.get(board.snapshot))
        if forecast is None:
            continue
        perm = list(forecast.get("perm") or [])
        for alias, by_h in forecast["p_up"].items():
            for horizon, p in by_h.items():
                label = state["labels"].get(board.snapshot, {}).get(alias, {}).get(horizon)
                if label is None:
                    continue
                sign = momentum_sign(board.context, alias)
                cols["snapshot"].append(board.snapshot)
                cols["alias"].append(alias)
                cols["horizon"].append(horizon)
                cols["p"].append(float(p))
                cols["y"].append(label.y)
                cols["p_clim"].append(state["climatology"].get(alias, {}).get(horizon, 0.5))
                cols["p_mom"].append(0.5 + state["momentum_k"].get(horizon, 0.0) * sign)
                cols["session"].append(order[board.session])
                cols["train"].append(board.session <= state["cutoff"])
                cols["position"].append(perm.index(alias) if alias in perm else -1)
    return {k: np.array(v) for k, v in cols.items()}


def _score_arm(rows: Mapping[str, np.ndarray], *, draws: int, seed: int) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if len(rows["p"]) == 0:
        return out
    for split, mask in (("test", ~rows["train"].astype(bool)), ("train", rows["train"].astype(bool))):
        out[split] = {}
        for horizon in ("all", *FORECAST_HORIZONS):
            sel = mask if horizon == "all" else mask & (rows["horizon"] == horizon)
            out[split][horizon] = score_set(
                rows["p"][sel].astype(float), rows["y"][sel].astype(float),
                rows["p_clim"][sel].astype(float), rows["p_mom"][sel].astype(float),
                rows["session"][sel].astype(int), block=BLOCKS[horizon], draws=draws, seed=seed)
    return out


def _position_bias(rows: Mapping[str, np.ndarray]) -> list[dict[str, Any]]:
    out = []
    for k in sorted({int(v) for v in rows["position"]} - {-1}):
        sel = rows["position"] == k
        p, y = rows["p"][sel].astype(float), rows["y"][sel].astype(float)
        out.append({"shown_as": f"U{k + 1}", "n": int(sel.sum()),
                    "mean_p": round(float(p.mean()), 4), "base_rate": round(float(y.mean()), 4),
                    "mean_p_minus_y": round(float((p - y).mean()), 4)})
    return out


def order_check(a: Mapping[str, np.ndarray], b: Mapping[str, np.ndarray],
                same_order: Mapping[str, bool], *, draws: int, seed: int) -> dict[str, Any]:
    """A/A on order sensitivity: the two repeats' forecasts on the same
    (board, underlying, horizon). Boards whose seeded orders coincide
    measure sampling noise alone; the rest add any order effect."""
    key_a = {(s, u, h): i for i, (s, u, h) in enumerate(zip(a["snapshot"], a["alias"],
                                                             a["horizon"], strict=True))}
    pairs = [(key_a[k], j) for j, k in enumerate(zip(b["snapshot"], b["alias"], b["horizon"],
                                                     strict=True)) if k in key_a]
    if not pairs:
        return {"status": "not_run", "reason": "no paired forecasts"}
    ia, ib = np.array([p[0] for p in pairs]), np.array([p[1] for p in pairs])
    pa, pb = a["p"][ia].astype(float), b["p"][ib].astype(float)
    y = a["y"][ia].astype(float)
    same = np.array([bool(same_order.get(str(s))) for s in a["snapshot"][ia]])
    diff = np.abs(pa - pb)
    sided = (pa != 0.5) & (pb != 0.5)
    se_diff = (pa - y) ** 2 - (pb - y) ** 2
    session = a["session"][ia].astype(int)
    present = np.unique(session)
    rows = np.zeros((len(present), 2))
    np.add.at(rows, np.searchsorted(present, session), np.column_stack([np.ones(len(y)), se_diff]))
    boot = block_bootstrap(rows, BLOCKS["all"], draws, seed)
    ci = _ci(boot[:, 1] / boot[:, 0])
    corr = (float(np.corrcoef(pa, pb)[0, 1]) if len(pa) > 1 and pa.std() > 0 and pb.std() > 0
            else None)
    return {"status": "ok", "n": len(pa),
            "mean_abs_diff": round(float(diff.mean()), 4),
            "mean_abs_diff_same_order": (round(float(diff[same].mean()), 4)
                                         if same.any() else None),
            "mean_abs_diff_other_order": (round(float(diff[~same].mean()), 4)
                                          if (~same).any() else None),
            "same_order_share": round(float(same.mean()), 4),
            "corr": None if corr is None else round(corr, 4),
            "side_disagreement": (round(float(np.mean((pa[sided] > 0.5) != (pb[sided] > 0.5))), 4)
                                  if sided.any() else None),
            "brier_diff": round(float(se_diff.mean()), 6), "brier_diff_ci95": ci,
            "order_sensitive": bool(ci is not None and (ci[0] > 0 or ci[1] < 0))}


def _derive(source_arms: Sequence[Arm], variant: str, boards: Sequence[Board],
            receipts: Mapping[str, Mapping[str, Mapping[str, Any]]], state: Mapping[str, Any],
            net: Callable[[str, str, str | None], float], grid: Sequence[float],
            run_dir: Path | None, key: str, edge_cap: float = EDGE_CAP
            ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """One derived deterministic arm per source repeat, tau fit on TRAIN
    (decisions see the capped views, like the model arm's)."""
    cost = round_trip_cost()
    payoff = state.get("payoff")
    if variant == "ev":
        if payoff is None:
            raise ValueError("the ev variant needs the payoff map (an outcome plug-in)")

        def decide(board: Board, p_up: Mapping[str, Mapping[str, float]],
                   tau: float | None) -> tuple[str | None, str | None, dict[str, Any]]:
            return decide_ev(board.rows, p_up, payoff, tau, cost)
    elif variant == "direction":
        def decide(board: Board, p_up: Mapping[str, Mapping[str, float]],
                   tau: float | None) -> tuple[str | None, str | None, dict[str, Any]]:
            return decide_direction(board, p_up, tau, key=key)
    else:
        raise ValueError(f"unknown derived variant {variant!r}")
    name = f"{source_arms[0].policy.name}.{'ev' if variant == 'ev' else 'dir'}-fit"

    def lookup(board: Board) -> longrun.Choice:  # scored from receipts, never executed
        raise RuntimeError("derived arms are scored from their receipts")

    spec = PolicySpec(name, "rule", repeats=len(source_arms), rule=lookup)
    items: list[dict[str, Any]] = []
    fits: dict[str, Any] = {}
    for arm_obj, source in zip(longrun.arms_of([spec]), source_arms, strict=True):
        mine = receipts.get(source.name, {})
        forecasts = [(board, forecast_receipt(mine.get(board.snapshot))) for board in boards]
        usable = [(board, cap_views(f["p_up"], edge_cap)) for board, f in forecasts
                  if f is not None]
        fit = fit_tau([(b, p) for b, p in usable if b.session <= state["cutoff"]], decide, net,
                      grid)
        fits[arm_obj.name] = {k: fit[k] for k in fit if k != "grid"} | {"grid": fit["grid"]}
        recs: dict[str, dict[str, Any]] = {}
        for board, p_up in usable:
            choice, horizon, detail = decide(board, p_up, fit["tau"])
            recs[board.snapshot] = {
                "schema": longrun.RECEIPT_SCHEMA, "arm": arm_obj.name, "policy": name,
                "repeat": arm_obj.repeat, "kind": "rule", "snapshot": board.snapshot,
                "session": board.session, "board_rows": len(board.rows), "ok": True,
                "derived_from": source.name, "decision": detail,
                **longrun._validated(board, choice, horizon, f"derived {variant}")}
        path = ""
        if run_dir is not None:
            target = longrun.receipts_path(run_dir, arm_obj.name)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("".join(json.dumps(r, sort_keys=True) + "\n"
                                      for r in recs.values()), encoding="utf-8")
            path = str(target)
        items.append({"arm": arm_obj, "receipts": recs, "file": path})
    return items, {"policy": name, "variant": variant, "fits": fits}


def _fmt(value: Any, digits: int = 4) -> str:
    return "n/a" if value is None else f"{value:+.{digits}f}"


def _fmt_ci(ci: Sequence[float] | None, digits: int = 4) -> str:
    return "n/a" if ci is None else f"[{ci[0]:+.{digits}f}, {ci[1]:+.{digits}f}]"


def report_markdown(section: Mapping[str, Any]) -> str:
    lines = [f"Forecaster `{section['source']}`: {section['boards_scored']} paired boards; "
             f"TRAIN <= {section['cutoff']} < TEST. Labels: strictly-higher spot at the exit "
             "(outcomes.py exit clocks; exit side from prints strictly after as-of). "
             "Scores are descriptive on TRAIN and confirmatory on TEST.", ""]
    lines.append("| arm | split | horizon | n | Brier [95% CI] | BSS vs clim [95% CI] | "
                 "BSS vs momentum [95% CI] | log loss | REL | RES | UNC | hit |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for arm, splits in section["scores"].items():
        for split in ("test", "train"):
            for horizon, s in (splits.get(split) or {}).items():
                if not s.get("n"):
                    continue
                m, ci = s["murphy"], s["ci95"]
                hit = "n/a" if s["hit_rate"] is None else f"{s['hit_rate']:.3f}"
                lines.append(
                    f"| {arm} | {split} | {horizon} | {s['n']} | {s['brier']:.4f} "
                    f"{_fmt_ci(ci['brier'])} | {_fmt(s['bss_clim'])} {_fmt_ci(ci['bss_clim'])} | "
                    f"{_fmt(s['bss_mom'])} {_fmt_ci(ci['bss_mom'])} | {s['log_loss']:.4f} | "
                    f"{m['reliability']:.4f} | {m['resolution']:.4f} | {m['uncertainty']:.4f} | "
                    f"{hit} |")
    lines.append("")
    aa = section.get("aa_order") or {}
    if aa.get("status") == "ok":
        lines.append(f"A/A order check ({aa['n']} paired forecasts): mean |dp| "
                     f"{aa['mean_abs_diff']:.4f} (same order {aa['mean_abs_diff_same_order']}, "
                     f"other order {aa['mean_abs_diff_other_order']}); corr {aa['corr']}; side "
                     f"disagreement {aa['side_disagreement']}; Brier diff "
                     f"{aa['brier_diff']:+.5f} {_fmt_ci(aa['brier_diff_ci95'], 5)}; order "
                     f"sensitive: {aa['order_sensitive']}.")
    else:
        lines.append(f"A/A order check: {aa.get('status', 'not_run')} - {aa.get('reason', '')}")
    lines.append("")
    for arm, rows in section["position_bias"].items():
        cells = "; ".join(f"{r['shown_as']}: p {r['mean_p']:.3f} vs y {r['base_rate']:.3f}"
                          for r in rows)
        lines.append(f"- position bias {arm}: {cells}")
    for derived in section["derived"].values():
        for arm, fit in derived.get("fits", {}).items():
            lines.append(f"- derived {arm} ({derived['variant']}): tau fit on TRAIN = "
                         f"{fit.get('tau')} (train net {fit.get('train_net')}, "
                         f"{fit.get('train_boards')} train boards; {fit.get('reason')})")
    k = section["momentum_k"]
    lines.append(f"- momentum baseline k (TRAIN): {k}; climatology (TRAIN base rates): "
                 f"{section['climatology']}")
    if section.get("warnings"):
        lines.append(f"- warnings: {section['warnings']}")
    return "\n".join(lines)


def report_plugin(params: Mapping[str, Any], ctx: PluginContext) -> Callable[..., dict[str, Any]]:
    """Params: source (the forecast policy name), cutoff, derived (["ev",
    "direction"]), tau_grid, key, edge_cap, draws (default: the protocol's)."""
    source = str(params.get("source") or "")
    if not source:
        raise ValueError("the forecast report needs 'source' (the forecast policy name)")
    cutoff = str(params.get("cutoff", DEFAULT_CUTOFF))
    variants = tuple(str(v) for v in params.get("derived", ("ev", "direction")))
    grid = tuple(float(t) for t in params.get("tau_grid", TAU_GRID))
    key = str(params.get("key", "board_order"))
    edge_cap = float(params.get("edge_cap", EDGE_CAP))
    state = fit_state(ctx, cutoff)

    def report(*, run_dir: Path | None, boards: Sequence[Board], arms: Sequence[Arm],
               receipts: Mapping[str, Mapping[str, Mapping[str, Any]]],
               outcomes: longrun.OutcomeCache, protocol: Protocol) -> dict[str, Any]:
        draws = int(params.get("draws", protocol.draws))
        seed = protocol.seed
        warnings = []
        if protocol.cutoff is not None and protocol.cutoff != cutoff:
            warnings.append(f"report cutoff {cutoff} != protocol cutoff {protocol.cutoff}")
        sources = sorted((a for a in arms if a.policy.name == source), key=lambda a: a.repeat)
        if not sources:
            return {"section": {"schema": REPORT_SCHEMA, "status": "no_source",
                                "source": source, "markdown": f"No arm of policy {source!r}."}}
        paired = [b for b in boards
                  if all(forecast_receipt(receipts.get(a.name, {}).get(b.snapshot))
                         for a in sources)]
        order = {s: i for i, s in enumerate(sorted({b.session for b in boards}))}
        rows = {a.name: _records(a, paired, receipts.get(a.name, {}), state, order)
                for a in sources}
        scores = {name: _score_arm(r, draws=draws, seed=seed) for name, r in rows.items()}
        aa: dict[str, Any] = {"status": "not_run", "reason": "the source needs repeats >= 2"}
        if len(sources) >= 2:
            first, second = (receipts.get(a.name, {}) for a in sources[:2])
            same = {b.snapshot: (forecast_receipt(first.get(b.snapshot)) or {}).get("perm")
                    == (forecast_receipt(second.get(b.snapshot)) or {}).get("perm")
                    for b in paired}
            aa = order_check(rows[sources[0].name], rows[sources[1].name], same, draws=draws,
                             seed=seed)
        derived_items: list[dict[str, Any]] = []
        derived: dict[str, Any] = {}
        for variant in variants:
            items, info = _derive(sources, variant, paired, receipts, state, outcomes.net,
                                  grid, run_dir, key, edge_cap)
            derived_items.extend(items)
            derived[info["policy"]] = info
        section = {"schema": REPORT_SCHEMA, "status": "ok", "source": source,
                   "cutoff": cutoff, "boards_scored": len(paired),
                   "forecasts_scored": {n: len(r["p"]) for n, r in rows.items()},
                   "horizons": list(FORECAST_HORIZONS), "blocks": BLOCKS, "draws": draws,
                   "log_loss_clip": [LOGLOSS_EPS, 1 - LOGLOSS_EPS],
                   "climatology": state["climatology"], "momentum_k": state["momentum_k"],
                   "payoff_map": state["payoff"], "round_trip_cost": round_trip_cost(),
                   "scores": scores, "aa_order": aa,
                   "position_bias": {n: _position_bias(r) for n, r in rows.items()},
                   "derived": derived, "warnings": warnings}
        section["markdown"] = report_markdown(section)
        return {"section": section, "derived": derived_items}
    return report


# ------------------------------------------------------------ power + smoke


def label_summary(labels: Mapping[str, Mapping[str, Mapping[str, Label | None]]],
                  sessions: Mapping[str, str], cutoff: str) -> dict[str, Any]:
    """Base rates per split and the dependence that shrinks the effective
    sample: the mean cross-underlying label correlation on a board and the
    within-session correlation (all boards of a session, same underlying)."""
    out: dict[str, Any] = {}
    for horizon in FORECAST_HORIZONS:
        by_board: dict[str, dict[str, int]] = {}
        for snapshot, board in labels.items():
            for alias, by_h in board.items():
                label = by_h.get(horizon)
                if label is not None:
                    by_board.setdefault(snapshot, {})[alias] = label.y
        aliases = sorted({a for ys in by_board.values() for a in ys})
        total = sum(len(ys) for ys in by_board.values())
        cross = []
        for i, left in enumerate(aliases):
            for right in aliases[i + 1:]:
                both = [(ys[left], ys[right]) for ys in by_board.values()
                        if left in ys and right in ys]
                xs, zs = np.array([b[0] for b in both]), np.array([b[1] for b in both])
                if len(both) > 2 and xs.std() > 0 and zs.std() > 0:
                    cross.append(float(np.corrcoef(xs, zs)[0, 1]))
        rho_u = float(np.mean(cross)) if cross else 0.0
        groups: dict[tuple[str, str], list[int]] = {}
        for snapshot, ys in by_board.items():
            for alias, y in ys.items():
                groups.setdefault((sessions[snapshot], alias), []).append(y)
        icc = icc_anova(list(groups.values()))
        m_board = max(1, len(aliases))
        m_session = float(np.mean([len(g) for g in groups.values()])) if groups else 1.0
        design = (1 + (m_board - 1) * max(0.0, rho_u)) * (1 + (m_session - 1) * max(0.0, icc))
        splits: dict[str, list[int]] = {"train": [], "test": []}
        for snapshot, ys in by_board.items():
            splits["train" if sessions[snapshot] <= cutoff else "test"].extend(ys.values())
        out[horizon] = {
            "labeled": total,
            **{f"base_rate_{name}": (round(float(np.mean(ys)), 4) if ys else None)
               for name, ys in splits.items()},
            "cross_underlying_corr": round(rho_u, 4), "within_session_icc": round(icc, 4),
            "design_effect": round(design, 2),
            "effective_n": round(total / design, 1) if design > 0 else None}
    return out


def icc_anova(groups: Sequence[Sequence[int]]) -> float:
    """One-way ANOVA intraclass correlation (MSB - MSW) / (MSB + (m0 - 1) MSW)."""
    groups = [g for g in groups if len(g) > 0]
    total = sum(len(g) for g in groups)
    k = len(groups)
    if k < 2 or total <= k:
        return 0.0
    grand = sum(sum(g) for g in groups) / total
    ssb = sum(len(g) * (float(np.mean(g)) - grand) ** 2 for g in groups)
    ssw = sum(float(np.sum((np.array(g, dtype=float) - np.mean(g)) ** 2)) for g in groups)
    msb, msw = ssb / (k - 1), ssw / (total - k)
    m0 = (total - sum(len(g) ** 2 for g in groups) / total) / (k - 1)
    denom = msb + (m0 - 1) * msw
    return float((msb - msw) / denom) if denom > 0 else 0.0


class CapturingTransport:
    """Wraps a PostTransport and records per-call status, latency, finish
    reason, token usage and the reply text (never the request headers)."""

    def __init__(self, inner: llm.PostTransport = llm.urllib_post) -> None:
        self.inner = inner
        self.calls: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def __call__(self, url: str, body: bytes, headers: dict[str, str],
                 timeout: float) -> tuple[int, bytes]:
        started = time.monotonic()
        entry: dict[str, Any] = {}
        try:
            status, raw = self.inner(url, body, headers, timeout)
        except Exception as error:
            entry = {"status": None, "error": type(error).__name__}
            raise
        else:
            entry = {"status": status}
            try:
                envelope = json.loads(raw)
                choice = envelope["choices"][0]
                entry.update(finish_reason=choice.get("finish_reason"),
                             usage=envelope.get("usage"),
                             content=str(choice["message"].get("content") or "")[-4000:])
            except (ValueError, KeyError, IndexError, TypeError, AttributeError):
                entry["envelope"] = "unparsed"
            return status, raw
        finally:
            entry["latency_s"] = round(time.monotonic() - started, 3)
            with self._lock:
                self.calls.append(entry)


def _pct(values: Sequence[float], q: float) -> float | None:
    return None if not values else round(float(np.percentile(values, q)), 2)


def smoke(*, bundle: Path, table: Path, out_root: Path, boards: int = 24, repeats: int = 2,
          concurrency: int = 3, provider: str = "minimax-flash", effort: str | None = None,
          transport: llm.PostTransport | None = None,
          quota: longrun.QuotaFn | None = None, now: datetime | None = None) -> dict[str, Any]:
    """A small live run of the forecaster (``boards`` boards spread evenly
    over the window, ``repeats`` repeats) through the real harness, plus
    transport statistics. Refuses to start without quota headroom."""
    if not 1 <= boards <= 48 or not 1 <= concurrency <= 3:
        raise ValueError("smoke: boards 1..48, concurrency 1..3")
    quota_ok = quota or longrun.broker_quota()
    ok, why = quota_ok()
    if not ok or "unavailable" in why:
        return {"status": "refused", "reason": f"quota: {why}"}
    capture = CapturingTransport(transport or llm.urllib_post)
    ctx = PluginContext(config_dir=out_root, shared={"transport": capture})
    every = longrun.plugin("boards", "v2")({"bundle": str(bundle)}, ctx)
    step = len(every) / boards
    chosen = [every[int(i * step + step / 2)] for i in range(boards)]
    ctx.boards = chosen
    outcome = longrun.plugin("outcome", "v2")({"table": str(table)}, ctx)
    ctx.shared["outcome"] = outcome
    name = "m31-fc"
    ask = longrun.PolicyAsk(None, {name: ask_plugin({"provider": provider, "effort": effort},
                                                    ctx)})
    report = report_plugin({"source": name, "draws": 2000}, ctx)
    run_dir = longrun.new_run_dir(out_root, now or datetime.now(UTC))
    policies = [PolicySpec(name, "model", repeats=repeats),
                *longrun.builtin_controls("intraday")]
    result = longrun.run_longrun(
        run_dir, boards=chosen, policies=policies, outcome=outcome, ask=ask, quota_ok=quota_ok,
        protocol=Protocol(draws=2000, random_seeds=200, cutoff=DEFAULT_CUTOFF,
                          incumbent=name if repeats >= 2 else None,
                          random_horizons=("intraday", "eod", "hold:5", "expiry")),
        settings=longrun.ExecSettings(concurrency=concurrency, pause_s=60.0, max_pause_s=0.0),
        reports={"forecast": report},
        meta={"smoke": True, "boards": [b.snapshot for b in chosen]})
    calls = capture.calls
    latencies = [c["latency_s"] for c in calls if c.get("status") == 200]
    usage = [c["usage"] for c in calls if isinstance(c.get("usage"), dict)]
    completion = [float(u.get("completion_tokens", 0)) for u in usage]
    prompt = [float(u.get("prompt_tokens", 0)) for u in usage]
    model_files = [longrun.receipts_path(run_dir, arm) for arm in policies[0].arm_names()]
    receipts = [json.loads(line) for path in model_files if path.is_file()  # model arm only
                for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    failures = [r.get("error", "") for r in receipts if not r.get("ok")]
    digest = (json.loads((run_dir / "digest.json").read_text(encoding="utf-8"))
              if (run_dir / "digest.json").is_file() else {})
    section = (digest.get("reports") or {}).get("forecast") or {}
    summary = {
        "schema": "desk-forecast-smoke/1", "run_dir": str(run_dir), "status": result["status"],
        "provider": provider, "effort": effort, "boards": len(chosen), "repeats": repeats,
        "calls": len(calls), "http_200": sum(1 for c in calls if c.get("status") == 200),
        "transport_errors": sum(1 for c in calls if c.get("status") is None),
        "truncated": sum(1 for c in calls if c.get("finish_reason") == "length"),
        "receipts": len(receipts), "parsed_ok": sum(1 for r in receipts if r.get("ok")),
        "parse_rate": (round(sum(1 for r in receipts if r.get("ok")) / len(receipts), 4)
                       if receipts else None),
        "failures": failures[:20],
        "latency_s": {"p50": _pct(latencies, 50), "p90": _pct(latencies, 90),
                      "max": max(latencies) if latencies else None},
        "tokens": {"completion_mean": round(float(np.mean(completion)), 1) if completion else None,
                   "completion_p90": _pct(completion, 90),
                   "prompt_mean": round(float(np.mean(prompt)), 1) if prompt else None},
        "forecast_report": {k: section.get(k) for k in ("status", "boards_scored",
                                                        "forecasts_scored", "aa_order")},
        "scores_all": {arm: {split: (s.get(split) or {}).get("all")
                             for split in ("train", "test")}
                       for arm, s in (section.get("scores") or {}).items()},
        "headline": digest.get("headline")}
    (run_dir / "smoke-summary.json").write_text(json.dumps(summary, indent=2, default=str),
                                                encoding="utf-8")
    with (run_dir / "transport-calls.jsonl").open("w", encoding="utf-8") as stream:
        for call in calls:
            stream.write(json.dumps(call, default=str) + "\n")
    return summary


def _cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tree_options.desk.forecast",
                                     description="Desk forecaster: labels summary and smoke.")
    sub = parser.add_subparsers(dest="command", required=True)
    labels = sub.add_parser("labels", help="label base rates + effective-sample summary")
    labels.add_argument("--bundle", required=True, type=Path)
    labels.add_argument("--out", required=True, type=Path)
    labels.add_argument("--cutoff", default=DEFAULT_CUTOFF)
    run = sub.add_parser("smoke", help="a small live forecaster run (quota-gated)")
    run.add_argument("--bundle", required=True, type=Path)
    run.add_argument("--table", required=True, type=Path)
    run.add_argument("--out-root", required=True, type=Path)
    run.add_argument("--boards", type=int, default=24)
    run.add_argument("--repeats", type=int, default=2)
    run.add_argument("--concurrency", type=int, default=3)
    run.add_argument("--provider", default="minimax-flash")
    run.add_argument("--effort", choices=EFFORTS, default=None,
                     help="M3.1 reasoning_effort (default: the provider's, max)")
    args = parser.parse_args(argv)
    if args.command == "labels":
        index = outcomes.prepare_index(json.loads(args.bundle.read_bytes()))
        table = label_table(index)
        sessions = {snapshot_id(d, c): d.isoformat() for d, c, _ in index.timeline}
        doc = {"schema": "desk-forecast-labels/1", "bundle": str(args.bundle),
               "cutoff": args.cutoff, "boards": len(table),
               "sessions": [index.sessions[0].isoformat(), index.sessions[-1].isoformat(),
                            len(index.sessions)],
               "summary": label_summary(table, sessions, args.cutoff)}
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with args.out.with_suffix(".jsonl").open("w", encoding="utf-8") as stream:
            for snapshot, per in table.items():
                stream.write(json.dumps({"snapshot": snapshot, "labels": {
                    alias: {h: (None if lab is None else {"y": lab.y, "ret_bps": round(
                        lab.ret_bps, 3), "exit_slot": lab.exit_slot}) for h, lab in by_h.items()}
                    for alias, by_h in per.items()}}) + "\n")
        args.out.write_text(json.dumps(doc, indent=2), encoding="utf-8")
        print(json.dumps(doc, indent=2))
        return 0
    summary = smoke(bundle=args.bundle, table=args.table, out_root=args.out_root,
                    boards=args.boards, repeats=args.repeats, concurrency=args.concurrency,
                    provider=args.provider, effort=args.effort)
    print(json.dumps(summary, indent=2, default=str))
    return 0 if summary.get("status") == "finished" else 3


if __name__ == "__main__":
    sys.exit(_cli())
