"""Custody reports re-derive evidence and cannot manufacture broker readiness."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tree_options.trex.desk_backup import restore, snapshot
from tree_options.trex.recovery import assess, verify_restoration
from tree_options.trex.supervised import SupervisedPaths

NOW = datetime(2026, 11, 4, 17, tzinfo=UTC)


@pytest.fixture
def custody(tmp_path: Path) -> tuple[Path, Path, Path]:
    run = tmp_path / "live/run"
    sup = SupervisedPaths(tmp_path / "live/sup")
    run.mkdir(parents=True)
    sup.root.mkdir()
    (run / "book.json").write_text('{"structures": {}}')
    (run / "commands").mkdir()
    (run / "commands/resume.json").write_text('{"kind":"resume_all"}')
    backup = snapshot(run, sup, tmp_path / "backups", now=NOW)
    scratch = tmp_path / "scratch"
    restore(backup, scratch, now=NOW)
    inventory = tmp_path / "inventory.json"
    inventory.write_text(
        json.dumps(
            {
                "schema": "trex-inventory/1",
                "generated_at": NOW.isoformat(),
                "read_only": True,
                "errors": [],
            }
        )
    )
    return inventory, backup, scratch


def test_local_artifacts_never_prove_broker_or_live_readiness(
    custody: tuple[Path, Path, Path],
) -> None:
    report = assess(*custody, now=NOW)
    assert report["read_only_recovery_artifacts_verified"] is True
    assert report["status"] == "BLOCKED"
    assert report["execution_authorized"] is report["live_authorized"] is False
    assert "current_broker_reconciliation_unverified" in report["blockers"]


@pytest.mark.parametrize("tamper", ["active_command", "book", "receipt", "extra", "link"])
def test_restore_rechecks_bytes_and_quarantine(
    custody: tuple[Path, Path, Path], tamper: str
) -> None:
    _, backup, scratch = custody
    if tamper == "active_command":
        (scratch / "run/commands").mkdir()
        (scratch / "archive/run/commands/resume.json").rename(scratch / "run/commands/resume.json")
    elif tamper == "book":
        (scratch / "run/book.json").write_text("changed")
    elif tamper == "receipt":
        receipt = json.loads((scratch / "RESTORE.json").read_text())
        receipt["backup_manifest_sha256"] = "0" * 64
        (scratch / "RESTORE.json").write_text(json.dumps(receipt))
    elif tamper == "extra":
        (scratch / "run/owner.json").write_text("old owner")
    else:
        (scratch / "other").symlink_to(scratch / "run", target_is_directory=True)
    with pytest.raises(ValueError):
        verify_restoration(backup, scratch)
    assert assess(*custody, now=NOW)["read_only_recovery_artifacts_verified"] is False


def test_stale_inventory_is_blocked(custody: tuple[Path, Path, Path]) -> None:
    report = assess(*custody, now=datetime(2026, 11, 5, tzinfo=UTC))
    assert "inventory_unusable" in report["blockers"]
