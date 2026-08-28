#!/usr/bin/env python3
"""Materialize the lane-1 Cboe manifest for the G4 sealed-event preflight.

``scripts/g4_seal.py preflight`` requires ``--lane1-manifest`` (a
``RealOptionsManifest`` JSON) plus ``--lane1-source`` (the Cboe CSV it pins);
no script materialized that artifact. This is the deterministic, custody-clean
materializer:

1. Custody FIRST: the source is read once under no-follow custody
   (``seal.input_custody.read_file_once``) and its sha256 must equal the pin
   (``--expect-sha256``, default the retained sample's SHA256SUMS hash). A
   missing source or a hash mismatch is a NAMED refusal — nothing is parsed,
   nothing is written, and no other file is ever defaulted to.
2. Parse EXACTLY the bytes already read, through the adapter's existing
   parser (``parse_cboe_eod_csv`` via its documented ``raw=`` seam) with the
   pinned file's declared variant (``no_cgi``) and one underlying selection
   (``--underlying``, default SPY — the retained demo bundles four
   underlyings and the adapter refuses multi-underlying files without a
   selection). Parsing is never re-implemented here.
3. Build via the REAL lane construction — ``build_real_overlay(parsed)`` then
   ``build_real_options_manifest(parsed, overlay=...)`` — the same calls
   ``tests/unit/test_g4_verified_inputs.py`` uses and the G4 verifier's own
   re-parse (``seal/verified_inputs._verify_lane1``) replays; there is no
   test shortcut being mirrored.
4. Write ``manifest.model_dump_json(indent=2) + "\\n"`` to ``--out``
   (mkdir -p the parent), then print the source custody hash, the written
   manifest's raw sha256 (the value the G4 packet binds as lane-1
   ``raw_sha256``), the re-read output file's sha256, and the
   ``verify_real_options_manifest_tokens`` result.

Determinism: the manifest is a pure function of (source bytes, variant,
underlying) — no timestamps anywhere in the model. The one field that could
carry environment state is ``RealOptionsManifest.source_path`` (it echoes the
parser's path argument), so the parse is handed the source's DELIVERY
FILENAME, never an absolute path: two runs over the same bytes are
byte-identical from any working directory. Byte identity is anchored by
``source_sha256``, which is what the G4 verifier cross-checks (it never
compares ``source_path``).

The live bars-era capture owns ``artifacts/bars*``,
``artifacts/m4b-coverage-era`` and ``runstate``; a ``--out`` intersecting
those protected components is refused before anything is read.

Exit codes: 0 written and verified; 2 named custody refusal; 3 adapter,
custody-reader, or serialization failure.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT / "src") not in sys.path:  # pragma: no cover - import plumbing
    sys.path.insert(0, str(REPO_ROOT / "src"))

from tree_options.data.cboe_eod import (  # noqa: E402
    CboeEodError,
    EodVariant,
    RealOptionsManifest,
    RealOptionsManifestError,
    build_real_options_manifest,
    parse_cboe_eod_csv,
    verify_real_options_manifest_tokens,
)
from tree_options.data.digest import sha256_hex  # noqa: E402
from tree_options.data.real_overlay import build_real_overlay  # noqa: E402
from tree_options.seal.errors import VerifiedInputsError  # noqa: E402
from tree_options.seal.input_custody import read_file_once  # noqa: E402

# The retained Cboe sample (~/m2-evidence/cboe-sample/, SHA256SUMS intact):
# the no_cgi product file is the campaign's canonical lane-1 source, pinned
# by the m3 spike (docs/m3-options-schema-spike.md) and docs/m4-real-data-plan.md.
PINNED_SOURCE_SHA256 = "e45af427934177f944d8fe51a5871f8f1e33c4dbd0d8b7fbf8be238eee3d173c"
CANONICAL_SOURCE = Path(
    "/home/alexk/m2-evidence/cboe-sample/"
    "UnderlyingOptionsEODCalcs_2023-08-25_no_cgi_subscription.csv"
)
DEFAULT_OUT_RELATIVE = Path("artifacts/lane1/cboe-manifest.json")
# The pinned file's declared product variant (its filename) — recorded in the
# manifest and replayed by the G4 verifier's re-parse, so it is a constant of
# the pinned source rather than a caller choice.
LANE1_VARIANT: EodVariant = "no_cgi"
# The retained demo bundles SPY/TSLA/^SPX/^VIX; the adapter refuses
# multi-underlying files without a selection. SPY is the campaign's symbol
# (the plan's "SPY root", the G4 fixtures' underlying).
DEFAULT_UNDERLYING = "SPY"
# Live bars-era capture territory (hard lane constraint): never write here.
PROTECTED_COMPONENTS = frozenset({"bars", "bars-authority", "m4b-coverage-era", "runstate"})


class Lane1CustodyError(RuntimeError):
    """Named custody refusal: missing/wrong-hash source or protected output."""


def _refuse_protected_out(out: Path) -> None:
    """A lane-1 artifact never intersects the live capture's directories."""
    absolute = Path(os.path.abspath(os.fspath(out)))
    hit = sorted(part for part in absolute.parts if part in PROTECTED_COMPONENTS)
    if hit:
        raise Lane1CustodyError(
            f"custody refusal: --out {out} intersects protected live-capture path "
            f"component(s) {hit} — artifacts/bars*, artifacts/bars-authority, "
            "artifacts/m4b-coverage-era and runstate belong to the running "
            "bars-era capture; lane 1 writes only to its own directory"
        )


def build_lane1_manifest(
    source: Path, *, expect_sha256: str, underlying: str
) -> RealOptionsManifest:
    """Custody-gate, parse, and build the manifest (free of side effects)."""
    if not source.is_file():
        raise Lane1CustodyError(
            f"custody refusal: source {source} does not exist — the pinned "
            "retained sample is required and nothing is defaulted to"
        )
    raw = read_file_once(source, component="lane1", purpose="Cboe source CSV")
    observed = sha256_hex(raw)
    if observed != expect_sha256:
        raise Lane1CustodyError(
            f"custody refusal: {source.name} sha256 {observed} != pinned "
            f"{expect_sha256} — the pinned bytes are required; nothing was "
            "parsed or written"
        )
    # The parser's path argument only labels the result (error prefixes and
    # the manifest's source_path field): the bytes parsed are exactly the
    # custody-read ones via the documented `raw=` seam, and the delivery
    # FILENAME keeps the payload free of absolute paths.
    parsed = parse_cboe_eod_csv(
        Path(source.name), variant=LANE1_VARIANT, underlying=underlying, raw=raw
    )
    overlay = build_real_overlay(parsed)
    return build_real_options_manifest(parsed, overlay=overlay)


def _write_manifest(manifest: RealOptionsManifest, out: Path) -> bytes:
    payload = (manifest.model_dump_json(indent=2) + "\n").encode("utf-8")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(payload)
    written = out.read_bytes()
    if written != payload:
        raise RealOptionsManifestError(
            f"write integrity: {out} does not hold the serialized manifest"
        )
    return written


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=CANONICAL_SOURCE,
        help="Cboe Option EOD summary CSV (default: the retained no_cgi sample)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help=(
            "output manifest path (default: artifacts/lane1/cboe-manifest.json "
            "under the repo root — never an era path)"
        ),
    )
    parser.add_argument(
        "--underlying",
        default=DEFAULT_UNDERLYING,
        help=(
            "one underlying symbol selected from the bundled file "
            "(recorded in the manifest; the G4 verifier replays it)"
        ),
    )
    parser.add_argument(
        "--expect-sha256",
        default=PINNED_SOURCE_SHA256,
        help=(
            "custody pin for the source bytes (default: the retained no_cgi "
            "sample's SHA256SUMS hash)"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    out = args.out if args.out is not None else REPO_ROOT / DEFAULT_OUT_RELATIVE
    try:
        _refuse_protected_out(out)
        manifest = build_lane1_manifest(
            args.source, expect_sha256=args.expect_sha256, underlying=args.underlying
        )
        verify_real_options_manifest_tokens(manifest)
        written = _write_manifest(manifest, out)
    except Lane1CustodyError as exc:
        print(exc, file=sys.stderr)
        return 2
    except (CboeEodError, RealOptionsManifestError, VerifiedInputsError, OSError) as exc:
        print(f"lane1 manifest build failed: {exc}", file=sys.stderr)
        return 3
    print(f"lane1 manifest written: {out}")
    print(f"source sha256 (custody pin matched): {manifest.source_sha256}")
    print(f"manifest raw_sha256: {sha256_hex(written)}")
    print(f"output file sha256 (re-read): {sha256_hex(out.read_bytes())}")
    print("verify_real_options_manifest_tokens: OK")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry
    sys.exit(main())
