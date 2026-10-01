"""Proposal structure checker, NOT an authorization service or trading runtime.

Checks only the supplied proposal schema, dependency graph, declared bindings,
fixture hashes and a few necessary effect-boundary conditions. It cannot verify
real identities, grants, risk, freshness, broker behavior or scientific evidence.
"""

from __future__ import annotations

import hashlib
import json
from collections import deque
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parent


class ModelError(ValueError):
    """The design fixture violates a checked structural rule."""


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def validate(
    plan: dict[str, Any], registry: dict[str, Any], schema: dict[str, Any]
) -> dict[str, Any]:
    """Validate a proposal fixture, without issuing permission or any effect."""
    Draft202012Validator.check_schema(schema)
    errors = sorted(Draft202012Validator(schema).iter_errors(plan), key=lambda e: str(e.path))
    if errors:
        raise ModelError(f"schema: {errors[0].message}")

    nodes = {n["id"]: n for n in plan["nodes"]}
    if len(nodes) != len(plan["nodes"]):
        raise ModelError("duplicate_node_id")
    artifacts = {a["id"]: a for a in plan["artifacts"]}
    if len(artifacts) != len(plan["artifacts"]):
        raise ModelError("duplicate_artifact_id")
    if nodes.keys() & artifacts.keys():
        raise ModelError("ambiguous_object_id")
    for a in artifacts.values():
        if hashlib.sha256(canonical_bytes(a["payload"])).hexdigest() != a["sha256"]:
            raise ModelError("fixture_payload_hash_mismatch")
    ops = {(o["operation"], o["operation_version"]): o for o in registry["operations"]}
    if len(ops) != len(registry["operations"]):
        raise ModelError("duplicate_operation_contract")

    indegree = {key: 0 for key in nodes}
    children: dict[str, list[str]] = {key: [] for key in nodes}
    for n in nodes.values():
        parents = [d["node_id"] for d in n["dependencies"]]
        if len(parents) != len(set(parents)):
            raise ModelError("duplicate_dependency")
        for parent in parents:
            if parent not in nodes:
                raise ModelError("unknown_dependency")
            indegree[n["id"]] += 1
            children[parent].append(n["id"])
    ready = deque(sorted(key for key, count in indegree.items() if count == 0))
    order: list[str] = []
    ancestors: dict[str, set[str]] = {key: set() for key in nodes}
    while ready:
        current = ready.popleft()
        order.append(current)
        for child in children[current]:
            ancestors[child].add(current)
            ancestors[child].update(ancestors[current])
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)
    if len(order) != len(nodes):
        raise ModelError("scheduling_cycle")

    for n in nodes.values():
        op = ops.get((n["operation"], n["operation_version"]))
        if op is None:
            raise ModelError("unregistered_operation")
        for field in ("owner_role", "effect_class", "target_environment", "outputs"):
            if n[field] != op[field]:
                raise ModelError(f"operation_contract_mismatch:{field}")
        actual_types = {key: b["expected_type"] for key, b in n["inputs"].items()}
        if actual_types != op["input_types"]:
            raise ModelError("input_port_contract_mismatch")
        for b in n["inputs"].values():
            if "artifact_id" in b:
                a = artifacts.get(b["artifact_id"])
                if a is None:
                    raise ModelError("unknown_artifact")
                if a["kind"] != b["expected_type"]:
                    raise ModelError("artifact_type_mismatch")
            else:
                source_id = b["producer_node_id"]
                if source_id not in ancestors[n["id"]]:
                    raise ModelError("input_producer_not_scheduled_ancestor")
                ports = nodes[source_id]["outputs"]
                if ports.get(b["output_name"]) != b["expected_type"]:
                    raise ModelError("output_port_type_mismatch")
        if n["effect_class"] == "paper_effect":
            necessary = {
                "mandate_active",
                "permit_active",
                "account_bound",
                "current_owner",
                "fresh_risk_snapshot",
                "fresh_quotes",
                "effect_hash_bound",
            }
            if not necessary <= set(n["required_guards"]):
                raise ModelError("paper_guard_missing")
            if n["owner_role"] != "broker_gateway" or n["target_environment"] != "paper":
                raise ModelError("paper_owner_or_environment")
            if n["retry"]["mode"] != "reconcile_before_retry":
                raise ModelError("paper_retry_requires_reconciliation")
            if n["on_failure"] != "preserve_uncertain_effect":
                raise ModelError("paper_failure_must_preserve_uncertainty")
            if not {"EffectPermit", "OrderIntent", "RiskReservation"} <= set(actual_types.values()):
                raise ModelError("paper_authority_binding_missing")
        if n["effect_class"] == "authority_change":
            if (
                n["owner_role"] != "operator"
                or "authenticated_operator" not in n["required_guards"]
            ):
                raise ModelError("authority_change_owner_missing")
    for link in plan["non_scheduling_links"]:
        if link["from"] not in nodes or link["to"] not in nodes:
            raise ModelError("unknown_link_endpoint")

    return {
        "schema": "trex-model-check-receipt/0.1",
        "valid_structure": True,
        "node_count": len(nodes),
        "dependency_count": sum(len(n["dependencies"]) for n in nodes.values()),
        "fixture_artifact_count": len(artifacts),
        "topological_order": order,
        "execution_authorized": False,
        "broker_contacted": False,
        "scope": "Static proposal/fixture conformance only; no runtime, authorization or financial correctness certification.",
    }


def load_design_example() -> tuple[dict[str, Any], dict[str, Any]]:
    """Return the packet's immutable synthetic plan and static check receipt.

    The fixture registry is intentionally kept inside this function. It is
    never an operation dispatcher or a source of runtime authority.
    """
    plan = json.loads((ROOT / "research-to-paper.plan.json").read_text())
    registry = json.loads((ROOT / "operation-registry.fixture.json").read_text())
    schema = json.loads((ROOT / "action-plan.schema.json").read_text())
    return plan, validate(plan, registry, schema)
