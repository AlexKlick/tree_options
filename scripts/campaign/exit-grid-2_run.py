#!/usr/bin/env python3
"""campaign-2026-09 EXIT-GRID-2 runner (scopes ``c09-eg2-a/-b/-c``, menu order 4).

The exit/hold-policy grid on the two survivors, executed exactly as the
sealed menu entry ``slots[4]`` of
``docs/theory/campaign-2026-09-registration.json`` (menu v3, sha256
``4aca2101...``) and its source doc
``docs/theory/campaign-2026-09/slots/exit-grid-2.md`` (sha-pinned by the
menu) specify. This runner only BINDS to the registration: ids, scopes,
cell semantics, sealed eras, statistics conventions, acceptance criteria
and verdict vocabulary are read from the menu / slot doc and any drift
refuses before anything runs (the wave-0 ``verify_config_against_ledger``
and tnull ``load_and_bind`` discipline).

What runs (the slot's fold mapping, binding):

* INNER loop: EMPTY BY DESIGN for scopes A and B (menu
  ``fold_mapping.inner``; every level pinned ex-ante, frozen-grid house
  precedent). Nothing is tuned, selected, or re-gridded -- so no tuning
  fold is consumed and NO inner-fold selection exists. The registered
  cells' ONE scored run per cell on their sealed era (menu
  ``fold_mapping.sealed_eg2a`` / ``sealed_eg2b``) is the registered
  experiment itself, the census EXIT-GRID.md / XSMOM-EXITGRID.md idiom
  this slot extends; multiplicity is charged at the full declared scope
  count (m=9 / 10 / 5) regardless of run subset.
* Scope C (6 configs, ``c-*``): DATA-GATED-NOT-RUN per the menu's
  ``data_gates`` -- the long-dated option-bar capture is still in flight
  (on-disk bars end 2026-09-03, short of the EG2-C span that mirrors A)
  AND the post-M0 short-leg machinery does not exist
  (``research_protocol.yaml`` ``short_options``). ``--withdraw-c``
  records the withdrawal with that evidence; the ids stay REGISTERED,
  never run, and no proxy is improvised.

Arms (spot proxy, close-trigger census semantics -- a condition observed
at a session close exits AT that observed close; levels are ``E*(1-x)``
or ``M*(1-x)`` only; context conditions use PRIOR-session values;
time-stop = close[t+20]):

* EG2-A (``c09-eg2-a``, m=9): ``pead_beat`` hold-20, beats-only stream
  recomputed from ``src/tree_options/desk/signals.py`` (move >= +1.5%,
  clean-back 5 / hole / prior-gap guards), 26 calendar-covered single
  stocks, sealed calendar era 2024-09-05..2026-08-28, entries restricted
  to hold-complete (the boundary session computed by the engine),
  5bp/10bp RT, paired per signal vs ``a-hold20``; cluster = signal
  session.
* EG2-B (``c09-eg2-b``, m=10): ``xsmom_top3`` monthly card exits, 36
  tradables ranked by the desk code (close(t)/close(t-273)-1, skip never
  skips), top 3 held 20; sealed basis era 2024-09-04..2026-08-28 -> 23
  rebalances 2024-10-01..2026-08-03, 69 legs (registration cross-check,
  refusal on drift); TQQQ/SQQQ picks logged and included in spot stats;
  5bp RT, paired per leg vs ``b-hold20``; cluster = rebalance session.

Statistics exactly as the family machinery (EXIT-GRID.md): mean paired
diff (net@5bp; the round trip cancels -- one RT per signal under every
variant), naive t, day-clustered t, conservative t = min(naive,
clustered); one-sided Bonferroni alpha = 0.05/m on the conservative t
(normal approx, pinned exactly at freeze); economic floor paired mean
>= 5bp; power floors A >= 30 sealed beats, B >= 60 legs and >= 20
rebalance clusters. Per-cell verdicts PASS / FAIL / UNDERPOWERED
(/ DATA-GATED-NOT-RUN for C); scope verdicts HOLD-STANDS or
EXIT-SUCCESSOR-NOMINATED. Base cells (``a-hold20`` / ``b-hold20``) are
the paired REFERENCE (census precedent: pass column "-"), stamped with
level statistics and excluded from per-cell verdict arithmetic. NOTHING
ADOPTS: a PASS only nominates an EXIT-SUCCESSOR for the forward sealed
card chain (menu ``rules.adoption``); the RESEARCH-LEDGER is not touched
by executors.

Phases (INV-13: registration precedes outcome, always):

* ``--plan``       read-only: bind every sealed input, print the geometry;
* ``--selftest``   synthetic-bar simulator checks (no real data, no outcomes);
* ``--register``   write the 27 trial rows (REGISTERED, no outcome) to the
                   slot registry sqlite BEFORE any outcome is viewed;
* ``--withdraw-c`` record scope C's DATA-GATED-NOT-RUN withdrawal + gate
                   evidence (never a proxy, never a run);
* ``--execute``    one-shot per A/B trial (REGISTERED -> RUNNING ->
                   COMPLETED with the artifact as metrics_uri);
* ``--evaluate``   read ONLY the executed artifacts (stamp-bound to their
                   trial ids), apply the menu criteria, stamp the round-1
                   verdicts (per cell, per scope, withdrawal block).

Environment pins (every invocation): DESK_STORE / DESK_PAPER_DIR /
TREX_DESK_STATE point at the MAIN checkout's artifacts (data lives on
main; the runner + registration live in this execution worktree) and
PYTHONPATH pins the interpreter to THIS worktree's src.
"""

from __future__ import annotations

import argparse
import csv
import fcntl
import hashlib
import json
import math
import os
import statistics
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence

for _name in (
    "OPENBLAS_NUM_THREADS",
    "OMP_NUM_THREADS",
    "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
):
    os.environ.setdefault(_name, "1")

REPO_ROOT = Path(__file__).resolve().parents[2]  # the EXECUTION worktree
if str(REPO_ROOT / "src") not in sys.path:  # pragma: no cover - import plumbing
    sys.path.insert(0, str(REPO_ROOT / "src"))

from tree_options.registry.scope import TrialScope  # noqa: E402
from tree_options.registry.sqlite import TrialRegistry  # noqa: E402
from tree_options.schemas.trial import TrialRecord  # noqa: E402

# Data lives in the MAIN checkout; the runner + registration live in the
# execution worktree. Both are pinned by sha256 against the menu below.
MAIN_ROOT = Path("/home/alexk/documents/tree_options")
REGISTRATION_PATH = REPO_ROOT / "docs" / "theory" / "campaign-2026-09-registration.json"
REGISTRATION_SIDECAR = Path(str(REGISTRATION_PATH) + ".sha256")
SLOT_DOC_PATH = REPO_ROOT / "docs" / "theory" / "campaign-2026-09" / "slots" / "exit-grid-2.md"
PROTOCOL_PATH = REPO_ROOT / "research_protocol.yaml"
PANEL_PATH = MAIN_ROOT / "artifacts" / "paper-trades" / "ohlc-panel.json"
EARNINGS_PATH = MAIN_ROOT / "artifacts" / "paper-trades" / "earnings-calendar.json"
CALENDAR_PATH = MAIN_ROOT / "data" / "calendar" / "nyse_sessions_2018_01_02_2026_12_31.json"
VIX_PATH = MAIN_ROOT / "artifacts" / "desk-store" / "indices" / "VIX.csv"
VIX3M_PATH = MAIN_ROOT / "artifacts" / "desk-store" / "indices" / "VIX3M.csv"
TNULL_V3_PATH = MAIN_ROOT / "artifacts" / "campaign-2026-09" / "tnull" / "calibration-v3.json"

CAMPAIGN_DIR = MAIN_ROOT / "artifacts" / "campaign-2026-09"
REGISTRY_PATH = CAMPAIGN_DIR / "exit-grid-2.db"
SLOT_DIR = CAMPAIGN_DIR / "exit-grid-2"
TRIALS_DIR = SLOT_DIR / "trials"
VERDICTS_PATH = SLOT_DIR / "verdicts-round1.json"
WITHDRAWAL_PATH = SLOT_DIR / "scope-c-withdrawal.json"
LOCK_PATH = SLOT_DIR / "execute.lock"

SLOT_ID = "exit-grid-2"
SCOPE_A = "c09-eg2-a"
SCOPE_B = "c09-eg2-b"
SCOPE_C = "c09-eg2-c"
MODEL_FAMILY = "exit-policy/1"
TRIAL_GENERATION = 1

# The pinned geometry (menu fold_mapping.sealed_* + slot doc section 4/5).
PHANTOM_ISO = "2025-01-09"  # ledger ruling 2026-09-23: not a session
ERA_A = ("2024-09-05", "2026-08-28")  # sealed_eg2a: calendar era, beats-only
ERA_B = ("2024-09-04", "2026-08-28")  # sealed_eg2b: basis era on the card stream
B_REBALANCES_EXPECTED = ("2024-10-01", "2026-08-03")  # 23 rebalances / 69 legs
B_EXPECTED_REBALANCES = 23
B_EXPECTED_LEGS = 69
HOLD_SESSIONS = 20
RT_PRIMARY = 0.0005  # 5bp round trip, primary
RT_ROBUST = 0.0010  # 10bp round trip (scope A disclosure; the paired diff cancels either way)
ALPHA = 0.05
M_ALTERNATIVES = {SCOPE_A: 9, SCOPE_B: 10, SCOPE_C: 5}
ECON_FLOOR = 0.0005  # paired mean >= 5bp (the intraday-pilot convention)
POWER_FLOOR_A_SIGNALS = 30
POWER_FLOOR_B_LEGS = 60
POWER_FLOOR_B_CLUSTERS = 20
POWER_FLOOR_C_EPISODES = 40  # disclosed in the withdrawal stamp; C never runs
BREADTH_THRESHOLD = Decimal("0.40")

# The 27 registered cells (menu config_ids, in menu order). Semantics are
# the slot doc's tables verbatim; the spec dicts drive the simulator.
CONFIGS: dict[str, dict[str, Any]] = {
    # -- scope A: pead_beat hold-20 x post-entry conditioning (spot proxy) --
    "a-hold20": {"scope": SCOPE_A, "family": "pead_beat", "base": True, "spec": {"kind": "hold"},
                 "semantics": "the sealed rule: time-stop at close[t+20]"},
    "a-adv05-arm5": {"scope": SCOPE_A, "family": "pead_beat", "base": False,
                     "spec": {"kind": "adverse", "level": "0.05", "arm": 5},
                     "semantics": "first close at i>=5 with close/entry-1 <= -5%"},
    "a-adv10-arm5": {"scope": SCOPE_A, "family": "pead_beat", "base": False,
                     "spec": {"kind": "adverse", "level": "0.10", "arm": 5},
                     "semantics": "first close at i>=5 with close/entry-1 <= -10%"},
    "a-adv05-arm10": {"scope": SCOPE_A, "family": "pead_beat", "base": False,
                      "spec": {"kind": "adverse", "level": "0.05", "arm": 10},
                      "semantics": "first close at i>=10 with close/entry-1 <= -5%"},
    "a-adv10-arm10": {"scope": SCOPE_A, "family": "pead_beat", "base": False,
                      "spec": {"kind": "adverse", "level": "0.10", "arm": 10},
                      "semantics": "first close at i>=10 with close/entry-1 <= -10%"},
    "a-trail10-arm5": {"scope": SCOPE_A, "family": "pead_beat", "base": False,
                       "spec": {"kind": "trail", "level": "0.10", "arm": 5},
                       "semantics": "first close at i>=5 with close <= running-max(prior closes)*(1-0.10)"},
    "a-trail15-arm5": {"scope": SCOPE_A, "family": "pead_beat", "base": False,
                       "spec": {"kind": "trail", "level": "0.15", "arm": 5},
                       "semantics": "first close at i>=5 with close <= running-max(prior closes)*(1-0.15)"},
    "a-vixterm": {"scope": SCOPE_A, "family": "pead_beat", "base": False,
                  "spec": {"kind": "vixterm", "arm": 1},
                  "semantics": "first close whose PRIOR session has VIX close >= VIX3M close"},
    "a-breadth40": {"scope": SCOPE_A, "family": "pead_beat", "base": False,
                    "spec": {"kind": "breadth", "arm": 1},
                    "semantics": "first close whose PRIOR session's breadth (36 tradables, close > prior close) < 0.40"},
    "a-mom20flip": {"scope": SCOPE_A, "family": "pead_beat", "base": False,
                    "spec": {"kind": "mom20flip", "arm": 1},
                    "semantics": "first close whose PRIOR session's name mom20 (close/close[-20]-1) <= 0"},
    # -- scope B: xsmom_top3 monthly card exits (spot proxy) --
    "b-hold20": {"scope": SCOPE_B, "family": "xsmom_top3", "base": True, "spec": {"kind": "hold"},
                 "semantics": "the sealed rule: exit at close[t+20]"},
    "b-trail10-arm5": {"scope": SCOPE_B, "family": "xsmom_top3", "base": False,
                       "spec": {"kind": "trail", "level": "0.10", "arm": 5},
                       "semantics": "trailing -10% off the running max close, armed i>=5"},
    "b-trail15-arm5": {"scope": SCOPE_B, "family": "xsmom_top3", "base": False,
                       "spec": {"kind": "trail", "level": "0.15", "arm": 5},
                       "semantics": "trailing -15% off the running max close, armed i>=5"},
    "b-trail20-arm5": {"scope": SCOPE_B, "family": "xsmom_top3", "base": False,
                       "spec": {"kind": "trail", "level": "0.20", "arm": 5},
                       "semantics": "trailing -20% off the running max close, armed i>=5"},
    "b-adv10": {"scope": SCOPE_B, "family": "xsmom_top3", "base": False,
                "spec": {"kind": "adverse", "level": "0.10", "arm": 1},
                "semantics": "static adverse: first close <= entry*0.90"},
    "b-adv15": {"scope": SCOPE_B, "family": "xsmom_top3", "base": False,
                "spec": {"kind": "adverse", "level": "0.15", "arm": 1},
                "semantics": "static adverse: first close <= entry*0.85"},
    "b-adv10-arm10": {"scope": SCOPE_B, "family": "xsmom_top3", "base": False,
                      "spec": {"kind": "adverse", "level": "0.10", "arm": 10},
                      "semantics": "adverse -10% armed i>=10"},
    "b-mom20flip": {"scope": SCOPE_B, "family": "xsmom_top3", "base": False,
                    "spec": {"kind": "mom20flip", "arm": 1},
                    "semantics": "exit leg at first close whose PRIOR session's leg mom20 <= 0"},
    "b-vixterm": {"scope": SCOPE_B, "family": "xsmom_top3", "base": False,
                  "spec": {"kind": "vixterm", "arm": 1, "all_legs": True},
                  "semantics": "exit ALL open legs at first close whose PRIOR session has VIX >= VIX3M"},
    "b-breadth40": {"scope": SCOPE_B, "family": "xsmom_top3", "base": False,
                    "spec": {"kind": "breadth", "arm": 1},
                    "semantics": "exit at first close whose PRIOR session's breadth < 0.40"},
    "b-monthend": {"scope": SCOPE_B, "family": "xsmom_top3", "base": False,
                   "spec": {"kind": "monthend"},
                   "semantics": "calendar-aligned time-stop: exit at the month's last NYSE session in the hold window"},
    # -- scope C: options-expression exits (DATA-GATED; never run) --
    "c-ts20": {"scope": SCOPE_C, "family": "pead_beat-debit-spread", "base": True,
               "spec": {"kind": "option-time-stop", "sessions": 20},
               "semantics": "time-stop: close the spread at session t+20 (BASE)"},
    "c-ts5": {"scope": SCOPE_C, "family": "pead_beat-debit-spread", "base": False,
              "spec": {"kind": "option-time-stop", "sessions": 5},
              "semantics": "time-stop at t+5"},
    "c-ts10": {"scope": SCOPE_C, "family": "pead_beat-debit-spread", "base": False,
               "spec": {"kind": "option-time-stop", "sessions": 10},
               "semantics": "time-stop at t+10"},
    "c-dstop70": {"scope": SCOPE_C, "family": "pead_beat-debit-spread", "base": False,
                  "spec": {"kind": "option-delta-stop", "leg": "short", "abs_delta_ge": "0.70"},
                  "semantics": "exit when short-leg abs(delta) >= 0.70"},
    "c-dstop15": {"scope": SCOPE_C, "family": "pead_beat-debit-spread", "base": False,
                  "spec": {"kind": "option-delta-stop", "leg": "long", "abs_delta_le": "0.15"},
                  "semantics": "exit when long-leg abs(delta) <= 0.15"},
    "c-expiry": {"scope": SCOPE_C, "family": "pead_beat-debit-spread", "base": False,
                 "spec": {"kind": "option-expiry"},
                 "semantics": "ride to expiry settlement (wave0 arm-B precedent)"},
}
MENU_CONFIG_IDS = tuple(CONFIGS)

VERDICT_VOCABULARY = ("PASS", "FAIL", "UNDERPOWERED", "DATA-GATED-NOT-RUN",
                      "HOLD-STANDS", "EXIT-SUCCESSOR-NOMINATED")


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _git_head(repo: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(f"REFUSED: git rev-parse HEAD failed: {result.stderr.strip()[:120]}")
    return result.stdout.strip()


# ---- sealed inputs -------------------------------------------------------------------


class Refused(RuntimeError):
    """A binding refusal: the sealed registration and the execution disagree."""


class SealedCalendar:
    """The sealed NYSE calendar minus the 2025-01-09 phantom session.

    (ClosureCorrectedCalendar idiom from scripts/xsmom_12_1_study.py; the
    sealed calendar FILE stays byte-identical, the walker removes the
    closure itself. previous_session uses bisect_LEFT -- the session
    strictly before d, never d itself.)"""

    def __init__(self, sessions_iso: Sequence[str]) -> None:
        kept = [d for d in sessions_iso if d != PHANTOM_ISO]
        self.removed = tuple(sorted(set(sessions_iso) & {PHANTOM_ISO}))
        self._sessions = tuple(date.fromisoformat(d) for d in kept)
        self._ordinals = {s: i for i, s in enumerate(self._sessions)}
        if len(self._ordinals) != len(self._sessions):
            raise Refused("sealed calendar carries duplicate sessions")

    def sessions(self) -> tuple[date, ...]:
        return self._sessions

    def is_session(self, d: date) -> bool:
        return d in self._ordinals

    def ordinal(self, d: date) -> int:
        try:
            return self._ordinals[d]
        except KeyError:
            raise Refused(f"{d.isoformat()} is not a session") from None

    def nth_after(self, d: date, n: int) -> date:
        i = self.ordinal(d) + n
        if not 0 <= i < len(self._sessions):
            raise Refused(f"{n} sessions after {d.isoformat()} is outside the calendar")
        return self._sessions[i]

    def previous_session(self, d: date) -> date | None:
        import bisect

        sessions = self._sessions
        i = bisect.bisect_left(sessions, d) - 1
        return sessions[i] if i >= 0 else None

    def first_session_after(self, d: date) -> date | None:
        import bisect

        sessions = self._sessions
        i = bisect.bisect_right(sessions, d)
        return sessions[i] if i < len(sessions) else None

    def is_first_session_of_month(self, d: date) -> bool:
        if not self.is_session(d):
            return False
        prev = self.previous_session(d)
        return prev is None or (prev.year, prev.month) != (d.year, d.month)


def _read_index_closes(path: Path) -> dict[str, Decimal]:
    closes: dict[str, Decimal] = {}
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            iso = (row.get("date") or "").strip()
            close = (row.get("close") or "").strip()
            if iso and close:
                closes[iso] = Decimal(close)
    if not closes:
        raise Refused(f"{path.name}: no (date, close) rows parsed")
    return closes


@dataclass(frozen=True)
class Inputs:
    menu: Mapping[str, Any]
    menu_sha256: str
    slot: Mapping[str, Any]
    protocol_raw_sha256: str
    protocol_canonical_sha256: str
    panel: Mapping[str, Mapping[str, Mapping[str, Any]]]
    panel_sha256: str
    earnings: Mapping[str, Sequence[str]]
    earnings_sha256: str
    calendar: SealedCalendar
    calendar_sha256: str
    vix_closes: dict[str, Decimal]
    vix_sha256: str
    vix3m_closes: dict[str, Decimal]
    vix3m_sha256: str
    tradables: tuple[str, ...]  # 36: panel minus SPY (== desk XSMOM_TRADABLES)
    chain35: tuple[str, ...]  # 35: panel minus TQQQ/SQQQ
    reporters: tuple[str, ...]  # the calendar's 26 single-stock names
    dataset_manifest_hash: str
    slot_doc_sha256: str


def load_and_bind() -> Inputs:
    """Read every sealed input and BIND it to the menu; any mismatch refuses."""

    menu_sha256 = _sha256_file(REGISTRATION_PATH)
    sidecar = REGISTRATION_SIDECAR.read_text().split()
    if not sidecar or sidecar[0] != menu_sha256:
        raise Refused(
            f"{REGISTRATION_PATH.name}: sha256 {menu_sha256} does not match its sidecar"
            " -- the registration is not sealed"
        )
    menu = json.loads(REGISTRATION_PATH.read_text(encoding="utf-8"))
    slot = next((s for s in menu["slots"] if s.get("slot_id") == SLOT_ID), None)
    if slot is None or slot.get("order") != 4 or slot.get("family") != "EXIT-GRID-2":
        raise Refused("the menu's order-4 EXIT-GRID-2 slot is missing or malformed")
    if tuple(slot.get("config_ids", ())) != MENU_CONFIG_IDS:
        raise Refused("menu exit-grid-2 config_ids are not the 27 registered ids in order")
    scopes = {sc["scope_id"]: sc for sc in slot["scope_ids"]}
    if [scopes[s]["config_count"] for s in (SCOPE_A, SCOPE_B, SCOPE_C)] != [10, 11, 6]:
        raise Refused("menu scope config counts are not 10 / 11 / 6")
    if slot.get("alternatives_count") != 24 or slot.get("config_count") != 27:
        raise Refused("menu exit-grid-2 is not 27 cells / 24 alternatives")
    if tuple(slot.get("verdict_vocabulary", ())) != VERDICT_VOCABULARY:
        raise Refused("menu verdict vocabulary is not the pre-declared six")
    fm = slot.get("fold_mapping", {})
    if "EMPTY BY DESIGN" not in fm.get("inner", ""):
        raise Refused("menu fold_mapping.inner is not the sealed EMPTY-BY-DESIGN declaration")
    # sealed_eg2a pins the calendar era outright; sealed_eg2b pins the
    # rebalance bounds + leg count (the basis-era dates 2024-09-04..2026-08-28
    # live in the sha-pinned slot doc and are bound against it below)
    text_a = fm.get("sealed_eg2a", "")
    if ERA_A[0] not in text_a or ERA_A[1] not in text_a:
        raise Refused(f"menu fold_mapping.sealed_eg2a does not pin the sealed era {ERA_A}")
    text_b = fm.get("sealed_eg2b", "")
    for anchor in (B_REBALANCES_EXPECTED[0], B_REBALANCES_EXPECTED[1], "69 legs"):
        if anchor not in text_b:
            raise Refused(f"menu fold_mapping.sealed_eg2b does not pin '{anchor}'")
    slot_doc_text = SLOT_DOC_PATH.read_text(encoding="utf-8")
    for anchor in (ERA_B[0], ERA_B[1], "2025-01-09", "close(t)/close(t-273)"):
        if anchor not in slot_doc_text:
            raise Refused(f"the sealed slot doc does not carry the pinned anchor '{anchor}'")
    for gate in slot.get("data_gates", []):
        if "c-" in gate and "DATA-GATED-NOT-RUN" not in "".join(slot.get("data_gates", [])):
            raise Refused("menu data_gates do not carry scope C's DATA-GATED-NOT-RUN rule")

    # protocol: raw bytes must equal the menu pin; canonical hash re-stamped (INV-14)
    from tree_options.protocol.loader import default_protocol, protocol_hash

    protocol_raw_sha256 = _sha256_file(PROTOCOL_PATH)
    if menu["protocol_hash"] != protocol_raw_sha256:
        raise Refused(
            "research_protocol.yaml raw sha256 does not match the menu's protocol_hash"
            " -- a protocol change requires a NEW registration"
        )
    protocol_canonical_sha256 = protocol_hash(default_protocol())

    # the source slot doc is sha-pinned by the menu (seal chain)
    slot_doc_sha256 = _sha256_file(SLOT_DOC_PATH)
    pinned_doc = menu["dataset_pinning"].get(str(SLOT_DOC_PATH.relative_to(REPO_ROOT)))
    if pinned_doc is None:
        pinned_doc = menu["dataset_pinning"].get("docs/theory/campaign-2026-09/slots/exit-grid-2.md")
    if pinned_doc is None or pinned_doc != slot_doc_sha256:
        raise Refused(
            f"{SLOT_DOC_PATH.name}: sha256 {slot_doc_sha256} != the menu's pinned slot-doc hash"
        )

    # data: the five pinned inputs this slot consumes
    pinning = menu["dataset_pinning"]
    panel_sha256 = _sha256_file(PANEL_PATH)
    earnings_sha256 = _sha256_file(EARNINGS_PATH)
    calendar_sha256 = _sha256_file(CALENDAR_PATH)
    vix_sha256 = _sha256_file(VIX_PATH)
    vix3m_sha256 = _sha256_file(VIX3M_PATH)
    for label, got in (
        ("artifacts/paper-trades/ohlc-panel.json", panel_sha256),
        ("artifacts/paper-trades/earnings-calendar.json", earnings_sha256),
        ("data/calendar/nyse_sessions_2018_01_02_2026_12_31.json", calendar_sha256),
        ("artifacts/desk-store/indices/VIX.csv", vix_sha256),
        ("artifacts/desk-store/indices/VIX3M.csv", vix3m_sha256),
    ):
        want = pinning.get(label)
        if want is None or want != got:
            raise Refused(f"{label}: sha256 {got} != the menu's pinned {want} -- swapped inputs refuse")

    panel = json.loads(PANEL_PATH.read_text(encoding="utf-8"))
    earnings = json.loads(EARNINGS_PATH.read_text(encoding="utf-8"))
    calendar_json = json.loads(CALENDAR_PATH.read_text(encoding="utf-8"))
    calendar = SealedCalendar(calendar_json["sessions"])
    if calendar.removed != (PHANTOM_ISO,):
        raise Refused(f"the phantom session was not removed: {calendar.removed}")

    # universes, pinned to the panel itself and cross-checked against the
    # desk config (menu rules.calendar_and_universe)
    from tree_options.desk.universe import CHAIN_UNIVERSE, XSMOM_TRADABLES

    panel_names = sorted(panel)
    if len(panel_names) != 37:
        raise Refused(f"panel carries {len(panel_names)} names, expected the sealed 37")
    tradables = tuple(n for n in panel_names if n != "SPY")
    chain35 = tuple(n for n in panel_names if n not in ("TQQQ", "SQQQ"))
    if len(tradables) != 36 or len(chain35) != 35:
        raise Refused("universe derivation is not 36 tradables / 35 chain names")
    if set(tradables) != set(XSMOM_TRADABLES):
        raise Refused("panel-minus-SPY does not equal the desk's sealed xsmom.tradables")
    if not set(chain35) <= set(CHAIN_UNIVERSE):
        raise Refused("panel-minus-TQQQ/SQQQ is not inside the desk's chain universe")
    reporters = tuple(sorted(n for n, reps in earnings.items() if reps))
    if len(reporters) != 26 or not set(reporters) <= set(chain35):
        raise Refused(
            f"earnings-calendar reporters are not the 26 chain-universe names: {len(reporters)}"
        )

    # menu rules.sequencing (AMENDMENT v3): family scoring is unfrozen ONLY
    # by a CALIBRATED calibration-v3.json citing THIS menu's sha
    if not TNULL_V3_PATH.exists():
        raise Refused(
            f"{TNULL_V3_PATH} is missing -- T-NULL calibration precedes every family"
            " (menu rules.sequencing)"
        )
    tnull_v3 = json.loads(TNULL_V3_PATH.read_text(encoding="utf-8"))
    stamp = tnull_v3.get("stamp", {})
    if stamp.get("registration_menu_sha256") != menu_sha256:
        raise Refused("calibration-v3.json does not cite this menu's sha256")
    if tnull_v3.get("verdict", {}).get("slot") != "CALIBRATED":
        raise Refused(
            "the T-NULL calibration is not CALIBRATED -- family scoring stays frozen"
            " pending an operator ruling (menu rules.sequencing)"
        )

    vix_closes = _read_index_closes(VIX_PATH)
    vix3m_closes = _read_index_closes(VIX3M_PATH)

    manifest_body = "".join(
        f"{label}\0{pinning[label]}\n"
        for label in (
            "artifacts/paper-trades/ohlc-panel.json",
            "artifacts/paper-trades/earnings-calendar.json",
            "data/calendar/nyse_sessions_2018_01_02_2026_12_31.json",
            "artifacts/desk-store/indices/VIX.csv",
            "artifacts/desk-store/indices/VIX3M.csv",
        )
    )
    dataset_manifest_hash = hashlib.sha256(manifest_body.encode("utf-8")).hexdigest()
    return Inputs(
        menu=menu,
        menu_sha256=menu_sha256,
        slot=slot,
        protocol_raw_sha256=protocol_raw_sha256,
        protocol_canonical_sha256=protocol_canonical_sha256,
        panel=panel,
        panel_sha256=panel_sha256,
        earnings=earnings,
        earnings_sha256=earnings_sha256,
        calendar=calendar,
        calendar_sha256=calendar_sha256,
        vix_closes=vix_closes,
        vix_sha256=vix_sha256,
        vix3m_closes=vix3m_closes,
        vix3m_sha256=vix3m_sha256,
        tradables=tradables,
        chain35=chain35,
        reporters=reporters,
        dataset_manifest_hash=dataset_manifest_hash,
        slot_doc_sha256=slot_doc_sha256,
    )


# ---- context feature maps (all PRIOR-session by construction) --------------------------


@dataclass(frozen=True)
class ContextMaps:
    vixterm: dict[str, bool]  # session iso -> VIX close >= VIX3M close
    vixterm_missing: tuple[str, ...]  # era sessions lacking either index close
    breadth: dict[str, float]  # session iso -> fraction of the 36 tradables up
    mom20: dict[tuple[str, str], float]  # (name, session iso) -> close/close[-20]-1
    month_last: dict[str, bool]  # session iso -> is the month's last NYSE session


def build_context_maps(inputs: Inputs) -> ContextMaps:
    cal = inputs.calendar
    sessions = cal.sessions()

    vixterm: dict[str, bool] = {}
    missing: list[str] = []
    for s in sessions:
        iso = s.isoformat()
        v = inputs.vix_closes.get(iso)
        m = inputs.vix3m_closes.get(iso)
        if v is None or m is None:
            missing.append(iso)
        else:
            vixterm[iso] = v >= m

    # breadth(s) = fraction of the 36 tradables with close(s) > close(prev
    # session); a name missing either bar simply is not "up" (denominator 36;
    # INV-10: the feature is read on the PRIOR session, never the fill session)
    breadth: dict[str, float] = {}
    panel = inputs.panel
    for i in range(1, len(sessions)):
        iso = sessions[i].isoformat()
        prev_iso = sessions[i - 1].isoformat()
        up = 0
        for name in inputs.tradables:
            bars = panel.get(name)
            if not bars:
                continue
            here = bars.get(iso)
            there = bars.get(prev_iso)
            if here is None or there is None:
                continue
            if Decimal(str(here["close"])) > Decimal(str(there["close"])):
                up += 1
        breadth[iso] = up / len(inputs.tradables)

    # mom20(name, s) = close(s)/close(20 own-series bars back) - 1 (census
    # indicators convention: 21 closes)
    mom20: dict[tuple[str, str], float] = {}
    for name in inputs.tradables:
        bars = panel.get(name)
        if not bars:
            continue
        ss = sorted(bars)
        for i in range(20, len(ss)):
            mom20[(name, ss[i])] = float(
                Decimal(str(bars[ss[i]]["close"])) / Decimal(str(bars[ss[i - 20]]["close"])) - 1
            )

    month_last: dict[str, bool] = {}
    for i, s in enumerate(sessions):
        iso = s.isoformat()
        if i + 1 < len(sessions):
            nxt = sessions[i + 1]
            month_last[iso] = (s.year, s.month) != (nxt.year, nxt.month)
        else:
            month_last[iso] = True  # calendar end guard (never a hold target in-era)

    return ContextMaps(
        vixterm=vixterm,
        vixterm_missing=tuple(missing),
        breadth=breadth,
        mom20=mom20,
        month_last=month_last,
    )


# ---- the exit simulator (close-trigger census semantics) -------------------------------


@dataclass(frozen=True)
class ExitFill:
    i: int  # sessions after entry (1..20)
    reason: str


def simulate_exit(
    spec: Mapping[str, Any],
    entry: Decimal,
    path: Sequence[tuple[int, str, Decimal]],  # (i, session iso, close)
    ctx: ContextMaps,
    name: str,
) -> ExitFill:
    """One exit policy over one hold path. A condition observed at a session
    close exits AT that observed close; levels are E*(1-x) / M*(1-x) only;
    no intraday fills are simulated, so no gap clamping exists. The time-stop
    is the last path close (i = HOLD_SESSIONS)."""
    kind = spec["kind"]
    if kind == "hold":
        return ExitFill(len(path), "time-stop")

    if kind == "adverse":
        level = Decimal(spec["level"])
        threshold = entry * (Decimal(1) - level)
        arm = spec.get("arm", 1)
        for i, _iso, close in path:
            if i >= arm and close <= threshold:
                return ExitFill(i, "adverse")
        return ExitFill(len(path), "time-stop")

    if kind == "trail":
        level = Decimal(spec["level"])
        arm = spec.get("arm", 1)
        running_max = entry  # running max over PRIOR closes (entry included)
        for i, _iso, close in path:
            if i >= arm and close <= running_max * (Decimal(1) - level):
                return ExitFill(i, "trail")
            if close > running_max:
                running_max = close
        return ExitFill(len(path), "time-stop")

    if kind == "vixterm":
        arm = spec.get("arm", 1)
        for i, iso, _close in path:
            prior = inputs_prev_session_iso(iso)
            if prior is not None and i >= arm and ctx.vixterm.get(prior, False):
                return ExitFill(i, "vixterm")
        return ExitFill(len(path), "time-stop")

    if kind == "breadth":
        arm = spec.get("arm", 1)
        for i, iso, _close in path:
            prior = inputs_prev_session_iso(iso)
            if prior is not None and i >= arm:
                b = ctx.breadth.get(prior)
                if b is not None and Decimal(str(b)) < BREADTH_THRESHOLD:
                    return ExitFill(i, "breadth")
        return ExitFill(len(path), "time-stop")

    if kind == "mom20flip":
        arm = spec.get("arm", 1)
        for i, iso, _close in path:
            prior = inputs_prev_session_iso(iso)
            if prior is None or i < arm:
                continue
            m = ctx.mom20.get((name, prior))
            if m is not None and m <= 0:
                return ExitFill(i, "mom20flip")
        return ExitFill(len(path), "time-stop")

    if kind == "monthend":
        for i, iso, _close in path:
            if ctx.month_last.get(iso, False):
                return ExitFill(i, "month-end")
        return ExitFill(len(path), "time-stop")

    raise Refused(f"unknown exit spec kind {kind!r}")


_PREV_SESSION_CACHE: dict[str, str | None] = {}
_CALENDAR_REF: SealedCalendar | None = None


def _bind_calendar_for_simulator(cal: SealedCalendar) -> None:
    global _CALENDAR_REF
    _CALENDAR_REF = cal
    _PREV_SESSION_CACHE.clear()


def inputs_prev_session_iso(iso: str) -> str | None:
    if iso in _PREV_SESSION_CACHE:
        return _PREV_SESSION_CACHE[iso]
    if _CALENDAR_REF is None:
        raise Refused("the simulator calendar was never bound")
    prev = _CALENDAR_REF.previous_session(date.fromisoformat(iso))
    out = prev.isoformat() if prev is not None else None
    _PREV_SESSION_CACHE[iso] = out
    return out


# ---- the two signal streams -----------------------------------------------------------


@dataclass(frozen=True)
class Signal:
    name: str
    entry: str  # session iso
    entry_close: Decimal
    path: tuple[tuple[int, str, Decimal], ...]  # (i, iso, close) for i=1..20
    cluster: str  # signal session (A) / rebalance session (B)
    detail: str


@dataclass(frozen=True)
class StreamBuild:
    signals: tuple[Signal, ...]
    disclosure: dict[str, Any]


def _hold_path(
    inputs: Inputs, name: str, entry: str
) -> tuple[tuple[tuple[int, str, Decimal], ...], str | None]:
    """The 20 post-entry sessions' closes (sealed calendar walk, house hold
    filter: every session in (t, t+20] must exist in the name's bars)."""
    cal = inputs.calendar
    bars = inputs.panel[name]
    i = cal.ordinal(date.fromisoformat(entry))
    sessions = cal.sessions()
    window = sessions[i + 1 : i + HOLD_SESSIONS + 1]
    if len(window) != HOLD_SESSIONS:
        return (), "hold-incomplete:calendar-end"
    path = []
    for j, s in enumerate(window, start=1):
        iso = s.isoformat()
        bar = bars.get(iso)
        if bar is None:
            return (), "hold-incomplete:name-hole"
        path.append((j, iso, Decimal(str(bar["close"]))))
    return tuple(path), None


def build_pead_stream(inputs: Inputs) -> StreamBuild:
    """EG2-A: the beats-only pead_beat stream recomputed from the DESK code
    (signals.py), sealed calendar era, hold-complete entries only."""
    from tree_options.desk.signals import pead_beats

    cal = inputs.calendar
    lo, hi = (date.fromisoformat(x) for x in ERA_A)
    # gather beats per session via the desk evaluator (clean-back / hole /
    # prior-gap guards included); dedup per (name, session), earliest report
    by_key: dict[tuple[str, str], tuple[str, Any]] = {}
    n_evaluated = 0
    n_fired_raw = 0
    deduped = 0  # same (name, entry session) reporting twice: earliest report wins
    for s in cal.sessions():
        if not (lo <= s <= hi):
            continue
        res = pead_beats(inputs.panel, inputs.earnings, s, cal)
        n_evaluated += len(res.evaluated)
        for ev in res.evaluated:
            if not ev.fires:
                continue
            n_fired_raw += 1
            key = (ev.name, s.isoformat())
            prev = by_key.get(key)
            if prev is None:
                by_key[key] = (ev.report_date, ev.move)
            elif ev.report_date < prev[0]:
                by_key[key] = (ev.report_date, ev.move)
                deduped += 1
            else:
                deduped += 1
    signals: list[Signal] = []
    n_dropped_hold = 0
    boundary_last_entry: str | None = None
    for (name, entry_iso) in sorted(by_key, key=lambda k: (k[1], k[0])):
        report_date, move = by_key[(name, entry_iso)]
        path, _why = _hold_path(inputs, name, entry_iso)
        if path is None or not path:
            n_dropped_hold += 1
            continue
        entry_bar = inputs.panel[name].get(entry_iso)
        if entry_bar is None:  # pragma: no cover - pead_beats never fires without one
            n_dropped_hold += 1
            continue
        if boundary_last_entry is None or entry_iso > boundary_last_entry:
            boundary_last_entry = entry_iso
        signals.append(
            Signal(
                name=name,
                entry=entry_iso,
                entry_close=Decimal(str(entry_bar["close"])),
                path=path,
                cluster=entry_iso,
                detail=f"post-report:{report_date};move:{move}",
            )
        )
    return StreamBuild(
        signals=tuple(signals),
        disclosure={
            "era": list(ERA_A),
            "n_evaluated_events": n_evaluated,
            "n_beats_fired_raw": n_fired_raw,
            "n_beats_deduped_per_name_per_session": deduped,
            "n_signals_hold_complete": len(signals),
            "n_dropped_hold": n_dropped_hold,
            "boundary_last_entry": boundary_last_entry,
            "convention": "entry at close(first post-report session); move >= +1.5% per desk signals.py",
        },
    )


def build_xsmom_stream(inputs: Inputs) -> StreamBuild:
    """EG2-B: the monthly xsmom_top3 card stream on the sealed basis era
    (desk ranking code), hold-complete legs only; 23 rebalances / 69 legs
    cross-checked against the registration."""
    from tree_options.desk.signals import xsmom_top3

    cal = inputs.calendar
    lo, hi = (date.fromisoformat(x) for x in ERA_B)
    signals: list[Signal] = []
    rebalances: list[str] = []
    n_dropped_legs = 0
    barred_picks: list[str] = []
    for s in cal.sessions():
        if not (lo <= s <= hi) or not cal.is_first_session_of_month(s):
            continue
        res = xsmom_top3(inputs.panel, s, cal)
        if not res.fires:
            continue
        iso = s.isoformat()
        legs_here = 0
        for name in res.top3:
            if name in ("TQQQ", "SQQQ"):
                barred_picks.append(f"{name}@{iso}")
            path, _why = _hold_path(inputs, name, iso)
            if path is None or not path:
                n_dropped_legs += 1
                continue
            entry_bar = inputs.panel[name].get(iso)
            if entry_bar is None:  # xsmom_rank already excludes those names
                n_dropped_legs += 1
                continue
            signals.append(
                Signal(
                    name=name,
                    entry=iso,
                    entry_close=Decimal(str(entry_bar["close"])),
                    path=path,
                    cluster=iso,
                    detail=f"rebalance:{iso};rank-top3",
                )
            )
            legs_here += 1
        if legs_here:
            rebalances.append(iso)
    # registration cross-checks (the sealed stream is pinned by the menu)
    if len(rebalances) != B_EXPECTED_REBALANCES or len(signals) != B_EXPECTED_LEGS:
        raise Refused(
            f"sealed_eg2b drift: derived {len(rebalances)} rebalances / {len(signals)} legs,"
            f" the menu pins {B_EXPECTED_REBALANCES} / {B_EXPECTED_LEGS}"
        )
    if rebalances[0] != B_REBALANCES_EXPECTED[0] or rebalances[-1] != B_REBALANCES_EXPECTED[1]:
        raise Refused(
            f"sealed_eg2b drift: rebalances {rebalances[0]}..{rebalances[-1]},"
            f" the menu pins {B_REBALANCES_EXPECTED[0]}..{B_REBALANCES_EXPECTED[1]}"
        )
    return StreamBuild(
        signals=tuple(signals),
        disclosure={
            "era": list(ERA_B),
            "n_rebalances": len(rebalances),
            "n_legs": len(signals),
            "first_rebalance": rebalances[0],
            "last_rebalance": rebalances[-1],
            "n_dropped_legs_hold": n_dropped_legs,
            "tqqq_sqqq_picks": barred_picks,
            "ranking_convention": "desk signals.py close(t)/close(t-273)-1 (the code behind PROTOCOL-XSMOM.md)",
        },
    )


# ---- statistics (family machinery conventions) ----------------------------------------


def _t_stats(diffs: Sequence[float], clusters: Sequence[str]) -> tuple[float, float]:
    """Naive t and day-clustered t (cluster = signal / rebalance session);
    the census backtest_exit_grid.t_stat conventions."""
    n = len(diffs)
    if n < 2:
        return (float("nan"), float("nan"))
    m = statistics.fmean(diffs)
    sd = statistics.stdev(diffs)
    naive = m / (sd / n**0.5) if sd else (float("inf") if m else 0.0)
    by_day: dict[str, list[float]] = {}
    for c, d in zip(clusters, diffs, strict=True):
        by_day.setdefault(c, []).append(d)
    day_means = [statistics.fmean(v) for v in by_day.values()]
    if len(day_means) < 2:
        return (naive, naive)
    sd_days = statistics.stdev(day_means)
    clustered = (
        m / (sd_days / len(day_means) ** 0.5)
        if sd_days
        else (float("inf") if m else 0.0)
    )
    return (naive, clustered)


def _critical_t(m: int) -> float:
    """One-sided Bonferroni normal-approx quantile at alpha = 0.05/m,
    pinned exactly at freeze (menu acceptance criterion; the slot doc's
    ~2.54 / 2.576 / 2.33 are display roundings of these)."""
    from statistics import NormalDist

    return NormalDist().inv_cdf(1.0 - ALPHA / m)


def run_config(
    inputs: Inputs,
    ctx_maps: ContextMaps,
    stream: StreamBuild,
    config_id: str,
) -> dict[str, Any]:
    """Execute one registered cell over the sealed stream: every signal's
    exit under this cell's policy, its net return, and (for alternatives)
    the paired diff vs the SAME signal set under the scope's hold-20 base."""
    cfg = CONFIGS[config_id]
    trades = []
    for sig in stream.signals:
        fill = simulate_exit(cfg["spec"], sig.entry_close, sig.path, ctx_maps, sig.name)
        exit_i, exit_iso, exit_close = sig.path[fill.i - 1]
        gross = float(exit_close / sig.entry_close - Decimal(1))
        trades.append(
            {
                "name": sig.name,
                "entry": sig.entry,
                "exit": exit_iso,
                "exit_i": exit_i,
                "reason": fill.reason,
                "gross": gross,
                "net5": gross - RT_PRIMARY,
                "net10": gross - RT_ROBUST,
                "cluster": sig.cluster,
                "detail": sig.detail,
            }
        )
    by_key = {(t["name"], t["entry"]): t for t in trades}
    return {"config_id": config_id, "trades": trades, "by_key": by_key}


def cell_statistics(base_run: Mapping[str, Any] | None, run: Mapping[str, Any]) -> dict[str, Any]:
    """Level + paired statistics for one cell (family machinery)."""
    trades = run["trades"]
    n = len(trades)
    rets = [t["net5"] for t in trades]
    mix: dict[str, int] = {}
    for t in trades:
        mix[t["reason"]] = mix.get(t["reason"], 0) + 1
    level = {
        "n": n,
        "hit": (sum(1 for r in rets if r > 0) / n) if n else None,
        "gross_mean": statistics.fmean([t["gross"] for t in trades]) if trades else None,
        "net5_mean": statistics.fmean(rets) if trades else None,
        "net10_mean": statistics.fmean([t["net10"] for t in trades]) if trades else None,
        "mean_exit_i": statistics.fmean([t["exit_i"] for t in trades]) if trades else None,
        "fill_mix": mix,
    }
    out: dict[str, Any] = {"level": level, "paired": None}
    if base_run is None:
        return out
    base_by_key = base_run["by_key"]
    if set(base_by_key) != set(run["by_key"]):
        raise Refused("paired signal sets differ between variant and base -- machinery defect")
    diffs = []
    clusters = []
    for key, t in run["by_key"].items():
        diffs.append(t["net5"] - base_by_key[key]["net5"])  # one RT per signal: cancels
        clusters.append(t["cluster"])
    naive, clustered = _t_stats(diffs, clusters)
    n_clusters = len(set(clusters))
    out["paired"] = {
        "n": len(diffs),
        "n_clusters": n_clusters,
        "paired_mean": statistics.fmean(diffs) if diffs else None,
        "t_naive": naive,
        "t_clustered": clustered,
        "t_conservative": (
            min(naive, clustered) if not (math.isnan(naive) or math.isnan(clustered)) else float("nan")
        ),
    }
    return out


# ---- registry / stamp plumbing ---------------------------------------------------------


def _trial_id(config_id: str) -> str:
    return f"{CONFIGS[config_id]['scope']}-{config_id}-g{TRIAL_GENERATION}"


def _trial_artifact_path(config_id: str) -> Path:
    return TRIALS_DIR / f"{_trial_id(config_id)}.json"


def _hyperparameters(inputs: Inputs, config_id: str) -> dict[str, Any]:
    cfg = CONFIGS[config_id]
    era = ERA_A if cfg["scope"] == SCOPE_A else ERA_B if cfg["scope"] == SCOPE_B else None
    return {
        "scope_id": cfg["scope"],
        "slot_id": SLOT_ID,
        "config_id": config_id,
        "family": cfg["family"],
        "is_base": cfg["base"],
        "semantics": cfg["semantics"],
        "spec": dict(cfg["spec"]),
        "direction_signal": ("pead_beat" if cfg["scope"] == SCOPE_A else "xsmom_top3")
        if cfg["scope"] in (SCOPE_A, SCOPE_B)
        else "pead_beat (debit-spread expression)",
        "lane": "spot proxy, close-trigger census semantics",
        "hold_sessions": HOLD_SESSIONS,
        "rt_primary_bp": 5,
        "rt_robust_bp": 10,
        "sealed_era": list(era) if era else "mirrors A on whatever bar span passes the data gate (never reached)",
        "cluster_convention": "signal session (A) / rebalance session (B)",
        "inner_loop": "EMPTY BY DESIGN (menu fold_mapping.inner); one scored run per cell on the sealed era",
        "multiplicity_m": M_ALTERNATIVES[cfg["scope"]],
        "bonferroni_alpha_one_sided": ALPHA,
        "economic_floor_paired_mean": ECON_FLOOR,
        "power_floor": (
            {"min_signals": POWER_FLOOR_A_SIGNALS}
            if cfg["scope"] == SCOPE_A
            else {"min_legs": POWER_FLOOR_B_LEGS, "min_rebalance_clusters": POWER_FLOOR_B_CLUSTERS}
            if cfg["scope"] == SCOPE_B
            else {"min_spread_episodes": POWER_FLOOR_C_EPISODES}
        ),
        "paired_base": None if cfg["base"] else ("a-hold20" if cfg["scope"] == SCOPE_A else "b-hold20"),
        "calendar_exclusions": [PHANTOM_ISO],
        "universe": {
            "panel_names": 37,
            "tradables": len(inputs.tradables),
            "chain35": len(inputs.chain35),
            "reporters": len(inputs.reporters),
        },
        "inputs_sha256": {
            "ohlc-panel.json": inputs.panel_sha256,
            "earnings-calendar.json": inputs.earnings_sha256,
            "nyse_sessions json": inputs.calendar_sha256,
            "VIX.csv": inputs.vix_sha256,
            "VIX3M.csv": inputs.vix3m_sha256,
        },
        "registration_menu_sha256": inputs.menu_sha256,
        "slot_doc_sha256": inputs.slot_doc_sha256,
        "protocol_raw_sha256": inputs.protocol_raw_sha256,
        "protocol_canonical_sha256": inputs.protocol_canonical_sha256,
        "dataset_manifest_hash": inputs.dataset_manifest_hash,
    }


def _config_hash(hyperparameters: Mapping[str, Any]) -> str:
    body = json.dumps(hyperparameters, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _scope(inputs: Inputs, scope_id: str) -> TrialScope:
    era = {"c09-eg2-a": ERA_A, "c09-eg2-b": ERA_B, "c09-eg2-c": ("mirrors-A", "data-gated")}[scope_id]
    return TrialScope(
        protocol_id="tree_options",
        protocol_hash=inputs.protocol_canonical_sha256,
        outer_fold_id=f"campaign-2026-09/{scope_id}/sealed-{era[0]}_{era[1]}",
        target_horizon="hold20",
        feature_set_id=(
            "ohlc-panel|card-lane|v1"
            if scope_id in (SCOPE_A, SCOPE_B)
            else "option-bars|g3-vwap|v1"
        ),
        model_family=MODEL_FAMILY,
    )


def _open_registry() -> TrialRegistry:
    CAMPAIGN_DIR.mkdir(parents=True, exist_ok=True)
    return TrialRegistry(REGISTRY_PATH)


def _registered_git_sha(trial_id: str) -> str:
    import sqlite3

    conn = sqlite3.connect(f"file:{REGISTRY_PATH}?mode=ro", uri=True)
    try:
        row = conn.execute("SELECT git_sha FROM trials WHERE trial_id = ?", (trial_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        raise Refused(f"{trial_id} is not registered -- INV-13 order violated")
    return str(row[0])


# Heads may move between --register and --execute: sibling executors commit
# their own slot's runner + round doc on this shared branch. A trial's
# identity is its CONTENT hashes (menu, protocol, slot doc, five pinned
# inputs, config_hash -- every one re-verified by load_and_bind on entry);
# execution therefore runs under the REGISTERED git provenance, but ONLY
# across commits whose diff is confined to other slots' runners and round
# docs. Anything else touching the registration surface refuses.
_PROVENANCE_ALLOWED_DIFF_PREFIXES = (
    "scripts/campaign/",  # sibling slot runners (never this file -- guarded below)
    "docs/campaign-2026-09/",  # sibling slot round docs
)
_PROVENANCE_FORBIDDEN_PATHS = (
    str(REPO_ROOT / "research_protocol.yaml"),
    str(REGISTRATION_PATH),
    str(REGISTRATION_SIDECAR),
    str(SLOT_DOC_PATH),
    str(Path(__file__).resolve()),
)


def _provenance_bridge(trial_id: str) -> tuple[str, str | None]:
    """(git_sha to run under, note). Equal heads need no bridge."""
    head = _git_head(REPO_ROOT)
    registered = _registered_git_sha(trial_id)
    if head == registered:
        return registered, None
    result = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "diff", "--name-only", registered, head],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise Refused(f"git diff {registered}..{head} failed: {result.stderr.strip()[:120]}")
    changed = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    for rel in changed:
        abs_path = str(REPO_ROOT / rel)
        if abs_path in _PROVENANCE_FORBIDDEN_PATHS:
            raise Refused(
                f"HEAD moved {registered[:8]} -> {head[:8]} and touches the registration"
                f" surface ({rel}) -- refusing to execute under the registered provenance"
            )
        if rel.startswith("data/") or rel.startswith("docs/theory/"):
            raise Refused(
                f"HEAD moved {registered[:8]} -> {head[:8]} and touches sealed inputs"
                f" ({rel}) -- refusing to execute"
            )
        if not rel.startswith(_PROVENANCE_ALLOWED_DIFF_PREFIXES):
            raise Refused(
                f"HEAD moved {registered[:8]} -> {head[:8]} with a non-sibling change"
                f" ({rel}) -- refusing to execute under the registered provenance"
            )
    note = (
        f"worktree HEAD moved {registered} -> {head} between --register and --execute"
        f" (sibling executor commits: {', '.join(changed)}); every content hash"
        " (menu, protocol, slot doc, five pinned inputs, config_hash) re-verified"
        " equal by load_and_bind; execution runs under the REGISTERED provenance"
    )
    return registered, note


def _stamp(
    inputs: Inputs,
    config_id: str,
    *,
    registered_git_sha: str | None = None,
    provenance_note: str | None = None,
) -> dict[str, Any]:
    cfg = CONFIGS[config_id]
    out = {
        "program": "campaign-2026-09",
        "slot_id": SLOT_ID,
        "config_id": config_id,
        "trial_id": _trial_id(config_id),
        "trial_generation": TRIAL_GENERATION,
        "scope_id": cfg["scope"],
        "is_base": cfg["base"],
        "registration_menu_sha256": inputs.menu_sha256,
        "protocol_raw_sha256": inputs.protocol_raw_sha256,
        "protocol_canonical_sha256": inputs.protocol_canonical_sha256,
        "dataset_manifest_hash": inputs.dataset_manifest_hash,
        "multiplicity_m": M_ALTERNATIVES[cfg["scope"]],
        "critical_t_one_sided_bonferroni": _critical_t(M_ALTERNATIVES[cfg["scope"]]),
        "git_sha": registered_git_sha or _git_head(REPO_ROOT),
        "executed_git_sha": _git_head(REPO_ROOT),
        "runner_sha256": _sha256_file(Path(__file__).resolve()),
        "generated_at": _utcnow().isoformat(),
    }
    if provenance_note:
        out["provenance_note"] = provenance_note
    return out


# ---- phases ---------------------------------------------------------------------------


def phase_plan() -> int:
    inputs = load_and_bind()
    _bind_calendar_for_simulator(inputs.calendar)
    print(f"menu sha256 {inputs.menu_sha256} (sidecar-verified; slot doc {inputs.slot_doc_sha256[:16]}...)")
    print(f"protocol raw {inputs.protocol_raw_sha256[:16]}... canonical {inputs.protocol_canonical_sha256[:16]}...")
    print(f"tnull v3: CALIBRATED (family scoring unfrozen)")
    print(f"sealed_eg2a (pead_beat, beats-only, hold-complete): {ERA_A[0]}..{ERA_A[1]}")
    print(f"sealed_eg2b (xsmom_top3 monthly cards): {ERA_B[0]}..{ERA_B[1]} -> 23 rebalances / 69 legs (cross-checked at execute)")
    print(f"scope C: DATA-GATED (capture in flight + post-M0 short-leg machinery absent)")
    for scope_id in (SCOPE_A, SCOPE_B, SCOPE_C):
        ids = [c for c in MENU_CONFIG_IDS if CONFIGS[c]["scope"] == scope_id]
        print(f"scope {scope_id}: {len(ids)} configs, m={M_ALTERNATIVES[scope_id]},"
              f" crit_t={_critical_t(M_ALTERNATIVES[scope_id]):.4f}")
    print(f"registry db: {REGISTRY_PATH}")
    print(f"artifacts dir: {SLOT_DIR}")
    return 0


def phase_selftest() -> int:
    """Synthetic-bar checks of the simulator (no real data, no outcomes)."""
    cal = SealedCalendar(
        [
            "2026-03-02", "2026-03-03", "2026-03-04", "2026-03-05", "2026-03-06",
            "2026-03-09", "2026-03-10", "2026-03-11", "2026-03-12", "2026-03-13",
            "2026-03-16", "2026-03-17", "2026-03-18", "2026-03-19", "2026-03-20",
            "2026-03-23", "2026-03-24", "2026-03-25", "2026-03-26", "2026-03-27",
            "2026-03-30", "2026-03-31", "2026-04-01",
        ]
    )
    _bind_calendar_for_simulator(cal)
    ctx = ContextMaps(
        vixterm={"2026-03-05": True, "2026-03-06": True},
        vixterm_missing=(),
        breadth={"2026-03-09": 0.39, "2026-03-10": 0.41},
        mom20={("X", "2026-03-11"): -0.01, ("X", "2026-03-12"): 0.02},
        month_last={
            "2026-03-13": True, "2026-03-31": True, "2026-03-30": False,
            "2026-03-16": False, "2026-03-02": False,
        },
    )
    D = Decimal
    path = tuple(
        (i, iso, c)
        for i, (iso, c) in enumerate(
            [
                ("2026-03-03", D("101")), ("2026-03-04", D("102")), ("2026-03-05", D("103")),
                ("2026-03-06", D("104")), ("2026-03-09", D("120")), ("2026-03-10", D("118")),
                ("2026-03-11", D("107")), ("2026-03-12", D("106")), ("2026-03-13", D("105")),
                ("2026-03-16", D("104")), ("2026-03-17", D("103")), ("2026-03-18", D("102")),
                ("2026-03-19", D("101")), ("2026-03-20", D("100")), ("2026-03-23", D("99")),
                ("2026-03-24", D("98")), ("2026-03-25", D("97")), ("2026-03-26", D("96")),
                ("2026-03-27", D("95")), ("2026-03-30", D("94")),
            ],
            start=1,
        )
    )
    cases = [
        # (spec, entry, name, want_i, want_reason); path closes by i:
        # 1:101 2:102 3:103 4:104 5:120 6:118 7:107 8:106 9:105 10:104
        # 11:103 12:102 13:101 14:100 15:99 16:98 17:97 18:96 19:95 20:94
        ({"kind": "hold"}, D("100"), "X", 20, "time-stop"),
        # adverse -5% armed 5: first close <= 95 is i=19 (close 95; inclusive)
        ({"kind": "adverse", "level": "0.05", "arm": 5}, D("100"), "X", 19, "adverse"),
        # adverse -5% armed 10: same trigger, arm does not push past it
        ({"kind": "adverse", "level": "0.05", "arm": 10}, D("100"), "X", 19, "adverse"),
        # adverse -10% armed 5: first close <= 90 is i=20 (close 94? no: 94 > 90) -> time-stop
        ({"kind": "adverse", "level": "0.10", "arm": 5}, D("100"), "X", 20, "time-stop"),
        # adverse un-armed (b-adv10, first close <= 90): none -> time-stop
        ({"kind": "adverse", "level": "0.10", "arm": 1}, D("100"), "X", 20, "time-stop"),
        # trail 10% armed 5: running max of prior closes hits 120 at i=5 -> level 108;
        # i=6 close 118 > 108; i=7 close 107 <= 108 -> 7
        ({"kind": "trail", "level": "0.10", "arm": 5}, D("100"), "X", 7, "trail"),
        # trail 15% armed 5: 120*0.85=102 -> i=12 close 102 <= 102 -> 12
        ({"kind": "trail", "level": "0.15", "arm": 5}, D("100"), "X", 12, "trail"),
        # trail 20% armed 5: 120*0.8=96 -> i=18 close 96 -> 18
        ({"kind": "trail", "level": "0.20", "arm": 5}, D("100"), "X", 18, "trail"),
        # vixterm: prior of session 2026-03-06 (i=4) is 2026-03-05 (True) -> 4
        ({"kind": "vixterm", "arm": 1}, D("100"), "X", 4, "vixterm"),
        # breadth: prior 2026-03-09 breadth 0.39 < 0.40 -> session 2026-03-10 = i=6
        ({"kind": "breadth", "arm": 1}, D("100"), "X", 6, "breadth"),
        # mom20flip: prior 2026-03-11 mom20=-0.01 <= 0 -> session 2026-03-12 = i=8
        ({"kind": "mom20flip", "arm": 1}, D("100"), "X", 8, "mom20flip"),
        # monthend with a month-last session INSIDE the path: 2026-03-13 (i=9)
        ({"kind": "monthend"}, D("100"), "X", 9, "month-end"),
    ]
    failures = 0
    for spec, entry, name, want_i, want_reason in cases:
        fill = simulate_exit(spec, entry, path, ctx, name)
        ok = fill.i == want_i and fill.reason == want_reason
        print(f"{'ok ' if ok else 'FAIL'} {json.dumps(spec, sort_keys=True):58s} -> i={fill.i} ({fill.reason})")
        failures += 0 if ok else 1
    # paired-stats conventions on a tiny synthetic set
    naive, clustered = _t_stats([0.01, 0.02, 0.03, 0.04], ["a", "a", "b", "b"])
    ok = not (math.isnan(naive) or math.isnan(clustered)) and naive > 0 and clustered > 0
    print(f"{'ok ' if ok else 'FAIL'} _t_stats conventions -> naive={naive:.3f} clustered={clustered:.3f}")
    failures += 0 if ok else 1
    zero_sd = _t_stats([0.01, 0.01, 0.01, 0.01], ["a", "a", "b", "b"])
    ok = zero_sd[0] == float("inf")
    print(f"{'ok ' if ok else 'FAIL'} zero-sd drift -> naive={zero_sd[0]}")
    failures += 0 if ok else 1
    print(f"selftest: {len(cases) + 3} checks, {failures} failures")
    return 1 if failures else 0


def phase_register() -> int:
    inputs = load_and_bind()
    registry = _open_registry()
    try:
        existing = [c for c in MENU_CONFIG_IDS if registry.is_registered(_trial_id(c))]
        if existing:
            raise Refused(
                f"registration is one-shot: {[ _trial_id(c) for c in existing ]} already registered"
            )
        for config_id in MENU_CONFIG_IDS:
            hyper = _hyperparameters(inputs, config_id)
            scope = _scope(inputs, CONFIGS[config_id]["scope"])
            record = TrialRecord(
                trial_id=_trial_id(config_id),
                created_at=_utcnow(),
                hypothesis=inputs.slot["hypothesis"],
                git_sha=_git_head(REPO_ROOT),
                config_hash=_config_hash(hyper),
                dataset_manifest_hash=inputs.dataset_manifest_hash,
                train_window=None,  # inner loop EMPTY BY DESIGN
                validation_window=None,  # nothing selected on data
                test_window=(
                    date.fromisoformat(ERA_A[0]), date.fromisoformat(ERA_A[1])
                ) if CONFIGS[config_id]["scope"] == SCOPE_A else (
                    date.fromisoformat(ERA_B[0]), date.fromisoformat(ERA_B[1])
                ) if CONFIGS[config_id]["scope"] == SCOPE_B else None,
                hyperparameters=hyper,
                scope_key=scope.scope_key(),
            )
            registry.register(record, scope)
            print(f"registered {_trial_id(config_id)} base={CONFIGS[config_id]['base']}")
    finally:
        registry.close()
    print(
        f"registry: {REGISTRY_PATH} (scope trials 10/11/6, cap=32 each);"
        " NO outcome has been computed or viewed"
    )
    return 0


def _scope_c_gate_evidence() -> dict[str, Any]:
    """The two menu gates, with the evidence that each is unmet today."""
    capture_sh = MAIN_ROOT / "scripts" / "desk_longdated_capture.sh"
    return {
        "gates": [
            {
                "gate": "(i) the long-dated option-bar capture lands and passes a"
                        " coverage/integrity check on the evaluation span",
                "status": "UNMET",
                "evidence": [
                    "menu data_gates / rules.data_gates_campaign: capture IN FLIGHT, ETA ~2026-09-27",
                    f"capture script present (in flight, not landed): {capture_sh.exists()}",
                    "on-disk option-bar history (desk-store/iv-history/vwap_atm.json) spans"
                    " 2024-08-26..2026-09-03 (508 sessions) -- short of the EG2-C evaluation"
                    " span, which mirrors A (calendar era through 2026-08-28 with hold-20"
                    " completes needing bars through ~2026-09-25)",
                ],
            },
            {
                "gate": "(ii) the post-M0 short-leg machinery exists (debit-spread short"
                        " legs require expiration/early-assignment/dividend/"
                        "exercise-by-exception logic, tested)",
                "status": "UNMET",
                "evidence": [
                    "research_protocol.yaml short_options: policy prohibited; short legs in"
                    " debit spreads allowed only after that machinery exists and is tested"
                    " (post-M0)",
                    "REGISTRATION-NOTES.md section 4 open question 3 records the machinery"
                    " as not built",
                ],
            },
        ],
        "menu_rule": "otherwise DATA-GATED-NOT-RUN forever and the slot's value collapses to A+B, disclosed",
        "multiplicity_note": "m=5 stays charged at the full declared scope count regardless of the run subset",
    }


def phase_withdraw_c() -> int:
    inputs = load_and_bind()
    if WITHDRAWAL_PATH.exists():
        raise Refused(f"{WITHDRAWAL_PATH} already exists -- the withdrawal stamp is one-shot")
    registry = _open_registry()
    try:
        for config_id in MENU_CONFIG_IDS:
            if CONFIGS[config_id]["scope"] != SCOPE_C:
                continue
            if not registry.is_registered(_trial_id(config_id)):
                raise Refused(f"{_trial_id(config_id)} is not registered -- INV-13 order violated")
            if registry.has_outcome(_trial_id(config_id)):
                raise Refused(f"{_trial_id(config_id)} already carries an outcome -- not a withdrawal")
    finally:
        registry.close()
    SLOT_DIR.mkdir(parents=True, exist_ok=True)
    TRIALS_DIR.mkdir(parents=True, exist_ok=True)
    evidence = _scope_c_gate_evidence()
    for config_id in MENU_CONFIG_IDS:
        if CONFIGS[config_id]["scope"] != SCOPE_C:
            continue
        body = {
            "stamp": _stamp(inputs, config_id),
            "verdict": "DATA-GATED-NOT-RUN",
            "config_id": config_id,
            "scope_id": SCOPE_C,
            "semantics": CONFIGS[config_id]["semantics"],
            "spec": dict(CONFIGS[config_id]["spec"]),
            "gates": evidence["gates"],
            "note": "withdrawn exactly as the menu marks it; no proxy improvised;"
                    " the trial row stays REGISTERED with no outcome (never run)",
        }
        _trial_artifact_path(config_id).write_text(
            json.dumps(body, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(f"withdrew {_trial_id(config_id)} (DATA-GATED-NOT-RUN)")
    withdrawal = {
        "stamp": {
            **_stamp(inputs, "c-ts20"),
            "trial_id": None,
            "config_id": None,
            "withdrawn_config_ids": [
                _trial_id(c) for c in MENU_CONFIG_IDS if CONFIGS[c]["scope"] == SCOPE_C
            ],
        },
        "verdict": "DATA-GATED-NOT-RUN",
        **evidence,
        "declared_use": inputs.slot["declared_use"],
    }
    WITHDRAWAL_PATH.write_text(
        json.dumps(withdrawal, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"scope C withdrawal stamped at {WITHDRAWAL_PATH}; scope value collapses to A+B, disclosed")
    return 0


def phase_execute() -> int:
    inputs = load_and_bind()
    _bind_calendar_for_simulator(inputs.calendar)
    SLOT_DIR.mkdir(parents=True, exist_ok=True)
    TRIALS_DIR.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise Refused("another exit-grid-2 execution holds the lock -- one run at a time") from None
        registry = _open_registry()
        try:
            ctx_maps = build_context_maps(inputs)
            stream_a = build_pead_stream(inputs)
            stream_b = build_xsmom_stream(inputs)
            runs: dict[str, dict[str, Any]] = {}
            provenance_notes: dict[str, str | None] = {}

            def execute_cell(config_id: str, stream: StreamBuild) -> None:
                trial_id = _trial_id(config_id)
                artifact = _trial_artifact_path(config_id)
                if artifact.exists():
                    raise Refused(f"{artifact} already exists -- executions are one-shot per trial")
                status = registry.status(trial_id)
                if status != "REGISTERED":
                    raise Refused(f"{trial_id} is {status}, not REGISTERED -- refusing to re-run")
                hyper = _hyperparameters(inputs, config_id)
                config_hash = _config_hash(hyper)
                git_sha, provenance_note = _provenance_bridge(trial_id)
                provenance_notes[config_id] = provenance_note
                registry.mark_running(
                    trial_id,
                    git_sha=git_sha,
                    config_hash=config_hash,
                    dataset_manifest_hash=inputs.dataset_manifest_hash,
                    at=_utcnow(),
                )
                runs[config_id] = run_config(inputs, ctx_maps, stream, config_id)
                n_stream = stream.disclosure.get("n_signals_hold_complete")
                if n_stream is None:
                    n_stream = stream.disclosure.get("n_legs")
                print(
                    f"{trial_id}: simulated n={len(runs[config_id]['trades'])}"
                    f" (stream signals={n_stream})"
                )

            # bases first (they are the paired reference), then alternatives
            for config_id in MENU_CONFIG_IDS:
                if CONFIGS[config_id]["scope"] == SCOPE_A:
                    execute_cell(config_id, stream_a)
            for config_id in MENU_CONFIG_IDS:
                if CONFIGS[config_id]["scope"] == SCOPE_B:
                    execute_cell(config_id, stream_b)

            # stamp artifacts: stats paired against the scope's base run
            for config_id, run in runs.items():
                trial_id = _trial_id(config_id)
                cfg = CONFIGS[config_id]
                base_id = "a-hold20" if cfg["scope"] == SCOPE_A else "b-hold20"
                base_run = None if cfg["base"] else runs[base_id]
                stats = cell_statistics(base_run, run)
                stream = stream_a if cfg["scope"] == SCOPE_A else stream_b
                body = {
                    "stamp": _stamp(
                        inputs,
                        config_id,
                        registered_git_sha=_registered_git_sha(trial_id),
                        provenance_note=provenance_notes.get(config_id),
                    ),
                    "payload": {
                        "config_id": config_id,
                        "scope_id": cfg["scope"],
                        "semantics": cfg["semantics"],
                        "spec": dict(cfg["spec"]),
                        "stream_disclosure": stream.disclosure,
                        "context_feature_conventions": {
                            "vixterm": "PRIOR session VIX close >= VIX3M close (16:15 print; INV-02)",
                            "breadth": "PRIOR session fraction of the 36 tradables with close > prior close (INV-10)",
                            "mom20": "PRIOR session name close/close[-20]-1 on the name's own bars",
                            "vixterm_missing_era_sessions": len(ctx_maps.vixterm_missing),
                        },
                        "stats": stats,
                        "trades": run["trades"],
                    },
                }
                artifact = _trial_artifact_path(config_id)
                artifact.write_text(
                    json.dumps(body, indent=2, sort_keys=True) + "\n", encoding="utf-8"
                )
                registry.complete(
                    _trial_id(config_id), metrics_uri=str(artifact), outcome_at=_utcnow()
                )
                paired = stats["paired"]
                if paired is not None:
                    print(
                        f"{trial_id}: COMPLETED artifact={artifact}"
                        f" paired_mean={paired['paired_mean']:+.6f}"
                        f" t_cons={paired['t_conservative']:+.3f}"
                    )
                else:
                    lvl = stats["level"]
                    print(
                        f"{trial_id}: COMPLETED artifact={artifact}"
                        f" BASE level net5={lvl['net5_mean']:+.6f} n={lvl['n']}"
                    )
        finally:
            registry.close()
    finally:
        os.close(lock_fd)
    return 0


def _read_artifact(inputs: Inputs, config_id: str) -> Mapping[str, Any]:
    trial_id = _trial_id(config_id)
    artifact = _trial_artifact_path(config_id)
    if not artifact.exists():
        raise Refused(f"{artifact} is missing -- only EXECUTED artifacts are evaluation evidence")
    body = json.loads(artifact.read_text(encoding="utf-8"))
    stamp = body.get("stamp", {})
    if stamp.get("trial_id") != trial_id:
        raise Refused(
            f"{artifact} carries trial_id {stamp.get('trial_id')!r}, expected {trial_id!r}"
            " -- only the EXECUTED artifact is evaluation evidence"
        )
    if stamp.get("registration_menu_sha256") != inputs.menu_sha256:
        raise Refused(f"{artifact} was executed against a different menu hash")
    if stamp.get("dataset_manifest_hash") != inputs.dataset_manifest_hash:
        raise Refused(f"{artifact} was executed against different inputs")
    return body


def _cell_verdict(scope_id: str, stats: Mapping[str, Any]) -> dict[str, Any]:
    """The menu's per-cell criteria on one alternative's paired stats."""
    paired = stats["paired"]
    if paired is None:
        raise Refused("per-cell verdict arithmetic requires paired stats")
    m = M_ALTERNATIVES[scope_id]
    crit = _critical_t(m)
    mean = paired["paired_mean"]
    t_cons = paired["t_conservative"]
    n = paired["n"]
    n_clusters = paired["n_clusters"]
    if scope_id == SCOPE_A:
        power_ok = n >= POWER_FLOOR_A_SIGNALS
        power_reason = f"n {n} >= {POWER_FLOOR_A_SIGNALS} sealed beats"
    else:
        power_ok = n >= POWER_FLOOR_B_LEGS and n_clusters >= POWER_FLOOR_B_CLUSTERS
        power_reason = (
            f"n {n} >= {POWER_FLOOR_B_LEGS} legs and clusters {n_clusters}"
            f" >= {POWER_FLOOR_B_CLUSTERS}"
        )
    row: dict[str, Any] = {
        "power_ok": power_ok,
        "power_reason": power_reason,
        "paired_mean": mean,
        "paired_mean_bp": mean * 1e4 if mean is not None else None,
        "t_conservative": t_cons,
        "critical_t": crit,
        "criteria": {
            "paired_mean_gt_0": (mean is not None and mean > 0),
            "t_cons_ge_crit": (t_cons is not None and not math.isnan(t_cons) and t_cons >= crit),
            "paired_mean_ge_5bp": (mean is not None and mean >= ECON_FLOOR),
        },
    }
    if not power_ok:
        row["verdict"] = "UNDERPOWERED"
        return row
    ok = all(row["criteria"].values())
    row["verdict"] = "PASS" if ok else "FAIL"
    return row


def phase_evaluate() -> int:
    inputs = load_and_bind()
    if VERDICTS_PATH.exists():
        raise Refused(f"{VERDICTS_PATH} already exists -- the round-1 verdict stamp is one-shot")
    if not WITHDRAWAL_PATH.exists():
        raise Refused(f"{WITHDRAWAL_PATH} is missing -- withdraw scope C before evaluating")
    registry = _open_registry()
    try:
        for config_id in MENU_CONFIG_IDS:
            trial_id = _trial_id(config_id)
            if not registry.is_registered(trial_id):
                raise Refused(f"{trial_id} is not registered -- INV-13 order violated")
            if CONFIGS[config_id]["scope"] == SCOPE_C:
                if registry.has_outcome(trial_id):
                    raise Refused(f"{trial_id} carries an outcome -- scope C must stay NOT-RUN")
                continue
            if registry.status(trial_id) != "COMPLETED":
                raise Refused(f"{trial_id} is {registry.status(trial_id)}, not COMPLETED")
    finally:
        registry.close()

    verdicts: dict[str, Any] = {"cells": {}, "scopes": {}}
    for scope_id, base_id in ((SCOPE_A, "a-hold20"), (SCOPE_B, "b-hold20")):
        ids = [c for c in MENU_CONFIG_IDS if CONFIGS[c]["scope"] == scope_id]
        base_body = _read_artifact(inputs, base_id)
        scope_cells: dict[str, Any] = {}
        any_pass = False
        for config_id in ids:
            body = _read_artifact(inputs, config_id)
            stats = body["payload"]["stats"]
            if CONFIGS[config_id]["base"]:
                scope_cells[config_id] = {
                    "verdict": None,
                    "verdict_note": "BASE reference cell (census precedent: pass column '-'):"
                    " the paired comparator, excluded from per-cell verdict arithmetic;"
                    " level statistics stamped in its artifact",
                    "level": stats["level"],
                }
                continue
            row = _cell_verdict(scope_id, stats)
            row["t_stats"] = stats["paired"]
            row["level"] = stats["level"]
            scope_cells[config_id] = row
            if row["verdict"] == "PASS":
                any_pass = True
        verdicts["cells"][scope_id] = scope_cells
        verdicts["scopes"][scope_id] = (
            "EXIT-SUCCESSOR-NOMINATED" if any_pass else "HOLD-STANDS"
        )
    c_bodies = {}
    for config_id in MENU_CONFIG_IDS:
        if CONFIGS[config_id]["scope"] != SCOPE_C:
            continue
        body = json.loads(_trial_artifact_path(config_id).read_text(encoding="utf-8"))
        if body.get("verdict") != "DATA-GATED-NOT-RUN":
            raise Refused(f"{_trial_artifact_path(config_id)} is not a withdrawal stamp")
        c_bodies[config_id] = {"verdict": "DATA-GATED-NOT-RUN", "semantics": body["semantics"]}
    verdicts["cells"][SCOPE_C] = c_bodies
    verdicts["scopes"][SCOPE_C] = "DATA-GATED-NOT-RUN"

    withdrawal = json.loads(WITHDRAWAL_PATH.read_text(encoding="utf-8"))
    out = {
        "stamp": {
            **_stamp(inputs, "a-hold20"),
            "trial_id": None,
            "config_id": None,
            "round": 1,
            "menu_hypothesis": inputs.slot["hypothesis"],
            "acceptance_criteria": inputs.slot["acceptance_criteria"],
            "verdict_vocabulary": inputs.slot["verdict_vocabulary"],
            "fold_mapping_note": inputs.slot["fold_mapping"]["inner"],
            "honesty_statement": inputs.slot["fold_mapping"]["honesty_statement"],
            "critical_t": {s: _critical_t(M_ALTERNATIVES[s]) for s in M_ALTERNATIVES},
        },
        "verdicts": verdicts,
        "scope_c_withdrawal": withdrawal,
        "posture": {
            "adoption": inputs.menu["rules"]["adoption"],
            "nothing_adopts": True,
            "dsr": "any survivor still faces the ledger's base-rate/holdout deflations and a"
                   " DSR report at N = 24 declared alternatives (DESK-CHECKS convention);"
                   " the RESEARCH-LEDGER is owned by the consolidator, not this executor",
            "multiplicity": "charged at the full declared scope count (m=9/10/5) regardless of run subset",
        },
        "notes": [
            "INNER loop EMPTY BY DESIGN (menu fold_mapping): nothing tuned or selected on data,",
            "so no inner-fold best exists; each registered cell's single scored run on its sealed",
            "era IS the registered experiment (frozen-grid house precedent). The 'best cell' below",
            "is a DESCRIPTIVE ranking of executed alternatives by paired mean, never a selection.",
            "Paired diffs are net@5bp; the round trip cancels (one RT per signal under every variant).",
        ],
    }
    SLOT_DIR.mkdir(parents=True, exist_ok=True)
    VERDICTS_PATH.write_text(json.dumps(out, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"verdicts stamped at {VERDICTS_PATH}")
    for scope_id in (SCOPE_A, SCOPE_B, SCOPE_C):
        print(f"scope {scope_id}: {verdicts['scopes'][scope_id]}")
        for config_id, row in verdicts["cells"][scope_id].items():
            if row.get("verdict") is None:
                print(f"  {config_id:14s} BASE (reference)")
            elif scope_id == SCOPE_C:
                print(f"  {config_id:14s} {row['verdict']}")
            else:
                print(
                    f"  {config_id:14s} {row['verdict']:12s}"
                    f" paired_mean={row['paired_mean_bp']:+.2f}bp"
                    f" t_cons={row['t_conservative']:+.2f} crit={row['critical_t']:.3f}"
                )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--plan", action="store_true", help="read-only: bind and print the geometry")
    parser.add_argument("--selftest", action="store_true", help="synthetic-bar simulator checks (no outcomes)")
    parser.add_argument(
        "--register",
        action="store_true",
        help="INV-13: write the 27 trial rows (REGISTERED, no outcome) to the slot registry",
    )
    parser.add_argument(
        "--withdraw-c",
        action="store_true",
        help="record scope C's DATA-GATED-NOT-RUN withdrawal with gate evidence (no run, no proxy)",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="run the 21 A/B cells one-shot (REGISTERED -> RUNNING -> COMPLETED + artifact)",
    )
    parser.add_argument(
        "--evaluate",
        action="store_true",
        help="apply the menu criteria to the EXECUTED artifacts and stamp round-1 verdicts",
    )
    args = parser.parse_args(argv)
    try:
        if args.plan:
            return phase_plan()
        if args.selftest:
            return phase_selftest()
        if args.register:
            return phase_register()
        if args.withdraw_c:
            return phase_withdraw_c()
        if args.execute:
            return phase_execute()
        if args.evaluate:
            return phase_evaluate()
    except Refused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
