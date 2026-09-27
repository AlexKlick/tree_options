"""Design conformance tests, not TREX application or broker tests."""
import copy
import json
from pathlib import Path

import pytest
from validate_model import ModelError, validate

ROOT = Path(__file__).resolve().parents[1]

@pytest.fixture
def packet():
    return tuple(json.loads((ROOT/p).read_text()) for p in [
        'examples/research-to-paper.plan.json',
        'examples/operation-registry.fixture.json',
        'contracts/action-plan.schema.json'])


def test_valid_proposal_never_grants_execution(packet):
    result = validate(*packet)
    assert result['node_count'] == 18
    assert result['execution_authorized'] is False
    assert result['broker_contacted'] is False


def test_input_order_does_not_change_dependency_order(packet):
    plan, reg, schema = packet
    baseline = validate(plan, reg, schema)
    plan['nodes'].reverse()
    result = validate(plan, reg, schema)
    order = result['topological_order']
    assert order.index('N13') < order.index('N14') < order.index('N15')
    assert set(order) == set(baseline['topological_order'])


@pytest.mark.parametrize('mutation,code',[
    ('duplicate','duplicate_node_id'),
    ('unknown_dependency','unknown_dependency'),
    ('cycle','scheduling_cycle'),
    ('missing_guard','paper_guard_missing'),
    ('unsafe_retry','paper_retry_requires_reconciliation'),
    ('drop_uncertainty','paper_failure_must_preserve_uncertainty'),
    ('wrong_owner','operation_contract_mismatch:owner_role'),
    ('unknown_artifact','unknown_artifact'),
    ('hash_drift','fixture_payload_hash_mismatch'),
    ('wrong_port','output_port_type_mismatch'),
    ('future_binding','input_producer_not_scheduled_ancestor'),
    ('claim_authorized','schema:'),
    ('unknown_operation','unregistered_operation'),
    ('missing_operator_guard','authority_change_owner_missing'),
])
def test_reject_invalid_proposal(packet,mutation,code):
    plan, reg, schema = copy.deepcopy(packet)
    nodes = {n['id']:n for n in plan['nodes']}
    if mutation == 'duplicate': plan['nodes'].append(copy.deepcopy(plan['nodes'][0]))
    elif mutation == 'unknown_dependency': nodes['N02']['dependencies'][0]['node_id'] = 'absent'
    elif mutation == 'cycle': nodes['N01']['dependencies'].append({'node_id':'N18','on_outcomes':['completed']})
    elif mutation == 'missing_guard': nodes['N14']['required_guards'].remove('permit_active')
    elif mutation == 'unsafe_retry': nodes['N14']['retry']['mode'] = 'bounded_idempotent'
    elif mutation == 'drop_uncertainty': nodes['N14']['on_failure'] = 'block_descendants'
    elif mutation == 'wrong_owner': nodes['N14']['owner_role'] = 'learning_agent'
    elif mutation == 'unknown_artifact': nodes['N01']['inputs']['source']['artifact_id'] = 'absent'
    elif mutation == 'hash_drift': plan['artifacts'][0]['payload']['changed'] = True
    elif mutation == 'wrong_port': nodes['N05']['inputs']['baseline']['output_name'] = 'does_not_exist'
    elif mutation == 'future_binding': nodes['N05']['inputs']['baseline']['producer_node_id'] = 'N17'
    elif mutation == 'claim_authorized': plan['execution_authorized'] = True
    elif mutation == 'unknown_operation': nodes['N14']['operation'] = 'shell.arbitrary'
    elif mutation == 'missing_operator_guard': nodes['N11']['required_guards'].remove('authenticated_operator')
    with pytest.raises(ModelError, match=code):
        validate(plan,reg,schema)
