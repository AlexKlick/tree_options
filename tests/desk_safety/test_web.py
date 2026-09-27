"""Read-only evidence routes on the existing cockpit, not a second server."""
import json

from fastapi.testclient import TestClient

from tree_options.trex_web.app import create_app


def client(world):
    return TestClient(create_app(state_dir=str(world.root / 'legacy'),
        plans_dir=str(world.root / 'plans'), discovery_dir=str(world.root / 'discovery'),
        desk_state_dir=str(world.root / 'desk-state')))


def test_readiness_does_not_claim_missing_evidence_is_healthy(world):
    response = client(world).get('/api/desk/health')
    assert response.status_code == 503
    doc = response.json()
    assert doc['evidence_status'] == 'not_initialized'
    assert doc['execution_enabled'] is False
    assert not (world.root / 'desk-state').exists()


def test_summary_and_page_are_read_only(world):
    c = client(world)
    response = c.get('/api/desk/scorecards')
    assert response.status_code == 200 and response.json()['families'] == []
    page = c.get('/desk/evidence')
    assert page.status_code == 200
    assert 'No desk broker orders' in page.text
    assert 'legacy execution is separate' in page.text
    assert 'frame-ancestors' in page.headers['content-security-policy']
    assert c.post('/api/desk/scorecards', json={'execution_enabled': True}).status_code == 405
    assert not (world.root / 'desk-state').exists()


def test_corrupt_store_is_not_silently_replaced(world):
    db = world.root / 'desk-state' / 'evidence' / 'desk.sqlite3'
    db.parent.mkdir(parents=True)
    db.write_bytes(b'not a database')
    response = client(world).get('/api/desk/scorecards')
    assert response.status_code == 503
    assert response.json()['error'] == 'evidence_unavailable'
    assert db.read_bytes() == b'not a database'
    assert str(world.root) not in response.text


def test_historical_replay_reports_are_read_only_and_summary_only(world):
    out = world.root / 'desk-store' / 'evaluations' / 'historical-replay'
    out.mkdir(parents=True)
    (out / 'replay-20260927T000000Z-abcdef.json').write_text(json.dumps({
        'schema': 'desk-historical-replay/1', 'label': 'exploratory modeled VWAP replay',
        'spec': {'start': '2025-01-01', 'end': '2025-02-01'},
        'counts': {'evaluable_within_trade_cap': 1},
        'by_structure': {'long_call': {'trades': 1, 'wins': 1, 'win_rate': 1.0}},
        'by_variant': {'pead_beat/long_call': {'trades': 1, 'wins': 1, 'win_rate': 1.0}},
        'provenance': {'sources': []}, 'limitations': ['not a fill'],
        'rows': [{'name': 'SPY', 'pnl': 100}],
    }))
    c = TestClient(create_app(state_dir=str(world.root / 'legacy'),
        plans_dir=str(world.root / 'plans'), discovery_dir=str(world.root / 'discovery'),
        desk_state_dir=str(world.root / 'desk-state'), desk_store_dir=str(world.root / 'desk-store')))
    response = c.get('/api/desk/historical-replays')
    assert response.status_code == 200
    doc = response.json()
    assert doc['execution_enabled'] is False
    assert doc['reports'][0]['counts']['evaluable_within_trade_cap'] == 1
    assert 'rows' not in doc['reports'][0]
    assert c.post('/api/desk/historical-replays').status_code == 405


def test_portfolio_scenarios_are_read_only_and_summary_only(world):
    out = world.root / 'desk-store' / 'evaluations' / 'portfolio-scenario'
    out.mkdir(parents=True)
    (out / 'portfolio-20260927T000000Z-abcdef.json').write_text(json.dumps({
        'schema': 'desk-portfolio-scenario/1', 'label': 'exploratory',
        'spec': {'intended_capital': '5000', 'max_trade_loss': '300', 'max_open_loss': '1500'},
        'variants': {'xsmom_top3/call_debit': {'considered': 1, 'admitted': 1,
            'skipped': {'trade_cap': 0, 'open_cap': 0, 'capital': 0},
            'peak_open_loss_reserved': '100', 'closed_pnl': '10',
            'ending_closed_capital': '5010', 'minimum_closed_capital': '5000',
            'admitted_decision_names': ['2025-01-01:SPY']}},
        'provenance': {'replay_sha256': 'a' * 64, 'code_head': 'b' * 40, 'code_dirty': False},
        'limitations': ['not a fill'], 'private_rows': [{'name': 'SPY'}],
    }))
    c = TestClient(create_app(state_dir=str(world.root / 'legacy'),
        plans_dir=str(world.root / 'plans'), discovery_dir=str(world.root / 'discovery'),
        desk_state_dir=str(world.root / 'desk-state'), desk_store_dir=str(world.root / 'desk-store')))
    response = c.get('/api/desk/portfolio-scenarios')
    assert response.status_code == 200
    assert response.json()['reports'][0]['variants']['xsmom_top3/call_debit']['admitted'] == 1
    assert 'admitted_decision_names' not in response.json()['reports'][0]['variants']['xsmom_top3/call_debit']
    assert 'private_rows' not in response.json()['reports'][0]
    assert c.post('/api/desk/portfolio-scenarios').status_code == 405


def test_intraday_graphs_project_only_summary_and_never_actions(world):
    out = world.root / 'desk-store' / 'evaluations' / 'intraday-graph' / 'run-one'
    out.mkdir(parents=True)
    (out / 'rolling-put-credit.summary.json').write_text(json.dumps({
        'schema': 'desk-intraday-graph-summary/1', 'policy': 'put_credit',
        'source_sha256': 'a' * 64, 'requested_contracts': 72, 'captured_contracts': 72,
        'traded_minute_bars': 1000, 'limitations': ['not a fill'],
        'windows': [{'start': '2026-05-26', 'end': '2026-08-25', 'sessions': 64,
                     'scheduled_snapshots': 512, 'potential_trades': 100, 'entered': 3,
                     'modeled_wins': 1, 'modeled_losses': 2, 'closed_capital_proxy': '4998',
                     'open_at_end': 0, 'minimum_closed_capital_proxy': '4970',
                     'peak_open_loss_reserved': '300',
                     'private_actions': [{'candidate_id': 'secret'}]}],
        'execution_authorized': False, 'private_bars': [1, 2, 3],
    }))
    c = TestClient(create_app(state_dir=str(world.root / 'legacy'),
        plans_dir=str(world.root / 'plans'), discovery_dir=str(world.root / 'discovery'),
        desk_state_dir=str(world.root / 'desk-state'), desk_store_dir=str(world.root / 'desk-store')))
    response = c.get('/api/desk/intraday-graphs')
    assert response.status_code == 200
    doc = response.json()
    assert doc['execution_enabled'] is False
    assert doc['reports'][0]['windows'][0]['scheduled_snapshots'] == 512
    assert 'private_actions' not in doc['reports'][0]['windows'][0]
    assert 'private_bars' not in doc['reports'][0]
    assert c.post('/api/desk/intraday-graphs').status_code == 405
