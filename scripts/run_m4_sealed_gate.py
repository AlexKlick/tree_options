#!/usr/bin/env python3
"""Run the sealed M4-G4 real-data gate exactly once (the M3 discipline).

Pre-declared in docs/m4-g4-sealed-gate-plan.md §4 (transcribed verbatim at
data/g4/sealed-criteria.json): SIX criteria over BOTH real lanes,
evaluated ONLY from the stamped payload files the machinery writes, verdict
PASS or FAIL recorded VERBATIM, no re-run inside the campaign regardless of
outcome. A FAIL triggers a remediation packet + a NEW pre-declared gate
(the M3 correction-run pattern), never an in-place re-run.

  1 manifest_integrity      : both lanes' typed manifests verified (the
      PRECONDITION — any verify failure means the gate does not start);
      counts reported against the era's stamped counts
  2 candidate_discipline    : accepted-candidate delta provenance in the
      regime's accepted set; the stamped volume-flow threshold == the 0.2.1
      amendment value EXACTLY; the NOT_APPLICABLE disclosure family present
      + counted (OI dropped, earnings disclosed absence) and OI withheld
  3 fill_discipline         : every stamped fill strictly after its decision
      session, against a bar received by the execution instant AND exactly
      one session before the execution session; cumulative participation
      per (contract, bar session) <= the bar's observed volume
  4 rejection_paths_live    : pooled >= 50 per lane under the STRICT
      per-lane class map (lane 1: FIRING parse refusals only, zero-bid rows
      an audit statistic; lane 2: zero-volume-bar refusals,
      MassiveDerivationError, master-row refusals, session_volume_flow
      below-min FAIL — no_bar NOT_EVALUABLE disclosed, never counted)
  5 determinism             : a clean-clone replay reproduces the stamped
      payload hashes byte-identically (the M3 cleanclone pattern)
  6 mutation_campaign       : at the sealed head: full suite green, registry
      N/N KILLED, restoration TRUE; the gate's own verdict logic covered by
      at least one mutant

One-shot: an existing registry or artifacts dir refuses. ``--yes`` is
required — without it the script prints the declaration and exits without
reading a single payload byte. The evidence lands at
<evidence-root>/m4-g4-sealed-gate.{md,json,log} plus the stamped payloads
under the artifacts dir (house rule: counts + samples, full lists elided).

This script consumes NO seal authority: the one-shot authority spend is
``scripts/g4_seal.py execute``'s (owner approval record -> CONSUMPTION
append -> the registered machinery). The two share the same library
machinery (``tree_options.trials.g4_event`` + ``tree_options.seal.g4_gate``).

THE FIREWALL this script was authored under: the machinery's first
execution against the real era artifacts IS the sealed event. It was
authored and tested exclusively against synthetic fixture captures.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

for _name in (
    "OPENBLAS_NUM_THREADS",
    "OMP_NUM_THREADS",
    "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
):
    os.environ[_name] = "1"

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:  # pragma: no cover - import plumbing
    sys.path.insert(0, str(REPO_ROOT / "src"))

DEFAULT_EVIDENCE_ROOT = REPO_ROOT / "docs" / "evidence-logs" / "m4"
DEFAULT_REGISTRY = REPO_ROOT / "artifacts" / "g4-sealed.db"
DEFAULT_ARTIFACTS_DIR = REPO_ROOT / "artifacts" / "g4-sealed"
DEFAULT_SCRATCH_ROOT = REPO_ROOT / "artifacts" / "g4-sealed-scratch"
DEFAULT_ERA_CENSUS = REPO_ROOT / "artifacts" / "census" / "43b0b040ea3c" / "census.json"
DEFAULT_REPLAY_ARTIFACTS = REPO_ROOT / "artifacts" / "g4-sealed-replay"
DEFAULT_MUTATION_REPORT = REPO_ROOT / "artifacts" / "m0-mutations.json"
DEFAULT_SPOT_PROXY_V2 = REPO_ROOT / "artifacts" / "spot-proxy-v2.json"


def _parser() -> argparse.ArgumentParser:
    from tree_options.seal.g4_gate import REJECTION_LANE1_FLOOR

    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--repo", type=Path, default=REPO_ROOT)
    parser.add_argument("--lane1-manifest", type=Path, required=True)
    parser.add_argument("--lane1-source", type=Path, required=True)
    parser.add_argument("--lane2-manifest", type=Path, required=True)
    parser.add_argument("--calendar-decision-artifact", type=Path, required=True)
    parser.add_argument("--evidence-root", type=Path, default=DEFAULT_EVIDENCE_ROOT)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--artifacts-dir", type=Path, default=DEFAULT_ARTIFACTS_DIR)
    parser.add_argument("--scratch-root", type=Path, default=DEFAULT_SCRATCH_ROOT)
    parser.add_argument(
        "--era-census",
        type=Path,
        default=DEFAULT_ERA_CENSUS,
        help="the era's stamped census (criterion 1's count target)",
    )
    parser.add_argument(
        "--replay-artifacts",
        type=Path,
        default=DEFAULT_REPLAY_ARTIFACTS,
        help="the clean-clone replay's stamped payload dir (criterion 5); a"
        " missing dir is an honest criterion-5 FAIL, never a skip",
    )
    parser.add_argument(
        "--mutation-report",
        type=Path,
        default=DEFAULT_MUTATION_REPORT,
        help="the mutation registry JSON at the sealed head (criterion 6)",
    )
    parser.add_argument(
        "--spot-proxy-v2",
        type=Path,
        default=DEFAULT_SPOT_PROXY_V2,
        help="the OPTIONAL declared v2 dollar-volume source (default the"
        " production sidecar; absent = the protocol's declared term fails"
        " honestly on the sentinel)",
    )
    parser.add_argument(
        "--geometry",
        type=int,
        nargs=6,
        metavar=("H", "E", "VAL", "TEST", "ROLL", "MIN_TRAIN"),
        default=None,
        help="the fold geometry in grid Fridays (default: the Agenda-D"
        " proposal 5 2 12 13 13 40, owner-ratified at head declaration)",
    )
    parser.add_argument(
        "--rejection-lane1-floor",
        type=int,
        default=REJECTION_LANE1_FLOOR,
        help="lane 1's pooled FIRING-parse-refusal floor (default: the"
        " 2026-09-01 owner ruling's 0 for real data — the pre-declared 50's"
        " premise measured false on a clean real session; pass 50 to"
        " restore the original fixture-rehearsal teeth)",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="required to run: the sealed event is one-shot and this switch"
        " is the operator's confirmation",
    )
    return parser


def _git_head(repo: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git rev-parse HEAD failed: {result.stderr.strip()[:120]}")
    return result.stdout.strip()


def _cli_gate_paths(args: argparse.Namespace):
    """The gate's path set from the CLI args (built once, used by the
    preflight BEFORE the event and by the evaluation after it)."""
    from tree_options.seal.g4_gate import G4GatePaths

    return G4GatePaths(
        evidence_root=args.evidence_root,
        registry=args.registry,
        artifacts_dir=args.artifacts_dir,
        scratch_root=args.scratch_root,
        era_census=args.era_census,
        replay_artifacts=args.replay_artifacts,
        mutation_report=args.mutation_report,
        spot_proxy_v2=args.spot_proxy_v2,
    )


def run_gate(argv: list[str] | None = None) -> int:
    from tree_options.seal.g4_gate import (
        GatePreflightError,
        evaluate_and_record,
        live_mutation_registry,
        preflight_gate_auxiliaries,
    )
    from tree_options.seal.verified_inputs import (
        SealedInputPaths,
        verify_sealed_inputs,
    )
    from tree_options.trials.g4_event import (
        AGENDA_D_GEOMETRY,
        run_g4_sealed_event,
        sealed_split_override,
    )

    args = _parser().parse_args(argv)
    head = _git_head(args.repo)
    print(f"SEALED_HEAD={head}")
    if not args.yes:
        print(
            "REFUSED: --yes is required (the sealed event is one-shot; without"
            " the confirmation switch nothing is read and nothing is run)",
            file=sys.stderr,
        )
        return 2

    # ---- the precondition: the real typed verifiers over every input -----
    # (remediation-3 review P1-1) the DEFAULT sidecar path may honestly be
    # absent (the v1-only packet shape); an EXPLICITLY supplied path that is
    # not a file REFUSES — a typo'd declaration is not an absent one
    if args.spot_proxy_v2 == DEFAULT_SPOT_PROXY_V2:
        declared_sidecar: Path | None = args.spot_proxy_v2 if args.spot_proxy_v2.is_file() else None
    elif args.spot_proxy_v2.is_file():
        declared_sidecar = args.spot_proxy_v2
    else:
        print(
            f"REFUSED: --spot-proxy-v2 {args.spot_proxy_v2} is not a file — an"
            " explicitly declared sidecar must exist (the default path may be"
            " absent: that is the v1-only packet shape)",
            file=sys.stderr,
        )
        return 2
    paths = SealedInputPaths(
        repo=args.repo,
        lane1_manifest=args.lane1_manifest,
        lane1_source=args.lane1_source,
        lane2_manifest=args.lane2_manifest,
        calendar_decision_artifact=args.calendar_decision_artifact,
        # (remediation-3, owner ruling 2026-09-02) the v2 sidecar is a
        # PACKET input now — held, validated, and bound into the packet's
        # self-hash (it feeds the derivation's daily spot); the runner
        # consumes the HELD bytes, never this path again
        spot_proxy_v2=declared_sidecar,
    )
    held = verify_sealed_inputs(paths)
    print(f"SEALED_PACKET={held.packet.packet_content_sha256}")

    # ---- the gate's auxiliary inputs: checked BEFORE the one-shot runs -----
    # (round-2/3 P0: an unloadable registry or an unparseable report would
    # otherwise raise only at evaluation time — AFTER the event created the
    # one-shot registry/artifacts paths, the exact crash-then-unknown
    # failure mode that consumed the 2026-08-31 event)
    try:
        preflight_gate_auxiliaries(paths=_cli_gate_paths(args), repo_root=args.repo)
    except GatePreflightError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    mutation_registry = live_mutation_registry(args.repo)
    mutation_registry_ids, mutation_registry_digest = mutation_registry  # preflight guarantees
    print(f"MUTATION_REGISTRY_IDS={len(mutation_registry_ids)}")

    geometry = sealed_split_override(
        tuple(args.geometry) if args.geometry is not None else AGENDA_D_GEOMETRY
    )
    run = run_g4_sealed_event(
        held,
        repo_root=args.repo,
        registry_path=args.registry,
        artifacts_dir=args.artifacts_dir,
        scratch_root=args.scratch_root,
        split_override=geometry,
    )
    for line in run.log_lines:
        print(line)

    # ---- the criteria: from the stamped payload files only ---------------
    gate_paths = _cli_gate_paths(args)
    if not gate_paths.replay_artifacts.is_dir():
        print(
            f"REPLAY_ARTIFACTS absent at {gate_paths.replay_artifacts} — criterion 5"
            " will FAIL (never silently skipped)"
        )
    evaluation = evaluate_and_record(
        run,
        held,
        paths=gate_paths,
        repo_root=args.repo,
        head=head,
        mutation_registry_ids=mutation_registry_ids,
        mutation_registry_digest=mutation_registry_digest,
        # the lane-1 floor DEFAULTS to the 2026-09-01 ruling's 0 (the real
        # gate); --rejection-lane1-floor restores the pre-declared 50 for
        # fixture rehearsal wanting its original teeth (Codex remediation-2
        # review, P1-2: this CLI is the documented real-data entry and must
        # not force the fixture floor)
        rejection_lane1_floor=args.rejection_lane1_floor,
        log_lines=(
            f"SEALED_HEAD={head}",
            f"SEALED_PACKET={held.packet.packet_content_sha256}",
            *run.log_lines,
        ),
    )
    evidence_json = gate_paths.evidence_root / "m4-g4-sealed-gate.json"
    print(f"SEALED_EVIDENCE_JSON={evidence_json}")
    print(f"SEALED_GATE_VERDICT={evaluation.verdict}")
    for outcome in evaluation.criteria:
        if outcome.verdict == "PASS":
            print(f"SEALED_CHECK PASS {outcome.criterion_id}")
        else:
            print(f"SEALED_CHECK FAIL {outcome.criterion_id}")
            for failure in outcome.failures:
                print(f"SEALED_CHECK FAIL {outcome.criterion_id}: {failure}")
    return 0 if evaluation.verdict == "PASS" else 4


def main(argv: list[str] | None = None) -> int:
    return run_gate(argv)


if __name__ == "__main__":
    sys.exit(main())
