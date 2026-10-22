"""Wave 4 lane H: the seven operational drills and the owner's drill record.

Acceptance (``contracts.acceptance``) needs all seven drills ``passed``,
with evidence, attested by an owner. A drill is the OWNER's act; this
module never passes one on its own. It offers two commands
(``python -m tree_options.trex.drills``):

- ``check <drill> --since T --until T``: read-only over the desk's own
  files for the drill's window; writes a ``trex-drill-check/1`` report
  under ``<TREX_ACCEPTANCE_DIR>/evidence/drills/<drill>/`` and prints its
  path and sha256. Every requirement is named with whether it held; the
  report is ``ok`` only when all held.
- ``record <drill> --status passed|failed --attested-by EMAIL --evidence
  REL [--copy FILE] [--note TEXT]``: writes ``drills/<drill>.json``
  (``DrillRecord``), refusing (exit 2, nothing written) unless the attester
  is in ``TREX_OWNER_EMAILS``, every evidence file is a regular file under
  the evidence root (hashed here), and, for ``passed``, one of the cited
  files is an ``ok`` check of the same drill. The previous record of the
  drill is kept in ``evidence/drills/<drill>/history.jsonl``.

What each check reads (window = [since, until]):

| Drill | Requirements |
| --- | --- |
| ``relogin`` | the gateway watch went to ``needs_login``/``needs_2fa``; it came back ``ok``; the desk logged ``gateway_connected`` after the login prompt |
| ``restart_mid_position`` | a recorder sample showed exposure before a desk restart (``gateway_connected`` with ``first: true``); a fresh, un-halted exit owner after it; the longest unguarded stretch of the window at most 300 s |
| ``gateway_outage`` | the gateway watch went ``api_down``/``down``; back ``ok``; a desk reconnect after the outage began |
| ``duplicate_command`` | the owner answered at least one command in the window; no command was applied twice (the browser's ``duplicate`` answer is not persisted by the web: attach it as evidence) |
| ``backup_restore`` | ``--restore-dir`` holds a ``trex-desk-restore/1`` receipt that restored files, dated inside the window (copied into the evidence) |
| ``notification`` | an alert source fired in the window (a desk event the runtime pushes, or a gateway watch alarm); the owner attests delivery and latency |
| ``unassisted_use`` | a command issued through the Family portal (``ingress: family``) was applied in the window; the owner attests it was unaided |

Gateway transitions come from the watch's own ``gateway.json`` event list
and from consecutive recorder samples. Nothing here touches a broker, a
unit or a live service; ``live_authorized`` is always ``false``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from typing import Annotated, Any, Final, Literal

from pydantic import Field, ValidationError, model_validator

from tree_options.schemas.common import IdStr, StrictModel
from tree_options.trex.acceptance import (
    Sample,
    Sources,
    acceptance_dir_from_env,
    heartbeat_fresh,
    log_lines,
    longest_unguarded_s,
    read_samples,
    sample_days,
    sample_path,
)
from tree_options.trex.contracts.acceptance import (
    DRILL_IDS,
    MAX_EXIT_UNGUARDED_S,
    DrillId,
    DrillRecord,
    EvidenceRef,
    FalseLiteral,
    et_date,
)
from tree_options.trex.contracts.canonical import UTCInstant, model_bytes, sha256_hex
from tree_options.trex.contracts.commands import AnyCommand, CommandResult, parse_command
from tree_options.trex.contracts.layout import Wave2RunDir
from tree_options.trex.contracts.wave3 import (
    TREX_OWNER_EMAILS_ENV,
    AcceptanceDir,
    parse_owner_emails,
)
from tree_options.trex.ledger import write_atomic

__all__ = [
    "ALERTING_DESK_EVENTS",
    "DRILL_CHECK_SCHEMA",
    "DrillCheck",
    "DrillFacts",
    "DrillRefused",
    "Finding",
    "Requirement",
    "evaluate",
    "load_facts",
    "main",
    "record_drill",
    "run_check",
]

DRILL_CHECK_SCHEMA: Final = "trex-drill-check/1"
RESTORE_SCHEMA: Final = "trex-desk-restore/1"
RESTORE_RECEIPT: Final = "RESTORE.json"
#: Desk events the runtime pushes (``trex.desk_runtime._NOTIFY_EVENTS``;
#: a test pins the equality).
ALERTING_DESK_EVENTS: Final = frozenset(
    {
        "unknown_exposure",
        "legs_mismatch",
        "exit_flat_unexplained",
        "entry_uncertain_held",
        "gateway_lost",
        "exit_blocked",
        "reconciliation_required",
        "equity_contract_mismatch",
        "equity_unqualified",
        "structure_tick_failed",
        "backup_failed",
        "entry_request",
        "entry_filled",
        "exit_begin",
        "closed",
    }
)
LOGIN_STATES: Final = frozenset({"needs_login", "needs_2fa"})
OUTAGE_STATES: Final = frozenset({"api_down", "down"})
#: the gateway watch alarms (and pushes) for these (``gateway_watch.BAD``)
GATEWAY_ALARMS: Final = LOGIN_STATES | OUTAGE_STATES
#: ``DrillRecord.note``'s bound
NOTE_MAX_CHARS: Final = 2000
MAX_READ_BYTES: Final = 16 * 1024 * 1024
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]")

Text = Annotated[str, Field(max_length=500)]


class Requirement(StrictModel):
    name: IdStr
    met: bool
    detail: Text


class Finding(StrictModel):
    kind: IdStr
    at: UTCInstant | None
    detail: Text


class DrillCheck(StrictModel):
    """``trex-drill-check/1``: what the desk's files showed for one drill
    window. ``ok`` is exactly "every requirement held"."""

    schema_version: Literal["trex-drill-check/1"] = Field(
        default="trex-drill-check/1", alias="schema"
    )
    drill_id: DrillId
    since: UTCInstant
    until: UTCInstant
    checked_at: UTCInstant
    requirements: tuple[Requirement, ...] = Field(min_length=1)
    findings: tuple[Finding, ...]
    artifacts: tuple[EvidenceRef, ...]
    ok: bool
    live_authorized: FalseLiteral

    @model_validator(mode="after")
    def _shape(self) -> DrillCheck:
        if self.since >= self.until:
            raise ValueError("a drill window starts before it ends")
        if self.ok != all(r.met for r in self.requirements):
            raise ValueError("ok is exactly every requirement met")
        return self


class DrillRefused(Exception):
    """The record (or check) was refused; nothing was written."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# =============================================================================
# facts (read-only)
# =============================================================================


@dataclass(frozen=True)
class DeskEventLine:
    at: datetime
    event: str
    payload: Mapping[str, Any]


@dataclass(frozen=True)
class GatewayTransition:
    at: datetime
    before: str
    after: str


@dataclass(frozen=True)
class DrillFacts:
    desk_events: tuple[DeskEventLine, ...] = ()
    samples: tuple[Sample, ...] = ()
    gateway: tuple[GatewayTransition, ...] = ()
    results: tuple[CommandResult, ...] = ()
    commands: Mapping[str, AnyCommand] = field(default_factory=dict)
    duplicates: tuple[str, ...] = ()
    receipt: Mapping[str, Any] | None = None
    receipt_bytes: bytes | None = None
    problems: tuple[str, ...] = ()


def _read_regular(path: Path, limit: int = MAX_READ_BYTES) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError(f"{path.name}: not a bounded regular file")
        chunks = []
        total = 0
        while chunk := os.read(fd, 1024 * 1024):
            chunks.append(chunk)
            total += len(chunk)
            if total > limit:
                raise ValueError(f"{path.name}: grew past its bound")
        return b"".join(chunks)
    finally:
        os.close(fd)


def _aware(text: object) -> datetime | None:
    if not isinstance(text, str):
        return None
    try:
        value = datetime.fromisoformat(text)
    except ValueError:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        return None
    return value.astimezone(UTC)


def _inside(at: datetime, since: datetime, until: datetime) -> bool:
    return since <= at <= until


def _desk_events(
    path: Path, since: datetime, until: datetime, problems: list[str]
) -> list[DeskEventLine]:
    out = []
    try:
        for line in log_lines(path):
            try:
                doc = json.loads(line)
            except ValueError:
                continue
            if not isinstance(doc, dict) or not isinstance(doc.get("event"), str):
                continue
            at = _aware(doc.get("ts"))
            if at is not None and _inside(at, since, until):
                out.append(DeskEventLine(at, doc["event"], doc))
    except (OSError, ValueError):
        problems.append("desk_events_unreadable")
        return []
    return sorted(out, key=lambda e: e.at)


def _gateway_events(path: Path, since: datetime, until: datetime) -> list[GatewayTransition]:
    try:
        doc = json.loads(_read_regular(path))
    except (OSError, ValueError):
        return []
    events = doc.get("events") if isinstance(doc, dict) else None
    out = []
    for event in events if isinstance(events, list) else []:
        if not isinstance(event, dict) or event.get("kind") != "status":
            continue
        at, detail = event.get("at"), event.get("detail")
        if isinstance(at, bool) or not isinstance(at, int | float) or not isinstance(detail, str):
            continue
        before, sep, after = detail.partition(" -> ")
        if not sep:
            continue
        try:
            instant = datetime.fromtimestamp(at, UTC)
        except (ValueError, OverflowError, OSError):
            continue
        if _inside(instant, since, until):
            out.append(GatewayTransition(instant, before.strip(), after.strip()))
    return out


def _window_samples(
    adir: AcceptanceDir, since: datetime, until: datetime, problems: list[str]
) -> list[Sample]:
    first, last = et_date(since), et_date(until)
    out: list[Sample] = []
    for day in sorted(sample_days(adir)):
        if not first <= day <= last:
            continue
        try:
            found = read_samples(sample_path(adir, day))
        except (OSError, ValueError):
            problems.append(f"samples_unreadable:{day.isoformat()}")
            continue
        out.extend(s for s in found.samples if _inside(s.at, since, until))
    return sorted(out, key=lambda s: s.at)


def _sample_transitions(samples: Sequence[Sample]) -> list[GatewayTransition]:
    known = [s for s in samples if s.gateway is not None]
    out = []
    for a, b in pairwise(known):
        assert a.gateway is not None and b.gateway is not None
        if a.gateway.status != b.gateway.status:
            out.append(GatewayTransition(b.at, a.gateway.status, b.gateway.status))
    return out


def _commands(
    run_dir: Path, since: datetime, until: datetime, problems: list[str]
) -> tuple[list[CommandResult], dict[str, AnyCommand], list[str]]:
    directory = Wave2RunDir(run_dir).commands()
    results: list[CommandResult] = []
    commands: dict[str, AnyCommand] = {}
    duplicates: list[str] = []
    if not directory.is_dir():
        return results, commands, duplicates
    for path in sorted(directory.glob("*.result.json")):
        try:
            result = CommandResult.model_validate_json(_read_regular(path))
        except (OSError, ValueError, ValidationError):
            problems.append(f"command_result_unreadable:{path.name}")
            continue
        if _inside(result.at, since, until):
            results.append(result)
    for result in results:
        key = result.idempotency_key
        for candidate in (
            directory / f"{key}.json",
            directory / f"{key}.claimed.json",
            directory / "done" / f"{key}.claimed.json",
        ):
            try:
                commands[key] = parse_command(json.loads(_read_regular(candidate)))
                break
            except (OSError, ValueError, ValidationError):
                continue
        for folder in (directory, directory / "done"):
            duplicates.extend(sorted(p.name for p in folder.glob(f"{key}.duplicate-*.json")))
    return results, commands, duplicates


def load_facts(
    adir: AcceptanceDir,
    sources: Sources,
    since: datetime,
    until: datetime,
    *,
    restore_dir: Path | None = None,
) -> DrillFacts:
    """Everything a drill check reads, for [since, until] (read-only)."""
    problems: list[str] = []
    samples = _window_samples(adir, since, until, problems)
    transitions = _gateway_events(sources.gateway_state, since, until)
    transitions.extend(_sample_transitions(samples))
    results, commands, duplicates = _commands(sources.run_dir, since, until, problems)
    receipt: dict[str, Any] | None = None
    receipt_bytes: bytes | None = None
    if restore_dir is not None:
        try:
            receipt_bytes = _read_regular(restore_dir / RESTORE_RECEIPT)
            loaded = json.loads(receipt_bytes)
            receipt = loaded if isinstance(loaded, dict) else None
        except (OSError, ValueError):
            problems.append("restore_receipt_unreadable")
            receipt_bytes = None
    return DrillFacts(
        desk_events=tuple(_desk_events(sources.run_dir / "events.jsonl", since, until, problems)),
        samples=tuple(samples),
        gateway=tuple(sorted(transitions, key=lambda t: t.at)),
        results=tuple(results),
        commands=commands,
        duplicates=tuple(duplicates),
        receipt=receipt,
        receipt_bytes=receipt_bytes,
        problems=tuple(problems),
    )


# =============================================================================
# the checks (pure)
# =============================================================================


def _req(name: str, met: bool, detail: str = "") -> Requirement:
    return Requirement(name=name, met=bool(met), detail=detail[:500])


def _z(instant: datetime | None) -> str:
    return "" if instant is None else instant.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _gateway_drill(
    facts: DrillFacts, states: frozenset[str], went: str
) -> tuple[list[Requirement], list[Finding]]:
    began = next((t.at for t in facts.gateway if t.after in states), None)
    back = began is not None and any(t.after == "ok" and t.at > began for t in facts.gateway)
    reconnect = began is not None and any(
        e.event == "gateway_connected" and e.at > began for e in facts.desk_events
    )
    findings = [
        Finding(kind="gateway_transition", at=t.at, detail=f"{t.before} -> {t.after}")
        for t in facts.gateway
    ]
    findings.extend(
        Finding(kind="desk_reconnect", at=e.at, detail=f"first={e.payload.get('first')}")
        for e in facts.desk_events
        if e.event == "gateway_connected"
    )
    requirements = [
        _req(went, began is not None, _z(began)),
        _req("gateway_back_ok", back),
        _req("desk_reconnected", reconnect),
    ]
    return requirements, findings


def _restart(
    facts: DrillFacts, since: datetime, until: datetime
) -> tuple[list[Requirement], list[Finding]]:
    restart = next(
        (
            e.at
            for e in facts.desk_events
            if e.event == "gateway_connected" and e.payload.get("first") is True
        ),
        None,
    )
    before = restart is not None and any(
        s.exposed is True and s.at < restart for s in facts.samples
    )
    after = restart is not None and any(
        s.at > restart and s.exposed is not None and heartbeat_fresh(s) and s.halt is False
        for s in facts.samples
    )
    longest = longest_unguarded_s(facts.samples, since, until)
    within = longest is not None and longest <= MAX_EXIT_UNGUARDED_S
    findings = [
        Finding(kind="desk_restart", at=restart, detail="gateway_connected first=true"),
        Finding(
            kind="max_exit_unguarded_s",
            at=None,
            detail="unknown" if longest is None else str(longest),
        ),
    ]
    requirements = [
        _req("position_open_before_restart", before),
        _req("desk_restarted", restart is not None, _z(restart)),
        _req("exit_owner_fresh_after_restart", after),
        _req("exit_unguarded_within_limit", within, f"limit {MAX_EXIT_UNGUARDED_S} s"),
    ]
    return requirements, findings


def _duplicate(facts: DrillFacts) -> tuple[list[Requirement], list[Finding]]:
    answered = [r for r in facts.results if r.status in ("applied", "refused", "failed")]
    keys = {r.idempotency_key for r in answered}
    applied = Counter(
        e.payload.get("command") for e in facts.desk_events if e.event == "command_applied"
    )
    twice = sorted(k for k in keys if applied.get(k, 0) > 1)
    findings = [
        Finding(kind="command_answered", at=r.at, detail=f"{r.kind} {r.status}") for r in answered
    ]
    findings.extend(
        Finding(kind="duplicate_set_aside", at=None, detail=n) for n in facts.duplicates
    )
    requirements = [
        _req("command_answered", bool(answered)),
        _req("applied_at_most_once", not twice, ", ".join(twice)),
    ]
    return requirements, findings


def _backup(
    facts: DrillFacts, since: datetime, until: datetime
) -> tuple[list[Requirement], list[Finding]]:
    receipt = facts.receipt or {}
    restored = receipt.get("restored")
    valid = (
        receipt.get("schema") == RESTORE_SCHEMA and isinstance(restored, list) and bool(restored)
    )
    restored_at = _aware(receipt.get("restored_at"))
    in_window = restored_at is not None and _inside(restored_at, since, until)
    findings = [
        Finding(
            kind="restore_receipt",
            at=restored_at,
            detail=f"{len(restored) if isinstance(restored, list) else 0} files restored",
        )
    ]
    requirements = [
        _req("restore_receipt_valid", valid),
        _req("restored_in_window", in_window, _z(restored_at)),
    ]
    return requirements, findings


def _notification(facts: DrillFacts) -> tuple[list[Requirement], list[Finding]]:
    findings = [
        Finding(kind="desk_alert", at=e.at, detail=e.event)
        for e in facts.desk_events
        if e.event in ALERTING_DESK_EVENTS
    ]
    findings.extend(
        Finding(kind="gateway_alarm", at=t.at, detail=t.after)
        for t in facts.gateway
        if t.after in GATEWAY_ALARMS
    )
    return [_req("alert_raised", bool(findings))], findings


def _unassisted(facts: DrillFacts) -> tuple[list[Requirement], list[Finding]]:
    findings = []
    for result in facts.results:
        command = facts.commands.get(result.idempotency_key)
        if result.status == "applied" and command is not None:
            if command.issued_by.ingress == "family":
                findings.append(
                    Finding(kind="family_command_applied", at=result.at, detail=result.kind)
                )
    return [_req("owner_command_via_family", bool(findings))], findings


def evaluate(
    drill_id: str,
    facts: DrillFacts,
    *,
    since: datetime,
    until: datetime,
    checked_at: datetime,
    artifacts: Sequence[EvidenceRef] = (),
) -> DrillCheck:
    """The check of one drill over its window (pure)."""
    if drill_id == "relogin":
        requirements, findings = _gateway_drill(facts, LOGIN_STATES, "gateway_needed_login")
    elif drill_id == "gateway_outage":
        requirements, findings = _gateway_drill(facts, OUTAGE_STATES, "gateway_went_down")
    elif drill_id == "restart_mid_position":
        requirements, findings = _restart(facts, since, until)
    elif drill_id == "duplicate_command":
        requirements, findings = _duplicate(facts)
    elif drill_id == "backup_restore":
        requirements, findings = _backup(facts, since, until)
    elif drill_id == "notification":
        requirements, findings = _notification(facts)
    elif drill_id == "unassisted_use":
        requirements, findings = _unassisted(facts)
    else:
        raise DrillRefused("drill_unknown")
    findings.extend(Finding(kind="problem", at=None, detail=p) for p in facts.problems)
    return DrillCheck.model_validate(
        {
            "drill_id": drill_id,
            "since": since,
            "until": until,
            "checked_at": checked_at,
            "requirements": requirements,
            "findings": findings,
            "artifacts": tuple(artifacts),
            "ok": all(r.met for r in requirements),
            "live_authorized": False,
        }
    )


# =============================================================================
# writing evidence and the record (I/O)
# =============================================================================


def _stamp(now: datetime) -> str:
    return now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def _put(adir: AcceptanceDir, rel: str, data: bytes) -> EvidenceRef:
    ref = EvidenceRef(path=rel, sha256=sha256_hex(data))
    write_atomic(adir.evidence() / rel, data)
    return ref


def run_check(
    adir: AcceptanceDir,
    sources: Sources,
    drill_id: str,
    *,
    since: datetime,
    until: datetime,
    now: datetime,
    restore_dir: Path | None = None,
) -> tuple[DrillCheck, EvidenceRef]:
    """Load, check and write ``evidence/drills/<drill>/<stamp>-check.json``
    (plus a copy of the restore receipt for ``backup_restore``)."""
    if drill_id not in DRILL_IDS:
        raise DrillRefused("drill_unknown")
    if since >= until or until > now:
        raise DrillRefused("drill_window_invalid")
    facts = load_facts(adir, sources, since, until, restore_dir=restore_dir)
    stamp = _stamp(now)
    artifacts = []
    if drill_id == "backup_restore" and facts.receipt_bytes is not None:
        rel = f"drills/{drill_id}/{stamp}-{RESTORE_RECEIPT}"
        artifacts.append(_put(adir, rel, facts.receipt_bytes))
    check = evaluate(drill_id, facts, since=since, until=until, checked_at=now, artifacts=artifacts)
    ref = _put(adir, f"drills/{drill_id}/{stamp}-check.json", model_bytes(check))
    return check, ref


def _evidence_ref(adir: AcceptanceDir, rel: str) -> EvidenceRef:
    try:
        EvidenceRef(path=rel, sha256="0" * 64)
    except ValidationError:
        raise DrillRefused("evidence_path_invalid") from None
    root = adir.evidence()
    path = root / rel
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        raise DrillRefused("evidence_missing") from None
    except OSError:
        raise DrillRefused("evidence_unreadable") from None
    if not stat.S_ISREG(info.st_mode):
        raise DrillRefused("evidence_not_regular_file")
    if not path.resolve().is_relative_to(root.resolve()):
        raise DrillRefused("evidence_outside_root")
    try:
        data = _read_regular(path)
    except (OSError, ValueError):
        raise DrillRefused("evidence_unreadable") from None
    return EvidenceRef(path=rel, sha256=sha256_hex(data))


def _ok_check(adir: AcceptanceDir, ref: EvidenceRef, drill_id: str) -> bool:
    try:
        data = _read_regular(adir.evidence() / ref.path)
        check = DrillCheck.model_validate_json(data)
    except (OSError, ValueError, ValidationError):
        return False
    return sha256_hex(data) == ref.sha256 and check.drill_id == drill_id and check.ok


def record_drill(
    adir: AcceptanceDir,
    drill_id: str,
    *,
    status: str,
    attested_by: str,
    owners: frozenset[str] | set[str],
    evidence: Sequence[str] = (),
    copy: Sequence[Path] = (),
    at: datetime | None = None,
    note: str = "",
    now: datetime | None = None,
) -> DrillRecord:
    """Write the owner's ``drills/<drill>.json`` (see the module docstring).
    Raises ``DrillRefused`` with a code, having written nothing."""
    now = (now or datetime.now(UTC)).astimezone(UTC)
    at = (at or now).astimezone(UTC)
    if drill_id not in DRILL_IDS:
        raise DrillRefused("drill_unknown")
    if status not in ("passed", "failed"):
        raise DrillRefused("status_invalid")
    if not owners:
        raise DrillRefused("owners_not_configured")
    who = attested_by.strip().lower()
    if who not in owners:
        raise DrillRefused("attester_not_owner")
    if at > now:
        raise DrillRefused("drill_at_in_future")
    if not evidence and not copy:
        raise DrillRefused("evidence_required")
    refs = [_evidence_ref(adir, rel) for rel in evidence]
    sources: list[tuple[str, bytes]] = []
    for path in copy:
        try:
            sources.append((path.name, _read_regular(Path(path))))
        except (OSError, ValueError):
            raise DrillRefused("copy_unreadable") from None
    if status == "passed" and not any(_ok_check(adir, ref, drill_id) for ref in refs):
        raise DrillRefused("drill_check_not_ok")
    if len(note) > NOTE_MAX_CHARS:
        raise DrillRefused("note_too_long")
    stamp = _stamp(now)
    for name, data in sources:
        safe = _SAFE_NAME.sub("_", name).lstrip("._-")[:70] or "file"
        refs.append(_put(adir, f"drills/{drill_id}/{stamp}-{safe}", data))
    record = DrillRecord.model_validate(
        {
            "id": drill_id,
            "status": status,
            "evidence": tuple(refs),
            "attested_by": who,
            "at": at,
            "note": note,
        }
    )
    current = adir.drill(drill_id)
    try:
        previous = DrillRecord.model_validate_json(_read_regular(current))
    except (OSError, ValueError, ValidationError):
        previous = None
    if previous is not None:
        history = adir.evidence() / "drills" / drill_id / "history.jsonl"
        history.parent.mkdir(parents=True, exist_ok=True)
        with history.open("ab") as stream:
            stream.write(model_bytes(previous) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
    write_atomic(current, model_bytes(record))
    return record


# =============================================================================
# CLI
# =============================================================================


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m tree_options.trex.drills",
        description="Paper acceptance drills: read-only checks and the owner's record.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check", help="check a drill window and write the report")
    record = sub.add_parser("record", help="write the owner's drill record")
    for cmd in (check, record):
        cmd.add_argument("drill", choices=DRILL_IDS)
        cmd.add_argument("--acceptance-dir", type=Path, default=None)
        cmd.add_argument("--run-dir", type=Path, default=None)
        cmd.add_argument("--supervised-dir", type=Path, default=None)
        cmd.add_argument("--gateway-state", type=Path, default=None)
    check.add_argument("--since", required=True, help="ISO instant with offset")
    check.add_argument("--until", required=True, help="ISO instant with offset")
    check.add_argument("--restore-dir", type=Path, default=None)
    record.add_argument("--status", required=True, choices=("passed", "failed"))
    record.add_argument("--attested-by", required=True, help="an email in TREX_OWNER_EMAILS")
    record.add_argument("--evidence", action="append", default=[], help="path under evidence/")
    record.add_argument("--copy", action="append", type=Path, default=[], help="file to copy in")
    record.add_argument("--at", default=None, help="when the drill ran (default: now)")
    record.add_argument("--note", default="")
    return parser


def _emit(payload: Mapping[str, Any]) -> None:
    print(json.dumps({**payload, "live_authorized": False}, sort_keys=True))


def main(
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    now: datetime | None = None,
) -> int:
    args = _parser().parse_args(argv)
    env = os.environ if env is None else env
    defaults = Sources.from_env(env)
    sources = Sources(
        run_dir=args.run_dir or defaults.run_dir,
        supervised_dir=args.supervised_dir or defaults.supervised_dir,
        gateway_state=args.gateway_state or defaults.gateway_state,
    )
    adir = AcceptanceDir(args.acceptance_dir or acceptance_dir_from_env(env))
    instant = (now or datetime.now(UTC)).astimezone(UTC)
    try:
        if args.command == "check":
            since, until = _aware(args.since), _aware(args.until)
            if since is None or until is None:
                raise DrillRefused("instant_invalid")
            result, ref = run_check(
                adir,
                sources,
                args.drill,
                since=since,
                until=until,
                now=instant,
                restore_dir=args.restore_dir,
            )
            _emit(
                {
                    "drill": args.drill,
                    "ok": result.ok,
                    "path": ref.path,
                    "sha256": ref.sha256,
                    "requirements": {r.name: r.met for r in result.requirements},
                }
            )
            return 0 if result.ok else 3
        try:
            owners = parse_owner_emails(env.get(TREX_OWNER_EMAILS_ENV, ""))
        except ValueError:
            raise DrillRefused("owners_invalid") from None
        at = None
        if args.at is not None:
            at = _aware(args.at)
            if at is None:
                raise DrillRefused("instant_invalid")
        record = record_drill(
            adir,
            args.drill,
            status=args.status,
            attested_by=args.attested_by,
            owners=owners,
            evidence=args.evidence,
            copy=args.copy,
            at=at,
            note=args.note,
            now=instant,
        )
    except DrillRefused as refused:
        _emit({"drill": args.drill, "refused": refused.code})
        return 2
    _emit(
        {
            "drill": record.id,
            "status": record.status,
            "attested_by": record.attested_by,
            "evidence": [r.path for r in record.evidence],
            "path": str(adir.drill(record.id)),
        }
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
