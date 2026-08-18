"""Option candidate filters (§9.2, audit §5.2): tri-state, provenance-aware.

Every rule emits PASS / FAIL / NOT_EVALUABLE / NOT_APPLICABLE; the decision
records ALL rule outcomes (an audit, not a first-failure gate). A candidate
is accepted only if no rule FAILs or is NOT_EVALUABLE — missing or
future-available inputs never silently pass. An optional rule whose input is
legitimately not yet available (same-day volume early in the session) is
recorded NOT_APPLICABLE and does not block acceptance.

Time-varying inputs (delta, OI, volume, quote, earnings, liquidity) each
carry an `available_at` instant; an input available AFTER the decision
instant is rejected as future data (INV-03 at the candidate layer).

DTE convention: CALENDAR days between decision session and expiration
(market convention for "days to expiration").

Nonclaim: synthetic M0 plumbing — historical option-chain selection is NOT
implemented here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from tree_options.protocol.schema import ResearchProtocol
from tree_options.schemas.options import OptionContract
from tree_options.time.calendar import NotASessionError

PASS = "PASS"
FAIL = "FAIL"
NOT_EVALUABLE = "NOT_EVALUABLE"
NOT_APPLICABLE = "NOT_APPLICABLE"


@dataclass(frozen=True)
class AsOf:
    """A value plus the instant it became available (provenance + payload)."""

    value: object
    available_at: datetime

    def available_by(self, decision_at: datetime) -> bool:
        return self.available_at <= decision_at


@dataclass(frozen=True)
class CandidateSnapshot:
    """Point-in-time candidate inputs.

    `contract` is the CONTRACT OBJECT: standardness is derived from it
    (flag + deliverable + corporate-action provenance), never from a
    caller-supplied boolean. `decision_at` must equal the calendar's close
    of `decision_session` (early closes included); the filter checks this
    itself rather than trusting the label.
    """

    contract: OptionContract
    underlying_security_id: str
    decision_session: date
    decision_at: datetime
    expiration: date
    abs_delta: AsOf | None
    open_interest: AsOf | None
    same_day_volume: AsOf | None
    same_day_volume_applicable: bool  # False: volume legitimately not yet published
    bid: AsOf | None
    ask: AsOf | None
    underlying_20d_median_dollar_volume: AsOf | None
    spans_earnings: AsOf | None

    @property
    def contract_id(self) -> str:
        return self.contract.contract_id


@dataclass(frozen=True)
class RuleResult:
    rule: str
    status: str
    detail: str


@dataclass(frozen=True)
class CandidateDecision:
    contract_id: str
    accepted: bool
    results: tuple[RuleResult, ...]

    def failed(self) -> tuple[RuleResult, ...]:
        return tuple(r for r in self.results if r.status in {FAIL, NOT_EVALUABLE})


def _tz_aware(ts: datetime) -> bool:
    return getattr(ts, "tzinfo", None) is not None


class CandidateFilter:
    def __init__(
        self,
        calendar,
        *,
        dte_min: int,
        dte_max: int,
        abs_delta_min: Decimal,
        abs_delta_max: Decimal,
        standard_deliverable_only: bool,
        min_open_interest: int,
        min_same_day_volume: int,
        volume_only_if_already_available: bool,
        max_spread_fraction_of_midpoint: Decimal,
        min_underlying_20d_median_dollar_volume: Decimal,
        exclude_earnings_spanning_hold: bool,
    ) -> None:
        self.calendar = calendar
        self.dte_min = dte_min
        self.dte_max = dte_max
        self.abs_delta_min = abs_delta_min
        self.abs_delta_max = abs_delta_max
        self.standard_deliverable_only = standard_deliverable_only
        self.min_open_interest = min_open_interest
        self.min_same_day_volume = min_same_day_volume
        self.volume_only_if_already_available = volume_only_if_already_available
        self.max_spread_fraction_of_midpoint = max_spread_fraction_of_midpoint
        self.min_underlying_20d_median_dollar_volume = min_underlying_20d_median_dollar_volume
        self.exclude_earnings_spanning_hold = exclude_earnings_spanning_hold

    @classmethod
    def from_protocol(cls, calendar, protocol: ResearchProtocol) -> CandidateFilter:
        d = protocol.option_candidate_defaults
        return cls(
            calendar,
            dte_min=d.dte_min,
            dte_max=d.dte_max,
            abs_delta_min=d.abs_delta_min,
            abs_delta_max=d.abs_delta_max,
            standard_deliverable_only=d.standard_deliverable_only,
            min_open_interest=d.min_open_interest,
            min_same_day_volume=d.min_same_day_volume,
            volume_only_if_already_available=d.volume_only_if_already_available,
            max_spread_fraction_of_midpoint=d.max_spread_fraction_of_midpoint,
            min_underlying_20d_median_dollar_volume=d.min_underlying_20d_median_dollar_volume,
            exclude_earnings_spanning_hold=d.exclude_earnings_spanning_hold,
        )

    def evaluate(self, snap: CandidateSnapshot) -> CandidateDecision:
        results: list[RuleResult] = []

        # Decision coherence FIRST: a post-close or naive decision instant
        # invalidates the whole evaluation — nothing downstream may run on a
        # mislabeled decision time. An unknown decision_session is the same
        # class of incoherence (the tri-state promise: NOT_EVALUABLE, not a
        # crash — review round 2, P2).
        coherent = True
        if not _tz_aware(snap.decision_at):
            coherent = False
            results.append(RuleResult("decision_coherence", NOT_EVALUABLE, "naive decision_at"))
        else:
            try:
                expected_close = self.calendar.session_close(snap.decision_session)
            except NotASessionError:
                coherent = False
                results.append(
                    RuleResult(
                        "decision_coherence",
                        NOT_EVALUABLE,
                        f"decision_session {snap.decision_session} not in calendar",
                    )
                )
            else:
                if snap.decision_at != expected_close:
                    coherent = False
                    results.append(
                        RuleResult(
                            "decision_coherence",
                            NOT_EVALUABLE,
                            f"decision_at {snap.decision_at} != session close {expected_close}",
                        )
                    )

        # Contract coherence: the snapshot must describe the contract it
        # carries — duplicated expiration/underlier fields that disagree with
        # the CONTRACT OBJECT make every rule unevaluable (the contract is
        # authoritative; the duplicates exist only for audit convenience).
        if snap.expiration != snap.contract.expiration or snap.underlying_security_id != (
            snap.contract.underlying_security_id
        ):
            coherent = False
            results.append(
                RuleResult(
                    "contract_coherence",
                    NOT_EVALUABLE,
                    f"snapshot expiration/underlier disagree with contract "
                    f"{snap.contract.contract_id}",
                )
            )
        if not coherent:
            # Nothing is evaluable at a mislabeled decision time: fail closed
            # for every rule rather than comparing against a bad instant.
            rule_names = [
                "dte",
                "delta",
                "deliverable",
                "open_interest",
                "same_day_volume",
                "spread",
                "underlying_liquidity",
                "earnings_span",
            ]
            results.extend(
                RuleResult(r, NOT_EVALUABLE, "decision instant incoherent") for r in rule_names
            )
            return CandidateDecision(
                contract_id=snap.contract_id, accepted=False, results=tuple(results)
            )

        # DTE: calendar days (explicit convention), derived from the
        # CONTRACT's expiration — never the caller-duplicated snapshot field.
        dte = (snap.contract.expiration - snap.decision_session).days
        if self.dte_min <= dte <= self.dte_max:
            results.append(RuleResult("dte", PASS, f"dte {dte}"))
        else:
            results.append(
                RuleResult("dte", FAIL, f"dte {dte} not in [{self.dte_min}, {self.dte_max}]")
            )

        # Delta.
        if snap.abs_delta is None:
            results.append(RuleResult("delta", NOT_EVALUABLE, "abs_delta missing"))
        elif not _tz_aware(snap.abs_delta.available_at):
            results.append(RuleResult("delta", NOT_EVALUABLE, "naive timestamp"))
        elif not snap.abs_delta.available_by(snap.decision_at):
            results.append(RuleResult("delta", NOT_EVALUABLE, "future-available delta"))
        elif not (self.abs_delta_min <= _v(snap.abs_delta) <= self.abs_delta_max):
            results.append(RuleResult("delta", FAIL, f"{_v(snap.abs_delta)} out of band"))
        else:
            results.append(RuleResult("delta", PASS, "in band"))

        # Deliverable standardness, derived from the CONTRACT OBJECT (never a
        # caller boolean): standard == flag set AND no corporate-action
        # provenance AND schema-validated 100-share deliverable.
        if not self.standard_deliverable_only:
            results.append(RuleResult("deliverable", NOT_APPLICABLE, "filter disabled"))
        elif (
            snap.contract.standard_contract_flag
            and snap.contract.corporate_action_id is None
            and snap.contract.deliverable.shares_per_contract == Decimal("100")
        ):
            results.append(RuleResult("deliverable", PASS, "standard 100 shares"))
        else:
            results.append(
                RuleResult(
                    "deliverable",
                    FAIL,
                    f"nonstandard/adjusted deliverable rejected ({snap.contract.contract_id})",
                )
            )

        # Open interest.
        if snap.open_interest is None:
            results.append(RuleResult("open_interest", NOT_EVALUABLE, "missing"))
        elif not _tz_aware(snap.open_interest.available_at):
            results.append(RuleResult("open_interest", NOT_EVALUABLE, "naive timestamp"))
        elif not snap.open_interest.available_by(snap.decision_at):
            results.append(RuleResult("open_interest", NOT_EVALUABLE, "future-available"))
        elif _v(snap.open_interest) < self.min_open_interest:
            results.append(RuleResult("open_interest", FAIL, "below min"))
        else:
            results.append(RuleResult("open_interest", PASS, "above min"))

        # Same-day volume: optional rule — unavailable-but-applicable is
        # recorded NOT_APPLICABLE (never silently skipped, never fabricated).
        # A SUPPLIED volume is always evaluated — the applicability flag only
        # excuses a missing one (F4: the flag must not hide a future input).
        if snap.same_day_volume is not None:
            if not _tz_aware(snap.same_day_volume.available_at):
                results.append(RuleResult("same_day_volume", NOT_EVALUABLE, "naive timestamp"))
            elif not snap.same_day_volume.available_by(snap.decision_at):
                results.append(
                    RuleResult("same_day_volume", NOT_EVALUABLE, "future-available volume")
                )
            elif _v(snap.same_day_volume) < self.min_same_day_volume:
                results.append(RuleResult("same_day_volume", FAIL, "below min"))
            else:
                results.append(RuleResult("same_day_volume", PASS, "above min"))
        elif self.volume_only_if_already_available and not snap.same_day_volume_applicable:
            results.append(
                RuleResult("same_day_volume", NOT_APPLICABLE, "not yet published at decision")
            )
        else:
            results.append(RuleResult("same_day_volume", NOT_EVALUABLE, "missing"))

        # Spread fraction of midpoint.
        if snap.bid is None or snap.ask is None:
            results.append(RuleResult("spread", NOT_EVALUABLE, "bid/ask missing"))
        elif not _tz_aware(snap.bid.available_at) or not _tz_aware(snap.ask.available_at):
            results.append(RuleResult("spread", NOT_EVALUABLE, "naive quote timestamp"))
        elif not snap.bid.available_by(snap.decision_at) or not snap.ask.available_by(
            snap.decision_at
        ):
            results.append(RuleResult("spread", NOT_EVALUABLE, "future-available quote"))
        else:
            bid_v, ask_v = _v(snap.bid), _v(snap.ask)
            if bid_v > ask_v:
                results.append(RuleResult("spread", NOT_EVALUABLE, f"crossed {bid_v}/{ask_v}"))
            else:
                mid = (bid_v + ask_v) / 2
                if mid <= 0:
                    results.append(RuleResult("spread", NOT_EVALUABLE, f"midpoint {mid}"))
                else:
                    fraction = (ask_v - bid_v) / mid
                    if fraction > self.max_spread_fraction_of_midpoint:
                        results.append(RuleResult("spread", FAIL, f"{fraction:.4f} exceeds limit"))
                    else:
                        results.append(RuleResult("spread", PASS, f"{fraction:.4f}"))

        # Underlying liquidity.
        if snap.underlying_20d_median_dollar_volume is None:
            results.append(RuleResult("underlying_liquidity", NOT_EVALUABLE, "missing"))
        elif not _tz_aware(snap.underlying_20d_median_dollar_volume.available_at):
            results.append(RuleResult("underlying_liquidity", NOT_EVALUABLE, "naive timestamp"))
        elif not snap.underlying_20d_median_dollar_volume.available_by(snap.decision_at):
            results.append(RuleResult("underlying_liquidity", NOT_EVALUABLE, "future-available"))
        elif (
            _v(snap.underlying_20d_median_dollar_volume)
            < self.min_underlying_20d_median_dollar_volume
        ):
            results.append(RuleResult("underlying_liquidity", FAIL, "below min"))
        else:
            results.append(RuleResult("underlying_liquidity", PASS, "above min"))

        # Earnings spanning hold.
        if not self.exclude_earnings_spanning_hold:
            results.append(RuleResult("earnings_span", NOT_APPLICABLE, "filter disabled"))
        elif snap.spans_earnings is None:
            results.append(RuleResult("earnings_span", NOT_EVALUABLE, "missing"))
        elif not _tz_aware(snap.spans_earnings.available_at):
            results.append(RuleResult("earnings_span", NOT_EVALUABLE, "naive timestamp"))
        elif not snap.spans_earnings.available_by(snap.decision_at):
            results.append(RuleResult("earnings_span", NOT_EVALUABLE, "future-available"))
        elif _v(snap.spans_earnings):
            results.append(RuleResult("earnings_span", FAIL, "earnings span the hold"))
        else:
            results.append(RuleResult("earnings_span", PASS, "no spanning earnings"))

        accepted = not any(r.status in {FAIL, NOT_EVALUABLE} for r in results)
        return CandidateDecision(
            contract_id=snap.contract_id, accepted=accepted, results=tuple(results)
        )


def _v(as_of: AsOf):
    return as_of.value
