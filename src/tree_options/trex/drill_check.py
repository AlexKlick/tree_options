"""Stage-B exit-drill readiness gates (READ-ONLY, prints, never mutates).

The Stage-B runbook (``~/.local/state/trex-canary/run-log.md``) re-checks
six gates before the drill entry goes out. This module is that re-check as
one command: ``python -m tree_options.trex.desk_cli drill-check``.

- **G2** the desk is alive: the user unit is active, ``owner.json`` names a
  FRESH epoch (started after the nightly gateway logoff, 17:45
  America/Denver — a new epoch each morning) with a live pid, the book
  heartbeats inside ``HEARTBEAT_MAX_AGE_S``, ``monitor.json`` is fresh +
  connected + clean, ``trex.exit_watch`` (the same ``--dry-run``
  verdict the runbook names) says the book is guarded, AND the ACCOUNT's
  exposure is stated: the desk book and every other book under the same
  scan root (the legacy trex books), with their leg/structure counts, max
  loss and exit deadlines. A flat desk book is never reported as a flat
  account, and unreadable books say they are unreadable.
  ``--max-account-open-loss USD`` is the operator's opt-in rail over that
  account total; unset it is printed and never enforced.
- **G3** the gateway is settled-ok and the drill is > 30 min from the
  Sunday 12:00 ET cold-restart instant.
- **G4** a mandate is granted for the CURRENT owner epoch (read the way
  ``supervised status`` reads it); on any miss the exact grant command is
  printed with the live epoch (and the live profile digest) filled in.
- **G5** the quote rail on the candidate strikes: two-sided quotes with
  spread <= 10% of mid and OI >= 100 WHEREVER the venue reports OI,
  probed the way the Monday quote probe probed (delayed paper quotes, a
  read-only market-data subscription, places nothing). Degrades to
  SKIPPED — honestly, never as a pass — outside RTH, without candidate
  strikes, or when the probe surface is unavailable, and when the paper
  API reports no open interest (it does not: verified 2026-09-29), in
  which case the operator verifies OI in TWS; a SKIPPED gate does not
  fail the exit code, a measured miss always does.
- **G6** the halt/inspect/resume rehearsal is operator knowledge: reported
  as MANUAL, never machine-passed.

Exit codes: 0 all gates pass-or-skipped, 1 any NO-GO, 2 usage error.
Nothing here places, cancels or routes anything: every external surface
(the unit check, the pid probe, the clock, the quote probe) is read or
injected, and the one IBKR touch is a read-only market-data subscription
on a clientId no desk owns.
"""

from __future__ import annotations

import argparse
import json
import math
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from tree_options.desk import book as desk_book
from tree_options.desk.book import AccountExposure, BookSlice
from tree_options.time import calendar_days, weekday_index
from tree_options.trex import exit_watch, gateway_watch
from tree_options.trex.alert_policy import market_hours, span_label, urgency
from tree_options.trex.clock import ET
from tree_options.trex.desk_cli import DEFAULT_ACCOUNT
from tree_options.trex.desk_runtime import HEARTBEAT_MAX_AGE_S, DeskPaths
from tree_options.trex.grant_policy import (
    DEFAULT_TTL_S,
    daily_grant,
    grant_command,
    load_windows,
    windows_path,
)
from tree_options.trex.supervised import SupervisedPaths

DESK_UNIT = "trex-desk.service"
#: the nightly IB Gateway logoff (runbook G2: "new epoch each morning")
DENVER_TZ = ZoneInfo("America/Denver")
LOGOFF_HOUR, LOGOFF_MINUTE = 17, 45
#: how close to the Sunday 12:00 ET cold restart is too close to drill
COLD_RESTART_MARGIN_S = 30 * 60
#: the runbook's liquidity rail
MIN_OPEN_INTEREST = 100
MAX_SPREAD_PCT_OF_MID = Decimal("10")
#: a probe clientId that is neither the supervised desk (83) nor legacy (77)
PROBE_CLIENT_ID = 84
PROBE_HOST = "127.0.0.1"
PROBE_PORT = 4002
PROBE_SETTLE_S = 6.0

GO = "GO"
NO_GO = "NO-GO"
SKIPPED = "SKIPPED"
MANUAL = "MANUAL"

# exit_watch's own readers, aliased so the watchdog's staleness rules (not a
# copy of them) decide what "fresh" means here too
_gateway_state = exit_watch._gateway
_iso_to_epoch = exit_watch._epoch
_num_or_none = exit_watch._num


def systemd_is_active(unit: str) -> str | None:
    """``systemctl --user is-active <unit>`` (None when the call fails)."""
    return exit_watch.unit_state(unit)


def pid_is_alive(pid: int) -> bool:
    """Signal-0 liveness; EPERM means the process exists (not ours to signal)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def now_et() -> datetime:
    return datetime.now(ET)


# ------------------------------------------------------------------ G5 probe


@dataclass(frozen=True)
class StrikePair:
    """The candidate legs the runbook's quote rail is checked on."""

    underlying: str
    buy_strike: Decimal
    sell_strike: Decimal
    expiry: date


@dataclass(frozen=True)
class LegQuote:
    strike: Decimal
    bid: Decimal | None
    ask: Decimal | None
    open_interest: int | None


@dataclass(frozen=True)
class ProbeOutcome:
    available: bool
    reason: str = ""
    legs: tuple[LegQuote, ...] = ()


QuoteProbe = Callable[[StrikePair], ProbeOutcome]


def _dec(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if not math.isfinite(number) else Decimal(str(number))


def _oi(value: Any) -> int | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return int(number) if math.isfinite(number) else None


def live_quote_probe(pair: StrikePair) -> ProbeOutcome:
    """The Monday quote probe's surface (read-only market data, places nothing).

    Delayed quotes (type 3) on the paper gateway, one clientId that belongs
    to no desk: qualify both legs, subscribe, sample, unsubscribe. Any
    failure is an unavailable probe (G5 skips), never a silent zero."""
    from ib_async import IB, Option  # lazy: the optional trex group

    ib = IB()
    tickers: list[tuple[Decimal, Any, Any]] = []
    try:
        ib.connect(PROBE_HOST, PROBE_PORT, clientId=PROBE_CLIENT_ID, timeout=20)
        ib.reqMarketDataType(3)
        month = pair.expiry.strftime("%Y%m%d")
        for strike in (pair.buy_strike, pair.sell_strike):
            contract = Option(symbol=pair.underlying, lastTradeDateOrContractMonth=month,
                              strike=float(strike), right="P", exchange="SMART",
                              tradingClass=pair.underlying)
            ib.qualifyContracts(contract)
            tickers.append((strike, contract, ib.reqMktData(contract, "", False, False)))
        ib.sleep(PROBE_SETTLE_S)
        legs = tuple(
            LegQuote(strike=strike, bid=_dec(ticker.bid), ask=_dec(ticker.ask),
                     open_interest=_oi(getattr(ticker, "openInterest", None)))
            for strike, _contract, ticker in tickers)
        return ProbeOutcome(available=True, legs=legs)
    except Exception as error:
        return ProbeOutcome(available=False, reason=type(error).__name__)
    finally:
        for _strike, contract, _ticker in tickers:
            try:
                ib.cancelMktData(contract)
            except Exception:
                pass
        try:
            ib.disconnect()
        except Exception:
            pass


def leg_verdict(leg: LegQuote) -> tuple[bool, str]:
    """(ok, evidence) for one leg's measurable rail: two-sided and
    spread <= 10% of mid. OI is handled by the caller: the paper API
    often does not report it (verified 2026-09-29: no openInterest tick,
    OPTION_OPEN_INTEREST history and regulatory snapshots both refused),
    and a venue that cannot report a datum must neither pass nor fail
    its gate silently."""
    if leg.bid is None or leg.ask is None or leg.bid <= 0 or leg.ask <= 0 \
            or leg.ask < leg.bid:
        return False, f"{leg.strike}: not two-sided (bid={leg.bid}, ask={leg.ask})"
    mid = (leg.bid + leg.ask) / 2
    spread_pct = (leg.ask - leg.bid) / mid * Decimal("100")
    ok = spread_pct <= MAX_SPREAD_PCT_OF_MID
    evidence = (f"{leg.strike} {leg.bid}/{leg.ask} spread "
                f"{spread_pct.quantize(Decimal('0.1'))}% of mid"
                + ("" if ok else f" > {MAX_SPREAD_PCT_OF_MID}%"))
    return ok, evidence


def quote_gate(legs: tuple[LegQuote, ...]) -> Gate:
    """The runbook's G5 over measured legs. Enforced hard: two-sided
    quotes, spread <= 10% of mid, and OI >= 100 WHEREVER OI is reported.
    When the venue reports no OI the gate is SKIPPED with the measured
    spread evidence and the OI left to the operator (TWS) — an
    unverifiable datum never fails the drill and never passes itself."""
    problems: list[str] = []
    segments: list[str] = []
    unverified: list[str] = []
    for leg in legs:
        ok, spread = leg_verdict(leg)
        if leg.open_interest is None:
            oi = "OI not reported"
            unverified.append(str(leg.strike))
        else:
            oi = f"OI {leg.open_interest}"
            if leg.open_interest < MIN_OPEN_INTEREST:
                problems.append(f"{leg.strike} OI {leg.open_interest} < "
                                f"{MIN_OPEN_INTEREST}")
        segments.append(f"{spread}, {oi}")
        if not ok:
            problems.append(spread)
    detail = "; ".join(segments)
    if problems:
        return Gate("G5", "quotes", NO_GO, f"{detail}; " + "; ".join(problems))
    if unverified:
        return Gate("G5", "quotes", SKIPPED,
                    f"{detail}; OI not reported by the paper API for "
                    f"{', '.join(unverified)} (spread ok): verify OI >= "
                    f"{MIN_OPEN_INTEREST} in TWS before entry")
    return Gate("G5", "quotes", GO, detail)


# ------------------------------------------------------------------ gates


@dataclass(frozen=True)
class Gate:
    gate: str  # G2..G6
    name: str
    verdict: str  # GO | NO-GO | SKIPPED | MANUAL
    evidence: str
    hint: str = ""  # e.g. the exact grant command, printed indented

    @property
    def line(self) -> str:
        out = f"{self.gate} {self.name}: {self.verdict} ({self.evidence})"
        if self.hint:
            out += "\n    " + self.hint
        return out


@dataclass(frozen=True)
class Deps:
    """Every external surface, injectable so tests touch nothing real."""

    unit_state: Callable[[str], str | None] = systemd_is_active
    pid_alive: Callable[[int], bool] = pid_is_alive
    clock: Callable[[], datetime] = now_et
    probe: QuoteProbe = live_quote_probe


@dataclass
class DeskState:
    """What G2 learned about the desk (G4 needs the epoch even when G2 red)."""

    epoch: str | None = None
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


def last_nightly_logoff(now: datetime) -> datetime:
    """The most recent 17:45 America/Denver instant at or before ``now``.

    The gateway logs off nightly at 17:45 Denver time; an owner epoch that
    predates the latest logoff is yesterday's session (runbook G2: the
    epoch must be fresh, a new one each morning)."""
    local = now.astimezone(DENVER_TZ)
    candidate = local.replace(hour=LOGOFF_HOUR, minute=LOGOFF_MINUTE,
                              second=0, microsecond=0)
    if candidate > local:
        candidate -= calendar_days(1)
    return candidate.astimezone(ET)


def next_sunday_noon_et(now: datetime) -> datetime:
    """The next Sunday 12:00 ET strictly after ``now`` (the cold restart)."""
    local = now.astimezone(ET)
    days_ahead = (6 - weekday_index(local)) % 7  # Monday=0 .. Sunday=6
    candidate = (local + calendar_days(days_ahead)).replace(
        hour=12, minute=0, second=0, microsecond=0)
    if candidate <= local:
        candidate += calendar_days(7)
    return candidate


def _span_days(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes >= 24 * 60:
        return f"{minutes // (24 * 60)}d {(minutes // 60) % 24}h"
    return span_label(seconds)


# ------------------------------------------------------------- account truth

#: the G2 evidence segment: the ACCOUNT's exposure, not just the desk book's
_ACCOUNT_HEAD = "account:"


def _legs_phrase(slice_: BookSlice) -> str:
    legs = slice_.legs
    if legs is None:
        return "leg count unknown"  # a structure did not validate: never say 0
    return f"{legs} leg{'' if legs == 1 else 's'}"


def _slice_phrase(slice_: BookSlice) -> str:
    """``no exposure`` or ``N legs / M structures - $X max loss``."""
    if slice_.structures == 0:
        return "no exposure"
    loss = slice_.max_loss_usd
    money = "uncountable" if loss is None else f"${loss:.2f}"
    return f"{_legs_phrase(slice_)} / {slice_.structures} structure" \
           f"{'' if slice_.structures == 1 else 's'} - {money} max loss"


def _slice_detail(slice_: BookSlice) -> str:
    """The structures with their time stops, soonest deadline first, a book
    named once per book: ``legacy:putspread-20260922/nvda-oct @2026-10-09,
    nvda-nov @2026-11-06``."""
    parts: list[str] = []
    source: str | None = None
    for position in sorted(slice_.positions, key=lambda p: (
            p.exit_deadline or date.max, p.source, p.id)):
        label = position.id if position.source != source \
            else position.id.split("/", 1)[-1]
        source = position.source
        deadline = position.exit_deadline
        parts.append(f"{label} @{'exit deadline unknown' if deadline is None
                                else deadline.isoformat()}")
    return ", ".join(parts)


def account_line(exposure: AccountExposure) -> str:
    """G2's account segment. The desk book and the rest of the ACCOUNT are
    named separately and never one for the other: a flat desk book on an
    account a legacy book still holds is the state this exists to make
    visible. An uncountable book says so and never reads as flat."""
    outside = exposure.outside
    if not exposure.countable:
        counted = outside.structures + exposure.desk.structures
        reason = exposure.problems[0] if exposure.problems else \
            "a position has no countable max loss"
        return (f"{_ACCOUNT_HEAD} exposure NOT COUNTABLE - {counted} structure"
                f"{'' if counted == 1 else 's'} read but the books are not fully "
                f"readable ({reason}); no total can be claimed, least of all none")
    if outside.structures == 0:
        head = f"{_ACCOUNT_HEAD} no positions outside the desk book"
    else:
        head = (f"{_ACCOUNT_HEAD} {_slice_phrase(outside)} outside the desk book"
                f" ({_slice_detail(outside)})")
    return f"{head}; desk book: {_slice_phrase(exposure.desk)}"


def account_rail(exposure: AccountExposure, rail: Decimal | None) -> str | None:
    """The G2 problem a ``--max-account-open-loss`` rail produces, else None.

    UNSET is today's behavior exactly (this drill's default can never be
    blocked by a rail the operator did not set). SET, the total must be
    countable and at or under it: an account whose books cannot be read
    against a rail the operator DID set fails closed, never passes.
    Exactly at the rail passes (only exceeding it is a NO-GO)."""
    if rail is None:
        return None
    if not exposure.countable:
        return (f"account open loss NOT COUNTABLE against the "
                f"--max-account-open-loss rail ${rail:.2f} (fail closed)")
    outside = exposure.outside
    total = (outside.max_loss_usd or Decimal(0)) + (exposure.desk.max_loss_usd or Decimal(0))
    if total > rail:
        return (f"account open loss ${total:.2f} exceeds the "
                f"--max-account-open-loss rail ${rail:.2f}")
    return None


def account_tail(exposure: AccountExposure, rail: Decimal | None) -> str:
    """The account total on the final summary line, set or not."""
    if not exposure.countable:
        counted = exposure.outside.structures + exposure.desk.structures
        return (f"account open loss NOT COUNTABLE ({counted} structure"
                f"{'' if counted == 1 else 's'} read, {len(exposure.problems)} book "
                f"problem{'' if len(exposure.problems) == 1 else 's'})"
                + (f" against a ${rail:.2f} rail" if rail is not None else ""))
    total = (exposure.outside.max_loss_usd or Decimal(0)) \
        + (exposure.desk.max_loss_usd or Decimal(0))
    deadline = exposure.outside.earliest_exit_deadline
    when = f", earliest exit {deadline.isoformat()}" if deadline is not None else ""
    verdict = "rail UNSET (informational, no exposure gate)" if rail is None \
        else (f"rail ${rail:.2f} EXCEEDED" if total > rail
              else f"within the ${rail:.2f} rail")
    return (f"account open loss ${total:.2f} account-wide "
            f"({exposure.outside.structures} structure"
            f"{'' if exposure.outside.structures == 1 else 's'} outside the desk book"
            f"{when}; desk book: {_slice_phrase(exposure.desk)}) - {verdict}")


def _gate_desk(paths: DeskPaths, deps: Deps, *, now: datetime, root: Path,
               exit_watch_state: Path, gateway_state: Path,
               account: AccountExposure, rail: Decimal | None) -> tuple[Gate, DeskState]:
    """G2: the desk unit, epoch, heartbeat, monitor.json, exit_watch, and
    the ACCOUNT's exposure (the desk book AND the other books under the
    same scan root: the desk book alone is not the account)."""
    state = DeskState()
    unit = deps.unit_state(DESK_UNIT)
    if unit == "active":
        state.notes.append(f"unit {DESK_UNIT} active")
    else:
        state.problems.append(f"unit {DESK_UNIT} {unit or 'unknown (systemctl failed)'}")

    owner = _read_json(paths.root / "owner.json")
    epoch = owner.get("owner_epoch") if owner else None
    started, pid = owner.get("started_at") if owner else None, \
        owner.get("pid") if owner else None
    if not isinstance(epoch, str) or not epoch:
        state.problems.append("owner.json has no owner_epoch")
    else:
        state.epoch = epoch
        started_at = _iso_to_epoch(started) if isinstance(started, str) else None
        if started_at is None:
            state.problems.append(f"epoch {epoch}: started_at unreadable")
        else:
            started_dt = datetime.fromtimestamp(started_at, ET)
            fresh_since = last_nightly_logoff(now)
            if started_dt <= fresh_since:
                state.problems.append(
                    f"epoch {epoch} stale: started {started_dt:%a %H:%M ET}, before the "
                    f"nightly gateway logoff {fresh_since:%a %H:%M ET}")
            else:
                state.notes.append(
                    f"epoch {epoch} (started {started_dt:%a %H:%M ET}, fresh)")
        if not isinstance(pid, int):
            state.problems.append(f"epoch {epoch}: owner.json has no pid")
        elif deps.pid_alive(pid):
            state.notes.append(f"pid {pid} alive")
        else:
            state.problems.append(f"epoch {epoch}: pid {pid} is not running")

    book = _read_json(paths.book())
    heartbeat = _iso_to_epoch((book or {}).get("heartbeat"))
    age = None if heartbeat is None else now.timestamp() - heartbeat
    if age is not None and 0 <= age <= HEARTBEAT_MAX_AGE_S:
        state.notes.append(f"heartbeat {int(age)}s")
    else:
        state.problems.append(
            "heartbeat " + ("absent" if age is None else f"stale {span_label(age)}"))

    monitor = _read_json(paths.health())
    at = _num_or_none(monitor.get("at")) if monitor else None
    if monitor is None or at is None:
        state.problems.append("monitor.json absent/unreadable")
    elif now.timestamp() - at > exit_watch.HEALTH_STALE_S:
        state.problems.append(
            f"monitor.json stale ({span_label(now.timestamp() - at)} old)")
    else:
        state.notes.append(f"monitor {int(now.timestamp() - at)}s old")
        if monitor.get("connected") is not True:
            state.problems.append("monitor.json not connected")
        failures = monitor.get("tick_failures")
        if failures not in (None, 0):
            state.problems.append(f"monitor.json tick_failures={failures}")

    # the runbook's own verdict: exit_watch --dry-run over this root. dry_run
    # writes no state file and sends no push: the read is the whole effect.
    try:
        verdict = exit_watch.watch_once(
            exit_watch_state, root=root, gateway_state=gateway_state,
            notify=lambda *_args, **_kw: False, now=now.timestamp(),
            urgency=urgency(now.timestamp(), exposed=False, quiet=None),
            unit_state=deps.unit_state(exit_watch.UNIT), dry_run=True)
        status = str(verdict.get("status"))
        rows = [r for r in verdict.get("books", []) if isinstance(r, dict)]
        row = next((r for r in rows if r.get("plan") == paths.root.name), None)
        # the other books under the SAME scan root are exposure on the same
        # account: naming only the desk row used to print "no exposure" while
        # a legacy book held legs (verified live 2026-09-29).
        others = sorted(str(r.get("plan")) for r in rows
                        if r.get("plan") != paths.root.name)
        if status in exit_watch.HEALTHY:
            guarded = "desk book guarded" if row is not None else "desk book: no exposure"
            if others:
                guarded += (f"; other book(s) under the scan root with exposure: "
                            f"{', '.join(others)}")
            state.notes.append(f"exit_watch {status} ({guarded})")
        else:
            detail = row.get("detail") if row is not None else verdict.get("detail")
            state.problems.append(f"exit_watch {status}: {detail}")
            if row is None and others:
                state.problems[-1] += (f" (the desk book has no row here; books under "
                                       f"the scan root: {', '.join(others)})")
    except (OSError, ValueError, KeyError, TypeError) as error:
        state.problems.append(f"exit_watch unreadable: {type(error).__name__}")

    # the account segment is EVIDENCE, never a reason to hide other evidence:
    # it is printed on a red G2 too (an operator reading a NO-GO still has to
    # see what the account holds, and an uncountable book must never go quiet).
    segment = account_line(account)
    breach = account_rail(account, rail)
    if breach is not None:
        state.problems.append(breach)
    if state.problems:
        state.problems.append(segment)
        evidence = "; ".join(state.problems)
    else:
        state.notes.append(segment)
        evidence = "; ".join(state.notes)
    return Gate("G2", "desk", NO_GO if state.problems else GO, evidence), state


def _gate_gateway(*, now: datetime, gateway_state: Path) -> Gate:
    """G3: gateway settled-ok and clear of the Sunday cold-restart window."""
    problems: list[str] = []
    notes: list[str] = []
    status, since = _gateway_state(gateway_state, now.timestamp())
    if status is None:
        problems.append("gateway watch silent or stale (>5 min)")
    else:
        if status != "ok":
            detail = (_read_json(gateway_state) or {}).get("detail", "not ok")
            problems.append(f"gateway {status}: {detail}")
        elif since is not None and now.timestamp() - since < exit_watch.GATEWAY_SETTLE_S:
            problems.append(
                f"gateway ok but not settled (recovered "
                f"{span_label(now.timestamp() - since)} ago, needs "
                f"{exit_watch.GATEWAY_SETTLE_S:.0f}s)")
        else:
            settled = f", settled {span_label(now.timestamp() - since)}" \
                if since is not None else ""
            notes.append(f"gateway ok{settled}")

    restart = next_sunday_noon_et(now)
    distance = (restart - now).total_seconds()
    if distance < COLD_RESTART_MARGIN_S:
        problems.append(f"Sunday cold-restart window in {_span_days(distance)} "
                        f"(< {COLD_RESTART_MARGIN_S // 60} min)")
    else:
        notes.append(f"cold-restart in {_span_days(distance)}")

    return Gate("G3", "gateway", NO_GO if problems else GO,
                "; ".join(problems) if problems else "; ".join(notes))


def _grant_hint(paths: DeskPaths, epoch: str | None, *, now: datetime) -> str:
    """The exact grant command (grant_policy's builder) with the live epoch."""
    try:
        snapshot = windows_path(paths)
        windows = load_windows(snapshot) if snapshot.exists() else ()
    except (OSError, ValueError, KeyError):
        windows = ()
    plan = daily_grant(windows)
    digest = "<digest from: python -m tree_options.trex.supervised_desk profile-digest>"
    try:
        from tree_options.trex.supervised_desk import load_profile

        _profile, digest = load_profile(paths.root / "profile.json")
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return "grant: " + grant_command(
        plan, account=DEFAULT_ACCOUNT, owner_epoch=epoch or "<epoch from owner.json>",
        strategy="operational-canary/1", profile_digest=digest,
        ttl_seconds=DEFAULT_TTL_S, granted_by="operator-terminal")


def _gate_mandate(supervised: SupervisedPaths, desk: DeskState, *,
                  now: datetime, paths: DeskPaths) -> Gate:
    """G4: a mandate for the CURRENT epoch, read the way ``supervised status``
    reads it (the same mandate.json / mandate.revoked.json files)."""
    epoch = desk.epoch
    if epoch is None:
        return Gate("G4", "mandate", NO_GO,
                    "no owner epoch (G2 failed); a grant must bind the live epoch",
                    hint=_grant_hint(paths, None, now=now))
    revoked = supervised.mandate_revoked().exists()
    raw = _read_json(supervised.mandate())
    if revoked or raw is None:
        return Gate("G4", "mandate", NO_GO,
                    f"mandate {'revoked' if revoked else 'absent'} (epoch {epoch})",
                    hint=_grant_hint(paths, epoch, now=now))
    from tree_options.trex.supervised import SupervisedMandate

    try:
        mandate = SupervisedMandate.model_validate(raw)
    except ValueError:
        return Gate("G4", "mandate", NO_GO, f"mandate unreadable (epoch {epoch})",
                    hint=_grant_hint(paths, epoch, now=now))
    if mandate.owner_epoch != epoch:
        return Gate("G4", "mandate", NO_GO,
                    f"mandate binds OLD epoch {mandate.owner_epoch}; current epoch {epoch}",
                    hint=_grant_hint(paths, epoch, now=now))
    if mandate.expired_at(now):
        return Gate("G4", "mandate", NO_GO,
                    f"mandate {mandate.mandate_id} expired for epoch {epoch}",
                    hint=_grant_hint(paths, epoch, now=now))
    if mandate.account_id != DEFAULT_ACCOUNT:
        return Gate("G4", "mandate", NO_GO,
                    f"mandate binds account {mandate.account_id}, not {DEFAULT_ACCOUNT}",
                    hint=_grant_hint(paths, epoch, now=now))
    return Gate("G4", "mandate", GO,
                f"granted for epoch {epoch}: {mandate.mandate_id}, "
                f"{mandate.days_left(now)} day(s) left, "
                f"{mandate.max_orders - mandate.orders_used} order(s) left")


def _gate_quotes(pair: StrikePair | None, deps: Deps, *, now: datetime) -> Gate:
    """G5: the quote rail on the candidate strikes (SKIPPED, never faked)."""
    if pair is None:
        return Gate("G5", "quotes", SKIPPED,
                    "no candidate strikes given (--buy-strike/--sell-strike/--expiry)")
    if not market_hours(now.timestamp()):
        return Gate("G5", "quotes", SKIPPED,
                    f"no live quotes outside RTH ({now:%a %H:%M ET}); "
                    "probe again inside the session")
    outcome = deps.probe(pair)
    if not outcome.available:
        return Gate("G5", "quotes", SKIPPED,
                    f"no live quotes: probe unavailable ({outcome.reason})")
    return quote_gate(outcome.legs)


def run_drill_check(paths: DeskPaths, supervised: SupervisedPaths, *,
                    deps: Deps | None = None, pair: StrikePair | None = None,
                    gateway_state: Path | None = None,
                    exit_watch_state: Path | None = None,
                    max_account_open_loss: Decimal | None = None,
                    plans_root: Path | None = None,
                    out: Callable[[str], None] = print) -> int:
    """Print one line per gate + the DRILL verdict; 0 pass-or-skipped, 1 NO-GO.

    ``max_account_open_loss`` is the operator's opt-in exposure rail over
    the WHOLE account (desk book + every other book under the scan root).
    Unset it is reported and never enforced, so this run cannot fail a
    drill the operator did not set a limit for.
    ``plans_root`` defaults to the repo's ``plans/``; the account's books
    are read from the desk run dir's PARENT (the same root exit_watch
    scans), never from the desk book alone."""
    deps = deps or Deps()
    now = deps.clock()
    if now.utcoffset() is None:
        raise ValueError("clock must return timezone-aware datetimes")
    now = now.astimezone(ET)
    if max_account_open_loss is not None and max_account_open_loss < 0:
        raise ValueError("--max-account-open-loss must be >= 0")
    gateway_state = gateway_state or gateway_watch.DEFAULT_STATE
    exit_watch_state = exit_watch_state or exit_watch.DEFAULT_STATE
    scan_root = paths.root.parent
    account = desk_book.account_exposure(
        as_of=now.date(), plans_root=plans_root or desk_book.default_plans_root(),
        state_root=scan_root, desk_specs=paths.specs(), desk_book=paths.book())

    desk_gate, desk = _gate_desk(paths, deps, now=now, root=scan_root,
                                 exit_watch_state=exit_watch_state,
                                 gateway_state=gateway_state,
                                 account=account, rail=max_account_open_loss)
    gates = [
        desk_gate,
        _gate_gateway(now=now, gateway_state=gateway_state),
        _gate_mandate(supervised, desk, now=now, paths=paths),
        _gate_quotes(pair, deps, now=now),
        Gate("G6", "rehearsal", MANUAL,
             "halt -> inspect -> resume by hand (resume removes HALT; flatten "
             "only closes after it)"),
    ]
    for gate in gates:
        out(gate.line)
    failed = [g.gate for g in gates if g.verdict == NO_GO]
    summary = ", ".join(f"{g.gate} {g.verdict}" for g in gates)
    tail = account_tail(account, max_account_open_loss)
    if failed:
        out(f"DRILL: NO-GO ({', '.join(failed)} failed) [{summary}]; {tail}")
        return 1
    skipped = [g.gate for g in gates if g.verdict == SKIPPED]
    note = f" ({', '.join(skipped)} skipped)" if skipped else ""
    out(f"DRILL: GO{note} [{summary}]; {tail}")
    return 0


def parse_pair(args: argparse.Namespace) -> StrikePair | None:
    """The three strike args together, or none of them; anything else is a
    usage error (raised) so the CLI can exit 2 before reading anything."""
    given = (args.buy_strike is not None, args.sell_strike is not None,
             args.expiry is not None)
    if all(given):
        return StrikePair(underlying=args.underlying, buy_strike=args.buy_strike,
                          sell_strike=args.sell_strike, expiry=args.expiry)
    if any(given):
        raise ValueError("drill-check needs --buy-strike, --sell-strike and "
                         "--expiry together (or none: G5 skips)")
    return None
