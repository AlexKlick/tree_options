"""Exercise settlement for long option positions (M3 plan §3.C).

Closes the reserved-EntryKind gap: this module mints the
`exercise_settlement` ledger event. Settlement MEDIUM is cash (declared
simplification, plan §5): the long holder receives

    cash = max(intrinsic, 0) * quantity * multiplier

struck at a reference UNDERLYING close, knowable only at that bar's
publication instant (`ts` — the first instant the intrinsic is knowable).
Three kinds: `expiry` (session == expiration), `early_exercise`
(session < expiration, american contracts only — the election policy lives
in options/exercise.py and consumes only file(t-1) data), and `terminal`
(M3 §2 iv: a delisting/merger death mid-hold settles at intrinsic vs the
final bar's close — forced by the exchange, not elected).
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import Field, model_validator

from tree_options.data.bars import BarRecord
from tree_options.schemas.common import FEE_TICK, IdStr, Money, Price, StrictModel, UTCDatetime
from tree_options.schemas.options import OptionContract

SettlementKind = Literal["expiry", "early_exercise", "terminal"]


def intrinsic_value(call_put: str, strike: Decimal, underlying: Decimal) -> Decimal:
    if call_put == "C":
        return max(underlying - strike, Decimal("0"))
    return max(strike - underlying, Decimal("0"))


class ExerciseSettlement(StrictModel):
    settlement_id: IdStr
    contract_id: IdStr
    kind: SettlementKind
    quantity: int = Field(ge=1)
    strike: Price
    call_put: Literal["C", "P"]
    multiplier: int = Field(ge=1)
    settlement_price: Price  # the underlying reference close
    cash: Money = Field(ge=0)
    session: date  # the exercise (or expiration) session
    ts: UTCDatetime  # the reference bar's publication — first knowable instant
    ref_id: IdStr  # the reference bar's source_record_id

    @model_validator(mode="after")
    def _cash_matches_intrinsic(self) -> ExerciseSettlement:
        intrinsic = intrinsic_value(self.call_put, self.strike, self.settlement_price)
        expected = (intrinsic * self.quantity * self.multiplier).quantize(FEE_TICK)
        if self.cash != expected:
            raise ValueError(
                f"settlement cash {self.cash} != intrinsic arithmetic {expected} "
                f"({self.call_put} K={self.strike} S={self.settlement_price} "
                f"qty={self.quantity} mult={self.multiplier})"
            )
        return self


class SettlementMintError(ValueError):
    """The requested settlement is not a well-formed exercise."""


def mint_settlement(
    *,
    contract: OptionContract,
    settlement_id: str,
    kind: SettlementKind,
    quantity: int,
    session: date,
    reference_bar: BarRecord,
    terminal_detect_at: datetime | None = None,
) -> ExerciseSettlement:
    """The single fail-closed door for settlement construction.

    The reference is an AUTHORITATIVE BarRecord (review r1 P1-6): close,
    publication instant, and provenance id all come from one bar, which
    must belong to the settlement session and the contract's underlying —
    free-floating close/timestamp fields could credit cash early or
    against the wrong name.

    `terminal_detect_at` is the SILENT-DEATH door (M3 §2 iv completion):
    bankruptcy_11 / voluntary_delisting / coverage_lapse deaths emit no
    action record — the death is knowable only at the first barless
    session (the complete-vendor inference). The reference bar is then
    the underlying's LAST bar (an earlier session is permitted), cash
    strikes at that bar's close, and ts is the detection instant so the
    ledger's application timeline stays non-decreasing. kind="terminal"
    only.

    Refuses: settling before the contract lists; early exercise of a
    non-american contract; kind/expiry mismatch; zero quantity; a
    reference bar from the wrong session or underlying."""
    if quantity < 1:
        raise SettlementMintError(f"settlement quantity must be >= 1, got {quantity}")
    if session < contract.listing_start:
        raise SettlementMintError(
            f"{contract.contract_id} cannot settle on {session}: before listing_start"
        )
    if kind == "expiry":
        if session != contract.expiration:
            raise SettlementMintError(
                f"expiry settlement of {contract.contract_id} must land on "
                f"expiration {contract.expiration}, got {session}"
            )
    elif kind == "terminal":
        # plan §2 (iv): a terminal delisting/merger mid-hold settles at
        # intrinsic vs the final bar's close at its publication instant —
        # forced by the exchange, not elected, so neither the american
        # right nor the pre-expiry restriction applies; the session must
        # still precede expiration (an at-expiry death is an expiry kind).
        if session > contract.expiration:
            raise SettlementMintError(
                f"terminal settlement of {contract.contract_id} on {session} is past "
                f"expiration {contract.expiration} (that is an expiry settlement)"
            )
    else:
        if contract.exercise_style != "american":
            raise SettlementMintError(
                f"early exercise of {contract.contract_id} refused: "
                f"{contract.exercise_style} contracts have no early exercise right"
            )
        if session >= contract.expiration:
            raise SettlementMintError(
                f"early_exercise of {contract.contract_id} on {session} must precede "
                f"expiration {contract.expiration} (that session is an expiry settlement)"
            )
    if terminal_detect_at is not None:
        # the silent-death door: an earlier-session LAST bar is permitted,
        # and ts is the detection instant (>= the bar's publication)
        if kind != "terminal":
            raise SettlementMintError(
                "terminal_detect_at is the silent-death door: kind must be terminal"
            )
        if reference_bar.session > session:
            raise SettlementMintError(
                f"silent-death reference bar {reference_bar.source_record_id} is from "
                f"{reference_bar.session}, after the detection session {session}"
            )
        if terminal_detect_at < reference_bar.available_at:
            raise SettlementMintError(
                f"detection instant {terminal_detect_at} precedes the reference bar's "
                f"publication {reference_bar.available_at}"
            )
    elif reference_bar.session != session:
        raise SettlementMintError(
            f"reference bar {reference_bar.source_record_id} is from session "
            f"{reference_bar.session}, not the settlement session {session}"
        )
    if reference_bar.security_id != contract.underlying_security_id:
        raise SettlementMintError(
            f"reference bar {reference_bar.source_record_id} is for "
            f"{reference_bar.security_id}, not underlying "
            f"{contract.underlying_security_id}"
        )
    cash = (
        intrinsic_value(contract.call_put, contract.strike, reference_bar.close)
        * quantity
        * contract.multiplier
    ).quantize(FEE_TICK)
    return ExerciseSettlement(
        settlement_id=settlement_id,
        contract_id=contract.contract_id,
        kind=kind,
        quantity=quantity,
        strike=contract.strike,
        call_put=contract.call_put,
        multiplier=contract.multiplier,
        settlement_price=reference_bar.close,
        cash=cash,
        session=session,
        ts=terminal_detect_at if terminal_detect_at is not None else reference_bar.available_at,
        ref_id=reference_bar.source_record_id,
    )
