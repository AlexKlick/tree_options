"""Protocol 0.2.1 amendment BUILDER — dry-run only (PR A3).

This module is structurally incapable of landing anything. It never chooses
a threshold value, never writes a tracked file, and every packet it emits
says ``landed: false``. It consumes three inputs — a coverage-era census
artifact, OWNER-SUPPLIED values, and OWNER-RATIFIED derivation rules — and
emits a proposal under ``artifacts/`` only, for the owner to read.

Pipeline (first failure wins; every failure is a refusal, never a landing):

1.  the census parses and passes its fail-closed verification (content hash
    recomputed, taxonomy intact);
2.  the census still describes the capture manifest ON DISK NOW (staleness
    double-check against ``provenance.input_manifest_sha256``);
3.  the owner-values doc parses strictly (NaN/Infinity refused, bools never
    pass as ints) and binds itself to the census content hash;
4.  every ratified rule binds itself to the same census content hash;
5.  the base protocol loads through the real loader and is exactly 0.2.0,
    and the target is exactly its patch+1 (0.2.1);
6.  every derived value recomputes exactly from facts the census classes
    ``observed_census_fact`` (anything else is future-derived and refused);
    each fact a rule REFERENCES must additionally be a strict int observed
    with confidence ``EXACT`` — a DERIVATION-TIME check per referenced
    fact, so an unreferenced non-EXACT observation (e.g. the numeric
    ``bar_volume_observations`` the canonical producer always emits as
    NOT_EVALUABLE) blocks nothing and the value must become an
    owner_deviation instead; every owner deviation carries a recorded
    decision reference;
7.  ``flow_min_session_volume`` is present as a real int > 0 — a missing or
    zero threshold is exactly the silent default this builder exists to
    prevent;
8.  the output root resolves under the repo's ``artifacts/`` directory;
9.  the packet is emitted under ``<out-root>/<census-hash[:12]>/`` — with
    the derived directory and every output path re-resolved and confined
    under the resolved out root (a precreated symlink refuses, it is never
    written through), the output PARENT then held under a component-wise
    no-follow custody fd for the emit (round-8: no operation re-resolves a
    path component after confinement), and the proposed protocol re-loaded
    from the RENDERED text through TODAY'S loader as proof it round-trips.

Output is byte-identical across re-runs over identical inputs: no clock, no
timestamps, no absolute paths in any emitted byte.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import os
import secrets
import stat
from pathlib import Path
from typing import Any, Literal, NoReturn

import yaml
from pydantic import Field, StrictInt, field_validator, model_validator

from tree_options.data.coverage_census import (
    CoverageCensus,
    census_content_sha256,
    verify_census,
)
from tree_options.protocol.loader import load_protocol_bytes, protocol_hash
from tree_options.protocol.schema import ResearchProtocol
from tree_options.schemas.common import StrictModel

OWNER_VALUES_SCHEMA_VERSION = "m4-owner-values/1"
AMENDMENT_PACKET_SCHEMA_VERSION = "m4-amendment-packet/1"
BASE_PROTOCOL_VERSION = "0.2.0"
PROPOSED_PROTOCOL_VERSION = "0.2.1"
FLOW_MIN_SESSION_VOLUME_ID = "flow_min_session_volume"
# Determinism: the amendment date is a pending marker, never a clock read.
AMENDMENT_DATE_PENDING = "PENDING-OWNER-RATIFICATION"


# ---- errors ------------------------------------------------------------------------


class AmendmentError(Exception):
    """Base: the builder refused. Every failure is a refusal, never a landing."""


class StaleCensusError(AmendmentError):
    """The census is invalid, tampered, or no longer describes the manifest."""


class OwnerValuesError(AmendmentError):
    """The owner-values or ratified-rules input is invalid or unbound."""


class VersionError(AmendmentError):
    """The base protocol is not 0.2.0, or the target is not its patch+1."""


class DerivationMismatchError(AmendmentError):
    """A supplied value disagrees with its rule, or derives from the future."""


class OutputRefusedError(AmendmentError):
    """The requested output root is outside artifacts/ (tracked-file protection)."""


# ---- owner-supplied values ---------------------------------------------------------


class OwnerValue(StrictModel):
    """One owner-supplied policy value: a number plus WHY it is that number.

    Either a derivation from census-observed facts under a ratified rule, or
    a recorded owner deviation. The value is a STRICT int: a bool is not a
    threshold, and neither is a float or a string.
    """

    id: str = Field(min_length=1)
    value: int
    provenance: Literal["derivation", "owner_deviation"]
    rule_id: str | None = None
    deviation_record: str | None = None

    @field_validator("value", mode="before")
    @classmethod
    def _value_is_strict_int(cls, v: object) -> int:
        if isinstance(v, bool):
            raise ValueError(f"OwnerValue.value: bool {v!r} is not a threshold value")
        if not isinstance(v, int):
            raise ValueError(f"OwnerValue.value must be a strict int, got {type(v).__name__}")
        return v

    @model_validator(mode="after")
    def _provenance_carries_its_evidence(self) -> OwnerValue:
        if self.provenance == "derivation":
            if self.rule_id is None:
                raise ValueError("provenance=derivation requires rule_id")
            if self.deviation_record is not None:
                raise ValueError("provenance=derivation must not carry deviation_record")
        else:
            if self.deviation_record is None:
                raise ValueError("provenance=owner_deviation requires deviation_record")
            if self.rule_id is not None:
                raise ValueError("provenance=owner_deviation must not carry rule_id")
        return self


class OwnerValuesDoc(StrictModel):
    """The owner's value sheet, bound to exactly one census by content hash."""

    census_content_sha256: str = Field(min_length=1)
    values: tuple[OwnerValue, ...] = Field(min_length=1)

    @field_validator("values")
    @classmethod
    def _ids_unique(cls, v: tuple[OwnerValue, ...]) -> tuple[OwnerValue, ...]:
        ids = [ov.id for ov in v]
        if len(set(ids)) != len(ids):
            duplicated = sorted({i for i in ids if ids.count(i) > 1})
            raise ValueError(f"duplicate owner value ids: {duplicated}")
        return v


# ---- ratified derivation rules -----------------------------------------------------


class FactRef(StrictModel):
    """A reference to one census fact id."""

    fact: str = Field(min_length=1)


class OpNode(StrictModel):
    """One whitelisted combinator node. Integer arithmetic only."""

    op: Literal["max", "min", "floor_div", "mul"]
    args: tuple[Expr, ...] = Field(min_length=2)


Expr = StrictInt | FactRef | OpNode
OpNode.model_rebuild()


class DerivationRule(StrictModel):
    """An owner-ratified derivation: an expression bound to one census."""

    rule_id: str = Field(min_length=1)
    census_binding: str = Field(min_length=1)
    expression: Expr


class RatifiedRulesDoc(StrictModel):
    rules: tuple[DerivationRule, ...] = Field(min_length=1)

    @field_validator("rules")
    @classmethod
    def _rule_ids_unique(cls, v: tuple[DerivationRule, ...]) -> tuple[DerivationRule, ...]:
        ids = [r.rule_id for r in v]
        if len(set(ids)) != len(ids):
            duplicated = sorted({i for i in ids if ids.count(i) > 1})
            raise ValueError(f"duplicate rule ids: {duplicated}")
        return v


def referenced_facts(node: Expr) -> tuple[str, ...]:
    """Every fact id an expression can reach, in evaluation order."""
    if isinstance(node, FactRef):
        return (node.fact,)
    if isinstance(node, OpNode):
        collected: list[str] = []
        for arg in node.args:
            collected.extend(referenced_facts(arg))
        return tuple(collected)
    return ()


def evaluate_expression(node: Expr, facts: dict[str, int]) -> int:
    """Walk one expression node. Integer arithmetic only."""
    if isinstance(node, int):
        return node
    if isinstance(node, FactRef):
        try:
            return facts[node.fact]
        except KeyError as exc:
            raise AmendmentError(f"derivation references unknown fact {node.fact!r}") from exc
    values = (evaluate_expression(arg, facts) for arg in node.args)
    if node.op == "max":
        return max(values)
    if node.op == "min":
        return min(values)
    if node.op == "mul":
        product = 1
        for v in values:
            product *= v
        return product
    # floor_div: left fold over the arguments
    quotient: int | None = None
    for v in values:
        quotient = v if quotient is None else quotient // v
    if quotient is None:  # pragma: no cover - min_length=2 keeps this unreachable
        raise AmendmentError("floor_div needs at least two arguments")
    return quotient


def evaluate(rule: DerivationRule, facts: dict[str, int]) -> int:
    """Evaluate a rule's expression over int-valued facts."""
    try:
        return evaluate_expression(rule.expression, facts)
    except ZeroDivisionError as exc:
        raise OwnerValuesError(f"rule {rule.rule_id!r} divides by zero") from exc


# ---- emitted packet ----------------------------------------------------------------


class AmendmentInputs(StrictModel):
    """Raw-byte SHA-256 of every input the builder consumed."""

    census_file_sha256: str
    owner_values_file_sha256: str
    rules_file_sha256: str
    protocol_file_sha256: str
    capture_manifest_file_sha256: str


class EmittedArtifact(StrictModel):
    name: str
    sha256: str


class AmendmentPacket(StrictModel):
    """The typed record of one dry-run build. ``landed`` is pinned false by
    the type itself: no value of this model can claim to have landed."""

    schema_version: str
    base_version: str
    proposed_version: str
    census_content_sha256: str
    protocol_hash_base: str
    flow_min_session_volume: int
    owner_values_schema_version: str
    inputs: AmendmentInputs
    emitted: tuple[EmittedArtifact, ...]
    landed: Literal[False] = False


# ---- build -------------------------------------------------------------------------


def _repo_root() -> Path:
    # src/tree_options/protocol/amendment.py -> repo root is parents[3].
    return Path(__file__).resolve().parents[3]


def _confine_output(path: Path, *, out_root: Path) -> Path:
    """Round-3 review fix (2026-08-23, finding 3): re-resolve one output
    path and require it to stay under the resolved out root. The out-root
    check alone cannot see a precreate: the derived hash directory (or one
    output filename) symlinked outside artifacts/ — or at a tracked file —
    would otherwise be written straight through.

    Round-6 review fix (2026-08-24, finding 2): an output path that is
    ITSELF a symlink is refused outright even when its target resolves
    IN ROOT. ``protocol-0.2.1-proposed.yaml -> amendment-packet.json`` (both
    inside the permitted hash dir) used to pass confinement; the write then
    landed under the RESOLVED (packet) name, the later packet write
    overwrote it, and the builder succeeded with two of its own artifacts
    aliased to one file and ``emitted`` carrying wrong hashes. Two own
    artifacts aliasing one file is never legitimate for this builder. This
    is the layer that guards every output path — the derived hash directory
    and all four emit sites route through here; ``_write_exclusive`` then
    publishes via an unpredictable temp + ``os.replace``, which can only
    swap a directory entry, never write through a late-planted link."""
    resolved = path.resolve()
    if not resolved.is_relative_to(out_root):
        raise OutputRefusedError(
            f"output path {path} resolves to {resolved}, outside the resolved "
            f"output root {out_root}: refusing to write through it"
        )
    if path.is_symlink():
        raise OutputRefusedError(
            f"output path {path} is itself a symlink (to {resolved}): two "
            "of this builder's own artifacts must never alias one file — "
            "refusing to write through it"
        )
    return resolved


def _refuse_shared_inode(path: Path) -> None:
    """Round-4 review fix (2026-08-23, finding 2): Path.resolve() detects
    symlinks but not HARD-LINK aliasing. An output FILE precreated as a hard
    link to a tracked file keeps its resolved path inside the output root, so
    confinement passed and write_text truncated the shared inode. A link
    count > 1 is aliasing regardless of how the link was created; only an
    absent path or a sole link may be written.

    Round-5 review fix (2026-08-24, finding 1): this check is now only the
    FAST refusal. It and the write were separate operations, so an
    interleaving process could plant a hard link at the output name AFTER the
    check and BEFORE the write — and write_text truncated the shared tracked
    inode anyway. The write itself now holds custody: see _write_exclusive."""
    if not path.exists():
        return
    nlink = os.stat(path).st_nlink
    if nlink > 1:
        raise OutputRefusedError(
            f"output file {path} has {nlink} hard links: the inode is shared "
            "(a tracked file?) however the link was created — refusing to "
            "write through it"
        )


def _open_output_custody(out_dir: Path) -> int:
    """Round-8 review fix (2026-08-24, finding 6): hold the output PARENT
    under custody — a component-wise no-follow walk mirroring
    ``seal.ledger._open_ledger_root``.

    ``_confine_output`` resolves once, but the write path used to RE-RESOLVE
    every intermediate component after that (``mkstemp(dir=path.parent)``,
    ``lstat``, the ``O_NOFOLLOW`` open). An attacker renaming the hash dir
    once the last confinement check passed — and planting it as a directory
    symlink — made the builder create, publish, and byte-verify INSIDE the
    link's target: an out-of-root write with no refusal. Here ``/`` is
    opened once with ``O_DIRECTORY``, then EVERY component of the resolved
    out dir (already resolved by confinement — never re-resolved here, which
    would follow a swapped ancestor) is opened
    ``O_RDONLY|O_DIRECTORY|O_NOFOLLOW`` relative to the previous component's
    fd (which is then closed). ``ELOOP``/``ENOTDIR`` at ANY component is an
    ``OutputRefusedError`` naming the offending component; a vanished
    component (``ENOENT``) equally refuses."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(os.sep, os.O_RDONLY | os.O_DIRECTORY)
    for component in out_dir.parts[1:]:
        prev = fd
        try:
            fd = os.open(component, flags, dir_fd=prev)
        except OSError as exc:
            os.close(prev)
            if exc.errno in (errno.ELOOP, errno.ENOTDIR, errno.ENOENT):
                if exc.errno == errno.ENOENT:
                    detail = "vanished under custody"
                else:
                    detail = "is not a real directory (a symlinked path component)"
                raise OutputRefusedError(
                    f"output directory component {component!r} of {out_dir} "
                    f"{detail} (opened O_NOFOLLOW|O_DIRECTORY component-wise "
                    f"from /, errno {exc.errno}) — this builder never creates "
                    "or writes through a symlinked path component"
                ) from None
            raise
        os.close(prev)
    return fd


def _absent_output_ancestors(out_dir: Path) -> tuple[Path, ...]:
    """R15 review fix (2026-08-25, finding 7, R14): the ancestors of the
    content directory that do NOT exist yet — the components
    ``out_dir.mkdir(parents=True, exist_ok=True)`` is about to create,
    OUTERMOST FIRST.

    Snapshotted BEFORE the mkdir so the commit below can fsync the parent of
    every component THIS run creates: the output root, and deeper ancestors
    (up to and including ``artifacts/``) when the output root names a
    hierarchy that does not exist yet."""
    absent: list[Path] = []
    for ancestor in out_dir.parents:
        if ancestor.exists():
            break
        absent.append(ancestor)
    absent.reverse()
    return tuple(absent)


def _commit_created_output_entries(created: tuple[Path, ...]) -> None:
    """R15 review fix (2026-08-25, finding 7, R14): commit every directory
    entry the output hierarchy just created IN THE PARENT THAT HOLDS IT — a
    component-wise no-follow custody walk (``_open_output_custody``) to each
    created directory's parent, then ``fsync`` of that parent.

    ``mkdir(parents=True)`` can create the output root ITSELF (and deeper
    ancestors, up to ``artifacts/``), and no fsync anywhere in this module
    covered those entries — the only one covered the FILE temps. A crash
    after the acknowledged packet return could lose the rename entries, the
    content directory, or an ancestor of the output root. Outermost first: a
    parent entry is committed before the children named inside it. Refusal
    is the ``OutputRefusedError`` family — the builder never publishes over
    an uncommitted hierarchy."""
    for directory in created:
        parent_fd = _open_output_custody(directory.parent)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)


def _commit_output_chain(out_root: Path) -> None:
    """R16 review fix (2026-08-25, finding 2 of the round-14 review): commit
    the parent of EVERY traversed component of the output chain — the
    outermost ancestor of ``out_root`` down through ``out_root`` itself,
    created by THIS run or left behind by a prior crashed one — then
    out_root's own entries, in ONE component-wise no-follow custody walk.

    A durable traversal mirroring ``runstate.custody.open_directory(
    durable=True)`` (the round-15 authority-walk contract): the walk fsyncs
    the PARENT fd for every component it passes through, existing-open
    components just as much as created ones. Restart closure for this
    publication walk: the R15 absent-only snapshot committed only components
    that did not exist yet, so an output root a crashed invocation created
    without committing stayed uncommitted through every retry — the builder
    returned the packet (CLI exit 0) over an outer hierarchy a reboot could
    drop whole. The final ``fsync`` of out_root itself commits the content
    directory's entry in it. Refusal is the ``OutputRefusedError`` family:
    the builder never attests a packet over an uncommitted chain."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(os.sep, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in out_root.parts[1:]:
            previous = fd
            try:
                fd = os.open(component, flags, dir_fd=previous)
            except OSError as exc:
                os.close(previous)
                if exc.errno in (errno.ELOOP, errno.ENOTDIR, errno.ENOENT):
                    raise OutputRefusedError(
                        f"output chain component {component!r} of {out_root} "
                        "could not be opened for the durable commit "
                        f"(errno {exc.errno}) — never attest a packet over an "
                        "uncommitted directory chain"
                    ) from None
                raise
            try:
                os.fsync(previous)
            finally:
                os.close(previous)
        os.fsync(fd)
    finally:
        os.close(fd)


def _emit_custody_write(path: Path, text: str, *, custody_fd: int) -> tuple[int, int]:
    """Round-8 review fix (finding 6) + round-11 review fix (finding 1):
    ONE emit = confinement check (the caller's, FIRST and unchanged) -> the
    dir_fd-relative exclusive write THROUGH THE SINGLE HELD CUSTODY FD. The
    pre-fix shape opened (and closed) a fresh custody walk per emit, so no
    fd was held from the first artifact to the packet's return; the caller
    now opens the output parent ONCE and holds it across ALL FOUR emits
    (three artifacts + packet), and the final sweep at packet return rides a
    fresh re-walk of the resolved out dir. Returns the published identity."""
    return _write_exclusive(path, text, custody_fd=custody_fd)


def _write_exclusive(path: Path, text: str, *, custody_fd: int) -> tuple[int, int]:
    """Round-5 review fix (2026-08-24, finding 1): publish one output file
    with custody held through the whole write.

    A plain ``write_text`` opens the FINAL NAME, so a hard link planted at
    that name between the shared-inode check and the write is written
    straight through — truncating whatever tracked file shares the inode.
    Instead the bytes go to a sibling TEMP file inside the same confined
    directory, are fsynced, are fstat-checked (``st_nlink`` must be exactly
    1), and only then are moved into place with ``os.replace``. ``replace``
    swaps the DIRECTORY ENTRY: a link planted at the output name is unlinked
    by the swap and the shared inode behind it is never written through.

    Round-6 review fix (2026-08-24, finding 1): the temp NAME is now
    UNPREDICTABLE and the publish is VERIFIED. The pre-fix temp name was
    ``.{name}.{pid}.tmp`` — guessable — and ``replace`` was trusted blindly:
    an attacker could rename the temp file away mid-write (the open fd keeps
    writing the RENAMED inode; the fstat above still sees one link), plant
    attacker bytes at the original temp name, and ``replace`` then published
    the ATTACKER inode while the builder returned its legitimate in-memory
    packet. The temp name is now unguessable (``secrets.token_hex`` —
    ``mkstemp`` cannot take a ``dir_fd``), and the inode this function wrote
    — ``(st_dev, st_ino)`` captured from fstat before the close — must be
    the inode seen at the output name after the replace; any mismatch is a
    refusal naming both identities.

    Round-7 review fix (2026-08-24, finding 1): inode identity is NOT final-
    name or byte custody. After the replace an attacker renames the published
    file to a sibling ``.held`` (inode unchanged), plants ``path -> .held`` as
    a SYMLINK, and rewrites ``.held`` in place with schema-valid protocol YAML
    that preserves protocol_version, the flow value, and the amendment count:
    ``os.stat(path)`` FOLLOWS the link so the identity check passed, and the
    round-trip load plus ``emitted``'s ``read_bytes`` both read through the
    link — the builder attested attacker YAML. The publish verification is
    now final-name + byte custody: (a) the final name — un-followed — must
    be a REGULAR file, a symlink there never is; (b) identity from the
    LSTAT values; (c) the final name opened ``O_RDONLY|O_NOFOLLOW``, fstat
    identity re-checked, and the FULL bytes read back must EQUAL the text
    this function wrote — a mismatch refuses naming both contents.

    Round-8 review fix (2026-08-24, finding 1): RETURNS the published inode
    identity ``(st_dev, st_ino)`` so the caller can carry custody past this
    function's return. Custody here ends when the verify fd closes; the
    builder's proof step therefore parses the RENDERED text, ``emitted``
    hashes the RENDERED bytes, and ``_verify_final_effect`` re-checks this
    identity at the packet attestation — the final effect.

    Round-8 review fix (2026-08-24, finding 6): EVERY operation below is
    ``custody_fd``-RELATIVE — the temp create, the replace, the lstat, and
    the readback all address bare names inside the output parent held under
    custody (see ``_open_output_custody``), so no operation re-resolves the
    intermediate components a swapped directory symlink could redirect."""
    tmp_name = f".{path.name}.{secrets.token_hex(16)}.tmp"
    try:
        fd = os.open(
            tmp_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o644,
            dir_fd=custody_fd,
        )
    except OSError as exc:
        raise OutputRefusedError(
            f"temp output for {path} could not be created under custody ({exc.strerror})"
        ) from None
    try:
        os.fchmod(fd, 0o644)
        # fdopen owns the fd from here: the with-block closes it on every path.
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
            written = os.fstat(handle.fileno())
        if written.st_nlink != 1:
            raise OutputRefusedError(
                f"temp output {path.parent / tmp_name} has {written.st_nlink} hard "
                "links: the inode is shared — refusing to publish it over the "
                "output name"
            )
        os.replace(tmp_name, path.name, src_dir_fd=custody_fd, dst_dir_fd=custody_fd)
        # (a) the final name, un-followed: a symlink planted at the output
        # name after the replace is not this builder's artifact, whatever it
        # points at (os.stat would FOLLOW it and see the moved inode).
        try:
            published = os.stat(path.name, dir_fd=custody_fd, follow_symlinks=False)
        except OSError as exc:
            raise OutputRefusedError(
                f"published output {path} vanished after the publish ({exc.strerror})"
            ) from None
        if not stat.S_ISREG(published.st_mode):
            raise OutputRefusedError(
                f"published output {path} is not a regular file (lstat mode "
                f"{stat.S_IFMT(published.st_mode):o} — a symlink at the final "
                "name is never this builder's artifact): refusing to attest it"
            )
        # (b) identity from the lstat values.
        if (published.st_dev, published.st_ino) != (written.st_dev, written.st_ino):
            raise OutputRefusedError(
                f"published output {path} is not the inode this builder wrote "
                f"(wrote dev {written.st_dev} ino {written.st_ino}, published "
                f"dev {published.st_dev} ino {published.st_ino}): the temp name "
                "was stolen and attacker bytes were published in its place — "
                "refusing to attest them"
            )
        # (c) byte custody: open the FINAL NAME without following a link at
        # it, re-check the identity on that fd, and require the full content
        # to equal what this function wrote.
        try:
            verify_fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=custody_fd)
        except OSError as exc:
            raise OutputRefusedError(
                f"published output {path} could not be opened without following "
                f"a symlink ({exc.strerror}): refusing to attest it"
            ) from None
        try:
            opened = os.fstat(verify_fd)
            if (opened.st_dev, opened.st_ino) != (written.st_dev, written.st_ino):
                raise OutputRefusedError(
                    f"published output {path} changed identity under the readback "
                    f"(wrote dev {written.st_dev} ino {written.st_ino}, read back "
                    f"dev {opened.st_dev} ino {opened.st_ino}): refusing to attest it"
                )
            chunks: list[bytes] = []
            offset = 0
            while True:
                chunk = os.pread(verify_fd, 65536, offset)
                if not chunk:
                    break
                chunks.append(chunk)
                offset += len(chunk)
        finally:
            os.close(verify_fd)
        expected = text.encode("utf-8")
        readback = b"".join(chunks)
        if readback != expected:
            raise OutputRefusedError(
                f"published output {path} does not hold the bytes this builder "
                f"wrote (wrote sha {hashlib.sha256(expected).hexdigest()[:12]}…, "
                f"read back sha {hashlib.sha256(readback).hexdigest()[:12]}…): the "
                "final name was swapped or rewritten after the publish — "
                "refusing to attest either content"
            )
    finally:
        # replace consumed the temp name on success; on any refusal the
        # half-written temp must not linger under artifacts/ (unlinked by
        # NAME inside the custody fd — never a path re-resolution).
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_name, dir_fd=custody_fd)
    # Round-8 review fix (2026-08-24, finding 1): the caller keeps the
    # rendered text and the PUBLISHED IDENTITY — the proof step parses the
    # rendered text (never a fresh read of the path), `emitted` hashes the
    # rendered bytes, and the final-effect sweep (_verify_final_effect)
    # re-verifies this identity at the moment the packet attests. Custody
    # inside this function ends at the close of the verify fd above; the
    # sweep is what carries it to the packet.
    return (written.st_dev, written.st_ino)


def _verify_final_effect(
    *, artifact: str, path: Path, identity: tuple[int, int], text: str, custody_fd: int
) -> None:
    """Round-8 review fix (2026-08-24, finding 1): re-verify one published
    artifact under custody at the moment the builder ATTESTS it.

    ``_write_exclusive``'s verification ends when its verify fd closes, and
    the builder then used to re-trust the published NAME — the round-trip
    proof re-read the path (``load_protocol(proposed_path)``) and
    ``emitted`` hashed a fresh ``path.read_bytes()`` — so a swap in the
    post-return window (rename the published file to ``.held``, plant the
    output name as a symlink to it, rewrite ``.held`` in place) was parsed
    and hashed as the builder's own artifact while the packet emitted
    successfully. The packet is the FINAL EFFECT: immediately before it is
    constructed (and on the packet itself right after its own write), every
    artifact is re-verified — the final name must lstat as a REGULAR file,
    its ``(st_dev, st_ino)`` must equal the identity ``_write_exclusive``
    published, and an ``O_NOFOLLOW`` re-read must equal the rendered text.
    Any drift is ``OutputRefusedError`` naming the artifact.

    Round-8 review fix (finding 6): the sweep rides the output parent's
    CUSTODY fd — the lstat and the readback address the bare NAME inside
    the custody fd (never a path re-resolution a swapped parent component
    could redirect)."""
    try:
        published = os.stat(path.name, dir_fd=custody_fd, follow_symlinks=False)
    except OSError as exc:
        raise OutputRefusedError(
            f"{artifact} ({path}) vanished before the packet attestation ({exc.strerror})"
        ) from None
    if not stat.S_ISREG(published.st_mode):
        raise OutputRefusedError(
            f"{artifact} ({path}) is not a regular file at attestation time "
            f"(lstat mode {stat.S_IFMT(published.st_mode):o} — a symlink at "
            "the final name is never this builder's artifact): refusing to "
            "attest it"
        )
    if (published.st_dev, published.st_ino) != identity:
        raise OutputRefusedError(
            f"{artifact} ({path}) changed identity after the publish "
            f"(published dev {identity[0]} ino {identity[1]}, now dev "
            f"{published.st_dev} ino {published.st_ino}): refusing to attest "
            "whatever holds the name now"
        )
    try:
        verify_fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=custody_fd)
    except OSError as exc:
        raise OutputRefusedError(
            f"{artifact} ({path}) could not be re-read without following a "
            f"symlink at attestation time ({exc.strerror}): refusing to "
            "attest it"
        ) from None
    try:
        opened = os.fstat(verify_fd)
        if (opened.st_dev, opened.st_ino) != identity:
            raise OutputRefusedError(
                f"{artifact} ({path}) changed identity under the attestation "
                f"readback (published dev {identity[0]} ino {identity[1]}, "
                f"read back dev {opened.st_dev} ino {opened.st_ino}): "
                "refusing to attest it"
            )
        chunks: list[bytes] = []
        offset = 0
        while True:
            chunk = os.pread(verify_fd, 65536, offset)
            if not chunk:
                break
            chunks.append(chunk)
            offset += len(chunk)
    finally:
        os.close(verify_fd)
    expected = text.encode("utf-8")
    readback = b"".join(chunks)
    if readback != expected:
        raise OutputRefusedError(
            f"{artifact} ({path}) does not hold the rendered bytes at "
            f"attestation time (rendered sha {hashlib.sha256(expected).hexdigest()[:12]}…, "
            f"read back sha {hashlib.sha256(readback).hexdigest()[:12]}…): the "
            "final name was swapped or rewritten after the publish — "
            "refusing to attest either content"
        )


def _reject_constant(name: str) -> NoReturn:
    raise ValueError(f"non-finite JSON constant {name!r} refused")


def _load_json_strict(data: bytes) -> Any:
    return json.loads(data, parse_constant=_reject_constant)


def _patch_plus_one(version: str) -> str:
    major, minor, patch = (int(part) for part in version.split("."))
    return f"{major}.{minor}.{patch + 1}"


def _flow_source_note(flow: OwnerValue) -> str:
    if flow.provenance == "derivation":
        return f"derivation rule {flow.rule_id} over observed census facts"
    return f"owner deviation {flow.deviation_record}"


def _render_schema_addition_proposal(
    *, census: CoverageCensus, census_hash: str, flow_value: int
) -> str:
    header = "\n".join(
        [
            "# NOT LANDED - PROPOSAL ONLY.",
            "#",
            '# src/tree_options/protocol/schema.py models are extra="forbid":',
            "# adding a final-holdout-window field to the protocol schema is a",
            "# schema change this PR must not make. This file records the",
            "# proposal for a later, owner-ratified PR; nothing here is",
            "# loadable protocol content.",
        ]
    )
    doc = {
        "NOT_LANDED": True,
        "proposed_schema_addition": {
            "field": "final_holdout_window",
            "target": "protocol holdout declaration (exact location to be ratified by the owner)",
            "rationale": (
                "the G3 packet Ask E declares the holdout only after real"
                " coverage inspection; the proposal must cite this census's"
                " observed grid"
            ),
            "census_content_sha256": census_hash,
            "expected_masters": census.coverage.expected_masters,
            "base_version": BASE_PROTOCOL_VERSION,
            "proposed_version": PROPOSED_PROTOCOL_VERSION,
            "flow_min_session_volume": flow_value,
            "emitted_by": "scripts/build_protocol_amendment.py (dry-run)",
        },
    }
    return header + "\n" + yaml.safe_dump(doc, sort_keys=True, default_flow_style=False)


def _render_diff(
    *,
    base: ResearchProtocol,
    census_hash: str,
    flow_value: int,
    flow_source: str,
) -> str:
    return (
        f"# Amendment diff (PROPOSED — NOT LANDED)\n"
        "\n"
        f"Base: research protocol {base.meta.protocol_version}, loaded and verified through\n"
        "the real loader. Proposed: "
        f"{PROPOSED_PROTOCOL_VERSION}, emitted by scripts/build_protocol_amendment.py.\n"
        "\n"
        f'- meta.protocol_version: "{base.meta.protocol_version}" -> "{PROPOSED_PROTOCOL_VERSION}"\n'
        f"- meta.amendments: {len(base.meta.amendments)} -> {len(base.meta.amendments) + 1} records\n"
        "  (the new record's date is the pending marker "
        f"{AMENDMENT_DATE_PENDING!r}, never a clock read)\n"
        "- option_candidate_defaults.liquidity_volume_flow.flow_min_session_volume:\n"
        f"  null -> {flow_value}\n"
        f"  provenance: {flow_source}\n"
        "- invariants: UNCHANGED (INV-01..INV-14, statements untouched)\n"
        "- schema additions: NONE in this packet. final_holdout_window is proposed\n"
        "  in schema-addition-proposal.yaml under NOT_LANDED: true; protocol models\n"
        '  are extra="forbid" and this PR must not touch them.\n'
        "\n"
        f"Census binding: {census_hash}\n"
        "\n"
        "This file is a proposal. Nothing is landed: research_protocol.yaml is not\n"
        "modified; landed:false in amendment-packet.json is the machine-readable pin.\n"
    )


def build_proposed_amendment(
    census_path: Path,
    owner_values_path: Path,
    rules_path: Path,
    *,
    protocol_path: Path,
    capture_manifest_path: Path,
    out_root: Path,
) -> AmendmentPacket:
    """Build the 0.2.1 proposal packet. Returns the packet (landed: false)."""
    # Round-3 review fix (2026-08-23, finding 4): every input file's bytes
    # are read ONCE here, and both the parse and the packet's input hashes
    # consume those same bytes. The previous shape re-read each file at
    # packet time, so the hashes attested bytes the builder may not have
    # parsed (a swap between parse and packet-build attested the wrong
    # content).
    census_bytes = census_path.read_bytes()
    owner_bytes = owner_values_path.read_bytes()
    rules_bytes = rules_path.read_bytes()
    protocol_bytes = protocol_path.read_bytes()
    manifest_bytes = capture_manifest_path.read_bytes()

    # 1. census: parse + fail-closed verification (recomputed content hash)
    try:
        census = CoverageCensus.model_validate_json(census_bytes)
        verify_census(census)
    except ValueError as exc:
        raise StaleCensusError(f"census invalid or tampered: {exc}") from exc

    # 2. staleness double-check: the census must describe the manifest on disk NOW
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    if manifest_sha256 != census.provenance.input_manifest_sha256:
        raise StaleCensusError(
            "capture manifest drifted since the census: census bound "
            f"{census.provenance.input_manifest_sha256[:12]}…, on disk now "
            f"{manifest_sha256[:12]}…"
        )

    census_hash = census_content_sha256(census)

    # 3. owner values: strict JSON, model validation, census binding
    try:
        owner_doc = OwnerValuesDoc.model_validate(_load_json_strict(owner_bytes))
    except ValueError as exc:
        raise OwnerValuesError(f"owner values doc invalid: {exc}") from exc
    if owner_doc.census_content_sha256 != census_hash:
        raise OwnerValuesError(
            "owner values doc is not bound to this census: doc says "
            f"{owner_doc.census_content_sha256[:12]}…, census is {census_hash[:12]}…"
        )

    # 4. ratified rules: strict JSON, every rule bound to this census
    try:
        rules_doc = RatifiedRulesDoc.model_validate(_load_json_strict(rules_bytes))
    except ValueError as exc:
        raise OwnerValuesError(f"ratified rules doc invalid: {exc}") from exc
    for rule in rules_doc.rules:
        if rule.census_binding != census_hash:
            raise OwnerValuesError(
                f"rule {rule.rule_id!r} is not bound to this census: binding "
                f"{rule.census_binding[:12]}…, census {census_hash[:12]}…"
            )

    # 5. base protocol through the real loader's validation, from the bytes
    # read ONCE above (the packet's protocol hash attests what was parsed);
    # target = its patch+1
    try:
        base = load_protocol_bytes(protocol_bytes)
    except ValueError as exc:
        raise VersionError(
            f"base protocol does not load as {BASE_PROTOCOL_VERSION}: {exc}"
        ) from exc
    base_version = base.meta.protocol_version

    # 5b. Round-1 review fix (2026-08-23, probe NOT_EVALUABLE_FACT_DERIVED +
    # INCOMPLETE_CENSUS_VERIFIED): amendment MUST refuse to derive from an
    # incomplete census (any pair in INCOMPLETE_CLASSES) or from a fact the
    # census classes NOT_EVALUABLE / PARTIAL. The previous code admitted
    # every observed_census_fact int and could `max(bar_volume_observations,
    # 1)` over a zero-marked-NOT_EVALUABLE fact, papering over the exact
    # bar-volume contradiction the runbook says must become an owner
    # deviation. Two gates below:
    #   a) coverage must be COMPLETE (zero INCOMPLETE_CLASSES pairs)
    #   b) every observed fact a rule REFERENCES must be a strict int with
    #      confidence == "EXACT" — checked at DERIVATION TIME, per
    #      referenced fact (round-2 fix, 2026-08-23, finding 3: the round-1
    #      emission-time check refused ANY non-EXACT numeric observation
    #      before any rule was consulted, so the canonical census — whose
    #      producer always emits numeric bar_volume_observations as
    #      NOT_EVALUABLE — could never feed even an owner-deviation
    #      amendment; PARTIAL or NOT_EVALUABLE operands are still refused)
    from tree_options.data.coverage_census import INCOMPLETE_CLASSES

    incomplete = sum(getattr(census.coverage.observed, cls) for cls in INCOMPLETE_CLASSES)
    if incomplete > 0:
        raise StaleCensusError(
            f"census is coverage-INCOMPLETE: {incomplete} pair(s) in "
            f"INCOMPLETE_CLASSES ({sorted(INCOMPLETE_CLASSES)}); an "
            "amendment may only be built against a whole census"
        )
    # Round-5 review fix (2026-08-24, finding 2): zero INCOMPLETE pairs is
    # not wholeness on its own. The census CLI's exit-0 rule is BOTH zero
    # INCOMPLETE_CLASSES pairs AND masters observed == expected_masters —
    # e.g. a one-pair universe whose sealed manifest contains that complete
    # pair PLUS one valid master outside the universe still exits 5 (masters
    # observed 2 != expected 1), yet this gate used to pass it: the out-of-
    # universe master is exactly the un-whole capture an amendment must
    # never be derived from. The observed count is the canonical producer's
    # ``masters_observed`` observation (scripts/build_coverage_census.py);
    # a census that does not carry it as a strict-int observation cannot
    # attest wholeness and refuses here too.
    masters_observed = census.values.observed_census_fact.get("masters_observed")
    if masters_observed is None or type(masters_observed.v) is not int:
        raise StaleCensusError(
            "census does not report a strict-int observed 'masters_observed' "
            "fact (the canonical census producer always emits one): "
            "whole-census coverage cannot be attested — an amendment may "
            "only be built against a whole census"
        )
    expected_masters = census.coverage.expected_masters
    if masters_observed.v != expected_masters:
        raise StaleCensusError(
            f"census is coverage-INCOMPLETE: masters observed "
            f"{masters_observed.v} != expected {expected_masters} (masters "
            "outside the declared universe, or fewer masters than the "
            "universe declares); an amendment may only be built against a "
            "whole census: zero INCOMPLETE_CLASSES pairs AND masters "
            "observed == expected_masters (the census CLI's exit-0 rule)"
        )
    if base_version != BASE_PROTOCOL_VERSION:
        raise VersionError(
            f"base protocol version must be exactly {BASE_PROTOCOL_VERSION!r}, got {base_version!r}"
        )
    target = _patch_plus_one(base_version)
    if target != PROPOSED_PROTOCOL_VERSION:
        raise VersionError(
            f"non-monotonic target: patch+1 of {base_version} is {target!r}, "
            f"expected {PROPOSED_PROTOCOL_VERSION!r}"
        )

    # 6. derivation: observed facts only; computed must equal supplied.
    # Round-2 review fix (2026-08-23, finding 3, probe
    # /tmp/pr-a-amendment-producer-consumer-probe.log): the operand
    # confidence/type gate is DERIVATION-TIME, applied per fact a rule
    # actually references — the facts dict below collects every observed
    # strict int regardless of confidence, and the per-reference gate (after
    # the value_registry class check) refuses anything that is not an EXACT
    # strict-int observation. An UNREFERENCED non-EXACT observation (e.g.
    # the numeric bar_volume_observations the canonical producer always
    # emits as NOT_EVALUABLE) therefore blocks nothing — not even an
    # owner-deviation-only amendment.
    rules_by_id = {r.rule_id: r for r in rules_doc.rules}
    facts: dict[str, int] = {}
    for fact_id, fact in census.values.observed_census_fact.items():
        # exact int only: bools are not facts, textual observations are not numeric
        if type(fact.v) is int:
            facts[fact_id] = fact.v
    for ov in owner_doc.values:
        if ov.provenance == "derivation":
            if ov.rule_id is None:  # the OwnerValue contract already refuses this
                raise OwnerValuesError(f"value {ov.id!r} derivation carries no rule_id")
            ov_rule = rules_by_id.get(ov.rule_id)
            if ov_rule is None:
                raise OwnerValuesError(f"value {ov.id!r} cites unknown rule {ov.rule_id!r}")
            for fid in referenced_facts(ov_rule.expression):
                if census.value_registry.get(fid) != "observed_census_fact":
                    declared = census.value_registry.get(fid, "<not in registry>")
                    raise DerivationMismatchError(
                        f"value {ov.id!r} rule {ov_rule.rule_id!r} references fact "
                        f"{fid!r} classed {declared!r}: only observed_census_fact "
                        "facts exist yet (future-derived)"
                    )
                observed = census.values.observed_census_fact.get(fid)
                if observed is None:
                    # Defensive: verify_census's taxonomy check makes a
                    # registry/section disagreement unreachable, but a
                    # referenced fact missing here must still refuse HERE —
                    # never as a generic evaluate-time KeyError.
                    raise DerivationMismatchError(
                        f"value {ov.id!r} rule {ov_rule.rule_id!r} references fact "
                        f"{fid!r} that is absent from observed_census_fact; only "
                        "EXACT strict-int observations are derivation operands — "
                        "make the value an owner_deviation instead"
                    )
                if type(observed.v) is not int:
                    raise DerivationMismatchError(
                        f"value {ov.id!r} rule {ov_rule.rule_id!r} references observed "
                        f"fact {fid!r} whose value is not a strict int "
                        f"({type(observed.v).__name__}); only EXACT strict-int "
                        "observations are derivation operands — make the value an "
                        "owner_deviation instead"
                    )
                if observed.confidence != "EXACT":
                    raise DerivationMismatchError(
                        f"value {ov.id!r} rule {ov_rule.rule_id!r} references observed "
                        f"fact {fid!r} with confidence {observed.confidence!r}; only "
                        "EXACT observations are derivation operands — make the value "
                        "an owner_deviation instead (the census must NOT be repaired "
                        "to hide the gap)"
                    )
            computed = evaluate(ov_rule, facts)
            if computed != ov.value:
                raise DerivationMismatchError(
                    f"value {ov.id!r}: owner supplied {ov.value}, rule "
                    f"{ov_rule.rule_id!r} computes {computed}"
                )
        elif not ov.deviation_record:
            raise OwnerValuesError(f"value {ov.id!r} owner_deviation has an empty deviation_record")

    # 7. hidden-default refusal: the flow threshold must be a real positive int
    flow = next((ov for ov in owner_doc.values if ov.id == FLOW_MIN_SESSION_VOLUME_ID), None)
    if flow is None or flow.value <= 0:
        raise OwnerValuesError(
            f"{FLOW_MIN_SESSION_VOLUME_ID} must be supplied by the owner as a "
            "real int > 0; a missing or zero threshold is exactly the silent "
            "default this builder exists to prevent"
        )
    flow_value = flow.value
    flow_source = _flow_source_note(flow)

    # 8. tracked-file write protection: proposals live under artifacts/ only
    artifacts_root = (_repo_root() / "artifacts").resolve()
    resolved_out_root = out_root.expanduser().resolve()
    if not resolved_out_root.is_relative_to(artifacts_root):
        raise OutputRefusedError(
            f"output root {out_root} resolves outside {artifacts_root}: the "
            "builder writes proposals under artifacts/ only"
        )

    # 9. emit under <out-root>/<census-hash[:12]>/ — re-resolved: a symlink
    # precreated at the hash dir (or at any output filename) must refuse,
    # never write through (round-3 finding 3).
    out_dir = resolved_out_root / census_hash[:12]
    # R15 review fix (2026-08-25, finding 7, R14): mkdir(parents=True) can
    # create the output root ITSELF (and deeper ancestors). Every component
    # this run creates is committed in ITS parent right here — before
    # anything is published beneath it — through the same component-wise
    # no-follow custody walk the emits ride; a refusal is the
    # OutputRefusedError family, so nothing is ever published over an
    # uncommitted hierarchy.
    absent_ancestors = _absent_output_ancestors(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_dir = _confine_output(out_dir, out_root=resolved_out_root)
    _commit_created_output_entries(absent_ancestors)

    data: dict[str, Any] = base.model_dump(mode="json")
    data["meta"]["protocol_version"] = PROPOSED_PROTOCOL_VERSION
    amendments = list(data["meta"]["amendments"])
    amendments.append(
        {
            "version": PROPOSED_PROTOCOL_VERSION,
            "date": AMENDMENT_DATE_PENDING,
            "decision": (
                f"coverage-era amendment PROPOSAL built from census "
                f"{census_hash[:12]} (dry-run: not landed, owner GO required)"
            ),
            "changes": (
                f"set liquidity_volume_flow.flow_min_session_volume = "
                f"{flow_value} ({flow_source}); final_holdout_window schema "
                "addition proposed separately (NOT_LANDED) because protocol "
                'models are extra="forbid"'
            ),
        }
    )
    data["meta"]["amendments"] = amendments
    data["option_candidate_defaults"]["liquidity_volume_flow"]["flow_min_session_volume"] = (
        flow_value
    )

    # Round-11 review fix (finding 1): ONE custody fd held across ALL FOUR
    # emits (three artifacts + packet). The pre-fix shape opened and closed a
    # fresh custody walk per emit and closed the three-artifact sweep fd
    # BEFORE the packet was built — so nothing re-verified the artifact names
    # at the moment the packet was returned. Nothing closes before the final
    # sweep at packet return.
    custody_fd = _open_output_custody(out_dir)
    try:
        proposed_path = _confine_output(
            out_dir / f"protocol-{PROPOSED_PROTOCOL_VERSION}-proposed.yaml",
            out_root=resolved_out_root,
        )
        _refuse_shared_inode(proposed_path)
        proposed_text = yaml.safe_dump(data, sort_keys=False, default_flow_style=False, width=1000)
        proposed_identity = _emit_custody_write(proposed_path, proposed_text, custody_fd=custody_fd)

        # PROOF STEP: the proposal must load through TODAY'S real loader —
        # from the RENDERED text, never a fresh read of the published path
        # (round-8 finding 1: a post-return swap of the published name used
        # to feed this proof attacker YAML through a planted symlink).
        try:
            parsed = load_protocol_bytes(proposed_text.encode("utf-8"))
        except ValueError as exc:
            raise AmendmentError(
                f"proposed protocol ({proposed_path}) does not load through the"
                f" current schema: {exc}"
            ) from exc
        parsed_flow = parsed.option_candidate_defaults.liquidity_volume_flow
        if (
            parsed.meta.protocol_version != PROPOSED_PROTOCOL_VERSION
            or parsed_flow is None
            or parsed_flow.flow_min_session_volume != flow_value
            or len(parsed.meta.amendments) != len(base.meta.amendments) + 1
        ):
            raise AmendmentError("proposed protocol round-trip lost the amendment content")

        schema_path = _confine_output(
            out_dir / "schema-addition-proposal.yaml", out_root=resolved_out_root
        )
        _refuse_shared_inode(schema_path)
        schema_text = _render_schema_addition_proposal(
            census=census, census_hash=census_hash, flow_value=flow_value
        )
        schema_identity = _emit_custody_write(schema_path, schema_text, custody_fd=custody_fd)

        diff_path = _confine_output(out_dir / "amendment-diff.md", out_root=resolved_out_root)
        _refuse_shared_inode(diff_path)
        diff_text = _render_diff(
            base=base, census_hash=census_hash, flow_value=flow_value, flow_source=flow_source
        )
        diff_identity = _emit_custody_write(diff_path, diff_text, custody_fd=custody_fd)

        # PRE-PACKET SWEEP (round-8, finding 1): the packet ATTESTS these
        # artifacts, so each is re-verified under the HELD custody fd
        # immediately before the packet is written — a swapped artifact
        # refuses while NO packet file exists on disk. Regular file at the
        # final name, the published identity, and the on-disk bytes still
        # equal to the rendered text.
        _verify_final_effect(
            artifact="protocol-0.2.1-proposed.yaml",
            path=proposed_path,
            identity=proposed_identity,
            text=proposed_text,
            custody_fd=custody_fd,
        )
        _verify_final_effect(
            artifact="schema-addition-proposal.yaml",
            path=schema_path,
            identity=schema_identity,
            text=schema_text,
            custody_fd=custody_fd,
        )
        _verify_final_effect(
            artifact="amendment-diff.md",
            path=diff_path,
            identity=diff_identity,
            text=diff_text,
            custody_fd=custody_fd,
        )

        packet = AmendmentPacket(
            schema_version=AMENDMENT_PACKET_SCHEMA_VERSION,
            base_version=base_version,
            proposed_version=PROPOSED_PROTOCOL_VERSION,
            census_content_sha256=census_hash,
            protocol_hash_base=protocol_hash(base),
            flow_min_session_volume=flow_value,
            owner_values_schema_version=OWNER_VALUES_SCHEMA_VERSION,
            inputs=AmendmentInputs(
                census_file_sha256=hashlib.sha256(census_bytes).hexdigest(),
                owner_values_file_sha256=hashlib.sha256(owner_bytes).hexdigest(),
                rules_file_sha256=hashlib.sha256(rules_bytes).hexdigest(),
                protocol_file_sha256=hashlib.sha256(protocol_bytes).hexdigest(),
                capture_manifest_file_sha256=manifest_sha256,
            ),
            # round-8 finding 1: `emitted` hashes the RENDERED bytes — the same
            # text handed to _write_exclusive and verified by the sweeps —
            # never a fresh read of the published path.
            emitted=tuple(
                EmittedArtifact(name=name, sha256=hashlib.sha256(text.encode("utf-8")).hexdigest())
                for name, text in sorted(
                    (
                        (proposed_path.name, proposed_text),
                        (schema_path.name, schema_text),
                        (diff_path.name, diff_text),
                    ),
                    key=lambda pair: pair[0],
                )
            ),
        )
        packet_path = _confine_output(out_dir / "amendment-packet.json", out_root=resolved_out_root)
        _refuse_shared_inode(packet_path)
        packet_text = (
            json.dumps(json.loads(packet.model_dump_json()), indent=2, sort_keys=True) + "\n"
        )
        packet_identity = _emit_custody_write(packet_path, packet_text, custody_fd=custody_fd)

        # R15 review fix (2026-08-25, finding 7, R14) + R16 review fix
        # (finding 2, R14): the four publishes are RENAMES — directory-entry
        # swaps — and the only fsync in this module used to cover the FILE
        # temps. Round-15 added the content directory and the output root;
        # round-16 completes the class: before the builder attests (the
        # final-effect sweep below, then the packet return) the FULL output
        # chain is committed durably, regardless of whether THIS run or a
        # prior crashed run created each component:
        #   * the content directory — the held custody fd — committing the
        #     rename set;
        #   * every component of the output root's own chain — outermost
        #     ancestor down through the output root itself — committed in
        #     ITS parent by the durable no-follow walk
        #     ``_commit_output_chain`` (the same contract as
        #     ``runstate.custody.open_directory(durable=True)``: an output
        #     root an interrupted earlier run created without committing is
        #     repaired by the retry, never attested over uncommitted);
        #   * the output root's own entries — the walk's final fsync —
        #     committing the content directory's entry in it.
        # A refusal is the OutputRefusedError family: the builder never
        # returns a packet over an uncommitted directory chain.
        try:
            os.fsync(custody_fd)
            _commit_output_chain(resolved_out_root)
        except OSError as exc:
            raise OutputRefusedError(
                f"the output hierarchy under {resolved_out_root} could not be "
                f"durably committed ({exc}) — never attest a packet over an "
                "uncommitted directory chain"
            ) from None

        # FINAL-EFFECT SWEEP (round-11, finding 1): the packet is written
        # LAST, and the sweep AT PACKET RETURN re-verifies ALL FOUR names —
        # the three artifacts plus the packet itself — through a FRESH
        # component-wise no-follow walk of the resolved out dir, so a swap in
        # any post-publish window (including between the pre-packet sweep and
        # this one) refuses before the builder attests. Nothing closes before
        # this sweep: the held fd spans every emit and outlives the packet's
        # own write.
        final_sweep_fd = _open_output_custody(out_dir)
        try:
            # Round-11 review fix (finding 4): output-root CONFINEMENT at the
            # final effect. The held fd proves the artifacts' directory, but
            # nothing proved the PATH still led to it: an attacker relocating
            # the digest dir outside artifacts/ (recreating a decoy at the
            # path) could stage the artifacts for the sweep and reclaim them
            # after it — the sweep rode dir fds while the outputs lived
            # outside the root. The re-walked fd's fstat identity must EQUAL
            # the held fd's identity: the directory this builder published
            # into must still BE the directory the confined path resolves to.
            held_dir = os.fstat(custody_fd)
            walked_dir = os.fstat(final_sweep_fd)
            if (walked_dir.st_dev, walked_dir.st_ino) != (held_dir.st_dev, held_dir.st_ino):
                raise OutputRefusedError(
                    f"the output directory {out_dir} no longer holds the"
                    f" artifacts this builder published (held dev"
                    f" {held_dir.st_dev} ino {held_dir.st_ino}, at the path"
                    f" now dev {walked_dir.st_dev} ino {walked_dir.st_ino}):"
                    " the digest directory was relocated out of the output"
                    " root — refusing to attest artifacts published outside"
                    " artifacts/"
                )
            _verify_final_effect(
                artifact="protocol-0.2.1-proposed.yaml",
                path=proposed_path,
                identity=proposed_identity,
                text=proposed_text,
                custody_fd=final_sweep_fd,
            )
            _verify_final_effect(
                artifact="schema-addition-proposal.yaml",
                path=schema_path,
                identity=schema_identity,
                text=schema_text,
                custody_fd=final_sweep_fd,
            )
            _verify_final_effect(
                artifact="amendment-diff.md",
                path=diff_path,
                identity=diff_identity,
                text=diff_text,
                custody_fd=final_sweep_fd,
            )
            _verify_final_effect(
                artifact="amendment-packet.json",
                path=packet_path,
                identity=packet_identity,
                text=packet_text,
                custody_fd=final_sweep_fd,
            )
        finally:
            os.close(final_sweep_fd)
        return packet
    finally:
        os.close(custody_fd)
