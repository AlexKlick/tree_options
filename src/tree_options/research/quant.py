"""Quant strategies in the Research Lab's immutable runstate and evidence model.

Scores and targets are proposals. This module imports no broker/runtime client.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from tree_options.research.contracts import EvidenceEnvelope, ResearchRegistration
from tree_options.research.runstate.store import RunstateStore
from tree_options.strategy_lab.catalog import by_id
from tree_options.strategy_lab.contracts import Observation, require_utc
from tree_options.strategy_lab.portfolio import equal_weight
from tree_options.strategy_lab.ranking import hqm_scores, percentile_scores, twelve_minus_one_return


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


@dataclass(frozen=True)
class FrozenUniverse:
    as_of: date
    members: tuple[str, ...]
    source: str
    source_sha256: str

    def __post_init__(self) -> None:
        if not self.source or len(self.source_sha256) != 64:
            raise ValueError('universe requires source and content hash')
        if len(set(self.members)) != len(self.members) or any(not x for x in self.members):
            raise ValueError('universe members must be distinct nonempty identities')
        object.__setattr__(self, 'members', tuple(sorted(self.members)))

    def to_dict(self) -> dict[str, Any]:
        return {'as_of': self.as_of.isoformat(), 'members': list(self.members), 'source': self.source, 'source_sha256': self.source_sha256}


@dataclass(frozen=True)
class QuantSnapshot:
    universe: FrozenUniverse
    cutoff: datetime
    observations: tuple[Observation, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, 'cutoff', require_utc(self.cutoff))
        if self.universe.as_of != self.cutoff.date():
            raise ValueError('historical universe date must match decision cutoff')
        seen: set[tuple[str, str]] = set()
        for obs in self.observations:
            if obs.available_at > self.cutoff:
                raise ValueError('observation exceeds knowledge cutoff')
            if obs.entity_id not in self.universe.members:
                raise ValueError('observation outside frozen universe')
            key = (obs.source, obs.source_id)
            if key in seen:
                raise ValueError('duplicate observation source identity')
            seen.add(key)
        object.__setattr__(self, 'observations', tuple(sorted(self.observations, key=lambda o: (o.entity_id, o.event_at, o.available_at, o.source, o.source_id))))

    def to_dict(self) -> dict[str, Any]:
        return {'universe': self.universe.to_dict(), 'knowledge_cutoff': self.cutoff.isoformat(), 'observations': [
            {'entity_id': o.entity_id, 'event_at': o.event_at.isoformat(), 'available_at': o.available_at.isoformat(),
             'values': {k: str(v) for k, v in o.values.items()}, 'source': o.source, 'source_id': o.source_id,
             'metadata': dict(o.metadata)} for o in self.observations]}

    @property
    def identity(self) -> str:
        return digest(self.to_dict())


def register_version(store: RunstateStore, strategy_id: str, *, code_sha: str, lock_sha: str,
                     parameters: dict[str, Any] | None = None) -> dict[str, Any]:
    if len(code_sha) != 40 or len(lock_sha) != 64:
        raise ValueError('strategy requires code SHA and dependency lock hash')
    definition = by_id(strategy_id)
    config = dict(parameters or {})
    if set(config) - {'top_n'} or ('top_n' in config and (type(config['top_n']) is not int or config['top_n'] < 1)):
        raise ValueError('invalid strategy configuration')
    payload = {'definition': json.loads(json.dumps(asdict(definition))), 'config': config, 'config_sha256': digest(config),
               'code_sha': code_sha, 'lock_sha256': lock_sha, 'constructor': 'equal_weight/1',
               'universe_policy': 'frozen_historical', 'availability_policy': 'observed_before_cutoff',
               'rebalance': 'monthly', 'cost_policy': 'comparison_spec'}
    payload['version_id'] = f'{strategy_id}/v{definition.version}/{digest(payload)}'
    store.put('quant_version', payload, key=payload['version_id'])
    return payload


def _monthly_prices(snapshot: QuantSnapshot, entity: str) -> dict[int, Decimal]:
    prices: dict[int, Decimal] = {}
    for obs in snapshot.observations:
        if obs.entity_id == entity and 'close' in obs.values:
            price = obs.values['close']
            if price <= 0:
                raise ValueError('close price must be positive')
            prices[obs.event_at.year * 12 + obs.event_at.month] = price
    return prices


def run_strategy(store: RunstateStore, version: dict[str, Any], snapshot: QuantSnapshot) -> dict[str, Any]:
    retained = store.get('quant_version', version['version_id'])
    if retained != version:
        raise ValueError('strategy version not registered or bytes changed')
    definition = by_id(version['definition']['strategy_id'])
    snapshot_id = snapshot.identity
    store.put('quant_snapshot', snapshot.to_dict(), key=snapshot_id)
    run_id = digest({'version': version['version_id'], 'snapshot': snapshot_id})
    gated = definition.data_status != 'supported'
    # Exploratory cluster runs require the full registered feature/beta panel,
    # never an implicit fallback to simple prices.

    components: dict[str, dict[str, Decimal]] = {}
    exclusions: dict[str, str] = {}
    month = snapshot.cutoff.year * 12 + snapshot.cutoff.month
    for entity in snapshot.universe.members:
        if gated:
            exclusions[entity] = definition.data_status if definition.data_status != 'supported' else 'requires_registered_factor_panel'
            continue
        prices = _monthly_prices(snapshot, entity)
        if definition.strategy_id == 'equal_weight_us_equities':
            if prices:
                components[entity] = {'control': Decimal('1')}
            else:
                exclusions[entity] = 'missing_prices'
        elif definition.strategy_id == 'momentum_12_1':
            if month - 12 in prices and month - 1 in prices:
                components[entity] = {'return_12_1': twelve_minus_one_return(price_t_minus_12=prices[month - 12], price_t_minus_1=prices[month - 1])}
            else:
                exclusions[entity] = 'missing_12_1_endpoints'
        elif definition.strategy_id == 'hqm_1_3_6_12':
            if all(month - n in prices for n in (0, 1, 3, 6, 12)):
                components[entity] = {f'{n}m': prices[month] / prices[month - n] - 1 for n in (1, 3, 6, 12)}
            else:
                exclusions[entity] = 'missing_hqm_endpoints'
    if definition.strategy_id == 'hqm_1_3_6_12':
        scores = {s.entity_id: s.score for s in hqm_scores(components)}
    else:
        scores = percentile_scores({entity: next(iter(c.values())) for entity, c in components.items()})
    if definition.strategy_id == 'cluster_rsi_factor':
        from tree_options.strategy_lab.clustering import (
            ClusteringError,
            fit_kmeans,
            rsi_seed_centroids,
            semantic_cluster_by_feature,
        )
        feature_order = ('rsi', 'momentum', 'factor_beta')
        panels: dict[str, tuple[Decimal, ...]] = {}
        for entity in snapshot.universe.members:
            rows = [o for o in snapshot.observations if o.entity_id == entity and all(k in o.values for k in feature_order)]
            if rows:
                panels[entity] = tuple(rows[-1].values[k] for k in feature_order)
            else:
                exclusions[entity] = 'requires_registered_factor_panel'
        components = {entity: dict(zip(feature_order, panel, strict=True)) for entity, panel in panels.items()}
        if len(panels) >= 4:
            try:
                entities = sorted(panels)
                clusters = fit_kmeans([[float(v) for v in panels[k]] for k in entities], initial_centroids=rsi_seed_centroids(feature_count=3, rsi_feature_index=0).tolist())
                target = semantic_cluster_by_feature(clusters, feature_index=0)
                scores = {k: Decimal('1') for k, label in zip(entities, clusters.labels, strict=True) if label == target}
                exclusions.update({k: 'outside_semantic_target_cluster' for k in entities if k not in scores})
            except ClusteringError as error:
                gated = True
                exclusions.update({k: str(error) for k in panels})
        else:
            gated = True
    ordered = sorted(scores, key=lambda k: (-scores[k], k))
    selected = ordered[:version['config'].get('top_n', len(ordered))]
    targets = [{'entity_id': w.entity_id, 'weight': str(w.weight)} for w in equal_weight(selected)]
    evidence = EvidenceEnvelope(
        candidate_id=version['version_id'], point_session=snapshot.cutoff.date(),
        hypothesis=definition.description, estimand='cross-sectional score and proposed target weight',
        exact_versions={'strategy': version['version_id'], 'data': snapshot_id, 'code': version['code_sha'], 'lock': version['lock_sha256']},
        cohort_membership=snapshot.universe.members, registered_or_exploratory=ResearchRegistration.RETROSPECTIVE_BACKFILL,
        source_artifacts=((f'runstate:quant_snapshot/{snapshot_id}', snapshot_id),),
        diagnostics={'exclusions': exclusions, 'registration': definition.registration},
        warnings=('Research targets grant no execution authority.',),
    )
    result = {'run_id': run_id, 'strategy_version': version['version_id'], 'evidence_kind': 'deterministic_replay',
              'snapshot_id': snapshot_id, 'universe': snapshot.universe.to_dict(), 'knowledge_cutoff': snapshot.cutoff.isoformat(),
              'disposition': 'DATA-GATED-NOT-RUN' if gated else ('SCORED' if scores else 'NO_SIGNAL'),
              'scores': [{'entity_id': k, 'score': str(scores[k]), 'rank': rank + 1, 'components': {n: str(v) for n, v in components[k].items()}} for rank, k in enumerate(ordered)],
              'targets': targets, 'exclusions': exclusions, 'evidence': evidence.to_dict(),
              'execution_authorized': False}
    store.put('quant_experiment', result, key=run_id)
    return result


def execution_for_funded_replay(lifecycle: Any) -> Any:
    """Adapt admitted exact economics into the existing funded ledger input.

    A broker order snapshot cannot pass this boundary. No second equity ledger.
    """
    from tree_options.execution.evidence import assess_evidence
    from tree_options.research.comparison.funded import TradeExecution

    receipt = assess_evidence(lifecycle)
    if not receipt.is_admissible or receipt.economics is None:
        raise ValueError('execution evidence refused before experiment attribution')
    fills = [r for r in lifecycle.records if r.record_type in {'PARTIAL_FILL', 'COMPLETE_FILL'}]
    if not fills:
        raise ValueError('no execution evidence with authoritative event time')
    return tuple(TradeExecution(date=r.exchange_event_at.date(), symbol=lifecycle.intent.contract_id,
        signed_quantity=r.fill_quantity if lifecycle.intent.side == 'BUY' else -r.fill_quantity,
        price=r.unit_price, fees=r.fees) for r in fills)


def compare_quant_runs(store: RunstateStore, candidate_run: str, control_run: str) -> dict[str, Any]:
    """Persist a common-input score/target comparison, never an invented NAV."""
    candidate = store.get('quant_experiment', candidate_run)
    control = store.get('quant_experiment', control_run)
    if candidate is None or control is None or candidate['snapshot_id'] != control['snapshot_id']:
        raise ValueError('quant comparison requires common frozen inputs')
    result = {'kind': 'quant_comparison', 'candidate_run': candidate_run, 'control_run': control_run,
        'common_snapshot': candidate['snapshot_id'], 'evidence_kind': 'deterministic_replay',
        'candidate_targets': candidate['targets'], 'control_targets': control['targets'],
        'performance': None, 'performance_reason': 'No funded execution schedule evaluated.'}
    store.put('comparison_row', result, key=digest(result))
    return result


def campaign_proposal(store: RunstateStore, candidate_run: str, control_run: str) -> dict[str, Any]:
    comparison = compare_quant_runs(store, candidate_run, control_run)
    proposal = {'schema': 'quant-campaign-proposal/1', 'candidate_run': candidate_run, 'control_run': control_run,
        'comparison_sha256': digest(comparison), 'common_snapshot': comparison['common_snapshot'],
        'execution_environment': 'broker_paper', 'venue': 'alpaca_paper', 'live_money': False,
        'max_orders': 1, 'max_gross_notional_usd': '100', 'max_ttl_seconds': 900,
        'attribution': 'execution.evidence.assess_evidence', 'stopping_rule': 'one_order_or_uncertainty_or_halt',
        'execution_authorized': False}
    store.put('quant_provenance', proposal, key=digest(proposal))
    return proposal
