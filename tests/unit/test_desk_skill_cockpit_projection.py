"""The cockpit view must carry the skill section's NO PRICE ledger.

On ``feat/cost-model-measured-v2`` (PR #48) a digest's skill section gains,
per arm, ``no_price`` (``{total, snapshots, reasons}``), a scalar
``boards_dropped_unpriced``, and a ``"NO PRICE (N dropped): "`` verdict
prefix, plus a section-level ``no_price`` (``{total, by_arm, by_reason}``).
Those shapes and every number below are pinned by
``tests/unit/test_desk_no_price_propagation.py`` on that branch (the STRICT
run: boards s3/s4/s5 refused for unknown_symbol / no_delta /
delta_out_of_universe -> 3 dropped, ``by_arm {arm: 3}``).

The cockpit contract this file adds: the refusal must SURVIVE the read-only
projection, so a fully-refused arm can render as NO PRICE in the SPA instead
of quietly reading as break-even. A digest that predates the measured cost
model (origin/main's shape, no ledger keys) must project exactly as before —
the view never invents a ledger.
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
                "boards_dropped_unpriced": 3},
        "no_trade": {"verdict": "NO TRADES (entered 0) — Descriptive; nothing "
                                "promoted.", "excess_total": 0.0},
    },
}

SECTION_WITHOUT_LEDGER = {  # origin/main's skill_section output shape
    "arms": {"m#1": {"verdict": "NO SKILL DETECTED (excess CI straddles 0) — "
                                "Descriptive; nothing promoted.",
                     "excess_total": 0.0}},
}


def test_the_view_carries_the_no_price_ledger() -> None:
    view = longrun._project_digest(_digest(SECTION_WITH_LEDGER))
    arm = view["skill"]["m#1"]
    assert arm["boards_dropped_unpriced"] == 3
    assert arm["no_price"]["total"] == 3
    assert arm["no_price"]["reasons"] == REFUSALS
    assert arm["verdict"].startswith("NO PRICE (3 dropped): ")
    # the pre-existing projection keys are untouched
    assert arm["excess_total"] == 0.0
    whole = view["skill_no_price"]
    assert whole["total"] == 3 and whole["by_arm"] == {"m#1": 3}
    assert whole["by_reason"] == {"unknown_symbol": 1, "no_delta": 1,
                                  "delta_out_of_universe": 1}


def test_an_arm_without_drops_carries_no_ledger_keys() -> None:
    view = longrun._project_digest(_digest(SECTION_WITH_LEDGER))
    clean = view["skill"]["no_trade"]
    assert "no_price" not in clean and "boards_dropped_unpriced" not in clean
    assert not clean["verdict"].startswith("NO PRICE")


def test_a_digest_that_predates_the_ledger_invents_nothing() -> None:
    view = longrun._project_digest(_digest(SECTION_WITHOUT_LEDGER))
    assert view["skill_no_price"] is None
    assert "no_price" not in view["skill"]["m#1"]
    assert "boards_dropped_unpriced" not in view["skill"]["m#1"]


def test_an_error_or_missing_section_still_projects_to_no_arms() -> None:
    assert longrun._project_digest(_digest({"status": "error", "error": "x"}))["skill"] == {}
    assert longrun._project_digest(_digest(None))["skill"] is None
