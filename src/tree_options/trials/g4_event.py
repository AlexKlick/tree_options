"""The G4 sealed-event lane machinery: the library seam the runner delegates
to (authored on ``m4/g4-sealed-machinery-20260829``).

WHAT THIS IS. The pre-declared M4-G4 sealed gate
(``docs/m4-g4-sealed-gate-plan.md`` §3-4, transcribed at
``data/g4/sealed-criteria.json``) evaluates six criteria over BOTH real
lanes from stamped payload files only. This module builds the two lane
worlds and runs the trials; the verdict lives in
``tree_options.seal.g4_gate`` (criteria + verdict + evidence). The
production entry is ``RepoCalendarSealedRunner.__call__``
(``tree_options.seal.runner``), which hands this module the SAME immutable
held-byte bundle that passed ``verify_sealed_inputs`` — the machinery never
re-reads the original input paths. Held bytes are materialized verbatim
into a scratch directory (the manifest's own fail-closed verify then runs
over exactly those bytes), so an unverified path can never be consumed.

THE HARD FIREWALL (the lane's own binding constraint): the machinery's
first execution against the REAL era artifacts (``artifacts/m4b-coverage-
era/``, ``artifacts/bars/``) or the real Cboe source IS the sealed event.
This module was authored and tested exclusively against SYNTHETIC fixture
captures (the ``era_world`` / ``era_friday_world`` fixture shapes in
``tests/unit/test_trials_options_run.py``); no smoke run, coverage peek,
or criterion dry-run ever touched a real payload.

THE LANE-2 WORLD, built exactly as the era tests build it:
``load_derived_surface(capture_dir)`` (the manifest verify is fail-closed
inside it) -> the ``VwapPitSurface`` wiring (decision grid + exchange
calendar + the protocol's declared liquidity term + the optional declared
v2 dollar-volume source) -> ``run_options_trial(liquidity_lane=2, ...)``
with:

- scores = the G5 null generator (``trials.null_score.null_scored_labels``)
  under DECLARED seeds — ``g4-sealed-null-alpha`` (lane 2, arm A),
  ``g4-sealed-null-beta`` (lane 2, arm B), ``g4-sealed-null-gamma``
  (lane 1) — fixed literals riding the config hash; the gate validates
  MACHINERY, not signal;
- flow threshold = the PROTOCOL's ratified value (NO override — the caller
  passes ``flow_min_session_volume=None`` so ``_volume_flow_threshold``
  reads the protocol; criterion 2 pins the stamped run to the 0.2.1
  amendment value EXACTLY);
- fold geometry = the parameterized ``OptionsSplitOverride`` defaulting to
  the Agenda-D proposal (``theory-menu.md`` §0.5 item 4: min_train ~40
  Fridays, val 12, test 13, roll 13, embargo >= 2, H=5 — grid Fridays);
  every knob rides the config hash, and the owner ratifies the values at
  head declaration;
- arms A and B (arm A carries the fills, arm B keeps the terminal
  machinery exercised under the sealed run; criterion 4 pools both arms'
  rejections per lane), mirroring the M3 gate's world x arm shape.

Lane 1 seals the ADAPTER (the retained Cboe one-session capture,
two-snapshot semantics): parse + overlay + the real manifest verifier over
the held bytes, stamped as a census payload with the FIRING parse
refusals counted and the zero-bid rows disclosed as the audit statistic
they are (the strict per-lane class map, plan §4 criterion 4).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path
from typing import Any

from tree_options.data.bars import BarRecord
from tree_options.data.massive_overlay import load_spot_proxy
from tree_options.data.vwap_pit_surface import (
    VwapPitSurface,
    load_spot_proxy_v2,
    repo_exchange_calendar,
)
from tree_options.evaluation.stats import ScoredLabel
from tree_options.options import OptionsStrategyConfig
from tree_options.protocol.holdout import FINAL_HOLDOUT_DATES
from tree_options.protocol.loader import load_protocol_bytes
from tree_options.protocol.schema import ResearchProtocol
from tree_options.protocol.stamping import build_stamp, write_artifact
from tree_options.registry.sqlite import TrialRegistry
from tree_options.schemas.common import PRICE_TICK
from tree_options.seal.verified_inputs import HeldVerifiedSealedInputs
from tree_options.time.calendar import SessionCalendar
from tree_options.trials.null_score import NULL_SCORE_MODEL_FAMILY, null_score
from tree_options.trials.options_run import OptionsSplitOverride, run_options_trial

# ---- the DECLARED machinery constants (fixed literals, stamped + hashed) --------

G4_SEALED_EVENT_ID = "m4-g4-sealed/1"
G4_SEALED_SEED_LANE2_ARM_A = "g4-sealed-null-alpha"
G4_SEALED_SEED_LANE2_ARM_B = "g4-sealed-null-beta"
G4_SEALED_SEED_LANE1 = "g4-sealed-null-gamma"
G4_SEALED_SEEDS: tuple[str, ...] = (
    G4_SEALED_SEED_LANE2_ARM_A,
    G4_SEALED_SEED_LANE2_ARM_B,
    G4_SEALED_SEED_LANE1,
)
LANE2_ARMS: tuple[str, ...] = ("A", "B")

# The declared null-label constant: the sealed gate judges DISCIPLINE, not
# signal (no pre-declared criterion consumes a performance statistic), and
# the null score model carries no feature panel — the label rides the
# declared configuration and every stamped payload discloses it.
G4_NULL_LABEL = 0.01

# (theory-menu §0.5 item 4, "Agenda D") the PROPOSED geometry the owner
# ratifies at head declaration: H=5, embargo=2, val=12, test=13, roll=13,
# min_train=40 — every value in GRID FRIDAYS.
AGENDA_D_GEOMETRY = (5, 2, 12, 13, 13, 40)

# The 0.2.1 amendment value (owner deviation bound to census 43b0b040…):
# criterion 2 pins the stamped run's flow threshold to this literal EXACTLY.
FLOW_MIN_SESSION_VOLUME_AMENDMENT = 100

# The era driver's staleness window (``era_world``/``era_friday_world`` in
# the era-twin tests): a long window so cells inside each contract's
# observed listing window stay fresh across the multi-month capture.
G4_STALENESS_SESSIONS = 400

# a FIXED clock: stamped artifacts are byte-deterministic, which is what
# criterion 5's clean-clone replay comparison relies on (the M3 pattern).
G4_FIXED_CLOCK = datetime(2026, 8, 29, 12, 0, 0, tzinfo=UTC)

# per-row parse refusals (lane 1's counted class under the strict map): the
# parser's own issue-line shape for a row it refused
_ROW_REFUSAL_PATTERN = re.compile(r"^row \d+: .+ — refused$")


def sealed_split_override(
    geometry: tuple[int, int, int, int, int, int] = AGENDA_D_GEOMETRY,
) -> OptionsSplitOverride:
    """The sealed geometry as the trial driver's ``OptionsSplitOverride``
    (H, E, val, test, roll, min_train), parameterized so the owner ratifies
    the values at head declaration; every knob rides the config hash."""
    label_horizon, embargo, val, test, roll, min_train = geometry
    return OptionsSplitOverride(
        label_horizon_sessions=label_horizon,
        embargo_sessions=embargo,
        val_sessions=val,
        test_sessions=test,
        roll_sessions=roll,
        min_train_sessions=min_train,
    )


# ---- held-byte materialization (the runner never re-reads input paths) ----------


def materialize_held_lane2(held: HeldVerifiedSealedInputs, scratch: Path) -> Path:
    """Write the held lane-2 bytes verbatim into one scratch capture dir.

    The manifest is written under its pinned name and every referenced
    payload at its manifest-relative logical path, so the fail-closed
    ``load_derived_surface`` manifest verify runs over EXACTLY the verified
    bytes — an unlisted or tampered scratch file refuses there, never loads
    silently."""
    capture = scratch / "lane2-capture"
    capture.mkdir(parents=True)
    (capture / "capture_manifest.json").write_bytes(held.lane2_manifest_bytes)
    for payload in held.lane2_payloads:
        target = capture / payload.logical_id
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload.raw)
    return capture


def materialize_held_lane1(held: HeldVerifiedSealedInputs, scratch: Path) -> Path:
    """Write the held lane-1 Cboe source bytes verbatim (the manifest bytes
    stay held — the machinery parses them directly)."""
    if len(held.lane1_payloads) != 1:
        raise ValueError(
            f"expected exactly one held lane-1 source payload, got {len(held.lane1_payloads)}"
        )
    lane1 = scratch / "lane1"
    lane1.mkdir(parents=True)
    source = lane1 / "cboe-source.csv"
    source.write_bytes(held.lane1_payloads[0].raw)
    return source


# ---- the lane-2 world ------------------------------------------------------------


@dataclass(frozen=True)
class Lane2World:
    """The sealed lane-2 world, built from held bytes exactly as the era
    tests build theirs (no network, no client, no real-path reads)."""

    world_id: str
    grid: SessionCalendar
    overlay: Any
    surface: VwapPitSurface
    dataset: Any
    decision_sessions: tuple[date, ...]
    scored_by_arm: dict[str, tuple[ScoredLabel, ...]]
    spot_v2_declared: bool
    # boundary-quantization custody: how many spot-proxy closes the flat
    # dataset bars moved to the cent tick, and the largest |close - tick|
    # distance among them (None when no row needed quantizing)
    spot_close_quantized_rows: int
    spot_close_max_quantization_delta: Decimal | None


def _friday_grid(overlay: Any, exchange: Any) -> SessionCalendar:
    """The Friday-only decision grid over the capture's own span: the NYSE
    fixture's Friday sessions inside [first overlay session, last overlay
    session], carrying the fixture's early closes (the era-twin grid shape
    — early closes are SESSIONS, the enumeration is unchanged by them)."""
    from tree_options.data.real_overlay import RealSessionCalendar
    from tree_options.time.expiries import is_friday

    sessions = overlay.calendar.sessions()
    fridays = [
        session
        for session in exchange.sessions()
        if is_friday(session) and sessions[0] <= session <= sessions[-1]
    ]
    if not fridays:
        raise ValueError(
            "the capture spans no exchange Friday: no decision grid exists for"
            " the sealed lane-2 run"
        )
    early = frozenset(exchange.early_close_sessions())
    return RealSessionCalendar(tuple(fridays), early & frozenset(fridays))


def _lane2_decision_sessions(grid: SessionCalendar) -> tuple[date, ...]:
    """Grid Fridays through the last one STRICTLY BEFORE the first sealed
    holdout date (the era profile's boundary rule: sealed-window decision
    sessions are excluded; the runner refuses them in test windows anyway),
    with the execution-tail headroom the fold runner's END_BUFFER needs:
    the deepest execution mark is 6 grid Fridays past the last decision
    session (the era profile's 2026-05-01 -> 2026-06-26 shape)."""
    from tree_options.trials.options_run import END_BUFFER_SESSIONS

    first_sealed = date.fromisoformat(min(FINAL_HOLDOUT_DATES))
    sessions = grid.sessions()
    last_with_tail = sessions[max(0, len(sessions) - 1 - END_BUFFER_SESSIONS)]
    return tuple(
        session for session in sessions if session < first_sealed and session <= last_with_tail
    )


def _lane2_scored_rows(
    seed: str, sessions: Sequence[date], underlyings: Sequence[str]
) -> tuple[ScoredLabel, ...]:
    """One null-scored row per (decision session, underlying) under the one
    declared seed — the G5 generator's own scores (the runner's P1-3 seed
    binding verifies every row against the generator)."""
    return tuple(
        ScoredLabel(
            security_id=underlying,
            session=session,
            score=null_score(seed=seed, session=session, security_id=underlying),
            label=G4_NULL_LABEL,
        )
        for session in sessions
        for underlying in underlyings
    )


def build_lane2_world(
    held: HeldVerifiedSealedInputs,
    *,
    repo_root: Path,
    scratch: Path,
    protocol: ResearchProtocol,
    spot_v2_path: Path | None = None,
    staleness_sessions: int = G4_STALENESS_SESSIONS,
) -> Lane2World:
    """Construct the lane-2 world from the HELD lane-2 bytes.

    ``load_derived_surface`` re-runs the fail-closed manifest verify over
    the materialized verified bytes; the surface carries the decision grid,
    the bound exchange calendar, the protocol's declared liquidity term,
    and the OPTIONAL declared v2 dollar-volume source (a file the caller
    declares; never fabricated)."""
    from tree_options.data.massive_overlay import load_derived_surface

    capture = materialize_held_lane2(held, scratch)
    # (remediation-3, owner ruling 2026-09-02) the v2 sidecar is loaded
    # BEFORE the overlay so its DAILY closes can feed the derivation —
    # event-3 failed criterion 2 precisely because the Friday-only v1 proxy
    # left every T+1-visible Thursday cell spot-less (no_in_band_strike
    # 312/312, zero candidates). The surface keeps the same mapping for the
    # liquidity term; the overlay consults the closes first, v1 backstops.
    spot_v2 = None
    spot_v2_declared = spot_v2_path is not None
    if spot_v2_path is not None:
        spot_v2 = load_spot_proxy_v2(spot_v2_path)
    overlay = load_derived_surface(capture, staleness_sessions=staleness_sessions, spot_v2=spot_v2)
    exchange = repo_exchange_calendar(repo_root)
    grid = _friday_grid(overlay, exchange)
    decision_sessions = _lane2_decision_sessions(grid)
    if not decision_sessions:
        raise ValueError("no unsealed decision sessions on the grid")
    lf = protocol.option_candidate_defaults.liquidity_volume_flow
    if lf is None:
        raise ValueError("the protocol carries no liquidity_volume_flow block")
    spot = load_spot_proxy(capture / "spot_proxy.json")
    surface = VwapPitSurface(
        overlay,
        spot=spot,
        spot_v2=spot_v2,
        exchange_calendar=exchange,
        decision_calendar=grid,
        underlying_liquidity_term=lf.underlying_liquidity_term,
    )
    world_id = overlay.spec.world_id
    publication_of = overlay.publication_of
    # The spot-proxy close is the VENDOR's exact token; the fabricated flat
    # dataset bar is a Price (2dp) by the shared schema's design. Real wire
    # closes sometimes carry a third decimal (the 2026-08-31 sealed event
    # crashed here on ADBE "417.125"), so a close whose wire EXPONENT
    # exceeds the cent tick is quantized to cents AT THE BarRecord BOUNDARY
    # with the shared PRICE_TICK and an EXPLICIT ROUND_HALF_EVEN — a bare
    # ``quantize`` would inherit the mutable decimal CONTEXT's rounding, and
    # a stamped payload's ties must not depend on ambient process state.
    # Rows already on the cent grid (exponent >= -2, any representation)
    # pass through as the ORIGINAL object — bit-identical, zero custody, no
    # representation rewrite (an exponent-0 "417" stays "417", never
    # "417.00"). Every row the boundary REWRITES is counted in custody
    # (including a trailing-zero "600.120", whose value does not move — its
    # delta is 0.000 and only the representation tightened); max_delta is
    # the largest VALUE movement among them, None when nothing was
    # rewritten. The SURFACE keeps the vendor-exact close untouched
    # (``spot`` above feeds it verbatim), and no consumer compares a
    # dataset bar close against the surface spot.
    spot_close_quantized_rows = 0
    spot_close_max_quantization_delta: Decimal | None = None
    bars: list[BarRecord] = []
    for underlying, sessions in spot.items():
        for session, close in sorted(sessions.items()):
            exponent = close.as_tuple().exponent
            if not isinstance(exponent, int):
                # a NaN/Infinity special carries no exponent at all: it can
                # never become a Price, and the loader's positive-token
                # contract means one here is corruption — refuse it named
                raise ValueError(
                    f"spot close {close} for {underlying} on {session:%Y-%m-%d} is"
                    " not a finite decimal — refusing the row"
                )
            if exponent < -2:
                quantized = close.quantize(PRICE_TICK, rounding=ROUND_HALF_EVEN)
                spot_close_quantized_rows += 1
                delta = abs(close - quantized)
                if (
                    spot_close_max_quantization_delta is None
                    or delta > spot_close_max_quantization_delta
                ):
                    spot_close_max_quantization_delta = delta
            else:
                quantized = close
            if quantized <= 0:
                # a positive sub-cent close would quantize to 0.00, which
                # Price (gt=0) can never carry (as would any non-positive
                # close): refuse naming the row rather than silently
                # dropping or flooring it
                raise ValueError(
                    f"spot close {close} for {underlying} on {session:%Y-%m-%d} quantizes"
                    f" to {quantized} at tick {PRICE_TICK}: a positive sub-cent close"
                    " cannot become a Price (gt=0) — refusing rather than"
                    " flooring the row to zero"
                )
            bars.append(
                BarRecord(
                    security_id=underlying,
                    session=session,
                    open=quantized,
                    high=quantized,
                    low=quantized,
                    close=quantized,
                    volume=1,
                    source="spot-proxy/declared",
                    source_record_id=f"{underlying}-{session:%Y%m%d}",
                    source_row_hash="0" * 64,
                    snapshot_id=world_id,
                    available_at=publication_of(session),
                )
            )
    dataset_bars = tuple(bars)
    if not dataset_bars:
        raise ValueError("the declared spot proxy carries no sessions: no dataset bars")

    @dataclass(frozen=True)
    class _Lane2Dataset:
        """The dataset slice the runner and its backtest read (snapshot
        identity + underlying bars from the DECLARED spot proxy — the
        era-twin ``_RealLaneDataset`` shape)."""

        snapshot_id: str
        bars: tuple[BarRecord, ...]
        actions: tuple[object, ...] = ()

    dataset = _Lane2Dataset(snapshot_id=world_id, bars=dataset_bars)
    underlyings = tuple(overlay.underlyings)
    scored_by_arm = {
        "A": _lane2_scored_rows(G4_SEALED_SEED_LANE2_ARM_A, decision_sessions, underlyings),
        "B": _lane2_scored_rows(G4_SEALED_SEED_LANE2_ARM_B, decision_sessions, underlyings),
    }
    return Lane2World(
        world_id=world_id,
        grid=grid,
        overlay=overlay,
        surface=surface,
        dataset=dataset,
        decision_sessions=decision_sessions,
        scored_by_arm=scored_by_arm,
        spot_v2_declared=spot_v2_declared,
        spot_close_quantized_rows=spot_close_quantized_rows,
        spot_close_max_quantization_delta=spot_close_max_quantization_delta,
    )


def lane2_census_payload(
    world: Lane2World,
    *,
    held: HeldVerifiedSealedInputs,
    protocol: ResearchProtocol,
    spot_v2_path: Path | None,
    split_override: OptionsSplitOverride,
) -> dict[str, object]:
    """The lane-2 census payload: the stamped facts the criteria evaluate
    (manifest integrity counts, the overlay's derived-cell census under the
    strict class map, the calendar identities, the declared seeds and
    geometry, and the protocol node values the run pinned)."""
    from tree_options.data.massive_overlay import DERIVATION_PROVENANCE

    overlay = world.overlay
    stats = overlay.derived_stats()
    zero_volume = 0
    derivation_errors = 0
    for cell in overlay.derived_quotes():
        reason = cell.reason or ""
        if reason.startswith("refused: zero-volume"):
            zero_volume += 1
        elif reason.startswith("refused: ") and not reason.startswith(
            ("refused: no spot proxy", "refused: bar after expiration")
        ):
            # the MassiveDerivationError class: cells the DERIVATION itself
            # refused (the solver's own fail-closed reasons — implied-vol
            # bounds and friends — surface as "refused: <message>")
            derivation_errors += 1
    lf = protocol.option_candidate_defaults.liquidity_volume_flow
    masters_files = sum(1 for p in held.lane2_payloads if p.kind == "master")
    bars_files = sum(1 for p in held.lane2_payloads if p.kind == "bar")
    census: dict[str, object] = {
        "lane": 2,
        "event": G4_SEALED_EVENT_ID,
        "world_id": world.world_id,
        "manifest": {
            "capture_version": "m4b-capture/1",
            "listed_files": len(held.lane2_payloads),
            "held_payloads": len(held.lane2_payloads),
            "masters_files": masters_files,
            "bars_files": bars_files,
            "spot_proxy_files": sum(1 for p in held.lane2_payloads if p.kind == "spot_proxy"),
            "typed_manifest_content_hash": held.packet.lane2_manifest.typed_manifest_content_hash,
            # the load-side verify re-ran fail-closed over the materialized
            # verified bytes (load_derived_surface refused otherwise — an
            # unlisted scratch file can never load)
            "verified": True,
            "verified_series": stats.contracts,
        },
        "derived_census": {
            "contracts": stats.contracts,
            "sessions": stats.sessions,
            "bars": stats.bars,
            "cells": stats.cells,
            "derived_ok": stats.derived_ok,
            "not_evaluable_stale": stats.not_evaluable_stale,
            "not_evaluable_nobar": stats.not_evaluable_nobar,
            "not_evaluable_refused": stats.not_evaluable_refused,
        },
        # the bar-boundary quantization custody (Decimal facts stringified,
        # the stamped-payload idiom): how many spot-proxy closes the flat
        # dataset bars REWROTE to the cent tick (a trailing-zero rewrite
        # moves no value and counts with delta 0.000), the largest value
        # movement among them, the tick, and the tie rule — rows already on
        # the grid pass through untouched and the SURFACE keeps the
        # vendor-exact closes
        "spot_close_quantization": {
            "rows_quantized": world.spot_close_quantized_rows,
            "max_delta": (
                str(world.spot_close_max_quantization_delta)
                if world.spot_close_max_quantization_delta is not None
                else None
            ),
            "tick": str(PRICE_TICK),
            "rule": (
                "a close whose wire exponent exceeds the cent tick is quantized"
                " to PRICE_TICK at the BarRecord boundary with an explicit"
                " ROUND_HALF_EVEN (never the mutable context default); a row"
                " already on the grid passes through as the original object"
            ),
        },
        # the STRICT per-lane class map (plan §4 criterion 4): the counted
        # classes, the disclosed-not-counted class, and the audit statistics
        "rejection_classes": {
            "zero_volume_bar_refusals": zero_volume,
            "massive_derivation_error_refusals": derivation_errors,
            "master_row_refusals": len(overlay.refused_master_contracts),
            # disclosed, NEVER counted (~availability disclosure)
            "no_bar_not_evaluable_disclosed": stats.not_evaluable_nobar,
            "stale_cells_disclosed": stats.not_evaluable_stale,
        },
        "unmatched_option_tickers": len(overlay.unmatched_option_tickers),
        "issues_count": len(overlay.issues),
        "calendars": {
            "decision_grid": {
                "n_sessions": len(world.grid.sessions()),
                "first": world.grid.sessions()[0].isoformat(),
                "last": world.grid.sessions()[-1].isoformat(),
            },
            "execution_overlay": {
                "n_sessions": len(overlay.calendar.sessions()),
                "first": overlay.calendar.sessions()[0].isoformat(),
                "last": overlay.calendar.sessions()[-1].isoformat(),
            },
        },
        "decision_window": {
            "first": world.decision_sessions[0].isoformat(),
            "last": world.decision_sessions[-1].isoformat(),
            "count": len(world.decision_sessions),
        },
        "declared_configuration": {
            "seeds": {
                "arm_a": G4_SEALED_SEED_LANE2_ARM_A,
                "arm_b": G4_SEALED_SEED_LANE2_ARM_B,
            },
            "null_label": G4_NULL_LABEL,
            "geometry": {
                "label_horizon_sessions": split_override.label_horizon_sessions,
                "embargo_sessions": split_override.embargo_sessions,
                "val_sessions": split_override.val_sessions,
                "test_sessions": split_override.test_sessions,
                "roll_sessions": split_override.roll_sessions,
                "min_train_sessions": split_override.min_train_sessions,
            },
            "geometry_source": "Agenda D proposal (theory-menu §0.5 item 4), owner-ratified at head declaration",
            "flow_min_session_volume": (lf.flow_min_session_volume if lf is not None else None),
            "underlying_liquidity_term": (lf.underlying_liquidity_term if lf is not None else None),
            "accepted_delta_provenance": (
                list(lf.abs_delta_provenance_accepted) if lf is not None else []
            ),
            # the lane's OWN derivation token (the overlay's single owner of
            # the string) — criterion 2 checks it sits in the accepted set
            "derivation_provenance": str(DERIVATION_PROVENANCE),
            "spot_v2_declared": world.spot_v2_declared,
            # the FILE NAME only (never an absolute checkout path — the
            # census payload must be byte-identical across clones)
            "spot_v2_file": (spot_v2_path.name if spot_v2_path is not None else None),
            # (remediation-3) the derivation's declared underlying spot
            # source — event-3's root cause was the Friday-only v1 proxy
            # refusing every T+1-visible Thursday cell; with the sidecar the
            # chain is v2-daily first, the v1 Friday proxy backstopping the
            # sessions the sidecar does not cover
            "derivation_spot_source": (
                "spot-proxy-v2-daily+v1-friday-backstop"
                if world.spot_v2_declared
                else "spot-proxy-v1-friday"
            ),
        },
    }
    return census


# ---- the lane-1 adapter census ---------------------------------------------------


def lane1_adapter_payload(held: HeldVerifiedSealedInputs, scratch: Path) -> dict[str, object]:
    """The lane-1 adapter census from the HELD Cboe bytes: the real parser,
    the real manifest verifier, the two-snapshot semantics, the FIRING
    parse refusals counted, and the zero-bid/zero-greeks audit statistics
    disclosed (never counted — the strict lane-1 class map)."""
    import json as _json

    from tree_options.data.cboe_eod import (
        parse_cboe_eod_csv,
        verify_real_options_manifest,
    )
    from tree_options.data.real_overlay import build_real_overlay

    manifest_raw = held.lane1_manifest_bytes
    manifest = _json.loads(manifest_raw)
    source = materialize_held_lane1(held, scratch)
    parsed = parse_cboe_eod_csv(
        source,
        variant=manifest["variant"],
        underlying=manifest["underlying_security_id"],
        raw=held.lane1_payloads[0].raw,
    )
    overlay = build_real_overlay(parsed)
    from tree_options.data.cboe_eod import RealOptionsManifest

    typed = RealOptionsManifest.model_validate_json(manifest_raw)
    verify_real_options_manifest(
        typed,
        parsed,
        overlay=overlay,
        source_bytes=held.lane1_payloads[0].raw,
    )
    firing_refusals = sum(1 for issue in parsed.issues if _ROW_REFUSAL_PATTERN.match(issue))
    sessions = overlay.world_sessions()
    with_1545 = 0
    eod_only = 0
    for session in sessions:
        for entry in overlay.day_file(manifest["underlying_security_id"], session).entries:
            if entry.quote_1545 is not None:
                with_1545 += 1
            else:
                eod_only += 1
    return {
        "lane": 1,
        "event": G4_SEALED_EVENT_ID,
        "underlying_security_id": manifest["underlying_security_id"],
        "manifest": {
            "schema_version": manifest["schema_version"],
            "provider": manifest["provider"],
            "sessions": manifest["sessions"],
            "content_sha256": manifest["content_sha256"],
            "source_sha256": manifest["source_sha256"],
            "verified": True,
            "verified_series": overlay.contract_count(),
        },
        "parse_stats": {
            "rows_total": parsed.stats.rows_total,
            "rows_mapped": parsed.stats.rows_mapped,
            "duplicate_rows": parsed.stats.duplicate_rows,
            "zero_greeks_rows": parsed.stats.zero_greeks_rows,
            "zero_bid_rows": parsed.stats.zero_bid_rows,
            "early_close_sessions": parsed.stats.early_close_sessions,
            "nonstandard_delivery_rows": parsed.stats.nonstandard_delivery_rows,
        },
        # the STRICT lane-1 class map: FIRING parse refusals only are counted
        "rejection_classes": {
            "firing_parse_refusals": firing_refusals,
            # audit statistics — reported, NEVER counted
            "zero_bid_rows_disclosed": parsed.stats.zero_bid_rows,
            "zero_greeks_rows_disclosed": parsed.stats.zero_greeks_rows,
        },
        "two_snapshot_semantics": {
            "entries_with_1545_snapshot": with_1545,
            "entries_eod_only": eod_only,
            "early_close_sessions": parsed.stats.early_close_sessions,
        },
        "contract_count": overlay.contract_count(),
        "sessions": [session.isoformat() for session in sessions],
        "issues_count": len(parsed.issues),
        "declared_configuration": {
            "seed": G4_SEALED_SEED_LANE1,
        },
    }


# ---- the sealed event run ---------------------------------------------------------


@dataclass(frozen=True)
class G4SealedRun:
    """One executed sealed-event run: the stamped payload files, the
    registry records, and the lane-2 execution calendar the fill-discipline
    criterion re-derives bar ordinals on."""

    run_id: str
    artifacts_dir: Path
    trial_payload_paths: dict[tuple[str, str], Path]  # (lane, arm) -> stamped payload
    census_payload_paths: dict[str, Path]  # lane id -> stamped census payload
    trial_statuses: dict[tuple[str, str], str]
    log_lines: tuple[str, ...]
    execution_calendar: SessionCalendar | None = None


def _stamp_write(
    payload: dict[str, object],
    *,
    path: Path,
    protocol: ResearchProtocol,
    config: dict[str, object],
    dataset_manifest_hash: str,
    repo: Path,
    allow_dirty: bool,
) -> None:
    stamp = build_stamp(
        protocol,
        trial_id=str(config.get("trial_id", G4_SEALED_EVENT_ID)),
        config=config,
        dataset_manifest_hash=dataset_manifest_hash,
        repo=repo,
        allow_dirty=allow_dirty,
    )
    write_artifact(path, payload, stamp)


def _refuse_symlinked_workspace(*locations: Path) -> None:
    """Round-2 P1-3: a directory symlink planted anywhere in a sealed
    workspace's ancestor chain (e.g. ``artifacts/g4-sealed-runs/<key>``
    itself) would redirect the registry, artifacts, scratch, and any
    sibling locations OUTSIDE the checkout while every lexical check stays
    green — the run refuses a symlinked component before a single byte is
    created, naming it. An ABSENT ancestor is fine (``mkdir(parents=True)``
    creates the chain as real directories); only a present symlink
    refuses."""
    import os
    import stat

    checked: set[Path] = set()
    for location in locations:
        for component in Path(location).absolute().parents:
            if component in checked:
                continue
            checked.add(component)
            try:
                mode = os.lstat(component).st_mode
            except OSError:
                continue  # absent ancestors are created, never followed
            if stat.S_ISLNK(mode):
                raise RuntimeError(f"refusing a symlinked sealed workspace component: {component}")


def run_g4_sealed_event(
    held: HeldVerifiedSealedInputs,
    *,
    repo_root: Path,
    registry_path: Path,
    artifacts_dir: Path,
    scratch_root: Path,
    split_override: OptionsSplitOverride | None = None,
    allow_dirty: bool = False,
    clock: Any = None,
) -> G4SealedRun:
    """Run the sealed-event trials ONCE and stamp every payload.

    One-shot discipline (the M3 pattern): an existing registry or artifacts
    directory refuses before a single byte is read, and a SYMLINKED
    workspace component refuses before a single byte is created (the
    workspace stays inside real directories). Lane 1 seals the
    adapter (census payload); lane 2 runs arms A and B through the
    unmodified trial machinery under the declared seeds and geometry.

    (remediation-3, owner ruling 2026-09-02) the v2 dollar-volume sidecar
    rides the PACKET: when the held inputs carry it, the run materializes
    the HELD bytes into this run's scratch and consumes those — never a
    caller-supplied path re-read (M262's discipline extended to the
    sidecar). A packet without the sidecar keeps the declared v1-only
    semantics."""
    if registry_path.exists():
        raise RuntimeError(f"refusing to reuse sealed registry: {registry_path}")
    if artifacts_dir.exists():
        raise RuntimeError(f"refusing to reuse sealed artifacts: {artifacts_dir}")
    _refuse_symlinked_workspace(registry_path, artifacts_dir, scratch_root)
    protocol = load_protocol_bytes(held.protocol_bytes)
    geometry = split_override if split_override is not None else sealed_split_override()
    fixed_clock: Any = (lambda: G4_FIXED_CLOCK) if clock is None else clock
    scratch = scratch_root / "g4-sealed-scratch"
    if scratch.exists():
        raise RuntimeError(f"refusing to reuse sealed scratch: {scratch}")
    scratch.mkdir(parents=True)
    spot_v2_path: Path | None = None
    if held.spot_proxy_v2_bytes is not None:
        spot_v2_path = scratch / "spot-proxy-v2.json"
        spot_v2_path.write_bytes(held.spot_proxy_v2_bytes)
    log: list[str] = []

    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry = TrialRegistry(registry_path)
    trial_paths: dict[tuple[str, str], Path] = {}
    census_paths: dict[str, Path] = {}
    statuses: dict[tuple[str, str], str] = {}
    try:
        # ---- lane 1: the adapter census (no trial — one session, T+1 wall)
        lane1_payload = lane1_adapter_payload(held, scratch)
        _stamp_write(
            lane1_payload,
            path=artifacts_dir / "lane1-adapter-census.json",
            protocol=protocol,
            config={
                "trial_id": f"{G4_SEALED_EVENT_ID}-lane1-census",
                "gate": G4_SEALED_EVENT_ID,
                "lane": 1,
                "seed": G4_SEALED_SEED_LANE1,
            },
            dataset_manifest_hash=held.packet.lane1_manifest.typed_manifest_content_hash,
            repo=repo_root,
            allow_dirty=allow_dirty,
        )
        census_paths["lane1"] = artifacts_dir / "lane1-adapter-census.json"
        log.append(
            f"LANE1_CENSUS_OK contracts={lane1_payload['contract_count']}"
            f" firing_parse_refusals="
            f"{lane1_payload['rejection_classes']['firing_parse_refusals']}"  # type: ignore[index]
        )

        # ---- lane 2: the null trials, arms A and B
        world = build_lane2_world(
            held,
            repo_root=repo_root,
            scratch=scratch,
            protocol=protocol,
            spot_v2_path=spot_v2_path,
        )
        lane2_census = lane2_census_payload(
            world,
            held=held,
            protocol=protocol,
            spot_v2_path=spot_v2_path,
            split_override=geometry,
        )
        _stamp_write(
            lane2_census,
            path=artifacts_dir / "lane2-census.json",
            protocol=protocol,
            config={
                "trial_id": f"{G4_SEALED_EVENT_ID}-lane2-census",
                "gate": G4_SEALED_EVENT_ID,
                "lane": 2,
                "seeds": [G4_SEALED_SEED_LANE2_ARM_A, G4_SEALED_SEED_LANE2_ARM_B],
                "geometry": {
                    "label_horizon_sessions": geometry.label_horizon_sessions,
                    "embargo_sessions": geometry.embargo_sessions,
                    "val_sessions": geometry.val_sessions,
                    "test_sessions": geometry.test_sessions,
                    "roll_sessions": geometry.roll_sessions,
                    "min_train_sessions": geometry.min_train_sessions,
                },
                "flow_min_session_volume": (
                    protocol.option_candidate_defaults.liquidity_volume_flow.flow_min_session_volume
                    if protocol.option_candidate_defaults.liquidity_volume_flow is not None
                    else None
                ),
            },
            dataset_manifest_hash=held.packet.lane2_manifest.typed_manifest_content_hash,
            repo=repo_root,
            allow_dirty=allow_dirty,
        )
        census_paths["lane2"] = artifacts_dir / "lane2-census.json"
        log.append(
            f"LANE2_CENSUS_OK contracts={lane2_census['derived_census']['contracts']}"  # type: ignore[index]
            f" decision_sessions={lane2_census['decision_window']['count']}"  # type: ignore[index]
        )
        for arm in LANE2_ARMS:
            seed = G4_SEALED_SEED_LANE2_ARM_A if arm == "A" else G4_SEALED_SEED_LANE2_ARM_B
            result = run_options_trial(
                dataset=world.dataset,  # type: ignore[arg-type]
                surface=world.surface,  # type: ignore[arg-type]
                calendar=world.grid,
                execution_calendar=world.overlay.calendar,
                protocol=protocol,
                world_id=world.world_id,
                arm=arm,  # type: ignore[arg-type]
                strategy_config=OptionsStrategyConfig(),
                scored=world.scored_by_arm[arm],
                model_family=NULL_SCORE_MODEL_FAMILY,
                model_sha256=None,
                hypothesis=(
                    f"{G4_SEALED_EVENT_ID}: arm {arm} on the massive-derived lane"
                    " (null scores under the declared seed; the gate validates"
                    " machinery, not signal)"
                ),
                decision_sessions=world.decision_sessions,
                options_manifest_hash=held.packet.lane2_manifest.typed_manifest_content_hash,
                registry=registry,
                artifacts_dir=artifacts_dir / "trials",
                repo=repo_root,
                clock=fixed_clock,
                split_override=geometry,
                liquidity_lane=2,
                score_seed=seed,
                allow_dirty=allow_dirty,
            )
            statuses[("2", arm)] = registry.status(result.trial_id)
            trial_paths[("2", arm)] = result.artifact_path
            log.append(
                f"SEALED_TRIAL lane=2 ARM={arm} STATUS={statuses[('2', arm)]}"
                f" TRIAL_ID={result.trial_id} FOLDS={result.n_folds}"
                f" POSITIONS={result.n_positions}"
            )
    finally:
        registry.close()
    return G4SealedRun(
        run_id=G4_SEALED_EVENT_ID,
        artifacts_dir=artifacts_dir,
        trial_payload_paths=trial_paths,
        census_payload_paths=census_paths,
        trial_statuses=statuses,
        log_lines=tuple(log),
        execution_calendar=world.overlay.calendar,
    )


__all__ = [
    "AGENDA_D_GEOMETRY",
    "FLOW_MIN_SESSION_VOLUME_AMENDMENT",
    "G4_FIXED_CLOCK",
    "G4_NULL_LABEL",
    "G4_SEALED_EVENT_ID",
    "G4_SEALED_SEEDS",
    "G4_SEALED_SEED_LANE1",
    "G4_SEALED_SEED_LANE2_ARM_A",
    "G4_SEALED_SEED_LANE2_ARM_B",
    "G4_STALENESS_SESSIONS",
    "LANE2_ARMS",
    "G4SealedRun",
    "Lane2World",
    "build_lane2_world",
    "lane1_adapter_payload",
    "lane2_census_payload",
    "materialize_held_lane1",
    "materialize_held_lane2",
    "run_g4_sealed_event",
    "sealed_split_override",
]
