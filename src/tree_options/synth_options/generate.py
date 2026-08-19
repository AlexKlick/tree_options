"""Deterministic lazy option-chain overlay generator (M3 plan §3.A).

`generate_overlay(spec, bars, master, actions, calendar)` is a pure
function of its inputs: name-keyed seeded `random.Random` streams, no wall
clock, no set-iteration order in any output path. The overlay NEVER
imports `tree_options.synth` — it consumes the parent world's ingested
records (bars/master/actions), which keeps the byte-frozen v1 generator
and its registry pin untouched.

Laziness is the design (plan §7 scale: naive materialization is ~10^9 rows
per world): the contract master, day files, and single chain entries are
computed on demand from deterministic per-key streams, so a single entry is
recomputable standalone and byte-identically. Full materialization happens
only on verifier anchor slices.

Structure per (underlying, session) file:
- contracts: every Friday expiry with a satisfied listing window; a
  quarterly (3rd Friday of Mar/Jun/Sep/Dec) lists at DTE <= quarterly_dte,
  any other Friday at DTE <= weekly_dte. The chain on session t carries
  the `max_live_expiries` nearest by calendar DTE.
- ladder: `n_moneyness_nodes` log-spaced nodes over +-moneyness_span of
  the close at the expiry's listing session, snapped to the $1/$2.50/$5
  exchange grid (deduplicated after snapping).
- IV: planted per (underlying, expiry), constant across sessions.
"""

from __future__ import annotations

import json
import math
import random
import statistics
from bisect import bisect_left
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from pydantic import Field

from tree_options.data.actions import CorporateActionRecord
from tree_options.data.bars import BarRecord
from tree_options.data.digest import canonical_bytes
from tree_options.schemas.common import IdStr, StrictModel, UTCDatetime
from tree_options.schemas.market import QuoteEvent
from tree_options.schemas.options import DeliverableSpec, OptionContract
from tree_options.schemas.security import SecurityMasterRecord
from tree_options.synth_options.greeks import bs_abs_delta, bs_price
from tree_options.synth_options.spec import OptionsOverlaySpec
from tree_options.synth_options.truth import OptionsOverlayTruth
from tree_options.time.calendar import SessionCalendar
from tree_options.time.expiries import (
    is_friday,
    is_third_friday_of_quarter_month,
    minus_calendar_days,
)
from tree_options.time.sessions import SESSION_TIMEZONE

CENT = Decimal("0.01")
MULTIPLIER = 100
PUB_WALL = time(9, 0)  # 09:00 America/New_York on the NEXT session (T+1)
SNAPSHOT_1545 = time(15, 45)


class OptionQuoteSnapshot(StrictModel):
    """One NBBO observation inside a session file (15:45 or the close)."""

    exchange_timestamp: UTCDatetime
    bid: Decimal = Field(ge=0)
    ask: Decimal = Field(ge=0)
    bid_size: int = Field(ge=0)
    ask_size: int = Field(ge=0)


class OptionChainEntry(StrictModel):
    """One contract-day inside a file: two snapshots + the file-level facts
    the §9.2 candidate filter consumes (OI, volume, |delta|, condition)."""

    contract_id: IdStr
    quote_1545: OptionQuoteSnapshot | None  # None on early-close sessions
    quote_eod: OptionQuoteSnapshot
    open_interest: int = Field(ge=0)
    same_day_volume: int = Field(ge=0)
    abs_delta: Decimal = Field(ge=0, le=1)
    quote_condition: IdStr


class OptionDayFile(StrictModel):
    underlying_security_id: IdStr
    session: date
    received_at: UTCDatetime  # 09:00 ET on the next session (T+1)
    underlying_bid: Decimal = Field(ge=0)
    underlying_ask: Decimal = Field(gt=0)
    underlying_20d_median_dollar_volume: Decimal = Field(ge=0)
    entries: tuple[OptionChainEntry, ...]

    def snapshot_count(self) -> int:
        return sum(2 if e.quote_1545 is not None else 1 for e in self.entries)


def _wall(session: date, wall: time) -> datetime:
    return datetime.combine(session, wall, tzinfo=SESSION_TIMEZONE).astimezone(UTC)


def _tick_ceil(x: Decimal) -> Decimal:
    return (x / CENT).to_integral_value(rounding=ROUND_CEILING) * CENT


def _tick_floor(x: Decimal) -> Decimal:
    return max((x / CENT).to_integral_value(rounding=ROUND_FLOOR) * CENT, Decimal("0.00"))


def _strike_step(price: Decimal) -> Decimal:
    if price < Decimal("25"):
        return Decimal("1")
    if price < Decimal("200"):
        return Decimal("2.5")
    return Decimal("5")


def _snap_strike(node: Decimal) -> Decimal:
    step = _strike_step(node)
    steps = (node / step).to_integral_value(rounding="ROUND_HALF_UP")
    return steps * step


def strike_ladder(anchor: Decimal, spec: OptionsOverlaySpec) -> tuple[Decimal, ...]:
    """Log-spaced moneyness nodes over +-span of the anchor close, snapped
    to the exchange grid and deduplicated (post-snap collisions merge)."""
    lo = math.log(float(anchor) * (1.0 - spec.moneyness_span))
    hi = math.log(float(anchor) * (1.0 + spec.moneyness_span))
    n = spec.n_moneyness_nodes
    nodes = [lo + (hi - lo) * i / (n - 1) for i in range(n)]
    snapped = sorted({_snap_strike(Decimal(repr(math.exp(m)))) for m in nodes})
    return tuple(snapped)


def is_quarterly_expiry(d: date) -> bool:
    """Re-exported from time.expiries (weekday arithmetic lives in time/)."""
    return is_third_friday_of_quarter_month(d)


def contract_id_of(underlying: str, expiration: date, call_put: str, strike: Decimal) -> str:
    return f"OPT-{underlying}-{expiration:%y%m%d}-{call_put}-{int(strike * 100):08d}"


@dataclass(frozen=True)
class _ExpiryMeta:
    expiration: date
    listing_start: date
    quarterly: bool


class GeneratedOptionOverlay:
    """Lazy, deterministic option chains over one frozen equity world."""

    def __init__(
        self,
        *,
        spec: OptionsOverlaySpec,
        bars: Sequence[BarRecord],
        master: Sequence[SecurityMasterRecord],
        actions: Sequence[CorporateActionRecord],
        calendar: SessionCalendar,
    ) -> None:
        self.spec = spec
        self.calendar = calendar
        self._actions = tuple(actions)
        self._master_ids = frozenset(m.security_id for m in master)
        # bars indexed per security, session-sorted (input bars are already
        # session-ordered per security; the sort makes that invariant local)
        by_sid: dict[str, dict[date, tuple[Decimal, int]]] = {}
        for bar in bars:
            if bar.security_id not in self._master_ids:
                raise ValueError(f"bar for unknown security {bar.security_id}")
            by_sid.setdefault(bar.security_id, {})[bar.session] = (bar.close, bar.volume)
        self._by_sid: dict[str, dict[date, tuple[Decimal, int]]] = by_sid
        self._sid_sorted: tuple[str, ...] = tuple(sorted(by_sid))
        # the world's session extent comes from the BARS (the parent world
        # may truncate the calendar); a file exists for every world session
        # except the last (its T+1 publication instant falls outside the
        # world — declared boundary)
        self._sessions: tuple[date, ...] = tuple(sorted({bar.session for bar in bars}))
        self._ordinal = {d: i for i, d in enumerate(self._sessions)}
        self._file_sessions: frozenset[date] = (
            frozenset(self._sessions[:-1]) if self._sessions else frozenset()
        )
        # caches (never observable in outputs)
        self._eligible: dict[date, tuple[str, ...]] = {}
        self._dv_series: dict[str, dict[date, tuple[float, int]]] = {}
        self._eligible_sessions: dict[str, tuple[date, ...]] | None = None
        self._expiries: dict[str, tuple[_ExpiryMeta, ...]] = {}
        self._ladder: dict[tuple[str, date], tuple[Decimal, ...]] = {}
        self._iv: dict[tuple[str, date], float] = {}
        self._contract_index: dict[str, tuple[str, _ExpiryMeta, Decimal, str]] = {}
        self._file_cache: dict[tuple[str, date], OptionDayFile] = {}

    # ---- eligibility ----------------------------------------------------

    def _liquidity_series(self, sid: str) -> dict[date, tuple[float, int]]:
        """Per session: (trailing-median DOLLAR volume, bars seen so far).
        Coverage ranks on the dollar median — the same liquidity concept the
        §9.2 underlying rule uses, computed PIT-honestly from the world's
        own bars (registry nulls and alphas are independent worlds: v1
        seeds every stream with world_id, so no twin-sharing is assumed)."""
        cached = self._dv_series.get(sid)
        if cached is not None:
            return cached
        series: dict[date, tuple[float, int]] = {}
        window: list[float] = []
        for session in sorted(self._by_sid[sid]):
            close, volume = self._by_sid[sid][session]
            window.append(float(close) * float(volume))
            if len(window) > self.spec.eligibility_window_bars:
                window.pop(0)
            series[session] = (statistics.median(window), len(window))
        self._dv_series[sid] = series
        return series

    def world_sessions(self) -> tuple[date, ...]:
        """The parent world's session extent (derived from its bars)."""
        return self._sessions

    def publication_of(self, session: date) -> datetime:
        """The receipt instant of session's file: 09:00 ET on the next
        world session (T+1)."""
        return self._publication(session)

    def eligible_on(self, session: date) -> tuple[str, ...]:
        """Top-N underlyings by trailing median DOLLAR volume, PIT-honest
        from the world's own bars (the trailing window closes AT session;
        the file publishes T+1, after the bars' 23:00 UTC publication)."""
        cached = self._eligible.get(session)
        if cached is not None:
            return cached
        if session not in self._file_sessions:
            self._eligible[session] = ()
            return ()
        rows: list[tuple[float, str]] = []
        for sid in self._sid_sorted:
            observed = self._liquidity_series(sid).get(session)
            if observed is None:
                continue
            median_dv, n_seen = observed
            # >= min trailing bars required (the window itself is capped)
            if n_seen < self.spec.min_eligible_bars:
                continue
            rows.append((median_dv, sid))
        rows.sort(key=lambda r: (-r[0], r[1]))
        top = tuple(sorted(sid for _, sid in rows[: self.spec.eligible_top_n]))
        self._eligible[session] = top
        return top

    def _ensure_eligibility_index(self) -> dict[str, tuple[date, ...]]:
        """One pass over file sessions building every sid's eligible-session
        list (avoiding the per-sid full-scan blowup)."""
        if self._eligible_sessions is not None:
            return self._eligible_sessions
        index: dict[str, list[date]] = {}
        for session in sorted(self._file_sessions):
            for sid in self.eligible_on(session):
                index.setdefault(sid, []).append(session)
        self._eligible_sessions = {sid: tuple(sessions) for sid, sessions in index.items()}
        return self._eligible_sessions

    def eligible_sessions(self, sid: str) -> tuple[date, ...]:
        return self._ensure_eligibility_index().get(sid, ())

    def underlyings_ever_eligible(self) -> tuple[str, ...]:
        seen: set[str] = set()
        for session in self._file_sessions:
            seen.update(self.eligible_on(session))
        return tuple(sorted(seen))

    # ---- contract master ------------------------------------------------

    def _fridays(self) -> tuple[date, ...]:
        return tuple(s for s in self._sessions if is_friday(s))

    def expiries_for(self, sid: str) -> tuple[_ExpiryMeta, ...]:
        cached = self._expiries.get(sid)
        if cached is not None:
            return cached
        if sid not in self._by_sid:
            self._expiries[sid] = ()
            return ()
        elig = self.eligible_sessions(sid)
        if not elig:
            self._expiries[sid] = ()
            return ()
        metas: list[_ExpiryMeta] = []
        for expiry in self._fridays():
            window = (
                self.spec.quarterly_listing_dte
                if is_quarterly_expiry(expiry)
                else self.spec.weekly_listing_dte
            )
            # first eligible session with DTE inside the listing window
            # (elig is session-sorted; bisect lands on the first candidate)
            i = bisect_left(elig, minus_calendar_days(expiry, window))
            if i >= len(elig) or elig[i] >= expiry:
                continue
            metas.append(
                _ExpiryMeta(
                    expiration=expiry,
                    listing_start=elig[i],
                    quarterly=is_quarterly_expiry(expiry),
                )
            )
        metas.sort(key=lambda m: m.expiration)
        self._expiries[sid] = tuple(metas)
        return self._expiries[sid]

    def ladder_for(self, sid: str, expiration: date) -> tuple[Decimal, ...]:
        key = (sid, expiration)
        cached = self._ladder.get(key)
        if cached is not None:
            return cached
        meta = next((m for m in self.expiries_for(sid) if m.expiration == expiration), None)
        if meta is None:
            raise ValueError(f"{sid} has no expiry {expiration}")
        anchor = self._by_sid[sid][meta.listing_start][0]
        result = strike_ladder(anchor, self.spec)
        self._ladder[key] = result
        return result

    def iv_for(self, sid: str, expiration: date) -> float:
        key = (sid, expiration)
        cached = self._iv.get(key)
        if cached is not None:
            return cached
        base_rng = random.Random(f"{self.spec.world_id}/{self.spec.seed}/ivbase/{sid}")
        base = base_rng.uniform(self.spec.iv_base_min, self.spec.iv_base_max)
        mult_rng = random.Random(f"{self.spec.world_id}/{self.spec.seed}/ivmult/{sid}/{expiration}")
        mult = mult_rng.uniform(self.spec.iv_expiry_mult_min, self.spec.iv_expiry_mult_max)
        value = base * mult
        self._iv[key] = value
        return value

    def contracts_for(self, sid: str) -> tuple[OptionContract, ...]:
        if sid not in self._by_sid:
            return ()
        contracts: list[OptionContract] = []
        for meta in self.expiries_for(sid):
            for strike in self.ladder_for(sid, meta.expiration):
                for call_put in ("C", "P"):
                    contracts.append(self._build_contract(sid, meta, strike, call_put))
        contracts.sort(key=lambda c: c.contract_id)
        return tuple(contracts)

    def _build_contract(
        self, sid: str, meta: _ExpiryMeta, strike: Decimal, call_put: str
    ) -> OptionContract:
        self._contract_index[contract_id_of(sid, meta.expiration, call_put, strike)] = (
            sid,
            meta,
            strike,
            call_put,
        )
        return OptionContract(
            contract_id=contract_id_of(sid, meta.expiration, call_put, strike),
            option_root=sid,
            underlying_security_id=sid,
            expiration=meta.expiration,
            strike=strike,
            call_put=call_put,  # type: ignore[arg-type]
            multiplier=MULTIPLIER,
            exercise_style="american",
            listing_start=meta.listing_start,
            listing_end=meta.expiration,
            deliverable=DeliverableSpec(shares_per_contract=Decimal(MULTIPLIER)),
            standard_contract_flag=True,
            corporate_action_id=None,
        )

    def _resolve_sid(self, contract_id: str) -> str:
        """Recover the underlying from a contract id. The `OPT-{sid}-`
        prefix ends in '-', so prefix matching is unambiguous even for
        hyphenated security ids; sorted scan keeps the lookup ordered."""
        for sid in self._sid_sorted:
            if contract_id.startswith(f"OPT-{sid}-"):
                return sid
        raise ValueError(f"unresolvable contract id: {contract_id}")

    def contract(self, contract_id: str) -> OptionContract:
        if contract_id not in self._contract_index:
            self.contracts_for(self._resolve_sid(contract_id))
        sid, meta, strike, call_put = self._contract_index[contract_id]
        return self._build_contract(sid, meta, strike, call_put)

    # ---- chains ---------------------------------------------------------

    def live_expiries_on(self, sid: str, session: date) -> tuple[_ExpiryMeta, ...]:
        """Public view of the session's live-expiry set (the file's chain
        composition — same predicate entry_for enforces)."""
        return self._live_expiries(sid, session)

    def _live_expiries(self, sid: str, session: date) -> tuple[_ExpiryMeta, ...]:
        """Nearest `max_live_expiries` listed, unexpired expiries by DTE.
        Metas are expiration-sorted (== DTE order for a fixed session), so
        the scan starts at the first unexpired one and collects listed
        candidates until the cap is reached."""
        metas = self.expiries_for(sid)
        expirations = [m.expiration for m in metas]
        live: list[_ExpiryMeta] = []
        for m in metas[bisect_left(expirations, session) :]:
            if m.listing_start > session:
                continue
            live.append(m)
            if len(live) >= self.spec.max_live_expiries:
                break
        return tuple(live)

    def has_file(self, sid: str, session: date) -> bool:
        return session in self._file_sessions and sid in self.eligible_on(session)

    def has_any_file(self, session: date) -> bool:
        """Whether ANY underlying has a file for the session (the file grid
        is world-wide: every eligible underlying's session-t file shares the
        same T+1 receipt instant)."""
        return bool(self.eligible_on(session))

    def _publication(self, session: date) -> datetime:
        idx = self._ordinal[session]
        return _wall(self._sessions[idx + 1], PUB_WALL)

    def _early_close(self, session: date) -> bool:
        return not self.calendar.contains_instant(session, _wall(session, SNAPSHOT_1545))

    def entry_for(self, sid: str, session: date, contract_id: str) -> OptionChainEntry:
        """One contract-day entry, computable standalone (the row stream is
        keyed per contract-day, independent of any iteration order).
        Review r1 P1-1: the single-entry path enforces the SAME membership
        predicate as the day file — the expiry must be among the session's
        LIVE expiries (the max-live cap), not merely inside the listing
        window — so lazy access can never quote a contract the file does
        not contain."""
        if not self.has_file(sid, session):
            raise ValueError(f"no file for {sid} on {session}")
        if contract_id not in self._contract_index:
            self.contracts_for(sid)
        cid_sid, meta, strike, call_put = self._contract_index[contract_id]
        if cid_sid != sid:
            raise ValueError(f"{contract_id} does not belong to {sid}")
        if not (meta.listing_start <= session <= meta.expiration):
            raise ValueError(f"{contract_id} not live on {session}")
        if not any(m.expiration == meta.expiration for m in self._live_expiries(sid, session)):
            raise ValueError(
                f"{contract_id} not in the visible chain on {session} "
                f"(expiry {meta.expiration} outside the live-expiry cap)"
            )

        spec = self.spec
        close, _volume = self._by_sid[sid][session]
        spot = float(close)
        dte = (meta.expiration - session).days
        iv = self.iv_for(sid, meta.expiration)
        mid = bs_price(
            spot=spot,
            strike=float(strike),
            dte_calendar_days=dte,
            iv=iv,
            risk_free=spec.risk_free,
            dividend_yield=spec.dividend_yield,
            call_put=call_put,  # type: ignore[arg-type]
        )
        moneyness = abs(math.log(spot / float(strike)))
        half = mid * spec.atm_half_spread_fraction * (1.0 + spec.wing_spread_scale * moneyness)
        ask = _tick_ceil(Decimal(repr(mid)) + Decimal(repr(half)))
        ask = max(ask, CENT)
        bid = _tick_floor(Decimal(repr(mid)) - Decimal(repr(half)))
        if ask <= bid:
            # rounding collapsed the market: keep it wide, never locked
            ask = bid + CENT

        rng = random.Random(f"{spec.world_id}/{spec.seed}/row/{contract_id}/{session}")
        base_oi = (
            spec.oi_base_atm
            * math.exp(-(moneyness**2) / (2.0 * spec.oi_moneyness_width**2))
            * math.exp(-dte / spec.oi_tenor_days)
        )
        bid_size = max(0, round(base_oi * math.exp(rng.gauss(0.0, 0.5))))
        ask_size = max(0, round(base_oi * math.exp(rng.gauss(0.0, 0.5))))
        open_interest = max(0, round(base_oi * math.exp(rng.gauss(0.0, 0.4))))
        if rng.random() < spec.untraded_fraction:
            same_day_volume = 0
        else:
            same_day_volume = max(0, round(open_interest * rng.uniform(0.02, 0.25)))
        condition = "irregular" if rng.random() < spec.non_tradable_fraction else "regular"

        delta = bs_abs_delta(
            spot=spot,
            strike=float(strike),
            dte_calendar_days=dte,
            iv=iv,
            risk_free=spec.risk_free,
            dividend_yield=spec.dividend_yield,
            call_put=call_put,  # type: ignore[arg-type]
        )
        abs_delta = Decimal(str(round(delta, 4)))

        eod_ts = self.calendar.session_close(session)
        snap_eod = OptionQuoteSnapshot(
            exchange_timestamp=eod_ts,
            bid=bid,
            ask=ask,
            bid_size=bid_size,
            ask_size=ask_size,
        )
        snap_1545: OptionQuoteSnapshot | None = None
        if not self._early_close(session):
            snap_1545 = OptionQuoteSnapshot(
                exchange_timestamp=_wall(session, SNAPSHOT_1545),
                bid=bid,
                ask=ask,
                bid_size=bid_size,
                ask_size=ask_size,
            )
        return OptionChainEntry(
            contract_id=contract_id,
            quote_1545=snap_1545,
            quote_eod=snap_eod,
            open_interest=open_interest,
            same_day_volume=same_day_volume,
            abs_delta=abs_delta,
            quote_condition=condition,
        )

    def day_file(self, sid: str, session: date) -> OptionDayFile:
        if not self.has_file(sid, session):
            raise ValueError(f"no file for {sid} on {session}")
        cached = self._file_cache.get((sid, session))
        if cached is not None:
            return cached
        close, _volume = self._by_sid[sid][session]
        half_underlying = float(close) * self.spec.underlying_half_spread_bps / 10_000.0
        underlying_bid = _tick_floor(Decimal(repr(float(close) - half_underlying)))
        underlying_ask = max(_tick_ceil(Decimal(repr(float(close) + half_underlying))), CENT)
        if underlying_ask <= underlying_bid:
            underlying_ask = underlying_bid + CENT
        entries = []
        for meta in self._live_expiries(sid, session):
            for strike in self.ladder_for(sid, meta.expiration):
                for call_put in ("C", "P"):
                    cid = contract_id_of(sid, meta.expiration, call_put, strike)
                    entries.append(self.entry_for(sid, session, cid))
        entries.sort(key=lambda e: e.contract_id)
        median_dv = self._liquidity_series(sid)[session][0]
        file = OptionDayFile(
            underlying_security_id=sid,
            session=session,
            received_at=self._publication(session),
            underlying_bid=underlying_bid,
            underlying_ask=underlying_ask,
            underlying_20d_median_dollar_volume=Decimal(repr(median_dv)).quantize(CENT),
            entries=tuple(entries),
        )
        # bounded FIFO cache — day files are pure functions, so caching is
        # unobservable; the bound keeps long backtests from hoarding rows
        self._file_cache[(sid, session)] = file
        if len(self._file_cache) > 512:
            self._file_cache.pop(next(iter(self._file_cache)))
        return file

    def median_dollar_volume(self, sid: str, session: date) -> Decimal:
        """The file's underlying 20d-median dollar volume, without building
        the chain (the §9.2 underlying-liquidity input)."""
        if sid not in self._by_sid or session not in self._by_sid[sid]:
            raise ValueError(f"no bar for {sid} on {session}")
        return Decimal(repr(self._liquidity_series(sid)[session][0])).quantize(CENT)

    def quote_history(self, contract_id: str) -> tuple[QuoteEvent, ...]:
        if contract_id not in self._contract_index:
            self.contracts_for(self._resolve_sid(contract_id))
        sid, meta, _strike, _cp = self._contract_index[contract_id]
        events: list[QuoteEvent] = []
        for session in self.eligible_sessions(sid):
            if session < meta.listing_start or session > meta.expiration:
                continue
            # same membership predicate as the day file (review r1 P1-1):
            # sessions where the expiry fell out of the live cap have no
            # quotes for this contract
            if not any(m.expiration == meta.expiration for m in self._live_expiries(sid, session)):
                continue
            entry = self.entry_for(sid, session, contract_id)
            received = self._publication(session)
            snapshots: list[OptionQuoteSnapshot] = []
            if entry.quote_1545 is not None:
                snapshots.append(entry.quote_1545)
            snapshots.append(entry.quote_eod)
            for snap in snapshots:
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

    # ---- counts, truth, canonical bytes ---------------------------------

    def contract_count(self) -> int:
        return sum(len(self.contracts_for(sid)) for sid in self.underlyings_ever_eligible())

    def contract_master_sha256(self) -> str:
        """Streaming canonical hash over the ENTIRE contract master in
        enumeration order (review r1 P1-3): binds what the overlay would
        deliver — style, multiplier, deliverable, listing windows — so a
        manifest cannot misdescribe the master. Costs the same single
        enumeration as contract_count."""
        import hashlib

        digest = hashlib.sha256()
        digest.update(b"tree-options-m3-contract-master-v1")
        for sid in self.underlyings_ever_eligible():
            for contract in self.contracts_for(sid):
                digest.update(canonical_bytes(contract))
        return digest.hexdigest()

    def entry_and_quote_counts(self) -> tuple[int, int]:
        entries = 0
        quotes = 0
        for session in self._file_sessions:
            early = self._early_close(session)
            for sid in self.eligible_on(session):
                for meta in self._live_expiries(sid, session):
                    n_rows = 2 * len(self.ladder_for(sid, meta.expiration))
                    entries += n_rows
                    quotes += n_rows * (1 if early else 2)
        return entries, quotes

    def truth(self) -> OptionsOverlayTruth:
        return OptionsOverlayTruth(
            world_id=self.spec.world_id,
            seed=self.spec.seed,
            spec=self.spec,
            n_underlyings_ever_eligible=len(self.underlyings_ever_eligible()),
            n_file_sessions=len(self._file_sessions),
            contract_count=self.contract_count(),
        )

    def canonical_file_bytes(self, sid: str, session: date) -> bytes:
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

    # ---- verifier anchors -------------------------------------------------

    def anchor_slices(self) -> tuple[tuple[str, date], ...]:
        """Deterministic anchor sample: 64 stream-drawn (underlying, session)
        slices plus structural boundaries (first/last file, pre-action,
        one quarterly expiry) for the first three ever-eligible names."""
        ever = self.underlyings_ever_eligible()
        if not ever:
            return ()
        rng = random.Random(f"{self.spec.world_id}/{self.spec.seed}/anchors")
        anchors: list[tuple[str, date]] = []
        seen: set[tuple[str, date]] = set()

        def add(sid: str, session: date | None) -> None:
            if session is None:
                return
            if not self.has_file(sid, session):
                return
            key = (sid, session)
            if key in seen:
                return
            seen.add(key)
            anchors.append(key)

        eligible_by_sid = {sid: self.eligible_sessions(sid) for sid in ever}
        for _ in range(64):
            sid = ever[rng.randrange(len(ever))]
            sessions = eligible_by_sid[sid]
            if not sessions:
                continue
            add(sid, sessions[rng.randrange(len(sessions))])
        for sid in ever[:3]:
            sessions = eligible_by_sid[sid]
            if not sessions:
                continue
            add(sid, sessions[0])
            add(sid, sessions[-1])
            # one session before the underlying's first action effective date
            for act in sorted(
                self._actions, key=lambda a: (a.effective_session, a.source_record_id)
            ):
                if act.security_id != sid:
                    continue
                idx = self._ordinal.get(act.effective_session)
                if idx is not None and idx > 0:
                    add(sid, self._sessions[idx - 1])
                break
            # one quarterly expiry session inside the file range
            for s in sessions:
                if is_quarterly_expiry(s):
                    add(sid, s)
                    break
        return tuple(anchors)


def generate_overlay(
    *,
    spec: OptionsOverlaySpec,
    bars: Sequence[BarRecord],
    master: Sequence[SecurityMasterRecord],
    actions: Sequence[CorporateActionRecord],
    calendar: SessionCalendar,
) -> GeneratedOptionOverlay:
    return GeneratedOptionOverlay(
        spec=spec, bars=bars, master=master, actions=actions, calendar=calendar
    )
