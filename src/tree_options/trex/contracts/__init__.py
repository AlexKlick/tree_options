"""Frozen cross-lane contracts for TREX Wave 2 (see docs/product/WAVE2-CONTRACTS.md).

Wave 2 = IBKR whole-share long-only equities + per-trade owner approval +
an entries-only pause + books/reconciliation. These modules are schemas and
pure helpers only: no I/O beyond path math, no broker contact, no
``ib_async`` import (the web command backend imports them).

| Id | Module | Schema ids |
| --- | --- | --- |
| C2.1 | ``effects`` | ``supervised-equity-effect/1``, ``EffectV1`` union |
| C2.2 | ``proposals``, ``commands`` | ``trex-entry-proposal/1``, ``trex-command/1``, ``trex-command-result/1`` |
| C2.3 | ``mandates`` | ``supervised-mandate/2`` |
| C2.4 | ``books`` | ``trex-ledger-event/1`` + broker fact models |
| C2.5 | ``risk`` | ``trex-risk-snapshot/1``, ``trex-reconciliation/1`` |
| C2.6 | ``desk_spec_v2``, ``layout`` | ``trex-desk-spec/2`` + run-dir layout |
| C2.7 | ``vocabulary`` | ``Blocker``, ``DeskEvent`` |

Waves 3 and 4 (docs/product/WAVE3-CONTRACTS.md) live in two submodules,
re-exported here as modules (their names are many and lane-facing; import
them as ``contracts.wave3`` / ``contracts.acceptance``):

| Id | Module | Schema ids |
| --- | --- | --- |
| C3.1-C3.3, C3.5-C3.7 | ``wave3`` | ``trex-family-command/1``, ``trex-today/1``, ``trex-activity/1``, ``trex-pilot-registration/1``, ``trex-pilot-usage/1`` |
| C3.4 | ``acceptance`` | ``trex-acceptance/1``, ``trex-acceptance-view/1`` |

The ``seal_registration`` command kind (the lane I amendment) is part of
``trex-command/1`` and exported with the other command names.
"""

from __future__ import annotations

# Waves 3 and 4: the submodules themselves (they import the Wave 2 submodules
# by full path, never this package's namespace, so the order is free)
from tree_options.trex.contracts import acceptance, wave3
from tree_options.trex.contracts.books import (
    DESK_REF_PREFIX,
    LEDGER_EVENT_SCHEMA,
    UNASSIGNED_SLEEVE,
    AttributedQuantity,
    Attribution,
    BrokerExecution,
    BrokerPortfolioItem,
    Holding,
    LedgerEvent,
    OrderRefOwner,
    base_exec_id_of,
    desk_order_ref,
    parse_order_ref,
)
from tree_options.trex.contracts.canonical import (
    IdempotencyKey,
    PathSafeId,
    Sha256Hex,
    UTCInstant,
    model_bytes,
    model_document,
    sha256_hex,
)
from tree_options.trex.contracts.commands import (
    COMMAND_ADAPTER,
    COMMAND_RESULT_SCHEMA,
    COMMAND_SCHEMA,
    Actor,
    AnyCommand,
    ApproveEntryArgs,
    ApproveEntryCommand,
    CommandAuthority,
    CommandAuthorityRefused,
    CommandKind,
    CommandResult,
    RawCommandRequest,
    SealRegistrationArgs,
    SealRegistrationCommand,
    TrexCommand,
    actor_binds_request,
    command_body_sha256,
    parse_command,
)
from tree_options.trex.contracts.desk_spec_v2 import (
    DESK_SPEC_V2_SCHEMA,
    DeskSpecV2,
    EquityDeskSpecV2,
    EquityPositionSpec,
    OptionDeskSpecV2,
    desk_spec_v2_for,
    parse_desk_spec,
)
from tree_options.trex.contracts.effects import (
    EFFECT_V1_ADAPTER,
    EQUITY_EFFECT_SCHEMA,
    OPTION_EFFECT_SCHEMA,
    EffectV1,
    EquityExitPolicy,
    SupervisedEffect,
    SupervisedEquityEffect,
    decode_effect_v1,
    effect_v1_bytes,
    effect_v1_sha256,
    equity_max_loss_cents,
    equity_notional_cents,
    equity_stop_loss_cents,
)
from tree_options.trex.contracts.layout import Wave2RunDir, Wave2SupervisedDir
from tree_options.trex.contracts.mandates import (
    MANDATE_V2_SCHEMA,
    SupervisedMandateV2,
    approval_blockers,
    approval_expiry,
    mandate_blockers,
    mandate_for_approval,
)
from tree_options.trex.contracts.proposals import (
    PROPOSAL_SCHEMA,
    EntryProposal,
    EntryProposalBody,
    Quote,
    ReviewFacts,
    proposal_bytes,
    proposal_id_of,
    proposal_sha256,
)
from tree_options.trex.contracts.risk import (
    RECONCILIATION_SCHEMA,
    RISK_SNAPSHOT_SCHEMA,
    OutsidePosition,
    PositionMismatch,
    Reconciliation,
    RiskSnapshot,
    reconciliation_blockers,
    risk_snapshot_blockers,
)
from tree_options.trex.contracts.vocabulary import Blocker, DeskEvent

__all__ = [
    "COMMAND_ADAPTER",
    "COMMAND_RESULT_SCHEMA",
    "COMMAND_SCHEMA",
    "DESK_REF_PREFIX",
    "DESK_SPEC_V2_SCHEMA",
    "EFFECT_V1_ADAPTER",
    "EQUITY_EFFECT_SCHEMA",
    "LEDGER_EVENT_SCHEMA",
    "MANDATE_V2_SCHEMA",
    "OPTION_EFFECT_SCHEMA",
    "PROPOSAL_SCHEMA",
    "RECONCILIATION_SCHEMA",
    "RISK_SNAPSHOT_SCHEMA",
    "UNASSIGNED_SLEEVE",
    "Actor",
    "AnyCommand",
    "ApproveEntryArgs",
    "ApproveEntryCommand",
    "AttributedQuantity",
    "Attribution",
    "Blocker",
    "BrokerExecution",
    "BrokerPortfolioItem",
    "CommandAuthority",
    "CommandAuthorityRefused",
    "CommandKind",
    "CommandResult",
    "DeskEvent",
    "DeskSpecV2",
    "EffectV1",
    "EntryProposal",
    "EntryProposalBody",
    "EquityDeskSpecV2",
    "EquityExitPolicy",
    "EquityPositionSpec",
    "Holding",
    "IdempotencyKey",
    "LedgerEvent",
    "OptionDeskSpecV2",
    "OrderRefOwner",
    "OutsidePosition",
    "PathSafeId",
    "PositionMismatch",
    "Quote",
    "RawCommandRequest",
    "Reconciliation",
    "ReviewFacts",
    "RiskSnapshot",
    "SealRegistrationArgs",
    "SealRegistrationCommand",
    "Sha256Hex",
    "SupervisedEffect",
    "SupervisedEquityEffect",
    "SupervisedMandateV2",
    "TrexCommand",
    "UTCInstant",
    "Wave2RunDir",
    "Wave2SupervisedDir",
    "acceptance",
    "actor_binds_request",
    "approval_blockers",
    "approval_expiry",
    "base_exec_id_of",
    "command_body_sha256",
    "decode_effect_v1",
    "desk_order_ref",
    "desk_spec_v2_for",
    "effect_v1_bytes",
    "effect_v1_sha256",
    "equity_max_loss_cents",
    "equity_notional_cents",
    "equity_stop_loss_cents",
    "mandate_blockers",
    "mandate_for_approval",
    "model_bytes",
    "model_document",
    "parse_command",
    "parse_desk_spec",
    "parse_order_ref",
    "proposal_bytes",
    "proposal_id_of",
    "proposal_sha256",
    "reconciliation_blockers",
    "risk_snapshot_blockers",
    "sha256_hex",
    "wave3",
]
