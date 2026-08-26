"""The ratified final holdout window (0.2.1, owner decision 2026-08-26).

Window A is one of the four FIXED decisions of the ratification package:
EXACTLY the 13 enumerated dates, scoped to the lane-2 evaluation folds,
bound to the exit-5 census 43b0b040ea3c…. These tests pin the single
source (tree_options.protocol.holdout), the amendment builder's rendering
of it (proposal and landing cannot drift), and the census binding (a
build against any other census never carries the ratified window).
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from tests.unit.test_protocol_amendment import (  # noqa: E402
    RATIFICATION_CENSUS_SHA256,
    REAL_CAPTURE_MANIFEST,
    REAL_CENSUS,
    _build,
    _bundle,
    _census_hash,
    _make_census_bytes,
    _noop_rules,
    _out_root,
    _owner_deviation_values,
    _real_census_available,
)
from tree_options.data.coverage_census import CoverageCensus  # noqa: E402
from tree_options.protocol import amendment as amd  # noqa: E402
from tree_options.protocol.holdout import (  # noqa: E402
    FINAL_HOLDOUT_DATES,
    FINAL_HOLDOUT_PLACEHOLDER,
    FINAL_HOLDOUT_SCOPE,
    RATIFIED_HOLDOUT_CENSUS_SHA256,
    final_holdout_window_record,
)
from tree_options.time.calendar import StaticSessionCalendar  # noqa: E402

CALENDAR = StaticSessionCalendar(
    REPO_ROOT / "data" / "calendar" / "nyse_sessions_2018_01_02_2026_12_31.json",
    REPO_ROOT / "data" / "calendar" / "nyse_sessions_2018_01_02_2026_12_31.sha256",
)


def test_ratified_window_is_exactly_the_13_friday_sessions() -> None:
    """Window A: EXACTLY 13 dates, strictly increasing, unique, every one a
    Friday session of the repo calendar (the lane-2 fold grid's sessions)."""
    assert len(FINAL_HOLDOUT_DATES) == 13
    assert len(set(FINAL_HOLDOUT_DATES)) == 13
    assert list(FINAL_HOLDOUT_DATES) == sorted(FINAL_HOLDOUT_DATES)
    for iso in FINAL_HOLDOUT_DATES:
        day = date.fromisoformat(iso)
        assert day.weekday() == 4, f"{iso} is not a Friday"
        assert CALENDAR.is_session(day), f"{iso} is not a session in the repo calendar"


def test_record_is_bound_to_the_ratified_census_only() -> None:
    """The enumeration record exists for census 43b0b040ea3c… and for NO
    other census — a foreign build can never carry the ratified window."""
    assert RATIFIED_HOLDOUT_CENSUS_SHA256 == RATIFICATION_CENSUS_SHA256
    record = final_holdout_window_record(RATIFIED_HOLDOUT_CENSUS_SHA256)
    assert record is not None
    assert tuple(record["dates"]) == FINAL_HOLDOUT_DATES
    assert record["scope"] == FINAL_HOLDOUT_SCOPE
    assert record["census_content_sha256"] == RATIFIED_HOLDOUT_CENSUS_SHA256
    assert record["decided"] == "2026-08-26"
    assert final_holdout_window_record("f" * 64) is None


def test_proposal_renders_placeholder_for_a_foreign_census() -> None:
    """A build against any census other than the ratified one renders the
    AWAITING_OWNER_DECLARATION placeholder — never a misbound enumeration."""
    census_bytes = _make_census_bytes()
    census = CoverageCensus.model_validate_json(census_bytes)
    text = amd._render_schema_addition_proposal(
        census=census, census_hash=_census_hash(census_bytes), flow_value=100
    )
    assert "NOT_LANDED: true" in text
    # compare through the parsed document: yaml wraps long plain scalars, so
    # a raw substring match on the placeholder sentence is not robust.
    assert yaml.safe_load(text)["proposed_schema_addition"]["final_holdout_window"] == (
        FINAL_HOLDOUT_PLACEHOLDER
    )
    for iso in FINAL_HOLDOUT_DATES:
        assert iso not in text, "the ratified enumeration must not attach to a foreign census"


def test_proposal_renders_the_enumeration_for_the_ratified_census() -> None:
    """The proposal renders the FULL ratified enumeration from the single
    source — every date, the scope, the census binding, and the landing
    contract — so the later schema-addition landing can be diffed against
    it exactly."""
    census_bytes = _make_census_bytes()
    census = CoverageCensus.model_validate_json(census_bytes)
    text = amd._render_schema_addition_proposal(
        census=census, census_hash=RATIFIED_HOLDOUT_CENSUS_SHA256, flow_value=100
    )
    assert "NOT_LANDED: true" in text
    assert FINAL_HOLDOUT_PLACEHOLDER not in text
    doc = yaml.safe_load(text)
    rendered = doc["proposed_schema_addition"]["final_holdout_window"]
    assert rendered == final_holdout_window_record(RATIFIED_HOLDOUT_CENSUS_SHA256), (
        "the rendered window is not exactly the single-source record"
    )
    for iso in FINAL_HOLDOUT_DATES:
        assert iso in text


@pytest.mark.skipif(not _real_census_available, reason="coverage-era artifacts not present")
def test_emitted_packet_carries_the_enumeration(tmp_path: Path) -> None:
    """End to end: the dry-run build against the REAL exit-5 census (the
    admitted owner_deviation path) emits a schema-addition-proposal.yaml
    whose final_holdout_window block is exactly the single-source record."""
    census_bytes = REAL_CENSUS.read_bytes()
    paths = _bundle(
        tmp_path,
        census_bytes=census_bytes,
        owner_values=_owner_deviation_values(RATIFICATION_CENSUS_SHA256),
        rules=_noop_rules(RATIFICATION_CENSUS_SHA256),
        manifest_body=REAL_CAPTURE_MANIFEST.read_bytes(),
    )
    with _out_root() as out:
        packet = _build(paths, out)
        assert packet.landed is False
        proposal = (
            out / _census_hash(census_bytes)[:12] / "schema-addition-proposal.yaml"
        ).read_text(encoding="utf-8")
    doc = yaml.safe_load(proposal)
    assert doc["NOT_LANDED"] is True
    assert doc["proposed_schema_addition"]["final_holdout_window"] == final_holdout_window_record(
        RATIFICATION_CENSUS_SHA256
    )
