"""Activation installs exactly the qualified bytes, proves them, or puts every byte back.

The fake host reads the effective unit from the files on disk the way systemd
merges them (fragment, then drop-ins in name order; an empty assignment resets
a list), so a drop-in that reopens writes or names blocked storage is visible
to the proof exactly as it would be to the service manager.
"""

from __future__ import annotations

import fcntl
import json
from collections.abc import Sequence
from pathlib import Path

import pytest

from tests.unit.test_trex_product_release import (
    _config,
    _copy_templates,
    _fake_git,
    _populate,
    _release_dir,
)
from tree_options.trex import release
from tree_options.trex.release_activate import (
    DROPIN,
    ActivationRefused,
    Host,
    apply,
    prepare,
    rollback,
)

HEAD = "a" * 40
LISTS = {"ExecStartPre", "ExecStart", "ReadWritePaths"}
SINGLE = {"WorkingDirectory", "ProtectHome", "ProtectSystem"}
OLD_RELEASE = b"[Service]\nWorkingDirectory=/media/raid/releases/old/source\n"


class FakeHost(Host):
    def __init__(self, root: Path, recovery: bool = True) -> None:
        super().__init__(root, self._systemctl, self._environ_of, self._http)
        self.calls: list[list[str]] = []
        self.running: dict[str, int] = {}
        self.env_files: dict[str, dict[str, str]] = {}
        self.web_read_only = recovery

    def _files(self, unit: str) -> list[Path]:
        fragment = self.config_root / unit
        folder = self.config_root / f"{unit}.d"
        dropins = sorted(folder.glob("*.conf")) if folder.is_dir() else []
        return ([fragment] if fragment.is_file() else []) + dropins

    def effective(self, unit: str) -> dict[str, str]:
        files = self._files(unit)
        lists: dict[str, list[str]] = {key: [] for key in LISTS}
        single: dict[str, str] = {}
        env: dict[str, str] = {}
        for path in files:
            for line in path.read_text().splitlines():
                key, sep, value = line.strip().partition("=")
                if not sep:
                    continue
                if key in LISTS:
                    lists[key] = [] if value == "" else [*lists[key], value]
                elif key in SINGLE:
                    single[key] = value
                elif key == "Environment":
                    name, _, setting = value.partition("=")
                    env[name] = setting
        shown = {
            "LoadState": "loaded" if files else "not-found",
            "DropInPaths": " ".join(str(p) for p in files if p.suffix == ".conf"),
            "Environment": " ".join(f"{k}={v}" for k, v in env.items()),
            "MainPID": str(self.running.get(unit, 0)),
            **{key: " ".join(values) for key, values in lists.items()},
            **single,
        }
        return shown

    def _systemctl(self, args: Sequence[str]) -> str:
        self.calls.append(list(args))
        if args[0] == "show":
            return "\n".join(f"{k}={v}" for k, v in self.effective(args[1]).items())
        if args[0] == "restart":
            self.running[args[1]] = 4242
        return ""

    def _environ_of(self, pid: int) -> dict[str, str]:
        unit = next(u for u, p in self.running.items() if p == pid)
        env = dict(item.split("=", 1) for item in self.effective(unit)["Environment"].split())
        return {**env, **self.env_files.get(unit, {})}

    def _http(self, method: str, url: str) -> tuple[int, bytes]:
        if method == "GET":
            return 200, json.dumps({"recovery_read_only": self.web_read_only}).encode()
        if self.web_read_only:
            return 403, b'{"reason":"recovery_read_only"}'
        return 202, b"{}"


@pytest.fixture
def rig(tmp_path, monkeypatch):
    source = tmp_path / "source"
    _populate(source)
    _copy_templates(source)
    config = _config(tmp_path, source)
    config["recovery_read_only"] = True
    config["blocked_storage_roots"] = ["/media/raid"]
    monkeypatch.setattr(release, "_git", _fake_git(tmp_path / "main", HEAD))
    monkeypatch.setattr(release, "_detached", lambda path: True, raising=False)
    monkeypatch.setattr(release, "_environment", lambda path: {"python": "frozen", "packages": []})
    manifest = release.seal(config)
    release_dir = _release_dir(tmp_path / "release", manifest)
    release.render_units(manifest, release_dir / "units")
    receipt = tmp_path / "m0.receipt"
    receipt.write_text(f"head={HEAD}\nlog=/x\nexit=0\n")
    root = tmp_path / "systemd"
    (root / "trex-web.service.d").mkdir(parents=True)
    (root / "trex-web.service").write_text(
        "[Service]\nProtectHome=read-only\nProtectSystem=strict\n"
        "ReadWritePaths=/home/x/state\nExecStart=/old/python -m tree_options.trex_web\n"
    )
    (root / "trex-web.service.d/95-release.conf").write_bytes(OLD_RELEASE)
    (root / "trex-web.service.d/96-narrow-writes.conf").write_text(
        "[Service]\nReadWritePaths=\nReadWritePaths=/home/x/commands\n"
    )
    (root / "trex-deadman.service").write_bytes(OLD_RELEASE)
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    return FakeHost(root), release_dir, receipt, evidence


def _tree(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()
    }


NAMES = ["trex-web-release.conf", "trex-deadman.service", "trex-deadman.timer"]


def test_activation_installs_the_rendered_bytes_and_retires_the_old_release(rig):
    host, release_dir, receipt, evidence = rig
    prepare(host, release_dir, receipt, evidence, NAMES)
    done = apply(host, release_dir, receipt, evidence, restart=["trex-web.service"])
    assert done["installed"] is True
    units = release_dir / "units"
    web = host.config_root / "trex-web.service.d"
    assert (web / DROPIN).read_bytes() == (units / "trex-web-release.conf").read_bytes()
    assert not (web / "95-release.conf").exists(), "the old release drop-in is retired"
    assert (web / "96-narrow-writes.conf").exists(), "hand-written drop-ins stay"
    deadman = host.config_root / "trex-deadman.service"
    assert deadman.read_bytes() == (units / "trex-deadman.service").read_bytes()
    assert host.effective("trex-web.service")["ReadWritePaths"] == ""
    assert (evidence / f"activation-{HEAD[:12]}.applied.json").is_file()


def test_rollback_restores_every_byte_after_a_completed_activation(rig):
    host, release_dir, receipt, evidence = rig
    before = _tree(host.config_root)
    prepare(host, release_dir, receipt, evidence, NAMES)
    apply(host, release_dir, receipt, evidence, restart=["trex-web.service"])
    rollback(host, release_dir, evidence)
    assert _tree(host.config_root) == before


def test_a_gate_receipt_for_another_head_refuses_before_any_write(rig):
    host, release_dir, receipt, evidence = rig
    receipt.write_text(f"head={'b' * 40}\nexit=0\n")
    before = _tree(host.config_root)
    with pytest.raises(ActivationRefused, match="gate_not_passed_for_release_head"):
        prepare(host, release_dir, receipt, evidence, NAMES)
    assert _tree(host.config_root) == before and not list(evidence.iterdir())


def test_release_units_that_differ_from_a_fresh_render_are_refused(rig):
    host, release_dir, receipt, evidence = rig
    unit = release_dir / "units/trex-web-release.conf"
    unit.write_text(unit.read_text() + "ProtectHome=no\n")
    with pytest.raises(ActivationRefused, match="rendered_units_drifted"):
        prepare(host, release_dir, receipt, evidence, NAMES)


def test_a_broker_owner_is_never_an_activation_target(rig, tmp_path):
    host, release_dir, receipt, evidence = rig
    template = tmp_path / "source/deploy/product/trex-monitor-release.conf.in"
    template.write_text("[Service]\n# would re-pin the exit machine\n")
    manifest = json.loads((release_dir / "config.json").read_text())
    other = _release_dir(tmp_path / "release-2", manifest)
    release.render_units(manifest, other / "units")
    with pytest.raises(ActivationRefused, match=r"broker_unit_refused:trex-monitor\.service"):
        prepare(host, other, receipt, evidence, ["trex-monitor-release.conf"])


def test_a_dropin_written_after_prepare_refuses_apply_with_nothing_changed(rig):
    host, release_dir, receipt, evidence = rig
    prepare(host, release_dir, receipt, evidence, NAMES)
    late = host.config_root / "trex-web.service.d/97-late.conf"
    late.write_text("[Service]\nProtectHome=no\n")
    before = _tree(host.config_root)
    with pytest.raises(ActivationRefused, match="prepared_state_changed"):
        apply(host, release_dir, receipt, evidence, restart=["trex-web.service"])
    assert _tree(host.config_root) == before


@pytest.mark.parametrize(
    ("setup", "reason"),
    [
        pytest.param("web_writable_mode", "web_runtime_mode_differs", id="viewer-not-read-only"),
        pytest.param("late_grant", "web_sandbox_writable", id="later-dropin-reopens-writes"),
        pytest.param("raid_env", "blocked_storage_in_effective_unit", id="raid-still-effective"),
        pytest.param("env_file", "process_environment_differs", id="environment-file-override"),
        pytest.param("receipt", "Is a directory", id="receipt-write-fails"),
    ],
)
def test_any_failed_proof_restores_every_byte(rig, setup, reason):
    host, release_dir, receipt, evidence = rig
    web = host.config_root / "trex-web.service.d"
    if setup == "late_grant":
        (web / "zzz-late.conf").write_text("[Service]\nReadWritePaths=/home/x/anything\n")
    elif setup == "raid_env":
        (web / "50-old.conf").write_text("[Service]\nEnvironment=OLD=/media/raid/state\n")
    elif setup == "receipt":
        (evidence / f"activation-{HEAD[:12]}.applied.json").mkdir()
    before = _tree(host.config_root)
    prepare(host, release_dir, receipt, evidence, NAMES)
    if setup == "web_writable_mode":
        host.web_read_only = False
    elif setup == "env_file":
        host.env_files["trex-web.service"] = {"RESEARCH_WORKSPACE_DIR": "/tmp/elsewhere"}
    with pytest.raises((ActivationRefused, OSError), match=reason):
        apply(host, release_dir, receipt, evidence, restart=["trex-web.service"])
    assert _tree(host.config_root) == before
    restarts = [call for call in host.calls if call[0] == "restart"]
    assert len(restarts) == 2, "restarted on the release, then again on the restored bytes"


def test_concurrent_activations_are_serialized(rig):
    host, release_dir, receipt, evidence = rig
    prepare(host, release_dir, receipt, evidence, NAMES)
    with open(evidence / "activate.lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        with pytest.raises(ActivationRefused, match="activation_in_progress"):
            apply(host, release_dir, receipt, evidence, restart=["trex-web.service"])
