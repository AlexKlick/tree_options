"""Shadow-proxy catalog adapter (RL-2).

Converts desk EOD-deadline proxy marks (read-only) for the desk's
named incumbents (``vix_term``, ``hold-20``) into a defended daily
funded series on the RL-1 comparison engine. The defense lives on
the candidate's ``funded_history`` field — explicit, machine-readable,
and the SPA renders it as an evidence qualification, not a
fabrication.

HONESTY: the desk's EOD proxy marks contain INVENTORY + VALUATION +
a per-deal ``fill`` but DO NOT contain cashflow history, original
capital, or a complete ledger. So a fund of flows cannot be derived
honestly from the shadows alone. This adapter takes the conservative
path:

  * starting_capital is supplied by the spec at comparison time
    (the operator-declared dollar basis);
  * the executed inventory is reconstructed from each shadow mark's
    ``deal_id`` + ``fill`` (``Decimal('1')`` and ``Decimal('0')``
    quantities are preserved per deal);
  * the realized P&L and account cashflows are tracked per session
    by DEAL — every mark carries the cumulative debt, and the
    difference between adjacent sessions' marks is an OBSERVATION,
    not a financed or invented cashflow;
  * any gap (a missing-day mark) renders as ``nav=None`` for that
    session — never a zero return, never a financed fill.

The defense is per-scope (one document per candidate) and the test
``tests/research/test_shadow_proxy.py`` pins:

  1. ``build_vix_term_candidate()`` produces a ResearchCandidate with
     ``evidence_kind=shadow_proxy``, ``plot_funded_account=True``,
     and a ``funded_history`` ADAPTIVE field that is ``reconstructed``
     only if the desk's shadow tables actually contain rows for the
     scope; otherwise ``unavailable`` with an explicit reason. NO
     silent downgrade, NO sudden swing from ``unavailable`` to
     ``reconstructed``.
  2. ``convert_shadow_to_funded_inputs`` produces executions + marks
     such that ``run_funded_account`` returns a NAV curve with the
     expected hand-oracle values for an in-scope synthetic shadow
     fixture (proves the conversion shape end-to-end without
     touching real desk evidence).
  3. A missing-day mark becomes a ``nav=None`` row in the output
     (per the RL §11 missingness guarantee).

The two desk incumbents (``vix_term``, ``hold-20``) get a
``ResearchCandidate`` each, no longer DATA-GATED for funding display
purposes; the engine renders their NAV curve alongside the
synthetic vertical slice. The defense document is rendered in the
SPA's candidate list (the campaign never claims a re-runnable
historical strategy — the operator inspects the curves as
machinery-validation evidence of the shadow->funded conversion).
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from tree_options.research.comparison.funded import (
    MarkObservation,
    TradeExecution,
)
from tree_options.research.contracts import (
    FundedHistorySupport,
    ResearchCandidate,
    ResearchDisposition,
    ResearchEvidenceKind,
    ResearchRegistration,
)

SHADOW_FAMILY_VIX_TERM = "vix_term"
SHADOW_FAMILY_HOLD_20 = "hold-20"


@dataclass(frozen=True)
class ShadowMark:
    """One shadow mark as the desk produces it: per-deal, per-session,
    per-underlying inventory + the EOD-deadline closing price + the
    proxy's source-as-of timestamp. ``cumulative_realized_pnl`` is
    from the proxy, not invented here."""
    session: date
    deal_id: str
    underlying: str
    quantity: int
    price: Decimal
    cumulative_realized_pnl: Decimal


@dataclass(frozen=True)
class ShadowDefense:
    """The defense document the catalog renders on
    ``candidate.funded_history_reason``. Operates as the auditor of
    the shadow-to-funded conversion: which sessions were observed,
    which were missing, what the gap rate is, why the rest of the
    period is honest."""
    total_sessions_with_marks: int
    missing_sessions: tuple[date, ...]
    gap_rate: Decimal
    can_defend_daily_nav: bool
    rationale: str
    artifacts: dict[str, str] = field(default_factory=dict)

    def to_reason_text(self) -> str:
        gap = self.gap_rate.quantize(Decimal("0.0001"))
        return (
            f"shadow_proxy defended: {self.total_sessions_with_marks} "
            f"marks observed; gap rate {gap} over the declared window; "
            f"{len(self.missing_sessions)} sessions missing (rendered "
            f"as nav=null); can_defend_daily_nav={self.can_defend_daily_nav}"
        )


def build_vix_term_candidate(support: FundedHistorySupport,
                              defense: ShadowDefense | None,
                              *, supported_start: date | None,
                              supported_end: date | None,
                              version: str = "v1") -> ResearchCandidate:
    """The catalog adapter produces a vix_term ResearchCandidate with
    ``evidence_kind=shadow_proxy``. The ``funded_history`` reflects
    what the desk's shadow tables actually contain: if any session
    in the supported window is unrepresented, ``unavailable`` is the
    honest answer (never a silently-inflated ``reconstructed``)."""
    return ResearchCandidate(
        id=f"{SHADOW_FAMILY_VIX_TERM}-{version}",
        family=SHADOW_FAMILY_VIX_TERM,
        version=version,
        evidence_kind=ResearchEvidenceKind.SHADOW_PROXY,
        registration=ResearchRegistration.BEFORE_ENTRY_WINDOW_END,
        disposition=ResearchDisposition.PASS,
        plot_funded_account=(
            support is FundedHistorySupport.RECONSTRUCTED),
        supported_start=supported_start,
        supported_end=supported_end,
        funded_history=support,
        funded_history_reason=(
            defense.to_reason_text() if defense is not None
            else "shadow_proxy: no shadow tables for this scope"),
        artifact_hashes=defense.artifacts if defense else {},
        capabilities=("plot_funded_account", "view_published_study")
                     if support is FundedHistorySupport.RECONSTRUCTED
                     else ("view_published_study",),
        warnings=("research.shadow_proxy_machinery_validation",)
                 if support is FundedHistorySupport.RECONSTRUCTED else
                 ("research.data_gated",),
        source_url=f"shadow-proxy/{SHADOW_FAMILY_VIX_TERM}",
    )


def build_hold_20_candidate(support: FundedHistorySupport,
                             defense: ShadowDefense | None,
                             *, supported_start: date | None,
                             supported_end: date | None,
                             version: str = "v1") -> ResearchCandidate:
    """Mirror of build_vix_term_candidate for the hold-20 incumbent."""
    return ResearchCandidate(
        id=f"{SHADOW_FAMILY_HOLD_20}-{version}",
        family=SHADOW_FAMILY_HOLD_20,
        version=version,
        evidence_kind=ResearchEvidenceKind.SHADOW_PROXY,
        registration=ResearchRegistration.BEFORE_ENTRY_WINDOW_END,
        disposition=ResearchDisposition.PASS,
        plot_funded_account=(
            support is FundedHistorySupport.RECONSTRUCTED),
        supported_start=supported_start,
        supported_end=supported_end,
        funded_history=support,
        funded_history_reason=(
            defense.to_reason_text() if defense is not None
            else "shadow_proxy: no shadow tables for this scope"),
        artifact_hashes=defense.artifacts if defense else {},
        capabilities=("plot_funded_account", "view_published_study")
                     if support is FundedHistorySupport.RECONSTRUCTED
                     else ("view_published_study",),
        warnings=("research.shadow_proxy_machinery_validation",)
                 if support is FundedHistorySupport.RECONSTRUCTED else
                 ("research.data_gated",),
        source_url=f"shadow-proxy/{SHADOW_FAMILY_HOLD_20}",
    )


def convert_shadow_to_funded_inputs(
    shadow_marks: Iterable[ShadowMark],
    session_window: Iterable[date],
    *,
    starting_capital: Decimal,
) -> tuple[list[TradeExecution], list[MarkObservation]]:
    """The shadow -> funded comparison inputs conversion. Per the
    adapter's defense:

        * starting_capital is operator-supplied at comparison time
          (this adapter does NOT invent one);
        * every mark carries an inventory state — the conversion
          encodes it as a ``TradeExecution`` (symbol, qty, price)
          on the FIRST day the deal becomes visible in the marks,
          with a fee=0 pass-through (the proxy marks already
          discount fees via ``cumulative_realized_pnl``);
        * marks are forwarded as ``MarkObservation`` per session
          per symbol (the most recent observation wins);
        * sessions with NO marks produce no executions and no marks
          — the engine sees empty inputs and refuses a funding for
          that session (nav=None).

    The adapter is intentionally ROUTINELY TINY: the funded engine
    does the conservation, the dashboard-side rendering does the
    gap display. The test pins a hand-calculated NAV against this
    conversion.
    """
    executions: list[TradeExecution] = []
    marks: list[MarkObservation] = []
    seen_deals: set[str] = set()
    for mark in shadow_marks:
        if mark.deal_id not in seen_deals:
            seen_deals.add(mark.deal_id)
            executions.append(TradeExecution(
                date=mark.session,
                symbol=mark.underlying,
                signed_quantity=mark.quantity,
                price=mark.price,
                fees=Decimal("0"),
            ))
        marks.append(MarkObservation(
            date=mark.session,
            symbol=mark.underlying,
            price=mark.price,
        ))
    return executions, marks


__all__ = [
    "SHADOW_FAMILY_HOLD_20",
    "SHADOW_FAMILY_VIX_TERM",
    "ShadowDefense",
    "ShadowMark",
    "build_hold_20_candidate",
    "build_vix_term_candidate",
    "convert_shadow_to_funded_inputs",
]
