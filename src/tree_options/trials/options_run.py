"""Options trial runner (M3 plan §3.F): register before outcome, execute,
stamp, complete — the INV-13/14 discipline mirroring trials/run.py.

One options trial = one (world x arm x strategy config) over protocol
walk-forward folds. The signal is an UNDERLYING-level scored cross-section
(the caller supplies it — the runner is model-agnostic; the model family
and its artifact hash ride in the config). Per fold the options backtest
runs with fresh cash; the payload carries per-position rows and a per-fill
log so the sealed gate's criteria 1-7 are evaluable from stamped artifacts
alone.

The 7200 s quote-age override (owner ruling 4) is an EXPLICIT config key —
hashed into every stamp — while research_protocol.yaml stays byte-frozen.

OD1/OD2/OD3 statistics stamped in the payload (all computed on the
direction-aligned SIGNED premium return — call return as-is, put return
sign-flipped — so a mixed long-call/long-put book pools against one label
direction; the first dev run's mixed-pool rho ~ 0 was exactly this sign
cancellation, caught by the §7 tripwire):
- fidelity factor rho = Spearman(signed premium return, H5 label) over
  closed positions (the vehicle transmits underlying exposure);
- per-cohort IC = Spearman(score, signed premium return) over positions
  entered on one session; the PRIMARY estimator uses DISJOINT cohorts
  (every 4th session, plan §7) and the payload stamps both the all-cohort
  series and the stride-4 subset with sd, autocorrelation, and t;
- per-session ICs (SECONDARY, reported only);
- OD2 machinery oracles: the OptionsCounters + CandidateAudit aggregates.

Determinism: no randomness; the caller injects the clock; tree_options
imports no synth here (the driver owns world construction).
"""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import cast

from tree_options.backtest.options import Arm, OptionsBacktestResult, run_options_backtest
from tree_options.candidates.filters import CandidateFilter
from tree_options.data.authority import PointInTimeDataset
from tree_options.data.options_pit import OptionPitSurface
from tree_options.evaluation.stats import ScoredLabel, backtest_summary
from tree_options.options import OptionSignal, OptionsStrategyConfig
from tree_options.protocol.holdout import (
    FINAL_HOLDOUT_DATES,
    FINAL_HOLDOUT_SCOPE,
    FINAL_HOLDOUT_WINDOW_ID,
)
from tree_options.protocol.loader import protocol_hash
from tree_options.protocol.schema import ResearchProtocol
from tree_options.protocol.stamping import build_stamp, write_artifact
from tree_options.registry.scope import TrialScope
from tree_options.registry.sqlite import TrialRegistry
from tree_options.schemas.trial import TrialRecord
from tree_options.splitting.splitter import Fold, WalkForwardSplitter
from tree_options.time.calendar import (
    CalendarError,
    CalendarIntegrityError,
    SessionCalendar,
    calendar_content_sha256,
)
from tree_options.trials.null_score import NULL_SCORE_MODEL_FAMILY, null_score

RUNNER_REVISION = "trials.options_run/v1"
OPTIONS_BACKTEST_INITIAL_CASH = Decimal("1000000.00")
DECLARED_MAX_QUOTE_AGE_SECONDS = 7200  # owner ruling 4 — a config key, hashed
COHORT_STRIDE = 4  # plan §7: disjoint entry cohorts every 4th session
END_BUFFER_SESSIONS = 6  # let arm-A exits land inside the evaluated window
# (w5, verdict D7) the ratified window A enumeration (protocol.holdout is the
# single source) as ISO session keys — the seal this runner ENFORCES at
# registration time and DISCLOSES against in every payload
_SEALED_HOLDOUT_SESSIONS = frozenset(FINAL_HOLDOUT_DATES)


@dataclass(frozen=True)
class _ODStats:
    n_folds: int
    n_positions: int
    fidelity_rho: float | None
    stride4_cohort_ic_mean: float | None
    stride4_cohort_ic_sd: float | None
    stride4_cohort_t: float | None


@dataclass(frozen=True)
class OptionsTrialResult:
    trial_id: str
    artifact_path: Path
    n_folds: int
    n_positions: int
    fidelity_rho: float | None
    cohort_ic_mean: float | None
    cohort_ic_sd: float | None
    cohort_t: float | None


def _spearman(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    """Spearman rank correlation with average-tie ranks (pooled, exact)."""
    n = len(xs)
    if n < 3 or n != len(ys):
        return None

    def ranks(values: Sequence[float]) -> list[float]:
        order = sorted(range(n), key=lambda i: values[i])
        out = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and values[order[j + 1]] == values[order[i]]:
                j += 1
            average = (i + j) / 2 + 1  # average of ranks i+1 .. j+1
            for k in range(i, j + 1):
                out[order[k]] = average
            i = j + 1
        return out

    rx, ry = ranks(xs), ranks(ys)
    mean_x = sum(rx) / n
    mean_y = sum(ry) / n
    cov = sum((a - mean_x) * (b - mean_y) for a, b in zip(rx, ry, strict=True))
    var_x = sum((a - mean_x) ** 2 for a in rx)
    var_y = sum((b - mean_y) ** 2 for b in ry)
    if var_x <= 0 or var_y <= 0:
        return None
    return cov / math.sqrt(var_x * var_y)


def _lag1_autocorrelation(xs: Sequence[float]) -> float | None:
    n = len(xs)
    if n < 3:
        return None
    mean = sum(xs) / n
    num = sum((xs[i] - mean) * (xs[i + 1] - mean) for i in range(n - 1))
    den = sum((x - mean) ** 2 for x in xs)
    if den <= 0:
        return None
    return num / den


def _t_statistic(values: Sequence[float]) -> float | None:
    n = len(values)
    if n < 2:
        return None
    mean = sum(values) / n
    if n < 3:
        return None
    sd = statistics.stdev(values)
    if sd <= 0:
        return None
    return mean * math.sqrt(n) / sd


def _cohort_series(
    closed: list[dict[str, object]],
) -> tuple[list[tuple[str, float]], list[float], list[int]]:
    """Per-entry-session cohort ICs and the stride-4 disjoint series.

    The stride selects every COHORT_STRIDE-th session of the FULL
    entry-session grid — the predeclared every-fourth-session statistic
    (review r3 P1-2) — and keeps only defined ICs among those grid points:
    an undefined cohort IC (fewer than 3 positions) drops its POINT
    without shifting later ICs onto earlier sessions and without
    advancing which sessions the grid selects.
    """
    by_entry: dict[str, list[dict[str, object]]] = {}
    for p in closed:
        by_entry.setdefault(str(p["entry_session"]), []).append(p)
    cohort_ics: list[float] = []
    cohort_counts: list[int] = []
    stride4: list[tuple[str, float]] = []
    for index, session in enumerate(sorted(by_entry)):
        rows = by_entry[session]
        ic = _spearman(
            [float(p["score"]) for p in rows],  # type: ignore[arg-type]
            [float(p["signed_premium_return"]) for p in rows],  # type: ignore[arg-type]
        )
        if ic is None:
            continue
        cohort_ics.append(ic)
        cohort_counts.append(len(rows))
        if index % COHORT_STRIDE == 0:
            stride4.append((session, ic))
    return stride4, cohort_ics, cohort_counts


def _dataset_provenance(snapshot_id: str, bar_sources: frozenset[str]) -> str:
    """(G3 extension, w4) The dataset's provenance, DERIVED — never the
    hardcoded ``"synthetic/v1"`` the payload used to claim for every world.

    A dataset whose every bar carries the synthetic generator's own source
    token (``synth.generate.PROVIDER`` == "synthetic/v1") keeps the
    historical token BYTE-IDENTICALLY, so every existing synthetic trial's
    artifact is unchanged. Any other dataset — a mixed-source one, or the
    lane-2 ``massive-derived-free/1`` world — is stamped with its SNAPSHOT
    IDENTITY, so a lane-2 artifact can never claim synthetic provenance.
    (This module deliberately does not import ``tree_options.synth``: the
    token is pinned by test against the generator's constant.)"""
    if bar_sources == {"synthetic/v1"}:
        return "synthetic/v1"
    return snapshot_id


def _label_window(
    sessions: tuple[date, ...],
    ordinal: dict[date, int],
    decision_session: date | None,
    horizon_sessions: int,
) -> dict[str, str] | None:
    """The DECISION's label window, in `labels.build` semantics: base = the
    session before the decision (the last bar visible at close(d)), end =
    base + `horizon_sessions`. None when the decision has no prior session
    or the window runs past the calendar (the label builder's own skips)."""
    if decision_session is None or decision_session not in ordinal:
        return None
    base_ordinal = ordinal[decision_session] - 1
    end_ordinal = base_ordinal + horizon_sessions
    if base_ordinal < 0 or end_ordinal >= len(sessions):
        return None
    return {
        "start": sessions[base_ordinal].isoformat(),
        "end": sessions[end_ordinal].isoformat(),
    }


def _window_touches_seal(window: dict[str, str]) -> bool:
    """Does a contiguous session window [start, end] contain any sealed
    date? Read from the module's sealed set at call time so the disclosure
    follows the ratified enumeration (protocol.holdout), the single source."""
    start = date.fromisoformat(window["start"])
    end = date.fromisoformat(window["end"])
    return any(start <= date.fromisoformat(session) <= end for session in _SEALED_HOLDOUT_SESSIONS)


def _position_payloads(
    result: OptionsBacktestResult,
    *,
    calendar: SessionCalendar,
    label_horizon_sessions: int,
) -> list[dict[str, object]]:
    sessions = calendar.sessions()
    ordinal = {session: index for index, session in enumerate(sessions)}
    rows: list[dict[str, object]] = []
    for p in result.positions:
        rows.append(
            {
                "underlying_security_id": p.underlying_security_id,
                "call_put": p.call_put,
                "score": p.score,
                "label": p.label,
                "entry_session": p.entry_session.isoformat(),
                "entry_price": str(p.entry_price),
                "contract_expiration": p.contract_expiration.isoformat(),
                "exit_kind": p.exit_kind,
                "exit_session": p.exit_session.isoformat() if p.exit_session else None,
                "exit_price": str(p.exit_price) if p.exit_price is not None else None,
                "premium_return": p.premium_return,
                # direction-aligned return: the premium return a LONG CALL on
                # the underlying would have earned — puts are sign-flipped so
                # the vehicle statistics pool both sides against the same
                # label direction (OD1's mixed-pool rho ~ 0 was exactly this
                # sign cancellation; found by the §7 tripwire, not tuned)
                "signed_premium_return": (
                    p.premium_return
                    if (p.call_put == "C" or p.premium_return is None)
                    else -p.premium_return
                ),
                # ---- G3 extension (verdict D6, additive): the per-position
                # facts that make T-BAND/T-DTE and the earnings retro-tag
                # artifact-computable. strike/abs_delta are the DECISION-
                # VISIBLE file(t-1) pair the band rules classified on; dte
                # is the filter's calendar-day convention.
                "strike": str(p.strike) if p.strike is not None else None,
                "abs_delta": str(p.abs_delta) if p.abs_delta is not None else None,
                "dte_at_entry": p.dte_at_entry,
                "decision_session": (
                    p.decision_session.isoformat() if p.decision_session else None
                ),
                "label_window": _label_window(
                    sessions, ordinal, p.decision_session, label_horizon_sessions
                ),
                # the executed round trip: entry fill to exit (end None while
                # the position is still open at the fold's last session)
                "hold_window": {
                    "start": p.entry_session.isoformat(),
                    "end": p.exit_session.isoformat() if p.exit_session else None,
                },
            }
        )
    return rows


def _fill_log(result: OptionsBacktestResult) -> list[dict[str, object]]:
    audit_by_fill = {a.fill_id: a for a in result.fill_audit}
    rows: list[dict[str, object]] = []
    for f in result.fills:
        audit = audit_by_fill.get(f.fill_id)
        rows.append(
            {
                "fill_id": f.fill_id,
                "contract_id": f.contract_id,
                "side": f.side,
                "quantity": f.quantity,
                "price": str(f.price),
                "execution_at": f.execution_at.isoformat(),
                "execution_session": f.execution_session.isoformat(),
                # criterion 2 (sealed gate): T+1 discipline + quote receipt,
                # re-derivable from the stamped artifact alone
                "decision_session": (audit.decision_session.isoformat() if audit else None),
                "decision_at": audit.decision_at.isoformat() if audit else None,
                "quote_received_at": (audit.quote_received_at.isoformat() if audit else None),
            }
        )
    return rows


@dataclass(frozen=True)
class OptionsSplitOverride:
    """Explicit fold geometry (fixture-scale runs); recorded in the config
    hash, so a deviation from protocol defaults is never silent."""

    label_horizon_sessions: int
    embargo_sessions: int
    val_sessions: int
    test_sessions: int
    roll_sessions: int
    min_train_sessions: int


def _split_params(
    protocol: ResearchProtocol, override: OptionsSplitOverride | None
) -> dict[str, int]:
    if override is not None:
        return {
            "label_horizon_sessions": override.label_horizon_sessions,
            "embargo_sessions": override.embargo_sessions,
            "val_sessions": override.val_sessions,
            "test_sessions": override.test_sessions,
            "roll_sessions": override.roll_sessions,
            "min_train_sessions": override.min_train_sessions,
        }
    f = protocol.folds
    return {
        "label_horizon_sessions": f.label_horizon_sessions,
        "embargo_sessions": f.embargo_sessions,
        "val_sessions": f.validation_window_sessions.default,
        "test_sessions": f.test_window_sessions.default,
        "roll_sessions": f.roll_forward_sessions,
        "min_train_sessions": f.min_train_sessions,
    }


def _fold_backtest(
    *,
    fold: Fold,
    fold_scored: Sequence[ScoredLabel],
    calendar: SessionCalendar,
    surface: OptionPitSurface,
    dataset: PointInTimeDataset,
    candidate_filter: CandidateFilter,
    config: OptionsStrategyConfig,
    arm: Arm,
    world_last_session: date,
    execution_calendar: SessionCalendar | None = None,
) -> tuple[OptionsBacktestResult, tuple[date, ...]]:
    """One fold's backtest plus its EXECUTION TAIL: the evaluated sessions
    strictly after the fold's last test session (the END_BUFFER window where
    the exits, settlements and marks of test-session decisions land). The
    tail is what the holdout-seal disclosure (w5, verdict D7.2) tags —
    decision sessions stay out of the seal, tail consumption is disclosed,
    never banned.

    (P1-1) The DECISION GRID calendar (`calendar`) owns the tail: the
    buffer is END_BUFFER GRID sessions deep, per the era profile's units.
    The clamp is in DATE space — `world_last_session` is the last DAILY
    bar session and need not be a grid session, so an ordinal comparison
    would refuse exactly the dual-calendar world the seam exists to
    serve; when the two calendars coincide it decides identically to the
    ordinal form it replaces."""
    last_test_session = max(fold.test_sessions)
    last_execution = calendar.nth_after(max(row.session for row in fold_scored), 1)
    buffered = calendar.nth_after(last_execution, END_BUFFER_SESSIONS)
    last_grid_session = max(s for s in calendar.sessions() if s <= world_last_session)
    end_session = buffered if buffered <= world_last_session else last_grid_session
    result = run_options_backtest(
        calendar=calendar,
        execution_calendar=execution_calendar,
        surface=surface,
        dataset=dataset,
        candidate_filter=candidate_filter,
        signals=[
            OptionSignal(
                decision_session=row.session,
                security_id=row.security_id,
                score=row.score,
                label=row.label,
            )
            for row in fold_scored
        ],
        initial_cash=OPTIONS_BACKTEST_INITIAL_CASH,
        config=config,
        arm=arm,
        end_session=end_session,
    )
    execution_tail = tuple(session for session in result.sessions if session > last_test_session)
    return result, execution_tail


def _calendar_descriptor(calendar: SessionCalendar) -> dict[str, object]:
    """(P1-1) The disclosure identity of one calendar: its declared name,
    its session count, its first/last session, and (R2-P1-b, Codex round 2)
    its COMPLETE content identity — enough for an artifact to name WHICH
    calendar drove decisions and which drove fills, without embedding the
    whole session list.

    (R2-P1-b) `content_sha256` is a domain-separated sha256 over the FULL
    session tuple, the early-close map, and the concrete class
    (`calendar_content_sha256`). The lossy fields alone let two calendars
    differing by ONE interior session — or by their early-close sets —
    share a config_hash, so INV-14 stamped an incomplete identity; the
    content hash closes that. (R3-P1-1, Codex round 3) the concrete class
    is in the digest for the same reason: a SUBCLASS reporting identical
    data may still override `ordinal`/`session_close`, so data identity
    alone is not behavioral identity. A calendar that does not disclose its
    early-close set refuses here (fail-closed), never hashes half its
    semantics."""
    sessions = calendar.sessions()
    return {
        "name": getattr(calendar, "name", type(calendar).__name__),
        "n_sessions": len(sessions),
        "first": sessions[0].isoformat(),
        "last": sessions[-1].isoformat(),
        "content_sha256": calendar_content_sha256(calendar),
    }


def _volume_flow_threshold(protocol: ResearchProtocol, override: int | None) -> int:
    """The lane-2 flow threshold: the explicit hashed override when given,
    else the protocol's ratified value. Validated by the filter's own rule
    (int >= 1, never a bool) BEFORE registration, so an illegal deviation
    refuses loudly instead of failing a registered trial."""
    lf = protocol.option_candidate_defaults.liquidity_volume_flow
    if override is not None:
        if not isinstance(override, int) or isinstance(override, bool) or override < 1:
            raise ValueError(f"flow_min_session_volume must be an int >= 1, got {override!r}")
        return override
    if lf is None:
        raise ValueError(
            "protocol carries no liquidity_volume_flow block: the volume-flow"
            " regime is not ratified"
        )
    value = lf.flow_min_session_volume
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(
            "the protocol's flow_min_session_volume is PENDING-era — supply the"
            " explicit flow_min_session_volume config key to run lane 2"
        )
    return value


def _refuse_slotted_bind(cls: type, seam: str) -> None:
    """(R8-P2, Codex round 8) Both bind factories copy state with
    `__dict__.update` ONLY — a class anywhere in the MRO declaring nonempty
    `__slots__` carries state that copy cannot reach, so the bound instance
    would run MISSING it: an incomplete bind. No repo surface or calendar
    uses slots; this refuses BY NAME before any construction, so a slotted
    class is a boundary refusal, never a half-bound run or a raw crash."""
    for klass in cls.__mro__:
        slots = klass.__dict__.get("__slots__", ())
        if slots:
            raise ValueError(
                f"{cls.__name__} declares __slots__"
                f" ({klass.__qualname__}: {tuple(slots)!r}): the bind's"
                " __dict__ copy cannot carry slot state, so the frozen"
                f" {seam} would ride an instance MISSING it — an incomplete"
                " bind must refuse before registration, never run half-bound"
            )


def _bind_decision_surface(
    underlying: OptionPitSurface, decision_closes: Mapping[date, datetime]
) -> OptionPitSurface:
    """(R6-P1 + R7-P2) THE FROZEN DECISION INSTANTS, bound onto one surface
    for the duration of a run.

    The boundary (`run_options_trial`) verifies `surface.decision_close(s)`
    against the stamped calendar ONCE per decision session before
    registration — but the runtime then called the same OVERRIDABLE method
    again (`options/strategy.py`'s candidate/expiry/strike reads, and the
    surfaces' own `candidate_snapshot`, which derives its stamped
    `decision_at` from `self.decision_close`), so a STATEFUL subclass
    answering the stamped close on each session's FIRST call and something
    else on later calls passed every boundary comparison while the wrong
    instant reached `build_candidates` (Codex round 6's call-sequence
    probe: pre_registration_equal=True, runtime_equal=False).

    R6 bound a WRAPPER; R7 binds a GENUINE same-class instance, because the
    wrapper's rebind was not an instance of the underlying's class:

    - `type(underlying).candidate_snapshot(self_wrapper, ...)` made a
      subclass's semantically neutral `super().candidate_snapshot(...)`
      raise TypeError AFTER registration — a failed registered trial on an
      input the boundary explicitly accepts (Codex round 7, P2);
    - `__getattr__`-delegated methods stayed bound to the UNDERLYING, so a
      subclass override that internally called `self.decision_close()`
      reached the unfrozen method (Codex round 7, P2).

    `bound` here IS an instance of the same concrete class, so subclass
    dispatch, `super()`, `self.__class__` and `isinstance` are all ordinary;
    the instance-attribute `decision_close` SHADOWS the class method at
    EVERY `self.decision_close(...)` call site — the class's own
    `candidate_snapshot`, any subclass override, any method that reaches
    the seam — and REFUSES (fail-closed) any session the boundary never
    verified, so nothing ever falls back to the overridable method.

    CAVEATS of the construction, which are the price of binding without
    wrapping:

    - `cls.__new__(cls)` SKIPS `__init__` deliberately: the underlying's
      constructor validates and re-derives, and must never run twice. All
      state comes EXCLUSIVELY from the copy below.
    - the copy is SHALLOW. The underlying is not used again after binding,
      and the surface's state is read-only for the duration of a run — the
      bound instance shares (not clones) every mutable attribute it holds,
      exactly as the underlying's own methods would share them with each
      other. A surface that mutates its own state mid-run was never a
      supportable input; the boundary's freeze is what makes that explicit.
    - `decision_close` is a RESERVED instance-attribute name on a bound
      surface. A class that itself sets `decision_close` as an instance
      attribute in `__init__` would be silently overridden here — but such
      a class is exactly the stateful shape the boundary freezes.
    - (R8-P2, Codex round 8) THE INSTALL IS VERIFIED. The assignment is a
      plain instance-attribute write, and Python does NOT let an instance
      attribute shadow a class-level DATA DESCRIPTOR: a subclass exposing
      `decision_close` as a callable-returning property with a no-op setter
      passed the ENTIRE preflight while the write silently installed
      NOTHING — the freeze never landed and the run proceeded on the
      unfrozen property. The bind now reads the attribute back and
      REQUIRES identity with the frozen closure (a descriptor-intercepted
      install refuses by name), wraps the assignment's AttributeError (a
      setter-less property) into the same named refusal, and refuses any
      class whose MRO declares nonempty `__slots__` (the `__dict__` copy
      cannot carry slot state — an incomplete bind refuses, never runs).

    Byte-identical for every wired configuration: the frozen instants ARE
    the stamped calendar's closes, which is exactly what an honest surface
    answers, so nothing is stamped, hashed, or registered differently. The
    static type stays `OptionPitSurface` by the documented adapter contract
    (static callers cast — `tests/unit/test_massive_overlay.py`); this
    factory performs that cast exactly once, at the bind."""
    cls = type(underlying)
    _refuse_slotted_bind(cls, "decision_close")
    bound = cast(OptionPitSurface, cls.__new__(cls))
    bound.__dict__.update(underlying.__dict__)

    def decision_close(decision_session: date) -> datetime:
        try:
            return decision_closes[decision_session]
        except KeyError:
            raise ValueError(
                f"no frozen decision instant for session"
                f" {decision_session.isoformat()}: the run consumes only the"
                " boundary-verified decision sessions — refusing fail-closed"
                " rather than consulting the surface's overridable"
                " decision_close()"
            ) from None

    # (R8-P2, Codex round 8) VERIFY THE INSTALL: read the attribute back and
    # REQUIRE identity — a class-level data descriptor (a property with a
    # no-op setter) swallows the write above without installing anything,
    # and the boundary's refusal is the only thing standing between that
    # class and a silently unfrozen run.
    try:
        bound.decision_close = decision_close  # type: ignore[method-assign]
    except AttributeError as exc:
        raise ValueError(
            f"cannot install the frozen decision_close on"
            f" {type(underlying).__name__}: the assignment raised"
            " AttributeError — a setter-less class-level data descriptor"
            " cannot accept the freeze — the freeze cannot be installed on"
            " this class; refusing before registration"
        ) from exc
    if bound.decision_close is not decision_close:
        raise ValueError(
            f"cannot install the frozen decision_close on"
            f" {type(underlying).__name__}: a class-level data descriptor"
            " intercepted the install — the freeze cannot be installed on"
            " this class; refusing before registration"
        )
    return bound


def _bind_decision_calendar(
    calendar: SessionCalendar, decision_closes: Mapping[date, datetime]
) -> SessionCalendar:
    """(R8-P1, Codex round 8) THE FROZEN CALENDAR CLOSES, bound onto the
    decision calendar for the duration of a run.

    R6/R7 froze the SURFACE's decision instants; the runtime still re-read
    the ORIGINAL mutable `calendar` directly at eight functional sites —
    the candidate filter's coherence read (`candidates/filters.py`), the
    entry `decision_at` stamp (`options/strategy.py`'s `plan_orders`),
    `plan_exit_order`, the retry/forced/decided sell stamps and the
    close(t) mark (`backtest/options.py`), and the fill doors
    (`guards/fills.py`). A calendar answering the fixture close on a
    session's first three reads (the two preflight reads plus the filter's
    coherence read) and 16:00 from the fourth therefore passed EVERY
    boundary guard while `plan_orders` stamped the early-close session at
    16:00 — an order executing on information unavailable at the verified
    decision instant (INV-02), under the configuration the boundary had
    verified (INV-14). This factory binds the calendar the same way
    `_bind_decision_surface` binds the surface, and `_execute` consumes
    ONLY the frozen closes.

    THE MAP covers the calendar's COMPLETE session set, not the decision
    set alone: the trial's decision sessions answer the loop's own VERIFIED
    `declared_close` values (R7-P1's discipline — the value the boundary
    compared, never a re-read), and every OTHER session of the calendar is
    read ONCE, here, before registration. The runtime's marks, exit
    decisions, retry and forced-close stamps read sessions the decision
    loop never visited — `backtest/options.py` stamps `close_at` for EVERY
    loop session including the execution tail — so a decision-only map
    would refuse honest reads; the complete map keeps every runtime read
    frozen while never consulting the overridable method again.

    SCOPING, disclosed: the geometry methods (`sessions`, `ordinal`,
    `nth_after`, `is_session`, `session_open`, `contains_instant`) stay the
    class's own over the copied state — the concrete class is digest-bound
    (round 3), so a different-geometry subclass is already refused at the
    boundary before this factory runs. Sessions outside the verified map —
    dates that are not sessions of this calendar at all — refuse
    fail-closed, naming the session, never an unverified instant. The FILL
    PATH: when `execution_calendar is None` the fill guard receives this
    bound calendar, so both sides of the `DECISION_INSTANT_NOT_CLOSE` door
    read frozen instants — coherent by construction; when the calendars
    differ the fill calendar remains the EXECUTION calendar (known item
    (a), 0.2.2-deferred — disclosed, unchanged).

    Byte-identical for every wired configuration: the frozen closes ARE the
    calendar's own answers, so no key, hash, or stamp moves. (R8-P2, Codex
    round 8) THE INSTALL IS VERIFIED, exactly as on the surface bind: the
    assignment is read back and required to BE the frozen closure (a
    class-level data descriptor intercepting the install refuses by name),
    an AttributeError from a setter-less descriptor becomes the same named
    refusal instead of escaping the boundary, and any class in the MRO
    declaring nonempty `__slots__` refuses up front — the `__dict__` copy
    cannot carry slot state, and an incomplete bind must refuse, never run."""
    cls = type(calendar)
    _refuse_slotted_bind(cls, "session_close")
    bound = cast(SessionCalendar, cls.__new__(cls))
    bound.__dict__.update(calendar.__dict__)

    frozen_closes: dict[date, datetime] = dict(decision_closes)
    for session in calendar.sessions():
        if session not in frozen_closes:
            frozen_closes[session] = calendar.session_close(session)

    def session_close(session: date) -> datetime:
        try:
            return frozen_closes[session]
        except KeyError:
            raise ValueError(
                f"no frozen close for session {session.isoformat()}: the run"
                " consumes only calendar sessions frozen at the boundary —"
                " refusing fail-closed rather than consulting the calendar's"
                " overridable session_close()"
            ) from None

    # (R8-P2, Codex round 8) VERIFY THE INSTALL — same shape, same reason
    # as the surface bind: a class-level data descriptor on `session_close`
    # would swallow the write and leave the run on the overridable method.
    try:
        bound.session_close = session_close  # type: ignore[method-assign,assignment]
    except AttributeError as exc:
        raise ValueError(
            f"cannot install the frozen session_close on"
            f" {type(calendar).__name__}: the assignment raised"
            " AttributeError — a setter-less class-level data descriptor"
            " cannot accept the freeze — the freeze cannot be installed on"
            " this class; refusing before registration"
        ) from exc
    if bound.session_close is not session_close:
        raise ValueError(
            f"cannot install the frozen session_close on"
            f" {type(calendar).__name__}: a class-level data descriptor"
            " intercepted the install — the freeze cannot be installed on"
            " this class; refusing before registration"
        )
    return bound


def run_options_trial(
    *,
    dataset: PointInTimeDataset,
    surface: OptionPitSurface,
    calendar: SessionCalendar,
    protocol: ResearchProtocol,
    world_id: str,
    arm: Arm,
    strategy_config: OptionsStrategyConfig,
    scored: Sequence[ScoredLabel],
    model_family: str,
    model_sha256: str | None,
    hypothesis: str,
    decision_sessions: Sequence[date],
    options_manifest_hash: str,
    registry: TrialRegistry,
    artifacts_dir: Path,
    repo: Path,
    clock: Callable[[], datetime],
    run_index: int = 1,
    split_override: OptionsSplitOverride | None = None,
    liquidity_lane: int = 1,
    flow_min_session_volume: int | None = None,
    score_seed: str | None = None,
    execution_calendar: SessionCalendar | None = None,
    allow_dirty: bool = False,
) -> OptionsTrialResult:
    """Register, execute, stamp, and complete one options trial.

    `liquidity_lane` selects the candidate-filter regime (G2): 1 keeps the
    two-sided `CandidateFilter.from_protocol` (byte-identical behavior);
    2 selects `from_protocol_volume_flow` for the Massive derived (vwap)
    lane. `flow_min_session_volume` is that regime's EXPLICIT hashed config
    key — default the protocol's ratified value (100), every deviation
    stamped into the config hash. Neither key perturbs a lane-1 trial.

    (P1-1, Codex round 1) `calendar` is the DECISION GRID (Friday-only on
    the real lane): it drives splitting, embargo, fold/test windows,
    decision-session stamps, the holdout guard, and the
    execution-tail/end_session logic. `execution_calendar` is the EXECUTION
    calendar (the overlay's daily `MassiveDerivedSessionCalendar`) the fill
    engine's session checks run against. Omitting it (or passing the SAME
    object) keeps one calendar for both roles — lane-1/synthetic payloads
    and config hashes are byte-identical; a DIFFERENT object is disclosed
    as additive `decision_calendar`/`execution_calendar` descriptors in
    the payload and rides the config hash."""
    if world_id != dataset.snapshot_id or world_id != surface.snapshot_id:
        raise ValueError(
            f"world_id {world_id!r} must match dataset {dataset.snapshot_id!r} "
            f"and surface {surface.snapshot_id!r}"
        )
    if arm not in ("A", "B"):
        raise ValueError(f"arm must be A or B, got {arm!r}")
    if liquidity_lane not in (1, 2):
        raise ValueError(f"liquidity_lane must be 1 or 2, got {liquidity_lane!r}")
    if liquidity_lane == 1 and flow_min_session_volume is not None:
        raise ValueError(
            "flow_min_session_volume is a lane-2 config key; lane 1 keeps the"
            " two-sided regime untouched"
        )
    flow_threshold = (
        _volume_flow_threshold(protocol, flow_min_session_volume) if liquidity_lane == 2 else None
    )
    if score_seed is not None and not score_seed:
        raise ValueError("score_seed must be a non-empty string when supplied")
    # (P1-3, Codex round 1) the null-score model's seed is BOUND to trial
    # identity. NULL_SCORE_MODEL_FAMILY was referenced nowhere in this
    # runner: score_seed=None passed silently and was omitted from the
    # hashed config, so two T-NULL trials with different seeds got
    # IDENTICAL config hashes, and nothing verified the rows against the
    # stamped seed. A null trial must now DECLARE its seed (an undeclared
    # seed is unregistered randomness) and every scored row must equal the
    # generator's own output under that seed — a misstated seed can no
    # longer masquerade as the declared score model. Non-null families keep
    # today's behavior exactly (seed optional, stamped when present).
    if model_family == NULL_SCORE_MODEL_FAMILY and score_seed is None:
        raise ValueError(
            f"a {NULL_SCORE_MODEL_FAMILY} trial must declare its seed —"
            " an undeclared seed is unregistered randomness"
        )
    if model_family == NULL_SCORE_MODEL_FAMILY and score_seed is not None:
        misstated = [
            f"{row.session.isoformat()}/{row.security_id}"
            for row in scored
            if row.score
            != null_score(seed=score_seed, session=row.session, security_id=row.security_id)
        ]
        if misstated:
            raise ValueError(
                f"score mismatch against the declared seed {score_seed!r}:"
                f" {misstated[:3]} do not equal {NULL_SCORE_MODEL_FAMILY}"
                " under that seed — a misstated seed cannot masquerade as"
                " the declared score model"
            )
    if not scored:
        raise ValueError("scored rows are required")
    normalized_sessions = tuple(decision_sessions)
    if not normalized_sessions or tuple(sorted(set(normalized_sessions))) != normalized_sessions:
        raise ValueError("decision_sessions must be non-empty, unique, and strictly increasing")
    scored_keys = {(row.session, row.security_id) for row in scored}
    if len(scored_keys) != len(scored):
        raise ValueError("duplicate (session, security_id) in scored rows")
    # (R4-P1, Codex round 4) BIND the surface's decision-calendar authority to
    # the trial's stamped calendar BEFORE registration. `decision_calendar`
    # was referenced only as a stamped descriptor — never verified against the
    # surface — so a VwapPitSurface constructed WITHOUT one answered
    # `decision_close()` from the overlay's nominal 16:00 while the trial was
    # stamped on an early-close-aware grid: different counters under the same
    # declared configuration (INV-02 + INV-14 at the trial boundary). The
    # surface must now DISCLOSE the calendar its decisions actually answer
    # from, and its COMPLETE content identity (`calendar_content_sha256` —
    # the existing digest machinery, never a second comparison) must equal
    # the stamped grid's. A surface that cannot disclose its authority — no
    # property, or a calendar that will not hash completely — cannot be
    # bound, and refuses; so does any identity mismatch. Byte-identical for
    # every wired configuration (the base surface's own calendar IS the
    # runner's calendar; a VwapPitSurface carrying the stamped grid):
    # digests equal, no keys added, no hash moved.
    try:
        disclosed = surface.decision_calendar
        surface_identity = calendar_content_sha256(disclosed)
        stamped_identity = calendar_content_sha256(calendar)
    except (AttributeError, CalendarIntegrityError) as exc:
        raise ValueError(
            f"surface {type(surface).__name__} cannot disclose its"
            f" decision-calendar authority for {world_id!r}: {exc} — a surface"
            " whose decision_close() answers from an undisclosed calendar"
            " cannot be bound to this trial's stamped calendar; refusing"
            " before registration"
        ) from exc
    if surface_identity != stamped_identity:
        raise ValueError(
            f"the surface's decision-calendar authority is not this trial's"
            f" stamped calendar for {world_id!r}: the surface's"
            f" decision_calendar content_sha256 {surface_identity} !="
            f" the stamped calendar's {stamped_identity} — an unwired surface"
            " must not run under a grid-stamped trial (its decision_close()"
            " answers from a calendar the trial never declared); refusing"
            " before registration"
        )
    # (R5-P1, Codex round 5) the digest above binds the calendar the surface
    # SAYS it answers from; the loop below binds the calendar it ACTUALLY
    # answers from — the decisions call `surface.decision_close()`
    # (options/strategy.py), never the disclosed property, and the adapter
    # explicitly supports subclassing. A subclass overriding ONLY
    # `decision_calendar` to return the stamped grid passes the digest (the
    # disclosure is self-attested) while the inherited `decision_close()`
    # still reads the unwired overlay calendar: the trial registers under a
    # configuration whose EFFECTIVE decision behavior differs (Codex's
    # probe: disclosed close 13:00, actual decision close 16:00 on
    # 2025-11-28). The boundary therefore additionally requires, for EVERY
    # session of the trial's full decision set (known here), that
    # `surface.decision_close(s) == calendar.session_close(s)`: a surface
    # whose method disagrees with its disclosure is caught by construction,
    # and a surface whose method agrees on every decision session of THIS
    # trial is behaviorally bound to the stamped calendar for everything the
    # trial can decide — which is the invariant. The digest stays as the
    # fast authority pre-filter (it also produces the unwired-case error);
    # the behavioral equality is the binding that cannot be self-attested
    # away. Byte-identical for every wired configuration: the base surface
    # and a wired adapter answer decision_close from the very calendar the
    # trial is stamped on, so every comparison is equal and nothing is
    # stamped, hashed, or registered differently.
    decision_closes: dict[date, datetime] = {}
    for session in normalized_sessions:
        declared_close = calendar.session_close(session)
        try:
            surface_close = surface.decision_close(session)
        except (AttributeError, CalendarError) as exc:
            raise ValueError(
                f"the surface's decision_close() cannot answer decision session"
                f" {session.isoformat()} for {world_id!r}: {exc} — a surface"
                " behaviorally bound to the stamped calendar answers every"
                " decision session's close; refusing before registration"
            ) from exc
        if surface_close != declared_close:
            raise ValueError(
                f"the surface's decision_close() disagrees with the trial's"
                f" stamped calendar for {world_id!r}: decision session"
                f" {session.isoformat()} closes {declared_close.isoformat()} on"
                f" the stamped calendar but the surface answers"
                f" {surface_close.isoformat()} — a surface whose method"
                " disagrees with its disclosed calendar cannot be bound to"
                " this trial; refusing before registration"
            )
        # (R7-P1, Codex round 7) THE FROZEN INSTANT IS THE VERIFIED ONE, by
        # construction. The loop's own `declared_close` is the only value the
        # boundary compared, so it is the only value the freeze may carry:
        # the calendar is NEVER re-read for the freeze. (R6-P1 froze the
        # instants but re-derived them with a second, unverified
        # `calendar.session_close(session)` call — a mutable calendar that
        # answers the fixture close on a session's first two reads and 16:00
        # thereafter passed the entire preflight while the THIRD, unverified
        # read froze the wrong instant; the digest covers sessions, early
        # closes and the class, never per-session method state, so nothing
        # else moved.) Storing it here also keeps the map EXACTLY the set the
        # boundary verified: a session this loop never reached is a session
        # the freeze never carries.
        decision_closes[session] = declared_close
    # (R6-P1, Codex round 6) FREEZE the verified decision instants. The loop
    # above is what VERIFIES them — one call per session, compared against
    # the stamped calendar; its refusals are unchanged. But verification
    # alone left the runtime consulting the same OVERRIDABLE method again,
    # so a stateful surface (first call right, later calls wrong) slipped
    # every comparison while the wrong instant reached build_candidates.
    # The values just verified ARE the trial's own calendar's closes; the
    # run below consumes EXACTLY those, through `_bind_decision_surface` —
    # a GENUINE instance of the surface's own class whose INSTANCE-attribute
    # decision_close answers the frozen map (refusing any session the
    # boundary never verified), shadowing the class method at every
    # `self.decision_close(...)` call site so the underlying's overridable
    # method is never consulted again (R7-P2: the R6 wrapper was not an
    # instance of the underlying's class, which broke `super()` dispatch
    # and left `__getattr__`-delegated methods bound to the underlying).
    # Byte-identical for every wired configuration: the frozen instants are
    # what an honest surface answers, so no key, hash, or stamp moves.
    bound_surface = _bind_decision_surface(surface, decision_closes)
    # (R8-P1, Codex round 8) BIND THE CALENDAR TOO. The surface freeze left
    # the runtime re-reading the ORIGINAL mutable `calendar` at its own
    # session_close sites — the filter's coherence read, the order/exit
    # stamps, the marks, the fill doors — so a calendar honest through the
    # third read (2 preflight + coherence) and lying from the fourth passed
    # every boundary guard while the fourth fed `plan_orders` the wrong
    # instant. The bound calendar below answers the VERIFIED decision
    # instants (plus one boundary-time read for every non-decision session
    # the runtime's marks and exits read), and NOTHING after the boundary
    # consults the overridable method again. Byte-identical for every wired
    # configuration: the frozen closes ARE the calendar's own answers.
    bound_calendar = _bind_decision_calendar(calendar, decision_closes)
    decision_sessions_sha256 = hashlib.sha256(
        json.dumps(
            [session.isoformat() for session in normalized_sessions],
            separators=(",", ":"),
        ).encode()
    ).hexdigest()

    split = _split_params(protocol, split_override)
    # (P1-1) the dual-calendar disclosure fires ONLY when the caller hands
    # the runner a DISTINCT execution calendar: same object (or none) keeps
    # the historical single-calendar config and payload byte-for-byte
    calendar_keys: dict[str, object] = {}
    if execution_calendar is not None and execution_calendar is not calendar:
        calendar_keys = {
            "decision_calendar": _calendar_descriptor(calendar),
            "execution_calendar": _calendar_descriptor(execution_calendar),
        }
    calendars_differ = bool(calendar_keys)
    config: dict[str, object] = {
        "runner": RUNNER_REVISION,
        "world_id": world_id,
        "arm": arm,
        "strategy": {
            **asdict(strategy_config),
            # Decimals are not JSON-native: string form is the hashed canon
            "target_abs_delta": str(strategy_config.target_abs_delta),
            "abs_delta_min": str(strategy_config.abs_delta_min),
            "abs_delta_max": str(strategy_config.abs_delta_max),
            "premium_budget_fraction": str(strategy_config.premium_budget_fraction),
        },
        "max_quote_age_seconds": DECLARED_MAX_QUOTE_AGE_SECONDS,  # owner ruling 4
        **(
            {
                # G2: the lane-2 regime and its effective flow threshold are
                # config keys — a deviation from the two-sided default (or
                # from the protocol's ratified threshold) rides the hash
                "liquidity_lane": liquidity_lane,
                "flow_min_session_volume": flow_threshold,
            }
            if liquidity_lane != 1
            else {}
        ),
        # G5: the null-score generator's REQUIRED seed is a first-class
        # config key — the declared score model's input rides the hash
        **({"score_seed": score_seed} if score_seed is not None else {}),
        # (P1-1) a dual-calendar trial binds BOTH calendar identities into
        # its config hash: which grid split the folds and which calendar
        # the fills ran on is trial identity, not an implementation detail
        **calendar_keys,
        "model_family": model_family,
        "model_sha256": model_sha256,
        "split": split,
        "decision_sessions_sha256": decision_sessions_sha256,
        "decision_session_count": len(normalized_sessions),
        "initial_cash_per_fold": str(OPTIONS_BACKTEST_INITIAL_CASH),
        "cohort_stride": COHORT_STRIDE,
        "options_manifest_hash": options_manifest_hash,
        "run_index": run_index,
    }
    trial_id = f"m3-{world_id}-{arm.lower()}-r{run_index}"
    scope = TrialScope(
        protocol_id="tree_options",
        protocol_hash=protocol_hash(protocol),
        outer_fold_id=world_id,
        target_horizon="h5",
        feature_set_id=f"{world_id}|options|ov1",
        model_family=f"options_{arm}:v1",
    )

    splitter = WalkForwardSplitter(
        calendar,
        label_horizon_sessions=split["label_horizon_sessions"],
        embargo_sessions=split["embargo_sessions"],
        val_sessions=split["val_sessions"],
        test_sessions=split["test_sessions"],
        roll_sessions=split["roll_sessions"],
        min_train_sessions=split["min_train_sessions"],
    )
    world_sessions_set = frozenset(normalized_sessions)
    folds = [
        fold
        for fold in splitter.splits(normalized_sessions)
        if fold.test_sessions <= world_sessions_set
    ]
    if not folds:
        raise ValueError(
            f"no folds for {world_id}: {len(normalized_sessions)} decision sessions "
            f"cannot carry the requested geometry {split}"
        )

    # (w5, verdict D7.1) The holdout seal, ENFORCED at registration time:
    # window A was previously declared (protocol.holdout) but consumed by
    # nothing at runtime — a mis-declared grid could put a sealed date inside
    # a REGISTERED fold's test window and leak the seal. Decision sessions
    # may never consume the seal: hard refusal BEFORE the stamp and the
    # registry write, so a leaking trial burns no config budget and never
    # exists as a record. (The EXECUTION tail after the last test session is
    # a different, disclosed matter — see the payload's holdout_seal block.)
    sealed_test_intersections = sorted(
        {
            session.isoformat()
            for fold in folds
            for session in fold.test_sessions
            if session.isoformat() in _SEALED_HOLDOUT_SESSIONS
        }
    )
    if sealed_test_intersections:
        raise ValueError(
            f"holdout seal violated for {world_id}: fold TEST sessions "
            f"{sealed_test_intersections} intersect the sealed window "
            f"{FINAL_HOLDOUT_WINDOW_ID!r} ({FINAL_HOLDOUT_SCOPE}); sealed "
            "decision sessions are never evaluable — refusing before "
            "registration"
        )

    stamp = build_stamp(
        protocol,
        trial_id=trial_id,
        config=config,
        dataset_manifest_hash=options_manifest_hash,
        repo=repo,
        allow_dirty=allow_dirty,
    )

    test_all = sorted({s for fold in folds for s in fold.test_sessions})
    train_all = sorted({s for fold in folds for s in fold.train_sessions})
    record = TrialRecord(
        trial_id=trial_id,
        created_at=clock(),
        hypothesis=hypothesis,
        git_sha=stamp.git_sha,
        config_hash=stamp.config_hash,
        dataset_manifest_hash=stamp.dataset_manifest_hash,
        train_window=(train_all[0], train_all[-1]),
        test_window=(test_all[0], test_all[-1]),
        hyperparameters={
            "arm": arm,
            "strategy": config["strategy"],
            "max_quote_age_seconds": DECLARED_MAX_QUOTE_AGE_SECONDS,
            **(
                {"liquidity_lane": liquidity_lane, "flow_min_session_volume": flow_threshold}
                if liquidity_lane != 1
                else {}
            ),
            **({"score_seed": score_seed} if score_seed is not None else {}),
            **calendar_keys,
            "model_family": model_family,
            "model_sha256": model_sha256,
            "split": split,
            "decision_sessions_sha256": decision_sessions_sha256,
            "cohort_stride": COHORT_STRIDE,
            "options_manifest_hash": options_manifest_hash,
        },
        scope_key=scope.scope_key(),
    )
    registry.register(record, scope)
    registry.mark_running(
        trial_id,
        git_sha=stamp.git_sha,
        config_hash=stamp.config_hash,
        dataset_manifest_hash=stamp.dataset_manifest_hash,
        at=clock(),
    )

    try:
        payload, stats = _execute(
            dataset=dataset,
            # (R6-P1/R7-P2) the run consumes the FROZEN boundary-verified
            # instants — the bound same-class instance, never the caller's
            # (overridable) surface. The factory already returns the
            # adapter-contract static type, so the one cast lives there.
            surface=bound_surface,
            # (R8-P1, Codex round 8) the run consumes the FROZEN calendar
            # closes too — never the caller's (overridable) calendar. The
            # ORIGINAL `calendar` above stays the object the boundary
            # verified and disclosed: the stamp, the descriptors and the
            # splitter all ran on it and are unchanged.
            calendar=bound_calendar,
            protocol=protocol,
            folds=folds,
            scored=scored,
            arm=arm,
            strategy_config=strategy_config,
            trial_id=trial_id,
            model_family=model_family,
            world_id=world_id,
            liquidity_lane=liquidity_lane,
            flow_min_session_volume=flow_threshold,
            score_seed=score_seed,
            split_label_horizon=split["label_horizon_sessions"],
            execution_calendar=execution_calendar,
            calendars_differ=calendars_differ,
        )
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        artifact_path = artifacts_dir / f"{trial_id}.json"
        write_artifact(artifact_path, payload, stamp)
        registry.complete(trial_id, metrics_uri=str(artifact_path), outcome_at=clock())
    except Exception as exc:  # a trial never ends in limbo
        if registry.status(trial_id) == "RUNNING":
            registry.fail(trial_id, f"{type(exc).__name__}: {exc}", at=clock())
        raise

    return OptionsTrialResult(
        trial_id=trial_id,
        artifact_path=artifact_path,
        n_folds=stats.n_folds,
        n_positions=stats.n_positions,
        fidelity_rho=stats.fidelity_rho,
        cohort_ic_mean=stats.stride4_cohort_ic_mean,
        cohort_ic_sd=stats.stride4_cohort_ic_sd,
        cohort_t=stats.stride4_cohort_t,
    )


def _execute(
    *,
    dataset: PointInTimeDataset,
    surface: OptionPitSurface,
    calendar: SessionCalendar,
    protocol: ResearchProtocol,
    folds: list[Fold],
    scored: Sequence[ScoredLabel],
    arm: Arm,
    strategy_config: OptionsStrategyConfig,
    trial_id: str,
    model_family: str,
    world_id: str,
    liquidity_lane: int = 1,
    flow_min_session_volume: int | None = None,
    score_seed: str | None = None,
    split_label_horizon: int = 5,
    execution_calendar: SessionCalendar | None = None,
    calendars_differ: bool = False,
) -> tuple[dict[str, object], _ODStats]:
    if liquidity_lane == 2:
        # G2: the Massive derived (vwap) lane's ratified regime — OI and the
        # spread dropped with disclosure, the session's traded contracts as
        # the liquidity term, model-derived |delta| accepted
        candidate_filter = CandidateFilter.from_protocol_volume_flow(
            calendar, protocol, flow_min_session_volume=flow_min_session_volume
        )
    else:
        candidate_filter = CandidateFilter.from_protocol(calendar, protocol)
    world_last_session = max(bar.session for bar in dataset.bars)
    scored_by_session: dict[date, list[ScoredLabel]] = {}
    for row in scored:
        scored_by_session.setdefault(row.session, []).append(row)

    per_fold: list[dict[str, object]] = []
    all_positions: list[dict[str, object]] = []
    all_fills: list[dict[str, object]] = []
    backtest_returns: list[float] = []
    backtest_turnovers: list[float] = []
    backtest_hits: list[bool] = []
    aggregated_counters: dict[str, int] = {}
    aggregated_rejections: dict[str, dict[str, int]] = {}
    rule_histogram: dict[str, dict[str, int]] = {}
    sealed_tail_union: set[str] = set()

    for fold in folds:
        fold_scored = [
            row
            for session in sorted(fold.test_sessions)
            for row in scored_by_session.get(session, ())
        ]
        result, execution_tail = _fold_backtest(
            fold=fold,
            fold_scored=fold_scored,
            calendar=calendar,
            surface=surface,
            dataset=dataset,
            candidate_filter=candidate_filter,
            config=strategy_config,
            arm=arm,
            world_last_session=world_last_session,
            execution_calendar=execution_calendar,
        )
        test_window = sorted(fold.test_sessions)
        sealed_tail_sessions = sorted(
            session.isoformat()
            for session in execution_tail
            if session.isoformat() in _SEALED_HOLDOUT_SESSIONS
        )
        sealed_tail_union.update(sealed_tail_sessions)
        per_fold.append(
            {
                "fold_id": fold.fold_id,
                "n_test_sessions": len(fold.test_sessions),
                # (w5) the fold's decision boundary + its disclosed
                # execution-tail consumption of the sealed window
                "test_window": {
                    "start": test_window[0].isoformat(),
                    "end": test_window[-1].isoformat(),
                },
                "execution_tail_start": (execution_tail[0].isoformat() if execution_tail else None),
                "execution_tail_session_count": len(execution_tail),
                "holdout_seal_consumed": bool(sealed_tail_sessions),
                "holdout_seal_consumed_sessions": sealed_tail_sessions,
                "n_positions": len(result.positions),
                "n_fills": len(result.fills),
                "fills_buy": sum(1 for f in result.fills if f.side == "buy"),
                "fills_sell": sum(1 for f in result.fills if f.side == "sell"),
                "n_sessions_evaluated": len(result.sessions),
                "conservation_checks": result.counters.conservation_checks,
                "force_closes": result.counters.force_closes,
                "early_exercises": result.counters.early_exercises,
                "expiries": result.counters.expiries,
                "terminals": result.counters.terminals,
                "total_return": result.summary.total_return,
                # G3 extension (verdict D6): the fold's OWN fill fees, so a
                # cost-floor decomposition is computable from the artifact
                "fees_total": str(sum((fill.fees for fill in result.fills), Decimal("0"))),
                # G3: the fold's own session-return series and equity
                # endpoints — computed today but discarded; stamped so the
                # artifact alone re-derives every fold statistic
                "session_returns": list(result.summary.session_returns),
                "equity_start": (str(result.equities[0]) if result.equities else None),
                "equity_end": (str(result.equities[-1]) if result.equities else None),
            }
        )
        fold_positions = _position_payloads(
            result,
            calendar=calendar,
            label_horizon_sessions=split_label_horizon,
        )
        for position_row in fold_positions:
            # (w5, verdict D7.2) per-position disclosure: an exit/settlement
            # executed inside the sealed window, and a label window that
            # overlaps it — the holdout-overlapping sensitivity subset every
            # verdict must be able to name
            position_row["exit_in_holdout_seal"] = bool(position_row["exit_session"]) and (
                position_row["exit_session"] in _SEALED_HOLDOUT_SESSIONS
            )
            window = position_row["label_window"]
            position_row["label_window_touches_holdout_seal"] = (
                _window_touches_seal(window) if window else False  # type: ignore[arg-type]
            )
        all_positions.extend(fold_positions)
        all_fills.extend(_fill_log(result))
        backtest_returns.extend(result.summary.session_returns)
        backtest_turnovers.extend(result.turnovers)
        backtest_hits.extend(result.label_hits)
        counters = result.counters
        for key in (
            "not_evaluable_candidates",
            "failed_candidates",
            "no_in_band_expiry",
            "no_in_band_strike",
            "excluded_pending_action",
            "entries_cancelled",
            "entries_skipped_open",
            "force_closes",
            "early_exercises",
            "expiries",
            "terminals",
            "exit_retries",
            "mark_misses",
            "conservation_checks",
        ):
            aggregated_counters[key] = aggregated_counters.get(key, 0) + getattr(counters, key)
        for bucket_name in (
            "entry_fill_rejections",
            "exit_fill_rejections",
            "force_close_rejections",
        ):
            merged = aggregated_rejections.setdefault(bucket_name, {})
            for code, n in getattr(counters, bucket_name).items():
                merged[code] = merged.get(code, 0) + n
        for (rule, status), n in counters.rule_histogram.items():
            rule_histogram.setdefault(rule, {}).setdefault(status, 0)
            rule_histogram[rule][status] += n

    # ---- OD statistics from the stamped per-position rows ------------------
    closed = [
        p for p in all_positions if p["exit_kind"] is not None and p["premium_return"] is not None
    ]
    fidelity_pairs = [
        (float(p["signed_premium_return"]), float(p["label"]))  # type: ignore[arg-type]
        for p in closed
        if p["label"] is not None
    ]
    fidelity_rho = _spearman([a for a, _ in fidelity_pairs], [b for _, b in fidelity_pairs])

    stride4, cohort_ics, cohort_counts = _cohort_series(closed)
    stride4_ics = [ic for _, ic in stride4]
    stride4_sd = statistics.stdev(stride4_ics) if len(stride4_ics) >= 2 else None
    stride4_t = _t_statistic(stride4_ics)
    autocorr = _lag1_autocorrelation(cohort_ics)

    aggregate = backtest_summary(backtest_returns, backtest_turnovers, backtest_hits)
    stats = _ODStats(
        n_folds=len(folds),
        n_positions=len(all_positions),
        fidelity_rho=fidelity_rho,
        stride4_cohort_ic_mean=(sum(stride4_ics) / len(stride4_ics) if stride4_ics else None),
        stride4_cohort_ic_sd=stride4_sd,
        stride4_cohort_t=stride4_t,
    )
    # (w5, verdict D7.2) The seal-consumption disclosure every artifact
    # carries: window A's decision sessions are never evaluated (the D7.1
    # refusal above guarantees the zero), and the execution-tail consumption
    # — exits, settlements and marks after the last test session — is
    # DISCLOSED here, never banned.
    # (P2-6, Codex round 1) DISCLOSURE HONESTY: the seal is DECLARED-scoped
    # to lane-2 evaluation folds, but the refusal this runner enforces is
    # UNCONDITIONAL (strictly safer — a sealed date can never enter a
    # registered fold on ANY lane). The block therefore states the declared
    # scope verbatim, the ARTIFACT's own liquidity_lane, and an explicit
    # `applied` field, so a lane-1 artifact can never again be read as
    # claiming the seal was lane-1-scoped. Additive keys only.
    seal_disclosure: dict[str, object] = {
        "window_id": FINAL_HOLDOUT_WINDOW_ID,
        "scope": FINAL_HOLDOUT_SCOPE,
        "declared_scope": FINAL_HOLDOUT_SCOPE,
        "applied": f"unconditional-refusal (declared scope: {FINAL_HOLDOUT_SCOPE})",
        "liquidity_lane": liquidity_lane,
        "sealed_dates": sorted(_SEALED_HOLDOUT_SESSIONS),
        "fold_test_window_intersections": 0,
        "folds_with_sealed_execution_tail": sum(
            1 for fold in per_fold if fold["holdout_seal_consumed"]
        ),
        "sealed_execution_tail_sessions": sorted(sealed_tail_union),
        "positions_exiting_in_seal": sum(1 for row in all_positions if row["exit_in_holdout_seal"]),
        "positions_with_seal_overlapping_label_window": sum(
            1 for row in all_positions if row["label_window_touches_holdout_seal"]
        ),
    }
    payload: dict[str, object] = {
        "runner": RUNNER_REVISION,
        "world_id": world_id,
        "arm": arm,
        "model_family": model_family,
        "max_quote_age_seconds": DECLARED_MAX_QUOTE_AGE_SECONDS,
        "cohort_stride": COHORT_STRIDE,
        "n_folds": len(folds),
        "world_last_session": world_last_session.isoformat(),
        "per_fold": per_fold,
        "pooled": {
            "n_positions": len(all_positions),
            "n_closed": len(closed),
            "fidelity_rho": fidelity_rho,
            "fidelity_n": len(fidelity_pairs),
            "n_cohorts": len(cohort_ics),
            "mean_cohort_positions": (
                sum(cohort_counts) / len(cohort_counts) if cohort_counts else None
            ),
            "cohort_ic_mean": (sum(cohort_ics) / len(cohort_ics) if cohort_ics else None),
            "cohort_ic_sd": statistics.stdev(cohort_ics) if len(cohort_ics) >= 2 else None,
            "cohort_ic_autocorr_lag1": autocorr,
            "n_stride4_cohorts": len(stride4_ics),
            "stride4_cohort_ic_mean": (
                sum(stride4_ics) / len(stride4_ics) if stride4_ics else None
            ),
            "stride4_cohort_ic_sd": stride4_sd,
            "stride4_cohort_t": stride4_t,
            # the raw series ride in the stamp so the sealed gate re-derives
            # t (and the two-world pooled t, criterion 7) from the artifact
            # alone instead of trusting a pre-computed scalar
            "stride4_cohort_ics": stride4_ics,
            "cohort_ics": cohort_ics,
            "positions": all_positions,
        },
        "fills_log": all_fills,
        "counters": {
            **aggregated_counters,
            "rejections": aggregated_rejections,
            "rule_histogram": rule_histogram,
        },
        # (w5, verdict D7.2) the holdout seal's disclosure block
        "holdout_seal": seal_disclosure,
        "backtest": {
            # G3 extension (w4): derived from the dataset's identity, never
            # hardcoded — synthetic-sourced worlds keep the historical token
            # byte-identically; every other world (the lane-2 one included)
            # is stamped with its own snapshot identity
            "dataset_provenance": _dataset_provenance(
                world_id, frozenset({bar.source for bar in dataset.bars})
            ),
            "initial_cash_per_fold": str(OPTIONS_BACKTEST_INITIAL_CASH),
            "n_session_returns": len(aggregate.session_returns),
            "total_return": aggregate.total_return,
            "mean_turnover": aggregate.mean_turnover,
            "hit_rate": aggregate.hit_rate,
            # G3: the pooled series itself (the folds concatenate — each
            # restarts from fresh cash), so the artifact alone re-derives
            # the product identity behind n_session_returns/total_return
            "session_returns": list(aggregate.session_returns),
        },
    }
    if liquidity_lane != 1:
        # G2: the regime deviation is stamped in the payload too, so the
        # artifact alone discloses which filter produced its audit rows
        payload["liquidity_lane"] = liquidity_lane
        payload["flow_min_session_volume"] = flow_min_session_volume
    if score_seed is not None:
        # G5: the declared score model's seed is stamped in the payload so
        # the artifact discloses it without re-hashing the config
        payload["score_seed"] = score_seed
    if calendars_differ and execution_calendar is not None:
        # (P1-1) both calendar identities are DISCLOSED: which grid split
        # the folds and which calendar the fill engine's session checks ran
        # on — additive keys, absent when the two calendars coincide
        payload["decision_calendar"] = _calendar_descriptor(calendar)
        payload["execution_calendar"] = _calendar_descriptor(execution_calendar)
    return payload, stats
