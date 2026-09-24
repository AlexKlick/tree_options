"""Desk rails (plan E4/D6): the one pure admission check.

Every expected figure below is computed BY HAND in this file (literal
arithmetic in the comments and the constants), never by calling the
implementation. Boundaries are pinned on both sides (at the limit passes,
one tick over fails), so a ``<=`` mutated to ``<`` (or the reverse) dies.
Fail-closed: each missing input turns its rule NOT_EVALUABLE and the
report not ok.
"""

from __future__ import annotations

import dataclasses
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from tree_options.desk import rails
from tree_options.desk.events import EarningsEvent
from tree_options.desk.rails import (
    FAIL,
    NOT_EVALUABLE,
    PASS,
    BookPosition,
    BookView,
    Candidate,
    CandidateLeg,
    ExDividend,
    NewsVeto,
    PositionRisk,
    RailContext,
    RailLimits,
)

ET = ZoneInfo("America/New_York")
D = Decimal

# The operator's limits (2026-09-23), written out as the sealed playbook's
# [limits] table carries them.
LIMITS_TOML = """
[limits]
max_loss_per_trade_usd = "500"
max_book_loss_usd = "5000"
max_per_underlying = 2
max_net_beta_delta_usd_per_1pct_spy = "250"
max_admissions_per_session = 3
max_roundtrip_cost_frac_of_max_loss = "0.15"
min_leg_open_interest = 100
max_leg_spread_frac_of_mid = "0.10"
min_long_single_abs_delta = "0.30"
max_chain_age_sessions = 1
max_book_short_vega_usd_per_volpt = "100"
"""

LIMIT_KEYS = {
    "max_loss_per_trade_usd",
    "max_book_loss_usd",
    "max_per_underlying",
    "max_net_beta_delta_usd_per_1pct_spy",
    "max_admissions_per_session",
    "max_roundtrip_cost_frac_of_max_loss",
    "min_leg_open_interest",
    "max_leg_spread_frac_of_mid",
    "min_long_single_abs_delta",
    "max_chain_age_sessions",
    "max_book_short_vega_usd_per_volpt",
}

ALL_RULES = (
    "paper_only",
    "defined_risk",
    "max_loss_per_trade",
    "max_book_loss",
    "per_underlying",
    "net_beta_delta",
    "admissions_per_session",
    "roundtrip_cost",
    "leg_open_interest",
    "leg_spread",
    "earnings_short_premium",
    "ex_dividend_short_call",
    "long_single_delta",
    "fresh_chain",
    "book_short_vega",
    "news_veto",
)

SESSION = date(2026, 9, 24)  # Thursday, an NYSE session
PREV_SESSION = date(2026, 9, 23)
EXIT = date(2026, 10, 22)  # a Thursday session, 20 sessions later
EXPIRY = date(2027, 1, 15)
NOW = datetime(2026, 9, 24, 10, 0, tzinfo=ET)


@pytest.fixture()
def limits(tmp_path: Path) -> RailLimits:
    p = tmp_path / "playbook.toml"
    p.write_text(LIMITS_TOML)
    return rails.load_limits(p)


@pytest.fixture()
def ctx(static_calendar) -> RailContext:
    return RailContext(
        now=NOW,
        session=SESSION,
        calendar=static_calendar,
        account_mode="paper",
        admissions_this_session=0,
    )


def _leg(right, action, strike, bid, ask, oi=500, delta="0.50", expiry=EXPIRY) -> CandidateLeg:
    return CandidateLeg(
        right=right,
        action=action,
        strike=D(strike),
        expiry=expiry,
        bid=None if bid is None else D(bid),
        ask=None if ask is None else D(ask),
        open_interest=oi,
        delta=None if delta is None else D(delta),
    )


def _call_debit_spread(**over) -> Candidate:
    """XLF 50/53 call debit spread, 2 lots at a 1.50 cap: max loss
    1.50 x 100 x 2 = $300. Leg spreads 0.10 and 0.05: round trip
    (0.10 + 0.05) x 100 x 2 = $30 plus 2 x 0.65 x 2 contracts x 2 legs =
    $5.20, total $35.20 <= 0.15 x 300 = $45. Delta $ per 1% SPY:
    50 x 52 x 1.1 x 0.01 = $28.60."""
    base: dict[str, object] = {
        "id": "deal-xlf-1",
        "underlying": "XLF",
        "kind": "debit_vertical",
        "legs": (
            _leg("C", "BUY", "50", "3.00", "3.10", oi=500, delta="0.60"),
            _leg("C", "SELL", "53", "1.50", "1.55", oi=400, delta="0.35"),
        ),
        "quantity": 2,
        "max_loss_usd": D("300"),
        "entry_price": D("1.50"),
        "planned_exit": EXIT,
        "risk": PositionRisk(
            delta_shares=D("50"), vega_usd_per_volpt=D("5"), spot=D("52"), beta=D("1.1")
        ),
        "chain_session": PREV_SESSION,
        "chain_as_of": datetime(2026, 9, 24, 6, 40, tzinfo=ET),
        "earnings": (),
        "ex_dividends": (),
        "news_veto": None,
    }
    base.update(over)
    return Candidate(**base)  # type: ignore[arg-type]


def _put_credit_spread(**over) -> Candidate:
    """XLF 48/45 bull put credit spread, 1 lot at a 1.00 credit floor: max
    loss (3 - 1.00) x 100 x 1 = $200."""
    base: dict[str, object] = {
        "kind": "credit_vertical",
        "legs": (
            _leg("P", "SELL", "48", "1.00", "1.05", delta="-0.30"),
            _leg("P", "BUY", "45", "0.40", "0.42", delta="-0.15"),
        ),
        "quantity": 1,
        "max_loss_usd": D("200"),
        "entry_price": D("1.00"),
    }
    base.update(over)
    return _call_debit_spread(**base)


def _long_call(delta: str, **over) -> Candidate:
    """One long XLF 50 call at a 2.00 cap: max loss 2.00 x 100 = $200."""
    base: dict[str, object] = {
        "kind": "long_single",
        "legs": (_leg("C", "BUY", "50", "1.98", "2.02", delta=delta),),
        "quantity": 1,
        "max_loss_usd": D("200"),
        "entry_price": D("2.00"),
    }
    base.update(over)
    return _call_debit_spread(**base)


def _pos(pid, underlying, max_loss, *, delta=None, spot=None, beta=None, vega=None) -> BookPosition:
    return BookPosition(
        id=pid,
        source="test",
        underlying=underlying,
        status="open",
        max_loss_usd=None if max_loss is None else D(max_loss),
        risk=PositionRisk(
            delta_shares=None if delta is None else D(delta),
            vega_usd_per_volpt=None if vega is None else D(vega),
            spot=None if spot is None else D(spot),
            beta=None if beta is None else D(beta),
        ),
    )


def _status(report: rails.RailReport, rule: str) -> str:
    [res] = [r for r in report.results if r.rule == rule]
    return res.status


def _detail(report: rails.RailReport, rule: str) -> str:
    [res] = [r for r in report.results if r.rule == rule]
    return res.detail


# ------------------------------------------------------------------ baseline


class TestBaseline:
    def test_clean_candidate_passes_every_rule(self, ctx, limits) -> None:
        report = rails.check(_call_debit_spread(), BookView(positions=()), ctx, limits)
        assert [r.rule for r in report.results] == list(ALL_RULES)
        assert {r.rule: r.status for r in report.results} == dict.fromkeys(ALL_RULES, PASS)
        assert report.ok is True
        assert report.failed() == ()
        assert report.candidate_id == "deal-xlf-1"

    def test_rules_constant_names_every_rule(self) -> None:
        assert rails.RULES == ALL_RULES

    def test_report_serializes(self, ctx, limits) -> None:
        doc = rails.check(_call_debit_spread(), BookView(positions=()), ctx, limits).to_dict()
        assert doc["ok"] is True
        assert doc["candidate_id"] == "deal-xlf-1"
        assert [r["rule"] for r in doc["results"]] == list(ALL_RULES)

    def test_one_failure_makes_the_report_not_ok(self, ctx, limits) -> None:
        live = dataclasses.replace(ctx, account_mode="live")
        report = rails.check(_call_debit_spread(), BookView(positions=()), live, limits)
        assert report.ok is False
        assert [r.rule for r in report.failed()] == ["paper_only"]

    def test_check_is_pure_same_inputs_same_report(self, ctx, limits) -> None:
        cand = _call_debit_spread()
        book = BookView(
            positions=(_pos("a", "QQQ", "100", delta="1", spot="1", beta="1", vega="0"),)
        )
        assert rails.check(cand, book, ctx, limits) == rails.check(cand, book, ctx, limits)


# ------------------------------------------------------------------ limits


class TestLimitsLoader:
    def test_loads_the_operator_values_exactly(self, limits) -> None:
        assert limits.max_loss_per_trade_usd == D("500")
        assert limits.max_book_loss_usd == D("5000")
        assert limits.max_per_underlying == 2
        assert limits.max_net_beta_delta_usd_per_1pct_spy == D("250")
        assert limits.max_admissions_per_session == 3
        assert limits.max_roundtrip_cost_frac_of_max_loss == D("0.15")
        assert limits.min_leg_open_interest == 100
        assert limits.max_leg_spread_frac_of_mid == D("0.10")
        assert limits.min_long_single_abs_delta == D("0.30")
        assert limits.max_chain_age_sessions == 1
        assert limits.max_book_short_vega_usd_per_volpt == D("100")
        assert {f.name for f in dataclasses.fields(RailLimits)} == LIMIT_KEYS

    def test_other_playbook_tables_are_ignored(self, tmp_path: Path) -> None:
        p = tmp_path / "playbook.toml"
        p.write_text('[meta]\nversion = "v1"\n' + LIMITS_TOML + '\n[[rows]]\nname = "x"\n')
        assert rails.load_limits(p).max_book_loss_usd == D("5000")

    @pytest.mark.parametrize("key", sorted(LIMIT_KEYS))
    def test_a_missing_key_fails_the_load(self, key: str, tmp_path: Path) -> None:
        text = "\n".join(ln for ln in LIMITS_TOML.splitlines() if not ln.startswith(key + " "))
        p = tmp_path / "p.toml"
        p.write_text(text)
        with pytest.raises(rails.RailLimitsError, match=key):
            rails.load_limits(p)

    def test_an_unknown_key_fails_the_load(self, tmp_path: Path) -> None:
        p = tmp_path / "p.toml"
        p.write_text(LIMITS_TOML + 'max_vega_typo = "1"\n')
        with pytest.raises(rails.RailLimitsError, match="max_vega_typo"):
            rails.load_limits(p)

    def test_no_limits_table_fails_the_load(self, tmp_path: Path) -> None:
        p = tmp_path / "p.toml"
        p.write_text('[meta]\nversion = "v1"\n')
        with pytest.raises(rails.RailLimitsError, match=r"\[limits\]"):
            rails.load_limits(p)

    @pytest.mark.parametrize(
        ("line", "bad"),
        [
            ('max_loss_per_trade_usd = "500"', "max_loss_per_trade_usd = 500.0"),  # float money
            ('max_loss_per_trade_usd = "500"', "max_loss_per_trade_usd = 500"),  # int money
            ('max_loss_per_trade_usd = "500"', 'max_loss_per_trade_usd = "abc"'),
            ('max_loss_per_trade_usd = "500"', 'max_loss_per_trade_usd = "NaN"'),
            ('max_loss_per_trade_usd = "500"', 'max_loss_per_trade_usd = "-1"'),
            ("max_per_underlying = 2", 'max_per_underlying = "2"'),
            ("max_per_underlying = 2", "max_per_underlying = true"),
            ("max_per_underlying = 2", "max_per_underlying = 0"),
            ("max_admissions_per_session = 3", "max_admissions_per_session = 3.0"),
            (
                'max_roundtrip_cost_frac_of_max_loss = "0.15"',
                'max_roundtrip_cost_frac_of_max_loss = "1.5"',
            ),
            ('min_long_single_abs_delta = "0.30"', 'min_long_single_abs_delta = "0"'),
            ("max_chain_age_sessions = 1", "max_chain_age_sessions = -1"),
            ("min_leg_open_interest = 100", "min_leg_open_interest = -5"),
        ],
    )
    def test_bad_values_fail_the_load(self, line: str, bad: str, tmp_path: Path) -> None:
        assert line in LIMITS_TOML
        p = tmp_path / "p.toml"
        p.write_text(LIMITS_TOML.replace(line, bad))
        with pytest.raises(rails.RailLimitsError):
            rails.load_limits(p)

    @pytest.mark.parametrize(
        ("line", "edge", "loads"),
        [
            (
                'max_book_short_vega_usd_per_volpt = "100"',
                'max_book_short_vega_usd_per_volpt = "0"',
                True,
            ),
            ("max_chain_age_sessions = 1", "max_chain_age_sessions = 0", True),
            ("min_leg_open_interest = 100", "min_leg_open_interest = 0", True),
            ("max_per_underlying = 2", "max_per_underlying = 1", True),
            ('max_loss_per_trade_usd = "500"', 'max_loss_per_trade_usd = "0"', False),
            (
                'max_roundtrip_cost_frac_of_max_loss = "0.15"',
                'max_roundtrip_cost_frac_of_max_loss = "1"',
                True,
            ),
            (
                'max_roundtrip_cost_frac_of_max_loss = "0.15"',
                'max_roundtrip_cost_frac_of_max_loss = "1.0001"',
                False,
            ),
            ('min_long_single_abs_delta = "0.30"', 'min_long_single_abs_delta = "1"', True),
        ],
    )
    def test_range_edges(self, line: str, edge: str, loads: bool, tmp_path: Path) -> None:
        p = tmp_path / "p.toml"
        p.write_text(LIMITS_TOML.replace(line, edge))
        if loads:
            rails.load_limits(p)
        else:
            with pytest.raises(rails.RailLimitsError):
                rails.load_limits(p)

    def test_limits_from_a_parsed_table(self) -> None:
        import tomllib

        table = tomllib.loads(LIMITS_TOML)["limits"]
        assert rails.limits_from_table(table).max_admissions_per_session == 3


# ------------------------------------------------------------ boundaries


class TestMaxLossPerTrade:
    def test_exactly_500_passes(self, ctx, limits) -> None:
        # 2.50 x 100 x 2 = $500 (width 3 > 2.50)
        c = _call_debit_spread(entry_price=D("2.50"), max_loss_usd=D("500"))
        report = rails.check(c, BookView(positions=()), ctx, limits)
        assert _status(report, "max_loss_per_trade") == PASS
        assert _status(report, "defined_risk") == PASS

    def test_one_cent_per_package_over_fails(self, ctx, limits) -> None:
        # 2.51 x 100 x 2 = $502
        c = _call_debit_spread(entry_price=D("2.51"), max_loss_usd=D("502"))
        report = rails.check(c, BookView(positions=()), ctx, limits)
        assert _status(report, "max_loss_per_trade") == FAIL
        assert _status(report, "defined_risk") == PASS
        assert not report.ok

    def test_credit_kind_max_loss_is_width_minus_credit(self, ctx, limits) -> None:
        # (3 - 1.00) x 100 x 1 = $200 stated and recomputed
        report = rails.check(_put_credit_spread(), BookView(positions=()), ctx, limits)
        assert _status(report, "defined_risk") == PASS
        assert _status(report, "max_loss_per_trade") == PASS

    def test_nonpositive_max_loss_is_not_evaluable(self, ctx, limits) -> None:
        c = _call_debit_spread(max_loss_usd=D("0"))
        report = rails.check(c, BookView(positions=()), ctx, limits)
        assert _status(report, "max_loss_per_trade") == NOT_EVALUABLE
        assert _status(report, "roundtrip_cost") == NOT_EVALUABLE


class TestDefinedRisk:
    def test_stated_max_loss_must_equal_the_recomputed_one(self, ctx, limits) -> None:
        # 1.50 x 100 x 2 = $300, stated $299: the deal lies about its risk
        c = _call_debit_spread(max_loss_usd=D("299"))
        report = rails.check(c, BookView(positions=()), ctx, limits)
        assert _status(report, "defined_risk") == FAIL
        assert "300" in _detail(report, "defined_risk")

    def test_uncovered_short_leg_fails(self, ctx, limits) -> None:
        naked = _call_debit_spread(
            kind="credit_vertical",
            legs=(
                _leg("C", "SELL", "50", "3.00", "3.10"),
                _leg("C", "SELL", "53", "1.50", "1.55"),
            ),
        )
        report = rails.check(naked, BookView(positions=()), ctx, limits)
        assert _status(report, "defined_risk") == FAIL

    def test_kind_shape_mismatch_fails(self, ctx, limits) -> None:
        # a "long_single" carrying two legs is not what it claims
        c = _call_debit_spread(kind="long_single")
        report = rails.check(c, BookView(positions=()), ctx, limits)
        assert _status(report, "defined_risk") == FAIL

    def test_planned_exit_at_or_after_first_expiry_fails(self, ctx, limits) -> None:
        c = _call_debit_spread(planned_exit=EXPIRY)
        report = rails.check(c, BookView(positions=()), ctx, limits)
        assert _status(report, "defined_risk") == FAIL


class TestBookMaxLoss:
    def test_book_plus_candidate_exactly_5000_passes(self, ctx, limits) -> None:
        # 4000 + 700 + 300 = 5000
        book = BookView(positions=(_pos("a", "QQQ", "4000"), _pos("b", "IWM", "700")))
        report = rails.check(_call_debit_spread(), book, ctx, limits)
        assert _status(report, "max_book_loss") == PASS

    def test_one_cent_over_5000_fails(self, ctx, limits) -> None:
        # 4000 + 700.01 + 300 = 5000.01
        book = BookView(positions=(_pos("a", "QQQ", "4000"), _pos("b", "IWM", "700.01")))
        report = rails.check(_call_debit_spread(), book, ctx, limits)
        assert _status(report, "max_book_loss") == FAIL

    def test_working_entries_count_at_the_cap_the_book_states(self, ctx, limits) -> None:
        working = BookPosition(
            id="w", source="t", underlying="QQQ", status="working", max_loss_usd=D("4701")
        )
        report = rails.check(_call_debit_spread(), BookView(positions=(working,)), ctx, limits)
        assert _status(report, "max_book_loss") == FAIL  # 4701 + 300 = 5001

    def test_a_position_without_max_loss_fails_closed(self, ctx, limits) -> None:
        book = BookView(positions=(_pos("a", "QQQ", None),))
        report = rails.check(_call_debit_spread(), book, ctx, limits)
        assert _status(report, "max_book_loss") == NOT_EVALUABLE


class TestPerUnderlying:
    def test_one_held_plus_this_is_two_passes(self, ctx, limits) -> None:
        book = BookView(positions=(_pos("a", "XLF", "100"), _pos("b", "QQQ", "100")))
        report = rails.check(_call_debit_spread(), book, ctx, limits)
        assert _status(report, "per_underlying") == PASS

    def test_two_held_blocks_a_third(self, ctx, limits) -> None:
        book = BookView(positions=(_pos("a", "XLF", "100"), _pos("b", "XLF", "100")))
        report = rails.check(_call_debit_spread(), book, ctx, limits)
        assert _status(report, "per_underlying") == FAIL
        assert "a" in _detail(report, "per_underlying")
        assert "b" in _detail(report, "per_underlying")

    def test_symbol_match_ignores_case_and_whitespace(self, ctx, limits) -> None:
        book = BookView(positions=(_pos("a", "xlf", "100"), _pos("b", " XLF", "100")))
        report = rails.check(_call_debit_spread(), book, ctx, limits)
        assert _status(report, "per_underlying") == FAIL


class TestNetBetaDelta:
    """$ P&L per 1% SPY = delta_shares x spot x beta x 0.01."""

    def _cand(self, delta: str) -> Candidate:
        return _call_debit_spread(
            risk=PositionRisk(
                delta_shares=D(delta), vega_usd_per_volpt=D("0"), spot=D("100"), beta=D("1")
            )
        )

    def _book(self, dollars: str) -> BookView:
        # delta_shares=dollars, spot 100, beta 1: dollars x 100 x 1 x 0.01 = dollars
        return BookView(
            positions=(_pos("h", "QQQ", "100", delta=dollars, spot="100", beta="1", vega="0"),)
        )

    @pytest.mark.parametrize(
        ("book", "cand", "want"),
        [
            ("0", "250", PASS),  # 250 x 100 x 1 x 0.01 = +250: at the cap
            ("0", "250.01", FAIL),  # +250.01
            ("0", "-250", PASS),  # -250: at the cap
            ("0", "-250.01", FAIL),
            ("100", "150", PASS),  # 100 + 150 = 250
            ("100", "150.01", FAIL),
            ("300", "-100", PASS),  # 300 -> 200: back inside the cap
            ("400", "-100", PASS),  # 400 -> 300: over the cap but |delta| shrinks
            ("-400", "100", PASS),  # -400 -> -300: shrinks
            ("300", "0.01", FAIL),  # 300 -> 300.01: grows while over
            ("300", "0", FAIL),  # unchanged is not a reduction
            ("300", "-700", FAIL),  # 300 -> -400: flips to a bigger |delta|
            ("300", "-550", PASS),  # 300 -> -250: inside the cap
        ],
    )
    def test_cap_and_reduction(self, book, cand, want, ctx, limits) -> None:
        report = rails.check(self._cand(cand), self._book(book), ctx, limits)
        assert _status(report, "net_beta_delta") == want

    def test_beta_and_spot_scale_the_dollars(self, ctx, limits) -> None:
        # 100 shares x 200 spot x 1.25 beta x 0.01 = $250 (at the cap)
        c = _call_debit_spread(
            risk=PositionRisk(
                delta_shares=D("100"), vega_usd_per_volpt=D("0"), spot=D("200"), beta=D("1.25")
            )
        )
        assert (
            _status(rails.check(c, BookView(positions=()), ctx, limits), "net_beta_delta") == PASS
        )
        c2 = dataclasses.replace(c, risk=dataclasses.replace(c.risk, beta=D("1.2501")))  # $250.02
        assert (
            _status(rails.check(c2, BookView(positions=()), ctx, limits), "net_beta_delta") == FAIL
        )

    def test_a_book_position_without_greeks_fails_closed(self, ctx, limits) -> None:
        book = BookView(positions=(_pos("h", "QQQ", "100", delta="1", spot="100"),))  # no beta
        report = rails.check(self._cand("1"), book, ctx, limits)
        assert _status(report, "net_beta_delta") == NOT_EVALUABLE
        assert "h" in _detail(report, "net_beta_delta")


class TestAdmissions:
    @pytest.mark.parametrize(("prior", "want"), [(0, PASS), (2, PASS), (3, FAIL), (7, FAIL)])
    def test_at_most_three_per_session(self, prior, want, ctx, limits) -> None:
        c = dataclasses.replace(ctx, admissions_this_session=prior)
        report = rails.check(_call_debit_spread(), BookView(positions=()), c, limits)
        assert _status(report, "admissions_per_session") == want

    def test_negative_count_is_not_evaluable(self, ctx, limits) -> None:
        c = dataclasses.replace(ctx, admissions_this_session=-1)
        report = rails.check(_call_debit_spread(), BookView(positions=()), c, limits)
        assert _status(report, "admissions_per_session") == NOT_EVALUABLE


class TestRoundTripCost:
    """Round trip = sum over legs of (ask - bid) x 100 x qty (the entry and
    the exit half-spread) + 2 x $0.65 x qty x legs."""

    def _with_second_leg_ask(self, ask: str) -> Candidate:
        return _call_debit_spread(
            legs=(
                _leg("C", "BUY", "50", "3.00", "3.10", delta="0.60"),
                _leg("C", "SELL", "53", "1.50", ask, delta="0.35"),
            )
        )

    def test_exactly_15pct_of_max_loss_passes(self, ctx, limits) -> None:
        # spreads 0.10 + 0.099 = 0.199 -> 0.199 x 100 x 2 = 39.80;
        # commissions 2 x 0.65 x 2 x 2 = 5.20; total 45.00 = 0.15 x 300
        report = rails.check(
            self._with_second_leg_ask("1.599"), BookView(positions=()), ctx, limits
        )
        assert _status(report, "roundtrip_cost") == PASS

    def test_two_cents_over_fails(self, ctx, limits) -> None:
        # 0.10 + 0.0991 = 0.1991 -> 39.82 + 5.20 = 45.02 > 45
        report = rails.check(
            self._with_second_leg_ask("1.5991"), BookView(positions=()), ctx, limits
        )
        assert _status(report, "roundtrip_cost") == FAIL
        assert "45.02" in _detail(report, "roundtrip_cost")

    def test_commissions_alone_can_fail_a_tiny_trade(self, ctx, limits) -> None:
        # long single, 1 lot, cap 0.04 -> max loss $4, 15% = $0.60; the two
        # commissions alone are 2 x 0.65 = $1.30
        c = _long_call(
            "0.35",
            legs=(_leg("C", "BUY", "60", "0.04", "0.04", delta="0.35"),),
            entry_price=D("0.04"),
            max_loss_usd=D("4"),
        )
        report = rails.check(c, BookView(positions=()), ctx, limits)
        assert _status(report, "roundtrip_cost") == FAIL


class TestLegLiquidity:
    @pytest.mark.parametrize(("oi", "want"), [(100, PASS), (99, FAIL), (5000, PASS), (0, FAIL)])
    def test_open_interest_floor(self, oi, want, ctx, limits) -> None:
        c = _call_debit_spread(
            legs=(
                _leg("C", "BUY", "50", "3.00", "3.10", oi=500, delta="0.60"),
                _leg("C", "SELL", "53", "1.50", "1.55", oi=oi, delta="0.35"),
            )
        )
        report = rails.check(c, BookView(positions=()), ctx, limits)
        assert _status(report, "leg_open_interest") == want

    @pytest.mark.parametrize(
        ("bid", "ask", "want"),
        [
            ("0.95", "1.05", PASS),  # spread 0.10 / mid 1.00 = 0.10: at the limit
            ("0.95", "1.0501", FAIL),  # 0.1001 / 1.00005 = 0.10009...
            ("1.90", "2.10", PASS),  # 0.20 / 2.00 = 0.10
            ("1.90", "2.11", FAIL),  # 0.21 / 2.005 = 0.1047...
            ("0.00", "0.05", FAIL),  # 0.05 / 0.025 = 2.0
        ],
    )
    def test_spread_fraction_of_mid(self, bid, ask, want, ctx, limits) -> None:
        c = _call_debit_spread(
            legs=(
                _leg("C", "BUY", "50", "3.00", "3.10", delta="0.60"),
                _leg("C", "SELL", "53", bid, ask, delta="0.35"),
            )
        )
        report = rails.check(c, BookView(positions=()), ctx, limits)
        assert _status(report, "leg_spread") == want

    @pytest.mark.parametrize(("bid", "ask"), [("1.10", "1.00"), ("0", "0"), ("-0.05", "0.05")])
    def test_crossed_or_empty_quote_is_not_evaluable(self, bid, ask, ctx, limits) -> None:
        c = _call_debit_spread(
            legs=(
                _leg("C", "BUY", "50", "3.00", "3.10", delta="0.60"),
                _leg("C", "SELL", "53", bid, ask, delta="0.35"),
            )
        )
        report = rails.check(c, BookView(positions=()), ctx, limits)
        assert _status(report, "leg_spread") == NOT_EVALUABLE
        if bid == ask == "0":
            assert "no market" in _detail(report, "leg_spread")


class TestLongSingleDelta:
    @pytest.mark.parametrize(
        ("delta", "want"),
        [("0.30", PASS), ("0.2999", FAIL), ("0.55", PASS), ("-0.30", PASS), ("-0.2999", FAIL)],
    )
    def test_abs_delta_floor(self, delta, want, ctx, limits) -> None:
        right = "P" if delta.startswith("-") else "C"
        c = _long_call(delta, legs=(_leg(right, "BUY", "50", "1.98", "2.02", delta=delta),))
        report = rails.check(c, BookView(positions=()), ctx, limits)
        assert _status(report, "long_single_delta") == want

    def test_spreads_are_not_held_to_it(self, ctx, limits) -> None:
        c = _call_debit_spread(
            legs=(
                _leg("C", "BUY", "50", "3.00", "3.10", delta="0.10"),
                _leg("C", "SELL", "53", "1.50", "1.55", delta="0.05"),
            )
        )
        report = rails.check(c, BookView(positions=()), ctx, limits)
        assert _status(report, "long_single_delta") == PASS


class TestFreshChain:
    """Age = NYSE sessions from the chain's session to the entry session;
    at most 1: the chain must be the latest completed session."""

    @pytest.mark.parametrize(
        ("chain", "want"),
        [
            (date(2026, 9, 23), PASS),  # the session before the entry session
            (date(2026, 9, 22), FAIL),  # two sessions old
            (date(2026, 9, 18), FAIL),
        ],
    )
    def test_age_in_sessions(self, chain, want, ctx, limits) -> None:
        c = _call_debit_spread(chain_session=chain)
        report = rails.check(c, BookView(positions=()), ctx, limits)
        assert _status(report, "fresh_chain") == want

    def test_monday_entry_on_friday_chain_is_one_session(self, static_calendar, limits) -> None:
        monday = date(2026, 9, 28)
        c_ctx = RailContext(
            now=datetime(2026, 9, 28, 10, 0, tzinfo=ET),
            session=monday,
            calendar=static_calendar,
            account_mode="paper",
            admissions_this_session=0,
        )
        fri = _call_debit_spread(
            chain_session=date(2026, 9, 25), chain_as_of=datetime(2026, 9, 26, 6, 40, tzinfo=ET)
        )
        thu = _call_debit_spread(
            chain_session=date(2026, 9, 24), chain_as_of=datetime(2026, 9, 25, 6, 40, tzinfo=ET)
        )
        assert (
            _status(rails.check(fri, BookView(positions=()), c_ctx, limits), "fresh_chain") == PASS
        )
        assert (
            _status(rails.check(thu, BookView(positions=()), c_ctx, limits), "fresh_chain") == FAIL
        )

    def test_holiday_is_not_a_session(self, static_calendar, limits) -> None:
        # 2026-11-26 is Thanksgiving: Friday 11-27's previous session is 11-25
        c_ctx = RailContext(
            now=datetime(2026, 11, 27, 10, 0, tzinfo=ET),
            session=date(2026, 11, 27),
            calendar=static_calendar,
            account_mode="paper",
            admissions_this_session=0,
        )
        c = _call_debit_spread(
            chain_session=date(2026, 11, 25),
            chain_as_of=datetime(2026, 11, 26, 6, 40, tzinfo=ET),
            planned_exit=date(2026, 12, 18),
        )
        assert _status(rails.check(c, BookView(positions=()), c_ctx, limits), "fresh_chain") == PASS

    def test_edges_of_the_as_of_stamp_pass(self, ctx, limits) -> None:
        # stamped exactly now, and stamped on the chain session's own date
        for stamp in (NOW, datetime(2026, 9, 23, 17, 50, tzinfo=ET)):
            c = _call_debit_spread(chain_as_of=stamp)
            report = rails.check(c, BookView(positions=()), ctx, limits)
            assert _status(report, "fresh_chain") == PASS, stamp

    def test_chain_of_the_entry_session_itself_is_age_zero(self, static_calendar, limits) -> None:
        # an evening check whose entry session is today: chain today, age 0
        evening = RailContext(
            now=datetime(2026, 9, 24, 19, 0, tzinfo=ET),
            session=SESSION,
            calendar=static_calendar,
            account_mode="paper",
            admissions_this_session=0,
        )
        c = _call_debit_spread(
            chain_session=SESSION, chain_as_of=datetime(2026, 9, 24, 17, 50, tzinfo=ET)
        )
        report = rails.check(c, BookView(positions=()), evening, limits)
        assert _status(report, "fresh_chain") == PASS
        assert "0 session(s)" in _detail(report, "fresh_chain")

    def test_chain_from_the_future_is_not_evaluable(self, ctx, limits) -> None:
        c = _call_debit_spread(chain_session=date(2026, 9, 25))
        report = rails.check(c, BookView(positions=()), ctx, limits)
        assert _status(report, "fresh_chain") == NOT_EVALUABLE

    def test_snapshot_stamped_after_now_is_not_evaluable(self, ctx, limits) -> None:
        c = _call_debit_spread(chain_as_of=datetime(2026, 9, 24, 10, 1, tzinfo=ET))
        report = rails.check(c, BookView(positions=()), ctx, limits)
        assert _status(report, "fresh_chain") == NOT_EVALUABLE

    def test_snapshot_stamped_before_its_session_is_not_evaluable(self, ctx, limits) -> None:
        c = _call_debit_spread(chain_as_of=datetime(2026, 9, 22, 20, 0, tzinfo=ET))
        report = rails.check(c, BookView(positions=()), ctx, limits)
        assert _status(report, "fresh_chain") == NOT_EVALUABLE

    def test_naive_as_of_is_not_evaluable(self, ctx, limits) -> None:
        c = _call_debit_spread(chain_as_of=datetime(2026, 9, 24, 6, 40))
        report = rails.check(c, BookView(positions=()), ctx, limits)
        assert _status(report, "fresh_chain") == NOT_EVALUABLE


class TestBookShortVega:
    def _cand(self, vega: str) -> Candidate:
        return _call_debit_spread(
            risk=PositionRisk(
                delta_shares=D("0"), vega_usd_per_volpt=D(vega), spot=D("52"), beta=D("1")
            )
        )

    def _book(self, vega: str) -> BookView:
        return BookView(
            positions=(_pos("h", "QQQ", "100", delta="0", spot="1", beta="1", vega=vega),)
        )

    @pytest.mark.parametrize(
        ("book", "cand", "want"),
        [
            ("-60", "-40", PASS),  # net -100: at the cap
            ("-60", "-40.01", FAIL),  # net -100.01
            ("0", "-100", PASS),
            ("0", "-100.01", FAIL),
            ("50", "-150", PASS),  # long vega elsewhere nets: -100
            ("-150", "10", PASS),  # over the cap, but the trade shrinks the short
            ("-150", "0", FAIL),  # unchanged is not a reduction
            ("-150", "-1", FAIL),
            ("-1000", "2000", PASS),  # long vega: net +1000
        ],
    )
    def test_net_short_vega_cap(self, book, cand, want, ctx, limits) -> None:
        report = rails.check(self._cand(cand), self._book(book), ctx, limits)
        assert _status(report, "book_short_vega") == want


class TestEarnings:
    def _ev(self, d: date, status: str = "confirmed") -> EarningsEvent:
        return EarningsEvent(
            date=d, timing="amc", status=status, source="t", blocker_only=status == "estimated"
        )

    @pytest.mark.parametrize(
        ("when", "status", "want"),
        [
            (SESSION, "confirmed", FAIL),  # an AMC report on the entry session is held
            (EXIT, "confirmed", FAIL),  # a report on the exit session: inclusive
            (date(2026, 10, 8), "estimated", FAIL),  # estimated dates block too
            (date(2026, 10, 8), "sealed", FAIL),
            (date(2026, 10, 23), "confirmed", PASS),  # after the planned exit
            (date(2026, 9, 23), "confirmed", PASS),  # before the entry session
        ],
    )
    def test_short_premium_across_a_report(self, when, status, want, ctx, limits) -> None:
        c = _put_credit_spread(underlying="JPM", earnings=(self._ev(when, status),))
        report = rails.check(c, BookView(positions=()), ctx, limits)
        assert _status(report, "earnings_short_premium") == want

    def test_iron_condor_is_short_premium(self, ctx, limits) -> None:
        # (5 - 1.20) x 100 = $380 per condor
        condor = _call_debit_spread(
            kind="iron_condor",
            legs=(
                _leg("P", "BUY", "40", "0.20", "0.21", delta="-0.08"),
                _leg("P", "SELL", "45", "0.70", "0.72", delta="-0.18"),
                _leg("C", "SELL", "55", "0.70", "0.72", delta="0.18"),
                _leg("C", "BUY", "60", "0.20", "0.21", delta="0.08"),
            ),
            quantity=1,
            entry_price=D("1.20"),
            max_loss_usd=D("380"),
            earnings=(self._ev(date(2026, 10, 1)),),
        )
        report = rails.check(condor, BookView(positions=()), ctx, limits)
        assert _status(report, "defined_risk") == PASS
        assert _status(report, "earnings_short_premium") == FAIL

    def test_debit_structures_may_hold_across_a_report(self, ctx, limits) -> None:
        c = _call_debit_spread(earnings=(self._ev(date(2026, 10, 8)),))
        report = rails.check(c, BookView(positions=()), ctx, limits)
        assert _status(report, "earnings_short_premium") == PASS

    def test_missing_earnings_source_fails_closed_for_short_premium(self, ctx, limits) -> None:
        report = rails.check(_put_credit_spread(earnings=None), BookView(positions=()), ctx, limits)
        assert _status(report, "earnings_short_premium") == NOT_EVALUABLE

    def test_missing_earnings_source_passes_a_debit_structure(self, ctx, limits) -> None:
        report = rails.check(_call_debit_spread(earnings=None), BookView(positions=()), ctx, limits)
        assert _status(report, "earnings_short_premium") == PASS


class TestExDividend:
    def _x(self, d: date, status: str = "declared") -> ExDividend:
        return ExDividend(ex_date=d, status=status)

    @pytest.mark.parametrize(
        ("when", "status", "want"),
        [
            (SESSION, "declared", FAIL),
            (EXIT, "declared", FAIL),  # inclusive
            (date(2026, 10, 5), "projected", FAIL),
            (date(2026, 10, 23), "declared", PASS),
            (date(2026, 9, 23), "declared", PASS),
        ],
    )
    def test_short_call_across_an_ex_date(self, when, status, want, ctx, limits) -> None:
        c = _call_debit_spread(ex_dividends=(self._x(when, status),))
        report = rails.check(c, BookView(positions=()), ctx, limits)
        assert _status(report, "ex_dividend_short_call") == want

    def test_no_short_call_passes_across_an_ex_date(self, ctx, limits) -> None:
        c = _put_credit_spread(ex_dividends=(self._x(date(2026, 10, 5)),))
        report = rails.check(c, BookView(positions=()), ctx, limits)
        assert _status(report, "ex_dividend_short_call") == PASS

    def test_unavailable_dividends_fail_closed_with_a_short_call(self, ctx, limits) -> None:
        report = rails.check(
            _call_debit_spread(ex_dividends=None), BookView(positions=()), ctx, limits
        )
        assert _status(report, "ex_dividend_short_call") == NOT_EVALUABLE

    def test_unavailable_dividends_pass_without_a_short_call(self, ctx, limits) -> None:
        c = _put_credit_spread(ex_dividends=None)
        report = rails.check(c, BookView(positions=()), ctx, limits)
        assert _status(report, "ex_dividend_short_call") == PASS


class TestNewsVeto:
    def test_no_source_configured_passes_and_says_so(self, ctx, limits) -> None:
        report = rails.check(
            _call_debit_spread(news_veto=None), BookView(positions=()), ctx, limits
        )
        assert _status(report, "news_veto") == PASS
        assert "no news veto source" in _detail(report, "news_veto")

    def test_configured_but_unavailable_is_not_evaluable(self, ctx, limits) -> None:
        v = NewsVeto(source="news.v1", available=False, veto=False)
        report = rails.check(_call_debit_spread(news_veto=v), BookView(positions=()), ctx, limits)
        assert _status(report, "news_veto") == NOT_EVALUABLE

    def test_veto_fails(self, ctx, limits) -> None:
        v = NewsVeto(source="news.v1", available=True, veto=True, reason="binary event pending")
        report = rails.check(_call_debit_spread(news_veto=v), BookView(positions=()), ctx, limits)
        assert _status(report, "news_veto") == FAIL
        assert "binary event pending" in _detail(report, "news_veto")

    def test_available_without_veto_passes(self, ctx, limits) -> None:
        v = NewsVeto(source="news.v1", available=True, veto=False)
        report = rails.check(_call_debit_spread(news_veto=v), BookView(positions=()), ctx, limits)
        assert _status(report, "news_veto") == PASS


class TestPaperOnly:
    @pytest.mark.parametrize(
        ("mode", "want"),
        [("paper", PASS), ("live", FAIL), (None, NOT_EVALUABLE), ("", NOT_EVALUABLE)],
    )
    def test_account_mode(self, mode, want, ctx, limits) -> None:
        c = dataclasses.replace(ctx, account_mode=mode)
        report = rails.check(_call_debit_spread(), BookView(positions=()), c, limits)
        assert _status(report, "paper_only") == want


# ------------------------------------------------------------ fail closed


def _leg0(c: Candidate, **over) -> Candidate:
    legs = list(c.legs)
    legs[0] = dataclasses.replace(legs[0], **over)
    return dataclasses.replace(c, legs=tuple(legs))


class TestFailClosed:
    """Each missing input: its rule(s) NOT_EVALUABLE, the report not ok."""

    @pytest.mark.parametrize(
        ("mutate", "rules"),
        [
            (
                lambda c: dataclasses.replace(c, max_loss_usd=None),
                {"defined_risk", "max_loss_per_trade", "max_book_loss", "roundtrip_cost"},
            ),
            (lambda c: dataclasses.replace(c, entry_price=None), {"defined_risk"}),
            # the base spread sells a call: no exit, no ex-dividend window
            (
                lambda c: dataclasses.replace(c, planned_exit=None),
                {"defined_risk", "ex_dividend_short_call"},
            ),
            (
                lambda c: dataclasses.replace(
                    c, risk=dataclasses.replace(c.risk, delta_shares=None)
                ),
                {"net_beta_delta"},
            ),
            (
                lambda c: dataclasses.replace(c, risk=dataclasses.replace(c.risk, spot=None)),
                {"net_beta_delta"},
            ),
            (
                lambda c: dataclasses.replace(c, risk=dataclasses.replace(c.risk, beta=None)),
                {"net_beta_delta"},
            ),
            (
                lambda c: dataclasses.replace(
                    c, risk=dataclasses.replace(c.risk, vega_usd_per_volpt=None)
                ),
                {"book_short_vega"},
            ),
            (lambda c: dataclasses.replace(c, chain_session=None), {"fresh_chain"}),
            (lambda c: dataclasses.replace(c, chain_as_of=None), {"fresh_chain"}),
            (lambda c: _leg0(c, bid=None), {"roundtrip_cost", "leg_spread"}),
            (lambda c: _leg0(c, ask=None), {"roundtrip_cost", "leg_spread"}),
            (lambda c: _leg0(c, open_interest=None), {"leg_open_interest"}),
            (lambda c: dataclasses.replace(c, ex_dividends=None), {"ex_dividend_short_call"}),
        ],
    )
    def test_missing_candidate_input(self, mutate, rules, ctx, limits) -> None:
        report = rails.check(mutate(_call_debit_spread()), BookView(positions=()), ctx, limits)
        got = {r.rule for r in report.results if r.status != PASS}
        assert got == rules
        assert all(r.status == NOT_EVALUABLE for r in report.results if r.rule in rules)
        assert report.ok is False

    def test_missing_long_single_delta(self, ctx, limits) -> None:
        c = _long_call("0.40", legs=(_leg("C", "BUY", "50", "1.98", "2.02", delta=None),))
        report = rails.check(c, BookView(positions=()), ctx, limits)
        assert _status(report, "long_single_delta") == NOT_EVALUABLE
        assert not report.ok

    def test_missing_admissions_count(self, ctx, limits) -> None:
        c = dataclasses.replace(ctx, admissions_this_session=None)
        report = rails.check(_call_debit_spread(), BookView(positions=()), c, limits)
        assert _status(report, "admissions_per_session") == NOT_EVALUABLE
        assert not report.ok

    @pytest.mark.parametrize(
        "book", [None, BookView(positions=(), problems=("plans dir unreadable",))]
    )
    def test_missing_or_unreadable_book(self, book, ctx, limits) -> None:
        report = rails.check(_call_debit_spread(), book, ctx, limits)
        for rule in ("max_book_loss", "per_underlying", "net_beta_delta", "book_short_vega"):
            assert _status(report, rule) == NOT_EVALUABLE, rule
        assert not report.ok

    def test_book_position_without_vega(self, ctx, limits) -> None:
        book = BookView(positions=(_pos("h", "QQQ", "100", delta="0", spot="1", beta="1"),))
        report = rails.check(_call_debit_spread(), book, ctx, limits)
        assert _status(report, "book_short_vega") == NOT_EVALUABLE

    def test_incoherent_context_fails_closed(self, ctx, limits) -> None:
        # entry session already past (a stale context reused the next day)
        stale = dataclasses.replace(ctx, now=datetime(2026, 9, 25, 10, 0, tzinfo=ET))
        report = rails.check(_call_debit_spread(), BookView(positions=()), stale, limits)
        assert _status(report, "fresh_chain") == NOT_EVALUABLE
        assert not report.ok

    def test_entry_session_that_is_no_session_fails_closed(self, ctx, limits) -> None:
        sat = dataclasses.replace(
            ctx, session=date(2026, 9, 26), now=datetime(2026, 9, 26, 10, 0, tzinfo=ET)
        )
        report = rails.check(_call_debit_spread(), BookView(positions=()), sat, limits)
        assert _status(report, "fresh_chain") == NOT_EVALUABLE
        assert not report.ok

    def test_a_float_where_money_belongs_fails_closed_not_crashes(self, ctx, limits) -> None:
        c = _call_debit_spread(max_loss_usd=300.0)
        report = rails.check(c, BookView(positions=()), ctx, limits)
        assert report.ok is False
        assert _status(report, "max_loss_per_trade") == NOT_EVALUABLE
