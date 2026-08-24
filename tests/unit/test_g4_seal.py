"""g4_seal CLI: preflight availability-only contract + the execute matrix."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import g4_seal  # noqa: E402
from tree_options.protocol.loader import load_protocol, protocol_hash  # noqa: E402
from tree_options.seal import ledger as L  # noqa: E402
from tree_options.seal.errors import (  # noqa: E402
    ApprovalInvalidError,
    SecondExecutionRefusedError,
)
from tree_options.seal.identity import (  # noqa: E402
    SealedIdentity,
    content_identity,
    sealed_run_id,
)

T0 = 1_800_000_000
CODE_SHA = "9" * 40
SIX_INPUT_IDS = {
    "code_sha",
    "protocol_hash",
    "lane1_manifest_sha256",
    "lane2_manifest_sha256",
    "calendar_decision",
    "criteria",
}


def _identity(**overrides: str) -> SealedIdentity:
    fields = dict(
        code_sha=CODE_SHA,
        protocol_hash="b" * 64,
        lane1_manifest_sha256="c" * 64,
        lane2_manifest_sha256="d" * 64,
        calendar_decision="repo-generated-calendar",
        criteria_sha256="e" * 64,
    )
    fields.update(overrides)
    return SealedIdentity(**fields)


# Same conflict resolution as test_seal_ledger: pytest tmp_path is under /tmp
# on this host and the ledger refuses roots under /tmp, so injected ledger
# roots live under the REPO's gitignored artifacts/ scratch area. Plain
# data files (lane manifests) may use tmp_path — the refusal is a ledger-root
# rule, not a general /tmp rule.
@pytest.fixture()
def ledger_root() -> Iterator[Path]:
    root = REPO_ROOT / "artifacts" / "g4-seal-tests" / uuid.uuid4().hex
    root.mkdir(parents=True, exist_ok=True)
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---- preflight -----------------------------------------------------------


def _completed(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


def _clean_git_runner(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
    if "rev-parse" in argv:
        return _completed(CODE_SHA + "\n")
    if "status" in argv:
        return _completed("")
    raise AssertionError(f"unexpected git call: {argv}")


def _dirty_git_runner(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
    if "rev-parse" in argv:
        return _completed(CODE_SHA + "\n")
    if "status" in argv:
        return _completed(" M src/tree_options/time/calendar.py\n")
    raise AssertionError(f"unexpected git call: {argv}")


def _criteria_sha() -> str:
    # Recomputed against the COMMITTED file — the pin that keeps the
    # transcribed criteria from drifting unnoticed.
    return hashlib.sha256(
        (REPO_ROOT / "data" / "g4" / "sealed-criteria.json").read_bytes()
    ).hexdigest()


def _write_manifests(tmp_path: Path) -> tuple[Path, Path]:
    lane1 = tmp_path / "lane1-manifest.json"
    lane1.write_text('{"lane": 1}', encoding="utf-8")
    lane2 = tmp_path / "lane2-manifest.json"
    lane2.write_text('{"lane": 2}', encoding="utf-8")
    return lane1, lane2


def _all_six_args(tmp_path: Path, ledger_root: Path) -> list[str]:
    lane1, lane2 = _write_manifests(tmp_path)
    return [
        "--ledger-root",
        str(ledger_root),
        "--lane1-manifest",
        str(lane1),
        "--lane2-manifest",
        str(lane2),
        "--calendar-decision",
        "repo-generated-calendar",
        "--criteria-sha256",
        _criteria_sha(),
    ]


def _preflight(
    args: list[str],
    capsys: pytest.CaptureFixture[str],
    *,
    git_runner: g4_seal.GitRunner | None = None,
) -> tuple[int, str, dict]:
    runner = git_runner if git_runner is not None else _clean_git_runner
    code = g4_seal.cmd_preflight(args, git_runner=runner)
    raw = capsys.readouterr().out
    return code, raw, json.loads(raw)


def test_preflight_all_available_verdict_is_null_and_not_computed(tmp_path, ledger_root, capsys):
    code, raw, payload = _preflight(_all_six_args(tmp_path, ledger_root), capsys)
    assert code == 0
    # The literal JSON is the firewall: a verdict can never appear here.
    assert '"verdict": null' in raw
    assert '"verdict_computed": false' in raw
    assert payload["verdict"] is None
    assert payload["verdict_computed"] is False
    assert set(payload["criteria_inputs"]) == SIX_INPUT_IDS
    assert all(s["available"] for s in payload["criteria_inputs"].values())
    assert payload["criteria_inputs"]["code_sha"]["evidence"] == CODE_SHA
    assert payload["criteria_inputs"]["protocol_hash"]["evidence"] == protocol_hash(load_protocol())
    assert payload["criteria_inputs"]["criteria"]["evidence"] == _criteria_sha()


def test_preflight_dirty_tracked_tree_unavailable(tmp_path, ledger_root, capsys):
    code, _, payload = _preflight(
        _all_six_args(tmp_path, ledger_root), capsys, git_runner=_dirty_git_runner
    )
    assert code == 2
    assert payload["criteria_inputs"]["code_sha"]["available"] is False
    assert "dirty" in payload["criteria_inputs"]["code_sha"]["reason"]


def test_preflight_untracked_artifacts_and_dist_ignored(tmp_path, ledger_root, capsys):
    def untracked_only_runner(
        argv: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        if "rev-parse" in argv:
            return _completed(CODE_SHA + "\n")
        if "status" in argv:
            return _completed("?? artifacts/g4-authority/\n?? dist/\n")
        raise AssertionError(f"unexpected git call: {argv}")

    code, _, payload = _preflight(
        _all_six_args(tmp_path, ledger_root), capsys, git_runner=untracked_only_runner
    )
    assert code == 0
    assert payload["criteria_inputs"]["code_sha"]["available"] is True


def _drop_pair(args: list[str], flag: str) -> list[str]:
    keep: list[str] = []
    skip_next = False
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if arg == flag:
            skip_next = True
            continue
        keep.append(arg)
    return keep


def test_preflight_missing_lane2_exit_2_with_reason(tmp_path, ledger_root, capsys):
    args = _drop_pair(_all_six_args(tmp_path, ledger_root), "--lane2-manifest")
    code, _, payload = _preflight(args, capsys)
    assert code == 2
    assert payload["criteria_inputs"]["lane2_manifest_sha256"]["available"] is False
    assert payload["criteria_inputs"]["lane2_manifest_sha256"]["reason"]
    assert payload["criteria_inputs"]["code_sha"]["available"] is True


def test_preflight_calendar_pending_unavailable(tmp_path, ledger_root, capsys):
    args = _drop_pair(_all_six_args(tmp_path, ledger_root), "--calendar-decision")
    code, _, payload = _preflight(args, capsys)
    assert code == 2
    status = payload["criteria_inputs"]["calendar_decision"]
    assert status["available"] is False
    assert "PENDING" in status["reason"]


def test_preflight_calendar_decided_is_available(tmp_path, ledger_root, capsys):
    args = _all_six_args(tmp_path, ledger_root)
    args[args.index("repo-generated-calendar")] = "weekend-only-accepted"
    code, _, payload = _preflight(args, capsys)
    assert code == 0
    assert payload["criteria_inputs"]["calendar_decision"]["evidence"] == "weekend-only-accepted"


def test_preflight_criteria_sha_mismatch_unavailable(tmp_path, ledger_root, capsys):
    args = _all_six_args(tmp_path, ledger_root)
    args[args.index(_criteria_sha())] = "f" * 64
    code, _, payload = _preflight(args, capsys)
    assert code == 2
    assert payload["criteria_inputs"]["criteria"]["available"] is False
    assert "mismatch" in payload["criteria_inputs"]["criteria"]["reason"]


def test_preflight_corrupt_ledger_exit_3(tmp_path, ledger_root, capsys):
    L.append_approval(ledger_root, _identity(), reason="one", at_epoch=T0)
    L.append_consumption(ledger_root, _identity(), reason="two", at_epoch=T0 + 1)
    path = ledger_root / L.LEDGER_FILENAME
    lines = path.read_text().splitlines()
    first = json.loads(lines[0])
    first["reason"] = "rewritten"
    lines[0] = json.dumps(first, sort_keys=True, separators=(",", ":"))
    path.write_text("\n".join(lines) + "\n")
    code = g4_seal.cmd_preflight(_all_six_args(tmp_path, ledger_root), git_runner=_clean_git_runner)
    assert code == 3
    assert "LEDGER UNREADABLE" in capsys.readouterr().err


def test_preflight_tmp_ledger_root_exit_3(tmp_path, capsys):
    args = _all_six_args(tmp_path, Path("/tmp") / f"g4-seal-refused-{uuid.uuid4().hex}")
    code = g4_seal.cmd_preflight(args, git_runner=_clean_git_runner)
    assert code == 3
    assert "LEDGER UNREADABLE" in capsys.readouterr().err


def test_bare_invocation_exits_2(capsys):
    assert g4_seal.main([]) == 2
    err = capsys.readouterr().err
    assert "preflight" in err
    assert "execute" in err


# ---- execute: the one-shot matrix (function seam; the CLI refuses) -------


def _approve(root: Path, identity: SealedIdentity) -> None:
    L.append_approval(root, identity, reason="owner approved via library", at_epoch=T0 - 1)


def _quiet_runner(identity: SealedIdentity) -> str:
    return "sealed-run-complete"


def _never_runs(identity: SealedIdentity) -> str:
    raise AssertionError("runner must not be invoked on a refused execution")


def test_first_execution_consumption_durable_before_runner(ledger_root):
    identity = _identity()
    _approve(ledger_root, identity)
    run_id = sealed_run_id(identity)

    def runner(presented: SealedIdentity) -> str:
        # ORDERING PROOF: by the time the runner is invoked, its CONSUMPTION
        # must already be durable in the ledger.
        view = L.read_ledger(ledger_root)
        consumptions = [r for r in view.records if r.kind == L.KIND_CONSUMPTION]
        assert consumptions, "runner invoked before the CONSUMPTION was durable"
        assert consumptions[-1].sealed_run_id == run_id
        assert presented == identity
        return "sealed-run-complete"

    summary = g4_seal.execute_sealed_run(
        identity,
        ledger_root=ledger_root,
        reason="G4 sealed event",
        at_epoch=T0,
        runner=runner,
    )
    assert summary.runner_outcome == "sealed-run-complete"
    assert summary.sealed_run_id == run_id
    records = L.read_ledger(ledger_root).records
    assert [r.kind for r in records] == ["APPROVAL", "CONSUMPTION"]
    assert records[-1].record_sha256 == summary.consumption_record_sha256
    assert records[-1].sealed_run_id == run_id
    assert records[-1].content_identity == content_identity(identity)


def test_second_execution_same_identity_exit_7(ledger_root):
    identity = _identity()
    _approve(ledger_root, identity)
    g4_seal.execute_sealed_run(
        identity, ledger_root=ledger_root, reason="first", at_epoch=T0, runner=_quiet_runner
    )
    with pytest.raises(SecondExecutionRefusedError) as exc_info:
        g4_seal.execute_sealed_run(
            identity,
            ledger_root=ledger_root,
            reason="second",
            at_epoch=T0 + 1,
            runner=_never_runs,
        )
    assert exc_info.value.exit_code == 7
    # Exactly one CONSUMPTION ever:
    assert len([r for r in L.read_ledger(ledger_root).records if r.kind == L.KIND_CONSUMPTION]) == 1


def test_second_execution_changed_code_sha_same_content_still_exit_7(ledger_root):
    identity = _identity()
    _approve(ledger_root, identity)
    g4_seal.execute_sealed_run(
        identity, ledger_root=ledger_root, reason="first", at_epoch=T0, runner=_quiet_runner
    )
    # A DIFFERENT checkout of the SAME research content: sealed_run_id moves,
    # content_identity does not — still refused.
    replay = identity.model_copy(update={"code_sha": "f" * 40})
    assert sealed_run_id(replay) != sealed_run_id(identity)
    assert content_identity(replay) == content_identity(identity)
    with pytest.raises(SecondExecutionRefusedError) as exc_info:
        g4_seal.execute_sealed_run(
            replay,
            ledger_root=ledger_root,
            reason="second checkout",
            at_epoch=T0 + 1,
            runner=_never_runs,
        )
    assert exc_info.value.exit_code == 7


def test_approval_missing_exit_6(ledger_root):
    identity = _identity()
    with pytest.raises(ApprovalInvalidError) as exc_info:
        g4_seal.execute_sealed_run(
            identity, ledger_root=ledger_root, reason="no approval", at_epoch=T0, runner=_never_runs
        )
    assert exc_info.value.exit_code == 6
    assert L.read_ledger(ledger_root).records == ()


def test_approval_tampered_payload_exit_6(ledger_root):
    # A chain-VALID ledger whose APPROVAL record does not recompute to its
    # stored sealed_run_id (forged through the public append API): only the
    # recompute-and-compare check catches it.
    real = _identity()
    forged = L.LedgerRecord(
        kind=L.KIND_APPROVAL,
        identity=_identity(code_sha="f" * 40),
        sealed_run_id=sealed_run_id(real),
        content_identity=content_identity(real),
        reason="tampered payload",
        at_epoch=T0,
        prev_record_sha256=L.GENESIS_PREV,
    )
    L.append_record(ledger_root, forged)
    with pytest.raises(ApprovalInvalidError) as exc_info:
        g4_seal.execute_sealed_run(
            real, ledger_root=ledger_root, reason="seal", at_epoch=T0, runner=_never_runs
        )
    assert exc_info.value.exit_code == 6
    assert not any(r.kind == L.KIND_CONSUMPTION for r in L.read_ledger(ledger_root).records)


def test_crash_after_consumption_is_durable_unknown_never_rerun(ledger_root):
    identity = _identity()
    _approve(ledger_root, identity)

    def crashing_runner(presented: SealedIdentity) -> str:
        raise RuntimeError("crash mid-seal")

    with pytest.raises(RuntimeError):
        g4_seal.execute_sealed_run(
            identity, ledger_root=ledger_root, reason="first", at_epoch=T0, runner=crashing_runner
        )
    # The CONSUMPTION outlived the crash: the state is UNKNOWN /
    # RECONCILIATION_REQUIRED, and a later identical execute refuses rather
    # than re-running the sealed event.
    assert len([r for r in L.read_ledger(ledger_root).records if r.kind == L.KIND_CONSUMPTION]) == 1
    with pytest.raises(SecondExecutionRefusedError) as exc_info:
        g4_seal.execute_sealed_run(
            identity, ledger_root=ledger_root, reason="retry", at_epoch=T0 + 1, runner=_never_runs
        )
    assert exc_info.value.exit_code == 7


def test_execute_cli_refuses_without_consuming(ledger_root, capsys):
    identity = _identity()
    _approve(ledger_root, identity)
    assert g4_seal.cmd_execute(["--ledger-root", str(ledger_root), "--reason", "cli attempt"]) == 2
    err = capsys.readouterr().err
    assert "EXECUTE REFUSED" in err
    assert "runner" in err


# --- Round-1 review probes (2026-08-23, g4_seal half of F7) -----------------


def test_forged_consumption_true_payload_forged_stored_ids_refused_as_corrupt(ledger_root):
    """Round-2 review fix (2026-08-23, finding 7): the round-1 version of
    this test first performed a LEGITIMATE execute, so the duplicate guard
    refused on the legit consumption before the forged record was ever
    reached. The forged record is now the ONLY consumption: stored ids
    ("0"*64) disagreeing with their own identity payload are refused as
    CORRUPTION — the stored-vs-recomputed-payload arm fires BEFORE the
    duplicate check (the self-consistent duplicate case is covered by
    test_second_execution_same_identity_exit_7)."""
    identity = _identity()
    _approve(ledger_root, identity)
    # Forge a CONSUMPTION that carries the LEGITIMATE identity payload (so
    # the recompute yields this run's real ids) but adversarial stored ids.
    forged = L.LedgerRecord(
        kind=L.KIND_CONSUMPTION,
        identity=identity,
        reason="forged consumption with adversarial stored ids",
        at_epoch=T0 + 10,
        sealed_run_id="0" * 64,  # deliberate mismatch with the payload
        content_identity="0" * 64,  # deliberate mismatch with the payload
        prev_record_sha256=L.read_ledger(ledger_root).tail_hash,
        record_sha256="",
    )
    forged = forged.model_copy(update={"record_sha256": L._record_hash(forged)})
    # Append directly via the public append API (chain-valid: the prev hash
    # matches the approval tail and record_sha256 binds the body).
    L.append_record(ledger_root, forged)
    # Execute must refuse: stored ids disagree with their own payload —
    # corruption, caught before the duplicate arm could trust anything.
    with pytest.raises(L.LedgerCorruptError, match="stored ids disagree"):
        g4_seal.execute_sealed_run(
            identity,
            ledger_root=ledger_root,
            reason="execute-after-forged-only",
            at_epoch=T0 + 20,
            runner=_never_runs,
        )
    # The runner was never invoked and exactly ONE consumption remains
    # (the forged one): no second consumption was appended.
    records = L.read_ledger(ledger_root).records
    consumptions = [r for r in records if r.kind == L.KIND_CONSUMPTION]
    assert len(consumptions) == 1
    assert consumptions[0].reason == "forged consumption with adversarial stored ids"


def test_consumption_record_with_inconsistent_stored_ids_refused_as_corrupt(
    ledger_root,
):
    """Round-1 review probe: a stored sealed_run_id that disagrees with
    the record's own identity payload is itself a corruption signal —
    refuse rather than skip. The chain must verify even the duplicate
    guard."""
    identity = _identity()
    _approve(ledger_root, identity)
    # Build a record with an identity that does NOT match the stored ids.
    # Use a different identity for the payload.
    other_identity = identity.model_copy(update={"code_sha": "f" * 40})
    record = L.LedgerRecord(
        kind=L.KIND_CONSUMPTION,
        identity=other_identity,  # payload belongs to a different identity
        reason="mismatched stored vs payload",
        at_epoch=T0 + 1,
        sealed_run_id=sealed_run_id(identity),  # STORED ids name the legit one
        content_identity=content_identity(identity),
        prev_record_sha256=L.read_ledger(ledger_root).tail_hash,
        record_sha256="",
    )
    record = record.model_copy(update={"record_sha256": L._record_hash(record)})
    L.append_record(ledger_root, record)
    # Now a real execute with the LEGITIMATE identity must refuse:
    # the duplicate guard detects that stored ids disagree with payload
    # (corruption), and raises LedgerCorruptError.
    with pytest.raises(L.LedgerCorruptError):
        g4_seal.execute_sealed_run(
            identity,
            ledger_root=ledger_root,
            reason="legit-after-mismatched",
            at_epoch=T0 + 2,
            runner=_never_runs,
        )
    records = L.read_ledger(ledger_root).records
    # The forged record IS appended (probe scenario); execute refused
    # WITHOUT appending a SECOND consumption. Exactly one consumption
    # in the ledger, and it's the forged one.
    consumptions = [r for r in records if r.kind == L.KIND_CONSUMPTION]
    assert len(consumptions) == 1
