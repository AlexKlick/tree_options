"""Write the desk owner's status files for admission tests.

Each writer validates its document with the frozen contract model before
writing it (a test can only stage a document the books lane could have
produced), then writes it where ``contracts.layout`` puts it. The
``monitor.json`` writer uses the shape ``DeskRuntime._write_health`` writes.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tests.unit.trex_fakes import contract_fixture
from tree_options.time.sessions import shift_instant
from tree_options.trex.contracts import Reconciliation, RiskSnapshot, Wave2RunDir

ACCOUNT = "DU0000001"


def iso(at: datetime) -> str:
    return at.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _utc(now: datetime) -> datetime:
    return now.astimezone(UTC)


def write_snapshot(run_dir: Path, now: datetime, *, age_s: int = 10, **changes: Any) -> dict:
    doc = contract_fixture("risk_snapshot.json")
    doc["as_of"] = iso(shift_instant(_utc(now), -age_s))
    doc.update(changes)
    RiskSnapshot.model_validate(doc)
    path = Wave2RunDir(run_dir).risk_snapshot()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc))
    return doc


def write_reconciliation(run_dir: Path, now: datetime, *, age_s: int = 10, **changes: Any) -> dict:
    doc: dict[str, Any] = {
        "schema": "trex-reconciliation/1",
        "account_id": ACCOUNT,
        "status": "clean",
        "as_of": iso(shift_instant(_utc(now), -age_s)),
        "mismatches": [],
        "max_age_s": 120,
    }
    doc.update(changes)
    Reconciliation.model_validate(doc)
    path = Wave2RunDir(run_dir).reconciliation()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc))
    return doc


def write_monitor(run_dir: Path, now: datetime, *, age_s: float = 3, **changes: Any) -> dict:
    doc: dict[str, Any] = {
        "at": now.timestamp() - age_s,
        "started_at": now.timestamp() - 3600,
        "pid": 4242,
        "connected": True,
        "tick_failures": 0,
        "last_tick_ok_at": now.timestamp() - age_s,
        "last_error": None,
        "halt": False,
        "flatten": False,
    }
    doc.update(changes)
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "monitor.json").write_text(json.dumps(doc))
    return doc


def healthy(run_dir: Path, now: datetime) -> Path:
    """Fresh, complete, clean facts and a ready exit owner."""
    write_snapshot(run_dir, now)
    write_reconciliation(run_dir, now)
    write_monitor(run_dir, now)
    return run_dir
