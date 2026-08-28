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


def load_protocol_bytes(data: bytes) -> ResearchProtocol:
    """Validate the frozen protocol from bytes ALREADY READ (round-3
    amendment fix, 2026-08-23): callers that hash the same bytes they parse
    must not re-read the file through a path."""
    return ResearchProtocol.model_validate(yaml.safe_load(data.decode("utf-8")))


def load_protocol(path: Path | str | None = None) -> ResearchProtocol:
    """Load and validate the frozen protocol. Raises on any defect."""
    p = resolve_protocol_path(path)
    return load_protocol_bytes(p.read_bytes())


@lru_cache(maxsize=8)
def _load_cached(path_str: str) -> ResearchProtocol:
    return load_protocol(Path(path_str))


def default_protocol() -> ResearchProtocol:
    """Cached default protocol (repo root or env override)."""
    return _load_cached(str(resolve_protocol_path()))


def canonical_json(protocol: ResearchProtocol) -> str:
    dump = protocol.model_dump(mode="json")
    # (P1-4, Codex round 1) The canonical hash represents what the yaml
    # DECLARES. `underlying_liquidity_term` is 0.2.2 PRE-DRAFT machinery
    # (w7a): the standing 0.2.1 yaml does not declare it, so a member that
    # merely equals its default ("evaluated") must NOT ride the identity —
    # the defaulted field silently re-hashed the untouched yaml and broke
    # the bars-authority ledger binding. A DECLARED
    # "dropped_no_equity_aggregates" (!= default) is a semantic protocol
    # fact and DOES ride the hash by design, and at the 0.2.2 version bump
    # the hash changes by design anyway. Surgical on purpose: a blanket
    # exclude_defaults/exclude_unset would drop other always-defaulted
    # members and change the hash a second time.
    liquidity = dump.get("option_candidate_defaults", {}).get("liquidity_volume_flow")
    if isinstance(liquidity, dict) and liquidity.get("underlying_liquidity_term") == "evaluated":
        del liquidity["underlying_liquidity_term"]
    return json.dumps(dump, sort_keys=True, separators=(",", ":"))


def protocol_hash(protocol: ResearchProtocol) -> str:
    return hashlib.sha256(canonical_json(protocol).encode("utf-8")).hexdigest()


def raw_file_hash(path: Path | str | None = None) -> str:
    return hashlib.sha256(resolve_protocol_path(path).read_bytes()).hexdigest()
