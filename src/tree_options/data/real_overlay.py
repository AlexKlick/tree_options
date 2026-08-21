"""Real-data option overlay (M4-A plan §3 WS-A).

`RealOptionOverlay` implements EXACTLY the read surface
`tree_options.data.options_pit.OptionPitSurface` consumes on the synthetic
`GeneratedOptionOverlay` — publication/eligibility/contract/chain/quote
queries — so the ENTIRE M3 PIT machinery runs unchanged on real Cboe EOD
data. It is a structural (duck-typed) stand-in: `OptionPitSurface` is
annotated with the synthetic type, so static callers need a cast, but the
surface constructs and answers queries against this class unmodified.

Surface answers over real data:

- `publication_of(t)` = 09:00 America/New_York on the next
  weekend-skipping session (the adapter's declared T+1 receipt wall);
  each day file's `received_at` must equal it (fail closed at
  construction).
- eligibility is trivial per underlying (one overlay per underlying; the
  underlying is "eligible" exactly on the sessions the file covers) — the
  synthetic liquidity ranking has no real counterpart here.
- `median_dollar_volume` answers Decimal("0"): this product carries NO
  underlying volume, so the value is a declared NOT_EVALUABLE-equivalent
  sentinel, NOT a real liquidity figure. Consumers must set their
  underlying-liquidity threshold to 0 for real overlays (flagged for the
  coverage brief, plan §4); returning None would crash `AsOf` comparisons.
- the contract master is the parse-accumulated identities (listing windows
  are the OBSERVED first..last session); when constructed standalone from
  day files alone, identities are recovered by decomposing the canonical
  contract ids (root falls back to the underlying symbol in that case).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Literal, cast

from tree_options.data.cboe_eod import (
    REAL_CONTRACT_MASTER_DOMAIN,
    REAL_OPTIONS_PROVIDER,
    CboeEodError,
    CboeEodParseResult,
    PITInvariantError,
    publication_instant,
    validate_pit_invariants,
)
from tree_options.data.digest import canonical_bytes
from tree_options.schemas.market import QuoteEvent
from tree_options.schemas.options import DeliverableSpec, OptionContract
from tree_options.synth_options.generate import (
    OptionChainEntry,
    OptionDayFile,
)
from tree_options.time.calendar import NotASessionError
from tree_options.time.sessions import (
    early_close_instant,
    require_utc,
    session_close_instant,
    session_open_instant,
)


@dataclass(frozen=True)
class RealOverlaySpec:
    """The real counterpart of `OptionsOverlaySpec` — only the fields the
    PIT surface reads (world_id, quote_source) plus lineage."""

    world_id: str
    quote_source: str
    provider: str = REAL_OPTIONS_PROVIDER
    source_sha256: str = ""


@dataclass(frozen=True)
class RealExpiryMeta:
    """The expiry view `OptionPitSurface.live_expiries_as_of` reads
    (`.expiration` only, mirroring the synthetic `_ExpiryMeta`)."""

    expiration: date


class RealSessionCalendar:
    """SessionCalendar over the overlay's own sessions (derived from the day
    files). `session_close` honors the early-close classification embedded
    in the files (13:00 ET when every entry lacks the 15:45 snapshot), so a
    decision instant always equals the EOD exchange timestamp of that
    session's snapshots."""

    def __init__(self, sessions: tuple[date, ...], early_closes: frozenset[date]) -> None:
        self._sessions = sessions
        self._ordinals = {d: i for i, d in enumerate(sessions)}
        self._early_closes = early_closes
        self.name = "cboe-eod-real"

    def sessions(self) -> tuple[date, ...]:
        return self._sessions

    def is_session(self, d: date) -> bool:
        return d in self._ordinals

    def ordinal(self, d: date) -> int:
        try:
            return self._ordinals[d]
        except KeyError:
            raise NotASessionError(f"{d} is not a session in {self.name}") from None

    def nth_after(self, d: date, n: int) -> date:
        if n < 0:
            raise ValueError(f"nth_after requires n >= 0, got {n}")
        idx = self.ordinal(d) + n
        if idx >= len(self._sessions):
            raise NotASessionError(
                f"only {len(self._sessions) - self.ordinal(d)} sessions remain after {d}"
            )
        return self._sessions[idx]

    def session_open(self, d: date) -> datetime:
        self.ordinal(d)  # fail closed on unknown session
        return session_open_instant(d)

    def session_close(self, d: date) -> datetime:
        self.ordinal(d)  # fail closed on unknown session
        if d in self._early_closes:
            return early_close_instant(d)
        return session_close_instant(d)

    def contains_instant(self, d: date, ts: datetime) -> bool:
        ts = require_utc(ts, what="execution timestamp")
        return self.session_open(d) <= ts <= self.session_close(d)


def _decompose_contract_id(contract_id: str) -> OptionContract:
    """Rebuild a contract from its canonical id when no parsed master is
    available (standalone/hand-built day files): OPT-{sid}-{yymmdd}-{C/P}-{8d}.
    The root is unknown in the id, so it falls back to the underlying."""
    parts = contract_id.split("-")
    if len(parts) < 5 or not parts[0] == "OPT":
        raise CboeEodError(f"unparseable contract id: {contract_id}")
    strike_cents = parts[-1]
    call_put = parts[-2]
    yymmdd = parts[-3]
    sid = "-".join(parts[1:-3])
    if (
        len(strike_cents) != 8
        or not strike_cents.isdigit()
        or call_put not in ("C", "P")
        or len(yymmdd) != 6
        or not yymmdd.isdigit()
        or not sid
    ):
        raise CboeEodError(f"unparseable contract id: {contract_id}")
    expiration = datetime.strptime(yymmdd, "%y%m%d").date()
    strike = Decimal(strike_cents).scaleb(-2)
    return OptionContract(
        contract_id=contract_id,
        option_root=sid,
        underlying_security_id=sid,
        expiration=expiration,
        strike=strike.quantize(Decimal("0.01")),
        call_put=cast('Literal["C", "P"]', call_put),
        multiplier=100,
        exercise_style="european" if sid.startswith("^") else "american",
        listing_start=date(1970, 1, 1),  # unknown without the parsed master
        listing_end=date(1970, 1, 1),
        deliverable=DeliverableSpec(shares_per_contract=Decimal("100")),
        standard_contract_flag=True,
        corporate_action_id=None,
    )


class RealOptionOverlay:
    """The M3 overlay surface over parsed Cboe EOD day files."""

    def __init__(
        self,
        day_files: dict[date, OptionDayFile],
        *,
        source_sha256: str,
        contracts: tuple[OptionContract, ...] | None = None,
    ) -> None:
        underlying = {f.underlying_security_id for f in day_files.values()}
        if len(underlying) > 1:
            raise CboeEodError(f"day files span multiple underlyings: {sorted(underlying)}")
        self.underlying_security_id = next(iter(underlying), "")
        self.source_sha256 = source_sha256
        self._sessions: tuple[date, ...] = tuple(sorted(day_files))
        for session in self._sessions:
            expected = publication_instant(session)
            if day_files[session].received_at != expected:
                raise CboeEodError(
                    f"session {session}: received_at {day_files[session].received_at}"
                    f" != T+1 publication wall {expected}"
                )
        validate_pit_invariants(day_files)
        self._day_files = dict(day_files)
        self._entries: dict[date, dict[str, OptionChainEntry]] = {
            session: {e.contract_id: e for e in file.entries} for session, file in day_files.items()
        }
        early = frozenset(
            session
            for session, file in day_files.items()
            if file.entries and all(e.quote_1545 is None for e in file.entries)
        )
        self.calendar = RealSessionCalendar(self._sessions, early)
        if contracts is None:
            ids = {e.contract_id for m in self._entries.values() for e in m.values()}
            master = tuple(_decompose_contract_id(cid) for cid in sorted(ids))
        else:
            master = tuple(contracts)
        self._contracts: dict[str, OptionContract] = {c.contract_id: c for c in master}
        self._expiries: dict[date, tuple[date, ...]] = {
            session: tuple(
                sorted(
                    {self._contracts[cid].expiration for cid in mapping if cid in self._contracts}
                )
            )
            for session, mapping in self._entries.items()
        }
        ladder_sets: dict[date, set[Decimal]] = {}
        for contract in self._contracts.values():
            ladder_sets.setdefault(contract.expiration, set()).add(contract.strike)
        self._ladders: dict[date, tuple[Decimal, ...]] = {
            expiry: tuple(sorted(strikes)) for expiry, strikes in ladder_sets.items()
        }
        windows: dict[date, list[date]] = {}
        for contract in self._contracts.values():
            # listing_end is schema-Optional but the model validator requires
            # it set (== expiration when dead); the `or` is a typed no-op.
            end = contract.listing_end or contract.expiration
            span = windows.setdefault(contract.expiration, [contract.listing_start])
            span[0] = min(span[0], contract.listing_start)
            span.append(end)
        self._expiry_windows: dict[date, tuple[date, date]] = {
            expiry: (min(dates), max(dates)) for expiry, dates in windows.items()
        }
        self.spec = RealOverlaySpec(
            world_id=(
                f"cboe-eod/{self.underlying_security_id}/{source_sha256[:12]}"
                if self.underlying_security_id
                else "cboe-eod/none"
            ),
            quote_source=REAL_OPTIONS_PROVIDER,
            source_sha256=source_sha256,
        )

    # ---- eligibility / sessions -----------------------------------------

    def world_sessions(self) -> tuple[date, ...]:
        return self._sessions

    def publication_of(self, session: date) -> datetime:
        if session not in self._day_files:
            raise CboeEodError(f"no real option file for session {session}")
        return publication_instant(session)

    def eligible_on(self, session: date) -> tuple[str, ...]:
        if session in self._day_files:
            return (self.underlying_security_id,)
        return ()

    def eligible_sessions(self, sid: str) -> tuple[date, ...]:
        if sid != self.underlying_security_id:
            return ()
        return self._sessions

    def underlyings_ever_eligible(self) -> tuple[str, ...]:
        return (self.underlying_security_id,) if self.underlying_security_id else ()

    def has_file(self, sid: str, session: date) -> bool:
        return sid == self.underlying_security_id and session in self._day_files

    def has_any_file(self, session: date) -> bool:
        return session in self._day_files

    # ---- contract master --------------------------------------------------

    def contracts_for(self, sid: str) -> tuple[OptionContract, ...]:
        if sid != self.underlying_security_id:
            return ()
        return tuple(self._contracts[cid] for cid in sorted(self._contracts))

    def contract(self, contract_id: str) -> OptionContract:
        """The master row when observed; otherwise the well-formed cell of an
        observed ladder (existence vs quoting: `OptionPitSurface.contracts_as_of`
        enumerates the FULL C/P grid of every live-expiry ladder, and a real
        file legitimately quotes one side only — a synthesized cell simply has
        no entries anywhere, so every quoting path answers NOT_EVALUABLE/None,
        never fabricated quotes). Malformed or foreign ids fail closed."""
        master = self._contracts.get(contract_id)
        if master is not None:
            return master
        try:
            candidate = _decompose_contract_id(contract_id)
        except CboeEodError:
            raise ValueError(f"unknown contract: {contract_id}") from None
        if candidate.underlying_security_id != self.underlying_security_id:
            raise ValueError(f"unknown contract: {contract_id}")
        ladder = self._ladders.get(candidate.expiration)
        if ladder is None or candidate.strike not in ladder:
            raise ValueError(f"unknown contract: {contract_id}")
        start, end = self._expiry_windows[candidate.expiration]
        return candidate.model_copy(
            update={
                "listing_start": start,
                "listing_end": end,
                "option_root": self.underlying_security_id,
            }
        )

    def contract_count(self) -> int:
        return len(self._contracts)

    def contract_master_sha256(self) -> str:
        digest = hashlib.sha256()
        digest.update(REAL_CONTRACT_MASTER_DOMAIN)
        for cid in sorted(self._contracts):
            digest.update(canonical_bytes(self._contracts[cid]))
        return digest.hexdigest()

    def ladder_for(self, sid: str, expiration: date) -> tuple[Decimal, ...]:
        if sid != self.underlying_security_id:
            raise ValueError(f"{sid} has no expiry {expiration}")
        ladder = self._ladders.get(expiration)
        if ladder is None:
            raise ValueError(f"{sid} has no expiry {expiration}")
        return ladder

    def live_expiries_on(self, sid: str, session: date) -> tuple[RealExpiryMeta, ...]:
        """Expiries with at least one chain entry in that session's FILE
        (the chain the file actually contains)."""
        if not self.has_file(sid, session):
            return ()
        return tuple(RealExpiryMeta(e) for e in self._expiries.get(session, ()))

    # ---- chains ------------------------------------------------------------

    def day_file(self, sid: str, session: date) -> OptionDayFile:
        if not self.has_file(sid, session):
            raise ValueError(f"no file for {sid} on {session}")
        return self._day_files[session]

    def entry_for(self, sid: str, session: date, contract_id: str) -> OptionChainEntry:
        if not self.has_file(sid, session):
            raise ValueError(f"no file for {sid} on {session}")
        entry = self._entries[session].get(contract_id)
        if entry is None:
            raise ValueError(f"{contract_id} not in the {session} file")
        return entry

    def median_dollar_volume(self, sid: str, session: date) -> Decimal:
        if not self.has_file(sid, session):
            raise ValueError(f"no file for {sid} on {session}")
        return Decimal("0")

    def quote_history(self, contract_id: str) -> tuple[QuoteEvent, ...]:
        if contract_id not in self._contracts:
            raise ValueError(f"unknown contract: {contract_id}")
        events: list[QuoteEvent] = []
        for session in self._sessions:
            entry = self._entries[session].get(contract_id)
            if entry is None:
                continue
            received = publication_instant(session)
            snaps = [entry.quote_1545] if entry.quote_1545 is not None else []
            snaps.append(entry.quote_eod)
            for snap in snaps:
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
                        source=self.spec.quote_source,
                    )
                )
        return tuple(events)

    # ---- canonical bytes ---------------------------------------------------

    def canonical_file_bytes(self, sid: str, session: date) -> bytes:
        """Byte-identical serialization discipline to the synthetic overlay
        (same payload shape) so manifest slice hashes are comparable."""
        file = self.day_file(sid, session)
        payload = {
            "underlying_security_id": file.underlying_security_id,
            "session": file.session.isoformat(),
            "received_at": file.received_at.isoformat(),
            "underlying_bid": str(file.underlying_bid),
            "underlying_ask": str(file.underlying_ask),
            "underlying_20d_median_dollar_volume": str(file.underlying_20d_median_dollar_volume),
            "entries": [
                {
                    "contract_id": e.contract_id,
                    "quote_1545": (
                        None
                        if e.quote_1545 is None
                        else {
                            "exchange_timestamp": e.quote_1545.exchange_timestamp.isoformat(),
                            "bid": str(e.quote_1545.bid),
                            "ask": str(e.quote_1545.ask),
                            "bid_size": e.quote_1545.bid_size,
                            "ask_size": e.quote_1545.ask_size,
                        }
                    ),
                    "quote_eod": {
                        "exchange_timestamp": e.quote_eod.exchange_timestamp.isoformat(),
                        "bid": str(e.quote_eod.bid),
                        "ask": str(e.quote_eod.ask),
                        "bid_size": e.quote_eod.bid_size,
                        "ask_size": e.quote_eod.ask_size,
                    },
                    "open_interest": e.open_interest,
                    "same_day_volume": e.same_day_volume,
                    "abs_delta": str(e.abs_delta),
                    "quote_condition": e.quote_condition,
                }
                for e in file.entries
            ],
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def build_real_overlay(result: CboeEodParseResult) -> RealOptionOverlay:
    """Wire a parse result into an overlay (day files + parsed master)."""
    return RealOptionOverlay(
        result.day_files,
        source_sha256=result.source_sha256,
        contracts=result.contracts,
    )


__all__ = [
    "PITInvariantError",
    "RealExpiryMeta",
    "RealOptionOverlay",
    "RealOverlaySpec",
    "RealSessionCalendar",
    "build_real_overlay",
]
