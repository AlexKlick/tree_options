"""Trial record (§6.5, INV-13): registered before outcomes are viewed."""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from tree_options.schemas.common import IdStr, StrictModel, UTCDatetime

TrialStatus = Literal["registered", "running", "completed", "failed"]


class TrialRecord(StrictModel):
    trial_id: IdStr
    parent_trial_id: IdStr | None = None
    created_at: UTCDatetime
    hypothesis: str = Field(min_length=8)
    git_sha: IdStr
    config_hash: IdStr
    dataset_manifest_hash: IdStr
    train_window: tuple[date, date] | None = None
    validation_window: tuple[date, date] | None = None
    test_window: tuple[date, date] | None = None
    hyperparameters: dict[str, Any]
    scope_key: IdStr  # protocol_version:outer_fold_id — the 32-cap counting key
    status: TrialStatus = "registered"
    metrics_uri: str | None = None
    failure_reason: str | None = None

    @field_validator("hyperparameters")
    @classmethod
    def _jsonable(cls, v: dict[str, Any]) -> dict[str, Any]:
        import json

        json.dumps(v, sort_keys=True, default=str)
        return v

    @model_validator(mode="after")
    def _checks(self) -> TrialRecord:
        for name in ("train_window", "validation_window", "test_window"):
            w = getattr(self, name)
            if w is not None and w[0] > w[1]:
                raise ValueError(f"{name} must be ordered")
        if self.status != "registered" and self.metrics_uri is None:
            raise ValueError("non-registered trial requires metrics_uri")
        return self
