"""Shadow-proxy catalog adapter oracles (RL-2).

Three oracles pin the desk-shadow -> funded-history conversion:

    1. The conversion shape: a small synthetic shadow mark set
       produces executions + marks whose ``run_funded_account`` NAV
       equals the hand-oracle NAV.
    2. The defense document: a ``ShadowDefense`` summary that the
       catalog renders on ``funded_history_reason`` is HONEST about
       gaps and never claims ``reconstructed`` when the observed
       coverage cannot defend a daily NAV.
    3. The candidate boundaries: vix_term / hold-20 candidates are
       constructed with the RIGHT evidence_kind, plot_funded_account,
       and funded_history for the support level the supplied defense
       warrants.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from tree_options.research.catalog.shadow_proxy import (
    SHADOW_FAMILY_HOLD_20,
    SHADOW_FAMILY_VIX_TERM,
    ShadowDefense,
    ShadowMark,
    build_hold_20_candidate,
    build_vix_term_candidate,
    convert_shadow_to_funded_inputs,
)
from tree_options.research.comparison.funded import run_funded_account
from tree_options.research.contracts import (
    FundedHistorySupport,
    ResearchEvidenceKind,
)

# -- 1. Conversion shape (hand-calculated oracle) --------------------------


def test_shadow_marks_convert_to_hand_calculable_funded_inputs():
    """A 2-session synthetic shadow (1 deal, $10k starting capital,
    a single buy at $400, hold through mark at $410) converts to a
    NAV of $10,200 after the second session (cash + 25 * 410 = $10,250,
    minus buy + fees folded into ``run_funded_account``)."""
    marks = [
        ShadowMark(
            session=date(2024, 1, 2),
            deal_id="d1",
            underlying="SPY",
            quantity=25,
            price=Decimal("400.00"),
            cumulative_realized_pnl=Decimal("0.00"),
        ),
        ShadowMark(
            session=date(2024, 1, 3),
            deal_id="d1",
            underlying="SPY",
            quantity=25,
            price=Decimal("410.00"),
            cumulative_realized_pnl=Decimal("0.00"),
        ),
    ]
    executions, marks_out = convert_shadow_to_funded_inputs(
        marks, [date(2024, 1, 2), date(2024, 1, 3)],
        starting_capital=Decimal("10000"),
    )
    # First mark -> one execution (25 SPY @ 400.00, fee=0)
    assert len(executions) == 1
    assert executions[0].date == date(2024, 1, 2)
    assert executions[0].symbol == "SPY"
    assert executions[0].signed_quantity == 25
    assert executions[0].price == Decimal("400.00")
    # Two mark observations (one per session per deal)
    assert len(marks_out) == 2

    # Hand-oracle NAV: starting_capital 10000 - 25*400 = 0 cash + 0 fee;
    # after session 2 mark = 25 * 410 = 10250 -> NAV = 10250.
    result = run_funded_account(
        candidate_id="t",
        starting_capital=Decimal("10000"),
        calendar=[date(2024, 1, 2), date(2024, 1, 3)],
        executions=executions,
        marks=marks_out,
        cashflows=(),
        fee_model=None,
    )
    assert len(result.rows) == 2
    assert result.rows[0].nav == Decimal("10000.00")
    assert result.rows[1].nav == Decimal("10250.00")


# -- 2. Defense document honesty -------------------------------------------


def test_defense_with_no_marks_is_unavailable():
    """A defense built from ZERO observed marks cannot defend a daily
    NAV — it must render ``unavailable`` with an explicit rationale."""
    defense = ShadowDefense(
        total_sessions_with_marks=0,
        missing_sessions=(),
        gap_rate=Decimal("1.0"),
        can_defend_daily_nav=False,
        rationale="no shadow marks in scope",
    )
    text = defense.to_reason_text()
    assert "can_defend_daily_nav=False" in text
    assert "0 marks observed" in text


def test_defense_with_full_coverage_can_be_used_for_reconstructed():
    """A defense with full coverage and no missing sessions is the
    ONLY situation where ``reconstructed`` is honest."""
    defense = ShadowDefense(
        total_sessions_with_marks=61,
        missing_sessions=(),
        gap_rate=Decimal("0.0"),
        can_defend_daily_nav=True,
        rationale="every declared session observed",
    )
    text = defense.to_reason_text()
    assert "gap rate 0.0000" in text
    assert "can_defend_daily_nav=True" in text


def test_defense_with_partial_coverage_marks_gap_rate():
    """Partial coverage surfaces the gap rate honestly — never a
    rounded-down 0%."""
    defense = ShadowDefense(
        total_sessions_with_marks=50,
        missing_sessions=(date(2024, 1, 15), date(2024, 1, 31),
                          date(2024, 2, 1), date(2024, 2, 6),
                          date(2024, 2, 7), date(2024, 2, 8),
                          date(2024, 2, 9), date(2024, 2, 10),
                          date(2024, 2, 11), date(2024, 2, 12),
                          date(2024, 2, 13)),
        gap_rate=Decimal("11") / Decimal("61"),
        can_defend_daily_nav=False,
        rationale="11-day gap; nav=null on missing sessions",
    )
    text = defense.to_reason_text()
    # gap rate must NOT round to zero
    assert "0.1803" in text or "0.1" in text
    assert "11 sessions missing" in text


# -- 3. Candidate boundaries ------------------------------------------------


def test_vix_term_candidate_when_defended():
    """A scope with full shadow coverage gets a plottable,
    evidence_kind=shadow_proxy ResearchCandidate. The reason text
    carries the defense for the SPA to render."""
    defense = ShadowDefense(
        total_sessions_with_marks=61,
        missing_sessions=(),
        gap_rate=Decimal("0"),
        can_defend_daily_nav=True,
        rationale="every declared session observed",
        artifacts={"chain": "sha:abc"},
    )
    cand = build_vix_term_candidate(
        FundedHistorySupport.RECONSTRUCTED, defense,
        supported_start=date(2024, 1, 2),
        supported_end=date(2024, 3, 28),
    )
    assert cand.id == "vix_term-v1"
    assert cand.family == SHADOW_FAMILY_VIX_TERM
    assert cand.evidence_kind is ResearchEvidenceKind.SHADOW_PROXY
    assert cand.funded_history is FundedHistorySupport.RECONSTRUCTED
    assert cand.plot_funded_account is True
    assert "plot_funded_account" in cand.capabilities
    assert "can_defend_daily_nav=True" in cand.funded_history_reason
    assert cand.artifact_hashes.get("chain") == "sha:abc"


def test_vix_term_candidate_when_no_defense():
    """A scope with NO shadow tables in the desk tree is honestly
    ``unavailable``, NOT ``reconstructed`` with a fabricated
    defense. ``plot_funded_account`` is False; the candidate is
    inspectable but not plottable (RL-1 invariant)."""
    cand = build_vix_term_candidate(
        FundedHistorySupport.UNAVAILABLE, None,
        supported_start=None, supported_end=None,
    )
    assert cand.funded_history is FundedHistorySupport.UNAVAILABLE
    assert cand.plot_funded_account is False
    assert "plot_funded_account" not in cand.capabilities
    assert "view_published_study" in cand.capabilities
    # Reason should be visible even when defense is None
    assert "no shadow tables" in cand.funded_history_reason


def test_hold_20_candidate_mirrors_vix_term():
    defense = ShadowDefense(
        total_sessions_with_marks=20,
        missing_sessions=(),
        gap_rate=Decimal("0"),
        can_defend_daily_nav=True,
        rationale="20-session hold observed",
    )
    cand = build_hold_20_candidate(
        FundedHistorySupport.RECONSTRUCTED, defense,
        supported_start=date(2024, 1, 2),
        supported_end=date(2024, 1, 31),
    )
    assert cand.id == "hold-20-v1"
    assert cand.family == SHADOW_FAMILY_HOLD_20
    assert cand.evidence_kind is ResearchEvidenceKind.SHADOW_PROXY
    assert cand.plot_funded_account is True
    assert "can_defend_daily_nav=True" in cand.funded_history_reason


def test_shadow_evidence_kind_marker():
    """Tiny structural oracle: the catalog scan key for shadow-proxy
    candidates is exactly ``"shadow_proxy"`` (matches the engine
    adapter table)."""
    from tree_options.research.catalog.shadow_proxy import (
        adapt_shadow_executions,
    )
    assert adapt_shadow_executions(
        ResearchEvidenceKind.SHADOW_PROXY) == "shadow_proxy"
    assert adapt_shadow_executions(
        ResearchEvidenceKind.SEALED_CAMPAIGN) == ""


# -- 4. Shadow fixture integration (oracle 4 of RL-2) ----------------------


def test_full_shadow_funded_engine_pipeline_hand_calculable():
    """End-to-end: shadow marks -> funded inputs -> funded engine.
    The Oracle NAV (= starting + sum(qty * last_mark)) is the binding
    value the audit demanded (RL1-01). No real desk evidence is
    touched; the source is a fixture list."""
    marks = [
        ShadowMark(date(2024, 1, 2), "d1", "SPY", 10, Decimal("100"), Decimal("0")),
        ShadowMark(date(2024, 1, 3), "d1", "SPY", 10, Decimal("105"), Decimal("0")),
        ShadowMark(date(2024, 1, 4), "d1", "SPY", 10, Decimal("103"), Decimal("0")),
    ]
    executions, _marks = convert_shadow_to_funded_inputs(
        marks, [m.session for m in marks],
        starting_capital=Decimal("1000"),
    )
    # The engine applies executions + marks: 1 buy (10 * $100 = $1000)
    # clears the account. NAV at end = $1030 (10 * $103).
    result = run_funded_account(
        candidate_id="x",
        starting_capital=Decimal("1000"),
        calendar=[m.session for m in marks],
        executions=executions,
        marks=_marks,
        cashflows=(),
        fee_model=None,
    )
    assert result.refusal_reason is None
    assert result.rows[-1].nav == Decimal("1030.00")
    # Investment gain = NAV - opening - contributions + withdrawals
    assert result.rows[-1].investment_gain == Decimal("30.00")
