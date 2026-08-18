"""Protocol loading and canonical hashing.

The canonical protocol hash is SHA-256 over the canonical JSON of the
*validated* model (sorted keys, compact separators) — a comment-only edit to
the YAML does not change it; any semantic edit does. The raw file SHA-256 is
additionally available for audit trails that want byte-level identity.
"""

from __future__ import annotations

import hashlib
import json
import os
from functools import lru_cache
from pathlib import Path

import yaml

from tree_options.protocol.schema import ResearchProtocol

_PROTOCOL_ENV_VAR = "TREE_OPTIONS_PROTOCOL"


def _repo_root() -> Path:
    # src/tree_options/protocol/loader.py -> repo root is parents[3].
    return Path(__file__).resolve().parents[3]


def resolve_protocol_path(path: Path | str | None = None) -> Path:
    """Resolve which YAML file is the protocol.

    Precedence: explicit path, then TREE_OPTIONS_PROTOCOL env var, then the
    repo-root default. Fails closed if the resolved file does not exist.
    """
    if path is not None:
        p = Path(path)
    elif env := os.environ.get(_PROTOCOL_ENV_VAR):
        p = Path(env)
    else:
        p = _repo_root() / "research_protocol.yaml"
    if not p.is_file():
        raise FileNotFoundError(f"protocol file not found: {p}")
    return p


def load_protocol(path: Path | str | None = None) -> ResearchProtocol:
    """Load and validate the frozen protocol. Raises on any defect."""
    p = resolve_protocol_path(path)
    raw = p.read_text(encoding="utf-8")
    data = yaml.safe_load(raw)
    return ResearchProtocol.model_validate(data)


@lru_cache(maxsize=8)
def _load_cached(path_str: str) -> ResearchProtocol:
    return load_protocol(Path(path_str))


def default_protocol() -> ResearchProtocol:
    """Cached default protocol (repo root or env override)."""
    return _load_cached(str(resolve_protocol_path()))


def canonical_json(protocol: ResearchProtocol) -> str:
    return json.dumps(protocol.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))


def protocol_hash(protocol: ResearchProtocol) -> str:
    return hashlib.sha256(canonical_json(protocol).encode("utf-8")).hexdigest()


def raw_file_hash(path: Path | str | None = None) -> str:
    return hashlib.sha256(resolve_protocol_path(path).read_bytes()).hexdigest()
