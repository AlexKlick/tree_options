"""Broker-free Wave 3 safety commands and honest evidence readiness.

No background threads, external network calls, broker imports, notifications,
research re-scoring or selection activation. All clocks and paths are injectable.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

from tree_options.desk import enter, scorecards, selection, shadows
from tree_options.desk.contracts import ContractError
from tree_options.desk.evidence import EvidenceError, EvidenceStore
from tree_options.desk.sessions import Calendar, first_session_after, latest_completed_session
from tree_options.trex.clock import ET

RELEASE = 'desk-wave3-safety-rc1'
COMMANDS = ('shadows', 'scorecards', 'desk-enter', 'desk-health',
            'verify-evidence', 'backup-evidence')


def register(sub: Any) -> None:
    for name in COMMANDS:
        p = sub.add_parser(name, help=f'{RELEASE}: {name} (no order placement)')
        p.set_defaults(production_command=True)
        p.add_argument('--database', type=Path, help='local evidence DB, never broker book.json')
        if name in ('shadows', 'desk-enter'):
            p.add_argument('--session', type=date.fromisoformat)
            p.add_argument('--queue-dir', type=Path)
            p.add_argument('--dry-run', action='store_true', help='in-memory evaluation, no persistent writes')
        if name == 'shadows':
            p.add_argument('--store-root', type=Path)
        if name == 'desk-enter':
            p.add_argument('--run-dir', type=Path, help='read-only location of HALT/AUTO_OFF')
            p.add_argument('--shadow', action='store_true', help='default; preview only')
            p.add_argument('--armed', action='store_true', help='always refused in this build')
        if name == 'scorecards':
            p.add_argument('--as-of', type=date.fromisoformat)
        if name == 'backup-evidence':
            p.add_argument('--out', type=Path, required=True, help='new snapshot file; never overwrite')


def health(*, database: Path | None, now: datetime, cal: Calendar) -> dict[str, Any]:
    database = database or shadows.database_path()
    expected = latest_completed_session(now, cal).isoformat()
    base: dict[str, Any] = {'schema': 'desk-health/2', 'release': RELEASE,
        'as_of': now.isoformat(), 'expected_session': expected,
        'execution_enabled': False, 'execution_status': 'disabled_unimplemented',
        'broker_connection_verified': False, 'source': 'local_evidence_not_broker',
        'selection_status': selection.load_config().status,
        'evidence_status': 'not_initialized', 'evidence_blockers': ['database_missing']}
    if not database.exists():
        return base
    with EvidenceStore(database, readonly=True) as store:
        store.conn.execute('BEGIN')
        audit = store.verify()
        horizon = max((e['session'] for e in store.all('evaluation')), default=None)
        present = store.get('queue', expected) is not None
        as_of = date.fromisoformat(expected)
        cards = scorecards.summarize(store.all('episode'), store.all('mark'),
                                     store.all('quality'), as_of=as_of)
        censored = sum(c['n_censored'] for c in cards)
        blockers, pending = [], []
        next_entry = first_session_after(as_of, cal)
        queue_due = datetime.combine(next_entry, time(9, 30), ET) if next_entry else None
        evaluation_due = datetime.combine(as_of, time(19, 35), ET)
        if horizon is None or horizon < expected:
            (pending if now < evaluation_due else blockers).append(
                'evaluation_cycle_pending' if now < evaluation_due else 'evaluation_lagging')
        if not present:
            not_due = queue_due is not None and now < queue_due
            (pending if not_due else blockers).append(
                'queue_publication_pending' if not_due else 'missing_current_queue')
        if censored:
            blockers.append('censored_deadline_outcomes')
        return {**base, 'evidence_status': 'degraded' if blockers else 'awaiting_publication' if pending else 'ready',
            'pending': pending, 'queue_due_at': queue_due.isoformat() if queue_due else None,
            'evidence_blockers': blockers, 'evaluated_through': horizon,
            'current_queue_recorded': present, 'censored_total': censored, 'audit': audit,
            'required_before_execution': ['separate_operator_activation',
                'single_owner_broker_runtime', 'fresh_authoritative_rails',
                'account_and_exposure_reconciliation', 'broker_margin_verification',
                'intraday_exit_observations', 'host_release_gate_and_shadow_soak']}


def dispatch(args: argparse.Namespace, *, now: datetime, cal: Calendar) -> int:
    """Write machine-readable JSON; successful preview != permission to trade."""
    code = 0
    database = args.database or shadows.database_path()
    try:
        if args.command == 'shadows':
            doc = shadows.update_shadows(session=args.session, now=now, cal=cal,
                database=database, store_root=args.store_root, queue_dir=args.queue_dir,
                dry_run=args.dry_run)
            code = 0 if doc['status'] == 'ok' else 3
        elif args.command == 'scorecards':
            doc = scorecards.build_scorecards(database, as_of=args.as_of)
        elif args.command == 'desk-enter':
            doc = enter.run_enter(now=now, cal=cal, session=args.session,
                database=database, queue_dir=args.queue_dir, run_dir=args.run_dir,
                shadow=not args.armed, dry_run=args.dry_run)
            code = 3 if doc['status'] == 'queue_not_ready' else 0
        elif args.command == 'desk-health':
            doc = health(database=database, now=now, cal=cal)
            code = 0 if doc['evidence_status'] == 'ready' else 3
        elif args.command == 'verify-evidence':
            with EvidenceStore(database, readonly=True) as store:
                doc = store.verify()
        elif args.command == 'backup-evidence':
            with EvidenceStore(database, readonly=True) as store:
                # Keep the verified source snapshot stable through backup.
                store.conn.execute('BEGIN')
                audit = store.verify()
                store.conn.rollback()
                store.backup(args.out)
            # Verify the actual copy too: writers may have committed after the
            # read snapshot above; destination's audit is the snapshot receipt.
            with EvidenceStore(args.out, readonly=True) as backup:
                audit = backup.verify()
            doc = {'schema': 'desk-backup/1', 'status': 'ok', 'audit': audit}
        else:
            raise ContractError('unknown_command')
    except (ContractError, EvidenceError) as exc:
        doc, code = {'schema': 'desk-error/1', 'execution_enabled': False, 'error': str(exc)}, 1
    except sqlite3.Error:
        doc, code = {'schema': 'desk-error/1', 'execution_enabled': False, 'error': 'database_error'}, 1
    except OSError:
        doc, code = {'schema': 'desk-error/1', 'execution_enabled': False, 'error': 'filesystem_error'}, 1
    print(json.dumps(doc, sort_keys=True, allow_nan=False))
    return code
