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
    skill = digest['skill']
    assert skill and all(isinstance(a.get('verdict'), str) for a in skill['arms'].values())
    # the mini run predates the measured cost ledger: absence stays visible as
    # None (fail-closed), never an invented zero-drop ledger
    assert skill['no_price'] is None
    assert all(a['boards_dropped_unpriced'] is None for a in skill['arms'].values())
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


# ---------------------------------------- forward digest shapes (open PRs #45/#48)
#
# The producer on origin/main cannot emit these keys yet (the per-arm null
# machinery and the no-price ledger live on open PR branches), so the fixtures
# below hand-build digests shaped EXACTLY like those branches' producers:
# - ``vs_random_own`` / ``null_percentile_own``: origin/fix/desk-per-arm-null-clean,
#   longrun.score_run emits ``{"vs_random_own": {**paired(...), "p_enter":
#   round(own.p_enter, 4)}}`` where paired() = {diff_total, ci95, p_one_sided,
#   sessions};
# - ``no_price`` / ``boards_dropped_unpriced`` / ``cost_provenance``:
#   origin/feat/cost-model-measured-v2, skill.arm_skill/skill_section, keys
#   asserted in tests/unit/test_desk_no_price_propagation.py on that branch.


def _write_run(world, name: str, digest: dict) -> None:
    target = root(world) / name
    target.mkdir(parents=True)
    (target / 'progress.json').write_text(json.dumps(
        {'schema': longrun.PROGRESS_SCHEMA, 'status': 'finished', 'total': 8,
         'finished': 8, 'failures': 0,
         'quota': {'ok': True, 'reason': '', 'checked_at': None}, 'arms': {},
         'eta_s': None}))
    (target / 'digest.json').write_text(json.dumps(digest))


def _digest(standings: list, *, skill: dict | None = None) -> dict:
    return {'schema': longrun.DIGEST_SCHEMA,
            'promotion': {'promoted': False, 'rule': 'preregistered'},
            'standings': standings, 'skill': skill}


def _degenerate_standings() -> list[dict]:
    """Two standings rows on which the legacy shared-null column and the
    per-arm own-null column DISAGREE about the ranking.

    Hand-derived arithmetic: 8 boards, and every random entry loses 0.50 on
    average in this window, so a null at entry rate r expects 8 * r * -0.50.
    The shared incumbent null (r = 0.875) expects -3.50 for EVERY arm -- the
    one shared constant that makes ``vs_random`` degenerate as a ranking key
    (each arm's diff_total is its net_total minus that same constant).
      'lazy':  entered 2/8, net_total 2.00 -> vs_random 2.00 + 3.50 = 5.50
               (ci95 low 4.00); its OWN null (r = 0.25) expects -1.00 ->
               vs_random_own 3.00 (ci95 low -0.50), p_enter 0.25.
      'eager': entered 8/8, net_total 1.00 -> vs_random 1.00 + 3.50 = 4.50
               (ci95 low 2.50); its OWN null (r = 1.00) expects -4.00 ->
               vs_random_own 5.00 (ci95 low 2.00), p_enter 1.00.
    Degenerate order: lazy (4.00) above eager (2.50). Own-null order: eager
    (2.00) above lazy (-0.50). The direction is the one PR #45 documents in
    score_run: "a low-entry-rate arm used to be handed a large constant credit
    simply because the shared null entered ~0.9"."""
    lazy = {'arm': 'lazy', 'policy': 'lazy', 'repeat': 1, 'kind': 'model',
            'boards': 8, 'entered': 2, 'entry_rate': 0.25, 'unevaluable': 0,
            'failures': 0, 'net_total': 2.0, 'net_ci95': [0.5, 3.5],
            'vs_random': {'diff_total': 5.5, 'ci95': [4.0, 7.0], 'p_one_sided': 0.002,
                          'sessions': 8},
            'vs_random_own': {'diff_total': 3.0, 'ci95': [-0.5, 5.0], 'p_one_sided': 0.31,
                              'sessions': 8, 'p_enter': 0.25},
            'vs_first_row': None, 'vs_incumbent': None, 'vs_regime': None,
            'null_percentile': 0.99, 'null_percentile_own': 0.72}
    eager = {'arm': 'eager', 'policy': 'eager', 'repeat': 1, 'kind': 'model',
             'boards': 8, 'entered': 8, 'entry_rate': 1.0, 'unevaluable': 0,
             'failures': 0, 'net_total': 1.0, 'net_ci95': [-0.5, 2.5],
             'vs_random': {'diff_total': 4.5, 'ci95': [2.5, 6.5], 'p_one_sided': 0.04,
                           'sessions': 8},
             'vs_random_own': {'diff_total': 5.0, 'ci95': [2.0, 7.0], 'p_one_sided': 0.03,
                               'sessions': 8, 'p_enter': 1.0},
             'vs_first_row': None, 'vs_incumbent': None, 'vs_regime': None,
             'null_percentile': 0.96, 'null_percentile_own': 0.9}
    return [lazy, eager]


def test_standings_carry_the_own_null_columns_when_the_digest_has_them(world) -> None:
    _write_run(world, '20260930T000000Z', _digest(_degenerate_standings()))
    standings = client(world).get('/api/desk/longrun').json()['digest']['standings']
    served = {row['arm']: row for row in standings}
    assert served['lazy']['vs_random_own'] == {
        'diff_total': 3.0, 'ci95': [-0.5, 5.0], 'p_one_sided': 0.31, 'sessions': 8,
        'p_enter': 0.25}
    assert served['lazy']['null_percentile_own'] == 0.72
    assert served['eager']['vs_random_own']['p_enter'] == 1.0
    # the legacy shared-null column is still served (retained, never ranked on)
    assert served['lazy']['vs_random']['diff_total'] == 5.5


def test_standings_rank_on_the_own_null_not_the_degenerate_shared_column(world) -> None:
    lazy, eager = _degenerate_standings()
    # file order = the degenerate order today's producer writes on disk
    # (longrun.score_run sorts on -vs_random.ci95[0])
    _write_run(world, '20260930T000000Z', _digest([lazy, eager]))
    standings = client(world).get('/api/desk/longrun').json()['digest']['standings']
    assert [row['arm'] for row in standings] == ['eager', 'lazy']
    # the fallback: a digest old enough to lack the own-null columns keeps the
    # legacy (degenerate) order, byte-for-byte unchanged behavior
    for row in (lazy, eager):
        row.pop('vs_random_own')
        row.pop('null_percentile_own')
    _write_run(world, '20260930T010000Z', _digest([lazy, eager]))
    doc = client(world).get('/api/desk/longrun').json()
    assert doc['run'] == '20260930T010000Z'
    assert [row['arm'] for row in doc['digest']['standings']] == ['lazy', 'eager']


def _refused_skill_section() -> dict:
    """A post-#48 skill section. Key shapes as asserted by
    tests/unit/test_desk_no_price_propagation.py on
    origin/feat/cost-model-measured-v2 (section: total/by_arm/by_reason;
    per arm: no_price{total,snapshots,reasons}, boards_dropped_unpriced,
    cost_provenance, verdict prefix 'NO PRICE (N dropped): ')."""
    reasons = {'unknown_symbol': 1, 'no_delta': 1, 'stale_quote': 1}
    provenance = {'source': 'cboe-delayed-eod-chains',
                  'snapshot_window_et': '17:45-06:30',
                  'decision_clocks_et': '09:35, 10:00, 14:30',
                  'describes_fill_clock': False}
    return {
        'schema': 'desk-skill/1',
        'arms': {
            'm#1': {'policy': 'm#1', 'complete': True, 'excess_total': 0.0,
                    'intervals': {'excess': {'block_ci95': [-1.5, 1.5]}},
                    'cs_forward': {'significant': False},
                    'no_price': {'total': 3, 'snapshots': ['s3', 's4', 's5'],
                                 'reasons': reasons},
                    'boards_dropped_unpriced': 3, 'cost_provenance': provenance,
                    'verdict': ('NO PRICE (3 dropped): NO SKILL DETECTED on the priced '
                                'boards; Descriptive; nothing promoted.')},
            'm#2': {'policy': 'm#2', 'complete': True, 'excess_total': 12.0,
                    'intervals': {'excess': {'block_ci95': [2.0, 22.0]}},
                    'cs_forward': {'significant': True},
                    'no_price': {'total': 0, 'snapshots': [], 'reasons': {}},
                    'boards_dropped_unpriced': 0, 'cost_provenance': None,
                    'verdict': 'EXCESS DETECTED; Descriptive; nothing promoted.'}},
        'no_price': {'total': 3, 'by_arm': {'m#1': 3}, 'by_reason': reasons}}


def test_the_skill_payload_carries_the_no_price_ledger_and_cost_provenance(world) -> None:
    _write_run(world, '20260930T000000Z', _digest([], skill=_refused_skill_section()))
    skill = client(world).get('/api/desk/longrun').json()['digest']['skill']
    assert skill['no_price'] == {'total': 3, 'by_arm': {'m#1': 3},
                                 'by_reason': {'unknown_symbol': 1, 'no_delta': 1,
                                               'stale_quote': 1}}
    refused = skill['arms']['m#1']
    assert refused['boards_dropped_unpriced'] == 3
    assert refused['no_price']['snapshots'] == ['s3', 's4', 's5']
    assert refused['cost_provenance']['source'] == 'cboe-delayed-eod-chains'
    assert refused['cost_provenance']['describes_fill_clock'] is False
    assert refused['verdict'].startswith('NO PRICE (3 dropped): ')


def test_a_refused_run_is_distinguishable_from_break_even_and_from_never_looking(world):
    _write_run(world, '20260930T000000Z', _digest([], skill=_refused_skill_section()))
    skill = client(world).get('/api/desk/longrun').json()['digest']['skill']
    clean = skill['arms']['m#2']
    assert clean['no_price']['total'] == 0
    assert not clean['verdict'].startswith('NO PRICE')  # clean, not refused
    # a section that predates the ledger serves None, never an invented zero
    # (#48's fail-closed guard: an omitted ledger cannot be told apart from a
    # clean one, so absence must stay visible)
    legacy = {'arms': {'m#1': {'excess_total': 0.0,
                               'verdict': 'NO SKILL DETECTED; nothing promoted.'}}}
    _write_run(world, '20260930T010000Z', _digest([], skill=legacy))
    served = client(world).get('/api/desk/longrun').json()['digest']['skill']
    assert served['no_price'] is None
    assert served['arms']['m#1']['boards_dropped_unpriced'] is None
