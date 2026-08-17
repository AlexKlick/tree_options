"""SQLite trial registry (INV-13): registration precedes outcome, always.

The database is the durable witness of what was registered BEFORE any metric
was looked at. Writes go through typed methods; the schema's CHECK
constraints are the last line of defense (hypothesis length >= 8, valid
JSON hyperparameters). WAL mode so concurrent readers never block the writer.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from tree_options.registry.errors import (
    DuplicateTrialError,
    OutcomeAlreadyRecordedError,
    UnregisteredOutcomeError,
)
from tree_options.registry.errors import (
    RegistryError as RegistryError,  # re-exported: callers import it from here
)
from tree_options.registry.errors import (
    ScopeBudgetExceededError as ScopeBudgetExceededError,  # re-exported
)
from tree_options.schemas.trial import TrialRecord

if TYPE_CHECKING:  # typing only; avoids an import cycle
    from tree_options.registry.budget import TrialBudget

_SCHEMA = """
CREATE TABLE IF NOT EXISTS trials (
    trial_id            TEXT PRIMARY KEY,
    scope_key           TEXT NOT NULL,
    hypothesis          TEXT NOT NULL CHECK (length(hypothesis) >= 8),
    hyperparameters_json TEXT NOT NULL CHECK (json_valid(hyperparameters_json)),
    registered_at       TEXT NOT NULL,
    outcome_at          TEXT,
    metrics_uri         TEXT
);
CREATE INDEX IF NOT EXISTS idx_trials_scope ON trials(scope_key);
"""


class TrialRegistry:
    def __init__(self, path: str | Path) -> None:
        self._conn = sqlite3.connect(str(path), isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    # -- reads ---------------------------------------------------------------

    def journal_mode(self) -> str:
        return str(self._conn.execute("PRAGMA journal_mode").fetchone()[0])

    def count_scope(self, scope_key: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM trials WHERE scope_key = ?", (scope_key,)
        ).fetchone()
        return int(row[0])

    def is_registered(self, trial_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM trials WHERE trial_id = ?", (trial_id,)
        ).fetchone()
        return row is not None

    def has_outcome(self, trial_id: str) -> bool:
        row = self._conn.execute(
            "SELECT outcome_at FROM trials WHERE trial_id = ?", (trial_id,)
        ).fetchone()
        return row is not None and row[0] is not None

    def metrics_uri(self, trial_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT metrics_uri FROM trials WHERE trial_id = ?", (trial_id,)
        ).fetchone()
        if row is None:
            raise UnregisteredOutcomeError(trial_id)
        return None if row[0] is None else str(row[0])

    # -- writes ----------------------------------------------------------------

    def register(
        self,
        record: TrialRecord,
        *,
        budget: TrialBudget | None = None,
    ) -> None:
        """Insert a registration. Fails on duplicate id or exhausted budget.

        The budget check and insert are one transaction, so two racing
        writers cannot both land the (cap+1)-th config.
        """
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            if budget is not None:
                budget.check(self, record.scope_key)
            self._conn.execute(
                "INSERT INTO trials (trial_id, scope_key, hypothesis,"
                " hyperparameters_json, registered_at) VALUES (?, ?, ?, ?, ?)",
                (
                    record.trial_id,
                    record.scope_key,
                    record.hypothesis,
                    json.dumps(record.hyperparameters, sort_keys=True, default=str),
                    record.created_at.isoformat(),
                ),
            )
            self._conn.execute("COMMIT")
        except sqlite3.IntegrityError as exc:
            self._conn.execute("ROLLBACK")
            if "UNIQUE" in str(exc):
                raise DuplicateTrialError(record.trial_id) from None
            raise
        except Exception:
            try:
                self._conn.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            raise

    def record_outcome(self, trial_id: str, metrics_uri: str, *, outcome_at: datetime) -> None:
        """Attach an outcome to an ALREADY-registered trial."""
        if not self.is_registered(trial_id):
            raise UnregisteredOutcomeError(trial_id)
        cursor = self._conn.execute(
            "UPDATE trials SET outcome_at = ?, metrics_uri = ?"
            " WHERE trial_id = ? AND outcome_at IS NULL",
            (outcome_at.isoformat(), metrics_uri, trial_id),
        )
        if cursor.rowcount != 1:
            raise OutcomeAlreadyRecordedError(trial_id)

    # -- test/ops helper -------------------------------------------------------

    def _execute_raw(self, sql: str, params: tuple = ()) -> None:
        """Escape hatch for integrity tests: bypass the typed methods."""
        self._conn.execute(sql, params)
