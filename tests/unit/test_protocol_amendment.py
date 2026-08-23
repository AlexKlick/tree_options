"""Protocol 0.2.1 amendment builder: dry-run posture, validation matrix, rules.

Synthetic censuses are built DIRECTLY through the coverage_census models —
the census script is never invoked. Output roots must live under the real
repo artifacts/ (the builder refuses anywhere else), so each test creates a
unique subtree and removes it afterwards.
"""

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
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from tree_options.data.coverage_census import (  # noqa: E402
    CENSUS_SCHEMA_VERSION,
    CensusFact,
    CensusProvenance,
    CensusValues,
    CoverageBlock,
    CoverageCensus,
    PairCoverage,
    census_content_sha256,
)
from tree_options.protocol import amendment as amd  # noqa: E402
from tree_options.protocol.amendment import (  # noqa: E402
    AmendmentError,
    DerivationMismatchError,
    DerivationRule,
    FactRef,
    OpNode,
    OutputRefusedError,
    OwnerValue,
    OwnerValuesError,
    RatifiedRulesDoc,
    StaleCensusError,
    VersionError,
    evaluate,
    referenced_facts,
)
from tree_options.protocol.loader import load_protocol  # noqa: E402

PROTOCOL_PATH = REPO_ROOT / "research_protocol.yaml"
MANIFEST_BODY = b'{"schema_version": "m4b-manifest/1", "entries": []}\n'
FLOW_RULE_ID = "R-FLOW-FLOOR-DIV-4"
# floor_div(3045, 4) = 761: the owner value a correct derivation yields.
FLOW_DERIVED = 761


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _make_census_bytes(
    *,
    manifest_body: bytes = MANIFEST_BODY,
    observed: dict[str, int] | None = None,
    predeclared: dict[str, str] | None = None,
) -> bytes:
    """A self-consistent census built directly from the coverage models."""
    observed_facts = {
        fid: CensusFact(v=v, support={"census": 1}, confidence="EXACT")
        for fid, v in (
            observed
            if observed is not None
            else {"era_observed_masters": 3045, "era_as_of_fridays": 105}
        ).items()
    }
    predeclared = (
        predeclared
        if predeclared is not None
        else {
            "era_bar_volume_p05": (
                "NOT_EVALUABLE: coverage era ran --bars 0; no bar exists until the"
                " ATM-grid bars era (owner decision 2026-08-23)"
            )
        }
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
            observed_census_fact=observed_facts,
            predeclared_derivation_input=dict(predeclared),
            owner_ratified_policy_value={},
            not_yet_decided={
                "flow_min_session_volume": "AWAITING_OWNER_RULE (owner decision 2026-08-23)"
            },
        ),
        value_registry=(
            {fid: "observed_census_fact" for fid in observed_facts}
            | {fid: "predeclared_derivation_input" for fid in predeclared}
            | {"flow_min_session_volume": "not_yet_decided"}
        ),
        content_sha256="",
    )
    census = census.model_copy(update={"content_sha256": census_content_sha256(census)})
    return census.model_dump_json().encode("utf-8")


def _census_hash(census_bytes: bytes) -> str:
    return str(json.loads(census_bytes)["content_sha256"])


def _owner_values(census_hash: str, flow_value: int | float | None = FLOW_DERIVED) -> dict:
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
    values.append(
        {
            "id": "tier2_min_trade_count",
            "value": 7,
            "provenance": "owner_deviation",
            "rule_id": None,
            "deviation_record": "owner-decision-2026-08-23-001",
        }
    )
    return {"census_content_sha256": census_hash, "values": values}


def _rules(census_hash: str, *, expression: dict | None = None) -> dict:
    return {
        "rules": [
            {
                "rule_id": FLOW_RULE_ID,
                "census_binding": census_hash,
                "expression": expression
                if expression is not None
                else {"op": "floor_div", "args": [{"fact": "era_observed_masters"}, 4]},
            }
        ]
    }


def _bundle(
    tmp_path: Path,
    *,
    census_bytes: bytes | None = None,
    owner_values: dict | None = None,
    owner_text: str | None = None,
    rules: dict | None = None,
    manifest_body: bytes = MANIFEST_BODY,
) -> dict[str, Path]:
    census_bytes = census_bytes if census_bytes is not None else _make_census_bytes()
    census_hash = _census_hash(census_bytes)
    census_path = tmp_path / "census.json"
    census_path.write_bytes(census_bytes)
    manifest_path = tmp_path / "capture_manifest.json"
    manifest_path.write_bytes(manifest_body)
    owner_path = tmp_path / "owner-values.json"
    if owner_text is not None:
        owner_path.write_text(owner_text, encoding="utf-8")
    else:
        doc = owner_values if owner_values is not None else _owner_values(census_hash)
        owner_path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    rules_path = tmp_path / "rules.json"
    rules_path.write_text(
        json.dumps(rules if rules is not None else _rules(census_hash), indent=2),
        encoding="utf-8",
    )
    return {
        "census": census_path,
        "owner_values": owner_path,
        "rules": rules_path,
        "manifest": manifest_path,
        "protocol": PROTOCOL_PATH,
    }


@contextlib.contextmanager
def _out_root() -> Iterator[Path]:
    root = REPO_ROOT / "artifacts" / f"amendment-test-{uuid.uuid4().hex[:12]}"
    root.mkdir(parents=True, exist_ok=True)
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _build(paths: dict[str, Path], out_root: Path) -> amd.AmendmentPacket:
    return amd.build_proposed_amendment(
        paths["census"],
        paths["owner_values"],
        paths["rules"],
        protocol_path=paths["protocol"],
        capture_manifest_path=paths["manifest"],
        out_root=out_root,
    )


def _wrong_base_protocol(tmp_path: Path) -> Path:
    """A protocol that LOADS but is not 0.2.0 (version + own record bumped)."""
    doc = yaml.safe_load(PROTOCOL_PATH.read_text(encoding="utf-8"))
    doc["meta"]["protocol_version"] = "0.2.1"
    doc["meta"]["amendments"].append(
        {
            "version": "0.2.1",
            "date": "2026-08-23",
            "decision": "test fixture only",
            "changes": "test fixture base for the wrong-base rejection",
        }
    )
    path = tmp_path / "protocol-0.2.1.yaml"
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return path


# ---- OwnerValue / docs model contract ----------------------------------------------


def test_owner_value_bool_true_rejected() -> None:
    with pytest.raises(ValueError, match="bool"):
        OwnerValue.model_validate(
            {
                "id": "flow_min_session_volume",
                "value": True,
                "provenance": "owner_deviation",
                "rule_id": None,
                "deviation_record": "owner-decision-1",
            }
        )


def test_owner_value_float_rejected() -> None:
    with pytest.raises(ValueError, match="strict int"):
        OwnerValue.model_validate(
            {
                "id": "x",
                "value": 7.5,
                "provenance": "owner_deviation",
                "rule_id": None,
                "deviation_record": "owner-decision-1",
            }
        )


def test_owner_value_derivation_requires_rule_id() -> None:
    with pytest.raises(ValueError, match="derivation requires rule_id"):
        OwnerValue.model_validate(
            {"id": "x", "value": 3, "provenance": "derivation", "deviation_record": None}
        )


def test_owner_value_deviation_requires_deviation_record() -> None:
    with pytest.raises(ValueError, match="owner_deviation requires deviation_record"):
        OwnerValue.model_validate(
            {"id": "x", "value": 3, "provenance": "owner_deviation", "rule_id": None}
        )


def test_owner_value_cross_evidence_rejected() -> None:
    with pytest.raises(ValueError, match="must not carry deviation_record"):
        OwnerValue.model_validate(
            {
                "id": "x",
                "value": 3,
                "provenance": "derivation",
                "rule_id": "R1",
                "deviation_record": "owner-decision-1",
            }
        )
    with pytest.raises(ValueError, match="must not carry rule_id"):
        OwnerValue.model_validate(
            {
                "id": "x",
                "value": 3,
                "provenance": "owner_deviation",
                "rule_id": "R1",
                "deviation_record": "owner-decision-1",
            }
        )


def test_owner_values_doc_duplicate_ids_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate owner value ids"):
        amd.OwnerValuesDoc.model_validate(
            {
                "census_content_sha256": "a" * 64,
                "values": [
                    {
                        "id": "x",
                        "value": 1,
                        "provenance": "owner_deviation",
                        "rule_id": None,
                        "deviation_record": "d1",
                    },
                    {
                        "id": "x",
                        "value": 2,
                        "provenance": "owner_deviation",
                        "rule_id": None,
                        "deviation_record": "d2",
                    },
                ],
            }
        )


def test_ratified_rules_doc_duplicate_rule_ids_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate rule ids"):
        RatifiedRulesDoc.model_validate(
            {
                "rules": [
                    {"rule_id": "R", "census_binding": "a" * 64, "expression": 1},
                    {"rule_id": "R", "census_binding": "a" * 64, "expression": 2},
                ]
            }
        )


# ---- derivation rule evaluation ----------------------------------------------------


def _rule(expression: object) -> DerivationRule:
    return DerivationRule.model_validate(
        {"rule_id": "R-TEST", "census_binding": "a" * 64, "expression": expression}
    )


def test_evaluate_nests_max_min_mul_floor_div() -> None:
    # floor_div(max(mul(3, 4), min(100, 7)), 5) = floor_div(max(12, 7), 5) = 2
    rule = _rule(
        {
            "op": "floor_div",
            "args": [
                {
                    "op": "max",
                    "args": [{"op": "mul", "args": [3, 4]}, {"op": "min", "args": [100, 7]}],
                },
                5,
            ],
        }
    )
    assert evaluate(rule, {}) == 2


def test_evaluate_resolves_fact_references() -> None:
    rule = _rule({"op": "mul", "args": [{"fact": "era_a"}, {"fact": "era_b"}]})
    assert evaluate(rule, {"era_a": 6, "era_b": 7}) == 42


def test_evaluate_unknown_fact_raises_amendment_error() -> None:
    rule = _rule({"op": "max", "args": [{"fact": "era_missing"}, 1]})
    with pytest.raises(AmendmentError, match="unknown fact 'era_missing'"):
        evaluate(rule, {"era_other": 5})


def test_evaluate_floor_div_by_zero_wrapped() -> None:
    rule = _rule({"op": "floor_div", "args": [761, 0]})
    with pytest.raises(OwnerValuesError, match="divides by zero"):
        evaluate(rule, {})


def test_expression_rejects_bool_literal() -> None:
    with pytest.raises(ValueError):
        _rule({"op": "max", "args": [True, 2]})


def test_expression_rejects_float_literal() -> None:
    with pytest.raises(ValueError):
        _rule({"op": "max", "args": [2.0, 3]})


def test_expression_rejects_unknown_op() -> None:
    with pytest.raises(ValueError):
        _rule({"op": "pow", "args": [2, 3]})


def test_expression_rejects_single_argument_op() -> None:
    with pytest.raises(ValueError):
        _rule({"op": "max", "args": [1]})


def test_referenced_facts_walks_the_whole_tree() -> None:
    rule = _rule(
        {"op": "max", "args": [{"fact": "f1"}, {"op": "mul", "args": [{"fact": "f2"}, 3]}]}
    )
    assert referenced_facts(rule.expression) == ("f1", "f2")


def test_opnode_model_roundtrip() -> None:
    node = OpNode.model_validate({"op": "min", "args": [4, {"fact": "f"}]})
    assert node.args == (4, FactRef(fact="f"))


# ---- builder: happy path + dry-run posture ------------------------------------------


def test_builder_emits_deterministic_not_landed_packet(tmp_path: Path) -> None:
    census_bytes = _make_census_bytes()
    paths = _bundle(tmp_path, census_bytes=census_bytes)
    with _out_root() as out1, _out_root() as out2:
        packet = _build(paths, out1)
        assert packet.landed is False
        assert packet.base_version == "0.2.0"
        assert packet.proposed_version == "0.2.1"
        assert packet.flow_min_session_volume == FLOW_DERIVED
        # re-run over identical inputs into a second root: byte-identical
        assert _build(paths, out2) == packet

        dir1 = out1 / _census_hash(census_bytes)[:12]
        dir2 = out2 / _census_hash(census_bytes)[:12]
        names = (
            "protocol-0.2.1-proposed.yaml",
            "schema-addition-proposal.yaml",
            "amendment-diff.md",
            "amendment-packet.json",
        )
        for name in names:
            assert (dir1 / name).is_file(), name
            assert (dir1 / name).read_bytes() == (dir2 / name).read_bytes(), name

        # the proposal round-trips through the REAL loader
        parsed = load_protocol(dir1 / "protocol-0.2.1-proposed.yaml")
        base = load_protocol(paths["protocol"])
        assert parsed.meta.protocol_version == "0.2.1"
        assert len(parsed.meta.amendments) == len(base.meta.amendments) + 1
        assert parsed.meta.amendments[-1].version == "0.2.1"
        flow = parsed.option_candidate_defaults.liquidity_volume_flow
        assert flow is not None
        assert flow.flow_min_session_volume == FLOW_DERIVED

        proposal = (dir1 / "schema-addition-proposal.yaml").read_text(encoding="utf-8")
        assert "NOT_LANDED: true" in proposal
        assert "final_holdout_window" in proposal

        record = json.loads((dir1 / "amendment-packet.json").read_text(encoding="utf-8"))
        assert record["landed"] is False
        for artifact in record["emitted"]:
            assert _sha256((dir1 / artifact["name"]).read_bytes()) == artifact["sha256"]

        diff = (dir1 / "amendment-diff.md").read_text(encoding="utf-8")
        assert "null -> 761" in diff
        assert "NOT LANDED" in diff


def test_builder_flow_value_via_owner_deviation(tmp_path: Path) -> None:
    census_bytes = _make_census_bytes()
    census_hash = _census_hash(census_bytes)
    paths = _bundle(
        tmp_path,
        census_bytes=census_bytes,
        owner_values={
            "census_content_sha256": census_hash,
            "values": [
                {
                    "id": "flow_min_session_volume",
                    "value": 900,
                    "provenance": "owner_deviation",
                    "rule_id": None,
                    "deviation_record": "owner-decision-2026-08-23-002",
                }
            ],
        },
    )
    with _out_root() as out:
        packet = _build(paths, out)
    assert packet.flow_min_session_volume == 900


# ---- builder: rejection matrix (each refusal is its own test) ------------------------


def test_census_content_hash_tamper_refused(tmp_path: Path) -> None:
    doc = json.loads(_make_census_bytes())
    # tamper a derivation input but keep the declared content hash
    doc["values"]["observed_census_fact"]["era_observed_masters"]["v"] = 4000
    paths = _bundle(tmp_path, census_bytes=json.dumps(doc).encode("utf-8"))
    with _out_root() as out:
        with pytest.raises(StaleCensusError, match="tampered"):
            _build(paths, out)


def test_census_manifest_drift_refused(tmp_path: Path) -> None:
    paths = _bundle(tmp_path)
    paths["manifest"].write_bytes(b'{"schema_version": "m4b-manifest/1", "entries": ["drift"]}\n')
    with _out_root() as out:
        with pytest.raises(StaleCensusError, match="drifted"):
            _build(paths, out)


def test_owner_doc_census_binding_mismatch_refused(tmp_path: Path) -> None:
    census_bytes = _make_census_bytes()
    paths = _bundle(tmp_path, census_bytes=census_bytes, owner_values=_owner_values("f" * 64))
    with _out_root() as out:
        with pytest.raises(OwnerValuesError, match="not bound to this census"):
            _build(paths, out)


def test_rules_census_binding_mismatch_refused(tmp_path: Path) -> None:
    census_bytes = _make_census_bytes()
    paths = _bundle(tmp_path, census_bytes=census_bytes, rules=_rules("e" * 64))
    with _out_root() as out:
        with pytest.raises(OwnerValuesError, match="not bound to this census"):
            _build(paths, out)


def test_wrong_base_version_refused(tmp_path: Path) -> None:
    paths = _bundle(tmp_path)
    paths["protocol"] = _wrong_base_protocol(tmp_path)
    with _out_root() as out:
        with pytest.raises(VersionError, match="base protocol version must be exactly"):
            _build(paths, out)


def test_target_version_not_patch_plus_one_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _bundle(tmp_path)
    monkeypatch.setattr(amd, "PROPOSED_PROTOCOL_VERSION", "9.9.9")
    with _out_root() as out:
        with pytest.raises(VersionError, match="non-monotonic"):
            _build(paths, out)


def test_missing_flow_min_session_volume_refused(tmp_path: Path) -> None:
    census_bytes = _make_census_bytes()
    census_hash = _census_hash(census_bytes)
    paths = _bundle(
        tmp_path,
        census_bytes=census_bytes,
        owner_values=_owner_values(census_hash, flow_value=None),
    )
    with _out_root() as out:
        with pytest.raises(OwnerValuesError, match="flow_min_session_volume"):
            _build(paths, out)


def test_zero_flow_min_session_volume_refused(tmp_path: Path) -> None:
    # via owner_deviation so the hidden-default branch itself fires (a
    # derived zero would hit the value!=rule mismatch first)
    census_bytes = _make_census_bytes()
    census_hash = _census_hash(census_bytes)
    paths = _bundle(
        tmp_path,
        census_bytes=census_bytes,
        owner_values={
            "census_content_sha256": census_hash,
            "values": [
                {
                    "id": "flow_min_session_volume",
                    "value": 0,
                    "provenance": "owner_deviation",
                    "rule_id": None,
                    "deviation_record": "owner-decision-2026-08-23-003",
                }
            ],
        },
    )
    with _out_root() as out:
        with pytest.raises(OwnerValuesError, match="silent"):
            _build(paths, out)


def test_builder_rejects_bool_flow_value(tmp_path: Path) -> None:
    census_bytes = _make_census_bytes()
    census_hash = _census_hash(census_bytes)
    doc = _owner_values(census_hash)
    doc["values"][0]["value"] = True
    paths = _bundle(tmp_path, census_bytes=census_bytes, owner_values=doc)
    with _out_root() as out:
        with pytest.raises(OwnerValuesError, match="bool"):
            _build(paths, out)


def test_nan_literal_in_owner_values_refused(tmp_path: Path) -> None:
    census_bytes = _make_census_bytes()
    census_hash = _census_hash(census_bytes)
    doc = _owner_values(census_hash)
    doc["values"][0]["value"] = float("nan")
    text = json.dumps(doc)  # emits a literal NaN, not a string
    assert "NaN" in text
    paths = _bundle(tmp_path, census_bytes=census_bytes, owner_text=text)
    with _out_root() as out:
        with pytest.raises(OwnerValuesError, match="non-finite"):
            _build(paths, out)


def test_infinity_literal_in_owner_values_refused(tmp_path: Path) -> None:
    census_bytes = _make_census_bytes()
    census_hash = _census_hash(census_bytes)
    doc = _owner_values(census_hash)
    doc["values"][0]["value"] = float("inf")
    text = json.dumps(doc)  # emits a literal Infinity, not a string
    assert "Infinity" in text
    paths = _bundle(tmp_path, census_bytes=census_bytes, owner_text=text)
    with _out_root() as out:
        with pytest.raises(OwnerValuesError, match="non-finite"):
            _build(paths, out)


def test_derived_value_not_equal_to_rule_refused(tmp_path: Path) -> None:
    census_bytes = _make_census_bytes()
    census_hash = _census_hash(census_bytes)
    paths = _bundle(
        tmp_path,
        census_bytes=census_bytes,
        owner_values=_owner_values(census_hash, flow_value=FLOW_DERIVED - 1),
    )
    with _out_root() as out:
        with pytest.raises(DerivationMismatchError, match="computes"):
            _build(paths, out)


def test_future_derived_fact_refused(tmp_path: Path) -> None:
    # the G3 contradiction, replayed: a rule over a predeclared (NOT_EVALUABLE)
    # bar-volume input derives a value no census observed
    census_bytes = _make_census_bytes()
    census_hash = _census_hash(census_bytes)
    paths = _bundle(
        tmp_path,
        census_bytes=census_bytes,
        owner_values=_owner_values(census_hash),
        rules=_rules(
            census_hash,
            expression={"op": "floor_div", "args": [{"fact": "era_bar_volume_p05"}, 1]},
        ),
    )
    with _out_root() as out:
        with pytest.raises(DerivationMismatchError, match="future-derived"):
            _build(paths, out)


def test_unknown_rule_cited_refused(tmp_path: Path) -> None:
    census_bytes = _make_census_bytes()
    census_hash = _census_hash(census_bytes)
    doc = _owner_values(census_hash)
    doc["values"][0]["rule_id"] = "R-NEVER-RATIFIED"
    paths = _bundle(tmp_path, census_bytes=census_bytes, owner_values=doc)
    with _out_root() as out:
        with pytest.raises(OwnerValuesError, match="unknown rule"):
            _build(paths, out)


def test_empty_deviation_record_refused(tmp_path: Path) -> None:
    census_bytes = _make_census_bytes()
    census_hash = _census_hash(census_bytes)
    doc = _owner_values(census_hash)
    doc["values"][1]["deviation_record"] = ""
    paths = _bundle(tmp_path, census_bytes=census_bytes, owner_values=doc)
    with _out_root() as out:
        with pytest.raises(OwnerValuesError, match="empty deviation_record"):
            _build(paths, out)


def test_out_root_outside_artifacts_refused(tmp_path: Path) -> None:
    paths = _bundle(tmp_path)
    rogue = REPO_ROOT / "data" / "amendment-refused-probe"
    try:
        with pytest.raises(OutputRefusedError, match="outside"):
            _build(paths, rogue)
    finally:
        shutil.rmtree(rogue, ignore_errors=True)
