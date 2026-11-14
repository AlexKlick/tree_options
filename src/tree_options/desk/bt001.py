"""DESK-BT-001: the sealed, never-run modeled options backtest (runner).

Every output of this study is labeled MODELED. It prices history from daily
VWAP bars plus a measured spread haircut, not from fills; it can support or
fail to support an options expression and can never promote, size or enable
a row (forward sealed cards decide).

Sealed lineage (the spec of record):

* ``docs/desk/DESK-BT-001.md``  sha256 ``e3e1d12d...39943139f77`` (2026-09-23)
* ``docs/desk/DESK-BT-001-AMENDMENT-1.md``  sha256 ``b4e2626a...c89b71ef1f86``
  ("where the two differ, this amendment governs; everything it does not
  name stays exactly as written" -- entries move to E0, the next NYSE
  session after the signal session D)
* ``docs/desk/DESK-BT-001-clarifications.md``  sha256
  ``bdc1c1a687eefc1412293730c57cb9b00b9d7abf232df1736dc1bcea5a7ed469``
  (sealed 2026-11-14 under the prereg's own BEFORE-THE-RUN device;
  narrowing-only: the A1 strike-rank basis, the A2 placebo pool and hash
  token, the A3 haircut symbol filter, the A4 snapshot pins enforced below,
  the A5 cache scope and max-bar-session report, the A6 miss-count reading,
  the A7 disclosed exploratory adjacency)

Run discipline (sealed in the spec of record, "Earliest run date and
procedure"): ONE scored run, under ``host-work``, into
``DESK_STORE/evaluations/DESK-BT-001.{json,md}``; a rerun is a
reproducibility check to a temp path (``--out-dir``), never a new verdict.
The runner refuses to write over an existing pair. Before that run,
``run_desk_bt001 --dry-run`` exercises this machinery end-to-end on the
synthetic corpus (``bt001_corpus``); it reads no real artifact for pricing
and verifies only the sealed documents' bytes. The scored run is an
operator action taken after the M0 gate passes; this module never scores on
import.

Three near-miss divergences of the exploratory ``historical_replay``
scaffold are corrected to the seal here and are NOT carried over: the
moneyness test runs on the forward F (``|ln(K0/F)| <= 0.03`` with DTB3,
never ``|K/spot - 1|``), strike ranks count distinct strikes with bars
(all rights; the selected rank still needs the leg's own right, else
NOT_EVALUABLE -- A1), and the haircut is the measured per-name h from the
first 20 recorded chain sessions (never a scenario input).

No lookahead: signals read panel rows dated on or before the signal session
D only; every entry price is a leg VWAP on E0 (the next session); exits
read E0 + at most 20 sessions; each DTB3 observation is the latest dated on
or before the session it prices. The haircut reads chain sessions that sit
after the entry window by the spec's own design (a declared known limit:
"measured on 2026 closing quotes and applied to 2024-2026 bars").
"""

from __future__ import annotations

import bisect
import hashlib
import json
import math
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from statistics import NormalDist, fmean, median, stdev
from typing import Any

from tree_options.desk import ivhist, signals, stats
from tree_options.desk.aibear.monitor import Chain, chain_sessions
from tree_options.desk.ivhist import RateSource
from tree_options.desk.sessions import Calendar, first_session_after, is_first_session_of_month
from tree_options.desk.universe import NO_OPTIONS_EXPRESSION, XSMOM_TRADABLES

SCHEMA = "desk-bt001/1"
STUDY = "DESK-BT-001"
MODELED_LABEL = "MODELED - never promotes, sizes or enables a row"

# ---- sealed constants ------------------------------------------------------------------------

WINDOW_FIRST = date(2024, 9, 3)
WINDOW_LAST = date(2026, 8, 31)
STRUCTURES = ("S1", "S2", "S3")
SIGNALS = ("xsmom_top3", "pead_beat")
MIN_ENTRY_DTE = 30
MAX_ENTRY_DTE = 60
MIN_EXIT_DTE = 7
HOLD_SESSIONS = 20
MONEYNESS_MAX = 0.03
HAIRCUT_MONEYNESS_MAX = 0.10
HAIRCUT_MIN_SESSIONS = 50
COMMISSION = 0.65
MULTIPLIER = 100
T_MIN = 20
K_MIN = 10
P_ALPHA = 0.05
DSR_MIN = 0.95
N_TRIALS = 6  # "N = 6 always"
EULER_GAMMA = 0.5772156649
PLACEBO_PREFIX = "DESK-BT-001-A1"  # Amendment 1's hash string
EQUITY_COST = 0.0005  # 5 bp
EQUITY_LABEL = "equity-dominated"
MAX_LOSS_REPORT_LEVEL = 500.0
SCAN_START = date(2024, 8, 26)  # the sealed IVHIST-001 window
SCAN_END = date(2026, 9, 3)
SCAN_MIN_DTE = MIN_EXIT_DTE  # entry 30..60 plus the exit floor of 7
SCAN_MAX_DTE = MAX_ENTRY_DTE

SPEC_SHA256 = "e3e1d12d75b043c6f344c928290f5f5a813c22a8df327de6c28e2d9943139f77"
AMENDMENT_1_SHA256 = "b4e2626aeb5d75f808b47941f07a357ef2c4fdcd947a2e06b5c3c89b71ef1f86"
CLARIFICATIONS_SHA256 = "bdc1c1a687eefc1412293730c57cb9b00b9d7abf232df1736dc1bcea5a7ed469"
PLAYBOOK_SHA256 = "bc69e250a9da181016e925f382657ef2b0db6de2630e93c709b7cb7e406e6cd1"
CALENDAR_SHA256 = "5c038a891c6ea99580b670518402fef7849ffd7a0f53d268fabdf1263f005f6b"
UNIVERSE_SHA256 = "5f79bc3fec2c078e4b4f17e8f3e20d942d12952b105d431f97bbf5935be94066"
N_XSMOM_TRADABLES = 36

# The original 29 names with on-disk massive-cache bars (A2's placebo pool).
BAR_NAMES: tuple[str, ...] = (
    "AAPL",
    "ADBE",
    "AMD",
    "AMZN",
    "AVGO",
    "COST",
    "CRM",
    "DIS",
    "GOOGL",
    "HD",
    "INTC",
    "IWM",
    "JPM",
    "KO",
    "LLY",
    "MA",
    "META",
    "MSFT",
    "NFLX",
    "NVDA",
    "PEP",
    "PG",
    "QCOM",
    "QQQ",
    "SPY",
    "TSLA",
    "UNH",
    "V",
    "XOM",
)

# A6: the first 20 recorded chain sessions, in order (2026-09-23 permanently
# missing shifted the earliest run date to 2026-10-21; the 10-26..10-30
# recorder outage falls outside the first 20 and shifts nothing).
FIRST_TWENTY: tuple[date, ...] = tuple(
    date.fromisoformat(d)
    for d in (
        "2026-09-22",
        "2026-09-24",
        "2026-09-25",
        "2026-09-28",
        "2026-09-29",
        "2026-09-30",
        "2026-10-01",
        "2026-10-02",
        "2026-10-05",
        "2026-10-06",
        "2026-10-07",
        "2026-10-08",
        "2026-10-09",
        "2026-10-12",
        "2026-10-13",
        "2026-10-14",
        "2026-10-15",
        "2026-10-16",
        "2026-10-19",
        "2026-10-20",
    )
)
MANIFEST_SHA256 = "9ec48aa44fa71fcd131bcbf7d5178ae9691ed616b995fb8e0e1a0aba9ae97ca7"
EARLIEST_RUN_DATE = date(2026, 10, 21)

# A4: consumed-slice pins for the growing inputs (measured 2026-11-14).
PANEL_CUTOFF = "2026-09-03"
PANEL_SLICE_SHA256 = "42ee7cd174a3748b0ea23bbc0268ee636d15d5ad3b2cc5fb70cc9e8f24a382e6"
DTB3_CUTOFF = "2026-10-20"
DTB3_SLICE_SHA256 = "4374f7741896985eeb8e255ef5c04a53390242d91173c1e2f362f28495f6a708"
EARNINGS_CUTOFF = "2026-08-31"
EARNINGS_SLICE_SHA256 = "e096a1ec2c099dbdc5a1ed6403c458e9f02d7e1dde527463d44c758e60fc26dc"


class Refused(RuntimeError):
    """A seal, pin or one-shot rule refused the run."""


@dataclass(frozen=True)
class StorePins:
    """The chain-store pins the runner enforces (A6)."""

    first_twenty: tuple[date, ...]
    manifest_sha256: str


@dataclass(frozen=True)
class SlicePins:
    """The consumed-slice pins for the growing inputs (A4)."""

    panel_cutoff: str
    panel_sha256: str
    dtb3_cutoff: str
    dtb3_sha256: str
    earnings_cutoff: str
    earnings_sha256: str


SEALED_STORE_PINS = StorePins(first_twenty=FIRST_TWENTY, manifest_sha256=MANIFEST_SHA256)
SEALED_SLICE_PINS = SlicePins(
    panel_cutoff=PANEL_CUTOFF,
    panel_sha256=PANEL_SLICE_SHA256,
    dtb3_cutoff=DTB3_CUTOFF,
    dtb3_sha256=DTB3_SLICE_SHA256,
    earnings_cutoff=EARNINGS_CUTOFF,
    earnings_sha256=EARNINGS_SLICE_SHA256,
)


# ---- seals and pins --------------------------------------------------------------------------


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_seals(docs: Path, repo: Path, *, calendar_path: Path | None = None) -> dict[str, str]:
    """Refuse anything but the sealed bytes; return the recorded sha256s."""
    doc_pins = {
        "DESK-BT-001.md": SPEC_SHA256,
        "DESK-BT-001-AMENDMENT-1.md": AMENDMENT_1_SHA256,
        "DESK-BT-001-clarifications.md": CLARIFICATIONS_SHA256,
    }
    recorded: dict[str, str] = {}
    for name, want in sorted(doc_pins.items()):
        path = docs / name
        if not path.exists():
            raise Refused(f"sealed document missing: {path}")
        got = _sha256_file(path)
        if got != want:
            raise Refused(f"{name} sha256 {got} is not the sealed {want}")
        side = docs / f"{name}.sha256"
        if side.exists():
            want_side, side_name = side.read_text().split()[:2]
            if side_name != path.name or want_side != want:
                raise Refused(f"{side.name} disagrees with the sealed bytes of {name}")
        recorded[f"docs/desk/{name}"] = got
    for rel, want in (
        ("data/desk/playbook/v1.toml", PLAYBOOK_SHA256),
        ("desk-universe.toml", UNIVERSE_SHA256),
    ):
        path = repo / rel
        if not path.exists():
            raise Refused(f"sealed companion missing: {path}")
        got = _sha256_file(path)
        if got != want:
            raise Refused(f"{rel} sha256 {got} is not the sealed {want}")
        recorded[rel] = got
    if len(XSMOM_TRADABLES) != N_XSMOM_TRADABLES:
        raise Refused(
            f"xsmom.tradables numbers {len(XSMOM_TRADABLES)}, not the sealed {N_XSMOM_TRADABLES}"
        )
    if calendar_path is not None:
        got = _sha256_file(calendar_path)
        if got != CALENDAR_SHA256:
            raise Refused(f"the run calendar sha256 {got} is not the sealed {CALENDAR_SHA256}")
        recorded["data/calendar/trex/nyse_sessions_2018_01_02_2028_12_29.json"] = got
    return recorded


def manifest_sha256(store: Path, through: date) -> str:
    """sha256 over each chain manifest's name and bytes through ``through``."""
    agg = hashlib.sha256()
    names = sorted(
        p.name
        for p in (store / "manifest").glob("*.json")
        if date.fromisoformat(p.name.removesuffix(".json")) <= through
    )
    for name in names:
        agg.update(name.encode("utf-8"))
        agg.update(hashlib.sha256((store / "manifest" / name).read_bytes()).digest())
    return agg.hexdigest()


def first_twenty_sessions(store: Path, pins: StorePins) -> tuple[list[date], str]:
    """The first 20 recorded chain sessions, pinned exactly (A6)."""
    sessions = chain_sessions(store)
    if len(sessions) < 20:
        raise Refused(
            f"the chain store holds {len(sessions)} recorded sessions, fewer than the"
            " 20 the haircut needs -- the window is not open"
        )
    twenty = sessions[:20]
    if tuple(twenty) != pins.first_twenty:
        raise Refused(
            "the first 20 recorded chain sessions are not the pinned list"
            f" (got {twenty[0]}..{twenty[-1]}, {len(twenty)} sessions)"
        )
    digest = manifest_sha256(store, twenty[-1])
    if digest != pins.manifest_sha256:
        raise Refused(f"the chain manifests' digest {digest} is not the pinned one")
    return twenty, digest


# ---- growing-input slice digests (A4) --------------------------------------------------------


def _canonical_sha256(doc: Mapping[str, Any]) -> str:
    body = json.dumps(doc, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def panel_slice_sha256(panel: Mapping[str, Mapping[str, Any]], cutoff: str) -> str:
    doc = {
        name: {d: row for d, row in sorted(bars.items()) if d <= cutoff}
        for name, bars in sorted(panel.items())
    }
    return _canonical_sha256(doc)


def earnings_slice_sha256(earnings: Mapping[str, Sequence[str]], cutoff: str) -> str:
    doc = {
        name: sorted(str(d) for d in dates if str(d) <= cutoff)
        for name, dates in sorted(earnings.items())
    }
    return _canonical_sha256(doc)


def dtb3_slice_sha256(dtb3_path: Path, cutoff: str) -> str:
    lines = dtb3_path.read_text(encoding="utf-8").splitlines()
    kept = [lines[0]] + [ln for ln in lines[1:] if ln.split(",")[0] <= cutoff]
    return hashlib.sha256("\n".join(kept).encode("utf-8")).hexdigest()


# ---- haircut (the sealed measurement) --------------------------------------------------------


@dataclass(frozen=True)
class Haircuts:
    h: dict[str, float]
    pooled_names: tuple[str, ...]
    counts: dict[str, int]
    pooled_value: float
    pooled_pool_n: int


def measure_haircuts(
    store: Path, sessions: Sequence[date], cal: Calendar, rates: RateSource
) -> Haircuts:
    """Per-name median half-spread fraction over the first-20 chain sessions.

    The sealed filter (A3-pinned): standard OCC roots only (``Chain`` keeps
    those), a monthly expiry, 30 <= DTE <= 60, |ln(K/F)| <= 0.10 on the
    chain session's own forward, bid > 0 and ask >= bid. A name with fewer
    than 50 qualifying contract-sessions takes the median pooled over the
    29 bar-names (A2's pool)."""
    per_name: dict[str, list[float]] = {}
    skipped_chains = 0
    monthly: dict[date, bool] = {}  # the expiry check is per-row; memoize it
    for session in sessions:
        folder = store / "chains" / session.isoformat()
        for path in sorted(folder.glob("*.json.gz")):
            name = path.name.removesuffix(".json.gz")
            chain = Chain.load(store, session, name)
            if chain is None or not chain.spot or chain.spot <= 0.0:
                skipped_chains += 1
                continue
            rate = rates.rate_on(session)
            if rate is None:
                skipped_chains += 1
                continue
            vals = per_name.setdefault(name, [])
            for row in chain.rows.values():
                dte = (row["exp"] - session).days
                if not MIN_ENTRY_DTE <= dte <= MAX_ENTRY_DTE:
                    continue
                if monthly.get(row["exp"]) is None:
                    monthly[row["exp"]] = ivhist.is_monthly_expiry_session(row["exp"], cal)
                if not monthly[row["exp"]]:
                    continue
                forward = chain.spot * math.exp(rate * dte / 365.0)
                if abs(math.log(row["strike"] / forward)) > HAIRCUT_MONEYNESS_MAX:
                    continue
                if not (row["bid"] > 0.0 and row["ask"] >= row["bid"]):
                    continue
                vals.append((row["ask"] - row["bid"]) / (row["ask"] + row["bid"]))
    pool: list[float] = []
    for name in BAR_NAMES:
        pool.extend(per_name.get(name, ()))
    if not pool:
        raise Refused("no qualifying contract-session in the first 20 for the 29-name pool")
    pooled_value = median(pool)
    h: dict[str, float] = {}
    pooled: list[str] = []
    for name, vals in sorted(per_name.items()):
        if len(vals) >= HAIRCUT_MIN_SESSIONS:
            h[name] = median(vals)
        else:
            h[name] = pooled_value
            pooled.append(name)
    return Haircuts(
        h=h,
        pooled_names=tuple(pooled),
        counts={name: len(vals) for name, vals in sorted(per_name.items())},
        pooled_value=pooled_value,
        pooled_pool_n=len(pool),
    )


# ---- entries ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Entry:
    signal: str
    name: str
    session: date  # D: the signal session, observed at its close


def enumerate_entries(
    panel: Mapping[str, Mapping[str, Any]],
    earnings: Mapping[str, Sequence[str]],
    cal: Calendar,
    *,
    window: tuple[date, date] = (WINDOW_FIRST, WINDOW_LAST),
) -> tuple[list[Entry], dict[str, Any]]:
    """Every sealed entry: XSMOM top-3 on each first-of-month session that
    fires (each name one entry), every PEAD beat, D inside the window."""
    first, last = window
    out: list[Entry] = []
    xsmom_cards = 0
    for session in cal.sessions():
        if not first <= session <= last:
            continue
        if is_first_session_of_month(session, cal):
            res = signals.xsmom_top3(panel, session, cal)
            if res.fires:
                xsmom_cards += 1
                out.extend(Entry("xsmom_top3", name, session) for name in res.top3)
        beats = signals.pead_beats(panel, earnings, session, cal).beats
        out.extend(Entry("pead_beat", ev.name, session) for ev in beats)
    notes = {
        "window": [first.isoformat(), last.isoformat()],
        "xsmom_cards": xsmom_cards,
        "entries_per_signal": dict(sorted(Counter(e.signal for e in out).items())),
    }
    return out, notes


# ---- structure selection and pricing ---------------------------------------------------------


@dataclass(frozen=True)
class StructureChoice:
    expiry: date
    dte: int
    spot: float
    rate: float
    forward: float
    k0: float
    strikes: tuple[float, ...]  # distinct strikes with ANY bar on E0, ascending (A1)


def select_structure(
    bars: Sequence[ivhist.OptionBar], *, e0: date, spot: float, rate: float
) -> tuple[StructureChoice | None, str]:
    """The LATEST monthly with 30..60 DTE at E0 among those with bars, then
    K0 nearest the forward among both-sides strikes (ties: the lower)."""
    by_exp: dict[date, dict[float, set[str]]] = {}
    for bar in bars:
        dte = (bar.expiry - e0).days
        if not MIN_ENTRY_DTE <= dte <= MAX_ENTRY_DTE:
            continue
        by_exp.setdefault(bar.expiry, {}).setdefault(bar.strike, set()).add(bar.right)
    if not by_exp:
        return None, "no_eligible_expiry"
    expiry = max(by_exp)
    sides = by_exp[expiry]
    dte = (expiry - e0).days
    forward = spot * math.exp(rate * dte / 365.0)
    both = sorted(k for k, rights in sides.items() if {"C", "P"} <= rights)
    if not both:
        return None, "no_atm_strike"
    k0 = min(both, key=lambda k: (abs(math.log(k / forward)), k))
    if abs(math.log(k0 / forward)) > MONEYNESS_MAX:
        return None, "moneyness_out"
    return (
        StructureChoice(
            expiry=expiry,
            dte=dte,
            spot=spot,
            rate=rate,
            forward=forward,
            k0=k0,
            strikes=tuple(sorted(sides)),
        ),
        "EVALUABLE",
    )


def exit_session(e0: date, expiry: date, cal: Calendar) -> date | None:
    """Earlier of E0 + 20 sessions and the last session with DTE >= 7."""
    sessions = cal.sessions()
    i = bisect.bisect_left(sessions, e0)
    if i == len(sessions) or sessions[i] != e0:
        return None
    limit = min(i + HOLD_SESSIONS, len(sessions) - 1)
    while limit > i and (expiry - sessions[limit]).days < MIN_EXIT_DTE:
        limit -= 1
    return sessions[limit] if limit > i else None


Leg = tuple[str, float, int]  # (right, strike, side: +1 buy / -1 sell)


def structure_legs(structure: str, choice: StructureChoice) -> tuple[Leg, ...] | None:
    """The sealed legs. Ranks move in ``choice.strikes`` (distinct strikes
    with ANY bar on E0 -- A1); the leg rule still demands the leg's right."""
    i = choice.strikes.index(choice.k0)
    if structure == "S1":
        return (("C", choice.k0, 1),)
    if structure == "S2":
        if i + 2 >= len(choice.strikes):
            return None
        return (("C", choice.k0, 1), ("C", choice.strikes[i + 2], -1))
    if structure == "S3":
        if i - 3 < 0:
            return None
        return (("P", choice.strikes[i - 1], -1), ("P", choice.strikes[i - 3], 1))
    raise ValueError(f"unknown structure {structure}")


def _marks(bars: Sequence[ivhist.OptionBar], expiry: date) -> dict[tuple[str, float], float]:
    return {(b.right, b.strike): b.vwap for b in bars if b.expiry == expiry}


def _cash(
    legs: Sequence[Leg],
    marks: Mapping[tuple[str, float], float],
    haircut: float,
    *,
    entry: bool,
) -> float | None:
    """Signed cash flow of one fill set: a buy pays VWAP*(1+h), a sell
    receives VWAP*(1-h); commission per contract per leg."""
    total = 0.0
    for right, strike, side in legs:
        px = marks.get((right, strike))
        if px is None or px <= 0.0:
            return None
        signed = side if entry else -side
        factor = (1.0 + haircut) if signed > 0 else (1.0 - haircut)
        total -= signed * px * factor * MULTIPLIER
        total -= COMMISSION
    return total


@dataclass(frozen=True)
class PricedTrade:
    """A (name, E0, structure) priced under the sealed rules. Signal agnostic:
    the same pricing serves a signal entry and its placebo (A2)."""

    name: str
    e0: date
    structure: str
    expiry: date
    k0: float
    legs: tuple[Leg, ...]
    exit_session: date
    haircut: float
    pnl_base: float
    max_loss_base: float
    r_base: float
    r_stress: float | None


def price_trade(
    name: str,
    e0: date,
    structure: str,
    scan: ivhist.CacheScan,
    cal: Calendar,
    rates: RateSource,
    haircuts: Mapping[str, float],
) -> tuple[PricedTrade | None, str]:
    """One (name, E0, structure) under the sealed rules; (None, status) when
    NOT_EVALUABLE. No substitution, no carried price."""
    if name in NO_OPTIONS_EXPRESSION:
        return None, "no_options_expression"
    opts = scan.options.get(name)
    if not opts:
        return None, "no_bars"
    bars_e0 = opts.get(e0)
    if not bars_e0:
        return None, "no_entry_bars"
    spot = scan.spot.get(name, {}).get(e0)
    if spot is None or spot <= 0.0:
        return None, "no_spot"
    rate = rates.rate_on(e0)
    if rate is None:
        return None, "no_rate"
    choice, status = select_structure(bars_e0, e0=e0, spot=spot, rate=rate)
    if choice is None:
        return None, status
    legs = structure_legs(structure, choice)
    if legs is None:
        return None, "rank_window_missing"
    exit_day = exit_session(e0, choice.expiry, cal)
    if exit_day is None:
        return None, "no_exit_session"
    marks_e0 = _marks(bars_e0, choice.expiry)
    marks_exit = _marks(opts.get(exit_day, ()), choice.expiry)
    h = haircuts.get(name)
    if h is None:
        return None, "no_haircut"

    def _r(mult: float) -> tuple[float, float, float] | None:
        hh = mult * h
        opening = _cash(legs, marks_e0, hh, entry=True)
        closing = _cash(legs, marks_exit, hh, entry=False)
        if opening is None or closing is None:
            return None
        if structure == "S3":
            width = abs(legs[0][1] - legs[1][1]) * MULTIPLIER
            max_loss = width - opening + len(legs) * COMMISSION
        else:
            max_loss = -opening + len(legs) * COMMISSION
        if max_loss <= 0.0:
            return None
        return opening + closing, max_loss, (opening + closing) / max_loss

    base = _r(1.0)
    if base is None:
        missing_bar = any(
            (right, strike) not in marks_e0 or (right, strike) not in marks_exit
            for right, strike, _side in legs
        )
        return None, "missing_leg_bar" if missing_bar else "invalid_payoff"
    stress = _r(2.0)
    pnl_base, max_loss, r_base = base
    return (
        PricedTrade(
            name=name,
            e0=e0,
            structure=structure,
            expiry=choice.expiry,
            k0=choice.k0,
            legs=legs,
            exit_session=exit_day,
            haircut=h,
            pnl_base=pnl_base,
            max_loss_base=max_loss,
            r_base=r_base,
            r_stress=None if stress is None else stress[2],
        ),
        "EVALUABLE",
    )


# ---- placebo (Amendment 1 + A2) --------------------------------------------------------------


def placebo_index(e0: date, name: str, structure: str, n: int) -> int:
    token = f"{PLACEBO_PREFIX}|{e0.isoformat()}|{name}|{structure}"
    return int(hashlib.sha256(token.encode("utf-8")).hexdigest()[:16], 16) % n


def draw_placebo(e0: date, name: str, structure: str, candidates: Sequence[str]) -> str:
    ordered = sorted(candidates)
    return ordered[placebo_index(e0, name, structure, len(ordered))]


# ---- statistics ------------------------------------------------------------------------------


def student_t_sf_one_sided(t: float, dof: int) -> float:
    """P(T > t) for Student's t with ``dof`` degrees of freedom (exact
    regularized incomplete beta; this environment has no SciPy)."""
    if dof <= 0:
        raise ValueError("dof must be positive")
    if t != t:
        return float("nan")
    if t == 0.0:
        return 0.5
    p = 0.5 * _betainc(dof / 2.0, 0.5, dof / (dof + t * t))
    return p if t > 0 else 1.0 - p


def _betainc(a: float, b: float, x: float) -> float:
    """The regularized incomplete beta I_x(a, b)."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    front = math.exp(
        math.lgamma(a + b)
        - math.lgamma(a)
        - math.lgamma(b)
        + a * math.log(x)
        + b * math.log(1.0 - x)
    )
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def _betacf(a: float, b: float, x: float) -> float:
    tiny = 1e-30
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < tiny:
        d = tiny
    d = 1.0 / d
    h = d
    for m in range(1, 220):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + aa / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + aa / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 3e-12:
            return h
    return h


@dataclass(frozen=True)
class PairedClusters:
    k: int
    t: float | None
    p: float | None
    cluster_means: tuple[float, ...]
    degenerate: bool


def clustered_paired_test(diffs: Sequence[float], clusters: Sequence[str]) -> PairedClusters | None:
    """d averaged within each entry session E0 (the cluster); one-sided
    Student t on the k session means, k-1 dof."""
    if not diffs:
        return None
    by_cluster: dict[str, list[float]] = {}
    for d, key in zip(diffs, clusters, strict=True):
        by_cluster.setdefault(key, []).append(d)
    means = [fmean(v) for _key, v in sorted(by_cluster.items())]
    k = len(means)
    mean = fmean(means)
    if k < 2:
        return PairedClusters(k=k, t=None, p=None, cluster_means=tuple(means), degenerate=True)
    sd = stdev(means)
    if sd == 0.0:
        p = 0.0 if mean > 0 else (1.0 if mean < 0 else 0.5)
        return PairedClusters(k=k, t=None, p=p, cluster_means=tuple(means), degenerate=True)
    t = mean / (sd / math.sqrt(k))
    return PairedClusters(
        k=k, t=t, p=student_t_sf_one_sided(t, k - 1), cluster_means=means, degenerate=False
    )


def sharpe_and_moments(returns: Sequence[float]) -> dict[str, float | None]:
    """SR = mean/sd (sd with T-1); g3 = m3/m2^1.5, g4 = m4/m2^2 population."""
    n = len(returns)
    if n < 2:
        return {"SR": None, "sd": None, "g3": None, "g4": None, "mean": None}
    mean = fmean(returns)
    m2 = fmean([(x - mean) ** 2 for x in returns])
    if m2 <= 0.0:
        return {"SR": None, "sd": 0.0, "g3": None, "g4": None, "mean": mean}
    sd = stdev(returns)
    m3 = fmean([(x - mean) ** 3 for x in returns])
    m4 = fmean([(x - mean) ** 4 for x in returns])
    return {
        "SR": mean / sd if sd > 0 else None,
        "sd": sd,
        "g3": m3 / m2**1.5,
        "g4": m4 / m2**2,
        "mean": mean,
    }


def deflated_sharpe(
    returns: Sequence[float], v_across: float, *, n_trials: int = N_TRIALS
) -> float | None:
    """Bailey & Lopez de Prado (2014) DSR exactly as the prereg states it:
    SR0 = sqrt(V) * ((1 - gamma) * PhiInv(1 - 1/N) + gamma * PhiInv(1 -
    1/(N*e))), N = 6 always; DSR = Phi((SR - SR0) * sqrt(T - 1) / sqrt(1 -
    g3*SR + (g4 - 1)/4 * SR^2))."""
    m = sharpe_and_moments(returns)
    sr, g3, g4 = m["SR"], m["g3"], m["g4"]
    if sr is None or g3 is None or g4 is None:
        return None
    nd = NormalDist()
    sr0 = math.sqrt(v_across) * (
        (1.0 - EULER_GAMMA) * nd.inv_cdf(1.0 - 1.0 / n_trials)
        + EULER_GAMMA * nd.inv_cdf(1.0 - 1.0 / (n_trials * math.e))
    )
    den = 1.0 - g3 * sr + (g4 - 1.0) / 4.0 * sr * sr
    if den <= 0.0:
        return None
    return nd.cdf((sr - sr0) * math.sqrt(len(returns) - 1) / math.sqrt(den))


# ---- the run ---------------------------------------------------------------------------------


@dataclass
class Inputs:
    panel: Mapping[str, Mapping[str, Any]]
    panel_sha256: str
    earnings: Mapping[str, Sequence[str]]
    earnings_sha256: str
    dtb3_path: Path
    cal: Calendar
    calendar_path: Path | None
    rates: RateSource
    scan: ivhist.CacheScan
    store: Path
    docs: Path
    repo: Path
    code_commit: str | None
    now: datetime
    window: tuple[date, date] = (WINDOW_FIRST, WINDOW_LAST)


def _equity_return(
    panel: Mapping[str, Mapping[str, Any]],
    name: str,
    e0: date,
    exit_day: date,
    *,
    cutoff: str,
) -> float | None:
    """The executable equity card: open(E0) to close(exit), minus 5 bp."""
    rows = panel.get(name, {})
    for d in (e0, exit_day):
        if d.isoformat() > cutoff:
            raise Refused(
                f"the equity card would read a panel row dated {d.isoformat()}, past"
                f" the amendment's consumption cutoff {cutoff}"
            )
    o = rows.get(e0.isoformat(), {}).get("open")
    c = rows.get(exit_day.isoformat(), {}).get("close")
    if o is None or c is None or not o > 0:
        return None
    return float(c) / float(o) - 1.0 - EQUITY_COST


def run(
    inputs: Inputs,
    *,
    out_dir: Path,
    store_pins: StorePins = SEALED_STORE_PINS,
    slice_pins: SlicePins = SEALED_SLICE_PINS,
    scored: bool = True,
) -> dict[str, Any]:
    """The one scored run (or the identical dry-run on the synthetic corpus).

    Refuses, in order: unsealed documents, a moved calendar, a chain store
    whose first-twenty sessions or manifest digest drifted, a growing input
    whose consumed-slice digest moved, an empty bar scan, and an existing
    ``DESK-BT-001.json``/``.md`` pair in ``out_dir`` (one scored run, never
    overwritten)."""
    seals = verify_seals(inputs.docs, inputs.repo, calendar_path=inputs.calendar_path)
    twenty, manifest_digest = first_twenty_sessions(inputs.store, store_pins)
    if inputs.now.date() <= twenty[-1]:
        raise Refused(
            f"the run date {inputs.now.date()} is not after the 20th recorded chain"
            f" session {twenty[-1]} (A6: the earliest run date is one session later)"
        )

    got_panel = panel_slice_sha256(inputs.panel, slice_pins.panel_cutoff)
    if got_panel != slice_pins.panel_sha256:
        raise Refused(f"panel consumed-slice sha256 {got_panel} is not the pinned one")
    got_dtb3 = dtb3_slice_sha256(inputs.dtb3_path, slice_pins.dtb3_cutoff)
    if got_dtb3 != slice_pins.dtb3_sha256:
        raise Refused(f"DTB3 consumed-slice sha256 {got_dtb3} is not the pinned one")
    got_earn = earnings_slice_sha256(inputs.earnings, slice_pins.earnings_cutoff)
    if got_earn != slice_pins.earnings_sha256:
        raise Refused(f"earnings consumed-slice sha256 {got_earn} is not the pinned one")
    if inputs.scan.stats.get("option_bars", 0) <= 0:
        raise Refused("the option-bar scan is empty -- check the cache path (A5)")
    max_bar_session = max(
        (d for by_name in inputs.scan.options.values() for d in by_name), default=None
    )

    haircuts = measure_haircuts(inputs.store, twenty, inputs.cal, inputs.rates)
    entries, entry_notes = enumerate_entries(
        inputs.panel, inputs.earnings, inputs.cal, window=inputs.window
    )
    fired: dict[tuple[str, str], set[str]] = {}
    for entry in entries:
        fired.setdefault((entry.signal, entry.session.isoformat()), set()).add(entry.name)

    cache: dict[tuple[str, date, str], tuple[PricedTrade | None, str]] = {}

    def price(name: str, e0: date, structure: str) -> tuple[PricedTrade | None, str]:
        key = (name, e0, structure)
        if key not in cache:
            cache[key] = price_trade(
                name, e0, structure, inputs.scan, inputs.cal, inputs.rates, haircuts.h
            )
        return cache[key]

    attempts: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    placebo_pairs: list[dict[str, Any]] = []
    no_candidate: Counter[tuple[str, str]] = Counter()
    for entry in entries:
        e0 = first_session_after(entry.session, inputs.cal)
        for structure in STRUCTURES:
            if e0 is None:
                attempts.append(_attempt(entry, None, structure, "no_next_session"))
                continue
            priced, status = price(entry.name, e0, structure)
            attempts.append(_attempt(entry, e0, structure, status))
            if priced is None:
                continue
            rows.append(_trade_row(entry, e0, priced))
            fired_names = fired.get((entry.signal, entry.session.isoformat()), set())
            candidates = [
                n
                for n in BAR_NAMES
                if n not in fired_names and price(n, e0, structure)[0] is not None
            ]
            pair: dict[str, Any] = {
                "signal": entry.signal,
                "structure": structure,
                "e0": e0.isoformat(),
                "name": entry.name,
            }
            if not candidates:
                no_candidate[(entry.signal, structure)] += 1
                placebo_pairs.append({**pair, "status": "no_candidate"})
                continue
            drawn = draw_placebo(e0, entry.name, structure, candidates)
            placebo, _pstatus = price(drawn, e0, structure)
            if placebo is None:  # candidates were screened evaluable; a defect if not
                raise Refused(f"placebo {drawn} drew from evaluable candidates but is not")
            placebo_pairs.append(
                {
                    **pair,
                    "placebo": drawn,
                    "d": priced.r_base - placebo.r_base,
                    "status": "PAIRED",
                }
            )

    equity: dict[tuple[str, str, str, str], float | None] = {}
    for row in rows:
        key = (row["signal"], row["name"], row["decision"], row["structure"])
        equity[key] = _equity_return(
            inputs.panel,
            row["name"],
            date.fromisoformat(row["e0"]),
            date.fromisoformat(row["exit"]),
            cutoff=slice_pins.panel_cutoff,
        )
        row["equity_return"] = equity[key]

    variants = _variant_table(rows, placebo_pairs, equity)
    signal_verdicts = {
        sig: (
            "SUPPORTED (MODELED)"
            if any(
                variants[f"{sig}|{s}"]["verdict"] == "PASS"
                for s in STRUCTURES
                if f"{sig}|{s}" in variants
            )
            else "NOT SUPPORTED (MODELED)"
        )
        for sig in SIGNALS
    }

    result = {
        "schema": SCHEMA,
        "study": STUDY,
        "label": MODELED_LABEL,
        "stamp": {
            "generated_at": inputs.now.isoformat(),
            "code_commit": inputs.code_commit,
            "scored": scored,
            "seals_sha256": seals,
            "first_twenty_chain_sessions": [d.isoformat() for d in twenty],
            "chain_manifests_sha256": manifest_digest,
            "earliest_run_date": EARLIEST_RUN_DATE.isoformat(),
            "inputs_sha256": {
                "ohlc-panel.json": inputs.panel_sha256,
                "ohlc-panel.consumed_slice": got_panel,
                "earnings-calendar.json": inputs.earnings_sha256,
                "earnings-calendar.consumed_slice": got_earn,
                "DTB3.csv.consumed_slice": got_dtb3,
            },
            "scan": {
                "source": inputs.scan.source,
                "input_digest": inputs.scan.input_digest,
                "input_files": inputs.scan.input_files,
                "stats": dict(sorted(inputs.scan.stats.items())),
                "max_bar_session": max_bar_session.isoformat() if max_bar_session else None,
            },
            "haircuts": {
                "h": dict(sorted(haircuts.h.items())),
                "pooled_names": list(haircuts.pooled_names),
                "contract_sessions_per_name": haircuts.counts,
                "pooled_value": haircuts.pooled_value,
                "pooled_pool_n": haircuts.pooled_pool_n,
            },
        },
        "entries": entry_notes,
        "attempts_by_status": dict(sorted(Counter(a["status"] for a in attempts).items())),
        "attempts": attempts,
        "trades": rows,
        "placebo_pairs": placebo_pairs,
        "placebo_no_candidate": {
            f"{sig}|{s}": no_candidate.get((sig, s), 0) for sig in SIGNALS for s in STRUCTURES
        },
        "variants": variants,
        "signal_verdicts": signal_verdicts,
        "over_max_loss_share": (
            round(sum(1 for r in rows if r["max_loss_base"] > MAX_LOSS_REPORT_LEVEL) / len(rows), 4)
            if rows
            else None
        ),
        "conventions": [
            "strike ranks count distinct strikes with ANY bar on E0 (A1); the leg's"
            " own right is then required by the evaluability rule, no per-right"
            " re-ranking",
            "placebo pool = the 29 original bar-names; structure token literally"
            " S1/S2/S3 in DESK-BT-001-A1|E0|X|structure (A2)",
            "the haircut filter keeps standard OCC roots, monthly expiries, 30<=DTE<=60,"
            " |ln(K/F)|<=0.10 on the chain session's forward, bid>0, ask>=bid (A3)",
            "r at the stress haircut 2h reuses the whole sealed formula at that level:"
            " P&L(2h)/max_loss(2h); trades with an undefined r at 2h are reported and"
            " excluded from (d) only",
            "V = the sample variance of SR across variants with T >= 20 and a defined SR",
            "a dropped placebo pair (no candidate) removes the pair from test (b) only;"
            " the signal trade stays in (a), (c) and (d)",
            "the equity card reads open(E0) to close(exit) from the split-adjusted panel"
            " minus 5 bp and decides nothing",
        ],
        "context": [
            "A7 disclosure: an exploratory modeled replay of the same signals with"
            " similar structures (scenario haircut, trade cap, spot moneyness,"
            " per-right ranks) was run and viewed after the seal; it evaluates none"
            " of the sealed bars. Nothing computed by this sealed runner existed"
            " before the 2026-09-23 seal beyond that disclosed lane.",
            "Known limit (Amendment 1): entries whose exits fall past the on-disk bars"
            " are NOT_EVALUABLE and counted.",
            "VWAP is a day average of trades, not a quote; the haircut is measured on"
            " 2026 closing quotes and applied to 2024-2026 bars.",
        ],
    }
    _write_outputs(out_dir, result)
    return result


def _attempt(entry: Entry, e0: date | None, structure: str, status: str) -> dict[str, Any]:
    return {
        "signal": entry.signal,
        "name": entry.name,
        "decision": entry.session.isoformat(),
        "e0": e0.isoformat() if e0 else None,
        "structure": structure,
        "status": status,
    }


def _trade_row(entry: Entry, e0: date, priced: PricedTrade) -> dict[str, Any]:
    return {
        "signal": entry.signal,
        "name": entry.name,
        "decision": entry.session.isoformat(),
        "e0": e0.isoformat(),
        "structure": priced.structure,
        "expiry": priced.expiry.isoformat(),
        "k0": priced.k0,
        "legs": [
            {"right": right, "strike": strike, "side": side} for right, strike, side in priced.legs
        ],
        "exit": priced.exit_session.isoformat(),
        "haircut": priced.haircut,
        "pnl_base": round(priced.pnl_base, 4),
        "max_loss_base": round(priced.max_loss_base, 2),
        "r_base": priced.r_base,
        "r_stress": priced.r_stress,
        "equity_return": None,
    }


def _variant_table(
    rows: Sequence[Mapping[str, Any]],
    placebo_pairs: Sequence[Mapping[str, Any]],
    equity: Mapping[tuple[str, str, str, str], float | None],
) -> dict[str, Any]:
    rows_by: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        rows_by.setdefault(f"{row['signal']}|{row['structure']}", []).append(row)
    stats_by: dict[str, dict[str, Any]] = {}
    for key, variant_rows in sorted(rows_by.items()):
        rets = [float(r["r_base"]) for r in variant_rows]
        m = sharpe_and_moments(rets)
        stress = [float(r["r_stress"]) for r in variant_rows if r["r_stress"] is not None]
        stats_by[key] = {
            "T": len(variant_rows),
            "mean_r_base": m["mean"],
            "mean_r_stress": fmean(stress) if stress else None,
            "d_undefined": len(variant_rows) - len(stress),
            **m,
        }
    sr_values = [v["SR"] for v in stats_by.values() if v["T"] >= T_MIN and v["SR"] is not None]
    n_evaluable = len(sr_values)
    v_across = stdev(sr_values) if n_evaluable >= 2 else None

    variants: dict[str, Any] = {}
    for sig in SIGNALS:
        for structure in STRUCTURES:
            key = f"{sig}|{structure}"
            variant_rows = rows_by.get(key, [])
            base = stats_by.get(key) or {
                "T": 0,
                "mean_r_base": None,
                "mean_r_stress": None,
                "d_undefined": 0,
                "SR": None,
            }
            cell: dict[str, Any] = {
                **base,
                "V_across": v_across,
                "n_evaluable_variants": n_evaluable,
            }
            if base["T"] < T_MIN:
                cell.update(a=None, b=None, c=None, d=None)
                cell["verdict"] = "NOT_EVALUABLE"
                cell["reason"] = f"T={base['T']} < {T_MIN}"
                cell["equity_card"] = _equity_card(variant_rows, equity, base)
                variants[key] = cell
                continue
            cell["a"] = base["mean_r_base"] is not None and base["mean_r_base"] > 0.0
            cell["d"] = base["mean_r_stress"] is not None and base["mean_r_stress"] > 0.0
            pairs = [p for p in placebo_pairs if p["status"] == "PAIRED" and _pair_key(p) == key]
            if pairs:
                clustered = clustered_paired_test(
                    [float(p["d"]) for p in pairs], [str(p["e0"]) for p in pairs]
                )
                assert clustered is not None
                if clustered.k < K_MIN:
                    cell["b"] = None
                    cell["b_reason"] = f"k={clustered.k} < {K_MIN}"
                else:
                    cell["b"] = clustered.p is not None and clustered.p < P_ALPHA
                    cell["b_detail"] = {
                        "k": clustered.k,
                        "t": clustered.t,
                        "p": clustered.p,
                        "degenerate": clustered.degenerate,
                    }
            else:
                cell["b"] = None
                cell["b_reason"] = "no paired placebo"
            if n_evaluable < 3:
                cell["c"] = None
                cell["c_reason"] = f"{n_evaluable} evaluable variants < 3"
            else:
                assert v_across is not None
                dsr = deflated_sharpe([float(r["r_base"]) for r in variant_rows], v_across)
                cell["c"] = dsr is not None and dsr >= DSR_MIN
                cell["c_detail"] = {"DSR": dsr, "N": N_TRIALS}
            if cell["b"] is None:
                cell["verdict"] = "NOT_EVALUABLE"
                cell["reason"] = cell.get("b_reason", "(b) not evaluable")
            elif cell["c"] is None:
                cell["verdict"] = "NOT_EVALUABLE"
                cell["reason"] = cell.get("c_reason", "(c) not evaluable")
            elif all(cell[x] for x in ("a", "b", "c", "d")):
                cell["verdict"] = "PASS"
                cell["reason"] = "a, b, c and d all pass"
            else:
                cell["verdict"] = "FAIL"
                cell["reason"] = "at least one of a, b, c, d fails"
            cell["equity_card"] = _equity_card(variant_rows, equity, base)
            variants[key] = cell
    return variants


def _equity_card(
    variant_rows: Sequence[Mapping[str, Any]],
    equity: Mapping[tuple[str, str, str, str], float | None],
    base: Mapping[str, Any],
) -> dict[str, Any]:
    eq = [
        equity[(r["signal"], r["name"], r["decision"], r["structure"])]
        for r in variant_rows
        if equity.get((r["signal"], r["name"], r["decision"], r["structure"])) is not None
    ]
    eq_stats = sharpe_and_moments(eq) if len(eq) >= 2 else {"SR": None, "mean": None}
    rs = [
        float(r["r_base"])
        for r in variant_rows
        if equity.get((r["signal"], r["name"], r["decision"], r["structure"])) is not None
    ]
    corr = stats.pearson(rs, eq) if len(eq) >= 2 else None
    return {
        "n": len(eq),
        "mean": eq_stats.get("mean"),
        "sharpe": eq_stats.get("SR"),
        "corr_r_equity": corr,
        EQUITY_LABEL: bool(
            eq_stats.get("SR") is not None
            and base.get("SR") is not None
            and base["SR"] < eq_stats["SR"]
        ),
    }


def _pair_key(pair: Mapping[str, Any]) -> str:
    return f"{pair['signal']}|{pair['structure']}"


def _write_outputs(out_dir: Path, result: Mapping[str, Any]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "DESK-BT-001.json"
    md_path = out_dir / "DESK-BT-001.md"
    if json_path.exists() or md_path.exists():
        raise Refused(
            f"{json_path} or {md_path} already exists -- one scored run, never overwritten"
        )
    json_path.write_text(
        json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8"
    )
    md_path.write_text(render_markdown(result), encoding="utf-8")


def render_markdown(result: Mapping[str, Any]) -> str:
    stamp = result["stamp"]
    ft = stamp["first_twenty_chain_sessions"]
    scan = stamp["scan"]
    h = stamp["haircuts"]
    lines = [
        f"# {STUDY} ({MODELED_LABEL})",
        "",
        f"Generated {stamp['generated_at']} | code commit `{stamp.get('code_commit')}`"
        f" | scored: {stamp['scored']}",
        "",
        "## Variant cells (every cell reported, pass or fail)",
        "",
        "| variant | T | mean r @h | mean r @2h | (b) k / t / p | (c) DSR | verdict |",
        "|---|---:|---:|---:|---|---:|---|",
    ]

    def _fmt(x: float | None, n: int = 4) -> str:
        return "-" if x is None else str(round(x, n))

    for key, cell in sorted(result["variants"].items()):
        b = cell.get("b_detail") or {}
        c = cell.get("c_detail") or {}
        lines.append(
            f"| {key} | {cell['T']} | {_fmt(cell.get('mean_r_base'))}"
            f" | {_fmt(cell.get('mean_r_stress'))}"
            f" | {b.get('k', '-')} / {_fmt(b.get('t'))} / {_fmt(b.get('p'))}"
            f" | {_fmt(c.get('DSR'))} | {cell['verdict']} |"
        )
    lines += ["", "## Signal verdicts", ""]
    lines += [f"- **{sig}: {v}**" for sig, v in sorted(result["signal_verdicts"].items())]
    lines += [
        "",
        "## Provenance",
        "",
        f"- spec sha256 `{SPEC_SHA256}`; amendment 1 `{AMENDMENT_1_SHA256}`;"
        f" clarifications `{CLARIFICATIONS_SHA256}`",
        f"- first 20 chain sessions {ft[0]}..{ft[-1]}; manifests sha256"
        f" `{stamp['chain_manifests_sha256'][:16]}...`",
        f"- scan: {scan['input_files']} files, digest `{scan['input_digest'][:16]}...`,"
        f" max bar session {scan['max_bar_session']}",
        f"- haircuts: {len(h['h'])} names measured, pooled fallback for"
        f" {h['pooled_names'] or 'none'}",
        f"- panel sha256 `{stamp['inputs_sha256']['ohlc-panel.json']}` (consumed slice"
        f" `{stamp['inputs_sha256']['ohlc-panel.consumed_slice'][:16]}...`)",
    ]
    lines += ["", "## Attempts by status", ""]
    lines += [f"- {k}: {v}" for k, v in sorted(result["attempts_by_status"].items())]
    lines += [
        "",
        "Every output is MODELED; nothing here promotes, sizes or enables a row.",
        "",
    ]
    return "\n".join(lines)


def scan_sealed_cache(cache_dir: Path, cal: Calendar) -> ivhist.CacheScan:
    """The A5 scan: massive-cache ONLY, the sealed IVHIST window, monthly
    expiries, reader DTE bounds 7..60, the 29 bar-names."""
    return ivhist.scan_cache(
        cache_dir,
        BAR_NAMES,
        SCAN_START,
        SCAN_END,
        cal,
        min_dte=SCAN_MIN_DTE,
        max_dte=SCAN_MAX_DTE,
        monthly_only=True,
    )
