"""G4 typed preflight and approval/consumption/execution cross-join."""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
import uuid
from collections.abc import Callable, Iterator
from dataclasses import replace as dataclass_replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tests.unit.test_g4_verified_inputs import (
    CODE_SHA,
    TEST_RUNNER_CONFIG_DIGEST,
    InputFixture,
    clean_git_runner,
    write_valid_inputs,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import g4_seal  # noqa: E402
from tree_options.seal import ledger as L  # noqa: E402
from tree_options.seal import verified_inputs as vi  # noqa: E402
from tree_options.seal.errors import (  # noqa: E402
    ApprovalInvalidError,
    SecondExecutionRefusedError,
    VerifiedInputsError,
)
from tree_options.seal.identity import RUNNER_VERSION, content_identity, sealed_run_id  # noqa: E402
from tree_options.seal.verified_inputs import (  # noqa: E402
    HeldVerifiedSealedInputs,
    VerifiedSealedInputs,
    build_calendar_decision_artifact,
    identity_from_packet,
    verify_sealed_inputs,
)

T0 = 1_800_000_000
SEVEN_INPUT_IDS = {
    "code_sha",
    "protocol_hash",
    "lane1",
    "lane2",
    "calendar_decision",
    "criteria",
    "runner",
}


@pytest.fixture()
def ledger_root() -> Iterator[Path]:
    # Authority roots may not live under pytest's /tmp tree. This ignored
    # repo-local scratch directory is test-only and removed after each test.
    root = REPO_ROOT / "artifacts" / "g4-seal-tests" / uuid.uuid4().hex
    root.mkdir(parents=True, exist_ok=True)
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _completed(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


def _dirty_git_runner(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
    if "rev-parse" in argv:
        return _completed(CODE_SHA + "\n")
    if "status" in argv:
        return _completed(" M src/tree_options/time/calendar.py\n")
    raise AssertionError(f"unexpected git call: {argv}")


def _git_runner_for(sha: str) -> g4_seal.GitRunner:
    def runner(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if "rev-parse" in argv:
            return _completed(sha + "\n")
        if "status" in argv:
            return _completed("")
        raise AssertionError(f"unexpected git call: {argv}")

    return runner


def _args(fixture: InputFixture, ledger_root: Path) -> list[str]:
    return [
        "--repo",
        str(fixture.paths.repo),
        "--ledger-root",
        str(ledger_root),
        "--lane1-manifest",
        str(fixture.lane1_manifest),
        "--lane1-source",
        str(fixture.lane1_source),
        "--lane2-manifest",
        str(fixture.lane2_manifest),
        "--calendar-decision-artifact",
        str(fixture.calendar),
    ]


def _preflight(
    fixture: InputFixture,
    ledger_root: Path,
    capsys: pytest.CaptureFixture[str],
    *,
    git_runner: g4_seal.GitRunner = clean_git_runner,
) -> tuple[int, str, dict]:
    code = g4_seal.cmd_preflight(_args(fixture, ledger_root), git_runner=git_runner)
    raw = capsys.readouterr().out
    return code, raw, json.loads(raw)


class StubRunner:
    runner_version = RUNNER_VERSION

    def __init__(self, callback: Callable[[HeldVerifiedSealedInputs], str] | None = None):
        self.calls = 0
        self.presented: HeldVerifiedSealedInputs | None = None
        self._callback = callback

    def __call__(self, inputs: HeldVerifiedSealedInputs) -> str:
        self.calls += 1
        self.presented = inputs
        if self._callback is not None:
            return self._callback(inputs)
        return "sealed-run-complete"


# Round-11 finding 8: the runner machinery is REGISTRY authority. The default
# fixture seeds a plain stub so packets bind machinery; tests that need a
# specific instance (callbacks, crash simulation, forged entries) seed their
# own with _seeded_registry BEFORE the packet is built — the packet records
# the registered implementation's code-file hash at build time.


@pytest.fixture(autouse=True)
def registered_runner() -> Iterator[StubRunner]:
    runner = StubRunner()
    vi.RUNNER_REGISTRY.clear()
    vi.register_runner(runner, config_digest=TEST_RUNNER_CONFIG_DIGEST)
    try:
        yield runner
    finally:
        vi.RUNNER_REGISTRY.clear()


@contextlib.contextmanager
def _seeded_registry(
    runner: object, *, config_digest: str = TEST_RUNNER_CONFIG_DIGEST
) -> Iterator[None]:
    saved = dict(vi.RUNNER_REGISTRY)
    vi.RUNNER_REGISTRY.clear()
    vi.register_runner(runner, config_digest=config_digest)  # type: ignore[arg-type]
    try:
        yield
    finally:
        vi.RUNNER_REGISTRY.clear()
        vi.RUNNER_REGISTRY.update(saved)


def _packet(fixture: InputFixture, *, git_runner: g4_seal.GitRunner = clean_git_runner):
    return verify_sealed_inputs(fixture.paths, git_runner=git_runner).packet


def _approve(root: Path, packet: VerifiedSealedInputs) -> None:
    L.append_approval(
        root,
        identity_from_packet(packet),
        reason="owner approved verified packet via library",
        at_epoch=T0 - 1,
    )


def _execute(
    packet: VerifiedSealedInputs,
    fixture: InputFixture,
    ledger_root: Path,
    *,
    git_runner: g4_seal.GitRunner = clean_git_runner,
    at_epoch: int = T0,
):
    return g4_seal.execute_sealed_run(
        packet,
        inputs=fixture.paths,
        ledger_root=ledger_root,
        reason="G4 sealed event test",
        at_epoch=at_epoch,
        git_runner=git_runner,
    )


# ---- preflight: typed availability only ---------------------------------


def test_preflight_all_verified_verdict_is_null_and_not_computed(
    tmp_path: Path, ledger_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fixture = write_valid_inputs(tmp_path)
    code, raw, payload = _preflight(fixture, ledger_root, capsys)
    assert code == 0
    assert '"verdict": null' in raw
    assert '"verdict_computed": false' in raw
    assert payload["verdict"] is None
    assert payload["verdict_computed"] is False
    assert set(payload["criteria_inputs"]) == SEVEN_INPUT_IDS
    assert all(status["available"] for status in payload["criteria_inputs"].values())
    assert payload["verified_inputs"]["code_sha"] == CODE_SHA
    assert payload["verified_inputs"]["packet_content_sha256"]


def test_preflight_dirty_tracked_tree_unavailable(
    tmp_path: Path, ledger_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fixture = write_valid_inputs(tmp_path)
    code, _, payload = _preflight(fixture, ledger_root, capsys, git_runner=_dirty_git_runner)
    assert code == 2
    assert payload["verified_inputs"] is None
    assert payload["criteria_inputs"]["code_sha"]["available"] is False
    assert "dirty" in payload["criteria_inputs"]["code_sha"]["reason"]


def test_preflight_untracked_output_is_ignored(
    tmp_path: Path, ledger_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def untracked_runner(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if "rev-parse" in argv:
            return _completed(CODE_SHA + "\n")
        if "status" in argv:
            return _completed("?? artifacts/g4-authority/\n?? dist/\n")
        raise AssertionError(f"unexpected git call: {argv}")

    fixture = write_valid_inputs(tmp_path)
    code, _, payload = _preflight(fixture, ledger_root, capsys, git_runner=untracked_runner)
    assert code == 0
    assert payload["criteria_inputs"]["code_sha"]["available"] is True


def test_preflight_missing_required_typed_path_exit_2(
    tmp_path: Path, ledger_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fixture = write_valid_inputs(tmp_path)
    args = _args(fixture, ledger_root)
    index = args.index("--lane2-manifest")
    del args[index : index + 2]
    code = g4_seal.cmd_preflight(args, git_runner=clean_git_runner)
    payload = json.loads(capsys.readouterr().out)
    assert code == 2
    assert payload["verified_inputs"] is None
    assert payload["criteria_inputs"]["lane2"]["available"] is False
    assert "required" in payload["criteria_inputs"]["lane2"]["reason"]


def test_preflight_arbitrary_json_lane_manifest_exit_2(
    tmp_path: Path, ledger_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fixture = write_valid_inputs(tmp_path)
    fixture.lane1_manifest.write_text('{"lane": 1}\n', encoding="utf-8")
    code, _, payload = _preflight(fixture, ledger_root, capsys)
    assert code == 2
    assert payload["verified_inputs"] is None
    assert payload["criteria_inputs"]["lane1"]["available"] is False


def test_preflight_corrupt_ledger_exit_3(
    tmp_path: Path, ledger_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fixture = write_valid_inputs(tmp_path)
    packet = _packet(fixture)
    identity = identity_from_packet(packet)
    L.append_approval(ledger_root, identity, reason="one", at_epoch=T0)
    L.append_consumption(ledger_root, identity, reason="two", at_epoch=T0 + 1)
    path = ledger_root / L.LEDGER_FILENAME
    lines = path.read_text().splitlines()
    first = json.loads(lines[0])
    first["reason"] = "rewritten"
    lines[0] = json.dumps(first, sort_keys=True, separators=(",", ":"))
    path.write_text("\n".join(lines) + "\n")
    assert g4_seal.cmd_preflight(_args(fixture, ledger_root), git_runner=clean_git_runner) == 3
    assert "LEDGER UNREADABLE" in capsys.readouterr().err


def test_preflight_tmp_ledger_root_exit_3(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fixture = write_valid_inputs(tmp_path)
    refused = Path("/tmp") / f"g4-seal-refused-{uuid.uuid4().hex}"
    assert g4_seal.cmd_preflight(_args(fixture, refused), git_runner=clean_git_runner) == 3
    assert "LEDGER UNREADABLE" in capsys.readouterr().err


def test_bare_invocation_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    assert g4_seal.main([]) == 2
    err = capsys.readouterr().err
    assert "preflight" in err
    assert "execute" in err


# ---- execute: approval + packet + current inputs + runner ----------------


def test_first_execution_consumption_durable_before_runner_gets_same_held_bytes(
    tmp_path: Path, ledger_root: Path
) -> None:
    fixture = write_valid_inputs(tmp_path)
    expected_source = fixture.lane1_source.read_bytes()

    def inspect(inputs: HeldVerifiedSealedInputs) -> str:
        consumptions = [
            record
            for record in L.read_ledger(ledger_root).records
            if record.kind == L.KIND_CONSUMPTION
        ]
        assert consumptions[-1].sealed_run_id == sealed_run_id(identity)
        # Path movement after consumption cannot change what the runner sees.
        fixture.lane1_source.write_text("replacement after verification\n", encoding="utf-8")
        assert inputs.lane1_payloads[0].raw == expected_source
        assert inputs.packet == packet
        return "sealed-run-complete"

    runner = StubRunner(inspect)
    with _seeded_registry(runner):
        packet = _packet(fixture)
        identity = identity_from_packet(packet)
        _approve(ledger_root, packet)
        summary = _execute(packet, fixture, ledger_root)
    assert summary.runner_outcome == "sealed-run-complete"
    assert runner.calls == 1
    records = L.read_ledger(ledger_root).records
    assert [record.kind for record in records] == ["APPROVAL", "CONSUMPTION"]
    assert records[-1].identity.verified_packet_sha256 == packet.packet_content_sha256


def test_runner_uses_preconsumption_held_bundle_when_paths_move_during_append(
    tmp_path: Path, ledger_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = write_valid_inputs(tmp_path)
    expected_source = fixture.lane1_source.read_bytes()
    real_append = g4_seal.seal_ledger.append_record

    def append_then_move(*args: object, **kwargs: object) -> str:
        digest = real_append(*args, **kwargs)
        fixture.lane1_source.write_text("moved after consumption\n", encoding="utf-8")
        return digest

    def inspect(inputs: HeldVerifiedSealedInputs) -> str:
        assert inputs.lane1_payloads[0].raw == expected_source
        return "same-held-bundle"

    runner = StubRunner(inspect)
    with _seeded_registry(runner):
        packet = _packet(fixture)
        _approve(ledger_root, packet)
        # the wrapper arms only for the CONSUMPTION append (the approval above
        # also routes through append_record and must see the file intact)
        monkeypatch.setattr(g4_seal.seal_ledger, "append_record", append_then_move)
        summary = _execute(packet, fixture, ledger_root)
    assert summary.runner_outcome == "same-held-bundle"


def test_second_execution_same_packet_exit_7(
    tmp_path: Path, ledger_root: Path, registered_runner: StubRunner
) -> None:
    fixture = write_valid_inputs(tmp_path)
    packet = _packet(fixture)
    _approve(ledger_root, packet)
    _execute(packet, fixture, ledger_root)
    with pytest.raises(SecondExecutionRefusedError) as exc_info:
        _execute(packet, fixture, ledger_root, at_epoch=T0 + 1)
    assert exc_info.value.exit_code == 7
    assert registered_runner.calls == 1, "the first execution ran the machinery once"
    assert (
        len(
            [
                record
                for record in L.read_ledger(ledger_root).records
                if record.kind == L.KIND_CONSUMPTION
            ]
        )
        == 1
    )


def test_changed_checkout_same_content_still_exit_7(tmp_path: Path, ledger_root: Path) -> None:
    fixture = write_valid_inputs(tmp_path)
    packet_a = _packet(fixture)
    _approve(ledger_root, packet_a)
    _execute(packet_a, fixture, ledger_root)

    packet_b = _packet(fixture, git_runner=_git_runner_for("f" * 40))
    identity_a = identity_from_packet(packet_a)
    identity_b = identity_from_packet(packet_b)
    assert sealed_run_id(identity_a) != sealed_run_id(identity_b)
    assert content_identity(identity_a) == content_identity(identity_b)
    with pytest.raises(SecondExecutionRefusedError):
        _execute(
            packet_b,
            fixture,
            ledger_root,
            git_runner=_git_runner_for("f" * 40),
            at_epoch=T0 + 1,
        )


def test_approval_missing_exit_6_before_consumption(tmp_path: Path, ledger_root: Path) -> None:
    fixture = write_valid_inputs(tmp_path)
    with pytest.raises(ApprovalInvalidError) as exc_info:
        _execute(_packet(fixture), fixture, ledger_root)
    assert exc_info.value.exit_code == 6
    assert L.read_ledger(ledger_root).records == ()


def test_execute_revalidates_packet_self_hash_before_ledger_access(
    tmp_path: Path, ledger_root: Path
) -> None:
    fixture = write_valid_inputs(tmp_path)
    packet = _packet(fixture)
    forged = packet.model_copy(update={"code_sha": "f" * 40})
    with pytest.raises(VerifiedInputsError, match="packet self-validation"):
        _execute(forged, fixture, ledger_root)
    assert not (ledger_root / L.LEDGER_FILENAME).exists()


def test_approval_packet_a_cannot_execute_packet_b(tmp_path: Path, ledger_root: Path) -> None:
    fixture = write_valid_inputs(tmp_path)
    packet_a = _packet(fixture)
    _approve(ledger_root, packet_a)
    changed = build_calendar_decision_artifact(
        decision="weekend-only-accepted",
        owner_decision_id="different-fixture-decision",
        decided_at=datetime(2026, 8, 24, 13, 0, tzinfo=UTC),
        rationale="A different typed calendar choice.",
    )
    fixture.calendar.write_text(changed.model_dump_json(indent=2) + "\n", encoding="utf-8")
    packet_b = _packet(fixture)

    with pytest.raises(ApprovalInvalidError):
        _execute(packet_b, fixture, ledger_root)
    assert not any(
        record.kind == L.KIND_CONSUMPTION for record in L.read_ledger(ledger_root).records
    )


def test_current_packet_must_equal_approved_packet(tmp_path: Path, ledger_root: Path) -> None:
    fixture = write_valid_inputs(tmp_path)
    packet = _packet(fixture)
    _approve(ledger_root, packet)
    changed = build_calendar_decision_artifact(
        decision="weekend-only-accepted",
        owner_decision_id="post-preflight-swap",
        decided_at=datetime(2026, 8, 24, 13, 0, tzinfo=UTC),
        rationale="A valid but unapproved replacement packet.",
    )
    fixture.calendar.write_text(changed.model_dump_json(indent=2) + "\n", encoding="utf-8")

    with pytest.raises(ApprovalInvalidError, match="current typed inputs"):
        _execute(packet, fixture, ledger_root)
    assert not any(
        record.kind == L.KIND_CONSUMPTION for record in L.read_ledger(ledger_root).records
    )


@pytest.mark.parametrize("movement", ["delete", "symlink"])
def test_manifest_deletion_or_symlink_replacement_after_preflight_refuses_without_consumption(
    tmp_path: Path, ledger_root: Path, movement: str
) -> None:
    fixture = write_valid_inputs(tmp_path)
    packet = _packet(fixture)
    _approve(ledger_root, packet)
    if movement == "delete":
        fixture.lane2_manifest.unlink()
    else:
        held = fixture.lane1_manifest.with_suffix(".held")
        fixture.lane1_manifest.rename(held)
        fixture.lane1_manifest.symlink_to(held)

    with pytest.raises(VerifiedInputsError):
        _execute(packet, fixture, ledger_root)
    assert not any(
        record.kind == L.KIND_CONSUMPTION for record in L.read_ledger(ledger_root).records
    )


def test_code_movement_after_approval_refuses_without_consumption(
    tmp_path: Path, ledger_root: Path
) -> None:
    fixture = write_valid_inputs(tmp_path)
    packet = _packet(fixture)
    _approve(ledger_root, packet)
    with pytest.raises(ApprovalInvalidError, match="current typed inputs"):
        _execute(
            packet,
            fixture,
            ledger_root,
            git_runner=_git_runner_for("f" * 40),
        )
    assert not any(
        record.kind == L.KIND_CONSUMPTION for record in L.read_ledger(ledger_root).records
    )


def test_protocol_movement_after_approval_refuses_without_consumption(
    tmp_path: Path, ledger_root: Path
) -> None:
    fixture = write_valid_inputs(tmp_path)
    packet = _packet(fixture)
    _approve(ledger_root, packet)
    protocol = fixture.paths.repo / "research_protocol.yaml"
    raw = protocol.read_text()
    protocol.write_text(raw.replace('owner: "architecture/leakage"', 'owner: "moved"', 1))
    with pytest.raises(ApprovalInvalidError, match="current typed inputs"):
        _execute(packet, fixture, ledger_root)
    assert not any(
        record.kind == L.KIND_CONSUMPTION for record in L.read_ledger(ledger_root).records
    )


def test_runner_version_is_cross_joined_before_consumption(
    tmp_path: Path, ledger_root: Path
) -> None:
    """Round-11 F8 form: the registry entry RESOLVED for the approved version
    must itself declare that version — a forged entry (correct lookup key,
    foreign declared machinery) is refused before consumption."""
    fixture = write_valid_inputs(tmp_path)
    runner = StubRunner()
    with _seeded_registry(runner):
        packet = _packet(fixture)
        _approve(ledger_root, packet)
        entry = vi.RUNNER_REGISTRY[RUNNER_VERSION]
        vi.RUNNER_REGISTRY[RUNNER_VERSION] = dataclass_replace(
            entry, runner_version="foreign-runner/1"
        )
        with pytest.raises(ApprovalInvalidError, match="runner version"):
            _execute(packet, fixture, ledger_root)
    assert runner.calls == 0
    assert not any(
        record.kind == L.KIND_CONSUMPTION for record in L.read_ledger(ledger_root).records
    )


# ---- round-11 (finding 8): runner IDENTITY is the registry, never a presented callable --
#
# Round-11 review fix: the approved runner machinery was a CALLER-ASSERTED
# STRING — any callable carrying .runner_version == the approved literal was
# compared on that attribute alone, then consumed and invoked. A foreign
# callable with the matching literal ran unapproved code after a durable
# CONSUMPTION append. Authority is now the module-level runner REGISTRY keyed
# by runner_version (seeded explicitly by the owning layer/test): the verified
# packet carries the implementation binding (sha256 of the registered
# implementation's code file bytes) recorded at approval time, and execution
# verifies the registry entry's CURRENT code hash equals the packet's binding
# before any consumption. A foreign callable with a matching string is never
# registered, so it is never authority: execute_sealed_run takes no callable
# parameter at all.


def test_foreign_callable_with_the_approved_version_literal_is_not_authority(
    tmp_path: Path, ledger_root: Path
) -> None:
    """Round-11 F8: the foreign callable carries the approved version literal
    and would happily run; it is never registered, so there is no seam through
    which it can be spent. The REGISTERED machinery is what executes — exactly
    once — and the foreign code runs zero times."""
    fixture = write_valid_inputs(tmp_path)
    registered = StubRunner()
    ran: list[bool] = []

    class ForeignRunner:
        runner_version = RUNNER_VERSION  # the approved literal, merely asserted

        def __call__(self, inputs: HeldVerifiedSealedInputs) -> str:
            ran.append(True)
            return "unapproved-machinery-ran"

    with _seeded_registry(registered):
        packet = _packet(fixture)
        _approve(ledger_root, packet)
        summary = _execute(packet, fixture, ledger_root)
    assert summary.runner_outcome == "sealed-run-complete"
    assert registered.calls == 1, "the registered implementation is the machinery that ran"
    assert registered.presented is not None
    assert ran == [], "a callable that merely asserts the version never runs"


def test_execution_without_registered_machinery_refuses_before_consumption(
    tmp_path: Path, ledger_root: Path
) -> None:
    """No machinery registered for the approved version: the packet cannot be
    rebuilt from current inputs (its own build refuses) and execution never
    reaches a consumption append."""
    fixture = write_valid_inputs(tmp_path)
    runner = StubRunner()
    with _seeded_registry(runner):
        packet = _packet(fixture)
        _approve(ledger_root, packet)
    vi.RUNNER_REGISTRY.clear()  # the machinery is gone before the spend
    with pytest.raises(VerifiedInputsError, match="no runner machinery is registered"):
        _execute(packet, fixture, ledger_root)
    assert runner.calls == 0
    assert not any(
        record.kind == L.KIND_CONSUMPTION for record in L.read_ledger(ledger_root).records
    )


def test_registered_runner_code_changed_since_approval_refuses(
    tmp_path: Path, ledger_root: Path
) -> None:
    """Round-11 F8: the approved packet binds the REGISTERED implementation's
    code-file sha256. When that file changes between approval and execution
    the registry entry's CURRENT code hash no longer equals the packet's
    binding — refusal before any consumption, naming both hashes."""
    fixture = write_valid_inputs(tmp_path)
    module_dir = tmp_path / "runner-machinery"
    module_dir.mkdir()
    module_path = module_dir / "approved_runner.py"
    module_path.write_text(
        "RUNNER_VERSION_LITERAL = 'm4-g4-runner/1'\n"
        "\n"
        "\n"
        "class ApprovedRunner:\n"
        "    runner_version = RUNNER_VERSION_LITERAL\n"
        "\n"
        "    def __init__(self):\n"
        "        self.calls = 0\n"
        "\n"
        "    def __call__(self, inputs):\n"
        "        self.calls += 1\n"
        "        return 'approved-machinery-ran'\n",
        encoding="utf-8",
    )
    spec = importlib.util.spec_from_file_location("approved_runner", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # inspect.getsourcefile resolves via sys.modules
    spec.loader.exec_module(module)
    runner = module.ApprovedRunner()
    with _seeded_registry(runner):
        packet = _packet(fixture)
        _approve(ledger_root, packet)
        assert (
            packet.runner_implementation_sha256
            == hashlib.sha256(module_path.read_bytes()).hexdigest()
        )
        # the machinery implementation CHANGES after approval
        module_path.write_text(
            module_path.read_text(encoding="utf-8") + "\n# post-approval machinery edit\n",
            encoding="utf-8",
        )
        with pytest.raises(ApprovalInvalidError, match="machinery changed since approval"):
            _execute(packet, fixture, ledger_root)
    assert runner.calls == 0
    assert not any(
        record.kind == L.KIND_CONSUMPTION for record in L.read_ledger(ledger_root).records
    )


def test_interleaved_consumption_after_input_verification_is_refused_at_effect_boundary(
    tmp_path: Path,
    ledger_root: Path,
    registered_runner: StubRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = write_valid_inputs(tmp_path)
    packet = _packet(fixture)
    identity = identity_from_packet(packet)
    _approve(ledger_root, packet)
    real_verify = g4_seal.verify_sealed_inputs

    def verify_then_interleave(*args: object, **kwargs: object):
        held = real_verify(*args, **kwargs)
        L.append_consumption(
            ledger_root,
            identity,
            reason="hostile interleaving",
            at_epoch=T0 - 1,
        )
        return held

    monkeypatch.setattr(g4_seal, "verify_sealed_inputs", verify_then_interleave)
    with pytest.raises(SecondExecutionRefusedError):
        _execute(packet, fixture, ledger_root)
    assert registered_runner.calls == 0
    assert (
        len(
            [
                record
                for record in L.read_ledger(ledger_root).records
                if record.kind == L.KIND_CONSUMPTION
            ]
        )
        == 1
    )


def test_crash_after_consumption_is_durable_unknown_never_rerun(
    tmp_path: Path, ledger_root: Path
) -> None:
    fixture = write_valid_inputs(tmp_path)

    def crash(_inputs: HeldVerifiedSealedInputs) -> str:
        raise RuntimeError("crash mid-seal")

    runner = StubRunner(crash)
    with _seeded_registry(runner):
        packet = _packet(fixture)
        _approve(ledger_root, packet)
        with pytest.raises(RuntimeError):
            _execute(packet, fixture, ledger_root)
    assert (
        len(
            [
                record
                for record in L.read_ledger(ledger_root).records
                if record.kind == L.KIND_CONSUMPTION
            ]
        )
        == 1
    )
    with pytest.raises(SecondExecutionRefusedError):
        _execute(packet, fixture, ledger_root, at_epoch=T0 + 1)


def test_forged_consumption_stored_ids_refused_as_corrupt(
    tmp_path: Path, ledger_root: Path
) -> None:
    fixture = write_valid_inputs(tmp_path)
    packet = _packet(fixture)
    identity = identity_from_packet(packet)
    _approve(ledger_root, packet)
    forged = L.LedgerRecord(
        kind=L.KIND_CONSUMPTION,
        identity=identity,
        reason="forged stored ids",
        at_epoch=T0,
        sealed_run_id="0" * 64,
        content_identity="0" * 64,
        prev_record_sha256=L.read_ledger(ledger_root).tail_hash,
        record_sha256="",
    )
    forged = forged.model_copy(update={"record_sha256": L._record_hash(forged)})
    L.append_record(ledger_root, forged)
    with pytest.raises(L.LedgerCorruptError, match="stored ids disagree"):
        _execute(packet, fixture, ledger_root)


def test_approval_tampered_payload_exit_6(tmp_path: Path, ledger_root: Path) -> None:
    fixture = write_valid_inputs(tmp_path)
    packet = _packet(fixture)
    real = identity_from_packet(packet)
    forged_identity = real.model_copy(update={"code_sha": "f" * 40})
    forged = L.LedgerRecord(
        kind=L.KIND_APPROVAL,
        identity=forged_identity,
        sealed_run_id=sealed_run_id(real),
        content_identity=content_identity(real),
        reason="tampered approval payload",
        at_epoch=T0,
        prev_record_sha256=L.GENESIS_PREV,
    )
    L.append_record(ledger_root, forged)
    with pytest.raises(ApprovalInvalidError):
        _execute(packet, fixture, ledger_root)


def test_execute_cli_refuses_before_reading_or_consuming(
    ledger_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sentinel = ledger_root / "sentinel"
    sentinel.write_text("unchanged\n")
    assert g4_seal.cmd_execute(["--ledger-root", str(ledger_root), "--reason", "attempt"]) == 2
    assert sentinel.read_text() == "unchanged\n"
    assert not (ledger_root / L.LEDGER_FILENAME).exists()
    assert "EXECUTE REFUSED" in capsys.readouterr().err


# ---- round-10 P1 (finding 4): the machinery binding is the CALLABLE, not the file --
#
# Round-10 review fix (2026-08-25): the packet's machinery binding was the
# sha256 of the implementation's whole CODE FILE, so (a) a ForeignRunner
# defined in the SAME module as the approved one shared the hash exactly and
# executed under a durable consumption once registered under the approved
# version, and (b) a configured instance of the approved class whose
# configuration changes authority behavior was indistinguishable from the
# approved configuration. The binding is now the four-tuple
# (version, qualname, file_sha256, config_digest), re-derived from the
# registry entry at execution and compared for exact equality on all four.


class _SameFileForeignRunner:
    """A foreign callable in the SAME module as the approved StubRunner."""

    runner_version = RUNNER_VERSION

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, inputs: HeldVerifiedSealedInputs) -> str:
        self.calls += 1
        return "same-file-foreign-machinery-ran"


class _ConfiguredRunner:
    """The approved class shape, configured at construction: the mode is the
    authority behavior a configuration change would alter."""

    runner_version = RUNNER_VERSION

    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.calls = 0

    def __call__(self, inputs: HeldVerifiedSealedInputs) -> str:
        self.calls += 1
        return f"configured-machinery-ran:{self.mode}"


def _no_consumption(ledger_root: Path) -> bool:
    return not any(
        record.kind == L.KIND_CONSUMPTION for record in L.read_ledger(ledger_root).records
    )


def test_same_file_foreign_runner_under_the_approved_version_is_refused(
    tmp_path: Path, ledger_root: Path
) -> None:
    """Round-10 F4 attack (a): StubRunner (approved) and
    _SameFileForeignRunner live in ONE module — this test file — so the
    file-hash-only binding saw an exact match and the foreign machinery
    executed under a durable consumption. The qualified name now changes the
    packet's machinery binding, so the re-registered foreign callable no
    longer matches the owner-approved packet."""
    fixture = write_valid_inputs(tmp_path)
    approved = StubRunner()
    foreign = _SameFileForeignRunner()
    assert vi.runner_implementation_sha256(approved) == vi.runner_implementation_sha256(foreign), (
        "the attack's premise: one module, one code-file hash"
    )

    with _seeded_registry(approved):
        packet = _packet(fixture)
        _approve(ledger_root, packet)
    with _seeded_registry(foreign):  # same file, same version, FOREIGN callable
        with pytest.raises(ApprovalInvalidError, match=r"qualified name|current typed inputs"):
            _execute(packet, fixture, ledger_root)
    assert foreign.calls == 0, "the foreign callable never runs"
    assert approved.calls == 0
    assert _no_consumption(ledger_root)


def test_differently_configured_machinery_reregistration_is_refused(
    tmp_path: Path, ledger_root: Path
) -> None:
    """Round-10 F4 attack (b): the approved CLASS registered a second time
    with a DIFFERENT configuration digest under the same version — same
    class file, same qualified name, different authority behavior. The
    packet's machinery binding now includes the registration-time
    configuration digest, so the re-registered configuration no longer
    matches the approved packet."""
    fixture = write_valid_inputs(tmp_path)
    safe = _ConfiguredRunner("safe-mode")
    dangerous = _ConfiguredRunner("dangerous-mode")
    assert vi.runner_implementation_qualname(safe) == vi.runner_implementation_qualname(
        dangerous
    ), "the attack's premise: one class, one qualified name, one code file"

    with _seeded_registry(safe, config_digest=hashlib.sha256(b"runner-config-safe").hexdigest()):
        packet = _packet(fixture)
        _approve(ledger_root, packet)
    with _seeded_registry(
        dangerous, config_digest=hashlib.sha256(b"runner-config-dangerous").hexdigest()
    ):
        with pytest.raises(ApprovalInvalidError):
            _execute(packet, fixture, ledger_root)
    assert dangerous.calls == 0, "the differently-configured machinery never runs"
    assert safe.calls == 0
    assert _no_consumption(ledger_root)


def test_execution_rederives_the_qualname_from_the_registry_entry(
    tmp_path: Path,
    ledger_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round-10 F4 defense layer: with the current-inputs rebuild bypassed
    (it already binds the registry), execution must STILL re-derive the
    qualified name from the registry entry and refuse a same-file foreign
    callable whose code-file hash matches the approved binding exactly."""
    fixture = write_valid_inputs(tmp_path)
    approved = StubRunner()
    foreign = _SameFileForeignRunner()
    with _seeded_registry(approved):
        packet = _packet(fixture)
        held = verify_sealed_inputs(fixture.paths, git_runner=clean_git_runner)
        _approve(ledger_root, packet)
    with _seeded_registry(foreign):
        monkeypatch.setattr(g4_seal, "verify_sealed_inputs", lambda *a, **k: held)
        with pytest.raises(ApprovalInvalidError, match="qualified name"):
            _execute(packet, fixture, ledger_root)
    assert foreign.calls == 0
    assert _no_consumption(ledger_root)


def test_execution_rederives_the_config_digest_from_the_registry_entry(
    tmp_path: Path,
    ledger_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round-10 F4 defense layer: a forged registry entry whose
    configuration digest differs from the approved binding is refused at
    execution even when version, qualified name, and code-file hash all
    match — the configuration the owner approved is part of the machinery."""
    fixture = write_valid_inputs(tmp_path)
    runner = StubRunner()
    with _seeded_registry(
        runner, config_digest=hashlib.sha256(b"runner-config-approved").hexdigest()
    ):
        packet = _packet(fixture)
        held = verify_sealed_inputs(fixture.paths, git_runner=clean_git_runner)
        _approve(ledger_root, packet)
    entry = vi.RUNNER_REGISTRY[RUNNER_VERSION]
    vi.RUNNER_REGISTRY[RUNNER_VERSION] = dataclass_replace(
        entry, config_digest=hashlib.sha256(b"runner-config-forged").hexdigest()
    )
    monkeypatch.setattr(g4_seal, "verify_sealed_inputs", lambda *a, **k: held)
    with pytest.raises(ApprovalInvalidError, match="configuration digest"):
        _execute(packet, fixture, ledger_root)
    assert runner.calls == 0
    assert _no_consumption(ledger_root)
