"""Books lane: the append-only ``trex-ledger-event/1`` ledger and its fold.

Pure functions over the C2.4 contract types plus small file helpers for
``ledger/events.jsonl``, ``ledger/executions.jsonl`` and the
``ledger/holdings.json`` projection (WAVE2-CONTRACTS.md C2.4, C2.6).

Broker facts to events (``fill_events``):

- one ``fill`` per base exec id (its oldest reported version), then one
  ``correction`` per later version carrying the ADJUSTMENT (corrected
  minus what the ledger already booked) and naming the event it
  supersedes; one ``commission`` per exec id whose report arrived;
- ids are deterministic (``fill:<exec_id>`` ...), so a replayed broker
  fact produces the same event and appending it is a no-op;
- given the events already booked, a late OLDER version of a booked fill
  is never booked again (no double count): only newer versions correct;
- attribution comes from the order tag: ``trex:sup:<intent>`` directly,
  ``trex:desk:<sid>`` through the desk spec (an equity's sid IS its
  intent id); an untagged or foreign tag is ``OUTSIDE``; a trex tag no
  proposal explains books as ``OUTSIDE`` with ``detail`` =
  ``unattributed:<order_ref>`` and is listed by the fold. W2c: the v1
  entry inbox writes no proposal, so an option desk spec tagged
  ``trex:sup:<its intent>`` explains its intent as a desk-owned structure
  (``v1_desk_attribution``; a proposal still wins for its own intent).

The fold (``fold_ledger``) is a pure sum where the contract says so
(position = sum of ``quantity_delta``, cash = sum of
``cash_delta_micros``) and is independent of event order everywhere: the
cost basis uses average cost per (account, contract, attribution) lot over
the fills in broker-time order (``at`` of the fill, then base exec id),
not file order. ``cost_basis_micros`` is net fill cash for the open
quantity; commissions stay their own rows but are realized with the lot
(IBKR's convention): an entry fee is held against the lot and released
pro rata when it closes, an exit fee is realized at the close, and a fee
with no booked fill is an expense when reported. Cash adjustments move
cash only, never realized trading P&L.

Canonical holdings projection: ``trex-holdings/1``
--------------------------------------------------

``ledger/holdings.json`` (``Wave2RunDir.holdings()``) is THE holdings
projection of the books (coordinator decision 2, 2026-10-21). The desk
owner writes it (``write_holdings``: canonical bytes, atomic replace); every
other reader (admission, the web portfolio, owner orchestration) loads it
with ``load_holdings`` and neither re-folds the ledger nor parses the file
by hand. Shape (``HoldingsDocument``, a strict model: unknown keys and
non-integer money refuse):

| Key | Type | Invariant |
| --- | --- | --- |
| ``schema`` | ``"trex-holdings/1"`` | a new shape is ``/2``, never an edit |
| ``account_id`` | id | the one account projected; every holding's |
| ``as_of`` | UTC instant (``...Z``) | when the ledger fold was projected |
| ``cash_micros`` | signed int, micros | sum of every ledger cash delta of the account (fills, corrections, commissions, cash adjustments). Ledger cash, NOT broker settled cash: that is the risk snapshot's ``cash_cents`` |
| ``holdings`` | list of C2.4 ``Holding`` | non-flat contracts, ascending con_id, one row per con_id; ``lots`` one per attribution, summing to ``quantity`` (outside lots carry the all-null attribution) |

``load_holdings(path)`` never follows a symlink, reads at most
``MAX_HOLDINGS_BYTES`` and raises ``HoldingsUnavailable`` whose ``reason``
is ``missing`` (absent), ``unreadable`` (symlink, not a regular file, too
big, not JSON) or ``invalid`` (not a ``trex-holdings/1`` document). A
reader shows an unavailable projection as unavailable, never as empty.
"""

from __future__ import annotations

import fcntl
import json
import os
import stat
import tempfile
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, ValidationError, model_validator

from tree_options.schemas.common import IdStr, StrictModel
from tree_options.time.sessions import SESSION_TIMEZONE
from tree_options.trex.contracts.books import (
    UNASSIGNED_SLEEVE,
    AttributedQuantity,
    Attribution,
    BrokerExecution,
    Holding,
    LedgerEvent,
    parse_order_ref,
)
from tree_options.trex.contracts.canonical import UTCInstant, model_bytes, model_document
from tree_options.trex.contracts.desk_spec_v2 import EquityDeskSpecV2, OptionDeskSpecV2
from tree_options.trex.contracts.layout import Wave2RunDir
from tree_options.trex.contracts.proposals import EntryProposal
from tree_options.trex.desk_spec import DeskSpec
from tree_options.trex.fills import Fill, FillBook, cash_micros, signed_quantity, version_key

__all__ = [
    "HOLDINGS_SCHEMA",
    "MAX_HOLDINGS_BYTES",
    "OUTSIDE",
    "UNATTRIBUTED_PREFIX",
    "V1_DESK_PROGRAM",
    "V1_DESK_STRATEGY",
    "AnyDeskSpec",
    "AppendResult",
    "AttributionIndex",
    "AttributionTotals",
    "CashIn",
    "HoldingsDocument",
    "HoldingsUnavailable",
    "LedgerConflict",
    "LedgerFold",
    "Lot",
    "Realization",
    "StockCashOut",
    "append_events",
    "append_executions",
    "append_ledger",
    "attribution_for_proposal",
    "cash_adjustment_event",
    "fill_events",
    "fold_ledger",
    "holdings_document",
    "load_holdings",
    "read_executions",
    "read_ledger",
    "spec_structure_id",
    "symbols_from_executions",
    "v1_desk_attribution",
    "write_atomic",
    "write_holdings",
]

#: Not ours: an untagged order, another book's tag, a manual trade.
OUTSIDE = Attribution(intent_id=None, strategy_version=None, program_id=None)
UNATTRIBUTED_PREFIX = "unattributed:"
#: the program and strategy a v1 desk structure books under (W2c; the v1
#: inbox has no Wave 2 program, and its request's strategy is not a fact the
#: books read)
V1_DESK_PROGRAM = "trex-desk-v1"
V1_DESK_STRATEGY = "trex-desk-v1"
#: ``ledger/holdings.json`` (the layout names the file; C2.4 has no schema
#: for the projection document, so the books lane versions it here and the
#: module doc is its schema note).
HOLDINGS_SCHEMA = "trex-holdings/1"
#: a holdings file bigger than this is not the books lane's (= the web
#: portfolio's ``MAX_BOOK_BYTES``)
MAX_HOLDINGS_BYTES = 4 * 1024 * 1024

AnyDeskSpec = DeskSpec | OptionDeskSpecV2 | EquityDeskSpecV2
PositionSecType = Literal["OPT", "STK"]
TotalsKey = Literal["intent_id", "program_id", "sleeve_ref"]


class LedgerConflict(ValueError):
    """The ledger (or a fact about to enter it) cannot be trusted as is."""


# ----------------------------------------------------------------- attribution


def attribution_for_proposal(proposal: EntryProposal) -> Attribution:
    """The attribution every order of the proposal's intent books under."""
    return Attribution(
        intent_id=proposal.effect.intent_id,
        strategy_version=proposal.strategy_version,
        program_id=proposal.program_id,
        sleeve_ref=proposal.sleeve_ref or UNASSIGNED_SLEEVE,
    )


def spec_structure_id(spec: AnyDeskSpec) -> str:
    """The structure id a desk exit is tagged with (``trex:desk:<sid>``)."""
    if isinstance(spec, EquityDeskSpecV2):
        return spec.position.position_id
    return spec.structure.id


def v1_desk_attribution(intent_id: str) -> Attribution:
    """The attribution of a v1 desk structure (W2c): an option spec the v1
    entry inbox registered, whose entry carries ``trex:sup:<intent_id>`` and
    which no Wave 2 proposal explains. Fixed program and strategy, so every
    fill of the intent books under one lot whatever the run reads later."""
    return Attribution(
        intent_id=intent_id, strategy_version=V1_DESK_STRATEGY, program_id=V1_DESK_PROGRAM
    )


def _v1_desk_spec(spec: AnyDeskSpec) -> bool:
    """A desk-owned defined-risk option spec tagged as its own intent's
    supervised entry (``trex:sup:<intent_id>``): the v1 inbox's shape."""
    if isinstance(spec, EquityDeskSpecV2):
        return False
    owner = parse_order_ref(spec.entry_order_ref)
    return owner is not None and owner.kind == "sup" and owner.ident == spec.intent_id


@dataclass(frozen=True)
class AttributionIndex:
    """intent id -> attribution (from proposals, else a v1 desk spec); desk
    sid -> intent id (from specs)."""

    intents: Mapping[str, Attribution] = field(default_factory=dict)
    structures: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def build(
        cls, proposals: Iterable[EntryProposal] = (), specs: Iterable[AnyDeskSpec] = ()
    ) -> AttributionIndex:
        """A proposal attributes its intent. W2c: an option desk spec of the
        v1 entry inbox (no proposal: the v1 path writes none) attributes its
        intent to the desk (``v1_desk_attribution``), so the v1 canary's
        fills are a DESK-owned defined-risk structure, never ``OUTSIDE``
        ``unattributed:<ref>`` (whose short leg failed every Wave 2 entry
        closed). A tag neither explains stays unattributed."""
        intents: dict[str, Attribution] = {}
        for proposal in proposals:
            attribution = attribution_for_proposal(proposal)
            intent = proposal.effect.intent_id
            if intents.setdefault(intent, attribution) != attribution:
                raise LedgerConflict(f"intent {intent}: proposals attribute it differently")
        structures: dict[str, str] = {}
        v1: dict[str, Attribution] = {}
        for spec in specs:
            sid = spec_structure_id(spec)
            if structures.setdefault(sid, spec.intent_id) != spec.intent_id:
                raise LedgerConflict(f"structure {sid}: specs name different intents")
            if spec.intent_id not in intents and _v1_desk_spec(spec):
                v1[spec.intent_id] = v1_desk_attribution(spec.intent_id)
        return cls(intents={**v1, **intents}, structures=structures)

    def resolve(self, order_ref: str | None) -> Attribution | None:
        """``OUTSIDE`` for an untagged or foreign order; None for a trex
        tag this index cannot explain."""
        owner = parse_order_ref(order_ref)
        if owner is None:
            return OUTSIDE
        if owner.kind == "sup":
            return self.intents.get(owner.ident)
        return self.intents.get(self.structures.get(owner.ident, owner.ident))


# -------------------------------------------------------- broker facts -> events


def _sec_type(execution: BrokerExecution) -> PositionSecType:
    if execution.sec_type == "OPT":
        return "OPT"
    if execution.sec_type == "STK":
        return "STK"
    raise LedgerConflict(f"exec {execution.exec_id}: a {execution.sec_type} report is not booked")


@dataclass(frozen=True)
class _Owner:
    """The attribution (and its unattributed note) one execution books under."""

    attribution: Attribution
    detail: str = ""

    @classmethod
    def of(cls, index: AttributionIndex, fill: Fill, booked: Sequence[LedgerEvent]) -> _Owner:
        """Booked chains keep the attribution they were booked under, so a
        later correction or commission never disagrees with its fill."""
        for event in booked:
            if event.kind == "fill":
                return cls(event.attribution, event.detail)
        order_ref = fill.versions[0].order_ref
        attribution = index.resolve(order_ref)
        if attribution is None:
            return cls(OUTSIDE, UNATTRIBUTED_PREFIX + order_ref)
        return cls(attribution)


def _event(
    execution: BrokerExecution,
    *,
    kind: Literal["fill", "correction", "commission"],
    quantity_delta: int,
    cash_delta_micros: int,
    owner: _Owner,
    recorded_at: datetime,
    supersedes: str | None = None,
) -> LedgerEvent:
    attribution, detail = owner.attribution, owner.detail
    return LedgerEvent(
        event_id=f"{kind}:{execution.exec_id}",
        kind=kind,
        account_id=execution.account_id,
        at=execution.time,
        recorded_at=recorded_at,
        attribution=attribution,
        quantity_delta=quantity_delta,
        cash_delta_micros=cash_delta_micros,
        con_id=execution.con_id,
        sec_type=_sec_type(execution),
        exec_id=execution.exec_id,
        base_exec_id=execution.base_exec_id,
        perm_id=execution.perm_id,
        order_ref=execution.order_ref or None,
        supersedes_event_id=supersedes,
        detail=detail,
    )


def _chain(
    fill: Fill,
    booked: Sequence[LedgerEvent],
    owner: _Owner,
    recorded_at: datetime,
) -> list[LedgerEvent]:
    """The fill/correction events still missing for ``fill``."""
    events: list[LedgerEvent] = []
    if booked:
        head = max(booked, key=lambda e: version_key(e.exec_id or ""))
        qty = sum(e.quantity_delta for e in booked)
        cash = sum(e.cash_delta_micros for e in booked)
        newer = [
            v for v in fill.versions if version_key(v.exec_id) > version_key(head.exec_id or "")
        ]
        previous = head.event_id
    else:
        first = fill.versions[0]
        qty, cash = signed_quantity(first), cash_micros(first)
        opened = _event(
            first,
            kind="fill",
            quantity_delta=qty,
            cash_delta_micros=cash,
            owner=owner,
            recorded_at=recorded_at,
        )
        events.append(opened)
        newer, previous = list(fill.versions[1:]), opened.event_id
    for version in newer:
        corrected_qty, corrected_cash = signed_quantity(version), cash_micros(version)
        correction = _event(
            version,
            kind="correction",
            quantity_delta=corrected_qty - qty,
            cash_delta_micros=corrected_cash - cash,
            owner=owner,
            recorded_at=recorded_at,
            supersedes=previous,
        )
        events.append(correction)
        qty, cash, previous = corrected_qty, corrected_cash, correction.event_id
    return events


def fill_events(
    book: FillBook,
    index: AttributionIndex,
    *,
    recorded_at: datetime,
    existing: Iterable[LedgerEvent] = (),
) -> tuple[LedgerEvent, ...]:
    """The ledger events the fill book adds to ``existing`` (new ones only)."""
    existing = tuple(existing)
    known = {e.event_id for e in existing}
    booked: dict[str, list[LedgerEvent]] = defaultdict(list)
    for event in existing:
        if event.kind in ("fill", "correction") and event.base_exec_id is not None:
            booked[event.base_exec_id].append(event)
    out: list[LedgerEvent] = []
    for fill in book.fills():
        chain = booked.get(fill.base_exec_id, [])
        owner = _Owner.of(index, fill, chain)
        out.extend(_chain(fill, chain, owner, recorded_at))
        for version in fill.versions:
            if version.commission_micros is None:
                continue
            out.append(
                _event(
                    version,
                    kind="commission",
                    quantity_delta=0,
                    cash_delta_micros=-version.commission_micros,
                    owner=owner,
                    recorded_at=recorded_at,
                )
            )
    fresh: list[LedgerEvent] = []
    for event in out:
        if event.event_id not in known:
            known.add(event.event_id)
            fresh.append(event)
    return tuple(fresh)


def cash_adjustment_event(
    *,
    account_id: str,
    source_id: str,
    at: datetime,
    recorded_at: datetime,
    cash_delta_micros: int,
    detail: str = "",
    attribution: Attribution = OUTSIDE,
) -> LedgerEvent:
    """A broker cash movement that is no fill (interest, fees, transfers)."""
    return LedgerEvent(
        event_id=f"cash:{source_id}",
        kind="cash_adjustment",
        account_id=account_id,
        at=at,
        recorded_at=recorded_at,
        attribution=attribution,
        quantity_delta=0,
        cash_delta_micros=cash_delta_micros,
        detail=detail,
    )


# --------------------------------------------------------------------- append


@dataclass(frozen=True)
class AppendResult:
    events: tuple[LedgerEvent, ...]
    appended: tuple[LedgerEvent, ...]
    duplicates: tuple[str, ...]


def _content(event: LedgerEvent) -> dict[str, Any]:
    """The fact an event records (when it was recorded is not the fact)."""
    document = model_document(event)
    document.pop("recorded_at")
    return document


def append_events(existing: Sequence[LedgerEvent], new: Iterable[LedgerEvent]) -> AppendResult:
    """Append-only with deterministic ids: the same fact again is a no-op,
    a different fact under a used id refuses, and a base exec id is filled
    once (its later versions are corrections)."""
    by_id: dict[str, LedgerEvent] = {}
    fills: dict[str, str] = {}
    for event in existing:
        _admit(event, by_id, fills)
    appended: list[LedgerEvent] = []
    duplicates: list[str] = []
    for event in new:
        if _admit(event, by_id, fills):
            appended.append(event)
        else:
            duplicates.append(event.event_id)
    for event in appended:
        target = event.supersedes_event_id
        if target is not None and target not in by_id:
            raise LedgerConflict(f"{event.event_id}: supersedes {target}, not in the ledger")
    return AppendResult(
        events=(*existing, *appended), appended=tuple(appended), duplicates=tuple(duplicates)
    )


def _admit(event: LedgerEvent, by_id: dict[str, LedgerEvent], fills: dict[str, str]) -> bool:
    held = by_id.get(event.event_id)
    if held is not None:
        if _content(held) != _content(event):
            raise LedgerConflict(f"{event.event_id}: a different fact under a booked id")
        return False
    if event.kind == "fill" and event.base_exec_id is not None:
        other = fills.setdefault(event.base_exec_id, event.event_id)
        if other != event.event_id:
            raise LedgerConflict(
                f"execution {event.base_exec_id}: already booked by {other}; "
                f"{event.event_id} would count it twice"
            )
    by_id[event.event_id] = event
    return True


# ----------------------------------------------------------------------- fold


@dataclass(frozen=True)
class Lot:
    """One attribution's position in one contract (zero once closed)."""

    account_id: str
    con_id: int
    sec_type: PositionSecType
    attribution: Attribution
    quantity: int
    cost_basis_micros: int
    #: entry commissions held against the open quantity (realized on close)
    fee_basis_micros: int


@dataclass(frozen=True)
class Realization:
    """Realized P&L (net of the commissions it releases) at a broker time."""

    account_id: str
    con_id: int | None
    attribution: Attribution
    at: datetime
    realized_micros: int
    source_event_id: str


@dataclass(frozen=True)
class StockCashOut:
    """What one booked stock execution took out of cash (micros, >= 0): a
    purchase its price net of its corrections plus its booked commission, a
    sale its commission alone (its proceeds are never counted before they
    settle). The risk snapshot subtracts the ones the broker's settled cash
    does not show yet (W2c)."""

    account_id: str
    con_id: int
    #: the broker time of the fill (its ET date is the trade date)
    at: datetime
    micros: int


@dataclass(frozen=True)
class CashIn:
    """What one booked execution, stock or option, brought into cash
    (micros, > 0): its proceeds net of its corrections (a sale, a credit).
    IBKR's ``TotalCashValue`` shows them on the trade date, settled cash
    only once they settle; the risk snapshot takes the unsettled ones back
    out of a ``TotalCashValue`` figure (never added early, W2c)."""

    account_id: str
    con_id: int
    #: the broker time of the fill (its ET date is the trade date)
    at: datetime
    micros: int


@dataclass(frozen=True)
class AttributionTotals:
    open_quantity: dict[int, int]
    realized_micros: int = 0
    fees_micros: int = 0
    cash_micros: int = 0


def _attribution_key(attribution: Attribution) -> tuple[str, str, str, str]:
    return (
        attribution.intent_id or "",
        attribution.strategy_version or "",
        attribution.program_id or "",
        attribution.sleeve_ref,
    )


@dataclass(frozen=True)
class LedgerFold:
    """Everything the books derive from the ledger, in canonical order."""

    lots: tuple[Lot, ...]
    realizations: tuple[Realization, ...]
    #: (account, attribution, commission micros, cash micros) per attribution
    flows: tuple[tuple[str, Attribution, int, int], ...]
    touched_contracts: tuple[tuple[str, int], ...]
    unattributed: tuple[str, ...]
    #: exec ids booked as a fill or correction whose commission has not been
    #: booked yet: realized figures are provisional (never zero-fee) until empty
    pending_commissions: tuple[str, ...] = ()
    #: every booked stock execution's cash out, in broker-time order (W2c)
    stock_cash_out: tuple[StockCashOut, ...] = ()
    #: every booked execution's proceeds, in broker-time order (cash tag fix)
    cash_in: tuple[CashIn, ...] = ()

    @property
    def complete(self) -> bool:
        return not self.pending_commissions

    def positions(self, account_id: str) -> dict[int, int]:
        """Ledger quantity for EVERY contract the ledger touched (0 = flat)."""
        out = {con: 0 for acct, con in self.touched_contracts if acct == account_id}
        for lot in self.lots:
            if lot.account_id == account_id:
                out[lot.con_id] = out.get(lot.con_id, 0) + lot.quantity
        return dict(sorted(out.items()))

    def touched(self, account_id: str) -> frozenset[int]:
        return frozenset(con for acct, con in self.touched_contracts if acct == account_id)

    def cash(self, account_id: str) -> int:
        """Sum of every event's cash delta for the account (micros)."""
        return sum(cash for acct, _, _, cash in self.flows if acct == account_id)

    def account_lots(self, account_id: str) -> tuple[Lot, ...]:
        return tuple(lot for lot in self.lots if lot.account_id == account_id and lot.quantity)

    def holdings(self, account_id: str, symbols: Mapping[int, str]) -> tuple[Holding, ...]:
        """Non-flat contracts as C2.4 ``Holding`` rows (one lot per attribution)."""
        by_con: dict[int, list[Lot]] = defaultdict(list)
        for lot in self.account_lots(account_id):
            by_con[lot.con_id].append(lot)
        out: list[Holding] = []
        for con_id in sorted(by_con):
            lots = by_con[con_id]
            quantity = sum(lot.quantity for lot in lots)
            if quantity == 0:
                continue
            if lots[0].sec_type == "STK" and quantity < 0:
                raise LedgerConflict(f"con_id {con_id}: {quantity} shares; equities are long-only")
            symbol = symbols.get(con_id)
            if not symbol:
                raise LedgerConflict(f"con_id {con_id}: no symbol known for the holding")
            out.append(
                Holding(
                    account_id=account_id,
                    con_id=con_id,
                    sec_type=lots[0].sec_type,
                    symbol=symbol,
                    quantity=quantity,
                    cost_basis_micros=sum(lot.cost_basis_micros for lot in lots),
                    lots=tuple(
                        AttributedQuantity(attribution=lot.attribution, quantity=lot.quantity)
                        for lot in lots
                    ),
                )
            )
        return tuple(out)

    def totals_by(self, key: TotalsKey, account_id: str) -> dict[str | None, AttributionTotals]:
        """Open quantity, realized P&L, commissions and cash per intent,
        program or sleeve (outside rows group under None; a sleeve-less
        intent under ``unassigned``)."""
        acc: dict[str | None, dict[str, Any]] = defaultdict(
            lambda: {"open": defaultdict(int), "realized": 0, "fees": 0, "cash": 0}
        )
        for lot in self.account_lots(account_id):
            acc[getattr(lot.attribution, key)]["open"][lot.con_id] += lot.quantity
        for real in self.realizations:
            if real.account_id == account_id:
                acc[getattr(real.attribution, key)]["realized"] += real.realized_micros
        for acct, attribution, fees, cash in self.flows:
            if acct == account_id:
                group = acc[getattr(attribution, key)]
                group["fees"] += fees
                group["cash"] += cash
        return {
            name: AttributionTotals(
                open_quantity={c: q for c, q in sorted(v["open"].items()) if q},
                realized_micros=v["realized"],
                fees_micros=v["fees"],
                cash_micros=v["cash"],
            )
            for name, v in acc.items()
        }

    def day_realized_loss_micros(self, account_id: str, day: date) -> int:
        """Realized losses on the ET session date ``day``: per attribution
        the day's net realized P&L, a loss counted, a gain never offsetting
        another attribution's loss."""
        net: dict[Attribution, int] = defaultdict(int)
        for real in self.realizations:
            if real.account_id == account_id and real.at.astimezone(SESSION_TIMEZONE).date() == day:
                net[real.attribution] += real.realized_micros
        return sum(-value for value in net.values() if value < 0)


def _part(amount: int, part: int, whole: int) -> int:
    """``amount x part / whole`` truncated toward zero (exact when part == whole)."""
    if part == whole:
        return amount
    magnitude = abs(amount) * part // whole
    return magnitude if amount >= 0 else -magnitude


@dataclass
class _Effective:
    """One booked execution as the ledger stands: fill + its corrections."""

    account_id: str
    con_id: int
    sec_type: PositionSecType
    attribution: Attribution
    at: datetime
    base: str
    source: str
    quantity: int = 0
    cash: int = 0
    fee: int = 0


@dataclass
class _LotState:
    quantity: int = 0
    cost: int = 0
    fee: int = 0


def _validated(events: Iterable[LedgerEvent]) -> dict[str, LedgerEvent]:
    by_id: dict[str, LedgerEvent] = {}
    fills: dict[str, str] = {}
    for event in sorted(events, key=lambda e: e.event_id):
        _admit(event, by_id, fills)
    for event in by_id.values():
        target = event.supersedes_event_id
        if target is None:
            continue
        superseded = by_id.get(target)
        if superseded is None:
            raise LedgerConflict(f"{event.event_id}: supersedes {target}, not in the ledger")
        if superseded.base_exec_id != event.base_exec_id:
            raise LedgerConflict(f"{event.event_id}: supersedes {target} of another execution")
    return by_id


def _effective_fills(by_id: Mapping[str, LedgerEvent]) -> dict[str, _Effective]:
    chains: dict[str, list[LedgerEvent]] = defaultdict(list)
    for event in by_id.values():
        if event.kind in ("fill", "correction") and event.base_exec_id is not None:
            chains[event.base_exec_id].append(event)
    out: dict[str, _Effective] = {}
    for base, chain in chains.items():
        roots = [e for e in chain if e.kind == "fill"]
        if len(roots) != 1:
            raise LedgerConflict(f"execution {base}: corrections without its fill")
        root = roots[0]
        if root.con_id is None or root.sec_type is None:
            raise LedgerConflict(f"{root.event_id}: a fill names its contract")
        for event in chain:
            same = (event.account_id, event.con_id, event.sec_type, event.attribution)
            if same != (root.account_id, root.con_id, root.sec_type, root.attribution):
                raise LedgerConflict(f"{event.event_id}: corrects {root.event_id} elsewhere")
        out[base] = _Effective(
            account_id=root.account_id,
            con_id=root.con_id,
            sec_type=root.sec_type,
            attribution=root.attribution,
            at=root.at,
            base=base,
            source=root.event_id,
            quantity=sum(e.quantity_delta for e in chain),
            cash=sum(e.cash_delta_micros for e in chain),
        )
    return out


def fold_ledger(events: Iterable[LedgerEvent]) -> LedgerFold:
    """Fold ledger events (any order) into lots, realized P&L and flows."""
    by_id = _validated(events)
    effective = _effective_fills(by_id)
    realizations: list[Realization] = []
    flows: dict[tuple[str, Attribution], list[int]] = defaultdict(lambda: [0, 0])
    touched: set[tuple[str, int]] = set()
    for event in by_id.values():
        flow = flows[(event.account_id, event.attribution)]
        flow[1] += event.cash_delta_micros
        if event.con_id is not None:
            touched.add((event.account_id, event.con_id))
        if event.kind != "commission":
            continue
        flow[0] -= event.cash_delta_micros
        fill = effective.get(event.base_exec_id or "")
        if fill is not None:
            fill.fee -= event.cash_delta_micros
        else:  # no booked fill to hold it against: an expense when reported
            realizations.append(
                Realization(
                    account_id=event.account_id,
                    con_id=event.con_id,
                    attribution=event.attribution,
                    at=event.at,
                    realized_micros=event.cash_delta_micros,
                    source_event_id=event.event_id,
                )
            )

    lots: dict[tuple[str, int, Attribution], _LotState] = defaultdict(_LotState)
    sec_types: dict[tuple[str, int], PositionSecType] = {}
    for fill in sorted(effective.values(), key=lambda f: (f.at, f.base)):
        if sec_types.setdefault((fill.account_id, fill.con_id), fill.sec_type) != fill.sec_type:
            raise LedgerConflict(f"con_id {fill.con_id}: booked as both OPT and STK")
        lot = lots[(fill.account_id, fill.con_id, fill.attribution)]
        realized = _apply(lot, fill)
        if realized is not None:
            realizations.append(
                Realization(
                    account_id=fill.account_id,
                    con_id=fill.con_id,
                    attribution=fill.attribution,
                    at=fill.at,
                    realized_micros=realized,
                    source_event_id=fill.source,
                )
            )

    return LedgerFold(
        lots=tuple(
            Lot(
                account_id=acct,
                con_id=con,
                sec_type=sec_types[(acct, con)],
                attribution=attribution,
                quantity=state.quantity,
                cost_basis_micros=state.cost,
                fee_basis_micros=state.fee,
            )
            for (acct, con, attribution), state in sorted(
                lots.items(), key=lambda kv: (kv[0][0], kv[0][1], _attribution_key(kv[0][2]))
            )
        ),
        realizations=tuple(
            sorted(
                realizations,
                key=lambda r: (r.at, r.source_event_id, _attribution_key(r.attribution)),
            )
        ),
        flows=tuple(
            (acct, attribution, fees, cash)
            for (acct, attribution), (fees, cash) in sorted(
                flows.items(), key=lambda kv: (kv[0][0], _attribution_key(kv[0][1]))
            )
        ),
        touched_contracts=tuple(sorted(touched)),
        unattributed=tuple(
            sorted(e.event_id for e in by_id.values() if e.detail.startswith(UNATTRIBUTED_PREFIX))
        ),
        pending_commissions=_pending_commissions(by_id.values()),
        stock_cash_out=tuple(
            _cash_out(fill)
            for fill in sorted(effective.values(), key=lambda f: (f.at, f.base))
            if fill.sec_type == "STK"
        ),
        cash_in=tuple(
            CashIn(account_id=fill.account_id, con_id=fill.con_id, at=fill.at, micros=fill.cash)
            for fill in sorted(effective.values(), key=lambda f: (f.at, f.base))
            if fill.cash > 0
        ),
    )


def _cash_out(fill: _Effective) -> StockCashOut:
    """A purchase's price (net of corrections) plus its commission; a
    sale's commission alone."""
    paid = -fill.cash if fill.quantity > 0 and fill.cash < 0 else 0
    return StockCashOut(
        account_id=fill.account_id,
        con_id=fill.con_id,
        at=fill.at,
        micros=paid + max(fill.fee, 0),
    )


def _pending_commissions(events: Iterable[LedgerEvent]) -> tuple[str, ...]:
    executed: set[str] = set()
    charged: set[str] = set()
    for event in events:
        if event.exec_id is None:
            continue
        (charged if event.kind == "commission" else executed).add(event.exec_id)
    return tuple(sorted(executed - charged))


def _apply(lot: _LotState, fill: _Effective) -> int | None:
    """Average-cost update of ``lot`` by ``fill``; the realized P&L of the
    part that closed (None when nothing closed)."""
    qty, cost, fee = fill.quantity, -fill.cash, fill.fee
    if qty == 0:  # corrected away: whatever cash and fee remain are realized
        lot_realized = -cost - fee
        return lot_realized if (cost or fee) else None
    if lot.quantity == 0 or (lot.quantity > 0) == (qty > 0):
        lot.quantity += qty
        lot.cost += cost
        lot.fee += fee
        return None
    held = abs(lot.quantity)
    closed = min(abs(qty), held)
    released_cost = _part(lot.cost, closed, held)
    released_fee = _part(lot.fee, closed, held)
    closing_cost = _part(cost, closed, abs(qty))
    closing_fee = _part(fee, closed, abs(qty))
    lot.quantity += qty
    lot.cost += cost - closing_cost - released_cost
    lot.fee += fee - closing_fee - released_fee
    return -(released_cost + closing_cost) - released_fee - closing_fee


def symbols_from_executions(executions: Iterable[BrokerExecution]) -> dict[int, str]:
    """con_id -> symbol from the raw broker facts (ledger events carry no
    symbol); a contract reported under two symbols refuses."""
    out: dict[int, str] = {}
    for execution in executions:
        if out.setdefault(execution.con_id, execution.symbol) != execution.symbol:
            raise LedgerConflict(f"con_id {execution.con_id}: reported as two symbols")
    return out


# ------------------------------------------------------------------- files


def write_atomic(path: Path, data: bytes) -> None:
    """Replace ``path`` with ``data`` via a same-directory tmp file + rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def _parse_lines(data: bytes, path: Path, model: type[Any]) -> list[Any]:
    out = []
    for number, line in enumerate(data.split(b"\n"), start=1):
        if not line:
            continue
        try:
            out.append(model.model_validate_json(line))
        except (ValidationError, ValueError) as exc:
            raise LedgerConflict(f"{path}: line {number} is not a {model.__name__}") from exc
    return out


def read_ledger(path: Path) -> tuple[LedgerEvent, ...]:
    """Every event of ``events.jsonl`` (none when absent); a torn or
    invalid line refuses rather than being skipped."""
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return ()
    return tuple(_parse_lines(data, path, LedgerEvent))


def _append_jsonl(path: Path, model: type[Any], merge: Any) -> Any:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+b") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        fh.seek(0)
        held = _parse_lines(fh.read(), path, model)
        result, lines = merge(held)
        if lines:
            fh.seek(0, os.SEEK_END)
            fh.write(b"".join(line + b"\n" for line in lines))
            fh.flush()
            os.fsync(fh.fileno())
        return result


def append_ledger(path: Path, events: Iterable[LedgerEvent]) -> AppendResult:
    """Append new events to ``events.jsonl`` under an exclusive lock; a
    replay appends nothing, a conflict appends nothing and raises."""
    events = tuple(events)

    def merge(held: list[LedgerEvent]) -> tuple[AppendResult, list[bytes]]:
        result = append_events(held, events)
        return result, [model_bytes(e) for e in result.appended]

    appended: AppendResult = _append_jsonl(path, LedgerEvent, merge)
    return appended


def read_executions(path: Path) -> tuple[BrokerExecution, ...]:
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return ()
    return tuple(_parse_lines(data, path, BrokerExecution))


def append_executions(path: Path, executions: Iterable[BrokerExecution]) -> int:
    """Record each DISTINCT broker report once (a later copy of an exec id
    that now carries its commission is a new report); the count appended."""
    executions = tuple(executions)

    def merge(held: list[BrokerExecution]) -> tuple[int, list[bytes]]:
        seen = {model_bytes(e) for e in held}
        lines: list[bytes] = []
        for execution in executions:
            line = model_bytes(execution)
            if line not in seen:
                seen.add(line)
                lines.append(line)
        return len(lines), lines

    count: int = _append_jsonl(path, BrokerExecution, merge)
    return count


class HoldingsDocument(StrictModel):
    """``trex-holdings/1``: the canonical holdings projection of the ledger
    (``ledger/holdings.json``; schema note in the module doc)."""

    schema_version: Literal["trex-holdings/1"] = Field(default="trex-holdings/1", alias="schema")
    account_id: IdStr
    as_of: UTCInstant
    cash_micros: int = Field(strict=True)
    holdings: tuple[Holding, ...]

    @model_validator(mode="after")
    def _one_account_one_row_per_contract(self) -> HoldingsDocument:
        if any(h.account_id != self.account_id for h in self.holdings):
            raise ValueError("every holding is of the document's account")
        con_ids = [h.con_id for h in self.holdings]
        if len(con_ids) != len(set(con_ids)):
            raise ValueError("one holding row per con_id")
        return self


class HoldingsUnavailable(Exception):
    """``ledger/holdings.json`` cannot be read as a ``trex-holdings/1``."""

    def __init__(self, reason: Literal["missing", "unreadable", "invalid"], detail: str) -> None:
        super().__init__(f"holdings {reason}: {detail}")
        self.reason: Literal["missing", "unreadable", "invalid"] = reason


def holdings_document(
    fold: LedgerFold, *, account_id: str, symbols: Mapping[int, str], as_of: datetime
) -> HoldingsDocument:
    """The account's projection of ``fold`` (raises ``LedgerConflict`` for a
    holding the contract cannot represent: short stock, no symbol)."""
    return HoldingsDocument(
        account_id=account_id,
        as_of=as_of,
        cash_micros=fold.cash(account_id),
        holdings=fold.holdings(account_id, symbols),
    )


def write_holdings(
    run_dir: Wave2RunDir,
    fold: LedgerFold,
    *,
    account_id: str,
    symbols: Mapping[int, str],
    as_of: datetime,
) -> Path:
    """Atomically replace ``ledger/holdings.json`` (canonical bytes)."""
    document = holdings_document(fold, account_id=account_id, symbols=symbols, as_of=as_of)
    path = run_dir.holdings()
    write_atomic(path, model_bytes(document))
    return path


def _read_regular_file(path: Path, limit: int) -> bytes:
    """At most ``limit`` + 1 bytes of a regular file, never via a symlink."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError as exc:
        raise HoldingsUnavailable("missing", str(path)) from exc
    except OSError as exc:  # ELOOP: a symlink
        raise HoldingsUnavailable("unreadable", f"{path}: {exc.strerror}") from exc
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise HoldingsUnavailable("unreadable", f"{path}: not a regular file")
    with os.fdopen(fd, "rb") as fh:
        try:
            return fh.read(limit + 1)
        except OSError as exc:
            raise HoldingsUnavailable("unreadable", f"{path}: {exc.strerror}") from exc


def load_holdings(path: Path) -> HoldingsDocument:
    """The ``trex-holdings/1`` document at ``path`` (normally
    ``Wave2RunDir.holdings()``); raises ``HoldingsUnavailable`` with reason
    ``missing``, ``unreadable`` or ``invalid`` (see the module doc)."""
    raw = _read_regular_file(Path(path), MAX_HOLDINGS_BYTES)
    if len(raw) > MAX_HOLDINGS_BYTES:
        raise HoldingsUnavailable("unreadable", f"{path}: over {MAX_HOLDINGS_BYTES} bytes")
    try:
        document = json.loads(raw)
    except ValueError as exc:
        raise HoldingsUnavailable("unreadable", f"{path}: not JSON") from exc
    try:
        return HoldingsDocument.model_validate(document)
    except ValidationError as exc:
        raise HoldingsUnavailable("invalid", f"{path}: not a {HOLDINGS_SCHEMA}") from exc
