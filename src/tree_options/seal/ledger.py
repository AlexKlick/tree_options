"""Hash-chained authority ledger for the G4 sealed event.

Mirrors the runstate journal's mechanics (PR A/A1) under its OWN domain, so
a seal record can never be confused with — or spliced into — a runstate
journal:

* one JSON line per record; ``fsync`` on the file AND the parent directory
  before an append returns, so a crash can lose at most the line being
  written;
* ``record_sha256 = sha256(domain ‖ canonical record without its hash)`` and
  ``prev_record_sha256`` chains to the predecessor (genesis chains to 64
  zeros). Replay verifies the chain, so a tampered or reordered record
  anywhere raises ``LedgerCorruptError``;
* a torn FINAL line is tolerated and reported (``tail_damaged``) — it was
  never acknowledged — but a further APPEND refuses: appending past a torn
  tail would turn the unacknowledged line into mid-file damage that poisons
  the chain at the next read. Reconcile first.
* the root is INJECTABLE everywhere and any root whose RESOLVED path is
  under ``/tmp`` is refused (``LedgerRootRefusedError``): /tmp is wiped on
  reboot, and seal authority may never live where a reboot destroys it;
* the ledger FILE name is opened ``O_NOFOLLOW`` on both the read and the
  append path (round-5 review fix, 2026-08-24): a symlink at
  ``ledger.jsonl`` — dangling or not — is ``LedgerCorruptError``, so
  authority can never be created through, or read through, a link;
* the ledger ROOT is taken into custody as a REAL directory
  (``O_RDONLY|O_DIRECTORY|O_NOFOLLOW``, round-6 review fix, 2026-08-24):
  a root swapped to a symlink between validation and the open is ``ELOOP``
  → ``LedgerCorruptError``, and every later open/fsync rides that one dir
  fd, so the root pathname is never re-resolved — a ``mkdir(exist_ok=True)``
  accepting a directory symlink can no longer land authority under the
  link's target. Round-7 review fix (2026-08-24): custody is taken
  COMPONENT-WISE from ``/`` (each path component opened no-follow relative
  to the previous component's fd), so a symlink planted at ANY intermediate
  ancestor — not just the final component — refuses naming that component.

Record kinds:

* ``APPROVAL`` — the owner's pre-declared approval of an identity tuple;
* ``CONSUMPTION`` — the one-shot spend of that authority by a sealed run;
* ``RECONCILIATION_NOTE`` — an operator note after an incident (e.g. a crash
  between consumption and run completion: UNKNOWN, never auto-rerun).

Consumers RECOMPUTE the identity from a record's own payload instead of
trusting its stored ids — see ``scripts/g4_seal.py`` execute step 2.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, NoReturn

from tree_options.data.digest import sha256_hex
from tree_options.schemas.common import StrictModel
from tree_options.seal.errors import LedgerCorruptError, LedgerRootRefusedError
from tree_options.seal.identity import SealedIdentity, content_identity, sealed_run_id

LEDGER_DOMAIN = b"tree-options-g4-ledger-v1"
GENESIS_PREV = "0" * 64
LEDGER_FILENAME = "ledger.jsonl"

# A RELATIVE constant on purpose: it resolves against the invoking checkout
# and stays out of git (artifacts/ is ignored). Tests ALWAYS inject their own
# root; one test pins the constant itself.
DEFAULT_G4_LEDGER_ROOT = Path("artifacts/g4-authority")

TMP_AUTHORITY_ROOT = Path("/tmp")

RecordKind = Literal["APPROVAL", "CONSUMPTION", "RECONCILIATION_NOTE"]
KIND_APPROVAL: RecordKind = "APPROVAL"
KIND_CONSUMPTION: RecordKind = "CONSUMPTION"
KIND_RECONCILIATION_NOTE: RecordKind = "RECONCILIATION_NOTE"


class LedgerRecord(StrictModel):
    """One ledger line. ``kind`` discriminates the authority semantics."""

    kind: RecordKind
    identity: SealedIdentity  # the full identity tuple, verbatim
    sealed_run_id: str  # stored id; consumers recompute rather than trust
    content_identity: str
    reason: str
    at_epoch: int
    prev_record_sha256: str
    record_sha256: str = ""  # filled by the writer; "" is invalid on disk


@dataclass(frozen=True)
class LedgerView:
    records: tuple[LedgerRecord, ...]
    tail_hash: str  # GENESIS_PREV when empty
    tail_damaged: bool


def validate_ledger_root(root: Path) -> Path:
    """Resolve and vet a ledger root. Under /tmp is REFUSED, mechanically.

    The check is on the RESOLVED path, so a symlink under the repo pointing
    into /tmp is caught too, and a sibling like ``/tmp-authority`` (which
    shares the string prefix but not the component) stays allowed.
    """
    resolved = Path(root).resolve()
    if resolved == TMP_AUTHORITY_ROOT or TMP_AUTHORITY_ROOT in resolved.parents:
        raise LedgerRootRefusedError(str(root), "resolved path is under /tmp")
    return resolved


def _record_hash(record: LedgerRecord) -> str:
    body = json.dumps(
        {k: v for k, v in record.model_dump().items() if k != "record_sha256"},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256_hex(LEDGER_DOMAIN + body)


def _encode(record: LedgerRecord) -> str:
    return json.dumps(
        json.loads(record.model_dump_json()),  # nested models -> plain dicts
        sort_keys=True,
        separators=(",", ":"),
    )


def _decode_record(line: str) -> LedgerRecord | None:
    try:
        return LedgerRecord.model_validate(json.loads(line))
    except Exception:
        return None


def _verify_chain(record: LedgerRecord, prev_hash: str) -> bool:
    if record.prev_record_sha256 != prev_hash:
        return False
    return record.record_sha256 == _record_hash(record)


def _replay_text(text: str) -> LedgerView:
    """Verify + decode. Torn FINAL line: tolerated, flagged, excluded."""
    lines = text.splitlines()
    records: list[LedgerRecord] = []
    prev_hash = GENESIS_PREV
    damaged_tail = False
    for index, line in enumerate(lines):
        record = _decode_record(line)
        is_final = index == len(lines) - 1
        if record is None or not _verify_chain(record, prev_hash):
            if is_final:
                damaged_tail = True
                continue  # a torn tail was never acknowledged; exclude it
            raise LedgerCorruptError(
                f"ledger line {index + 1} failed decode/hash/chain verification"
            )
        records.append(record)
        prev_hash = record.record_sha256
    return LedgerView(records=tuple(records), tail_hash=prev_hash, tail_damaged=damaged_tail)


def read_ledger(root: Path) -> LedgerView:
    """Replay + verify the ledger at ``root``.

    An ABSENT ledger is not corruption (nothing approved, nothing consumed —
    execute classifies it as "no approval" and refuses); a refused root or a
    broken chain is.

    Round-6 review fix (2026-08-24, finding 3): the root is taken into
    custody as a REAL directory (``O_NOFOLLOW``; a root swapped to a symlink
    between validation and the open is ``ELOOP`` → ``LedgerCorruptError``)
    and the ledger is opened BY NAME inside that custody fd — the read can
    never be redirected through a symlinked root.
    """
    root = validate_ledger_root(root)
    root_fd = _open_ledger_root(root, create=False)
    if root_fd is None:
        return LedgerView(records=(), tail_hash=GENESIS_PREV, tail_damaged=False)
    try:
        try:
            fd = _open_ledger_nofollow(root / LEDGER_FILENAME, root_fd, os.O_RDONLY)
        except FileNotFoundError:
            return LedgerView(records=(), tail_hash=GENESIS_PREV, tail_damaged=False)
        try:
            chunks: list[bytes] = []
            offset = 0
            while True:
                chunk = os.pread(fd, 65536, offset)
                if not chunk:
                    break
                chunks.append(chunk)
                offset += len(chunk)
        finally:
            os.close(fd)
    finally:
        os.close(root_fd)
    # Tolerant decode: a torn final append may have cut mid-UTF-8 byte; the
    # replacement chars fail JSON decode and classify as a torn tail, while a
    # mid-file undecodable line stays LEDGER_CORRUPT.
    return _replay_text(b"".join(chunks).decode("utf-8", errors="replace"))


def _open_ledger_root(root: Path, *, create: bool) -> int | None:
    """Round-6 review fix (2026-08-24, finding 3): take custody of the ledger
    ROOT as a REAL directory, once, with ``O_NOFOLLOW``.

    The final-name ``O_NOFOLLOW`` (round-5) guards only ``ledger.jsonl``.
    Between ``validate_ledger_root()`` and the mkdir/open, an attacker could
    create the (previously nonexistent) allowed root as a directory SYMLINK:
    ``mkdir(exist_ok=True)`` accepts a directory symlink, the ledger name
    inside it is a regular file, and the final-name ``O_NOFOLLOW`` never
    fired — authority landed under the link's target. The root is now opened
    ``O_RDONLY|O_DIRECTORY|O_NOFOLLOW`` (a symlink at the root is ``ELOOP``,
    refused by name) and every later operation — the ledger open, the
    directory fsync — rides that one dir fd, so the root PATHNAME is never
    re-resolved (and therefore never re-followed) after custody is taken.

    Round-7 review fix (2026-08-24, finding 2): a single open of the root
    path guards only the FINAL component. Renaming an INTERMEDIATE ancestor
    (e.g. the repo's ``artifacts/``) and planting it as a symlink to an
    attack dir — with a real root dir inside — left the final component a
    REAL directory, the open FOLLOWED the intermediate link, and custody
    landed on the target. Custody is now taken COMPONENT-WISE: ``/`` is
    opened once, then every component of the resolved root path is opened
    ``O_RDONLY|O_DIRECTORY|O_NOFOLLOW`` relative to the previous component's
    fd (which is then closed). ``ELOOP`` or ``ENOTDIR`` at ANY component is a
    ``LedgerCorruptError`` naming the offending component. The ``create``
    branch creates a missing component ONE at a time with
    ``os.mkdir(name, dir_fd=prev)`` (``EEXIST`` proceeds to the no-follow
    open, so a lost race that left a symlink refuses there) — and only after
    the walked prefix is already under custody.

    ``create`` is append-only: a read of an absent root stays an empty view.
    A lost mkdir race is never suppressed on the attacker's terms — every
    created component is re-opened under the same ``O_NOFOLLOW`` rule."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW

    def _refuse_component(component: str, exc: OSError) -> NoReturn:
        # ELOOP: O_NOFOLLOW met a symlink. ENOTDIR: Linux reports a
        # symlink-to-directory this way under O_DIRECTORY|O_NOFOLLOW (and any
        # non-directory equally). Either way the walked path left REAL
        # directories — refuse naming the offending component.
        raise LedgerCorruptError(
            f"{root}: ledger-root component {component!r} is not a real "
            f"directory (opened O_NOFOLLOW|O_DIRECTORY component-wise from /, "
            f"errno {exc.errno}) — a seal authority ledger is never created or "
            "followed through a symlinked path component"
        ) from None

    resolved = Path(os.path.abspath(str(root)))  # callers pass the validated,
    # RESOLVED root; abspath only normalizes here — re-resolving would FOLLOW
    # a swapped ancestor and change the components under custody.
    fd = os.open(os.sep, os.O_RDONLY | os.O_DIRECTORY)
    for component in resolved.parts[1:]:
        prev = fd
        try:
            fd = os.open(component, flags, dir_fd=prev)
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                os.close(prev)
                _refuse_component(component, exc)
            if exc.errno == errno.ENOENT and create:
                # Absent component (append only): create it as a SINGLE
                # component under custody — a lost race re-opens it under the
                # no-follow rule, so a symlink the concurrent creator left
                # refuses with ELOOP.
                try:
                    os.mkdir(component, 0o755, dir_fd=prev)
                except FileExistsError:
                    pass
                try:
                    fd = os.open(component, flags, dir_fd=prev)
                except OSError as retry:
                    os.close(prev)
                    if retry.errno in (errno.ELOOP, errno.ENOTDIR):
                        _refuse_component(component, retry)
                    raise
            else:
                os.close(prev)
                if exc.errno == errno.ENOENT:
                    return None  # absent root on the read path: an empty view
                raise
        os.close(prev)
    return fd


def _open_ledger_nofollow(path: Path, root_fd: int, flags: int) -> int:
    """Round-5 review fix (2026-08-24, finding 3): open the ledger NAME
    without ever following a symlink at it.

    ``Path.exists()`` is False for a DANGLING symlink, so the read path used
    to treat a symlinked ledger as absent, and the append's
    ``os.open(path, O_RDWR|O_CREAT)`` FOLLOWED the link — creating seal
    authority under /tmp (or wherever the link points). ``O_NOFOLLOW`` makes
    the open fail with ``ELOOP`` for a symlink at the final component
    regardless of where (or whether) its target exists; that is corruption
    of the authority surface, refused by name.

    Round-6 review fix (2026-08-24, finding 3): the open rides the custody
    root fd (``dir_fd=root_fd``, opening the bare NAME), so even a symlink
    swap of the root PATHNAME after custody was taken cannot redirect it."""
    try:
        return os.open(path.name, flags | os.O_NOFOLLOW, 0o644, dir_fd=root_fd)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise LedgerCorruptError(
                f"{path}: the ledger name is a SYMLINK — a seal authority "
                "ledger is never created or followed through a symlink"
            ) from None
        raise


def append_record(root: Path, record: LedgerRecord) -> str:
    """Append one hash-chained record; returns its ``record_sha256``.

    The exclusive ``flock`` spans read-verify-append, so the prev hash is the
    VERIFIED tail even under a concurrent appender, and the record's hash is
    computed + fsynced (file, then parent dir) before the call returns.
    Refuses to append past a torn tail or on a supplied prev that does not
    match the verified tail.
    """
    root = validate_ledger_root(root)
    root_fd = _open_ledger_root(root, create=True)
    assert root_fd is not None  # create=True always returns an open fd or raises
    try:
        path = root / LEDGER_FILENAME
        fd = _open_ledger_nofollow(path, root_fd, os.O_RDWR | os.O_CREAT)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                chunks: list[bytes] = []
                offset = 0
                while True:
                    chunk = os.pread(fd, 65536, offset)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    offset += len(chunk)
                view = _replay_text(b"".join(chunks).decode("utf-8", errors="replace"))
                if view.tail_damaged:
                    raise LedgerCorruptError(
                        "the ledger's final line is torn; reconcile it (append a "
                        "RECONCILIATION_NOTE from a durable root) before any further "
                        "append — appending past it would hide an unacknowledged write"
                    )
                if record.prev_record_sha256 != view.tail_hash:
                    raise LedgerCorruptError(
                        "supplied prev_record_sha256 does not match the verified "
                        "ledger tail — rebuild the record from read_ledger()"
                    )
                signed = record.model_copy(update={"record_sha256": _record_hash(record)})
                os.lseek(fd, 0, os.SEEK_END)
                os.write(fd, (_encode(signed) + "\n").encode("utf-8"))
                os.fsync(fd)
                # Round-8 review fix (2026-08-24, finding 3): verify the NAME
                # still maps to the locked inode BEFORE returning success.
                # During the append an attacker can rename ledger.jsonl to a
                # sibling .held (the locked fd follows the inode) and install
                # a byte-copy clone at the name: the consumption lands under
                # .held, the call used to return SUCCESS, and a second
                # execution on the clone consumed again — the one-shot was
                # broken. The one-shot lock domain is the locked INODE, so
                # the name is checked under the same flock and the same
                # custody root fd; a divergence is RECONCILIATION, never
                # success.
                locked = os.fstat(fd)
                try:
                    named = os.stat(LEDGER_FILENAME, dir_fd=root_fd, follow_symlinks=False)
                except OSError as exc:
                    raise LedgerCorruptError(
                        f"{path}: the ledger NAME vanished after the append "
                        f"({exc.strerror}) — the one-shot lock domain is the "
                        "locked inode, so authority may have been consumed "
                        "under a renamed file: this refusal is RECONCILIATION, "
                        "never success"
                    ) from None
                if not stat.S_ISREG(named.st_mode) or (named.st_dev, named.st_ino) != (
                    locked.st_dev,
                    locked.st_ino,
                ):
                    raise LedgerCorruptError(
                        f"{path}: the ledger NAME no longer maps to the locked "
                        f"inode (locked fd dev {locked.st_dev} ino {locked.st_ino}, "
                        f"name holds dev {named.st_dev} ino {named.st_ino}, mode "
                        f"{stat.S_IFMT(named.st_mode):o}) — authority may have "
                        "been consumed under a renamed file while a clone holds "
                        "the name: this refusal is RECONCILIATION, never success"
                    )
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
        # directory durability on the CUSTODY fd: the root pathname is never
        # re-resolved (round-6 finding 3), so a swapped root cannot redirect
        # this fsync either.
        os.fsync(root_fd)
    finally:
        os.close(root_fd)
    return signed.record_sha256


def _append_kind(
    root: Path,
    kind: RecordKind,
    identity: SealedIdentity,
    *,
    reason: str,
    at_epoch: int,
) -> LedgerRecord:
    view = read_ledger(root)
    record = LedgerRecord(
        kind=kind,
        identity=identity,
        sealed_run_id=sealed_run_id(identity),
        content_identity=content_identity(identity),
        reason=reason,
        at_epoch=at_epoch,
        prev_record_sha256=view.tail_hash,
    )
    digest = append_record(root, record)
    return record.model_copy(update={"record_sha256": digest})


def append_approval(
    root: Path, identity: SealedIdentity, *, reason: str, at_epoch: int
) -> LedgerRecord:
    """Record the owner's approval of an identity tuple (library API)."""
    return _append_kind(root, KIND_APPROVAL, identity, reason=reason, at_epoch=at_epoch)


def append_consumption(
    root: Path, identity: SealedIdentity, *, reason: str, at_epoch: int
) -> LedgerRecord:
    """Spend the one-shot authority for this sealed content (library API)."""
    return _append_kind(root, KIND_CONSUMPTION, identity, reason=reason, at_epoch=at_epoch)


def append_reconciliation_note(
    root: Path, identity: SealedIdentity, *, reason: str, at_epoch: int
) -> LedgerRecord:
    """Leave an operator note after an incident (library API)."""
    return _append_kind(root, KIND_RECONCILIATION_NOTE, identity, reason=reason, at_epoch=at_epoch)
