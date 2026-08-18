"""SQLite trial registry (INV-13): registration precedes outcome, always.

State machine: REGISTERED -> RUNNING -> COMPLETED | FAILED. Transitions are
single-step only; outcomes attach only to RUNNING trials; nothing is ever
overwritten. The `events` table is an append-only audit trail.

Scope evasion: `scope_key` must be a canonical TrialScope hash — a caller
cannot invent a fresh scope string to escape the 32-cap. The registry enforces
SOFTWARE ordering; it does not cryptographically prove a human never edited
the SQLite file.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from tree_options.registry.errors import (
    DuplicateTrialError,
    InvalidTransitionError,
    NonCanonicalScopeError,
    UnregisteredOutcomeError,
)
from tree_options.registry.errors import (
    OutcomeAlreadyRecordedError as OutcomeAlreadyRecordedError,  # re-export
)
from tree_options.registry.errors import (
    RegistryError as RegistryError,  # re-export
)
from tree_options.registry.errors import (
    ScopeBudgetExceededError as ScopeBudgetExceededError,  # re-export
)
from tree_options.registry.scope import TrialScope
from tree_options.schemas.trial import TrialRecord

if TYPE_CHECKING:  # typing only; avoids an import cycle
    from tree_options.registry.budget import TrialBudget

_SCHEMA = """
CREATE TABLE IF NOT EXISTS trials (
    trial_id            TEXT PRIMARY KEY,
    scope_key           TEXT NOT NULL,
    scope_json          TEXT NOT NULL,
    hypothesis          TEXT NOT NULL CHECK (length(hypothesis) >= 8),
    hyperparameters_json TEXT NOT NULL CHECK (json_valid(hyperparameters_json)),
    git_sha             TEXT NOT NULL,
    config_hash         TEXT NOT NULL,
    dataset_manifest_hash TEXT NOT NULL,
    registered_at       TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'REGISTERED'
                        CHECK (status IN ('REGISTERED','RUNNING','COMPLETED','FAILED')),
    outcome_at          TEXT,
    metrics_uri         TEXT,
    failure_reason      TEXT
);
CREATE INDEX IF NOT EXISTS idx_trials_scope ON trials(scope_key);
CREATE TABLE IF NOT EXISTS outcomes (
    trial_id    TEXT PRIMARY KEY,
    kind        TEXT NOT NULL CHECK (kind IN ('COMPLETED','FAILED')),
    detail_json TEXT NOT NULL CHECK (json_valid(detail_json)),
    ts          TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    trial_id    TEXT NOT NULL,
    kind        TEXT NOT NULL,
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
    ts          TEXT NOT NULL
);
"""

_TRANSITIONS = {"RUNNING": {"REGISTERED"}, "COMPLETED": {"RUNNING"}, "FAILED": {"RUNNING"}}


class TrialRegistry:
    def __init__(self, path: str | Path, *, budget: TrialBudget | None = None) -> None:
        # The cap is NOT optional (review F13): the registry owns a default
        # budget (32, matching the frozen protocol) even when the caller
        # supplies none.
        from tree_options.registry.budget import TrialBudget as _TB

        self.budget = budget or _TB()
        # check_same_thread=False: callers may hand a registry to a worker thread;
        # each connection is still used by one thread at a time (tests prove the
        # budget transaction holds under two concurrent connections).
        self._conn = sqlite3.connect(
            str(path), isolation_level=None, timeout=10.0, check_same_thread=False
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def _event(self, trial_id: str, kind: str, payload: dict, ts: datetime) -> None:
        self._conn.execute(
            "INSERT INTO events (trial_id, kind, payload_json, ts) VALUES (?, ?, ?, ?)",
            (trial_id, kind, json.dumps(payload, sort_keys=True, default=str), ts.isoformat()),
        )

    # -- reads ---------------------------------------------------------------

    def journal_mode(self) -> str:
        return str(self._conn.execute("PRAGMA journal_mode").fetchone()[0])

    def count_scope(self, scope_key: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM trials WHERE scope_key = ?", (scope_key,)
        ).fetchone()
        return int(row[0])

    def is_registered(self, trial_id: str) -> bool:
        return (
            self._conn.execute("SELECT 1 FROM trials WHERE trial_id = ?", (trial_id,)).fetchone()
            is not None
        )

    def status(self, trial_id: str) -> str:
        row = self._conn.execute(
            "SELECT status FROM trials WHERE trial_id = ?", (trial_id,)
        ).fetchone()
        if row is None:
            raise UnregisteredOutcomeError(trial_id)
        return str(row[0])

    def has_outcome(self, trial_id: str) -> bool:
        return self.status(trial_id) in {"COMPLETED", "FAILED"}

    def metrics_uri(self, trial_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT metrics_uri FROM trials WHERE trial_id = ?", (trial_id,)
        ).fetchone()
        if row is None:
            raise UnregisteredOutcomeError(trial_id)
        return None if row[0] is None else str(row[0])

    def scope_json(self, trial_id: str) -> str:
        row = self._conn.execute(
            "SELECT scope_json FROM trials WHERE trial_id = ?", (trial_id,)
        ).fetchone()
        if row is None:
            raise UnregisteredOutcomeError(trial_id)
        return str(row[0])

    def events(self, trial_id: str) -> tuple[tuple[str, str], ...]:
        rows = self._conn.execute(
            "SELECT kind, payload_json FROM events WHERE trial_id = ? ORDER BY seq",
            (trial_id,),
        ).fetchall()
        return tuple((str(k), str(p)) for k, p in rows)

    # -- writes ----------------------------------------------------------------

    def register(
        self,
        record: TrialRecord,
        scope: TrialScope | None = None,
        *,
        budget: TrialBudget | None = None,
    ) -> None:
        """REGISTERED insertion. The caller must PRESENT the TrialScope whose
        hash the record carries — the registry recomputes and compares, so a
        syntactically valid but fabricated scope_key is rejected. The budget
        ALWAYS applies (caller budget overrides the default). Budget check +
        insert share one transaction so racing writers cannot both land the
        (cap+1)-th config."""
        if scope is None:
            raise NonCanonicalScopeError(
                record.scope_key, "the TrialScope must be presented at registration"
            )
        if scope.scope_key() != record.scope_key:
            raise NonCanonicalScopeError(
                record.scope_key,
                "scope_key does not derive from the presented TrialScope",
            )
        effective_budget = budget or self.budget
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            effective_budget.check(self, record.scope_key)
            self._conn.execute(
                "INSERT INTO trials (trial_id, scope_key, scope_json, hypothesis,"
                " hyperparameters_json, git_sha, config_hash, dataset_manifest_hash,"
                " registered_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record.trial_id,
                    record.scope_key,
                    scope.canonical_json(),
                    record.hypothesis,
                    json.dumps(record.hyperparameters, sort_keys=True, default=str),
                    record.git_sha,
                    record.config_hash,
                    record.dataset_manifest_hash,
                    record.created_at.isoformat(),
                ),
            )
            self._event(
                record.trial_id, "REGISTERED", {"scope": record.scope_key}, record.created_at
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

    def mark_running(
        self,
        trial_id: str,
        *,
        git_sha: str,
        config_hash: str,
        dataset_manifest_hash: str,
        at: datetime,
    ) -> None:
        """REGISTERED -> RUNNING. The exact provenance hashes are required and
        must MATCH the registration — you cannot run under someone else's
        identity or revise your own after the fact."""
        row = self._conn.execute(
            "SELECT git_sha, config_hash, dataset_manifest_hash, status"
            " FROM trials WHERE trial_id = ?",
            (trial_id,),
        ).fetchone()
        if row is None:
            raise UnregisteredOutcomeError(trial_id)
        stored_git, stored_cfg, stored_ds, _current = row
        if (git_sha, config_hash, dataset_manifest_hash) != (
            stored_git,
            stored_cfg,
            stored_ds,
        ):
            raise InvalidTransitionError(
                trial_id, "provenance hashes do not match the registration"
            )
        self._transition(trial_id, "RUNNING", at, "provenance confirmed")

    def complete(self, trial_id: str, metrics_uri: str, *, outcome_at: datetime) -> None:
        """RUNNING -> COMPLETED. The only door for an outcome. The outcome is
        CLAIMED by inserting into the outcomes table whose PRIMARY KEY makes
        a second outcome physically impossible — racing completions yield
        exactly one winner and one COMPLETED event regardless of snapshot
        staleness (review F14)."""
        self._record_outcome(
            trial_id,
            "COMPLETED",
            {"metrics_uri": metrics_uri},
            outcome_at,
            "UPDATE trials SET outcome_at = ?, metrics_uri = ? WHERE trial_id = ?",
            (outcome_at.isoformat(), metrics_uri, trial_id),
        )

    def fail(self, trial_id: str, reason: str, *, at: datetime) -> None:
        """RUNNING -> FAILED (same single-outcome claim)."""
        self._record_outcome(
            trial_id,
            "FAILED",
            {"reason": reason},
            at,
            "UPDATE trials SET outcome_at = ?, failure_reason = ? WHERE trial_id = ?",
            (at.isoformat(), reason, trial_id),
        )

    def _record_outcome(
        self,
        trial_id: str,
        kind: str,
        payload: dict,
        at: datetime,
        extra_sql: str,
        extra_args: tuple,
    ) -> None:
        allowed_from = _TRANSITIONS[kind]
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            self._conn.execute(
                "INSERT INTO outcomes (trial_id, kind, detail_json, ts) VALUES (?, ?, ?, ?)",
                (trial_id, kind, json.dumps(payload, sort_keys=True), at.isoformat()),
            )
            cursor = self._conn.execute(
                "UPDATE trials SET status = ? WHERE trial_id = ? AND status IN "
                "(SELECT value FROM json_each(?))",
                (kind, trial_id, json.dumps(sorted(allowed_from))),
            )
            if cursor.rowcount != 1:
                current = self.status(trial_id)
                raise InvalidTransitionError(
                    trial_id, f"{current} -> {kind} is not a legal transition"
                )
            self._conn.execute(extra_sql, extra_args)
            self._event(trial_id, kind, payload, at)
            self._conn.execute("COMMIT")
        except sqlite3.IntegrityError:
            try:
                self._conn.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            raise InvalidTransitionError(trial_id, "outcome already claimed") from None
        except Exception:
            try:
                self._conn.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            raise

    def _transition(self, trial_id: str, to_status: str, at: datetime, payload) -> None:
        allowed_from = _TRANSITIONS[to_status]
        current = self.status(trial_id)
        if current not in allowed_from:
            raise InvalidTransitionError(
                trial_id, f"{current} -> {to_status} is not a legal transition"
            )
        payload_json = payload if isinstance(payload, dict) else {"detail": payload}
        self._conn.execute(
            "UPDATE trials SET status = ? WHERE trial_id = ? AND status IN "
            "(SELECT value FROM json_each(?))",
            (to_status, trial_id, json.dumps(sorted(allowed_from))),
        )
        self._event(trial_id, to_status, payload_json, at)

    # -- test/ops helper -------------------------------------------------------

    def _execute_raw(self, sql: str, params: tuple = ()) -> None:
        """Escape hatch for integrity tests: bypass the typed methods."""
        self._conn.execute(sql, params)
