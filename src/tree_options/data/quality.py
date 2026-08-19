"""Fail-closed data-quality gates (M1 packet workstream E).

Silent bad data is worse than no data: duplicate bars inflate panels,
off-session bars fake history, pre-close publication leaks, and an
unrepresented split corrupts every label that spans it. Every gate
raises with the offending source_record_id so the operator can trace
the row back to raw.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from itertools import pairwise

from tree_options.data.actions import ActionKind, CorporateActionRecord
from tree_options.data.bars import BarRecord
from tree_options.data.ingest import DatasetSnapshot
from tree_options.data.manifest import MANIFEST_SCHEMA_VERSION, content_sha256
from tree_options.time.calendar import StaticSessionCalendar

# Declared engineering defaults (not claimed optimal): an overnight close
# factor at/beyond these bounds requires a covering split/reverse-split
# action, and a declared action must match the observed factor to 2%.
SPLIT_FACTOR_BOUND = Decimal("2")
SPLIT_FACTOR_INVERSE = Decimal("0.5")
SPLIT_RATIO_TOLERANCE = Decimal("0.02")


class DataQualityError(ValueError):
    """A snapshot violates a data-quality gate; nothing is silently dropped."""


def validate_snapshot(snapshot: DatasetSnapshot, calendar: StaticSessionCalendar) -> None:
    known = {r.security_id for r in snapshot.master}

    seen: set[tuple[str, date]] = set()
    for bar in sorted(snapshot.bars, key=lambda b: (b.security_id, b.session, b.source_record_id)):
        key = (bar.security_id, bar.session)
        if key in seen:
            raise DataQualityError(
                f"duplicate bar for {bar.security_id} on {bar.session} ({bar.source_record_id})"
            )
        seen.add(key)
        if bar.security_id not in known:
            raise DataQualityError(
                f"unknown security id {bar.security_id} ({bar.source_record_id})"
            )
        if not calendar.is_session(bar.session):
            raise DataQualityError(f"bar {bar.source_record_id} on non-session date {bar.session}")
        if bar.available_at < calendar.session_close(bar.session):
            raise DataQualityError(
                f"bar {bar.source_record_id} published {bar.available_at} before "
                f"session close {calendar.session_close(bar.session)}"
            )

    for action in snapshot.actions:
        if action.security_id not in known:
            raise DataQualityError(
                f"unknown security id {action.security_id} ({action.source_record_id})"
            )
        if not calendar.is_session(action.effective_session):
            raise DataQualityError(
                f"action {action.source_record_id} on non-session date {action.effective_session}"
            )

    by_security: dict[str, list[BarRecord]] = {}
    for bar in snapshot.bars:
        by_security.setdefault(bar.security_id, []).append(bar)
    for bars in by_security.values():
        bars.sort(key=lambda b: b.session)

    ratio_actions = {
        (a.security_id, a.effective_session): a
        for a in snapshot.actions
        if a.kind in (ActionKind.SPLIT, ActionKind.REVERSE_SPLIT)
    }

    for security_id, bars in by_security.items():
        for prev, nxt in pairwise(bars):
            factor = prev.close / nxt.close
            if factor <= SPLIT_FACTOR_INVERSE or factor >= SPLIT_FACTOR_BOUND:
                covering = ratio_actions.get((security_id, nxt.session))
                if covering is None:
                    raise DataQualityError(
                        f"undeclared price discontinuity: {security_id} "
                        f"{prev.session}->{nxt.session} factor {factor}"
                    )
                _require_ratio_match(covering, factor)

    for (security_id, effective), action in ratio_actions.items():
        security_bars = by_security.get(security_id, [])
        before = [b for b in security_bars if b.session < effective]
        effective_bars = [b for b in security_bars if b.session == effective]
        if not before or not effective_bars:
            raise DataQualityError(
                f"declared {action.kind} {action.source_record_id} has no price "
                f"transition into {effective}"
            )
        factor = max(before, key=lambda b: b.session).close / effective_bars[0].close
        _require_ratio_match(action, factor)


def _require_ratio_match(action: CorporateActionRecord, factor: Decimal) -> None:
    assert action.ratio_numerator is not None and action.ratio_denominator is not None
    ratio = Decimal(action.ratio_numerator) / Decimal(action.ratio_denominator)
    if abs(factor / ratio - Decimal(1)) > SPLIT_RATIO_TOLERANCE:
        raise DataQualityError(
            f"declared {action.kind} {action.source_record_id} without matching "
            f"price effect: observed factor {factor}, declared ratio {ratio}"
        )


def verify_manifest(snapshot: DatasetSnapshot, calendar: StaticSessionCalendar) -> None:
    """The manifest is bound to content AND its own metadata: post-ingest
    swaps of rows, master records, provider, or counts must all fail
    (review round 1, P1-1)."""
    m = snapshot.manifest
    rows_identity = all(b.snapshot_id == m.snapshot_id for b in snapshot.bars) and all(
        a.snapshot_id == m.snapshot_id for a in snapshot.actions
    )
    identity_ok = snapshot.snapshot_id == m.snapshot_id and rows_identity
    if not identity_ok:
        raise DataQualityError(
            f"snapshot identity mismatch: outer id {snapshot.snapshot_id!r}, manifest "
            f"id {m.snapshot_id!r}, or a row disagrees — the id cannot be rebound"
        )
    expected = content_sha256(snapshot.master, snapshot.bars, snapshot.actions)
    if expected != snapshot.manifest.content_sha256:
        raise DataQualityError(
            f"manifest content mismatch for {snapshot.snapshot_id}: "
            f"manifest says {snapshot.manifest.content_sha256}, rows hash to {expected}"
        )
    if m.schema_version != MANIFEST_SCHEMA_VERSION:
        raise DataQualityError(
            f"manifest schema version mismatch for {snapshot.snapshot_id}: "
            f"claims {m.schema_version!r}, this code writes {MANIFEST_SCHEMA_VERSION!r}"
        )
    if m.bar_count != len(snapshot.bars) or m.action_count != len(snapshot.actions):
        raise DataQualityError(
            f"manifest count mismatch for {snapshot.snapshot_id}: "
            f"claims {m.bar_count}/{m.action_count}, snapshot has "
            f"{len(snapshot.bars)}/{len(snapshot.actions)}"
        )
    if m.source_row_count != len(snapshot.bars) + len(snapshot.actions):
        raise DataQualityError(
            f"manifest source-row count mismatch for {snapshot.snapshot_id}: "
            f"claims {m.source_row_count}, snapshot has "
            f"{len(snapshot.bars) + len(snapshot.actions)}"
        )
    if m.security_count != len({b.security_id for b in snapshot.bars}):
        raise DataQualityError(
            f"manifest security count mismatch for {snapshot.snapshot_id}: "
            f"claims {m.security_count}, snapshot has "
            f"{len({b.security_id for b in snapshot.bars})}"
        )
    sources = {b.source for b in snapshot.bars} | {a.source for a in snapshot.actions}
    if sources and sources != {m.provider}:
        raise DataQualityError(
            f"manifest provider mismatch for {snapshot.snapshot_id}: "
            f"manifest says {m.provider!r}, rows carry {sorted(sources)}"
        )
    row_hashes = tuple(
        sorted(
            [b.source_row_hash for b in snapshot.bars]
            + [a.source_row_hash for a in snapshot.actions]
        )
    )
    if row_hashes != m.source_row_hashes:
        raise DataQualityError(f"manifest source-row hash list mismatch for {snapshot.snapshot_id}")
    sessions = sorted(b.session for b in snapshot.bars)
    coverage = (sessions[0], sessions[-1]) if sessions else None
    if coverage != m.session_coverage:
        raise DataQualityError(
            f"manifest session coverage mismatch for {snapshot.snapshot_id}: "
            f"manifest says {m.session_coverage}, bars span {coverage}"
        )
    validate_snapshot(snapshot, calendar)
