"""Fee models (§8): per-contract commissions with an order minimum.

M0 constants are PLACEHOLDERS recorded in docs/m0-evidence.md remaining
decisions: per-contract $0.65, per-order minimum $1.00, no regulatory/markout
line items yet. The model is a seam — swap the implementation, not the callers.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Protocol

from tree_options.schemas.common import FEE_TICK


class FeeModel(Protocol):
    def order_fees(self, quantity: int) -> Decimal:
        """Total fees for one filled order of `quantity` contracts."""
        ...


class PerContractFeeModel:
    def __init__(
        self,
        fee_per_contract: Decimal = Decimal("0.65"),
        minimum_per_order: Decimal = Decimal("1.00"),
    ) -> None:
        if fee_per_contract < 0 or minimum_per_order < 0:
            raise ValueError("fee parameters must be >= 0")
        self.fee_per_contract = fee_per_contract
        self.minimum_per_order = minimum_per_order

    def order_fees(self, quantity: int) -> Decimal:
        if quantity < 1:
            raise ValueError(f"quantity must be >= 1, got {quantity}")
        raw = self.fee_per_contract * quantity
        return max(raw, self.minimum_per_order).quantize(FEE_TICK)
