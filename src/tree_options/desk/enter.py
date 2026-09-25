"""E6 admission PREVIEW only. No broker imports, order submission or spec spool.

A validated research queue is not a trading authorization. Runtime readiness is
not inferred from the mere existence of book.json. The broker-owning E5 process,
fresh authoritative rails, reconciliation and operator policy activation remain
required integration work. This build intentionally cannot arm itself.
"""
from __future__ import annotations

import os
from datetime import date, datetime, time
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
from tree_options.trex.clock import ET


def execution_directory() -> Path:
    # DESK_PAPER_DIR belongs to the existing RESEARCH panel. Never reuse it.
    return Path(os.environ.get('TREX_DESK_RUN_DIR', '~/.local/state/trex/desk-paper')).expanduser()


def run_enter(*, now: datetime, cal: Calendar, session: date | None = None,
              database: Path | None = None, queue_dir: Path | None = None,
              run_dir: Path | None = None, shadow: bool = True,
              dry_run: bool = False) -> dict[str, Any]:
    timestamp(now.isoformat())
    if not shadow:
        raise ContractError('execution_not_implemented')
    latest = latest_completed_session(now, cal)
    if session is not None and session != latest:
        raise ContractError('session_not_current')
    local = now.astimezone(ET)
    result: dict[str, Any] = {'schema': 'desk-admission-preview/2',
        'mode': 'shadow', 'execution_enabled': False, 'admitted': 0,
        'previewed': 0, 'dry_run': dry_run, 'as_of': now.isoformat(),
        'session': latest.isoformat(), 'decisions': []}
    if not cal.is_session(local.date()) or not time(9, 50) <= local.time() < time(11, 30):
        return {**result, 'status': 'outside_entry_window'}
    path = (queue_dir or paths.queue_dir()) / f'{latest.isoformat()}.json'
    if not path.exists():
        return {**result, 'status': 'queue_not_ready'}
    if path.is_symlink():
        raise ContractError('queue_symlink')
    with path.open('rb') as stream:
        queue = parse_queue(read_json(stream.read(MAX_JSON_BYTES + 1)), cal, expected_session=latest)
    if queue.entry_session != local.date() or not queue.cutoff <= now < queue.valid_until:
        raise ContractError('queue_not_current')
    deals = [parse_deal(d, queue, cal) for d in queue.admissible]
    run_dir = run_dir or execution_directory()
    blockers = ['shadow_only_build', 'broker_risk_snapshot_not_available',
        'broker_reconciliation_not_available', 'broker_margin_not_verified',
        'runtime_exit_observations_not_wired']
    if selection.load_config().status == 'PROPOSED':
        blockers.append('selection_rule_not_activated')
    for flag in ('HALT', 'AUTO_OFF'):
        # lexists also treats a dangling kill-file symlink as STOP, never as absent.
        if os.path.lexists(run_dir / flag):
            blockers.append(flag.lower())
    with EvidenceStore(database or database_path(), transient=dry_run) as store:
        with store.atomic():
            store.verify()
            for deal in deals:
                preview = {'schema': 'desk-admission-decision/2', 'deal_id': deal.deal_id,
                    'queue_sha256': queue.sha256, 'entry_session': queue.entry_session.isoformat(),
                    'rank': deal.raw['rank'], 'contract_valid': True,
                    'decision': 'blocked', 'blockers': blockers, 'execution_enabled': False}
                # Same facts replay once. A changed stop state creates a new audit
                # event; the timestamp is in the journal, not the idempotency key.
                store.put('admission_preview', digest(preview), preview, now)
                result['decisions'].append(preview)
        result['audit'] = store.verify()
    result['previewed'] = len(deals)
    result['status'] = 'blocked' if deals else 'empty_queue'
    return result
