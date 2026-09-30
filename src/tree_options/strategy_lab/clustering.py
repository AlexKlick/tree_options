"""Deterministic K-means implementation for registered research experiments."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np


class ClusteringError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class KMeansResult:
    labels: tuple[int, ...]
    centroids: tuple[tuple[float, ...], ...]
    iterations: int


def rsi_seed_centroids(
    *, feature_count: int, rsi_feature_index: int, targets: Sequence[float] = (30, 45, 55, 70)
) -> np.ndarray:
    if feature_count < 1 or not 0 <= rsi_feature_index < feature_count:
        raise ClusteringError("invalid feature count / RSI feature index")
    centers = np.zeros((len(tuple(targets)), feature_count), dtype=float)
    centers[:, rsi_feature_index] = np.asarray(tuple(targets), dtype=float)
    return centers


def fit_kmeans(
    values: Sequence[Sequence[float]],
    *,
    initial_centroids: Sequence[Sequence[float]],
    max_iter: int = 100,
    tolerance: float = 1e-10,
) -> KMeansResult:
    x = np.asarray(values, dtype=float)
    centers = np.asarray(initial_centroids, dtype=float).copy()
    if x.ndim != 2 or centers.ndim != 2 or x.shape[1] != centers.shape[1]:
        raise ClusteringError("values and centroids must be 2-D with the same feature count")
    if x.shape[0] < centers.shape[0] or not np.all(np.isfinite(x)) or not np.all(np.isfinite(centers)):
        raise ClusteringError("finite data with at least one row per cluster is required")
    if max_iter < 1 or tolerance < 0:
        raise ClusteringError("invalid convergence controls")

    labels = np.full(x.shape[0], -1, dtype=int)
    for iteration in range(1, max_iter + 1):
        distances = ((x[:, None, :] - centers[None, :, :]) ** 2).sum(axis=2)
        new_labels = distances.argmin(axis=1)
        new_centers = centers.copy()
        for cluster in range(centers.shape[0]):
            members = x[new_labels == cluster]
            if members.size == 0:
                raise ClusteringError(f"cluster {cluster} became empty")
            new_centers[cluster] = members.mean(axis=0)
        shift = float(np.max(np.abs(new_centers - centers)))
        stable_labels = np.array_equal(labels, new_labels)
        labels, centers = new_labels, new_centers
        if stable_labels or shift <= tolerance:
            return KMeansResult(
                labels=tuple(int(item) for item in labels),
                centroids=tuple(tuple(float(v) for v in row) for row in centers),
                iterations=iteration,
            )
    return KMeansResult(
        labels=tuple(int(item) for item in labels),
        centroids=tuple(tuple(float(v) for v in row) for row in centers),
        iterations=max_iter,
    )


def semantic_cluster_by_feature(
    result: KMeansResult, *, feature_index: int, highest: bool = True
) -> int:
    """Select a cluster by centroid meaning, not by an arbitrary label number."""
    if not result.centroids or not 0 <= feature_index < len(result.centroids[0]):
        raise ClusteringError("feature index outside centroid")
    # Equal feature values are ordered by the full centroid meaning, not
    # by a provider's arbitrary label number. Identical centroids refuse.
    chosen = (max if highest else min)(result.centroids, key=lambda c: (c[feature_index], c))
    matches = [i for i, centroid in enumerate(result.centroids) if centroid == chosen]
    if len(matches) != 1:
        raise ClusteringError("semantic target centroid is not unique")
    return matches[0]
