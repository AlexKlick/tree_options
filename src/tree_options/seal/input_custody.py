"""Read-only, no-follow custody for the files entering a G4 seal packet.

Every path is walked lexically from ``/``. Directory components and final
files are opened relative to held directory descriptors with ``O_NOFOLLOW``;
regular-file type, link count, inode/name identity, stable metadata, and the
continued reachability of held directories are checked before bytes escape.
"""

from __future__ import annotations

import errno
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import NoReturn

from tree_options.seal.errors import VerifiedInputsError

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def _refuse(component: str, detail: str) -> NoReturn:
    raise VerifiedInputsError(component, detail)


def _absolute_lexical(path: Path) -> Path:
    """Collapse dot components without resolving or erasing symlinks."""
    return Path(os.path.abspath(os.fspath(path)))


def _one_name(name: str, *, component: str, purpose: str) -> None:
    if not name or name in {".", ".."} or Path(name).name != name or Path(name).is_absolute():
        _refuse(component, f"{purpose} name {name!r} is not one lexical path component")


def _validate_directory(st: os.stat_result, *, component: str, purpose: str) -> None:
    if not stat.S_ISDIR(st.st_mode):
        _refuse(component, f"{purpose} is not a directory")


def _validate_regular(st: os.stat_result, *, component: str, purpose: str) -> None:
    if not stat.S_ISREG(st.st_mode):
        _refuse(component, f"{purpose} is not a regular file (no-follow custody refused it)")
    if st.st_nlink != 1:
        _refuse(component, f"{purpose} has link count {st.st_nlink}, expected exactly 1")


def _open_directory(path: Path, *, component: str, purpose: str) -> int:
    absolute = _absolute_lexical(path)
    fd = os.open(os.sep, os.O_RDONLY | os.O_DIRECTORY)
    for name in absolute.parts[1:]:
        previous = fd
        try:
            fd = os.open(name, _DIR_FLAGS, dir_fd=previous)
        except OSError as exc:
            os.close(previous)
            if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                _refuse(
                    component,
                    f"{purpose} component {name!r} in {absolute} is a symlink or "
                    "non-directory (component-wise no-follow walk)",
                )
            if exc.errno == errno.ENOENT:
                _refuse(component, f"{purpose} {absolute} does not exist")
            _refuse(component, f"{purpose} {absolute} is unreadable ({exc})")
        os.close(previous)
    return fd


def _verify_directory_path(
    path: Path, held_fd: int, *, component: str, purpose: str
) -> None:
    try:
        fresh_fd = _open_directory(path, component=component, purpose=purpose)
    except VerifiedInputsError as exc:
        _refuse(component, f"{purpose} identity is no longer reachable ({exc.detail})")
    try:
        held = os.fstat(held_fd)
        fresh = os.fstat(fresh_fd)
        if (held.st_dev, held.st_ino) != (fresh.st_dev, fresh.st_ino):
            _refuse(component, f"{purpose} identity changed while its inputs were read")
    finally:
        os.close(fresh_fd)


def _verify_child_directory(
    parent_fd: int,
    name: str,
    child_fd: int,
    *,
    component: str,
    purpose: str,
) -> None:
    try:
        named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        _refuse(component, f"{purpose} directory {name!r} vanished while held")
    held = os.fstat(child_fd)
    _validate_directory(named, component=component, purpose=f"{purpose} name {name!r}")
    if (held.st_dev, held.st_ino) != (named.st_dev, named.st_ino):
        _refuse(component, f"{purpose} directory {name!r} was replaced while held")


def _open_child_directory(
    parent_fd: int,
    name: str,
    *,
    component: str,
    purpose: str,
    allow_missing: bool = False,
) -> int | None:
    _one_name(name, component=component, purpose=purpose)
    try:
        return os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
    except OSError as exc:
        if exc.errno == errno.ENOENT and allow_missing:
            return None
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            _refuse(
                component,
                f"{purpose} directory {name!r} is a symlink or non-directory "
                "(O_DIRECTORY|O_NOFOLLOW)",
            )
        if exc.errno == errno.ENOENT:
            _refuse(component, f"{purpose} directory {name!r} is missing")
        _refuse(component, f"{purpose} directory {name!r} is unreadable ({exc})")


def _read_all(fd: int) -> bytes:
    chunks: list[bytes] = []
    offset = 0
    while True:
        chunk = os.pread(fd, 65536, offset)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        offset += len(chunk)


def _stable_file_fields(st: os.stat_result) -> tuple[int, int, int, int, int]:
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def _read_named(
    parent_fd: int,
    name: str,
    *,
    component: str,
    purpose: str,
) -> bytes:
    _one_name(name, component=component, purpose=purpose)
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            _refuse(component, f"{purpose} {name!r} is a symlink; no-follow custody refused it")
        if exc.errno == errno.ENOENT:
            _refuse(component, f"{purpose} {name!r} is missing")
        _refuse(component, f"{purpose} {name!r} is unreadable ({exc})")
    try:
        before = os.fstat(fd)
        _validate_regular(before, component=component, purpose=f"held {purpose} {name!r}")
        raw = _read_all(fd)
        after = os.fstat(fd)
        _validate_regular(after, component=component, purpose=f"held {purpose} {name!r}")
        if _stable_file_fields(before) != _stable_file_fields(after) or len(raw) != after.st_size:
            _refuse(component, f"{purpose} {name!r} changed while its bytes were read")
        try:
            named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            _refuse(component, f"{purpose} {name!r} vanished while its inode was held")
        _validate_regular(named, component=component, purpose=f"named {purpose} {name!r}")
        if (after.st_dev, after.st_ino) != (named.st_dev, named.st_ino):
            _refuse(component, f"{purpose} {name!r} was replaced while its inode was held")
        return raw
    finally:
        os.close(fd)


@dataclass(frozen=True)
class HeldDirectory:
    path: Path
    fd: int
    component: str
    purpose: str

    def read_name(self, name: str, *, purpose: str) -> bytes:
        return _read_named(
            self.fd,
            name,
            component=self.component,
            purpose=purpose,
        )

    def read_relative(self, relative_path: str, *, purpose: str) -> bytes:
        posix = PurePosixPath(relative_path)
        if posix.is_absolute() or not posix.parts or ".." in posix.parts or "." in posix.parts:
            _refuse(
                self.component,
                f"{purpose} path {relative_path!r} is not a lexical child of {self.path}",
            )
        directories: list[tuple[int, str, int]] = []
        current = self.fd
        try:
            for name in posix.parts[:-1]:
                child = _open_child_directory(
                    current,
                    name,
                    component=self.component,
                    purpose=purpose,
                )
                assert child is not None
                directories.append((current, name, child))
                current = child
            raw = _read_named(
                current,
                posix.parts[-1],
                component=self.component,
                purpose=purpose,
            )
            for parent, name, child in reversed(directories):
                _verify_child_directory(
                    parent,
                    name,
                    child,
                    component=self.component,
                    purpose=purpose,
                )
            self.verify()
            return raw
        finally:
            for _parent, _name, child in reversed(directories):
                os.close(child)

    def json_file_set(self, *, manifest_name: str) -> set[str]:
        """List the JSON names owned by the Massive capture under custody."""
        found: set[str] = set()

        def add_json_names(directory_fd: int, prefix: str) -> None:
            for name in os.listdir(directory_fd):
                if not name.endswith(".json"):
                    continue
                try:
                    entry = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                except FileNotFoundError:
                    _refuse(self.component, f"capture JSON {prefix}{name} vanished during scan")
                _validate_regular(
                    entry,
                    component=self.component,
                    purpose=f"capture JSON {prefix}{name}",
                )
                found.add(f"{prefix}{name}")

        add_json_names(self.fd, "")
        found.discard(manifest_name)
        for directory_name in ("masters", "bars"):
            child = _open_child_directory(
                self.fd,
                directory_name,
                component=self.component,
                purpose="Massive capture",
                allow_missing=True,
            )
            if child is None:
                continue
            try:
                add_json_names(child, f"{directory_name}/")
                _verify_child_directory(
                    self.fd,
                    directory_name,
                    child,
                    component=self.component,
                    purpose="Massive capture",
                )
            finally:
                os.close(child)
        self.verify()
        return found

    def verify(self) -> None:
        _verify_directory_path(
            self.path,
            self.fd,
            component=self.component,
            purpose=self.purpose,
        )


@contextmanager
def hold_directory(
    path: Path, *, component: str, purpose: str
) -> Iterator[HeldDirectory]:
    absolute = _absolute_lexical(path)
    fd = _open_directory(absolute, component=component, purpose=purpose)
    held = HeldDirectory(path=absolute, fd=fd, component=component, purpose=purpose)
    try:
        yield held
        held.verify()
    finally:
        os.close(fd)


def read_file_once(path: Path, *, component: str, purpose: str) -> bytes:
    absolute = _absolute_lexical(path)
    with hold_directory(absolute.parent, component=component, purpose=f"{purpose} parent") as held:
        return held.read_name(absolute.name, purpose=purpose)


__all__ = ["HeldDirectory", "hold_directory", "read_file_once"]
