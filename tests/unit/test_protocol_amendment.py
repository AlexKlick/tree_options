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
import os
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
    CENSUS_DOMAIN,
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
    observed_confidence: str = "EXACT",
    observed_extra: dict[str, tuple[int | str, str]] | None = None,
) -> bytes:
    """A self-consistent census built directly from the coverage models.

    ``observed_extra`` adds observed facts with an explicit (v, confidence)
    pair — the shape the round-2 producer/consumer probe needs (the canonical
    producer always emits a numeric ``bar_volume_observations`` as
    NOT_EVALUABLE, and textual observations exist too).
    """
    observed_facts = {
        fid: CensusFact(v=v, support={"census": 1}, confidence=observed_confidence)
        for fid, v in (
            observed
            if observed is not None
            else {
                "era_observed_masters": 3045,
                "era_as_of_fridays": 105,
                # The canonical producer's wholeness attestation fact
                # (scripts/build_coverage_census.py): masters observed ==
                # coverage.expected_masters in the default whole fixture.
                "masters_observed": 3045,
            }
        ).items()
    }
    for fid, (v, confidence) in (observed_extra or {}).items():
        observed_facts[fid] = CensusFact(v=v, support={"census": 1}, confidence=confidence)
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
            report_version=CENSUS_SCHEMA_VERSION,
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


# ---- round-2 (finding 3): the confidence gate is derivation-time ---------------------
#
# The canonical census producer (scripts/build_coverage_census.py) ALWAYS
# emits the numeric fact bar_volume_observations with confidence
# NOT_EVALUABLE. The round-1 gate refused ANY non-EXACT numeric observation
# at emission time — before any rule was consulted — so the canonical census
# could never feed even an owner-deviation amendment. The gate now applies
# per fact a rule actually REFERENCES, at derivation time.


def test_unreferenced_not_evaluable_int_fact_does_not_block_owner_deviation(
    tmp_path: Path,
) -> None:
    """The exact producer/consumer probe shape: a census carrying a numeric
    NOT_EVALUABLE bar_volume_observations that NO rule references, with the
    flow threshold supplied as an owner deviation, must BUILD."""
    census_bytes = _make_census_bytes(
        observed_extra={"bar_volume_observations": (0, "NOT_EVALUABLE")}
    )
    census_hash = _census_hash(census_bytes)
    paths = _bundle(
        tmp_path,
        census_bytes=census_bytes,
        owner_values={
            "census_content_sha256": census_hash,
            "values": [
                {
                    "id": "flow_min_session_volume",
                    "value": 761,
                    "provenance": "owner_deviation",
                    "rule_id": None,
                    "deviation_record": "owner-decision-2026-08-23-004",
                }
            ],
        },
    )
    with _out_root() as out:
        packet = _build(paths, out)
    assert packet.flow_min_session_volume == 761
    assert packet.landed is False


@pytest.mark.parametrize("confidence", ["NOT_EVALUABLE", "PARTIAL"])
def test_rule_referencing_non_exact_int_fact_refused(tmp_path: Path, confidence: str) -> None:
    """A rule that DERIVES from a non-EXACT numeric observation is refused
    at derivation time, naming the value, rule, fact, and confidence."""
    census_bytes = _make_census_bytes(observed_extra={"bar_volume_observations": (0, confidence)})
    census_hash = _census_hash(census_bytes)
    paths = _bundle(
        tmp_path,
        census_bytes=census_bytes,
        rules=_rules(
            census_hash,
            expression={"op": "max", "args": [{"fact": "bar_volume_observations"}, 1]},
        ),
    )
    with _out_root() as out:
        with pytest.raises(DerivationMismatchError) as exc_info:
            _build(paths, out)
    message = str(exc_info.value)
    assert "flow_min_session_volume" in message
    assert FLOW_RULE_ID in message
    assert "bar_volume_observations" in message
    assert confidence in message
    assert "owner_deviation" in message  # the guidance, not a census repair


def test_rule_referencing_str_observed_fact_refused(tmp_path: Path) -> None:
    """A rule referencing a textual (non-int) observed fact is refused at the
    derivation-time gate, not by a evaluate-time KeyError."""
    census_bytes = _make_census_bytes(
        observed_extra={"era_vendor_ticker_text": ("twenty-nine", "EXACT")}
    )
    census_hash = _census_hash(census_bytes)
    paths = _bundle(
        tmp_path,
        census_bytes=census_bytes,
        rules=_rules(
            census_hash,
            expression={"op": "max", "args": [{"fact": "era_vendor_ticker_text"}, 1]},
        ),
    )
    with _out_root() as out:
        with pytest.raises(DerivationMismatchError, match="not a strict int"):
            _build(paths, out)


# ---- round-3 (finding 1): a boolean observed fact cannot even parse ---------------
#
# Round-3 review fix (2026-08-23): CensusFact.v lived under a NON-strict
# config, so `v: true` was coerced to int 1 at parse time and the
# derivation-time `type(observed.v) is int` gates saw an already-coerced
# int. The attack census below carries `v: true` on disk and is
# self-consistent UNDER THE OLD LAX PARSER (its content hash is the one the
# coerced model computes), so nothing but the parse boundary can refuse it.
# The refusing layer is CensusFact validation itself — step 1 of the builder
# wraps that ValidationError in StaleCensusError; no DerivationMismatchError
# is ever reached.


def _canonical_census_digest(doc: dict) -> str:
    """census_content_sha256 over a RAW dict (same bytes canonical_bytes emits)."""
    core = {**doc, "content_sha256": ""}
    return _sha256(
        CENSUS_DOMAIN + json.dumps(core, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )


def test_boolean_referenced_fact_refused_at_census_parse(tmp_path: Path) -> None:
    doc = json.loads(_make_census_bytes())
    fact = doc["values"]["observed_census_fact"]["era_observed_masters"]
    fact["v"] = True
    # Self-consistency under the OLD lax parser: the declared hash is the
    # one the coerced (v=1) model computes, so only strict parsing refuses.
    coerced = json.loads(json.dumps(doc))  # true survives the copy
    coerced["values"]["observed_census_fact"]["era_observed_masters"]["v"] = 1
    census_bytes = json.dumps({**doc, "content_sha256": _canonical_census_digest(coerced)})
    census_hash = _census_hash(census_bytes.encode("utf-8"))
    # max(<fact>, 1): under the old coercion the boolean fact parses as 1,
    # the rule computes 1, and the owner-supplied 1 matches — a proposal
    # DERIVED FROM A BOOLEAN is emitted. Strict parsing must refuse earlier.
    paths = _bundle(
        tmp_path,
        census_bytes=census_bytes.encode("utf-8"),
        owner_values=_owner_values(census_hash, flow_value=1),
        rules=_rules(
            census_hash, expression={"op": "max", "args": [{"fact": "era_observed_masters"}, 1]}
        ),
    )
    with _out_root() as out:
        with pytest.raises(StaleCensusError, match="census invalid or tampered") as exc_info:
            _build(paths, out)
    # The refusing layer is the CensusFact parse boundary (a pydantic
    # ValidationError naming the field), surfaced through the step-1 wrapper.
    assert "era_observed_masters" in str(exc_info.value)
    assert "bool" in str(exc_info.value)


# ---- round-3 (finding 3): every output is re-resolved under the out root ---------
#
# Round-3 review fix (2026-08-23): the builder resolved + checked only the
# OUT ROOT; the derived hash directory and the output filenames were used
# as-is, so a precreate of <out-root>/<census-hash12> as a directory symlink
# outside artifacts/ (worse: one output filename symlinked to a tracked
# file) was written straight through.


def test_hash_dir_symlink_escape_refused(tmp_path: Path) -> None:
    """Precreate <out-root>/<census-hash12> as a directory symlink OUTSIDE
    artifacts/: the builder refuses naming both paths and writes nothing
    through the link."""
    census_bytes = _make_census_bytes()
    paths = _bundle(tmp_path, census_bytes=census_bytes)
    escape = tmp_path / "escape"
    escape.mkdir()
    with _out_root() as out:
        (out / _census_hash(census_bytes)[:12]).symlink_to(escape)
        with pytest.raises(OutputRefusedError, match="outside") as exc_info:
            _build(paths, out)
    message = str(exc_info.value)
    assert str(escape.resolve()) in message, "the refusal names where the link resolves"
    assert _census_hash(census_bytes)[:12] in message, "the refusal names the requested path"
    assert list(escape.iterdir()) == [], "nothing was written through the symlink"


def test_output_filename_symlink_to_tracked_file_refused(tmp_path: Path) -> None:
    """Precreate the proposed-protocol FILENAME as a symlink to the tracked
    research_protocol.yaml: the builder refuses and the tracked file is
    byte-identical afterwards."""
    census_bytes = _make_census_bytes()
    paths = _bundle(tmp_path, census_bytes=census_bytes)
    before = PROTOCOL_PATH.read_bytes()
    with _out_root() as out:
        out_dir = out / _census_hash(census_bytes)[:12]
        out_dir.mkdir()
        (out_dir / "protocol-0.2.1-proposed.yaml").symlink_to(PROTOCOL_PATH)
        with pytest.raises(OutputRefusedError, match="outside") as exc_info:
            _build(paths, out)
    assert str(PROTOCOL_PATH) in str(exc_info.value)
    assert PROTOCOL_PATH.read_bytes() == before, "the tracked file was not written through"


# ---- round-4 (finding 2): confinement must see HARD-LINK aliasing ------------------
#
# Round-4 review fix (2026-08-23): Path.resolve() detects symlinks but not
# hard-link aliasing. A precreate of an output FILENAME as a hard link to
# research_protocol.yaml (same filesystem) keeps its resolved path inside the
# output root, so confinement passed and write_text() truncated the shared
# inode — the tracked protocol was overwritten while the builder succeeded.


def test_output_filename_hard_link_to_tracked_file_refused(tmp_path: Path) -> None:
    """Precreate the proposed-protocol FILENAME as a HARD LINK to the tracked
    research_protocol.yaml: the builder must refuse naming the path and its
    link count, and the tracked file must stay byte-identical. A NORMAL
    output file (nlink == 1) still writes."""
    census_bytes = _make_census_bytes()
    paths = _bundle(tmp_path, census_bytes=census_bytes)
    before = PROTOCOL_PATH.read_bytes()
    with _out_root() as out:
        out_dir = out / _census_hash(census_bytes)[:12]
        out_dir.mkdir()
        linked = out_dir / "protocol-0.2.1-proposed.yaml"
        linked.hardlink_to(PROTOCOL_PATH)
        assert linked.stat().st_nlink == 2, "fixture: the inode is shared with the tracked file"
        with pytest.raises(OutputRefusedError, match="hard link") as exc_info:
            _build(paths, out)
        assert str(linked) in str(exc_info.value), "the refusal names the output path"
        assert "2" in str(exc_info.value), "the refusal names the observed link count"
        assert PROTOCOL_PATH.read_bytes() == before, "the tracked file was not truncated"
        # A normal output file (nlink == 1) still writes: same out root, the
        # hard link removed, the build succeeds.
        linked.unlink()
        packet = _build(paths, out)
        assert packet.landed is False
        assert (out_dir / "protocol-0.2.1-proposed.yaml").stat().st_nlink == 1
        assert PROTOCOL_PATH.read_bytes() == before
    assert PROTOCOL_PATH.read_bytes() == before, "still byte-identical at teardown"


# ---- round-5 (finding 1): custody must span the WRITE, not just the check ---------
#
# Round-5 review fix (2026-08-24): _refuse_shared_inode and write_text were
# SEPARATE operations — an interleaving process could hard-link the tracked
# research_protocol.yaml at the proposed-path name AFTER the check passed and
# BEFORE the write, and write_text truncated the shared tracked inode. The
# write now holds custody end to end: a sibling temp file inside the confined
# dir, fstat-checked (nlink == 1), then os.replace — which swaps the DIRECTORY
# ENTRY, so the planted link is unlinked and the tracked inode is never
# written through.


def test_hard_link_planted_between_check_and_write_cannot_truncate_the_tracked_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Interleaving attack: the hard link lands AFTER the shared-inode check
    and BEFORE the write (simulated by wrapping the real check — the wrapper
    plants the link once the check has passed). Pre-fix the tracked
    research_protocol.yaml is truncated through the shared inode while the
    builder succeeds; post-fix the build completes via the directory-entry
    swap and the tracked file stays byte-identical."""
    census_bytes = _make_census_bytes()
    paths = _bundle(tmp_path, census_bytes=census_bytes)
    before = PROTOCOL_PATH.read_bytes()
    real_check = amd._refuse_shared_inode
    planted: list[Path] = []

    def check_then_plant_link(path: Path) -> None:
        real_check(path)  # the check passed: the interleaving window opens HERE
        if not planted:  # one plant, at the first output name (the proposal)
            os.link(PROTOCOL_PATH, path)
            planted.append(path)

    monkeypatch.setattr(amd, "_refuse_shared_inode", check_then_plant_link)
    with _out_root() as out:
        packet = _build(paths, out)
        assert packet.landed is False
        assert PROTOCOL_PATH.read_bytes() == before, (
            "the tracked protocol was truncated through the hard link planted"
            " between the inode check and the write"
        )
        swapped_in = planted[0]
        assert swapped_in.exists(), "the proposal was published at its own name"
        assert swapped_in.stat().st_nlink == 1, (
            "os.replace swapped the directory entry: the planted link is gone,"
            " the published file is a sole link"
        )
        assert swapped_in.read_bytes() != before, "the published file is the proposal"
    assert PROTOCOL_PATH.read_bytes() == before, "still byte-identical at teardown"


# ---- round-6 (finding 1): the temp NAME is unpredictable and the publish is verified -
#
# Round-6 review fix (2026-08-24): _write_exclusive's temp name was PREDICTABLE
# (".{name}.{pid}.tmp") and the publish was unverified. An attacker renamed the
# temp file away mid-write (the open fd keeps writing the RENAMED inode; fstat
# still sees nlink == 1), planted attacker bytes at the original temp name, and
# os.replace(tmp, path) published the ATTACKER inode while the builder returned
# its legitimate in-memory packet. The temp name is now mkstemp-random and the
# PUBLISHED inode must be the inode that was written: (st_dev, st_ino) captured
# from fstat before close, re-checked with os.stat(path) after os.replace.


def test_stolen_temp_name_publishing_attacker_bytes_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Interleaving attack: the steal+plant happens between the builder's
    fsync/fstat and its os.replace (simulated by wrapping os.replace — the
    wrapper receives the temp path, renames it away, plants attacker bytes at
    the temp name, then performs the real replace). Pre-fix the builder
    succeeds while the PUBLISHED artifact carries the attacker's bytes;
    post-fix the build refuses naming the inode mismatch, and the temp name
    was never pid-predictable to begin with."""
    census_bytes = _make_census_bytes()
    paths = _bundle(tmp_path, census_bytes=census_bytes)
    attacker = b'{"attacker": "published in place of the builder"}\n'
    seen_temp_names: list[str] = []
    real_replace = os.replace

    def replace_stealing_the_temp(src: object, dst: object) -> object:
        # the interleaving window opens exactly here: the builder's bytes are
        # fsynced and fstat'ed, the publish has not happened yet. The steal
        # targets the FINAL output (amendment-packet.json): the earlier three
        # outputs publish legitimately, `emitted` hashes the legitimate bytes,
        # and the builder returns its legitimate in-memory packet — while the
        # PACKET NAME on disk carries the attacker's inode.
        if Path(str(dst)).name != "amendment-packet.json":
            return real_replace(src, dst)
        seen_temp_names.append(Path(str(src)).name)
        stolen = Path(str(src)).parent / (Path(str(src)).name + ".stolen")
        os.rename(str(src), str(stolen))  # steal the builder's inode AWAY
        Path(str(src)).write_bytes(attacker)  # plant attacker bytes at the temp name
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", replace_stealing_the_temp)
    with _out_root() as out:
        with pytest.raises(OutputRefusedError, match="not the inode") as exc_info:
            _build(paths, out)
        assert "attacker" in str(exc_info.value), (
            "the refusal explains that attacker bytes were published in place"
        )
        # defense in depth, pinned: the temp name was never pid-predictable,
        # so the steal could not have located the temp file by guessing.
        assert seen_temp_names, "the wrapper observed the packet's publish"
        assert str(os.getpid()) not in seen_temp_names[0], (
            "temp names must not embed the pid (the pre-fix name was guessable)"
        )
        assert seen_temp_names[0] != f".amendment-packet.json.{os.getpid()}.tmp"


# ---- round-7 (finding 1): the publish must prove FINAL-NAME and BYTE custody ---------
#
# Round-7 review fix (2026-08-24): the round-6 identity check proved the
# published INODE, never the final NAME or the CONTENT. After os.replace an
# attacker renames the published file to a sibling `.held` (inode unchanged),
# plants `path -> .held` as a SYMLINK, and rewrites `.held` IN PLACE with
# schema-valid protocol YAML that changes content while preserving
# protocol_version, the flow value, and the amendment count: os.stat(path)
# FOLLOWS the link, so the (st_dev, st_ino) check passed, and both the
# round-trip load and `emitted`'s read_bytes went through the link — the
# builder attested attacker YAML. The publish verification now lstats the
# final name (a symlink there is not a regular file), takes identity from
# the LSTAT values, and reads the full bytes back through an O_NOFOLLOW fd
# that must equal the text this function wrote.


def test_final_name_swapped_to_symlink_with_inplace_rewrite_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Interleaving attack on the proposal's publish: the wrapper performs the
    real os.replace, THEN renames the published file to `.held`, plants the
    output name as a symlink to it, and rewrites `.held` IN PLACE with
    modified-but-schema-plausible YAML (protocol_version, the flow value, and
    the amendment count all preserved). Pre-fix the build SUCCEEDS while the
    on-disk artifact carries the attacker's bytes; post-fix the builder
    refuses naming the final name, and no packet is emitted."""
    census_bytes = _make_census_bytes()
    paths = _bundle(tmp_path, census_bytes=census_bytes)
    real_replace = os.replace
    armed = {"done": False}

    def replace_then_swap_final_name(src: object, dst: object) -> object:
        result = real_replace(src, dst)  # the legitimate publish happens
        dst_path = Path(str(dst))
        if armed["done"] or dst_path.name != "protocol-0.2.1-proposed.yaml":
            return result
        armed["done"] = True  # the window: published, not yet verified
        held = dst_path.parent / (dst_path.name + ".held")
        os.rename(dst_path, held)  # the inode moves to .held unchanged
        dst_path.symlink_to(held.name)  # path -> .held
        # rewrite .held IN PLACE (same inode, new content): schema-valid,
        # version/flow/amendment-count preserved, only the record text changes
        doc = yaml.safe_load(held.read_text(encoding="utf-8"))
        doc["meta"]["amendments"][-1]["decision"] = "ATTACKER: rewritten in place"
        with open(held, "w", encoding="utf-8") as handle:
            handle.write(yaml.safe_dump(doc, sort_keys=False, default_flow_style=False, width=1000))
        return result

    monkeypatch.setattr(os, "replace", replace_then_swap_final_name)
    with _out_root() as out:
        out_dir = out / _census_hash(census_bytes)[:12]
        published = out_dir / "protocol-0.2.1-proposed.yaml"
        try:
            with pytest.raises(OutputRefusedError, match="not a regular file") as exc_info:
                _build(paths, out)
            assert "protocol-0.2.1-proposed.yaml" in str(exc_info.value), (
                "the refusal names the final name that is not a regular file"
            )
            assert not (out_dir / "amendment-packet.json").exists(), (
                "no packet was emitted attesting the swapped artifact"
            )
        finally:
            # never leave a symlink under artifacts/ (the harness copytree
            # crashes on those), and never leave the attacker's .held either.
            with contextlib.suppress(OSError):
                published.unlink()
            with contextlib.suppress(OSError):
                (out_dir / (published.name + ".held")).unlink()


def test_published_bytes_rewritten_in_place_without_a_symlink_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The byte-custody half of the same fix, pinned independently: the final
    name stays a REAL file (no symlink, same inode) and the attacker only
    rewrites its content in place between the publish and the verification
    (simulated by wrapping os.replace — the wrapper rewrites the published
    bytes after the real replace). lstat identity alone cannot see this;
    the readback must. Pre-fix the build succeeds attesting the rewritten
    bytes; post-fix it refuses naming both contents."""
    census_bytes = _make_census_bytes()
    paths = _bundle(tmp_path, census_bytes=census_bytes)
    real_replace = os.replace
    armed = {"done": False}

    def replace_then_rewrite_in_place(src: object, dst: object) -> object:
        result = real_replace(src, dst)
        dst_path = Path(str(dst))
        if armed["done"] or dst_path.name != "protocol-0.2.1-proposed.yaml":
            return result
        armed["done"] = True
        # same NAME, same INODE, new CONTENT (schema-plausible as above)
        doc = yaml.safe_load(dst_path.read_text(encoding="utf-8"))
        doc["meta"]["amendments"][-1]["changes"] = "ATTACKER: content rewritten in place"
        with open(dst_path, "w", encoding="utf-8") as handle:
            handle.write(yaml.safe_dump(doc, sort_keys=False, default_flow_style=False, width=1000))
        return result

    monkeypatch.setattr(os, "replace", replace_then_rewrite_in_place)
    with _out_root() as out:
        with pytest.raises(OutputRefusedError, match="does not hold the bytes") as exc_info:
            _build(paths, out)
        message = str(exc_info.value)
        assert "protocol-0.2.1-proposed.yaml" in message
        assert "refusing to attest either content" in message


# ---- round-6 (finding 2): an output path that IS a symlink is refused ---------------
#
# Round-6 review fix (2026-08-24): `protocol-0.2.1-proposed.yaml -> amendment-
# packet.json` (both inside the permitted hash dir) resolves IN ROOT, so
# confinement accepted it; the proposal was then written under the RESOLVED
# (packet) name, the packet write overwrote it, and the builder SUCCEEDED with
# two own artifacts aliased to one file and `emitted` carrying wrong hashes.
# An output path that is ITSELF a symlink is never legitimate for this
# builder, whatever its target.


def test_in_root_output_symlink_aliasing_two_artifacts_refused(tmp_path: Path) -> None:
    """The reviewer's precreate: the proposal FILENAME symlinked to the
    packet FILENAME inside the same permitted hash dir. Pre-fix the build
    succeeds aliased (the proposal lands on the packet's name and is then
    overwritten; the yaml name keeps aliasing the packet; `emitted` attests
    the wrong bytes); post-fix the build refuses naming the symlink and
    writes NOTHING — not through the link, not under its target."""
    census_bytes = _make_census_bytes()
    paths = _bundle(tmp_path, census_bytes=census_bytes)
    with _out_root() as out:
        out_dir = out / _census_hash(census_bytes)[:12]
        out_dir.mkdir()
        link = out_dir / "protocol-0.2.1-proposed.yaml"
        link.symlink_to(out_dir / "amendment-packet.json")
        try:
            with pytest.raises(OutputRefusedError, match="symlink") as exc_info:
                _build(paths, out)
            assert "protocol-0.2.1-proposed.yaml" in str(exc_info.value), (
                "the refusal names the output path"
            )
            assert "amendment-packet.json" in str(exc_info.value), (
                "the refusal names the in-root target it aliases"
            )
            assert not (out_dir / "amendment-packet.json").exists(), (
                "nothing was written through the link onto the packet name"
            )
        finally:
            # never leave a (dangling) symlink under artifacts/ — the harness
            # copytree crashes on those.
            with contextlib.suppress(FileNotFoundError):
                link.unlink()


# ---- round-5 (finding 2): zero INCOMPLETE pairs is not wholeness on its own --------
#
# Round-5 review fix (2026-08-24): the builder refused only
# `incomplete > 0`, but the census CLI's exit-0 rule is BOTH zero
# INCOMPLETE_CLASSES pairs AND masters observed == expected_masters. A
# census whose sealed manifest holds one valid master OUTSIDE the universe
# (masters_observed 3046 vs expected 3045, zero incomplete pairs) exits 5 —
# yet the builder used to emit a proposal from it.


def test_builder_refuses_masters_observed_above_expected(tmp_path: Path) -> None:
    """The reviewer's scenario at census scale: every universe pair COMPLETE
    but one extra valid master captured outside the universe. The census CLI
    exits 5 for exactly this; the builder must refuse naming both counts and
    emit nothing."""
    census_bytes = _make_census_bytes(
        observed={
            "era_observed_masters": 3045,
            "era_as_of_fridays": 105,
            "masters_observed": 3046,  # one valid master outside the universe
        }
    )
    paths = _bundle(tmp_path, census_bytes=census_bytes)
    with _out_root() as out:
        with pytest.raises(StaleCensusError, match="masters observed 3046 != expected 3045"):
            _build(paths, out)
        assert list(out.iterdir()) == [], "no packet was emitted for an un-whole census"


def test_builder_refuses_masters_observed_below_expected(tmp_path: Path) -> None:
    """Fewer observed masters than the universe declares is equally un-whole
    (a pair can sit outside INCOMPLETE_CLASSES only via holiday classes while
    its master never parsed)."""
    census_bytes = _make_census_bytes(
        observed={
            "era_observed_masters": 3045,
            "era_as_of_fridays": 105,
            "masters_observed": 3044,
        }
    )
    paths = _bundle(tmp_path, census_bytes=census_bytes)
    with _out_root() as out:
        with pytest.raises(StaleCensusError, match="masters observed 3044 != expected 3045"):
            _build(paths, out)


def test_builder_refuses_a_census_that_cannot_attest_its_masters_count(
    tmp_path: Path,
) -> None:
    """No strict-int `masters_observed` observation (the canonical producer
    always emits one): wholeness cannot be attested, so the builder refuses
    rather than assume the count matches."""
    census_bytes = _make_census_bytes(
        observed={"era_observed_masters": 3045, "era_as_of_fridays": 105}
    )
    paths = _bundle(tmp_path, census_bytes=census_bytes)
    with _out_root() as out:
        with pytest.raises(StaleCensusError, match="masters_observed"):
            _build(paths, out)


# ---- round-3 (finding 4): input hashes attest the bytes that were PARSED ---------
#
# Round-3 review fix (2026-08-23): the models were parsed at steps 1/3/4/5
# but the packet re-read each input fresh at packet time, so the hashes
# described bytes the builder may not have consumed. Every input is now
# read ONCE and both the parse and the hash consume those same bytes.


def test_packet_hashes_attest_the_consumed_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Attack a re-read: after the first read of the owner-values file,
    every further read (any method) returns a different, valid doc. The
    packet must still attest the bytes that produced the parsed model."""
    census_bytes = _make_census_bytes()
    paths = _bundle(tmp_path, census_bytes=census_bytes)
    owner_path = paths["owner_values"]
    original = owner_path.read_bytes()
    original_doc = json.loads(original)
    swapped = json.dumps(
        {
            **original_doc,
            "values": [
                {**original_doc["values"][0], "value": FLOW_DERIVED + 239},
                *original_doc["values"][1:],
            ],
        }
    ).encode("utf-8")
    assert swapped != original

    reads = {"owner": 0}
    real_read_bytes = Path.read_bytes
    real_read_text = Path.read_text

    def read_bytes_attacking(self: Path) -> bytes:
        if self == owner_path:
            reads["owner"] += 1
            return original if reads["owner"] == 1 else swapped
        return real_read_bytes(self)

    def read_text_attacking(self: Path, *args: object, **kwargs: object) -> str:
        if self == owner_path:
            reads["owner"] += 1
            body = original if reads["owner"] == 1 else swapped
            return body.decode("utf-8")
        return real_read_text(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_bytes", read_bytes_attacking)
    monkeypatch.setattr(Path, "read_text", read_text_attacking)
    with _out_root() as out:
        packet = _build(paths, out)
    # the parse consumed the ORIGINAL bytes (761, not 761+239) …
    assert packet.flow_min_session_volume == FLOW_DERIVED
    # … and the packet attests exactly those bytes, read exactly once.
    assert packet.inputs.owner_values_file_sha256 == _sha256(original)
    assert reads["owner"] == 1, "the owner-values file is read once, by construction"
