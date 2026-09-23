"""Exit-machine watchdog (systemd timer, once a minute).

The gateway watchdog covers the broker session; this covers what actually
guards the book: trex-monitor. It can be dead (no heartbeat in book.json),
or alive with every tick failing (caught and logged each poll, while
systemd and the heartbeat both look fine). Either way open positions have
no touch or time-stop exits, and until 2026-09-23 nothing said so.

Each run scans the run dirs under ``~/.local/state/trex`` for books with
exposure (a structure entering, open or exiting; an unreadable book.json
counts, since exposure can't be ruled out) and classifies:

* ``idle``: no exposure, nothing to guard;
* ``ok``;
* ``waiting_for_gateway``: the monitor is down or failing because of the
  gateway: the gateway watchdog is alarming (its push covers it), or the
  gateway is not settled-healthy and the outage is under
  ATTRIBUTION_MAX_S (a gateway that flaps without ever alarming can't
  hide a monitor outage for longer);
* ``monitor_down``: heartbeat older than HEARTBEAT_STALE_S (or the book is
  unreadable) and the gateway not to blame (a silent gateway watchdog
  never takes the blame);
* ``monitor_failing``: the monitor beats but its ``monitor.json`` is absent
  or older than HEALTH_STALE_S (the loop never finishes); or, in market
  hours, TICK_FAILURES_BAD ticks failed in a row or none succeeded for
  TICK_SILENT_S (the last good tick survives monitor restarts).

Pushes follow trex.alert_policy; the verdict goes to
``~/.local/state/trex/exit_watch.json`` for the cockpit banner. Detection
only: restarting the monitor is systemd's job (Restart=on-failure).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from tree_options.trex import gateway_watch
from tree_options.trex.alert_policy import (
    Urgency,
    et_label,
    load_quiet_hours,
    market_hours,
    next_push,
    settle_push,
    span_label,
    urgency,
)

STATE_ROOT = Path.home() / ".local" / "state" / "trex"
DEFAULT_STATE = STATE_ROOT / "exit_watch.json"
DEFAULT_GATEWAY_STATE = STATE_ROOT / "gateway.json"
UNIT = "trex-monitor.service"

HEARTBEAT_STALE_S = 120  # the loop beats every ~20 s
HEALTH_STALE_S = 180  # monitor.json is written at start and after every loop
GATEWAY_SETTLE_S = 120  # a recovered gateway: ExecStartPre + connect + qualify
GATEWAY_WATCH_STALE_S = 300  # an older gateway.json says nothing
ATTRIBUTION_MAX_S = 900  # a non-alarming gateway can excuse an outage this long
TICK_FAILURES_BAD = 3
TICK_SILENT_S = 180
MAX_EVENTS = 30

EXPOSED = frozenset({"enter_working", "open", "exit_working"})
BAD = frozenset({"monitor_down", "monitor_failing"})
HEALTHY = frozenset({"ok", "idle"})
GATEWAY_ALARMS = gateway_watch.BAD  # states the gateway watchdog pushes for
_SEVERITY = ("idle", "ok", "waiting_for_gateway", "monitor_failing", "monitor_down")


@dataclass(frozen=True)
class BookObs:
    plan: str  # run dir name
    heartbeat: float | None
    health: dict[str, Any] | None  # monitor.json, None if absent/unreadable
    unreadable: bool = False  # book.json exists but can't be read: exposure unknown


@dataclass(frozen=True)
class ExitObs:
    now: float
    books: list[BookObs]  # books with exposure only
    gateway_status: str | None  # None = gateway watchdog silent/stale
    gateway_since: float | None
    market: bool
    unit_state: str | None  # `systemctl --user is-active trex-monitor`


@dataclass(frozen=True)
class Action:
    kind: str  # notify
    detail: str = ""
    title: str = ""
    message: str = ""
    priority: str = "default"


def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


def _epoch(iso: Any) -> float | None:
    if not isinstance(iso, str):
        return None
    try:
        return datetime.fromisoformat(iso).timestamp()
    except ValueError:
        return None


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


def scan_books(root: Path) -> list[BookObs]:
    """Books with exposure under ``root`` (one run dir per plan). A book.json
    that exists but can't be read is included (exposure can't be ruled
    out); a root that can't be listed raises, so the run fails and the
    banner goes stale rather than reading "idle"."""
    try:
        entries = list(root.iterdir())
    except FileNotFoundError:
        return []  # nothing has ever run here
    books: list[BookObs] = []
    for d in sorted(p for p in entries if p.is_dir()):
        path = d / "book.json"
        if not path.exists():
            continue
        raw = _read_json(path)
        structures = raw.get("structures") if raw else None
        if raw is None or not isinstance(structures, dict):
            books.append(BookObs(d.name, None, _read_json(d / "monitor.json"), unreadable=True))
            continue
        if any(isinstance(s, dict) and s.get("status") in EXPOSED for s in structures.values()):
            books.append(
                BookObs(d.name, _epoch(raw.get("heartbeat")), _read_json(d / "monitor.json"))
            )
    return books


def _book_verdict(
    b: BookObs, obs: ExitObs, missing_since: float | None
) -> tuple[str, float | None, str]:
    """(status, since, detail) for one book. ``missing_since``: when this
    watchdog first saw a beating monitor with no tick health."""
    if b.unreadable:
        return "monitor_down", None, f"{b.plan}: book.json unreadable (exposure unknown)"
    gw = obs.gateway_status
    gateway_unsettled = gw is not None and not (
        gw == "ok" and obs.gateway_since is not None
        and obs.now - obs.gateway_since >= GATEWAY_SETTLE_S
    )

    def blame_gateway(outage_since: float | None) -> bool:
        if gw in GATEWAY_ALARMS:
            return True  # its push covers this outage
        return (
            gateway_unsettled
            and outage_since is not None
            and obs.now - outage_since < ATTRIBUTION_MAX_S
        )

    waiting = f"{b.plan}: monitor waiting for the IB Gateway ({gw})"
    age = obs.now - b.heartbeat if b.heartbeat is not None else None
    if age is None or age > HEARTBEAT_STALE_S:
        if blame_gateway(b.heartbeat):
            return "waiting_for_gateway", b.heartbeat, waiting
        what = f"no heartbeat for {span_label(age)}" if age is not None else "no heartbeat yet"
        unit = f" (unit {obs.unit_state})" if obs.unit_state else ""
        return "monitor_down", b.heartbeat, f"{b.plan}: {what}{unit}"

    h = b.health
    at = _num(h.get("at")) if h else None
    if h is None or at is None:
        since = missing_since if missing_since is not None else obs.now
        if obs.now - since > HEALTH_STALE_S:
            return "monitor_failing", since, (
                f"{b.plan}: tick health not reported for {span_label(obs.now - since)}"
            )
        return "ok", None, f"{b.plan}: heartbeat {int(age)}s ago; tick health not reported yet"
    if obs.now - at > HEALTH_STALE_S:
        return "monitor_failing", at, (
            f"{b.plan}: tick health last reported {span_label(obs.now - at)} ago"
        )
    failures = int(h.get("tick_failures") or 0)
    ok_at = _num(h.get("last_tick_ok_at"))
    failing = failures >= TICK_FAILURES_BAD or (
        ok_at is not None and obs.now - ok_at > TICK_SILENT_S
    )
    if failing and obs.market:
        if blame_gateway(ok_at):
            return "waiting_for_gateway", ok_at, waiting
        return "monitor_failing", ok_at, (
            f"{b.plan}: {failures} checks failed in a row (last: {h.get('last_error')})"
        )
    return "ok", None, f"{b.plan}: heartbeat {int(age)}s ago"


def _assess(
    obs: ExitObs, health_missing_since: dict[str, float]
) -> tuple[list[dict[str, Any]], dict[str, float]]:
    """Per-book rows, plus when each beating-but-silent monitor was first
    seen without tick health (timed here: the heartbeat can't say)."""
    rows: list[dict[str, Any]] = []
    missing: dict[str, float] = {}
    for b in obs.books:
        beating = b.heartbeat is not None and obs.now - b.heartbeat <= HEARTBEAT_STALE_S
        if beating and not b.unreadable and (b.health is None or _num(b.health.get("at")) is None):
            missing[b.plan] = health_missing_since.get(b.plan, obs.now)
        status, since, detail = _book_verdict(b, obs, missing.get(b.plan))
        age = round(obs.now - b.heartbeat) if b.heartbeat is not None else None
        rows.append({"plan": b.plan, "status": status, "since": since, "detail": detail,
                     "heartbeat_age": age})
    return rows, missing


def book_rows(obs: ExitObs) -> list[dict[str, Any]]:
    return _assess(obs, {})[0]


def _verdict(rows: list[dict[str, Any]]) -> tuple[str, float | None, str]:
    if not rows:
        return "idle", None, "no open positions"
    status = max((r["status"] for r in rows), key=_SEVERITY.index)
    worst = [r for r in rows if r["status"] == status]
    since = min((r["since"] for r in worst if r["since"] is not None), default=None)
    return status, since, "; ".join(r["detail"] for r in worst)


def classify(
    obs: ExitObs, health_missing_since: dict[str, float] | None = None
) -> tuple[str, float | None, str]:
    """(status, since-hint, detail): the worst book decides."""
    return _verdict(_assess(obs, health_missing_since or {})[0])


def _message(status: str, since: float, now: float) -> tuple[str, str]:
    lasting = f"since {et_label(since)} ({span_label(now - since)})"
    if status == "monitor_down":
        return "trex: exit machine down", (
            f"The trex monitor has not run {lasting}. Open positions have no touch or "
            f"time-stop exits."
        )
    return "trex: exit machine failing", (
        f"The trex monitor runs but its checks have failed {lasting}. Open positions have "
        f"no working exits."
    )


def decide(
    obs: ExitObs, prior: dict[str, Any], *, urgency: Urgency
) -> tuple[dict[str, Any], list[Action]]:
    """Pure policy: next persisted state + pushes to send."""
    prior_missing = prior.get("health_missing_since")
    rows, missing = _assess(obs, prior_missing if isinstance(prior_missing, dict) else {})
    status, since_hint, detail = _verdict(rows)
    prev = prior.get("status")
    if prev == status and isinstance(prior.get("since"), (int, float)):
        since = float(prior["since"])
    else:
        since = since_hint if since_hint is not None else obs.now

    def recovery() -> tuple[str, str]:
        if status == "idle":
            return "trex: exit machine back", "No open positions left to guard."
        was = prior.get("since")
        outage = f" after {span_label(obs.now - was)}" if isinstance(was, (int, float)) else ""
        return "trex: exit machine back", f"The monitor is guarding the book again{outage}."

    push, notified = next_push(
        status=status, now=obs.now, prior=prior, urgency=urgency, bad=BAD, healthy=HEALTHY,
        alarm=lambda: _message(status, since, obs.now), recovery=recovery,
    )
    actions = (
        [Action("notify", push.status, push.title, push.message, push.priority)] if push else []
    )
    events: list[dict[str, Any]] = list(prior.get("events", []))[-MAX_EVENTS:]
    if prev != status:
        events.append({"at": obs.now, "kind": "status", "detail": f"{prev} -> {status}"})
    state: dict[str, Any] = {
        "status": status,
        "since": since,
        "detail": detail,
        "checked_at": obs.now,
        "market": obs.market,
        "gateway_status": obs.gateway_status,
        "unit_state": obs.unit_state,
        "books": rows,
        "health_missing_since": missing,
        **notified,
        "events": events,
    }
    return state, actions


def _gateway(path: Path, now: float) -> tuple[str | None, float | None]:
    raw = _read_json(path)
    checked = _num(raw.get("checked_at")) if raw else None
    if raw is None or checked is None or now - checked > GATEWAY_WATCH_STALE_S:
        return None, None
    status = raw.get("status")
    return (status if isinstance(status, str) else None), _num(raw.get("since"))


def unit_state(unit: str = UNIT) -> str | None:
    try:
        out = subprocess.run(["systemctl", "--user", "is-active", unit], capture_output=True,
                             text=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout.strip() or None


def _write_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state, indent=1))
    os.replace(tmp, path)


def watch_once(
    state_path: Path,
    *,
    root: Path,
    gateway_state: Path,
    notify: Callable[[str, str, str], bool],
    now: float,
    urgency: Urgency,
    unit_state: str | None,
    dry_run: bool = False,
) -> dict[str, Any]:
    prior = _read_json(state_path) or {}
    gw_status, gw_since = _gateway(gateway_state, now)
    obs = ExitObs(now, scan_books(root), gw_status, gw_since, market_hours(now), unit_state)
    state, actions = decide(obs, prior, urgency=urgency)
    for action in actions:
        ok = True
        if not dry_run:
            ok = notify(action.title, action.message, action.priority)
            settle_push(state, prior, ok, now)
        state["events"].append({"at": now, "kind": action.kind, "detail": action.detail,
                                "ok": ok, "dry_run": dry_run})
    state["events"] = state["events"][-MAX_EVENTS:]
    if not dry_run:
        _write_state(state_path, state)
    return state


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--root", type=Path, default=STATE_ROOT)
    parser.add_argument("--gateway-state", type=Path, default=DEFAULT_GATEWAY_STATE)
    parser.add_argument("--dry-run", action="store_true",
                        help="observe and print the verdict; write nothing, send nothing")
    args = parser.parse_args(argv)

    from tree_options.trex.notify import DEFAULT_CONFIG, load_config, read_env, send

    cfg = load_config()
    now = time.time()
    urg = urgency(now, exposed=bool(scan_books(args.root)),
                  quiet=load_quiet_hours(read_env(DEFAULT_CONFIG)))
    state = watch_once(
        args.state, root=args.root, gateway_state=args.gateway_state,
        notify=lambda title, message, priority: send(cfg, title, message, priority),
        now=now, urgency=urg, unit_state=unit_state(), dry_run=args.dry_run,
    )
    summary = {k: state[k] for k in ("status", "detail", "gateway_status", "unit_state",
                                     "notify_held")}
    summary["actions"] = [e["kind"] for e in state["events"] if e["at"] == now
                          and e["kind"] != "status"]
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
