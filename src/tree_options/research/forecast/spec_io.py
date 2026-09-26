"""ForecastSpec wire I/O (RL-3).

Sits beside ``tree_options.research.spec_io`` and
``tree_options.research.scenarios.spec_io``: all three parsers are
siblings speaking the wire dialect the HTTP view and the bounded worker
share VERBATIM (RL1-03: the stored canonical spec and the POSTed body
must parse identically).

This module is intentionally FREE of imports from
``tree_options.research.forecast.engine`` / ``harness`` / ``sources`` /
``metrics`` so either side can be loaded before the other — the import
graph stays acyclic (the scenarios-package lesson).
"""
from __future__ import annotations

from datetime import date
from typing import Any, Final

from tree_options.research.forecast.contracts import (
    ForecastSourceId,
    ForecastSpec,
)

#: The exact wire surface of a ForecastSpec. Anything else in the body is
#: rejected pre-write: the spec is the hashed request, so an unknown field
#: is a different request that must not silently alias an existing run.
FORECAST_SPEC_FIELDS: Final[tuple[str, ...]] = (
    "source",
    "horizon",
    "evaluation_start",
    "evaluation_end",
    "proposed_by",
    "notes",
)


def forecast_from_dict(payload: dict[str, Any]) -> ForecastSpec:
    """Parse a wire-format forecast evaluation request.

    Body shape::

        {
            "source": "synthetic-forecast-v1" | "index:VIX",
            "horizon": 5,                       # positive int, sessions
            "evaluation_start": "2018-02-01",   # ISO date
            "evaluation_end": null,             # ISO date or null (open)
            "proposed_by": "operator",
            "notes": ""
        }

    Raises ``ValueError`` with a field-specific message on anything else
    (unknown field, unknown source, non-positive or non-int horizon,
    unparsable dates, inverted window). ``bool`` is rejected for horizon
    even though ``isinstance(True, int)`` — a wire bool is never a count.
    """
    if not isinstance(payload, dict):
        raise ValueError("forecast body must be a JSON object")
    unknown = set(payload) - set(FORECAST_SPEC_FIELDS)
    if unknown:
        raise ValueError(
            f"forecast body contains fields outside the spec surface "
            f"{list(FORECAST_SPEC_FIELDS)}: {sorted(unknown)}"
        )
    for required in ("source", "horizon", "evaluation_start"):
        if required not in payload:
            raise ValueError(f"forecast body is missing '{required}'")

    raw_source = payload["source"]
    if not isinstance(raw_source, str):
        raise ValueError(f"'source' must be a string, got {raw_source!r}")
    try:
        source = ForecastSourceId(raw_source)
    except ValueError as exc:
        raise ValueError(
            f"unknown forecast source {raw_source!r}; known sources: "
            f"{[s.value for s in ForecastSourceId]}"
        ) from exc

    raw_horizon = payload["horizon"]
    if isinstance(raw_horizon, bool) or not isinstance(raw_horizon, int):
        raise ValueError(
            f"'horizon' must be an integer session count, got {raw_horizon!r}")
    if raw_horizon <= 0:
        raise ValueError(
            f"'horizon' must be positive, got {raw_horizon}")

    start = _parse_iso(payload["evaluation_start"], "evaluation_start")
    end: date | None = None
    if payload.get("evaluation_end") is not None:
        end = _parse_iso(payload["evaluation_end"], "evaluation_end")
    if end is not None and end < start:
        raise ValueError(
            f"'evaluation_end' ({end.isoformat()}) precedes "
            f"'evaluation_start' ({start.isoformat()})")

    proposed_by = payload.get("proposed_by", "operator")
    if not isinstance(proposed_by, str):
        raise ValueError(
            f"'proposed_by' must be a string, got {proposed_by!r}")
    notes = payload.get("notes", "")
    if not isinstance(notes, str):
        raise ValueError(f"'notes' must be a string, got {notes!r}")

    return ForecastSpec(
        source=source,
        horizon=raw_horizon,
        evaluation_start=start,
        evaluation_end=end,
        proposed_by=proposed_by,
        notes=notes,
    )


def _parse_iso(value: Any, field_name: str) -> date:
    if not isinstance(value, str):
        raise ValueError(
            f"'{field_name}' must be an ISO date string, got {value!r}")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(
            f"'{field_name}' is not an ISO date: {value!r}") from exc


__all__ = ["FORECAST_SPEC_FIELDS", "forecast_from_dict"]
