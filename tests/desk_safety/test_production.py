"""Command/health integration: operational success is not trading readiness."""
import json
from datetime import datetime, time

from tree_options.desk import production, shadows
from tree_options.desk.sessions import cutoff_instant
from tree_options.trex.clock import ET


def invoke(world, capsys, args, *, now=None):
    from tree_options.desk.__main__ import run_cli
    code = run_cli([*args, '--database', str(world.db)], now=now or world.now, cal=world.cal)
    return code, json.loads(capsys.readouterr().out)


def test_read_only_commands_do_not_create_missing_database(world, capsys):
    code, doc = invoke(world, capsys, ['desk-health'])
    assert code == 3 and doc['evidence_status'] == 'not_initialized'
    assert doc['execution_status'] == 'disabled_unimplemented'
    assert not world.db.exists()
    code, doc = invoke(world, capsys, ['scorecards'])
    assert code == 0 and doc['status'] == 'not_initialized'
    assert not world.db.exists()


def test_admission_cli_refuses_armed_and_never_writes(world, capsys):
    world.queue_file()
    code, doc = invoke(world, capsys, ['desk-enter', '--armed', '--queue-dir', str(world.queues)])
    assert code == 1 and doc['error'] == 'execution_not_implemented'
    assert not world.db.exists()


def test_shadow_cli_dry_run_and_integrity_commands(world, capsys):
    world.queue_file()
    args = ['shadows', '--session', world.entry.isoformat(), '--queue-dir', str(world.queues),
            '--store-root', str(world.store)]
    now = cutoff_instant(world.entry)
    code, doc = invoke(world, capsys, [*args, '--dry-run'], now=now)
    assert code == 3  # missing entry day's NEXT-session queue, but adoption works
    assert doc['episodes_created'] == 1 and not world.db.exists()
    code, doc = invoke(world, capsys, args, now=now)
    assert code == 3 and world.db.exists()
    code, doc = invoke(world, capsys, ['verify-evidence'], now=now)
    assert code == 0 and doc['ok']
    dest = world.root / 'backup.sqlite3'
    code, doc = invoke(world, capsys, ['backup-evidence', '--out', str(dest)], now=now)
    assert code == 0 and dest.exists()
    assert doc['audit']['head_sha256']


def test_censored_outcomes_degrade_health(world):
    world.queue_file()
    shadows.update_shadows(session=world.deadline, now=cutoff_instant(world.deadline),
        cal=world.cal, database=world.db, store_root=world.store, queue_dir=world.queues)
    next_day = world.cal.nth_after(world.deadline, 1)
    doc = production.health(database=world.db, now=datetime.combine(next_day, time(10), ET), cal=world.cal)
    assert doc['evidence_status'] == 'degraded'
    assert 'missing_current_queue' in doc['evidence_blockers']
    assert 'censored_deadline_outcomes' in doc['evidence_blockers']
    assert doc['execution_enabled'] is False


def test_bad_evidence_cli_reports_stable_error_without_private_paths(world, capsys):
    world.db.parent.mkdir()
    world.db.write_bytes(b'not a database')
    code, doc = invoke(world, capsys, ['verify-evidence'])
    assert code == 1 and doc['error'] == 'database_error'
    assert str(world.root) not in json.dumps(doc)


def test_publication_not_due_is_pending_not_missing_data_alarm(world):
    from tree_options.desk.evidence import EvidenceStore
    with EvidenceStore(world.db):
        pass
    doc = production.health(database=world.db, now=cutoff_instant(world.session), cal=world.cal)
    assert doc['evidence_status'] == 'awaiting_publication'
    assert 'missing_current_queue' not in doc['evidence_blockers']
    assert 'queue_publication_pending' in doc['pending']
