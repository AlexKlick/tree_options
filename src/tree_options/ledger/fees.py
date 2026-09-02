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
    # (theory wave-0, D6) the ONE source of the default cost constants: the
    # backtest instantiates with defaults and the lane-2 payload STAMPS the
    # same values, so an artifact names its fee model without re-reading
    # this module
    DEFAULT_FEE_PER_CONTRACT = Decimal("0.65")
    DEFAULT_MINIMUM_PER_ORDER = Decimal("1.00")

    def __init__(
        self,
        fee_per_contract: Decimal = DEFAULT_FEE_PER_CONTRACT,
        minimum_per_order: Decimal = DEFAULT_MINIMUM_PER_ORDER,
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
