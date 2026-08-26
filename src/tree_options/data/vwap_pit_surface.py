"""G1: the lane-2 PIT adapter — a vwap-bar read surface shaped like
`tree_options.data.options_pit.OptionPitSurface` over
`MassiveDerivedOverlay`, so the UNMODIFIED M3 machinery
(`backtest.options.run_options_backtest`, `guards.fills.FillEngine`,
`options.strategy.build_candidates`) executes against the Massive derived
(vwap-bar) lane.

Like the Cboe overlay before it, this adapter duck-types the synthetic
surface: static callers cast (the documented contract — see
`tests/unit/test_massive_overlay.py`). `MassiveDerivedOverlay.entry_for`
deliberately raises (no bid/ask, no open interest on this tier), so the
lane-1 surface's chain-entry reads cannot serve; this adapter answers the
same questions from the overlay's OWN stamped surface (derived cells and
`vwap_quote_event`), re-deriving nothing.

SURFACE METHOD SET (exactly what the unmodified consumers call):
`backtest/options.py` reads `snapshot_id`, `visible_quotes_as_of`,
`contract`, `entry_as_of`, `spot_mid_as_of`, `visible_file_session`;
`options/strategy.py` (via `build_candidates`) reads `overlay.calendar`,
`eligible_as_of`, `live_expiries_as_of`, `strike_ladder`, `entry_as_of`
(the ladder |delta| probe), `contract`, `candidate_snapshot`. The fill
engine consumes only the `VwapQuoteEvent` stream `visible_quotes_as_of`
returns — never the mark shape below.

THE DECLARED MARK (fail-closed, never a fabricated market). `entry_as_of`
answers a `VwapChainEntry` whose `quote_eod` carries the VISIBLE session's
bar VWAP on both sides. Those fields exist only because the unmodified
consumers read those names (the entry affordability clamp reads `.ask`,
the close mark reads `.bid`, arm-B elections read both); semantically this
is a MARK — the vendor's own volume-weighted average of the session's
prints — not a two-sided market, and the fill door never sees it. The
entry is non-None exactly when the visible session's cell is DERIVED (a
fresh, non-zero-volume bar whose derivation succeeded under the ratified
provenance): a missing bar, a stale cell, a zero-volume session, or a
refused derivation answers None, and the backtest's existing path counts
`mark_misses` and marks at zero. No mid is ever synthesized.

PUBLICATION WALL: reused unchanged from the overlay —
`MassiveDerivedOverlay.publication_of` IS `cboe_eod.publication_instant`
(T+1 09:00 America/New_York), so a decision at close(t) sees session t-1's
bars and a fill at 10:00 of t+1 selects bar(t) — never bar(t+1), never a
bar inside its own session.

SPOT: `spot_mid_as_of` answers the DECLARED spot proxy (the coverage-era
`spot_proxy.json` — a declared input, never a vendor quote) under the same
availability discipline the overlay applies to derivations: the session-
keyed value, else the flat form's `date.min` sentinel, else a fail-closed
`NoOptionFileError` (the consumer's own skip path). The proxy's session is
the VISIBLE session — a spot for a session whose bar is not yet published
is future data.

Candidate snapshots are built by
`massive_options.build_option_candidate_inputs` over the visible session's
derived cell (G3, protocol 0.2.0): |delta| under
`model-derived-from-vwap`, session volume from the bar, bid/ask/OI None —
the volume-flow filter's NOT_APPLICABLE rows. The dollar-volume stamp is
the overlay's declared `Decimal("0")` sentinel (this tier never calls the
equity-aggregates endpoint; the protocol's 50M minimum therefore FAILS the
rule until an owner ruling says otherwise — an honest audit row, pinned by
test). `spans_earnings` is None — the honest "no evidence" encoding (w2,
theory-panel P0-2 ruling (ii), owner ruled 2026-08-26): this lane carries
no earnings calendar, and `AsOf(value=False)` would launder a
vendor-stamped PASS "no spanning earnings" no source supports (the lane-1
`False` stamp is true only of synthetic worlds, which contain no earnings
events by construction). Under the still-current 0.2.1 protocol the
earnings rule answers NOT_EVALUABLE "missing" on this None and lane 2
trades zero; the ruled 0.2.2 packet turns the rule off, and the filter
then answers NOT_APPLICABLE "filter disabled" before ever reading the
value.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import cast

from tree_options.candidates.filters import AsOf, CandidateSnapshot
from tree_options.data.massive_options import (
    MassiveDerivedQuoteLike,
    build_option_candidate_inputs,
)
from tree_options.data.massive_overlay import (
    DERIVED,
    MassiveDerivedOverlay,
    MassiveDerivedQuote,
    MassiveOverlayError,
    vwap_quote_event,
)
from tree_options.data.options_pit import NoOptionFileError
from tree_options.schemas.market import VwapQuoteEvent, ZeroVolumeVwapError
from tree_options.schemas.options import OptionContract

SPOT_SENTINEL_SESSION = date.min  # the flat form's every-session key


@dataclass(frozen=True)
class VwapMarkQuote:
    """The EOD side of a `VwapChainEntry`: the DECLARED MARK (the visible
    session's bar VWAP) on both fields. A mark, not a market — the fill
    door consumes `visible_quotes_as_of`, never this shape."""

    bid: Decimal
    ask: Decimal


@dataclass(frozen=True)
class VwapChainEntry:
    """The `OptionChainEntry`-shaped lane-2 answer: the derived |delta| and
    the VWAP mark of one contract on the VISIBLE session. Carries no open
    interest and no two-sided quote — the fields the free tier does not
    have are simply absent, never zero-filled."""

    contract_id: str
    quote_eod: VwapMarkQuote
    abs_delta: Decimal
    same_day_volume: int


class VwapPitSurface:
    """The lane-2 PIT read surface over one `MassiveDerivedOverlay` plus
    the declared spot proxy mapping (see `massive_overlay.load_spot_proxy`).
    Fail-closed everywhere: no bar, no spot, no derivation -> None or
    `NoOptionFileError`, never a synthesized price."""

    def __init__(
        self,
        overlay: MassiveDerivedOverlay,
        *,
        spot: Mapping[str, Mapping[date, Decimal]] | None = None,
    ) -> None:
        self._overlay = overlay
        self._spot: dict[str, dict[date, Decimal]] = {
            underlying: dict(sessions) for underlying, sessions in (spot or {}).items()
        }

    # ---- identity / delegation ------------------------------------------------

    @property
    def overlay(self) -> MassiveDerivedOverlay:
        return self._overlay

    @property
    def snapshot_id(self) -> str:
        return self._overlay.spec.world_id

    def visible_file_session(self, underlying_id: str, as_of: datetime) -> date | None:
        """The latest session whose captured data for the underlying is
        published at or before as_of — the overlay's own T+1 wall, applied
        to the overlay's own eligibility sessions."""
        for session in sorted(self._overlay.eligible_sessions(underlying_id), reverse=True):
            if self._overlay.publication_of(session) <= as_of:
                return session
        return None

    def contract(self, contract_id: str) -> OptionContract:
        return self._overlay.contract(contract_id)

    def strike_ladder(self, underlying_id: str, expiration: date) -> tuple[Decimal, ...]:
        return self._overlay.ladder_for(underlying_id, expiration)

    def live_expiries_as_of(self, underlying_id: str, as_of: datetime) -> tuple[date, ...]:
        """Live expiries on the VISIBLE session (T+1 gate applied), read
        from the overlay's observed listing windows."""
        session = self.visible_file_session(underlying_id, as_of)
        if session is None:
            return ()
        return tuple(
            meta.expiration for meta in self._overlay.live_expiries_on(underlying_id, session)
        )

    def eligible_as_of(self, session: date) -> tuple[str, ...]:
        """The eligible cross-section for a decision session: the eligible
        set of the latest session whose capture is published by the close
        of the decision session (file(t-1) for a close(t) decision)."""
        decision_at = self._overlay.calendar.session_close(session)
        for candidate in sorted(self._overlay.world_sessions(), reverse=True):
            if not self._overlay.has_any_file(candidate):
                continue
            if self._overlay.publication_of(candidate) <= decision_at:
                return self._overlay.eligible_on(candidate)
        return ()

    # ---- the marks / ladder probe ----------------------------------------------

    def _visible_cell(self, contract_id: str, session: date) -> MassiveDerivedQuote | None:
        """The censused cell of the contract on `session`, or None when the
        contract is not an overlay-master contract / has no cell there (the
        lane-1 not-in-file signal — `OptionChainEntry` reads raised
        ValueError there; the overlay's own reads raise here)."""
        try:
            cells = self._overlay.derived_quotes_for(contract_id)
        except MassiveOverlayError:
            return None
        for cell in cells:
            if cell.session == session:
                return cell
        return None

    def entry_as_of(
        self, underlying_id: str, as_of: datetime, contract_id: str
    ) -> VwapChainEntry | None:
        """The visible session's DERIVED cell as the chain-entry shape the
        unmodified strategy/backtest read: |delta| for the ladder probe and
        the strike pick, the VWAP mark for sizing and the close mark.

        None whenever the cell is not DERIVED — no bar, stale, zero-volume,
        refused derivation, or not an overlay-master contract: the callers'
        own not-in-file paths (mark_misses / skip), never a guess."""
        session = self.visible_file_session(underlying_id, as_of)
        if session is None:
            raise NoOptionFileError(f"no captured session for {underlying_id} visible at {as_of}")
        cell = self._visible_cell(contract_id, session)
        if cell is None or cell.status != DERIVED:
            return None
        derived = cell.derived
        premium = cell.premium
        volume = cell.volume
        assert derived is not None and premium is not None and volume is not None
        return VwapChainEntry(
            contract_id=contract_id,
            quote_eod=VwapMarkQuote(bid=premium, ask=premium),
            abs_delta=derived.abs_delta,
            same_day_volume=volume,
        )

    # ---- the fill stream --------------------------------------------------------

    def visible_quotes_as_of(self, contract_id: str, as_of: datetime) -> tuple[VwapQuoteEvent, ...]:
        """The VISIBLE session's bar as the single-event vwap quote stream
        the fill engine selects from (the lane-1 surface's own shape: only
        the visible file's snapshots, so an older published bar can never
        win selection).

        Empty — unfillable, never fabricated — when the contract is
        unknown, nothing is published yet, the visible session has no bar
        for the contract, or the bar's volume is zero
        (`vwap_quote_event`'s own graded refusals)."""
        try:
            underlying = self._overlay.contract(contract_id).underlying_security_id
        except ValueError:
            return ()
        session = self.visible_file_session(underlying, as_of)
        if session is None:
            return ()
        cell = self._visible_cell(contract_id, session)
        if cell is None or cell.premium is None or cell.volume is None:
            return ()  # no bar that session: no trade to participate in
        try:
            return (vwap_quote_event(cell),)
        except (MassiveOverlayError, ZeroVolumeVwapError):
            # the conversion's own fail-closed refusals (a zero-volume bar
            # has no VWAP executions to participate in): the session stays
            # unfillable, never converted
            return ()

    # ---- spot -------------------------------------------------------------------

    def _spot_for(self, underlying_id: str, session: date) -> Decimal | None:
        """The overlay's own spot discipline: the session-keyed value, else
        the flat form's sentinel, else None (a derivation refuses without
        one rather than guessing — mirrored here)."""
        per_underlying = self._spot.get(underlying_id)
        if not per_underlying:
            return None
        if session in per_underlying:
            return per_underlying[session]
        return per_underlying.get(SPOT_SENTINEL_SESSION)

    def spot_mid_as_of(self, underlying_id: str, as_of: datetime) -> Decimal:
        """The declared spot proxy for the VISIBLE session (a proxy for an
        unpublished session would be future data). Fail closed — the
        consumer's own `NoOptionFileError` skip path — when nothing is
        visible or the proxy declares no spot for the visible session."""
        session = self.visible_file_session(underlying_id, as_of)
        if session is None:
            raise NoOptionFileError(f"no captured session for {underlying_id} visible at {as_of}")
        spot = self._spot_for(underlying_id, session)
        if spot is None:
            raise NoOptionFileError(
                f"no declared spot proxy for {underlying_id} on {session}:"
                " the spot proxy is a DECLARED INPUT and none was declared"
                " for that session — refusing to guess a mid"
            )
        return spot

    # ---- candidate snapshots ------------------------------------------------------

    def candidate_snapshot(
        self, contract: OptionContract, decision_session: date
    ) -> CandidateSnapshot:
        """The §9.2 snapshot from the visible session's derived cell, built
        by `build_option_candidate_inputs` (G3): |delta| under the ratified
        provenance, session volume from the bar, bid/ask/OI None. The two
        unconditional lane-1 stamps are mirrored from the overlay itself."""
        decision_at = self._overlay.calendar.session_close(decision_session)
        underlying = contract.underlying_security_id
        session = self.visible_file_session(underlying, decision_at)
        if session is None:
            raise NoOptionFileError(
                f"no captured session for {underlying} visible at {decision_at}"
            )
        received = self._overlay.publication_of(session)
        dollar_volume = AsOf(
            value=self._overlay.median_dollar_volume(underlying, session),
            available_at=received,
        )
        # (w2, theory-panel P0-2 ruling (ii), owner ruled 2026-08-26): the
        # honest "no evidence" encoding. This lane carries no earnings
        # calendar, so `AsOf(value=False)` would LAUNDER a vendor-stamped
        # PASS "no spanning earnings" no source supports. Under the
        # still-current 0.2.1 protocol the rule answers NOT_EVALUABLE
        # "missing" on this None and the candidate is refused (lane 2
        # trades zero — pinned by test); under 0.2.2
        # (`exclude_earnings_spanning_hold: false`) the filter answers
        # NOT_APPLICABLE "filter disabled" BEFORE reading the value.
        spans_earnings: AsOf | None = None
        cell = self._visible_cell(contract.contract_id, session)
        if cell is None:
            # not an overlay-master contract: the lane-1 None-inputs path
            # (the filter's NOT_EVALUABLE discipline, never a silent drop)
            return CandidateSnapshot(
                contract=contract,
                underlying_security_id=underlying,
                decision_session=decision_session,
                decision_at=decision_at,
                expiration=contract.expiration,
                abs_delta=None,
                open_interest=None,
                same_day_volume=None,
                same_day_volume_applicable=True,
                bid=None,
                ask=None,
                underlying_20d_median_dollar_volume=dollar_volume,
                spans_earnings=spans_earnings,
            )
        return build_option_candidate_inputs(
            contract,
            # the structural-slice cast: `MassiveDerivedQuoteLike` is a
            # Protocol with settable members, so the frozen dataclass that
            # implements its shape needs the static bridge (the module's
            # own one-way-dependency note explains the Protocol)
            cast(MassiveDerivedQuoteLike, cell),
            decision_session=decision_session,
            decision_at=decision_at,
            underlying_20d_median_dollar_volume=dollar_volume,
            spans_earnings=spans_earnings,
        )


__all__ = [
    "SPOT_SENTINEL_SESSION",
    "VwapChainEntry",
    "VwapMarkQuote",
    "VwapPitSurface",
]
