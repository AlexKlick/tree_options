"""``GET /api/portfolio``: the desk owner's books as the files stand (lane G).

Read-only and broker-free. Every figure comes from the books lane's
projections under ``<desk run dir>/ledger`` (``Wave2RunDir``):

- ``holdings.json``: the books lane's ``trex-holdings/1`` document, parsed
  by its own model (``trex.ledger.HoldingsDocument``; coordinator decision
  2026-10-21: every reader uses it). Any other shape (a bare list, a dict
  without the schema, a float, an extra field) is ``holdings_invalid``;
- ``risk-snapshot.json``: ``trex-risk-snapshot/1`` (cash, risk, outside
  positions);
- ``reconciliation.json``: ``trex-reconciliation/1``.

An absent, unreadable or invalid file is ``unavailable`` with a reason and
null figures, never a zero; one invalid holding makes the whole list
unavailable (a partial list would undercount), and so does a projection
whose rows name another account than the document. A stale snapshot keeps
its figures and says ``stale``. Entry blockers come from the contract's own
blocker functions plus ``PAUSE_ENTRIES``; exits are blocked by ``HALT``
only (the pause never blocks an exit).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError

from tree_options.trex.contracts import (
    Blocker,
    Holding,
    Reconciliation,
    RiskSnapshot,
    Wave2RunDir,
    model_document,
    reconciliation_blockers,
    risk_snapshot_blockers,
)
from tree_options.trex.contracts.layout import FLATTEN, HALT, PAUSE_ENTRIES
from tree_options.trex.ledger import HOLDINGS_SCHEMA, HoldingsDocument

__all__ = ["PORTFOLIO_SCHEMA", "attach", "portfolio_payload"]

PORTFOLIO_SCHEMA: Final = "trex-portfolio/1"
#: A projection bigger than this is not the books lane's; refuse to parse it.
MAX_BOOK_BYTES: Final = 4 * 1024 * 1024
#: IBKR paper account ids start with ``D`` (``DU...``); live ones do not.
PAPER_ACCOUNT_PREFIX: Final = "D"


@dataclass(frozen=True)
class _Read[M: BaseModel]:
    value: M | None
    reason: str | None


def _iso(instant: datetime) -> str:
    return instant.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _age_s(as_of: datetime, now: datetime) -> int:
    return int((now - as_of).total_seconds())


def _load_json(path: Path, label: str) -> tuple[Any, str | None]:
    """(document, None) or (None, ``<label>_missing``/``<label>_unreadable``)."""
    try:
        if path.is_symlink() or path.stat().st_size > MAX_BOOK_BYTES:
            return None, f"{label}_unreadable"
        return json.loads(path.read_bytes()), None
    except FileNotFoundError:
        return None, f"{label}_missing"
    except (OSError, ValueError):
        return None, f"{label}_unreadable"


def _read_model[M: BaseModel](path: Path, model: type[M], label: str) -> _Read[M]:
    document, reason = _load_json(path, label)
    if reason is not None:
        return _Read(None, reason)
    try:
        return _Read(model.model_validate(document), None)
    except ValidationError:
        return _Read(None, f"{label}_invalid")


def _holdings_unavailable(reason: str) -> dict[str, Any]:
    return {
        "status": "unavailable",
        "reason": reason,
        "account_id": None,
        "as_of": None,
        "age_s": None,
        "ledger_cash_micros": None,
        "rows": None,
    }


def _holdings(path: Path, now: datetime) -> tuple[dict[str, Any], set[str]]:
    """The ``trex-holdings/1`` block, and every account the document names."""
    document, reason = _load_json(path, "holdings")
    if reason is not None:
        return _holdings_unavailable(reason), set()
    # the model defaults ``schema``; a reader requires the books lane to say it
    if not isinstance(document, dict) or document.get("schema") != HOLDINGS_SCHEMA:
        return _holdings_unavailable("holdings_invalid"), set()
    try:
        projection = HoldingsDocument.model_validate(document)
    except ValidationError:
        return _holdings_unavailable("holdings_invalid"), set()
    holdings: tuple[Holding, ...] = projection.holdings
    accounts = {projection.account_id, *(h.account_id for h in holdings)}
    if len(accounts) > 1:
        return _holdings_unavailable("holdings_account_mismatch"), accounts
    block = {
        "status": "ok",
        "reason": None,
        "account_id": projection.account_id,
        "as_of": _iso(projection.as_of),
        "age_s": _age_s(projection.as_of, now),
        "ledger_cash_micros": projection.cash_micros,
        "rows": [model_document(h) for h in holdings],
    }
    return block, accounts


def _risk_blocks(
    read: _Read[RiskSnapshot], now: datetime
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """(cash, risk, outside_positions) from one snapshot read."""
    snapshot = read.value
    status, reason = "unavailable", read.reason
    if snapshot is not None:
        if snapshot.status == "unavailable":
            reason = "risk_snapshot_unavailable"
        elif snapshot.usable_at(now):
            status, reason = "fresh", None
        else:
            status, reason = "stale", Blocker.RISK_SNAPSHOT_STALE.value
    figures = snapshot if snapshot is not None and snapshot.status == "complete" else None
    as_of = None if snapshot is None else _iso(snapshot.as_of)
    age = None if snapshot is None else _age_s(snapshot.as_of, now)
    cash = {
        "status": status,
        "reason": reason,
        "settled_cash_cents": None if figures is None else figures.cash_cents,
        "as_of": as_of,
        "age_s": age,
    }
    risk = {
        "status": status,
        "reason": reason,
        "account_id": None if snapshot is None else snapshot.account_id,
        "as_of": as_of,
        "age_s": age,
        "stale_after_s": None if snapshot is None else snapshot.stale_after_s,
        "open_loss_cents": None if figures is None else figures.open_loss_cents,
        "day_realized_loss_cents": None if figures is None else figures.day_realized_loss_cents,
        "equity_gross_cents": None if figures is None else figures.equity_gross_cents,
    }
    outside = {
        "status": status,
        "reason": reason,
        "rows": (
            None if figures is None else [model_document(row) for row in figures.outside_positions]
        ),
    }
    return cash, risk, outside


def _reconciliation_block(read: _Read[Reconciliation], now: datetime) -> dict[str, Any]:
    rec = read.value
    if rec is None:
        return {
            "status": "unavailable",
            "reason": read.reason,
            "fresh": False,
            "as_of": None,
            "age_s": None,
            "mismatches": None,
        }
    age = _age_s(rec.as_of, now)
    seconds = (now - rec.as_of).total_seconds()
    fresh = 0 <= seconds <= rec.max_age_s
    if rec.status == "clean":
        reason = None if fresh else "reconciliation_stale"
    elif rec.status == "reconciliation_required":
        reason = Blocker.RECONCILIATION_REQUIRED.value
    else:
        reason = "reconciliation_unavailable"
    return {
        "status": rec.status,
        "reason": reason,
        "fresh": fresh,
        "as_of": _iso(rec.as_of),
        "age_s": age,
        "mismatches": (
            None if rec.status == "unavailable" else [model_document(m) for m in rec.mismatches]
        ),
    }


def portfolio_payload(run_dir: Path, now: datetime) -> dict[str, Any]:
    """The ``trex-portfolio/1`` document (see the module docstring)."""
    layout = Wave2RunDir(run_dir)
    holdings, accounts = _holdings(layout.holdings(), now)
    risk_read = _read_model(layout.risk_snapshot(), RiskSnapshot, "risk_snapshot")
    rec_read = _read_model(layout.reconciliation(), Reconciliation, "reconciliation")
    cash, risk, outside = _risk_blocks(risk_read, now)
    kill_files = {name: (run_dir / name).exists() for name in (PAUSE_ENTRIES, HALT, FLATTEN)}

    blockers = {
        *risk_snapshot_blockers(risk_read.value, now),
        *reconciliation_blockers(rec_read.value, now),
    }
    if kill_files[PAUSE_ENTRIES]:
        blockers.add(Blocker.ENTRIES_PAUSED)

    for read in (risk_read, rec_read):
        if read.value is not None:
            accounts.add(read.value.account_id)
    problems: list[dict[str, str]] = []
    if len(accounts) > 1:
        problems.append(
            {"reason": "account_mismatch", "detail": "the books name more than one account"}
        )
    problems.extend(
        {"reason": "non_paper_account", "account_id": account}
        for account in sorted(accounts)
        if not account.startswith(PAPER_ACCOUNT_PREFIX)
    )
    return {
        "schema": PORTFOLIO_SCHEMA,
        "now": _iso(now),
        "account_ids": sorted(accounts),
        "holdings": holdings,
        "cash": cash,
        "risk": risk,
        "outside_positions": outside,
        "reconciliation": _reconciliation_block(rec_read, now),
        "kill_files": kill_files,
        "entry_blockers": sorted(str(b) for b in blockers),
        "exits_blocked": kill_files[HALT],
        "problems": problems,
        "execution_enabled": False,
    }


def attach(app: FastAPI, *, run_dir: Callable[[], Path], clock: Callable[[], datetime]) -> None:
    """Mount ``GET /api/portfolio``; the run dir resolves per request."""

    @app.get("/api/portfolio")
    def portfolio() -> JSONResponse:
        return JSONResponse(
            portfolio_payload(run_dir(), clock()), headers={"Cache-Control": "no-store"}
        )
