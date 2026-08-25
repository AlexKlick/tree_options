"""Authority ledger: chain verify, tamper, torn tail, /tmp refusal, kinds."""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from tree_options.seal import ledger as L
from tree_options.seal.errors import LedgerCorruptError, LedgerRootRefusedError
from tree_options.seal.identity import SealedIdentity, content_identity, sealed_run_id

REPO_ROOT = Path(__file__).resolve().parents[2]
T0 = 1_800_000_000


def _identity(**overrides: str) -> SealedIdentity:
    fields = dict(
        code_sha="a" * 40,
        protocol_hash="b" * 64,
        lane1_manifest_sha256="c" * 64,
        lane2_manifest_sha256="d" * 64,
        calendar_decision_artifact_sha256="e" * 64,
        criteria_artifact_sha256="f" * 64,
        verified_packet_sha256="0" * 64,
    )
    fields.update(overrides)
    return SealedIdentity(**fields)


# CONFLICT RESOLUTION (host rule): pytest's tmp_path lives under /tmp on this
# host, and validate_ledger_root refuses any root whose RESOLVED path is under
# /tmp — authority may never live where a reboot wipes it. Valid-ledger tests
# therefore use a scratch root under the REPO's gitignored artifacts/
# directory (removed in teardown); the refusal test below uses an explicit
# /tmp path instead of tmp_path.
@pytest.fixture()
def ledger_root() -> Iterator[Path]:
    root = REPO_ROOT / "artifacts" / "g4-seal-tests" / uuid.uuid4().hex
    root.mkdir(parents=True, exist_ok=True)
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)
        # The dual-tree anchor (round-11 finding 1, R13) lives in the
        # runstate store ADJACENT to the ledger root — a SIBLING this
        # fixture must also clean, or anchors accumulate forever under
        # artifacts/g4-seal-tests/runstate/.
        anchor = L.runstate_anchor_path(root)
        with contextlib.suppress(OSError):
            anchor.unlink()
            anchor.parent.rmdir()  # seal-ledger-anchor/
            anchor.parent.parent.rmdir()  # runstate/ (only when now empty)


def test_genesis_chains_to_zeros_and_domain_separates(ledger_root):
    record = L.append_approval(ledger_root, _identity(), reason="owner approved", at_epoch=T0)
    view = L.read_ledger(ledger_root)
    assert len(view.records) == 1
    assert view.records[0].prev_record_sha256 == L.GENESIS_PREV == "0" * 64
    assert view.records[0].record_sha256 == record.record_sha256
    assert view.tail_hash == record.record_sha256
    assert not view.tail_damaged
    # The chain hash is over the ledger's OWN domain: it differs from the
    # sealed-run id domain even for the same identity payload.
    assert record.record_sha256 != sealed_run_id(_identity())


def test_multi_record_chain_verifies_with_all_three_kinds(ledger_root):
    identity = _identity()
    approval = L.append_approval(ledger_root, identity, reason="approved", at_epoch=T0)
    consumption = L.append_consumption(
        ledger_root, identity, reason="G4 sealed event", at_epoch=T0 + 1
    )
    note = L.append_reconciliation_note(
        ledger_root, identity, reason="operator reviewed the tail damage", at_epoch=T0 + 2
    )
    view = L.read_ledger(ledger_root)
    assert [r.kind for r in view.records] == ["APPROVAL", "CONSUMPTION", "RECONCILIATION_NOTE"]
    assert view.records[0].record_sha256 == approval.record_sha256
    assert view.records[1].prev_record_sha256 == approval.record_sha256
    assert view.records[2].prev_record_sha256 == consumption.record_sha256
    assert view.tail_hash == note.record_sha256
    assert not view.tail_damaged


def test_records_carry_recomputed_identity_ids(ledger_root):
    identity = _identity()
    record = L.append_consumption(ledger_root, identity, reason="seal", at_epoch=T0)
    assert record.sealed_run_id == sealed_run_id(identity)
    assert record.content_identity == content_identity(identity)
    assert record.identity == identity


def test_midfile_reason_tamper_detected(ledger_root):
    L.append_approval(ledger_root, _identity(), reason="one", at_epoch=T0)
    L.append_consumption(ledger_root, _identity(), reason="two", at_epoch=T0 + 1)
    L.append_reconciliation_note(ledger_root, _identity(), reason="three", at_epoch=T0 + 2)
    path = ledger_root / L.LEDGER_FILENAME
    lines = path.read_text().splitlines()
    tampered = json.loads(lines[1])
    tampered["reason"] = "rewritten history"
    lines[1] = json.dumps(tampered, sort_keys=True, separators=(",", ":"))
    path.write_text("\n".join(lines) + "\n")
    with pytest.raises(LedgerCorruptError):
        L.read_ledger(ledger_root)


def test_reordering_detected(ledger_root):
    L.append_approval(ledger_root, _identity(), reason="one", at_epoch=T0)
    L.append_consumption(ledger_root, _identity(), reason="two", at_epoch=T0 + 1)
    path = ledger_root / L.LEDGER_FILENAME
    lines = path.read_text().splitlines()
    lines[0], lines[1] = lines[1], lines[0]
    path.write_text("\n".join(lines) + "\n")
    with pytest.raises(LedgerCorruptError):
        L.read_ledger(ledger_root)


def test_truncated_final_line_tolerated_and_flagged(ledger_root):
    L.append_approval(ledger_root, _identity(), reason="one", at_epoch=T0)
    L.append_consumption(ledger_root, _identity(), reason="two", at_epoch=T0 + 1)
    path = ledger_root / L.LEDGER_FILENAME
    path.write_text(path.read_text() + '{"kind": "CONSUMPTIO')  # crash mid-append
    view = L.read_ledger(ledger_root)
    assert view.tail_damaged
    assert len(view.records) == 2


def test_tampered_final_line_is_tail_damaged_not_corrupt(ledger_root):
    L.append_approval(ledger_root, _identity(), reason="one", at_epoch=T0)
    L.append_consumption(ledger_root, _identity(), reason="two", at_epoch=T0 + 1)
    path = ledger_root / L.LEDGER_FILENAME
    lines = path.read_text().splitlines()
    last = json.loads(lines[-1])
    last["reason"] = "never happened"
    lines[-1] = json.dumps(last, sort_keys=True, separators=(",", ":"))
    path.write_text("\n".join(lines) + "\n")
    view = L.read_ledger(ledger_root)
    assert view.tail_damaged
    assert len(view.records) == 1


def test_append_past_torn_tail_refused(ledger_root):
    L.append_approval(ledger_root, _identity(), reason="one", at_epoch=T0)
    path = ledger_root / L.LEDGER_FILENAME
    path.write_text(path.read_text() + '{"kind": "CONSUMPTIO')
    with pytest.raises(LedgerCorruptError):
        L.append_consumption(ledger_root, _identity(), reason="next", at_epoch=T0 + 1)


def test_append_with_wrong_prev_refused(ledger_root):
    L.append_approval(ledger_root, _identity(), reason="one", at_epoch=T0)
    stale = L.LedgerRecord(
        kind=L.KIND_CONSUMPTION,
        identity=_identity(),
        sealed_run_id=sealed_run_id(_identity()),
        content_identity=content_identity(_identity()),
        reason="built against a stale tail",
        at_epoch=T0 + 1,
        prev_record_sha256=L.GENESIS_PREV,  # not the current tail
    )
    with pytest.raises(LedgerCorruptError):
        L.append_record(ledger_root, stale)


def test_missing_ledger_is_an_empty_view_not_corruption(ledger_root):
    view = L.read_ledger(ledger_root / "absent")
    assert view.records == ()
    assert view.tail_hash == L.GENESIS_PREV
    assert not view.tail_damaged


def test_default_root_constant_pinned():
    # The production default is a RELATIVE constant against the checkout;
    # tests always inject their own root, and this pin keeps the default from
    # drifting silently into something durable-hostile.
    assert L.DEFAULT_G4_LEDGER_ROOT == Path("artifacts/g4-authority")


def test_tmp_root_refused():
    # Explicit /tmp paths (NOT tmp_path): read and append both refuse, and a
    # never-created path still resolves under /tmp, so the refusal is a pure
    # path rule — nothing needs to exist for authority to be refused there.
    under_tmp = Path("/tmp") / f"g4-seal-refused-{uuid.uuid4().hex}"
    with pytest.raises(LedgerRootRefusedError):
        L.read_ledger(under_tmp)
    with pytest.raises(LedgerRootRefusedError):
        L.append_approval(under_tmp, _identity(), reason="x", at_epoch=T0)
    with pytest.raises(LedgerRootRefusedError):
        L.read_ledger(Path("/tmp"))


def test_tmp_prefix_sibling_is_not_tmp():
    # The rule is a COMPONENT prefix, not a string prefix: /tmp-authority is
    # a sibling of /tmp, not under it.
    assert L.validate_ledger_root(Path("/tmp-authority-ok")) == Path("/tmp-authority-ok").resolve()


def test_symlink_into_tmp_refused_after_resolve(ledger_root):
    target = Path("/tmp") / f"g4-seal-refused-{uuid.uuid4().hex}"
    link = ledger_root / "into-tmp"
    link.symlink_to(target)
    with pytest.raises(LedgerRootRefusedError):
        L.read_ledger(link)


def test_concurrent_shaped_append_extends_verified_tail(ledger_root):
    # A fresh open + append continues from the VERIFIED tail (the flock spans
    # read-verify-append).
    L.append_approval(ledger_root, _identity(), reason="one", at_epoch=T0)
    view = L.read_ledger(ledger_root)
    record = L.LedgerRecord(
        kind=L.KIND_RECONCILIATION_NOTE,
        identity=_identity(),
        sealed_run_id=sealed_run_id(_identity()),
        content_identity=content_identity(_identity()),
        reason="continued later",
        at_epoch=T0 + 5,
        prev_record_sha256=view.tail_hash,
    )
    digest = L.append_record(ledger_root, record)
    view2 = L.read_ledger(ledger_root)
    assert view2.records[-1].record_sha256 == digest
    assert view2.records[-1].prev_record_sha256 == view.tail_hash
    assert not view2.tail_damaged


# ---- round-5 (finding 3): a symlinked ledger NAME is never created or followed ------
#
# Round-5 review fix (2026-08-24): Path.exists() is False for a DANGLING
# symlink at ledger.jsonl, so read_ledger treated the ledger as absent, and
# the append's os.open(O_RDWR|O_CREAT) FOLLOWED the link — creating seal
# authority under /tmp (or wherever the link points). Both paths now open
# with O_NOFOLLOW and refuse on ELOOP.


def test_dangling_symlink_ledger_name_refused_and_never_created(ledger_root) -> None:
    """A dangling symlink at <valid durable root>/ledger.jsonl: the read must
    refuse as corruption (naming the symlink), the append must refuse, and
    the /tmp target must NEVER be created."""
    target = Path("/tmp") / f"g4-seal-dangling-{uuid.uuid4().hex}"
    link = ledger_root / L.LEDGER_FILENAME
    link.symlink_to(target)  # dangling: target does not exist
    try:
        with pytest.raises(LedgerCorruptError, match="symlink") as read_exc:
            L.read_ledger(ledger_root)
        assert str(link) in str(read_exc.value), "the read refusal names the link"
        with pytest.raises(LedgerCorruptError, match="symlink") as append_exc:
            L.append_approval(ledger_root, _identity(), reason="r", at_epoch=T0)
        assert str(link) in str(append_exc.value), "the append refusal names the link"
        assert not target.exists(), "authority was never created through the link"
    finally:
        # the RED run of this test follows the link and creates the target;
        # never leave it behind (and never leave a dangling symlink under
        # artifacts/ — the harness copytree crashes on those).
        with contextlib.suppress(FileNotFoundError):
            link.unlink()
        with contextlib.suppress(FileNotFoundError):
            target.unlink()


def test_symlinked_ledger_name_to_a_real_file_also_refused(ledger_root, tmp_path) -> None:
    """The rule is about the NAME, not the target: a symlink to an existing
    ledger file elsewhere is equally refused on both paths."""
    real_root = ledger_root / "real"
    real_root.mkdir()
    L.append_approval(real_root, _identity(), reason="seed", at_epoch=T0)
    link = ledger_root / L.LEDGER_FILENAME
    link.symlink_to(real_root / L.LEDGER_FILENAME)
    try:
        with pytest.raises(LedgerCorruptError, match="symlink"):
            L.read_ledger(ledger_root)
        with pytest.raises(LedgerCorruptError, match="symlink"):
            L.append_approval(ledger_root, _identity(), reason="r", at_epoch=T0)
        # the linked ledger is untouched
        assert len(L.read_ledger(real_root).records) == 1
    finally:
        with contextlib.suppress(FileNotFoundError):
            link.unlink()


# ---- round-6 (finding 3): the ledger ROOT itself must be a REAL directory -----------
#
# Round-6 review fix (2026-08-24): the final-name O_NOFOLLOW (round-5) guards
# only `ledger.jsonl`. Between validate_ledger_root() and the mkdir/open, an
# attacker could create the (previously nonexistent) ALLOWED root as a
# directory symlink into /tmp: mkdir(exist_ok=True) accepts a directory
# symlink, the ledger name inside it is a regular file, so O_NOFOLLOW on that
# name never fired — authority landed under the link's target.


def test_root_symlink_swap_refused_and_authority_stays_out_of_tmp(
    ledger_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reviewer's interleaving: the (absent, allowed) root is created as a
    dir symlink to /tmp BETWEEN validation and the open (simulated by wrapping
    validate_ledger_root — the wrapper plants the link once the check has
    passed). Pre-fix the append succeeds and creates the ledger under /tmp;
    post-fix BOTH the append and the read refuse (LedgerCorruptError naming
    the root) and nothing is ever created under the /tmp target."""
    target = Path("/tmp") / f"g4-seal-rootswap-{uuid.uuid4().hex}"
    target.mkdir()
    root = ledger_root / "fresh"  # allowed (under repo artifacts/), ABSENT
    assert not root.exists()
    real_validate = L.validate_ledger_root
    armed = {"next": root}

    def validate_then_swap_root(path):
        resolved = real_validate(path)  # the check passed: the window opens HERE
        if Path(path) == armed["next"]:
            armed["next"].symlink_to(target)
            armed["next"] = None  # one plant per phase
        return resolved

    identity = _identity()
    # a genesis-valid record: the direct append primitive (one validate, one
    # open) is the exact race surface; append_approval/append_consumption
    # ride it through _append_kind.
    record = L.LedgerRecord(
        kind=L.KIND_APPROVAL,
        identity=identity,
        sealed_run_id=sealed_run_id(identity),
        content_identity=content_identity(identity),
        reason="owner approved",
        at_epoch=T0,
        prev_record_sha256=L.GENESIS_PREV,
    )
    monkeypatch.setattr(L, "validate_ledger_root", validate_then_swap_root)
    try:
        with pytest.raises(LedgerCorruptError, match="symlink") as append_exc:
            L.append_record(root, record)
        assert str(root) in str(append_exc.value), "the append refusal names the root"
        # The same race on the READ path, its own plant: pre-fix the read
        # follows the swapped root silently (an attacker ledger there would
        # be returned as authority); post-fix the custody open refuses.
        read_root = ledger_root / "fresh-read"  # allowed, ABSENT
        armed["next"] = read_root
        with pytest.raises(LedgerCorruptError, match="symlink") as read_exc:
            L.read_ledger(read_root)
        assert str(read_root) in str(read_exc.value), "the read refusal names the root"
        assert list(target.iterdir()) == [], "authority never landed under /tmp"
    finally:
        # the RED run of this test creates the ledger under the /tmp target;
        # never leave it behind, and never leave a symlink under artifacts/.
        for planted in (root, ledger_root / "fresh-read"):
            with contextlib.suppress(OSError):
                planted.unlink()
        shutil.rmtree(target, ignore_errors=True)


# ---- round-7 (finding 2): custody must cover EVERY path component, not the last ------
#
# Round-7 review fix (2026-08-24): os.open(root, O_NOFOLLOW|O_DIRECTORY)
# guards only the FINAL path component. Renaming an INTERMEDIATE ancestor
# (e.g. the repo's artifacts/) and planting `artifacts -> /tmp/attack` — with
# a real g4-authority dir inside — leaves the final component a REAL
# directory: the single custody open FOLLOWS the intermediate symlink, custody
# lands on /tmp, and the append writes /tmp/attack/g4-authority/ledger.jsonl
# and returns success. The root is now taken into custody COMPONENT-WISE: /
# is opened once, then every component of the resolved root path is opened
# O_RDONLY|O_DIRECTORY|O_NOFOLLOW relative to the previous component's fd —
# ELOOP or ENOTDIR at ANY component refuses (LedgerCorruptError naming the
# offending component).


def test_intermediate_component_symlink_swap_refused_component_wise(
    ledger_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reviewer's interleaving: the PARENT of the ledger root is renamed
    away and replaced by a symlink to a /tmp attack dir that already contains
    a real `g4-authority`, armed at the custody open (validate has already
    passed there). Pre-fix the single custody open FOLLOWS the intermediate
    symlink: the append succeeds and the ledger lands under /tmp. Post-fix the
    component-wise walk refuses naming the swapped component, and no ledger
    file exists anywhere under the /tmp target."""
    target = tmp_path / f"g4-seal-f2-{uuid.uuid4().hex}"
    (target / "g4-authority").mkdir(parents=True)  # a real root dir inside
    parent = ledger_root
    root = parent / "g4-authority"
    real_open = os.open
    armed = {"done": False}

    def open_swapping_parent(path: object, flags: int, *args: object, **kwargs: object) -> int:
        # the racing call: the custody open. Pre-fix it is the ONE
        # os.open(root, O_NOFOLLOW|O_DIRECTORY); post-fix the first component
        # open with the same flag signature arms the attack — the walk has not
        # reached the swapped ancestor yet either way.
        if not armed["done"] and (flags & os.O_DIRECTORY) and (flags & os.O_NOFOLLOW):
            armed["done"] = True  # validate has passed: the window opens HERE
            held = parent.parent / (parent.name + ".held")
            os.rename(parent, held)  # the real ancestor moves away
            parent.symlink_to(target, target_is_directory=True)
        return real_open(path, flags, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "open", open_swapping_parent)
    identity = _identity()
    record = L.LedgerRecord(
        kind=L.KIND_APPROVAL,
        identity=identity,
        sealed_run_id=sealed_run_id(identity),
        content_identity=content_identity(identity),
        reason="owner approved",
        at_epoch=T0,
        prev_record_sha256=L.GENESIS_PREV,
    )
    try:
        with pytest.raises(LedgerCorruptError) as append_exc:
            L.append_record(root, record)
        message = str(append_exc.value)
        assert parent.name in message, "the refusal names the swapped component"
        assert str(root) in message, "the refusal names the ledger root"
        assert not list(target.rglob("ledger.jsonl")), (
            "no ledger file may exist anywhere under the /tmp target"
        )
    finally:
        # the RED run creates the ledger under the /tmp target; never leave it
        # behind, never leave the planted symlink under artifacts/, and never
        # leave the renamed .held ancestor either.
        with contextlib.suppress(OSError):
            parent.unlink()
        shutil.rmtree(target, ignore_errors=True)
        shutil.rmtree(parent.parent / (parent.name + ".held"), ignore_errors=True)


# ---- round-8 (finding 3): the append must verify the NAME still maps to the locked inode
#
# Round-8 review fix (2026-08-24): append_record opens ledger.jsonl
# O_NOFOLLOW at the final name, flocks, appends, fsyncs — but never verified
# that the NAME still maps to the locked inode. During the first append an
# attacker renames ledger.jsonl to .held and installs a byte-copy clone
# (approval-only) at the name: the first execution appends its consumption
# to .held, returns SUCCESS, and invokes the runner; a second execution
# reads the clone, appends its own consumption, and invokes again — the
# one-shot is broken. After the append + fsync, still holding the flock and
# the custody root fd, os.stat(LEDGER_FILENAME, dir_fd=root_fd,
# follow_symlinks=False) must be a regular file whose (st_dev, st_ino)
# equals the locked fd's fstat identity; any divergence is
# LedgerCorruptError naming both identities — the refusal is RECONCILIATION,
# never success.


def test_final_name_clone_swap_during_append_refused(ledger_root, monkeypatch: pytest.MonkeyPatch):
    """The racing point is the append's os.write: the wrapper renames the
    ledger to .held (the locked fd follows the inode) and installs a
    byte-copy clone (approval-only) at the name, then performs the real
    write. Pre-fix the append returns SUCCESS — the consumption lands under
    .held while the authority name holds the clone, and a second execution
    on the clone would consume again; post-fix the name check refuses
    naming both identities, the authority name still holds ONLY the
    approval, and the consumption is stranded as reconciliation evidence
    under .held."""
    identity = _identity()
    L.append_approval(ledger_root, identity, reason="owner approved", at_epoch=T0)
    ledger_path = ledger_root / L.LEDGER_FILENAME
    before = ledger_path.read_bytes()  # the APPROVAL line only
    real_write = os.write
    armed = {"done": False}

    def write_cloning_the_name(fd: int, data: bytes) -> int:
        if not armed["done"]:
            armed["done"] = True  # the window: between the open and the write
            held = ledger_root / (L.LEDGER_FILENAME + ".held")
            os.rename(ledger_path, held)  # the locked fd keeps the inode
            ledger_path.write_bytes(before)  # a byte-copy CLONE at the name
        return real_write(fd, data)

    monkeypatch.setattr(os, "write", write_cloning_the_name)
    try:
        with pytest.raises(LedgerCorruptError) as exc_info:
            L.append_consumption(ledger_root, identity, reason="G4 sealed event", at_epoch=T0 + 1)
        message = str(exc_info.value)
        assert L.LEDGER_FILENAME in message, "the refusal names the ledger"
        assert "RECONCIL" in message, "the refusal says this is reconciliation, never success"
        # the authority NAME still holds ONLY the approval: the consumption
        # never landed at the name the next reader opens, so the one-shot
        # was not spent at the authority surface
        assert ledger_path.read_bytes() == before, (
            "no consumption may land at the cloned authority name"
        )
        # the orphaned consumption lives under .held — reconciliation
        # evidence of the incident, never a second spend at the name
        held = ledger_root / (L.LEDGER_FILENAME + ".held")
        assert b'"CONSUMPTION"' in held.read_bytes(), (
            "the refused append's record is stranded under the renamed file"
        )
    finally:
        # the RED run strands the consumption under .held: never leave it
        with contextlib.suppress(OSError):
            (ledger_root / (L.LEDGER_FILENAME + ".held")).unlink()


# ---- round-11 (finding 3): the durable name→inode binding closes the SUCCESSOR window --
#
# Round-11 review fix (2026-08-25): the round-8 name check closes only the
# in-process window — it runs while the append still holds the flock. A swap
# landing AFTER that check but BEFORE the call returns (the LOCK_UN, the fd
# close, the custody-root dir fsync) was never seen by anyone: the append
# returned SUCCESS, and the byte-copy clone installed at the authority name
# was a fully valid approval-only ledger that a SECOND process happily
# consumed — split authority (the consumption under the renamed .held plus a
# second consumption on the clone). Each ledger now carries a DURABLE
# name→inode binding: at ledger creation the file's (st_dev, st_ino) is
# recorded in a companion identity record custody-written beside the ledger,
# and EVERY open verifies the name still maps to that bound inode, refusing
# as corruption/reconciliation (never success) on divergence. A clone at the
# canonical name has the wrong inode, so it is refused at the next open —
# the clone can never be consumed.


def test_clone_swap_after_the_name_check_is_refused_at_the_next_open(
    ledger_root, monkeypatch: pytest.MonkeyPatch
):
    """The racing point is the append's FINAL custody-root fsync, which runs
    AFTER the round-8 name check has already passed: the wrapper renames the
    ledger to .held and installs an approval-only byte clone at the name
    there. The racing append may still acknowledge (the check already
    passed); the CONTRACT is the successor — a second execution against the
    clone must REFUSE on the durable binding, and the clone must never gain
    a consumption."""
    identity = _identity()
    L.append_approval(ledger_root, identity, reason="owner approved", at_epoch=T0)
    ledger_path = ledger_root / L.LEDGER_FILENAME
    before = ledger_path.read_bytes()  # the APPROVAL line only
    real_fsync = os.fsync
    calls = {"n": 0}

    def fsync_swapping_after_the_name_check(fd: int) -> None:
        calls["n"] += 1
        if calls["n"] == 2:  # fsync #1 is the ledger fd; #2 is the root dir
            held = ledger_root / (L.LEDGER_FILENAME + ".held")
            os.rename(ledger_path, held)  # the locked fd keeps the inode
            ledger_path.write_bytes(before)  # a byte-copy CLONE at the name
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fsync_swapping_after_the_name_check)
    try:
        # The racing append already passed the name check, so it may still
        # return success — the consumption lands under .held (reconciliation
        # evidence, exactly like round 8). What must never happen again is a
        # SECOND process consuming the clone at the authority name.
        L.append_consumption(ledger_root, identity, reason="G4 sealed event", at_epoch=T0 + 1)
        with pytest.raises(LedgerCorruptError, match="durable binding"):
            L.append_consumption(
                ledger_root, identity, reason="second execution on the clone", at_epoch=T0 + 2
            )
        assert ledger_path.read_bytes() == before, (
            "the clone at the authority name must never gain a consumption"
        )
    finally:
        with contextlib.suppress(OSError):
            (ledger_root / (L.LEDGER_FILENAME + ".held")).unlink()


# ---- round-11 (finding 1, R13): the DUAL-TREE anchor — the companion alone
# is co-replaceable OFFLINE --------------------------------------------------------
#
# Round-11 review fix (2026-08-25, R13 wave): the durable name->inode binding
# verifies the ledger name against whatever companion record CURRENTLY
# occupies the adjacent path — so an OFFLINE attacker (no concurrency at all:
# the swap lands between invocations) replaces BOTH files with a
# self-consistent pair: a regular clone carrying the approval-only prefix
# bytes plus a replacement ledger.jsonl.identity.json naming the clone's
# dev/inode. The next open verified the clone against its FORGED companion
# and re-spent the approval — the companion added no security over the file
# it guards, because it lives beside it. The ledger identity is now ALSO
# anchored in a SECOND tree the artifacts/ attacker must separately forge —
# the runstate store: at the first ledger open/creation an anchor record is
# custody-written under <ledger-root-parent>/runstate/seal-ledger-anchor/
# recording the ledger root path, the ledger file's (st_dev, st_ino), and
# the companion digest; every subsequent open verifies BOTH the
# beside-the-file companion AND the runstate anchor. Divergence — or a
# MISSING anchor for a non-empty ledger — is a corruption-class refusal.


def test_offline_co_replacement_of_ledger_and_companion_refused_at_next_open(
    ledger_root,
) -> None:
    """The round-11 attack: replace ledger.jsonl AND its companion with a
    self-consistent approval-only pair (a regular clone at a NEW inode plus a
    companion naming that inode). The forged pair satisfies the beside-the-
    file companion check exactly; the runstate anchor still names the REAL
    ledger's identity, so the next open — read AND append — refuses and the
    approval is never re-spent."""
    identity = _identity()
    L.append_approval(ledger_root, identity, reason="owner approved", at_epoch=T0)
    L.append_consumption(ledger_root, identity, reason="G4 sealed event", at_epoch=T0 + 1)
    ledger_path = ledger_root / L.LEDGER_FILENAME
    approval_only = (ledger_path.read_text().splitlines(keepends=True)[0]).encode("utf-8")
    real = ledger_path.with_name(L.LEDGER_FILENAME + ".real")
    os.rename(ledger_path, real)  # keep the real ledger aside for teardown
    ledger_path.write_bytes(approval_only)  # the CLONE: a new regular inode
    clone_stat = os.stat(ledger_path)
    # the replacement companion names the CLONE — the pair is self-consistent
    (ledger_root / "ledger.jsonl.identity.json").write_text(
        json.dumps(
            {
                "format": 1,
                "name": L.LEDGER_FILENAME,
                "st_dev": clone_stat.st_dev,
                "st_ino": clone_stat.st_ino,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    try:
        with pytest.raises(LedgerCorruptError, match="runstate anchor"):
            L.read_ledger(ledger_root)
        with pytest.raises(LedgerCorruptError, match="runstate anchor"):
            L.append_consumption(
                ledger_root, identity, reason="second execution on the clone", at_epoch=T0 + 2
            )
        assert ledger_path.read_bytes() == approval_only, (
            "the co-replaced pair must never gain a consumption — the approval is never re-spent"
        )
    finally:
        with contextlib.suppress(OSError):
            real.unlink()


def test_runstate_anchor_is_written_at_creation_and_names_the_real_ledger(ledger_root) -> None:
    """The anchor lands in the SECOND tree (the runstate store), is keyed by
    the resolved ledger root, and pins the real ledger file's (st_dev,
    st_ino) plus the companion digest."""
    L.append_approval(ledger_root, _identity(), reason="owner approved", at_epoch=T0)
    anchor = L.runstate_anchor_path(ledger_root)
    assert anchor.is_file(), "the creation append custody-writes the anchor"
    record = json.loads(anchor.read_text(encoding="utf-8"))
    ledger_stat = os.stat(ledger_root / L.LEDGER_FILENAME)
    companion = ledger_root / "ledger.jsonl.identity.json"
    assert (record["st_dev"], record["st_ino"]) == (ledger_stat.st_dev, ledger_stat.st_ino)
    assert record["ledger_root"] == str(ledger_root.resolve())
    assert record["ledger_name"] == L.LEDGER_FILENAME
    assert record["companion_sha256"] == L.sha256_hex(companion.read_bytes())
    # the documented mapping: the default production ledger root
    # (artifacts/g4-authority) anchors in the runstate STORE tree
    # (artifacts/runstate), a sibling the g4-authority attacker must
    # separately forge.
    default_anchor = L.runstate_anchor_path(L.DEFAULT_G4_LEDGER_ROOT)
    assert default_anchor.parent.parent == (REPO_ROOT / "artifacts" / "runstate").resolve()
    # a second append does NOT rewrite the anchor (it is exclusive-by-name):
    before = anchor.read_bytes()
    L.append_consumption(ledger_root, _identity(), reason="seal", at_epoch=T0 + 1)
    assert anchor.read_bytes() == before


def test_nonempty_ledger_without_a_runstate_anchor_is_reconciliation(ledger_root) -> None:
    """A non-empty ledger with NO runstate anchor (a pre-anchor-era ledger, or
    an attacker who deleted the second tree's record) is never read as
    authority and never appended — corruption class, never a silent re-bind."""
    L.append_approval(ledger_root, _identity(), reason="owner approved", at_epoch=T0)
    L.runstate_anchor_path(ledger_root).unlink()
    with pytest.raises(LedgerCorruptError, match="runstate anchor"):
        L.read_ledger(ledger_root)
    with pytest.raises(LedgerCorruptError, match="runstate anchor"):
        L.append_consumption(ledger_root, _identity(), reason="seal", at_epoch=T0 + 1)


def test_absent_ledger_name_with_a_surviving_anchor_is_reconciliation(ledger_root) -> None:
    """Deleting BOTH the ledger and its companion between invocations leaves
    the runstate anchor naming created authority: the total disappearance of
    a bound ledger is reconciliation, never a silent empty view."""
    L.append_approval(ledger_root, _identity(), reason="owner approved", at_epoch=T0)
    os.unlink(ledger_root / L.LEDGER_FILENAME)
    os.unlink(ledger_root / "ledger.jsonl.identity.json")
    with pytest.raises(LedgerCorruptError, match="runstate anchor"):
        L.read_ledger(ledger_root)


# ---- round-11 (finding 5): a short append write is never acknowledged ------------------
#
# Round-11 review fix (2026-08-25): the append did ONE os.write and ignored
# its return count. A short positive count left a torn prefix on disk while
# fsync + the name check still passed and the call returned SUCCESS — an
# acknowledged consumption that is not durable at the tail. The append now
# routes through custody.write_all (the looped write the journal already
# used), the only write path for authority records: every byte lands, or
# the write raises — there is no third outcome.


def test_short_append_write_is_completed_never_acknowledged_torn(
    ledger_root, monkeypatch: pytest.MonkeyPatch
):
    """A writer whose os.write accepts at most 5 bytes per call: pre-fix the
    single unchecked write left a 5-byte torn prefix and STILL returned
    success; post-fix the looped write completes the full line before the
    append can acknowledge, so the replayed ledger holds the consumption
    with no damaged tail."""
    identity = _identity()
    L.append_approval(ledger_root, identity, reason="owner approved", at_epoch=T0)
    real_write = os.write

    def chunked_write(fd: int, data) -> int:
        view = memoryview(data)
        return real_write(fd, view[:5])  # a truthful, always-short writer

    monkeypatch.setattr(os, "write", chunked_write)
    L.append_consumption(ledger_root, identity, reason="G4 sealed event", at_epoch=T0 + 1)
    view = L.read_ledger(ledger_root)
    assert [record.kind for record in view.records] == ["APPROVAL", "CONSUMPTION"], (
        "an acknowledged append must be durable at the tail, never a torn prefix"
    )
    assert not view.tail_damaged


def test_persistently_short_append_write_refuses_never_succeeds(
    ledger_root, monkeypatch: pytest.MonkeyPatch
):
    """A writer that gives up (returns 0) after a few bytes: the looped
    write raises instead of letting fsync + the name check bless the torn
    prefix — refusal, never a successful return over damage."""
    identity = _identity()
    L.append_approval(ledger_root, identity, reason="owner approved", at_epoch=T0)
    real_write = os.write
    calls = {"n": 0}

    def write_then_give_up(fd: int, data) -> int:
        calls["n"] += 1
        if calls["n"] > 3:
            return 0  # the transport stalls: no byte is accepted anymore
        view = memoryview(data)
        return real_write(fd, view[:4])

    monkeypatch.setattr(os, "write", write_then_give_up)
    with pytest.raises(OSError, match="short write"):
        L.append_consumption(ledger_root, identity, reason="G4 sealed event", at_epoch=T0 + 1)
