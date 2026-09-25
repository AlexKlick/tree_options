"""Read-only evidence routes on the existing cockpit, not a second server."""
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
