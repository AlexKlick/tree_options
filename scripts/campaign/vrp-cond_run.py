#!/usr/bin/env python3
"""campaign-2026-09 VRP-COND runner (scopes ``c09-vrp-e`` / ``c09-vrp-o``, menu order 1).

Round 1 = INNER FOLDS ONLY. Executes exactly the 24 configs the sealed menu
(``docs/theory/campaign-2026-09-registration.json`` @ menu v3, sha256 sidecar
beside it) registers for slot ``vrp-cond``:

* scope E (``c09-vrp-e``, 20 configs) -- equity timing/size gates on the two
  ALLOWED directions, scored on the pooled inner folds V1+V2 with the INV-06
  purge at the desk hold length (entry ordinal <= 417, so every 20-session
  hold + the 5-session embargo ends before the sealed window at ordinal 443);
* scope O (``c09-vrp-o``, 4 configs) -- options-expression arm, DATA_GATED on
  the in-flight long-dated capture (>= 126 sessions of history) AND an
  IVHIST-002-successor fidelity verdict; neither gate is met, so the four
  configs are registered and WITHDRAWN without running (no proxy is
  improvised).

THE SEALED WINDOW (ordinals 443..505 = 2026-06-02..2026-08-31) IS NEVER
READ HERE: no outcome, no gate value, no percentile pool slot and no hold
return at ordinal >= 443 is computed or viewed by this runner. Round 1
scores the tuning region only.

Feature (registration section 3, byte-faithful):

* ``iv30_i(t)`` -- ``artifacts/desk-store/iv-history/vwap_atm.json``
  (schema ``desk-ivhist/1``; 35 names x 508 sessions 2024-08-26..2026-09-03;
  13,610 evaluable name-sessions; the menu pins the repo copy's sha256).
* ``harvol20_i(t) = sqrt(F_HAR,i,h20(t) * 365 / c(t, t+20))`` -- the h=20
  pooled log-HAR forecast from ``tree_options.desk.har`` (monthly expanding
  refits from the first session of 2024-09, strictly point in time; origins
  exist from 2024-09-03), annualized on the calendar days to the session 20
  NYSE sessions ahead -- exactly the denominator of ``surface.vrp()``, so
  ``s_i(t) = iv30 - harvol20`` is the desk's displayed ``vrp_har``.
* ``r_i(t) = iv30_i(t) / harvol20_i(t)``; percentile ``p_i(t)`` = fraction of
  STRICTLY-LOWER evaluable r values over the trailing 252 sessions ENDING AT
  t (the window includes t; evaluable sessions only), requiring n >= 126 else
  NOT_EVALUABLE. (Convention disclosure: the registration says "rank of
  r_i(t) among r_i over the trailing 252 sessions"; the window ending at t is
  the literal reading -- fraction-below, window-inclusive.)
* Schedule withholding (registration risk 5, ``surface.forecast_block``
  semantics): a REPORTER name whose forward report schedule does not pin the
  event count through t+20 (``har.schedule_status`` in
  incomplete/unavailable) has its condition WITHHELD at t (NOT_EVALUABLE);
  ETF names (incl. IWM) are never withheld. The trailing percentile POOL is
  not thinned by schedule status -- only the condition at the decision
  instant is (the desk displays ``vrp_har`` on degraded sessions and
  withholds the SELECTED ``vrp``).
* ``pbar(t)`` -- mean of ``p_i(t)`` over the rebalance's top-3 picks with
  evaluable p (TQQQ/SQQQ picks carry no IV history by construction); a
  rebalance with none evaluable is NOT_EVALUABLE and every gated xe config
  ABSTAINS on it (counted, never silently ON).
* PEAD conditions read p at t-1 (the last session before the entry session).

Directions are BYTE-IDENTICAL to ``tree_options.desk.signals``: XSMOM-TOP3
via ``signals.xsmom_top3`` (close(t)/close(t-273)-1, no-skip, top 3, >= 30
ranked) and PEAD beats via ``signals.pead_beats`` (first post-report session,
move >= +1.5%). Evaluation basis: the census spot-proxy convention --
signal-close entry, close-to-close hold 20 sessions, Decimal closes, house
hold filter (the name must carry every session in (t, t+20]; beyond-panel or
hole holds are dropped and counted), 5bp RT primary / 15bp robustness,
day-clustered t over per-entry-day means, 2025-01-09 excluded as a
non-session (sealed calendar file byte-identical).

Selection metric (registration section 6, the ONLY number that picks a
promoted config in round 1): the pooled V1+V2 conditioned-minus-base
per-trade net delta at 5bp RT -- the SQUEEZE ``conditional`` column (strategy
mean minus the same-region unconditional base mean of the unchanged
direction rule). For size configs the strategy mean is the size-weighted
per-trade mean ``sum(w*net)/sum(w)`` (w in {1, 0.5}); for xe-defer the
strategy trades are the deferred entries. The ON-minus-OFF matched-sessions
column (the decisive SEALED criterion 1) is stamped beside it,
descriptively, never for selection. Placebos (xe-lag21 / xp-lag21 /
xp-shuffle) run through the identical path and are ranked but are CONTROLS,
not promotion candidates: registration criterion 3 compares the promoted
config's sealed delta against the same-family placebo deltas, so a placebo
cannot be the nominee it is compared against (reading disclosed in the
selection stamp).

INV-13: ``--register`` writes all 24 trial rows (REGISTERED, no outcome) to
the slot registry sqlite BEFORE any outcome exists; ``--execute`` is
one-shot per trial (REGISTERED -> RUNNING -> COMPLETED with the artifact as
metrics_uri); ``--select`` reads ONLY the executed artifacts and stamps the
round-1 selection. Every phase refuses on any mismatch with the sealed menu
(hashes, ids, geometry, floors) and refuses to touch the sealed window.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import random
import statistics
import subprocess
import sys
import time
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

from tree_options.desk import har as har_mod  # noqa: E402
from tree_options.desk import signals as signals_mod  # noqa: E402
from tree_options.desk.panel import read_panel_with_sha256  # noqa: E402
from tree_options.registry.scope import TrialScope  # noqa: E402
from tree_options.registry.sqlite import TrialRegistry  # noqa: E402
from tree_options.schemas.trial import TrialRecord  # noqa: E402

# Data lives in the MAIN checkout; the runner + registration live in the
# execution worktree. Both are pinned by sha256 against the menu below.
MAIN_ROOT = Path("/home/alexk/documents/tree_options")
REGISTRATION_PATH = REPO_ROOT / "docs" / "theory" / "campaign-2026-09-registration.json"
REGISTRATION_SIDECAR = Path(str(REGISTRATION_PATH) + ".sha256")
PROTOCOL_PATH = REPO_ROOT / "research_protocol.yaml"
PANEL_PATH = MAIN_ROOT / "artifacts" / "paper-trades" / "ohlc-panel.json"
EARNINGS_PATH = MAIN_ROOT / "artifacts" / "paper-trades" / "earnings-calendar.json"
CALENDAR_PATH = MAIN_ROOT / "data" / "calendar" / "nyse_sessions_2018_01_02_2026_12_31.json"
IVHIST_PATH = MAIN_ROOT / "artifacts" / "desk-store" / "iv-history" / "vwap_atm.json"
SLOT_DOC_PATH = REPO_ROOT / "docs" / "theory" / "campaign-2026-09" / "slots" / "vrp-cond.md"
IVHIST_VERDICT_PATH = MAIN_ROOT / "docs" / "desk" / "IVHIST-001-verdict.json"
CALIBRATION_V3_PATH = MAIN_ROOT / "artifacts" / "campaign-2026-09" / "tnull" / "calibration-v3.json"

CAMPAIGN_DIR = MAIN_ROOT / "artifacts" / "campaign-2026-09"
REGISTRY_PATH = CAMPAIGN_DIR / "vrp-cond.db"
SLOT_DIR = CAMPAIGN_DIR / "vrp-cond"
TRIALS_DIR = SLOT_DIR / "trials"
SELECTION_PATH = SLOT_DIR / "round1-selection.json"
SCOPE_O_RECORD_PATH = SLOT_DIR / "scope-O-withdrawal.json"
LOCK_PATH = SLOT_DIR / "execute.lock"

SCOPE_E = "c09-vrp-e"
SCOPE_O = "c09-vrp-o"
SLOT_ID = "vrp-cond"
MODEL_FAMILY_E = "vrp-cond/gate-v1"
MODEL_FAMILY_O = "vrp-cond/option-expression-v1"
ROUND = 1

# ---- pinned geometry (menu fold_mapping + slot doc section 4) -------------------------
PHANTOM_ISO = "2025-01-09"  # ledger ruling 2026-09-23: not a session
IV_START = "2024-08-26"
IV_END = "2026-09-03"
IV_SESSIONS = 508
ORD_V1 = (253, 378)  # 2025-08-28..2026-02-27 (126)
ORD_V2 = (316, 441)  # 2025-11-26..2026-05-29 (roll 63)
ORD_SHOULDER = 442  # 2026-06-01 (unused; not tuned, not scored)
ORD_SEALED_START = 443  # 2026-06-02
ORD_SEALED_END = 505  # 2026-08-31
ORD_TUNE_MAX_ENTRY = 417  # 2026-04-24; 417+20+5 = 442 < 443 (INV-06 at desk hold)
HAR_START = date(2024, 9, 3)  # FORECAST-001: monthly refits from 2024-09's first session
HOLD_SESSIONS = 20
RT_PRIMARY = 0.0005
RT_ROBUST = 0.0015
PCT_WINDOW = 252
PCT_MIN_N = 126
TER_LO = 1.0 / 3.0
TER_HI = 2.0 / 3.0
LAG_SESSIONS = 21
SHUFFLE_SEED_BYTES = b"vrp-cond-xp-shuffle-1"
FLOORS = {"xe": 6, "xp": 8}  # xe: n_ON entries; xp: n_ON trades (tuning floors same)
XSMOM_EXPECTED_REBALANCES = (
    "2025-09-02",
    "2025-10-01",
    "2025-11-03",
    "2025-12-01",
    "2026-01-02",
    "2026-02-02",
    "2026-03-02",
    "2026-04-01",
)
CONFIG_IDS_E = (
    # the menu's own order: 9 xe gates, 8 xp gates, then the 3 placebos
    "xe-book-lo",
    "xe-book-hi",
    "xe-book-mid",
    "xe-mkt-lo",
    "xe-mkt-hi",
    "xe-playbook",
    "xe-size-mkt",
    "xe-size-book",
    "xe-defer",
    "xp-name-lo",
    "xp-name-hi",
    "xp-name-mid",
    "xp-mkt-lo",
    "xp-mkt-hi",
    "xp-playbook",
    "xp-size-name",
    "xp-size-mkt",
    "xe-lag21",
    "xp-lag21",
    "xp-shuffle",
)
CONFIG_IDS_O = ("ox-cheap", "ox-rich-fallback", "op-cheap", "op-rich-fallback")
PLACEBOS = frozenset({"xe-lag21", "xp-lag21", "xp-shuffle"})

EXPECTED_ORDINAL_DATES = {
    252: "2025-08-27",
    253: "2025-08-28",
    316: "2025-11-26",
    378: "2026-02-27",
    417: "2026-04-24",
    441: "2026-05-29",
    442: "2026-06-01",
    443: "2026-06-02",
    505: "2026-08-31",
    508: "2026-09-03",
}

# The registered gate text per config (menu section 5 table), carried into
# every trial row so the executed rule is self-describing.
GATE_RULES: dict[str, str] = {
    "xe-book-lo": "XSMOM entry: fire the month's top-3 only if pbar(t) <= 1/3",
    "xe-book-hi": "XSMOM entry: fire only if pbar(t) >= 2/3",
    "xe-book-mid": "XSMOM entry: fire only if 1/3 < pbar(t) < 2/3 (symmetry control)",
    "xe-mkt-lo": "XSMOM entry: fire only if p_IWM(t) <= 1/3",
    "xe-mkt-hi": "XSMOM entry: fire only if p_IWM(t) >= 2/3",
    "xe-playbook": "XSMOM entry: fire only if r_IWM(t) < 1.0 (IWM raw threshold; alignment-only, bias-flagged)",
    "xe-size-mkt": "XSMOM size: always fire; half size when p_IWM(t) >= 2/3",
    "xe-size-book": "XSMOM size: always fire; half size when pbar(t) >= 2/3",
    "xe-defer": "XSMOM timing: when p_IWM(t) >= 2/3 at rebalance, defer entry one session (names unchanged)",
    "xe-lag21": "placebo: xe-book-lo's rule with pbar lagged 21 sessions",
    "xp-name-lo": "PEAD entry: fire the beat only if p_i(t-1) <= 1/3",
    "xp-name-hi": "PEAD entry: fire only if p_i(t-1) >= 2/3",
    "xp-name-mid": "PEAD entry: fire only if 1/3 < p_i(t-1) < 2/3 (control)",
    "xp-mkt-lo": "PEAD entry: fire only if p_IWM(t-1) <= 1/3",
    "xp-mkt-hi": "PEAD entry: fire only if p_IWM(t-1) >= 2/3",
    "xp-playbook": "PEAD entry: fire only if r_IWM(t-1) < 1.0",
    "xp-size-name": "PEAD size: always fire; half size when p_i(t-1) >= 2/3",
    "xp-size-mkt": "PEAD size: always fire; half size when p_IWM(t-1) >= 2/3",
    "xp-lag21": "placebo: xp-name-lo's rule with p_i lagged 21 sessions",
    "xp-shuffle": "placebo: xp-name-lo's rule with p_i permuted across reporter-events by sha256(vrp-cond-xp-shuffle-1)",
    "ox-cheap": "XSMOM vehicle: top-3 pick with p_i(t) <= 1/3: long call expression (DATA_GATED)",
    "ox-rich-fallback": "XSMOM vehicle: top-3 pick with p_i(t) >= 2/3: spot only (DATA_GATED)",
    "op-cheap": "PEAD vehicle: beat with p_i(t-1) <= 1/3: long call expression (DATA_GATED)",
    "op-rich-fallback": "PEAD vehicle: beat with p_i(t-1) >= 2/3: spot only (DATA_GATED)",
}


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


class Refused(RuntimeError):
    """A binding refusal: the sealed menu and the execution disagree."""


# ---- sealed calendar (tnull idiom: the file stays byte-identical) ----------------------


class SealedCalendar:
    """The sealed NYSE calendar minus the 2025-01-09 phantom session."""

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

    def nth_before(self, d: date, n: int) -> date:
        i = self.ordinal(d) - n
        if not 0 <= i < len(self._sessions):
            raise Refused(f"{n} sessions before {d.isoformat()} is outside the calendar")
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


# ---- inputs and binding ---------------------------------------------------------------


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
    ivhist: Mapping[str, Any]
    ivhist_sha256: str
    iv_labels: Mapping[str, str]
    chain35: tuple[str, ...]
    tradables: tuple[str, ...]
    reporters: tuple[str, ...]
    window_sessions: tuple[date, ...]  # the 508 IV-window sessions
    dataset_manifest_hash: str

    def window_ordinal(self, d: date) -> int:
        """1-based ordinal inside the 508-session IV window."""
        try:
            return self.calendar.ordinal(d) - self.calendar.ordinal(self.window_sessions[0]) + 1
        except Refused:
            raise Refused(f"{d.isoformat()} is not on the IV window grid") from None


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
    if menu.get("version") != 3:
        raise Refused(f"menu version {menu.get('version')!r} is not the v3 amendment")
    slot = next((s for s in menu["slots"] if s.get("slot_id") == SLOT_ID), None)
    if slot is None or slot.get("order") != 1 or slot.get("family") != "VRP-COND":
        raise Refused("the menu's order-1 VRP-COND slot is missing or malformed")
    if tuple(slot["config_ids"]) != CONFIG_IDS_E + CONFIG_IDS_O:
        raise Refused("menu config ids are not the sealed 24 (20 scope E + 4 scope O) in order")
    scopes = {sc["scope_id"]: sc for sc in slot["scope_ids"]}
    if scopes.get(SCOPE_E, {}).get("config_count") != 20 or scopes.get(SCOPE_O, {}).get(
        "config_count"
    ) != 4:
        raise Refused("menu scope counts are not 20 (c09-vrp-e) + 4 (c09-vrp-o)")
    if tuple(slot["fold_mapping"][k] for k in ("inner_v1", "inner_v2")) != (
        "253..378 = 2025-08-28..2026-02-27 (126 = validation default)",
        "316..441 = 2025-11-26..2026-05-29 (roll 63; overlaps V1 by 63 sessions;"
        " FIXED at assembly - see rules.fixes_applied_at_assembly)",
    ):
        raise Refused("menu fold_mapping inner folds are not the sealed V1/V2 rows")
    if not slot["fold_mapping"]["sealed"].startswith("443..505 = 2026-06-02..2026-08-31"):
        raise Refused("menu fold_mapping sealed row is not the sealed 443..505 window")
    if "ordinal <= 417" not in slot["fold_mapping"]["purge"] and "ordinal<=417" not in slot[
        "fold_mapping"
    ]["purge"]:
        raise Refused("menu fold_mapping purge row does not carry the ordinal<=417 restriction")

    # protocol: raw bytes must equal the menu pin; canonical hash re-stamped (INV-14)
    from tree_options.protocol.loader import default_protocol, protocol_hash

    protocol_raw_sha256 = _sha256_file(PROTOCOL_PATH)
    if menu["protocol_hash"] != protocol_raw_sha256:
        raise Refused(
            "research_protocol.yaml raw sha256 does not match the menu's protocol_hash"
            " -- a protocol change requires a NEW registration"
        )
    protocol_canonical_sha256 = protocol_hash(default_protocol())

    # every pinned input the runner consumes
    pinning = menu["dataset_pinning"]
    for label, path in (
        ("docs/theory/campaign-2026-09/slots/vrp-cond.md", SLOT_DOC_PATH),
        ("artifacts/desk-store/iv-history/vwap_atm.json", IVHIST_PATH),
        ("artifacts/paper-trades/earnings-calendar.json", EARNINGS_PATH),
        ("data/calendar/nyse_sessions_2018_01_02_2026_12_31.json", CALENDAR_PATH),
    ):
        got = _sha256_file(path)
        want = pinning.get(label)
        if want is None or want != got:
            raise Refused(f"{label}: sha256 {got} != the menu's pinned {want} -- swapped inputs refuse")

    # the panel is read under the writers' shared flock; sha256 of the exact bytes
    panel, panel_sha256 = read_panel_with_sha256(PANEL_PATH)
    want_panel = pinning.get("artifacts/paper-trades/ohlc-panel.json")
    if want_panel != panel_sha256:
        raise Refused(f"ohlc-panel.json: sha256 {panel_sha256} != the menu's pinned {want_panel}")

    earnings = json.loads(EARNINGS_PATH.read_text(encoding="utf-8"))
    calendar_json = json.loads(CALENDAR_PATH.read_text(encoding="utf-8"))
    calendar = SealedCalendar(calendar_json["sessions"])
    if calendar.removed != (PHANTOM_ISO,):
        raise Refused(f"the phantom session was not removed: {calendar.removed}")

    # sequencing: family scoring is unfrozen only by a CALIBRATED v3 stamp of THIS menu
    if not CALIBRATION_V3_PATH.exists():
        raise Refused(f"{CALIBRATION_V3_PATH} is missing -- the T-NULL v3 stamp gates this slot")
    v3 = json.loads(CALIBRATION_V3_PATH.read_text(encoding="utf-8"))
    if v3.get("stamp", {}).get("registration_menu_sha256") != menu_sha256:
        raise Refused("the tnull calibration-v3 stamp does not bind this menu sha")
    if v3.get("verdict", {}).get("slot") != "CALIBRATED":
        raise Refused(
            f"tnull calibration-v3 verdict is {v3.get('verdict', {}).get('slot')!r},"
            " not CALIBRATED -- family scoring stays frozen"
        )

    # the IV history: window, names, and the fidelity labels
    ivhist = json.loads(IVHIST_PATH.read_text(encoding="utf-8"))
    if ivhist.get("schema") != "desk-ivhist/1":
        raise Refused(f"iv-history schema {ivhist.get('schema')!r} is not desk-ivhist/1")
    if list(ivhist.get("window", [])) != [IV_START, IV_END]:
        raise Refused(f"iv-history window {ivhist.get('window')} is not [{IV_START}, {IV_END}]")
    iv_names = tuple(sorted(ivhist["names"]))
    if len(iv_names) != 35:
        raise Refused(f"iv-history carries {len(iv_names)} names, expected 35")
    iv_labels = json.loads(IVHIST_VERDICT_PATH.read_text(encoding="utf-8"))["labels"]
    if iv_labels.get("IWM") != "ok":
        raise Refused("IWM is not the 'ok' IV name -- the fidelity split moved")

    # universes, pinned to the panel and cross-checked against the desk config
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
    # PLTR/SPCX joined desk-universe.toml 2026-09-23 but are NOT in the ohlc
    # panel: the registration pins this slot to the 35 full-history chain
    # names (FORECAST-001's set) -- widening is a successor registration.
    if set(CHAIN_UNIVERSE) - set(chain35) - {"PLTR", "SPCX"}:
        raise Refused(
            f"the desk chain universe grew beyond the registered 35+PLTR/SPCX:"
            f" {sorted(set(CHAIN_UNIVERSE) - set(chain35))}"
        )
    if set(chain35) != set(iv_names):
        raise Refused("the 35 chain names are not exactly the IV history's 35 names")
    reporters = tuple(sorted(n for n, reps in earnings.items() if reps))
    if len(reporters) != 26 or not set(reporters) <= set(chain35):
        raise Refused(
            f"earnings-calendar reporters are not the 26 chain-universe names: {len(reporters)}"
        )

    # the 508-session IV window on the sealed calendar, ordinals verified
    sessions = calendar.sessions()
    lo = sessions.index(date.fromisoformat(IV_START))
    window = sessions[lo : lo + IV_SESSIONS]
    if len(window) != IV_SESSIONS or window[-1] != date.fromisoformat(IV_END):
        raise Refused(
            f"the IV window is not {IV_SESSIONS} sessions ending {IV_END}:"
            f" {len(window)} ending {window[-1] if window else None}"
        )
    for ordinal, iso in EXPECTED_ORDINAL_DATES.items():
        if window[ordinal - 1].isoformat() != iso:
            raise Refused(
                f"window ordinal {ordinal} is {window[ordinal - 1]}, the registration says {iso}"
            )
    window_iso = {d.isoformat() for d in window}
    for name in iv_names:
        keys = set(ivhist["names"][name]["sessions"])
        if not keys <= window_iso:
            extra = sorted(keys - window_iso)[:3]
            raise Refused(f"iv-history name {name} carries sessions off the sealed window: {extra}")

    manifest_body = "".join(
        f"{label}\0{pinning[label]}\n"
        for label in (
            "artifacts/paper-trades/ohlc-panel.json",
            "artifacts/paper-trades/earnings-calendar.json",
            "data/calendar/nyse_sessions_2018_01_02_2026_12_31.json",
            "artifacts/desk-store/iv-history/vwap_atm.json",
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
        earnings_sha256=pinning["artifacts/paper-trades/earnings-calendar.json"],
        calendar=calendar,
        calendar_sha256=pinning["data/calendar/nyse_sessions_2018_01_02_2026_12_31.json"],
        ivhist=ivhist,
        ivhist_sha256=pinning["artifacts/desk-store/iv-history/vwap_atm.json"],
        iv_labels=iv_labels,
        chain35=chain35,
        tradables=tradables,
        reporters=reporters,
        window_sessions=tuple(window),
        dataset_manifest_hash=dataset_manifest_hash,
    )


# ---- the feature layer ----------------------------------------------------------------


@dataclass(frozen=True)
class Features:
    """r_i(t) per name over the IV window + cached percentile evaluations."""

    r: dict[str, dict[date, float]]  # name -> session -> iv30 / harvol20
    n_r_union: int
    har_first_origin: date
    har_last_origin: date
    pct_cache: dict[tuple[str, date], tuple[float | None, str]]
    sched_cache: dict[tuple[str, date], tuple[bool, str]]


def build_features(inputs: Inputs) -> Features:
    """r_i(t) = iv30_i(t) / harvol20_i(t), strictly point in time."""
    cal = inputs.calendar
    data = har_mod.build_har_data(inputs.panel, inputs.earnings, cal, list(inputs.chain35))
    wf = har_mod.walk_forward(data, HOLD_SESSIONS, start=HAR_START)
    origins = sorted({t for per_name in wf.forecasts.values() for t in per_name})
    if not origins:
        raise Refused("the HAR walk-forward produced no forecasts -- machinery defect")
    if data.sessions[origins[0]] != HAR_START:
        raise Refused(
            f"first HAR origin is {data.sessions[origins[0]]}, expected {HAR_START}"
        )
    r: dict[str, dict[date, float]] = {}
    n_r = 0
    for name in inputs.chain35:
        iv_sessions = inputs.ivhist["names"][name]["sessions"]
        per: dict[date, float] = {}
        for t, f in sorted(wf.forecasts.get(name, {}).items()):
            session = data.sessions[t]
            iso = session.isoformat()
            rec = iv_sessions.get(iso)
            if not isinstance(rec, dict) or "iv30" not in rec:
                continue  # no evaluable iv30 on this session
            days = har_mod.calendar_days_between(
                iso, cal.nth_after(session, HOLD_SESSIONS).isoformat()
            )
            if days <= 0:
                continue
            harvol = math.sqrt(f * 365.0 / days)
            if harvol > 0.0:
                per[session] = rec["iv30"] / harvol
        r[name] = per
        n_r += len(per)
    return Features(
        r=r,
        n_r_union=n_r,
        har_first_origin=data.sessions[origins[0]],
        har_last_origin=data.sessions[origins[-1]],
        pct_cache={},
        sched_cache={},
    )


def schedule_withheld(inputs: Inputs, feats: Features, name: str, session: date) -> tuple[bool, str]:
    """(withheld, why): a reporter whose forward schedule does not pin the
    event count through session+20 has the condition withheld at that session
    (registration risk 5, surface.forecast_block semantics)."""
    key = (name, session)
    if key in feats.sched_cache:
        return feats.sched_cache[key]
    if name in har_mod.ETF_NAMES or not inputs.earnings.get(name):
        out = (False, "n/a (etf)")
    else:
        end = inputs.calendar.nth_after(session, HOLD_SESSIONS)
        status, reason = har_mod.schedule_status(
            inputs.earnings.get(name, ()), session, end, inputs.calendar
        )
        out = (status in ("incomplete", "unavailable"), f"{status}: {reason}")
    feats.sched_cache[key] = out
    return out


def percentile(inputs: Inputs, feats: Features, name: str, session: date) -> tuple[float | None, str]:
    """p_i(session): fraction of strictly-lower evaluable r over the trailing
    252 sessions ending AT session (window-inclusive), n >= 126 required; the
    condition is withheld for a degraded reporter schedule at session."""
    key = (name, session)
    if key in feats.pct_cache:
        return feats.pct_cache[key]
    series = feats.r.get(name, {})
    cur = series.get(session)
    if cur is None:
        out = (None, "no evaluable r (iv30 or HAR h20 missing)")
    else:
        withheld, why = schedule_withheld(inputs, feats, name, session)
        if withheld:
            out = (None, f"schedule withheld: {why}")
        else:
            cal = inputs.calendar
            i = cal.ordinal(session)
            pool_sessions = cal.sessions()[max(0, i - (PCT_WINDOW - 1)) : i + 1]
            pool = [series[d] for d in pool_sessions if d in series]
            n = len(pool)
            if n < PCT_MIN_N:
                out = (None, f"percentile n={n} < {PCT_MIN_N}")
            else:
                out = (sum(1 for v in pool if v < cur) / n, f"n={n}")
    feats.pct_cache[key] = out
    return out


def raw_ratio(
    inputs: Inputs, feats: Features, name: str, session: date
) -> tuple[float | None, str]:
    """The raw IV/HAR ratio (absolute threshold, IWM-only per the fidelity split)."""
    cur = feats.r.get(name, {}).get(session)
    if cur is None:
        return None, "no evaluable r"
    withheld, why = schedule_withheld(inputs, feats, name, session)
    if withheld:
        return None, f"schedule withheld: {why}"
    return cur, "ok"


# ---- the base (unconditioned) streams --------------------------------------------------


@dataclass(frozen=True)
class Trade:
    entry: str
    exit_session: str | None
    name: str
    gross: float | None
    weight: float
    detail: str


def _complete_trade(
    inputs: Inputs, name: str, entry: date, weight: float, detail: str
) -> Trade:
    """House path: Decimal closes, hold 20 close-to-close, every session of
    (t, t+20] present in the name's bars, else dropped-and-counted."""
    cal, panel = inputs.calendar, inputs.panel
    bars = panel.get(name) or {}
    entry_iso = entry.isoformat()
    if entry_iso not in bars:
        return Trade(entry_iso, None, name, None, weight, detail + ";no-entry-bar")
    sessions = cal.sessions()
    i = cal.ordinal(entry)
    window = [s.isoformat() for s in sessions[i + 1 : i + HOLD_SESSIONS + 1]]
    if len(window) != HOLD_SESSIONS or any(w not in bars for w in window):
        return Trade(entry_iso, None, name, None, weight, detail + ";hold-incomplete")
    exit_iso = window[-1]
    gross = float(
        Decimal(str(bars[exit_iso]["close"])) / Decimal(str(bars[entry_iso]["close"])) - 1
    )
    return Trade(entry_iso, exit_iso, name, gross, weight, detail)


def _tuning_sessions(inputs: Inputs) -> list[date]:
    """Entry sessions allowed in round 1: IV-window ordinals 253..417 (the
    pooled V1+V2 union under the INV-06 restriction at the desk hold length).
    NEVER a session at window ordinal >= 443 (the sealed window)."""
    return [
        inputs.window_sessions[o - 1]
        for o in range(ORD_V1[0], ORD_TUNE_MAX_ENTRY + 1)
    ]


def xsmom_base(inputs: Inputs) -> list[dict[str, Any]]:
    """The unconditioned XSMOM-TOP3 book over the tuning rebalances, via the
    desk's own signals module (byte-identical direction rule)."""
    rows: list[dict[str, Any]] = []
    rebalances: list[str] = []
    for session in _tuning_sessions(inputs):
        if not inputs.calendar.is_first_session_of_month(session):
            continue
        res = signals_mod.xsmom_top3(inputs.panel, session, inputs.calendar)
        rebalances.append(session.isoformat())
        if not res.fires:
            raise Refused(
                f"xsmom did not fire on rebalance {session} (n_ranked={res.n_ranked})"
                " -- machinery defect"
            )
        for name in res.top3:
            rows.append(
                {
                    "family": "xe",
                    "name": name,
                    "session": session,
                    "detail": f"xsmom-top3:{session.isoformat()}",
                }
            )
    if tuple(rebalances) != XSMOM_EXPECTED_REBALANCES:
        raise Refused(
            f"tuning rebalances {rebalances} are not the registration's 8"
            f" {XSMOM_EXPECTED_REBALANCES}"
        )
    if len(rows) != len(XSMOM_EXPECTED_REBALANCES) * signals_mod.XSMOM_TOPK:
        raise Refused(f"xsmom base carries {len(rows)} entries, expected 24")
    return rows


def pead_base(inputs: Inputs) -> list[dict[str, Any]]:
    """Every PEAD beat entering in the tuning region, via the desk's own
    signals module (byte-identical direction rule)."""
    rows: list[dict[str, Any]] = []
    for session in _tuning_sessions(inputs):
        res = signals_mod.pead_beats(inputs.panel, inputs.earnings, session, inputs.calendar)
        for ev in res.beats:
            rows.append(
                {
                    "family": "xp",
                    "name": ev.name,
                    "session": session,
                    "report_date": ev.report_date,
                    "move": float(ev.move) if ev.move is not None else None,
                    "detail": f"pead-beat:{ev.report_date}",
                }
            )
    if not rows:
        raise Refused("the tuning region carries zero PEAD beats -- machinery defect")
    return rows


# ---- statistics (house conventions) ----------------------------------------------------


def _cell_stats(entries: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Per-trade stats over trade rows ({entry, gross, weight, ...})."""
    complete = [e for e in entries if e["gross"] is not None]
    out: dict[str, Any] = {
        "n_entries": len(entries),
        "n_complete": len(complete),
        "n_dropped_hold": len(entries) - len(complete),
    }
    if not complete:
        out.update(
            {
                "days": 0,
                "gross_clustered_t": None,
                "per_trade_mean_sd_day_clustered": None,
                "net_per_trade_mean": None,
                "net15_per_trade_mean": None,
            }
        )
        return out
    by_day: dict[str, list[float]] = {}
    for e in complete:
        by_day.setdefault(e["entry"], []).append(e["gross"])
    day_means = [statistics.fmean(v) for _d, v in sorted(by_day.items())]
    m = statistics.fmean(day_means)
    sd = statistics.stdev(day_means) if len(day_means) > 1 else None
    if sd is not None and sd > 0:
        t_stat = m / (sd / math.sqrt(len(day_means)))
        cse = sd / math.sqrt(len(day_means))
    elif sd == 0:
        t_stat = math.inf if m else math.nan
        cse = 0.0
    else:
        t_stat, cse = None, None
    out.update(
        {
            "days": len(day_means),
            "gross_clustered_t": t_stat,
            "per_trade_mean_sd_day_clustered": cse,
            "net_per_trade_mean": statistics.fmean(e["gross"] - RT_PRIMARY for e in complete),
            "net15_per_trade_mean": statistics.fmean(e["gross"] - RT_ROBUST for e in complete),
        }
    )
    return out


def _weighted_net_mean(complete: Sequence[Mapping[str, Any]], rt: float) -> float | None:
    """sum(w * net) / sum(w) -- the per-unit-size net mean (size configs)."""
    if not complete:
        return None
    wsum = sum(e["weight"] for e in complete)
    if wsum <= 0:
        return None
    return sum(e["weight"] * (e["gross"] - rt) for e in complete) / wsum


# ---- gate application -----------------------------------------------------------------


@dataclass(frozen=True)
class Decision:
    fire: bool
    weight: float
    entry_shift: int  # sessions to shift the entry (xe-defer: 0 or +1)
    condition: str
    value: float | None
    note: str


def _pbar(
    inputs: Inputs, feats: Features, picks: Sequence[str], session: date, lag: int = 0
) -> tuple[float | None, str]:
    """Mean of p_i over the picks with evaluable p, lag sessions back
    (lag > 0: the PLACEBO's stale feature; the picks stay the month's book)."""
    at = session if lag == 0 else inputs.calendar.nth_before(session, lag)
    vals: list[float] = []
    missing: list[str] = []
    for name in picks:
        p, why = percentile(inputs, feats, name, at)
        if p is None:
            missing.append(f"{name}[{why}]")
        else:
            vals.append(p)
    if not vals:
        return None, "no evaluable pick p: " + ";".join(missing)
    return statistics.fmean(vals), f"picks_eval={len(vals)} lag={lag} missing={missing}"


def xe_decisions(
    inputs: Inputs,
    feats: Features,
    base: Sequence[Mapping[str, Any]],
    config_id: str,
) -> list[Decision]:
    cal = inputs.calendar
    picks_of: dict[date, list[str]] = {}
    for row in base:
        picks_of.setdefault(row["session"], []).append(row["name"])
    # book/market conditions per rebalance (computed once, applied to the 3 entries)
    cond_of: dict[date, dict[str, tuple[float | None, str]]] = {}
    for session in sorted(picks_of):
        pbar, why_bar = _pbar(inputs, feats, picks_of[session], session)
        p_iwm, why_iwm = percentile(inputs, feats, "IWM", session)
        r_iwm, why_r = raw_ratio(inputs, feats, "IWM", session)
        cond_of[session] = {
            "pbar": (pbar, why_bar),
            "p_IWM": (p_iwm, why_iwm),
            "r_IWM": (r_iwm, "ok" if r_iwm is not None else why_r),
        }
    out: list[Decision] = []
    for row in base:
        session: date = row["session"]
        c = cond_of[session]
        pbar, why_bar = c["pbar"]
        p_iwm, why_iwm = c["p_IWM"]
        r_iwm, why_r = c["r_IWM"]
        if config_id == "xe-book-lo":
            out.append(Decision(pbar is not None and pbar <= TER_LO, 1.0, 0, "pbar", pbar, why_bar))
        elif config_id == "xe-book-hi":
            out.append(Decision(pbar is not None and pbar >= TER_HI, 1.0, 0, "pbar", pbar, why_bar))
        elif config_id == "xe-book-mid":
            out.append(
                Decision(pbar is not None and TER_LO < pbar < TER_HI, 1.0, 0, "pbar", pbar, why_bar)
            )
        elif config_id == "xe-mkt-lo":
            out.append(Decision(p_iwm is not None and p_iwm <= TER_LO, 1.0, 0, "p_IWM", p_iwm, why_iwm))
        elif config_id == "xe-mkt-hi":
            out.append(Decision(p_iwm is not None and p_iwm >= TER_HI, 1.0, 0, "p_IWM", p_iwm, why_iwm))
        elif config_id == "xe-playbook":
            out.append(Decision(r_iwm is not None and r_iwm < 1.0, 1.0, 0, "r_IWM", r_iwm, why_r))
        elif config_id == "xe-size-mkt":
            half = p_iwm is not None and p_iwm >= TER_HI
            out.append(Decision(True, 0.5 if half else 1.0, 0, "p_IWM", p_iwm, why_iwm))
        elif config_id == "xe-size-book":
            half = pbar is not None and pbar >= TER_HI
            out.append(Decision(True, 0.5 if half else 1.0, 0, "pbar", pbar, why_bar))
        elif config_id == "xe-defer":
            defer = p_iwm is not None and p_iwm >= TER_HI
            out.append(Decision(True, 1.0, 1 if defer else 0, "p_IWM", p_iwm, why_iwm))
        elif config_id == "xe-lag21":
            lag_pbar, why = _pbar(inputs, feats, picks_of[session], session, lag=LAG_SESSIONS)
            out.append(
                Decision(
                    lag_pbar is not None and lag_pbar <= TER_LO, 1.0, 0, "pbar_lag21", lag_pbar, why
                )
            )
        else:
            raise Refused(f"{config_id}: not a scope-E xe config")
    return out


def xp_decisions(
    inputs: Inputs,
    feats: Features,
    base: Sequence[Mapping[str, Any]],
    config_id: str,
) -> list[Decision]:
    cal = inputs.calendar
    shuffled: dict[int, tuple[float | None, str]] = {}
    if config_id == "xp-shuffle":
        # Deterministic permutation of the region's own condition values across
        # its event list (seed fixed by the registration): every event's value
        # is re-assigned within the region, marginals preserved, so a value
        # landing on a NOT_EVALUABLE slot abstains exactly as the real rule does.
        vals: list[tuple[float | None, str]] = []
        for row in base:
            prev = cal.previous_session(row["session"])
            p, why = percentile(inputs, feats, row["name"], prev)
            vals.append((p, why))
        perm = list(range(len(vals)))
        random.Random(
            int.from_bytes(hashlib.sha256(SHUFFLE_SEED_BYTES).digest(), "big")
        ).shuffle(perm)
        shuffled = {perm[i]: vals[i] for i in range(len(vals))}
    out: list[Decision] = []
    for idx, row in enumerate(base):
        session: date = row["session"]
        prev = cal.previous_session(session)
        if config_id in ("xp-name-lo", "xp-name-hi", "xp-name-mid"):
            p, why = percentile(inputs, feats, row["name"], prev)
            if config_id == "xp-name-lo":
                fire = p is not None and p <= TER_LO
            elif config_id == "xp-name-hi":
                fire = p is not None and p >= TER_HI
            else:
                fire = p is not None and TER_LO < p < TER_HI
            out.append(Decision(fire, 1.0, 0, f"p_{row['name']}(t-1)", p, why))
        elif config_id in ("xp-mkt-lo", "xp-mkt-hi"):
            p, why = percentile(inputs, feats, "IWM", prev)
            fire = p is not None and (p <= TER_LO if config_id == "xp-mkt-lo" else p >= TER_HI)
            out.append(Decision(fire, 1.0, 0, "p_IWM(t-1)", p, why))
        elif config_id == "xp-playbook":
            r_iwm, why = raw_ratio(inputs, feats, "IWM", prev)
            out.append(Decision(r_iwm is not None and r_iwm < 1.0, 1.0, 0, "r_IWM(t-1)", r_iwm, why))
        elif config_id == "xp-size-name":
            p, why = percentile(inputs, feats, row["name"], prev)
            half = p is not None and p >= TER_HI
            out.append(Decision(True, 0.5 if half else 1.0, 0, f"p_{row['name']}(t-1)", p, why))
        elif config_id == "xp-size-mkt":
            p, why = percentile(inputs, feats, "IWM", prev)
            half = p is not None and p >= TER_HI
            out.append(Decision(True, 0.5 if half else 1.0, 0, "p_IWM(t-1)", p, why))
        elif config_id == "xp-lag21":
            at = cal.nth_before(prev, LAG_SESSIONS)
            p, why = percentile(inputs, feats, row["name"], at)
            out.append(
                Decision(p is not None and p <= TER_LO, 1.0, 0, f"p_{row['name']}(t-1-21)", p, why)
            )
        elif config_id == "xp-shuffle":
            p, why = shuffled[idx]
            out.append(Decision(p is not None and p <= TER_LO, 1.0, 0, "p_shuffled", p, why))
        else:
            raise Refused(f"{config_id}: not a scope-E xp config")
    return out


# ---- stamps / registry ----------------------------------------------------------------


def _stamp(inputs: Inputs, config_id: str, scope_id: str) -> dict[str, Any]:
    return {
        "program": "campaign-2026-09",
        "slot_id": SLOT_ID,
        "round": ROUND,
        "config_id": config_id,
        "scope_id": scope_id,
        "registration_menu_sha256": inputs.menu_sha256,
        "protocol_raw_sha256": inputs.protocol_raw_sha256,
        "protocol_canonical_sha256": inputs.protocol_canonical_sha256,
        "dataset_manifest_hash": inputs.dataset_manifest_hash,
        "inputs_sha256": {
            "ohlc-panel.json": inputs.panel_sha256,
            "earnings-calendar.json": inputs.earnings_sha256,
            "nyse_sessions json": inputs.calendar_sha256,
            "iv-history/vwap_atm.json": inputs.ivhist_sha256,
        },
        "tnull_calibration_v3_verdict": "CALIBRATED",
        "iv_fidelity_labels": dict(inputs.iv_labels),
        "git_sha": _git_head(REPO_ROOT),
        "runner_sha256": _sha256_file(Path(__file__).resolve()),
        "generated_at": _utcnow().isoformat(),
    }


def _trial_id(config_id: str) -> str:
    scope = SCOPE_E if config_id in CONFIG_IDS_E else SCOPE_O
    return f"{scope}-{config_id}-r{ROUND}"


def _artifact_path(config_id: str) -> Path:
    return TRIALS_DIR / f"{_trial_id(config_id)}.json"


def _scope(inputs: Inputs, config_id: str) -> TrialScope:
    scope = SCOPE_E if config_id in CONFIG_IDS_E else SCOPE_O
    return TrialScope(
        protocol_id="tree_options",
        protocol_hash=inputs.protocol_canonical_sha256,
        outer_fold_id=(
            f"campaign-2026-09/{scope}/tuning-pooled-v1v2-ord253_417"
            if scope == SCOPE_E
            else f"campaign-2026-09/{scope}/no-evaluatable-window"
        ),
        target_horizon="hold20",
        feature_set_id="ohlc-panel|iv-vwap-atm-30d|har-h20-pooled|v1",
        model_family=MODEL_FAMILY_E if scope == SCOPE_E else MODEL_FAMILY_O,
    )


def _hyperparameters(inputs: Inputs, config_id: str) -> dict[str, Any]:
    scope = SCOPE_E if config_id in CONFIG_IDS_E else SCOPE_O
    family = "xe" if config_id.startswith("xe-") else "xp" if config_id.startswith("xp-") else "o"
    return {
        "scope_id": scope,
        "config_id": config_id,
        "slot_id": SLOT_ID,
        "round": ROUND,
        "family": family,
        "placebo": config_id in PLACEBOS,
        "gate_rule": GATE_RULES[config_id],
        "model_family": MODEL_FAMILY_E if scope == SCOPE_E else MODEL_FAMILY_O,
        "lane": "card-lane (equity close-to-close)" if scope == SCOPE_E else "options expression (data-gated)",
        "hold_sessions": HOLD_SESSIONS,
        "rt_primary_bp": 5,
        "rt_robust_bp": 15,
        "feature": {
            "iv30": "desk-store/iv-history/vwap_atm.json (desk-ivhist/1)",
            "har": "pooled log-HAR h=20, monthly expanding refits from 2024-09-03 (tree_options.desk.har)",
            "harvol20": "sqrt(F * 365 / c(t, t+20 calendar days))",
            "r": "iv30 / harvol20",
            "percentile": "fraction strictly below over the trailing 252 sessions ending at t (window-inclusive), n >= 126",
            "pbar": "mean of p_i over the rebalance top-3 picks with evaluable p; none evaluable -> abstain",
            "pead_condition_lag": "p read at t-1",
            "schedule_withholding": "reporter conditions withheld when har.schedule_status(t, t+20) is incomplete/unavailable",
        },
        "folds": {
            "window_sessions": IV_SESSIONS,
            "inner_v1_ordinals": list(ORD_V1),
            "inner_v2_ordinals": list(ORD_V2),
            "tuning_entry_ordinals": [ORD_V1[0], ORD_TUNE_MAX_ENTRY],
            "inv06": "entry ordinal <= 417 so entry+20+5 < 443 (purge at the desk hold length)",
            "sealed_window_untouched": [ORD_SEALED_START, ORD_SEALED_END],
        },
        "power_floors_tuning": {"n_on_entries_xe": FLOORS["xe"], "n_on_trades_xp": FLOORS["xp"]},
        "selection_metric": "pooled conditioned-minus-base per-trade net delta (5bp RT), SQUEEZE conditional column",
        "inputs_sha256": {
            "ohlc-panel.json": inputs.panel_sha256,
            "earnings-calendar.json": inputs.earnings_sha256,
            "nyse_sessions json": inputs.calendar_sha256,
            "iv-history/vwap_atm.json": inputs.ivhist_sha256,
        },
        "registration_menu_sha256": inputs.menu_sha256,
        "dataset_manifest_hash": inputs.dataset_manifest_hash,
    }


def _config_hash(hyperparameters: Mapping[str, Any]) -> str:
    body = json.dumps(hyperparameters, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _open_registry() -> TrialRegistry:
    CAMPAIGN_DIR.mkdir(parents=True, exist_ok=True)
    return TrialRegistry(REGISTRY_PATH)


# ---- phases ---------------------------------------------------------------------------


def phase_plan() -> int:
    """Read-only bind + geometry + feature coverage. No returns, no gates."""
    t0 = time.monotonic()
    inputs = load_and_bind()
    feats = build_features(inputs)
    print(f"menu sha256 {inputs.menu_sha256} (sidecar-verified)")
    print(f"protocol raw {inputs.protocol_raw_sha256[:16]}... canonical {inputs.protocol_canonical_sha256[:16]}...")
    print(f"tnull calibration-v3: CALIBRATED (family scoring unfrozen)")
    print(
        f"IV window: {IV_SESSIONS} sessions {IV_START}..{IV_END}; "
        f"tuning entries = window ordinals {ORD_V1[0]}..{ORD_TUNE_MAX_ENTRY}"
    )
    print(f"sealed window ordinals {ORD_SEALED_START}..{ORD_SEALED_END} -- NOT touched")
    print(f"HAR h20 origins: {feats.har_first_origin}..{feats.har_last_origin}")
    print(f"evaluable r name-sessions (union): {feats.n_r_union}")
    # percentile coverage over the tuning decision instants (no returns viewed)
    n_eval = 0
    n_ne = 0
    withheld = 0
    for name in inputs.chain35:
        for session in _tuning_sessions(inputs):
            p, why = percentile(inputs, feats, name, session)
            if p is None:
                n_ne += 1
                if why.startswith("schedule withheld"):
                    withheld += 1
            else:
                n_eval += 1
    print(
        f"percentile coverage on the tuning grid (35 names x "
        f"{len(_tuning_sessions(inputs))} sessions): evaluable {n_eval},"
        f" not-evaluable {n_ne} (schedule-withheld {withheld})"
    )
    print(f"registry db: {REGISTRY_PATH}")
    print(f"elapsed {time.monotonic() - t0:.1f}s")
    return 0


def phase_register() -> int:
    inputs = load_and_bind()
    registry = _open_registry()
    try:
        existing = [c for c in CONFIG_IDS_E + CONFIG_IDS_O if registry.is_registered(_trial_id(c))]
        if existing:
            raise Refused(
                f"registration is one-shot: {[_trial_id(c) for c in existing]} already registered"
            )
        for config_id in CONFIG_IDS_E + CONFIG_IDS_O:
            hyper = _hyperparameters(inputs, config_id)
            record = TrialRecord(
                trial_id=_trial_id(config_id),
                created_at=_utcnow(),
                hypothesis=inputs.slot["hypothesis"],
                git_sha=_git_head(REPO_ROOT),
                config_hash=_config_hash(hyper),
                dataset_manifest_hash=inputs.dataset_manifest_hash,
                train_window=None,
                validation_window=(
                    (
                        inputs.window_sessions[ORD_V1[0] - 1],
                        inputs.window_sessions[ORD_TUNE_MAX_ENTRY - 1],
                    )
                    if config_id in CONFIG_IDS_E
                    else None
                ),
                test_window=None,
                hyperparameters=hyper,
                scope_key=_scope(inputs, config_id).scope_key(),
            )
            registry.register(record, _scope(inputs, config_id))
            print(
                f"registered {_trial_id(config_id)} ({GATE_RULES[config_id].split(':')[0]})"
            )
    finally:
        registry.close()
    print(
        f"registry: {REGISTRY_PATH} (scope {SCOPE_E}: 20/32, scope {SCOPE_O}: 4/32);"
        " NO outcome has been computed or viewed"
    )
    return 0


def _execute_config(
    inputs: Inputs,
    feats: Features,
    base_rows: Sequence[Mapping[str, Any]],
    config_id: str,
) -> dict[str, Any]:
    """Score ONE scope-E config on the tuning region (the single scored run)."""
    cal = inputs.calendar
    if config_id.startswith("xe-"):
        decisions = xe_decisions(inputs, feats, base_rows, config_id)
    else:
        decisions = xp_decisions(inputs, feats, base_rows, config_id)
    base_trades: list[dict[str, Any]] = []
    for row in base_rows:
        t = _complete_trade(inputs, row["name"], row["session"], 1.0, row["detail"])
        base_trades.append(
            {
                "entry": t.entry,
                "exit": t.exit_session,
                "name": t.name,
                "gross": t.gross,
                "weight": t.weight,
                "detail": t.detail,
                "condition": None,
                "condition_value": None,
            }
        )
    on_trades: list[dict[str, Any]] = []
    abstained = 0
    ne_notes: dict[str, int] = {}
    for row, dec in zip(base_rows, decisions, strict=True):
        if not dec.fire:
            if dec.value is None:
                abstained += 1
                key = dec.note.split(":")[0][:80]
                ne_notes[key] = ne_notes.get(key, 0) + 1
            continue
        entry_session = (
            row["session"] if dec.entry_shift == 0 else cal.nth_after(row["session"], dec.entry_shift)
        )
        # sealed-window guard: a shifted entry must still clear the purge
        if inputs.window_ordinal(entry_session) > ORD_TUNE_MAX_ENTRY:
            raise Refused(
                f"{config_id}: deferred entry {entry_session} exceeds ordinal {ORD_TUNE_MAX_ENTRY}"
                " -- the hold would read the sealed window"
            )
        t = _complete_trade(
            inputs, row["name"], entry_session, dec.weight, row["detail"] + f";{config_id}"
        )
        on_trades.append(
            {
                "entry": t.entry,
                "exit": t.exit_session,
                "name": t.name,
                "gross": t.gross,
                "weight": t.weight,
                "detail": t.detail,
                "condition": dec.condition,
                "condition_value": dec.value,
                "note": dec.note,
            }
        )
    family = "xe" if config_id.startswith("xe-") else "xp"
    base_complete = [t for t in base_trades if t["gross"] is not None]
    on_complete = [t for t in on_trades if t["gross"] is not None]
    base_mean = statistics.fmean(t["gross"] - RT_PRIMARY for t in base_complete)
    base_mean15 = statistics.fmean(t["gross"] - RT_ROBUST for t in base_complete)
    strat_mean = _weighted_net_mean(on_complete, RT_PRIMARY)
    strat_mean15 = _weighted_net_mean(on_complete, RT_ROBUST)
    delta = strat_mean - base_mean if strat_mean is not None else None
    delta15 = strat_mean15 - base_mean15 if strat_mean15 is not None else None
    # matched-sessions ON/OFF column (descriptive; decisive only on the sealed window)
    if config_id in ("xe-size-mkt", "xe-size-book", "xp-size-name", "xp-size-mkt"):
        hi = [t for t in on_complete if t["weight"] < 1.0]
        lo = [t for t in on_complete if t["weight"] >= 1.0]
        on_off = {
            "meaning": "half-sized (condition high) vs full-sized entries",
            "on_mean": statistics.fmean(t["gross"] - RT_PRIMARY for t in hi) if hi else None,
            "off_mean": statistics.fmean(t["gross"] - RT_PRIMARY for t in lo) if lo else None,
            "n_on": len(hi),
            "n_off": len(lo),
        }
        if on_off["on_mean"] is not None and on_off["off_mean"] is not None:
            on_off["on_minus_off"] = on_off["on_mean"] - on_off["off_mean"]
    elif config_id == "xe-defer":
        base_keys = {(b["name"], b["entry"]) for b in base_complete}
        deferred = [t for t in on_complete if (t["name"], t["entry"]) not in base_keys]
        on_time = [t for t in on_complete if (t["name"], t["entry"]) in base_keys]
        on_off = {
            "meaning": "deferred entries vs on-time entries (config trades)",
            "on_mean": statistics.fmean(t["gross"] - RT_PRIMARY for t in deferred) if deferred else None,
            "off_mean": statistics.fmean(t["gross"] - RT_PRIMARY for t in on_time) if on_time else None,
            "n_on": len(deferred),
            "n_off": len(on_time),
        }
        if on_off["on_mean"] is not None and on_off["off_mean"] is not None:
            on_off["on_minus_off"] = on_off["on_mean"] - on_off["off_mean"]
    else:
        fired_keys = {(t["name"], t["entry"]) for t in on_complete}
        off = [t for t in base_complete if (t["name"], t["entry"]) not in fired_keys]
        on_mean = statistics.fmean(t["gross"] - RT_PRIMARY for t in on_complete) if on_complete else None
        off_mean = statistics.fmean(t["gross"] - RT_PRIMARY for t in off) if off else None
        on_off = {
            "meaning": "fired (ON) vs base-not-fired (OFF) complete trades, matched region",
            "on_mean": on_mean,
            "off_mean": off_mean,
            "n_on": len(on_complete),
            "n_off": len(off),
        }
        if on_mean is not None and off_mean is not None:
            on_off["on_minus_off"] = on_mean - off_mean
    n_fired = len(on_trades)
    floor = FLOORS[family]
    return {
        "config_id": config_id,
        "family": family,
        "placebo": config_id in PLACEBOS,
        "gate_rule": GATE_RULES[config_id],
        "region": {
            "meaning": "pooled inner V1+V2 with entry ordinal <= 417 (INV-06 at the desk hold length)",
            "first_entry": base_rows[0]["session"].isoformat() if base_rows else None,
            "last_entry": base_rows[-1]["session"].isoformat() if base_rows else None,
            "sealed_window_read": False,
        },
        "base_cell": _cell_stats(base_trades),
        "on_cell": _cell_stats(on_trades),
        "deltas": {
            "cond_minus_base_5bp": delta,
            "cond_minus_base_15bp": delta15,
            "registered_selection_metric": "cond_minus_base_5bp",
        },
        "on_minus_off": on_off,
        "power_floor": {
            "required_n_on": floor,
            "n_on_fired": n_fired,
            "n_on_complete": len(on_complete),
            "met_on_fired": n_fired >= floor,
            "met_on_complete": len(on_complete) >= floor,
        },
        "condition_tallies": {
            "base_entries": len(base_rows),
            "fired": n_fired,
            "not_fired_gate_off": len(base_rows) - n_fired - abstained,
            "abstained_condition_not_evaluable": abstained,
            "not_evaluable_notes": ne_notes,
        },
        "trades_on": on_trades,
        "base_trades": base_trades,
    }


def phase_execute() -> int:
    """One-shot scored run of the 20 scope-E configs on the tuning region.
    Scope O is refused (data-gated) and its withdrawal is recorded."""
    inputs = load_and_bind()
    feats = build_features(inputs)
    SLOT_DIR.mkdir(parents=True, exist_ok=True)
    TRIALS_DIR.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise Refused("another vrp-cond execution holds the lock -- one run at a time") from None

        # scope O: data-gated, WITHDRAWN without running (no proxy)
        gates = _scope_o_gate_facts()
        record = {
            "stamp": _stamp(inputs, "scope-O", SCOPE_O),
            "configs": list(CONFIG_IDS_O),
            "verdict": "WITHDRAWN",
            "not_run": True,
            "menu_data_gates": inputs.slot["data_gates"],
            "gate_facts": gates,
            "note": (
                "Scope O (ox-cheap, ox-rich-fallback, op-cheap, op-rich-fallback) is DATA_GATED"
                " on (i) the long-dated option-bar capture landing >= 126 sessions of history and"
                " (ii) an IVHIST-002-successor fidelity verdict. Neither gate is met; the four"
                " configs are registered and withdrawn WITHOUT running. No proxy is improvised."
                " If the capture fails the withdrawal is terminal (menu scope-O gate)."
            ),
        }
        SCOPE_O_RECORD_PATH.write_text(
            json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        for config_id in CONFIG_IDS_O:
            print(f"{_trial_id(config_id)}: WITHDRAWN (data-gated, not run) -> {SCOPE_O_RECORD_PATH}")

        registry = _open_registry()
        try:
            for config_id in CONFIG_IDS_E:
                trial_id = _trial_id(config_id)
                artifact = _artifact_path(config_id)
                if artifact.exists():
                    raise Refused(f"{artifact} already exists -- executions are one-shot per trial")
                status = registry.status(trial_id)
                if status != "REGISTERED":
                    raise Refused(f"{trial_id} is {status}, not REGISTERED -- refusing to re-run")
                hyper = _hyperparameters(inputs, config_id)
                git_sha = _git_head(REPO_ROOT)
                registry.mark_running(
                    trial_id,
                    git_sha=git_sha,
                    config_hash=_config_hash(hyper),
                    dataset_manifest_hash=inputs.dataset_manifest_hash,
                    at=_utcnow(),
                )
                if config_id.startswith("xe-"):
                    base_rows = xsmom_base(inputs)
                else:
                    base_rows = pead_base(inputs)
                payload = _execute_config(inputs, feats, base_rows, config_id)
                # machinery self-check (the tnull g1 lesson): the feature layer
                # must be alive -- every family must carry at least one evaluable
                # condition somewhere in the region, else the trial FAILS.
                tallies = payload["condition_tallies"]
                if tallies["abstained_condition_not_evaluable"] == tallies["base_entries"]:
                    registry.fail(
                        trial_id,
                        "every base entry carried a NOT_EVALUABLE condition -- feature machinery"
                        " defect, not an outcome",
                        at=_utcnow(),
                    )
                    raise Refused(
                        f"{trial_id}: every base entry NOT_EVALUABLE (machinery defect;"
                        " trial FAILED, no artifact written)"
                    )
                body = {"stamp": _stamp(inputs, config_id, SCOPE_E), "payload": payload}
                artifact.write_text(
                    json.dumps(body, indent=2, sort_keys=True) + "\n", encoding="utf-8"
                )
                registry.complete(trial_id, metrics_uri=str(artifact), outcome_at=_utcnow())
                d = payload["deltas"]["cond_minus_base_5bp"]
                fl = payload["power_floor"]
                print(
                    f"{trial_id}: COMPLETED artifact={artifact}"
                    f" n_on={fl['n_on_fired']}/{fl['required_n_on']}"
                    f" delta_5bp={d if d is None else round(d, 6)}"
                )
        finally:
            registry.close()
    finally:
        os.close(lock_fd)
    return 0


def _scope_o_gate_facts() -> dict[str, Any]:
    """Read-only facts behind the scope-O data gates (no outcome involved)."""
    capture_dir = MAIN_ROOT / "artifacts" / "desk-longdated-capture"
    manifest = capture_dir / "capture_manifest.json"
    facts: dict[str, Any] = {
        "capture_dir": str(capture_dir),
        "capture_manifest_exists": manifest.exists(),
        "ivhist_002_successor_exists": any(
            p.name.startswith("IVHIST-002") for p in (MAIN_ROOT / "docs" / "desk").glob("IVHIST-*")
        ),
    }
    if manifest.exists():
        doc = json.loads(manifest.read_text(encoding="utf-8"))
        as_of = sorted({m.get("as_of") for m in doc.get("masters", []) if m.get("as_of")})
        facts["capture_as_of_dates"] = len(as_of)
        facts["capture_required_sessions"] = 126
        facts["gate_i_met"] = len(as_of) >= 126
    else:
        facts["gate_i_met"] = False
    facts["gate_ii_met"] = False
    facts["gates_met"] = False
    return facts


def _read_artifact(inputs: Inputs, config_id: str) -> Mapping[str, Any]:
    trial_id = _trial_id(config_id)
    artifact = _artifact_path(config_id)
    body = json.loads(artifact.read_text(encoding="utf-8"))
    stamp = body.get("stamp", {})
    if (
        stamp.get("config_id") != config_id
        or stamp.get("scope_id") != SCOPE_E
        or stamp.get("round") != ROUND
    ):
        raise Refused(f"{artifact} does not bind {trial_id}")
    if stamp.get("registration_menu_sha256") != inputs.menu_sha256:
        raise Refused(f"{artifact} was executed against a different menu hash")
    if stamp.get("dataset_manifest_hash") != inputs.dataset_manifest_hash:
        raise Refused(f"{artifact} was executed against different inputs")
    return body


def phase_select() -> int:
    """Read ONLY the executed artifacts and apply the registered selection rule."""
    inputs = load_and_bind()
    if SELECTION_PATH.exists():
        raise Refused(f"{SELECTION_PATH} already exists -- the round-1 selection is one-shot")
    rows: dict[str, Any] = {}
    for config_id in CONFIG_IDS_E:
        body = _read_artifact(inputs, config_id)
        payload = body["payload"]
        delta = payload["deltas"]["cond_minus_base_5bp"]
        fl = payload["power_floor"]
        # the floor binds on fired n_ON (the registration's entry/trade count)
        rows[config_id] = {
            "family": payload["family"],
            "placebo": payload["placebo"],
            "delta_cond_minus_base_5bp": delta,
            "delta_cond_minus_base_15bp": payload["deltas"]["cond_minus_base_15bp"],
            "on_minus_off_5bp": payload["on_minus_off"].get("on_minus_off"),
            "n_on_fired": fl["n_on_fired"],
            "n_on_complete": fl["n_on_complete"],
            "n_base_entries": payload["condition_tallies"]["base_entries"],
            "floor_required": fl["required_n_on"],
            "floor_met": fl["met_on_fired"],
            "on_fraction": (
                fl["n_on_fired"] / payload["condition_tallies"]["base_entries"]
                if payload["condition_tallies"]["base_entries"]
                else None
            ),
            "verdict": (
                "EVALUABLE" if fl["met_on_fired"] else "NOT_EVALUABLE"
            ),
            "abstained_ne": payload["condition_tallies"]["abstained_condition_not_evaluable"],
        }
    families: dict[str, Any] = {}
    for family in ("xe", "xp"):
        members = [cid for cid in CONFIG_IDS_E if rows[cid]["family"] == family]
        eligible = [
            cid
            for cid in members
            if rows[cid]["floor_met"]
            and rows[cid]["delta_cond_minus_base_5bp"] is not None
            and not rows[cid]["placebo"]
        ]
        ranked = sorted(
            members,
            key=lambda cid: (
                -(rows[cid]["delta_cond_minus_base_5bp"] or float("-inf")),
                rows[cid]["on_fraction"] if rows[cid]["on_fraction"] is not None else 1.0,
                cid,
            ),
        )
        if eligible:
            promoted = sorted(
                eligible,
                key=lambda cid: (
                    -rows[cid]["delta_cond_minus_base_5bp"],
                    rows[cid]["on_fraction"],
                    cid,
                ),
            )[0]
        else:
            promoted = None
        families[family] = {
            "members": members,
            "ranking_all_by_delta": ranked,
            "eligible_non_placebo_floor_met": eligible,
            "promoted": promoted,
            "promoted_delta": rows[promoted]["delta_cond_minus_base_5bp"] if promoted else None,
        }
    selection = {
        "stamp": _stamp(inputs, "round1-selection", SCOPE_E),
        "round": ROUND,
        "region": "pooled inner V1+V2, entry window ordinals 253..417 (sealed window untouched)",
        "registered_selection_rule": inputs.slot["acceptance_criteria"][0],
        "metric": "cond_minus_base_5bp (pooled conditioned-minus-base per-trade net delta)",
        "tie_break": "sparser gate (smaller on_fraction), then config id lexicographic",
        "placebo_reading": (
            "xe-lag21 / xp-lag21 / xp-shuffle run through the identical path and are ranked,"
            " but are CONTROLS, not promotion candidates: sealed criterion 3 compares the"
            " promoted config's delta against the same-family placebo deltas, so a placebo"
            " cannot be the nominee it is compared against."
        ),
        "per_config": rows,
        "families": families,
        "notes": [
            "Round 1 (tuning) only. The sealed window (ordinals 443..505) was never read;",
            "sealed acceptance criteria 1-4 apply only after the promoted configs are frozen",
            "and are evaluated by the sealed round, not here.",
            "Nothing is adopted: nomination at most (>= 20 forward sealed cards govern promotion).",
        ],
    }
    SELECTION_PATH.write_text(json.dumps(selection, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    for family in ("xe", "xp"):
        blk = families[family]
        print(
            f"{family}: promoted={blk['promoted']}"
            f" delta={blk['promoted_delta'] if blk['promoted_delta'] is None else round(blk['promoted_delta'], 6)}"
            f" eligible={len(blk['eligible_non_placebo_floor_met'])}"
        )
        for cid in blk["ranking_all_by_delta"][:6]:
            r = rows[cid]
            d = r["delta_cond_minus_base_5bp"]
            d_str = "None" if d is None else f"{d:+.6f}"
            print(
                f"  {cid:14s} delta={d_str}"
                f" n_on={r['n_on_fired']}/{r['floor_required']}"
                f" {'PLACEBO' if r['placebo'] else ('eligible' if r['floor_met'] else 'NOT_EVALUABLE')}"
            )
    print(f"selection stamped: {SELECTION_PATH}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--plan", action="store_true", help="read-only: bind, geometry and feature coverage"
    )
    parser.add_argument(
        "--register",
        action="store_true",
        help="INV-13: write the 24 trial rows (REGISTERED, no outcome) to the slot registry",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="one-shot scored run of the 20 scope-E configs on the pooled inner folds;"
        " scope O is withdrawn (data-gated) without running",
    )
    parser.add_argument(
        "--select",
        action="store_true",
        help="read ONLY the executed artifacts and stamp the round-1 selection",
    )
    args = parser.parse_args(argv)
    try:
        if args.plan:
            return phase_plan()
        if args.register:
            return phase_register()
        if args.execute:
            return phase_execute()
        if args.select:
            return phase_select()
    except Refused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
