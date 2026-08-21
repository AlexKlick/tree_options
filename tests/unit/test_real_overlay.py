"""WS-A: RealOptionOverlay — the OptionPitSurface contract over real data,
PIT refusals, and the real-data manifest (build/verify fail-closed).

Hermetic: runs off `tests/fixtures/cboe_eod_rows.py` only. The surface is
constructed UNMODIFIED (`OptionPitSurface`) — the overlay duck-types the
synthetic `GeneratedOptionOverlay`, so the tests cast for the type checker.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from typing import cast

import pytest

from tests.fixtures import cboe_eod_rows as fx
from tree_options.data.cboe_eod import (
    REAL_MANIFEST_DOMAIN,
    CboeEodError,
    PITInvariantError,
    RealOptionsManifestError,
    build_real_options_manifest,
    parse_cboe_eod_csv,
    validate_pit_invariants,
    verify_real_options_manifest,
)
from tree_options.data.digest import canonical_bytes
from tree_options.data.options_pit import NoOptionFileError, OptionPitSurface
from tree_options.data.real_overlay import RealOptionOverlay, build_real_overlay
from tree_options.synth_options.generate import (
    GeneratedOptionOverlay,
    OptionQuoteSnapshot,
    contract_id_of,
)
from tree_options.time.calendar import NotASessionError

STRIKE_440 = Decimal("440.00")
S_0825 = date(2023, 8, 25)
S_0828 = date(2023, 8, 28)
S_0829 = date(2023, 8, 29)
S_1124 = date(2023, 11, 24)
EXP_SEP = date(2023, 9, 15)
EXP_NOV = date(2023, 11, 17)
EXP_DEC = date(2023, 12, 15)
CID_440C = contract_id_of("SPY", EXP_SEP, "C", STRIKE_440)
CID_440P = contract_id_of("SPY", EXP_SEP, "P", STRIKE_440)
CID_275C = contract_id_of("SPY", EXP_SEP, "C", Decimal("275.00"))
CID_720C = contract_id_of("SPY", EXP_NOV, "C", Decimal("720.00"))
CID_720P = contract_id_of("SPY", EXP_NOV, "P", Decimal("720.00"))
CID_200P = contract_id_of("SPY", EXP_NOV, "P", Decimal("200.00"))
CID_4425C = contract_id_of("SPY", EXP_SEP, "C", Decimal("442.50"))
CID_440C_DEC = contract_id_of("SPY", EXP_DEC, "C", STRIKE_440)


@pytest.fixture()
def overlay(tmp_path: Path) -> RealOptionOverlay:
    result = parse_cboe_eod_csv(fx.write_csv(tmp_path / "spy.csv", fx.SPY_MAIN_ROWS))
    return build_real_overlay(result)


@pytest.fixture()
def parsed(tmp_path: Path):
    return parse_cboe_eod_csv(fx.write_csv(tmp_path / "spy.csv", fx.SPY_MAIN_ROWS))


@pytest.fixture()
def surface(overlay: RealOptionOverlay) -> OptionPitSurface:
    return OptionPitSurface(cast(GeneratedOptionOverlay, overlay))


# ---- construction + spec ---------------------------------------------------


def test_spec_world_id_is_deterministic(parsed, overlay: RealOptionOverlay) -> None:
    again = build_real_overlay(parsed)
    assert again.spec.world_id == overlay.spec.world_id
    assert overlay.spec.world_id.startswith("cboe-eod/SPY/")
    assert overlay.spec.quote_source == "cboe-option-eod/1"
    assert overlay.source_sha256 == parsed.source_sha256
    assert OptionPitSurface(cast(GeneratedOptionOverlay, overlay)).snapshot_id == (
        overlay.spec.world_id
    )


def test_overlay_rejects_pit_violation(overlay: RealOptionOverlay) -> None:
    files = {s: f.model_copy(deep=True) for s, f in overlay._day_files.items()}
    bad = files[S_0825].model_copy(deep=True)
    entry = bad.entries[0].model_copy(
        update={
            "quote_eod": OptionQuoteSnapshot(
                exchange_timestamp=datetime(2023, 8, 30, 20, 0, tzinfo=UTC),
                bid=Decimal("1.00"),
                ask=Decimal("1.02"),
                bid_size=1,
                ask_size=1,
            )
        }
    )
    files[S_0825] = bad.model_copy(update={"entries": (entry, *bad.entries[1:])})
    with pytest.raises(PITInvariantError, match=CID_440C):
        RealOptionOverlay(files, source_sha256=overlay.source_sha256)
    with pytest.raises(PITInvariantError):
        validate_pit_invariants(files)


def test_overlay_rejects_received_at_mismatch(overlay: RealOptionOverlay) -> None:
    files = dict(overlay._day_files)
    files[S_0825] = files[S_0825].model_copy(
        update={"received_at": datetime(2023, 8, 28, 13, 5, tzinfo=UTC)}
    )
    with pytest.raises(CboeEodError, match="publication wall"):
        RealOptionOverlay(files, source_sha256="0" * 64)


def test_overlay_rejects_mixed_underlyings(overlay: RealOptionOverlay) -> None:
    files = dict(overlay._day_files)
    files[S_0825] = files[S_0825].model_copy(update={"underlying_security_id": "TSLA"})
    with pytest.raises(CboeEodError, match="multiple underlyings"):
        RealOptionOverlay(files, source_sha256="0" * 64)


def test_standalone_master_from_ids(overlay: RealOptionOverlay) -> None:
    bare = RealOptionOverlay(overlay._day_files, source_sha256=overlay.source_sha256)
    contract = bare.contract(CID_440C)  # recovered from the canonical id
    assert contract.underlying_security_id == "SPY"
    assert contract.expiration == EXP_SEP
    assert contract.strike == STRIKE_440
    assert bare.contract_count() == 6  # entry-bearing cells only (no zero-greeks)
    with pytest.raises(ValueError):
        bare.contract("OPT-SPY-230915-C-09999999")  # strike off every ladder
    with pytest.raises(ValueError):
        bare.contract("garbage")


# ---- eligibility / sessions -------------------------------------------------


def test_publication_of_unknown_session_fails(overlay: RealOptionOverlay) -> None:
    assert overlay.publication_of(S_0825) == datetime(2023, 8, 28, 13, 0, tzinfo=UTC)
    with pytest.raises(CboeEodError):
        overlay.publication_of(date(2023, 8, 22))


def test_eligibility_surface(overlay: RealOptionOverlay) -> None:
    assert overlay.world_sessions() == (S_0825, S_0828, S_0829, S_1124)
    assert overlay.eligible_sessions("SPY") == (S_0825, S_0828, S_0829, S_1124)
    assert overlay.eligible_sessions("TSLA") == ()
    assert overlay.eligible_on(S_0825) == ("SPY",)
    assert overlay.eligible_on(date(2023, 8, 22)) == ()
    assert overlay.has_file("SPY", S_0825)
    assert not overlay.has_file("SPY", date(2023, 8, 22))
    assert overlay.has_any_file(S_1124)
    assert overlay.underlyings_ever_eligible() == ("SPY",)


def test_calendar_protocol(overlay: RealOptionOverlay) -> None:
    cal = overlay.calendar
    assert cal.session_close(S_0825) == datetime(2023, 8, 25, 20, 0, tzinfo=UTC)
    assert cal.session_close(S_1124) == datetime(2023, 11, 24, 18, 0, tzinfo=UTC)
    assert cal.session_open(S_0825) == datetime(2023, 8, 25, 13, 30, tzinfo=UTC)
    assert cal.nth_after(S_0825, 1) == S_0828
    assert cal.ordinal(S_0828) == 1
    with pytest.raises(NotASessionError):
        cal.session_close(date(2023, 8, 22))


# ---- master / chains ----------------------------------------------------------


def test_contract_master_from_parse(overlay: RealOptionOverlay) -> None:
    contracts = overlay.contracts_for("SPY")
    ids = [c.contract_id for c in contracts]
    assert ids == sorted(ids)
    assert len(contracts) == 8  # includes the zero-greeks identities
    assert {CID_275C, CID_200P} <= set(ids)
    c440 = overlay.contract(CID_440C)
    assert c440.option_root == "SPY"
    assert c440.exercise_style == "american"
    assert c440.listing_start == S_0825
    assert c440.listing_end == S_0829
    assert c440.exists_on(S_0828)
    assert not c440.exists_on(S_1124)
    assert overlay.contract(CID_440C_DEC).exists_on(S_1124)


def test_ladder_and_live_expiries(overlay: RealOptionOverlay) -> None:
    assert overlay.ladder_for("SPY", EXP_SEP) == (
        Decimal("275.00"),
        STRIKE_440,
        Decimal("442.50"),
    )
    assert overlay.ladder_for("SPY", EXP_NOV) == (Decimal("200.00"), Decimal("720.00"))
    with pytest.raises(ValueError):
        overlay.ladder_for("SPY", date(2030, 1, 18))
    assert [m.expiration for m in overlay.live_expiries_on("SPY", S_0825)] == [
        EXP_SEP,
        EXP_NOV,
    ]
    assert [m.expiration for m in overlay.live_expiries_on("SPY", S_1124)] == [EXP_DEC]
    assert overlay.live_expiries_on("SPY", date(2023, 8, 22)) == ()


def test_entry_for_membership(overlay: RealOptionOverlay) -> None:
    assert overlay.entry_for("SPY", S_0825, CID_440C).abs_delta == Decimal("0.5123")
    with pytest.raises(ValueError, match="no file"):
        overlay.entry_for("SPY", date(2023, 8, 22), CID_440C)
    with pytest.raises(ValueError, match="not in the"):
        overlay.entry_for("SPY", S_0825, CID_4425C)  # first quoted 8/28
    with pytest.raises(ValueError, match="not in the"):
        overlay.entry_for("SPY", S_0825, CID_275C)  # zero-greeks: no entry


def test_median_dollar_volume_is_declared_zero(overlay: RealOptionOverlay) -> None:
    assert overlay.median_dollar_volume("SPY", S_0825) == Decimal("0")


def test_quote_history_pit_invariants(overlay: RealOptionOverlay) -> None:
    events = overlay.quote_history(CID_440C)
    assert len(events) == 6  # 3 sessions x 2 snapshots
    assert [e.received_timestamp for e in events] == sorted(e.received_timestamp for e in events)
    for event in events:
        assert event.exchange_timestamp <= event.received_timestamp
        assert event.source == "cboe-option-eod/1"
        assert event.quote_condition == "regular"
    assert events[0].received_timestamp == datetime(2023, 8, 28, 13, 0, tzinfo=UTC)
    assert str(events[0].bid) == "2.0100"


def test_quote_history_zero_greeks_contract_empty(overlay: RealOptionOverlay) -> None:
    assert overlay.quote_history(CID_275C) == ()
    assert overlay.quote_history(CID_200P) == ()


def test_canonical_file_bytes(overlay: RealOptionOverlay) -> None:
    payload = json.loads(overlay.canonical_file_bytes("SPY", S_0825))
    assert set(payload) == {
        "underlying_security_id",
        "session",
        "received_at",
        "underlying_bid",
        "underlying_ask",
        "underlying_20d_median_dollar_volume",
        "entries",
    }
    assert payload["session"] == "2023-08-25"
    assert overlay.canonical_file_bytes("SPY", S_0825) == overlay.canonical_file_bytes(
        "SPY", S_0825
    )


def test_contract_master_sha256(overlay: RealOptionOverlay, tmp_path: Path) -> None:
    assert overlay.contract_master_sha256() == overlay.contract_master_sha256()
    assert overlay.contract_count() == 8
    spx = parse_cboe_eod_csv(fx.write_csv(tmp_path / "spx.csv", fx.SPX_CGI_ROWS))
    spx_overlay = build_real_overlay(spx)
    assert spx_overlay.contract_master_sha256() != overlay.contract_master_sha256()
    spx_call = spx_overlay.contract(contract_id_of("^SPX", EXP_SEP, "C", Decimal("4400.00")))
    assert spx_call.exercise_style == "european"
    assert spx_call.option_root == "SPX"


# ---- the unmodified M3 PIT surface over real data ------------------------------


def test_surface_t_plus_1_gate(surface: OptionPitSurface, overlay: RealOptionOverlay) -> None:
    decision_at = overlay.calendar.session_close(S_0828)
    assert surface.visible_file_session("SPY", decision_at) == S_0825  # file(t-1)
    assert surface.file_as_of("SPY", decision_at).session == S_0825
    before = datetime(2023, 8, 28, 12, 59, tzinfo=UTC)
    with pytest.raises(NoOptionFileError):
        surface.file_as_of("SPY", before)


def test_surface_candidate_snapshot(surface: OptionPitSurface, overlay: RealOptionOverlay) -> None:
    snap = surface.candidate_snapshot(overlay.contract(CID_440C), S_0828)
    assert snap.decision_at == overlay.calendar.session_close(S_0828)
    assert snap.abs_delta is not None and snap.abs_delta.value == Decimal("0.5123")
    assert snap.abs_delta.available_at == datetime(2023, 8, 28, 13, 0, tzinfo=UTC)
    assert snap.open_interest is not None and snap.open_interest.value == 5678
    assert snap.same_day_volume is not None and snap.same_day_volume.value == 1234
    assert snap.bid is not None and snap.bid.value == Decimal("1.9500")  # EOD pair
    assert snap.ask is not None and snap.ask.value == Decimal("1.9700")
    assert snap.underlying_20d_median_dollar_volume is not None
    assert snap.underlying_20d_median_dollar_volume.value == Decimal("0")
    assert snap.spans_earnings is not None and snap.spans_earnings.value is False


def test_surface_candidate_not_evaluable_paths(
    surface: OptionPitSurface, overlay: RealOptionOverlay
) -> None:
    # zero-greeks contract: no entry in the visible file -> None inputs
    snap = surface.candidate_snapshot(overlay.contract(CID_275C), S_0828)
    assert snap.abs_delta is None
    assert snap.open_interest is None
    assert snap.bid is None and snap.ask is None
    # not yet quoted on the visible file (first quoted 8/28)
    assert surface.entry_as_of("SPY", overlay.calendar.session_close(S_0828), CID_4425C) is None


def test_surface_quotes_spot_and_cross_section(
    surface: OptionPitSurface, overlay: RealOptionOverlay
) -> None:
    at = overlay.calendar.session_close(S_0828)
    events = surface.visible_quotes_as_of(CID_440C, at)
    assert len(events) == 2
    assert all(e.received_timestamp == datetime(2023, 8, 28, 13, 0, tzinfo=UTC) for e in events)
    assert surface.spot_mid_as_of("SPY", at) == Decimal("440.7900")
    assert CID_440C in {c.contract_id for c in surface.contracts_as_of("SPY", at)}
    assert CID_720P in {c.contract_id for c in surface.contracts_as_of("SPY", at)}  # grid cell
    assert surface.live_expiries_as_of("SPY", at) == (EXP_SEP, EXP_NOV)
    assert surface.eligible_as_of(S_0828) == ("SPY",)
    existing = surface.contracts_existing_on("SPY", S_0825)
    assert {CID_440C, CID_275C, CID_720C, CID_200P, CID_440P} <= {c.contract_id for c in existing}
    assert CID_4425C not in {c.contract_id for c in existing}  # listed 8/28


def test_surface_early_close_visible_file(
    surface: OptionPitSurface, overlay: RealOptionOverlay
) -> None:
    # decision at close(11/27-equivalent): the 11/24 file publishes 11/27 09:00
    at = datetime(2023, 11, 27, 15, 0, tzinfo=UTC)
    file = surface.file_as_of("SPY", at)
    assert file.session == S_1124
    assert all(e.quote_1545 is None for e in file.entries)


# ---- manifest -----------------------------------------------------------------


def test_manifest_roundtrip(parsed, overlay: RealOptionOverlay) -> None:
    manifest = build_real_options_manifest(parsed, overlay=overlay)
    assert manifest.provider == "cboe-option-eod/1"
    assert manifest.schema_version == "m4/1"
    assert manifest.underlying_security_id == "SPY"
    assert manifest.stats == parsed.stats
    verify_real_options_manifest(manifest, parsed, overlay=overlay)


def _rebind(manifest):
    """Recompute only the content binding — keeps the tamper, drops the
    self-hash alibi, so the specific check must catch it."""
    core = manifest.model_copy(update={"content_sha256": ""})
    digest = sha256(REAL_MANIFEST_DOMAIN + canonical_bytes(core)).hexdigest()
    return manifest.model_copy(update={"content_sha256": digest})


def test_manifest_refuses_tampered_stats(parsed, overlay: RealOptionOverlay) -> None:
    manifest = build_real_options_manifest(parsed, overlay=overlay)
    verify_real_options_manifest(manifest, parsed, overlay=overlay)
    tampered = _rebind(
        manifest.model_copy(update={"stats": replace(manifest.stats, rows_mapped=8)})
    )
    with pytest.raises(RealOptionsManifestError, match="row accounting"):
        verify_real_options_manifest(tampered, parsed, overlay=overlay)


def test_manifest_refuses_tampered_source_bytes(parsed, overlay: RealOptionOverlay) -> None:
    manifest = build_real_options_manifest(parsed, overlay=overlay)
    with pytest.raises(RealOptionsManifestError, match="tampered source"):
        verify_real_options_manifest(
            manifest, parsed, overlay=overlay, source_bytes=b"mutated bytes"
        )


def test_manifest_refuses_tampered_slice(parsed, overlay: RealOptionOverlay) -> None:
    manifest = build_real_options_manifest(parsed, overlay=overlay)
    slices = list(manifest.sample_slice_hashes)
    slices[0] = (slices[0][0], slices[0][1], "0" * 64)
    tampered = _rebind(manifest.model_copy(update={"sample_slice_hashes": tuple(slices)}))
    with pytest.raises(RealOptionsManifestError, match="sample slice"):
        verify_real_options_manifest(tampered, parsed, overlay=overlay)


def test_manifest_refuses_tampered_master_hash(parsed, overlay: RealOptionOverlay) -> None:
    manifest = build_real_options_manifest(parsed, overlay=overlay)
    tampered = _rebind(manifest.model_copy(update={"contract_master_sha256": "0" * 64}))
    with pytest.raises(RealOptionsManifestError, match="contract master"):
        verify_real_options_manifest(tampered, parsed, overlay=overlay)


def test_manifest_refuses_unbound_content(parsed, overlay: RealOptionOverlay) -> None:
    manifest = build_real_options_manifest(parsed, overlay=overlay)
    # source_path has no dedicated check: only the content binding catches it
    tampered = manifest.model_copy(update={"source_path": "/gone/moved.csv"})
    with pytest.raises(RealOptionsManifestError, match="content_sha256"):
        verify_real_options_manifest(tampered, parsed, overlay=overlay)


def test_manifest_refuses_foreign_overlay(
    parsed, overlay: RealOptionOverlay, tmp_path: Path
) -> None:
    manifest = build_real_options_manifest(parsed, overlay=overlay)
    spx = parse_cboe_eod_csv(fx.write_csv(tmp_path / "spx.csv", fx.SPX_CGI_ROWS))
    with pytest.raises(RealOptionsManifestError):
        verify_real_options_manifest(manifest, spx, overlay=build_real_overlay(spx))
