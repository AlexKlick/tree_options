"""The shadow mark's OCC integrity: only standard contracts are priced.

Since 2026-11-05 the CBOE delayed chain lists ``B1VDA…``/``B1SLA…``/
``B1SP…``/``B1IW…`` roots beside the standard rows, at the SAME (expiry,
right, strike), ahead of them, with a price unrelated to the stock
(AI-BEAR-002 AMENDMENT 2).

:func:`desk.shadows.package_prices` keys its quotes on ``(right, strike,
exp)``, which names no OCC root. Before the canonical
:func:`desk.store.standard_row_indexes` filter was applied there, it read
EVERY row of the recorded chain, with two consequences:

* **orphan** — a nonstandard row at a key with NO standard row was priced as
  that leg: its bid/ask reached ``realistic``/``mid`` and the modeled P&L
  (8 such rows exist in the recorded ``2026-11-05/IWM.json.gz``);
* **duplicate** — a nonstandard row beside its standard twin raised
  ``duplicate_contract_quote``, or ``quote_book`` when the junk row (read
  first) was ``0.00/0.00``: a refusal that was only incidental.

With the filter the orphan leg has no quote (``contract_quote_missing``),
the duplicate key holds the standard row alone (it prices exactly as a
standard-only document does), and the duplicate guard still refuses two
rows the canonical rule KEEPS at one key.
"""

from __future__ import annotations

import inspect
from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from tree_options.desk import shadows, store
from tree_options.desk.contracts import ContractError
from tree_options.desk.evidence import EvidenceStore
from tree_options.desk.sessions import cutoff_instant
from tree_options.desk.store import encode_document
from tree_options.trex.plan import LegStructure

EXP = date(2026, 11, 20)

# The plausible junk quote: the 11-13 NVDA B1 row's own prices (0.11/0.41),
# exactly the shape that corrupted a live QSL position.
B1_150 = "B1VDA261120P00150000"
STD_150 = "NVDA261120P00150000"
STD_145 = "NVDA261120P00145000"

# The pre-change package_prices result for the standard-only document below,
# taken from the code at 73d2a2f (before the filter). A byte-level pin: the
# fix must not move any honest mark.
PRE_CHANGE_STANDARD_ONLY = ("-1.24", "-1.37")
# The pre-change result for the ORPHAN document: the junk 150P bid (0.11)
# against the standard 145P ask (4.20) — the number that reached the book.
PRE_CHANGE_ORPHAN_JUNK_PRICED = ("-3.84", "-4.09")


def _spec() -> LegStructure:
    """A 150/145 put debit vertical: BUY the 150P, SELL the 145P."""
    return LegStructure(
        id="shadows-occ-1",
        underlying="NVDA",
        kind="debit_vertical",
        legs=[
            {"right": "P", "action": "BUY", "strike": "150", "expiry": EXP},
            {"right": "P", "action": "SELL", "strike": "145", "expiry": EXP},
        ],
        quantity=1,
        entry_date=date(2026, 11, 13),
        exit_deadline=date(2026, 11, 19),
        limit="1.00",
        exits={"touch": False, "breach": False},
    )


def _row(occ: Any, strike: float, bid: float, ask: float, oi: int) -> dict[str, Any]:
    return {
        "occ": occ,
        "exp": EXP.isoformat(),
        "strike": strike,
        "right": "P",
        "bid": bid,
        "ask": ask,
        "oi": oi,
    }


def _doc(rows: list[dict[str, Any]], underlying: str | None = "NVDA") -> dict[str, Any]:
    cols: dict[str, list[Any]] = {k: [r[k] for r in rows] for k in rows[0]}
    header: dict[str, Any] = {"schema": "desk-chain/1", "n": len(rows)}
    if underlying is not None:
        header["underlying"] = underlying
    return {"header": header, "columns": cols}


def _orphan() -> dict[str, Any]:
    """The BUY leg's key carries ONLY a junk row; the SELL leg is standard."""
    return _doc(
        [
            _row(B1_150, 150.0, 0.11, 0.41, 0),
            _row(STD_145, 145.0, 4.00, 4.20, 42115),
        ]
    )


def _standard_rows() -> list[dict[str, Any]]:
    return [
        _row(STD_150, 150.0, 2.83, 2.89, 42115),
        _row(STD_145, 145.0, 4.00, 4.20, 42115),
    ]


def _standard_only() -> dict[str, Any]:
    return _doc(_standard_rows())


def _priced(doc: dict[str, Any]) -> tuple[str, str]:
    mid, realistic = shadows.package_prices(_spec(), doc)
    return str(mid), str(realistic)


def _without_occ(doc: dict[str, Any]) -> dict[str, Any]:
    doc["columns"].pop("occ")
    return doc


# ----------------------------------------------------------------- the orphan


def test_an_orphan_nonstandard_row_is_refused_not_priced() -> None:
    """The orphan junk row never reaches ``realistic``/``mid``: the leg has
    no standard quote, so the package is refused as ``contract_quote_missing``
    — the existing refusal, not a new one."""
    with pytest.raises(ContractError) as exc:
        shadows.package_prices(_spec(), _orphan())
    assert str(exc.value) == "contract_quote_missing"


def test_the_orphan_refusal_is_the_occ_filters() -> None:
    """The control: the SAME rows with nothing to test (no occ column) are
    served unfiltered and price the junk quote — the pre-change number. So
    the refusal above comes from the OCC filter and nothing else."""
    assert _priced(_without_occ(_orphan())) == PRE_CHANGE_ORPHAN_JUNK_PRICED


@pytest.mark.parametrize("b1_root", ["B1VDA", "B1SLA", "B1SP", "B1IW", "B1IWM"])
def test_every_recorded_nonstandard_root_is_refused(b1_root: str) -> None:
    doc = _orphan()
    doc["columns"]["occ"][0] = f"{b1_root}261120P00150000"
    with pytest.raises(ContractError) as exc:
        shadows.package_prices(_spec(), doc)
    assert str(exc.value) == "contract_quote_missing"


# -------------------------------------------------------------- the duplicate


@pytest.mark.parametrize("junk_first", [True, False], ids=["junk-first", "junk-last"])
def test_a_nonstandard_twin_is_dropped_and_the_standard_row_prices(junk_first: bool) -> None:
    """A junk row beside its standard twin: the filter drops the junk row,
    the key holds ONE row, and the package prices exactly as the
    standard-only document does (the junk quote's 0.11/0.41 is nowhere).

    Before the filter this shape raised ``duplicate_contract_quote`` (or
    ``quote_book`` when the junk row read first was 0.00/0.00): with the
    canonical rule applied there is no duplicate left at the key."""
    junk = _row(B1_150, 150.0, 0.11, 0.41, 0)
    standard = _standard_rows()
    doc = _doc([junk, *standard] if junk_first else [*standard, junk])
    assert _priced(doc) == _priced(_standard_only()) == PRE_CHANGE_STANDARD_ONLY


@pytest.mark.parametrize(
    "twin_occ",
    [STD_150, None],
    ids=["two-standard-rows", "non-string-occ-kept-by-the-canonical-rule"],
)
def test_two_kept_rows_at_one_key_still_fail_closed(twin_occ: Any) -> None:
    """The duplicate guard is untouched by the filter: two rows the canonical
    rule KEEPS at one key (a repeated standard contract, or a row whose occ
    is not a string — nothing to test, kept) still raise
    ``duplicate_contract_quote``; no row is silently preferred."""
    doc = _doc(
        [
            _row(twin_occ, 150.0, 2.70, 2.95, 7),
            _row(STD_150, 150.0, 2.83, 2.89, 42115),
            _row(STD_145, 145.0, 4.00, 4.20, 42115),
        ]
    )
    with pytest.raises(ContractError) as exc:
        shadows.package_prices(_spec(), doc)
    assert str(exc.value) == "duplicate_contract_quote"


# --------------------------------------------------------- the honest marks


def test_a_standard_only_document_prices_exactly_as_before() -> None:
    """Byte-level: the filtered path's result for a standard-only document is
    the pre-change result, and the same as the unfiltered (no occ column)
    path's — the fix moves no honest mark."""
    assert _priced(_standard_only()) == PRE_CHANGE_STANDARD_ONLY
    assert _priced(_without_occ(_standard_only())) == PRE_CHANGE_STANDARD_ONLY


# ------------------------------------------------------- the fail-open control


def test_a_document_without_an_underlying_is_served_as_before() -> None:
    """The canonical rule's fail-open branch: a document that names no
    underlying cannot be tested, so every row is served exactly as before —
    the orphan junk row included (the pre-change number). The shadow lane
    never reaches this branch: ``_read_mark`` refuses a chain that does not
    name the episode's underlying (``chain_identity``)."""
    doc = _orphan()
    doc["header"].pop("underlying")
    assert store.standard_row_indexes(doc) == [0, 1]
    assert _priced(doc) == PRE_CHANGE_ORPHAN_JUNK_PRICED
    honest = _standard_only()
    honest["header"].pop("underlying")
    assert _priced(honest) == PRE_CHANGE_STANDARD_ONLY


def test_a_document_without_an_occ_column_is_served_as_before() -> None:
    """No occ column, nothing to test: every row is served as before. (The
    canonical ``standard_row_indexes`` enumerates the occ column, so it has
    no rows to return here; ``package_prices`` serves the document whole.)"""
    assert _priced(_without_occ(_standard_only())) == PRE_CHANGE_STANDARD_ONLY
    assert _priced(_without_occ(_orphan())) == PRE_CHANGE_ORPHAN_JUNK_PRICED


def test_the_filter_is_the_canonical_rule() -> None:
    """No competing implementation: the shadow lane calls the store's own
    ``standard_row_indexes``."""
    assert shadows.standard_row_indexes is store.standard_row_indexes
    assert "standard_row_indexes(doc)" in inspect.getsource(shadows.package_prices)


# ----------------------------------------------------------- the lane itself


def _run(world, as_of):
    return shadows.update_shadows(
        session=as_of,
        now=cutoff_instant(as_of),
        cal=world.cal,
        database=world.db,
        store_root=world.store,
        queue_dir=world.queues,
    )


def _occ_chain(world, session, occ: list[str]) -> None:
    """The fixture chain with an occ column added (rewritten in place)."""
    path, doc = world.chain(session)
    doc["columns"]["occ"] = occ
    path.write_bytes(encode_document(doc))


def _occ(root: str, world, right: str, strike: int) -> str:
    return f"{root}{world.expiry:%y%m%d}{right}{strike * 1000:08d}"


def test_the_shadow_lane_censors_a_leg_whose_only_row_is_nonstandard(world) -> None:
    """End to end: the SELL 105C leg's only row is a nonstandard root, so
    no mark is written and the refusal is recorded as a quality event."""
    world.queue_file()
    for d in (world.entry, world.deadline):
        _occ_chain(world, d, [_occ("AAPL", world, "C", 100), _occ("B1APL", world, "C", 105)])
    result = _run(world, world.deadline)
    assert result["marks_created"] == 0
    with EvidenceStore(world.db, readonly=True) as ev:
        assert ev.all("mark") == []
        # the sessions between entry and deadline have no chain: chain_missing
        codes = {q["session"]: q["code"] for q in ev.all("quality") if q["code"] != "chain_missing"}
    assert codes == {
        world.entry.isoformat(): "contract_quote_missing",
        world.deadline.isoformat(): "contract_quote_missing",
    }


def test_the_shadow_lane_marks_standard_rows_as_before(world) -> None:
    """End to end, the control: the same chain with standard roots marks
    exactly as the occ-less fixture chain does (test_shadows: -10.00 gross
    at the deadline)."""
    world.queue_file()
    for d in (world.entry, world.deadline):
        _occ_chain(world, d, [_occ("AAPL", world, "C", 100), _occ("AAPL", world, "C", 105)])
    result = _run(world, world.deadline)
    assert result["marks_created"] == 2
    with EvidenceStore(world.db, readonly=True) as ev:
        episode = ev.all("episode")[0]
        terminal = next(m for m in ev.all("mark") if m["session"] == world.deadline.isoformat())
    assert Decimal(terminal["modeled_gross_pnl_dollars"]) == Decimal("-10.00")
    # the occ-less fixture chain (served unfiltered) prices the same bytes
    _, plain = world.chain(world.deadline)
    spec = LegStructure.model_validate(episode["deal"]["structure"])
    mid, realistic = shadows.package_prices(spec, plain)
    assert (terminal["mid"], terminal["realistic"]) == (str(mid), str(realistic))
