"""Process-lifetime account-alias fence shared by supervised broker runtimes."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import uuid
from pathlib import Path
from typing import BinaryIO

from tree_options.trex.supervised import SupervisedRefused


class AccountOwnership:
    def __init__(self, root: Path, alias: str, *, epoch: str | None = None) -> None:
        if not alias:
            raise ValueError("account alias required")
        self.root = root
        self.alias = alias
        self.epoch = epoch or uuid.uuid4().hex
        self._handle: BinaryIO | None = None
        self.path = root / (hashlib.sha256(alias.encode()).hexdigest() + ".lock")

    @property
    def held(self) -> bool:
        return self._handle is not None

    def acquire(self) -> None:
        if self.held:
            raise SupervisedRefused("account_already_owned")
        self.root.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            raise SupervisedRefused("account_already_owned", self.alias) from None
        self._handle = handle
        handle.seek(0)
        handle.truncate()
        handle.write(
            json.dumps(
                {"account_alias": self.alias, "owner_epoch": self.epoch, "pid": os.getpid()}
            ).encode()
        )
        handle.flush()
        os.fsync(handle.fileno())

    def close(self) -> None:
        if self._handle is not None:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            self._handle.close()
            self._handle = None


def ownership_root() -> Path:
    return Path(
        os.environ.get("TREX_ACCOUNT_OWNERS", Path.home() / ".local/state/trex-account-owners")
    )
