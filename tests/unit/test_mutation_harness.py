"""Mutation harness source-copy boundary."""

from __future__ import annotations

import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_disposable_copy_excludes_generated_and_authority_artifacts() -> None:
    spec = importlib.util.spec_from_file_location(
        "tree_options_mutation_harness",
        REPO_ROOT / "scripts/mutate.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    ignored = set(module.DISPOSABLE_COPY_IGNORE)
    assert {"artifacts", "dist", ".git", ".venv"} <= ignored
