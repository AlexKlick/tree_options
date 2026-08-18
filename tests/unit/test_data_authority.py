"""M1 workstream C: the point-in-time query authority.

The research layer never receives arbitrary rows from a caller — it asks
the authority, which enforces available_at <= decision_at on every datum
and reconstructs the historical universe WITHOUT current-survivor
filtering (M1 acceptance 3/5; packet workstream C).
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from tests.fixtures import raw_vendor as rv
from tree_options.data.authority import PointInTimeDataset
from tree_options.data.ingest import ingest_snapshot
from tree_options.data.raw import build_payload

SNAPSHOT_ID = "snap-m1-fixture-001"
CODE_SHA = "0" * 40
UNIVERSE_ID = "pit-master-v1"


def _authority(static_calendar):
    payload = build_payload(
        provider=rv.PROVIDER,
        rows=rv.raw_rows(),
        retrieved_at=rv.RETRIEVED_AT,
        known_exclusions=rv.KNOWN_EXCLUSIONS,
    )
    snapshot = ingest_snapshot(
        payload, rv.m1_master(), snapshot_id=SNAPSHOT_ID, normalization_code_sha=CODE_SHA
    )
    return PointInTimeDataset(snapshot, static_calendar, universe_id=UNIVERSE_ID)


def _at(session: date, hour: int = 23) -> datetime:
    return datetime(session.year, session.month, session.day, hour, 0, tzinfo=UTC)


def test_universe_is_point_in_time_not_survivors(static_calendar):
    """February: SEC-001 alive, SEC-002 not yet IPO'd, SEC-005/006/007 listed.
    September: SEC-001 (delisted) and SEC-006 (merged away) OUT, SEC-002 IN.
    November: the chapter-11 name is gone too — the survivor-filtered view
    (everyone still trading today) is a different, wrong set at every date."""
    ds = _authority(static_calendar)
    february = ds.universe_as_of(_at(date(2024, 2, 1)))
    september = ds.universe_as_of(_at(date(2024, 9, 10)))
    november = ds.universe_as_of(_at(date(2024, 11, 1)))
    assert set(february) == {"SEC-001", "SEC-003", "SEC-005", "SEC-006", "SEC-007"}
    assert set(september) == {"SEC-002", "SEC-005", "SEC-007"}
    assert set(november) == {"SEC-002", "SEC-005"}


def test_merger_and_bankruptcy_delistings_are_point_in_time(static_calendar):
    """Acceptance 4 (merger + terminal delisting): the merger action names
    its successor and the target leaves the universe at its delisting; the
    bankruptcy delisting carries no final price."""
    payload = build_payload(
        provider=rv.PROVIDER,
        rows=rv.raw_rows(),
        retrieved_at=rv.RETRIEVED_AT,
        known_exclusions=rv.KNOWN_EXCLUSIONS,
    )
    snapshot = ingest_snapshot(
        payload, rv.m1_master(), snapshot_id=SNAPSHOT_ID, normalization_code_sha=CODE_SHA
    )
    master = {r.security_id: r for r in snapshot.master}
    assert master["SEC-006"].delisting is not None
    assert master["SEC-006"].delisting.reason == "merger"
    assert master["SEC-007"].delisting is not None
    assert master["SEC-007"].delisting.final_price_available is False
    mergers = [a for a in snapshot.actions if a.kind == "merger"]
    assert len(mergers) == 1
    assert mergers[0].security_id == "SEC-006"
    assert mergers[0].successor_security_id == "SEC-001"


def test_future_bar_is_invisible(static_calendar):
    """RAW-0005 (2024-07-01 bar) publishes 23:00 UTC; a 22:00 decision on
    the same day must not see it — only the 2024-06-28 bar."""
    ds = _authority(static_calendar)
    before = ds.visible_bars("SEC-001", _at(date(2024, 7, 1), hour=22))
    after = ds.visible_bars("SEC-001", _at(date(2024, 7, 2)))
    assert max(b.session for b in before) == date(2024, 6, 28)
    assert max(b.session for b in after) == date(2024, 7, 1)


def test_features_as_of_enforces_availability_and_provenance(static_calendar):
    """Packet acceptance 5: every feature carries provenance and
    available_at <= decision_at, on every returned row."""
    ds = _authority(static_calendar)
    decision_at = _at(date(2024, 7, 8))
    rows = ds.features_as_of(
        decision_at=decision_at, universe_id=UNIVERSE_ID, dataset_snapshot_id=SNAPSHOT_ID
    )
    by_sec = {r.security_id: r for r in rows}
    # SEC-003 has no bars (excluded by vendor, per known_exclusions); SEC-002's
    # first bar is 2024-09-03 — not yet visible; SEC-001 and SEC-005 have history.
    assert set(by_sec) == {"SEC-001", "SEC-005"}
    sec1 = by_sec["SEC-001"]
    feats = {f.feature_name: f for f in sec1.features}
    assert feats["ret_1"].value == pytest.approx(40.00 / 80.00 - 1)
    assert feats["dol_vol"].value == pytest.approx(40.00 * 1_600_000)
    sec5 = by_sec["SEC-005"]
    feats5 = {f.feature_name: f for f in sec5.features}
    assert feats5["ret_1"].value == pytest.approx(20.00 / 10.00 - 1)  # reverse split
    for row in rows:
        assert row.decision_session == date(2024, 7, 8)
        for f in row.features:
            assert f.available_at <= decision_at
            assert f.decision_at == decision_at
            assert f.source == rv.PROVIDER
            assert f.source_record_id and f.revision_id


def test_snapshot_identity_is_enforced(static_calendar):
    ds = _authority(static_calendar)
    with pytest.raises(ValueError, match="snapshot"):
        ds.features_as_of(
            decision_at=_at(date(2024, 7, 2)),
            universe_id=UNIVERSE_ID,
            dataset_snapshot_id="somebody-elses-snapshot",
        )
    with pytest.raises(ValueError, match="universe"):
        ds.features_as_of(
            decision_at=_at(date(2024, 7, 2)),
            universe_id="survivors-only",
            dataset_snapshot_id=SNAPSHOT_ID,
        )


def test_reverse_split_fixture_passes_quality(static_calendar):
    """Acceptance 4: the 1:2 reverse split (10 -> 20) validates clean."""
    ds = _authority(static_calendar)  # constructor runs verify_manifest -> validate
    bars = ds.visible_bars("SEC-005", _at(date(2024, 7, 8)))
    assert [b.session for b in bars] == [date(2024, 7, 3), date(2024, 7, 5)]
