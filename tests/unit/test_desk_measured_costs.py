"""desk.cost: the MEASURED execution-cost model, tests first.

This file is the contract. It is written before the module exists, so the
whole file is RED; the RED is the deliverable, not an accident.

Why the flat model is being replaced
------------------------------------
``outcomes.CostModel`` charges every leg a ``$0.06`` two-sided quote
(``half_spread_per_share = Decimal("0.03")``), so a 2-leg vertical always
costs ``$14.60``. Measured against the only real bid/ask on disk
(``/home/alexk/.local/state/trex-strategy-sweep-20260930/chains.json``,
612,371 rows from 184 CBOE chain files), the quoted spread varies ~10x with
moneyness. The POINT ESTIMATE is roughly right; the PER-LEG SHAPE is wrong,
and a strategy that systematically trades one moneyness is mis-costed in one
direction or the other. The win here is the SHAPE, not a bigger scalar.

The one-line finding this file exists to pin: the flat ``$14.60`` is EXACTLY
the cell (|delta| 0.35-0.50, dte 7-21). The flat constant is one arbitrary
cell of the measured surface, silently applied to all fifteen.

The oracle (hand-transcribed; n = 18,783 over symbol in IWM/QQQ/SPY,
7 <= dte <= 60, volume > 0, oi > 0, |delta| <= 0.70):

    MEASURED median FULL two-sided quoted spread ($/share), by |delta|
      0.00-0.10  n= 8,504  $0.020      0.35-0.50  n= 2,223  $0.060
      0.10-0.20  n= 2,717  $0.030      0.50-0.70  n= 2,616  $0.190
      0.20-0.35  n= 2,723  $0.050
    MEASURED dte marginal (|delta| <= 0.70)
      7-21  n= 9,568  $0.030
      22-45 n= 7,019  $0.040
      46-60 n= 2,196  $0.050

UNIT: HALF-SPREAD PER SHARE PER FILL. The desk pays half the quoted spread
on each fill, so a cell's half is ``median_full / 2``: 0.010, 0.015, 0.025,
0.030, 0.095. The dte multipliers are the measured dte-marginal ratio
(0.030 : 0.040 : 0.050), pinned to 3 dp.

The arithmetic is inherited unchanged from the model being replaced: every
leg fills twice, so a 2-leg package is 4 fills:
``4 * half * 100 + 4 * 0.65``.

TEST CONTRACT B -- the NO_PRICE GATE
------------------------------------
A measurement gap is NOT a cheap fill. Every unknown ``(symbol, |delta|,
dte)`` must raise a TYPED refusal that drops the board from scoring, and
the drop must be COUNTED and REPORTED. The single forbidden behaviour is a
``dict.get(key, default)``: one scalar default silently reverts the whole
exercise to the flattering constant, and the desk never finds out.

The model is pure and deterministic: no network, no file IO, no clock.
"""

from __future__ import annotations

import ast
import inspect
from decimal import Decimal
from typing import Any

import pytest

# The model does not exist yet. Import it in a way that turns "absent" into
# a FAILED test carrying the reason, never a silent skip: a skip would let a
# missing model pass the gate.
try:  # pragma: no cover - the RED path is the point
    from tree_options.desk import cost
    _IMPORT_ERROR: Exception | None = None
except Exception as exc:  # any import failure is the RED
    cost = None  # type: ignore[assignment]
    _IMPORT_ERROR = exc


def mc() -> Any:
    """The module under test, or a FAILURE that names why it is absent."""
    if cost is None:
        pytest.fail(
            "tree_options.desk.cost does not exist yet "
            f"(import error: {_IMPORT_ERROR!r}). The measured cost model is "
            "the deliverable these tests demand; until it exists they must "
            "fail, not skip."
        )
    return cost


# ------------------------------------------------------------------ fixtures
#
# The published marginals, hand-transcribed from the corpus. Values are
# HALF-spreads per share per fill (median full spread / 2) where the desk
# pays half the quote on each fill.
ORACLE_DELTA_MARGINAL: dict[str, Decimal] = {
    "0.00-0.10": Decimal("0.010"),
    "0.10-0.20": Decimal("0.015"),
    "0.20-0.35": Decimal("0.025"),
    "0.35-0.50": Decimal("0.030"),
    "0.50-0.70": Decimal("0.095"),
}
#: The measured dte MARGINALS (full two-sided), hand-transcribed.
ORACLE_DTE_MARGINAL: dict[str, Decimal] = {
    "7-21": Decimal("0.030"),
    "22-45": Decimal("0.040"),
    "46-60": Decimal("0.050"),
}

#: one interior |delta| per |delta| band, and one day-count per dte band
DELTA_REPS_ALL = ("0.05", "0.15", "0.275", "0.425", "0.60")
DTE_DAY_REPS = (14, 30, 53)


def measured_model() -> Any:
    return mc().SpreadCostModel.measured()


def leg(abs_delta: str | None = "0.05", symbol: str = "SPY", dte: int = 14) -> Any:
    """A caller-supplied observation record. The model is TOLD, never guesses."""
    return mc().Leg(
        symbol=symbol,
        abs_delta=None if abs_delta is None else Decimal(abs_delta),
        dte=dte,
        source_session="2026-06-01",
        source_timestamp_et="2026-06-01T18:05:00-04:00",
        is_eod_snapshot=True,
    )


def provenance() -> Any:
    m = mc()
    return m.CostProvenance(
        source="cboe-delayed-eod-chains",
        snapshot_window_et="17:45-06:30",
        universe_filter="|delta|<=0.70, 7<=dte<=60, volume>0, oi>0, symbol in IWM/QQQ/SPY",
        n_rows=18783,
        decision_clocks_et=("10:00", "10:15", "15:15"),
    )


# ------------------------------------------------------- universe and bucketing


def test_delta_buckets_are_the_five_measured_edges_right_closed() -> None:
    """0.10 belongs to the FIRST bucket: the corpus bands are (lo, hi].

    Right-closed is the only convention that reproduces the measured
    per-bucket row counts 8,504 / 2,717 / 2,723 / 2,223 / 2,616 exactly.
    Equivalently ``band = count(edges strictly less than abs_delta)``.
    """
    bucket = mc().delta_bucket
    assert bucket(Decimal("0")) == "0.00-0.10"
    assert bucket(Decimal("0.0099")) == "0.00-0.10"
    assert bucket(Decimal("0.09999")) == "0.00-0.10"
    assert bucket(Decimal("0.10")) == "0.00-0.10"      # upper edge of band 1 is INCLUSIVE
    assert bucket(Decimal("0.10001")) == "0.10-0.20"
    assert bucket(Decimal("0.19999")) == "0.10-0.20"
    assert bucket(Decimal("0.20")) == "0.10-0.20"
    assert bucket(Decimal("0.20001")) == "0.20-0.35"
    assert bucket(Decimal("0.34999")) == "0.20-0.35"
    assert bucket(Decimal("0.35")) == "0.20-0.35"
    assert bucket(Decimal("0.35001")) == "0.35-0.50"
    assert bucket(Decimal("0.49999")) == "0.35-0.50"
    assert bucket(Decimal("0.50")) == "0.35-0.50"
    assert bucket(Decimal("0.50001")) == "0.50-0.70"
    assert bucket(Decimal("0.69999")) == "0.50-0.70"
    assert bucket(Decimal("0.70")) == "0.50-0.70"      # the ceiling itself IS tradeable


def test_a_leg_outside_the_measured_universe_has_no_bucket() -> None:
    """|delta| > 0.70 was EXCLUDED from the corpus on purpose: the deep-ITM
    rows that dominate the raw mean are not the desk's tradeable universe.
    A bucket for them would be an invention, so there is none."""
    with pytest.raises(ValueError):
        mc().delta_bucket(Decimal("0.70001"))
    with pytest.raises(ValueError):
        mc().delta_bucket(Decimal("0.93"))


def test_dte_buckets_are_the_three_measured_edges_inclusive() -> None:
    bucket = mc().dte_bucket
    assert bucket(7) == "7-21" and bucket(21) == "7-21"
    assert bucket(22) == "22-45" and bucket(45) == "22-45"
    assert bucket(46) == "46-60" and bucket(60) == "46-60"


def test_dte_outside_seven_to_sixty_has_no_bucket() -> None:
    for dte in (0, 6, 61, 200):
        with pytest.raises(ValueError):
            mc().dte_bucket(dte)


def test_the_tradeable_symbols_are_exactly_the_three_measured() -> None:
    assert mc().TRADEABLE_SYMBOLS == frozenset({"IWM", "QQQ", "SPY"})


# ----------------------------------------------------------------- the oracle


def test_the_shipped_marginals_are_the_measured_ones_exactly() -> None:
    """The five |delta| medians and the three dte medians, hand-transcribed
    from the corpus. These are the numbers the whole model is calibrated on;
    if the implementation drifts here, the shape is wrong.

    The model has NO table object: the marginals are module constants and the
    surface is ``SpreadCostModel.half_spread_per_share(abs_delta, dte)``.
    """
    assert dict(zip(("0.00-0.10", "0.10-0.20", "0.20-0.35", "0.35-0.50", "0.50-0.70"),
                    mc().MEASURED_MEDIAN_FULL_SPREAD, strict=True)) \
        == {k: v * 2 for k, v in ORACLE_DELTA_MARGINAL.items()}
    # the dte multipliers ARE the measured dte-marginal ratio, at 3 dp
    for band, raw in zip(("7-21", "22-45", "46-60"), ORACLE_DTE_MARGINAL.values(),
                         strict=True):
        assert band  # readability
        expected = (raw / ORACLE_DTE_MARGINAL["7-21"]).quantize(Decimal("0.001"))
        assert mc().DTE_BAND_MULTIPLIER[("7-21", "22-45", "46-60").index(band)] == expected


def test_every_one_of_the_fifteen_derived_cells_is_pinned() -> None:
    """All 15 cells, as (half_spread_per_share, two-leg round trip).

    ``round_trip(2 legs) = 4 * half * 100 + 4 * 0.65``. Hand-written: the
    implementation is never asked what the answer should be.

    These cells are DERIVED from the two marginals, not measured jointly --
    the corpus publishes no joint. The construction reproduces the |delta|
    marginals exactly at dte 7-21 and the dte ratio exactly; the joint is an
    approximation and the module docstring says so.
    """
    cells = {
        (0, 0): (Decimal("0.010000"), Decimal("6.600000")),
        (0, 1): (Decimal("0.015000"), Decimal("8.600000")),
        (0, 2): (Decimal("0.025000"), Decimal("12.600000")),
        (0, 3): (Decimal("0.030000"), Decimal("14.600000")),  # <- the flat model
        (0, 4): (Decimal("0.095000"), Decimal("40.600000")),
        (1, 0): (Decimal("0.013330"), Decimal("7.932000")),
        (1, 1): (Decimal("0.019995"), Decimal("10.598000")),
        (1, 2): (Decimal("0.033325"), Decimal("15.930000")),
        (1, 3): (Decimal("0.039990"), Decimal("18.596000")),
        (1, 4): (Decimal("0.126635"), Decimal("53.254000")),
        (2, 0): (Decimal("0.016670"), Decimal("9.268000")),
        (2, 1): (Decimal("0.025005"), Decimal("12.602000")),
        (2, 2): (Decimal("0.041675"), Decimal("19.270000")),
        (2, 3): (Decimal("0.050010"), Decimal("22.604000")),
        (2, 4): (Decimal("0.158365"), Decimal("65.946000")),
    }
    assert len(cells) == 15, "the measured surface is 5 |delta| bands x 3 dte bands"
    model = measured_model()
    for (band, bucket), (half, trip) in cells.items():
        abs_delta = Decimal(DELTA_REPS_ALL[bucket])
        dte = DTE_DAY_REPS[band]
        assert model.half_spread_per_share(abs_delta, dte) == half, f"band {band} bucket {bucket}"
        quote = model.price([leg(abs_delta=str(abs_delta), dte=dte)] * 2)
        assert quote.total_round_trip == trip, f"band {band} bucket {bucket}"


# ------------------------------------------------------------ the shape wins


def test_round_trip_arithmetic_is_hand_derived_and_unchanged() -> None:
    """fills = 2 per leg, cost = fills * half * 100 + fills * 0.65.

      2 legs @ |delta| 0.05 : 4 * 0.010 * 100 = 4.00, 4 * 0.65 = 2.60 ->  6.60
      2 legs @ |delta| 0.25 : 4 * 0.025 * 100 = 10.00 + 2.60         -> 12.60
      1 leg  @ |delta| 0.60 : 2 * 0.095 * 100 = 19.00 + 1.30         -> 20.30
      2 mixed legs (0.05, 0.60) : 2*0.010*100 + 2*0.095*100 = 2.00 + 19.00 = 21.00 + 2.60 -> 23.60
    """
    model = measured_model()
    assert model.round_trip([leg("0.05"), leg("0.05")]) == Decimal("6.600000")
    assert model.round_trip([leg("0.25"), leg("0.25")]) == Decimal("12.600000")
    assert model.round_trip([leg("0.60")]) == Decimal("20.300000")
    assert model.round_trip([leg("0.05"), leg("0.60")]) == Decimal("23.600000")


def test_the_quote_splits_spread_from_commission_and_sums_exactly() -> None:
    quote = measured_model().price([leg("0.05"), leg("0.05")])
    assert quote.spread_total == Decimal("4.00")
    assert quote.commission_total == Decimal("2.60")
    assert quote.total_round_trip == quote.spread_total + quote.commission_total
    assert quote.total_round_trip == Decimal("6.600000")
    assert len(quote.legs) == 2


def test_the_shape_is_the_win_not_the_scalar() -> None:
    """Cheap wings must come in UNDER the old flat $14.60 and expensive
    near-the-money money must come in OVER it. If both land on 14.60 the
    model has silently reverted to the constant it was written to replace."""
    model = measured_model()
    flat = Decimal("14.60")            # outcomes.CostModel().round_trip()
    cheap = model.round_trip([leg("0.05"), leg("0.05")])
    dear = model.round_trip([leg("0.60"), leg("0.60")])
    assert cheap < flat < dear
    assert dear > 2 * flat
    # monotone in |delta| at fixed everything else
    ladder = [model.round_trip([leg(d), leg(d)])
              for d in ("0.05", "0.25", "0.60")]
    assert ladder == sorted(ladder), f"cost must not fall as |delta| rises: {ladder}"


def test_the_flat_constant_is_exactly_one_cell_of_the_measured_surface() -> None:
    """|delta| 0.35-0.50 at dte 7-21 prices at $14.60 -- to the cent.

    That is the whole finding. The flat model is not a bigger or smaller
    constant than the measurement; it is one arbitrary cell of a 15-cell
    surface, applied to all fifteen.
    """
    model = measured_model()
    assert model.round_trip([leg("0.42"), leg("0.42")]) == Decimal("14.600000")
    assert model.round_trip([leg("0.42"), leg("0.42")]) != model.round_trip(
        [leg("0.05"), leg("0.05")])


def test_the_model_exposes_no_zero_argument_half_spread() -> None:
    """The method is ``half_spread_per_share(abs_delta, dte)`` and takes BOTH.

    A zero-argument form would be the flat constant wearing a new name, and
    that is the one thing this model must not become. The flat model keeps
    its own ``half_spread_per_share`` Decimal FIELD (frozen separately by
    ``test_the_flat_cost_model_is_untouched`` in test_desk_cost_shape.py).
    """
    import inspect as _inspect

    signature = _inspect.signature(mc().SpreadCostModel.half_spread_per_share)
    required = [name for name, p in signature.parameters.items()
                if p.default is p.empty and name != "self"]
    assert required == ["abs_delta", "dte"], required
    assert not hasattr(measured_model(), "default_half_spread")


# ------------------------------------------------- TEST CONTRACT B: NO_PRICE


def test_an_unmeasured_symbol_is_refused_not_defaulted() -> None:
    with pytest.raises(mc().UnpricedCostError) as excinfo:
        measured_model().round_trip([leg("0.05", symbol="XLF")])
    assert excinfo.value.reason == "unknown_symbol"
    assert excinfo.value.key.symbol == "XLF"


def test_a_leg_with_no_delta_is_refused_not_defaulted() -> None:
    """Board rows carry no delta today. A missing delta is a MEASUREMENT GAP,
    never a reason to assume a cheap leg."""
    with pytest.raises(mc().UnpricedCostError) as excinfo:
        measured_model().round_trip([leg(None)])
    assert excinfo.value.reason == "no_delta"
    assert excinfo.value.key.abs_delta is None


def test_a_leg_outside_the_tradeable_universe_is_refused() -> None:
    with pytest.raises(mc().UnpricedCostError) as excinfo:
        measured_model().round_trip([leg("0.90")])
    assert excinfo.value.reason == "delta_out_of_universe"


def test_dte_outside_the_measured_window_is_refused() -> None:
    with pytest.raises(mc().UnpricedCostError) as excinfo:
        measured_model().round_trip([leg("0.05", dte=61)])
    assert excinfo.value.reason == "dte_out_of_universe"


def test_one_unpriceable_leg_refuses_the_whole_package() -> None:
    """A spread is only as priced as its worse leg. Pricing the cheap leg and
    charging for it alone is exactly the flattering error this gate exists to
    prevent."""
    with pytest.raises(mc().UnpricedCostError) as excinfo:
        measured_model().round_trip([leg("0.05"), leg("0.05", symbol="XLF")])
    assert excinfo.value.key.symbol == "XLF"


def test_an_empty_package_is_refused_not_priced_free() -> None:
    """Zero legs is not a zero-cost trade; it is a bug that would score as a
    perfect fill."""
    with pytest.raises(mc().UnpricedCostError) as excinfo:
        measured_model().round_trip([])
    assert excinfo.value.reason == "no_legs"


def test_the_refusal_is_a_typed_exception_carrying_its_key() -> None:
    """It subclasses ValueError so the CLI's existing
    ``except (ValueError, ArithmeticError, OSError, KeyError)`` catches a
    measurement gap instead of letting it escape as a traceback."""
    err = mc().UnpricedCostError
    assert issubclass(err, ValueError), "call sites already catch ValueError"
    assert issubclass(err, mc().CostModelError)
    with pytest.raises(err) as excinfo:
        measured_model().price([leg("0.05", symbol="QQQ", dte=3)])
    payload = excinfo.value.as_dict()
    assert payload["reason"] == "dte_out_of_universe"
    assert payload["key"]["symbol"] == "QQQ"
    assert payload["key"]["dte"] == 3


def test_no_entry_point_returns_a_scalar_for_an_unknown_key() -> None:
    """Every public pricing surface must REFUSE, not answer. A single one that
    returns a number is the hole the whole exercise falls through -- and it
    would answer with a plausible-looking figure nobody would question."""
    model = measured_model()
    surfaces = {
        "round_trip": lambda: model.round_trip([leg("0.05", symbol="XLF")]),
        "price": lambda: model.price([leg("0.05", symbol="XLF")]),
        "half_spread_per_share": lambda: model.half_spread_per_share(Decimal("0.85"), 14),
    }
    answered: list[str] = []
    for name, call in surfaces.items():
        try:
            answered.append(f"{name} -> {call()!r}")
        except mc().UnpricedCostError:
            pass
    assert not answered, ("a pricing surface answered an unmeasured key instead of "
                          f"refusing: {answered}")


def test_a_missing_quote_can_never_be_priced_as_zero_or_the_old_default() -> None:
    """A measurement gap is not a cheap fill. Every MEASURED cell is strictly
    positive, and the value being replaced -- a $0.03 half-spread per leg --
    survives nowhere as a scalar a caller can reach for."""
    model = measured_model()
    cells = [model.half_spread_per_share(Decimal(d), 14)
             for d in ("0.05", "0.15", "0.275", "0.425", "0.60")]
    assert cells, "the measured surface is empty"
    assert all(v > 0 for v in cells), [str(v) for v in cells if v <= 0]
    # the cheapest measured cell is still a real quote, not the old constant
    assert min(cells) > 0
    assert not hasattr(model, "default_half_spread")


# ------------------------------------------------- commission stays a constant


def test_commission_is_a_named_constant_and_is_never_folded_into_the_table() -> None:
    m = mc()
    assert m.COMMISSION_PER_LEG == Decimal("0.65")
    assert m.CONTRACT_MULTIPLIER == 100
    assert measured_model().commission_per_leg == Decimal("0.65")
    quote = measured_model().price([leg("0.05"), leg("0.60")])
    assert quote.commission_total == Decimal("0.65") * 4
    assert quote.spread_total == Decimal("2.00") + Decimal("19.00")


def test_commission_can_be_changed_without_touching_the_spread_table() -> None:
    """They are separate knobs. A changed commission must move the commission
    line only, never the measured spread."""
    base = measured_model()
    other = mc().SpreadCostModel(commission_per_leg=Decimal("1.00"))
    legs_ = [leg("0.05"), leg("0.60")]
    assert other.price(legs_).spread_total == base.price(legs_).spread_total == Decimal("21.00")
    assert other.price(legs_).commission_total == Decimal("4.00")
    assert other.price(legs_).total_round_trip == Decimal("25.00")


# ------------------------------------------------------------------ provenance


def test_every_quote_carries_provenance_and_says_it_is_not_a_fill_clock() -> None:
    """These are EOD CBOE-delayed snapshots fetched 17:45-06:30 ET. The run
    trades on 10:00-15:15 ET decision clocks. A snapshot at >= 16:00 ET
    cannot describe a 10:00 ET fill, and that gap is unfalsifiable from this
    data, so the cost MUST SAY SO rather than paper over it."""
    payload = provenance().as_dict()
    assert payload["source"] == "cboe-delayed-eod-chains"
    assert payload["snapshot_window_et"] == "17:45-06:30"
    assert payload["n_rows"] == 18783
    assert "0.70" in payload["universe_filter"] and "dte" in payload["universe_filter"]
    # the disclosure, not a claim of clock-matched measurement
    assert payload["describes_fill_clock"] is False
    clocks = payload["decision_clocks_et"]
    assert "10:00" in clocks and "15:15" in clocks
    assert max(int(c.split(":")[0]) for c in clocks) < 16, clocks
    gap = payload["gap"]
    assert "17:45-06:30" in gap and "10:00" in gap


def test_the_cost_block_quotes_the_single_provenance_authority() -> None:
    """``outcomes.py`` once hand-copied this record into its own constructor
    and the universe_filter wording drifted from the digest's provenance for
    the SAME corpus -- two descriptions of one measurement. The block a
    digest actually carries must equal ``cost.py``'s ``measured_corpus()``,
    the declared single authority, byte for byte."""
    from tree_options.desk import cost, outcomes
    block = outcomes._costs_block(measured_model(), mc().NoPriceLedger(), True)
    assert block["provenance"] == cost.CostProvenance.measured_corpus().as_dict()


def test_provenance_survives_into_the_quote() -> None:
    """Cost that reaches a digest without its provenance is a number nobody
    can audit."""
    quote = measured_model().price([leg("0.30", dte=30)])
    assert quote.is_eod_snapshot is True
    payload = quote.to_json()
    assert payload["schema"] == mc().COST_SCHEMA
    assert str(payload["total_round_trip"]) == str(quote.total_round_trip)
    assert payload["legs"][0]["provenance"] == leg("0.30", dte=30).to_json()


# ------------------------------------------ TEST CONTRACT B: no default in src


def test_the_implementation_contains_no_mapping_default_that_could_mask_a_gap() -> None:
    """A single ``dict.get(key, default)`` silently reverts the model to the
    flattering number. The source is scanned, not the behaviour, because the
    behaviour is only observable for the keys this suite happens to try.

    The scan covers the PRICING surface only. ``NoPriceLedger`` counts refusals
    and legitimately reaches for a zero-initialized counter; forbidding a
    default there would be forbidding the very bookkeeping this gate demands.
    """
    source = inspect.getsourcefile(mc())
    assert source and source.endswith(".py")
    with open(source, encoding="utf-8") as handle:
        text = handle.read()
    tree = ast.parse(text)
    scanned, skipped = _pricing_subtrees(tree)
    assert scanned, "found nothing to scan: name the pricing types at module level"
    assert "NoPriceLedger" in skipped, "the ledger must be named so it can be excluded"
    assert EXEMPT_NAMES, "the exemption list must be non-empty or it can be widened silently"

    offences: list[str] = []
    for node in ast.walk(scanned):
        if isinstance(node, ast.Call):
            func = node.func
            # dict.get(key, default) / setdefault(key, default)
            if isinstance(func, ast.Attribute) and func.attr in {"get", "setdefault"}:
                supplied = len(node.args) + len(node.keywords)
                if supplied >= 2:
                    offences.append(f"line {node.lineno}: {func.attr} with a default")
            if isinstance(func, ast.Name) and func.id == "open":
                offences.append(f"line {node.lineno}: open() -- the model must not do file IO")
        if isinstance(node, ast.IfExp) and isinstance(node.orelse, ast.Constant):
            if node.orelse.value is not None:
                offences.append(
                    f"line {node.lineno}: conditional scalar default {node.orelse.value!r}")

    assert not offences, ("the cost model must never be able to answer with a default: "
                          + "; ".join(sorted(offences)))


#: the module-level defs/classes that make up the PRICING surface. Everything
#: else (the refusal ledger) is exempt: it counts, it never answers with a cost.
PRICING_NAMES = frozenset({
    "delta_bucket", "dte_bucket", "half_spread_per_share",
    "SpreadCostModel", "Leg", "LegQuote", "SpreadQuote",
    "CostModelError", "UnpricedCostError", "ProvenanceError",
    "LegOutsideTradeableUniverseError", "CostProvenance",
})
EXEMPT_NAMES = frozenset({"NoPriceLedger"})


def _pricing_subtrees(tree: ast.Module) -> tuple[ast.AST, set[str]]:
    """The pricing defs/classes merged into one walkable tree, plus the names
    deliberately excluded."""
    picked = [node for node in tree.body
              if _name_of(node) in PRICING_NAMES]
    excluded = {_name_of(node) for node in tree.body if _name_of(node) in EXEMPT_NAMES}
    merged = ast.Module(body=picked, type_ignores=[])
    return ast.fix_missing_locations(merged), excluded


def _name_of(node: ast.stmt) -> str:
    return getattr(node, "name", "")


def test_the_implementation_is_pure_no_network_no_file_io() -> None:
    source = inspect.getsourcefile(mc())
    with open(source, encoding="utf-8") as handle:
        text = handle.read()
    banned = ("import requests", "import httpx", "import urllib", "import socket",
              "import http", "from urllib", "from http", "import os", "from os import",
              "import pathlib", "from pathlib")
    hits = [b for b in banned if b in text]
    assert not hits, f"the cost model must be pure and deterministic; found {hits}"


def test_no_network_and_no_clock_are_imported_by_the_cost_path() -> None:
    """Evidence, not authority. A model that can reach out or read the clock
    is not reproducible and its numbers cannot be audited."""
    source = inspect.getsourcefile(mc())
    with open(source, encoding="utf-8") as handle:
        text = handle.read()
    for banned in ("time.time", "datetime.now", "datetime.utcnow", "random.", "secrets."):
        assert banned not in text, f"{banned} in the cost model"
