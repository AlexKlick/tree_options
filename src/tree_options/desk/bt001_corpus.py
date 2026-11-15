"""The DESK-BT-001 synthetic corpus (dry-run machinery proof; no real data).

A tiny deterministic world that exercises the sealed runner end-to-end:
signal enumeration through the REAL ``desk.signals`` functions (a weekday
calendar, the sealed 36 tradables with linear-growth closes, two PEAD
beaters), entry anchoring at E0, structure selection on synthetic bars,
the exit walk, the measured haircut from synthetic chain files on disk
(including one sparse name on the pooled fallback), the sha256 placebo
draw, the six variant cells with T >= 20 and k >= 10, the equity card,
and the one-shot output refusal.

It reads NO real artifact: everything is generated under a caller-supplied
root. Only the sealed documents are read (the worktree's bytes, by
``run``'s seal verification).

Calendar-day steps are epoch arithmetic (the repo bans naive date
arithmetic outside ``time/``); 1970-01-01 was a Thursday, so
``(epoch_day + 3) % 7`` is the Monday-first weekday index without calling
any banned helper.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import math
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from tree_options.desk import bt001, ivhist
from tree_options.desk.bt001 import RateSource
from tree_options.desk.sessions import first_session_after, is_first_session_of_month
from tree_options.desk.universe import XSMOM_TRADABLES

_MIDNIGHT = datetime.min.time()
_DAY_S = 86_400


def _epoch_day(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp() // _DAY_S)


def _from_epoch_day(n: int) -> date:
    return datetime.fromtimestamp(n * _DAY_S, tz=UTC).date()


def day_offset(d: date, days: int) -> date:
    """``d`` plus/minus whole calendar days, epoch arithmetic."""
    return _from_epoch_day(_epoch_day(d) + days)


def is_weekday(d: date) -> bool:
    return (_epoch_day(d) + 3) % 7 < 5  # 1970-01-01 was a Thursday


class WeekdayCalendar:
    """A minimal Calendar over fixed sessions (the protocol's four methods)."""

    def __init__(self, sessions: list[date]) -> None:
        self._sessions = tuple(sessions)
        self._index = {d: i for i, d in enumerate(self._sessions)}

    def sessions(self) -> tuple[date, ...]:
        return self._sessions

    def is_session(self, d: date) -> bool:
        return d in self._index

    def ordinal(self, d: date) -> int:
        return self._index[d]

    def nth_after(self, d: date, n: int) -> date:
        return self._sessions[self._index[d] + n]


def weekdays(start: date, end: date) -> list[date]:
    out = []
    cursor = start
    while cursor <= end:
        if is_weekday(cursor):
            out.append(cursor)
        cursor = day_offset(cursor, 1)
    return out


def third_friday(year: int, month: int) -> date:
    """The month's third Friday (epoch weekday index, no banned calls)."""
    for day in range(15, 22):
        d = date(year, month, day)
        if (_epoch_day(d) + 3) % 7 == 4:
            return d
    raise AssertionError("unreachable: every month has a Friday in 15..21")


@dataclass
class SyntheticWorld:
    root: Path
    store: Path
    paper_dir: Path
    panel: dict[str, Any]
    panel_sha256: str
    earnings: dict[str, list[str]]
    earnings_sha256: str
    cal: WeekdayCalendar
    scan: ivhist.CacheScan
    rates: RateSource
    store_pins: bt001.StorePins
    slice_pins: bt001.SlicePins
    bar_names: tuple[str, ...]
    expected: dict[str, Any]

    def inputs(
        self, *, docs: Path, repo: Path, code_commit: str | None, now: datetime
    ) -> bt001.Inputs:
        return bt001.Inputs(
            panel=self.panel,
            panel_sha256=self.panel_sha256,
            earnings=self.earnings,
            earnings_sha256=self.earnings_sha256,
            dtb3_path=self.store / "indices" / "DTB3.csv",
            cal=self.cal,
            calendar_path=None,
            rates=self.rates,
            scan=self.scan,
            store=self.store,
            docs=docs,
            repo=repo,
            code_commit=code_commit,
            now=now,
            window=(self.expected["window_first"], self.expected["window_last"]),
        )


STRIKES: tuple[float, ...] = (85.0, 90.0, 95.0, 100.0, 105.0, 110.0, 115.0)
_CALL_PRICES = {85.0: 20.0, 90.0: 15.5, 95.0: 10.5, 100.0: 5.5, 105.0: 2.5, 110.0: 1.2, 115.0: 0.6}
_PUT_PRICES = {85.0: 0.5, 90.0: 1.1, 95.0: 2.6, 100.0: 5.6, 105.0: 10.6, 110.0: 15.6, 115.0: 20.5}
HAIRCUT_HALFSPREAD = 0.02  # (5.1 - 4.9) / (5.1 + 4.9)
CHAIN_BID = 4.9
CHAIN_ASK = 5.1


def synthetic_world(root: Path) -> SyntheticWorld:
    """Build the corpus under ``root`` (never a real artifacts tree)."""
    cal = WeekdayCalendar(weekdays(date(2023, 5, 1), date(2025, 7, 31)))
    sessions = cal.sessions()
    tradables = sorted(XSMOM_TRADABLES)
    with_bars = [n for n in tradables if n in bt001.BAR_NAMES]
    top3 = tuple(with_bars[:3])
    beaters = tuple(with_bars[6:8])
    extra = with_bars[11]
    bar_names = (*top3, *beaters, extra)
    growth = {n: 0.00040 - 0.00001 * i for i, n in enumerate(tradables)}

    win_first, win_last = date(2024, 6, 1), date(2025, 3, 31)
    by_month: dict[tuple[int, int], list[date]] = {}
    for s in sessions:
        if win_first <= s <= win_last:
            by_month.setdefault((s.year, s.month), []).append(s)
    jumps: dict[str, set[date]] = {b: set() for b in beaters}
    for _ym, month_sessions in sorted(by_month.items()):
        for slot, beater in enumerate(beaters):
            # a beat whose E0 has a monthly inside the 30..60 DTE band (the
            # band has real holes where third Fridays sit 35 days apart)
            for s in month_sessions[6 + 4 * slot :]:
                e0 = first_session_after(s, cal)
                if e0 is not None and _banded_monthly(e0, cal) is not None:
                    jumps[beater].add(s)
                    break
    earnings = {b: sorted(day_offset(s, -1).isoformat() for s in ss) for b, ss in jumps.items()}

    idx = {s: i for i, s in enumerate(sessions)}
    panel: dict[str, dict[str, Any]] = {}
    for name in tradables:
        rows: dict[str, Any] = {}
        for s in sessions:
            close = 100.0 * (1.0 + growth[name] * idx[s])
            if name in jumps and s in jumps[name]:
                close *= 1.02  # a one-session spike: the beat fires, then it reverts
            rows[s.isoformat()] = {
                "open": round(close * 0.999, 6),
                "high": round(close * 1.001, 6),
                "low": round(close * 0.998, 6),
                "close": round(close, 6),
                "volume": 1_000_000,
            }
        panel[name] = rows

    fom = [s for s in sessions if win_first <= s <= win_last and is_first_session_of_month(s, cal)]
    beat_sessions = sorted(s for ss in jumps.values() for s in ss)
    e0s = sorted(
        {
            e
            for e in (first_session_after(s, cal) for s in [*fom, *beat_sessions])
            if e is not None
        }
    )

    options: dict[str, dict[date, list[ivhist.OptionBar]]] = {n: {} for n in bar_names}
    spot: dict[str, dict[date, float]] = {n: {} for n in bar_names}
    for e0 in e0s:
        expiry = _banded_monthly(e0, cal)
        assert expiry is not None, f"no monthly with 30..60 DTE at {e0}"
        exit_day = bt001.exit_session(e0, expiry, cal)
        assert exit_day is not None
        for name in bar_names:
            # one session can be E0 of one entry and the exit of another:
            # bars of different expiries coexist there, as in the real cache
            options[name].setdefault(e0, []).extend(_bars(expiry, entry=True))
            options[name].setdefault(exit_day, []).extend(_bars(expiry, entry=False))
            spot[name][e0] = 100.0
    scan = ivhist.CacheScan(source="synthetic://bt001-corpus", options=options, spot=spot)
    scan.input_digest = "synthetic-bt001-corpus-v1"
    scan.input_files = 1
    scan.stats["option_bars"] = sum(
        len(rows) for by_day in options.values() for rows in by_day.values()
    )

    store = root / "desk-store"
    (store / "chains").mkdir(parents=True)
    (store / "manifest").mkdir(parents=True)
    (store / "indices").mkdir(parents=True)
    chain_days = [s for s in sessions if s > win_last][:20]
    for s in chain_days:
        for name in bar_names:
            _write_chain(store, s, name, sparse=(name == extra))
        _write_manifest(store, s, bar_names)
    (store / "indices" / "DTB3.csv").write_text(
        "date,open,high,low,close\n2023-06-01,,,,4.0\n", encoding="utf-8"
    )
    rates = ivhist.rate_source_for_store(store)[0]

    paper = root / "paper-trades"
    paper.mkdir(parents=True)
    panel_bytes = json.dumps(panel, sort_keys=True).encode()
    (paper / "ohlc-panel.json").write_bytes(panel_bytes)
    earnings_bytes = (json.dumps(earnings, sort_keys=True) + "\n").encode()
    (paper / "earnings-calendar.json").write_bytes(earnings_bytes)

    slice_pins = bt001.SlicePins(
        panel_cutoff="2025-04-30",
        panel_sha256=bt001.panel_slice_sha256(panel, "2025-04-30"),
        dtb3_cutoff="2025-07-31",
        dtb3_sha256=bt001.dtb3_slice_sha256(store / "indices" / "DTB3.csv", "2025-07-31"),
        earnings_cutoff="2025-03-31",
        earnings_sha256=bt001.earnings_slice_sha256(earnings, "2025-03-31"),
    )
    store_pins = bt001.StorePins(
        first_twenty=tuple(chain_days),
        manifest_sha256=bt001.manifest_sha256(store, chain_days[-1]),
    )
    expected = {
        "window_first": win_first,
        "window_last": win_last,
        "top3": top3,
        "n_xsmom_entries": 3 * len(fom),
        "n_pead_entries": len(beat_sessions),
        "n_fom": len(fom),
        "pooled_names": [extra],
        "haircut": HAIRCUT_HALFSPREAD,
    }
    return SyntheticWorld(
        root=root,
        store=store,
        paper_dir=paper,
        panel=panel,
        panel_sha256=hashlib.sha256(panel_bytes).hexdigest(),
        earnings=earnings,
        earnings_sha256=hashlib.sha256(earnings_bytes).hexdigest(),
        cal=cal,
        scan=scan,
        rates=rates,
        store_pins=store_pins,
        slice_pins=slice_pins,
        bar_names=tuple(bar_names),
        expected=expected,
    )


def _banded_monthly(e0: date, cal: WeekdayCalendar) -> date | None:
    """The latest third-Friday expiry with 30..60 DTE at e0."""
    best: date | None = None
    for offset in (0, 1, 2, 3):
        y, m = e0.year, e0.month + offset
        y += (m - 1) // 12
        m = (m - 1) % 12 + 1
        tf = third_friday(y, m)
        if cal.is_session(tf) and bt001.MIN_ENTRY_DTE <= (tf - e0).days <= bt001.MAX_ENTRY_DTE:
            best = tf if best is None or tf > best else best
    return best


def _bars(expiry: date, *, entry: bool) -> list[ivhist.OptionBar]:
    call_scale = 1.0 if entry else 1.1
    put_scale = 2.0 - call_scale
    out = []
    for k in STRIKES:
        out.append(ivhist.OptionBar(expiry, "C", k, round(_CALL_PRICES[k] * call_scale, 4)))
        out.append(ivhist.OptionBar(expiry, "P", k, round(_PUT_PRICES[k] * put_scale, 4)))
    return out


def _nearby_third_fridays(session: date, horizon_days: int = 70) -> list[date]:
    """Every third Friday within ``horizon_days`` of ``session`` (real chains
    carry all nearby monthlies; the sealed haircut filter picks in-band)."""
    out = []
    y, m = session.year, session.month
    for _ in range(4):
        tf = third_friday(y, m)
        if 0 <= (tf - session).days <= horizon_days:
            out.append(tf)
        m += 1
        if m == 13:
            m, y = 1, y + 1
    return sorted(out)


def _write_chain(store: Path, session: date, name: str, *, sparse: bool) -> None:
    """A chain doc in the recorded store's exact shape (standard OCC rows).

    Dense names carry every strike at every nearby monthly (the sealed
    filter keeps the in-band DTEs and moneyness); the sparse name carries
    only the ATM strike, landing under the 50-contract-session pooled
    fallback."""
    strikes = (100.0,) if sparse else STRIKES
    rows: list[tuple[date, float, str]] = [
        (expiry, k, right)
        for expiry in _nearby_third_fridays(session)
        for k in strikes
        for right in ("C", "P")
    ]

    def occ(expiry: date, k: float, right: str) -> str:
        return f"{name}{expiry.strftime('%y%m%d')}{right}{int(k * 1000):08d}"

    doc = {
        "schema": "desk-chain/1",
        "header": {
            "schema": "desk-chain-header/1",
            "session": session.isoformat(),
            "underlying": name,
            "underlying_quote": {"close": 100.0},
            "fetched_at": "2025-01-01T16:15:00-04:00",
            "n": len(rows),
            "n_skipped": 0,
            "raw_sha256": "0" * 64,
            "source": "synthetic",
            "source_as_of": session.isoformat(),
        },
        "columns": {
            "occ": [occ(e, k, right) for e, k, right in rows],
            "exp": [e.isoformat() for e, _k, _right in rows],
            "strike": [k for _e, k, _right in rows],
            "right": [right for _e, _k, right in rows],
            "bid": [CHAIN_BID] * len(rows),
            "ask": [CHAIN_ASK] * len(rows),
        },
    }
    folder = store / "chains" / session.isoformat()
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{name}.json.gz").write_bytes(
        gzip.compress(json.dumps(doc, sort_keys=True).encode())
    )


def _write_manifest(store: Path, session: date, names: tuple[str, ...] | list[str]) -> None:
    doc = {
        "schema": "desk-chain-manifest/1",
        "session": session.isoformat(),
        "symbols": {
            n: {"status": "ok", "n": 12, "detail": "", "at": "2025-01-01T16:20:00-04:00"}
            for n in names
        },
        "updated_at": "2025-01-01T16:20:00-04:00",
    }
    (store / "manifest" / f"{session.isoformat()}.json").write_text(
        json.dumps(doc, sort_keys=True) + "\n", encoding="utf-8"
    )


# ---- the dry-run self-check ------------------------------------------------------------------


def self_check(result: dict[str, Any], world: SyntheticWorld) -> list[str]:
    """Structural assertions on a dry-run result (machinery, not numbers).
    Returns the checks performed; raises AssertionError on any failure."""
    exp = world.expected
    checks: list[str] = []

    def check(label: str, ok: bool) -> None:
        if not ok:
            raise AssertionError(f"dry-run self-check failed: {label}")
        checks.append(label)

    per_signal = result["entries"]["entries_per_signal"]
    check(
        "entries: 3 x FOM XSMOM names + every PEAD beat",
        per_signal.get("xsmom_top3") == exp["n_xsmom_entries"]
        and per_signal.get("pead_beat") == exp["n_pead_entries"],
    )
    trades = result["trades"]
    check(
        "every (entry, structure) attempt is EVALUABLE in the corpus",
        set(result["attempts_by_status"]) == {"EVALUABLE"},
    )
    check(
        "trades: T = 30 (XSMOM) and 20 (PEAD) per structure",
        all(
            result["variants"][f"{sig}|{s}"]["T"] == n
            for sig, n in (("xsmom_top3", 30), ("pead_beat", 20))
            for s in bt001.STRUCTURES
        ),
    )
    check(
        "no NOT_EVALUABLE verdict in the corpus",
        all(v["verdict"] in ("PASS", "FAIL") for v in result["variants"].values()),
    )
    check(
        "every trade enters at E0 strictly after its signal session and exits later",
        all(
            date.fromisoformat(t["e0"]) > date.fromisoformat(t["decision"])
            and date.fromisoformat(t["exit"]) > date.fromisoformat(t["e0"])
            for t in trades
        ),
    )
    paired = [p for p in result["placebo_pairs"] if p["status"] == "PAIRED"]
    check(
        "every evaluable trade drew a placebo on the identical path",
        len(paired) == len(trades),
    )
    check(
        "placebos never draw a name that fired the same signal that day",
        all(p["placebo"] != p["name"] for p in paired),
    )
    haircuts = result["stamp"]["haircuts"]
    check(
        "haircut measured at 0.02 for the dense names, pooled for the sparse one",
        all(abs(haircuts["h"][n] - HAIRCUT_HALFSPREAD) < 1e-12 for n in world.bar_names)
        and haircuts["pooled_names"] == exp["pooled_names"],
    )
    check(
        "the scan reports a max bar session inside the corpus calendar",
        result["stamp"]["scan"]["max_bar_session"] is not None
        and date.fromisoformat(result["stamp"]["scan"]["max_bar_session"])
        <= world.cal.sessions()[-1],
    )
    check(
        "k clusters meet the floor on both signals",
        all(
            (result["variants"][f"{sig}|S1"].get("b_detail") or {}).get("k", 0) >= bt001.K_MIN
            for sig in bt001.SIGNALS
        ),
    )
    check(
        "signal verdicts are the sealed vocabulary",
        set(result["signal_verdicts"].values())
        <= {"SUPPORTED (MODELED)", "NOT SUPPORTED (MODELED)"},
    )
    return checks


def spot_forward(spot: float, rate: float, dte: int) -> float:
    """The sealed forward F = S * exp(r * tau / 365) (shared by tests)."""
    return spot * math.exp(rate * dte / 365.0)
