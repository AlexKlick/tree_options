"""Resolved-plan and binding-spec oracles (RL1-02 correction).

The 2026-09-25 audit reproduced a spec that was hashed, stored, and
then ignored: requested windows didn't bind, contribution settings
produced no contributions, cost models produced no fees, and cumulative
fee columns were summed into an inflated total ($1 + $2 -> $3). These
tests pin effect-or-refusal for every control the spec names: a setting
either changes the computation or produces a machine-readable refusal —
never a silently hashed no-op.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from tree_options.backtest.equity import FiveBasisPointFeeModel
from tree_options.research.comparison import engine as engine_mod
from tree_options.research.comparison.calendar import sessions_between
from tree_options.research.comparison.engine import run_comparison
from tree_options.research.comparison.funded import (
    MarkObservation,
    TradeExecution,
)
from tree_options.research.comparison.plan import (
    REFUSAL_CUTOFF_NEEDS_TIMEZONE,
    REFUSAL_INVALID_WINDOW,
    REFUSAL_NEGATIVE_CONTRIBUTION,
    REFUSAL_NO_SESSIONS,
    REFUSAL_UNSUPPORTED_REBALANCING,
    REFUSAL_UNSUPPORTED_SIZING,
    REFUSAL_WINDOW_REQUIRED,
    resolve_plan,
)
from tree_options.research.contracts import (
    CashflowTiming,
    ComparisonSpec,
    CostModelKind,
    PositionSizing,
    Rebalancing,
    ResearchCandidate,
    ResearchDisposition,
    ResearchEvidenceKind,
    ResearchRegistration,
)

D = Decimal
_WSTART = date(2024, 1, 2)
_WEND = date(2024, 2, 29)


def _spec(**overrides) -> ComparisonSpec:
    """A valid default spec: Jan-Feb 2024, $10k, five-bp costs."""
    defaults: dict = {
        "candidate_ids": ("synthetic-a-v1",),
        "starting_capital": D("10000"),
        "common_start": _WSTART,
        "common_end": _WEND,
    }
    defaults.update(overrides)
    return ComparisonSpec(**defaults)


def _synthetic_candidate(cid: str = "synthetic-a-v1") -> ResearchCandidate:
    return ResearchCandidate(
        id=cid, family=cid, version="v1",
        evidence_kind=ResearchEvidenceKind.SYNTHETIC_BACKTEST,
        registration=ResearchRegistration.BEFORE_ENTRY_WINDOW_END,
        disposition=ResearchDisposition.PASS,
        plot_funded_account=True,
        supported_start=_WSTART, supported_end=_WEND,
    )


# -- plan resolution: effect or refusal, per control --------------------------


def test_valid_spec_resolves_calendar_fee_model_and_window() -> None:
    plan = resolve_plan(_spec())
    assert not plan.refused
    assert plan.window_start == _WSTART and plan.window_end == _WEND
    assert plan.sessions == sessions_between(_WSTART, _WEND)
    assert isinstance(plan.fee_model, FiveBasisPointFeeModel)
    assert plan.calendar_sha256  # bound into the run receipt


def test_missing_window_refuses_rather_than_defaulting() -> None:
    assert resolve_plan(_spec(common_start=None)).refusal_reason == REFUSAL_WINDOW_REQUIRED
    assert resolve_plan(_spec(common_end=None)).refusal_reason == REFUSAL_WINDOW_REQUIRED


def test_reversed_window_refuses() -> None:
    plan = resolve_plan(_spec(common_start=date(2024, 3, 1),
                              common_end=date(2024, 1, 2)))
    assert plan.refusal_reason == REFUSAL_INVALID_WINDOW


def test_window_without_declared_sessions_refuses() -> None:
    # A weekend-only window contains no declared sessions.
    plan = resolve_plan(_spec(common_start=date(2024, 1, 6),
                              common_end=date(2024, 1, 7)))
    assert plan.refusal_reason == REFUSAL_NO_SESSIONS


def test_unimplemented_rebalancing_refuses_instead_of_ignoring() -> None:
    plan = resolve_plan(_spec(rebalancing=Rebalancing.MONTHLY))
    assert plan.refusal_reason == REFUSAL_UNSUPPORTED_REBALANCING


def test_fractional_sizing_refuses_instead_of_ignoring() -> None:
    plan = resolve_plan(_spec(position_sizing=PositionSizing.FRACTIONAL))
    assert plan.refusal_reason == REFUSAL_UNSUPPORTED_SIZING


def test_negative_contribution_refuses() -> None:
    plan = resolve_plan(_spec(contribution_per_period=D("-1")))
    assert plan.refusal_reason == REFUSAL_NEGATIVE_CONTRIBUTION


def test_naive_cutoff_refuses_aware_cutoff_is_preserved_exactly() -> None:
    naive = resolve_plan(_spec(knowledge_cutoff=datetime(2024, 1, 15, 12, 0)))
    assert naive.refusal_reason == REFUSAL_CUTOFF_NEEDS_TIMEZONE
    instant = datetime(2024, 1, 15, 12, 0, tzinfo=UTC)
    aware = resolve_plan(_spec(knowledge_cutoff=instant))
    assert not aware.refused
    assert aware.cutoff == instant  # the exact instant, never date-rounded


def test_monthly_contribution_schedule_respects_timing() -> None:
    sessions = sessions_between(_WSTART, _WEND)
    beginning = resolve_plan(_spec(contribution_per_period=D("500")))
    assert beginning.cashflows == (
        type(beginning.cashflows[0])(date(2024, 1, 2), D("500")),
        type(beginning.cashflows[0])(date(2024, 2, 1), D("500")),
    )
    end = resolve_plan(_spec(contribution_per_period=D("500"),
                             cashflow_timing=CashflowTiming.END_OF_PERIOD))
    # last declared session of each month: Jan 31 (Wed), Feb 29 (Thu)
    assert [c.date for c in end.cashflows] == [date(2024, 1, 31), date(2024, 2, 29)]
    assert all(s in sessions for c in end.cashflows for s in [c.date])


def test_zero_contribution_yields_no_schedule() -> None:
    assert resolve_plan(_spec()).cashflows == ()


def test_pass_through_cost_model_carries_no_fee_model() -> None:
    plan = resolve_plan(_spec(cost_model_kind=CostModelKind.PASS_THROUGH))
    assert not plan.refused
    assert plan.fee_model is None  # explicit per-execution fees only


# -- the plan binds the engine ------------------------------------------------


def _fake_adapter_streams(sessions, *, executions=(), marks=()):
    return executions, marks


def test_requested_window_excludes_out_of_window_data(monkeypatch: pytest.MonkeyPatch) -> None:
    """The audit: a comparison ending January 2 still contained a
    February 2 execution row. The window now binds — out-of-window
    observations are excluded AND COUNTED, never quietly plotted."""
    sessions = sessions_between(date(2024, 1, 2), date(2024, 1, 31))
    feb_2 = date(2024, 2, 2)
    in_window_exec = TradeExecution(sessions[0], "SYN", 1, D("100.00"))
    out_exec = TradeExecution(feb_2, "SYN", -1, D("110.00"))
    marks = [MarkObservation(sessions[0], "SYN", D("100.00")),
             MarkObservation(feb_2, "SYN", D("110.00"))]

    monkeypatch.setitem(
        engine_mod._ADAPTERS, ResearchEvidenceKind.SYNTHETIC_BACKTEST,
        lambda cand, plan: ([in_window_exec, out_exec], marks))
    res = run_comparison(
        _spec(common_start=date(2024, 1, 2), common_end=date(2024, 1, 31)),
        (_synthetic_candidate(),))
    summary = res.candidates[0]
    assert summary.rejection_reason is None
    # internal rows are date-keyed; to_wire makes them ISO
    assert all(d <= date(2024, 1, 31) for d in summary.rows_by_date)
    assert summary.excluded_out_of_window == 2  # one execution + one mark


def test_contribution_setting_now_produces_contributions(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """The audit: a positive per-period contribution was accepted and
    output contributions stayed zero. Hand oracle: $10,000, buy 1 @
    $100 on Jan 2 (5 bps fee = $0.05), marks flat at $100, $500 on the
    first session of each of Jan and Feb ⇒ final NAV = 10,000 + 1,000
    - 0.05 = $10,999.95; contributions_cum = $1,000; investment_gain
    = -$0.05 (contributions are wealth, not profit)."""
    sessions = sessions_between(_WSTART, _WEND)
    execs = [TradeExecution(sessions[0], "SYN", 1, D("100.00"))]
    marks = [MarkObservation(s, "SYN", D("100.00")) for s in sessions]
    monkeypatch.setitem(
        engine_mod._ADAPTERS, ResearchEvidenceKind.SYNTHETIC_BACKTEST,
        lambda cand, plan: (execs, marks))
    res = run_comparison(_spec(contribution_per_period=D("500")),
                         (_synthetic_candidate(),))
    s = res.candidates[0]
    assert s.rejection_reason is None
    final = s.rows_by_date[max(s.rows_by_date)]
    assert final["contributions_cum"] == "1000.00"
    assert final["nav"] == "10999.95"
    assert final["investment_gain"] == "-0.05"
    assert s.final_ending_value == D("10999.95")


def test_five_bp_cost_model_prices_executions_pass_through_does_not(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """The audit: five-bp and pass-through selections BOTH produced
    zero fees. Five-bp now prices the fill (1 @ $200 ⇒ $0.10);
    pass-through uses only explicitly supplied fees."""
    sessions = sessions_between(_WSTART, _WEND)
    execs = [TradeExecution(sessions[0], "SYN", 1, D("200.00"))]
    marks = [MarkObservation(sessions[0], "SYN", D("200.00"))]
    monkeypatch.setitem(
        engine_mod._ADAPTERS, ResearchEvidenceKind.SYNTHETIC_BACKTEST,
        lambda cand, plan: (execs, marks))

    five_bp = run_comparison(_spec(), (_synthetic_candidate(),))
    assert five_bp.candidates[0].fees_paid_total == D("0.10")

    passthrough = run_comparison(
        _spec(cost_model_kind=CostModelKind.PASS_THROUGH), (_synthetic_candidate(),))
    assert passthrough.candidates[0].fees_paid_total == D("0.00")


def test_total_fees_are_final_cumulative_not_sum_of_rows(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """The audit: two $1 fees produced cumulative row fees $1 and $2,
    which the orchestrator summed into an incorrect $3 total."""
    sessions = sessions_between(_WSTART, _WEND)
    execs = [
        TradeExecution(sessions[0], "SYN", 10, D("5.00"), fees=D("1.00")),
        TradeExecution(sessions[5], "SYN", -10, D("6.00"), fees=D("1.00")),
    ]
    marks = [MarkObservation(sessions[0], "SYN", D("5.00")),
             MarkObservation(sessions[5], "SYN", D("6.00"))]
    monkeypatch.setitem(
        engine_mod._ADAPTERS, ResearchEvidenceKind.SYNTHETIC_BACKTEST,
        lambda cand, plan: (execs, marks))
    res = run_comparison(
        _spec(cost_model_kind=CostModelKind.PASS_THROUGH), (_synthetic_candidate(),))
    assert res.candidates[0].fees_paid_total == D("2.00")


def test_refused_plan_rejects_every_candidate_with_the_reason(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(
        engine_mod._ADAPTERS, ResearchEvidenceKind.SYNTHETIC_BACKTEST,
        lambda cand, plan: ([], []))
    res = run_comparison(_spec(rebalancing=Rebalancing.MONTHLY),
                         (_synthetic_candidate(),))
    assert res.rejection == REFUSAL_UNSUPPORTED_REBALANCING
    assert res.candidates[0].rejection_reason == REFUSAL_UNSUPPORTED_REBALANCING
    assert res.candidates[0].rows_by_date == {}


def test_nonempty_result_serializes_through_one_wire_boundary(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """The audit's HTTP 500: ``rows_by_date``/``drawdown`` keyed by
    ``datetime.date`` went straight into ``JSONResponse``. ``to_wire``
    is the single boundary and must make a NONEMPTY result
    json-serializable with ISO keys."""
    sessions = sessions_between(date(2024, 1, 2), date(2024, 1, 31))
    execs = [TradeExecution(sessions[0], "SYN", 10, D("5.00"), fees=D("1.00")),
             TradeExecution(sessions[5], "SYN", -10, D("6.00"), fees=D("1.00"))]
    marks = [MarkObservation(s, "SYN", D("5.00")) for s in sessions]
    monkeypatch.setitem(
        engine_mod._ADAPTERS, ResearchEvidenceKind.SYNTHETIC_BACKTEST,
        lambda cand, plan: (execs, marks))
    res = run_comparison(
        _spec(common_start=date(2024, 1, 2), common_end=date(2024, 1, 31),
              cost_model_kind=CostModelKind.PASS_THROUGH),
        (_synthetic_candidate(),))
    wire = res.to_wire()
    text = json.dumps(wire)  # must not raise
    assert '"2024-01-02"' in text
    for s in wire["candidates"]:
        for key in s["rows_by_date"]:
            assert date.fromisoformat(key)  # every row key is an ISO date
    # money crosses as strings
    row = wire["candidates"][0]["rows_by_date"]["2024-01-02"]
    assert isinstance(row["nav"], str) or row["nav"] is None
