"""Outcome quality, denominator transparency and no implicit promotion."""

from datetime import date
from decimal import Decimal

import pytest

from tree_options.desk import scorecards, shadows
from tree_options.desk.evidence import EvidenceError, EvidenceStore
from tree_options.desk.sessions import cutoff_instant


def record(world, session=None):
    d = session or world.deadline
    return shadows.update_shadows(
        session=d,
        now=cutoff_instant(d),
        cal=world.cal,
        database=world.db,
        store_root=world.store,
        queue_dir=world.queues,
    )


def test_missing_store_is_not_zero_pnl_or_healthy(world):
    doc = scorecards.build_scorecards(world.db)
    assert doc["status"] == "not_initialized"
    assert not doc["execution_enabled"]
    assert doc["families"] == []
    assert not world.db.exists()


def test_missing_deadline_remains_censored_not_a_loss_or_win(world):
    world.queue_file()
    world.chain(world.entry)
    record(world)
    card = scorecards.build_scorecards(world.db)["families"][0]
    assert card["n_episodes"] == 1
    assert card["n_resolved"] == 0 and card["n_censored"] == 1
    assert card["modeled_net_pnl_dollars"] is None
    assert card["promotion_ready"] is False
    world.chain(world.deadline)
    record(world)
    card = scorecards.build_scorecards(world.db)["families"][0]
    assert card["n_resolved"] == 1 and card["n_censored"] == 0
    assert Decimal(card["modeled_net_pnl_dollars"]) == Decimal("-12.60")
    assert Decimal(card["assumed_commissions_dollars"]) == Decimal("2.60")
    assert card["win_rate"] == 0


def test_open_episodes_do_not_count_as_censored(world):
    world.queue_file()
    record(world, world.entry)
    card = scorecards.build_scorecards(world.db)["families"][0]
    assert card["n_open"] == 1 and card["n_censored"] == 0


def test_asof_view_does_not_use_future_evaluation_or_marks(world):
    world.queue_file()
    world.chain(world.deadline)
    record(world)
    card = scorecards.build_scorecards(world.db, as_of=world.entry)["families"][0]
    assert card["n_open"] == 1 and card["n_resolved"] == 0


def test_vendor_conflict_disqualifies_previous_mark_without_deleting_it(world):
    world.queue_file()
    path, _doc = world.chain(world.deadline)
    record(world)
    path.with_name("AAPL.conflict.json.gz").write_bytes(b"conflict")
    record(world)
    card = scorecards.build_scorecards(world.db)["families"][0]
    assert card["n_resolved"] == 0 and card["n_censored"] == 1
    assert card["n_invalidated"] == 1
    with EvidenceStore(world.db, readonly=True) as store:
        assert len(store.all("mark")) == 1  # retained for audit, not silently rewritten


def test_tampered_store_never_returns_a_healthy_summary(world):
    world.queue_file()
    record(world)
    with EvidenceStore(world.db) as store:
        store.conn.execute("DELETE FROM objects WHERE kind='episode'")
    with pytest.raises(EvidenceError):
        scorecards.build_scorecards(world.db)


def test_count_floor_does_not_authorize_promotion(world):
    # Independent outcome records test the summary boundary, not twenty
    # duplicated calls to the production selector as a bogus sample generator.
    episodes = []
    marks = []
    horizon = world.cal.nth_after(world.entry, 30)
    for i in range(20):
        did = f"example-{i}"
        episodes.append(
            {
                "deal_id": did,
                "entry_session": world.cal.nth_after(world.entry, i).isoformat(),
                "exit_deadline": horizon.isoformat(),
                "row": "R1",
                "tier": "signal",
                "miner_sha256": "a" * 64,
                "playbook_sha256": "b" * 64,
                "source_session": world.session.isoformat(),
            }
        )
        marks.append(
            {
                "deal_id": did,
                "session": horizon.isoformat(),
                "modeled_net_pnl_dollars": "1.00",
                "modeled_gross_pnl_dollars": "3.60",
                "modeled_fees_dollars": "2.60",
            }
        )
    card = scorecards.summarize(episodes, marks, [], as_of=horizon)[0]
    assert card["sample_floor_met"] is True
    assert card["promotion_ready"] is False
    assert "deadline_eod_proxy_is_not_execution_evidence" in card["promotion_blockers"]


def test_historical_card_does_not_use_quotes_received_after_requested_session(world):
    world.queue_file()
    later = world.cal.nth_after(world.deadline, 1)
    world.chain(world.deadline, fetched=cutoff_instant(later))
    record(world, later)
    current = scorecards.build_scorecards(world.db)["families"][0]
    historical = scorecards.build_scorecards(world.db, as_of=world.deadline)["families"][0]
    assert current["n_resolved"] == 1
    assert historical["n_resolved"] == 0 and historical["n_censored"] == 1


def test_historical_card_does_not_use_quality_events_discovered_after_requested_session(world):
    """A vendor conflict discovered on D+1 must censor the current view but
    not the as-of D view (the same cutoff principle marks already follow).
    Codex round 1, finding 1."""
    world.queue_file()
    world.chain(world.deadline)
    record(world)
    later = world.cal.nth_after(world.deadline, 1)
    world.chain(later)  # a later chain exists; the deadline one stays valid
    conflict = world.store / "chains" / world.deadline.isoformat() / "AAPL.conflict.json.gz"
    conflict.parent.mkdir(parents=True, exist_ok=True)
    conflict.write_bytes(b"conflict")
    record(world, later)  # discovers the conflict a session later
    current = scorecards.build_scorecards(world.db)["families"][0]
    historical = scorecards.build_scorecards(world.db, as_of=world.deadline)["families"][0]
    assert current["n_resolved"] == 0 and current["n_censored"] == 1
    assert historical["n_resolved"] == 1 and historical["n_censored"] == 0


def test_future_asof_does_not_censor_unobserved_outcomes(world):
    """An as-of beyond the evaluated horizon must clamp to that horizon:
    outcomes that simply have not happened yet are open, not censored.
    Codex round 1, finding 4."""
    world.queue_file()
    record(world, world.entry)  # evaluation exists through the entry only
    future = world.cal.nth_after(world.deadline, 2)
    doc = scorecards.build_scorecards(world.db, as_of=future)
    card = doc["families"][0]
    assert card["n_open"] == 1 and card["n_censored"] == 0
    assert doc["as_of_clamped_to_evaluation"] is True


# ---- WT7-a: a void means what it says — the scorecard reads the whole episode ----
# An episode is INVALIDATED when an invalidating quality row of its deal has
# entry_session <= session <= the as-of horizon (NOT the exit_deadline). One
# episode: deal D, entry 2026-11-16, deadline 2026-11-18, a clean deadline mark.
_D = "d-wt7a-void"
_ENTRY, _DEADLINE, _HORIZON = "2026-11-16", "2026-11-18", "2026-11-23"


def _episode(*, deal=_D, deadline=_DEADLINE):
    return {
        "deal_id": deal,
        "entry_session": _ENTRY,
        "exit_deadline": deadline,
        "row": "R1",
        "tier": "signal",
        "miner_sha256": "a" * 64,
        "playbook_sha256": "b" * 64,
        "source_session": "2026-11-13",
    }


def _mark(*, deal=_D, session=_DEADLINE):
    return {
        "deal_id": deal,
        "session": session,
        "modeled_net_pnl_dollars": "1.00",
        "modeled_gross_pnl_dollars": "3.60",
        "modeled_fees_dollars": "2.60",
    }


def _row(session, *, deal=_D, code="voided_corrupt_quote"):
    return {"deal_id": deal, "session": session, "code": code}


def _class(card):
    """The one-episode card's class. An invalidated episode counts censored
    AND invalidated; anything else is a reading no class describes."""
    counts = (card["n_resolved"], card["n_censored"], card["n_invalidated"], card["n_open"])
    names = {
        (1, 0, 0, 0): "RESOLVED",
        (0, 1, 1, 0): "INVALIDATED",
        (0, 0, 0, 1): "OPEN",
        (0, 1, 0, 0): "CENSORED",
    }
    return names.get(counts, f"UNCLASSIFIED{counts}")


def _card(quality, *, as_of=_HORIZON, deadline=_DEADLINE):
    [card] = scorecards.summarize(
        [_episode(deadline=deadline)], [_mark()], quality, as_of=date.fromisoformat(as_of)
    )
    assert card["n_episodes"] == 1
    return card


def test_a_void_filed_after_the_deadline_invalidates_the_episode():
    """The owner's real case (qsl.sqlite3, NVDA): the void is filed on 11-20
    for an episode whose deadline is 11-18. A void the reading never reads is
    not a void."""
    card = _card([_row("2026-11-20")])
    assert _class(card) == "INVALIDATED", (
        "a voided_corrupt_quote filed after the deadline but on or before the"
        " as-of horizon must invalidate the episode"
    )
    assert card["modeled_net_pnl_dollars"] is None


def test_a_void_from_before_the_entry_session_is_not_this_episodes():
    before = _card([_row("2026-11-13")])
    assert _class(before) == "RESOLVED", (
        "a void on a session before the entry session belongs to no session of"
        " this episode: the window starts at the entry session"
    )
    on_entry = _card([_row(_ENTRY)])
    assert _class(on_entry) == "INVALIDATED", (
        "a void ON the entry session is inside the window: the lower bound is inclusive"
    )


def test_a_void_discovered_after_the_requested_as_of_does_not_count(world):
    """The knowledge cutoff still bounds which rows an as-of card sees. The
    void row is written through the store's own API (no code path writes
    voided_corrupt_quote), the way shadows records a quality event, at a
    DISCOVERY instant after the as-of 11-18 cutoff: unknown to that card,
    known to the current one."""
    as_of = date.fromisoformat(_DEADLINE)
    with EvidenceStore(world.db) as store, store.atomic():
        store.put("episode", _D, _episode(), cutoff_instant(date(2026, 11, 13)))
        store.put(
            "mark",
            f"{_D}|{_DEADLINE}",
            {**_mark(), "fetched_at": cutoff_instant(as_of).isoformat()},
            cutoff_instant(as_of),
        )
        store.put(
            "quality",
            f"{_D}|2026-11-17|voided_corrupt_quote",
            _row("2026-11-17"),
            cutoff_instant(date(2026, 11, 20)),  # discovered after the as-of cutoff
        )
        store.put("evaluation", _HORIZON, {"session": _HORIZON}, cutoff_instant(date(2026, 11, 23)))
    historical = scorecards.build_scorecards(world.db, as_of=as_of)
    assert historical["as_of_session"] == _DEADLINE
    assert historical["as_of_clamped_to_evaluation"] is False
    assert _class(historical["families"][0]) == "RESOLVED", (
        "a void recorded after the requested as-of's cutoff is not yet known to that card"
    )
    current = scorecards.build_scorecards(world.db)
    assert current["as_of_session"] == _HORIZON
    assert _class(current["families"][0]) == "INVALIDATED", (
        "once recorded, the void on a session inside the episode's window invalidates it"
    )


def test_another_deals_void_does_not_count():
    card = _card([_row("2026-11-17", deal="d-wt7a-another-deal")])
    assert _class(card) == "RESOLVED", (
        "a void filed for another deal must not touch this deal's episode:"
        " the window is read on the episode's own deal_id"
    )


def test_a_censored_class_row_inside_the_window_with_a_clean_deadline_stays_resolved():
    assert "chain_missing" not in scorecards._INVALIDATING  # the premise: a censoring code
    card = _card([_row("2026-11-17", code="chain_missing")])
    assert _class(card) == "RESOLVED", (
        "a censoring code is decided by the deadline session: the deadline mark is"
        " clean, so a missing chain on an intermediate session changes nothing"
    )


def test_a_void_after_the_as_of_horizon_on_a_closed_episode_does_not_count():
    beyond = _card([_row("2026-11-25")], as_of=_HORIZON)
    assert _class(beyond) == "RESOLVED", (
        "a void on a session after the as-of horizon is outside the window:"
        " the upper bound is the horizon"
    )
    on_horizon = _card([_row(_HORIZON)], as_of=_HORIZON)
    assert _class(on_horizon) == "INVALIDATED", (
        "a void ON the as-of horizon is inside the window: the upper bound is inclusive"
    )


def test_an_open_episode_with_a_void_inside_its_window_stays_open():
    card = _card([_row("2026-11-20")], deadline="2026-11-28")
    assert card["n_open"] == 1 and card["n_invalidated"] == 0 and card["n_censored"] == 0, (
        "an episode whose deadline is after the as-of horizon stays open, whatever"
        " its window holds so far"
    )
