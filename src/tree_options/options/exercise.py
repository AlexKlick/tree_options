"""Early-exercise election policy (M3 plan §3.C, owner decision 2).

The election happens at the 10:00 ET window of session t and consumes
ONLY file(t-1) facts plus actions visible by then — never file(t), never
the session-t close (a file(t)-only signal must not trigger an election;
this is mutant-guarded). The settlement itself strikes at close(t), with
cash knowable only at that bar's publication instant.

Pre-declared rule — elect iff either branch fires:
(a) CALL: a cash dividend on the underlying is visible with effective
    session in (t, expiration] AND dividend/share >= the file(t-1) time
    value (mid premium - intrinsic); or
(b) ANY: the file(t-1) bid < intrinsic * 0.98 — the market pays less
    than 98% of intrinsic, so exercising beats selling (the classic
    deep-ITM zero-bid case).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

EXERCISE_DISCOUNT = Decimal("0.98")


@dataclass(frozen=True)
class ExerciseElectionInputs:
    """Everything the policy may see — all of it file(t-1) or earlier."""

    exercise_style: str
    call_put: str
    expiration_seen: bool  # the contract's expiration is known from file(t-1)
    mid_premium: Decimal  # file(t-1) mid premium for the contract
    bid: Decimal  # file(t-1) EOD bid for the contract
    intrinsic: Decimal  # intrinsic at the file(t-1) underlying mid
    pending_dividend_per_share: Decimal | None  # visible, effective in (t, expiration]


def should_elect_exercise(inputs: ExerciseElectionInputs) -> bool:
    if inputs.exercise_style != "american":
        return False  # no early exercise right (european fixtures exercise only at expiry)
    if not inputs.expiration_seen:
        return False
    time_value = max(inputs.mid_premium - inputs.intrinsic, Decimal("0"))
    dividend_dominates = (
        inputs.call_put == "C"
        and inputs.pending_dividend_per_share is not None
        and inputs.pending_dividend_per_share >= time_value
    )
    market_underpays_intrinsic = inputs.intrinsic > 0 and inputs.bid < (
        inputs.intrinsic * EXERCISE_DISCOUNT
    )
    return dividend_dominates or market_underpays_intrinsic
