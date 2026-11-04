"""Restoration must not reactivate commands introduced after the original backup tool."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from tree_options.trex.desk_backup import restore, snapshot
from tree_options.trex.supervised import SupervisedPaths


@pytest.mark.parametrize(
    "relative",
    [
        "run/commands/resume.json",
        "run/commands/approve.json",
        "run/commands/approve.result.json",
        "run/proposals/pending.json",
        "run/proposal-drafts/candidate.json",
        "supervised/mandates/pending.json",
        "supervised/mandates/used.consumed.json",
    ],
)
def test_restore_archives_every_wave2_command_and_mandate(tmp_path: Path, relative: str) -> None:
    run = tmp_path / "live/run"
    supervised = SupervisedPaths(tmp_path / "live/supervised")
    run.mkdir(parents=True)
    supervised.root.mkdir()
    original = tmp_path / "live" / relative
    original.parent.mkdir(parents=True, exist_ok=True)
    original.write_bytes(b'{"historical_authority":true}')
    (run / "PAUSE_ENTRIES").write_text("preserve this restriction")
    backup = snapshot(run, supervised, tmp_path / "backups", now=datetime(2026, 11, 4, tzinfo=UTC))

    destination = tmp_path / "scratch"
    receipt = restore(backup, destination)

    assert not (destination / relative).exists(), "a restored owner must never see old commands"
    assert (destination / "archive" / relative).read_bytes() == original.read_bytes()
    assert relative in receipt["quarantined"]
    assert (destination / "run/PAUSE_ENTRIES").read_text() == "preserve this restriction"
