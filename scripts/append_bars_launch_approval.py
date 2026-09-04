"""Append the owner's BARS_LAUNCH_APPROVAL record (owner-runnable).

``append_bars_launch_approval`` in ``tree_options.data.bars_manifest`` is
the library seam; this script is the OWNER-RUNNABLE form of it — the
deliberate, inspectable act that opens (or continues) a bars era. The
window-A-extension continuation (2026-09-04) runs under a NEW record
binding the LIVE 0.2.2 protocol hash, the new work manifest's raw file
sha256, the verified census's content hash, and the ratified amendment
packet's sha:

  uv run python scripts/append_bars_launch_approval.py \\
      --protocol research_protocol.yaml \\
      --amendment-packet artifacts/amendment/022-declaration/5caf56568941/amendment-packet.json \\
      --census artifacts/census/43b0b040ea3c/census.json \\
      --work-manifest artifacts/bars/work-manifest-ext-1-c1.json \\
      --reason "owner standing authorization <date>: window-A-extension
                 continuation, Fridays 2026-08-28..2026-09-04, protocol 0.2.2"

Fail-closed, exactly-once:
  - every bound hash is computed from the bytes on disk IN THIS invocation
    (the protocol through the repo loader, the census through its own
    fail-closed verification, the work manifest parsed + self-hash bound
    from ONE bytes read) — nothing is taken on the caller's word;
  - the record's work_manifest_sha256 is the RAW FILE sha256, exactly what
    the launcher's authority join compares — parity by construction;
  - a duplicate (protocol, work manifest) tuple refuses under the ledger
    lock: the existing record already grants it, and a new approval needs
    a NEW work manifest (one manifest, one approval, one launch);
  - the append itself is the library's hash-chained, flock-held,
    fsync-durable write — this tool never touches the ledger bytes any
    other way.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
for _entry in (REPO_ROOT / "src", REPO_ROOT / "scripts"):
    if str(_entry) not in sys.path:  # pragma: no cover - import plumbing
        sys.path.insert(0, str(_entry))

from launch_bars_era import REQUIRED_BARS_PROTOCOL_VERSION  # noqa: E402
from tree_options.data.bars_manifest import (  # noqa: E402
    DEFAULT_BARS_AUTHORITY_ROOT,
    BarsManifestError,
    DuplicateApprovalRefusedError,
    append_bars_launch_approval,
    parse_bars_work_manifest,
    work_manifest_content_sha256,
)
from tree_options.data.coverage_census import CoverageCensus, verify_census  # noqa: E402
from tree_options.protocol.loader import load_protocol, protocol_hash  # noqa: E402
from tree_options.seal.errors import SealError  # noqa: E402


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--protocol",
        type=Path,
        default=REPO_ROOT / "research_protocol.yaml",
        help="protocol YAML (default: research_protocol.yaml at the repo root;"
        " the record binds the LOADED protocol hash, exactly what the"
        " launcher's gate compares)",
    )
    parser.add_argument(
        "--amendment-packet",
        type=Path,
        required=True,
        help="the ratified amendment packet JSON; the record binds its raw file sha256",
    )
    parser.add_argument(
        "--census",
        type=Path,
        required=True,
        help="the census JSON; parsed + fail-closed verified here, and the"
        " record binds its content hash",
    )
    parser.add_argument(
        "--work-manifest",
        type=Path,
        required=True,
        help="the work manifest the approval grants; parsed + self-hash bound"
        " from one bytes read, and the record binds its RAW FILE sha256"
        " (the launcher's authority join compares exactly that)",
    )
    parser.add_argument(
        "--reason",
        required=True,
        help="the owner-visible why, recorded verbatim on the ledger line",
    )
    parser.add_argument(
        "--authority-root",
        type=Path,
        default=DEFAULT_BARS_AUTHORITY_ROOT,
        help=f"bars-authority ledger root (default: {DEFAULT_BARS_AUTHORITY_ROOT},"
        " relative to the invoking checkout)",
    )
    parser.add_argument(
        "--at-epoch",
        type=int,
        help="the record's timestamp (default: now; override only in fixtures)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if not args.reason.strip():
        print("REFUSED: --reason is required (the approval records its why)", file=sys.stderr)
        return 2
    # every binding is computed from the bytes on disk in THIS invocation —
    # the same discipline the launcher's gates apply at verify time
    try:
        protocol = load_protocol(args.protocol)
    except Exception as exc:  # the loader's own error family
        print(f"REFUSED: protocol does not load: {exc}", file=sys.stderr)
        return 2
    current_hash = protocol_hash(protocol)
    if protocol.meta.protocol_version != REQUIRED_BARS_PROTOCOL_VERSION:
        print(
            f"REFUSED: protocol version {protocol.meta.protocol_version!r} is not"
            f" the live {REQUIRED_BARS_PROTOCOL_VERSION} — the continuation"
            " approval binds the live protocol (the launcher's re-opened gate;"
            " see launch_bars_era.REQUIRED_BARS_PROTOCOL_VERSION)",
            file=sys.stderr,
        )
        return 2
    try:
        packet_sha = hashlib.sha256(args.amendment_packet.read_bytes()).hexdigest()
    except OSError as exc:
        print(f"REFUSED: amendment packet unreadable: {exc}", file=sys.stderr)
        return 2
    try:
        census = CoverageCensus.model_validate_json(args.census.read_text(encoding="utf-8"))
        verify_census(census)
    except (OSError, ValueError) as exc:
        print(f"REFUSED: census invalid or tampered: {exc}", file=sys.stderr)
        return 2
    try:
        raw = args.work_manifest.read_bytes()
    except OSError as exc:
        print(f"REFUSED: work manifest unreadable: {exc}", file=sys.stderr)
        return 2
    try:
        manifest = parse_bars_work_manifest(raw, source=str(args.work_manifest))
    except BarsManifestError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    if work_manifest_content_sha256(manifest) != manifest.content_sha256:
        print(
            "REFUSED: the work manifest's content_sha256 does not bind its body"
            " — an approval never binds a manifest that cannot re-verify",
            file=sys.stderr,
        )
        return 2
    work_file_sha = hashlib.sha256(raw).hexdigest()
    print(
        f"binding: protocol {current_hash[:12]}… ({REQUIRED_BARS_PROTOCOL_VERSION}),"
        f" packet {packet_sha[:12]}…, census {census.content_sha256[:12]}…,"
        f" work manifest {work_file_sha[:12]}…"
        f" ({len(manifest.entries)} entries)"
    )
    try:
        record = append_bars_launch_approval(
            args.authority_root,
            protocol_hash=current_hash,
            amendment_packet_sha256=packet_sha,
            census_sha256=census.content_sha256,
            work_manifest_sha256=work_file_sha,
            reason=args.reason,
            at_epoch=args.at_epoch if args.at_epoch is not None else int(time.time()),
        )
    except DuplicateApprovalRefusedError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 3
    except (BarsManifestError, SealError) as exc:
        print(f"REFUSED: the append refused: {exc}", file=sys.stderr)
        return 3
    print(f"appended: record {record.record_sha256}")
    print(
        "NEXT: the launcher's protocol gate now opens on this record; the"
        " continuation preflight's documented answer remains the census clause"
        " (exit 3 — the census deliberately stays sealed at the coverage era)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
