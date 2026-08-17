"""Artifact stamping (INV-14).

Every performance/metrics artifact must carry the exact Git SHA, protocol
hash, config hash, dataset-manifest hash, and trial ID. `write_artifact`
structurally requires a stamp — there is no default — so an unstamped
artifact cannot be produced through this path.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

from tree_options.protocol.loader import protocol_hash
from tree_options.protocol.schema import ResearchProtocol


class DirtyWorktreeError(RuntimeError):
    """Refusing to stamp an artifact from a dirty worktree."""


class UnstampedArtifactError(RuntimeError):
    """Payload already contains a stamp key or stamp is missing."""


@dataclass(frozen=True)
class ArtifactStamp:
    git_sha: str
    protocol_hash: str
    config_hash: str
    dataset_manifest_hash: str
    trial_id: str


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def config_hash_of(config: dict) -> str:
    canonical = json.dumps(config, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_stamp(
    protocol: ResearchProtocol,
    *,
    trial_id: str,
    config: dict,
    dataset_manifest_hash: str,
    repo: Path | None = None,
    allow_dirty: bool = False,
) -> ArtifactStamp:
    """Build the mandatory provenance stamp for an artifact.

    Fails closed on a dirty worktree unless `allow_dirty` is explicitly set;
    in that case the dirtiness is recorded in the sha itself ("dirty:<sha>")
    so the artifact can never masquerade as a clean-tree result.
    """
    repo = repo or Path.cwd()
    try:
        sha = _git(repo, "rev-parse", "HEAD")
        dirty = bool(_git(repo, "status", "--porcelain"))
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        raise DirtyWorktreeError(f"not a usable git repository: {repo}") from exc

    if dirty and not allow_dirty:
        raise DirtyWorktreeError(
            "worktree is dirty; commit before stamping artifacts or pass allow_dirty=True"
        )
    git_sha = f"dirty:{sha}" if dirty else sha

    return ArtifactStamp(
        git_sha=git_sha,
        protocol_hash=protocol_hash(protocol),
        config_hash=config_hash_of(config),
        dataset_manifest_hash=dataset_manifest_hash,
        trial_id=trial_id,
    )


def write_artifact(path: Path, payload: dict, stamp: ArtifactStamp) -> None:
    """Write {stamp, payload} as JSON. The stamp is not optional (INV-14)."""
    if stamp is None:
        raise UnstampedArtifactError("artifact requires an ArtifactStamp; got None")
    if not isinstance(stamp, ArtifactStamp):
        raise UnstampedArtifactError(f"artifact requires an ArtifactStamp; got {type(stamp)}")
    if "stamp" in payload:
        raise UnstampedArtifactError(
            "payload must not carry its own 'stamp' key; provenance is added by the writer"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {"stamp": asdict(stamp), "payload": payload}
    path.write_text(json.dumps(body, sort_keys=True, indent=2) + "\n", encoding="utf-8")
