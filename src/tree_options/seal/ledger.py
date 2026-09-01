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
* the ledger name carries a DURABLE name→inode binding (round-11 review
  fix, 2026-08-25, finding 3): at creation the file's ``(st_dev, st_ino)``
  is pinned in a companion identity record (``ledger.jsonl.identity.json``,
  custody-written beside the ledger), and EVERY open verifies the name
  still maps to that inode. An in-append name check can never see a swap
  that lands after it; the binding makes the clone such a swap installs
  unusable by every successor process — refused at open as reconciliation,
  never success.
* the ledger identity is ALSO anchored in a SECOND tree (round-11 review
  fix, 2026-08-25, finding 1, R13): the companion record lives BESIDE the
  file it guards, so an OFFLINE attacker could co-replace both files with
  a self-consistent pair (an approval-only clone plus a companion naming
  the clone). A dedicated anchor record in the RUNSTATE STORE tree —
  ``<ledger-root-parent>/runstate/seal-ledger-anchor/<key>.identity.json``,
  custody-written once at ledger creation — records the ledger root path,
  the ledger file's ``(st_dev, st_ino)``, and the companion digest; every
  open verifies BOTH trees, and divergence or a MISSING anchor for a
  non-empty ledger is a corruption-class refusal (see
  ``runstate_anchor_path`` for the store-identity mapping).
* the anchor also pins the ledger's COMMITTED EXTENT (round-12 review fix,
  2026-08-25, finding 1, R14): the identity fields above bind WHICH inode
  the ledger name maps to, but not HOW MUCH of it was committed, and
  ``_replay_text`` accepts any valid hash-chain prefix — so a same-inode
  truncation that rewrote only the original approval line verified against
  every identity check and silently un-spent an acknowledged consumption.
  The anchor record therefore carries ``ledger_size`` (the byte count at
  the last committed append) and ``committed_tail_sha256`` (the view's
  tail hash), advanced at every successful append: at open, a ledger
  SMALLER than the anchored extent — or one holding exactly the anchored
  bytes with a different committed tail — is a corruption-class refusal.
  A ledger LARGER than the anchored extent is the benign
  next-append-after-crash window (the ledger fsync landed, the anchor
  update did not) ONLY when the anchored extent is PROVEN as its prefix
  (R15, finding 1, via ``custody.check_committed_extent``): the pinned
  size must fall on a record boundary and replaying ONLY the pinned bytes
  must chain to the pinned committed tail. Anything else at a larger size
  — e.g. a re-chained approval padded past the extent by a later record —
  refused; it is then re-anchored at the next append.

Record kinds:

* ``APPROVAL`` — the owner's pre-declared approval of an identity tuple;
* ``CONSUMPTION`` — the one-shot spend of that authority by a sealed run;
* ``RECONCILIATION`` — the owner's act re-arming authority for sealed CONTENT
  whose consumption produced no verdict (the 2026-08-31 crash class): it must
  name the identity of a CONSUMPTION record already in the ledger, and each
  record permits exactly ONE further consumption of that content — the
  budget arithmetic lives in ``_check_authority`` (scripts/g4_seal.py), which
  counts consumptions against reconciliations per content identity;
* ``RECONCILIATION_NOTE`` — an operator note after an incident (e.g. a crash
  between consumption and run completion: UNKNOWN, never auto-rerun). A NOTE
  carries NO authority semantics: appending one never re-arms anything.

Consumers RECOMPUTE the identity from a record's own payload instead of
trusting its stored ids — see ``scripts/g4_seal.py`` execute step 2.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import stat
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal, NoReturn

from tree_options.data.digest import sha256_hex
from tree_options.runstate import custody
from tree_options.runstate.errors import StoreCustodyError
from tree_options.schemas.common import StrictModel
from tree_options.seal.errors import (
    LedgerCorruptError,
    LedgerRootRefusedError,
    ReconciliationInvalidError,
)
from tree_options.seal.identity import SealedIdentity, content_identity, sealed_run_id

LEDGER_DOMAIN = b"tree-options-g4-ledger-v1"
GENESIS_PREV = "0" * 64
LEDGER_FILENAME = "ledger.jsonl"

# A RELATIVE constant on purpose: it resolves against the invoking checkout
# and stays out of git (artifacts/ is ignored). Tests ALWAYS inject their own
# root; one test pins the constant itself.
DEFAULT_G4_LEDGER_ROOT = Path("artifacts/g4-authority")

TMP_AUTHORITY_ROOT = Path("/tmp")

RecordKind = Literal["APPROVAL", "CONSUMPTION", "RECONCILIATION", "RECONCILIATION_NOTE"]
KIND_APPROVAL: RecordKind = "APPROVAL"
KIND_CONSUMPTION: RecordKind = "CONSUMPTION"
KIND_RECONCILIATION: RecordKind = "RECONCILIATION"
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

    Round-11 review fix (2026-08-25, finding 1, R13): the read verifies BOTH
    identity trees — the beside-the-file companion AND the runstate anchor
    (see ``_verify_bound_ledger_name`` and the dual-tree anchor section) —
    so an offline co-replacement of the ledger and its companion is refused
    against the second tree's record of the REAL ledger.

    Round-12 review fix (2026-08-25, finding 1, R14): the replayed view is
    additionally checked against the anchor's COMMITTED EXTENT — a ledger
    smaller than the anchored extent (or one holding exactly the anchored
    bytes with a different committed tail) is a prefix-rollback refusal,
    never an approval-only view over an un-spent consumption.
    """
    root = validate_ledger_root(root)
    root_fd = _open_ledger_root(root, create=False)
    if root_fd is None:
        # Round-11 (finding 1, R13): an absent ROOT with a surviving runstate
        # anchor is the total disappearance of created authority — the anchor
        # is the second tree's memory that a ledger WAS created here.
        if _load_runstate_anchor(root) is not None:
            raise LedgerCorruptError(
                f"{root}: the ledger root is absent while a runstate anchor records "
                "created authority — bound authority may not silently vanish; this "
                "refusal is RECONCILIATION, never an empty view"
            ) from None
        return LedgerView(records=(), tail_hash=GENESIS_PREV, tail_damaged=False)
    try:
        try:
            fd = _open_ledger_nofollow(root / LEDGER_FILENAME, root_fd, os.O_RDONLY)
        except FileNotFoundError:
            # Round-11 (finding 3): a bound name may not silently vanish —
            # the absent-name case stays an empty view only when NOTHING was
            # ever bound.
            if (
                custody.load_name_binding(
                    root,
                    root_fd,
                    LEDGER_FILENAME,
                    purpose="ledger.jsonl authority",
                    refuse=_binding_refusal,
                )
                is not None
            ):
                raise LedgerCorruptError(
                    f"{root / LEDGER_FILENAME}: the ledger NAME is absent while "
                    "its durable name binding exists — bound authority may not "
                    "silently vanish; this refusal is RECONCILIATION, never an "
                    "empty view"
                ) from None
            # Round-11 (finding 1, R13): ditto for the SECOND tree — deleting
            # both the ledger and its companion is never a silent reset when
            # the runstate anchor still names created authority.
            if _load_runstate_anchor(root) is not None:
                raise LedgerCorruptError(
                    f"{root / LEDGER_FILENAME}: the ledger NAME is absent while a "
                    "runstate anchor records created authority — bound authority "
                    "may not silently vanish; this refusal is RECONCILIATION, "
                    "never an empty view"
                ) from None
            return LedgerView(records=(), tail_hash=GENESIS_PREV, tail_damaged=False)
        try:
            binding = _verify_bound_ledger_name(root, root_fd, fd)
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
    raw = b"".join(chunks)
    view = _replay_text(raw.decode("utf-8", errors="replace"))
    # Round-12 (finding 1, R14) + R15 (findings 1 and 4): a valid prefix is
    # not committed authority — the anchored extent must still be there in
    # full, a ledger LARGER than it must PROVE the anchored prefix, and BOTH
    # durable extent records (the runstate anchor AND the companion) must
    # agree with the ledger bytes.
    _refuse_anchor_extent_rollback(
        root,
        binding=binding,
        ledger_bytes=offset,
        view=view,
        raw_ledger_bytes=raw,
    )
    return view


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
    created component is re-opened under the same ``O_NOFOLLOW`` rule.

    R15 (finding 2, 2026-08-25): the walk is a DURABLE TRAVERSAL — the
    PARENT fd is fsynced for EVERY successfully traversed component, on
    BOTH branches (created — already committed since round-12 — and
    existing-open). This is the restart-closure repair: a component a
    PRIOR invocation left between its mkdir and its parent fsync is
    committed by the next walk that merely opens it, so a reboot can no
    longer drop that ancestor entry together with the ledger root and the
    anchor tree beneath it. The ledger-root walk is authority-bearing BY
    CONSTRUCTION, so durability is unconditional here (the opt-in
    ``durable=`` flag exists on ``custody.open_directory`` for walks with
    non-authority callers; this local walk has none)."""
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
        parent_committed = False
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
                else:
                    # Round-12 review fix (2026-08-25, finding 3, R14): a
                    # first-use ledger-root component is committed in its
                    # PARENT before the walk proceeds — the pre-fix fsyncs
                    # covered only deeper directories (the ledger root at
                    # append time), so a reboot could drop the g4-authority
                    # entry together with the anchor-tree entries and leave
                    # the next read an EMPTY view over an acknowledged
                    # consumption.
                    os.fsync(prev)
                    parent_committed = True
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
        if not parent_committed:
            # R15 (finding 2): the component already existed — commit its
            # entry in the parent before relying on anything beneath it (a
            # prior invocation may have crashed between its mkdir and this
            # fsync).
            os.fsync(prev)
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


def _binding_refusal(detail: str) -> NoReturn:
    """The durable name→inode binding refuses in the ledger's error family."""
    raise LedgerCorruptError(detail) from None


# ---- the dual-tree runstate anchor (round-11 finding 1, R13, 2026-08-25) ---------
#
# The companion identity record pins the ledger name to ONE inode — but it
# lives BESIDE the file it guards, so an OFFLINE attacker (between
# invocations, no concurrency needed) could replace BOTH files with a
# self-consistent pair: a regular clone carrying the approval-only prefix
# bytes plus a replacement companion naming the clone's dev/inode. The next
# open verified the clone against its FORGED companion and re-spent the
# approval — the companion alone added no security over the ledger itself.
# The ledger identity is therefore ALSO anchored in a SECOND tree the
# artifacts/ attacker must separately forge: the runstate store. At the
# first ledger open/creation (an empty ledger, under the flock, BEFORE the
# first append lands) a dedicated anchor record is custody-written there
# recording the ledger root path, the ledger file's (st_dev, st_ino), and
# the sha256 of the companion bytes; every subsequent open verifies BOTH
# the beside-the-file companion AND this anchor. Divergence — or a MISSING
# anchor for a non-empty ledger — is a corruption-class refusal
# (RECONCILIATION, never success), so a co-replaced pair still leaves the
# anchor naming the REAL ledger and the clone can never be opened as
# authority.
#
# Round-12 (finding 1, R14, 2026-08-25): the anchor additionally pins the
# ledger's COMMITTED EXTENT — ``ledger_size`` bytes and the committed tail
# hash at the last acknowledged append — advanced by an identity-conditional
# replacement at every successful append (see ``_commit_anchor_extent``),
# so a same-inode prefix rollback that un-spends an acknowledged consumption
# is refused at the next open even though every identity field verifies.
#
# Store-identity mapping (the seal ledger has no natural run_id/run store):
# the anchor tree is the runstate store root ADJACENT to the ledger root —
# ``<resolved-ledger-root>.parent / "runstate"`` — so the default ledger
# root ``artifacts/g4-authority`` anchors in ``artifacts/runstate`` (the
# store's own root; the anchor lives in a dedicated ``seal-ledger-anchor/``
# namespace beside the per-run directories). The record name keys the
# anchor to the RESOLVED ledger root path (sha256 prefix), so one store
# tree holds every ledger's anchor without collision, and a MOVED ledger
# root is a different key — an owner reconcile act, never a silent re-bind.

ANCHOR_FORMAT = 2
ANCHOR_PURPOSE = "g4-seal-ledger-anchor"
RUNSTATE_STORE_DIRNAME = "runstate"
ANCHOR_DIRNAME = "seal-ledger-anchor"
_ANCHOR_RUN_ID = "g4-seal-ledger"  # cosmetic: the custody error-detail context


@dataclass(frozen=True)
class RunstateAnchorRecord:
    """The ledger identity pinned in the SECOND tree (the runstate store).

    Round-12 (finding 1, R14) added the COMMITTED EXTENT: ``ledger_size``
    is the ledger's byte count at the last committed append and
    ``committed_tail_sha256`` is that view's tail hash (``GENESIS_PREV``
    for the empty ledger anchored at creation), so a same-inode prefix
    rollback or an in-place truncation of committed authority is refused
    at the next open even though every identity field still verifies."""

    anchor_key: str
    ledger_root: str
    ledger_name: str
    st_dev: int
    st_ino: int
    companion_sha256: str
    ledger_size: int
    committed_tail_sha256: str


def anchor_store_root(ledger_root: Path) -> Path:
    """The runstate store root anchoring this ledger (the second tree)."""
    return Path(ledger_root).resolve().parent / RUNSTATE_STORE_DIRNAME


def _anchor_key(resolved_root: Path) -> str:
    return sha256_hex(str(resolved_root).encode("utf-8"))[:16]


def runstate_anchor_path(ledger_root: Path) -> Path:
    """The anchor record's path in the runstate store tree (public so callers
    and tests can name — and clean — exactly the record their ledger root
    owns; see the section comment above for the mapping)."""
    resolved = Path(ledger_root).resolve()
    return anchor_store_root(resolved) / ANCHOR_DIRNAME / f"{_anchor_key(resolved)}.identity.json"


def _anchor_bytes(record: RunstateAnchorRecord) -> bytes:
    payload = {
        "format": ANCHOR_FORMAT,
        "purpose": ANCHOR_PURPOSE,
        "anchor_key": record.anchor_key,
        "ledger_root": record.ledger_root,
        "ledger_name": record.ledger_name,
        "st_dev": record.st_dev,
        "st_ino": record.st_ino,
        "companion_sha256": record.companion_sha256,
        "ledger_size": record.ledger_size,
        "committed_tail_sha256": record.committed_tail_sha256,
    }
    return (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _anchor_unreachable(resolved_root: Path, action: str, detail: str) -> NoReturn:
    raise LedgerCorruptError(
        f"{resolved_root}: the runstate anchor tree could not be {action} through real "
        f"directories ({detail}) — the seal ledger's second-tree anchor is corruption, "
        "never a silent absence; reconcile with the owner"
    ) from None


def _parse_anchor_bytes(path: Path, resolved_root: Path, raw: bytes) -> RunstateAnchorRecord:
    """Decode + vet one anchor record's bytes (shared by load and update)."""
    try:
        parsed = json.loads(raw)
        record = RunstateAnchorRecord(
            anchor_key=str(parsed["anchor_key"]),
            ledger_root=str(parsed["ledger_root"]),
            ledger_name=str(parsed["ledger_name"]),
            st_dev=int(parsed["st_dev"]),
            st_ino=int(parsed["st_ino"]),
            companion_sha256=str(parsed["companion_sha256"]),
            ledger_size=int(parsed["ledger_size"]),
            committed_tail_sha256=str(parsed["committed_tail_sha256"]),
        )
        if int(parsed["format"]) != ANCHOR_FORMAT:
            raise ValueError(f"unknown format {parsed['format']!r}")
        if str(parsed["purpose"]) != ANCHOR_PURPOSE:
            raise ValueError(f"foreign purpose {parsed['purpose']!r}")
        if record.anchor_key != _anchor_key(resolved_root):
            raise ValueError(f"keyed to a different ledger root ({record.anchor_key!r})")
        if record.ledger_size < 0:
            raise ValueError(f"negative committed extent {record.ledger_size}")
    except (KeyError, TypeError, ValueError) as exc:
        raise LedgerCorruptError(
            f"{path}: the runstate anchor record is malformed ({exc}) — an anchor is "
            "never guessed around; reconcile with the owner (this refusal is "
            "RECONCILIATION, never success)"
        ) from None
    return record


def _load_runstate_anchor(resolved_root: Path) -> RunstateAnchorRecord | None:
    """Load this ledger root's anchor record (``None`` when absent).

    Unreachable anchor storage (a symlinked component — the custody walk
    refuses it) or a present-but-malformed record is ``LedgerCorruptError``,
    never a silent ``None``: an anchor is never guessed around."""
    path = runstate_anchor_path(resolved_root)
    try:
        anchor_fd = custody.open_directory(
            path.parent,
            create=False,
            durable=True,  # R15 (finding 2): authority walk — repair residues
            run_id=_ANCHOR_RUN_ID,
            purpose="g4 seal runstate anchor",
        )
    except StoreCustodyError as exc:
        _anchor_unreachable(resolved_root, "opened", exc.detail)
    if anchor_fd is None:
        return None
    try:
        try:
            raw = custody.read_named_bytes(
                path.parent,
                anchor_fd,
                path.name,
                run_id=_ANCHOR_RUN_ID,
                purpose="g4 seal runstate anchor",
                allow_missing=True,
            )
        except StoreCustodyError as exc:
            _anchor_unreachable(resolved_root, "read", exc.detail)
    finally:
        os.close(anchor_fd)
    if raw is None:
        return None
    return _parse_anchor_bytes(path, resolved_root, raw)


def _write_runstate_anchor(resolved_root: Path, root_fd: int, ledger_fd: int) -> None:
    """Custody-write the anchor record exactly ONCE, at ledger creation (under
    the caller's flock, before the first append lands): the publish is
    EXCLUSIVE, so a second binder refuses instead of silently re-pointing the
    second tree at a new inode.

    The creation anchor pins the EMPTY extent (``ledger_size`` 0, the
    genesis tail): the caller holds an empty ledger, and every successful
    append afterwards advances the extent through
    ``_commit_anchor_extent``."""
    held = os.fstat(ledger_fd)
    if held.st_size != 0:
        raise LedgerCorruptError(
            f"{resolved_root / LEDGER_FILENAME}: the creation runstate anchor may only "
            f"be written over an EMPTY ledger (holds {held.st_size} bytes) — an anchor "
            "is never minted over existing authority; reconcile with the owner"
        ) from None
    companion_name = custody.name_binding_filename(LEDGER_FILENAME)
    try:
        companion = custody.read_named_bytes(
            resolved_root,
            root_fd,
            companion_name,
            run_id=_ANCHOR_RUN_ID,
            purpose="ledger.jsonl authority name binding",
        )
    except StoreCustodyError as exc:
        raise LedgerCorruptError(
            f"{resolved_root / companion_name}: the companion identity record could not "
            f"be read while anchoring ({exc.detail})"
        ) from None
    assert companion is not None  # bind_name_identity ran immediately before
    record = RunstateAnchorRecord(
        anchor_key=_anchor_key(resolved_root),
        ledger_root=str(resolved_root),
        ledger_name=LEDGER_FILENAME,
        st_dev=held.st_dev,
        st_ino=held.st_ino,
        companion_sha256=sha256_hex(companion),
        ledger_size=0,
        committed_tail_sha256=GENESIS_PREV,
    )
    path = runstate_anchor_path(resolved_root)
    try:
        anchor_fd = custody.open_directory(
            path.parent,
            create=True,
            durable=True,  # R15 (finding 2): authority walk — repair residues
            run_id=_ANCHOR_RUN_ID,
            purpose="g4 seal runstate anchor",
        )
    except StoreCustodyError as exc:
        _anchor_unreachable(resolved_root, "created", exc.detail)
    assert anchor_fd is not None  # create=True always returns an open fd or raises
    try:
        custody.atomic_write(
            path.parent,
            anchor_fd,
            path.name,
            _anchor_bytes(record),
            run_id=_ANCHOR_RUN_ID,
            purpose="g4 seal runstate anchor",
            mode=0o644,
            exclusive=True,
        )
    except StoreCustodyError as exc:
        raise LedgerCorruptError(
            f"{path}: the runstate anchor could not be published ({exc.detail}) — a "
            "ledger is never created without its second-tree anchor; this refusal is "
            "RECONCILIATION, never success"
        ) from None
    finally:
        os.close(anchor_fd)


def _check_runstate_anchor(
    resolved_root: Path, root_fd: int, ledger_fd: int, record: RunstateAnchorRecord
) -> None:
    """Both trees must agree on ONE ledger identity: the anchor's recorded
    root/name must be exactly this root, its ``(st_dev, st_ino)`` must be the
    held inode's, and the companion bytes must still hash to the anchored
    digest. Any divergence is the co-replacement refusal."""
    held = os.fstat(ledger_fd)
    if record.ledger_root != str(resolved_root) or record.ledger_name != LEDGER_FILENAME:
        raise LedgerCorruptError(
            f"{resolved_root / LEDGER_FILENAME}: the runstate anchor names a different "
            f"ledger ({record.ledger_root}/{record.ledger_name}) — an anchor never "
            "transfers between roots; this refusal is RECONCILIATION, never success"
        ) from None
    if (record.st_dev, record.st_ino) != (held.st_dev, held.st_ino):
        raise LedgerCorruptError(
            f"{resolved_root / LEDGER_FILENAME}: the runstate anchor records dev/inode "
            f"{record.st_dev}/{record.st_ino} but the ledger name now maps to "
            f"{held.st_dev}/{held.st_ino} — the beside-the-file companion may have been "
            "co-replaced offline, but the second-tree anchor still names the real "
            "ledger, so a clone is never opened as authority: this refusal is "
            "RECONCILIATION, never success"
        ) from None
    companion_name = custody.name_binding_filename(LEDGER_FILENAME)
    try:
        companion = custody.read_named_bytes(
            resolved_root,
            root_fd,
            companion_name,
            run_id=_ANCHOR_RUN_ID,
            purpose="ledger.jsonl authority name binding",
        )
    except StoreCustodyError as exc:
        raise LedgerCorruptError(
            f"{resolved_root / companion_name}: the companion identity record could not "
            f"be read against the runstate anchor ({exc.detail})"
        ) from None
    if companion is None:
        raise LedgerCorruptError(
            f"{resolved_root / companion_name}: the companion identity record no longer "
            "holds the bytes the runstate anchor pins — the beside-the-file tree "
            "diverges from the second tree; this refusal is RECONCILIATION, never success"
        ) from None
    if sha256_hex(companion) != record.companion_sha256:
        # R15 (finding 4): the companion's committed-extent advance changes
        # its bytes AFTER the anchor pinned them (the crash window between
        # the companion replacement and the anchor re-commit at the same
        # append). Tolerate ONLY the advance shape: the replacement must
        # still parse as the binding format and bind the SAME ledger inode
        # the anchor names — its extent fields are verified separately by
        # the class extent check, and the anchor's own pinned extent still
        # bounds the authority. Anything else diverges from the second tree.
        try:
            advanced = custody.parse_name_binding_bytes(companion, LEDGER_FILENAME)
        except (KeyError, TypeError, ValueError) as exc:
            raise LedgerCorruptError(
                f"{resolved_root / companion_name}: the companion identity record no "
                "longer holds the bytes the runstate anchor pins and no longer parses "
                f"as a binding ({exc}) — the beside-the-file tree diverges from the "
                "second tree; this refusal is RECONCILIATION, never success"
            ) from None
        if (advanced.st_dev, advanced.st_ino) != (record.st_dev, record.st_ino):
            raise LedgerCorruptError(
                f"{resolved_root / companion_name}: the companion identity record no "
                "longer holds the bytes the runstate anchor pins — it names dev/inode "
                f"{advanced.st_dev}/{advanced.st_ino} while the runstate anchor pins "
                f"{record.st_dev}/{record.st_ino}: the beside-the-file tree diverges "
                "from the second tree; this refusal is RECONCILIATION, never success"
            ) from None


def _check_anchor_extent(
    resolved_root: Path,
    record: RunstateAnchorRecord,
    *,
    ledger_bytes: int,
    view: LedgerView,
    raw_ledger_bytes: bytes,
) -> None:
    """Round-12 review fix (2026-08-25, finding 1, R14) + R15 (2026-08-25,
    finding 1): the anchored COMMITTED EXTENT — the same-inode prefix-rollback
    closer, all three branches.

    The identity fields bind which inode the ledger name maps to, but not
    how much of it was committed, and ``_replay_text`` accepts any valid
    hash-chain prefix: truncating the ledger in place and rewriting only the
    original approval line kept the inode, the companion, and the anchor
    verifications green while silently un-spending an acknowledged
    consumption (round-12). R15 closed the LARGER-branch hole: a ledger
    bigger than the anchored extent used to be accepted as the benign
    next-append-after-crash window with NO proof that its first
    ``ledger_size`` bytes were the anchored history — a re-chained approval
    padded past the anchored size by a later record removed a consumption
    while every identity check stayed green. All three branches now run
    through the ONE class mechanism (``custody.check_committed_extent``):
    SMALLER refuses (rollback), EQUAL refuses a different committed tail
    (in-place rewrite), LARGER accepts ONLY a PROVEN prefix (the pinned
    extent falls on a record boundary and replays, alone, to the pinned
    committed tail)."""
    custody.check_committed_extent(
        extent_size=record.ledger_size,
        committed_tail_sha256=record.committed_tail_sha256,
        ledger_bytes=ledger_bytes,
        view_tail_sha256=view.tail_hash,
        raw_ledger_bytes=raw_ledger_bytes,
        replay_prefix=_replay_text,
        subject=str(resolved_root / LEDGER_FILENAME),
        origin="the runstate anchor",
        refuse=_binding_refusal,
    )


def _refuse_anchor_extent_rollback(
    resolved_root: Path,
    *,
    binding: custody.NameBinding | None,
    ledger_bytes: int,
    view: LedgerView,
    raw_ledger_bytes: bytes,
) -> None:
    """The open-side extent rule (R15, finding 4): the replayed view is
    verified against BOTH durable extent records — the runstate anchor AND
    the companion identity record. The two records must AGREE with the
    ledger bytes: each one's pinned extent must hold as a full/proven prefix
    of the actual ledger (the class three-branch check), and the anchor —
    the record in the tree the artifacts/ attacker cannot co-forge — remains
    the bounding authority. An ABSENT anchor is already policed by the
    identity checks (a non-empty unanchored ledger refuses there), and a
    None binding means an empty never-bound ledger, so neither is
    re-refused here. The one benign divergence is the interrupted-append
    crash window (one record's advance landed, the other's did not): both
    extents still prove as prefixes of the same ledger, the open accepts,
    and the next append re-advances both."""
    record = _load_runstate_anchor(resolved_root)
    if record is None:
        return
    _check_anchor_extent(
        resolved_root,
        record,
        ledger_bytes=ledger_bytes,
        view=view,
        raw_ledger_bytes=raw_ledger_bytes,
    )
    if binding is None:
        return
    custody.check_committed_extent(
        extent_size=binding.extent_size,
        committed_tail_sha256=binding.committed_tail_sha256,
        ledger_bytes=ledger_bytes,
        view_tail_sha256=view.tail_hash,
        raw_ledger_bytes=raw_ledger_bytes,
        replay_prefix=_replay_text,
        subject=str(resolved_root / LEDGER_FILENAME),
        origin="the companion identity record",
        refuse=_binding_refusal,
    )


def _commit_anchor_extent(
    resolved_root: Path,
    *,
    ledger_bytes: int,
    committed_tail_sha256: str,
    companion_sha256: str,
) -> None:
    """Advance the anchor's committed extent after a successful append (under
    the caller's flock, after the ledger fsync, the name check, and the
    companion extent advance — ``companion_sha256`` re-pins the ADVANCED
    companion, R15 finding 4).

    The replacement is IDENTITY-CONDITIONAL through custody (``expected``
    binds the exact classified anchor bytes), so an anchor swapped between
    the verified read and this write is never overwritten. A refusal here
    leaves the append DURABLE on the ledger — the next open sees the ledger
    larger than the anchored extent with a valid chain, accepts it, and
    re-anchors at the next append — so a failed extent commit can never
    un-spend an acknowledged record."""
    path = runstate_anchor_path(resolved_root)
    try:
        anchor_fd = custody.open_directory(
            path.parent,
            create=False,
            durable=True,  # R15 (finding 2): authority walk — repair residues
            run_id=_ANCHOR_RUN_ID,
            purpose="g4 seal runstate anchor",
        )
    except StoreCustodyError as exc:
        _anchor_unreachable(resolved_root, "opened for the extent commit", exc.detail)
    assert anchor_fd is not None  # the anchor was verified moments ago
    try:
        try:
            current = custody.read_named_bytes(
                path.parent,
                anchor_fd,
                path.name,
                run_id=_ANCHOR_RUN_ID,
                purpose="g4 seal runstate anchor",
                allow_missing=True,
            )
        except StoreCustodyError as exc:
            _anchor_unreachable(resolved_root, "read for the extent commit", exc.detail)
        if current is None:
            raise LedgerCorruptError(
                f"{path}: the runstate anchor vanished under the append — the second "
                "tree's memory of created authority may not silently disappear; this "
                "refusal is RECONCILIATION, never success"
            ) from None
        record = _parse_anchor_bytes(path, resolved_root, current)
        if (
            record.ledger_size == ledger_bytes
            and record.committed_tail_sha256 == committed_tail_sha256
            and record.companion_sha256 == companion_sha256
        ):
            return  # already anchored at exactly this extent (idempotent)
        if ledger_bytes < record.ledger_size:
            # Never move the anchored extent BACKWARDS: the append wrote past
            # a committed extent this anchor no longer describes.
            raise LedgerCorruptError(
                f"{path}: the append committed {ledger_bytes} bytes but the anchor "
                f"already pins {record.ledger_size} — the extent may only advance; "
                "reconcile with the owner (this refusal is RECONCILIATION, never "
                "success)"
            ) from None
        try:
            expected = custody.capture_replacement_expectation(
                anchor_fd,
                path.name,
                current,
                run_id=_ANCHOR_RUN_ID,
                purpose="g4 seal runstate anchor",
            )
            custody.atomic_write(
                path.parent,
                anchor_fd,
                path.name,
                _anchor_bytes(
                    replace(
                        record,
                        ledger_size=ledger_bytes,
                        committed_tail_sha256=committed_tail_sha256,
                        companion_sha256=companion_sha256,
                    )
                ),
                run_id=_ANCHOR_RUN_ID,
                purpose="g4 seal runstate anchor",
                mode=0o644,
                exclusive=False,
                expected=expected,
            )
        except StoreCustodyError as exc:
            raise LedgerCorruptError(
                f"{path}: the committed extent could not be re-anchored ({exc.detail}) — "
                "the append itself is durable, so this refusal is RECONCILIATION, "
                "never success"
            ) from None
    finally:
        os.close(anchor_fd)


def _verify_or_bind_runstate_anchor(resolved_root: Path, root_fd: int, ledger_fd: int) -> None:
    """The append-side anchor rule (under the flock): verify the anchor
    against the held inode; bind it when the ledger is still empty and the
    anchor is absent (creation, or the crash window between the companion
    write and the anchor write); a NON-EMPTY ledger with no anchor is
    reconciliation, never an append and never a silent re-bind."""
    record = _load_runstate_anchor(resolved_root)
    if record is None:
        held_size = os.fstat(ledger_fd).st_size
        if held_size != 0:
            raise LedgerCorruptError(
                f"{resolved_root / LEDGER_FILENAME}: the ledger holds {held_size} bytes "
                "with no runstate anchor — an unanchored non-empty ledger is never "
                "appended and never silently re-anchored in place; reconcile with the "
                "owner (this refusal is RECONCILIATION, never success)"
            ) from None
        _write_runstate_anchor(resolved_root, root_fd, ledger_fd)
        return
    _check_runstate_anchor(resolved_root, root_fd, ledger_fd, record)


def _verify_runstate_anchor_read_side(resolved_root: Path, root_fd: int, ledger_fd: int) -> None:
    """The read-side anchor rule: a PRESENT anchor must match the held inode
    and the companion digest; a MISSING anchor is tolerated only while the
    ledger carries no authority yet (an empty file — the creation crash
    window, closed at the next append); a NON-EMPTY ledger with no anchor is
    reconciliation, never authority."""
    record = _load_runstate_anchor(resolved_root)
    if record is None:
        held_size = os.fstat(ledger_fd).st_size
        if held_size != 0:
            raise LedgerCorruptError(
                f"{resolved_root / LEDGER_FILENAME}: the ledger holds {held_size} bytes "
                "with no runstate anchor — an unanchored non-empty ledger is never read "
                "as authority; reconcile with the owner (this refusal is RECONCILIATION, "
                "never success)"
            ) from None
        return
    _check_runstate_anchor(resolved_root, root_fd, ledger_fd, record)


def _verify_or_bind_ledger_name(root: Path, root_fd: int, ledger_fd: int) -> custody.NameBinding:
    """Round-11 review fix (2026-08-25, finding 3): the durable name→inode
    binding — the successor-window closer.

    The round-8 in-append name check can only see swaps that land BEFORE it;
    a swap landing after it but before the return left a byte-copy clone at
    the authority name that a SECOND process then consumed (split authority:
    one consumption under the renamed file, another on the clone). At ledger
    creation (an empty ledger, under the flock, BEFORE the first append
    lands) the file's ``(st_dev, st_ino)`` is recorded in a companion
    identity record written through custody; every open after that verifies
    the name still maps to the bound inode. A clone has the wrong inode and
    is refused at the next open — it can never be consumed. Unbound NON-EMPTY
    ledgers (pre-binding era, or a deleted record) are reconciliation, never
    an append and never a silent re-bind.

    Round-11 review fix (2026-08-25, finding 1, R13): the companion alone is
    co-replaceable OFFLINE (a clone plus a replacement companion naming the
    clone), so the ledger identity is ALSO anchored in a second tree — the
    runstate store. Creation writes that anchor immediately after the
    companion; every later append verifies BOTH trees.

    R15 (finding 4): returns the binding (which now carries the companion's
    COMMITTED EXTENT) so the caller runs the class extent check against BOTH
    durable records.
    """
    purpose = "ledger.jsonl authority"
    binding = custody.load_name_binding(
        root, root_fd, LEDGER_FILENAME, purpose=purpose, refuse=_binding_refusal
    )
    if binding is None:
        held = os.fstat(ledger_fd)
        if held.st_size != 0:
            raise LedgerCorruptError(
                f"{root / LEDGER_FILENAME}: the ledger holds {held.st_size} bytes "
                "with no durable name binding — an unbound ledger is never "
                "appended or re-bound in place; reconcile with the owner "
                "(this refusal is RECONCILIATION, never success)"
            ) from None
        binding = custody.bind_name_identity(
            root, root_fd, LEDGER_FILENAME, ledger_fd, purpose=purpose, refuse=_binding_refusal
        )
        _write_runstate_anchor(root, root_fd, ledger_fd)
        return binding
    custody.verify_name_binding(
        root_fd,
        LEDGER_FILENAME,
        ledger_fd,
        binding,
        purpose=purpose,
        refuse=_binding_refusal,
    )
    _verify_or_bind_runstate_anchor(root, root_fd, ledger_fd)
    return binding


def _verify_bound_ledger_name(
    root: Path, root_fd: int, ledger_fd: int
) -> custody.NameBinding | None:
    """The read-side rule: a bound name that stopped mapping to its bound
    inode is refused; an EMPTY unbound ledger carries no authority yet
    (creation crash window — it is bound at the next append); a NON-EMPTY
    unbound ledger is reconciliation.

    Round-11 review fix (2026-08-25, finding 1, R13): BOTH trees are
    verified — the beside-the-file companion AND the runstate anchor. A
    present anchor that names a different inode is the co-replacement
    refusal; a missing anchor is tolerated only while the ledger carries no
    authority yet (an empty file — the creation crash window).

    R15 (finding 4): returns the binding (None when nothing was ever bound)
    for the caller's committed-extent check."""
    purpose = "ledger.jsonl authority"
    binding = custody.load_name_binding(
        root, root_fd, LEDGER_FILENAME, purpose=purpose, refuse=_binding_refusal
    )
    if binding is None:
        if os.fstat(ledger_fd).st_size != 0:
            raise LedgerCorruptError(
                f"{root / LEDGER_FILENAME}: the ledger holds "
                f"{os.fstat(ledger_fd).st_size} bytes with no durable name "
                "binding — an unbound ledger is never read as authority; "
                "reconcile with the owner (this refusal is RECONCILIATION, "
                "never success)"
            ) from None
        _verify_runstate_anchor_read_side(root, root_fd, ledger_fd)
        return None
    custody.verify_name_binding(
        root_fd,
        LEDGER_FILENAME,
        ledger_fd,
        binding,
        purpose=purpose,
        refuse=_binding_refusal,
    )
    _verify_runstate_anchor_read_side(root, root_fd, ledger_fd)
    return binding


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
                # Round-11 (finding 3): bind at creation (empty ledger,
                # before the first append lands) or verify the name still
                # maps to the bound inode — under the flock either way.
                # R15 (finding 4): the binding carries the companion's
                # committed extent for the class extent check below.
                binding = _verify_or_bind_ledger_name(root, root_fd, fd)
                chunks: list[bytes] = []
                offset = 0
                while True:
                    chunk = os.pread(fd, 65536, offset)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    offset += len(chunk)
                raw = b"".join(chunks)
                view = _replay_text(raw.decode("utf-8", errors="replace"))
                if view.tail_damaged:
                    raise LedgerCorruptError(
                        "the ledger's final line is torn; reconcile it (append a "
                        "RECONCILIATION_NOTE from a durable root) before any further "
                        "append — appending past it would hide an unacknowledged write"
                    )
                # Round-12 (finding 1, R14) + R15 (findings 1 and 4): never
                # append onto a ledger that no longer holds its committed
                # extent in full as a PROVEN prefix — a rolled-back or padded
                # rewrite must not be re-spent by this append. BOTH durable
                # extent records (anchor and companion) are checked.
                _refuse_anchor_extent_rollback(
                    root,
                    binding=binding,
                    ledger_bytes=offset,
                    view=view,
                    raw_ledger_bytes=raw,
                )
                if record.prev_record_sha256 != view.tail_hash:
                    raise LedgerCorruptError(
                        "supplied prev_record_sha256 does not match the verified "
                        "ledger tail — rebuild the record from read_ledger()"
                    )
                signed = record.model_copy(update={"record_sha256": _record_hash(record)})
                os.lseek(fd, 0, os.SEEK_END)
                # Round-11 (finding 5): the looped authority write — a short
                # write is completed (or raises), never acknowledged torn.
                line = (_encode(signed) + "\n").encode("utf-8")
                custody.write_all(fd, line)
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
                # R15 (finding 4): the append is durable and the name still
                # maps to the locked inode, so the companion's COMMITTED
                # EXTENT advances FIRST (an identity-conditional custody
                # replacement), and the anchor's extent commit then re-pins
                # the ADVANCED companion's digest — the anchor's companion
                # pin tracks the bytes that now guard the ledger. A crash
                # between the two writes leaves the companion ahead of the
                # anchor: the open-side digest tolerance accepts exactly that
                # advance shape, both extents prove as prefixes, and the next
                # append re-advances both. A refusal on either path leaves
                # the record durable and the next open accepting it only
                # through the prefix proof (the crash-window case).
                custody.advance_name_binding_extent(
                    root,
                    root_fd,
                    LEDGER_FILENAME,
                    fd,
                    new_extent_size=offset + len(line),
                    new_committed_tail_sha256=signed.record_sha256,
                    purpose="ledger.jsonl authority",
                    refuse=_binding_refusal,
                )
                advanced_companion = custody.read_named_bytes(
                    root,
                    root_fd,
                    custody.name_binding_filename(LEDGER_FILENAME),
                    run_id=_ANCHOR_RUN_ID,
                    purpose="ledger.jsonl authority name binding",
                )
                assert advanced_companion is not None  # the advance just wrote it
                # Round-12 review fix (2026-08-25, finding 1, R14): the
                # anchor's COMMITTED EXTENT advances as the last act under
                # the flock.
                _commit_anchor_extent(
                    root,
                    ledger_bytes=offset + len(line),
                    committed_tail_sha256=signed.record_sha256,
                    companion_sha256=sha256_hex(advanced_companion),
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


def append_reconciliation(
    root: Path, identity: SealedIdentity, *, reason: str, at_epoch: int
) -> LedgerRecord:
    """Re-arm one-shot authority for sealed CONTENT consumed without a
    verdict (library API — the owner's act, recorded by the orchestrator on
    instruction exactly like an approval).

    The identity must be the CONSUMED run's own tuple (the record binds BOTH
    recomputed ids to that exact checkout): a reconciliation may only follow
    a CONSUMPTION record that already holds it, so it re-arms a real
    consumed-without-verdict spend and can never pre-authorize a re-run of
    content nothing has spent yet. Each record permits exactly ONE further
    consumption — the budget arithmetic is ``_check_authority``'s
    (scripts/g4_seal.py), which counts consumptions against reconciliations
    per content identity. The exact consumed CHECKOUT is never re-runnable:
    the sealed-run-id arm of the authority check stays absolute regardless
    of any budget."""
    run_id = sealed_run_id(identity)
    view = read_ledger(root)
    for record in view.records:
        if record.kind != KIND_CONSUMPTION:
            continue
        try:
            record_run_id = sealed_run_id(record.identity)
            record_content_id = content_identity(record.identity)
        except Exception:
            raise LedgerCorruptError(
                f"CONSUMPTION record {record.record_sha256[:12]}… has an"
                " unparseable identity payload; the reconciliation cannot"
                " be joined to it safely — refusing to append"
            ) from None
        if record.sealed_run_id != record_run_id or record.content_identity != record_content_id:
            raise LedgerCorruptError(
                f"CONSUMPTION record {record.record_sha256[:12]}… stored"
                " ids disagree with its own identity payload (corruption)"
            )
        if record.identity == identity:
            return _append_kind(
                root, KIND_RECONCILIATION, identity, reason=reason, at_epoch=at_epoch
            )
    raise ReconciliationInvalidError(
        run_id,
        "no CONSUMPTION record in the ledger holds this exact consumed"
        " identity — a reconciliation re-arms an existing"
        " consumed-without-verdict spend; it is never minted ahead of one",
    )
