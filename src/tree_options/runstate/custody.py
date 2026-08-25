"""Shared no-follow filesystem custody for the complete run-state store.

Paths are walked lexically from ``/``.  Every directory component and final
authority name is opened relative to a held directory FD with ``O_NOFOLLOW``;
regular-file type, link count, inode identity, bytes, and final path reachability
are checked at the operation's success boundary.

Round-11 consolidation (2026-08-25): this module also owns the DURABLE
name→inode binding — the successor-window closer for every append-only
authority file (journal.jsonl, the seal ledger, the bars ledger).  A name
check that runs *inside* one append can never see a swap that lands after
it, so each authority name carries a companion identity record (written
once, at creation, through :func:`atomic_write`) pinning the one inode the
name may map to; every later open verifies the name against that record and
refuses — corruption/reconciliation class, never success — on divergence.
"""

from __future__ import annotations

import errno
import json
import os
import secrets
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

from tree_options.data.digest import sha256_hex
from tree_options.runstate.errors import StoreCustodyError

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def lexical_absolute(path: Path) -> Path:
    """Normalize ``.``/``..`` without resolving or following any symlink."""
    return Path(os.path.abspath(os.fspath(path)))


def _refuse(run_id: str, detail: str) -> NoReturn:
    raise StoreCustodyError(run_id, detail)


def _component_name(name: str, *, run_id: str, purpose: str) -> None:
    if not name or name in {".", ".."} or Path(name).name != name or Path(name).is_absolute():
        _refuse(run_id, f"{purpose} name {name!r} is not exactly one path component")


def open_directory(
    path: Path,
    *,
    create: bool,
    run_id: str,
    purpose: str = "run-state directory",
) -> int | None:
    """Open a lexical path component-wise from ``/`` under no-follow custody."""
    absolute = lexical_absolute(path)
    fd = os.open(os.sep, os.O_RDONLY | os.O_DIRECTORY)
    for component in absolute.parts[1:]:
        previous = fd
        try:
            fd = os.open(component, _DIR_FLAGS, dir_fd=previous)
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                os.close(previous)
                _refuse(
                    run_id,
                    f"{purpose} component {component!r} in {absolute} is not a real "
                    f"directory (O_DIRECTORY|O_NOFOLLOW, errno {exc.errno})",
                )
            if exc.errno == errno.ENOENT and create:
                try:
                    os.mkdir(component, 0o755, dir_fd=previous)
                except FileExistsError:
                    pass
                except Exception:
                    os.close(previous)
                    raise
                else:
                    # Round-12 review fix (2026-08-25, finding 3, R14): a
                    # first-use namespace creation is committed in its PARENT
                    # before the walk proceeds — the pre-fix fsyncs covered
                    # only deeper directories, so a reboot could drop the
                    # newly created entry (and, for the seal anchor tree, the
                    # anchor beside a durable ledger) and silently forget
                    # acknowledged authority.
                    os.fsync(previous)
                try:
                    fd = os.open(component, _DIR_FLAGS, dir_fd=previous)
                except OSError as retry:
                    os.close(previous)
                    if retry.errno in (errno.ELOOP, errno.ENOTDIR):
                        _refuse(
                            run_id,
                            f"{purpose} component {component!r} in {absolute} lost a "
                            "creation race to a non-directory or symlink",
                        )
                    raise
            else:
                os.close(previous)
                if exc.errno == errno.ENOENT:
                    return None
                raise
        os.close(previous)
    return fd


def open_child_directory(
    parent_fd: int,
    name: str,
    *,
    create: bool,
    run_id: str,
    purpose: str,
) -> int | None:
    """Open one child directory without resolving the held parent's path."""
    _component_name(name, run_id=run_id, purpose=purpose)
    try:
        return os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            _refuse(
                run_id,
                f"{purpose} {name!r} is not a real directory "
                f"(O_DIRECTORY|O_NOFOLLOW, errno {exc.errno})",
            )
        if exc.errno == errno.ENOENT and create:
            try:
                os.mkdir(name, 0o755, dir_fd=parent_fd)
            except FileExistsError:
                pass
            try:
                return os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
            except OSError as retry:
                if retry.errno in (errno.ELOOP, errno.ENOTDIR):
                    _refuse(
                        run_id,
                        f"{purpose} {name!r} lost a creation race to a non-directory or symlink",
                    )
                raise
        if exc.errno == errno.ENOENT:
            return None
        raise


def verify_directory_identity(path: Path, held_fd: int, *, run_id: str) -> None:
    """Require the lexical path still to name the held directory inode."""
    try:
        fresh_fd = open_directory(path, create=False, run_id=run_id)
    except StoreCustodyError as exc:
        _refuse(run_id, f"directory identity for {path} is no longer reachable ({exc.detail})")
    if fresh_fd is None:
        _refuse(run_id, f"directory identity for {path} vanished while under custody")
    try:
        held = os.fstat(held_fd)
        fresh = os.fstat(fresh_fd)
        if (held.st_dev, held.st_ino) != (fresh.st_dev, fresh.st_ino):
            _refuse(
                run_id,
                f"directory identity for {path} changed from dev/inode "
                f"{held.st_dev}/{held.st_ino} to {fresh.st_dev}/{fresh.st_ino}",
            )
    finally:
        os.close(fresh_fd)


def _validate_regular(st: os.stat_result, *, run_id: str, purpose: str) -> None:
    if not stat.S_ISREG(st.st_mode):
        _refuse(run_id, f"{purpose} is not a regular file (mode {stat.S_IFMT(st.st_mode):o})")
    if st.st_nlink != 1:
        _refuse(run_id, f"{purpose} has unexpected link count {st.st_nlink}, expected 1")


def _named_stat(parent_fd: int, name: str, *, run_id: str, purpose: str) -> os.stat_result:
    try:
        named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        _refuse(run_id, f"{purpose} name {name!r} vanished while its inode was held")
    _validate_regular(named, run_id=run_id, purpose=f"{purpose} name {name!r}")
    return named


def verify_name_identity(
    parent_fd: int,
    name: str,
    held_fd: int,
    *,
    run_id: str,
    purpose: str,
) -> None:
    """Require a safe final name to still map to its held regular-file inode."""
    held = os.fstat(held_fd)
    _validate_regular(held, run_id=run_id, purpose=f"held {purpose} {name!r}")
    named = _named_stat(parent_fd, name, run_id=run_id, purpose=purpose)
    if (held.st_dev, held.st_ino) != (named.st_dev, named.st_ino):
        _refuse(
            run_id,
            f"{purpose} {name!r} inode changed: held dev/inode "
            f"{held.st_dev}/{held.st_ino}, named {named.st_dev}/{named.st_ino}",
        )


def open_regular(
    parent_fd: int,
    name: str,
    flags: int,
    *,
    run_id: str,
    purpose: str,
    mode: int = 0o644,
    allow_missing: bool = False,
) -> int | None:
    """Open one authority file by name and bind the name to its safe inode."""
    _component_name(name, run_id=run_id, purpose=purpose)
    try:
        fd = os.open(name, flags | os.O_NOFOLLOW, mode, dir_fd=parent_fd)
    except FileNotFoundError:
        if allow_missing:
            return None
        raise
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            _refuse(run_id, f"{purpose} {name!r} is a symlink and is never followed")
        raise
    try:
        verify_name_identity(
            parent_fd,
            name,
            fd,
            run_id=run_id,
            purpose=purpose,
        )
    except Exception:
        os.close(fd)
        raise
    return fd


def read_all(fd: int) -> bytes:
    chunks: list[bytes] = []
    offset = 0
    while True:
        chunk = os.pread(fd, 65536, offset)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        offset += len(chunk)


def read_named_bytes(
    directory_path: Path,
    directory_fd: int,
    name: str,
    *,
    run_id: str,
    purpose: str,
    allow_missing: bool = False,
) -> bytes | None:
    fd = open_regular(
        directory_fd,
        name,
        os.O_RDONLY,
        run_id=run_id,
        purpose=purpose,
        allow_missing=allow_missing,
    )
    if fd is None:
        verify_directory_identity(directory_path, directory_fd, run_id=run_id)
        return None
    try:
        payload = read_all(fd)
        verify_name_identity(
            directory_fd,
            name,
            fd,
            run_id=run_id,
            purpose=purpose,
        )
        verify_directory_identity(directory_path, directory_fd, run_id=run_id)
        return payload
    finally:
        os.close(fd)


def name_exists(
    directory_path: Path,
    directory_fd: int,
    name: str,
    *,
    run_id: str,
    purpose: str,
) -> bool:
    fd = open_regular(
        directory_fd,
        name,
        os.O_RDONLY,
        run_id=run_id,
        purpose=purpose,
        allow_missing=True,
    )
    if fd is None:
        verify_directory_identity(directory_path, directory_fd, run_id=run_id)
        return False
    try:
        verify_name_identity(
            directory_fd,
            name,
            fd,
            run_id=run_id,
            purpose=purpose,
        )
        verify_directory_identity(directory_path, directory_fd, run_id=run_id)
        return True
    finally:
        os.close(fd)


def _safe_existing_name(
    parent_fd: int,
    name: str,
    *,
    run_id: str,
    purpose: str,
) -> os.stat_result | None:
    try:
        existing = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    _validate_regular(existing, run_id=run_id, purpose=f"{purpose} {name!r}")
    return existing


def _new_temp_name(name: str) -> str:
    return f".{name}.{os.getpid()}.{secrets.token_hex(16)}.tmp"


def write_all(fd: int, payload: bytes) -> None:
    """The ONLY write path for authority records (round-11, finding 5).

    A single ``os.write`` whose return count is ignored accepts a short
    append: a torn prefix lands, fsync and the name check still pass, and
    the caller returns success over damage. This loop returns only after
    every byte is written; a writer that accepts nothing raises. There is
    no third outcome.
    """
    view = memoryview(payload)
    written = 0
    while written < len(view):
        count = os.write(fd, view[written:])
        if count <= 0:
            raise OSError("short write while publishing run-state authority")
        written += count


# ---- identity-conditional replacement (round-11 consolidation, 2026-08-25) ---------
#
# A classify-then-replace sequence may never replace "whatever currently
# holds the name": the classification's subject must still BE the name's
# inode at the write moment, or the write refuses.


@dataclass(frozen=True)
class ReplaceExpectation:
    """The single inode (and exact bytes digest) a replacement may swap out."""

    identity: tuple[int, int]
    sha256: str


def capture_replacement_expectation(
    directory_fd: int,
    name: str,
    classified_bytes: bytes,
    *,
    run_id: str,
    purpose: str,
) -> ReplaceExpectation:
    """Bind the bytes that were classified to the inode now holding the name.

    The caller classified ``classified_bytes`` (read earlier); this re-opens
    the name under custody and REFUSES unless it still holds exactly those
    bytes, returning the held inode's identity + the classified bytes'
    digest for :func:`atomic_write`'s identity-conditional replace. A
    replacement that landed between the read and here is a refusal — it is
    never silently classified, and never overwritten.
    """
    fd = open_regular(
        directory_fd,
        name,
        os.O_RDONLY,
        run_id=run_id,
        purpose=purpose,
    )
    if fd is None:
        _refuse(
            run_id,
            f"{purpose} {name!r} vanished after its bytes were classified — "
            "the identity-conditional replace has nothing authorized to "
            "replace; reconciliation, never a silent recreate",
        )
    try:
        verify_name_identity(directory_fd, name, fd, run_id=run_id, purpose=purpose)
        if read_all(fd) != classified_bytes:
            _refuse(
                run_id,
                f"{purpose} {name!r} now holds different bytes than the ones "
                "classified — a replacement landed in the classification "
                "window and is never overwritten; reconciliation, never success",
            )
        held = os.fstat(fd)
        return ReplaceExpectation(
            identity=(held.st_dev, held.st_ino),
            sha256=sha256_hex(classified_bytes),
        )
    finally:
        os.close(fd)


def _verify_replacement_expectation(
    directory_fd: int,
    name: str,
    existing: os.stat_result | None,
    expected: ReplaceExpectation,
    *,
    run_id: str,
    purpose: str,
) -> None:
    """Refuse unless the name still maps to exactly the expected inode+bytes."""
    if existing is None:
        _refuse(
            run_id,
            f"{purpose} {name!r} vanished before the identity-conditional "
            "replace — the classified inode is gone; reconciliation, never a "
            "silent recreate",
        )
    if (existing.st_dev, existing.st_ino) != expected.identity:
        _refuse(
            run_id,
            f"{purpose} {name!r} was replaced before the identity-conditional "
            f"publish: expected dev/inode {expected.identity[0]}/{expected.identity[1]}, "
            f"the name now holds dev/inode {existing.st_dev}/{existing.st_ino} — "
            "a replacement owner is never overwritten; reconciliation, never success",
        )
    current_fd = open_regular(
        directory_fd,
        name,
        os.O_RDONLY,
        run_id=run_id,
        purpose=purpose,
    )
    assert current_fd is not None
    try:
        verify_name_identity(directory_fd, name, current_fd, run_id=run_id, purpose=purpose)
        if sha256_hex(read_all(current_fd)) != expected.sha256:
            _refuse(
                run_id,
                f"{purpose} {name!r} bytes changed since classification — the "
                "identity-conditional replace refuses; reconciliation, never success",
            )
    finally:
        os.close(current_fd)


def _unlink_temp_if_ours(
    parent_fd: int,
    temp_name: str,
    temp_identity: tuple[int, int] | None,
) -> None:
    if temp_identity is None:
        return
    try:
        named = os.stat(temp_name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if (named.st_dev, named.st_ino) == temp_identity:
        os.unlink(temp_name, dir_fd=parent_fd)


def atomic_write(
    directory_path: Path,
    directory_fd: int,
    name: str,
    payload: bytes,
    *,
    run_id: str,
    purpose: str,
    mode: int,
    exclusive: bool,
    expected: ReplaceExpectation | None = None,
) -> None:
    """Publish verified bytes from an exclusive temp inode in the held dir.

    ``exclusive=True`` publishes by an atomic no-replace hard-link operation,
    then drops the temp name.  Replacement writes refuse unsafe pre-existing
    final names before replacing them.

    ``expected`` (round-11 review fix, finding 6) makes the replacement
    IDENTITY-CONDITIONAL: the write may swap out only the exact inode (and
    bytes digest) that the caller classified — a name that vanished, was
    replaced, or was rewritten in the classification window is a refusal
    (reconciliation, never success), never an overwrite of whatever
    currently holds the name.
    """
    if expected is not None and exclusive:
        raise ValueError(
            f"{purpose} {name!r}: an exclusive publish never replaces, so an "
            "expected replacement identity is contradictory"
        )
    _component_name(name, run_id=run_id, purpose=purpose)
    existing = _safe_existing_name(
        directory_fd,
        name,
        run_id=run_id,
        purpose=purpose,
    )
    if exclusive and existing is not None:
        _refuse(run_id, f"{purpose} {name!r} already exists; exclusive publish refused")

    temp_name = _new_temp_name(name)
    _component_name(temp_name, run_id=run_id, purpose=f"{purpose} temporary")
    temp_fd: int | None = None
    temp_identity: tuple[int, int] | None = None
    published = False
    try:
        try:
            temp_fd = os.open(
                temp_name,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                mode,
                dir_fd=directory_fd,
            )
        except FileExistsError:
            _refuse(
                run_id,
                f"{purpose} temporary name {temp_name!r} already exists; "
                "exclusive temp custody refused",
            )
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                _refuse(run_id, f"{purpose} temporary name {temp_name!r} is a symlink")
            raise
        temp_stat = os.fstat(temp_fd)
        _validate_regular(temp_stat, run_id=run_id, purpose=f"{purpose} temporary inode")
        temp_identity = (temp_stat.st_dev, temp_stat.st_ino)
        write_all(temp_fd, payload)
        os.fsync(temp_fd)
        verify_name_identity(
            directory_fd,
            temp_name,
            temp_fd,
            run_id=run_id,
            purpose=f"{purpose} temporary",
        )
        if read_all(temp_fd) != payload:
            _refuse(run_id, f"{purpose} temporary bytes changed before publish")

        if exclusive:
            try:
                os.link(
                    temp_name,
                    name,
                    src_dir_fd=directory_fd,
                    dst_dir_fd=directory_fd,
                    follow_symlinks=False,
                )
            except FileExistsError:
                _refuse(run_id, f"{purpose} {name!r} appeared before exclusive publish")
            os.unlink(temp_name, dir_fd=directory_fd)
        else:
            existing = _safe_existing_name(
                directory_fd,
                name,
                run_id=run_id,
                purpose=purpose,
            )
            if expected is not None:
                # Round-11 (finding 6): the replace is identity-conditional —
                # checked at the write moment, against a held fd.
                _verify_replacement_expectation(
                    directory_fd,
                    name,
                    existing,
                    expected,
                    run_id=run_id,
                    purpose=purpose,
                )
            os.replace(
                temp_name,
                name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
        published = True
        os.fsync(directory_fd)

        published_fd = open_regular(
            directory_fd,
            name,
            os.O_RDONLY,
            run_id=run_id,
            purpose=purpose,
        )
        assert published_fd is not None
        try:
            published_stat = os.fstat(published_fd)
            if (published_stat.st_dev, published_stat.st_ino) != temp_identity:
                _refuse(
                    run_id,
                    f"{purpose} {name!r} published inode differs from the verified "
                    "exclusive temporary inode",
                )
            if read_all(published_fd) != payload:
                _refuse(run_id, f"{purpose} {name!r} published bytes changed after publish")
            verify_name_identity(
                directory_fd,
                name,
                published_fd,
                run_id=run_id,
                purpose=purpose,
            )
        finally:
            os.close(published_fd)
        verify_directory_identity(directory_path, directory_fd, run_id=run_id)
    finally:
        if temp_fd is not None:
            os.close(temp_fd)
        if not published:
            _unlink_temp_if_ours(directory_fd, temp_name, temp_identity)


# ---- durable name→inode bindings (round-11 consolidation, 2026-08-25) -------------
#
# An in-append name check can never observe a swap that lands after it, so a
# clone installed at an authority name during the append's success window
# used to be a fully valid file that the NEXT process consumed. The binding
# makes that impossible: the one inode a name may map to is pinned in a
# companion record at creation, and every open refuses a name that no longer
# maps to it.

NAME_BINDING_FORMAT = 1


@dataclass(frozen=True)
class NameBinding:
    """The one inode an authority name may map to (dev, ino)."""

    name: str
    st_dev: int
    st_ino: int


def name_binding_filename(name: str) -> str:
    """The companion identity-record name for an authority file name."""
    return f"{name}.identity.json"


def _binding_bytes(binding: NameBinding) -> bytes:
    payload = {
        "format": NAME_BINDING_FORMAT,
        "name": binding.name,
        "st_dev": binding.st_dev,
        "st_ino": binding.st_ino,
    }
    return (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def load_name_binding(
    directory_path: Path,
    directory_fd: int,
    name: str,
    *,
    run_id: str = "?",
    purpose: str,
    refuse: Callable[[str], NoReturn] | None = None,
) -> NameBinding | None:
    """Load the durable name→inode binding for ``name`` (None when absent).

    A present-but-malformed binding record is corruption, never a silent
    None. ``refuse`` lets a non-runstate caller (the seal and bars ledgers)
    keep its own error family; every refusal detail is passed through it.
    """

    def say(detail: str) -> NoReturn:
        if refuse is not None:
            refuse(f"{purpose} name binding for {name!r}: {detail}")
        _refuse(run_id, f"{purpose} name binding for {name!r}: {detail}")

    try:
        raw = read_named_bytes(
            directory_path,
            directory_fd,
            name_binding_filename(name),
            run_id=run_id,
            purpose=f"{purpose} name binding",
            allow_missing=True,
        )
    except StoreCustodyError as exc:
        say(exc.detail)
    if raw is None:
        return None
    try:
        parsed = json.loads(raw)
        binding = NameBinding(
            name=str(parsed["name"]),
            st_dev=int(parsed["st_dev"]),
            st_ino=int(parsed["st_ino"]),
        )
        if int(parsed["format"]) != NAME_BINDING_FORMAT:
            raise ValueError(f"unknown format {parsed['format']!r}")
        if binding.name != name:
            raise ValueError(f"binds a different name {binding.name!r}")
    except (KeyError, TypeError, ValueError) as exc:
        say(f"the record is malformed ({exc}); a binding is never guessed around")
    return binding


def bind_name_identity(
    directory_path: Path,
    directory_fd: int,
    name: str,
    held_fd: int,
    *,
    run_id: str = "?",
    purpose: str,
    refuse: Callable[[str], NoReturn] | None = None,
) -> NameBinding:
    """Durably record the (st_dev, st_ino) of the inode ``held_fd`` names.

    Called exactly once per authority file — at creation, BEFORE the first
    append lands, under the caller's flock — so a crash between the file's
    creation and this record still leaves an EMPTY (rebindable) file, never
    bound authority without a record. Exclusive by construction: a second
    binder refuses instead of silently re-pointing the name.
    """
    held = os.fstat(held_fd)
    _validate_regular(held, run_id=run_id, purpose=f"held {purpose} {name!r}")
    binding = NameBinding(name=name, st_dev=held.st_dev, st_ino=held.st_ino)
    try:
        atomic_write(
            directory_path,
            directory_fd,
            name_binding_filename(name),
            _binding_bytes(binding),
            run_id=run_id,
            purpose=f"{purpose} name binding",
            mode=0o644,
            exclusive=True,
        )
    except StoreCustodyError as exc:
        if refuse is not None:
            refuse(f"{purpose} name binding for {name!r}: {exc.detail}")
        raise
    return binding


def verify_name_binding(
    directory_fd: int,
    name: str,
    held_fd: int,
    binding: NameBinding,
    *,
    run_id: str = "?",
    purpose: str,
    refuse: Callable[[str], NoReturn] | None = None,
) -> None:
    """Refuse unless ``name`` still maps to the BOUND identity == ``held_fd``'s.

    This is the successor-window closer: a byte-copy clone or any replacement
    installed at the canonical name has the wrong inode and is refused at the
    next open — corruption/reconciliation class, never silent success.
    """

    def say(detail: str) -> NoReturn:
        if refuse is not None:
            refuse(f"{purpose} {name!r} no longer maps to its durable binding: {detail}")
        _refuse(run_id, f"{purpose} {name!r} no longer maps to its durable binding: {detail}")

    held = os.fstat(held_fd)
    try:
        _validate_regular(held, run_id=run_id, purpose=f"held {purpose} {name!r}")
        named = _named_stat(directory_fd, name, run_id=run_id, purpose=purpose)
    except StoreCustodyError as exc:
        say(exc.detail)
    if (held.st_dev, held.st_ino) != (binding.st_dev, binding.st_ino):
        say(
            f"the held inode is dev/inode {held.st_dev}/{held.st_ino}, "
            f"the binding records dev/inode {binding.st_dev}/{binding.st_ino}"
        )
    if (named.st_dev, named.st_ino) != (binding.st_dev, binding.st_ino):
        say(
            f"the name holds dev/inode {named.st_dev}/{named.st_ino}, "
            f"the binding records dev/inode {binding.st_dev}/{binding.st_ino} "
            "— a clone or replacement at the authority name is refused at "
            "open; this is reconciliation, never success"
        )


def unlink_held_name(
    directory_path: Path,
    directory_fd: int,
    name: str,
    held_fd: int,
    *,
    run_id: str,
    purpose: str,
) -> None:
    """Unlink the held inode only — identity-safe BY CONSTRUCTION.

    Round-11 review fix (2026-08-25, finding 7): the old sequence verified
    the name then ``os.unlink``-ed the NAME — a swap landing between those
    two syscalls unlinked a successor published at the name. The held name
    is now RENAMED (dir_fd-relative) to an unpredictable temp first, the
    RENAMED entry is verified to map to the held fd's identity, and only
    then is the TEMP unlinked. A successor published at the old name is
    never touched by the unlink; a swap before the rename is caught by the
    verify-before-rename, and one landing between the verify and the rename
    moves the successor's bytes aside (never deletes them) and refuses;
    a swap after the rename lands at the now-free name, harmless.

    Round-10 review fix (2026-08-25, finding 3): the mismatch refusal used
    to raise only AFTER the rename had emptied the canonical name — an
    ordinary ``acquire`` then saw no owner and exclusively created a fresh
    launcher while the successor was still live: two live launchers
    manufactured by the refusal path itself. When the renamed entry does
    NOT map to the held identity it is renamed BACK to the canonical name
    (restore) BEFORE the refusal raises, so the successor stays reachable
    at the authority name and no fresh exclusive create is possible. Only
    a verified match ever unlinks the temp.
    """
    verify_name_identity(
        directory_fd,
        name,
        held_fd,
        run_id=run_id,
        purpose=purpose,
    )
    held = os.fstat(held_fd)
    temp_name = _new_temp_name(name)
    _component_name(temp_name, run_id=run_id, purpose=f"{purpose} release temporary")
    try:
        os.rename(name, temp_name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
    except FileNotFoundError as exc:
        _refuse(
            run_id,
            f"{purpose} name {name!r} vanished between verification and the "
            f"release rename ({exc.strerror}); reconciliation, never success",
        )
    except OSError as exc:
        _refuse(
            run_id,
            f"{purpose} name {name!r} could not be renamed aside for release "
            f"({exc.strerror}); reconciliation, never success",
        )
    renamed = _named_stat(
        directory_fd,
        temp_name,
        run_id=run_id,
        purpose=f"{purpose} renamed",
    )
    if (renamed.st_dev, renamed.st_ino) != (held.st_dev, held.st_ino):
        _refuse_swapped_and_restore(
            directory_fd,
            name,
            temp_name,
            renamed=renamed,
            held=held,
            run_id=run_id,
            purpose=purpose,
        )
    os.unlink(temp_name, dir_fd=directory_fd)
    os.fsync(directory_fd)
    verify_directory_identity(directory_path, directory_fd, run_id=run_id)


def _refuse_swapped_and_restore(
    directory_fd: int,
    name: str,
    temp_name: str,
    *,
    renamed: os.stat_result,
    held: os.stat_result,
    run_id: str,
    purpose: str,
) -> NoReturn:
    """Round-10 P1 (finding 3): refuse a swapped release entry WITHOUT ever
    leaving the canonical name empty.

    ``renamed`` is the entry the release rename moved to ``temp_name`` — a
    successor published at the canonical name in the verify→rename window.
    The rename above emptied the canonical name; raising here alone handed
    the next ordinary ``acquire`` an exclusive create while the successor
    was still live. The moved-aside entry is renamed BACK first, and only
    then does the refusal raise — the successor's entry is never deleted by
    this path in any outcome.
    """
    prefix = (
        f"{purpose} {name!r} was swapped between verification and the "
        "release rename: the renamed entry maps to dev/inode "
        f"{renamed.st_dev}/{renamed.st_ino}, not the held "
        f"{held.st_dev}/{held.st_ino}"
    )
    try:
        os.rename(temp_name, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
    except OSError as exc:
        _refuse(
            run_id,
            f"{prefix} — and the moved-aside entry could not be restored to "
            f"the canonical name ({exc.strerror}); it remains at "
            f"{temp_name!r}, never deleted; reconciliation, never success",
        )
    restored = _named_stat(
        directory_fd,
        name,
        run_id=run_id,
        purpose=f"{purpose} restored",
    )
    if (restored.st_dev, restored.st_ino) != (renamed.st_dev, renamed.st_ino):
        _refuse(
            run_id,
            f"{prefix} — and the canonical name could not be confirmed to "
            "hold the restored successor (named dev/inode "
            f"{restored.st_dev}/{restored.st_ino}); the successor's entry "
            "was never deleted; reconciliation, never success",
        )
    _refuse(
        run_id,
        f"{prefix} — the successor's entry was moved aside by the rename and "
        "has been RESTORED at the canonical name, never deleted; "
        "reconciliation, never success",
    )
