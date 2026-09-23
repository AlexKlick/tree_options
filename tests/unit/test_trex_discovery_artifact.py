"""C7: scan artifact stamping + the D3 claim-by-rename spool protocol."""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tree_options.protocol.stamping import config_hash_of
from tree_options.trex.discovery.artifact import (
    MAX_RUNS,
    STALE_CLAIM_SECONDS,
    DiscoveryStamp,
    build_stamp,
    claim_scan_request,
    complete_scan,
    read_latest,
    read_runs,
    read_scan_result,
    spool_pending,
    write_scan,
    write_scan_request,
)

NOW = datetime(2026, 9, 22, 21, 54, 5, tzinfo=UTC)


def _stamp() -> DiscoveryStamp:
    return DiscoveryStamp(
        git_sha="abc1234",
        config_hash=config_hash_of({"a": 1}),
        generated_at=NOW.isoformat(),
        runner="manual",
    )


def _payload() -> dict:
    return {
        "generated_at": NOW.isoformat(),
        "mode": "manual",
        "data_quality": {"chains_available": True},
        "candidates": [{"underlying": "NVDA", "yield_ratio": 8.6}],
        "rejected": [],
    }


class TestStamp:
    def test_config_hash_is_stable(self) -> None:
        s = _stamp()
        assert s.config_hash == config_hash_of({"a": 1})

    def test_git_rev_prefixed_when_dirty(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "tree_options.trex.discovery.artifact._git_state",
            lambda repo: ("abc1234", True),
        )
        stamp = build_stamp(repo=tmp_path, config={}, generated_at=NOW, runner="manual")
        assert stamp.git_sha.startswith("dirty:abc1234")

    def test_git_rev_unknown_outside_repo(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "tree_options.trex.discovery.artifact._git_state",
            lambda repo: None,
        )
        stamp = build_stamp(repo=tmp_path / "nope", config={}, generated_at=NOW, runner="manual")
        assert stamp.git_sha == "unknown"


class TestWriteScan:
    def test_latest_and_run_written_with_stamp(self, tmp_path: Path) -> None:
        path = write_scan(tmp_path, _payload(), _stamp(), now=NOW)
        assert path.parent.name.startswith("20")  # runs/<run_id>/scan.json
        latest = read_latest(tmp_path)
        assert latest is not None
        assert latest["stamp"]["git_sha"] == "abc1234"
        assert latest["payload"]["candidates"][0]["underlying"] == "NVDA"
        assert latest["run_id"] == path.parent.name

    def test_atomic_no_tmp_left(self, tmp_path: Path) -> None:
        write_scan(tmp_path, _payload(), _stamp(), now=NOW)
        assert list(tmp_path.rglob("*.tmp")) == []

    def test_old_runs_pruned(self, tmp_path: Path) -> None:
        for i in range(MAX_RUNS + 2):
            write_scan(tmp_path, _payload(), _stamp(), now=NOW + timedelta(seconds=i))
        runs = read_runs(tmp_path, limit=MAX_RUNS)
        assert len(runs) == MAX_RUNS
        assert sorted(p.name for p in (tmp_path / "runs").iterdir()) == sorted(
            r["run_id"] for r in runs
        )

    def test_runs_summary_counts(self, tmp_path: Path) -> None:
        write_scan(tmp_path, _payload(), _stamp(), now=NOW)
        runs = read_runs(tmp_path)
        assert runs[0]["accepted"] == 1
        assert runs[0]["rejected"] == 0
        assert runs[0]["generated_at"] == NOW.isoformat()


class TestSpool:
    def test_request_is_atomic_and_pending(self, tmp_path: Path) -> None:
        write_scan_request(tmp_path, "req-1", NOW)
        assert not list(tmp_path.glob("*.tmp"))
        assert spool_pending(tmp_path) is True

    def test_claim_is_atomic_exactly_one_winner(self, tmp_path: Path) -> None:
        write_scan_request(tmp_path, "req-1", NOW)
        first = claim_scan_request(tmp_path, now=NOW)
        second = claim_scan_request(tmp_path, now=NOW)
        assert first is not None
        assert first[0] == "req-1"
        assert second is None  # the request is claimed; no read-then-unlink race
        # retain-until-complete: the request persists while claimed, so a
        # crashed runner's acknowledged request is never lost
        assert spool_pending(tmp_path) is True
        complete_scan(tmp_path, "req-1", {"request_id": "req-1", "status": "ok"})
        assert spool_pending(tmp_path) is False

    def test_complete_scan_writes_result_and_frees_claim(self, tmp_path: Path) -> None:
        write_scan_request(tmp_path, "req-1", NOW)
        claim_scan_request(tmp_path, now=NOW)
        result = {"request_id": "req-1", "status": "ok", "run_id": "r1"}
        complete_scan(tmp_path, "req-1", result)
        assert list(tmp_path.glob("scan.claim.*")) == []
        assert list(tmp_path.glob("scan.request.*")) == []
        got = read_scan_result(tmp_path)
        assert got is not None and got["request_id"] == "req-1"

    def test_stale_claim_is_reclaimed(self, tmp_path: Path) -> None:
        write_scan_request(tmp_path, "req-1", NOW)
        claim_scan_request(tmp_path, now=NOW)
        # simulate a runner that died mid-scan: age the claim
        claim = next(tmp_path.glob("scan.claim.*"))
        stale = time.time() - STALE_CLAIM_SECONDS - 5
        os.utime(claim, (stale, stale))
        # reclaim RETURNS the retained request to the pool (a crashed
        # runner's acknowledged request is re-executed, not lost), then
        # the later request follows in ts order
        write_scan_request(tmp_path, "req-2", NOW + timedelta(seconds=1))
        won = claim_scan_request(tmp_path, now=NOW + timedelta(seconds=2))
        assert won is not None
        assert won[0] == "req-1"  # requeued after the stale reclaim
        complete_scan(tmp_path, "req-1", {"request_id": "req-1", "status": "ok"})
        again = claim_scan_request(tmp_path, now=NOW + timedelta(seconds=3))
        assert again is not None and again[0] == "req-2"

    def test_empty_spool_is_none(self, tmp_path: Path) -> None:
        assert spool_pending(tmp_path) is False
        assert claim_scan_request(tmp_path, now=NOW) is None
        assert read_scan_result(tmp_path) is None

    def test_request_payload_carries_id_and_ts(self, tmp_path: Path) -> None:
        write_scan_request(tmp_path, "req-9", NOW)
        raw = json.loads(next(tmp_path.glob("scan.request.*")).read_text())
        assert raw["request_id"] == "req-9"
        assert raw["request_ts"] == NOW.isoformat()


class TestSpoolHardeningCodexM456:
    """Codex M4-M6 review #1/#2/#16 regression pins."""

    def test_receipt_lands_before_request_is_removed(self, tmp_path: Path) -> None:
        from unittest import mock

        from tree_options.trex.discovery import artifact

        spool = tmp_path / "spool"
        artifact.write_request(spool, "propose", "r1", {"request_ts": "2026-09-22T19:00:00"})
        assert artifact.claim_request(spool, ["propose"]) is not None
        with mock.patch.object(artifact, "_atomic_write", side_effect=OSError("disk full")):
            try:
                artifact.complete_request(spool, "propose", "r1", {"status": "ok"})
            except OSError:
                pass
        # the receipt failed, so the request must survive to be retried
        assert (spool / "propose.request.r1").exists()

    def test_unpublished_tmp_request_is_never_claimed(self, tmp_path: Path) -> None:
        from tree_options.trex.discovery import artifact

        spool = tmp_path / "spool"
        spool.mkdir()
        (spool / "propose.request.r2.tmp").write_text(
            json.dumps({"request_id": "r2", "kind": "propose", "request_ts": "x"})
        )
        assert artifact.claim_request(spool, ["propose"]) is None
        assert artifact.spool_pending(spool) is False

    def test_claim_age_is_claim_time_not_queue_time(self, tmp_path: Path) -> None:
        from tree_options.trex.discovery import artifact

        spool = tmp_path / "spool"
        req = artifact.write_request(spool, "backtest", "r3", {"request_ts": "x"})
        old = time.time() - 3 * 3600  # queued long before it was claimed
        os.utime(req, (old, old))
        assert artifact.claim_request(spool, ["backtest"]) is not None
        # a second consumer's reclaim sweep must NOT treat the fresh claim as stale
        assert artifact.claim_request(spool, ["backtest"]) is None
        assert (spool / "backtest.claim.r3").exists()
