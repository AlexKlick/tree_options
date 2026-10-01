"""The rail arithmetic behind ``committed_at_caps`` (audit Gate 3, 2026-09-30).

The readiness audit claimed the plan rail understates the live NVDA book's
max loss 31.8x: "$880 committed against a $5,000 cap while the true
width-based max loss is $28,000". These tests pin the CORRECT rule with
hand-derived numbers and show the audit's 31.8x is a category error:

- A LONG DEBIT vertical's max loss is the debit paid (the cap); its width
  is its max WIN (max value), reachable only as a gain.
- Width-based max loss belongs to CREDIT verticals and condors only:
  (width or wider wing) x 100 - credit floor.

Every expected number below is derived by hand in the comment beside it.
The payoff tables are independent oracles (intrinsic-value arithmetic
written out as literals), not calls into the code under test.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tree_options.trex.plan import (
    LIVE_HARD_RAIL,
    LegStructure,
    PutSpread,
    RailReading,
    TradePlan,
    load_plan,
    rail_reading,
)

REPO = Path(__file__).resolve().parents[2]
ENTRY = date(2026, 9, 22)
DEADLINE = date(2026, 10, 9)
FRONT = date(2026, 10, 16)
BACK = date(2026, 11, 20)


def _leg(right: str, action: str, strike: str, expiry: date = FRONT) -> dict[str, Any]:
    return {"right": right, "action": action, "strike": strike, "expiry": expiry}


def _exits() -> dict[str, Any]:
    return {"touch": False, "breach": False}


def _leg_structure(kind: str, legs: list[dict[str, Any]], limit: str, **ov: Any) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "id": "s1",
        "underlying": "SPY",
        "kind": kind,
        "legs": legs,
        "quantity": 1,
        "entry_date": ENTRY,
        "exit_deadline": DEADLINE,
        "limit": limit,
        "exits": _exits(),
    }
    raw.update(ov)
    return raw


def _nvda_oct(**ov: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "nvda-oct",
        "underlying": "NVDA",
        "entry_date": "2026-09-22",
        "expiry": "2026-10-16",
        "long_strike": "185",
        "short_strike": "150",
        "quantity": 5,
        "limit_cap": "0.50",
        "exit_deadline": "2026-10-09",
    }
    base.update(ov)
    return base


def _nvda_nov() -> dict[str, Any]:
    return _nvda_oct(
        id="nvda-nov",
        expiry="2026-11-20",
        quantity=3,
        limit_cap="2.10",
        exit_deadline="2026-11-06",
    )


def _plan(structures: list[Any], **ov: Any) -> TradePlan:
    kwargs: dict[str, Any] = {
        "id": "p",
        "account_mode": "paper",
        "structures": structures,
        "total_debit_cap": "10000",
        "entry_window_start": "09:45",
        "entry_window_end": "12:00",
    }
    kwargs.update(ov)
    return TradePlan(**kwargs)


class TestDebitVerticalMaxLossIsTheDebitPaid:
    """Long put vertical 185/150: BUY the 185 put, SELL the 150 put."""

    def test_caps_and_widths_of_the_live_nvda_structures(self) -> None:
        oct_ = PutSpread(**_nvda_oct())
        nov = PutSpread(**_nvda_nov())
        # width 185 - 150 = 35 points both months
        assert oct_.width == Decimal("35") and nov.width == Decimal("35")
        # max loss at caps = limit_cap x 100 x qty:
        #   oct: 0.50 x 100 x 5  = 250
        #   nov: 2.10 x 100 x 3  = 630
        assert oct_.max_debit == Decimal("250.00")
        assert nov.max_debit == Decimal("630.0")
        # max value (full width) = width x 100 x qty:
        #   oct: 35 x 100 x 5 = 17,500   nov: 35 x 100 x 3 = 10,500
        assert oct_.max_value == Decimal("17500")
        assert nov.max_value == Decimal("10500")

    @pytest.mark.parametrize(
        ("spot", "pkg_value", "book_pnl"),
        [
            # package value at expiry = max(0, 185 - spot) - max(0, 150 - spot)
            # book P&L = (pkg_value - 0.50) x 100 x 5
            ("250", "0", "-250"),  # both legs worthless: the whole debit, no more
            ("185", "0", "-250"),  # long put exactly at the money: worthless
            ("170", "15", "7250"),  # (15 - 0.50) x 500
            ("150", "35", "17250"),  # full width (35 - 0.50) x 500: the best case
            ("0", "35", "17250"),  # pin the floor: deep ITM cannot beat the width
        ],
    )
    def test_expiry_payoff_bounded_by_the_debit(self, spot: str, pkg_value: str,
                                                book_pnl: str) -> None:
        s = Decimal(spot)
        long_k, short_k = Decimal("185"), Decimal("150")
        # intrinsic of a long put vertical, by hand:
        value = max(Decimal(0), long_k - s) - max(Decimal(0), short_k - s)
        assert value == Decimal(pkg_value)
        oct_ = PutSpread(**_nvda_oct())
        # hand arithmetic; max_debit/max_value appear only as bounds to satisfy
        pnl = (value - Decimal("0.50")) * 100 * 5
        assert pnl == Decimal(book_pnl)
        # the loss can never exceed the debit paid, in any state of the world
        assert -oct_.max_debit <= pnl
        # and the width is the WIN ceiling: pnl tops out at max_value - debit
        assert pnl <= oct_.max_value - oct_.max_debit

    def test_leg_view_agrees_debit_cap_is_the_loss(self) -> None:
        spec = PutSpread(**_nvda_oct()).as_spec()
        # a debit_vertical's max loss is its cap x 100 x qty: 0.50 x 500 = 250
        assert spec.max_loss() == Decimal("250") == PutSpread(**_nvda_oct()).max_debit

    def test_long_single_cap_is_the_loss(self) -> None:
        # long_single: 3.20 x 100 x 2 = 640
        spec = LegStructure(
            **_leg_structure("long_single", [_leg("C", "BUY", "100", BACK)], "3.20", quantity=2)
        )
        assert spec.max_loss_per_package() == Decimal("3.20")
        assert spec.max_loss() == Decimal("640")


class TestCreditKindsMaxLossIsWidthMinusCredit:
    """Where width-based max loss actually lives: sold packages."""

    def test_put_credit_vertical(self) -> None:
        # SELL the 100 put, BUY the 95 put: width 5, credit floor 1.50, qty 2.
        # max loss per package = width - floor = 5 - 1.50 = 3.50 -> 3.50 x 100 x 2 = 700
        spec = LegStructure(
            **_leg_structure(
                "credit_vertical",
                [_leg("P", "SELL", "100"), _leg("P", "BUY", "95")],
                "1.50",
                quantity=2,
            )
        )
        assert spec.max_loss_per_package() == Decimal("3.50")
        assert spec.max_loss() == Decimal("700")

    @pytest.mark.parametrize(
        ("spot", "pkg_value", "book_pnl"),
        [
            # debit orientation (BUY 95 / SELL 100 flipped) value at expiry:
            #   max(0, 100 - spot) - max(0, 95 - spot); book P&L = (1.50 - v) x 100 x 2
            ("110", "0", "300"),  # both worthless: keep the credit 1.50 x 200
            ("100", "0", "300"),  # short put at the money: still worthless
            ("97", "3", "-300"),  # (1.50 - 3) x 200
            ("95", "5", "-700"),  # full width: the max loss (5 - 1.50) x 200
            ("0", "5", "-700"),  # pin the floor: loss stops at width - credit
        ],
    )
    def test_credit_payoff_bounded_by_width_minus_credit(self, spot: str, pkg_value: str,
                                                         book_pnl: str) -> None:
        s = Decimal(spot)
        value = max(Decimal(0), Decimal("100") - s) - max(Decimal(0), Decimal("95") - s)
        assert value == Decimal(pkg_value)
        pnl = (Decimal("1.50") - value) * 100 * 2
        assert pnl == Decimal(book_pnl)
        # the sampled states bottom out exactly at width - credit, in dollars:
        # (5 - 1.50) x 100 x 2 = 700 -- the width-based max loss is HERE,
        # on the sold package, not on the long debit verticals above
        assert pnl >= -Decimal("700")

    def test_iron_condor_uses_the_wider_wing(self) -> None:
        # put wing 95 - 90 = 5, call wing 115 - 105 = 10; wider = 10.
        # max loss = (10 - 2.00) x 100 x 1 = 800
        raw = _leg_structure(
            "iron_condor",
            [
                _leg("P", "BUY", "90"),
                _leg("P", "SELL", "95"),
                _leg("C", "SELL", "105"),
                _leg("C", "BUY", "115"),
            ],
            "2.00",
        )
        assert LegStructure(**raw).max_loss() == Decimal("800")

    def test_calendar_and_diagonal_cap_is_the_loss(self) -> None:
        # calendar: 1.10 x 100 x 3 = 330
        cal = _leg_structure(
            "calendar", [_leg("C", "SELL", "100", FRONT), _leg("C", "BUY", "100", BACK)],
            "1.10", quantity=3,
        )
        # protective put diagonal: 7.25 x 100 x 2 = 1,450
        diag = _leg_structure(
            "diagonal", [_leg("P", "SELL", "100", FRONT), _leg("P", "BUY", "105", BACK)],
            "7.25", quantity=2,
        )
        assert LegStructure(**cal).max_loss() == Decimal("330")
        assert LegStructure(**diag).max_loss() == Decimal("1450")


class TestGate3AuditNumbersRefuted:
    """The audit's $880 observation is right; its 31.8x reading is not."""

    def test_repo_plan_of_record_commits_exactly_1840(self) -> None:
        plan = load_plan(REPO / "plans" / "2026-09-22.toml")
        # 0.50x5x100 + 2.40x4x100 + 2.10x3x100 = 250 + 960 + 630
        assert plan.committed_at_caps == Decimal("1840") == plan.total_debit_cap

    def test_the_880_is_the_nvda_pair_at_caps(self) -> None:
        plan = _plan([_nvda_oct(), _nvda_nov()], total_debit_cap="880")
        # the audit's "$880": 250 + 630 -- the two NVDA structures at caps
        assert plan.committed_at_caps == Decimal("880")

    def test_width_is_max_win_not_max_loss(self) -> None:
        oct_, nov = PutSpread(**_nvda_oct()), PutSpread(**_nvda_nov())
        # the audit's "$28,000 true max loss" is 17,500 + 10,500: the sum of
        # the two structures' max VALUEs -- their best-case GROSS worth
        assert oct_.max_value + nov.max_value == Decimal("28000")
        # their worst-case loss at caps is the debit: 250 + 630 = 880
        assert oct_.max_debit + nov.max_debit == Decimal("880")

    def test_claimed_loss_exceeds_the_best_possible_win(self) -> None:
        # best-case book win at caps = (35 - 0.50) x 500 + (35 - 2.10) x 300
        #                           = 17,250 + 9,870 = 27,120
        best_win = (Decimal("35") - Decimal("0.50")) * 100 * 5 + (
            Decimal("35") - Decimal("2.10")
        ) * 100 * 3
        assert best_win == Decimal("27120")
        # a claimed max LOSS of 28,000 exceeds the best possible WIN: no
        # state of the world realizes it. The claim is impossible, not merely
        # unlikely.
        assert Decimal("28000") > best_win

    def test_mixed_book_sums_every_kind_correctly(self) -> None:
        # 250 spread + 250 debit_vertical + 700 credit_vertical + 800 condor
        # + 640 long_single + 330 calendar + 1,450 diagonal = 4,420
        plan = _plan(
            [
                _nvda_oct(),
                _leg_structure(
                    "debit_vertical", [_leg("P", "BUY", "185"), _leg("P", "SELL", "150")],
                    "0.50", quantity=5, id="oct-ml",
                ),
                _leg_structure(
                    "credit_vertical", [_leg("P", "SELL", "100"), _leg("P", "BUY", "95")],
                    "1.50", quantity=2, id="cv",
                ),
                _leg_structure(
                    "iron_condor",
                    [
                        _leg("P", "BUY", "90"), _leg("P", "SELL", "95"),
                        _leg("C", "SELL", "105"), _leg("C", "BUY", "115"),
                    ],
                    "2.00", id="ic",
                ),
                _leg_structure(
                    "long_single", [_leg("C", "BUY", "100", BACK)], "3.20", quantity=2, id="ls"
                ),
                _leg_structure(
                    "calendar", [_leg("C", "SELL", "100", FRONT), _leg("C", "BUY", "100", BACK)],
                    "1.10", quantity=3, id="cal",
                ),
                _leg_structure(
                    "diagonal", [_leg("P", "SELL", "100", FRONT), _leg("P", "BUY", "105", BACK)],
                    "7.25", quantity=2, id="diag",
                ),
            ],
            total_debit_cap="4420",
        )
        assert plan.committed_at_caps == Decimal("4420") == plan.total_debit_cap


class TestRailEnforcementIsParseTime:
    """The 5,000 hard rail runs at construction; reads never re-enforce."""

    def test_live_book_at_exactly_5000_constructs(self) -> None:
        # 0.50 x 100 x 100 = 5,000: at the rail, not above it
        plan = _plan([_nvda_oct(quantity=100)], account_mode="live", total_debit_cap="5000")
        assert plan.committed_at_caps == Decimal("5000")

    def test_live_book_above_5000_refused_at_parse(self) -> None:
        # 0.51 x 100 x 100 = 5,100 > 5,000
        with pytest.raises(ValueError, match="hard rail"):
            _plan([_nvda_oct(limit_cap="0.51", quantity=100)], account_mode="live")

    def test_paper_book_is_never_against_the_live_rail(self) -> None:
        # the 2026-09-22 plan of record is paper: 5,100 committed is fine
        plan = _plan([_nvda_oct(limit_cap="0.51", quantity=100)], account_mode="paper")
        assert plan.committed_at_caps == Decimal("5100")

    def test_unvalidated_mutation_is_not_re_enforced_on_read(self) -> None:
        # model_copy skips validators; the rail does not run again on read.
        plan = _plan([_nvda_oct()], total_debit_cap="250")
        mutated = plan.model_copy(update={"total_debit_cap": Decimal("1")})
        assert mutated.committed_at_caps == Decimal("250")  # reads fine, silently


class TestRuntimeRailReading:
    """The additive runtime-readable check: a monitor/desk can re-read the
    rails of any plan object without parsing TOML or catching exceptions.
    Construction stays the only ENFORCING point; this is the reading
    surface (and it stays honest on books that reached an over-rail state
    through unvalidated pydantic paths like model_copy)."""

    def test_hard_rail_constant_is_the_parse_time_number(self) -> None:
        # one shared constant, so the runtime readout can never drift from
        # the parse-time assertion in TradePlan._validate_book
        assert LIVE_HARD_RAIL == Decimal(5000)

    def test_repo_plan_of_record_reading(self) -> None:
        plan = load_plan(REPO / "plans" / "2026-09-22.toml")
        reading = rail_reading(plan)
        assert isinstance(reading, RailReading)
        # committed 1,840 == book cap 1,840: within, at the boundary (the
        # parse-time check refuses only strictly-greater)
        assert reading.committed == Decimal("1840")
        assert reading.book_cap == Decimal("1840")
        assert reading.account_mode == "paper"
        assert reading.within_book_cap is True
        # paper books are never against the live hard rail
        assert reading.live_rail == Decimal(5000)
        assert reading.within_live_rail is True

    def test_live_book_at_the_rail_reads_within(self) -> None:
        # 0.50 x 100 x 100 = 5,000: at the rail reads within it
        plan = _plan([_nvda_oct(quantity=100)], account_mode="live", total_debit_cap="5000")
        reading = rail_reading(plan)
        assert reading.within_live_rail is True and reading.within_book_cap is True

    def test_reading_catches_an_unvalidated_over_the_rail_book(self) -> None:
        # a live book at the rail, doubled through model_copy (validators
        # skipped): 0.50 x 200 x 100 = 10,000 committed against the 5,000 rail
        plan = _plan([_nvda_oct(quantity=100)], account_mode="live", total_debit_cap="5000")
        mutated = plan.model_copy(
            update={"structures": [PutSpread(**_nvda_oct(quantity=200))]}
        )
        assert mutated.committed_at_caps == Decimal("10000")  # nothing raised
        reading = rail_reading(mutated)
        assert reading.committed == Decimal("10000")
        assert reading.within_live_rail is False

    def test_reading_catches_an_unvalidated_book_cap_breach(self) -> None:
        # paper book committed 250, cap mutated down to 1: within_book_cap
        # is False even though account_mode is paper
        plan = _plan([_nvda_oct()], total_debit_cap="250")
        mutated = plan.model_copy(update={"total_debit_cap": Decimal("1")})
        reading = rail_reading(mutated)
        assert reading.within_book_cap is False
        assert reading.within_live_rail is True  # paper: the live rail never applies
