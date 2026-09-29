"""Exact counterfactual skill accounting for the desk long run.

The environment-v2 outcome table prices EVERY board row at EVERY horizon, so
the P&L a uniformly random row would have made on the same board at the same
horizon is KNOWN, not estimated. That known counterfactual is used here as a
control variate, as an exact additive decomposition of every arm's P&L, and
as the null of the confidence sequences.

Notation. One arm; B = the boards it holds an ok receipt for (its own
coverage: the excess is a within-board contrast, so it needs no pairing).

- ``v(b, r, h)``: the harness value of row r of board b at horizon h: net
  P&L, 0 when the row does not fill (exactly the outcome plug-in +
  ``OutcomeCache.net`` convention; a missing horizon is the plug-in default).
- ``m(b, h) = mean_r v(b, r, h)``: the random row at horizon h.
- ``base(b) = mean_{h in H} m(b, h)``: the random (row, horizon) pick; H is
  the protocol's random-null horizons (default the v2 menu), so ``base`` is
  exactly the harness's random-null per-board expectation.
- ``f_b``: the board's bullish row share; ``m_d(b, h)``: the mean over rows of
  direction d; ``delta(b, h) = m_bull - m_bear`` (0 on a one-sided board);
  ``m_s(b, h)`` / ``m_su(b, h)``: the mean over rows of the chosen row's
  structure / structure and underlying (the board's alias names it).
- ``e_b = 1`` when the arm entered b with row ``r_b``, horizon ``h_b`` and
  direction ``x_b`` (1 bullish); ``rho = |E| / |B|``; ``xbar`` = the arm's
  bullish share among its entries.

The identity (exact per board, hence per session and in total)::

    e_b v(b, r_b, h_b) = rho base(b)                               BASE
                       + (e_b - rho) base(b)                       PARTICIPATION
                       + e_b [m(b, h_b) - base(b)]                 HORIZON
                       + e_b (xbar - f_b) delta(b, h_b)            DIRECTION_TILT
                       + e_b (x_b - xbar) delta(b, h_b)            DIRECTION_TIMING
                       + e_b [v(b, r_b, h_b) - m_{x_b}(b, h_b)]    SELECTION

(``m_{x}(b, h) - m(b, h) = (x - f_b) delta(b, h)`` is the direction choice;
it splits into the TILT a constant bullish share ``xbar`` would earn - the
regime beta of a rising window - and the TIMING of going bullish on the
right boards.) SELECTION further splits exactly into STRUCTURE
(``m_s - m_d``: which structure within the direction), UNDERLYING
(``m_su - m_s``: which underlying within the structure - in a window where
the underlyings drift apart this is a regime bet too) and ROW (``v - m_su``:
which strike/expiry within both). BASE is what a random picker at the arm's
own entry rate earns (costs plus the average row's drift); PARTICIPATION is
entering on boards whose base is better than average.

``excess = DIRECTION + SELECTION = v - m(b, h_b)`` is the control variate:
it strips the board-level market move and the horizon choice exactly, and
under a random pick its conditional mean is exactly 0 whatever the market
did. ``alpha = TIMING + SELECTION = excess - TILT`` also strips the static
directional bet. ``vs_random = net - BASE``. A fixed RULE's excess is a
regime-dependent payoff of the rule, never decision skill.

Inference.

- Dependence: hold:N and expiry trades from nearby boards share one future
  path, so the session bootstrap is too optimistic for them. Each series is
  also resampled with the CIRCULAR BLOCK BOOTSTRAP (Politis & Romano 1992)
  over the window's sessions; the block length L is the 75th percentile of
  the arm's own trade spans in sessions (intraday/eod 1; hold:N N; expiry
  the row's dte x 252/365, capped at the sessions left, default 15 without a
  dte), clamped to [1, max_block = 20]. Series built on ``base`` (vs_random)
  use at least the MENU block (the same rule over every row at every menu
  horizon: ``base`` averages the expiry horizon in). The effective sample
  size is ``n x var_session / var_block``; a CI from fewer than 10 blocks
  (n / L) is flagged unreliable.
- FORWARD confidence sequence (the digest's superpopulation monitor): the
  per-session excess is summed over time-ordered blocks of L sessions. A
  trade entered in block k resolves by the end of block k + 1, so under the
  no-skill null (a pick carries no information beyond the as-of board) the
  ODD blocks form a martingale-difference sequence given the history up to
  each block's start, and so do the EVEN blocks (a lag-1 split). Each gets
  the Gaussian-mixture ASYMPTOTIC CS of Waudby-Smith, Arbour, Sinha, Kennedy
  & Ramdas (2024, Ann. Statist., "Time-uniform central limit theory and
  asymptotic confidence sequences", Thm 2.2) at alpha / 2; their
  intersection is a (1 - alpha) CS for the mean block excess (union bound).
  Below 20 blocks per parity no CS is reported (an asymptotic CS on a
  handful of blocks is meaningless): the window is too short to monitor
  that arm dependence-aware.
- IN-SAMPLE confidence sequence (the live progress field): the harness
  executes model tasks in a seeded shuffle, so an arm's decided boards
  arrive in random order and its per-board excess (0 for a skip) is a random
  sample of THIS run's finite population; mean x N boards is the run's
  final in-sample total excess. The primary CS is the predictable plug-in
  EMPIRICAL-BERNSTEIN CS of Waudby-Smith & Ramdas (2024, JRSS-B 86(1),
  "Estimating means of bounded random variables by betting", Thm 2) on the
  a-priori bound ``|excess| <= 100 x width + 300 (risk cap) + 50 (cost
  cap)``; the asymptotic CS rides beside it. Both treat the draws as
  i.i.d.; sampling without replacement only concentrates the sum further
  (Hoeffding 1963, Thm 4). It answers "will this run's in-sample excess end
  positive?", NOT "is there skill" (that is the forward CS / block CI).
- Power: the minimum detectable per-trade effect at 80% power, two-sided
  alpha 0.05, for raw net P&L (i.i.d., and with the board-mean component's
  batch-means long-run variance over horizon-length blocks) versus the
  demeaned excess (the within-board variance, exact under a random pick:
  it bounds what NON-directional selection can show), plus the excess of a
  random BULLISH row with its long-run variance (what a directional bet can
  show, dependence-aware).

Evidence, not authority: descriptive, mechanical, no model calls; nothing
here promotes anything.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, fields
from datetime import UTC, datetime
from pathlib import Path
from statistics import NormalDist
from typing import Any

import numpy as np

from tree_options.desk import longrun
from tree_options.desk.longrun import Arm, Board, OutcomeCache, Protocol

SKILL_SCHEMA = "desk-longrun-skill/1"
#: the v2 horizon menu (== lab.V2_HORIZONS; a test pins the equality)
MENU_HORIZONS: tuple[str, ...] = ("intraday", "eod", "hold:5", "expiry")
COMPONENTS = ("base", "participation", "horizon", "direction_tilt", "direction_timing",
              "selection")
SELECTION_SPLIT = ("structure", "underlying", "row")
POWER_TRADES = (250, 500, 1000, 2000, 5000)
RISK_CAP = 300.0  # replay's per-trade max loss (outcomes' entry_risk_cap)
MAX_COST = 50.0  # the EB bound's cap on a round-trip cost ($14.60 today)
MIN_BOOT_BLOCKS = 10  # a block-bootstrap CI from fewer blocks is flagged unreliable
MIN_CS_BLOCKS = 20  # per parity, for the forward CS

#: (snapshot, row id, horizon) -> (gross, net), or None for no fill / not evaluable
GetFn = Callable[[str, str, str | None], tuple[float, float] | None]


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class SkillOptions:
    """The skill section's knobs (config key ``skill``; recorded in the digest)."""

    alpha: float = 0.05
    max_block: int = 20
    expiry_span: int = 15
    eb_c: float = 0.5

    def __post_init__(self) -> None:
        if not 0 < self.alpha < 0.5:
            raise ValueError("skill.alpha must be in (0, 0.5)")
        if not 1 <= self.max_block <= 60:
            raise ValueError("skill.max_block must be 1..60")
        if not 1 <= self.expiry_span <= 60:
            raise ValueError("skill.expiry_span must be 1..60")
        if not 0 < self.eb_c < 1:
            raise ValueError("skill.eb_c must be in (0, 1)")

    @classmethod
    def from_mapping(cls, doc: Mapping[str, Any] | None) -> SkillOptions:
        doc = dict(doc or {})
        known = {f.name for f in fields(cls)}
        unknown = sorted(set(doc) - known)
        if unknown:
            raise ValueError(f"unknown skill option(s) {unknown}; known {sorted(known)}")
        values: dict[str, Any] = {k: (int(v) if k in ("max_block", "expiry_span") else float(v))
                                  for k, v in doc.items()}
        return cls(**values)


# ------------------------------------------------------------ counterfactuals


class ValueBook:
    """Memoized per-board row values at each horizon: the counterfactual
    table ``v(b, ., h)`` (gross and net; 0 for a row that does not fill)."""

    def __init__(self, get: GetFn, horizons: Sequence[str | None]) -> None:
        self._get = get
        self.horizons = tuple(horizons)
        if not self.horizons:
            raise ValueError("at least one base horizon is required")
        self._values: dict[tuple[str, str | None], tuple[np.ndarray, np.ndarray]] = {}
        self._bullish: dict[str, np.ndarray] = {}

    def values(self, board: Board, horizon: str | None, basis: str = "net") -> np.ndarray:
        key = (board.snapshot, horizon)
        if key not in self._values:
            gross = np.zeros(len(board.rows))
            net = np.zeros(len(board.rows))
            for i, rid in enumerate(board.ids):
                got = self._get(board.snapshot, rid, horizon)
                if got is not None:
                    gross[i], net[i] = got
            self._values[key] = (gross, net)
        gross, net = self._values[key]
        if basis == "net":
            return net
        if basis == "gross":
            return gross
        raise ValueError("basis must be 'net' or 'gross'")

    def mean(self, board: Board, horizon: str | None, basis: str = "net") -> float:
        return float(self.values(board, horizon, basis).mean())

    def base(self, board: Board, basis: str = "net") -> float:
        return float(np.mean([self.mean(board, h, basis) for h in self.horizons]))

    def bullish(self, board: Board) -> np.ndarray:
        if board.snapshot not in self._bullish:
            self._bullish[board.snapshot] = np.array(
                [longrun.is_bullish(row) for row in board.rows], dtype=bool)
        return self._bullish[board.snapshot]


@dataclass
class Decomposition:
    """One arm's per-board components (aligned to ``boards``)."""

    boards: list[Board]
    entered: np.ndarray
    realized: np.ndarray
    parts: dict[str, np.ndarray]
    selection_split: dict[str, np.ndarray]
    rho: float
    bullish_share: float | None

    def total(self, name: str) -> float:
        return float(self.parts[name].sum())

    @property
    def excess(self) -> np.ndarray:
        return self.parts["direction_tilt"] + self.parts["direction_timing"] \
            + self.parts["selection"]

    @property
    def alpha(self) -> np.ndarray:
        return self.parts["direction_timing"] + self.parts["selection"]

    @property
    def vs_random(self) -> np.ndarray:
        return self.realized - self.parts["base"]


def decompose(book: ValueBook, boards: Sequence[Board],
              decisions: Sequence[tuple[str | None, str | None]],
              basis: str = "net") -> Decomposition:
    """The exact six-part decomposition of an arm's per-board value (plus the
    exact structure/pick split of SELECTION)."""
    boards = list(boards)
    n = len(boards)
    if len(decisions) != n:
        raise ValueError("one decision per board")
    entered = np.array([choice is not None for choice, _ in decisions], dtype=bool)
    rho = float(entered.mean()) if n else 0.0
    base = np.array([book.base(b, basis) for b in boards]) if n else np.zeros(0)
    parts = {name: np.zeros(n) for name in COMPONENTS}
    parts["base"] = rho * base
    parts["participation"] = (entered.astype(float) - rho) * base
    split = {name: np.zeros(n) for name in SELECTION_SPLIT}
    realized = np.zeros(n)
    direction = np.zeros(n)
    x = np.zeros(n)
    share = np.zeros(n)
    spread = np.zeros(n)
    for i, (board, (choice, horizon)) in enumerate(zip(boards, decisions, strict=True)):
        if choice is None:
            continue
        ids = board.ids
        if choice not in ids:
            raise ValueError(f"{board.snapshot}: choice {choice!r} is not a row of the board")
        r = ids.index(choice)
        vals = book.values(board, horizon, basis)
        bull = book.bullish(board)
        same_direction = bull == bull[r]
        chosen = board.rows[r]
        same_structure = same_direction & np.array(
            [row.get("structure") == chosen.get("structure") for row in board.rows], dtype=bool)
        same_underlying = same_structure & np.array(
            [row.get("underlying") == chosen.get("underlying") for row in board.rows],
            dtype=bool)
        m = float(vals.mean())
        m_d = float(vals[same_direction].mean())
        m_s = float(vals[same_structure].mean())
        m_su = float(vals[same_underlying].mean())
        realized[i] = float(vals[r])
        parts["horizon"][i] = m - base[i]
        direction[i] = m_d - m
        parts["selection"][i] = realized[i] - m_d
        split["structure"][i] = m_s - m_d
        split["underlying"][i] = m_su - m_s
        split["row"][i] = realized[i] - m_su
        x[i] = float(bull[r])
        share[i] = float(bull.mean())
        if 0 < int(bull.sum()) < len(bull):
            spread[i] = float(vals[bull].mean() - vals[~bull].mean())
    xbar = float(x[entered].mean()) if entered.any() else None
    tilt = np.where(entered, ((xbar or 0.0) - share) * spread, 0.0)
    parts["direction_tilt"] = tilt
    parts["direction_timing"] = direction - tilt
    return Decomposition(boards=boards, entered=entered, realized=realized, parts=parts,
                         selection_split=split, rho=rho, bullish_share=xbar)


# ------------------------------------------------------------ dependence


def horizon_span(horizon: str | None, row: Mapping[str, Any] | None, sessions_left: int,
                 expiry_span: int = 15) -> int:
    """Sessions a trade's future path covers: intraday/eod 1, hold:N N, expiry
    the row's dte in sessions (x 252/365; ``expiry_span`` without one), each
    capped at the sessions left in the window (at least 1)."""
    left = max(1, sessions_left)
    if horizon is None or horizon in ("intraday", "eod"):
        return 1
    if horizon.startswith("hold:"):
        return max(1, min(int(horizon.split(":", 1)[1]), left))
    if horizon == "expiry":
        dte = None if row is None else longrun._num(row, "dte")
        span = expiry_span if dte is None else round(dte * 252 / 365)
        return max(1, min(span, left))
    return 1


def block_length(spans: Sequence[int], max_block: int = 20) -> int:
    """The 75th percentile of the spans (ceiling), clamped to [1, max_block]."""
    if len(spans) == 0:
        return 1
    return int(max(1, min(max_block, math.ceil(float(np.percentile(spans, 75))))))


def block_bootstrap_sums(series: Sequence[float] | np.ndarray, block: int, *, draws: int,
                         seed: int) -> np.ndarray:
    """Circular block bootstrap (Politis & Romano 1992) of the series SUM:
    ceil(n / block) blocks of ``block`` consecutive values (wrapping at the
    end) from uniform starts, truncated to n. ``block = 1`` is the plain
    i.i.d. bootstrap of the series."""
    arr = np.asarray(series, dtype=float)
    n = arr.size
    if n == 0:
        return np.zeros(draws)
    block = max(1, min(int(block), n))
    k = -(-n // block)
    offsets = np.arange(block)
    rng = np.random.default_rng(seed)
    out = np.empty(draws)
    for start in range(0, draws, 1000):
        m = min(1000, draws - start)
        starts = rng.integers(0, n, size=(m, k))
        idx = ((starts[:, :, None] + offsets) % n).reshape(m, k * block)[:, :n]
        out[start:start + m] = arr[idx].sum(axis=1)
    return out


def _pct(sums: np.ndarray) -> list[float]:
    lo, hi = np.percentile(sums, [2.5, 97.5])
    return [round(float(lo), 2), round(float(hi), 2)]


def interval_report(series: Sequence[float] | np.ndarray, block: int, *, draws: int,
                    seed: int) -> dict[str, Any]:
    """Session vs circular-block bootstrap 95% CIs of the series sum, the
    width ratio, the effective sample size n x var_session / var_block, and
    the number of blocks the window holds (reliable when >= 10)."""
    arr = np.asarray(series, dtype=float)
    block = int(max(1, min(block, max(1, arr.size))))
    iid = block_bootstrap_sums(arr, 1, draws=draws, seed=seed)
    blk = block_bootstrap_sums(arr, block, draws=draws, seed=seed)
    s_ci, b_ci = _pct(iid), _pct(blk)
    var_iid, var_blk = float(iid.var()), float(blk.var())
    s_width, b_width = s_ci[1] - s_ci[0], b_ci[1] - b_ci[0]
    blocks = arr.size / block
    return {"total": round(float(arr.sum()), 2), "sessions": int(arr.size), "block": block,
            "blocks": round(blocks, 1), "reliable": bool(blocks >= MIN_BOOT_BLOCKS),
            "session_ci95": s_ci, "block_ci95": b_ci,
            "width_ratio": round(b_width / s_width, 3) if s_width > 0 else None,
            "ess_sessions": round(arr.size * var_iid / var_blk, 1) if var_blk > 0 else None}


# ------------------------------------------------------------ confidence sequences


def asymptotic_cs(x: Sequence[float] | np.ndarray, alpha: float = 0.05,
                  t_star: float | None = None) -> tuple[np.ndarray, np.ndarray]:
    """Gaussian-mixture asymptotic CS for the MEAN (Waudby-Smith, Arbour,
    Sinha, Kennedy & Ramdas 2024, Ann. Statist., Thm 2.2)::

        mean_t +/- sd_t sqrt(2 (t rho^2 + 1) / (t^2 rho^2) log(sqrt(t rho^2 + 1) / alpha))

    with ``sd_t`` the running empirical SD (1/t) and ``rho`` tightest at
    ``t_star`` (default n): rho^2 = (-2 log alpha + log(1 - 2 log alpha)) / t_star.
    Two-sided (1 - alpha), time-uniform; t = 1 is unbounded."""
    arr = np.asarray(x, dtype=float)
    n = arr.size
    if n == 0:
        return np.zeros(0), np.zeros(0)
    t = np.arange(1, n + 1, dtype=float)
    mean = np.cumsum(arr) / t
    var = np.maximum(np.cumsum(arr * arr) / t - mean * mean, 0.0)
    rho2 = (-2 * math.log(alpha) + math.log(1 - 2 * math.log(alpha))) / float(t_star or n)
    radius = np.sqrt(var) * np.sqrt(2 * (t * rho2 + 1) / (t * t * rho2)
                                    * np.log(np.sqrt(t * rho2 + 1) / alpha))
    radius[0] = np.inf
    return mean - radius, mean + radius


def eb_cs(x: Sequence[float] | np.ndarray, lo: float, hi: float, alpha: float = 0.05,
          c: float = 0.5) -> tuple[np.ndarray, np.ndarray]:
    """Predictable plug-in empirical-Bernstein CS for the MEAN of values in
    [lo, hi] (Waudby-Smith & Ramdas 2024, JRSS-B 86(1), Thm 2), on the
    rescaled y = (x - lo) / (hi - lo) in [0, 1]::

        mu_t = (1/2 + sum y) / (t + 1);  s2_t = (1/4 + sum (y_i - mu_i)^2) / (t + 1)
        lam_t = min(sqrt(2 log(2/alpha) / (s2_{t-1} t log(1 + t))), c)
        v_t = 4 (y_t - mu_{t-1})^2;  psi(l) = (-log(1 - l) - l) / 4
        CS_t = sum lam y / sum lam +/- (log(2/alpha) + sum v psi(lam)) / sum lam

    Nonasymptotic, time-uniform, two-sided (1 - alpha), clipped to [lo, hi]."""
    if not hi > lo:
        raise ValueError("the EB bound needs hi > lo")
    arr = np.asarray(x, dtype=float)
    n = arr.size
    if n == 0:
        return np.zeros(0), np.zeros(0)
    width = hi - lo
    if float(arr.min()) < lo - 1e-9 * width or float(arr.max()) > hi + 1e-9 * width:
        raise ValueError("a value lies outside the declared EB bound")
    y = np.clip((arr - lo) / width, 0.0, 1.0)
    t = np.arange(1, n + 1, dtype=float)
    mu = (0.5 + np.cumsum(y)) / (t + 1)
    s2 = (0.25 + np.cumsum((y - mu) ** 2)) / (t + 1)
    mu_prev = np.concatenate([[0.5], mu[:-1]])
    s2_prev = np.concatenate([[0.25], s2[:-1]])
    lam = np.minimum(np.sqrt(2 * math.log(2 / alpha) / (s2_prev * t * np.log1p(t))), c)
    v = 4 * (y - mu_prev) ** 2
    psi = (-np.log1p(-lam) - lam) / 4
    sum_lam = np.cumsum(lam)
    center = np.cumsum(lam * y) / sum_lam
    margin = (math.log(2 / alpha) + np.cumsum(v * psi)) / sum_lam
    lower = np.clip(center - margin, 0.0, 1.0)
    upper = np.clip(center + margin, 0.0, 1.0)
    return lo + width * lower, lo + width * upper


def excess_bound(boards: Sequence[Board]) -> float | None:
    """A-priori per-board |excess| bound from the board rows alone: a fill's
    gross lies in [-max_loss, width x 100 - max_loss] with max_loss <= 300
    (the replay risk cap), net = gross - cost (cost <= 50), no fill = 0, so
    |v - m| <= 100 x width + 300 + 50. None when a row carries no width."""
    widest = 0.0
    for board in boards:
        for row in board.rows:
            width = longrun._num(row, "width")
            if width is None or width <= 0:
                return None
            widest = max(widest, width)
    return 100 * widest + RISK_CAP + MAX_COST if boards else None


def _first_true(mask: np.ndarray) -> int | None:
    hits = np.flatnonzero(mask)
    return int(hits[0]) + 1 if hits.size else None


def _round_total(value: float) -> float | None:
    return round(float(value), 2) if math.isfinite(value) else None


def monitor(values: Sequence[float] | np.ndarray, population: int, *, alpha: float,
            bound: float | None, eb_c: float = 0.5) -> dict[str, Any]:
    """IN-SAMPLE anytime CS: a random-order sample of per-board excess (0 =
    skip), mapped to the population TOTAL (mean x ``population``)."""
    arr = np.asarray(values, dtype=float)
    doc: dict[str, Any] = {"target": "this run's in-sample total excess (random decision "
                                     "order; not superpopulation skill)",
                           "observed": int(arr.size), "population": int(population),
                           "excess_observed": round(float(arr.sum()), 2)}
    if arr.size == 0:
        return {**doc, "eb": None, "asymptotic": None, "significant": False}
    a_lo, a_hi = asymptotic_cs(arr, alpha, t_star=population)
    doc["asymptotic"] = {
        "cs_total": [_round_total(a_lo[-1] * population), _round_total(a_hi[-1] * population)],
        "significant": bool(a_lo[-1] > 0), "first_significant_at": _first_true(a_lo > 0),
        "method": "Waudby-Smith, Arbour, Sinha, Kennedy & Ramdas 2024 (Ann. Statist.) "
                  "Gaussian-mixture asymptotic CS, Thm 2.2"}
    if bound is None or float(np.abs(arr).max()) > bound:
        doc["eb"] = None  # no a-priori bound (or it was violated): the asymptotic CS only
        doc["eb_unavailable"] = ("no a-priori bound" if bound is None
                                 else f"a value exceeds the a-priori bound {bound}")
    else:
        e_lo, e_hi = eb_cs(arr, -bound, bound, alpha, eb_c)
        doc["eb"] = {
            "cs_total": [_round_total(e_lo[-1] * population), _round_total(e_hi[-1] * population)],
            "significant": bool(e_lo[-1] > 0), "first_significant_at": _first_true(e_lo > 0),
            "bound": bound,
            "method": "Waudby-Smith & Ramdas 2024 (JRSS-B) predictable plug-in "
                      "empirical-Bernstein CS, Thm 2"}
    primary = doc["eb"] if doc["eb"] is not None else doc["asymptotic"]
    doc["significant"] = bool(primary["significant"])
    return doc


def _block_sums(per_session: np.ndarray, block: int) -> np.ndarray:
    """Sums over consecutive FULL blocks of ``block`` sessions (a trailing
    partial block is dropped: every block must share one mean)."""
    block = max(1, block)
    full = (per_session.size // block) * block
    return per_session[:full].reshape(-1, block).sum(axis=1) if full else np.zeros(0)


def forward_cs(per_session: Sequence[float] | np.ndarray, block: int,
               alpha: float = 0.05) -> dict[str, Any]:
    """The forward (time-ordered) CS of the TOTAL excess: odd and even block
    sums each get the asymptotic CS at alpha / 2 (each parity is a
    martingale-difference sequence under the no-skill null when trades
    resolve within the next block); the intersection covers the mean block
    excess with probability >= 1 - alpha (union bound)."""
    arr = np.asarray(per_session, dtype=float)
    blocks = _block_sums(arr, block)
    doc: dict[str, Any] = {"block": int(max(1, block)), "blocks": int(blocks.size),
                           "sessions_used": int(blocks.size * max(1, block)),
                           "method": ("odd/even lag-1 block split, asymptotic CS (Waudby-Smith "
                                      "et al. 2024, Thm 2.2) at alpha/2 each, intersected")}
    odd, even = blocks[0::2], blocks[1::2]
    if min(odd.size, even.size) < MIN_CS_BLOCKS:
        return {**doc, "cs_total": None, "significant": False, "reliable": False,
                "per_parity": [int(odd.size), int(even.size)],
                "reason": (f"fewer than {MIN_CS_BLOCKS} blocks per parity: the window is too "
                           "short to monitor this arm dependence-aware")}
    o_lo, o_hi = asymptotic_cs(odd, alpha / 2)
    e_lo, e_hi = asymptotic_cs(even, alpha / 2)
    lower, upper = max(o_lo[-1], e_lo[-1]), min(o_hi[-1], e_hi[-1])
    k = blocks.size
    return {**doc, "per_parity": [int(odd.size), int(even.size)],
            "cs_total": [_round_total(lower * k), _round_total(upper * k)],
            "significant": bool(lower > 0), "reliable": True}


# ------------------------------------------------------------ per arm


def _ordered_ok(receipts: Mapping[str, Mapping[str, Any]],
                snapshots: set[str]) -> list[str]:
    """The arm's ok snapshots in the order they were decided (receipt ``at``,
    else file order)."""
    items = [(str(rec.get("at") or ""), i, snap) for i, (snap, rec) in enumerate(receipts.items())
             if rec.get("ok") and snap in snapshots]
    return [snap for _, _, snap in sorted(items)]


def _windows(boards: Sequence[Board]) -> tuple[list[str], dict[str, int]]:
    sessions = sorted({b.session for b in boards})
    return sessions, {s: i for i, s in enumerate(sessions)}


def menu_block(boards: Sequence[Board], horizons: Sequence[str | None],
               options: SkillOptions) -> int:
    """The block length for series built on ``base`` (every menu horizon)."""
    sessions, pos = _windows(boards)
    blocks = []
    for h in horizons:
        spans = [horizon_span(h, row, len(sessions) - 1 - pos[b.session], options.expiry_span)
                 for b in boards for row in b.rows]
        blocks.append(block_length(spans, options.max_block))
    return max(blocks) if blocks else 1


def arm_skill(book: ValueBook, boards: Sequence[Board],
              decisions: Sequence[tuple[str | None, str | None]], *,
              window: Sequence[Board], order: Sequence[int] | None = None,
              options: SkillOptions, draws: int, seed: int, bound: float | None,
              base_block: int, population: int | None = None,
              kind: str = "model") -> dict[str, Any]:
    """The skill document of one arm over its covered ``boards`` (time order).

    ``window`` is every board of the run (its sessions are the bootstrap's
    time axis; uncovered sessions count 0); ``order`` the indices of
    ``boards`` in decision order (default time order); ``population`` the
    in-sample CS's board count (default len(boards))."""
    boards = list(boards)
    net = decompose(book, boards, decisions, "net")
    gross = decompose(book, boards, decisions, "gross")
    sessions, pos = _windows(window)
    board_sessions = [b.session for b in boards]

    def per_session(values: np.ndarray) -> np.ndarray:
        return longrun.session_sums(board_sessions, values.tolist(), sessions)

    spans = [horizon_span(h, b.rows[b.ids.index(c)], len(sessions) - 1 - pos[b.session],
                          options.expiry_span)
             for b, (c, h) in zip(boards, decisions, strict=True) if c is not None]
    block = block_length(spans, options.max_block)
    series = {"net": net.realized, "excess": net.excess, "alpha": net.alpha,
              "selection": net.parts["selection"], "vs_random": net.vs_random}
    intervals = {name: interval_report(per_session(values),
                                       max(block, base_block) if name == "vs_random" else block,
                                       draws=draws, seed=seed)
                 for name, values in series.items()}
    net_total = float(net.realized.sum())
    ordered = list(order) if order is not None else list(range(len(boards)))
    excess = net.excess
    horizons: dict[str, int] = {}
    for choice, h in decisions:
        if choice is not None:
            horizons[str(h)] = horizons.get(str(h), 0) + 1
    doc: dict[str, Any] = {
        "kind": kind, "boards": len(boards), "entered": int(net.entered.sum()),
        "entry_rate": round(net.rho, 4),
        "bullish_share": None if net.bullish_share is None else round(net.bullish_share, 4),
        "horizon_mix": dict(sorted(horizons.items())),
        "net_total": round(net_total, 2),
        "gross_total": round(float(gross.realized.sum()), 2),
        "cost_drag": round(float(gross.realized.sum()) - net_total, 2),
        "components": {name: round(net.total(name), 2) for name in COMPONENTS},
        "selection_split": {name: round(float(v.sum()), 2)
                            for name, v in net.selection_split.items()},
        "components_gross": {name: round(gross.total(name), 2) for name in COMPONENTS},
        "identity_residual": round(net_total - sum(net.total(k) for k in COMPONENTS), 6),
        "excess_total": round(float(excess.sum()), 2),
        "alpha_total": round(float(net.alpha.sum()), 2),
        "vs_random_total": round(float(net.vs_random.sum()), 2),
        "block": {"length": block, "base_length": max(block, base_block),
                  "spans_p75": (round(float(np.percentile(spans, 75)), 1) if spans else None),
                  "rule": ("ceil(p75 of the arm's trade spans in sessions), clamped to "
                           f"[1, {options.max_block}]; vs_random uses max(arm, menu) block")},
        "intervals": intervals,
        "cs_forward": forward_cs(per_session(excess), block, options.alpha),
        "cs_in_sample": {"order": "decision" if order is not None else "time",
                         **monitor(excess[ordered], population or len(boards),
                                   alpha=options.alpha, bound=bound, eb_c=options.eb_c)},
    }
    doc["verdict"] = verdict(doc)
    return doc


def verdict(doc: Mapping[str, Any], prefix: str = "") -> str:
    """One honest line: is there excess over a random row, what carries it,
    and how much the window can say."""
    if doc["entered"] == 0:
        return prefix + "NO TRADES - nothing to attribute. Descriptive; nothing promoted."
    ex, al = doc["intervals"]["excess"], doc["intervals"]["alpha"]
    lo, hi = ex["block_ci95"]
    alo, ahi = al["block_ci95"]
    comps = doc["components"]
    carrier = max(COMPONENTS, key=lambda k: (abs(comps[k]), k))
    span = f"[block CI {lo:+.0f}, {hi:+.0f}; L={ex['block']}]"
    weak = "" if ex["reliable"] else f" (only {ex['blocks']} blocks: CI unreliable)"
    split = doc.get("selection_split") or {}
    parts = (f"selection {comps['selection']:+.0f} = structure {split.get('structure', 0):+.0f}"
             f" + underlying {split.get('underlying', 0):+.0f} + row {split.get('row', 0):+.0f}")
    alpha_text = f"alpha {al['total']:+.0f} [{alo:+.0f}, {ahi:+.0f}]"
    if lo > 0 and alo > 0:
        core = (f"EXCESS over a random row {ex['total']:+.0f} {span}{weak} survives removing "
                f"the static direction tilt ({alpha_text}; {parts})")
    elif lo > 0:
        core = (f"EXCESS over a random row {ex['total']:+.0f} {span}{weak} is carried by the "
                f"static direction tilt {comps['direction_tilt']:+.0f} (regime beta); "
                f"{alpha_text} is not significant")
    elif alo > 0:
        core = (f"excess over a random row {ex['total']:+.0f} {span}{weak} spans 0, but "
                f"{alpha_text} is positive: {parts} and timing "
                f"{comps['direction_timing']:+.0f}, offset by the static direction tilt "
                f"{comps['direction_tilt']:+.0f}")
    elif hi < 0:
        core = f"picks WORSE than a random row: excess {ex['total']:+.0f} {span}{weak}"
    else:
        core = (f"NO SKILL DETECTED: excess over a random row {ex['total']:+.0f} {span}{weak} "
                "spans 0")
    fwd = doc.get("cs_forward") or {}
    flag = (("significant" if fwd.get("significant") else "not significant")
            if fwd.get("reliable") else f"n/a ({fwd.get('per_parity')} blocks per parity < "
                                        f"{MIN_CS_BLOCKS})")
    rule = ("; a FIXED RULE: its excess is a regime-dependent payoff, not decision skill"
            if doc.get("kind") in ("rule", "control") else "")
    return (f"{prefix}{core}; net {doc['net_total']:+.0f} mostly {carrier} "
            f"{comps[carrier]:+.0f}; forward CS {flag}{rule}. Descriptive; nothing promoted.")


# ------------------------------------------------------------ power


def power_table(book: ValueBook, boards: Sequence[Board], horizons: Sequence[str | None], *,
                trades: Sequence[int] = POWER_TRADES, alpha: float = 0.05, power: float = 0.8,
                options: SkillOptions | None = None) -> dict[str, Any]:
    """Minimum detectable per-trade effect (two-sided ``alpha``, ``power``)
    of one random-row trade per board: raw net (i.i.d.; and with the board
    mean's batch-means long-run variance over horizon blocks) vs the
    demeaned excess (within-board variance: exact under a random pick)."""
    options = options or SkillOptions()
    z = NormalDist().inv_cdf(1 - alpha / 2) + NormalDist().inv_cdf(power)
    sessions, pos = _windows(boards)
    out: dict[str, Any] = {}
    if not boards:
        return {"z": round(z, 4), "alpha": alpha, "power": power, "horizons": out}
    for h in horizons:
        means = np.array([book.mean(b, h) for b in boards])
        within = np.array([float(book.values(b, h).var()) for b in boards])
        s2_e = float(within.mean())
        s2_m = float(means.var())
        spans = [horizon_span(h, row, len(sessions) - 1 - pos[b.session], options.expiry_span)
                 for b in boards for row in b.rows]
        block = block_length(spans, options.max_block)
        batch = np.array([pos[b.session] // block for b in boards])
        sums = np.bincount(batch, weights=means - means.mean())
        s2_m_lr = float((sums ** 2).sum() / len(boards))
        raw, raw_lr = s2_m + s2_e, s2_m_lr + s2_e
        # the excess of a random BULLISH row (boards that carry one): its board-level
        # part m_bull - m moves with the market, so it gets the long-run variance too
        bull_boards = [i for i, b in enumerate(boards) if book.bullish(b).any()]
        s2_dir: float | None = None
        if bull_boards:
            tilt = np.array([float(book.values(boards[i], h)[book.bullish(boards[i])].mean())
                             - means[i] for i in bull_boards])
            noise = float(np.mean([book.values(boards[i], h)[book.bullish(boards[i])].var()
                                   for i in bull_boards]))
            dir_sums = np.bincount(batch[bull_boards], weights=tilt - tilt.mean())
            s2_dir = float((dir_sums ** 2).sum() / len(bull_boards)) + noise

        def mde(var: float | None, n: int) -> float | None:
            return None if var is None else round(z * math.sqrt(var / n), 2)

        out[str(h)] = {
            "boards": len(boards), "mean_row_value": round(float(means.mean()), 2),
            "sd_raw": round(math.sqrt(raw), 2), "sd_raw_dependent": round(math.sqrt(raw_lr), 2),
            "sd_excess": round(math.sqrt(s2_e), 2),
            "sd_bullish_bet_dependent": (None if s2_dir is None
                                         else round(math.sqrt(s2_dir), 2)),
            "vrf_iid": round(raw / s2_e, 3) if s2_e > 0 else None,
            "vrf_dependent": round(raw_lr / s2_e, 3) if s2_e > 0 else None,
            "block": block, "batches": int(sums.size),
            "rows": [{"trades": n, "mde_raw_iid": mde(raw, n),
                      "mde_raw_dependent": mde(raw_lr, n), "mde_excess": mde(s2_e, n),
                      "mde_bullish_bet_dependent": mde(s2_dir, n)}
                     for n in trades]}
    return {"z": round(z, 4), "alpha": alpha, "power": power, "horizons": out}


# ------------------------------------------------------------ digest section


NOTE = ("Descriptive and mechanical; nothing promoted. excess = pnl - the mean of the SAME "
        "board's rows at the SAME horizon (known exactly from the outcome table; a no-fill "
        "row is 0 as in the harness); components sum exactly to each arm's net (identity "
        "residual shown). Coverage is each arm's own ok receipts. CIs: session vs circular "
        "block bootstrap over the window's sessions (unreliable below 10 blocks), per arm "
        "and UNCORRECTED for the number of arms (confirmatory claims go through the "
        "walk-forward section). The forward CS is the superpopulation monitor; the in-sample "
        "CS only forecasts this run's own total excess.")

PAIR_NA = ("two-leg package arm ('idA+idB' rule choice): the exact single-row counterfactual "
           "decomposition does not apply (one leg is bullish, the other bearish: DIRECTION "
           "and the structure/row split are not defined for a package), so it is marked "
           "n/a here rather than approximated; the arm's net is on the same paired "
           "scoreboard, scored against the single-row random null")


def _any_pair(receipts: Mapping[str, Mapping[str, Any]]) -> bool:
    """The arm ever held a two-leg package (an ok receipt with a pair choice)."""
    return any(longrun.pair_legs(rec.get("choice")) is not None
               for rec in receipts.values() if rec.get("ok"))


def pair_arm_skill(outcomes: OutcomeCache, covered: Sequence[Board],
                   mine: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """A package arm's reduced skill doc: realized net/gross and entry
    accounting only; the six-part decomposition is explicitly n/a (PAIR_NA),
    never silently approximated."""
    entered = unevaluable = 0
    net = gross = 0.0
    horizons: dict[str, int] = {}
    for board in covered:
        rec = mine.get(board.snapshot, {})
        choice, horizon = rec.get("choice"), rec.get("horizon")
        if choice is None:
            continue
        entered += 1
        horizons[str(horizon)] = horizons.get(str(horizon), 0) + 1
        value = outcomes.get(board.snapshot, choice, horizon)
        if value is None:
            unevaluable += 1
            continue
        gross, net = gross + value[0], net + value[1]
    return {"boards": len(covered), "entered": entered, "unevaluable": unevaluable,
            "entry_rate": round(entered / len(covered), 4) if covered else 0.0,
            "horizon_mix": dict(sorted(horizons.items())),
            "net_total": round(net, 2), "gross_total": round(gross, 2),
            "cost_drag": round(gross - net, 2),
            "decomposition": "n/a", "decomposition_note": PAIR_NA,
            "verdict": (f"PAIR ARM: {entered} two-leg package(s), net {net:+.2f} "
                        f"({unevaluable} unevaluable); the single-row counterfactual "
                        "decomposition is n/a; scored against the single-row random null. "
                        "Descriptive; nothing promoted.")}


def skill_section(boards: Sequence[Board], arms: Sequence[Arm],
                  receipts: Mapping[str, Mapping[str, Mapping[str, Any]]],
                  outcomes: OutcomeCache, protocol: Protocol, *,
                  options: Mapping[str, Any] | SkillOptions | None = None) -> dict[str, Any]:
    """The digest's ``skill`` section: every executed arm (rules and controls
    included) decomposed, bootstrapped, monitored; the power table."""
    opts = options if isinstance(options, SkillOptions) else SkillOptions.from_mapping(options)
    horizons = (tuple(protocol.random_horizons) if protocol.random_horizons is not None
                else MENU_HORIZONS)
    book = ValueBook(outcomes.get, horizons)
    boards = list(boards)
    bound = excess_bound(boards)
    base_block = menu_block(boards, horizons, opts)
    doc_arms: dict[str, Any] = {}
    for arm in arms:
        mine = receipts.get(arm.name, {})
        covered = [b for b in boards if mine.get(b.snapshot, {}).get("ok")]
        complete = len(covered) == len(boards)
        if _any_pair(mine):
            doc_arms[arm.name] = {"policy": arm.policy.name, "complete": complete,
                                  "kind": arm.policy.kind,
                                  **pair_arm_skill(outcomes, covered, mine)}
            continue
        decisions = [(mine[b.snapshot].get("choice"), mine[b.snapshot].get("horizon"))
                     for b in covered]
        where = {b.snapshot: i for i, b in enumerate(covered)}
        order = [where[s] for s in _ordered_ok(mine, set(where))]
        doc = arm_skill(book, covered, decisions, window=boards, order=order, options=opts,
                        draws=protocol.draws, seed=protocol.seed, bound=bound,
                        base_block=base_block, population=len(boards), kind=arm.policy.kind)
        doc = {"policy": arm.policy.name, "complete": complete, **doc}
        if not complete:
            doc["verdict"] = verdict(doc, f"PARTIAL ({len(covered)}/{len(boards)} boards): ")
        doc_arms[arm.name] = doc
    return {"schema": SKILL_SCHEMA, "note": NOTE, "options": asdict(opts),
            "base_horizons": [str(h) for h in horizons], "excess_bound": bound,
            "menu_block": base_block, "components": list(COMPONENTS),
            "random_control": ("the random picker's excess, participation, horizon, direction "
                               "and selection are 0 by construction; its total is BASE"),
            "arms": doc_arms,
            "power": power_table(book, boards, horizons, options=opts)}


def progress_skill(boards: Sequence[Board], arms: Sequence[Arm],
                   receipts: Mapping[str, Mapping[str, Mapping[str, Any]]], get: GetFn,
                   memo: dict[str, Any], alpha: float = 0.05) -> dict[str, Any]:
    """The live field: per arm, the excess so far and the in-sample anytime
    CS of the run's final total excess (random decision order)."""
    book = memo.get("book")
    if not isinstance(book, ValueBook):
        book = memo["book"] = ValueBook(get, MENU_HORIZONS)
    if "bound" not in memo:
        memo["bound"] = excess_bound(boards)
    by_snapshot = {b.snapshot: b for b in boards}
    out: dict[str, Any] = {}
    for arm in arms:
        mine = receipts.get(arm.name, {})
        if _any_pair(mine):
            decided = _ordered_ok(mine, set(by_snapshot))
            out[arm.name] = {
                "decided": len(decided),
                "entered": sum(1 for s in decided if mine[s].get("choice") is not None),
                "excess": None, "in_sample_significant": None,
                "cs_total_excess": None, "cs_total_excess_asymptotic": None,
                "note": "pair arm: the single-row excess and decomposition are n/a "
                        "(see the digest's skill section)"}
            continue
        values: list[float] = []
        entered = 0
        for snap in _ordered_ok(mine, set(by_snapshot)):
            rec = mine[snap]
            choice = rec.get("choice")
            board = by_snapshot[snap]
            if choice is None or choice not in board.ids:
                values.append(0.0)
                continue
            entered += 1
            vals = book.values(board, rec.get("horizon"))
            values.append(float(vals[board.ids.index(choice)] - vals.mean()))
        live = monitor(values, len(boards), alpha=alpha, bound=memo["bound"])
        out[arm.name] = {
            "decided": live["observed"], "entered": entered,
            "excess": live["excess_observed"],
            "in_sample_significant": live["significant"],
            "cs_total_excess": (live["eb"] or live["asymptotic"] or {}).get("cs_total"),
            "cs_total_excess_asymptotic": (live["asymptotic"] or {}).get("cs_total")}
    return {"note": ("excess = pnl - the random row's mean on the same board and horizon. The "
                     "CS (empirical-Bernstein, Waudby-Smith & Ramdas 2024) is anytime-valid "
                     "for THIS run's final in-sample total excess under the random decision "
                     "order; it is not superpopulation skill (see the digest's forward CS "
                     "and block CIs)"),
            "arms": out}


def cockpit_projection(section: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(section, Mapping):
        return None
    arms = section.get("arms") or {}
    return {name: {"verdict": a.get("verdict"), "excess_total": a.get("excess_total"),
                   "excess_block_ci95": ((a.get("intervals") or {}).get("excess") or {})
                   .get("block_ci95"),
                   "forward_significant": (a.get("cs_forward") or {}).get("significant")}
            for name, a in arms.items() if isinstance(a, Mapping)}


def _ci(ci: Sequence[float | None] | None) -> str:
    if ci is None or any(v is None for v in ci):
        return "n/a"
    lo, hi = ci
    return f"[{float(lo or 0):+.0f}, {float(hi or 0):+.0f}]"


def skill_markdown(section: Mapping[str, Any]) -> list[str]:
    lines: list[str] = []
    add = lines.append
    add("## Skill accounting (exact counterfactual; descriptive, never promotes)")
    add("")
    if section.get("status") == "error":
        add(f"Unavailable: {section.get('error')}")
        add("")
        return lines
    add(section["note"])
    add("")
    add("pnl = base + participation + horizon + direction_tilt + direction_timing + selection "
        "(exact per board); excess = direction + selection; alpha = timing + selection; "
        "selection = structure + underlying + row. Component totals ($) are accounting "
        "identities; their uncertainty is in the second block (session vs circular-block "
        "bootstrap 95% CIs).")
    add("")
    width = max([len(str(n)) for n in section["arms"]] + [4])
    singles = [name for name, a in section["arms"].items()
               if a.get("decomposition") != "n/a"]
    if len(singles) != len(section["arms"]):
        pairs = [str(n) for n in section["arms"] if n not in singles]
        add(f"Two-leg package arm(s) {', '.join(pairs)}: decomposition n/a (the single-row "
            "counterfactual does not apply to a package); net on the paired scoreboard.")
        add("")
    add("```text")
    add(f"{'arm':<{width}} {'boards':>6} {'enter':>5} {'net':>8} {'base':>8} {'partic':>7} "
        f"{'horizon':>8} {'dirTilt':>8} {'dirTime':>8} {'select':>8} {'struct':>8} "
        f"{'undl':>7} {'row':>7} {'cost':>7} residual")
    for name in singles:
        a = section["arms"][name]
        c, s = a["components"], a["selection_split"]
        add(f"{name:<{width}} {a['boards']:>6} {a['entered']:>5} {a['net_total']:>+8.0f} "
            f"{c['base']:>+8.0f} {c['participation']:>+7.0f} {c['horizon']:>+8.0f} "
            f"{c['direction_tilt']:>+8.0f} {c['direction_timing']:>+8.0f} "
            f"{c['selection']:>+8.0f} {s['structure']:>+8.0f} {s['underlying']:>+7.0f} "
            f"{s['row']:>+7.0f} {a['cost_drag']:>+7.0f} {a['identity_residual']:.1e}")
    add("```")
    add("")
    add("```text")
    add(f"{'arm':<{width}} {'L':>2} {'blk':>5} {'net session CI':>17} {'net block CI':>17} "
        f"{'ratio':>6} {'ESS':>5}  {'excess [block CI]':<26} {'alpha [block CI]':<26} "
        "forward CS (total excess)")
    for name in singles:
        a = section["arms"][name]
        iv = a["intervals"]
        fwd = a["cs_forward"]
        excess = f"{iv['excess']['total']:+.0f} {_ci(iv['excess']['block_ci95'])}"
        alpha = f"{iv['alpha']['total']:+.0f} {_ci(iv['alpha']['block_ci95'])}"
        add(f"{name:<{width}} {a['block']['length']:>2} {iv['net']['blocks']:>5} "
            f"{_ci(iv['net']['session_ci95']):>17} {_ci(iv['net']['block_ci95']):>17} "
            f"{iv['net']['width_ratio']!s:>6} {iv['net']['ess_sessions']!s:>5}  "
            f"{excess:<26} {alpha:<26} {_ci(fwd.get('cs_total'))}"
            f"{'' if fwd.get('reliable') else ' (unreliable)'}")
    add("```")
    add("")
    for name, a in section["arms"].items():
        add(f"- {name}: {a['verdict']}")
    add("")
    power = section.get("power") or {}
    if power.get("horizons"):
        add(f"Power (one random-row trade per board; two-sided alpha {power['alpha']}, power "
            f"{power['power']}): minimum detectable per-trade effect in $.")
        add("")
        add("```text")
        add(f"{'horizon':<9} {'trades':>6} {'raw iid':>8} {'raw dep':>8} {'excess':>8} "
            f"{'bull dep':>8} {'VRF iid':>8} {'VRF dep':>8} block")
        for h, doc in power["horizons"].items():
            for row in doc["rows"]:
                add(f"{h:<9} {row['trades']:>6} {row['mde_raw_iid']:>8.2f} "
                    f"{row['mde_raw_dependent']:>8.2f} {row['mde_excess']:>8.2f} "
                    f"{row['mde_bullish_bet_dependent']!s:>8} "
                    f"{doc['vrf_iid']!s:>8} {doc['vrf_dependent']!s:>8} {doc['block']}")
        add("```")
        add("")
    return lines


# ------------------------------------------------------------ redigest


def _protocol_from_plan(plan: Mapping[str, Any], cfg: Mapping[str, Any]) -> Protocol:
    doc = plan.get("protocol")
    if not isinstance(doc, dict):
        return longrun.protocol_from_config(cfg)
    known = {f.name for f in fields(Protocol)}
    values = {k: v for k, v in doc.items() if k in known}
    if values.get("random_horizons") is not None:
        values["random_horizons"] = tuple(values["random_horizons"])
    return Protocol(**values)


def load_boards(run_dir: Path) -> list[Board]:
    boards: list[Board] = []
    with (run_dir / "boards.jsonl").open(encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                d = json.loads(line)
                boards.append(Board(d["snapshot"], d["session"], d["clock"], d["rows"],
                                    d.get("context")))
    return boards


def redigest(run_dir: Path, *, table: Path | None = None, out: Path | None = None,
             sessions: tuple[str | None, str | None] | None = None,
             arms: Sequence[str] | None = None,
             clock: Callable[[], datetime] = _utcnow) -> dict[str, Any]:
    """Re-score a run dir from its receipts + the outcome table: the full
    digest plus the skill section. ZERO model calls (no ask plug-in is ever
    built) and no bundle parse. In place only when the run is not live (its
    lock is free); ``out`` writes elsewhere and never touches the run dir.
    ``sessions`` (first, last; inclusive ISO dates, either open) re-scores
    only that window's boards - e.g. a period no prompt, rule or agent ever
    saw - and needs ``out`` (a window digest never replaces the run's).
    ``arms`` names an arm SUBSET (duplicates deduped; config order kept):
    only those arms are scored - the built-in controls are NOT forced in -
    paired on the boards where every named arm has an ok receipt, which is
    what a mid-run partial read needs (the executor shuffles its worklist,
    so the full pairing stays empty until the run is nearly done). It needs
    ``out`` (a subset digest never replaces the run's) and is labelled
    ARM SUBSET in the headline, the markdown and ``redigest["arms"]``."""
    if arms is not None and out is None:
        raise ValueError("an arm-subset redigest needs --out")
    named = []  # deduped, order-preserving, empty entries dropped
    for name in arms or ():
        if name and name not in named:
            named.append(str(name))
    if arms is not None and not named:
        raise ValueError("--arms names at least one arm (comma-separated; the built-in "
                         "controls are only included when named)")
    cfg = json.loads((run_dir / "config.json").read_text(encoding="utf-8"))
    plan = json.loads((run_dir / "plan.json").read_text(encoding="utf-8"))
    boards = load_boards(run_dir)
    if plan.get("boards_fingerprint") not in (None, longrun.boards_fingerprint(boards)):
        raise ValueError("boards.jsonl does not match the plan's boards fingerprint")
    if sessions is not None:
        if out is None:
            raise ValueError("a session-window redigest needs --out")
        first, last = sessions
        boards = [b for b in boards if (first is None or b.session >= first)
                  and (last is None or b.session <= last)]
        if not boards:
            raise ValueError(f"no boards in the session window {first}..{last}")
    policies = longrun.policies_from_config(cfg["policies"],
                                            builtin=bool(cfg.get("builtin_controls", True)),
                                            horizon=cfg.get("control_horizon"))
    every = longrun.arms_of(policies)
    if named:
        known = {a.name for a in every}
        unknown = [name for name in named if name not in known]
        if unknown:
            raise ValueError(f"unknown arm(s) {', '.join(unknown)}; the config's arms are: "
                             + ", ".join(a.name for a in every))
        keep = set(named)
        arms_used = [a for a in every if a.name in keep]  # config order, once each
    else:
        arms_used = every
    protocol = _protocol_from_plan(plan, cfg)
    outcome_cfg = dict(cfg.get("outcome") or {})
    table_path = table or (Path(str(outcome_cfg["table"])) if outcome_cfg.get("table") else None)
    if table_path is None:
        raise ValueError("redigest needs an outcome table (--table or the config's "
                         "outcome.table); it never parses the bundle or calls a model")
    if outcome_cfg.get("plugin") not in (None, "v2") and table is None:
        raise ValueError(f"outcome plug-in {outcome_cfg.get('plugin')!r} has no table form; "
                         "pass --table to rescore under the v2 table explicitly")
    ctx = longrun.PluginContext(config_dir=run_dir, boards=boards)
    outcome = longrun.plugin("outcome", "v2")(
        {"table": str(table_path),
         "default_horizon": outcome_cfg.get("default_horizon", "intraday")}, ctx)
    notes: list[str] = []
    bench_cfg = cfg.get("benchmarks") or {"plugin": "none"}
    try:
        benchmarks = longrun.plugin("benchmarks", str(bench_cfg.get("plugin", "none")))(
            bench_cfg, ctx)
    except (OSError, ValueError, KeyError, TypeError) as error:
        benchmarks = {}
        notes.append(f"benchmarks unavailable: {type(error).__name__}")
    receipts = {a.name: longrun.load_receipts(longrun.receipts_path(run_dir, a.name))
                for a in arms_used}
    if named and not any(all(receipts[a.name].get(b.snapshot, {}).get("ok") for a in arms_used)
                         for b in boards):
        raise ValueError("no board has an ok receipt for every named arm; nothing to pair")
    complete = all(receipts[a.name].get(b.snapshot, {}).get("ok")
                   for a in arms_used for b in boards)
    files = {a.name: str(longrun.receipts_path(run_dir, a.name)) for a in arms_used}

    def score() -> dict[str, Any]:
        doc = longrun.score_run(boards, arms_used, receipts, OutcomeCache(outcome), protocol,
                                benchmarks=benchmarks, receipts_files=files,
                                run_id=run_dir.name, plan_created=plan.get("created"),
                                complete=complete, clock=clock, skill_options=cfg.get("skill"))
        if named:  # a subset digest is never mistaken for the full pairing
            doc["headline"] = (f"ARM SUBSET ({doc['boards']['scored']} boards where all "
                               f"{len(arms_used)} arms answered) - {doc['headline']}")
        doc["redigest"] = {"at": clock().isoformat(), "model_calls": 0,
                           "source": "receipts on disk + the outcome table (no model, no bundle)",
                           "table": str(table_path), "notes": notes,
                           "written_to": str(out if out is not None else run_dir),
                           "arms": [a.name for a in arms_used] if named else None,
                           "sessions": None if sessions is None else {
                               "first": sessions[0], "last": sessions[1],
                               "boards": len(boards)}}
        return doc

    if out is not None:
        doc = score()
        out.mkdir(parents=True, exist_ok=True)
        longrun.write_digest(out, doc)
        return doc
    with longrun._run_lock(run_dir) as owned:
        if not owned:
            raise longrun.RunLocked(f"{run_dir.name} is live (its lock is held); pass --out DIR "
                                    "to redigest without touching it")
        doc = score()
        prior = run_dir / "digest.json"
        backup = run_dir / "digest.pre-redigest.json"
        if prior.is_file() and not backup.exists():
            prior.replace(backup)
        longrun.write_digest(run_dir, doc)
    return doc


def redigest_cli(args: argparse.Namespace) -> int:
    window: tuple[str | None, str | None] | None = None
    if getattr(args, "sessions", None):
        first, sep, last = str(args.sessions).partition(":")
        if not sep:
            print("longrun redigest: refused: --sessions is FIRST:LAST (either may be empty)",
                  file=sys.stderr)
            return 2
        window = (first or None, last or None)
    arm_names: list[str] | None = None
    if getattr(args, "arms", None) is not None:  # '' or 'a,b' given: never silently "all"
        arm_names = [name.strip() for name in str(args.arms).split(",")]
    try:
        doc = redigest(args.run_dir, table=args.table, out=args.out, sessions=window,
                       arms=arm_names)
    except longrun.RunLocked as error:
        print(f"longrun redigest: {error}", file=sys.stderr)
        return 3
    except (ValueError, OSError, KeyError, TypeError) as error:
        print(f"longrun redigest: refused: {error}", file=sys.stderr)
        return 2
    skill = doc.get("skill") or {}
    print(json.dumps({"headline": doc["headline"], "complete": doc["complete"],
                      "scored_boards": doc["boards"]["scored"],
                      "written_to": doc["redigest"]["written_to"], "model_calls": 0,
                      "skill_verdicts": {k: v.get("verdict")
                                         for k, v in (skill.get("arms") or {}).items()}},
                     indent=2))
    return 0
