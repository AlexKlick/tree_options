"""Run-scoped sealed workspaces (the 2026-08-31 successor-enablement lane).

The crashed sealed event occupies the LEGACY fixed production paths
(``artifacts/g4-sealed{.db,/,-scratch/}``), and ``run_g4_sealed_event``
refuses to reuse any of them — correctly. The successor event therefore
derives its OUTPUT locations from its own sealed run id:
``artifacts/g4-sealed-runs/<sealed_run_id>/``. The DECLARED per-checkout
inputs (era census, mutation report, the optional spot-proxy sidecar) are
shared and never move; the evidence root is a shared OUTPUT destination by
design (the docs triple at fixed filenames is superseded by a later event —
the durable per-run record is the run workspace's stamped summary). The
legacy layout itself is pinned: the crashed run's residue stays exactly
where the one-shot left it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tree_options.seal.g4_gate import G4GatePaths, production_gate_paths

REPO = Path(__file__).resolve().parents[2]

RUN_KEY = "a" * 64

# The pre-successor layout, byte-for-byte: nothing about the crashed run's
# locations may silently move under this lane.
LEGACY = G4GatePaths(
    evidence_root=REPO / "docs" / "evidence-logs" / "m4",
    registry=REPO / "artifacts" / "g4-sealed.db",
    artifacts_dir=REPO / "artifacts" / "g4-sealed",
    scratch_root=REPO / "artifacts" / "g4-sealed-scratch",
    era_census=REPO / "artifacts" / "census" / "43b0b040ea3c" / "census.json",
    replay_artifacts=REPO / "artifacts" / "g4-sealed-replay",
    mutation_report=REPO / "artifacts" / "m0-mutations.json",
    spot_proxy_v2=REPO / "artifacts" / "spot-proxy-v2.json",
)


def test_the_legacy_production_layout_is_pinned() -> None:
    """``run_key=None`` (the fixture lanes and the pre-declared gate CLI)
    keeps the exact legacy paths — the crashed run's residue is untouched
    history, never moved or reinterpreted."""
    assert production_gate_paths(REPO) == LEGACY


def test_a_run_key_scopes_the_sealed_workspace_under_g4_sealed_runs() -> None:
    """One workspace per sealed RUN: registry, artifacts, scratch, and the
    replay comparison dir all land under ``g4-sealed-runs/<run_key>/``, so a
    successor checkout (a different sealed_run_id) never collides with the
    crashed run's occupied outputs."""
    paths = production_gate_paths(REPO, run_key=RUN_KEY)
    root = REPO / "artifacts" / "g4-sealed-runs" / RUN_KEY
    assert paths.registry == root / "g4-sealed.db"
    assert paths.artifacts_dir == root / "artifacts"
    assert paths.scratch_root == root  # run_g4_sealed_event appends the scratch name
    assert paths.replay_artifacts == root / "replay"
    # the DECLARED per-checkout inputs are shared, never run-scoped; the
    # evidence root is a shared OUTPUT DESTINATION by design (the docs-side
    # triple at fixed filenames is superseded by a later event; the durable
    # per-run record is the stamped summary inside the run's workspace —
    # round 2, P2, disclosed)
    assert paths.evidence_root == LEGACY.evidence_root
    assert paths.era_census == LEGACY.era_census
    assert paths.mutation_report == LEGACY.mutation_report
    assert paths.spot_proxy_v2 == LEGACY.spot_proxy_v2


def test_two_run_keys_never_share_a_workspace() -> None:
    """The separation the lane exists for: distinct sealed runs derive
    distinct roots for every output location."""
    first = production_gate_paths(REPO, run_key="a" * 64)
    second = production_gate_paths(REPO, run_key="b" * 64)
    for field in ("registry", "artifacts_dir", "scratch_root", "replay_artifacts"):
        assert getattr(first, field) != getattr(second, field)


def test_a_run_key_must_be_a_sealed_run_id_shaped_token() -> None:
    """The run key names a directory under gitignored ``artifacts/`` — only a
    full 64-hex sealed-run-id token is accepted, so traversal, partial, or
    foreign-shaped keys can never steer the workspace path."""
    for bad in (
        "",
        "f" * 63,
        "f" * 65,
        "f" * 32,
        "F" * 64,
        "../" + "f" * 61,
        "f" * 62 + "/x",
        "g" * 64,
    ):
        with pytest.raises(ValueError, match="run key"):
            production_gate_paths(REPO, run_key=bad)
