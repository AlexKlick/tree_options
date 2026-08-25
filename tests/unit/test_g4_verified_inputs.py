"""Typed, no-follow G4 input verification and immutable packet binding."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

import pytest

from tests.fixtures import cboe_eod_rows as cboe_fx
from tree_options.data.cboe_eod import (
    REAL_MANIFEST_DOMAIN,
    RealOptionsManifest,
    build_real_options_manifest,
    parse_cboe_eod_csv,
)
from tree_options.data.digest import canonical_bytes
from tree_options.data.massive_manifest import (
    CAPTURE_MANIFEST_FILENAME,
    MASSIVE_MANIFEST_DOMAIN,
    MassiveCaptureManifest,
    build_massive_capture_manifest,
)
from tree_options.data.real_overlay import build_real_overlay
from tree_options.protocol.loader import load_protocol_bytes, protocol_hash
from tree_options.seal import input_custody, verified_inputs
from tree_options.seal.identity import RUNNER_VERSION
from tree_options.seal.verified_inputs import (
    CALENDAR_DECISION_DOMAIN,
    CALENDAR_DECISION_SCHEMA_VERSION,
    EXPECTED_MASSIVE_CAPTURE_VERSION,
    HeldVerifiedSealedInputs,
    SealedInputPaths,
    VerifiedInputsError,
    VerifiedSealedInputs,
    build_calendar_decision_artifact,
    verify_sealed_inputs,
)

SOURCE_REPO = Path(__file__).resolve().parents[2]
CODE_SHA = "9" * 40


class _FakeSealedRunner:
    """The registry-seeded fake machinery these packet-building tests bind.

    Round-11 finding 8: a verified packet carries the sha256 of the
    REGISTERED runner implementation's code file, so the registry must hold
    an entry before any packet can be built."""

    runner_version = RUNNER_VERSION

    def __call__(self, inputs: HeldVerifiedSealedInputs) -> str:
        return "fake-sealed-run-complete"


@pytest.fixture(autouse=True)
def _registered_fake_runner() -> Iterator[None]:
    verified_inputs.RUNNER_REGISTRY.clear()
    verified_inputs.register_runner(_FakeSealedRunner())
    try:
        yield
    finally:
        verified_inputs.RUNNER_REGISTRY.clear()


def _completed(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


def clean_git_runner(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
    if "rev-parse" in argv:
        return _completed(CODE_SHA + "\n")
    if "status" in argv:
        return _completed("")
    raise AssertionError(f"unexpected git call: {argv}")


@dataclass(frozen=True)
class InputFixture:
    paths: SealedInputPaths
    lane1_source: Path
    lane1_manifest: Path
    lane2_manifest: Path
    lane2_payload: Path
    calendar: Path


def _copy_repo_inputs(target: Path) -> Path:
    for relative in (
        Path("research_protocol.yaml"),
        Path("data/g4/sealed-criteria.json"),
        Path("docs/m4-g4-sealed-gate-plan.md"),
    ):
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(SOURCE_REPO / relative, destination)
    return target


def write_valid_inputs(tmp_path: Path) -> InputFixture:
    repo = _copy_repo_inputs(tmp_path / "repo")

    lane1 = tmp_path / "lane1"
    lane1.mkdir()
    source = cboe_fx.write_csv(lane1 / "spy.csv", cboe_fx.SPY_MAIN_ROWS)
    parsed = parse_cboe_eod_csv(source)
    overlay = build_real_overlay(parsed)
    manifest1 = build_real_options_manifest(parsed, overlay=overlay)
    manifest1_path = lane1 / "capture-manifest.json"
    manifest1_path.write_text(manifest1.model_dump_json(indent=2) + "\n", encoding="utf-8")

    lane2 = tmp_path / "lane2"
    masters = lane2 / "masters"
    bars = lane2 / "bars"
    masters.mkdir(parents=True)
    bars.mkdir()
    master = masters / "SPY_2025-03-14.json"
    master.write_text(
        '{"capture_version":"m4b-capture/1","pages":[{"results":[]}]}\n',
        encoding="utf-8",
    )
    bar = bars / "O_SPY250417C00560000.json"
    bar.write_text('{"resultsCount":1,"results":[{"c":40.08}]}\n', encoding="utf-8")
    (lane2 / "spot_proxy.json").write_text('{"SPY":{"2025-03-14":"587.5"}}\n', encoding="utf-8")
    manifest2 = build_massive_capture_manifest(
        lane2,
        capture_version=EXPECTED_MASSIVE_CAPTURE_VERSION,
        budget_limit=45,
        requests_charged=2,
        client_stats={"requests": 2},
        masters=(
            {
                "underlying": "SPY",
                "as_of": "2025-03-14",
                "pages": 1,
                "rows": 0,
                "complete": True,
                "truncated": False,
                "error": None,
                "file": master.name,
            },
        ),
        bars=(bar.name,),
        spot_proxy={"SPY": {"2025-03-14": "587.5"}},
        notes=("hermetic G4 verifier fixture",),
    )
    manifest2_path = lane2 / CAPTURE_MANIFEST_FILENAME
    manifest2_path.write_text(manifest2.model_dump_json(indent=2) + "\n", encoding="utf-8")

    calendar = tmp_path / "calendar-decision.json"
    artifact = build_calendar_decision_artifact(
        decision="repo-generated-calendar",
        owner_decision_id="fixture-owner-decision",
        decided_at=datetime(2026, 8, 24, 12, 0, tzinfo=UTC),
        rationale="Hermetic typed decision for verifier tests only.",
    )
    calendar.write_text(artifact.model_dump_json(indent=2) + "\n", encoding="utf-8")

    return InputFixture(
        paths=SealedInputPaths(
            repo=repo,
            lane1_manifest=manifest1_path,
            lane1_source=source,
            lane2_manifest=manifest2_path,
            calendar_decision_artifact=calendar,
        ),
        lane1_source=source,
        lane1_manifest=manifest1_path,
        lane2_manifest=manifest2_path,
        lane2_payload=master,
        calendar=calendar,
    )


def test_verified_packet_comes_only_from_real_typed_verifiers(tmp_path: Path) -> None:
    fixture = write_valid_inputs(tmp_path)
    held = verify_sealed_inputs(fixture.paths, git_runner=clean_git_runner)

    packet = held.packet
    assert packet.code_sha == CODE_SHA
    assert packet.runner_version == RUNNER_VERSION
    assert packet.runner_implementation_sha256 == (
        verified_inputs.RUNNER_REGISTRY[RUNNER_VERSION].implementation_sha256
    ), "round-11 F8: the packet binds the registered machinery implementation"
    assert packet.lane1_manifest.manifest_version == "m4/1"
    assert packet.lane2_manifest.manifest_version == "m4b-manifest/1"
    assert packet.lane1_manifest.raw_sha256 == sha256(held.lane1_manifest_bytes).hexdigest()
    assert packet.lane2_manifest.raw_sha256 == sha256(held.lane2_manifest_bytes).hexdigest()
    assert packet.lane1_manifest.referenced_payload_set_hash != packet.lane1_manifest.raw_sha256
    assert packet.lane2_manifest.referenced_payload_set_hash != packet.lane2_manifest.raw_sha256
    assert (
        packet.calendar_decision_artifact_sha256
        == sha256(held.calendar_decision_artifact_bytes).hexdigest()
    )
    assert (
        packet.criteria_source_document_sha256
        == sha256(held.criteria_source_document_bytes).hexdigest()
    )
    assert held.calendar_decision.schema_version == CALENDAR_DECISION_SCHEMA_VERSION


def test_protocol_hash_comes_from_the_validated_protocol_model(tmp_path: Path) -> None:
    fixture = write_valid_inputs(tmp_path)
    held = verify_sealed_inputs(fixture.paths, git_runner=clean_git_runner)
    assert held.packet.protocol_hash == protocol_hash(load_protocol_bytes(held.protocol_bytes))


def test_checkout_movement_during_verification_refuses(tmp_path: Path) -> None:
    fixture = write_valid_inputs(tmp_path)
    heads = iter((CODE_SHA, "f" * 40))

    def moving_git_runner(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if "rev-parse" in argv:
            return _completed(next(heads) + "\n")
        if "status" in argv:
            return _completed("")
        raise AssertionError(f"unexpected git call: {argv}")

    with pytest.raises(VerifiedInputsError, match="checkout moved"):
        verify_sealed_inputs(fixture.paths, git_runner=moving_git_runner)


def test_packet_self_hash_rejects_caller_tamper(tmp_path: Path) -> None:
    fixture = write_valid_inputs(tmp_path)
    packet = verify_sealed_inputs(fixture.paths, git_runner=clean_git_runner).packet
    payload = packet.model_dump()
    payload["code_sha"] = "f" * 40
    with pytest.raises(Exception, match="packet_content_sha256"):
        VerifiedSealedInputs.model_validate(payload)


def test_packet_binds_the_registered_runner_implementation_sha(tmp_path: Path) -> None:
    """Round-11 F8: the machinery binding is the REGISTERED implementation's
    code-file hash — recomputing it from the registered implementation
    reproduces the packet field exactly."""
    fixture = write_valid_inputs(tmp_path)
    packet = verify_sealed_inputs(fixture.paths, git_runner=clean_git_runner).packet
    entry = verified_inputs.RUNNER_REGISTRY[RUNNER_VERSION]
    assert packet.runner_implementation_sha256 == entry.implementation_sha256
    assert packet.runner_implementation_sha256 == verified_inputs.runner_implementation_sha256(
        entry.implementation
    )


def test_no_registered_runner_machinery_refuses_the_packet(tmp_path: Path) -> None:
    """Round-11 F8: nothing registered means no packet — the builder refuses
    to attest a packet that cannot name the machinery that will consume it."""
    fixture = write_valid_inputs(tmp_path)
    verified_inputs.RUNNER_REGISTRY.clear()
    with pytest.raises(VerifiedInputsError, match="no runner machinery is registered"):
        verify_sealed_inputs(fixture.paths, git_runner=clean_git_runner)


@pytest.mark.parametrize("lane", ["lane1", "lane2"])
def test_readable_arbitrary_json_is_not_a_lane_manifest(tmp_path: Path, lane: str) -> None:
    fixture = write_valid_inputs(tmp_path)
    target = fixture.lane1_manifest if lane == "lane1" else fixture.lane2_manifest
    target.write_text('{"lane": 1}\n', encoding="utf-8")

    with pytest.raises(VerifiedInputsError, match=lane):
        verify_sealed_inputs(fixture.paths, git_runner=clean_git_runner)


@pytest.mark.parametrize("lane", ["lane1", "lane2"])
def test_correctly_self_hashed_foreign_manifest_version_refuses(tmp_path: Path, lane: str) -> None:
    fixture = write_valid_inputs(tmp_path)
    target = fixture.lane1_manifest if lane == "lane1" else fixture.lane2_manifest
    payload = json.loads(target.read_text())
    payload["schema_version"] = "foreign/99"
    payload["content_sha256"] = ""
    domain = REAL_MANIFEST_DOMAIN if lane == "lane1" else MASSIVE_MANIFEST_DOMAIN
    model_type = RealOptionsManifest if lane == "lane1" else MassiveCaptureManifest
    foreign = model_type.model_validate(payload)
    payload["content_sha256"] = sha256(domain + canonical_bytes(foreign)).hexdigest()
    target.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    with pytest.raises(VerifiedInputsError, match=r"version|schema"):
        verify_sealed_inputs(fixture.paths, git_runner=clean_git_runner)


def test_correctly_self_hashed_foreign_massive_capture_version_refuses(
    tmp_path: Path,
) -> None:
    fixture = write_valid_inputs(tmp_path)
    payload = json.loads(fixture.lane2_manifest.read_text())
    payload["capture_version"] = "foreign-capture/99"
    payload["content_sha256"] = ""
    foreign = MassiveCaptureManifest.model_validate(payload)
    payload["content_sha256"] = sha256(
        MASSIVE_MANIFEST_DOMAIN + canonical_bytes(foreign)
    ).hexdigest()
    fixture.lane2_manifest.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    with pytest.raises(VerifiedInputsError, match="capture_version"):
        verify_sealed_inputs(fixture.paths, git_runner=clean_git_runner)


@pytest.mark.parametrize("which", ["lane1", "lane2"])
def test_missing_or_tampered_referenced_payload_refuses(tmp_path: Path, which: str) -> None:
    fixture = write_valid_inputs(tmp_path)
    if which == "lane1":
        fixture.lane1_source.write_bytes(fixture.lane1_source.read_bytes() + b"# tampered\n")
    else:
        fixture.lane2_payload.unlink()

    with pytest.raises(VerifiedInputsError, match=which):
        verify_sealed_inputs(fixture.paths, git_runner=clean_git_runner)


def test_stale_criteria_source_document_sha_refuses(tmp_path: Path) -> None:
    fixture = write_valid_inputs(tmp_path)
    source = fixture.paths.repo / "docs/m4-g4-sealed-gate-plan.md"
    source.write_bytes(source.read_bytes() + b"\nstale-source-probe\n")

    with pytest.raises(VerifiedInputsError, match=r"criteria.*source"):
        verify_sealed_inputs(fixture.paths, git_runner=clean_git_runner)


def test_criteria_identifiers_are_exact_and_ordered(tmp_path: Path) -> None:
    fixture = write_valid_inputs(tmp_path)
    criteria_path = fixture.paths.repo / "data/g4/sealed-criteria.json"
    payload = json.loads(criteria_path.read_text())
    payload["criteria"][0]["id"] = "caller_substituted_criterion"
    criteria_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    with pytest.raises(VerifiedInputsError, match="criterion identifiers"):
        verify_sealed_inputs(fixture.paths, git_runner=clean_git_runner)


def test_massive_unlisted_json_is_reconciled_from_held_directory(tmp_path: Path) -> None:
    fixture = write_valid_inputs(tmp_path)
    extra = fixture.lane2_manifest.parent / "bars" / "unlisted.json"
    extra.write_text("{}\n", encoding="utf-8")

    with pytest.raises(VerifiedInputsError, match="unlisted"):
        verify_sealed_inputs(fixture.paths, git_runner=clean_git_runner)


def test_arbitrary_calendar_text_or_json_refuses(tmp_path: Path) -> None:
    fixture = write_valid_inputs(tmp_path)
    fixture.calendar.write_text('{"decision":"whatever caller wants"}\n', encoding="utf-8")

    with pytest.raises(VerifiedInputsError, match="calendar"):
        verify_sealed_inputs(fixture.paths, git_runner=clean_git_runner)


def test_correctly_self_hashed_foreign_calendar_decision_refuses(tmp_path: Path) -> None:
    fixture = write_valid_inputs(tmp_path)
    payload = json.loads(fixture.calendar.read_text())
    payload["decision"] = "caller-invented-calendar"
    payload["content_sha256"] = ""
    foreign = build_calendar_decision_artifact(
        decision="repo-generated-calendar",
        owner_decision_id=payload["owner_decision_id"],
        decided_at=datetime.fromisoformat(payload["decided_at"].replace("Z", "+00:00")),
        rationale=payload["rationale"],
    ).model_copy(update={"decision": payload["decision"], "content_sha256": ""})
    payload["content_sha256"] = sha256(
        CALENDAR_DECISION_DOMAIN + canonical_bytes(foreign)
    ).hexdigest()
    fixture.calendar.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    with pytest.raises(VerifiedInputsError, match="calendar"):
        verify_sealed_inputs(fixture.paths, git_runner=clean_git_runner)


def test_calendar_decision_content_hash_rejects_typed_body_tamper(tmp_path: Path) -> None:
    fixture = write_valid_inputs(tmp_path)
    payload = json.loads(fixture.calendar.read_text())
    payload["rationale"] = "tampered without owner artifact re-hash"
    fixture.calendar.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    with pytest.raises(VerifiedInputsError, match="content_sha256"):
        verify_sealed_inputs(fixture.paths, git_runner=clean_git_runner)


def test_symlink_manifest_is_never_followed(tmp_path: Path) -> None:
    fixture = write_valid_inputs(tmp_path)
    original = fixture.lane1_manifest.with_suffix(".held")
    fixture.lane1_manifest.rename(original)
    fixture.lane1_manifest.symlink_to(original)

    with pytest.raises(VerifiedInputsError, match=r"symlink|no-follow"):
        verify_sealed_inputs(fixture.paths, git_runner=clean_git_runner)


def test_intermediate_manifest_directory_symlink_is_never_followed(tmp_path: Path) -> None:
    fixture = write_valid_inputs(tmp_path)
    linked = tmp_path / "linked-lane1"
    linked.symlink_to(fixture.lane1_manifest.parent, target_is_directory=True)
    paths = replace(
        fixture.paths,
        lane1_manifest=linked / fixture.lane1_manifest.name,
    )

    with pytest.raises(VerifiedInputsError, match=r"symlink|no-follow"):
        verify_sealed_inputs(paths, git_runner=clean_git_runner)


def test_hard_linked_manifest_is_refused(tmp_path: Path) -> None:
    fixture = write_valid_inputs(tmp_path)
    original = fixture.lane1_manifest.with_suffix(".held")
    fixture.lane1_manifest.rename(original)
    os.link(original, fixture.lane1_manifest)

    with pytest.raises(VerifiedInputsError, match="link count"):
        verify_sealed_inputs(fixture.paths, git_runner=clean_git_runner)


def test_in_place_rewrite_during_single_read_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "single-read.json"
    target.write_bytes(b"original-bytes\n")
    real_read = input_custody._read_all

    def read_then_rewrite(fd: int) -> bytes:
        raw = real_read(fd)
        target.write_bytes(b"changed-longer-bytes\n")
        return raw

    monkeypatch.setattr(input_custody, "_read_all", read_then_rewrite)
    with pytest.raises(VerifiedInputsError, match="changed while"):
        input_custody.read_file_once(target, component="probe", purpose="probe file")


# ---- round-11 (finding 10): the entry-set snapshot must still be TRUE at exit -------
#
# Round-11 review fix: _verify_lane2 captured json_file_set() once and handed
# the frozen set to the Massive verifier; the custody context exit verified
# only directory-inode reachability. A file planted AFTER the snapshot
# (bars/unlisted.json) was invisible to the verifier, so the packet attested a
# directory state that was already false at return. The exit boundary now
# re-scans the held directory and requires EXACT equality with the snapshot
# the verifier consumed; divergence is a corruption-class refusal.


def test_capture_entry_set_changed_after_the_verifier_snapshot_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = write_valid_inputs(tmp_path)
    real_verify = verified_inputs.verify_massive_capture_manifest

    def verify_then_plant_unlisted(*args: object, **kwargs: object) -> None:
        real_verify(*args, **kwargs)  # the verifier consumed the frozen snapshot
        # the window: snapshot consumed, packet not yet returned
        (fixture.lane2_manifest.parent / "bars" / "unlisted.json").write_text(
            "{}\n", encoding="utf-8"
        )

    monkeypatch.setattr(
        verified_inputs, "verify_massive_capture_manifest", verify_then_plant_unlisted
    )
    with pytest.raises(VerifiedInputsError, match="entry set"):
        verify_sealed_inputs(fixture.paths, git_runner=clean_git_runner)
