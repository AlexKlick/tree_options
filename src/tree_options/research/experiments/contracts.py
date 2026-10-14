"""Product read models. Unknown counts and money remain null, never zero-filled."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Count = Annotated[int, Field(ge=0, strict=True)]
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Evidence = Literal["historical_simulation", "forward_shadow", "broker_paper", "live", "unavailable"]


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, serialize_by_alias=True)


class Metric(Record):
    label: str
    value: Count | str | None = None
    unit: str
    reason: str | None = None

    @model_validator(mode="after")
    def unknown_reason(self) -> Metric:
        if self.value is None and not self.reason:
            raise ValueError("unknown metric requires a reason")
        return self


class Artifact(Record):
    label: str
    sha256: Digest


class Experiment(Record):
    id: Annotated[str, Field(pattern=r"^[a-zA-Z0-9_.:-]{1,200}$")]
    source: str
    title: str
    hypothesis: str
    evidence_class: Evidence
    status: str
    source_time: str | None = None
    freshness: Literal["archived", "observed", "stale", "unavailable"]
    freshness_reason: str
    metrics: list[Metric] = Field(default_factory=list)
    limitations: list[str]
    blockers: list[str] = Field(default_factory=list)
    next_decision: str
    artifacts: list[Artifact] = Field(default_factory=list)
    supersedes: str | None = None
    superseded_by: str | None = None
    advanced_link: Literal["#/research", "#/quant", "#/workspace", "desk/evidence"]
    execution_authorized: Literal[False] = False
    strategy_qualified: Literal[False] = False
    account_return: None = None


class Source(Record):
    id: str
    status: Literal["available", "unavailable"]
    reason: str | None = None
    count: Count | None = None


class PilotProgram(Record):
    id: str
    title: str
    hypothesis: str
    state: Literal["existing_observation", "blocked_draft"]
    blockers: list[str]
    next_decision: str
    # These caps authorize no NEW jobs; existing QSL has its original owner.
    new_requests: Literal[0] = 0
    new_compute_seconds: Literal[0] = 0
    new_storage_bytes: Literal[0] = 0


class Index(Record):
    schema_version: Literal["trex-experiment-index/1"] = Field(
        default="trex-experiment-index/1", alias="schema"
    )
    published_at: str
    source_release: str
    sources: list[Source]
    experiments: list[Experiment]
    pilot: list[PilotProgram]
    execution_authorized: Literal[False] = False
    live_money: Literal[False] = False

    @field_validator("published_at")
    @classmethod
    def aware_time(cls, value: str) -> str:
        if datetime.fromisoformat(value).tzinfo is None:
            raise ValueError("publication requires an aware timestamp")
        return value

    @model_validator(mode="after")
    def unique_ids(self) -> Index:
        ids = [r.id for r in self.experiments]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate experiment identity")
        for row in self.experiments:
            if any(ref and ref not in ids for ref in (row.supersedes, row.superseded_by)):
                raise ValueError("missing supersession identity")
        return self


def encoded(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value: object) -> str:
    return hashlib.sha256(encoded(value)).hexdigest()
