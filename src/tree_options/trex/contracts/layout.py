"""C2.6 layout: where Wave 2 state lives. Pure path math, no I/O.

Two roots, both pre-existing:

- the desk RUN DIR (``TREX_DESK_RUN_DIR``, default
  ``~/.local/state/trex/desk-paper``; ``DeskPaths.root``): kill files,
  specs, the entry inbox, and the new ``PAUSE_ENTRIES`` file plus
  ``commands/``, ``proposals/`` and ``ledger/``;
- the SUPERVISED STATE DIR (``TREX_SUPERVISED_DIR``, default
  ``~/.local/state/trex/supervised``; ``SupervisedPaths.root``): authority.
  Per-trade ``mandates/`` live HERE, under the same advisory lock that
  ``issue_permit`` holds, so spending a mandate and minting its permit
  cannot race across directories.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final

__all__ = [
    "COMMANDS_DIR",
    "FLATTEN",
    "HALT",
    "LEDGER_DIR",
    "MANDATES_DIR",
    "PAUSE_ENTRIES",
    "PROPOSALS_DIR",
    "REQUESTS_DIR",
    "SPECS_DIR",
    "Wave2RunDir",
    "Wave2SupervisedDir",
]

# -- run dir --------------------------------------------------------------
#: Kill file: no new ENTRIES (exits, fills and reconciliation continue).
PAUSE_ENTRIES: Final = "PAUSE_ENTRIES"
#: Pre-existing kill files (desk_runtime): HALT gates exits too; FLATTEN closes.
HALT: Final = "HALT"
FLATTEN: Final = "FLATTEN"
COMMANDS_DIR: Final = "commands"
PROPOSALS_DIR: Final = "proposals"
LEDGER_DIR: Final = "ledger"
SPECS_DIR: Final = "specs"
REQUESTS_DIR: Final = "requests"

# -- supervised state dir -------------------------------------------------
MANDATES_DIR: Final = "mandates"


@dataclass(frozen=True)
class Wave2RunDir:
    """Wave 2 files under the desk run dir."""

    root: Path

    def pause_entries(self) -> Path:
        return self.root / PAUSE_ENTRIES

    def halt(self) -> Path:
        return self.root / HALT

    def flatten(self) -> Path:
        return self.root / FLATTEN

    def spec(self, structure_id: str) -> Path:
        """``trex-desk-spec/1`` or ``/2``; an equity position's id is its intent id."""
        return self.root / SPECS_DIR / f"{structure_id}.json"

    def commands(self) -> Path:
        return self.root / COMMANDS_DIR

    def command(self, idempotency_key: str) -> Path:
        """A durable ``trex-command/1`` (written once; the key is the name)."""
        return self.commands() / f"{idempotency_key}.json"

    def command_result(self, idempotency_key: str) -> Path:
        """The ``trex-command-result/1`` for the command (latest status)."""
        return self.commands() / f"{idempotency_key}.result.json"

    def proposals(self) -> Path:
        return self.root / PROPOSALS_DIR

    def proposal(self, proposal_id: str) -> Path:
        """A ``trex-entry-proposal/1`` in its canonical bytes."""
        return self.proposals() / f"{proposal_id}.json"

    def proposal_rejected(self, proposal_id: str) -> Path:
        """Tombstone written by ``reject_entry`` (the proposal file stays)."""
        return self.proposals() / f"{proposal_id}.rejected.json"

    def ledger(self) -> Path:
        return self.root / LEDGER_DIR

    def broker_executions(self) -> Path:
        """Append-only ``BrokerExecution`` facts as received (JSONL)."""
        return self.ledger() / "executions.jsonl"

    def ledger_events(self) -> Path:
        """Append-only ``trex-ledger-event/1`` stream (JSONL)."""
        return self.ledger() / "events.jsonl"

    def holdings(self) -> Path:
        """Latest projection of the ledger into ``Holding`` rows."""
        return self.ledger() / "holdings.json"

    def risk_snapshot(self) -> Path:
        """Latest ``trex-risk-snapshot/1``."""
        return self.ledger() / "risk-snapshot.json"

    def reconciliation(self) -> Path:
        """Latest ``trex-reconciliation/1``."""
        return self.ledger() / "reconciliation.json"


@dataclass(frozen=True)
class Wave2SupervisedDir:
    """Wave 2 files under the supervised state dir (authority)."""

    root: Path

    def mandates(self) -> Path:
        return self.root / MANDATES_DIR

    def mandate(self, intent_id: str) -> Path:
        """A granted ``supervised-mandate/2`` (one per approved intent)."""
        return self.mandates() / f"{intent_id}.json"

    def mandate_consumed(self, intent_id: str) -> Path:
        """The same file after its permit was minted: rename = consumption."""
        return self.mandates() / f"{intent_id}.consumed.json"
