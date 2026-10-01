"""Measured, SHAPED execution cost (red-first; the model is not written yet).

The flat ``CostModel`` (outcomes.py) charges every leg the same $0.03 half
spread, i.e. a $0.06 two-sided quote. Measured against the only real bid/ask
on disk -- 612,371 rows of CBOE delayed EOD chains, filtered to the desk's
tradeable universe (IWM/QQQ/SPY, 7 <= dte <= 60, volume > 0, oi > 0,
|delta| <= 0.70; n = 18,783) -- the quoted spread varies about 10x with
moneyness. The flat constant is wrong in BOTH directions, and a strategy that
systematically trades one moneyness is mis-costed. What is wanted is the
SHAPE, not a bigger scalar.

Every expected number below is transcribed BY HAND from the derived surface
in the comments, and every expected dollar figure is literal arithmetic in
the test that uses it. The tests never import a constant, bucket enum, or
helper out of the implementation to build an expectation -- doing so would
make the oracle share the code under test.

THE UNIT: HALF-SPREAD PER SHARE PER FILL
----------------------------------------
The corpus medians are FULL two-sided quotes. The desk pays HALF the quote on
each fill, exactly as the flat ``CostModel`` it replaces paid half of $0.06.
Every leg fills twice, so a 2-leg package is 4 fills and

    round_trip(2 legs) = 4 * half * 100 + 4 * 0.65 = 400 * half + 2.60

Any claim of the form "cost is 3.7x optimistic" crossed the FULL quote on all
four fills. That is a convention error, not a finding.

PROVENANCE (not optional, and not a default): these are CBOE-delayed END OF
DAY snapshots taken between 17:45 and 06:30 ET. The desk decides and fills on
10:00-15:15 ET intraday clocks. An EOD snapshot at or after 16:00 ET cannot
describe a 10:00 ET fill, and that gap is not falsifiable from this data. So
``Leg`` carries ``is_eod_snapshot`` with NO DEFAULT -- a cost that does not
declare where it came from cannot be constructed at all.
"""

from __future__ import annotations

import re
from decimal import Decimal

import pytest

try:  # pragma: no cover - the RED path is the point
    from tree_options.desk import cost
    _IMPORT_ERROR: Exception | None = None
except Exception as exc:  # any import failure is the RED
    cost = None  # type: ignore[assignment]
    _IMPORT_ERROR = exc


def mc():
    """The module under test, or a FAILURE that names why it is absent."""
    if cost is None:
        pytest.fail(
            "tree_options.desk.cost does not exist yet "
            f"(import error: {_IMPORT_ERROR!r}). The shaped cost model is the "
            "deliverable these tests demand; until it exists they must fail, "
            "not skip."
        )
    return cost


# --------------------------------------------------------------- the oracle
#
# MEASURED median FULL two-sided quoted spread, $/share, by |delta|, over the
# tradeable universe above (n = 18,783):
#
#   |d| 0.00-0.10  n= 8,504  0.020
#   |d| 0.10-0.20  n= 2,717  0.030
#   |d| 0.20-0.35  n= 2,723  0.050
#   |d| 0.35-0.50  n= 2,223  0.060
#   |d| 0.50-0.70  n= 2,616  0.190
#   dte marginal: 7-21  0.030  |  22-45  0.040  |  46-60  0.050
#
# The rows are strictly increasing in |delta|, which is the shape.
#
# The corpus publishes the |delta| marginals and a dte marginal, NOT their
# joint. The 15 cells below are therefore DERIVED separably:
#
#     half = MEASURED_MEDIAN_FULL_SPREAD[b] / 2 * DTE_BAND_MULTIPLIER[t]
#
# which reproduces the |delta| marginals exactly at dte 7-21 and reproduces
# the dte ratio exactly. The joint is an approximation and the module
# docstring says so.
#
# Bucket membership is RIGHT-CLOSED -- (lo, hi] -- with 0.00 in the first
# bucket. That is the convention which reproduces the measured per-bucket row
# counts exactly: 8,504 / 2,717 / 2,723 / 2,223 / 2,616, summing to the
# 18,783 universe.
DELTA_BUCKETS = ("0.00-0.10", "0.10-0.20", "0.20-0.35", "0.35-0.50", "0.50-0.70")
DTE_BUCKETS = ("7-21", "22-45", "46-60")

#: one representative interior point per (delta bucket, dte bucket) cell
DELTA_REPS = ("0.05", "0.15", "0.275", "0.425", "0.60")
DTE_REPS = (14, 30, 53)

#: the derived grid, transcribed by hand. DERIVED_GRID[i][j] is the
#: half-spread per share PER FILL for DELTA_REPS[i] at DTE_REPS[j].
#:
#:   400 * half + 2.60 is the 2-leg round trip, shown in the second grid.
DERIVED_GRID = (
    (Decimal("0.010000"), Decimal("0.013330"), Decimal("0.016670")),
    (Decimal("0.015000"), Decimal("0.019995"), Decimal("0.025005")),
    (Decimal("0.025000"), Decimal("0.033325"), Decimal("0.041675")),
    (Decimal("0.030000"), Decimal("0.039990"), Decimal("0.050010")),
    (Decimal("0.095000"), Decimal("0.126635"), Decimal("0.158365")),
)
TWO_LEG_ROUND_TRIP = (
    (Decimal("6.600000"), Decimal("7.932000"), Decimal("9.268000")),
    (Decimal("8.600000"), Decimal("10.598000"), Decimal("12.602000")),
    (Decimal("12.600000"), Decimal("15.930000"), Decimal("19.270000")),
    (Decimal("14.600000"), Decimal("18.596000"), Decimal("22.604000")),
    (Decimal("40.600000"), Decimal("53.254000"), Decimal("65.946000")),
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


def _legs(*deltas: str, dte: int = 14):
    return [mc().Leg(symbol="SPY", abs_delta=Decimal(d), dte=dte,
                     source_session="2026-06-01",
                     source_timestamp_et="2026-06-01T18:05:00-04:00",
                     is_eod_snapshot=True) for d in deltas]


def _model(*deltas: str, dte: int = 14):
    """Price a candidate's legs, the way the harness holds one cost provider."""
    return mc().SpreadCostModel.measured().price(_legs(*deltas, dte=dte))


# ------------------------------------------------- 1. the lookup, pinned


@pytest.mark.parametrize("i", range(5), ids=DELTA_BUCKETS)
@pytest.mark.parametrize("j", range(3), ids=DTE_BUCKETS)
def test_derived_grid_cell_pinned(i: int, j: int) -> None:
    """Every one of the 15 cells equals the derived measurement, literally."""
    model = mc().SpreadCostModel.measured()
    got = model.half_spread_per_share(Decimal(DELTA_REPS[i]), DTE_REPS[j])
    assert got == DERIVED_GRID[i][j]
    assert isinstance(got, Decimal), "spreads are money, never float"
    quote = model.price(_legs(DELTA_REPS[i], DELTA_REPS[i], dte=DTE_REPS[j]))
    assert quote.total_round_trip == TWO_LEG_ROUND_TRIP[i][j]


def test_every_delta_row_is_strictly_increasing_in_moneyness() -> None:
    """The shape itself: at a fixed dte, wider in |delta| costs strictly more.

    This is the assertion a single-constant table cannot satisfy -- with one
    scalar substituted for all fifteen cells, every row is a flat sequence and
    the ``<`` on the first pair fails immediately.
    """
    model = mc().SpreadCostModel.measured()
    for dte in DTE_REPS:
        row = [model.half_spread_per_share(Decimal(d), dte) for d in DELTA_REPS]
        assert row[0] < row[1] < row[2] < row[3] < row[4], f"flat or non-monotone at dte {dte}: {row}"


def test_deep_otm_and_near_the_money_differ_by_almost_ten_times() -> None:
    """0.190 / 0.020 = 9.5x on the measured marginals; a scalar collapses this to 1x."""
    model = mc().SpreadCostModel.measured()
    cheap = model.half_spread_per_share(Decimal("0.05"), 30)
    dear = model.half_spread_per_share(Decimal("0.60"), 30)
    assert dear / cheap == Decimal("0.158365") / Decimal("0.016670")
    assert dear > 5 * cheap


def test_grid_is_not_reducible_to_few_values() -> None:
    """A whole-grid census: at least four distinct quotes across the 15 cells.

    A guard against "fix" attempts that collapse the table to a handful of
    tiers, and the bluntest detector of a single-constant substitution.
    """
    model = mc().SpreadCostModel.measured()
    seen = {model.half_spread_per_share(Decimal(d), t) for d in DELTA_REPS for t in DTE_REPS}
    assert len(seen) >= 4, f"the shape has collapsed to {len(seen)} distinct quote(s): {seen}"
    assert max(seen) / min(seen) >= 5


# ----------------------------------------------- 2. the crossing algebra


def test_package_crossing_costs_the_sum_of_the_two_legs_halves() -> None:
    """A 2-leg vertical costs each leg's half-spread on both of its fills.

    Hand-built, nothing borrowed: the deep leg's half is $0.095 and the cheap
    leg's is $0.010, so the package must cost exactly their sum and nothing
    else.

        deep leg  : 2 fills * 0.095 * 100 = $19.00
        cheap leg : 2 fills * 0.010 * 100 =  $2.00
        crossing  :                            $21.00
        4 fills * 0.65 =                        $2.60 of commission
        total     :                            $23.60
    """
    quote = _model("0.60", "0.05", dte=14)  # deep, then cheap
    assert quote.spread_total == Decimal("21.00")
    assert quote.commission_total == FOUR_FILLS
    assert quote.total_round_trip == Decimal("23.60")
    assert len(quote.legs) == FILL_COUNT // 2


def test_the_package_cost_is_not_double_counted() -> None:
    """The whole regression this exercise exists to catch.

    Every leg fills TWICE, so its half-spread is charged on the way in and on
    the way out. Charging the full quote on each of the four fills doubles the
    crossing.

        wrong, doubled: 2 * 21.00 + 2.60 = $44.60
        right:                  21.00 + 2.60 = $23.60
    """
    quote = _model("0.60", "0.05", dte=14)
    right = Decimal("23.60")
    doubled = 2 * quote.spread_total + FOUR_FILLS
    assert quote.total_round_trip == right
    assert quote.total_round_trip != doubled
    assert quote.total_round_trip < doubled
    # the crossing is not charged once per FILL either
    assert quote.spread_total != FOUR_FILLS * quote.spread_total / (FILL_COUNT // 2)


def test_adding_a_leg_adds_exactly_that_leg() -> None:
    """A two-leg package costs one leg's quote plus exactly the other's.

    round_trip([A])    = 2 * S_A * 100 + 2 fills * 0.65
    round_trip([A, B]) = 2 * (S_A + S_B) * 100 + 4 fills * 0.65
    so the difference is 2 * S_B * 100 + $1.30 -- one more quote and one more
    pair of fills, with A never re-charged.

        S_A = 0.095 -> 19.00 + 1.30 = $20.30
        S_B = 0.010 ->  2.00 + 1.30 =  $3.30 of increment
        20.30 + 3.30  = $23.60 -- the mixed figure, exactly
    """
    one = _model("0.60", dte=14)
    two = _model("0.60", "0.05", dte=14)
    assert one.total_round_trip == Decimal("20.30")   # 19.00 + 1.30
    assert len(one.legs) == 1
    assert two.total_round_trip - one.total_round_trip == Decimal("3.30")  # 2.00 + 1.30
    assert one.spread_total == Decimal("19.00")


def test_the_package_cost_does_not_depend_on_leg_order() -> None:
    """A long/short pair is a package, not a sequence: same bill either way.

    Sums are commutative, so a spread table that accidentally charged the
    first leg at entry and the second at exit would be order-dependent and
    would price the same structure two different ways depending on which leg
    the miner happened to list first.
    """
    long_first = _model("0.60", "0.05", dte=14)
    short_first = _model("0.05", "0.60", dte=14)
    assert long_first.total_round_trip == short_first.total_round_trip == Decimal("23.60")
    assert long_first.spread_total == short_first.spread_total


def test_commission_is_flat_and_still_there() -> None:
    """Removing commission must leave exactly the spread, unmultiplied.

    Two 0.60-delta legs: 2 fills each * 0.095 * 100 = $38.00. A model that
    also scaled by the fill count here would report $152.00.
    """
    model = mc().SpreadCostModel(commission_per_leg=Decimal(0))
    quote = model.price(_legs("0.60", "0.60", dte=14))
    assert quote.spread_total == Decimal("38.00")
    assert quote.commission_total == Decimal("0")
    assert quote.total_round_trip == Decimal("38.00")


def test_round_trip_takes_the_legs_as_a_required_argument() -> None:
    """``outcomes._evaluate`` prices PER CANDIDATE inside a loop.

    The legs are a property of the candidate, not of the model, so
    ``round_trip(legs)`` takes them as a required positional argument rather
    than binding them at construction. A model with the legs bound would need
    one instance per candidate.
    """
    import inspect

    signature = inspect.signature(mc().SpreadCostModel.round_trip)
    parameters = list(signature.parameters.values())
    assert parameters[1].name == "legs" and parameters[1].default is inspect.Parameter.empty
    model = mc().SpreadCostModel.measured()
    assert model.round_trip(_legs("0.60", "0.05", dte=14)) == Decimal("23.60")


# ------------------------------------- 3. the stops: refuse, never default


def test_legs_beyond_the_tradeable_universe_are_refused() -> None:
    """|delta| > 0.70 has no measured quote, so it has NO price.

    The corpus filter stops at 0.70; the deep-ITM rows above it are exactly
    the ones whose mean is poisoned (9.2% of rows carry a >= $1.00 spread and
    contribute 768% of the mean). The desk does not trade them, so the model
    must refuse rather than extrapolate a value into a regime it cannot see.
    """
    for delta in ("0.71", "0.75", "0.90", "0.99"):
        with pytest.raises(mc().UnpricedCostError):
            _model(delta, "0.05", dte=14)
    assert issubclass(mc().UnpricedCostError, ValueError), "call sites already catch ValueError"


def test_dte_outside_the_measured_window_is_refused() -> None:
    """dte < 7 or > 60 has no measured quote, so it has NO price."""
    for dte in (0, 1, 6, 61, 90, 365):
        with pytest.raises(mc().UnpricedCostError):
            _model("0.30", dte=dte)
    assert mc().dte_bucket(7) == DTE_BUCKETS[0]
    assert mc().dte_bucket(60) == DTE_BUCKETS[2]


def test_a_refused_leg_carries_a_reason_and_the_offending_leg() -> None:
    """A refusal is typed and inspectable, not a bare assert-and-default.

    ``price`` is the public entry point and sees BOTH inputs, so the error it
    raises must carry the offending ``Leg`` back -- a caller that asked about
    ``(|delta|=0.85, dte=30)`` must not have to re-derive which half it
    passed.
    """
    with pytest.raises(mc().UnpricedCostError) as deep:
        _model("0.85", dte=30)
    assert deep.value.key.abs_delta == Decimal("0.85")
    assert deep.value.key.dte == 30
    assert isinstance(deep.value.reason, str) and deep.value.reason.strip()

    with pytest.raises(mc().UnpricedCostError) as stale:
        _model("0.30", dte=3)
    assert stale.value.key.dte == 3
    assert stale.value.key.abs_delta == Decimal("0.30")
    assert stale.value.reason != deep.value.reason, "distinct causes, distinct reasons"


def test_a_refusal_names_which_stop_fired() -> None:
    """The reason must distinguish the two stops, not just say "no price".

    "|delta| beyond the measured universe" and "dte outside the measured
    window" are different bugs with different fixes, and a caller triaging a
    rejected candidate needs to tell them apart.
    """
    with pytest.raises(mc().UnpricedCostError) as deep:
        _model("0.85", dte=30)
    with pytest.raises(mc().UnpricedCostError) as stale:
        _model("0.30", dte=3)
    assert deep.value.reason == "delta_out_of_universe"
    assert stale.value.reason == "dte_out_of_universe"
    assert re.search(r"delta|moneyness", str(deep.value), re.I)
    assert re.search(r"dte|expiry|tenor", str(stale.value), re.I)


def test_a_refused_leg_can_be_built_but_never_priced() -> None:
    """``Leg`` is a dumb frozen record: the refusal happens at the quote.

    Building one out-of-universe leg must not explode (a candidate row the
    miner produced is data, not a programming error) but costing it must
    explode loudly. The danger this guards is a silent default creeping in.
    """
    leg = mc().Leg(symbol="SPY", abs_delta=Decimal("0.95"), dte=120,
                   source_session="2026-06-01",
                   source_timestamp_et="2026-06-01T18:05:00-04:00",
                   is_eod_snapshot=True)
    assert leg.abs_delta == Decimal("0.95")
    with pytest.raises(mc().UnpricedCostError):
        mc().SpreadCostModel.measured().price([leg, _legs("0.05", dte=120)[0]])


def test_a_package_with_one_unpriceable_leg_refuses_the_whole_package() -> None:
    """No partial quote, and no "price what we could" fallback."""
    with pytest.raises(mc().UnpricedCostError):
        _model("0.05", "0.92", dte=14)


# ------------------------------------------- 4. shape, not a scalar


def test_two_deep_wings_cost_much_more_than_two_cheap_wings() -> None:
    """The headline: 40.60 vs 6.60, with the flat model's 14.60 in between.

        two |delta| 0.50-0.70 legs: (0.095+0.095) * 200 = $38.00 + $2.60 = $40.60
        two |delta| 0.00-0.10 legs: (0.010+0.010) * 200 =  $4.00 + $2.60 =  $6.60
        the flat model charges both of them exactly            $14.60

    So the flat model OVER-charges the cheap structure by 8.00 and
    UNDER-charges the dear one by 26.00. A single-constant substitution makes
    the two equal, which fails the first assertion.
    """
    deep = _model("0.60", "0.60", dte=14)
    cheap = _model("0.05", "0.05", dte=14)
    assert deep.total_round_trip == Decimal("40.60")
    assert cheap.total_round_trip == Decimal("6.60")
    assert deep.total_round_trip > cheap.total_round_trip
    assert deep.total_round_trip > 3 * cheap.total_round_trip
    assert deep.total_round_trip > FLAT_ROUND_TRIP > cheap.total_round_trip
    assert FLAT_ROUND_TRIP - cheap.total_round_trip == Decimal("8.00")
    assert deep.total_round_trip - FLAT_ROUND_TRIP == Decimal("26.00")


def test_a_strategy_that_trades_one_moneyness_is_moved_by_the_shape() -> None:
    """The reason the shape matters: same width, same days, different bill.

    A $5-wide structure is the same trade either way. What changes with
    moneyness is only what crossing it costs -- and under the flat model that
    number is identical, so a wing-heavy book and a delta-heavy book are
    indistinguishable in the digest. They must not be here.
    """
    wing = _model("0.05", "0.05", dte=30)
    core = _model("0.60", "0.60", dte=30)
    # dte 30 is band 1 (22-45), so the halves are 0.013330 and 0.126635
    assert wing.spread_total == Decimal("5.332")   # (0.013330 + 0.013330) * 200
    assert core.spread_total == Decimal("50.654")  # (0.126635 + 0.126635) * 200
    assert core.total_round_trip - wing.total_round_trip == Decimal("45.322")


def test_the_shape_survives_the_change_in_moneyness_alone() -> None:
    """Hold dte fixed at 14, walk |delta| across the five buckets.

    The five round trips, each 400 * half + $2.60:
        0.00-0.10  0.010000 * 400 + 2.60 =  $6.60
        0.10-0.20  0.015000 * 400 + 2.60 =  $8.60
        0.20-0.35  0.025000 * 400 + 2.60 = $12.60
        0.35-0.50  0.030000 * 400 + 2.60 = $14.60
        0.50-0.70  0.095000 * 400 + 2.60 = $40.60
    A constant table returns 14.60 five times and dies at the first pair.
    """
    got = [_model(d, d).total_round_trip
           for d in ("0.05", "0.15", "0.275", "0.425", "0.60")]
    assert got == [Decimal("6.600000"), Decimal("8.600000"), Decimal("12.600000"),
                   Decimal("14.600000"), Decimal("40.600000")]


# ----------------------------------------- 5. buckets and their boundaries


@pytest.mark.parametrize(
    ("abs_delta", "bucket"),
    (("0.00", "0.00-0.10"),  # zero itself is in the first bucket
     ("0.10", "0.00-0.10"),  # the edge belongs to the LOWER bucket ((lo, hi])
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
    """(lo, hi] buckets, pinned a micro-tick either side of every edge."""
    assert mc().delta_bucket(Decimal(abs_delta)) == bucket


@pytest.mark.parametrize(
    ("dte", "bucket"),
    ((7, "7-21"), (14, "7-21"), (21, "7-21"), (22, "22-45"),
     (45, "22-45"), (46, "46-60"), (60, "46-60")),
)
def test_dte_buckets_are_closed_at_both_ends(dte: int, bucket: str) -> None:
    """dte is integral, so its buckets are plainly inclusive on both ends."""
    assert mc().dte_bucket(dte) == bucket


def test_delta_bucket_of_an_untradeable_leg_is_refused() -> None:
    """Classification agrees with pricing: above 0.70 there is no bucket."""
    assert mc().delta_bucket(Decimal("0.70")) == "0.50-0.70"
    with pytest.raises(ValueError):
        mc().delta_bucket(Decimal("0.70001"))


def test_a_negative_delta_is_refused_not_folded() -> None:
    """|delta| is the magnitude; a signed -0.30 is a put at 0.30, but a
    negative MAGNITUDE is nonsense and must not slip through the ``<= 0.70``
    check by arithmetic accident. The universe is ``[0, 0.70]``, closed at
    both ends, and both ends refuse."""
    with pytest.raises(mc().UnpricedCostError):
        mc().delta_bucket(Decimal("-0.30"))
    with pytest.raises(mc().UnpricedCostError):
        _model("-0.71", dte=14)


# ---------------------------------------------- 6. purity and provenance


def test_is_eod_snapshot_cannot_be_omitted() -> None:
    """A cost that does not say where it came from cannot be built.

    ``is_eod_snapshot`` has no default, so ``Leg(...)`` without it is a
    TypeError. That is the enforcement mechanism for the rule that every cost
    emitted must carry its provenance -- it is not a documentation promise.
    """
    with pytest.raises(TypeError):
        mc().Leg(  # type: ignore[call-arg]
            symbol="SPY", abs_delta=Decimal("0.30"), dte=14,
            source_session="2026-06-01",
            source_timestamp_et="2026-06-01T18:05:00-04:00")


def test_provenance_is_declared_after_the_required_fields() -> None:
    """Every provenance field is REQUIRED, so none may follow a defaulted one.

    A dataclass refuses a non-default field that follows a defaulted one, so
    "required" and "declared last" are mutually exclusive. An implementer who
    reaches for the natural reading order hits ``TypeError: non-default
    argument follows default argument`` at import time, i.e. the module will
    not even load. Pin the order so that cannot be discovered the hard way.
    """
    import dataclasses

    fields = list(dataclasses.fields(mc().Leg))
    names = [f.name for f in fields]
    assert names == ["symbol", "abs_delta", "dte", "source_session",
                     "source_timestamp_et", "is_eod_snapshot"]
    for field in fields:
        assert field.default is dataclasses.MISSING, f"{field.name} must have no default"
        assert field.default_factory is dataclasses.MISSING, \
            f"{field.name} must have no default factory either"


def test_a_blank_provenance_field_is_refused() -> None:
    """A whitespace-only session or timestamp is not a provenance record."""
    for field in ("source_session", "source_timestamp_et"):
        kwargs = {"symbol": "SPY", "abs_delta": Decimal("0.30"), "dte": 14,
                  "source_session": "2026-06-01",
                  "source_timestamp_et": "2026-06-01T18:05:00-04:00",
                  "is_eod_snapshot": True}
        kwargs[field] = "   "
        with pytest.raises(mc().ProvenanceError):
            mc().Leg(**kwargs)


def test_the_shipped_provenance_admits_it_is_not_a_decision_clock_quote() -> None:
    """The record must say what it is: a delayed EOD snapshot."""
    payload = mc().CostProvenance(
        source="cboe-delayed-eod-chains",
        snapshot_window_et="17:45-06:30",
        universe_filter="|delta|<=0.70, 7<=dte<=60, volume>0, oi>0",
        n_rows=18783,
        decision_clocks_et=("10:00", "15:15"),
    ).as_dict()
    assert payload["source"].strip()
    assert payload["snapshot_window_et"] == "17:45-06:30"
    assert payload["snapshot_window_et"] not in ("", "10:00-15:15")
    text = " ".join(str(v) for v in payload.values()).lower()
    assert re.search(r"eod|end of day|snapshot", text)
    assert re.search(r"10:00|15:15|intraday|decision", text)


def test_every_cost_comes_with_its_provenance() -> None:
    """The quote hands its per-leg provenance back with the number."""
    quote = _model("0.60", "0.05", dte=14)
    assert len(quote.provenance) == 2
    for record in quote.provenance:
        assert record.source_session == "2026-06-01"
        assert record.source_timestamp_et == "2026-06-01T18:05:00-04:00"
        assert record.is_eod_snapshot is True
    assert quote.is_eod_snapshot is True
    assert quote.assert_never_observed_at_a_decision_clock() is None
    assert "decision clock" in quote.provenance[0].observation_gap()


def test_the_lookup_is_pure_no_io_no_network() -> None:
    """Static guard: the model reads no files and opens no sockets.

    It must be a table, not a fetch -- a cost that silently hits the network
    mid-backtest is not reproducible and would not fail a gate.
    """
    from tree_options.desk import cost as the_module

    with open(the_module.__file__, encoding="utf-8") as handle:
        source = handle.read()
    for forbidden in ("open(", "Path(", "os.environ", "socket", "requests",
                      "urllib", "httpx", "subprocess", "read_text", "json.load"):
        assert forbidden not in source, f"the cost model must stay pure: found {forbidden!r}"


def test_the_lookup_is_deterministic() -> None:
    """Same inputs, same Decimal, every time -- no clock, no cache, no RNG."""
    model = mc().SpreadCostModel.measured()
    first = [model.half_spread_per_share(Decimal(d), t)
             for d in DELTA_REPS for t in DTE_REPS]
    second = [model.half_spread_per_share(Decimal(d), t)
              for d in DELTA_REPS for t in DTE_REPS]
    assert first == second
    assert all(isinstance(v, Decimal) for v in first)


# ------------------------------------------------- 7. back-compat / migrate


def test_the_flat_cost_model_is_untouched() -> None:
    """``CostModel`` keeps working unchanged, and still says 14.60.

    The digest and the skill decomposition are calibrated on the flat basis
    (verified: cost_drag / 14.60 is an exact integer for all 25 arms), so the
    shaped model lands BESIDE it, not on top of it. Its
    ``half_spread_per_share`` stays a frozen Decimal FIELD.
    """
    import dataclasses

    from tree_options.desk.outcomes import CostModel

    assert CostModel().round_trip() == FLAT_ROUND_TRIP
    assert CostModel().round_trip(legs=2) == FLAT_ROUND_TRIP
    fields = {f.name: f for f in dataclasses.fields(CostModel)}
    assert isinstance(fields["half_spread_per_share"].default, Decimal), \
        "the flat model's half_spread_per_share must stay a Decimal field"


def test_a_flat_candidate_can_be_priced_by_the_shaped_model() -> None:
    """Migration path: hand the model a leg pair, get a shaped number.

    The harness holds one cost provider per candidate, so the migration is
    passing that candidate's legs to ``round_trip``.
    """
    quote = _model("0.30", "0.30", dte=30)
    assert quote.total_round_trip == Decimal("15.930000")  # 0.033325 * 400 + 2.60
    assert quote.total_round_trip != FLAT_ROUND_TRIP
    # a mid-moneyness, mid-dte structure prices within 15% of the flat model:
    assert abs(quote.total_round_trip - FLAT_ROUND_TRIP) / FLAT_ROUND_TRIP < Decimal("0.15")
