"""Ledger entries (INV-12): signed cash impacts with exact provenance."""

from __future__ import annotations

from datetime import date
from typing import Literal

from tree_options.schemas.common import IdStr, Money, StrictModel, UTCDatetime

EntryKind = Literal[
    "fill_notional",
    "fee",
    "exercise_settlement",
    "assignment",
    "corporate_action",
    "cash_adjustment",
]


class LedgerEntry(StrictModel):
    entry_id: IdStr
    ts: UTCDatetime
    session: date
    kind: EntryKind
    amount: Money  # signed cash impact
    contract_id: IdStr | None = None
    ref_id: IdStr  # fill_id / corporate_action_id / ...
