"""Synthetic bars-era scenario fixtures (PR A4).

Hand-built, vendor-shaped capture data for the ATM-grid work manifest and
the launcher gates. Raw JSON TEXT via `massive_structural_sample`'s row
builders (never Python floats), synthetic census through the REAL
`coverage_census` models, and a 0.2.1-shaped protocol produced through the
repo's own loader/schema models. No network, no API key, no host paths.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import yaml

from tests.fixtures.massive_structural_sample import contract_result, contracts_payload
from tree_options.runstate import RunIdentity

AS_OF = "2025-03-05"
MONTHLY_EXPIRY = "2025-04-18"  # the third Friday of April 2025
NON_MONTHLY_EXPIRY = "2025-04-04"  # the first Friday: excluded by expiries=monthly
OUT_OF_BAND_EXPIRY = "2025-03-07"  # 2 DTE: excluded by the 30-60 band
SPOT = {"SPY": {AS_OF: "580.00"}, "I:SPX": {AS_OF: "5750.00"}}
T0 = 1_800_000_000
BOOT = "11111111-2222-3333-4444-555555555555"
RUN_ID = "m4-barstest-20260823-abcdef12"
# The synthetic census's provenance pins. Round-2 review fix (2026-08-23,
# finding 5): the execute path cross-joins the run-state identity's code_sha
# and universe_manifest_sha256 against the VERIFIED census provenance, so a
# matching identity defaults to exactly these values.
CENSUS_CODE_SHA = "f" * 40
CENSUS_UNIVERSE_MANIFEST_SHA256 = "c" * 64

# The expected ATM grid at 2025-04-18 (spot 580, band 3): distinct strikes
# ranked |strike - spot| (ties by strike): 580, 587.5, 570, 590.
EXPECTED_ENTRIES: tuple[tuple[str, str, str, int, str, str], ...] = (
    ("SPY", AS_OF, MONTHLY_EXPIRY, 0, "call", "O:SPY250418C00580000"),
    ("SPY", AS_OF, MONTHLY_EXPIRY, 0, "put", "O:SPY250418P00580000"),
    ("SPY", AS_OF, MONTHLY_EXPIRY, 1, "call", "O:SPY250418C00587500"),
    ("SPY", AS_OF, MONTHLY_EXPIRY, 2, "call", "O:SPY250418C00570000"),
    ("SPY", AS_OF, MONTHLY_EXPIRY, 2, "put", "O:SPY250418P00570000"),
    ("SPY", AS_OF, MONTHLY_EXPIRY, 3, "call", "O:SPY250418C00590000"),
    ("SPY", AS_OF, MONTHLY_EXPIRY, 3, "put", "O:SPY250418P00590000"),
)


def _row(ticker: str, strike: str, kind: str, expiration: str = MONTHLY_EXPIRY) -> str:
    return contract_result(
        ticker=ticker,
        underlying="SPY",
        expiration=expiration,
        strike=strike,
        contract_type=kind,
    )


SPY_ROWS: tuple[str, ...] = (
    _row("O:SPY250418C00580000", "580", "call"),
    _row("O:SPY250418P00580000", "580", "put"),
    _row("O:SPY250418C00587500", "587.5", "call"),  # fractional: the exactness pin
    _row("O:SPY250418C00570000", "570", "call"),
    _row("O:SPY250418P00570000", "570", "put"),
    _row("O:SPY250418C00590000", "590", "call"),
    _row("O:SPY250418P00590000", "590", "put"),
    _row("O:SPY250404C00575000", "575", "call", expiration=NON_MONTHLY_EXPIRY),
    _row("O:SPY250307C00560000", "560", "call", expiration=OUT_OF_BAND_EXPIRY),
)

SPX_ROWS: tuple[str, ...] = (
    contract_result(
        ticker="O:SPX250321C05800000",
        underlying="I:SPX",
        expiration="2025-03-21",  # 16 DTE: out of band, so SPX contributes no entry
        strike="5800",
        contract_type="call",
        exercise_style="european",
        primary_exchange="XCBO",
    ),
)


def write_bars_capture(capture_dir: Path) -> Path:
    """Masters + spot proxy for one (SPY, I:SPX) x as_of synthetic capture."""
    masters = capture_dir / "masters"
    masters.mkdir(parents=True, exist_ok=True)
    (masters / "spy_2025-03-05.json").write_text(
        contracts_payload(results=SPY_ROWS, as_of=AS_OF), encoding="utf-8"
    )
    (masters / "spx_2025-03-05.json").write_text(
        contracts_payload(results=SPX_ROWS, as_of=AS_OF), encoding="utf-8"
    )
    (capture_dir / "spot_proxy.json").write_text(
        json.dumps(SPOT, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return capture_dir


def write_capture_manifest(capture_dir: Path, path: Path) -> Path:
    """A self-consistent capture manifest over the synthetic capture."""
    from tree_options.data.massive_manifest import build_massive_capture_manifest

    manifest = build_massive_capture_manifest(
        capture_dir,
        capture_version="m4b-capture/1",
        budget_limit=45,
        requests_charged=5,
        client_stats={"requests": 5},
        masters=[
            {
                "underlying": "SPY",
                "as_of": AS_OF,
                "pages": 1,
                "rows": len(SPY_ROWS),
                "complete": True,
                "truncated": False,
                "error": None,
                "file": "spy_2025-03-05.json",
            },
            {
                "underlying": "I:SPX",
                "as_of": AS_OF,
                "pages": 1,
                "rows": len(SPX_ROWS),
                "complete": True,
                "truncated": False,
                "error": None,
                "file": "spx_2025-03-05.json",
            },
        ],
        bars=[],
        spot_proxy=SPOT,
        notes=["synthetic bars-era scenario (tests/fixtures/bars_sample.py)"],
    )
    path.write_text(manifest.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return path


def census_bytes(manifest_bytes: bytes) -> bytes:
    """A self-consistent census bound to exactly these manifest bytes,
    built directly through the coverage_census models."""
    from tree_options.data.coverage_census import (
        CENSUS_SCHEMA_VERSION,
        CensusFact,
        CensusProvenance,
        CensusValues,
        CoverageBlock,
        CoverageCensus,
        PairCoverage,
        census_content_sha256,
    )

    census = CoverageCensus(
        schema_version=CENSUS_SCHEMA_VERSION,
        provenance=CensusProvenance(
            code_sha=CENSUS_CODE_SHA,
            protocol_hash="a" * 64,
            protocol_raw_sha256="b" * 64,
            input_manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
            universe_manifest_sha256=CENSUS_UNIVERSE_MANIFEST_SHA256,
            uv_lock_sha256="d" * 64,
            command=("uv", "run", "--frozen", "python", "scripts/inspect_structural_coverage.py"),
        ),
        coverage=CoverageBlock(expected_masters=2, observed=PairCoverage(COMPLETE=2)),
        values=CensusValues(
            observed_census_fact={
                "era_observed_masters": CensusFact(v=2, support={"census": 1}, confidence="EXACT")
            },
            predeclared_derivation_input={},
            owner_ratified_policy_value={},
            not_yet_decided={
                "flow_min_session_volume": "AWAITING_OWNER_RULE (owner decision 2026-08-23)"
            },
        ),
        value_registry={
            "era_observed_masters": "observed_census_fact",
            "flow_min_session_volume": "not_yet_decided",
        },
        content_sha256="",
    )
    census = census.model_copy(update={"content_sha256": census_content_sha256(census)})
    return census.model_dump_json().encode("utf-8")


def write_021_protocol(path: Path, base: Path) -> Path:
    """A 0.2.1-shaped protocol built from the REAL base through the repo's own
    models (load -> model_dump -> bump -> dump), so it loads through today's
    real loader. Fixture-only: nothing lands anywhere."""
    from tree_options.protocol.loader import load_protocol

    base_protocol = load_protocol(base)
    data = base_protocol.model_dump(mode="json")
    data["meta"]["protocol_version"] = "0.2.1"
    amendments = list(data["meta"]["amendments"])
    amendments.append(
        {
            "version": "0.2.1",
            "date": "PENDING-OWNER-RATIFICATION",
            "decision": "test fixture only (tests/fixtures/bars_sample.py)",
            "changes": "fixture base for the A4 launcher gate tests",
        }
    )
    data["meta"]["amendments"] = amendments
    path.write_text(
        yaml.safe_dump(data, sort_keys=False, default_flow_style=False, width=1000),
        encoding="utf-8",
    )
    return path


def make_run_identity():
    """A RunIdentity for the synthetic bars-era run."""
    from tree_options.runstate import RunIdentity

    return RunIdentity(
        run_id=RUN_ID,
        campaign="m4-barstest",
        protocol_hash="a" * 64,
        code_sha="b" * 40,
        provider="massive-polygon-free/1",
        capture_version="m4b-capture/1",
        universe_manifest_sha256="c" * 64,
        boot_id=BOOT,
        pid=12345,
        pid_start_ticks=99,
        started_epoch=T0,
        args_hash="d" * 64,
    )


def make_matching_run_identity(
    *,
    protocol_hash: str,
    code_sha: str = CENSUS_CODE_SHA,
    universe_manifest_sha256: str = CENSUS_UNIVERSE_MANIFEST_SHA256,
    capture_manifest_sha256: str | None = None,
    campaign: str = "m4-barstest",
) -> RunIdentity:
    """Round-1 review fix (2026-08-23): the bars launcher cross-joins the
    runstate store's identity against the approval record. Round-2 review
    fix (finding 5): execute additionally cross-joins code_sha and
    universe_manifest_sha256 against the VERIFIED census provenance — the
    defaults here match ``census_bytes()``'s provenance exactly, so an
    identity built without overrides is consistent with the scenario census.
    """
    identity = RunIdentity(
        run_id=RUN_ID,
        campaign=campaign,
        protocol_hash=protocol_hash,
        code_sha=code_sha,
        provider="massive-polygon-free/1",
        capture_version="m4b-capture/1",
        universe_manifest_sha256=universe_manifest_sha256,
        capture_manifest_sha256=capture_manifest_sha256,
        boot_id=BOOT,
        pid=12345,
        pid_start_ticks=99,
        started_epoch=T0,
        args_hash="d" * 64,
    )
    return identity
