"""Source-session quote provenance for frozen paper episodes."""

from datetime import date
from decimal import Decimal

import pytest

from tree_options.desk import shadows
from tree_options.desk.contracts import ContractError
from tree_options.desk.evidence import EvidenceStore
from tree_options.desk.sessions import cutoff_instant
from tree_options.desk.store import encode_document

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
        assert store.get("quality", f"{DEAL_ID}|{DEADLINE}|{CODE}") is not None
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
