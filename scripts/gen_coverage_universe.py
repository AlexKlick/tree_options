#!/usr/bin/env python
"""Generate the DECLARED universe x Friday-grid work manifest (PR A/A2).

Parses the era wrapper ONCE (`--underlyings` list + the `for d in …` Friday
list), validates the grid (ISO dates, Fridays, sorted, unique; names
unique), and writes the committed manifest `data/coverage/coverage_universe.json`
with a domain-separated content hash. Re-running against the same wrapper
is byte-identical.

Owner decision 2026-08-23: the census derives expected counts from THIS
manifest — 29 underlyings x 105 Fridays = 3,045 masters — NOT from the
"30 x 105 = 3,150" in the era doc / G4 plan. The 30-vs-29 discrepancy is
recorded in the Phase-0 reconciliation report for owner reconciliation at
era-results; this generator does not edit those documents.

Exit codes: 0 written; 2 wrapper unreadable/unparseable; 3 grid invalid.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT / "src") not in sys.path:  # pragma: no cover - import plumbing
    sys.path.insert(0, str(REPO_ROOT / "src"))

from tree_options.data.coverage_census import (  # noqa: E402
    UNIVERSE_SCHEMA_VERSION,
    CoverageUniverse,
    universe_content_sha256,
    verify_universe,
)
from tree_options.time.expiries import is_friday  # noqa: E402

DEFAULT_OUT = Path("data/coverage/coverage_universe.json")

_DATES_RE = re.compile(r"for d in ([^;]+); do")
_UNDERLYINGS_RE = re.compile(r"--underlyings ([A-Z0-9.,]+)")


def parse_wrapper(text: str, *, source: str) -> tuple[list[str], list[str]]:
    """Extract (underlyings, as_of dates) from an era-wrapper script."""
    dates_match = _DATES_RE.search(text)
    if not dates_match:
        raise ValueError(f"{source}: no 'for d in …; do' date list found")
    names_match = _UNDERLYINGS_RE.search(text)
    if not names_match:
        raise ValueError(f"{source}: no --underlyings list found")
    dates = dates_match.group(1).split()
    names = names_match.group(1).split(",")
    return names, dates


def validate_grid(names: list[str], dates: list[str], *, source: str) -> None:
    if not names or any(not n for n in names):
        raise ValueError(f"{source}: empty underlying name in {names!r}")
    if len(set(names)) != len(names):
        raise ValueError(f"{source}: duplicate underlyings")
    if len(set(dates)) != len(dates):
        raise ValueError(f"{source}: duplicate as_of dates")
    parsed = [date.fromisoformat(d) for d in dates]
    if parsed != sorted(parsed):
        raise ValueError(f"{source}: as_of dates are not sorted ascending")
    non_fridays = [d for d in parsed if not is_friday(d)]
    if non_fridays:
        raise ValueError(f"{source}: non-Friday grid dates: {non_fridays}")


def build_universe(text: str, *, source: str) -> CoverageUniverse:
    names, dates = parse_wrapper(text, source=source)
    validate_grid(names, dates, source=source)
    underlyings = sorted(names)
    fridays = sorted(dates)
    universe = CoverageUniverse(
        schema_version=UNIVERSE_SCHEMA_VERSION,
        source=source,
        source_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        underlyings=tuple(underlyings),
        as_of_fridays=tuple(fridays),
        expected_masters=len(underlyings) * len(fridays),
        notes=(
            "declared from the era wrapper (owner decision 2026-08-23):"
            " expected counts derive from THIS manifest, not from the docs'"
            " 30/3,150; the 29-vs-30 discrepancy is an owner reconciliation"
            " at era-results",
        ),
        content_sha256="",
    )
    return universe.model_copy(update={"content_sha256": universe_content_sha256(universe)})


def render(universe: CoverageUniverse) -> str:
    return (
        json.dumps(
            json.loads(universe.model_dump_json()),
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--from-run-sh", type=Path, required=True, help="era wrapper script path")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)
    try:
        text = args.from_run_sh.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"WRAPPER UNREADABLE: {exc}", file=sys.stderr)
        return 2
    try:
        universe = build_universe(text, source=str(args.from_run_sh))
    except ValueError as exc:
        print(f"WRAPPER INVALID: {exc}", file=sys.stderr)
        return 3
    verify_universe(universe)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render(universe), encoding="utf-8")
    print(
        json.dumps(
            {
                "out": str(args.out),
                "underlyings": len(universe.underlyings),
                "as_of_fridays": len(universe.as_of_fridays),
                "expected_masters": universe.expected_masters,
                "content_sha256": universe.content_sha256,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
