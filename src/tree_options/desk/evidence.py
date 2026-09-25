"""Local transactional evidence custody, separate from the broker's book.

Objects are append-only: same key + same content is a replay; changed content is
an error. The object and its hash-chain event commit together. This detects
accidental/casual tampering, not deletion of a whole suffix by an attacker who
also replaces the database and backups. Anchor the audit head off-host for that.
Use a local filesystem, not an NFS/SMB share, for the active SQLite database.
"""
from __future__ import annotations

import contextlib
import os
import sqlite3
import tempfile
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

from tree_options.desk.contracts import canonical, digest, read_json, timestamp

SCHEMA_VERSION = 1
GENESIS = "0" * 64
_DDL = """
CREATE TABLE objects (
    kind TEXT NOT NULL, object_key TEXT NOT NULL, payload TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL, PRIMARY KEY(kind, object_key)
);
CREATE TABLE audit (
    seq INTEGER PRIMARY KEY, occurred_at TEXT NOT NULL,
    kind TEXT NOT NULL, object_key TEXT NOT NULL, payload_sha256 TEXT NOT NULL,
    previous_sha256 TEXT NOT NULL, event_sha256 TEXT NOT NULL,
    UNIQUE(kind, object_key)
);
"""


class EvidenceError(RuntimeError):
    pass


class EvidenceStore:
    def __init__(self, path: Path, *, readonly: bool = False, transient: bool = False) -> None:
        self.path = Path(path)
        self.readonly = readonly
        if self.path.is_symlink():
            raise EvidenceError("database_symlink")
        if transient:
            self.conn = sqlite3.connect(":memory:", isolation_level=None)
            if self.path.exists():
                source = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)
                try:
                    source.backup(self.conn)
                finally:
                    source.close()
        elif readonly:
            self.conn = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, isolation_level=None)
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            # Create restrictively before SQLite opens the file (and its WAL).
            fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            os.close(fd)
            self.conn = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.execute("PRAGMA foreign_keys=ON")
        try:
            if not readonly:
                self.conn.execute("PRAGMA journal_mode=WAL")
                self.conn.execute("PRAGMA synchronous=FULL")
                self.conn.execute("BEGIN IMMEDIATE")
            version = self.conn.execute("PRAGMA user_version").fetchone()[0]
            if version == 0 and not readonly:
                if self.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table'").fetchone():
                    raise EvidenceError("unknown_database")
                for statement in _DDL.split(";"):
                    if statement.strip():
                        self.conn.execute(statement)
                self.conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            elif version != SCHEMA_VERSION:
                raise EvidenceError("unsupported_database_version")
            if not readonly:
                self.conn.commit()
            else:
                self.conn.execute("PRAGMA query_only=ON")
        except BaseException:
            self.conn.close()
            raise

    def __enter__(self) -> EvidenceStore:
        return self

    def __exit__(self, *args: Any) -> None:
        self.conn.close()

    @contextlib.contextmanager
    def atomic(self) -> Iterator[None]:
        if self.readonly or self.conn.in_transaction:
            raise EvidenceError("transaction_state")
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.conn.commit()
        except BaseException:
            self.conn.rollback()
            raise

    def get(self, kind: str, key: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT payload FROM objects WHERE kind=? AND object_key=?", (kind, key)).fetchone()
        return read_json(row[0].encode()) if row else None

    def latest_key(self, kind: str) -> str | None:
        row = self.conn.execute("SELECT MAX(object_key) FROM objects WHERE kind=?", (kind,)).fetchone()
        return row[0]

    def all(self, kind: str) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT payload FROM objects WHERE kind=? ORDER BY object_key", (kind,))
        return [read_json(r[0].encode()) for r in rows]

    def put(self, kind: str, key: str, payload: dict[str, Any], at: datetime) -> bool:
        if self.readonly or not self.conn.in_transaction:
            raise EvidenceError("transaction_required")
        timestamp(at.isoformat())
        encoded, sha = canonical(payload).decode(), digest(payload)
        old = self.conn.execute("SELECT payload_sha256 FROM objects WHERE kind=? AND object_key=?", (kind, key)).fetchone()
        if old:
            if old[0] != sha:
                raise EvidenceError("content_conflict")
            return False
        previous = self.conn.execute("SELECT seq, event_sha256 FROM audit ORDER BY seq DESC LIMIT 1").fetchone()
        event = {"seq": previous[0] + 1 if previous else 1, "occurred_at": at.isoformat(),
                 "kind": kind, "object_key": key, "payload_sha256": sha,
                 "previous_sha256": previous[1] if previous else GENESIS}
        self.conn.execute("INSERT INTO objects VALUES (?,?,?,?)", (kind, key, encoded, sha))
        self.conn.execute("INSERT INTO audit VALUES (?,?,?,?,?,?,?)", (*event.values(), digest(event)))
        return True

    def verify(self) -> dict[str, Any]:
        # A read transaction gives one consistent view even while a writer commits.
        owned = not self.conn.in_transaction
        if owned:
            self.conn.execute("BEGIN")
        try:
            if self.conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise EvidenceError("sqlite_integrity")
            previous, count = GENESIS, 0
            seen: dict[tuple[str, str], str] = {}
            for row in self.conn.execute("SELECT * FROM audit ORDER BY seq"):
                obj = dict(row)
                claimed = obj.pop("event_sha256")
                count += 1
                if obj["seq"] != count or obj["previous_sha256"] != previous or digest(obj) != claimed:
                    raise EvidenceError("audit_chain")
                seen[(obj["kind"], obj["object_key"])] = obj["payload_sha256"]
                previous = claimed
            objects = 0
            for row in self.conn.execute("SELECT * FROM objects"):
                sha = digest(read_json(row["payload"].encode()))
                if sha != row["payload_sha256"] or seen.get((row["kind"], row["object_key"])) != sha:
                    raise EvidenceError("object_integrity")
                objects += 1
            if objects != count:
                raise EvidenceError("object_event_count")
            return {"ok": True, "events": count, "objects": objects, "head_sha256": previous}
        finally:
            if owned:
                self.conn.rollback()

    def backup(self, destination: Path) -> None:
        if self.conn.in_transaction:
            raise EvidenceError("backup_in_transaction")
        if destination.exists() or destination.resolve() == self.path.resolve():
            raise EvidenceError("backup_exists")
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, tmp = tempfile.mkstemp(prefix=".desk-backup-", dir=destination.parent)
        os.close(fd)
        try:
            target = sqlite3.connect(tmp)
            try:
                self.conn.backup(target)
                target.execute("PRAGMA journal_mode=DELETE")
            finally:
                target.close()
            # Verify the exact snapshot before publishing its destination name.
            with EvidenceStore(Path(tmp), readonly=True) as snapshot:
                snapshot.verify()
            with open(tmp, "rb") as fh:
                os.fsync(fh.fileno())
            os.link(tmp, destination)  # no overwrite, even with a concurrent backup
            dfd = os.open(destination.parent, os.O_DIRECTORY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        finally:
            for suffix in ("", "-wal", "-shm", "-journal"):
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(tmp + suffix)
