"""Desk automation helpers: timer status, kill-file verbs, entries pause.

Coordinator decision 2026-10-21: every command is APPLIED BY THE DESK OWNER
process. The web only authenticates, validates and queues
(``commands_view``); no web route calls a function here that writes. What
the web serves from this module is read-only: ``automation_status`` (timer
states over a FIXED WHITELIST of the desk's own user units, plus the kill
files as they stand). The systemctl runner is injectable so tests never
touch the real session bus. Timer CONTROL has no ``trex-command/1`` kind,
so the cockpit cannot queue it; the operator uses ``systemctl --user``.

For the owner process (and the CLI):

- ``kill_file_action``: the desk_cli HALT/FLATTEN/resume verbs on a run
  dir, audited to ``automation.jsonl``. ``resume`` keeps its meaning (HALT
  and FLATTEN) and never lifts the entries pause.
- the entries-only pause (``PAUSE_ENTRIES``, ``trex.pause``):
  ``pause_entries_action`` / ``resume_entries_action`` (actor and reason
  supplied by the caller) and ``apply_entries_command`` for a validated
  ``pause_entries`` / ``resume_entries`` ``trex-command/1``, judged by the
  same validity rule as the web's intake
  (``command_store.command_window_blocker``). They are audited by
  ``trex.pause`` (``pause-entries.jsonl``, with the actor).
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tree_options.trex import pause
from tree_options.trex.clock import ET
from tree_options.trex.contracts.commands import (
    AnyCommand,
    PauseEntriesCommand,
    ResumeEntriesCommand,
)
from tree_options.trex.desk_cli import HALT_FLATTEN_WARNING
from tree_options.trex_web.command_store import command_window_blocker

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
        "entries_paused": pause.is_paused(run_dir),
        "timers": timers,
    }


def kill_file_action(run_dir: Path, action: str) -> dict[str, Any]:
    """HALT/FLATTEN/resume for the supervised desk (the desk_cli verbs;
    the file verbs are case-insensitive, resume clears both).

    For the desk owner process and the CLI only: no web route calls this
    (the cockpit's legacy verbs queue ``halt``/``flatten``/``resume_all``
    commands instead). A FLATTEN placed while HALT is active carries the
    same warning desk_cli prints, verbatim (HALT_FLATTEN_WARNING): the
    runtime gates exits on HALT too."""
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


# ------------------------------------------------------- entries-only pause


def _entries_doc(run_dir: Path, outcome: dict[str, Any]) -> dict[str, Any]:
    return {
        "entries_paused": outcome["entries_paused"],
        "changed": outcome["changed"],
        "kill_files": sorted(f for f in KILL_FILES if (run_dir / f).exists()),
        "pause": pause.pause_record(run_dir),
    }


def pause_entries_action(
    run_dir: Path,
    *,
    actor: str,
    reason: str = "",
    command_key: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Place ``PAUSE_ENTRIES`` (new entries refused; exits continue)."""
    outcome = pause.pause_entries(
        run_dir,
        actor=actor,
        reason=reason,
        now=now or datetime.now(UTC),
        command_key=command_key,
    )
    return _entries_doc(run_dir, outcome)


def resume_entries_action(
    run_dir: Path,
    *,
    actor: str,
    reason: str = "",
    command_key: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Remove ``PAUSE_ENTRIES`` (HALT and FLATTEN untouched)."""
    outcome = pause.resume_entries(
        run_dir,
        actor=actor,
        reason=reason,
        now=now or datetime.now(UTC),
        command_key=command_key,
    )
    return _entries_doc(run_dir, outcome)


def apply_entries_command(
    run_dir: Path, command: AnyCommand, *, now: datetime | None = None
) -> dict[str, Any]:
    """Apply a validated ``pause_entries`` / ``resume_entries`` command.

    The audit actor is ``<ingress>:<sub>`` of the command's verified
    ``issued_by``; the audit carries the idempotency key. A command is
    applied only while ``command_window_blocker`` (the one rule the web's
    intake also uses) has no objection; otherwise ``ValueError`` names the
    reason (``command_not_yet_valid`` / ``command_expired``). Any other
    kind is not an entries command (ValueError)."""
    if not isinstance(command, PauseEntriesCommand | ResumeEntriesCommand):
        raise ValueError(f"not an entries-pause command: {command.kind}")
    at = now or datetime.now(UTC)
    blocker = command_window_blocker(command, at)
    if blocker is not None:
        raise ValueError(blocker)
    action = (
        pause_entries_action if isinstance(command, PauseEntriesCommand) else resume_entries_action
    )
    return action(
        run_dir,
        actor=f"{command.issued_by.ingress}:{command.issued_by.sub}",
        reason=command.args.reason,
        command_key=command.idempotency_key,
        now=at,
    )
