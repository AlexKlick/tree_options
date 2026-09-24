"""Desk D6 price-path model: block bootstrap of standardized returns,
HAR-rescaled, with earnings-day draws and declared drifts.

Oracles are hand values (literal closes, a constant-variance series), the
exact variance of a circular block bootstrap computed here from the pool's
own autocovariances, and the martingale property of the declared drift.
"""

from __future__ import annotations

import hashlib
import math
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from tests.fixtures import desk_pricing as fx
from tree_options.desk import distribution as dist
from tree_options.desk import pit
from tree_options.desk.events import EarningsEvent
from tree_options.time.calendar import StaticSessionCalendar

D = date(2025, 3, 12)
NAMES = ("AAA", "SPY", "QQQ")
GK = 2.0 * math.log(2.0) - 1.0


@pytest.fixture(scope="module")
def cal() -> StaticSessionCalendar:
    return fx.trex_calendar()


@pytest.fixture(scope="module")
def panel(cal: StaticSessionCalendar) -> dict[str, dict[str, Any]]:
    return fx.synthetic_panel(cal, NAMES, date(2023, 1, 3), date(2025, 6, 30), seed=5)


def _sessions(cal: StaticSessionCalendar, start: date, n: int) -> list[date]:
    s = cal.sessions()
    i = s.index(start)
    return list(s[i : i + n])


# ------------------------------------------------------------- plumbing


def test_path_seed_is_the_declared_hash() -> None:
    want = int.from_bytes(hashlib.sha256(b"desk-paths/1|2025-03-12|AAA|row1").digest()[:16], "big")
    assert dist.path_seed(D, "AAA", "row1") == want
    assert dist.path_seed(D, "AAA", "row2") != want
    assert dist.path_seed(D, "BBB", "row1") != want
    assert dist.path_seed(date(2025, 3, 13), "AAA", "row1") != want


def test_block_indices_are_circular_runs() -> None:
    idx = dist.block_indices(np.random.default_rng(0), 7, 50, 8, 3)
    assert idx.shape == (50, 8)
    assert idx.min() >= 0 and idx.max() < 7
    for row in idx:
        for start in (0, 3, 6):
            for j in range(1, 3):
                if start + j < 8:
                    assert row[start + j] == (row[start] + j) % 7


def _constant_variance_bars(sessions: list[date]) -> tuple[dict[str, Any], list[float], float]:
    """Every session: open = prev close * e^a, close = open * e^b (b's sign
    alternates), high/low = e^+-c beyond the body; the proxy is constant."""
    a, b, c = 0.001, 0.01, 0.002
    close = 100.0
    bars: dict[str, Any] = {}
    returns = []
    for i, s in enumerate(sessions):
        bi = b if i % 2 == 0 else -b
        o = close * math.exp(a)
        cl = o * math.exp(bi)
        hi = max(o, cl) * math.exp(c)
        lo = min(o, cl) * math.exp(-c)
        bars[s.isoformat()] = {"open": str(o), "high": str(hi), "low": str(lo), "close": str(cl)}
        returns.append(math.log(cl / close))
        close = cl
    v = a * a + 0.5 * (b + 2 * c) ** 2 - GK * b * b
    return bars, returns, v


def test_standardized_returns_divide_by_the_prior_session_ewma(
    cal: StaticSessionCalendar,
) -> None:
    sessions = _sessions(cal, date(2024, 1, 2), 40)
    bars, r, v = _constant_variance_bars(sessions)
    # the EWMA is seeded at the 22nd proxy value (index 22: v[0] has no
    # previous close) and stays at v, so z_t = r_t / sqrt(v) from t = 23;
    # the event pair sessions 30, 31 are dropped
    z = dist.standardized_returns(bars, sessions, {sessions[30], sessions[31]}, window=40)
    want = [r[t] / math.sqrt(v) for t in range(23, 40) if t not in (30, 31)]
    assert z.tolist() == pytest.approx(want, rel=1e-9)
    # the window keeps the last 10 grid positions (30..39), minus the pair
    z10 = dist.standardized_returns(bars, sessions, {sessions[30], sessions[31]}, window=10)
    assert z10.tolist() == pytest.approx([r[t] / math.sqrt(v) for t in range(32, 40)], rel=1e-9)


def test_prepare_pool_restandardizes_and_fails_closed_when_short() -> None:
    raw = np.random.default_rng(3).normal(0.3, 2.0, size=300)
    z = dist.prepare_pool(raw)
    assert abs(float(z.mean())) < 1e-12
    assert float(z.std()) == pytest.approx(1.0, abs=1e-12)
    with pytest.raises(pit.NotEvaluable, match="standardized returns"):
        dist.prepare_pool(raw[: dist.MIN_POOL - 1])


def test_event_moves_are_two_session_log_moves(cal: StaticSessionCalendar) -> None:
    closes = {
        "2025-01-22": 100.0,
        "2025-01-23": 104.0,
        "2025-01-24": 110.0,
        "2025-01-31": 90.0,
        "2025-02-03": 80.0,
        "2025-02-04": 85.0,
        "2025-03-11": 50.0,
        "2025-03-12": 55.0,
    }
    bars = {d: {"close": str(c)} for d, c in closes.items()}
    reports = [
        date(2025, 1, 23),  # pair (01-23, 01-24), base 01-22
        date(2025, 2, 1),  # a Saturday: pair (02-03, 02-04), base 01-31
        date(2025, 2, 20),  # no bars: skipped
        date(2025, 3, 12),  # pair (03-12, 03-13) not realized by D: skipped
    ]
    got = dist.event_moves(bars, reports, D, cal)
    assert got == pytest.approx([math.log(110.0 / 100.0), math.log(85.0 / 90.0)], rel=1e-12)


def _ev(d: str, timing: str, status: str = "estimated") -> EarningsEvent:
    return EarningsEvent(date.fromisoformat(d), timing, status, "test", status == "estimated")


@pytest.mark.parametrize(
    ("event", "steps"),
    [
        (("2025-03-12", "bmo"), None),  # reacted on D: already in D's close
        (("2025-03-12", "amc"), (0,)),  # reacts on D+1; pair (D, D+1)
        (("2025-03-12", "unknown"), (0,)),
        (("2025-03-17", "unknown"), (2, 3)),
        (("2025-03-15", "unknown"), (2, 3)),  # Saturday: the pair starts Monday
        (("2025-03-26", "amc"), None),  # the exit session: reacts after the exit
        (("2025-03-26", "bmo"), (9,)),
        (("2025-03-26", "unknown"), (9,)),
        (("2025-03-27", "bmo"), None),
    ],
)
def test_path_events_by_timing(
    cal: StaticSessionCalendar, event: tuple[str, str], steps: tuple[int, ...] | None
) -> None:
    path = _sessions(cal, date(2025, 3, 13), 10)  # (D, 2025-03-26]
    assert path[-1] == date(2025, 3, 26)
    slots = dist.path_events([_ev(*event)], path, cal)
    if steps is None:
        assert slots == ()
    else:
        assert [s.steps for s in slots] == [steps]
        assert slots[0].report == date.fromisoformat(event[0])


def test_path_events_dedupe_prefers_the_record(cal: StaticSessionCalendar) -> None:
    path = _sessions(cal, date(2025, 3, 13), 10)
    slots = dist.path_events(
        [_ev("2025-03-17", "unknown"), _ev("2025-03-18", "amc", "sealed")], path, cal
    )
    assert [(s.report, s.status, s.steps) for s in slots] == [(date(2025, 3, 18), "sealed", (3, 4))]


# ------------------------------------------------------------ simulation


@pytest.fixture(scope="module")
def pool() -> np.ndarray:
    return dist.prepare_pool(np.random.default_rng(1).normal(size=5000))


def test_simulate_is_deterministic_and_seed_sensitive(pool: np.ndarray) -> None:
    kw: dict[str, Any] = dict(
        z_pool=pool, daily_var=4e-4, step_drift=np.zeros(10), n_paths=500, block=5
    )
    a = dist.simulate(seed=7, **kw)
    assert a.shape == (500, 10)
    assert np.array_equal(a, dist.simulate(seed=7, **kw))
    assert not np.array_equal(a, dist.simulate(seed=8, **kw))


def test_simulate_drift_is_a_martingale_offset(pool: np.ndarray) -> None:
    drift = np.full(10, 4e-4)
    paths = dist.simulate(
        z_pool=pool, daily_var=4e-4, step_drift=drift, n_paths=20_000, block=5, seed=11
    )
    growth = np.exp(paths[:, -1])
    se = float(growth.std()) / math.sqrt(len(growth))
    assert abs(float(growth.mean()) - math.exp(float(drift.sum()))) < 4 * se


def test_simulate_variance_is_the_block_bootstrap_variance(pool: np.ndarray) -> None:
    sigma2 = 4e-4
    paths = dist.simulate(
        z_pool=pool, daily_var=sigma2, step_drift=np.zeros(10), n_paths=20_000, block=5, seed=12
    )
    # two independent circular blocks of 5: var = 2 sigma^2 (5 + 2 sum_k (5-k) g_k),
    # g_k the pool's circular autocovariance at lag k
    g = [float(np.mean(pool * np.roll(pool, -k))) for k in range(1, 5)]
    want = 2 * sigma2 * (5 + 2 * sum((5 - k) * g[k - 1] for k in range(1, 5)))
    got = float(paths[:, -1].var())
    assert got == pytest.approx(want, rel=0.05)


def test_simulate_event_slot_replaces_the_pair_steps(pool: np.ndarray) -> None:
    drift = np.linspace(1e-4, 1e-3, 8)
    slot = dist.EventSlot(report=D, status="sealed", timing="unknown", steps=(3, 4))
    paths = dist.simulate(
        z_pool=pool,
        daily_var=4e-4,
        step_drift=drift,
        n_paths=1000,
        block=5,
        seed=13,
        events=(slot,),
        event_moves=(0.10, -0.06),
    )
    x = np.diff(paths, axis=1, prepend=0.0)
    # centered moves +-0.08, normalized so E[exp(move)] = 1
    lm = math.log((math.exp(0.08) + math.exp(-0.08)) / 2.0)
    up, down = 0.08 - lm + drift[3], -0.08 - lm + drift[3]
    hit = np.isclose(x[:, 3], up, atol=1e-12) | np.isclose(x[:, 3], down, atol=1e-12)
    assert hit.all()
    assert np.isclose(x[:, 3], up, atol=1e-12).any() and np.isclose(x[:, 3], down, atol=1e-12).any()
    assert np.allclose(x[:, 4], drift[4], atol=1e-12)
    assert not np.allclose(x[:, 2], drift[2], atol=1e-6)  # an ordinary step


def test_simulate_paths_wires_calendar_rate_and_signal(
    cal: StaticSessionCalendar, pool: np.ndarray
) -> None:
    exit_session = cal.nth_after(D, 10)
    signal = dist.SignalDrift(excess_20=0.03, weight=0.5)
    ps = dist.simulate_paths(
        name="AAA",
        session=D,
        exit_session=exit_session,
        cal=cal,
        row_id="r",
        z_pool=pool,
        daily_var=4e-4,
        rate=0.05,
        signal=signal,
        n_paths=20_000,
    )
    steps = _sessions(cal, date(2025, 3, 13), 10)
    assert ps.spec.steps == tuple(steps)
    prev = [D, *steps[:-1]]
    assert ps.spec.step_days == tuple(float((s - p).days) for s, p in zip(steps, prev, strict=True))
    assert ps.spec.seed == dist.path_seed(D, "AAA", "r")
    growth = np.exp(ps.terminal())
    se = float(growth.std()) / math.sqrt(len(growth))
    days = (exit_session - D).days
    assert abs(float(growth.mean()) - math.exp(0.05 * days / 365.0)) < 4 * se
    per = 0.5 * math.log(1.03) / 20.0
    assert ps.signal_offset is not None
    assert ps.signal_offset.tolist() == pytest.approx([per * (k + 1) for k in range(10)], rel=1e-12)
    assert np.array_equal(ps.terminal(signal=True), ps.terminal() + ps.signal_offset[-1])
    no_signal = dist.simulate_paths(
        name="AAA", session=D, exit_session=exit_session, cal=cal, row_id="r",
        z_pool=pool, daily_var=4e-4, rate=0.05, n_paths=20_000,
    )  # fmt: skip
    assert no_signal.signal_offset is None
    assert np.array_equal(no_signal.log_paths, ps.log_paths)  # common random numbers
    with pytest.raises(ValueError, match="signal"):
        no_signal.terminal(signal=True)


# ------------------------------------------------------------ build_paths


SEALED = {
    "AAA": [
        "2023-04-27", "2023-07-27", "2023-10-26", "2024-01-25", "2024-04-25",
        "2024-07-25", "2024-10-24", "2025-01-23", "2025-04-24", "2025-07-24",
    ],
    "SPY": [],
}  # fmt: skip


def _t(timing: str, status: str, fetched_at: str) -> dict[str, str]:
    return {"timing": timing, "status": status, "fetched_at": fetched_at, "source": "test"}


TIMING = {
    "AAA": {
        "2023-01-26": _t("amc", "confirmed", "2026-09-01T12:00:00-04:00"),
        "2025-01-23": _t("amc", "confirmed", "2026-09-01T12:00:00-04:00"),
        "2025-03-19": _t("unknown", "estimated", "2025-03-03T21:00:00-05:00"),
        "2025-04-10": _t("bmo", "estimated", "2025-03-03T21:00:00-05:00"),
    }
}


def _pit(
    cal: StaticSessionCalendar,
    panel: dict[str, Any],
    store: Path,
    *,
    sealed: dict[str, list[str]] | None = None,
    timing: dict[str, Any] | None = None,
) -> pit.PointInTime:
    fx.write_index(store, "DTB3", [("2025-03-10", "4.30"), ("2025-03-11", "4.31")])
    src = pit.Sources(
        panel=panel,
        sealed=SEALED if sealed is None else sealed,
        timing=TIMING if timing is None else timing,
        iv_history=None,
        store=store,
    )
    return pit.PointInTime(D, cal, src, har_names=NAMES)


def test_build_paths_end_to_end(
    cal: StaticSessionCalendar, panel: dict[str, Any], tmp_path: Path
) -> None:
    p = _pit(cal, panel, tmp_path)
    exit_session = date(2025, 4, 16)  # D + 25 sessions
    ps = dist.build_paths(p, "AAA", exit_session, row_id="xsmom", n_paths=2000)
    spec = ps.spec
    assert len(spec.steps) == 25 and spec.steps[-1] == exit_session
    assert spec.rate == pytest.approx(0.0431)
    assert spec.daily_var == p.har(20)["AAA"].forecast_ex_events / 20
    # 03-19 (unknown): pair (03-19, 03-20) = steps 4, 5; 04-10 (bmo): (04-10, 04-11) = 20, 21
    assert [(s.report, s.steps) for s in spec.events] == [
        (date(2025, 3, 19), (4, 5)),
        (date(2025, 4, 10), (20, 21)),
    ]
    # the record: 2023-01-26 (confirmed) + the eight sealed reports through 2025-01-23
    assert spec.n_event_moves == 9
    assert spec.schedule == "complete"
    assert ps.log_paths.shape == (2000, 25)
    again = dist.build_paths(p, "AAA", exit_session, row_id="xsmom", n_paths=2000)
    assert np.array_equal(ps.log_paths, again.log_paths)
    other = dist.build_paths(p, "AAA", exit_session, row_id="pead", n_paths=2000)
    assert not np.array_equal(ps.log_paths, other.log_paths)
    etf = dist.build_paths(p, "SPY", exit_session, row_id="xsmom", n_paths=2000)
    assert etf.spec.events == () and etf.spec.schedule == "n/a"


def test_build_paths_refuses_an_unknown_schedule(
    cal: StaticSessionCalendar, panel: dict[str, Any], tmp_path: Path
) -> None:
    timing = {"AAA": {k: v for k, v in TIMING["AAA"].items() if k < "2025-03-12"}}
    p = _pit(cal, panel, tmp_path, timing=timing)
    # last record 2025-01-23; a report could land before 2025-04-16
    with pytest.raises(pit.NotEvaluable, match="schedule"):
        dist.build_paths(p, "AAA", date(2025, 4, 16), row_id="x", n_paths=100)
    # 2025-01-23 + 56 days > 2025-03-14: no report can land inside a 2-session hold
    ps = dist.build_paths(p, "AAA", date(2025, 3, 14), row_id="x", n_paths=100)
    assert ps.spec.schedule == "complete" and ps.spec.events == ()


def test_build_paths_refuses_too_few_event_moves(
    cal: StaticSessionCalendar, panel: dict[str, Any], tmp_path: Path
) -> None:
    sealed = {"AAA": ["2024-10-24", "2025-01-23"], "SPY": []}
    timing = {"AAA": {k: v for k, v in TIMING["AAA"].items() if k > "2025-03-12"}}
    p = _pit(cal, panel, tmp_path, sealed=sealed, timing=timing)
    with pytest.raises(pit.NotEvaluable, match="earnings moves"):
        dist.build_paths(p, "AAA", date(2025, 4, 16), row_id="x", n_paths=100)


def test_build_paths_refuses_a_bad_exit(
    cal: StaticSessionCalendar, panel: dict[str, Any], tmp_path: Path
) -> None:
    p = _pit(cal, panel, tmp_path)
    with pytest.raises(pit.NotEvaluable, match="exit"):
        dist.build_paths(p, "SPY", D, row_id="x", n_paths=100)
    with pytest.raises(pit.NotEvaluable, match="exit"):
        dist.build_paths(p, "SPY", date(2025, 3, 15), row_id="x", n_paths=100)  # Saturday
