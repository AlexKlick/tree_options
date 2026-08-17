"""Purged anchored-expanding walk-forward splitter (INV-05/06).

Fold k over calendar ordinals (H = label horizon, E = embargo, G = H + E):
    test  = [T_k, T_k + test_len)
    val   = [T_k - val_len, T_k)
    train = [0, T_k - val_len - G)          # anchor fixed at session 0
    T_{k+1} = T_k + roll
Required gap: ordinal(first eval) - ordinal(last train) > G — the label
window [s+1, s+H] of any retained train session cannot touch the eval block.

`Fold` never exposes the raw union of train and validation: the M2 refit set is the
pre-purged `final_fit_train_sessions`, so skipping the val→test purge is not
accidentally possible. `assign_rows` keys membership by session VALUE only,
which makes INV-05 (same-decision-session rows stay together) structural.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from tree_options.protocol.schema import ResearchProtocol
from tree_options.time.calendar import SessionCalendar


@dataclass(frozen=True)
class Fold:
    fold_id: int
    train_sessions: frozenset[date]
    validation_sessions: frozenset[date]
    test_sessions: frozenset[date]
    final_fit_train_sessions: frozenset[date]

    @property
    def all_eval_sessions(self) -> frozenset[date]:
        return self.validation_sessions | self.test_sessions


class WalkForwardSplitter:
    def __init__(
        self,
        calendar: SessionCalendar,
        *,
        label_horizon_sessions: int,
        embargo_sessions: int,
        val_sessions: int,
        test_sessions: int,
        roll_sessions: int,
        min_train_sessions: int,
    ) -> None:
        for name, v in dict(
            label_horizon_sessions=label_horizon_sessions,
            val_sessions=val_sessions,
            test_sessions=test_sessions,
            roll_sessions=roll_sessions,
            min_train_sessions=min_train_sessions,
        ).items():
            if v < 1:
                raise ValueError(f"{name} must be >= 1, got {v}")
        if embargo_sessions < 0:
            # The frozen protocol requires E >= 1; the engine admits E == 0 so
            # the purge invariant can be tested in isolation.
            raise ValueError(f"embargo_sessions must be >= 0, got {embargo_sessions}")
        self.calendar = calendar
        self.label_horizon_sessions = label_horizon_sessions
        self.embargo_sessions = embargo_sessions
        self.gap = label_horizon_sessions + embargo_sessions
        self.val_sessions = val_sessions
        self.test_sessions = test_sessions
        self.roll_sessions = roll_sessions
        self.min_train_sessions = min_train_sessions

    @classmethod
    def from_protocol(
        cls, calendar: SessionCalendar, protocol: ResearchProtocol
    ) -> WalkForwardSplitter:
        f = protocol.folds
        return cls(
            calendar,
            label_horizon_sessions=f.label_horizon_sessions,
            embargo_sessions=f.embargo_sessions,
            val_sessions=f.validation_window_sessions.default,
            test_sessions=f.test_window_sessions.default,
            roll_sessions=f.roll_forward_sessions,
            min_train_sessions=f.min_train_sessions,
        )

    def splits(self, panel_decision_sessions=None) -> list[Fold]:
        sessions = self.calendar.sessions()
        if panel_decision_sessions is not None:
            unknown = [d for d in panel_decision_sessions if not self.calendar.is_session(d)]
            if unknown:
                raise ValueError(f"panel sessions not in calendar: {unknown[:5]}")
        n = len(sessions)
        folds: list[Fold] = []
        fold_id = 0
        t0 = self.min_train_sessions + self.gap + self.val_sessions  # first test start
        while t0 + self.test_sessions <= n:
            test_block = sessions[t0 : t0 + self.test_sessions]
            val_block = sessions[t0 - self.val_sessions : t0]
            train_end = t0 - self.val_sessions - self.gap  # exclusive ordinal
            if train_end < self.min_train_sessions:
                t0 += self.roll_sessions
                continue
            train_block = sessions[:train_end]
            # Refit set: train plus the purged head of validation (drop the
            # final G sessions of val whose labels would touch the test block).
            val_ordinals = range(t0 - self.val_sessions, t0)
            keep_val = [
                sessions[i]
                for i in val_ordinals
                if (t0 - i) > self.gap  # ordinal(test[0]) - ordinal(v) > G
            ]
            folds.append(
                Fold(
                    fold_id=fold_id,
                    train_sessions=frozenset(train_block),
                    validation_sessions=frozenset(val_block),
                    test_sessions=frozenset(test_block),
                    final_fit_train_sessions=frozenset(train_block) | frozenset(keep_val),
                )
            )
            fold_id += 1
            t0 += self.roll_sessions
        return folds


def assign_rows(fold: Fold, decision_sessions: list[date]) -> dict[str, tuple[int, ...]]:
    """Index rows into fold roles BY SESSION VALUE ONLY (INV-05 structural)."""
    out: dict[str, list[int]] = {
        "train": [],
        "validation": [],
        "test": [],
        "final_fit_train": [],
    }
    for idx, d in enumerate(decision_sessions):
        if d in fold.train_sessions:
            out["train"].append(idx)
            out["final_fit_train"].append(idx)
        elif d in fold.validation_sessions:
            out["validation"].append(idx)
            if d in fold.final_fit_train_sessions:
                out["final_fit_train"].append(idx)
        elif d in fold.test_sessions:
            out["test"].append(idx)
    return {k: tuple(v) for k, v in out.items()}
