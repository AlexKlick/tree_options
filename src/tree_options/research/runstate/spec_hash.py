"""Canonical hashing for ``ComparisonSpec`` and ``ResearchRun``.

Reuses ``tree_options.desk.contracts.canonical`` so the spec/content
hash scheme is identical between the desk evidence store and the
research lane's runstate store (RL §8: "every displayed numerical
claim must resolve to a result field or explicit derived formula").
"""

from __future__ import annotations

import hashlib

from tree_options.desk.contracts import canonical
from tree_options.research.contracts import ComparisonSpec


def spec_hash(spec: ComparisonSpec) -> str:
    """Stable sha256 over the canonicalised spec."""
    raw = spec.to_dict()
    # Decimal-as-string already in to_dict; canonical sorts and serialises.
    payload = canonical(raw)
    return hashlib.sha256(payload).hexdigest()


__all__ = ["spec_hash"]
