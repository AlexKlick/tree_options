"""Scan artifact persistence + the scan-on-demand spool (D3 protocol).

Artifact layout under ``~/.local/state/trex-discovery/``::

    latest.json                 newest scan (stamp + payload + run_id)
    runs/<run_id>/scan.json     every run, newest MAX_RUNS kept
    spool/scan.request.<id>     POSTed scan requests (web lane writes)
    spool/scan.claim.<id>       claimed by the runner (atomic link+unlink)
    spool/scan.result           last completed request outcome

Claim protocol (codex-review BLOCKER #8): the runner claims a request by
``link()``ing it to the claim name — exactly one consumer can win — then
unlinks the request file. A claim older than STALE_CLAIM_SECONDS is
treated as a crashed runner and reclaimed.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tree_options.protocol.stamping import config_hash_of

MAX_RUNS = 30
STALE_CLAIM_SECONDS = 600
RUN_ID_FORMAT = "%Y%m%dT%H%M%S.%fZ"


@dataclass(frozen=True)
class DiscoveryStamp:
    git_sha: str
    config_hash: str
    generated_at: str
    runner: str


def _git_state(repo: Path | None) -> tuple[str, bool] | None:
    """(sha, dirty) or None when outside a git repo / git unavailable."""
    if repo is None or not (repo / ".git").exists():
        return None
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo, capture_output=True, text=True, timeout=10, check=True,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo, capture_output=True, text=True, timeout=10,
        ).stdout.strip() != ""
        return (sha or "unknown", dirty)
    except (OSError, subprocess.SubprocessError):
        return None


def build_stamp(
    repo: Path | None,
    config: dict[str, Any],
    generated_at: datetime,
    runner: str,
) -> DiscoveryStamp:
    state = _git_state(repo)
    if state is None:
        sha = "unknown"
    else:
        sha, dirty = state
        if dirty:
            sha = f"dirty:{sha}"
    return DiscoveryStamp(
        git_sha=sha,
        config_hash=config_hash_of(config),
        generated_at=generated_at.isoformat(),
        runner=runner,
    )


def _run_id(now: datetime) -> str:
    return now.astimezone(UTC).strftime(RUN_ID_FORMAT)


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, default=str) + "\n")
    os.replace(tmp, path)


def write_scan(
    state_dir: Path, payload: dict[str, Any], stamp: DiscoveryStamp, now: datetime
) -> Path:
    """Persist one scan: runs/<run_id>/scan.json + latest.json (atomic)."""
    run_id = _run_id(now)
    doc = {"stamp": asdict(stamp), "run_id": run_id, "payload": payload}
    run_dir = state_dir / "runs" / run_id
    _atomic_write(run_dir / "scan.json", doc)
    _atomic_write(state_dir / "latest.json", doc)
    _prune_runs(state_dir)
    return run_dir / "scan.json"


def _prune_runs(state_dir: Path) -> None:
    runs = state_dir / "runs"
    if not runs.exists():
        return
    dirs = sorted((d for d in runs.iterdir() if d.is_dir()), key=lambda d: d.name)
    for old in dirs[:-MAX_RUNS] if len(dirs) > MAX_RUNS else []:
        for f in old.iterdir():
            f.unlink(missing_ok=True)
        old.rmdir()


def read_latest(state_dir: Path) -> dict[str, Any] | None:
    path = state_dir / "latest.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def read_runs(state_dir: Path, limit: int = 10) -> list[dict[str, Any]]:
    runs_dir = state_dir / "runs"
    if not runs_dir.exists():
        return []
    out: list[dict[str, Any]] = []
    for d in sorted((x for x in runs_dir.iterdir() if x.is_dir()), reverse=True)[:limit]:
        scan = d / "scan.json"
        if not scan.exists():
            continue
        try:
            doc = json.loads(scan.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        payload = doc.get("payload", {})
        out.append(
            {
                "run_id": doc.get("run_id", d.name),
                "generated_at": payload.get("generated_at"),
                "scanned": payload.get("data_quality", {}).get("underlyings_scanned", 0),
                "accepted": len(payload.get("candidates", [])),
                "rejected": len(payload.get("rejected", [])),
            }
        )
    return out


# -- spool (kind-based requests: scan, watch, market, backtest) -------------
#
# Durability contract (Codex-arch #1): the request file is RETAINED until
# durable completion - claim and request coexist, complete removes both.
# A runner crash mid-processing leaves the stale claim to be reclaimed,
# which returns the request to the pool (execution must be idempotent).
# Claims are ordered by request_ts, never by the random id filename.


def write_request(
    spool_dir: Path, kind: str, request_id: str, payload: dict[str, Any]
) -> Path:
    body = {"request_id": request_id, "kind": kind, **payload}
    _atomic_write(spool_dir / f"{kind}.request.{request_id}", body)
    return spool_dir / f"{kind}.request.{request_id}"


def claim_request(
    spool_dir: Path, kinds: list[str], now: datetime | None = None
) -> tuple[str, str, dict[str, Any]] | None:
    """Atomically claim the oldest pending request of the given kinds.

    link() is the POSIX atomic-claim: two runners racing on the same
    request cannot both win. Returns (kind, request_id, payload) or None.
    """
    if not spool_dir.exists():
        return None
    _reclaim_stale_claims(spool_dir)
    candidates: list[tuple[str, str, Path, dict[str, Any]]] = []
    for kind in kinds:
        for request in spool_dir.glob(f"{kind}.request.*"):
            try:
                payload = json.loads(request.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            ts = str(payload.get("request_ts", ""))
            candidates.append((ts, request.name, request, payload))
    for _ts, _name, request, payload in sorted(candidates):
        request_id = str(payload.get("request_id", ""))
        claim = spool_dir / f"{payload.get('kind', 'scan')}.claim.{request_id}"
        try:
            os.link(request, claim)  # atomic: fails if the claim exists
        except FileExistsError:
            continue  # another runner won this one
        return str(payload.get("kind", "scan")), request_id, payload
    return None


def complete_request(
    spool_dir: Path, kind: str, request_id: str, result: dict[str, Any]
) -> None:
    (spool_dir / f"{kind}.claim.{request_id}").unlink(missing_ok=True)
    (spool_dir / f"{kind}.request.{request_id}").unlink(missing_ok=True)
    _atomic_write(spool_dir / f"{kind}.result", result)


def read_result(spool_dir: Path, kind: str) -> dict[str, Any] | None:
    path = spool_dir / f"{kind}.result"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _reclaim_stale_claims(spool_dir: Path) -> None:
    cutoff = time.time() - STALE_CLAIM_SECONDS
    for claim in spool_dir.glob("*.claim.*"):
        try:
            if claim.stat().st_mtime < cutoff:
                claim.unlink(missing_ok=True)
        except OSError:
            continue


# -- scan-kind wrappers (existing names + wire format preserved) -------------


def write_scan_request(spool_dir: Path, request_id: str, request_ts: datetime) -> Path:
    return write_request(
        spool_dir, "scan", request_id, {"request_ts": request_ts.isoformat()}
    )


def spool_pending(spool_dir: Path) -> bool:
    return any(spool_dir.glob("scan.request.*")) if spool_dir.exists() else False


def claim_scan_request(
    spool_dir: Path, now: datetime | None = None
) -> tuple[str, dict[str, Any]] | None:
    claimed = claim_request(spool_dir, ["scan"], now=now)
    if claimed is None:
        return None
    _kind, request_id, payload = claimed
    return request_id, payload


def complete_scan(spool_dir: Path, request_id: str, result: dict[str, Any]) -> None:
    complete_request(spool_dir, "scan", request_id, result)


def read_scan_result(spool_dir: Path) -> dict[str, Any] | None:
    return read_result(spool_dir, "scan")
