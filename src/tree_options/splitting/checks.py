"""Fold invariant checks (INV-05/06): used by tests AND production consumers."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from datetime import date

from tree_options.splitting.splitter import Fold
from tree_options.time.calendar import SessionCalendar


class FoldInvariantViolation(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def check_folds(
    folds: Sequence[Fold],
    *,
    calendar: SessionCalendar,
    label_horizon_sessions: int,
    embargo_sessions: int,
) -> None:
    """Raise FoldInvariantViolation(code) if any fold invariant is broken.

    Codes: SETS_DISJOINT, PURGE_OVERLAP, EMBARGO_GAP, FINAL_FIT_PURGE,
    ANCHOR_MONOTONE, COVERAGE.
    """
    gap = label_horizon_sessions + embargo_sessions
    sessions = calendar.sessions()
    base = sessions[0]

    for fold in folds:
        if (
            fold.train_sessions & fold.validation_sessions
            or fold.train_sessions & fold.test_sessions
            or fold.validation_sessions & fold.test_sessions
        ):
            raise FoldInvariantViolation("SETS_DISJOINT", f"fold {fold.fold_id} role sets overlap")
        # Purge: for every train session s and eval session t, the label
        # window [s+1, s+H] must not touch t: ordinal(t) - ordinal(s) > H.
        for s in fold.train_sessions:
            s_ord = calendar.ordinal(s)
            for t in fold.all_eval_sessions:
                if calendar.ordinal(t) - s_ord <= label_horizon_sessions:
                    raise FoldInvariantViolation(
                        "PURGE_OVERLAP",
                        f"fold {fold.fold_id}: train {s} label window touches eval {t}",
                    )
        # Embargo: min gap > H + E.
        last_train = max(
            (calendar.ordinal(s) for s in fold.train_sessions),
            default=None,
        )
        if last_train is not None:
            first_eval = min(calendar.ordinal(t) for t in fold.all_eval_sessions)
            if first_eval - last_train <= gap:
                raise FoldInvariantViolation(
                    "EMBARGO_GAP",
                    f"fold {fold.fold_id}: gap {first_eval - last_train} <= {gap}",
                )
        # Refit set purge vs test.
        for s in fold.final_fit_train_sessions - fold.train_sessions:
            for t in fold.test_sessions:
                if (
                    calendar.ordinal(t) - calendar.ordinal(s)
                    <= label_horizon_sessions + embargo_sessions
                ):
                    raise FoldInvariantViolation(
                        "FINAL_FIT_PURGE",
                        f"fold {fold.fold_id}: retained val {s} within gap of test {t}",
                    )
        # Anchor: train always starts at calendar session 0.
        if fold.train_sessions and min(fold.train_sessions) != base:
            raise FoldInvariantViolation(
                "ANCHOR_MONOTONE", f"fold {fold.fold_id} train does not start at anchor"
            )

    # Fold ids are unique; test blocks are disjoint regardless of ids.
    ids = [fold.fold_id for fold in folds]
    if len(set(ids)) != len(ids):
        raise FoldInvariantViolation(
            "DUPLICATE_FOLD_ID", f"fold ids repeat: {sorted(ids)[:6]}"
        )
    seen: dict[date, int] = {}
    for fold in folds:
        for t in fold.test_sessions:
            if t in seen:
                raise FoldInvariantViolation("COVERAGE", f"test session {t} in two folds")
            seen[t] = fold.fold_id


def check_same_session_grouping(
    assignment: dict[str, tuple[int, ...]], decision_sessions: Sequence[date]
) -> None:
    """INV-05: every row of a session lands in exactly one role per call."""
    where: defaultdict[date, set[str]] = defaultdict(set)
    for role, idxs in assignment.items():
        if role == "final_fit_train":
            continue  # derived role, not a partition member
        for i in idxs:
            where[decision_sessions[i]].add(role)
    for d, roles in where.items():
        if len(roles) > 1:
            raise FoldInvariantViolation(
                "SESSION_GROUPING", f"session {d} split across roles {sorted(roles)}"
            )
