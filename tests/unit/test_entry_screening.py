"""Kind-aware entry screening: the equity arm (whole-share, long-only, cash-only).

Oracles: the expected cents are recomputed here with ``fractions`` and
``math.ceil`` from the fixture's own numbers (never through the contract
helpers the implementation uses), and every blocker is a frozen ``Blocker``
wire string (C2.7) or the option screen's existing reason string.
"""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from fractions import Fraction
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from tests.unit.trex_fakes import contract_fixture, equity_effect
from tree_options.action_graph import canary
from tree_options.action_graph.canary import (
    EquityEntryFacts,
    entry_session_blockers,
    review_equity_entry,
)
from tree_options.action_graph.capital import CapitalProfile
from tree_options.time.sessions import shift_instant
from tree_options.trex.clock import session_calendar
from tree_options.trex.contracts import Quote, RiskSnapshot
from tree_options.trex.supervised_canary import (
    EQUITY_SCREENING_SCHEMA,
    EquityScreeningInputs,
    OperatorCanaryInputs,
    collect_entry_screening,
    collect_equity_screening,
)
from tree_options.trex.supervised_ibkr import SupervisedEffect

EFFECT_DOC = contract_fixture("equity_effect.json")
RISK_DOC = contract_fixture("risk_snapshot.json")
ACCOUNT = EFFECT_DOC["account_id"]
#: 30 s after the fixture snapshot (fresh: it goes stale after 120 s)
NOW = shift_instant(datetime.fromisoformat(RISK_DOC["as_of"]), 30)
QTY = EFFECT_DOC["quantity"]
LIMIT = EFFECT_DOC["limit_cents"]
STOP = EFFECT_DOC["exit_policy"]["stop_price_cents"]
#: independent oracle (C2.1): notional = qty x limit; max loss = max(stop
#: loss, ceil(20% of notional)) with the 2000 bps default gap reserve
NOTIONAL = QTY * LIMIT
GAP = math.ceil(Fraction(NOTIONAL * 2000, 10000))
MAX_LOSS = max(QTY * (LIMIT - STOP), GAP)


def _profile(**changes) -> CapitalProfile:
    base = CapitalProfile(
        profile_id="paper-5k",
        revision=1,
        intended_capital=Decimal("5000"),
        risk_style="defined-risk",
        goals=("equity-pilot",),
        allowed_strategy_versions=("operational-canary/1", "equity-pilot/1"),
        max_loss_per_trade=Decimal("300"),
        max_open_loss=Decimal("1500"),
        max_daily_loss=Decimal("600"),
        horizon_days=30,
    )
    return replace(base, **changes)


def _facts(**changes) -> EquityEntryFacts:
    base = EquityEntryFacts(
        mandate_account_id=ACCOUNT,
        checked_at=NOW,
        market_data_type="realtime",
        quote_observed_at=shift_instant(NOW, -5),
        settled_cash_cents=NOTIONAL * 10,
        open_loss_cents=0,
        day_realized_loss_cents=0,
        entries_this_session=0,
        owner_healthy=True,
        protective_exit_ready=True,
    )
    return replace(base, **changes)


def test_fixture_oracle_numbers():
    # 2 x 671.50 = 1343.00 USD; the 20% gap reserve (268.60) beats the stop loss (13.00)
    assert (NOTIONAL, QTY * (LIMIT - STOP), MAX_LOSS) == (134300, 1300, 26860)


def test_clean_spy_entry_screens_clear():
    assert review_equity_entry(_profile(), equity_effect(), _facts()) == ()


def test_only_allowlisted_symbols_qualify():
    assert review_equity_entry(_profile(), equity_effect(symbol="QQQ"), _facts()) == (
        "equity_symbol_not_qualified",
    )
    widened = review_equity_entry(
        _profile(),
        equity_effect(symbol="QQQ"),
        _facts(),
        symbol_allowlist=frozenset({"SPY", "QQQ"}),
    )
    assert widened == ()


@pytest.mark.parametrize("kind", ["delayed", "frozen", "unknown", None])
def test_a_non_realtime_quote_blocks(kind):
    blockers = review_equity_entry(_profile(), equity_effect(), _facts(market_data_type=kind))
    assert blockers == ("equity_quote_not_realtime",)


@pytest.mark.parametrize("offset_s", [-31, 1, None])
def test_a_stale_future_or_missing_quote_time_blocks(offset_s):
    observed = None if offset_s is None else shift_instant(NOW, offset_s)
    blockers = review_equity_entry(_profile(), equity_effect(), _facts(quote_observed_at=observed))
    assert blockers == ("quote_stale_or_future",)


@pytest.mark.parametrize(
    ("cash", "expected"),
    [
        (NOTIONAL, ()),
        (NOTIONAL - 1, ("insufficient_cash",)),
        (-1, ("insufficient_cash",)),
        (None, ("insufficient_cash",)),
    ],
    ids=["exact", "one-cent-short", "borrowing", "unknown"],
)
def test_cash_only_notional_must_fit_settled_cash(cash, expected):
    blockers = review_equity_entry(_profile(), equity_effect(), _facts(settled_cash_cents=cash))
    assert blockers == expected


def test_gap_reserve_is_held_against_the_per_trade_cap():
    at_cap = _profile(max_loss_per_trade=Decimal(MAX_LOSS) / 100)
    assert review_equity_entry(at_cap, equity_effect(), _facts()) == ()
    below = _profile(max_loss_per_trade=Decimal(MAX_LOSS - 1) / 100)
    assert review_equity_entry(below, equity_effect(), _facts()) == ("trade_loss_cap_exceeded",)


def test_a_wide_stop_is_held_when_it_exceeds_the_gap_reserve():
    stop = 50000
    stop_loss = QTY * (LIMIT - stop)
    assert stop_loss > GAP
    wide = equity_effect(exit_policy={"stop_price_cents": stop})
    at_cap = _profile(max_loss_per_trade=Decimal(stop_loss) / 100)
    assert review_equity_entry(at_cap, wide, _facts()) == ()
    below = _profile(max_loss_per_trade=Decimal(stop_loss - 1) / 100)
    assert review_equity_entry(below, wide, _facts()) == ("trade_loss_cap_exceeded",)


def test_notional_beyond_the_intended_capital_blocks():
    roomy = _profile(
        max_loss_per_trade=Decimal("5000"),
        max_open_loss=Decimal("5000"),
        max_daily_loss=Decimal("5000"),
    )
    big = equity_effect(quantity=8)  # 8 x 671.50 = 5372.00 > 5000
    blockers = review_equity_entry(roomy, big, _facts(settled_cash_cents=10**9))
    assert blockers == ("equity_notional_exceeds_intended_capital",)


@pytest.mark.parametrize(
    ("entries", "expected"),
    [
        (0, ()),
        (2, ()),
        (3, ("session_entry_limit_reached",)),
        (None, ("session_entry_limit_reached",)),
    ],
)
def test_entries_per_session_cap(entries, expected):
    blockers = review_equity_entry(
        _profile(), equity_effect(), _facts(entries_this_session=entries)
    )
    assert blockers == expected
    assert entry_session_blockers(entries) == expected


def test_session_cap_is_a_parameter():
    assert entry_session_blockers(3, max_entries=4) == ()
    assert entry_session_blockers(4, max_entries=4) == ("session_entry_limit_reached",)
    assert entry_session_blockers(-1) == ("session_entry_limit_reached",)


def test_open_and_daily_loss_caps_hold_the_reserved_loss():
    # max_open_loss 1500.00, max_daily_loss 600.00 (cents below)
    open_edge = 150000 - MAX_LOSS
    day_edge = 60000 - MAX_LOSS
    edge = _facts(open_loss_cents=open_edge, day_realized_loss_cents=day_edge)
    assert review_equity_entry(_profile(), equity_effect(), edge) == ()
    over = _facts(open_loss_cents=open_edge + 1, day_realized_loss_cents=day_edge + 1)
    assert review_equity_entry(_profile(), equity_effect(), over) == (
        "open_loss_cap_exceeded",
        "daily_loss_cap_exceeded",
    )
    unknown = _facts(open_loss_cents=None, day_realized_loss_cents=None)
    assert review_equity_entry(_profile(), equity_effect(), unknown) == (
        "open_exposure_unknown",
        "daily_loss_unknown",
    )


def test_owner_exit_account_and_profile_preconditions():
    blockers = review_equity_entry(
        _profile(max_open_loss=None),
        equity_effect(),
        _facts(owner_healthy=False, protective_exit_ready=False, mandate_account_id="DU7777777"),
    )
    assert blockers == (
        "profile_limits_missing",
        "effect_account_not_mandate_account",
        "broker_owner_unhealthy",
        "protective_exit_unavailable",
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"settled_cash_cents": 1.5},
        {"open_loss_cents": True},
        {"entries_this_session": 1.0},
        {"checked_at": NOW.replace(tzinfo=None)},
    ],
)
def test_facts_refuse_float_money_and_naive_instants(changes):
    with pytest.raises(ValueError):
        _facts(**changes)


# --------------------------------------------------- the collected screening


def _quote(kind: str = "realtime", age_s: int = 5) -> Quote:
    return Quote.model_validate(
        {
            "bid_cents": LIMIT - 2,
            "ask_cents": LIMIT + 2,
            "market_data_type": kind,
            "as_of": shift_instant(NOW, -age_s),
        }
    )


def _inputs(**changes) -> EquityScreeningInputs:
    base = EquityScreeningInputs(
        quote=_quote(),
        risk=RiskSnapshot.model_validate(RISK_DOC),
        owner_healthy=True,
        protective_exit_ready=True,
    )
    return replace(base, **changes)


def _collect(inputs=None, effect=None, entries=0, **kw):
    return collect_equity_screening(
        effect or equity_effect(),
        profile=kw.pop("profile", _profile()),
        mandate_account_id=kw.pop("account", ACCOUNT),
        inputs=inputs or _inputs(),
        entries_this_session=entries,
        clock=lambda: NOW,
    )


def test_collected_equity_screening_is_clear_and_bound_to_its_evidence():
    screening = _collect()
    assert screening.blockers == ()
    assert screening.clear
    assert screening.evidence["schema"] == EQUITY_SCREENING_SCHEMA
    assert screening.evidence["notional_cents"] == NOTIONAL
    assert screening.evidence["max_loss_cents"] == MAX_LOSS
    assert screening.facts.settled_cash_cents == RISK_DOC["cash_cents"]
    assert screening.facts.open_loss_cents == RISK_DOC["open_loss_cents"]
    assert len(screening.screening_sha256) == 64
    moved = _collect(inputs=_inputs(quote=_quote(age_s=6)))
    assert moved.screening_sha256 != screening.screening_sha256


def test_a_stale_risk_snapshot_blocks_and_leaves_cash_unknown():
    stale = RiskSnapshot.model_validate({**RISK_DOC, "as_of": shift_instant(NOW, -121).isoformat()})
    screening = _collect(inputs=_inputs(risk=stale))
    assert screening.blockers[0] == "risk_snapshot_stale"
    assert "insufficient_cash" in screening.blockers
    assert screening.facts.settled_cash_cents is None
    absent = _collect(inputs=_inputs(risk=None))
    assert absent.blockers[0] == "risk_snapshot_stale"


def test_another_accounts_snapshot_is_not_cash_evidence():
    other = RiskSnapshot.model_validate({**RISK_DOC, "account_id": "DU7777777"})
    screening = _collect(inputs=_inputs(risk=other))
    assert "risk_snapshot_account_mismatch" in screening.blockers
    assert "insufficient_cash" in screening.blockers


def test_collected_quote_type_and_session_cap_block():
    delayed = _collect(inputs=_inputs(quote=_quote("delayed")))
    assert delayed.blockers == ("equity_quote_not_realtime",)
    missing = _collect(inputs=_inputs(quote=None))
    assert missing.blockers == ("equity_quote_not_realtime", "quote_stale_or_future")
    capped = _collect(entries=3)
    assert capped.blockers == ("session_entry_limit_reached",)


def test_kind_aware_entry_screening_routes_equities():
    routed = collect_entry_screening(
        equity_effect(),
        profile=_profile(),
        mandate_account_id=ACCOUNT,
        entries_this_session=3,
        equity_inputs=_inputs(),
        clock=lambda: NOW,
    )
    assert routed.blockers == ("session_entry_limit_reached",)
    assert routed.evidence == _collect(entries=3).evidence
    with pytest.raises(ValueError):
        collect_entry_screening(
            equity_effect(),
            profile=_profile(),
            mandate_account_id=ACCOUNT,
            entries_this_session=0,
            clock=lambda: NOW,
        )


# ------------------------------------------- the regular-session entry window

# Oracle: the window is written out as ET wall times on the repo calendar's
# own session days ([open + 5 min, close - 30 min]), never derived through
# the calendar arithmetic the implementation uses.
ET_ZONE = ZoneInfo("America/New_York")
CLOSED = ("entry_window_closed",)
NORMAL_DAY = date(2026, 10, 22)  # Thursday, EDT, 16:00 close: [09:35, 15:30]
EARLY_DAY = date(2026, 11, 27)  # day after Thanksgiving, 13:00 close: [09:35, 12:30]
WINTER_DAY = date(2026, 12, 1)  # Tuesday, EST (UTC-5), 16:00 close: [09:35, 15:30]
SATURDAY = date(2026, 10, 24)
THANKSGIVING = date(2026, 11, 26)


def _et(day: date, hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, second, tzinfo=ET_ZONE)


def _at(instant: datetime, **changes) -> EquityEntryFacts:
    """Otherwise clean facts checked at ``instant`` (a 5 s old realtime quote)."""
    return _facts(checked_at=instant, quote_observed_at=shift_instant(instant, -5), **changes)


def test_the_repo_calendar_has_the_days_these_tests_assume():
    cal = session_calendar()
    assert all(cal.is_session(day) for day in (NORMAL_DAY, EARLY_DAY, WINTER_DAY))
    early = set(cal.early_close_sessions())
    assert EARLY_DAY in early
    assert NORMAL_DAY not in early and WINTER_DAY not in early
    assert not cal.is_session(SATURDAY) and not cal.is_session(THANKSGIVING)


def test_the_window_bounds_are_module_constants():
    assert canary.EQUITY_ENTRY_OPEN_DELAY_S == 5 * 60
    assert canary.EQUITY_ENTRY_CLOSE_CUTOFF_S == 30 * 60
    assert canary.ENTRY_WINDOW_CLOSED == CLOSED[0]
    doc = canary.review_equity_entry.__doc__ or ""
    assert "EQUITY_ENTRY_OPEN_DELAY_S" in doc and "EQUITY_ENTRY_CLOSE_CUTOFF_S" in doc


@pytest.mark.parametrize(
    ("day", "start", "end"),
    [
        (NORMAL_DAY, (9, 35), (15, 30)),
        (EARLY_DAY, (9, 35), (12, 30)),
        (WINTER_DAY, (9, 35), (15, 30)),
    ],
    ids=["normal", "early-close", "winter-est"],
)
def test_the_window_of_a_session(day, start, end):
    assert canary.equity_entry_window(day) == (_et(day, *start), _et(day, *end))


WINDOW_CASES = [
    # a normal day: [09:35:00, 15:30:00] ET, both ends inclusive
    ("normal-open-itself", NORMAL_DAY, (9, 30, 0), False),
    ("normal-last-second-before", NORMAL_DAY, (9, 34, 59), False),
    ("normal-first-second", NORMAL_DAY, (9, 35, 0), True),
    ("normal-midday", NORMAL_DAY, (12, 0, 0), True),
    ("normal-last-second", NORMAL_DAY, (15, 30, 0), True),
    ("normal-first-second-after", NORMAL_DAY, (15, 30, 1), False),
    ("normal-forced-exit-time", NORMAL_DAY, (15, 45, 0), False),
    ("normal-after-close-desk-still-polling", NORMAL_DAY, (16, 10, 0), False),
    ("normal-premarket", NORMAL_DAY, (8, 0, 0), False),
    # an early close (13:00): the window ends at 12:30
    ("early-last-second-before", EARLY_DAY, (9, 34, 59), False),
    ("early-first-second", EARLY_DAY, (9, 35, 0), True),
    ("early-last-second", EARLY_DAY, (12, 30, 0), True),
    ("early-first-second-after", EARLY_DAY, (12, 30, 1), False),
    ("early-before-its-close", EARLY_DAY, (12, 59, 59), False),
    ("early-after-its-close", EARLY_DAY, (13, 30, 0), False),
    ("early-inside-a-normal-window", EARLY_DAY, (15, 0, 0), False),
    # standard time: the window follows the ET wall clock, not UTC
    ("winter-last-second-before", WINTER_DAY, (9, 34, 59), False),
    ("winter-first-second", WINTER_DAY, (9, 35, 0), True),
    ("winter-last-second", WINTER_DAY, (15, 30, 0), True),
    ("winter-first-second-after", WINTER_DAY, (15, 30, 1), False),
    # not a session at all
    ("saturday", SATURDAY, (11, 0, 0), False),
    ("thanksgiving", THANKSGIVING, (11, 0, 0), False),
]


@pytest.mark.parametrize(
    ("day", "wall", "inside"),
    [case[1:] for case in WINDOW_CASES],
    ids=[case[0] for case in WINDOW_CASES],
)
def test_equity_entries_only_inside_the_entry_window(day, wall, inside):
    checked_at = _et(day, *wall)
    blockers = review_equity_entry(_profile(), equity_effect(), _at(checked_at))
    assert blockers == (() if inside else CLOSED)
    assert canary.equity_entry_window_blockers(checked_at) == blockers


@pytest.mark.parametrize(
    ("instant", "expected"),
    [
        # 13:35Z on 2026-10-22 is 09:35 EDT; 14:35Z on 2026-12-01 is 09:35 EST
        (datetime(2026, 10, 22, 13, 35, tzinfo=UTC), ()),
        (datetime(2026, 10, 22, 13, 34, 59, tzinfo=UTC), CLOSED),
        (datetime(2026, 12, 1, 14, 35, tzinfo=UTC), ()),
        (datetime(2026, 12, 1, 14, 34, 59, tzinfo=UTC), CLOSED),
        # 13:35Z on 2026-12-01 is 08:35 EST: the EDT reading would wrongly open
        (datetime(2026, 12, 1, 13, 35, tzinfo=UTC), CLOSED),
        # 01:00Z Friday 2026-10-23 is 21:00 ET Thursday: no session is open
        (datetime(2026, 10, 23, 1, 0, tzinfo=UTC), CLOSED),
    ],
)
def test_the_window_reads_any_instant_in_eastern_time(instant, expected):
    assert canary.equity_entry_window_blockers(instant) == expected


def test_the_session_is_the_eastern_date_of_the_instant():
    # 01:00Z Friday 10-23 is 21:00 ET Thursday; 01:00Z Monday 10-26 is
    # Sunday evening in New York (no session, though Monday's UTC date is one)
    assert canary.equity_entry_session(datetime(2026, 10, 23, 1, 0, tzinfo=UTC)) == NORMAL_DAY
    assert canary.equity_entry_session(datetime(2026, 10, 26, 1, 0, tzinfo=UTC)) is None
    assert canary.equity_entry_session(_et(SATURDAY, 11, 0)) is None


def test_a_naive_instant_refuses():
    with pytest.raises(ValueError):
        canary.equity_entry_window_blockers(datetime(2026, 10, 22, 10, 0))
    with pytest.raises(ValueError):
        canary.equity_entry_session(datetime(2026, 10, 22, 10, 0))


def test_a_closed_window_is_listed_beside_the_other_blockers():
    facts = _at(_et(NORMAL_DAY, 9, 31), market_data_type="delayed")
    assert review_equity_entry(_profile(), equity_effect(), facts) == (
        "entry_window_closed",
        "equity_quote_not_realtime",
    )


def _collect_at(instant: datetime):
    """The collected screening at ``instant`` with fresh quote and snapshot."""
    risk = RiskSnapshot.model_validate({**RISK_DOC, "as_of": shift_instant(instant, -30)})
    quote = Quote.model_validate(
        {
            "bid_cents": LIMIT - 2,
            "ask_cents": LIMIT + 2,
            "market_data_type": "realtime",
            "as_of": shift_instant(instant, -5),
        }
    )
    return collect_equity_screening(
        equity_effect(),
        profile=_profile(),
        mandate_account_id=ACCOUNT,
        inputs=_inputs(quote=quote, risk=risk),
        entries_this_session=0,
        clock=lambda: instant,
    )


def test_collected_screening_refuses_after_an_early_window_and_records_it():
    last = _collect_at(_et(EARLY_DAY, 12, 30))
    assert last.blockers == ()
    late = _collect_at(_et(EARLY_DAY, 12, 30, 1))
    assert late.blockers == CLOSED
    window = {
        "open_delay_s": 300,
        "close_cutoff_s": 1800,
        "session": "2026-11-27",
        "start": _et(EARLY_DAY, 9, 35).astimezone(UTC).isoformat(),
        "end": _et(EARLY_DAY, 12, 30).astimezone(UTC).isoformat(),
    }
    assert late.evidence["entry_window"] == window == last.evidence["entry_window"]
    assert late.evidence["blockers"] == list(CLOSED)
    # the window is bound into the screening digest
    assert late.screening_sha256 != last.screening_sha256


def test_collected_screening_refuses_on_a_non_session():
    weekend = _collect_at(_et(SATURDAY, 11, 0))
    assert weekend.blockers == CLOSED
    assert weekend.evidence["entry_window"] == {
        "open_delay_s": 300,
        "close_cutoff_s": 1800,
        "session": None,
        "start": None,
        "end": None,
    }


def test_the_kind_aware_router_applies_the_window_to_equities():
    routed = collect_entry_screening(
        equity_effect(),
        profile=_profile(),
        mandate_account_id=ACCOUNT,
        entries_this_session=0,
        equity_inputs=_inputs(),
        clock=lambda: _et(NORMAL_DAY, 15, 30, 1),
    )
    assert "entry_window_closed" in routed.blockers


def test_option_screening_has_no_equity_entry_window():
    option = SupervisedEffect.model_validate(contract_fixture("option_effect_canary.json"))

    def unreadable():
        raise RuntimeError("account view down")

    # a broker whose account view fails: the option screen stops there, and a
    # Saturday check instant adds nothing (options keep their own screen)
    broker = SimpleNamespace(
        paper_blockers=lambda account: (), ib=SimpleNamespace(account_snapshot=unreadable)
    )
    screening = collect_entry_screening(
        option,
        profile=_profile(),
        mandate_account_id=option.account_id,
        entries_this_session=0,
        broker=broker,
        option_inputs=OperatorCanaryInputs(
            owner_epoch="owner-test",
            owner_healthy=True,
            assignment_plan_verified=True,
            protective_exit_ready=True,
            current_open_loss=Decimal("0"),
            realized_daily_loss=Decimal("0"),
        ),
        clock=lambda: _et(SATURDAY, 11, 0),
    )
    assert screening.blockers == ("broker_view_unreadable:account_snapshot",)
    assert "entry_window" not in screening.evidence
