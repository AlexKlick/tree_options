"""The ``outcomes`` CLI's cost-model seam: two models, coexisting BY NAME.

``outcomes.CostModel`` charges every 2-leg vertical exactly ``$14.60`` and the
25-arm digest is calibrated on that flat basis. ``desk.cost.SpreadCostModel``
prices per moneyness. They coexist; the choice is EXPLICIT at the call site
and never a default that changes silently.

WHAT THIS FILE PINS
-------------------
1. ``--cost-model`` exists and defaults to ``flat``. The switchover of the
   digest is a separate decision, not part of this work.
2. The measured branch's summary carries ``model``, the CORPUS PROVENANCE,
   and a per-moneyness cost TABLE. Never a scalar: a scalar would re-flatten
   the whole exercise in the one artifact a human is most likely to read.
3. The CLI's flat key ``half_spread_per_share`` becomes ``half_spread_basis``
   in the measured branch, because the measured model has a METHOD of that
   name taking ``(abs_delta, dte)`` and a field/key collision would be a lie
   about which one the document is quoting.
4. THE WIRING GAP IS COUNTED, NOT SILENT. A board candidate carries no
   ``|delta|`` (see ``desk.cost`` module docstring: the board's full field set
   has no delta, no iv, no per-leg object). So every candidate the measured
   model is asked about refuses with ``delta_unavailable``, and the run
   REPORTS that count. The alternative -- falling back to the flat constant --
   is the exact failure this whole model exists to prevent.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.unit.test_desk_outcomes import ladder_bundle
from tree_options.desk.__main__ import run_cli

#: the 2-leg round trip at every (|delta| band, dte band), hand-transcribed
#: from the derived surface. A summary that publishes a scalar cannot hold
#: all of them; one that publishes the table can.
MEASURED_2LEG_ROUND_TRIP = {
    "0.00-0.10": {"7-21": "6.600000", "22-45": "7.932000", "46-60": "9.268000"},
    "0.10-0.20": {"7-21": "8.600000", "22-45": "10.598000", "46-60": "12.602000"},
    "0.20-0.35": {"7-21": "12.600000", "22-45": "15.930000", "46-60": "19.270000"},
    "0.35-0.50": {"7-21": "14.600000", "22-45": "18.596000", "46-60": "22.604000"},
    "0.50-0.70": {"7-21": "40.600000", "22-45": "53.254000", "46-60": "65.946000"},
}


def _run(tmp_path: Path, argv: list[str]) -> dict[str, Any]:
    bundle = tmp_path / "bundle.json"
    bundle.write_text(json.dumps(ladder_bundle()))
    out = tmp_path / "table.jsonl"
    rc = run_cli(["outcome-table", "--bundle", str(bundle), "--out", str(out), *argv])
    assert rc == 0, f"the CLI refused: {argv}"
    return json.loads(Path(f"{out}.summary.json").read_text())


def test_the_cost_model_flag_exists_and_defaults_to_flat(tmp_path: Path) -> None:
    """The digest is calibrated on the flat basis; moving it is a decision,
    not a side effect of shipping the new model."""
    flat = _run(tmp_path, [])
    assert flat["costs"]["model"] == "flat"
    assert flat["round_trip_cost"] == "14.60"
    assert flat["costs"]["half_spread_per_share"] == "0.03"


def test_the_flat_branch_is_byte_identical_to_before(tmp_path: Path) -> None:
    """Coexistence means the legacy path is untouched, not merely still
    importable: same scalar, same keys, same number."""
    default = _run(tmp_path, [])
    explicit = _run(tmp_path, ["--cost-model", "flat"])
    assert default["costs"] == explicit["costs"]
    assert default["round_trip_cost"] == explicit["round_trip_cost"] == "14.60"
    from decimal import Decimal

    from tree_options.desk.outcomes import CostModel
    assert CostModel().round_trip() == Decimal("14.60")


def test_an_unknown_cost_model_is_refused(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle.json"
    bundle.write_text(json.dumps(ladder_bundle()))
    out = tmp_path / "table.jsonl"
    rc = run_cli(["outcome-table", "--bundle", str(bundle), "--out", str(out),
                  "--cost-model", "guess"])
    assert rc == 2, "an unknown cost model must be refused, not defaulted"


def test_the_measured_branch_publishes_a_table_not_a_scalar(tmp_path: Path) -> None:
    """A scalar would re-flatten the whole exercise in the one artifact a
    human is most likely to read."""
    doc = _run(tmp_path, ["--cost-model", "measured"])
    assert doc["costs"]["model"] == "measured-spread/1"
    table = doc["costs"]["round_trip_by_moneyness"]
    assert table == MEASURED_2LEG_ROUND_TRIP
    # every cell is a distinct real measurement, and one of them IS the flat
    # constant -- which is the whole finding
    flat = {cell for row in table.values() for cell in row.values()}
    assert len(flat) == 15
    assert "14.600000" in flat
    assert not all(cell == "14.600000" for cell in flat)
    assert isinstance(doc["round_trip_cost"], dict), \
        "round_trip_cost must be the table, not one number"


def test_the_measured_branch_carries_the_corpus_provenance(tmp_path: Path) -> None:
    """EOD snapshots cannot describe a 10:00 fill, and that gap is
    unfalsifiable from this data. It must be stated on every artifact."""
    doc = _run(tmp_path, ["--cost-model", "measured"])
    prov = doc["costs"]["provenance"]
    assert prov["source"] == "cboe-delayed-eod-chains"
    assert prov["snapshot_window_et"] == "17:45-06:30"
    assert prov["n_rows"] == 18783
    assert prov["describes_fill_clock"] is False
    assert "10:00" in prov["decision_clocks_et"]
    assert "17:45-06:30" in prov["gap"]


def test_the_measured_branch_renames_the_colliding_cli_key(tmp_path: Path) -> None:
    """``half_spread_per_share`` is a Decimal FIELD on the flat model and a
    two-argument METHOD on the measured one. A document that published the
    key without saying which would be ambiguous at best."""
    doc = _run(tmp_path, ["--cost-model", "measured"])
    assert "half_spread_per_share" not in doc["costs"]
    assert doc["costs"]["half_spread_basis"] == "derived: measured |delta| marginal / 2 * dte multiplier"


def test_the_wiring_gap_is_counted_not_silently_flattened(tmp_path: Path) -> None:
    """A board candidate carries no |delta|. The measured model REFUSES it
    (fail-closed) and the run REPORTS the count, instead of quietly falling
    back to the flat constant -- which is the exact failure this model exists
    to prevent."""
    doc = _run(tmp_path, ["--cost-model", "measured"])
    gap = doc["costs"]["no_price"]
    assert gap["total"] > 0, \
        "the board carries no delta, so nothing that fills can have been priced; a zero here means something priced it"
    assert gap["by_reason"] == {"delta_unavailable": gap["total"]}, gap["by_reason"]
    assert doc["costs"]["boards_dropped_unpriced"] == gap["total"]
    # and the FLAT branch reports none, from the same ledger
    flat = _run(tmp_path, ["--cost-model", "flat"])
    assert "no_price" not in flat["costs"], "the flat branch must not claim a wiring gap it does not have"


def test_an_unpriced_candidate_is_never_scored_at_a_net_of_zero(tmp_path: Path) -> None:
    """A refused candidate must not be written as a zero-profit trade: that is
    the ``ValueBook`` zero-fill hole, in the table.

    A candidate that never FILLED is a different event and keeps its own
    ``no_fill`` status -- it never reached pricing, so it is not a wiring gap
    and must not be counted as one.
    """
    bundle = tmp_path / "bundle.json"
    bundle.write_text(json.dumps(ladder_bundle()))
    out = tmp_path / "table.jsonl"
    assert run_cli(["outcome-table", "--bundle", str(bundle), "--out", str(out),
                    "--cost-model", "measured"]) == 0
    rows = [json.loads(line) for line in out.read_text().splitlines()]
    assert rows
    unpriced = [r for r in rows if r["status"] != "no_fill"]
    assert unpriced, "the fixture produced nothing that filled, so nothing was tested"
    for row in unpriced:
        assert row["status"] == "no_price"
        assert row["net"] is None and row["gross"] is None
        assert row["exit_reason"] == "delta_unavailable"
    # no priced row exists at all under the measured model, and none is
    # silently carrying the flat $14.60
    for row in rows:
        if row["status"] in ("closed", "marked_at_end"):
            pytest.fail(f"a candidate was priced with no delta available: {row}")
