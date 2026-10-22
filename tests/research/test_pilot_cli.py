"""Wave 4 lane I: the loopback pilot CLI (``python -m tree_options.research.pilot``).

The owner seals from the loopback CLI by naming their email explicitly; the
email must be in ``TREX_OWNER_EMAILS``. Exit 0 = done, 2 = refused (the
reason on stderr), nothing else is ever written on a refusal.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from tree_options.research.pilot.cli import main

REPO = Path(__file__).resolve().parents[2]
DRAFTS = REPO / "data" / "research" / "pilot"
OWNER = "owner@example.com"
OBSERVATION = "qsl-observation"
BASELINE = "eq-baseline-spy-weekly"
SEAL_FIELDS = ("status", "sealed_by", "sealed_at", "sha256")


def reviewed_sha(program_id: str) -> str:
    document = json.loads((DRAFTS / f"{program_id}.draft.json").read_text())
    for name in SEAL_FIELDS:
        document.pop(name)
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@pytest.fixture()
def pilot_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "pilot"
    monkeypatch.setenv("TREX_PILOT_DIR", str(root))
    monkeypatch.setenv("TREX_OWNER_EMAILS", OWNER)
    return root


def test_import_then_list(pilot_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["import-drafts", str(DRAFTS)]) == 0
    imported = capsys.readouterr().out
    assert f"{BASELINE} draft {reviewed_sha(BASELINE)}" in imported
    assert main(["list"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert [row["program_id"] for row in listed["programs"]] == [
        OBSERVATION,
        BASELINE,
        "eq-challenger-spy-reversal",
    ]
    assert listed["live_authorized"] is False


def test_the_owner_seals_by_naming_their_email(
    pilot_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    main(["import-drafts", str(DRAFTS)])
    capsys.readouterr()
    code = main(
        [
            "seal",
            "--program-id",
            OBSERVATION,
            "--sha256",
            reviewed_sha(OBSERVATION),
            "--owner-email",
            OWNER,
        ]
    )
    assert code == 0
    sealed = json.loads((pilot_dir / "registrations" / f"{OBSERVATION}.sealed.json").read_text())
    assert (sealed["status"], sealed["sealed_by"]) == ("sealed", OWNER)


@pytest.mark.parametrize(
    ("argv_tail", "reason"),
    [
        (["--owner-email", "intruder@example.com"], "sealer_not_owner"),
        (["--owner-email", OWNER, "--sha256", "0" * 64], "registration_sha256_mismatch"),
    ],
    ids=["not-owner", "unreviewed-hash"],
)
def test_a_refused_seal_exits_2_and_writes_nothing(
    pilot_dir: Path, capsys: pytest.CaptureFixture[str], argv_tail: list[str], reason: str
) -> None:
    main(["import-drafts", str(DRAFTS)])
    capsys.readouterr()
    argv = ["seal", "--program-id", OBSERVATION, "--sha256", reviewed_sha(OBSERVATION)]
    assert main(argv + argv_tail) == 2
    assert reason in capsys.readouterr().err
    assert not (pilot_dir / "registrations" / f"{OBSERVATION}.sealed.json").exists()


def test_a_malformed_owner_list_refuses(
    pilot_dir: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    main(["import-drafts", str(DRAFTS)])
    monkeypatch.setenv("TREX_OWNER_EMAILS", "not-an-email")
    argv = ["seal", "--program-id", OBSERVATION, "--sha256", reviewed_sha(OBSERVATION)]
    assert main([*argv, "--owner-email", OWNER]) == 2
    assert "owners_unconfigured" in capsys.readouterr().err


def test_usage_and_release_of_a_lost_job(
    pilot_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from datetime import UTC, date, datetime

    from tree_options.research.pilot.usage import UsageMeter

    meter = UsageMeter(pilot_dir)
    day = date(2026, 10, 22)
    at = datetime(2026, 10, 22, 14, 0, tzinfo=UTC)
    meter.charge(BASELINE, day, requests=1, at=at)
    meter.open_job(BASELINE, "e" * 64, day, at=at)
    assert main(["usage", "--program-id", BASELINE, "--day", "2026-10-22"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["usage"] is None
    assert shown["usage_unavailable_reason"] == "budget_usage_unknown:job_in_flight"
    assert shown["jobs_in_flight"] == ["e" * 64]
    argv = ["release", "--program-id", BASELINE, "--run-id", "e" * 64, "--compute-seconds", "90"]
    assert main(argv) == 0
    capsys.readouterr()
    main(["usage", "--program-id", BASELINE, "--day", "2026-10-22"])
    shown = json.loads(capsys.readouterr().out)
    assert shown["usage"]["compute_seconds"] == 90
    assert shown["jobs_in_flight"] == []


def test_module_entry_point_runs(tmp_path: Path) -> None:
    env = {"PYTHONPATH": str(REPO / "src"), "TREX_PILOT_DIR": str(tmp_path), "HOME": str(tmp_path)}
    done = subprocess.run(
        [sys.executable, "-B", "-m", "tree_options.research.pilot", "list"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout)["programs"] == []
