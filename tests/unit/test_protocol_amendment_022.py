"""The 0.2.2 lane-on declaration packet (owner ruling m4-022-ruling-20260828).

Pins the dry-run builder's contract: the packet is constructed from the
pinned 0.2.1 fixture (the pre-flip standing protocol; the flip landed
2026-08-28 and the builder refuses non-0.2.1 bases), carries the three
owner-ruled declarations each
with its owner-decision id, computes (never applies) the projected post-flip
identity through the real loader's own hashing, emits under artifacts/ only,
says landed: false, and leaves research_protocol.yaml untouched. The base
identity the packet records must equal the ledger-bound 0.2.1 pin.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import shutil
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.conftest import REPO_ROOT
from tree_options.protocol import amendment as amd
from tree_options.protocol.amendment import build_declaration_amendment

PROTOCOL_PATH = REPO_ROOT / "research_protocol.yaml"
# (0.2.2 flip) the builder's BASE is the pinned 0.2.1 fixture: the builder
# refuses non-0.2.1 bases, and the live yaml is 0.2.2 since the flip
PROTOCOL_021 = REPO_ROOT / "tests" / "fixtures" / "protocol-0.2.1.yaml"
LEDGER_BOUND_PROTOCOL_SHA256 = "cfafc884d9c45d805f6d6028d6991daf9e2e1751d91823306d780506bbaffeb7"


@contextlib.contextmanager
def _out_root() -> Iterator[Path]:
    root = REPO_ROOT / "artifacts" / f"amendment-022-test-{uuid.uuid4().hex[:12]}"
    root.mkdir(parents=True, exist_ok=True)
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_the_declaration_packet_carries_the_three_ruled_declarations(tmp_path: Path) -> None:
    with _out_root() as out:
        packet = build_declaration_amendment(protocol_path=PROTOCOL_021, out_root=out)
        assert packet.landed is False
        assert packet.base_version == "0.2.1"
        assert packet.proposed_version == "0.2.2"
        assert packet.owner_decision == "m4-022-ruling-20260828"
        assert [(d.field, d.value) for d in packet.declarations] == [
            (
                "option_candidate_defaults.liquidity_volume_flow.underlying_liquidity_term",
                "evaluated",
            ),
            ("option_candidate_defaults.earnings_evaluation", "disclosed_absence"),
            ("fills.fill_door_decision_close", "decision_grid"),
        ]
        for declaration in packet.declarations:
            assert declaration.owner_decision == "m4-022-ruling-20260828"
        # the packet's BASE identity IS the ledger-bound 0.2.1 pin: the
        # declaration packet is built from exactly the protocol every trial
        # and ledger stamped
        assert packet.protocol_hash_base == LEDGER_BOUND_PROTOCOL_SHA256
        # the projected post-flip hash is a real sha256 hex and MOVES
        assert len(packet.protocol_hash_projected) == 64
        int(packet.protocol_hash_projected, 16)
        assert packet.protocol_hash_projected != packet.protocol_hash_base
        # emitted under <out-root>/<projected-hash[:12]>/ with all artifacts
        packet_dir = out / packet.protocol_hash_projected[:12]
        proposed_yaml = packet_dir / "protocol-0.2.2-proposed.yaml"
        diff_md = packet_dir / "amendment-diff.md"
        packet_json = packet_dir / "amendment-packet.json"
        for artifact in (proposed_yaml, diff_md, packet_json):
            assert artifact.is_file()
        on_disk = json.loads(packet_json.read_text(encoding="utf-8"))
        assert on_disk == json.loads(packet.model_dump_json())


def test_the_projected_hash_is_the_loaders_own_answer_for_the_flipped_yaml(
    tmp_path: Path,
) -> None:
    """Coherence with the flip it proposes: the EMITTED proposed yaml —
    loaded through the real loader exactly as it will be post-flip — hashes
    to the packet's projected value, and the declarations actually RIDE that
    identity (flipping one declaration value moves the hash): the projected
    number is not decorative, it is the identity protocol_hash will answer
    for the flipped protocol."""
    from tree_options.protocol.loader import load_protocol, protocol_hash

    with _out_root() as out:
        packet = build_declaration_amendment(protocol_path=PROTOCOL_021, out_root=out)
        proposed = load_protocol(
            out / packet.protocol_hash_projected[:12] / "protocol-0.2.2-proposed.yaml"
        )
        assert proposed.meta.protocol_version == "0.2.2"
        lf = proposed.option_candidate_defaults.liquidity_volume_flow
        assert lf is not None
        assert lf.underlying_liquidity_term == "evaluated"
        assert proposed.option_candidate_defaults.earnings_evaluation == "disclosed_absence"
        assert proposed.fills.fill_door_decision_close == "decision_grid"
        assert len(proposed.meta.amendments) == 3  # 0.2.0 + 0.2.1 + 0.2.2 records
        assert protocol_hash(proposed) == packet.protocol_hash_projected
        # the DECLARED values ride the projected identity at 0.2.2 (the
        # pre-draft strip is version-gated OFF there): flipping one
        # declaration's value moves the hash — including the DECLARED
        # "evaluated", which equals its default yet rides by design
        flipped_earnings = proposed.model_copy(
            update={
                "option_candidate_defaults": proposed.option_candidate_defaults.model_copy(
                    update={"earnings_evaluation": "evaluated"}
                )
            }
        )
        assert protocol_hash(flipped_earnings) != packet.protocol_hash_projected
        flipped_term = proposed.model_copy(
            update={
                "option_candidate_defaults": proposed.option_candidate_defaults.model_copy(
                    update={
                        "liquidity_volume_flow": lf.model_copy(
                            update={"underlying_liquidity_term": "dropped_no_equity_aggregates"}
                        )
                    }
                )
            }
        )
        assert protocol_hash(flipped_term) != packet.protocol_hash_projected
        flipped_door = proposed.model_copy(
            update={
                "fills": proposed.fills.model_copy(
                    update={"fill_door_decision_close": "execution_calendar"}
                )
            }
        )
        assert protocol_hash(flipped_door) != packet.protocol_hash_projected


def test_the_builder_is_dry_run_only_the_yaml_never_moves(tmp_path: Path) -> None:
    before = hashlib.sha256(PROTOCOL_PATH.read_bytes()).hexdigest()
    with _out_root() as out:
        build_declaration_amendment(protocol_path=PROTOCOL_021, out_root=out)
    after = hashlib.sha256(PROTOCOL_PATH.read_bytes()).hexdigest()
    assert before == after
    from tree_options.protocol.loader import load_protocol, protocol_hash

    # the builder's base (the pinned pre-flip protocol) IS the ledger-bound
    # 0.2.1 identity; the live yaml answers the LANDED 0.2.2 pin instead
    assert protocol_hash(load_protocol(PROTOCOL_021)) == LEDGER_BOUND_PROTOCOL_SHA256


def test_a_non_021_base_refuses(tmp_path: Path) -> None:
    import yaml

    doc = yaml.safe_load(PROTOCOL_021.read_text(encoding="utf-8"))
    doc["meta"]["protocol_version"] = "0.2.0"
    doc["meta"]["amendments"] = [
        record for record in doc["meta"]["amendments"] if record["version"] != "0.2.1"
    ]
    path = tmp_path / "protocol-0.2.0.yaml"
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    with _out_root() as out:
        with pytest.raises(amd.VersionError, match=r"0\.2\.1"):
            build_declaration_amendment(protocol_path=path, out_root=out)


def test_an_out_root_outside_artifacts_refuses(tmp_path: Path) -> None:
    rogue = REPO_ROOT / "data" / "amendment-022-refused-probe"
    try:
        with pytest.raises(amd.OutputRefusedError):
            build_declaration_amendment(protocol_path=PROTOCOL_021, out_root=rogue)
    finally:
        shutil.rmtree(rogue, ignore_errors=True)


def test_rebuilds_are_byte_identical(tmp_path: Path) -> None:
    with _out_root() as out:
        first = build_declaration_amendment(protocol_path=PROTOCOL_021, out_root=out)
        digest_dir = out / first.protocol_hash_projected[:12]
        first_bytes = {p.name: p.read_bytes() for p in sorted(digest_dir.iterdir()) if p.is_file()}
        second = build_declaration_amendment(protocol_path=PROTOCOL_021, out_root=out)
        second_bytes = {p.name: p.read_bytes() for p in sorted(digest_dir.iterdir()) if p.is_file()}
    assert first_bytes == second_bytes
    assert first == second


# ---- the CLI surface ----------------------------------------------------------------


def _cli(argv: list[str]) -> int:
    import sys

    scripts = REPO_ROOT / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    import build_protocol_amendment as cli

    return cli.main(argv)


def test_cli_target_version_022_builds_the_declaration_packet(capsys) -> None:
    with _out_root() as out:
        argv = [
            "--target-version",
            "0.2.2",
            "--out-root",
            str(out),
            "--protocol",
            str(PROTOCOL_021),
        ]
        assert _cli(argv) == 0
    record = json.loads(capsys.readouterr().out)
    assert record["landed"] is False
    assert record["proposed_version"] == "0.2.2"
    assert record["owner_decision"] == "m4-022-ruling-20260828"
    assert record["protocol_hash_base"] == LEDGER_BOUND_PROTOCOL_SHA256
    assert record["protocol_hash_projected"]


def test_cli_021_mode_still_requires_its_derivation_inputs() -> None:
    # the threshold builder never invents an owner value or a census
    with pytest.raises(SystemExit) as excinfo:
        _cli(["--census", "x", "--rules", "y", "--capture-manifest", "z"])
    assert excinfo.value.code == 2
    with pytest.raises(SystemExit) as excinfo:
        _cli([])
    assert excinfo.value.code == 2
