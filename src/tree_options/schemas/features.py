"""Feature events, labels, panel rows (§6.2).

Features are availability-gated (INV-01/02/03/04). Labels are targets and are
NEVER availability-gated — their information legitimately postdates the
decision; purge (INV-06) protects them instead. An implementer who "fixes"
failing label availability would be introducing a bug.
"""

from __future__ import annotations

import math
from datetime import date

from pydantic import model_validator

from tree_options.schemas.common import IdStr, StrictModel, UTCDatetime


class FeatureEvent(StrictModel):
    security_id: IdStr
    feature_name: IdStr
    value: float
    observed_at: UTCDatetime
    available_at: UTCDatetime
    decision_at: UTCDatetime
    source: IdStr
    source_record_id: IdStr
    revision_id: IdStr

    @model_validator(mode="after")
    def _checks(self) -> FeatureEvent:
        if not math.isfinite(self.value):
            raise ValueError("feature value must be finite; missing data is an absent event")
        if self.observed_at > self.available_at:
            raise ValueError("observed_at must be <= available_at")
        return self


class LabelEvent(StrictModel):
    security_id: IdStr
    decision_session: date
    horizon_sessions: int
    label_window: tuple[date, date]
    value: float
    observed_at: UTCDatetime
    source: IdStr
    source_record_id: IdStr

    @model_validator(mode="after")
    def _checks(self) -> LabelEvent:
        if self.horizon_sessions < 1:
            raise ValueError("horizon_sessions must be >= 1")
        if self.label_window[0] > self.label_window[1]:
            raise ValueError("label_window must be ordered")
        return self


class PanelRow(StrictModel):
    security_id: IdStr
    decision_session: date
    features: tuple[FeatureEvent, ...]
    label: LabelEvent | None = None

    @model_validator(mode="after")
    def _checks(self) -> PanelRow:
        for f in self.features:
            if f.security_id != self.security_id:
                raise ValueError("feature security_id mismatch in panel row")
        if self.label is not None and self.label.security_id != self.security_id:
            raise ValueError("label security_id mismatch in panel row")
        if self.label is not None and self.label.decision_session != self.decision_session:
            raise ValueError("label decision_session mismatch in panel row")
        return self
