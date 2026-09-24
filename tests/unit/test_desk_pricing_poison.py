"""Desk D6 look-ahead guard: pit + distribution + pricing end to end.

Altering every datum a decision for session D could not know (prices, IV,
chains, events, indices dated or published after D's cutoff) must leave the
whole output byte-identical; altering a datum D could know must change it
(positive controls, so the guard is not vacuous). Also: the candidate API's
refusals and its memory footprint at the production path count.
"""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
import math
import shutil
import tracemalloc
from collections.abc import Callable
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from tests.fixtures import desk_pricing as fx
from tree_options.desk import distribution as dist
from tree_options.desk import pit, pricing
from tree_options.time.calendar import StaticSessionCalendar
from tree_options.trex.plan import ExitRules, Leg, LegStructure

D = date(2025, 3, 12)
ENTRY = date(2025, 3, 13)
EXIT = date(2025, 4, 16)  # D + 25 sessions: the 03-19 and 04-10 estimates fall inside
EXP1 = date(2025, 5, 16)
EXP2 = date(2025, 6, 20)
NAMES = ("AAA", "SPY", "QQQ")
ROW = "xsmom-call-debit"
SIGNAL = dist.SignalDrift(excess_20=0.02, weight=0.5)
N = 4000
SPOT = 100.0


def _t(timing: str, status: str, fetched_at: str) -> dict[str, str]:
    return {"timing": timing, "status": status, "fetched_at": fetched_at, "source": "test"}


SEALED = {
    "AAA": [
        "2023-04-27", "2023-07-27", "2023-10-26", "2024-01-25", "2024-04-25",
        "2024-07-25", "2024-10-24", "2025-01-23", "2025-04-24", "2025-07-24",
    ],
    "SPY": [],
}  # fmt: skip
TIMING = {
    "AAA": {
        "2023-01-26": _t("amc", "confirmed", "2026-09-01T12:00:00-04:00"),
        "2025-01-23": _t("amc", "confirmed", "2026-09-01T12:00:00-04:00"),
        "2025-03-19": _t("unknown", "estimated", "2025-03-03T21:00:00-05:00"),
        "2025-04-10": _t("bmo", "estimated", "2025-03-03T21:00:00-05:00"),
        "2025-07-24": _t("unknown", "estimated", "2025-06-01T21:00:00-04:00"),
    }
}


def _struct(
    sid: str, kind: str, legs: list[tuple[str, str, float, date]], limit: str
) -> LegStructure:
    return LegStructure(
        id=sid,
        underlying="AAA",
        kind=kind,  # type: ignore[arg-type]
        legs=tuple(
            Leg(right=r, action=a, strike=Decimal(str(k)), expiry=e)  # type: ignore[arg-type]
            for r, a, k, e in legs
        ),
        quantity=1,
        entry_date=ENTRY,
        exit_deadline=EXIT,
        limit=Decimal(limit),
        exits=ExitRules(touch=False, breach=False),
    )


STRUCTS = [
    _struct("cv", "debit_vertical", [("C", "BUY", 100.0, EXP1), ("C", "SELL", 110.0, EXP1)], "4"),
    _struct(
        "ic",
        "iron_condor",
        [
            ("P", "BUY", 85.0, EXP1),
            ("P", "SELL", 90.0, EXP1),
            ("C", "SELL", 110.0, EXP1),
            ("C", "BUY", 115.0, EXP1),
        ],
        "1",
    ),
    _struct("cal", "calendar", [("C", "SELL", 100.0, EXP1), ("C", "BUY", 100.0, EXP2)], "4"),
    _struct("bad", "debit_vertical", [("C", "BUY", 101.0, EXP1), ("C", "SELL", 110.0, EXP1)], "4"),
]


@pytest.fixture(scope="module")
def cal() -> StaticSessionCalendar:
    return fx.trex_calendar()


def _chain(spot: float, session: date, as_of: str, rate: float = 0.0431) -> dict[str, Any]:
    return fx.chain_doc(
        sym="AAA",
        session=session,
        spot=spot,
        rate=rate,
        expiries=[EXP1, EXP2],
        strikes=[float(k) for k in range(70, 135, 5)],
        iv=fx.smile,
        half_spread=fx.half_spread,
        source_as_of=as_of,
    )


@dataclasses.dataclass
class Inputs:
    panel: dict[str, Any]
    sealed: dict[str, list[str]]
    timing: dict[str, Any]
    iv: dict[str, dict[str, float | None]]
    dtb3: list[tuple[str, str]]
    vix: list[tuple[str, str]]
    chains: list[tuple[dict[str, Any], bool]]  # (doc, conflict)


def _base(cal: StaticSessionCalendar) -> Inputs:
    panel = fx.synthetic_panel(cal, NAMES, date(2023, 1, 3), date(2025, 6, 30), seed=3)
    # drop a fifth of the post-D bars, so the poisoned variant also ADDS bars
    rng = np.random.default_rng(4)
    panel = {
        n: {d: b for d, b in bars.items() if d <= D.isoformat() or rng.random() > 0.2}
        for n, bars in panel.items()
    }
    s = cal.sessions()
    i = s.index(D)
    iv = {
        "AAA": {
            d.isoformat(): 0.25 + 0.03 * math.sin(k / 9.0)
            for k, d in enumerate(s[i - 199 : i + 30])
        }
    }
    # the chain's unadjusted close is its own datum (the split-adjusted
    # panel's level is never read); 100 keeps the 70..130 strikes near the money
    spot = SPOT
    return Inputs(
        panel=panel,
        sealed=copy.deepcopy(SEALED),
        timing=copy.deepcopy(TIMING),
        iv=iv,
        dtb3=[(d.isoformat(), f"{4.2 + 0.01 * k:.2f}") for k, d in enumerate(s[i - 5 : i + 8])],
        vix=[(d.isoformat(), f"{18 + k:.2f}") for k, d in enumerate(s[i - 5 : i + 8])],
        chains=[
            (_chain(spot, D, "2025-03-13T03:49:00+00:00"), False),
            (_chain(spot * 1.03, D, "2025-03-13T16:00:00+00:00"), True),
            (_chain(spot * 0.97, s[i + 1], "2025-03-14T03:49:00+00:00"), False),
        ],
    )


def _materialize(inp: Inputs, store: Path) -> pit.Sources:
    if store.exists():
        shutil.rmtree(store)
    for doc, conflict in inp.chains:
        fx.write_chain(store, doc, conflict=conflict)
    fx.write_index(store, "DTB3", inp.dtb3)
    fx.write_index(store, "VIX", inp.vix)
    return pit.Sources(
        panel=inp.panel,
        sealed=inp.sealed,
        timing=inp.timing,
        iv_history=fx.iv_history_doc(inp.iv),
        store=store,
    )


def _run(inp: Inputs, store: Path, cal: StaticSessionCalendar) -> bytes:
    p = pit.PointInTime(D, cal, _materialize(inp, store), har_names=NAMES)
    results = pricing.value_candidates(
        p, "AAA", STRUCTS, EXIT, row_id=ROW, signal=SIGNAL, n_paths=N
    )
    paths = dist.build_paths(p, "AAA", EXIT, row_id=ROW, signal=SIGNAL, n_paths=N)
    earn = p.earnings("AAA")
    blob = {
        "results": [r.as_dict() for r in results],
        "paths_sha256": hashlib.sha256(paths.log_paths.tobytes()).hexdigest(),
        "har": {n: dataclasses.asdict(h) for n, h in sorted(p.har(20).items())},
        "earnings": [
            dataclasses.asdict(e) for e in (*earn.past, *earn.past_estimated, *earn.upcoming)
        ],
        "iv": sorted(p.iv_history("AAA").items()),
        "rate": p.rate(),
        "vix": sorted(p.index("VIX").items()),
        "bars": p.bars("AAA"),
    }
    return json.dumps(blob, sort_keys=True, default=str).encode()


def _poison(inp: Inputs, cal: StaticSessionCalendar) -> Inputs:
    """Everything after D's cutoff, altered."""
    out = copy.deepcopy(inp)
    cut = D.isoformat()
    full = fx.synthetic_panel(cal, NAMES, date(2023, 1, 3), date(2025, 6, 30), seed=3)
    rng = np.random.default_rng(99)
    for name, bars in full.items():
        for d, bar in bars.items():
            if d > cut:
                f = float(np.exp(rng.normal() * 0.3))
                out.panel[name][d] = {
                    k: (f"{float(v) * f:.4f}" if k != "volume" else v) for k, v in bar.items()
                }
    out.sealed["AAA"] = [d for d in out.sealed["AAA"] if d <= cut] + ["2025-03-20", "2025-04-02"]
    out.timing["AAA"]["2025-03-25"] = _t("bmo", "estimated", "2025-03-13T09:31:00-04:00")
    out.timing["AAA"]["2025-04-01"] = _t("amc", "confirmed", "2025-04-01T17:00:00-04:00")
    out.timing["AAA"]["2025-07-24"] = _t("bmo", "estimated", "2025-06-02T21:00:00-04:00")
    out.timing["SPY"] = {"2025-03-20": _t("bmo", "estimated", "2025-03-14T21:00:00-04:00")}
    out.iv["AAA"] = {d: (v if d <= cut else 0.9) for d, v in out.iv["AAA"].items()}
    out.iv["AAA"]["2025-04-30"] = None
    s = cal.sessions()
    prev = s[s.index(D) - 1].isoformat()
    out.dtb3 = [(d, v if d <= prev else "9.99") for d, v in out.dtb3]  # D's rate posts D+1
    out.vix = [(d, v if d <= cut else "80.00") for d, v in out.vix] + [("2025-03-31", "90.00")]
    spot = SPOT
    out.chains = [
        (inp.chains[0][0], False),  # D's canonical record stays
        (_chain(spot * 1.2, D, "2025-03-13T18:00:00+00:00"), True),  # a later conflict
        (_chain(spot * 0.5, s[s.index(D) + 1], "2025-03-14T03:49:00+00:00"), False),
        (_chain(spot * 2.0, s[s.index(D) + 2], "2025-03-15T03:49:00+00:00"), False),
    ]
    return out


def test_future_poison_leaves_the_output_byte_identical(
    cal: StaticSessionCalendar, tmp_path: Path
) -> None:
    base = _base(cal)
    a = _run(base, tmp_path / "a", cal)
    b = _run(_poison(base, cal), tmp_path / "b", cal)
    assert a == b
    doc = json.loads(a)
    # the run is not vacuous: priced candidates, a refusal, events in the path
    assert [r["structure_id"] for r in doc["results"]] == ["cv", "ic", "cal", "bad"]
    assert [r["valuation"] is not None for r in doc["results"]] == [True, True, True, False]
    assert "quote" in doc["results"][3]["reason"]
    cv = doc["results"][0]["valuation"]
    assert cv["iv_mean_reversion"]["status"] == "applied"
    assert cv["ev_signal"] is not None
    assert [e["report"] for e in cv["events"]] == ["2025-03-19", "2025-04-10"]


def _edit_d_bar(inp: Inputs, cal: StaticSessionCalendar) -> None:
    bar = inp.panel["AAA"][D.isoformat()]
    bar["close"] = f"{float(bar['close']) * 1.01:.4f}"


def _edit_rate(inp: Inputs, cal: StaticSessionCalendar) -> None:
    s = cal.sessions()
    prev = s[s.index(D) - 1].isoformat()
    inp.dtb3 = [(d, "5.50" if d == prev else v) for d, v in inp.dtb3]


def _edit_chain(inp: Inputs, cal: StaticSessionCalendar) -> None:
    doc = inp.chains[0][0]
    doc["columns"]["ask"] = [a * 1.05 if a is not None else None for a in doc["columns"]["ask"]]


def _edit_known_estimate(inp: Inputs, cal: StaticSessionCalendar) -> None:
    inp.timing["AAA"]["2025-03-27"] = _t("unknown", "estimated", "2025-03-13T09:29:00-04:00")


def _edit_iv_at_d(inp: Inputs, cal: StaticSessionCalendar) -> None:
    inp.iv["AAA"][D.isoformat()] = 0.40


@pytest.mark.parametrize(
    "edit", [_edit_d_bar, _edit_rate, _edit_chain, _edit_known_estimate, _edit_iv_at_d]
)
def test_positive_controls_change_the_output(
    cal: StaticSessionCalendar,
    tmp_path: Path,
    edit: Callable[[Inputs, StaticSessionCalendar], None],
) -> None:
    base = _base(cal)
    a = _run(base, tmp_path / "a", cal)
    changed = copy.deepcopy(base)
    edit(changed, cal)
    assert _run(changed, tmp_path / "b", cal) != a


def test_candidates_fit_in_memory_at_the_production_path_count(
    cal: StaticSessionCalendar, tmp_path: Path
) -> None:
    p = pit.PointInTime(D, cal, _materialize(_base(cal), tmp_path / "s"), har_names=NAMES)
    p.har(20)  # the pooled fit is shared by every name; warm it outside the trace
    structs = []
    for lo in range(80, 125, 5):
        for w in (5, 10, 15):
            if lo + w <= 130:
                legs = [("C", "BUY", float(lo), EXP1), ("C", "SELL", float(lo + w), EXP1)]
                structs.append(_struct(f"c{lo}-{w}", "debit_vertical", legs, str(w - 0.5)))
    assert len(structs) >= 25
    tracemalloc.start()
    try:
        results = pricing.value_candidates(p, "AAA", structs, EXIT, row_id=ROW)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert all(r.valuation is not None for r in results), [r.reason for r in results]
    assert all(r.valuation.n_paths == 20_000 for r in results if r.valuation)
    assert peak < 200 * 2**20, f"peak {peak / 2**20:.0f} MiB"


def test_candidates_refuse_all_when_the_name_cannot_be_modeled(
    cal: StaticSessionCalendar, tmp_path: Path
) -> None:
    inp = _base(cal)
    inp.chains = []
    p = pit.PointInTime(D, cal, _materialize(inp, tmp_path / "s"), har_names=NAMES)
    results = pricing.value_candidates(p, "AAA", STRUCTS, EXIT, row_id=ROW, n_paths=200)
    assert [r.valuation for r in results] == [None] * len(STRUCTS)
    assert all("chain" in r.reason for r in results)
