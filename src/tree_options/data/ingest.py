"""Ingestion: raw payload + master → normalized snapshot + manifest.

Pure and deterministic: the same payload, master, snapshot id, and code
SHA always produce the same snapshot. Every resolution failure is
aggregated and raised with the offending raw record ids — nothing is
silently dropped (silent exclusions are how survivorship bias enters).
"""

from __future__ import annotations

from tree_options.data.actions import CorporateActionRecord
from tree_options.data.bars import BarRecord
from tree_options.data.digest import canonical_bytes, sha256_hex
from tree_options.data.manifest import DatasetManifest, content_sha256
from tree_options.data.raw import RawActionRow, RawBarRow, RawPayload
from tree_options.data.resolve import AmbiguousTickerError, TickerResolver, UnknownTickerError
from tree_options.schemas.common import IdStr, StrictModel
from tree_options.schemas.security import SecurityMasterRecord


class IngestionError(ValueError):
    """One or more raw rows failed point-in-time resolution."""


class DatasetSnapshot(StrictModel):
    """The immutable unit of M1 data: normalized rows + master + manifest."""

    snapshot_id: IdStr
    master: tuple[SecurityMasterRecord, ...]
    bars: tuple[BarRecord, ...]
    actions: tuple[CorporateActionRecord, ...]
    manifest: DatasetManifest


def ingest_snapshot(
    payload: RawPayload,
    master: tuple[SecurityMasterRecord, ...] | list[SecurityMasterRecord],
    *,
    snapshot_id: str,
    normalization_code_sha: str,
) -> DatasetSnapshot:
    resolver = TickerResolver(master)
    bars: list[BarRecord] = []
    actions: list[CorporateActionRecord] = []
    failures: list[str] = []

    for row in payload.rows:
        try:
            if isinstance(row, RawBarRow):
                security_id = resolver.resolve(row.vendor_symbol, row.session, row.available_at)
                bars.append(
                    BarRecord(
                        security_id=security_id,
                        session=row.session,
                        open=row.open,
                        high=row.high,
                        low=row.low,
                        close=row.close,
                        volume=row.volume,
                        source=payload.provider,
                        source_record_id=row.source_record_id,
                        source_row_hash=sha256_hex(canonical_bytes(row)),
                        snapshot_id=snapshot_id,
                        available_at=row.available_at,
                    )
                )
            elif isinstance(row, RawActionRow):
                security_id = resolver.resolve(
                    row.vendor_symbol, row.effective_session, row.available_at
                )
                actions.append(
                    CorporateActionRecord(
                        security_id=security_id,
                        kind=row.kind.value,
                        effective_session=row.effective_session,
                        ratio_numerator=row.ratio_numerator,
                        ratio_denominator=row.ratio_denominator,
                        cash_amount=row.cash_amount,
                        successor_security_id=row.successor_security_id,
                        source=payload.provider,
                        source_record_id=row.source_record_id,
                        source_row_hash=sha256_hex(canonical_bytes(row)),
                        snapshot_id=snapshot_id,
                        available_at=row.available_at,
                    )
                )
        except (UnknownTickerError, AmbiguousTickerError) as exc:
            failures.append(f"{row.source_record_id}: {exc}")

    if failures:
        raise IngestionError("; ".join(failures))

    sessions = sorted(b.session for b in bars)
    row_hashes = sorted(
        [sha256_hex(canonical_bytes(r)) for r in payload.bars]
        + [sha256_hex(canonical_bytes(r)) for r in payload.actions]
    )
    manifest = DatasetManifest(
        provider=payload.provider,
        snapshot_id=snapshot_id,
        normalization_code_sha=normalization_code_sha,
        retrieved_at=payload.retrieved_at,
        known_exclusions=payload.known_exclusions,
        source_row_count=len(payload.bars) + len(payload.actions),
        bar_count=len(bars),
        action_count=len(actions),
        security_count=len({b.security_id for b in bars}),
        session_coverage=(sessions[0], sessions[-1]) if sessions else None,
        source_row_hashes=tuple(row_hashes),
        content_sha256=content_sha256(tuple(master), bars, actions),
    )
    return DatasetSnapshot(
        snapshot_id=snapshot_id,
        master=tuple(master),
        bars=tuple(bars),
        actions=tuple(actions),
        manifest=manifest,
    )
