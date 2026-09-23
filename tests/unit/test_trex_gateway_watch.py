"""IB Gateway watchdog: detect a logged-out / stalled gateway, self-heal
within caps, and tell the operator.

Fixtures are the real IBC sequences from the 2026-09-22/23 incident: every
automated login after a daily restart stalled at "Setting password" (the
image read TWS_USERID, ib.env set TWS_USERNAME), and x11vnc died at boot
(XOpenDisplay raced Xvfb). Lines use `docker logs -t` stamps (UTC).
"""

from __future__ import annotations

import json
import re
import socket
import struct
import subprocess
import threading
from pathlib import Path
from typing import Any

import pytest

from tree_options.trex.alert_policy import NOTIFY_RETRY_S, REMIND_EVERY_S, REMIND_MARKET_S, Urgency
from tree_options.trex.gateway_watch import (
    API_DOWN_AFTER_S,
    NEEDS_LOGIN_AFTER_S,
    RESTART_COOLDOWN_S,
    RESTARTS_PER_DAY,
    STARTUP_GRACE_S,
    VNC_STATE_SCRIPT,
    IbcState,
    Observation,
    decide,
    parse_ibc_log,
    parse_ts,
    probe_api,
    watch_once,
    x11vnc_script,
)

DEPLOY = Path(__file__).resolve().parents[2] / "deploy" / "trex"

T0 = parse_ts("2026-09-22T23:45:02Z")
assert T0 is not None

STALLED = [
    "2026-09-22T23:45:00.571000000Z 2026-09-22 23:45:00:571 IBC: detected dialog entitled: Shutdown progress; event=Opened",
    "2026-09-22T23:45:02.670000000Z 2026-09-22 23:45:02:670 IBC: Starting session: will exit if login dialog is not displayed within 60 seconds",
    "2026-09-22T23:45:09.837000000Z 2026-09-22 23:45:09:837 IBC: Login dialog WINDOW_OPENED: LoginState is LOGGED_OUT",
    "2026-09-22T23:45:09.925000000Z 2026-09-22 23:45:09:925 IBC: Setting user name",
    "2026-09-22T23:45:49.000000000Z 2026/09/22 23:45:49 socat[402] E connect(5, AF=2 127.0.0.1:4002, 16): Connection refused",
]
COMPLETED = [
    *STALLED[:4],
    "2026-09-23T16:18:01.265000000Z 2026-09-23 16:18:01:265 IBC: detected frame entitled: Authenticating...; event=Opened",
    "2026-09-23T16:18:04.572000000Z 2026-09-23 16:18:04:572 IBC: Login has completed",
]
TWOFA = [
    *STALLED[:4],
    "2026-09-22T23:45:12.000000000Z 2026-09-22 23:45:12:000 IBC: detected frame entitled: Authenticating...; event=Opened",
    "2026-09-22T23:45:14.000000000Z 2026-09-22 23:45:14:000 IBC: detected dialog entitled: Second Factor Authentication; event=Opened",
]
# IBC 3.24 SessionManager logs "Re-starting session" on an auto-restart.
# Synthetic: no warm restart has run with AUTO_RESTART_TIME set yet.
WARM = [
    *COMPLETED,
    "2026-09-24T03:45:00.500000000Z 2026-09-23 23:45:00:500 IBC: detected dialog entitled: Shutdown progress; event=Opened",
    "2026-09-24T03:45:04.100000000Z 2026-09-23 23:45:04:100 IBC: Re-starting session",
]


def _obs(**over: object) -> Observation:
    base: dict[str, object] = {
        "now": T0 + 3600,
        "container_running": True,
        "container_started": T0 - 5 * 86_400,
        "api_ok": False,
        "api_detail": "closed before reply",
        "ibc": IbcState("login_dialog", parse_ts("2026-09-22T23:45:09Z")),
        "vnc_running": True,
    }
    base.update(over)
    return Observation(**base)  # type: ignore[arg-type]


class TestParseIbcLog:
    def test_stall_after_credentials_is_an_open_login_dialog(self) -> None:
        state = parse_ibc_log(STALLED)
        assert state.phase == "login_dialog"
        assert state.since == parse_ts("2026-09-22T23:45:09Z")

    def test_completed_login(self) -> None:
        state = parse_ibc_log(COMPLETED)
        assert state.phase == "logged_in"
        assert state.since == parse_ts("2026-09-23T16:18:04Z")

    def test_second_factor_prompt(self) -> None:
        assert parse_ibc_log(TWOFA).phase == "twofa"

    def test_no_ibc_lines_is_unknown(self) -> None:
        assert parse_ibc_log(["garbage", STALLED[-1]]) == IbcState("unknown", None)

    def test_warm_restart_is_a_fresh_session(self) -> None:
        """Codex P2: only "Starting session" was recognised, so an
        auto-restart kept the shutdown phase and its date."""
        assert parse_ibc_log(WARM) == IbcState("starting", parse_ts("2026-09-24T03:45:04Z"))

    def test_every_session_start_redates_the_phase(self) -> None:
        lines = [STALLED[1],
                 "2026-09-23T03:45:04.100000000Z 2026-09-22 23:45:04:100 IBC: Re-starting session"]
        assert parse_ibc_log(lines) == IbcState("starting", parse_ts("2026-09-23T03:45:04Z"))


class TestProbeApi:
    def _server(self, reply: bytes | None) -> tuple[int, threading.Thread]:
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]

        def run() -> None:
            conn, _ = srv.accept()
            conn.recv(64)
            if reply is not None:
                conn.sendall(reply)
            conn.close()
            srv.close()

        t = threading.Thread(target=run, daemon=True)
        t.start()
        return port, t

    def test_server_version_reply_is_healthy(self) -> None:
        body = b"187\x0020260923 12:18:54 EST\x00"
        port, t = self._server(struct.pack(">I", len(body)) + body)
        ok, detail = probe_api("127.0.0.1", port, timeout=2.0)
        t.join(2)
        assert ok and "server_version=187" in detail

    def test_accept_then_close_is_the_logged_out_signature(self) -> None:
        """socat on 4004 accepts, finds nothing on the gateway's 4002, closes."""
        port, t = self._server(None)
        ok, detail = probe_api("127.0.0.1", port, timeout=2.0)
        t.join(2)
        assert not ok and "closed" in detail

    def test_refused(self) -> None:
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        assert probe_api("127.0.0.1", port, timeout=1.0)[0] is False


class TestClassifyAndDecide:
    def test_api_answering_is_ok_and_quiet(self) -> None:
        state, actions = decide(_obs(api_ok=True, api_detail="server_version=187",
                                     ibc=parse_ibc_log(COMPLETED)), {})
        assert state["status"] == "ok"
        assert actions == []

    def test_fresh_login_dialog_is_starting_not_an_alarm(self) -> None:
        since = parse_ts("2026-09-22T23:45:09Z")
        assert since is not None
        state, actions = decide(_obs(now=since + 60), {})
        assert state["status"] == "starting"
        assert not any(a.kind in ("notify", "restart_gateway") for a in actions)

    def test_stuck_login_dialog_needs_login_notifies_and_retries(self) -> None:
        since = parse_ts("2026-09-22T23:45:09Z")
        assert since is not None
        state, actions = decide(_obs(now=since + NEEDS_LOGIN_AFTER_S + 1), {})
        assert state["status"] == "needs_login"
        assert state["since"] == since  # dated from the dialog, not from detection
        kinds = [a.kind for a in actions]
        assert "restart_gateway" in kinds and "notify" in kinds
        note = next(a for a in actions if a.kind == "notify")
        assert "needs login" in note.title.lower() and note.priority == "high"
        assert "http" not in note.message  # no hostnames/URLs leave the box

    def test_restart_cap_cooldown_and_daily_limit(self) -> None:
        now = T0 + 5 * 3600
        prior = {"status": "needs_login", "since": T0, "restarts": [now - 600],
                 "last_notified_status": "needs_login", "last_notified_at": now - 600}
        _, actions = decide(_obs(now=now), prior)
        assert "restart_gateway" not in [a.kind for a in actions]  # inside cooldown
        prior["restarts"] = [now - RESTART_COOLDOWN_S - 1]
        _, actions = decide(_obs(now=now), prior)
        assert "restart_gateway" in [a.kind for a in actions]
        spaced = [now - RESTART_COOLDOWN_S * (i + 1) - 1 for i in range(RESTARTS_PER_DAY)]
        prior["restarts"] = [t for t in spaced if now - t < 86_400]
        assert len(prior["restarts"]) == RESTARTS_PER_DAY
        state, actions = decide(_obs(now=now), prior)
        assert "restart_gateway" not in [a.kind for a in actions]  # 4/day used
        assert state["restarts_left"] == 0

    def test_same_bad_status_does_not_renotify_until_reminder(self) -> None:
        now = T0 + 3 * 3600
        prior = {"status": "needs_login", "since": T0, "restarts": [now - 60],
                 "last_notified_status": "needs_login", "last_notified_at": now - 120}
        _, actions = decide(_obs(now=now), prior)
        assert "notify" not in [a.kind for a in actions]
        prior["last_notified_at"] = now - 5 * 3600
        _, actions = decide(_obs(now=now), prior)
        assert "notify" in [a.kind for a in actions]

    def test_recovery_notifies_once(self) -> None:
        prior = {"status": "needs_login", "since": T0, "last_notified_status": "needs_login",
                 "last_notified_at": T0}
        state, actions = decide(_obs(api_ok=True, ibc=parse_ibc_log(COMPLETED)), prior)
        assert state["status"] == "ok"
        notes = [a for a in actions if a.kind == "notify"]
        assert len(notes) == 1 and "back" in notes[0].title.lower()
        _, again = decide(_obs(api_ok=True, ibc=parse_ibc_log(COMPLETED)), state)
        assert again == []

    def test_second_factor_prompt_asks_for_approval(self) -> None:
        state, actions = decide(_obs(ibc=parse_ibc_log(TWOFA)), {})
        assert state["status"] == "needs_2fa"
        assert any(a.kind == "notify" and "2FA" in a.title for a in actions)
        assert "restart_gateway" not in [a.kind for a in actions]  # IBC re-prompts itself

    def test_missing_vnc_is_restarted_with_backoff(self) -> None:
        state, actions = decide(_obs(api_ok=True, ibc=parse_ibc_log(COMPLETED),
                                     vnc_running=False), {})
        assert [a.kind for a in actions] == ["restart_vnc"]
        _, again = decide(_obs(api_ok=True, ibc=parse_ibc_log(COMPLETED), vnc_running=False,
                               now=T0 + 3600 + 60), state)
        assert "restart_vnc" not in [a.kind for a in again]  # 5 min backoff

    def test_container_down(self) -> None:
        state, actions = decide(_obs(container_running=False, vnc_running=None), {})
        assert state["status"] == "down"
        assert "notify" in [a.kind for a in actions]
        assert "restart_vnc" not in [a.kind for a in actions]

    def test_logged_in_but_api_silent_after_grace_is_api_down(self) -> None:
        done = parse_ts("2026-09-23T16:18:04Z")
        assert done is not None
        early, _ = decide(_obs(now=done + 30, ibc=parse_ibc_log(COMPLETED)), {})
        assert early["status"] == "starting"
        failing = {"status": "checking", "since": done + STARTUP_GRACE_S, "api_ok": False,
                   "api_fail_since": done + STARTUP_GRACE_S}
        late, actions = decide(_obs(now=done + STARTUP_GRACE_S + API_DOWN_AFTER_S,
                                    ibc=parse_ibc_log(COMPLETED)), failing)
        assert late["status"] == "api_down"
        assert "restart_gateway" in [a.kind for a in actions]

    def test_one_failed_probe_on_a_healthy_gateway_is_rechecked_not_restarted(self) -> None:
        """Codex P1: one handshake timeout after the startup grace went
        straight to api_down and a container restart."""
        done = parse_ts("2026-09-23T16:18:04Z")
        assert done is not None
        healthy = {"status": "ok", "since": done, "api_ok": True, "api_fail_since": None}
        state, actions = decide(_obs(now=done + 3600, ibc=parse_ibc_log(COMPLETED)), healthy)
        assert state["status"] == "checking"
        assert actions == []  # no restart, no push for a blip
        still, actions = decide(_obs(now=done + 3600 + API_DOWN_AFTER_S - 60,
                                     ibc=parse_ibc_log(COMPLETED)), state)
        assert still["status"] == "checking" and actions == []
        assert still["api_fail_since"] == done + 3600  # dated from the first miss

    def test_phase_aged_out_of_the_log_window_is_carried_forward(self) -> None:
        """Codex P2: the phase came only from the last 26h of logs, so a 2FA
        wait whose marker aged out read as "unknown" and became restartable."""
        prior = {"status": "needs_2fa", "since": T0, "ibc_phase": "twofa", "ibc_since": T0,
                 "container_started": T0 - 5 * 86_400, "api_ok": False, "api_fail_since": T0}
        state, actions = decide(_obs(ibc=IbcState("unknown", None)), prior)
        assert state["status"] == "needs_2fa"
        assert (state["ibc_phase"], state["ibc_since"]) == ("twofa", T0)
        assert "restart_gateway" not in [a.kind for a in actions]

    def test_a_new_container_does_not_inherit_the_old_phase(self) -> None:
        prior = {"ibc_phase": "twofa", "ibc_since": T0, "container_started": T0 - 9 * 86_400}
        state, _ = decide(_obs(ibc=IbcState("unknown", None)), prior)
        assert state["ibc_phase"] == "unknown"


class FakeDocker:
    """``lines=None`` = the log read failed (timeout / docker error)."""

    def __init__(self, lines: list[str] | None, running: bool = True,
                 vnc: bool | None = True) -> None:
        self.lines, self.running, self.vnc = lines, running, vnc
        self.calls: list[str] = []

    def inspect(self) -> tuple[bool, float | None]:
        return self.running, T0 - 86_400

    def ibc_lines(self, since: float) -> list[str] | None:
        return self.lines

    def vnc_running(self) -> bool | None:
        return self.vnc

    def start_vnc(self) -> bool:
        self.calls.append("start_vnc")
        return True

    def restart(self) -> bool:
        self.calls.append("restart")
        return True


class TestWatchOnce:
    def test_stuck_gateway_end_to_end(self, tmp_path: Path) -> None:
        sent: list[tuple[str, str, str]] = []
        docker = FakeDocker(STALLED, vnc=False)
        state_path = tmp_path / "gateway.json"
        state = watch_once(
            state_path, docker, probe=lambda: (False, "closed before reply"),
            notify=lambda t, m, p: sent.append((t, m, p)) or True,
            now=T0 + 3600, login_url="https://example.invalid/vnc.html?path=vnc",
        )
        assert state["status"] == "needs_login"
        assert docker.calls == ["start_vnc", "restart"]
        assert len(sent) == 1
        saved = json.loads(state_path.read_text())
        assert saved["login_url"].endswith("path=vnc")
        assert [e["kind"] for e in saved["events"]][-3:] == ["restart_vnc", "restart_gateway",
                                                            "notify"]
        assert "Setting user name" not in state_path.read_text()  # raw log never persisted

    def test_dry_run_changes_nothing(self, tmp_path: Path) -> None:
        docker = FakeDocker(STALLED, vnc=False)
        sent: list[object] = []
        watch_once(tmp_path / "g.json", docker, probe=lambda: (False, "x"),
                   notify=lambda *a: sent.append(a) or True, now=T0 + 3600,
                   login_url="u", dry_run=True)
        assert docker.calls == [] and sent == []

    def test_corrupt_prior_state_is_tolerated(self, tmp_path: Path) -> None:
        path = tmp_path / "g.json"
        path.write_text("{not json")
        state = watch_once(path, FakeDocker(COMPLETED), probe=lambda: (True, "ok"),
                           notify=lambda *a: True, now=T0 + 3600, login_url="u")
        assert state["status"] == "ok"

    def test_corrupt_ledger_holds_restarts_for_a_cooldown(self, tmp_path: Path) -> None:
        """Codex P1: an unreadable ledger reset the restart budget to full."""
        path = tmp_path / "g.json"
        path.write_text("{not json")
        docker = FakeDocker(STALLED)
        state = watch_once(path, docker, probe=lambda: (False, "closed"),
                           notify=lambda *a: True, now=T0 + 3600, login_url="u")
        assert state["status"] == "needs_login"
        assert "restart" not in docker.calls
        assert state["restart_hold_until"] == T0 + 3600 + RESTART_COOLDOWN_S

    def test_restart_is_reserved_on_disk_before_docker_is_called(self, tmp_path: Path) -> None:
        """Codex P1: the ledger was written after the restart, so a failed
        write or a killed run left the budget unspent."""
        path = tmp_path / "g.json"
        seen: dict[str, Any] = {}

        class Recording(FakeDocker):
            def restart(self) -> bool:
                seen["restarts"] = json.loads(path.read_text())["restarts"]
                return super().restart()

        watch_once(path, Recording(STALLED), probe=lambda: (False, "closed"),
                   notify=lambda *a: True, now=T0 + 3600, login_url="u")
        assert seen["restarts"] == [T0 + 3600]

    def test_unwritable_ledger_refuses_the_restart(self, tmp_path: Path) -> None:
        docker = FakeDocker(STALLED)

        def no_disk(path: Path, state: dict[str, Any]) -> None:
            raise OSError("No space left on device")

        with pytest.raises(OSError):
            watch_once(tmp_path / "g.json", docker, probe=lambda: (False, "closed"),
                       notify=lambda *a: True, now=T0 + 3600, login_url="u",
                       write_state=no_disk)
        assert "restart" not in docker.calls

    def test_restart_is_cancelled_when_the_api_answers_on_recheck(self, tmp_path: Path) -> None:
        answers = iter([(False, "closed before reply"), (True, "server_version=187")])
        docker = FakeDocker(STALLED)
        state = watch_once(tmp_path / "g.json", docker, probe=lambda: next(answers),
                           notify=lambda *a: True, now=T0 + 3600, login_url="u")
        assert "restart" not in docker.calls
        assert state["restarts"] == [] and state["restarts_left"] == RESTARTS_PER_DAY
        assert "restart_skipped" in [e["kind"] for e in state["events"]]

    def test_failed_log_read_never_restarts(self, tmp_path: Path) -> None:
        """Codex P2: a timed-out `docker logs` returned [] = phase unknown, so
        a gateway known to be waiting on 2FA could be restarted."""
        path = tmp_path / "g.json"
        path.write_text(json.dumps({
            "status": "api_down", "since": T0, "ibc_phase": "logged_in", "ibc_since": T0 - 3600,
            "container_started": T0 - 86_400, "api_ok": False, "api_fail_since": T0,
        }))
        docker = FakeDocker(None)
        state = watch_once(path, docker, probe=lambda: (False, "closed"),
                           notify=lambda *a: True, now=T0 + 3600, login_url="u")
        assert state["status"] == "api_down"
        assert state["ibc_observed"] is False
        assert "restart" not in docker.calls

    def test_failed_push_is_retried_not_marked_delivered(self, tmp_path: Path) -> None:
        """Codex P2: a failed push advanced last_notified_* and suppressed the
        alert for 4 hours."""
        path = tmp_path / "g.json"
        first = watch_once(path, FakeDocker(STALLED), probe=lambda: (False, "closed"),
                           notify=lambda *a: False, now=T0 + 3600, login_url="u")
        assert first["last_notified_status"] is None
        sent: list[tuple[str, str, str]] = []

        def record(t: str, m: str, p: str) -> bool:
            sent.append((t, m, p))
            return True

        watch_once(path, FakeDocker(STALLED), probe=lambda: (False, "closed"),
                   notify=record, now=T0 + 3660, login_url="u")
        assert sent == []  # inside the retry backoff
        after = watch_once(path, FakeDocker(STALLED), probe=lambda: (False, "closed"),
                           notify=record, now=T0 + 3600 + NOTIFY_RETRY_S, login_url="u")
        assert len(sent) == 1 and after["last_notified_status"] == "needs_login"


def test_worst_case_run_fits_the_service_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Codex P1: sequential docker timeouts (270 s) outran the unit's
    TimeoutStartSec, so systemd could kill a run after docker accepted a
    restart but before the ledger was written."""
    from tree_options.trex import gateway_watch, notify

    timeouts: list[float] = []

    def fake_run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        timeouts.append(kw["timeout"])
        return subprocess.CompletedProcess(cmd, 0, "true|2026-09-23T16:18:04Z", "")

    monkeypatch.setattr(gateway_watch.subprocess, "run", fake_run)
    d = gateway_watch.Docker()
    d.inspect(), d.ibc_lines(0), d.vnc_running(), d.start_vnc(), d.restart()
    probes = 2 * 2 * gateway_watch.PROBE_TIMEOUT_S  # first probe + pre-restart recheck
    worst = sum(timeouts) + probes + notify.TIMEOUT_S
    unit = (DEPLOY / "trex-gateway-watch.service").read_text()
    m = re.search(r"TimeoutStartSec=(\d+)", unit)
    assert m is not None
    assert worst + 30 <= int(m.group(1)), (worst, m.group(1))


def _stub(bin_dir: Path, name: str, body: str) -> None:
    path = bin_dir / name
    path.write_text("#!/bin/sh\n" + body + "\n")
    path.chmod(0o755)


class TestVncScripts:
    """Codex P1: the x11vnc restart passed ``-passwd "$VNC_SERVER_PASSWORD"``
    even when that was unset (an open desktop, overriding an intentionally
    disabled VNC) and ignored VNC_SERVER_PASSWORD_FILE. Run for real under
    sh with stub x11vnc/pgrep; nothing touches a container."""

    def _sh(self, tmp_path: Path, script: str, **env: str) -> subprocess.CompletedProcess[str]:
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir(exist_ok=True)
        _stub(bin_dir, "x11vnc", 'printf "%s\\n" "$@" > "$STUB_OUT"')
        _stub(bin_dir, "pgrep", 'exit "${PGREP_RC:-1}"')
        full = {"PATH": f"{bin_dir}:/usr/bin:/bin", "STUB_OUT": str(tmp_path / "x11vnc.args"),
                **env}
        return subprocess.run(["sh", "-c", script], env=full, capture_output=True, text=True,
                              timeout=10, check=False)

    def _args(self, tmp_path: Path) -> list[str]:
        return (tmp_path / "x11vnc.args").read_text().splitlines()

    def test_empty_password_refuses_to_start(self, tmp_path: Path) -> None:
        out = self._sh(tmp_path, x11vnc_script(str(tmp_path)), VNC_SERVER_PASSWORD="")
        assert out.returncode != 0
        assert not (tmp_path / "x11vnc.args").exists()

    def test_password_from_env_or_file(self, tmp_path: Path) -> None:
        out = self._sh(tmp_path, x11vnc_script(str(tmp_path)), VNC_SERVER_PASSWORD="pw-env")
        assert out.returncode == 0
        args = self._args(tmp_path)
        assert args[args.index("-passwd") + 1] == "pw-env"
        assert args[args.index("-display") + 1] == ":1"
        secret = tmp_path / "vnc.secret"
        secret.write_text("pw-file\n")
        out = self._sh(tmp_path, x11vnc_script(str(tmp_path)),
                       VNC_SERVER_PASSWORD_FILE=str(secret))
        assert out.returncode == 0
        args = self._args(tmp_path)
        assert args[args.index("-passwd") + 1] == "pw-file"

    @pytest.mark.parametrize(
        ("rc", "env", "expected"),
        [("0", {}, "up"), ("1", {"VNC_SERVER_PASSWORD": "x"}, "down"), ("1", {}, "off")],
    )
    def test_vnc_state_tells_disabled_from_dead(
        self, tmp_path: Path, rc: str, env: dict[str, str], expected: str
    ) -> None:
        out = self._sh(tmp_path, VNC_STATE_SCRIPT, PGREP_RC=rc, **env)
        assert out.stdout.strip() == expected


class TestWaitForApi:
    """trex-monitor's ExecStartPre: start the exit machine the moment the
    API answers instead of crash-looping every 35s (1,276 restarts)."""

    def test_returns_as_soon_as_the_api_answers(self) -> None:
        from tree_options.trex.gateway_watch import wait_for_api

        answers = iter([(False, "closed"), (False, "closed"), (True, "server_version=187")])
        clock = [0.0]
        sleeps: list[float] = []

        def sleep(s: float) -> None:
            sleeps.append(s)
            clock[0] += s

        assert wait_for_api(lambda: next(answers), 300, sleep=sleep, clock=lambda: clock[0])
        assert sleeps == [5.0, 5.0]

    def test_gives_up_at_the_deadline(self) -> None:
        from tree_options.trex.gateway_watch import wait_for_api

        clock = [0.0]

        def sleep(s: float) -> None:
            clock[0] += s

        assert not wait_for_api(lambda: (False, "x"), 30, sleep=sleep, clock=lambda: clock[0])
        assert clock[0] <= 35


def test_monitor_unit_waits_for_the_api_before_starting() -> None:
    root = Path(__file__).resolve().parents[2] / "deploy" / "trex"
    unit = (root / "trex-monitor.service").read_text()
    assert "ExecStartPre=" in unit and "--wait-api" in unit
    assert "TimeoutStartSec=" in unit
    timer = (root / "trex-gateway-watch.timer").read_text()
    assert "OnUnitActiveSec=" in timer


def test_env_example_uses_the_image_variable_names() -> None:
    """Root cause 2026-09-22: the image renders IbLoginId from TWS_USERID;
    ib.env set TWS_USERNAME, so every automated login typed a blank user."""
    root = Path(__file__).resolve().parents[2] / "deploy" / "trex"
    names = {line.split("=", 1)[0] for line in (root / "ib.env.example").read_text().splitlines()
             if "=" in line and not line.startswith("#")}
    assert {"TWS_USERID", "TWS_PASSWORD"} <= names
    compose = (root / "docker-compose.yml").read_text()
    for setting in ("AUTO_RESTART_TIME", "TWOFA_TIMEOUT_ACTION", "RELOGIN_AFTER_TWOFA_TIMEOUT",
                    "EXISTING_SESSION_DETECTED_ACTION", "TZ: America/New_York"):
        assert setting in compose, setting


@pytest.mark.parametrize("stamp", ["2026-09-23T16:18:04.572123456Z", "2026-09-23T16:18:04Z"])
def test_parse_ts_accepts_docker_stamps(stamp: str) -> None:
    assert parse_ts(stamp) == parse_ts("2026-09-23T16:18:04Z")


class TestUrgency:
    """Operator 2026-09-23: nothing buzzes at night; market hours with open
    positions are loud and remind hourly. Self-heal is never quiet."""

    HUSH = Urgency("default", REMIND_EVERY_S, quiet=True)
    DAY = Urgency("default", REMIND_EVERY_S, quiet=False)
    LOUD = Urgency("high", REMIND_MARKET_S, quiet=False)

    def _stuck_at(self) -> float:
        since = parse_ts("2026-09-22T23:45:09Z")
        assert since is not None
        return since + NEEDS_LOGIN_AFTER_S + 1

    def test_quiet_hours_hold_the_push_but_still_self_heal(self) -> None:
        now = self._stuck_at()
        state, actions = decide(_obs(now=now), {}, urgency=self.HUSH)
        kinds = [a.kind for a in actions]
        assert state["status"] == "needs_login" and state["notify_held"] is True
        assert "notify" not in kinds and "restart_gateway" in kinds
        _, morning = decide(_obs(now=now + 3600), state, urgency=self.DAY)
        note = next(a for a in morning if a.kind == "notify")
        assert note.priority == "default"

    def test_market_hours_with_positions_is_high_and_hourly(self) -> None:
        now = self._stuck_at() + 5 * 3600
        prior = {"status": "needs_login", "since": T0, "restarts": [now - 60],
                 "last_notified_status": "needs_login", "last_notified_at": now - REMIND_MARKET_S}
        _, actions = decide(_obs(now=now), prior, urgency=self.LOUD)
        assert [a.priority for a in actions if a.kind == "notify"] == ["high"]
        _, actions = decide(_obs(now=now), prior, urgency=self.DAY)
        assert "notify" not in [a.kind for a in actions]  # 4 h cadence off-market

    def test_recovery_after_a_held_alarm_is_silent(self) -> None:
        prior = {"status": "needs_login", "since": T0, "last_notified_status": None,
                 "notify_held": True}
        _, actions = decide(_obs(api_ok=True, ibc=parse_ibc_log(COMPLETED)), prior,
                            urgency=self.DAY)
        assert actions == []
