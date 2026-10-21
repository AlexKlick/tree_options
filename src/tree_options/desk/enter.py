"""E6 admission PREVIEW only. No broker imports, order submission or spec spool.

A validated research queue is not a trading authorization. Runtime readiness is
not inferred from the mere existence of book.json: admission READS the desk
owner's published facts (``trex.admission``: the risk snapshot, settled cash,
the reconciliation and the exit owner's ``monitor.json``), and each missing,
stale or bad fact is its own blocker. The entries-only pause
(``PAUSE_ENTRIES``), HALT and AUTO_OFF add theirs. The sealed selection rule's
activation gates this AUTOMATIC admission only. This build intentionally
cannot arm itself (``shadow_only_build``).
"""

from __future__ import annotations

import os
from datetime import date, datetime, time
from decimal import ROUND_CEILING
from pathlib import Path
from typing import Any

from tree_options.desk import paths, selection
from tree_options.desk.contracts import (
    MAX_JSON_BYTES,
    ContractError,
    digest,
    parse_deal,
    parse_queue,
    read_json,
    timestamp,
)
from tree_options.desk.evidence import EvidenceStore
from tree_options.desk.sessions import Calendar, latest_completed_session
from tree_options.desk.shadows import database_path
from tree_options.trex.admission import cash_blockers, owner_fact_blockers, read_risk_snapshot
from tree_options.trex.clock import ET
from tree_options.trex.pause import pause_blockers
from tree_options.trex.plan import LegStructure


def execution_directory() -> Path:
    # DESK_PAPER_DIR belongs to the existing RESEARCH panel. Never reuse it.
    return Path(os.environ.get("TREX_DESK_RUN_DIR", "~/.local/state/trex/desk-paper")).expanduser()


def deal_cost_cents(spec: LegStructure) -> int:
    """Settled cash the deal can consume: its worst case at the cap
    (``LegStructure.max_loss``, x100 per package), in whole cents."""
    return int((spec.max_loss() * 100).to_integral_value(rounding=ROUND_CEILING))


def run_enter(
    *,
    now: datetime,
    cal: Calendar,
    session: date | None = None,
    database: Path | None = None,
    queue_dir: Path | None = None,
    run_dir: Path | None = None,
    shadow: bool = True,
    dry_run: bool = False,
) -> dict[str, Any]:
    timestamp(now.isoformat())
    if not shadow:
        raise ContractError("execution_not_implemented")
    latest = latest_completed_session(now, cal)
    if session is not None and session != latest:
        raise ContractError("session_not_current")
    local = now.astimezone(ET)
    result: dict[str, Any] = {
        "schema": "desk-admission-preview/2",
        "mode": "shadow",
        "execution_enabled": False,
        "admitted": 0,
        "previewed": 0,
        "dry_run": dry_run,
        "as_of": now.isoformat(),
        "session": latest.isoformat(),
        "decisions": [],
    }
    if not cal.is_session(local.date()) or not time(9, 50) <= local.time() < time(11, 30):
        return {**result, "status": "outside_entry_window"}
    path = (queue_dir or paths.queue_dir()) / f"{latest.isoformat()}.json"
    if not path.exists():
        return {**result, "status": "queue_not_ready"}
    if path.is_symlink():
        raise ContractError("queue_symlink")
    with path.open("rb") as stream:
        queue = parse_queue(
            read_json(stream.read(MAX_JSON_BYTES + 1)), cal, expected_session=latest
        )
    if queue.entry_session != local.date() or not queue.cutoff <= now < queue.valid_until:
        raise ContractError("queue_not_current")
    deals = [parse_deal(d, queue, cal) for d in queue.admissible]
    run_dir = run_dir or execution_directory()
    blockers = ["shadow_only_build", *pause_blockers(run_dir)]
    for flag in ("HALT", "AUTO_OFF"):
        # lexists also treats a dangling kill-file symlink as STOP, never as absent.
        if os.path.lexists(run_dir / flag):
            blockers.append(flag.lower())
    # the owner's published facts (risk snapshot, cash, reconciliation, exit
    # owner health): each missing or stale fact is its own blocker
    blockers += owner_fact_blockers(run_dir, now)
    # AUTOMATIC admission only: owner-approved entries never need the rule
    if selection.load_config().status == "PROPOSED":
        blockers.append("selection_rule_not_activated")
    snapshot = read_risk_snapshot(run_dir)
    with EvidenceStore(database or database_path(), transient=dry_run) as store:
        with store.atomic():
            store.verify()
            for deal in deals:
                cash = cash_blockers(snapshot, now, deal_cost_cents(deal.spec))
                preview = {
                    "schema": "desk-admission-decision/2",
                    "deal_id": deal.deal_id,
                    "queue_sha256": queue.sha256,
                    "entry_session": queue.entry_session.isoformat(),
                    "rank": deal.raw["rank"],
                    "contract_valid": True,
                    "decision": "blocked",
                    "blockers": [*blockers, *(b for b in cash if b not in blockers)],
                    "execution_enabled": False,
                }
                # Same facts replay once. A changed stop state creates a new audit
                # event; the timestamp is in the journal, not the idempotency key.
                store.put("admission_preview", digest(preview), preview, now)
                result["decisions"].append(preview)
        result["audit"] = store.verify()
    result["previewed"] = len(deals)
    result["status"] = "blocked" if deals else "empty_queue"
    return result
