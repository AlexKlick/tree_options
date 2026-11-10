"""Pure policy for reusing already validated TREX plan TOML models.

Ordinary uniquely identified plans do not need to be resolved and reparsed.
Ambiguous IDs and filename precedence retain the existing legacy resolver.
"""
from __future__ import annotations

from pathlib import Path
from typing import Mapping


def requires_legacy_resolution(
    plan_id: str, parsed_path: Path, plans_root: Path, id_counts: Mapping[str, int]
) -> bool:
    """Preserve duplicate-ID and literal <id>.toml precedence semantics."""
    if id_counts.get(plan_id) != 1:
        return True
    candidate = plans_root / f"{plan_id}.toml"
    return candidate.exists() and candidate != parsed_path
