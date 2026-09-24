"""GET /api/market/{sym}/ideas: the advisory idea-context payload.

The Ideas tab is context, never a trading instruction. It assembles what
the desk already knows about one name — the signals file (xsmom rank +
PEAD events + the next known report), the deal miner's entry queue
(``trex.deal/1``, filtered to the name), the paper book's structures on
the name, the sealed scratch lane's LEDGER.md rows that mention it, and
the RESEARCH-LEDGER.md sections that mention it — and hands the protocol
boundary along with the data: only ``ALLOWED_DIRECTION`` signals may ever
point a trade, everything else (including every research line) is
information. ``advisory: true`` is a literal.

Everything is read-only and nullable: a missing signals dir, queue dir or
ledger degrades its section to null/empty; money that the desk persists
as strings is coerced to floats only where the viewer contract types a
number, and an unparseable value passes as None (never a guess).
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

from tree_options.trex_web.discovery_view import _age
from tree_options.trex_web.options_view import (
    FEATURES_LOOKBACK,
    _feature_sessions,
    _read_features,
)
from tree_options.trex_web.reader import list_plans

# cards: LEDGER.md lines mentioning the name, capped (order preserved)
LEDGER_MAX_LINES = 24
# research: the recognized RESEARCH-LEDGER.md sections, in file order
RESEARCH_SECTIONS = ("SURVIVORS", "DEFLATED", "DEAD", "DATA INTEGRITY")
RESEARCH_MAX_PER_SECTION = 12
RESEARCH_MAX_SECTIONS = 3
# pead: the evaluated events kept per name (the file carries every name's)
EVALUATED_KEEP = 5
_ISO_DATE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
_HEADING = re.compile(r"^##\s+(.+?)\s*$")


def _fnum(raw: Any) -> float | None:
    """A desk string money/ratio (Decimal-formatted) — or a JSON number —
    as float; None on junk, never a guess."""
    if raw is None:
        return None
    try:
        return float(str(raw))
    except ValueError:
        return None


def _as_int(raw: Any) -> int:
    """A count as int; 0 on junk (rank/quantity are always ints on disk)."""
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def _read_json_dict(path: Path) -> dict[str, Any] | None:
    try:
        doc = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def _newest_dated(directory: Path) -> Path | None:
    """The newest ``<D>.json`` in a dated-artifact dir (odd stems ignored)."""
    if not directory.is_dir():
        return None
    sessions: list[str] = []
    for p in directory.glob("*.json"):
        try:
            sessions.append(date.fromisoformat(p.stem).isoformat())
        except ValueError:
            continue
    if not sessions:
        return None
    return directory / f"{max(sessions)}.json"


# ------------------------------------------------------------------ signals


def _next_report(store_root: Path, paper_dir: Path, sym: str, now: datetime) -> str | None:
    """The next known earnings report: the newest features card that
    carries one, else earnings-calendar.json (first future date, else the
    last known — a schedule that ends in the past is still the schedule)."""
    features_dir = store_root / "features"
    for d in _feature_sessions(features_dir)[:FEATURES_LOOKBACK]:
        doc = _read_features(features_dir / f"{d}.json")
        names = doc.get("names", {}) if doc else {}
        name = names.get(sym) if isinstance(names, dict) else None
        earnings = name.get("earnings") if isinstance(name, dict) else None
        rep = earnings.get("next_report") if isinstance(earnings, dict) else None
        if isinstance(rep, str) and rep:
            return rep
    cal = _read_json_dict(paper_dir / "earnings-calendar.json")
    raw = cal.get(sym) if cal is not None else None
    if not isinstance(raw, list):
        return None
    dates = sorted(x for x in raw if isinstance(x, str))
    if not dates:
        return None
    future = [d for d in dates if d > now.date().isoformat()]
    return future[0] if future else dates[-1]


def _signals_section(
    signals_dir: Path, store_root: Path, paper_dir: Path, sym: str, now: datetime
) -> dict[str, Any] | None:
    """The newest signals file (schema desk-signals/1) projected onto the
    name. No dated file anywhere -> None; the xsmom section survives a name
    the ranking excluded (``score: null``)."""
    path = _newest_dated(signals_dir)
    if path is None:
        return None
    doc = _read_json_dict(path)
    if doc is None:
        return None
    xsmom = doc.get("xsmom") if isinstance(doc.get("xsmom"), dict) else {}
    scores = xsmom.get("scores") if isinstance(xsmom.get("scores"), dict) else {}
    top3_raw = xsmom.get("top3") if isinstance(xsmom.get("top3"), list) else []
    beats_raw = doc.get("pead") if isinstance(doc.get("pead"), list) else []
    eval_raw = doc.get("pead_evaluated") if isinstance(doc.get("pead_evaluated"), list) else []
    provenance = doc.get("provenance") if isinstance(doc.get("provenance"), dict) else {}
    evaluated = [
        {
            "report_date": e.get("report_date"),
            "prior_session": e.get("prior_session"),
            "move": _fnum(e.get("move")),
            "fires": e.get("fires") if isinstance(e.get("fires"), bool) else None,
            "reason": e.get("reason"),
        }
        for e in eval_raw
        if isinstance(e, dict) and e.get("name") == sym
    ][-EVALUATED_KEEP:]
    return {
        "session": doc.get("session"),
        "age_seconds": _age(provenance.get("generated_at"), now),
        "xsmom": {
            "score": _fnum(scores.get(sym)),
            "in_top3": sym in top3_raw,
            "top3": [str(t) for t in top3_raw],
            "is_rebalance_day": xsmom.get("is_rebalance_day") is True,
            "n_ranked": _as_int(xsmom.get("n_ranked")),
            "conventions_agree": xsmom.get("conventions_agree") is True,
        },
        "pead": {
            "beats": [
                {
                    "report_date": b.get("report_date"),
                    "move": _fnum(b.get("move")),
                }
                for b in beats_raw
                if isinstance(b, dict) and b.get("name") == sym
            ],
            "evaluated": evaluated,
        },
        "next_report": _next_report(store_root, paper_dir, sym, now),
    }


# -------------------------------------------------------------------- queue


def _deal_leg(leg: Any) -> dict[str, Any] | None:
    if not isinstance(leg, dict):
        return None
    return {
        "right": leg.get("right"),
        "action": leg.get("action"),
        "strike": _fnum(leg.get("strike")),
        "expiry": leg.get("expiry"),
        "bid": _fnum(leg.get("bid")),
        "ask": _fnum(leg.get("ask")),
        "oi": _fnum(leg.get("oi")),
        "iv": _fnum(leg.get("iv")),
        "delta": _fnum(leg.get("delta")),
    }


def _deal_row(d: dict[str, Any]) -> dict[str, Any]:
    """One queue deal in the viewer's shape: the display fields, the legs
    and the pointing signal (weight is the miner's, not the UI's)."""
    raw_signal = d.get("signal")
    signal = None
    if isinstance(raw_signal, dict):
        signal = {"name": raw_signal.get("name"), "excess_20": _fnum(raw_signal.get("excess_20"))}
    legs = [leg for leg in map(_deal_leg, d.get("legs") or []) if leg is not None]
    return {
        "deal_id": d.get("deal_id"),
        "rank": _as_int(d.get("rank")),
        "status": d.get("status"),
        "row_title": d.get("row_title"),
        "kind": d.get("kind"),
        "underlying": d.get("underlying"),
        "quantity": _as_int(d.get("quantity")),
        "legs": legs,
        "ref_mid": _fnum(d.get("ref_mid")),
        "fill": _fnum(d.get("fill")),
        "limit": _fnum(d.get("limit")),
        "max_loss": _fnum(d.get("max_loss")),
        "signal": signal,
        "reasons": [str(r) for r in d.get("reasons") or []],
        "notes": [str(n) for n in d.get("notes") or []],
    }


def _queue_section(queue_dir: Path, sym: str) -> dict[str, Any] | None:
    """The newest entry queue (schema trex.deal/1) reduced to the name:
    admissible first, then surfaced, other underlyings filtered out.
    ``miner_status`` is the selection rule's status — PROPOSED means the
    queue awaits an operator ruling, which the UI says in words."""
    path = _newest_dated(queue_dir)
    if path is None:
        return None
    doc = _read_json_dict(path)
    if doc is None:
        return None
    deals = [
        _deal_row(d)
        for key in ("admissible", "surfaced")
        for d in doc.get(key) or []
        if isinstance(d, dict) and d.get("underlying") == sym
    ]
    miner = doc.get("miner") if isinstance(doc.get("miner"), dict) else {}
    return {
        "session": doc.get("session"),
        "entry_session": doc.get("entry_session"),
        "valid_until": doc.get("valid_until"),
        "miner_status": str(miner.get("status") or ""),
        "deals": deals,
    }


# ---------------------------------------------------------- paper positions


def _paper_positions(
    state_root: Path, plans_root: Path, sym: str
) -> list[dict[str, Any]]:
    """The paper book's structures on the name, open first then by plan id
    (the status field lets the UI filter; closed rows stay for history)."""
    rows: list[dict[str, Any]] = []
    for view in list_plans(state_root, plans_root):
        for s in view.plan.structures:
            if s.underlying != sym:
                continue
            st = view.structures.get(s.id)
            if st is None:
                continue
            rows.append(
                {
                    "plan_id": view.plan.id,
                    "structure_id": s.id,
                    "account_mode": str(view.plan.account_mode),
                    "expiry": s.expiry.isoformat(),
                    "long_strike": float(s.long_strike),
                    "short_strike": float(s.short_strike),
                    "quantity": int(s.quantity),
                    "open_qty": int(st.open_qty),
                    "status": st.state.value,
                    "entry_fill": float(st.entry_fill) if st.entry_fill is not None else None,
                    "exit_deadline": s.exit_deadline.isoformat(),
                }
            )
    rows.sort(key=lambda r: (r["status"] != "open", r["plan_id"], r["structure_id"]))
    return rows


# ------------------------------------------------------------------ ledgers


def _cards_section(paper_dir: Path, sym: str) -> dict[str, Any] | None:
    """LEDGER.md rows mentioning the name (word-boundary, case-sensitive),
    capped and order-preserved — sealed text served verbatim, never
    re-rendered. Missing file -> None."""
    try:
        raw = (paper_dir / "LEDGER.md").read_bytes()
    except OSError:
        return None
    pattern = re.compile(rf"\b{re.escape(sym)}\b")
    lines = [
        line for line in raw.decode("utf-8", "replace").splitlines() if pattern.search(line)
    ][:LEDGER_MAX_LINES]
    return {"lines": lines, "ledger_sha256_12": hashlib.sha256(raw).hexdigest()[:12]}


def _canonical_section(heading: str) -> str | None:
    """A recognized research heading -> its canonical section label; the
    campaign section keeps its dated slug (``campaign-2026-09``)."""
    for known in RESEARCH_SECTIONS:
        if heading == known or heading.startswith(f"{known} ") or heading.startswith(
            f"{known}("
        ):
            return known
    if heading.startswith("campaign"):
        return heading.split()[0]
    return None


def _research_section(paper_dir: Path, sym: str) -> dict[str, Any] | None:
    """RESEARCH-LEDGER.md context: lines mentioning the name inside the
    recognized sections (max 12 per section, first 3 sections with
    matches). Display-only — the panel renders these non-directional.
    ``ledger_date`` is the newest date-like token in the kept lines, else
    the file mtime's date. Missing file -> None."""
    path = paper_dir / "RESEARCH-LEDGER.md"
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    pattern = re.compile(rf"\b{re.escape(sym)}\b")
    entries: list[dict[str, Any]] = []
    dates: list[str] = []
    used: list[str] = []
    section: str | None = None
    count = 0
    for line in raw.decode("utf-8", "replace").splitlines():
        heading = _HEADING.match(line)
        if heading:
            section = _canonical_section(heading.group(1))
            count = 0
            continue
        if section is None or not pattern.search(line):
            continue
        if section not in used and len(used) >= RESEARCH_MAX_SECTIONS:
            continue  # section cap reached; later sections are not collected
        count += 1
        if count > RESEARCH_MAX_PER_SECTION:
            continue
        if section not in used:
            used.append(section)
        entries.append({"section": section, "line": line})
        dates.extend(_ISO_DATE.findall(line))
    ledger_date = (
        max(dates)
        if dates
        else datetime.fromtimestamp(path.stat().st_mtime).date().isoformat()
    )
    return {
        "ledger_date": ledger_date,
        "sha256_12": hashlib.sha256(raw).hexdigest()[:12],
        "entries": entries,
    }


# ----------------------------------------------------------------- protocol


def _protocol() -> dict[str, Any]:
    """The boundary itself, from the desk's own constants — the panel never
    hardcodes which signals may point a trade."""
    from tree_options.desk import signals as desk_signals

    return {
        "allowed_direction": sorted(desk_signals.ALLOWED_DIRECTION),
        "context_only": sorted(desk_signals.CONTEXT_ONLY),
        "advisory": True,
    }


# ------------------------------------------------------------------ payload


def ideas_payload(
    sym: str,
    signals_dir: Path,
    queue_dir: Path,
    store_root: Path,
    paper_dir: Path,
    state_root: Path,
    plans_root: Path,
    now: datetime,
) -> dict[str, Any]:
    """The /api/market/{sym}/ideas body (paths resolved by the caller)."""
    return {
        "now": now.isoformat(),
        "symbol": sym,
        "signals": _signals_section(signals_dir, store_root, paper_dir, sym, now),
        "queue": _queue_section(queue_dir, sym),
        "paper_positions": _paper_positions(state_root, plans_root, sym),
        "cards": _cards_section(paper_dir, sym),
        "research": _research_section(paper_dir, sym),
        "protocol": _protocol(),
    }
