"""Bounded reflective search over TREX strategies, with a sealed outer evaluation.

Uses the existing catalog, GEPA Pareto mechanics, trial registry, funded ledger
and Research Lab store. A model proposes restricted configurations; only code
scores them. Every result remains exploratory modeled evidence, never authority.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from tree_options.desk.gepa import pareto_front
from tree_options.protocol.loader import load_protocol, protocol_hash
from tree_options.registry.budget import TrialBudget
from tree_options.registry.scope import TrialScope
from tree_options.registry.sqlite import TrialRegistry
from tree_options.research.paths import assert_no_overlap_with_desk
from tree_options.research.quant import digest, register_version
from tree_options.research.quant_backtest import ReplayPeriod, evaluate_periods
from tree_options.research.runstate.store import RunstateStore, open_runstate_store
from tree_options.schemas.trial import TrialRecord
from tree_options.time.calendar import SessionCalendar, calendar_content_sha256

SUPPORTED = ("equal_weight_us_equities", "momentum_12_1", "hqm_1_3_6_12")
CONTROL = "equal_weight_us_equities"


@dataclass(frozen=True)
class Proposal:
    strategy_id: str
    top_n: int | None
    rationale: str

    def __post_init__(self) -> None:
        if self.strategy_id not in SUPPORTED:
            raise ValueError("proposal requires a supported registered strategy")
        if self.top_n is not None and (type(self.top_n) is not int or not 1 <= self.top_n <= 1000):
            raise ValueError("proposal top_n must be an integer in [1,1000]")
        if not isinstance(self.rationale, str) or not 8 <= len(self.rationale) <= 2000:
            raise ValueError("proposal requires bounded rationale")

    @property
    def config(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "parameters": {} if self.top_n is None else {"top_n": self.top_n},
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.config, "rationale": self.rationale}


@dataclass(frozen=True)
class CampaignSpec:
    hypothesis: str
    train: tuple[ReplayPeriod, ...]
    validation: tuple[ReplayPeriod, ...]
    holdout: tuple[ReplayPeriod, ...]
    code_sha: str
    lock_sha: str
    seeds: tuple[Proposal, ...]
    max_candidates: int = 8
    generations: int = 1
    capital: Decimal = Decimal("10000")
    slippage_bps: Decimal = Decimal("10")
    risk_penalty: Decimal = Decimal("0.25")
    turnover_penalty: Decimal = Decimal("0.001")
    data_class: str = "user_supplied_unqualified"

    def __post_init__(self) -> None:
        if not isinstance(self.hypothesis, str) or not 8 <= len(self.hypothesis) <= 2000:
            raise ValueError("bounded hypothesis required")
        for field, length in ((self.code_sha, 40), (self.lock_sha, 64)):
            if len(field) != length or any(c not in "0123456789abcdef" for c in field):
                raise ValueError("exact code/lock identity required")
        if type(self.max_candidates) is not int or not 2 <= self.max_candidates <= 32:
            raise ValueError("candidate budget must be in [2,32]")
        if type(self.generations) is not int or not 0 <= self.generations <= 4:
            raise ValueError("generation budget must be in [0,4]")
        if self.data_class not in {"synthetic_fixture", "user_supplied_unqualified"}:
            raise ValueError("input data classification cannot claim qualification")
        for name in ("capital", "slippage_bps", "risk_penalty", "turnover_penalty"):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
                raise ValueError(f"invalid {name}")
            if name in {"risk_penalty", "turnover_penalty"} and value > Decimal("1000"):
                raise ValueError(f"invalid bounded {name}")
        if self.capital <= 0 or self.slippage_bps >= 10000:
            raise ValueError("capital must be positive and slippage below 10000 bps")
        previous: ReplayPeriod | None = None
        for name in ("train", "validation", "holdout"):
            periods = tuple(getattr(self, name))
            object.__setattr__(self, name, periods)
            if not periods:
                raise ValueError("all chronological splits require periods")
            for period in periods:
                if previous is not None and previous.mark_at >= period.snapshot.cutoff:
                    raise ValueError("chronological splits require purged nonoverlapping labels")
                previous = period
        object.__setattr__(self, "seeds", tuple(self.seeds))
        seed_configs = {digest(p.config) for p in self.seeds} | {
            digest(Proposal(CONTROL, None, "Equal weight control").config)
        }
        if len(seed_configs) > self.max_candidates:
            raise ValueError("seed candidates exceed candidate budget")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "quant-theory-campaign/1",
            "hypothesis": self.hypothesis,
            "train": [p.to_dict() for p in self.train],
            "validation": [p.to_dict() for p in self.validation],
            "holdout": [p.to_dict() for p in self.holdout],
            "code_sha": self.code_sha,
            "lock_sha": self.lock_sha,
            "seeds": [p.to_dict() for p in self.seeds],
            "max_candidates": self.max_candidates,
            "generations": self.generations,
            "capital": str(self.capital),
            "slippage_bps": str(self.slippage_bps),
            "risk_penalty": str(self.risk_penalty),
            "turnover_penalty": str(self.turnover_penalty),
            "data_class": self.data_class,
            "objective": "mean_next_session_net_return-minus-endpoint_loss-and-turnover",
            "fees": "five_basis_points_per_side",
            "registration": "exploratory_retrospective",
            "execution_authorized": False,
        }


def engine_identity() -> str:
    root = Path(__file__).resolve().parents[1]
    modules = (
        "research/quant_campaign.py",
        "research/quant_campaign_io.py",
        "research/quant_backtest.py",
        "research/quant.py",
        "research/catalog/quant.py",
        "research/contracts.py",
        "research/comparison/funded.py",
        "backtest/equity.py",
        "ledger/book.py",
        "desk/gepa.py",
        "registry/sqlite.py",
        "registry/budget.py",
        "strategy_lab/contracts.py",
        "strategy_lab/ranking.py",
        "strategy_lab/portfolio.py",
        "time/calendar.py",
        "time/sessions.py",
    )
    return digest({p: hashlib.sha256((root / p).read_bytes()).hexdigest() for p in modules})


def _metric_objectives(row: Mapping[str, Any]) -> tuple[Decimal, Decimal, Decimal]:
    metrics = row["metrics"]
    return (
        Decimal(metrics["mean_net_return"]),
        -Decimal(metrics["max_drawdown"]),
        Decimal(metrics["turnover"]),
    )


def _fitness(metrics: Mapping[str, Any], spec: CampaignSpec) -> Decimal:
    return (
        Decimal(metrics["mean_net_return"])
        - spec.risk_penalty * Decimal(metrics["max_drawdown"])
        - spec.turnover_penalty * Decimal(metrics["turnover"])
    )


def _node(
    store: RunstateStore, campaign: str, stage: str, payload: dict[str, Any], parents: list[str]
) -> dict[str, Any]:
    node: dict[str, Any] = {
        "schema": "quant-research-node/1",
        "campaign_id": campaign,
        "stage": stage,
        "payload_sha256": digest(payload),
        "parents": parents,
        "execution_authorized": False,
        "payload_ref": f"runstate:quant_provenance/{digest(payload)}",
    }
    store.put(
        "quant_provenance",
        {"schema": "quant-research-payload/1", "payload": payload},
        key=digest(payload),
    )
    node["node_id"] = digest(node)
    store.put("quant_provenance", node, key=node["node_id"])
    return node


def _provider_receipt(proposer: Any) -> dict[str, str] | None:
    receipt = getattr(proposer, "last_receipt", None)
    if receipt is None:
        return None
    fields = {"requested_model", "returned_model", "response_id", "response_sha256"}
    if (
        not isinstance(receipt, dict)
        or set(receipt) != fields
        or any(not isinstance(v, str) or not v for v in receipt.values())
    ):
        raise ValueError("unsafe provider receipt fields")
    if (
        receipt["requested_model"] != receipt["returned_model"]
        or len(receipt["response_sha256"]) != 64
        or any(c not in "0123456789abcdef" for c in receipt["response_sha256"])
    ):
        raise ValueError("provider receipt identity mismatch")
    return dict(receipt)


def run_campaign(
    workspace: Path,
    spec: CampaignSpec,
    calendar: SessionCalendar,
    *,
    proposer: Callable[[dict[str, Any]], list[Proposal]] | None = None,
    proposer_identity: str = "none",
) -> dict[str, Any]:
    """Recover deterministic computations, never silently retry uncertain reflection.

    One campaign per workspace. The lock protects registry/runstate coordination;
    incomplete evaluations replay under the same identity and budget slot.
    """
    assert_no_overlap_with_desk(workspace=workspace)
    if (proposer is None) != (proposer_identity == "none") or not proposer_identity:
        raise ValueError("proposer must have an explicit pinned identity")
    workspace.mkdir(parents=True, exist_ok=True)
    with (workspace / "campaign.lock").open("a+b") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("campaign already owned") from None
        with open_runstate_store(workspace) as store:
            store.verify()
            binding = {
                "spec": spec.to_dict(),
                "engine_sha256": engine_identity(),
                "calendar_sha256": calendar_content_sha256(calendar),
                "protocol_sha256": protocol_hash(load_protocol()),
                "proposer": proposer_identity,
            }
            campaign = digest(binding)
            previous = store.get("spec", "quant-campaign-binding")
            if previous is not None and previous != binding:
                raise ValueError("campaign binding changed; use a new workspace")
            store.put("spec", binding, key="quant-campaign-binding")
            retained = store.get("result", campaign)
            if retained is not None:
                return retained
            registry = TrialRegistry(
                workspace / "trials.sqlite3", budget=TrialBudget(spec.max_candidates)
            )
            try:
                return _run(store, registry, spec, calendar, campaign, binding, proposer)
            finally:
                registry.close()


def _run(
    store: RunstateStore,
    registry: TrialRegistry,
    spec: CampaignSpec,
    calendar: SessionCalendar,
    campaign: str,
    binding: dict[str, Any],
    proposer: Callable[[dict[str, Any]], list[Proposal]] | None,
) -> dict[str, Any]:
    scope = TrialScope(
        "quant-theory-campaign/1",
        binding["protocol_sha256"],
        campaign,
        "next_session_open_to_close",
        "frozen_pit_quant",
        "bounded_catalog_configs",
    )
    graph = [_node(store, campaign, "freeze_inputs", binding, [])]
    generation_parent = graph[0]["node_id"]
    rows: dict[str, dict[str, Any]] = {}
    excluded: list[dict[str, Any]] = []
    control_id = ""

    def evaluate(proposal: Proposal, generation: int) -> None:
        nonlocal control_id
        if (store.path.parent / "STOP").exists():
            raise ValueError("research campaign halted by STOP file")
        identifier = digest(proposal.config)
        if identifier in rows:
            return
        if len(rows) >= spec.max_candidates:
            return
        version = register_version(
            store,
            proposal.strategy_id,
            code_sha=spec.code_sha,
            lock_sha=spec.lock_sha,
            parameters=proposal.config["parameters"],
        )
        attempt = {
            "schema": "quant-search-attempt/1",
            "campaign_id": campaign,
            "id": identifier,
            "version_id": version["version_id"],
            "config": proposal.config,
            "rationale": proposal.rationale,
            "generation": generation,
            "status": "REGISTERED_BEFORE_EVALUATION",
        }
        attempt_key = digest({"campaign": campaign, "config": identifier})
        retained_attempt = store.get("quant_provenance", attempt_key)
        if retained_attempt is not None:
            attempt = retained_attempt
        store.put("quant_provenance", attempt, key=attempt_key)
        if proposal.strategy_id == CONTROL and proposal.top_n is None:
            control_id = identifier
        if not registry.is_registered(attempt_key):
            registry.register(
                TrialRecord(
                    trial_id=attempt_key,
                    created_at=datetime.now(UTC),
                    hypothesis=spec.hypothesis,
                    git_sha=spec.code_sha,
                    config_hash=identifier,
                    dataset_manifest_hash=campaign,
                    hyperparameters=proposal.config,
                    scope_key=scope.scope_key(),
                ),
                scope,
            )
        if registry.status(attempt_key) == "REGISTERED":
            registry.mark_running(
                attempt_key,
                git_sha=spec.code_sha,
                config_hash=identifier,
                dataset_manifest_hash=campaign,
                at=datetime.now(UTC),
            )
        metric_key = f"{attempt_key}/train"
        metrics = store.get("comparison_row", metric_key)
        if metrics is None:
            metrics = evaluate_periods(
                store,
                version,
                spec.train,
                calendar=calendar,
                capital=spec.capital,
                slippage_bps=spec.slippage_bps,
            )
            store.put("comparison_row", metrics, key=metric_key)
        if registry.status(attempt_key) == "RUNNING":
            registry.complete(
                attempt_key, f"runstate:comparison_row/{metric_key}", outcome_at=datetime.now(UTC)
            )
        node = _node(
            store,
            campaign,
            "training_evaluation",
            {"attempt": attempt_key, "metrics": metrics},
            [generation_parent],
        )
        graph.append(node)
        rows[identifier] = {
            "id": identifier,
            "proposal": proposal.to_dict(),
            "version": version,
            "metrics": metrics,
            "split": "train",
            "node_id": node["node_id"],
        }
        if metrics["disposition"] != "SCORED":
            excluded.append({"id": identifier, "reason": metrics["disposition"]})

    evaluate(Proposal(CONTROL, None, "Common equal weight control"), 0)
    for seed in spec.seeds:
        evaluate(seed, 0)
    calls = 0
    if proposer is not None:
        for generation in range(1, spec.generations + 1):
            if (store.path.parent / "STOP").exists():
                raise ValueError("research campaign halted by STOP file")
            remaining = spec.max_candidates - len(rows)
            if remaining <= 0:
                break
            feedback = {
                "schema": "quant-training-feedback/1",
                "hypothesis": spec.hypothesis,
                "generation": generation,
                "training": [
                    {
                        "id": r["id"],
                        "config": r["proposal"],
                        "split": "train",
                        "metrics": r["metrics"],
                    }
                    for r in rows.values()
                ],
                "allowed_strategies": list(SUPPORTED),
                "remaining_candidates": remaining,
                "objective": {
                    "metric": spec.to_dict()["objective"],
                    "risk_penalty": str(spec.risk_penalty),
                    "turnover_penalty": str(spec.turnover_penalty),
                },
            }
            key = f"{campaign}/reflection/{generation}"
            saved = store.get("quant_provenance", key)
            if saved is None:
                claim_key = key + "/claim"
                if store.get("quant_provenance", claim_key) is not None:
                    raise ValueError(
                        "reflection outcome uncertain; operator must inspect and start a new campaign"
                    )
                store.put(
                    "quant_provenance",
                    {
                        "schema": "quant-reflection-claim/1",
                        "feedback_sha256": digest(feedback),
                        "proposer": binding["proposer"],
                    },
                    key=claim_key,
                )
                try:
                    proposals = proposer(json.loads(json.dumps(feedback, allow_nan=False)))
                    provider_receipt = _provider_receipt(proposer)
                    if (
                        not isinstance(proposals, list)
                        or len(proposals) > remaining
                        or any(not isinstance(p, Proposal) for p in proposals)
                    ):
                        raise ValueError("invalid bounded proposals")
                except Exception:
                    raise ValueError("reflection failed; outcome retained as uncertain") from None
                saved = {
                    "schema": "quant-reflection-result/1",
                    "feedback_sha256": digest(feedback),
                    "proposals": [p.to_dict() for p in proposals],
                    "provider_receipt": provider_receipt,
                }
                store.put("quant_provenance", saved, key=key)
            if saved["feedback_sha256"] != digest(feedback):
                raise ValueError("reflection feedback drift")
            reflection_node = _node(
                store,
                campaign,
                "reflection_proposals",
                saved,
                [r["node_id"] for r in rows.values()],
            )
            graph.append(reflection_node)
            generation_parent = reflection_node["node_id"]
            calls += 1
            for p in saved["proposals"]:
                evaluate(
                    Proposal(p["strategy_id"], p["parameters"].get("top_n"), p["rationale"]),
                    generation,
                )
    valid = [r for r in rows.values() if r["metrics"]["disposition"] == "SCORED"]
    if not valid or rows[control_id]["metrics"]["disposition"] != "SCORED":
        raise ValueError("common control and scored candidates required")
    # No validation outcome is shown to the reflector. Freeze the entire pool first.
    pool = _node(
        store,
        campaign,
        "freeze_candidate_pool",
        {"candidate_ids": list(rows)},
        [n["node_id"] for n in graph[1:]],
    )
    graph.append(pool)
    validation = []
    for row in valid:
        if (store.path.parent / "STOP").exists():
            raise ValueError("research campaign halted by STOP file")
        metrics = evaluate_periods(
            store,
            row["version"],
            spec.validation,
            calendar=calendar,
            capital=spec.capital,
            slippage_bps=spec.slippage_bps,
        )
        store.put("comparison_row", metrics, key=f"{campaign}/{row['id']}/validation")
        if metrics["disposition"] == "SCORED":
            validation.append({**row, "metrics": metrics, "split": "validation"})
        else:
            excluded.append({"id": row["id"], "reason": "validation_incomplete"})
    if not any(r["id"] == control_id for r in validation):
        raise ValueError("validation control incomplete")
    frontier = pareto_front(validation, objectives=_metric_objectives)
    winner = sorted(frontier, key=lambda r: (-_fitness(r["metrics"], spec), r["id"]))[0]
    winner_config = {
        "strategy_id": winner["proposal"]["strategy_id"],
        "parameters": winner["proposal"]["parameters"],
        "version_id": winner["version"]["version_id"],
    }
    seal = _node(
        store,
        campaign,
        "freeze_winner",
        {
            "winner": winner_config,
            "validation": [{"id": r["id"], "metrics": r["metrics"]} for r in validation],
            "pareto_ids": [r["id"] for r in frontier],
        },
        [pool["node_id"]],
    )
    graph.append(seal)
    holdout: dict[str, Any] = {}
    for label, row in (("candidate", winner), ("control", rows[control_id])):
        if (store.path.parent / "STOP").exists():
            raise ValueError("research campaign halted by STOP file")
        key = f"{campaign}/holdout/{label}"
        outer_metrics = store.get("comparison_row", key)
        if outer_metrics is None:
            outer_metrics = evaluate_periods(
                store,
                row["version"],
                spec.holdout,
                calendar=calendar,
                capital=spec.capital,
                slippage_bps=spec.slippage_bps,
            )
            store.put("comparison_row", outer_metrics, key=key)
        holdout[label] = outer_metrics
    outer = _node(store, campaign, "sealed_holdout", holdout, [seal["node_id"]])
    graph.append(outer)
    graph.append(
        _node(
            store,
            campaign,
            "review_proposal",
            {"winner": winner_config, "execution_authorized": False},
            [outer["node_id"]],
        )
    )
    result = {
        "schema": "quant-theory-result/1",
        "campaign_id": campaign,
        "hypothesis": spec.hypothesis,
        "data_class": spec.data_class,
        "evidence_kind": "synthetic_backtest"
        if spec.data_class == "synthetic_fixture"
        else "simulated_execution",
        "registration": "exploratory_retrospective",
        "candidate_count": len(rows),
        "reflection_calls": calls,
        "winner": winner_config,
        "pareto_ids": [r["id"] for r in frontier],
        "validation": [
            {"id": r["id"], "proposal": r["proposal"], "metrics": r["metrics"]} for r in validation
        ],
        "holdout": holdout,
        "excluded_candidates": excluded,
        "graph": graph,
        "disposition": "REVIEW_REQUIRED"
        if all(r["disposition"] == "SCORED" for r in holdout.values())
        else "HOLDOUT_INCOMPLETE",
        "objective": spec.to_dict()["objective"],
        "limitations": [
            "Independent one-session flat-book roundtrips; no compounded multi-session strategy performance.",
            "Endpoint loss is not intraday drawdown.",
            "User-supplied inputs and corporate-action treatment are not independently qualified.",
            "Repeated campaigns on an exposed holdout invalidate independent confirmation; register a fresh holdout.",
        ],
        "execution_authorized": False,
        "exact_external_economics": False,
        "live_money": False,
    }
    store.put("result", result, key=campaign)
    return result
