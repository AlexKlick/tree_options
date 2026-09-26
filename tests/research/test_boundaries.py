"""HARD-BOUNDARY guard: no module under ``tree_options.research`` may
import the broker stack. This is the only enforcement mechanism — the
package docstring states the same rule but docstrings don't fail CI.

Also a path-overlap guard: research workspace / adapter / cache paths
must not resolve to the desk evidence or paper-trades directories
(RL §9: research writes to its own workspace, not the live evidence
audit chain).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tree_options.desk.paths import paper_dir, state_root
from tree_options.research.paths import (
    assert_no_overlap_with_desk,
    cache_dir,
    workspace_root,
)

# Forbidden broker-side imports. ``tree_options.research`` MUST NOT depend on
# any of these — the research lane is broker-free by construction.
FORBIDDEN_TOP_LEVEL = ("tree_options.trex",)


def _collect_imports(path: Path) -> list[tuple[int, str]]:
    """Return (line_no, module) for every import statement in ``path``."""
    try:
        source = path.read_text(encoding="utf-8")
    except (FileNotFoundError, UnicodeDecodeError):
        return []
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return []
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                hits.append((node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if node.level:  # `from . import x` — relative import; leave alone
                continue
            hits.append((node.lineno, mod))
    return hits


def _walk_research_sources(root: Path) -> list[Path]:
    pkg = root / "src" / "tree_options" / "research"
    return [p for p in pkg.rglob("*.py") if p.is_file()]


def test_no_broker_imports_in_research() -> None:
    root = Path(__file__).resolve().parents[2]
    forbidden_fragments = {
        # Anything under tree_options.trex other than .trex_web (the FastAPI
        # web surface, which IS allowed: research routes live there) and
        # .trex.discovery (the broker-free discovery runner).
        "tree_options.trex.ibkr",
        "tree_options.trex.monitor",
        "tree_options.trex.gateway_watch",
        "tree_options.trex.enter",
    }
    offenders: list[tuple[Path, int, str]] = []
    for path in _walk_research_sources(root):
        for lineno, module in _collect_imports(path):
            for frag in forbidden_fragments:
                if module == frag or module.startswith(frag + "."):
                    offenders.append((path, lineno, module))
    assert not offenders, (
        "tree_options.research must not import broker-side modules:\n  "
        + "\n  ".join(f"{p.relative_to(root)}:{ln} imports {m}" for p, ln, m in offenders)
    )


def test_workspace_root_default_does_not_collide_with_desk_state() -> None:
    """Default ``workspace_root()`` must not resolve inside the desk state
    or paper-trades directories."""
    ws = workspace_root().resolve()
    forbidden = {state_root().resolve(), paper_dir().resolve()}
    assert ws not in forbidden, (
        f"workspace_root()={ws} collides with desk path; "
        "set RESEARCH_WORKSPACE_DIR to a different location"
    )
    assert not any(ws == f or f in ws.parents for f in forbidden), (
        "workspace_root() must not live under a desk path"
    )


def test_adapter_dir_default_does_not_collide_with_desk_state() -> None:
    from tree_options.research.paths import adapter_dir

    ad = adapter_dir().resolve()
    forbidden = {state_root().resolve(), paper_dir().resolve()}
    assert ad not in forbidden
    assert not any(ad == f or f in ad.parents for f in forbidden)


def test_cache_dir_default_does_not_collide_with_desk_state() -> None:
    cd = cache_dir().resolve()
    forbidden = {state_root().resolve(), paper_dir().resolve()}
    assert cd not in forbidden
    assert not any(cd == f or f in cd.parents for f in forbidden)


def test_assert_no_overlap_raises_on_collision(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the operator misconfigures RESEARCH_WORKSPACE_DIR to point at the
    desk evidence dir, ``attach_research`` must refuse."""
    monkeypatch.setenv("RESEARCH_WORKSPACE_DIR", str(state_root()))
    with pytest.raises(RuntimeError, match="collides"):
        assert_no_overlap_with_desk()


def test_assert_no_overlap_raises_on_paper_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RESEARCH_ADAPTER_DIR", str(paper_dir()))
    with pytest.raises(RuntimeError, match="collides"):
        assert_no_overlap_with_desk()
