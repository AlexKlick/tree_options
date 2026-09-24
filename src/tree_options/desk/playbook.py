"""The desk's sealed playbook (plan D5): ``data/desk/playbook/v<N>.toml``.

``<name>.sha256`` holds the sha256 of the file bytes and ``SEALS.md`` the
append-only seal rows; :func:`load_playbook` refuses a file whose bytes do
not match its sidecar, or whose sha256 has no seal row. A change is a new
version, sealed by :func:`seal_playbook` before it is used.

The loader is fail-closed and has NO defaults: every table and every row
must state every key (``"none"`` is an explicit value where a rule is
off), unknown keys are refused, and money is a string (never a TOML
float). It enforces by construction:

* a row points a direction only with the allowed signals
  (:data:`signals.ALLOWED_DIRECTION`); a banned, context-only or unknown
  signal name is refused anywhere, dormant rows included;
* bear rows exist only as ``dormant`` rows with no capacity (shorts are
  refuted at every horizon, RESEARCH-LEDGER.md);
* every row's exits build the engine's own :class:`trex.plan.ExitRules`
  and pass the engine's kind rules (a probe :class:`LegStructure` of the
  row's kind is validated), plus an explicit time stop;
* the drift weight is the pinned signal weight on signal rows and 0
  elsewhere; the XSMOM leveraged-ETF picks are logged, never substituted;
* the ``[limits]`` table has exactly the rails lane's keys.

Only stdlib and tree_options imports (desk/ never imports research code).
"""

from __future__ import annotations

import hashlib
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from pathlib import Path
from typing import Any, cast

from tree_options.desk import paths
from tree_options.desk.signals import ALLOWED_DIRECTION, BannedSignalError, require_direction_signal
from tree_options.desk.store import atomic_write_bytes
from tree_options.desk.universe import CHAIN_UNIVERSE, NO_OPTIONS_EXPRESSION, PANEL_ETFS
from tree_options.trex.plan import ExitRules, Kind, Leg, LegStructure, StopLoss, TakeProfit

SCHEMA = "desk-playbook/1"
DEFAULT_FILE = "v1.toml"
SEALS_FILE = "SEALS.md"
NONE = "none"

TIERS = frozenset({"signal-validated", "textbook-prior", "risk-control"})
STATUSES = frozenset({"active", "dormant"})
KINDS = frozenset(
    {"long_single", "debit_vertical", "credit_vertical", "iron_condor", "calendar", "diagonal"}
)
TWO_EXPIRY = frozenset({"calendar", "diagonal"})
VOL_STATES = frozenset({"cheap", "fair", "rich"})
DIRECTIONS = frozenset({"bull", "bear", "none", "any"})
NAME_TERMS = frozenset({"any", "contango", "backwardation", "steep_contango"})
MARKET_TERMS = frozenset({"any", "contango", "backwardation"})
EVENT_CONDITIONS = frozenset({"any", "no_event", "macro_event_ahead"})
FRONT_OVER_BACK = frozenset({"any", "required"})
BOOK_CONDITIONS = frozenset({"any", "net_delta_over_cap"})
NEWS = frozenset({"block_if_flagged", "ignore"})
FIDELITY = frozenset({"validated_only", "unvalidated_wide_band", "not_used"})
MACRO_KINDS = frozenset(
    {"fomc", "fomc_unscheduled", "fomc_notation_vote", "cpi", "nfp", "opex", "vix_expiry"}
)
EARNINGS_STATUSES = frozenset({"sealed", "confirmed", "estimated"})

LIMIT_MONEY: tuple[str, ...] = (
    "max_loss_per_trade_usd",
    "max_book_loss_usd",
    "max_net_beta_delta_usd_per_1pct_spy",
    "max_roundtrip_cost_frac_of_max_loss",
    "max_leg_spread_frac_of_mid",
    "min_long_single_abs_delta",
    "max_book_short_vega_usd_per_volpt",
)
LIMIT_INT: tuple[str, ...] = (
    "max_per_underlying",
    "max_admissions_per_session",
    "min_leg_open_interest",
    "max_chain_age_sessions",
)


class PlaybookError(ValueError):
    pass


class PlaybookSealError(PlaybookError):
    pass


# ------------------------------------------------------------- dataclasses


@dataclass(frozen=True)
class Limits:
    max_loss_per_trade_usd: Decimal
    max_book_loss_usd: Decimal
    max_per_underlying: int
    max_net_beta_delta_usd_per_1pct_spy: Decimal
    max_admissions_per_session: int
    max_roundtrip_cost_frac_of_max_loss: Decimal
    min_leg_open_interest: int
    max_leg_spread_frac_of_mid: Decimal
    min_long_single_abs_delta: Decimal
    max_chain_age_sessions: int
    max_book_short_vega_usd_per_volpt: Decimal


@dataclass(frozen=True)
class VolBand:
    cheap_below: Fraction
    rich_above: Fraction


@dataclass(frozen=True)
class VolStatePolicy:
    metric: str
    source: str
    features_schema: str
    cross_source: str
    window_sessions: int
    min_history: int
    validated: VolBand
    unvalidated: VolBand
    validated_names: frozenset[str]
    validated_basis: str
    har_status_required: str

    def band_for(self, name: str) -> tuple[str, VolBand]:
        if name in self.validated_names:
            return "validated", self.validated
        return "unvalidated", self.unvalidated


@dataclass(frozen=True)
class TermPolicy:
    slope: str
    steep_above: Fraction
    window_sessions: int
    min_history: int
    market_front: str
    market_back: str
    event_front_min_dte: int
    event_back_min_gap_days: int


@dataclass(frozen=True)
class EventPolicy:
    window_sessions: int
    macro_kinds: frozenset[str]
    earnings_statuses: frozenset[str]
    etf_holdings: str


@dataclass(frozen=True)
class DriftPolicy:
    signal_weight: Decimal
    horizon_sessions: int
    excess_20: dict[str, Decimal]
    excess_basis: dict[str, str]


@dataclass(frozen=True)
class XsmomPolicy:
    ranking: str
    no_options_expression: frozenset[str]
    policy: str


@dataclass(frozen=True)
class When:
    direction: str
    signals: tuple[str, ...]
    vol: frozenset[str] | None  # None: any vol state (the row reads none)
    name_term: str
    market_term: str
    events: str
    front_over_back: str
    book: str
    news: str


@dataclass(frozen=True)
class Universe:
    names: tuple[str, ...]
    min_liquidity_score: int


@dataclass(frozen=True)
class LegSpec:
    role: str
    right: str
    action: str
    strike: str  # by_delta | same_as_<role>
    abs_delta: tuple[Decimal, Decimal] | None  # None when the strike is another leg's
    dte: tuple[int, int] | None  # None: the expiry of ``same_expiry_as``
    same_expiry_as: str | None


@dataclass(frozen=True)
class TimeStop:
    hold_sessions: int | None
    min_dte: int | None
    sessions_after_event: int | None


@dataclass(frozen=True)
class RowExits:
    rules: ExitRules
    time_stop: TimeStop


@dataclass(frozen=True)
class Row:
    id: str
    number: int
    title: str
    tier: str
    evidence: str
    status: str
    kind: str
    max_open: int
    iv_fidelity: str
    when: When
    universe: Universe
    legs: tuple[LegSpec, ...]
    exits: RowExits
    drift_view: str
    drift_weight: Decimal
    expiry_gap_days: tuple[int, int] | None
    dormant_reason: str | None

    @property
    def is_signal_row(self) -> bool:
        return self.status == "active" and bool(self.when.signals)


@dataclass(frozen=True)
class Playbook:
    sha256: str
    version: str
    written: str
    delta_source: str
    limits: Limits
    vol_state: VolStatePolicy
    term: TermPolicy
    events: EventPolicy
    drift: DriftPolicy
    xsmom: XsmomPolicy
    rows: tuple[Row, ...]

    def active_rows(self) -> tuple[Row, ...]:
        return tuple(r for r in self.rows if r.status == "active")


# ---------------------------------------------------------------- helpers


def _table(obj: Any, where: str, required: Sequence[str], optional: Sequence[str] = ()) -> dict:
    if not isinstance(obj, dict):
        raise PlaybookError(f"{where}: not a table")
    keys = set(obj)
    missing = set(required) - keys
    unknown = keys - set(required) - set(optional)
    if missing:
        raise PlaybookError(f"{where}: missing {sorted(missing)} (the playbook has no defaults)")
    if unknown:
        raise PlaybookError(f"{where}: unknown {sorted(unknown)}")
    return obj


def _str(v: Any, where: str, choices: frozenset[str] | None = None) -> str:
    if not isinstance(v, str) or not v:
        raise PlaybookError(f"{where}: must be a non-empty string")
    if choices is not None and v not in choices:
        raise PlaybookError(f"{where}: {v!r} not in {sorted(choices)}")
    return v


def _int(v: Any, where: str, lo: int = 0) -> int:
    if type(v) is not int or v < lo:
        raise PlaybookError(f"{where}: must be an integer >= {lo}")
    return v


def _int_or_none(v: Any, where: str, lo: int = 1) -> int | None:
    return None if v == NONE else _int(v, where, lo)


def _bool(v: Any, where: str) -> bool:
    if type(v) is not bool:
        raise PlaybookError(f"{where}: must be true or false")
    return v


def _dec(v: Any, where: str) -> Decimal:
    """A decimal given as a string (TOML floats never carry money)."""
    if not isinstance(v, str):
        raise PlaybookError(f"{where}: must be a decimal string, not {type(v).__name__}")
    try:
        d = Decimal(v)
    except InvalidOperation as exc:
        raise PlaybookError(f"{where}: {v!r} is not a decimal") from exc
    if not d.is_finite():
        raise PlaybookError(f"{where}: {v!r} is not finite")
    return d


def _positive(v: Any, where: str) -> Decimal:
    d = _dec(v, where)
    if d <= 0:
        raise PlaybookError(f"{where}: must be > 0")
    return d


def _frac(v: Any, where: str) -> Fraction:
    """A fraction strictly between 0 and 1, written "a/b" or as a decimal."""
    if not isinstance(v, str):
        raise PlaybookError(f"{where}: must be a string like '1/3'")
    try:
        f = Fraction(v)
    except (ValueError, ZeroDivisionError) as exc:
        raise PlaybookError(f"{where}: {v!r} is not a fraction") from exc
    if not 0 < f < 1:
        raise PlaybookError(f"{where}: {v!r} must lie strictly between 0 and 1")
    return f


def _str_list(v: Any, where: str) -> list[str]:
    if not isinstance(v, list) or not all(isinstance(x, str) and x for x in v):
        raise PlaybookError(f"{where}: must be a list of strings")
    if len(set(v)) != len(v):
        raise PlaybookError(f"{where}: duplicates")
    return list(v)


# --------------------------------------------------------------- sections


def _limits(obj: Any) -> Limits:
    t = _table(obj, "[limits]", LIMIT_MONEY + LIMIT_INT)
    vals: dict[str, Any] = {k: _positive(t[k], f"[limits] {k}") for k in LIMIT_MONEY}
    vals.update({k: _int(t[k], f"[limits] {k}", 1) for k in LIMIT_INT})
    return Limits(**vals)


def _band(cheap: Any, rich: Any, where: str) -> VolBand:
    band = VolBand(_frac(cheap, f"{where} cheap_below"), _frac(rich, f"{where} rich_above"))
    if not band.cheap_below < band.rich_above:
        raise PlaybookError(f"{where}: cheap_below must lie below rich_above")
    return band


def _window(t: dict, where: str) -> tuple[int, int]:
    window = _int(t["window_sessions"], f"{where} window_sessions", 1)
    min_history = _int(t["min_history"], f"{where} min_history", 1)
    if min_history > window:
        raise PlaybookError(f"{where}: min_history exceeds the window")
    return window, min_history


def _vol_state(obj: Any) -> VolStatePolicy:
    w = "[vol_state]"
    t = _table(
        obj,
        w,
        (
            "metric",
            "source",
            "features_schema",
            "cross_source",
            "window_sessions",
            "min_history",
            "cheap_below",
            "rich_above",
            "unvalidated_cheap_below",
            "unvalidated_rich_above",
            "validated_names",
            "validated_basis",
            "har_status_required",
        ),
    )
    window, min_history = _window(t, w)
    validated = _band(t["cheap_below"], t["rich_above"], w)
    unvalidated = _band(
        t["unvalidated_cheap_below"], t["unvalidated_rich_above"], f"{w} unvalidated"
    )
    if not (
        unvalidated.cheap_below <= validated.cheap_below
        and unvalidated.rich_above >= validated.rich_above
    ):
        raise PlaybookError(f"{w}: the unvalidated band must be at least as wide as the validated")
    names = _str_list(t["validated_names"], f"{w} validated_names")
    bad = [n for n in names if n not in CHAIN_UNIVERSE]
    if bad:
        raise PlaybookError(f"{w} validated_names: {bad} not in the chain universe")
    return VolStatePolicy(
        metric=_str(t["metric"], f"{w} metric", frozenset({"iv30_over_har20"})),
        source=_str(t["source"], f"{w} source", frozenset({"chain"})),
        features_schema=_str(t["features_schema"], f"{w} features_schema"),
        # v1 never mixes the VWAP history with chain values (no measured offset)
        cross_source=_str(t["cross_source"], f"{w} cross_source", frozenset({"forbidden"})),
        window_sessions=window,
        min_history=min_history,
        validated=validated,
        unvalidated=unvalidated,
        validated_names=frozenset(names),
        validated_basis=_str(t["validated_basis"], f"{w} validated_basis"),
        har_status_required=_str(
            t["har_status_required"], f"{w} har_status_required", frozenset({"validated"})
        ),
    )


def _term(obj: Any) -> TermPolicy:
    w = "[term]"
    t = _table(
        obj,
        w,
        (
            "slope",
            "steep_above",
            "window_sessions",
            "min_history",
            "market_front",
            "market_back",
            "event_front_min_dte",
            "event_back_min_gap_days",
        ),
    )
    window, min_history = _window(t, w)
    return TermPolicy(
        slope=_str(t["slope"], f"{w} slope", frozenset({"iv90_over_iv30_minus_1"})),
        steep_above=_frac(t["steep_above"], f"{w} steep_above"),
        window_sessions=window,
        min_history=min_history,
        market_front=_str(t["market_front"], f"{w} market_front"),
        market_back=_str(t["market_back"], f"{w} market_back"),
        event_front_min_dte=_int(t["event_front_min_dte"], f"{w} event_front_min_dte", 1),
        event_back_min_gap_days=_int(
            t["event_back_min_gap_days"], f"{w} event_back_min_gap_days", 1
        ),
    )


def _events(obj: Any) -> EventPolicy:
    w = "[events]"
    t = _table(obj, w, ("window_sessions", "macro_kinds", "earnings_statuses", "etf_holdings"))
    kinds = _str_list(t["macro_kinds"], f"{w} macro_kinds")
    if not kinds or not set(kinds) <= MACRO_KINDS:
        raise PlaybookError(f"{w} macro_kinds: must be a non-empty subset of {sorted(MACRO_KINDS)}")
    statuses = _str_list(t["earnings_statuses"], f"{w} earnings_statuses")
    if set(statuses) != EARNINGS_STATUSES:
        # estimated dates block too (they may never trigger; blocking is the safe side)
        raise PlaybookError(f"{w} earnings_statuses: must be exactly {sorted(EARNINGS_STATUSES)}")
    return EventPolicy(
        window_sessions=_int(t["window_sessions"], f"{w} window_sessions", 1),
        macro_kinds=frozenset(kinds),
        earnings_statuses=frozenset(statuses),
        etf_holdings=_str(t["etf_holdings"], f"{w} etf_holdings", frozenset({"count"})),
    )


def _drift(obj: Any) -> DriftPolicy:
    w = "[drift]"
    t = _table(obj, w, ("signal_weight", "horizon_sessions", "excess_20", "excess_basis"))
    weight = _dec(t["signal_weight"], f"{w} signal_weight")
    if not 0 < weight <= 1:
        raise PlaybookError(f"{w} signal_weight: must lie in (0, 1]")
    excess = _table(t["excess_20"], f"{w} excess_20", sorted(ALLOWED_DIRECTION))
    basis = _table(t["excess_basis"], f"{w} excess_basis", sorted(ALLOWED_DIRECTION))
    return DriftPolicy(
        signal_weight=weight,
        horizon_sessions=_int(t["horizon_sessions"], f"{w} horizon_sessions", 1),
        excess_20={k: _dec(v, f"{w} excess_20 {k}") for k, v in sorted(excess.items())},
        excess_basis={k: _str(v, f"{w} excess_basis {k}") for k, v in sorted(basis.items())},
    )


def _xsmom(obj: Any) -> XsmomPolicy:
    w = "[xsmom]"
    t = _table(obj, w, ("ranking", "no_options_expression", "policy"))
    noexpr = frozenset(_str_list(t["no_options_expression"], f"{w} no_options_expression"))
    if noexpr != NO_OPTIONS_EXPRESSION:
        raise PlaybookError(f"{w} no_options_expression: must be {sorted(NO_OPTIONS_EXPRESSION)}")
    return XsmomPolicy(
        ranking=_str(t["ranking"], f"{w} ranking"),
        no_options_expression=noexpr,
        # an XSMOM pick without an options expression is logged; the next
        # name is never substituted (plan D5)
        policy=_str(t["policy"], f"{w} policy", frozenset({"log_never_substitute"})),
    )


# ------------------------------------------------------------------- rows

_ROW_KEYS = (
    "id",
    "number",
    "title",
    "tier",
    "evidence",
    "status",
    "kind",
    "max_open",
    "iv_fidelity",
    "when",
    "universe",
    "legs",
    "exits",
    "drift",
)


def _when(obj: Any, where: str, tier: str) -> When:
    w = f"{where} when"
    t = _table(
        obj,
        w,
        (
            "direction",
            "signals",
            "vol",
            "name_term",
            "market_term",
            "events",
            "front_over_back",
            "book",
            "news",
        ),
    )
    names = _str_list(t["signals"], f"{w} signals")
    for name in names:
        try:
            require_direction_signal(name)
        except BannedSignalError as exc:
            raise PlaybookError(f"{w} signals: {exc}") from exc
    direction = _str(t["direction"], f"{w} direction", DIRECTIONS)
    if (direction == "bull") != bool(names):
        raise PlaybookError(
            f"{w}: a bull row names its allowed direction signals; no other row names any"
        )
    if t["vol"] == "any":
        vol = None
    else:
        states = _str_list(t["vol"], f"{w} vol")
        if not states or not set(states) <= VOL_STATES:
            raise PlaybookError(f"{w} vol: 'any' or a non-empty subset of {sorted(VOL_STATES)}")
        vol = frozenset(states)
    news = _str(t["news"], f"{w} news", NEWS)
    if news == "ignore" and tier != "risk-control":
        raise PlaybookError(f"{w} news: only a risk-control row may ignore the news veto")
    return When(
        direction=direction,
        signals=tuple(names),
        vol=vol,
        name_term=_str(t["name_term"], f"{w} name_term", NAME_TERMS),
        market_term=_str(t["market_term"], f"{w} market_term", MARKET_TERMS),
        events=_str(t["events"], f"{w} events", EVENT_CONDITIONS),
        front_over_back=_str(t["front_over_back"], f"{w} front_over_back", FRONT_OVER_BACK),
        book=_str(t["book"], f"{w} book", BOOK_CONDITIONS),
        news=news,
    )


def _universe(obj: Any, where: str) -> Universe:
    w = f"{where} universe"
    t = _table(obj, w, ("names", "min_liquidity_score"))
    raw = t["names"]
    if raw == "chain_universe":
        names = CHAIN_UNIVERSE
    elif raw == "chain_etfs":
        names = tuple(n for n in CHAIN_UNIVERSE if n in PANEL_ETFS)
    else:
        listed = _str_list(raw, f"{w} names")
        lev = sorted(set(listed) & NO_OPTIONS_EXPRESSION)
        if lev:
            raise PlaybookError(f"{w} names: {lev} have no options expression")
        bad = [n for n in listed if n not in CHAIN_UNIVERSE]
        if bad or not listed:
            raise PlaybookError(f"{w} names: {bad or 'empty'} not in the chain universe")
        names = tuple(listed)
    return Universe(names, _int(t["min_liquidity_score"], f"{w} min_liquidity_score", 0))


def _delta_range(v: Any, where: str) -> tuple[Decimal, Decimal]:
    if not isinstance(v, list) or len(v) != 2:
        raise PlaybookError(f"{where}: a [low, high] pair of decimal strings")
    lo, hi = _dec(v[0], where), _dec(v[1], where)
    if not 0 < lo < hi < 1:
        raise PlaybookError(f"{where}: needs 0 < low < high < 1")
    return lo, hi


def _dte_range(v: Any, where: str) -> tuple[int, int]:
    if not isinstance(v, list) or len(v) != 2:
        raise PlaybookError(f"{where}: a [low, high] pair of days")
    lo, hi = _int(v[0], where, 1), _int(v[1], where, 1)
    if lo > hi:
        raise PlaybookError(f"{where}: low above high")
    return lo, hi


def _leg(obj: Any, where: str) -> LegSpec:
    t = _table(obj, where, ("role", "right", "action", "strike", "abs_delta", "dte"))
    role = _str(t["role"], f"{where} role")
    strike = _str(t["strike"], f"{where} strike")
    if strike == "by_delta":
        abs_delta: tuple[Decimal, Decimal] | None = _delta_range(
            t["abs_delta"], f"{where} abs_delta"
        )
    elif strike.startswith("same_as_") and t["abs_delta"] == "n/a":
        abs_delta = None
    else:
        raise PlaybookError(
            f"{where}: strike 'by_delta' with an abs_delta range, or 'same_as_<role>' with 'n/a'"
        )
    dte: tuple[int, int] | None
    same: str | None
    if isinstance(t["dte"], str) and t["dte"].startswith("same_as_"):
        dte, same = None, t["dte"][len("same_as_") :]
    else:
        dte, same = _dte_range(t["dte"], f"{where} dte"), None
    return LegSpec(
        role=role,
        right=_str(t["right"], f"{where} right", frozenset({"C", "P"})),
        action=_str(t["action"], f"{where} action", frozenset({"BUY", "SELL"})),
        strike=strike,
        abs_delta=abs_delta,
        dte=dte,
        same_expiry_as=same,
    )


def _above(a: LegSpec, b: LegSpec) -> bool:
    """Every |delta| of ``a`` strictly above every |delta| of ``b``."""
    return a.abs_delta is not None and b.abs_delta is not None and a.abs_delta[0] > b.abs_delta[1]


def _one_expiry(legs: Sequence[LegSpec], where: str) -> None:
    concrete = [g for g in legs if g.dte is not None]
    if len(concrete) != 1:
        raise PlaybookError(f"{where}: one leg states the dte, the others share its expiry")
    for g in legs:
        if g.dte is None and g.same_expiry_as != concrete[0].role:
            raise PlaybookError(
                f"{where}: leg {g.role} must share the expiry of {concrete[0].role}"
            )
    for g in legs:
        if g.strike != "by_delta":
            raise PlaybookError(f"{where}: leg {g.role} must pick its strike by delta")


def _shape(
    kind: str, legs: tuple[LegSpec, ...], gap: tuple[int, int] | None, where: str, limits: Limits
) -> None:
    roles = [g.role for g in legs]
    if len(set(roles)) != len(roles):
        raise PlaybookError(f"{where}: duplicate leg roles")
    buys = [g for g in legs if g.action == "BUY"]
    sells = [g for g in legs if g.action == "SELL"]
    if kind == "long_single":
        if len(legs) != 1 or not buys or legs[0].dte is None or legs[0].abs_delta is None:
            raise PlaybookError(f"{where}: one BUY leg with its own dte and delta range")
        if legs[0].abs_delta[0] < limits.min_long_single_abs_delta:
            raise PlaybookError(f"{where}: a long single needs |delta| >= the limits floor")
        return
    if kind in ("debit_vertical", "credit_vertical"):
        if len(legs) != 2 or len(buys) != 1 or len(sells) != 1 or buys[0].right != sells[0].right:
            raise PlaybookError(f"{where}: one BUY and one SELL leg of one right")
        _one_expiry(legs, where)
        # the debit vertical buys the strike nearer the money, the credit
        # vertical sells it (and buys the protective wing further out)
        near, far = (buys[0], sells[0]) if kind == "debit_vertical" else (sells[0], buys[0])
        if not _above(near, far):
            raise PlaybookError(f"{where}: the {near.action} leg's |delta| range must lie above")
        return
    if kind == "iron_condor":
        if len(legs) != 4:
            raise PlaybookError(f"{where}: four legs")
        _one_expiry(legs, where)
        for right in ("P", "C"):
            b = [g for g in buys if g.right == right]
            s = [g for g in sells if g.right == right]
            if len(b) != 1 or len(s) != 1 or not _above(s[0], b[0]):
                raise PlaybookError(
                    f"{where}: per side one SELL above one protective BUY in |delta|"
                )
        return
    # calendar / diagonal: SELL the front, BUY the back
    if len(legs) != 2 or len(buys) != 1 or len(sells) != 1 or buys[0].right != sells[0].right:
        raise PlaybookError(f"{where}: one BUY (back) and one SELL (front) leg of one right")
    back, front = buys[0], sells[0]
    if back.dte is None or front.dte is None or gap is None:
        raise PlaybookError(f"{where}: both legs state their dte; the row states expiry_gap_days")
    if front.strike != "by_delta":
        raise PlaybookError(f"{where}: the front leg picks its strike by delta")
    if not (back.dte[1] - front.dte[0] >= gap[0] and back.dte[0] - front.dte[1] <= gap[1]):
        raise PlaybookError(f"{where}: no expiry pair satisfies the dte windows and the gap")
    if kind == "calendar":
        if back.strike != f"same_as_{front.role}":
            raise PlaybookError(f"{where}: a calendar's back leg shares the front strike")
    elif not _above(back, front):
        # protective: long call strike below the short (higher |delta|), long
        # put strike above the short (higher |delta|)
        raise PlaybookError(f"{where}: diagonal not protective (back |delta| must lie above)")


_PROBE_ENTRY, _PROBE_DEADLINE = date(2030, 1, 2), date(2030, 1, 3)
_PROBE_FRONT, _PROBE_BACK = date(2030, 2, 15), date(2030, 3, 15)


def _probe_legs(kind: str, right: str) -> tuple[Leg, ...]:
    """Legs of a structure of ``kind`` that the engine accepts, so its own
    validator can judge a row's exits (strikes and dates are arbitrary)."""
    r = cast(Any, right)

    def leg(rt: str, action: str, strike: int, expiry: date = _PROBE_FRONT) -> Leg:
        return Leg(
            right=cast(Any, rt), action=cast(Any, action), strike=Decimal(strike), expiry=expiry
        )

    up = right == "C"
    if kind == "long_single":
        return (leg(r, "BUY", 100),)
    if kind == "debit_vertical":
        return (leg(r, "BUY", 100 if up else 110), leg(r, "SELL", 110 if up else 100))
    if kind == "credit_vertical":
        return (leg(r, "SELL", 100), leg(r, "BUY", 110 if up else 90))
    if kind == "iron_condor":
        return (
            leg("P", "BUY", 80),
            leg("P", "SELL", 90),
            leg("C", "SELL", 110),
            leg("C", "BUY", 120),
        )
    if kind == "calendar":
        return (leg(r, "SELL", 100), leg(r, "BUY", 100, _PROBE_BACK))
    return (leg(r, "SELL", 105 if up else 95), leg(r, "BUY", 95 if up else 105, _PROBE_BACK))


def _exits(obj: Any, where: str, kind: str, right: str, when: When) -> RowExits:
    w = f"{where} exits"
    t = _table(
        obj, w, ("touch", "breach", "take_profit", "stop_loss", "stop_confirm_ticks", "time_stop")
    )
    tp_raw, sl_raw = t["take_profit"], t["stop_loss"]
    try:
        take_profit = None
        if tp_raw != NONE:
            tp = _table(tp_raw, f"{w} take_profit", ("basis", "value"))
            take_profit = TakeProfit(basis=tp["basis"], value=_dec(tp["value"], f"{w} take_profit"))
        stop_loss = None
        if sl_raw != NONE:
            sl = _table(sl_raw, f"{w} stop_loss", ("basis", "value"))
            stop_loss = StopLoss(basis=sl["basis"], value=_dec(sl["value"], f"{w} stop_loss"))
        rules = ExitRules(
            touch=_bool(t["touch"], f"{w} touch"),
            breach=_bool(t["breach"], f"{w} breach"),
            take_profit=take_profit,
            stop_loss=stop_loss,
            stop_confirm_ticks=_int(t["stop_confirm_ticks"], f"{w} stop_confirm_ticks", 1),
        )
        LegStructure(
            id="playbook-probe",
            underlying="PROBE",
            kind=cast(Kind, kind),
            legs=_probe_legs(kind, right),
            quantity=1,
            entry_date=_PROBE_ENTRY,
            exit_deadline=_PROBE_DEADLINE,
            limit=Decimal(1),
            exits=rules,
        )
    except PlaybookError:
        raise
    except ValueError as exc:  # the engine's own model refused them
        raise PlaybookError(f"{w}: the engine refuses these exits: {exc}") from exc
    ts = _table(
        t["time_stop"], f"{w} time_stop", ("hold_sessions", "min_dte", "sessions_after_event")
    )
    time_stop = TimeStop(
        hold_sessions=_int_or_none(ts["hold_sessions"], f"{w} hold_sessions"),
        min_dte=_int_or_none(ts["min_dte"], f"{w} min_dte"),
        sessions_after_event=_int_or_none(ts["sessions_after_event"], f"{w} sessions_after_event"),
    )
    if time_stop == TimeStop(None, None, None):
        raise PlaybookError(f"{w} time_stop: at least one bound")
    if time_stop.sessions_after_event is not None and when.events != "macro_event_ahead":
        raise PlaybookError(f"{w} time_stop: sessions_after_event needs an event row")
    if kind in TWO_EXPIRY and time_stop.min_dte is None:
        raise PlaybookError(f"{w} time_stop: a two-expiry kind closes before the front expiry")
    return RowExits(rules, time_stop)


def _row(obj: Any, i: int, limits: Limits, drift: DriftPolicy) -> Row:
    where = f"rows[{i}]"
    if not isinstance(obj, dict):
        raise PlaybookError(f"{where}: not a table")
    kind = _str(obj.get("kind"), f"{where} kind", KINDS)
    status = _str(obj.get("status"), f"{where} status", STATUSES)
    optional = (("expiry_gap_days",) if kind in TWO_EXPIRY else ()) + (
        ("dormant_reason",) if status == "dormant" else ()
    )
    t = _table(obj, where, _ROW_KEYS + optional)
    rid = _str(t["id"], f"{where} id")
    where = f"row {rid}"
    tier = _str(t["tier"], f"{where} tier", TIERS)
    when = _when(t["when"], where, tier)
    max_open = _int(t["max_open"], f"{where} max_open", 0)
    if when.direction == "bear" and status != "dormant":
        raise PlaybookError(
            f"{where}: bear rows stay dormant (shorts are refuted at every horizon)"
        )
    if status == "dormant":
        reason = _str(t["dormant_reason"], f"{where} dormant_reason")
        if max_open != 0:
            raise PlaybookError(f"{where}: a dormant row has max_open 0")
    else:
        reason = None
        if max_open < 1:
            raise PlaybookError(f"{where}: an active row has max_open >= 1")
    fidelity = _str(t["iv_fidelity"], f"{where} iv_fidelity", FIDELITY)
    if (when.vol is None) != (fidelity == "not_used"):
        raise PlaybookError(
            f"{where}: a vol condition declares how it treats unvalidated names; no vol "
            "condition means iv_fidelity 'not_used'"
        )
    gap = (
        _dte_range(t["expiry_gap_days"], f"{where} expiry_gap_days") if kind in TWO_EXPIRY else None
    )
    if not isinstance(t["legs"], list) or not t["legs"]:
        raise PlaybookError(f"{where} legs: a non-empty array")
    legs = tuple(_leg(g, f"{where} legs[{j}]") for j, g in enumerate(t["legs"]))
    _shape(kind, legs, gap, f"{where} ({kind})", limits)
    exits = _exits(t["exits"], where, kind, legs[0].right, when)
    dr = _table(t["drift"], f"{where} drift", ("view", "weight"))
    view = _str(dr["view"], f"{where} drift view", frozenset({"signal", "none"}))
    weight = _dec(dr["weight"], f"{where} drift weight")
    signal_row = status == "active" and bool(when.signals)
    if view != ("signal" if signal_row else "none") or weight != (
        drift.signal_weight if signal_row else 0
    ):
        raise PlaybookError(
            f"{where} drift: signal rows drift at the pinned {drift.signal_weight} x the "
            "backtest excess, every other row at 0 (no view)"
        )
    return Row(
        id=rid,
        number=_int(t["number"], f"{where} number", 1),
        title=_str(t["title"], f"{where} title"),
        tier=tier,
        evidence=_str(t["evidence"], f"{where} evidence"),
        status=status,
        kind=kind,
        max_open=max_open,
        iv_fidelity=fidelity,
        when=when,
        universe=_universe(t["universe"], where),
        legs=legs,
        exits=exits,
        drift_view=view,
        drift_weight=weight,
        expiry_gap_days=gap,
        dormant_reason=reason,
    )


# ------------------------------------------------------------- the file


def parse_playbook(doc: Mapping[str, Any], *, sha256: str) -> Playbook:
    """Validate a parsed playbook document (fail-closed, no defaults)."""
    t = _table(
        dict(doc),
        "playbook",
        (
            "schema",
            "version",
            "written",
            "plan",
            "delta_source",
            "limits",
            "vol_state",
            "term",
            "events",
            "drift",
            "xsmom",
            "rows",
        ),
    )
    _str(t["schema"], "schema", frozenset({SCHEMA}))
    _str(t["plan"], "plan")
    limits = _limits(t["limits"])
    drift = _drift(t["drift"])
    if not isinstance(t["rows"], list) or not t["rows"]:
        raise PlaybookError("rows: a non-empty array of tables")
    rows = tuple(_row(r, i, limits, drift) for i, r in enumerate(t["rows"]))
    ids = [r.id for r in rows]
    if len(set(ids)) != len(ids):
        raise PlaybookError(f"rows: duplicate ids {sorted(i for i in ids if ids.count(i) > 1)}")
    return Playbook(
        sha256=sha256,
        version=_str(t["version"], "version"),
        written=_str(t["written"], "written"),
        delta_source=_str(t["delta_source"], "delta_source"),
        limits=limits,
        vol_state=_vol_state(t["vol_state"]),
        term=_term(t["term"]),
        events=_events(t["events"]),
        drift=drift,
        xsmom=_xsmom(t["xsmom"]),
        rows=rows,
    )


def _sealed(seals: Path, name: str, sha: str) -> bool:
    try:
        lines = seals.read_text().splitlines()
    except OSError:
        return False
    for line in lines:
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if line.startswith("| ") and len(cells) >= 3 and cells[1] == name and cells[2] == sha:
            return True
    return False


def _decode(data: bytes, name: str) -> dict[str, Any]:
    try:
        return tomllib.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise PlaybookError(f"{name}: not TOML ({exc})") from exc


def load_playbook(path: Path | None = None) -> Playbook:
    """The sealed playbook (default ``<DESK_PLAYBOOK_DIR>/v1.toml``)."""
    path = path or paths.playbook_dir() / DEFAULT_FILE
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise PlaybookSealError(f"{path.name}: unreadable ({type(exc).__name__})") from exc
    try:
        fields = path.with_suffix(".sha256").read_text().split()
    except OSError as exc:
        raise PlaybookSealError(f"{path.name}: no sha256 sidecar") from exc
    if len(fields) != 2 or fields[1] != path.name:
        raise PlaybookSealError(f"{path.name}: malformed sha256 sidecar")
    got = hashlib.sha256(data).hexdigest()
    if got != fields[0]:
        raise PlaybookSealError(f"{path.name}: sha256 {got[:12]} != sealed {fields[0][:12]}")
    if not _sealed(path.parent / SEALS_FILE, path.name, got):
        raise PlaybookSealError(f"{path.name}: sha256 {got[:12]} has no row in {SEALS_FILE}")
    return parse_playbook(_decode(data, path.name), sha256=got)


_SEALS_HEADER = """\
# Desk playbook: append-only seal log

Each row seals one playbook version (sha256 of the file bytes, also in the
``.sha256`` sidecar). ``tree_options.desk.playbook.load_playbook`` refuses a
file whose bytes do not match its sidecar or whose sha256 has no row here.
Rows are only ever appended; never edit or delete a row.

| sealed (UTC) | file | sha256 | rows | basis |
|---|---|---|---|---|
"""


def seal_playbook(path: Path, *, basis: str, sealed_at: datetime | None = None) -> str:
    """Validate the file, write its sidecar and append a seal row."""
    data = path.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    pb = parse_playbook(_decode(data, path.name), sha256=sha)  # never seal an invalid file
    atomic_write_bytes(path.with_suffix(".sha256"), f"{sha}  {path.name}\n".encode("ascii"))
    seals = path.parent / SEALS_FILE
    if not seals.exists():
        atomic_write_bytes(seals, _SEALS_HEADER.encode("ascii"))
    at = (sealed_at or datetime.now(UTC)).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    counts = f"rows={len(pb.rows)} active={len(pb.active_rows())}"
    with open(seals, "a") as fh:
        fh.write(f"| {at} | {path.name} | {sha} | {counts} | {basis} |\n")
    return sha
