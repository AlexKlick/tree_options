"""Read-only evidence routes on the existing cockpit, not a second server."""
import json
from datetime import UTC, datetime

from fastapi.testclient import TestClient

from tree_options.trex.supervised import SupervisedPaths, grant_mandate
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


def _supervised_world(world, monkeypatch):
    """Point the env-defaulted desk/supervised/lab roots at tmp dirs."""
    run_dir = world.root / 'desk-paper-run'
    supervised_dir = world.root / 'supervised'
    monkeypatch.setenv('TREX_DESK_RUN_DIR', str(run_dir))
    monkeypatch.setenv('TREX_SUPERVISED_DIR', str(supervised_dir))
    monkeypatch.setenv('DESK_STORE', str(world.root / 'desk-store'))
    return run_dir, supervised_dir


def test_supervised_status_is_a_read_only_dump(world, monkeypatch):
    run_dir, supervised_dir = _supervised_world(world, monkeypatch)
    run_dir.mkdir(parents=True)
    (run_dir / 'owner.json').write_text(json.dumps({'pid': 4242}))
    (run_dir / 'book.json').write_text(json.dumps({'structures': {
        'canary-2026-09-28-a': {'status': 'open', 'filled_qty': 1,
                                'exit_filled_qty': 0}}}))
    (run_dir / 'HALT').write_text('')
    (run_dir / 'events.jsonl').write_text(
        json.dumps({'kind': 'entry_request', 'status': 'refused'}) + '\n')
    inbox = run_dir / 'requests'
    inbox.mkdir()
    (inbox / 'canary-2026-09-28-b.json').write_text('{}')
    (inbox / 'canary-2026-09-28-a.result.json').write_text(json.dumps({
        'schema': 'desk-entry-result/1', 'at': '2026-09-28T15:00:00+00:00',
        'request': 'canary-2026-09-28-a.json', 'intent_id': 'canary-2026-09-28-a',
        'status': 'refused', 'reason': 'kill_file_present'}))
    grant_mandate(SupervisedPaths(supervised_dir), now=datetime.now(UTC),
        account_id='DU1234567', owner_epoch='gateway-epoch-1',
        strategy_version='operational-canary/1', profile_digest='c' * 64,
        max_orders=5, ttl_seconds=3 * 24 * 60 * 60,
        granted_by='operator-terminal')
    c = client(world)
    response = c.get('/api/desk/supervised')
    assert response.status_code == 200
    doc = response.json()
    assert doc['schema'] == 'desk-cli-status/1'
    assert doc['kill_files'] == ['HALT']
    assert doc['book']['canary-2026-09-28-a'] == {'status': 'open', 'open_qty': 1}
    assert doc['inbox'] == ['canary-2026-09-28-b.json']
    assert doc['last_results'][0]['status'] == 'refused'
    assert doc['events'][0]['kind'] == 'entry_request'
    mandate = doc['supervised']['mandate']
    assert mandate['state'] == 'active' and mandate['orders_used'] == 0 \
        and mandate['max_orders'] == 5 and mandate['long_running'] is True
    assert mandate['days_left'] in (2, 3)  # the route stamps its own now
    assert c.post('/api/desk/supervised', json={}).status_code == 405
    assert (run_dir / 'book.json').read_text().startswith('{"structures"')


def test_supervised_status_is_unavailable_when_the_book_is_corrupt(world, monkeypatch):
    run_dir, _ = _supervised_world(world, monkeypatch)
    run_dir.mkdir(parents=True)
    (run_dir / 'book.json').write_text('not json')
    response = client(world).get('/api/desk/supervised')
    assert response.status_code == 503
    assert response.json()['error'] == 'evidence_unavailable'
    assert (run_dir / 'book.json').read_text() == 'not json'
    assert str(world.root) not in response.text


def test_lab_scoreboard_aggregates_runs_and_never_promotes(world, monkeypatch):
    run_dir, _ = _supervised_world(world, monkeypatch)
    lab = world.root / 'desk-store' / 'evaluations' / 'lab'
    runs = {'run-a': ('5010', 2, 1, 1), 'run-b': ('4980', 1, 0, 1),
            'run-c': ('5005', 1, 1, 0)}
    for name, (closed, entered, wins, losses) in runs.items():
        out = lab / name
        out.mkdir(parents=True)
        (out / 'summary.json').write_text(json.dumps({
            'schema': 'desk-lab-run/1', 'policy': 'model:zai', 'status': 'ok',
            'boards_shown': 4, 'model_calls': 4, 'model_failures': 0,
            'summary': {'entered': entered, 'modeled_wins': wins,
                        'modeled_losses': losses, 'closed_capital_proxy': closed,
                        'minimum_closed_capital_proxy': '4970'}}))
    flat = lab / 'run-flat'
    flat.mkdir(parents=True)
    (flat / 'summary.json').write_text(json.dumps({
        'schema': 'desk-lab-run/1', 'policy': 'no_trade', 'status': 'ok',
        'boards_shown': 0, 'model_calls': 0, 'model_failures': 0,
        'summary': {'entered': 0, 'modeled_wins': 0, 'modeled_losses': 0,
                    'closed_capital_proxy': '5000'}}))
    c = client(world)
    response = c.get('/api/desk/lab')
    assert response.status_code == 200
    doc = response.json()
    assert doc['schema'] == 'desk-lab-scoreboard/1'
    assert doc['execution_enabled'] is False
    policy = doc['policies']['model:zai']
    assert policy['runs'] == 3 and policy['entered'] == 4
    assert policy['modeled_wins'] == 2 and policy['modeled_losses'] == 2
    assert policy['closed_pnl_sum'] == '-5'
    assert doc['advisory']['policy'] == 'model:zai'
    assert doc['advisory']['promoted'] is False
    assert c.post('/api/desk/lab', json={}).status_code == 405
    assert not run_dir.exists()  # reading the lab never touches the desk run dir


# ---------------------------------------------------- automation controls


class FakeSystemctl:
    """Records every call; answers `show` from a scripted state map."""

    def __init__(self, state=None):
        self.state = state or {}
        self.calls = []

    def __call__(self, args):
        self.calls.append(list(args))
        if args[:1] == ['show']:
            prop = args[args.index('--property') + 1]
            return self.state.get((args[1], prop), '')
        return ''


def _automation_world(world, monkeypatch, state=None):
    import tree_options.trex_web.automation as automation_mod
    import tree_options.trex_web.desk_view as desk_view_mod
    run_dir = world.root / 'desk-paper'
    run_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv('TREX_DESK_RUN_DIR', str(run_dir))
    fake = FakeSystemctl(state)
    monkeypatch.setattr(desk_view_mod, 'automation_status',
                        lambda root: automation_mod.automation_status(root, systemctl=fake))
    monkeypatch.setattr(desk_view_mod, 'automation_action',
                        lambda root, key, action: automation_mod.automation_action(
                            root, key, action, systemctl=fake))
    return run_dir, fake


def test_automation_status_lists_whitelisted_timers_and_kill_files(world, monkeypatch):
    _run_dir, _fake = _automation_world(world, monkeypatch, state={
        ('desk-lab.timer', 'UnitFileState'): 'enabled',
        ('desk-lab.timer', 'ActiveState'): 'active',
        ('desk-lab.timer', 'NextElapseUSecRealtime'): 'Mon 2026-09-28 18:17:00 MDT',
        ('desk-lab.service', 'Result'): 'success',
    })
    c = client(world)
    doc = c.get('/api/desk/automation').json()
    keys = {t['key'] for t in doc['timers']}
    assert keys == {'desk-lab', 'desk-lab-overnight', 'desk-challenge',
                    'desk-supervised-preview'}
    lab = next(t for t in doc['timers'] if t['key'] == 'desk-lab')
    assert lab['enabled'] and lab['active']
    assert lab['next_elapse'].startswith('Mon 2026')
    assert doc['kill_files'] == []


def test_automation_actions_are_whitelisted_and_audited(world, monkeypatch):
    run_dir, fake = _automation_world(world, monkeypatch)
    c = client(world)
    assert c.post('/api/desk/automation/desk-lab/disable').status_code == 200
    assert ['disable', '--now', 'desk-lab.timer'] in fake.calls
    assert c.post('/api/desk/automation/desk-lab/run').status_code == 200
    assert ['start', 'desk-lab.service'] in fake.calls
    # unknown unit / unknown action: refused, no systemctl call for them
    before = len(fake.calls)
    assert c.post('/api/desk/automation/trex-monitor/disable').status_code == 404
    assert c.post('/api/desk/automation/desk-lab/restart').status_code == 422
    assert len(fake.calls) == before
    audit = [json.loads(line) for line in (run_dir / 'automation.jsonl').read_text().splitlines()]
    assert [a['action'] for a in audit] == ['disable', 'run']
    assert all(a['actor'] == 'cockpit' for a in audit)


def test_supervised_control_toggles_kill_files(world, monkeypatch):
    run_dir, _ = _automation_world(world, monkeypatch)
    c = client(world)
    assert c.post('/api/desk/supervised/halt').json()['kill_files'] == ['HALT']
    assert (run_dir / 'HALT').exists()
    assert c.post('/api/desk/supervised/flatten').json()['kill_files'] == ['FLATTEN', 'HALT']
    assert c.post('/api/desk/supervised/resume').json()['kill_files'] == []
    assert not (run_dir / 'HALT').exists()
    assert c.post('/api/desk/supervised/arm').status_code == 422


def test_a_failed_systemctl_is_unavailable_not_silent_false(world, monkeypatch):
    """Live defect 09-28: the serving unit blocked the session bus (AF_UNIX)
    and every timer showed 'disabled'. A failing systemctl must surface as
    503, never as empty state."""
    import tree_options.trex_web.automation as automation_mod
    import tree_options.trex_web.desk_view as desk_view_mod

    def broken(args):
        raise RuntimeError('systemctl show: rc=1 Failed to connect to bus')

    monkeypatch.setattr(desk_view_mod, 'automation_status',
                        lambda root: automation_mod.automation_status(root, systemctl=broken))
    assert client(world).get('/api/desk/automation').status_code == 503
