"""The overnight historical-research engine (lane L7, Mode H).

One invocation = one night's step, idempotent per date: hindsight gap
analysis of the champion policy runs, a GEPA-style reflective evolution of
policy PROMPTS (glm-5.3-flash on zai and MiniMax-M3.1-Flash-Preview on
minimax-flash each reflect per night — both burn), held-out evaluation of
every proposal by the same mechanical replay accounting, and a nightly
research digest.

The law (unchanged): LLMs only propose (policies, diagnoses). Every number
in the archive and the digest is mechanical — replay summaries and pure
functions over them. Nothing is promoted; promotion to any live influence
stays the operator's pre-registered-rule path (docs/desk/DESK-LAB.md).

Budget: HARD CAPS of <= 200 board calls and <= 8 reflection calls per
invocation, enforced by a counter that refuses (raises) before a call can
exceed them — regardless of quota gating. A fresh (< 6 h) quota-windows
snapshot gates the burn like the lab (skip when no window is under-using);
a missing or stale snapshot falls back to this standing conservative budget.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from tree_options.desk import gepa, hindsight, lab
from tree_options.desk import intraday_action_graph as iag
from tree_options.trex.clock import ET
from tree_options.trex.discovery.llm import LlmError
from tree_options.trex.grant_policy import QuotaWindow, load_windows

OVERNIGHT_SCHEMA = "desk-lab-overnight/1"
#: archive policies burn the flash volume lane for board choices
BOARD_PROVIDER = "zai"
#: both flash tiers reflect each night (glm-5.3-flash + M3.1-Flash)
REFLECT_PROVIDERS = ("zai", "minimax-flash")
HARD_BOARD_CALLS = 200
HARD_REFLECTION_CALLS = 8
CHAMPION_SLICE = 8   # boards per champion run, before availability caps
CANDIDATE_SLICE = 6  # boards per held-out candidate evaluation (upper bound)
FRESH_WINDOW_S = 6 * 3600
AGE_S = 15 * 60  # the lab's board freshness limit
UNTRUSTED_NOTE = (
    "Model output is untrusted prose. Outcomes are mechanical proxies from "
    "replay accounting on last-traded-minute closes, not executable fills. "
    "Nothing in this digest is promoted: promotion is the operator's "
    "pre-registered-rule path (docs/desk/DESK-LAB.md).")


class BudgetRefused(RuntimeError):
    """The night's plan would exceed a hard call cap; nothing was burned."""


@dataclass(frozen=True)
class OvernightBudget:
    """The night's call budget; the hard caps are not configurable around."""

    boards: int = HARD_BOARD_CALLS
    reflections: int = HARD_REFLECTION_CALLS

    def __post_init__(self) -> None:
        if not 1 <= self.boards <= HARD_BOARD_CALLS:
            raise ValueError(f"boards budget must be within 1..{HARD_BOARD_CALLS}")
        if not 1 <= self.reflections <= HARD_REFLECTION_CALLS:
            raise ValueError(
                f"reflections budget must be within 1..{HARD_REFLECTION_CALLS}")


class BudgetCounter:
    """Reserves calls; refuses (raises) BEFORE a grant can exceed the cap."""

    def __init__(self, budget: OvernightBudget) -> None:
        self._boards = budget.boards
        self._reflections = budget.reflections

    @property
    def boards_left(self) -> int:
        return self._boards

    @property
    def reflections_left(self) -> int:
        return self._reflections

    def take_boards(self, count: int) -> None:
        if count > self._boards:
            raise BudgetRefused(
                f"board calls {count} exceed the remaining cap {self._boards}")
        self._boards -= count

    def take_reflections(self, count: int) -> None:
        if count > self._reflections:
            raise BudgetRefused(
                f"reflection calls {count} exceed the remaining cap "
                f"{self._reflections}")
        self._reflections -= count


def _pick(transports: Mapping[str, Any] | None, provider: str) -> Any:
    return None if transports is None else transports.get(provider)


def _scheduled_boards(sessions: list[date], bars: Any, contracts: Any
                      ) -> list[tuple[date, str]]:
    """The ordered (day, clock) snapshots whose board has rows to show."""
    boards: list[tuple[date, str]] = []
    for day in sessions:
        for clock in iag.schedule_for(day):
            candidates = iag._candidates(contracts, bars, iag._instant(day, clock),
                                         AGE_S)
            if lab.board_rows({"candidates": candidates}):
                boards.append((day, clock))
    return boards


def _split_consecutive(items: list[Any], parts: int) -> list[list[Any]]:
    base, extra = divmod(len(items), max(1, parts))
    out: list[list[Any]] = []
    start = 0
    for index in range(max(1, parts)):
        size = base + (1 if index < extra else 0)
        out.append(items[start:start + size])
        start += size
    return out


def _new_run_dir(root: Path, policy_id: str, now: datetime) -> Path:
    base = root / f"{now.strftime('%Y%m%dT%H%M%SZ')}-{lab.slug(policy_id)}"
    suffix = 0
    while True:
        target = base if suffix == 0 else base.with_name(f"{base.name}-{suffix}")
        try:
            target.mkdir(parents=True, exist_ok=False)
            return target
        except FileExistsError:
            suffix += 1


def _run_slice(*, raw: Mapping[str, Any], sessions: list[date],
               boards: list[tuple[date, str]], bars: Any, contracts: Any,
               policy_id: str, policy_prompt: str, provider: str,
               transport: Any, counter: BudgetCounter, now: datetime,
               root: Path) -> dict[str, Any]:
    """One policy on one explicit board slice, scored by iag.replay — the
    same helpers and accounting run_lab uses, with the boards chosen by the
    overnight driver so held-out slices are disjoint by construction."""
    decisions: dict[str, str | None] = {}
    receipts: list[dict[str, Any]] = []
    for day, clock in boards:
        candidates = iag._candidates(contracts, bars, iag._instant(day, clock), AGE_S)
        rows = lab.board_rows({"candidates": candidates})
        if not rows:
            continue
        counter.take_boards(1)  # refuses before the call when the cap is gone
        snapshot = f"s:{day.isoformat()}T{clock}"
        started = time.monotonic()
        receipt: dict[str, Any] = {"snapshot": snapshot, "provider": provider,
                                   "board_rows": len(rows)}
        try:
            reply = lab.ask_board(provider, rows, transport=transport,
                                  policy_prompt=policy_prompt)
            choice, note = lab.parse_choice(reply, {row["id"] for row in rows})
            receipt.update({"ok": True, "choice": choice, "note": note,
                            "prompt_sha256": hashlib.sha256(json.dumps(
                                rows, sort_keys=True).encode()).hexdigest()})
            if choice is not None:
                decisions[snapshot] = choice
        except LlmError as error:
            receipt.update({"ok": False, "error": str(error)[:200]})
        receipt["latency_s"] = round(time.monotonic() - started, 3)
        receipts.append(receipt)
    summary = iag.replay(raw, sessions, decisions)
    document: dict[str, Any] = {
        "schema": lab.LAB_SCHEMA, "policy": policy_id, "at": now.isoformat(),
        "status": "ok", "sessions": [str(d) for d in sessions],
        "boards_shown": len(receipts), "model_calls": len(receipts),
        "model_failures": sum(1 for r in receipts if not r.get("ok")),
        "windows": [], "summary": summary, "receipts": receipts,
        "policy_prompt_sha256": hashlib.sha256(
            policy_prompt.encode()).hexdigest(),
    }
    out_dir = _new_run_dir(root, policy_id, now)
    (out_dir / "summary.json").write_text(json.dumps(document, indent=2,
                                                     default=str))
    if receipts:
        with (out_dir / "receipts.jsonl").open("w", encoding="utf-8") as stream:
            for receipt in receipts:
                stream.write(json.dumps(receipt, default=str) + "\n")
    document["run_dir"] = str(out_dir)
    return document


def _run_summary(document: Mapping[str, Any]) -> dict[str, Any]:
    summary = document.get("summary", {})
    return {"run_dir": document.get("run_dir"),
            "boards": document.get("boards_shown", 0),
            "model_failures": document.get("model_failures", 0),
            "entered": summary.get("entered", 0),
            "modeled_wins": summary.get("modeled_wins", 0),
            "modeled_losses": summary.get("modeled_losses", 0),
            "closed_capital_proxy": summary.get("closed_capital_proxy"),
            "minimum_closed_capital_proxy": summary.get("minimum_closed_capital_proxy")}


def run_overnight(*, bundle: Path, now: datetime,
                  windows: tuple[QuotaWindow, ...] = (),
                  budget: OvernightBudget, lab_root: Path | None = None,
                  transports: Mapping[str, Any] | None = None,
                  windows_age_s: float | None = None) -> dict[str, Any]:
    """One night: champions -> hindsight gaps -> reflections -> held-out
    candidates -> archive update -> digest. Idempotent per night date."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    root = Path(lab_root) if lab_root is not None else lab.default_root()
    night = now.astimezone(ET).date().isoformat()
    digest_dir = root / "overnight" / night
    if (digest_dir / "digest.json").exists():
        return {"schema": OVERNIGHT_SCHEMA, "status": "already_done",
                "night": night, "digest": str(digest_dir / "digest.json")}

    # the gate: a FRESH snapshot gates like the lab; stale/missing means the
    # standing conservative budget (the hard caps below bound either path)
    fresh = (windows_age_s is not None and windows_age_s <= FRESH_WINDOW_S
             and bool(windows))
    if fresh and not lab.burn_allowed(windows):
        return {"schema": OVERNIGHT_SCHEMA, "status": "skipped", "night": night,
                "mode": "under_using_gate", "reason": lab.BURN_NOTE}
    mode = "under_using_gate" if fresh else "standing_budget"

    counter = BudgetCounter(budget)
    counter.take_reflections(len(REFLECT_PROVIDERS))  # the plan refuses here,
    # before any transport call, when the cap cannot hold the night

    raw = json.loads(Path(bundle).read_bytes())
    days = hindsight.all_sessions(raw)
    state = gepa.load_state(root)
    used = set(state["used_sessions"])
    unused = [d for d in days if d.isoformat() not in used]
    sessions = unused[-2:] if len(unused) >= 2 else days[-2:]

    archive = gepa.load_archive(root)
    seeded = False
    front = gepa.pareto_front(archive)
    if not front:
        seed = gepa.new_policy(lab.POLICY_SENTENCE, generation=0, parents=[],
                               created_by="seed:lab-base-prompt")
        gepa.save_policy(root, seed)
        archive = [seed]
        front = [seed]
        seeded = True
    champions = front[:2]

    bars, contracts = hindsight.parse_bundle(raw)
    scheduled = _scheduled_boards(sessions, bars, contracts)[:budget.boards]
    if not scheduled:
        return {"schema": OVERNIGHT_SCHEMA, "status": "no_boards",
                "night": night, "sessions": [str(d) for d in sessions]}

    # slice sizing: champions take up to CHAMPION_SLICE boards each but never
    # more than two thirds of the boards, so a held-out slice always remains
    n_champ = len(champions)
    a_total = max(n_champ, min(2 * CHAMPION_SLICE, (len(scheduled) * 2) // 3))
    champion_slices = _split_consecutive(scheduled[:a_total], n_champ)
    held_out_pool = scheduled[a_total:]

    champion_entries: list[dict[str, Any]] = []
    gap_reports: dict[str, dict[str, Any]] = {}
    touched: list[dict[str, Any]] = []
    for champion, slice_boards in zip(champions, champion_slices, strict=True):
        if not slice_boards:
            continue  # e.g. two champions but one board: no zero-board runs
        document = _run_slice(
            raw=raw, sessions=sessions, boards=slice_boards, bars=bars,
            contracts=contracts, policy_id=f"gepa:{champion['id']}",
            policy_prompt=str(champion["prompt"]), provider=BOARD_PROVIDER,
            transport=_pick(transports, BOARD_PROVIDER), counter=counter,
            now=now, root=root)
        report = hindsight.gap_report(document, raw)
        gap_reports[str(champion["id"])] = report
        stats_before = dict(champion.get("stats") or {})
        gepa.fold_run(champion, document)
        touched.append(champion)
        champion_entries.append({
            "id": champion["id"], "generation": champion["generation"],
            "parents": champion.get("parents", []),
            "policy": f"gepa:{champion['id']}",
            "prompt_sha256": hashlib.sha256(
                str(champion["prompt"]).encode()).hexdigest(),
            "stats_before": stats_before, "stats_after": champion["stats"],
            "run": _run_summary(document), "gap_totals": report["totals"],
            "top_gaps": hindsight.top_gaps(report, 3)})

    # reflections: each flash tier sees the LEADING champion's gap evidence
    leading = champions[0]
    leading_id = str(leading["id"])
    batch = hindsight.top_gaps(gap_reports.get(leading_id, {"boards": []}), 12)
    reflections: list[dict[str, Any]] = []
    for provider in REFLECT_PROVIDERS:
        reflections.append(gepa.reflect(
            provider, batch, str(leading["prompt"]),
            transport=_pick(transports, provider)))

    # candidates: revised prompts + variants, deduped, never repaired
    known_ids = {str(record["id"]) for record in archive}
    champion_prompts = {str(c["prompt"]) for c in champions}
    candidates: list[dict[str, Any]] = []
    candidate_prompts: set[str] = set()
    for result in reflections:
        if result.get("status") != "ok":
            continue
        offered = ([result["revised_prompt"]]
                   if result.get("revised_prompt") else [])
        offered += list(result.get("variants") or [])
        for prompt in offered:
            if (prompt in champion_prompts or prompt in candidate_prompts
                    or gepa.policy_id_for(prompt) in known_ids):
                continue
            candidate = gepa.new_policy(
                prompt, generation=int(leading.get("generation", 0)) + 1,
                parents=[leading_id],
                created_by=f"reflect:{result['provider']}")
            known_ids.add(candidate["id"])
            candidate_prompts.add(prompt)
            candidates.append(candidate)

    # held-out evaluation: disjoint boards, round-robin across candidates,
    # each capped at CANDIDATE_SLICE boards
    candidate_entries: list[dict[str, Any]] = []
    for index, candidate in enumerate(candidates):
        share = held_out_pool[index::len(candidates)][:CANDIDATE_SLICE]
        entry: dict[str, Any] = {
            "id": candidate["id"], "parents": candidate["parents"],
            "generation": candidate["generation"],
            "created_by": candidate["created_by"],
            "prompt_sha256": hashlib.sha256(
                str(candidate["prompt"]).encode()).hexdigest(),
            "held_out_boards": 0, "run": None, "gap_totals": None}
        if share:
            document = _run_slice(
                raw=raw, sessions=sessions, boards=share, bars=bars,
                contracts=contracts, policy_id=f"gepa:{candidate['id']}",
                policy_prompt=str(candidate["prompt"]), provider=BOARD_PROVIDER,
                transport=_pick(transports, BOARD_PROVIDER), counter=counter,
                now=now, root=root)
            report = hindsight.gap_report(document, raw)
            gepa.fold_run(candidate, document)
            touched.append(candidate)
            entry["held_out_boards"] = int(document["boards_shown"])
            entry["run"] = _run_summary(document)
            entry["gap_totals"] = report["totals"]
        gepa.save_policy(root, candidate)
        candidate_entries.append(entry)

    for record in touched:
        gepa.save_policy(root, record)
    state["used_sessions"] = sorted(set(state["used_sessions"])
                                    | {d.isoformat() for d in sessions})
    gepa.save_state(root, state)

    document = {
        "schema": OVERNIGHT_SCHEMA, "status": "ok", "night": night,
        "at": now.isoformat(), "mode": mode, "seeded": seeded,
        "budget": {"boards": budget.boards, "reflections": budget.reflections,
                   "boards_used": budget.boards - counter.boards_left,
                   "reflections_used": (
                       budget.reflections - counter.reflections_left)},
        "sessions": [str(d) for d in sessions],
        "champions": champion_entries, "reflections": reflections,
        "candidates": candidate_entries,
        "pareto_front_after": [
            {"id": record["id"], "generation": record["generation"],
             "stats": record["stats"]}
            for record in gepa.pareto_front(gepa.load_archive(root))],
        "untrusted_note": UNTRUSTED_NOTE,
    }
    digest_dir.mkdir(parents=True, exist_ok=True)
    (digest_dir / "digest.json").write_text(json.dumps(document, indent=2,
                                                       default=str))
    (digest_dir / "digest.md").write_text(_digest_md(document))
    document["digest_dir"] = str(digest_dir)
    return document


def _digest_md(document: Mapping[str, Any]) -> str:
    budget = document.get("budget", {})
    lines: list[str] = []
    add = lines.append
    add(f"# Overnight lab digest — {document['night']}")
    add("")
    add(f"Mode: {document.get('mode')}; sessions "
        + " ".join(document.get("sessions", []))
        + f"; board calls {budget.get('boards_used', 0)}"
          f"/{budget.get('boards', 0)}, reflection calls "
          f"{budget.get('reflections_used', 0)}/{budget.get('reflections', 0)}.")
    add("")
    add(f"> {UNTRUSTED_NOTE}")
    add("")
    add("## Champions (mechanical run + hindsight gaps)")
    for champion in document.get("champions", []):
        add("")
        add(f"### gepa:{champion['id']} (generation {champion['generation']}, "
            f"parents {champion.get('parents') or ['none']})")
        run = champion.get("run") or {}
        add(f"- run: boards {run.get('boards', 0)}, entered "
            f"{run.get('entered', 0)}, wins {run.get('modeled_wins', 0)}, "
            f"losses {run.get('modeled_losses', 0)}, closed capital "
            f"{run.get('closed_capital_proxy')}, minimum closed capital "
            f"{run.get('minimum_closed_capital_proxy')}")
        totals = champion.get("gap_totals") or {}
        add(f"- hindsight gaps: {totals.get('boards', 0)} boards with "
            f"outcomes, gap sum {totals.get('gap_sum')}")
        for gap in champion.get("top_gaps") or []:
            add(f"- gap {gap['gap']} at {gap['snapshot']}: chosen "
                f"{gap['chosen']} (outcome {gap['chosen_outcome']}) vs best "
                f"{gap['best']} (outcome {gap['best_outcome']})")
            if gap.get("chosen_row") is not None:
                add(f"  - chosen row: {json.dumps(gap['chosen_row'], sort_keys=True)}")
            if gap.get("best_row") is not None:
                add(f"  - best row: {json.dumps(gap['best_row'], sort_keys=True)}")
    add("")
    add("## Reflections — MODEL OUTPUT, untrusted prose, quoted verbatim")
    for result in document.get("reflections", []):
        add("")
        model = f" ({result['model']})" if result.get("model") else ""
        add(f"### {result['provider']}{model}")
        if result.get("status") != "ok":
            add(f"- failed: {result.get('error')}")
            continue
        for line in str(result.get("diagnosis") or "").splitlines() or [""]:
            add(f"> {line}")
        add(f"- revised prompt proposed: {bool(result.get('revised_prompt'))}")
        add(f"- variants proposed: {len(result.get('variants') or [])}")
    add("")
    add("## New candidates (held-out boards, mechanical outcomes)")
    for candidate in document.get("candidates", []):
        add("")
        add(f"### gepa:{candidate['id']} (generation {candidate['generation']}, "
            f"parents {candidate.get('parents')}, by {candidate['created_by']})")
        run = candidate.get("run")
        if run is None:
            add(f"- proposed but NOT evaluated this night "
                f"(held-out boards: {candidate.get('held_out_boards', 0)})")
        else:
            totals = candidate.get("gap_totals") or {}
            add(f"- held-out boards {candidate.get('held_out_boards', 0)}, "
                f"entered {run.get('entered', 0)}, closed capital "
                f"{run.get('closed_capital_proxy')}, gap sum "
                f"{totals.get('gap_sum')}")
    add("")
    add("## Pareto front after the night (mechanical stats; nothing promoted)")
    for record in document.get("pareto_front_after", []):
        add(f"- gepa:{record['id']} (generation {record['generation']}): "
            + json.dumps(record["stats"], sort_keys=True))
    add("")
    return "\n".join(lines) + "\n"


# -------------------------------------------------------------------- CLI


def _cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tree_options.desk lab-overnight",
        description="One night of the overnight lab: hindsight gaps + GEPA "
                    "policy evolution + the research digest.")
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--windows", type=Path, default=None,
                        help="quota snapshot (its age selects the gating mode)")
    parser.add_argument("--lab-root", type=Path, default=None)
    args = parser.parse_args(argv)
    now = datetime.now(UTC)
    windows: tuple[QuotaWindow, ...] = ()
    age: float | None = None
    if args.windows is not None and args.windows.exists():
        try:
            windows = load_windows(args.windows)
            age = max(0.0, now.timestamp() - args.windows.stat().st_mtime)
        except (ValueError, OSError) as error:
            print(f"refused: {error}", file=sys.stderr)
            return 2
    try:
        document = run_overnight(bundle=args.bundle, now=now, windows=windows,
                                 budget=OvernightBudget(),
                                 lab_root=args.lab_root, windows_age_s=age)
    except (BudgetRefused, ValueError, OSError, KeyError) as error:
        print(f"refused: {error}", file=sys.stderr)
        return 2
    print(json.dumps({k: document[k] for k in document
                      if k not in ("receipts", "reflections")},
                     indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
