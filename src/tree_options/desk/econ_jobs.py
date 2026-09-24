"""I/O wiring of the desk's econometrics commands (``features``,
``ivhist-build``, ``ivhist-001``, ``forecast-001``): read the inputs
(read-only: the chain store, the research panel under its shared lock, the
sealed earnings calendar, the market lane's earnings-timing file and stored
index histories, the Polygon cache), call the pure modules, write under
``DESK_STORE``. Every input is named by sha256 in the output so a result can
say what it was computed from.

Index histories and DTB3 are read in the market lane's stored format
(``<store>/indices/<X>.csv``, :mod:`tree_options.desk.indices`). The raw
vendor snapshots the sealed IVHIST-001 run used (CBOE ``<X>_History.csv``,
the raw FRED ``DTB3.csv``) are read only through the explicit
``raw_snapshots`` / ``raw_cboe`` adapters.
"""

from __future__ import annotations

import hashlib
import json
import sys
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Any

from tree_options.desk import evaluate, events, ivhist, paths, surface
from tree_options.desk.panel import PanelLocked, read_panel_with_sha256
from tree_options.desk.sessions import Calendar
from tree_options.desk.store import atomic_write_bytes, atomic_write_json
from tree_options.desk.universe import CHAIN_UNIVERSE

HISTORY_FILE = "vwap_atm.json"
VERDICT_FILE = "IVHIST-001-verdict.json"
SENSITIVITY_FILE = "FORECAST-001-coverage-sensitivity.json"


def _sha(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text())


def iv_history_dir(store: Path) -> Path:
    return store / "iv-history"


def load_panel(warnings: list[str]) -> tuple[dict[str, Any] | None, str | None]:
    path = paths.paper_dir() / "ohlc-panel.json"
    try:
        return read_panel_with_sha256(path)
    except (OSError, ValueError, PanelLocked) as exc:
        warnings.append(f"panel unavailable ({type(exc).__name__}: {exc})")
        return None, None


def load_earnings(warnings: list[str]) -> tuple[dict[str, list[str]], str | None]:
    """The sealed calendar; ({}, None) when unreadable (callers must treat
    that as schedule UNAVAILABLE, never as 'no reports')."""
    path = paths.paper_dir() / events.SEALED_CALENDAR
    try:
        doc = _load_json(path)
    except (OSError, ValueError) as exc:
        warnings.append(f"earnings calendar unavailable ({type(exc).__name__})")
        return {}, None
    if not isinstance(doc, dict):
        warnings.append("earnings calendar is not an object")
        return {}, None
    return {str(k): [str(d) for d in v] for k, v in doc.items() if isinstance(v, list)}, _sha(path)


def forward_schedule(
    sealed: dict[str, list[str]], warnings: list[str]
) -> tuple[dict[str, list[str]], str | None]:
    """Known report dates per name for forward windows: the sealed calendar
    plus the market lane's earnings-timing file (confirmed and estimated;
    estimated dates count as scheduled for variance purposes)."""
    path = paths.paper_dir() / events.TIMING_FILE
    try:
        timing = events.load_timing(path)
    except events.EventsError as exc:
        warnings.append(f"earnings timing unreadable ({exc}); sealed calendar only")
        timing = {}
    if not path.exists():
        warnings.append(f"no {events.TIMING_FILE}: forward schedule = sealed calendar only")
    names = set(sealed) | set(timing)
    merged = {n: sorted(set(sealed.get(n, [])) | set(timing.get(n, {}))) for n in names}
    return merged, _sha(path)


def load_history(store: Path, warnings: list[str]) -> tuple[dict[str, Any] | None, str | None]:
    path = iv_history_dir(store) / HISTORY_FILE
    try:
        doc = _load_json(path)
    except (OSError, ValueError) as exc:
        warnings.append(f"IV history unavailable ({type(exc).__name__})")
        return None, None
    return (doc if isinstance(doc, dict) else None), _sha(path)


def load_labels(store: Path, history_sha: str | None, warnings: list[str]) -> dict[str, str]:
    """IVHIST-001 labels, accepted only for the history they scored: the
    verdict's recorded ``inputs.iv_history_sha256`` must equal the loaded
    history's sha256; otherwise every name is ``unvalidated``."""
    try:
        doc = _load_json(iv_history_dir(store) / VERDICT_FILE)
    except (OSError, ValueError):
        return {}
    if not isinstance(doc, dict) or not isinstance(doc.get("labels"), dict):
        return {}
    labels = {str(k): str(v) for k, v in doc["labels"].items()}
    scored = (doc.get("inputs") or {}).get("iv_history_sha256")
    if history_sha is None or scored != history_sha:
        warnings.append(
            f"IVHIST-001 verdict scored history {str(scored)[:12]}, loaded history is "
            f"{str(history_sha)[:12]}: IV labels unvalidated"
        )
        return {name: "unvalidated" for name in labels}
    return labels


# --------------------------------------------------------------- features


def run_features(session: date, cal: Calendar) -> int:
    if not cal.is_session(session):
        print(f"features: {session} is not an NYSE session", file=sys.stderr)
        return 2
    store = paths.store_root()
    chain_dir = store / "chains" / session.isoformat()
    chains = {
        p.name[: -len(".json.gz")]: p
        for p in sorted(chain_dir.glob("*.json.gz"))
        if not p.name.endswith(".conflict.json.gz")
    }
    if not chains:
        print(f"features: no recorded chains for {session}", file=sys.stderr)
        return 1
    warnings: list[str] = []
    rates, warn = ivhist.rate_source_for_store(store)
    if warn:
        warnings.append(warn)
    rate = rates.rate_on(session)
    rate_label = rates.label
    if rate is None:
        rate = ivhist.DEFAULT_RATE
        rate_label = f"declared-constant {rate} (no DTB3 observation on or before {session})"
        warnings.append(rate_label)
    panel, panel_sha = load_panel(warnings)
    sealed, earnings_sha = load_earnings(warnings)
    schedule, timing_sha = forward_schedule(sealed, warnings) if earnings_sha else ({}, None)
    history, history_sha = load_history(store, warnings)
    labels = load_labels(store, history_sha, warnings)
    har_names = [n for n in CHAIN_UNIVERSE if panel is not None and n in panel]
    doc = surface.build_features(
        session,
        chains=chains,
        cal=cal,
        rate=rate,
        rate_label=rate_label,
        panel=panel,
        panel_sha256=panel_sha,
        har_names=har_names,
        earnings=sealed,
        earnings_sha256=earnings_sha,
        schedule=schedule,
        schedule_available=earnings_sha is not None,
        timing_sha256=timing_sha,
        history=history,
        history_sha256=history_sha,
        labels=labels,
        warnings=warnings,
    )
    out = store / "features" / f"{session.isoformat()}.json"
    atomic_write_json(out, doc)
    for w in warnings:
        print(f"  warning: {w}")
    print(f"features session={session} names={len(doc['names'])} -> {out}")
    return 0


# ----------------------------------------------------------------- ivhist


def run_ivhist_build(
    cache: Path,
    start: date,
    end: date,
    names: Sequence[str],
    cal: Calendar,
    *,
    raw_snapshots: bool = False,
) -> int:
    if not cache.is_dir():
        print(f"ivhist-build: no cache directory {cache}", file=sys.stderr)
        return 1
    store = paths.store_root()
    rates, warn = ivhist.rate_source_for_store(store, raw_snapshots=raw_snapshots)
    if warn:
        print(f"  warning: {warn}")
    sessions = [s for s in cal.sessions() if start <= s <= end]
    scan = ivhist.scan_cache(cache, names, start, end, cal)
    doc = ivhist.build_history(scan, names, sessions, rates)
    out = iv_history_dir(store) / HISTORY_FILE
    atomic_write_json(out, doc)
    ok = sum(
        1
        for n in doc["names"].values()
        for rec in n["sessions"].values()
        if isinstance(rec.get("iv30"), float)
    )
    print(f"  scan: {dict(sorted(scan.stats.items()))}")
    print(f"ivhist-build sessions={len(sessions)} names={len(names)} iv30={ok} -> {out}")
    return 0


def load_index_histories(
    indices_dir: Path, *, raw_cboe: bool = False
) -> tuple[dict[str, dict[date, float]], dict[str, str | None]]:
    """The benchmark histories: the market lane's stored ``<X>.csv`` or,
    with ``raw_cboe``, raw CBOE ``<X>_History.csv`` snapshots."""
    found: dict[str, dict[date, float]] = {}
    shas: dict[str, str | None] = {}
    for name in ivhist.INDEX_NAMES:
        path = indices_dir / (f"{name}_History.csv" if raw_cboe else f"{name}.csv")
        if path.exists():
            found[name] = (
                ivhist.read_raw_cboe_csv(path) if raw_cboe else ivhist.read_index_store(path)
            )
            shas[name] = _sha(path)
    return found, shas


def run_ivhist_001(indices_dir: Path, *, raw_cboe: bool = False) -> int:
    store = paths.store_root()
    hist_path = iv_history_dir(store) / HISTORY_FILE
    try:
        history = _load_json(hist_path)
    except (OSError, ValueError) as exc:
        print(
            f"ivhist-001: no IV history ({type(exc).__name__}); run ivhist-build", file=sys.stderr
        )
        return 1
    try:
        indices, shas = load_index_histories(indices_dir, raw_cboe=raw_cboe)
    except ValueError as exc:
        print(f"ivhist-001: unreadable index history ({exc})", file=sys.stderr)
        return 1
    res = ivhist.evaluate(history, indices)
    res["inputs"] = {
        "iv_history_sha256": _sha(hist_path),
        "indices_dir": str(indices_dir),
        "indices_sha256": shas,
    }
    out = iv_history_dir(store) / VERDICT_FILE
    atomic_write_json(out, res)
    for index, rec in res["pairs"].items():
        print(
            f"  {index}~{rec['name']}: {rec['status']} n={rec.get('n')}"
            f" median_bias={rec.get('median_bias')} corr={rec.get('corr')}"
        )
    print(f"ivhist-001 overall={res['overall']} -> {out}")
    return 0


# --------------------------------------------------------------- forecast


def run_forecast_001(
    out_dir: Path | None, cal: Calendar, *, coverage_sensitivity: bool = False
) -> int:
    store = paths.store_root()
    warnings: list[str] = []
    panel, panel_sha = load_panel(warnings)
    if panel is None:
        print(f"forecast-001: {warnings[-1]}", file=sys.stderr)
        return 1
    earnings, earnings_sha = load_earnings(warnings)
    if earnings_sha is None:
        print(f"forecast-001: {warnings[-1]}", file=sys.stderr)
        return 1
    history, history_sha = load_history(store, warnings)
    labels = load_labels(store, history_sha, warnings)
    names = [n for n in CHAIN_UNIVERSE if n in panel]
    target = out_dir or store / "evaluations"
    if coverage_sensitivity:
        sens = evaluate.coverage_sensitivity(
            panel, earnings, cal, names=names, iv_history=history, iv_labels=labels
        )
        sens["provenance"] = {"panel_sha256": panel_sha, "earnings_calendar_sha256": earnings_sha}
        atomic_write_json(target / SENSITIVITY_FILE, sens)
        for h, c in sens["horizons"].items():
            print(
                f"  h={h} uncovered={c['n_uncovered']}/{c['n_rows']}"
                f" p_all={c['p_all']} p_covered={c['p_covered_only']}"
            )
        print(
            f"forecast-001 coverage sensitivity verdict_covered_only={sens['verdict_covered_only']}"
        )
        return 0
    res = evaluate.run_forecast_001(
        panel,
        earnings,
        cal,
        names=names,
        iv_history=history,
        iv_labels=labels,
        provenance={
            "panel_sha256": panel_sha,
            "earnings_calendar_sha256": earnings_sha,
            "iv_history_sha256": history_sha,
            "ivhist_verdict_sha256": _sha(iv_history_dir(store) / VERDICT_FILE),
            "warnings": warnings,
        },
    )
    atomic_write_json(target / "FORECAST-001.json", res)
    atomic_write_bytes(target / "FORECAST-001.md", evaluate.render_markdown(res).encode())
    for h, by_b in res["cells"].items():
        c = by_b["rv22"]["qlike"]
        print(f"  h={h} HAR vs RV22 QLIKE: dm={c.get('dm')} p={c.get('p_one_sided')}")
    print(f"forecast-001 verdict={res['verdict']} cutoff={res['cutoff']} -> {target}")
    return 0
