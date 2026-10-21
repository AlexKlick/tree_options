"""Fact-based entry admission: the desk owner's status files decide.

Admission reads what the single supervised owner (the desk process) and
the books lane publish in the desk run dir, and turns each missing, stale
or bad fact into a specific blocker. Nothing here is a constant "not
available yet": a fresh, clean set of facts admits.

| Fact | File (``contracts.layout``) | Blocker when absent / stale / bad |
| --- | --- | --- |
| risk snapshot | ``ledger/risk-snapshot.json`` | ``risk_snapshot_stale`` (contract fn) |
| settled cash | the risk snapshot's ``cash_cents`` | ``insufficient_cash`` (negative cash) |
| reconciliation | ``ledger/reconciliation.json`` | ``reconciliation_required`` (contract fn) |
| exit owner | ``monitor.json`` (``DeskRuntime._write_health``) | ``exit_owner_not_ready`` |

``exit_owner_not_ready`` is not a ``Blocker`` member (the frozen
vocabulary has none for it); it is a plain wire string here, beside the
desk's existing ``halt`` / ``auto_off`` strings.

The exit owner is ready when ``monitor.json`` was written within
``EXIT_OWNER_HEALTH_MAX_AGE_S`` (the canary's exit-owner bound,
``desk_runtime.HEARTBEAT_MAX_AGE_S``; the runtime rewrites the file every
tick and beat), never in the future, says ``connected: true`` and counts
zero tick failures.

Fail closed: a symlinked, oversized, unparseable or contract-invalid file is
no fact (its blocker applies); a reader never raises. A fact for another
account than the one being admitted is no fact either. Every one of these
blocks ENTRIES only, never exits (the exit lane does not read them).

``entry_send_blockers`` is the kill-file part alone (``PAUSE_ENTRIES``,
``HALT``): the live entry sender (``trex.supervised_desk``) checks it before
it spends any authority and again after its screening.

The sealed selection rule's activation is NOT an owner fact: it gates
AUTOMATIC admission only (``desk.enter``); product entries are approved by
the owner one at a time.

No broker contact, no broker library import (the web backend can call it).
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Final, Literal

from pydantic import ValidationError

from tree_options.trex.contracts.layout import HALT, Wave2RunDir
from tree_options.trex.contracts.risk import (
    Reconciliation,
    RiskSnapshot,
    reconciliation_blockers,
    risk_snapshot_blockers,
)
from tree_options.trex.contracts.vocabulary import Blocker
from tree_options.trex.pause import HALTED, pause_blockers

__all__ = [
    "EXIT_OWNER_HEALTH_MAX_AGE_S",
    "EXIT_OWNER_NOT_READY",
    "HALTED",
    "MAX_FACT_BYTES",
    "MONITOR_FILE",
    "cash_blockers",
    "entry_blockers",
    "entry_send_blockers",
    "exit_owner_blockers",
    "exit_owner_ready",
    "owner_fact_blockers",
    "owner_fact_report",
    "read_exit_owner_health",
    "read_reconciliation",
    "read_risk_snapshot",
]

#: wire string for an exit owner whose health is absent, stale or failing
EXIT_OWNER_NOT_READY: Final = "exit_owner_not_ready"
#: = ``desk_runtime.HEARTBEAT_MAX_AGE_S`` (pinned by a test; not imported:
#: desk_runtime pulls in the broker adapter)
EXIT_OWNER_HEALTH_MAX_AGE_S: Final = 30
#: the desk runtime's health file (``DeskPaths.health``)
MONITOR_FILE: Final = "monitor.json"
#: a status file larger than this is not a fact
MAX_FACT_BYTES: Final = 1 << 20

_State = Literal["absent", "unreadable", "present"]


def _require_aware(now: datetime) -> None:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")


def _load(path: Path) -> tuple[_State, Any]:
    """(state, parsed JSON). Never follows a symlink, never raises."""
    if not os.path.lexists(path):
        return "absent", None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            raw = stream.read(MAX_FACT_BYTES + 1)
        if len(raw) > MAX_FACT_BYTES:
            return "unreadable", None
        return "present", json.loads(raw)
    except (OSError, ValueError):
        return "unreadable", None


def _risk_snapshot(run_dir: Path) -> tuple[_State, RiskSnapshot | None]:
    state, doc = _load(Wave2RunDir(Path(run_dir)).risk_snapshot())
    if state != "present":
        return state, None
    try:
        return "present", RiskSnapshot.model_validate(doc)
    except (ValidationError, ValueError, TypeError):
        return "unreadable", None


def _reconciliation(run_dir: Path) -> tuple[_State, Reconciliation | None]:
    state, doc = _load(Wave2RunDir(Path(run_dir)).reconciliation())
    if state != "present":
        return state, None
    try:
        return "present", Reconciliation.model_validate(doc)
    except (ValidationError, ValueError, TypeError):
        return "unreadable", None


def _health(run_dir: Path) -> tuple[_State, dict[str, Any] | None]:
    state, doc = _load(Path(run_dir) / MONITOR_FILE)
    if state != "present":
        return state, None
    if not isinstance(doc, dict):
        return "unreadable", None
    return "present", doc


def read_risk_snapshot(run_dir: Path) -> RiskSnapshot | None:
    """The latest ``trex-risk-snapshot/1``; None when absent or not a valid one."""
    return _risk_snapshot(run_dir)[1]


def read_reconciliation(run_dir: Path) -> Reconciliation | None:
    """The latest ``trex-reconciliation/1``; None when absent or not a valid one."""
    return _reconciliation(run_dir)[1]


def read_exit_owner_health(run_dir: Path) -> dict[str, Any] | None:
    """The desk runtime's ``monitor.json`` object; None when absent or unreadable."""
    return _health(run_dir)[1]


def _health_age_s(health: dict[str, Any], now: datetime) -> float | None:
    at = health.get("at")
    if isinstance(at, bool) or not isinstance(at, int | float):
        return None
    return now.timestamp() - float(at)


def exit_owner_ready(health: dict[str, Any] | None, now: datetime) -> bool:
    """Fresh (within the bound, not future-dated), connected, no tick failures."""
    _require_aware(now)
    if not isinstance(health, dict):
        return False
    age = _health_age_s(health, now)
    if age is None or not 0 <= age <= EXIT_OWNER_HEALTH_MAX_AGE_S:
        return False
    if health.get("connected") is not True:
        return False
    failures = health.get("tick_failures")
    return type(failures) is int and failures == 0


def exit_owner_blockers(health: dict[str, Any] | None, now: datetime) -> tuple[str, ...]:
    return () if exit_owner_ready(health, now) else (EXIT_OWNER_NOT_READY,)


def cash_blockers(
    snapshot: RiskSnapshot | None, now: datetime, required_cents: int
) -> tuple[str, ...]:
    """``insufficient_cash`` when settled cash is negative (a cash-only account
    is borrowing) or below ``required_cents``. Without a usable snapshot this
    says nothing: ``risk_snapshot_stale`` already blocks."""
    _require_aware(now)
    if type(required_cents) is not int or required_cents < 0:
        raise ValueError("required_cents must be a non-negative int")
    if snapshot is None or not snapshot.usable_at(now) or snapshot.cash_cents is None:
        return ()
    cash = snapshot.cash_cents
    if cash < 0 or required_cents > cash:
        return (Blocker.INSUFFICIENT_CASH.value,)
    return ()


def _bound_facts(
    run_dir: Path, account_id: str | None
) -> tuple[RiskSnapshot | None, Reconciliation | None]:
    """The facts for one account: ``account_id``, or else the snapshot's."""
    snapshot = read_risk_snapshot(run_dir)
    reconciliation = read_reconciliation(run_dir)
    account = account_id
    if account is None and snapshot is not None:
        account = snapshot.account_id
    if account is not None:
        if snapshot is not None and snapshot.account_id != account:
            snapshot = None
        if reconciliation is not None and reconciliation.account_id != account:
            reconciliation = None
    return snapshot, reconciliation


def owner_fact_blockers(
    run_dir: Path, now: datetime, *, account_id: str | None = None
) -> tuple[str, ...]:
    """Blockers from the owner's status files (order: risk snapshot, cash,
    reconciliation, exit owner). Empty = the facts admit an entry."""
    _require_aware(now)
    snapshot, reconciliation = _bound_facts(Path(run_dir), account_id)
    blockers: list[str] = [b.value for b in risk_snapshot_blockers(snapshot, now)]
    blockers += cash_blockers(snapshot, now, 0)
    blockers += [b.value for b in reconciliation_blockers(reconciliation, now)]
    blockers += exit_owner_blockers(read_exit_owner_health(Path(run_dir)), now)
    return tuple(blockers)


def entry_send_blockers(run_dir: Path) -> tuple[str, ...]:
    """The kill files every entry SENDER checks before it spends authority
    (mandate, intent, permit, spec, broker order), and again just before
    the send: ``entries_paused`` while ``PAUSE_ENTRIES`` stands, ``halt``
    while ``HALT`` stands (``lexists``: a dangling link still stops).

    No owner fact is read here, so a sender that predates the books lane
    keeps its own screening. Exits never call this (``pause.exit_blockers``).
    """
    root = Path(run_dir)
    kill = (HALTED,) if os.path.lexists(root / HALT) else ()
    return (*pause_blockers(root), *kill)


def entry_blockers(
    run_dir: Path, now: datetime, *, account_id: str | None = None
) -> tuple[str, ...]:
    """Everything that refuses an owner-approved entry right now: the
    entries-only pause, a HALT, then the owner's facts."""
    root = Path(run_dir)
    return (*entry_send_blockers(root), *owner_fact_blockers(root, now, account_id=account_id))


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def owner_fact_report(run_dir: Path, now: datetime) -> dict[str, Any]:
    """A read-only view of the facts for status displays (ages in seconds)."""
    _require_aware(now)
    root = Path(run_dir)
    report: dict[str, Any] = {}

    state, snapshot = _risk_snapshot(root)
    if snapshot is None:
        report["risk_snapshot"] = {"state": state, "usable": False}
    else:
        report["risk_snapshot"] = {
            "state": state,
            "status": snapshot.status,
            "account_id": snapshot.account_id,
            "as_of": _iso(snapshot.as_of),
            "age_s": round((now - snapshot.as_of).total_seconds()),
            "usable": snapshot.usable_at(now),
            "cash_cents": snapshot.cash_cents,
            "open_loss_cents": snapshot.open_loss_cents,
        }

    state, reconciliation = _reconciliation(root)
    if reconciliation is None:
        report["reconciliation"] = {"state": state, "clean": False}
    else:
        report["reconciliation"] = {
            "state": state,
            "status": reconciliation.status,
            "account_id": reconciliation.account_id,
            "as_of": _iso(reconciliation.as_of),
            "age_s": round((now - reconciliation.as_of).total_seconds()),
            "mismatches": len(reconciliation.mismatches),
            "clean": reconciliation.clean_at(now),
        }

    state, health = _health(root)
    exit_owner: dict[str, Any] = {"state": state, "ready": exit_owner_ready(health, now)}
    if health is not None:
        age = _health_age_s(health, now)
        exit_owner.update(
            age_s=None if age is None else round(age),
            connected=health.get("connected"),
            tick_failures=health.get("tick_failures"),
        )
    report["exit_owner"] = exit_owner
    report["blockers"] = list(owner_fact_blockers(root, now))
    return report
