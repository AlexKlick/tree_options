"""Security-master fixture (handoff §7 fixture 3): rename + delisting.

SEC-001 lists as "NEWM" on 2024-01-02, renames to "OLDA" effective the
2024-03-15 session, and delists on 2024-08-02. Same security_id throughout —
the whole point of INV-08.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from tree_options.schemas.security import (
    DelistingRecord,
    SecurityMasterRecord,
    SectorMappingRecord,
    TickerMappingRecord,
)


def renamed_and_delisted_security() -> SecurityMasterRecord:
    return SecurityMasterRecord(
        security_id="SEC-001",
        figi="BBG000FIXTURE",
        cik="0000000042",
        listing_start=date(2024, 1, 2),
        listing_end=date(2024, 8, 2),
        exchange="NASDAQ",
        source="sec-master-fixture",
        available_at=datetime(2024, 1, 2, 21, 0, tzinfo=UTC),
        ticker_mappings=(
            TickerMappingRecord(
                security_id="SEC-001",
                ticker="NEWM",
                effective_from=date(2024, 1, 2),
                effective_to=date(2024, 3, 14),
                available_at=datetime(2024, 1, 2, 21, 0, tzinfo=UTC),
            ),
            TickerMappingRecord(
                security_id="SEC-001",
                ticker="OLDA",
                effective_from=date(2024, 3, 15),
                effective_to=date(2024, 8, 2),
                # rename knowable at the 2024-03-14 close (announced after hours)
                available_at=datetime(2024, 3, 14, 21, 0, tzinfo=UTC),
            ),
        ),
        sector_mappings=(
            SectorMappingRecord(
                security_id="SEC-001",
                sector="TECH",
                effective_from=date(2024, 1, 2),
                available_at=datetime(2024, 1, 2, 21, 0, tzinfo=UTC),
            ),
            # reclassified effective the 2024-04-02 reopen (2024-04-01 is
            # Easter Monday), knowable a week later — the gap is the leak
            # window proven in test_sector_pit.py
            SectorMappingRecord(
                security_id="SEC-001",
                sector="FIN",
                effective_from=date(2024, 4, 2),
                available_at=datetime(2024, 4, 10, 23, 0, tzinfo=UTC),
            ),
        ),
        delisting=DelistingRecord(
            delisting_session=date(2024, 8, 2),
            reason="voluntary_delisting",
            final_price_available=True,
            # delisting knowable only at the final session's close
            available_at=datetime(2024, 8, 2, 20, 0, tzinfo=UTC),
        ),
    )


def finite_listing_end_security() -> SecurityMasterRecord:
    """Lists 2024-01-02..2024-06-30 with NO delisting event: the only record
    of the end is the record's own listing_end (review round 3, F2)."""
    return SecurityMasterRecord(
        security_id="SEC-003",
        figi="BBG000FIXTUR3",
        cik="0000000011",
        listing_start=date(2024, 1, 2),
        listing_end=date(2024, 6, 30),
        exchange="NASDAQ",
        source="sec-master-fixture",
        available_at=datetime(2024, 1, 3, 21, 0, tzinfo=UTC),
        ticker_mappings=(
            TickerMappingRecord(
                security_id="SEC-003",
                ticker="FINL",
                effective_from=date(2024, 1, 2),
                effective_to=date(2024, 6, 30),
                available_at=datetime(2024, 1, 3, 21, 0, tzinfo=UTC),
            ),
        ),
        delisting=None,
    )


def successor_security_on_old_ticker() -> SecurityMasterRecord:
    """A DIFFERENT issuer that takes over the recycled ticker "NEWM" later.

    Joining by ticker instead of security_id welds this entity onto SEC-001's
    history — the spurious-security failure INV-08 exists to prevent.
    """
    return SecurityMasterRecord(
        security_id="SEC-002",
        figi="BBG000FIXTUR2",
        cik="0000000099",
        listing_start=date(2024, 9, 1),
        listing_end=None,
        exchange="NASDAQ",
        source="sec-master-fixture",
        available_at=datetime(2024, 9, 1, 21, 0, tzinfo=UTC),
        ticker_mappings=(
            TickerMappingRecord(
                security_id="SEC-002",
                ticker="NEWM",
                effective_from=date(2024, 9, 1),
                available_at=datetime(2024, 9, 1, 21, 0, tzinfo=UTC),
            ),
        ),
        sector_mappings=(
            # classified with its first session's bar publication
            SectorMappingRecord(
                security_id="SEC-002",
                sector="INDU",
                effective_from=date(2024, 9, 1),
                available_at=datetime(2024, 9, 3, 23, 0, tzinfo=UTC),
            ),
        ),
    )
