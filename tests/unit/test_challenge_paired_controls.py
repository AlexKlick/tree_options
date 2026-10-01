"""The paired-control lane: replay's by_session accounting, the first_row
trivial-picker policy, and the digest's paired columns — the mechanical
inputs the REGISTERED promotion rule (docs/desk/PROMOTION-RULE.md) reads.

Oracle discipline: expectations are arithmetic on hand-built numbers, never
the implementation's own computations.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from tests.unit.test_desk_challenge import BUNDLE_NAME, _store
from tests.unit.test_desk_hindsight import multi_day_bundle
from tree_options.desk import lab
from tree_options.desk.challenge import (
    PolicyEntry,
    _paired_columns,
    _session_series,
    policy_field,
    run_challenge,
)

UTC = timezone.utc
DAYS = (date(2026, 9, 21), date(2026, 9, 22), date(2026, 9, 23))
T0 = datetime(2026, 9, 25, 1, 0, tzinfo=UTC)
VINTAGE = "20260927-v1"


# ------------------------------------------------------- by_session accounting


def test_by_session_deltas_sum_exactly_to_the_total(tmp_path: Path) -> None:
    """The arithmetic oracle: sum(session deltas) == total closed pnl."""
    raw = multi_day_bundle(*DAYS)
    store = _store(tmp_path, {VINTAGE: raw})
    doc = lab.run_lab(
        lab.LabConfig(
            bundle=store / "evaluations" / "intraday-graph" / VINTAGE / BUNDLE_NAME,
            policy="no_trade",
            sessions=len(DAYS),
            lab_root=tmp_path / "lab",
        ),
        now=T0,
    )
    rows = doc["summary"]["by_session"]
    assert [r["session"] for r in rows] == [d.isoformat() for d in DAYS]
    total = Decimal(doc["summary"]["closed_capital_proxy"]) - Decimal(5000)
    assert sum((Decimal(r["closed_pnl"]) for r in rows), Decimal(0)) == total


def test_first_row_picks_rows_enters_and_burns_nothing(tmp_path: Path) -> None:
    store = _store(tmp_path, {VINTAGE: multi_day_bundle(*DAYS)})
    doc = lab.run_lab(
        lab.LabConfig(
            bundle=store / "evaluations" / "intraday-graph" / VINTAGE / BUNDLE_NAME,
            policy=lab.FIRST_ROW_POLICY,
            sessions=len(DAYS),
            lab_root=tmp_path / "lab",
        ),
        now=T0,
    )
    assert doc["status"] == "ok"
    assert doc["policy"] == lab.FIRST_ROW_POLICY
    assert doc["model_calls"] == 0  # a rules control never burns
    assert doc["summary"]["entered"] > 0  # and it does trade the first row
    assert doc["summary"]["by_session"]  # paired bars present


def test_policy_field_always_carries_both_controls(tmp_path: Path) -> None:
    field = policy_field(tmp_path / "empty-lab")
    assert [e.policy for e in field[:2]] == ["no_trade", lab.FIRST_ROW_POLICY]
    assert all(e.kind == "rules" for e in field[:2])


# ------------------------------------------------------------ paired columns


def _run_doc(policy: str, by_session: dict[str, float]) -> dict:
    return {
        "policy": policy,
        "summary": {"by_session": [
            {"session": s, "closed_pnl": str(v)} for s, v in by_session.items()
        ]},
    }


def test_session_series_keys_by_session_and_pairs_by_hand_math() -> None:
    own = _run_doc("gepa:x", {"2026-09-21": 10.0, "2026-09-22": -4.0, "2026-09-23": 6.0})
    base = _run_doc("no_trade", {"2026-09-21": 1.0, "2026-09-22": 1.0, "2026-09-23": 1.0})
    entries = [
        PolicyEntry(policy="no_trade", kind="rules"),
        PolicyEntry(policy=lab.FIRST_ROW_POLICY, kind="rules"),
        PolicyEntry(policy="gepa:x", kind="model"),
    ]
    runs = {
        "gepa:x": [own],
        "no_trade": [base],
        lab.FIRST_ROW_POLICY: [base],  # first_row ties no_trade here
    }
    cols = _paired_columns(runs, entries, seed=7)
    # hand math: diffs are (10-1), (-4-1), (6-1) -> 9 - 5 + 5 = 9.0 over 3 sessions
    assert cols["gepa:x"]["vs_no_trade"]["diff_total"] == 9.0
    assert cols["gepa:x"]["vs_no_trade"]["sessions"] == 3
    assert "vs_no_trade" not in cols.get("no_trade", {})  # a control never pairs with itself
    assert "vs_first_row" not in cols["gepa:x"] or cols["gepa:x"]["vs_first_row"] is not None


def test_disjoint_sessions_pair_to_nothing() -> None:
    entries = [
        PolicyEntry(policy="no_trade", kind="rules"),
        PolicyEntry(policy="gepa:x", kind="model"),
    ]
    runs = {
        "gepa:x": [_run_doc("gepa:x", {"2026-08-01": 5.0})],
        "no_trade": [_run_doc("no_trade", {"2026-09-01": 5.0})],
    }
    assert _paired_columns(runs, entries, seed=7) == {}


# ------------------------------------------------------- digest carries them


def test_digest_carries_paired_columns_and_md_lines(tmp_path: Path) -> None:
    from tests.unit.test_desk_challenge import BoardTransport

    store = _store(tmp_path, {VINTAGE: multi_day_bundle(*DAYS)})
    document = run_challenge(
        store_root=store, now=T0, lab_root=tmp_path / "lab", transport=BoardTransport()
    )
    assert document["status"] == "ok"
    cards = {c["policy"]: c for c in document["rounds"][0]["scorecards"]}
    model_card = cards["model:zai"]
    assert model_card["vs_no_trade"]["sessions"] > 1
    assert model_card["vs_first_row"]["sessions"] > 1
    # the by_session invariant inside a real digest: paired diffs of the
    # no_trade card against ITSELF would be zero; instead check the columns
    # are consistent with the card's own totals: diff_total vs no_trade ==
    # closed_pnl_sum - no_trade's closed_pnl_sum (both sum the same sessions)
    assert Decimal(model_card["vs_no_trade"]["diff_total"]) == Decimal(
        model_card["closed_pnl_sum"]
    ) - Decimal(cards["no_trade"]["closed_pnl_sum"])
    markdown = (Path(document["digest_dir"]) / "digest.md").read_text()
    assert "vs_no_trade: diff" in markdown
    assert "vs_first_row: diff" in markdown
    digest = json.loads((Path(document["digest_dir"]) / "digest.json").read_bytes())
    assert digest["promotion"]["rule"].startswith("nothing is promoted by a digest")
