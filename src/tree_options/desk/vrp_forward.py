"""VRP-COND-FORWARD-001: the forward gate-state emitter (REPORT-ONLY).

Implements the machinery section (§8) of the sealed pre-registration
``docs/desk/VRP-COND-FORWARD-001.md`` (body sha256 ``49c0865e...``, sealed
2026-11-14). Nothing here gates, sizes, pauses, refuses or prices an actual
card; the emitter never edits sealed cards or LEDGER.md, deploys no timer and
no unit, and writes only under ``<TREX_DESK_STATE>/vrp-cond-forward/``.

Per recorded EOD chain session D (§8.1-8.5):

1. ``iv30_IWM(D)`` — ATM 30-calendar-day interpolated IV from D's recorded
   chain snapshot, only standard OCC contracts (:func:`store.standard_occ`),
   through :mod:`tree_options.desk.surface` (``atm_term`` /
   ``constant_maturity``); a session without a usable surface is REFUSED and
   marked (never stale carry, never silent).
2. ``harvol20_IWM(D)`` — the FORECAST-001 pooled log-HAR h=20 forecast
   annualized on the calendar days of the next 20 sessions, strictly
   point-in-time (monthly expanding refits, walk-forward, no future rows) —
   the same machinery ``scripts/campaign/vrp-cond_run.py::build_features``
   used, re-implemented here (the campaign script is never imported).
3. ``r_IWM(D) = iv30/harvol20``, appended to the SPLICED series: the frozen
   vwap_atm-derived observations through 2026-09-03 (the sealed vintage
   break; the two estimators share zero sessions), then chain-mid-derived
   observations from 2026-09-22.
4. p under the PRIMARY pool and both sensitivity pools (§3.4), with n,
   status and refusal marks per pool.
5. ONE JSON per session into ``vrp-cond-forward/gates/<D>.json``, write-once:
   a re-run with the same builder sha is a no-op, a different builder sha
   refuses to rewrite history (the builder sha applies forward-only).
6. per recorded XSMOM entry: the ``xe`` gate state at t and the ``xp``
   percentile at t-1 plus the dictated size, appended to
   ``vrp-cond-forward/card-events.jsonl`` (state area only).

The frozen base is DERIVED ONCE into ``vrp-cond-forward/base-r.json`` and its
sealed anchors (§3.3) are enforced at derivation: a base that does not
reproduce them refuses everything downstream. Constants are never tuned to
fit — a mismatch means a wrong calendar/HAR/IV-window upstream and is a hard
stop (load-bearing rule of the registration).
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from tree_options.desk import har, ivhist, paths, surface
from tree_options.desk.ivhist import RateSource
from tree_options.desk.panel import read_panel_with_sha256
from tree_options.desk.sessions import Calendar
from tree_options.desk.store import atomic_create_bytes, standard_occ
from tree_options.time.calendar import SessionCalendar, calendar_content_sha256

# ---- the sealed registration, pinned (VRP-COND-FORWARD-001 §3.4/§3.5/§8) ----

STUDY = "VRP-COND-FORWARD-001"
BASE_SCHEMA = "vrp-cond-forward-base-r/1"
GATE_SCHEMA = "vrp-cond-forward-gate/1"
CARD_EVENT_SCHEMA = "vrp-cond-forward-card-event/1"
STATE_DIR = "vrp-cond-forward"
GATES_DIR = "gates"
BASE_R_FILE = "base-r.json"
CARD_EVENTS_FILE = "card-events.jsonl"

NAME = "IWM"
IVHIST_FILE = "iv-history/vwap_atm.json"
# the frozen splice base (§3.2): the sealed IVHIST-001 history, sha-pinned
IVHIST_SHA256 = "b7305fd9dd229f26254cc10948608b53bd7e5f6b797b6a59dc9ab65d99b160d4"
FROZEN_LAST = date(2026, 9, 3)  # the frozen vintage's last session
FORWARD_FIRST = date(2026, 9, 22)  # the first recorded-chain session (the break)

HOLD_SESSIONS = 20  # the r denominator's horizon (desk VRP convention)
HAR_START = date(2024, 9, 3)  # FORECAST-001: monthly refits from 2024-09's first session
PCT_WINDOW = 252  # §3.4: trailing window, window-inclusive
PCT_MIN_N = 126  # §3.4: minimum evaluable pool
TER_HI = 2.0 / 3.0  # threshold semantics as registered: ON iff p >= 2/3
POOL_BAND = 0.17  # §3.4: pre-committed pool-choice sensitivity band
WARMUP_MIN_FORWARD = 50  # §3.5: no card conditioned before 50 forward r-obs
REGISTRATION_DATE = date(2026, 11, 14)  # §4: the commit that added the prereg

# the frozen-base acceptance anchors (§3.3, measured by RT-5; n is exact, the
# stats are the sealed 3-decimal prints)
FROZEN_ANCHORS: dict[str, float] = {
    "n": 452,
    "mean": 0.973,
    "p25": 0.903,
    "p50": 0.966,
    "p75": 1.030,
    "p90": 1.103,
}

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_BAD_ARGS = 2
EXIT_NOT_RECORDED = 3


class EmitterRefusal(RuntimeError):
    """A binding refusal (anchor mismatch, history rewrite, bad state)."""


def builder_sha256() -> str:
    """The emitter's own sha256 (§8.5: one sha-pinned builder)."""
    return hashlib.sha256(Path(__file__).resolve().read_bytes()).hexdigest()


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _utcnow() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------- state paths


def state_dir(state_root: Path) -> Path:
    return state_root / STATE_DIR


def gates_dir(state_root: Path) -> Path:
    return state_dir(state_root) / GATES_DIR


def base_r_path(state_root: Path) -> Path:
    return state_dir(state_root) / BASE_R_FILE


def card_events_path(state_root: Path) -> Path:
    return state_dir(state_root) / CARD_EVENTS_FILE


def gate_path(state_root: Path, session: date) -> Path:
    return gates_dir(state_root) / f"{session.isoformat()}.json"


# ------------------------------------------------------------------ the legs


@dataclass(frozen=True)
class Inputs:
    """Every read-only input the emitter consumes, with its sha256."""

    cal: SessionCalendar
    panel: Mapping[str, Mapping[str, Mapping[str, Any]]]
    panel_sha256: str
    earnings: Mapping[str, Sequence[str]]
    earnings_sha256: str
    ivhist: Mapping[str, Any]
    ivhist_sha256: str
    rates: RateSource
    rate_label: str
    calendar_sha256: str


def load_inputs(cal: SessionCalendar) -> Inputs:
    """Read the panel (under its shared lock), the sealed earnings calendar,
    the sha-pinned IVHIST history and the DTB3 rate source. Any missing input
    refuses (the emitter never improvises a proxy)."""
    paper = paths.paper_dir()
    panel_path = paper / "ohlc-panel.json"
    panel, panel_sha = read_panel_with_sha256(panel_path)  # raises on lock/IO
    earnings_path = paper / "earnings-calendar.json"
    earnings = json.loads(earnings_path.read_text(encoding="utf-8"))
    if not isinstance(earnings, dict):
        raise EmitterRefusal(f"{earnings_path}: the earnings calendar is not an object")
    ivhist_path = paths.store_root() / IVHIST_FILE
    iv_sha = _sha256_file(ivhist_path)
    if iv_sha != IVHIST_SHA256:
        raise EmitterRefusal(
            f"{IVHIST_FILE}: sha256 {iv_sha} is not the sealed frozen base "
            f"{IVHIST_SHA256} -- the splice base is FROZEN (VRP-COND-FORWARD-001 §3.2)"
        )
    iv_doc = json.loads(ivhist_path.read_text(encoding="utf-8"))
    rates, warn = ivhist.rate_source_for_store(paths.store_root())
    return Inputs(
        cal=cal,
        panel=panel,
        panel_sha256=panel_sha,
        earnings=earnings,
        earnings_sha256=_sha256_file(earnings_path),
        ivhist=iv_doc,
        ivhist_sha256=iv_sha,
        rates=rates,
        rate_label=warn or rates.label,
        calendar_sha256=calendar_content_sha256(cal),
    )


def harvol20_by_session(inputs: Inputs) -> dict[date, tuple[float, dict[str, Any]]]:
    """``harvol20_IWM(D)`` per session: the pooled log-HAR h=20 walk-forward
    forecast (FORECAST-001 machinery, strictly PIT), annualized on the
    calendar days to the session 20 NYSE sessions ahead — the exact
    denominator ``vrp-cond_run.build_features`` computed, re-implemented.

    The pooled fit runs over the REGISTERED 35 names (the frozen IVHIST
    history's own name set — the sealed round's universe; widening is a
    successor registration, not this emitter's call)."""
    names = sorted(inputs.ivhist.get("names", {}))
    if NAME not in names:
        raise EmitterRefusal("the frozen IV history carries no IWM — the fidelity split moved")
    data = har.build_har_data(inputs.panel, inputs.earnings, inputs.cal, names)
    wf = har.walk_forward(data, HOLD_SESSIONS, start=HAR_START)
    out: dict[date, tuple[float, dict[str, Any]]] = {}
    for t, f in sorted(wf.forecasts.get(NAME, {}).items()):
        session = data.sessions[t]
        end = inputs.cal.nth_after(session, HOLD_SESSIONS)
        days = har.calendar_days_between(session.isoformat(), end.isoformat())
        if days <= 0:
            continue
        harvol = math.sqrt(f * 365.0 / days)
        if harvol > 0.0:
            out[session] = (
                harvol,
                {
                    "har_var": f,
                    "window_calendar_days": days,
                    "window_end": end.isoformat(),
                    "h": HOLD_SESSIONS,
                    "fit_names": len(names),
                    "machinery": "tree_options.desk.har walk_forward (FORECAST-001)",
                },
            )
    return out


def chain_iv30(
    chain_path: Path, session: date, rates: RateSource
) -> tuple[float | None, dict[str, Any]]:
    """``iv30_IWM(D)`` from the recorded EOD chain snapshot: only standard
    OCC contracts (``store.standard_occ``), mid-inverted IVs, ATM per expiry
    via ``surface.atm_term``, 30 days by total-variance interpolation
    (``surface.constant_maturity``). (None, meta) with a reason when the
    session has no usable surface."""
    meta: dict[str, Any] = {
        "chain_file": str(chain_path),
        "chain_file_sha256": _sha256_file(chain_path),
        "iv30_method": (
            "chain mid ATM 30d: standard_occ filter, surface.atm_term/constant_maturity"
        ),
    }
    doc = surface.read_chain_file(chain_path)
    meta["chain_raw_sha256"] = doc.get("header", {}).get("raw_sha256")
    spot = surface.chain_spot(doc)
    if spot is None:
        return None, {**meta, "reason": "chain has no underlying close"}
    rate = rates.rate_on(session)
    if rate is None:
        rate = ivhist.DEFAULT_RATE  # the desk features convention (warned there)
        meta["rate_fallback"] = True
    std = standard_occ(NAME)
    cols = doc["columns"]
    keep = [i for i, occ in enumerate(cols["occ"]) if std.fullmatch(occ)]
    meta["rows_standard"] = len(keep)
    meta["rows_nonstandard"] = len(cols["occ"]) - len(keep)
    filtered = {"header": doc["header"], "columns": {k: [cols[k][i] for i in keep] for k in cols}}
    quotes = surface.chain_quotes(filtered, session=session, spot=spot, rate=rate)
    term = surface.atm_term(quotes, spot=spot, rate=rate)
    iv30 = surface.constant_maturity(term)[30]
    meta["rate"] = rate
    if iv30 is None:
        return None, {**meta, "reason": "no ATM 30d IV on this chain (no bracketing expiry)"}
    meta.pop("reason", None)
    return iv30, meta


# ------------------------------------------------------------ the frozen base


def frozen_base_series(inputs: Inputs) -> dict[date, float]:
    """The 452-observation frozen IWM r-series, exactly as the sealed round's
    ``build_features`` built it: vwap_atm iv30 over the pooled-HAR walk-forward,
    restricted to the frozen window's sessions."""
    iv_sessions = inputs.ivhist["names"][NAME]["sessions"]
    out: dict[date, float] = {}
    for session, (harvol, _meta) in harvol20_by_session(inputs).items():
        if session > FROZEN_LAST:
            continue
        rec = iv_sessions.get(session.isoformat())
        if not isinstance(rec, dict) or "iv30" not in rec:
            continue  # no evaluable iv30 on this session
        out[session] = rec["iv30"] / harvol
    return out


def _q(sorted_values: Sequence[float], p: float) -> float:
    """Nearest-rank quantile on the sorted values (RT-5's anchor convention)."""
    i = min(len(sorted_values) - 1, max(0, round(p * (len(sorted_values) - 1))))
    return sorted_values[i]


def anchor_report(values: Mapping[date, float]) -> dict[str, Any]:
    """Compare the derived frozen base against the sealed anchors (§3.3).
    n must match exactly; every stat must round to its sealed 3-decimal
    print. Constants are never tuned: a fail is a hard stop."""
    sorted_v = sorted(values.values())
    n = len(sorted_v)
    mean = sum(sorted_v) / n if n else float("nan")
    observed = {
        "n": n,
        "mean": mean,
        "p25": _q(sorted_v, 0.25),
        "p50": _q(sorted_v, 0.50),
        "p75": _q(sorted_v, 0.75),
        "p90": _q(sorted_v, 0.90),
    }
    mismatches: list[str] = []
    for key, sealed in FROZEN_ANCHORS.items():
        got = observed[key]
        if key == "n":
            if got != sealed:
                mismatches.append(f"n={got} != {sealed:.0f}")
        elif round(float(got), 3) != sealed:
            mismatches.append(f"{key}={got:.4f} (sealed {sealed:.3f})")
    return {
        "sealed": dict(FROZEN_ANCHORS),
        "observed": {k: (v if k == "n" else round(float(v), 6)) for k, v in observed.items()},
        "pass": not mismatches,
        "mismatches": mismatches,
    }


@dataclass(frozen=True)
class BaseRecord:
    """The once-derived frozen base, as pinned in ``base-r.json``."""

    series: dict[date, float]
    doc: dict[str, Any]


def load_base(inputs: Inputs, state_root: Path, *, now: datetime | None = None) -> BaseRecord:
    """The frozen base DERIVED ONCE: reuse ``base-r.json`` when present
    (whatever builder wrote it — history is never rewritten), else derive,
    enforce the sealed anchors and publish it write-once."""
    path = base_r_path(state_root)
    if path.exists():
        doc = json.loads(path.read_text(encoding="utf-8"))
        if doc.get("schema") != BASE_SCHEMA:
            raise EmitterRefusal(f"{path}: schema {doc.get('schema')!r} is not {BASE_SCHEMA}")
        if not doc.get("anchors", {}).get("pass"):
            raise EmitterRefusal(f"{path}: the recorded base failed its anchors")
        series = {date.fromisoformat(d): float(v) for d, v in doc["series"]}
        return BaseRecord(series=series, doc=doc)
    series = frozen_base_series(inputs)
    report = anchor_report(series)
    doc = {
        "schema": BASE_SCHEMA,
        "study": STUDY,
        "vintage": "frozen: IVHIST-001 VWAP ATM 30d through 2026-09-03",
        "declared_break": [FROZEN_LAST.isoformat(), FORWARD_FIRST.isoformat()],
        "series": [[d.isoformat(), round(v, 10)] for d, v in sorted(series.items())],
        "n": len(series),
        "anchors": report,
        "inputs": {
            "iv_history_sha256": inputs.ivhist_sha256,
            "panel_sha256": inputs.panel_sha256,
            "earnings_calendar_sha256": inputs.earnings_sha256,
            "calendar_content_sha256": inputs.calendar_sha256,
            "har": {
                "machinery": "tree_options.desk.har walk_forward h=20",
                "start": HAR_START.isoformat(),
                "names": sorted(inputs.ivhist.get("names", {})),
            },
        },
        "builder_sha256": builder_sha256(),
        "generated_at": (now or _utcnow()).isoformat(),
    }
    if not report["pass"]:
        raise EmitterRefusal(
            "the frozen-base derivation does not reproduce the sealed anchors "
            f"(VRP-COND-FORWARD-001 §3.3): {'; '.join(report['mismatches'])} "
            "-- refusing to write base-r.json (never tune constants to fit)"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    body = (json.dumps(doc, indent=1, sort_keys=True) + "\n").encode()
    if not atomic_create_bytes(path, body):  # a racing writer got there first
        return load_base(inputs, state_root, now=now)
    return BaseRecord(series=series, doc=doc)


# -------------------------------------------------------------------- pools


def _pool_read(p: float | None, n: int, status: str) -> dict[str, Any]:
    on = (p >= TER_HI) if p is not None else None
    within = (abs(p - TER_HI) <= POOL_BAND) if p is not None else None
    return {"p": p, "n": n, "status": status, "on": on, "within_band": within}


def pool_primary(spliced: Mapping[date, float], at: date, cal: Calendar) -> dict[str, Any]:
    """§3.4 PRIMARY (the verdict binds to this): the registered statistic on
    the SPLICED series — the fraction of strictly-lower evaluable r over the
    trailing 252 sessions ending AT ``at`` (window-inclusive), n >= 126."""
    cur = spliced.get(at)
    if cur is None:
        return _pool_read(None, 0, "no evaluable r at session")
    sessions = cal.sessions()
    i = cal.ordinal(at)
    window = sessions[max(0, i - (PCT_WINDOW - 1)) : i + 1]
    pool = [spliced[s] for s in window if s in spliced]
    n = len(pool)
    if n < PCT_MIN_N:
        return _pool_read(None, n, f"n/a (n={n} < {PCT_MIN_N})")
    return _pool_read(sum(1 for v in pool if v < cur) / n, n, "evaluable")


def pool_mixed_fullbase(
    frozen: Mapping[date, float],
    forward: Mapping[date, float],
    at: date,
) -> dict[str, Any]:
    """§3.4 sensitivity (a): r(at) ranked against all evaluable r in the
    frozen base plus all forward obs to date (window-inclusive, the
    registered statistic's own convention). Never verdict-binding."""
    cur = forward[at] if at in forward else frozen.get(at)
    if cur is None:
        return _pool_read(None, 0, "no evaluable r at session")
    pool = [v for d, v in frozen.items() if d <= FROZEN_LAST]
    pool += [v for d, v in forward.items() if d <= at]
    n = len(pool)
    return _pool_read(sum(1 for v in pool if v < cur) / n, n, "evaluable")


def pool_forward_only(forward: Mapping[date, float], at: date, cal: Calendar) -> dict[str, Any]:
    """§3.4 sensitivity (b): the registered trailing-252 window over
    FORWARD-vintage observations only; ``n/a`` until n >= 126 forward r-obs
    exist (~2027-07+). Never verdict-binding."""
    cur = forward.get(at)
    if cur is None:
        return _pool_read(None, 0, "no evaluable r at session")
    sessions = cal.sessions()
    i = cal.ordinal(at)
    window = sessions[max(0, i - (PCT_WINDOW - 1)) : i + 1]
    pool = [forward[s] for s in window if s in forward]
    n = len(pool)
    if n < PCT_MIN_N:
        return _pool_read(None, n, f"n/a (n={n} < {PCT_MIN_N} forward obs)")
    return _pool_read(sum(1 for v in pool if v < cur) / n, n, "evaluable")


def forward_r_from_gates(state_root: Path) -> dict[date, float]:
    """The emitted forward r record: every ok gate file's r (write-once
    history is the pinned forward vintage — never recomputed)."""
    out: dict[date, float] = {}
    root = gates_dir(state_root)
    if not root.is_dir():
        return out
    for p in sorted(root.glob("*.json")):
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if doc.get("schema") != GATE_SCHEMA or doc.get("status") != "ok":
            continue
        try:
            out[date.fromisoformat(doc["session"])] = float(doc["r"])
        except (KeyError, TypeError, ValueError):
            continue
    return out


# -------------------------------------------------------------------- emit


@dataclass(frozen=True)
class EmitOutcome:
    session: date
    action: str  # written | no-op | conflict | refused
    status: str  # ok | refused | (file states)
    detail: str
    doc: dict[str, Any] | None = None


def _publish_write_once(path: Path, doc: Mapping[str, Any], builder: str) -> tuple[str, str]:
    """§8.5 write-once: same builder sha re-run = no-op; a different builder
    sha refuses to rewrite history (the new sha applies forward-only)."""
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return "conflict", f"existing {path.name} unreadable ({type(exc).__name__})"
        if existing.get("builder_sha256") == builder:
            return "no-op", "same builder sha; the recorded gate stands"
        return (
            "conflict",
            f"existing {path.name} carries builder {str(existing.get('builder_sha256'))[:12]}"
            f" != {builder[:12]} -- history is never rewritten",
        )
    body = (json.dumps(doc, indent=1, sort_keys=True) + "\n").encode()
    if not atomic_create_bytes(path, body):  # a racing writer got there first
        return "no-op", "another writer published it first"
    return "written", ""


def emit_session(
    session: date,
    *,
    inputs: Inputs,
    base: BaseRecord,
    state_root: Path,
    harvol: Mapping[date, tuple[float, dict[str, Any]]],
    forward_prior: Mapping[date, float],
    chain_path: Path,
    now: datetime | None = None,
) -> EmitOutcome:
    """Emit ONE session's gate JSON (§8). A session whose chain exists but
    yields no usable surface is REFUSED and marked — never stale carry, never
    silent."""
    if session < FORWARD_FIRST:
        return EmitOutcome(session, "refused", "refused", "session precedes the declared break")
    builder = builder_sha256()
    iv30, iv_meta = chain_iv30(chain_path, session, inputs.rates)
    hv = harvol.get(session)
    reasons = [iv_meta.get("reason", "")] if iv30 is None else []
    if hv is None:
        reasons.append("no strictly-PIT HAR h20 forecast for this session")
    doc: dict[str, Any] = {
        "schema": GATE_SCHEMA,
        "study": STUDY,
        "session": session.isoformat(),
        "vintage": "forward",
        "iv30": iv30,
        "iv30_inputs": iv_meta,
        "harvol20": hv[0] if hv else None,
        "har": hv[1] if hv else None,
        "r": (iv30 / hv[0]) if (iv30 is not None and hv is not None) else None,
        "threshold": {
            "ter_hi": TER_HI,
            "semantics": "gate ON iff p >= 2/3 (TER_HI); xe reads p at t, xp reads p at t-1",
            "pool_sensitivity_band": POOL_BAND,
            "band_note": "a decision within ±band of 2/3 is pool-choice-dependent (§3.4)",
        },
        "inputs": {
            "panel_sha256": inputs.panel_sha256,
            "earnings_calendar_sha256": inputs.earnings_sha256,
            "iv_history_sha256": inputs.ivhist_sha256,
            "rate": iv_meta.get("rate"),
            "rate_source": inputs.rate_label,
            "calendar_content_sha256": inputs.calendar_sha256,
        },
        "builder_sha256": builder,
        "generated_at": (now or _utcnow()).isoformat(),
    }
    forward = dict(forward_prior)
    if doc["r"] is not None:
        forward[session] = doc["r"]
    spliced = {**base.series, **forward}
    if doc["r"] is None:
        doc["status"] = "refused"
        doc["reason"] = "; ".join(r for r in reasons if r) or "no usable surface"
        doc["pools"] = {
            name: _pool_read(None, 0, "no evaluable r at session") for name in _POOL_NAMES
        }
    else:
        doc["status"] = "ok"
        doc["reason"] = ""
        doc["pools"] = {
            "primary": pool_primary(spliced, session, inputs.cal),
            "a_mixed_fullbase": pool_mixed_fullbase(base.series, forward, session),
            "b_forward_only_trailing252": pool_forward_only(forward, session, inputs.cal),
        }
    n_forward = sum(1 for d in forward if d >= FORWARD_FIRST)
    warmup_met = n_forward >= WARMUP_MIN_FORWARD
    doc["warmup"] = {
        "min_forward_r": WARMUP_MIN_FORWARD,
        "n_forward_r_to_date": n_forward,
        "met": warmup_met,
        "card_disposition": "conditionable" if warmup_met else "DEFERRED-UNCONDITIONED",
        "note": "§3.5 warm-up (not a vintage control): before 50 forward r-obs, cards are "
        "recorded DEFERRED-UNCONDITIONED and excluded from any criterion needing p",
    }
    path = gate_path(state_root, session)
    path.parent.mkdir(parents=True, exist_ok=True)
    action, detail = _publish_write_once(path, doc, builder)
    return EmitOutcome(session, action, doc["status"], detail, doc)


_POOL_NAMES = ("primary", "a_mixed_fullbase", "b_forward_only_trailing252")


def recorded_chain_sessions(store_root: Path, sym: str = NAME) -> list[date]:
    """The recorded EOD chain sessions (clock tiers excluded): session dirs
    whose ``chains/<D>/<SYM>.json.gz`` exists."""
    root = store_root / "chains"
    out: list[date] = []
    if not root.is_dir():
        return out
    for p in root.iterdir():
        if not p.is_dir():
            continue
        try:
            d = date.fromisoformat(p.name)
        except ValueError:
            continue  # clock=... snapshot namespaces are not sessions
        if (p / f"{sym}.json.gz").is_file():
            out.append(d)
    return sorted(out)


def run_emit(
    *,
    sessions: Sequence[date],
    cal: SessionCalendar,
    state_root: Path | None = None,
    now: datetime | None = None,
) -> list[EmitOutcome]:
    """Emit gates for the given recorded sessions (in order), then annotate
    card events. The orchestrator the CLI calls."""
    state_root = state_root or paths.state_root()
    inputs = load_inputs(cal)
    base = load_base(inputs, state_root, now=now)
    harvol = harvol20_by_session(inputs)
    outcomes: list[EmitOutcome] = []
    for session in sessions:
        chain_path = paths.store_root() / "chains" / session.isoformat() / f"{NAME}.json.gz"
        prior = forward_r_from_gates(state_root)
        outcomes.append(
            emit_session(
                session,
                inputs=inputs,
                base=base,
                state_root=state_root,
                harvol=harvol,
                forward_prior=prior,
                chain_path=chain_path,
                now=now,
            )
        )
    annotate_card_events(state_root, cal, now=now)
    return outcomes


# ------------------------------------------------------- card-event annotation


def _signals_docs(state_root: Path) -> list[tuple[date, dict[str, Any]]]:
    root = state_root / "signals"
    if not root.is_dir():
        return []
    out = []
    for p in sorted(root.glob("*.json")):
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        try:
            out.append((date.fromisoformat(p.stem), doc))
        except ValueError:
            continue
    return out


def _existing_card_keys(path: Path) -> set[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    if not path.exists():
        return keys
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            doc = json.loads(line)
        except ValueError:
            continue
        if isinstance(doc, dict) and "session" in doc and "name" in doc:
            keys.add((str(doc["session"]), str(doc["name"])))
    return keys


def _gate_at(state_root: Path, session: date) -> dict[str, Any] | None:
    path = gate_path(state_root, session)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def annotate_card_events(
    state_root: Path, cal: Calendar, *, now: datetime | None = None
) -> list[str]:
    """§8.6: for each recorded XSMOM entry, append the ``xe`` gate state at t
    and the ``xp`` percentile at t-1 plus the dictated size to
    ``card-events.jsonl`` (append-only, idempotent per (session, name); state
    area only — sealed cards and LEDGER.md are never touched)."""
    path = card_events_path(state_root)
    seen = _existing_card_keys(path)
    builder = builder_sha256()
    prev_of: dict[date, date | None] = {}
    lines: list[str] = []
    skipped: list[str] = []
    for t, sig in _signals_docs(state_root):
        xs = sig.get("xsmom") if isinstance(sig, dict) else None
        if not isinstance(xs, dict) or not xs.get("fires"):
            continue
        if t < FORWARD_FIRST:
            continue  # before any forward gate can exist (the declared break)
        gate = _gate_at(state_root, t)
        if gate is None:
            skipped.append(f"{t.isoformat()} (no gate file)")
            continue
        if t not in prev_of:
            sessions = cal.sessions()
            i = sessions.index(t) if t in sessions else -1
            prev_of[t] = sessions[i - 1] if i > 0 else None
        prev = prev_of[t]
        prev_gate = _gate_at(state_root, prev) if prev is not None else None
        warmup = gate.get("warmup", {})
        for name in xs.get("top3") or []:
            key = (t.isoformat(), str(name))
            if key in seen:
                continue
            primary = (gate.get("pools") or {}).get("primary") or {}
            xp_p = (prev_gate or {}).get("pools", {}).get("primary") if prev_gate else None
            xp_p = xp_p or {}
            if xp_p.get("p") is not None:
                xp_status = "ok"
            elif prev_gate is None:
                xp_status = (
                    f"no gate for t-1 ({prev.isoformat() if prev else 'no previous session'})"
                )
            else:
                xp_status = str(xp_p.get("status") or "not evaluable")
            size = None
            if xp_p.get("p") is not None:
                size = 0.5 if xp_p["p"] >= TER_HI else 1.0
            event = {
                "schema": CARD_EVENT_SCHEMA,
                "study": STUDY,
                "kind": "xsmom-top3-entry",
                "session": t.isoformat(),
                "name": str(name),
                "recorded_at": (sig.get("provenance") or {}).get("generated_at"),
                "accrual": (
                    "in-accrual"
                    if t > REGISTRATION_DATE
                    else "excluded-pre-registration (§4: observed-but-unscored)"
                ),
                "xe_mkt_hi": {
                    "read_at": "t",
                    "pool": "primary",
                    "p": primary.get("p"),
                    "on": primary.get("on"),
                    "gate_status": gate.get("status"),
                    "within_band": primary.get("within_band"),
                },
                "xp_size_mkt": {
                    "read_at": "t-1",
                    "prev_session": prev.isoformat() if prev else None,
                    "pool": "primary",
                    "p": xp_p.get("p"),
                    "status": xp_status,
                    "dictated_size": size,
                },
                "warmup": {
                    "met": warmup.get("met"),
                    "disposition": warmup.get("card_disposition"),
                },
                "builder_sha256": builder,
                "generated_at": (now or _utcnow()).isoformat(),
            }
            lines.append(json.dumps(event, sort_keys=True))
            seen.add(key)
    if lines:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("".join(line + "\n" for line in lines))
    notes = list(skipped)
    if lines or skipped:
        notes.append(f"appended {len(lines)} card-event line(s)")
    return notes
