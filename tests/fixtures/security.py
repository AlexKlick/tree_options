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
            ),
            TickerMappingRecord(
                security_id="SEC-001",
                ticker="OLDA",
                effective_from=date(2024, 3, 15),
                effective_to=date(2024, 8, 2),
            ),
        ),
        delisting=DelistingRecord(
            delisting_session=date(2024, 8, 2),
            reason="voluntary_delisting",
            final_price_available=True,
        ),
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
            ),
        ),
    )
