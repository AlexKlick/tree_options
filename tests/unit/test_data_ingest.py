"""M1 workstreams B/D: ingestion, provenance, deterministic manifests.

Acceptance: every normalized row traces to its raw record (packet §1);
two clean ingestions of the same source produce byte-identical manifests
(packet §8); symbol resolution is point-in-time (packet workstream C's
join authority — ticker reuse must not weld issuers together).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from tests.fixtures import raw_vendor as rv
from tree_options.data.digest import canonical_bytes, sha256_hex
from tree_options.data.ingest import IngestionError, ingest_snapshot
from tree_options.data.raw import build_payload
from tree_options.data.resolve import AmbiguousTickerError, TickerResolver, UnknownTickerError

SNAPSHOT_ID = "snap-m1-fixture-001"
CODE_SHA = "0" * 40


def _ingest(rows=None):
    payload = build_payload(
        provider=rv.PROVIDER,
        rows=rv.raw_rows() if rows is None else rows,
        retrieved_at=rv.RETRIEVED_AT,
        known_exclusions=rv.KNOWN_EXCLUSIONS,
    )
    return ingest_snapshot(
        payload, rv.m1_master(), snapshot_id=SNAPSHOT_ID, normalization_code_sha=CODE_SHA
    )


def _by_key(snapshot):
    return {(b.security_id, b.session): b for b in snapshot.bars}


def test_manifest_is_byte_identical_on_re_ingest():
    a = _ingest()
    b = _ingest()
    assert canonical_bytes(a.manifest) == canonical_bytes(b.manifest)
    assert a.manifest.content_sha256 == b.manifest.content_sha256


def test_every_bar_traces_to_its_raw_row():
    """Packet acceptance 1: normalized rows trace to exact raw records."""
    from tree_options.data.bars import RawBarRow

    snapshot = _ingest()
    raw_by_id = {
        r.source_record_id: r
        for r in build_payload(provider=rv.PROVIDER, rows=rv.raw_rows()).rows
        if isinstance(r, RawBarRow)
    }
    assert len(snapshot.bars) == len(raw_by_id)
    for bar in snapshot.bars:
        raw = raw_by_id[bar.source_record_id]
        assert bar.source_row_hash == sha256_hex(canonical_bytes(raw))
        assert bar.source == rv.PROVIDER
        assert bar.snapshot_id == SNAPSHOT_ID
        assert bar.available_at == raw.available_at
        assert bar.close == raw.close


def test_ingest_resolves_symbols_point_in_time():
    """The rename and the reuse both resolve through dated mappings."""
    snapshot = _ingest()
    bars = _by_key(snapshot)
    assert bars[("SEC-001", date(2024, 3, 14))].close == Decimal("52.00")  # NEWM era
    assert bars[("SEC-001", date(2024, 3, 15))].close == Decimal("52.50")  # OLDA era
    # the recycled NEWM belongs to the SUCCESSOR issuer, never SEC-001
    assert bars[("SEC-002", date(2024, 9, 3))].close == Decimal("30.00")
    assert not any(
        b.security_id == "SEC-001" and b.session >= date(2024, 9, 1) for b in snapshot.bars
    )


def test_bars_carry_provenance_and_snapshot_id():
    snapshot = _ingest()
    for bar in snapshot.bars:
        assert bar.source and bar.source_record_id and bar.source_row_hash
        assert bar.snapshot_id == SNAPSHOT_ID


def test_unknown_vendor_symbol_fails_ingestion():
    rows = (
        *rv.raw_rows(),
        dict(
            vendor_symbol="GHOST",
            session=date(2024, 7, 2),
            open="10.00",
            high="10.10",
            low="9.90",
            close="10.00",
            volume=100,
            available_at=datetime(2024, 7, 2, 23, 0, tzinfo=UTC),
            source_record_id="RAW-GHOST",
        ),
    )
    with pytest.raises(IngestionError, match="RAW-GHOST"):
        _ingest(rows)


def test_impossible_prices_rejected_at_the_schema():
    with pytest.raises(ValidationError):
        build_payload(
            provider=rv.PROVIDER,
            rows=(
                dict(
                    vendor_symbol="OLDA",
                    session=date(2024, 7, 2),
                    open="10.00",
                    high="9.00",
                    low="9.50",
                    close="9.75",
                    volume=100,
                    available_at=datetime(2024, 7, 2, 23, 0, tzinfo=UTC),
                    source_record_id="RAW-BADH",
                ),
            ),
        )


def test_negative_volume_rejected_at_the_schema():
    with pytest.raises(ValidationError):
        build_payload(
            provider=rv.PROVIDER,
            rows=(
                dict(
                    vendor_symbol="OLDA",
                    session=date(2024, 7, 2),
                    open="10.00",
                    high="10.10",
                    low="9.90",
                    close="10.00",
                    volume=-5,
                    available_at=datetime(2024, 7, 2, 23, 0, tzinfo=UTC),
                    source_record_id="RAW-BADV",
                ),
            ),
        )


def test_ticker_resolution_is_point_in_time():
    resolver = TickerResolver(rv.m1_master())
    # the OLDA rename mapping was announced 2024-03-14 21:00 UTC
    with pytest.raises(UnknownTickerError):
        resolver.resolve("OLDA", date(2024, 3, 15), datetime(2024, 3, 14, 20, 0, tzinfo=UTC))
    got = resolver.resolve("OLDA", date(2024, 3, 15), datetime(2024, 3, 14, 22, 0, tzinfo=UTC))
    assert got == "SEC-001"
    # reuse: same ticker, different issuer, disjoint windows
    assert (
        resolver.resolve("NEWM", date(2024, 1, 2), datetime(2024, 1, 2, 23, 0, tzinfo=UTC))
        == "SEC-001"
    )
    assert (
        resolver.resolve("NEWM", date(2024, 9, 2), datetime(2024, 9, 2, 23, 0, tzinfo=UTC))
        == "SEC-002"
    )


def test_ambiguous_ticker_windows_raise():
    from tree_options.schemas.security import SecurityMasterRecord, TickerMappingRecord

    def _mk(sec, eff_from, avail):
        return SecurityMasterRecord(
            security_id=sec,
            listing_start=eff_from,
            exchange="NASDAQ",
            source="t",
            available_at=avail,
            ticker_mappings=(
                TickerMappingRecord(
                    security_id=sec, ticker="TWO", effective_from=eff_from, available_at=avail
                ),
            ),
        )

    clash = (
        _mk("SEC-A", date(2024, 1, 2), datetime(2024, 1, 2, 21, 0, tzinfo=UTC)),
        _mk("SEC-B", date(2024, 1, 2), datetime(2024, 1, 2, 21, 0, tzinfo=UTC)),
    )
    resolver = TickerResolver(clash)
    with pytest.raises(AmbiguousTickerError):
        resolver.resolve("TWO", date(2024, 2, 1), datetime(2024, 2, 1, 23, 0, tzinfo=UTC))
