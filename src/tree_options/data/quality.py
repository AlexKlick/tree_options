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
from tree_options.data.manifest import content_sha256
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
    """The manifest is bound to content: post-ingest row swaps must fail."""
    expected = content_sha256(snapshot.bars, snapshot.actions)
    if expected != snapshot.manifest.content_sha256:
        raise DataQualityError(
            f"manifest content mismatch for {snapshot.snapshot_id}: "
            f"manifest says {snapshot.manifest.content_sha256}, rows hash to {expected}"
        )
    validate_snapshot(snapshot, calendar)
