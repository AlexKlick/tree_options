"""The point-in-time query authority (M1 packet workstream C).

Research code never receives arbitrary rows from a caller: it asks this
object, which reconstructs the historical universe without current-
survivor filtering and enforces `available_at <= decision_at` on every
datum it returns. Joins are by security_id only — the API takes no
ticker anywhere, so the current-ticker join is structurally impossible
(resolver-level coverage is mutant M65). This layer is where the M0
quote-stream completeness boundary (owner decision 10) will land when
M1 grows a real vendor surface.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from tree_options.data.actions import CorporateActionRecord
from tree_options.data.bars import BarRecord
from tree_options.data.ingest import DatasetSnapshot
from tree_options.data.quality import verify_manifest
from tree_options.schemas.common import IdStr
from tree_options.schemas.features import FeatureEvent, PanelRow
from tree_options.time.calendar import StaticSessionCalendar

REVISION_ZERO = "r0"


class AuthorityError(ValueError):
    """Wrong snapshot or universe identity — read refused."""


class PointInTimeDataset:
    """Read gate over one validated DatasetSnapshot."""

    def __init__(
        self,
        snapshot: DatasetSnapshot,
        calendar: StaticSessionCalendar,
        *,
        universe_id: str,
    ) -> None:
        verify_manifest(snapshot, calendar)  # fail closed before any query
        self._snapshot = snapshot
        self._calendar = calendar
        self._universe_id = universe_id
        self._bars_by_security: dict[str, list[BarRecord]] = {}
        for bar in sorted(snapshot.bars, key=lambda b: (b.security_id, b.session)):
            self._bars_by_security.setdefault(bar.security_id, []).append(bar)

    @property
    def snapshot_id(self) -> IdStr:
        return self._snapshot.snapshot_id

    @property
    def actions(self) -> tuple[CorporateActionRecord, ...]:
        """Normalized corporate actions of the underlying snapshot.

        Label construction consumes these as OUTCOME-side adjustment
        facts (labels are never availability-gated — INV-06 purge
        protects them instead; see labels/build.py).
        """
        return self._snapshot.actions

    def universe_as_of(self, decision_at: datetime) -> tuple[str, ...]:
        """Listing membership at decision_at.date() under decision_at knowledge.

        Delisted names are members while they were listed; a name that
        IPO'd after the date is not; nothing is filtered by whether it
        survives to today (M1 acceptance 3).
        """
        members = [
            record.security_id
            for record in self._snapshot.master
            if record.listed_on(decision_at.date(), as_of=decision_at)
        ]
        return tuple(sorted(members))

    def visible_bars(self, security_id: str, decision_at: datetime) -> tuple[BarRecord, ...]:
        """Bars for one security knowable at decision_at, session-ordered."""
        return tuple(
            bar
            for bar in self._bars_by_security.get(security_id, ())
            if bar.available_at <= decision_at
        )

    def features_as_of(
        self,
        *,
        decision_at: datetime,
        universe_id: str,
        dataset_snapshot_id: str,
    ) -> tuple[PanelRow, ...]:
        """V1 price/volume feature snapshots for the point-in-time universe.

        Every feature is derived ONLY from bars visible at decision_at and
        carries the provenance of the bar it was computed from. Securities
        without enough visible history contribute no row (absent, not
        imputed) — missing data is an absent event, never a zero.
        """
        if dataset_snapshot_id != self._snapshot.snapshot_id:
            raise AuthorityError(
                f"dataset_snapshot_id {dataset_snapshot_id!r} does not match this "
                f"authority's snapshot {self._snapshot.snapshot_id!r}"
            )
        if universe_id != self._universe_id:
            raise AuthorityError(
                f"universe_id {universe_id!r} does not match this authority's "
                f"universe {self._universe_id!r}"
            )
        decision_session = decision_at.date()
        rows: list[PanelRow] = []
        for security_id in self.universe_as_of(decision_at):
            bars = self.visible_bars(security_id, decision_at)
            features = _v1_features(bars, decision_at, self._snapshot.manifest.provider)
            if features:
                rows.append(
                    PanelRow(
                        security_id=security_id,
                        decision_session=decision_session,
                        features=features,
                    )
                )
        return tuple(rows)


def _v1_features(
    bars: tuple[BarRecord, ...], decision_at: datetime, provider: str
) -> tuple[FeatureEvent, ...]:
    """V1: ret_1 (last close vs previous visible close) and dol_vol
    (last visible close x volume), with bar-level provenance."""
    builders: list[Callable[[], FeatureEvent]] = []

    if len(bars) >= 2:
        last, prev = bars[-1], bars[-2]

        def _ret_1() -> FeatureEvent:
            return FeatureEvent(
                security_id=last.security_id,
                feature_name="ret_1",
                value=float(last.close / prev.close - 1),
                observed_at=last.available_at,
                available_at=last.available_at,
                decision_at=decision_at,
                source=provider,
                source_record_id=last.source_record_id,
                revision_id=REVISION_ZERO,
            )

        builders.append(_ret_1)

    if bars:
        last = bars[-1]

        def _dol_vol() -> FeatureEvent:
            return FeatureEvent(
                security_id=last.security_id,
                feature_name="dol_vol",
                value=float(last.close * last.volume),
                observed_at=last.available_at,
                available_at=last.available_at,
                decision_at=decision_at,
                source=provider,
                source_record_id=last.source_record_id,
                revision_id=REVISION_ZERO,
            )

        builders.append(_dol_vol)

    return tuple(builder() for builder in builders)
