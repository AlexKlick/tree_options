"""Dead-man heartbeat (Wave 4 lane J), with a fake transport.

The heartbeat is the one alarm that still works when this host is down: an
external check the owner configured alerts them when the pings STOP. Rules:
off by default (no URL committed anywhere), the URL is read from a config
file by name and never logged, the ping carries no account data, a failed
ping is logged and waits for the next interval (never retried in a loop),
and the ping only goes out after the alert sweep completed.
"""

from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from tree_options.trex import deadman as dm
from tree_options.trex import release

REPO = Path(__file__).resolve().parents[2]
URL = "https://hc-ping.example/SECRET-check-uuid-123"
NOW = 1_792_000_000.0  # an epoch second; only differences matter here


@dataclass
class Transport:
    status: int = 200
    exc: Exception | None = None
    calls: list[tuple[str, float]] = field(default_factory=list)

    def __call__(self, url: str, timeout: float) -> int:
        self.calls.append((url, timeout))
        if self.exc is not None:
            raise self.exc
        return self.status


@dataclass
class FakeSweep:
    degraded: bool = False
    exc: Exception | None = None
    runs: int = 0

    def __call__(self) -> Any:
        self.runs += 1
        if self.exc is not None:
            raise self.exc
        return _SweepResult(degraded=self.degraded)


@dataclass
class _SweepResult:
    degraded: bool

    def summary(self) -> dict[str, Any]:
        return {"degraded": self.degraded}


def _config(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "deadman.env"
    path.write_text(text)
    return path


def _tick(tmp_path: Path, cfg: Path, transport: Transport, now: float = NOW, **kw: Any) -> Any:
    return dm.tick(
        state_dir=tmp_path / "state",
        now=now,
        config_file=cfg,
        transport=transport,
        sweep=kw.pop("sweep", FakeSweep()),
        **kw,
    )


# -- configuration --------------------------------------------------------------------


def test_unconfigured_is_disabled_by_default(tmp_path: Path) -> None:
    cfg, reason = dm.load_config(tmp_path / "missing.env")
    assert cfg is None and reason == "deadman_not_configured"
    transport = Transport()
    summary, rc = _tick(tmp_path, tmp_path / "missing.env", transport)
    assert transport.calls == [] and rc == 0
    assert summary["heartbeat"] == "disabled:deadman_not_configured"


def test_an_empty_url_is_not_configured(tmp_path: Path) -> None:
    cfg, reason = dm.load_config(_config(tmp_path, "DEADMAN_URL=\n"))
    assert cfg is None and reason == "deadman_not_configured"


def test_a_non_https_url_is_refused(tmp_path: Path) -> None:
    cfg, reason = dm.load_config(_config(tmp_path, "DEADMAN_URL=http://hc-ping.example/x\n"))
    assert cfg is None and reason == "deadman_url_not_https"


def test_config_parsing_and_interval(tmp_path: Path) -> None:
    cfg, reason = dm.load_config(_config(tmp_path, f"# owner check\nDEADMAN_URL='{URL}'\n"))
    assert reason == "ok" and cfg is not None
    assert cfg.url == URL and cfg.every_s == dm.DEFAULT_EVERY_S == 300
    cfg, _ = dm.load_config(_config(tmp_path, f"DEADMAN_URL={URL}\nDEADMAN_EVERY_S=600\n"))
    assert cfg is not None and cfg.every_s == 600


@pytest.mark.parametrize(
    ("raw", "every"), [("5", 60), ("99999", 3600), ("soon", 300), ("-1", 60), ("120.5", 300)]
)
def test_the_interval_is_clamped(tmp_path: Path, raw: str, every: int) -> None:
    cfg, _ = dm.load_config(_config(tmp_path, f"DEADMAN_URL={URL}\nDEADMAN_EVERY_S={raw}\n"))
    assert cfg is not None and cfg.every_s == every


def test_the_config_never_shows_its_url(tmp_path: Path) -> None:
    cfg, _ = dm.load_config(_config(tmp_path, f"DEADMAN_URL={URL}\n"))
    assert "SECRET" not in repr(cfg) and "SECRET" not in str(cfg)


def test_the_config_file_is_found_by_name_at_call_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    assert dm.config_path() == tmp_path / ".config" / "trex" / "deadman.env"
    monkeypatch.setenv("TREX_DEADMAN_CONFIG", str(tmp_path / "other.env"))
    assert dm.config_path() == tmp_path / "other.env"


def test_no_url_is_committed_in_the_repo() -> None:
    """Default disabled: the templates and the module name the file, never a URL."""
    for path in (
        REPO / "src/tree_options/trex/deadman.py",
        REPO / "deploy/product/trex-deadman.service.in",
        REPO / "deploy/product/trex-deadman.timer.in",
    ):
        text = path.read_text()
        assert "DEADMAN_URL=https" not in text, path.name
        assert "hc-ping.com/" not in text and "/ping/" not in text, path.name


# -- the ping ---------------------------------------------------------------------------


def test_the_ping_is_a_bare_get_of_the_configured_url(tmp_path: Path) -> None:
    transport = Transport()
    summary, rc = _tick(tmp_path, _config(tmp_path, f"DEADMAN_URL={URL}\n"), transport)
    assert transport.calls == [(URL, dm.TIMEOUT_S)]
    assert summary["heartbeat"] == "sent" and rc == 0


def test_urllib_ping_sends_no_body_and_no_extra_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[urllib.request.Request] = []

    class Response:
        status = 200

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

    def fake_urlopen(req: urllib.request.Request, timeout: float) -> Response:
        seen.append(req)
        return Response()

    monkeypatch.setattr(dm.urllib.request, "urlopen", fake_urlopen)
    assert dm.urllib_ping(URL, 3.0) == 200
    (req,) = seen
    assert req.get_method() == "GET" and req.data is None
    assert req.full_url == URL
    assert {k.lower() for k in req.headers} == {"user-agent"}
    assert req.headers["User-agent"] == "trex-deadman/1"


def test_a_failed_ping_is_logged_without_the_url_and_waits_for_the_next_interval(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = _config(tmp_path, f"DEADMAN_URL={URL}\nDEADMAN_EVERY_S=300\n")
    down = Transport(exc=OSError(f"connect to {URL} refused"))
    summary, rc = _tick(tmp_path, cfg, down)
    assert summary["heartbeat"] == "failed" and rc == 3
    err = capsys.readouterr().err
    assert "OSError" in err and "SECRET" not in err and "hc-ping" not in err
    # the timer fires every ~30 s: nothing until the interval has passed
    for later in (30, 60, 299):
        assert _tick(tmp_path, cfg, down, now=NOW + later)[0]["heartbeat"] == "not_due"
    assert len(down.calls) == 1
    up = Transport()
    assert _tick(tmp_path, cfg, up, now=NOW + 300)[0]["heartbeat"] == "sent"
    assert len(up.calls) == 1


def test_an_http_error_status_is_a_failed_ping(tmp_path: Path) -> None:
    cfg = _config(tmp_path, f"DEADMAN_URL={URL}\n")
    summary, rc = _tick(tmp_path, cfg, Transport(status=503))
    assert summary["heartbeat"] == "failed" and rc == 3
    state = json.loads((tmp_path / "state" / dm.STATE_NAME).read_text())
    assert state["last_ok"] is False and state["last_error"] == "HTTP 503"


def test_a_sent_ping_waits_for_the_interval(tmp_path: Path) -> None:
    cfg = _config(tmp_path, f"DEADMAN_URL={URL}\nDEADMAN_EVERY_S=120\n")
    transport = Transport()
    _tick(tmp_path, cfg, transport)
    _tick(tmp_path, cfg, transport, now=NOW + 119)
    _tick(tmp_path, cfg, transport, now=NOW + 120)
    assert len(transport.calls) == 2


def test_a_clock_that_went_backwards_makes_the_ping_due(tmp_path: Path) -> None:
    cfg = _config(tmp_path, f"DEADMAN_URL={URL}\n")
    transport = Transport()
    _tick(tmp_path, cfg, transport)
    _tick(tmp_path, cfg, transport, now=NOW - 3600)
    assert len(transport.calls) == 2


def test_heartbeat_due_rules() -> None:
    assert dm.heartbeat_due({}, NOW, 300)
    assert dm.heartbeat_due({"last_attempt_at": "garbage"}, NOW, 300)
    assert not dm.heartbeat_due({"last_attempt_at": NOW - 299}, NOW, 300)
    assert dm.heartbeat_due({"last_attempt_at": NOW - 300}, NOW, 300)
    assert dm.heartbeat_due({"last_attempt_at": NOW + 1}, NOW, 300)


def test_the_state_file_never_records_the_url(tmp_path: Path) -> None:
    _tick(tmp_path, _config(tmp_path, f"DEADMAN_URL={URL}\n"), Transport())
    text = (tmp_path / "state" / dm.STATE_NAME).read_text()
    assert "SECRET" not in text and "hc-ping" not in text
    state = json.loads(text)
    assert state["schema"] == "trex-deadman/1"
    assert state["last_ok"] is True and state["last_ok_at"] == NOW


# -- the tick: sweep first, then the ping --------------------------------------------------


def test_the_sweep_runs_before_the_ping(tmp_path: Path) -> None:
    order: list[str] = []
    transport = Transport()

    def sweep() -> Any:
        order.append("sweep")
        assert transport.calls == []
        return _SweepResult(degraded=False)

    def ping(url: str, timeout: float) -> int:
        order.append("ping")
        return transport(url, timeout)

    dm.tick(
        state_dir=tmp_path / "state",
        now=NOW,
        config_file=_config(tmp_path, f"DEADMAN_URL={URL}\n"),
        transport=ping,
        sweep=sweep,
    )
    assert order == ["sweep", "ping"]


def test_a_failed_sweep_suppresses_the_ping(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The external check then alarms: the alert path itself is broken."""
    transport = Transport()
    boom = FakeSweep(exc=RuntimeError(f"account DU0000001 at {URL}"))
    summary, rc = _tick(tmp_path, _config(tmp_path, f"DEADMAN_URL={URL}\n"), transport, sweep=boom)
    assert transport.calls == [] and rc == 1
    assert summary["alerts"] == "failed" and summary["heartbeat"] == "skipped"
    err = capsys.readouterr().err
    assert "RuntimeError" in err and "DU0000001" not in err and "SECRET" not in err


def test_a_degraded_sweep_still_pings_and_reports_degraded(tmp_path: Path) -> None:
    transport = Transport()
    summary, rc = _tick(
        tmp_path,
        _config(tmp_path, f"DEADMAN_URL={URL}\n"),
        transport,
        sweep=FakeSweep(degraded=True),
    )
    assert len(transport.calls) == 1 and rc == 3
    assert summary["heartbeat"] == "sent" and summary["alerts"] == {"degraded": True}


def test_dry_run_pings_nothing_and_writes_nothing(tmp_path: Path) -> None:
    transport = Transport()
    summary, rc = _tick(
        tmp_path, _config(tmp_path, f"DEADMAN_URL={URL}\n"), transport, dry_run=True
    )
    assert transport.calls == [] and summary["heartbeat"] == "due"
    assert not (tmp_path / "state").exists() and rc == 0


def test_without_a_sweep_the_ping_still_goes(tmp_path: Path) -> None:
    transport = Transport()
    summary, _ = _tick(tmp_path, _config(tmp_path, f"DEADMAN_URL={URL}\n"), transport, sweep=None)
    assert summary["alerts"] == "skipped" and len(transport.calls) == 1


def test_an_unwritable_state_dir_is_degraded_not_a_crash(tmp_path: Path) -> None:
    (tmp_path / "state").write_text("a file where the directory should be")
    summary, rc = _tick(tmp_path, _config(tmp_path, f"DEADMAN_URL={URL}\n"), Transport())
    assert summary["heartbeat"] == "sent" and summary["state"] == "unwritable" and rc == 3


# -- the command line -------------------------------------------------------------------


def _dirs(tmp_path: Path) -> list[str]:
    for name in ("run", "sup", "acceptance"):
        (tmp_path / name).mkdir(exist_ok=True)
    return [
        "--state-dir",
        str(tmp_path / "state"),
        "--run-dir",
        str(tmp_path / "run"),
        "--supervised-dir",
        str(tmp_path / "sup"),
        "--acceptance-dir",
        str(tmp_path / "acceptance"),
        "--notify-config",
        str(tmp_path / "notify.env"),
    ]


def test_main_prints_a_summary_and_books_the_sweep(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    transport = Transport()
    monkeypatch.setattr(dm, "urllib_ping", transport)
    cfg = _config(tmp_path, f"DEADMAN_URL={URL}\n")
    rc = dm.main([*_dirs(tmp_path), "--deadman-config", str(cfg)])
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert rc == 0 and out["heartbeat"] == "sent" and out["alerts"]["degraded"] is False
    assert (tmp_path / "state" / dm.ALERT_BOOK_NAME).is_file()
    assert (tmp_path / "state" / dm.STATE_NAME).is_file()
    assert len(transport.calls) == 1
    assert "SECRET" not in json.dumps(out)


def test_main_dry_run_touches_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    transport = Transport()
    monkeypatch.setattr(dm, "urllib_ping", transport)
    cfg = _config(tmp_path, f"DEADMAN_URL={URL}\n")
    rc = dm.main([*_dirs(tmp_path), "--deadman-config", str(cfg), "--dry-run"])
    assert rc == 0 and transport.calls == [] and not (tmp_path / "state").exists()
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["dry_run"] is True


def test_main_defaults_resolve_at_call_time(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("TREX_DESK_STATE", str(tmp_path / "desk-state"))
    monkeypatch.setenv("TREX_ACCEPTANCE_DIR", str(tmp_path / "acc"))
    assert dm.default_state_dir() == tmp_path / "desk-state" / "alerts"
    assert dm.default_acceptance_dir() == tmp_path / "acc"
    monkeypatch.delenv("TREX_ACCEPTANCE_DIR")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert dm.default_acceptance_dir() == tmp_path / ".local/state/trex/acceptance"


def test_the_default_run_dir_is_the_desks(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from tree_options.desk.enter import execution_directory

    monkeypatch.setenv("HOME", str(tmp_path))
    assert (
        dm.default_run_dir() == execution_directory() == tmp_path / ".local/state/trex/desk-paper"
    )
    monkeypatch.setenv("TREX_DESK_RUN_DIR", str(tmp_path / "run"))
    assert dm.default_run_dir() == execution_directory() == tmp_path / "run"


def test_main_does_not_import_the_desk_entry_module(tmp_path: Path) -> None:
    """``desk.enter`` reads the desk universe config at import; the sweep
    must not depend on it (the release unit only needs its own files)."""
    import subprocess
    import sys

    probe = (
        "import sys, tree_options.trex.deadman as dm;"
        f" dm.main(['--state-dir', {str(tmp_path / 's')!r}, '--run-dir', {str(tmp_path)!r},"
        f" '--supervised-dir', {str(tmp_path)!r}, '--acceptance-dir', {str(tmp_path)!r},"
        f" '--deadman-config', {str(tmp_path / 'none.env')!r},"
        f" '--notify-config', {str(tmp_path / 'none.env')!r}, '--dry-run']);"
        " sys.exit(1 if 'tree_options.desk.enter' in sys.modules else 0)"
    )
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "PYTHONPATH": str(REPO / "src")}
    done = subprocess.run(
        [sys.executable, "-B", "-c", probe], cwd=tmp_path, env=env, capture_output=True, timeout=120
    )
    assert done.returncode == 0, done.stderr.decode()[-2000:]


# -- the release-gated unit ---------------------------------------------------------------

SERVICE = REPO / "deploy/product/trex-deadman.service.in"
TIMER = REPO / "deploy/product/trex-deadman.timer.in"


def _directives(text: str) -> list[tuple[str, str]]:
    pairs = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", ";", "[")):
            continue
        key, _, value = line.partition("=")
        pairs.append((key, value))
    return pairs


def test_the_unit_is_gated_on_the_release_identity_first() -> None:
    pairs = _directives(SERVICE.read_text())
    commands = [(k, v) for k, v in pairs if k in release.EXEC_KEYS and v]
    assert commands[0] == (
        "ExecStartPre",
        "@SOURCE@/.venv/bin/python -m tree_options.trex.premarket --check",
    )
    assert release._unit_commands(SERVICE.read_text()) == [
        "tree_options.trex.premarket",
        "tree_options.trex.deadman",
    ]
    for key, value in commands:
        assert value.split()[0] == "@SOURCE@/.venv/bin/python", (key, value)
    assert ("WorkingDirectory", "@SOURCE@") in pairs
    assert ("Environment", "PYTHONPATH=@SOURCE@/src") in pairs
    assert ("Environment", "TREX_RELEASE_DIR=@RELEASE_DIR@") in pairs
    assert ("Environment", "TREX_PREMARKET_RUNS=@PREMARKET_RUNS@") in pairs
    assert ("Environment", "TREX_DESK_STATE=@DESK_STATE@") in pairs


def test_the_unit_writes_only_its_desk_state_and_stays_sandboxed() -> None:
    pairs = _directives(SERVICE.read_text())
    writes = [v for k, v in pairs if k == "ReadWritePaths"]
    assert writes == ["@DESK_STATE@"]
    for setting in (
        ("ProtectHome", "read-only"),
        ("ProtectSystem", "strict"),
        ("NoNewPrivileges", "true"),
        ("PrivateTmp", "true"),
        ("UMask", "0077"),
        ("Type", "oneshot"),
        ("SuccessExitStatus", "3"),
        ("Slice", "host-work.slice"),
    ):
        assert setting in pairs, setting
    families = dict(pairs)["RestrictAddressFamilies"].split()
    assert sorted(families) == ["AF_INET", "AF_INET6", "AF_UNIX"]
    text = SERVICE.read_text()
    assert "host-work" not in text.replace("host-work.slice", "")
    assert "host-test" not in text and "--armed" not in text


def test_every_placeholder_is_a_release_manifest_key() -> None:
    import re

    keys = {k.upper() for k in (*release.STATE_KEYS, "source", "release_dir", "release_head")}
    for path in (SERVICE, TIMER):
        for name in re.findall(r"@([A-Z_]+)@", path.read_text()):
            assert name in keys, (path.name, name)


def test_the_timer_sweeps_often_enough_for_the_approval_window() -> None:
    from tree_options.trex.alert_policy import APPROVAL_WARN_S

    pairs = dict(_directives(TIMER.read_text()))
    every = int(pairs["OnUnitInactiveSec"].removesuffix("s"))
    # one tick plus the gate and sweep (well under 30 s) must fit twice in
    # the warning window, so every expiring approval is seen at least once
    assert 2 * every <= APPROVAL_WARN_S
    assert pairs["Persistent"] == "false"
    assert dict(_directives(SERVICE.read_text()))["TimeoutStartSec"] == "60"
