"""``python -m tree_options.research`` — READ-ONLY inspection surface.

The evidence drawer's reproduction command resolves here (RL1-05: a
real command against pinned artifacts, never a placeholder pointing at
sealed campaign executors). This entry point never writes, never
invokes a campaign runner, never opens the broker plane; it prints
catalog entries, evidence envelopes, and stored run results exactly as
recorded.

    python -m tree_options.research inspect --candidate <id> [--session YYYY-MM-DD]
    python -m tree_options.research inspect --run <run_id> [--workspace DIR]
    python -m tree_options.research inspect --scenario <child_run_id> [--workspace DIR]
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]


def _cmd_inspect(args: argparse.Namespace) -> int:
    if args.scenario:
        return _inspect_scenario(args)
    if args.run:
        return _inspect_run(args)
    if args.candidate:
        return _inspect_candidate(args)
    print("nothing to inspect: pass --candidate, --run, or --scenario",
          file=sys.stderr)
    return 2


def _inspect_candidate(args: argparse.Namespace) -> int:
    from tree_options.research.catalog.sealed_round import build_candidate
    from tree_options.research.evidence.drawer import evidence_for_point

    scopes_root = Path(args.scopes_root) if args.scopes_root else (
        _REPO_ROOT / "artifacts" / "campaign-2026-09")
    if not scopes_root.is_dir():
        print(json.dumps({"error": "scopes_root_missing",
                          "path": str(scopes_root)}), file=sys.stderr)
        return 1
    target = None
    for scope_dir in sorted(scopes_root.iterdir()):
        if not scope_dir.is_dir():
            continue
        try:
            cand = build_candidate(scope_dir)
        except Exception:
            continue
        if cand.id == args.candidate or cand.family == args.candidate:
            target = cand
            break
    if target is None:
        print(json.dumps({"error": "candidate_not_found",
                          "candidate": args.candidate}), file=sys.stderr)
        return 1
    session = date.fromisoformat(args.session) if args.session else None
    cutoff = (datetime.fromisoformat(args.as_of) if args.as_of else None)
    envelope = evidence_for_point(target, session=session,
                                  knowledge_cutoff=cutoff)
    print(json.dumps(envelope.to_dict(), indent=2, sort_keys=True))
    return 0


def _inspect_run(args: argparse.Namespace) -> int:
    from tree_options.research.runstate.store import RunstateStore

    workspace = Path(args.workspace) if args.workspace else (
        Path.home() / ".local" / "state" / "trex-research" / "run-anon")
    db = workspace / "runstate.sqlite3"
    if not db.is_file():
        print(json.dumps({"error": "runstate_store_missing",
                          "path": str(db)}), file=sys.stderr)
        return 1
    store = RunstateStore(db)
    try:
        run = store.get("run", args.run)
        result = store.get("result", args.run)
        spec = store.get("spec", args.run)
    finally:
        store.close()
    if run is None and result is None and spec is None:
        print(json.dumps({"error": "run_not_found", "run_id": args.run}),
              file=sys.stderr)
        return 1
    payload: dict = {"run_id": args.run}
    if spec is not None:
        payload["spec"] = spec
    if run is not None:
        payload["status"] = run
    if result is not None:
        payload["result_sha256"] = result["result_sha256"]
        payload["engine_sha256"] = result["engine_sha256"]
        # Scenario results (success AND refusal) legitimately lack an
        # input_snapshot — only comparison computes and scenarios that
        # ran the engine carry one. Read conditionally (the
        # unconditional read crashed on scenario run ids).
        if "input_snapshot" in result:
            payload["input_snapshot"] = result["input_snapshot"]
        if "input_snapshot_sha256" in result:
            payload["input_snapshot_sha256"] = result[
                "input_snapshot_sha256"]
        if "calendar_sha256" in result:
            payload["calendar_sha256"] = result["calendar_sha256"]
        if "parent_run_id" in result:
            payload["parent_run_id"] = result["parent_run_id"]
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    return 0


def _inspect_scenario(args: argparse.Namespace) -> int:
    """RL-2: a scenario fork's full trace — parent spec, lineage
    pointer, child result with content-bound identity shas. The CLI
    prints every identity field the API's ``GET /runs/<id>/result``
    surfaces (engine / input-snapshot / calendar / scenario-diff /
    result shas, parent_run_id) from the SAME stored record, so the
    parity oracle (RL-2 acceptance matrix #6) compares like with
    like across transport; the payload SHAPES differ (the CLI adds
    the spec/lineage records, the API adds the workspace envelope)."""
    from tree_options.research.runstate.store import RunstateStore

    workspace = Path(args.workspace) if args.workspace else (
        Path.home() / ".local" / "state" / "trex-research" / "run-anon")
    db = workspace / "runstate.sqlite3"
    if not db.is_file():
        print(json.dumps({"error": "runstate_store_missing",
                          "path": str(db)}), file=sys.stderr)
        return 1
    store = RunstateStore(db)
    try:
        run = store.get("run", args.scenario)
        result = store.get("result", args.scenario)
        spec = store.get("spec", args.scenario)
        child = store.get("child", args.scenario)
    finally:
        store.close()
    if (run is None and result is None and spec is None
            and child is None):
        print(json.dumps({"error": "scenario_not_found",
                          "child_run_id": args.scenario}), file=sys.stderr)
        return 1
    payload: dict = {"child_run_id": args.scenario}
    if spec is not None:
        payload["scenario_spec"] = spec
    if run is not None:
        payload["run"] = run
    if child is not None:
        payload["lineage"] = child
    if result is not None:
        payload["result_sha256"] = result["result_sha256"]
        payload["engine_sha256"] = result["engine_sha256"]
        if "scenario_diff_sha256" in result:
            payload["scenario_diff_sha256"] = result[
                "scenario_diff_sha256"]
        if "parent_run_id" in result:
            payload["parent_run_id"] = result["parent_run_id"]
        if "input_snapshot_sha256" in result:
            payload["input_snapshot_sha256"] = result[
                "input_snapshot_sha256"]
        if "calendar_sha256" in result:
            payload["calendar_sha256"] = result["calendar_sha256"]
        payload["wire"] = result.get("wire")
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tree_options.research",
                                     description="Read-only research inspection")
    sub = parser.add_subparsers(dest="command", required=True)
    inspect = sub.add_parser("inspect", help="inspect a candidate or a stored run")
    inspect.add_argument("--candidate", help="candidate id or family")
    inspect.add_argument("--session", help="ISO session date (point inspection)")
    inspect.add_argument("--as-of", help="ISO knowledge-cutoff instant")
    inspect.add_argument("--run", help="stored run id")
    inspect.add_argument("--scenario", help="stored scenario child run id (RL-2)")
    inspect.add_argument("--workspace", help="research workspace (default: run-anon)")
    inspect.add_argument("--scopes-root", help="catalog scopes root")
    inspect.set_defaults(func=_cmd_inspect)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
