"""Small file primitives of the pilot dir: bounded regular-file reads, atomic
rewrites (drafts, usage counters) and write-once creation (seals, job
markers). Files are 0600; a symlink is never followed."""

from __future__ import annotations

import contextlib
import fcntl
import os
import stat
import tempfile
from collections.abc import Iterator
from pathlib import Path

#: no pilot document is bigger than this (a registration is a few KiB)
MAX_DOCUMENT_BYTES = 256 * 1024
FILE_MODE = 0o600


def read_regular(path: Path, max_bytes: int = MAX_DOCUMENT_BYTES) -> bytes:
    """The bytes of a regular file, refusing links, special files and
    oversized files (``OSError`` / ``ValueError``)."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"{path.name} is not a regular file")
        if info.st_size > max_bytes:
            raise ValueError(f"{path.name} is larger than {max_bytes} bytes")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            data = handle.read(max_bytes + 1)
    finally:
        os.close(fd)
    if len(data) > max_bytes:
        raise ValueError(f"{path.name} is larger than {max_bytes} bytes")
    return data


def _fsync_dir(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_atomic(path: Path, payload: bytes) -> None:
    """Replace ``path`` with ``payload`` (temp file in the same dir, fsync,
    rename): a reader sees the old or the new bytes, never a torn file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, FILE_MODE)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise
    _fsync_dir(path.parent)


def create_once(path: Path, payload: bytes) -> None:
    """Create ``path`` with ``payload``; ``FileExistsError`` when anything
    (a file, a link) already stands there. Never rewrites."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, FILE_MODE)
    try:
        with os.fdopen(fd, "wb", closefd=False) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(fd)
    _fsync_dir(path.parent)


@contextlib.contextmanager
def locked(lock_path: Path) -> Iterator[None]:
    """An exclusive advisory lock held for the block (one per program)."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
