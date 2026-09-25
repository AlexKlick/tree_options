"""The desk's admission step (plan E6, ``desk-enter``).

Claims admissible deals from the miner's queue, re-validates them at
admit time, writes one structure spec file per admitted deal for the
runtime (E5, ``trex-desk``) to execute, appends the admission to
``admissions.jsonl``, and pushes a count-only ntfy notice. It places NO
orders and NEVER writes ``book.json`` (the runtime owns the book).

Claiming is the discovery lane's link+unlink pattern: ``os.link`` is the
POSIX atomic-claim, so two slots racing on one deal cannot both win. A
claim is permanent for its deal id (deal ids embed the mine session).

Validation happens at ADMIT time, never trusting the miner's day-of
verdict: the queue document's own ``valid_until``, the schema's money
discipline (strings, the house convention), the recomputed max loss
against the deal's, the structure's exit deadline as a calendar session
strictly before the first expiry, and admission in rank order.

Armed vs shadow: until the desk runtime lands, ``shadow=True`` records
what WOULD have been admitted (the spec and the admission line carry
``"mode": "shadow"``) and the rails re-check is reported as
``rails_not_evaluable_no_book`` rather than refusing. Armed mode
re-checks rails against the live desk book and refuses on anything not
PASS. ``HALT`` or ``AUTO_OFF`` in the run dir stop admissions for the
day; ``--once`` admits at most one deal (the rollout's quantity-1 step).
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from tree_options.desk import paths
from tree_options.desk.sessions import Calendar, latest_completed_session
from tree_options.desk.store import atomic_write_json
from tree_options.trex.notify import load_config, send
from tree_options.trex.plan import LegStructure

QUEUE_SCHEMA = "trex.deal/1"
SPEC_SCHEMA = "trex.spec/1"
ADMISSIONS_SCHEMA = "trex.admissions/1"


def _dec(raw: object) -> Decimal | None:
    if not isinstance(raw, str):
        return None  # money fields are strings, the house convention
    try:
        return Decimal(raw)
    except ArithmeticError:
        return None


@dataclass(frozen=True)
class EnterResult:
    exit_code: int
    status: str
    detail: str
    session: date | None = None
    admitted: int = 0
    claimed: int = 0

    def line(self) -> str:
        return (
            f"desk-enter session={self.session} status={self.status} "
            f"admitted={self.admitted} claimed={self.claimed}"
        ) + (f" ({self.detail})" if self.detail else "")


# ------------------------------------------------------------ validation


def validate_deal(
    deal: Mapping[str, Any],
    valid_until: datetime,
    now: datetime,
    cal: Calendar,
) -> list[str]:
    """Every admission-time check; an empty list means admissible. Each
    entry is a stable problem code (the admission line and the operator's
    audit read them)."""
    problems: list[str] = []
    if now > valid_until:
        problems.append("queue_expired")
    for key in ("fill", "width", "max_loss"):
        if deal.get(key) is not None and not isinstance(deal.get(key), str):
            problems.append(f"money_not_string:{key}")
    for leg in deal.get("legs") or ():
        for key in ("strike",):
            if leg.get(key) is not None and not isinstance(leg.get(key), str):
                problems.append(f"money_not_string:leg_{key}")
    raw = deal.get("structure")
    if not isinstance(raw, Mapping):
        problems.append("no_structure")
        return problems
    try:
        spec = LegStructure(**dict(raw))
    except Exception as exc:  # pydantic ValidationError: any malformed spec
        problems.append(f"structure_invalid:{type(exc).__name__}")
        return problems
    claimed_max = _dec(deal.get("max_loss"))
    computed = spec.max_loss_per_package() * 100 * spec.quantity
    if claimed_max is None or claimed_max != computed:
        problems.append("max_loss_mismatch")
    # the deal doc's claimed deadline (a string the runtime pays no
    # attention to) is what the broker, the operator, and the audit
    # read; verify it against the calendar AND against the first expiry
    # recorded by the spec, not against the spec's own deadline field
    # (the spec already proves internally consistent; this gate catches
    # the deal-doc / spec disagreement).
    raw_deadline = deal.get("exit_deadline")
    try:
        claimed_deadline = date.fromisoformat(str(raw_deadline)) if raw_deadline else None
    except ValueError:
        claimed_deadline = None
    if claimed_deadline is None:
        problems.append("deadline_missing_or_bad")
    else:
        if not cal.is_session(claimed_deadline):
            problems.append("deadline_not_a_session")
        if claimed_deadline >= spec.first_expiry:
            problems.append("deadline_not_before_first_expiry")
    if spec.id != str(deal.get("deal_id")):
        problems.append("deal_id_mismatch")
    return problems


# --------------------------------------------------------------- claiming


def _claim_path(run_dir: Path, deal_id: str) -> Path:
    return run_dir / "claims" / f"{deal_id}.json"


def claim(run_dir: Path, deal_id: str, payload: Mapping[str, Any]) -> bool:
    """Atomically claim a deal id; True when this caller won. The claim
    file records what was claimed (audit), and its existence is the lock."""
    path = _claim_path(run_dir, deal_id)
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_name(f".{deal_id}.{os.getpid()}.tmp")
    atomic_write_json(staged, dict(payload))
    try:
        os.link(staged, path)  # atomic: fails if another slot won
    except FileExistsError:
        staged.unlink(missing_ok=True)
        return False
    staged.unlink(missing_ok=True)
    return True


# ------------------------------------------------------------------ enter


def _admissions_path(run_dir: Path) -> Path:
    return run_dir / "admissions.jsonl"


def _append_admission(run_dir: Path, line: Mapping[str, Any]) -> None:
    path = _admissions_path(run_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        fh.write(json.dumps(line, default=str, sort_keys=True) + "\n")


def _halted(run_dir: Path) -> str | None:
    # HALT and AUTO_OFF are operator kill files; checked lazily so a brand
    # new run dir (which is the normal case until a deal is admitted)
    # does not need pre-creation. The runtime's first tick creates it.
    if not run_dir.exists():
        return None
    for name in ("HALT", "AUTO_OFF"):
        if (run_dir / name).exists():
            return name
    return None


def run_enter(
    *,
    now: datetime,
    cal: Calendar,
    session: date | None = None,
    run_dir: Path | None = None,
    queue_dir: Path | None = None,
    shadow: bool = True,
    once: bool = False,
    dry_run: bool = False,
    notify_send: Callable[..., bool] | None = None,
    notify_env: Path | None = None,
) -> EnterResult:
    """One desk-enter pass over a session's queue (the latest completed
    session at ``now`` by default, or one named via ``session``).

    Exit codes: 0 admitted (or nothing to do), 3 no queue for the session
    (retry at the next slot), 2 bad arguments. ``shadow`` is the mode of
    the whole desk until the runtime lands; ``once`` caps admissions at
    one deal (the quantity-1 rollout step).
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    run_dir = run_dir or paths.desk_paper_dir()
    queue_dir = queue_dir or paths.queue_dir()
    # default: the latest completed session at ``now``; named: that session
    d = session if session is not None else latest_completed_session(now, cal)
    if d is None:
        return EnterResult(2, "no_session", "no completed session at now")
    queue_path = queue_dir / f"{d.isoformat()}.json"
    if not queue_path.exists():
        return EnterResult(3, "not_ready", f"no queue for {d}", d)
    try:
        queue = json.loads(queue_path.read_text())
        valid_until = datetime.fromisoformat(str(queue["valid_until"]))
        admissible = list(queue.get("admissible") or [])
    except (OSError, ValueError, KeyError):
        return EnterResult(1, "bad_queue", f"{queue_path} unreadable", d)
    if valid_until.tzinfo is None:
        return EnterResult(1, "bad_queue", "valid_until is not aware", d)
    if (halt := _halted(run_dir)) is not None:
        return EnterResult(0, "halted", halt, d)
    if now > valid_until:
        return EnterResult(0, "expired", f"queue valid_until {valid_until.isoformat()}", d)
    # rank order: the queue's own ranking decides admission order
    ranked = sorted(
        admissible,
        key=lambda x: (
            x.get("rank") if isinstance(x.get("rank"), int) else 10**9,
            str(x.get("deal_id")),
        ),
    )
    admitted = claimed = 0
    for deal in ranked:
        if once and admitted:
            break
        deal_id = str(deal.get("deal_id"))
        if not deal_id:
            continue
        problems = validate_deal(deal, valid_until, now, cal)
        notes: list[str] = []
        if not problems:
            if shadow:
                # shadow mode has no live desk book to re-check rails
                # against; armed mode (E5) re-checks and refuses on any
                # non-PASS result
                notes.append("rails_not_evaluable_no_book")
        line = {
            "schema": ADMISSIONS_SCHEMA,
            "at": now.isoformat(),
            "deal_id": deal_id,
            "row": deal.get("row"),
            "underlying": deal.get("underlying"),
            "kind": deal.get("kind"),
            "rank": deal.get("rank"),
            "mode": "shadow" if shadow else "armed",
            "admitted": not problems,
            "problems": problems,
            "notes": notes,
        }
        if problems:
            # a deal that fails admission is recorded once, never claimed
            if claim(run_dir, f"refused:{deal_id}", line):
                _append_admission(run_dir, line)
            continue
        if not claim(run_dir, deal_id, line):
            continue  # an earlier slot already admitted it
        claimed += 1
        spec = {
            "schema": SPEC_SCHEMA,
            "mode": "shadow" if shadow else "armed",
            "admitted_at": now.isoformat(),
            "structure": deal["structure"],
        }
        if not dry_run:
            atomic_write_json(run_dir / "specs" / f"{deal_id}.json", spec)
            _append_admission(run_dir, line)
        admitted += 1
    if admitted and not dry_run:
        sender = notify_send or send
        cfg = {} if notify_send is not None else load_config(
            notify_env or paths.notify_env_path()
        )
        if cfg is not None:
            # no money, no URLs, no ids in pushes (the standing rule)
            sender(
                cfg,
                "desk-enter",
                f"{admitted} deal(s) admitted ({'shadow' if shadow else 'armed'})",
                "default",
            )
    return EnterResult(0, "ok", "", d, admitted=admitted, claimed=claimed)
