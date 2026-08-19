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

import math
from bisect import bisect_right
from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal
from itertools import pairwise

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
        self._ordinals = {s: i for i, s in enumerate(calendar.sessions())}
        by_security: dict[str, list[BarRecord]] = {}
        for bar in sorted(snapshot.bars, key=lambda b: (b.security_id, b.session)):
            by_security.setdefault(bar.security_id, []).append(bar)
        # (session-ordered bars, their publication instants, monotone flag).
        # When publication order follows session order (r0 data: one
        # publication per bar), visibility is a prefix and bisect answers
        # it exactly; otherwise the linear filter below preserves the
        # reference semantics.
        self._bars_by_security: dict[
            str, tuple[tuple[BarRecord, ...], tuple[datetime, ...], bool]
        ] = {}
        for security_id, bars in by_security.items():
            available = tuple(bar.available_at for bar in bars)
            monotone = all(a <= b for a, b in pairwise(available))
            self._bars_by_security[security_id] = (tuple(bars), available, monotone)

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
        entry = self._bars_by_security.get(security_id)
        if entry is None:
            return ()
        bars, available, monotone = entry
        if monotone:
            return bars[: bisect_right(available, decision_at)]
        return tuple(bar for bar, at in zip(bars, available, strict=True) if at <= decision_at)

    def features_as_of(
        self,
        *,
        decision_at: datetime,
        universe_id: str,
        dataset_snapshot_id: str,
    ) -> tuple[PanelRow, ...]:
        """Price/volume feature snapshots for the point-in-time universe
        (V1: ret_1, dol_vol; V2 adds mom_5/mom_20/dol_vol_20 — M2 §3.C).

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
            features = _v2_features(
                bars, decision_at, self._snapshot.manifest.provider, self._ordinals
            )
            if features:
                rows.append(
                    PanelRow(
                        security_id=security_id,
                        decision_session=decision_session,
                        features=features,
                    )
                )
        return tuple(rows)


_MOMENTUM_HORIZONS = (5, 20)
_DOL_VOL_WINDOW = 20


def _contiguous_tail(
    bars: tuple[BarRecord, ...], count: int, ordinals: dict[date, int]
) -> tuple[BarRecord, ...] | None:
    """The last `count` bars iff they occupy `count` consecutive calendar
    sessions; otherwise None (a lapse inside the lookback means the
    feature is absent, never imputed)."""
    if len(bars) < count:
        return None
    tail = bars[-count:]
    positions = [ordinals[bar.session] for bar in tail]
    if any(later - earlier != 1 for earlier, later in pairwise(positions)):
        return None
    return tail


def _v2_features(
    bars: tuple[BarRecord, ...],
    decision_at: datetime,
    provider: str,
    ordinals: dict[date, int],
) -> tuple[FeatureEvent, ...]:
    """V1: ret_1 (last close vs previous visible close, simple return) and
    dol_vol (last visible close x volume). V2 (M2-proper §3.C) adds the
    momentum family and the smoothed liquidity control:

      mom_H  — log(c_b / c_{b-H}) over the last H+1 bars, which must
               occupy H+1 consecutive calendar sessions (mom_5/mom_20;
               ret_1 IS the aligned 1-session member of this family)
      dol_vol_20 — mean of close x volume over the last 20 bars, also
               contiguity-required

    Every feature derives ONLY from bars visible at decision_at and
    carries the provenance of its binding (latest) input bar."""
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

        aligned = _contiguous_tail(bars, 2, ordinals)
        if aligned is not None:

            def _mom_1(tail: tuple[BarRecord, ...] = aligned) -> FeatureEvent:
                return FeatureEvent(
                    security_id=tail[-1].security_id,
                    feature_name="mom_1",
                    value=float(tail[-1].close / tail[-2].close - 1),
                    observed_at=tail[-1].available_at,
                    available_at=tail[-1].available_at,
                    decision_at=decision_at,
                    source=provider,
                    source_record_id=tail[-1].source_record_id,
                    revision_id=REVISION_ZERO,
                )

            builders.append(_mom_1)

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

        for horizon in _MOMENTUM_HORIZONS:
            tail = _contiguous_tail(bars, horizon + 1, ordinals)
            if tail is None:
                continue

            def _momentum(
                tail: tuple[BarRecord, ...] = tail, horizon: int = horizon
            ) -> FeatureEvent:
                return FeatureEvent(
                    security_id=tail[-1].security_id,
                    feature_name=f"mom_{horizon}",
                    value=math.log(float(tail[-1].close / tail[0].close)),
                    observed_at=tail[-1].available_at,
                    available_at=tail[-1].available_at,
                    decision_at=decision_at,
                    source=provider,
                    source_record_id=tail[-1].source_record_id,
                    revision_id=REVISION_ZERO,
                )

            builders.append(_momentum)

        window = _contiguous_tail(bars, _DOL_VOL_WINDOW, ordinals)
        if window is not None:

            def _dol_vol_20(win: tuple[BarRecord, ...] = window) -> FeatureEvent:
                total = sum((bar.close * bar.volume for bar in win), Decimal(0))
                return FeatureEvent(
                    security_id=win[-1].security_id,
                    feature_name="dol_vol_20",
                    value=float(total / Decimal(_DOL_VOL_WINDOW)),
                    observed_at=win[-1].available_at,
                    available_at=win[-1].available_at,
                    decision_at=decision_at,
                    source=provider,
                    source_record_id=win[-1].source_record_id,
                    revision_id=REVISION_ZERO,
                )

            builders.append(_dol_vol_20)

    return tuple(builder() for builder in builders)
