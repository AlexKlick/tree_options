"""The P4 window-A sealed-evaluation driver (owner rulings 2026-09-03).

The ONE-SHOT first look at the sealed holdout window (protocol.holdout,
window A, 13 enumerated Fridays 2026-05-08..2026-08-14) on the lane-2
world the whole campaign ran on. Four owner rulings fix the design:

1. CONFIGS — six trials ride the single consumption: the null book x3
   (seeds theory-null-1/2/3, arm A), mom20-quintile on BOTH arms, and
   the exit-2 hold ladder point (arm A).
2. CRITERIA — the return-channel dual falsifier, pre-registered BEFORE
   the look: F1 = at least 2 of 3 window-A null seeds negative
   total_return (bleed persistence); F2 = BOTH mom arms strictly above
   the window-A null max (anomaly persistence). Cohort ICs vs the
   wave-0 calibrated 2-SE bar are SECONDARY disclosures, never verdicts.
3. DATE SCOPE — the label-complete subset computed by the machinery
   (>= label_horizon grid steps of headroom to the world's last grid
   session; on this world: 2026-05-08..2026-07-10, eight dates). The
   remaining sealed dates are disclosed as not-label-complete-in-world
   — their consumption windows run past the 2026-08-14 world end and
   can never complete on THIS world.
4. SEEDS — reuse theory-null-1/2/3 (paired continuity with the
   research-era null spread; the hash-null draw on new dates is fresh
   anyway).

Procedure (the sealed-event discipline, wave-0 registration pattern):

  --register-only  write the TRACKED registration (docs/theory/
                   p4-window-a-registration.json) + state; COMMIT the
                   registration before anything else. Never overwrites.
  --approve        the OWNER act: record the approval binding the
                   registration's content hash, the declared head, and
                   the permitted date set (requires the registration
                   committed clean at the declared head).
  --execute        verify every binding, append the one-shot CONSUMPTION
                   record BEFORE the first trial, run the six trials,
                   evaluate the dual falsifier from the stamped
                   artifacts, write the verdict.
  --verdict        recompute the verdict from the executed artifacts
                   (read-only; refuses an incomplete set).

The w5 seal stays enforced in the runner: sealed test sessions are
impossible WITHOUT the HoldoutEvaluationAuthority this driver builds
from the approval record — no other path can consume window A.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

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
if str(REPO_ROOT / "scripts") not in sys.path:  # pragma: no cover
    sys.path.insert(0, str(REPO_ROOT / "scripts"))

from tree_options.protocol.holdout import FINAL_HOLDOUT_DATES  # noqa: E402
from tree_options.protocol.loader import protocol_hash  # noqa: E402
from tree_options.registry.sqlite import TrialRegistry  # noqa: E402
from tree_options.seal.verified_inputs import (  # noqa: E402
    SealedInputPaths,
    verify_sealed_inputs,
)
from tree_options.trials.g4_event import (  # noqa: E402
    G4_FIXED_CLOCK,
    G4_STALENESS_SESSIONS,
    build_lane2_world,
)
from tree_options.trials.null_score import NULL_SCORE_MODEL_FAMILY  # noqa: E402
from tree_options.trials.options_run import (  # noqa: E402
    HoldoutEvaluationAuthority,
    OptionsSplitOverride,
    run_options_trial,
)
from tree_options.trials.p4_verdict import (  # noqa: E402
    P4_SLOT_ORDER,
    evaluate_window_a,
    label_complete_permitted_sessions,
)

# the wave driver is the audited owner of the null/momentum scored-row
# machinery; P4 reuses it verbatim (momentum with include_holdout=True —
# legal ONLY here, under the authority this driver builds)
_wave_spec = importlib.util.spec_from_file_location(
    "run_lane2_wave", REPO_ROOT / "scripts" / "run_lane2_wave.py"
)
assert _wave_spec is not None and _wave_spec.loader is not None  # import plumbing
wave = importlib.util.module_from_spec(_wave_spec)
# register BEFORE exec: the wave module's dataclasses resolve their
# namespace through sys.modules (an unregistered module cannot process)
sys.modules.setdefault("run_lane2_wave", wave)
_wave_spec.loader.exec_module(wave)

P4_ROOT = REPO_ROOT / "artifacts" / "theory" / "p4"
REGISTRATION_PATH = REPO_ROOT / "docs" / "theory" / "p4-window-a-registration.json"
STATE_PATH = P4_ROOT / "state.json"
REGISTRY_PATH = P4_ROOT / "registry.db"
TRIALS_DIR = P4_ROOT / "trials"
SCRATCH_ROOT = P4_ROOT / "scratch"
AUTHORITY_ROOT = REPO_ROOT / "artifacts" / "p4-authority"
APPROVAL_PATH = AUTHORITY_ROOT / "approval.json"
CONSUMPTION_PATH = AUTHORITY_ROOT / "consumption.jsonl"
VERDICT_PATH = P4_ROOT / "verdict.json"
WAVE0_STATE_PATH = REPO_ROOT / "artifacts" / "theory" / "wave0" / "state.json"

# (D4, unchanged from the theory waves) H, embargo, val, test, roll,
# min_train — in GRID Fridays
P4_GEOMETRY = (5, 2, 6, 13, 13, 34)
P4_LABEL_HORIZON = P4_GEOMETRY[0]
P4_WINDOW_ID = "final-holdout-window-a"

_DEFAULT_DELTA = Decimal("0.45")
_DEFAULT_DTE = 45
_DEFAULT_EXIT = 4
_DEFAULT_FLOW = 100
_MOM = "mom20-quintile/v1"

_NULL_RUN_BASE = 15  # the wave consumed a-r1..a-r14 and b-r1..b-r2
_MOM_B_RUN_INDEX = 3


@dataclass(frozen=True)
class P4Config:
    """One pre-registered evaluation slot (no aliasing: six unique
    configs, six fixed run indices)."""

    slot_id: str
    family: str
    hypothesis: str
    arm: Literal["A", "B"]
    model_family: str
    score_seed: str | None
    target_abs_delta: Decimal
    target_dte: int
    exit_sessions_after_entry: int
    flow_min_session_volume: int
    run_index: int

    def params_key(self) -> tuple[Any, ...]:
        return (
            self.arm,
            self.model_family,
            self.score_seed,
            str(self.target_abs_delta),
            self.target_dte,
            self.exit_sessions_after_entry,
            self.flow_min_session_volume,
        )

    def strategy_config(self):
        from tree_options.options.strategy import OptionsStrategyConfig

        return OptionsStrategyConfig(
            target_abs_delta=self.target_abs_delta,
            target_dte=self.target_dte,
            exit_sessions_after_entry=self.exit_sessions_after_entry,
        )


_NULL_HYPOTHESIS = (
    "P4 F1 reference: the null book (seed {seed}) on window A — the"
    " out-of-sample bleed bar. Pre-registered expectation: negative"
    " total_return (fee + tick drag persists); F1 confirms at >=2 of 3"
    " seeds negative"
)
_MOM_HYPOTHESIS = (
    "P4 F2: the mom20-quintile composition book on window A — the P3"
    " in-sample composition anomaly (+4.2%/+5.6%, both arms above the"
    " research-era null spread) tested out-of-sample. F2 replicates"
    " ONLY if BOTH arms post total_return strictly above the window-A"
    " null max; the ranking channel is NOT under test (P3 killed it)"
)
_HOLD_HYPOTHESIS = (
    "P4 disclosure: exit-2 on window A — the lose-least ladder point"
    " against the window-A null spread (secondary reading, never a"
    " verdict)"
)


def p4_menu() -> tuple[P4Config, ...]:
    nulls = tuple(
        P4Config(
            slot_id=f"p4-null-{n}",
            family="P4-NULL",
            hypothesis=_NULL_HYPOTHESIS.format(seed=f"theory-null-{n}"),
            arm="A",
            model_family=NULL_SCORE_MODEL_FAMILY,
            score_seed=f"theory-null-{n}",
            target_abs_delta=_DEFAULT_DELTA,
            target_dte=_DEFAULT_DTE,
            exit_sessions_after_entry=_DEFAULT_EXIT,
            flow_min_session_volume=_DEFAULT_FLOW,
            run_index=_NULL_RUN_BASE + n - 1,
        )
        for n in (1, 2, 3)
    )
    mom_a = P4Config(
        slot_id="p4-mom-a",
        family="P4-MOM",
        hypothesis=_MOM_HYPOTHESIS,
        arm="A",
        model_family=_MOM,
        score_seed=None,
        target_abs_delta=_DEFAULT_DELTA,
        target_dte=_DEFAULT_DTE,
        exit_sessions_after_entry=_DEFAULT_EXIT,
        flow_min_session_volume=_DEFAULT_FLOW,
        run_index=_NULL_RUN_BASE + 3,
    )
    mom_b = P4Config(
        slot_id="p4-mom-b",
        family="P4-MOM",
        hypothesis=_MOM_HYPOTHESIS,
        arm="B",
        model_family=_MOM,
        score_seed=None,
        target_abs_delta=_DEFAULT_DELTA,
        target_dte=_DEFAULT_DTE,
        exit_sessions_after_entry=_DEFAULT_EXIT,
        flow_min_session_volume=_DEFAULT_FLOW,
        run_index=_MOM_B_RUN_INDEX,
    )
    hold = P4Config(
        slot_id="p4-hold-exit2",
        family="P4-HOLD",
        hypothesis=_HOLD_HYPOTHESIS,
        arm="A",
        model_family=NULL_SCORE_MODEL_FAMILY,
        score_seed="theory-null-1",
        target_abs_delta=_DEFAULT_DELTA,
        target_dte=_DEFAULT_DTE,
        exit_sessions_after_entry=2,
        flow_min_session_volume=_DEFAULT_FLOW,
        run_index=_NULL_RUN_BASE + 4,
    )
    return (*nulls, mom_a, mom_b, hold)


_F1_TEXT = (
    "F1 (bleed persistence): at least 2 of the 3 window-A null seeds"
    " post negative pooled total_return"
)
_F2_TEXT = (
    "F2 (anomaly persistence): BOTH momentum arms post total_return"
    " strictly above the maximum of the 3 window-A null seeds"
)


def _git_head(repo: Path) -> str:
    out = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip()


def _registration_bytes(registration: dict[str, Any]) -> bytes:
    return json.dumps(registration, indent=2, sort_keys=True).encode("utf-8") + b"\n"


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _sha256_json(obj: Any) -> str:
    return _sha256_bytes(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def load_registration() -> dict[str, Any]:
    if not REGISTRATION_PATH.is_file():
        raise SystemExit(
            f"REFUSED: the tracked registration {REGISTRATION_PATH} does not"
            " exist — run --register-only and COMMIT it before anything else"
        )
    return json.loads(REGISTRATION_PATH.read_text(encoding="utf-8"))


def load_state() -> dict[str, Any]:
    if not STATE_PATH.is_file():
        raise SystemExit(f"REFUSED: no state at {STATE_PATH} — run --register-only first")
    return json.loads(STATE_PATH.read_text(encoding="utf-8"))


def save_state(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(STATE_PATH)


def _held_paths() -> SealedInputPaths:
    return wave._held_paths()


def _fresh_scratch() -> Path:
    """(the wave-0 collision lesson, made structural) every invocation
    gets its OWN scratch dir — `build_lane2_world` materializes into
    scratch/lane2-capture and refuses any leftover; a shared dir wedged
    consecutive waves."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    scratch = SCRATCH_ROOT / f"world-{stamp}-{os.getpid()}"
    scratch.mkdir(parents=True, exist_ok=False)
    return scratch


def _build_world():
    from tree_options.protocol.loader import load_protocol_bytes
    from tree_options.seal.runner import wire_production_runner

    wire_production_runner(REPO_ROOT)  # the packet binds the runner impl
    held = verify_sealed_inputs(_held_paths())
    protocol = load_protocol_bytes(held.protocol_bytes)
    world = build_lane2_world(
        held,
        repo_root=REPO_ROOT,
        scratch=_fresh_scratch(),
        protocol=protocol,
        spot_v2_path=REPO_ROOT / "artifacts" / "spot-proxy-v2.json",
        staleness_sessions=G4_STALENESS_SESSIONS,
    )
    manifest_hash = held.packet.lane2_manifest.typed_manifest_content_hash
    return world, protocol, manifest_hash


def _world_permitted(world) -> tuple[date, ...]:
    world_last = max(bar.session for bar in world.dataset.bars)
    return label_complete_permitted_sessions(world.grid.sessions(), world_last, P4_LABEL_HORIZON)


def _decision_sessions(world, permitted: tuple[date, ...]) -> tuple[date, ...]:
    """The research grid (everything strictly before the seal — train
    depth) plus EXACTLY the permitted sealed dates."""
    first_sealed = date.fromisoformat(min(FINAL_HOLDOUT_DATES))
    research = tuple(s for s in world.grid.sessions() if s < first_sealed)
    return tuple(sorted(set(research) | set(permitted)))


def register_only() -> int:
    if REGISTRATION_PATH.exists():
        raise SystemExit(
            f"REFUSED: {REGISTRATION_PATH} already exists — the registration"
            " is written once; a rewrite is a NEW registration and requires"
            " removing this file as a deliberate owner act"
        )
    menu = p4_menu()
    keys = [config.params_key() for config in menu]
    if len(set(keys)) != len(keys):
        raise SystemExit("REFUSED: duplicate params in the P4 menu")
    world, protocol, manifest_hash = _build_world()
    permitted = _world_permitted(world)
    excluded = sorted(set(FINAL_HOLDOUT_DATES) - {d.isoformat() for d in permitted})
    registration = {
        "program": "p4-window-a",
        "registered_at_head": _git_head(REPO_ROOT),
        "world_id": world.world_id,
        "dataset_manifest_hash": manifest_hash,
        "protocol_hash": protocol_hash(protocol),
        "geometry_grid_fridays": {
            "label_horizon": P4_GEOMETRY[0],
            "embargo": P4_GEOMETRY[1],
            "val": P4_GEOMETRY[2],
            "test": P4_GEOMETRY[3],
            "roll": P4_GEOMETRY[4],
            "min_train": P4_GEOMETRY[5],
        },
        "rules": {
            "configs": (
                "owner ruling 2026-09-03: null x3 (seeds theory-null-1/2/3,"
                " arm A) + mom20-quintile (arms A and B) + hold exit-2 (arm"
                " A) — six trials on the single window-A consumption"
            ),
            "criteria": (
                "owner ruling 2026-09-03 (return-channel dual falsifier):"
                f" {_F1_TEXT}; {_F2_TEXT}. Secondary disclosures (never"
                " verdicts): cohort ICs vs the wave-0 calibrated 2-SE bar"
                " 2*prior/sqrt(3), and exit-2 against the window-A null"
                " spread"
            ),
            "date_scope": (
                "owner ruling 2026-09-03: the label-complete subset computed"
                " by the machinery (>= label_horizon grid steps of headroom"
                " to the world's last grid session); the remaining sealed"
                " dates are disclosed as not-label-complete-in-world"
            ),
            "seeds": (
                "owner ruling 2026-09-03: reuse theory-null-1/2/3 (paired"
                " continuity with the research-era null spread; the"
                " hash-null draw on new dates is fresh anyway)"
            ),
        },
        "evaluation_window": {
            "window_id": P4_WINDOW_ID,
            "label_horizon_sessions": P4_LABEL_HORIZON,
            "expected_permitted": [d.isoformat() for d in permitted],
            "expected_excluded": excluded,
        },
        "criteria": {"f1": _F1_TEXT, "f2": _F2_TEXT},
        "slots": [
            {
                "slot_id": config.slot_id,
                "family": config.family,
                "arm": config.arm,
                "model_family": config.model_family,
                "score_seed": config.score_seed,
                "params_key": [str(part) for part in config.params_key()],
                "hypothesis": config.hypothesis,
                "run_index": config.run_index,
            }
            for config in menu
        ],
    }
    REGISTRATION_PATH.parent.mkdir(parents=True, exist_ok=True)
    REGISTRATION_PATH.write_bytes(_registration_bytes(registration))
    state: dict[str, Any] = {
        "registration_sha256": _sha256_bytes(_registration_bytes(registration)),
        "executions": [],
        "verdict": None,
    }
    save_state(state)
    print(
        f"registered: {len(menu)} slots, permitted={[d.isoformat() for d in permitted]}",
        flush=True,
    )
    print(f"excluded (not label-complete in world): {excluded}", flush=True)
    print(
        "NEXT: commit docs/theory/p4-window-a-registration.json, then the"
        " owner approves at the declared head (scripts/run_p4_holdout.py"
        " --approve --declared-head <sha> --reason '...')",
        flush=True,
    )
    return 0


def _require_committed_registration(declared_head: str) -> None:
    """The registration must live in HEAD's tree with the on-disk bytes —
    an uncommitted or edited registration cannot be approved."""
    blob = subprocess.run(
        [
            "git",
            "-C",
            str(REPO_ROOT),
            "cat-file",
            "-e",
            f"{declared_head}:docs/theory/p4-window-a-registration.json",
        ],
        capture_output=True,
    )
    if blob.returncode != 0:
        raise SystemExit(
            f"REFUSED: docs/theory/p4-window-a-registration.json is not"
            f" committed at {declared_head[:12]}… — commit the registration"
            " before approving"
        )
    committed = subprocess.run(
        [
            "git",
            "-C",
            str(REPO_ROOT),
            "show",
            f"{declared_head}:docs/theory/p4-window-a-registration.json",
        ],
        capture_output=True,
        check=True,
    ).stdout
    on_disk = REGISTRATION_PATH.read_bytes()
    if committed != on_disk:
        raise SystemExit(
            "REFUSED: the on-disk registration differs from the committed"
            " one — approve what is committed, not a working-tree rewrite"
        )


def approve(declared_head: str, reason: str) -> int:
    registration = load_registration()
    state = load_state()
    head = _git_head(REPO_ROOT)
    if head != declared_head:
        raise SystemExit(
            f"REFUSED: the declared head {declared_head[:12]}… is not the"
            f" checkout's HEAD {head[:12]}… — the owner declares the exact"
            " head the evaluation runs at"
        )
    _require_committed_registration(declared_head)
    registration_sha = _sha256_bytes(_registration_bytes(registration))
    if state.get("registration_sha256") != registration_sha:
        raise SystemExit(
            "REFUSED: the state's recorded registration hash does not"
            " match the tracked registration — re-register deliberately"
            " (the registration is written once)"
        )
    if not reason.strip():
        raise SystemExit("REFUSED: the approval needs a reason")
    if APPROVAL_PATH.exists():
        raise SystemExit(
            f"REFUSED: {APPROVAL_PATH} already exists — the approval is the"
            " owner's ONE act; a second approval requires removing the"
            " record as a deliberate owner decision"
        )
    record = {
        "kind": "P4_HOLDOUT_APPROVAL",
        "window_id": registration["evaluation_window"]["window_id"],
        "world_id": registration["world_id"],
        "protocol_hash": registration["protocol_hash"],
        "dataset_manifest_hash": registration["dataset_manifest_hash"],
        "registration_sha256": registration_sha,
        "declared_head": declared_head,
        "permitted_test_sessions": registration["evaluation_window"]["expected_permitted"],
        "reason": reason,
        "at_epoch": int(time.time()),
    }
    AUTHORITY_ROOT.mkdir(parents=True, exist_ok=True)
    APPROVAL_PATH.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        f"approved: head={declared_head[:12]}… permitted={record['permitted_test_sessions']}",
        flush=True,
    )
    print("NEXT: scripts/run_p4_holdout.py --execute (one-shot)", flush=True)
    return 0


def _content_identity(approval: dict[str, Any]) -> str:
    """Identity of the consumed CONTENT (head-independent — a re-execution
    at a different head is still the same window's second look)."""
    return _sha256_json(
        {
            "kind": approval["kind"],
            "window_id": approval["window_id"],
            "world_id": approval["world_id"],
            "protocol_hash": approval["protocol_hash"],
            "dataset_manifest_hash": approval["dataset_manifest_hash"],
            "registration_sha256": approval["registration_sha256"],
            "permitted_test_sessions": approval["permitted_test_sessions"],
        }
    )


def _read_consumptions() -> list[dict[str, Any]]:
    if not CONSUMPTION_PATH.is_file():
        return []
    records: list[dict[str, Any]] = []
    prev = "0" * 64
    for line in CONSUMPTION_PATH.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        if record.get("prev_record_sha256") != prev or record.get("record_sha256") != _sha256_json(
            {k: v for k, v in record.items() if k not in ("record_sha256",)}
        ):
            raise SystemExit(
                "REFUSED: the consumption ledger is damaged (a broken hash"
                " chain is an incident — reconcile with the owner, never"
                " append past it)"
            )
        prev = record["record_sha256"]
        records.append(record)
    return records


def _append_consumption(record: dict[str, Any]) -> str:
    existing = _read_consumptions()
    prev = existing[-1]["record_sha256"] if existing else "0" * 64
    record = {**record, "prev_record_sha256": prev}
    record_sha = _sha256_json(record)
    record["record_sha256"] = record_sha
    AUTHORITY_ROOT.mkdir(parents=True, exist_ok=True)
    with CONSUMPTION_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return record_sha


def _verify_config_against_registration(registration: dict[str, Any], config: P4Config) -> None:
    rows = {row["slot_id"]: row for row in registration["slots"]}
    row = rows.get(config.slot_id)
    if row is None:
        raise SystemExit(f"REFUSED: slot {config.slot_id} is not in the committed pre-registration")
    key = [str(part) for part in config.params_key()]
    if row["params_key"] != key:
        raise SystemExit(
            f"REFUSED: slot {config.slot_id} drifted from the committed"
            f" pre-registration (registered {row['params_key']} !="
            f" executed {key})"
        )
    if row["hypothesis"] != config.hypothesis:
        raise SystemExit(
            f"REFUSED: slot {config.slot_id}'s hypothesis text drifted from"
            " the committed pre-registration"
        )
    if row["run_index"] != config.run_index:
        raise SystemExit(
            f"REFUSED: slot {config.slot_id}'s run_index drifted from the"
            " committed pre-registration"
        )


def _scored_for(config: P4Config, world, permitted: tuple[date, ...]):
    if config.model_family == NULL_SCORE_MODEL_FAMILY:
        assert config.score_seed is not None
        return wave.null_scored_rows(config.score_seed, permitted, tuple(world.overlay.underlyings))
    if config.model_family == _MOM:
        return wave.momentum_scored_rows(
            world.dataset.bars, world.grid, permitted, include_holdout=True
        )
    raise SystemExit(f"REFUSED: unknown model family {config.model_family}")


def _wave0_prior() -> float:
    if not WAVE0_STATE_PATH.is_file():
        raise SystemExit(
            "REFUSED: the wave-0 state is absent — the P4 secondary IC"
            " disclosure needs the calibration prior"
        )
    calibration = json.loads(WAVE0_STATE_PATH.read_text(encoding="utf-8")).get("calibration")
    prior = (calibration or {}).get("prior_stride4_cohort_ic_sd")
    if not isinstance(prior, (int, float)) or isinstance(prior, bool):
        raise SystemExit(
            "REFUSED: the wave-0 calibration prior is absent or malformed"
            " — the secondary IC disclosure refuses without it"
        )
    return float(prior)


def _refuse_second_consumption(approval: dict[str, Any]) -> None:
    """The one-shot check: any consumption record already in the ledger
    whose content identity matches this approval's content refuses — a
    second look at the same window is the thing the seal exists to
    prevent (head-INDEPENDENT: a re-run at a new head is still the same
    content)."""
    identity = _content_identity(approval)
    for record in _read_consumptions():
        if record.get("content_identity") == identity:
            raise SystemExit(
                "REFUSED: this window-A content was already consumed"
                f" (consumption {record.get('record_sha256', '')[:12]}… at"
                f" head {str(record.get('head', ''))[:12]}…) — a second"
                " look is the one thing the seal exists to prevent"
            )


def _execute() -> int:
    registration = load_registration()
    state = load_state()
    registration_sha = _sha256_bytes(_registration_bytes(registration))
    if state.get("registration_sha256") != registration_sha:
        raise SystemExit(
            "REFUSED: the tracked registration's content hash does not"
            " match the state's recorded hash — the registration was"
            " rewritten after the fact or the state is not this"
            " registration's"
        )
    if not APPROVAL_PATH.is_file():
        raise SystemExit(
            f"REFUSED: no approval record at {APPROVAL_PATH} — the owner"
            " approves (once) before any execution"
        )
    approval = json.loads(APPROVAL_PATH.read_text(encoding="utf-8"))
    head = _git_head(REPO_ROOT)
    if approval["declared_head"] != head:
        raise SystemExit(
            f"REFUSED: the approval declares head"
            f" {approval['declared_head'][:12]}… but the checkout is at"
            f" {head[:12]}… — execute at the declared head or re-approve"
        )
    if approval["registration_sha256"] != registration_sha:
        raise SystemExit(
            "REFUSED: the approval binds a different registration content"
            " hash than the tracked registration"
        )
    _refuse_second_consumption(approval)
    world, protocol, manifest_hash = _build_world()
    if approval["dataset_manifest_hash"] != manifest_hash:
        raise SystemExit(
            "REFUSED: the verified bars manifest does not match the"
            " approved dataset_manifest_hash — swapped inputs refuse"
        )
    if approval["protocol_hash"] != protocol_hash(protocol):
        raise SystemExit("REFUSED: the loaded protocol does not match the approved protocol_hash")
    if approval["world_id"] != world.world_id:
        raise SystemExit("REFUSED: the built world does not match the approved world_id")
    permitted = _world_permitted(world)
    permitted_iso = [d.isoformat() for d in permitted]
    if approval["permitted_test_sessions"] != permitted_iso:
        raise SystemExit(
            "REFUSED: the recomputed label-complete set differs from the"
            " approved permitted set — the world changed under the"
            " approval; a new approval is required"
        )
    expected = registration["evaluation_window"]["expected_permitted"]
    if expected != permitted_iso:
        raise SystemExit(
            "REFUSED: the recomputed label-complete set differs from the"
            " registered expectation — the world changed after"
            " registration"
        )
    # the one-shot spend, durably recorded BEFORE the first trial
    # registers (a crash after this line is UNKNOWN / reconciliation,
    # never a silent retry)
    identity = _content_identity(approval)
    consumption_sha = _append_consumption(
        {
            "kind": "P4_CONSUMPTION",
            "content_identity": identity,
            "approval_sha256": _sha256_bytes(APPROVAL_PATH.read_bytes()),
            "head": head,
            "at_epoch": int(time.time()),
        }
    )
    authority = HoldoutEvaluationAuthority(
        window_id=approval["window_id"],
        world_id=approval["world_id"],
        protocol_hash_value=approval["protocol_hash"],
        registration_sha256=registration_sha,
        authority_record_sha256=_sha256_bytes(APPROVAL_PATH.read_bytes()),
        declared_head=approval["declared_head"],
        permitted_test_sessions=permitted,
    )
    menu = {config.slot_id: config for config in p4_menu()}
    registry = TrialRegistry(REGISTRY_PATH)
    decision_sessions = _decision_sessions(world, permitted)
    try:
        for slot_id in P4_SLOT_ORDER:
            config = menu[slot_id]
            _verify_config_against_registration(registration, config)
            run_dir = TRIALS_DIR / config.slot_id
            if run_dir.exists():
                raise SystemExit(
                    f"REFUSED: {run_dir} already exists — P4 executions are one-shot per slot"
                )
            run_dir.mkdir(parents=True)
            result = run_options_trial(
                dataset=world.dataset,
                surface=world.surface,
                calendar=world.grid,
                execution_calendar=world.overlay.calendar,
                protocol=protocol,
                world_id=world.world_id,
                arm=config.arm,
                strategy_config=config.strategy_config(),
                scored=_scored_for(config, world, permitted),
                model_family=config.model_family,
                model_sha256=None,
                hypothesis=config.hypothesis,
                decision_sessions=decision_sessions,
                options_manifest_hash=manifest_hash,
                registry=registry,
                artifacts_dir=run_dir,
                repo=REPO_ROOT,
                clock=lambda: G4_FIXED_CLOCK,
                split_override=OptionsSplitOverride(
                    label_horizon_sessions=P4_GEOMETRY[0],
                    embargo_sessions=P4_GEOMETRY[1],
                    val_sessions=P4_GEOMETRY[2],
                    test_sessions=P4_GEOMETRY[3],
                    roll_sessions=P4_GEOMETRY[4],
                    min_train_sessions=P4_GEOMETRY[5],
                ),
                holdout_evaluation=authority,
                liquidity_lane=2,
                flow_min_session_volume=config.flow_min_session_volume,
                score_seed=config.score_seed,
                run_index=config.run_index,
            )
            print(
                f"{config.slot_id}: {result.trial_id} ->"
                f" folds={result.n_folds} positions={result.n_positions}",
                flush=True,
            )
            executions = list(state["executions"])
            executions.append(
                {
                    "slot_id": config.slot_id,
                    "trial_id": result.trial_id,
                    "artifact_path": str(result.artifact_path.relative_to(REPO_ROOT)),
                    "n_folds": result.n_folds,
                    "n_positions": result.n_positions,
                    "at_head": head,
                }
            )
            state["executions"] = executions
            save_state(state)
    finally:
        registry.close()
    verdict = _verdict_from_state(state)
    state["verdict"] = verdict
    save_state(state)
    VERDICT_PATH.write_text(json.dumps(verdict, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"consumption: {consumption_sha[:12]}…", flush=True)
    print(
        f"VERDICT: F1 bleed_persisted={verdict['f1_bleed_persisted']}"
        f" ({verdict['f1_null_negative_count']}/3 negative)"
        f" F2 anomaly_persisted={verdict['f2_anomaly_persisted']}",
        flush=True,
    )
    return 0


def _verdict_from_state(state: dict[str, Any]) -> dict[str, Any]:
    executions = {row["slot_id"]: row for row in state["executions"]}
    bodies: dict[str, Any] = {}
    for slot_id in P4_SLOT_ORDER:
        execution = executions.get(slot_id)
        if execution is None:
            raise SystemExit(
                f"REFUSED: {slot_id} has no executed artifact recorded in"
                " the state — the verdict refuses an incomplete set"
            )
        artifact = REPO_ROOT / execution["artifact_path"]
        body = json.loads(artifact.read_text(encoding="utf-8"))
        if body.get("stamp", {}).get("trial_id") != execution["trial_id"]:
            raise SystemExit(
                f"REFUSED: {slot_id}'s artifact stamp carries"
                f" {body.get('stamp', {}).get('trial_id')!r}, the state"
                f" recorded {execution['trial_id']!r} — only the EXECUTED"
                " artifact is verdict evidence"
            )
        bodies[slot_id] = body
    return evaluate_window_a(bodies, _wave0_prior())


def verdict_only() -> int:
    state = load_state()
    verdict = _verdict_from_state(state)
    print(json.dumps(verdict, indent=2, sort_keys=True), flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--register-only", action="store_true")
    modes.add_argument("--approve", action="store_true")
    modes.add_argument("--execute", action="store_true")
    modes.add_argument("--verdict", action="store_true")
    parser.add_argument("--declared-head", help="the commit the owner declares (with --approve)")
    parser.add_argument("--reason", help="the approval's recorded reason (with --approve)")
    args = parser.parse_args(argv)
    if args.approve:
        if not args.declared_head or not args.reason:
            raise SystemExit("--approve requires --declared-head and --reason")
        return approve(args.declared_head, args.reason)
    if args.register_only:
        return register_only()
    if args.execute:
        return _execute()
    return verdict_only()


if __name__ == "__main__":
    raise SystemExit(main())
