"""Integration defects: the wiring failures an adversarial review found.

Each test here pins ONE defect that a green suite did not catch, because
the suite exercised the happy path. They are the reason this file exists and
they are deliberately narrow.

  1. ``_drop_unpriced`` computed the new decision order against the ORIGINAL
     ``order`` length, so it emitted an index that no longer existed in the
     filtered ``excess`` and the whole skill section died inside ``monitor``.
  2. ``_candidate_legs`` emitted ONE leg for a 2-leg vertical, which HALVES
     the round trip the moment a delta source lands.
  3. ``skill.MAX_COST`` still described the flat ``$14.60`` world, so the
     empirical-Bernstein bound would be tighter than the cost it exists to
     absorb.
  4. ``longrun`` built the refusal ledger and then never handed it to
     ``skill_section``, so a fully-refused run reported ZERO drops and an
     unprefixed verdict -- pixel-identical to a strategy that broke even.

ORACLE DISCIPLINE: every expected number below is hand-derived from the
documented contract or from the merged specification's table, never obtained
by calling the implementation under test. ``_drop_unpriced``'s expectation is
derived from its own docstring ("``order`` the indices of ``boards`` in
decision order") plus the fact that ``excess`` is indexed by the position of
the FILTERED list.
"""

from __future__ import annotations

import contextlib
import re
from decimal import Decimal
from typing import Any
from unittest import mock

import pytest

from tree_options.desk import cost as measured_costs
from tree_options.desk import longrun, outcomes, skill
from tree_options.desk.longrun import Board


def _leg(symbol: str = "SPY", abs_delta: str = "0.05", dte: int = 14) -> Any:
    return measured_costs.Leg(
        symbol=symbol, abs_delta=Decimal(abs_delta), dte=dte,
        source_session="2026-06-01", source_timestamp_et="2026-06-01T18:05:00-04:00",
        is_eod_snapshot=True)


# ------------------------------------------------------------------ defect 1
#
# `excess` is indexed by the position of the board in the FILTERED list, and
# `ordered` must therefore be a permutation of range(len(kept)). The old
# remap was `[order.index(i) for i in keep ...]`: each survivor's rank in
# the ORIGINAL order, gathered in TIME order. Two separate errors -- the
# gather axis is wrong (time, not decision) and the rank is taken against
# the pre-drop length, so it can exceed len(kept)-1.

_ORDER_CASES = [
    # (boards, decision order, dropped snapshot, expected remap)
    (["s1", "s2", "s3", "s4", "s5"], [2, 0, 4, 1, 3], "s2", [1, 0, 3, 2]),
    (["s1", "s2", "s3", "s4", "s5"], [4, 3, 2, 1, 0], "s1", [3, 2, 1, 0]),
    (["s1", "s2", "s3"], [0, 1, 2], "s2", [0, 1]),
    (["s1", "s2", "s3", "s4"], [1, 0, 3, 2], "s3", [1, 0, 2]),
]


def _boards(names: list[str]) -> list[Board]:
    return [Board(n, "2026-06-01", "10:00",
                  [{"id": f"{n}R", "structure": "put_credit", "underlying": "SPY"}])
            for n in names]


def _ledger_for(arm: str, snapshot: str) -> Any:
    ledger = measured_costs.NoPriceLedger()
    ledger.record(arm=arm, snapshot=snapshot, key=_leg(abs_delta="0.90"),
                  reason="delta_out_of_universe")
    return ledger


@pytest.mark.parametrize(("names", "order", "dropped", "expected"),
                         _ORDER_CASES)
def test_dropping_a_board_remaps_the_decision_order_onto_the_kept_positions(
        names: list[str], order: list[int], dropped: str, expected: list[int]) -> None:
    """The new order must be the surviving boards' decision order, as
    positions in the FILTERED list -- not ranks in the pre-drop list."""
    ledger = _ledger_for("arm-a", dropped)
    kept, _decisions, new_order, _report = skill._drop_unpriced(
        _boards(names), [(f"{n}R", "h1") for n in names], order, ledger, "arm-a")
    assert new_order == expected, (
        f"decision order {order} with {dropped!r} dropped over {names}: the remap must be "
        f"{expected} (survivors in decision order, as positions in the filtered list); "
        f"got {new_order}. A rank taken against the PRE-drop length indexes past the "
        f"end of the filtered excess array and raises inside monitor().")
    assert sorted(new_order) == list(range(len(kept))), \
        "every kept board must appear exactly once"


def test_the_remap_stays_inside_the_filtered_excess_array() -> None:
    """The concrete crash: the old remap emitted an index >= len(kept)."""
    names = ["s1", "s2", "s3", "s4", "s5"]
    order = [2, 0, 4, 1, 3]
    ledger = _ledger_for("arm-a", "s2")
    kept, _dec, new_order, _rep = skill._drop_unpriced(
        _boards(names), [(f"{n}R", "h1") for n in names], order, ledger, "arm-a")
    excess = [1.0] * len(kept)          # stands in for net.excess
    assert new_order is not None
    for i in new_order:
        assert 0 <= i < len(excess), f"index {i} is outside a {len(excess)}-element excess"


def test_arm_skill_with_a_drop_and_a_decision_order_does_not_degrade() -> None:
    """End to end: the whole section must not become ``{"status": "error"}``.

    ``longrun`` wraps ``skill_section`` in a bare ``except Exception``, so an
    IndexError from the remap is swallowed and the digest silently loses its
    entire skill section.
    """
    names = ["s1", "s2", "s3", "s4", "s5"]
    kept_names = [n for n in names if n != "s2"]
    order = [2, 0, 4, 1, 3]

    def get(snapshot: str, row_id: str, horizon: str | None) -> tuple[float, float] | None:
        if snapshot == "s2":
            return None
        gross = 10.0 + names.index(snapshot)
        return (gross, gross - 3.30)

    book = skill.ValueBook(get, ("h1",))
    ledger = _ledger_for("arm-a", "s2")
    # The FULL board list plus a decision order, exactly as `skill_section`
    # hands them over: `_drop_unpriced` does the filtering.
    doc = skill.arm_skill(
        book, _boards(names), [(f"{n}R", "h1") for n in names],
        window=_boards(names), order=order,
        options=skill.SkillOptions(), draws=50, seed=1, bound=None, base_block=1,
        arm="arm-a", no_price=ledger)
    assert doc.get("status") != "error", doc
    assert doc["boards"] == len(kept_names)
    assert doc["boards_dropped_unpriced"] == 1
    assert doc["cs_in_sample"]["observed"] == len(kept_names)
    assert doc["verdict"].startswith("NO PRICE (1 dropped): ")


# ------------------------------------------------------------------ defect 2
#
# Every desk structure is a 2-leg vertical. A round trip is 2 legs x 2
# fills; pricing one leg charges HALF. Today this is latent only because the
# board carries no delta -- the moment a delta derivation lands, the cost
# silently halves at every cell. That is the "flatter the strategy" failure
# wired up and waiting, so it is pinned now.

def test_a_two_leg_vertical_is_priced_as_two_legs() -> None:
    """Two legs in the SAME cell must cost exactly twice one leg.

    Hand-derived: at |delta| 0.05 / dte 14 the measured full median is
    $0.020, so half = 0.020/2 x 1.000 = 0.010 per share per fill. One leg
    round trip = 2 fills x 0.010 x 100 + 2 x 0.65 = 2.00 + 1.30 = $3.30.
    Two legs = $6.60. (Spec table 2.2: the 0.00-0.10 / dte 7-21 cell.)
    """
    candidate = {
        "structure": "put_credit", "underlying": "SPY", "expiry": 14,
        "session": "2026-06-01", "source_timestamp_et": "2026-06-01T18:05:00-04:00",
        "is_eod_snapshot": True,
        "legs": [{"symbol": "SPY", "delta": "0.05"}, {"symbol": "SPY", "delta": "0.05"}],
    }
    model = measured_costs.SpreadCostModel.measured()
    priced = outcomes._price_candidate(model, candidate, None, "arm-a", "s1")
    assert priced is not None, "the candidate carries deltas for both legs; it must price"
    assert priced == Decimal("6.60"), (
        f"a 2-leg vertical at the measured cell must cost $6.60 (2 legs x $3.30); got "
        f"{priced}. Pricing one leg would HALVE every round trip the moment a delta "
        f"source lands.")


def test_a_single_leg_structure_is_priced_as_one_leg() -> None:
    """The one-leg case must still be $3.30 -- the two-leg test is not a
    blanket doubling."""
    candidate = {
        "structure": "single", "underlying": "SPY", "expiry": 14,
        "session": "2026-06-01", "source_timestamp_et": "2026-06-01T18:05:00-04:00",
        "is_eod_snapshot": True,
        "legs": [{"symbol": "SPY", "delta": "0.05"}],
    }
    model = measured_costs.SpreadCostModel.measured()
    assert outcomes._price_candidate(model, candidate, None, "arm-a", "s1") == Decimal("3.30")


def test_two_legs_in_different_cells_cost_the_sum_not_a_middle_cell() -> None:
    """Hand-derived from spec table 2.2 (dte 7-21 column):
      |delta| 0.05 -> half 0.010000 -> leg round trip 2 x 0.010 x 100 + 1.30 = 3.30
      |delta| 0.60 -> half 0.095000 -> leg round trip 2 x 0.095 x 100 + 1.30 = 20.30
      sum = 23.60.  (Neither the cheap cell, the dear cell, nor their mean.)
    """
    candidate = {
        "structure": "vertical", "underlying": "SPY", "expiry": 14,
        "session": "2026-06-01", "source_timestamp_et": "2026-06-01T18:05:00-04:00",
        "is_eod_snapshot": True,
        "legs": [{"symbol": "SPY", "delta": "0.05"}, {"symbol": "SPY", "delta": "0.60"}],
    }
    model = measured_costs.SpreadCostModel.measured()
    priced = outcomes._price_candidate(model, candidate, None, "arm-a", "s1")
    assert priced == Decimal("23.60"), (
        f"a cheap wing plus a near-the-money body is $3.30 + $20.30 = $23.60; got {priced}")


def test_a_candidate_with_no_leg_deltas_is_still_refused_not_half_priced() -> None:
    """Fail-closed survives the two-leg change."""
    candidate = {"structure": "put_credit", "underlying": "SPY", "expiry": 14,
                 "session": "2026-06-01",
                 "source_timestamp_et": "2026-06-01T18:05:00-04:00",
                 "is_eod_snapshot": True}
    ledger = measured_costs.NoPriceLedger()
    assert outcomes._price_candidate(
        measured_costs.SpreadCostModel.measured(), candidate, ledger, "arm-a", "s1") is None
    assert ledger.as_dict()["total"] == 1
    assert ledger.for_arm("arm-a")["reasons"]["s1"] == "delta_unavailable"


# ------------------------------------------------------------------ defect 3

def test_max_cost_absorbs_the_dearest_measured_round_trip() -> None:
    """``MAX_COST`` caps the round-trip cost the empirical-Bernstein bound may
    absorb. Hand-derived from spec table 2.2, the dearest 2-leg cell is
    |delta| 0.50-0.70 at dte 46-60: half 0.158365 -> 400 x 0.158365 + 2.60
    = 63.346 + 2.60 = $65.946. A bound of $50 is TIGHTER than the cost it
    exists to absorb, so ``monitor`` would report eb_unavailable the moment
    the model is wired."""
    assert skill.MAX_COST >= 65.946, (
        f"MAX_COST={skill.MAX_COST} is below the dearest measured 2-leg round trip "
        f"($65.946 = 400 x 0.158365 + 2.60), so the EB bound would be tighter than "
        f"the cost it bounds.")
    assert "14.60" not in (skill.MAX_COST.__doc__ or ""), "stale comment"


def test_the_max_cost_comment_does_not_claim_the_flat_model_is_current() -> None:
    """The constant's own comment said "$14.60 today"."""
    source = (skill.__file__ or "")
    with open(source, encoding="utf-8") as handle:
        text = handle.read()
    line = next(ln for ln in text.splitlines() if ln.startswith("MAX_COST"))
    assert "today" not in line, f"stale present-tense claim: {line.strip()!r}"


# ------------------------------------------------------------------ defect 4
#
# The ledger is built in `_v2_outcome` and consumed only by `skill_section`.
# Before the fix the one call site passed neither `no_price=` nor
# `cost_provenance=`, so a run in which EVERY board was refused reported
# `no_price.total == 0`, no verdict prefix, and zero-profit rows.

def test_longrun_hands_the_ledger_and_the_provenance_to_the_skill_section() -> None:
    """Both must be forwarded at the one real call site."""
    source_path = longrun.__file__ or ""
    with open(source_path, encoding="utf-8") as handle:
        text = handle.read()
    calls = re.findall(r"skill\.skill_section\((?:[^()]|\([^()]*\))*\)", text)
    assert calls, "no skill_section call found in longrun.py"
    for call in calls:
        assert "no_price=" in call, f"the ledger is not forwarded: {call}"
        assert "cost_provenance=" in call, f"the provenance is not forwarded: {call}"


def test_a_fully_refused_run_is_named_in_the_digest_not_scored_as_break_even() -> None:
    """BEHAVIOURAL, not a source scan: drive ``skill_section`` the way
    ``longrun`` does and require the refusal to be visible."""
    names = ["s1", "s2", "s3", "s4"]
    boards = _boards(names)
    ledger = measured_costs.NoPriceLedger()
    for name in names:                     # every board refused
        ledger.record(arm="arm-a", snapshot=name, key=_leg(abs_delta="0.90"),
                      reason="delta_out_of_universe")
    arms = [longrun.Arm(name="arm-a",
                        policy=longrun.PolicySpec(name="arm-a", kind="rule",
                                                 rule=lambda *_: None),
                        repeat=1)]
    receipts = {"arm-a": {b.snapshot: {"ok": True, "choice": f"{b.snapshot}R",
                                       "horizon": "h1"} for b in boards}}
    def get(snapshot: str, row_id: str,
            horizon: str | None) -> dict[str, Any] | None:
        # Every board is refused, so the drop removes them all and this is
        # never consulted for a scored row. It must still be a real fn
        # returning the OutcomeCache's dict shape: `ValueBook` is built
        # eagerly and `power_table` walks it.
        return {"gross": 10.0, "net": 6.70}
    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch.object(
            measured_costs, "TRADEABLE_SYMBOLS", frozenset({"SPY"})))
        section = skill.skill_section(boards, arms, receipts, longrun.OutcomeCache(get),
                                      _protocol(), options=skill.SkillOptions(),
                                      no_price=ledger, cost_provenance=None)
    doc = section["arms"]["arm-a"]
    assert doc["boards_dropped_unpriced"] == len(names), doc
    assert doc["no_price"]["total"] == len(names), doc
    assert doc["verdict"].startswith(f"NO PRICE ({len(names)} dropped): "), doc["verdict"]
    assert section["no_price"]["total"] == len(names), section["no_price"]


def _protocol() -> Any:
    return longrun.Protocol(draws=1000, seed=1, random_horizons=None)


# --------------------------------------------- adopted from impl B (kept)
#
# Two tests B carried that A did not. They are kept, not dropped: each is an
# independent oracle on a capability A lacks.
#
#   * ``price(..., delta_unavailable=[...])`` is how a delta SOURCE says "I
#     declined". A detects the wiring gap at the call site in outcomes.py;
#     B lets the pricer itself be told. Both are needed: the call-site check
#     catches a board that never carried a delta, this catches a derivation
#     component that had one and gave up.
#   * Grid TOTAlity. A pins the 15 cells by value; this pins that all 15
#     exist, are positive, and are 15 DISTINCT numbers, which a value-pin
#     list cannot state.

def test_a_leg_the_delta_source_declined_is_a_named_refusal() -> None:
    """The derivation component's 'I declined' is a DROP, not a price."""
    declined = _leg(abs_delta="0.05")
    model = measured_costs.SpreadCostModel.measured()
    with pytest.raises(measured_costs.UnpricedCostError) as excinfo:
        model.price([_leg(abs_delta="0.30")], delta_unavailable=[declined])
    assert excinfo.value.reason == "delta_unavailable"
    assert excinfo.value.key is declined
    assert "delta_unavailable" in tuple(measured_costs.REASON_TOKENS)


def test_the_delta_source_declining_refuses_the_whole_package() -> None:
    """Declining one leg must not let the other be priced alone."""
    model = measured_costs.SpreadCostModel.measured()
    with pytest.raises(measured_costs.UnpricedCostError) as excinfo:
        model.round_trip([_leg(abs_delta="0.05"), _leg(abs_delta="0.05")],
                         delta_unavailable=[_leg(abs_delta="0.05")])
    assert excinfo.value.reason == "delta_unavailable"


def test_the_derived_surface_is_total_over_the_whole_measured_grid() -> None:
    """5 |delta| bands x 3 dte bands = 15 cells, all populated, all positive,
    all DISTINCT. A hole in the grid is a measurement gap, and under the
    separable construction there cannot be one."""
    model = measured_costs.SpreadCostModel.measured()
    seen = set()
    for delta in ("0.05", "0.15", "0.25", "0.40", "0.60"):
        for dte in (14, 30, 55):
            half = model.half_spread_per_share(Decimal(delta), dte)
            assert half > 0, f"cell (|delta| {delta}, dte {dte}) priced at {half}"
            seen.add(half)
    assert len(seen) == 15, f"the 15 derived cells collapsed to {len(seen)}: {sorted(seen)}"
    assert "unmeasured_cell" in tuple(measured_costs.REASON_TOKENS), (
        "the token stays available to an offline builder even though the "
        "separable shipped surface cannot produce a hole")
