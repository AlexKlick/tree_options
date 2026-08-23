"""Authority ledger: chain verify, tamper, torn tail, /tmp refusal, kinds."""

from __future__ import annotations

import json
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
        calendar_decision="repo-generated-calendar",
        criteria_sha256="e" * 64,
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
