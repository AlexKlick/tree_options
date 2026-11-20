"""The QSL lane's OCC integrity: nonstandard roots never reach the surface.

Since 2026-11-05 the CBOE delayed chain lists ``B1VDA…``/``B1SLA…``/
``B1SP…``/``B1IW…`` roots beside the standard rows, at the SAME (expiry,
right, strike), ahead of them. They carry a price unrelated to the stock and
almost no open interest.

Both of the QSL lane's indexes read the same chain document but key their
rows differently:

* :func:`desk.surface.chain_quotes` keys by ROW, so it kept BOTH rows and
  the picker took whichever came first — the B1 one;
* :func:`desk.qsl._sizes` keys by ``(right, strike, expiry)``, which names
  no OCC root, so the B1 row landed on the standard row's key.

The blend that produced was therefore: the B1 row's bid/ask (and the IV and
delta the surface solved from the B1 mid) paired with the standard row's
``oi`` and touch sizes — a contract that does not exist. The recorded
instance is the desk's own 2026-11-13 NVDA put-credit episode
``d-20261113-qsl-put-credit-NVDA-98470619e0ec``: bid 0.11 / ask 0.41 (B1's
prices) with open interest 42115 (the standard row's), which then cleared
the OI and size floors.

The filter is :func:`desk.store.standard_occ`, applied at the row level by
:func:`desk.store.standard_row_indexes`. These tests pin the three
properties that matter:

1. the B1 row appears in NEITHER index;
2. the open-interest floor can never be satisfied by a nonstandard row;
3. both indexes apply the filter — they read the SAME row list, so a
   quote and the sizes entry behind it always come from one physical row.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest

from tree_options.desk import qsl, store, surface
from tree_options.synth_options.greeks import bs_price

SESSION = date(2026, 11, 13)
SPOT = 100.0
RATE = 0.04
IV = 0.32
EXPIRY = "2026-11-20"
DTE = (date.fromisoformat(EXPIRY) - SESSION).days

# The two rows of the recorded 2026-11-13 NVDA chain that share
# (2026-11-20, P, 150.0): the B1 root FIRST, the standard row second.
B1_OCC = "B1VDA261120P00150000"
STD_OCC = "NVDA261120P00150000"
STD_KEY = ("P", Decimal("150.0"), EXPIRY)

COLUMNS = (
    "occ",
    "exp",
    "strike",
    "right",
    "bid",
    "bid_size",
    "ask",
    "ask_size",
    "oi",
    "volume",
    "iv",
    "delta",
)


def _bs_mid(strike: float, right: str) -> float:
    """The Black-Scholes mid at the test's IV — a price the surface can solve."""
    return bs_price(
        spot=SPOT,
        strike=strike,
        dte_calendar_days=DTE,
        iv=IV,
        risk_free=RATE,
        dividend_yield=0.0,
        call_put=right,
    )


def _row(
    occ: str,
    *,
    right: str = "P",
    strike: float = 150.0,
    bid: float,
    ask: float,
    bid_size: int = 25,
    ask_size: int = 25,
    oi: int,
) -> dict[str, Any]:
    return {
        "occ": occ,
        "exp": EXPIRY,
        "strike": strike,
        "right": right,
        "bid": bid,
        "bid_size": bid_size,
        "ask": ask,
        "ask_size": ask_size,
        "oi": oi,
        "volume": 100,
        "iv": IV * 100,
        "delta": 0.0,
    }


def _doc(rows: list[dict[str, Any]], underlying: str = "NVDA") -> dict[str, Any]:
    """A minimal desk-chain/1 document: the given rows, in the given order."""
    cols: dict[str, list[Any]] = {k: [] for k in COLUMNS}
    for r in rows:
        for k in COLUMNS:
            cols[k].append(r.get(k))
    return {
        "header": {
            "schema": "desk-chain/1",
            "session": SESSION.isoformat(),
            "underlying": underlying,
            "source_as_of": "2026-11-13T17:46:00-04:00",
            "fetched_at": "2026-11-13T17:46:00-04:00",
            "raw_sha256": "e" * 64,
            "underlying_quote": {"close": SPOT},
            "n": len(cols["occ"]),
        },
        "columns": cols,
    }


def _duplicated_chain() -> dict[str, Any]:
    """The recorded shape: the B1 row first, its standard twin second.

    The two rows sit at the recorded strike (150.0), so the picker's
    geometry is the recorded one; the STANDARD row's price is the
    Black-Scholes mid, which the surface can invert into an IV and a delta.
    """
    mid = _bs_mid(150.0, "P")
    return _doc(
        [
            _row(B1_OCC, bid=0.11, ask=0.41, bid_size=0, ask_size=0, oi=0),
            _row(STD_OCC, bid=round(mid - 0.03, 2), ask=round(mid + 0.03, 2), oi=42115),
        ]
    )


def _near_the_money_chain() -> dict[str, Any]:
    """A duplicated key whose standard row is solvable (so a picker runs).

    Strike 100 on a 100 spot: the delta solves, the picker has something to
    pick, and the B1 twin sits ahead of it exactly as the recorded chain
    does."""
    mid = _bs_mid(100.0, "C")
    return _doc(
        [
            _row("B1VDA261120C00100000", right="C", strike=100.0, bid=0.21, ask=0.51, oi=0),
            _row(
                "NVDA261120C00100000",
                right="C",
                strike=100.0,
                bid=round(mid - 0.03, 2),
                ask=round(mid + 0.03, 2),
                oi=42115,
            ),
        ]
    )


def _quotes(doc: dict[str, Any]) -> list[surface.Quote]:
    return surface.chain_quotes(doc, session=SESSION, spot=SPOT, rate=RATE)


# ---------------------------------------------------------------- (a) the row


def test_the_nonstandard_row_is_absent_from_chain_quotes() -> None:
    """(a) The B1 quote never appears in the surface index."""
    doc = _duplicated_chain()
    quotes = _quotes(doc)
    assert len(quotes) == 1, "only the standard row may be quoted"
    q = quotes[0]
    assert q.oi == 42115
    # the B1 row's own prices are nowhere in the surface
    assert all(not (q.bid == 0.11 and q.ask == 0.41) for q in quotes)


def test_the_nonstandard_row_is_absent_from_sizes() -> None:
    """(a) The B1 row never appears in the sizes index."""
    # A chain of ONE nonstandard row: the sizes index must be EMPTY. A
    # nonstandard contract has no place in the lane's sizes map, and the
    # key names no root, so an unfiltered map would serve it.
    only_b1 = _doc([_row(B1_OCC, bid=0.11, ask=0.41, oi=0)])
    assert qsl._sizes(only_b1) == {}

    # And in the recorded duplicate, the key carries the STANDARD row's
    # sizes and open interest.
    doc = _duplicated_chain()
    sizes = qsl._sizes(doc)
    assert len(sizes) == 1
    # the key carries the STANDARD row's sizes and open interest
    assert sizes[STD_KEY] == (25, 25, 42115)


def test_the_nonstandard_row_cannot_be_reached_through_the_pickers() -> None:
    """(a) The pickers cannot hand a B1 quote to the gate."""
    doc = _near_the_money_chain()
    quotes = _quotes(doc)
    expiry = qsl.pick_expiry(quotes)
    assert expiry == date(2026, 11, 20)
    short = qsl.pick_short(quotes, expiry, "C")
    assert short is not None
    assert (short.bid, short.ask) != (0.21, 0.51)
    assert short.oi == 42115
    long = qsl.pick_long(quotes, expiry, "C", short.strike)
    if long is not None:
        assert (long.bid, long.ask) != (0.21, 0.51)


def test_the_nonstandard_row_is_dropped_before_it_can_be_picked() -> None:
    """(a) The filter runs on the ROW, before any picker sees a quote."""
    doc = _duplicated_chain()
    kept = store.standard_row_indexes(doc)
    assert kept == [1], "only the standard row survives the filter"
    assert doc["columns"]["occ"][kept[0]] == STD_OCC


# ------------------------------------------------- (b) the open-interest floor


def test_the_open_interest_floor_cannot_be_satisfied_by_a_nonstandard_row() -> None:
    """(b) A nonstandard row's open interest can never clear the OI floor.

    The B1 row alone carries open interest 9000 (over the floor) and a
    one-cent spread; without the standard row there is nothing to blend,
    so no leg can be built out of it."""
    doc = _doc([_row(B1_OCC, bid=0.50, ask=0.51, bid_size=500, ask_size=500, oi=9000)])
    assert doc["columns"]["oi"][0] >= qsl.OI_FLOOR
    # the row is gone from both indexes...
    quotes = _quotes(doc)
    assert quotes == []
    assert qsl._sizes(doc) == {}
    # ...so no leg can be quoted and no gate can be passed on it.
    assert qsl.pick_short(quotes, date(2026, 11, 20), "P") is None


def test_a_nonstandard_row_cannot_supply_open_interest_to_a_standard_leg() -> None:
    """(b) The sizes index never carries a nonstandard row's open interest.

    The B1 row is written LAST here, so a sizes map that keyed on
    (expiry, right, strike) without filtering would overwrite the
    standard row's 42115 with the B1 row's 0."""
    doc = _doc(
        [
            _row(STD_OCC, bid=2.83, ask=2.89, bid_size=13, ask_size=55, oi=42115),
            _row(B1_OCC, bid=0.11, ask=0.41, bid_size=0, ask_size=0, oi=0),
        ]
    )
    assert qsl._sizes(doc)[STD_KEY] == (13, 55, 42115)
    assert qsl._sizes(doc)[STD_KEY][2] >= qsl.OI_FLOOR


def test_the_gate_would_have_passed_on_the_blended_leg() -> None:
    """(b) The blend the filter forbids really did clear the gate.

    The recorded 11-13 NVDA leg: the B1 row's prices with the standard
    row's open interest. Built by hand here (the prices come from the B1
    row, the sizes and the open interest from the standard one), the leg
    passes every SPEC 2 clause — which is why the corrupt episode reached
    the desk's queue at all."""
    b1 = _row(B1_OCC, bid=0.11, ask=0.41, bid_size=0, ask_size=0, oi=0)
    std = _row(STD_OCC, bid=4.45, ask=4.35, bid_size=25, ask_size=25, oi=19599)
    std_long = _row(STD_OCC, bid=2.83, ask=2.89, bid_size=13, ask_size=55, oi=42115)

    def leg(row: dict[str, Any], size_row: dict[str, Any]) -> qsl.LegQuote:
        return qsl.LegQuote(
            right=row["right"],
            strike=Decimal(str(row["strike"])),
            expiry=date.fromisoformat(row["exp"]),
            dte=DTE,
            bid=Decimal(str(row["bid"])),
            ask=Decimal(str(row["ask"])),
            mid=Decimal(str((row["bid"] + row["ask"]) / 2)),
            bid_size=size_row["bid_size"],
            ask_size=size_row["ask_size"],
            oi=size_row["oi"],
            delta=None,
            iv=None,
        )

    blended = leg(b1, std_long)
    assert blended.oi >= qsl.OI_FLOOR, "the corrupt leg's open interest cleared the floor"
    assert blended.bid_size >= qsl.SIZE_FLOOR, "the corrupt leg's touch size cleared the floor"
    # and the filter now keeps that open interest on the standard row only
    doc = _doc([b1, std, std_long])
    assert qsl._sizes(doc)[STD_KEY] == (13, 55, 42115)


# ------------------------------------------------ (c) the filter on BOTH indexes


def test_the_filter_is_applied_on_both_indexes() -> None:
    """(c) Both indexes read the SAME row list, so they cannot disagree.

    This is the structural property: the surface index and the sizes index
    are built from one row selection, so a quote and the open interest
    behind it always come from one physical row of the chain."""
    doc = _duplicated_chain()
    kept = store.standard_row_indexes(doc)
    assert kept == [1]

    # the surface index holds exactly the kept rows...
    quotes = _quotes(doc)
    assert len(quotes) == len(kept)
    # ...and the sizes index holds exactly the kept rows too.
    sizes = qsl._sizes(doc)
    assert len(sizes) == len(kept)

    # no key of the sizes index names a row the surface index does not carry
    for (right, strike, exp), _ in sizes.items():
        matches = [
            q
            for q in quotes
            if q.right == right and q.strike == float(strike) and q.expiry.isoformat() == exp
        ]
        assert matches, f"the sizes index has a key the surface index lacks: {right} {strike} {exp}"


def test_the_two_indexes_agree_row_for_row() -> None:
    """(c) The quote and the open interest behind it are the same contract."""
    doc = _duplicated_chain()
    quotes = _quotes(doc)
    sizes = qsl._sizes(doc)
    for q in quotes:
        key = (q.right, Decimal(str(q.strike)), q.expiry.isoformat())
        assert key in sizes
        assert sizes[key][2] == q.oi, "price and open interest come from different rows"


def test_a_document_without_an_underlying_serves_every_row() -> None:
    """The fail-open case, matching the web view: nothing to test, no filter.

    :func:`desk.pricing.EntrySurface` hands the surface a header-less
    sub-document, and the web view treats a missing underlying as
    "nothing to filter". The filter must not change those documents."""
    doc = _doc([_row(B1_OCC, bid=0.11, ask=0.41, oi=0)])
    doc["header"].pop("underlying")
    assert store.standard_row_indexes(doc) == [0]
    assert len(_quotes(doc)) == 1
    assert len(qsl._sizes(doc)) == 1


def test_a_row_whose_occ_is_not_a_string_is_kept() -> None:
    """Nothing to test, nothing to drop — the fail-closed-on-nothing rule."""
    doc = _duplicated_chain()
    doc["columns"]["occ"][0] = None
    assert store.standard_row_indexes(doc) == [0, 1]


def test_the_filter_reuses_the_canonical_standard_occ() -> None:
    """The filter IS ``store.standard_occ`` — no competing implementation."""
    import inspect

    src = inspect.getsource(store.standard_row_indexes)
    assert "standard_occ(underlying)" in src
    # and the canonical pattern still accepts what the real chains list
    std = store.standard_occ("NVDA")
    assert std.fullmatch(STD_OCC)
    assert std.fullmatch(B1_OCC) is None


@pytest.mark.parametrize(
    ("underlying", "b1_root"),
    [
        ("NVDA", "B1VDA"),
        ("TSLA", "B1SLA"),
        ("SPY", "B1SP"),
        ("IWM", "B1IW"),
    ],
)
def test_every_affected_name_is_filtered(underlying: str, b1_root: str) -> None:
    """The four names whose chains carry a nonstandard root."""
    occ = f"{b1_root}261120P00150000"
    std = f"{underlying}261120P00150000"
    doc = _doc(
        [
            _row(occ, bid=0.11, ask=0.41, oi=0),
            _row(std, bid=2.83, ask=2.89, oi=42115),
        ],
        underlying=underlying,
    )
    assert store.standard_row_indexes(doc) == [1]
    assert len(_quotes(doc)) == 1


def test_the_expiry_of_a_duplicated_row_is_the_same_for_both() -> None:
    """The duplicate really is a duplicate: same (expiry, right, strike).

    This is the fact the defect rests on — the B1 row and the standard row
    name the same contract, differing only in the root, so a key that omits
    the root cannot tell them apart."""
    doc = _duplicated_chain()
    cols = doc["columns"]
    keys = {(cols["exp"][i], cols["right"][i], float(cols["strike"][i])) for i in (0, 1)}
    assert len(keys) == 1, "the two rows must share one (expiry, right, strike)"


def test_the_expiry_is_inside_the_qsl_window() -> None:
    """The duplicated expiry is one the QSL lane actually trades."""
    dte = (date.fromisoformat(EXPIRY) - SESSION).days
    assert qsl.DTE_MIN <= dte <= qsl.DTE_MAX
    assert timedelta(days=dte) == date.fromisoformat(EXPIRY) - SESSION
