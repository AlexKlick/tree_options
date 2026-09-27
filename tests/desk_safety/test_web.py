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
