"""Desk D5 regime: point-in-time conditions per name per session, and the
playbook rows they match.

Inputs are synthetic (desk-features/1 shaped documents, a signals file,
index closes, report schedules, a macro calendar), built here; every
expected state is derived in the test from the numbers it planted (the
percentile oracle counts by hand), never by calling the implementation.
The real sealed playbook supplies the policy.
"""

from __future__ import annotations

from datetime import date
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from tree_options.desk import playbook, regime
from tree_options.desk.events import MacroEvent

REPO = Path(__file__).resolve().parents[2]
PB_DIR = REPO / "data" / "desk" / "playbook"
SESSION = date(2026, 6, 1)
SCHEMA = "desk-features/1"
NE = "NOT_EVALUABLE"


@pytest.fixture(scope="module")
def pb() -> playbook.Playbook:
    return playbook.load_playbook(PB_DIR / "v2.toml")  # the active version


@pytest.fixture(scope="module")
def pb_v1() -> playbook.Playbook:
    return playbook.load_playbook(PB_DIR / "v1.toml")


def _entry(
    iv30: float | None = 0.20,
    har_vol: float | None = 0.25,
    *,
    har_status: str = "validated",
    slope: float | None = 0.05,
    term: list[list[Any]] | None = None,
    liquidity: int = 50,
    h: Any = 20,
    fit_through: Any = "2024-06-28",
) -> dict[str, Any]:
    """One name's features as desk features writes them (desk-features/1)."""
    return {
        "iv": {"30": iv30, "60": None, "90": None, "180": None},
        "term_slope": slope,
        "atm_term": term or [],
        "liquidity_score": liquidity,
        "forecast": {
            "source": "har",
            "har_status": har_status,
            "har_vol": har_vol,
            "h": h,
            "fit_through": fit_through,
        },
    }


def _doc(
    names: dict[str, dict[str, Any]],
    schema: str = SCHEMA,
    *,
    session: date = SESSION,
    chains: bool = True,
) -> dict[str, Any]:
    """A features document: its own session and the recorded chains' raw sha256."""
    raw = {n: "ab" * 32 for n in names} if chains else {}
    return {
        "schema": schema,
        "session": session.isoformat(),
        "inputs": {"chains_raw_sha256": raw},
        "names": names,
    }


def _prior(cal, n: int, end: date = SESSION) -> list[date]:
    i = cal.ordinal(end)
    return list(cal.sessions()[i - n : i])


def _history(cal, ratios: list[float], name: str = "IWM", **kw: Any) -> dict[date, dict]:
    """features docs for the len(ratios) sessions before SESSION: IV30 =
    ratio x 0.25 over a HAR vol of 0.25 (so R = ratio)."""
    days = _prior(cal, len(ratios))
    return {
        d: _doc({name: _entry(0.25 * r, 0.25, **kw)}, session=d)
        for d, r in zip(days, ratios, strict=True)
    }


def _oracle_state(current: float, hist: list[float], lo: Fraction, hi: Fraction) -> str:
    below = sum(1 for h in hist if h < current)
    p = Fraction(below, len(hist))
    return "cheap" if p < lo else "rich" if p > hi else "fair"


# -------------------------------------------------------------- vol state


class TestVolState:
    def test_percentile_of_the_names_own_history(self, pb, static_calendar) -> None:
        hist = [0.70 + 0.002 * k for k in range(150)]  # R history 0.70..0.998
        feats = _history(static_calendar, hist)
        # off the 0.002 grid: no tie can hinge on a float's last bit
        for current, want_state in ((0.721, "cheap"), (0.851, "fair"), (0.991, "rich")):
            feats[SESSION] = _doc({"IWM": _entry(0.25 * current, 0.25)})
            got = regime.vol_state("IWM", SESSION, static_calendar, feats, pb.vol_state)
            assert got.state == _oracle_state(current, hist, Fraction(1, 3), Fraction(2, 3))
            assert got.state == want_state
            assert got.n == 150 and got.band_name == "validated"
            assert got.percentile == Fraction(sum(1 for h in hist if h < current), 150)
            assert got.ratio == pytest.approx(current)

    def test_ratio_below_one_is_not_cheap_by_itself(self, pb, static_calendar) -> None:
        """The plan's fixed 1.0 line would call this cheap; against the
        name's own history (all below 1, the structural bias) it is rich."""
        hist = [0.60 + 0.001 * k for k in range(150)]
        feats = _history(static_calendar, hist)
        feats[SESSION] = _doc({"IWM": _entry(0.25 * 0.80, 0.25)})
        got = regime.vol_state("IWM", SESSION, static_calendar, feats, pb.vol_state)
        assert got.ratio < 1.0 and got.state == "rich"

    def test_unvalidated_names_use_the_wider_band(self, pb, static_calendar) -> None:
        hist = [float(k) for k in range(1, 151)]
        current = 113.5  # 113 of 150 below: p = 0.753 (> 2/3, < 4/5)
        assert Fraction(2, 3) < Fraction(113, 150) < Fraction(4, 5)
        for name, band, want in (("IWM", "validated", "rich"), ("AAPL", "unvalidated", "fair")):
            feats = _history(static_calendar, hist, name=name)
            feats[SESSION] = _doc({name: _entry(0.25 * current, 0.25)})
            got = regime.vol_state(name, SESSION, static_calendar, feats, pb.vol_state)
            assert (got.band_name, got.state) == (band, want)

    @pytest.mark.parametrize(("n", "evaluable"), [(119, False), (120, True)])
    def test_warm_up_is_not_evaluable_never_fair(self, pb, static_calendar, n, evaluable) -> None:
        hist = [0.9] * n
        feats = _history(static_calendar, hist)
        feats[SESSION] = _doc({"IWM": _entry(0.25 * 0.9, 0.25)})
        got = regime.vol_state("IWM", SESSION, static_calendar, feats, pb.vol_state)
        if evaluable:
            assert got.state == "cheap"  # ties are not below: p = 0
        else:
            assert got.state == "NOT_EVALUABLE" and got.n == 119
            assert "warm-up" in got.reason and got.percentile is None

    def test_only_source_consistent_validated_history_counts(self, pb, static_calendar) -> None:
        days = _prior(static_calendar, 200)
        feats: dict[date, dict] = {}
        for k, d in enumerate(days):
            if k < 60:  # another features schema (another source/method): not counted
                feats[d] = _doc({"IWM": _entry(0.1, 0.25)}, schema="desk-features/0", session=d)
            elif k < 70:  # HAR degraded that day: not evaluable, not counted
                feats[d] = _doc({"IWM": _entry(0.1, 0.25, har_status="degraded")}, session=d)
            elif k < 75:  # no IV30
                feats[d] = _doc({"IWM": _entry(None, 0.25)}, session=d)
            else:
                feats[d] = _doc({"IWM": _entry(0.25 * 0.9, 0.25)}, session=d)
        feats[SESSION] = _doc({"IWM": _entry(0.25 * 0.95, 0.25)})
        got = regime.vol_state("IWM", SESSION, static_calendar, feats, pb.vol_state)
        assert got.n == 125 and got.state == "rich"

    def test_window_is_252_sessions(self, pb, static_calendar) -> None:
        hist = [0.5] * 100 + [0.9] * 252  # the 100 oldest fall outside the window
        feats = _history(static_calendar, hist)
        feats[SESSION] = _doc({"IWM": _entry(0.25 * 0.7, 0.25)})
        got = regime.vol_state("IWM", SESSION, static_calendar, feats, pb.vol_state)
        assert got.n == 252 and got.state == "cheap"

    @pytest.mark.parametrize(
        "current",
        [
            None,  # no features for the name
            _entry(None, 0.25),
            _entry(0.2, 0.25, har_status="degraded"),
            _entry(0.2, None),
            {"status": "NOT_EVALUABLE", "reason": "chain has no underlying close"},
        ],
    )
    def test_current_session_must_be_evaluable(self, pb, static_calendar, current) -> None:
        feats = _history(static_calendar, [0.9] * 150)
        feats[SESSION] = _doc({"IWM": current} if current is not None else {})
        got = regime.vol_state("IWM", SESSION, static_calendar, feats, pb.vol_state)
        assert got.state == "NOT_EVALUABLE"

    def test_current_from_another_source_is_refused(self, pb, static_calendar) -> None:
        feats = _history(static_calendar, [0.9] * 150)
        feats[SESSION] = _doc({"IWM": _entry(0.3, 0.25)}, schema="ivhist-vwap/1")
        got = regime.vol_state("IWM", SESSION, static_calendar, feats, pb.vol_state)
        assert got.state == "NOT_EVALUABLE" and "schema" in got.reason

    def test_future_poison(self, pb, static_calendar) -> None:
        """Nothing dated after the session, nor the session's own value,
        enters the history: poisoning them changes nothing. That includes
        future-dated documents filed under PAST keys (Codex P1-2)."""
        hist = [0.70 + 0.002 * k for k in range(150)]
        feats = _history(static_calendar, hist)
        feats[SESSION] = _doc({"IWM": _entry(0.25 * 0.851, 0.25)})
        base = regime.vol_state("IWM", SESSION, static_calendar, feats, pb.vol_state)
        assert base.state == "fair"
        sessions = static_calendar.sessions()
        i = static_calendar.ordinal(SESSION)
        poisoned = dict(feats)
        for d in sessions[i + 1 : i + 40]:
            poisoned[d] = _doc({"IWM": _entry(9.0, 0.25)}, session=d)
        # the 100 window sessions older than the history, each holding a
        # copy of a document dated after the session
        for d in sessions[i - 252 : i - 150]:
            poisoned[d] = _doc({"IWM": _entry(9.0, 0.25)}, session=sessions[i + 5])
        assert regime.vol_state("IWM", SESSION, static_calendar, poisoned, pb.vol_state) == base


class TestFeatureProvenance:
    """Codex P1-2: a document is identified by its own metadata, never by
    the dictionary key it was filed under."""

    def test_future_dated_documents_under_past_keys_never_count(self, pb, static_calendar) -> None:
        future = static_calendar.sessions()[static_calendar.ordinal(SESSION) + 10]
        days = _prior(static_calendar, 130)
        feats = {d: _doc({"IWM": _entry(0.25 * 0.9, 0.25)}, session=future) for d in days}
        feats[SESSION] = _doc({"IWM": _entry(0.25 * 0.95, 0.25)})
        got = regime.vol_state("IWM", SESSION, static_calendar, feats, pb.vol_state)
        assert (got.state, got.n) == (NE, 0)
        for d in days:  # the same documents, correctly dated, do count
            feats[d] = _doc({"IWM": _entry(0.25 * 0.9, 0.25)}, session=d)
        got = regime.vol_state("IWM", SESSION, static_calendar, feats, pb.vol_state)
        assert (got.state, got.n) == ("rich", 130)

    @pytest.mark.parametrize(
        "bad",
        [
            {"h": 5},  # another horizon
            {"h": None},
            {"fit_through": "2026-12-01"},  # fit after every origin in the window
            {"fit_through": None},  # no fit provenance
            {"fit_through": "last month"},
        ],
    )
    def test_forecast_provenance_is_checked(self, pb, static_calendar, bad) -> None:
        feats = _history(static_calendar, [0.9] * 150, **bad)
        feats[SESSION] = _doc({"IWM": _entry(0.25 * 0.95, 0.25)})
        got = regime.vol_state("IWM", SESSION, static_calendar, feats, pb.vol_state)
        assert (got.state, got.n) == (NE, 0)  # none of the history counts
        feats = _history(static_calendar, [0.9] * 150)
        feats[SESSION] = _doc({"IWM": _entry(0.25 * 0.95, 0.25, **bad)})
        got = regime.vol_state("IWM", SESSION, static_calendar, feats, pb.vol_state)
        assert got.state == NE and got.n == 0

    def test_the_fit_must_precede_the_origin(self, pb, static_calendar) -> None:
        feats = _history(static_calendar, [0.9] * 150)
        feats[SESSION] = _doc({"IWM": _entry(0.25 * 0.95, 0.25, fit_through=SESSION.isoformat())})
        got = regime.vol_state("IWM", SESSION, static_calendar, feats, pb.vol_state)
        assert got.state == NE and "fit" in got.reason
        prev = _prior(static_calendar, 1)[0]
        feats[SESSION] = _doc({"IWM": _entry(0.25 * 0.95, 0.25, fit_through=prev.isoformat())})
        got = regime.vol_state("IWM", SESSION, static_calendar, feats, pb.vol_state)
        assert got.state == "rich"

    def test_missing_metadata_is_refused(self, pb, static_calendar) -> None:
        feats = _history(static_calendar, [0.9] * 150)
        for doc in list(feats.values())[:40]:
            del doc["session"]
        for doc in list(feats.values())[40:80]:
            doc["inputs"] = {"chains_raw_sha256": {}}  # no recorded chain behind the name
        for doc in list(feats.values())[80:100]:
            del doc["inputs"]
        feats[SESSION] = _doc({"IWM": _entry(0.25 * 0.95, 0.25)})
        got = regime.vol_state("IWM", SESSION, static_calendar, feats, pb.vol_state)
        assert (got.state, got.n) == (NE, 50)  # warm-up: only the 50 intact documents count
        feats[SESSION] = _doc({"IWM": _entry(0.25 * 0.95, 0.25)}, chains=False)
        got = regime.vol_state("IWM", SESSION, static_calendar, feats, pb.vol_state)
        assert got.state == NE and "chain" in got.reason and got.n == 0

    def test_slope_history_uses_the_same_rules(self, pb, static_calendar) -> None:
        future = static_calendar.sessions()[static_calendar.ordinal(SESSION) + 3]
        days = _prior(static_calendar, 150)
        feats = {d: _doc({"SPY": _entry(slope=0.01)}, session=future) for d in days}
        feats[SESSION] = _doc({"SPY": _entry(slope=0.14)})
        got = regime.term_state("SPY", SESSION, static_calendar, feats, pb, next_event=None)
        assert got.state == "contango" and got.steep == NE and got.steep_n == 0
        feats[SESSION] = _doc({"SPY": _entry(slope=0.14)}, session=future)  # today's own doc
        got = regime.term_state("SPY", SESSION, static_calendar, feats, pb, next_event=None)
        assert got.state == NE


# ------------------------------------------------------------------- term


def _term_points(front_iv: float, back_iv: float) -> list[list[Any]]:
    # [expiry, dte, iv, n_strikes, how] as desk features writes them
    return [
        ["2026-06-05", 4, 0.30, 5, "bracket"],  # before the event: never the front
        ["2026-06-19", 18, front_iv, 5, "bracket"],
        ["2026-07-02", 31, 0.10, 5, "bracket"],  # only 13 days after the front
        ["2026-07-17", 46, back_iv, 5, "bracket"],
    ]


class TestTerm:
    @pytest.mark.parametrize(
        ("slope", "state"),
        [(0.08, "contango"), (-0.02, "backwardation"), (0.0, "flat"), (None, "NOT_EVALUABLE")],
    )
    def test_name_term_from_the_same_chain(self, pb, static_calendar, slope, state) -> None:
        feats = {SESSION: _doc({"SPY": _entry(slope=slope)})}
        got = regime.term_state("SPY", SESSION, static_calendar, feats, pb, next_event=None)
        assert got.state == state

    def test_steep_contango_is_a_percentile_with_warm_up(self, pb, static_calendar) -> None:
        days = _prior(static_calendar, 150)
        slopes = [0.001 * k for k in range(150)]  # 0.000..0.149
        feats = {
            d: _doc({"SPY": _entry(slope=s)}, session=d) for d, s in zip(days, slopes, strict=True)
        }
        for current, want in ((0.14, "yes"), (0.05, "no")):
            feats[SESSION] = _doc({"SPY": _entry(slope=current)})
            got = regime.term_state("SPY", SESSION, static_calendar, feats, pb, next_event=None)
            p = Fraction(sum(1 for s in slopes if s < current), 150)
            assert (p > Fraction(2, 3)) == (want == "yes")
            assert got.steep == want and got.steep_n == 150
        short = {d: v for d, v in feats.items() if d in days[-50:] or d == SESSION}
        short[SESSION] = _doc({"SPY": _entry(slope=0.14)})
        got = regime.term_state("SPY", SESSION, static_calendar, short, pb, next_event=None)
        assert got.state == "contango" and got.steep == "NOT_EVALUABLE"
        # backwardation is never steep contango, history or not
        short[SESSION] = _doc({"SPY": _entry(slope=-0.1)})
        got = regime.term_state("SPY", SESSION, static_calendar, short, pb, next_event=None)
        assert got.steep == "no"

    @pytest.mark.parametrize(("front", "back", "want"), [(0.25, 0.20, "yes"), (0.18, 0.20, "no")])
    def test_event_inversion(self, pb, static_calendar, front, back, want) -> None:
        """Event 2026-06-10: the front is the first expiry after it with
        DTE >= 7 (06-19); the back the first at least 21 days later (07-17)."""
        feats = {SESSION: _doc({"SPY": _entry(term=_term_points(front, back))})}
        got = regime.term_state(
            "SPY", SESSION, static_calendar, feats, pb, next_event=date(2026, 6, 10)
        )
        assert got.event_inversion == want
        none = regime.term_state("SPY", SESSION, static_calendar, feats, pb, next_event=None)
        assert none.event_inversion == "n/a"
        thin = {SESSION: _doc({"SPY": _entry(term=_term_points(front, back)[:2])})}
        got = regime.term_state(
            "SPY", SESSION, static_calendar, thin, pb, next_event=date(2026, 6, 10)
        )
        assert got.event_inversion == "NOT_EVALUABLE"

    @pytest.mark.parametrize(
        ("vix", "vix3m", "state"),
        [(15.0, 17.0, "contango"), (25.0, 22.0, "backwardation"), (18.0, 18.0, "flat")],
    )
    def test_market_term(self, pb, vix, vix3m, state) -> None:
        idx = {"VIX": {SESSION: vix}, "VIX3M": {SESSION: vix3m}}
        assert regime.market_term(SESSION, idx, pb.term).state == state

    def test_market_term_needs_the_sessions_own_close(self, pb, static_calendar) -> None:
        prev = static_calendar.sessions()[static_calendar.ordinal(SESSION) - 1]
        idx = {"VIX": {prev: 15.0, SESSION: 15.0}, "VIX3M": {prev: 17.0}}
        got = regime.market_term(SESSION, idx, pb.term)
        assert got.state == "NOT_EVALUABLE"


# ----------------------------------------------------------------- events


def _macro(*items: tuple[date, str], gaps: frozenset[str] = frozenset()) -> regime.MacroCalendar:
    return regime.MacroCalendar(
        events=tuple(MacroEvent(d, k, "") for d, k in items),
        covered=(date(2026, 1, 1), date(2027, 12, 31)),
        gaps=gaps,
    )


class TestEvents:
    # SESSION = Mon 2026-06-01; the 5-session window is 06-02..06-08
    def test_earnings_within_none_unknown(self, pb, static_calendar) -> None:
        def state(reports: list[str] | None, name: str = "AAPL") -> str:
            sched = None if reports is None else {name: reports}
            return regime.events_state(
                name, SESSION, static_calendar, pb.events, schedule=sched, macro=_macro()
            ).earnings

        assert state(["2026-04-30", "2026-06-04", "2026-07-30"]) == "within"
        # an after-close report on the session: its reaction session is ahead
        assert state(["2026-04-30", "2026-06-01", "2026-07-30"]) == "within"
        assert state(["2026-04-30", "2026-07-30"]) == "none"
        assert state(["2026-04-30"]) == "unknown"  # nothing known past the window
        assert state(["2026-01-30", "2026-07-30"]) == "unknown"  # a quarter is missing
        assert state(None) == "unknown"  # schedule unavailable
        assert state([]) == "unknown"
        assert state(["2026-06-09", "2026-09-09"]) == "none"  # just after the window

    def test_etfs_have_no_earnings_but_mapped_holdings_flag(self, pb, static_calendar) -> None:
        sched = {h: ["2026-04-28", "2026-07-28"] for h in ("NVDA", "TSM", "AVGO")}
        got = regime.events_state(
            "SMH", SESSION, static_calendar, pb.events, schedule=sched, macro=_macro()
        )
        assert (got.earnings, got.holdings) == ("n/a", "none")
        sched["NVDA"] = ["2026-04-28", "2026-06-03", "2026-08-26"]
        got = regime.events_state(
            "SMH", SESSION, static_calendar, pb.events, schedule=sched, macro=_macro()
        )
        assert got.holdings == "within"
        del sched["TSM"]
        sched["NVDA"] = ["2026-04-28", "2026-08-26"]
        got = regime.events_state(
            "SMH", SESSION, static_calendar, pb.events, schedule=sched, macro=_macro()
        )
        assert got.holdings == "unknown"
        spy = regime.events_state(
            "SPY", SESSION, static_calendar, pb.events, schedule=sched, macro=_macro()
        )
        assert (spy.earnings, spy.holdings) == ("n/a", "unmapped")

    def test_macro(self, pb, static_calendar) -> None:
        def state(macro: regime.MacroCalendar | None) -> tuple[str, date | None]:
            got = regime.events_state(
                "SPY", SESSION, static_calendar, pb.events, schedule={}, macro=macro
            )
            return got.macro, got.next_macro

        assert state(_macro((date(2026, 6, 5), "nfp"))) == ("within", date(2026, 6, 5))
        assert state(_macro((date(2026, 6, 8), "fomc"))) == ("within", date(2026, 6, 8))
        assert state(_macro((date(2026, 6, 1), "cpi"))) == ("none", None)  # the session's own
        assert state(_macro((date(2026, 6, 9), "fomc"))) == ("none", None)  # past the window
        assert state(_macro((date(2026, 6, 5), "opex"))) == ("none", None)  # not a counted kind
        assert state(None) == ("unknown", None)
        gap = _macro(gaps=frozenset({"cpi 2026"}))
        assert state(gap) == ("unknown", None)
        other = _macro(gaps=frozenset({"cpi 2027"}))
        assert state(other) == ("none", None)
        outside = regime.MacroCalendar(
            events=(), covered=(date(2026, 1, 1), date(2026, 6, 4)), gaps=frozenset()
        )
        assert state(outside) == ("unknown", None)

    def test_calendar_exhaustion_is_an_unknown_window(self, pb, static_calendar) -> None:
        """Codex P2-6: with fewer than 5 sessions left the calendar raises
        NotASessionError (a CalendarError); the window is unknown, no crash."""
        late = static_calendar.sessions()[-3]
        got = regime.events_state(
            "AAPL",
            late,
            static_calendar,
            pb.events,
            schedule={"AAPL": ["2026-10-29", "2027-01-28"]},
            macro=_macro(),
        )
        assert got.window_end is None
        assert (got.earnings, got.holdings, got.macro) == ("unknown", "n/a", "unknown")
        res = regime.conditions_at(
            late,
            static_calendar,
            pb,
            names=["AAPL", "SMH", "SPY"],
            signals_doc=None,
            features={},
            indices={},
            schedule={},
            macro=_macro(),
            news_flags=None,
        )
        assert {n: c.events.macro for n, c in res.names.items()} == dict.fromkeys(
            ("AAPL", "SMH", "SPY"), "unknown"
        )
        assert res.names["SMH"].events.holdings == "unknown"


# -------------------------------------------------------------- direction


def _signals(
    session: date = SESSION, *, fires: bool = True, top3=("AMD", "TQQQ", "INTC"), beats=()
) -> dict:
    return {
        "session": session.isoformat(),
        "xsmom": {"fires": fires, "top3": list(top3)},
        "pead": [{"name": n, "report_date": "2026-05-28", "move": "0.031"} for n in beats],
        "volspike": {"fires": True, "names": ["KO"]},  # not a direction source: ignored
    }


class TestDirection:
    def test_bull_only_from_allowed_signals(self) -> None:
        d = regime.directions(_signals(beats=("KO",)), SESSION)
        assert d.status == "ok"
        assert d.bull == {"AMD": ("xsmom_top3",), "INTC": ("xsmom_top3",), "KO": ("pead_beat",)}
        assert d.no_options_expression == ("TQQQ",)  # logged, never substituted
        assert d.of("AMD") == "bull" and d.of("KO") == "bull" and d.of("NVDA") == "none"
        assert d.of("TQQQ") == "none"

    def test_non_rebalance_day_points_nothing(self) -> None:
        d = regime.directions(_signals(fires=False), SESSION)
        assert d.bull == {} and d.of("AMD") == "none"

    @pytest.mark.parametrize(
        "doc",
        [None, _signals(date(2026, 5, 29)), {"session": SESSION.isoformat(), "xsmom": "x"}],
    )
    def test_missing_or_foreign_signals_are_unknown(self, doc) -> None:
        d = regime.directions(doc, SESSION)
        assert d.status != "ok" and d.of("AMD") == "unknown" and d.of("KO") == "unknown"

    def test_never_bear(self) -> None:
        doc = _signals(beats=())
        doc["pead_evaluated"] = [{"name": "META", "move": "-0.086", "fires": False}]
        d = regime.directions(doc, SESSION)
        assert "bear" not in {d.of(n) for n in ("META", "AMD", "KO")}


# ------------------------------------------------------------ whole regime


def _full(
    pb,
    static_calendar,
    *,
    name: str,
    ratios: list[float],
    current: float,
    signals_doc: Any = "default",
    **kw: Any,
):
    feats = _history(static_calendar, ratios, name=name)
    feats[SESSION] = _doc({name: _entry(0.25 * current, 0.25, **kw)})
    return regime.conditions_at(
        SESSION,
        static_calendar,
        pb,
        names=[name],
        signals_doc=_signals() if signals_doc == "default" else signals_doc,
        features=feats,
        indices={"VIX": {SESSION: 15.0}, "VIX3M": {SESSION: 17.0}},
        schedule={name: ["2026-04-30", "2026-07-30"]},
        macro=_macro(),
        news_flags=None,
    )


RAMP = [0.70 + 0.002 * k for k in range(150)]  # R history 0.700..0.998
FAIR = 0.851  # 76 of 150 below: p = 38/75
RICH = 0.999  # all below: p = 1


def _matches(pb, c, book: bool | None = False) -> dict[str, regime.RowMatch]:
    return {x.row_id: x for x in regime.match_rows(pb, c, book_over_delta_cap=book)}


class TestMatchRows:
    def test_xsmom_name_fair_vol_matches_row_1_only(self, pb, static_calendar) -> None:
        res = _full(pb, static_calendar, name="AMD", ratios=RAMP, current=FAIR)
        c = res.names["AMD"]
        assert (c.direction, c.vol.state, c.news_source) == ("bull", "fair", "absent")
        m = _matches(pb, c)
        assert m["R1"].matched
        assert not m["R2"].matched and any("vol" in r for r in m["R2"].reasons)
        assert not m["R3"].matched  # pead_beat did not fire
        assert not m["R6"].matched  # flat slope history: not steep
        assert not m["R8a"].matched and m["R8a"].reasons == ("dormant",)
        assert [k for k, v in m.items() if v.matched] == ["R1"]

    def test_rich_vol_flips_to_row_2(self, pb, static_calendar) -> None:
        res = _full(pb, static_calendar, name="AMD", ratios=RAMP, current=RICH)
        m = _matches(pb, res.names["AMD"])
        assert m["R2"].matched and not m["R1"].matched

    def test_warm_up_only_the_debit_spread_matches_in_v2(self, pb, pb_v1, static_calendar) -> None:
        """Operator ruling 2026-09-23: in v2 row 1 (the XSMOM call debit
        spread) may match while the vol state is NOT_EVALUABLE; v1 and every
        other vol-conditioned row keep waiting."""
        for book, r1 in ((pb_v1, False), (pb, True)):
            res = _full(book, static_calendar, name="AMD", ratios=[0.8] * 30, current=FAIR)
            c = res.names["AMD"]
            assert c.vol.state == NE and "warm-up" in c.vol.reason
            m = _matches(book, c)
            assert m["R1"].matched is r1
            assert not m["R2"].matched and not m["R6"].matched
            if r1:
                assert any("NOT_EVALUABLE" in n for n in m["R1"].notes)
            else:
                assert any("vol" in r for r in m["R1"].reasons)

    def test_warm_up_condor_still_waits(self, pb, static_calendar) -> None:
        res = _full(
            pb,
            static_calendar,
            name="IWM",
            ratios=[0.8] * 30,
            current=RICH,
            signals_doc=_signals(top3=()),
        )
        m = _matches(pb, res.names["IWM"])
        assert res.names["IWM"].vol.state == NE and not m["R4"].matched

    def test_evaluable_vol_still_gates_row_1_in_v2(self, pb, static_calendar) -> None:
        res = _full(pb, static_calendar, name="AMD", ratios=RAMP, current=RICH)
        m = _matches(pb, res.names["AMD"])
        assert not m["R1"].matched and any("vol: rich" in r for r in m["R1"].reasons)
        assert m["R1"].notes == ()

    def test_condor_needs_validated_fidelity(self, pb, static_calendar) -> None:
        for name, want in (("IWM", True), ("SPY", False)):
            res = _full(
                pb,
                static_calendar,
                name=name,
                ratios=RAMP,
                current=RICH,
                signals_doc=_signals(top3=()),
            )
            c = res.names[name]
            assert (c.direction, c.vol.state, c.term.state, c.market.state) == (
                "none",
                "rich",
                "contango",
                "contango",
            )
            assert (c.events.earnings, c.events.holdings, c.events.macro) == (
                "n/a",
                "unmapped",
                "none",
            )
            m = _matches(pb, c)
            assert m["R4"].matched is want
            if not want:
                assert any("fidelity" in r for r in m["R4"].reasons)

    def test_unknown_direction_blocks_the_neutral_row(self, pb, static_calendar) -> None:
        res = _full(pb, static_calendar, name="IWM", ratios=RAMP, current=RICH, signals_doc=None)
        c = res.names["IWM"]
        assert c.direction == "unknown"
        assert not _matches(pb, c)["R4"].matched

    def test_news_flag_vetoes_but_never_the_hedge(self, pb, static_calendar) -> None:
        feats = _history(static_calendar, RAMP, name="QQQ")
        feats[SESSION] = _doc({"QQQ": _entry(0.25 * FAIR, 0.25)})
        res = regime.conditions_at(
            SESSION,
            static_calendar,
            pb,
            names=["QQQ"],
            signals_doc=_signals(top3=("QQQ", "AMD", "INTC")),
            features=feats,
            indices={"VIX": {SESSION: 15.0}, "VIX3M": {SESSION: 17.0}},
            schedule={},
            macro=_macro(),
            news_flags={"QQQ": ["binary_event_pending"]},
        )
        c = res.names["QQQ"]
        assert c.news_flags == ("binary_event_pending",) and c.news_source == "model"
        m = _matches(pb, c, book=True)
        assert not m["R1"].matched and any("news" in r for r in m["R1"].reasons)
        assert m["R7"].matched
        assert not _matches(pb, c, book=None)["R7"].matched  # book state unknown: fail closed
        assert not _matches(pb, c, book=False)["R7"].matched

    def test_liquidity_and_universe(self, pb, static_calendar) -> None:
        res = _full(pb, static_calendar, name="AMD", ratios=RAMP, current=FAIR, liquidity=9)
        m = _matches(pb, res.names["AMD"], book=True)
        assert not m["R1"].matched and any("liquidity" in r for r in m["R1"].reasons)
        assert not m["R7"].matched and any("universe" in r for r in m["R7"].reasons)

    def test_event_calendar_row(self, pb, static_calendar) -> None:
        feats = _history(static_calendar, RAMP, name="SPY")
        feats[SESSION] = _doc({"SPY": _entry(0.25 * FAIR, 0.25, term=_term_points(0.25, 0.20))})
        res = regime.conditions_at(
            SESSION,
            static_calendar,
            pb,
            names=["SPY"],
            signals_doc=_signals(),
            features=feats,
            indices={"VIX": {SESSION: 15.0}, "VIX3M": {SESSION: 17.0}},
            schedule={},
            macro=_macro((date(2026, 6, 5), "cpi")),
            news_flags=None,
        )
        c = res.names["SPY"]
        assert (c.events.macro, c.events.next_macro) == ("within", date(2026, 6, 5))
        assert c.term.event_inversion == "yes"
        m = _matches(pb, c)
        assert m["R5"].matched
        assert not m["R4"].matched  # an event ahead is not "no event"

    def test_regime_doc_is_json_and_carries_the_seal(self, pb, static_calendar) -> None:
        import json

        res = _full(pb, static_calendar, name="AMD", ratios=RAMP, current=FAIR)
        doc = regime.regime_doc(res, pb, book_over_delta_cap=None)
        assert json.loads(json.dumps(doc)) == doc
        assert doc["schema"] == "desk-regime/1" and doc["playbook_sha256"] == pb.sha256
        amd = doc["names"]["AMD"]
        assert amd["vol"]["state"] == "fair" and amd["vol"]["percentile"] == "38/75"
        assert amd["vol"]["n"] == 150 and amd["vol"]["band"] == "unvalidated"
        assert amd["rows"]["R1"]["matched"] is True
        assert amd["direction"] == {"state": "bull", "sources": ["xsmom_top3"]}
        assert doc["no_options_expression"] == ["TQQQ"]
        assert doc["signals_status"] == "ok"
