"""Forward Massive minute-bar corpus at the run's decision clocks.

WHY THIS EXISTS
---------------
``desk/cost.py``'s provenance block names the gap: the desk's chains are
CBOE **end-of-day** snapshots (published once overnight, byte-stable during
RTH), so nothing on disk can describe a 10:00/10:15/15:15 ET fill. The one
intraday source the repo already trusts is Massive per-contract minute
aggregates -- the 1.12M-bar longrun bundle was built from them. This module
accumulates a FORWARD corpus: after every session's close, the minute bars
of the currently selected desk-universe contracts are captured into
``<repo>/artifacts/desk-forward-minutes`` (sibling of the existing
``desk-longdated-capture``), through the same content-addressed Massive
cache the desk captures share.

Three moving parts, each independently testable:

* :func:`refresh_selection` re-selects the measured universe (IWM/QQQ/SPY
  rows with ``7 <= dte <= 60``, ``|delta| <= 0.70``, volume > 0,
  open interest > 0, monthly third-Friday expiries) from the latest
  recorded CBOE chain, bounded to ``strikes_per_group`` nearest-the-money
  contracts per (underlying, expiry, right) and a hard ``max_contracts``
  cap. Weekly cadence (the Saturday 09:00 ET slot, after ``desk-chain``'s
  06:30 Saturday recording) keeps the corpus tracking the universe as
  deltas and dtes age; write-once per ``selected_on`` date.
* :class:`WireBudget` is the daily request guard: a per-ET-date ledger
  under ``budget/``, a headroom rule (a request is only made when the
  retry budget still fits under the cap), stop-on-exceed with every
  refusal logged. Cache hits cost nothing and are not ledgered.
* :func:`verify_session` is the coverage proof: for every captured
  contract, a minute bar must land inside each decision clock's minute
  (``10:00``, ``10:15``, ``15:15`` ET -- exactly ``outcomes.py``'s
  ``decision_clocks_et``; a test pins that equality).

Calendar-day distances are epoch arithmetic; weekday/monthly-expiry logic
is imported from ``time/`` (the AST lint bans it elsewhere). Nothing here
authorizes execution: every document says ``execution_authorized: False``.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any

from tree_options.data.massive_client import (
    MassiveClient,
    ResponseCache,
    cache_key_for,
    client_from_environment,
    loads_exact,
)
from tree_options.desk import paths, store
from tree_options.desk.intraday_action_graph import parse_contract
from tree_options.desk.sessions import calendar_days_between
from tree_options.time.monthlies import is_monthly_expiry
from tree_options.trex.clock import ET

SOURCE = "polygon /v2/aggs (Massive) minute aggregates"

#: the measured universe's underlyings and row filters (desk/cost.py: IWM/QQQ/SPY,
#: 7 <= dte <= 60, volume > 0, oi > 0, |delta| <= 0.70; n = 18,783 of 612,371)
UNDERLYINGS = ("IWM", "QQQ", "SPY")
DELTA_ABS_MAX = Decimal("0.70")
DTE_MIN, DTE_MAX = 7, 60
STRIKES_PER_GROUP = 3
MAX_CONTRACTS = 60
#: a selection older than this (calendar days, epoch math) no longer describes
#: the universe: the capture refuses to run until `forward-select` re-selects
MAX_SELECTION_AGE_DAYS = 10.0
DAILY_BUDGET_DEFAULT = 120

#: == outcomes.py's decision_clocks_et (pinned by test to force a revisit
#: if the run's clocks ever move)
DECISION_CLOCKS = ("10:00", "10:15", "15:15")

SELECTION_SCHEMA = "desk-forward-selection/1"
BARS_SCHEMA = "desk-forward-minute-bars/1"
BUDGET_SCHEMA = "desk-forward-wire-budget/1"
VERIFY_SCHEMA = "desk-forward-verify/1"

# fixed exit codes beyond the desk's shared 0/1/2/3 convention
STALE_SELECTION_EXIT = 4  # no usable selection: run forward-select
BUDGET_EXIT = 5  # the daily wire budget stopped the capture before anything landed
COVERAGE_EXIT = 6  # captured, but a decision clock has no bar (or no bars at all)

# the shipped schedule (deploy/desk/desk-forward-*.timer must match exactly;
# tests/unit/test_desk_forward_minutes.py fails if either side drifts)
SELECT_COMMAND = "forward-select"
CAPTURE_COMMAND = "forward-minutes"
SELECT_SLOT = time(9, 0)  # Sat, after desk-chain's 06:30 Saturday recording
CAPTURE_SLOT = time(17, 25)  # Mon-Fri evening, after the 16:15 ET options close
CAPTURE_CATCHUP_SLOT = time(6, 50)  # Mon-Sat morning second chance
CAPTURE_DAYS = frozenset(range(5))  # Mon..Fri
CAPTURE_CATCHUP_DAYS = frozenset(range(6))  # Mon..Sat

AGGS_PARAMS = {"adjusted": "true", "sort": "asc", "limit": 50000}


class SelectionError(RuntimeError):
    """No recorded chain session can source a selection (fixed text, no paths)."""


def forward_root() -> Path:
    return paths.forward_dir()


def default_massive_cache() -> Path:
    """The desk captures' shared content-addressed Massive cache."""
    return paths.repo_root() / "artifacts" / "massive-cache-desk"


# ---------------------------------------------------------------- paths


def selection_path(selected_on: date, root: Path | None = None) -> Path:
    return (root or forward_root()) / "selection" / f"{selected_on}.json"


def bars_path(session: date, root: Path | None = None) -> Path:
    return (root or forward_root()) / "bars" / f"{session}.json"


def verdict_path(session: date, root: Path | None = None) -> Path:
    return (root or forward_root()) / "verify" / f"{session}.json"


def budget_path(day: date, root: Path | None = None) -> Path:
    return (root or forward_root()) / "budget" / f"{day}.json"


def latest_selection(root: Path | None = None) -> dict[str, Any] | None:
    """The newest selection file at or before today (``None`` if there is none)."""
    directory = (root or forward_root()) / "selection"
    if not directory.is_dir():
        return None
    best: date | None = None
    for entry in directory.iterdir():
        try:
            d = date.fromisoformat(entry.stem)
        except ValueError:
            continue
        if best is None or d > best:
            best = d
    if best is None:
        return None
    return json.loads((directory / f"{best}.json").read_text())


# ---------------------------------------------------------------- selection


def _chain_session_for(selected_on: date, underlyings: tuple[str, ...]) -> date:
    """The latest recorded chain session at or before ``selected_on`` that has
    every underlying's chain document. Fails closed, naming what is missing."""
    chains = paths.store_root() / "chains"
    candidates: list[tuple[date, Path, list[str]]] = []
    if chains.is_dir():
        for entry in chains.iterdir():
            try:
                d = date.fromisoformat(entry.name)
            except ValueError:
                continue
            if d <= selected_on and entry.is_dir():
                candidates.append((d, entry, []))
    for _d, entry, missing in candidates:
        missing.extend(u for u in underlyings if not (entry / f"{u}.json.gz").is_file())
    complete = [c for c in candidates if not c[2]]
    if complete:
        return max(complete, key=lambda c: c[0])[0]
    if candidates:
        d, _, missing = max(candidates, key=lambda c: c[0])
        raise SelectionError(
            f"no chain session at or before {d} records all of "
            f"{', '.join(underlyings)} (missing {', '.join(missing)})"
        )
    raise SelectionError(f"no recorded chain session at or before {selected_on}")


def _measured_universe_rows(
    rows: list[dict[str, Any]], selected_on: date
) -> list[tuple[dict[str, Any], int]]:
    """The rows that pass the measured-universe filters, with their dte."""
    kept: list[tuple[dict[str, Any], int]] = []
    for row in rows:
        delta = row["delta"]
        if not isinstance(delta, (int, float)) or isinstance(delta, bool):
            continue
        if abs(delta) > float(DELTA_ABS_MAX):
            continue
        dte = calendar_days_between(selected_on.isoformat(), row["exp"].isoformat())
        if not DTE_MIN <= dte <= DTE_MAX:
            continue
        if not is_monthly_expiry(row["exp"]):
            continue
        if not (isinstance(row["volume"], (int, float)) and row["volume"] > 0):
            continue
        if not (isinstance(row["oi"], (int, float)) and row["oi"] > 0):
            continue
        kept.append((row, int(dte)))
    return kept


def refresh_selection(
    *,
    selected_on: date,
    underlyings: tuple[str, ...] = UNDERLYINGS,
    strikes_per_group: int = STRIKES_PER_GROUP,
    max_contracts: int = MAX_CONTRACTS,
    root: Path | None = None,
) -> dict[str, Any]:
    """Re-select the forward corpus's contracts from the latest recorded chain.

    Write-once per ``selected_on``: a second call the same day returns the
    stored document with ``status == "exists"`` and changes nothing.
    """
    target = selection_path(selected_on, root)
    if target.exists():
        doc = json.loads(target.read_text())
        doc["status"] = "exists"
        return doc
    if strikes_per_group < 1 or max_contracts < 1:
        raise ValueError("strikes_per_group and max_contracts must be positive")
    chain_session = _chain_session_for(selected_on, underlyings)
    chains_dir = paths.store_root() / "chains" / chain_session.isoformat()
    contracts: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    source_files: dict[str, str] = {}
    grouped: dict[tuple[date, str, str], tuple[Decimal, list[tuple[dict[str, Any], int]]]] = {}
    for sym in underlyings:
        doc = store.read_chain(chains_dir / f"{sym}.json.gz")
        if doc["header"].get("underlying") != sym:
            raise SelectionError(f"chain identity mismatch for {sym}")
        source_files[f"{sym}.json.gz"] = str(doc["header"].get("raw_sha256", ""))
        spot = Decimal(str(doc["header"].get("underlying_quote", {}).get("current_price")))
        rows = [
            {
                "occ": doc["columns"]["occ"][i],
                "exp": date.fromisoformat(doc["columns"]["exp"][i]),
                "right": doc["columns"]["right"][i],
                "strike": Decimal(str(doc["columns"]["strike"][i])),
                "delta": doc["columns"]["delta"][i],
                "volume": doc["columns"]["volume"][i],
                "oi": doc["columns"]["oi"][i],
            }
            for i in range(len(doc["columns"]["occ"]))
        ]
        for row, dte in _measured_universe_rows(rows, selected_on):
            key = (row["exp"], sym, row["right"])
            if key not in grouped:
                grouped[key] = (spot, [])
            grouped[key][1].append((row, dte))
    # nearest expiries first (the corpus always covers the front monthly), the
    # symbol order UNDERLYINGS defines within an expiry
    for (expiry, sym, right) in sorted(grouped, key=lambda k: (k[0], underlyings.index(k[1]), k[2])):
        spot, members = grouped[(expiry, sym, right)]
        picked = sorted(members, key=lambda m: (abs(m[0]["strike"] - spot), m[0]["strike"])
                        )[:strikes_per_group]
        if len(contracts) + len(picked) > max_contracts:
            skipped.append({"key": f"{sym}/{expiry.isoformat()}/{right}",
                            "reason": "cap", "size": len(picked)})
            continue
        for row, dte in sorted(picked, key=lambda m: m[0]["strike"]):
            ticker = f"O:{row['occ']}"
            try:
                parsed = parse_contract(ticker)
            except ValueError:
                continue  # an OCC the repo cannot re-parse is not corpus material
            contracts.append({
                "ticker": ticker,
                "underlying": sym,
                "expiry": expiry.isoformat(),
                "right": right,
                "strike": float(parsed.strike),
                "delta_at_selection": row["delta"],
                "dte_at_selection": dte,
            })
    report = {
        "schema": SELECTION_SCHEMA,
        "selected_on": selected_on.isoformat(),
        "chain_session": chain_session.isoformat(),
        "underlyings": list(underlyings),
        "filters": {
            "delta_abs_max": str(DELTA_ABS_MAX),
            "dte_min": DTE_MIN,
            "dte_max": DTE_MAX,
            "monthly_expiry_only": True,
            "min_volume": 1,
            "min_open_interest": 1,
            "strikes_per_group": strikes_per_group,
        },
        "source_files": source_files,
        "source_sha256": hashlib.sha256(
            json.dumps(source_files, sort_keys=True).encode()
        ).hexdigest(),
        "contracts": contracts,
        "tickers": sorted({c["ticker"] for c in contracts}),
        "skipped_groups": skipped,
        "max_selection_age_days": MAX_SELECTION_AGE_DAYS,
        "status": "written",
        "execution_authorized": False,
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    if not store.atomic_create_bytes(
        target, (json.dumps(report, indent=1, sort_keys=True) + "\n").encode()
    ):
        doc = json.loads(target.read_text())
        doc["status"] = "exists"
        return doc
    return report


# ---------------------------------------------------------------- budget


@dataclass
class WireBudget:
    """The daily wire guard: one append-only ledger per ET date.

    ``allow(n)`` is the headroom rule -- a wire call may start only when the
    retry budget still fits under the cap; ``record`` books the requests the
    call actually spent (cache hits book nothing); ``refuse`` logs a refusal
    without moving ``spent``.
    """

    path: Path
    cap: int
    spent: int = 0
    entries: list[dict[str, Any]] = field(default_factory=list)
    refusals: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path, *, cap: int) -> WireBudget:
        if path.exists():
            doc = json.loads(path.read_text())
            if doc.get("schema") != BUDGET_SCHEMA:
                raise ValueError(f"{path.name}: not a wire-budget ledger")
            return cls(path, cap, int(doc["spent"]), list(doc.get("entries", [])),
                       list(doc.get("refusals", [])))
        return cls(path, cap)

    def _flush(self) -> None:
        doc = {
            "schema": BUDGET_SCHEMA,
            "date": self.path.stem,
            "cap": self.cap,
            "spent": self.spent,
            "entries": self.entries,
            "refusals": self.refusals,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        store.atomic_write_json(self.path, doc)

    def allow(self, requests: int) -> bool:
        return self.spent + requests <= self.cap

    def record(self, label: str, requests: int) -> None:
        self.entries.append({"label": label, "requests": requests})
        self.spent += requests
        self._flush()

    def refuse(self, label: str, wanted: int) -> None:
        self.refusals.append({"label": label, "wanted": wanted, "cap": self.cap,
                              "spent": self.spent})
        self._flush()


# ---------------------------------------------------------------- capture


@dataclass
class CaptureResult:
    session: date
    status: str  # written | exists | stale
    exit_code: int
    wire_requests: int = 0
    captured: int = 0
    invalid: int = 0
    budget_refused: int = 0
    note: str = ""

    def line(self) -> str:
        return (f"forward-minutes: {self.session} {self.status} "
                f"ok={self.captured} invalid={self.invalid} "
                f"budget={self.budget_refused} wire={self.wire_requests}"
                + (f" ({self.note})" if self.note else ""))


def _validate_bars(ticker: str, body: dict[str, Any]) -> list[dict[str, Any]]:
    """The same contract capture_desk_option_minutes holds the vendor to, plus
    a normalization step: cached bodies decode through ``loads_exact`` (every
    number a ``Decimal``) while wire bodies decode as floats, so each bar is
    rebuilt as plain JSON scalars (``t`` int ms, prices float) and the stored
    document is identical whichever path produced it."""
    if body.get("status") not in ("OK", "DELAYED") or body.get("ticker") != ticker:
        raise ValueError("status/ticker")
    raw = body.get("results", [])
    if (body.get("next_url") or len(raw) >= AGGS_PARAMS["limit"]
            or body.get("resultsCount", len(raw)) != len(raw)):
        raise ValueError("truncated")
    bars: list[dict[str, Any]] = []
    for b in raw:
        t = b.get("t")
        if not isinstance(t, int) or isinstance(t, bool):
            raise ValueError("bar shape")
        try:
            v = float(b["v"])
            bar = {"t": t, "o": float(b["o"]), "h": float(b["h"]),
                   "l": float(b["l"]), "c": float(b["c"]), "v": v}
        except (KeyError, TypeError, ValueError):
            raise ValueError("bar shape") from None
        if v <= 0:
            raise ValueError("bar shape")
        bars.append(bar)
    return bars


def capture_session(
    session: date,
    *,
    selection: dict[str, Any],
    client: MassiveClient,
    budget: WireBudget,
    now: datetime,
    root: Path | None = None,
    dry_run: bool = False,
) -> CaptureResult:
    """Capture ``session``'s minute bars for the selection's contracts.

    Write-once: an existing ``bars/<session>.json`` short-circuits to
    ``status == "exists"`` without touching the wire. A selection that
    postdates the session, or has aged past ``MAX_SELECTION_AGE_DAYS``,
    refuses before any request (exit :data:`STALE_SELECTION_EXIT`).
    ``dry_run`` is cache-only: no wire request is made or budgeted, and
    nothing is written (the report says what a real run would find).
    """
    age = calendar_days_between(selection["selected_on"], session.isoformat())
    chain_session = date.fromisoformat(selection["chain_session"])
    if not 0 <= age <= MAX_SELECTION_AGE_DAYS or chain_session >= session:
        return CaptureResult(
            session, "stale", STALE_SELECTION_EXIT,
            note=f"selection {selection['selected_on']} is {age:g} days from the "
                 f"session (chain {selection['chain_session']}); run forward-select",
        )
    target = bars_path(session, root)
    if target.exists():
        return CaptureResult(session, "exists", 0)
    cache: ResponseCache | None = client.cache
    headroom = client.backoff.max_attempts
    wire = 0
    entries: dict[str, dict[str, Any]] = {}
    for contract in selection["contracts"]:
        ticker = contract["ticker"]
        path = f"/v2/aggs/ticker/{ticker}/range/1/minute/{session}/{session}"
        key = cache_key_for(path, AGGS_PARAMS)
        body: dict[str, Any] | None = None
        cached = cache.get(key) if cache is not None else None
        if cached is not None:
            try:
                body = loads_exact(cached)
            except Exception:  # a corrupt entry self-heals through the wire below
                body = None
                if cache is not None:
                    cache.discard(key)
        if body is None:
            if dry_run or not budget.allow(headroom):
                if not dry_run:
                    budget.refuse(ticker, headroom)
                entries[ticker] = {**contract, "status": "budget", "bars": []}
                continue
            before = client.stats.requests
            body = client.get_json(path, AGGS_PARAMS)
            spent = client.stats.requests - before
            wire += spent
            budget.record(ticker, spent)
        try:
            bars = _validate_bars(ticker, body)
        except ValueError as exc:
            entries[ticker] = {**contract, "status": "invalid",
                               "reason": str(exc), "bars": []}
            continue
        entries[ticker] = {
            **contract,
            "status": "ok",
            "dte_at_session": int(calendar_days_between(
                session.isoformat(), contract["expiry"])),
            "n_bars": len(bars),
            "bars": bars,
        }
    counts = {s: sum(1 for e in entries.values() if e["status"] == s)
              for s in ("ok", "invalid", "budget")}
    refused = counts["budget"]
    if dry_run:
        return CaptureResult(session, "dry-run", 0, 0, counts["ok"],
                             counts["invalid"], refused,
                             note=f"cache-only: {refused} not in cache")
    if counts["ok"]:
        exit_code = 0
    elif refused:
        exit_code = BUDGET_EXIT
    else:
        exit_code = 1
    doc = {
        "schema": BARS_SCHEMA,
        "source": SOURCE,
        "session": session.isoformat(),
        "selected_on": selection["selected_on"],
        "chain_session": selection["chain_session"],
        "selection_source_sha256": selection["source_sha256"],
        "captured_at": now.isoformat(),
        "wire_requests": wire,
        "budget": {"date": budget.path.stem, "cap": budget.cap, "spent": budget.spent},
        "contracts": entries,
        "counts": counts,
        "execution_authorized": False,
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    store.atomic_write_json(target, doc)
    return CaptureResult(session, "written", exit_code, wire, counts["ok"],
                         counts["invalid"], refused)


# ---------------------------------------------------------------- verify


@dataclass
class Verdict:
    session: date
    exit_code: int
    ok: bool
    covered_contracts: int = 0
    no_trade: int = 0
    note: str = ""

    def line(self) -> str:
        return (f"forward-verify: {self.session} ok={self.ok} "
                f"covered={self.covered_contracts} no_trade={self.no_trade}"
                + (f" ({self.note})" if self.note else ""))


def _clock_ms(session: date, clock: str) -> int:
    hour, minute = (int(x) for x in clock.split(":"))
    return int(datetime.combine(session, time(hour, minute), tzinfo=ET).timestamp() * 1000)


def _clock_covered(bars: list[dict[str, Any]], session: date, clock: str) -> bool:
    """A bar's minute [clock, clock+60s) ET (DST-correct via zoneinfo)."""
    lo = _clock_ms(session, clock)
    return any(lo <= b["t"] < lo + 60_000 for b in bars)


def verify_session(
    session: date, *, root: Path | None = None, now: datetime | None = None
) -> Verdict:
    """Decision-clock coverage of a captured session (no wire, idempotent)."""
    source = bars_path(session, root)
    if not source.exists():
        return Verdict(session, COVERAGE_EXIT, False,
                       note="no bars document for the session")
    raw = source.read_bytes()
    doc = json.loads(raw)
    contracts: dict[str, dict[str, Any]] = {}
    no_trade: list[str] = []
    covered = 0
    for ticker, entry in doc.get("contracts", {}).items():
        if entry.get("status") != "ok":
            continue
        bars = entry.get("bars", [])
        if not bars:
            no_trade.append(ticker)
            continue
        missing = [c for c in DECISION_CLOCKS
                   if not _clock_covered(bars, session, c)]
        contracts[ticker] = {"n_bars": len(bars),
                             "covered": [c for c in DECISION_CLOCKS if c not in missing],
                             "missing": missing}
        covered += not missing
    ok = bool(contracts) and covered == len(contracts)
    verdict = {
        "schema": VERIFY_SCHEMA,
        "session": session.isoformat(),
        "clocks_et": list(DECISION_CLOCKS),
        "bars_sha256": hashlib.sha256(raw).hexdigest(),
        "selected_on": doc.get("selected_on"),
        "checked_at": (now or datetime.now(ET)).isoformat(),
        "contracts": contracts,
        "no_trade": sorted(no_trade),
        "covered_contracts": covered,
        "ok": ok,
        "execution_authorized": False,
    }
    out = verdict_path(session, root)
    out.parent.mkdir(parents=True, exist_ok=True)
    store.atomic_write_json(out, verdict)
    return Verdict(session, 0 if ok else COVERAGE_EXIT, ok, covered, len(no_trade))


# ---------------------------------------------------------------- cli


def build_client(cache_dir: Path) -> MassiveClient:
    """The free-tier client (5/min governor, bounded backoff), never raising
    key material."""
    return client_from_environment(cache_dir=cache_dir)


__all__ = [
    "BARS_SCHEMA",
    "BUDGET_EXIT",
    "BUDGET_SCHEMA",
    "CAPTURE_CATCHUP_DAYS",
    "CAPTURE_CATCHUP_SLOT",
    "CAPTURE_DAYS",
    "CAPTURE_SLOT",
    "COVERAGE_EXIT",
    "DAILY_BUDGET_DEFAULT",
    "DECISION_CLOCKS",
    "MAX_CONTRACTS",
    "MAX_SELECTION_AGE_DAYS",
    "SELECTION_SCHEMA",
    "SELECT_SLOT",
    "STALE_SELECTION_EXIT",
    "STRIKES_PER_GROUP",
    "CaptureResult",
    "SelectionError",
    "Verdict",
    "WireBudget",
    "bars_path",
    "budget_path",
    "build_client",
    "capture_session",
    "default_massive_cache",
    "forward_root",
    "latest_selection",
    "refresh_selection",
    "selection_path",
    "verdict_path",
    "verify_session",
]
