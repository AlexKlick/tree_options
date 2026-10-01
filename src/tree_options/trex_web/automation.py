"""Operator automation surface for the cockpit: timer settings + kill files.

Every action goes through a FIXED WHITELIST of the desk's own user units
and the desk run dir's kill files - never an arbitrary unit, path or
command. Each action appends one audit line to
``<desk run dir>/automation.jsonl`` (actor is always the cockpit; the
operator's own systemctl use is not logged here).

The systemctl runner is injectable so tests never touch the real
session bus.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from tree_options.trex.clock import ET
from tree_options.trex.desk_cli import HALT_FLATTEN_WARNING

#: the desk's own timers -> their oneshot services (the whole surface)
AUTOMATION_UNITS: dict[str, dict[str, str]] = {
    "desk-lab": {
        "timer": "desk-lab.timer",
        "service": "desk-lab.service",
        "what": "hourly flash boards on the frozen bundle (quota-gated)",
    },
    "desk-lab-overnight": {
        "timer": "desk-lab-overnight.timer",
        "service": "desk-lab-overnight.service",
        "what": "nightly hindsight-gap + GEPA policy evolution",
    },
    "desk-challenge": {
        "timer": "desk-challenge.timer",
        "service": "desk-challenge.service",
        "what": "nightly end-to-end challenge scoreboard",
    },
    "desk-supervised-preview": {
        "timer": "desk-supervised-preview.timer",
        "service": "desk-supervised-preview.service",
        "what": "E6 shadow request previews (entry window)",
    },
}

#: kill-file controls (the supervised desk's stop states)
KILL_FILES = ("HALT", "FLATTEN")

SystemctlRunner = Callable[[list[str]], str]


def real_systemctl(args: list[str]) -> str:
    """One bounded ``systemctl --user`` call; stdout, or raise on failure.

    A failed call must never look like empty state: the first live deploy
    showed every timer "disabled" because the unit sandbox blocked the
    session bus (AF_UNIX) and the empty stdout was displayed as fact."""
    result = subprocess.run(
        ["systemctl", "--user", *args], capture_output=True, text=True, timeout=30, check=False
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"systemctl {' '.join(args[:2])}: rc={result.returncode} {result.stderr.strip()[:120]}"
        )
    return result.stdout


def _audit(run_dir: Path, action: str, subject: str, detail: str = "") -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "at": datetime.now(ET).isoformat(),
        "actor": "cockpit",
        "action": action,
        "subject": subject,
        "detail": detail,
    }
    with (run_dir / "automation.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, sort_keys=True) + "\n")


def automation_status(
    run_dir: Path, *, systemctl: SystemctlRunner = real_systemctl
) -> dict[str, Any]:
    """Timer states + kill files for the cockpit's automation card."""
    timers: list[dict[str, Any]] = []
    for key, spec in AUTOMATION_UNITS.items():
        timer, service = spec["timer"], spec["service"]

        def query(unit: str, prop: str) -> str:
            return systemctl(["show", unit, "--property", prop, "--value"]).strip()

        timers.append(
            {
                "key": key,
                "what": spec["what"],
                "timer": timer,
                "service": service,
                "enabled": query(timer, "UnitFileState") == "enabled",
                "active": query(timer, "ActiveState") == "active",
                "next_elapse": query(timer, "NextElapseUSecRealtime"),
                "last_result": query(service, "Result"),
                "last_exit": query(service, "ExecMainExitTimestamp"),
            }
        )
    return {
        "schema": "desk-automation/1",
        "kill_files": sorted(f for f in KILL_FILES if (run_dir / f).exists()),
        "timers": timers,
    }


def automation_action(
    run_dir: Path, key: str, action: str, *, systemctl: SystemctlRunner = real_systemctl
) -> dict[str, Any]:
    """enable/disable a whitelisted timer, or run its service once now."""
    if key not in AUTOMATION_UNITS:
        raise KeyError(key)
    spec = AUTOMATION_UNITS[key]
    commands = {
        "enable": ["enable", "--now", spec["timer"]],
        "disable": ["disable", "--now", spec["timer"]],
        "run": ["start", spec["service"]],
    }
    if action not in commands:
        raise ValueError(action)
    systemctl(commands[action])
    _audit(run_dir, action, spec["timer"])
    return {"key": key, "action": action, "unit": spec["timer"]}


def kill_file_action(run_dir: Path, action: str) -> dict[str, Any]:
    """HALT/FLATTEN/resume for the supervised desk (the desk_cli verbs;
    the file verbs are case-insensitive, resume clears both).

    A FLATTEN placed while HALT is active carries the same warning
    desk_cli prints, verbatim (HALT_FLATTEN_WARNING): the runtime gates
    exits on HALT too, so the cockpit must not look quieter than the CLI
    about a flatten that will close nothing."""
    verb = action.lower()
    if verb == "resume":
        for flag in KILL_FILES:
            (run_dir / flag).unlink(missing_ok=True)
        _audit(run_dir, "kill_file", verb)
    elif verb in (f.lower() for f in KILL_FILES):
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / verb.upper()).touch()
        _audit(run_dir, "kill_file", verb)
    else:
        raise ValueError(action)
    doc: dict[str, Any] = {"kill_files": sorted(f for f in KILL_FILES if (run_dir / f).exists())}
    if verb == "flatten" and (run_dir / "HALT").exists():
        doc["warning"] = HALT_FLATTEN_WARNING
    return doc
