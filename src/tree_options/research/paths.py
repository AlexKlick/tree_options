"""Where the research lane reads and writes (resolved at call time,
env-overridable). Mirrors ``tree_options.desk.paths`` so the existing
admission / unit conventions apply unchanged.

* ``RESEARCH_WORKSPACE_DIR``: the research workspace root, default
  ``~/.local/state/trex-research/<run-id>/``. The implementer picks the
  ``<run-id>`` at run time (sha256 of the ``ComparisonSpec`` is the
  recommended choice). Everything the research lane writes — catalogs,
  comparisons, evidence envelopes, runs — lives here.
* ``RESEARCH_ADAPTER_DIR``: where the catalog adapter Python files live,
  default ``<repo>/data/research/adapters/`` (tracked, sha-pinned
  alongside the playbook and miner files).
* ``RESEARCH_CACHE_DIR``: where the catalog and evidence caches live,
  default ``<RESEARCH_WORKSPACE_DIR>/.cache/`` (untracked; safe to wipe).

Tests must pin all of these to tmp: nothing here caches a value. A path
overlap guard in ``attach_research`` (see ``tree_options.trex_web.research_view``)
refuses to wire the lane if any of these resolves to the desk evidence
or paper-trades directory.
"""

from __future__ import annotations

import os
from pathlib import Path

from tree_options.desk.paths import paper_dir, state_root


def _env_path(var: str) -> Path | None:
    raw = os.environ.get(var, "").strip()
    return Path(raw) if raw else None


def workspace_root(run_id: str | None = None) -> Path:
    """Research workspace root.

    ``RESEARCH_WORKSPACE_DIR`` overrides; otherwise
    ``~/.local/state/trex-research/<run-id>/``. ``run_id`` defaults to a
    timestamp suffix so two simultaneous lanes don't collide; the
    implementer usually passes the spec sha instead for repeatability.
    """
    explicit = _env_path("RESEARCH_WORKSPACE_DIR")
    if explicit is not None:
        return explicit
    suffix = run_id or f"run-{os.environ.get('TREX_RUN_ID', '') or 'anon'}"
    return Path.home() / ".local" / "state" / "trex-research" / suffix


def adapter_dir() -> Path:
    """Where typed catalog adapters live (in-repo, tracked)."""
    return _env_path("RESEARCH_ADAPTER_DIR") or (
        Path(__file__).resolve().parents[3] / "data" / "research" / "adapters"
    )


def cache_dir() -> Path:
    """Research cache root (caches content-addressed by sha + spec)."""
    return _env_path("RESEARCH_CACHE_DIR") or workspace_root() / ".cache"


def assert_no_overlap_with_desk() -> None:
    """Refuse to wire the research lane if any research path collides with
    a desk/treasury path. Called from the FastAPI ``attach_research`` hook.

    The boundary is part of the RL §9 contract: research writes to its own
    workspace, not the live evidence audit chain.
    """
    forbidden = {state_root().resolve(), paper_dir().resolve()}
    for label, path in (
        ("workspace", workspace_root()),
        ("adapter_dir", adapter_dir()),
        ("cache_dir", cache_dir()),
    ):
        try:
            resolved = path.resolve()
        except (FileNotFoundError, OSError):
            continue
        if resolved in forbidden:
            raise RuntimeError(
                f"research path {label}={resolved} collides with a desk "
                f"workspace; choose a different RESEARCH_* env var"
            )
