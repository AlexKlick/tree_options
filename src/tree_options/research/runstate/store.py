"""Content-addressed store for the research lane's own artifacts.

The research lane never writes to the desk evidence store; it has its
own workspace (``~/.local/state/trex-research/<run-id>/`` per
``tree_options.research.paths``) and a small per-run SQLite store at
``<workspace>/runstate.sqlite3``.

The surface mirrors the desk's ``EvidenceStore.put/all_at/verify/backup``
but is scoped to the research lane's purpose:
    - ``put(kind, key, payload, at)`` — record an immutable artifact
      (spec, result); idempotent on identical payload, conflicting on
      different content. The audit head is updated INSIDE the same
      transaction as the insert.
    - ``replace(kind, key, payload, at)`` — append a new state snapshot
      for a MUTABLE record (run lifecycle); the previous payload is
      superseded but the audit trail keeps every version.
    - ``all(kind)`` / ``all_at(kind)`` — list artifacts of a kind
    - ``verify()`` — read-only integrity check (see below)

RL1-04 correction (2026-09-25 audit): the previous verifier compared a
cached audit head that legitimate writes never updated (so a valid
append after a first verify FAILED), and never rehashed the stored
payload bytes (so tampered ``payload_json`` with an untouched claimed
hash PASSED). Verification now recomputes, in one consistent snapshot:

    1. every object's payload hash (sha256 of the stored bytes vs the
       claimed ``payload_sha256``);
    2. object/audit correspondence (every object has at least one audit
       row; every audit row references an existing object);
    3. the full hash chain over audit rows, compared against the
       committed head.

The committed head is written only by mutating operations, in the same
transaction as their changes. ``verify()`` NEVER writes.

Receipt semantics: ``verify()`` attests LOCAL CONSISTENCY of this
database's contents — it is not an independently anchored authenticity
claim about how the artifacts came to exist.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from tree_options.research.paths import assert_no_overlap_with_desk

# Same table names as the desk, but a separate database file. RL-2
# adds two more immutable kinds for scenario lineage:
#   ``scenario_parent`` — the effective identity of a parent at fork
#         attach-time (ParentRef)
#   ``child``          — parent_run_id -> child_run_id pointer (ChildRef)
# Both are immutable keys, written via the same ``put`` discipline.
_KINDS: tuple[str, ...] = (
    "run", "spec", "result", "evidence_snapshot", "comparison_row",
    "scenario_parent", "child",
)


class RunstateStoreError(RuntimeError):
    """Raised when the research lane's runstate store fails an
    invariant (path-overlap, tampering, corruption, content conflict)."""


@contextmanager
def open_runstate_store(workspace: Path) -> Iterator[RunstateStore]:
    """Open (and lazily create) the runstate store at
    ``<workspace>/runstate.sqlite3``.

    The ACTUAL resolved workspace is validated here against the desk
    trees (RL1-04: the explicit argument is checked, not just
    environment-derived defaults — a workspace passed by a caller counts
    exactly as much as one read from the environment).
    """
    assert_no_overlap_with_desk(workspace=workspace)
    db_path = workspace / "runstate.sqlite3"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    store = RunstateStore(db_path)
    try:
        store._init_schema()
        yield store
    finally:
        store.close()


class RunstateStore:
    """Minimal content-addressed store for the research lane.

    NOT a subclass of ``tree_options.desk.evidence.EvidenceStore`` —
    the SQL surface is similar but the boundary is explicit.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.conn = sqlite3.connect(str(path), isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self._initialized = False

    def close(self) -> None:
        self.conn.close()

    def _init_schema(self) -> None:
        if self._initialized:
            return
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS objects (
                kind TEXT NOT NULL,
                object_key TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (kind, object_key)
            );
            CREATE TABLE IF NOT EXISTS audit (
                audit_seq INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                object_key TEXT NOT NULL,
                action TEXT NOT NULL,
                prev_sha256 TEXT,
                next_sha256 TEXT,
                occurred_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS audit_head (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                head_sha256 TEXT NOT NULL,
                events INTEGER NOT NULL,
                objects INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            );
        """)
        self._initialized = True

    # -- application -----------------------------------------------------

    def put(self, kind: str, payload: dict[str, Any], *,
            key: str, at: datetime | None = None) -> str:
        """Record one immutable artifact. Returns the payload sha256.

        Idempotent on identical content. Raises ``RunstateStoreError``
        if ``(kind, key)`` already exists with a different payload (the
        desk's content_conflict semantics, in our namespace).
        """
        if kind not in _KINDS:
            raise RunstateStoreError(f"unknown kind: {kind!r}")
        sha, body, at_iso = _canonical_entry(payload, at)
        cur = self.conn.execute(
            "SELECT payload_sha256 FROM objects WHERE kind = ? AND object_key = ?",
            (kind, key),
        ).fetchone()
        if cur is not None:
            if cur["payload_sha256"] == sha:
                return sha  # idempotent re-write
            raise RunstateStoreError(
                f"content_conflict: {kind}/{key} already exists with a "
                "different payload"
            )
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.execute(
                "INSERT INTO objects (kind, object_key, payload_sha256, payload_json, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (kind, key, sha, body, at_iso),
            )
            self._audit(kind, key, "put", prev=None, next_sha=sha, at=at_iso)
            self._refresh_head_locked()
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return sha

    def replace(self, kind: str, payload: dict[str, Any], *,
                key: str, at: datetime | None = None) -> str:
        """Publish a new state snapshot for a MUTABLE record (run
        lifecycle). Requires the key to exist; the full audit trail
        keeps every prior version. Returns the new payload sha256.
        """
        if kind not in _KINDS:
            raise RunstateStoreError(f"unknown kind: {kind!r}")
        sha, body, at_iso = _canonical_entry(payload, at)
        cur = self.conn.execute(
            "SELECT payload_sha256 FROM objects WHERE kind = ? AND object_key = ?",
            (kind, key),
        ).fetchone()
        if cur is None:
            raise RunstateStoreError(f"no such object: {kind}/{key} to replace")
        prev_sha: str = cur["payload_sha256"]
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.execute(
                "UPDATE objects SET payload_sha256 = ?, payload_json = ?, created_at = ? "
                "WHERE kind = ? AND object_key = ?",
                (sha, body, at_iso, kind, key),
            )
            self._audit(kind, key, "replace", prev=prev_sha, next_sha=sha, at=at_iso)
            self._refresh_head_locked()
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return sha

    def get(self, kind: str, key: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT payload_json FROM objects WHERE kind = ? AND object_key = ?",
            (kind, key),
        ).fetchone()
        return json.loads(row["payload_json"]) if row is not None else None

    def get_kind_payload_sha(self, kind: str, key: str) -> str | None:
        """Read the stored payload sha for an existing object (immutable
        kind). Returns ``None`` if no such object exists. Used by
        idempotent lineage writes to confirm the prior payload matched
        without writing again (RL-2)."""
        cur = self.conn.execute(
            "SELECT payload_sha256 FROM objects WHERE kind = ? AND object_key = ?",
            (kind, key),
        ).fetchone()
        return cur["payload_sha256"] if cur is not None else None

    def all(self, kind: str) -> tuple[dict[str, Any], ...]:
        rows = self.conn.execute(
            "SELECT payload_json FROM objects WHERE kind = ? ORDER BY created_at",
            (kind,),
        ).fetchall()
        return tuple(json.loads(r["payload_json"]) for r in rows)

    def all_at(self, kind: str) -> tuple[tuple[dict[str, Any], str], ...]:
        """Payloads with their recorded timestamps as ISO strings (the
        desk store's convention — callers normalize once)."""
        rows = self.conn.execute(
            "SELECT payload_json, created_at FROM objects WHERE kind = ? ORDER BY created_at",
            (kind,),
        ).fetchall()
        return tuple(
            (json.loads(r["payload_json"]), r["created_at"]) for r in rows
        )

    def verify(self) -> dict[str, Any]:
        """Read-only integrity verification (never writes). Raises
        ``RunstateStoreError`` on any tampering, drift, or corruption.
        """
        self.conn.execute("BEGIN DEFERRED")  # one consistent snapshot
        try:
            # 1) every object's payload bytes hash to its claimed hash
            for row in self.conn.execute(
                "SELECT kind, object_key, payload_sha256, payload_json FROM objects"
            ):
                actual = _sha256_hex(row["payload_json"])
                if actual != row["payload_sha256"]:
                    raise RunstateStoreError(
                        f"payload_tampering: {row['kind']}/{row['object_key']} "
                        f"stored bytes hash to {actual} but claim "
                        f"{row['payload_sha256']}"
                    )
            # 2) object/audit correspondence
            object_keys = {
                (r["kind"], r["object_key"])
                for r in self.conn.execute("SELECT kind, object_key FROM objects")
            }
            audited_keys = {
                (r["kind"], r["object_key"])
                for r in self.conn.execute("SELECT DISTINCT kind, object_key FROM audit")
            }
            missing_audit = object_keys - audited_keys
            if missing_audit:
                raise RunstateStoreError(
                    f"orphan_objects with no audit trail: {sorted(missing_audit)}"
                )
            dangling_audit = audited_keys - object_keys
            if dangling_audit:
                raise RunstateStoreError(
                    f"audit_rows referencing missing objects: {sorted(dangling_audit)}"
                )
            # 3) chain + committed head agreement
            head = self._compute_head()
            cur = self.conn.execute(
                "SELECT head_sha256, events, objects, updated_at "
                "FROM audit_head WHERE id = 1"
            ).fetchone()
            if cur is None:
                if head["events"] or head["objects"]:
                    raise RunstateStoreError(
                        "missing audit_head: store has content but no "
                        "committed head"
                    )
            elif (cur["head_sha256"] != head["head_sha256"]
                  or cur["events"] != head["events"]
                  or cur["objects"] != head["objects"]):
                raise RunstateStoreError(
                    f"audit_head mismatch: committed {dict(cur)} vs "
                    f"computed {head}"
                )
        finally:
            self.conn.execute("COMMIT")
        return {
            "ok": True,
            "head_sha256": head["head_sha256"],
            "events": head["events"],
            "objects": head["objects"],
            "scope": "local-consistency",
        }

    # -- internals --------------------------------------------------------

    def _audit(self, kind: str, key: str, action: str, *,
               prev: str | None, next_sha: str | None, at: str) -> None:
        self.conn.execute(
            "INSERT INTO audit (kind, object_key, action, prev_sha256, next_sha256, occurred_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (kind, key, action, prev, next_sha, at),
        )

    def _refresh_head_locked(self) -> None:
        """Recompute and commit the audit head inside the caller's open
        transaction — the head can never lag the content it summarizes
        (RL1-04: a stale head made legitimate appends fail verify)."""
        head = self._compute_head()
        self.conn.execute(
            "INSERT INTO audit_head (id, head_sha256, events, objects, updated_at) "
            "VALUES (1, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET head_sha256 = excluded.head_sha256, "
            "events = excluded.events, objects = excluded.objects, "
            "updated_at = excluded.updated_at",
            (head["head_sha256"], head["events"], head["objects"],
             head["updated_at"]),
        )

    def _compute_head(self) -> dict[str, Any]:
        # Hash chains audit rows together (similar to the desk's
        # EvidenceStore pattern, but simpler — no transient / no backup).
        prev = "0" * 64
        events = 0
        for r in self.conn.execute(
            "SELECT kind, object_key, action, prev_sha256, next_sha256, occurred_at "
            "FROM audit ORDER BY audit_seq"
        ):
            row_str = "|".join((r["kind"], r["object_key"], r["action"],
                                r["prev_sha256"] or "", r["next_sha256"] or "",
                                r["occurred_at"]))
            h = _sha256_hex(prev + "|" + row_str)
            prev = h
            events += 1
        objects = self.conn.execute("SELECT COUNT(*) AS n FROM objects").fetchone()["n"]
        return {
            "head_sha256": prev,
            "events": events,
            "objects": objects,
            "updated_at": datetime.now().isoformat(),
        }


def _canonical_entry(payload: dict[str, Any], at: datetime | None) -> tuple[str, str, str]:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    sha = _sha256_hex(body)
    at_iso = (at or datetime.now()).isoformat()
    return sha, body, at_iso


def _sha256_hex(s: str) -> str:
    import hashlib
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


__all__ = ["RunstateStore", "RunstateStoreError", "open_runstate_store"]
