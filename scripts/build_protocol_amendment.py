#!/usr/bin/env python
"""Build the protocol amendment PROPOSAL — dry-run only (PR A3, 0.2.2-extended).

Two modes, both structurally incapable of landing anything — no tracked
file is ever written, and every packet says landed: false.

--target-version 0.2.1 (the default; byte-identical to the original
single-purpose builder): consumes a coverage census artifact, OWNER-
SUPPLIED values, and OWNER-RATIFIED derivation rules, and emits the 0.2.1
threshold proposal packet under artifacts/ only. The builder never chooses
a threshold value (there is no default for --owner-values).

--target-version 0.2.2 (owner ruling m4-022-ruling-20260828): constructs
the 0.2.2 lane-on declaration packet from the standing 0.2.1 protocol —
the version bump plus the three owner-ruled declarations (underlying
liquidity term EVALUATED; the earnings DISCLOSED ABSENCE; the fill door's
DECISION-GRID close source), each recorded with its owner-decision id.
The PROJECTED post-flip protocol hash is COMPUTED through the real
loader's own hashing and recorded in the packet, never applied: the yaml
flip is a post-closeout follow-up. No census/owner-values/rules inputs —
the declarations are ruled facts, not derived values.

Exit codes (contract):
  0  built (proposal emitted; nothing landed)
  2  stale/invalid census, or the capture manifest drifted since the census
  3  owner-values/rules invalid (hidden default, bool-as-int, NaN/Infinity
     literal, census-binding mismatch, future-derived facts, value != rule)
  4  base/target protocol version violation
  5  output-root refusal (outside artifacts/)
  1  unexpected error

Usage (0.2.1 threshold proposal):
  uv run --frozen python scripts/build_protocol_amendment.py \\
      --census artifacts/census/m4-coverage-census.json \\
      --owner-values /abs/path/owner-values.json \\
      --rules /abs/path/ratified-rules.json \\
      --capture-manifest artifacts/capture/m4b-manifest.json

Usage (0.2.2 declaration packet):
  uv run --frozen python scripts/build_protocol_amendment.py \\
      --target-version 0.2.2
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
    AmendmentDeclarationPacket,
    AmendmentError,
    AmendmentPacket,
    DerivationMismatchError,
    OutputRefusedError,
    OwnerValuesError,
    StaleCensusError,
    VersionError,
    build_declaration_amendment,
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

# The 0.2.1 mode's four derivation inputs are required TOGETHER and only
# in that mode; the 0.2.2 declaration packet derives from nothing but the
# base protocol and the owner ruling.
_THRESHOLD_MODE_INPUTS = ("census", "owner_values", "rules", "capture_manifest")
_FLAG_FOR: dict[str, str] = {
    "census": "--census",
    "owner_values": "--owner-values",
    "rules": "--rules",
    "capture_manifest": "--capture-manifest",
}


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--target-version",
        choices=("0.2.1", "0.2.2"),
        default="0.2.1",
        help=(
            "which amendment packet to construct: 0.2.1 (the census-bound"
            " threshold proposal, the original single-purpose builder) or"
            " 0.2.2 (the owner-ruled lane-on declaration packet)"
        ),
    )
    parser.add_argument(
        "--census",
        type=Path,
        default=None,
        help="coverage census JSON (content-hash verified; 0.2.1 mode only)",
    )
    parser.add_argument(
        "--owner-values",
        type=Path,
        default=None,
        help="OWNER-supplied values JSON; no default is ever invented (0.2.1 mode only)",
    )
    parser.add_argument(
        "--rules",
        type=Path,
        default=None,
        help="OWNER-ratified derivation rules JSON (0.2.1 mode only)",
    )
    parser.add_argument(
        "--capture-manifest",
        type=Path,
        default=None,
        help="the capture manifest the census must still describe (0.2.1 mode only)",
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
    args = parser.parse_args(argv)
    if args.target_version == "0.2.1":
        missing = [
            _FLAG_FOR[name] for name in _THRESHOLD_MODE_INPUTS if getattr(args, name) is None
        ]
        if missing:
            parser.error(
                f"--target-version 0.2.1 requires {' '.join(missing)}: the"
                " threshold builder never invents an owner value or a census"
            )
    return args


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        if args.target_version == "0.2.2":
            packet: AmendmentPacket | AmendmentDeclarationPacket = build_declaration_amendment(
                protocol_path=args.protocol,
                out_root=args.out_root,
            )
        else:
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
