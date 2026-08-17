"""Option contract fixtures: standard call, ITM-at-expiration, split-adjusted.

Fixture 5 (handoff §7): an ITM American call through expiration — tradable ON
the expiration session, dead after (early assignment of SHORT legs is out of
scope in M0 because short options are structurally unrepresentable).
Fixture 4 (split-adjusted deliverable) is consumed by the corporate-action
tests but the builder lives here next to its contract.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from tree_options.schemas.options import (
    CorporateActionRecord,
    DeliverableSpec,
    OptionContract,
)


def standard_call(
    *,
    contract_id: str = "OPT-C-2024-06-21-50",
    underlying: str = "SEC-001",
    expiration: date = date(2024, 6, 21),
    strike: str = "50.00",
    listing_start: date = date(2024, 1, 2),
) -> OptionContract:
    return OptionContract(
        contract_id=contract_id,
        option_root="OPT",
        underlying_security_id=underlying,
        expiration=expiration,
        strike=Decimal(strike),
        call_put="C",
        multiplier=100,
        exercise_style="american",
        listing_start=listing_start,
        listing_end=expiration,
        deliverable=DeliverableSpec(
            shares_per_contract=Decimal("100"),
        ),
        standard_contract_flag=True,
    )


def itm_call_at_expiration() -> tuple[OptionContract, date, date]:
    """Fixture 5: ITM call with known expiration and the session after it.

    Underlier at 62.30 vs strike 50: deep ITM at expiration. Returns
    (contract, expiration_session, next_session) — the caller supplies a
    calendar to verify next_session is really the following session.
    """
    contract = standard_call(
        contract_id="OPT-C-2024-04-19-50",
        expiration=date(2024, 4, 19),
        strike="50.00",
        listing_start=date(2024, 1, 2),
    )
    return contract, date(2024, 4, 19), date(2024, 4, 22)


def split_adjusted_contract() -> tuple[OptionContract, CorporateActionRecord]:
    """Fixture 4: 3-for-2 split turns a 100-share contract into 150.

    The nonstandard deliverable MUST trace to its corporate action (INV-08/09
    join): a contract that quietly delivers 150 shares without provenance is
    exactly the silent-corruption case the schema rejects.
    """
    action = CorporateActionRecord(
        corporate_action_id="CA-001",
        security_id="SEC-001",
        action_type="split",
        effective_session=date(2024, 3, 15),
        ratio=Decimal("1.5"),
        available_at=datetime(2024, 3, 14, 21, 0, tzinfo=UTC),
    )
    contract = OptionContract(
        contract_id="OPT-C-2024-06-21-50-ADJ",
        option_root="OPT",
        underlying_security_id="SEC-001",
        expiration=date(2024, 6, 21),
        strike=Decimal("33.33"),
        call_put="C",
        multiplier=100,
        exercise_style="american",
        listing_start=date(2024, 1, 2),
        listing_end=date(2024, 6, 21),
        deliverable=DeliverableSpec(
            shares_per_contract=Decimal("150"),
            underlier_per_share_terms="150 shares per contract after 3-for-2 split",
            corporate_action_id="CA-001",
        ),
        standard_contract_flag=False,
        corporate_action_id="CA-001",
    )
    return contract, action
