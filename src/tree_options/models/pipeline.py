"""Ridge pipeline with FittingGuard enforcement inside the pipeline
(M2-proper §3.D — discharges the INV-07 "full pipeline enforcement"
debt named in guards/fitting.py's docstring).

Contract: standardizer + ridge coefficients are fit ONCE, on the train
sessions the caller declares, and can then only be APPLIED. The guard
wiring lives here, not in caller convention:

  fit()   registers the single artifact "<name>/ridge:v1" with the
          sessions it saw (refits under the same name are refused);
  score() first asserts the fit set is disjoint from the declared
          target sessions (FIT_ON_EVAL otherwise), then records the
          application, then scores — and refuses any row whose session
          is not in the declared target set (the audit trail must be
          truthful).

Rows missing any feature are skipped at fit AND score — absent, never
imputed. Constant columns standardize to an exact zero column (std is
replaced by 1) and receive a zero ridge coefficient: they are inert,
deterministically. Drivers must pin single-threaded BLAS before the
first numpy import (models.determinism).
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date

import numpy as np

from tree_options.guards.fitting import FittingGuard

ARTIFACT_REVISION = "v1"


@dataclass(frozen=True)
class FitRow:
    """One training observation: features plus the outcome label."""

    session: date
    security_id: str
    features: dict[str, float]
    label: float


@dataclass(frozen=True)
class ObsRow:
    """One scoring observation: features only."""

    session: date
    security_id: str
    features: dict[str, float]


@dataclass(frozen=True)
class ScoreRow:
    session: date
    security_id: str
    score: float


class RidgePipeline:
    """Standardized ridge regression, fit-once/apply-only by construction."""

    def __init__(
        self,
        *,
        name: str,
        feature_names: tuple[str, ...],
        ridge_lambda: float = 1.0,
        guard: FittingGuard | None = None,
    ) -> None:
        if not feature_names:
            raise ValueError("feature_names must not be empty")
        if len(set(feature_names)) != len(feature_names):
            raise ValueError("feature_names must be unique")
        if not math.isfinite(ridge_lambda) or ridge_lambda < 0:
            raise ValueError("ridge_lambda must be finite and >= 0")
        self.guard = guard if guard is not None else FittingGuard()
        self.feature_names = tuple(feature_names)
        self.ridge_lambda = float(ridge_lambda)
        self.artifact = f"{name}/ridge:{ARTIFACT_REVISION}"
        self._mean: np.ndarray | None = None
        self._scale: np.ndarray | None = None
        self._coef: np.ndarray | None = None
        self._intercept = 0.0
        self._fit_row_count = 0

    @property
    def fitted(self) -> bool:
        return self._coef is not None

    def _usable(self, row: FitRow | ObsRow) -> bool:
        return all(name in row.features for name in self.feature_names)

    def _matrix(self, rows: Sequence[FitRow | ObsRow]) -> np.ndarray:
        return np.array(
            [[row.features[name] for name in self.feature_names] for row in rows],
            dtype=np.float64,
        )

    def fit(self, rows: Iterable[FitRow], *, fit_sessions: frozenset[date]) -> None:
        usable = [row for row in rows if self._usable(row)]
        if not usable:
            raise ValueError("no usable rows: every row is missing some feature")
        undeclared = {row.session for row in usable} - fit_sessions
        if undeclared:
            raise ValueError(
                "usable row sessions are not in the declared fit sessions: "
                f"{sorted(undeclared)[:3]}"
            )
        x = self._matrix(usable)
        y = np.array([row.label for row in usable], dtype=np.float64)
        if not np.isfinite(x).all() or not np.isfinite(y).all():
            raise ValueError("fit features and labels must be finite")
        mean = x.mean(axis=0)
        scale = x.std(axis=0)
        scale = np.where(scale == 0.0, 1.0, scale)
        z = (x - mean) / scale
        y_mean = float(y.mean())
        gram = z.T @ z + self.ridge_lambda * np.eye(len(self.feature_names))
        rhs = z.T @ (y - y_mean)
        coef = np.linalg.solve(gram, rhs)

        self.guard.fit_on(self.artifact, fit_sessions)
        self._mean, self._scale, self._coef = mean, scale, coef
        self._intercept = y_mean
        self._fit_row_count = len(usable)

    def score(
        self, rows: Iterable[ObsRow], *, target_sessions: frozenset[date]
    ) -> tuple[ScoreRow, ...]:
        if self._coef is None:
            # surface the guard's own refusal for consistency
            self.guard.fit_sessions(self.artifact)
            raise RuntimeError("unreachable")  # pragma: no cover
        usable: list[ObsRow] = []
        for row in rows:
            if row.session not in target_sessions:
                raise ValueError(
                    f"row session {row.session} is not in the declared target sessions"
                )
            if self._usable(row):
                usable.append(row)
        self.guard.assert_fit_excludes(self.artifact, target_sessions)
        self.guard.apply_to(self.artifact, target_sessions)
        if not usable:
            return ()
        x = self._matrix(usable)
        if not np.isfinite(x).all():
            raise ValueError("score features must be finite")
        assert self._mean is not None
        assert self._scale is not None
        z = (x - self._mean) / self._scale
        values = z @ self._coef + self._intercept
        return tuple(
            ScoreRow(session=row.session, security_id=row.security_id, score=float(value))
            for row, value in zip(usable, values, strict=True)
        )

    def artifact_bytes(self) -> bytes:
        """Canonical, stamped-artifact-ready serialization of the fit."""
        if self._coef is None or self._mean is None or self._scale is None:
            raise ValueError("pipeline is not fitted")
        payload = {
            "artifact": self.artifact,
            "revision": ARTIFACT_REVISION,
            "feature_names": list(self.feature_names),
            "ridge_lambda": self.ridge_lambda,
            "mean": [float(v) for v in self._mean],
            "scale": [float(v) for v in self._scale],
            "coefficients": [float(v) for v in self._coef],
            "intercept": self._intercept,
            "fit_row_count": self._fit_row_count,
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
