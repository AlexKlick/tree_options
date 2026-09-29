"""GET /api/desk/longrun: the read-only long-run projection on the cockpit."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from tests.unit.test_desk_longrun import FakeAsk, run

from tree_options.desk import longrun
from tree_options.trex_web.app import create_app


def client(world) -> TestClient:
    return TestClient(create_app(state_dir=str(world.root / 'legacy'),
        plans_dir=str(world.root / 'plans'), discovery_dir=str(world.root / 'discovery'),
        desk_state_dir=str(world.root / 'desk-state'),
        desk_store_dir=str(world.root / 'desk-store')))


def root(world) -> Path:
    return world.root / 'desk-store' / 'evaluations' / 'longrun'


@pytest.fixture(autouse=True)
def _no_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(longrun.DIR_ENV, raising=False)


def test_no_run_yet_is_an_empty_200(world) -> None:
    response = client(world).get('/api/desk/longrun')
    assert response.status_code == 200
    doc = response.json()
    assert doc == {'schema': longrun.VIEW_SCHEMA, 'run': None, 'progress': None,
                   'digest': None, 'execution_enabled': False}


def test_finished_run_serves_progress_and_standings_read_only(world) -> None:
    run(root(world) / '20260929T000000Z', FakeAsk())
    c = client(world)
    response = c.get('/api/desk/longrun')
    assert response.status_code == 200
    assert response.headers['cache-control'] == 'no-store'
    doc = response.json()
    assert doc['run'] == '20260929T000000Z' and doc['execution_enabled'] is False
    assert doc['progress']['status'] == 'finished'
    assert doc['progress']['arms']['m#1']['done'] == 6
    digest = doc['digest']
    assert digest['promotion']['promoted'] is False
    assert digest['untrusted_note'].startswith('UNTRUSTED / NEVER PROMOTED')
    assert digest['aa']['status'] == 'valid'
    arms = {row['arm'] for row in digest['standings']}
    assert {'m#1', 'm#2', 'first_row', 'no_trade', 'always_bullish'} <= arms
    assert all(len(row['net_ci95']) == 2 for row in digest['standings'])
    assert digest['random_null']['seeds'] == 200
    assert str(world.root) not in response.text  # no absolute paths (receipts omitted)
    assert c.post('/api/desk/longrun').status_code == 405


def test_the_latest_run_is_served_while_it_is_still_running(world) -> None:
    run(root(world) / '20260929T000000Z', FakeAsk())
    live = root(world) / '20260929T010000Z'
    live.mkdir(parents=True)
    (live / 'progress.json').write_text(json.dumps({
        'schema': longrun.PROGRESS_SCHEMA, 'status': 'paused', 'total': 10, 'finished': 4,
        'failures': 1, 'paused_s': 300, 'quota': {'ok': False, 'reason': 'left=40 planned=60'},
        'arms': {}, 'eta_s': None}))
    doc = client(world).get('/api/desk/longrun').json()
    assert doc['run'] == '20260929T010000Z' and doc['digest'] is None
    assert doc['progress']['status'] == 'paused'


def test_corrupt_progress_is_a_503(world) -> None:
    target = root(world) / '20260929T000000Z'
    target.mkdir(parents=True)
    (target / 'progress.json').write_text('{"torn": ')
    response = client(world).get('/api/desk/longrun')
    assert response.status_code == 503
    assert response.json()['error'] == 'evidence_unavailable'
    assert str(world.root) not in response.text


def test_a_digest_claiming_promotion_is_refused(world) -> None:
    target = root(world) / '20260929T000000Z'
    run(target, FakeAsk())
    doc = json.loads((target / 'digest.json').read_text())
    doc['promotion']['promoted'] = True
    (target / 'digest.json').write_text(json.dumps(doc))
    assert client(world).get('/api/desk/longrun').status_code == 503


def test_env_override_serves_the_prototype_progress(world, tmp_path: Path,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    legacy = tmp_path / 'v1-full'
    legacy.mkdir()
    (legacy / 'progress.json').write_text(json.dumps({
        'at': '2026-09-29T00:47:02+00:00', 'finished': 243, 'total': 1376,
        'calls_this_process': 250, 'failures': 86, 'calls_per_s': 1.157, 'eta_min': 16.3,
        'quota': 'left=97.0 planned=85.2', 'paused_s': 0,
        'policies': {'m31-A': {'done': 121, 'of': 688, 'fail': 42, 'entered': 76, 'row0': 70}}}))
    monkeypatch.setenv(longrun.DIR_ENV, str(legacy))
    doc = client(world).get('/api/desk/longrun').json()
    assert doc['run'] == 'v1-full' and doc['progress']['legacy'] == 'run_v1'
    arm = doc['progress']['arms']['m31-A']
    assert (arm['done'], arm['total'], arm['failures'], arm['entered']) == (121, 688, 42, 76)
    assert doc['progress']['eta_s'] == pytest.approx(978.0)
