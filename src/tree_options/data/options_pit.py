"""Point-in-time option-chain read gate (M3 plan §3.B).

`OptionPitSurface` is the ONLY sanctioned way strategy/backtest code reads
option data. Availability is the T+1 file publication: session t's file
(describing session t's snapshots) is visible exactly from 09:00
America/New_York on session t+1 — a decision at close(t) therefore sees
file(t-1), never file(t). Fail closed when nothing is visible yet.

The candidate snapshot carries every §9.2 input as an `AsOf` whose
available_at is the file's receipt instant. `spans_earnings` is fed
`AsOf(False, receipt)`: the synthetic worlds contain NO earnings events,
so False is the TRUE value, not an imputation (M3 plan §2 correction) —
feeding None would NOT_EVALUABLE every candidate and empty the backtest.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from tree_options.candidates.filters import AsOf, CandidateSnapshot
from tree_options.schemas.market import QuoteEvent
from tree_options.schemas.options import OptionContract
from tree_options.synth_options import (
    GeneratedOptionOverlay,
    OptionChainEntry,
    OptionDayFile,
    contract_id_of,
)


class NoOptionFileError(RuntimeError):
    """No file for the underlying is visible at the requested instant."""


class OptionPitSurface:
    def __init__(self, overlay: GeneratedOptionOverlay) -> None:
        self._overlay = overlay

    @property
    def snapshot_id(self) -> str:
        return self._overlay.spec.world_id

    @property
    def overlay(self) -> GeneratedOptionOverlay:
        return self._overlay

    def visible_file_session(self, underlying_id: str, as_of: datetime) -> date | None:
        """The latest session whose file for this underlying is published
        at or before as_of — computed from publication instants only (no
        chain materialization). None when nothing is visible yet."""
        for session in sorted(self._overlay.eligible_sessions(underlying_id), reverse=True):
            if self._overlay.publication_of(session) <= as_of:
                return session
        return None

    def file_as_of(self, underlying_id: str, as_of: datetime) -> OptionDayFile:
        """The latest file for the underlying whose receipt instant is
        <= as_of. Fail closed if none (before the first T+1 publication)."""
        session = self.visible_file_session(underlying_id, as_of)
        if session is None:
            raise NoOptionFileError(f"no option file for {underlying_id} visible at {as_of}")
        return self._overlay.day_file(underlying_id, session)

    def entry_as_of(
        self, underlying_id: str, as_of: datetime, contract_id: str
    ) -> OptionChainEntry | None:
        """The contract-day entry from the latest visible file, or None if
        the contract did not quote that session (wings, pre-listing).
        Single-entry access — no full-chain materialization."""
        session = self.visible_file_session(underlying_id, as_of)
        if session is None:
            raise NoOptionFileError(f"no option file for {underlying_id} visible at {as_of}")
        try:
            return self._overlay.entry_for(underlying_id, session, contract_id)
        except ValueError:
            return None  # not live on the visible file's session

    def contracts_as_of(self, underlying_id: str, as_of: datetime) -> tuple[OptionContract, ...]:
        """Contracts quotable from the VISIBLE file — the T+1 read gate
        applies (review r1 P1-2): only contracts present in the file an
        as_of instant can actually see. Existence-only queries (INV-09
        listing windows, a session-date fact) use contracts_existing_on."""
        session = self.visible_file_session(underlying_id, as_of)
        if session is None:
            return ()
        live = self._overlay.live_expiries_on(underlying_id, session)
        contracts = []
        for meta in live:
            for strike in self._overlay.ladder_for(underlying_id, meta.expiration):
                for call_put in ("C", "P"):
                    contracts.append(
                        self._overlay.contract(
                            contract_id_of(underlying_id, meta.expiration, call_put, strike)
                        )
                    )
        contracts.sort(key=lambda c: c.contract_id)
        return tuple(contracts)

    def contracts_existing_on(
        self, underlying_id: str, session: date
    ) -> tuple[OptionContract, ...]:
        """Contracts of the underlying that EXIST on the session (INV-09
        listing window) — existence is a session-date fact, not an
        availability fact; quoting is separate."""
        return tuple(c for c in self._overlay.contracts_for(underlying_id) if c.exists_on(session))

    def live_expiries_as_of(self, underlying_id: str, as_of: datetime) -> tuple[date, ...]:
        """Live expiries on the VISIBLE file's session (the chain the file
        actually contains, T+1 gate applied)."""
        session = self.visible_file_session(underlying_id, as_of)
        if session is None:
            return ()
        return tuple(
            meta.expiration for meta in self._overlay.live_expiries_on(underlying_id, session)
        )

    def strike_ladder(self, underlying_id: str, expiration: date) -> tuple[Decimal, ...]:
        """The deterministic strike grid for one (underlying, expiry) — a
        spec/existence fact, not quote data (same class as
        contracts_existing_on)."""
        return self._overlay.ladder_for(underlying_id, expiration)

    def quote_history(self, contract_id: str) -> tuple[QuoteEvent, ...]:
        return self._overlay.quote_history(contract_id)

    def visible_quotes_as_of(self, contract_id: str, as_of: datetime) -> tuple[QuoteEvent, ...]:
        """The VISIBLE file's snapshots for the contract as QuoteEvents
        (received = that file's receipt instant) — the visible stream the
        fill engine selects from at as_of. Equivalent under select_quote to
        the full history (older files lose the (received, exchange) max)
        without materializing it. Empty when the contract did not quote on
        the visible file's session."""
        overlay = self._overlay
        try:
            underlying = overlay.contract(contract_id).underlying_security_id
        except ValueError:
            return ()
        session = self.visible_file_session(underlying, as_of)
        if session is None:
            return ()
        try:
            entry = overlay.entry_for(underlying, session, contract_id)
        except ValueError:
            return ()
        received = overlay.publication_of(session)
        events: list[QuoteEvent] = []
        for snap in (entry.quote_1545, entry.quote_eod):
            if snap is None:
                continue
            events.append(
                QuoteEvent(
                    contract_id=contract_id,
                    exchange_timestamp=snap.exchange_timestamp,
                    received_timestamp=received,
                    bid=snap.bid,
                    ask=snap.ask,
                    bid_size=snap.bid_size,
                    ask_size=snap.ask_size,
                    quote_condition=entry.quote_condition,
                    source=overlay.spec.quote_source,
                )
            )
        return tuple(events)

    def contract(self, contract_id: str) -> OptionContract:
        return self._overlay.contract(contract_id)

    def candidate_snapshot(
        self, contract: OptionContract, decision_session: date
    ) -> CandidateSnapshot:
        """Build the §9.2 snapshot from the file visible at the decision
        instant. A contract absent from that file carries None inputs —
        the filter's NOT_EVALUABLE path, never a silent drop. Builds ONE
        chain entry, never the full file."""
        decision_at = self._overlay.calendar.session_close(decision_session)
        underlying = contract.underlying_security_id
        session = self.visible_file_session(underlying, decision_at)
        if session is None:
            raise NoOptionFileError(f"no option file for {underlying} visible at {decision_at}")
        received = self._overlay.publication_of(session)
        dollar_volume = self._overlay.median_dollar_volume(underlying, session)
        try:
            entry = self._overlay.entry_for(underlying, session, contract.contract_id)
        except ValueError:
            entry = None
        if entry is None:
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
                underlying_20d_median_dollar_volume=AsOf(
                    value=dollar_volume, available_at=received
                ),
                spans_earnings=AsOf(value=False, available_at=received),
            )
        # same-day volume arrives WITH the T+1 file — always applicable
        # once the file is visible (protocol volume_only_if_already_available)
        return CandidateSnapshot(
            contract=contract,
            underlying_security_id=underlying,
            decision_session=decision_session,
            decision_at=decision_at,
            expiration=contract.expiration,
            abs_delta=AsOf(value=entry.abs_delta, available_at=received),
            open_interest=AsOf(value=entry.open_interest, available_at=received),
            same_day_volume=AsOf(value=entry.same_day_volume, available_at=received),
            same_day_volume_applicable=True,
            bid=AsOf(value=entry.quote_eod.bid, available_at=received),
            ask=AsOf(value=entry.quote_eod.ask, available_at=received),
            underlying_20d_median_dollar_volume=AsOf(value=dollar_volume, available_at=received),
            spans_earnings=AsOf(value=False, available_at=received),
        )

    def eligible_as_of(self, session: date) -> tuple[str, ...]:
        """The option-eligible cross-section for a decision session: the
        eligible set of the session whose file the decision sees (the
        file(t-1) eligible set for a close(t) decision)."""
        decision_at = self._overlay.calendar.session_close(session)
        for candidate_session in sorted(self._overlay.world_sessions(), reverse=True):
            if not self._overlay.has_any_file(candidate_session):
                continue
            if self._overlay.publication_of(candidate_session) <= decision_at:
                return self._overlay.eligible_on(candidate_session)
        return ()

    def spot_mid_as_of(self, underlying_id: str, as_of: datetime) -> Decimal:
        """The underlying mid from the latest visible file (file-level
        underlying bid/ask — strictly knowable at the instant)."""
        file = self.file_as_of(underlying_id, as_of)
        return (file.underlying_bid + file.underlying_ask) / 2
