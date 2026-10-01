"""TEST CONTRACT B, end to end: a refusal must REACH the digest.

``test_desk_measured_costs`` pins the model's own behaviour. This file pins
the consequence: an arm whose boards cannot be priced must show FEWER
evaluated entries, must say how many and why, and must still produce a
number that is valid rather than quietly wrong.

The failure this guards against is specific and quiet. ``ValueBook.values``
writes ``0`` for any row whose outcome fn returns ``None``. So a board the
cost model refused to price scores as a ZERO-PROFIT TRADE: the arm's net
falls, its cost drag falls, its entry rate is unchanged, and nothing in the
digest says a single board was never priced. The arm looks like a strategy
that took some bad trades. It is a strategy whose cost model had a hole.

So the contract is fail-closed: a board the ledger could not price is
EXCLUDED from the decomposition, COUNTED in the doc, and NAMED in the
verdict line a human actually reads.

Hand fixture (all five boards: one row, one horizon ``h1``, put_credit)::

    board  legs                        measured?   gross    cost   net
    s1     SPY  |delta| 0.05 dte 14     yes          40.00   3.30  36.70
    s2     SPY  |delta| 0.05 dte 14     yes          25.00   3.30  21.70
    s3     XLF  |delta| 0.05 dte 14     no: unknown_symbol
    s4     SPY  delta MISSING dte 14    no: no_delta
    s5     SPY  |delta| 0.90 dte 14     no: delta_out_of_universe

    cost per leg = 2 fills * 0.010 * 100 + 2 * 0.65 = 2.00 + 1.30 = 3.30

  STRICT run (SPY/IWM/QQQ measured):  evaluated s1, s2  -> entered 2,
    net_total 36.70 + 21.70 = 58.40, gross_total 65.00, cost_drag 6.60,
    3 boards dropped, reasons {s3: unknown_symbol, s4: no_delta,
    s5: delta_out_of_universe}

  EXTENDED run (XLF also measured):   evaluated s1, s2, s3 -> entered 3,
    net_total 36.70 + 21.70 + 6.70 = 65.10, gross_total 75.00,
    cost_drag 9.90, 2 boards dropped.

Neither cost_drag is an exact multiple of the old flat $14.60 (6.60 and
9.90), so the flat signature is provably broken by these numbers alone.
"""

from __future__ import annotations

import contextlib
import inspect
from decimal import Decimal
from typing import Any
from unittest import mock

import pytest

from tree_options.desk import skill
from tree_options.desk.longrun import Board

try:  # pragma: no cover - the RED path is the point
    from tree_options.desk import cost as measured_costs
    _IMPORT_ERROR: Exception | None = None
except Exception as exc:  # any import failure is the RED
    measured_costs = None  # type: ignore[assignment]
    _IMPORT_ERROR = exc


ARM = "arm-A"
HORIZONS = ("h1",)
FLAT = Decimal("14.60")          # outcomes.CostModel().round_trip(), being replaced
CHEAP_HALF = Decimal("0.010")   # SPY |delta| 0.05 @ dte 14: full median 0.020 / 2
LEG_COST = CHEAP_HALF * 100 * 2 + Decimal("0.65") * 2   # 3.30

#: snapshot -> (symbol, delta-or-None, gross)
FIXTURE: dict[str, tuple[str, str | None, str]] = {
    "s1": ("SPY", "0.05", "40.00"),
    "s2": ("SPY", "0.05", "25.00"),
    "s3": ("XLF", "0.05", "10.00"),
    "s4": ("SPY", None, "15.00"),
    "s5": ("SPY", "0.90", "12.00"),
}
DECISIONS: list[tuple[str | None, str | None]] = [(f"{s}R", "h1") for s in FIXTURE]
EXPECTED_REASONS = {
    "s3": "unknown_symbol",
    "s4": "no_delta",
    "s5": "delta_out_of_universe",
}


def mc() -> Any:
    if measured_costs is None:
        pytest.fail(
            "tree_options.desk.cost does not exist yet "
            f"(import error: {_IMPORT_ERROR!r}). The NO_PRICE ledger that this "
            "file demands is part of the measured cost model; until it exists "
            "these tests must fail, not skip."
        )
    return measured_costs


def boards() -> list[Board]:
    return [Board(s, f"2026-06-0{index + 1}", "10:00",
                  [{"id": f"{s}R", "structure": "put_credit", "underlying": symbol}])
            for index, (s, (symbol, _, _)) in enumerate(FIXTURE.items())]


@contextlib.contextmanager
def measured_symbols(sources: tuple[str, ...]):
    """Narrow the shipped tradeable-symbol pool to exactly ``sources``.

    The measured model has no per-symbol table (the corpus marginals are
    POOLED across IWM/QQQ/SPY, so there is nothing to differentiate), and it
    refuses any symbol outside its declared pool. Overriding the module
    constant is therefore the ONLY way to run the strict-vs-extended A/B, and
    it changes nothing about the arithmetic: the cheap cell is the same
    measured cell either way.
    """
    with mock.patch.object(mc(), "TRADEABLE_SYMBOLS", frozenset(sources)):
        yield


def model() -> Any:
    """The shipped measured model, unmodified."""
    return mc().SpreadCostModel.measured()


def provenance() -> Any:
    m = mc()
    return m.CostProvenance(
        source="cboe-delayed-eod-chains",
        snapshot_window_et="17:45-06:30",
        universe_filter="|delta|<=0.70, 7<=dte<=60, volume>0, oi>0, symbol in IWM/QQQ/SPY",
        n_rows=18783,
        decision_clocks_et=("10:00", "15:15"),
    )


def price_every_board(the_model: Any, ledger: Any) -> Any:
    """An outcome fn that prices through the model and RECORDS every refusal.

    This is the seam the desk must wire: price -> raise -> count -> drop.
    """
    def get(snapshot: str, row_id: str, horizon: str | None) -> tuple[float, float] | None:
        symbol, abs_delta, gross = FIXTURE[snapshot]
        legs = [mc().Leg(symbol=symbol,
                         abs_delta=None if abs_delta is None else Decimal(abs_delta),
                         dte=14,
                         source_session="2026-06-01",
                         source_timestamp_et="2026-06-01T18:05:00-04:00",
                         is_eod_snapshot=True)]
        try:
            cost = the_model.round_trip(legs)
        except mc().UnpricedCostError as exc:
            ledger.record(arm=ARM, snapshot=snapshot, key=exc.key, reason=exc.reason)
            return None                      # dropped from scoring, never priced
        net = float(Decimal(gross) - cost)
        return (float(gross), net)
    return get


def run_digest(sources: tuple[str, ...]) -> tuple[dict[str, Any], Any]:
    """Price every board, then digest the arm with the ledger's own report."""
    with measured_symbols(sources):
        the_model = model()
        ledger = mc().NoPriceLedger()
        get = price_every_board(the_model, ledger)
        for snapshot in FIXTURE:              # drive the refusals into the ledger
            get(snapshot, f"{snapshot}R", "h1")
        book = skill.ValueBook(get, HORIZONS)
        doc = skill.arm_skill(
            book, boards(), DECISIONS, window=boards(),
            options=skill.SkillOptions(), draws=200, seed=1, bound=None, base_block=1,
            arm=ARM, no_price=ledger,
            cost_provenance=provenance().as_dict())
    return doc, ledger


#: the only two fixture boards that price on every symbol pool, so a run over
#: them is genuinely CLEAN -- zero refusals, zero drops. The prefix assertion
#: below needs a run that really dropped nothing; the five-board fixture never
#: is one, because s4 has no delta and s5 is out of the measured universe at
#: every pool.
CLEAN = ("s1", "s2")


def clean_boards() -> list[Board]:
    return [b for b in boards() if b.snapshot in CLEAN]


def run_clean_digest() -> tuple[dict[str, Any], Any]:
    """Digest only the priceable boards, with the ledger driven empty."""
    with measured_symbols(("SPY", "XLF")):
        the_model = model()
        ledger = mc().NoPriceLedger()
        get = price_every_board(the_model, ledger)
        for snapshot in CLEAN:
            assert get(snapshot, f"{snapshot}R", "h1") is not None, \
                f"{snapshot} is supposed to price; if it does not, the fixture is wrong"
        kept = clean_boards()
        book = skill.ValueBook(get, HORIZONS)
        doc = skill.arm_skill(
            book, kept, [(f"{s}R", "h1") for s in CLEAN], window=kept,
            options=skill.SkillOptions(), draws=200, seed=1, bound=None, base_block=1,
            arm=ARM, no_price=ledger, cost_provenance=provenance().as_dict())
    return doc, ledger


# ------------------------------------------------------------------ the ledger


def test_the_ledger_counts_every_refusal_by_arm_snapshot_and_reason() -> None:
    _, ledger = run_digest(("SPY",))
    report = ledger.for_arm(ARM)
    assert report["total"] == 3
    assert report["snapshots"] == ["s3", "s4", "s5"]
    assert report["reasons"] == EXPECTED_REASONS
    whole = ledger.as_dict()
    assert whole["total"] == 3
    assert whole["by_arm"] == {ARM: 3}
    assert whole["by_reason"] == {"unknown_symbol": 1, "no_delta": 1,
                                  "delta_out_of_universe": 1}
    # the count is per arm, so one arm's hole never hides behind another's
    assert ledger.for_arm("some-other-arm")["total"] == 0


def test_a_fully_priced_run_reports_no_drops() -> None:
    doc, ledger = run_digest(("SPY", "XLF"))     # only the no_delta / 0.90 legs still refuse
    assert ledger.for_arm(ARM)["total"] == 2
    assert ledger.for_arm(ARM)["snapshots"] == ["s4", "s5"]
    assert doc["boards"] == 3 and doc["entered"] == 3


def test_the_ledger_is_json_serialisable_so_a_digest_can_carry_it() -> None:
    import json
    _, ledger = run_digest(("SPY",))
    payload = json.loads(json.dumps(ledger.as_dict(), default=str))
    assert payload["total"] == 3
    assert payload["by_arm"] == {ARM: 3}


# ------------------------------------------------------------- the propagation


def test_a_refused_board_leaves_the_count_of_evaluated_entries() -> None:
    """The whole point. Three boards cannot be priced, so the arm is scored
    on two, and says so."""
    doc, _ = run_digest(("SPY",))
    assert doc["entered"] == 2, "a refused board must not count as a zero-profit trade"
    assert doc["boards"] == 2
    assert doc["boards_dropped_unpriced"] == 3
    assert doc["no_price"]["total"] == 3
    assert doc["no_price"]["snapshots"] == ["s3", "s4", "s5"]
    assert doc["no_price"]["reasons"] == EXPECTED_REASONS
    # the doc's own counts agree with the ledger's -- one source of truth
    assert doc["boards_dropped_unpriced"] == doc["no_price"]["total"]


def test_the_digest_still_produces_a_valid_number_after_the_drops() -> None:
    """Fewer entries, not a broken digest. 36.70 + 21.70 = 58.40, the
    components still sum to it exactly, and the identity residual is zero."""
    doc, _ = run_digest(("SPY",))
    assert doc["net_total"] == pytest.approx(58.40)
    assert doc["gross_total"] == pytest.approx(65.00)
    assert doc["cost_drag"] == pytest.approx(6.60)
    assert doc["identity_residual"] == 0.0
    assert sum(doc["components"].values()) == pytest.approx(58.40)


def test_the_drop_is_named_in_the_verdict_a_human_reads() -> None:
    doc, _ = run_digest(("SPY",))
    assert doc["verdict"].startswith("NO PRICE (3 dropped): ")
    assert "Descriptive; nothing promoted." in doc["verdict"]
    # a run where NOTHING was refused carries no such prefix
    clean, ledger = run_clean_digest()
    assert ledger.for_arm(ARM)["total"] == 0
    assert clean["verdict"].startswith("NO SKILL DETECTED") or \
        clean["verdict"].startswith("NO TRADES")
    assert not clean["verdict"].startswith("NO PRICE")
    assert clean["no_price"]["total"] == 0
    assert clean["boards_dropped_unpriced"] == 0


def test_pricing_one_more_source_moves_both_the_count_and_the_total() -> None:
    """A/B: extending the measured universe to XLF prices s3 and changes
    entered 2 -> 3 and net_total 58.40 -> 65.10. The drop count is wired to
    the pricing, not a hardcoded constant."""
    strict, _ = run_digest(("SPY",))
    extended, _ = run_digest(("SPY", "XLF"))
    assert (strict["entered"], extended["entered"]) == (2, 3)
    assert (strict["no_price"]["total"], extended["no_price"]["total"]) == (3, 2)
    assert strict["net_total"] == pytest.approx(58.40)
    assert extended["net_total"] == pytest.approx(65.10)
    assert strict["cost_drag"] == pytest.approx(6.60)     # 2 x 3.30
    assert extended["cost_drag"] == pytest.approx(9.90)   # 3 x 3.30
    assert extended["gross_total"] == pytest.approx(75.00)


def test_cost_drag_is_no_longer_an_exact_multiple_of_the_flat_model() -> None:
    """The harness has been reading ``cost_drag / 14.60`` as an exact integer
    for all 25 arms. If it still is, the model did not change."""
    for sources, expected_entered in ((("SPY",), 2), (("SPY", "XLF"), 3)):
        doc, _ = run_digest(sources)
        drag = Decimal(str(doc["cost_drag"]))
        assert doc["entered"] == expected_entered
        assert drag == Decimal(expected_entered) * LEG_COST
        assert drag != FLAT * expected_entered
        assert drag % FLAT != 0, f"cost_drag {drag} is a multiple of the flat model"


def test_provenance_reaches_the_digest_and_still_disclaims_the_fill_clock() -> None:
    doc, _ = run_digest(("SPY",))
    payload = doc["cost_provenance"]
    assert payload["source"] == "cboe-delayed-eod-chains"
    assert payload["snapshot_window_et"] == "17:45-06:30"
    assert payload["describes_fill_clock"] is False
    assert "10:00" in payload["decision_clocks_et"]


# ------------------------------------------------------------- fail-closed guards


def test_a_digest_with_no_ledger_serves_none_never_an_invented_zero() -> None:
    """The key is ALWAYS present; its VALUE distinguishes the states.

    Ruling 2026-10-01: no ledger passed = never looked = ``None``. Serving
    zeros for that state collapsed 'never looked' into 'looked, nothing
    refused'; a real (even empty) ledger is the only thing allowed to report
    zero drops."""
    with measured_symbols(("SPY", "XLF")):
        the_model = model()
        book = skill.ValueBook(price_every_board(the_model, mc().NoPriceLedger()),
                               HORIZONS)
    doc = skill.arm_skill(
        book, boards(), DECISIONS, window=boards(),
        options=skill.SkillOptions(), draws=200, seed=1, bound=None, base_block=1)
    assert doc["no_price"] is None
    assert doc["boards_dropped_unpriced"] is None
    assert doc["cost_provenance"] is None, "never invent provenance that was not supplied"
    looked = skill.arm_skill(
        book, boards(), DECISIONS, window=boards(),
        options=skill.SkillOptions(), draws=200, seed=1, bound=None, base_block=1,
        no_price=mc().NoPriceLedger())
    assert looked["no_price"]["total"] == 0
    assert looked["boards_dropped_unpriced"] == 0


def test_arm_skill_and_skill_section_take_the_no_price_parameters() -> None:
    """The digest section must forward the SAME ledger object per arm, so the
    top-level count and the per-arm counts cannot disagree -- and so a
    caller cannot pass a stale hand-copied report."""
    section_params = inspect.signature(skill.skill_section).parameters
    for name in ("no_price", "cost_provenance"):
        assert name in section_params, f"skill_section must accept {name}"
    assert section_params["no_price"].kind is inspect.Parameter.KEYWORD_ONLY
    arm_params = inspect.signature(skill.arm_skill).parameters
    for name in ("arm", "no_price", "cost_provenance"):
        assert name in arm_params, f"arm_skill must accept {name}"
        assert arm_params[name].kind is inspect.Parameter.KEYWORD_ONLY
    assert arm_params["no_price"].default is None, "no_price defaults to None (no drops)"
    assert arm_params["cost_provenance"].default is None, "never invent provenance"
    assert arm_params["arm"].default == "", "arm names the ledger row to report"
