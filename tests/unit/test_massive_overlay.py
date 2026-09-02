"""M4-C: MassiveDerivedOverlay — the derived free-tier surface over captured
files, its PIT stamps, the staleness/refusal census, and the unmodified
`OptionPitSurface` consumer path (constructed with a cast, exactly like the
Cboe overlay tests: `OptionPitSurface` is annotated with the synthetic type).

Hermetic: hand-built vendor-shaped captures under tmp_path via the
`massive_structural_sample` builders (raw JSON TEXT, exact number tokens).
No network, no API key, no client.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest

from tests.fixtures import massive_structural_sample as fx
from tree_options.data import massive_overlay as mo
from tree_options.data.cboe_eod import publication_instant
from tree_options.data.massive_derived import PricingAssumptions, derived_abs_delta
from tree_options.data.massive_manifest import (
    MassiveManifestError,
    build_massive_capture_manifest,
)
from tree_options.data.massive_options import MassiveCapabilityError
from tree_options.data.massive_overlay import (
    DERIVATION_PROVENANCE,
    MASSIVE_DERIVED_PROVIDER,
    MASSIVE_DERIVED_SCHEMA_VERSION,
    MassiveDerivedOverlay,
    MassiveOverlayError,
    load_derived_surface,
)
from tree_options.data.options_pit import OptionPitSurface
from tree_options.synth_options.generate import GeneratedOptionOverlay, contract_id_of
from tree_options.time.sessions import session_close_instant

SPY = "SPY"
EXP = date(2026, 6, 19)  # a third Friday (LEAPS-grade tenor)

# Seven contiguous weekday sessions, 2025-04-07 (Mon) .. 2025-04-15 (Tue).
S1 = date(2025, 4, 7)
S2 = date(2025, 4, 8)
S3 = date(2025, 4, 9)
S4 = date(2025, 4, 10)
S5 = date(2025, 4, 11)  # Friday: its wall is Monday 09:00 ET
S6 = date(2025, 4, 14)
S7 = date(2025, 4, 15)
SESSIONS = (S1, S2, S3, S4, S5, S6, S7)

# Bar timestamps: ms epoch of 00:00 America/New_York (EDT, UTC-4).
T_S1 = "1743998400000"
T_S2 = fx.T_2025_04_08
T_S3 = fx.T_2025_04_09
T_S4 = "1744257600000"
T_S5 = "1744344000000"
T_S6 = fx.T_2025_04_14
T_S7 = fx.T_2025_04_15

C650_TICKER = "O:SPY260619C00650000"
C100_TICKER = "O:SPY260619C00100000"  # OCC strike field is x1000: 100 -> 00100000
P640_TICKER = "O:SPY260619P00640000"
C650_ID = contract_id_of(SPY, EXP, "C", Decimal("650"))
P650_ID = contract_id_of(SPY, EXP, "P", Decimal("650"))
C640_ID = contract_id_of(SPY, EXP, "C", Decimal("640"))
P640_ID = contract_id_of(SPY, EXP, "P", Decimal("640"))
C100_ID = contract_id_of(SPY, EXP, "C", Decimal("100"))

# The master: two clean rows, one deep-ITM row (its VWAP is under intrinsic,
# so its derivation refuses), plus two rows the overlay master must refuse —
# a nonstandard deliverable and an off-cent-grid strike.
ROW_C650 = fx.contract_result(
    ticker=C650_TICKER,
    underlying=SPY,
    expiration="2026-06-19",
    strike="650",
    contract_type="call",
)
ROW_P640 = fx.contract_result(
    ticker=P640_TICKER,
    underlying=SPY,
    expiration="2026-06-19",
    strike="640",
    contract_type="put",
)
ROW_C100 = fx.contract_result(
    ticker=C100_TICKER,
    underlying=SPY,
    expiration="2026-06-19",
    strike="100",
    contract_type="call",
)
ROW_NONSTANDARD = fx.contract_result(
    ticker="O:SPY1250619C00570000",
    underlying=SPY,
    expiration="2025-06-19",
    strike="570",
    contract_type="call",
    shares_per_contract="80",
)
ROW_OFFGRID = fx.contract_result(
    ticker="O:SPY250620C00587555",
    underlying=SPY,
    expiration="2025-06-20",
    strike="587.555",
    contract_type="call",
)
MASTER_JSON = fx.contracts_payload(
    results=(ROW_C650, ROW_P640, ROW_C100, ROW_NONSTANDARD, ROW_OFFGRID),
    as_of="2025-04-07",
)

# C650 bars: S1 (goes stale at the default frontier), the fractional-token
# bar on S2, then S4..S7 — NO bar on S3 (the no_bar cell).
BARS_C650 = fx.bars_payload(
    ticker=C650_TICKER,
    results_count="6",
    results=(
        fx.bar(v="250", t=T_S1, vw="12.50", o="12.45", c="12.55", h="12.60", low="12.40", n="40"),
        fx.bar(v="100", t=T_S2, vw="12.505", o="12.50", c="12.51", h="12.51", low="12.50", n="20"),
        fx.bar(v="90", t=T_S4, vw="12.40", o="12.40", c="12.40", h="12.45", low="12.35", n="18"),
        fx.bar(v="80", t=T_S5, vw="12.30", o="12.30", c="12.30", h="12.35", low="12.25", n="16"),
        fx.bar(v="70", t=T_S6, vw="12.20", o="12.20", c="12.20", h="12.25", low="12.15", n="14"),
        fx.bar(v="60", t=T_S7, vw="12.10", o="12.10", c="12.10", h="12.15", low="12.05", n="12"),
    ),
)
# The deep-ITM call: spot ~602 against strike 100, VWAP 5.00 — far under
# discounted intrinsic, so the pricer refuses to invert it.
BARS_C100 = fx.bars_payload(
    ticker=C100_TICKER,
    results_count="1",
    results=(fx.bar(v="10", t=T_S3, vw="5.00", o="5.00", c="5.00", h="5.00", low="5.00", n="2"),),
)
# No contract master owns this series: counted and named, never censused.
BARS_UNMATCHED = fx.bars_payload(
    ticker="O:QQQ250404C00480000",
    results_count="1",
    results=(fx.bar(v="3", t=T_S3, vw="4.00", o="4.00", c="4.00", h="4.00", low="4.00", n="2"),),
)
DEFAULT_BARS = (
    ("bars_c650.json", BARS_C650),
    ("bars_c100.json", BARS_C100),
    ("bars_unmatched.json", BARS_UNMATCHED),
)
SPOT_JSON = json.dumps(
    {
        SPY: {
            "2025-04-07": "600.00",
            "2025-04-08": "601.00",
            "2025-04-09": "602.00",
            "2025-04-10": "603.00",
            "2025-04-11": "604.00",
            "2025-04-14": "605.00",
            "2025-04-15": "606.00",
        }
    }
)


def write_capture(
    root: Path,
    *,
    master: str = MASTER_JSON,
    bars: tuple[tuple[str, str], ...] = DEFAULT_BARS,
    spot: str | None = SPOT_JSON,
) -> Path:
    masters = root / "masters"
    masters.mkdir(parents=True)
    (masters / "spy_2025-04-07.json").write_text(master, encoding="utf-8")
    if bars:
        bar_dir = root / "bars"
        bar_dir.mkdir()
        for name, text in bars:
            (bar_dir / name).write_text(text, encoding="utf-8")
    if spot is not None:
        (root / "spot_proxy.json").write_text(spot, encoding="utf-8")
    return root


@pytest.fixture()
def overlay(tmp_path: Path) -> MassiveDerivedOverlay:
    return load_derived_surface(write_capture(tmp_path / "capture"))


@pytest.fixture()
def surface(overlay: MassiveDerivedOverlay) -> OptionPitSurface:
    # The cast is the documented contract: the overlay duck-types the
    # synthetic GeneratedOptionOverlay (same as the Cboe overlay tests).
    return OptionPitSurface(cast(GeneratedOptionOverlay, overlay))


# ---- tokens, defaults, lineage -------------------------------------------------


def test_module_docstring_states_the_landed_candidate_seam() -> None:
    """(e) documentation truth: the pre-0.2.0 claim that
    `build_option_candidate_inputs` "keeps raising unconditionally" is rot —
    the G3 amendment (protocol 0.2.0) landed the builder, and the lane-2
    adapter feeds it. The module docstring must describe the seam that
    exists, never the gated one that was replaced."""
    doc = mo.__doc__ or ""
    assert "keeps raising unconditionally" not in doc
    assert "awaits a future owner-ratified G3 amendment packet" not in doc
    assert "build_option_candidate_inputs" in doc


def test_entry_for_docstring_states_the_landed_candidate_seam() -> None:
    """(w6 tail) The third stale pre-0.2.0 claim: `entry_for`'s docstring
    said candidate wiring "awaits the G3 amendment packet" — false since
    protocol 0.2.0 landed the builder and doubly since the lane-2 adapter
    (`data.vwap_pit_surface.VwapPitSurface`) shipped. The REFUSAL itself is
    unchanged (still the not-in-file ValueError); only the prose rot goes.
    Whitespace-normalized: the replacement prose wraps across source lines,
    and a raw substring assert passes vacuously over a line break."""
    import re

    doc = re.sub(r"\s+", " ", mo.MassiveDerivedOverlay.entry_for.__doc__ or "")
    assert "awaits the G3 amendment packet" not in doc
    assert "build_option_candidate_inputs" in doc
    assert "VwapPitSurface" in doc


def test_provider_and_schema_tokens(overlay: MassiveDerivedOverlay) -> None:
    assert MASSIVE_DERIVED_PROVIDER == "massive-derived-free/1"
    assert MASSIVE_DERIVED_SCHEMA_VERSION == "m4-massive-derived/1"
    for name in ("MASSIVE_DERIVED_PROVIDER", "MASSIVE_DERIVED_SCHEMA_VERSION"):
        assert name in mo.__all__
    assert overlay.spec.quote_source == MASSIVE_DERIVED_PROVIDER
    assert overlay.spec.provider == MASSIVE_DERIVED_PROVIDER
    assert overlay.spec.schema_version == MASSIVE_DERIVED_SCHEMA_VERSION
    quote = overlay.derived_quote(C650_ID, S2)
    assert quote.provider == MASSIVE_DERIVED_PROVIDER
    assert quote.schema_version == MASSIVE_DERIVED_SCHEMA_VERSION


def test_defaults_are_pricing_assumptions_defaults(overlay: MassiveDerivedOverlay) -> None:
    assert overlay.assumptions == PricingAssumptions()
    assert overlay.assumptions.risk_free == 0.03
    assert overlay.assumptions.dividend_yield == 0.0
    derived = overlay.derived_quote(C650_ID, S2).derived
    assert derived is not None
    assert derived.model == "black-scholes-1"
    assert derived.assumptions_version == "derived-pricing/1"
    assert derived.provenance == DERIVATION_PROVENANCE == "model-derived-from-vwap"


def test_spec_lineage_is_deterministic(tmp_path: Path) -> None:
    first = load_derived_surface(write_capture(tmp_path / "one"))
    second = load_derived_surface(write_capture(tmp_path / "two"))
    assert first.spec.world_id == second.spec.world_id
    assert first.source_sha256 == second.source_sha256
    assert first.contract_master_sha256() == second.contract_master_sha256()
    assert first.spec.world_id.startswith("massive-derived/SPY/")
    assert OptionPitSurface(cast(GeneratedOptionOverlay, first)).snapshot_id == (
        first.spec.world_id
    )


def test_missing_manifest_is_disclosed_not_fatal(overlay: MassiveDerivedOverlay) -> None:
    assert any(issue.startswith("no capture_manifest.json") for issue in overlay.issues)
    assert overlay.unmatched_option_tickers == ("O:QQQ250404C00480000",)
    assert any("O:QQQ250404C00480000" in issue for issue in overlay.issues)


# ---- derived reads: facts, stamps, PIT ------------------------------------------


def test_derived_quote_carries_vendor_facts_and_derived_stamps(
    overlay: MassiveDerivedOverlay,
) -> None:
    quote = overlay.derived_quote(C650_ID, S2)
    assert quote.status == "DERIVED"
    assert quote.reason is None
    assert quote.option_ticker == C650_TICKER
    assert quote.volume == 100
    assert quote.transactions == 20
    assert quote.underlying_security_id == SPY
    assert quote.derived is not None and quote.derived.provenance == DERIVATION_PROVENANCE


def test_derived_quote_matches_the_pinned_derivation_surface(
    overlay: MassiveDerivedOverlay,
) -> None:
    quote = overlay.derived_quote(C650_ID, S2)
    assert quote.derived is not None
    iv, abs_delta = derived_abs_delta(
        premium=float(Decimal("12.505")),
        spot=float(Decimal("601.00")),
        strike=650.0,
        dte_calendar_days=(EXP - S2).days,
        call_put="C",
        assumptions=PricingAssumptions(),
    )
    assert quote.derived.iv == Decimal(repr(iv))
    assert quote.derived.abs_delta == Decimal(repr(abs_delta))
    assert quote.derived.iv > 0
    assert Decimal(0) < quote.derived.abs_delta < 1


def test_custom_assumptions_change_the_derivation_and_ride_the_stamps(
    tmp_path: Path,
) -> None:
    capture = write_capture(tmp_path / "capture")
    overlay = load_derived_surface(capture, assumptions=PricingAssumptions(risk_free=0.05))
    quote = overlay.derived_quote(C650_ID, S2)
    assert quote.derived is not None
    iv, abs_delta = derived_abs_delta(
        premium=float(Decimal("12.505")),
        spot=float(Decimal("601.00")),
        strike=650.0,
        dte_calendar_days=(EXP - S2).days,
        call_put="C",
        assumptions=PricingAssumptions(risk_free=0.05),
    )
    assert quote.derived.iv == Decimal(repr(iv))
    assert quote.derived.abs_delta == Decimal(repr(abs_delta))
    assert overlay.assumptions.risk_free == 0.05


def test_vwap_token_survives_as_decimal_end_to_end(overlay: MassiveDerivedOverlay) -> None:
    fractional = overlay.derived_quote(C650_ID, S2)
    assert type(fractional.premium) is Decimal
    assert fractional.premium == Decimal("12.505")
    assert str(fractional.premium) == "12.505"
    stale = overlay.derived_quote(C650_ID, S1)
    assert type(stale.premium) is Decimal
    assert stale.premium == Decimal("12.50")  # the vendor fact outlives the refusal


def test_pit_invariants_on_every_row(overlay: MassiveDerivedOverlay) -> None:
    rows = overlay.derived_quotes()
    assert len(rows) == 11
    for row in rows:
        assert row.exchange_timestamp == session_close_instant(row.session)  # 16:00 ET
        assert row.received_timestamp == publication_instant(row.session)  # T+1 09:00 ET
        assert row.exchange_timestamp <= row.received_timestamp
    friday = overlay.derived_quote(C650_ID, S5)
    assert friday.received_timestamp == datetime(2025, 4, 14, 13, 0, tzinfo=UTC)
    assert friday.exchange_timestamp == datetime(2025, 4, 11, 20, 0, tzinfo=UTC)


def test_derived_quotes_for_orders_by_session(overlay: MassiveDerivedOverlay) -> None:
    rows = overlay.derived_quotes_for(C650_ID)
    assert [row.session for row in rows] == list(SESSIONS)
    with pytest.raises(MassiveOverlayError):
        overlay.derived_quotes_for("OPT-SPY-260619-C-00999999")
    with pytest.raises(MassiveOverlayError):
        overlay.derived_quote("OPT-SPY-260619-C-00999999", S2)


# ---- staleness / no_bar / refusals ----------------------------------------------


def test_stale_bar_never_carries_forward(overlay: MassiveDerivedOverlay) -> None:
    stale = overlay.derived_quote(C650_ID, S1)  # 6 sessions behind the frontier
    assert stale.status == "NOT_EVALUABLE"
    assert stale.reason == "stale"
    assert stale.derived is None
    assert stale.premium == Decimal("12.50")  # the fact stays, the quote does not
    assert overlay.derived_stats().not_evaluable_stale == 1


def test_staleness_boundary_is_inclusive(tmp_path: Path) -> None:
    capture = write_capture(tmp_path / "capture")
    relaxed = load_derived_surface(capture, staleness_sessions=6)
    assert relaxed.derived_quote(C650_ID, S1).status == "DERIVED"
    pinned = load_derived_surface(capture, staleness_sessions=0)
    stats = pinned.derived_stats()
    assert stats.derived_ok == 1  # only the frontier session derives
    assert stats.not_evaluable_stale == 6  # C650's five older bars + C100's S3 bar
    with pytest.raises(ValueError, match="staleness_sessions"):
        load_derived_surface(capture, staleness_sessions=-1)


def test_missing_bar_is_no_trade(overlay: MassiveDerivedOverlay) -> None:
    mid = overlay.derived_quote(C650_ID, S3)
    assert mid.status == "NOT_EVALUABLE"
    assert mid.reason == "no_bar"
    assert mid.premium is None and mid.volume is None and mid.transactions is None
    assert mid.derived is None
    never_traded = overlay.derived_quote(P640_ID, S1)
    assert never_traded.reason == "no_bar"
    outside_window = overlay.derived_quote(P640_ID, S4)  # never observed that session
    assert outside_window.status == "NOT_EVALUABLE"
    assert outside_window.reason == "no_bar"
    assert overlay.derived_stats().not_evaluable_nobar == 4


def test_derivation_refusal_is_counted_not_fatal(overlay: MassiveDerivedOverlay) -> None:
    refused = overlay.derived_quote(C100_ID, S3)  # VWAP far under intrinsic
    assert refused.status == "NOT_EVALUABLE"
    assert refused.reason is not None
    assert refused.reason.startswith("refused:")
    assert "lower vol bound" in refused.reason
    assert refused.premium == Decimal("5.00")
    assert refused.derived is None
    stats = overlay.derived_stats()
    assert stats.not_evaluable_refused == 1


def test_zero_volume_bar_refuses(tmp_path: Path) -> None:
    zero = fx.bars_payload(
        ticker=C650_TICKER,
        results_count="1",
        results=(
            fx.bar(v="0", t=T_S1, vw="12.00", o="12.00", c="12.00", h="12.00", low="12.00", n="0"),
        ),
    )
    capture = write_capture(
        tmp_path / "capture",
        master=fx.contracts_payload(results=(ROW_C650,), as_of="2025-04-07"),
        bars=(("bars_c650.json", zero),),
        spot=json.dumps({SPY: "600.00"}),  # the FLAT proxy form covers every session
    )
    overlay = load_derived_surface(capture)
    quote = overlay.derived_quote(C650_ID, S1)
    assert quote.status == "NOT_EVALUABLE"
    assert quote.reason is not None and "zero-volume" in quote.reason


def test_flat_spot_proxy_form_derives(tmp_path: Path) -> None:
    one = fx.bars_payload(
        ticker=C650_TICKER,
        results_count="1",
        results=(
            fx.bar(
                v="50", t=T_S1, vw="12.50", o="12.45", c="12.55", h="12.60", low="12.40", n="10"
            ),
        ),
    )
    capture = write_capture(
        tmp_path / "capture",
        master=fx.contracts_payload(results=(ROW_C650,), as_of="2025-04-07"),
        bars=(("bars_c650.json", one),),
        spot=json.dumps({SPY: "600.00"}),
    )
    quote = load_derived_surface(capture).derived_quote(C650_ID, S1)
    assert quote.status == "DERIVED"


def test_missing_spot_proxy_refuses_every_derivation(tmp_path: Path) -> None:
    one = fx.bars_payload(
        ticker=C650_TICKER,
        results_count="1",
        results=(
            fx.bar(
                v="50", t=T_S1, vw="12.50", o="12.45", c="12.55", h="12.60", low="12.40", n="10"
            ),
        ),
    )
    capture = write_capture(
        tmp_path / "capture",
        master=fx.contracts_payload(results=(ROW_C650,), as_of="2025-04-07"),
        bars=(("bars_c650.json", one),),
        spot=None,
    )
    overlay = load_derived_surface(capture)
    quote = overlay.derived_quote(C650_ID, S1)
    assert quote.status == "NOT_EVALUABLE"
    assert quote.reason is not None and "no spot proxy" in quote.reason
    assert overlay.derived_stats().not_evaluable_refused == 1
    assert any("spot_proxy" in issue for issue in overlay.issues)


# ---- the daily spot source (remediation-3, owner ruling 2026-09-02) ---------------
#
# Event-3's recorded FAIL: the capture's own spot proxy is FRIDAY-ONLY while
# option bars are DAILY — the T+1-visible session at a close(t)-Friday
# decision is a THURSDAY, every such cell refused "no spot proxy", no
# candidate ever carried a derived |delta| (no_in_band_strike 312/312), and
# the gate FAILED criterion 2 with zero candidates constructed. These tests
# pin the fix: the v2 sidecar's DAILY closes are consulted first; the v1
# proxy backstops the sessions it does cover; an injected non-finite close
# refuses at construction exactly as a file would.


def _monday_bar_payload() -> str:
    return fx.bars_payload(
        ticker=C650_TICKER,
        results_count="1",
        results=(
            fx.bar(
                v="50", t=T_S1, vw="12.50", o="12.45", c="12.55", h="12.60", low="12.40", n="10"
            ),
        ),
    )


FRIDAY_ONLY_SPOT = json.dumps({SPY: {"2025-04-11": "600.00"}})


def test_a_non_friday_session_without_daily_spot_refuses(tmp_path: Path) -> None:
    """THE EVENT-3 CLASS: a Monday bar under a Friday-only per-session proxy
    (the flat form is deliberately NOT used — it would cover every session
    and hide the gap the sealed event died on)."""
    capture = write_capture(
        tmp_path / "capture",
        master=fx.contracts_payload(results=(ROW_C650,), as_of="2025-04-07"),
        bars=(("bars_c650.json", _monday_bar_payload()),),
        spot=FRIDAY_ONLY_SPOT,
    )
    overlay = load_derived_surface(capture)
    quote = overlay.derived_quote(C650_ID, S1)
    assert quote.status == "NOT_EVALUABLE"
    assert quote.reason is not None and "no spot proxy" in quote.reason


def test_the_daily_spot_source_unblocks_the_non_friday_session(tmp_path: Path) -> None:
    capture = write_capture(
        tmp_path / "capture",
        master=fx.contracts_payload(results=(ROW_C650,), as_of="2025-04-07"),
        bars=(("bars_c650.json", _monday_bar_payload()),),
        spot=FRIDAY_ONLY_SPOT,
    )
    overlay = load_derived_surface(capture, spot_v2={SPY: {S1: (Decimal("600.00"), 12_345_678)}})
    quote = overlay.derived_quote(C650_ID, S1)
    assert quote.status == "DERIVED", quote.reason


def test_the_v1_proxy_backstops_a_session_the_sidecar_lacks(tmp_path: Path) -> None:
    capture = write_capture(
        tmp_path / "capture",
        master=fx.contracts_payload(results=(ROW_C650,), as_of="2025-04-07"),
        bars=(("bars_c650.json", _monday_bar_payload()),),
        spot=json.dumps({SPY: {S1.isoformat(): "600.00"}}),
    )
    # the sidecar covers a DIFFERENT session only — the derivation must fall
    # back to the v1 proxy's own per-session row for S1
    overlay = load_derived_surface(capture, spot_v2={SPY: {S2: (Decimal("601.00"), 100)}})
    quote = overlay.derived_quote(C650_ID, S1)
    assert quote.status == "DERIVED", quote.reason


def test_an_injected_non_finite_daily_close_refuses_at_construction(tmp_path: Path) -> None:
    capture = write_capture(
        tmp_path / "capture",
        master=fx.contracts_payload(results=(ROW_C650,), as_of="2025-04-07"),
        bars=(("bars_c650.json", _monday_bar_payload()),),
        spot=FRIDAY_ONLY_SPOT,
    )
    with pytest.raises(mo.MassiveOverlayError, match="spot_v2"):
        load_derived_surface(capture, spot_v2={SPY: {S1: (Decimal("Infinity"), 1)}})


def test_derived_stats_census(overlay: MassiveDerivedOverlay) -> None:
    stats = overlay.derived_stats()
    assert stats.contracts == 3
    assert stats.sessions == 7
    assert stats.bars == 7  # C650's six + C100's one (the QQQ series is unmatched)
    assert stats.derived_ok == 5
    assert stats.not_evaluable_stale == 1
    assert stats.not_evaluable_nobar == 4
    assert stats.not_evaluable_refused == 1
    assert stats.cells == 11
    assert stats.bars == stats.derived_ok + stats.not_evaluable_stale + stats.not_evaluable_refused


# ---- master refusals / grid synthesis -------------------------------------------


def test_master_row_refusals_are_named(overlay: MassiveDerivedOverlay) -> None:
    assert overlay.contract_count() == 3
    refused = overlay.refused_master_contracts
    assert any("shares_per_contract 80" in line for line in refused)
    assert any("off the cent grid" in line for line in refused)
    assert {c.contract_id for c in overlay.contracts_for(SPY)} == {C650_ID, P640_ID, C100_ID}


def test_grid_cell_synthesis(overlay: MassiveDerivedOverlay) -> None:
    observed = overlay.contract(C650_ID)
    assert observed.option_root == "SPY"
    assert observed.exercise_style == "american"
    assert observed.listing_start == S1 and observed.listing_end == S7
    synthesized = overlay.contract(P650_ID)  # the unobserved put side of the 650 cell
    assert synthesized.call_put == "P"
    assert synthesized.exercise_style == observed.exercise_style
    assert synthesized.option_root == observed.option_root
    assert synthesized.listing_start == S1 and synthesized.listing_end == S7
    with pytest.raises(ValueError):
        overlay.contract("OPT-SPY-260619-C-00660000")  # strike off every ladder
    with pytest.raises(ValueError):
        overlay.contract("garbage")


def test_ladder_and_live_expiries(overlay: MassiveDerivedOverlay) -> None:
    assert overlay.ladder_for(SPY, EXP) == (Decimal("100.00"), Decimal("640.00"), Decimal("650.00"))
    with pytest.raises(ValueError):
        overlay.ladder_for(SPY, date(2030, 1, 18))
    assert [m.expiration for m in overlay.live_expiries_on(SPY, S6)] == [EXP]
    assert [m.expiration for m in overlay.live_expiries_on(SPY, S1)] == [EXP]  # master as_of
    assert overlay.live_expiries_on(SPY, date(2025, 4, 18)) == ()
    assert overlay.contract(C100_ID).exists_on(S3)


# ---- the unmodified M3 PIT surface over the derived overlay ----------------------


def test_surface_contract_runs(surface: OptionPitSurface, overlay: MassiveDerivedOverlay) -> None:
    at = overlay.calendar.session_close(S7)
    assert surface.visible_file_session(SPY, at) == S6  # file(t-1): T+1 gate
    assert surface.entry_as_of(SPY, at, C650_ID) is None  # entry_for's ValueError path
    assert surface.live_expiries_as_of(SPY, at) == (EXP,)
    assert surface.strike_ladder(SPY, EXP) == (
        Decimal("100.00"),
        Decimal("640.00"),
        Decimal("650.00"),
    )
    assert surface.eligible_as_of(S7) == (SPY,)
    ids = {c.contract_id for c in surface.contracts_as_of(SPY, at)}
    assert {C650_ID, P650_ID, C640_ID, P640_ID} <= ids  # full C/P grid, synthesized cells
    existing = {c.contract_id for c in surface.contracts_existing_on(SPY, S1)}
    assert {C650_ID, P640_ID, C100_ID} <= existing


def test_surface_candidate_snapshot_is_the_not_evaluable_path(
    surface: OptionPitSurface, overlay: MassiveDerivedOverlay
) -> None:
    snap = surface.candidate_snapshot(overlay.contract(C650_ID), S7)
    assert snap.decision_at == overlay.calendar.session_close(S7)
    assert snap.abs_delta is None
    assert snap.open_interest is None
    assert snap.same_day_volume is None
    assert snap.bid is None and snap.ask is None
    assert snap.underlying_20d_median_dollar_volume is not None
    assert snap.underlying_20d_median_dollar_volume.value == Decimal("0")
    assert snap.spans_earnings is not None and snap.spans_earnings.value is False


def test_capability_boundary_is_loud(
    surface: OptionPitSurface, overlay: MassiveDerivedOverlay
) -> None:
    with pytest.raises(MassiveCapabilityError):
        overlay.day_file(SPY, S6)
    with pytest.raises(MassiveCapabilityError):
        overlay.quote_history(C650_ID)
    with pytest.raises(MassiveCapabilityError):
        surface.spot_mid_as_of(SPY, overlay.calendar.session_close(S7))
    with pytest.raises(ValueError, match="no bid/ask"):
        overlay.entry_for(SPY, S6, C650_ID)
    assert overlay.median_dollar_volume(SPY, S6) == Decimal("0")
    with pytest.raises(ValueError):
        overlay.median_dollar_volume(SPY, date(2025, 4, 18))


def test_sessions_calendar_and_publication(overlay: MassiveDerivedOverlay) -> None:
    assert overlay.world_sessions() == SESSIONS
    assert overlay.eligible_sessions(SPY) == SESSIONS
    assert overlay.eligible_on(S3) == (SPY,)
    assert overlay.underlyings_ever_eligible() == (SPY,)
    assert overlay.has_file(SPY, S1) and overlay.has_any_file(S3)
    assert not overlay.has_file("QQQ", S3)
    assert overlay.calendar.session_close(S5) == datetime(2025, 4, 11, 20, 0, tzinfo=UTC)
    assert overlay.publication_of(S5) == datetime(2025, 4, 14, 13, 0, tzinfo=UTC)
    assert overlay.publication_of(S5) == publication_instant(S5)  # the shared wall
    with pytest.raises(MassiveOverlayError):
        overlay.publication_of(date(2025, 4, 18))


def test_multi_underlying_and_european_style(tmp_path: Path) -> None:
    spx = fx.contract_result(
        ticker="O:SPXW250411C05900000",
        underlying="I:SPX",
        expiration="2025-04-11",
        strike="5900",
        contract_type="call",
        exercise_style="european",
        primary_exchange="XCBO",
    )
    capture = write_capture(tmp_path / "capture")
    (capture / "masters" / "spx_2025-04-07.json").write_text(
        fx.contracts_payload(results=(spx,), as_of="2025-04-07"), encoding="utf-8"
    )
    overlay = load_derived_surface(capture)
    assert overlay.underlyings == ("I:SPX", SPY)
    spx_id = contract_id_of("I:SPX", date(2025, 4, 11), "C", Decimal("5900"))
    assert overlay.contract(spx_id).exercise_style == "european"
    put_id = contract_id_of("I:SPX", date(2025, 4, 11), "P", Decimal("5900"))
    assert overlay.contract(put_id).exercise_style == "european"  # synthesized cell
    assert overlay.eligible_on(S1) == ("I:SPX", SPY)


# ---- load-time fail-closed refusals ----------------------------------------------


def test_foreign_provider_stamp_refuses(tmp_path: Path) -> None:
    bad = fx.contracts_payload(results=(ROW_C650,), as_of="2025-04-07", provider="someone-else/1")
    capture = write_capture(tmp_path / "capture", master=bad)
    with pytest.raises(MassiveOverlayError, match="provider"):
        load_derived_surface(capture)


def test_unstamped_master_refuses(tmp_path: Path) -> None:
    bare = fx.contracts_payload(
        results=(ROW_C650,), as_of="2025-04-07", provider=None, capture_version=None
    )
    capture = write_capture(tmp_path / "capture", master=bare)
    with pytest.raises(MassiveOverlayError, match="unknown origin"):
        load_derived_surface(capture)


def test_not_authorized_bars_body_refuses(tmp_path: Path) -> None:
    poisoned = fx.bars_payload(ticker=C650_TICKER, results=(), status="NOT_AUTHORIZED")
    capture = write_capture(
        tmp_path / "capture",
        bars=(("bars_c650.json", poisoned),),
    )
    with pytest.raises(MassiveOverlayError, match="NOT_AUTHORIZED"):
        load_derived_surface(capture)


def test_duplicate_master_refuses(tmp_path: Path) -> None:
    capture = write_capture(tmp_path / "capture")
    (capture / "masters" / "spy_again_2025-04-07.json").write_text(MASTER_JSON, encoding="utf-8")
    with pytest.raises(MassiveOverlayError, match="duplicate master"):
        load_derived_surface(capture)


def test_no_masters_dir_refuses(tmp_path: Path) -> None:
    (tmp_path / "empty").mkdir()
    with pytest.raises(MassiveOverlayError, match="no master captures"):
        load_derived_surface(tmp_path / "empty")


# ---- optional capture-manifest integration ----------------------------------------


def _write_manifest(capture: Path) -> None:
    manifest = build_massive_capture_manifest(
        capture,
        capture_version="m4b-capture/1",
        budget_limit=10,
        requests_charged=4,
        client_stats={"requests": 4},
        masters=[
            {
                "underlying": SPY,
                "as_of": "2025-04-07",
                "pages": 1,
                "rows": 5,
                "complete": True,
                "truncated": False,
                "error": None,
                "file": "spy_2025-04-07.json",
            }
        ],
        bars=[name for name, _text in DEFAULT_BARS],
        spot_proxy=json.loads(SPOT_JSON),
        notes=[],
    )
    (capture / "capture_manifest.json").write_text(manifest.model_dump_json(), encoding="utf-8")


def test_manifest_present_loads_and_tampering_refuses(tmp_path: Path) -> None:
    capture = write_capture(tmp_path / "capture")
    _write_manifest(capture)
    overlay = load_derived_surface(capture)
    assert not any(issue.startswith("no capture_manifest") for issue in overlay.issues)
    assert overlay.derived_stats().derived_ok == 5

    (capture / "bars" / "bars_c650.json").write_text(
        BARS_C650.replace('"v": 250', '"v": 999'), encoding="utf-8"
    )
    with pytest.raises(MassiveManifestError):
        load_derived_surface(capture)
