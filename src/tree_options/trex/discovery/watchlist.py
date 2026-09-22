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
from datetime import datetime
from pathlib import Path
from typing import Any

SYMBOL_RE = re.compile(r"^[A-Z.]{1,6}$")


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
        # proposal lifecycle lands with M6; the op must still be valid here
        # so the spool can drain without erroring
        return {"status": "noop", "detail": "proposals not live yet"}
    else:
        return {"status": "invalid", "detail": f"unknown op {op!r}"}
    _save(state_dir, doc)
    return {"status": "ok", "symbols": [row["symbol"] for row in doc["symbols"]]}


def _save(state_dir: Path, doc: dict[str, Any]) -> None:
    path = state_dir / "watchlist.json"
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(doc, indent=2) + "\n")
    tmp.replace(path)
