"""Census CLI + taxonomy: exit-code contract, reconciliation, values.

Hermetic: every capture directory is synthetic, built under `tmp_path` with
`tests/fixtures/massive_structural_sample` builders; nothing ever points at
the live main checkout or its era directory. The git seam is monkeypatched
(`build_coverage_census.GIT_RUNNER`), so no test depends on the state of
this worktree. The session calendar is the repo's real committed fixture,
read-only.
"""

from __future__ import annotations

import hashlib
import json
import sys
from collections.abc import Mapping
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import build_coverage_census as bcc  # type: ignore[import-not-found]  # scripts/  # noqa: E402
import gen_coverage_universe as gen  # type: ignore[import-not-found]  # scripts/  # noqa: E402
from tests.fixtures import massive_structural_sample as fx  # noqa: E402
from tree_options.data.coverage_census import (  # noqa: E402
    CENSUS_SCHEMA_VERSION,
    G3_DERIVATION_CONTRADICTION,
    UNIVERSE_SCHEMA_VERSION,
    CensusFact,
    CensusTaxonomyError,
    CoverageCensus,
    census_content_sha256,
    classify_pair,
    universe_content_sha256,
    validate_value_taxonomy,
    verify_census,
    verify_universe,
)
from tree_options.data.massive_manifest import (  # noqa: E402
    build_massive_capture_manifest,
)

CALENDAR_DIR = REPO_ROOT / "data" / "calendar"
SESSION_FRIDAY_A = "2025-03-07"
SESSION_FRIDAY_B = "2025-03-14"
HOLIDAY_FRIDAY = "2025-04-18"  # Good Friday 2025: a real NYSE non-session
FAR_EXPIRY = "2026-12-18"
HEAD = "f" * 40


# ---- synthetic inputs -----------------------------------------------------------


def _wrapper(underlyings: list[str], fridays: list[str]) -> str:
    """A small era-wrapper script: the `for d in …; do` and --underlyings lines."""
    return (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f"for d in {' '.join(fridays)}; do\n"
        "  python scripts/capture_massive_structural.py"
        f' --underlyings {",".join(underlyings)} --as-of "$d" --bars 0\n'
        "done\n"
    )


def _write_universe(root: Path, underlyings: list[str], fridays: list[str]) -> Path:
    universe = gen.build_universe(_wrapper(underlyings, fridays), source="synthetic-test-wrapper")
    path = root / "universe.json"
    path.write_text(gen.render(universe), encoding="utf-8")
    return path


def _contract_rows(underlying: str, n: int) -> list[str]:
    return [
        fx.contract_result(
            ticker=f"O:{underlying}261218C{1000 * (100 + i):08d}",
            underlying=underlying,
            expiration=FAR_EXPIRY,
            strike=str(100 + i),
            contract_type="call",
        )
        for i in range(n)
    ]


def _build_capture(
    root: Path,
    *,
    underlyings: list[str],
    fridays: list[str],
    rows: int = 2,
    overrides: dict[tuple[str, str], dict[str, object]] | None = None,
    drop_pairs: set[tuple[str, str]] = frozenset(),
    omit_spot: set[tuple[str, str]] = frozenset(),
    content_overrides: dict[tuple[str, str], str] | None = None,
) -> Path:
    """A synthetic sealed capture dir: masters + spot proxy + a valid manifest.

    `content_overrides` replaces a pair's FILE BODY verbatim (the manifest
    still hashes whatever is on disk, so the round-3 semantic-join fixtures
    can pin bytes the manifest's own hashes attest)."""
    capture = root / "capture"
    masters_dir = capture / "masters"
    masters_dir.mkdir(parents=True)
    overrides = overrides or {}
    content_overrides = content_overrides or {}
    entries: list[dict[str, object]] = []
    spot: dict[str, dict[str, str]] = {}
    for underlying in underlyings:
        for friday in fridays:
            pair = (underlying, friday)
            spec: dict[str, object] = {
                "underlying": underlying,
                "as_of": friday,
                "pages": 1,
                "rows": rows,
                "complete": True,
                "truncated": False,
                "error": None,
                "file": f"{underlying}_{friday}.json",
            }
            spec.update(overrides.get(pair, {}))
            if pair in drop_pairs:
                continue
            # `file_rows` decouples the FILE's contract count from the
            # manifest's declared `rows` (the rows-disagreement fixture).
            file_rows = int(spec.get("file_rows", spec["rows"]))
            file_name = spec["file"]
            if isinstance(file_name, str):
                body = content_overrides.get(pair) or fx.contracts_payload(
                    results=_contract_rows(underlying, file_rows), as_of=friday
                )
                (masters_dir / file_name).write_text(body, encoding="utf-8")
            entries.append({k: v for k, v in spec.items() if k != "file_rows"})
            if pair not in omit_spot:
                spot.setdefault(underlying, {})[friday] = "600.00"
    (capture / "spot_proxy.json").write_text(
        json.dumps(spot, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    manifest = build_massive_capture_manifest(
        capture,
        capture_version="m4b-capture/1",
        budget_limit=100,
        requests_charged=1,
        client_stats={"requests": 1},
        masters=entries,
        bars=(),
        spot_proxy=spot,
        notes=(),
    )
    (capture / "capture_manifest.json").write_text(
        manifest.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    return capture


class _FakeGit:
    """The injected command runner for `resolve_code_state`."""

    def __init__(self, head: str = HEAD, status: str = "") -> None:
        self.head = head
        self.status = status

    def __call__(self, *args: str) -> str:
        if args[:1] == ("rev-parse",):
            return self.head + "\n"
        if args[:1] == ("status",):
            return self.status
        raise AssertionError(f"unexpected git call {args!r}")


def _census(
    monkeypatch: pytest.MonkeyPatch,
    capture: Path,
    universe: Path,
    out_root: Path,
    *,
    git: bcc.GitRunner | None = None,
) -> int:
    monkeypatch.setattr(bcc, "GIT_RUNNER", git if git is not None else _FakeGit())
    return bcc.main(
        [
            "--capture-dir",
            str(capture),
            "--universe",
            str(universe),
            "--out-root",
            str(out_root),
            "--calendar-dir",
            str(CALENDAR_DIR),
        ]
    )


def _run_tiny(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, CoverageCensus]:
    """2 underlyings x 2 session fridays, every pair COMPLETE -> exit 0."""
    universe = _write_universe(tmp_path, ["SPY", "QQQ"], [SESSION_FRIDAY_A, SESSION_FRIDAY_B])
    capture = _build_capture(
        tmp_path, underlyings=["SPY", "QQQ"], fridays=[SESSION_FRIDAY_A, SESSION_FRIDAY_B]
    )
    out_root = tmp_path / "census-out"
    assert _census(monkeypatch, capture, universe, out_root) == 0
    digest_dir = next(iter(out_root.iterdir()))
    census = CoverageCensus.model_validate_json((digest_dir / "census.json").read_text())
    return digest_dir, out_root, census


# ---- the whole happy path ---------------------------------------------------------


def test_tiny_complete_universe_exits_zero_and_round_trips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    digest_dir, out_root, census = _run_tiny(tmp_path, monkeypatch)

    body = (digest_dir / "census.json").read_bytes()
    verify_census(census)
    validate_value_taxonomy(census)
    assert (digest_dir / "census.json.sha256").read_text().strip() == hashlib.sha256(
        body
    ).hexdigest()

    cov = census.coverage.observed
    assert cov.COMPLETE == 4 and cov.MISSING == 0 and cov.SPOT_MISSING_SESSION == 0
    assert census.coverage.expected_masters == 4
    assert census.coverage.findings == ()
    assert census.coverage.holiday_fridays == ()
    values = census.values
    assert values.owner_ratified_policy_value == {}
    assert values.observed_census_fact["pair_complete"].v == 4
    assert values.observed_census_fact["pair_complete"].confidence == "EXACT"
    assert values.observed_census_fact["masters_observed"].v == 4
    assert values.observed_census_fact["rows_declared_total"].v == 8
    assert values.observed_census_fact["rows_parsed_total"].v == 8
    # Same ticker on two fridays is ONE contract identity: 2 names x 2 tickers.
    assert values.observed_census_fact["distinct_contracts"].v == 4
    assert values.observed_census_fact["spot_sessions_with_close"].v == 4
    assert values.observed_census_fact["spot_holiday_fridays"].v == 0
    assert values.observed_census_fact["bar_volume_observations"].confidence == "NOT_EVALUABLE"
    assert values.predeclared_derivation_input["expected_masters"].v == 4
    assert values.predeclared_derivation_input["universe_underlyings"].v == 2
    assert values.predeclared_derivation_input["universe_fridays"].v == 2
    assert (
        values.not_yet_decided["flow_min_session_volume"]
        == "AWAITING_OWNER_RULE — " + G3_DERIVATION_CONTRADICTION
    )
    # Every id registered exactly once, in its own class.
    assert len(census.value_registry) == 13 + 3 + 2
    assert census.value_registry["pair_complete"] == "observed_census_fact"
    assert census.value_registry["expected_masters"] == "predeclared_derivation_input"
    assert census.value_registry["flow_min_session_volume"] == "not_yet_decided"

    markdown = (digest_dir / "census.md").read_text()
    assert G3_DERIVATION_CONTRADICTION in markdown
    assert census.provenance.code_sha == HEAD
    assert out_root.name == "census-out"


def test_census_is_byte_identical_across_out_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    universe = _write_universe(tmp_path, ["SPY"], [SESSION_FRIDAY_A, SESSION_FRIDAY_B])
    capture = _build_capture(
        tmp_path, underlyings=["SPY"], fridays=[SESSION_FRIDAY_A, SESSION_FRIDAY_B]
    )
    first, second = tmp_path / "out-one", tmp_path / "out-two"
    assert _census(monkeypatch, capture, universe, first) == 0
    assert _census(monkeypatch, capture, universe, second) == 0
    body_one = next(first.iterdir()).joinpath("census.json").read_bytes()
    body_two = next(second.iterdir()).joinpath("census.json").read_bytes()
    assert body_one == body_two, "identical inputs must emit identical bytes"


# ---- incomplete variants: emitted census + exit 5 ---------------------------------


def test_exit_5_and_census_emitted_when_a_pair_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    universe = _write_universe(tmp_path, ["SPY"], [SESSION_FRIDAY_A, SESSION_FRIDAY_B])
    capture = _build_capture(
        tmp_path,
        underlyings=["SPY"],
        fridays=[SESSION_FRIDAY_A, SESSION_FRIDAY_B],
        drop_pairs={("SPY", SESSION_FRIDAY_B)},
    )
    out_root = tmp_path / "out"
    assert _census(monkeypatch, capture, universe, out_root) == 5
    census = CoverageCensus.model_validate_json(
        next(out_root.iterdir()).joinpath("census.json").read_text()
    )
    assert census.coverage.observed.MISSING == 1
    assert census.coverage.observed.COMPLETE == 1
    assert census.coverage.findings[0].classification == "MISSING"
    assert census.values.observed_census_fact["pair_missing"].v == 1
    assert census.values.observed_census_fact["pair_complete"].confidence == "PARTIAL"


def test_a_truncated_entry_counts_truncated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    universe = _write_universe(tmp_path, ["SPY"], [SESSION_FRIDAY_A])
    capture = _build_capture(
        tmp_path,
        underlyings=["SPY"],
        fridays=[SESSION_FRIDAY_A],
        overrides={
            ("SPY", SESSION_FRIDAY_A): {"complete": False, "truncated": True},
        },
    )
    out_root = tmp_path / "out"
    assert _census(monkeypatch, capture, universe, out_root) == 5
    census = CoverageCensus.model_validate_json(
        next(out_root.iterdir()).joinpath("census.json").read_text()
    )
    assert census.coverage.observed.TRUNCATED == 1
    assert census.values.observed_census_fact["pair_truncated"].v == 1


def test_an_errored_entry_counts_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    universe = _write_universe(tmp_path, ["SPY"], [SESSION_FRIDAY_A])
    capture = _build_capture(
        tmp_path,
        underlyings=["SPY"],
        fridays=[SESSION_FRIDAY_A],
        overrides={
            ("SPY", SESSION_FRIDAY_A): {
                "complete": False,
                "error": "MassiveError: wire cut",
            },
        },
    )
    out_root = tmp_path / "out"
    assert _census(monkeypatch, capture, universe, out_root) == 5
    census = CoverageCensus.model_validate_json(
        next(out_root.iterdir()).joinpath("census.json").read_text()
    )
    assert census.coverage.observed.ERROR == 1
    assert census.values.observed_census_fact["pair_error"].v == 1


def test_a_fileless_entry_counts_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    universe = _write_universe(tmp_path, ["SPY"], [SESSION_FRIDAY_A])
    capture = _build_capture(
        tmp_path,
        underlyings=["SPY"],
        fridays=[SESSION_FRIDAY_A],
        overrides={
            ("SPY", SESSION_FRIDAY_A): {"file": None, "rows": 0, "complete": False},
        },
    )
    out_root = tmp_path / "out"
    assert _census(monkeypatch, capture, universe, out_root) == 5
    census = CoverageCensus.model_validate_json(
        next(out_root.iterdir()).joinpath("census.json").read_text()
    )
    assert census.coverage.observed.MISSING == 1


def test_manifest_rows_disagreement_is_a_finding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every pair is COMPLETE, so the exit contract still says 0 — but the
    disagreement is a named finding and forces PARTIAL confidence."""
    universe = _write_universe(tmp_path, ["SPY"], [SESSION_FRIDAY_A])
    capture = _build_capture(
        tmp_path,
        underlyings=["SPY"],
        fridays=[SESSION_FRIDAY_A],
        overrides={("SPY", SESSION_FRIDAY_A): {"rows": 3, "file_rows": 2}},
    )
    out_root = tmp_path / "out"
    assert _census(monkeypatch, capture, universe, out_root) == 0
    census = CoverageCensus.model_validate_json(
        next(out_root.iterdir()).joinpath("census.json").read_text()
    )
    disagree = [f for f in census.coverage.findings if f.classification == "MASTER_ROWS_DISAGREE"]
    assert len(disagree) == 1 and "rows 3" in disagree[0].detail
    assert census.values.observed_census_fact["rows_declared_total"].v == 3
    assert census.values.observed_census_fact["rows_parsed_total"].v == 2
    assert census.values.observed_census_fact["masters_observed"].confidence == "PARTIAL"


# ---- round-3 (finding 2): the manifest joins its masters SEMANTICALLY -------------
#
# Round-3 review fix (2026-08-23): the manifest's file hashes pin BYTES, not
# MEANING — a correctly-hashed entry declaring complete SPY/<friday> could
# point at a valid pinned envelope for a different pair (or one whose
# envelope says capture_complete=false) and reconciliation alone counted the
# declared pair COMPLETE with exit 0. Parsing now joins each master's
# underlying/as_of against the entry that selected it; any disagreement
# downgrades the pair to an INCOMPLETE class and blocks exit 0.


def test_qqq_envelope_under_a_spy_entry_downgrades_and_blocks_exit_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exact review attack: the entry declares complete SPY/FRIDAY_A,
    the pinned bytes are a valid QQQ/FRIDAY_B capture (manifest hashes
    attest them), and the spot proxy covers the declared pair."""
    universe = _write_universe(tmp_path, ["SPY"], [SESSION_FRIDAY_A])
    capture = _build_capture(
        tmp_path,
        underlyings=["SPY"],
        fridays=[SESSION_FRIDAY_A],
        content_overrides={
            ("SPY", SESSION_FRIDAY_A): fx.contracts_payload(
                results=_contract_rows("QQQ", 2), as_of=SESSION_FRIDAY_B, underlying="QQQ"
            )
        },
    )
    out_root = tmp_path / "out"
    assert _census(monkeypatch, capture, universe, out_root) == 5
    census = CoverageCensus.model_validate_json(
        next(out_root.iterdir()).joinpath("census.json").read_text()
    )
    cov = census.coverage.observed
    assert cov.COMPLETE == 0
    assert cov.MISSING == 1, "the declared pair's master is not on disk"
    mismatch = [
        f for f in census.coverage.findings if f.classification == "MASTER_IDENTITY_MISMATCH"
    ]
    assert len(mismatch) == 1
    assert "QQQ" in mismatch[0].detail and SESSION_FRIDAY_B in mismatch[0].detail
    assert "SPY" in mismatch[0].detail and SESSION_FRIDAY_A in mismatch[0].detail
    assert census.values.observed_census_fact["pair_complete"].confidence == "PARTIAL"


def test_envelope_capture_complete_false_never_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The entry DECLARES complete, but the pinned envelope's own last page
    carries next_url (capture stopped at the page cap). The parsed ground
    truth downgrades the pair to TRUNCATED and exit 0 is impossible."""
    universe = _write_universe(tmp_path, ["SPY"], [SESSION_FRIDAY_A])
    capture = _build_capture(
        tmp_path,
        underlyings=["SPY"],
        fridays=[SESSION_FRIDAY_A],
        content_overrides={
            ("SPY", SESSION_FRIDAY_A): fx.contracts_payload(
                results=_contract_rows("SPY", 2),
                as_of=SESSION_FRIDAY_A,
                next_url="https://api.polygon.io/v3/reference/options/contracts?cursor=MORE",
            )
        },
    )
    out_root = tmp_path / "out"
    assert _census(monkeypatch, capture, universe, out_root) == 5
    census = CoverageCensus.model_validate_json(
        next(out_root.iterdir()).joinpath("census.json").read_text()
    )
    cov = census.coverage.observed
    assert cov.COMPLETE == 0
    assert cov.TRUNCATED == 1
    truncated = [
        f for f in census.coverage.findings if f.classification == "MASTER_CAPTURE_TRUNCATED"
    ]
    assert len(truncated) == 1 and "capture_complete=false" in truncated[0].detail
    assert census.values.observed_census_fact["pair_truncated"].v == 1
    assert census.values.observed_census_fact["pair_truncated"].confidence == "PARTIAL"


# ---- round-4 (finding 1): a holiday pair with a semantic disagreement ---------------
#
# Round-4 review fix (2026-08-23): the round-3 demotion fired only when the
# pair's current class was COMPLETE, but SPOT_MISSING_HOLIDAY is deliberately
# OUTSIDE INCOMPLETE_CLASSES — so a holiday pair whose entry correctly hashes
# a foreign envelope recorded the finding while the census still said whole
# coverage and exited 0. The declared pair's master is NOT on disk; that is
# missing data, not a holiday.


def test_qqq_envelope_under_a_holiday_spy_entry_downgrades_and_blocks_exit_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reviewer's exact scenario: SPY on Good Friday 2025 is declared
    complete with no spot close (the exchange was closed — SPOT_MISSING_HOLIDAY),
    and the entry's correctly-hashed bytes are a COMPLETE QQQ envelope for the
    same friday with a matching row count. Pre-fix: exit 0, complete=true.
    Post-fix: the pair demotes to MISSING and exit 0 is impossible."""
    universe = _write_universe(tmp_path, ["SPY"], [HOLIDAY_FRIDAY])
    capture = _build_capture(
        tmp_path,
        underlyings=["SPY"],
        fridays=[HOLIDAY_FRIDAY],
        omit_spot={("SPY", HOLIDAY_FRIDAY)},
        content_overrides={
            ("SPY", HOLIDAY_FRIDAY): fx.contracts_payload(
                results=_contract_rows("QQQ", 2), as_of=HOLIDAY_FRIDAY, underlying="QQQ"
            )
        },
    )
    out_root = tmp_path / "out"
    assert _census(monkeypatch, capture, universe, out_root) == 5
    census = CoverageCensus.model_validate_json(
        next(out_root.iterdir()).joinpath("census.json").read_text()
    )
    cov = census.coverage.observed
    assert cov.SPOT_MISSING_HOLIDAY == 0, "the holiday class lost the pair to MISSING"
    assert cov.MISSING == 1, "the declared pair's master is not on disk"
    mismatch = [
        f for f in census.coverage.findings if f.classification == "MASTER_IDENTITY_MISMATCH"
    ]
    assert len(mismatch) == 1
    assert census.values.observed_census_fact["pair_complete"].confidence == "PARTIAL"


def test_a_clean_holiday_pair_still_exits_zero_after_the_round4_join(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The demotion must not eat the LEGITIMATE holiday behavior: the same
    holiday capture with the CORRECT envelope (no semantic finding) still
    classifies SPOT_MISSING_HOLIDAY and still permits exit 0."""
    universe = _write_universe(tmp_path, ["SPY"], [HOLIDAY_FRIDAY])
    capture = _build_capture(
        tmp_path,
        underlyings=["SPY"],
        fridays=[HOLIDAY_FRIDAY],
        omit_spot={("SPY", HOLIDAY_FRIDAY)},
    )
    assert _census(monkeypatch, capture, universe, tmp_path / "out") == 0


# ---- holiday vs session spot gaps -------------------------------------------------


def test_holiday_friday_gap_is_not_incomplete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Good Friday 2025: no close BY DEFINITION — SPOT_MISSING_HOLIDAY, the
    census reconciles EXACTLY (only INCOMPLETE_CLASSES force PARTIAL), and a
    holiday-only gap is WHOLE coverage: exit 0. Requiring every pair COMPLETE
    would make exit 0 unreachable for any grid containing a Good Friday."""
    universe = _write_universe(tmp_path, ["SPY"], [HOLIDAY_FRIDAY])
    capture = _build_capture(
        tmp_path, underlyings=["SPY"], fridays=[HOLIDAY_FRIDAY], omit_spot={("SPY", HOLIDAY_FRIDAY)}
    )
    out_root = tmp_path / "out"
    assert _census(monkeypatch, capture, universe, out_root) == 0
    census = CoverageCensus.model_validate_json(
        next(out_root.iterdir()).joinpath("census.json").read_text()
    )
    assert census.coverage.observed.SPOT_MISSING_HOLIDAY == 1
    assert census.coverage.observed.SPOT_MISSING_SESSION == 0
    assert census.coverage.holiday_fridays == (HOLIDAY_FRIDAY,)
    assert census.coverage.session_spot_gaps == ()
    assert census.values.observed_census_fact["spot_holiday_fridays"].v == 1
    assert census.values.observed_census_fact["spot_holiday_fridays"].confidence == "EXACT"
    assert census.values.observed_census_fact["pair_spot_missing_holiday"].confidence == "EXACT"


def test_session_friday_missing_close_is_incomplete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    universe = _write_universe(tmp_path, ["SPY"], [SESSION_FRIDAY_B])
    capture = _build_capture(
        tmp_path,
        underlyings=["SPY"],
        fridays=[SESSION_FRIDAY_B],
        omit_spot={("SPY", SESSION_FRIDAY_B)},
    )
    out_root = tmp_path / "out"
    assert _census(monkeypatch, capture, universe, out_root) == 5
    census = CoverageCensus.model_validate_json(
        next(out_root.iterdir()).joinpath("census.json").read_text()
    )
    assert census.coverage.observed.SPOT_MISSING_SESSION == 1
    assert census.coverage.observed.SPOT_MISSING_HOLIDAY == 0
    assert len(census.coverage.session_spot_gaps) == 1
    assert census.values.observed_census_fact["pair_spot_missing_session"].v == 1
    assert census.values.observed_census_fact["pair_spot_missing_session"].confidence == "PARTIAL"


# ---- round-6 (finding 5): re-hash every capture file the census derives from ---------
#
# Round-6 review fix (2026-08-24): the same verify-then-re-read race class
# 7211e0a fixed for BARS regeneration, on the CENSUS producer. The pinned
# spot proxy is verified with the manifest (step 2) and then load_spot
# RE-READS the path (step 4); swapping the proxy in that window with
# byte-different JSON that HAS the session close turned the sealed
# SPOT_MISSING_SESSION/exit-5 state into COMPLETE/exit 0. Masters are re-read
# by observe_masters the same way (step 5) — same fix, same rule: read once,
# hash against the manifest pin at the point of consumption.


def test_swapped_spot_proxy_after_verify_refuses_exit_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The reviewer's scenario: the SEALED bytes have no session close (exit 5
    baseline, asserted first); the proxy is swapped between manifest
    verification and load_spot with byte-different JSON that HAS the close.
    Pre-fix the census derives from the swapped bytes and exits 0 COMPLETE;
    post-fix exit 2 naming both hashes, and NO census is emitted."""
    universe = _write_universe(tmp_path, ["SPY"], [SESSION_FRIDAY_A])
    capture = _build_capture(
        tmp_path,
        underlyings=["SPY"],
        fridays=[SESSION_FRIDAY_A],
        omit_spot={("SPY", SESSION_FRIDAY_A)},
    )
    # the honest sealed state: no close -> SPOT_MISSING_SESSION -> exit 5
    assert _census(monkeypatch, capture, universe, tmp_path / "out-honest") == 5

    real_load_spot = bcc.load_spot
    swapped = {"done": False}

    def load_spot_swapping_proxy(capture_dir: Path, **kwargs: object) -> object:
        if not swapped["done"]:
            swapped["done"] = True  # the window: manifest verified, spot unread
            (capture_dir / "spot_proxy.json").write_text(
                json.dumps({"SPY": {SESSION_FRIDAY_A: "600.00"}}, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        return real_load_spot(capture_dir, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(bcc, "load_spot", load_spot_swapping_proxy)
    out_root = tmp_path / "out-attacked"
    assert _census(monkeypatch, capture, universe, out_root) == 2, (
        "a swapped spot proxy must refuse (the sealed bytes said exit 5), "
        "never upgrade the census to COMPLETE/exit 0"
    )
    err = capsys.readouterr().err
    assert "SPOT PROXY REFUSED" in err
    assert "drifted" in err, "the refusal names the drift against the manifest pin"
    assert not out_root.exists() or list(out_root.iterdir()) == [], (
        "no census was emitted from unsealed bytes"
    )


def test_swapped_master_after_verify_refuses_exit_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The same gap on the MASTERS leg: observe_masters re-reads every master
    the manifest already verified. A master swapped between verification and
    the re-parse (byte-different envelope, one extra contract row — still
    parseable, still a finding-free COMPLETE pair) used to feed the census
    rows the seal never attested. Post-fix: exit 2, no census."""
    universe = _write_universe(tmp_path, ["SPY"], [SESSION_FRIDAY_A])
    capture = _build_capture(tmp_path, underlyings=["SPY"], fridays=[SESSION_FRIDAY_A])
    real_observe = bcc.observe_masters
    swapped = {"done": False}

    def observe_swapping_master(manifest: object, capture_dir: Path, **kwargs: object) -> object:
        if not swapped["done"]:
            swapped["done"] = True  # the window: manifest verified, masters unread
            (capture_dir / "masters" / f"SPY_{SESSION_FRIDAY_A}.json").write_text(
                fx.contracts_payload(results=_contract_rows("SPY", 3), as_of=SESSION_FRIDAY_A),
                encoding="utf-8",
            )
        return real_observe(manifest, capture_dir, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(bcc, "observe_masters", observe_swapping_master)
    out_root = tmp_path / "out"
    assert _census(monkeypatch, capture, universe, out_root) == 2, (
        "a swapped master must refuse — the census may derive only from the "
        "bytes the sealed manifest attests"
    )
    err = capsys.readouterr().err
    assert "drifted" in err, "the refusal names the drift against the manifest pin"
    assert not out_root.exists() or list(out_root.iterdir()) == [], (
        "no census was emitted from unsealed bytes"
    )


# ---- round-5 (finding 5): the emitted census.md states the REAL exit rule ----------


def test_census_md_exit_contract_names_the_real_holiday_rule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round-5 review fix (2026-08-24, finding 5): census.md used to claim
    exit 0 requires "every universe pair is COMPLETE" — stale since the
    holiday rule: a clean SPOT_MISSING_HOLIDAY pair legitimately exits 0, so
    that claim is wrong for exactly the census this test builds. The emitted
    evidence must name the REAL rule (zero INCOMPLETE_CLASSES pairs AND
    masters observed == expected_masters) and never the obsolete one."""
    universe = _write_universe(tmp_path, ["SPY"], [HOLIDAY_FRIDAY])
    capture = _build_capture(
        tmp_path,
        underlyings=["SPY"],
        fridays=[HOLIDAY_FRIDAY],
        omit_spot={("SPY", HOLIDAY_FRIDAY)},
    )
    out_root = tmp_path / "out"
    assert _census(monkeypatch, capture, universe, out_root) == 0
    markdown = next(out_root.iterdir()).joinpath("census.md").read_text()
    assert "every universe pair is COMPLETE" not in markdown, (
        "the obsolete pre-holiday-rule claim is gone"
    )
    assert "0 iff zero pairs sit in INCOMPLETE_CLASSES" in markdown, (
        "the real rule names the INCOMPLETE_CLASSES criterion"
    )
    assert "masters observed == expected_masters" in markdown
    assert "SPOT_MISSING_HOLIDAY) are EXPECTED" in markdown, (
        "the real rule says holiday Fridays without a close are expected"
    )


# ---- refusal exits ------------------------------------------------------------------


def test_manifest_verification_failure_exits_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A manifest that fails verify refuses with 2 before any fact is derived.

    Both reconciliation directions, one phase each: a listed file DELETED
    from disk, and an UNLISTED *.json appearing on disk (unprovenance).
    The deletion is also caught, independently, by the round-8 pinned-read
    re-hash (_read_pinned_bytes refuses the unreadable pinned master) — a
    layered defense worth pinning in its own right. But the unlisted file
    is never a derivation input: the census walks manifest entries and
    reads only the pinned spot proxy, so NOTHING but the manifest
    verification's disk-vs-listing reconciliation refuses it. That second
    phase is what keeps M199 (census-manifest-verify-skipped) killed: with
    verify gutted, the deleted-master phase still refuses (exit 2 via the
    re-hash) while the unlisted-file phase derives a whole census and
    exits 0."""
    universe = _write_universe(tmp_path, ["SPY"], [SESSION_FRIDAY_A])
    deleted = _build_capture(tmp_path / "a", underlyings=["SPY"], fridays=[SESSION_FRIDAY_A])
    (deleted / "masters" / f"SPY_{SESSION_FRIDAY_A}.json").unlink()
    assert _census(monkeypatch, deleted, universe, tmp_path / "out-a") == 2

    capture = _build_capture(tmp_path / "b", underlyings=["SPY"], fridays=[SESSION_FRIDAY_A])
    (capture / "masters" / "UNLISTED_extra.json").write_text("{}")
    assert _census(monkeypatch, capture, universe, tmp_path / "out-b") == 2


def test_absent_manifest_exits_2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    universe = _write_universe(tmp_path, ["SPY"], [SESSION_FRIDAY_A])
    empty = tmp_path / "no-manifest"
    empty.mkdir()
    assert _census(monkeypatch, empty, universe, tmp_path / "out") == 2


def test_tampered_universe_exits_3(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    universe_path = _write_universe(tmp_path, ["SPY"], [SESSION_FRIDAY_A])
    payload = json.loads(universe_path.read_text())
    payload["content_sha256"] = "0" * 64
    universe_path.write_text(json.dumps(payload))
    capture = _build_capture(tmp_path, underlyings=["SPY"], fridays=[SESSION_FRIDAY_A])
    assert _census(monkeypatch, capture, universe_path, tmp_path / "out") == 3


def test_missing_universe_file_exits_3(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    capture = _build_capture(tmp_path, underlyings=["SPY"], fridays=[SESSION_FRIDAY_A])
    assert _census(monkeypatch, capture, tmp_path / "absent.json", tmp_path / "out") == 3


def test_existing_output_dir_exits_4(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    universe = _write_universe(tmp_path, ["SPY"], [SESSION_FRIDAY_A])
    capture = _build_capture(tmp_path, underlyings=["SPY"], fridays=[SESSION_FRIDAY_A])
    out_root = tmp_path / "out"
    assert _census(monkeypatch, capture, universe, out_root) == 0
    assert _census(monkeypatch, capture, universe, out_root) == 4, "never overwrite"


# ---- the factored dirty-tree function -----------------------------------------------


def test_resolve_code_state_refuses_a_dirty_tracked_tree() -> None:
    dirty = _FakeGit(status=" M src/tree_options/time/calendar.py\n")
    with pytest.raises(bcc.ReproducibilityError, match="dirty"):
        bcc.resolve_code_state(dirty)


def test_resolve_code_state_ignores_untracked_and_output_roots() -> None:
    status = "\n".join(
        [
            "?? artifacts/census/abc012345678/census.json",
            "?? notes.txt",
            " M artifacts/census/deadbeef/census.json",
            "M  dist/lib/vendor.py",
            "R  dist/old.py -> dist/new.py",
        ]
    )
    assert bcc.resolve_code_state(_FakeGit(status=status)) == HEAD


def test_resolve_code_state_refuses_a_mixed_dirty_line() -> None:
    with pytest.raises(bcc.ReproducibilityError, match="src/"):
        bcc.resolve_code_state(_FakeGit(status=" M src/tree_options/x.py\n?? artifacts/census"))


def test_resolve_code_state_refuses_unusable_git(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def dead(*args: str) -> str:
        raise FileNotFoundError("git")

    with pytest.raises(bcc.ReproducibilityError, match="git unusable"):
        bcc.resolve_code_state(dead)


# ---- classify_pair: the full matrix ---------------------------------------------------


def test_classify_pair_matrix_covers_every_class() -> None:
    base = dict(
        underlying="SPY",
        as_of=SESSION_FRIDAY_B,
        entry_complete=True,
        entry_truncated=False,
        entry_error="",
        has_file=True,
        spot_present=True,
        is_session=True,
    )
    assert classify_pair(**base) == "COMPLETE"
    assert classify_pair(**{**base, "entry_complete": None}) == "MISSING"
    assert classify_pair(**{**base, "has_file": False}) == "MISSING"
    assert classify_pair(**{**base, "entry_error": "boom"}) == "ERROR"
    assert classify_pair(**{**base, "entry_truncated": True}) == "TRUNCATED"
    assert classify_pair(**{**base, "entry_complete": False}) == "TRUNCATED"
    assert classify_pair(**{**base, "spot_present": False}) == "SPOT_MISSING_SESSION"
    assert (
        classify_pair(**{**base, "spot_present": False, "is_session": False})
        == "SPOT_MISSING_HOLIDAY"
    )


# ---- the taxonomy guards ---------------------------------------------------------------


def test_verify_universe_refuses_a_rehashed_wrong_expected_masters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The product check must fire even when the tamperer RECOMPUTES the hash."""
    universe = gen.build_universe(
        _wrapper(["SPY"], [SESSION_FRIDAY_A]), source="synthetic-test-wrapper"
    )
    tampered = universe.model_copy(update={"expected_masters": universe.expected_masters + 1})
    rehashed = tampered.model_copy(update={"content_sha256": universe_content_sha256(tampered)})
    with pytest.raises(CensusTaxonomyError, match="expected_masters"):
        verify_universe(rehashed)

    # And through the CLI: the same trick still refuses with exit 3.
    bad = tmp_path / "bad-universe.json"
    bad.write_text(gen.render(rehashed), encoding="utf-8")
    capture = _build_capture(tmp_path, underlyings=["SPY"], fridays=[SESSION_FRIDAY_A])
    assert _census(monkeypatch, capture, bad, tmp_path / "out") == 3


def test_registry_disagreement_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _digest_dir, _out_root, census = _run_tiny(tmp_path, monkeypatch)
    moved = census.model_copy(
        update={"value_registry": {**census.value_registry, "pair_complete": "not_yet_decided"}}
    )
    with pytest.raises(CensusTaxonomyError, match="registry says"):
        validate_value_taxonomy(moved)


def test_same_id_in_two_sections_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _digest_dir, _out_root, census = _run_tiny(tmp_path, monkeypatch)
    values = census.values.model_copy(
        update={"not_yet_decided": {**census.values.not_yet_decided, "pair_complete": "dup"}}
    )
    duplicated = census.model_copy(update={"values": values})
    with pytest.raises(CensusTaxonomyError, match="more than one value class"):
        validate_value_taxonomy(duplicated)


def test_verify_census_refuses_any_owner_ratified_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fresh id, correctly registered and correctly re-hashed: the refusal
    can only come from the owner-ratified-must-be-EMPTY rule itself."""
    _digest_dir, _out_root, census = _run_tiny(tmp_path, monkeypatch)
    values = census.values.model_copy(
        update={"owner_ratified_policy_value": {"some_policy_value": "ratified"}}
    )
    ratified = census.model_copy(
        update={
            "values": values,
            "value_registry": {
                **census.value_registry,
                "some_policy_value": "owner_ratified_policy_value",
            },
        }
    )
    rehashed = ratified.model_copy(update={"content_sha256": census_content_sha256(ratified)})
    with pytest.raises(CensusTaxonomyError, match="owner_ratified_policy_value must be EMPTY"):
        verify_census(rehashed)


# ---- round-3 (finding 1): CensusFact.v is a strict int or a str ------------------
#
# Round-3 review fix (2026-08-23): CensusFact sat under a NON-strict config,
# so pydantic lax mode coerced `true` -> int 1 and `1.0` -> int 1 at parse
# time — the downstream `type(observed.v) is int` derivation gates in the
# amendment builder then saw an already-coerced int and could not detect the
# boolean/float origin. The refusal belongs at the CensusFact parse boundary;
# textual observations (`v: "text"`) must keep parsing.


def test_census_fact_true_refused_at_parse() -> None:
    with pytest.raises(ValueError, match="v"):
        CensusFact.model_validate_json('{"v": true, "support": {}, "confidence": "EXACT"}')


def test_census_fact_float_refused_at_parse() -> None:
    with pytest.raises(ValueError, match="v"):
        CensusFact.model_validate_json('{"v": 1.0, "support": {}, "confidence": "EXACT"}')


def test_census_fact_bool_refused_in_python_mode() -> None:
    with pytest.raises(ValueError, match="v"):
        CensusFact(v=True, confidence="EXACT")  # type: ignore[arg-type]


def test_census_fact_int_accepted() -> None:
    fact = CensusFact.model_validate_json('{"v": 5, "support": {}, "confidence": "EXACT"}')
    assert fact.v == 5
    assert type(fact.v) is int


def test_census_fact_text_observation_accepted() -> None:
    fact = CensusFact.model_validate_json('{"v": "text", "support": {}, "confidence": "EXACT"}')
    assert fact.v == "text"


# ---- round-3 (finding 5): schema/report versions are pinned, not assumed ----------
#
# Round-3 review fix (2026-08-23): the verifiers checked hashes and taxonomy
# but never the version tokens, so a foreign-era artifact (rehashed,
# otherwise valid) passed every check. A content hash binds CONTENT, not
# COMPATIBILITY — the version constants must be required explicitly.


def test_verify_census_refuses_a_foreign_schema_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """schema_version 'foreign/999', correctly rehashed: the refusal names
    both the found and the expected version."""
    _digest_dir, _out_root, census = _run_tiny(tmp_path, monkeypatch)
    foreign = census.model_copy(update={"schema_version": "foreign/999"})
    rehashed = foreign.model_copy(update={"content_sha256": census_content_sha256(foreign)})
    with pytest.raises(CensusTaxonomyError, match="schema_version") as exc_info:
        verify_census(rehashed)
    assert "foreign/999" in str(exc_info.value)
    assert CENSUS_SCHEMA_VERSION in str(exc_info.value)


def test_verify_census_refuses_a_wrong_report_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _digest_dir, _out_root, census = _run_tiny(tmp_path, monkeypatch)
    provenance = census.provenance.model_copy(update={"report_version": "m4-coverage-census/0"})
    wrong = census.model_copy(update={"provenance": provenance})
    rehashed = wrong.model_copy(update={"content_sha256": census_content_sha256(wrong)})
    with pytest.raises(CensusTaxonomyError, match="report_version") as exc_info:
        verify_census(rehashed)
    assert "m4-coverage-census/0" in str(exc_info.value)
    assert CENSUS_SCHEMA_VERSION in str(exc_info.value)


def test_verify_census_accepts_the_current_versions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _digest_dir, _out_root, census = _run_tiny(tmp_path, monkeypatch)
    assert census.schema_version == CENSUS_SCHEMA_VERSION
    assert census.provenance.report_version == CENSUS_SCHEMA_VERSION
    verify_census(census)  # current tokens pass every check


def test_census_without_report_version_refused_at_parse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round-4 review fix (2026-08-23, finding 3): the reviewer's exact probe
    — delete provenance.report_version from a valid current census, leave
    content_sha256 unchanged. The field carried a DEFAULT equal to the
    current token, so the parse re-inserted it, canonical hashing reproduced
    the original content hash, and verify_census accepted an artifact that
    never declared its report version. The token is now REQUIRED: an absent
    one is a parse refusal. A fully-explicit current census still verifies."""
    _digest_dir, _out_root, census = _run_tiny(tmp_path, monkeypatch)
    verify_census(census)  # a fully-explicit current census still verifies
    doc = json.loads(census.model_dump_json())
    del doc["provenance"]["report_version"]
    stripped = json.dumps(doc).encode("utf-8")
    with pytest.raises(ValueError, match="report_version"):
        CoverageCensus.model_validate_json(stripped)


def test_verify_universe_refuses_a_foreign_schema_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A foreign-schema universe manifest refuses both directly and through
    the CLI (exit 3), even when the tamperer RECOMPUTES the content hash."""
    universe = gen.build_universe(
        _wrapper(["SPY"], [SESSION_FRIDAY_A]), source="synthetic-test-wrapper"
    )
    assert universe.schema_version == UNIVERSE_SCHEMA_VERSION
    foreign = universe.model_copy(update={"schema_version": "foreign/999"})
    rehashed = foreign.model_copy(update={"content_sha256": universe_content_sha256(foreign)})
    with pytest.raises(CensusTaxonomyError, match="schema_version") as exc_info:
        verify_universe(rehashed)
    assert "foreign/999" in str(exc_info.value)
    assert UNIVERSE_SCHEMA_VERSION in str(exc_info.value)

    # And through the CLI: the same trick still refuses with exit 3.
    bad = tmp_path / "foreign-universe.json"
    bad.write_text(gen.render(rehashed), encoding="utf-8")
    capture = _build_capture(tmp_path, underlyings=["SPY"], fridays=[SESSION_FRIDAY_A])
    assert _census(monkeypatch, capture, bad, tmp_path / "out") == 3


# ---- round-7 (finding 3): the capture manifest is consumed bytes-once -----------------
#
# Round-7 review fix (2026-08-24): capture_manifest.json was loaded+verified
# once (load_sealed_manifest) and then RE-READ for the provenance hash
# (step 3's manifest_bytes_sha). Swapping the manifest file in that window
# (B = `{}` bytes) left the derivation consuming A-pinned bytes while the
# emitted provenance named B. The manifest is now read ONCE (threaded into
# the loader as raw bytes), the provenance uses the sha of those VERIFIED
# bytes, and a guard read at provenance time refuses — exit 2, the
# manifest-tamper family — when the file no longer holds them.


def test_swapped_capture_manifest_between_verify_and_provenance_refuses_exit_2(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The racing call is the manifest's SECOND read (the provenance read
    pre-fix, the guard read post-fix): the wrapper swaps the file to `{}`
    bytes there and returns the real read. Pre-fix the census derives from
    the verified A bytes, stamps provenance with B's hash, and exits 0 with
    a whole census; post-fix it refuses exit 2 and emits nothing."""
    universe = _write_universe(tmp_path, ["SPY"], [SESSION_FRIDAY_A])
    capture = _build_capture(tmp_path, underlyings=["SPY"], fridays=[SESSION_FRIDAY_A])
    manifest_path = capture / "capture_manifest.json"
    swapped = b"{}\n"
    real_read_bytes = Path.read_bytes
    reads = {"n": 0}

    def read_bytes_swapping_manifest(self: Path) -> bytes:
        if self == manifest_path:
            reads["n"] += 1
            if reads["n"] == 2:  # the window: verified once, provenance unread
                self.write_bytes(swapped)
            return real_read_bytes(self)
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", read_bytes_swapping_manifest)
    out_root = tmp_path / "out"
    assert _census(monkeypatch, capture, universe, out_root) == 2, (
        "a capture manifest swapped between verification and provenance must "
        "refuse — exit 2, the manifest-tamper family (pinned: the same exit "
        "the round-6 spot/master drift refusals use) — never emit a census "
        "whose provenance names bytes the derivation never consumed"
    )
    err = capsys.readouterr().err
    assert "MANIFEST REFUSED" in err and "drifted" in err, (
        "the refusal names the drift between the verified and the read bytes"
    )
    assert not out_root.exists() or list(out_root.iterdir()) == [], (
        "no census was emitted from the swap window"
    )


# ---- round-7 (finding 4): every PINNED master is census evidence — no orphans ---------
#
# Round-7 review fix (2026-08-24): the manifest's files[] pins EVERY on-disk
# masters/*.json (build_massive_capture_manifest's directory scan), but
# masters[] (the metadata entries) may reference only a subset. A stale
# master pinned in files[] yet referenced by NO entry was never read by the
# entry-driven loop, and masters_observed == expected still exited 0 — the
# census silently ignored a sealed master. (The strengthened M199 test
# covers UNLISTED files; this is LISTED-but-unreferenced — the completeness
# mirror of 2c3db7b.) After the masters loop, the pinned master-kind files
# minus the entry-referenced set must be EMPTY or the census refuses naming
# every unreferenced file.


def test_pinned_but_unreferenced_master_refuses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Fixture: a normal SPY capture, then a stale QQQ master file on disk
    and the manifest REBUILT via build_massive_capture_manifest with the same
    SPY-only entries — its directory scan pins both files, so the manifest
    stays self-consistent and verifies. Pre-fix: exit 0 with a whole census
    (the sealed QQQ master is silently ignored); post-fix: exit 2 naming the
    QQQ file, nothing emitted."""
    universe = _write_universe(tmp_path, ["SPY"], [SESSION_FRIDAY_A])
    capture = _build_capture(tmp_path, underlyings=["SPY"], fridays=[SESSION_FRIDAY_A])
    (capture / "masters" / f"QQQ_{SESSION_FRIDAY_A}.json").write_text(
        fx.contracts_payload(results=_contract_rows("QQQ", 2), as_of=SESSION_FRIDAY_A),
        encoding="utf-8",
    )
    old = json.loads((capture / "capture_manifest.json").read_text())
    rebuilt = build_massive_capture_manifest(
        capture,
        capture_version="m4b-capture/1",
        budget_limit=old["budget_limit"],
        requests_charged=old["requests_charged"],
        client_stats=old["client_stats"],
        masters=old["masters"],  # SPY-only entries: QQQ is pinned, never referenced
        bars=old["bars"],
        spot_proxy=old["spot_proxy"],
        notes=old["notes"],
    )
    (capture / "capture_manifest.json").write_text(
        rebuilt.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    out_root = tmp_path / "out"
    assert _census(monkeypatch, capture, universe, out_root) == 2, (
        "a sealed master pinned by files[] but referenced by no masters[] entry "
        "is silently ignored evidence — the census must refuse naming it"
    )
    err = capsys.readouterr().err
    assert f"masters/QQQ_{SESSION_FRIDAY_A}.json" in err, (
        "the refusal names the pinned-but-unreferenced master file"
    )
    assert not out_root.exists() or list(out_root.iterdir()) == [], "no census emitted"


# ---- round-8 (finding 2): the manifest drift guard must sit at the FINAL EFFECT --------
#
# Round-8 review fix (2026-08-24): the round-7 provenance guard re-read
# capture_manifest.json at census time (~953) — BEFORE the spot/master
# derivation (~975) and BEFORE artifact emission (~1030). A manifest swapped
# AFTER that guard produced an exit-0 census bound to the verified A bytes
# while the capture directory held B. The load-bearing re-check now sits at
# the FINAL EFFECT: immediately before anything is written under out_dir
# (and before out_dir.mkdir, so a refusal emits nothing) the manifest NAME
# must lstat as a regular file and its bytes must still hash to the threaded
# verified sha — else MassiveManifestError, exit 2, no artifact.


def test_swapped_capture_manifest_after_the_early_guard_refuses_exit_2(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The racing call is the derivation entry (load_spot): the wrapper
    swaps the manifest to `{}` bytes AFTER the round-7 provenance guard has
    passed and before the emission. Pre-fix the census exits 0 with an
    artifact whose provenance names the verified A bytes while the capture
    dir holds B; post-fix it refuses exit 2 at the emission gate and emits
    nothing."""
    universe = _write_universe(tmp_path, ["SPY"], [SESSION_FRIDAY_A])
    capture = _build_capture(tmp_path, underlyings=["SPY"], fridays=[SESSION_FRIDAY_A])
    manifest_path = capture / "capture_manifest.json"
    swapped = b"{}\n"
    real_load_spot = bcc.load_spot

    def load_spot_swapping_manifest(capture_dir: Path, *, pinned: Mapping[str, str] | None = None):
        # the window: the early (round-7) guard has passed, the emission has
        # not run — swap the file the census verified
        manifest_path.write_bytes(swapped)
        return real_load_spot(capture_dir, pinned=pinned)

    monkeypatch.setattr(bcc, "load_spot", load_spot_swapping_manifest)
    out_root = tmp_path / "out"
    assert _census(monkeypatch, capture, universe, out_root) == 2, (
        "a capture manifest swapped AFTER the provenance guard must refuse at "
        "the emission — exit 2, the manifest-tamper family — never emit a "
        "census bound to bytes the capture directory no longer holds"
    )
    err = capsys.readouterr().err
    assert "MANIFEST REFUSED" in err and "drifted" in err, (
        "the refusal names the drift between the verified sha and the bytes read at emission"
    )
    assert not out_root.exists() or list(out_root.iterdir()) == [], (
        "no census was emitted from the swap window"
    )


# ---- round-8 (finding 5): the emitter writes through CUSTODY, never the bare name -------
#
# Round-8 review fix (2026-08-24): after out_dir.mkdir (~1036), three plain
# write_text calls wrote census.json / census.md / census.json.sha256
# THROUGH whatever the names pointed at. A `census.json ->
# research_protocol.yaml` link planted between the mkdir and the write
# truncated the PROTECTED protocol file with census JSON and the command
# still exited 0. Each output is now emitted through a custody write
# (refuse a non-regular final name; unpredictable O_EXCL|O_NOFOLLOW temp
# under the out-dir fd; fsync; nlink==1; os.replace; identity + full
# readback), and a refusal is CensusEmitRefused -> exit 4 — the
# reproducibility/EMISSION refusal family, the same family the
# output-dir-exists refusal already uses (pinned below).


def test_output_name_symlinked_at_a_protected_file_refuses_exit_4(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Fixture: a DECOY file named like the protected protocol inside the
    scratch tmp tree (the repo's real research_protocol.yaml is never
    touched), and `census.json -> <decoy>` planted right after the digest
    dir's mkdir. Pre-fix the write_text follows the link, REPLACES the
    decoy's content with census JSON, and the command exits 0; post-fix the
    custody emitter refuses (CensusEmitRefused, exit 4) and the decoy is
    byte-identical."""
    universe = _write_universe(tmp_path, ["SPY"], [SESSION_FRIDAY_A])
    capture = _build_capture(tmp_path, underlyings=["SPY"], fridays=[SESSION_FRIDAY_A])
    out_root = tmp_path / "out"
    decoy = tmp_path / "research_protocol.yaml"
    decoy.write_text("# PROTECTED decoy — must never be truncated\n", encoding="utf-8")
    before = decoy.read_bytes()
    real_mkdir = Path.mkdir
    planted: list[bool] = []

    def mkdir_then_plant(self: Path, *args: object, **kwargs: object) -> None:
        real_mkdir(self, *args, **kwargs)  # type: ignore[arg-type]
        # one plant only: pathlib's parents=True recursion re-enters mkdir
        # for the same digest dir (the internal no-parents retry)
        if self.parent == out_root and not planted:  # the digest dir: emission next
            planted.append(True)
            (self / "census.json").symlink_to(decoy)  # the planted link

    monkeypatch.setattr(Path, "mkdir", mkdir_then_plant)
    assert _census(monkeypatch, capture, universe, out_root) == 4, (
        "an output name that is a symlink at a protected file is an EMISSION "
        "refusal — exit 4, the reproducibility/emission family (pinned: the "
        "same exit the output-dir-exists refusal uses) — never a write "
        "through the link and never exit 0"
    )
    err = capsys.readouterr().err
    assert "EMISSION REFUSED" in err, "the refusal family is named on stderr"
    assert "census.json" in err, "the refusal names the output"
    assert decoy.read_bytes() == before, (
        "the protected-file stand-in was never written through the link"
    )
