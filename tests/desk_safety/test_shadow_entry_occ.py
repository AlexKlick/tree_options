"""Source-session quote provenance for frozen paper episodes."""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from tree_options.desk import scorecards, shadows
from tree_options.desk.contracts import ContractError
from tree_options.desk.evidence import EvidenceStore
from tree_options.desk.sessions import cutoff_instant
from tree_options.desk.store import encode_document, read_chain

DEAL_ID = "d-20261113-qsl-put-credit-NVDA-98470619e0ec"
SOURCE = date(2026, 11, 13)
ENTRY = date(2026, 11, 16)
DEADLINE = date(2026, 11, 18)
EXP = "2026-11-20"
CODE = "entry_fill_occ_provenance"
B1 = ("B1VDA261120P00150000", 150, 0.11, 0.41)
LONG = ("NVDA261120P00150000", 150, 2.83, 2.89)
SHORT = ("NVDA261120P00155000", 155, 4.35, 4.45)


def write_chain(world, session, symbol, cols):
    at = cutoff_instant(session).isoformat()
    doc = {
        "header": {
            "schema": "desk-chain/1",
            "underlying": symbol,
            "session": session.isoformat(),
            "source_as_of": at,
            "fetched_at": at,
            "underlying_quote": {"last_trade_time": at},
        },
        "columns": cols,
    }
    path = world.store / "chains" / session.isoformat() / f"{symbol}.json.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(encode_document(doc))


def chain(world, session, rows):
    cols = {
        name: [row[i] for row in rows] for i, name in enumerate(("occ", "strike", "bid", "ask"))
    }
    cols.update(right=["P"] * len(rows), exp=[EXP] * len(rows))
    write_chain(world, session, "NVDA", cols)


def frozen_episode(deal_id, symbol, kind, source, entry, deadline, fill, legs, quotes):
    return {
        "deal_id": deal_id,
        "entry_session": entry.isoformat(),
        "exit_deadline": deadline.isoformat(),
        "source_session": source.isoformat(),
        "commission_per_contract_per_side": "0.65",
        "deal": {
            "fill": fill,
            "legs": [
                dict(leg, bid=str(bid), ask=str(ask))
                for leg, (bid, ask) in zip(legs, quotes, strict=True)
            ],
            "structure": {
                "id": deal_id,
                "underlying": symbol,
                "kind": kind,
                "entry_date": entry.isoformat(),
                "exit_deadline": deadline.isoformat(),
                "quantity": 1,
                "limit": fill,
                "legs": legs,
                "exits": {"touch": False, "breach": False},
            },
        },
    }


def episode(long_quote):
    legs = [
        {"right": "P", "expiry": EXP, "strike": "155", "action": "SELL"},
        {"right": "P", "expiry": EXP, "strike": "150", "action": "BUY"},
    ]
    return frozen_episode(
        DEAL_ID,
        "NVDA",
        "credit_vertical",
        SOURCE,
        ENTRY,
        DEADLINE,
        "3.94" if long_quote == B1 else "1.46",
        legs,
        [SHORT[2:], long_quote[2:]],
    )


def write_episode(world, ep):
    with EvidenceStore(world.db) as store, store.atomic():
        store.put(
            "episode", ep["deal_id"], ep, cutoff_instant(date.fromisoformat(ep["entry_session"]))
        )


def run(world, session=DEADLINE):
    return shadows.update_shadows(
        session=session,
        now=cutoff_instant(session),
        cal=world.cal,
        database=world.db,
        store_root=world.store,
        queue_dir=world.queues,
    )


@pytest.fixture
def deadline_chain(world):
    # A standard-only exit isolates the entry check: closing the spread costs 2.90.
    chain(world, DEADLINE, [(SHORT[0], 155, 4.85, 4.9), (LONG[0], 150, 2.0, 2.07)])


def test_blended_entry_records_quality_instead_of_a_deadline_mark(world, deadline_chain):
    chain(world, SOURCE, [B1, LONG, SHORT])
    ep = episode(B1)
    write_episode(world, ep)
    assert run(world)["marks_created"] == 0
    with EvidenceStore(world.db, readonly=True) as store:
        assert store.all("mark") == []
        assert store.get("quality", f"{DEAL_ID}|{DEADLINE}|{CODE}") == {
            "deal_id": DEAL_ID,
            "session": DEADLINE.isoformat(),
            "code": CODE,
        }
        assert store.get("episode", DEAL_ID) == ep
    assert run(world)["quality_events"] == 0


@pytest.mark.parametrize("occ", [LONG[0], None], ids=["standard", "non-string-kept"])
def test_standard_entry_writes_the_deadline_mark(world, deadline_chain, occ):
    chain(world, SOURCE, [B1, (LONG[0], 150, None, None), (occ, *LONG[1:]), SHORT])
    write_episode(world, episode(LONG))
    assert run(world)["marks_created"] == 1
    with EvidenceStore(world.db, readonly=True) as store:
        mark = store.get("mark", f"{DEAL_ID}|{DEADLINE}")
        assert mark is not None
        assert (mark["realistic"], mark["modeled_net_pnl_dollars"]) == ("2.9", "-146.60")


def test_missing_source_chain_refuses_until_it_arrives(world, deadline_chain):
    write_episode(world, episode(LONG))
    assert run(world)["marks_created"] == 0
    with EvidenceStore(world.db, readonly=True) as store:
        # WT4-b E1: the source chain's own cause, not the catch-all
        assert store.get("quality", f"{DEAL_ID}|{DEADLINE}|entry_source_chain_missing") is not None
        assert store.get("quality", f"{DEAL_ID}|{DEADLINE}|{CODE}") is None
    chain(world, SOURCE, [LONG, SHORT])
    assert run(world)["marks_created"] == 1


def test_recorded_bid_and_ask_must_match_on_one_standard_row(world, deadline_chain):
    # The two kept twins can each explain one side. Only B1 explains the pair.
    chain(world, SOURCE, [B1, (LONG[0], 150, 0.11, 2.89), (LONG[0], 150, 0.10, 0.41), SHORT])
    with pytest.raises(ContractError, match=f"^{CODE}$"):
        shadows._read_mark(episode(B1), DEADLINE, world.store, cutoff_instant(DEADLINE))


@pytest.mark.parametrize("absent", ["source_session", "legs", "structure_legs", "one_leg", "bid"])
def test_incomplete_episode_cannot_be_marked(world, deadline_chain, absent):
    chain(world, SOURCE, [LONG, SHORT])
    ep = episode(LONG)
    if absent == "source_session":
        del ep["source_session"]
    elif absent == "structure_legs":
        ep["deal"]["structure"]["legs"] = []
    elif absent == "one_leg":
        ep["deal"]["legs"].pop()
    elif absent == "bid":
        del ep["deal"]["legs"][0]["bid"]
    else:
        ep["deal"]["legs"] = []
    with pytest.raises(ContractError, match=f"^{CODE}$"):
        shadows._read_mark(ep, DEADLINE, world.store, cutoff_instant(DEADLINE))


# Reconstructed R5 put calendars. The sessions, fills and expected marks are
# pinned to the brief. The books exercise source/exit separation in isolation.
@pytest.mark.parametrize(
    ("deal_id", "source", "front", "back", "fill", "books"),
    [
        (
            "d-20261102-R5-IWM-1f1ce7b7a654",
            2,
            (9.65, 9.75),
            (11.95, 12.07),
            "2.365",
            [
                (5, 10.23, 12.09, "1.86", "-53.10"),
                (6, 9.81, 12.48, "2.67", "27.90"),
                (9, 10.10, 13.42, "3.32", "92.90"),
            ],
        ),
        (
            "d-20261106-R5-IWM-0114d4f7f870",
            6,
            (11.66, 11.83),
            (14.74, 14.93),
            "3.18",
            [
                (9, 12.00, 15.24, "3.24", "3.40"),
                (10, 11.87, 15.13, "3.26", "5.40"),
                (11, 11.82, 15.13, "3.31", "10.40"),
            ],
        ),
    ],
)
def test_clean_calendar_marks_preserve_the_modeled_fill(
    world, deal_id, source, front, back, fill, books
):
    source = date(2026, 11, source)
    entry = world.cal.nth_after(source, 1)
    deadline = date(2026, 11, books[-1][0])
    expiries = ["2026-11-13", "2026-12-04"]
    legs = [
        {"right": "P", "expiry": exp, "strike": "280", "action": action}
        for exp, action in zip(expiries, ("SELL", "BUY"), strict=True)
    ]
    ep = frozen_episode(
        deal_id, "IWM", "calendar", source, entry, deadline, fill, legs, [front, back]
    )
    write_episode(world, ep)
    cols = {
        "occ": ["IWM261113P00280000", "IWM261204P00280000"],
        "strike": [280, 280],
        "right": ["P", "P"],
        "exp": expiries,
    }
    write_chain(world, source, "IWM", dict(cols, bid=[front[0], back[0]], ask=[front[1], back[1]]))
    for day, short_ask, long_bid, _, _ in books:
        write_chain(
            world,
            date(2026, 11, day),
            "IWM",
            dict(
                cols,
                bid=[round(short_ask - 0.10, 2), long_bid],
                ask=[short_ask, round(long_bid + 0.24, 2)],
            ),
        )
    assert run(world, deadline)["marks_created"] == 3
    with EvidenceStore(world.db, readonly=True) as store:
        actual = [
            (m["session"], Decimal(m["realistic"]), Decimal(m["modeled_net_pnl_dollars"]))
            for m in store.all("mark")
        ]
    assert actual == [
        (date(2026, 11, day).isoformat(), Decimal(realistic), Decimal(net))
        for day, _, _, realistic, net in books
    ]


# ---- WT4-b (2026-11-22): E1, E2, E3 and F-B1 ---------------------------------

B1_SHORT = ("B1VDA261120P00155000", 155, 0.21, 0.51)
PREV_SESSION = date(2026, 11, 12)
INVALIDATING_CHAIN_CAUSES = {
    "chain_vendor_conflict",
    "chain_identity",
    "chain_schema",
    "chain_source_session",
    # WT6-a: evidence present and wrong, same class as identity/schema
    "chain_header_source_after_fetch",
}


def scored(ep):
    """The episode plus the family fields the production scorecard keys on."""
    return dict(ep, row="QSL", tier=None, miner_sha256="m" * 64, playbook_sha256="p" * 64)


def quality_codes(world):
    with EvidenceStore(world.db, readonly=True) as store:
        return {q["code"] for q in store.all("quality")}


def the_family(world):
    (family,) = scorecards.build_scorecards(world.db)["families"]
    return family


def test_a_rebuilt_store_still_invalidates_the_blended_entry(world, deadline_chain):
    """Census C1-A: the NVDA episode's INVALIDATED verdict rested on its
    2026-11-18 ``duplicate_contract_quote`` row, which WT2-b's exit filter no
    longer produces. On a store the current code writes, the deadline row is
    the entry guard's own, and that row alone must invalidate the episode;
    an unwitnessed entry fill is a defect, not missing data (WT4-b E1)."""
    chain(world, SOURCE, [B1, LONG, SHORT])
    write_episode(world, scored(episode(B1)))
    run(world)
    assert quality_codes(world) == {CODE}  # no duplicate_contract_quote row exists
    family = the_family(world)
    assert (family["n_episodes"], family["n_resolved"]) == (1, 0)
    assert (family["n_censored"], family["n_invalidated"]) == (1, 1)


def _source_doc(rows):
    at = cutoff_instant(SOURCE).isoformat()
    cols = {
        name: [row[i] for row in rows] for i, name in enumerate(("occ", "strike", "bid", "ask"))
    }
    cols.update(right=["P"] * len(rows), exp=[EXP] * len(rows))
    header = {
        "schema": "desk-chain/1",
        "underlying": "NVDA",
        "session": SOURCE.isoformat(),
        "source_as_of": at,
        "fetched_at": at,
        "underlying_quote": {"last_trade_time": at},
    }
    return {"header": header, "columns": cols}


@pytest.mark.parametrize(
    "cause",
    [
        "chain_missing",
        "chain_vendor_conflict",
        "chain_schema",
        "chain_identity",
        "chain_source_session",
        "chain_occ_missing",
        "chain_column_lengths",
        "chain_header_source_after_fetch",
    ],
)
def test_a_failed_source_chain_keeps_its_own_cause(world, deadline_chain, cause):
    """WT4-b E1: the catch-all stored every source-chain failure as
    ``entry_fill_occ_provenance`` (ContractError is a ValueError), losing the
    cause and censoring what should invalidate. Each failure now keeps its
    own code under ``entry_source_`` and is scored exactly as the same code
    on an exit chain: conflict/identity/schema/stale invalidate, a missing
    chain censors. E2: a chain with no occ column is refused by name (it
    cannot witness OCC provenance), and censors. F-B1: a ragged occ column
    is a recorded quality event on the source chain too. WT6-a: a header
    whose source stamp is after its own fetch has its own permanent code
    (test_shadow_chain_header_times), and the prefix carries it to the
    entry side unchanged."""
    doc = _source_doc([LONG, SHORT])
    path = world.store / "chains" / SOURCE.isoformat() / "NVDA.json.gz"
    if cause == "chain_vendor_conflict":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.with_name("NVDA.conflict.json.gz").write_bytes(encode_document(doc))
    elif cause == "chain_schema":
        doc["header"]["schema"] = "desk-chain/0"
    elif cause == "chain_identity":
        doc["header"]["underlying"] = "TSLA"
    elif cause == "chain_source_session":
        doc["header"]["source_as_of"] = cutoff_instant(PREV_SESSION).isoformat()
    elif cause == "chain_occ_missing":
        del doc["columns"]["occ"]
    elif cause == "chain_column_lengths":
        doc["columns"]["occ"].append("NVDA261120P00160000")  # ragged: one occ too many
    elif cause == "chain_header_source_after_fetch":
        # fetched five minutes before the source stamp it carries; both known
        fetched = cutoff_instant(SOURCE) - timedelta(minutes=5)
        doc["header"]["fetched_at"] = fetched.isoformat()
    if cause != "chain_missing":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(encode_document(doc))
    write_episode(world, scored(episode(LONG)))

    assert run(world)["marks_created"] == 0
    code = f"{shadows.ENTRY_SOURCE_PREFIX}{cause}"
    assert quality_codes(world) == {code}
    invalidates = cause in INVALIDATING_CHAIN_CAUSES
    # the entry side is classified exactly as the exit side classifies the cause
    assert (code in scorecards._INVALIDATING) is (cause in scorecards._INVALIDATING) is invalidates
    assert the_family(world)["n_invalidated"] == int(invalidates)


def restamp(world, session, **stamps):
    """Re-write a written NVDA chain's header times (WT6-a)."""
    path = world.store / "chains" / session.isoformat() / "NVDA.json.gz"
    doc = read_chain(path)
    doc["header"].update(stamps)
    path.write_bytes(encode_document(doc))


@pytest.mark.parametrize(
    ("fetched", "source", "code", "invalidates"),
    [
        (0, 1, "chain_header_source_after_fetch", True),
        (2, 0, "chain_not_known_at_asof", False),
    ],
    ids=["corrupt-header-invalidates", "late-fetch-only-censors"],
)
def test_the_exit_side_classifies_each_header_time_as_the_entry_side_does(
    world, fetched, source, code, invalidates
):
    """WT6-a, the exit side's scorecard class: the mirror of the test above,
    with the roles swapped. The source chain is clean and the DEADLINE chain's
    header is wrong. Evidence present and wrong (a source stamp after the
    chain's own fetch) INVALIDATES the (deal, exit_deadline) test; evidence not
    yet available (a fetch after the as-of) only censors, the class
    ``chain_not_known_at_asof`` has always had. One class per cause, on both
    sides."""
    chain(world, SOURCE, [LONG, SHORT])
    # every chain is clean; the deadline chain's two header times are set below
    for day in (date(2026, 11, 16), date(2026, 11, 17), DEADLINE):
        chain(world, day, [LONG, SHORT])
    t = cutoff_instant(DEADLINE)
    restamp(
        world,
        DEADLINE,
        fetched_at=(t + fetched * timedelta(hours=1)).isoformat(),
        source_as_of=(t + source * timedelta(hours=1)).isoformat(),
    )
    write_episode(world, scored(episode(LONG)))
    run(world)
    with EvidenceStore(world.db, readonly=True) as store:
        assert store.get("mark", f"{DEAL_ID}|{DEADLINE}") is None
    assert quality_codes(world) == {code}
    family = the_family(world)
    assert (family["n_episodes"], family["n_resolved"]) == (1, 0)
    assert (family["n_censored"], family["n_invalidated"]) == (1, int(invalidates))


def test_b1_twins_on_both_chains_the_production_scenario(world):
    """WT4-b E3: the production shape — B1 twins on the SOURCE chain AND on
    every EXIT chain, at both legs' keys. The blended entry is refused on
    every session with the guard's own code (never ``duplicate_contract_quote``:
    the exit filter drops the twins); the standard entry is marked from the
    standard exit rows only, never the twins' prices."""
    twins = [B1, LONG, B1_SHORT, SHORT]
    chain(world, SOURCE, twins)
    for day, (short_ask, long_bid) in {16: (4.6, 2.5), 17: (4.7, 2.3), 18: (4.9, 2.0)}.items():
        chain(
            world,
            date(2026, 11, day),
            [
                (B1[0], 150, 0.01, 0.02),  # a twin whose price would invent a mark
                (LONG[0], 150, long_bid, round(long_bid + 0.07, 2)),
                (B1_SHORT[0], 155, 9.0, 9.5),
                (SHORT[0], 155, round(short_ask - 0.05, 2), short_ask),
            ],
        )
    blended = scored(episode(B1))
    write_episode(world, blended)
    run(world)
    with EvidenceStore(world.db, readonly=True) as store:
        assert store.all("mark") == []
        assert {(q["session"], q["code"]) for q in store.all("quality")} == {
            (f"2026-11-{day}", CODE) for day in (16, 17, 18)
        }

    clean_id = DEAL_ID.replace("98470619e0ec", "0123456789ab")
    clean = scored(episode(LONG))
    clean["deal_id"] = clean["deal"]["structure"]["id"] = clean_id
    write_episode(world, clean)
    run(world)
    with EvidenceStore(world.db, readonly=True) as store:
        marks = {m["session"]: m for m in store.all("mark") if m["deal_id"] == clean_id}
        assert not [q for q in store.all("quality") if q["deal_id"] == clean_id]
    # credit 1.46 frozen from the standard rows; close = short ask - long bid
    assert {s: (m["realistic"], m["modeled_net_pnl_dollars"]) for s, m in marks.items()} == {
        "2026-11-16": ("2.1", "-66.60"),
        "2026-11-17": ("2.4", "-96.60"),
        "2026-11-18": ("2.9", "-146.60"),
    }


def test_a_ragged_exit_occ_is_a_quality_event_not_a_rolled_back_run(world):
    """WT4-b F-B1, the regression WT2-b introduced: an exit chain whose occ
    list is longer than its quote columns made standard_row_indexes return
    an index past them; the IndexError escaped ``except ContractError`` and
    rolled back the whole store.atomic() run, every run, until the chain
    was fixed. It is now ``chain_column_lengths``, recorded, and the run
    commits (the reviewer's repro asserted the IndexError; this asserts the
    fix)."""
    chain(world, SOURCE, [LONG, SHORT])
    rows = [(SHORT[0], 155, 4.85, 4.9), (LONG[0], 150, 2.0, 2.07)]
    cols = {name: [r[i] for r in rows] for i, name in enumerate(("occ", "strike", "bid", "ask"))}
    cols.update(right=["P", "P"], exp=[EXP, EXP])
    cols["occ"].append("NVDA261120P00145000")  # ragged
    write_chain(world, DEADLINE, "NVDA", cols)
    write_episode(world, episode(LONG))

    report = run(world)  # must not raise

    assert report["marks_created"] == 0
    with EvidenceStore(world.db, readonly=True) as store:
        assert store.get("quality", f"{DEAL_ID}|{DEADLINE}|chain_column_lengths") is not None
        assert store.get("evaluation", DEADLINE.isoformat()) is not None  # the run committed
