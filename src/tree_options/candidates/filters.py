"""Option candidate filters (§9.2, handoff §14 item 8).

An AUDIT filter, not a first-failure gate: every predicate is evaluated and
every rejection is reported. Missing required inputs reject DATA_NOT_EVALUABLE
(never silently include a candidate you could not check). The same-day volume
predicate applies only when the volume datum is already available at decision
time — when unavailable it is skipped, never fabricated.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from tree_options.protocol.schema import ResearchProtocol


@dataclass(frozen=True)
class CandidateSnapshot:
    contract_id: str
    underlying_security_id: str
    decision_session: date
    expiration: date
    abs_delta: Decimal | None
    open_interest: int | None
    same_day_volume: int | None
    same_day_volume_available: bool
    bid: Decimal | None
    ask: Decimal | None
    standard_contract: bool
    underlying_20d_median_dollar_volume: Decimal | None
    spans_earnings: bool | None


@dataclass(frozen=True)
class Rejection:
    code: str
    field: str
    detail: str


@dataclass(frozen=True)
class CandidateDecision:
    contract_id: str
    accepted: bool
    rejections: tuple[Rejection, ...]


class CandidateFilter:
    def __init__(
        self,
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
    def from_protocol(cls, protocol: ResearchProtocol) -> CandidateFilter:
        d = protocol.option_candidate_defaults
        return cls(
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
        rejections: list[Rejection] = []

        # DTE in calendar days (market convention for "days to expiration").
        dte = (snap.expiration - snap.decision_session).days
        if not (self.dte_min <= dte <= self.dte_max):
            rejections.append(
                Rejection("DTE_OUT_OF_RANGE", "expiration", f"dte {dte} not in [{self.dte_min}, {self.dte_max}]")
            )

        if snap.abs_delta is None:
            rejections.append(Rejection("DATA_NOT_EVALUABLE", "abs_delta", "abs_delta missing"))
        elif not (self.abs_delta_min <= snap.abs_delta <= self.abs_delta_max):
            rejections.append(
                Rejection(
                    "DELTA_OUT_OF_RANGE", "abs_delta",
                    f"{snap.abs_delta} not in [{self.abs_delta_min}, {self.abs_delta_max}]",
                )
            )

        if self.standard_deliverable_only and not snap.standard_contract:
            rejections.append(
                Rejection("NONSTANDARD_DELIVERABLE", "standard_contract", "nonstandard deliverable excluded")
            )

        if snap.open_interest is None:
            rejections.append(Rejection("DATA_NOT_EVALUABLE", "open_interest", "open_interest missing"))
        elif snap.open_interest < self.min_open_interest:
            rejections.append(
                Rejection("OPEN_INTEREST_BELOW_MIN", "open_interest", f"{snap.open_interest} < {self.min_open_interest}")
            )

        # Same-day volume: applied only when the datum is already available.
        if self.volume_only_if_already_available:
            if snap.same_day_volume_available:
                if snap.same_day_volume is None:
                    rejections.append(
                        Rejection("DATA_NOT_EVALUABLE", "same_day_volume", "flagged available but value missing")
                    )
                elif snap.same_day_volume < self.min_same_day_volume:
                    rejections.append(
                        Rejection(
                            "VOLUME_BELOW_MIN", "same_day_volume",
                            f"{snap.same_day_volume} < {self.min_same_day_volume}",
                        )
                    )
        elif snap.same_day_volume is not None and snap.same_day_volume < self.min_same_day_volume:
            rejections.append(
                Rejection("VOLUME_BELOW_MIN", "same_day_volume", f"{snap.same_day_volume} < {self.min_same_day_volume}")
            )

        if snap.bid is None:
            rejections.append(Rejection("DATA_NOT_EVALUABLE", "bid", "bid missing"))
        elif snap.ask is None:
            rejections.append(Rejection("DATA_NOT_EVALUABLE", "ask", "ask missing"))
        elif snap.bid > snap.ask:
            rejections.append(Rejection("DATA_NOT_EVALUABLE", "quote", f"crossed inputs bid={snap.bid} ask={snap.ask}"))
        else:
            mid = (snap.bid + snap.ask) / 2
            if mid <= 0:
                rejections.append(Rejection("DATA_NOT_EVALUABLE", "quote", f"non-positive midpoint {mid}"))
            else:
                fraction = (snap.ask - snap.bid) / mid
                if fraction > self.max_spread_fraction_of_midpoint:
                    rejections.append(
                        Rejection(
                            "SPREAD_FRACTION_EXCEEDS", "quote",
                            f"{fraction:.4f} > {self.max_spread_fraction_of_midpoint}",
                        )
                    )

        if snap.underlying_20d_median_dollar_volume is None:
            rejections.append(
                Rejection("DATA_NOT_EVALUABLE", "underlying_20d_median_dollar_volume", "missing")
            )
        elif snap.underlying_20d_median_dollar_volume < self.min_underlying_20d_median_dollar_volume:
            rejections.append(
                Rejection(
                    "DOLLAR_VOLUME_BELOW_MIN", "underlying_20d_median_dollar_volume",
                    f"{snap.underlying_20d_median_dollar_volume} < {self.min_underlying_20d_median_dollar_volume}",
                )
            )

        if self.exclude_earnings_spanning_hold:
            if snap.spans_earnings is None:
                rejections.append(Rejection("DATA_NOT_EVALUABLE", "spans_earnings", "missing"))
            elif snap.spans_earnings:
                rejections.append(
                    Rejection("EARNINGS_SPAN_HOLD", "spans_earnings", "earnings span the holding window")
                )

        return CandidateDecision(
            contract_id=snap.contract_id,
            accepted=not rejections,
            rejections=tuple(rejections),
        )
