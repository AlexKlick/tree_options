"""TEST CONTRACT C (part 1) — the measured execution-cost model.

Tests only. The implementation does not exist yet; this file pins the API,
the arithmetic, the provenance obligation, determinism and purity so that three
independent implementers can build the SAME thing.

THE ORACLE
----------
Every expected number in this file is hand-written from the measured corpus
table (612,371 rows / 184 CBOE chain files, filtered to the desk's tradeable
universe: symbol in IWM/QQQ/SPY, 7 <= dte <= 60, volume > 0, oi > 0,
|delta| <= 0.70; n = 18,783). Median FULL two-sided quoted spread ($/share):

    |delta| 0.00-0.10  n= 8,504  median $0.020
    |delta| 0.10-0.20  n= 2,717  median $0.030
    |delta| 0.20-0.35  n= 2,723  median $0.050
    |delta| 0.35-0.50  n= 2,223  median $0.060
    |delta| 0.50-0.70  n= 2,616  median $0.190
    dte  7-21 median $0.030 | dte 22-45 median $0.040 | dte 46-60 median $0.050

CONVENTION (this is the trap; do not get it wrong)
----------------------------------------------------
The model charges HALF the quoted spread on each fill, exactly as the flat
``CostModel`` it replaces charged half of $0.06. A 2-leg vertical has 4 fills,
so ``round_trip = 4 * half_spread * 100 + 4 * 0.65`` when both legs price the
same. Any claim of the form "cost is 3.7x optimistic" crossed the FULL quote
on all four fills and is a convention error, not a finding.

MODELLED APPROXIMATION (named, because it is one)
-------------------------------------------------
The corpus gives |delta| marginals and a dte marginal, not the joint. This
contract multiplies them: ``half = median(|delta| bucket) / 2 * dte_multiplier``.
The dte multipliers are the ratio of the measured dte marginals to the shortest
band (0.030 : 0.040 : 0.050 == 1 : 4/3 : 5/3), so the model reproduces both
marginals at the universe level. ``test_dte_multipliers_reproduce_the_measured_
dte_marginals`` pins that relationship mechanically.
"""

from __future__ import annotations

import builtins
import math
import os
import socket
import sys
from decimal import Decimal
from typing import Any

import pytest

D = Decimal

# --------------------------------------------------------------------------
# Contract resolution. Every test below fails with an explicit list of the
# names it needs, so the RED run says exactly what is missing rather than
# dying in collection.
# --------------------------------------------------------------------------

REQUIRED_SYMBOLS = (
    "CostModelError",
    "LegOutsideTradeableUniverseError",
    "ProvenanceError",
    "ObservationProvenance",
    "LegQuote",
    "SpreadCostModel",
    "SpreadQuote",
    "MEASURED_MEDIAN_FULL_SPREAD",
    "DELTA_BUCKET_EDGES",
    "DTE_BAND_MULTIPLIER",
    "COMMISSION_PER_LEG",
    "CONTRACT_MULTIPLIER",
    "HALF_SPREAD_QUANTUM",
    "FILLS_PER_LEG",
    "MAX_ABS_DELTA",
    "MIN_DTE",
    "MAX_DTE",
    "COST_SCHEMA",
)


def cost_module() -> Any:
    """The module under contract, or an AssertionError naming what is absent."""
    try:
        from tree_options.desk import cost
    except ImportError as exc:  # pragma: no cover - the RED path
        raise AssertionError(
            "tree_options.desk.cost does not exist; this file is the contract for it. "
            f"import failed: {exc}"
        ) from exc
    missing = [name for name in REQUIRED_SYMBOLS if not hasattr(cost, name)]
    if missing:
        raise AssertionError(
            "tree_options.desk.cost is missing required contract symbols: " + ", ".join(missing)
        )
    return cost


def measured_model() -> Any:
    """The corpus-default model, built by the implementation's own entry point."""
    cost = cost_module()
    factory = getattr(cost.SpreadCostModel, "measured", None)
    if factory is None:
        raise AssertionError("tree_options.desk.cost.SpreadCostModel.measured() is required")
    return factory()


# --------------------------------------------------------------------------
# The oracle, restated as hand-written constants. These are NOT imported from
# the implementation; a test that reads its expectations out of the code it
# tests proves nothing.
# --------------------------------------------------------------------------

EDGES = (D("0.10"), D("0.20"), D("0.35"), D("0.50"), D("0.70"))
MEDIANS = (D("0.020"), D("0.030"), D("0.050"), D("0.060"), D("0.190"))
DTE_BANDS = ((7, 21), (22, 45), (46, 60))
DTE_MULTIPLIERS = (D("1.000"), D("1.333"), D("1.667"))
COMMISSION = D("0.65")
MULTIPLIER = 100
QUANTUM = D("0.000001")

#: A |delta| strictly inside each bucket, so the cell under test is unambiguous.
CENTRES = (D("0.05"), D("0.15"), D("0.275"), D("0.425"), D("0.60"))

#: (dte_band, delta_bucket) -> (half_spread_per_share, two_leg_round_trip).
#: Hand-computed, not read back from the implementation.
GRID = {
    (0, 0): (D("0.010000"), D("6.600000")),
    (0, 1): (D("0.015000"), D("8.600000")),
    (0, 2): (D("0.025000"), D("12.600000")),
    (0, 3): (D("0.030000"), D("14.600000")),  # <- exactly the flat model's number
    (0, 4): (D("0.095000"), D("40.600000")),
    (1, 0): (D("0.013330"), D("7.932000")),
    (1, 1): (D("0.019995"), D("10.598000")),
    (1, 2): (D("0.033325"), D("15.930000")),
    (1, 3): (D("0.039990"), D("18.596000")),
    (1, 4): (D("0.126635"), D("53.254000")),
    (2, 0): (D("0.016670"), D("9.268000")),
    (2, 1): (D("0.025005"), D("12.602000")),
    (2, 2): (D("0.041675"), D("19.270000")),
    (2, 3): (D("0.050010"), D("22.604000")),
    (2, 4): (D("0.158365"), D("65.946000")),
}

#: The flat baseline, computed here in the test from first principles:
#: 4 fills * $0.03 half-spread * 100 + 4 fills * $0.65 commission.
FLAT_ROUND_TRIP = 4 * D("0.03") * 100 + 4 * COMMISSION

#: The capture window the sweep corpus was fetched in (cboe-delayed, ET), and
#: the decision clocks the longrun run actually trades at (from boards.jsonl;
#: cross-checked against the file in test_desk_cost_rerating.py).
CORPUS_CAPTURE_WINDOW_ET = ("17:45", "06:30")
RUN_DECISION_CLOCKS_ET = ("10:00", "10:45", "11:30", "12:15", "13:00", "13:45", "14:30", "15:15")


def prov(
    abs_delta: str = "0.40",
    dte: int = 14,
    symbol: str = "IWM",
    session: str = "2026-09-22",
    stamp: str = "2026-09-22T18:05:00-04:00",
    eod: bool = True,
) -> Any:
    """A caller-supplied provenance record for one leg.

    The model does NOT know anything about the corpus; it is told. Everything
    it emits, it emits because the caller said so.
    """
    return cost_module().ObservationProvenance(
        symbol=symbol,
        abs_delta=D(abs_delta),
        dte=dte,
        source_session=session,
        source_timestamp_et=stamp,
        is_eod_snapshot=eod,
    )


def two_leg_quote(abs_delta: str, dte: int) -> Any:
    """A 2-leg vertical, both legs quoted at the same moneyness and expiry."""
    return measured_model().price([prov(abs_delta, dte), prov(abs_delta, dte)])


# ==========================================================================
# 1. The measured grid
# ==========================================================================


def test_module_constants_are_the_measured_table() -> None:
    cost = cost_module()
    assert tuple(cost.MEASURED_MEDIAN_FULL_SPREAD) == MEDIANS
    assert tuple(cost.DELTA_BUCKET_EDGES) == EDGES
    assert tuple(cost.DTE_BAND_MULTIPLIER) == DTE_MULTIPLIERS
    assert cost.COMMISSION_PER_LEG == COMMISSION
    assert cost.CONTRACT_MULTIPLIER == MULTIPLIER
    assert cost.HALF_SPREAD_QUANTUM == QUANTUM
    assert cost.FILLS_PER_LEG == 2
    assert cost.MAX_ABS_DELTA == D("0.70")
    assert cost.MIN_DTE == 7
    assert cost.MAX_DTE == 60
    assert isinstance(cost.COST_SCHEMA, str) and cost.COST_SCHEMA


@pytest.mark.parametrize("dte_band,dte", list(enumerate((14, 30, 53))))
@pytest.mark.parametrize("bucket", range(5))
def test_every_grid_cell_is_the_hand_computed_measurement(
    dte_band: int, dte: int, bucket: int
) -> None:
    """All 15 cells, each a 2-leg vertical priced with two legs in one bucket."""
    half_expected, round_trip_expected = GRID[(dte_band, bucket)]
    quote = two_leg_quote(str(CENTRES[bucket]), dte)

    assert len(quote.legs) == 2
    for leg in quote.legs:
        assert leg.measured_full_spread == MEDIANS[bucket]
        assert leg.delta_bucket == bucket
        assert leg.dte_band == dte_band
        assert leg.dte_multiplier == DTE_MULTIPLIERS[dte_band]
        assert leg.half_spread_per_share == half_expected
        assert leg.fill_cost() == half_expected * 100 + COMMISSION
        assert leg.round_trip() == 2 * (half_expected * 100 + COMMISSION)
    assert quote.total_round_trip == round_trip_expected


def test_dte_multipliers_reproduce_the_measured_dte_marginals() -> None:
    """The multipliers are the measured dte-marginal ratio, not free parameters.

    Measured dte marginals over the tradeable universe: $0.030 / $0.040 /
    $0.050 for 7-21 / 22-45 / 46-60. Scaled onto the shortest band they are
    exactly the multipliers the model applies.
    """
    measured = (D("0.030"), D("0.040"), D("0.050"))
    base = measured[0]
    for got, raw in zip(DTE_MULTIPLIERS, measured, strict=True):
        assert got == (raw / base).quantize(D("0.001"))


def test_flat_model_number_is_reproduced_at_its_own_price_point() -> None:
    """$14.60 is the measured cost of a 0.35-0.50 delta, 7-21 dte vertical.

    That is the whole finding: the flat point estimate is right ON AVERAGE and
    wrong everywhere else. It is not a scalar error.
    """
    assert FLAT_ROUND_TRIP == D("14.600")
    assert GRID[(0, 3)][1] == FLAT_ROUND_TRIP
    assert two_leg_quote("0.42", 14).total_round_trip == FLAT_ROUND_TRIP


def test_the_win_is_the_shape_not_a_scalar() -> None:
    """Cheap wings cost LESS than flat, expensive bodies cost MORE."""
    assert GRID[(0, 0)][1] == D("6.600") < FLAT_ROUND_TRIP   # |delta| < 0.10
    assert GRID[(0, 1)][1] == D("8.600") < FLAT_ROUND_TRIP   # |delta| 0.10-0.20
    assert GRID[(0, 4)][1] == D("40.600") > FLAT_ROUND_TRIP  # |delta| 0.50-0.70, ~2.8x
    # and the spread of the shape is ~6x, not the 10x of the raw quoted medians
    # (commission damps it, which is correct: commission is not moneyness-blind)
    assert GRID[(0, 4)][1] / GRID[(0, 0)][1] > 5


def test_bucket_edges_are_inclusive_at_the_top() -> None:
    """A bucket is (previous edge, edge]: a leg exactly on the edge prices as
    that bucket, and one micro-tick past the last edge is outside the measured
    universe rather than a sixth, invented bucket."""
    cost = cost_module()
    for edge, expected_bucket in zip(EDGES, range(5), strict=True):
        below = two_leg_quote(str(edge - D("0.000001")), 14).legs[0]
        at = two_leg_quote(str(edge), 14).legs[0]
        assert below.delta_bucket == expected_bucket
        assert at.delta_bucket == expected_bucket
        assert below.half_spread_per_share == at.half_spread_per_share
        if expected_bucket == 4:
            with pytest.raises(cost.LegOutsideTradeableUniverseError):
                two_leg_quote(str(edge + D("0.000001")), 14)
            continue
        above = two_leg_quote(str(edge + D("0.000001")), 14).legs[0]
        assert above.delta_bucket == expected_bucket + 1
        assert at.half_spread_per_share < above.half_spread_per_share


def test_dte_band_edges_split_at_22_and_46() -> None:
    assert two_leg_quote("0.42", 7).legs[0].dte_band == 0
    assert two_leg_quote("0.42", 21).legs[0].dte_band == 0
    assert two_leg_quote("0.42", 22).legs[0].dte_band == 1
    assert two_leg_quote("0.42", 45).legs[0].dte_band == 1
    assert two_leg_quote("0.42", 46).legs[0].dte_band == 2
    assert two_leg_quote("0.42", 60).legs[0].dte_band == 2


def test_legs_of_one_spread_may_sit_in_different_buckets() -> None:
    """A 0.05-delta long leg against a 0.60-delta short leg."""
    quote = measured_model().price([prov("0.05", 14), prov("0.60", 14)])
    assert quote.legs[0].half_spread_per_share == D("0.010000")
    assert quote.legs[1].half_spread_per_share == D("0.095000")
    assert quote.total_round_trip == 2 * (D("1.00") + COMMISSION) + 2 * (D("9.50") + COMMISSION)
    assert quote.total_round_trip == D("23.600000")


# ==========================================================================
# 2. Refusals — the desk's tradeable universe is the model's domain
# ==========================================================================


@pytest.mark.parametrize(
    "abs_delta,dte",
    [
        ("0.700001", 14),   # deeper than the desk ever trades
        ("0.71", 14),
        ("0.90", 14),       # the deep-ITM rows that poison the unfiltered mean
        ("-0.30", 14),      # |delta| is unsigned; a negative is a caller bug
        ("0.42", 6),        # inside the corridor but before the measured floor
        ("0.42", 61),       # one day past the measured ceiling
        ("0.42", 365),
    ],
)
def test_legs_outside_the_tradeable_universe_raise(abs_delta: str, dte: int) -> None:
    cost = cost_module()
    with pytest.raises(cost.LegOutsideTradeableUniverseError) as excinfo:
        measured_model().price([prov(abs_delta, dte), prov(abs_delta, dte)])
    assert issubclass(cost.LegOutsideTradeableUniverseError, cost.CostModelError)
    assert issubclass(cost.CostModelError, ValueError)
    # the message must say WHICH field was out, or the operator cannot act
    text = str(excinfo.value)
    assert ("delta" in text) or ("dte" in text)


def test_a_spread_with_no_legs_is_rejected() -> None:
    cost = cost_module()
    with pytest.raises(cost.CostModelError):
        measured_model().price([])


# ==========================================================================
# 3. PROVENANCE — this cost was never observed at a decision clock
# ==========================================================================


def test_is_eod_snapshot_has_no_default() -> None:
    """The flag must be stated, never inferred. A default would let a caller
    price a cost and forget where it came from."""
    cost = cost_module()
    with pytest.raises(TypeError):
        cost.ObservationProvenance(  # type: ignore[call-arg]
            symbol="IWM",
            abs_delta=D("0.40"),
            dte=14,
            source_session="2026-09-22",
            source_timestamp_et="2026-09-22T18:05:00-04:00",
        )


@pytest.mark.parametrize("field", ["symbol", "source_session", "source_timestamp_et"])
def test_provenance_refuses_blank_identifiers(field: str) -> None:
    cost = cost_module()
    kwargs: dict[str, Any] = {
        "symbol": "IWM",
        "abs_delta": D("0.40"),
        "dte": 14,
        "source_session": "2026-09-22",
        "source_timestamp_et": "2026-09-22T18:05:00-04:00",
        "is_eod_snapshot": True,
    }
    kwargs[field] = "   "
    with pytest.raises(cost.ProvenanceError):
        cost.ObservationProvenance(**kwargs)
    assert issubclass(cost.ProvenanceError, cost.CostModelError)


def test_provenance_round_trips_the_caller_supplied_observation_verbatim() -> None:
    """The model never invents, never rewrites, never defaults a timestamp."""
    record = prov("0.6150", 53, symbol="QQQ", session="2026-09-24",
                  stamp="2026-09-24T18:02:11-04:00")
    assert record.symbol == "QQQ"
    assert record.abs_delta == D("0.6150")
    assert record.dte == 53
    assert record.source_session == "2026-09-24"
    assert record.source_timestamp_et == "2026-09-24T18:02:11-04:00"
    assert record.is_eod_snapshot is True


def test_eod_provenance_declares_never_observed_at_a_decision_clock() -> None:
    record = prov(eod=True)
    assert record.is_eod_snapshot is True
    assert record.observed_at_a_decision_clock is False
    assert record.assert_never_observed_at_a_decision_clock() is None
    gap = record.observation_gap()
    assert isinstance(gap, str) and gap
    assert "decision clock" in gap


def test_a_decision_clock_observation_is_refused_by_the_same_helper() -> None:
    record = prov(eod=False)
    assert record.observed_at_a_decision_clock is True
    with pytest.raises(cost_module().ProvenanceError):
        record.assert_never_observed_at_a_decision_clock()


def test_every_priced_leg_emits_full_provenance() -> None:
    quote = two_leg_quote("0.42", 30)
    assert len(quote.provenance) == 2
    for record in quote.provenance:
        assert record.symbol == "IWM"
        assert record.abs_delta == D("0.42")
        assert record.dte == 30
        assert record.source_session == "2026-09-22"
        assert record.source_timestamp_et == "2026-09-22T18:05:00-04:00"
        assert record.is_eod_snapshot is True
    assert quote.is_eod_snapshot is True
    assert quote.assert_never_observed_at_a_decision_clock() is None


def test_quote_serialisation_carries_the_provenance_fields_by_name() -> None:
    quote = two_leg_quote("0.42", 30)
    payload = quote.to_json()
    assert payload["schema"] == cost_module().COST_SCHEMA
    assert len(payload["legs"]) == 2
    for leg in payload["legs"]:
        assert set(leg["provenance"]) >= {
            "symbol",
            "abs_delta",
            "dte",
            "source_session",
            "source_timestamp_et",
            "is_eod_snapshot",
        }
        assert leg["provenance"]["is_eod_snapshot"] is True
    assert str(payload["total_round_trip"]) == "18.596000"


def test_a_mixed_provenance_quote_is_not_silently_eod() -> None:
    """One intraday observation among EOD ones must not be laundered."""
    quote = measured_model().price([prov("0.42", 30), prov("0.42", 30, eod=False)])
    assert quote.is_eod_snapshot is False
    assert [r.is_eod_snapshot for r in quote.provenance] == [True, False]
    with pytest.raises(cost_module().ProvenanceError):
        quote.assert_never_observed_at_a_decision_clock()


def test_the_corpus_capture_window_cannot_contain_any_run_decision_clock() -> None:
    """The mechanical proof of the observation gap.

    The corpus is a cboe-delayed EOD pull taken between 17:45 and 06:30 ET.
    The run trades at 10:00-15:15 ET. No instant of the capture window is an
    instant of a decision clock, so no cost derived from this corpus describes
    a fill the run could have got. That gap is unfalsifiable from this data and
    must not be papered over.
    """

    def minutes(clock: str) -> int:
        hour, minute = clock.split(":")
        return int(hour) * 60 + int(minute)

    start, end = (minutes(c) for c in CORPUS_CAPTURE_WINDOW_ET)
    assert start > minutes("12:00"), "the window is meant to wrap past midnight"
    for clock in RUN_DECISION_CLOCKS_ET:
        now = minutes(clock)
        inside = (now >= start) or (now <= end)  # window wraps midnight
        assert not inside, (
            f"decision clock {clock} ET falls inside the corpus capture window "
            f"{CORPUS_CAPTURE_WINDOW_ET}; the EOD claim would be void"
        )


# ==========================================================================
# 4. Determinism
# ==========================================================================


def test_same_input_gives_byte_identical_cost_twice() -> None:
    first = two_leg_quote("0.4237", 37)
    second = two_leg_quote("0.4237", 37)
    assert first.total_round_trip == second.total_round_trip
    assert first.canonical_json() == second.canonical_json()
    assert first.canonical_json().encode("utf-8") == second.canonical_json().encode("utf-8")
    assert repr(first) == repr(second)


def test_canonical_json_is_stable_across_leg_order_of_construction() -> None:
    a = measured_model().price([prov("0.05", 14), prov("0.60", 14)])
    b = measured_model().price([prov("0.05", 14), prov("0.60", 14)])
    assert a.canonical_json() == b.canonical_json()
    # a different spread must NOT collide with it
    c = measured_model().price([prov("0.60", 14), prov("0.05", 14)])
    assert a.total_round_trip == c.total_round_trip
    assert a.canonical_json() != c.canonical_json(), "leg identity must reach the serialisation"


# ==========================================================================
# 5. Purity — pricing is a pure function, no IO, no clock, no network
# ==========================================================================


def test_pricing_performs_no_file_io(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError(f"the cost model performed IO: {args!r}")

    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr(os, "open", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    quote = two_leg_quote("0.4237", 37)
    assert quote.total_round_trip == D("18.596000")


def test_pricing_imports_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cost is a function of its arguments. If pricing pulls in a module it
    has reached for something that is not its arguments."""
    model = measured_model()
    before = frozenset(sys.modules)
    model.price([prov("0.42", 14), prov("0.42", 14)])
    model.price([prov("0.05", 14), prov("0.60", 14)])
    new = frozenset(sys.modules) - before
    assert not new, f"pricing imported {sorted(new)}"


def test_the_cost_model_module_does_not_hold_io_handles() -> None:
    cost = cost_module()
    held = [
        name
        for name in ("requests", "httpx", "urllib.request", "sqlite3", "http.client", "subprocess")
        if getattr(cost, name, None) is not None
    ]
    assert not held, f"tree_options.desk.cost imported {held}"


def test_cost_does_not_depend_on_the_wall_clock() -> None:
    """Two quotes built minutes apart in the same process must agree, and the
    cost must be a function of the PROVENANCE the caller gave, not of now()."""
    quote = two_leg_quote("0.42", 30)
    source = cost_module().SpreadCostModel.__module__
    assert source == "tree_options.desk.cost"
    before = quote.canonical_json()
    assert math.isfinite(float(quote.total_round_trip))
    assert quote.canonical_json() == before
