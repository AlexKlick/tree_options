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
`decision_close` (R3-P1-2: the decision-instant authority — the base
surface answers its own calendar's close, this one the decision grid's),
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
the DECLARED `spot_proxy_v2` source's 20-session median of close*volume
(`load_spot_proxy_v2` — the ruled P0-1(b) preferred leg; the capture script
and the file land post-closeout) whenever that source can answer a true
contiguous 20-session median — contiguity validated against the EXCHANGE
calendar (P1-2: an explicit constructor dependency, R2-P1-a provenance-
bound to the committed fixture; the overlay's union-of-captures calendar
self-certifies exactly when a market session is missing from every
capture) — and otherwise the overlay's declared `Decimal("0")` sentinel —
this tier never calls the equity-aggregates endpoint today, so the
protocol's 50M minimum FAILs the rule until the recapture lands (an
honest audit row, pinned by test; the ruled 0.2.2 fallback drops the term
with disclosure instead — R2-P2-d: under the DECLARED
`dropped_no_equity_aggregates` term the snapshot supplies ABSENCE, which
is what makes that NOT_APPLICABLE branch reachable through this adapter
at all). `spans_earnings` is None —
the honest "no evidence" encoding (w2,
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

import statistics
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import cast

from tree_options.candidates.filters import AsOf, CandidateSnapshot
from tree_options.data.massive_client import loads_exact
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
from tree_options.time.calendar import (
    CalendarIntegrityError,
    NotASessionError,
    SessionCalendar,
    StaticSessionCalendar,
    calendar_content_sha256,
)

SPOT_SENTINEL_SESSION = date.min  # the flat form's every-session key
DOLLAR_VOLUME_WINDOW_SESSIONS = 20  # the protocol's 20d median term

# (R2-P1-a, Codex round 2) THE EXCHANGE AUTHORITY IS PROVENANCE-BOUND. The
# repo-adopted NYSE fixture `research_protocol.yaml` itself declares (0.2.1
# ruling, data/g4/calendar-decision.json "repo-generated-calendar"): the
# committed, checksummed files under data/calendar/. The PIN below is that
# fixture's COMPLETE content identity (full session tuple + early-close
# map + the concrete class, `calendar_content_sha256`) — an identity the
# `.sha256` sidecar alone cannot give (it pins file bytes, not semantics,
# and a regenerated fixture with the same bytes-checksum discipline could
# carry different sessions).
# A fixture edit that keeps its sidecar in sync STILL refuses here until
# the pin is re-derived deliberately: the exchange calendar is trial
# identity, and identity changes are conscious acts.
# (R3-P1-1, Codex round 3) the pin below is the re-derived digest AFTER the
# concrete class entered the payload — the pre-R3 value 7aa83aa7… hashed
# the data alone and therefore also certified any SUBCLASS reporting it.
REPO_EXCHANGE_CALENDAR_JSON = Path("data/calendar/nyse_sessions_2018_01_02_2026_12_31.json")
REPO_EXCHANGE_CALENDAR_CHECKSUM = Path("data/calendar/nyse_sessions_2018_01_02_2026_12_31.sha256")
REPO_EXCHANGE_CALENDAR_CONTENT_SHA256 = (
    "38714f6ea879466a789251bf646957a9aa6a3c63ad6b4abde6b5e01c4f83a766"
)


def repo_exchange_calendar(repo_root: Path | str | None = None) -> StaticSessionCalendar:
    """The BOUND exchange-calendar authority (R2-P1-a): the committed NYSE
    fixture, checksum-verified by `StaticSessionCalendar` AND bound to its
    pinned content identity — the only calendar `VwapPitSurface` accepts as
    `exchange_calendar`.

    `repo_root` defaults to the repository this module lives in (the src
    layout resolves `tree_options` into `<repo>/src`, so the data/ fixture
    sits three levels up); an explicit root serves embedders and tests. The
    factory is the one sanctioned way to obtain the authority: constructing
    a `StaticSessionCalendar` by hand still works, but the surface's
    constructor gate refuses it unless its CONTENT identity equals the pin.
    """
    root = Path(repo_root) if repo_root is not None else Path(__file__).resolve().parents[3]
    calendar = StaticSessionCalendar(
        root / REPO_EXCHANGE_CALENDAR_JSON,
        root / REPO_EXCHANGE_CALENDAR_CHECKSUM,
    )
    bound_identity = calendar_content_sha256(calendar)
    if bound_identity != REPO_EXCHANGE_CALENDAR_CONTENT_SHA256:
        raise MassiveOverlayError(
            "the committed NYSE fixture's content identity changed:"
            f" calendar_content_sha256 {bound_identity} != the pinned"
            f" exchange-authority identity {REPO_EXCHANGE_CALENDAR_CONTENT_SHA256}"
            " — the exchange calendar is trial identity; re-pin it"
            " deliberately, never in passing"
        )
    return calendar


def load_spot_proxy_v2(path: Path) -> dict[str, dict[date, tuple[Decimal, int]]]:
    """Parse one `spot_proxy_v2.json` — the OPTIONAL dollar-volume source
    (theory-panel §2 P0-1, option (b): the ~29-call equity-aggregates
    recapture that funds the $50M liquidity term AND the real `dol_vol_20`
    feature in one stroke).

    THE EXACT SHAPE (a declared input, never a vendor quote):

        {"<underlying>": {"<ISO date>": {"close": "<decimal token>",
                                         "volume": <int>}}}

    - `close` is a STRING carrying the exact decimal token (the house
      discipline: a JSON number is refused — `loads_exact` never lets a
      float exist, and this loader additionally requires the token form) —
      and must be positive;
    - `volume` is a STRICT int (bools, floats/Decimals, and strings all
      refuse) and must be >= 0 — a zero-volume session is a real
      observation, not a gap;
    - every session object carries EXACTLY the two keys.

    THE FILE AND THE CAPTURE SCRIPT THAT WRITES IT LAND POST-CLOSEOUT
    (owner ruling 2026-08-26: the running bars-era capture is untouched
    until it closes). Until a v2 file is declared, the adapter's declared
    `Decimal("0")` sentinel stands and the $50M term honestly FAILs.
    """
    path = Path(path)
    payload = loads_exact(path.read_bytes())
    if not isinstance(payload, dict):
        raise MassiveOverlayError(f"{path.name}: top-level JSON is not an object")
    parsed: dict[str, dict[date, tuple[Decimal, int]]] = {}
    for underlying, sessions in payload.items():
        if not isinstance(underlying, str) or not underlying:
            raise MassiveOverlayError(f"{path.name}: key {underlying!r} is not a symbol")
        where = f"{path.name}[{underlying!r}]"
        if not isinstance(sessions, dict):
            raise MassiveOverlayError(f"{where}: session map is not an object")
        rows: dict[date, tuple[Decimal, int]] = {}
        for raw_session, cell in sessions.items():
            try:
                session = date.fromisoformat(str(raw_session).strip())
            except ValueError as exc:
                raise MassiveOverlayError(
                    f"{where}: key {raw_session!r} is not an ISO date"
                ) from exc
            if not isinstance(cell, dict) or set(cell) != {"close", "volume"}:
                raise MassiveOverlayError(
                    f"{where}[{raw_session!r}]: each session must carry exactly the"
                    f" keys close+volume, got {sorted(cell) if isinstance(cell, dict) else cell!r}"
                )
            close, volume = cell["close"], cell["volume"]
            if not isinstance(close, str):
                raise MassiveOverlayError(
                    f"{where}[{raw_session!r}]: close must be an exact decimal STRING"
                    f" token, got {type(close).__name__}"
                )
            try:
                close_value = Decimal(close.strip())
            except ArithmeticError:
                raise MassiveOverlayError(
                    f"{where}[{raw_session!r}]: close {close!r} is not a decimal"
                ) from None
            # (P2-5, Codex round 1) non-finite tokens are a CLEAN refusal:
            # Decimal("Infinity") is POSITIVE-looking (it would load and an
            # infinity median flips the liquidity rule to PASS — fail-open),
            # and Decimal("NaN") raises InvalidOperation on comparison. The
            # finiteness gate comes FIRST (short-circuit), and the whole
            # validation sits inside the ArithmeticError net so neither
            # token can escape as anything but the loader's own error shape.
            try:
                if not close_value.is_finite() or close_value <= 0:
                    raise MassiveOverlayError(
                        f"{where}[{raw_session!r}]: close {close_value} is not"
                        " a positive finite decimal"
                    )
            except ArithmeticError:
                raise MassiveOverlayError(
                    f"{where}[{raw_session!r}]: close {close!r} is not a positive finite decimal"
                ) from None
            if type(volume) is not int or volume < 0:
                raise MassiveOverlayError(
                    f"{where}[{raw_session!r}]: volume must be a strict int >= 0, got {volume!r}"
                )
            rows[session] = (close_value, volume)
        parsed[underlying] = rows
    return parsed


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
    `NoOptionFileError`, never a synthesized price.

    `exchange_calendar` (P1-2) is the EXPLICIT exchange-calendar dependency
    the optional v2 dollar-volume source validates its 20-session window
    against — the repo-adopted NYSE fixture the protocol declares, threaded
    by the caller, never a global; without it the v2 source fails closed.
    (R2-P1-a, Codex round 2) When supplied it must BE that fixture in
    content: the constructor refuses any calendar whose
    `calendar_content_sha256` differs from the pinned
    `REPO_EXCHANGE_CALENDAR_CONTENT_SHA256` — obtain it from
    `repo_exchange_calendar()`.

    `decision_calendar` (R2-P1-c, Codex round 2) is the DECISION-grid
    calendar `candidate_snapshot` derives `decision_at` from — the grid the
    runner splits on, early-close aware, so `CandidateFilter.evaluate`'s
    exact `decision_at == session_close(decision_session)` check finds the
    snapshot coherent at the TRUE close (13:00 ET on an early-close
    Friday, never the overlay's nominal 16:00). Default None keeps today's
    behavior byte-identically: the OVERLAY (execution) calendar answers,
    which is correct for lane-1/synthetic worlds where the two calendars
    are one. The visible-session lookup, publication wall, marks and fills
    stay on the overlay calendar — only the decision instant changes
    hands.

    `underlying_liquidity_term` (R2-P2-d, Codex round 2) is the DECLARED
    disposition of the underlying-liquidity term — the value the protocol
    carries at `option_candidate_defaults.liquidity_volume_flow.
    underlying_liquidity_term`, threaded by whoever builds the surface from
    the same protocol the runner builds its filter from. "evaluated" (the
    default) keeps today's chain byte-identically: the v2 median when it
    can answer, else the overlay's declared sentinel. The ruled 0.2.2
    `dropped_no_equity_aggregates` supplies ABSENCE instead — the regime's
    premise is no equity-aggregates source, and the sentinel must never be
    minted into it (the filter judges any supplied value regime-incoherent,
    so the ruled NOT_APPLICABLE disclosure would otherwise be unreachable
    through this adapter)."""

    def __init__(
        self,
        overlay: MassiveDerivedOverlay,
        *,
        spot: Mapping[str, Mapping[date, Decimal]] | None = None,
        spot_v2: Mapping[str, Mapping[date, tuple[Decimal, int]]] | None = None,
        exchange_calendar: SessionCalendar | None = None,
        decision_calendar: SessionCalendar | None = None,
        underlying_liquidity_term: str = "evaluated",
    ) -> None:
        self._overlay = overlay
        self._spot: dict[str, dict[date, Decimal]] = {
            underlying: dict(sessions) for underlying, sessions in (spot or {}).items()
        }
        # (w3) the OPTIONAL dollar-volume source (see `load_spot_proxy_v2`):
        # None keeps the declared Decimal("0") sentinel — the ruled fallback.
        self._spot_v2: dict[str, dict[date, tuple[Decimal, int]]] = {
            underlying: dict(sessions) for underlying, sessions in (spot_v2 or {}).items()
        }
        # (P1-2, Codex round 1) the EXCHANGE calendar is an explicit
        # constructor dependency — never a global, never auto-loaded. The
        # repo-adopted one is the committed, checksummed NYSE fixture
        # `research_protocol.yaml` itself declares (0.2.1 ruling,
        # data/g4/calendar-decision.json "repo-generated-calendar"). Without
        # it, exchange-session contiguity cannot be proven and the v2
        # dollar-volume source fails closed (the declared sentinel).
        self._exchange_calendar = exchange_calendar
        # (R2-P1-a, Codex round 2) and when supplied it is PROVENANCE-BOUND:
        # only a calendar whose COMPLETE content identity equals the
        # committed fixture's is the exchange authority. Accepting any
        # `SessionCalendar` here re-opens the exact self-certification
        # vector P1-2 closed — the branch's own tests passed
        # `exchange_calendar=overlay.calendar`, the union of CAPTURED dates,
        # and a market session missing from every capture would vanish from
        # the very calendar meant to catch it. The refusal is loud and at
        # construction, never a silent fall-back to unbounded behavior.
        # (R3-P1-1, Codex round 3) the digest now names the concrete class,
        # so this SAME gate also refuses a `StaticSessionCalendar` subclass
        # reporting the canonical data — its methods are free to disagree
        # with its data, and data identity alone cannot certify behavior.
        # No separate `type() is` check is added: the digest is the one
        # identity authority (it is what `_calendar_descriptor` and the
        # config hash ride too), and a second, structural gate could only
        # drift from it.
        if exchange_calendar is not None:
            try:
                supplied_identity = calendar_content_sha256(exchange_calendar)
            except CalendarIntegrityError as exc:
                raise MassiveOverlayError(
                    "exchange_calendar must be the repo-adopted NYSE fixture"
                    f" (repo_exchange_calendar()): {exc}"
                ) from exc
            if supplied_identity != REPO_EXCHANGE_CALENDAR_CONTENT_SHA256:
                raise MassiveOverlayError(
                    "exchange_calendar must be the repo-adopted NYSE fixture"
                    " (repo_exchange_calendar()): its content identity"
                    f" {supplied_identity} != the pinned authority"
                    f" {REPO_EXCHANGE_CALENDAR_CONTENT_SHA256} — an unbound"
                    " calendar self-certifies exactly where the design says"
                    " fail-closed (the Jan-16 scenario)"
                )
        # (R2-P1-c, Codex round 2) the DECISION-grid calendar: None keeps
        # today's overlay-calendar behavior byte-identically (lane-1/
        # synthetic); when supplied, `candidate_snapshot` stamps decision_at
        # from THIS calendar's early-close-aware session_close — the same
        # grid the runner splits and filters on.
        self._decision_calendar = decision_calendar
        # (R2-P2-d, Codex round 2) the DECLARED liquidity term — mirrored
        # from the filter's own constructor gate: a two-token Literal, and
        # an unknown token refuses here exactly as it refuses there, never
        # silently behaves as "evaluated".
        if underlying_liquidity_term not in {"evaluated", "dropped_no_equity_aggregates"}:
            raise ValueError(f"unknown underlying_liquidity_term {underlying_liquidity_term!r}")
        self._underlying_liquidity_term = underlying_liquidity_term

    # ---- identity / delegation ------------------------------------------------

    @property
    def overlay(self) -> MassiveDerivedOverlay:
        return self._overlay

    @property
    def snapshot_id(self) -> str:
        return self._overlay.spec.world_id

    def decision_close(self, decision_session: date) -> datetime:
        """(R3-P1-2, Codex round 3) THE decision-instant authority, overriding
        the base surface's: the DECISION-grid calendar's early-close-aware
        close when the constructor carries one, else the overlay (execution)
        calendar's — today's behavior, byte-identically, which is correct for
        lane-1/synthetic worlds where the two calendars are one.

        One seam, not two: this is the wave-2 `candidate_snapshot` seam
        generalized to every decision-side read (`build_candidates`'
        pending-action visibility, the expiry/strike ladder probes, the
        sizing entry read). The visible-session lookup, publication wall,
        marks and fills stay on the overlay calendar — only the decision
        instant changes hands."""
        if self._decision_calendar is not None:
            return self._decision_calendar.session_close(decision_session)
        return self._overlay.calendar.session_close(decision_session)

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
        of the decision session (file(t-1) for a close(t) decision).

        Deliberately NOT the `decision_close` seam: this is a
        publication-wall sweep, and the wall sits at T+1 09:00 — never
        inside a session — so the visible session is invariant across the
        13:00/16:00 disagreement and file visibility is the OVERLAY's
        question, not the decision grid's."""
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

    def _dollar_volume_as_of(
        self, underlying_id: str, visible_session: date, received: datetime
    ) -> AsOf | None:
        """The ruled P0-1(b) dollar-volume stamp: the 20-session median of
        close*volume over the CONTIGUOUS EXCHANGE-calendar window ending at
        the visible session, vendor-observed, available at the window's last
        session's T+1 wall (`received`).

        (P1-2, Codex round 1) The window is enumerated on the REPO-ADOPTED
        exchange calendar — an explicit constructor dependency — NEVER on
        the overlay's own calendar, which is the union of CAPTURED dates: a
        market session missing from every capture vanishes from that union,
        so a window sliced on it self-certifies as contiguity and the median
        can pass where the design says fail-closed (the Jan-16 scenario).
        The 20 sessions of the window are therefore exactly the 20
        consecutive EXCHANGE sessions ending at the visible session.

        Fail-closed on availability (never a median over whatever happened
        to be captured): None when no v2 source is declared for the
        underlying, when no exchange calendar is threaded, when the visible
        session is not an exchange session, or when the 20 consecutive
        exchange sessions are not ALL present in the declared map — the
        caller then answers the overlay's declared sentinel and the rule
        fails honestly."""
        rows = self._spot_v2.get(underlying_id)
        if not rows:
            return None
        # (P1-2) the exchange calendar is the window's authority; falling
        # back to the overlay calendar here is exactly the self-certifying
        # contiguity the guard exists to refuse
        calendar = self._exchange_calendar
        if calendar is None:
            return None
        try:
            end_ordinal = calendar.ordinal(visible_session)
        except NotASessionError:
            return None
        start_ordinal = end_ordinal - DOLLAR_VOLUME_WINDOW_SESSIONS + 1
        if start_ordinal < 0:
            return None
        window = calendar.sessions()[start_ordinal : end_ordinal + 1]
        if any(session not in rows for session in window):
            return None
        median = statistics.median([rows[session][0] * rows[session][1] for session in window])
        return AsOf(value=median, available_at=received, provenance="vendor")

    def candidate_snapshot(
        self, contract: OptionContract, decision_session: date
    ) -> CandidateSnapshot:
        """The §9.2 snapshot from the visible session's derived cell, built
        by `build_option_candidate_inputs` (G3): |delta| under the ratified
        provenance, session volume from the bar, bid/ask/OI None. The two
        unconditional lane-1 stamps are mirrored from the overlay itself.

        (R2-P1-c, folded into `decision_close` by R3-P1-2) `decision_at`
        comes from the DECISION-grid calendar when the constructor carries
        one — early-close aware, exactly the close
        `CandidateFilter.evaluate` demands — and from the overlay
        (execution) calendar otherwise (today's behavior). The 13:00/16:00
        disagreement cannot move the VISIBLE session: publication walls sit
        at T+1 09:00, so both closes of one session see the same file."""
        decision_at = self.decision_close(decision_session)
        underlying = contract.underlying_security_id
        session = self.visible_file_session(underlying, decision_at)
        if session is None:
            raise NoOptionFileError(
                f"no captured session for {underlying} visible at {decision_at}"
            )
        received = self._overlay.publication_of(session)
        if self._underlying_liquidity_term == "dropped_no_equity_aggregates":
            # (R2-P2-d, Codex round 2) the DECLARED liquidity term decides
            # whether the dollar-volume input exists AT ALL. The ruled 0.2.2
            # disposition's premise is NO source, so the snapshot supplies
            # ABSENCE (`None`) — never the sentinel minted into a dropped
            # regime (any supplied value is regime-incoherent to the filter,
            # and the ruled NOT_APPLICABLE disclosure would be unreachable).
            dollar_volume: AsOf | None = None
        else:
            # (w3) the ruled P0-1 chain: the declared v2 dollar-volume source
            # when it can answer a true 20-session median, else the overlay's
            # declared Decimal("0") sentinel — the term fails honestly until
            # the post-closeout equity-aggregates recapture lands.
            sourced = self._dollar_volume_as_of(underlying, session, received)
            dollar_volume = (
                sourced
                if sourced is not None
                else AsOf(
                    value=self._overlay.median_dollar_volume(underlying, session),
                    available_at=received,
                )
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
    "DOLLAR_VOLUME_WINDOW_SESSIONS",
    "REPO_EXCHANGE_CALENDAR_CONTENT_SHA256",
    "SPOT_SENTINEL_SESSION",
    "VwapChainEntry",
    "VwapMarkQuote",
    "VwapPitSurface",
    "load_spot_proxy_v2",
    "repo_exchange_calendar",
]
