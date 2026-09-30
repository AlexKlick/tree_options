"""Read-only quant cockpit projection from Research Lab and runtime artifacts.

No credentials, SDK, order submit method, or broker session is imported here.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException

from tree_options.research.catalog.quant import STRATEGIES
from tree_options.research.paths import workspace_root
from tree_options.research.quant import digest
from tree_options.research.runstate.store import RunstateStore, open_runstate_store


def _digest(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _theory_metrics(source: Any) -> dict[str, Any]:
    """Expose aggregate modeled returns; refuse contradictory evidence claims."""
    if not isinstance(source, dict) or any(
        source.get(key) is not False for key in ("execution_authorized", "exact_external_economics")
    ):
        raise ValueError("Invalid theory evidence flags")
    if (
        source.get("evidence_kind") != "BACKTEST"
        or source.get("capital_policy") != "independent_equal_capital_roundtrips"
        or source.get("max_drawdown_scope") != "endpoint_loss_only"
        or "compound_nav" not in source
        or source["compound_nav"] is not None
    ):
        raise ValueError("Unsupported theory metric meaning")
    total, scored = source.get("period_count"), source.get("scored_period_count")
    if type(total) is not int or type(scored) is not int or not 0 <= scored <= total or total < 1:
        raise ValueError("Invalid theory period counts")
    complete = scored == total
    if source.get("disposition") != ("SCORED" if complete else "INCOMPLETE"):
        raise ValueError("Contradictory theory disposition")
    fields = ("mean_net_return", "max_drawdown", "turnover", "fees")
    for key in fields:
        value = source[key]
        if not complete:
            if value is not None:
                raise ValueError("Incomplete theory result has scored metrics")
            continue
        if not isinstance(value, str):
            raise ValueError("Theory metrics require decimal strings")
        numeric = Decimal(value)
        if not numeric.is_finite() or not math.isfinite(float(numeric)):
            raise ValueError("Theory metrics must be finite")
        if key != "mean_net_return" and numeric < 0:
            raise ValueError("Negative loss, turnover or fee")
    keys = (
        "evidence_kind",
        "capital_policy",
        "disposition",
        "execution_authorized",
        "exact_external_economics",
        "period_count",
        "scored_period_count",
        "compound_nav",
        "max_drawdown_scope",
        *fields,
    )
    return {key: source[key] for key in keys}


def _theory_campaign(source: dict[str, Any], store: RunstateStore) -> dict[str, Any]:
    """Project only persisted theory records and their persisted DAG, without evaluation."""
    if any(
        source.get(key) is not False
        for key in ("execution_authorized", "exact_external_economics", "live_money")
    ):
        raise ValueError("Invalid theory effect claims")
    evidence = {
        "synthetic_fixture": "synthetic_backtest",
        "user_supplied_unqualified": "simulated_execution",
    }
    if (
        source.get("data_class") not in evidence
        or source.get("evidence_kind") != evidence[source["data_class"]]
        or source.get("registration") != "exploratory_retrospective"
        or source.get("objective")
        != "mean_next_session_net_return-minus-endpoint_loss-and-turnover"
        or not _digest(source.get("campaign_id"))
        or not isinstance(source.get("hypothesis"), str)
        or not source["hypothesis"].strip()
    ):
        raise ValueError("Invalid theory identity or evidence class")
    for key, minimum, maximum in (("candidate_count", 1, 32), ("reflection_calls", 0, 4)):
        if type(source.get(key)) is not int or not minimum <= source[key] <= maximum:
            raise ValueError("Invalid theory candidate budget")
    winner = source["winner"]
    if (
        not isinstance(winner, dict)
        or winner.get("strategy_id")
        not in ("equal_weight_us_equities", "momentum_12_1", "hqm_1_3_6_12")
        or not isinstance(winner.get("version_id"), str)
        or not winner["version_id"]
        or not isinstance(winner.get("parameters"), dict)
        or set(winner["parameters"]) - {"top_n"}
    ):
        raise ValueError("Invalid theory winner")
    if "top_n" in winner["parameters"]:
        top_n = winner["parameters"]["top_n"]
        if type(top_n) is not int or not 1 <= top_n <= 1000:
            raise ValueError("Invalid theory winner parameters")
    version = store.get("quant_version", winner["version_id"])
    if (
        not isinstance(version, dict)
        or not isinstance(version.get("definition"), dict)
        or version["definition"].get("strategy_id") != winner["strategy_id"]
        or version.get("config") != winner["parameters"]
        or version.get("config_sha256") != digest(winner["parameters"])
        or version.get("version_id") != winner["version_id"]
        or winner["version_id"]
        != (
            f"{winner['strategy_id']}/v{version['definition']['version']}/"
            f"{digest({key: value for key, value in version.items() if key != 'version_id'})}"
        )
    ):
        raise ValueError("Theory winner does not match a persisted strategy version")
    holdout = {key: _theory_metrics(source["holdout"][key]) for key in ("candidate", "control")}
    complete = all(row["disposition"] == "SCORED" for row in holdout.values())
    if source.get("disposition") != ("REVIEW_REQUIRED" if complete else "HOLDOUT_INCOMPLETE"):
        raise ValueError("Contradictory theory holdout disposition")
    limitations = source["limitations"]
    if (
        not isinstance(limitations, list)
        or not limitations
        or any(not isinstance(item, str) or not item.strip() for item in limitations)
    ):
        raise ValueError("Missing theory limitations")
    graph = source["graph"]
    if not isinstance(graph, list) or not graph:
        raise ValueError("Missing theory DAG")
    seen: set[str] = set()
    nodes = []
    for node in graph:
        if (
            not isinstance(node, dict)
            or node.get("schema") != "quant-research-node/1"
            or node.get("campaign_id") != source["campaign_id"]
            or node.get("execution_authorized") is not False
            or not _digest(node.get("node_id"))
            or not _digest(node.get("payload_sha256"))
            or not isinstance(node.get("stage"), str)
            or not node["stage"]
            or not isinstance(node.get("parents"), list)
            or any(not isinstance(parent, str) or parent not in seen for parent in node["parents"])
            or node["node_id"] in seen
            or store.get("quant_provenance", node["node_id"]) != node
        ):
            raise ValueError("Invalid or unpersisted theory DAG node")
        seen.add(node["node_id"])
        if "payload_ref" in node:
            payload = store.get("quant_provenance", node["payload_sha256"])
            if (
                node["payload_ref"] != f"runstate:quant_provenance/{node['payload_sha256']}"
                or not isinstance(payload, dict)
                or payload.get("schema") != "quant-research-payload/1"
                or not isinstance(payload.get("payload"), dict)
                or digest(payload["payload"]) != node["payload_sha256"]
            ):
                raise ValueError("Invalid theory payload reference")
        nodes.append(
            {
                key: node[key]
                for key in (
                    "schema",
                    "node_id",
                    "campaign_id",
                    "stage",
                    "payload_sha256",
                    "parents",
                    "execution_authorized",
                )
            }
        )
        if "payload_ref" in node:
            nodes[-1]["payload_ref"] = node["payload_ref"]
    return {
        **{
            key: source[key]
            for key in (
                "schema",
                "campaign_id",
                "hypothesis",
                "data_class",
                "evidence_kind",
                "registration",
                "candidate_count",
                "reflection_calls",
                "disposition",
                "objective",
                "limitations",
                "execution_authorized",
                "exact_external_economics",
                "live_money",
            )
        },
        "winner": {key: winner[key] for key in ("strategy_id", "parameters", "version_id")},
        "holdout": holdout,
        "graph": nodes,
    }


def attach(
    app: FastAPI, *, workspace: Path | None = None, execution_state: Path | None = None
) -> None:
    workspace = workspace or workspace_root()
    execution_state = execution_state or Path(
        os.environ.get("TREX_SNAPTRADE_STATE", Path.home() / ".local/state/trex/snaptrade-paper")
    )

    @app.get("/api/research/quant")
    def quant_projection() -> dict[str, Any]:
        results: list[dict[str, Any]] = []
        versions: list[dict[str, Any]] = []
        comparisons: list[dict[str, Any]] = []
        campaigns: list[dict[str, Any]] = []
        theory_campaigns: list[dict[str, Any]] = []
        if (workspace / "runstate.sqlite3").exists():
            try:
                with open_runstate_store(workspace) as store:
                    store.verify()
                    results = list(store.all("quant_experiment"))
                    versions = list(store.all("quant_version"))
                    campaigns = list(store.all("quant_provenance"))
                    theory_campaigns = [
                        _theory_campaign(row, store)
                        for row in store.all("result")
                        if row.get("schema") == "quant-theory-result/1"
                    ]
                    comparisons = [
                        row
                        for row in store.all("comparison_row")
                        if row.get("kind") == "quant_comparison"
                    ]
            except Exception:
                raise HTTPException(503, "Research artifacts failed integrity validation") from None
        projection_file = execution_state / "projection.json"
        execution: dict[str, Any] = {
            "state": "NOT_OBSERVED",
            "environment": "BROKER PAPER",
            "live_money": False,
        }
        if projection_file.exists():
            try:
                source = json.loads(projection_file.read_text())
                if source["environment"] != "BROKER PAPER" or source["live_money"] is not False:
                    raise ValueError
                at = datetime.fromisoformat(source["observed_at"])
                if at.tzinfo is None:
                    raise ValueError
                age = (datetime.now(UTC) - at).total_seconds()
                if age < 0:
                    raise ValueError
                if type(source["ready"]) is not bool or type(source["owner_held"]) is not bool:
                    raise ValueError
                rows = source.get("executions", [])
                if not isinstance(rows, list):
                    raise ValueError
                for row in rows:
                    if not isinstance(row, dict):
                        raise ValueError
                    if any(
                        not isinstance(row[key], str) or not row[key]
                        for key in ("intent_id", "state", "broker_state", "evidence_verdict")
                    ):
                        raise ValueError
                    if any(
                        type(row[key]) is not bool
                        for key in ("reconciliation_clean", "exact_economics")
                    ):
                        raise ValueError
                    if not isinstance(row["findings"], list) or any(
                        not isinstance(finding, str) for finding in row["findings"]
                    ):
                        raise ValueError
                    if not isinstance(row["records"], list) or any(
                        not isinstance(record, dict) for record in row["records"]
                    ):
                        raise ValueError
                execution = {
                    k: source.get(k)
                    for k in (
                        "environment",
                        "account_alias",
                        "owner_epoch",
                        "owner_held",
                        "mandate",
                        "risk",
                        "executions",
                        "observed_at",
                        "live_money",
                    )
                }
                execution["state"] = "STALE" if age > 30 else "OBSERVED"
                execution["executions"] = rows
                execution["ready"] = (
                    bool(source["ready"]) and age <= 30 and bool(source["owner_held"])
                )
                execution["age_seconds"] = age
                provenance = execution_state / "provenance.jsonl"
                execution["provenance"] = (
                    [json.loads(line) for line in provenance.read_text().splitlines()]
                    if provenance.exists()
                    else []
                )
                if any(row.get("environment") != "broker_paper" for row in execution["provenance"]):
                    raise ValueError
            except (ValueError, KeyError, TypeError):
                raise HTTPException(503, "Broker-paper projection is invalid") from None
        return {
            "strategies": [asdict(s) for s in STRATEGIES],
            "versions": versions,
            "experiments": results,
            "comparisons": comparisons,
            "campaigns": campaigns,
            "theory_campaigns": theory_campaigns,
            "execution": execution,
            "evidence_classes": [
                "BACKTEST",
                "DETERMINISTIC REPLAY",
                "SIMULATED EXECUTION",
                "BROKER PAPER",
                "LIVE",
            ],
            "live_money": False,
            "execution_authorized": False,
        }
