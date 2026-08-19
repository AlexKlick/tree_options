"""M1 workstream E: fail-closed data-quality gates.

Every gate here exists because silent bad data is worse than no data:
duplicate bars inflate panels, off-session bars fake history, pre-close
publication leaks, and unrepresented splits corrupt labels.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from tests.fixtures import raw_vendor as rv
from tree_options.data.ingest import ingest_snapshot
from tree_options.data.quality import DataQualityError, validate_snapshot
from tree_options.data.raw import build_payload

SNAPSHOT_ID = "snap-m1-fixture-001"
CODE_SHA = "0" * 40


def _snapshot(rows):
    payload = build_payload(
        provider=rv.PROVIDER,
        rows=rows,
        retrieved_at=rv.RETRIEVED_AT,
        known_exclusions=rv.KNOWN_EXCLUSIONS,
    )
    return ingest_snapshot(
        payload, rv.m1_master(), snapshot_id=SNAPSHOT_ID, normalization_code_sha=CODE_SHA
    )


def _dup_row():
    return dict(
        vendor_symbol="OLDA",
        session=date(2024, 7, 1),
        open="40.00",
        high="40.10",
        low="39.90",
        close="40.05",
        volume=10,
        available_at=datetime(2024, 7, 1, 23, 0, tzinfo=UTC),
        source_record_id="RAW-DUP",
    )


def test_clean_fixture_validates(static_calendar):
    validate_snapshot(_snapshot(rv.raw_rows()), static_calendar)  # no raise


def test_duplicate_bars_rejected(static_calendar):
    with pytest.raises(DataQualityError, match="duplicate"):
        validate_snapshot(_snapshot((*rv.raw_rows(), _dup_row())), static_calendar)


def test_bars_outside_valid_sessions_rejected(static_calendar):
    # 2024-07-06 is a Saturday
    bad = dict(
        vendor_symbol="OLDA",
        session=date(2024, 7, 6),
        open="40.00",
        high="40.10",
        low="39.90",
        close="40.00",
        volume=10,
        available_at=datetime(2024, 7, 8, 23, 0, tzinfo=UTC),
        source_record_id="RAW-SAT",
    )
    with pytest.raises(DataQualityError, match="RAW-SAT"):
        validate_snapshot(_snapshot((*rv.raw_rows(), bad)), static_calendar)


def test_bar_published_before_session_close_rejected(static_calendar):
    rows = list(rv.raw_rows())
    for r in rows:
        if r.get("source_record_id") == "RAW-0005":
            r["available_at"] = datetime(2024, 7, 1, 14, 0, tzinfo=UTC)  # pre-close
    with pytest.raises(DataQualityError, match="RAW-0005"):
        validate_snapshot(_snapshot(tuple(rows)), static_calendar)


def test_undeclared_price_discontinuity_rejected(static_calendar):
    """80.00 -> 40.00 overnight with NO split action is a defect."""
    with pytest.raises(DataQualityError, match="discontinuity"):
        validate_snapshot(_snapshot(rv.raw_rows(include_split_action=False)), static_calendar)


def test_declared_split_without_price_effect_rejected(static_calendar):
    """A declared 2:1 whose price barely moved is equally a defect."""
    with pytest.raises(DataQualityError, match="declared"):
        validate_snapshot(_snapshot(rv.raw_rows(post_split_close="79.50")), static_calendar)


def test_manifest_tampering_is_detected(static_calendar):
    """Packet workstream D: the manifest is bound to content — swapping a
    bar after ingest must not survive manifest verification."""
    from tree_options.data.quality import verify_manifest

    snapshot = _snapshot(rv.raw_rows())
    tampered_bars = tuple(
        b.model_copy(update={"close": b.close + 1}) if b.source_record_id == "RAW-0005" else b
        for b in snapshot.bars
    )
    tampered = snapshot.model_copy(update={"bars": tampered_bars}, deep=False)
    verify_manifest(snapshot, static_calendar)  # clean passes
    with pytest.raises(DataQualityError, match="manifest"):
        verify_manifest(tampered, static_calendar)


def test_unknown_security_in_actions_rejected(static_calendar):
    rows = (
        *rv.raw_rows(),
        dict(
            vendor_symbol="GHOST",
            kind="split",
            effective_session=date(2024, 7, 2),
            ratio_numerator=2,
            ratio_denominator=1,
            available_at=datetime(2024, 7, 1, 23, 0, tzinfo=UTC),
            source_record_id="ACT-GHOST",
        ),
    )
    with pytest.raises(Exception, match="ACT-GHOST"):
        _snapshot(rows)


def test_master_tampering_is_detected(static_calendar):
    """P1-1: the manifest must bind the MASTER too — swapping listing data
    after ingest changes universe_as_of and must not survive verification."""
    snapshot = _snapshot(rv.raw_rows())
    tampered_master = tuple(
        r.model_copy(update={"listing_end": None}) if r.security_id == "SEC-001" else r
        for r in snapshot.master
    )
    tampered = snapshot.model_copy(update={"master": tampered_master}, deep=False)
    from tree_options.data.quality import verify_manifest

    verify_manifest(snapshot, static_calendar)
    with pytest.raises(DataQualityError, match="manifest"):
        verify_manifest(tampered, static_calendar)


def test_manifest_metadata_is_bound(static_calendar):
    """P1-1: provider and counts are cross-bound — a swapped provider would
    be emitted as feature provenance."""
    from tree_options.data.quality import verify_manifest

    snapshot = _snapshot(rv.raw_rows())
    evil_provider = snapshot.manifest.model_copy(update={"provider": "evil-vendor"})
    with pytest.raises(DataQualityError, match="manifest"):
        verify_manifest(
            snapshot.model_copy(update={"manifest": evil_provider}, deep=False),
            static_calendar,
        )
    wrong_count = snapshot.manifest.model_copy(update={"bar_count": 0})
    with pytest.raises(DataQualityError, match="manifest"):
        verify_manifest(
            snapshot.model_copy(update={"manifest": wrong_count}, deep=False),
            static_calendar,
        )
    # P2-3: every recomputable metadata field is pinned, not just provider/bar_count
    for field, evil in (
        ("action_count", 0),
        ("source_row_count", 0),
        ("security_count", 99),
        ("session_coverage", None),
        ("source_row_hashes", ()),
        ("schema_version", "m1/999"),
    ):
        tampered_manifest = snapshot.manifest.model_copy(update={field: evil})
        with pytest.raises(DataQualityError, match="manifest"):
            verify_manifest(
                snapshot.model_copy(update={"manifest": tampered_manifest}, deep=False),
                static_calendar,
            )


def test_snapshot_identity_is_bound(static_calendar):
    """P1-3: the outer snapshot id cannot be rebound post-ingest — it must
    agree with the manifest AND every row's snapshot_id."""
    from tree_options.data.quality import verify_manifest

    snapshot = _snapshot(rv.raw_rows())
    renamed = snapshot.model_copy(update={"snapshot_id": "renamed-snap"}, deep=False)
    verify_manifest(snapshot, static_calendar)
    with pytest.raises(DataQualityError, match="snapshot"):
        verify_manifest(renamed, static_calendar)
    rebound_row = tuple(
        b.model_copy(update={"snapshot_id": "renamed-snap"})
        if b.source_record_id == "RAW-0001"
        else b
        for b in snapshot.bars
    )
    with pytest.raises(DataQualityError, match="snapshot"):
        verify_manifest(
            snapshot.model_copy(update={"bars": rebound_row}, deep=False), static_calendar
        )
