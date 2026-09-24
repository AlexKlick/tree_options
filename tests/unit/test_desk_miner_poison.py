"""Desk D6 look-ahead guard on the WHOLE queue (the future-poison test).

The miner runs for D on a synthetic world (``tests/fixtures/desk_miner.py``)
and writes its queue; the same run on a world where EVERYTHING dated after
D (or published after D's cutoff) is altered must write a BYTE-IDENTICAL
queue: chains (a later conflict for D, later sessions' chains), indices
(VIX/VIX3M after D, DTB3 from D on: FRED posts D's rate after the cutoff),
panel bars after D (altered and added), the earnings-timing vintages after
D, the live timing file (rewritten after the cutoff), the sealed calendar's
future dates, features and signals documents after D, and dividend
snapshots after D. The sealed macro calendar is a schedule sealed in
advance (its events after D are knowable at D) and the book is not dated;
neither is poisoned.

Positive controls alter one datum D COULD know and require the bytes to
change, so the guard is not vacuous; the base queue itself must hold
admissible, refused, rail-failed and capacity deals.
"""

from __future__ import annotations

import copy
import dataclasses
import json
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tests.fixtures import desk_miner as wm
from tree_options.desk import miner, selection
from tree_options.time.calendar import StaticSessionCalendar

REPO = Path(__file__).resolve().parents[2]
QUEUE = f"{wm.D.isoformat()}.json"


@pytest.fixture(scope="module")
def cal() -> StaticSessionCalendar:
    return wm.calendar()


@pytest.fixture(scope="module")
def cfg() -> selection.MinerConfig:
    """1000 paths, open thresholds (every rail-passing deal is eligible)
    and a cap of 12 valued structures per (name, row), so the queue holds
    every kind of outcome; the sealed thresholds are tested elsewhere."""
    base = selection.load_config(REPO / "data" / "desk" / "miner" / "v1.toml")
    return dataclasses.replace(
        base,
        n_paths=1000,
        max_valued_per_name_row=12,
        selection=selection.Selection(Decimal("-100000"), Decimal("-100")),
    )


def _queue(
    w: wm.World, root: Path, cal: StaticSessionCalendar, cfg: selection.MinerConfig
) -> bytes:
    mp = pytest.MonkeyPatch()
    try:
        where = wm.materialize(w, root)
        wm.env(mp, where)
        res = miner.run_mine(session=wm.D, now=wm.NOW, cal=cal, config=cfg)
        assert (res.exit_code, res.status) == (0, "written"), res.line()
        return (where["queue"] / QUEUE).read_bytes()
    finally:
        mp.undo()


@pytest.fixture(scope="module")
def base_bytes(cal, cfg, tmp_path_factory) -> bytes:
    return _queue(wm.base(cal), tmp_path_factory.mktemp("base") / "w", cal, cfg)


def test_future_poison_leaves_the_queue_byte_identical(cal, cfg, base_bytes, tmp_path) -> None:
    poisoned = _queue(wm.poison(wm.base(cal), cal), tmp_path / "w", cal, cfg)
    assert poisoned == base_bytes


def test_the_guarded_queue_is_not_vacuous(base_bytes) -> None:
    q = json.loads(base_bytes)
    statuses = [d["status"] for d in q["surfaced"]]
    assert q["admissible"]
    for s in ("refused", "rail_failed", "capacity"):
        assert s in statuses, s
    assert any(r.startswith("enumeration_cap") for d in q["surfaced"] for r in d["reasons"])
    assert {d["row"] for d in q["admissible"] + q["surfaced"]} >= {"R1", "R3"}
    assert q["no_options_expression"]["names"] == ["TQQQ"]
    assert q["inputs"]["timing_vintage"]["session"] == wm.D.isoformat()
    assert q["inputs"]["book"]["positions"][0]["delta_shares"] is not None
    # valued deals carry the stress, the view and the path seed they used
    v = q["admissible"][0]["valuation"]
    assert v["ev_signal"] is not None and v["ev_signal_fill_stress"] is not None
    assert v["assumptions"]["paths"]["seed"]


def _chain_ask(w: wm.World, cal: StaticSessionCalendar) -> None:
    doc = next(d for d, c in w.chains if d["header"]["underlying"] == "AAPL" and not c)
    doc["columns"]["ask"] = [a * 1.05 if a is not None else None for a in doc["columns"]["ask"]]


def _known_estimate(w: wm.World, cal: StaticSessionCalendar) -> None:
    at, timing = w.vintages[wm.D.isoformat()]
    timing = copy.deepcopy(timing)
    timing["AAPL"]["2025-03-27"] = wm._t("bmo", "estimated", "2025-03-01T21:00:00-05:00")
    w.vintages[wm.D.isoformat()] = (at, timing)


def _signals(w: wm.World, cal: StaticSessionCalendar) -> None:
    w.signals[wm.D.isoformat()] = wm.signals_doc(wm.D, ["AAPL", "TQQQ", "MSFT"], ["QQQ"])


def _rate(w: wm.World, cal: StaticSessionCalendar) -> None:
    s = cal.sessions()
    prev = s[s.index(wm.D) - 1].isoformat()
    w.dtb3 = [(d, "5.50" if d == prev else v) for d, v in w.dtb3]


def _bar(w: wm.World, cal: StaticSessionCalendar) -> None:
    bar = w.panel["AAPL"][wm.D.isoformat()]
    bar["close"] = f"{float(bar['close']) * 1.01:.4f}"


def _dividend(w: wm.World, cal: StaticSessionCalendar) -> None:
    rec = dict(w.dividends[wm.D.isoformat()]["AAPL"][0])
    rec.update(declaration_date="2025-03-05", ex_dividend_date="2025-03-20", cash_amount="5.00",
               dividend_type="SC", frequency=0)  # fmt: skip
    w.dividends[wm.D.isoformat()]["AAPL"].append(rec)


def _liquidity(w: wm.World, cal: StaticSessionCalendar) -> None:
    w.features[wm.D.isoformat()]["names"]["QQQ"]["liquidity_score"] = 3


@pytest.mark.parametrize(
    "edit", [_chain_ask, _known_estimate, _signals, _rate, _bar, _dividend, _liquidity]
)
def test_positive_controls_change_the_queue(
    cal, cfg, base_bytes, tmp_path, edit: Callable[[wm.World, StaticSessionCalendar], Any]
) -> None:
    w = wm.base(cal)
    edit(w, cal)
    assert _queue(w, tmp_path / "w", cal, cfg) != base_bytes
