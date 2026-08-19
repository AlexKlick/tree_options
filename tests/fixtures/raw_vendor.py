"""M1 vendor-shaped raw payload (handoff §6.4 + M1 packet workstreams B/D).

Synthetic vendor delivery covering: a rename (NEWM→OLDA on SEC-001), a 2:1
split, a cash dividend, a terminal delisting, and ticker REUSE (SEC-002
takes "NEWM" over after SEC-001 dies). Prices are deliberately round and
the split discontinuity is exact so the quality gates are deterministic.
All dates are real NYSE sessions (static calendar 2018..2026).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from tree_options.schemas.security import (
    DelistingRecord,
    SectorMappingRecord,
    SecurityMasterRecord,
    TickerMappingRecord,
)

PROVIDER = "fixture-vendor-v1"
RETRIEVED_AT = datetime(2024, 12, 31, 2, 0, tzinfo=UTC)  # fixed: manifests must be deterministic
KNOWN_EXCLUSIONS: tuple[str, ...] = ("SEC-003: no vendor coverage (fixture)",)


def _pub(session: date) -> datetime:
    """Vendor publication instant: 23:00 UTC on the session day — after the
    close in both DST states, before the next session."""
    return datetime(session.year, session.month, session.day, 23, 0, tzinfo=UTC)


def m1_master() -> tuple[SecurityMasterRecord, ...]:
    """The M0 fixture securities + SEC-005 (reverse-split fixture)."""
    from tests.fixtures.security import (
        finite_listing_end_security,
        renamed_and_delisted_security,
        successor_security_on_old_ticker,
    )

    rvsp = SecurityMasterRecord(
        security_id="SEC-005",
        figi="BBG000FIXTUR5",
        cik="0000000505",
        listing_start=date(2024, 1, 2),
        listing_end=None,
        exchange="NYSE",
        source="sec-master-fixture",
        available_at=datetime(2024, 1, 2, 21, 0, tzinfo=UTC),
        ticker_mappings=(
            TickerMappingRecord(
                security_id="SEC-005",
                ticker="RVSP",
                effective_from=date(2024, 1, 2),
                available_at=datetime(2024, 1, 2, 21, 0, tzinfo=UTC),
            ),
        ),
        sector_mappings=(
            SectorMappingRecord(
                security_id="SEC-005",
                sector="DISC",
                effective_from=date(2024, 1, 2),
                available_at=datetime(2024, 1, 2, 21, 0, tzinfo=UTC),
            ),
        ),
    )
    # SEC-006: acquired — merger action names the successor, then a terminal
    # delisting; SEC-007: chapter-11 terminal delisting with NO final price.
    merged = SecurityMasterRecord(
        security_id="SEC-006",
        figi="BBG000FIXTUR6",
        cik="0000000606",
        listing_start=date(2024, 1, 2),
        listing_end=date(2024, 8, 15),
        exchange="NASDAQ",
        source="sec-master-fixture",
        available_at=datetime(2024, 1, 2, 21, 0, tzinfo=UTC),
        ticker_mappings=(
            TickerMappingRecord(
                security_id="SEC-006",
                ticker="MRGD",
                effective_from=date(2024, 1, 2),
                effective_to=date(2024, 8, 15),
                available_at=datetime(2024, 1, 2, 21, 0, tzinfo=UTC),
            ),
        ),
        delisting=DelistingRecord(
            delisting_session=date(2024, 8, 15),
            reason="merger",
            final_price_available=True,
            available_at=datetime(2024, 8, 15, 20, 0, tzinfo=UTC),
        ),
        sector_mappings=(
            SectorMappingRecord(
                security_id="SEC-006",
                sector="HLTH",
                effective_from=date(2024, 1, 2),
                available_at=datetime(2024, 1, 2, 21, 0, tzinfo=UTC),
            ),
        ),
    )
    bankrupt = SecurityMasterRecord(
        security_id="SEC-007",
        figi="BBG000FIXTUR7",
        cik="0000000707",
        listing_start=date(2024, 1, 2),
        listing_end=date(2024, 10, 1),
        exchange="NASDAQ",
        source="sec-master-fixture",
        available_at=datetime(2024, 1, 2, 21, 0, tzinfo=UTC),
        ticker_mappings=(
            TickerMappingRecord(
                security_id="SEC-007",
                ticker="BNKR",
                effective_from=date(2024, 1, 2),
                effective_to=date(2024, 10, 1),
                available_at=datetime(2024, 1, 2, 21, 0, tzinfo=UTC),
            ),
        ),
        delisting=DelistingRecord(
            delisting_session=date(2024, 10, 1),
            reason="bankruptcy_11",
            final_price_available=False,
            available_at=datetime(2024, 10, 1, 20, 0, tzinfo=UTC),
        ),
        sector_mappings=(
            SectorMappingRecord(
                security_id="SEC-007",
                sector="ENRG",
                effective_from=date(2024, 1, 2),
                available_at=datetime(2024, 1, 2, 21, 0, tzinfo=UTC),
            ),
        ),
    )
    return (
        renamed_and_delisted_security(),
        finite_listing_end_security(),
        successor_security_on_old_ticker(),
        rvsp,
        merged,
        bankrupt,
    )


def raw_rows(
    *,
    post_split_close: str = "40.00",
    include_split_action: bool = True,
) -> tuple[dict[str, object], ...]:
    """Vendor bar/action rows as plain dicts (exactly as delivered).

    Variants exist for the quality-gate tests: an undeclared split
    (include_split_action=False) and a declared-but-unreflected split
    (post_split_close near the pre-split price).
    """
    bars: list[dict[str, object]] = [
        # SEC-001 under NEWM (pre-rename)
        dict(
            vendor_symbol="NEWM",
            session=date(2024, 1, 2),
            open="50.00",
            high="50.10",
            low="49.90",
            close="50.00",
            volume=1_000_000,
            available_at=_pub(date(2024, 1, 2)),
            source_record_id="RAW-0001",
        ),
        dict(
            vendor_symbol="NEWM",
            session=date(2024, 3, 14),
            open="51.80",
            high="52.20",
            low="51.60",
            close="52.00",
            volume=900_000,
            available_at=_pub(date(2024, 3, 14)),
            source_record_id="RAW-0002",
        ),
        # SEC-001 under OLDA (post-rename); the vendor symbol follows the rename
        dict(
            vendor_symbol="OLDA",
            session=date(2024, 3, 15),
            open="52.40",
            high="52.60",
            low="52.20",
            close="52.50",
            volume=910_000,
            available_at=_pub(date(2024, 3, 15)),
            source_record_id="RAW-0003",
        ),
        dict(
            vendor_symbol="OLDA",
            session=date(2024, 6, 28),
            open="79.00",
            high="80.50",
            low="78.80",
            close="80.00",
            volume=800_000,
            available_at=_pub(date(2024, 6, 28)),
            source_record_id="RAW-0004",
        ),
        # post-split session: 80.00 -> 40.00 exactly at the declared 2:1
        # (OHLC derives from the close so the without-effect variant stays coherent)
        dict(
            vendor_symbol="OLDA",
            session=date(2024, 7, 1),
            open=str(Decimal(post_split_close) + Decimal("0.10")),
            high=str(Decimal(post_split_close) + Decimal("0.60")),
            low=str(Decimal(post_split_close) - Decimal("0.10")),
            close=post_split_close,
            volume=1_600_000,
            available_at=_pub(date(2024, 7, 1)),
            source_record_id="RAW-0005",
        ),
        # SEC-002 (the successor issuer) under the recycled NEWM ticker
        # (2024-09-02 is Labor Day — real sessions are 09-03/09-04)
        dict(
            vendor_symbol="NEWM",
            session=date(2024, 9, 3),
            open="29.50",
            high="30.20",
            low="29.40",
            close="30.00",
            volume=500_000,
            available_at=_pub(date(2024, 9, 3)),
            source_record_id="RAW-0006",
        ),
        dict(
            vendor_symbol="NEWM",
            session=date(2024, 9, 4),
            open="30.40",
            high="31.20",
            low="30.30",
            close="31.00",
            volume=480_000,
            available_at=_pub(date(2024, 9, 4)),
            source_record_id="RAW-0007",
        ),
        # SEC-005 across a 1:2 reverse split: 10.00 -> 20.00 exactly
        dict(
            vendor_symbol="RVSP",
            session=date(2024, 7, 3),
            open="9.90",
            high="10.10",
            low="9.80",
            close="10.00",
            volume=200_000,
            available_at=_pub(date(2024, 7, 3)),
            source_record_id="RAW-0008",
        ),
        dict(
            vendor_symbol="RVSP",
            session=date(2024, 7, 5),
            open="19.90",
            high="20.10",
            low="19.80",
            close="20.00",
            volume=100_000,
            available_at=_pub(date(2024, 7, 5)),
            source_record_id="RAW-0009",
        ),
    ]
    actions: list[dict[str, object]] = [
        dict(
            vendor_symbol="OLDA",
            kind="cash_dividend",
            effective_session=date(2024, 5, 1),
            cash_amount="0.25",
            available_at=_pub(date(2024, 4, 29)),
            source_record_id="ACT-0001",
        ),
        dict(
            vendor_symbol="MRGD",
            kind="merger",
            effective_session=date(2024, 8, 15),
            successor_security_id="SEC-001",
            available_at=_pub(date(2024, 8, 14)),
            source_record_id="ACT-0004",
        ),
    ]
    if include_split_action:
        actions.append(
            dict(
                vendor_symbol="OLDA",
                kind="split",
                effective_session=date(2024, 7, 1),
                ratio_numerator=2,
                ratio_denominator=1,
                available_at=_pub(date(2024, 6, 28)),
                source_record_id="ACT-0002",
            ),
        )
        actions.append(
            dict(
                vendor_symbol="RVSP",
                kind="reverse_split",
                effective_session=date(2024, 7, 5),
                ratio_numerator=1,
                ratio_denominator=2,
                available_at=_pub(date(2024, 7, 3)),
                source_record_id="ACT-0003",
            ),
        )
    return tuple([*bars, *actions])


def delisting_available_at() -> datetime:
    """The SEC-001 delisting's knowable instant (matches the M0 fixture)."""
    return datetime(2024, 8, 2, 20, 0, tzinfo=UTC)


def sec001_delisting() -> DelistingRecord:
    return DelistingRecord(
        delisting_session=date(2024, 8, 2),
        reason="voluntary_delisting",
        final_price_available=True,
        available_at=delisting_available_at(),
    )


def renamed_record_with_delisting() -> SecurityMasterRecord:
    """SEC-001 with its delisting attached (the M0 fixture already carries it)."""
    from tests.fixtures.security import renamed_and_delisted_security

    return renamed_and_delisted_security()


def reused_ticker_window() -> tuple[TickerMappingRecord, TickerMappingRecord]:
    """The two 'NEWM' mappings, different issuers, disjoint windows."""
    return (
        TickerMappingRecord(
            security_id="SEC-001",
            ticker="NEWM",
            effective_from=date(2024, 1, 2),
            effective_to=date(2024, 3, 14),
            available_at=datetime(2024, 1, 2, 21, 0, tzinfo=UTC),
        ),
        TickerMappingRecord(
            security_id="SEC-002",
            ticker="NEWM",
            effective_from=date(2024, 9, 1),
            available_at=datetime(2024, 9, 1, 21, 0, tzinfo=UTC),
        ),
    )


SPLIT_RATIO = Decimal(2)  # declared 2:1 on SEC-001 effective 2024-07-01
