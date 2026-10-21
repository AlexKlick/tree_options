"""C2.1: ``supervised-equity-effect/1`` and the ``EffectV1`` union.

The equity effect is the one whole-share, long-only stock order a per-trade
mandate may send, with the exit policy the desk (not the broker) enforces.
Like the option ``supervised-effect/1`` (``trex.supervised_ibkr``), the
permit hashes its canonical bytes and the IBKR adapter builds the order
FROM those bytes, so every order attribute that reaches the wire is a
hashed field here (``tif``, ``outside_rth``, ``currency``, ``exchange``
are fixed literals rather than adapter defaults).

``EffectV1`` discriminates on the ``schema`` key. The option member is the
EXISTING ``SupervisedEffect`` class, imported, never redefined: its bytes
and hash are produced by the existing ``effect_bytes`` (pinned by the
golden test in ``tests/contracts``).

Money is integer cents: an order limit is always on a penny tick.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Annotated, Literal

from pydantic import Field, TypeAdapter, model_validator

from tree_options.schemas.common import IdStr, StrictModel
from tree_options.trex.contracts.canonical import (
    PathSafeId,
    Symbol,
    model_bytes,
    sha256_hex,
)
from tree_options.trex.contracts.defaults import (
    DEFAULT_STOP_CONFIRM_TICKS,
    EQUITY_GAP_RESERVE_BPS,
)
from tree_options.trex.supervised_ibkr import (
    PAPER_ACCOUNT_PREFIX,
    SupervisedEffect,
    effect_bytes,
    supervised_order_ref,
)

__all__ = [
    "EFFECT_V1_ADAPTER",
    "EQUITY_EFFECT_SCHEMA",
    "OPTION_EFFECT_SCHEMA",
    "EffectV1",
    "EquityExitPolicy",
    "SupervisedEffect",
    "SupervisedEquityEffect",
    "decode_effect_v1",
    "effect_v1_bytes",
    "effect_v1_sha256",
    "equity_max_loss_cents",
    "equity_notional_cents",
    "equity_stop_loss_cents",
]

OPTION_EFFECT_SCHEMA = "supervised-effect/1"
EQUITY_EFFECT_SCHEMA = "supervised-equity-effect/1"


class EquityExitPolicy(StrictModel):
    """How the desk closes the position; every exit is a desk-sent SELL.

    No resting broker stop or target is placed: the desk watches the quote
    and sends a DAY limit sell when a rule fires.

    - ``stop_price_cents``: sell once the bid prints at or below it for
      ``stop_confirm_ticks`` consecutive quote ticks;
    - ``target_price_cents`` (optional): sell at or above it;
    - ``exit_deadline_session``: the NYSE session by which the position
      is closed at the latest;
    - ``forced_exit_et``: wall time (America/New_York) on the deadline
      session after which the desk closes at marketable prices.
    """

    stop_price_cents: int = Field(strict=True, gt=0)
    target_price_cents: Annotated[int, Field(strict=True, gt=0)] | None = None
    exit_deadline_session: date
    stop_confirm_ticks: int = Field(default=DEFAULT_STOP_CONFIRM_TICKS, strict=True, ge=1, le=20)
    forced_exit_et: Literal["15:45"] = "15:45"


class SupervisedEquityEffect(StrictModel):
    """The one whole-share, long-only stock order a per-trade mandate authorizes."""

    schema_version: Literal["supervised-equity-effect/1"] = Field(
        default="supervised-equity-effect/1", alias="schema"
    )
    intent_id: PathSafeId
    account_id: IdStr
    symbol: Symbol
    con_id: int = Field(strict=True, ge=1)
    side: Literal["BUY"]
    quantity: int = Field(strict=True, ge=1)
    limit_cents: int = Field(strict=True, gt=0)
    tif: Literal["DAY"] = "DAY"
    outside_rth: Literal[False] = False
    currency: Literal["USD"] = "USD"
    exchange: Literal["SMART"] = "SMART"
    order_ref: str
    exit_policy: EquityExitPolicy

    @model_validator(mode="after")
    def _bind(self) -> SupervisedEquityEffect:
        if self.order_ref != supervised_order_ref(self.intent_id):
            raise ValueError("order_ref must be the intent's supervised tag")
        if not self.account_id.startswith(PAPER_ACCOUNT_PREFIX):
            raise ValueError("the supervised path binds IBKR paper accounts only")
        policy = self.exit_policy
        if policy.stop_price_cents >= self.limit_cents:
            raise ValueError("stop_price_cents must be below limit_cents")
        if policy.target_price_cents is not None and policy.target_price_cents <= self.limit_cents:
            raise ValueError("target_price_cents must be above limit_cents")
        return self


#: Discriminated on the ``schema`` key; an absent or unknown schema refuses.
EffectV1 = Annotated[
    SupervisedEffect | SupervisedEquityEffect, Field(discriminator="schema_version")
]
EFFECT_V1_ADAPTER: TypeAdapter[SupervisedEffect | SupervisedEquityEffect] = TypeAdapter(EffectV1)


def effect_v1_bytes(effect: SupervisedEffect | SupervisedEquityEffect) -> bytes:
    """The canonical bytes a permit hashes; the option arm is the EXISTING builder."""
    if isinstance(effect, SupervisedEffect):
        return effect_bytes(effect)
    return model_bytes(effect)


def effect_v1_sha256(effect: SupervisedEffect | SupervisedEquityEffect) -> str:
    return sha256_hex(effect_v1_bytes(effect))


def decode_effect_v1(payload: bytes) -> SupervisedEffect | SupervisedEquityEffect:
    """Parse either effect, refusing any non-canonical encoding."""
    effect = EFFECT_V1_ADAPTER.validate_python(json.loads(payload))
    if effect_v1_bytes(effect) != payload:
        raise ValueError("effect payload is not the canonical encoding")
    return effect


# -- equity risk arithmetic (integer cents, shared by every lane) ----------


def equity_notional_cents(effect: SupervisedEquityEffect) -> int:
    """Cash the entry needs at its limit: quantity x limit."""
    return effect.quantity * effect.limit_cents


def equity_stop_loss_cents(effect: SupervisedEquityEffect) -> int:
    """Loss if the desk exits exactly at the stop: quantity x (limit - stop)."""
    return effect.quantity * (effect.limit_cents - effect.exit_policy.stop_price_cents)


def equity_max_loss_cents(
    effect: SupervisedEquityEffect, gap_reserve_bps: int = EQUITY_GAP_RESERVE_BPS
) -> int:
    """The loss reserved for an equity entry: the stop loss, or the gap
    reserve (ceil(notional x bps / 10000)) when that is larger."""
    if not 0 <= gap_reserve_bps <= 10_000:
        raise ValueError("gap_reserve_bps must be within [0, 10000]")
    gap = -(-equity_notional_cents(effect) * gap_reserve_bps // 10_000)
    return max(equity_stop_loss_cents(effect), gap)
