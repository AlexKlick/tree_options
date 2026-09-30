import numpy as np

from tree_options.strategy_lab.clustering import (
    fit_kmeans,
    rsi_seed_centroids,
    semantic_cluster_by_feature,
)


def test_deterministic_clusters_and_semantic_high_rsi_selection():
    x = np.array([[28.0, 0.0], [31.0, 0.1], [44.0, 0.0], [46.0, 0.2], [55.0, 0.0], [57.0, 0.1], [69.0, 0.0], [72.0, 0.1]])
    seeds = rsi_seed_centroids(feature_count=2, rsi_feature_index=0)
    first = fit_kmeans(x, initial_centroids=seeds)
    second = fit_kmeans(x, initial_centroids=seeds)
    assert first == second
    target = semantic_cluster_by_feature(first, feature_index=0, highest=True)
    members = [x[i, 0] for i, label in enumerate(first.labels) if label == target]
    assert min(members) >= 69


def test_label_permutation_and_ties_preserve_semantic_target():
    from tree_options.strategy_lab.clustering import KMeansResult
    first = KMeansResult((0, 1), ((70.0, 1.0), (70.0, 2.0)), 1)
    swapped = KMeansResult((1, 0), ((70.0, 2.0), (70.0, 1.0)), 1)
    a = semantic_cluster_by_feature(first, feature_index=0)
    b = semantic_cluster_by_feature(swapped, feature_index=0)
    assert first.centroids[a] == swapped.centroids[b] == (70.0, 2.0)
