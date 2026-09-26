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

Boundary contract (RL §9 + RL1-04): the research workspace must never
overlap the desk's state or paper-trades trees — not equal to them, not
inside them, and not containing them. ``assert_no_overlap_with_desk``
enforces CONTAINMENT in both directions over fully resolved paths
(symlinks included) and fails CLOSED when resolution itself fails: an
unresolvable path is unsafe until proven otherwise, never waved through.

Every entry point that opens a research workspace validates the path it
was ACTUALLY given — ``attach(workspace=...)`` and
``open_runstate_store(workspace)`` call this guard on their explicit
argument, not only on environment-derived defaults.
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


def _resolve_strict(path: Path) -> Path:
    """Resolve symlinks, failing closed on resolution errors (an
    unresolvable path cannot be proven safe)."""
    try:
        return path.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise RuntimeError(
            f"research path {path} could not be resolved; failing closed "
            f"until it can be proven outside the desk workspace ({exc})"
        ) from exc


def _overlaps(a: Path, b: Path) -> bool:
    """True when either path contains the other (equality counts)."""
    return a == b or b in a.parents or a in b.parents


def assert_no_overlap_with_desk(*, workspace: Path | None = None) -> None:
    """Refuse any research path that overlaps a desk/treasury tree.

    Called from the FastAPI ``attach_research`` hook (on the workspace it
    actually resolved), from ``open_runstate_store`` on its explicit
    argument, and by the lane gate. With ``workspace`` supplied, the
    check targets that path; without it, the environment-derived
    defaults are checked (backward-compatible gate behavior).
    """
    forbidden = (_resolve_strict(state_root()), _resolve_strict(paper_dir()))
    targets: list[tuple[str, Path]] = []
    if workspace is not None:
        targets.append(("workspace", workspace))
    else:
        targets.extend((
            ("workspace", workspace_root()),
            ("adapter_dir", adapter_dir()),
            ("cache_dir", cache_dir()),
        ))
    for label, path in targets:
        resolved = _resolve_strict(path)
        for desk_root in forbidden:
            if _overlaps(resolved, desk_root):
                raise RuntimeError(
                    f"research path {label}={resolved} collides with the "
                    f"desk workspace {desk_root}; choose a different "
                    f"research workspace (equality, descendant, and "
                    f"ancestor all count as overlap)"
                )
