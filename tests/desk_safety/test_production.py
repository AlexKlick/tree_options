"""Command/health integration: operational success is not trading readiness."""

import json
from datetime import datetime, time

from tree_options.desk import production, shadows
from tree_options.desk.sessions import cutoff_instant
from tree_options.trex.clock import ET


def invoke(world, capsys, args, *, now=None):
    from tree_options.desk.__main__ import run_cli

    code = run_cli([*args, "--database", str(world.db)], now=now or world.now, cal=world.cal)
    return code, json.loads(capsys.readouterr().out)


def test_read_only_commands_do_not_create_missing_database(world, capsys):
    code, doc = invoke(world, capsys, ["desk-health"])
    assert code == 3 and doc["evidence_status"] == "not_initialized"
    assert doc["execution_status"] == "disabled_unimplemented"
    assert not world.db.exists()
    code, doc = invoke(world, capsys, ["scorecards"])
    assert code == 0 and doc["status"] == "not_initialized"
    assert not world.db.exists()


def test_admission_cli_refuses_armed_and_never_writes(world, capsys):
    world.queue_file()
    code, doc = invoke(world, capsys, ["desk-enter", "--armed", "--queue-dir", str(world.queues)])
    assert code == 1 and doc["error"] == "execution_not_implemented"
    assert not world.db.exists()


def test_shadow_cli_dry_run_and_integrity_commands(world, capsys):
    world.queue_file()
    args = [
        "shadows",
        "--session",
        world.entry.isoformat(),
        "--queue-dir",
        str(world.queues),
        "--store-root",
        str(world.store),
    ]
    now = cutoff_instant(world.entry)
    code, doc = invoke(world, capsys, [*args, "--dry-run"], now=now)
    assert code == 3  # missing entry day's NEXT-session queue, but adoption works
    assert doc["episodes_created"] == 1 and not world.db.exists()
    code, doc = invoke(world, capsys, args, now=now)
    assert code == 3 and world.db.exists()
    code, doc = invoke(world, capsys, ["verify-evidence"], now=now)
    assert code == 0 and doc["ok"]
    dest = world.root / "backup.sqlite3"
    code, doc = invoke(world, capsys, ["backup-evidence", "--out", str(dest)], now=now)
    assert code == 0 and dest.exists()
    assert doc["audit"]["head_sha256"]


def test_censored_outcomes_degrade_health(world):
    world.queue_file()
    shadows.update_shadows(
        session=world.deadline,
        now=cutoff_instant(world.deadline),
        cal=world.cal,
        database=world.db,
        store_root=world.store,
        queue_dir=world.queues,
    )
    next_day = world.cal.nth_after(world.deadline, 1)
    doc = production.health(
        database=world.db, now=datetime.combine(next_day, time(10), ET), cal=world.cal
    )
    assert doc["evidence_status"] == "degraded"
    assert "missing_current_queue" in doc["evidence_blockers"]
    assert "censored_deadline_outcomes" in doc["evidence_blockers"]
    assert doc["execution_enabled"] is False


def test_bad_evidence_cli_reports_stable_error_without_private_paths(world, capsys):
    world.db.parent.mkdir()
    world.db.write_bytes(b"not a database")
    code, doc = invoke(world, capsys, ["verify-evidence"])
    assert code == 1 and doc["error"] == "database_error"
    assert str(world.root) not in json.dumps(doc)


def test_publication_not_due_is_pending_not_missing_data_alarm(world):
    from tree_options.desk.evidence import EvidenceStore

    with EvidenceStore(world.db):
        pass
    doc = production.health(database=world.db, now=cutoff_instant(world.session), cal=world.cal)
    assert doc["evidence_status"] == "awaiting_publication"
    assert "missing_current_queue" not in doc["evidence_blockers"]
    assert "queue_publication_pending" in doc["pending"]


# ---- WT8-a: the verdict path's mutation gap — health reads what it found ----
# The store holds EXACTLY the rows each test names, written with EvidenceStore.put
# (not through shadows.update_shadows). Every `now` falls at or after
# world.session's 16:15 ET cutoff and before the next session's, so health's
# expected session is world.session (2025-03-03) in all five. The episode below is
# CLOSED at that as-of: entered 2025-02-24, deadline 2025-02-28.
_WT8A_DEAL = "d-wt8a-health"


def _wt8a_episode():
    return {
        "deal_id": _WT8A_DEAL,
        "entry_session": "2025-02-24",
        "exit_deadline": "2025-02-28",
        "row": "R1",
        "tier": "signal",
        "miner_sha256": "a" * 64,
        "playbook_sha256": "b" * 64,
        "source_session": "2025-02-21",
    }


def _wt8a_store(world, *, evaluation=False, queue=False, episode=False, void=False):
    """Write exactly the named rows into a fresh store (none: an empty store)."""
    from tree_options.desk.evidence import EvidenceStore

    session = world.session.isoformat()
    at = cutoff_instant(world.session)
    with EvidenceStore(world.db) as store, store.atomic():
        if evaluation:
            store.put("evaluation", session, {"session": session}, at)
        if queue:
            store.put("queue", session, {"session": session}, at)
        if episode:
            store.put("episode", _WT8A_DEAL, _wt8a_episode(), at)
        if void:
            row = {"deal_id": _WT8A_DEAL, "session": "2025-02-26", "code": "voided_corrupt_quote"}
            store.put("quality", f"{_WT8A_DEAL}|2025-02-26|voided_corrupt_quote", row, at)


def test_an_invalidated_episode_degrades_health(world):
    """An INVALIDATED episode counts censored (scorecards.summarize), and the
    censored count is what keeps invalidation loud to health."""
    _wt8a_store(world, evaluation=True, queue=True, episode=True, void=True)
    doc = production.health(
        database=world.db, now=datetime.combine(world.session, time(17), ET), cal=world.cal
    )
    assert doc["expected_session"] == world.session.isoformat()  # the premise
    assert doc["evaluated_through"] == world.session.isoformat()  # the premise
    assert doc["evidence_status"] == "degraded", (
        "an invalidated episode must degrade health: it counts censored, and a"
        " censored outcome is a blocker"
    )
    assert doc["evidence_blockers"] == ["censored_deadline_outcomes"]
    assert doc["pending"] == []
    assert doc["censored_total"] == 1, "health reports the censored total it computed"


def test_a_complete_session_is_ready(world):
    _wt8a_store(world, evaluation=True, queue=True)
    doc = production.health(
        database=world.db, now=datetime.combine(world.session, time(17), ET), cal=world.cal
    )
    assert doc["expected_session"] == world.session.isoformat()  # the premise
    assert doc["evidence_status"] == "ready", (
        "an evaluation exactly at the expected session is not lagging: with the"
        " queue recorded and nothing censored, health is ready"
    )
    assert doc["pending"] == []
    assert doc["evidence_blockers"] == []
    assert doc["evaluated_through"] == world.session.isoformat()
    assert doc["current_queue_recorded"] is True
    assert doc["censored_total"] == 0


def test_an_unevaluated_store_reports_no_horizon(world):
    _wt8a_store(world)
    doc = production.health(database=world.db, now=cutoff_instant(world.session), cal=world.cal)
    assert doc["expected_session"] == world.session.isoformat()  # the premise
    assert doc["evaluated_through"] is None, (
        "a store with no evaluation has no horizon: it is pending, never evaluated"
    )
    assert "evaluation_cycle_pending" in doc["pending"]
    assert "queue_publication_pending" in doc["pending"]
    assert doc["evidence_status"] == "awaiting_publication"
    assert doc["evidence_blockers"] == []


def test_an_evaluation_is_lagging_after_1935_et(world):
    _wt8a_store(world, queue=True)
    late = production.health(
        database=world.db, now=datetime.combine(world.session, time(20), ET), cal=world.cal
    )
    assert late["expected_session"] == world.session.isoformat()  # the premise
    assert late["evidence_status"] == "degraded", (
        "the evaluation cycle is due at 19:35 ET: missing at 20:00 ET it is lagging"
    )
    assert "evaluation_lagging" in late["evidence_blockers"]
    assert "evaluation_cycle_pending" not in late["pending"]
    early = production.health(
        database=world.db, now=datetime.combine(world.session, time(19), ET), cal=world.cal
    )
    assert early["expected_session"] == world.session.isoformat()  # the premise
    assert "evaluation_cycle_pending" in early["pending"], (
        "before 19:35 ET a missing evaluation is still pending"
    )
    assert "evaluation_lagging" not in early["evidence_blockers"]


def test_a_blocker_outranks_a_pending_item(world):
    _wt8a_store(world, evaluation=True, episode=True)
    doc = production.health(database=world.db, now=cutoff_instant(world.session), cal=world.cal)
    assert doc["expected_session"] == world.session.isoformat()  # the premise
    assert doc["evaluated_through"] == world.session.isoformat()  # the premise
    assert doc["evidence_status"] == "degraded", (
        "a blocker outranks a pending item: the status is degraded whenever a blocker exists"
    )
    assert doc["evidence_blockers"] == ["censored_deadline_outcomes"]
    assert doc["pending"] == ["queue_publication_pending"]
