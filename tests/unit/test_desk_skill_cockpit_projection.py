"""The cockpit view must carry the skill section's NO PRICE ledger and cost basis.

Merge-resolution note (feat/cockpit-spa-reconciled): the served skill payload
follows PR #50's nested shape — ``digest.skill = skill.cockpit_projection(section)
= {"no_price": <section ledger {total, by_arm, by_reason} | None>, "arms":
{name: {verdict, excess_total, excess_block_ci95, forward_significant,
boards_dropped_unpriced, no_price, cost_provenance}}}``. The earlier SPA
draft flattened the arms into ``digest.skill`` itself and served the section
ledger under a separate ``digest.skill_no_price`` key; the API shape won, so
this file indexes ``view["skill"]["arms"]`` / ``view["skill"]["no_price"]``
and the per-arm ledger keys are ALWAYS present (``None`` when the section
predates them — a stable shape whose absence stays visible).

On ``feat/cost-model-measured-v2`` (PR #48) a digest's skill section gains,
per arm, ``no_price`` (``{total, snapshots, reasons}``), a scalar
``boards_dropped_unpriced``, a ``cost_provenance`` dict, and a
``"NO PRICE (N dropped): "`` verdict prefix, plus a section-level
``no_price`` (``{total, by_arm, by_reason}``). Those shapes and every refusal
number below are pinned by ``tests/unit/test_desk_no_price_propagation.py``
on that branch (the STRICT run: boards s3/s4/s5 refused for unknown_symbol /
no_delta / delta_out_of_universe -> 3 dropped, ``by_arm {arm: 3}``; a clean
arm carries ``no_price.total == 0`` / ``boards_dropped_unpriced == 0``, never
a missing key, and ``cost_provenance is None`` when none was supplied —
"never invent provenance that was not supplied"). The provenance dict itself
is ``CostProvenance.measured_corpus().as_dict()`` from
``src/tree_options/desk/cost.py`` on that branch: source
``cboe-delayed-eod-chains``, snapshot window ``17:45-06:30`` ET, decision
clocks ``(10:00, 10:15, 15:15)`` ET, and ``describes_fill_clock`` hard-coded
``False`` — EOD snapshots cannot describe intraday fills.

The cockpit contract this file adds: the refusal AND its cost basis must
SURVIVE the read-only projection, so a fully-refused arm can render as NO
PRICE in the SPA instead of quietly reading as break-even, and cost-derived
numbers never travel without their provenance. A digest that predates the
measured cost model (no ledger keys at all) must project its absence
visibly — ``None``, never an invented zero ledger.
"""

from __future__ import annotations

from typing import Any

from tree_options.desk import longrun


def _digest(skill: Any) -> dict[str, Any]:
    return {"schema": longrun.DIGEST_SCHEMA,
            "promotion": {"promoted": False, "rule": "pre-registered"},
            "standings": [], "walk_forward": {}, "skill": skill}


# numbers: STRICT run in tests/unit/test_desk_no_price_propagation.py (#48)
REFUSALS = {"s3": "unknown_symbol", "s4": "no_delta", "s5": "delta_out_of_universe"}

# CostProvenance.measured_corpus().as_dict() — src/tree_options/desk/cost.py
# on feat/cost-model-measured-v2 (#48), transcribed verbatim.
COST_PROVENANCE = {
    "source": "cboe-delayed-eod-chains",
    "snapshot_window_et": "17:45-06:30",
    "universe_filter": ("symbol in IWM/QQQ/SPY, 7 <= dte <= 60, volume > 0, "
                        "oi > 0, |delta| <= 0.70"),
    "n_rows": 18_783,
    "decision_clocks_et": ["10:00", "10:15", "15:15"],
    "describes_fill_clock": False,
    "gap": ("the cboe-delayed-eod-chains corpus was captured 17:45-06:30 ET, "
            "outside every decision clock this desk trades (10:00, 10:15, "
            "15:15 ET); no instant of the capture window describes a fill at "
            "a decision clock, and this gap is not falsifiable from the corpus"),
}

SECTION_WITH_LEDGER = {
    "no_price": {"total": 3, "by_arm": {"m#1": 3},
                 "by_reason": {"unknown_symbol": 1, "no_delta": 1,
                               "delta_out_of_universe": 1}},
    "arms": {
        "m#1": {"verdict": "NO PRICE (3 dropped): NO SKILL DETECTED (excess CI "
                           "straddles 0) — Descriptive; nothing promoted.",
                "excess_total": 0.0,
                "no_price": {"total": 3, "snapshots": ["s3", "s4", "s5"],
                             "reasons": REFUSALS},
                "boards_dropped_unpriced": 3,
                "cost_provenance": COST_PROVENANCE},
        # a clean arm on #48 carries a ZERO ledger and no invented provenance
        "no_trade": {"verdict": "NO TRADES (entered 0) — Descriptive; nothing "
                                "promoted.", "excess_total": 0.0,
                     "no_price": {"total": 0, "snapshots": [], "reasons": {}},
                     "boards_dropped_unpriced": 0, "cost_provenance": None},
    },
}

SECTION_WITHOUT_LEDGER = {  # a digest that predates #48 entirely
    "arms": {"m#1": {"verdict": "NO SKILL DETECTED (excess CI straddles 0) — "
                                "Descriptive; nothing promoted.",
                     "excess_total": 0.0}},
}


def test_the_view_carries_the_no_price_ledger_and_cost_provenance() -> None:
    view = longrun._project_digest(_digest(SECTION_WITH_LEDGER))
    whole = view["skill"]["no_price"]
    assert whole == {"total": 3, "by_arm": {"m#1": 3},
                     "by_reason": {"unknown_symbol": 1, "no_delta": 1,
                                   "delta_out_of_universe": 1}}
    arm = view["skill"]["arms"]["m#1"]
    assert arm["boards_dropped_unpriced"] == 3
    assert arm["no_price"] == {"total": 3, "snapshots": ["s3", "s4", "s5"],
                               "reasons": REFUSALS}
    assert arm["verdict"].startswith("NO PRICE (3 dropped): ")
    # the pre-existing projection keys are untouched
    assert arm["excess_total"] == 0.0
    # the cost basis travels with the cost-derived numbers, verbatim
    assert arm["cost_provenance"] == COST_PROVENANCE
    assert arm["cost_provenance"]["describes_fill_clock"] is False


def test_a_clean_arm_carries_a_zero_ledger_never_a_refusal() -> None:
    view = longrun._project_digest(_digest(SECTION_WITH_LEDGER))
    clean = view["skill"]["arms"]["no_trade"]
    assert clean["no_price"]["total"] == 0
    assert clean["boards_dropped_unpriced"] == 0
    assert not clean["verdict"].startswith("NO PRICE")
    # #48's guard: never invent provenance that was not supplied
    assert clean["cost_provenance"] is None


def test_a_digest_that_predates_the_ledger_serves_visible_absence() -> None:
    view = longrun._project_digest(_digest(SECTION_WITHOUT_LEDGER))
    # the section-level ledger is None — "never looked" stays distinct from
    # "looked, nothing refused" (a zero ledger), never an invented zero
    assert view["skill"]["no_price"] is None
    arm = view["skill"]["arms"]["m#1"]
    # the per-arm keys are always present but visibly absent
    assert arm["no_price"] is None
    assert arm["boards_dropped_unpriced"] is None
    assert arm["cost_provenance"] is None


def test_an_error_or_missing_section_still_projects_to_no_arms() -> None:
    empty = longrun._project_digest(_digest({"status": "error", "error": "x"}))["skill"]
    assert empty["arms"] == {}
    assert empty["no_price"] is None
    assert longrun._project_digest(_digest(None))["skill"] is None
