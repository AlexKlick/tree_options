"""Workstream B: the PIT options surface + manifest (M3 plan §3.B)."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from tests.conftest import REPO_ROOT
from tree_options.candidates.filters import CandidateFilter
from tree_options.data.ingest import ingest_snapshot
from tree_options.data.options_manifest import (
    OptionsManifest,
    build_options_manifest,
    paired_dataset_hash,
)
from tree_options.data.options_pit import NoOptionFileError, OptionPitSurface
from tree_options.data.quality_options import OptionsManifestError, verify_options_manifest
from tree_options.synth import generate_world
from tree_options.synth.spec import WorldSpec
from tree_options.synth_options import OptionsOverlaySpec, generate_overlay

WORLD_ID = "m3-unit-surface-905"
N_SESSIONS = 160


def _build():
    from tree_options.time.calendar import StaticSessionCalendar

    calendar = StaticSessionCalendar(
        REPO_ROOT / "data" / "calendar" / "nyse_sessions_2018_01_02_2026_12_31.json",
        REPO_ROOT / "data" / "calendar" / "nyse_sessions_2018_01_02_2026_12_31.sha256",
    )
    spec = WorldSpec(
        world_id=WORLD_ID, seed=905, kind="null", n_securities=24, n_sessions=N_SESSIONS
    )
    world = generate_world(spec, calendar)
    snapshot = ingest_snapshot(
        world.payload, world.master, snapshot_id=spec.world_id, normalization_code_sha="0" * 64
    )
    overlay = generate_overlay(
        spec=OptionsOverlaySpec(world_id=WORLD_ID, seed=905, eligible_top_n=10),
        bars=snapshot.bars,
        master=snapshot.master,
        actions=snapshot.actions,
        calendar=calendar,
    )
    return overlay, calendar, snapshot


@pytest.fixture(scope="module")
def built():
    return _build()


@pytest.fixture(scope="module")
def surface(built):
    overlay, _cal, _snap = built
    return OptionPitSurface(overlay)


@pytest.fixture(scope="module")
def protocol():
    from tree_options.protocol.loader import load_protocol

    return load_protocol()


def _a_session_with_files(overlay) -> date:  # pick a mid-world session
    sessions = overlay.world_sessions()
    return sessions[60]


# ---- availability (the T+1 leak tests) ------------------------------------


def test_file_visible_exactly_from_receipt(built, surface) -> None:  # type: ignore[no-untyped-def]
    overlay, calendar, _snap = built
    session = _a_session_with_files(overlay)
    sid = overlay.eligible_on(session)[0]
    receipt = overlay.publication_of(session)
    decision_at = calendar.session_close(session)
    # close(t) sees file(t-1), NEVER file(t): file(t) publishes 09:00 ET on t+1
    visible_at_close = surface.file_as_of(sid, decision_at)
    assert visible_at_close.session < session
    assert visible_at_close.received_at <= decision_at < receipt
    # visible from the receipt instant itself (inclusive)
    visible = surface.file_as_of(sid, receipt)
    assert visible.session == session
    # fail closed before ANY file exists: at the close of the first
    # eligible session, nothing has published yet
    first = overlay.eligible_sessions(sid)[0]
    with pytest.raises(NoOptionFileError):
        surface.file_as_of(sid, calendar.session_close(first))


def test_latest_file_wins(built, surface) -> None:  # type: ignore[no-untyped-def]
    overlay, calendar, _snap = built
    session = _a_session_with_files(overlay)
    sid = overlay.eligible_on(session)[0]
    next_session = calendar.nth_after(session, 1)
    # at close(t+1), the visible file is file(t)
    visible = surface.file_as_of(sid, calendar.session_close(next_session))
    assert visible.session == session
    assert visible.received_at <= calendar.session_close(next_session)


def test_entry_as_of_reads_the_visible_file(surface, built) -> None:  # type: ignore[no-untyped-def]
    overlay, calendar, _snap = built
    session = _a_session_with_files(overlay)
    sid = overlay.eligible_on(session)[0]
    file = overlay.day_file(sid, session)
    contract = overlay.contract(file.entries[0].contract_id)
    next_close = calendar.session_close(calendar.nth_after(session, 1))
    entry = surface.entry_as_of(sid, next_close, contract.contract_id)
    assert entry is not None and entry.contract_id == contract.contract_id


def test_contracts_as_of_honors_listing_windows(surface, built) -> None:  # type: ignore[no-untyped-def]
    overlay, _cal, _snap = built
    sid = overlay.underlyings_ever_eligible()[0]
    contracts = overlay.contracts_for(sid)
    early = min(contracts, key=lambda c: c.listing_start)
    before = early.listing_start - timedelta(days=3)
    assert surface.contracts_as_of(sid, before) == ()
    on_listing = surface.contracts_as_of(sid, early.listing_start)
    assert any(c.contract_id == early.contract_id for c in on_listing)
    past = early.expiration + timedelta(days=3)
    assert all(c.contract_id != early.contract_id for c in surface.contracts_as_of(sid, past))


# ---- candidate snapshots ----------------------------------------------------


def test_candidate_snapshot_carries_file_receipt_and_truthful_earnings(
    surface,
    built,
) -> None:  # type: ignore[no-untyped-def]
    overlay, calendar, _snap = built
    session = _a_session_with_files(overlay)
    sid = overlay.eligible_on(session)[0]
    file = overlay.day_file(sid, session)
    contract = overlay.contract(file.entries[0].contract_id)
    decision_session = calendar.nth_after(session, 1)  # close(t+1) sees file(t)
    snap = surface.candidate_snapshot(contract, decision_session)
    assert snap.decision_at == calendar.session_close(decision_session)
    assert snap.abs_delta is not None and snap.abs_delta.available_at == file.received_at
    assert snap.abs_delta.available_at <= snap.decision_at
    assert (
        snap.open_interest is not None and snap.open_interest.value == file.entries[0].open_interest
    )
    assert snap.same_day_volume is not None and snap.same_day_volume_applicable
    assert snap.spans_earnings is not None and snap.spans_earnings.value is False
    assert (
        snap.underlying_20d_median_dollar_volume is not None
        and snap.underlying_20d_median_dollar_volume.value
        == file.underlying_20d_median_dollar_volume
    )


def test_contract_absent_from_file_lands_not_evaluable(surface, built, protocol) -> None:  # type: ignore[no-untyped-def]
    overlay, calendar, _snap = built
    session = _a_session_with_files(overlay)
    sid = overlay.eligible_on(session)[0]
    # a contract that exists but does not quote in the visible file: pick a
    # far-future quarterly whose listing window has not started yet
    contracts = overlay.contracts_for(sid)
    not_yet_listed = next(
        (c for c in contracts if c.listing_start > calendar.nth_after(session, 5)), None
    )
    if not_yet_listed is None:
        pytest.skip("fixture has no not-yet-listed contract")
    snap = surface.candidate_snapshot(not_yet_listed, calendar.nth_after(session, 1))
    filt = CandidateFilter.from_protocol(calendar, protocol)
    decision = filt.evaluate(snap)
    assert not decision.accepted
    assert any(r.status == "NOT_EVALUABLE" for r in decision.results)


def test_in_band_candidate_is_accepted_by_the_filter(surface, built, protocol) -> None:  # type: ignore[no-untyped-def]
    """End to end: a §9.2-band contract from the visible file passes the
    frozen filter — the strategy's entry path exists."""
    overlay, calendar, _snap = built
    session = _a_session_with_files(overlay)
    sid = overlay.eligible_on(session)[0]
    file = overlay.day_file(sid, session)
    decision_session = calendar.nth_after(session, 1)
    filt = CandidateFilter.from_protocol(calendar, protocol)
    accepted = []
    for entry in file.entries:
        contract = overlay.contract(entry.contract_id)
        decision = filt.evaluate(surface.candidate_snapshot(contract, decision_session))
        if decision.accepted:
            accepted.append(entry.contract_id)
    assert accepted, "the fixture file must yield at least one accepted candidate"


def test_eligible_as_of_returns_file_session_cross_section(surface, built) -> None:  # type: ignore[no-untyped-def]
    overlay, calendar, _snap = built
    session = _a_session_with_files(overlay)
    cross = surface.eligible_as_of(calendar.nth_after(session, 1))
    assert cross == overlay.eligible_on(session)


# ---- manifest ----------------------------------------------------------------


def _manifest(built) -> OptionsManifest:  # type: ignore[no-untyped-def]
    overlay, _cal, snap = built
    return build_options_manifest(
        overlay, parent_content_sha256=snap.manifest.content_sha256, synth_options_code_sha="P" * 64
    )


def test_manifest_verifies(built) -> None:  # type: ignore[no-untyped-def]
    overlay, _cal, snap = built
    manifest = _manifest(built)
    verify_options_manifest(
        manifest,
        overlay,
        parent_content_sha256=snap.manifest.content_sha256,
        synth_options_code_sha="P" * 64,
    )
    assert manifest.snapshot_id == WORLD_ID
    assert manifest.sample_slice_hashes


def test_manifest_tamper_detected(built) -> None:  # type: ignore[no-untyped-def]
    overlay, _cal, snap = built
    base = _manifest(built)
    slices = list(base.sample_slice_hashes)
    slices[0] = (slices[0][0], slices[0][1], "0" * 64)
    tampered = base.model_copy(
        update={
            "sample_slice_hashes": tuple(slices),
            "content_sha256": base.content_sha256,  # stale self-hash on purpose
        }
    )
    with pytest.raises(OptionsManifestError, match="hash mismatch"):
        verify_options_manifest(
            tampered,
            overlay,
            parent_content_sha256=snap.manifest.content_sha256,
            synth_options_code_sha="P" * 64,
        )


def test_manifest_parent_binding_detected(built) -> None:  # type: ignore[no-untyped-def]
    overlay, _cal, snap = built
    manifest = _manifest(built)
    with pytest.raises(OptionsManifestError, match="parent"):
        verify_options_manifest(
            manifest, overlay, parent_content_sha256="0" * 64, synth_options_code_sha="P" * 64
        )
    with pytest.raises(OptionsManifestError, match="code pin"):
        verify_options_manifest(
            manifest,
            overlay,
            parent_content_sha256=snap.manifest.content_sha256,
            synth_options_code_sha="0" * 64,
        )


def test_manifest_self_hash_detected(built) -> None:  # type: ignore[no-untyped-def]
    overlay, _cal, snap = built
    manifest = _manifest(built)
    tampered = manifest.model_copy(update={"contract_count": manifest.contract_count + 1})
    with pytest.raises(OptionsManifestError, match="does not bind"):
        verify_options_manifest(
            tampered,
            overlay,
            parent_content_sha256=snap.manifest.content_sha256,
            synth_options_code_sha="P" * 64,
        )


def test_paired_dataset_hash_is_order_sensitive() -> None:
    a = paired_dataset_hash("A", "B")
    b = paired_dataset_hash("B", "A")
    assert a != b and len(a) == 64
