"""Order, Fill, Position (§9.1/§9.4).

M0 freeze: long options only. `sell_to_open` is structurally absent — a naked
short is not representable (short legs arrive post-M0 with assignment logic).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import Field, model_validator

from tree_options.schemas.common import IdStr, Money, Price, StrictModel, UTCDatetime


class NakedShortProhibitedError(RuntimeError):
    pass


class Order(StrictModel):
    order_id: IdStr
    contract_id: IdStr
    side: Literal["buy", "sell"]
    intent: Literal["open_long", "close_long", "sell_to_open"]
    quantity: int = Field(ge=1)
    order_type: Literal["market", "limit"] = "market"
    limit_price: Price | None = None
    decision_at: UTCDatetime
    decision_session: date

    @model_validator(mode="after")
    def _checks(self) -> Order:
        if self.intent == "sell_to_open" or (self.side, self.intent) not in {
            ("buy", "open_long"),
            ("sell", "close_long"),
        }:
            raise NakedShortProhibitedError(
                f"side/intent ({self.side}, {self.intent}) not permitted; "
                "M0 allows buy-to-open and sell-to-close only"
            )
        if self.order_type == "limit" and self.limit_price is None:
            raise ValueError("limit order requires limit_price")
        if self.order_type == "market" and self.limit_price is not None:
            raise ValueError("market order must not carry limit_price")
        return self


class Fill(StrictModel):
    fill_id: IdStr
    order_id: IdStr
    contract_id: IdStr
    side: Literal["buy", "sell"]
    quantity: int = Field(ge=1)
    price: Price
    fees: Money = Field(ge=0)
    execution_at: UTCDatetime
    execution_session: date
    price_improvement_fraction: Decimal = Field(default=Decimal("0"), ge=0, le=Decimal("0.5"))

    def notional(self) -> Money:
        return (self.price * self.quantity).quantize(Decimal("0.01"))

    def signed_cash(self) -> Money:
        sign = -1 if self.side == "buy" else 1
        return (sign * self.price * self.quantity).quantize(Decimal("0.01"))


class Position(StrictModel):
    contract_id: IdStr
    quantity: int = Field(ge=0)  # long-only M0: negative impossible by type
    average_cost: Price
    opened_session: date

    def is_open(self) -> bool:
        return self.quantity > 0
