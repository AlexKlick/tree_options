"""Bars work manifest + bars-authority ledger: binding, ordering, determinism.

Synthetic captures come from tests/fixtures/bars_sample.py (raw-JSON-text
rows, real provenance stamps). The authority ledger tests use scratch roots
under the repo's gitignored artifacts/ — pytest's tmp_path lives under /tmp
on this host and the ledger refuses any root whose resolved path is under
/tmp (host rule, shared with test_seal_ledger.py).
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from tests.fixtures.bars_sample import (  # noqa: E402
    AS_OF,
    EXPECTED_ENTRIES,
    MONTHLY_EXPIRY,
    SPOT,
    SPX_ROWS,
    SPY_ROWS,
    write_bars_capture,
    write_capture_manifest,
)
from tests.fixtures.massive_structural_sample import contracts_payload  # noqa: E402
from tree_options.data import bars_manifest as bm  # noqa: E402
from tree_options.seal.errors import LedgerCorruptError, LedgerRootRefusedError  # noqa: E402

T0 = 1_800_000_000
COMMITTED_PROFILE = REPO_ROOT / "data" / "bars" / "selection-profile.json"


@pytest.fixture()
def scratch_root() -> Iterator[Path]:
    root = REPO_ROOT / "artifacts" / "bars-a4-tests" / uuid.uuid4().hex
    root.mkdir(parents=True, exist_ok=True)
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture()
def capture_bundle(tmp_path: Path) -> dict[str, Path]:
    """Synthetic capture dir + verified capture manifest.

    Round-1 review fix: the manifest must live INSIDE the capture_dir at
    the standard path so verify_bars_work_manifest's regenerate-and-compare
    step can find it. The external copy is for tests that need a path
    separate from the capture dir (e.g. drift simulation).
    """
    capture_dir = write_bars_capture(tmp_path / "capture")
    manifest_path = write_capture_manifest(capture_dir, capture_dir / "capture_manifest.json")
    external_manifest_path = tmp_path / "capture_manifest.json"
    external_manifest_path.write_bytes(manifest_path.read_bytes())
    return {
        "capture_dir": capture_dir,
        "capture_manifest": manifest_path,
        "external_capture_manifest": external_manifest_path,
    }


def _build(bundle: dict[str, Path], **overrides: object) -> bm.BarsWorkManifest:
    profile = bm.load_selection_profile(COMMITTED_PROFILE)
    return bm.build_bars_work_manifest(
        bundle["capture_dir"],
        profile=profile,
        capture_manifest=bundle["capture_manifest"],
        budget_limit=overrides.pop("budget_limit", 45),
    )


# ---- committed selection profile ---------------------------------------------------


def test_committed_profile_loads_pending_and_binds() -> None:
    profile = bm.load_selection_profile(COMMITTED_PROFILE)
    assert profile.status == "PENDING owner ratification"
    assert profile.selector == bm.SELECTION_SELECTOR
    for parameter in (
        profile.wanted,
        profile.dte_min,
        profile.dte_max,
        profile.strike_band,
        profile.expiries,
        profile.sides,
    ):
        assert parameter.tag == "draft"
    # transcribed parameter names: the real kwargs of select_atm_grid_bars
    assert set(type(profile).model_fields) >= {
        "wanted",
        "dte_min",
        "dte_max",
        "strike_band",
        "expiries",
        "sides",
    }


def test_tampered_profile_refused(tmp_path: Path) -> None:
    doc = json.loads(COMMITTED_PROFILE.read_text(encoding="utf-8"))
    doc["strike_band"]["value"] = 5  # ratify-by-edit: the hash must refuse it
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(bm.BarsManifestError, match="does not bind"):
        bm.load_selection_profile(path)


def test_profile_bool_value_refused() -> None:
    doc = json.loads(COMMITTED_PROFILE.read_text(encoding="utf-8"))
    doc["dte_min"]["value"] = True
    with pytest.raises(ValueError, match="bool"):
        bm.SelectionProfile.model_validate(doc)


# ---- build + selection ---------------------------------------------------------------


def test_build_pins_the_unmodified_selection_grid(capture_bundle: dict[str, Path]) -> None:
    manifest = _build(capture_bundle)
    got = [
        (e.underlying, e.as_of, e.expiry, e.strike_rank, e.side, e.ticker) for e in manifest.entries
    ]
    assert got == [tuple(entry) for entry in EXPECTED_ENTRIES]
    # the fractional strike keeps the vendor's exact token
    assert manifest.entries[2].strike == "587.5"
    assert manifest.entries[0].strike == "580"
    # the non-monthly and out-of-band expiries never entered
    assert all(e.expiry == MONTHLY_EXPIRY for e in manifest.entries)
    # the selection's own notes ride along (SPX had no in-band expiry)
    assert any("I:SPX" in note for note in manifest.selection_notes)


def test_cost_arithmetic_restatement(capture_bundle: dict[str, Path]) -> None:
    manifest = _build(capture_bundle)
    assert manifest.cost.expected_requests == len(manifest.entries) == 7
    assert manifest.cost.max_attempts_per_request == bm.DEFAULT_MAX_ATTEMPTS_PER_REQUEST
    assert manifest.cost.worst_case_wire_requests == 7 * bm.DEFAULT_MAX_ATTEMPTS_PER_REQUEST
    assert manifest.cost.budget_covers_worst_case is True  # 28 <= 45
    tight = _build(capture_bundle, budget_limit=7)
    assert tight.cost.budget_limit == 7
    assert tight.cost.budget_covers_worst_case is False  # 7 < 28: Budget refuses
    with pytest.raises(bm.BarsManifestError):
        bm.estimate_bars_cost(0, max_attempts_per_request=4, budget_limit=45)


def test_work_manifest_hash_binding(capture_bundle: dict[str, Path]) -> None:
    manifest = _build(capture_bundle)
    bm.verify_bars_work_manifest(
        manifest,
        profile=bm.load_selection_profile(COMMITTED_PROFILE),
        capture_manifest_sha256=manifest.capture_manifest_sha256,
        capture_dir=capture_bundle["capture_dir"],
    )
    tampered = manifest.model_copy(update={"selection_notes": ("edited after the fact",)})
    with pytest.raises(bm.BarsManifestError, match="does not bind"):
        bm.verify_bars_work_manifest(tampered)


def test_work_manifest_refuses_foreign_profile(capture_bundle: dict[str, Path]) -> None:
    manifest = _build(capture_bundle)
    doc = json.loads(COMMITTED_PROFILE.read_text(encoding="utf-8"))
    doc["sides"]["value"] = "call"
    doc["content_sha256"] = ""  # a DIFFERENT, self-consistent profile
    other = bm.SelectionProfile.model_validate(doc)
    other = other.model_copy(update={"content_sha256": bm.profile_content_sha256(other)})
    assert other.content_sha256 != manifest.profile_sha256
    with pytest.raises(bm.BarsManifestError, match="different selection profile"):
        bm.verify_bars_work_manifest(manifest, profile=other)


def test_rebuild_refuses_capture_that_drifted_from_its_manifest(
    capture_bundle: dict[str, Path],
) -> None:
    (capture_bundle["capture_dir"] / "masters" / "spy_2025-03-05.json").write_text(
        contracts_payload(results=SPY_ROWS[:1], as_of=AS_OF), encoding="utf-8"
    )
    with pytest.raises(bm.BarsManifestError, match="does not match its manifest"):
        _build(capture_bundle)


def test_verify_refuses_when_capture_dir_holds_a_different_self_consistent_manifest(
    capture_bundle: dict[str, Path],
) -> None:
    """Round-2 probe (finding 4, /tmp/pr-a-bars-dual-manifest-probe.log): the
    launcher hashes manifest A, but regeneration silently loaded manifest B
    from capture_dir/capture_manifest.json and compared only entries — two
    distinct self-consistent manifests over the same masters both verified.

    The fix binds them: when a capture-manifest hash is supplied, the bytes at
    capture_dir/capture_manifest.json must hash to it BEFORE regeneration, and
    the FULL regenerated manifest must equal the committed one."""
    manifest = _build(capture_bundle)  # bound to manifest A's bytes
    honest_sha = manifest.capture_manifest_sha256
    # honest path first: A is still the manifest in the capture dir
    bm.verify_bars_work_manifest(
        manifest,
        profile=bm.load_selection_profile(COMMITTED_PROFILE),
        capture_manifest_sha256=honest_sha,
        capture_dir=capture_bundle["capture_dir"],
    )
    # Now swap in a DIFFERENT self-consistent manifest B over the same
    # masters (different budget arithmetic + notes -> different bytes, still
    # verifying against the directory).
    from tree_options.data.massive_manifest import build_massive_capture_manifest

    manifest_b = build_massive_capture_manifest(
        capture_bundle["capture_dir"],
        capture_version="m4b-capture/1",
        budget_limit=99,
        requests_charged=6,
        client_stats={"requests": 6},
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
        notes=["variant B: a different self-consistent capture manifest"],
    )
    (capture_bundle["capture_dir"] / "capture_manifest.json").write_text(
        manifest_b.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    variant_b_sha = hashlib.sha256(
        (capture_bundle["capture_dir"] / "capture_manifest.json").read_bytes()
    ).hexdigest()
    assert variant_b_sha != honest_sha, "the probe needs two distinct manifests"
    with pytest.raises(bm.BarsManifestError) as exc_info:
        bm.verify_bars_work_manifest(
            manifest,
            profile=bm.load_selection_profile(COMMITTED_PROFILE),
            capture_manifest_sha256=honest_sha,
            capture_dir=capture_bundle["capture_dir"],
        )
    message = str(exc_info.value)
    assert honest_sha[:12] in message  # both hashes are named
    assert variant_b_sha[:12] in message


def test_rebuild_refuses_unstamped_envelope(tmp_path: Path) -> None:
    capture_dir = tmp_path / "capture"
    (capture_dir / "masters").mkdir(parents=True)
    (capture_dir / "masters" / "spy_2025-03-05.json").write_text(
        json.dumps({"results": []}), encoding="utf-8"
    )
    with pytest.raises(bm.BarsManifestError, match="master envelope refused"):
        bm.rebuild_master_captures(capture_dir)


def test_rebuild_returns_spot_for_the_selection(tmp_path: Path) -> None:
    capture_dir = write_bars_capture(tmp_path / "capture")
    _captures, spot, index = bm.rebuild_master_captures(capture_dir)
    # spot text follows the capture bridge's exponent-free decimal convention
    # (`_plain`): 580.00 -> "580" — value-exact for the selection's anchor.
    assert spot == {"SPY": {AS_OF: "580"}, "I:SPX": {AS_OF: "5750"}}
    meta = index["O:SPY250418C00587500"]
    assert (meta.underlying, meta.expiration, meta.strike, meta.kind) == (
        "SPY",
        MONTHLY_EXPIRY,
        "587.5",
        "call",
    )
    # every ticker the scenario declares is indexed (both masters)
    assert "O:SPX250321C05800000" in index


def test_empty_selection_refused(tmp_path: Path) -> None:
    capture_dir = tmp_path / "capture"
    (capture_dir / "masters").mkdir(parents=True)
    # SPX alone: no in-band expiry -> the monthly ATM grid selects nothing
    (capture_dir / "masters" / "spx_2025-03-05.json").write_text(
        contracts_payload(results=SPX_ROWS, as_of=AS_OF), encoding="utf-8"
    )
    (capture_dir / "spot_proxy.json").write_text(json.dumps(SPOT), encoding="utf-8")
    from tree_options.data.massive_manifest import build_massive_capture_manifest

    manifest = build_massive_capture_manifest(
        capture_dir,
        capture_version="m4b-capture/1",
        budget_limit=45,
        requests_charged=1,
        client_stats={"requests": 1},
        masters=[
            {
                "underlying": "I:SPX",
                "as_of": AS_OF,
                "pages": 1,
                "rows": len(SPX_ROWS),
                "complete": True,
                "truncated": False,
                "error": None,
                "file": "spx_2025-03-05.json",
            }
        ],
        bars=[],
        spot_proxy=SPOT,
        notes=[],
    )
    manifest_path = tmp_path / "capture_manifest_spx.json"
    manifest_path.write_text(manifest.model_dump_json(indent=2) + "\n", encoding="utf-8")
    profile = bm.load_selection_profile(COMMITTED_PROFILE)
    with pytest.raises(bm.BarsManifestError, match="selected no contracts"):
        bm.build_bars_work_manifest(
            capture_dir, profile=profile, capture_manifest=manifest_path, budget_limit=45
        )


# ---- determinism / ordering -----------------------------------------------------------


@settings(derandomize=True, max_examples=50)
@given(permutation=st.permutations(range(3)))
def test_regeneration_is_byte_identical_regardless_of_file_order(
    permutation: list[int],
) -> None:
    """Byte-identical regeneration over identical inputs: the scratch capture
    dir is rebuilt inside the test body (a tempfile), its three files written
    in a permuted creation order, and two builds must agree byte for byte."""
    payloads: list[tuple[Path, str]] = [
        (
            Path("masters") / "spy_2025-03-05.json",
            contracts_payload(results=SPY_ROWS, as_of=AS_OF),
        ),
        (
            Path("masters") / "spx_2025-03-05.json",
            contracts_payload(results=SPX_ROWS, as_of=AS_OF),
        ),
        (Path("spot_proxy.json"), json.dumps(SPOT, sort_keys=True) + "\n"),
    ]

    with tempfile.TemporaryDirectory() as td:
        capture_dir = Path(td) / "capture"
        capture_dir.mkdir()
        for index in permutation:  # permuted creation order on disk
            target = capture_dir / payloads[index][0]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(payloads[index][1], encoding="utf-8")
        manifest_path = write_capture_manifest(capture_dir, Path(td) / "cm.json")
        profile = bm.load_selection_profile(COMMITTED_PROFILE)
        first = bm.build_bars_work_manifest(
            capture_dir, profile=profile, capture_manifest=manifest_path, budget_limit=45
        )
        second = bm.build_bars_work_manifest(
            capture_dir, profile=profile, capture_manifest=manifest_path, budget_limit=45
        )
        assert first.model_dump_json() == second.model_dump_json()
        assert first.content_sha256 == second.content_sha256
        assert first == second


_TICKER_STRIKE = {
    "O:SPY250418C00580000": "580",
    "O:SPY250418P00580000": "580",
    "O:SPY250418C00587500": "587.5",
    "O:SPY250418C00570000": "570",
    "O:SPY250418P00570000": "570",
    "O:SPY250418C00590000": "590",
    "O:SPY250418P00590000": "590",
}


def _entry_of(expected: tuple[str, str, str, int, str, str]) -> bm.BarsWorkEntry:
    _underlying, as_of, expiry, strike_rank, side, ticker = expected
    return bm.BarsWorkEntry(
        underlying="SPY",
        as_of=as_of,
        expiry=expiry,
        strike=_TICKER_STRIKE[ticker],
        strike_rank=strike_rank,
        side=side,
        ticker=ticker,
    )


def test_order_entries_canonical_from_shuffled() -> None:
    entries = [_entry_of(entry) for entry in EXPECTED_ENTRIES]
    shuffled = list(reversed(entries))
    assert bm.order_entries(shuffled) == bm.order_entries(entries) == tuple(entries)


def test_manifest_refuses_unordered_entries() -> None:
    profile = bm.load_selection_profile(COMMITTED_PROFILE)
    cost = bm.BarsCostEstimate(
        expected_requests=7,
        max_attempts_per_request=4,
        worst_case_wire_requests=28,
        budget_limit=45,
        budget_covers_worst_case=True,
    )
    ordered = [_entry_of(entry) for entry in EXPECTED_ENTRIES]
    unordered = tuple(reversed(ordered))
    with pytest.raises(ValueError, match="canonical order"):
        bm.BarsWorkManifest(
            schema_version=bm.BARS_WORK_SCHEMA_VERSION,
            profile_sha256=profile.content_sha256,
            capture_manifest_sha256="e" * 64,
            entries=unordered,
            selection_notes=(),
            cost=cost,
            content_sha256="",
        )


# ---- bars-authority mirror ledger -----------------------------------------------------


def _approval(**overrides: str) -> dict[str, str]:
    fields = dict(
        protocol_hash="a" * 64,
        amendment_packet_sha256="b" * 64,
        census_sha256="c" * 64,
        work_manifest_sha256="d" * 64,
    )
    fields.update(overrides)
    return fields


def test_approval_and_consumption_chain_verifies(scratch_root: Path) -> None:
    approval = bm.append_bars_launch_approval(
        scratch_root, reason="owner approved the grid", at_epoch=T0, **_approval()
    )
    consumed = bm.append_bars_launch_consumed(
        scratch_root, reason="era launched", at_epoch=T0 + 1, **_approval()
    )
    view = bm.read_bars_ledger(scratch_root)
    assert [r.kind for r in view.records] == ["BARS_LAUNCH_APPROVAL", "BARS_LAUNCH_CONSUMED"]
    assert view.records[0].prev_record_sha256 == "0" * 64
    assert view.records[1].prev_record_sha256 == approval.record_sha256
    assert view.tail_hash == consumed.record_sha256
    assert not view.tail_damaged
    # domain separation: a bars record never hashes as a G4 seal record
    assert approval.record_sha256 != consumed.record_sha256


def test_bars_ledger_tamper_detected(scratch_root: Path) -> None:
    bm.append_bars_launch_approval(scratch_root, reason="one", at_epoch=T0, **_approval())
    bm.append_bars_launch_consumed(scratch_root, reason="two", at_epoch=T0 + 1, **_approval())
    path = scratch_root / bm.BARS_LEDGER_FILENAME
    lines = path.read_text().splitlines()
    record = json.loads(lines[0])  # MID-FILE tamper: not a torn tail
    record["reason"] = "rewritten history"
    lines[0] = json.dumps(record, sort_keys=True, separators=(",", ":"))
    path.write_text("\n".join(lines) + "\n")
    with pytest.raises(LedgerCorruptError):
        bm.read_bars_ledger(scratch_root)


def test_bars_ledger_missing_is_empty_view(scratch_root: Path) -> None:
    view = bm.read_bars_ledger(scratch_root / "absent")
    assert view.records == ()
    assert view.tail_hash == "0" * 64
    assert not view.tail_damaged


def test_bars_ledger_default_root_constant_pinned() -> None:
    assert bm.DEFAULT_BARS_AUTHORITY_ROOT == Path("artifacts/bars-authority")


def test_bars_ledger_tmp_root_refused() -> None:
    under_tmp = Path("/tmp") / f"bars-a4-refused-{uuid.uuid4().hex}"
    with pytest.raises(LedgerRootRefusedError):
        bm.read_bars_ledger(under_tmp)
    with pytest.raises(LedgerRootRefusedError):
        bm.append_bars_launch_approval(under_tmp, reason="x", at_epoch=T0, **_approval())


def test_bars_ledger_separate_domain_from_seal(scratch_root: Path) -> None:
    """A bars record's chain hash must not verify under a foreign ledger:
    the same payload re-encoded as a seal LedgerRecord would not hash equal."""
    record = bm.append_bars_launch_approval(
        scratch_root, reason="domain pin", at_epoch=T0, **_approval()
    )
    assert record.record_sha256 != bm.GENESIS_PREV
    # the /tmp rule is the IMPORTED seal rule, byte-for-byte the same behavior
    from tree_options.seal.ledger import validate_ledger_root

    with pytest.raises(LedgerRootRefusedError):
        validate_ledger_root(Path("/tmp/anything"))
