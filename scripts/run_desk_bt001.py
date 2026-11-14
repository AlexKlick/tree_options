#!/usr/bin/env python3
"""Run (or dry-run) DESK-BT-001, the sealed never-run modeled options backtest.

--dry-run (default): the synthetic corpus end-to-end, including the seal
verification against this checkout's sealed documents, the measured-haircut
path on synthetic chain files, the placebo draw and the six variant cells;
writes nothing outside --out-dir (a fresh temp dir unless given) and reads
no real artifact for pricing.

--scored: the ONE sealed run. Run it only after the M0 gate passes, under
host-work admission, with DESK_STORE / DESK_PAPER_DIR pointing at the
serving checkout's artifacts (read-only) and --cache at its massive-cache:

    DESK_STORE=/home/alexk/documents/tree_options/artifacts/desk-store \
    DESK_PAPER_DIR=/home/alexk/documents/tree_options/artifacts/paper-trades \
    .venv/bin/python scripts/run_desk_bt001.py --scored \
        --cache /home/alexk/documents/tree_options/artifacts/massive-cache

Outputs land in $DESK_STORE/evaluations/DESK-BT-001.{json,md} (or --out-dir);
an existing pair is refused (one scored run; a rerun is a reproducibility
check to a temp --out-dir, never a new verdict).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tree_options.desk import bt001, bt001_corpus  # noqa: E402
from tree_options.desk.ivhist import RateSource  # noqa: E402
from tree_options.desk.paths import paper_dir, store_root  # noqa: E402
from tree_options.trex.clock import session_calendar  # noqa: E402

CALENDAR_REL = Path("data/calendar/trex/nyse_sessions_2018_01_02_2028_12_29.json")


def _git_head() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True
        )
        return out.stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return None


def _load_earnings(path: Path) -> tuple[dict[str, list[str]], str]:
    doc = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict):
        raise SystemExit(f"REFUSED: {path} is not an object")
    return (
        {str(k): [str(d) for d in v] for k, v in doc.items() if isinstance(v, list)},
        hashlib.sha256(path.read_bytes()).hexdigest(),
    )


def dry_run(args: argparse.Namespace, now: datetime) -> int:
    tmp = Path(tempfile.mkdtemp(prefix="bt001-dryrun-")) if args.out_dir is None else args.out_dir
    world = bt001_corpus.synthetic_world(tmp / "corpus")
    inputs = world.inputs(docs=ROOT / "docs" / "desk", repo=ROOT, code_commit=_git_head(), now=now)
    out_dir = tmp / "out"
    result = bt001.run(
        inputs,
        out_dir=out_dir,
        store_pins=world.store_pins,
        slice_pins=world.slice_pins,
        scored=False,
    )
    checks = bt001_corpus.self_check(result, world)
    print(f"dry-run OK: {len(checks)} machinery checks passed")
    for label in checks:
        print(f"  - {label}")
    verdicts = {k: v["verdict"] for k, v in sorted(result["variants"].items())}
    print(f"corpus variant verdicts (machinery proof, not a study result): {verdicts}")
    print(f"outputs: {out_dir / 'DESK-BT-001.json'}")
    try:
        bt001.run(
            inputs,
            out_dir=out_dir,
            store_pins=world.store_pins,
            slice_pins=world.slice_pins,
            scored=False,
        )
    except bt001.Refused as exc:
        print(f"one-shot refusal verified: {exc}")
        return 0
    print("REFUSED NOT raised on a second write -- a defect", file=sys.stderr)
    return 1


def scored_run(args: argparse.Namespace, now: datetime) -> int:
    if args.cache is None:
        raise SystemExit("REFUSED: --scored needs --cache (the serving checkout's massive-cache)")
    store = store_root()
    paper = paper_dir()
    panel_path = paper / "ohlc-panel.json"
    if not panel_path.exists():
        raise SystemExit(f"REFUSED: panel missing: {panel_path}")
    from tree_options.desk.panel import read_panel_with_sha256

    panel, panel_sha = read_panel_with_sha256(panel_path)
    earnings, earnings_sha = _load_earnings(paper / "earnings-calendar.json")
    dtb3_path = store / "indices" / "DTB3.csv"
    if not dtb3_path.exists():
        raise SystemExit(f"REFUSED: DTB3 missing: {dtb3_path}")
    cal = session_calendar()
    print(f"scanning the massive cache at {args.cache} (A5 scope; this takes a few minutes)")
    scan = bt001.scan_sealed_cache(Path(args.cache), cal)
    rates = RateSource.from_store(dtb3_path)
    inputs = bt001.Inputs(
        panel=panel,
        panel_sha256=panel_sha,
        earnings=earnings,
        earnings_sha256=earnings_sha,
        dtb3_path=dtb3_path,
        cal=cal,
        calendar_path=ROOT / CALENDAR_REL,
        rates=rates,
        scan=scan,
        store=store,
        docs=ROOT / "docs" / "desk",
        repo=ROOT,
        code_commit=_git_head(),
        now=now,
    )
    out_dir = args.out_dir or (store / "evaluations")
    result = bt001.run(inputs, out_dir=out_dir, scored=True)
    print(f"DESK-BT-001 scored run complete -> {out_dir}")
    for sig, verdict in sorted(result["signal_verdicts"].items()):
        cells = {s: result["variants"][f"{sig}|{s}"]["verdict"] for s in bt001.STRUCTURES}
        print(f"  {sig}: {verdict} ({cells})")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="synthetic corpus (default)")
    mode.add_argument("--scored", action="store_true", help="the ONE sealed run")
    parser.add_argument("--cache", type=Path, default=None, help="massive-cache directory")
    parser.add_argument("--out-dir", type=Path, default=None, help="output directory")
    args = parser.parse_args(argv)
    now = datetime.now(UTC)
    if args.scored:
        return scored_run(args, now)
    return dry_run(args, now)


if __name__ == "__main__":
    raise SystemExit(main())
