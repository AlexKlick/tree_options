"""Closeout ops artifacts: checklist schema, runbook content, template guard.

The checklist is the machine-readable closeout sequence; the runbook is the
human procedure; the systemd template is documentation for a FUTURE
owner-authorized supervisor. These tests pin: the checklist is well-formed
and every script it names exists and answers --help; the runbook carries
the seven CLI contracts and the standing hard rules; the template stays a
template (and no unit is installed on this host).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

CHECKLIST = REPO_ROOT / "docs" / "m4-closeout-checklist.json"
RUNBOOK = REPO_ROOT / "docs" / "m4-closeout-runbook.md"
TEMPLATE = REPO_ROOT / "templates" / "systemd-user" / "tree-options-era.service"
TEMPLATE_README = REPO_ROOT / "templates" / "systemd-user" / "README.md"

SEVEN_SCRIPTS = (
    "scripts/runstate_mark.py",
    "scripts/era_status.py",
    "scripts/gen_coverage_universe.py",
    "scripts/build_coverage_census.py",
    "scripts/build_protocol_amendment.py",
    "scripts/launch_bars_era.py",
    "scripts/g4_seal.py",
)

EXPECTED_KEYS = {"id", "command", "expect_exit", "description", "mutates"}

# --help is pure argparse and never mutates; cache so the suite pays for
# each script's process spawn exactly once.
_HELP_CACHE: dict[str, int] = {}


def _help_exit(script: str) -> int:
    if script not in _HELP_CACHE:
        proc = subprocess.run(
            ["uv", "run", "--frozen", "python", script, "--help"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        _HELP_CACHE[script] = proc.returncode
    return _HELP_CACHE[script]


def _load_checklist() -> list[dict]:
    raw = json.loads(CHECKLIST.read_text(encoding="utf-8"))
    assert isinstance(raw, list) and raw, "checklist must be a non-empty array"
    return raw


# ---- checklist --------------------------------------------------------------------


def test_checklist_entries_have_exactly_the_five_typed_keys() -> None:
    for entry in _load_checklist():
        assert set(entry) == EXPECTED_KEYS, entry.get("id", "<no id>")
        assert isinstance(entry["id"], str) and entry["id"]
        assert isinstance(entry["command"], list) and all(
            isinstance(tok, str) for tok in entry["command"]
        )
        assert isinstance(entry["expect_exit"], int) and not isinstance(entry["expect_exit"], bool)
        assert isinstance(entry["description"], str) and entry["description"]
        assert isinstance(entry["mutates"], bool)


def test_checklist_ids_unique() -> None:
    ids = [entry["id"] for entry in _load_checklist()]
    assert len(ids) == len(set(ids))


def test_checklist_commands_resolve_to_real_binaries_and_repo_scripts() -> None:
    for entry in _load_checklist():
        command = entry["command"]
        assert shutil.which(command[0]) is not None, (entry["id"], command[0])
        for token in command:
            if token.startswith("scripts/"):
                assert (REPO_ROOT / token).is_file(), (entry["id"], token)


def test_checklist_references_every_closeout_script_and_help_exits_zero() -> None:
    referenced: set[str] = set()
    for entry in _load_checklist():
        for token in entry["command"]:
            if token.startswith("scripts/"):
                referenced.add(token)
    for script in SEVEN_SCRIPTS:
        assert script in referenced, f"checklist never references {script}"
    for script in sorted(referenced):
        assert _help_exit(script) == 0, f"{script} --help exited non-zero"


def test_mutating_entries_say_what_they_mutate() -> None:
    for entry in _load_checklist():
        if entry["mutates"]:
            low = entry["description"].lower()
            assert "mutat" in low or "mutating" in low, entry["id"]


def test_readonly_entries_never_claim_to_mutate() -> None:
    for entry in _load_checklist():
        if not entry["mutates"]:
            assert "mutat" not in entry["description"].lower(), entry["id"]


def test_checklist_never_instructs_appending_a_concrete_era_exit() -> None:
    """Round-3 review fix (2026-08-23, finding 5): the launcher records
    ERA_EXIT itself (runbook 4.1); the operator may only append
    ERA_EXIT=UNKNOWN when the launcher's line is missing — the checklist
    must not teach hand-writing a concrete code."""
    for entry in _load_checklist():
        low = entry["description"].lower()
        assert "append the era_exit=<code> line by hand" not in low, entry["id"]
        if "append" in low and "era_exit" in low:
            assert "unknown" in low, entry["id"]


# ---- runbook ----------------------------------------------------------------------


def test_runbook_mismatch_row_names_reconciliation() -> None:
    """Round-3 review fix (2026-08-23, finding 6): the classifier sends
    EVERY nonterminal heartbeat/journal state mismatch to
    UNKNOWN_RECONCILIATION_REQUIRED; the runbook table must not park any
    mismatch under UNKNOWN_RESUMABLE."""
    for line in RUNBOOK.read_text(encoding="utf-8").splitlines():
        if not line.startswith("| `UNKNOWN_"):
            continue  # only the era_status classification table rows
        if "UNKNOWN_RESUMABLE" in line:
            assert "mismatch" not in line.lower(), line
        if "mismatch" in line.lower():
            assert "UNKNOWN_RECONCILIATION_REQUIRED" in line, line


def test_runbook_names_every_cli() -> None:
    text = RUNBOOK.read_text(encoding="utf-8")
    for script in SEVEN_SCRIPTS:
        assert script in text, script


def test_runbook_carries_the_standing_rules_and_contracts() -> None:
    text = RUNBOOK.read_text(encoding="utf-8")
    for needle in (
        "MANIFEST_MISMATCH",
        "Restart=no",
        "UNKNOWN_RESUMABLE",
        "UNKNOWN_RECONCILIATION_REQUIRED",
        "/tmp/m4h-era.log",
        "0.2.1",
        "PENDING owner ratification",
        "OWNER-RATIFIED INPUT",
    ):
        assert needle in text, needle
    # The census incompleteness exit and the amendment dry-run contract.
    assert "exit 5" in text.lower() or "exits 5" in text.lower()
    assert "landed: false" in text


# ---- supervisor template ----------------------------------------------------------


def test_template_is_do_not_install_and_restart_no() -> None:
    text = TEMPLATE.read_text(encoding="utf-8")
    assert "DO NOT INSTALL" in text
    assert "Restart=no" in text
    assert "/tmp" in text  # the never-/tmp logging rule is stated


def test_template_readme_gates_on_owner_authorization() -> None:
    text = TEMPLATE_README.read_text(encoding="utf-8")
    assert "owner" in text.lower()
    assert "Restart=no" in text


def test_no_tree_options_unit_installed_on_this_host() -> None:
    """The template must stay a template: no installed unit on this host."""
    units_root = Path.home() / ".config" / "systemd" / "user"
    if not units_root.is_dir():
        return  # nothing installed anywhere — the intended state
    installed = [p.name for p in units_root.rglob("tree-options-era.service")]
    assert installed == [], f"template unit is installed: {installed}"


if __name__ == "__main__":  # pragma: no cover
    sys.exit(0)
