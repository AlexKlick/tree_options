"""Fitting guard (INV-07): transforms fit on training sessions only.

INV-07: imputation, scaling, feature selection, calibration, and
hyperparameter search are fit on TRAINING data and applied (never re-fit) to
validation/test rows. In M0 there is no model pipeline yet, so this guard is
the EXECUTABLE CORE of that invariant: every fitted artifact records the
sessions it was fit on, and any use of that artifact refuses sessions
outside its fit set. The full pipeline enforcement (transform chains,
selection loops) lands with M2 — the evidence doc claims exactly this much
and no more.
"""

from __future__ import annotations

from datetime import date


class FittingLeakError(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class FittingGuard:
    def __init__(self) -> None:
        self._fit_sessions: dict[str, frozenset[date]] = {}

    def fit_on(self, name: str, sessions: frozenset[date]) -> None:
        """Register a fitted artifact together with the sessions it saw."""
        if not sessions:
            raise FittingLeakError("EMPTY_FIT_SET", f"{name} fit on no sessions")
        if name in self._fit_sessions:
            raise FittingLeakError("REFIT", f"{name} already fitted; refits are new names")
        self._fit_sessions[name] = sessions

    def fit_sessions(self, name: str) -> frozenset[date]:
        try:
            return self._fit_sessions[name]
        except KeyError:
            raise FittingLeakError("UNFITTED_ARTIFACT", f"{name} was never fitted") from None

    def apply_to(self, name: str, target_sessions: frozenset[date]) -> None:
        """Refuse to APPLY an artifact to sessions it was not fit on.

        Applying a scaler/imputer fitted on train to eval rows is the ONE
        sanctioned direction; an artifact applied to sessions outside its fit
        set (e.g. a selector 'fit' on validation) is a leak by construction.
        """
        fit = self.fit_sessions(name)
        leak = target_sessions - fit
        if leak:
            raise FittingLeakError(
                "FIT_SET_VIOLATION",
                f"{name} fit on {len(fit)} sessions; applied to {len(leak)} "
                f"sessions outside the fit set (e.g. {sorted(leak)[:3]})",
            )

    def assert_fit_excludes(self, name: str, forbidden: frozenset[date]) -> None:
        """Assert the fit set contains none of the forbidden (eval) sessions."""
        fit = self.fit_sessions(name)
        overlap = fit & forbidden
        if overlap:
            raise FittingLeakError(
                "FIT_ON_EVAL",
                f"{name} was fit on forbidden sessions {sorted(overlap)[:3]}",
            )
