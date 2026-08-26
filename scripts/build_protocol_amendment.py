#!/usr/bin/env python
"""Build the protocol 0.2.1 amendment PROPOSAL — dry-run only (PR A3).

Consumes a coverage census artifact, OWNER-SUPPLIED values, and
OWNER-RATIFIED derivation rules, and emits a proposal packet under
artifacts/ only. The builder never chooses a threshold value (there is no
default for --owner-values), never writes a tracked file, and every packet
it emits says landed: false.

Exit codes (contract):
  0  built (proposal emitted; nothing landed)
  2  stale/invalid census, or the capture manifest drifted since the census
  3  owner-values/rules invalid (hidden default, bool-as-int, NaN/Infinity
     literal, census-binding mismatch, future-derived facts, value != rule)
  4  base/target protocol version violation
  5  output-root refusal (outside artifacts/)
  1  unexpected error

Usage:
  uv run --frozen python scripts/build_protocol_amendment.py \\
      --census artifacts/census/m4-coverage-census.json \\
      --owner-values /abs/path/owner-values.json \\
      --rules /abs/path/ratified-rules.json \\
      --capture-manifest artifacts/capture/m4b-manifest.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT / "src") not in sys.path:  # pragma: no cover - import plumbing
    sys.path.insert(0, str(REPO_ROOT / "src"))

from tree_options.protocol.amendment import (  # noqa: E402
    AmendmentError,
    DerivationMismatchError,
    OutputRefusedError,
    OwnerValuesError,
    StaleCensusError,
    VersionError,
    build_proposed_amendment,
)

DEFAULT_PROTOCOL = REPO_ROOT / "research_protocol.yaml"
DEFAULT_OUT_ROOT = REPO_ROOT / "artifacts" / "amendment"

_EXIT_CODES: tuple[tuple[type[AmendmentError], int], ...] = (
    (StaleCensusError, 2),
    (OwnerValuesError, 3),
    (DerivationMismatchError, 3),
    (VersionError, 4),
    (OutputRefusedError, 5),
)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--census", type=Path, required=True, help="coverage census JSON (content-hash verified)"
    )
    parser.add_argument(
        "--owner-values",
        type=Path,
        required=True,
        help="OWNER-supplied values JSON; no default is ever invented",
    )
    parser.add_argument(
        "--rules", type=Path, required=True, help="OWNER-ratified derivation rules JSON"
    )
    parser.add_argument(
        "--capture-manifest",
        type=Path,
        required=True,
        help="the capture manifest the census must still describe",
    )
    parser.add_argument(
        "--protocol",
        type=Path,
        default=DEFAULT_PROTOCOL,
        help=f"base protocol YAML (default: {DEFAULT_PROTOCOL.name} at the repo root)",
    )
    parser.add_argument(
        "--out-root",
        type=Path,
        default=DEFAULT_OUT_ROOT,
        help="proposal root under artifacts/ (default: artifacts/amendment)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        packet = build_proposed_amendment(
            args.census,
            args.owner_values,
            args.rules,
            protocol_path=args.protocol,
            capture_manifest_path=args.capture_manifest,
            out_root=args.out_root,
        )
    except AmendmentError as exc:
        code = next((c for kind, c in _EXIT_CODES if isinstance(exc, kind)), 1)
        print(f"AMENDMENT REFUSED ({type(exc).__name__}): {exc}", file=sys.stderr)
        return code
    print(json.dumps(json.loads(packet.model_dump_json()), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
