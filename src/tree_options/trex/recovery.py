"""Assess local custody artifacts without contacting a broker or authorizing execution.

A verified local backup and scratch restore permit preparation of a read-only
recovery viewer. They never prove current positions, exits, off-host recovery,
or readiness to replace the execution owner.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tree_options.trex.desk_backup import MANIFEST, RECEIPT, RESTORE_SCHEMA, quarantined, verify

SCHEMA = "trex-recovery-assessment/1"
INVENTORY_MAX_AGE_S = 300


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_restoration(backup: Path, restored: Path) -> dict[str, Any]:
    """Rehash the restored bytes and independently check their active/archive disposition."""
    manifest = verify(backup)
    receipt = json.loads((restored / RECEIPT).read_bytes())
    if (
        receipt.get("schema") != RESTORE_SCHEMA
        or receipt.get("backup_manifest_sha256") != _hash(backup / MANIFEST)
        or receipt.get("backup_owner_epoch") != manifest.get("owner_epoch")
        or receipt.get("backup_taken_at") != manifest.get("taken_at")
    ):
        raise ValueError("restore_provenance_mismatch")
    expected = {RECEIPT}
    archived: set[str] = set()
    active: set[str] = set()
    for entry in manifest["files"]:
        rel = entry["path"]
        held = quarantined(rel)
        target = f"archive/{rel}" if held else rel
        expected.add(target)
        (archived if held else active).add(rel)
        path = restored / target
        if path.is_symlink() or not path.is_file() or _hash(path) != entry["sha256"]:
            raise ValueError("restore_file_mismatch")
    if any(path.is_symlink() for path in restored.rglob("*")):
        raise ValueError("restore_symlink")
    actual = {str(path.relative_to(restored)) for path in restored.rglob("*") if path.is_file()}
    if actual != expected:
        raise ValueError("restore_unexpected_files")
    if (
        set(receipt.get("restored", [])) != active
        or set(receipt.get("quarantined", [])) != archived
    ):
        raise ValueError("restore_disposition_mismatch")
    return {"files": len(manifest["files"]), "archived_authority_files": len(archived)}


def assess(inventory: Path, backup: Path, restored: Path, *, now: datetime) -> dict[str, Any]:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("recovery_clock_naive")
    checks: dict[str, Any] = {}
    issues: list[str] = []
    try:
        doc = json.loads(inventory.read_bytes())
        stamp = datetime.fromisoformat(doc["generated_at"])
        age = (now - stamp).total_seconds()
        if (
            doc.get("schema") != "trex-inventory/1"
            or doc.get("read_only") is not True
            or doc.get("errors") != []
            or not 0 <= age <= INVENTORY_MAX_AGE_S
        ):
            raise ValueError("inventory_unusable")
        checks["inventory"] = {"status": "PASS", "sha256": _hash(inventory)}
    except (OSError, ValueError, TypeError, KeyError):
        issues.append("inventory_unusable")
        checks["inventory"] = {"status": "BLOCKED"}
    try:
        details = verify_restoration(backup, restored)
        checks["local_custody"] = {
            "status": "PASS",
            "backup_manifest_sha256": _hash(backup / MANIFEST),
            **details,
        }
    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
        issues.append("local_custody_unverified")
        checks["local_custody"] = {"status": "BLOCKED"}
    return {
        "schema": SCHEMA,
        "as_of": now.astimezone(UTC).isoformat(),
        "status": "BLOCKED",
        "read_only_recovery_artifacts_verified": not issues,
        "execution_authorized": False,
        "live_authorized": False,
        "checks": checks,
        "blockers": [
            *issues,
            "current_broker_reconciliation_unverified",
            "current_exit_coverage_unverified",
            "off_host_restore_unverified",
            "off_host_alert_delivery_unverified",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--backup", type=Path, required=True)
    parser.add_argument("--restored", type=Path, required=True)
    args = parser.parse_args(argv)
    print(
        json.dumps(
            assess(args.inventory, args.backup, args.restored, now=datetime.now(UTC)), indent=2
        )
    )
    return 3  # local custody never closes P0's broker and off-host gates


if __name__ == "__main__":
    sys.exit(main())
