"""Read-only descriptive cards over verified, exactly dated shadow outcomes.

A sample floor is NOT a significance test, a policy promotion, or a broker
permission. EOD deadline proxies do not emulate the engine's intraday exits.
All denominators and censored observations remain visible.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from tree_options.desk.contracts import digest, money, timestamp
from tree_options.desk.evidence import EvidenceStore
from tree_options.desk.shadows import database_path
from tree_options.desk.sessions import cutoff_instant

# An operational sample floor from the D7 plan, NOT an adoption criterion.
MIN_RESOLVED = 20
_INVALIDATING = frozenset({'chain_vendor_conflict', 'chain_identity', 'chain_schema',
                          'chain_source_session', 'duplicate_contract_quote'})


def summarize(episodes: list[dict[str, Any]], marks: list[dict[str, Any]],
              quality: list[dict[str, Any]], *, as_of: date) -> list[dict[str, Any]]:
    end = as_of.isoformat()
    by_mark = {(m['deal_id'], m['session']): m for m in marks if m['session'] <= end}
    invalid = {(q['deal_id'], q['session']) for q in quality
               if q['code'] in _INVALIDATING and q['session'] <= end}
    families: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for ep in episodes:
        if ep['entry_session'] <= end:
            key = digest({k: ep[k] for k in ('row', 'tier', 'miner_sha256', 'playbook_sha256')})
            families[key].append(ep)
    out = []
    for key, group in sorted(families.items()):
        group.sort(key=lambda e: (e['entry_session'], e['deal_id']))
        resolved, censored, opened, invalidated = [], 0, 0, 0
        for ep in group:
            mk = (ep['deal_id'], ep['exit_deadline'])
            if ep['exit_deadline'] > end:
                opened += 1
            elif mk in invalid:
                censored += 1
                invalidated += 1
            elif mk in by_mark:
                resolved.append(by_mark[mk])
            else:
                censored += 1
        n = len(resolved)
        def total(field: str) -> str | None:
            return str(sum((money(m[field]) for m in resolved), Decimal(0))) if n else None
        net = total('modeled_net_pnl_dollars')
        out.append({'family_id': key, **{k: group[0][k] for k in
            ('row', 'tier', 'miner_sha256', 'playbook_sha256')},
            'n_episodes': len(group), 'n_resolved': n, 'n_censored': censored,
            'n_retrospective_registrations': sum(e.get('registration_timing') == 'retrospective_backfill' for e in group),
            'n_open': opened, 'n_invalidated': invalidated,
            'modeled_gross_pnl_dollars': total('modeled_gross_pnl_dollars'),
            'assumed_commissions_dollars': total('modeled_fees_dollars'),
            'modeled_net_pnl_dollars': net,
            'mean_modeled_net_pnl_dollars': str(Decimal(net) / n) if net is not None else None,
            'win_rate': sum(money(m['modeled_net_pnl_dollars']) > 0 for m in resolved) / n if n else None,
            'sample_floor': MIN_RESOLVED, 'sample_floor_met': n >= MIN_RESOLVED,
            'promotion_ready': False,
            'promotion_blockers': ['deadline_eod_proxy_is_not_execution_evidence',
                'forward_card_policy_and_operator_ruling_required'],
            'outcome_scope': 'hypothetical_entry_to_deadline_eod_adverse_side_close',
            'fee_scope': 'assumed_commission_only', 'execution_observed': False})
    return out


def build_scorecards(database: Path | None = None, *, as_of: date | None = None) -> dict[str, Any]:
    database = database or database_path()
    base: dict[str, Any] = {'schema': 'desk-scorecards/2', 'execution_enabled': False,
        'promotion_ready': False, 'families': [], 'status': 'not_initialized'}
    if not database.exists():
        return base
    with EvidenceStore(database, readonly=True) as store:
        store.conn.execute('BEGIN')  # audit and aggregates see one consistent snapshot
        audit = store.verify()
        evaluated = [date.fromisoformat(x['session']) for x in store.all('evaluation')]
        horizon = as_of or max(evaluated, default=None)
        if horizon is None:
            return {**base, 'status': 'no_evaluation', 'audit': audit}
        episodes = store.all('episode')
        marks, quality = store.all('mark'), store.all('quality')
        if as_of is not None:
            known_by = cutoff_instant(as_of)
            marks = [m for m in marks if timestamp(m['fetched_at']) <= known_by]
        families = summarize(episodes, marks, quality, as_of=horizon)
        candidates = [x for x in store.all('candidate') if x['source_session'] <= horizon.isoformat()]
        return {**base, 'status': 'ok', 'as_of_session': horizon.isoformat(),
            'audit': audit, 'families': families,
            'quote_knowledge_cutoff': cutoff_instant(as_of).isoformat() if as_of else 'all_recorded_observations',
            'historical_view': 'reconstructed_from_sealed_queue_and_vendor_timestamps_not_contemporaneous_capture_proof',
            'candidates_total': len(candidates),
            'candidates_with_contract_errors': sum(bool(x['contract_errors']) for x in candidates),
            'episodes_total': sum(c['n_episodes'] for c in families),
            'resolved_total': sum(c['n_resolved'] for c in families),
            'censored_total': sum(c['n_censored'] for c in families),
            'open_total': sum(c['n_open'] for c in families),
            'quality_events_total': len([q for q in quality if q['session'] <= horizon.isoformat()]),
            'notes': ['Historical quality events are retained after late data arrives.',
                'One representative per name/row/tier/week/policy; all candidates retained.',
                'No hypothesis test or automatic promotion is inferred from these proxy outcomes.']}
