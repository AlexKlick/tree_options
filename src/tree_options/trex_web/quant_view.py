"""Read-only quant cockpit projection from Research Lab and runtime artifacts.

No credentials, SDK, order submit method, or broker session is imported here.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException

from tree_options.research.catalog.quant import STRATEGIES
from tree_options.research.paths import workspace_root
from tree_options.research.runstate.store import open_runstate_store


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
        if (workspace / "runstate.sqlite3").exists():
            try:
                with open_runstate_store(workspace) as store:
                    store.verify()
                    results = list(store.all("quant_experiment"))
                    versions = list(store.all("quant_version"))
                    campaigns = list(store.all("quant_provenance"))
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
                age = (datetime.now(UTC) - at).total_seconds()
                if age < 0 or at.tzinfo is None:
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
