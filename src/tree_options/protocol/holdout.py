"""The owner-ratified final holdout window (0.2.1 ratification, 2026-08-26).

Window A: EXACTLY the 13 enumerated session dates below, scoped to the
lane-2 evaluation folds (``massive-derived-free/1``), bound to the exit-5
coverage census ``43b0b040ea3c…``. The owner ratified this enumeration as
one of the four fixed decisions of the 0.2.1 package.

This module is the SINGLE SOURCE of the enumeration. The amendment
builder's schema-addition proposal renders it verbatim
(``_render_schema_addition_proposal``), and the later owner-ratified PR
that lands ``final_holdout_window`` in the protocol schema (protocol
models are ``extra="forbid"``; the schema change is deliberately NOT this
package) must transcribe it EXACTLY — tests pin the rendered proposal to
this tuple so proposal and landing cannot drift.

The record is bound to ONE census: a build against any other census gets
the AWAITING_OWNER_DECLARATION placeholder, never a misbound window.
"""

from __future__ import annotations

from typing import Any

RATIFIED_HOLDOUT_CENSUS_SHA256 = "43b0b040ea3c7936fc08e6b1028ce446e46c99f44ca1d87da9fec02099e12e14"
FINAL_HOLDOUT_WINDOW_ID = "final-holdout-window-a"
# (window-A extension, owner direction 2026-09-04) the SECOND ratified
# window: the five sealed dates window A never evaluated (not
# label-complete on the window-A world — 2026-07-17..2026-08-14). A NEW
# packet under a NEW authority once the world grows; the spent window-A
# packet stays sealed forever. Same enumeration, same census binding —
# only the window identity (and its derived driver surfaces) differ.
FINAL_HOLDOUT_EXT_WINDOW_ID = "final-holdout-window-a-ext-1"
RATIFIED_HOLDOUT_WINDOW_IDS = (FINAL_HOLDOUT_WINDOW_ID, FINAL_HOLDOUT_EXT_WINDOW_ID)
FINAL_HOLDOUT_SCOPE = "lane-2 evaluation folds (massive-derived-free/1)"
FINAL_HOLDOUT_OWNER_DECISION = "m4-protocol-021-ratification-2026-08-26"
FINAL_HOLDOUT_DECIDED = "2026-08-26"
FINAL_HOLDOUT_DATES: tuple[str, ...] = (
    "2026-05-08",
    "2026-05-15",
    "2026-05-22",
    "2026-05-29",
    "2026-06-05",
    "2026-06-12",
    "2026-06-26",
    "2026-07-10",
    "2026-07-17",
    "2026-07-24",
    "2026-07-31",
    "2026-08-07",
    "2026-08-14",
)
FINAL_HOLDOUT_PLACEHOLDER = (
    "AWAITING_OWNER_DECLARATION: no ratified holdout window is bound to "
    "this census (the ratified window-a enumeration is bound to census "
    f"{RATIFIED_HOLDOUT_CENSUS_SHA256[:12]}… only)"
)


def final_holdout_window_record(census_content_sha256: str) -> dict[str, Any] | None:
    """The ratified window-a record when the build's census IS the ratified
    census; ``None`` for any other census (the caller renders the
    placeholder). The returned record is freshly built from the constants —
    never a shared mutable object."""
    if census_content_sha256 != RATIFIED_HOLDOUT_CENSUS_SHA256:
        return None
    return {
        "window_id": FINAL_HOLDOUT_WINDOW_ID,
        "dates": list(FINAL_HOLDOUT_DATES),
        "scope": FINAL_HOLDOUT_SCOPE,
        "census_content_sha256": RATIFIED_HOLDOUT_CENSUS_SHA256,
        "owner_decision": FINAL_HOLDOUT_OWNER_DECISION,
        "decided": FINAL_HOLDOUT_DECIDED,
        "landing_contract": (
            "the owner-ratified schema-addition PR must transcribe this "
            "enumeration EXACTLY (dates, scope, census binding); the "
            "proposal and the landing are pinned to this module's constants"
        ),
    }


__all__ = [
    "FINAL_HOLDOUT_DATES",
    "FINAL_HOLDOUT_DECIDED",
    "FINAL_HOLDOUT_EXT_WINDOW_ID",
    "FINAL_HOLDOUT_OWNER_DECISION",
    "FINAL_HOLDOUT_PLACEHOLDER",
    "FINAL_HOLDOUT_SCOPE",
    "FINAL_HOLDOUT_WINDOW_ID",
    "RATIFIED_HOLDOUT_CENSUS_SHA256",
    "RATIFIED_HOLDOUT_WINDOW_IDS",
    "final_holdout_window_record",
]
