"""IB Gateway watchdog (systemd timer, once a minute).

The 2026-09-22/23 incident: every automated IBC login after the gateway's
daily restart stalled on an open login dialog (the image renders IbLoginId
from TWS_USERID; ib.env set TWS_USERNAME), x11vnc died at boot
(XOpenDisplay raced Xvfb) so the login screen was unreachable, and nothing
said so: the monitor crash-looped 1,276 times while the book had no exit
machine for ~14 hours.

Each run:

1. observes the container (running, start time), the API with a real IB
   handshake (server version reply, not just an open socket: the socat
   bridge accepts even when the gateway is logged out), IBC's login phase
   from ``docker logs -t``, and whether x11vnc runs;
2. classifies: ok | starting | checking | needs_login | needs_2fa |
   api_down | down (``checking`` = the API missed, not yet for
   API_DOWN_AFTER_S);
3. self-heals within caps: restarts x11vnc when missing (5-minute backoff;
   never when VNC is deliberately off); restarts the container when a
   login is stuck or the API stays silent (at most one per 2 hours and 4
   per 24 hours: IBKR can lock an account after repeated login attempts).
   The attempt is written to the ledger before docker acts, the API is
   re-probed first, and nothing restarts on an uncertain observation (log
   read failed, ledger unreadable); a stopped container is never touched;
4. notifies on entering a bad state, every 4 hours while it lasts, and on
   recovery; a failed push is retried every 5 minutes. Push text carries
   no URLs, hostnames, account ids or money;
5. writes ``~/.local/state/trex/gateway.json`` for the cockpit banner.

Raw log lines are parsed in memory and never persisted.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import struct
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from tree_options.trex.clock import ET

CONTAINER = "trex-ib-gateway"
API_HOST = "127.0.0.1"
API_PORT = 4002
LOGIN_URL = "https://pop-os.tail2b3f82.ts.net:6080/vnc.html?path=vnc"
DEFAULT_STATE = Path.home() / ".local" / "state" / "trex" / "gateway.json"

STARTUP_GRACE_S = 180  # container start / completed login -> API up
API_DOWN_AFTER_S = 300  # continuous API misses before "api_down"
NEEDS_LOGIN_AFTER_S = 600  # a login dialog open this long is stuck
RESTART_COOLDOWN_S = 2 * 3600
RESTARTS_PER_DAY = 4
VNC_RETRY_S = 300
NOTIFY_RETRY_S = 300  # a failed push is retried this often
REMIND_EVERY_S = 4 * 3600
DAY_S = 86_400
LOG_WINDOW_S = 26 * 3600
MAX_EVENTS = 30

# Per-call deadlines. A worst-case run (every call timing out) must finish
# inside the unit's TimeoutStartSec, or systemd can kill it between docker
# accepting a restart and the run finishing.
PROBE_TIMEOUT_S = 5.0
DOCKER_TIMEOUT_S = 15.0
LOGS_TIMEOUT_S = 30.0
RESTART_TIMEOUT_S = 90.0

BAD = frozenset({"needs_login", "needs_2fa", "api_down", "down"})
RESTARTABLE = frozenset({"needs_login", "api_down"})

# IBC log markers, most specific first; the phase is the latest one seen.
# A session start ("Starting session" at boot, "Re-starting session" on an
# auto-restart, IBC 3.24 SessionManager) always opens a fresh phase.
_MARKERS = (
    ("Login has completed", "logged_in"),
    ("Second Factor Authentication", "twofa"),
    ("frame entitled: Authenticating", "authenticating"),
    ("Login dialog WINDOW_OPENED", "login_dialog"),
    ("Re-starting session", "starting"),
    ("Starting session", "starting"),
    ("dialog entitled: Shutdown progress", "shutting_down"),
)
_SESSION_START = frozenset({"Starting session", "Re-starting session"})
_DOCKER_TS = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.\d+)?Z\s?(.*)$")

# up = x11vnc running; down = configured but not running; off = VNC
# deliberately disabled (no password): never started behind the image's back
VNC_STATE_SCRIPT = (
    "if pgrep -x x11vnc >/dev/null 2>&1; then echo up; "
    'elif [ -n "${VNC_SERVER_PASSWORD:-}${VNC_SERVER_PASSWORD_FILE:-}" ]; then echo down; '
    "else echo off; fi"
)


def x11vnc_script(tmp: str = "/tmp") -> str:
    """x11vnc as the image's run.sh starts it, pinned to the Xvfb display.

    The password resolves inside the container the way the image does
    (VNC_SERVER_PASSWORD, else VNC_SERVER_PASSWORD_FILE) and never crosses
    to this side; an empty one refuses to start rather than serve the
    broker desktop without auth.
    """
    return (
        'pw="${VNC_SERVER_PASSWORD:-}"; '
        'if [ -z "$pw" ] && [ -r "${VNC_SERVER_PASSWORD_FILE:-}" ]; then '
        'pw="$(cat "$VNC_SERVER_PASSWORD_FILE")"; fi; '
        '[ -n "$pw" ] || { echo "no VNC password configured" >&2; exit 3; }; '
        "exec x11vnc -ncache_cr -display :1 -forever -shared -bg -noipv6 "
        f'-passwd "$pw" -o {tmp}/x11vnc.log >{tmp}/x11vnc.start 2>&1'
    )


def parse_ts(stamp: str) -> float | None:
    """Docker RFC3339 UTC stamp (ns precision allowed) -> epoch seconds."""
    m = _DOCKER_TS.match(stamp.strip())
    if not m:
        return None
    return datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UTC).timestamp()


@dataclass(frozen=True)
class IbcState:
    phase: str  # logged_in|twofa|authenticating|login_dialog|starting|shutting_down|unknown
    since: float | None


def parse_ibc_log(lines: list[str]) -> IbcState:
    """Latest IBC login phase from `docker logs -t` lines, dated from the
    first line of that phase (a dialog open since 23:45 is "since 23:45")."""
    phase, since = "unknown", None
    for line in lines:
        m = _DOCKER_TS.match(line)
        if not m or "IBC:" not in m.group(2):
            continue
        for marker, name in _MARKERS:
            if marker in m.group(2):
                if name != phase or marker in _SESSION_START:
                    phase, since = name, parse_ts(line)
                break
    return IbcState(phase, since)


def probe_api(
    host: str = API_HOST, port: int = API_PORT, timeout: float = PROBE_TIMEOUT_S
) -> tuple[bool, str]:
    """IB API handshake: healthy only if the gateway answers with its
    server version. No clientId is sent and no API session is started."""
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            versions = b"v100..187"
            sock.sendall(b"API\x00" + struct.pack(">I", len(versions)) + versions)
            header = b""
            while len(header) < 4:
                chunk = sock.recv(4 - len(header))
                if not chunk:
                    return False, "closed before reply (gateway not logged in)"
                header += chunk
            (size,) = struct.unpack(">I", header)
            body = sock.recv(min(size, 256))
            version = body.split(b"\x00", 1)[0].decode("ascii", "replace")
            if not version.isdigit():
                return False, "unexpected handshake reply"
            return True, f"server_version={version}"
    except OSError as exc:
        return False, type(exc).__name__


def wait_for_api(
    probe: Callable[[], tuple[bool, str]],
    timeout_s: float,
    *,
    poll_s: float = 5.0,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> bool:
    """Block until the API answers or ``timeout_s`` passes (trex-monitor's
    ExecStartPre: start the exit machine the moment the gateway is back
    rather than crash-looping against a logged-out gateway)."""
    deadline = clock() + timeout_s
    while True:
        if probe()[0]:
            return True
        if clock() + poll_s > deadline:
            return False
        sleep(poll_s)


@dataclass(frozen=True)
class Observation:
    now: float
    container_running: bool
    container_started: float | None
    api_ok: bool
    api_detail: str
    ibc: IbcState
    vnc_running: bool | None  # None = unknown, or VNC deliberately off
    ibc_observed: bool = True  # False = the log read failed


@dataclass(frozen=True)
class Action:
    kind: str  # restart_vnc | restart_gateway | notify
    detail: str = ""
    title: str = ""
    message: str = ""
    priority: str = "default"


def _et(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, ET).strftime("%a %H:%M ET")


def _span(seconds: float) -> str:
    m = int(seconds // 60)
    return f"{m // 60}h {m % 60}m" if m >= 60 else f"{m}m"


def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


def _api_silent(obs: Observation, fail_for: float, what: str) -> tuple[str, float | None, str]:
    detail = f"{what} ({obs.api_detail})"
    if fail_for < API_DOWN_AFTER_S:
        return "checking", obs.now - fail_for, f"{detail}; rechecking"
    return "api_down", obs.now - fail_for, detail


def classify(obs: Observation, api_fail_for: float = 0.0) -> tuple[str, float | None, str]:
    """(status, since-hint, detail). ``api_fail_for`` = seconds the API
    has been failing continuously (one miss is a recheck, not an outage)."""
    if not obs.container_running:
        return "down", None, "gateway container is not running"
    if obs.api_ok:
        return "ok", None, obs.api_detail
    phase, since = obs.ibc.phase, obs.ibc.since
    age = obs.now - since if since is not None else None
    young = (
        obs.container_started is not None
        and obs.now - obs.container_started < STARTUP_GRACE_S
    )
    if phase == "twofa":
        return "needs_2fa", since, "waiting for 2FA approval in IBKR Mobile"
    if phase == "logged_in":
        if young or (age is not None and age < STARTUP_GRACE_S):
            return "starting", since, "logged in, API opening"
        return _api_silent(obs, api_fail_for, "logged in but API not answering")
    if phase in ("login_dialog", "starting", "authenticating", "shutting_down"):
        if age is not None and age >= NEEDS_LOGIN_AFTER_S:
            what = "login screen open" if phase == "login_dialog" else f"stuck {phase}"
            return "needs_login", since, f"{what}, not logged in"
        return "starting", since, phase.replace("_", " ")
    if young:
        return "starting", obs.container_started, "container starting"
    return _api_silent(obs, api_fail_for, "API not answering; IBC state unknown")


def _message(status: str, since: float, now: float, restarts_left: int) -> tuple[str, str]:
    lasting = f"since {_et(since)} ({_span(now - since)})"
    retry = (
        f" Auto-retry: {restarts_left} left today."
        if status in RESTARTABLE
        else ""
    )
    if status == "needs_login":
        return "trex: IB Gateway needs login", (
            f"Logged out {lasting}. Open positions have no exit machine. "
            f"Open the trex cockpit banner to log in.{retry}"
        )
    if status == "needs_2fa":
        return "trex: approve IB Gateway 2FA", (
            f"Waiting for IBKR Mobile approval {lasting}."
        )
    if status == "api_down":
        return "trex: IB Gateway API down", (
            f"Gateway API not answering {lasting}.{retry}"
        )
    return "trex: IB Gateway container down", (
        f"The gateway container is not running {lasting}. Start it with "
        f"docker compose (deploy/trex)."
    )


def _budget(restarts: list[float]) -> tuple[int, float | None]:
    """(restarts left today, when the cooldown next allows one)."""
    left = RESTARTS_PER_DAY - len(restarts)
    return left, (restarts[-1] + RESTART_COOLDOWN_S if left > 0 and restarts else None)


def decide(obs: Observation, prior: dict[str, Any]) -> tuple[dict[str, Any], list[Action]]:
    """Pure policy: next persisted state + actions to execute."""
    same_container = (
        obs.container_started is not None
        and prior.get("container_started") == obs.container_started
    )
    if (
        obs.ibc.phase == "unknown"
        and same_container
        and prior.get("ibc_phase") not in (None, "unknown")
    ):
        # nothing in the log window (or the read failed): for the same
        # container the last observed phase still holds
        obs = replace(obs, ibc=IbcState(str(prior["ibc_phase"]), _num(prior.get("ibc_since"))))

    fail_since = None if obs.api_ok else (_num(prior.get("api_fail_since")) or obs.now)
    status, since_hint, detail = classify(obs, obs.now - fail_since if fail_since else 0.0)
    prev = prior.get("status")
    if prev == status and isinstance(prior.get("since"), (int, float)):
        since = float(prior["since"])
    else:
        since = since_hint if since_hint is not None else obs.now

    restarts = [t for t in prior.get("restarts", []) if obs.now - t < DAY_S]
    vnc_restarts = [t for t in prior.get("vnc_restarts", []) if obs.now - t < DAY_S]
    actions: list[Action] = []

    if obs.container_running and obs.vnc_running is False and (
        not vnc_restarts or obs.now - vnc_restarts[-1] >= VNC_RETRY_S
    ):
        actions.append(Action("restart_vnc", "x11vnc not running (login screen unreachable)"))
        vnc_restarts.append(obs.now)

    hold = _num(prior.get("restart_hold_until"))
    held = hold is not None and obs.now < hold
    can_restart = (
        obs.ibc_observed
        and not held
        and len(restarts) < RESTARTS_PER_DAY
        and (not restarts or obs.now - restarts[-1] >= RESTART_COOLDOWN_S)
    )
    if status in RESTARTABLE and can_restart:
        actions.append(Action("restart_gateway", detail))
        restarts.append(obs.now)
    restarts_left, next_restart = _budget(restarts)

    last_status = prior.get("last_notified_status")
    last_at = prior.get("last_notified_at")
    failed_at = _num(prior.get("notify_failed_at"))
    backing_off = failed_at is not None and obs.now - failed_at < NOTIFY_RETRY_S
    notified_status, notified_at = last_status, last_at
    if status in BAD and not backing_off:
        due = (
            last_status != status
            or not isinstance(last_at, (int, float))
            or obs.now - last_at >= REMIND_EVERY_S
        )
        if due:
            title, message = _message(status, since, obs.now, restarts_left)
            actions.append(Action("notify", status, title, message, "high"))
            notified_status, notified_at = status, obs.now
    elif status == "ok" and last_status in BAD and not backing_off:
        was = prior.get("since")
        outage = f" after {_span(obs.now - was)}" if isinstance(was, (int, float)) else ""
        actions.append(
            Action("notify", "recovered", "trex: IB Gateway back",
                   f"API answering again{outage}.", "default")
        )
        notified_status, notified_at = "ok", obs.now

    events: list[dict[str, Any]] = list(prior.get("events", []))[-MAX_EVENTS:]
    if prev != status:
        events.append({"at": obs.now, "kind": "status", "detail": f"{prev} -> {status}"})
    state: dict[str, Any] = {
        "status": status,
        "since": since,
        "detail": detail,
        "checked_at": obs.now,
        "api_ok": obs.api_ok,
        "api_detail": obs.api_detail,
        "api_fail_since": fail_since,
        "ibc_phase": obs.ibc.phase,
        "ibc_since": obs.ibc.since,
        "ibc_observed": obs.ibc_observed,
        "container_running": obs.container_running,
        "container_started": obs.container_started,
        "vnc_running": obs.vnc_running,
        "restarts": restarts,
        "restarts_left": restarts_left,
        "next_restart_at": next_restart,
        "restart_hold_until": hold if held else None,
        "vnc_restarts": vnc_restarts,
        "last_notified_status": notified_status,
        "last_notified_at": notified_at,
        "notify_failed_at": failed_at,
        "events": events,
    }
    return state, actions


class DockerOps(Protocol):
    def inspect(self) -> tuple[bool, float | None]: ...
    def ibc_lines(self, since: float) -> list[str] | None: ...
    def vnc_running(self) -> bool | None: ...
    def start_vnc(self) -> bool: ...
    def restart(self) -> bool: ...


class Docker:
    """The real docker CLI (the user is in the docker group)."""

    def __init__(self, container: str = CONTAINER) -> None:
        self.container = container

    def _run(
        self, *args: str, timeout: float = DOCKER_TIMEOUT_S
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["docker", *args], capture_output=True, text=True, timeout=timeout, check=False
        )

    def inspect(self) -> tuple[bool, float | None]:
        try:
            out = self._run("inspect", "-f", "{{.State.Running}}|{{.State.StartedAt}}", self.container)
        except (OSError, subprocess.TimeoutExpired):
            return False, None
        if out.returncode != 0:
            return False, None
        running, _, started = out.stdout.strip().partition("|")
        return running == "true", parse_ts(started)

    def ibc_lines(self, since: float) -> list[str] | None:
        """IBC lines since ``since``; None when the read failed (an
        uncertain phase must not look like "no phase")."""
        try:
            out = self._run("logs", "-t", "--since", str(int(since)), self.container,
                            timeout=LOGS_TIMEOUT_S)
        except (OSError, subprocess.TimeoutExpired):
            return None
        if out.returncode != 0:
            return None
        return [ln for ln in (out.stdout + "\n" + out.stderr).splitlines() if "IBC:" in ln]

    def vnc_running(self) -> bool | None:
        try:
            out = self._run("exec", self.container, "sh", "-c", VNC_STATE_SCRIPT)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return {"up": True, "down": False}.get(out.stdout.strip()) if out.returncode == 0 else None

    def start_vnc(self) -> bool:
        try:
            out = self._run("exec", "-u", "ibgateway", self.container, "sh", "-c", x11vnc_script())
        except (OSError, subprocess.TimeoutExpired):
            return False
        return out.returncode == 0

    def restart(self) -> bool:
        try:
            return self._run("restart", self.container, timeout=RESTART_TIMEOUT_S).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False


def _read_state(path: Path) -> tuple[dict[str, Any], bool]:
    """(state, readable). A missing file is a fresh start; one that exists
    but can't be read may hide recent restarts."""
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {}, True
    except (OSError, ValueError):
        return {}, False
    return (data, True) if isinstance(data, dict) else ({}, False)


def _write_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state, indent=1))
    os.replace(tmp, path)


def _release_restart(state: dict[str, Any]) -> None:
    if state["restarts"]:
        state["restarts"].pop()
    state["restarts_left"], state["next_restart_at"] = _budget(state["restarts"])


def watch_once(
    state_path: Path,
    docker: DockerOps,
    *,
    probe: Callable[[], tuple[bool, str]],
    notify: Callable[[str, str, str], bool],
    now: float,
    login_url: str,
    dry_run: bool = False,
    write_state: Callable[[Path, dict[str, Any]], None] = _write_state,
) -> dict[str, Any]:
    prior, readable = _read_state(state_path)
    if not readable:
        prior = {
            "restart_hold_until": now + RESTART_COOLDOWN_S,
            "events": [{"at": now, "kind": "ledger", "ok": False,
                        "detail": "state unreadable; container restarts held for a cooldown"}],
        }
    running, started = docker.inspect()
    observed = True
    if running:
        api_ok, api_detail = probe()
        lines = docker.ibc_lines(max(started or 0.0, now - LOG_WINDOW_S))
        observed = lines is not None
        ibc = parse_ibc_log(lines) if lines is not None else IbcState("unknown", None)
        vnc = docker.vnc_running()
    else:
        api_ok, api_detail, ibc, vnc = False, "container down", IbcState("unknown", None), None
    obs = Observation(now, running, started, api_ok, api_detail, ibc, vnc, observed)
    state, actions = decide(obs, prior)
    state["login_url"] = login_url

    if not dry_run and any(a.kind == "restart_gateway" for a in actions):
        try:
            write_state(state_path, state)  # reserve the attempt before docker acts
        except OSError:
            actions = [a for a in actions if a.kind != "restart_gateway"]
            _release_restart(state)
            state["events"].append({"at": now, "kind": "restart_refused", "ok": False,
                                    "detail": "ledger not writable", "dry_run": False})

    api_back = False
    for action in actions:
        kind, detail, ok = action.kind, action.detail, True
        if not dry_run:
            if kind == "restart_vnc":
                ok = docker.start_vnc()
            elif kind == "restart_gateway":
                api_back = probe()[0]
                if api_back:
                    _release_restart(state)
                    kind, detail = "restart_skipped", "API answered on recheck"
                else:
                    ok = docker.restart()
            elif kind == "notify" and api_back and action.detail in BAD:
                kind, detail = "notify_skipped", "API answered on recheck"
                state["last_notified_status"] = prior.get("last_notified_status")
                state["last_notified_at"] = prior.get("last_notified_at")
            elif kind == "notify":
                ok = notify(action.title, action.message, action.priority)
                if ok:
                    state["notify_failed_at"] = None
                else:  # not delivered: retry after NOTIFY_RETRY_S, not in 4 h
                    state["last_notified_status"] = prior.get("last_notified_status")
                    state["last_notified_at"] = prior.get("last_notified_at")
                    state["notify_failed_at"] = now
        state["events"].append(
            {"at": now, "kind": kind, "detail": detail, "ok": ok, "dry_run": dry_run}
        )
    state["events"] = state["events"][-MAX_EVENTS:]
    if not dry_run:
        write_state(state_path, state)
    return state


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--container", default=CONTAINER)
    parser.add_argument("--login-url", default=LOGIN_URL)
    parser.add_argument("--dry-run", action="store_true",
                        help="observe and print decisions; change nothing, send nothing")
    parser.add_argument("--wait-api", type=float, metavar="SECONDS",
                        help="only wait until the API answers (exit 0) or time out (exit 1)")
    args = parser.parse_args(argv)

    if args.wait_api is not None:
        if wait_for_api(probe_api, args.wait_api):
            return 0
        print(f"gateway API not answering after {args.wait_api:.0f}s", file=sys.stderr)
        return 1

    from tree_options.trex.notify import load_config, send

    cfg = load_config()
    state = watch_once(
        args.state,
        Docker(args.container),
        probe=probe_api,
        notify=lambda title, message, priority: send(cfg, title, message, priority),
        now=time.time(),
        login_url=args.login_url,
        dry_run=args.dry_run,
    )
    summary = {k: state[k] for k in ("status", "detail", "ibc_phase", "vnc_running",
                                     "restarts_left")}
    summary["since"] = _et(state["since"])
    summary["actions"] = [e["kind"] for e in state["events"] if e["at"] == state["checked_at"]
                          and e["kind"] != "status"]
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
