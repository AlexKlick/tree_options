"""Workstream A: PIT sector membership (M2 packet §3.A).

Sector is a dated, availability-gated classification like a ticker mapping:
a reclassification is INVISIBLE until its available_at instant, and a
security with no (visible) mapping is honestly sector-less (None) — never
a leaked future classification. Recorded in the synthetic-era packet,
owner sign-off 2026-08-18.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from tests.fixtures.raw_vendor import PROVIDER, m1_master, raw_rows
from tests.fixtures.security import (
    finite_listing_end_security,
    renamed_and_delisted_security,
)
from tree_options.data.ingest import ingest_snapshot
from tree_options.schemas.security import SectorMappingRecord, SecurityMasterRecord


def _at(day: date, hour: int = 16) -> datetime:
    return datetime(day.year, day.month, day.day, hour, 0, tzinfo=UTC)


# SEC-001 reclassification instants (real NYSE sessions): effective on the
# 2024-04-02 reopen after Easter, knowable a week later.
RECLASS_EFFECTIVE = date(2024, 4, 2)
RECLASS_AVAILABLE = datetime(2024, 4, 10, 23, 0, tzinfo=UTC)


def test_reclassification_visible_only_after_available_at() -> None:
    sec = renamed_and_delisted_security()
    # before the effective date: original sector
    assert sec.sector_on(date(2024, 3, 28), as_of=_at(date(2024, 3, 28))) == "TECH"
    # after effective AND available: new sector
    assert sec.sector_on(date(2024, 4, 15), as_of=_at(date(2024, 4, 15))) == "FIN"


def test_leak_window_returns_prior_sector() -> None:
    """Decision between effective_from and available_at must NOT see the
    reclassification — the gap is exactly the leak window."""
    sec = renamed_and_delisted_security()
    between = datetime(2024, 4, 5, 16, 0, tzinfo=UTC)
    assert RECLASS_EFFECTIVE < between.date() < RECLASS_AVAILABLE.date()
    assert sec.sector_on(between.date(), as_of=between) == "TECH"
    # retrospective (settled) view sees the new sector from the effective date
    assert sec.sector_on(RECLASS_EFFECTIVE, as_of=None) == "FIN"


def test_unclassified_security_returns_none() -> None:
    sec = finite_listing_end_security()
    assert sec.sector_on(date(2024, 6, 3), as_of=_at(date(2024, 6, 3))) is None
    assert sec.sector_on(date(2024, 6, 3), as_of=None) is None


def test_mapping_before_effective_date_returns_none() -> None:
    from tree_options.schemas.security import TickerMappingRecord

    sec = SecurityMasterRecord(
        security_id="SEC-L8",
        listing_start=date(2024, 1, 2),
        exchange="NASDAQ",
        source="t",
        available_at=_at(date(2024, 1, 2)),
        ticker_mappings=(
            TickerMappingRecord(
                security_id="SEC-L8",
                ticker="LATE",
                effective_from=date(2024, 1, 2),
                available_at=_at(date(2024, 1, 2)),
            ),
        ),
        sector_mappings=(
            # record is visible from January, but the classification only
            # becomes effective in July — before that the answer is None
            SectorMappingRecord(
                security_id="SEC-L8",
                sector="MATR",
                effective_from=date(2024, 7, 1),
                available_at=_at(date(2024, 7, 1)),
            ),
        ),
    )
    assert sec.sector_on(date(2024, 6, 28), as_of=_at(date(2024, 6, 28))) is None
    assert sec.sector_on(date(2024, 7, 1), as_of=_at(date(2024, 7, 1))) == "MATR"


def test_invisible_record_fails_closed() -> None:
    sec = renamed_and_delisted_security()
    before_record = datetime(2024, 1, 1, 16, 0, tzinfo=UTC)
    with pytest.raises(KeyError):
        sec.sector_on(date(2024, 1, 3), as_of=before_record)


def test_sector_mapping_security_id_mismatch_rejected() -> None:
    sec = renamed_and_delisted_security()
    bad = SectorMappingRecord(
        security_id="SEC-999",
        sector="TECH",
        effective_from=date(2024, 1, 2),
        available_at=_at(date(2024, 1, 2)),
    )
    payload = sec.model_dump()
    payload["sector_mappings"] = [*payload.get("sector_mappings", ()), bad.model_dump()]
    with pytest.raises(ValueError, match="sector mapping security_id mismatch"):
        SecurityMasterRecord.model_validate(payload)


def test_duplicate_effective_from_rejected() -> None:
    sec = renamed_and_delisted_security()
    payload = sec.model_dump()
    dup = SectorMappingRecord(
        security_id="SEC-001",
        sector="HEALTH",
        effective_from=RECLASS_EFFECTIVE,
        available_at=RECLASS_AVAILABLE,
    )
    payload["sector_mappings"] = [
        *payload.get("sector_mappings", ()),
        dup.model_dump(),
    ]
    with pytest.raises(ValueError, match="strictly increasing effective_from"):
        SecurityMasterRecord.model_validate(payload)


def test_manifest_schema_version_is_m2_1(static_calendar) -> None:  # type: ignore[no-untyped-def]
    """The sector schema change bumps the manifest protocol version."""
    from tree_options.data.raw import build_payload

    payload = build_payload(
        provider=PROVIDER,
        rows=raw_rows(),
        retrieved_at=datetime(2024, 12, 31, 2, 0, tzinfo=UTC),
    )
    snapshot = ingest_snapshot(
        payload,
        m1_master(),
        snapshot_id="sector-schema-check",
        normalization_code_sha="0" * 64,
    )
    assert snapshot.manifest.schema_version == "m2/1"
    # the fixture master carries sector mappings on some securities
    assert any(r.sector_mappings for r in m1_master())
