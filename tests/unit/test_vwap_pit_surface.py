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
from tree_options.options import OptionSignal, OptionsStrategyConfig
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


def _write_v2_capture(root: Path) -> Path:
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
        for session in V2_SESSIONS
    )
    (bars / "bars_c600.json").write_text(
        fx.bars_payload(ticker=_ticker("C", 600), results_count=str(len(rows)), results=rows),
        encoding="utf-8",
    )
    (root / "spot_proxy.json").write_text(
        '{"SPY": {' + ", ".join(f'"{s:%Y-%m-%d}": "{SPOT_LEVEL}"' for s in V2_SESSIONS) + "}}",
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


def test_dollar_volume_source_passes_the_50m_rule(v2_capture, protocol) -> None:
    """(w3, P0-1(b) preferred leg) With a declared spot_proxy_v2 the
    underlying-liquidity term is honestly EVALUABLE: the 20-session median of
    close*volume at realistic mega-cap magnitude clears the protocol's $50M
    minimum by three orders, under vendor provenance at the T+1 wall of the
    window's last session."""
    overlay = load_derived_surface(v2_capture, staleness_sessions=10)
    adapter = VwapPitSurface(
        overlay, spot={SPY: {s: SPOT_LEVEL for s in V2_SESSIONS}}, spot_v2=_v2_map()
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
    20 sessions, or one that does not reach 20 sessions back, is not a 20d
    median — the adapter falls back to the declared sentinel rather than
    averaging over whatever happened to be captured."""
    overlay = load_derived_surface(v2_capture, staleness_sessions=10)
    spot = {SPY: {s: SPOT_LEVEL for s in V2_SESSIONS}}
    # a hole: drop the 10th session of the required window
    holed = {SPY: {s: v for s, v in _v2_map()[SPY].items() if s != V2_WINDOW[9]}}
    short = _v2_map(V2_SESSIONS[:5])  # only 5 sessions of history
    for source in (holed, short):
        adapter = VwapPitSurface(overlay, spot=spot, spot_v2=source)
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


def test_dollar_volume_median_is_exact_over_distinct_values(v2_capture) -> None:
    """(w3) The PASS path's statistic is the exact MEDIAN of the trailing 20
    sessions' close*volume — never the mean, and never a median over more
    history than the declared window. The 50M fixture repeats one identical
    daily value (median == mean == any-window median there), so this seam
    needs DISTINCT values with a spike, chosen so the three statistics
    separate:

        trailing 20 sorted = [10e9 x10, 30e9 x9, 1000e9] -> median 20e9
        trailing 20 mean                                      -> 68.5e9
        all captured history (21 sessions) sorted             -> 11th = 30e9
    """
    overlay = load_derived_surface(v2_capture, staleness_sessions=10)
    ordinal_of = {session: i for i, session in enumerate(V2_SESSIONS)}
    volume_by_ordinal = {
        0: 300_000_000,  # outside the trailing window but inside the capture
        **{i: 100_000_000 for i in range(1, 11)},  # ten 10e9 days (close 100)
        **{i: 300_000_000 for i in range(11, 20)},  # nine 30e9 days
        20: 10_000_000_000,  # the visible session's 1000e9 spike
        21: 100_000_000,  # the decision session: never inside any window
    }
    source = {SPY: {s: (Decimal("100.00"), volume_by_ordinal[ordinal_of[s]]) for s in V2_SESSIONS}}
    adapter = VwapPitSurface(
        overlay, spot={SPY: {s: SPOT_LEVEL for s in V2_SESSIONS}}, spot_v2=source
    )
    snap = adapter.candidate_snapshot(adapter.contract(C600_ID), V2_DECISION)
    stamp = snap.underlying_20d_median_dollar_volume
    assert stamp is not None
    # the exact median of the 20 trailing sessions: the average of the 10th
    # and 11th sorted values, (10e9 + 30e9) / 2 — an independent oracle, not
    # a re-derivation through the adapter's own statistics call
    assert stamp.value == Decimal("20000000000")


def test_dollar_volume_needs_twenty_sessions_of_calendar_history(overlay, spot) -> None:
    """(w3) Fail-closed on history: the module fixture's calendar carries 11
    sessions — fewer than the declared 20-session window — so even a v2
    source covering EVERY session cannot answer a 20d median: the adapter
    must fall back to the declared Decimal("0") sentinel, never a median
    over the 11 sessions it happens to have."""
    every_session = {SPY: {s: (Decimal("600.00"), 80_000_000) for s in SESSIONS}}
    adapter = VwapPitSurface(overlay, spot=spot, spot_v2=every_session)
    snap = adapter.candidate_snapshot(adapter.contract(C600_ID), S6)
    stamp = snap.underlying_20d_median_dollar_volume
    assert stamp is not None
    assert stamp.value == Decimal("0")


def test_dollar_volume_refuses_a_non_calendar_visible_session(v2_capture) -> None:
    """(w3) Fail-closed on calendar identity: a visible session that is not
    a calendar session has no ordinal, so no window can be anchored to it —
    the source is refused (None, i.e. the declared sentinel fallback), never
    silently re-anchored to some other session's window."""
    overlay = load_derived_surface(v2_capture, staleness_sessions=10)
    adapter = VwapPitSurface(
        overlay,
        spot={SPY: {s: SPOT_LEVEL for s in V2_SESSIONS}},
        spot_v2=_v2_map(),  # every session of the 22-session calendar
    )
    saturday = V2_FIRST + timedelta(days=5)  # 2025-04-19: inside the span, never a session
    assert saturday not in V2_SESSIONS
    assert adapter._dollar_volume_as_of(SPY, saturday, publication_instant(V2_VISIBLE)) is None


# ---- end to end: the UNMODIFIED backtest over the adapter --------------------------


@dataclass(frozen=True)
class _LaneDataset:
    """The slice of `PointInTimeDataset` the backtest reads (bars for the
    settlement/silent-death scans, actions). Building the real lane-2
    dataset is the T-NULL driver's job, not the adapter's."""

    snapshot_id: str
    bars: tuple[BarRecord, ...]
    actions: tuple[object, ...] = ()


def _lane_dataset(world_id: str) -> _LaneDataset:
    bars = []
    for session in SESSIONS:
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
    """The consumed method set (backtest/options.py + options/strategy.py)
    exists on the adapter with the lane-1 signatures."""
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
    ):
        assert callable(getattr(surface, name)), name
    assert isinstance(surface.snapshot_id, str)
    assert hasattr(surface, "overlay")
