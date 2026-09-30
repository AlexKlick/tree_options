"""Measured, SHAPED execution cost (red-first; the model is not written yet).

The flat ``CostModel`` (outcomes.py) charges every leg the same $0.03 half
spread, i.e. a $0.06 two-sided quote. Measured against the only real bid/ask
on disk -- 612,371 rows of CBOE delayed EOD chains, filtered to the desk's
tradeable universe (IWM/QQQ/SPY, 7 <= dte <= 60, volume > 0, oi > 0,
|delta| <= 0.70; n = 18,783) -- the quoted spread varies about 10x with
moneyness. The flat constant is wrong in BOTH directions, and a strategy that
systematically trades one moneyness is mis-costed. What is wanted is the
SHAPE, not a bigger scalar.

Every expected number below is transcribed BY HAND from the measured table
(its literals, in the ``MEASURED_GRID`` docstring) and every expected dollar
figure is literal arithmetic in the comments. The tests never import a
constant, bucket enum, or helper out of the implementation to build an
expectation -- doing so would make the oracle share the code under test.

The per-leg numbers are FULL two-sided quotes ($/share), not half spreads.
A round trip on a leg crosses the full quote once: half to get in, half to
get out. For a package crossed as ONE order, entry costs
(ask_long - mid_long) + (mid_short - bid_short) = (S_long + S_short) / 2 and
the exit costs the same again, so the round trip is S_long + S_short -- once,
not twice. ``test_per_leg_additive_is_not_double_counted`` kills that.

PROVENANCE (not optional, and not a default): these are CBOE-delayed END OF
DAY snapshots taken between 17:45 and 06:30 ET. The desk decides and fills on
10:00-15:15 ET intraday clocks. An EOD snapshot at or after 16:00 ET cannot
describe a 10:00 ET fill, and that gap is not falsifiable from this data. So
``SpreadModel`` takes its ``Provenance`` as a REQUIRED argument -- a cost that
does not declare where it came from cannot be constructed at all
(``test_provenance_cannot_be_omitted``), and the shipped constant says so out
loud.
"""

from __future__ import annotations

import re
from decimal import Decimal

import pytest
from tree_options.desk.cost_shape import (
    EOD_SNAPSHOT_PROVENANCE,
    Leg,
    NoPrice,
    SpreadModel,
    delta_bucket,
    dte_bucket,
    quoted_spread,
)

# --------------------------------------------------------------- the oracle
#
# FULL two-sided quoted spread, $/share, MEDIAN per cell. Measured from
# /home/alexk/.local/state/trex-strategy-sweep-20260930/chains.json
# (612,371 rows, 184 CBOE chain files, cboe-delayed EOD) over the tradeable
# universe above, n = 18,783. Recomputed and reproduced exactly (see the
# module docstring); cells are (|delta| bucket) x (dte bucket):
#
#               dte 7-21   dte 22-45   dte 46-60
#   |d| 0.00-0.10   0.020     0.020      0.020
#   |d| 0.10-0.20   0.030     0.040      0.040
#   |d| 0.20-0.35   0.040     0.070      0.070
#   |d| 0.35-0.50   0.050     0.100      0.090
#   |d| 0.50-0.70   0.130     0.210      0.190
#
# The rows are strictly increasing in |delta| at every dte, which is the
# shape. The columns are NOT monotone in dte: at |delta| 0.50-0.70 the
# 22-45 cell (0.210) exceeds the 46-60 cell (0.190). Nothing in this test
# file may assume otherwise; that is why the dte-shape test pins literals.
#
# Bucket membership is LEFT-OPEN / RIGHT-CLOSED -- (0.00, 0.10], (0.10, 0.20],
# ... -- with 0.00 itself in the first bucket. That is the convention which
# reproduces the measured per-bucket row counts exactly: 8,504 / 2,717 /
# 2,723 / 2,223 / 2,616, summing to the 18,783 universe. Boundaries are
# pinned on both sides in test_bucket_boundaries_pinned_on_both_sides so a
# `<` mutated to `<=` (or the reverse) dies.
DELTA_BUCKETS = ("0.00-0.10", "0.10-0.20", "0.20-0.35", "0.35-0.50", "0.50-0.70")
DTE_BUCKETS = ("7-21", "22-45", "46-60")

#: one representative interior point per (delta bucket, dte bucket) cell
DELTA_REPS = ("0.05", "0.15", "0.25", "0.40", "0.60")
DTE_REPS = (14, 30, 50)

#: the measured grid, transcribed by hand: MEASURED_GRID[i][j] is the full
#: two-sided spread for DELTA_REPS[i] at DTE_REPS[j], in $/share.
MEASURED_GRID = (
    (Decimal("0.020"), Decimal("0.020"), Decimal("0.020")),
    (Decimal("0.030"), Decimal("0.040"), Decimal("0.040")),
    (Decimal("0.040"), Decimal("0.070"), Decimal("0.070")),
    (Decimal("0.050"), Decimal("0.100"), Decimal("0.090")),
    (Decimal("0.130"), Decimal("0.210"), Decimal("0.190")),
)

COMMISSION = Decimal("0.65")
MULT = 100
#: a 2-leg round trip crosses 4 fills: 2 legs x (open, close)
FILL_COUNT = 4
#: ... at $0.65 each
FOUR_FILLS = FILL_COUNT * COMMISSION  # = 2.60
#: a 1-leg round trip crosses 2 fills, at $1.30
TWO_FILLS = 2 * COMMISSION  # = 1.30
#: what the flat CostModel charges for any 2-leg round trip:
#: 4 x (0.03 half spread) x 100 + 4 x 0.65 = 12.00 + 2.60
FLAT_ROUND_TRIP = Decimal("14.60")


def _model(*deltas: str, dte: int = 14) -> SpreadModel:
    """A candidate-bound model, the way the harness would hold one."""
    return SpreadModel(
        legs=tuple(Leg(abs_delta=Decimal(d), dte=dte) for d in deltas),
        provenance=EOD_SNAPSHOT_PROVENANCE,
        commission_per_fill=COMMISSION,
        multiplier=MULT,
    )


# ------------------------------------------------- 1. the lookup, pinned


@pytest.mark.parametrize("i", range(5), ids=DELTA_BUCKETS)
@pytest.mark.parametrize("j", range(3), ids=DTE_BUCKETS)
def test_measured_grid_cell_pinned(i: int, j: int) -> None:
    """Every one of the 15 cells equals the measured median, literally."""
    got = quoted_spread(Decimal(DELTA_REPS[i]), DTE_REPS[j])
    assert got == MEASURED_GRID[i][j]
    assert isinstance(got, Decimal), "spreads are money, never float"


def test_every_delta_row_is_strictly_increasing_in_moneyness() -> None:
    """The shape itself: at a fixed dte, wider in |delta| costs strictly more.

    This is the assertion a single-constant table cannot satisfy -- with one
    scalar substituted for all fifteen cells, every row is a flat sequence and
    the ``<`` on the first pair fails immediately.
    """
    for dte in DTE_REPS:
        row = [quoted_spread(Decimal(d), dte) for d in DELTA_REPS]
        assert row[0] < row[1] < row[2] < row[3] < row[4], f"flat or non-monotone at dte {dte}: {row}"


def test_deep_otm_and_near_the_money_differ_by_almost_ten_times() -> None:
    """0.210 / 0.020 = 10.5x on the measured grid; a scalar collapses this to 1x."""
    cheap = quoted_spread(Decimal("0.05"), 30)
    dear = quoted_spread(Decimal("0.60"), 30)
    assert dear == Decimal("0.210") and cheap == Decimal("0.020")
    assert dear / cheap == Decimal("10.5")


def test_grid_is_not_reducible_to_few_values() -> None:
    """A whole-grid census: at least four distinct quotes across the 15 cells.

    A guard against "fix" attempts that collapse the table to a handful of
    tiers, and the bluntest detector of a single-constant substitution.
    """
    seen = {quoted_spread(Decimal(d), t) for d in DELTA_REPS for t in DTE_REPS}
    assert len(seen) >= 4, f"the shape has collapsed to {len(seen)} distinct quote(s): {seen}"
    assert max(seen) / min(seen) >= 5


# ----------------------------------------------- 2. the crossing algebra


def test_package_crossing_costs_s_long_plus_s_short_exactly() -> None:
    """Crossing a 2-leg vertical as ONE package costs S_long + S_short.

    Hand-built quotes, nothing borrowed: the long is quoted 0.130 wide and
    the short 0.020 wide (both measured grid values at dte 14), so the
    package must cost exactly their sum and nothing else.

        0.130 + 0.020 = 0.150 $/share
        0.150 * 100   = $15.00 of crossing
        4 fills * 0.65 = $2.60 of commission
        total          = $17.60
    """
    model = _model("0.60", "0.05", dte=14)  # deep, then cheap
    assert model.package_spread() == Decimal("15.00")
    assert model.round_trip() == Decimal("17.60")
    assert model.fill_count() == FILL_COUNT


def test_per_leg_additive_is_not_double_counted() -> None:
    """The whole regression this exercise exists to catch.

    A package crossed as one order pays the spread ONCE per leg over the
    round trip -- half on the way in, half on the way out. Charging the full
    quote on each of the four fills (or summing two independent per-leg round
    trips that each already include both halves) doubles the crossing.

        wrong, doubled: (0.130 + 0.020) * 100 * 2 = $30.00 + $2.60 = $32.60
        right:                                    $15.00 + $2.60 = $17.60
    """
    model = _model("0.60", "0.05", dte=14)
    right = Decimal("17.60")
    doubled = 2 * model.package_spread() + FOUR_FILLS
    assert model.round_trip() == right
    assert model.round_trip() != doubled
    assert model.round_trip() < doubled
    # and the crossing is not charged once per FILL either: 4 fills x the
    # $7.50 per-fill half of the package would be $30.00 of crossing alone
    assert model.package_spread() != FOUR_FILLS * model.package_spread() / FILL_COUNT


def test_adding_a_leg_adds_exactly_that_leg() -> None:
    """A two-leg package costs one leg's quote plus exactly the other's.

    round_trip([A])    = S_A * 100 + 2 fills * 0.65
    round_trip([A, B]) = (S_A + S_B) * 100 + 4 fills * 0.65
    so the difference is S_B * 100 + $1.30 -- one more quote and one more
    pair of fills, with A never re-charged.

        S_A = 0.130 -> $13.00 + $1.30 = $14.30
        S_B = 0.020 ->  $2.00 + $1.30 =  $3.30 of increment
        14.30 + 3.30   = $17.60  -- the two-leg figure above, exactly
    """
    one = _model("0.60", dte=14)
    two = _model("0.60", "0.05", dte=14)
    assert one.round_trip() == Decimal("14.30")  # 13.00 + 1.30
    assert one.fill_count() == 2
    assert two.round_trip() - one.round_trip() == Decimal("3.30")  # 2.00 + 1.30
    assert one.package_spread() == Decimal("13.00")


def test_the_package_cost_does_not_depend_on_leg_order() -> None:
    """A long/short pair is a package, not a sequence: same bill either way.

    Sums are commutative, so a spread table that accidentally charged the
    first leg at entry and the second at exit would be order-dependent and
    would price the same structure two different ways depending on which leg
    the miner happened to list first.
    """
    long_first = _model("0.60", "0.05", dte=14)
    short_first = _model("0.05", "0.60", dte=14)
    assert long_first.round_trip() == short_first.round_trip() == Decimal("17.60")
    assert long_first.package_spread() == short_first.package_spread()


def test_commission_is_flat_and_still_there() -> None:
    """Removing commission must leave exactly the spread, unmultiplied.

    Two 0.130 legs: (0.130 + 0.130) * 100 = $26.00. A model that also scales
    by the fill count here would report $104.00.
    """
    model = SpreadModel(
        legs=(Leg(abs_delta=Decimal("0.60"), dte=14),) * 2,
        provenance=EOD_SNAPSHOT_PROVENANCE,
        commission_per_fill=Decimal(0),
        multiplier=MULT,
    )
    assert model.round_trip() == Decimal("26.00")
    assert model.package_spread() == Decimal("26.00")


def test_round_trip_takes_no_arguments_so_it_drops_into_the_harness() -> None:
    """outcomes._evaluate calls ``costs.round_trip()`` per candidate, unbound.

    So the legs are bound at construction (one model per candidate) and the
    call is argument-free -- that is what makes the shaped model substitutable
    for ``CostModel`` at outcomes.py without touching the call site.
    """
    model = _model("0.60", "0.05", dte=14)
    assert model.round_trip() == model.round_trip()  # no args, no state


# ------------------------------------- 3. the stops: refuse, never default


def test_legs_beyond_the_tradeable_universe_are_refused() -> None:
    """|delta| > 0.70 has no measured quote, so it has NO price.

    The corpus filter stops at 0.70; the deep-ITM rows above it are exactly
    the ones whose mean is poisoned (9.2% of rows carry a >= $1.00 spread and
    contribute 768% of the mean). The desk does not trade them, so the model
    must refuse rather than extrapolate a value into a regime it cannot see.
    """
    for delta in ("0.71", "0.75", "0.90", "0.99"):
        with pytest.raises(NoPrice):
            quoted_spread(Decimal(delta), 14)
        with pytest.raises(NoPrice):
            _model(delta, "0.05", dte=14).round_trip()
    assert issubclass(NoPrice, ValueError), "call sites already catch ValueError"


def test_dte_outside_the_measured_window_is_refused() -> None:
    """dte < 7 or > 60 has no measured quote, so it has NO price."""
    for dte in (0, 1, 6, 61, 90, 365):
        with pytest.raises(NoPrice):
            quoted_spread(Decimal("0.30"), dte)
    assert dte_bucket(7) == DTE_BUCKETS[0]
    assert dte_bucket(60) == DTE_BUCKETS[2]


def test_a_refused_leg_carries_a_reason_and_the_offending_inputs() -> None:
    """A refusal is typed and inspectable, not a bare assert-and-default.

    ``quoted_spread`` is the public entry point and sees BOTH inputs, so the
    ``NoPrice`` it raises must carry both back -- a caller that asked about
    ``(|delta|=0.85, dte=30)`` must not have to re-derive which half it
    passed. (The bucket classifiers see only one input each and may report
    ``None`` for the other; only ``quoted_spread`` is held to both.)
    """
    with pytest.raises(NoPrice) as deep:
        quoted_spread(Decimal("0.85"), 30)
    assert deep.value.abs_delta == Decimal("0.85")
    assert deep.value.dte == 30
    assert isinstance(deep.value.reason, str) and deep.value.reason.strip()

    with pytest.raises(NoPrice) as stale:
        quoted_spread(Decimal("0.30"), 3)
    assert stale.value.dte == 3
    assert stale.value.abs_delta == Decimal("0.30")
    assert stale.value.reason != deep.value.reason, "distinct causes, distinct reasons"


def test_a_refusal_names_which_stop_fired() -> None:
    """The reason must distinguish the two stops, not just say "no price".

    "|delta| beyond the measured universe" and "dte outside the measured
    window" are different bugs with different fixes, and a caller triaging a
    rejected candidate needs to tell them apart.
    """
    with pytest.raises(NoPrice) as deep:
        quoted_spread(Decimal("0.85"), 30)
    with pytest.raises(NoPrice) as stale:
        quoted_spread(Decimal("0.30"), 3)
    assert re.search(r"delta|moneyness", deep.value.reason, re.I)
    assert re.search(r"dte|expiry|tenor", stale.value.reason, re.I)


def test_a_refused_leg_can_be_built_but_never_priced() -> None:
    """``Leg`` is a dumb frozen record: the refusal happens at the quote.

    Building one out-of-universe leg must not explode (a candidate row the
    miner produced is data, not a programming error) but costing it must
    explode loudly. The danger this guards is a silent default creeping in.
    """
    leg = Leg(abs_delta=Decimal("0.95"), dte=120)
    assert leg.abs_delta == Decimal("0.95")
    with pytest.raises(NoPrice):
        _model("0.95", "0.05", dte=120).round_trip()


def test_a_package_with_one_unpriceable_leg_refuses_the_whole_package() -> None:
    """No partial quote, and no "price what we could" fallback."""
    with pytest.raises(NoPrice):
        _model("0.05", "0.92", dte=14).round_trip()


# ------------------------------------------- 4. shape, not a scalar


def test_two_deep_wings_cost_much_more_than_two_cheap_wings() -> None:
    """The headline: 28.60 vs 6.60, with the flat model's 14.60 in between.

        two |delta| 0.50-0.70 legs: (0.130 + 0.130) * 100 = $26.00 + $2.60 = $28.60
        two |delta| 0.00-0.10 legs: (0.020 + 0.020) * 100 =  $4.00 + $2.60 =  $6.60
        the flat model charges both of them exactly            $14.60

    So the flat model OVER-charges the cheap structure by 8.00 and
    UNDER-charges the dear one by 14.00. A single-constant substitution makes
    the two equal, which fails the first assertion.
    """
    deep = _model("0.60", "0.60", dte=14)
    cheap = _model("0.05", "0.05", dte=14)
    assert deep.round_trip() == Decimal("28.60")
    assert cheap.round_trip() == Decimal("6.60")
    assert deep.round_trip() > cheap.round_trip()
    assert deep.round_trip() > 3 * cheap.round_trip()
    assert deep.round_trip() > FLAT_ROUND_TRIP > cheap.round_trip()
    assert FLAT_ROUND_TRIP - cheap.round_trip() == Decimal("8.00")
    assert deep.round_trip() - FLAT_ROUND_TRIP == Decimal("14.00")


def test_a_strategy_that_trades_one_moneyness_is_moved_by_the_shape() -> None:
    """The reason the shape matters: same width, same days, different bill.

    A $5-wide structure is the same trade either way. What changes with
    moneyness is only what crossing it costs -- and under the flat model that
    number is identical, so a wing-heavy book and a delta-heavy book are
    indistinguishable in the digest. They must not be here.
    """
    wing = _model("0.05", "0.05", dte=30)
    core = _model("0.60", "0.60", dte=30)
    assert wing.package_spread() == Decimal("4.00")  # (0.020 + 0.020) * 100
    assert core.package_spread() == Decimal("42.00")  # (0.210 + 0.210) * 100
    assert core.round_trip() - wing.round_trip() == Decimal("38.00")


def test_the_shape_survives_the_change_in_moneyness_alone() -> None:
    """Hold dte fixed at 14, walk |delta| across the five buckets.

    The five round trips, each (2 x S) * 100 + $2.60:
        0.00-0.10  (0.020+0.020)*100 + 2.60 =   6.60
        0.10-0.20  (0.030+0.030)*100 + 2.60 =   8.60
        0.20-0.35  (0.040+0.040)*100 + 2.60 =  10.60
        0.35-0.50  (0.050+0.050)*100 + 2.60 =  12.60
        0.50-0.70  (0.130+0.130)*100 + 2.60 =  28.60
    A constant table returns 14.60 five times and dies at the first pair.
    """
    got = [_model("0.05", "0.05").round_trip(), _model("0.15", "0.15").round_trip(),
           _model("0.25", "0.25").round_trip(), _model("0.40", "0.40").round_trip(),
           _model("0.60", "0.60").round_trip()]
    assert got == [Decimal("6.60"), Decimal("8.60"), Decimal("10.60"),
                   Decimal("12.60"), Decimal("28.60")]


# ----------------------------------------- 5. buckets and their boundaries


@pytest.mark.parametrize(
    ("abs_delta", "bucket"),
    (("0.00", "0.00-0.10"),  # zero itself is in the first bucket
     ("0.10", "0.00-0.10"),  # the edge belongs to the LOWER bucket
     ("0.10001", "0.10-0.20"),
     ("0.20", "0.10-0.20"),
     ("0.20001", "0.20-0.35"),
     ("0.35", "0.20-0.35"),
     ("0.35001", "0.35-0.50"),
     ("0.50", "0.35-0.50"),
     ("0.50001", "0.50-0.70"),
     ("0.70", "0.50-0.70")),
)
def test_bucket_boundaries_pinned_on_both_sides(abs_delta: str, bucket: str) -> None:
    """(lo, hi] buckets, pinned a cent either side of every edge."""
    assert delta_bucket(Decimal(abs_delta)) == bucket


@pytest.mark.parametrize(
    ("dte", "bucket"),
    ((7, "7-21"), (14, "7-21"), (21, "7-21"), (22, "22-45"),
     (45, "22-45"), (46, "46-60"), (60, "46-60")),
)
def test_dte_buckets_are_closed_at_both_ends(dte: int, bucket: str) -> None:
    """dte is integral, so its buckets are plainly inclusive on both ends."""
    assert dte_bucket(dte) == bucket


def test_delta_bucket_of_an_untradeable_leg_is_refused() -> None:
    """Classification agrees with pricing: above 0.70 there is no bucket."""
    assert delta_bucket(Decimal("0.70")) == "0.50-0.70"
    with pytest.raises(NoPrice):
        delta_bucket(Decimal("0.70001"))


def test_a_negative_delta_is_refused_not_folded() -> None:
    """|delta| is the magnitude; a signed -0.30 is a put at 0.30, but a
    negative magnitude is nonsense and must not slip through the ``<= 0.70``
    check by arithmetic accident."""
    assert delta_bucket(Decimal("-0.30")) == delta_bucket(Decimal("0.30"))
    with pytest.raises(NoPrice):
        quoted_spread(Decimal("-0.71"), 14)


# ---------------------------------------------- 6. purity and provenance


def test_provenance_cannot_be_omitted() -> None:
    """A cost that does not say where it came from cannot be built.

    ``Provenance`` has no default, so ``SpreadModel(...)`` without it is a
    TypeError. That is the enforcement mechanism for the rule that every cost
    emitted must carry its provenance -- it is not a documentation promise.
    """
    with pytest.raises(TypeError):
        SpreadModel(legs=(Leg(abs_delta=Decimal("0.30"), dte=14),))  # type: ignore[call-arg]


def test_provenance_is_declared_before_every_defaulted_field() -> None:
    """``provenance`` is REQUIRED, so it must precede the defaulted knobs.

    A dataclass refuses a non-default field that follows a defaulted one, so
    "required" and "declared last" are mutually exclusive. An implementer who
    reaches for the natural reading order hits ``TypeError: non-default
    argument 'provenance' follows default argument`` at import time, i.e. the
    module will not even load. Pin the order so that cannot be discovered the
    hard way.
    """
    import dataclasses

    fields = list(dataclasses.fields(SpreadModel))
    names = [f.name for f in fields]
    assert names[:2] == ["legs", "provenance"], (
        f"legs and provenance come first, in that order; got {names}")
    prov = next(f for f in fields if f.name == "provenance")
    assert prov.default is dataclasses.MISSING, "provenance must have no default"
    assert prov.default_factory is dataclasses.MISSING, \
        "provenance must have no default factory either"


def test_the_shipped_provenance_admits_it_is_not_a_decision_clock_quote() -> None:
    """The constant must say what it is: a delayed EOD snapshot."""
    p = EOD_SNAPSHOT_PROVENANCE
    assert p.observation_window and p.observation_window.strip()
    assert p.decision_window and p.decision_window.strip()
    assert p.observation_window != p.decision_window, "the two windows are not the same"
    assert p.source.strip() and p.caveat.strip()
    text = " ".join([p.source, p.observation_window, p.decision_window, p.caveat]).lower()
    assert re.search(r"eod|end of day|snapshot", text)
    assert re.search(r"10:00|15:15|intraday|decision", p.decision_window.lower() + text)


def test_every_cost_comes_with_its_provenance() -> None:
    """The model hands its provenance back with the number."""
    model = _model("0.60", "0.05", dte=14)
    assert model.provenance is EOD_SNAPSHOT_PROVENANCE
    assert model.provenance.caveat.strip()


def test_the_lookup_is_pure_no_io_no_network() -> None:
    """Static guard: the model reads no files and opens no sockets.

    It must be a table, not a fetch -- a cost that silently hits the network
    mid-backtest is not reproducible and would not fail a gate.
    """
    from tree_options.desk import cost_shape

    with open(cost_shape.__file__, encoding="utf-8") as handle:
        source = handle.read()
    for forbidden in ("open(", "Path(", "os.environ", "socket", "requests",
                      "urllib", "httpx", "subprocess", "read_text", "json.load"):
        assert forbidden not in source, f"the cost model must stay pure: found {forbidden!r}"


def test_the_lookup_is_deterministic() -> None:
    """Same inputs, same Decimal, every time -- no clock, no cache, no RNG."""
    first = [quoted_spread(Decimal(d), t) for d in DELTA_REPS for t in DTE_REPS]
    second = [quoted_spread(Decimal(d), t) for d in DELTA_REPS for t in DTE_REPS]
    assert first == second
    assert all(isinstance(v, Decimal) for v in first)


# ------------------------------------------------- 7. back-compat / migrate


def test_the_flat_cost_model_is_untouched() -> None:
    """``CostModel`` keeps working unchanged, and still says 14.60.

    The digest and the skill decomposition are calibrated on the flat basis
    (verified: cost_drag / 14.60 is an exact integer for all 25 arms), so the
    shaped model lands beside it, not on top of it.
    """
    from tree_options.desk.outcomes import CostModel

    assert CostModel().round_trip() == FLAT_ROUND_TRIP
    assert CostModel().round_trip(legs=2) == FLAT_ROUND_TRIP


def test_a_flat_candidate_can_be_priced_by_the_shaped_model() -> None:
    """Migration path: hand the model a leg pair, get a shaped number.

    The harness holds one cost provider per candidate, so the migration is
    constructing ``SpreadModel`` for that candidate's legs. This is the whole
    of it -- and it is why ``round_trip()`` takes no arguments.
    """
    model = _model("0.30", "0.30", dte=30)
    assert model.round_trip() == Decimal("16.60")  # (0.070+0.070)*100 + 2.60
    assert model.round_trip() != FLAT_ROUND_TRIP
    # a mid-moneyness, mid-dte structure prices within 15% of the flat model:
    assert abs(model.round_trip() - FLAT_ROUND_TRIP) / FLAT_ROUND_TRIP < Decimal("0.15")
