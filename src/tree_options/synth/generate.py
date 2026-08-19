"""Deterministic synthetic world generator (M2 packet §3.B).

`generate_world(spec, calendar)` is a pure function: the same WorldSpec on
the same calendar produces the byte-identical payload on any machine
(stdlib-only randomness from name-keyed seeded streams; no wall clock; no
set-iteration order anywhere in an output path).

Structure: one pass over sessions. Market and sector factors come from
shared streams; every seat (security) draws from its own listing/price/
volume/events streams, so a null world and an alpha world with the same
seed share seats, sectors, tickers, volumes, and event timelines — only
closes differ. At most one event fires per (security, session), only
after two listed sessions and never on a session whose close is already
ratio-derived; terminal events emit a final bar the same session.

Engineering defaults worth knowing (recorded in the evidence doc, not
claimed realistic):
- Terminal bankruptcy sessions lose 40-49%: the M1 quality gate rejects
  undeclared overnight factors >= 2x, so single-session synthetic crashes
  are bounded under the gate (real venues halt; the generator does not).
- Split/reverse/stock-dividend sessions derive the post-event close
  EXACTLY from the declared ratio (the day's market move is surrendered,
  as in the M1 fixture) so the ratio-match gate holds deterministically.
- Renames/splits/dividends announce one session ahead at the publication
  instant; terminal delistings are knowable at the final session's close.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal

from tree_options.data.raw import RawPayload, build_payload
from tree_options.schemas.common import StrictModel
from tree_options.schemas.security import (
    DelistingRecord,
    SectorMappingRecord,
    SecurityMasterRecord,
    TickerMappingRecord,
)
from tree_options.synth.spec import DEFAULT_SECTORS, WorldSpec
from tree_options.synth.truth import LifecycleEvent, WorldTruth
from tree_options.time.calendar import SessionCalendar

PROVIDER = "synthetic/v1"

CENT = Decimal("0.01")
MARKET_VOL = 0.010
SECTOR_VOL = 0.008
IDIO_SCALE = 0.025
T_DF = 5
BANKRUPTCY_LOSS = (0.40, 0.49)  # bounded under the 2x discontinuity gate
SPLIT_RATIOS = ((2, 1), (3, 2), (3, 1))
REVERSE_RATIOS = ((1, 2), (1, 3), (1, 5))
STOCK_DIVIDEND_RATIO = (105, 100)
CASH_DIVIDEND_FRACTION = Decimal("0.005")
SPAN_FRACTION = Decimal("0.004")
VOLUME_FLOOR = 100
PRICE_FLOOR = Decimal("0.01")
# a ratio-derived close at/above this keeps quantization error <= 0.5%,
# safely inside the 2% ratio-match tolerance (round-1 P1-2)
RATIO_FLOOR = Decimal("1.00")
# round-2 P1-1: the minimum close. Cent rounding at prices near the old
# $0.01 floor could land EXACTLY on the 0.5x/2x discontinuity bounds; at
# $1.00 the worst quantization error is 0.5%, so no clamped move (<= 1.9x)
# or bounded bankruptcy loss (<= 49%) can reach either bound — proven as a
# property in test_quantized_moves_stay_inside_gate_property.
MIN_CLOSE = Decimal("1.00")
# round-1 remediation: fat-tail idio plus market/sector factors can jointly
# produce an undeclared >=2x overnight move, which the M1 discontinuity gate
# rejects — per-session returns are bounded under the gate bound (real
# venues halt; the generator does too)
DAILY_RET_LIMIT = math.log(1.9)
# round-4 P1-1: cumulative alpha drift (close/base) is walled at +-1.5%.
# Every undeclared session factor is then bounded by
# 1.9 * exp(2*DRIFT_CAP) = 1.958 before cent rounding (worst 1.977 at the
# $1.00 floor), strictly inside the 2x gate bound — and the ratio-
# announcement resync jump is bounded by exp(DRIFT_CAP) = 1.015.
DRIFT_CAP = 0.015


class GeneratedWorld(StrictModel):
    spec: WorldSpec
    payload: RawPayload
    master: tuple[SecurityMasterRecord, ...]
    truth: WorldTruth


@dataclass
class _Mapping:
    ticker: str
    effective_from: date
    effective_to: date | None
    available_at: datetime


@dataclass
class _PendingRatio:
    """A ratio event announced at session t, decided and applied at t+1
    against the application session's actual prices (round-3 P1-1)."""

    kind: str
    factor: Decimal
    n: int
    d: int
    announced_at: datetime


class _Seat:
    """Mutable simulation state for one security (never part of a payload)."""

    __slots__ = (
        "base_close",
        "base_volume",
        "close",
        "end_idx",
        "end_kind",
        "exchange",
        "factor_override",
        "index",
        "listed_from_idx",
        "pending_from",
        "pending_ratio",
        "pending_ticker",
        "prev_ret",
        "rng_events",
        "rng_listing",
        "rng_price",
        "rng_volume",
        "sector_idx",
        "security_id",
        "ticker",
        "timeline",
    )

    def __init__(self, index: int, spec: WorldSpec) -> None:
        self.index = index
        self.security_id = f"SYN-{index:04d}"
        base = f"{spec.world_id}/{spec.seed}/seat/{self.security_id}"
        self.rng_listing = random.Random(f"{base}/listing")
        self.rng_price = random.Random(f"{base}/price")
        self.rng_volume = random.Random(f"{base}/volume")
        self.rng_events = random.Random(f"{base}/events")
        self.sector_idx = 0
        self.exchange = "NYSE"
        self.base_volume = 0.0
        self.close = Decimal("0.00")
        self.prev_ret: float | None = None
        self.ticker = ""
        self.listed_from_idx: int | None = None
        self.end_idx: int | None = None
        self.end_kind: str | None = None
        self.pending_ticker: str | None = None
        self.pending_from: date | None = None
        self.factor_override: Decimal | None = None
        self.pending_ratio: _PendingRatio | None = None
        # alpha-independent price trajectory: EXACTLY what the same-seat
        # null world's close would be at every session (round-2 P1-2) —
        # suppression decisions read THIS, never the alpha-moved close
        self.base_close = Decimal("0.00")
        self.timeline: list[_Mapping] = []


def _pub(session: date, hour: int) -> datetime:
    return datetime(session.year, session.month, session.day, hour, 0, tzinfo=UTC)


def _cents(x: Decimal) -> Decimal:
    return max(x.quantize(CENT, rounding=ROUND_HALF_UP), MIN_CLOSE)


def _t_draw(rng: random.Random) -> float:
    g = rng.gauss(0.0, 1.0)
    chi2 = sum(rng.gauss(0.0, 1.0) ** 2 for _ in range(T_DF))
    return g / math.sqrt(chi2 / T_DF)


def _clamp_session_return(ret: float) -> float:
    """Bound an undeclared overnight move strictly inside the 2x gate."""
    return max(-DAILY_RET_LIMIT, min(DAILY_RET_LIMIT, ret))


def _ohlc(close: Decimal, rng: random.Random) -> tuple[Decimal, Decimal, Decimal]:
    """(open, high, low) around close: coherent by construction."""
    span = max((close * SPAN_FRACTION).quantize(CENT, rounding=ROUND_HALF_UP), CENT)
    low = max(close - span, PRICE_FLOOR)
    high = max(close + span, low + CENT)
    open_ = min(max(close + span * rng.choice((-1, 0, 1)), low), high)
    return open_, high, low


def generate_world(spec: WorldSpec, calendar: SessionCalendar) -> GeneratedWorld:
    sessions_all = calendar.sessions()
    sessions = sessions_all if spec.n_sessions is None else sessions_all[: spec.n_sessions]
    sectors = spec.sectors if spec.sectors is not None else DEFAULT_SECTORS
    hour = spec.publication_hour_utc
    rates = spec.rates

    market = random.Random(f"{spec.world_id}/{spec.seed}/market")
    sector_rngs = [random.Random(f"{spec.world_id}/{spec.seed}/sector/{s}") for s in sectors]
    ipo_rng = random.Random(f"{spec.world_id}/{spec.seed}/ipo")
    ticker_rng = random.Random(f"{spec.world_id}/{spec.seed}/tickers")

    seats = [_Seat(i, spec) for i in range(spec.n_securities)]
    n_initial = min(
        spec.n_securities, max(10, round(spec.n_securities * spec.initial_listing_fraction))
    )
    pending_seats = seats[n_initial:]

    used_tickers: set[str] = set()
    freed_tickers: list[str] = []
    recycled: list[str] = []
    bar_rows: list[dict[str, object]] = []
    action_rows: list[dict[str, object]] = []
    events: list[LifecycleEvent] = []
    sector_of: dict[str, str] = {}
    raw_n = 0
    act_n = 0
    cross_mean: float | None = None

    def allocate_ticker() -> str:
        if freed_tickers:
            ticker = freed_tickers.pop(0)
            recycled.append(ticker)
            return ticker
        while True:
            ticker = "".join(ticker_rng.choices("ABCDEFGHIJKLMNOPQRSTUVWXYZ", k=4))
            if ticker not in used_tickers:
                return ticker

    def list_seat(seat: _Seat, idx: int) -> None:
        seat.listed_from_idx = idx
        seat.sector_idx = seat.rng_listing.randrange(len(sectors))
        seat.exchange = seat.rng_listing.choice(("NYSE", "NASDAQ"))
        seat.base_volume = math.exp(seat.rng_listing.uniform(math.log(1e4), math.log(1e7)))
        initial = math.exp(seat.rng_listing.uniform(math.log(5.0), math.log(500.0)))
        seat.close = _cents(Decimal(repr(initial)))
        seat.base_close = seat.close
        seat.ticker = allocate_ticker()
        used_tickers.add(seat.ticker)
        seat.timeline = [_Mapping(seat.ticker, sessions[idx], None, _pub(sessions[idx], hour))]
        events.append(
            LifecycleEvent(session=sessions[idx], security_id=seat.security_id, kind="ipo")
        )
        sector_of[seat.security_id] = sectors[seat.sector_idx]

    # terminal events first: they end the seat and constrain everything else
    event_walk: tuple[tuple[str, float], ...] = (
        ("bankruptcy_11", rates.bankruptcy / 252.0),
        ("merger", rates.merger / 252.0),
        ("voluntary_delisting", rates.voluntary_delisting / 252.0),
        ("coverage_lapse", rates.coverage_lapse / 252.0),
        ("rename", rates.rename / 252.0),
        ("split", rates.split / 252.0),
        ("reverse_split", rates.reverse_split / 252.0),
        ("stock_dividend", rates.stock_dividend / 252.0),
        ("cash_dividend", rates.cash_dividend / 252.0),
    )

    for t_idx, session in enumerate(sessions):
        if t_idx == 0:
            # the initial cohort lists on the first session; later seats
            # arrive via the IPO path below
            for seat in seats[:n_initial]:
                list_seat(seat, 0)

        # apply announced ticker switches
        for seat in seats:
            if seat.pending_from is not None and session >= seat.pending_from:
                seat.ticker = seat.pending_ticker or seat.ticker
                seat.pending_ticker = None
                seat.pending_from = None

        # at most one IPO per session
        if pending_seats and ipo_rng.random() < rates.ipo_per_year / 252.0:
            list_seat(pending_seats.pop(0), t_idx)

        market_ret = market.gauss(0.0, MARKET_VOL)
        sector_ret = [rng.gauss(0.0, SECTOR_VOL) for rng in sector_rngs]

        live_now = [s for s in seats if s.listed_from_idx is not None and s.end_idx is None]
        rets_today: dict[str, float] = {}

        for seat in live_now:
            assert seat.listed_from_idx is not None
            # round-3 P1-1: a ratio event announced yesterday is decided
            # HERE, against THIS session's actual base price — the price the
            # override will actually multiply. Deciding at announcement time
            # let an intervening return push the applied product under the
            # floor. The decision reads base_close only, so null/alpha
            # same-seed worlds still decide identically.
            override_today = seat.factor_override
            seat.factor_override = None
            announced = seat.pending_ratio
            seat.pending_ratio = None
            if announced is not None:
                if seat.base_close * announced.factor < RATIO_FLOOR:
                    override_today = None  # canceled: normal session
                else:
                    override_today = announced.factor
                    act_n += 1
                    action_rows.append(
                        dict(
                            vendor_symbol=seat.ticker,
                            kind=announced.kind,
                            effective_session=session,
                            ratio_numerator=announced.n,
                            ratio_denominator=announced.d,
                            available_at=announced.announced_at,
                            source_record_id=f"ACT-{act_n:07d}",
                        )
                    )
                    events.append(
                        LifecycleEvent(
                            session=session, security_id=seat.security_id, kind=announced.kind
                        )
                    )

            eligible = (
                t_idx - seat.listed_from_idx >= 2
                and t_idx + 1 < len(sessions)
                and override_today is None
                # round-4 P2-1: a consumed pending (applied OR canceled)
                # consumes the session in both twins — the walk never runs,
                # so cancellation is genuinely draw-neutral
                and announced is None
            )
            fired: str | None = None
            if eligible:
                u = seat.rng_events.random()
                acc = 0.0
                for kind, p in event_walk:
                    acc += p
                    if u < acc:
                        fired = kind
                        break
                if fired == "merger" and not any(
                    s is not seat and s.end_idx is None for s in live_now
                ):
                    fired = None

            if fired == "bankruptcy_11":
                loss = seat.rng_events.uniform(*BANKRUPTCY_LOSS)
                ret = math.log(1.0 - loss)
                base_ret = ret
                seat.end_idx = t_idx
                seat.end_kind = "bankruptcy_11"
                events.append(
                    LifecycleEvent(session=session, security_id=seat.security_id, kind=fired)
                )
            elif fired in ("merger", "voluntary_delisting", "coverage_lapse"):
                ret = _clamp_session_return(
                    market_ret + sector_ret[seat.sector_idx] + IDIO_SCALE * _t_draw(seat.rng_price)
                )
                base_ret = ret
                seat.end_idx = t_idx
                seat.end_kind = fired
                events.append(
                    LifecycleEvent(session=session, security_id=seat.security_id, kind=fired)
                )
                if fired == "merger":
                    successor = next(s for s in live_now if s is not seat and s.end_idx is None)
                    act_n += 1
                    action_rows.append(
                        dict(
                            vendor_symbol=seat.ticker,
                            kind="merger",
                            effective_session=session,
                            successor_security_id=successor.security_id,
                            available_at=_pub(sessions[t_idx - 1], hour),
                            source_record_id=f"ACT-{act_n:07d}",
                        )
                    )
            else:
                if fired == "rename":
                    seat.pending_ticker = allocate_ticker()
                    used_tickers.add(seat.pending_ticker)
                    seat.pending_from = sessions[t_idx + 1]
                    seat.timeline[-1].effective_to = session
                    seat.timeline.append(
                        _Mapping(
                            seat.pending_ticker, sessions[t_idx + 1], None, _pub(session, hour)
                        )
                    )
                elif fired in ("split", "reverse_split", "stock_dividend"):
                    if fired == "split":
                        n, d = seat.rng_events.choice(SPLIT_RATIOS)
                    elif fired == "reverse_split":
                        n, d = seat.rng_events.choice(REVERSE_RATIOS)
                    else:
                        n, d = STOCK_DIVIDEND_RATIO
                    # round-3 P1-1: DEFER the emission and the decision to
                    # the application session (decided above); resync the
                    # alpha drift so both trajectories apply the factor to
                    # the same price. No action/truth event yet.
                    seat.pending_ratio = _PendingRatio(
                        kind=fired,
                        factor=Decimal(d) / Decimal(n),
                        n=n,
                        d=d,
                        announced_at=_pub(session, hour),
                    )
                    fired = None
                elif fired == "cash_dividend":
                    cash = max(
                        (seat.close * CASH_DIVIDEND_FRACTION).quantize(
                            CENT, rounding=ROUND_HALF_UP
                        ),
                        CENT,
                    )
                    act_n += 1
                    action_rows.append(
                        dict(
                            vendor_symbol=seat.ticker,
                            kind="cash_dividend",
                            effective_session=sessions[t_idx + 1],
                            cash_amount=str(cash),
                            available_at=_pub(session, hour),
                            source_record_id=f"ACT-{act_n:07d}",
                        )
                    )
                if fired is not None:
                    events.append(
                        LifecycleEvent(session=session, security_id=seat.security_id, kind=fired)
                    )

                if override_today is not None:
                    ret = math.log(float(override_today))
                    base_ret = ret
                else:
                    raw_ret = (
                        market_ret
                        + sector_ret[seat.sector_idx]
                        + IDIO_SCALE * _t_draw(seat.rng_price)
                    )
                    base_ret = _clamp_session_return(raw_ret)
                    ret = raw_ret
                    if (
                        spec.kind == "alpha"
                        and spec.alpha is not None
                        and seat.prev_ret is not None
                        and cross_mean is not None
                    ):
                        ret += spec.alpha.coefficient * (seat.prev_ret - cross_mean)
                    ret = _clamp_session_return(ret)

            if override_today is None:
                new_close = _cents(Decimal(repr(float(seat.close) * math.exp(ret))))
                # the null-world twin of this close, chained identically
                seat.base_close = _cents(Decimal(repr(float(seat.base_close) * math.exp(base_ret))))
                # round-4 P1-1: wall the cumulative drift so the
                # announcement-session resync can never jump near the gate
                drift_up = _cents(Decimal(repr(float(seat.base_close) * math.exp(DRIFT_CAP))))
                drift_down = _cents(Decimal(repr(float(seat.base_close) * math.exp(-DRIFT_CAP))))
                if new_close > drift_up:
                    new_close = drift_up
                elif new_close < drift_down:
                    new_close = drift_down
            else:
                # exact ratio-derived close: the declared action must match
                # the observed factor to the gate's tolerance, so no float
                # round-trip on split/reverse/stock-dividend sessions
                new_close = _cents(seat.close * override_today)
                seat.base_close = _cents(seat.base_close * override_today)
            open_, high, low = _ohlc(new_close, seat.rng_price)
            raw_n += 1
            bar_rows.append(
                dict(
                    vendor_symbol=seat.ticker,
                    session=session,
                    open=str(open_),
                    high=str(high),
                    low=str(low),
                    close=str(new_close),
                    volume=int(
                        max(
                            VOLUME_FLOOR,
                            seat.base_volume * math.exp(seat.rng_volume.gauss(0.0, 0.3)),
                        )
                    ),
                    available_at=_pub(session, hour),
                    source_record_id=f"RAW-{raw_n:07d}",
                )
            )
            seat.close = new_close
            seat.prev_ret = ret
            rets_today[seat.security_id] = ret

        # freed tickers become available for reuse by later IPOs
        for seat in live_now:
            if seat.end_idx == t_idx:
                freed_tickers.append(seat.ticker)

        if rets_today:
            cross_mean = sum(rets_today.values()) / len(rets_today)

    # ---- master assembly -------------------------------------------------
    master_records: list[SecurityMasterRecord] = []
    for seat in seats:
        if seat.listed_from_idx is None:
            continue
        assert seat.listed_from_idx is not None
        listing_start = sessions[seat.listed_from_idx]
        ended = seat.end_idx is not None
        end_session = sessions[seat.end_idx] if ended and seat.end_idx is not None else None
        timeline = list(seat.timeline)
        if end_session is not None:
            timeline = [m for m in timeline if m.effective_from <= end_session]
            timeline[-1].effective_to = end_session
        mappings = tuple(
            TickerMappingRecord(
                security_id=seat.security_id,
                ticker=m.ticker,
                effective_from=m.effective_from,
                # ended seats were closed at assembly above; survivors keep None
                effective_to=m.effective_to,
                available_at=m.available_at,
            )
            for m in timeline
        )
        delisting: DelistingRecord | None = None
        if ended and seat.end_kind in ("bankruptcy_11", "merger", "voluntary_delisting"):
            assert seat.end_idx is not None and seat.end_kind is not None
            delisting = DelistingRecord(
                delisting_session=sessions[seat.end_idx],
                reason=seat.end_kind,
                final_price_available=seat.end_kind != "bankruptcy_11",
                available_at=_pub(sessions[seat.end_idx], hour),
            )
        master_records.append(
            SecurityMasterRecord(
                security_id=seat.security_id,
                listing_start=listing_start,
                listing_end=end_session,
                exchange=seat.exchange,
                source="synthetic-generator-v1",
                available_at=_pub(listing_start, hour),
                ticker_mappings=mappings,
                sector_mappings=(
                    SectorMappingRecord(
                        security_id=seat.security_id,
                        sector=sectors[seat.sector_idx],
                        effective_from=listing_start,
                        available_at=_pub(listing_start, hour),
                    ),
                ),
                delisting=delisting,
            )
        )

    payload = build_payload(
        provider=PROVIDER,
        rows=(*bar_rows, *action_rows),
        retrieved_at=_pub(sessions[-1], hour),
    )
    truth = WorldTruth(
        world_id=spec.world_id,
        kind=spec.kind,
        seed=spec.seed,
        sectors=sectors,
        rates=rates,
        alpha=spec.alpha,
        events=tuple(events),
        recycled_tickers=tuple(recycled),
        sector_of=sector_of,
    )
    return GeneratedWorld(spec=spec, payload=payload, master=tuple(master_records), truth=truth)
