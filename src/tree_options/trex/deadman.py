"""Dead-man heartbeat (Wave 4 lane J): the alarm that works when this host does not.

Each tick (``trex-deadman.timer``, about every 30 s) first runs the Wave 4
owner alert sweep (:mod:`tree_options.trex.alert_sweep`), then, when due,
sends ONE outbound ping to an external check the owner configured. That
check (any service that alarms when expected pings stop) tells the owner
when this host, its timers, the release gate or the alert sweep itself has
stopped: the one failure no local alert can report. A failed sweep sends no
ping, on purpose.

Configured by file NAME, never by value (the URL is a capability):

    ~/.config/trex/deadman.env   (chmod 600; TREX_DEADMAN_CONFIG overrides the path)
    DEADMAN_URL=<the check's https ping URL>
    DEADMAN_EVERY_S=300          (optional: 60..3600, default 300)

No file, no URL or a non-https URL: disabled, the default (no URL is
committed anywhere). The ping is a bare GET of exactly that URL: no body,
no query, no header but ``User-Agent``; it carries no account data. The URL
is never logged, printed or stored. A failed ping is logged by class and
waits for the next interval: it is never retried in a loop.

Exit 0: done; 3: degraded (a push or the ping failed, or a state file could
not be written); 1: the sweep failed (no ping was sent); 2: usage.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Protocol

from tree_options.trex.notify import read_env

CONFIG_ENV: Final = "TREX_DEADMAN_CONFIG"
CONFIG_NAME: Final = "deadman.env"
DEFAULT_EVERY_S: Final = 300
MIN_EVERY_S: Final = 60
MAX_EVERY_S: Final = 3600
TIMEOUT_S: Final = 10.0
USER_AGENT: Final = "trex-deadman/1"
STATE_SCHEMA: Final = "trex-deadman/1"
TICK_SCHEMA: Final = "trex-deadman-tick/1"
STATE_NAME: Final = "deadman.json"
ALERT_BOOK_NAME: Final = "wave4-alerts.json"
EXIT_OK: Final = 0
EXIT_SWEEP_FAILED: Final = 1
EXIT_DEGRADED: Final = 3

#: (url, timeout) -> HTTP status
PingTransport = Callable[[str, float], int]


class SweepOutcome(Protocol):
    @property
    def degraded(self) -> bool: ...

    def summary(self) -> dict[str, Any]: ...


# -- configuration ------------------------------------------------------------------


def config_path() -> Path:
    """The config file, resolved at call time (never an import-time HOME)."""
    raw = os.environ.get(CONFIG_ENV, "").strip()
    return Path(raw).expanduser() if raw else Path.home() / ".config" / "trex" / CONFIG_NAME


def default_state_dir() -> Path:
    """``<TREX_DESK_STATE>/alerts``: the release unit's only writable root."""
    from tree_options.desk.paths import state_root

    return state_root() / "alerts"


def default_acceptance_dir() -> Path:
    """``TREX_ACCEPTANCE_DIR`` (WAVE3-CONTRACTS.md C3.7)."""
    raw = os.environ.get("TREX_ACCEPTANCE_DIR", "").strip()
    return Path(raw or "~/.local/state/trex/acceptance").expanduser()


def default_run_dir() -> Path:
    """``TREX_DESK_RUN_DIR``, as ``desk.enter.execution_directory`` (pinned by a
    test; that module reads the desk universe config when imported)."""
    raw = os.environ.get("TREX_DESK_RUN_DIR", "~/.local/state/trex/desk-paper")
    return Path(raw).expanduser()


@dataclass(frozen=True)
class DeadmanConfig:
    url: str = field(repr=False)  # a capability: never shown
    every_s: int


def _every(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_EVERY_S
    return min(MAX_EVERY_S, max(MIN_EVERY_S, value))


def load_config(path: Path) -> tuple[DeadmanConfig | None, str]:
    """(config, "ok") or (None, why it is off)."""
    values = read_env(path)
    url = values.get("DEADMAN_URL", "")
    if not url:
        return None, "deadman_not_configured"
    if not url.startswith("https://"):
        return None, "deadman_url_not_https"
    raw = values.get("DEADMAN_EVERY_S", "")
    return DeadmanConfig(url, _every(raw) if raw else DEFAULT_EVERY_S), "ok"


# -- the ping ---------------------------------------------------------------------------


def urllib_ping(url: str, timeout: float) -> int:
    """A bare GET: no body, no query added, only a ``User-Agent`` header."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return int(resp.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)


def _epoch(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def heartbeat_due(prior: Mapping[str, Any], now: float, every_s: int) -> bool:
    """Due once ``every_s`` has passed since the last ATTEMPT (sent or not),
    so a failing check is tried once per interval, never in a loop."""
    last = _epoch(prior.get("last_attempt_at"))
    if last is None or now < last:  # never tried, or the clock went back
        return True
    return now - last >= every_s


def ping(
    cfg: DeadmanConfig, prior: Mapping[str, Any], now: float, transport: PingTransport
) -> tuple[dict[str, Any], bool]:
    """One attempt; the state to persist and whether the check accepted it."""
    error: str | None = None
    try:
        status = transport(cfg.url, TIMEOUT_S)
        ok = 200 <= status < 300
        if not ok:
            error = f"HTTP {status}"
    except Exception as exc:  # never echo: the message can embed the URL
        ok, error = False, type(exc).__name__
    if not ok:
        print(
            f"deadman: ping failed ({error}); next attempt in {cfg.every_s}s "
            f"(config {CONFIG_NAME})",
            file=sys.stderr,
        )
    return {
        "schema": STATE_SCHEMA,
        "last_attempt_at": now,
        "last_ok": ok,
        "last_ok_at": now if ok else _epoch(prior.get("last_ok_at")),
        "last_error": error,
        "every_s": cfg.every_s,
    }, ok


def _read_state(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(json.dumps(state, sort_keys=True))
    os.replace(tmp, path)


def _where(exc: BaseException) -> str:
    """The class and innermost frame of an exception, never its message
    (a validation error can quote file contents)."""
    tb = exc.__traceback__
    while tb is not None and tb.tb_next is not None:
        tb = tb.tb_next
    if tb is None:
        return type(exc).__name__
    return f"{type(exc).__name__} at {Path(tb.tb_frame.f_code.co_filename).name}:{tb.tb_lineno}"


# -- the tick ---------------------------------------------------------------------------


def tick(
    *,
    state_dir: Path,
    now: float,
    config_file: Path,
    transport: PingTransport,
    sweep: Callable[[], SweepOutcome] | None,
    dry_run: bool = False,
) -> tuple[dict[str, Any], int]:
    """Sweep the alerts, then ping when due. (summary, exit code)."""
    summary: dict[str, Any] = {"schema": TICK_SCHEMA, "at": now, "dry_run": dry_run}
    degraded = False
    if sweep is None:
        summary["alerts"] = "skipped"
    else:
        try:
            outcome = sweep()
        except Exception as exc:
            print(f"deadman: alert sweep failed ({_where(exc)}); no ping sent", file=sys.stderr)
            summary["alerts"] = "failed"
            summary["heartbeat"] = "skipped"
            return summary, EXIT_SWEEP_FAILED
        summary["alerts"] = outcome.summary()
        degraded = outcome.degraded
    cfg, reason = load_config(config_file)
    if cfg is None:
        summary["heartbeat"] = f"disabled:{reason}"
        return summary, EXIT_DEGRADED if degraded else EXIT_OK
    state_path = state_dir / STATE_NAME
    prior = _read_state(state_path)
    if not heartbeat_due(prior, now, cfg.every_s):
        summary["heartbeat"] = "not_due"
    elif dry_run:
        summary["heartbeat"] = "due"
    else:
        state, ok = ping(cfg, prior, now, transport)
        summary["heartbeat"] = "sent" if ok else "failed"
        degraded = degraded or not ok
        try:
            _write_state(state_path, state)
            summary["state"] = "written"
        except OSError as exc:
            print(f"deadman: state not written ({type(exc).__name__})", file=sys.stderr)
            summary["state"] = "unwritable"
            degraded = True
    return summary, EXIT_DEGRADED if degraded else EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--state-dir", type=Path, help="default: <TREX_DESK_STATE>/alerts")
    parser.add_argument("--run-dir", type=Path, help="default: TREX_DESK_RUN_DIR")
    parser.add_argument("--supervised-dir", type=Path, help="default: TREX_SUPERVISED_DIR")
    parser.add_argument("--acceptance-dir", type=Path, help="default: TREX_ACCEPTANCE_DIR")
    parser.add_argument(
        "--deadman-config", type=Path, help=f"default: {CONFIG_ENV} or ~/.config/trex/{CONFIG_NAME}"
    )
    parser.add_argument(
        "--notify-config", type=Path, help="default: TREX_NOTIFY_ENV or ~/.config/trex/notify.env"
    )
    parser.add_argument("--no-alerts", action="store_true", help="heartbeat only")
    parser.add_argument(
        "--dry-run", action="store_true", help="observe and print; send, ping and write nothing"
    )
    args = parser.parse_args(argv)

    from tree_options.desk.paths import notify_env_path
    from tree_options.trex import alert_sweep, notify
    from tree_options.trex.alert_policy import load_quiet_hours
    from tree_options.trex.supervised import SupervisedPaths

    now = time.time()
    state_dir = args.state_dir or default_state_dir()
    notify_env = args.notify_config or notify_env_path()
    notify_cfg = notify.load_config(notify_env)
    quiet = load_quiet_hours(read_env(notify_env))
    paths = alert_sweep.SweepPaths(
        run_dir=args.run_dir or default_run_dir(),
        supervised_dir=args.supervised_dir or SupervisedPaths.default().root,
        acceptance_dir=args.acceptance_dir or default_acceptance_dir(),
        book=state_dir / ALERT_BOOK_NAME,
    )

    def send(title: str, message: str, priority: str) -> bool:
        return notify.send(notify_cfg, title, message, priority)

    def run_sweep() -> alert_sweep.SweepResult:
        return alert_sweep.sweep(
            paths,
            now=datetime.fromtimestamp(now, UTC),
            quiet=quiet,
            send=send,
            dry_run=args.dry_run,
        )

    summary, rc = tick(
        state_dir=state_dir,
        now=now,
        config_file=args.deadman_config or config_path(),
        transport=urllib_ping,
        sweep=None if args.no_alerts else run_sweep,
        dry_run=args.dry_run,
    )
    print(json.dumps(summary, sort_keys=True))
    return rc


if __name__ == "__main__":
    sys.exit(main())
