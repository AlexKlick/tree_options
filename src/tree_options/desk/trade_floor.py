"""Compact, receipt-bound spectator replay from historical action graphs.

This module reads completed research artifacts. It has no broker imports or
order path. The exported rounds reveal later modeled marks only after the
viewer advances the virtual floor.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

SCHEMA = "desk-trade-floor-replay/1"
TRADERS = {"zai": "Z.ai", "flash": "Z.ai Flash", "minimax": "MiniMax"}


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _inside(root: Path, value: str) -> Path:
    path = Path(value)
    resolved = (path if path.is_absolute() else root / path).resolve(strict=True)
    if not resolved.is_relative_to(root):
        raise ValueError("artifact path escapes run root")
    return resolved


def _read_bound(root: Path, value: str, expected_sha: str) -> dict[str, Any]:
    path = _inside(root, value)
    if _sha(path) != expected_sha:
        raise ValueError("artifact hash mismatch")
    doc = json.loads(path.read_text())
    if not isinstance(doc, dict):
        raise ValueError("artifact document required")
    return doc


def _money(value: Any) -> Decimal:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("invalid modeled amount") from exc
    if not amount.is_finite():
        raise ValueError("nonfinite modeled amount")
    return amount


def _model_graph(root: Path, row: dict[str, Any]) -> dict[str, Any]:
    report = _read_bound(root, row["graph"], row["graph_sha256"])
    summary = _read_bound(root, row["summary"], row["summary_sha256"])
    if (report.get("execution_authorized") is not False
            or summary.get("execution_authorized") is not False
            or report.get("policy") != "external_decisions"
            or summary.get("policy") != "external_decisions"
            or len(report.get("windows", [])) != 1
            or len(summary.get("windows", [])) != 1):
        raise ValueError("unqualified matched graph")
    graph = report["windows"][0]["graph"]
    metrics = summary["windows"][0]
    if graph.get("execution_authorized") is not False:
        raise ValueError("graph authority must be disabled")
    actions = {action["snapshot"]: action for action in graph["actions"]}
    nodes = {node["id"]: node for node in graph["nodes"]}
    exits = {edge["from"]: nodes[edge["to"]]
             for edge in graph["edges"] if edge["kind"] == "later_mark"}
    return {"graph": graph, "metrics": metrics, "actions": actions,
            "nodes": nodes, "exits": exits}


def build_replay(run_dir: Path) -> dict[str, Any]:
    """Export one validated, compact game without raw bars or model responses."""
    root = run_dir.resolve(strict=True)
    source_path = root / "source/manifest.json"
    sample_path = root / "model-sample/manifest.json"
    provider_path = root / "model-sample/provider-manifest.json"
    scope_path = root / "model-sample/ANALYSIS_SCOPE.json"
    replays_path = root / "model-sample/replay-manifest.json"
    source = json.loads(source_path.read_text())
    sample = json.loads(sample_path.read_text())
    providers = json.loads(provider_path.read_text())
    scope = json.loads(scope_path.read_text())
    replays = json.loads(replays_path.read_text())
    if (source.get("execution_authorized") is not False
            or sample.get("execution_authorized") is not False
            or providers.get("execution_authorized") is not False
            or scope.get("execution_authorized") is not False
            or replays.get("execution_authorized") is not False
            or sample.get("code_head") != source.get("code_head")
            or sample.get("source_manifest_sha256") != _sha(source_path)
            or providers.get("sample_manifest_sha256") != _sha(sample_path)
            or scope.get("sample_manifest_sha256") != _sha(sample_path)
            or replays.get("sample_manifest_sha256") != _sha(sample_path)
            or replays.get("provider_manifest_sha256") != _sha(provider_path)
            or replays.get("analysis_scope_sha256") != _sha(scope_path)):
        raise ValueError("unqualified replay custody")
    if (scope.get("excluded_calibration_snapshot") != "w1-01"
            or providers.get("excluded_calibration_snapshot") != "w1-01"
            or providers.get("analysis_snapshots") != 23
            or replays.get("analysis_snapshots") != 23):
        raise ValueError("analysis scope mismatch")
    sample_rows = {row["id"]: row for row in sample["snapshots"]}
    analysis_ids = scope["analysis_snapshots"]
    if (len(sample_rows) != 24 or len(analysis_ids) != 23
            or set(analysis_ids) != set(sample_rows) - {"w1-01"}):
        raise ValueError("sample membership mismatch")
    receipt_rows = {row["id"]: row for row in providers["snapshots"]}
    if set(receipt_rows) != set(sample_rows):
        raise ValueError("provider receipt membership mismatch")
    graph_rows = {(row["window"], row["alias"]): row for row in replays["results"]}
    expected_graphs = {(window["name"], alias) for window in source["windows"]
                       for alias in TRADERS}
    if not expected_graphs.issubset(graph_rows):
        raise ValueError("missing model graph")
    graphs = {key: _model_graph(root, graph_rows[key]) for key in expected_graphs}

    rounds = []
    totals: dict[tuple[str, str], Decimal] = defaultdict(lambda: Decimal(0))
    wins: dict[tuple[str, str], int] = defaultdict(int)
    losses: dict[tuple[str, str], int] = defaultdict(int)
    entries: dict[tuple[str, str], int] = defaultdict(int)
    for stem in analysis_ids:
        row = sample_rows[stem]
        packet = _read_bound(root, row["packet"], row["packet_sha256"])
        receipt = _read_bound(root, receipt_rows[stem]["receipt"],
                              receipt_rows[stem]["receipt_sha256"])
        if (packet.get("execution_authorized") is not False
                or receipt.get("execution_authorized") is not False
                or receipt.get("snapshot_id") != row["snapshot_id"]
                or [candidate["id"] for candidate in packet["candidates"]] != row["candidate_ids"]):
            raise ValueError("round packet mismatch")
        window = row["window"]
        first_graph = graphs[(window, "zai")]
        candidates = []
        for visible in packet["candidates"]:
            full = first_graph["nodes"].get(visible["id"])
            if full is None or full.get("kind") != "potential_trade":
                raise ValueError("presented candidate missing from graph")
            for field in ("structure", "width", "max_loss_proxy", "max_gain_proxy",
                          "observed_premium", "reward_to_risk_proxy"):
                if visible[field] != full[field]:
                    raise ValueError("presented candidate disagrees with graph")
            candidates.append({"id": visible["id"], "symbol": full["underlying"],
                               "structure": visible["structure"], "dte": visible["dte"],
                               "width": visible["width"],
                               "premium_proxy": visible["observed_premium"],
                               "max_loss_proxy": visible["max_loss_proxy"],
                               "max_gain_proxy": visible["max_gain_proxy"],
                               "reward_to_risk_proxy": visible["reward_to_risk_proxy"]})
        traders = []
        for alias, label in TRADERS.items():
            model = receipt["models"][alias]
            if model["status"] not in ("valid", "no_candidates"):
                raise ValueError("invalid model receipt in analysis round")
            selected = model.get("selected_id")
            if selected is not None and selected not in row["candidate_ids"]:
                raise ValueError("model selected unseen candidate")
            matched = graphs[(window, alias)]
            action = matched["actions"][row["snapshot_id"]]
            if (action["candidate_id"] != selected
                    or action["execution_authorized"] is not False):
                raise ValueError("model choice and graph action differ")
            status = "skip" if selected is None else "blocked"
            pnl: str | None = None
            entry_risk: str | None = None
            if action["decision"] == "enter":
                status = "entered"
                risk = _money(action["max_loss_at_entry_proxy"])
                if not Decimal(0) < risk <= Decimal(300):
                    raise ValueError("entry risk above research cap")
                entry_risk = str(risk)
                exit_node = matched["exits"].get(action["id"])
                if exit_node is not None:
                    change = _money(exit_node["pnl"])
                    pnl = str(change)
                    totals[(window, alias)] += change
                    wins[(window, alias)] += int(change > 0)
                    losses[(window, alias)] += int(change < 0)
                entries[(window, alias)] += 1
            traders.append({"id": alias, "label": label, "selected_id": selected,
                            "action": status, "action_reason": action["reason"],
                            "model_reason": str(model.get("reason", ""))[:240],
                            "entry_loss_proxy": entry_risk,
                            "eventual_pnl_proxy": pnl})
        rounds.append({"id": stem, "window": window, "snapshot_id": row["snapshot_id"],
                       "as_of": row["as_of"],
                       "all_as_of_candidates": row["all_as_of_candidates"],
                       "candidates": candidates, "traders": traders})
    if len(rounds) != 23 or len({row["id"] for row in rounds}) != 23:
        raise ValueError("round count mismatch")
    windows = []
    for window in source["windows"]:
        name = window["name"]
        scores = []
        for alias, label in TRADERS.items():
            key = name, alias
            metrics = graphs[key]["metrics"]
            if (metrics["entered"] != entries[key]
                    or metrics["modeled_wins"] != wins[key]
                    or metrics["modeled_losses"] != losses[key]
                    or metrics["open_at_end"] != 0
                    or _money(metrics["closed_capital_proxy"]) != Decimal(5000) + totals[key]):
                raise ValueError("reconstructed score disagrees with graph")
            scores.append({"id": alias, "label": label, "entered": entries[key],
                           "wins": wins[key], "losses": losses[key],
                           "closed_capital_proxy": str(Decimal(5000) + totals[key])})
        windows.append({"id": name, "start": window["start"], "end": window["end"],
                        "series": window["series"],
                        "traded_minute_bars": window["traded_minute_bars"],
                        "rounds": sum(row["window"] == name for row in rounds),
                        "final_scores": scores})
    return {"schema": SCHEMA, "id": root.name,
            "source_head": source["code_head"],
            "source_manifest_sha256": _sha(source_path),
            "sample_manifest_sha256": _sha(sample_path),
            "provider_manifest_sha256": _sha(provider_path),
            "replay_manifest_sha256": _sha(replays_path),
            "starting_capital": "5000", "windows": windows, "rounds": rounds,
            "excluded_calibration_snapshot": "w1-01",
            "limitations": ["Historical option trade bars are valuation proxies, not executable quotes or fills.",
                            "Round reveals show eventual later-mark outcomes, not same-minute fills.",
                            "The 23 comparable rounds do not establish a reliable win rate."],
            "execution_enabled": False, "research_only": True}


def project_replay(doc: Any) -> dict[str, Any]:
    """Validate and whitelist a compact artifact before serving it to the GUI."""
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA:
        raise ValueError("trade-floor schema required")
    if doc.get("execution_enabled") is not False or doc.get("research_only") is not True:
        raise ValueError("trade-floor authority must be disabled")
    if not isinstance(doc.get("rounds"), list) or not 1 <= len(doc["rounds"]) <= 512:
        raise ValueError("invalid trade-floor round count")
    if not isinstance(doc.get("windows"), list) or not 1 <= len(doc["windows"]) <= 12:
        raise ValueError("invalid trade-floor windows")
    starting = _money(doc.get("starting_capital"))
    if starting <= 0:
        raise ValueError("invalid starting capital")
    if (not isinstance(doc.get("id"), str) or len(doc["id"]) > 160
            or not isinstance(doc.get("source_head"), str) or len(doc["source_head"]) != 40
            or any(not isinstance(doc.get(key), str) or len(doc[key]) != 64 for key in (
                "source_manifest_sha256", "sample_manifest_sha256",
                "provider_manifest_sha256", "replay_manifest_sha256"))
            or not isinstance(doc.get("limitations"), list)
            or any(not isinstance(text, str) or len(text) > 300 for text in doc["limitations"])):
        raise ValueError("invalid trade-floor provenance")
    rounds = []
    for row in doc["rounds"]:
        if (not isinstance(row, dict) or not isinstance(row.get("candidates"), list)
                or len(row["candidates"]) > 12 or not isinstance(row.get("traders"), list)
                or len(row["traders"]) != len(TRADERS)
                or any(not isinstance(trader, dict) for trader in row["traders"])
                or {trader.get("id") for trader in row["traders"]} != set(TRADERS)
                or not isinstance(row.get("id"), str)
                or not isinstance(row.get("window"), str)
                or not isinstance(row.get("snapshot_id"), str)
                or not isinstance(row.get("as_of"), str)
                or not isinstance(row.get("all_as_of_candidates"), int)
                or row["all_as_of_candidates"] < len(row["candidates"])):
            raise ValueError("invalid trade-floor round")
        candidates = []
        ids = set()
        for item in row["candidates"]:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                raise ValueError("invalid trade-floor candidate")
            ids.add(item["id"])
            risk = _money(item["max_loss_proxy"])
            if not Decimal(0) < risk <= Decimal(300):
                raise ValueError("candidate exceeds research risk cap")
            if (not re.fullmatch(r"[A-Z.]{1,6}", item["symbol"])
                    or item["structure"] not in ("put_credit", "call_credit", "put_debit", "call_debit")
                    or not isinstance(item["dte"], int) or not 7 <= item["dte"] <= 60
                    or _money(item["max_gain_proxy"]) <= 0
                    or _money(item["reward_to_risk_proxy"]) <= 0):
                raise ValueError("invalid trade-floor candidate fields")
            candidates.append({key: item[key] for key in (
                "id", "symbol", "structure", "dte", "width", "premium_proxy",
                "max_loss_proxy", "max_gain_proxy", "reward_to_risk_proxy")})
        if len(ids) != len(candidates):
            raise ValueError("duplicate trade-floor candidate")
        traders = []
        for trader in row["traders"]:
            if (not isinstance(trader.get("label"), str) or trader["label"] != TRADERS[trader["id"]]
                    or not isinstance(trader.get("model_reason"), str)
                    or len(trader["model_reason"]) > 240
                    or not isinstance(trader.get("action_reason"), str)
                    or len(trader["action_reason"]) > 100):
                raise ValueError("invalid trader fields")
            if trader["selected_id"] is not None and trader["selected_id"] not in ids:
                raise ValueError("trader selected unknown candidate")
            if trader["action"] not in ("skip", "blocked", "entered"):
                raise ValueError("invalid trader action")
            if ((trader["action"] == "skip") != (trader["selected_id"] is None)):
                raise ValueError("trader action and selection disagree")
            if trader["action"] == "entered":
                risk = _money(trader["entry_loss_proxy"])
                if not Decimal(0) < risk <= Decimal(300):
                    raise ValueError("trader entry exceeds research cap")
                if trader["eventual_pnl_proxy"] is not None:
                    _money(trader["eventual_pnl_proxy"])
            elif trader["eventual_pnl_proxy"] is not None or trader["entry_loss_proxy"] is not None:
                raise ValueError("nonentry has outcome")
            traders.append({key: trader[key] for key in (
                "id", "label", "selected_id", "action", "action_reason", "model_reason",
                "entry_loss_proxy", "eventual_pnl_proxy")})
        rounds.append({"id": row["id"], "window": row["window"],
                       "snapshot_id": row["snapshot_id"], "as_of": row["as_of"],
                       "all_as_of_candidates": row["all_as_of_candidates"],
                       "candidates": candidates, "traders": traders})
    if len({row["id"] for row in rounds}) != len(rounds) or rounds != sorted(rounds, key=lambda row: row["as_of"]):
        raise ValueError("duplicate or unordered trade-floor rounds")
    windows = []
    for item in doc["windows"]:
        if not isinstance(item, dict) or not isinstance(item.get("final_scores"), list):
            raise ValueError("invalid trade-floor window")
        scores = []
        for score in item["final_scores"]:
            if score["id"] not in TRADERS:
                raise ValueError("unknown trader score")
            if (score.get("label") != TRADERS[score["id"]]
                    or any(not isinstance(score.get(key), int) or score[key] < 0
                           for key in ("entered", "wins", "losses"))):
                raise ValueError("invalid trader score fields")
            modeled = [row for row in rounds if row["window"] == item["id"]]
            selected = [next(trader for trader in row["traders"] if trader["id"] == score["id"])
                        for row in modeled]
            entered = [trader for trader in selected if trader["action"] == "entered"]
            outcomes = [_money(trader["eventual_pnl_proxy"]) for trader in entered
                        if trader["eventual_pnl_proxy"] is not None]
            if (score["entered"] != len(entered)
                    or score["wins"] != sum(amount > 0 for amount in outcomes)
                    or score["losses"] != sum(amount < 0 for amount in outcomes)
                    or _money(score["closed_capital_proxy"]) != starting + sum(outcomes, Decimal(0))):
                raise ValueError("trade-floor score disagrees with rounds")
            scores.append({key: score[key] for key in (
                "id", "label", "entered", "wins", "losses", "closed_capital_proxy")})
        if {score["id"] for score in scores} != set(TRADERS):
            raise ValueError("incomplete trader score")
        windows.append({key: item[key] for key in (
            "id", "start", "end", "series", "traded_minute_bars", "rounds")}
                       | {"final_scores": scores})
    if {row["window"] for row in rounds} != {window["id"] for window in windows}:
        raise ValueError("round/window mismatch")
    if (len({window["id"] for window in windows}) != len(windows)
            or any(window["rounds"] != sum(row["window"] == window["id"] for row in rounds)
                   for window in windows)):
        raise ValueError("window round count mismatch")
    projected = {key: doc[key] for key in (
        "schema", "id", "source_head", "source_manifest_sha256",
        "sample_manifest_sha256", "provider_manifest_sha256",
        "replay_manifest_sha256", "starting_capital",
        "excluded_calibration_snapshot", "limitations")}
    projected.update({"windows": windows, "rounds": rounds,
                      "execution_enabled": False, "research_only": True})
    return projected
