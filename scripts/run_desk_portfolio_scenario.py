#!/usr/bin/env python3
"""Project one frozen exploratory replay through a $5,000 risk budget."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tree_options.desk.portfolio_replay import simulate  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--replay", type=Path, required=True)
    parser.add_argument("--capital", type=Decimal, default=Decimal("5000"))
    parser.add_argument("--max-trade-loss", type=Decimal, default=Decimal("300"))
    parser.add_argument("--max-open-loss", type=Decimal, default=Decimal("1500"))
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    if (
        args.replay.is_symlink()
        or not args.replay.is_file()
        or args.replay.stat().st_size > 20_000_000
    ):
        parser.error("--replay must be a regular file of at most 20 MB")
    source = args.replay.read_bytes()
    replay = json.loads(source)
    result = simulate(
        replay,
        capital=args.capital,
        max_trade_loss=args.max_trade_loss,
        max_open_loss=args.max_open_loss,
    )
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout.strip()
    dirty = bool(
        subprocess.run(
            ["git", "status", "--porcelain"], cwd=ROOT, check=True, capture_output=True, text=True
        ).stdout
    )
    result["provenance"] = {
        "replay_path": str(args.replay.resolve()),
        "replay_sha256": hashlib.sha256(source).hexdigest(),
        "code_head": head,
        "code_dirty": dirty,
    }
    raw = json.dumps(result, indent=2, sort_keys=True, allow_nan=False).encode() + b"\n"
    stem = (
        f"portfolio-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-"
        f"{hashlib.sha256(raw).hexdigest()[:12]}.json"
    )
    args.out_dir.mkdir(parents=True, exist_ok=True)
    target = args.out_dir / stem
    with target.open("xb") as stream:
        stream.write(raw)
    print(f"portfolio-scenario variants={len(result['variants'])} -> {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
