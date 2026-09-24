"""Price paths for one name, from decision session D to a planned exit
session (plan D6). Every input comes through :class:`desk.pit.PointInTime`.

The model, per path step k (the sessions in (D, exit], in order)::

    x_k = sigma z_k - ln M(sigma) + mu_k            an ordinary session
    x_k = e_j - ln M_e + mu_k                       the first session of a
                                                    known earnings pair
    x_k = mu_k                                      its second session
    ln(S_k / S_0) = x_1 + ... + x_k

* z: the name's standardized daily returns, ln(C_t/C_{t-1}) divided by the
  point-in-time daily vol sqrt(EWMA_{t-1}), the FORECAST-001 EWMA(0.94) of
  the earnings-cleaned variance proxy (``desk.rv``, ``har.clean_proxy``)
  known the session before (filtered historical simulation). Earnings pair
  sessions are left out (they are drawn separately); the last
  POOL_SESSIONS grid sessions up to D are used, at least MIN_POOL returns,
  re-standardized to mean 0 and variance 1 so that the HAR forecast alone
  sets the scale and the declared drift alone the mean.
* sigma^2: the HAR per-session variance at D, NO-EVENT baseline
  (``HarPit.forecast_ex_events / h``, h = HAR_H = 20, FORECAST-001's
  validated horizon), for every ordinary step whatever the path length.
* z_k is drawn by a circular block bootstrap, blocks of BLOCK_SESSIONS = 5
  consecutive pool returns (wrapping at the pool's end, so every return
  has the same marginal probability). Why 5: EWMA standardization removes
  most volatility clustering, and what dependence is left in z and |z|
  (short reversals, residual clustering, day-of-week effects) decays within
  a trading week; 5 keeps that week intact. It is still short enough that a
  20-session hold mixes 4 independent blocks (n_pool^4 block combinations);
  the n^(1/3) rule (~9 for a 756-return pool) would leave about 2 blocks
  per path and much less path variety. The same length is used for every
  path length.
* earnings: a known report (``PointInTime.earnings``: record, estimate or
  known upcoming date) whose reaction session lies in (D, exit] replaces
  the in-path sessions of its event pair {s(R), next session} (HAR's
  convention) with ONE draw from the name's own history of pair moves
  ln(C_{pair end} / C_{before the pair}) over reports on record whose pair
  completed by D; the pool is centered (the average reaction is not a
  view) and at least MIN_EVENT_MOVES long. Reaction session: s(R) for bmo,
  the next session for amc, either for unknown timing. When only one pair
  session is inside the path it carries the whole 2-session move (one
  ordinary session too many: conservative). A reporter whose known dates
  do not pin the event count through the exit is refused (fail closed),
  except when the last known report is so recent that the quarterly
  spacing (>= MIN_REPORT_GAP_DAYS) rules a new one out.
* drift: mu_k = r d_k / 365, r = DTB3 at the cutoff and d_k the calendar
  days of step k (the Black-Scholes clock: with no view the exit repricing
  is arbitrage-consistent with the paths); ln M(sigma) and ln M_e (the
  pools' empirical moment-generating functions) make E[exp(x_k)] =
  exp(mu_k) exactly for each marginal draw. A signal row adds
  w ln(1 + excess_20) / 20 per session (the backtest's 20-session excess,
  weight w passed in), kept as a separate offset over the SAME draws
  (common random numbers: no-view and signal EV differ by the view only).
* q = 0 (declared, as in ``desk.surface``).
* N_PATHS = 20,000 paths, seeded from sha256("desk-paths/1|D|name|row");
  the draw order is fixed (block starts, then one event draw per slot in
  path order), so a run is reproducible on the pinned numpy.
"""

from __future__ import annotations

import bisect
import hashlib
import itertools
import math
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

import numpy as np

from tree_options.desk import har, rv
from tree_options.desk.events import EarningsEvent
from tree_options.desk.pit import EarningsPit, NotEvaluable, PointInTime
from tree_options.desk.sessions import Calendar, calendar_days_between, previous_session

N_PATHS = 20_000
BLOCK_SESSIONS = 5
POOL_SESSIONS = 756  # three years of sessions
MIN_POOL = 250
MIN_EVENT_MOVES = 4
MIN_REPORT_GAP_DAYS = 56  # the sealed calendar's quarterly spacing is >= 60 days
HAR_H = 20
DEDUP_SESSIONS = 2  # two report dates this close are one quarter's report
SEED_SCHEMA = "desk-paths/1"
# preference among duplicate dates of one report: the record first
_RANK = {"confirmed": 0, "sealed": 0, "estimated": 2, "sealed-assumed": 3}


@dataclass(frozen=True)
class SignalDrift:
    """A signal row's view: the backtest's 20-session excess return (simple)
    and its weight (the sealed playbook fixes it; never assumed here)."""

    excess_20: float
    weight: float

    def __post_init__(self) -> None:
        if not (math.isfinite(self.excess_20) and self.excess_20 > -1.0):
            raise ValueError(f"excess_20 must be finite and > -1: {self.excess_20}")
        if not (math.isfinite(self.weight) and self.weight >= 0.0):
            raise ValueError(f"weight must be finite and >= 0: {self.weight}")

    def per_session(self) -> float:
        return self.weight * math.log1p(self.excess_20) / 20.0


@dataclass(frozen=True)
class EventSlot:
    """A known report whose pair move lands inside the path."""

    report: date
    status: str
    timing: str
    steps: tuple[int, ...]  # 0-based path steps of its in-path pair sessions


@dataclass(frozen=True)
class PathSpec:
    name: str
    session: date
    exit_session: date
    row_id: str
    steps: tuple[date, ...]  # the sessions (D, exit]
    step_days: tuple[float, ...]  # calendar days of each step
    daily_var: float
    rate: float
    signal: SignalDrift | None
    events: tuple[EventSlot, ...]
    n_pool: int
    n_event_moves: int
    block: int
    n_paths: int
    seed: int
    schedule: str  # n/a | complete
    schedule_reason: str
    har_h: int
    har_forecast: float | None  # with the known events (information)
    be_estimated: bool | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "session": self.session.isoformat(),
            "exit_session": self.exit_session.isoformat(),
            "row_id": self.row_id,
            "n_steps": len(self.steps),
            "daily_var": self.daily_var,
            "rate": self.rate,
            "signal": (
                None
                if self.signal is None
                else {"excess_20": self.signal.excess_20, "weight": self.signal.weight}
            ),
            "events": [
                {
                    "report": e.report.isoformat(),
                    "status": e.status,
                    "timing": e.timing,
                    "steps": list(e.steps),
                }
                for e in self.events
            ],
            "n_pool": self.n_pool,
            "n_event_moves": self.n_event_moves,
            "block": self.block,
            "n_paths": self.n_paths,
            "seed": str(self.seed),
            "schedule": self.schedule,
            "schedule_reason": self.schedule_reason,
            "har_h": self.har_h,
            "har_forecast": self.har_forecast,
            "be_estimated": self.be_estimated,
        }


@dataclass(frozen=True)
class PathSet:
    spec: PathSpec
    log_paths: np.ndarray  # (n_paths, n_steps): ln(S_k / S_0) under the no-view drift
    signal_offset: np.ndarray | None  # (n_steps,): cumulative extra log drift of the view

    def terminal(self, *, signal: bool = False) -> np.ndarray:
        """ln(S_exit / S_0) per path (no-view, or with the signal view)."""
        out = self.log_paths[:, -1]
        if not signal:
            return out
        if self.signal_offset is None:
            raise ValueError("these paths carry no signal view")
        return out + self.signal_offset[-1]


# ------------------------------------------------------------ primitives


def path_seed(session: date, name: str, row_id: str) -> int:
    key = f"{SEED_SCHEMA}|{session.isoformat()}|{name}|{row_id}".encode()
    return int.from_bytes(hashlib.sha256(key).digest()[:16], "big")


def block_indices(
    rng: np.random.Generator, n_pool: int, n_paths: int, n_steps: int, block: int
) -> np.ndarray:
    """(n_paths, n_steps) pool indices: runs of ``block`` consecutive
    indices (mod n_pool) from uniform starts."""
    n_blocks = -(-n_steps // block)
    starts = rng.integers(0, n_pool, size=(n_paths, n_blocks))
    idx = (starts[:, :, None] + np.arange(block)[None, None, :]) % n_pool
    return idx.reshape(n_paths, n_blocks * block)[:, :n_steps]


def _close(bar: Mapping[str, Any] | None) -> float | None:
    if bar is None:
        return None
    try:
        c = float(bar["close"])
    except (KeyError, TypeError, ValueError):
        return None
    return c if c > 0.0 else None


def standardized_returns(
    bars: Mapping[str, Mapping[str, Any]],
    sessions: Sequence[date],
    event_sessions: Collection[date],
    *,
    window: int = POOL_SESSIONS,
) -> np.ndarray:
    """z_t = ln(C_t/C_{t-1}) / sqrt(EWMA_{t-1}) over the consecutive
    ``sessions`` (the grid ending at D), for t among the last ``window``
    grid positions, skipping event sessions and any missing input. The
    EWMA runs over the whole grid on the earnings-cleaned proxy."""
    iso = [s.isoformat() for s in sessions]
    v = rv.proxy_series(bars, iso)
    marks = [i for i, s in enumerate(sessions) if s in event_sessions]
    ew = rv.ewma_series(har.clean_proxy(v, marks))
    out: list[float] = []
    for t in range(max(1, len(sessions) - window), len(sessions)):
        if sessions[t] in event_sessions:
            continue
        c0, c1, s2 = _close(bars.get(iso[t - 1])), _close(bars.get(iso[t])), ew[t - 1]
        if c0 is None or c1 is None or s2 is None or s2 <= 0.0:
            continue
        out.append(math.log(c1 / c0) / math.sqrt(s2))
    return np.array(out, dtype=np.float64)


def prepare_pool(z: np.ndarray, *, min_n: int = MIN_POOL) -> np.ndarray:
    """Mean 0, variance 1 (population); NotEvaluable when too short."""
    if len(z) < min_n:
        raise NotEvaluable(f"{len(z)} standardized returns < {min_n}")
    sd = float(z.std())
    if not sd > 0.0:
        raise NotEvaluable("standardized returns have no variance")
    return (z - float(z.mean())) / sd


def event_pair(d: date, cal: Calendar) -> tuple[date, date] | None:
    """(s(R), next session): the first session on/after the report date."""
    sessions = cal.sessions()
    i = bisect.bisect_left(sessions, d)
    if i + 1 >= len(sessions):
        return None
    return sessions[i], sessions[i + 1]


def event_moves(
    bars: Mapping[str, Mapping[str, Any]], reports: Iterable[date], session: date, cal: Calendar
) -> list[float]:
    """ln(C at the pair's second session / C before the pair) for every
    report whose pair completed by ``session`` (both closes present)."""
    out = []
    for rep in sorted(set(reports)):
        pair = event_pair(rep, cal)
        if pair is None or pair[1] > session:
            continue
        before = previous_session(pair[0], cal)
        a = _close(bars.get(before.isoformat())) if before is not None else None
        b = _close(bars.get(pair[1].isoformat()))
        if a is not None and b is not None:
            out.append(math.log(b / a))
    return out


def dedupe_events(evs: Iterable[EarningsEvent], cal: Calendar) -> list[EarningsEvent]:
    """One event per report: dates whose sessions lie within DEDUP_SESSIONS
    of a better-ranked (record first, then earlier) date are dropped."""
    sessions = cal.sessions()
    kept: list[tuple[int, EarningsEvent]] = []
    for e in sorted(evs, key=lambda e: (_RANK.get(e.status, 1), e.date)):
        pos = bisect.bisect_left(sessions, e.date)
        if all(abs(pos - p) > DEDUP_SESSIONS for p, _ in kept):
            kept.append((pos, e))
    return [e for _, e in sorted(kept, key=lambda x: x[1].date)]


def path_events(
    evs: Iterable[EarningsEvent], steps: Sequence[date], cal: Calendar
) -> tuple[EventSlot, ...]:
    """The known reports whose reaction lands inside ``steps`` (the path
    sessions, in order), with the in-path steps of their pairs."""
    where = {s: k for k, s in enumerate(steps)}
    out = []
    for e in dedupe_events(evs, cal):
        pair = event_pair(e.date, cal)
        if pair is None:
            continue
        reaction = {"bmo": (pair[0],), "amc": (pair[1],)}.get(e.timing, pair)
        if not any(s in where for s in reaction):
            continue
        slot = tuple(where[s] for s in pair if s in where)
        out.append(EventSlot(report=e.date, status=e.status, timing=e.timing, steps=slot))
    return tuple(sorted(out, key=lambda s: s.steps))


def schedule_through(
    earn: EarningsPit, session: date, exit_session: date, cal: Calendar
) -> tuple[str, str]:
    """Whether the known report dates pin the events in (D, exit]:
    ``complete`` when a known report lies past the exit with no missing
    quarter (``har.schedule_status``), or when the latest known report on or
    before the exit is so recent that no new one can land by the exit (and
    none is missing up to it); else incomplete / unavailable with why."""
    if not earn.reporter:
        return "n/a", ""
    dates = earn.known_dates()
    status, reason = har.schedule_status(dates, session, exit_session, cal)
    if status == "complete":
        return "complete", ""
    known = [date.fromisoformat(d) for d in dates]
    upto = [d for d in known if d <= exit_session]
    if not upto:
        return status, reason
    last = upto[-1]
    gap = calendar_days_between(last.isoformat(), exit_session.isoformat())
    if gap >= MIN_REPORT_GAP_DAYS:
        return status, reason
    base = [d for d in known if d <= session][-1:]
    chain = base + [d for d in upto if d > session]
    for a, b in itertools.pairwise(chain):
        if calendar_days_between(a.isoformat(), b.isoformat()) > har.MAX_REPORT_GAP_DAYS:
            return "incomplete", f"a quarter is missing between {a} and {b}"
    return "complete", (
        f"spacing: the last known report {last} is {gap:.0f} < {MIN_REPORT_GAP_DAYS} days "
        f"before the exit {exit_session}"
    )


# ------------------------------------------------------------ simulation


def simulate(
    *,
    z_pool: np.ndarray,
    daily_var: float,
    step_drift: np.ndarray,
    n_paths: int,
    block: int,
    seed: int,
    events: Sequence[EventSlot] = (),
    event_moves: Sequence[float] = (),
) -> np.ndarray:
    """Cumulative log paths (n_paths, n_steps) under the module's model."""
    n_steps = len(step_drift)
    if n_steps < 1 or n_paths < 1:
        raise ValueError("need at least one step and one path")
    if not (math.isfinite(daily_var) and daily_var > 0.0):
        raise ValueError(f"daily_var must be positive: {daily_var}")
    rng = np.random.default_rng(seed)
    sigma = math.sqrt(daily_var)
    idx = block_indices(rng, len(z_pool), n_paths, n_steps, block)
    x = sigma * z_pool[idx]
    x -= math.log(float(np.mean(np.exp(sigma * z_pool))))
    if events:
        moves = np.asarray(event_moves, dtype=np.float64)
        if len(moves) == 0:
            raise ValueError("event slots need a pool of event moves")
        moves = moves - float(moves.mean())
        lm = math.log(float(np.mean(np.exp(moves))))
        for slot in events:
            j = rng.integers(0, len(moves), size=n_paths)
            x[:, slot.steps[0]] = moves[j] - lm
            for s in slot.steps[1:]:
                x[:, s] = 0.0
    x += step_drift[None, :]
    return np.cumsum(x, axis=1)


def simulate_paths(
    *,
    name: str,
    session: date,
    exit_session: date,
    cal: Calendar,
    row_id: str,
    z_pool: np.ndarray,
    daily_var: float,
    rate: float,
    signal: SignalDrift | None = None,
    events: Sequence[EventSlot] = (),
    event_moves: Sequence[float] = (),
    n_paths: int = N_PATHS,
    block: int = BLOCK_SESSIONS,
    schedule: str = "n/a",
    schedule_reason: str = "",
    har_h: int = HAR_H,
    har_forecast: float | None = None,
    be_estimated: bool | None = None,
) -> PathSet:
    """The pure model from explicit inputs (``build_paths`` feeds it from a
    PointInTime): steps and their calendar days come from ``cal``."""
    if not (cal.is_session(exit_session) and exit_session > session):
        raise NotEvaluable(f"the exit {exit_session} is not a session after {session}")
    sessions = cal.sessions()
    lo = bisect.bisect_right(sessions, session)
    hi = bisect.bisect_right(sessions, exit_session)
    steps = tuple(sessions[lo:hi])
    prev = (session, *steps[:-1])
    days = tuple(
        calendar_days_between(a.isoformat(), b.isoformat())
        for a, b in zip(prev, steps, strict=True)
    )
    drift = rate * np.array(days, dtype=np.float64) / 365.0
    seed = path_seed(session, name, row_id)
    log_paths = simulate(
        z_pool=z_pool,
        daily_var=daily_var,
        step_drift=drift,
        n_paths=n_paths,
        block=block,
        seed=seed,
        events=events,
        event_moves=event_moves,
    )
    offset = None
    if signal is not None:
        offset = np.cumsum(np.full(len(steps), signal.per_session(), dtype=np.float64))
    spec = PathSpec(
        name=name,
        session=session,
        exit_session=exit_session,
        row_id=row_id,
        steps=steps,
        step_days=days,
        daily_var=daily_var,
        rate=rate,
        signal=signal,
        events=tuple(events),
        n_pool=len(z_pool),
        n_event_moves=len(event_moves),
        block=block,
        n_paths=n_paths,
        seed=seed,
        schedule=schedule,
        schedule_reason=schedule_reason,
        har_h=har_h,
        har_forecast=har_forecast,
        be_estimated=be_estimated,
    )
    return PathSet(spec=spec, log_paths=log_paths, signal_offset=offset)


def build_paths(
    pit: PointInTime,
    name: str,
    exit_session: date,
    *,
    row_id: str,
    signal: SignalDrift | None = None,
    n_paths: int = N_PATHS,
    block: int = BLOCK_SESSIONS,
    har_h: int = HAR_H,
) -> PathSet:
    """Paths for ``name`` from ``pit.session`` to ``exit_session`` from
    point-in-time inputs only; NotEvaluable (with the reason) when any
    input is missing or the earnings schedule is not pinned."""
    session, cal = pit.session, pit.cal
    if not (cal.is_session(exit_session) and exit_session > session):
        raise NotEvaluable(f"{name}: the exit {exit_session} is not a session after {session}")
    hp = pit.har(har_h).get(name)
    if hp is None:
        raise NotEvaluable(f"{name}: no HAR forecast at {session}")
    rate = pit.rate()
    if rate is None:
        raise NotEvaluable(f"{name}: no DTB3 observation knowable at {session}")
    bars = pit.bars(name)
    if not bars:
        raise NotEvaluable(f"{name}: no panel bars")
    earn = pit.earnings(name)
    status, reason = schedule_through(earn, session, exit_session, cal)
    if status not in ("n/a", "complete"):
        raise NotEvaluable(f"{name}: earnings schedule {status} through {exit_session}: {reason}")
    all_sessions = cal.sessions()
    first = date.fromisoformat(min(bars))
    grid = all_sessions[
        bisect.bisect_left(all_sessions, first) : bisect.bisect_right(all_sessions, session)
    ]
    past = [e for e in earn.all_known() if e.date <= session]
    excluded = {s for e in past if (pair := event_pair(e.date, cal)) is not None for s in pair}
    pool = prepare_pool(standardized_returns(bars, grid, excluded))
    slots: tuple[EventSlot, ...] = ()
    moves: list[float] = []
    if earn.reporter:
        lo = bisect.bisect_right(all_sessions, session)
        steps = all_sessions[lo : bisect.bisect_right(all_sessions, exit_session)]
        slots = path_events(earn.all_known(), steps, cal)
        record = [e.date for e in dedupe_events(earn.past, cal)]
        moves = event_moves(bars, record, session, cal)
        if slots and len(moves) < MIN_EVENT_MOVES:
            raise NotEvaluable(
                f"{name}: {len(moves)} historical earnings moves < {MIN_EVENT_MOVES} "
                f"for {len(slots)} report(s) inside the path"
            )
    return simulate_paths(
        name=name,
        session=session,
        exit_session=exit_session,
        cal=cal,
        row_id=row_id,
        z_pool=pool,
        daily_var=hp.forecast_ex_events / har_h,
        rate=rate,
        signal=signal,
        events=slots,
        event_moves=moves,
        n_paths=n_paths,
        block=block,
        schedule=status,
        schedule_reason=reason,
        har_h=har_h,
        har_forecast=hp.forecast,
        be_estimated=hp.be is not None,
    )
