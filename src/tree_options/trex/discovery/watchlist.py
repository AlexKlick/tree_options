"""The watchlist: operator-owned symbol list for the market desk.

Mutations arrive through the spool (trex-web stays read-only) and are
IDEMPOTENT by design: the retain-until-complete spool can redeliver a
request after a runner crash, so re-applying an op must be a no-op.
Seeds from the discovery config underlyings + every plan TOML's
underlyings on first creation.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

SYMBOL_RE = re.compile(r"^[A-Z.]{1,6}$")
DISMISS_SUPPRESS_SECONDS = 7 * 86_400  # no reviving a dismissal for a week
DECIDED_RETAIN_SECONDS = 30 * 86_400
MAX_PROPOSALS_KEPT = 200


def seed_symbols(cfg: Any, plans_dir: Path | None) -> list[str]:
    """config underlyings + plan TOML underlyings, deduped, order-stable."""
    out: list[str] = []
    for sym in list(getattr(cfg, "underlyings", [])):
        s = str(sym).upper()
        if s not in out:
            out.append(s)
    if plans_dir is not None and plans_dir.exists():
        from tree_options.trex.plan import load_plan

        for path in sorted(plans_dir.glob("*.toml")):
            try:
                plan = load_plan(path)
            except (OSError, ValueError):
                continue
            for structure in plan.structures:
                s = structure.underlying.upper()
                if s not in out:
                    out.append(s)
    return out


def read_watchlist(state_dir: Path) -> dict[str, Any]:
    """Read-only view for the web lane: NEVER creates or repairs the file
    (a GET that wrote an unseeded document would pre-empt the runner's
    seeding, and trex-web may not write here at all)."""
    path = state_dir / "watchlist.json"
    try:
        doc = json.loads(path.read_text())
    except (OSError, ValueError):
        return {"version": 1, "symbols": [], "proposals": []}
    if not isinstance(doc, dict) or "symbols" not in doc:
        return {"version": 1, "symbols": [], "proposals": []}
    return doc


def load_watchlist(
    state_dir: Path,
    seed: list[str] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Load (or create from seed) the watchlist document."""
    path = state_dir / "watchlist.json"
    if path.exists():
        try:
            doc = json.loads(path.read_text())
            if isinstance(doc, dict) and "symbols" in doc:
                return doc
        except (OSError, ValueError):
            pass  # junk: re-seed below (symbols are recoverable)
    stamp = (now or datetime.now()).isoformat()
    doc = {
        "version": 1,
        "symbols": [
            {"symbol": s, "origin": "seed", "added_at": stamp} for s in (seed or [])
        ],
        "proposals": [],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(doc, indent=2) + "\n")
    tmp.replace(path)
    return doc


def apply_watch_op(
    state_dir: Path,
    op: str,
    symbol: str | None = None,
    proposal_id: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Apply one idempotent watchlist mutation; returns a result dict."""
    doc = load_watchlist(state_dir)
    stamp = (now or datetime.now()).isoformat()
    symbols: list[dict[str, Any]] = doc["symbols"]
    have = {row["symbol"] for row in symbols}
    if op == "add":
        if not symbol:
            return {"status": "invalid", "detail": "add requires a symbol"}
        sym = symbol.strip().upper()
        if not SYMBOL_RE.match(sym):
            return {"status": "invalid", "detail": f"bad symbol {symbol!r}"}
        if sym in have:
            return {"status": "noop", "detail": f"{sym} already watched"}
        symbols.append({"symbol": sym, "origin": "operator", "added_at": stamp})
    elif op == "remove":
        if not symbol:
            return {"status": "invalid", "detail": "remove requires a symbol"}
        sym = symbol.strip().upper()
        if sym not in have:
            return {"status": "noop", "detail": f"{sym} not watched"}
        doc["symbols"] = [row for row in symbols if row["symbol"] != sym]
        _save(state_dir, doc)
        return {"status": "removed", "symbol": sym}
    elif op in ("approve", "dismiss"):
        return _decide(state_dir, doc, op, proposal_id, stamp)
    else:
        return {"status": "invalid", "detail": f"unknown op {op!r}"}
    _save(state_dir, doc)
    return {"status": "ok", "symbols": [row["symbol"] for row in doc["symbols"]]}


def _save(state_dir: Path, doc: dict[str, Any]) -> None:
    path = state_dir / "watchlist.json"
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(doc, indent=2) + "\n")
    tmp.replace(path)


# -- LLM proposal lifecycle (M6, Codex-arch #12) ----------------------------
#
# Proposals live HERE (never in the 30-run pruned scan store). Each has a
# durable id + provenance; pending proposals are never regenerated over,
# dismissed ones are not revived for DISMISS_SUPPRESS_SECONDS, and every
# decision is idempotent (the spool may redeliver).


def _age_seconds(stamp: object, now: datetime) -> float | None:
    if not isinstance(stamp, str):
        return None
    try:
        return (now - datetime.fromisoformat(stamp)).total_seconds()
    except (TypeError, ValueError):
        return None


def blocked_symbols(doc: dict[str, Any], now: datetime) -> set[str]:
    """Symbols the model must not propose: pending, or dismissed recently."""
    out: set[str] = set()
    for prop in doc.get("proposals", []):
        status = prop.get("status")
        if status == "pending":
            out.add(prop["symbol"])
        elif status == "dismissed":
            age = _age_seconds(prop.get("decided_at"), now)
            if age is None or age < DISMISS_SUPPRESS_SECONDS:
                out.add(prop["symbol"])
    return out


def record_proposals(
    state_dir: Path,
    proposals: list[dict[str, Any]],
    provenance: dict[str, Any],
    now: datetime,
    run_note: dict[str, Any] | None = None,
) -> list[str]:
    """Append vetted proposals as pending; returns the new ids. Re-checks
    the blocked set at write time (a concurrent approve may have landed)."""
    doc = load_watchlist(state_dir)
    blocked = blocked_symbols(doc, now)
    props: list[dict[str, Any]] = doc.setdefault("proposals", [])
    added: list[str] = []
    for item in proposals:
        if item["symbol"] in blocked:
            continue
        pid = uuid.uuid4().hex[:10]
        props.append(
            {
                "id": pid,
                "symbol": item["symbol"],
                "action": item["action"],
                "rationale": item.get("rationale", ""),
                "confidence": item.get("confidence"),
                "status": "pending",
                "created_at": now.isoformat(),
                "decided_at": None,
                "provenance": provenance,
            }
        )
        blocked.add(item["symbol"])
        added.append(pid)
    # Prune long-decided entries. The size cap only ever evicts history:
    # pending proposals and dismissals still inside their suppression
    # window are load-bearing (approve-by-id, no-revival) and always kept.
    def _protected(p: dict[str, Any]) -> bool:
        if p.get("status") == "pending":
            return True
        age = _age_seconds(p.get("decided_at"), now)
        return p.get("status") == "dismissed" and (age is None or age < DISMISS_SUPPRESS_SECONDS)

    retained = [
        p
        for p in props
        if _protected(p)
        or (_age_seconds(p.get("decided_at"), now) or 0) < DECIDED_RETAIN_SECONDS
    ]
    protected_n = sum(1 for p in retained if _protected(p))
    history_budget = max(0, MAX_PROPOSALS_KEPT - protected_n)
    history = [p for p in retained if not _protected(p)]
    keep_history = {id(p) for p in history[len(history) - history_budget :]} if history_budget else set()
    doc["proposals"] = [p for p in retained if _protected(p) or id(p) in keep_history]
    if run_note is not None:
        doc["last_proposal_run"] = {**run_note, "at": now.isoformat(), "added": len(added)}
    _save(state_dir, doc)
    return added


def _decide(
    state_dir: Path,
    doc: dict[str, Any],
    op: str,
    proposal_id: str | None,
    stamp: str,
) -> dict[str, Any]:
    prop = next(
        (p for p in doc.get("proposals", []) if p.get("id") == proposal_id), None
    )
    if prop is None:
        return {"status": "invalid", "detail": f"unknown proposal {proposal_id!r}"}
    if prop.get("status") != "pending":
        return {"status": "noop", "detail": f"proposal already {prop.get('status')}"}
    prop["status"] = "approved" if op == "approve" else "dismissed"
    prop["decided_at"] = stamp
    sym = prop["symbol"]
    if op == "approve":
        have = {row["symbol"] for row in doc["symbols"]}
        if prop["action"] == "add" and sym not in have:
            doc["symbols"].append(
                {"symbol": sym, "origin": "llm", "added_at": stamp, "proposal_id": prop["id"]}
            )
        elif prop["action"] == "remove":
            doc["symbols"] = [row for row in doc["symbols"] if row["symbol"] != sym]
    _save(state_dir, doc)
    return {"status": prop["status"], "symbol": sym, "proposal_id": prop["id"]}

