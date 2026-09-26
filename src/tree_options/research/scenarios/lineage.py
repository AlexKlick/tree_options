"""Scenario lineage: honest parent → child binding.

A child scenario records the parent's *effective* identity at
attach-time:

    parent_run_id         (the run the scenario was forked from)
    parent_engine_sha256  (the engine bytes that ran the parent)
    parent_input_sha256   (the resolved input snapshot of the parent)
    parent_calendar_sha   (the calendar the parent pinned)
    parent_spec_hash      (the parent's ComparisonSpec spec_hash)

At compute time the child refuses if those four identity values no
longer match the parent's currently-stored result record — the
parent's source or inputs changed, so a rerun would not be the same
study. This is the "lineage != whatever the parent looks like today"
property.

The lineage is stored inside the runstate store as a pair of record
kinds:

    ``parent``  key=parent_run_id, value=ParentRef
                (idempotent: same parent written under one key)
    ``child``   key=child_run_id,  value=ChildRef (parent_run_id -> child)

No new persistence layer; the same put/replace/get surface from
RL1-04 (a content-addressed store where the audit head follows every
write) keeps the lineage honest. The keys are SCOPED INSIDE the run
records' namespace — children look up by ``child.run.parent_run_id``
stored in the result's wire envelope.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from tree_options.research.scenarios.refusal_codes import (
    SCENARIO_PARENT_CHANGED,
    SCENARIO_PARENT_MISSING,
    ScenarioRefusal,
)

#: Store kind for ParentRef records (sibling of "spec", "result").
#: The allowlist lives in ``tree_options.research.runstate.store._KINDS``.
PARENT_KIND: str = "scenario_parent"


@dataclass(frozen=True)
class ParentRef:
    """The identity of a ComparisonResult at the moment a scenario was
    forked from it. The four shas are the *effective* identity: what
    produced the parent as a function of its inputs, not whatever the
    parent happens to look like today."""
    parent_run_id: str
    parent_spec_hash: str
    parent_engine_sha256: str
    parent_input_snapshot_sha256: str
    parent_calendar_sha256: str

    def to_dict(self) -> dict[str, str]:
        return {
            "parent_run_id": self.parent_run_id,
            "parent_spec_hash": self.parent_spec_hash,
            "parent_engine_sha256": self.parent_engine_sha256,
            "parent_input_snapshot_sha256": self.parent_input_snapshot_sha256,
            "parent_calendar_sha256": self.parent_calendar_sha256,
        }


@dataclass(frozen=True)
class ChildRef:
    """A child's pointer back to its parent. Stored as a wire-visible
    field on the child's stored ``result`` so any reader can resolve
    the lineage without traversing the store."""
    child_run_id: str
    parent_run_id: str
    scenario_kind: str
    scenario_diff_sha256: str

    def to_dict(self) -> dict[str, str]:
        return {
            "child_run_id": self.child_run_id,
            "parent_run_id": self.parent_run_id,
            "scenario_kind": self.scenario_kind,
            "scenario_diff_sha256": self.scenario_diff_sha256,
        }


# -- operations -------------------------------------------------------------


def attach_child(store: Any, child: ChildRef,
                 *, at: Any) -> str:
    """Persist the parent → child pointer. Idempotent: re-attaching with
    the same content returns the existing sha; conflicting content
    raises ``RunstateStoreError`` (the lineage is immutable)."""
    from tree_options.research.runstate.store import RunstateStoreError

    existing = store.get("child", child.child_run_id)
    if existing is not None:
        if existing == child.to_dict():
            sha = store.get_kind_payload_sha("child", child.child_run_id)
            if sha is None:
                raise RunstateStoreError(
                    "child pointer present but sha unavailable")
            return sha
        raise RunstateStoreError(
            "child_run_id already attached to a different parent"
        )
    from datetime import datetime as _dt
    return store.put("child", child.to_dict(),
                     key=child.child_run_id,
                     at=at or _dt.now())


def list_children(store: Any, parent_run_id: str) -> tuple[str, ...]:
    """Walk all child pointers and return the child_run_ids attached to
    this parent, ordered by the store's ``created_at`` (insertion
    order)."""
    out: list[str] = []
    for payload, _at in store.all_at("child"):
        if (isinstance(payload, dict)
                and payload.get("parent_run_id") == parent_run_id):
            cid = payload.get("child_run_id")
            if isinstance(cid, str):
                out.append(cid)
    return tuple(out)


def load_parent_ref(store: Any, parent_run_id: str) -> ParentRef | None:
    payload = store.get(PARENT_KIND, parent_run_id)
    if payload is None:
        return None
    return ParentRef(
        parent_run_id=payload["parent_run_id"],
        parent_spec_hash=payload["parent_spec_hash"],
        parent_engine_sha256=payload["parent_engine_sha256"],
        parent_input_snapshot_sha256=payload["parent_input_snapshot_sha256"],
        parent_calendar_sha256=payload["parent_calendar_sha256"],
    )


def store_parent_ref(store: Any, ref: ParentRef,
                     *, at: Any) -> str:
    """Persist a parent's effective identity. Idempotent on identical
    content; conflicting content raises (the parent identity is
    immutable once attached for the first time, mirroring the run
    records' audit chain)."""
    from datetime import datetime as _dt
    return store.put(PARENT_KIND, ref.to_dict(),
                     key=ref.parent_run_id,
                     at=at or _dt.now())


def parent_changed(current_result: dict[str, Any],
                   ref: ParentRef) -> ScenarioRefusal | None:
    """Compare a parent's *currently-stored* identity (the sha fields
    bound into its result envelope) against the ``ParentRef`` recorded
    at attach-time. A mismatch is a refused scenario: the parent's
    source or inputs shifted, the child cannot honestly inherit."""
    checks = (
        ("engine_sha256", ref.parent_engine_sha256,
         current_result.get("engine_sha256")),
        ("input_snapshot_sha256", ref.parent_input_snapshot_sha256,
         current_result.get("input_snapshot_sha256")),
        ("calendar_sha256", ref.parent_calendar_sha256,
         current_result.get("calendar_sha256")),
    )
    for name, expected, actual in checks:
        if not isinstance(actual, str) or actual != expected:
            return ScenarioRefusal(
                code=SCENARIO_PARENT_CHANGED,
                message=(f"parent {name} changed: expected "
                         f"{expected!r}, parent currently reports "
                         f"{actual!r}; refuse to publish a scenario "
                         "inheriting from a re-run parent"),
            )
    return None


def parent_missing(parent_result: dict[str, Any] | None) -> ScenarioRefusal | None:
    """A child that lost its parent (the parent's run record was
    deleted under owner action, or never landed under RL1-03 custody)
    refuses to publish — never silently inherits an unanchored
    lineage."""
    if parent_result is None:
        return ScenarioRefusal(
            code=SCENARIO_PARENT_MISSING,
            message="parent run has no stored result record; refusing "
                    "to publish an unanchored scenario fork",
        )
    status = parent_result.get("status")
    if status != "completed":
        return ScenarioRefusal(
            code=SCENARIO_PARENT_MISSING,
            message=(f"parent run status is {status!r}; only completed "
                     "parent results can be forked"),
        )
    return None


__all__ = [
    "PARENT_KIND",
    "ChildRef",
    "ParentRef",
    "attach_child",
    "list_children",
    "load_parent_ref",
    "parent_changed",
    "parent_missing",
    "store_parent_ref",
]
