"""The desk's historical theory lab: LLM traders on recorded sessions.

MODE H of the agent trading desk (operator 2026-09-28): burn the zai +
MiniMax subscriptions converting idle quota into evidence. Each run takes
the most recent sessions of a frozen minute-bar bundle, builds the AS-OF
decision boards (``intraday_action_graph``: no lookahead by construction),
asks a policy to choose on each board, and scores the whole window through
the SAME replay accounting the desk's own baselines use.

Policies:
- rules baselines: the replay() policies (no_trade, put_credit, ...);
- model policies: ``model:<provider>`` — one JSON choice per board via
  ``discovery.llm.chat_json`` (glm-5.3-flash on zai for volume boards,
  MiniMax-M3 on minimax; flash never scores or judges anything).

Quota discipline (the burn is a CONSUMER): a model policy runs only while
a subscription window is under-using (``grant_policy`` windows snapshot,
the same dashboard numbers the daily grant uses); no snapshot, no burn.
Every call's prompt and raw reply lands in an append-only receipts file.

Runs are evidence, not authority: nothing here touches the broker, the
supervised chain, or the live desk. Scoreboard promotion to advisory-live
is by pre-registered rule only, never from a run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tree_options.desk import intraday_action_graph as iag
from tree_options.trex.discovery.llm import LlmError, chat_json
from tree_options.trex.grant_policy import QuotaWindow, load_windows

LAB_SCHEMA = "desk-lab-run/1"
BOARD_ROWS = 12  # the board a model sees: top candidates by reward/risk
BURN_NOTE = "quota gate: no under-using window in the snapshot"

_POLICY_RE = re.compile(r"^[a-z0-9:_-]+$")


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", text.lower()).strip("-")[:40] or "policy"


@dataclass(frozen=True)
class LabConfig:
    bundle: Path
    policy: str
    sessions: int = 3
    boards_cap: int = 24
    lab_root: Path | None = None

    def __post_init__(self) -> None:
        if not _POLICY_RE.fullmatch(self.policy):
            raise ValueError(f"invalid policy id {self.policy!r}")
        if self.sessions < 1 or self.boards_cap < 1:
            raise ValueError("sessions and boards_cap must be >= 1")


def is_model_policy(policy: str) -> bool:
    return policy.startswith("model:")


def model_provider(policy: str) -> str:
    if not is_model_policy(policy):
        raise ValueError("not a model policy")
    provider = policy.split(":", 1)[1]
    if provider not in ("zai", "minimax", "local"):
        raise ValueError(f"unknown provider {provider!r}")
    return provider


def burn_allowed(windows: tuple[QuotaWindow, ...] = ()) -> bool:
    """A model policy may burn while ANY window is under-using."""
    return any(window.under_using for window in windows)


# ------------------------------------------------------------------ boards


def board_rows(packet: dict[str, Any]) -> list[dict[str, Any]]:
    """The compact, aliased board a model chooses from (no tickers, no dates)."""
    candidates = sorted(packet["candidates"],
                        key=lambda c: -float(c["reward_to_risk_proxy"]))[:BOARD_ROWS]
    return [{"id": c["id"], "structure": c["structure"], "width": c["width"],
             "premium": c["observed_premium"], "max_loss": c["max_loss_proxy"],
             "max_gain": c["max_gain_proxy"], "reward_risk": c["reward_to_risk_proxy"],
             "long_recent_move": c["long_recent_trade_move"],
             "short_recent_move": c["short_recent_trade_move"],
             "data_kind": c["data_kind"]} for c in candidates]


def board_prompt(rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    task = (
        "You are a paper-trading policy choosing ONE defined-risk option "
        "spread board row, or skipping. Capital 5000, max loss per trade 300, "
        "max combined open loss 1500. Prices are last-traded-minute closes "
        "(valuation proxies, not executable quotes). Return STRICT JSON "
        '{"choice": "<row id>" | null, "note": "<=40 chars"}. No other text.')
    return [{"role": "user", "content": json.dumps({"task": task, "board": rows})}]


def parse_choice(reply: dict[str, Any], valid_ids: set[str]) -> tuple[str | None, str]:
    """The model's choice, or None; never trusts an unknown id."""
    choice = reply.get("choice")
    note = str(reply.get("note", ""))[:60]
    if choice is None:
        return None, note
    if choice not in valid_ids:
        return None, f"unknown id rejected: {choice}"[:60]
    return str(choice), note


def ask_board(provider: str, rows: list[dict[str, Any]], *,
              transport: Any = None, model: str | None = None) -> dict[str, Any]:
    """One model call; raises LlmError on failure (the caller records it)."""
    kwargs: dict[str, Any] = {}
    if transport is not None:
        kwargs["transport"] = transport
    if model is not None:
        kwargs["model"] = model
    reply, _used_model = chat_json(provider, board_prompt(rows), **kwargs)
    return reply


# -------------------------------------------------------------------- runs


def latest_sessions(raw: dict[str, Any], count: int) -> list[Any]:
    """The bundle's most recent session days (UTC dates of its minute bars)."""
    days: set[Any] = set()
    for contract in raw.get("contracts", {}).values():
        for bar in contract.get("results", []):
            stamp = bar.get("t")
            if isinstance(stamp, int):
                days.add(datetime.fromtimestamp(stamp / 1000, UTC).date())
    if not days:
        raise ValueError("bundle has no sessions")
    return sorted(days)[-count:]


def run_lab(config: LabConfig, *, windows: tuple[QuotaWindow, ...] = (),
            transport: Any = None, now: datetime | None = None) -> dict[str, Any]:
    """One scored run. Model policies are quota-gated; rules policies are not."""
    now = now or datetime.now(UTC)
    raw = json.loads(config.bundle.read_bytes())
    sessions = latest_sessions(raw, config.sessions)
    policy = config.policy
    model = is_model_policy(policy)
    if model and not burn_allowed(windows):
        return {"schema": LAB_SCHEMA, "policy": policy, "at": now.isoformat(),
                "status": "skipped", "reason": BURN_NOTE}

    decisions: dict[str, str | None] = {}
    receipts: list[dict[str, Any]] = []
    boards = 0
    if model:
        provider = model_provider(policy)
        for day in sessions:
            for clock in iag.schedule_for(day):
                if boards >= config.boards_cap:
                    break
                packet = iag.decision_packet(raw, day, clock)
                rows = board_rows(packet)
                if not rows:
                    continue
                boards += 1
                snapshot = packet["snapshot_id"]
                started = time.monotonic()
                receipt = {"snapshot": snapshot, "provider": provider,
                           "board_rows": len(rows)}
                try:
                    reply = ask_board(provider, rows, transport=transport)
                    choice, note = parse_choice(reply, {r["id"] for r in rows})
                    receipt.update({"ok": True, "choice": choice, "note": note,
                                    "prompt_sha256": hashlib.sha256(
                                        json.dumps(rows, sort_keys=True).encode()
                                    ).hexdigest()})
                    if choice is not None:
                        decisions[snapshot] = choice
                except LlmError as error:
                    receipt.update({"ok": False, "error": str(error)[:200]})
                receipt["latency_s"] = round(time.monotonic() - started, 3)
                receipts.append(receipt)
            if boards >= config.boards_cap:
                break

    summary = iag.replay(raw, sessions, decisions if model else None,
                         policy="no_trade" if model else policy)
    document = {"schema": LAB_SCHEMA, "policy": policy, "at": now.isoformat(),
                "status": "ok", "sessions": [str(d) for d in sessions],
                "boards_shown": boards, "model_calls": len(receipts),
                "model_failures": sum(1 for r in receipts if not r.get("ok")),
                "windows": [{"name": w.name, "under_using": w.under_using}
                            for w in windows],
                "summary": summary, "receipts": receipts}
    out_dir = (config.lab_root or default_root()) / (
        f"{now.strftime('%Y%m%dT%H%M%SZ')}-{slug(policy)}")
    suffix = 0
    while True:
        target = out_dir if suffix == 0 else out_dir.with_name(f"{out_dir.name}-{suffix}")
        try:
            target.mkdir(parents=True, exist_ok=False)
            break
        except FileExistsError:
            suffix += 1
    out_dir = target
    (out_dir / "summary.json").write_text(json.dumps(document, indent=2, default=str))
    if receipts:
        with (out_dir / "receipts.jsonl").open("w", encoding="utf-8") as stream:
            for receipt in receipts:
                stream.write(json.dumps(receipt, default=str) + "\n")
    document["run_dir"] = str(out_dir)
    return document


def default_root() -> Path:
    from tree_options.desk.paths import store_root

    return store_root() / "evaluations" / "lab"


# -------------------------------------------------------------------- CLI


def _cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tree_options.desk lab-run",
        description="One lab run: a policy on the bundle's latest sessions.")
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--policy", required=True,
                        help="model:zai | model:minimax | model:local | no_trade | "
                             "put_credit | call_credit | put_debit | call_debit")
    parser.add_argument("--sessions", type=int, default=3)
    parser.add_argument("--boards-cap", type=int, default=24)
    parser.add_argument("--windows", type=Path, default=None,
                        help="quota snapshot (required in effect for model policies)")
    parser.add_argument("--lab-root", type=Path, default=None)
    args = parser.parse_args(argv)
    try:
        windows = load_windows(args.windows) if args.windows and args.windows.exists() else ()
        config = LabConfig(bundle=args.bundle, policy=args.policy,
                           sessions=args.sessions, boards_cap=args.boards_cap,
                           lab_root=args.lab_root)
        document = run_lab(config, windows=windows)
    except (ValueError, OSError, KeyError) as error:
        print(f"refused: {error}", file=sys.stderr)
        return 2
    print(json.dumps({k: document[k] for k in document if k != "receipts"},
                     indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
