"""Discovery CLI: python -m tree_options.trex.discovery --once|--serve|--probe"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from tree_options.trex.discovery.config import (
    DISCOVERY_CLIENT_ID,
    ScanConfig,
    load_scan_config,
)
from tree_options.trex.discovery.runner import run_once, serve

DEFAULT_CONFIG = Path("~/.config/trex/discovery.toml").expanduser()
REPO_ROOT = Path(__file__).resolve().parents[3]

log = logging.getLogger("trex.discovery")


def _load_config_or_default(path: Path) -> ScanConfig:
    if path.exists():
        return load_scan_config(path)
    repo_default = REPO_ROOT / "deploy" / "trex" / "discovery.toml"
    if repo_default.exists():
        log.warning("using repo default config %s (no %s)", repo_default, path)
        return load_scan_config(repo_default)
    raise FileNotFoundError(f"no discovery config at {path} or {repo_default}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="trex-discovery")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true", help="single scan, then exit")
    mode.add_argument("--serve", action="store_true", help="spool loop + daily auto scan")
    mode.add_argument("--probe", action="store_true", help="gateway capability report, no writes")
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--state-dir", type=Path, default=None)
    ap.add_argument("--underlying", action="append", default=[], help="probe universe")
    ap.add_argument("--client-id", type=int, default=DISCOVERY_CLIENT_ID)
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )

    if args.probe:
        from tree_options.trex.discovery.probe import probe_json
        from tree_options.trex.ibkr import IbkrTrex

        ibk = IbkrTrex(client_id=args.client_id)
        ibk.connect()
        try:
            underlyings = args.underlying or ["NVDA", "QQQ", "SPY"]
            print(json.dumps(probe_json(ibk, underlyings), indent=1, default=str))
        finally:
            ibk.disconnect()
        return 0

    cfg = _load_config_or_default(args.config)
    state_dir = args.state_dir or Path("~/.local/state/trex-discovery").expanduser()
    state_dir.mkdir(parents=True, exist_ok=True)

    if args.once:
        from tree_options.trex.discovery.gateway import IbkrDiscovery

        source = IbkrDiscovery(client_id=args.client_id)
        source.connect()
        try:
            path = run_once(source, cfg, state_dir, "manual", repo=REPO_ROOT)
        finally:
            source.disconnect()
        log.info("scan written: %s", path)
        return 0

    from tree_options.trex.discovery.gateway import IbkrDiscovery

    source = IbkrDiscovery(client_id=args.client_id)
    # NO startup connect (Codex-arch #2): a down gateway used to kill the
    # unit before the loop ever ran. serve() connects lazily per tick;
    # market/watch work proceeds with the broker unreachable.
    try:
        serve(source, cfg, state_dir, repo=REPO_ROOT)
    finally:
        source.disconnect()
    return 0


if __name__ == "__main__":
    sys.exit(main())
