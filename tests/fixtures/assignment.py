"""Early-assignment candidate fixture (audit §5.3).

M0 has NO assignment engine and short option legs are prohibited; this
fixture pins the CANDIDATE shape and an explicit deferred/nonclaim record so
post-M0 work inherits a tested starting point instead of a blank page.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from tests.fixtures.contracts import standard_call


@dataclass(frozen=True)
class EarlyAssignmentCandidate:
    contract_id: str
    underlying_security_id: str
    call_put: str
    exercise_style: str
    strike: Decimal
    spot_at_decision: Decimal
    expiration: date
    itm: bool
    claim: str  # always DEFERRED_TO_POST_M0 in M0
    note: str


def early_assignment_candidate() -> EarlyAssignmentCandidate:
    contract = standard_call()  # American call, strike 50
    spot = Decimal("62.30")
    return EarlyAssignmentCandidate(
        contract_id=contract.contract_id,
        underlying_security_id=contract.underlying_security_id,
        call_put=contract.call_put,
        exercise_style=contract.exercise_style,
        strike=contract.strike,
        spot_at_decision=spot,
        expiration=contract.expiration,
        itm=spot > contract.strike,
        claim="DEFERRED_TO_POST_M0",
        note=(
            "no assignment engine in M0; short option legs are structurally "
            "prohibited (Order side/intent pairing), so early assignment has "
            "no live target until post-M0 short-leg support"
        ),
    )
