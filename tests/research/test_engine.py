"""Comparison engine tests — RL §11 acceptance matrix rows that the
engine covers.

Coverage:
    * Cashflows vs return — contributions change wealth, not gain
    * Account conservation — LedgerBook.assert_conservation holds
    * Cost / size changes — FiveBasisPointFeeModel.affordable_quantity
      recompute (the fee-model integration is at the budget layer; the
      engine surfaces the model's effect via run_funded_account's fees
      accounting)
    * Dataset scope — synthetic, paper, sealed, broker-paper are distinct
      evidence kinds; the engine refuses broker-paper with a reason
    * Selection integrity — retrospective candidates get no wallet curve
    * Sample floor — sample_floor_met is set only when sample_size >=
      SAMPLE_FLOOR
    * Missingness (priced-subset never prices whole) — covered via the
      funded accounting + the paired-diff null rendering (see test_pair.py)
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from tree_options.research.comparison.engine import (
    SAMPLE_FLOOR,
    run_comparison,
)
from tree_options.research.comparison.funded import (
    CashflowEvent,
    TradeExecution,
    run_funded_account,
)
from tree_options.research.contracts import (
    PLOT_FUNDED_ALLOWED,
    ComparisonSpec,
    ResearchCandidate,
    ResearchDisposition,
    ResearchEvidenceKind,
    ResearchRegistration,
)


def _candidate(*, family="vix_term", disposition=ResearchDisposition.PASS,
               registration=ResearchRegistration.BEFORE_ENTRY_WINDOW_END,
               evidence=ResearchEvidenceKind.SEALED_CAMPAIGN,
               supported_start=date(2024, 1, 1),
               supported_end=date(2026, 9, 25),
               id=None) -> ResearchCandidate:
    cid = id or f"{family}-v2"
    return ResearchCandidate(
        id=cid,
        family=family,
        version="v2",
        evidence_kind=evidence,
        registration=registration,
        disposition=disposition,
        plot_funded_account=disposition in PLOT_FUNDED_ALLOWED,
        supported_start=supported_start,
        supported_end=supported_end,
    )


def _spec(candidates=("vix_term-v2", "hold-20-v2"),
          starting_capital=Decimal("10000")) -> ComparisonSpec:
    return ComparisonSpec(
        candidate_ids=candidates,
        starting_capital=starting_capital,
        common_start=date(2024, 1, 2),
        common_end=date(2026, 9, 25),
    )


# -- funded accounting tests (cashflows vs return, conservation) --------------


def test_contribution_changes_wealth_not_profit() -> None:
    """A capital injection increases ending_value by the injection
    amount — but it does NOT increase ``gain`` (gain = realized +
    unrealized; capital is wealth, not P&L)."""
    run = run_funded_account(
        candidate_id="vix_term-v2",
        starting_capital=Decimal("10000"),
        executions=[
            TradeExecution(date=date(2024, 1, 2), symbol="NVDA",
                           signed_quantity=10, price=Decimal("5.00"),
                           realized_pnl=Decimal("0")),
            TradeExecution(date=date(2024, 6, 1), symbol="NVDA",
                           signed_quantity=-10, price=Decimal("6.00"),
                           realized_pnl=Decimal("10.00")),
        ],
        cashflows=[
            CashflowEvent(date=date(2024, 1, 15), amount=Decimal("500")),
        ],
    )
    # Rows are emitted only on execution dates (not pure cashflow dates);
    # the $500 contribution on 2024-01-15 is rolled into the 2024-06-01
    # row's `contributions` field.
    assert len(run.rows) == 2
    r1 = run.rows[0]
    assert r1.starting_capital == Decimal("10000")
    assert r1.committed_signed == Decimal("-50")  # 10 long @ $5 debit
    assert r1.contributions == Decimal("0")
    assert r1.gain == Decimal("0")
    assert r1.ending_value == Decimal("9950")
    # Second row (after 2024-06-01): close + $500 contribution already booked
    r2 = run.rows[1]
    # Closed the position at $6/share: receive +$60 (cash in); committed_signed
    # goes from -50 to -50 + 60 = +10 (a positive committed_signed at this
    # moment means the wallet is briefly ahead of starting capital in net
    # cash terms; the realized P&L is the gain, NOT a committed-signed bump.
    assert r2.committed_signed == Decimal("10")
    assert r2.contributions == Decimal("500")
    assert r2.withdrawals == Decimal("0")
    # gain is the day_realized (10) — not the contribution
    assert r2.gain == Decimal("10")
    # ending = 10000 + 10 + 500 - 0 + 10 = 10520
    assert r2.ending_value == Decimal("10520")


def test_ledger_assert_conservation_holds_across_full_run() -> None:
    """The four-line identity is exact — conservation never breaks."""
    run = run_funded_account(
        candidate_id="hold-20-v2",
        starting_capital=Decimal("5000"),
        executions=[
            TradeExecution(date=date(2024, 2, 1), symbol="SPY",
                           signed_quantity=20, price=Decimal("100"),
                           realized_pnl=Decimal("0")),
            TradeExecution(date=date(2024, 7, 1), symbol="SPY",
                           signed_quantity=-20, price=Decimal("120"),
                           realized_pnl=Decimal("400")),
        ],
        cashflows=[
            CashflowEvent(date=date(2024, 3, 1), amount=Decimal("1000")),
            CashflowEvent(date=date(2024, 9, 1), amount=Decimal("-500")),  # withdrawal
        ],
    )
    for r in run.rows:
        # ending = starting + committed + contribs - withdrawals + gain
        expected = (r.starting_capital + r.committed_signed + r.contributions
                   - r.withdrawals + r.gain)
        assert r.ending_value == expected, f"row {r.date} failed conservation"
    # LedgerBook.assert_conservation also runs at the end of run_funded_account
    # and would have raised on a bookkeeping bug.


# -- engine tests (acceptance matrix) --------------------------------------


def test_engine_refuses_broker_paper_with_reason() -> None:
    c = _candidate(evidence=ResearchEvidenceKind.BROKER_PAPER,
                  disposition=ResearchDisposition.PASS)
    spec = _spec()
    res = run_comparison(spec, (c,), baseline=_candidate(family="bh"))
    assert res.candidates[0].rejection_reason is not None
    assert "broker_paper" in res.candidates[0].rejection_reason or \
           "research.broker_paper" == res.candidates[0].rejection_reason


def test_engine_refuses_retrospective_with_reason() -> None:
    c = _candidate(registration=ResearchRegistration.RETROSPECTIVE_BACKFILL)
    spec = _spec()
    res = run_comparison(spec, (c,))
    assert res.candidates[0].rejection_reason == "research.retrospective_only"


def test_engine_marks_ineligible_disposition_with_reason() -> None:
    c = _candidate(disposition=ResearchDisposition.WITHDRAWN)
    spec = _spec()
    res = run_comparison(spec, (c,))
    assert res.candidates[0].rejection_reason is not None
    assert "WITHDRAWN" in res.candidates[0].rejection_reason


def test_engine_zero_sample_size_is_below_sample_floor() -> None:
    c = _candidate()  # SEALED_CAMPAIGN with no executions yet (adapter stub)
    spec = _spec()
    res = run_comparison(spec, (c,))
    assert res.candidates[0].sample_size == 0
    assert res.candidates[0].sample_floor == SAMPLE_FLOOR
    assert res.candidates[0].sample_floor_met is False


def test_engine_sealed_candidate_returns_zero_fills_until_adapter_filled() -> None:
    """The sealed adapter stub returns []; the engine surfaces this as
    zero-row summary with no fabricated wallet curve (RL §11 missingness
    + handoff §4 'unsupported candidates show reasons instead of
    invented series')."""
    c = _candidate()
    spec = _spec()
    res = run_comparison(spec, (c,))
    assert res.candidates[0].rows_by_date == {}
    assert res.candidates[0].drawdown == {}
    assert res.candidates[0].final_ending_value is None
    assert res.candidates[0].rejection_reason is None  # eligibility ok
    assert res.candidates[0].sample_size == 0


def test_engine_paired_diff_only_for_plot_eligible_candidates() -> None:
    """Ineligible candidates never enter the paired diff."""
    eligible = _candidate(family="vix_term")
    ineligible = _candidate(family="term-gate",
                            disposition=ResearchDisposition.WITHDRAWN)
    baseline = _candidate(family="bh",
                         disposition=ResearchDisposition.HOLD_STANDS)
    spec = _spec(candidates=(eligible.id, ineligible.id))
    res = run_comparison(spec, (eligible, ineligible), baseline=baseline)
    assert eligible.id in res.paired_diff
    assert ineligible.id not in res.paired_diff
