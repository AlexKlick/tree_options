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
  MiniMax-M3.1-Flash-Preview on minimax at effort high and on
  minimax-flash at effort max — M3 retired 2026-09-28; flash never scores
  or judges anything).

Quota discipline (the burn is a CONSUMER): a model policy runs only while
a subscription window is under-using (``grant_policy`` windows snapshot,
the same dashboard numbers the daily grant uses); no snapshot, no burn.
Every call's prompt and raw reply lands in an append-only receipts file.

Runs are evidence, not authority: nothing here touches the broker, the
supervised chain, or the live desk. Scoreboard promotion to advisory-live
is by pre-registered rule only, never from a run.

Board v2 (environment v2, additive — the v1 board, prompt and parser above
are pinned byte-identical): the v1 board is the top-12 rows by reward/risk
with no market context, which hides bullish rows and gives a model nothing
to reason about direction with. ``board_rows_v2`` stratifies (up to four
rows per structure, nearest-to-median reward/risk, no rr ordering),
``board_context`` carries strictly as-of context per ALIASED underlying
(U1/U2/...: no tickers, no dates, no spot levels — a level or a date would
invite memorized-history lookahead), and the v2 JSON contract adds a
holding horizon that ``parse_choice_v2`` validates without repair.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import statistics
import sys
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from itertools import pairwise
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tree_options.desk import intraday_action_graph as iag
from tree_options.trex.discovery.llm import LlmError, chat_json
from tree_options.trex.grant_policy import QuotaWindow, load_windows

if TYPE_CHECKING:  # outcomes imports hindsight, which imports this module
    from tree_options.desk.outcomes import OutcomeIndex

LAB_SCHEMA = "desk-lab-run/1"
BOARD_ROWS = 12  # the board a model sees: top candidates by reward/risk
BURN_NOTE = "quota gate: no under-using window in the snapshot"
#: archive policies (GEPA lane) run as model policies under this prefix
GEPA_PREFIX = "gepa:"
#: the default policy sentence of the board task; the GEPA lane evolves it
POLICY_SENTENCE = (
    "You are a paper-trading policy choosing ONE defined-risk option "
    "spread board row, or skipping."
)

#: The measured 2-leg round trip, in dollars, at the two ends of the tradeable
#: moneyness range. Quoted in the board prompt so the agent is not told a
#: constant the model no longer uses: the flat $14.60 is exactly the
#: (|delta| 0.35-0.50, dte 7-21) cell of a fifteen-cell measured surface.
COST_LOW = "$6.60"    # |delta| < 0.10, dte 7-21
COST_HIGH = "$40.60"  # |delta| 0.50-0.70, dte 7-21

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
    policy_prompt: str | None = None

    def __post_init__(self) -> None:
        if not _POLICY_RE.fullmatch(self.policy):
            raise ValueError(f"invalid policy id {self.policy!r}")
        if self.sessions < 1 or self.boards_cap < 1:
            raise ValueError("sessions and boards_cap must be >= 1")


def is_model_policy(policy: str) -> bool:
    return policy.startswith("model:") or policy.startswith(GEPA_PREFIX)


#: providers a model policy may name (entries of discovery.llm.PROVIDERS)
MODEL_PROVIDERS = ("zai", "minimax", "local", "minimax-flash")


def model_provider(policy: str) -> str:
    """``model:<provider>``; ``gepa:<id>`` burns zai (the flash volume lane);
    ``gepa:<provider>:<id>`` names its provider explicitly."""
    if policy.startswith(GEPA_PREFIX):
        rest = policy[len(GEPA_PREFIX):]
        if ":" not in rest:
            return "zai"  # archive policies burn the flash volume lane
        provider = rest.split(":", 1)[0]
    elif not is_model_policy(policy):
        raise ValueError("not a model policy")
    else:
        provider = policy.split(":", 1)[1]
    if provider not in MODEL_PROVIDERS:
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


def board_prompt(rows: list[dict[str, Any]],
                 policy_prompt: str | None = None) -> list[dict[str, str]]:
    """The board task. ``policy_prompt`` (the GEPA lane) replaces ONLY the
    policy sentence; the risk caps and the JSON reply contract never move."""
    sentence = POLICY_SENTENCE if policy_prompt is None else policy_prompt
    task = (
        sentence
        + " Capital 5000, max loss per trade 300, "
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
              transport: Any = None, model: str | None = None,
              policy_prompt: str | None = None) -> dict[str, Any]:
    """One model call; raises LlmError on failure (the caller records it)."""
    kwargs: dict[str, Any] = {}
    if transport is not None:
        kwargs["transport"] = transport
    if model is not None:
        kwargs["model"] = model
    reply, _used_model = chat_json(provider, board_prompt(rows, policy_prompt), **kwargs)
    return reply


# ---------------------------------------------------------------- board v2

#: the horizons a v2 reply may name (a subset of outcomes.EXIT_MODES)
V2_HORIZONS = ("intraday", "eod", "hold:5", "expiry")
V2_STRUCTURES = ("put_credit", "put_debit", "call_credit", "call_debit")
V2_ROWS_PER_STRUCTURE = 4
#: the only row fields a v2 prompt ever renders
V2_ROW_FIELDS = ("id", "structure", "direction", "underlying", "width", "observed_premium",
                 "max_loss", "max_gain", "reward_risk", "dte", "short_strike_moneyness_pct",
                 "long_recent_move", "short_recent_move")


@dataclass(frozen=True)
class BoardContext:
    """The as-of context of one board. ``public`` is the ONLY part a model
    sees (aliased underlyings, returns, vol, time of day); the alias map and
    the spot levels are private inputs of the rows, never rendered."""

    public: dict[str, Any]
    aliases: dict[str, str]
    spot: dict[str, Decimal]
    as_of: datetime


def _time_of_day(clock: str, schedule: tuple[str, ...]) -> str:
    position = schedule.index(clock)
    if position == 0:
        return "open"
    if position == len(schedule) - 1:
        return "close"
    if clock < "12:00":
        return "morning"
    return "midday" if clock < "14:00" else "afternoon"


def _pct_change(now: Decimal | None, base: Decimal | None) -> str | None:
    if now is None or base is None or base == 0:
        return None
    return str(((now / base - 1) * 100).quantize(Decimal("0.01")))


def board_context(index: OutcomeIndex, day: date, clock: str) -> BoardContext:
    """Strictly as-of context of the (day, clock) board, per aliased
    underlying: spot returns over 1/5/20 sessions (the as-of spot against
    earlier sessions' closes), 20-session annualized realized vol of those
    closes, the time-of-day bucket and the session's ordinal in the window.
    Nothing here reads a print after the decision instant."""
    from tree_options.desk import outcomes

    slot = index.slots.get((day, clock))
    if slot is None:
        raise ValueError(f"{day} {clock} is not a scheduled decision clock of the window")
    as_of = index.timeline[slot][2]
    position = index.session_pos[day]
    prior = index.sessions[:position]  # completed sessions strictly before the board
    closes = outcomes.spot_series(index)
    spot = outcomes.spot_asof(index, as_of)
    aliases = {underlying: f"U{i + 1}" for i, underlying in enumerate(index.underlyings)}
    per: dict[str, dict[str, str | None]] = {}
    for underlying in index.underlyings:
        series = closes.get(underlying, {})
        now = spot.get(underlying)

        def back(n: int, series: dict[date, Decimal] = series) -> Decimal | None:
            return series.get(prior[-n]) if len(prior) >= n else None

        window = [series.get(d) for d in prior[-21:]]
        logs = [math.log(float(b) / float(a)) for a, b in pairwise(window)
                if a is not None and b is not None and a > 0 and b > 0]
        per[aliases[underlying]] = {
            "ret_1s_pct": _pct_change(now, back(1)),
            "ret_5s_pct": _pct_change(now, back(5)),
            "ret_20s_pct": _pct_change(now, back(20)),
            "rv_20s_ann_pct": (f"{statistics.stdev(logs) * math.sqrt(252) * 100:.1f}"
                               if len(logs) >= 5 else None)}
        if index.iv:  # board universe v3: the 30-day implied-vol index, prior close
            from tree_options.desk.board_universe import iv_prev_close

            iv = iv_prev_close(index.iv.get(underlying, {}), day)
            per[aliases[underlying]]["iv30_prev_close_pct"] = (
                None if iv is None else str(iv.quantize(Decimal("0.01"))))
    public = {"time_of_day": _time_of_day(clock, iag.schedule_for(day)),
              "session_ordinal": position + 1, "underlyings": per}
    return BoardContext(public=public, aliases=aliases, spot=spot, as_of=as_of)


def board_rows_v2(packet: dict[str, Any], context: BoardContext) -> list[dict[str, Any]]:
    """The stratified, aliased v2 board: up to four rows per structure, the
    ones nearest their structure's median reward/risk (ties by id) — never
    a reward/risk sort — listed in id order (a hash: no rr position bias)."""
    from tree_options.desk import outcomes

    as_of = datetime.fromisoformat(packet["as_of"])
    if as_of != context.as_of:
        raise ValueError("the context belongs to another board")
    today = as_of.astimezone(iag.ET).date()
    chosen: list[dict[str, Any]] = []
    for structure in V2_STRUCTURES:
        pool = [c for c in packet["candidates"] if c["structure"] == structure]
        if not pool:
            continue
        median = statistics.median(Decimal(c["reward_to_risk_proxy"]) for c in pool)
        pool.sort(key=lambda c: (abs(Decimal(c["reward_to_risk_proxy"]) - median), c["id"]))
        chosen.extend(pool[:V2_ROWS_PER_STRUCTURE])
    rows = []
    for candidate in sorted(chosen, key=lambda c: c["id"]):
        alias = context.aliases.get(candidate["underlying"])
        if alias is None:
            raise ValueError("a board underlying has no alias")  # never fall back to a ticker
        spot = context.spot.get(candidate["underlying"])
        strike = iag.parse_contract(candidate["short"]).strike
        moneyness = (None if spot is None or spot == 0
                     else str(((strike / spot - 1) * 100).quantize(Decimal("0.01"))))
        rows.append({"id": candidate["id"], "structure": candidate["structure"],
                     "direction": outcomes.direction(candidate["structure"]),
                     "underlying": alias, "width": candidate["width"],
                     "observed_premium": candidate["observed_premium"],
                     "max_loss": candidate["max_loss_proxy"],
                     "max_gain": candidate["max_gain_proxy"],
                     "reward_risk": candidate["reward_to_risk_proxy"],
                     "dte": (date.fromisoformat(candidate["expiry"]) - today).days,
                     "short_strike_moneyness_pct": moneyness,
                     "long_recent_move": candidate["long_recent_trade_move"],
                     "short_recent_move": candidate["short_recent_trade_move"]})
    return rows


def _has_iv(context: BoardContext) -> bool:
    underlyings = context.public.get("underlyings", {})
    return any("iv30_prev_close_pct" in values for values in underlyings.values())


def board_prompt_v2(rows: list[dict[str, Any]], context: BoardContext,
                    policy_prompt: str | None = None) -> list[dict[str, str]]:
    """The v2 board task: the (GEPA-swappable) policy sentence, the caps, the
    cost, the horizon menu and the strict JSON contract; renders only the
    public context and the whitelisted row fields."""
    from tree_options.desk.outcomes import CostModel

    sentence = POLICY_SENTENCE if policy_prompt is None else policy_prompt
    task = (
        sentence
        + " Capital 5000, max loss per trade 300, max combined open loss 1500. "
        "Prices are last-traded-minute closes (valuation proxies, not executable "
        "quotes). Execution cost is PER-MONEYNESS, not a constant: measured against "
        "CBOE delayed end-of-day chain snapshots (captured 17:45-06:30 ET, which are "
        f"NOT the decision clocks you trade on), a 2-leg round trip runs from about "
        f"{COST_LOW} deep out of the money to about {COST_HIGH} near the money, against "
        f"the flat {CostModel().round_trip()} the desk's historical digests used. Treat "
        "the flat figure as one price point, not the rule: a wing-heavy book and a "
        "delta-heavy book of the same width and tenor cost very differently. Underlyings "
        "are aliased (U1, U2, ...); the "
        "context gives each one's as-of returns over 1/5/20 sessions and 20-session "
        "realized vol"
        + (" and the prior session's close of its 30-day implied-vol index "
           "(iv30_prev_close_pct)" if _has_iv(context) else "")
        + ". Choose a holding horizon: intraday = the next decision clock, "
        "eod = this session's last clock, hold:5 = five sessions, expiry = the last "
        "mark before expiry. Return STRICT JSON "
        '{"choice": "<row id>" | null, "horizon": "intraday" | "eod" | "hold:5" | '
        '"expiry", "note": "<=40 chars"}. No other text.')
    board = [{key: row.get(key) for key in V2_ROW_FIELDS} for row in rows]
    return [{"role": "user", "content": json.dumps(
        {"task": task, "context": context.public, "board": board})}]


def parse_choice_v2(reply: dict[str, Any],
                    valid_ids: set[str]) -> tuple[str | None, str | None, str]:
    """(choice, horizon, note). An unknown id or an unknown horizon rejects
    the whole reply (None, None, reason); nothing is ever repaired."""
    choice = reply.get("choice")
    note = str(reply.get("note", ""))[:60]
    if choice is None:
        return None, None, note
    if not isinstance(choice, str) or choice not in valid_ids:
        return None, None, f"unknown id rejected: {choice}"[:60]
    horizon = reply.get("horizon")
    if not isinstance(horizon, str) or horizon not in V2_HORIZONS:
        return None, None, f"unknown horizon rejected: {horizon}"[:60]
    return choice, horizon, note


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
            transport: Any = None, now: datetime | None = None,
            burn_gate: bool = True) -> dict[str, Any]:
    """One scored run. Model policies are quota-gated; rules policies are not.

    ``burn_gate=False`` is for a caller that gates the burn itself and holds
    its own hard caps (the challenge game's standing budget): the windows are
    still recorded verbatim in the run document either way."""
    now = now or datetime.now(UTC)
    raw = json.loads(config.bundle.read_bytes())
    sessions = latest_sessions(raw, config.sessions)
    policy = config.policy
    model = is_model_policy(policy)
    if model and burn_gate and not burn_allowed(windows):
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
                    reply = ask_board(provider, rows, transport=transport,
                                      policy_prompt=config.policy_prompt)
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
    document: dict[str, Any] = {"schema": LAB_SCHEMA, "policy": policy,
                                "at": now.isoformat(),
                                "status": "ok", "sessions": [str(d) for d in sessions],
                "boards_shown": boards, "model_calls": len(receipts),
                "model_failures": sum(1 for r in receipts if not r.get("ok")),
                "windows": [{"name": w.name, "under_using": w.under_using}
                            for w in windows],
                "summary": summary, "receipts": receipts}
    if config.policy_prompt is not None:
        # provenance: which evolved policy instruction produced the choices
        document["policy_prompt_sha256"] = hashlib.sha256(
            config.policy_prompt.encode()).hexdigest()
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
                        help="model:zai | model:minimax | model:minimax-flash | "
                             "model:local | no_trade | "
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
