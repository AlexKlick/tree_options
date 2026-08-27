"""G1: the lane-2 vwap PIT adapter (`VwapPitSurface`) over
`MassiveDerivedOverlay` — the wiring that lets the UNMODIFIED
`run_options_backtest` + `FillEngine` execute against the Massive derived
(vwap-bar) lane.

Hermetic: a hand-built vendor-shaped capture through the
`massive_structural_sample` builders (raw JSON TEXT, exact Decimal tokens;
no network, no API key, no client). `artifacts/bars/capture/bars/` was
still EMPTY when this suite was written (the running bars-era capture had
produced no bar files yet), so per the implementer brief the fixtures are
synthetic `MassiveDailyBar`-shaped bars built through the REAL parse path
(`parse_daily_bars`) — bar premiums are the repo's own pricer at iv=0.18,
so every fresh in-band cell derives and the ladder deltas are the analytic
ones.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest

from tests.conftest import REPO_ROOT
from tests.fixtures import massive_structural_sample as fx
from tree_options.backtest.options import run_options_backtest
from tree_options.candidates.filters import CandidateFilter
from tree_options.data.actions import CorporateActionRecord
from tree_options.data.bars import BarRecord
from tree_options.data.cboe_eod import publication_instant
from tree_options.data.massive_overlay import (
    MassiveDerivedOverlay,
    MassiveOverlayError,
    load_derived_surface,
    vwap_quote_event,
)
from tree_options.data.options_pit import NoOptionFileError
from tree_options.data.vwap_pit_surface import VwapPitSurface, load_spot_proxy_v2
from tree_options.options import CandidateAudit, OptionSignal, OptionsStrategyConfig
from tree_options.protocol.loader import load_protocol
from tree_options.schemas.market import VwapQuoteEvent, ZeroVolumeVwapError, conservative_tick
from tree_options.schemas.options import OptionContract
from tree_options.synth_options.generate import GeneratedOptionOverlay, contract_id_of
from tree_options.synth_options.greeks import bs_price
from tree_options.time.sessions import (
    SESSION_TIMEZONE,
    session_close_instant,
    session_open_instant,
    shift_instant,
)

SPY = "SPY"
EXP = date(2025, 6, 20)  # the third Friday of June 2025
# Eleven contiguous weekday sessions: 2025-05-05 .. 2025-05-19 (the last
# one exists so the backtest's next-open look-ahead has calendar headroom
# past end_session).
SESSIONS = (
    date(2025, 5, 5),
    date(2025, 5, 6),
    date(2025, 5, 7),
    date(2025, 5, 8),
    date(2025, 5, 9),
    date(2025, 5, 12),
    date(2025, 5, 13),
    date(2025, 5, 14),
    date(2025, 5, 15),
    date(2025, 5, 16),
    date(2025, 5, 19),
)
S1, S2, S3, S4, S5, S6, S7, S8, S9, S10, S11 = SESSIONS
STRIKES = (590, 600, 610)
LADDER = tuple(Decimal(s) for s in (100, *STRIKES))  # the deep-ITM 100 masters too
IV = 0.18
SPOT_LEVEL = Decimal("600.00")
FLOW_VOLUME = "120"  # >= the protocol's flow_min_session_volume = 100
ZERO_VOLUME_SESSION = S4  # C610 trades nothing this session
NO_BAR_SESSION = S9  # C610 has no bar at all this session

C600_ID = contract_id_of(SPY, EXP, "C", Decimal("600"))
C610_ID = contract_id_of(SPY, EXP, "C", Decimal("610"))
C100_ID = contract_id_of(SPY, EXP, "C", Decimal("100"))


def _t(session: date) -> str:
    """ms epoch of midnight America/New_York — the vendor's bar anchor
    (`massive_options.session_of_epoch_ms` inverts exactly this)."""
    local = datetime.combine(session, time(0), tzinfo=SESSION_TIMEZONE)
    return str(int(local.timestamp() * 1000))


def _premium(call_put: str, strike: int, session: date) -> str:
    """The bar's VWAP token: the repo's own pricer at the fixture iv, so
    every fresh cell derives and the ladder |delta| is the analytic one."""
    price = bs_price(
        spot=float(SPOT_LEVEL),
        strike=float(strike),
        dte_calendar_days=(EXP - session).days,
        iv=IV,
        risk_free=0.03,
        dividend_yield=0.0,
        call_put=call_put,  # type: ignore[arg-type]
    )
    return f"{price:.4f}"


def _bar_rows(
    call_put: str,
    strike: int,
    *,
    volume: str = FLOW_VOLUME,
    skip: frozenset[date] = frozenset(),
    zero_volume_on: frozenset[date] = frozenset(),
) -> tuple[str, ...]:
    rows = []
    for session in SESSIONS:
        if session in skip:
            continue
        vwap = _premium(call_put, strike, session)
        rows.append(
            fx.bar(
                v="0" if session in zero_volume_on else volume,
                t=_t(session),
                vw=vwap,
                o=vwap,
                c=vwap,
                h=str(Decimal(vwap) + Decimal("0.10")),
                low=str(Decimal(vwap) - Decimal("0.10")),
                n="24",
            )
        )
    return tuple(rows)


def _ticker(call_put: str, strike: int) -> str:
    return f"O:SPY{EXP:%y%m%d}{call_put}{strike * 1000:08d}"


def _master_rows() -> tuple[str, ...]:
    rows = []
    for strike in STRIKES:
        for call_put in ("C", "P"):
            rows.append(
                fx.contract_result(
                    ticker=_ticker(call_put, strike),
                    underlying=SPY,
                    expiration=f"{EXP:%Y-%m-%d}",
                    strike=str(strike),
                    contract_type="call" if call_put == "C" else "put",
                )
            )
    # The deep-ITM call: its VWAP sits far under discounted intrinsic, so
    # the pricer refuses to invert it (the marks-fail-closed fixture).
    rows.append(
        fx.contract_result(
            ticker=_ticker("C", 100),
            underlying=SPY,
            expiration=f"{EXP:%Y-%m-%d}",
            strike="100",
            contract_type="call",
        )
    )
    return tuple(rows)


def _write_capture(root: Path) -> Path:
    masters = root / "masters"
    masters.mkdir(parents=True)
    (masters / f"spy_{S1:%Y-%m-%d}.json").write_text(
        fx.contracts_payload(results=_master_rows(), as_of=f"{S1:%Y-%m-%d}"),
        encoding="utf-8",
    )
    bars = root / "bars"
    bars.mkdir()
    for call_put in ("C", "P"):
        for strike in STRIKES:
            skip: frozenset[date] = frozenset()
            zero: frozenset[date] = frozenset()
            if call_put == "C" and strike == 610:
                skip, zero = frozenset({NO_BAR_SESSION}), frozenset({ZERO_VOLUME_SESSION})
            rows = _bar_rows(call_put, strike, skip=skip, zero_volume_on=zero)
            (bars / f"bars_{call_put}{strike}.json").write_text(
                fx.bars_payload(
                    ticker=_ticker(call_put, strike),
                    results_count=str(len(rows)),
                    results=rows,
                ),
                encoding="utf-8",
            )
    # The deep-ITM call's single bar (under intrinsic -> derivation refuses).
    (bars / "bars_c100.json").write_text(
        fx.bars_payload(
            ticker=_ticker("C", 100),
            results_count="1",
            results=(
                fx.bar(
                    v="10",
                    t=_t(S2),
                    vw="5.00",
                    o="5.00",
                    c="5.00",
                    h="5.00",
                    low="5.00",
                    n="2",
                ),
            ),
        ),
        encoding="utf-8",
    )
    (root / "spot_proxy.json").write_text(
        '{"SPY": {' + ", ".join(f'"{s:%Y-%m-%d}": "{SPOT_LEVEL}"' for s in SESSIONS) + "}}",
        encoding="utf-8",
    )
    return root


@pytest.fixture(scope="module")
def capture(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _write_capture(tmp_path_factory.mktemp("vwappit") / "capture")


@pytest.fixture(scope="module")
def overlay(capture: Path) -> MassiveDerivedOverlay:
    # staleness_sessions=10: every fixture session is fresh (the staleness
    # refusal path is pinned separately with a staleness=1 overlay below).
    return load_derived_surface(capture, staleness_sessions=10)


@pytest.fixture(scope="module")
def overlay_stale(capture: Path) -> MassiveDerivedOverlay:
    return load_derived_surface(capture, staleness_sessions=1)


@pytest.fixture(scope="module")
def spot() -> dict[str, dict[date, Decimal]]:
    return {SPY: {s: SPOT_LEVEL for s in SESSIONS}}


@pytest.fixture(scope="module")
def surface(overlay: MassiveDerivedOverlay, spot: dict[str, dict[date, Decimal]]) -> VwapPitSurface:
    return VwapPitSurface(overlay, spot=spot)


@pytest.fixture(scope="module")
def surface_stale(
    overlay_stale: MassiveDerivedOverlay, spot: dict[str, dict[date, Decimal]]
) -> VwapPitSurface:
    return VwapPitSurface(overlay_stale, spot=spot)


@pytest.fixture(scope="module")
def protocol():
    return load_protocol()


def _close(session: date) -> datetime:
    return session_close_instant(session)


def _execution_at(session: date) -> datetime:
    """10:00 ET — the backtest's execution window instant."""
    return shift_instant(session_open_instant(session), 1800)


# ---- identity and the delegated read set -----------------------------------------


def test_identity_and_delegated_reads(surface, overlay) -> None:
    assert surface.snapshot_id == overlay.spec.world_id
    assert surface.overlay is overlay
    assert surface.strike_ladder(SPY, EXP) == LADDER
    assert surface.live_expiries_as_of(SPY, _close(S6)) == (EXP,)
    assert surface.eligible_as_of(S6) == (SPY,)
    contract = surface.contract(C600_ID)
    assert isinstance(contract, OptionContract)
    assert contract.expiration == EXP and contract.strike == Decimal("600")


# ---- the publication wall (T+1 09:00 ET, the overlay's own semantics) ------------


def test_publication_wall_governs_visibility(surface) -> None:
    # a close(S6) decision sees S5's bar — never S6's own
    assert surface.visible_file_session(SPY, _close(S6)) == S5
    # one minute before the 09:00 wall of S6, S5's bar is still future data
    wall = publication_instant(S5)
    assert surface.visible_file_session(SPY, wall - timedelta(minutes=1)) == S4
    assert surface.visible_file_session(SPY, wall) == S5


def test_entry_as_of_reads_the_visible_sessions_cell(surface, overlay) -> None:
    entry = surface.entry_as_of(SPY, _close(S6), C600_ID)
    cell = overlay.derived_quote(C600_ID, S5)
    assert cell.status == "DERIVED" and cell.derived is not None
    assert entry is not None
    # the adapter reuses the overlay's own derivation — never re-derives
    assert entry.abs_delta == cell.derived.abs_delta
    # the DECLARED MARK: both sides carry the previous session's bar VWAP
    assert entry.quote_eod.bid == cell.premium == entry.quote_eod.ask
    assert entry.same_day_volume == cell.volume


def test_no_visible_file_fails_closed(surface) -> None:
    # before the first T+1 publication nothing is visible
    with pytest.raises(NoOptionFileError):
        surface.entry_as_of(SPY, _close(S1), C600_ID)
    with pytest.raises(NoOptionFileError):
        surface.spot_mid_as_of(SPY, _close(S1))
    assert surface.visible_quotes_as_of(C600_ID, _close(S1)) == ()
    assert surface.visible_file_session(SPY, _close(S1)) is None


# ---- the fill stream: exactly the vwap quote kind the engine consumes -------------


def test_visible_quotes_is_the_visible_sessions_bar(surface, overlay) -> None:
    events = surface.visible_quotes_as_of(C600_ID, _execution_at(S6))
    assert len(events) == 1
    event = events[0]
    assert isinstance(event, VwapQuoteEvent)
    cell = overlay.derived_quote(C600_ID, S5)
    assert event.session == S5  # the session immediately before execution
    assert event.received_timestamp == publication_instant(S5)
    assert event.exchange_timestamp == _close(S5)
    assert event.vwap == cell.premium
    assert event.volume == 120
    assert event.trade_count == 24
    assert event.quote_condition == "regular"
    assert event.source == overlay.spec.quote_source


def test_zero_volume_session_is_unfillable(surface, overlay) -> None:
    cell = overlay.derived_quote(C610_ID, ZERO_VOLUME_SESSION)
    assert cell.status == "NOT_EVALUABLE" and "zero-volume" in (cell.reason or "")
    # the conversion itself refuses the zero-volume bar ...
    with pytest.raises(ZeroVolumeVwapError):
        vwap_quote_event(cell)
    # ... so the adapter's stream is EMPTY: no event, no fill, no fabrication
    assert surface.visible_quotes_as_of(C610_ID, _execution_at(S5)) == ()


def test_no_bar_session_yields_no_stream(surface, overlay) -> None:
    cell = overlay.derived_quote(C610_ID, NO_BAR_SESSION)
    assert cell.status == "NOT_EVALUABLE" and cell.reason == "no_bar"
    assert surface.visible_quotes_as_of(C610_ID, _execution_at(S10)) == ()
    # and the mark path fails closed with it
    assert surface.entry_as_of(SPY, _close(S10), C610_ID) is None


def test_stale_visible_session_marks_fail_closed(surface_stale) -> None:
    # staleness=1: every session before the frontier-minus-one withholds
    # its derivation, so a decision whose visible session is stale gets
    # neither a mark nor a delta — never a carried-forward VWAP
    assert surface_stale.entry_as_of(SPY, _close(S3), C600_ID) is None


def test_refused_derivation_marks_fail_closed(surface) -> None:
    # the deep-ITM call's bar exists but its VWAP is under intrinsic: the
    # pricer refuses, so there is no delta and no mark — never a guess
    cell = surface.overlay.derived_quote(C100_ID, S2)
    assert cell.status == "NOT_EVALUABLE" and (cell.reason or "").startswith("refused:")
    assert surface.entry_as_of(SPY, _close(S3), C100_ID) is None


# ---- spot proxy: DECLARED INPUT, same availability discipline ---------------------


def test_spot_mid_from_declared_proxy(overlay) -> None:
    per_session = {SPY: {S4: Decimal("601.25"), S5: Decimal("602.50")}}
    adapter = VwapPitSurface(overlay, spot=per_session)
    assert adapter.spot_mid_as_of(SPY, _close(S6)) == Decimal("602.50")  # sees S5
    assert adapter.spot_mid_as_of(SPY, _close(S5)) == Decimal("601.25")  # sees S4
    with pytest.raises(NoOptionFileError):
        adapter.spot_mid_as_of(SPY, _close(S4))  # S3 has no declared spot


def test_spot_missing_fails_closed(overlay) -> None:
    no_proxy = VwapPitSurface(overlay)
    with pytest.raises(NoOptionFileError):
        no_proxy.spot_mid_as_of(SPY, _close(S6))


def test_flat_spot_proxy_form_covers_every_session(overlay, tmp_path: Path) -> None:
    from tree_options.data.massive_overlay import load_spot_proxy

    path = tmp_path / "spot_proxy.json"
    path.write_text('{"SPY": "600.00"}', encoding="utf-8")
    adapter = VwapPitSurface(overlay, spot=load_spot_proxy(path))
    assert adapter.spot_mid_as_of(SPY, _close(S6)) == Decimal("600.00")
    assert adapter.spot_mid_as_of(SPY, _close(S10)) == Decimal("600.00")


def test_load_spot_proxy_refuses_bad_input(tmp_path: Path) -> None:
    from tree_options.data.massive_overlay import MassiveOverlayError, load_spot_proxy

    path = tmp_path / "spot_proxy.json"
    path.write_text('{"SPY": {"2025-05-05": "-1.00"}}', encoding="utf-8")
    with pytest.raises(MassiveOverlayError, match="not positive"):
        load_spot_proxy(path)


# ---- (R5-P2, Codex round 5) ordinary-spot finiteness: file + constructor ------------


@pytest.mark.parametrize("token", ["Infinity", "NaN"])
def test_the_spot_proxy_loader_refuses_non_finite_spots(tmp_path: Path, token: str) -> None:
    """(R5-P2) `_dec` accepts "Infinity" as a Decimal and `_load_spot`'s only
    gate was `spot <= 0` — POSITIVE infinity passes it. An infinite spot then
    flows into `intrinsic_value` and the election policy
    (`should_elect_exercise`): any finite bid is below Infinity * 0.98, so
    malformed input forces an early-exercise election instead of refusing.
    Both tokens must refuse with the loader's own error shape, naming the
    underlying, the session, and the token (RED before R5-P2: Infinity
    LOADED; "NaN" escaped as a raw decimal.InvalidOperation out of the
    `<= 0` comparison)."""
    from tree_options.data.massive_overlay import MassiveOverlayError, load_spot_proxy

    path = tmp_path / "spot_proxy.json"
    path.write_text(f'{{"SPY": {{"2025-05-05": "{token}"}}}}', encoding="utf-8")
    with pytest.raises(MassiveOverlayError, match="finite"):
        load_spot_proxy(path)


@pytest.mark.parametrize("value", [Decimal("Infinity"), Decimal("NaN")])
def test_the_spot_constructor_refuses_non_finite_spots(overlay, value) -> None:
    """(R5-P2 — the constructor horn, the election probe's entry) The
    adapter's constructor copied injected ordinary `spot` mappings without
    validation, so an infinite spot reached `spot_mid_as_of` unchanged and
    from there the intrinsic -> election chain (a forced early exercise on
    malformed input). The copy loop now applies the loader's own discipline
    — ONE shared row check: exact decimal, FINITE, positive — and refuses
    naming the underlying, the session, and the token (RED before R5-P2:
    both were accepted silently; an infinite spot could reach
    `exercise.py`)."""
    with pytest.raises(MassiveOverlayError, match="finite"):
        VwapPitSurface(overlay, spot={SPY: {S1: value}})


def test_the_spot_constructor_refuses_inexact_and_nonpositive_spots(overlay) -> None:
    """(R5-P2, the rest of the shared discipline) The ordinary-spot copy
    loop refuses exactly what the loader refuses: a float close (a binary
    approximation of a price) and a non-positive close — an injected
    mapping can never carry what a file cannot."""
    with pytest.raises(MassiveOverlayError, match="exact decimal"):
        VwapPitSurface(overlay, spot={SPY: {S1: 600.5}})
    with pytest.raises(MassiveOverlayError, match="not positive"):
        VwapPitSurface(overlay, spot={SPY: {S1: Decimal("-600.00")}})


def test_the_spot_constructor_loads_a_valid_mapping_exactly(overlay, tmp_path) -> None:
    """(R5-P2, the parity direction that must not move) A valid ordinary
    mapping stores EXACTLY as today — same keys, same Decimals — and the
    loader's own output feeds the constructor identically (byte identity:
    the shared check validates, it never transforms)."""
    from tree_options.data.massive_overlay import load_spot_proxy
    from tree_options.data.vwap_pit_surface import SPOT_SENTINEL_SESSION

    path = tmp_path / "spot_proxy.json"
    path.write_text('{"SPY": {"2025-05-05": "600.00", "2025-05-06": "601.25"}}', encoding="utf-8")
    parsed = load_spot_proxy(path)
    assert VwapPitSurface(overlay, spot=parsed)._spot == parsed
    # the flat form's date.min sentinel is an ordinary key to the copy loop
    hand = {SPY: {S1: SPOT_LEVEL, SPOT_SENTINEL_SESSION: Decimal("599.00")}}
    assert VwapPitSurface(overlay, spot=hand)._spot == hand


def test_coverage_era_spot_proxy_is_a_loadable_declared_input() -> None:
    """The brief's DECLARED INPUT (read-only): the coverage-era spot proxy
    parses under the same discipline when present on the host."""
    from tree_options.data.massive_overlay import load_spot_proxy

    path = REPO_ROOT / "artifacts" / "m4b-coverage-era" / "spot_proxy.json"
    if not path.is_file():
        pytest.skip("coverage-era spot proxy not present on this host")
    proxy = load_spot_proxy(path)
    assert proxy
    for sessions in proxy.values():
        assert sessions
        assert all(v > 0 for v in sessions.values())


# ---- candidate snapshots: build_option_candidate_inputs + derived cells -----------


def _protocol_volume_flow(surface, protocol) -> CandidateFilter:
    return CandidateFilter.from_protocol_volume_flow(surface.overlay.calendar, protocol)


def _zeroed_liquidity_volume_flow(surface, protocol) -> CandidateFilter:
    """The strategy-suite convention (test_options_strategy): zeroing the
    underlying-liquidity minimum keeps the small fixture world usable, and
    (w2) turning the earnings rule OFF adopts the ruled 0.2.2 shape — the
    adapter's honest `spans_earnings=None` makes the still-current 0.2.1
    rule NOT_EVALUABLE, so a fixture that kept the rule on would trade
    zero and exercise none of the adapter's mechanics. Every other knob
    stays at the protocol's ratified values; both deviations are pinned
    against the protocol-built filter in the rows test above."""
    d = protocol.option_candidate_defaults
    lf = d.liquidity_volume_flow
    assert lf is not None
    return CandidateFilter(
        surface.overlay.calendar,
        dte_min=d.dte_min,
        dte_max=d.dte_max,
        abs_delta_min=d.abs_delta_min,
        abs_delta_max=d.abs_delta_max,
        standard_deliverable_only=d.standard_deliverable_only,
        min_open_interest=d.min_open_interest,
        min_same_day_volume=d.min_same_day_volume,
        volume_only_if_already_available=d.volume_only_if_already_available,
        max_spread_fraction_of_midpoint=d.max_spread_fraction_of_midpoint,
        min_underlying_20d_median_dollar_volume=Decimal("0"),
        exclude_earnings_spanning_hold=False,
        liquidity_regime="volume_flow",
        flow_min_session_volume=lf.flow_min_session_volume,
        accepted_delta_provenance=lf.abs_delta_provenance_accepted,
    )


def test_candidate_snapshot_volume_flow_rows(surface, protocol) -> None:
    snap = surface.candidate_snapshot(surface.contract(C600_ID), S6)
    decision = _protocol_volume_flow(surface, protocol).evaluate(snap)
    by_rule = {r.rule: r for r in decision.results}
    # the ratified drops, WITH disclosure
    assert by_rule["open_interest"].status == "NOT_APPLICABLE"
    assert "no open interest" in by_rule["open_interest"].detail
    assert by_rule["spread"].status == "NOT_APPLICABLE"
    assert "no two-sided market" in by_rule["spread"].detail
    # the derived |delta| passes under its ratified provenance stamp
    assert by_rule["delta"].status == "PASS"
    assert "model-derived-from-vwap" in by_rule["delta"].detail
    assert by_rule["session_volume_flow"].status == "PASS"  # volume 120 >= 100
    # the lane's honest audit row: the overlay's declared 0 dollar-volume
    # sentinel sits under the protocol's 50M minimum, so the protocol-built
    # filter fails the rule (pinned: an owner ruling is needed to lift it)
    assert by_rule["underlying_liquidity"].status == "FAIL"
    assert not decision.accepted


def test_candidate_snapshot_accepted_under_zeroed_liquidity_min(surface, protocol) -> None:
    snap = surface.candidate_snapshot(surface.contract(C600_ID), S6)
    decision = _zeroed_liquidity_volume_flow(surface, protocol).evaluate(snap)
    assert decision.accepted, decision.results
    assert snap.abs_delta is not None
    assert snap.abs_delta.provenance == "model-derived-from-vwap"
    assert snap.same_day_volume is not None and snap.same_day_volume.value == 120
    assert snap.open_interest is None and snap.bid is None and snap.ask is None


def test_candidate_snapshot_earnings_is_the_honest_no_evidence_encoding(surface, protocol) -> None:
    """(w2, theory-panel P0-2 ruling (ii), owner ruled 2026-08-26):
    `AsOf(value=False)` LAUNDERS — the rule would read a vendor-stamped
    PASS "no spanning earnings" that no source supports, indistinguishable
    from a funded events feed (the lane-1 precedent is honest ONLY for
    synthetic worlds, which contain no earnings events by construction).
    The honest encoding on this lane is `spans_earnings=None`: under the
    still-current 0.2.1 protocol (`exclude_earnings_spanning_hold: true`)
    the rule answers NOT_EVALUABLE "missing" and the candidate is REFUSED —
    lane 2 trades zero until the 0.2.2 packet turns the rule off."""
    snap = surface.candidate_snapshot(surface.contract(C600_ID), S6)
    assert snap.spans_earnings is None
    decision = _protocol_volume_flow(surface, protocol).evaluate(snap)
    by_rule = {r.rule: r for r in decision.results}
    assert by_rule["earnings_span"].status == "NOT_EVALUABLE"
    assert by_rule["earnings_span"].detail == "missing"
    # NOT_EVALUABLE blocks acceptance: zero trades under 0.2.1, pinned
    assert not decision.accepted


def test_earnings_rule_off_answers_not_applicable_without_reading_the_value(
    surface, protocol
) -> None:
    """(w2) The 0.2.2 destination (`exclude_earnings_spanning_hold: false`,
    verdict §2 P0-2(ii)): the filter answers NOT_APPLICABLE "filter
    disabled" BEFORE reading the snapshot value, so the None encoding
    stops mattering the moment the rule is off — the artifact self-
    discloses instead of laundering a PASS."""
    d = protocol.option_candidate_defaults
    lf = d.liquidity_volume_flow
    assert lf is not None
    rule_off = CandidateFilter(
        surface.overlay.calendar,
        dte_min=d.dte_min,
        dte_max=d.dte_max,
        abs_delta_min=d.abs_delta_min,
        abs_delta_max=d.abs_delta_max,
        standard_deliverable_only=d.standard_deliverable_only,
        min_open_interest=d.min_open_interest,
        min_same_day_volume=d.min_same_day_volume,
        volume_only_if_already_available=d.volume_only_if_already_available,
        max_spread_fraction_of_midpoint=d.max_spread_fraction_of_midpoint,
        min_underlying_20d_median_dollar_volume=Decimal("0"),
        exclude_earnings_spanning_hold=False,  # the 0.2.2 shape
        liquidity_regime="volume_flow",
        flow_min_session_volume=lf.flow_min_session_volume,
        accepted_delta_provenance=lf.abs_delta_provenance_accepted,
    )
    snap = surface.candidate_snapshot(surface.contract(C600_ID), S6)
    assert snap.spans_earnings is None  # the value the rule never reads
    by_rule = {r.rule: r for r in rule_off.evaluate(snap).results}
    assert by_rule["earnings_span"].status == "NOT_APPLICABLE"
    assert by_rule["earnings_span"].detail == "filter disabled"


def test_candidate_snapshot_none_inputs_on_underrived_cells(
    surface,
) -> None:  # zero-volume visible session: the bar's volume 0 is a SUPPLIED fact
    # (the session genuinely traded nothing) but the derivation is withheld
    zero = surface.candidate_snapshot(surface.contract(C610_ID), S5)  # sees S4
    assert zero.abs_delta is None
    assert zero.same_day_volume is not None and zero.same_day_volume.value == 0
    # no bar at all: every input is None — never a guess
    nobar = surface.candidate_snapshot(surface.contract(C610_ID), S10)  # sees S9
    assert nobar.abs_delta is None
    assert nobar.same_day_volume is None


def test_candidate_snapshot_fails_closed_before_first_publication(surface) -> None:
    with pytest.raises(NoOptionFileError):
        surface.candidate_snapshot(surface.contract(C600_ID), S1)


# ---- participation cap: the engine's cumulative per-(contract, session) bound ------


def test_participation_cap_is_cumulative_per_contract_session(surface) -> None:
    from tree_options.guards.fills import FillEngine, FillRejection
    from tree_options.schemas.trading import Order

    engine = FillEngine(surface.overlay.calendar, max_quote_age_seconds=7200)
    contract = surface.contract(C600_ID)

    def _order(seq: int, quantity: int) -> Order:
        return Order(
            order_id=f"VP-{seq}",
            contract_id=C600_ID,
            side="buy",
            intent="open_long",
            quantity=quantity,
            decision_at=_close(S5),
            decision_session=S5,
        )

    # execution S6 10:00 fills against S5's bar: volume 120 caps the session
    first = engine.execute(
        _order(1, 200),
        surface.visible_quotes_as_of(C600_ID, _execution_at(S6)),
        contract,
        execution_session=S6,
        execution_at=_execution_at(S6),
    )
    assert first.quantity == 120
    assert first.price == conservative_tick(
        surface.overlay.derived_quote(C600_ID, S5).premium, "buy"
    )
    # a second order against the SAME bar cannot mint the session's volume twice
    with pytest.raises(FillRejection) as exc:
        engine.execute(
            _order(2, 50),
            surface.visible_quotes_as_of(C600_ID, _execution_at(S6)),
            contract,
            execution_session=S6,
            execution_at=_execution_at(S6),
        )
    assert exc.value.code == "NO_LIQUIDITY"


def test_each_bar_session_carries_its_own_participation_capacity(surface) -> None:
    """(G1) The participation ledger is keyed per (contract, BAR SESSION):
    the next execution session consumes the NEXT session's bar, whose own
    observed volume is its own capacity — an earlier bar's consumption never
    depletes a later bar. The test above pins the cumulative bound WITHIN
    one bar session; this pins the key's second dimension (a per-contract
    key would starve every session after the first)."""
    from tree_options.guards.fills import FillEngine
    from tree_options.schemas.trading import Order

    engine = FillEngine(surface.overlay.calendar, max_quote_age_seconds=7200)
    contract = surface.contract(C600_ID)

    def _order(seq: int, quantity: int, decision: date) -> Order:
        return Order(
            order_id=f"VX-{seq}",
            contract_id=C600_ID,
            side="buy",
            intent="open_long",
            quantity=quantity,
            decision_at=_close(decision),
            decision_session=decision,
        )

    # execution S6 fills against S5's bar (volume 120): capacity 120
    first = engine.execute(
        _order(1, 200, S5),
        surface.visible_quotes_as_of(C600_ID, _execution_at(S6)),
        contract,
        execution_session=S6,
        execution_at=_execution_at(S6),
    )
    assert first.quantity == 120
    # execution S7 fills against S6's bar — a DIFFERENT bar session with its
    # own 120 contracts of observed volume: the capacity resets, and the
    # 120 already consumed against bar(S5) does not count against bar(S6)
    second = engine.execute(
        _order(2, 200, S6),
        surface.visible_quotes_as_of(C600_ID, _execution_at(S7)),
        contract,
        execution_session=S7,
        execution_at=_execution_at(S7),
    )
    assert second.quantity == 120


# ---- the optional dollar-volume source (spot_proxy_v2, P0-1(b) seam) ----------------
#
# The ruled fallback chain (theory-panel §2 P0-1, "both, staged"): the preferred
# leg funds real underlying volume via a ~29-call equity-aggregates recapture that
# lands POST-CLOSEOUT as a `spot_proxy_v2` file beside the spot proxy; until it
# lands, the declared Decimal("0") sentinel stands and the $50M term honestly FAILs.
# The fixture needs >= 20 contiguous calendar sessions before the visible one, which
# the 11-session module fixture cannot carry — hence a dedicated capture here.


V2_FIRST = date(2025, 4, 14)  # a Monday
V2_SESSIONS = tuple(
    V2_FIRST + timedelta(days=offset)
    for offset in range(32)  # 4 weeks + the Mon/Tue that complete 22 weekday sessions
    if (V2_FIRST + timedelta(days=offset)).weekday() < 5
)[:22]
assert len(V2_SESSIONS) == 22 and V2_SESSIONS[-1] == date(2025, 5, 13)
V2_DECISION = V2_SESSIONS[-1]  # close(t) sees t-1's cell: the 21st session
V2_VISIBLE = V2_SESSIONS[-2]
V2_CLOSE = "600.00"
V2_VOLUME = 80_000_000  # a realistic mega-cap session: ~$48B of notional
V2_WINDOW = V2_SESSIONS[-21:-1]  # the 20 contiguous sessions ending at the visible one


def _write_v2_capture(root: Path, *, skip: frozenset[date] = frozenset()) -> Path:
    masters = root / "masters"
    masters.mkdir(parents=True)
    (masters / f"spy_{V2_SESSIONS[0]:%Y-%m-%d}.json").write_text(
        fx.contracts_payload(
            results=(
                fx.contract_result(
                    ticker=_ticker("C", 600),
                    underlying=SPY,
                    expiration=f"{EXP:%Y-%m-%d}",
                    strike="600",
                    contract_type="call",
                ),
            ),
            as_of=f"{V2_SESSIONS[0]:%Y-%m-%d}",
        ),
        encoding="utf-8",
    )
    bars = root / "bars"
    bars.mkdir()
    captured = tuple(session for session in V2_SESSIONS if session not in skip)
    rows = tuple(
        fx.bar(
            v=FLOW_VOLUME,
            t=_t(session),
            vw=_premium("C", 600, session),
            o=_premium("C", 600, session),
            c=_premium("C", 600, session),
            h=str(Decimal(_premium("C", 600, session)) + Decimal("0.10")),
            low=str(Decimal(_premium("C", 600, session)) - Decimal("0.10")),
            n="24",
        )
        for session in captured
    )
    (bars / "bars_c600.json").write_text(
        fx.bars_payload(ticker=_ticker("C", 600), results_count=str(len(rows)), results=rows),
        encoding="utf-8",
    )
    (root / "spot_proxy.json").write_text(
        '{"SPY": {' + ", ".join(f'"{s:%Y-%m-%d}": "{SPOT_LEVEL}"' for s in captured) + "}}",
        encoding="utf-8",
    )
    return root


@pytest.fixture(scope="module")
def v2_capture(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _write_v2_capture(tmp_path_factory.mktemp("vwappitv2") / "capture")


def _v2_map(
    sessions: tuple[date, ...] = V2_SESSIONS,
    *,
    close: str = V2_CLOSE,
    volume: int = V2_VOLUME,
) -> dict[str, dict[date, tuple[Decimal, int]]]:
    return {SPY: {session: (Decimal(close), volume) for session in sessions}}


def _v2_json(map_: dict[str, dict[date, tuple[Decimal, int]]]) -> str:
    return json.dumps(
        {
            ticker: {
                f"{session:%Y-%m-%d}": {"close": f"{close}", "volume": volume}
                for session, (close, volume) in sessions.items()
            }
            for ticker, sessions in map_.items()
        }
    )


def test_load_spot_proxy_v2_parses_the_declared_shape(tmp_path: Path) -> None:
    path = tmp_path / "spot_proxy_v2.json"
    path.write_text(_v2_json(_v2_map(V2_SESSIONS[:3])), encoding="utf-8")
    parsed = load_spot_proxy_v2(path)
    assert set(parsed) == {SPY}
    assert parsed[SPY] == {session: (Decimal(V2_CLOSE), V2_VOLUME) for session in V2_SESSIONS[:3]}
    # a zero-volume session is a real observation, not a gap: it parses
    quiet = tmp_path / "quiet.json"
    quiet.write_text('{"SPY": {"2025-04-14": {"close": "600.00", "volume": 0}}}', encoding="utf-8")
    assert load_spot_proxy_v2(quiet)[SPY][V2_SESSIONS[0]] == (Decimal("600.00"), 0)


@pytest.mark.parametrize(
    ("body", "complaint"),
    [
        ('{"SPY": {"2025-04-14": {"close": "600.00"}}}', "close"),
        ('{"SPY": {"2025-04-14": {"close": "600.00", "volume": -1}}}', "volume"),
        ('{"SPY": {"2025-04-14": {"close": "600.00", "volume": true}}}', "volume"),
        ('{"SPY": {"2025-04-14": {"close": "600.00", "volume": "80000000"}}}', "volume"),
        ('{"SPY": {"2025-04-14": {"close": 600.0, "volume": 1}}}', "close"),
        ('{"SPY": {"2025-04-14": {"close": "-600.00", "volume": 1}}}', "positive"),
        ('{"SPY": {"2025-04-14": {"close": "Infinity", "volume": 1}}}', "finite"),
        ('{"SPY": {"2025-04-14": {"close": "NaN", "volume": 1}}}', "finite"),
        ('{"SPY": {"2025-04-14": {"close": "600.00", "volume": 1, "extra": 1}}}', "key"),
        ('{"SPY": {"not-a-date": {"close": "600.00", "volume": 1}}}', "ISO date"),
        ("[]", "object"),
    ],
)
def test_load_spot_proxy_v2_refuses_everything_else(
    tmp_path: Path, body: str, complaint: str
) -> None:
    path = tmp_path / "spot_proxy_v2.json"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(MassiveOverlayError, match=complaint):
        load_spot_proxy_v2(path)


@pytest.mark.parametrize("token", ["Infinity", "NaN"])
def test_non_finite_closes_refuse_cleanly(tmp_path: Path, token: str) -> None:
    """(P2-5, Codex round 1) `Decimal(close.strip())` accepted "Infinity" and
    the only gate was `<= 0` — an infinity close is POSITIVE-looking, so it
    loaded, and an infinity median flipped the liquidity rule to PASS
    (fail-open). "NaN" parsed too, but its `<= 0` comparison raised
    InvalidOperation — an uncontrolled exception, not the loader's refusal.
    Both must refuse with the loader's own error shape (RED before P2-5:
    Infinity LOADED, NaN raised decimal.InvalidOperation)."""
    path = tmp_path / "spot_proxy_v2.json"
    path.write_text(
        f'{{"SPY": {{"2025-04-14": {{"close": "{token}", "volume": 1}}}}}}', encoding="utf-8"
    )
    with pytest.raises(MassiveOverlayError, match="finite"):
        load_spot_proxy_v2(path)


# ---- (R4-P2, Codex round 4) constructor validation parity for spot_v2 ---------------


@pytest.mark.parametrize(
    ("row", "complaint"),
    [
        ((Decimal("Infinity"), 1), "finite"),
        ((Decimal("-1"), 1), "positive"),
        ((Decimal("600.00"), True), "volume"),
        ((Decimal("600.00"), -1), "volume"),
        ((600.00, 1), "exact decimal"),
        ((True, 1), "exact decimal"),
        (Decimal("600.00"), "exactly the keys"),
    ],
)
def test_the_spot_v2_constructor_refuses_invalid_rows(overlay, row, complaint) -> None:
    """(R4-P2) The file loader rejected non-finite closes but the constructor
    copied the injected mapping WITHOUT validation (a bare dict(sessions)):
    the median path stamped an infinite vendor value and the liquidity
    comparison fell through to PASS — fail-open. The constructor now refuses
    any row failing the loader's own discipline, with the loader's error
    shape naming the underlying and the session (RED before R4-P2: every
    case below was accepted silently — the Codex round-4 probe is the
    Infinity one)."""
    session = V2_SESSIONS[0]
    with pytest.raises(MassiveOverlayError, match=complaint):
        VwapPitSurface(overlay, spot_v2={SPY: {session: row}})


def test_the_spot_v2_constructor_loads_a_valid_mapping_exactly(overlay, tmp_path) -> None:
    """(R4-P2, the parity direction that must not move) A valid mapping
    stores EXACTLY as today — same keys, same (Decimal, int) pairs — and
    the loader's own output feeds the constructor identically (byte
    identity: the shared helper validates, it never transforms)."""
    path = tmp_path / "spot_proxy_v2.json"
    mapping = _v2_map(V2_SESSIONS[:4])
    path.write_text(_v2_json(mapping), encoding="utf-8")
    parsed = load_spot_proxy_v2(path)
    assert VwapPitSurface(overlay, spot_v2=parsed)._spot_v2 == parsed
    assert VwapPitSurface(overlay, spot_v2=mapping)._spot_v2 == parsed


def test_dollar_volume_source_passes_the_50m_rule(v2_capture, protocol) -> None:
    """(w3, P0-1(b) preferred leg) With a declared spot_proxy_v2 the
    underlying-liquidity term is honestly EVALUABLE: the 20-session median of
    close*volume at realistic mega-cap magnitude clears the protocol's $50M
    minimum by three orders, under vendor provenance at the T+1 wall of the
    window's last session. (P1-2: the exchange calendar threaded is the
    repo-adopted NYSE fixture; the fixture span 2025-04-14..05-13 carries no
    exchange holiday, so the exchange window IS the fixture's 20 weekdays.)"""
    overlay = load_derived_surface(v2_capture, staleness_sessions=10)
    adapter = VwapPitSurface(
        overlay,
        spot={SPY: {s: SPOT_LEVEL for s in V2_SESSIONS}},
        spot_v2=_v2_map(),
        exchange_calendar=_exchange_calendar(),
    )
    snap = adapter.candidate_snapshot(adapter.contract(C600_ID), V2_DECISION)
    stamp = snap.underlying_20d_median_dollar_volume
    assert stamp is not None
    assert stamp.value == Decimal("48000000000")  # 20 x an identical 48e9 day
    assert stamp.provenance == "vendor"
    assert stamp.available_at == publication_instant(V2_VISIBLE)
    by_rule = {
        r.rule: r
        for r in CandidateFilter.from_protocol_volume_flow(overlay.calendar, protocol)
        .evaluate(snap)
        .results
    }
    assert by_rule["underlying_liquidity"].status == "PASS"


def test_without_the_source_the_sentinel_fail_stands(v2_capture, protocol) -> None:
    """(w3, P0-1 declared fallback) Same capture, NO spot_proxy_v2: the
    overlay's Decimal("0") sentinel answers and the rule FAILs "below min" —
    the term honestly fails until the post-closeout recapture lands."""
    overlay = load_derived_surface(v2_capture, staleness_sessions=10)
    adapter = VwapPitSurface(overlay, spot={SPY: {s: SPOT_LEVEL for s in V2_SESSIONS}})
    snap = adapter.candidate_snapshot(adapter.contract(C600_ID), V2_DECISION)
    stamp = snap.underlying_20d_median_dollar_volume
    assert stamp is not None and stamp.value == Decimal("0")
    by_rule = {
        r.rule: r
        for r in CandidateFilter.from_protocol_volume_flow(overlay.calendar, protocol)
        .evaluate(snap)
        .results
    }
    assert by_rule["underlying_liquidity"].status == "FAIL"
    assert by_rule["underlying_liquidity"].detail == "below min"


def test_dollar_volume_requires_a_contiguous_20_session_window(v2_capture, protocol) -> None:
    """Fail-closed on availability: a v2 map with a HOLE inside the trailing
    20 EXCHANGE sessions, or one that does not reach 20 sessions back, is not
    a 20d median — the adapter falls back to the declared sentinel rather
    than averaging over whatever happened to be captured."""
    overlay = load_derived_surface(v2_capture, staleness_sessions=10)
    spot = {SPY: {s: SPOT_LEVEL for s in V2_SESSIONS}}
    # a hole: drop the 10th session of the required window
    holed = {SPY: {s: v for s, v in _v2_map()[SPY].items() if s != V2_WINDOW[9]}}
    short = _v2_map(V2_SESSIONS[:5])  # only 5 sessions of history
    for source in (holed, short):
        adapter = VwapPitSurface(
            overlay, spot=spot, spot_v2=source, exchange_calendar=_exchange_calendar()
        )
        snap = adapter.candidate_snapshot(adapter.contract(C600_ID), V2_DECISION)
        stamp = snap.underlying_20d_median_dollar_volume
        assert stamp is not None and stamp.value == Decimal("0"), source.keys()
        by_rule = {
            r.rule: r
            for r in CandidateFilter.from_protocol_volume_flow(overlay.calendar, protocol)
            .evaluate(snap)
            .results
        }
        assert by_rule["underlying_liquidity"].status == "FAIL"


def _dropped_term_filter(adapter: VwapPitSurface, protocol) -> CandidateFilter:
    """The protocol's volume-flow regime with the ruled 0.2.2
    `dropped_no_equity_aggregates` disposition — the filter side of the
    term the adapter must MIRROR (a filter that drops while the snapshot
    supplies is regime-incoherent by construction)."""
    d = protocol.option_candidate_defaults
    lf = d.liquidity_volume_flow
    assert lf is not None
    return CandidateFilter(
        adapter.overlay.calendar,
        dte_min=d.dte_min,
        dte_max=d.dte_max,
        abs_delta_min=d.abs_delta_min,
        abs_delta_max=d.abs_delta_max,
        standard_deliverable_only=d.standard_deliverable_only,
        min_open_interest=d.min_open_interest,
        min_same_day_volume=d.min_same_day_volume,
        volume_only_if_already_available=d.volume_only_if_already_available,
        max_spread_fraction_of_midpoint=d.max_spread_fraction_of_midpoint,
        min_underlying_20d_median_dollar_volume=d.min_underlying_20d_median_dollar_volume,
        exclude_earnings_spanning_hold=False,  # the ruled 0.2.2 shape
        liquidity_regime="volume_flow",
        flow_min_session_volume=lf.flow_min_session_volume,
        underlying_liquidity_term="dropped_no_equity_aggregates",
        accepted_delta_provenance=lf.abs_delta_provenance_accepted,
    )


def test_a_dropped_liquidity_term_supplies_absence_through_the_adapter(
    overlay, spot, protocol
) -> None:
    """(R2-P2-d, Codex round 2) The surface ALWAYS supplied a dollar-volume
    `AsOf` — the v2 median or the overlay sentinel — never None, so the
    0.2.2 pre-draft's dropped branch (NOT_APPLICABLE "dropped: no
    equity-aggregates dollar volume") was UNREACHABLE through the shipped
    adapter: any supplied value, sentinel included, is judged regime-
    incoherent -> NOT_EVALUABLE, and the pre-drafted packet would not do
    what the branch claims. With the DECLARED term threaded, a dropped
    regime supplies ABSENCE — the regime's premise is NO source, and the
    sentinel must never be minted into it (RED before R2-P2-d: the filter
    answered NOT_EVALUABLE 'regime incoherent' on the sentinel)."""
    adapter = VwapPitSurface(
        overlay,
        spot=spot,
        underlying_liquidity_term="dropped_no_equity_aggregates",
    )
    snap = adapter.candidate_snapshot(adapter.contract(C600_ID), S6)
    assert snap.underlying_20d_median_dollar_volume is None  # ABSENCE, never a value
    by_rule = {r.rule: r for r in _dropped_term_filter(adapter, protocol).evaluate(snap).results}
    assert by_rule["underlying_liquidity"].status == "NOT_APPLICABLE"
    assert by_rule["underlying_liquidity"].detail == (
        "dropped: no equity-aggregates dollar volume on this tier"
    )
    # declared evaluated (the DEFAULT): exactly today's chain — the overlay's
    # declared Decimal("0") sentinel under the protocol's 50M minimum FAILs
    evaluated = VwapPitSurface(overlay, spot=spot)
    evaluated_snap = evaluated.candidate_snapshot(evaluated.contract(C600_ID), S6)
    stamp = evaluated_snap.underlying_20d_median_dollar_volume
    assert stamp is not None and stamp.value == Decimal("0")


def test_an_unknown_liquidity_term_refuses_at_the_constructor(overlay, spot) -> None:
    """(R2-P2-d) The declared disposition is a two-token Literal, mirrored
    from the filter's own constructor gate: an unknown token refuses here
    exactly as it refuses there — never silently behaves as 'evaluated'."""
    with pytest.raises(ValueError, match="unknown underlying_liquidity_term"):
        VwapPitSurface(overlay, spot=spot, underlying_liquidity_term="sometimes")


def test_dollar_volume_median_is_exact_over_distinct_values(v2_capture) -> None:
    """(w3) The PASS path's statistic is the exact MEDIAN of the trailing 20
    EXCHANGE sessions' close*volume — never the mean, and never a median over
    more history than the declared window. The 50M fixture repeats one
    identical daily value (median == mean == any-window median there), so
    this seam needs DISTINCT values with a spike, chosen so the three
    statistics separate:

        trailing 20 sorted = [10e9 x10, 30e9 x9, 1000e9] -> median 20e9
        trailing 20 mean                                      -> 68.5e9
        all captured history (22 sessions) sorted             -> 11th = 30e9

    (P1-2) The volumes are keyed by POSITION INSIDE the exchange window:
    the weekday-only fixture carries 2025-04-18 (Good Friday — never an
    NYSE session), so the overlay-ordinal layout and the exchange window
    differ, and the session the window actually contains (2025-04-14) is
    the one the old layout left outside. The never-windowed sessions
    (Good Friday itself and the decision session) carry distinct values so
    a wrongly-widened window changes the median."""
    overlay = load_derived_surface(v2_capture, staleness_sessions=10)
    exchange = _exchange_calendar()
    end = exchange.ordinal(V2_VISIBLE)
    window = exchange.sessions()[end - 19 : end + 1]
    assert len(window) == 20 and window[-1] == V2_VISIBLE
    good_friday = date(2025, 4, 18)
    assert good_friday in V2_SESSIONS and not exchange.is_session(good_friday)
    volume_by_session: dict[date, int] = {
        session: (100_000_000 if position < 10 else 300_000_000)
        for position, session in enumerate(window)
    }
    volume_by_session[V2_VISIBLE] = 10_000_000_000  # the visible session's 1000e9 spike
    volume_by_session[good_friday] = 400_000_000  # never an exchange session
    volume_by_session[V2_DECISION] = 500_000_000  # the decision session: never windowed
    assert set(volume_by_session) == set(V2_SESSIONS)
    source = {SPY: {s: (Decimal("100.00"), volume_by_session[s]) for s in V2_SESSIONS}}
    adapter = VwapPitSurface(
        overlay,
        spot={SPY: {s: SPOT_LEVEL for s in V2_SESSIONS}},
        spot_v2=source,
        exchange_calendar=exchange,
    )
    snap = adapter.candidate_snapshot(adapter.contract(C600_ID), V2_DECISION)
    stamp = snap.underlying_20d_median_dollar_volume
    assert stamp is not None
    # the exact median of the 20 trailing exchange sessions: the average of
    # the 10th and 11th sorted values, (10e9 + 30e9) / 2 — an independent
    # oracle, not a re-derivation through the adapter's own statistics call
    assert stamp.value == Decimal("20000000000")


def test_dollar_volume_needs_twenty_sessions_of_calendar_history(overlay, spot) -> None:
    """(w3) Fail-closed on history: a visible session whose EXCHANGE ordinal
    sits inside the first 19 sessions of the bound NYSE fixture has no
    20-session window behind it — the adapter must fall back to the declared
    Decimal("0") sentinel, never a median over the sessions it happens to
    have. (R2-P1-a: this test used to thread the 11-session OVERLAY calendar
    as the exchange authority — the exact unbound self-certification the
    constructor gate now refuses; the floor is exercised where it lives, at
    the head of the real fixture, with a v2 map covering EVERY exchange
    session so the in-map guard passes and the floor is the only refusal.)"""
    exchange = _exchange_calendar()
    early = exchange.sessions()[4]  # ordinal 4: window would start at -15
    every_exchange_session = {
        SPY: {s: (Decimal("600.00"), 80_000_000) for s in exchange.sessions()}
    }
    adapter = VwapPitSurface(
        overlay, spot=spot, spot_v2=every_exchange_session, exchange_calendar=exchange
    )
    assert adapter._dollar_volume_as_of(SPY, early, publication_instant(S6)) is None


def test_dollar_volume_refuses_a_non_calendar_visible_session(v2_capture) -> None:
    """(w3) Fail-closed on calendar identity: a visible session that is not
    a calendar session has no ordinal, so no window can be anchored to it —
    the source is refused (None, i.e. the declared sentinel fallback), never
    silently re-anchored to some other session's window. (R2-P1-a: the
    authority is the BOUND NYSE fixture, and the v2 map covers BOTH the
    regular window and the fixture's trailing 20 sessions — the re-anchor
    mutant lands on the LAST fixture session's fully-covered window and
    answers a median, so the kill needs the mutated call to answer a
    median, not None.)"""
    overlay = load_derived_surface(v2_capture, staleness_sessions=10)
    exchange = _exchange_calendar()
    tail = exchange.sessions()[-20:]
    source = {SPY: {s: (Decimal(V2_CLOSE), V2_VOLUME) for s in (*V2_SESSIONS, *tail)}}
    adapter = VwapPitSurface(
        overlay,
        spot={SPY: {s: SPOT_LEVEL for s in V2_SESSIONS}},
        spot_v2=source,
        exchange_calendar=exchange,
    )
    saturday = V2_FIRST + timedelta(days=5)  # 2025-04-19: inside the span, never a session
    assert saturday not in V2_SESSIONS
    assert not exchange.is_session(saturday)
    assert adapter._dollar_volume_as_of(SPY, saturday, publication_instant(V2_VISIBLE)) is None


def _exchange_calendar():
    """The repo-adopted EXCHANGE calendar (0.2.1 ruling, data/g4/
    calendar-decision.json "repo-generated-calendar"): the committed,
    checksummed NYSE fixture `research_protocol.yaml` itself declares —
    loaded through the surface's BOUND factory (R2-P1-a), which verifies
    the file checksum AND the pinned CONTENT identity before handing the
    authority to the adapter's constructor gate."""
    from tree_options.data.vwap_pit_surface import repo_exchange_calendar

    return repo_exchange_calendar(REPO_ROOT)


def test_exchange_session_missing_from_every_capture_fails_closed(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """(P1-2, Codex round 1 — the Jan-16 scenario) The 20-session window is
    enumerated on the EXCHANGE calendar, never on the overlay's union of
    CAPTURED dates: a market session missing from every capture vanishes
    from the overlay calendar, and a window sliced on that union
    SELF-CERTIFIES as contiguity — the hole is simply not in the slice, the
    v2 proxy looks complete, and the median PASSES where the design says
    fail-closed. Here the proxy covers every CAPTURED session of the window
    and exactly one EXCHANGE session (2025-04-24, a regular NYSE Thursday)
    is absent everywhere: the answer must be the declared sentinel."""
    hole = date(2025, 4, 24)
    exchange = _exchange_calendar()
    assert exchange.is_session(hole)  # the market was open — the capture missed it
    assert hole in V2_WINDOW  # inside the 20-session window ending at the visible one
    capture = _write_v2_capture(
        tmp_path_factory.mktemp("vwappitx") / "capture", skip=frozenset({hole})
    )
    overlay = load_derived_surface(capture, staleness_sessions=10)
    assert hole not in overlay.calendar.sessions()  # the hole self-erased
    source = {SPY: {s: (Decimal(V2_CLOSE), V2_VOLUME) for s in overlay.calendar.sessions()}}
    assert hole not in source[SPY]  # complete on the overlay calendar, absent at the hole
    adapter = VwapPitSurface(
        overlay,
        spot={SPY: {s: SPOT_LEVEL for s in overlay.calendar.sessions()}},
        spot_v2=source,
        exchange_calendar=exchange,
    )
    snap = adapter.candidate_snapshot(adapter.contract(C600_ID), V2_DECISION)
    stamp = snap.underlying_20d_median_dollar_volume
    assert stamp is not None and stamp.value == Decimal("0"), (
        "an exchange session missing from every capture must fail closed to the"
        " declared sentinel — never a median over the 19 sessions that happened"
        " to be captured"
    )
    protocol = load_protocol()
    by_rule = {
        r.rule: r
        for r in CandidateFilter.from_protocol_volume_flow(overlay.calendar, protocol)
        .evaluate(snap)
        .results
    }
    assert by_rule["underlying_liquidity"].status == "FAIL"


def test_a_true_20_exchange_session_window_answers_the_median(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """(P1-2, the PASS-path control) The same holed capture with the hole
    MAPPED in the v2 proxy: the window is now exactly the 20 consecutive
    EXCHANGE sessions ending at the visible session, all present — the
    median answers and the rule passes. The guard refuses holes, not the
    lane."""
    hole = date(2025, 4, 24)
    exchange = _exchange_calendar()
    capture = _write_v2_capture(
        tmp_path_factory.mktemp("vwappitxc") / "capture", skip=frozenset({hole})
    )
    overlay = load_derived_surface(capture, staleness_sessions=10)
    source = {
        SPY: {
            **{s: (Decimal(V2_CLOSE), V2_VOLUME) for s in overlay.calendar.sessions()},
            hole: (Decimal(V2_CLOSE), V2_VOLUME),
        }
    }
    adapter = VwapPitSurface(
        overlay,
        spot={SPY: {s: SPOT_LEVEL for s in overlay.calendar.sessions()}},
        spot_v2=source,
        exchange_calendar=exchange,
    )
    snap = adapter.candidate_snapshot(adapter.contract(C600_ID), V2_DECISION)
    stamp = snap.underlying_20d_median_dollar_volume
    assert stamp is not None
    assert stamp.value == Decimal("48000000000")
    assert stamp.provenance == "vendor"


def test_without_an_exchange_calendar_the_v2_source_never_answers(v2_capture) -> None:
    """(P1-2) The exchange calendar is an EXPLICIT constructor dependency —
    never a global, never auto-loaded: without it, contiguity on the
    exchange cannot be proven and the v2 source must fail closed (the
    declared sentinel), exactly as with no v2 source at all."""
    overlay = load_derived_surface(v2_capture, staleness_sessions=10)
    adapter = VwapPitSurface(
        overlay,
        spot={SPY: {s: SPOT_LEVEL for s in V2_SESSIONS}},
        spot_v2=_v2_map(),  # complete on every captured session
    )
    snap = adapter.candidate_snapshot(adapter.contract(C600_ID), V2_DECISION)
    stamp = snap.underlying_20d_median_dollar_volume
    assert stamp is not None and stamp.value == Decimal("0")


# ---- (R2-P1-a, Codex round 2) the exchange authority is PROVENANCE-BOUND -------------


def test_an_unbound_exchange_calendar_refuses_at_construction(overlay, spot) -> None:
    """(R2-P1-a) `VwapPitSurface.__init__` used to accept ANY `SessionCalendar`
    as `exchange_calendar` — and this suite itself passed
    `exchange_calendar=overlay.calendar`, the union of CAPTURED dates: the
    exact self-certification vector round-1 P1-2 closed, handed back in
    through the constructor. The overlay calendar now REFUSES, loudly, at
    construction (RED before R2-P1-a: the constructor accepted it)."""
    with pytest.raises(MassiveOverlayError, match="repo-adopted NYSE fixture"):
        VwapPitSurface(overlay, spot=spot, exchange_calendar=overlay.calendar)


def test_a_doctored_content_identity_still_refuses(overlay, spot) -> None:
    """(R2-P1-a) The gate binds CONTENT, not object identity — so a calendar
    BUILT to look like the fixture still refuses when its semantics differ:
    (i) one interior session swapped for the adjacent Sunday (same count,
    same first/last — the lossy-descriptor attack R2-P1-b closes at the
    descriptor), and (ii) the identical session tuple carrying ONE extra
    early close. Both must name the bound authority in the refusal."""
    from tree_options.data.real_overlay import RealSessionCalendar

    sessions = _exchange_calendar().sessions()
    # (i) find a session whose successor is >= 3 days out (a Friday before a
    # Monday): Friday+2 is a Sunday strictly between its neighbors
    index = next(
        i for i in range(1000, len(sessions) - 1) if (sessions[i + 1] - sessions[i]).days >= 3
    )
    doctored = list(sessions)
    doctored[index] = doctored[index] + timedelta(days=2)
    assert doctored[index] not in sessions
    assert len(doctored) == len(sessions)
    assert doctored[0] == sessions[0] and doctored[-1] == sessions[-1]
    with pytest.raises(MassiveOverlayError, match="repo-adopted NYSE fixture"):
        VwapPitSurface(
            overlay,
            spot=spot,
            exchange_calendar=RealSessionCalendar(tuple(doctored), frozenset()),
        )
    # (ii) same sessions, one extra early close: the early-close map is part
    # of the identity, so this is a DIFFERENT authority
    with pytest.raises(MassiveOverlayError, match="repo-adopted NYSE fixture"):
        VwapPitSurface(
            overlay,
            spot=spot,
            exchange_calendar=RealSessionCalendar(sessions, frozenset({sessions[100]})),
        )


def test_the_bound_factory_loads_the_committed_fixture(overlay) -> None:
    """(R2-P1-a) `repo_exchange_calendar()` is the sanctioned way to obtain
    the authority: it loads the committed checksummed fixture, verifies its
    COMPLETE content identity against the pin, and the resulting calendar is
    exactly what the constructor gate accepts (an independent construction
    of the same committed files hashes identically — the pin is the
    fixture's identity, not a single load's accident)."""
    from tree_options.data.vwap_pit_surface import (
        REPO_EXCHANGE_CALENDAR_CONTENT_SHA256,
        repo_exchange_calendar,
    )
    from tree_options.time.calendar import calendar_content_sha256

    calendar = repo_exchange_calendar()  # default root: the repo itself
    assert calendar.name == "XNYS"
    assert calendar_content_sha256(calendar) == REPO_EXCHANGE_CALENDAR_CONTENT_SHA256
    twin = repo_exchange_calendar(REPO_ROOT)
    assert twin.sessions() == calendar.sessions()
    assert twin.early_close_sessions() == calendar.early_close_sessions()
    # and the bound calendar is ACCEPTED where the doctored ones refuse
    adapter = VwapPitSurface(overlay, exchange_calendar=calendar)
    assert adapter._exchange_calendar is calendar


def test_the_bound_factory_refuses_a_checksum_consistent_doctored_fixture(
    tmp_path: Path,
) -> None:
    """(R2-P1-a) The pin binds SEMANTICS, not file bytes: a doctored fixture
    copy whose sidecar checksum is REGENERATED to match its own content
    passes `StaticSessionCalendar`'s checksum gate and every fixture
    invariant — only the CONTENT changed — and still refuses at the pin.
    The content identity is the authority, so a fixture edit that keeps the
    checksum discipline honest cannot slip past as the exchange authority
    (RED under a gutted pin check: the doctored copy loads silently)."""
    import hashlib
    import shutil

    from tree_options.data.vwap_pit_surface import repo_exchange_calendar

    calendar_dir = tmp_path / "repo" / "data" / "calendar"
    calendar_dir.mkdir(parents=True)
    fixture = calendar_dir / "nyse_sessions_2018_01_02_2026_12_31.json"
    sidecar = calendar_dir / "nyse_sessions_2018_01_02_2026_12_31.sha256"
    for source, target in (
        (REPO_ROOT / "data" / "calendar" / fixture.name, fixture),
        (REPO_ROOT / "data" / "calendar" / sidecar.name, sidecar),
    ):
        shutil.copyfile(source, target)
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    # drop one interior session that is NOT an early close: every fixture
    # invariant still holds (strictly increasing, no duplicates, early
    # closes still a subset) — only the session CONTENT changed
    early = set(payload["early_close_sessions"])
    victim = next(s for s in payload["sessions"][1:-1] if s not in early)
    payload["sessions"] = [s for s in payload["sessions"] if s != victim]
    body = json.dumps(payload)
    fixture.write_text(body, encoding="utf-8")
    sidecar.write_text(
        hashlib.sha256(body.encode("utf-8")).hexdigest() + "  " + fixture.name + "\n",
        encoding="utf-8",
    )
    with pytest.raises(MassiveOverlayError, match="content identity changed"):
        repo_exchange_calendar(tmp_path / "repo")


# ---- (R3-P1-1, Codex round 3) class identity binds BEHAVIOR, not just data ---------


def _committed_fixture_paths() -> tuple[Path, Path]:
    """The committed NYSE fixture's (json, sidecar) paths — the same files
    `repo_exchange_calendar()` loads, so a subclass built over them carries
    IDENTICAL data by construction (never a hand-copied session tuple)."""
    return (
        REPO_ROOT / "data" / "calendar" / "nyse_sessions_2018_01_02_2026_12_31.json",
        REPO_ROOT / "data" / "calendar" / "nyse_sessions_2018_01_02_2026_12_31.sha256",
    )


def test_a_subclassed_exchange_calendar_with_identical_data_refuses(overlay, spot) -> None:
    """(R3-P1-1, Codex round 3) `calendar_content_sha256` hashed only the
    DATA (`{n_sessions, sessions, early_close_sessions}`), so a
    `StaticSessionCalendar` SUBCLASS reporting the canonical data — with or
    without overriding behavior — retained the pinned digest and was ACCEPTED
    by the constructor gate. No SHA collision is needed: the self-reported
    hashed payload is literally identical. For the concrete canonical classes
    behavior is DERIVED deterministically from that data, so class identity +
    data identity is complete behavioral identity; the digest therefore now
    names WHO computed it too, and the same gate refuses the twin (RED before
    R3-P1-1: the twin was accepted — identical digest)."""
    from tree_options.time.calendar import StaticSessionCalendar, calendar_content_sha256

    class _TwinExchangeCalendar(StaticSessionCalendar):
        """IDENTICAL committed data, ZERO overrides — a different concrete
        class carrying the canonical authority's exact semantics."""

    json_path, checksum_path = _committed_fixture_paths()
    twin = _TwinExchangeCalendar(json_path, checksum_path)
    exchange = _exchange_calendar()
    assert twin.sessions() == exchange.sessions()
    assert twin.early_close_sessions() == exchange.early_close_sessions()
    assert twin.session_close(exchange.sessions()[100]) == exchange.session_close(
        exchange.sessions()[100]
    )
    # the identity split is the DIGEST's, not the gate's alone
    assert calendar_content_sha256(twin) != calendar_content_sha256(exchange)
    with pytest.raises(MassiveOverlayError, match="repo-adopted NYSE fixture"):
        VwapPitSurface(overlay, spot=spot, exchange_calendar=twin)


def test_an_overriding_subclass_is_refused_and_a_different_identity(overlay, spot) -> None:
    """(R3-P1-1) The Codex live probe: a subclass overriding `ordinal()` —
    `super().ordinal(d) + 1` — reports the canonical data and shifted a
    liquidity median across the $50M protocol threshold while retaining the
    pinned digest. Data identity alone cannot certify a class whose methods
    are free to disagree with its data; the class identity in the payload
    makes the probe a REFUSED calendar and a DIFFERENT digest (RED before
    R3-P1-1: identical digest, accepted at the gate)."""
    from tree_options.time.calendar import StaticSessionCalendar, calendar_content_sha256

    class _OrdinalShiftedCalendar(StaticSessionCalendar):
        """The Codex probe shape: canonical data, shifted ordinal."""

        def ordinal(self, d: date) -> int:
            return super().ordinal(d) + 1

    json_path, checksum_path = _committed_fixture_paths()
    shifted = _OrdinalShiftedCalendar(json_path, checksum_path)
    exchange = _exchange_calendar()
    probe = exchange.sessions()[100]
    assert shifted.sessions() == exchange.sessions()
    assert shifted.early_close_sessions() == exchange.early_close_sessions()
    assert shifted.ordinal(probe) == exchange.ordinal(probe) + 1  # behavior differs
    assert calendar_content_sha256(shifted) != calendar_content_sha256(exchange)
    with pytest.raises(MassiveOverlayError, match="repo-adopted NYSE fixture"):
        VwapPitSurface(overlay, spot=spot, exchange_calendar=shifted)


# ---- end to end: the UNMODIFIED backtest over the adapter --------------------------


@dataclass(frozen=True)
class _LaneDataset:
    """The slice of `PointInTimeDataset` the backtest reads (bars for the
    settlement/silent-death scans, actions). Building the real lane-2
    dataset is the T-NULL driver's job, not the adapter's."""

    snapshot_id: str
    bars: tuple[BarRecord, ...]
    actions: tuple[object, ...] = ()


def _lane_dataset(world_id: str, sessions: tuple[date, ...] = SESSIONS) -> _LaneDataset:
    bars = []
    for session in sessions:
        bars.append(
            BarRecord(
                security_id=SPY,
                session=session,
                open=SPOT_LEVEL,
                high=SPOT_LEVEL,
                low=SPOT_LEVEL,
                close=SPOT_LEVEL,
                volume=1_000_000,
                source="spot-proxy/declared",
                source_record_id=f"SPY-{session:%Y%m%d}",
                source_row_hash="0" * 64,
                snapshot_id=world_id,
                available_at=publication_instant(session),
            )
        )
    return _LaneDataset(snapshot_id=world_id, bars=tuple(bars))


def test_run_options_backtest_unmodified_over_the_adapter(surface) -> None:
    protocol = load_protocol()
    candidate_filter = _zeroed_liquidity_volume_flow(surface, protocol)
    result = run_options_backtest(
        calendar=surface.overlay.calendar,
        surface=cast(GeneratedOptionOverlay, surface),  # the lane-2 cast, as documented
        dataset=_lane_dataset(surface.snapshot_id),
        candidate_filter=candidate_filter,
        signals=(
            OptionSignal(decision_session=S4, security_id=SPY, score=0.7, label=0.02),
            OptionSignal(decision_session=S5, security_id=SPY, score=0.8, label=0.01),
        ),
        initial_cash=Decimal("1000000.00"),
        config=OptionsStrategyConfig(),
        arm="A",
        end_session=S10,
    )
    # it ran, unmodified, end to end: fills, conservation, equity, rows
    buys = [f for f in result.fills if f.side == "buy"]
    sells = [f for f in result.fills if f.side == "sell"]
    assert buys and sells
    assert result.counters.conservation_checks == len(result.sessions)
    assert len(result.summary.session_returns) == len(result.sessions)
    assert result.equities and result.equities[0] > 0
    closed = [p for p in result.positions if p.exit_kind is not None]
    assert closed, result.positions
    # every fill selected the previous session's published bar (the wall)
    for fill in result.fills:
        audit = next(a for a in result.fill_audit if a.fill_id == fill.fill_id)
        assert audit.decision_session < audit.execution_session
        prev = surface.overlay.calendar.sessions()[
            surface.overlay.calendar.ordinal(audit.execution_session) - 1
        ]
        assert audit.quote_received_at == publication_instant(prev)
    # marks came from the previous session's VWAP (no misses in this window)
    assert result.counters.mark_misses == 0
    assert result.terminal_equity == result.equities[-1]


def test_surface_is_option_pit_surface_shaped(surface) -> None:
    """The consumed method set (backtest/options.py + options/strategy.py,
    plus the runner's R4-P1 boundary read) exists on the adapter with the
    lane-1 signatures. (Ride-along, wave-3 deviation (d): `decision_close`
    was owed then; `decision_calendar` rides the same declared-method-set
    tuple because `run_options_trial` now reads it at the boundary.)"""
    for name in (
        "visible_file_session",
        "entry_as_of",
        "spot_mid_as_of",
        "visible_quotes_as_of",
        "contract",
        "candidate_snapshot",
        "eligible_as_of",
        "live_expiries_as_of",
        "strike_ladder",
        "decision_close",
    ):
        assert callable(getattr(surface, name)), name
    assert isinstance(surface.snapshot_id, str)
    assert hasattr(surface, "overlay")
    # (R4-P1) the DISCLOSED decision authority is a property, not a call:
    # the module fixture is UNWIRED, so the honest disclosure is the overlay
    # (execution) calendar its decision_close() actually answers from — the
    # object the runner's boundary digest will hash
    assert surface.decision_calendar is surface.overlay.calendar


# ---- (P1-1, Codex round 1) the dual-calendar seam: Friday grid x daily bars ---------


# The Codex scenario: a world whose decision grid is Fridays-only and whose
# bars are DAILY. A Friday decision at D executes at the NEXT GRID Friday
# D+1 (2025-04-04 -> 2025-04-11); the adapter supplies the previous TRADING
# day's bar (2025-04-10, a Thursday that is not a grid session). Under ONE
# calendar both horns fail: the daily calendar collapses the ruled geometry
# (13 consecutive daily sessions are never a subset of a Fridays-only set,
# and arm-A's hold becomes 4 days, not 4 weeks); the Friday-only calendar
# rejects the daily bar (BAR_SESSION_NOT_IN_CALENDAR / BAR_NOT_MOST_RECENT).
A_EXP = date(2025, 5, 16)  # the third Friday of May 2025
# daily bars: the Thursdays the decisions/fills read, through the headroom
# past the exit execution (the backtest's next-open look-ahead needs one
# session past end_session on WHICH EVER calendar carries the loop)
A_BAR_SPAN = (date(2025, 3, 20), date(2025, 6, 5))
A_DECISION = date(2025, 4, 4)  # a grid Friday, DTE 42 to A_EXP
A_EXECUTION = date(2025, 4, 11)  # the NEXT GRID Friday
A_BAR_SESSION = date(2025, 4, 10)  # the previous TRADING day's bar


def _a_ticker() -> str:
    return f"O:SPY{A_EXP:%y%m%d}C00600000"


def _a_premium(session: date) -> str:
    price = bs_price(
        spot=float(SPOT_LEVEL),
        strike=600.0,
        dte_calendar_days=(A_EXP - session).days,
        iv=IV,
        risk_free=0.03,
        dividend_yield=0.0,
        call_put="C",
    )
    return f"{price:.4f}"


def _friday_grid(first: date, last: date):
    """The Friday-only decision grid derived from the NYSE fixture (the era
    profile's `friday_only_grid_derived_from_the_nyse_fixture`): the
    fixture's Friday sessions in [first, last] — holiday Fridays (Good
    Friday) are absent because the FIXTURE omits them, not by local rule.
    (R2-P1-c) The grid carries the fixture's early closes — the fixture AS
    COMMITTED, never a `frozenset()` that silently strips them."""
    from tree_options.data.real_overlay import RealSessionCalendar

    exchange = _exchange_calendar()
    fridays = tuple(s for s in exchange.sessions() if s.weekday() == 4 and first <= s <= last)
    assert fridays and fridays[0].weekday() == 4
    early = frozenset(exchange.early_close_sessions())
    return RealSessionCalendar(fridays, early & frozenset(fridays))


def _write_dual_calendar_capture(root: Path) -> Path:
    exchange = _exchange_calendar()
    bar_sessions = tuple(s for s in exchange.sessions() if A_BAR_SPAN[0] <= s <= A_BAR_SPAN[1])
    assert len(bar_sessions) > 30
    grid = _friday_grid(date(2025, 3, 21), date(2025, 5, 30))
    masters = root / "masters"
    masters.mkdir(parents=True)
    (masters / f"spy_{grid.sessions()[0]:%Y-%m-%d}.json").write_text(
        fx.contracts_payload(
            results=(
                fx.contract_result(
                    ticker=_a_ticker(),
                    underlying=SPY,
                    expiration=f"{A_EXP:%Y-%m-%d}",
                    strike="600",
                    contract_type="call",
                ),
            ),
            as_of=f"{grid.sessions()[0]:%Y-%m-%d}",
        ),
        encoding="utf-8",
    )
    bars = root / "bars"
    bars.mkdir()
    rows = tuple(
        fx.bar(
            v=FLOW_VOLUME,
            t=_t(session),
            vw=_a_premium(session),
            o=_a_premium(session),
            c=_a_premium(session),
            h=str(Decimal(_a_premium(session)) + Decimal("0.10")),
            low=str(Decimal(_a_premium(session)) - Decimal("0.10")),
            n="24",
        )
        for session in bar_sessions
    )
    (bars / "bars_c600.json").write_text(
        fx.bars_payload(ticker=_a_ticker(), results_count=str(len(rows)), results=rows),
        encoding="utf-8",
    )
    (root / "spot_proxy.json").write_text(
        '{"SPY": {' + ", ".join(f'"{s:%Y-%m-%d}": "{SPOT_LEVEL}"' for s in bar_sessions) + "}}",
        encoding="utf-8",
    )
    return root


def _volume_flow_filter_022(protocol, calendar) -> CandidateFilter:
    """The protocol's volume-flow regime with the ONE ruled 0.2.2 deviation
    the real lane runs under (earnings rule off — without it 0.2.1 answers
    NOT_EVALUABLE 'missing' on spans_earnings=None and the lane trades
    zero, the w2 ruling). Every other knob at the protocol's values,
    including the $50M liquidity minimum."""
    d = protocol.option_candidate_defaults
    lf = d.liquidity_volume_flow
    assert lf is not None
    return CandidateFilter(
        calendar,
        dte_min=d.dte_min,
        dte_max=d.dte_max,
        abs_delta_min=d.abs_delta_min,
        abs_delta_max=d.abs_delta_max,
        standard_deliverable_only=d.standard_deliverable_only,
        min_open_interest=d.min_open_interest,
        min_same_day_volume=d.min_same_day_volume,
        volume_only_if_already_available=d.volume_only_if_already_available,
        max_spread_fraction_of_midpoint=d.max_spread_fraction_of_midpoint,
        min_underlying_20d_median_dollar_volume=d.min_underlying_20d_median_dollar_volume,
        exclude_earnings_spanning_hold=False,  # the ruled 0.2.2 shape
        liquidity_regime="volume_flow",
        flow_min_session_volume=lf.flow_min_session_volume,
        accepted_delta_provenance=lf.abs_delta_provenance_accepted,
    )


def test_dual_calendar_friday_grid_daily_bars_fills(tmp_path_factory) -> None:
    """(P1-1, Codex round 1) The healthy scenario: the DECISION GRID
    (Friday-only, per the era profile) drives the backtest's decision
    sequencing — close(D) decision, entry at the next GRID Friday D+1,
    arm-A hold of 4 GRID sessions (4 weeks) — while the EXECUTION calendar
    (the overlay's daily MassiveDerivedSessionCalendar) drives the fill
    engine's session checks, so the adapter's previous-TRADING-day bar
    (Thursday 2025-04-10) fills at Friday 2025-04-11 without
    BAR_SESSION_NOT_IN_CALENDAR or BAR_NOT_MOST_RECENT. RED before P1-1
    under either single-calendar choice (demonstrated at value level in
    reallane-r1-p1-1-red.log)."""
    capture = _write_dual_calendar_capture(tmp_path_factory.mktemp("dualcal") / "capture")
    overlay = load_derived_surface(capture, staleness_sessions=60)
    grid = _friday_grid(date(2025, 3, 21), date(2025, 5, 30))
    daily = overlay.calendar
    exchange = _exchange_calendar()
    assert A_DECISION in grid.sessions() and A_EXECUTION in grid.sessions()
    assert A_BAR_SESSION in daily.sessions() and A_BAR_SESSION not in grid.sessions()
    assert exchange.is_session(A_BAR_SESSION)
    adapter = VwapPitSurface(
        overlay,
        spot={SPY: {s: SPOT_LEVEL for s in daily.sessions()}},
        spot_v2={
            SPY: {
                s: (Decimal("600.00"), 80_000_000)
                for s in exchange.sessions()
                if date(2025, 3, 6) <= s <= date(2025, 5, 15)
            }
        },
        exchange_calendar=exchange,
    )
    protocol = load_protocol()
    result = run_options_backtest(
        calendar=grid,
        execution_calendar=daily,
        surface=cast(GeneratedOptionOverlay, adapter),  # the lane-2 cast, as documented
        dataset=_lane_dataset(adapter.snapshot_id, daily.sessions()),
        candidate_filter=_volume_flow_filter_022(protocol, daily),
        signals=(
            OptionSignal(decision_session=A_DECISION, security_id=SPY, score=0.9, label=0.01),
        ),
        initial_cash=Decimal("1000000.00"),
        config=OptionsStrategyConfig(),
        arm="A",
        end_session=date(2025, 5, 16),  # the exit execution; the grid keeps headroom past it
    )
    buys = [f for f in result.fills if f.side == "buy"]
    assert buys, f"no entry fills: {dict(result.counters.entry_fill_rejections)}"
    assert not any(code.startswith("BAR_") for code in result.counters.entry_fill_rejections), dict(
        result.counters.entry_fill_rejections
    )
    for fill in buys:
        assert fill.execution_session == A_EXECUTION  # the NEXT GRID Friday, not the next day
        audit = next(a for a in result.fill_audit if a.fill_id == fill.fill_id)
        assert audit.decision_session == A_DECISION
        assert audit.execution_session == A_EXECUTION
        # the selected bar is the previous TRADING day's: Thursday 2025-04-10
        assert audit.quote_received_at == publication_instant(A_BAR_SESSION)
    # the arm-A hold is 4 GRID sessions (weeks): entry exec 04-11, exit exec 05-16
    sells = [f for f in result.fills if f.side == "sell"]
    assert sells and all(f.execution_session == date(2025, 5, 16) for f in sells)


# ---- (R3-P1-2, Codex round 3) the GRID decision instant in candidate construction -----


# The Codex scenario: an EARLY-CLOSE grid Friday whose overlay (execution)
# calendar still answers the nominal 16:00. 2025-11-28 is a 13:00 close in
# the committed NYSE fixture, and MassiveDerivedSessionCalendar carries an
# empty early-close set, so the two closes of that one session genuinely
# disagree — a corporate action PUBLISHED in the 13:00-16:00 gap is future
# information at the true decision instant and file-visible either way (the
# publication wall sits at T+1 09:00, never inside a session).
EC_DECISION = date(2025, 11, 28)  # the early-close Friday after Thanksgiving
EC_EXPIRY = date(2025, 12, 19)  # the contract the grid lane carries
EC_BAR_SPAN = (date(2025, 10, 23), date(2025, 12, 5))


def _ec_premium(session: date) -> str:
    price = bs_price(
        spot=float(SPOT_LEVEL),
        strike=600.0,
        dte_calendar_days=(EC_EXPIRY - session).days,
        iv=IV,
        risk_free=0.03,
        dividend_yield=0.0,
        call_put="C",
    )
    return f"{price:.4f}"


def _write_early_close_capture(root: Path) -> Path:
    exchange = _exchange_calendar()
    bar_sessions = tuple(s for s in exchange.sessions() if EC_BAR_SPAN[0] <= s <= EC_BAR_SPAN[1])
    assert EC_DECISION in bar_sessions  # the overlay carries the early-close session too
    masters = root / "masters"
    masters.mkdir(parents=True)
    (masters / f"spy_{bar_sessions[0]:%Y-%m-%d}.json").write_text(
        fx.contracts_payload(
            results=(
                fx.contract_result(
                    ticker=f"O:SPY{EC_EXPIRY:%y%m%d}C00600000",
                    underlying=SPY,
                    expiration=f"{EC_EXPIRY:%Y-%m-%d}",
                    strike="600",
                    contract_type="call",
                ),
            ),
            as_of=f"{bar_sessions[0]:%Y-%m-%d}",
        ),
        encoding="utf-8",
    )
    bars = root / "bars"
    bars.mkdir()
    rows = tuple(
        fx.bar(
            v=FLOW_VOLUME,
            t=_t(session),
            vw=_ec_premium(session),
            o=_ec_premium(session),
            c=_ec_premium(session),
            h=str(Decimal(_ec_premium(session)) + Decimal("0.10")),
            low=str(Decimal(_ec_premium(session)) - Decimal("0.10")),
            n="24",
        )
        for session in bar_sessions
    )
    (bars / "bars_c600.json").write_text(
        fx.bars_payload(
            ticker=f"O:SPY{EC_EXPIRY:%y%m%d}C00600000", results_count=str(len(rows)), results=rows
        ),
        encoding="utf-8",
    )
    (root / "spot_proxy.json").write_text(
        '{"SPY": {' + ", ".join(f'"{s:%Y-%m-%d}": "{SPOT_LEVEL}"' for s in bar_sessions) + "}}",
        encoding="utf-8",
    )
    return root


@pytest.fixture(scope="module")
def early_close_grid():
    """The Friday-only decision grid over the early-close window, carrying the
    committed fixture's early closes (so 2025-11-28 answers 13:00 ET)."""
    return _friday_grid(EC_BAR_SPAN[0], EC_BAR_SPAN[1])


@pytest.fixture(scope="module")
def early_close_surface(tmp_path_factory: pytest.TempPathFactory, early_close_grid):
    """The grid-supplied surface: `decision_calendar` is the grid, exactly as
    the runner threads it."""
    capture = _write_early_close_capture(tmp_path_factory.mktemp("earlyclose") / "capture")
    overlay = load_derived_surface(capture, staleness_sessions=400)
    return VwapPitSurface(overlay, decision_calendar=early_close_grid)


def _ec_action(available_at: datetime) -> CorporateActionRecord:
    """A pending ratio action on SPY, published at `available_at`, effective
    after the decision session — the §2 (i) entry-exclusion shape."""
    return CorporateActionRecord(
        security_id=SPY,
        kind="split",
        effective_session=date(2025, 12, 5),
        ratio_numerator=2,
        ratio_denominator=1,
        source="declared/v1",
        source_record_id="ACT-EC-1",
        source_row_hash="0" * 64,
        snapshot_id="early-close-world",
        available_at=available_at,
    )


def _ec_build(surface, grid, protocol, actions) -> CandidateAudit:
    from tree_options.options import build_candidates

    audit = CandidateAudit()
    build_candidates(
        surface=surface,
        candidate_filter=CandidateFilter.from_protocol_volume_flow(grid, protocol),
        decision_session=EC_DECISION,
        scores=(
            OptionSignal(decision_session=EC_DECISION, security_id=SPY, score=0.9, label=0.01),
        ),
        config=OptionsStrategyConfig(),
        actions=actions,
        audit=audit,
    )
    return audit


def test_an_action_published_after_the_true_close_is_not_yet_known(
    early_close_surface, early_close_grid, protocol
) -> None:
    """(R3-P1-2) `build_candidates` derived the decision instant from the
    OVERLAY (daily, nominal 16:00) calendar while the grid lane decides at
    the grid's TRUE close — so on the early-close Friday a pending action
    PUBLISHED 14:00 ET, three hours AFTER the 13:00 close, was treated as
    known at decision and the name was excluded on future information
    (INV-02), flipping the audit stamp from the honest earnings
    NOT_EVALUABLE to `excluded_pending_action`. The decision instant now
    comes from the surface's `decision_close` seam, which the grid-supplied
    surface answers from the decision calendar: the action is NOT yet known,
    the name is NOT excluded, and it flows on to the honest expiry rule (DTE
    21 to 2025-12-19 is outside the protocol's [30, 60] band — RED before
    R3-P1-2: excluded_pending_action == 1 and no_in_band_expiry == 0)."""
    from tree_options.time.sessions import early_close_instant, session_close_instant

    overlay_close = early_close_surface.overlay.calendar.session_close(EC_DECISION)
    true_close = early_close_grid.session_close(EC_DECISION)
    assert true_close == early_close_instant(EC_DECISION)  # 13:00 ET
    assert overlay_close == session_close_instant(EC_DECISION)  # the nominal 16:00
    published_14_et = shift_instant(true_close, 3600)  # 14:00 ET, inside the gap
    assert true_close < published_14_et < overlay_close

    audit = _ec_build(
        early_close_surface, early_close_grid, protocol, (_ec_action(published_14_et),)
    )
    assert audit.scored_cross_section == 1
    assert audit.selected == 1
    assert audit.excluded_pending_action == 0, (
        "an action published after the true close is future information at"
        " the decision instant — the name must not be excluded on it"
    )
    # the name was NOT dropped anywhere upstream: it reached the expiry rule
    assert audit.no_in_band_expiry == 1
    assert audit.accepted == 0


def test_an_action_published_before_the_true_close_is_known(
    early_close_surface, early_close_grid, protocol
) -> None:
    """(R3-P1-2) The seam must not over-correct into ignoring genuinely-known
    actions: the same action published 12:00 ET — BEFORE the 13:00 true
    close — IS known at decision and the name IS excluded (the pre-existing
    §2 (i) behavior, unchanged)."""
    from tree_options.time.sessions import early_close_instant

    true_close = early_close_grid.session_close(EC_DECISION)
    assert true_close == early_close_instant(EC_DECISION)
    published_12_et = shift_instant(true_close, -3600)  # 12:00 ET
    assert published_12_et < true_close

    audit = _ec_build(
        early_close_surface, early_close_grid, protocol, (_ec_action(published_12_et),)
    )
    assert audit.excluded_pending_action == 1
    assert audit.no_in_band_expiry == 0
    assert audit.accepted == 0


def test_without_a_decision_calendar_the_overlay_close_stands(
    early_close_surface, early_close_grid, protocol
) -> None:
    """(R3-P1-2) No `decision_calendar` supplied -> today's behavior pinned:
    the OVERLAY (execution) calendar's nominal 16:00 answers the decision
    instant, so the 14:00-published action IS treated as known and the name
    IS excluded (the lane-1/synthetic default, where the two calendars are
    one and this is correct).

    (R4-P1 scoping note) This is a SURFACE-level pin — the unwired surface
    keeps its documented 16:00 fallback, honestly disclosed by its
    `decision_calendar` property — and it stays green as such. The RUNNER
    is where the unwired authority refuses: a grid-stamped
    `run_options_trial` over an unwired surface is refused before
    registration (see test_trials_options_run), so the fallback answers
    only unwired surfaces on unwired (single-calendar) trials, never a
    grid-stamped one."""
    from tree_options.time.sessions import early_close_instant

    plain = VwapPitSurface(early_close_surface.overlay)
    assert plain.decision_close(EC_DECISION) == early_close_surface.overlay.calendar.session_close(
        EC_DECISION
    )
    assert plain.decision_close(EC_DECISION) != early_close_instant(EC_DECISION)

    audit = _ec_build(
        plain,
        early_close_grid,
        protocol,
        (_ec_action(shift_instant(early_close_instant(EC_DECISION), 3600)),),
    )
    assert audit.excluded_pending_action == 1
