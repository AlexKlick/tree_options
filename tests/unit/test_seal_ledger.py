"""Authority ledger: chain verify, tamper, torn tail, /tmp refusal, kinds."""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import stat
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


def test_tampered_final_line_is_tail_damaged_not_corrupt(ledger_root) -> None:
    """A tampered FINAL line is tail damage, never mid-file corruption —
    R15 (finding 1) scopes the tolerance: the damaged line must lie BEYOND
    the anchored committed extent, because only then was it never
    acknowledged. The fixture lands a third record the way the interrupted
    append does (direct write + fsync, no anchor advance), then rewrites
    that line's reason: the anchored prefix still proves, the tampered line
    fails chain verification as the FINAL line, and the view reports tail
    damage with the two committed records intact. A tampered line INSIDE
    the committed extent is the in-place-rewrite refusal instead (the R15
    extent tests)."""
    identity = _identity()
    L.append_approval(ledger_root, identity, reason="one", at_epoch=T0)
    L.append_consumption(ledger_root, identity, reason="two", at_epoch=T0 + 1)
    path = ledger_root / L.LEDGER_FILENAME
    anchored_size = path.stat().st_size
    # the crash window: a chained third record lands and is fsynced on the
    # ledger, but its anchor update never runs
    view = L.read_ledger(ledger_root)
    note = L.LedgerRecord(
        kind=L.KIND_RECONCILIATION_NOTE,
        identity=identity,
        sealed_run_id=sealed_run_id(identity),
        content_identity=content_identity(identity),
        reason="operator note",
        at_epoch=T0 + 2,
        prev_record_sha256=view.tail_hash,
    )
    signed = note.model_copy(update={"record_sha256": L._record_hash(note)})
    fd = os.open(path, os.O_WRONLY | os.O_APPEND)
    try:
        os.write(fd, (L._encode(signed) + "\n").encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    assert path.stat().st_size > anchored_size
    # tamper the final (never-anchored) line
    lines = path.read_text().splitlines()
    last = json.loads(lines[-1])
    last["reason"] = "never happened"
    lines[-1] = json.dumps(last, sort_keys=True, separators=(",", ":"))
    path.write_text("\n".join(lines) + "\n")
    tampered = L.read_ledger(ledger_root)
    assert tampered.tail_damaged
    assert len(tampered.records) == 2


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
    """The racing point is the append's first custody-ROOT-directory fsync
    AFTER the round-8 name check has already passed (R15: the companion
    extent advance fsyncs the root after the name check; the final root
    fsync follows). It is identified by the root's REAL directory identity
    (st_dev/st_ino), never by call count — the durable-traversal walks
    (R15, finding 2) add many earlier fsyncs. The racing append may still
    acknowledge (the check already passed); the CONTRACT is the successor —
    a second execution against the clone must REFUSE on the durable
    binding, and the clone must never gain a consumption."""
    identity = _identity()
    L.append_approval(ledger_root, identity, reason="owner approved", at_epoch=T0)
    ledger_path = ledger_root / L.LEDGER_FILENAME
    before = ledger_path.read_bytes()  # the APPROVAL line only
    real_fsync = os.fsync
    root_identity = (os.stat(ledger_root).st_dev, os.stat(ledger_root).st_ino)
    armed = {"done": False}

    def fsync_swapping_after_the_name_check(fd: int) -> None:
        held = os.fstat(fd)
        if not armed["done"] and (held.st_dev, held.st_ino) == root_identity:
            armed["done"] = True  # after the name check: the companion advance
            held_name = ledger_root / (L.LEDGER_FILENAME + ".held")
            os.rename(ledger_path, held_name)  # the locked fd keeps the inode
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
    # (format 2 with extent fields, R15 finding 4: the forged companion must
    # parse as a well-formed record; the anchor still refuses the pair)
    (ledger_root / "ledger.jsonl.identity.json").write_text(
        json.dumps(
            {
                "format": 2,
                "name": L.LEDGER_FILENAME,
                "st_dev": clone_stat.st_dev,
                "st_ino": clone_stat.st_ino,
                "extent_size": len(approval_only),
                "committed_tail_sha256": "0" * 64,
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
    # Round-12 (finding 1, R14): the anchor's IDENTITY fields are immutable —
    # the creation publish is exclusive-by-name and never re-points — while
    # the COMMITTED EXTENT advances at every successful append.
    before = json.loads(anchor.read_text(encoding="utf-8"))
    L.append_consumption(ledger_root, _identity(), reason="seal", at_epoch=T0 + 1)
    after = json.loads(anchor.read_text(encoding="utf-8"))
    assert (after["st_dev"], after["st_ino"]) == (before["st_dev"], before["st_ino"])
    assert after["ledger_root"] == before["ledger_root"] == str(ledger_root.resolve())
    assert after["ledger_size"] == (ledger_root / L.LEDGER_FILENAME).stat().st_size
    assert after["ledger_size"] > before["ledger_size"]
    assert after["committed_tail_sha256"] == L.read_ledger(ledger_root).tail_hash
    # R15 (finding 4): the companion's extent advance changes its bytes, so
    # the anchor's companion digest re-pins the ADVANCED companion after
    # every append — it always names the bytes that now guard the ledger,
    # and the identity fields it protects stay immutable.
    assert after["companion_sha256"] == L.sha256_hex(companion.read_bytes())
    companion_record = json.loads(companion.read_text(encoding="utf-8"))
    assert companion_record["format"] == 2
    assert companion_record["extent_size"] == after["ledger_size"]
    assert companion_record["committed_tail_sha256"] == after["committed_tail_sha256"]


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


# ---- round-12 (finding 1, R14): the anchored COMMITTED EXTENT — a same-inode
# prefix rollback removed a consumption ------------------------------------------------
#
# The runstate anchor bound the ledger's (st_dev, st_ino) and the companion
# digest but NOT the ledger's committed extent, and ``_replay_text`` accepts
# any valid hash-chain PREFIX. So after an approval+consumption, with no
# process running, an attacker opened ledger.jsonl with truncation and
# rewrote only the original approval line: the inode never changed, the
# companion and the runstate anchor still verified, the read returned an
# approval-only view with tail_damaged=False — and the approval was
# consumed a second time. The anchor record now also pins the committed
# extent (``ledger_size`` bytes at the last committed append plus
# ``committed_tail_sha256``, the view's tail hash), advanced at every
# successful append: at open, a ledger SMALLER than the anchored extent, or
# one that holds exactly the anchored bytes but a different committed tail,
# is a corruption-class refusal — prefix rollback and in-place truncation
# both refuse. A ledger LARGER than the anchored extent with a valid chain
# is the benign next-append-after-crash window (an append acknowledged by
# the ledger fsync whose anchor update was interrupted): it is re-derived,
# accepted, and re-anchored at the next append.


def test_same_inode_prefix_rollback_removing_a_consumption_is_refused(
    ledger_root,
) -> None:
    """The exact round-12 attack: approval+consumption committed, then the
    file is truncated IN PLACE and only the approval line is rewritten — the
    name still maps to the SAME inode, so every identity check the round-11
    anchor performs still passes. The next open must refuse on the anchored
    committed extent (read AND append), and the approval must never be
    re-spent."""
    identity = _identity()
    L.append_approval(ledger_root, identity, reason="owner approved", at_epoch=T0)
    L.append_consumption(ledger_root, identity, reason="G4 sealed event", at_epoch=T0 + 1)
    ledger_path = ledger_root / L.LEDGER_FILENAME
    approval_only = ledger_path.read_text().splitlines(keepends=True)[0].encode("utf-8")
    before = os.stat(ledger_path)
    # the attack: truncate in place, rewrite only the original approval line
    fd = os.open(ledger_path, os.O_WRONLY)
    try:
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, approval_only)
        os.fsync(fd)
    finally:
        os.close(fd)
    after = os.stat(ledger_path)
    assert (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino), (
        "the attack keeps the inode, so every round-11 identity check passes"
    )
    with pytest.raises(LedgerCorruptError, match="committed extent"):
        L.read_ledger(ledger_root)
    with pytest.raises(LedgerCorruptError, match="committed extent"):
        L.append_consumption(
            ledger_root, identity, reason="second execution on the prefix", at_epoch=T0 + 2
        )
    assert ledger_path.read_bytes() == approval_only, (
        "a rolled-back prefix must never gain a second consumption — "
        "an acknowledged consumption is never silently forgotten"
    )


def test_in_place_rewrite_of_the_anchored_extent_refused(ledger_root) -> None:
    """The size rule's partner: a ledger holding EXACTLY the anchored byte
    count but a different committed tail is the same-inode in-place rewrite
    of the committed extent — refused, never accepted as authority, even
    though the replacement is a fully VALID chain (one re-chained approval
    padded to the anchored length)."""
    identity = _identity()
    L.append_approval(ledger_root, identity, reason="one", at_epoch=T0)
    L.append_consumption(ledger_root, identity, reason="two", at_epoch=T0 + 1)
    ledger_path = ledger_root / L.LEDGER_FILENAME
    anchored_size = ledger_path.stat().st_size
    # a re-chained single APPROVAL whose encoded line is padded (via the
    # reason) to exactly the anchored byte count: the chain verifies, the
    # size matches, and the committed tail is NOT the anchored one
    base = L.LedgerRecord(
        kind=L.KIND_APPROVAL,
        identity=identity,
        sealed_run_id=sealed_run_id(identity),
        content_identity=content_identity(identity),
        reason="one",
        at_epoch=T0,
        prev_record_sha256=L.GENESIS_PREV,
    )
    plain = len(
        (L._encode(base.model_copy(update={"record_sha256": L._record_hash(base)})) + "\n").encode(
            "utf-8"
        )
    )
    padded = base.model_copy(update={"reason": "one" + " " * (anchored_size - plain)})
    signed = padded.model_copy(update={"record_sha256": L._record_hash(padded)})
    line = (L._encode(signed) + "\n").encode("utf-8")
    assert len(line) == anchored_size, "the rewrite must hold exactly the anchored bytes"
    fd = os.open(ledger_path, os.O_WRONLY)
    try:
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, line)
        os.fsync(fd)
    finally:
        os.close(fd)
    assert ledger_path.stat().st_size == anchored_size
    with pytest.raises(LedgerCorruptError, match="committed extent"):
        L.read_ledger(ledger_root)


def test_next_append_after_crash_window_opens_and_reanchors(ledger_root) -> None:
    """The benign window the extent anchor must NOT refuse: an append
    acknowledged by the ledger fsync whose ANCHOR update was interrupted
    leaves the ledger LARGER than the anchored extent with a valid chain —
    the open re-derives and accepts it, and the NEXT append re-anchors at
    the new committed extent."""
    identity = _identity()
    L.append_approval(ledger_root, identity, reason="owner approved", at_epoch=T0)
    L.append_consumption(ledger_root, identity, reason="G4 sealed event", at_epoch=T0 + 1)
    ledger_path = ledger_root / L.LEDGER_FILENAME
    anchored = json.loads(L.runstate_anchor_path(ledger_root).read_text(encoding="utf-8"))
    # simulate the crash window: the NEXT chained record lands and is fsynced
    # on the ledger, but its anchor update never runs (the direct write below
    # is the fsync-landed half of the interrupted append)
    view = L.read_ledger(ledger_root)
    note = L.LedgerRecord(
        kind=L.KIND_RECONCILIATION_NOTE,
        identity=identity,
        sealed_run_id=sealed_run_id(identity),
        content_identity=content_identity(identity),
        reason="operator reviewed the interrupted append",
        at_epoch=T0 + 2,
        prev_record_sha256=view.tail_hash,
    )
    signed = note.model_copy(update={"record_sha256": L._record_hash(note)})
    line = (L._encode(signed) + "\n").encode("utf-8")
    fd = os.open(ledger_path, os.O_WRONLY | os.O_APPEND)
    try:
        os.write(fd, line)
        os.fsync(fd)
    finally:
        os.close(fd)
    assert ledger_path.stat().st_size > anchored["ledger_size"], (
        "the ledger is larger than the anchored extent, chain valid"
    )
    recovered = L.read_ledger(ledger_root)
    assert [record.kind for record in recovered.records] == [
        "APPROVAL",
        "CONSUMPTION",
        "RECONCILIATION_NOTE",
    ]
    assert not recovered.tail_damaged
    # the next append re-anchors at the new committed extent
    L.append_consumption(ledger_root, _identity(code_sha="9" * 40), reason="seal", at_epoch=T0 + 3)
    reanchored = json.loads(L.runstate_anchor_path(ledger_root).read_text(encoding="utf-8"))
    assert reanchored["ledger_size"] == ledger_path.stat().st_size
    assert reanchored["committed_tail_sha256"] == L.read_ledger(ledger_root).tail_hash


# ---- R15 (finding 1, class mechanism): a ledger LARGER than the anchored extent
# must PROVE the anchored prefix ---------------------------------------------------------
#
# Round-12 pinned the committed extent but accepted ANY larger ledger as the
# benign next-append-after-crash window — no proof was demanded that the
# first ``ledger_size`` bytes were still the anchored history. Attack (no
# concurrency, same inode): after approval+consumption are anchored at size
# N, truncate the ledger in place and rewrite a VALID chain — one re-chained
# APPROVAL plus a re-chained RECONCILIATION_NOTE whose reason pads the file
# beyond N. The chain verifies, the inode/companion/anchor identities all
# verify, and G4's authority scan skips non-consumption records
# (scripts/g4_seal.py ``_check_authority``) — the consumption silently
# vanished and the one-shot authority is spendable again. The larger branch
# now demands the PREFIX PROOF: the byte at the pinned extent must fall
# exactly on a record boundary, and replaying ONLY the pinned extent must
# chain to the pinned committed tail. Anything else at a larger size is
# corruption-class refusal.


def test_larger_rewrite_beyond_the_anchored_extent_refused_without_prefix_proof(
    ledger_root,
) -> None:
    """The R15 finding-1 attack: a same-inode rewrite that is a fully VALID
    chain LARGER than the anchored extent, whose first N bytes are NOT the
    anchored history (the approval line was re-chained, the consumption is
    gone, a padded note pushes the size past N). Pre-R15 the open accepted it
    as the benign crash window; post-R15 read AND append refuse on the
    committed extent and the approval is never re-spent."""
    identity = _identity()
    L.append_approval(ledger_root, identity, reason="one", at_epoch=T0)
    L.append_consumption(ledger_root, identity, reason="two", at_epoch=T0 + 1)
    ledger_path = ledger_root / L.LEDGER_FILENAME
    anchored_size = ledger_path.stat().st_size
    before = os.stat(ledger_path)

    def _line(record: L.LedgerRecord) -> tuple[bytes, str]:
        signed = record.model_copy(update={"record_sha256": L._record_hash(record)})
        return (L._encode(signed) + "\n").encode("utf-8"), signed.record_sha256

    approval = L.LedgerRecord(
        kind=L.KIND_APPROVAL,
        identity=identity,
        sealed_run_id=sealed_run_id(identity),
        content_identity=content_identity(identity),
        reason="one",
        at_epoch=T0,
        prev_record_sha256=L.GENESIS_PREV,
    )
    approval_line, approval_digest = _line(approval)
    note_plain = L.LedgerRecord(
        kind=L.KIND_RECONCILIATION_NOTE,
        identity=identity,
        sealed_run_id=sealed_run_id(identity),
        content_identity=content_identity(identity),
        reason="",
        at_epoch=T0 + 2,
        prev_record_sha256=approval_digest,
    )
    plain_len = len(_line(note_plain)[0])
    pad = anchored_size - len(approval_line) - plain_len + 16
    assert pad > 0, "fixture: the padded note must be constructible"
    note = note_plain.model_copy(update={"reason": "pad" + " " * pad})
    note_line, _ = _line(note)
    rewritten = approval_line + note_line
    assert len(rewritten) > anchored_size, "the rewrite must be LARGER than the anchored extent"

    # the attack: truncate in place, rewrite the padded valid chain beyond N
    fd = os.open(ledger_path, os.O_WRONLY)
    try:
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, rewritten)
        os.fsync(fd)
    finally:
        os.close(fd)
    after = os.stat(ledger_path)
    assert (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino), (
        "the attack keeps the inode, so every identity check still passes"
    )
    with pytest.raises(LedgerCorruptError, match="committed extent"):
        L.read_ledger(ledger_root)
    with pytest.raises(LedgerCorruptError, match="committed extent"):
        L.append_consumption(ledger_root, identity, reason="re-spend", at_epoch=T0 + 3)
    assert ledger_path.read_bytes() == rewritten, (
        "a larger rewrite that destroyed the anchored prefix must never gain a "
        "second consumption — an acknowledged consumption is never silently "
        "forgotten"
    )


def test_crash_between_companion_advance_and_anchor_commit_opens_and_reanchors(
    ledger_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R15 (finding 4) crash path: an append's ledger fsync and companion
    extent advance landed, but the anchor extent commit — which re-pins the
    ADVANCED companion's digest — never ran. The companion is now AHEAD of
    the anchor and no longer hashes to the anchor's pinned digest. The open
    must accept this benign window (the digest tolerance accepts exactly the
    advance shape; both extents prove as prefixes of the same ledger), and
    the next append must re-anchor BOTH durable records."""
    identity = _identity()
    L.append_approval(ledger_root, identity, reason="owner approved", at_epoch=T0)
    L.append_consumption(ledger_root, identity, reason="G4 sealed event", at_epoch=T0 + 1)
    companion_path = ledger_root / "ledger.jsonl.identity.json"
    # the crash: everything up to and including the companion advance is
    # durable, the anchor commit never runs
    monkeypatch.setattr(L, "_commit_anchor_extent", lambda *args, **kwargs: None)
    L.append_reconciliation_note(
        ledger_root, identity, reason="interrupted append", at_epoch=T0 + 2
    )
    monkeypatch.undo()

    ledger_path = ledger_root / L.LEDGER_FILENAME
    companion = json.loads(companion_path.read_text(encoding="utf-8"))
    anchor = json.loads(L.runstate_anchor_path(ledger_root).read_text(encoding="utf-8"))
    assert companion["extent_size"] == ledger_path.stat().st_size, (
        "the companion advance landed before the crash"
    )
    assert anchor["ledger_size"] < ledger_path.stat().st_size, (
        "the anchor commit never ran"
    )
    assert anchor["companion_sha256"] != L.sha256_hex(companion_path.read_bytes()), (
        "the anchor still pins the PRE-advance companion digest"
    )
    # the open accepts the crash window: digest tolerance + both prefix proofs
    view = L.read_ledger(ledger_root)
    assert [record.kind for record in view.records] == [
        "APPROVAL",
        "CONSUMPTION",
        "RECONCILIATION_NOTE",
    ]
    assert not view.tail_damaged
    # the next append re-anchors BOTH records at the new committed extent
    L.append_consumption(ledger_root, _identity(code_sha="7" * 40), reason="seal", at_epoch=T0 + 3)
    reanchored = json.loads(L.runstate_anchor_path(ledger_root).read_text(encoding="utf-8"))
    companion = json.loads(companion_path.read_text(encoding="utf-8"))
    assert reanchored["ledger_size"] == ledger_path.stat().st_size
    assert companion["extent_size"] == ledger_path.stat().st_size
    assert companion["committed_tail_sha256"] == reanchored["committed_tail_sha256"]
    assert reanchored["companion_sha256"] == L.sha256_hex(companion_path.read_bytes())
    assert reanchored["committed_tail_sha256"] == L.read_ledger(ledger_root).tail_hash


def test_seal_root_walk_repairs_a_preexisting_residue_component(
    ledger_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R15 (finding 2), seal adoption: the ledger-root custody walk
    (_open_ledger_root) is a durable traversal — a MID component a PRIOR
    invocation created without its parent fsync (the residue) is repaired
    when this invocation merely OPENS it. The residue's parent is never
    fsynced by anything else in this flow (the append creates only DEEPER
    components), so observing its fsync by directory identity proves the
    existing-open repair."""
    residue = ledger_root / "residue-mid"  # a prior invocation's uncommitted mkdir
    residue.mkdir()
    root = residue / "g4-authority"
    fsynced: list[tuple[int, int]] = []
    real_fsync = os.fsync

    def traced_fsync(fd: int) -> None:
        held = os.fstat(fd)
        fsynced.append((held.st_dev, held.st_ino))
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", traced_fsync)
    L.append_approval(root, _identity(), reason="owner approved", at_epoch=T0)
    monkeypatch.undo()
    residue_parent_identity = (os.stat(ledger_root).st_dev, os.stat(ledger_root).st_ino)
    assert residue_parent_identity in fsynced, (
        "the ledger-root walk opened the residue component without committing "
        "its entry in the residue's parent — a reboot can drop that ancestor "
        "together with the ledger root and the runstate anchor tree, silently "
        "forgetting an acknowledged consumption"
    )
    # the recovery invariant: the append succeeded and reads back
    assert [record.kind for record in L.read_ledger(root).records] == ["APPROVAL"]


# ---- round-12 (finding 3, R14): first-use namespace creations are
# PARENT-fsynced -----------------------------------------------------------------------
#
# The ledger root is created by mkdir under artifacts/ (the ledger-root
# custody walk) and the anchor-tree components by mkdir (the runstate
# custody walk), but the existing fsyncs covered only the DEEPER
# directories (the anchor dir, the ledger root) — never the PARENT that
# holds the newly created entry. After a successful approval+consumption on
# a fresh root, a reboot could lose the g4-authority entry AND the
# anchor-tree entries together; both absent, the next read returns an EMPTY
# view and an acknowledged consumption is silently forgotten. Ordering is
# not observable state, so the owning test pins it two ways: the RECOVERY
# INVARIANT (a structural walk of the whole namespace after a fresh-root
# first append, everything present and self-consistent) and the ORDER
# ITSELF (every directory component the append CREATED is committed by an
# fsync of its PARENT, observed on the parent's real directory identity).


def test_fresh_root_first_append_parent_fsyncs_every_created_namespace_component(
    ledger_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round-12 (finding 3): on a fresh root, the first append creates the
    ledger-root components AND the anchor-tree components. Each created
    entry must be durably committed in its PARENT before the append can
    acknowledge — otherwise a reboot loses the g4-authority entry and the
    anchor-tree entries together, both reads then see nothing, and an
    acknowledged consumption is silently forgotten."""
    fresh_parent = ledger_root / "fresh"
    fresh_root = fresh_parent / "g4-authority"
    anchor_dir = fresh_parent / "runstate" / "seal-ledger-anchor"
    # (b) the ORDER: trace mkdir/fsync by DIRECTORY IDENTITY (dev, ino), so
    # fd reuse can never forge a match.
    created_parents: list[tuple[int, int]] = []
    fsyncs: list[tuple[tuple[int, int], int]] = []  # (identity, mkdirs-so-far)
    real_mkdir, real_fsync = os.mkdir, os.fsync

    def traced_mkdir(path, mode=0o777, *, dir_fd=None):
        real_mkdir(path, mode, dir_fd=dir_fd)  # FileExistsError is not a creation
        if dir_fd is not None:
            held = os.fstat(dir_fd)
            created_parents.append((held.st_dev, held.st_ino))

    def traced_fsync(fd):
        held = os.fstat(fd)
        fsyncs.append(((held.st_dev, held.st_ino), len(created_parents)))
        real_fsync(fd)

    monkeypatch.setattr(os, "mkdir", traced_mkdir)
    monkeypatch.setattr(os, "fsync", traced_fsync)
    L.append_approval(fresh_root, _identity(), reason="owner approved", at_epoch=T0)
    monkeypatch.undo()

    assert len(created_parents) >= 4, "the fresh-root append creates at least 4 components"
    for index, parent in enumerate(created_parents):
        assert any(identity == parent and seq > index for identity, seq in fsyncs), (
            f"the parent directory of created namespace component #{index + 1} "
            f"(dev/ino {parent[0]}/{parent[1]}) is never fsynced after the mkdir — "
            "a reboot can lose the entry and silently forget an acknowledged "
            "consumption"
        )

    # (a) the RECOVERY INVARIANT: every namespace component from artifacts/
    # down exists as a REAL directory (never a symlink), and the whole
    # authority surface is present and self-consistent.
    for component in (
        REPO_ROOT / "artifacts",
        REPO_ROOT / "artifacts" / "g4-seal-tests",
        ledger_root,
        fresh_parent,
        fresh_root,
        fresh_parent / "runstate",
        anchor_dir,
    ):
        named = component.lstat()
        assert stat.S_ISDIR(named.st_mode), f"{component} must be a real directory"
        assert not stat.S_ISLNK(named.st_mode), f"{component} must not be a symlink"
    ledger_path = fresh_root / L.LEDGER_FILENAME
    assert stat.S_ISREG(ledger_path.lstat().st_mode)
    view = L.read_ledger(fresh_root)
    assert [record.kind for record in view.records] == ["APPROVAL"]
    assert not view.tail_damaged
    companion = fresh_root / "ledger.jsonl.identity.json"
    anchor = json.loads(L.runstate_anchor_path(fresh_root).read_text(encoding="utf-8"))
    ledger_stat = os.stat(ledger_path)
    assert (anchor["st_dev"], anchor["st_ino"]) == (ledger_stat.st_dev, ledger_stat.st_ino)
    assert anchor["ledger_root"] == str(fresh_root.resolve())
    assert anchor["companion_sha256"] == L.sha256_hex(companion.read_bytes())
    assert anchor["ledger_size"] == ledger_stat.st_size
    assert anchor["committed_tail_sha256"] == view.tail_hash
