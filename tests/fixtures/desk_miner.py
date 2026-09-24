"""A synthetic desk world for the deal-miner tests (plan D6).

One decision session D = 2025-03-12 (entry 2025-03-13, decision cutoff
2025-03-13 09:30 ET) with everything the miner reads: a research panel, the
sealed earnings calendar, an earnings-timing vintage for D, recorded chains
for AAPL, SPY and QQQ, DTB3/VIX/VIX3M index files, desk features documents
(D and a short history: the vol state is in warm-up), the signals file of
D (XSMOM fires: AAPL, TQQQ, QQQ), dividend snapshots, and a legacy trex book
holding one SPY put spread. Every number is invented; the builders only
exercise the code. :func:`materialize` writes a :class:`World` to disk and
:func:`env` pins every desk path to it.

:func:`poison` alters EVERYTHING dated after D (or published after its
cutoff) and nothing a decision for D could know.
"""

from __future__ import annotations

import copy
import json
import math
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np

from tests.fixtures import desk_pricing as fx
from tree_options.time.calendar import StaticSessionCalendar

ET = ZoneInfo("America/New_York")
REPO = Path(__file__).resolve().parents[2]
D = date(2025, 3, 12)
ENTRY = date(2025, 3, 13)
NOW = datetime(2025, 3, 13, 7, 15, tzinfo=ET)  # the timer slot, before the cutoff
NAMES = ("AAPL", "SPY", "QQQ")
# AAPL at 100 on $1 strikes: its wider R1 spreads risk more than the $500
# per-trade rail allows; QQQ's narrow ones fit
SPOT = {"AAPL": 100.0, "SPY": 60.0, "QQQ": 45.0}
# expiries from the entry session: 127, 155 and 190 calendar days (R1 takes
# 120-180 DTE, so the third is out) plus a 71-day one (R7's 60-120)
EXPIRIES = (date(2025, 5, 23), date(2025, 7, 18), date(2025, 8, 15), date(2025, 9, 19))
RATE_PCT = 4.20


def _t(timing: str, status: str, fetched_at: str) -> dict[str, str]:
    return {"timing": timing, "status": status, "fetched_at": fetched_at, "source": "test"}


SEALED = {
    "AAPL": [
        "2023-05-04", "2023-08-03", "2023-11-02", "2024-02-01", "2024-05-02",
        "2024-08-01", "2024-10-31", "2025-01-30",
    ],
}  # fmt: skip
TIMING = {
    "AAPL": {
        "2025-01-30": _t("amc", "confirmed", "2025-02-01T12:00:00-05:00"),
        # the next report, known at D: after every R1 exit (hold 20 sessions)
        "2025-05-01": _t("amc", "estimated", "2025-03-01T21:00:00-05:00"),
        "2025-07-31": _t("amc", "estimated", "2025-03-01T21:00:00-05:00"),
    }
}


def smile(m: float) -> float:
    return 0.22 - 0.10 * m + 0.20 * m * m


def half_spread(px: float) -> float:
    return 0.01 + 0.004 * px


@dataclass
class World:
    panel: dict[str, Any]
    sealed: dict[str, list[str]]
    timing_live: dict[str, Any]  # the market lane's file as it stands NOW
    vintages: dict[str, tuple[str, dict[str, Any]]]  # session -> (snapshot_at, timing)
    chains: list[tuple[dict[str, Any], bool]]  # (doc, conflict)
    dtb3: list[tuple[str, str]]
    vix: list[tuple[str, str]]
    vix3m: list[tuple[str, str]]
    features: dict[str, dict[str, Any]]  # session -> desk-features/1 document
    signals: dict[str, dict[str, Any]]  # session -> desk-signals/1 document
    dividends: dict[str, dict[str, list[dict[str, Any]]]]  # session -> sym -> records


def calendar() -> StaticSessionCalendar:
    return fx.trex_calendar()


def chain(
    sym: str,
    session: date,
    as_of: str,
    *,
    scale: float = 1.0,
    expiries: Sequence[date] = EXPIRIES,
) -> dict[str, Any]:
    spot = SPOT[sym] * scale
    strikes = [float(k) for k in range(int(spot * 0.6), int(spot * 1.45) + 1)]
    return fx.chain_doc(
        sym=sym,
        session=session,
        spot=spot,
        rate=RATE_PCT / 100.0,
        expiries=expiries,
        strikes=strikes,
        iv=smile,
        half_spread=half_spread,
        source_as_of=as_of,
    )


def _features_entry(iv30: float, liquidity: int = 50) -> dict[str, Any]:
    return {
        "iv": {"30": iv30, "60": iv30, "90": iv30 * 1.02, "180": None},
        "term_slope": 0.02,
        "atm_term": [],
        "liquidity_score": liquidity,
        "forecast": {
            "source": "har",
            "har_status": "validated",
            "har_vol": 0.25,
            "h": 20,
            "fit_through": "2025-01-31",
        },
    }


def features_doc(session: date, *, iv30: float = 0.21) -> dict[str, Any]:
    return {
        "schema": "desk-features/1",
        "session": session.isoformat(),
        "inputs": {"chains_raw_sha256": {n: "ab" * 32 for n in NAMES}},
        "names": {n: _features_entry(iv30) for n in NAMES},
    }


def signals_doc(session: date, top3: list[str], pead: Sequence[str] = ()) -> dict[str, Any]:
    return {
        "schema": "desk-signals/1",
        "session": session.isoformat(),
        "xsmom": {"fires": True, "top3": top3},
        "pead": [{"name": n} for n in pead],
    }


def _dividend_records(sym: str) -> list[dict[str, Any]]:
    if sym == "AAPL":  # quarterly: the next ex-date projects to 2025-05-09 +/- 7d
        return [
            {
                "declaration_date": "2025-01-30",
                "ex_dividend_date": "2025-02-07",
                "pay_date": "2025-02-13",
                "record_date": "2025-02-07",
                "cash_amount": "0.25",
                "currency": "USD",
                "dividend_type": "CD",
                "frequency": 4,
            }
        ]
    return []  # a known non-payer (the ETFs here)


def base(cal: StaticSessionCalendar) -> World:
    s = cal.sessions()
    i = s.index(D)
    panel = fx.synthetic_panel(cal, NAMES, date(2023, 1, 3), date(2025, 6, 30), seed=11)
    # drop a fifth of the post-D bars so the poisoned world also ADDS bars
    rng = np.random.default_rng(12)
    panel = {
        n: {d: b for d, b in bars.items() if d <= D.isoformat() or rng.random() > 0.2}
        for n, bars in panel.items()
    }
    as_of = "2025-03-13T03:49:00+00:00"  # 23:49 ET on D: the overnight snapshot
    history = {
        d.isoformat(): features_doc(d, iv30=0.20 + 0.002 * k) for k, d in enumerate(s[i - 6 : i])
    }
    history[D.isoformat()] = features_doc(D)
    return World(
        panel=panel,
        sealed=copy.deepcopy(SEALED),
        timing_live=copy.deepcopy(TIMING),
        vintages={D.isoformat(): ("2025-03-13T07:10:00-04:00", copy.deepcopy(TIMING))},
        chains=[(chain(n, D, as_of), False) for n in NAMES],
        dtb3=[
            (d.isoformat(), f"{RATE_PCT + 0.01 * k:.2f}") for k, d in enumerate(s[i - 5 : i + 8])
        ],
        vix=[(d.isoformat(), f"{18 + 0.1 * k:.2f}") for k, d in enumerate(s[i - 5 : i + 8])],
        vix3m=[(d.isoformat(), f"{19 + 0.1 * k:.2f}") for k, d in enumerate(s[i - 5 : i + 8])],
        features=history,
        # XSMOM picks AAPL, TQQQ (no options expression: logged) and QQQ; a
        # (synthetic) PEAD beat points QQQ too, so R3 competes with R1
        signals={D.isoformat(): signals_doc(D, ["AAPL", "TQQQ", "QQQ"], ["QQQ"])},
        dividends={D.isoformat(): {n: _dividend_records(n) for n in NAMES}},
    )


def poison(w: World, cal: StaticSessionCalendar) -> World:
    """Everything dated after D (or published after its cutoff), altered."""
    out = copy.deepcopy(w)
    cut = D.isoformat()
    s = cal.sessions()
    i = s.index(D)
    full = fx.synthetic_panel(cal, NAMES, date(2023, 1, 3), date(2025, 6, 30), seed=11)
    rng = np.random.default_rng(99)
    for name, bars in full.items():
        for d, bar in bars.items():
            if d > cut:
                f = float(np.exp(rng.normal() * 0.3))
                out.panel[name][d] = {
                    k: (f"{float(v) * f:.4f}" if k != "volume" else v) for k, v in bar.items()
                }
    # the sealed calendar's future dates (no vintage: not knowable at D)
    out.sealed["AAPL"] = [d for d in out.sealed["AAPL"] if d <= cut] + ["2025-03-20", "2025-04-02"]
    # the live timing file moved on after the cutoff: never read for D
    out.timing_live["AAPL"]["2025-03-25"] = _t("bmo", "estimated", "2025-03-14T21:00:00-04:00")
    out.timing_live["AAPL"].pop("2025-05-01")
    # later vintages know more (and less)
    later = {"AAPL": {"2025-03-18": _t("bmo", "estimated", "2025-03-14T20:00:00-04:00")}}
    out.vintages[s[i + 1].isoformat()] = ("2025-03-14T07:10:00-04:00", later)
    out.vintages[s[i + 5].isoformat()] = ("2025-03-20T07:10:00-04:00", {})
    # a later conflict for D and later sessions' chains
    out.chains = [
        *[(doc, False) for doc, conflict in w.chains if not conflict],
        (chain("AAPL", D, "2025-03-13T18:00:00+00:00", scale=1.2), True),
        (chain("AAPL", s[i + 1], "2025-03-14T03:49:00+00:00", scale=0.5), False),
        (chain("SPY", s[i + 2], "2025-03-15T03:49:00+00:00", scale=2.0), False),
    ]
    prev = s[i - 1].isoformat()  # D's DTB3 posts on D+1, after the cutoff
    out.dtb3 = [(d, v if d <= prev else "9.99") for d, v in w.dtb3]
    out.vix = [(d, v if d <= cut else "80.00") for d, v in w.vix]
    out.vix3m = [(d, v if d <= cut else "10.00") for d, v in w.vix3m] + [("2025-03-31", "11.0")]
    for k in (1, 2, 3):
        d = s[i + k]
        out.features[d.isoformat()] = features_doc(d, iv30=0.9)
        out.signals[d.isoformat()] = signals_doc(d, ["SPY", "QQQ", "AAPL"])
        out.dividends[d.isoformat()] = {n: [] for n in NAMES}
    return out


def materialize(w: World, root: Path) -> dict[str, Path]:
    """Write the world under ``root`` (replacing it); the desk paths."""
    if root.exists():
        shutil.rmtree(root)
    store, state, paper = root / "store", root / "state", root / "paper"
    for p in (store, state, paper):
        p.mkdir(parents=True)
    (paper / "ohlc-panel.json").write_text(json.dumps(w.panel, sort_keys=True))
    (paper / "earnings-calendar.json").write_text(json.dumps(w.sealed, sort_keys=True))
    (paper / "earnings-timing.json").write_text(json.dumps(w.timing_live, indent=1, sort_keys=True))
    for sess, (at, timing) in w.vintages.items():
        path = store / "earnings-timing" / f"{sess}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "schema": "desk-earnings-timing-vintage/1",
                    "session": sess,
                    "snapshot_at": at,
                    "source": "earnings-timing.json",
                    "source_sha256": "cd" * 32,
                    "timing": timing,
                },
                indent=1,
                sort_keys=True,
            )
        )
    for doc, conflict in w.chains:
        fx.write_chain(store, doc, conflict=conflict)
    fx.write_index(store, "DTB3", w.dtb3)
    fx.write_index(store, "VIX", w.vix)
    fx.write_index(store, "VIX3M", w.vix3m)
    for sess, doc in w.features.items():
        p = store / "features" / f"{sess}.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(doc, indent=1, sort_keys=True))
    for sess, doc in w.signals.items():
        p = state / "signals" / f"{sess}.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(doc, indent=1, sort_keys=True))
    for sess, per_sym in w.dividends.items():
        for sym, records in per_sym.items():
            p = store / "dividends" / sess / f"{sym}.json"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(
                json.dumps(
                    {
                        "schema": "desk.dividends/1",
                        "symbol": sym,
                        "session": sess,
                        "fetched_at": f"{sess}T17:20:00-04:00",
                        "source": "polygon /v3/reference/dividends",
                        "since": "2023-03-01",
                        "request_ids": [],
                        "records": records,
                    }
                )
            )
    # the legacy trex book: one SPY put debit spread, open 1 @ 1.10
    repo = root / "repo"
    (repo / "plans").mkdir(parents=True)
    (repo / "plans" / "spy.toml").write_text(
        'id = "spy-hedge"\naccount_mode = "paper"\ntotal_debit_cap = 500.00\n'
        'entry_window_start = "09:45"\nentry_window_end = "12:00"\n\n'
        '[[structures]]\nid = "spy-may"\nunderlying = "SPY"\nentry_date = 2025-03-03\n'
        "expiry = 2025-05-23\nlong_strike = 58.0\nshort_strike = 52.0\nquantity = 1\n"
        "limit_cap = 1.50\nexit_deadline = 2025-05-09\n"
    )
    trex_state = root / "trex-state"
    (trex_state / "spy-hedge").mkdir(parents=True)
    (trex_state / "spy-hedge" / "book.json").write_text(
        json.dumps(
            {
                "heartbeat": None,
                "structures": {
                    "spy-may": {
                        "status": "open",
                        "filled_qty": 1,
                        "exit_filled_qty": 0,
                        "entry_fill": "1.10",
                    }
                },
            }
        )
    )
    return {
        "store": store,
        "state": state,
        "paper": paper,
        "repo": repo,
        "trex_state": trex_state,
        "queue": state / "queue",
    }


def env(monkeypatch: Any, where: dict[str, Path]) -> None:
    """Pin every path the miner resolves to the materialized world."""
    monkeypatch.setenv("DESK_STORE", str(where["store"]))
    monkeypatch.setenv("TREX_DESK_STATE", str(where["state"]))
    monkeypatch.setenv("DESK_PAPER_DIR", str(where["paper"]))
    monkeypatch.setenv("DESK_REPO_ROOT", str(where["repo"]))
    monkeypatch.setenv("TREX_STATE", str(where["trex_state"]))
    monkeypatch.setenv("TREX_DESK_QUEUE", str(where["queue"]))
    monkeypatch.setenv("DESK_EVENTS_DIR", str(REPO / "data" / "desk" / "events"))
    monkeypatch.setenv("DESK_PLAYBOOK_DIR", str(REPO / "data" / "desk" / "playbook"))
    monkeypatch.setenv("DESK_MINER_DIR", str(REPO / "data" / "desk" / "miner"))
    monkeypatch.setenv("TREX_NOTIFY_ENV", str(where["state"] / "no-notify.env"))


def spot_of(sym: str) -> float:
    return SPOT[sym]


def log_moneyness(k: float, sym: str) -> float:
    return math.log(k / SPOT[sym])
