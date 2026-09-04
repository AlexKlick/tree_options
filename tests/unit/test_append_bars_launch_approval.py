"""scripts/append_bars_launch_approval.py: the owner's approval act, runnable.

(window-A extension continuation, 2026-09-04) the continuation runs under a
NEW BARS_LAUNCH_APPROVAL record binding the live 0.2.2 protocol hash, the
new work manifest's RAW FILE sha256 (exactly what the launcher's authority
join compares), the verified census's content hash, and the ratified
amendment packet's sha. Every binding is computed from the bytes on disk
in the invoking run — nothing is taken on the caller's word — and the
append is the library's hash-chained, flock-held write.

The authority ledger lives under scratch roots in the repo's gitignored
artifacts/ (never /tmp — the ledger refuses it).
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from tests.fixtures.bars_sample import (  # noqa: E402
    T0,
    census_bytes,
    write_021_protocol,
    write_022_protocol,
    write_bars_capture,
    write_capture_manifest,
)
from tree_options.data.bars_manifest import read_bars_ledger  # noqa: E402
from tree_options.protocol.loader import load_protocol, protocol_hash  # noqa: E402

COMMITTED_PROFILE = REPO_ROOT / "data" / "bars" / "selection-profile.json"


@pytest.fixture()
def scratch_root() -> Iterator[Path]:
    root = REPO_ROOT / "artifacts" / "ext1-approval-tests" / uuid.uuid4().hex
    root.mkdir(parents=True, exist_ok=True)
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture()
def tuple_bundle(tmp_path: Path) -> dict[str, Path]:
    """A coherent (protocol, packet, census, work manifest) tuple built
    through the repo's own models — the exact inputs an approval binds."""
    from tree_options.data.bars_manifest import build_bars_work_manifest, load_selection_profile

    capture_dir = write_bars_capture(tmp_path / "capture")
    manifest_path = write_capture_manifest(capture_dir, capture_dir / "capture_manifest.json")
    census_path = tmp_path / "census.json"
    census_path.write_bytes(census_bytes(manifest_path.read_bytes()))
    work = build_bars_work_manifest(
        capture_dir,
        profile=load_selection_profile(COMMITTED_PROFILE),
        capture_manifest=manifest_path,
        budget_limit=45,
    )
    work_path = tmp_path / "work-manifest.json"
    work_path.write_text(work.model_dump_json(indent=2) + "\n", encoding="utf-8")
    packet_path = tmp_path / "amendment-packet.json"
    packet_path.write_text(
        json.dumps({"landed": True, "proposed_version": "0.2.2"}, sort_keys=True),
        encoding="utf-8",
    )
    protocol_path = write_022_protocol(tmp_path / "protocol-0.2.2.yaml")
    return {
        "capture_dir": capture_dir,
        "capture_manifest": manifest_path,
        "census": census_path,
        "work_manifest": work_path,
        "packet": packet_path,
        "protocol": protocol_path,
    }


def approval_main(argv: list[str]) -> int:
    """Load scripts/append_bars_launch_approval.py by path (the established
    script-loading pattern) and run its main."""
    spec = importlib.util.spec_from_file_location(
        "append_bars_launch_approval", REPO_ROOT / "scripts" / "append_bars_launch_approval.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("append_bars_launch_approval", module)
    spec.loader.exec_module(module)
    return module.main(argv)


def _argv(bundle: dict[str, Path], authority: Path, **overrides: str) -> list[str]:
    fields = dict(
        protocol=str(bundle["protocol"]),
        amendment_packet=str(bundle["packet"]),
        census=str(bundle["census"]),
        work_manifest=str(bundle["work_manifest"]),
    )
    fields.update(overrides)
    argv = [
        "--protocol",
        fields["protocol"],
        "--amendment-packet",
        fields["amendment_packet"],
        "--census",
        fields["census"],
        "--work-manifest",
        fields["work_manifest"],
        "--reason",
        fields.get("reason", "owner approval fixture"),
        "--authority-root",
        str(authority),
        "--at-epoch",
        str(T0),
    ]
    return argv


def _expected_bindings(bundle: dict[str, Path]) -> dict[str, str]:
    census_sha = str(json.loads(bundle["census"].read_text(encoding="utf-8"))["content_sha256"])
    return {
        "protocol_hash": protocol_hash(load_protocol(bundle["protocol"])),
        "amendment_packet_sha256": hashlib.sha256(bundle["packet"].read_bytes()).hexdigest(),
        "census_sha256": census_sha,
        # the record binds the RAW FILE sha256 — the launcher's join compares
        "work_manifest_sha256": hashlib.sha256(bundle["work_manifest"].read_bytes()).hexdigest(),
    }


def test_appends_exactly_one_record_binding_the_verified_bytes(
    tuple_bundle: dict[str, Path], scratch_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    authority = scratch_root / "bars-authority"
    assert approval_main(_argv(tuple_bundle, authority)) == 0
    view = read_bars_ledger(authority)
    assert len(view.records) == 1
    record = view.records[0]
    assert record.kind == "BARS_LAUNCH_APPROVAL"
    expected = _expected_bindings(tuple_bundle)
    for field, value in expected.items():
        assert getattr(record, field) == value, field
    out = capsys.readouterr().out
    assert record.record_sha256 in out  # the printed digest IS the appended line
    assert expected["work_manifest_sha256"][:12] in out


def test_a_duplicate_tuple_refuses_and_appends_nothing(
    tuple_bundle: dict[str, Path], scratch_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    authority = scratch_root / "bars-authority"
    assert approval_main(_argv(tuple_bundle, authority)) == 0
    assert approval_main(_argv(tuple_bundle, authority, reason="second attempt")) == 3
    assert "already grants this tuple" in capsys.readouterr().err
    view = read_bars_ledger(authority)
    assert len(view.records) == 1  # exactly the first append stands


def test_a_new_work_manifest_under_the_same_protocol_appends(
    tuple_bundle: dict[str, Path], scratch_root: Path, tmp_path: Path
) -> None:
    """The two-cycle continuation shape: cycle 2 approves a NEW manifest
    under the same live protocol — a legal second record, never a tuple
    duplicate."""
    from tree_options.data.bars_manifest import (
        build_bars_work_manifest,
        load_selection_profile,
    )

    authority = scratch_root / "bars-authority"
    assert approval_main(_argv(tuple_bundle, authority)) == 0
    # a genuinely different, self-consistent manifest over the same capture
    # (a different budget rail -> a different content hash and file bytes)
    second_model = build_bars_work_manifest(
        tuple_bundle["capture_dir"],
        profile=load_selection_profile(COMMITTED_PROFILE),
        capture_manifest=tuple_bundle["capture_manifest"],
        budget_limit=46,
    )
    second_path = tmp_path / "work-manifest-cycle-2.json"
    second_path.write_text(second_model.model_dump_json(indent=2) + "\n", encoding="utf-8")
    bundle2 = dict(tuple_bundle)
    bundle2["work_manifest"] = second_path
    assert approval_main(_argv(bundle2, authority, reason="cycle 2")) == 0
    view = read_bars_ledger(authority)
    assert [r.kind for r in view.records] == ["BARS_LAUNCH_APPROVAL", "BARS_LAUNCH_APPROVAL"]
    assert view.records[1].reason == "cycle 2"
    assert (
        view.records[1].work_manifest_sha256 == hashlib.sha256(second_path.read_bytes()).hexdigest()
    )


def test_a_tampered_census_refuses_before_anything_is_appended(
    tuple_bundle: dict[str, Path], scratch_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    doc = json.loads(tuple_bundle["census"].read_text(encoding="utf-8"))
    doc["content_sha256"] = "0" * 64
    tuple_bundle["census"].write_text(json.dumps(doc), encoding="utf-8")
    authority = scratch_root / "bars-authority"
    assert approval_main(_argv(tuple_bundle, authority)) == 2
    assert "census invalid" in capsys.readouterr().err
    assert not authority.exists()  # nothing was created anywhere


def test_an_unbound_work_manifest_refuses(
    tuple_bundle: dict[str, Path], scratch_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    doc = json.loads(tuple_bundle["work_manifest"].read_text(encoding="utf-8"))
    doc["cost"]["budget_limit"] = 46  # parse-valid, self-hash broken
    tuple_bundle["work_manifest"].write_text(json.dumps(doc), encoding="utf-8")
    authority = scratch_root / "bars-authority"
    assert approval_main(_argv(tuple_bundle, authority)) == 2
    assert "does not bind" in capsys.readouterr().err
    assert not authority.exists()


def test_an_empty_reason_refuses(
    tuple_bundle: dict[str, Path], scratch_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    authority = scratch_root / "bars-authority"
    assert approval_main(_argv(tuple_bundle, authority, reason="   ")) == 2
    assert "--reason" in capsys.readouterr().err
    assert not authority.exists()


def test_a_stale_protocol_version_refuses(
    tuple_bundle: dict[str, Path],
    scratch_root: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The approval binds the LIVE protocol: a 0.2.1-era yaml refuses (the
    continuation record must bind the re-opened gate's version)."""
    stale = write_021_protocol(tmp_path / "protocol-0.2.1.yaml")
    bundle = dict(tuple_bundle)
    bundle["protocol"] = stale
    authority = scratch_root / "bars-authority"
    assert approval_main(_argv(bundle, authority)) == 2
    assert "0.2.2" in capsys.readouterr().err
    assert not authority.exists()


def test_a_tmp_authority_root_refuses(
    tuple_bundle: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    under_tmp = Path("/tmp") / f"ext1-approval-refused-{uuid.uuid4().hex}"
    assert approval_main(_argv(tuple_bundle, under_tmp)) == 3
    assert "REFUSED" in capsys.readouterr().err
