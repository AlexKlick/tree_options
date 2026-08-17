"""Artifact stamping tests (INV-14): every artifact carries full provenance."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest


def _make_repo(tmp_path: Path, *, dirty: bool) -> Path:
    repo = tmp_path / ("clean_repo" if not dirty else "dirty_repo")
    repo.mkdir()
    (repo / "file.txt").write_text("tracked\n")
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init"],
        check=True,
    )
    if dirty:
        (repo / "uncommitted.txt").write_text("dirt\n")
    return repo


class TestBuildStamp:
    def test_not_a_repo_fails_closed(self, protocol, tmp_path):
        from tree_options.protocol.stamping import DirtyWorktreeError, build_stamp

        with pytest.raises(DirtyWorktreeError):
            build_stamp(
                protocol,
                trial_id="T-001",
                config={"model": "xgb"},
                dataset_manifest_hash="0" * 64,
                repo=tmp_path / "nonexistent",
            )

    def test_clean_repo_stamp(self, protocol, tmp_path):
        from tree_options.protocol.stamping import build_stamp

        repo = _make_repo(tmp_path, dirty=False)
        stamp = build_stamp(
            protocol,
            trial_id="T-001",
            config={"model": "xgb"},
            dataset_manifest_hash="1" * 64,
            repo=repo,
        )
        assert stamp.trial_id == "T-001"
        assert len(stamp.git_sha) == 40
        assert not stamp.git_sha.startswith("dirty:")
        assert len(stamp.protocol_hash) == 64
        assert len(stamp.config_hash) == 64
        assert stamp.dataset_manifest_hash == "1" * 64

    def test_dirty_worktree_rejected(self, protocol, tmp_path):
        from tree_options.protocol.stamping import DirtyWorktreeError, build_stamp

        repo = _make_repo(tmp_path, dirty=True)
        with pytest.raises(DirtyWorktreeError):
            build_stamp(
                protocol,
                trial_id="T-001",
                config={},
                dataset_manifest_hash="1" * 64,
                repo=repo,
            )

    def test_dirty_escape_hatch_records_dirtiness(self, protocol, tmp_path):
        from tree_options.protocol.stamping import build_stamp

        repo = _make_repo(tmp_path, dirty=True)
        stamp = build_stamp(
            protocol,
            trial_id="T-001",
            config={},
            dataset_manifest_hash="1" * 64,
            repo=repo,
            allow_dirty=True,
        )
        assert stamp.git_sha.startswith("dirty:")

    def test_config_hash_sensitive_to_values(self, protocol, tmp_path):
        from tree_options.protocol.stamping import build_stamp

        repo = _make_repo(tmp_path, dirty=False)
        s1 = build_stamp(protocol, trial_id="T", config={"a": 1}, dataset_manifest_hash="1" * 64, repo=repo)
        s2 = build_stamp(protocol, trial_id="T", config={"a": 2}, dataset_manifest_hash="1" * 64, repo=repo)
        assert s1.config_hash != s2.config_hash


class TestWriteArtifact:
    def _stamp(self, protocol, tmp_path, trial_id="T-002"):
        from tree_options.protocol.stamping import build_stamp

        repo = _make_repo(tmp_path, dirty=False)
        return build_stamp(
            protocol,
            trial_id=trial_id,
            config={"a": 1},
            dataset_manifest_hash="2" * 64,
            repo=repo,
        )

    def test_write_artifact_embeds_stamp(self, protocol, tmp_path):
        from tree_options.protocol.stamping import write_artifact

        stamp = self._stamp(protocol, tmp_path)
        out = tmp_path / "artifact.json"
        write_artifact(out, {"x": 1}, stamp)
        data = json.loads(out.read_text())
        assert data["stamp"]["trial_id"] == "T-002"
        assert data["stamp"]["protocol_hash"] == stamp.protocol_hash
        assert data["payload"] == {"x": 1}

    def test_write_artifact_refuses_payload_stamp_collision(self, protocol, tmp_path):
        from tree_options.protocol.stamping import write_artifact

        stamp = self._stamp(protocol, tmp_path, trial_id="T-003")
        out = tmp_path / "artifact.json"
        with pytest.raises(Exception, match="stamp"):
            write_artifact(out, {"stamp": {"trial_id": "EVIL"}}, stamp)

    def test_write_artifact_refuses_none_stamp(self, protocol, tmp_path):
        from tree_options.protocol.stamping import UnstampedArtifactError, write_artifact

        out = tmp_path / "artifact.json"
        with pytest.raises(UnstampedArtifactError):
            write_artifact(out, {"x": 1}, None)  # type: ignore[arg-type]
