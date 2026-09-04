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

(window-A extension, owner direction 2026-09-04) The five sealed dates
window A never evaluated (2026-07-17..2026-08-14 — not label-complete on
the window-A world) become evaluable once the world grows, as a NEW
packet under a NEW authority: ``--window window-a-ext-1`` rebinds every
driver surface (registration/authority/evidence paths, window id,
program id, run-index namespaces a-r20..r24/b-r4) to the extension; the
extension's date scope is DERIVED from the spent packet (the sealed
enumeration minus window A's registered permitted set, gated on the
tracked window-A evidence existing), so a spent date can never ride a
second window and the default invocation (window A) refuses a second
look exactly as before.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import posixpath
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

from tree_options.protocol.holdout import (  # noqa: E402
    FINAL_HOLDOUT_DATES,
    FINAL_HOLDOUT_WINDOW_ID,
)
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
# (Codex round 1 P1-2) the TRACKED one-shot record: written by --execute,
# committed by the owner after the run — it travels with the repo, so a
# SECOND CHECKOUT (whose local artifacts/ never saw the first spend)
# still refuses the same window
EVIDENCE_PATH = REPO_ROOT / "docs" / "evidence-logs" / "m4" / "m4-p4-window-a.json"
WAVE0_STATE_PATH = REPO_ROOT / "artifacts" / "theory" / "wave0" / "state.json"

# (window-A extension, owner direction 2026-09-04) the BASE window's
# tracked records — NEVER rebound: the extension's date scope derives
# from the base registration, and the base evidence's existence is what
# proves the base look was spent before any extension may exist.
WINDOW_A_REGISTRATION_PATH = REGISTRATION_PATH
WINDOW_A_EVIDENCE_PATH = EVIDENCE_PATH

# (D4, unchanged from the theory waves) H, embargo, val, test, roll,
# min_train — in GRID Fridays
P4_GEOMETRY = (5, 2, 6, 13, 13, 34)
P4_LABEL_HORIZON = P4_GEOMETRY[0]
P4_WINDOW_ID = "final-holdout-window-a"
P4_PROGRAM_ID = "p4-window-a"


@dataclass(frozen=True)
class P4Window:
    """One sealed-evaluation window's COMPLETE binding: identity, paths,
    run-index namespaces, and pre-registration texts. The module globals
    below are the ACTIVE window's binding (the module loads bound to
    WINDOW_A — the spent packet — so its one-shot refusals keep firing
    through the default invocation exactly as before); ``--window`` calls
    ``_bind_window`` ONCE at CLI entry, before any mode function runs."""

    key: str
    window_id: str
    program: str
    p4_root: Path
    registration_path: Path
    authority_root: Path
    evidence_path: Path
    null_run_base: int
    mom_b_run_index: int
    extends_window_a: bool
    null_hypothesis: str
    mom_hypothesis: str
    hold_hypothesis: str
    f1_text: str
    f2_text: str
    verdict_rule: str
    rules: dict[str, str]

    @property
    def state_path(self) -> Path:
        return self.p4_root / "state.json"

    @property
    def registry_path(self) -> Path:
        return self.p4_root / "registry.db"

    @property
    def trials_dir(self) -> Path:
        return self.p4_root / "trials"

    @property
    def scratch_root(self) -> Path:
        return self.p4_root / "scratch"

    @property
    def verdict_path(self) -> Path:
        return self.p4_root / "verdict.json"

    @property
    def approval_path(self) -> Path:
        return self.authority_root / "approval.json"

    @property
    def consumption_path(self) -> Path:
        return self.authority_root / "consumption.jsonl"


# window-A's verdict texts, hoisted so the registration's criteria rule
# interpolates them EXACTLY as the original inline f-string did (the
# tracked registration's bytes are the reference)
_WA_F1_TEXT = (
    "F1 (bleed persistence): at least 2 of the 3 window-A null seeds"
    " post negative pooled total_return"
)
_WA_F2_TEXT = (
    "F2 (anomaly persistence): BOTH momentum arms post total_return"
    " strictly above the maximum of the 3 window-A null seeds"
)

WINDOW_A = P4Window(
    key="window-a",
    window_id="final-holdout-window-a",
    program="p4-window-a",
    p4_root=REPO_ROOT / "artifacts" / "theory" / "p4",
    registration_path=REPO_ROOT / "docs" / "theory" / "p4-window-a-registration.json",
    authority_root=REPO_ROOT / "artifacts" / "p4-authority",
    evidence_path=REPO_ROOT / "docs" / "evidence-logs" / "m4" / "m4-p4-window-a.json",
    null_run_base=15,
    mom_b_run_index=3,
    extends_window_a=False,
    null_hypothesis=(
        "P4 F1 reference: the null book (seed {seed}) on window A — the"
        " out-of-sample bleed bar. Pre-registered expectation: negative"
        " total_return (fee + tick drag persists); F1 confirms at >=2 of 3"
        " seeds negative"
    ),
    mom_hypothesis=(
        "P4 F2: the mom20-quintile composition book on window A — the P3"
        " in-sample composition anomaly (+4.2%/+5.6%, both arms above the"
        " research-era null spread) tested out-of-sample. F2 replicates"
        " ONLY if BOTH arms post total_return strictly above the window-A"
        " null max; the ranking channel is NOT under test (P3 killed it)"
    ),
    hold_hypothesis=(
        "P4 disclosure: exit-2 on window A — the lose-least ladder point"
        " against the window-A null spread (secondary reading, never a"
        " verdict)"
    ),
    f1_text=_WA_F1_TEXT,
    f2_text=_WA_F2_TEXT,
    verdict_rule=(
        # byte-identical to p4_verdict._VERDICT_RULE (the spent packet's
        # --verdict re-read must render the same rule it rendered at
        # consumption) — pinned by test
        "owner ruling 2026-09-03 (return-channel dual falsifier): F1 = at"
        " least 2 of 3 window-A null seeds negative; F2 = BOTH momentum arms"
        " strictly above the max of the 3 window-A null seeds"
    ),
    rules={
        "configs": (
            "owner ruling 2026-09-03: null x3 (seeds theory-null-1/2/3,"
            " arm A) + mom20-quintile (arms A and B) + hold exit-2 (arm"
            " A) — six trials on the single window-A consumption"
        ),
        "criteria": (
            "owner ruling 2026-09-03 (return-channel dual falsifier):"
            f" {_WA_F1_TEXT}; {_WA_F2_TEXT}. Secondary disclosures (never"
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
)

# the extension's verdict texts (same hoist-and-interpolate pattern)
_EXT_F1_TEXT = (
    "F1 (bleed persistence): at least 2 of the 3 extension null"
    " seeds post negative pooled total_return"
)
_EXT_F2_TEXT = (
    "F2 (anomaly persistence): BOTH momentum arms post total_return"
    " strictly above the maximum of the 3 extension null seeds"
)

WINDOW_A_EXT_1 = P4Window(
    key="window-a-ext-1",
    window_id="final-holdout-window-a-ext-1",
    program="p4-window-a-ext-1",
    p4_root=REPO_ROOT / "artifacts" / "theory" / "p4-ext-1",
    registration_path=REPO_ROOT / "docs" / "theory" / "p4-window-a-ext-1-registration.json",
    authority_root=REPO_ROOT / "artifacts" / "p4-authority-ext-1",
    evidence_path=REPO_ROOT / "docs" / "evidence-logs" / "m4" / "m4-p4-window-a-ext-1.json",
    null_run_base=20,
    mom_b_run_index=4,
    extends_window_a=True,
    null_hypothesis=(
        "P4-ext F1 reference: the null book (seed {seed}) on the"
        " window-A extension — the out-of-sample bleed bar on the five"
        " sealed dates window A never evaluated. Pre-registered"
        " expectation: negative total_return (fee + tick drag persists);"
        " F1 confirms at >=2 of 3 seeds negative"
    ),
    mom_hypothesis=(
        "P4-ext F2: the mom20-quintile composition book on the window-A"
        " extension — the P3 in-sample composition anomaly tested"
        " out-of-sample on the dates window A never evaluated. F2"
        " replicates ONLY if BOTH arms post total_return strictly above"
        " the extension null max; the ranking channel is NOT under test"
        " (P3 killed it; window A's F2 did not fire)"
    ),
    hold_hypothesis=(
        "P4-ext disclosure: exit-2 on the window-A extension — the"
        " lose-least ladder point against the extension null spread"
        " (secondary reading, never a verdict)"
    ),
    f1_text=_EXT_F1_TEXT,
    f2_text=_EXT_F2_TEXT,
    verdict_rule=(
        "owner direction 2026-09-04 (window-A extension, return-channel"
        " dual falsifier): F1 = at least 2 of 3 extension null seeds"
        " negative; F2 = BOTH momentum arms strictly above the max of the"
        " 3 extension null seeds"
    ),
    rules={
        "configs": (
            "owner direction 2026-09-04 (window-A extension): the same"
            " six-trial budget as window A — null x3 (seeds"
            " theory-null-1/2/3, arm A) + mom20-quintile (arms A and B)"
            " + hold exit-2 (arm A) — six trials on the single"
            " extension consumption; run indices continue the per-arm"
            " namespace (a-r20..r24, b-r4)"
        ),
        "criteria": (
            "owner direction 2026-09-04 (window-A extension): the same"
            " return-channel dual falsifier, re-registered BEFORE the"
            f" look at the extension dates — {_EXT_F1_TEXT};"
            f" {_EXT_F2_TEXT}. Secondary"
            " disclosures (never verdicts): cohort ICs vs the wave-0"
            " calibrated 2-SE bar 2*prior/sqrt(3), and exit-2 against"
            " the extension null spread"
        ),
        "date_scope": (
            "owner direction 2026-09-04 (window-A extension): ONLY the"
            " sealed dates window A never consumed — derived from the"
            " tracked window-A registration (the enumeration minus its"
            " expected_permitted) and gated on the tracked window-A"
            " evidence existing (the base look must be spent); within"
            " that scope, the label-complete subset computed by the"
            " machinery (>= label_horizon grid steps of headroom to the"
            " grown world's last grid session)"
        ),
        "seeds": (
            "owner direction 2026-09-04 (window-A extension): reuse"
            " theory-null-1/2/3 (paired continuity with the window-A"
            " null spread; the hash-null draw on the extension dates is"
            " fresh anyway)"
        ),
    },
)

P4_WINDOWS: dict[str, P4Window] = {window.key: window for window in (WINDOW_A, WINDOW_A_EXT_1)}

# the ACTIVE window's binding — the module loads bound to WINDOW_A
_ACTIVE_WINDOW = WINDOW_A
_NULL_HYPOTHESIS = WINDOW_A.null_hypothesis
_MOM_HYPOTHESIS = WINDOW_A.mom_hypothesis
_HOLD_HYPOTHESIS = WINDOW_A.hold_hypothesis
_F1_TEXT = WINDOW_A.f1_text
_F2_TEXT = WINDOW_A.f2_text
_RULES = WINDOW_A.rules
_NULL_RUN_BASE = 15  # the wave consumed a-r1..a-r14 and b-r1..b-r2
_MOM_B_RUN_INDEX = 3

# every global _bind_window moves — exported so tests can snapshot and
# restore the binding (a leaked extension binding would silently
# re-target later tests' paths)
_BINDING_GLOBALS = (
    "P4_WINDOW_ID",
    "P4_PROGRAM_ID",
    "P4_ROOT",
    "REGISTRATION_PATH",
    "STATE_PATH",
    "REGISTRY_PATH",
    "TRIALS_DIR",
    "SCRATCH_ROOT",
    "AUTHORITY_ROOT",
    "APPROVAL_PATH",
    "CONSUMPTION_PATH",
    "VERDICT_PATH",
    "EVIDENCE_PATH",
    "_NULL_HYPOTHESIS",
    "_MOM_HYPOTHESIS",
    "_HOLD_HYPOTHESIS",
    "_F1_TEXT",
    "_F2_TEXT",
    "_RULES",
    "_NULL_RUN_BASE",
    "_MOM_B_RUN_INDEX",
    "_ACTIVE_WINDOW",
)


def _bind_window(window: P4Window) -> None:
    """Rebind the driver to ``window`` — the ONE rebind point, called at
    CLI entry (and by tests). Window A's module-load binding is the spent
    packet's surface; the extension moves EVERY path, identity, text, and
    run-index namespace so no extension artifact can land on a window-A
    surface or vice versa."""
    global P4_WINDOW_ID, P4_PROGRAM_ID, P4_ROOT, REGISTRATION_PATH, STATE_PATH
    global REGISTRY_PATH, TRIALS_DIR, SCRATCH_ROOT, AUTHORITY_ROOT, APPROVAL_PATH
    global CONSUMPTION_PATH, VERDICT_PATH, EVIDENCE_PATH, _ACTIVE_WINDOW
    global _NULL_HYPOTHESIS, _MOM_HYPOTHESIS, _HOLD_HYPOTHESIS
    global _F1_TEXT, _F2_TEXT, _RULES, _NULL_RUN_BASE, _MOM_B_RUN_INDEX
    P4_WINDOW_ID = window.window_id
    P4_PROGRAM_ID = window.program
    P4_ROOT = window.p4_root
    REGISTRATION_PATH = window.registration_path
    STATE_PATH = window.state_path
    REGISTRY_PATH = window.registry_path
    TRIALS_DIR = window.trials_dir
    SCRATCH_ROOT = window.scratch_root
    AUTHORITY_ROOT = window.authority_root
    APPROVAL_PATH = window.approval_path
    CONSUMPTION_PATH = window.consumption_path
    VERDICT_PATH = window.verdict_path
    EVIDENCE_PATH = window.evidence_path
    _NULL_HYPOTHESIS = window.null_hypothesis
    _MOM_HYPOTHESIS = window.mom_hypothesis
    _HOLD_HYPOTHESIS = window.hold_hypothesis
    _F1_TEXT = window.f1_text
    _F2_TEXT = window.f2_text
    _RULES = window.rules
    _NULL_RUN_BASE = window.null_run_base
    _MOM_B_RUN_INDEX = window.mom_b_run_index
    _ACTIVE_WINDOW = window
    _refuse_aliased_roots(window)


_VOLATILE_ROOTS = (Path("/tmp"), Path("/var/tmp"))


def _refuse_aliased_roots(window: P4Window) -> None:
    """(Codex round 1 F3) the bound window's writable surfaces must be
    real directories-in-waiting, checked ONCE at bind before any mode
    function can write:

    - every EXISTING path component from the repo root down to each
      artifact root is a real directory, never a symlink (a preplanted
      ``p4-ext-1 -> p4`` alias routes extension state/registry/trials
      writes into the SPENT packet's tree);
    - neither artifact root RESOLVES onto the other window's roots
      (cross-window aliasing) or under a volatile root (/tmp and
      friends — authority may not live where a reboot wipes it);
    - the tracked file surfaces (registration, evidence) refuse a
      pre-existing symlink leaf.

    Mid-invocation swaps by a concurrent writer remain out of scope
    under the owner's ruled threat model; this closes the sequential
    preplant."""
    for label, root in (("p4-root", window.p4_root), ("authority-root", window.authority_root)):
        try:
            parts = root.relative_to(REPO_ROOT).parts
        except ValueError:
            parts = ()
        component = REPO_ROOT
        for part in parts:
            component = component / part
            if component.is_symlink():
                raise SystemExit(
                    f"REFUSED: the {label} path component {component} is a"
                    " symlink — the window's writable surfaces must be real"
                    " directories (an alias onto another window's surface"
                    " or a volatile root refuses)"
                )
        resolved = root.resolve()
        for volatile in _VOLATILE_ROOTS:
            if resolved == volatile or volatile in resolved.parents:
                raise SystemExit(
                    f"REFUSED: the {label} {root} resolves onto the"
                    f" volatile root {volatile} — durable authority may"
                    " not live where a reboot wipes it"
                )
        other = WINDOW_A if window is not WINDOW_A else WINDOW_A_EXT_1
        other_label = "extension" if window is WINDOW_A else "base"
        for other_kind, other_root in (
            ("p4-root", other.p4_root),
            ("authority-root", other.authority_root),
        ):
            other_resolved = other_root.resolve()
            if (
                resolved == other_resolved
                or other_resolved in resolved.parents
                or resolved in other_resolved.parents
            ):
                raise SystemExit(
                    f"REFUSED: the {label} {root} resolves onto the"
                    f" {other_label} window's {other_kind} — the two"
                    " windows' writable surfaces must stay disjoint"
                )
    for label, tracked in (
        ("registration", window.registration_path),
        ("evidence", window.evidence_path),
    ):
        if tracked.is_symlink():
            raise SystemExit(
                f"REFUSED: the {label} path {tracked} is a symlink — the"
                " tracked surfaces are real files, never aliases"
            )


_DEFAULT_DELTA = Decimal("0.45")
_DEFAULT_DTE = 45
_DEFAULT_EXIT = 4
_DEFAULT_FLOW = 100
_MOM = "mom20-quintile/v1"


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


def _committed_file_bytes(path: Path) -> bytes | None:
    """The file's bytes as committed at HEAD, or None if HEAD does not
    carry it (uncommitted, rewritten, or not a git tree — all fail-closed
    for the caller's committed-clean comparison)."""
    try:
        rel = path.relative_to(REPO_ROOT).as_posix()
        out = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "show", f"HEAD:{rel}"],
            capture_output=True,
            check=True,
        )
    except (OSError, ValueError, subprocess.CalledProcessError):
        return None
    return out.stdout


def _active_date_scope() -> frozenset[str]:
    """The sealed dates the ACTIVE window may evaluate (ISO strings).

    Window A: the full ratified enumeration. The extension: ONLY the
    dates window A never consumed — derived from the BASE window's
    TRACKED registration (the enumeration minus its expected_permitted),
    and only after the tracked window-A evidence exists: the base look
    must be spent before a second window may exist at all. Spent dates
    can never ride a second window — that is the one-shot the seal
    exists for.

    (Codex round 1 F2) the base records are VERIFIED, not merely present:
    the registration must be the COMMITTED-CLEAN canonical bytes at HEAD
    (git-tracked, so an offline rewrite is a working-tree diff, not a
    silent input), it must carry the window-A program/window identity,
    and the evidence must CORROBORATE the registration's permitted set —
    the scope and the spent-date check must not both derive from one
    untrusted list."""
    if not _ACTIVE_WINDOW.extends_window_a:
        return frozenset(FINAL_HOLDOUT_DATES)
    if not WINDOW_A_EVIDENCE_PATH.is_file():
        raise SystemExit(
            f"REFUSED: the tracked window-A evidence {WINDOW_A_EVIDENCE_PATH}"
            " does not exist — the extension may only be registered after"
            " the base window was consumed"
        )
    if not WINDOW_A_REGISTRATION_PATH.is_file():
        raise SystemExit(
            f"REFUSED: the tracked window-A registration"
            f" {WINDOW_A_REGISTRATION_PATH} does not exist — the extension"
            " scope derives from the base window's registered permitted set"
        )
    raw = WINDOW_A_REGISTRATION_PATH.read_bytes()
    committed = _committed_file_bytes(WINDOW_A_REGISTRATION_PATH)
    if committed is None or committed != raw:
        raise SystemExit(
            "REFUSED: the tracked window-A registration is not"
            " committed-clean at HEAD — the extension scope derives from"
            " the CANONICAL spent packet, never a working-tree rewrite"
        )
    base = json.loads(raw.decode("utf-8"))
    if (
        base.get("program") != "p4-window-a"
        or (base.get("evaluation_window") or {}).get("window_id") != FINAL_HOLDOUT_WINDOW_ID
    ):
        raise SystemExit(
            "REFUSED: the base registration does not carry the window-A"
            " program/window identity — this is not the tracked"
            " registration and the scope refuses to derive from it"
        )
    consumed = (base.get("evaluation_window") or {}).get("expected_permitted")
    if not isinstance(consumed, list) or not consumed:
        raise SystemExit(
            "REFUSED: the window-A registration carries no expected_permitted"
            " set — the extension scope cannot derive from it"
        )
    evidence = json.loads(WINDOW_A_EVIDENCE_PATH.read_text(encoding="utf-8"))
    if evidence.get("program") != "p4-window-a" or (
        evidence.get("permitted_test_sessions") != consumed
    ):
        raise SystemExit(
            "REFUSED: the tracked window-A evidence does not corroborate"
            " the base registration's permitted set — the base-consumed"
            " record and the base registration disagree; reconcile with"
            " the owner before any extension exists"
        )
    sealed = frozenset(FINAL_HOLDOUT_DATES)
    foreign = sorted(set(consumed) - sealed)
    if foreign:
        raise SystemExit(
            f"REFUSED: the window-A registration names dates {foreign} that"
            " are not sealed window-A dates — this is not the tracked"
            " registration and the scope refuses to derive from it"
        )
    return sealed - set(consumed)


def _world_permitted(world) -> tuple[date, ...]:
    world_last = max(bar.session for bar in world.dataset.bars)
    permitted = label_complete_permitted_sessions(
        world.grid.sessions(), world_last, P4_LABEL_HORIZON
    )
    scope = _active_date_scope()
    scoped = tuple(d for d in permitted if d.isoformat() in scope)
    if not _ACTIVE_WINDOW.extends_window_a:
        return scoped
    # (Codex round 1 F1) the extension consumes ALL of its derived scope
    # or NONE of it: the window is one-shot, so a partial registration
    # would irreversibly strand the still-immature dates (a later look at
    # them needs a NEWLY ratified window). Grow the world until every
    # scoped date has >= label_horizon grid Fridays of headroom.
    immature = sorted(scope - {d.isoformat() for d in scoped})
    if immature:
        raise SystemExit(
            "REFUSED: the extension is all-or-nothing — the scoped dates"
            f" {immature} are not yet label-complete on this world, and a"
            " partial consumption would strand them forever (the window"
            " is one-shot); grow the world (a sealed date needs >="
            f" {P4_LABEL_HORIZON} grid Fridays of headroom) and register"
            " only when ALL scoped dates are mature"
        )
    return scoped


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
    excluded = sorted(_active_date_scope() - {d.isoformat() for d in permitted})
    registration = {
        "program": P4_PROGRAM_ID,
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
        "rules": _RULES,
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
        f"NEXT: commit {REGISTRATION_PATH},"
        " then the owner approves at the declared head"
        " "
        "(scripts/run_p4_holdout.py"
        f" --window {_ACTIVE_WINDOW.key}"
        " --approve --declared-head <sha> --reason '...')",
        flush=True,
    )
    return 0


def _require_committed_registration(declared_head: str) -> None:
    """The registration must live in HEAD's tree with the on-disk bytes —
    an uncommitted or edited registration cannot be approved. The path is
    the ACTIVE window's registration (window A and its extension each
    carry their own tracked file)."""
    registration_rel = REGISTRATION_PATH.relative_to(REPO_ROOT).as_posix()
    blob = subprocess.run(
        [
            "git",
            "-C",
            str(REPO_ROOT),
            "cat-file",
            "-e",
            f"{declared_head}:{registration_rel}",
        ],
        capture_output=True,
    )
    if blob.returncode != 0:
        raise SystemExit(
            f"REFUSED: {registration_rel} is not"
            f" committed at {declared_head[:12]}… — commit the registration"
            " before approving"
        )
    committed = subprocess.run(
        [
            "git",
            "-C",
            str(REPO_ROOT),
            "show",
            f"{declared_head}:{registration_rel}",
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
    # (Codex round 1 P1-2) O_CREAT|O_EXCL: the exists() check and the
    # write are one OS-atomic act — two concurrent approvals cannot both
    # land
    payload = json.dumps(record, indent=2, sort_keys=True) + "\n"
    try:
        fd = os.open(
            APPROVAL_PATH,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
            0o644,
        )
    except FileExistsError:
        raise SystemExit(
            f"REFUSED: {APPROVAL_PATH} already exists — the approval is the owner's ONE act"
        ) from None
    try:
        os.write(fd, payload.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    print(
        f"approved: head={declared_head[:12]}… permitted={record['permitted_test_sessions']}",
        flush=True,
    )
    # (Codex round 1 F8) the printed command must name the ACTIVE window —
    # following a window-A-spelled instruction after approving the
    # extension invokes the spent default binding and refuses
    print(
        f"NEXT: scripts/run_p4_holdout.py --window {_ACTIVE_WINDOW.key} --execute (one-shot)",
        flush=True,
    )
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


_HEX64 = set("0123456789abcdef")


def _is_hex64(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and set(value) <= _HEX64


def _validate_approval_record(record: Any) -> dict[str, Any]:
    """(Codex round 1 P1-1) the approval RECORD is the owner act — a
    hand-written lookalike must refuse. Shape AND binding: the ratified
    kind, the ratified window, full binding hashes, a well-formed head,
    exactly the sealed-date shape for the permitted set, a non-empty
    reason, a positive timestamp. Anything else is not an approval."""
    if not isinstance(record, dict):
        raise SystemExit("REFUSED: the approval record is not an object")
    if record.get("kind") != "P4_HOLDOUT_APPROVAL":
        raise SystemExit(
            f"REFUSED: the approval kind is {record.get('kind')!r}, not P4_HOLDOUT_APPROVAL"
        )
    if record.get("window_id") != P4_WINDOW_ID:
        raise SystemExit(f"REFUSED: the approval names window {record.get('window_id')!r}")
    # world_id is the lane-2 world's COMPOSITE identity (e.g.
    # "massive-derived/AAPL+…/d467d7878609-…"), not a hex sha — the
    # execute path BINDS it twice (against the built world and the
    # registration), so validation here only demands a non-empty string
    # (the 2026-09-03 first execute refused on a hex requirement the real
    # world id can never satisfy — caught pre-consumption, zero spent)
    if not isinstance(record.get("world_id"), str) or not record["world_id"].strip():
        raise SystemExit("REFUSED: the approval's world_id is empty")
    for field in ("protocol_hash", "dataset_manifest_hash", "registration_sha256"):
        if not _is_hex64(record.get(field)):
            raise SystemExit(f"REFUSED: the approval's {field} is not a 64-hex sha256")
    declared = record.get("declared_head")
    if not isinstance(declared, str) or not 40 <= len(declared) <= 64 or set(declared) > _HEX64:
        raise SystemExit(f"REFUSED: the approval's declared_head {declared!r} is not a commit hash")
    permitted = record.get("permitted_test_sessions")
    if not isinstance(permitted, list) or not permitted:
        raise SystemExit(
            "REFUSED: the approval's permitted set is not a non-empty window-A date list"
        )
    # (Codex round 1 F4) every element must be a STRING before any set
    # membership — a frozenset membership test on an unhashable element
    # raises TypeError where the pre-extension list-membership check
    # refused cleanly; the default window's refusal must stay a refusal
    if any(not isinstance(d, str) for d in permitted):
        raise SystemExit(
            "REFUSED: the approval's permitted set is not a non-empty window-A date list"
        )
    # the permitted set must live inside the ACTIVE window's scope: the
    # extension may never re-permit a date window A consumed
    scope = _active_date_scope()
    sealed = frozenset(FINAL_HOLDOUT_DATES)
    spent = sorted(d for d in permitted if d in sealed and d not in scope)
    unsealed = sorted(d for d in permitted if d not in sealed)
    if spent:
        raise SystemExit(
            f"REFUSED: the approval's permitted set names {spent} —"
            " consumed by window A (spent dates can never ride a second"
            " window; that is the one-shot the seal exists for)"
        )
    if unsealed:
        raise SystemExit(
            f"REFUSED: the approval's permitted set names {unsealed} — not sealed window-A dates"
        )
    if not isinstance(record.get("reason"), str) or not record["reason"].strip():
        raise SystemExit("REFUSED: the approval carries no reason")
    if (
        not isinstance(record.get("at_epoch"), int)
        or isinstance(record.get("at_epoch"), bool)
        or record["at_epoch"] <= 0
    ):
        raise SystemExit("REFUSED: the approval's at_epoch is not a positive integer")
    return record


def _refuse_second_consumption(approval: dict[str, Any]) -> None:
    """The one-shot check: any consumption record already in the ledger
    whose content identity matches this approval's content refuses — a
    second look at the same window is the thing the seal exists to
    prevent (head-INDEPENDENT: a re-run at a new head is still the same
    content). The TRACKED evidence record closes the same hole across
    checkouts: it travels with the repo, so a second worktree (whose
    local artifacts/ know nothing of the first spend) still refuses."""
    if EVIDENCE_PATH.is_file():
        raise SystemExit(
            f"REFUSED: the tracked window-A evidence {EVIDENCE_PATH}"
            " already exists — this window was consumed (possibly in"
            " another checkout); the evidence file is the durable record"
        )
    identity = _content_identity(approval)
    for record in _read_consumptions():
        if record.get("content_identity") == identity:
            raise SystemExit(
                "REFUSED: this window-A content was already consumed"
                f" (consumption {record.get('record_sha256', '')[:12]}… at"
                f" head {str(record.get('head', ''))[:12]}…) — a second"
                " look is the one thing the seal exists to prevent"
            )


def _consume_authority(approval: dict[str, Any], head: str) -> str:
    """(Codex round 1 P1-2) the duplicate check and the append are ONE
    flock-held act: read-verify-refuse-append under an exclusive lock on
    the ledger file, so two concurrent executions cannot both pass the
    check and both spend."""
    import fcntl

    AUTHORITY_ROOT.mkdir(parents=True, exist_ok=True)
    identity = _content_identity(approval)
    fd = os.open(CONSUMPTION_PATH, os.O_CREAT | os.O_RDWR, 0o644)
    handle = os.fdopen(fd, "r+", encoding="utf-8")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        existing = _read_consumptions()
        for record in existing:
            if record.get("content_identity") == identity:
                raise SystemExit(
                    "REFUSED: this window-A content was already consumed"
                    f" (consumption {record.get('record_sha256', '')[:12]}…)"
                    " — a second look is the one thing the seal exists to"
                    " prevent"
                )
        prev = existing[-1]["record_sha256"] if existing else "0" * 64
        record = {
            "kind": "P4_CONSUMPTION",
            "content_identity": identity,
            "approval_sha256": _sha256_bytes(APPROVAL_PATH.read_bytes()),
            "head": head,
            "at_epoch": int(time.time()),
            "prev_record_sha256": prev,
        }
        record_sha = _sha256_json(record)
        record["record_sha256"] = record_sha
        handle.seek(0, os.SEEK_END)
        handle.write(json.dumps(record, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
        return record_sha
    finally:
        handle.close()


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
    approval = _validate_approval_record(json.loads(APPROVAL_PATH.read_text(encoding="utf-8")))
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
    if approval["permitted_test_sessions"] != expected:
        raise SystemExit(
            "REFUSED: the approval's permitted set is not the REGISTERED"
            " expected_permitted set — a lookalike approval that swapped"
            " the window refuses"
        )
    if expected != permitted_iso:
        raise SystemExit(
            "REFUSED: the recomputed label-complete set differs from the"
            " registered expectation — the world changed after"
            " registration"
        )
    # the one-shot spend, durably recorded BEFORE the first trial
    # registers (a crash after this line is UNKNOWN / reconciliation,
    # never a silent retry) — check and append are ONE flock-held act
    consumption_sha = _consume_authority(approval, head)
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
    # (Codex round 1 P1-2/P1-3) the TRACKED evidence record: the durable,
    # repo-travelling proof of the consumption + the verdict. The owner
    # commits it after the run; a second checkout (or an erased local
    # ledger) still refuses the same window at the next --execute.
    EVIDENCE_PATH.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE_PATH.write_text(
        json.dumps(
            {
                "program": P4_PROGRAM_ID,
                "consumption_sha256": consumption_sha,
                "content_identity": _content_identity(approval),
                "approval_sha256": _sha256_bytes(APPROVAL_PATH.read_bytes()),
                "declared_head": head,
                "permitted_test_sessions": [d.isoformat() for d in permitted],
                "trials": {row["slot_id"]: row["trial_id"] for row in state["executions"]},
                "verdict": verdict,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"consumption: {consumption_sha[:12]}…", flush=True)
    print(
        f"VERDICT: F1 bleed_persisted={verdict['f1_bleed_persisted']}"
        f" ({verdict['f1_null_negative_count']}/3 negative)"
        f" F2 anomaly_persisted={verdict['f2_anomaly_persisted']}",
        flush=True,
    )
    return 0


def _trials_rel_prefix() -> str:
    """The verdict's artifact-path prefix for the ACTIVE window (repo
    relative, trailing slash) — extension evidence lives under the
    extension's own trials directory, never window A's."""
    return TRIALS_DIR.relative_to(REPO_ROOT).as_posix() + "/"


def _verdict_from_state(state: dict[str, Any]) -> dict[str, Any]:
    """(Codex round 1 P1-3) the verdict certifies only EXECUTED evidence.
    Beyond the stamp binding: each artifact must live at the driver's own
    TRIALS_DIR path shape, the six paths must be DISTINCT, the trial
    registry must hold each trial COMPLETE, a consumption record for THIS
    content must exist, and every stamp's git_sha must equal the approval's
    declared head. Fabricated state + JSON alone can no longer produce a
    verdict."""
    executions = {row["slot_id"]: row for row in state["executions"]}
    if not APPROVAL_PATH.is_file():
        raise SystemExit("REFUSED: no approval record — nothing has been consumed to verdict")
    approval = _validate_approval_record(json.loads(APPROVAL_PATH.read_text(encoding="utf-8")))
    consumed = any(
        record.get("content_identity") == _content_identity(approval)
        for record in _read_consumptions()
    )
    if not consumed:
        raise SystemExit(
            "REFUSED: no consumption record for this approval — the verdict"
            " follows a consumption, never a bare state file"
        )
    registry = TrialRegistry(REGISTRY_PATH)
    try:
        bodies: dict[str, Any] = {}
        seen_paths: set[str] = set()
        for slot_id in P4_SLOT_ORDER:
            execution = executions.get(slot_id)
            if execution is None:
                raise SystemExit(
                    f"REFUSED: {slot_id} has no executed artifact recorded in"
                    " the state — the verdict refuses an incomplete set"
                )
            artifact_path = execution["artifact_path"]
            expected_prefix = f"{_trials_rel_prefix()}{slot_id}/"
            # normpath FIRST: a `..` segment escapes the slot directory
            # while keeping the literal prefix (Codex round 1 P1-3)
            if not posixpath.normpath(artifact_path).startswith(expected_prefix):
                raise SystemExit(
                    f"REFUSED: {slot_id}'s recorded artifact path"
                    f" {artifact_path!r} is not under {expected_prefix!r}"
                    " — only the driver's own trial directory is verdict"
                    " evidence"
                )
            if artifact_path in seen_paths:
                raise SystemExit(
                    f"REFUSED: artifact path {artifact_path!r} serves two"
                    " slots — the six executions are six DISTINCT artifacts"
                )
            seen_paths.add(artifact_path)
            if not registry.is_registered(execution["trial_id"]) or (
                registry.status(execution["trial_id"]) != "COMPLETED"
            ):
                raise SystemExit(
                    f"REFUSED: {slot_id}'s trial {execution['trial_id']!r} is"
                    " not registered-and-COMPLETED in the registry — the"
                    " verdict certifies finished executions only"
                )
            artifact = REPO_ROOT / artifact_path
            body = json.loads(artifact.read_text(encoding="utf-8"))
            if body.get("stamp", {}).get("trial_id") != execution["trial_id"]:
                raise SystemExit(
                    f"REFUSED: {slot_id}'s artifact stamp carries"
                    f" {body.get('stamp', {}).get('trial_id')!r}, the state"
                    f" recorded {execution['trial_id']!r} — only the EXECUTED"
                    " artifact is verdict evidence"
                )
            if body.get("stamp", {}).get("git_sha") != approval["declared_head"]:
                raise SystemExit(
                    f"REFUSED: {slot_id}'s artifact was stamped at"
                    f" {str(body.get('stamp', {}).get('git_sha'))[:12]}…, not"
                    " the approval's declared head — evidence from another"
                    " head is not this consumption's verdict"
                )
            bodies[slot_id] = body
    finally:
        registry.close()
    # (Codex round 1 F6) the verdict's self-describing rule follows the
    # ACTIVE window — extension evidence carries the extension's rule,
    # never the base window's ruling text
    return evaluate_window_a(bodies, _wave0_prior(), rule=_ACTIVE_WINDOW.verdict_rule)


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
    parser.add_argument(
        "--window",
        choices=sorted(P4_WINDOWS),
        default="window-a",
        help=(
            "the sealed window to bind (default: window-a, the SPENT"
            " packet — its one-shot refusals keep firing; window-a-ext-1"
            " is the extension of the five dates window A never"
            " evaluated, own registration/authority/evidence surfaces)"
        ),
    )
    parser.add_argument("--declared-head", help="the commit the owner declares (with --approve)")
    parser.add_argument("--reason", help="the approval's recorded reason (with --approve)")
    args = parser.parse_args(argv)
    _bind_window(P4_WINDOWS[args.window])
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
