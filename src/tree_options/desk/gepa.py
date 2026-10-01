"""GEPA-style reflective policy evolution: LLMs propose, mechanics decide.

The archive is DATA under ``<lab_root>/policies/`` (the lab root is
``DESK_STORE/evaluations/lab``): one JSON per policy prompt with its
cumulative MECHANICAL stats — every number in a policy record comes from
``iag.replay`` summaries folded by the same arithmetic the lab scoreboard
uses; a model never supplies, judges, or revises a stat.

Per night the flash models (glm-5.3-flash on zai, MiniMax-M3.1-Flash-Preview
on minimax-flash) each see the hindsight-gap evidence of the champion run and
PROPOSE a diagnosis plus revised policy prompts. Proposals are complete
standalone policy sentences that replace the policy sentence of the lab board
task (risk caps and the JSON reply contract stay fixed). A malformed proposal
is dropped, never repaired. Scoring of every proposal is mechanical replay
accounting on a held-out board slice; the Pareto archive keeps the
non-dominated prompts by (closed_pnl_sum, worst minimum capital, prompt
tokens). Nothing here promotes anything: promotion stays the operator's
pre-registered-rule path (docs/desk/DESK-LAB.md).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any

from tree_options.desk.lab_scoreboard import PolicyStats
from tree_options.trex.discovery.llm import LlmError, chat_json

POLICY_SCHEMA = "desk-gepa-policy/1"
STATE_SCHEMA = "desk-gepa-state/1"
ARCHIVE_DIRNAME = "policies"
STATE_FILENAME = "gepa-state.json"
MAX_VARIANTS = 2

REFLECTION_TASK = (
    "You revise a paper-trading policy PROMPT (reflective evolution). The "
    "evidence is mechanical hindsight-gap analysis from replay accounting: "
    "for each board, the row the policy chose, the hindsight-best achievable "
    "row on the SAME board, the gap, and the aliased feature rows of both. "
    "Diagnose why the gaps exist, then propose improved policy prompts. The "
    "capital/risk caps and the reply contract are fixed and not yours to "
    "change. Return STRICT JSON "
    '{"diagnosis": "<=600 chars", "revised_prompt": "<one COMPLETE standalone '
    'policy instruction>", "variants": ["<COMPLETE standalone policy '
    'instruction>", ...]} with at most 2 variants. revised_prompt and each '
    "variant replace the policy sentence of the board task. No other text."
)


def policy_id_for(prompt: str) -> str:
    """Deterministic id from the prompt text (identical re-proposals fold)."""
    return hashlib.sha256(prompt.encode()).hexdigest()[:12]


def prompt_tokens(prompt: str) -> int:
    """Mechanical token proxy: whitespace-separated word count (deterministic,
    no tokenizer dependency; the archive only ever compares like to like)."""
    return len(prompt.split())


def empty_stats() -> dict[str, Any]:
    return {
        "runs": 0,
        "boards": 0,
        "entered": 0,
        "wins": 0,
        "losses": 0,
        "closed_pnl_sum": "0",
        "worst_minimum_capital": None,
        "last_run": "",
    }


def _archive_dir(lab_root: Path) -> Path:
    return Path(lab_root) / ARCHIVE_DIRNAME


def new_policy(
    prompt: str, *, generation: int, parents: list[str], created_by: str
) -> dict[str, Any]:
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("a policy prompt must be a non-empty string")
    return {
        "schema": POLICY_SCHEMA,
        "id": policy_id_for(prompt),
        "generation": generation,
        "parents": list(parents),
        "prompt": prompt,
        "stats": empty_stats(),
        "created_by": created_by,
    }


def load_archive(lab_root: Path) -> list[dict[str, Any]]:
    """Every valid policy record, in a deterministic (file-name) order."""
    directory = _archive_dir(lab_root)
    if not directory.is_dir():
        return []
    policies: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        if path.name == STATE_FILENAME:
            continue
        try:
            record = json.loads(path.read_bytes())
        except (OSError, ValueError):
            continue  # a torn or foreign file never poisons the archive
        if (
            isinstance(record, dict)
            and record.get("schema") == POLICY_SCHEMA
            and isinstance(record.get("id"), str)
            and isinstance(record.get("prompt"), str)
        ):
            policies.append(record)
    return policies


def save_policy(lab_root: Path, policy: Mapping[str, Any]) -> Path:
    directory = _archive_dir(lab_root)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{policy['id']}.json"
    target.write_text(json.dumps(policy, indent=2, default=str))
    return target


def load_state(lab_root: Path) -> dict[str, Any]:
    """The evolution state (which bundle sessions GEPA already burned)."""
    path = _archive_dir(lab_root) / STATE_FILENAME
    try:
        state = json.loads(path.read_bytes())
    except (OSError, ValueError):
        return {"schema": STATE_SCHEMA, "used_sessions": []}
    used = state.get("used_sessions") if isinstance(state, dict) else None
    sessions = [str(s) for s in used] if isinstance(used, list) else []
    return {"schema": STATE_SCHEMA, "used_sessions": sessions}


def save_state(lab_root: Path, state: Mapping[str, Any]) -> None:
    directory = _archive_dir(lab_root)
    directory.mkdir(parents=True, exist_ok=True)
    used = list(dict.fromkeys(str(s) for s in state.get("used_sessions", ())))
    (directory / STATE_FILENAME).write_text(
        json.dumps({"schema": STATE_SCHEMA, "used_sessions": used[-200:]})
    )


# ----------------------------------------------------------------- mechanics


def _decimal(value: Any, default: Decimal) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (ArithmeticError, ValueError):
        return default
    return parsed if parsed.is_finite() else default


def _objectives(policy: Mapping[str, Any]) -> tuple[Decimal, Decimal, int]:
    """(closed_pnl_sum, worst_minimum_capital, prompt tokens): pnl and worst
    higher is better, tokens lower is better. A policy with no runs has NO
    evidence, so it sorts as -inf on both measured axes and can never
    dominate a measured policy (it may still be carried as a proposal)."""
    stats = policy.get("stats") or {}
    try:
        runs = int(stats.get("runs", 0) or 0)
    except (TypeError, ValueError):
        runs = 0
    if runs <= 0:
        return (
            Decimal("-Infinity"),
            Decimal("-Infinity"),
            prompt_tokens(str(policy.get("prompt", ""))),
        )
    worst = stats.get("worst_minimum_capital")
    return (
        _decimal(stats.get("closed_pnl_sum", "0"), Decimal(0)),
        Decimal("-Infinity") if worst in (None, "") else _decimal(worst, Decimal(0)),
        prompt_tokens(str(policy.get("prompt", ""))),
    )


def _dominates(
    a: tuple[Decimal, Decimal, int | Decimal], b: tuple[Decimal, Decimal, int | Decimal]
) -> bool:
    """a dominates b: at least as good on every objective, strictly better
    on at least one. Exact ties dominate nobody (both stay on the front)."""
    if a[0] < b[0] or a[1] < b[1] or a[2] > b[2]:
        return False
    return a[0] > b[0] or a[1] > b[1] or a[2] < b[2]


def pareto_front(
    policies: list[dict[str, Any]],
    *,
    objectives: Callable[[Mapping[str, Any]], tuple[Decimal, Decimal, int | Decimal]] = _objectives,
) -> list[dict[str, Any]]:
    """The non-dominated policies, deterministic order (equal objectives keep
    the lower id first)."""
    keyed = [(policy, objectives(policy)) for policy in policies]
    front = [
        policy
        for policy, objectives in keyed
        if not any(other is not policy and _dominates(score, objectives) for other, score in keyed)
    ]
    front.sort(key=lambda p: str(p.get("id")))
    front.sort(key=lambda p: (objectives(p)[0], objectives(p)[1], -objectives(p)[2]), reverse=True)
    return front


def _stats_of(record: Mapping[str, Any]) -> PolicyStats:
    stats = record.get("stats") or {}

    def count(key: str, *aliases: str) -> int:
        for name in (key, *aliases):
            if name in stats:
                try:
                    return int(stats.get(name) or 0)
                except (TypeError, ValueError):
                    return 0
        return 0

    return PolicyStats(
        runs=count("runs"),
        boards=count("boards"),
        entered=count("entered"),
        wins=count("wins", "modeled_wins"),
        losses=count("losses", "modeled_losses"),
        closed_pnl_sum=_decimal(stats.get("closed_pnl_sum", "0"), Decimal(0)),
        worst_minimum_capital=(
            None
            if stats.get("worst_minimum_capital") in (None, "")
            else _decimal(stats.get("worst_minimum_capital"), Decimal(0))
        ),
        last_run=str(stats.get("last_run", "") or ""),
    )


def fold_run(record: dict[str, Any], run_document: Mapping[str, Any]) -> None:
    """Fold ONE run's MECHANICAL summary into the policy's cumulative stats
    (the scoreboard's own fold arithmetic). Only replay-accounting numbers
    enter here; nothing an LLM claimed about a policy is ever a stat."""
    stats = _stats_of(record)
    stats.fold(dict(run_document), str(run_document.get("at", "")))
    worst = stats.worst_minimum_capital
    record["stats"] = {
        "runs": stats.runs,
        "boards": stats.boards,
        "entered": stats.entered,
        "wins": stats.wins,
        "losses": stats.losses,
        "closed_pnl_sum": str(stats.closed_pnl_sum),
        "worst_minimum_capital": None if worst is None else str(worst),
        "last_run": stats.last_run,
    }


# --------------------------------------------------------------- reflection


def reflect(
    provider: str,
    gap_report_batch: list[dict[str, Any]],
    champion_prompt: str,
    *,
    transport: Any = None,
) -> dict[str, Any]:
    """One flash reflection call: a diagnosis plus up to 2 evolved prompt
    proposals. STRICT JSON; a variant that is not a string or is empty is
    DROPPED, never repaired; a provider failure is recorded, never raised."""
    payload = {
        "task": REFLECTION_TASK,
        "champion_prompt": champion_prompt,
        "gap_boards": gap_report_batch,
    }
    messages = [{"role": "user", "content": json.dumps(payload)}]
    try:
        reply, used_model = chat_json(
            provider, messages, **({"transport": transport} if transport is not None else {})
        )
    except LlmError as error:
        return {"status": "failed", "provider": provider, "error": str(error)[:200]}
    diagnosis = reply.get("diagnosis")
    revised = reply.get("revised_prompt")
    offered = reply.get("variants")
    variants = (
        [item for item in offered if isinstance(item, str) and item.strip()][:MAX_VARIANTS]
        if isinstance(offered, list)
        else []
    )
    return {
        "status": "ok",
        "provider": provider,
        "model": used_model,
        "diagnosis": diagnosis if isinstance(diagnosis, str) else "",
        "revised_prompt": (revised if isinstance(revised, str) and revised.strip() else None),
        "variants": variants,
    }
