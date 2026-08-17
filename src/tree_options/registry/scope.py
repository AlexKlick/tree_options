"""Canonical trial scope (audit §5.1): caller-invented scope keys are how the
32-cap gets evaded. A scope is a STRUCTURE, canonicalized and hashed; the
registry accepts only canonical scope keys."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass

PREFIX = "scope-v1:"


@dataclass(frozen=True)
class TrialScope:
    protocol_id: str
    protocol_hash: str
    outer_fold_id: str
    target_horizon: str
    feature_set_id: str
    model_family: str

    def canonical_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))

    def scope_key(self) -> str:
        digest = hashlib.sha256(self.canonical_json().encode()).hexdigest()
        return f"{PREFIX}{digest}"

    @staticmethod
    def is_canonical(scope_key: str) -> bool:
        if not scope_key.startswith(PREFIX):
            return False
        body = scope_key[len(PREFIX) :]
        return len(body) == 64 and all(c in "0123456789abcdef" for c in body)
