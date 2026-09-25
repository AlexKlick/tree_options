"""Broker-free, point-in-time deadline-EOD shadow evidence (D7).

All candidates remain visible; one usable representative per name/row/tier/week
and sealed policy lineage is tracked. This is NOT a simulation of intraday
engine exits. Never use its EOD proxy outcome as proof an order filled or as an
automatic promotion decision. Missing deadline marks stay censored, not replaced
by old prices. The existing immutable chain store is the only pricing source.
"""
from __future__ import annotations

import gzip
from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from tree_options.desk import paths
from tree_options.desk.contracts import (
    MAX_JSON_BYTES, ContractError, Deal, Queue, digest, parse_deal, parse_queue,
    read_json, timestamp,
)
from tree_options.desk.evidence import EvidenceError, EvidenceStore
from tree_options.desk.rails import COMMISSION_PER_CONTRACT_USD
from tree_options.desk.sessions import Calendar, cutoff_instant, latest_completed_session
from tree_options.trex.clock import ET
from tree_options.trex.plan import LegStructure


def database_path() -> Path:
    return paths.state_root() / 'evidence' / 'desk.sqlite3'


def _cohort(deal: Deal, queue: Queue) -> str:
    year, week, _day = queue.entry_session.isocalendar()
    return digest({'name': deal.spec.underlying, 'row': deal.raw['row'],
        'tier': deal.raw.get('tier'), 'year': year, 'week': week,
        'miner': queue.raw['miner']['sha256'], 'playbook': queue.raw['playbook']['sha256']})


def _adopt(store: EvidenceStore, queue: Queue, cal: Calendar, now: datetime) -> int:
    if now < queue.cutoff:
        return 0  # conservative: do not backdate a future decision artifact
    old = store.get('queue', queue.session.isoformat())
    if old is not None:
        if digest(old) != queue.sha256:
            raise EvidenceError('content_conflict')
        return 0
    newest = store.latest_key('queue') or ''
    if queue.session.isoformat() < newest:
        raise EvidenceError('out_of_order_queue_requires_new_evidence_store')
    store.put('queue', queue.session.isoformat(), queue.raw, now)
    valid: list[Deal] = []
    for row in queue.rows:
        errors: list[str] = []
        try:
            valid.append(parse_deal(row, queue, cal))
        except ContractError as exc:
            if row['status'] == 'admissible':
                raise
            errors.append(str(exc))
        store.put('candidate', row['deal_id'], {'deal_id': row['deal_id'],
            'source_session': queue.session.isoformat(), 'queue_sha256': queue.sha256,
            'contract_errors': errors, 'raw': row}, now)
    created = 0
    valid.sort(key=lambda d: (d.raw.get('status') != 'admissible',
        d.raw['rank'] if type(d.raw.get('rank')) is int else 10**9, d.deal_id))
    for deal in valid:
        cohort = _cohort(deal, queue)
        if store.get('cohort', cohort) is not None:
            continue
        store.put('cohort', cohort, {'deal_id': deal.deal_id}, now)
        episode = {'schema': 'desk-episode/2', 'deal_id': deal.deal_id,
            'cohort': cohort, 'source_session': queue.session.isoformat(),
            'entry_session': queue.entry_session.isoformat(),
            'decision_cutoff': queue.cutoff.isoformat(), 'recorded_at': now.isoformat(),
            'registration_timing': ('before_entry_window_end' if now < queue.valid_until
                                    else 'retrospective_backfill'),
            'exit_deadline': deal.spec.exit_deadline.isoformat(),
            'queue_sha256': queue.sha256, 'miner_sha256': queue.raw['miner']['sha256'],
            'playbook_sha256': queue.raw['playbook']['sha256'],
            'row': deal.raw['row'], 'tier': deal.raw.get('tier'),
            'entry_model': 'miner_hypothetical_fill', 'exit_model': 'deadline_eod_proxy',
            'commission_per_contract_per_side': str(COMMISSION_PER_CONTRACT_USD),
            'deal': deal.raw}
        created += int(store.put('episode', deal.deal_id, episode, now))
    return created


def _number(value: Any) -> Decimal:
    if type(value) not in (int, float, str):
        raise ContractError('quote_number')
    try:
        out = Decimal(str(value))
    except InvalidOperation as exc:
        raise ContractError('quote_number') from exc
    if not out.is_finite() or abs(out) > Decimal('1e12'):
        raise ContractError('quote_number')
    return out


def package_prices(spec: LegStructure, doc: Mapping[str, Any]) -> tuple[Decimal | None, Decimal]:
    columns = doc.get('columns')
    needed = ('right', 'strike', 'exp', 'bid', 'ask')
    if not isinstance(columns, dict) or any(not isinstance(columns.get(k), list) for k in needed):
        raise ContractError('chain_columns')
    n = len(columns['right'])
    if not n or any(len(columns[k]) != n for k in needed):
        raise ContractError('chain_column_lengths')
    target = {(g.right, g.strike, g.expiry.isoformat()) for g in spec.legs}
    quotes: dict[tuple[Any, ...], tuple[Decimal, Decimal]] = {}
    for i in range(n):
        key = (columns['right'][i], _number(columns['strike'][i]), columns['exp'][i])
        if key not in target:
            continue
        if key in quotes:
            raise ContractError('duplicate_contract_quote')
        bid, ask = _number(columns['bid'][i]), _number(columns['ask'][i])
        if bid < 0 or ask <= 0 or ask < bid:
            raise ContractError('quote_book')
        quotes[key] = bid, ask
    mid: Decimal | None = Decimal(0)
    realistic = Decimal(0)
    for leg in spec.legs:
        key = (leg.right, leg.strike, leg.expiry.isoformat())
        if key not in quotes:
            raise ContractError('contract_quote_missing')
        bid, ask = quotes[key]
        sign = leg.sign * (-1 if spec.is_credit else 1)
        realistic += sign * (bid if leg.action == 'BUY' else ask)
        mid = mid + sign * (bid + ask) / 2 if bid > 0 and mid is not None else None
    return mid, realistic


def _read_mark(episode: dict[str, Any], session: date, root: Path, now: datetime) -> dict[str, Any]:
    spec = LegStructure.model_validate(episode['deal']['structure'])
    path = root / 'chains' / session.isoformat() / f'{spec.underlying}.json.gz'
    if path.is_symlink():
        raise ContractError('chain_symlink')
    if path.with_name(f'{spec.underlying}.conflict.json.gz').exists():
        raise ContractError('chain_vendor_conflict')
    try:
        with gzip.open(path, 'rb') as stream:
            doc = read_json(stream.read(MAX_JSON_BYTES + 1))
    except FileNotFoundError as exc:
        raise ContractError('chain_missing') from exc
    except (OSError, EOFError) as exc:
        raise ContractError('chain_unreadable') from exc
    h = doc.get('header')
    if not isinstance(h, dict) or h.get('schema') != 'desk-chain/1':
        raise ContractError('chain_schema')
    if h.get('session') != session.isoformat() or h.get('underlying') != spec.underlying:
        raise ContractError('chain_identity')
    fetched, source = timestamp(h.get('fetched_at')), timestamp(h.get('source_as_of'))
    if fetched > now or source > now or source > fetched:
        raise ContractError('chain_not_known_at_asof')
    # The producer already validates the actual class-specific closing time.
    # This consumer additionally prevents a previous-session stamp masquerading as D.
    if source.astimezone(ET).date() < session:
        raise ContractError('chain_source_session')
    underlying = h.get('underlying_quote')
    if not isinstance(underlying, dict):
        raise ContractError('chain_underlying_timestamp')
    last_trade = timestamp(underlying.get('last_trade_time'))
    if last_trade.astimezone(ET).date() != session or last_trade > source:
        raise ContractError('chain_source_session')
    mid, realistic = package_prices(spec, doc)
    fill = Decimal(episode['deal']['fill'])
    gross = (fill - realistic if spec.is_credit else realistic - fill) * 100 * spec.quantity
    fees = Decimal(episode['commission_per_contract_per_side']) * 2 * len(spec.legs) * spec.quantity
    return {'schema': 'desk-mark/2', 'deal_id': spec.id, 'session': session.isoformat(),
        'chain_sha256': digest(doc), 'source_as_of': source.isoformat(), 'fetched_at': fetched.isoformat(),
        'mid': str(mid) if mid is not None else None, 'realistic': str(realistic),
        'modeled_gross_pnl_dollars': str(gross), 'modeled_fees_dollars': str(fees),
        'modeled_net_pnl_dollars': str(gross - fees), 'execution_observed': False,
        'valuation_scope': 'deadline_eod_proxy', 'fee_scope': 'assumed_commission_only'}


def update_shadows(*, session: date | None, now: datetime, cal: Calendar,
                   database: Path | None = None, store_root: Path | None = None,
                   queue_dir: Path | None = None, dry_run: bool = False) -> dict[str, Any]:
    timestamp(now.isoformat())
    session = session or latest_completed_session(now, cal)
    if not cal.is_session(session) or now < cutoff_instant(session):
        raise ContractError('session_not_closed')
    database = database or database_path()
    store_root, queue_dir = store_root or paths.store_root(), queue_dir or paths.queue_dir()
    queue_files = []
    for p in queue_dir.glob('*.json'):
        try:
            d = date.fromisoformat(p.stem)
        except ValueError:
            continue
        if d <= session:
            queue_files.append((d, p))
    report: dict[str, Any] = {'schema': 'desk-shadow-run/2', 'session': session.isoformat(),
        'as_of': now.isoformat(), 'dry_run': dry_run, 'execution_enabled': False,
        'episodes_created': 0, 'marks_created': 0, 'queue_files': len(queue_files), 'quality_events': 0}
    with EvidenceStore(database, transient=dry_run) as store:
        with store.atomic():
            store.verify()  # refuse to extend compromised custody
            for d, p in sorted(queue_files):
                if p.is_symlink():
                    raise ContractError('queue_symlink')
                with p.open('rb') as fh:
                    queue = parse_queue(read_json(fh.read(MAX_JSON_BYTES + 1)), cal, expected_session=d)
                report['episodes_created'] += _adopt(store, queue, cal, now)
            for episode in store.all('episode'):
                entry = date.fromisoformat(episode['entry_session'])
                end = min(session, date.fromisoformat(episode['exit_deadline']))
                for d in cal.sessions():
                    if not entry <= d <= end:
                        continue
                    key = f"{episode['deal_id']}|{d.isoformat()}"
                    try:
                        mark = _read_mark(episode, d, store_root, now)
                    except ContractError as exc:
                        issue = {'deal_id': episode['deal_id'], 'session': d.isoformat(), 'code': str(exc)}
                        report['quality_events'] += int(store.put('quality', f'{key}|{exc}', issue, now))
                        continue
                    report['marks_created'] += int(store.put('mark', key, mark, now))
            store.put('evaluation', session.isoformat(), {'session': session.isoformat()}, now)
        report['audit'] = store.verify()
    # A missing current queue is a producer problem, but never stops mark/retry work.
    report['current_queue_present'] = any(d == session for d, _ in queue_files)
    report['status'] = 'ok' if report['current_queue_present'] else 'partial_missing_queue'
    return report
