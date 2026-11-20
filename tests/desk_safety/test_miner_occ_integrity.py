"""The MINER lane's OCC integrity: a nonstandard root can be neither
selected nor priced.

The QSL lane was fixed first (commit ``2c468c8``): ``surface.chain_quotes``
and ``qsl._sizes`` both iterate ``store.standard_row_indexes(doc)``, so the
two indexes can no longer disagree about one contract.

The MINER lane was still exposed.  ``desk.pricing.EntrySurface.quotes``
built a HEADER-LESS sub-document, so the canonical filter had nothing to
test and its fail-open rule served EVERY row, the nonstandard ``B1VDA...``
roots included.  The miner then

* ENUMERATED structures from the B1 quote (its own mid, its own IV, its own
  |delta|), and
* PRICED every leg from the B1 row: ``EntrySurface.leg_quote`` takes the
  FIRST quote at a (right, strike) and CBOE lists the B1 row first, so a leg
  whose strike also exists as a B1 row was priced from the B1 prices even
  where the enumeration had picked the standard row; and
* carried the B1 row's OPEN INTEREST into the rails' ``leg_open_interest``
  floor, which the OI floor alone cannot catch (some B1 rows carry OI).

These tests pin the three properties that matter:

1. the B1 row appears in NEITHER the enumeration nor the priced leg;
2. a nonstandard row's open interest can never satisfy the OI floor;
3. the PRICING path applies the canonical filter too, not just the QSL one.
"""

from __future__ import annotations

import copy
import dataclasses
import inspect
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import numpy as np
import pytest
from tests.fixtures import desk_miner as wm

from tree_options.desk import distribution as dist
from tree_options.desk import miner, playbook, pricing, rails, selection, store, surface
from tree_options.synth_options.greeks import bs_price
from tree_options.trex.plan import ExitRules, Leg, LegStructure

REPO = wm.REPO
SESSION = date(2026, 11, 13)
ENTRY = date(2026, 11, 16)
EXIT_SESSION = date(2026, 11, 18)
EXPIRY = date(2026, 11, 20)
SPOT = 100.0
RATE = 0.04
IV = 0.32
STRIKE = 100.0
D = Decimal

# The two rows that share (2026-11-20, P, 100): the B1 root FIRST, the
# standard row second - the order the CBOE delayed chain lists them in.
B1_OCC = "B1VDA261120P00100000"
STD_OCC = "NVDA261120P00100000"

COLUMNS = ("occ", "exp", "strike", "right", "bid", "ask", "oi", "volume", "iv", "delta")


# ------------------------------------------------------------------ fixtures


@pytest.fixture(scope="module")
def cal():
    return wm.calendar()


@pytest.fixture(scope="module")
def pb() -> playbook.Playbook:
    return playbook.load_playbook(REPO / "data" / "desk" / "playbook" / "v2.toml")


@pytest.fixture(scope="module")
def open_cfg() -> selection.MinerConfig:
    """Thresholds nobody would trade on: the queue mechanics are under test,
    not the proposed thresholds."""
    cfg = dataclasses.replace(
        selection.load_config(REPO / "data" / "desk" / "miner" / "v1.toml"), n_paths=1000
    )
    return dataclasses.replace(
        cfg,
        selection=selection.Selection(
            min_stress_fill_ev_usd=D("-100000"), min_ev_per_max_loss=D("-100")
        ),
    )


# ------------------------------------------------------------------- helpers


def _mid() -> float:
    """The standard row's mid: the pinned Black-Scholes price at IV."""
    return bs_price(
        spot=SPOT,
        strike=STRIKE,
        dte_calendar_days=(EXPIRY - SESSION).days,
        iv=IV,
        risk_free=RATE,
        dividend_yield=0.0,
        call_put="P",
    )


def _row(occ: str, *, bid: float, ask: float, oi: int) -> dict[str, Any]:
    return {
        "occ": occ,
        "exp": EXPIRY.isoformat(),
        "strike": STRIKE,
        "right": "P",
        "bid": bid,
        "ask": ask,
        "oi": oi,
        "volume": 100,
        "iv": IV * 100,
        "delta": 0.0,
    }


def _doc(rows: list[dict[str, Any]], underlying: str = "NVDA") -> dict[str, Any]:
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


def _duplicated_chain(*, b1_oi: int = 0, std_oi: int = 42115) -> dict[str, Any]:
    """The B1 row FIRST, the standard row second: the same (expiry, right,
    strike), the order the CBOE delayed chain lists them in."""
    mid = _mid()
    return _doc(
        [
            _row(B1_OCC, bid=0.11, ask=0.41, oi=b1_oi),
            _row(STD_OCC, bid=mid - 0.03, ask=mid + 0.03, oi=std_oi),
        ]
    )


def _surface(doc: dict[str, Any], underlying: str = "NVDA") -> pricing.EntrySurface:
    return pricing.EntrySurface(doc, session=SESSION, rate=RATE, underlying=underlying)


def _fixture_surface(doc: dict[str, Any]) -> pricing.EntrySurface:
    """A surface over a desk_miner-fixture chain: that world's own session,
    rate and underlying."""
    return pricing.EntrySurface(
        doc, session=wm.D, rate=wm.RATE_PCT / 100.0, underlying=str(doc["header"]["underlying"])
    )


def _with_b1_rows(doc: dict[str, Any]) -> dict[str, Any]:
    """Duplicate every twentieth row with a nonstandard B1 root: the same
    (expiry, right, strike), a junk price and a huge open interest.  The
    duplicate is listed AHEAD of the standard row, the order the CBOE
    delayed chain lists them in."""
    cols = doc["columns"]
    n = len(cols["occ"])
    out: dict[str, list[Any]] = {k: [] for k in cols}
    for i in range(n):
        if i % 20 == 0:
            for k in cols:
                v = cols[k][i]
                if k == "occ":
                    out[k].append("B1" + str(v)[1:])
                elif k == "bid":
                    out[k].append(0.01)
                elif k == "ask":
                    out[k].append(0.05)
                elif k == "oi":
                    out[k].append(999_999)
                else:
                    out[k].append(v)
        for k in cols:
            out[k].append(cols[k][i])
    doc["columns"] = out
    return doc


def _shape(protos: list[miner.Proto]) -> list[Any]:
    return [(p.key, [(q.bid, q.ask, q.oi, q.delta) for q in p.quotes]) for p in protos]


def _struct() -> LegStructure:
    return LegStructure(
        id="s",
        underlying="NVDA",
        kind="long_single",  # type: ignore[arg-type]
        legs=(
            Leg(  # type: ignore[arg-type]
                right="P", action="BUY", strike=D("100.0"), expiry=EXPIRY
            ),
        ),
        quantity=1,
        entry_date=ENTRY,
        exit_deadline=EXIT_SESSION,
        limit=D("1.80"),
        exits=ExitRules(touch=False, breach=False),
    )


def _paths(cal) -> dist.PathSet:
    pool = dist.prepare_pool(np.random.default_rng(7).normal(size=400))
    return dist.simulate_paths(
        name="NVDA",
        session=SESSION,
        exit_session=EXIT_SESSION,
        cal=cal,
        row_id="occ",
        z_pool=pool,
        daily_var=1e-4,
        rate=RATE,
        n_paths=400,
    )


def _limits() -> rails.RailLimits:
    return rails.RailLimits(
        max_loss_per_trade_usd=D("500"),
        max_book_loss_usd=D("5000"),
        max_per_underlying=2,
        max_net_beta_delta_usd_per_1pct_spy=D("250"),
        max_admissions_per_session=3,
        max_roundtrip_cost_frac_of_max_loss=D("0.15"),
        min_leg_open_interest=100,
        max_leg_spread_frac_of_mid=D("0.10"),
        min_long_single_abs_delta=D("0.30"),
        max_chain_age_sessions=1,
        max_book_short_vega_usd_per_volpt=D("100"),
    )


def _candidate(oi: int | None) -> rails.Candidate:
    return rails.Candidate(
        id="deal-nvda-1",
        underlying="NVDA",
        kind="long_single",  # type: ignore[arg-type]
        legs=(
            rails.CandidateLeg(
                right="P",
                action="BUY",
                strike=D("100.0"),
                expiry=EXPIRY,
                bid=D("1.70"),
                ask=D("1.76"),
                open_interest=oi,
                delta=D("-0.5"),
            ),
        ),
        quantity=1,
        max_loss_usd=D("180"),
        entry_price=D("1.80"),
        planned_exit=EXIT_SESSION,
        risk=rails.PositionRisk(
            delta_shares=D("-50"), vega_usd_per_volpt=D("1"), spot=D("100"), beta=D("1.5")
        ),
        chain_session=SESSION,
        chain_as_of=datetime(2026, 11, 13, 17, 46, tzinfo=wm.ET),
        earnings=(),
        ex_dividends=(),
        news_veto=None,
    )


def _status(report: rails.RailReport, rule: str) -> str:
    [res] = [r for r in report.results if r.rule == rule]
    return res.status


# --------------------------------------------------------------------- tests


def test_the_nonstandard_row_is_absent_from_the_miners_quotes() -> None:
    """(a) The surface serves only the standard contract."""
    doc = _duplicated_chain()
    assert store.standard_row_indexes(doc) == [1], "the canonical filter changed"
    quotes = _surface(doc).quotes(EXPIRY)
    assert len(quotes) == 1, "the B1 row reached the miner's surface"
    q = quotes[0]
    assert q.bid == pytest.approx(_mid() - 0.03)
    assert q.ask == pytest.approx(_mid() + 0.03)
    assert q.oi == 42115


def test_the_nonstandard_row_can_neither_be_enumerated_nor_priced(cal, pb) -> None:
    """(b) Neither the enumeration nor the priced leg can be the B1 row."""
    doc = _duplicated_chain()
    surf = _surface(doc)
    leg = Leg(right="P", action="BUY", strike=D("100.0"), expiry=EXPIRY)  # type: ignore[arg-type]
    q = surf.leg_quote(leg)
    assert q.bid == pytest.approx(_mid() - 0.03), "the leg was priced off the B1 row"
    assert q.ask == pytest.approx(_mid() + 0.03)
    # the package mid the miner records is the STANDARD row's, not the B1's
    val = pricing.price_structure(_struct(), _paths(cal), surf, mr=pricing.IvMeanReversion.off())
    assert float(val.entry_mid) == pytest.approx(_mid(), abs=1e-6)
    assert float(val.entry_mid) != pytest.approx(0.26, abs=1e-6), "the B1 mid was priced"

    # the enumeration of a real playbook row is unchanged by the B1 rows
    plain = wm.chain("AAPL", wm.D, "2025-03-13T03:49:00+00:00")
    poisoned = _with_b1_rows(copy.deepcopy(plain))
    row = next(r for r in pb.rows if r.id == "R1")
    a = miner.enumerate_row(row, "AAPL", _fixture_surface(plain), wm.ENTRY)
    b = miner.enumerate_row(row, "AAPL", _fixture_surface(poisoned), wm.ENTRY)
    assert a, "the fixture enumerated nothing"
    assert _shape(a) == _shape(b), "a nonstandard row changed the enumeration"


def test_a_nonstandard_row_cannot_satisfy_the_open_interest_floor(static_calendar) -> None:
    """(c) The B1 row's open interest can never clear the rails' OI floor."""
    doc = _duplicated_chain(b1_oi=999_999, std_oi=0)
    q = _surface(doc).quotes(EXPIRY)[0]
    assert q.oi == 0, "the B1 row's open interest reached the miner"
    ctx = rails.RailContext(
        now=datetime(2026, 11, 13, 18, 0, tzinfo=wm.ET),
        session=SESSION,
        calendar=static_calendar,
        account_mode="paper",
        admissions_this_session=0,
    )
    book = rails.BookView(positions=())
    report = rails.check(_candidate(q.oi), book, ctx, _limits())
    assert _status(report, "leg_open_interest") == rails.FAIL


def test_the_pricing_path_applies_the_canonical_filter_too() -> None:
    """(d) The PRICING path is filtered, not only the QSL one.

    The miner reaches the chain twice: through ``miner._Ctx.surface`` (the
    enumeration) and through ``pricing.value_candidates`` (the pricing).
    Both must name the underlying, or the canonical filter has nothing to
    test and fails open.
    """
    assert "underlying=name" in inspect.getsource(pricing.value_candidates)
    assert "underlying=name" in inspect.getsource(miner._Ctx.surface)
    # and the sub-document the surface hands the chain reader names it too
    src = inspect.getsource(pricing.EntrySurface.quotes)
    assert '"underlying": self.underlying' in src
    # the reader itself applies the canonical filter
    assert "standard_row_indexes(doc)" in inspect.getsource(surface.chain_quotes)


def test_the_surface_needs_an_underlying() -> None:
    """(e) No underlying, no surface: the fail-open path is unreachable."""
    doc = _duplicated_chain()
    with pytest.raises(TypeError):
        pricing.EntrySurface(doc, session=SESSION, rate=RATE)  # type: ignore[call-arg]
    # an EMPTY underlying must be refused even when the chain itself names
    # no underlying — there is nothing to test, so the constructor may not
    # fail open and serve the B1 rows
    bare = _duplicated_chain()
    bare["header"].pop("underlying")
    with pytest.raises(pricing.NotEvaluable):
        pricing.EntrySurface(bare, session=SESSION, rate=RATE, underlying="")
    with pytest.raises(pricing.NotEvaluable):
        pricing.EntrySurface(doc, session=SESSION, rate=RATE, underlying="")
    # a chain that names another underlying is refused, not served
    with pytest.raises(pricing.NotEvaluable):
        pricing.EntrySurface(doc, session=SESSION, rate=RATE, underlying="TSLA")


def _run(w, tmp_path, cal, cfg) -> miner.MineResult:
    where = wm.materialize(w, tmp_path / "world")
    mp = pytest.MonkeyPatch()
    try:
        wm.env(mp, where)
        return miner.run_mine(session=wm.D, now=wm.NOW, cal=cal, config=cfg, dry_run=True)
    finally:
        mp.undo()


def test_a_nonstandard_row_cannot_change_the_miners_queue(cal, open_cfg, tmp_path) -> None:
    """(f) End to end: the produced queue is the same with and without the
    nonstandard rows.  Before the fix the B1 rows moved prices, deltas and
    open interests through the whole lane."""
    plain = _run(wm.base(cal), tmp_path / "plain", cal, open_cfg)
    world = wm.base(cal)
    world.chains = [
        (_with_b1_rows(doc) if doc["header"]["underlying"] in ("AAPL", "QQQ") else doc, c)
        for doc, c in world.chains
    ]
    poisoned = _run(world, tmp_path / "poison", cal, open_cfg)
    assert plain.exit_code == 0 and poisoned.exit_code == 0
    assert plain.payload is not None and poisoned.payload is not None
    assert plain.payload["admissible"], "the fixture queued nothing"
    assert miner.encode(plain.payload) == miner.encode(poisoned.payload)
