"""Exercise the public command, including its installed module entry point."""

import os
import subprocess
import sys

from tree_options.desk.__main__ import run_cli


def test_qsl_command_missing_inputs_is_readiness_failure(world, capsys, monkeypatch):
    monkeypatch.setenv("TREX_DESK_STATE", str(world.root / "state"))
    rc = run_cli(
        ["qsl", "--dry-run", "--session", str(world.session), "--store-root", str(world.store)],
        now=world.now,
        cal=world.cal,
    )
    assert rc == 3
    assert "no recorded chains" in capsys.readouterr().out
    assert not (world.root / "state").exists()


def test_module_entrypoint_registers_qsl(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "tree_options.desk", "qsl", "--help"],
        cwd=tmp_path,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "--dry-run" in result.stdout
    assert "--store-root" in result.stdout
