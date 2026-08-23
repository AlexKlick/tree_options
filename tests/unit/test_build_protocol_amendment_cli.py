"""build_protocol_amendment CLI: exit-code contract and the dry-run happy path."""

from __future__ import annotations

import contextlib
import hashlib
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

import build_protocol_amendment as cli  # noqa: E402

PROTOCOL_PATH = REPO_ROOT / "research_protocol.yaml"
MANIFEST_BODY = b'{"schema_version": "m4b-manifest/1", "entries": []}\n'
FLOW_RULE_ID = "R-FLOW-FLOOR-DIV-4"
FLOW_DERIVED = 761  # floor_div(3045, 4)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _census_bytes(manifest_body: bytes = MANIFEST_BODY) -> bytes:
    from tree_options.data.coverage_census import (
        CENSUS_SCHEMA_VERSION,
        CensusFact,
        CensusProvenance,
        CensusValues,
        CoverageBlock,
        CoverageCensus,
        PairCoverage,
        census_content_sha256,
    )

    census = CoverageCensus(
        schema_version=CENSUS_SCHEMA_VERSION,
        provenance=CensusProvenance(
            code_sha="f" * 40,
            protocol_hash="a" * 64,
            protocol_raw_sha256="b" * 64,
            input_manifest_sha256=_sha256(manifest_body),
            universe_manifest_sha256="c" * 64,
            uv_lock_sha256="d" * 64,
            command=("uv", "run", "--frozen", "python", "scripts/inspect_structural_coverage.py"),
        ),
        coverage=CoverageBlock(expected_masters=3045, observed=PairCoverage(COMPLETE=3045)),
        values=CensusValues(
            observed_census_fact={
                "era_observed_masters": CensusFact(
                    v=3045, support={"census": 1}, confidence="EXACT"
                )
            },
            predeclared_derivation_input={
                "era_bar_volume_p05": "NOT_EVALUABLE: coverage era ran --bars 0"
            },
            owner_ratified_policy_value={},
            not_yet_decided={
                "flow_min_session_volume": "AWAITING_OWNER_RULE (owner decision 2026-08-23)"
            },
        ),
        value_registry={
            "era_observed_masters": "observed_census_fact",
            "era_bar_volume_p05": "predeclared_derivation_input",
            "flow_min_session_volume": "not_yet_decided",
        },
        content_sha256="",
    )
    census = census.model_copy(update={"content_sha256": census_content_sha256(census)})
    return census.model_dump_json().encode("utf-8")


@contextlib.contextmanager
def _out_root() -> Iterator[Path]:
    root = REPO_ROOT / "artifacts" / f"amendment-cli-test-{uuid.uuid4().hex[:12]}"
    root.mkdir(parents=True, exist_ok=True)
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _write_bundle(
    tmp_path: Path,
    *,
    flow_value: int | None = FLOW_DERIVED,
    manifest_body: bytes = MANIFEST_BODY,
) -> dict[str, Path]:
    census_bytes = _census_bytes(manifest_body)
    census_hash = str(json.loads(census_bytes)["content_sha256"])
    census_path = tmp_path / "census.json"
    census_path.write_bytes(census_bytes)
    manifest_path = tmp_path / "capture_manifest.json"
    manifest_path.write_bytes(manifest_body)
    values: list[dict] = []
    if flow_value is not None:
        values.append(
            {
                "id": "flow_min_session_volume",
                "value": flow_value,
                "provenance": "derivation",
                "rule_id": FLOW_RULE_ID,
                "deviation_record": None,
            }
        )
    owner_path = tmp_path / "owner-values.json"
    owner_path.write_text(
        json.dumps({"census_content_sha256": census_hash, "values": values}, indent=2),
        encoding="utf-8",
    )
    rules_path = tmp_path / "rules.json"
    rules_path.write_text(
        json.dumps(
            {
                "rules": [
                    {
                        "rule_id": FLOW_RULE_ID,
                        "census_binding": census_hash,
                        "expression": {
                            "op": "floor_div",
                            "args": [{"fact": "era_observed_masters"}, 4],
                        },
                    }
                ]
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return {
        "census": census_path,
        "owner_values": owner_path,
        "rules": rules_path,
        "manifest": manifest_path,
        "protocol": PROTOCOL_PATH,
    }


def _run_cli(paths: dict[str, Path], out_root: Path) -> int:
    return cli.main(
        [
            "--census",
            str(paths["census"]),
            "--owner-values",
            str(paths["owner_values"]),
            "--rules",
            str(paths["rules"]),
            "--capture-manifest",
            str(paths["manifest"]),
            "--protocol",
            str(paths["protocol"]),
            "--out-root",
            str(out_root),
        ]
    )


def test_cli_happy_path_builds_not_landed_packet(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    paths = _write_bundle(tmp_path)
    with _out_root() as out:
        assert _run_cli(paths, out) == 0
        record = json.loads(capsys.readouterr().out)
        assert record["landed"] is False
        assert record["proposed_version"] == "0.2.1"
        assert record["flow_min_session_volume"] == FLOW_DERIVED
        census_hash = record["census_content_sha256"]
        packet_dir = out / census_hash[:12]
        assert (packet_dir / "protocol-0.2.1-proposed.yaml").is_file()
        assert (packet_dir / "schema-addition-proposal.yaml").is_file()
        assert (packet_dir / "amendment-diff.md").is_file()
        on_disk = json.loads((packet_dir / "amendment-packet.json").read_text(encoding="utf-8"))
        assert on_disk == record


def test_cli_exit_2_on_manifest_drift(tmp_path: Path) -> None:
    paths = _write_bundle(tmp_path)
    paths["manifest"].write_bytes(b'{"schema_version": "m4b-manifest/1", "entries": ["d"]}\n')
    with _out_root() as out:
        assert _run_cli(paths, out) == 2


def test_cli_exit_3_on_value_rule_mismatch(tmp_path: Path) -> None:
    # a supplied value that disagrees with its ratified rule (the dedicated
    # hidden-default zero case lives in the unit builder tests)
    paths = _write_bundle(tmp_path, flow_value=0)
    with _out_root() as out:
        assert _run_cli(paths, out) == 3


def test_cli_exit_4_on_wrong_base_version(tmp_path: Path) -> None:
    import yaml

    paths = _write_bundle(tmp_path)
    doc = yaml.safe_load(PROTOCOL_PATH.read_text(encoding="utf-8"))
    doc["meta"]["protocol_version"] = "0.2.1"
    doc["meta"]["amendments"].append(
        {
            "version": "0.2.1",
            "date": "2026-08-23",
            "decision": "test fixture only",
            "changes": "test fixture base for the wrong-base exit",
        }
    )
    other = tmp_path / "protocol-0.2.1.yaml"
    other.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    paths["protocol"] = other
    with _out_root() as out:
        assert _run_cli(paths, out) == 4


def test_cli_exit_5_on_out_root_outside_artifacts(tmp_path: Path) -> None:
    paths = _write_bundle(tmp_path)
    rogue = REPO_ROOT / "data" / "amendment-cli-refused-probe"
    try:
        assert _run_cli(paths, rogue) == 5
    finally:
        shutil.rmtree(rogue, ignore_errors=True)


def test_cli_owner_values_has_no_default() -> None:
    # the script must never invent a threshold: --owner-values is required
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--census", "x", "--rules", "y", "--capture-manifest", "z"])
    assert excinfo.value.code == 2
