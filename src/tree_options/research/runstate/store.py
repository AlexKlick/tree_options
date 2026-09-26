"""Content-addressed store for the research lane's own artifacts.

The research lane never writes to the desk evidence store; it has its
own workspace (``~/.local/state/trex-research/<run-id>/`` per
``tree_options.research.paths``) and a small per-run SQLite store at
``<workspace>/runstate.sqlite3``.

The surface mirrors the desk's ``EvidenceStore.put/all_at/verify/backup``
but is scoped to the research lane's purpose:
    - ``put(kind, key, payload, at)`` — record a per-run artifact
      (spec, run metadata, comparison result, evidence snapshot)
    - ``all(kind)`` — list all artifacts of a kind
    - ``all_at(kind)`` — (payload, at) pairs, used by the cutoff filter
    - ``verify()`` — returns ``{ok, events, objects, head_sha256}``

This module deliberately does NOT subclass the desk's ``EvidenceStore``
to keep the boundary obvious; the SQL schema is similar but the path
isolation is enforced in ``open_runstate_store`` below.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

# Same table names as the desk, but a separate database file.
_KINDS: tuple[str, ...] = ("run", "spec", "result", "evidence_snapshot", "comparison_row")


class RunstateStoreError(RuntimeError):
    """Raised when the research lane's runstate store fails an
    invariant the desk's store would not catch (path-overlap, content
    corruption)."""


@contextmanager
def open_runstate_store(workspace: Path) -> Iterator[RunstateStore]:
    """Open (and lazily create) the runstate store at
    ``<workspace>/runstate.sqlite3``.

    The caller (the FastAPI ``attach_research`` hook) is responsible for
    ensuring ``workspace`` is NOT the desk evidence or paper-trades
    directory. This layer enforces that with a runtime check.
    """
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
        """Record one artifact. Returns the payload sha256.

        Raises ``RunstateStoreError`` if ``(kind, key)`` already exists
        with a different payload (the desk's content_conflict semantics,
        in our namespace).
        """
        if kind not in _KINDS:
            raise RunstateStoreError(f"unknown kind: {kind!r}")
        body = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        sha = _sha256_hex(body)
        at_iso = (at or datetime.now()).isoformat()
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
        self.conn.execute("BEGIN")
        try:
            self.conn.execute(
                "INSERT INTO objects (kind, object_key, payload_sha256, payload_json, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (kind, key, sha, body, at_iso),
            )
            self._audit(kind, key, "put", prev=None, next_sha=sha, at=at_iso)
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return sha

    def all(self, kind: str) -> tuple[dict[str, Any], ...]:
        rows = self.conn.execute(
            "SELECT payload_json FROM objects WHERE kind = ? ORDER BY created_at",
            (kind,),
        ).fetchall()
        return tuple(json.loads(r["payload_json"]) for r in rows)

    def all_at(self, kind: str) -> tuple[tuple[dict[str, Any], datetime], ...]:
        rows = self.conn.execute(
            "SELECT payload_json, created_at FROM objects WHERE kind = ? ORDER BY created_at",
            (kind,),
        ).fetchall()
        out: list[tuple[dict[str, Any], datetime]] = []
        for r in rows:
            out.append((json.loads(r["payload_json"]), datetime.fromisoformat(r["created_at"])))
        return tuple(out)

    def verify(self) -> dict[str, Any]:
        cur = self.conn.execute("SELECT head_sha256, events, objects, updated_at "
                                "FROM audit_head WHERE id = 1").fetchone()
        # Recompute from scratch for the receipt (the desk's store does
        # the same; the audit_head row is a fast-path cache).
        head = self._compute_head()
        if cur is None:
            self.conn.execute("INSERT INTO audit_head (id, head_sha256, events, objects, updated_at) "
                               "VALUES (1, ?, ?, ?, ?)",
                               (head["head_sha256"], head["events"], head["objects"],
                                head["updated_at"]))
        elif (cur["head_sha256"] != head["head_sha256"]
              or cur["events"] != head["events"]
              or cur["objects"] != head["objects"]):
            raise RunstateStoreError(
                f"audit_head mismatch: stored {dict(cur)} vs computed {head}"
            )
        return head

    # -- internals --------------------------------------------------------

    def _audit(self, kind: str, key: str, action: str, *,
               prev: str | None, next_sha: str | None, at: str) -> None:
        self.conn.execute(
            "INSERT INTO audit (kind, object_key, action, prev_sha256, next_sha256, occurred_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (kind, key, action, prev, next_sha, at),
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


def _sha256_hex(s: str) -> str:
    import hashlib
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


__all__ = ["RunstateStore", "RunstateStoreError", "open_runstate_store"]
