"""C2.4: broker facts, the append-only ledger, holdings and attribution.

Units. Orders, limits and risk numbers are integer CENTS (we only send
penny-tick limits). Broker-REPORTED execution prices, average costs,
commissions and ledger cash deltas are integer MICROS (1 USD = 1_000_000):
IBKR reports sub-penny execution prices (price improvement, midpoint
fills) and fractional-cent commissions, and the ledger must sum exactly.

Execution ids. IBKR execution ids look like ``0000e0d5.6576d6a5.01.01``;
a correction re-reports the same execution with the last dot segment
incremented (``...01.02``). ``base_exec_id`` = the id up to (not
including) its LAST ``.``; an id without a dot is its own base. Two
executions with the same base are the same fill: the later one corrects
the earlier.

Scope. Executions and positions carry only the security types Wave 2
trades (``OPT``, ``STK``; ``BAG`` combo-level executions are evidence
only, never ledger fills: the legs are). A broker row of any other type
cannot be represented, and the producer must surface the view as
``unavailable`` or ``reconciliation_required``, never drop it. Whole
shares only: a fractional quantity fails validation the same way.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import Field, model_validator

from tree_options.schemas.common import IdStr, StrictModel
from tree_options.trex.contracts.canonical import PathSafeId, Sha256Hex, UTCInstant
from tree_options.trex.ibkr import ORDER_REF_PREFIX
from tree_options.trex.supervised_ibkr import SUPERVISED_REF_PREFIX

__all__ = [
    "DESK_REF_PREFIX",
    "LEDGER_EVENT_SCHEMA",
    "UNASSIGNED_SLEEVE",
    "AttributedQuantity",
    "Attribution",
    "BrokerExecution",
    "BrokerPortfolioItem",
    "Holding",
    "LedgerEvent",
    "OrderRefOwner",
    "base_exec_id_of",
    "desk_order_ref",
    "parse_order_ref",
]

LEDGER_EVENT_SCHEMA = "trex-ledger-event/1"
UNASSIGNED_SLEEVE = "unassigned"
#: The desk runtime's exit tag (``desk_runtime.DESK_REF_PREFIX``).
DESK_REF_PREFIX = ORDER_REF_PREFIX + "desk:"

ExecSecType = Literal["OPT", "STK", "BAG"]
PositionSecType = Literal["OPT", "STK"]
PositiveInt = Annotated[int, Field(strict=True, ge=1)]
NonZeroInt = Annotated[int, Field(strict=True)]


def base_exec_id_of(exec_id: str) -> str:
    """The execution id without its correction suffix (see module doc)."""
    head, dot, _ = exec_id.rpartition(".")
    return head if dot and head else exec_id


def desk_order_ref(position_id: str) -> str:
    """The exit tag the desk puts on every close of ``position_id``."""
    return DESK_REF_PREFIX + position_id


@dataclass(frozen=True)
class OrderRefOwner:
    """What a trex order tag names: a supervised entry or a desk exit."""

    kind: Literal["sup", "desk"]
    ident: str


def parse_order_ref(order_ref: str | None) -> OrderRefOwner | None:
    """``trex:sup:<intent_id>`` / ``trex:desk:<structure_id>``; else None."""
    if not order_ref:
        return None
    if order_ref.startswith(SUPERVISED_REF_PREFIX) and order_ref != SUPERVISED_REF_PREFIX:
        return OrderRefOwner(kind="sup", ident=order_ref.removeprefix(SUPERVISED_REF_PREFIX))
    if order_ref.startswith(DESK_REF_PREFIX) and order_ref != DESK_REF_PREFIX:
        return OrderRefOwner(kind="desk", ident=order_ref.removeprefix(DESK_REF_PREFIX))
    return None


class BrokerExecution(StrictModel):
    """One IBKR execution as reported (``execDetails`` + ``commissionReport``)."""

    exec_id: IdStr
    base_exec_id: IdStr
    perm_id: PositiveInt
    order_ref: str
    account_id: IdStr
    con_id: PositiveInt
    sec_type: ExecSecType
    symbol: IdStr
    side: Literal["BOT", "SLD"]
    quantity: PositiveInt
    price_micros: int = Field(strict=True, ge=0)
    multiplier: PositiveInt
    time: UTCInstant
    #: None until the separate commission report arrives
    commission_micros: Annotated[int, Field(strict=True)] | None = None

    @model_validator(mode="after")
    def _base(self) -> BrokerExecution:
        if self.base_exec_id != base_exec_id_of(self.exec_id):
            raise ValueError("base_exec_id must be exec_id without its last '.' segment")
        if self.sec_type == "STK" and self.multiplier != 1:
            raise ValueError("a stock execution has multiplier 1")
        return self


class BrokerPortfolioItem(StrictModel):
    """One broker position row (``reqPositions`` / ``portfolio``)."""

    account_id: IdStr
    con_id: PositiveInt
    sec_type: PositionSecType
    symbol: IdStr
    #: signed: option short legs are negative; 0 rows appear after a close
    position: Annotated[int, Field(strict=True)]
    #: IBKR avgCost (per share; per contract including the multiplier)
    avg_cost_micros: Annotated[int, Field(strict=True)]
    market_price_micros: Annotated[int, Field(strict=True, ge=0)] | None = None
    market_value_cents: Annotated[int, Field(strict=True)] | None = None
    as_of: UTCInstant


class Attribution(StrictModel):
    """Whose a ledger row is. ``intent_id`` None = not ours (outside)."""

    intent_id: PathSafeId | None
    strategy_version: IdStr | None
    program_id: IdStr | None
    sleeve_ref: IdStr = UNASSIGNED_SLEEVE

    @model_validator(mode="after")
    def _ours_or_outside(self) -> Attribution:
        ours = self.intent_id is not None
        if ours != (self.strategy_version is not None) or ours != (self.program_id is not None):
            raise ValueError(
                "intent_id, strategy_version and program_id are all set (ours) or all None"
            )
        return self


LedgerKind = Literal["fill", "correction", "commission", "cash_adjustment", "reconciliation"]


class LedgerEvent(StrictModel):
    """One append-only ledger row (``ledger/events.jsonl``).

    The ledger is a pure sum: position(con_id) = sum of ``quantity_delta``,
    cash = sum of ``cash_delta_micros``. A ``correction`` therefore carries
    the ADJUSTMENT (corrected minus superseded), and names the event it
    supersedes. ``event_id`` is deterministic, so replaying the same broker
    fact twice yields the same id (dedupe on append):
    ``fill:<exec_id>``, ``correction:<exec_id>``, ``commission:<exec_id>``,
    ``cash:<source id>``, ``reconciliation:<ref_sha256>``.
    """

    schema_version: Literal["trex-ledger-event/1"] = Field(
        default="trex-ledger-event/1", alias="schema"
    )
    event_id: IdStr
    kind: LedgerKind
    account_id: IdStr
    at: UTCInstant
    recorded_at: UTCInstant
    attribution: Attribution
    quantity_delta: int = Field(strict=True)
    cash_delta_micros: int = Field(strict=True)
    con_id: PositiveInt | None = None
    sec_type: PositionSecType | None = None
    exec_id: IdStr | None = None
    base_exec_id: IdStr | None = None
    perm_id: PositiveInt | None = None
    order_ref: str | None = None
    supersedes_event_id: IdStr | None = None
    ref_sha256: Sha256Hex | None = None
    detail: str = ""

    @model_validator(mode="after")
    def _kind_shape(self) -> LedgerEvent:
        kind = self.kind
        if kind in ("fill", "correction", "commission"):
            if self.exec_id is None or self.base_exec_id != base_exec_id_of(self.exec_id):
                raise ValueError(f"a {kind} carries exec_id and its base_exec_id")
            if self.event_id != f"{kind}:{self.exec_id}":
                raise ValueError(f"a {kind} event_id is '{kind}:<exec_id>'")
        elif self.exec_id is not None or self.base_exec_id is not None:
            raise ValueError(f"a {kind} carries no exec_id")
        if kind in ("fill", "correction"):
            if self.con_id is None or self.sec_type is None or self.perm_id is None:
                raise ValueError(f"a {kind} names con_id, sec_type and perm_id")
        if kind == "fill" and self.quantity_delta == 0:
            raise ValueError("a fill moves quantity")
        if (kind == "correction") != (self.supersedes_event_id is not None):
            raise ValueError("exactly a correction names supersedes_event_id")
        if kind in ("commission", "cash_adjustment", "reconciliation") and self.quantity_delta:
            raise ValueError(f"a {kind} moves no quantity")
        if kind == "cash_adjustment" and not self.event_id.startswith("cash:"):
            raise ValueError("a cash_adjustment event_id is 'cash:<source id>'")
        if kind == "reconciliation":
            if self.ref_sha256 is None or self.event_id != f"reconciliation:{self.ref_sha256}":
                raise ValueError("a reconciliation names ref_sha256 and its event_id")
            if self.cash_delta_micros:
                raise ValueError("a reconciliation moves no cash")
        elif self.ref_sha256 is not None:
            raise ValueError("only a reconciliation carries ref_sha256")
        return self


class AttributedQuantity(StrictModel):
    attribution: Attribution
    quantity: NonZeroInt

    @model_validator(mode="after")
    def _nonzero(self) -> AttributedQuantity:
        if self.quantity == 0:
            raise ValueError("an attributed quantity is non-zero")
        return self


class Holding(StrictModel):
    """A ledger position in one contract, split by attribution."""

    account_id: IdStr
    con_id: PositiveInt
    sec_type: PositionSecType
    symbol: IdStr
    quantity: NonZeroInt
    cost_basis_micros: int = Field(strict=True)
    lots: tuple[AttributedQuantity, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _sums(self) -> Holding:
        if self.quantity == 0:
            raise ValueError("a holding is non-zero (flat contracts are not held)")
        if self.sec_type == "STK" and self.quantity < 0:
            raise ValueError("equities are long-only")
        if sum(lot.quantity for lot in self.lots) != self.quantity:
            raise ValueError("lot quantities must sum to the holding quantity")
        keys = [lot.attribution for lot in self.lots]
        if len(keys) != len(set(keys)):
            raise ValueError("one lot per attribution")
        return self
