"""Workstream A: the lazy option-chain overlay generator (M3 plan §3.A).

The module fixture builds one small parent world (24 securities x 160
sessions, top-10 eligible) and its overlay ONCE; every behavioral test
reads from it. Determinism tests additionally build fresh instances
(same inputs -> byte-identical slices) and a subprocess (cross-process
byte identity).
"""

from __future__ import annotations

import ast
import hashlib
import math
import subprocess
import sys
from datetime import UTC, date, datetime, time
from decimal import Decimal

import pytest

from tests.conftest import REPO_ROOT
from tree_options.data.ingest import ingest_snapshot
from tree_options.synth import generate_world
from tree_options.synth.spec import WorldSpec
from tree_options.synth_options import (
    GeneratedOptionOverlay,
    OptionsOverlaySpec,
    generate_overlay,
    is_quarterly_expiry,
    strike_ladder,
)
from tree_options.time.sessions import SESSION_TIMEZONE

WORLD_ID = "m3-unit-null-901"
N_SESSIONS = 160  # covers a July 3rd early close and two quarterly expiries


def _build(world_id: str = WORLD_ID, kind: str = "null", seed: int = 901, overlay_seed: int = 901):
    from tree_options.time.calendar import StaticSessionCalendar

    calendar = StaticSessionCalendar(
        REPO_ROOT / "data" / "calendar" / "nyse_sessions_2018_01_02_2026_12_31.json",
        REPO_ROOT / "data" / "calendar" / "nyse_sessions_2018_01_02_2026_12_31.sha256",
    )
    spec = WorldSpec(
        world_id=world_id, seed=seed, kind=kind, n_securities=24, n_sessions=N_SESSIONS
    )
    world = generate_world(spec, calendar)
    snapshot = ingest_snapshot(
        world.payload, world.master, snapshot_id=spec.world_id, normalization_code_sha="0" * 64
    )
    overlay_spec = OptionsOverlaySpec(world_id=world_id, seed=overlay_seed, eligible_top_n=10)
    overlay = generate_overlay(
        spec=overlay_spec,
        bars=snapshot.bars,
        master=snapshot.master,
        actions=snapshot.actions,
        calendar=calendar,
    )
    return overlay, calendar, snapshot


@pytest.fixture(scope="module")
def overlay() -> GeneratedOptionOverlay:
    built, _calendar, _snapshot = _build()
    return built


@pytest.fixture(scope="module")
def calendar():
    _built, cal, _snap = _build()
    return cal


def _first_file(overlay: GeneratedOptionOverlay) -> tuple[str, date]:
    sid = overlay.underlyings_ever_eligible()[0]
    for session in sorted(overlay._file_sessions):
        if overlay.has_file(sid, session):
            return sid, session
    raise AssertionError("no file sessions for the first eligible underlying")


# ---- structure: snapshots, stamps, availability -------------------------


def test_two_snapshots_with_correct_stamps(overlay, calendar) -> None:  # type: ignore[no-untyped-def]
    sid, session = _first_file(overlay)
    file = overlay.day_file(sid, session)
    assert file.entries
    received = file.received_at
    # T+1: 09:00 America/New_York on the NEXT session
    expected_local = datetime.combine(
        calendar.nth_after(session, 1), time(9, 0), tzinfo=SESSION_TIMEZONE
    ).astimezone(UTC)
    assert received == expected_local
    for entry in file.entries:
        assert entry.quote_1545 is not None
        assert entry.quote_1545.exchange_timestamp < entry.quote_eod.exchange_timestamp
        assert entry.quote_1545.exchange_timestamp <= received
        assert entry.quote_eod.exchange_timestamp <= received


def test_early_close_session_has_eod_snapshot_only(overlay, calendar) -> None:  # type: ignore[no-untyped-def]
    # 2018-07-03 is an XNYS early close inside the fixture window
    session = date(2018, 7, 3)
    assert calendar.session_close(session).hour == 17  # 13:00 ET == 17:00 UTC
    sid = next(
        (s for s in overlay.underlyings_ever_eligible() if overlay.has_file(s, session)), None
    )
    if sid is None:
        pytest.skip("no eligible underlying on the early-close session")
    file = overlay.day_file(sid, session)
    assert file.entries
    for entry in file.entries:
        assert entry.quote_1545 is None, "15:45 does not exist on a 13:00 close"
        assert entry.quote_eod is not None


def test_last_session_has_no_file(overlay) -> None:  # type: ignore[no-untyped-def]
    last = overlay.world_sessions()[-1]
    for sid in overlay.underlyings_ever_eligible():
        assert not overlay.has_file(sid, last)


def test_quote_history_never_past_expiration(overlay) -> None:  # type: ignore[no-untyped-def]
    sid, session = _first_file(overlay)
    file = overlay.day_file(sid, session)
    contract_id = file.entries[0].contract_id
    contract = overlay.contract(contract_id)
    history = overlay.quote_history(contract_id)
    assert history
    for event in history:
        assert event.contract_id == contract_id
        assert event.exchange_timestamp.date() <= contract.expiration
        assert event.source == overlay.spec.quote_source
    received = [e.received_timestamp for e in history]
    assert received == sorted(received)


def test_quote_history_receipt_is_t1_publication(overlay) -> None:  # type: ignore[no-untyped-def]
    """Every quoted event carries the T+1 09:00 ET publication wall as its
    receipt — never the intraday exchange stamp (mutant M109)."""
    sid, session = _first_file(overlay)
    file = overlay.day_file(sid, session)
    contract_id = file.entries[0].contract_id
    for event in overlay.quote_history(contract_id):
        assert event.received_timestamp > event.exchange_timestamp, (
            "receipt is the NEXT-session publication wall, strictly after the stamp"
        )
        delay = event.received_timestamp - event.exchange_timestamp
        assert delay.total_seconds() >= 12 * 3600, f"receipt only {delay} after the stamp"
        assert (event.received_timestamp.hour, event.received_timestamp.minute) in {
            (13, 0),
            (14, 0),
        }, "receipt lands on the 09:00 America/New_York wall (13/14 UTC by DST)"


def test_zero_bid_tail_exists_and_never_negative(overlay) -> None:  # type: ignore[no-untyped-def]
    """Deep wings quantize to a ZERO bid — the tail exists — and no quoted
    bid is ever negative (mutant M113: removing the floor mints negative
    prices the engine could fill against)."""
    import itertools

    sids = overlay.underlyings_ever_eligible()
    assert sids
    bids: list[Decimal] = []
    for sid, session in itertools.islice(
        (
            (sid, session)
            for sid in sids
            for session in overlay.eligible_sessions(sid)[-40:]
        ),
        400,
    ):
        for entry in overlay.day_file(sid, session).entries:
            for snap in (entry.quote_1545, entry.quote_eod):
                if snap is not None:
                    bids.append(snap.bid)
    assert bids, "the sample must quote something"
    assert min(bids) >= Decimal("0"), "a quoted bid went negative"
    assert any(bid == Decimal("0") for bid in bids), "the zero-bid tail must exist"


def test_publication_is_dst_correct(overlay) -> None:  # type: ignore[no-untyped-def]
    """January (EST) receipts are 14:00 UTC; a July (EDT) receipt is 13:00 UTC."""
    sid, first = _first_file(overlay)
    jan_receipt = overlay.day_file(sid, first).received_at
    assert (jan_receipt.hour, jan_receipt.minute) == (14, 0)
    july = date(2018, 7, 10)  # a regular July session
    july_sid = next((s for s in overlay.eligible_on(july)), None)
    assert july_sid is not None, "some underlying must be eligible in July"
    july_receipt = overlay.day_file(july_sid, july).received_at
    assert (july_receipt.hour, july_receipt.minute) == (13, 0)


# ---- economics -----------------------------------------------------------


def test_strike_ladder_grid_and_span() -> None:
    spec = OptionsOverlaySpec(world_id="ladder-x", seed=1)
    anchor = Decimal("100.00")
    ladder = strike_ladder(anchor, spec)
    assert len(ladder) >= 5
    assert list(ladder) == sorted(set(ladder))
    for strike in ladder:
        within = (
            0.99 * float(anchor) * (1 - spec.moneyness_span)
            <= float(strike)
            <= (1 + spec.moneyness_span) * 1.01 * float(anchor)
        )
        assert within, strike
        # snapped to the exchange grid of its own price bucket
        if strike < Decimal("25"):
            assert strike == strike.quantize(Decimal("1"))
        elif strike < Decimal("200"):
            assert (strike / Decimal("2.5")) % 1 == 0
        else:
            assert strike % Decimal("5") == 0


def test_quarterly_predicate() -> None:
    assert is_quarterly_expiry(date(2018, 3, 16))  # 3rd Friday of March
    assert is_quarterly_expiry(date(2018, 6, 15))
    assert not is_quarterly_expiry(date(2018, 3, 9))  # 2nd Friday
    assert not is_quarterly_expiry(date(2018, 4, 20))  # 3rd Friday, not quarterly month


def test_premiums_quote_on_tick_and_never_crossed(overlay) -> None:  # type: ignore[no-untyped-def]
    sid, session = _first_file(overlay)
    file = overlay.day_file(sid, session)
    zero_bid = 0
    for entry in file.entries:
        for snap in (entry.quote_1545, entry.quote_eod):
            if snap is None:
                continue
            for side in (snap.bid, snap.ask):
                cents = side * 100
                assert cents == cents.to_integral_value(), side
            assert snap.bid <= snap.ask
            if snap.bid == 0:
                zero_bid += 1
    # zero-bid tails EXIST (deep wings quantize down)
    assert zero_bid > 0


def test_delta_monotone_in_strike(overlay) -> None:  # type: ignore[no-untyped-def]
    sid, session = _first_file(overlay)
    file = overlay.day_file(sid, session)
    by_expiry: dict[date, list[tuple[Decimal, Decimal, str]]] = {}
    for entry in file.entries:
        contract = overlay.contract(entry.contract_id)
        by_expiry.setdefault(contract.expiration, []).append(
            (contract.strike, entry.abs_delta, contract.call_put)
        )
    checked = 0
    for rows in by_expiry.values():
        for cp in ("C", "P"):
            series = sorted((k, d) for k, d, c in rows if c == cp)
            deltas = [d for _, d in series]
            if len(deltas) < 3:
                continue
            if cp == "C":
                assert deltas == sorted(deltas, reverse=True), (cp, series)
            else:
                assert deltas == sorted(deltas), (cp, series)
            checked += 1
    assert checked >= 2


def test_put_call_parity_within_combined_spread(overlay) -> None:  # type: ignore[no-untyped-def]
    """C - P must straddle S - K*exp(-rT) within the quoted spread (+ tick
    slack for the two roundings)."""
    import math

    sid, session = _first_file(overlay)
    file = overlay.day_file(sid, session)
    spot = (file.underlying_bid + file.underlying_ask) / 2
    pairs: dict[tuple[date, Decimal], dict[str, object]] = {}
    for entry in file.entries:
        contract = overlay.contract(entry.contract_id)
        pairs.setdefault((contract.expiration, contract.strike), {})[contract.call_put] = entry
    checked = 0
    for (expiration, strike), both in pairs.items():
        if "C" not in both or "P" not in both:
            continue
        t = max((expiration - session).days, 0.5) / 365.0
        forward_value = float(spot) - float(strike) * math.exp(-overlay.spec.risk_free * t)
        call, put = both["C"], both["P"]  # type: ignore[index]
        lower = float(call.quote_eod.bid) - float(put.quote_eod.ask)  # type: ignore[attr-defined]
        upper = float(call.quote_eod.ask) - float(put.quote_eod.bid)  # type: ignore[attr-defined]
        assert lower - 0.03 <= forward_value <= upper + 0.03, (strike, forward_value, lower, upper)
        checked += 1
    assert checked >= 5


def test_oi_concentrated_near_the_money(overlay) -> None:  # type: ignore[no-untyped-def]
    sid, session = _first_file(overlay)
    file = overlay.day_file(sid, session)
    mid = (file.underlying_bid + file.underlying_ask) / 2
    best = max(file.entries, key=lambda e: e.open_interest)
    contract = overlay.contract(best.contract_id)
    assert abs(math.log(float(mid) / float(contract.strike))) < 0.05


def test_untraded_and_liquidity_shape(overlay) -> None:  # type: ignore[no-untyped-def]
    """Within the OI-bearing band roughly half the contract-days are
    untraded; wings carry zero OI/volume (the rejection paths carry weight)."""
    sid, session = _first_file(overlay)
    file = overlay.day_file(sid, session)
    oi_rows = [e for e in file.entries if e.open_interest > 0]
    assert oi_rows, "some rows must carry open interest"
    untraded = [e for e in oi_rows if e.same_day_volume == 0]
    assert 0.2 <= len(untraded) / len(oi_rows) <= 0.8
    zero_oi = [e for e in file.entries if e.open_interest == 0]
    assert zero_oi, "wing rows must exist with zero OI"


def test_candidate_band_is_populated(overlay) -> None:  # type: ignore[no-untyped-def]
    """The §9.2 band (|delta| 0.30-0.60, OI >= 500, volume >= 100) is
    reachable on the fixture file — the strategy's target contract exists."""
    sid, session = _first_file(overlay)
    file = overlay.day_file(sid, session)
    passing = [
        e
        for e in file.entries
        if Decimal("0.30") <= e.abs_delta <= Decimal("0.60")
        and e.open_interest >= 500
        and e.same_day_volume >= 100
        and e.quote_condition == "regular"
    ]
    assert passing, "no candidate in the §9.2 band on the fixture file"


# ---- determinism ---------------------------------------------------------


def test_same_inputs_byte_identical_and_lazy_consistency() -> None:  # type: ignore[no-untyped-def]
    a, _cal, _snap = _build()
    b, _cal2, _snap2 = _build()
    sid, session = _first_file(a)
    assert a.canonical_file_bytes(sid, session) == b.canonical_file_bytes(sid, session)
    # single-entry access is byte-consistent with full-file construction
    file = a.day_file(sid, session)
    entry = a.entry_for(sid, session, file.entries[len(file.entries) // 2].contract_id)
    assert entry == file.entries[len(file.entries) // 2]
    # counts agree between two builds
    assert a.entry_and_quote_counts() == b.entry_and_quote_counts()


def test_cross_process_byte_identity(overlay) -> None:  # type: ignore[no-untyped-def]
    sid, session = _first_file(overlay)
    expected = hashlib.sha256(overlay.canonical_file_bytes(sid, session)).hexdigest()
    code = (
        "import hashlib, sys\n"
        "sys.path.insert(0, 'src')\n"
        "from datetime import date\n"
        "from tree_options.time.calendar import StaticSessionCalendar\n"
        "from tree_options.synth import generate_world\n"
        "from tree_options.synth.spec import WorldSpec\n"
        "from tree_options.data.ingest import ingest_snapshot\n"
        "from tree_options.synth_options import OptionsOverlaySpec, generate_overlay\n"
        f"cal = StaticSessionCalendar('data/calendar/nyse_sessions_2018_01_02_2026_12_31.json',"
        " 'data/calendar/nyse_sessions_2018_01_02_2026_12_31.sha256')\n"
        f"spec = WorldSpec(world_id='{WORLD_ID}', seed=901, kind='null', n_securities=24,"
        f" n_sessions={N_SESSIONS})\n"
        "world = generate_world(spec, cal)\n"
        "snap = ingest_snapshot(world.payload, world.master, snapshot_id=spec.world_id,"
        " normalization_code_sha='0'*64)\n"
        f"os_ = OptionsOverlaySpec(world_id='{WORLD_ID}', seed=901, eligible_top_n=10)\n"
        "ov = generate_overlay(spec=os_, bars=snap.bars, master=snap.master,"
        " actions=snap.actions, calendar=cal)\n"
        f"print(hashlib.sha256(ov.canonical_file_bytes('{sid}', date({session.year},"
        f" {session.month}, {session.day}))).hexdigest())\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=600,
        check=True,
    )
    assert result.stdout.strip() == expected


def test_eligibility_matches_independent_dollar_volume_oracle(overlay, calendar) -> None:  # type: ignore[no-untyped-def]
    """An independent recomputation of the rule (top-N by trailing-median
    close*volume, >= min bars, bar present at the session) names exactly
    the eligible set, across EARLY and late sessions — an unbounded or
    future-informed window diverges somewhere (mutant M110). Registry
    nulls/alphas are independent worlds — no twin-sharing is claimed or
    relied on anywhere in M3."""
    import statistics as stats

    bars_by_sid: dict[str, dict[date, tuple[Decimal, int]]] = {}
    for sid_ in overlay.underlyings_ever_eligible():
        bars_by_sid[sid_] = dict(overlay._by_sid[sid_])
    world_sessions = overlay.world_sessions()
    for index in (25, 45, 60, 80, 110, 150):
        session = world_sessions[index]
        rows: list[tuple[float, str]] = []
        for sid_ in sorted(bars_by_sid):
            window = [
                float(c) * v
                for s, (c, v) in sorted(bars_by_sid[sid_].items())
                if s <= session
            ][-overlay.spec.eligibility_window_bars :]
            if len(window) < overlay.spec.min_eligible_bars or session not in bars_by_sid[sid_]:
                continue
            rows.append((stats.median(window), sid_))
        rows.sort(key=lambda r: (-r[0], r[1]))
        expected = tuple(sorted(sid_ for _, sid_ in rows[: overlay.spec.eligible_top_n]))
        assert overlay.eligible_on(session) == expected, f"session index {index}"


def test_contracts_standard_american_expire_on_listing_end(overlay) -> None:  # type: ignore[no-untyped-def]
    sid = overlay.underlyings_ever_eligible()[0]
    contracts = overlay.contracts_for(sid)
    assert contracts
    for contract in contracts:
        assert contract.standard_contract_flag
        assert contract.exercise_style == "american"
        assert contract.multiplier == 100
        assert contract.listing_end == contract.expiration
        assert contract.underlying_security_id == sid
        assert contract.deliverable.shares_per_contract == Decimal(100)


def test_entry_refuses_out_of_file_requests(overlay, calendar) -> None:  # type: ignore[no-untyped-def]
    sid = overlay.underlyings_ever_eligible()[0]
    ineligible_session = next(
        s for s in sorted(overlay._file_sessions) if sid not in overlay.eligible_on(s)
    )
    with pytest.raises(ValueError, match="no file"):
        overlay.day_file(sid, ineligible_session)


# ---- package hygiene -----------------------------------------------------


def _import_offenders(module: str, source: str, forbidden_prefix: str) -> list[str]:
    """Every import form that reaches a module under `forbidden_prefix`."""
    offenders: list[str] = []
    tree = ast.parse(source)
    for node in ast.walk(tree):
        targets: list[str] = []
        if isinstance(node, ast.Import):
            targets = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                targets = [node.module]
            elif node.level and node.module:
                parts = module.split(".")
                parts = parts[: len(parts) - node.level]
                targets = [".".join(parts + node.module.split("."))]
            elif node.level:
                parts = module.split(".")
                targets = [".".join(parts[: len(parts) - node.level])]
        for target in targets:
            if target == forbidden_prefix or target.startswith(forbidden_prefix + "."):
                offenders.append(f"{module}: {target}")
    return offenders


def test_no_synth_import_inside_synth_options() -> None:
    """The overlay consumes the parent world's RECORDS; importing the
    equity generator would couple the two pin lanes."""
    pkg = REPO_ROOT / "src" / "tree_options" / "synth_options"
    offenders: list[str] = []
    for path in sorted(pkg.glob("*.py")):
        module = f"tree_options.synth_options.{path.stem}"
        offenders.extend(_import_offenders(module, path.read_text(), "tree_options.synth"))
    assert not offenders, offenders


def test_synth_options_is_stdlib_only() -> None:
    """numpy/pytest imports would break the stdlib-only pin discipline."""
    pkg = REPO_ROOT / "src" / "tree_options" / "synth_options"
    for path in sorted(pkg.glob("*.py")):
        source = path.read_text()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            targets: list[str] = []
            if isinstance(node, ast.Import):
                targets = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                targets = [node.module]
            for target in targets:
                root = target.split(".")[0]
                assert root not in {"numpy", "pytest", "hypothesis", "scipy"}, (path.name, target)


def test_truth_sidecar_import_boundary() -> None:
    """OptionsOverlayTruth is unreachable outside synth_options.* — mirrors
    the v1 WorldTruth boundary (ground truth never reaches feature code)."""
    src_root = REPO_ROOT / "src" / "tree_options"
    offenders: list[str] = []
    for path in sorted(src_root.rglob("*.py")):
        module = ".".join(path.relative_to(path.parents[2]).with_suffix("").parts)
        if module.startswith("tree_options.synth_options"):
            continue
        offenders.extend(
            _import_offenders(module, path.read_text(), "tree_options.synth_options.truth")
        )
    assert not offenders, offenders
