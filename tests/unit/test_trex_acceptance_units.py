"""Wave 4 lane H: the acceptance publisher's release-gated unit templates.

Like the evidence and experiment publishers: the release identity check
runs first from the release interpreter, the job is sandboxed, its only
write root is its own acceptance directory inside the release's
``desk_state``, and the command it runs is one the release seals. The
generic render rules (every template, every release unit) are pinned by
``tests/unit/test_trex_product_release.py``; this file pins this pair.
"""

from __future__ import annotations

from pathlib import Path

from tree_options.trex import release

TEMPLATES = Path(__file__).resolve().parents[2] / "deploy/product"
SERVICE = TEMPLATES / "trex-acceptance-publish.service.in"
TIMER = TEMPLATES / "trex-acceptance-publish.timer.in"
PYTHON = "@SOURCE@/.venv/bin/python"


def directives(path: Path) -> list[tuple[str, str]]:
    pairs = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith(("#", ";", "[")):
            continue
        key, _, value = line.partition("=")
        pairs.append((key, value))
    return pairs


def values(path: Path, key: str) -> list[str]:
    return [v for k, v in directives(path) if k == key]


def test_gate_first_then_the_sealed_publish_command() -> None:
    execs = [(k, v) for k, v in directives(SERVICE) if k in release.EXEC_KEYS]
    assert execs[0] == ("ExecStartPre", f"{PYTHON} -m tree_options.trex.premarket --check")
    (start,) = values(SERVICE, "ExecStart")
    assert start.startswith(f"{PYTHON} -m tree_options.trex.acceptance publish")
    assert release._unit_commands(SERVICE.read_text()) == [
        "tree_options.trex.premarket",
        "tree_options.trex.acceptance publish",
    ]
    assert "tree_options.trex.acceptance publish" in release.COMMANDS
    assert "--armed" not in start.split()


def test_release_pinned_environment() -> None:
    env = values(SERVICE, "Environment")
    assert "PYTHONPATH=@SOURCE@/src" in env
    assert "TREX_RELEASE_DIR=@RELEASE_DIR@" in env
    assert "TREX_PREMARKET_RUNS=@PREMARKET_RUNS@" in env
    assert "TREX_ACCEPTANCE_DIR=@DESK_STATE@/acceptance" in env
    assert values(SERVICE, "WorkingDirectory") == ["@SOURCE@"]


def test_sandboxed_and_writes_only_its_acceptance_dir() -> None:
    pairs = dict(directives(SERVICE))
    assert pairs["ProtectSystem"] == "strict"
    assert pairs["ProtectHome"] == "read-only"
    assert pairs["NoNewPrivileges"] == "true"
    assert pairs["PrivateTmp"] == "true"
    assert pairs["UMask"] == "0077"
    assert pairs["Type"] == "oneshot"
    assert values(SERVICE, "ReadWritePaths") == ["@DESK_STATE@/acceptance"]
    assert "3" in pairs["SuccessExitStatus"].split()  # degraded is a valid publication
    for key, value in directives(SERVICE):
        if key in release.EXEC_KEYS:
            assert "host-work" not in value and "host-test" not in value


def test_owner_list_comes_from_an_optional_private_env_file() -> None:
    assert values(SERVICE, "EnvironmentFile") == ["-%h/.config/trex/acceptance.env"]


def test_every_production_reader_reads_the_directory_the_publisher_writes() -> None:
    """Integration (review P2): the dead-man sweep (streak alert) and the
    production web (Today, Activity, GET /api/acceptance) read the same
    TREX_ACCEPTANCE_DIR the publisher writes; staging names none."""
    (written,) = [v for v in values(SERVICE, "Environment") if v.startswith("TREX_ACCEPTANCE_DIR=")]
    for reader in ("trex-deadman.service.in", "trex-web-release.conf.in"):
        read = [
            v
            for v in values(TEMPLATES / reader, "Environment")
            if v.startswith("TREX_ACCEPTANCE_DIR=")
        ]
        assert read == [written], reader
    staging = values(TEMPLATES / "trex-web-staging-release.conf.in", "Environment")
    assert not [v for v in staging if v.startswith("TREX_ACCEPTANCE_DIR=")]


def test_timer_samples_at_least_twice_a_minute() -> None:
    pairs = dict(directives(TIMER))
    assert pairs["OnUnitInactiveSec"] == "20s"
    assert pairs["AccuracySec"] == "1s"
    assert pairs["WantedBy"] == "timers.target"
